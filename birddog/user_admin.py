"""Account administration for when web signup is closed (BIRDDOG_SIGNUP=closed).

    python -m birddog.user_admin add EMAIL "NAME" [--admin]
    python -m birddog.user_admin password EMAIL
    python -m birddog.user_admin role EMAIL admin|user
    python -m birddog.user_admin remove EMAIL
    python -m birddog.user_admin sync [EMAIL]
    python -m birddog.user_admin list

`add` and `password` print a generated password once; hand it to the user,
who can change it in the app. Admins can do the same on the web app's Users
page (/users), which calls the functions below. Each command that changes an account also
copies it into the other tools (birddog/user_sync.py) and prints the result per
tool. `sync` re-sends name, role and active state without a password; it
cannot create a missing account, which happens at the person's next login.
On the server, run inside the web container:
    docker compose exec birddog-web python -m birddog.user_admin add ...
"""

import argparse
import re
import secrets
import sys

from birddog import user_sync
from birddog.user import User, load_user_record

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class AccountError(ValueError):
    """A request the account functions refuse (shown to the admin as is)."""


# --- account functions: shared by this CLI and the web app's Users page -------

def _new_password():
    return secrets.token_urlsafe(12)


def _existing(email):
    email = (email or "").strip().lower()
    record = load_user_record(email)
    if record is None:
        raise AccountError(f"{email} has no account")
    return email, record


def create_account(email, name, admin=False):
    """Make an account with a generated password; returns (password, sync results)."""
    email = (email or "").strip().lower()
    name = (name or "").strip()
    if not _EMAIL.fullmatch(email):
        raise AccountError("Enter a valid email address")
    if not name:
        raise AccountError("Enter the person's name")
    if load_user_record(email) is not None:
        raise AccountError(f"{email} already has an account")
    password = _new_password()
    User(name, email, password, role="admin" if admin else "user").save()
    return password, user_sync.sync_account(email, password)


def new_password(email):
    """Give an account a new generated password; returns (password, sync results)."""
    email, record = _existing(email)
    password = _new_password()
    User(record["name"], email, password, role=record.get("role") or "user").save()
    return password, user_sync.sync_account(email, password)


def change_role(email, role):
    if role not in ("admin", "user"):
        raise AccountError("Role must be admin or user")
    email, record = _existing(email)
    User(record["name"], email, record["password"], is_hashed=True, role=role).save()
    return user_sync.sync_account(email)


def delete_account(email):
    """Remove an account from the other tools, then here (watches etc. cascade)."""
    from birddog import app_db
    email, _ = _existing(email)
    results = user_sync.remove_account(email)
    with app_db.cursor() as cur:
        cur.execute("DELETE FROM app.user_account WHERE email = %s", (email,))
    return results


def accounts():
    """Every account with its per-tool sync state, for the Users page."""
    from birddog import app_db
    with app_db.cursor() as cur:
        cur.execute("SELECT email, name, role, created_at FROM app.user_account ORDER BY lower(name), email")
        rows = cur.fetchall()
        cur.execute("SELECT email, tool, state, detail FROM app.user_sync")
        sync = {}
        for email, tool, state, detail in cur.fetchall():
            sync.setdefault(email, {})[tool] = {"state": state, "detail": detail}
    return [{"email": e, "name": n, "role": r or "user", "created_at": c, "sync": sync.get(e, {})}
            for e, n, r, c in rows]


# --- CLI ------------------------------------------------------------------------

def _run(fn, *args):
    try:
        return fn(*args)
    except AccountError as err:
        sys.exit(str(err))


def add(email, name, admin=False):
    password, results = _run(create_account, email, name, admin)
    print(f"created {email.strip().lower()} ({'admin' if admin else 'user'}), password: {password}")
    _report(results)


def reset_password(email):
    password, results = _run(new_password, email)
    print(f"new password for {email.strip().lower()}: {password}")
    _report(results)


def set_role(email, role):
    results = _run(change_role, email, role)
    print(f"{email.strip().lower()} is now {role}")
    _report(results)


def remove(email):
    results = _run(delete_account, email)
    _report(results)
    print(f"removed {email.strip().lower()}")


def sync(email=None):
    emails = [email.strip().lower()] if email else [a["email"] for a in accounts()]
    for each in emails:
        print(each)
        _report(user_sync.sync_account(each))


def _report(results):
    if not results:
        print("  other tools: none configured (BIRDDOG_SYNC_* settings), nothing copied")
    for tool, (state, detail) in sorted(results.items()):
        print(f"  {tool:9} {state:5} {detail or ''}")


def list_users():
    for a in sorted(accounts(), key=lambda a: a["created_at"]):
        print(f"{a['created_at']:%Y-%m-%d}  {a['role']:5}  {a['email']}  {a['name']}")


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
