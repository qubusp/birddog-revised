# Data sources (the source kit)

Every external data source Birddog ingests - another archive website, an
FTP/SFTP listing, a spreadsheet, an API - is one folder here:

```
dags/sources/<name>/
    source.yml       what it is, where it comes from, when it runs, what it produces, how it's checked
    transform.sql    landed records -> core rows
    fetch.py         only if no built-in fetcher fits
    view.sql         optional: your own Mathesar view instead of the generated one
```

`dags/load_sources.py` turns each folder into an Airflow DAG `source_<name>`
with four tasks, always the same pattern:

```
land  ->  transform  ->  verify  ->  expose
```

| step | what happens | where |
|---|---|---|
| **land** | the fetcher runs; every record is stored *unchanged* as JSON in `raw.source_record` (one row per key). A record is only rewritten when its content changed, and then carries the batch that changed it. The run is logged in `raw.source_run`, the source's watermark in `raw.watermark` (`source:<name>`). Runs in the source's pool, with its retries. | `birddog/sources/runner.py` `land()` |
| **transform** | your `transform.sql`, in one transaction, over the records landed since the last successful transform (temp table `landed`). Unchanged records never reach core, so re-runs don't overwrite curator edits. | `transform()` |
| **verify** | the checks in `source.yml`. Any failure fails the run; results are stored in `raw.source_run.checks`. The `produces` assets are emitted only when verify passes, so downstream DAGs never run on unverified data. | `verify()` |
| **expose** | (re)creates `views.source__<name>`: the landed records, one text column per field, for Mathesar and SQL. | `expose()` |

Code: `birddog/sources/` (spec, fetchers, runner, dags, scaffold). Tables:
`deploy/db/migrations/013_source_kit.sql`.

## Onboarding a source, step by step

1. **Scaffold** (from the repo root):

   ```
   python -m birddog.sources new archive_xyz --type html_listing
   ```

   Types: `http_json`, `csv`, `xlsx`, `gsheet`, `sftp_listing`, `ftp_listing`,
   `html_listing`, `python`. This creates `dags/sources/archive_xyz/` with a
   commented `source.yml`, a `transform.sql` template, and for `python` a
   `fetch.py`. Folders starting with `_` are ignored (use them for drafts).

2. **Describe the fetch.** Fill in the `fetch:` block. Try it without
   touching the database - it prints the first records:

   ```
   python -m birddog.sources fetch archive_xyz --limit 5
   ```

   Pick the **key**: the field (or list of fields) that identifies a record
   across runs - a URL, a path, an id. Records must have it.

   Pick the **mode**: `full` if every run returns the whole source (then
   records that vanish show up in the `gone` view and as `in_source = false`),
   `incremental` if it only returns new/changed records - usually with a
   `watermark:` (`field:` whose maximum is remembered and handed to the next
   fetch as `ctx.watermark`; `http_json` sends it as `watermark_param`).

3. **Write the transform.** In `transform.sql` you can read:

   | name | contents |
   |---|---|
   | `landed` | records new or changed since the last transform: `key, payload, batch_id, landed_at, first_landed_at` |
   | `landed_all` | every current record of the source |
   | `gone` | records the last full fetch didn't return (mode `full`) |
   | `current_setting('birddog.source')`, `current_setting('birddog.batch_id')` | the source name, the batch |

   Write it idempotent (`INSERT ... ON CONFLICT DO UPDATE`): if it fails, the
   next run retries the same records plus newer ones. If the source needs new
   core tables, add them in a migration first (see below).

4. **Add checks** under `verify:` - at least one:

   | check | passes when |
   |---|---|
   | `landed` (`min`, `max_shrink`) | the fetch returned at least `min` records and (full mode) didn't shrink by more than `max_shrink` (0.1 = 10%) since the last good run |
   | `row_count` (`table`, `min`, `max`, `where`) | the core table has that many rows |
   | `null_rate` (`table`, `column`, `max`, `where`) | at most `max` (0-1) of the rows are NULL or empty |
   | `sample_match` (`table`, `key_column`, `fields`, `size`, `min_percent`, `key_field`, `where`) | at least `min_percent` of a random sample of landed records have a core row (`key_column` = the landing key, or the payload's `key_field`) whose columns equal the payload fields (`fields: {payload_field: core_column}`) |
   | `sql` (`sql`, `name`) | the query's single value is true |

5. **Declare outputs:** `produces:` lists the tables written, as assets
   (`core/location` is short for `postgres://postgres:5432/birddog/core/location`,
   the URIs the wiki DAGs use). Another DAG can then run when this source has
   delivered: `schedule: {cron: "0 * * * *", assets: [core/location]}` in a
   source.yml, or the same `AssetOrTimeSchedule` in `dags/yaml/*.yml`.

6. **Schedule, pool, owner:** `schedule:` is `null` (trigger by hand), a cron
   string, or `{cron, assets}`. Anything calling the wiki API goes in pool
   `wiki_api`; others stay in `default_pool`. `owner`, `description` and
   `notes` show up in the Airflow UI (DAG docs) and the view's description.

7. **Validate** - this is what the DAG loader does at parse time:

   ```
   python -m birddog.sources validate archive_xyz
   ```

   A source.yml that doesn't validate still appears in Airflow, as
   `source_<name>` tagged `invalid`, with the problems in its docs and a task
   that fails with them.

### Secrets

Never in `source.yml`. Options ending in `_env` name an environment variable
(`password_env: SFTP_PASSWORD`), header values can be `env:VAR`, and a
python fetcher calls `ctx.env("token_env")`. The variable must exist in the
Airflow containers' environment (`deploy/docker-compose.yml`,
`x-airflow-common`), which means a container recreate - plan it with whoever
runs the server. Sources that only read public URLs need nothing.

## Testing locally

- `python -m birddog.sources validate` - all sources.
- `python -m birddog.sources fetch <name>` - the fetcher alone, nothing written.
- A full run against a scratch database (never the live one while
  developing): create one with the migrations applied, then

  ```
  BIRDDOG_DATABASE_URL=postgresql://.../birddog_scratch python -m birddog.sources run <name>
  python -m birddog.sources run <name> --steps transform,verify   # re-run just these
  python -m birddog.sources run <name> --full-refresh             # ignore the watermark
  ```

- Tests: `pytest test/test_sources_spec.py` (schema, scaffolding, fetchers),
  `test/test_sources_dags.py` (DAGs parse; needs Airflow installed), and
  `test/test_sources_db.py` (the whole pipeline on a scratch database it
  creates and drops; set `SOURCEKIT_ADMIN_DSN`, see the file's docstring).
  When you add a source, add a case there that runs your transform on a few
  hand-made records (`runner.run(folder, records=[...])` skips the fetcher).

## Rolling out

1. Merge, then copy the folder to the server:
   `dags/sources/<name>/` -> `~/birddog-revised/dags/sources/<name>/`.
   The dag-processor picks up new and changed sources on its next parse
   (`dags/` is mounted into the Airflow containers); **no restart**. The
   kit's own code is in `birddog/sources/`, also mounted.
2. New DAGs start **paused**. Look at `source_<name>` in the Airflow UI,
   trigger it once by hand, check the verify task's result and
   `views.source__<name>` in Mathesar, then unpause it if it has a schedule.
3. Only if the source needs new core tables or columns: add a migration
   (`deploy/db/migrations/NNN_*.sql`, idempotent) and apply it with the
   migrate service before the first run.
4. Only if it needs a new secret: see Secrets above.

First rollout of the kit itself: copy `birddog/sources/`, `dags/load_sources.py`,
`dags/.airflowignore`, `dags/sources/`, and apply
`deploy/db/migrations/013_source_kit.sql` (the migrate service runs every
file in order; 013 is idempotent). Until 013 is applied, source DAGs parse but
their runs fail at the land step.

## Examples

- **`nocodb_locations`** (enabled, manual): the NocoDB Locations table into
  `core.location`, with a python fetcher reusing `birddog/nocodb_import.py`'s
  paced client. It runs alongside the one-off `import_nocodb` DAG, which is
  unchanged. Unlike that import it only writes locations whose NocoDB content
  changed since its last run - except the very first run, when every record
  is new to the kit and is written once.
- **`jewishgen_sftp`** (disabled): the JewishGen SFTP tree into
  `core.ftp_entry` with the built-in `sftp_listing` fetcher and no Python;
  waiting for `SFTP_PASSWORD` in the Airflow environment.

## Monitoring

```sql
SELECT batch_id, source, status, fetched, changed, started_at, error
FROM raw.source_run ORDER BY batch_id DESC LIMIT 20;

SELECT source, count(*), max(landed_at) FROM raw.source_record GROUP BY 1;
```
