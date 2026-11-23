# shellcheck shell=bash
# Runs on the server, in deploy/, piped to `bash -s -- DRY_RUN SPEC` by
# sh/lib/deploy-common.sh. Adds keys missing from deploy/.env; never changes or
# prints an existing value, and new random values are made here, on the server.
#
# SPEC has one "KEY KIND" per line. KIND:
#   hexN        random hex, N bytes          fernet     Airflow Fernet key
#   literal:V   the fixed value V            blank      KEY= (empty)
#   require     must already be set, non-empty; never generated (secrets that
#               must match the other box)
set -euo pipefail
dry=$1
spec=$2
fail=0

if [ ! -f .env ]; then
    if [ "$dry" = 1 ]; then
        echo "    would create deploy/.env"
    else
        (umask 077; : > .env)
        echo "    created deploy/.env"
    fi
fi
if [ -f .env ] && [ "$dry" != 1 ]; then chmod 600 .env; fi

has()      { [ -f .env ] && grep -q "^$1=" .env; }
nonempty() { [ -f .env ] && grep -q "^$1=." .env; }
gen() {
    case $1 in
        hex*)      openssl rand -hex "${1#hex}" ;;
        fernet)    openssl rand -base64 32 | tr '+/' '-_' ;;
        literal:*) printf '%s\n' "${1#literal:}" ;;
        blank)     echo ;;
        *)         echo "unknown kind $1" >&2; return 1 ;;
    esac
}

# keys that must come from the other box first: when one is missing, write
# nothing (unless dry run, which reports everything)
while read -r key kind; do
    [ "$kind" = require ] || continue
    nonempty "$key" || { echo "    MISSING $key: copy it from the other box's deploy/.env (the values must match)"; fail=1; }
done <<< "$spec"
if [ "$fail" = 1 ] && [ "$dry" != 1 ]; then exit 1; fi

added=0
while read -r key kind; do
    [ -n "$key" ] && [ "$kind" != require ] || continue
    has "$key" && continue
    if [ "$dry" = 1 ]; then
        echo "    would add $key"
    else
        printf '%s=%s\n' "$key" "$(gen "$kind")" >> .env
        echo "    added $key"
    fi
    added=1
done <<< "$spec"

[ "$added" = 1 ] || [ "$fail" = 1 ] || echo "    deploy/.env has every key"
exit "$fail"
