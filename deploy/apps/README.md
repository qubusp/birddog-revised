# Low-code app builders: ToolJet and Saltcorn

Two app builders run side by side for comparison. Each one has its own login,
sits behind Caddy with a Let's Encrypt certificate, and stores its own metadata
in a separate database on our Postgres. Neither can reach Birddog data until you
add a datasource as described below.

| Tool | URL | Image | Metadata DB / role |
|---|---|---|---|
| ToolJet CE | https://apps.204-12-218-112.sslip.io | `tooljet/tooljet-ce:v3.20.256-lts` | `tooljet` (+ `tooljet_db`, `tooljet_sample`) / `tooljet` |
| Saltcorn | https://saltcorn.204-12-218-112.sslip.io | `saltcorn/saltcorn:1.6.4` | `saltcorn` / `saltcorn` |

## First person to sign up becomes admin: do it now

Neither tool has an admin account until someone creates one, and **the first
person to open the page and fill in the form becomes the instance admin**.
Both URLs are public, so create your admin account in each tool right away:

- ToolJet: open https://apps.204-12-218-112.sslip.io. On an empty instance it
  shows the "set up your admin account" (super admin) form, then asks for a
  workspace name.
- Saltcorn: open https://saltcorn.204-12-218-112.sslip.io. It redirects to
  `/auth/create_first_user`.

## Connecting to Birddog data

Use the curator role `birddog_ui`. It can read and write the curated core
tables and has no access to the app tables.

| Setting | Value |
|---|---|
| Host | `postgres` (the compose service name, which both containers can reach) |
| Port | `5432` |
| Database | `birddog` |
| User | `birddog_ui` |
| Password | `BIRDDOG_UI_DB_PASSWORD` in the server's `~/birddog-revised/deploy/.env` (`grep BIRDDOG_UI_DB_PASSWORD ~/birddog-revised/deploy/.env`) |
| SSL | off (traffic stays on the internal Docker network) |

**ToolJet:** go to Data sources, add PostgreSQL, and enter the values above.
ToolJet encrypts the stored password with `TOOLJET_LOCKBOX_MASTER_KEY`. Build
your apps on this datasource. The built-in "ToolJet Database" feature (tables
hosted by ToolJet itself) is **not available** here, because it needs a
PostgREST sidecar that we don't run.

**Saltcorn:** Saltcorn's own tables live in the `saltcorn` database. To work on
Birddog tables, install the **postgres-tables** plugin
([saltcorn/postgres-tables](https://github.com/saltcorn/postgres-tables)) from
Settings > Plugins. It provides two ways in:
- To expose one table, create a table and choose the "PostgreSQL remote table"
  provider, then enter the connection values above plus the schema and table
  name.
- To import several related tables at once, use the admin-only "PostgreSQL
  Database Explorer" view: enter the connection values without a table name,
  then "Look up tables" and "Import tables". Foreign keys become relations.

The container already has `SC_EXTPG_PASS_birddog` set to the `birddog_ui`
password, so you can leave the password field empty and Saltcorn never stores
it. We haven't tested whether writes go through, so check that before relying
on it.

## ToolJet: the Curator app

`tooljet/curator.json` is a ready-made ToolJet app (an export file for
ToolJet CE 3.20.256-lts). It has a search box over `core.search()`, a page
panel (page fields, parent, linked documents) and a document form for the
curation fields of `core.document` (description, process code, doc type,
save status, doc structure, doc scope, language, processor, pages processed,
when transcribed, comments, anomaly, anomaly comment). All queries use bind
parameters. To change the app, edit `tooljet/build_curator.py` and run it to
regenerate the JSON.

Import it (ToolJet has no API tokens in CE, so this is done in the browser):

1. Open https://apps.204-12-218-112.sslip.io and log in as admin.
2. On the Applications page, click the **⋮** button to the right of
   **Create an app**, then **Import from device**, and pick `curator.json`.
3. Keep the app name `curator` (or change it) and click **Import app**. The
   editor opens.
4. The import also creates a PostgreSQL datasource named **birddog** with host,
   port, database, user and SSL already filled in. ToolJet never imports
   passwords, so set it: open **Data sources** (left sidebar, database icon),
   then **birddog**. Next to **Password**, click **Edit**, paste
   `BIRDDOG_UI_DB_PASSWORD` from the server's `deploy/.env`, then click **Save**.
   You can check it with **Test connection**. In CE, saving applies to all three
   environments.
5. Back in the app, type a query (for example `ДАЗО 1-1-12`) and press Enter.
   Use **Preview**, or **Release** the version to give curators the
   viewer-only URL.

If a PostgreSQL datasource named `birddog` already exists, the import reuses
it as it is, and you can skip step 4. ToolJet 3.20 can't read server
environment variables in datasource settings (`{{globals.server.*}}` is not
resolved in CE), so the password can't come from `docker-compose.yml`. An
alternative we haven't tested: store it as a workspace secret (Workspace
settings > Workspace constants) and enter `{{secrets.<name>}}` as the password.

Saving a document overwrites all of the form's fields at once. Empty text
becomes NULL. Doc type is split on commas into a `text[]`. If someone else
edits the same document between your load and your save, their changes to
these fields are lost.

## Memory

No swap on the host, so each service has a hard `mem_limit`:

| Service | Limit | Observed after first start |
|---|---|---|
| tooljet | 1024m (Node heap capped at 768 MB via `NODE_OPTIONS`) | ~430 MiB idle, ~755 MiB just after a restart |
| saltcorn | 512m (1 web worker, `SALTCORN_NWORKERS=1`) | ~150 MiB |

If one of them is OOM-killed (`docker inspect -f '{{.State.OOMKilled}}' deploy-tooljet-1`),
raise its limit in `deploy/docker-compose.yml` and start it again with
`docker compose up -d --no-deps tooljet` (or `saltcorn`).

## Closing signup after the admin exists

- **ToolJet:** the first-admin form stops working once one user exists. Public
  sign-up for further users is an instance setting: as admin, go to Settings >
  Instance settings and turn off "Enable signup", so people join by invitation
  only. To enforce this at the environment level instead, add
  `DISABLE_SIGNUPS: "true"` to the `tooljet` service and run
  `docker compose up -d --no-deps tooljet`.
- **Saltcorn:** `/auth/create_first_user` only works while there are no users.
  Self-signup for further users is controlled by Settings > Users and security
  > "Allow signups" (`allow_signup`), which is **off** by default. Make sure it
  stays off, or set it explicitly.

## Operations

- Start or update only these services:
  `docker compose up -d --no-deps tooljet saltcorn`
- Databases and roles come from `deploy/db/init/04-apps.sh`, which is
  idempotent. `sh/deploy` runs it after `up`. To run it by hand:
  ```
  TOOLJET_DB_PASSWORD=... SALTCORN_DB_PASSWORD=... \
    docker compose exec -T -e TOOLJET_DB_PASSWORD -e SALTCORN_DB_PASSWORD postgres \
    bash /docker-entrypoint-initdb.d/04-apps.sh </dev/null
  ```
  On a fresh deploy the apps may restart a few times until this script has
  created their databases. That's expected, because they use
  `restart: unless-stopped`.
- Secrets live in the server's `deploy/.env`: `TOOLJET_DB_PASSWORD`,
  `TOOLJET_LOCKBOX_MASTER_KEY`, `TOOLJET_SECRET_KEY_BASE`,
  `SALTCORN_DB_PASSWORD`, `SALTCORN_SESSION_SECRET`. Do not rotate
  `TOOLJET_LOCKBOX_MASTER_KEY`, because saved datasource credentials are
  encrypted with it.
- Saltcorn keeps uploaded files and installed plugins in the `saltcorn-data`
  volume. ToolJet keeps everything in Postgres.
- ToolJet telemetry is off (`DISABLE_TOOLJET_TELEMETRY=true`, `CHECK_FOR_UPDATES=false`).
- Both apps are also reachable over an SSH tunnel on 127.0.0.1:8082 (ToolJet)
  and 127.0.0.1:8083 (Saltcorn).
