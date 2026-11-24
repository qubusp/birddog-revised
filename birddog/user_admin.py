"""Account administration for when web signup is closed (BIRDDOG_SIGNUP=closed).

    python -m birddog.user_admin add EMAIL "NAME" [--admin]
    python -m birddog.user_admin password EMAIL
    python -m birddog.user_admin role EMAIL admin|user
    python -m birddog.user_admin remove EMAIL
    python -m birddog.user_admin sync [EMAIL]
    python -m birddog.user_admin list

`add` and `password` print a generated password once; hand it to the user,
who can change it in the app. Each command that changes an account also
copies it into the other tools (birddog/user_sync.py) and prints the result per
tool. `sync` re-sends name, role and active state without a password; it
cannot create a missing account, which happens at the person's next login.
On the server, run inside the web container:
    docker compose exec birddog-web python -m birddog.user_admin add ...
"""

import argparse
import secrets
import sys

from birddog import user_sync
from birddog.user import User, load_user_record


def _new_password():
    return secrets.token_urlsafe(12)


def add(email, name, admin=False):
    email = email.strip().lower()
    if load_user_record(email) is not None:
        sys.exit(f"{email} already has an account")
    password = _new_password()
    User(name, email, password, role="admin" if admin else "user").save()
    print(f"created {email} ({'admin' if admin else 'user'}), password: {password}")
    _report(user_sync.sync_account(email, password))


def reset_password(email):
    email = email.strip().lower()
    record = load_user_record(email)
    if record is None:
        sys.exit(f"{email} has no account")
    password = _new_password()
    User(record["name"], email, password, role=record.get("role") or "user").save()
    print(f"new password for {email}: {password}")
    _report(user_sync.sync_account(email, password))


def set_role(email, role):
    email = email.strip().lower()
    record = load_user_record(email)
    if record is None:
        sys.exit(f"{email} has no account")
    User(record["name"], email, record["password"], is_hashed=True, role=role).save()
    print(f"{email} is now {role}")
    _report(user_sync.sync_account(email))


def remove(email):
    from birddog import app_db
    email = email.strip().lower()
    if load_user_record(email) is None:
        sys.exit(f"{email} has no account")
    _report(user_sync.remove_account(email))
    with app_db.cursor() as cur:   # watches, preferences and sync rows cascade
        cur.execute("DELETE FROM app.user_account WHERE email = %s", (email,))
    print(f"removed {email}")


def sync(email=None):
    from birddog import app_db
    if email:
        emails = [email.strip().lower()]
    else:
        with app_db.cursor() as cur:
            cur.execute("SELECT email FROM app.user_account ORDER BY email")
            emails = [row[0] for row in cur.fetchall()]
    for each in emails:
        print(each)
        _report(user_sync.sync_account(each))


def _report(results):
    if not results:
        print("  other tools: none configured (BIRDDOG_SYNC_* settings), nothing copied")
    for tool, (state, detail) in sorted(results.items()):
        print(f"  {tool:9} {state:5} {detail or ''}")


def list_users():
    from birddog import app_db
    with app_db.cursor() as cur:
        cur.execute("SELECT email, name, role, created_at FROM app.user_account ORDER BY created_at")
        for email, name, role, created in cur.fetchall():
            print(f"{created:%Y-%m-%d}  {role:5}  {email}  {name}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m birddog.user_admin")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("add", help="create an account")
    p.add_argument("email")
    p.add_argument("name")
    p.add_argument("--admin", action="store_true")
    p = sub.add_parser("password", help="set a new generated password")
    p.add_argument("email")
    p = sub.add_parser("role", help="make an account admin or user")
    p.add_argument("email")
    p.add_argument("role", choices=["admin", "user"])
    p = sub.add_parser("remove", help="delete an account here and in the other tools")
    p.add_argument("email")
    p = sub.add_parser("sync", help="re-send accounts to the other tools (no password)")
    p.add_argument("email", nargs="?")
    sub.add_parser("list", help="list accounts (Postgres store)")
    args = parser.parse_args(argv)
    if args.command == "add":
        add(args.email, args.name, args.admin)
    elif args.command == "password":
        reset_password(args.email)
    elif args.command == "role":
        set_role(args.email, args.role)
    elif args.command == "remove":
        remove(args.email)
    elif args.command == "sync":
        sync(args.email)
    else:
        list_users()


if __name__ == "__main__":
    main()
