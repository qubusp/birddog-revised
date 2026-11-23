# Codebase map

Where things live, grouped by role. Paths are relative to the repo root.
Modules marked **legacy** are only used when `BIRDDOG_DATABASE_URL` is unset
(see [Legacy vs Postgres mode](#legacy-vs-postgres-mode)).

## birddog/: the library

### Pipeline and DAG tasks (Postgres writers)

| Module | What it does |
|---|---|
| `pipeline.py` | Postgres writers for the wiki pipeline: `ingest_recent_changes`, stale pages, `refresh_pages`, document metadata, `translate_pages`. Each step is safe to re-run. |
| `dag_tasks.py` | Task callables for `dags/yaml/wiki.yml`, `enrich.yml`, `ops.yml`, `migration.yml`. Holds the batch-size constants. |
| `dag_tasks_app.py` | Task callables for `dags/yaml/app.yml`: `refresh_page`, `refresh_subtree`, `translate_page`, `check_watches`. |
| `dag_tasks_users.py` | Task callables for `dags/yaml/migration_users.yml`; thin wrappers over `dynamo_migrate.py`. |
| `dump_backfill.py` | Loads core pages from a Wikimedia XML dump and verifies a live sample. See [pipelines.md](pipelines.md#dump-backfill-and-the-watermark). |
| `nocodb_import.py` (+ `nocodb_import.sql`) | One-off NocoDB to Postgres import: `land()` raw records, `transform()` into `core`. |
| `dynamo_migrate.py` | One-off copy of AWS app state (S3 accounts, DynamoDB watchlists and KV) into `app.*`. Dry run by default. |
| `database_updater.py` | Wiki record parsing and shaping (`normalize_url`, `form_document_record`, ...) used by `pipeline.py` and `dump_backfill.py`. Despite the name, only `DatabaseUpdater` (the class) is **legacy**: it writes to NocoDB. |
| `wiki.py` | MediaWiki API client, title canonicalisation, page URLs, archive roots. Used throughout. |
| `archive_roots.py` | Registry of archive roots: Postgres `core.archive_root` or the legacy KV store. |
| `change_feed.py` | `WikiChangeFeed`: reads `raw.wiki_change` for watchlist checks. |
| `significance.py` | Classifies watch alerts (for example `new_page`, `insignificant`). |
| `translate.py` | Google Cloud Translate client and the translation cache logic. Translation is disabled unless `BIRDDOG_USE_GOOGLE_CLOUD_TRANSLATE` is true. |
| `__main__.py` | `python -m birddog update_master_archive_list \| process_tracker_unknowns`. **Legacy** commands. |

### Catalog read layer (web app reads)

| Module | What it does |
|---|---|
| `catalog.py` | Read side of the web app: archives, pages (`page_payload()` builds the browse JSON from `core.*`), documents, search via `core.search()`. Read-only connections. `enabled()` is `pg.enabled()`. |
| `pg.py` | `BIRDDOG_DATABASE_URL` handling and `pg.connect()`, a connection per call that commits or rolls back. |
| `app_db.py` | Small per-process connection pool for the `app.*` stores (store, log, user_store). |

### Airflow client and jobs

| Module | What it does |
|---|---|
| `airflow_client.py` | Minimal client for the Airflow 3 REST API (`/api/v2`). Logs in with the `birddog-web` user, caches the JWT, retries once on 401. Also `get_client()`. |
| `airflow_jobs.py` | `AirflowJobs`: the web app's background jobs as DAG runs. Starts runs (de-duplicated on identical conf), reports progress, cancels by marking the run failed. |
| `runtime.py` | The web app's background runtime (page cache, wiki change poller, significance sweep). With Postgres it does not sync to NocoDB; it hands jobs to `AirflowJobs`. |

### State: store, log, user, cache, env

| Module | What it does |
|---|---|
| `env.py` | `detect_environment()`, `store_backend()` (see below), per-base table-name suffixes (legacy NocoDB base split). |
| `store.py` | `KeyValueStore` and `StringQueue`, one implementation per backend: Postgres (`app.kv`, `app.string_queue`), DynamoDB, SQLite. |
| `log.py` | `EventLogger` (request log) and `LogService` / service usage log, per backend. |
| `user.py`, `user_store.py` | Accounts, watchlists, preferences. With the Postgres backend, `user_store.py` uses `app.user_account`, `app.watch`, `app.user_preference`. |
| `cache.py` | Object cache for large payloads: local filesystem under `.cache/` or S3. Postgres backend means local filesystem. |
| `watcher.py` | Watchlist change detection and alert state. Uses the store. |
| `task.py` | `TaskManager` base class and task state, used by `translate.py`, `excel.py`, `database_updater.py`. |
| `tracker.py` | Document and page tracker state. The trackers run in the legacy runtime, and `python -m birddog process_tracker_unknowns` uses it. **Legacy**. |
| `timer.py` | Small timing helper, used by `nocodb_database.py`. **Legacy**. |

### Web app

| Module | What it does |
|---|---|
| `service.py` | Flask app: `create_app()`, all HTTP routes, `start_runtime_once()`. Local dev entry point: `python -m birddog.service`. |
| `application.py` (repo root) | WSGI entry point for gunicorn (`application:application`). |
| `excel.py` | Excel export (`/download`). Still runs inside the web app; imported by `service.py` and `runtime.py`. |
| `core.py` | Page and archive model and scraping logic (its docstring: "Ukraine records archive monitor and scraper"). Imported by `service.py`, `runtime.py`, `significance.py`, `excel.py`. Not reviewed in depth for this pack. |
| `utility.py` | Shared helpers, for example the multilingual text items (`form_text_item`, `get_text`, `match_text`). Imported almost everywhere. |
| `fetch.py` | HTTP fetch helpers (`fetch_url`, `make_session`). Used by wiki, translation and others. |

### Legacy NocoDB / DynamoDB modules

Kept for legacy mode, and for code that still imports them:

| Module | Status |
|---|---|
| `database.py` | Selects the output database. Always `NocoDBDatabase`, even in Postgres mode. |
| `nocodb_database.py` | NocoDB implementation of the output database. **Legacy**. |
| `abstract_database.py` | The `Database` interface and error types. Shared by old and new code (`InvalidRecordId` is imported by `database_updater.py`). |
| `database_importer.py` | Spreadsheet/NocoDB importer. **Legacy**; only `test/test_importer.py` imports it. |
| `ftp.py` | `FTPSiteManager`: FTP listing and matching against NocoDB. **Legacy** (the replacement is the source kit). |
| `locations/` | Experimental location tools (`matcher.py`, `finder.py`, `extractor.py`, `manager.py`). Manager writes NocoDB status fields. Not run by Airflow. |
| `google_drive_procs.py`, `download_convert_to_pdf.py` | No importers found in the repo. Candidates for removal, not verified against the owner's use. |
| `doc_compare/` | PDF and image comparison helpers. Only `test/test_compare_pdfs.py` imports them; no runtime caller found. |
| `dynamo_migrate.py` | Only the one-off AWS copy (see above). |

### Source kit

`sources/` turns a folder in `dags/sources/<name>/` into an Airflow DAG
`source_<name>` (land, transform, verify, expose). Read
[dags/sources/README.md](../../dags/sources/README.md) for the workflow.

| Module | What it does |
|---|---|
| `sources/__main__.py` | CLI: `python -m birddog.sources new \| validate \| fetch \| run`. |
| `sources/spec.py` | Schema for `source.yml`; validation errors. |
| `sources/fetchers.py` | Built-in fetchers: `http_json`, `csv`, `xlsx`, `gsheet`, `sftp_listing`, `ftp_listing`, `html_listing`, `python`. |
| `sources/runner.py` | `land`, `transform`, `verify`, `expose`. Writes `raw.source_run`, `raw.source_record`, `raw.watermark` (`source:<name>`). |
| `sources/dags.py` | Builds one DAG per source folder (`build_dags`). |
| `sources/scaffold.py` | Creates a new source folder (`new`). |

## dags/

| Path | What it is |
|---|---|
| `load_yaml_dags.py` | Parse-time loader: runs dag-factory over `dags/yaml/`. |
| `load_sources.py` | Parse-time loader: builds `source_<name>` DAGs from `dags/sources/`. |
| `.airflowignore` | Ignores `sources/` so the folders are not parsed as DAG files. |
| `yaml/` | `app.yml`, `wiki.yml`, `enrich.yml`, `ops.yml`, `migration.yml`, `migration_users.yml`. See [pipelines.md](pipelines.md). |
| `sources/` | One folder per data source: `nocodb_locations/`, `jewishgen_sftp/`. Each has `source.yml`, `transform.sql`, optional `fetch.py`. |
| `elyra/elyra_hello.py` | Proof-of-concept DAG exported from Elyra. Paused on creation. |

## deploy/

| Path | What it is |
|---|---|
| `docker-compose.yml` | The whole stack: services, memory limits, healthchecks, volumes, networks. |
| `.env.example` | Every variable the stack reads, with placeholder values. The real `deploy/.env` exists only on the server. |
| `airflow/Dockerfile`, `requirements.txt` | Airflow image: pg client 16, rclone, library dependencies. |
| `db/init/01-databases.sh` | Creates roles `airflow` and `birddog`, databases `airflow` and `birddog`. Runs on first volume init. |
| `db/init/02-ui.sh` | Roles `birddog_ui` and `mathesar`, database `mathesar`. Re-run on every deploy. |
| `db/init/03-reader.sh` | Role `birddog_reader`. Re-run on every deploy. |
| `db/init/04-apps.sh` | Roles and databases for ToolJet and Saltcorn. Re-run on every deploy. |
| `db/init/05-web.sh` | Role `birddog_web`. Re-run on every deploy. |
| `db/migrations/` | Numbered SQL migrations, applied in name order. See the list below. |
| `caddy/Caddyfile` | Public sites: airflow, data, hub, apps, saltcorn, birddog. |
| `web/` | Dockerfile, `gunicorn.conf.py` (one worker, 8 threads), README. |
| `apps/` | `saltcorn/setup.js`, `curator-pack.json`, `tooljet/build_curator.py`, `curator.json`. README. |
| `ui/` | Mathesar setup: `setup.sh` (admin, msar helper schemas, connection), `views.sh` (editable links), `nocodb_views.json` (the NocoDB view definitions it recreated). README. |
| `jupyterhub/` | Hub config, Dockerfiles (hub and per-user image), example notebook. README. |
| `elyra/` | Local DAG authoring with Elyra: components, pipelines, export script. README. |
| `rclone/` | README only. The real `rclone.conf` is gitignored and not set up. |

### Migrations

Applied by the `migrate` service on `docker compose run --rm migrate` (and by
`sh/deploy`). Every file is written to be re-run. **010 does not exist**; the
numbering goes 009 to 011.

| File | One line |
|---|---|
| `001_schemas.sql` | Creates schemas `raw`, `core`, `app`; `raw.watermark`, `raw.wiki_change`, `raw.sftp_listing`. |
| `002_core_app.sql` | `core` tables (archive_root, page, document, page_document, duplicates, location, translation, ftp_entry, ftp_document), first work-queue views, `app` user, watch and alert tables. |
| `003_doc_meta_queue.sql` | Redefines `core.v_docs_no_meta` to skip unresolvable files and non-wiki domains. |
| `004_ui_role.sql` | Grants to `birddog_ui`: read `core`, write seven curated tables, nothing in `app` or `raw`. |
| `005_nocodb_landing.sql` | `raw.nocodb_record`: NocoDB records as JSON, input to the import. |
| `006_curator_views.sql` | `views` schema: curator views that mirror the NocoDB grid views (`pages__*`, `documents__*`, `ftp_site__all`, `locations__*`). |
| `007_reader_role.sql` | Grants to `birddog_reader`: read `core` and `raw` (also future objects), nothing in `app`. |
| `008_search.sql` | `pg_trgm`, `core.search_vector()`, full-text and trigram indexes, `core.search()`. |
| `009_saltcorn_curator.sql` | `saltcorn` schema: search requests and hit/link views for the Saltcorn app. |
| `011_app_users.sql` | `app.kv`, `app.string_queue`, `app.request_log`, `app.service_log`; adjusts `app.user_account` and `app.watch`. |
| `012_web_role.sql` | Grants to `birddog_web`: read `core`, CRUD on `app`, write `core.archive_root`, nothing in `raw`. |
| `013_source_kit.sql` | `raw.source_run`, `raw.source_record`, source-kit comments. |
| `014_doc_matching.sql` | `fp_error` on `core.document` and `core.ftp_entry`; size and link-table indexes for duplicate and FTP matching. |

### Legacy vs Postgres mode

Two environment switches decide the mode. They are separate, and they can disagree.

| Question | Legacy | Postgres (the new stack) |
|---|---|---|
| Is `BIRDDOG_DATABASE_URL` set? (`pg.enabled()`) | no | yes |
| Where do page and archive reads come from? | live wiki and NocoDB | `core.*` via `catalog.py` |
| Does the web app sync to NocoDB in the background? | only if `BIRDDOG_NOCODB_ENV` is set (`runtime._legacy_sync_enabled()`) | no; background work is Airflow DAG runs (`AirflowJobs`) |
| Where is app state (accounts, watches, KV, queues, logs)? | `store_backend()`: SQLite locally, DynamoDB when `BIRDDOG_AWS_ENVIRONMENT` is set | Postgres `app.*` |
| Object cache | `.cache/` when `BIRDDOG_USE_LOCAL_CACHE` is set, otherwise S3 | `.cache/` (local filesystem) |
| Output database | NocoDB | Postgres `core.*` |

`store_backend()` (in `birddog/env.py`) is decided as follows:

1. `BIRDDOG_STORE_BACKEND` (`postgres`, `dynamodb` or `sqlite`) wins when set.
2. Otherwise `postgres` if `BIRDDOG_DATABASE_URL` is set.
3. Otherwise `dynamodb` on AWS and `sqlite` locally.

The trap: `BIRDDOG_STORE_BACKEND=sqlite` with `BIRDDOG_DATABASE_URL` set gives
Postgres catalog reads with local SQLite app state. Nothing warns about it.
`birddog/database.py` (the output database) is always NocoDB; the Postgres
pipeline writes through `pg.connect()` and does not use it.
