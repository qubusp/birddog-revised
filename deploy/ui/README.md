# Curator UI (Mathesar)

[Mathesar](https://mathesar.org) 0.12.0 (GPLv3) gives curators spreadsheet-style
grids and record forms over the Birddog database, taking over NocoDB's role for
browsing and editing records.

## Opening it

<https://data.204-12-218-112.sslip.io>, served over HTTPS by Caddy
(`deploy/caddy/Caddyfile`) behind Mathesar's own login. The base domain is
`PUBLIC_DOMAIN` in `deploy/.env`. On the server itself it also answers on
<http://127.0.0.1:8000>.

## Admin login

User `MATHESAR_ADMIN_USER` (default `admin`), password `MATHESAR_ADMIN_PASSWORD`, both in
`~/birddog-revised/deploy/.env` on the server:

    ssh administrator@204.12.218.112 'grep ^MATHESAR_ADMIN ~/birddog-revised/deploy/.env'

`ui/setup.sh` resets the admin password to the `.env` value on every deploy. To change it,
edit `.env` and redeploy, or run `bash ui/setup.sh` from `deploy/`.

## What curators can edit

Everyone, including the admin, reaches the `birddog` database as the Postgres role
`birddog_ui`. Postgres enforces its grants (`db/migrations/004_ui_role.sql`), so
the UI can't get around them:

| Schema / table | Access |
|---|---|
| `core.page`, `core.document` (curation fields: process_code, doc_type, save_status, doc_structure, doc_scope, language, processor, pages_processed, when_transcribed, comments, anomaly, anomaly_comment, ...), `core.location`, `core.archive_root` | read / insert / update / delete |
| link tables `core.page_document`, `core.document_location`, `core.document_duplicate` | read / insert / update / delete. Mathesar shows them as linked records on a record's page |
| `core.translation`, `core.ftp_entry`, `core.ftp_document`, `core.v_*` work-queue views, `views.*` curator views | read only |
| `app.*` (accounts, watches, alert state), `raw.*` | no access. The schemas show in the list, but opening them fails with "permission denied" |

The grants are on whole tables, so the pipeline-owned columns of
`core.document` and `core.page` (sha1, availability, ...) can also be edited.
Curators should change only the curation fields.

Mathesar keeps its own users and settings in a separate `mathesar` database. It also
installs helper schemas (`msar`, `__msar`, `mathesar_types`) into `birddog`.
`ui/setup.sh` installs them as `birddog`, so `birddog_ui` never needs CREATE on the
database.

## Adding a curator

1. Log in as admin, go to **Administration → Users → Add user**. Give the curator a
   username and a temporary password. Leave the user as a standard user, not admin.
2. Open the **Birddog** database, then **Collaborators → Add collaborator**. Pick the
   user and the role **birddog_ui**.
3. Send the curator the URL and their login. Mathesar asks them to change
   the password the first time they log in.

Don't add any other Postgres role to Mathesar. It would bypass the restrictions above.

## How it's wired

- `docker-compose.yml`, service `mathesar`, published on `127.0.0.1:8000`, with
  `mathesar-media` as its volume.
- `db/init/02-ui.sh` (runs as `postgres`) creates the roles `birddog_ui` and `mathesar`
  and the database `mathesar`. Role creation needs CREATEROLE, which `birddog` doesn't
  have. initdb runs the script on a fresh volume, and `sh/deploy-data --init-roles`
  re-runs it, which also re-syncs the passwords from `.env`. To run it by hand:
  `docker compose exec -T postgres bash /docker-entrypoint-initdb.d/02-ui.sh`.
- `db/migrations/004_ui_role.sql` holds the grants for `birddog_ui`.
- `ui/setup.sh` is idempotent. No deploy script runs it since the two-box split
  (its `ui/views.sh` step execs into `postgres`, on the data box); run it by hand. It creates or updates the
  admin user, installs or upgrades the Mathesar SQL into `birddog` after an image
  bump, and connects the `birddog` database through `birddog_ui`.
- `.env` keys: `BIRDDOG_UI_DB_PASSWORD`, `MATHESAR_DB_PASSWORD`,
  `MATHESAR_SECRET_KEY`, `MATHESAR_ADMIN_USER`, `MATHESAR_ADMIN_PASSWORD`.
  `sh/deploy-apps` adds the missing ones on the apps box, except
  `BIRDDOG_UI_DB_PASSWORD`, which must be copied from the data box.

## Saved views (recreated from NocoDB)

The curator-facing grid views of the NocoDB STAGE base are recreated as Postgres
views in schema **`views`** (`db/migrations/006_curator_views.sql`). They show up in
Mathesar under the `views` schema, and any SQL tool can query them. The full NocoDB
export (31 views: columns, order, visibility, groupings, lock and personal flags, row
counts, inferred filters and sorts with the evidence for each) is in
`ui/nocodb_views.json`.

**Views are read-only in Mathesar 0.12.** Mathesar edits a row through its table's
primary key (`msar.get_pk_column` reads `pg_constraint`), and a Postgres view can't
have one. A `records.patch` on a view row fails with `NullValueNotAllowed`. Mathesar
also has no saved filters on tables: its table metadata stores only column order and
the record summary. It does keep a table's filter, sort and hidden columns in the page
URL (`?q=`). `ui/views.sh` (run by `ui/setup.sh`) builds that URL for every view whose
filter uses only its own table's columns. It appends the URL to the view's description
as **"Editable version"**. To find records, use the view. To edit them, open the
editable version, or open the record in its `core` table by the `id` / `location_id`
shown in the first columns.

| NocoDB table / view | Postgres view | Filter | Sort (apply in the UI) | Editable version |
|---|---|---|---|---|
| Pages / All | `views.pages__all` | none | id | yes |
| Pages / Recently Updated | `views.pages__recently_updated` | last_birddog_update or last_imported within 1 day | unknown | no (relative date) |
| Pages / CDIAK | `views.pages__cdiak` | root_label = CDIAK | seq_label | yes |
| Pages / Juliana test | `views.pages__juliana_test` | root_label = DACHVO and level in (archive, fond, opus) | label | yes |
| Pages / Import Messages | `views.pages__import_messages` | import_message not blank | UpdatedAt desc, so use last_imported desc | yes |
| Documents / All | `views.documents__all` | none | id | yes (without page fields) |
| Documents / Recently Updated | `views.documents__recently_updated` | as for Pages | unknown | no |
| Documents / Multi-Owner | `views.documents__multi_owner` | num_owners > 1 | id | no (link count) |
| Documents / Orphans | `views.documents__orphans` | num_owners = 0 | id | no |
| Documents / Has Duplicates | `views.documents__has_duplicates` | num_duplicates > 0 | id | no |
| Documents / CDIAK | `views.documents__cdiak` | none (NocoDB had none either) | label | yes (unsorted) |
| FTP Site / All | `views.ftp_site__all` | none | first_seen | n/a (read-only table) |
| Locations / Locations | `views.locations__locations` | none | location_id | yes |
| Locations / Has Documents | `views.locations__has_documents` | num_documents > 0 | location_id | no |

`views._page_base` and `views._document_base` are helpers. They add the link lookups
to `core.page` / `core.document`: the parent title, child count, linked document
titles, owning pages, duplicates, duplicate_of, FTP items, locations and their counts,
plus the fields copied from a document's owning page.

Not recreated:
- Pipeline queues ("BD:*"). Untranslated, Missing Metadata, Need Dupe Check, Need
  Locations and Need Match Check already exist as `core.v_*`. BD:WDT, BD:Need Page
  Lookups and BD:Directories are internal loop cursors.
- Personal views (Jon Debug on Pages and Documents, Has Wikisource Dup) and the
  developer "Alex Debug" / "debug" views.
- Documents / Juliana test. It was empty at export, so its filter couldn't be
  inferred (see below).

NocoDB features that can't be reproduced:
- **Exact filter and sort definitions.** The import token lacks NocoDB's
  `filterList`/`sortList` permissions (HTTP 403). Filters and sorts were inferred from
  record membership and order through the data API (`nocodb_views.json` →
  `inference_evidence`). Views that were empty at export (Recently Updated, Orphans,
  Has Duplicates, Has Documents) follow their name and description, so they're
  unverified. Re-export with a token that has full meta permissions to confirm them.
- **Sort order.** Mathesar ignores a view's ORDER BY, so each view's sort is in its
  description.
- **Groupings.** Only the skipped debug views were grouped.
- **CreatedAt / UpdatedAt.** Core has no row-modified timestamp, so "Recently
  Updated" uses pipeline and import timestamps and misses curator edits.
- **Fields core doesn't store.** On Documents: thumb_*, save_pending,
  native_description. On Locations: region, jewish_population, notes, latitude,
  longitude, *_town, *_country.
- **Link fields.** They show as newline-joined titles, not as clickable chips.
  Pages.children shows as a count, because archive pages have thousands of children.
- **Per-view lock and personal flags.** Every curator sees every view.
