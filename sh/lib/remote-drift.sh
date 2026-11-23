# shellcheck shell=bash
# Runs on the server, in deploy/, piped to `bash -s -- SERVICE...` by
# sh/lib/deploy-common.sh. Read only. For each service, compares the running
# container with what `docker compose up` would create:
#   - config: the container's com.docker.compose.config-hash label against
#     `docker compose config --hash` (compose recreates on a mismatch; any
#     deploy/.env change counts, since the services read it)
#   - image: the image the container runs against the image its tag points
#     to now (changed by a build or pull)
# Prints "ok", "not running" or "DRIFT <service>: <reasons>" per service.
set -euo pipefail

for svc in "$@"; do
    cid=$(docker compose ps -q "$svc" 2>/dev/null || true)
    if [ -z "$cid" ]; then
        echo "    $svc: not running"
        continue
    fi
    reasons=()
    want=$(docker compose config --hash "$svc" 2>/dev/null | awk '{print $2}')
    have=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.config-hash"}}' "$cid")
    [ -n "$want" ] && [ "$want" != "$have" ] && reasons+=("config changed since it was created")
    ref=$(docker inspect -f '{{.Config.Image}}' "$cid")
    running=$(docker inspect -f '{{.Image}}' "$cid")
    current=$(docker image inspect -f '{{.Id}}' "$ref" 2>/dev/null || true)
    [ -n "$current" ] && [ "$current" != "$running" ] && reasons+=("image $ref was rebuilt or pulled")
    if [ ${#reasons[@]} -eq 0 ]; then
        echo "    $svc: ok"
    else
        (IFS=';'; echo "    DRIFT $svc: ${reasons[*]}")
    fi
done
