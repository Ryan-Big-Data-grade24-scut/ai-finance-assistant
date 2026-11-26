-- 账本代数 V2 · 表结构（ledger2_* 前缀，与 V1 的 ledger_* 互不相干）
-- 一个数据库文件 = 一本账。没有 book_id。
-- 交易本身不在这里：事实来自既有的 transactions_raw / tx_overrides / tx_deletions，V2 只读。

CREATE TABLE IF NOT EXISTS ledger2_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- M 节点。没有 op 列：M 恒 +1。parent_id IS NULL 就是根。
-- id 形如 'cat:<分类名>' 只是「平台分类预设」建出来的节点的命名约定（读出时标 kind=category）；
-- 它和普通 M 一样是一行真实记录，可以改名、移动、解散。
-- note = 人工备注（节点自己的备忘，不参与金额）。
CREATE TABLE IF NOT EXISTS ledger2_node (
    id        TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES ledger2_node(id) ON DELETE RESTRICT,
    name      TEXT NOT NULL,
    note      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ledger2_node_parent ON ledger2_node(parent_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger2_node_one_root
    ON ledger2_node((1)) WHERE parent_id IS NULL;

-- +2 净额组。node_id 显式存组的位置。note = 人工备注（不参与金额）。
CREATE TABLE IF NOT EXISTS ledger2_net_group (
    id      TEXT PRIMARY KEY,
    name    TEXT,
    note    TEXT NOT NULL DEFAULT '',
    node_id TEXT NOT NULL REFERENCES ledger2_node(id) ON DELETE RESTRICT
);

-- 账本交易：归属 + 人工字段，一张表。
--   role='primary' 一行 = 一笔已初始化的交易；没有这一行 = 没初始化，所有读都会把它过滤掉。
--   role='mirror'  一行 = 它的分裂副本 t′（人工字段保持空）。
-- 已初始化的交易和 t′ **必有**一行（没有「默认父节点」这回事）。
-- net_group_id 非空 = 属于某个 +2 组。
-- 卸载 = 删掉相关的行（primary + mirror），四个字段一并注销。
--   title / note   ：初始化前为空；初始化时可指定，之后可改；平台预设直接抄交易的不可变字段。
--   category_path  ：初始化时写入的「完整路径」（JSON 数组，根以下各级节点名）；之后静默更新（暂未实现）。
CREATE TABLE IF NOT EXISTS ledger2_tx (
    tx_id         INTEGER NOT NULL REFERENCES transactions_raw(id) ON DELETE CASCADE,
    role          TEXT    NOT NULL CHECK (role IN ('primary', 'mirror')),
    node_id       TEXT    NOT NULL REFERENCES ledger2_node(id) ON DELETE RESTRICT,
    net_group_id  TEXT    REFERENCES ledger2_net_group(id) ON DELETE RESTRICT,
    initialized   INTEGER NOT NULL DEFAULT 1 CHECK (initialized IN (0, 1)),
    title         TEXT    NOT NULL DEFAULT '',
    note          TEXT    NOT NULL DEFAULT '',
    category_path TEXT    NOT NULL DEFAULT '[]',
    PRIMARY KEY (tx_id, role)
);
CREATE INDEX IF NOT EXISTS idx_ledger2_tx_node  ON ledger2_tx(node_id);
CREATE INDEX IF NOT EXISTS idx_ledger2_tx_group ON ledger2_tx(net_group_id);

-- 分裂事实：一行 = 这笔交易有一个镜像副本 t′。
CREATE TABLE IF NOT EXISTS ledger2_split_tx (
    src_tx_id INTEGER PRIMARY KEY REFERENCES transactions_raw(id) ON DELETE CASCADE
);

-- 操作日志：正向原语序列 + 逆序列。state: applied / undone / discarded
CREATE TABLE IF NOT EXISTS ledger2_op_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    op_json    TEXT NOT NULL,
    undo_json  TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'applied' CHECK (state IN ('applied', 'undone', 'discarded')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 播放顺序与撤销栈的当前状态独立。正向操作和撤销共用已有原语。
CREATE TABLE IF NOT EXISTS ledger2_replay_event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    op_seq     INTEGER NOT NULL REFERENCES ledger2_op_log(seq) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('apply', 'undo', 'redo')),
    run_id     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
