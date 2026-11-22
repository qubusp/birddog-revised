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
| `core.translation`, `core.ftp_entry`, `core.ftp_document`, `core.v_*` work-queue views | read only |
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
  have. initdb runs the script on a fresh volume, and `sh/deploy` runs it on every
  deploy, which also re-syncs the passwords from `.env`. To run it by hand:
  `docker compose exec -T postgres bash /docker-entrypoint-initdb.d/02-ui.sh`.
- `db/migrations/004_ui_role.sql` holds the grants for `birddog_ui`.
- `ui/setup.sh` is idempotent and runs on every deploy. It creates or updates the
  admin user, installs or upgrades the Mathesar SQL into `birddog` after an image
  bump, and connects the `birddog` database through `birddog_ui`.
- `.env` keys: `BIRDDOG_UI_DB_PASSWORD`, `MATHESAR_DB_PASSWORD`,
  `MATHESAR_SECRET_KEY`, `MATHESAR_ADMIN_USER`, `MATHESAR_ADMIN_PASSWORD`.
  `sh/deploy` appends any that are missing.
