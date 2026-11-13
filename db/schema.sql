-- ============================================================================
--  AI Finance Assistant · 数据库 DDL
--  唯一真相源。修改流程：改这里 → python -m backend.db → 跑测试
--
--  设计原则：
--    1. 原始层写入后永不 UPDATE（凭证不可涂改）
--    2. 所有修正落在修正层，不回写原始层
--    3. 多平台：source 自由文本，platform_id 外键指向 platforms
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ============================================================================
--  平台元数据
--  加新平台 = INSERT 一行 + 放一个 mappings/*.yaml，数据库零结构改动
-- ============================================================================
CREATE TABLE IF NOT EXISTS platforms (
    id          TEXT PRIMARY KEY,          -- 'wechat' / 'alipay' / 'meituan' / 'cmb'
    name        TEXT NOT NULL UNIQUE,      -- 显示名 '微信支付'
    kind        TEXT NOT NULL,             -- 'wallet' 钱包 / 'bank' 银行 / 'ecommerce' 电商
    adapter     TEXT NOT NULL,             -- 'wechat' / 'generic_csv' / 'bank' ...
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================================
--  原始层 —— 不可变
--
--  写权限约束：仅 INSERT。代码中禁止出现
--      UPDATE transactions_raw
--      DELETE FROM transactions_raw
--  验证：tests/test_immutability.py + grep 零命中
-- ============================================================================
CREATE TABLE IF NOT EXISTS transactions_raw (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,

    -- ---- 平台原始字段（归一化后写入，只写一次）----
    source       TEXT    NOT NULL,          -- 平台显示名 '微信支付'/'美团'/'招商银行'
    platform_id  TEXT    NOT NULL
                          REFERENCES platforms(id)
                          ON DELETE RESTRICT,  -- 自由文本，非两值枚举

    tx_time      TEXT    NOT NULL,          -- 'YYYY-MM-DD HH:MM:SS'
    direction    TEXT    NOT NULL,          -- 支出 / 收入 / 中性
    amount       REAL    NOT NULL,          -- 恒为正，两位小数
    pay_method   TEXT,                      -- 支付方式原始字符串
    counterparty TEXT,                      -- 交易对方
    item         TEXT,                      -- 商品说明
    tx_type      TEXT,                      -- 平台原始分类（第一轮直接用作 category）
    status       TEXT,                      -- 交易状态
    tx_no        TEXT,                      -- 平台交易单号
    merchant_no  TEXT,                      -- 商户/订单单号
    remark       TEXT,                      -- 用户备注

    -- ---- 派生字段（导入时从 tx_time 推导，不人工填）----
    date         TEXT    NOT NULL,          -- 'YYYY-MM-DD'
    month        TEXT    NOT NULL,          -- 'YYYY-MM'
    year         TEXT    NOT NULL,          -- 'YYYY'
    weekday      INTEGER NOT NULL,          -- 0=周一 … 6=周日
    hour         INTEGER NOT NULL,          -- 0-23

    -- ---- 导入元信息 ----
    dedupe_key   TEXT    NOT NULL,          -- 去重指纹，UNIQUE
    raw_json     TEXT    NOT NULL,          -- 原始整行 JSON，永久留底
    imported_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    import_batch TEXT,                      -- 同一次导入的批次号，便于回滚/排查

    -- ---- 约束 ----
    UNIQUE (dedupe_key),
    CHECK (direction IN ('支出', '收入', '中性')),
    CHECK (amount > 0),
    CHECK (weekday BETWEEN 0 AND 6),
    CHECK (hour    BETWEEN 0 AND 23),
    CHECK (length(tx_time) = 19)
);

-- 报告查询全是 WHERE direction=? AND month=? / date BETWEEN ...
CREATE INDEX IF NOT EXISTS idx_raw_month_dir
    ON transactions_raw (month, direction);
CREATE INDEX IF NOT EXISTS idx_raw_date
    ON transactions_raw (date);
CREATE INDEX IF NOT EXISTS idx_raw_year
    ON transactions_raw (year, direction);
-- 按支付方式分组（支出方式页）
CREATE INDEX IF NOT EXISTS idx_raw_pay_method
    ON transactions_raw (pay_method) WHERE direction = '支出';
-- 按对手方搜索
CREATE INDEX IF NOT EXISTS idx_raw_counterparty
    ON transactions_raw (counterparty);
-- 按批次回溯某次导入
CREATE INDEX IF NOT EXISTS idx_raw_batch
    ON transactions_raw (import_batch);

-- ============================================================================
--  修正层 —— 前端与 AI 均可写
--
--  一笔交易最多一行修正。没有这行 = 完全按原始数据计算。
--  changed_at / updated_by 让"谁在什么时候改了这笔"可追溯
-- ============================================================================
CREATE TABLE IF NOT EXISTS tx_overrides (
    tx_id           INTEGER PRIMARY KEY
                    REFERENCES transactions_raw(id) ON DELETE CASCADE,

    manual_category TEXT,                   -- 手动大类，覆盖 tx_type
    manual_subcat   TEXT,                   -- 手动小类
    is_exempt       INTEGER NOT NULL DEFAULT 0
                    CHECK (is_exempt IN (0, 1)),   -- 豁免：不计入消费/收入统计
    exempt_reason   TEXT,                   -- 豁免理由（人看的）
    funding         TEXT    NOT NULL DEFAULT '自有',  -- 自有/亲情卡/他人代付
    note            TEXT,                   -- 单笔备注

    updated_by      TEXT,                   -- 'ui' / 'mcp'
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 报表过滤 is_exempt=0 时的索引
CREATE INDEX IF NOT EXISTS idx_ovr_exempt
    ON tx_overrides (is_exempt) WHERE is_exempt = 0;

-- ============================================================================
--  用户删除标记 —— 原始凭证仍保持不可变
--
--  前端“删除”只在这里写一条墓碑记录。这样既能让交易从明细和报表消失，
--  又能保留 dedupe_key，避免同一账单再次导入时把已删除记录重新加回来。
-- ============================================================================
CREATE TABLE IF NOT EXISTS tx_deletions (
    tx_id       INTEGER PRIMARY KEY
                REFERENCES transactions_raw(id) ON DELETE CASCADE,
    deleted_by  TEXT NOT NULL DEFAULT 'ui',
    reason      TEXT,
    deleted_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================================
--  导入批次 —— 支持"这次导错了，回滚这一批"
-- ============================================================================
CREATE TABLE IF NOT EXISTS import_batches (
    batch_id    TEXT PRIMARY KEY,
    platform_id TEXT NOT NULL REFERENCES platforms(id),
    filename    TEXT,
    total       INTEGER NOT NULL DEFAULT 0,   -- 解析到的行数
    inserted    INTEGER NOT NULL DEFAULT 0,   -- 实际写入（未重复）
    skipped     INTEGER NOT NULL DEFAULT 0,   -- dedupe_key 命中跳过
    failed      INTEGER NOT NULL DEFAULT 0,
    date_from   TEXT,
    date_to     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================================
--  查询视图 —— 把两层缝在一起
--
--  所有报告模块只 FROM v_tx，看不到 JOIN。
--  以后修正层加字段/加规则，改这里一行，7 个报告模块零改动。
--
--  分类来源优先级：人工明确覆盖 > Live Ledger 当前分类 > 平台原始分类
-- ============================================================================
DROP VIEW IF EXISTS v_tx;
CREATE VIEW v_tx AS
SELECT
    -- 原始层
    r.id,
    r.source,
    r.platform_id,
    r.tx_time,
    r.date,
    r.month,
    r.year,
    r.weekday,
    r.hour,
    r.direction,
    r.amount,
    r.pay_method,
    r.counterparty,
    r.item,
    r.tx_type,
    r.status,
    r.remark,
    r.tx_no,
    r.imported_at,

    -- 分类优先级：人工明确覆盖 > Ledger Engine 当前分类 > 平台原始分类。
    CASE
        WHEN NULLIF(trim(o.manual_category), '') IS NOT NULL THEN o.manual_category
        WHEN ledger_leaf.id IS NOT NULL AND ledger_parent.kind = 'root' THEN ledger_leaf.name
        WHEN ledger_parent.id IS NOT NULL THEN ledger_parent.name
        ELSE COALESCE(r.tx_type, '其他')
    END AS category,
    CASE
        WHEN NULLIF(trim(o.manual_category), '') IS NOT NULL
          OR NULLIF(trim(o.manual_subcat), '') IS NOT NULL
            THEN COALESCE(o.manual_subcat, '')
        WHEN ledger_leaf.id IS NOT NULL AND ledger_parent.kind = 'root' THEN ''
        WHEN ledger_leaf.id IS NOT NULL THEN ledger_leaf.name
        ELSE ''
    END AS subcat,
    COALESCE(o.is_exempt,       0)                  AS is_exempt,
    COALESCE(o.exempt_reason,   '')                 AS exempt_reason,
    COALESCE(o.funding,         '自有')              AS funding,
    COALESCE(o.note,            '')                 AS note,
    COALESCE(o.updated_by,      '')                 AS updated_by,
    COALESCE(o.updated_at,      '')                 AS updated_at,

    -- 人工覆盖或已由用户确认的 AI 分类均不再出现在待分类清单。
    CASE
        WHEN o.tx_id IS NOT NULL OR EXISTS (
            SELECT 1 FROM ai_classification_reviews ar
            WHERE ar.book_id = 'star-live' AND ar.tx_id = r.id
        ) THEN 1 ELSE 0
    END AS is_overridden

FROM transactions_raw r
LEFT JOIN tx_overrides o ON o.tx_id = r.id
LEFT JOIN ledger_legs primary_leg
    ON primary_leg.book_id = 'star-live'
   AND primary_leg.tx_id = r.id
   AND primary_leg.role = 'primary'
LEFT JOIN ledger_nodes ledger_leaf
    ON ledger_leaf.book_id = primary_leg.book_id
   AND ledger_leaf.id = primary_leg.node_id
LEFT JOIN ledger_nodes ledger_parent
    ON ledger_parent.book_id = ledger_leaf.book_id
   AND ledger_parent.id = ledger_leaf.parent_id
WHERE NOT EXISTS (
    SELECT 1 FROM tx_deletions d WHERE d.tx_id = r.id
);

-- ============================================================================
--  便捷视图：仅统计口径的"真消费"
--  ⚠️ 第一轮口径硬编码在这里，不做动态配置（推迟到第二轮智能层）
-- ============================================================================
CREATE VIEW IF NOT EXISTS v_tx_counted AS
SELECT *
FROM v_tx
WHERE is_exempt = 0;

-- ============================================================================
-- 星图账本（第二轮最小安全基座）—— 只追加，不修改第一轮表/视图。
-- 每本账冻结初始交易集；归属改动只写 ledger_*；原始流水绝不改写。
-- ============================================================================
CREATE TABLE IF NOT EXISTS ledger_books (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    current_seq INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS ledger_book_tx (
    book_id TEXT NOT NULL REFERENCES ledger_books(id) ON DELETE CASCADE,
    tx_id INTEGER NOT NULL REFERENCES transactions_raw(id) ON DELETE RESTRICT,
    PRIMARY KEY (book_id, tx_id)
);
CREATE TABLE IF NOT EXISTS ledger_nodes (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES ledger_books(id) ON DELETE CASCADE,
    parent_id TEXT,
    name TEXT NOT NULL,
    op TEXT NOT NULL DEFAULT 'ADD' CHECK (op IN ('ADD','NET')),
    kind TEXT NOT NULL DEFAULT 'group',
    FOREIGN KEY(parent_id) REFERENCES ledger_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_ledger_nodes_book ON ledger_nodes(book_id);
CREATE TABLE IF NOT EXISTS ledger_placements (
    book_id TEXT NOT NULL,
    tx_id INTEGER NOT NULL,
    node_id TEXT NOT NULL REFERENCES ledger_nodes(id),
    PRIMARY KEY (book_id, tx_id),
    FOREIGN KEY(book_id, tx_id) REFERENCES ledger_book_tx(book_id, tx_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ledger_placements_node ON ledger_placements(node_id);
CREATE TABLE IF NOT EXISTS ledger_history (
    book_id TEXT NOT NULL REFERENCES ledger_books(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    label TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(book_id, seq)
);

-- ============================================================================
-- V3.1 AI 分类工作流（模型无关）
-- AI 只能写分类草案；用户确认后由分类服务调用 Ledger 引擎批量归位。
-- 这些表保存草案、确认记录与可复用规则，不保存模型密钥，也不改原始流水。
-- ============================================================================
CREATE TABLE IF NOT EXISTS ai_classification_proposals (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES ledger_books(id) ON DELETE CASCADE,
    base_seq INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','applied','rejected','undone')),
    created_by TEXT NOT NULL DEFAULT 'external_mcp',
    applied_seq INTEGER,
    changed_count INTEGER NOT NULL DEFAULT 0,
    decision_note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_proposals_book_status
    ON ai_classification_proposals(book_id, status, created_at);

CREATE TABLE IF NOT EXISTS ai_classification_suggestions (
    proposal_id TEXT NOT NULL REFERENCES ai_classification_proposals(id) ON DELETE CASCADE,
    tx_id INTEGER NOT NULL REFERENCES transactions_raw(id) ON DELETE RESTRICT,
    original_node_id TEXT NOT NULL,
    target_node_id TEXT,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    reason TEXT NOT NULL,
    needs_clarification INTEGER NOT NULL DEFAULT 0 CHECK (needs_clarification IN (0,1)),
    question TEXT,
    remember_rule INTEGER NOT NULL DEFAULT 0 CHECK (remember_rule IN (0,1)),
    rule_field TEXT CHECK (rule_field IS NULL OR rule_field IN ('counterparty','item','pay_method','source')),
    rule_pattern TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected')),
    PRIMARY KEY(proposal_id, tx_id)
);

CREATE TABLE IF NOT EXISTS ai_classification_reviews (
    book_id TEXT NOT NULL REFERENCES ledger_books(id) ON DELETE CASCADE,
    tx_id INTEGER NOT NULL REFERENCES transactions_raw(id) ON DELETE RESTRICT,
    proposal_id TEXT NOT NULL REFERENCES ai_classification_proposals(id) ON DELETE CASCADE,
    target_node_id TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT NOT NULL,
    confirmed_by TEXT NOT NULL DEFAULT 'user_via_mcp',
    reviewed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(book_id, tx_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_reviews_proposal
    ON ai_classification_reviews(proposal_id);
CREATE INDEX IF NOT EXISTS idx_ai_classification_suggestions_tx
    ON ai_classification_suggestions(tx_id);
-- 清理早期重复定义遗留的冗余索引（旧数据库升级时也会执行）。
DROP INDEX IF EXISTS idx_ai_classification_proposals_book_status;
DROP INDEX IF EXISTS idx_ai_classification_reviews_proposal;
DROP INDEX IF EXISTS idx_ai_classification_rules_book_enabled;

CREATE TABLE IF NOT EXISTS ai_classification_rules (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES ledger_books(id) ON DELETE CASCADE,
    match_field TEXT NOT NULL CHECK (match_field IN ('counterparty','item','pay_method','source')),
    pattern TEXT NOT NULL,
    pattern_norm TEXT NOT NULL,
    target_node_id TEXT NOT NULL,
    source_proposal_id TEXT REFERENCES ai_classification_proposals(id) ON DELETE SET NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(book_id, match_field, pattern_norm)
);
CREATE INDEX IF NOT EXISTS idx_ai_rules_book_enabled
    ON ai_classification_rules(book_id, enabled);

-- ============================================================================
-- AI Finance Web V1.0：模型配置与导入智能工作流
-- API Key 只存 Fernet 密文；本机主密钥保存在项目目录之外。
-- 导入会话暂存归一化交易与 AI 建议，24 小时过期；不写原始交易表。
-- ============================================================================
CREATE TABLE IF NOT EXISTS ai_provider_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    provider TEXT NOT NULL CHECK (provider IN ('openai_compatible','anthropic')),
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    encrypted_api_key TEXT,
    auto_apply_rules INTEGER NOT NULL DEFAULT 0 CHECK (auto_apply_rules IN (0,1)),
    allow_external_processing INTEGER NOT NULL DEFAULT 0 CHECK (allow_external_processing IN (0,1)),
    confidence_threshold REAL NOT NULL DEFAULT 0.90
        CHECK (confidence_threshold >= 0.70 AND confidence_threshold <= 0.99),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ai_import_sessions (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'preview'
        CHECK (status IN ('preview','committed','cancelled','expired')),
    rows_json TEXT,
    analysis_json TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    committed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_import_sessions_status_expiry
    ON ai_import_sessions(status, expires_at);

CREATE TABLE IF NOT EXISTS ai_import_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES ai_import_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_import_messages_session
    ON ai_import_messages(session_id, id);

-- ============================================================================
-- AI 历史账单学习：只保存分析任务与建议，不复制或改写原始流水。
-- 实际分类变更仍必须通过 classification.service -> Ledger Engine。
-- ============================================================================
CREATE TABLE IF NOT EXISTS ai_history_learning_sessions (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES ledger_books(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','preview','partially_applied','applied','failed','cancelled')),
    base_seq INTEGER NOT NULL DEFAULT 0,
    total_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0,
    analysis_json TEXT,
    result_json TEXT,
    commit_request_json TEXT,
    applied_tx_ids_json TEXT NOT NULL DEFAULT '[]',
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_history_sessions_status
    ON ai_history_learning_sessions(status, created_at);

CREATE TABLE IF NOT EXISTS ai_history_learning_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES ai_history_learning_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_history_messages_session
    ON ai_history_learning_messages(session_id, id);

-- ============================================================================
--  Ledger Legs：记账腿结构持久化（金额仍由原子层实时计算）
--
--  只保存结构，不保存 e/i/amount：
--    book_id + tx_id + role -> node_id
--  primary 对应原始交易的主腿；mirror 对应外部额度镜像腿。
--  每本账只物化当前版本，历史版本仍由 ledger_history.state_json 保存，
--  Undo/Redo 时由 service 重新计算并替换当前腿结构。
-- ============================================================================
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_nodes_book_id_unique
    ON ledger_nodes(book_id, id);

CREATE TABLE IF NOT EXISTS ledger_legs (
    book_id TEXT NOT NULL,
    tx_id   INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    role    TEXT NOT NULL CHECK (role IN ('primary', 'mirror')),
    PRIMARY KEY (book_id, tx_id, role),
    FOREIGN KEY (book_id, tx_id)
        REFERENCES ledger_book_tx(book_id, tx_id) ON DELETE CASCADE,
    FOREIGN KEY (book_id, node_id)
        REFERENCES ledger_nodes(book_id, id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_ledger_legs_node
    ON ledger_legs(book_id, node_id);

CREATE TRIGGER IF NOT EXISTS trg_ledger_legs_role_node_insert
BEFORE INSERT ON ledger_legs
BEGIN
    SELECT CASE
        WHEN NEW.role = 'mirror'
         AND NOT EXISTS (
            SELECT 1 FROM ledger_nodes
            WHERE book_id = NEW.book_id
              AND id = NEW.node_id
              AND kind = 'external_income'
         )
        THEN RAISE(ABORT, 'mirror leg must target external_income node')
    END;
    SELECT CASE
        WHEN NEW.role = 'primary'
         AND EXISTS (
            SELECT 1 FROM ledger_nodes
            WHERE book_id = NEW.book_id
              AND id = NEW.node_id
              AND kind = 'external_income'
         )
        THEN RAISE(ABORT, 'primary leg cannot target external_income node')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_ledger_legs_role_node_update
BEFORE UPDATE OF book_id, tx_id, node_id, role ON ledger_legs
BEGIN
    SELECT CASE
        WHEN NEW.role = 'mirror'
         AND NOT EXISTS (
            SELECT 1 FROM ledger_nodes
            WHERE book_id = NEW.book_id
              AND id = NEW.node_id
              AND kind = 'external_income'
         )
        THEN RAISE(ABORT, 'mirror leg must target external_income node')
    END;
    SELECT CASE
        WHEN NEW.role = 'primary'
         AND EXISTS (
            SELECT 1 FROM ledger_nodes
            WHERE book_id = NEW.book_id
              AND id = NEW.node_id
              AND kind = 'external_income'
         )
        THEN RAISE(ABORT, 'primary leg cannot target external_income node')
    END;
END;


-- ============================================================================
-- AI Finance Companion：全局对话持久化与用户确认的操作提案。
-- 模型只提出建议；分类规则/交易修改必须由用户点击确认后走业务服务。
-- ============================================================================
CREATE TABLE IF NOT EXISTS ai_chat_conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '新对话',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_chat_conversations_updated
    ON ai_chat_conversations(updated_at DESC);

CREATE TABLE IF NOT EXISTS ai_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES ai_chat_conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_chat_messages_conversation
    ON ai_chat_messages(conversation_id,id);

CREATE TABLE IF NOT EXISTS ai_chat_actions (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES ai_chat_conversations(id) ON DELETE CASCADE,
    -- action_type 由 chat_service 的白名单校验；不在数据库层写死枚举，
    -- 以便后续安全增加批量分类、手动记账等“先提案、后确认”的操作类型。
    action_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','confirmed','rejected')),
    payload_json TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_chat_actions_conversation
    ON ai_chat_actions(conversation_id,status,created_at);

-- Penny Agent Runtime：任务状态、规划版本与执行 Checkpoint。
-- 不修改 Ledger 核心表；删除对话时通过外键级联清理 Agent 任务。
CREATE TABLE IF NOT EXISTS ai_agent_tasks (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES ai_chat_conversations(id) ON DELETE CASCADE,
    parent_task_id TEXT REFERENCES ai_agent_tasks(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'planning'
        CHECK (status IN ('planning','running','waiting_for_user','completed','failed','interrupted')),
    phase TEXT NOT NULL DEFAULT 'understanding',
    goal TEXT NOT NULL,
    intent TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL DEFAULT 1 CHECK (generation >= 1),
    context_json TEXT NOT NULL DEFAULT '{}',
    plan_json TEXT,
    result_json TEXT,
    clarification_question TEXT,
    progress_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_agent_tasks_conversation
    ON ai_agent_tasks(conversation_id,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_agent_tasks_status
    ON ai_agent_tasks(status,updated_at DESC);

CREATE TABLE IF NOT EXISTS ai_agent_task_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES ai_agent_tasks(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL,
    step_key TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    arguments_json TEXT NOT NULL DEFAULT '{}',
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','succeeded','failed','skipped')),
    result_json TEXT,
    error_text TEXT,
    evidence_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(task_id,generation,step_key)
);
CREATE INDEX IF NOT EXISTS idx_ai_agent_task_steps_task
    ON ai_agent_task_steps(task_id,generation,id);

-- Penny 的长期、非敏感交互偏好，与单次任务条件分开保存。
CREATE TABLE IF NOT EXISTS ai_agent_preferences (
    scope_key TEXT PRIMARY KEY,
    preferences_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
