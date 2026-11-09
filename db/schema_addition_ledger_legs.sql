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
