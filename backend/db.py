"""
backend/db.py —— 连接管理 / 建表 / 事务

三件容易做错的事统一收口：
    1. 建表（新人 / CI 不会拿到空库）
    2. 连接配置（row_factory、WAL、绝对路径）
    3. 事务边界（导入失败整体回滚，不留半截数据）

用法：
    python -m backend.db            # 建表
    python -m backend.db --stats    # 看当前库状态
"""
from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

# ---- 路径：必须绝对，相对路径会在不同工作目录下指向不同数据库 ----
BASE_DIR = Path(__file__).resolve().parent.parent
DB_DIR = BASE_DIR / "db"
DB_PATH = DB_DIR / "bills.db"
SCHEMA_PATH = DB_DIR / "schema.sql"


def init_db(db_path: Path | None = None) -> None:
    """执行 schema.sql 建表建索引建视图。幂等，可重复执行。"""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = sqlite3.connect(path)
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    """返回配置好的连接。

    - row_factory=Row  →  可以 row['amount']，避免魔法下标 row[3]
    - WAL               →  读写不互斥，解决 FastAPI 多线程写冲突
    - busy_timeout      →  写冲突时等待而不是立刻抛 locked
    """
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection | None = None):
    """显式事务。

    任何异常都 ROLLBACK —— 导入 1200 笔第 847 笔失败时，
    库里不会留下 1~846 笔的残缺数据。
    """
    own = conn is None
    c = conn if conn is not None else get_conn()
    try:
        c.execute("BEGIN")
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        if own:
            c.close()


def health_snapshot(db_path: Path | None = None) -> dict:
    """存活探针要的那几个数。

    放在这里而不是 main.py —— **main.py 不含任何 SQL**（第一轮 DNA）。
    """
    conn = get_conn(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) c FROM transactions_raw").fetchone()["c"]
        batches = conn.execute("SELECT COUNT(*) c FROM import_batches").fetchone()["c"]
    finally:
        conn.close()
    return {"transactions": total, "batches": batches}


def show_stats(db_path: Path | None = None) -> None:
    """打印当前库状态，用于验证导入结果。"""
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        print(f"数据库不存在：{path}")
        return
    conn = get_conn(path)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM transactions_raw")
    total = c.fetchone()[0]
    print(f"库文件      : {path}")
    print(f"总笔数      : {total}")

    if total == 0:
        conn.close()
        return

    print("\n-- 按平台 --")
    for r in c.execute(
        "SELECT platform_id, source, direction, COUNT(*) n, ROUND(SUM(amount),2) amt "
        "FROM transactions_raw GROUP BY platform_id, source, direction "
        "ORDER BY platform_id, direction"
    ):
        print(f"  {r['source']:<12} {r['direction']:<4} {r['n']:>6} 笔  ¥{r['amt']}")

    print("\n-- 按月 --")
    for r in c.execute(
        "SELECT month, direction, COUNT(*) n, ROUND(SUM(amount),2) amt "
        "FROM transactions_raw GROUP BY month, direction ORDER BY month, direction"
    ):
        print(f"  {r['month']}  {r['direction']:<4} {r['n']:>6} 笔  ¥{r['amt']}")

    print("\n-- 分类 TOP10（修正层已生效）--")
    for r in c.execute(
        "SELECT category, COUNT(*) n, ROUND(SUM(amount),2) amt FROM v_tx "
        "WHERE direction='支出' GROUP BY category ORDER BY amt DESC LIMIT 10"
    ):
        print(f"  {r['category']:<12} {r['n']:>5} 笔  ¥{r['amt']}")

    print("\n-- 支付方式（支出）TOP8 --")
    for r in c.execute(
        "SELECT pay_method, COUNT(*) n, ROUND(SUM(amount),2) amt FROM v_tx "
        "WHERE direction='支出' GROUP BY pay_method ORDER BY amt DESC LIMIT 8"
    ):
        print(f"  {r['pay_method']:<24} {r['n']:>5} 笔  ¥{r['amt']}")

    c.execute("SELECT COUNT(*) FROM tx_overrides")
    print(f"\n修正记录    : {c.fetchone()[0]} 条")
    conn.close()


if __name__ == "__main__":
    # Windows 控制台默认 GBK，✓ 编不出来 —— 这里兜底，别让打印把脚本干掉
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    init_db()
    print(f"[OK] 已初始化 {DB_PATH}")
    if "--stats" in sys.argv:
        print()
        show_stats()
