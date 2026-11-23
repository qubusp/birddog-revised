# Data model

The Postgres database `birddog` (owner role `birddog`) holds the data. Migrations
are in `deploy/db/migrations/`, numbered and idempotent. This page lists what
matters. The column lists are not complete: read the migration for the full
definition. Migration-by-migration summary: [codebase-map.md](codebase-map.md#migrations).

## Schemas

| Schema | Purpose | Written by |
|---|---|---|
| `raw` | Data as received: wiki changes, source landings, NocoDB records, watermarks | Airflow (`birddog`) |
| `core` | Cleaned records, the system of record for pages, documents, locations | Airflow pipeline, curators (`birddog_ui`), web app (`archive_root` only) |
| `app` | The web app's state: accounts, watchlists, preferences, KV, queues, logs | web app (`birddog_web`) |
| `views` | Curator views for Mathesar: NocoDB-style grids and one `source__<name>` per source | migrations and the source kit's expose step |
| `saltcorn` | Helpers for the Saltcorn curator app (search requests, hit and link views) | migration 009 |

Mathesar also installs its helper schemas `msar`, `__msar` and `mathesar_types`
into the `birddog` database, as `birddog` (`deploy/ui/setup.sh`). They are not ours.

## raw

| Table | Contents |
|---|---|
| `raw.watermark` | `source` (PK), `value` (text), `updated_at`. Keys: `uk.wikisource.org:recentchanges:116` for wiki changes, `source:<name>` for each source. |
| `raw.wiki_change` | One row per wiki change: `site`, `title`, `rev_id`, `action` (edit, new, move, delete), `target_title`, `changed_at`, `payload`. Unique on (site, title, rev_id, action). Read by `change_feed.py`. |
| `raw.sftp_listing` | Created in 001. No code in the repo reads or writes it. The source kit's `sftp_listing` fetcher replaces the idea. |
| `raw.nocodb_record` | `table_name`, `nocodb_id`, `fields` (jsonb), `landed_at`. The NocoDB records as received, kept after the import. |
| `raw.source_run` | One row per land, transform or verify run of a source: `batch_id`, `source`, `status`, counts, watermarks, `checks`, `error`. |
| `raw.source_record` | Latest copy of each record of each source: `(source, key)` PK, `payload` (jsonb), `payload_hash`, `batch_id` (last change), `seen_batch_id` (last fetch that returned it). |

## core

| Table | Key columns | Notes |
|---|---|---|
| `core.archive_root` | `title` (PK), `label`, `description` | Registry of archive roots (the `Архів:` pages at top level). The web app can write it. |
| `core.page` | `id`, `url` (unique), `title`, `parent_id` (self), `label`, `root_label`, `level` (archive, fond, opus, ...), `native_description` (uk), `description_en`, `years`, `wiki_timestamp`, `availability` (linked, unlinked, redlinked, error), `stale`, `last_birddog_update` | `stale` default true; the refresh DAG clears it. Legacy import fields: `comments`, `reference_date`, `change_date`, `import_message`, `last_imported`. |
| `core.document` | `id`, `url` (unique), `title`, `domain`, `filename`, `wiki_timestamp`, `byte_size`, `mimetype`, `page_count`, `sha1_hash`, `content_fp`, `availability`, `lookup_status`, `location_status` (pending, found, none, failed), `dupes_checked`, `anomaly`, `description`, `process_code`, `doc_type[]`, `save_status`, `doc_scope`, `language` | `content_fp` is the fingerprint used for FTP matching; `fp_error` (014) says why one could not be taken. `old_*` columns keep the legacy codes as imported. |
| `core.page_document` | `page_id`, `document_id` (PK both) | Many-to-many. Use it instead of copying page fields onto documents. |
| `core.document_duplicate` | `prevailing_id`, `duplicate_id` | A duplicate points at the prevailing document. |
| `core.location` | `location_id` (PK), `modern_location_name`, `alternate_names`, `c1900_district` ... `c2000_province` | Administrative hierarchy by census year. |
| `core.document_location` | `document_id`, `location_id` | Document to location links. |
| `core.translation` | `source_hash` (PK, sha256 of the text), `source_text`, `translated_text`, `target_lang` | Translation cache shared by all pages, one row per distinct text. |
| `core.ftp_entry` | `path` (PK), `filename`, `folder`, `suffix`, `type` (dir, file), `size`, `mtime`, `depth`, `content_fp`, `match_checked`, `fp_error` (014) | Listing of the FTP/SFTP tree (today: NocoDB import; the JewishGen source when enabled). |
| `core.ftp_document` | `ftp_path`, `document_id` | Matches between FTP files and documents. |

### Work-queue views

Each view is a queue. A DAG or a curator works through the rows it returns.

| View | Rows are | Consumer |
|---|---|---|
| `core.v_pages_stale` | pages with `stale` | `refresh_pages` DAG |
| `core.v_untranslated` | pages with Ukrainian text and no `description_en` | `translate` DAG and `app_translate_page` |
| `core.v_docs_no_meta` | documents with no sha1 from wiki-hosted or Commons domains, not failed | `refresh_doc_metadata` DAG (defined in 002, redefined in 003) |
| `core.v_docs_need_dupe_check` | documents with sha1 not yet dupe-checked | `link_duplicates` DAG |
| `core.v_docs_no_location` | documents with `location_status = 'pending'` | **no consumer found**; set by `app_refresh_page` with `update_locations` |
| `core.v_ftp_need_match` | FTP files not yet matched | `match_ftp_documents` DAG (its `reconcile` step also puts checked files back) |

## app

| Table | Contents |
|---|---|
| `app.user_account` | `email` (PK), `name`, `password_hash` (werkzeug format, kept from S3), `role` (default `user`), timestamps |
| `app.user_preference` | `(email, key)` PK, `value` (jsonb) |
| `app.watch` | One row per watched archive: `(email, archive_title)` PK, `include[]`, `exclude[]`, `cutoff_date`, `last_checked_date`, `moved_to` |
| `app.alert_state` | Per-item alert inside a watch: `(email, archive_title, item_title)` PK, `resolved`, `modified`, `significance` (insignificant, new_page), `sig_span`, `entry` (jsonb). FK to `app.watch`. |
| `app.kv` | Generic key-value store: `(store, namespace, key)` PK, `value` (text). `store` is the former DynamoDB or SQLite table name. |
| `app.string_queue` | Leased queue: `store`, `queue_name`, `value`, `lease_until_ms`, `lease_owner`. |
| `app.request_log`, `app.service_log` | Request and external-service call logs. `source_key` is set for rows copied from AWS. |

## views

| View group | Contents |
|---|---|
| `views.pages__*` | `all`, `recently_updated`, `cdiak`, `juliana_test` (the NocoDB view of that name, kept as is), `import_messages` |
| `views.documents__*` | `all`, `recently_updated`, `multi_owner`, `orphans`, `has_duplicates`, `cdiak` |
| `views.ftp_site__all`, `views.locations__*` | FTP listing and locations (`locations`, `has_documents`) |
| `views.source__<name>` | Created by the source kit's expose step for each source. Granted to `birddog_ui` by the step itself. |

Views always start with the record key (`id`, `path`, `location_id`) so that
Mathesar can edit through the underlying table. `deploy/ui/views.sh` adds an
"Editable version" link where the filter allows it.

## saltcorn

| Object | Contents |
|---|---|
| `saltcorn.search_request` | `q` (1 to 200 characters), `requested_at`. Each search a curator runs is logged here. |
| `saltcorn.search_hit` (view) | `core.search(q, 50)` for each distinct `q`. Always filter on `q`. |
| `saltcorn.page_document_link` (view) | `core.page_document` with page and document titles for the Saltcorn screens. |

## core.search()

```sql
SELECT * FROM core.search('ДАЗО 1-1-12');        -- default max_rows 50
SELECT * FROM core.search('дахмо 315', 20);
```

Returns `(kind, id, title, url, snippet, score)`, where `kind` is `page` or
`document`. The logic is in `008_search.sql`:

1. **Keyword**: full-text search with the `simple` configuration (no Ukrainian
   stemmer ships with Postgres). `/ - _ : .` count as spaces, so `ДАЗО 1-1-12`,
   `ДАЗО/1/1/12` and `File:ДАЗО_1-1-12.pdf` share the same words. A page whose
   code is the query gets a bonus.
2. **Fuzzy**: trigram similarity (`pg_trgm`, threshold 0.5) on titles and labels,
   used only to fill the rest of `max_rows` after keyword hits.

Callers: `birddog/catalog.py` (web app search), `saltcorn.search_hit`, notebooks.

## Roles

Created by `deploy/db/init/*.sh` (they need CREATEROLE, which `birddog` lacks, so
they run as `postgres`, on first volume init and on every deploy). Grants are in
the migrations, which run as `birddog` and so work once the roles exist.

| Role | Used by | May | May not |
|---|---|---|---|
| `postgres` | operators, init scripts, restore check | everything | - (not used by the app) |
| `birddog` | the `migrate` service, every Airflow DAG (`BIRDDOG_DATABASE_URL`, `AIRFLOW_CONN_BIRDDOG_PG`), backups | own the `birddog` database; create objects; default privileges give `birddog_reader` and `birddog_web` their rights on new tables | nothing; it owns the database and its objects |
| `birddog_ui` | Mathesar, Saltcorn (`postgres-tables`), ToolJet datasource | read `core` and `views`; insert, update, delete on `core.page`, `core.document`, `core.location`, `core.archive_root`, `core.page_document`, `core.document_location`, `core.document_duplicate`; insert into `saltcorn.search_request`; run `core.search()` | touch `app` or `raw` (revoked); write `core` tables not listed in 004 |
| `birddog_reader` | JupyterHub user containers | read `core` and `raw` (tables, views, sequences, including future ones); run `core.search()` | write anything; read `app`. Session is read-only by default, idle transactions end after 10 minutes, 30 connections max. |
| `birddog_web` | the Flask app (`birddog-web`) | read `core`; insert, update, delete `core.archive_root`; full CRUD on `app.*` | read or write `raw`. 40 connections max, idle transactions end after 5 minutes. |
| `airflow` | Airflow metadata (database `airflow` only) | own database `airflow` | has no rights in `birddog` |
| `tooljet` | ToolJet metadata (`tooljet`, `tooljet_db`, `tooljet_sample`) | own those databases | cannot connect to `birddog`; the app reaches data as `birddog_ui` |
| `saltcorn` | Saltcorn metadata (`saltcorn`) | own that database | cannot connect to `birddog`; the app reaches data as `birddog_ui` |
| `mathesar` | Mathesar metadata (`mathesar`) | own that database | reaches `birddog` as `birddog_ui` |

Notes:

- The `birddog` role has no CREATEDB, so a test or scratch database has to be
  created by `postgres` (see [local-dev.md](local-dev.md)).
- Default privileges (`ALTER DEFAULT PRIVILEGES FOR ROLE birddog`) are set for
  `birddog_reader` (core, raw) and `birddog_web` (core select, app CRUD). They
  are **not** set for `birddog_ui`. A new `core` table is invisible to curators,
  and not writable by them, until a migration grants it.
- `GRANT EXECUTE ON core.search` names only `birddog_ui` and `birddog_reader`.
  PostgreSQL grants EXECUTE to PUBLIC on new functions by default, and nothing
  revokes it, so every role that can connect to `birddog` can call the function.
  `birddog_web` calls it in `catalog.py` and is not in the grant. See the owner
  report for this and other inconsistencies.
