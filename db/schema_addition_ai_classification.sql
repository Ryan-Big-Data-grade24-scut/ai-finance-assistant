-- ============================================================================
-- AI 分类工作流（只保存草案、用户确认记录与可复用规则；不写原始流水）。
-- MCP/内置模型均通过 backend.classification.service 访问这些表。
-- ============================================================================
CREATE TABLE IF NOT EXISTS ai_classification_proposals (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES ledger_books(id) ON DELETE CASCADE,
    base_seq INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'applied', 'rejected', 'undone')),
    created_by TEXT NOT NULL,
    applied_seq INTEGER,
    changed_count INTEGER NOT NULL DEFAULT 0,
    decision_note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_classification_proposals_book_status
    ON ai_classification_proposals(book_id, status);

CREATE TABLE IF NOT EXISTS ai_classification_suggestions (
    proposal_id TEXT NOT NULL
        REFERENCES ai_classification_proposals(id) ON DELETE CASCADE,
    tx_id INTEGER NOT NULL,
    original_node_id TEXT NOT NULL,
    target_node_id TEXT,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    reason TEXT NOT NULL,
    needs_clarification INTEGER NOT NULL DEFAULT 0
        CHECK (needs_clarification IN (0, 1)),
    question TEXT,
    remember_rule INTEGER NOT NULL DEFAULT 0
        CHECK (remember_rule IN (0, 1)),
    rule_field TEXT,
    rule_pattern TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    PRIMARY KEY (proposal_id, tx_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_classification_suggestions_tx
    ON ai_classification_suggestions(tx_id);

CREATE TABLE IF NOT EXISTS ai_classification_reviews (
    book_id TEXT NOT NULL REFERENCES ledger_books(id) ON DELETE CASCADE,
    tx_id INTEGER NOT NULL,
    proposal_id TEXT NOT NULL
        REFERENCES ai_classification_proposals(id) ON DELETE CASCADE,
    target_node_id TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    reason TEXT NOT NULL,
    confirmed_by TEXT NOT NULL DEFAULT 'user_via_mcp',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (book_id, tx_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_classification_reviews_proposal
    ON ai_classification_reviews(proposal_id);

CREATE TABLE IF NOT EXISTS ai_classification_rules (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES ledger_books(id) ON DELETE CASCADE,
    match_field TEXT NOT NULL
        CHECK (match_field IN ('counterparty', 'item', 'pay_method', 'source')),
    pattern TEXT NOT NULL,
    pattern_norm TEXT NOT NULL,
    target_node_id TEXT NOT NULL,
    source_proposal_id TEXT
        REFERENCES ai_classification_proposals(id) ON DELETE SET NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (book_id, match_field, pattern_norm)
);
CREATE INDEX IF NOT EXISTS idx_ai_classification_rules_book_enabled
    ON ai_classification_rules(book_id, enabled);
