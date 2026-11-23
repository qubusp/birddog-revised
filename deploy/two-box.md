# Two-box deployment

| Box | Address | Runs | Public sites |
|---|---|---|---|
| Data box | 204.12.218.112 | Postgres, Airflow (api-server, scheduler, dag-processor), backups | `airflow.204-12-218-112.sslip.io` |
| Apps box | 108.181.191.55 | birddog-web, Mathesar, JupyterHub, ToolJet, Saltcorn | `birddog.`, `data.`, `hub.`, `apps.`, `saltcorn.` on `108-181-191-55.sslip.io` |

Both boxes use the same `docker-compose.yml`. Each box's `deploy/.env` sets
`COMPOSE_FILE` to add its own override, so a plain `docker compose up -d` only
starts that box's services:

- data box: `COMPOSE_FILE=docker-compose.yml:docker-compose.databox.yml`
  (apps behind profile `apps`, Caddy serves `caddy/Caddyfile.databox`)
- apps box: `COMPOSE_FILE=docker-compose.yml:docker-compose.appsbox.yml`
  (Postgres and Airflow behind profile `databox`, Caddy serves `caddy/Caddyfile.appsbox`)

The apps' own metadata databases (mathesar, tooljet, tooljet_db,
tooljet_sample, saltcorn) stay in the data box's Postgres.

## Private link (WireGuard)

| | Data box | Apps box |
|---|---|---|
| wg0 address | 10.77.0.1 | 10.77.0.2 |
| config | `/etc/wireguard/wg0.conf` | `/etc/wireguard/wg0.conf` |
| private key | `/etc/wireguard/wg0.key` (never leaves the box) | same |

UDP 51820 on both. The data box forwards `10.77.0.1:5432` (Postgres) and
`10.77.0.1:8080` (Airflow API) to the compose ports on 127.0.0.1 with the
systemd units `wg-forward@5432` and `wg-forward@8080`
(`/etc/systemd/system/wg-forward@.service`, socat). Nothing else listens on
10.77.0.1, and Postgres stays unreachable from the internet.

On the apps box, `extra_hosts` maps the compose names `postgres` and
`airflow-apiserver` to 10.77.0.1, so app settings are unchanged. JupyterHub
passes the same mapping to notebook containers (`JUPYTER_EXTRA_HOSTS`).

Round trip between the boxes is about 23 ms; every query from an app pays it.

## Checks

```
sudo wg show wg0                          # latest handshake under 2 minutes
systemctl status wg-forward@5432 wg-forward@8080   # data box
timeout 3 bash -c '</dev/tcp/10.77.0.1/5432' && echo ok   # apps box
```

## Deploying

Sync the repo to each box (rsync, excluding `deploy/.env`, `.git`, caches;
see docs/onboarding/operations.md). Apply migrations on the data box with psql
before syncing code that needs them. On the apps box:
`docker compose build birddog-web jupyterhub && docker compose up -d`.
