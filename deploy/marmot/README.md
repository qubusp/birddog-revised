# Marmot: data catalog

[Marmot](https://github.com/marmotdata/marmot) (MIT, one Go binary) catalogs the
Birddog database and shows lineage. Apps box only; `https://catalog.<PUBLIC_DOMAIN>`,
locally `127.0.0.1:8085` (tunnel: `ssh -N -L 8085:127.0.0.1:8085 administrator@108.181.191.55`).

| Piece | Where |
|---|---|
| Services | `marmot-db-init` (one-shot: role and database `marmot` in `apps-postgres`), `marmot` (`ghcr.io/marmotdata/marmot:0.14.0`, 512m), both in `docker-compose.appsbox.yml` |
| Secrets | `MARMOT_DB_PASSWORD`, `MARMOT_ENCRYPTION_KEY` in the apps box's `deploy/.env`, made by `sh/deploy-apps`. **Never rotate `MARMOT_ENCRYPTION_KEY`**: stored pipeline credentials are encrypted with it. |
| MCP | `https://catalog.<PUBLIC_DOMAIN>/api/v1/mcp`. Local clients (Claude Code) use an API key header. Hosted clients (the claude.ai connector) sign in with OAuth and register themselves, which Marmot allows only for hosts in `MARMOT_AUTH_DCR_ALLOWED_REDIRECT_HOSTS` (set to `claude.ai` in the compose file). Users revoke clients under **Account → Connected apps**. |
| Login | Marmot's own users. The first start creates `admin` / `admin` and forces a password change; it was changed on 2026-11-24 (LOGINS.md). On a new box, change it over the tunnel before the Caddy route is reachable. |

## What feeds it

- **Postgres pipeline** (scheduled inside Marmot): host `postgres` (the data box
  over WireGuard), database `birddog`, user `marmot_reader` (read-only, made by
  `deploy/db/init/06-marmot-reader.sh`; password `MARMOT_READER_DB_PASSWORD` in the
  data box's `.env`). Catalogs `raw`, `core` and `views`: tables, columns, foreign
  keys. It cannot read `app`. Its own role because the plugin reads every
  database's size, which needs `pg_read_all_stats`.
- **Airflow**: Marmot's Airflow plugin speaks only the Airflow 2 API (`/api/v1`),
  so it does not work with our Airflow 3. Lineage comes from Airflow's OpenLineage
  provider instead (already in the image), posting to
  `https://catalog.<PUBLIC_DOMAIN>/api/v1/lineage` with a Marmot API key (the data
  box reaches the apps box's Marmot only through Caddy). Settings:
  `AIRFLOW__OPENLINEAGE__NAMESPACE` and `AIRFLOW__OPENLINEAGE__TRANSPORT` in the
  data box's `.env` (format in `.env.example`). The key belongs to the Marmot
  service account `airflow`, whose only role is `lineage-writer` (`assets:view`,
  `assets:manage`); the built-in `user` role cannot write lineage. The settings
  reach Airflow only when the `airflow-*` services are recreated (owner's OK).
  Our tasks use psycopg2 directly, not Airflow hooks, so OpenLineage sees job
  runs and the Assets a DAG declares, not table-level SQL lineage.

## Operations

- Restart without asking: `docker compose up -d --no-deps marmot`.
- Upgrade: bump the image tag, read the release notes first (they carry DB migrations).
