-- A15.c.03: the address where a project's result is live (from an accepted, checked url_quote marked live),
-- and when that page last answered with the quoted text. A15.f checks it on a schedule.
ALTER TABLE projects ADD COLUMN live_url TEXT;
ALTER TABLE projects ADD COLUMN live_checked_at TEXT;
