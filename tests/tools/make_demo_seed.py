"""生成**虚构**的启动种子库 `sample_data/star-v4-startup.sqlite3`。

**为什么需要这个脚本**
    V4 的「首次启动自动装一份快照」功能（`backend/db.py::_install_seed`）原本装的是
    **真实微信/支付宝账单**（110 笔，含真人姓名、真实商户名、真实订单号）。
    这违反项目自己的规矩「真实账单数据一律不入 git」，而且新克隆的人第一次启动
    看到的是别人的账单。

    这个脚本按**完全相同的 schema** 造一份**纯虚构**的库替换它：
    - 110 笔交易，商户名 / 商品 / 订单号 / 金额 / 日期全部虚构
    - 每笔的 `tx_no` 一律以 `MOCK` 开头 —— 可被机器校验（见 §护栏）
    - 账本层由引擎自己建（`sync_live_book()`），和真实流程走同一条路

**用法**
```bash
.venv/Scripts/python.exe tests/tools/make_demo_seed.py            # 写 sample_data/star-v4-startup.sqlite3
.venv/Scripts/python.exe tests/tools/make_demo_seed.py --check    # 只校验现有种子是否合规
```

⚠️ 本脚本**不碰** `db/bills.db`（用户的真实库）。它只新建一个临时库，
最后把它挪到 `sample_data/` 下。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

MOCK_PREFIX = "MOCK"
SEED_REL = Path("sample_data") / "star-v4-startup.sqlite3"

# ---- 虚构素材：全是编的，不对应任何真实商户 ----
# 说明：这些名字刻意带「示例」二字，肉眼扫一眼就能知道不是真数据。
MERCHANTS_WECHAT = [
    ("示例咖啡馆", "拿铁咖啡", "商户消费"),
    ("示例超市", "日用百货", "商户消费"),
    ("示例书店", "图书", "商户消费"),
    ("示例食堂", "午餐", "商户消费"),
    ("示例地铁", "交通出行", "乘车码支付"),
    ("示例便利店", "饮料", "商户消费"),
    ("示例健身房", "月卡", "商户消费"),
    ("示例网课", "课程订阅", "商户消费"),
]
MERCHANTS_ALIPAY = [
    ("示例电商店铺", "日用品", "在线支付"),
    ("示例餐饮", "晚餐", "即时交易"),
    ("示例打车", "车费", "即时交易"),
]
# 收入类：退款、工资、理财 —— 让星图上有 NET/退款两种腿
INCOME_ITEMS = [
    ("示例公司", "工资", "转账"),
    ("示例平台", "退款", "退款"),
    ("示例理财", "收益发放", "转账"),
]


def _weekday(d: date) -> int:
    return (d.weekday() + 1) % 7  # 0=周一


def _make_rows(seed: int = 20261113) -> list[dict]:
    """110 笔虚构交易：100 微信 + 10 支付宝。"""
    rng = random.Random(seed)
    rows: list[dict] = []
    seq = 0

    def emit(source: str, platform_id: str, d: date, hh: int, mm: int,
             direction: str, amount: float, counterparty: str, item: str,
             tx_type: str, batch_id: str) -> None:
        nonlocal seq
        seq += 1
        tx_no = f"{MOCK_PREFIX}{d.strftime('%Y%m%d')}{seq:08d}"
        tx_time = datetime(d.year, d.month, d.day, hh, mm, seq % 60).strftime("%Y-%m-%d %H:%M:%S")
        raw = {
            "交易时间": tx_time,
            "交易类型": tx_type,
            "交易对方": counterparty,
            "商品": item,
            "收/支": direction,
            "金额(元)": f"{amount:.2f}",
            "当前状态": "支付成功",
            "交易单号": tx_no,
            "商户单号": f"{MOCK_PREFIX}M{seq:09d}",
            "备注": "演示数据·虚构",
            "_synthetic": True,
        }
        rows.append({
            "source": source,
            "platform_id": platform_id,
            "tx_time": tx_time,
            "direction": direction,
            "amount": round(amount, 2),
            "pay_method": "零钱" if platform_id == "wechat" else "余额宝",
            "counterparty": counterparty,
            "item": item,
            "tx_type": tx_type,
            "status": "支付成功",
            "tx_no": tx_no,
            "merchant_no": f"{MOCK_PREFIX}M{seq:09d}",
            "remark": "演示数据·虚构",
            "date": d.isoformat(),
            "month": d.strftime("%Y-%m"),
            "year": str(d.year),
            "weekday": _weekday(d),
            "hour": hh,
            "dedupe_key": f"{source}|{tx_no}",
            "raw_json": json.dumps(raw, ensure_ascii=False),
            "imported_at": "2026-11-12 04:15:21",
            "import_batch": batch_id,
        })

    # 微信：2026-09-29 ~ 2026-10-29，100 笔
    start = date(2026, 9, 29)
    batch_wx = "demo0000000001"
    for i in range(100):
        d = start + timedelta(days=i % 31)
        cp, item, tx_type = MERCHANTS_WECHAT[i % len(MERCHANTS_WECHAT)]
        # 每 17 笔来一笔收入（退款/工资/收益），让星图上有 NET 与退款腿
        if i % 17 == 3:
            cp, item, tx_type = INCOME_ITEMS[i % len(INCOME_ITEMS)]
            emit("微信支付", "wechat", d, 9 + i % 12, i % 60, "收入",
                 round(rng.uniform(30, 480), 2), cp, item, tx_type, batch_wx)
        elif i % 23 == 7:
            emit("微信支付", "wechat", d, 12 + i % 8, i % 60, "中性",
                 round(rng.uniform(20, 200), 2), "示例内部转账", "账户互转",
                 "转账", batch_wx)
        else:
            emit("微信支付", "wechat", d, 7 + i % 14, i % 60, "支出",
                 round(rng.uniform(3.5, 260), 2), cp, item, tx_type, batch_wx)

    # 支付宝：2026-10-04 ~ 2026-10-30，10 笔
    start_a = date(2026, 10, 4)
    batch_ali = "demo0000000002"
    for i in range(10):
        d = start_a + timedelta(days=i * 3)
        cp, item, tx_type = MERCHANTS_ALIPAY[i % len(MERCHANTS_ALIPAY)]
        emit("支付宝", "alipay", d, 10 + i % 10, i % 60, "支出",
             round(rng.uniform(12, 320), 2), cp, item, tx_type, batch_ali)
    return rows


BATCHES = [
    {"batch_id": "demo0000000001", "platform_id": "wechat",
     "filename": "演示用微信账单流水(20260929-20261029).csv",
     "total": 100, "inserted": 100, "skipped": 0, "failed": 0,
     "date_from": "2026-09-29", "date_to": "2026-10-29",
     "created_at": "2026-11-12 04:15:21"},
    {"batch_id": "demo0000000002", "platform_id": "alipay",
     "filename": "演示用支付宝交易明细(20261004-20261030).csv",
     "total": 10, "inserted": 10, "skipped": 0, "failed": 0,
     "date_from": "2026-10-04", "date_to": "2026-10-30",
     "created_at": "2026-11-12 04:21:26"},
]

PLATFORMS = [
    {"id": "wechat", "name": "微信支付", "kind": "wallet", "adapter": "wechat",
     "enabled": 1, "created_at": "2026-11-12 04:15:21"},
    {"id": "alipay", "name": "支付宝", "kind": "wallet", "adapter": "alipay",
     "enabled": 1, "created_at": "2026-11-12 04:21:26"},
]

# 这些表在种子库里必须是空的（真实库里带着别人的 AI 审阅文本）
EMPTY_TABLES = [
    "ai_classification_proposals", "ai_classification_reviews",
    "ai_classification_suggestions", "ai_classification_rules",
    "ai_history_learning_messages", "ai_history_learning_sessions",
    "ai_chat_actions", "ai_chat_conversations", "ai_chat_messages",
    "ai_import_messages", "ai_import_sessions",
    "tx_overrides", "tx_deletions", "ledger_placements",
]


def build(target: Path) -> None:
    from backend import db

    tmp_dir = Path(tempfile.mkdtemp(prefix="demo_seed_"))
    tmp_db = tmp_dir / "seed.db"
    try:
        db.DB_PATH = tmp_db
        db.init_db(tmp_db)          # 建 schema（不走 seed 分支：路径不是 db/bills.db）

        conn = sqlite3.connect(tmp_db)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            for p in PLATFORMS:
                conn.execute(
                    "INSERT OR REPLACE INTO platforms(id,name,kind,adapter,enabled,created_at)"
                    " VALUES(:id,:name,:kind,:adapter,:enabled,:created_at)", p)
            for b in BATCHES:
                conn.execute(
                    "INSERT OR REPLACE INTO import_batches"
                    "(batch_id,platform_id,filename,total,inserted,skipped,failed,"
                    "date_from,date_to,created_at)"
                    " VALUES(:batch_id,:platform_id,:filename,:total,:inserted,:skipped,"
                    ":failed,:date_from,:date_to,:created_at)", b)
            for r in _make_rows():
                conn.execute(
                    "INSERT INTO transactions_raw"
                    "(source,platform_id,tx_time,direction,amount,pay_method,counterparty,"
                    "item,tx_type,status,tx_no,merchant_no,remark,date,month,year,weekday,"
                    "hour,dedupe_key,raw_json,imported_at,import_batch)"
                    " VALUES(:source,:platform_id,:tx_time,:direction,:amount,:pay_method,"
                    ":counterparty,:item,:tx_type,:status,:tx_no,:merchant_no,:remark,:date,"
                    ":month,:year,:weekday,:hour,:dedupe_key,:raw_json,:imported_at,:import_batch)", r)
            conn.commit()
        finally:
            conn.close()

        # 账本层交给引擎自己建（和真实启动走同一条路）
        from backend.ledger import service as ledger_service
        ledger_service.sync_live_book()

        # 关键：把 WAL 落回主文件。种子是**单个文件**进 git 的，
        # 如果数据还留在 -wal 里，新克隆的人装到的就是一个空库。
        conn = sqlite3.connect(tmp_db)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            conn.close()
        for leftover in tmp_dir.glob(tmp_db.name + "-*"):
            leftover.unlink(missing_ok=True)

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tmp_db, target)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def check(path: Path) -> int:
    """校验种子库是**虚构**的。返回 0 = 合规。"""
    problems: list[str] = []
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT tx_no, counterparty, raw_json FROM transactions_raw").fetchall()
        if not rows:
            problems.append("transactions_raw 是空的")
        for tx_no, cp, raw_json in rows:
            if not str(tx_no).startswith(MOCK_PREFIX):
                problems.append(f"tx_no 不是 {MOCK_PREFIX} 开头：{tx_no}")
                break
            if "示例" not in str(cp) and "演示" not in str(cp):
                problems.append(f"交易对方不像虚构商户：{cp}")
                break
            if '"_synthetic": true' not in str(raw_json).replace("True", "true"):
                problems.append(f"raw_json 缺少 _synthetic 标记：{tx_no}")
                break
        for table in EMPTY_TABLES:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if n:
                problems.append(f"{table} 有 {n} 行，种子库里应该是空的")
        n_tx = conn.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0]
        n_book = conn.execute("SELECT COUNT(*) FROM ledger_books").fetchone()[0]
        if n_book != 1:
            problems.append(f"ledger_books 有 {n_book} 本，预期 1 本（star-live）")
    finally:
        conn.close()

    if problems:
        print("种子库不合规：")
        for p in problems:
            print("  -", p)
        return 1
    print(f"种子库合规：{n_tx} 笔虚构交易，1 本 star-live 账本，无任何真实数据。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验，不重新生成")
    ap.add_argument("--out", default=str(BASE_DIR / SEED_REL))
    args = ap.parse_args()
    out = Path(args.out)
    if args.check:
        return check(out)
    build(out)
    print(f"已生成 {out}（{out.stat().st_size} 字节）")
    return check(out)


if __name__ == "__main__":
    raise SystemExit(main())
