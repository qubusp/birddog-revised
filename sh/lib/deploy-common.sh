# shellcheck shell=bash
# (remote commands are built on this side on purpose)
# shellcheck disable=SC2029
# Shared by sh/deploy-data and sh/deploy-apps. Source it; do not run it.
#
# What lives here: argument parsing, ssh with a shared connection, the rsync
# of the repo (with the paths it must never copy or delete), and the calls to
# the two remote helpers (remote-ensure-env.sh, remote-drift.sh), which are
# piped to `bash -s` on the server, so nothing extra is copied there.

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$LIB_DIR/../.." && pwd)"
# remote checkout, relative to the ssh user's home
REMOTE_DIR="${BIRDDOG_REMOTE_DIR:-birddog-revised}"

DRY_RUN=0
HOST=""

say()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# Paths never copied to the server. rsync does not delete excluded paths on
# the receiving side either (no --delete-excluded), so this list also protects
# the server-only files: deploy/.env (secrets), deploy/rclone/*.conf (backup
# credentials), and caches.
RSYNC_EXCLUDES=(
    --exclude=.git
    --exclude=__pycache__
    --exclude='*.pyc'
    --exclude=.pytest_cache
    --exclude=.coverage
    --exclude=htmlcov
    --exclude=.DS_Store
    --exclude=.idea
    --exclude=.claude
    --exclude=.cache
    --exclude='.venv*'
    --exclude='venv*'
    --exclude=graphify-out
    --exclude=/notebooks
    --exclude=/research
    --exclude=/var
    --exclude=/excel
    --exclude=/static/downloads
    --exclude=/deploy/.env
    --exclude='/deploy/.env.*.bak'
    --exclude='/deploy/.env.bak*'
    --exclude='/deploy/rclone/*.conf'
    --exclude='*key.json'
    --exclude=token.json
    --exclude=credentials.json
)
# a sync that would delete more than this many files is probably pointed at
# the wrong place: sync_repo stops before copying anything
RSYNC_MAX_DELETE=100

ssh_setup() {
    SSH_CTL_DIR="$(mktemp -d /tmp/bdssh.XXXXXX)"
    SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=$SSH_CTL_DIR/%C" -o ControlPersist=120
              -o ConnectTimeout=15)
    trap ssh_teardown EXIT
}

ssh_teardown() {
    [ -n "${SSH_CTL_DIR:-}" ] || return 0
    ssh "${SSH_OPTS[@]}" -O exit "$HOST" 2>/dev/null || true
    rm -rf "$SSH_CTL_DIR"
}

# remote CMD...: runs CMD in the remote deploy/ directory
remote() {
    # a terminal on stdin is not for the remote command (keeps ssh from
    # reading keystrokes); piped input (psql) is passed on
    if [ -t 0 ]; then
        ssh "${SSH_OPTS[@]}" "$HOST" "cd $REMOTE_DIR/deploy && $*" </dev/null
    else
        ssh "${SSH_OPTS[@]}" "$HOST" "cd $REMOTE_DIR/deploy && $*"
    fi
}

# remote_helper NAME ARGS...: pipes sh/lib/NAME to bash on the server
remote_helper() {
    local name=$1; shift
    local args="" a
    for a in "$@"; do args+=" $(printf '%q' "$a")"; done
    ssh "${SSH_OPTS[@]}" "$HOST" "cd $REMOTE_DIR/deploy && bash -s --$args" < "$LIB_DIR/$name"
}

check_local_repo() {
    [ -f "$REPO_ROOT/deploy/docker-compose.yml" ] && [ -d "$REPO_ROOT/birddog" ] \
        || die "$REPO_ROOT does not look like the birddog repo"
}

# check_remote_box OVERRIDE: the remote checkout exists and its .env selects
# OVERRIDE (so the data-box script never runs against the apps box, and the
# other way round). A missing COMPOSE_FILE is added by ensure_env.
check_remote_box() {
    local want=$1 got
    remote "test -f docker-compose.yml" \
        || die "$HOST:$REMOTE_DIR/deploy/docker-compose.yml not found (first deploy? see docs/onboarding/operations.md)"
    got=$(remote "sed -n 's/^COMPOSE_FILE=//p' .env 2>/dev/null | tail -1")
    if [ -n "$got" ] && [[ "$got" != *"$want"* ]]; then
        die "$HOST's deploy/.env has COMPOSE_FILE=$got, expected $want; wrong box?"
    fi
}

_rsync() {
    rsync -az --checksum --delete --itemize-changes \
        "${RSYNC_EXCLUDES[@]}" -e "ssh ${SSH_OPTS[*]:-}" "$@" "$REPO_ROOT/" "$DEST/"
}

# sync_repo DEST: DEST is host:dir or a local directory. Always lists the
# changes first (rsync --dry-run) and refuses when that would delete more
# than RSYNC_MAX_DELETE files; copies only when DRY_RUN is not 1. Prints the
# itemized changes ("*deleting" = removed on the server).
sync_repo() {
    local DEST=$1 plan n
    plan=$(_rsync --dry-run)
    n=$(grep -c '^\*deleting' <<< "$plan" || true)
    if [ "$n" -gt "$RSYNC_MAX_DELETE" ]; then
        printf '%s\n' "$plan" | grep '^\*deleting' | head -20 >&2
        die "rsync would delete $n files on $DEST (limit $RSYNC_MAX_DELETE); wrong directory?"
    fi
    # attribute-only lines (".f..t", ".d") are not shown
    printf '%s\n' "$plan" | grep -v '^\.' || true
    [ "$DRY_RUN" = 1 ] || _rsync --max-delete="$RSYNC_MAX_DELETE" >/dev/null
}

# ensure_env SPEC: adds missing keys to the server's deploy/.env (never
# changes or prints existing values); with --dry-run only reports them
ensure_env() {
    remote_helper remote-ensure-env.sh "$DRY_RUN" "$1"
}

# drift SERVICES...: prints which running containers no longer match the
# compose config or their image (what a plain `up -d` would recreate)
drift() {
    remote_helper remote-drift.sh "$@"
}

# caddyfile_stale FILE: true when the running Caddy still has an older copy of
# deploy/caddy/FILE. A single-file bind mount keeps the inode it was started
# with, and rsync replaces files by rename, so after a change Caddy needs a
# restart (a `caddy reload` would re-read the old copy).
caddyfile_stale() {
    remote "cid=\$(docker compose ps -q caddy); [ -n \"\$cid\" ] || exit 1;
        a=\$(docker compose exec -T caddy cat /etc/caddy/Caddyfile | sha256sum);
        b=\$(sha256sum < caddy/$1); [ \"\$a\" != \"\$b\" ]"
}

usage_common() {
    cat <<EOF
Common options:
  --dry-run   change nothing: show what rsync would copy or delete, which
              .env keys are missing, and what is out of date on the server
  -h, --help  this text
Environment: BIRDDOG_REMOTE_DIR (default birddog-revised, relative to the
ssh user's home)
EOF
}
