"""Postgres persistence for accounts, watchlists and preferences.

Used by birddog/user.py when birddog.env.store_backend() is "postgres";
otherwise user.py keeps its legacy home (account JSON in the object cache,
watchlist and preferences in the default KeyValueStore).

    app.user_account     one row per account; password_hash is the werkzeug
                         hash string exactly as before
    app.watch            one row per watchlist entry (email, archive_title)
    app.user_preference  one row per (email, key), JSON value

Watchlist items keep the dict shape user.py has always used:
{"cutoff_date", ["last_checked_date"], "include", "exclude"}, dates as
UTC "YYYY-MM-DDTHH:MM:SSZ" strings."""

import json

from birddog import app_db

_ISO = 'YYYY-MM-DD"T"HH24:MI:SS"Z"'
_WATCH_COLUMNS = (
    f"archive_title, to_char(cutoff_date AT TIME ZONE 'UTC', '{_ISO}'), "
    f"to_char(last_checked_date AT TIME ZONE 'UTC', '{_ISO}'), include, exclude"
)


def _watch_item(row):
    _, cutoff, last_checked, include, exclude = row
    item = {"cutoff_date": cutoff}
    if last_checked:
        item["last_checked_date"] = last_checked
    item["include"] = list(include or [])
    item["exclude"] = list(exclude or [])
    return item


class PostgresUserStore:

    # --- accounts --------------------------------------------------------

    def load_user(self, email):
        """The account as User.from_dict() takes it, or None."""
        with app_db.cursor() as cur:
            cur.execute("SELECT name, password_hash, role FROM app.user_account WHERE email = %s", (email,))
            row = cur.fetchone()
        if row is None:
            return None
        return {"name": row[0], "password": row[1], "role": row[2]}

    def save_user(self, email, record):
        with app_db.cursor() as cur:
            cur.execute(
                """INSERT INTO app.user_account (email, name, password_hash, role)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (email) DO UPDATE
                   SET name = EXCLUDED.name, password_hash = EXCLUDED.password_hash,
                       role = EXCLUDED.role, updated_at = now()""",
                (email, record["name"], record["password"], record.get("role") or "user"))

    # --- watchlist -------------------------------------------------------

    def watchlist(self, email):
        with app_db.cursor() as cur:
            cur.execute(f"SELECT {_WATCH_COLUMNS} FROM app.watch WHERE email = %s ORDER BY archive_title", (email,))
            return {row[0]: _watch_item(row) for row in cur.fetchall()}

    def get_watch(self, email, title):
        """Raises KeyError when the title isn't watched (like the KV store)."""
        with app_db.cursor() as cur:
            cur.execute(f"SELECT {_WATCH_COLUMNS} FROM app.watch WHERE email = %s AND archive_title = %s",
                        (email, title))
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"{email}:{title} not watched")
        return _watch_item(row)

    def put_watch(self, email, title, item, cur=None):
        params = (email, title, item.get("include") or [], item.get("exclude") or [],
                  item.get("cutoff_date"), item.get("last_checked_date"))
        sql = """INSERT INTO app.watch (email, archive_title, include, exclude, cutoff_date, last_checked_date)
                 VALUES (%s, %s, %s, %s, %s, %s)
                 ON CONFLICT (email, archive_title) DO UPDATE
                 SET include = EXCLUDED.include, exclude = EXCLUDED.exclude,
                     cutoff_date = EXCLUDED.cutoff_date, last_checked_date = EXCLUDED.last_checked_date"""
        if cur is not None:
            cur.execute(sql, params)
            return
        with app_db.cursor() as own:
            own.execute(sql, params)

    def remove_watch(self, email, title):
        with app_db.cursor() as cur:
            cur.execute("DELETE FROM app.watch WHERE email = %s AND archive_title = %s", (email, title))
            return cur.rowcount > 0

    # --- preferences -----------------------------------------------------

    def get_preference(self, email, key):
        """Raises KeyError when unset."""
        with app_db.cursor() as cur:
            cur.execute("SELECT value FROM app.user_preference WHERE email = %s AND key = %s", (email, key))
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"{email}:{key} not set")
        return row[0]

    def set_preference(self, email, key, value, cur=None):
        # value goes in as JSON text, so a JSON string "x" stays a string
        sql = """INSERT INTO app.user_preference (email, key, value) VALUES (%s, %s, %s::jsonb)
                 ON CONFLICT (email, key) DO UPDATE SET value = EXCLUDED.value"""
        params = (email, key, json.dumps(value))
        if cur is not None:
            cur.execute(sql, params)
            return
        with app_db.cursor() as own:
            own.execute(sql, params)

