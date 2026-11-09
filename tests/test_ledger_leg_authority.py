"""Task 2.2：ledger_legs 成为当前交易归属的唯一权威来源。"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend import db
from backend.ledger import service
from backend.ledger.errors import LedgerError


@pytest.fixture
def authority_db(tmp_path: Path, monkeypatch):
    path = tmp_path / "ledger-leg-authority.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)

    conn = db.get_conn(path)
    conn.execute(
        "INSERT INTO platforms(id,name,kind,adapter) "
        "VALUES('alipay','支付宝','wallet','alipay')"
    )
    rows = [
        (1, "2026-09-01 09:00:00", "支出", 25.50, "普通消费"),
        (2, "2026-09-02 10:00:00", "收入", 10.00, "普通收入"),
    ]
    for tx_id, tx_time, direction, amount, item in rows:
        date = tx_time[:10]
        conn.execute(
            "INSERT INTO transactions_raw "
            "(id,source,platform_id,tx_time,direction,amount,date,month,year,"
            "weekday,hour,dedupe_key,raw_json,tx_type,item,tx_no,merchant_no) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tx_id, "支付宝", "alipay", tx_time, direction, amount, date,
                date[:7], date[:4], 1, 10, f"authority-{tx_id}", "{}",
                "消费" if direction == "支出" else "收入", item,
                f"TX-{tx_id}", f"ORDER-{tx_id}",
            ),
        )
    conn.commit()
    conn.close()
    return path


def _create_book():
    return service.create_book(
        "腿归属权威测试", "2026-09-01", "2026-09-30"
    )


def _rows(path: Path, table: str, book_id: str):
    conn = db.get_conn(path)
    try:
        return [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM {table} WHERE book_id=? ORDER BY tx_id",
                (book_id,),
            )
        ]
    finally:
        conn.close()


def test_new_books_write_primary_placement_only_to_ledger_legs(authority_db):
    tree = _create_book()
    book_id = tree["book"]["id"]

    assert len(_rows(authority_db, "ledger_legs", book_id)) == 2
    assert _rows(authority_db, "ledger_placements", book_id) == []
    assert {row["role"] for row in _rows(authority_db, "ledger_legs", book_id)} == {
        "primary"
    }


def test_normal_reads_ignore_legacy_ledger_placements(authority_db):
    tree = _create_book()
    book_id = tree["book"]["id"]
    tx_before = next(tx for tx in tree["transactions"] if tx["id"] == 1)

    conn = db.get_conn(authority_db)
    try:
        alternate = next(
            node["id"] for node in tree["nodes"]
            if node["id"] != tx_before["node_id"] and node["kind"] == "group"
        )
        conn.execute(
            "INSERT INTO ledger_placements(book_id,tx_id,node_id) VALUES(?,?,?)",
            (book_id, 1, alternate),
        )
        conn.commit()
    finally:
        conn.close()

    refreshed = service.get_tree(book_id)
    tx_after = next(tx for tx in refreshed["transactions"] if tx["id"] == 1)
    assert tx_after["node_id"] == tx_before["node_id"]

    # Any normal write clears the retired compatibility table and writes legs only.
    updated = service.apply_op(
        book_id,
        {"op": "reassign", "tx_id": 1, "to_node_id": tx_before["node_id"]},
        expected_seq=refreshed["seq"],
    )
    assert _rows(authority_db, "ledger_placements", book_id) == []
    assert len(_rows(authority_db, "ledger_legs", book_id)) == 2
    assert updated["invariants_ok"]


def test_primary_leg_and_current_snapshot_must_match(authority_db):
    tree = _create_book()
    book_id = tree["book"]["id"]
    tx_before = next(tx for tx in tree["transactions"] if tx["id"] == 1)
    alternate = next(
        node["id"] for node in tree["nodes"]
        if node["id"] != tx_before["node_id"] and node["kind"] == "group"
    )

    conn = db.get_conn(authority_db)
    try:
        conn.execute(
            "UPDATE ledger_legs SET node_id=? "
            "WHERE book_id=? AND tx_id=1 AND role='primary'",
            (alternate, book_id),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(LedgerError) as exc:
        service.get_tree(book_id)
    assert exc.value.code == "LEG_PERSISTENCE_MISMATCH"


def test_explicit_migration_backfills_legs_and_retires_legacy_rows(authority_db):
    tree = _create_book()
    book_id = tree["book"]["id"]
    expected_primary = [
        (row["tx_id"], row["node_id"])
        for row in _rows(authority_db, "ledger_legs", book_id)
        if row["role"] == "primary"
    ]

    conn = db.get_conn(authority_db)
    try:
        conn.execute("DELETE FROM ledger_legs WHERE book_id=?", (book_id,))
        conn.executemany(
            "INSERT INTO ledger_placements(book_id,tx_id,node_id) VALUES(?,?,?)",
            ((book_id, tx_id, node_id) for tx_id, node_id in expected_primary),
        )
        conn.commit()
    finally:
        conn.close()

    first = service.migrate_ledger_legs()
    assert first == {"books_migrated": 1, "legs_written": 2, "idempotent": True}
    assert _rows(authority_db, "ledger_placements", book_id) == []
    assert len(_rows(authority_db, "ledger_legs", book_id)) == 2
    assert service.get_tree(book_id)["invariants_ok"]

    second = service.migrate_ledger_legs()
    assert second == first


def test_migration_refuses_conflicting_legacy_placement(authority_db):
    tree = _create_book()
    book_id = tree["book"]["id"]
    tx = next(tx for tx in tree["transactions"] if tx["id"] == 1)
    alternate = next(
        node["id"] for node in tree["nodes"]
        if node["id"] != tx["node_id"] and node["kind"] == "group"
    )

    conn = db.get_conn(authority_db)
    try:
        conn.execute(
            "INSERT INTO ledger_placements(book_id,tx_id,node_id) VALUES(?,?,?)",
            (book_id, 1, alternate),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(LedgerError) as exc:
        service.migrate_ledger_legs()
    assert exc.value.code == "LEG_PERSISTENCE_MISMATCH"
    # A failed migration must not delete or rewrite either projection.
    assert len(_rows(authority_db, "ledger_placements", book_id)) == 1



def test_migration_refuses_corrupt_existing_leg_projection(authority_db):
    tree = _create_book()
    book_id = tree["book"]["id"]
    tx = next(tx for tx in tree["transactions"] if tx["id"] == 1)
    alternate = next(
        node["id"] for node in tree["nodes"]
        if node["id"] != tx["node_id"] and node["kind"] == "group"
    )

    conn = db.get_conn(authority_db)
    try:
        conn.execute(
            "UPDATE ledger_legs SET node_id=? "
            "WHERE book_id=? AND tx_id=1 AND role='primary'",
            (alternate, book_id),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(LedgerError) as exc:
        service.migrate_ledger_legs()
    assert exc.value.code == "LEG_PERSISTENCE_MISMATCH"
