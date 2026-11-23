"""Account administration for when web signup is closed (BIRDDOG_SIGNUP=closed).

    python -m birddog.user_admin add EMAIL "NAME" [--admin]
    python -m birddog.user_admin password EMAIL
    python -m birddog.user_admin list

`add` and `password` print a generated password once; hand it to the user,
who can change it in the app. On the server, run inside the web container:
    docker compose exec birddog-web python -m birddog.user_admin add ...
"""

import argparse
import secrets
import sys

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


def reset_password(email):
    email = email.strip().lower()
    record = load_user_record(email)
    if record is None:
        sys.exit(f"{email} has no account")
    password = _new_password()
    User(record["name"], email, password, role=record.get("role") or "user").save()
    print(f"new password for {email}: {password}")


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
    sub.add_parser("list", help="list accounts (Postgres store)")
    args = parser.parse_args(argv)
    if args.command == "add":
        add(args.email, args.name, args.admin)
    elif args.command == "password":
        reset_password(args.email)
    else:
        list_users()


if __name__ == "__main__":
    main()
