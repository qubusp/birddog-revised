# Local development

What you need on your machine, how to run the tests, how to run the web app
(legacy or Postgres mode), and the conventions for migrations and commits.

## Setup

- Python 3.12 (`runtime.txt` says `python-3.12`).
- Docker, for the Airflow image tests and for the optional NocoDB or web container.
- `psql` (optional, for running migrations by hand).

```bash
cd ~/birddog-revised            # your checkout
python3.12 -m venv venv         # "venv" is gitignored; ".venv" is not (see gaps below)
. venv/bin/activate
pip install -r requirements.txt # web app and library
pip install pytest              # the test runner (also listed in requirements-dev.txt)
```

`requirements.txt` covers the web app and the library. The Airflow-only
dependencies (dag-factory, the Google Translate client version, paramiko) are in
`deploy/airflow/requirements.txt`, which is installed in the Airflow image. The
top-level `requirements-dev.txt` is a full freeze from the old EB-era venv
(it includes `awsebcli`). You do not need it.

## Layout you will touch

| Path | What is there |
|---|---|
| `birddog/` | the library (see [codebase-map.md](codebase-map.md)) |
| `dags/` | DAG definitions (YAML and source folders) |
| `deploy/` | compose stack, migrations, init scripts, area READMEs |
| `test/` | tests; `test/pg_scratch.py` is the scratch-database helper |
| `resources/` | JSON config and xlsx templates the library loads relative to the working directory |
| `templates/`, `static/` | Flask templates and the browser code (`static/js/birddog.js`) |

## Running the tests

Use pytest, from the repo root:

```bash
python -m pytest test
```

Several test files import `pytest`, so `python -m unittest discover` does not
work for the whole suite. The legacy NocoDB tests (`test_database.py`,
`test_importer.py`, the `test_location_*` files) fail at import unless
`BIRDDOG_NOCODB_ENV` is set (`LOCAL` plus `NOCODB_API_TOKEN_LOCAL`), and some
tests call live services (the wiki, Wikimedia Commons, a local NocoDB), so a
full run needs the network and can be slow.

Tests that need a service skip themselves when their variable is missing, so a
run with skips is normal.

### Postgres tests: the scratch database

These need a **throwaway** database, named by `BIRDDOG_TEST_DATABASE_URL`:

- `test/pg_scratch.py` refuses any URL whose database name does not contain
  `test`. That is the only guard, so never point it at `birddog`.
- The helper applies every file in `deploy/db/migrations/` to the scratch
  database. Some tests then `TRUNCATE` the `app.*` tables, and `test_catalog.py`
  inserts fixture rows under clearly fake titles and deletes them afterwards.
- Tests that use it: `test_catalog.py`, `test_archive_roots.py`,
  `test_pg_app_users.py`, `test_dynamo_migrate.py`.

The `birddog` role has no CREATEDB, so a scratch database has to be created by
the `postgres` superuser. Get there through an SSH tunnel to the server:

```bash
ssh -N -L 15432:127.0.0.1:5432 administrator@204.12.218.112   # keep open in another terminal
```

Then, as `postgres` (the password is on the server in `deploy/.env`; do not copy
it into any file or commit):

```sql
CREATE DATABASE birddog_test OWNER birddog;
```

Run the tests:

```bash
export BIRDDOG_TEST_DATABASE_URL='postgresql://birddog:<password>@127.0.0.1:15432/birddog_test'
python -m pytest -q test/test_catalog.py test/test_archive_roots.py test/test_pg_app_users.py test/test_dynamo_migrate.py
```

The `birddog` password is the server's `BIRDDOG_DB_PASSWORD`. Ask the owner for
access rather than reading `.env` yourself.

### Source kit database tests

`test/test_sources_db.py` runs land, transform, verify and expose on a scratch
database that the fixture creates and drops (`birddog_test_sourcekit`). It needs
a superuser DSN:

```bash
ssh -N -L 15434:127.0.0.1:5432 administrator@204.12.218.112
SOURCEKIT_ADMIN_DSN='postgresql://postgres:<password>@127.0.0.1:15434/postgres' \
  python -m pytest -q test/test_sources_db.py
```

`SOURCEKIT_LIVE_LOCATIONS=1` also runs the worked example against the **live**
`core.location` data (read only). Leave it unset unless you intend that.

### DAG parse tests

`test/test_dags.py` parses `dags/` the way the dag-processor does (dag-factory
through `dags/load_yaml_dags.py`) and checks the `app_*` DAGs. It needs Airflow,
so it runs in the Airflow image. Build the image from the repo root (the build
context is the root):

```bash
docker build -f deploy/airflow/Dockerfile -t birddog-airflow:local .
docker run --rm -v "$PWD":/opt/birddog -w /opt/birddog -e PYTHONPATH=/opt/birddog \
    birddog-airflow:local python -m pytest -q test/test_dags.py
```

The file's docstring assumes pytest is in the image. I did not check that.
If it is missing, install it in the container for that run. `test/test_sources_dags.py`
is also skipped outside an Airflow environment and can run in the same image.

### Coverage

`sh/coverage_report [pytest args]` runs `coverage run -m pytest test` and opens
the HTML report (`pip install coverage`).

## Running the web app

### Legacy mode (NocoDB as the output)

Use this only when you need the old NocoDB behaviour.

```bash
cd archive/nocodb-dev && docker compose up -d   # NocoDB on :8080
export NOCODB_API_TOKEN_LOCAL=...            # create a token in the NocoDB UI
sh/local_run                                 # debug on :2003, sets BIRDDOG_NOCODB_ENV=LOCAL
sh/local_run --no-debug                      # no reloader
```

The full variable list is in the top-level `README.md`, which describes the legacy
app. `BIRDDOG_SECRET_KEY` is required.

### Postgres mode (the new stack)

Set `BIRDDOG_DATABASE_URL`. Do not use `sh/local_run` for this: it sets
`BIRDDOG_NOCODB_ENV=LOCAL`, which is the legacy switch.

```bash
export BIRDDOG_DATABASE_URL='postgresql://birddog:<password>@127.0.0.1:15432/birddog_test'
export BIRDDOG_SECRET_KEY=dev
python -m birddog.service --port 2003 --debug
```

- Catalog pages read `core.*`. They are empty until a pipeline has run against
  that database. Use a scratch database, or load a source (`python -m birddog.sources run <name>`
  against the scratch database; see [dags/sources/README.md](../../dags/sources/README.md)).
- The web app's own state is in `app.*`, so the migrations must be applied first.
- Actions that start DAG runs (database update, translate, watch check) need the
  Airflow API: `AIRFLOW_API_URL`, `AIRFLOW_API_USER`, `AIRFLOW_API_PASSWORD`.
  Without them the action fails with "Airflow API is not configured". Do not
  point a local app at the server's Airflow API.

To apply all migrations to a scratch database by hand:

```bash
for f in deploy/db/migrations/*.sql; do
    psql "$BIRDDOG_TEST_DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$f" || break
done
```

Some migrations are no-ops until the roles exist (004, 007, 012). That is expected
outside the server.

### The web container

```bash
docker build -f deploy/web/Dockerfile -t birddog-web:local .
docker run --rm -p 8000:8000 \
    -e BIRDDOG_DATABASE_URL='postgresql://birddog:<password>@host.docker.internal:15432/birddog_test' \
    -e BIRDDOG_SECRET_KEY=dev birddog-web:local
```

See [deploy/web/README.md](../../deploy/web/README.md) for the production setup.

## Conventions

### Migrations

- File name `NNN_short_name.sql`, three digits. 010 and 015 are not used; 014
  and 016 are taken, so the next free number is 017. Check the directory (and
  open branches) before picking a number.
- `sh/deploy-data` applies each new file once and records it in
  `public.schema_migrations`, but files are still re-run: by the `migrate`
  service, by `sh/deploy-data --all-migrations`, and on every scratch database
  in the tests. So each one must be idempotent: `CREATE ... IF NOT EXISTS`, `CREATE OR REPLACE VIEW`, `DROP ... IF
  EXISTS` before a re-create, and `DO` blocks that skip a role that does not exist yet.
- `CREATE OR REPLACE VIEW` can only append columns. To drop, rename or reorder
  one, `DROP VIEW` it first in the same file (009 and 006 say so).
- A new core table is not visible to `birddog_ui` until a migration grants it.
  Add the grant in the same migration (see [data-model.md](data-model.md#roles)).
- Prefer a new migration to changing one that is already deployed. The existing
  files are additive by convention (009, 011, 013).

### Commits

- Commit as the repo owner (`git config user.name` is `qubusp`). Do not add AI or
  co-author trailers.
- Subject: `Area: short imperative summary`, as in the recent history
  (`Importer: ...`, `Updater: ...`). The body says why, when the change is not obvious.
- One change per commit. Do not commit `deploy/.env`, anything under `.cache/`,
  or local virtualenvs. `deploy/.env` is gitignored, and so is `deploy/rclone/rclone.conf`.

### Code and docs

- Docstrings explain why, not what. Module docstrings describe the role of the
  module and any gotcha.
- Do not change behaviour in a docs-only change.

## Gaps in local tooling

- `requirements-dev.txt` is a freeze of the old EB-era venv (with pytest added
  at the end), not a curated dev list.
