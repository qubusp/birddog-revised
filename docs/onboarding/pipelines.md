# Pipelines

Every DAG the stack runs, what triggers it, and what it touches. Schedules are
UTC.

**Paused or not.** Airflow runs with `DAGS_ARE_PAUSED_AT_CREATION=true`, and only
`dags/yaml/app.yml` sets `is_paused_upon_creation: false`. That decides the state
a DAG gets the first time Airflow sees it, nothing more: after that the state is
whatever someone last set in the UI, and it is kept in Airflow's database, not in
the repo. On the data box most DAGs have been unpaused by hand. State on
2026-11-23 (`airflow dags list`):

| Unpaused (schedules run) | Paused |
|---|---|
| `app_refresh_page`, `app_translate_page`, `app_watchlist_check`, `wiki_changes`, `refresh_pages`, `refresh_doc_metadata`, `backup_postgres`, `backfill_from_dump`, `import_nocodb` | `translate`, `verify_restore`, `stack_check`, `migrate_dynamodb_users`, `source_nocodb_locations`, `elyra_hello` |

`backup_postgres` is unpaused although `deploy/rclone/rclone.conf` is not on the
server, so it fails every night (see [Ops](#ops-dagsyamlopsyml)). Check the
current state before expecting a scheduled run:

```bash
ssh administrator@204.12.218.112 \
  'cd birddog-revised/deploy && docker compose exec -T airflow-scheduler airflow dags list -o plain'
```

Files: DAGs from `dags/yaml/*.yml` are built by `dags/load_yaml_dags.py`
(dag-factory). Task code is in `birddog/dag_tasks.py`, `dag_tasks_app.py`,
`dag_tasks_users.py` and `dag_tasks_docs.py`, and the Postgres writers are in `birddog/pipeline.py`.
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

## Location finder (`dags/yaml/locations.yml`)

| DAG | Trigger | What it does | Assets |
|---|---|---|---|
| `locate_documents` | cron `30 * * * *`, or `core/v_docs_no_location` (emitted by `app_refresh_page`) | `batches` takes up to 300 documents (param `limit`) from `core.v_docs_no_location`, oldest id first, in batches of 50. `locate` (mapped, pool `llm_api`) runs the finder in `birddog/locations/` over each batch, 10 documents per LLM call, and writes `core.document_location` and `location_status` (`found`, `none`, `failed`) after each call. | in: `core/v_docs_no_location` |

Code: `birddog/dag_tasks_locations.py`, `birddog/locations/pg_store.py`. The
extraction and matching logic is the legacy `LocationUpdateManager`'s, reading
`core.*` instead of NocoDB.

**To enable it** (it is paused, and skips while the key is missing):

1. Put `GROQ_LOCATION_KEY=<key>` (console.groq.com) in the data box's
   `deploy/.env`. The Airflow containers read `.env` only when created, so the
   key reaches the tasks after `docker compose up -d` recreates `airflow-*`.
   That restart needs the owner's agreement.
2. Create the pool: `airflow pools set llm_api 1 'LLM API callers (location extraction)'`.
   `airflow-init` does this on its next run. Without the pool, `locate`
   tasks wait in `scheduled` forever; `batches` runs in `default_pool`.
3. Unpause `locate_documents`.

Behaviour:

- **No key**: both tasks raise a skip; the run succeeds with skipped tasks and
  documents stay `pending`.
- **Rate limits**: `birddog.fetch` throttles `api.groq.com` and waits out 429s
  of up to 120 s. A longer one (daily token quota) stops the batch: finished
  calls are kept, the rest stay `pending` for the next hourly run.
- **Errors**: a failing call raises, so Airflow retries the task (2 retries,
  300 s apart; finished documents are not redone). On the last try the call's
  documents are marked `failed` instead, so one bad document does not block the
  head of the queue.
- **Fallbacks** (legacy behaviour): when no place is found, the document gets
  its archive's city (from `root_label`), if the archive is known; otherwise
  `none`. A failed or empty result never removes existing links.
- **Translation**: the finder translates the Cyrillic part of the file title
  through `core.translation`. With Google translation off (the default here)
  that part is left out, and the summary counts it as `title_untranslated`.
- Only English page descriptions (`core.page.description_en`) are used, as in
  the legacy code.
- **Throughput** (measured on 294 real pending documents with a mocked LLM):
  about 2 calls per 10 documents at worst (a second call for documents whose
  first pass found no settlement), about 4,800 characters (~1,500 tokens) of
  prompt plus up to ~500 output tokens per call. Per 1,000 documents: 100 to
  200 calls, 150k to 300k tokens. Groq's listed limits for
  `qwen/qwen3.8-27b` (30 RPM, 1k RPD, 8k TPM, 200k TPD) make the daily
  token quota the bound: about 650 to 1,300 documents a day, so a run of 300
  usually stops on the quota after a few hours and the rest wait.

## Book reading (`dags/yaml/books.yml`)

| DAG | Trigger | What it does |
|---|---|---|
| `read_books` | manual (pilot) | `enqueue` queues `enqueue` new PDF books of `doc_type` (default `Vitals`, optional `max_pages`) into `core.book_read`. `probe` (Haiku) looks at 3 sample pages of every pending book, measures tokens per page and estimates the whole book (Sonnet on every page plus the expected Opus re-reads, calibrated on the pilot; about 104 pages fit under 500k): unreadable scans stop, books over `auto_limit_tokens` (500k) go to `needs_approval`, the rest to `approved`. `read` (Sonnet; Opus re-reads the pages Sonnet marks illegible, plus low-confidence ones if `REREAD_LOW_CONFIDENCE` in `birddog/books.py` is set) reads approved books a few pages per call into `core.book_page`, until the run has spent `budget_usd` ($10); an interrupted book resumes next run. `assemble` (Haiku) groups spellings of the same place and fuzzy-matches them to `core.location` into `core.book_read.places`. All Claude tasks in pool `llm_api`. |
| `transcribe_books` | manual | Full transcription, with its own queue `core.book_transcript_queue` (the `read_books` rows stay as they are). `enqueue` queues `enqueue` books that `read_books` finished (status `done`) and/or the comma-separated `document_ids`. `transcribe` (Sonnet, 2 pages per streamed call; Opus redoes, asking for its best reading of every name with uncertain letters marked `?`, the pages Sonnet marks illegible or whose names came back unreadable: at least 2 entries, or half of them, with no readable person; 63 of the pilot's 380 pages) stores each page's text, register entries and places in `core.book_transcript`, until the run has spent `budget_usd` ($60); an interrupted book resumes next run. `reread_names: true` with `document_ids` puts finished books back (status `reread_names`) for that Opus step only, on pages whose best reading still needs it and was not yet read with the names prompt; then `index` rebuilds their entries. `index` (no API calls, runs even if `transcribe` failed) rebuilds `core.book_entry` / `core.book_entry_place` from each page's best reading and matches entry places to `core.location`. Read in `views.books__transcripts`, `books__entries`, `books__entry_places`, `books__transcription_progress`; search with `core.search_books(q)`. Claude task in pool `llm_api`. |
| `guess_unmatched_places` | manual | `guess_places` (Sonnet, low effort, 25 places per call, `budget_usd` $2, optional `limit`) gives a rough latitude/longitude, confidence and basis for each book place that matched no `core.location` row (from `read_books` and `transcribe_books`, deduplicated on `core.place_key(normalized, admin_context)`), into `core.place_guess`; places already guessed are skipped (delete a row to redo it). The same task ends `read_books` and `transcribe_books` (param `guess_budget_usd`, $1). Shown with the 3 nearest known locations in `views.books__unmatched_places`. Guesses are never written as links. Pool `llm_api`. |

Code: `birddog/books.py`, `birddog/book_transcripts.py`, `birddog/place_guess.py`, `birddog/dag_tasks_books.py`; tables: migrations 017, 023 and 024.

Entry years: register entries often give only day and month, so `views.books__entries.year` (and `books__entry_places.year`) falls back to the book's year: `core.book_year_range(document_id)` reads it from the title, else from the first 3 transcribed pages. A book covering a range ("1846-1854") gives no single year; `year_source` says where a year came from (`entry` or `book`), `book_year_from` / `book_year_to` give the book's range.
Page images come from Commons' page renderer (`iiurlparam=pageN-1600px`), so
PDFs are never downloaded. Costs are stored per book and per page.

**Approving** a book over the limit: it is listed in `views.books__needs_approval`
(Mathesar); set `status` to `approved` or `rejected` on the same `document_id` in
`core.book_read` (`birddog_ui` may update only `status` and `approval_note`), then
trigger the DAG again.

**Key**: Airflow connection `anthropic`, password = the API key
(`airflow connections add anthropic --conn-type generic --conn-password ...` in
the scheduler container). Stored in Airflow's database, so no restart. Without
it the Claude tasks skip.

Nothing writes `core.document_location` yet: the pilot's places are checked by
hand first.

## App-triggered DAGs (`dags/yaml/app.yml`)

These are started by the web app through the REST API (`birddog/airflow_jobs.py`).
They are unpaused on creation. Each takes a `conf` (the rendered `params`).

| DAG | Trigger | Params | What it does |
|---|---|---|---|
| `app_refresh_page` | web app (admin: database update) | `title`, `deep` (false), `update_locations` (false) | `page`: `refresh_page` refreshes one page and its children. `subtree`: `refresh_subtree` does nothing unless `deep`; with `deep` it marks the known descendants stale and refreshes them in passes of 20, up to 20 000 pages. With `update_locations`, documents of the page (or subtree) are set to `location_status = 'pending'`, and the `subtree` task emits `core/v_docs_no_location`, which starts `locate_documents`. Pool `wiki_api`; `max_active_runs: 4`. |
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
[operations.md](operations.md#backups). `verify_restore` is paused;
`backup_postgres` is unpaused and fails until the remote is set up (pause it, or
set up the remote).

## Documents (`dags/yaml/documents.yml`)

Consumers of the queue views `core.v_docs_need_dupe_check` and
`core.v_ftp_need_match`. Task code: `birddog/dag_tasks_docs.py`, work in
`birddog/doc_matching.py`. Both DAGs come up paused. Default 1 retry, 300 s apart.

| DAG | Trigger | What it does | Assets |
|---|---|---|---|
| `link_duplicates` | cron `55 * * * *`, or `core/document` | `batches` takes up to 20 000 sha1 hashes with an unchecked document, 1000 per batch (a hash group never spans two batches). `link` (mapped) links each group in `core.document_duplicate` and sets `dupes_checked`. | in: `core/document`; out: `core/document_duplicate` |
| `match_ftp_documents` | cron `40 */6 * * *`, or `core/ftp_entry` | `ftp_batches` / `ftp_fingerprint`: up to 2000 FTP files with a same-size document and no fingerprint are read over SFTP, 100 per batch, one batch at a time. `doc_batches` / `doc_fingerprint`: up to 2000 wiki files that share a size with a fingerprinted FTP file get theirs (imageinfo + two HTTP Range reads, pool `wiki_api`). `reconcile`: SQL only, writes `core.ftp_document` and `match_checked`. | in: `core/ftp_entry`; out: `core/ftp_document` |

**Duplicates.** Same rules as the legacy `link_hash_duplicates`. A group that
already has a prevailing document keeps it. Otherwise an imported document not
coded "duplicate" prevails, then any imported one, then the lowest id. The other
members get a link to it. A birddog-created member gets `process_code =
'duplicate'`. An imported member keeps its code and gets `anomaly` with
"hash duplicate of <url>" in `anomaly_comment`, once.

**FTP matching.** A fingerprint is `<size>:<sha1 of first 64 KB>:<sha1 of last
64 KB>` (the whole file below 128 KB), as in `birddog/ftp.py`. It is only
trusted while it starts with the row's current size. `reconcile`:

- puts a checked file back on the queue when a same-size document has not been
  compared with it yet, or its links disagree with the fingerprints;
- links a file to every same-size document with the same fingerprint (one file
  can back a Commons and a Wikisource document);
- removes a link only when every comparison for that file was made;
- marks the file `match_checked` when it has no same-size document, could not be
  read (`fp_error`), or every comparison was made.

`fp_error` (migration 014) records a file the SFTP server refused, or a wiki
file that is missing or changed size, so it is not retried on every run. The
`jewishgen_sftp` transform clears it when the file changes.

**Without `SFTP_PASSWORD`** (today), `ftp_batches` raises a skip and
`ftp_fingerprint` is skipped with it. `doc_batches` and `reconcile` have
`trigger_rule: none_failed`, so they still run on the fingerprints already
stored (3384 FTP files carry one from the NocoDB import). The run ends green.

**To turn on SFTP reading** (plan it with the owner, it recreates containers):

1. Add `SFTP_PASSWORD=...` to `deploy/.env` on the data box. The Airflow
   services read that file (`env_file: .env`), so no compose change is needed.
   Host, port and user come from `resources/ftp_config.json`
   (`sftp.jewishgen.org`, 22, `jbrandt`); `SFTP_HOST`, `SFTP_PORT`, `SFTP_USER`
   override them.
2. Recreate the Airflow containers so they get the variable.
3. Optionally set `enabled: true` in `dags/sources/jewishgen_sftp/source.yml`
   so the listing stays current. Its successful run triggers `match_ftp_documents`.
4. Unpause `match_ftp_documents`. Check the `ftp_fingerprint` logs for
   `connection lost` or `keeps dropping`: JewishGen rate-limits logins.

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
6. Pool: set `pool: wiki_api` for anything that calls MediaWiki, `pool: llm_api`
   for LLM calls. A new pool needs `airflow pools set` (the `airflow-init`
   command sets `wiki_api` and `llm_api`).
7. Check it parses: see the DAG test in [local-dev.md](local-dev.md#dag-parse-tests).
8. Deploy: `sh/deploy-data` (after `--dry-run`). DAG files and `birddog/` are
   bind-mounted, so the dag-processor picks up changes with no restart. A new DAG
   is paused until someone unpauses it (unless it sets `is_paused_upon_creation: false`).

**From Python** (rare): a file in `dags/` that defines a `DAG` object, as in
`dags/elyra/elyra_hello.py`.

**A data source**: a folder under `dags/sources/`. See
[dags/sources/README.md](../../dags/sources/README.md).

If the web app should start the DAG, add its id and conf to `birddog/airflow_jobs.py`,
and add a test in `test/test_dags.py` (it checks that the `app_*` DAGs exist and are unpaused).
