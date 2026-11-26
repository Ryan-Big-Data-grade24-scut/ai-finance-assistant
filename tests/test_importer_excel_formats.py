from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

from backend import db, importer
from backend.main import app
from backend.importer.base import ParseIssues


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    db.init_db(db.DB_PATH)
    return tmp_path


def make_alipay_xlsx(path: Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "支付宝交易记录"
    ws.append(["支付宝交易记录明细查询"])
    ws.append(["账号:[已脱敏]"])
    ws.append(["起始日期:[2026-09-01] 终止日期:[2026-10-02]"])
    ws.append(["交易记录明细列表"])
    ws.append([
        "交易号", "商家订单号", "交易创建时间", "付款时间", "最近修改时间",
        "交易来源地", "类型", "交易对方", "商品名称", "金额（元）", "收/支",
        "交易状态", "服务费（元）", "成功退款（元）", "备注", "资金状态",
    ])
    ws.append([
        "mock-alipay-001", "mock-order-001", "2026-09-30 14:24:39",
        "2026-09-30 14:24:40", "2026-09-30 14:24:40",
        "其他（包括外部商家）", "即时到账交易", "模拟商户", "模拟商品",
        100.00, "支出", "交易成功", 0, 30.00, "", "已支出",
    ])
    ws.append([
        "mock-alipay-closed", "mock-order-closed", "2026-09-30 15:00:00",
        "2026-09-30 15:00:01", "2026-09-30 15:00:01",
        "淘宝", "支付宝担保交易", "模拟商户", "已关闭商品",
        12.00, "支出", "交易关闭", 0, 0, "", "未支出",
    ])
    ws.append([
        "mock-alipay-invalid", "mock-order-invalid", "2026-09-30 16:00:00",
        "2026-09-30 16:00:01", "2026-09-30 16:00:01",
        "淘宝", "支付宝担保交易", "模拟商户", "无效金额商品",
        "not-a-number", "支出", "交易成功", 0, 0, "", "未支出",
    ])
    ws.append(["-" * 80])
    ws.append(["共3笔记录"])
    wb.save(path)
    wb.close()
    return path


def make_wechat_xlsx(path: Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["微信支付账单明细"])
    ws.append(["昵称：[模拟用户]"])
    ws.append(["导出说明"])
    ws.append(["共1笔记录"])
    ws.append(["收入：0笔 0.00元"])
    ws.append(["支出：1笔 8.00元"])
    ws.append(["中性交易：0笔 0.00元"])
    ws.append(["注："])
    ws.append(["交易时间", "交易类型", "交易对方", "商品", "收/支", "金额(元)",
               "支付方式", "当前状态", "交易单号", "商户单号", "备注"])
    ws.append([
        "2026-10-01 12:00:00", "商户消费", "模拟商户", "模拟午餐", "支出",
        8.00, "零钱", "支付成功", "mock-wechat-001", "mock-merchant-001", "/",
    ])
    wb.save(path)
    wb.close()
    return path


WALLET_HEADER = [
    "订单号", "交易时间", "交易说明", "备注",
    "操作人昵称", "操作人姓名", "收入金额", "支出金额",
]


def _force_sheet_dimension(path: Path, ref: str = "A1") -> Path:
    """把工作簿声明的尺寸改错，复现真实小荷包文件 A1（1×1）的假尺寸。"""
    tmp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                tag = f'<dimension ref="{ref}"/>'.encode()
                if b"<dimension" in data:
                    data = re.sub(rb'<dimension ref="[^"]*"\s*/>', tag, data, count=1)
                else:
                    data = re.sub(rb"(<worksheet[^>]*>)", rb"\1" + tag, data, count=1)
            dst.writestr(item, data)
    tmp.replace(path)
    return path


def make_wallet_xlsx(path: Path, *, account_id: str = "0000000000000001",
                     order_prefix: str = "WALLET", wrong_dimension: bool = True) -> Path:
    """虚构的小荷包账单：12 行表头前的元信息 + 两列金额 + 假尺寸。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "小荷包收支明细"
    ws.append(["支付宝小荷包余额收支明细"])
    ws.append(["支付宝小荷包名称：[模拟小荷包]"])
    ws.append([f"支付宝小荷包账户ID：{account_id}"])
    ws.append(["支付宝小荷包创建时间：[2026-01-01 00:00:00]"])
    ws.append(["起始时间：[2026-09-01] 终止时间：[2026-10-01]"])
    for line in ("特别提示：", "备注：", "说明一", "说明二", "说明三", "说明四", "说明五"):
        ws.append([line])
    ws.append(WALLET_HEADER)
    ws.append([f"{order_prefix}-001", "2026-09-02 09:00:00", "转入", "",
               "模拟昵称", "模拟姓名", 100.00, ""])
    ws.append([f"{order_prefix}-002", "2026-09-03 12:30:00", "消费", "",
               "模拟昵称", "模拟姓名", "", 8.50])
    ws.append([f"{order_prefix}-003", "2026-09-04 08:00:00", "支付", "",
               "模拟昵称", "模拟姓名", 5.00, 5.00])
    ws.append([f"{order_prefix}-004", "2026-09-05 08:00:00", "支付", "",
               "模拟昵称", "模拟姓名", "", ""])
    ws.append([f"{order_prefix}-005", "不是时间", "消费", "",
               "模拟昵称", "模拟姓名", "", 3.00])
    ws.append(["", "2026-09-06 08:00:00", "消费", "",
               "模拟昵称", "模拟姓名", "", 3.00])
    ws.append([f"{order_prefix}-007", "2026-09-07 08:00:00", "转出", "",
               "模拟昵称", "模拟姓名", "-", 9.90])
    ws.append([f"{order_prefix}-{'8' * 53}", "2026-09-08 08:00:00", "退款-模拟", "复合订单号",
               "模拟昵称", "模拟姓名", 2.20, ""])
    wb.save(path)
    wb.close()
    if wrong_dimension:
        _force_sheet_dimension(path)
    return path


def test_alipay_xlsx_detected_by_content_and_parsed(isolated_db: Path):
    path = make_alipay_xlsx(isolated_db / "alipay_record_export.xlsx")
    assert importer.detect(path) == ("alipay", 0.99)

    issues = ParseIssues()
    rows = importer.parse_file(path, issues=issues)
    assert len(rows) == 1
    assert rows[0].platform_id == "alipay"
    assert rows[0].tx_no == "mock-alipay-001"
    assert rows[0].tx_time == "2026-09-30 14:24:39"
    assert rows[0].amount == 100.00
    assert rows[0].raw["成功退款（元）"] == "30"
    assert rows[0].raw["交易来源地"] == "其他（包括外部商家）"
    assert issues.failed == 1
    assert "金额无效" in (issues.first_error or "")


def test_alipay_xlsx_import_preserves_refund_field_and_is_idempotent(isolated_db: Path):
    path = make_alipay_xlsx(isolated_db / "alipay_record_export.xlsx")
    first = importer.import_file(path)
    assert (first.inserted, first.skipped, first.failed) == (1, 0, 1)

    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT raw_json, amount, tx_no FROM transactions_raw WHERE tx_no='mock-alipay-001'"
        ).fetchone()
        assert row is not None
        raw = json.loads(row["raw_json"])
        assert raw["成功退款（元）"] == "30"
        assert raw["资金状态"] == "已支出"
    finally:
        conn.close()

    second = importer.import_file(path)
    assert (second.inserted, second.skipped) == (0, 1)


def test_wechat_xlsx_detection_uses_header_not_english_filename(isolated_db: Path):
    path = make_wechat_xlsx(isolated_db / "wechat_export.xlsx")
    assert importer.detect(path) == ("wechat", 0.99)
    rows = importer.parse_file(path)
    assert len(rows) == 1
    assert rows[0].source == "微信支付"
    assert rows[0].amount == 8.00


def test_alipay_xlsx_missing_header_is_not_misclassified_as_wechat(isolated_db: Path):
    path = isolated_db / "random_export.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["not a bill", "value"])
    wb.save(path)
    wb.close()
    with pytest.raises(ValueError, match="无法识别账单平台"):
        importer.detect(path)



def test_alipay_xlsx_preview_and_commit_api(isolated_db: Path):
    path = make_alipay_xlsx(isolated_db / "alipay_record_export.xlsx")
    with TestClient(app) as client:
        with path.open("rb") as stream:
            preview = client.post(
                "/api/import/preview",
                files={"file": (path.name, stream, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            )
        assert preview.status_code == 200, preview.text
        assert preview.json()["total"] == 1
        assert preview.json()["amount_sum"] == 100.0

        with path.open("rb") as stream:
            committed = client.post(
                "/api/import/commit",
                files={"file": (path.name, stream, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            )
        assert committed.status_code == 200, committed.text
        assert committed.json()["inserted"] == 1
        assert committed.json()["failed"] == 1

    conn = db.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0] == 1
    finally:
        conn.close()


def test_wallet_xlsx_detected_by_header_despite_wrong_dimensions(isolated_db: Path):
    path = make_wallet_xlsx(isolated_db / "余额收支明细_随便起的名字.xlsx")
    # 文件确实把尺寸错报成 A1（1×1），read_only 不修正就只能读到第 1 行。
    with zipfile.ZipFile(path) as zf:
        assert b'<dimension ref="A1"/>' in zf.read("xl/worksheets/sheet1.xml")

    assert importer.detect(path) == ("alipay_wallet", 0.99)
    issues = ParseIssues()
    rows = importer.parse_file(path, issues=issues)
    assert len(rows) == 4
    assert issues.failed == 4
    assert all(row.platform_id == "alipay_wallet" and row.source == "支付宝小荷包" for row in rows)


def test_wallet_xlsx_maps_two_amount_columns_and_keeps_raw(isolated_db: Path):
    path = make_wallet_xlsx(isolated_db / "wallet.xlsx")
    rows = importer.parse_file(path)
    by_no = {row.tx_no: row for row in rows}

    assert (by_no["WALLET-001"].direction, by_no["WALLET-001"].amount) == ("收入", 100.00)
    assert (by_no["WALLET-002"].direction, by_no["WALLET-002"].amount) == ("支出", 8.50)
    # 支出侧写成占位符 '-'，仍按另一列判定。
    assert (by_no["WALLET-007"].direction, by_no["WALLET-007"].amount) == ("支出", 9.90)
    assert by_no["WALLET-001"].raw["收入金额"] == "100"
    assert by_no["WALLET-001"].raw["支出金额"] == ""
    # 操作人姓名/昵称不充当交易对方；订单号保留原文本（含长编号）。
    assert by_no["WALLET-001"].counterparty == ""
    assert len(by_no["WALLET-" + "8" * 53].tx_no) > 50
    # 账户 ID 不进任何持久字段，只留摘要供分域去重。
    assert by_no["WALLET-001"].wallet_domain
    assert "0000000000000001" not in json.dumps(by_no["WALLET-001"].raw)


def test_wallet_xlsx_dedupe_is_wallet_scoped_and_platform_local(isolated_db: Path):
    first = make_wallet_xlsx(isolated_db / "a.xlsx", account_id="0000000000000001")
    second = make_wallet_xlsx(isolated_db / "b.xlsx", account_id="0000000000000002")
    result_a = importer.import_file(first)
    result_b = importer.import_file(second)
    # 两个钱包的订单号相同，但账户域不同 → 不互相吞掉。
    assert (result_a.inserted, result_b.inserted) == (4, 4)

    # 普通支付宝里出现同样的订单号，也不能和小荷包共用一个去重键。
    alipay_path = make_alipay_xlsx(isolated_db / "alipay.xlsx")
    wb = openpyxl.load_workbook(alipay_path)
    ws = wb.active
    ws["A6"] = "WALLET-001"
    wb.save(alipay_path)
    wb.close()
    assert importer.import_file(alipay_path).inserted == 1

    conn = db.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0] == 9
        assert conn.execute(
            "SELECT COUNT(DISTINCT platform_id) FROM transactions_raw"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_wallet_xlsx_preview_does_not_write_and_commit_is_idempotent(isolated_db: Path):
    path = make_wallet_xlsx(isolated_db / "余额收支明细.xlsx")
    with TestClient(app) as client:
        with path.open("rb") as stream:
            preview = client.post(
                "/api/import/preview",
                files={"file": (path.name, stream, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            )
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["platform_id"] == "alipay_wallet"
        assert body["platform_name"] == "支付宝小荷包"
        assert body["total"] == 4
        assert body["by_direction"] == {"收入": 2, "支出": 2}
        assert body["amount_sum"] == 18.40
        assert len(body["warnings"]) == 4

        conn = db.get_conn()
        try:
            assert conn.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
        finally:
            conn.close()

        for _ in range(2):
            with path.open("rb") as stream:
                committed = client.post(
                    "/api/import/commit",
                    files={"file": (path.name, stream, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                )
            assert committed.status_code == 200, committed.text
        assert committed.json()["inserted"] == 0
        assert committed.json()["skipped"] == 4

    conn = db.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0] == 4
        assert conn.execute("SELECT name FROM platforms WHERE id='alipay_wallet'").fetchone()[0] == "支付宝小荷包"
    finally:
        conn.close()
