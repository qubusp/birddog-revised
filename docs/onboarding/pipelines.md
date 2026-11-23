# Pipelines

Every DAG the stack runs, what triggers it, and what it touches. Schedules are
UTC. Airflow runs with `DAGS_ARE_PAUSED_AT_CREATION=true`, so a DAG that does
not set `is_paused_upon_creation: false` comes up **paused**, and its schedule
does nothing until someone unpauses it in the Airflow UI. Only the `app_*` DAGs
are set to start unpaused. Check the state before expecting a scheduled run.

Files: DAGs from `dags/yaml/*.yml` are built by `dags/load_yaml_dags.py`
(dag-factory). Task code is in `birddog/dag_tasks.py`, `dag_tasks_app.py` and
`dag_tasks_users.py`, and the Postgres writers are in `birddog/pipeline.py`.
Source DAGs come from `dags/load_sources.py`.

Asset URIs have the form `postgres://postgres:5432/birddog/core/page`. A DAG
with an `outlets` entry emits that asset when it succeeds, and a DAG scheduled
on the asset runs after it.

## Wiki pipeline (`dags/yaml/wiki.yml`)

Default pool `wiki_api` (3 slots, shared with every DAG that calls MediaWiki),
`max_active_runs: 1`, 2 retries, 120 s apart.

| DAG | Trigger | What it does | Assets | Params |
|---|---|---|---|---|
| `wiki_changes` | cron `0 3 * * *` | `ingest_recent_changes()`: reads wiki changes since the watermark into `raw.wiki_change` and marks the affected pages stale. On the first run (no watermark) it marks every archive page stale from the API instead. | out: `core/page` | none |
| `refresh_pages` | cron `15 * * * *`, or `core/page` | `stale_page_batches` takes up to 400 stale titles from `core.v_pages_stale`, in batches of 20. `refresh_page_batch` (mapped over the batches) fetches each page and writes `core.page`, its parent and child pages, and document links. | in: `core/page`; out: `core/document` | none |
| `refresh_doc_metadata` | cron `45 * * * *`, or `core/document` | Up to 1000 documents from `core.v_docs_no_meta`, in batches of 50, get MediaWiki file metadata (sha1, size, dimensions). | in: `core/document` | none |
| `backfill_from_dump` | manual | Loads every archive page from a Wikimedia dump (see below), then verifies a sample against the live wiki. Pool `default_pool` for the load, `wiki_api` for the verify. | out: `core/document` | `dump_date` (empty = newest finished dump) |

## Enrichment (`dags/yaml/enrich.yml`)

| DAG | Trigger | What it does | Notes |
|---|---|---|---|
| `translate` | cron `0 5 * * *` | Up to 5000 untranslated page texts from `core.v_untranslated`, in batches of 50 (at most 2 batches running). Fills `core.page.description_en`; each distinct text is translated once and cached in `core.translation`. | Needs `BIRDDOG_USE_GOOGLE_CLOUD_TRANSLATE=true` and `GOOGLE_TRANSLATE_API_KEY` in the Airflow environment. Without them the task raises `TranslationDisabledError`. Not in `.env.example` or compose today. |

## App-triggered DAGs (`dags/yaml/app.yml`)

These are started by the web app through the REST API (`birddog/airflow_jobs.py`).
They are unpaused on creation. Each takes a `conf` (the rendered `params`).

| DAG | Trigger | Params | What it does |
|---|---|---|---|
| `app_refresh_page` | web app (admin: database update) | `title`, `deep` (false), `update_locations` (false) | `page`: `refresh_page` refreshes one page and its children. `subtree`: `refresh_subtree` does nothing unless `deep`; with `deep` it marks the known descendants stale and refreshes them in passes of 20, up to 20 000 pages. With `update_locations`, documents of the page (or subtree) are set to `location_status = 'pending'`. Pool `wiki_api`; `max_active_runs: 4`. |
| `app_translate_page` | web app (translate button) | `title` | Translates the page's own text and its children's, using the shared cache. `max_active_runs: 4`. |
| `app_watchlist_check` | web app ("check now", with both params), or asset `core/page` (every `wiki_changes` run, with both params empty) | `email`, `archive_title` | Checks watch entries against `raw.wiki_change` and stores new alerts. With both params empty it checks every active watch, and one failing watch does not stop the others. `retries: 0`; `max_active_runs: 3`. |

The web app de-duplicates requests: a new run is not started while a queued or
running run has the same `conf`. Progress comes from task-instance states.
Cancel marks the run `failed`, which stops its tasks.

## Ops (`dags/yaml/ops.yml`)

Default `max_active_runs: 1`, 1 retry, 600 s apart.

| DAG | Trigger | What it does |
|---|---|---|
| `stack_check` | manual | Smoke test: `birddog` imports and the database has schemas `raw`, `core` and `app`. |
| `backup_postgres` | cron `0 2 * * *` | `pg_dump --format=custom` of `$BIRDDOG_DATABASE_URL`, copied with rclone to `gdrive-crypt:birddog-backups/daily/`. Sundays also go to `weekly/`, the 1st of the month to `monthly/`. Retention: daily 8 days, weekly 35 days, monthly 400 days. |
| `verify_restore` | cron `0 4 * * 0` (Sunday) | Restores the newest `daily/` dump into `birddog_restore_check` as `postgres`, compares table counts with the live database (fails if a restored table has under 90 % of the live rows), then drops the scratch database. |

**Not working yet.** Both backup DAGs need `deploy/rclone/rclone.conf` with the
`gdrive-crypt` remote, which is not on the server. See
[operations.md](operations.md#backups). Keep these DAGs paused until it is set up.

## One-off migrations (manual)

| DAG | File | What it does |
|---|---|---|
| `import_nocodb` | `dags/yaml/migration.yml` | `land` pulls NocoDB `pages`, `documents`, `ftp`, `locations` into `raw.nocodb_record` (one at a time), then `transform` upserts into `core`. Needs `NOCODB_IMPORT_TOKEN` and `NOCODB_IMPORT_BASE`. Safe to re-run. |
| `migrate_dynamodb_users` | `dags/yaml/migration_users.yml` | Copies AWS accounts, watchlists, preferences and KV tables into `app.*`. Params: `apply` (false = dry run, writes rolled back, counts reported), `include_logs` (false), `log_days` (30), `source_env`, `kv_tables` (`[bd_watchers]`). Needs AWS credentials in the Airflow environment. Not in `.env.example`. |

## Source DAGs (`dags/sources/`, built by `dags/load_sources.py`)

Each source folder gives one DAG, `source_<name>`, with four tasks: land,
transform, verify, expose. Folders starting with `_` are ignored.

| DAG | State | Trigger |
|---|---|---|
| `source_nocodb_locations` | enabled | manual (`schedule: null`) |
| `source_jewishgen_sftp` | **disabled** (`enabled: false`), so no DAG is built | cron `30 1 * * *` when enabled. Needs `SFTP_PASSWORD`. |

A source.yml that does not validate still produces a DAG, tagged `invalid`,
whose task fails with the problems. The workflow, the checks and the rollout
steps are in [dags/sources/README.md](../../dags/sources/README.md).

## Other

| DAG | File | Notes |
|---|---|---|
| `elyra_hello` | `dags/elyra/elyra_hello.py` | Two BashOperator steps exported from Elyra. A proof of the Elyra path, paused on creation. See [deploy/elyra/README.md](../../deploy/elyra/README.md). |

## Dump backfill and the watermark

The API route to load all archive pages takes days at MediaWiki's rate limits.
`backfill_from_dump` reads one Wikimedia pages-articles dump instead
(`birddog/dump_backfill.py`).

1. `latest_dump_date()` picks the newest dated folder at dumps.wikimedia.org
   (`ukwikisource`) whose `articlesdump` status is `done`, unless `dump_date` is set.
2. `download()` fetches the `.xml.bz2` into `BIRDDOG_DUMP_DIR` (default
   `/opt/birddog/.cache/dumps`, the `birddog-cache` volume). After a successful
   load, `prune_old_dumps()` deletes dumps (and `.part` leftovers) older than the
   one just loaded; newer ones are kept.
3. `load()` parses each page's wikitext into the same structure the live API
   returns, and writes it with `pipeline.write_page_info`. Commits every 500
   pages. One bad page is rolled back to its savepoint and counted as an error.
   Whether a linked page exists is decided from the dump itself.
4. Then it moves the **watermark** back to the newest revision timestamp in the
   dump, if the stored value is missing or later. The next `wiki_changes` run
   reads everything edited after the dump.
5. `verify_sample(size=300)` compares 300 random pages with the live API. The
   task fails if any match percentage is below 98. Template-generated links are
   the known gap.

**Order matters.** If `wiki_changes` runs before `backfill_from_dump`, it seeds
the page list from the live API (slow), and the dump then has to correct it.
Run the backfill first.

### The watermark

`raw.watermark` holds one row per feed. The wiki feed key is
`uk.wikisource.org:recentchanges:116` (116 is the namespace id of `Архів:`). The
value is an ISO timestamp in UTC (`2026-11-23T03:00:00Z`).

- `ingest_recent_changes` reads changes since the stored value and then sets the
  value to the **start** of its own run. Changes made during the run are picked
  up next time.
- The dump moves the value back (see above). Comparison is on the text value, so
  it relies on the ISO format.
- A source's watermark is `source:<name>`, for incremental sources only.

To re-read the wiki feed from a point, set the value (for example with
`UPDATE raw.watermark SET value = ... WHERE source = ...`) and run `wiki_changes`.
Do this on the server only with the owner's agreement.

## Adding a DAG

**From YAML** (most cases):

1. Add a block to a file in `dags/yaml/` (or a new `.yml`). Use the existing
   files as the pattern: a `default:` block for `catchup`, `default_args` (pool,
   retries), and `tags`. Give each task `decorator: airflow.sdk.task` and a
   `python_callable: birddog.<module>.<function>`.
2. Schedule: a cron string, `null` for manual, or `AssetOrTimeSchedule` for
   "cron or asset".
3. Params are passed to tasks as `"{{ params.name }}"`. Those values arrive as
   strings, so convert them (`_flag()` in `dag_tasks_app.py` is an example).
4. Dynamic mapping: an upstream task returns a list, and a downstream task has
   `expand: { arg: +upstream }`.
5. Write the function in the matching `birddog/dag_tasks*.py`. Keep it thin and
   put the work in `birddog/pipeline.py` or another module. Return something
   JSON-serialisable, a small summary.
6. Pool: set `pool: wiki_api` for anything that calls MediaWiki. A new pool needs
   `airflow pools set` (the `airflow-init` command already sets `wiki_api`).
7. Check it parses: see the DAG test in [local-dev.md](local-dev.md#dag-parse-tests).
8. Deploy: copy the files to the server. The dag-processor picks up changes, with
   no restart. A new DAG is paused until someone unpauses it.

**From Python** (rare): a file in `dags/` that defines a `DAG` object, as in
`dags/elyra/elyra_hello.py`.

**A data source**: a folder under `dags/sources/`. See
[dags/sources/README.md](../../dags/sources/README.md).

If the web app should start the DAG, add its id and conf to `birddog/airflow_jobs.py`,
and add a test in `test/test_dags.py` (it checks that the `app_*` DAGs exist and are unpaused).
