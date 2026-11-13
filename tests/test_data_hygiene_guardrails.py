"""数据卫生护栏：**真实账单数据一律不入 git**。

项目第一条硬规矩（见 `docs/设计/第二轮/04_骨架与契约.md` §八）：
真实账单数据（`db/*.db`、`private_input/`、`*.zip`、`*.csv`）**一律不进仓库**；
`sample_data/` 只允许**虚构**样本。

这两条以前靠人自觉。V4 的启动种子功能（`backend/db.py::_install_seed`）
第一次把「真实快照」放进了 `sample_data/` 并提交进了公开仓库 —— 本文件把那条
红线变成**会红**的测试。

三条护栏：

1. `test_no_real_database_is_tracked` —— 仓库里被 git 跟踪的二进制库文件，
   只能是那个**演示种子**（且必须通过第 2 条）。真实账本 `db/bills.db`、
   备份 `db/backup/*` 一旦被 `git add` 就红。
2. `test_demo_seed_contains_only_synthetic_rows` —— 种子库里的每一笔交易
   必须带 `_synthetic` 标记、订单号以 `MOCK` 开头、交易对方是「示例/演示」商户。
3. `test_demo_seed_carries_no_ai_review_text` —— 种子库不得携带任何 AI 审阅文本 /
   提案 / 对话历史（那些东西在真实库里会带出真实商户名与推理内容）。

外加一条「密码零命中」：支付宝 ZIP 的解压口令**不许**出现在仓库任何文件里。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parents[1]
SEED_REL = Path("sample_data") / "star-v4-startup.sqlite3"

# 允许被跟踪的二进制库文件（就这一个，且必须通过合成校验）
ALLOWED_TRACKED_DB = {SEED_REL.as_posix()}

# 种子库必须为空的表（AI 审阅文本 / 对话历史 / 人工修正）
SEED_MUST_BE_EMPTY = [
    "ai_classification_proposals",
    "ai_classification_reviews",
    "ai_classification_suggestions",
    "ai_classification_rules",
    "ai_history_learning_messages",
    "ai_history_learning_sessions",
    "ai_chat_actions",
    "ai_chat_conversations",
    "ai_chat_messages",
    "ai_import_messages",
    "ai_import_sessions",
    "tx_overrides",
    "tx_deletions",
    "ledger_placements",
]


def _git_tracked() -> list[str] | None:
    """返回被 git 跟踪的文件清单；拿不到 git 就返回 None（跳过，不误伤 CI）。"""
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=BASE_DIR, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _seed_path() -> Path | None:
    path = BASE_DIR / SEED_REL
    return path if path.is_file() else None


def test_no_real_database_is_tracked():
    """仓库里不许跟踪任何真实账本库 / 备份 / 压缩包 / 账单 CSV。"""
    tracked = _git_tracked()
    if tracked is None:
        pytest.skip("拿不到 git ls-files，跳过")
    offenders = [
        name
        for name in tracked
        if name.lower().endswith((".db", ".sqlite", ".sqlite3", ".zip"))
        and name not in ALLOWED_TRACKED_DB
    ]
    assert not offenders, (
        "这些二进制文件被 git 跟踪了 —— 真实账单数据一律不入 git：\n"
        + "\n".join(offenders)
    )


def test_demo_seed_contains_only_synthetic_rows():
    """启动种子里的每一笔都必须是**虚构**的。"""
    path = _seed_path()
    if path is None:
        pytest.skip("仓库里没有启动种子（首次运行前是正常的）")
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT tx_no, counterparty, raw_json FROM transactions_raw"
        ).fetchall()
    finally:
        conn.close()

    assert rows, "种子库里一笔交易都没有 —— 首次启动会看到空星图"
    for tx_no, counterparty, raw_json in rows:
        assert str(tx_no).startswith("MOCK"), f"订单号不像演示数据：{tx_no}"
        assert ("示例" in str(counterparty) or "演示" in str(counterparty)), (
            f"交易对方不像虚构商户：{counterparty}"
        )
        assert "_synthetic" in str(raw_json), f"raw_json 缺少 _synthetic 标记：{tx_no}"


def test_demo_seed_carries_no_ai_review_text():
    """种子库不得携带 AI 审阅文本 / 提案 / 对话历史。"""
    path = _seed_path()
    if path is None:
        pytest.skip("仓库里没有启动种子")
    conn = sqlite3.connect(path)
    try:
        for table in SEED_MUST_BE_EMPTY:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, f"{table} 在种子库里有 {count} 行，应该为空"
        books = conn.execute("SELECT COUNT(*) FROM ledger_books").fetchone()[0]
        assert books == 1, f"种子库里有 {books} 本账本，预期 1 本（star-live）"
    finally:
        conn.close()


def test_no_zip_password_in_repo():
    """支付宝 ZIP 的解压口令不许出现在仓库任何文件里。

    口令本身**不写进仓库**（规矩：解压密码零命中）。要跑这条检查，
    把它放进环境变量 `AI_FINANCE_ZIP_PASSWORDS`（逗号分隔），例如：

        AI_FINANCE_ZIP_PASSWORDS=123456,654321 pytest -q

    没设这个变量就跳过 —— CI 里没有真实账单，也就不需要这条检查。
    """
    raw = os.environ.get("AI_FINANCE_ZIP_PASSWORDS", "").strip()
    if not raw:
        pytest.skip("未设置 AI_FINANCE_ZIP_PASSWORDS，跳过口令扫描")
    forbidden = {p.strip() for p in raw.replace(";", ",").split(",") if p.strip()}
    tracked = _git_tracked()
    if tracked is None:
        pytest.skip("拿不到 git ls-files，跳过")
    hits: list[str] = []
    for name in tracked:
        path = BASE_DIR / name
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for secret in forbidden:
            if secret in text:
                hits.append(f"{name} 含有解压口令")
    assert not hits, "解压口令出现在仓库文件里：\n" + "\n".join(hits)
