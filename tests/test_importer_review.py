"""Synthetic transaction import tests for AI Personal Finance Assistant.

Copy this file into PROJECT_ROOT/tests/, then run from PROJECT_ROOT:
    python -m pytest -q tests/test_importer_review.py -rx

All fixtures use fake transactions and an isolated temporary SQLite database.
Known bugs are marked xfail so the suite is usable before the fixes land.
"""
from __future__ import annotations

import inspect
import zipfile
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

from backend import db
from backend import importer
from backend.importer.base import Row, norm_amount
from backend.main import app, import_commit


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(db, 'DB_DIR', tmp_path)
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.db')
    db.init_db(db.DB_PATH)
    return tmp_path


def wechat_file(root: Path) -> Path:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(['账单说明'])
    sheet.append(['2026-09 仅为模拟数据'])
    sheet.append(['无个人隐私信息'])
    sheet.append(['交易时间', '交易类型', '交易对方', '商品', '收/支', '金额(元)',
                  '支付方式', '当前状态', '交易单号', '商户单号', '备注'])
    sheet.append(['2026-09-01 10:23:00', '餐饮', '模拟商户A', '午餐', '支出', '¥24.50',
                  '零钱', '交易成功', 'mock-wx-001', '', ''])
    sheet.append(['2026-09-01 10:24:00', '餐饮', '模拟商户B', '取消订单', '支出', '¥7.00',
                  '零钱', '交易关闭', 'mock-wx-002', '', ''])
    sheet.append(['2026-09-01 10:25:00', '转账', '模拟商户C', '收款', '收入', '¥30.00',
                  '零钱', '交易成功', 'mock-wx-003', '', ''])
    output = root / '微信模拟账单.xlsx'
    workbook.save(output)
    return output


def alipay_file(root: Path, *, extra_rows: list[str] | None = None) -> Path:
    heading = '收/支,交易时间,交易分类,交易对方,商品说明,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注'
    rows = ['支出,2026-09-03 12:30:00,餐饮,模拟商户D,咖啡,18.80,余额宝,交易成功,mock-ali-001,,']
    rows += extra_rows or []
    output = root / '支付宝模拟账单.csv'
    output.write_bytes(('支付宝交易记录明细查询\n' + heading + '\n' + '\n'.join(rows) + '\n').encode('gb18030'))
    return output


def count_rows() -> int:
    conn = db.get_conn()
    try:
        return conn.execute('SELECT COUNT(*) FROM transactions_raw').fetchone()[0]
    finally:
        conn.close()


def test_wechat_insert_and_drop_closed(isolated_db: Path):
    result = importer.import_file(wechat_file(isolated_db))
    assert (result.inserted, result.skipped, result.failed) == (2, 0, 0)
    assert count_rows() == 2


def test_wechat_second_import_is_idempotent(isolated_db: Path):
    path = wechat_file(isolated_db)
    importer.import_file(path)
    result = importer.import_file(path)
    assert (result.inserted, result.skipped) == (0, 2)
    assert count_rows() == 2


def test_alipay_gb18030_csv_and_zip(isolated_db: Path):
    path = alipay_file(isolated_db)
    result = importer.import_file(path)
    assert (result.inserted, result.skipped) == (1, 0)
    zpath = isolated_db / '支付宝模拟账单.zip'
    with zipfile.ZipFile(zpath, 'w') as archive:
        archive.writestr('records.csv', path.read_bytes())
    repeat = importer.import_file(zpath)
    assert repeat.inserted == 0
    assert repeat.skipped == 1


def test_alipay_summary_footer_is_not_reported_as_invalid(isolated_db: Path):
    path = alipay_file(isolated_db, extra_rows=[
        '------------------------------------------------------------------------------------',
        '共1笔记录',
        '已收入:0笔,0.00元',
        '待收入:0笔,0.00元',
        '已支出:1笔,18.80元',
        '待支出:0笔,0.00元',
        '导出时间:[2026-10-25 19:21:56]    用户:模拟用户',
    ])
    issues = importer.ParseIssues()
    rows = importer.parse_file(path, issues=issues)
    assert len(rows) == 1
    assert issues.failed == 0
    assert issues.warnings == []


def test_deleted_transaction_stays_hidden_after_duplicate_import(isolated_db: Path):
    path = alipay_file(isolated_db)
    first = importer.import_file(path)
    assert first.inserted == 1

    conn = db.get_conn()
    try:
        tx_id = conn.execute('SELECT id FROM transactions_raw').fetchone()[0]
    finally:
        conn.close()

    from backend import overrides
    overrides.delete_transaction(tx_id, deleted_by='test')

    conn = db.get_conn()
    try:
        assert conn.execute('SELECT COUNT(*) FROM transactions_raw').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM tx_deletions').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM v_tx').fetchone()[0] == 0
    finally:
        conn.close()

    repeat = importer.import_file(path)
    assert (repeat.inserted, repeat.skipped) == (0, 1)
    conn = db.get_conn()
    try:
        assert conn.execute('SELECT COUNT(*) FROM v_tx').fetchone()[0] == 0
    finally:
        conn.close()


def test_delete_api_hides_transaction_from_detail_and_calendar(isolated_db: Path):
    path = alipay_file(isolated_db)
    importer.import_file(path)
    conn = db.get_conn()
    try:
        tx_id = conn.execute('SELECT id FROM transactions_raw').fetchone()[0]
    finally:
        conn.close()

    with TestClient(app) as client:
        assert client.get(f'/api/tx/{tx_id}').status_code == 200
        deleted = client.delete(f'/api/tx/{tx_id}')
        assert deleted.status_code == 200
        assert deleted.json() == {'tx_id': tx_id, 'deleted': True}
        assert client.get(f'/api/tx/{tx_id}').status_code == 404
        calendar = client.get(
            '/api/report/calendar',
            params={'type': 'expense', 'period': 'month', 'value': '2026-09'},
        )
        assert calendar.status_code == 200
        assert calendar.json()['days'] == []


def test_import_failure_rolls_back_all_rows(isolated_db: Path, monkeypatch):
    original = Row.to_sql_tuple
    counter = {'n': 0}

    def fail_on_second_row(self: Row, batch_id: str):
        counter['n'] += 1
        if counter['n'] == 2:
            raise RuntimeError('mock failure in second row')
        return original(self, batch_id)

    monkeypatch.setattr(Row, 'to_sql_tuple', fail_on_second_row)
    with pytest.raises(ValueError, match='整体回滚'):
        importer.import_file(wechat_file(isolated_db))
    assert count_rows() == 0


def test_backend_preview_and_commit(isolated_db: Path):
    path = wechat_file(isolated_db)
    with TestClient(app) as client:
        with path.open('rb') as stream:
            response = client.post('/api/import/preview', files={'file': (path.name, stream)})
        assert response.status_code == 200, response.text
        assert response.json()['total'] == 2
        with path.open('rb') as stream:
            response = client.post('/api/import/commit', files={'file': (path.name, stream)})
        assert response.status_code == 200, response.text
        assert response.json()['inserted'] == 2
        assert count_rows() == 2


def test_zero_amount_is_not_reported_as_duplicate(isolated_db: Path):
    path = alipay_file(isolated_db, extra_rows=[
        '支出,2026-09-03 12:31:00,优惠,模拟商户E,抵扣,0.00,余额宝,交易成功,mock-ali-zero,,'
    ])
    result = importer.import_file(path)
    assert result.skipped == 0, 'Zero-value record is not a duplicate'
    assert result.failed >= 1  # or an explicit zero-amount policy with separate count


def test_invalid_amount_has_explicit_failure_count(isolated_db: Path):
    path = alipay_file(isolated_db, extra_rows=[
        '支出,2026-09-03 12:32:00,餐饮,模拟商户F,未知金额,not-a-number,余额宝,交易成功,mock-ali-bad,,'
    ])
    result = importer.import_file(path)
    assert result.failed >= 1


def test_commit_api_exposes_alipay_password_field():
    assert 'alipay_password' in inspect.signature(import_commit).parameters


def test_nonfinite_negative_and_tiny_amounts_are_invalid():
    for value in ('0', '0.00', '-1.00', 'NaN', 'inf', '-Infinity', 'not-a-number', '0.001'):
        assert norm_amount(value) is None, value
    assert norm_amount('¥1,234.567') == 1234.57


def test_preview_discloses_invalid_rows(isolated_db: Path):
    path = alipay_file(isolated_db, extra_rows=[
        '支出,2026-09-03 12:32:00,餐饮,模拟商户F,未知金额,not-a-number,余额宝,交易成功,mock-ali-bad,,'
    ])
    with TestClient(app) as client:
        with path.open('rb') as stream:
            response = client.post('/api/import/preview', files={'file': (path.name, stream)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['total'] == 1
    assert body['warnings'] and '金额无效' in body['warnings'][0]


def test_wechat_invalid_rows_are_counted(isolated_db: Path):
    path = wechat_file(isolated_db)
    book = openpyxl.load_workbook(path)
    book.active.append(['2026-09-03 00:00:00', '餐饮', '模拟商户', '坏数据', '支出', '-1.00', '零钱', '交易成功', 'mock-bad', '', ''])
    book.save(path)
    r = importer.import_file(path)
    assert (r.inserted, r.skipped, r.failed) == (2, 0, 1)
    assert r.first_error and '金额无效' in r.first_error
    assert count_rows() == 2


def test_db_constraint_failure_rolls_back_not_skipped(isolated_db: Path, monkeypatch):
    original_parse = importer.parse_file

    def corrupt_second_row(path, platform_id=None, alipay_password=None, issues=None):
        rows = original_parse(path, platform_id, alipay_password, issues=issues)
        rows[1].amount = 0.0  # simulate unexpected invalid data reaching DB layer
        return rows

    monkeypatch.setattr(importer, 'parse_file', corrupt_second_row)
    with pytest.raises(ValueError, match='整体回滚'):
        importer.import_file(wechat_file(isolated_db))
    assert count_rows() == 0
