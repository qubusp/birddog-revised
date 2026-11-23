# Operations

Read this before you touch the server. The rules in it come from the owner.

## Access and layout

| Item | Value |
|---|---|
| SSH | Data box `ssh administrator@204.12.218.112` (default host of `sh/deploy-data`); apps box `ssh administrator@108.181.191.55` (default host of `sh/deploy-apps`). Key-based login only. See [two-box.md](../../deploy/two-box.md). |
| Code | `~/birddog-revised/` on each box. **Not a git checkout.** Code arrives by rsync from your laptop, from whatever checkout you run the deploy script in. |
| Compose | `~/birddog-revised/deploy/`. Run all `docker compose` commands from there. |
| Containers | Project name `deploy`, so names look like `deploy-postgres-1`. |
| Public sites | `https://airflow.204-12-218-112.sslip.io` (data box); `https://<service>.108-181-191-55.sslip.io` for `data`, `hub`, `apps`, `saltcorn`, `birddog` (apps box) (see [README.md](README.md#target-architecture)). |
| Local ports | Bound to `127.0.0.1` on the server. Reach them with SSH tunnels (below). |

Tunnels from your laptop:

```bash
# data box
ssh -N -L 8080:127.0.0.1:8080 administrator@204.12.218.112   # Airflow      http://localhost:8080
ssh -N -L 15432:127.0.0.1:5432 administrator@204.12.218.112  # Postgres (superuser or birddog)
# apps box
ssh -N -L 8000:127.0.0.1:8000 administrator@108.181.191.55   # Mathesar     http://localhost:8000
ssh -N -L 8001:127.0.0.1:8001 administrator@108.181.191.55   # JupyterHub   http://localhost:8001
ssh -N -L 8082:127.0.0.1:8082 administrator@108.181.191.55   # ToolJet
ssh -N -L 8083:127.0.0.1:8083 administrator@108.181.191.55   # Saltcorn
ssh -N -L 8084:127.0.0.1:8084 administrator@108.181.191.55   # birddog-web
```

Only Caddy (80 and 443) is meant to be reachable from the internet.

## What is and is not in git

- Everything under `~/birddog-revised/` on the server came from the repo, except:
  - `deploy/.env`: secrets, one per box, created on the server by the deploy
    scripts, never in git.
  - `deploy/rclone/rclone.conf` (not yet present): backup remote credentials, gitignored.
  - Docker volumes. Data box: `pg-data` (the database), `airflow-logs`,
    `birddog-cache`, `caddy-data`. Apps box: `apps-pg-data`, `jupyterhub-data`,
    `saltcorn-data`, `mathesar-media`, `birddog-web-cache`, `caddy-data`.
- Do not edit files on the server. The next deploy overwrites them, and deletes
  files that are not in your checkout (`rsync --delete`). The rsync never copies
  or deletes `deploy/.env*`, `deploy/rclone/*.conf`, `.git`, caches (`.cache`,
  `__pycache__`), virtualenvs, `graphify-out`, `notebooks/` or `research/`
  (list: `RSYNC_EXCLUDES` in `sh/lib/deploy-common.sh`).

## Deploying code

One script per box, run from the repo root of the checkout you want to ship.
**Run each with `--dry-run` first**: it changes nothing on the server and shows
what rsync would copy or delete, which `.env` keys are missing, which
migrations are new, and which containers are out of date.

```bash
sh/deploy-data --dry-run        # data box, 204.12.218.112
sh/deploy-data
sh/deploy-apps --dry-run        # apps box, 108.181.191.55
sh/deploy-apps
sh/deploy-data --dry-run user@otherhost   # another host
```

Order: `sh/deploy-data` first when the change has a migration or DAG/task code
the web app relies on, then `sh/deploy-apps`. Both refuse to run when the box's
`deploy/.env` selects the other box's override (`COMPOSE_FILE`).

### `sh/deploy-data` (data box)

1. Adds missing keys to `deploy/.env` (random values made on the server; never
   changes or prints an existing value).
2. **Migrations, before any code is synced.** It streams the new files from
   your checkout into `psql` as `birddog` and records each in
   `public.schema_migrations` (file name and sha256; the table is created on
   the first run, which therefore applies every file once, as the `migrate`
   service used to on each `up`). A file that fails stops the deploy before the
   code goes out. A recorded file whose content changed is reported, not
   re-applied; `--all-migrations` re-runs every file. On a database known to be
   up to date, `--baseline-migrations` records every file as applied without
   running any (use it for the first run if re-applying all 12 is unwanted).
3. rsync of the repo. DAG files, `birddog/` and `resources/` are bind-mounted
   into the Airflow containers, so new code is live without a restart.
4. Reports, for `postgres` and `airflow-*`, whether the running container still
   matches the compose config (the config hash compose stores on the container)
   and its image. A `DRIFT` line means a plain `docker compose up -d` would
   recreate it (any `.env` change does this). **It never recreates them.** With
   the owner's OK:
   - `--restart-airflow`: rebuild the Airflow image, run `airflow-init`, then
     `up -d --no-deps` the Airflow services (only changed ones are recreated).
     Needed after a change to `deploy/airflow/Dockerfile` or
     `deploy/airflow/requirements.txt` (the script points it out).
   - `--restart-postgres`: `up -d --no-deps postgres`.
5. Caddy: recreated if its config drifted, restarted if `caddy/Caddyfile.databox`
   changed (a single-file bind mount keeps the old file until a restart, so a
   `caddy reload` is not enough).

`--init-roles` also re-runs `db/init/02-ui.sh`, `03-reader.sh` and `05-web.sh`
as `postgres` (creates missing roles, re-syncs their passwords from `.env`).
Needed on a new box; otherwise only after changing one of those passwords.

### `sh/deploy-apps` (apps box)

1. Adds missing apps-box keys to `deploy/.env`. Keys that must equal the data
   box's (`BIRDDOG_UI_DB_PASSWORD`, `JUPYTER_DB_PASSWORD`,
   `BIRDDOG_WEB_DB_PASSWORD`, `AIRFLOW_API_USER`, `AIRFLOW_API_PASSWORD`) are
   only checked: if one is missing it stops and writes nothing.
2. rsync of the repo.
3. Builds `birddog-web`, `jupyterhub` and the `jupyter-singleuser` notebook image.
4. `docker compose up -d`: everything on this box may be recreated, so compose
   recreates whatever changed (new images, config, `.env`).
5. Restarts Caddy if `caddy/Caddyfile.appsbox` changed.

It does not run `ui/setup.sh` (Mathesar admin and connection): its `views.sh`
step execs into `postgres`, which is on the data box. Run the Mathesar part by
hand after a Mathesar upgrade.

### Things to know

- The deploy ships your working tree, uncommitted changes included, and
  deletes server files that are not in it. Deploy from an up-to-date checkout
  of the branch that is live, or you roll back other people's work.
- `rsync --delete` stops (before copying anything) when it would delete more
  than 100 files: a sign of the wrong directory or branch.
- `sh/deploy` only prints a pointer to the two scripts.

### A migration by hand

`sh/deploy-data` does this for you. By hand, from your laptop, streaming the
file into `psql` as `birddog` over the container's local socket (no password,
as the init scripts do):

```bash
ssh administrator@204.12.218.112 \
  'cd ~/birddog-revised/deploy && docker compose exec -T postgres psql -U birddog -d birddog -v ON_ERROR_STOP=1 -q' \
  < deploy/db/migrations/014_example.sql
```

A file applied this way is not recorded in `public.schema_migrations`, so the
next `sh/deploy-data` applies it again (harmless: every migration is idempotent).
Migrations that grant to roles (004, 007, 012) need the roles to exist: on a new
box run `sh/deploy-data --init-roles` first.

## Restarts

The owner's rule: `airflow-*` and `postgres` restarts need an explicit OK first.

| Service | Restart without asking? | Command (from `~/birddog-revised/deploy`) | Notes |
|---|---|---|---|
| `birddog-web` (apps box) | yes | `docker compose up -d --no-deps birddog-web` | Code is copied into the image. After a code change: `docker compose build birddog-web` first. Drops in-flight requests. |
| `caddy` | yes | `docker compose up -d --no-deps caddy` | Config reload without restart: `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`. |
| `saltcorn` | yes | `docker compose up -d --no-deps saltcorn` | Memory limit 512m. |
| `tooljet` | yes | `docker compose up -d --no-deps tooljet` | Memory limit 1024m. Starts slowly (healthcheck `start_period` 300 s). |
| `airflow-apiserver`, `airflow-scheduler`, `airflow-dag-processor` | **no, ask the owner first** | | Also any `airflow-init` run. |
| `postgres` | **no, ask the owner first** | | Drops every connection, including the web app, Airflow, Mathesar and the notebooks. |
| `mathesar`, `jupyterhub`, `apps-postgres` (apps box) | yes | `docker compose up -d --no-deps <service>` | `sh/deploy-apps` recreates whatever changed on the apps box. |
| `migrate` (data box) | not a restart | `docker compose run --rm migrate` | One-shot; re-runs every migration. `sh/deploy-data` applies new ones itself. |

DAG files and `birddog/` library code are bind-mounted into the Airflow
containers. Changes there need no restart. Changes to `deploy/airflow/requirements.txt`
or the Airflow image do need a rebuild and a recreate of the `airflow-*` services,
so they need the owner's OK.

Do not run `docker compose down -v`. It deletes the named volumes, including
`pg-data`, which is the only copy of the database.

## Secrets

- The real values are only in each box's `deploy/.env`, mode 600. The deploy
  scripts create it and add missing keys; they never print values. It is
  gitignored and never copied or deleted by rsync.
- `deploy/.env.example` in git has placeholders only. It is the list of keys.
- Read `deploy/.env` only with the owner. Do not paste values into chats,
  tickets, commits or docs.
- Every Airflow container gets the **whole** `.env` (`env_file`). A DAG task can
  read every secret, including the Postgres superuser password (`POSTGRES_PASSWORD`,
  used by `verify_restore`) and the Mathesar and JupyterHub admin passwords. Keep
  that in mind before a DAG runs third-party code.
- `TOOLJET_LOCKBOX_MASTER_KEY` must not be rotated: saved datasource credentials
  are encrypted with it.
- Changing a database password: update `.env`, re-run the matching init script
  (`docker compose exec -T -e <VAR> postgres bash /docker-entrypoint-initdb.d/0N-*.sh`),
  then recreate the consumer so it gets the new value. Consumers read env at start.
- The Airflow API user for the web app is `birddog-web` (FAB role `User`). Rotation
  command: [deploy/web/README.md](../../deploy/web/README.md).

## Memory

The hosts have no swap, and it stays that way. Each service has a hard `mem_limit`
in `deploy/docker-compose.yml` (or the box's override), so one runaway process gets OOM-killed inside its
own container rather than swapping or taking the host down.

| Service | `mem_limit` |
|---|---|
| `postgres` | 3g on the data box (`docker-compose.databox.yml`, with larger `shared_buffers`); 1536m in the base file |
| `airflow-scheduler` (LocalExecutor runs task processes here) | 2560m on the data box; 1536m in the base file |
| `airflow-apiserver` | 768m |
| `airflow-dag-processor` | 512m |
| `airflow-init` | 512m (one-shot) |
| `birddog-web` | 768m |
| `tooljet` | 1024m (Node heap capped at 768 MB) |
| `mathesar` | 512m |
| `saltcorn` | 512m |
| `jupyterhub` | 256m (each user container: 2 GB, 1 CPU) |
| `apps-postgres` (apps box) | 768m |
| `caddy` | 128m |
| `migrate` | 256m (one-shot) |

Check whether a container was killed:

```bash
docker inspect -f '{{.State.OOMKilled}}' deploy-tooljet-1
```

To raise a limit, edit `deploy/docker-compose.yml`, then recreate that service
(`up -d --no-deps <service>`). Apply the restart rules above. The host's total RAM
is not recorded in the repo.

## Backups

**There is no off-box backup yet.** The two backup DAGs exist but cannot work:

- `backup_postgres` (daily 02:00 UTC) and `verify_restore` (Sunday 04:00 UTC) read
  `deploy/rclone/rclone.conf`, which is not on the server.
- Both DAGs should stay paused until the remote is configured. As of 2026-11-23
  `verify_restore` is paused but `backup_postgres` is **unpaused** and fails
  nightly (see [pipelines.md](pipelines.md)).
- The off-box target is Google Drive, encrypted with rclone's crypt remote
  (`gdrive-crypt`). One-time setup, with the crypt password and salt kept safe:
  [deploy/rclone/README.md](../../deploy/rclone/README.md). Then copy the config to
  `deploy/rclone/rclone.conf` on the data box. The deploy rsync never copies or
  deletes `deploy/rclone/*.conf`.
- Until then the only copy of the data is the `pg-data` volume on the server.
  Downloaded Wikimedia dumps in `birddog-cache` are not backups; they can be
  re-downloaded.

## Logs and health

| What | Where |
|---|---|
| Container output | `docker compose logs -f <service>`, from `~/birddog-revised/deploy`. Gunicorn access and error logs go to stdout for `birddog-web`. |
| Airflow DAG runs and task logs | Airflow UI (`airflow.<domain>` or the tunnel). Task log files are in the `airflow-logs` volume at `/opt/airflow/logs`. |
| Web app admin pages | `/logs` (an in-memory buffer of the last 1000 log lines in the gunicorn worker), `/service_usage` (reads `app.service_log` on Postgres). Admin login required. |
| Request and service log rows | `app.request_log`, `app.service_log` |
| Source runs | `raw.source_run` (query in [dags/sources/README.md](../../dags/sources/README.md#monitoring)) |
| Health | `docker compose ps` (healthchecks). Airflow API: `curl -fsS http://127.0.0.1:8080/api/v2/monitor/health` on the server. Web app: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8084/`. |

## Open items for the owner

- Off-box backup not set up (above).
- `backup_postgres` is unpaused and failing (no rclone remote).
- `ui/setup.sh` (Mathesar admin and connection) is not run by any deploy script
  since the two-box split.
- Translation settings (`BIRDDOG_USE_GOOGLE_CLOUD_TRANSLATE`, `GOOGLE_TRANSLATE_API_KEY`),
  AWS credentials for `migrate_dynamodb_users` and `SFTP_PASSWORD` for
  `source_jewishgen_sftp` are listed in `.env.example` but not set on the data
  box. Adding them changes `.env`, so the Airflow containers only see them after
  a recreate (`sh/deploy-data --restart-airflow`, with the owner's OK).
