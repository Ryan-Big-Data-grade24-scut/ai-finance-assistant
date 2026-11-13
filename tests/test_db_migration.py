from contextlib import closing
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.main import app


@pytest.fixture
def legacy_database(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    # The transaction tables are shared by both versions. Reproduce the old ledger keys.
    shared_schema = db.SCHEMA_PATH.read_text(encoding="utf-8").split("DROP VIEW IF EXISTS v_tx;", 1)[0]
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(shared_schema + """
            CREATE TABLE ledger_books (book_id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE ledger_book_tx (
                book_id TEXT REFERENCES ledger_books(book_id), tx_id INTEGER REFERENCES transactions_raw(id),
                PRIMARY KEY (book_id, tx_id)
            );
            CREATE TABLE ledger_nodes (
                book_id TEXT REFERENCES ledger_books(book_id), node_id TEXT, name TEXT,
                PRIMARY KEY (book_id, node_id)
            );
            CREATE TABLE ledger_legs (
                book_id TEXT, node_id TEXT, tx_id INTEGER, role TEXT,
                FOREIGN KEY (book_id, node_id) REFERENCES ledger_nodes(book_id, node_id),
                FOREIGN KEY (book_id, tx_id) REFERENCES ledger_book_tx(book_id, tx_id)
            );
            CREATE TABLE ledger_ops (book_id TEXT REFERENCES ledger_books(book_id), seq INTEGER);
            CREATE TABLE ledger_book_state (book_id TEXT REFERENCES ledger_books(book_id), current_seq INTEGER);
            CREATE TABLE ledger_snapshots (book_id TEXT REFERENCES ledger_books(book_id), tree_json TEXT);
            CREATE TABLE ledger_seq (scope TEXT PRIMARY KEY, next_val INTEGER);
        """)
        conn.execute("INSERT INTO platforms(id,name,kind,adapter) VALUES('test','Test','wallet','test')")
        conn.execute("""INSERT INTO transactions_raw
            (source,platform_id,tx_time,direction,amount,date,month,year,weekday,hour,dedupe_key,raw_json)
            VALUES('Test','test','2026-01-01 00:00:00','支出',20,'2026-01-01','2026-01','2026',3,0,'test','{}')""")
        conn.execute("INSERT INTO tx_overrides(tx_id,manual_category) VALUES(1,'餐饮')")
        conn.execute("INSERT INTO import_batches(batch_id,platform_id,filename) VALUES('batch','test','demo.csv')")
        conn.execute("INSERT INTO ledger_books VALUES('old','旧账本')")
        conn.execute("INSERT INTO ledger_book_tx VALUES('old',1)")
        conn.execute("INSERT INTO ledger_nodes VALUES('old','old:node','自定义分组')")
        conn.execute("INSERT INTO ledger_legs VALUES('old','old:node',1,'primary')")
        conn.execute("INSERT INTO ledger_ops VALUES('old',1)")
        conn.execute("INSERT INTO ledger_book_state VALUES('old',1)")
        conn.execute("INSERT INTO ledger_snapshots VALUES('old','{}')")
        conn.execute("INSERT INTO ledger_seq VALUES('node:old',2)")
        tables = conn.execute("SELECT name FROM sqlite_master WHERE name GLOB 'ledger_*'").fetchall()
        old_rows = {name: conn.execute(f"SELECT * FROM {name}").fetchall() for (name,) in tables}
        raw = conn.execute("SELECT * FROM transactions_raw").fetchall()
        overrides = conn.execute("SELECT * FROM tx_overrides").fetchall()
    return path, old_rows, raw, overrides


def test_startup_archives_old_ledger_and_rebuilds_live_book_once(legacy_database):
    path, old_rows, raw, overrides = legacy_database
    for _ in range(2):
        with TestClient(app) as client:
            assert client.get("/health").json() == {
                "status": "ok", "db": str(path), "transactions": 1, "batches": 1,
            }
            assert client.get("/api/import/history").json()["batches"][0]["platform_name"] == "Test"
            assert client.get("/api/ledger/live").json()["count"] == 1
        with closing(sqlite3.connect(path)) as conn:
            for name, expected in old_rows.items():
                assert conn.execute(f"SELECT * FROM archive_legacy_{name}").fetchall() == expected
            assert conn.execute("SELECT * FROM transactions_raw").fetchall() == raw
            assert conn.execute("SELECT * FROM tx_overrides").fetchall() == overrides
            assert conn.execute("SELECT id FROM ledger_books").fetchall() == [("star-live",)]
            assert conn.execute("SELECT seq FROM ledger_history").fetchall() == [(0,)]
            assert conn.execute("SELECT category FROM v_tx_counted").fetchall() == [("餐饮",)]
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema_failure_rolls_back_archival(legacy_database, tmp_path, monkeypatch):
    path, old_rows, _, _ = legacy_database
    broken_schema = tmp_path / "broken.sql"
    broken_schema.write_text(db.SCHEMA_PATH.read_text(encoding="utf-8") + "\nSELECT missing_column;\n", encoding="utf-8")
    monkeypatch.setattr(db, "SCHEMA_PATH", broken_schema)
    with pytest.raises(sqlite3.OperationalError, match="missing_column"):
        db.init_db(path)
    with closing(sqlite3.connect(path)) as conn:
        for name, expected in old_rows.items():
            assert conn.execute(f"SELECT * FROM {name}").fetchall() == expected
        assert conn.execute("SELECT name FROM sqlite_master WHERE name GLOB 'archive_legacy_*'").fetchall() == []
