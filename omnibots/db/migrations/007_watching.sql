-- A15.f: watching a project's result, and scheduling tied to a project.

-- the quote the live page must still show, and how the last check went
ALTER TABLE projects ADD COLUMN live_quote TEXT;
ALTER TABLE projects ADD COLUMN live_status TEXT;              -- ok | down | NULL (never checked)
ALTER TABLE projects ADD COLUMN live_error TEXT;

-- a routine or trigger that belongs to a project continues that project instead of starting a new one;
-- closing the project turns them off
ALTER TABLE routines ADD COLUMN project_id TEXT;
ALTER TABLE triggers ADD COLUMN project_id TEXT;

-- the night shift's queue survives a restart (it was a list in memory)
CREATE TABLE night_queue (
    id          INTEGER PRIMARY KEY,
    goal        TEXT NOT NULL,
    project_id  TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',               -- queued | started
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
