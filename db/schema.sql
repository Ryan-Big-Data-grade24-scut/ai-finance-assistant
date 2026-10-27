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
--  第一轮分类来源优先级：手动大类 > 平台原始分类
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

    -- 修正层（COALESCE 兜底到原始值/默认值）
    COALESCE(o.manual_category, r.tx_type, '其他') AS category,
    COALESCE(o.manual_subcat,   '')                 AS subcat,
    COALESCE(o.is_exempt,       0)                  AS is_exempt,
    COALESCE(o.exempt_reason,   '')                 AS exempt_reason,
    COALESCE(o.funding,         '自有')              AS funding,
    COALESCE(o.note,            '')                 AS note,
    COALESCE(o.updated_by,      '')                 AS updated_by,
    COALESCE(o.updated_at,      '')                 AS updated_at,

    -- 是否被人工/AI 修正过（用于"待分类"列表）
    CASE WHEN o.tx_id IS NULL THEN 0 ELSE 1 END  AS is_overridden

FROM transactions_raw r
LEFT JOIN tx_overrides o ON o.tx_id = r.id
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
