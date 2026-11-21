"""账本代数 V2 · 并行于 backend/ledger（V1）的独立实现。

表前缀 ledger2_*；事实层只读 transactions_raw / tx_overrides / tx_deletions。
HTTP 入口：`backend.ledger2.api.router`（/api/ledger2/*）。
"""
