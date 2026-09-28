-- A16.b: the user's 👍 / 👎 (and an optional note) on a finished goal or on one bot's accepted claim.
-- It counts more than the bots grading themselves: a 👎 on a goal marks its playbook run failed.
CREATE TABLE verdicts (
    id          INTEGER PRIMARY KEY,
    project_id  TEXT NOT NULL,
    claim_id    INTEGER,                                -- NULL = the whole goal
    bot_id      TEXT,                                   -- the claim's bot, or omi for the goal
    provider    TEXT,                                   -- that bot's first provider when rated
    playbook_id TEXT,                                   -- the playbook the goal followed, if any
    verdict     INTEGER NOT NULL CHECK (verdict IN (-1, 1)),
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_verdicts_project ON verdicts(project_id);
