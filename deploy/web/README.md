# Birddog web app (birddog-web)

The Flask app under gunicorn, served at `https://birddog.<PUBLIC_DOMAIN>` by
Caddy. It reads and writes Postgres only, connecting as the `birddog_web`
role. Background work runs in Airflow: the app triggers DAG runs through the
Airflow REST API (`birddog/airflow_client.py`, `birddog/airflow_jobs.py`) and
reports their state.

| App action | DAG (`dags/yaml/app.yml`) | Task code |
|---|---|---|
| Database update of a page (admin), optionally deep | `app_refresh_page` | `dag_tasks_app.refresh_page`, `refresh_subtree` |
| Translate a page | `app_translate_page` | `dag_tasks_app.translate_page` |
| Watchlist "check now" (one watch) | `app_watchlist_check` | `dag_tasks_app.check_watches` |
| All watches, after each `wiki_changes` run | `app_watchlist_check` (asset-scheduled) | same |

The app DAGs are unpaused on creation. Wiki calls use the `wiki_api` pool.
`/database_status` lists running `app_refresh_page` runs; *Cancel* marks the
run failed in Airflow. Excel export (`/download`) still runs in the app: it
builds from the live wiki page and takes seconds.

## Configuration (deploy/.env)

| Variable | Purpose |
|---|---|
| `BIRDDOG_SECRET_KEY` | Flask session signing key |
| `BIRDDOG_WEB_DB_PASSWORD` | password of the `birddog_web` Postgres role |
| `AIRFLOW_API_USER` / `AIRFLOW_API_PASSWORD` | Airflow user the app triggers DAG runs as (default user name `birddog-web`) |
| `BIRDDOG_SMTP_*` | optional, password-reset mail |

On the data box, `sh/deploy-data` adds `BIRDDOG_WEB_DB_PASSWORD` and
`AIRFLOW_API_*` with random values when missing. Copy those three to the apps
box's `.env`; `sh/deploy-apps` stops when they are missing there, and adds
`BIRDDOG_SECRET_KEY` itself.

## First rollout

From `~/birddog-revised/deploy` on the server, after the code is synced:

```bash
# 1. secrets (skip any the deploy scripts already added)
grep -q '^BIRDDOG_SECRET_KEY=' .env      || echo "BIRDDOG_SECRET_KEY=$(openssl rand -hex 32)" >> .env
grep -q '^BIRDDOG_WEB_DB_PASSWORD=' .env || echo "BIRDDOG_WEB_DB_PASSWORD=$(openssl rand -hex 24)" >> .env
grep -q '^AIRFLOW_API_USER=' .env        || echo "AIRFLOW_API_USER=birddog-web" >> .env
grep -q '^AIRFLOW_API_PASSWORD=' .env    || echo "AIRFLOW_API_PASSWORD=$(openssl rand -hex 24)" >> .env

# 2. the birddog_web role (as postgres; idempotent), then its grants (012_web_role.sql)
BIRDDOG_WEB_DB_PASSWORD="$(grep '^BIRDDOG_WEB_DB_PASSWORD=' .env | cut -d= -f2-)" \
    docker compose exec -T -e BIRDDOG_WEB_DB_PASSWORD postgres \
    bash /docker-entrypoint-initdb.d/05-web.sh
docker compose run --rm migrate

# 3. the Airflow service user (FAB role "User": may trigger and read DAG runs).
#    Passwords are read inside the container from the exec environment, never
#    put on the command line.
AIRFLOW_API_USER="$(grep '^AIRFLOW_API_USER=' .env | cut -d= -f2-)" \
AIRFLOW_API_PASSWORD="$(grep '^AIRFLOW_API_PASSWORD=' .env | cut -d= -f2-)" \
    docker compose exec -T -e AIRFLOW_API_USER -e AIRFLOW_API_PASSWORD airflow-apiserver \
    bash -c 'airflow users create --role User --username "$AIRFLOW_API_USER" \
        --password "$AIRFLOW_API_PASSWORD" --firstname Birddog --lastname Web \
        --email birddog-web@localhost'
#    (to rotate later: airflow users reset-password --username ... --password ...)

# 4. build and start the app, then load the new Caddy site
docker compose build birddog-web
docker compose up -d --no-deps birddog-web caddy
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

`--no-deps` keeps compose from touching Postgres and Airflow. Caddy reads `.env`
(`env_file`), so the new keys make compose recreate it (a second or two of
downtime for all sites); the `caddy reload` then is a no-op safety net. Note
the Airflow services read `.env` too: a later plain `docker compose up -d`
will recreate them because of the new keys (`sh/deploy-data` never runs it
without `--restart-airflow`).

The DAG files (`dags/yaml/app.yml`) and task code are bind-mounted into the
Airflow containers, so the dag-processor picks them up without a restart.

## Checks

```bash
docker compose ps birddog-web                      # healthy
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8084/    # 200
docker compose logs --tail 50 birddog-web
# the app can log in to Airflow as its service user
docker compose exec -T birddog-web python -c "from birddog.airflow_client import get_client as g; c=g(); print(len(c.list_dag_runs('app_refresh_page', limit=1)), 'ok')"
```

Then on `https://birddog.<PUBLIC_DOMAIN>`: log in, translate a page (an
`app_translate_page` run appears in Airflow), and as an admin run a database
update and watch it on `/database_status`.

## Updating

```bash
docker compose build birddog-web && docker compose up -d --no-deps birddog-web
```

## Notes

- One gunicorn process with 8 threads (`deploy/web/gunicorn.conf.py`): the
  app's Runtime (page cache, wiki change poller, alert significance sweep)
  starts once per process.
- Until `05-web.sh` and the migrations have run, the container fails to
  connect and restarts; it recovers on its own once the role exists.
- Local run against a scratch database:
  `docker run --rm -p 8000:8000 -e BIRDDOG_DATABASE_URL=postgresql://... -e BIRDDOG_SECRET_KEY=dev birddog-web:local`
