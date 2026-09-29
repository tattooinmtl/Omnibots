-- A1.s.01: provider keys OmniBots keeps itself, when Omni isn't installed.
-- `secret` is a Windows DPAPI blob (omnibots/security/dpapi.py): only this Windows user on this PC
-- can decrypt it. The key text is never stored. `account` is '' for a provider without accounts.
CREATE TABLE provider_keys (
    provider    TEXT NOT NULL,
    account     TEXT NOT NULL DEFAULT '',
    secret      BLOB NOT NULL,
    fingerprint TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (provider, account)
);
