"""测试会话级别的**真实库隔离**。

**为什么必须有这个文件**：
    仓库里有 16 个测试文件是这么写的 ——

        db.init_db(db.DB_PATH)          # ← db.DB_PATH 就是 db/bills.db

    也就是说，**跑一次 pytest 就会打开用户的真实账本库并在里面建表**。
    在 V3 之前这最多是「多建几张空表」；V3 之后 `init_db()` 会做**迁移**
    （归档旧账本表 + 重建新表），于是「跑测试」会**真的迁移用户的库**。

    本文件的 session 级 fixture 把 `backend.db.DB_PATH` 指向临时目录，
    让上面那 16 个文件里的 `db.init_db(db.DB_PATH)` 自动落到临时库上 ——
    **不用改那 16 个文件**，也不用逐个 monkeypatch。

    ⚠️ 这条隔离是**硬要求**，不是风格问题：
    用户的真实库 `db/bills.db` 里有 3112 笔真实交易，
    一旦被测试跑坏，**没法从 git 里恢复**。

    副作用：所有测试都在**空库**上跑（不是真实库）。
    需要真实数据形态的测试，用 `tmp_path` 造自己的库（见 test_db_migration.py）。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def isolate_real_database():
    """整个测试会话期间，`backend.db.DB_PATH` 指向临时目录里的库。

    真实路径在测试结束时原样恢复（进程退出后自然失效，但这儿写清楚）。
    """
    from backend import db

    real_path = Path(db.DB_PATH)
    temp_dir = Path(tempfile.mkdtemp(prefix="ai_finance_test_db_"))
    fake_path = temp_dir / "bills.db"

    db.DB_PATH = fake_path
    try:
        yield fake_path
    finally:
        db.DB_PATH = real_path
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_seed_install(monkeypatch):
    """**不许**把 sample_data 里的启动快照装进测试用的库。

    `backend/db.py::_install_seed` 只在「目标路径正好是 db/bills.db 且文件不存在」
    时才触发，正常情况下测试跑不到；这里显式堵死，防止哪天条件变了
    把一个 663 KB 的二进制快照拷进临时目录（还会污染断言）。
    """
    from backend import db

    monkeypatch.setattr(db, "SEED_PATH", Path("__no_seed__"), raising=False)
