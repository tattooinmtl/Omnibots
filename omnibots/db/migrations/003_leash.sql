-- A15.b (the leash): who started each job, and each project's autonomy dial.

-- user | routine | trigger | night | watch (Omi's check after files change) | fix (a repair a check started)
-- | continue (A15.d) | relay (A8.d.02). Everything but 'user' is background work.
ALTER TABLE jobs ADD COLUMN origin TEXT NOT NULL DEFAULT 'user';

-- off: the bots start nothing on their own here. watch: checks and reports only. fix: may open repair jobs.
ALTER TABLE projects ADD COLUMN autonomy TEXT NOT NULL DEFAULT 'watch';
