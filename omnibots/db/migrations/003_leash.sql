-- A15.b (the leash): who started each job, and each project's autonomy dial.

-- user | routine | trigger | night | watch (Omi's check after files change) | fix (a repair a check started)
-- | continue (A15.d) | relay (A8.d.02). Everything but 'user' is background work.
ALTER TABLE jobs ADD COLUMN origin TEXT NOT NULL DEFAULT 'user';

-- off: the bots start nothing on their own here. watch: checks and reports only. fix: may open repair jobs.
ALTER TABLE projects ADD COLUMN autonomy TEXT NOT NULL DEFAULT 'watch';

-- A15.a.03: multi_provider now means something (off = pinned to the first provider). Every bot so far
-- really failed over across its whole chain, so they're all on; new bots are created on too.
UPDATE bots SET multi_provider = 1;

-- A9.c.03: background tokens the user added for one bot in one project, for one day
-- (on top of [budgets] background_tokens_per_bot). Omi and work the user starts have no allocation.
CREATE TABLE token_allocations (
    project_id   TEXT NOT NULL,
    bot_id       TEXT NOT NULL,
    day          TEXT NOT NULL,                      -- the user's local date, YYYY-MM-DD
    extra_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (project_id, bot_id, day)
);
