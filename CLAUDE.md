# CLAUDE.md

Birddog tracks Ukrainian archival documents on uk.wikisource.org (`Архів:`
namespace): pages, linked documents, translations, locations, user watchlists.

**Read [docs/onboarding/README.md](docs/onboarding/README.md) first.** It and its
siblings are the source of truth; this file is the short list of rules and
pointers. Keep both in step when you change behaviour.

| Doc | For |
|---|---|
| [codebase-map.md](docs/onboarding/codebase-map.md) | Which module does what; legacy vs Postgres mode; migration list |
| [data-model.md](docs/onboarding/data-model.md) | Schemas `raw`, `core`, `app`, `views`; roles and grants |
| [pipelines.md](docs/onboarding/pipelines.md) | Every DAG: trigger, pool, assets, paused state |
| [local-dev.md](docs/onboarding/local-dev.md) | Setup, tests, scratch DB, conventions |
| [operations.md](docs/onboarding/operations.md) | Servers, deploys, restarts, secrets. **Read before touching a server.** |
| [deploy/two-box.md](deploy/two-box.md) | Data box vs apps box |

## Stack in one paragraph

Postgres (`birddog` db) is the system of record. Airflow 3 DAGs (YAML via
dag-factory in `dags/yaml/`, source DAGs in `dags/sources/`) keep it current;
task code lives in `birddog/dag_tasks*.py`, Postgres writers in
`birddog/pipeline.py`. The Flask app (`birddog/service.py`, served as
`birddog-web`) reads `core.*` through `birddog/catalog.py` and starts slow work
as Airflow DAG runs (`birddog/airflow_jobs.py`). Everything runs in Docker
Compose (`deploy/`) on two boxes: data box (Postgres, Airflow) and apps box
(web app, Mathesar, JupyterHub, ToolJet, Saltcorn).

**Two modes in one codebase.** `BIRDDOG_DATABASE_URL` set → Postgres mode (the
new stack). Unset → legacy mode (NocoDB output, SQLite/DynamoDB state, S3
cache, Elastic Beanstalk). Most of the top-level README describes legacy mode.
New work targets Postgres mode; don't break the legacy path without being asked.
`store_backend()` in `birddog/env.py` can disagree with the DB switch: see
codebase-map.md "Legacy vs Postgres mode".

## Commands

```bash
python3.12 -m venv venv && . venv/bin/activate     # use "venv", not ".venv"
pip install -r requirements.txt pytest             # ignore requirements-dev.txt (old EB freeze)

python -m pytest test                              # skips are normal (missing services)
python -m pytest test/test_catalog.py -q           # one file
sh/coverage_report                                 # pytest under coverage + HTML
sh/local_run                                       # legacy-mode web app on :2003
```

- Use pytest, not `unittest discover` (several files import pytest).
- Postgres tests need `BIRDDOG_TEST_DATABASE_URL` pointing at a throwaway db
  whose name contains `test` (`test/pg_scratch.py` refuses otherwise, and that is
  the only guard). Never point it at `birddog`.
- Legacy NocoDB tests fail at import without `BIRDDOG_NOCODB_ENV`; some tests
  hit the live wiki.
- Airflow-only deps are in `deploy/airflow/requirements.txt`, not `requirements.txt`.

## Migrations (`deploy/db/migrations/NNN_name.sql`)

- Three-digit prefix. 010 and 015 are unused gaps; check the directory and
  open branches for the next free number.
- **Every file must be idempotent**: they get re-run (tests, `migrate` service,
  `--all-migrations`). `IF NOT EXISTS`, `CREATE OR REPLACE`, `DROP ... IF EXISTS`,
  `DO` blocks that tolerate missing roles.
- No `BEGIN`/`COMMIT`: the deploy runs each file in autocommit with
  `lock_timeout = 5s`.
- `CREATE OR REPLACE VIEW` can only append columns. Drop the view first to
  remove, rename or reorder.
- New `core` objects need explicit grants (`birddog_ui`, `birddog_web`,
  `birddog_reader`) in the same migration.
- Add a new migration instead of editing a deployed one.

## Servers: hard rules

These come from the owner. Don't bend them without an explicit OK in the
conversation.

- **Never restart `postgres` or `airflow-*`** (or run `airflow-init`,
  `--restart-airflow`, `--restart-postgres`) without the owner's explicit OK.
  `birddog-web`, `caddy`, `saltcorn`, `tooljet`, `mathesar`, `jupyterhub` are fine.
- **Never `docker compose down -v`**: `pg-data` is the only copy of the
  database (no off-box backup exists yet).
- Don't edit files on a server. Boxes aren't git checkouts; deploys rsync your
  working tree with `--delete`.
- Deploy with `sh/deploy-data` then `sh/deploy-apps`, **`--dry-run` first**,
  from an up-to-date checkout of the live branch (uncommitted changes ship too).
- Never read out, print, or commit `deploy/.env` values. `deploy/.env.example`
  lists the keys. `LOGINS.md` is a local credentials sheet: gitignored, keep
  it that way and don't quote it.
- DAG files, `birddog/` and `resources/` are bind-mounted into Airflow, so code
  changes there are live after a deploy with no restart. Changes to `.env` or the
  Airflow image need a recreate, which needs the owner's OK.
- DAG paused/unpaused state lives in Airflow's DB, not the repo; check it
  before expecting a scheduled run.

## Conventions

- Commits: subject `Area: short imperative summary` (see `git log`); body says
  why when not obvious. One change per commit. Commit as the repo owner. **No AI
  or co-author trailers.**
- Commit, push or deploy only when asked. Work branches merge into `day0-stack`
  (the rebuilt stack, live on the servers); `main` tracks the upstream repo's
  legacy app.
- Docstrings explain why, not what. Module docstrings state the module's role
  and gotchas.
- Docs-only changes don't change behaviour. When behaviour changes, update the
  matching onboarding doc in the same change.
- Don't commit `deploy/.env`, `deploy/rclone/rclone.conf`, `.cache/`,
  virtualenvs, or `graphify-out/`.
