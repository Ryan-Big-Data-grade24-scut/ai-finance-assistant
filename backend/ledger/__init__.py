"""
backend/ledger —— 第二轮「账本代数引擎」

分层（依赖方向单向无环）：

    errors.py       统一错误（人话文案在这里生成）
    amount.py       Y = (e, i) 与 +₁ / +₂  —— 唯一的算术实现点
    atoms.py        原子层：所有判断都在这里做完
    tree.py         树结构与聚合
    ops.py          八个原子操作（纯函数）
    invariants.py   I1–I6 / C1 / C2 / T1 / 审计链
    ────────────────────────────────────
    store.py        唯一写 ledger_* 表的模块
    loader.py       唯一的账本侧 SQL 入口
    service.py      HTTP 与 MCP 共用的读写接口

**依赖纪律**：`backend/ledger/*` 只允许 `import backend.db` 和
`from backend import schemas as S`。不得 import `backend.reports` /
`backend.overrides` / `backend.importer` —— 账本是**第四条平行通道**，
不插进第一轮的链路中间。
"""
from .errors import Code, LedgerError

__all__ = ["Code", "LedgerError"]
