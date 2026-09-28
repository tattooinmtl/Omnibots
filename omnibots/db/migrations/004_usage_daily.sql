-- A16.c.04: housekeeping rolls old usage rows up into daily totals before deleting them,
-- so cost history (A13) outlives the retention window.
CREATE TABLE usage_daily (
    day        TEXT NOT NULL,                          -- YYYY-MM-DD (UTC, from created_at)
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL DEFAULT '',
    bot_id     TEXT NOT NULL DEFAULT '',
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    calls      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider, model, bot_id)
);
