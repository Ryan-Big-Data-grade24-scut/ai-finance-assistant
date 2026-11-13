from __future__ import annotations

import json
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
