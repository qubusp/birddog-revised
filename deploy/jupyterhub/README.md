# JupyterHub: shared notebooks next to the data

**URL:** https://hub.204-12-218-112.sslip.io (`hub.<PUBLIC_DOMAIN>`, served by Caddy)

JupyterHub runs as the `jupyterhub` compose service. Each user gets their own
JupyterLab container, started on demand, on the same Docker network as
Postgres, with read-only access to the `birddog` database.

## Logging in

- **Admin:** username `JUPYTERHUB_ADMIN_USER` (default `admin`), password
  `JUPYTERHUB_ADMIN_PASSWORD`, both in the server's `deploy/.env`:
  `ssh administrator@204.12.218.112 grep JUPYTERHUB_ deploy/.env` (from
  `~/birddog-revised`). The admin is not a sign-up: the password always comes
  from `.env` (change it there, then `docker compose up -d --no-deps jupyterhub`).
  Nobody can claim the admin name by signing up.
- **Birddog users:** sign in with their Birddog email and password. The hub
  checks them against birddog-web's `/login` (`BIRDDOG_LOGIN_URL`), and Birddog
  admins are hub admins. Nothing to approve; removing someone from Birddog
  locks them out here too. If birddog-web is down, these logins fail and the
  native accounts below still work.
- **Everyone else:** sign up at `/hub/signup` (username + password, min. 10
  characters). New accounts can't log in until the admin approves them at
  `/hub/authorize` (also linked as "Authorize Users" from the admin's home
  page). The admin can discard/unauthorize accounts there too, and manage
  running servers at `/hub/admin`. 5 failed logins lock an account for 5 min.
- Users change their own password at `/hub/change-password`.

## What a user gets

- JupyterLab from `singleuser.Dockerfile` (`quay.io/jupyter/scipy-notebook`
  + DuckDB with its postgres extension, psycopg 3, SQLAlchemy, Polars,
  PyArrow, jupysql, and the birddog library's dependencies).
- **Limits per user:** 2 GB RAM (hard; the kernel is killed above it), 1 CPU.
- **Idle culling:** servers idle for 1 hour are stopped (jupyterhub-idle-culler).
  Files survive; only running kernels are lost.
- **Storage:** `~/work` is a per-user Docker volume `jupyterhub-user-<name>`
  that persists across restarts. Everything outside `~/work` is discarded when
  the server stops.
- **Examples:** `~/work/examples/birddog_quickstart.ipynb` is copied in on
  each start if missing (an edited copy is never overwritten; delete it to get
  the current version). Shows psycopg, DuckDB `ATTACH ... (TYPE postgres,
  READ_ONLY)`, `%sql`, and importing `birddog`.
- **birddog library:** the repo's `birddog/` (and `resources/`) are mounted
  read-only at `~/birddog-lib`, which is on `PYTHONPATH`. It reflects whatever
  was last deployed to the server.

## Database access

Notebooks connect as Postgres role `birddog_reader` via
`BIRDDOG_DATABASE_URL` (also `DATABASE_URL`, SQLAlchemy form):

- `SELECT` on everything in schemas `core` and `raw`, including tables/views
  `birddog` creates later (default privileges).
- No access to schema `app` (user accounts and other user-facing state).
- Sessions are `default_transaction_read_only`; idle-in-transaction sessions
  are killed after 10 min (so a forgotten open transaction can't block
  imports/migrations); max 30 connections.

Role: `deploy/db/init/03-reader.sh` (run as postgres by `sh/deploy-data --init-roles`, needs
`JUPYTER_DB_PASSWORD`). Grants: `deploy/db/migrations/007_reader_role.sql`.

## Operating it

```
cd ~/birddog-revised/deploy
docker compose --profile build-only build jupyter-singleuser   # user image
docker compose up -d --build --no-deps jupyterhub              # hub
docker compose logs -f jupyterhub
docker ps --filter name=jupyter-                                # user servers
```

Users' servers keep running across a hub restart. A new user image is picked
up the next time each user's server starts (stop it from `/hub/admin`).
`sh/deploy-apps` builds both images and runs `docker compose up -d` on the apps box.

## Security notes

- **The hub has the host's Docker socket** (`/var/run/docker.sock`) so
  DockerSpawner can start containers. Anything that compromises the hub
  process gets root-equivalent control of the server. The hub runs only
  JupyterHub's own code, but keep the image updated and the admin password
  strong.
- User containers do **not** get the socket. They run as `jovyan` (non-root),
  but they share the `deploy_default` network with every stack service, so
  users can reach Postgres (only as `birddog_reader`, unless they know other
  credentials), the Airflow API and Mathesar (both behind their own logins).
- Every approved user can read `BIRDDOG_DATABASE_URL` (including the reader
  password) and the code in `birddog/`. Approve only people who may see all
  of `core` and `raw`.
- Users have unrestricted outbound internet access from their containers.
- Hub state (users, sign-ups, cookie secret) is in the `jupyterhub-data`
  volume (SQLite).
