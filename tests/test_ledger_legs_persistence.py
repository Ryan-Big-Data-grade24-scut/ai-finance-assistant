"""Task 2：ledger_legs 结构持久化、迁移和完整性回归。"""
from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from backend import db
from backend.ledger import service
from backend.ledger.errors import LedgerError


@pytest.fixture
def ledger_db(tmp_path: Path, monkeypatch):
    path = tmp_path / "ledger-legs.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)

    conn = db.get_conn(path)
    conn.execute(
        "INSERT INTO platforms(id,name,kind,adapter) "
        "VALUES('alipay','支付宝','wallet','alipay')"
    )
    rows = [
        (1, "2026-09-01 09:00:00", "支出", 25.50, "外部额度消费"),
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
                date[:7], date[:4], 1, 10, f"ledger-leg-{tx_id}", "{}",
                "消费" if direction == "支出" else "收入", item,
                f"TX-{tx_id}", f"ORDER-{tx_id}",
            ),
        )
    conn.execute(
        "INSERT INTO tx_overrides(tx_id,funding,updated_by) VALUES(1,'亲情卡','test')"
    )
    conn.commit()
    conn.close()
    return path


def _rows(path: Path, book_id: str):
    conn = db.get_conn(path)
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT book_id,tx_id,node_id,role FROM ledger_legs "
                "WHERE book_id=? ORDER BY tx_id,role",
                (book_id,),
            )
        ]
    finally:
        conn.close()


def _create_book():
    return service.create_book("Ledger Legs 测试", "2026-09-01", "2026-09-30")


def test_ledger_legs_table_persists_structure_only(ledger_db):
    tree = _create_book()
    book_id = tree["book"]["id"]

    conn = db.get_conn(ledger_db)
    try:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(ledger_legs)")
        }
    finally:
        conn.close()

    assert columns == {"book_id", "tx_id", "node_id", "role"}
    rows = _rows(ledger_db, book_id)
    assert len(rows) == 2
    assert {(row["tx_id"], row["role"]) for row in rows} == {
        (1, "primary"),
        (2, "primary"),
    }
    assert all(row["book_id"] == book_id for row in rows)


def test_split_persists_primary_and_mirror_destinations(ledger_db):
    tree = _create_book()
    book_id = tree["book"]["id"]

    split = service.apply_op(
        book_id, {"op": "split", "tx_id": 1}, expected_seq=tree["seq"]
    )
    rows = _rows(ledger_db, book_id)
    tx1 = [row for row in rows if row["tx_id"] == 1]
    assert {row["role"] for row in tx1} == {"primary", "mirror"}

    primary = next(row for row in tx1 if row["role"] == "primary")
    mirror = next(row for row in tx1 if row["role"] == "mirror")
    assert primary["node_id"] == next(
        tx["node_id"] for tx in split["transactions"] if tx["id"] == 1
    )
    mirror_node = next(node for node in split["nodes"] if node["id"] == mirror["node_id"])
    assert mirror_node["kind"] == "external_income"
    assert len(split["legs"]) == 3
    assert split["invariants_ok"] and split["audit_ok"]


def test_reassign_undo_redo_unsplit_and_reset_keep_persisted_legs_in_sync(ledger_db):
    tree = _create_book()
    book_id = tree["book"]["id"]
    root_id = tree["root_id"]

    grouped = service.apply_op(
        book_id,
        {
            "op": "create_node",
            "parent_id": root_id,
            "name": "独立分组",
            "op_value": "ADD",
            "tx_ids": [2],
        },
        tree["seq"],
    )
    group_id = next(node["id"] for node in grouped["nodes"] if node["name"] == "独立分组")
    primary = next(row for row in _rows(ledger_db, book_id) if row["tx_id"] == 2)
    assert primary["node_id"] == group_id

    undone = service.undo(book_id, grouped["seq"])
    assert next(row for row in _rows(ledger_db, book_id) if row["tx_id"] == 2)["node_id"] != group_id

    redone = service.redo(book_id, undone["seq"])
    assert next(row for row in _rows(ledger_db, book_id) if row["tx_id"] == 2)["node_id"] == group_id

    split = service.apply_op(book_id, {"op": "split", "tx_id": 1}, redone["seq"])
    assert len([row for row in _rows(ledger_db, book_id) if row["tx_id"] == 1]) == 2

    unsplit = service.apply_op(book_id, {"op": "unsplit", "tx_id": 1}, split["seq"])
    assert len([row for row in _rows(ledger_db, book_id) if row["tx_id"] == 1]) == 1
    assert not any(node["kind"] == "external_income" for node in unsplit["nodes"])

    reset = service.reset_to_m0(book_id, unsplit["seq"])
    assert len(_rows(ledger_db, book_id)) == 2
    assert all(row["role"] == "primary" for row in _rows(ledger_db, book_id))
    assert reset["invariants_ok"]


def test_migration_backfills_existing_books_and_is_idempotent(ledger_db):
    tree = _create_book()
    book_id = tree["book"]["id"]
    split = service.apply_op(book_id, {"op": "split", "tx_id": 1}, tree["seq"])

    conn = db.get_conn(ledger_db)
    conn.execute("DELETE FROM ledger_legs WHERE book_id=?", (book_id,))
    conn.commit()
    conn.close()

    with pytest.raises(LedgerError) as exc:
        service.get_tree(book_id)
    assert exc.value.code == "LEG_PERSISTENCE_MISMATCH"

    first = service.migrate_ledger_legs()
    assert first == {"books_migrated": 1, "legs_written": 3, "idempotent": True}
    restored = service.get_tree(book_id)
    assert restored["count"] == 2 and restored["split_count"] == 1
    assert len(_rows(ledger_db, book_id)) == 3

    second = service.migrate_ledger_legs()
    assert second == first
    assert len(_rows(ledger_db, book_id)) == 3


def test_persisted_structure_corruption_is_detected(ledger_db):
    tree = _create_book()
    book_id = tree["book"]["id"]

    grouped = service.apply_op(
        book_id,
        {
            "op": "create_node",
            "parent_id": tree["root_id"],
            "name": "另一个合法节点",
            "op_value": "ADD",
            "tx_ids": [],
        },
        tree["seq"],
    )
    target = next(node["id"] for node in grouped["nodes"] if node["name"] == "另一个合法节点")

    conn = db.get_conn(ledger_db)
    conn.execute(
        "UPDATE ledger_legs SET node_id=? WHERE book_id=? AND tx_id=1 AND role='primary'",
        (target, book_id),
    )
    conn.commit()
    conn.close()

    with pytest.raises(LedgerError) as exc:
        service.get_tree(book_id)
    assert exc.value.code == "LEG_PERSISTENCE_MISMATCH"


def test_database_rejects_mirror_leg_on_normal_group(ledger_db):
    tree = _create_book()
    book_id = tree["book"]["id"]
    normal_node = next(node["id"] for node in tree["nodes"] if node["kind"] == "group")

    conn = db.get_conn(ledger_db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="mirror leg must target external_income"):
            conn.execute(
                "INSERT INTO ledger_legs(book_id,tx_id,node_id,role) VALUES(?,?,?,'mirror')",
                (book_id, 2, normal_node),
            )
    finally:
        conn.close()



def test_database_rejects_cross_book_node_assignment(ledger_db):
    first = _create_book()
    second = _create_book()
    first_id = first["book"]["id"]
    second_root = second["root_id"]

    conn = db.get_conn(ledger_db)
    try:
        conn.execute(
            "DELETE FROM ledger_legs WHERE book_id=? AND tx_id=2 AND role='primary'",
            (first_id,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            conn.execute(
                "INSERT INTO ledger_legs(book_id,tx_id,node_id,role) VALUES(?,?,?,'primary')",
                (first_id, 2, second_root),
            )
    finally:
        conn.rollback()
        conn.close()



def test_write_operation_does_not_silently_repair_corrupt_leg_projection(ledger_db):
    tree = _create_book()
    book_id = tree["book"]["id"]
    group = next(node for node in tree["nodes"] if node["kind"] == "group")

    conn = db.get_conn(ledger_db)
    conn.execute(
        "UPDATE ledger_legs SET node_id=? WHERE book_id=? AND tx_id=1 AND role='primary'",
        (tree["root_id"], book_id),
    )
    conn.commit()
    conn.close()

    with pytest.raises(LedgerError) as exc:
        service.apply_op(
            book_id,
            {"op": "set_op", "node_id": group["id"], "op_value": "NET"},
            expected_seq=tree["seq"],
        )
    assert exc.value.code == "LEG_PERSISTENCE_MISMATCH"
