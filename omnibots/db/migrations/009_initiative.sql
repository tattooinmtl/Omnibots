-- PLAN.md A17.e.01: goals Omi proposes on its own (you accept or reject) and the team's daily journal.
CREATE TABLE IF NOT EXISTS proposals (
    id          TEXT PRIMARY KEY,
    bot_id      TEXT NOT NULL DEFAULT 'omi',
    title       TEXT NOT NULL,
    goal        TEXT NOT NULL,
    why         TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',      -- pending | accepted | rejected
    project_id  TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    decided_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);

CREATE TABLE IF NOT EXISTS journal (
    day         TEXT PRIMARY KEY,                     -- YYYY-MM-DD (local time)
    entry       TEXT NOT NULL,
    stats_json  TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
