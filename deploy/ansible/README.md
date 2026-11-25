# Deploying with Ansible (and GitHub Actions)

Two playbooks deploy Birddog to the two boxes ([../two-box.md](../two-box.md)).
They are a port of `sh/deploy-data` and `sh/deploy-apps`: the same steps in the
same order, with the same safety rules. The sh scripts still work and are
the fallback.

| File | What |
|---|---|
| `deploy-data.yml` | Data box (Postgres, Airflow): `.env` keys, migrations, rsync, drift report, optional Airflow/Postgres restart, Caddy |
| `deploy-apps.yml` | Apps box: `.env` keys, rsync, build, `docker compose up -d`, Caddy |
| `group_vars/` | `.env` key specs, rsync excludes, service lists. `test/test_deploy_ansible.py` fails when they differ from the sh scripts |
| `inventory.yml` | `data_box` (group `databox`) and `apps_box` (group `appsbox`), from `DATA_BOX_HOST` / `APPS_BOX_HOST` (`user@host`) |
| `tasks/` | The steps, one file each |
| `../../.github/workflows/deploy.yml` | The GitHub workflow that runs them |
| `../../.github/workflows/deploy-check.yml` | A dry run on every push to `day0-stack` |

## Deploy from GitHub

1. GitHub, repo `qubusp/birddog-revised`, tab **Actions**, workflow **Deploy**,
   button **Run workflow**.
2. Branch: `day0-stack` (the live branch). The run deploys exactly the commit
   at the head of that branch when you click.
3. Leave **Dry run** ticked the first time and click **Run workflow**. Open the
   run: its summary shows which `.env` keys would be added, which migrations
   would run, the rsync change list, and which containers drifted.
4. If that looks right, run it again with **Dry run** unticked.

Order: `test`, then `deploy-data`, then `deploy-apps`. The tests must pass
before anything touches a server. With target `both`, the apps box waits for
the data box and is skipped if the data box failed (migrations go first, so
the new web app never meets an old schema). Only one deploy runs at a time
(concurrency group `deploy-production`); a second one waits.

| Input | Default | Effect |
|---|---|---|
| `target` | `both` | `data`, `apps` or `both` |
| `dry_run` | ticked | Check mode (`--check --diff`): reads the servers, changes nothing |
| `restart_airflow` | off | Data box: rebuild the Airflow image, run `airflow-init`, `up -d --no-deps` the `airflow-*` services. **Owner's OK** (needed after a change to `deploy/airflow/Dockerfile` or `requirements.txt`, or to `.env`) |
| `restart_postgres` | off | Data box: `up -d --no-deps postgres` (recreates it if its config or image changed). **Owner's OK** |
| `init_roles` | off | Data box: re-run `db/init/02-ui.sh`, `03-reader.sh`, `05-web.sh`, `06-marmot-reader.sh` as `postgres` (creates missing roles, re-syncs their passwords from `.env`). New box, or a changed role password |
| `all_migrations` | off | Re-apply every migration, not only new and changed ones (they are idempotent) |
| `baseline_migrations` | off | Record the pending migrations as applied **without running them** (a database known to be up to date) |

### Approvals

Two GitHub environments:

- `production`: no approval. Plain deploys and every dry run.
- `production-restart`: the owner must approve. The data-box job uses it when
  the run is **not** a dry run and `restart_airflow`, `restart_postgres` or
  `init_roles` is on. The run then waits at `deploy-data` with "Review
  deployments"; the owner opens the run, clicks **Review deployments**, ticks
  `production-restart` and **Approve and deploy**. Nothing has touched the
  servers before that point (the test job doesn't need the environment).

The apps-box job always uses `production`: everything on that box may be
recreated.

### Dry run on every push

`deploy-check.yml` runs both playbooks in check mode on every push to
`day0-stack` (environment `production`, read only) and puts the result in the
run summary, so each push shows what a deploy would do. It never deploys and
never waits for (or cancels) a real deploy.

## Run it from a laptop

```bash
python3 -m pip install 'ansible-core>=2.15'          # CI pins 2.21.3
ansible-galaxy collection install -r deploy/ansible/requirements.yml
cd deploy/ansible
ansible-playbook deploy-data.yml --check --diff      # dry run, data box
ansible-playbook deploy-data.yml
ansible-playbook deploy-apps.yml --check --diff      # dry run, apps box
ansible-playbook deploy-apps.yml
ansible-playbook deploy-data.yml -e restart_airflow=true   # owner's OK first
DATA_BOX_HOST=user@otherhost ansible-playbook deploy-data.yml --check --diff
```

It uses your own ssh access (`administrator@` on both boxes) and refuses a host
key it doesn't know (`StrictHostKeyChecking=yes`). Run it from `deploy/ansible`
so `ansible.cfg` is read.

Unlike the sh scripts, a real run refuses a checkout with uncommitted or
untracked files, or whose commit is on no remote branch: rsync `--delete`
ships the working tree, so unpushed work would be rolled back by the next
person's deploy. A dry run only warns. Use the sh scripts if you really must
ship a working tree.

Environment: `DATA_BOX_HOST`, `APPS_BOX_HOST`, `BIRDDOG_REMOTE_DIR` (default
`birddog-revised`, relative to the ssh user's home), `BIRDDOG_LOCK_TIMEOUT`
(default `5s`). Extra vars: the inputs above, plus `summary_file=PATH` to append
a markdown report.

## What the playbooks do

`deploy-data.yml`:

1. **Box check**: the remote checkout exists and `deploy/.env`'s
   `COMPOSE_FILE` selects `docker-compose.databox.yml`; it refuses the apps box.
2. **`.env` keys**: adds the keys in `env_spec` that are missing. Values are
   generated on the server (`hexN`: `openssl rand -hex N`; `b64_N`:
   `openssl rand -base64 N`; `fernet`; `literal:V`; `blank`) and appended
   without being printed; only key names leave the server. Existing values are
   never changed or read. A backup `.env.bak.<UTC time>` (mode 600) is made
   before anything is added, and `.env` is set to mode 600.
3. **Role scripts** (`init_roles` only), streamed from the checkout into
   `docker compose exec -T [-e VAR] postgres bash -s`, the password taken from
   `.env` on the server. Run before the migrations, as in `sh/deploy-data`
   (migrations grant to these roles).
4. **Migrations, before any code is synced**: every `deploy/db/migrations/*.sql`
   that is new, or whose sha256 differs from the one recorded in
   `public.schema_migrations`, is streamed from the checkout into
   `psql -U birddog -d birddog -v ON_ERROR_STOP=1`, one file per psql session,
   in autocommit, after `SET lock_timeout = '5s'`. The record (file name,
   sha256) is written in the same session after the file, so a failed file is
   not recorded; nothing after it runs and the code is not synced. Same table
   and columns as `sh/deploy-data`, so the two tools agree on what is applied.
   Skipped when postgres is not running.
5. **rsync** of the repo with `--delete --checksum` and the excludes in
   `group_vars/all.yml` (identical to `RSYNC_EXCLUDES`): never copies or deletes
   `LOGINS.md`, `deploy/.env*`, `deploy/rclone/*.conf`, `.git`, caches,
   virtualenvs, `graphify-out`, `notebooks/`, `research/`, `archive/` and the
   rest of the list. A dry run comes first; more than 100 deletions stops the
   deploy before anything is copied.
6. **Drift** of `postgres` and `airflow-*` (sh/lib/remote-drift.sh, read only).
   They are recreated only with `restart_postgres` / `restart_airflow`.
7. **Caddy**: `up -d --no-deps caddy` on drift; otherwise `restart caddy` if
   the running Caddyfile differs from `caddy/Caddyfile.databox` in the checkout
   (a single-file bind mount keeps the old file until a restart).

`deploy-apps.yml`: steps 1, 2 (its `env_spec` has `require` keys, which must
already be set because they have to equal the data box's: if one is missing it
stops and writes nothing), 5, the drift report, then
`docker compose build birddog-web jupyterhub`,
`docker compose --profile build-only build jupyter-singleuser` (built, never
started), `docker compose up -d`, and `restart caddy` if
`caddy/Caddyfile.appsbox` changed.

Check mode runs every read-only probe for real (key names, recorded
migrations, rsync `--dry-run`, drift, Caddyfile checksum) and skips every
command that changes something, reporting what it would do instead.

## Manual, one-time state the playbooks never touch

Set up by hand; a deploy neither creates, changes nor removes it:

- Marmot's ingestion schedules (postgres, and Airflow via
  `http://birddog-web:8000/airflow-v1`).
- Marmot's admin user, and its service account `airflow` (role `lineage-writer`).
- The Airflow users `marmot` (read only, role Viewer) and the `BIRDDOG_SYNC`
  user on the data box.
- The Airflow connection `anthropic`.
- The ToolJet admin.
- Mathesar's admin and connection (`ui/setup.sh`; not run by any deploy).
- `LOGINS.md` (copied to both boxes by hand, mode 600) and
  `deploy/rclone/rclone.conf`.
- DAG paused/unpaused state (Airflow's DB).

Grafana has no deploy-time steps: provisioning is bind-mounted from
`deploy/grafana`, and the dashboards are generated by
`deploy/grafana/make_dashboards.py` on a laptop and committed.

## Rotating the deploy key

The workflow logs in with the repo secret `DEPLOY_SSH_KEY` (an ed25519 key
authorized for `administrator@` on both boxes) and trusts only the host keys in
`DEPLOY_KNOWN_HOSTS`.

```bash
ssh-keygen -t ed25519 -N '' -C birddog-deploy-$(date +%Y%m) -f /tmp/birddog-deploy
for h in administrator@204.12.218.112 administrator@108.181.191.55; do
  ssh "$h" 'cat >> ~/.ssh/authorized_keys' < /tmp/birddog-deploy.pub
done
gh secret set DEPLOY_SSH_KEY -R qubusp/birddog-revised < /tmp/birddog-deploy
rm /tmp/birddog-deploy /tmp/birddog-deploy.pub
```

Run the Deploy workflow with **Dry run** to check the new key, then delete the
old key's line (by its comment) from `~/.ssh/authorized_keys` on both boxes.

If a box's host key changes (reinstall), refresh the known hosts, comparing
the fingerprints with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on
the box itself:

```bash
ssh-keyscan 204.12.218.112 108.181.191.55 2>/dev/null | gh secret set DEPLOY_KNOWN_HOSTS -R qubusp/birddog-revised
```
