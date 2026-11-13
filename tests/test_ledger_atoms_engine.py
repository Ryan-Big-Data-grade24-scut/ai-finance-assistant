from pathlib import Path

import pytest

from backend import db
from backend.ledger import service


@pytest.fixture
def split_date_transfer_db(tmp_path: Path, monkeypatch):
    path = tmp_path / "split-date-transfer.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    conn = db.get_conn(path)
    conn.execute(
        "INSERT INTO platforms(id,name,kind,adapter) VALUES('alipay','支付宝','wallet','alipay')"
    )
    rows = [
        (1, "2026-09-01 09:00:00", "支出", 500.0, "本人银行卡", "本人账户互转测试", "MOVE-1", "MOVE-OUT"),
        (2, "2026-09-02 09:00:00", "收入", 500.0, "本人支付宝", "本人账户互转测试", "MOVE-1", "MOVE-IN"),
    ]
    for tx_id, tx_time, direction, amount, counterparty, item, merchant_no, tx_no in rows:
        date = tx_time[:10]
        conn.execute(
            "INSERT INTO transactions_raw "
            "(id,source,platform_id,tx_time,direction,amount,counterparty,item,tx_type,status,"
            "tx_no,merchant_no,date,month,year,weekday,hour,dedupe_key,raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tx_id, "支付宝", "alipay", tx_time, direction, amount, counterparty, item,
                "转账", "交易成功", tx_no, merchant_no, date, date[:7], date[:4],
                1, 9, f"move-{tx_id}", "{}",
            ),
        )
    conn.commit()
    conn.close()
    return path


def test_date_filter_preserves_full_book_internal_transfer_zeroing(split_date_transfer_db):
    full = service.get_live_tree()
    assert full["root_e"] == 0.0
    assert full["root_i"] == 0.0
    assert len(full["internal_transfer_pairs"]) == 1

    one_side = service.get_live_tree(
        date_from="2026-09-01",
        date_to="2026-09-01",
    )
    assert one_side["count"] == 1
    assert one_side["root_e"] == 0.0
    assert one_side["root_i"] == 0.0
    assert one_side["transactions"][0]["internal_transfer_zeroed"] is True
