-- raw: data as received from sources, append-only
-- core: cleaned records, the system of record
-- app: user-facing state (accounts, watchlists, alert state)
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS app;

-- per-source high-water marks ("changes since"), replaces the KV sentinels
CREATE TABLE IF NOT EXISTS raw.watermark (
    source      text PRIMARY KEY,
    value       text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw.wiki_change (
    id          bigserial PRIMARY KEY,
    site        text NOT NULL,
    title       text NOT NULL,
    rev_id      bigint,
    action      text NOT NULL,          -- edit | new | move | delete
    target_title text,                  -- for moves
    changed_at  timestamptz NOT NULL,
    payload     jsonb,
    received_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (site, title, rev_id, action)
);
CREATE INDEX IF NOT EXISTS wiki_change_changed_at ON raw.wiki_change (changed_at);

CREATE TABLE IF NOT EXISTS raw.sftp_listing (
    path        text NOT NULL,
    mtime       timestamptz,
    size        bigint,
    seen_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (path, seen_at)
);
