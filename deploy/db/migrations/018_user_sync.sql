-- Where each Birddog account stands in the other tools (birddog/user_sync.py).
--
-- birddog-web copies an account into the other tools whenever it sees the
-- plain password (account creation, login, password change or reset), so
-- every tool gets the same password. One row per account and tool records the
-- last attempt: state 'ok' or 'error' (with detail), and password_version, a
-- fingerprint of the Birddog password hash the tool's copy was made from. A
-- login re-syncs only the tools whose version differs or that failed.
-- Genizah shows the state.

CREATE TABLE IF NOT EXISTS app.user_sync (
    email            text NOT NULL REFERENCES app.user_account(email) ON DELETE CASCADE,
    tool             text NOT NULL,
    state            text NOT NULL CHECK (state IN ('ok', 'error')),
    detail           text,
    password_version text,
    synced_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (email, tool)
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'birddog_web') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON app.user_sync TO birddog_web';
    END IF;
END
$$;
