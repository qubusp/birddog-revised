# Marmot: data catalog

[Marmot](https://github.com/marmotdata/marmot) (MIT, one Go binary) catalogs the
Birddog database and shows lineage. Apps box only; `https://catalog.<PUBLIC_DOMAIN>`,
locally `127.0.0.1:8085` (tunnel: `ssh -N -L 8085:127.0.0.1:8085 administrator@108.181.191.55`).

| Piece | Where |
|---|---|
| Services | `marmot-db-init` (one-shot: role and database `marmot` in `apps-postgres`), `marmot` (`ghcr.io/marmotdata/marmot:0.14.0`, 512m), both in `docker-compose.appsbox.yml` |
| Secrets | `MARMOT_DB_PASSWORD`, `MARMOT_ENCRYPTION_KEY` in the apps box's `deploy/.env`, made by `sh/deploy-apps`. **Never rotate `MARMOT_ENCRYPTION_KEY`**: stored pipeline credentials are encrypted with it. |
| Login | Marmot's own users. The first start creates `admin` / `admin` and forces a password change: change it over the tunnel before the Caddy route goes live. |

## What feeds it

- **Postgres pipeline** (set up in the UI, scheduled inside Marmot): host `postgres`
  (the data box over WireGuard), database `birddog`, user `birddog_reader`
  (read-only; password = `JUPYTER_DB_PASSWORD`). Catalogs the `raw` and `core`
  schemas, columns and foreign keys. `birddog_reader` cannot read `app`.
- **Airflow**: Marmot's Airflow plugin speaks only the Airflow 2 API (`/api/v1`),
  so it does not work with our Airflow 3. Lineage comes from Airflow's OpenLineage
  provider instead (already in the image), posting to
  `https://catalog.<PUBLIC_DOMAIN>/api/v1/lineage` with a Marmot API key (the data
  box reaches the apps box's Marmot only through Caddy). Turning that on means new `.env` keys on the data box and
  recreating the `airflow-*` services, which needs the owner's OK (operations.md).

## Operations

- Restart without asking: `docker compose up -d --no-deps marmot`.
- Upgrade: bump the image tag, read the release notes first (they carry DB migrations).
