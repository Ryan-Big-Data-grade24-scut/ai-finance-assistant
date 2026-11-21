-- 账本代数 V2 原型 · 表结构
-- 一个数据库文件 = 一本账（N2 / C14）。没有 ledger_book，没有 book_id。

PRAGMA foreign_keys = ON;

-- ── 原始交易（原型自带最小版；正式环境沿用既有 transactions_raw）────────────
CREATE TABLE IF NOT EXISTS transactions_raw (
    id           INTEGER PRIMARY KEY,
    tx_time      TEXT    NOT NULL,
    date         TEXT    NOT NULL,
    direction    INTEGER NOT NULL CHECK (direction IN (-1, 0, 1)),
    amount       INTEGER NOT NULL CHECK (amount >= 0),   -- 分
    category     TEXT,
    counterparty TEXT,
    item         TEXT,
    source       TEXT
);

-- ── M 节点（树）────────────────────────────────────────────────────────────
-- 没有 op 列：M 恒 +1。没有 kind 列：parent_id IS NULL 就是根。
CREATE TABLE IF NOT EXISTS ledger_node (
    id        TEXT PRIMARY KEY,                        -- UUID，全局唯一
    parent_id TEXT REFERENCES ledger_node(id) ON DELETE RESTRICT,
    name      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_node_parent ON ledger_node(parent_id);

-- 至多一个根（N13：不会出现分散的树）
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_node_one_root
    ON ledger_node((1)) WHERE parent_id IS NULL;

-- ── +2 净额组 ──────────────────────────────────────────────────────────────
-- node_id 显式存组的位置：位置不再靠「从成员推导」，漂移可检测、可修复。
CREATE TABLE IF NOT EXISTS ledger_net_group (
    id      TEXT PRIMARY KEY,                          -- UUID
    name    TEXT,
    node_id TEXT NOT NULL REFERENCES ledger_node(id) ON DELETE RESTRICT
);

-- ── 归属 + 标记 ───────────────────────────────────────────────────────────
-- 一行 = 一笔账本交易（原始或镜像）的一个归属 + 可选的 +2 标记。
-- 规则：有行 ⇔ (不在根) 或 (属于某个组)；无行 = 在根且不属于任何组。
CREATE TABLE IF NOT EXISTS ledger_placement (
    tx_id        INTEGER NOT NULL REFERENCES transactions_raw(id) ON DELETE CASCADE,
    role         TEXT    NOT NULL CHECK (role IN ('primary', 'mirror')),
    node_id      TEXT    NOT NULL REFERENCES ledger_node(id) ON DELETE RESTRICT,
    net_group_id TEXT    REFERENCES ledger_net_group(id) ON DELETE RESTRICT,
    PRIMARY KEY (tx_id, role)
);
CREATE INDEX IF NOT EXISTS idx_ledger_placement_node  ON ledger_placement(node_id);
CREATE INDEX IF NOT EXISTS idx_ledger_placement_group ON ledger_placement(net_group_id);

-- ── 分裂事实 ───────────────────────────────────────────────────────────────
-- 一行 = 这笔交易被分裂过（t′ 存在）。t′ 的其它属性全部派生。
CREATE TABLE IF NOT EXISTS ledger_split_tx (
    src_tx_id INTEGER PRIMARY KEY REFERENCES transactions_raw(id) ON DELETE CASCADE
);

-- ── 操作日志（撤销 / 重做）─────────────────────────────────────────────────
-- Q4：所有操作可逆 → 撤销 = 用同一操作集里的其它操作抵消。
-- 这里存的是「这次操作原语序列」和「它的逆序列」。
-- state：
--   applied  —— 已生效
--   undone   —— 已撤销，可以重做
--   discarded—— 撤销之后又做了别的事，这个操作作废了，不能再重做
CREATE TABLE IF NOT EXISTS ledger_op_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    op_json    TEXT NOT NULL,
    undo_json  TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'applied' CHECK (state IN ('applied', 'undone', 'discarded')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
