-- FinPilot schema.
-- Money is integer cents (negative = outflow). Dates are ISO strings; months are 'YYYY-MM'.
-- patterns, forecasts, categorizations, recommendations, audit_trail and agent_events are
-- append-only: code inserts, never updates (runs.status and users.as_of_month are the exceptions).

CREATE TABLE IF NOT EXISTS users (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL,
    persona               TEXT,                       -- synthetic persona key; NULL for real data
    opening_balance_cents INTEGER NOT NULL DEFAULT 0,
    opening_date          TEXT NOT NULL,
    as_of_month           TEXT NOT NULL,              -- simulated clock
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    filename    TEXT NOT NULL,
    row_count   INTEGER NOT NULL,
    dup_count   INTEGER NOT NULL DEFAULT 0,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    batch_id        TEXT NOT NULL REFERENCES batches(id),
    external_id     TEXT NOT NULL,
    posted_date     TEXT NOT NULL,
    raw_description TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'USD',
    UNIQUE (user_id, external_id)
);
CREATE INDEX IF NOT EXISTS ix_txn_user_date ON transactions(user_id, posted_date);

CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    batch_id    TEXT REFERENCES batches(id),
    kind        TEXT NOT NULL CHECK (kind IN ('main', 'whatif', 'goal_check')),
    status      TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed')),
    as_of_month TEXT NOT NULL,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS agent_events (
    id                INTEGER PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES runs(id),
    seq               INTEGER NOT NULL,
    node              TEXT NOT NULL,
    attempt           INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL CHECK (status IN ('ok', 'rejected', 'error')),
    summary           TEXT NOT NULL DEFAULT '',
    input_ref         TEXT,
    output_ref        TEXT,
    model             TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    duration_ms       INTEGER,
    ts                TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (run_id, seq)
);

CREATE TABLE IF NOT EXISTS merchant_cache (
    merchant_key   TEXT PRIMARY KEY,
    category       TEXT NOT NULL,
    confidence     REAL NOT NULL,
    rationale      TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categorizations (
    id             INTEGER PRIMARY KEY,
    txn_id         INTEGER NOT NULL REFERENCES transactions(id),
    run_id         TEXT NOT NULL REFERENCES runs(id),
    category       TEXT NOT NULL,
    confidence     REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    rationale      TEXT NOT NULL DEFAULT '',          -- model's short reason (copied on cache hits)
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    from_cache     INTEGER NOT NULL CHECK (from_cache IN (0, 1)),
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_cat_txn ON categorizations(txn_id, id);

CREATE TABLE IF NOT EXISTS patterns (
    id         INTEGER PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES runs(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    type       TEXT NOT NULL CHECK (type IN ('recurring', 'anomaly', 'trend')),
    title      TEXT NOT NULL,
    params     TEXT NOT NULL,                         -- JSON, shape per type (backend/models.py)
    narration  TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pattern_evidence (
    pattern_id INTEGER NOT NULL REFERENCES patterns(id),
    txn_id     INTEGER NOT NULL REFERENCES transactions(id),
    PRIMARY KEY (pattern_id, txn_id)
);

CREATE TABLE IF NOT EXISTS forecasts (
    id             INTEGER PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES runs(id),
    user_id        INTEGER NOT NULL REFERENCES users(id),
    as_of_month    TEXT NOT NULL,
    horizon_months INTEGER NOT NULL CHECK (horizon_months > 0),
    method         TEXT NOT NULL,
    params         TEXT NOT NULL,                     -- JSON
    scenario       TEXT,                              -- JSON; NULL = baseline
    output         TEXT NOT NULL,                     -- JSON list of ForecastPoint
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS goals (
    id                         INTEGER PRIMARY KEY,
    user_id                    INTEGER NOT NULL REFERENCES users(id),
    name                       TEXT NOT NULL,
    target_cents               INTEGER NOT NULL CHECK (target_cents > 0),
    target_date                TEXT NOT NULL,
    monthly_contribution_cents INTEGER NOT NULL CHECK (monthly_contribution_cents >= 0),
    start_month                TEXT NOT NULL,
    created_at                 TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS goal_snapshots (
    id                   INTEGER PRIMARY KEY,
    goal_id              INTEGER NOT NULL REFERENCES goals(id),
    run_id               TEXT REFERENCES runs(id),
    month                TEXT NOT NULL,
    planned_cents        INTEGER NOT NULL,
    actual_cents         INTEGER NOT NULL,
    projected_completion TEXT,
    on_track             INTEGER NOT NULL CHECK (on_track IN (0, 1)),
    forecast_id          INTEGER REFERENCES forecasts(id),
    UNIQUE (goal_id, month)
);

CREATE TABLE IF NOT EXISTS recommendations (
    id             INTEGER PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES runs(id),
    lineage_id     TEXT NOT NULL,                     -- groups retries of one recommendation
    attempt        INTEGER NOT NULL CHECK (attempt >= 1),
    status         TEXT NOT NULL CHECK (status IN ('draft', 'rejected', 'accepted', 'dropped')),
    trigger_type   TEXT NOT NULL CHECK (trigger_type IN ('pattern', 'forecast', 'goal', 'scenario')),
    trigger_id     TEXT NOT NULL,
    goal_id        INTEGER REFERENCES goals(id),
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,
    claims         TEXT NOT NULL,                     -- JSON list of Claim
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_rec_lineage ON recommendations(lineage_id, attempt);

CREATE TABLE IF NOT EXISTS audit_trail (
    id               INTEGER PRIMARY KEY,
    rec_id           INTEGER NOT NULL REFERENCES recommendations(id),
    lineage_id       TEXT NOT NULL,
    run_id           TEXT NOT NULL REFERENCES runs(id),
    trigger_type     TEXT NOT NULL,
    trigger_id       TEXT NOT NULL,
    attempt          INTEGER NOT NULL,
    verifier_verdict TEXT NOT NULL CHECK (verifier_verdict IN ('pass', 'reject')),
    reasons          TEXT NOT NULL,                   -- JSON list of str
    checks           TEXT NOT NULL,                   -- JSON list of ClaimCheck
    inputs           TEXT NOT NULL,                   -- JSON {pattern_ids, forecast_id, as_of_month}
    ts               TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_audit_lineage ON audit_trail(lineage_id, attempt);

CREATE TABLE IF NOT EXISTS eval_runs (
    id           INTEGER PRIMARY KEY,
    git_sha      TEXT NOT NULL,
    dataset      TEXT NOT NULL,
    seed         INTEGER,
    models       TEXT NOT NULL,                       -- JSON
    metrics      TEXT NOT NULL,                       -- JSON
    results_path TEXT NOT NULL,                       -- eval/results/*.json is canonical
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
