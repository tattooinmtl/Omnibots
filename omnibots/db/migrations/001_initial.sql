-- 001_initial: base schema for OmniBots (PLAN.md A0.b.03).
-- Times are ISO-8601 UTC text. JSON columns hold JSON text.
-- Provider connection data (base URLs, keys, models) is NOT stored here: it is
-- read live from Omni. Secret VALUES are never stored here: only vault names.

-- ── Bots (identities; ADR-11: identity is separate from compute) ─────────
CREATE TABLE bots (
    id                 TEXT PRIMARY KEY,              -- bot_<uuid>
    name               TEXT NOT NULL,
    role               TEXT NOT NULL,
    description        TEXT,
    status             TEXT NOT NULL DEFAULT 'idle',  -- idle|thinking|tool|waiting|blocked|error|done|sleeping|rate_limited|archived
    provider_chain_json TEXT,                         -- ["minimax.io", ...] or lane name
    multi_provider     INTEGER NOT NULL DEFAULT 0,
    risk_ceiling       TEXT NOT NULL DEFAULT 'R2',
    limits_json        TEXT,
    profile_json       TEXT,
    memory_path        TEXT,
    workspace_path     TEXT,
    created_by         TEXT,                          -- 'user' | bot id
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_active_at     TEXT
);

CREATE TABLE bot_skills (
    bot_id   TEXT NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    skill_id TEXT NOT NULL,
    PRIMARY KEY (bot_id, skill_id)
);

CREATE TABLE bot_tools (
    bot_id  TEXT NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    tool_id TEXT NOT NULL,
    PRIMARY KEY (bot_id, tool_id)
);

-- ── Seats (ADR-11: 4 MiniMax seats + the cheap lane) ─────────────────────
CREATE TABLE seats (
    id           TEXT PRIMARY KEY,                   -- minimax-1..4, lane
    kind         TEXT NOT NULL,                      -- minimax | lane
    pinned_to    TEXT,                               -- bot id (seat 1 = boss)
    holder_bot_id TEXT,
    granted_at   TEXT,
    priority     INTEGER
);

-- ── Projects, jobs (DAG record, ADR-10), artifacts ───────────────────────
CREATE TABLE projects (
    id          TEXT PRIMARY KEY,
    goal        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    path        TEXT,
    budget_json TEXT,
    created_by  TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT
);

CREATE TABLE jobs (
    id              TEXT PRIMARY KEY,
    project_id      TEXT REFERENCES projects(id) ON DELETE CASCADE,
    parent_job_id   TEXT,
    title           TEXT NOT NULL,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending|ready|assigned|running|waiting_approval|blocked|review|completed|failed|cancelled
    priority        INTEGER NOT NULL DEFAULT 0,
    depends_on_json TEXT,                            -- ["job_id", ...]
    done_criteria   TEXT,
    verifier        TEXT,
    budget_json     TEXT,
    risk_ceiling    TEXT NOT NULL DEFAULT 'R2',
    assigned_bot_id TEXT REFERENCES bots(id),
    created_by      TEXT,
    result_summary  TEXT,
    error_message   TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    started_at      TEXT,
    finished_at     TEXT
);
CREATE INDEX idx_jobs_project ON jobs(project_id);
CREATE INDEX idx_jobs_bot ON jobs(assigned_bot_id);
CREATE INDEX idx_jobs_status ON jobs(status);

CREATE TABLE artifacts (
    id         INTEGER PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    job_id     TEXT,
    bot_id     TEXT,
    kind       TEXT NOT NULL,                        -- file|url|screenshot|report|...
    path_or_url TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ── Board (ADR-3) and the live bot stream (fixes audit §1.2) ─────────────
CREATE TABLE messages (
    id           INTEGER PRIMARY KEY,               -- replay cursor
    topic        TEXT NOT NULL,
    sender_type  TEXT NOT NULL,                     -- bot|user|system
    sender_id    TEXT,
    recipient_id TEXT,                              -- A2A target (ADR-10), NULL = broadcast
    job_id       TEXT,
    project_id   TEXT,
    message_type TEXT NOT NULL,
    payload_json TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_messages_topic ON messages(topic, id);
CREATE INDEX idx_messages_job ON messages(job_id, id);
CREATE INDEX idx_messages_recipient ON messages(recipient_id, id);

CREATE TABLE bot_events (
    id         INTEGER PRIMARY KEY,
    bot_id     TEXT NOT NULL,
    job_id     TEXT,
    kind       TEXT NOT NULL,                       -- console|terminal|thinking|state
    content    TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_bot_events_bot ON bot_events(bot_id, id);

-- ── The Ledger (ADR-12: proof-carrying results) ──────────────────────────
CREATE TABLE claims (
    id          INTEGER PRIMARY KEY,
    job_id      TEXT,
    project_id  TEXT,
    bot_id      TEXT NOT NULL,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'submitted',  -- submitted|accepted|rejected
    decided_by  TEXT,
    reason      TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    decided_at  TEXT
);
CREATE INDEX idx_claims_job ON claims(job_id);

CREATE TABLE evidence (
    id        INTEGER PRIMARY KEY,
    claim_id  INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,                        -- file|diff|test|url_quote|screenshot|command
    ref       TEXT NOT NULL,                        -- path / URL / artifact id (never the full blob)
    detail_json TEXT                                -- e.g. {"exit_code":0} or {"quote":"..."}
);

-- ── Providers: OmniBots-side state only (merged, audit §6) ───────────────
CREATE TABLE providers (
    name                 TEXT PRIMARY KEY,           -- Omni provider name, e.g. minimax.io
    enabled_for_bots     INTEGER NOT NULL DEFAULT 1,
    usage_pct            REAL NOT NULL DEFAULT 0,
    window_seconds       INTEGER,
    window_started_at    TEXT,
    last_429_at          TEXT,
    reset_at             TEXT,
    status               TEXT NOT NULL DEFAULT 'ok', -- ok|cooling|disabled|error
    verified_limits_json TEXT,                       -- from the A2.b.05 probe
    token_budget         INTEGER,                    -- e.g. MiniMax 1.5B reservoir
    tokens_used          INTEGER NOT NULL DEFAULT 0,
    last_tested_at       TEXT
);

CREATE TABLE provider_usage_events (
    id          INTEGER PRIMARY KEY,
    provider    TEXT NOT NULL,
    model       TEXT,
    bot_id      TEXT,
    job_id      TEXT,
    tokens_in   INTEGER,                             -- NULL when unknown
    tokens_out  INTEGER,
    estimated   INTEGER NOT NULL DEFAULT 0,          -- 1 = counts were estimated (audit §9.11)
    status_code INTEGER,
    latency_ms  INTEGER,
    cost_usd    REAL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_usage_provider ON provider_usage_events(provider, created_at);

-- ── Skills, tools, playbooks ─────────────────────────────────────────────
CREATE TABLE skills_cache (
    id          TEXT PRIMARY KEY,                    -- source:name
    name        TEXT NOT NULL,
    command     TEXT,
    description TEXT,
    source      TEXT NOT NULL,                       -- omni|omni-user|index|omnibots
    path        TEXT NOT NULL,
    loaded_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE tools (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT,
    risk_class    TEXT NOT NULL,                     -- R0..R5 (ADR-8)
    origin        TEXT NOT NULL,                     -- builtin|forge|mcp
    manifest_json TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'active',    -- active|pending_review|retired
    created_by    TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE playbooks (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    parent_id       TEXT,                            -- previous version / variant source
    body_md         TEXT NOT NULL,                   -- steps, decision rules, outputs, approval limits
    status          TEXT NOT NULL DEFAULT 'active',  -- active|variant|retired
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE playbook_runs (
    id          INTEGER PRIMARY KEY,
    playbook_id TEXT NOT NULL REFERENCES playbooks(id) ON DELETE CASCADE,
    project_id  TEXT,
    success     INTEGER,
    tokens      INTEGER,
    cost_usd    REAL,
    seconds     REAL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ── Routines and triggers (§4.5) ─────────────────────────────────────────
CREATE TABLE routines (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    goal        TEXT NOT NULL,
    schedule    TEXT NOT NULL,                       -- cron-style
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    next_run_at TEXT
);

CREATE TABLE triggers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,                       -- file|webhook|connector|board|threshold
    config_json TEXT NOT NULL,
    goal        TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_fired_at TEXT
);

-- ── Safety: approvals, vault index, locks (§3) ───────────────────────────
CREATE TABLE approvals (
    id           TEXT PRIMARY KEY,
    job_id       TEXT,
    bot_id       TEXT,
    risk_class   TEXT NOT NULL,                      -- R3|R4|R5
    action       TEXT NOT NULL,
    summary      TEXT NOT NULL,
    rehearsal_json TEXT,                             -- screenshots, fields, amount, destination
    scope_json   TEXT,                               -- pre-approved scope (domain, action, max count, expiry)
    status       TEXT NOT NULL DEFAULT 'pending',    -- pending|approved|denied|expired|used
    decided_at   TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_approvals_status ON approvals(status);

CREATE TABLE secrets_index (
    name       TEXT PRIMARY KEY,                     -- handle used as secret:<name>; the value is in the OS vault
    kind       TEXT,
    note       TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE locks (
    resource   TEXT PRIMARY KEY,
    holder_bot_id TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- ── Parameters and audit ─────────────────────────────────────────────────
CREATE TABLE parameters (
    id     INTEGER PRIMARY KEY,
    scope  TEXT NOT NULL,                            -- global|bot|orchestrator
    bot_id TEXT,
    key    TEXT NOT NULL,
    value  TEXT,
    UNIQUE (scope, bot_id, key)
);

CREATE TABLE audit_logs (
    id           INTEGER PRIMARY KEY,
    actor_type   TEXT NOT NULL,                      -- user|bot|system
    actor_id     TEXT,
    action       TEXT NOT NULL,
    details_json TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
