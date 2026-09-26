-- A9: vault host scopes and the money ledger for spend caps.

-- A secret can only be sent to these hosts (comma-separated; empty = any host, still R3).
ALTER TABLE secrets_index ADD COLUMN hosts TEXT NOT NULL DEFAULT '';

-- Every money-costing action (R4), approved or estimated, for per-task / per-bot / per-day caps.
CREATE TABLE spend_events (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    bot_id      TEXT,
    job_id      TEXT,
    project_id  TEXT,
    tool        TEXT NOT NULL,
    amount_usd  REAL NOT NULL,
    description TEXT,
    approval_id TEXT
);
CREATE INDEX idx_spend_day ON spend_events (created_at);
CREATE INDEX idx_spend_bot ON spend_events (bot_id, created_at);
