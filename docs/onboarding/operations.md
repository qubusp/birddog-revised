# Operations

Read this before you touch the server. The rules in it come from the owner.

## Access and layout

| Item | Value |
|---|---|
| SSH | `ssh administrator@204.12.218.112` (the default host in `sh/deploy`). Key-based login. |
| Code | `~/birddog-revised/` on the server. **Not a git checkout.** Code arrives by rsync from your laptop. |
| Compose | `~/birddog-revised/deploy/`. Run all `docker compose` commands from there. |
| Containers | Project name `deploy`, so names look like `deploy-postgres-1`. |
| Public sites | `https://<service>.204-12-218-112.sslip.io` for `airflow`, `data`, `hub`, `apps`, `saltcorn`, `birddog` (see [README.md](README.md#target-architecture)). |
| Local ports | Bound to `127.0.0.1` on the server. Reach them with SSH tunnels (below). |

Tunnels from your laptop:

```bash
ssh -N -L 8080:127.0.0.1:8080 administrator@204.12.218.112   # Airflow      http://localhost:8080
ssh -N -L 8000:127.0.0.1:8000 administrator@204.12.218.112   # Mathesar     http://localhost:8000
ssh -N -L 8001:127.0.0.1:8001 administrator@204.12.218.112   # JupyterHub   http://localhost:8001
ssh -N -L 8082:127.0.0.1:8082 administrator@204.12.218.112   # ToolJet
ssh -N -L 8083:127.0.0.1:8083 administrator@204.12.218.112   # Saltcorn
ssh -N -L 8084:127.0.0.1:8084 administrator@204.12.218.112   # birddog-web
ssh -N -L 15432:127.0.0.1:5432 administrator@204.12.218.112  # Postgres (superuser or birddog)
```

Only Caddy (80 and 443) is meant to be reachable from the internet.

## What is and is not in git

- Everything under `~/birddog-revised/` on the server came from the repo, except:
  - `deploy/.env`: secrets, created on the server by `sh/deploy`, never in git.
  - `deploy/rclone/rclone.conf` (not yet present): backup remote credentials, gitignored.
  - Docker volumes: `pg-data` (the database), `airflow-logs`, `birddog-cache`, `caddy-data`, `jupyterhub-data`, `saltcorn-data`, `mathesar-media`, `birddog-web-cache`.
- Do not edit files on the server. The next deploy overwrites them (`--delete`).

## Deploying code

From your laptop, at the repo root:

```bash
sh/deploy                       # default host administrator@204.12.218.112
sh/deploy user@host             # another host
```

What it does:

1. `rsync -az --delete` the repo to `~/birddog-revised/`, excluding `.git`,
   `__pycache__`, `*.pyc`, `notebooks/`, `research/` and `deploy/.env`.
2. On the server: creates `deploy/.env` on the first run (random secrets), and
   appends any missing keys on later runs. It never regenerates existing ones.
3. Builds the `jupyter-singleuser` image, then `docker compose up -d --build`.
4. Runs the init scripts `02-ui.sh`, `03-reader.sh`, `04-apps.sh`, `05-web.sh`
   as `postgres` (idempotent, and they re-sync role passwords from `.env`).
5. `docker compose run --rm migrate` (every migration, idempotent).
6. `bash ui/setup.sh` (Mathesar admin, helper schemas, connection).
7. Prints `docker compose ps`.

Three things to fix or know before relying on it:

- **`rsync --delete` will delete `deploy/rclone/rclone.conf` on the server** once
  it exists, because the file is neither in the repo nor excluded. Add
  `--exclude deploy/rclone/rclone.conf` (or a protect filter) to `sh/deploy`
  before backups are set up.
- The excludes do not cover `.cache/` or `.venv-elyra/`, which are local-only
  and get copied to the server. Add them.
- Step 3 (`up -d --build`) runs before step 5 (migrations). `birddog-web` does not
  depend on `migrate`, so new web code can run briefly before a new migration.
  Migrate first for schema changes (below).

`sh/deploy` runs `up -d`, which recreates any service whose config or image
changed. That can include `postgres` and the `airflow-*` services. Check with the
owner before a deploy that changes either (see [Restarts](#restarts)).

### Migrations before code

For a change that needs a new migration:

1. Write the migration (idempotent, next free number; see [local-dev.md](local-dev.md#migrations)).
2. Apply it on the server, before the code that uses it goes out. Run it from
   your laptop, streaming the file into `psql` as `birddog` over the container's
   local socket (no password, as the init scripts do):

   ```bash
   ssh administrator@204.12.218.112 \
     'cd ~/birddog-revised/deploy && docker compose exec -T postgres psql -U birddog -d birddog -v ON_ERROR_STOP=1 -q' \
     < deploy/db/migrations/014_example.sql
   ```

   Or re-run every migration, which is what the `migrate` service does and is
   safe because they are idempotent:

   ```bash
   ssh administrator@204.12.218.112 'cd ~/birddog-revised/deploy && docker compose run --rm migrate'
   ```

3. Then `sh/deploy`, or rsync and restart the consuming service.

Migrations that grant to roles (004, 007, 012) need the roles to exist. The
init scripts create them, so run the init script first on a new server.

## Restarts

The owner's rule: `airflow-*` and `postgres` restarts need an explicit OK first.

| Service | Restart without asking? | Command (from `~/birddog-revised/deploy`) | Notes |
|---|---|---|---|
| `birddog-web` | yes | `docker compose up -d --no-deps birddog-web` | Code is copied into the image. After a code change: `docker compose build birddog-web` first. Drops in-flight requests. |
| `caddy` | yes | `docker compose up -d --no-deps caddy` | Config reload without restart: `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`. |
| `saltcorn` | yes | `docker compose up -d --no-deps saltcorn` | Memory limit 512m. |
| `tooljet` | yes | `docker compose up -d --no-deps tooljet` | Memory limit 1024m. Starts slowly (healthcheck `start_period` 300 s). |
| `airflow-apiserver`, `airflow-scheduler`, `airflow-dag-processor` | **no, ask the owner first** | | Also any `airflow-init` run. |
| `postgres` | **no, ask the owner first** | | Drops every connection, including the web app, Airflow, Mathesar and the notebooks. |
| `mathesar`, `jupyterhub` | not on the owner's list | | Ask before restarting. |
| `migrate` | not a restart | `docker compose run --rm migrate` | One-shot. |

DAG files and `birddog/` library code are bind-mounted into the Airflow
containers. Changes there need no restart. Changes to `deploy/airflow/requirements.txt`
or the Airflow image do need a rebuild and a recreate of the `airflow-*` services,
so they need the owner's OK.

Do not run `docker compose down -v`. It deletes the named volumes, including
`pg-data`, which is the only copy of the database.

## Secrets

- The real values are only in the server's `deploy/.env`, mode 600. `sh/deploy`
  creates it. It is gitignored and excluded from rsync.
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

The host has no swap, and it stays that way. Each service has a hard `mem_limit`
in `deploy/docker-compose.yml`, so one runaway process gets OOM-killed inside its
own container rather than swapping or taking the host down.

| Service | `mem_limit` |
|---|---|
| `postgres` | 1536m |
| `airflow-scheduler` (LocalExecutor runs task processes here) | 1536m |
| `airflow-apiserver` | 768m |
| `airflow-dag-processor` | 512m |
| `airflow-init` | 512m (one-shot) |
| `birddog-web` | 768m |
| `tooljet` | 1024m (Node heap capped at 768 MB) |
| `mathesar` | 512m |
| `saltcorn` | 512m |
| `jupyterhub` | 256m (each user container: 2 GB, 1 CPU) |
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
- Both DAGs should stay paused until the remote is configured. They are paused on
  creation by the stack's default, unless someone has unpaused them.
- The off-box target is Google Drive, encrypted with rclone's crypt remote
  (`gdrive-crypt`). One-time setup, with the crypt password and salt kept safe:
  [deploy/rclone/README.md](../../deploy/rclone/README.md). Then copy the config to
  `deploy/rclone/rclone.conf` on the server, after fixing the `sh/deploy` exclude.
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
- `sh/deploy`: the `rclone.conf` exclude, and the `.cache/` and `.venv-elyra/` excludes.
- Translation settings (`BIRDDOG_USE_GOOGLE_CLOUD_TRANSLATE`, `GOOGLE_TRANSLATE_API_KEY`)
  are not in compose or `.env.example`, so the translate DAGs fail unless they are set in the server's `.env`.
- `migrate_dynamodb_users` needs AWS credentials in the Airflow environment, which
  are not in `.env.example`.
- `source_jewishgen_sftp` needs `SFTP_PASSWORD` in the Airflow environment (the
  source is disabled until then).
