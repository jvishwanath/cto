-- Code graph schema: symbols (nodes) + edges (calls/imports/extends).
-- Recursive CTEs over code_edges power find_callers / find_callees.

CREATE TABLE IF NOT EXISTS code_symbols (
    id          SERIAL PRIMARY KEY,
    repo        TEXT NOT NULL,
    filepath    TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- method | function | class | interface | struct | enum | trait | impl | namespace | service
    class_name  TEXT,                   -- enclosing container (class/impl/namespace)
    fqn         TEXT,                   -- best-effort fully-qualified name
    language    TEXT,
    start_line  INT,
    end_line    INT
);

CREATE INDEX IF NOT EXISTS idx_symbols_name       ON code_symbols (name);
CREATE INDEX IF NOT EXISTS idx_symbols_repo_class ON code_symbols (repo, class_name);
CREATE INDEX IF NOT EXISTS idx_symbols_fqn        ON code_symbols (fqn);

CREATE TABLE IF NOT EXISTS code_edges (
    id           SERIAL PRIMARY KEY,
    from_symbol  INT NOT NULL REFERENCES code_symbols(id) ON DELETE CASCADE,
    relation     TEXT NOT NULL,         -- calls | imports | extends | implements | depends_on | belongs_to
    callee_name  TEXT NOT NULL,         -- raw name from AST (always present)
    callee_class TEXT,                  -- resolved via imports (best-effort)
    to_symbol    INT REFERENCES code_symbols(id) ON DELETE SET NULL,  -- NULL = external/unresolved
    line         INT
);

CREATE INDEX IF NOT EXISTS idx_edges_callee_name ON code_edges (callee_name);
CREATE INDEX IF NOT EXISTS idx_edges_to          ON code_edges (to_symbol);
CREATE INDEX IF NOT EXISTS idx_edges_from        ON code_edges (from_symbol);
CREATE INDEX IF NOT EXISTS idx_edges_relation    ON code_edges (relation);
