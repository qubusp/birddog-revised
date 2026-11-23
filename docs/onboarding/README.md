# Birddog onboarding

Start here. This pack describes the rebuilt Birddog stack on branch `day0-stack`:
one Intel Ubuntu server running Docker Compose. It is written from the code and
config in this repo. Where the code is unclear or inconsistent, the docs say so.

## What Birddog does

Birddog monitors and tracks Ukrainian archival documents hosted on
uk.wikisource.org (the `Архів:` namespace).

- **Pages**: archive pages (archive, fond, opus and children) in `core.page`,
  refreshed from the wiki when they change.
- **Documents**: files linked from pages (wiki-hosted files and Commons) with
  MediaWiki metadata (sha1, size, dimensions) in `core.document`, plus duplicate
  and curation fields.
- **FTP/SFTP matching**: a remote tree is listed into `core.ftp_entry` and its
  files are matched to documents by content fingerprint. The source is defined
  (JewishGen SFTP, disabled) but the matcher is not wired into any DAG yet.
- **Locations**: documents are tagged with modern locations (`core.location`,
  `core.document_location`) by the `locate_documents` DAG, which uses an LLM
  (Groq) to pull place names out of descriptions. It skips until
  `GROQ_LOCATION_KEY` is set (see [pipelines.md](pipelines.md#location-finder-dagsyamllocationsyml)).
- **Translation**: Ukrainian page descriptions are machine-translated to English
  into `core.page.description_en`, cached per text in `core.translation`.
- **Watchlists**: a user watches an archive, and wiki changes produce alerts the
  user resolves (`app.watch`, `app.alert_state`).

Curators edit the curated `core` tables in Mathesar (and in the Saltcorn and
ToolJet apps). Analysts read `core` and `raw` from JupyterHub notebooks through a
read-only role. The Flask web app (`birddog-web`) serves browse, search,
watchlist and export features to users.

## Target architecture

Two servers share one Docker Compose project (`deploy/docker-compose.yml`,
project name `deploy`), each with its own override; see
[deploy/two-box.md](../../deploy/two-box.md). The data box (204.12.218.112)
runs Postgres and Airflow and serves `airflow.204-12-218-112.sslip.io`. The apps
box (108.181.191.55) runs the web app and UI tools and serves
`<service>.108-181-191-55.sslip.io`. They talk over a WireGuard link. Caddy on
each box issues Let's Encrypt certificates. Every other port is bound to `127.0.0.1`
and reached through an SSH tunnel.

| Service | Image / code | Public host | Local port | Role |
|---|---|---|---|---|
| `caddy` | caddy:2.11.8 | 80, 443 | - | HTTPS reverse proxy for the sites below |
| `postgres` | postgres:16 | - | 5432 | data box. System of record: database `birddog`; also Airflow's metadata |
| `apps-postgres` | postgres:16 | - | - | apps box. Metadata of Mathesar, ToolJet and Saltcorn |
| `migrate` | postgres:16, one-shot | - | - | applies `deploy/db/migrations/*.sql` in order |
| `airflow-apiserver` | `birddog-airflow:local` (apache/airflow:3.3.2) | `airflow.` | 8080 | Airflow UI and REST API |
| `airflow-scheduler`, `airflow-dag-processor` | same image | - | - | LocalExecutor; task processes run in the scheduler container |
| `airflow-init` | same image, one-shot | - | - | DB migrate, admin user, pool `wiki_api` (3 slots) |
| `birddog-web` | `birddog-web:local` (python 3.12, gunicorn) | `birddog.` | 8084 | Flask web app, reads Postgres, starts DAG runs via REST |
| `mathesar` | mathesar/mathesar:0.12.0 | `data.` | 8000 | curator UI, connects as `birddog_ui` |
| `jupyterhub` | `birddog-jupyterhub:local` | `hub.` | 8001 | shared notebooks, per-user containers as `birddog_reader` |
| `tooljet` | tooljet-ce:v3.20.256-lts | `apps.` | 8082 | low-code curator app |
| `saltcorn` | saltcorn/saltcorn:1.6.4 | `saltcorn.` | 8083 | low-code curator app |

The `birddog` database has four schemas of ours: `raw` (data as received), `core`
(cleaned records, the system of record), `app` (web app state) and `views` (curator
views), plus `saltcorn` (helpers for the Saltcorn app). Details are in
[data-model.md](data-model.md).

Airflow DAGs come from two places: YAML files built by dag-factory
(`dags/yaml/`) and one folder per data source (`dags/sources/`). Both are
bind-mounted into the Airflow containers, so DAG changes do not need an image
rebuild.

## Data flow

1. **wiki_changes** (daily, 03:00 UTC) reads the wiki's recent changes into
   `raw.wiki_change` and marks changed pages stale (`core.page.stale`).
2. **refresh_pages** (hourly, and after each wiki_changes run) fetches stale pages
   and writes `core.page`, child pages, `core.document` and `core.page_document`.
3. **refresh_doc_metadata** fills in MediaWiki file metadata for new documents.
   **translate** (daily, 05:00 UTC) fills `core.page.description_en`.
4. **Sources** land records unchanged in `raw.source_record`, transform them into
   `core` rows, verify them, and expose `views.source__<name>` for Mathesar.
5. **Web app** reads `core.*` (`birddog/catalog.py`, `core.search()`) and writes
   its own state to `app.*`. Slow work (page refresh, translation, watch check)
   is started as an Airflow DAG run through the REST API
   (`birddog/airflow_client.py`, `birddog/airflow_jobs.py`); the app reads run
   state back from Airflow.
6. **Curators** write the curated `core` tables through `birddog_ui` (Mathesar,
   Saltcorn, ToolJet). **Analysts** read `core` and `raw` through `birddog_reader`.

Roles and their rights are in [data-model.md](data-model.md).

## Legacy mode

The code still contains the pre-migration app: AWS Elastic Beanstalk, DynamoDB
and S3 for app state, and NocoDB as the output database. It runs whenever
`BIRDDOG_DATABASE_URL` is unset. The mode switch is described in
[codebase-map.md](codebase-map.md#legacy-vs-postgres-mode). The top-level
`README.md` gives a short overview of the new stack, then documents the legacy app.

## Reading order

1. This file.
2. [codebase-map.md](codebase-map.md): where each part of the code lives.
3. [data-model.md](data-model.md): schemas, tables, views, roles.
4. [pipelines.md](pipelines.md): every DAG, how the app triggers them, how to add one.
5. [local-dev.md](local-dev.md): tests and running the web app.
6. [operations.md](operations.md): server access, deploys, restarts, secrets,
   backups. Read it before touching the server.

Area READMEs (linked from the docs above, not repeated here):

- [deploy/web/README.md](../../deploy/web/README.md): the web app and its DAG triggers
- [deploy/apps/README.md](../../deploy/apps/README.md): ToolJet and Saltcorn
- [deploy/ui/README.md](../../deploy/ui/README.md): Mathesar curator UI
- [deploy/jupyterhub/README.md](../../deploy/jupyterhub/README.md): notebooks
- [deploy/rclone/README.md](../../deploy/rclone/README.md): backup remote setup
- [deploy/elyra/README.md](../../deploy/elyra/README.md): drawing DAGs in Elyra
- [dags/sources/README.md](../../dags/sources/README.md): adding a data source

End-user documentation is in `docs/help.md`, and the HTTP API is in
`docs/birddog_api_reference.md`.
