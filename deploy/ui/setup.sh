#!/bin/bash
# Configures the curator UI (Mathesar). Idempotent; sh/deploy runs it on every
# deploy. Run from deploy/:  bash ui/setup.sh
#
#  - creates/updates the Mathesar admin user from MATHESAR_ADMIN_USER/_PASSWORD
#  - installs Mathesar's helper schemas (msar, __msar, mathesar_types) into the
#    birddog database *as birddog*, when missing or after a Mathesar upgrade,
#    so the UI role itself never needs CREATE on the database
#  - registers the birddog database with the birddog_ui role and gives the
#    admin access through that role (never through birddog)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

echo "waiting for mathesar to become healthy..."
for _ in $(seq 60); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q mathesar)" 2>/dev/null || true)
    [ "$status" = healthy ] && break
    sleep 5
done
[ "$status" = healthy ] || { echo "mathesar not healthy ($status)"; docker compose logs --tail 40 mathesar; exit 1; }

docker compose exec -T \
    -e MATHESAR_ADMIN_USER -e MATHESAR_ADMIN_PASSWORD \
    -e BIRDDOG_DB_PASSWORD -e BIRDDOG_UI_DB_PASSWORD \
    mathesar python manage.py shell <<'PY'
import os
from mathesar import __version__
from mathesar.models.base import Server, Database, ConfiguredRole, UserDatabaseRoleMap
from mathesar.models.users import User

env = os.environ
admin, created = User.objects.get_or_create(
    username=env["MATHESAR_ADMIN_USER"],
    defaults={"is_superuser": True, "is_staff": True},
)
admin.is_superuser = admin.is_staff = True
# set_password() rotates the session hash and logs the admin out everywhere,
# so only touch it when the password in .env actually changed
if not admin.check_password(env["MATHESAR_ADMIN_PASSWORD"]):
    admin.set_password(env["MATHESAR_ADMIN_PASSWORD"])
admin.save()
print(("created" if created else "updated"), "admin user", admin.username)

server, _ = Server.objects.get_or_create(host="postgres", port=5432)
database, _ = Database.objects.get_or_create(
    name="birddog", server=server, defaults={"nickname": "Birddog"}
)
if database.needs_upgrade_attention:
    # install/upgrade helper schemas as the owner role; the password is used
    # for this one connection and not stored in Mathesar
    database.install_sql(username="birddog", password=env["BIRDDOG_DB_PASSWORD"])
    print("installed Mathesar SQL", __version__, "into birddog")

role, _ = ConfiguredRole.objects.update_or_create(
    name="birddog_ui", server=server,
    defaults={"password": env["BIRDDOG_UI_DB_PASSWORD"]},
)
UserDatabaseRoleMap.objects.update_or_create(
    user=admin, database=database, server=server,
    defaults={"configured_role": role},
)
print("birddog database (id %d) connected as birddog_ui" % database.id)
PY

# editable-twin links in the descriptions of the curator views (views.*)
bash ui/views.sh
