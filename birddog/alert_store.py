"""Watcher alert state on Postgres: app.watch.watcher and app.alert_state
(deploy/db/migrations/016_alert_state.sql).

watcher.py keeps its state behind a small key-value interface, in three
partitions per (user, watch) plus an index of active watches:

    namespace               key            value (JSON)
    w:<email>               archive title  watcher header
    u:<email>:<archive>     item title     unresolved entry
    r:<email>:<archive>     item title     resolve history (list)
    active                  <email>:<arch> {"email", "title"}

AlertStateStore implements that interface on the structured tables, so
watcher.py's logic is the same on every backend: the header is a column of
the watch row, and an item's unresolved entry and history share one
app.alert_state row. "active" is derived (every watch with a header) and
writes to it are ignored.

Differences from the generic KV store, both deliberate:
- State is only kept for watches on the watchlist (app.watch rows). Writes
  for a watch that is not there are dropped instead of leaving orphans, and
  removing a watch from the watchlist drops its state (FK cascade).
- The state that was in app.kv (store "bd_watchers", from the earlier
  Postgres code or the DynamoDB migration) is copied in once per process,
  before the first call, and moved to the store "bd_watchers_copied" so it
  is copied only once but kept for recovery (copy_kv_state())."""

import json
import threading

from birddog import app_db
from birddog.log import LogService, get_logger
from birddog.utility import json_size

_logger = get_logger()

_ACTIVE_NS = "active"
# moved here after the copy; kept for recovery, read by nothing
_COPIED_SUFFIX = "_copied"
# pg_advisory_xact_lock key, so two processes don't copy at once
_COPY_LOCK_KEY = 0x62645f616c657274   # "bd_alert"


def _parse_namespace(namespace):
    """("w", email, None) / ("u" | "r", email, archive_title) / ("active", None, None)."""
    if namespace == _ACTIVE_NS:
        return _ACTIVE_NS, None, None
    kind, _, rest = namespace.partition(":")
    if kind == "w" and rest:
        return kind, rest, None
    if kind in ("u", "r"):
        email, sep, archive_title = rest.partition(":")
        if sep and email:
            return kind, email, archive_title
    raise ValueError(f"alert store: unknown namespace {namespace!r}")


# ----------------------------------------------------------------------------
# one-time copy from app.kv

_PARSED_ITEMS = """
    SELECT substring(namespace from '^[ur]:([^:]*):') AS email,
           substring(namespace from '^[ur]:[^:]*:(.*)$') AS archive_title,
           key AS item_title, left(namespace, 1) AS kind, value
    FROM app.kv WHERE store = %(store)s AND namespace ~ '^[ur]:[^:]+:'"""


def copy_kv_state(cur, store):
    """Copies watcher state from app.kv store `store` into app.watch.watcher
    and app.alert_state, then moves those kv rows to `store`_copied. Existing
    structured state wins over a kv row for the same watch or item. Run inside
    a transaction. Returns counts."""
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_COPY_LOCK_KEY,))
    params = {"store": store}
    counts = {}

    cur.execute(
        """UPDATE app.watch w
           SET watcher = kv.value::jsonb, moved_to = kv.value::jsonb ->> 'moved_to'
           FROM app.kv kv
           WHERE kv.store = %(store)s AND kv.namespace = 'w:' || w.email
             AND kv.key = w.archive_title AND w.watcher IS NULL""",
        params)
    counts["headers"] = cur.rowcount

    cur.execute(
        f"""INSERT INTO app.alert_state (email, archive_title, item_title, resolved, entry, history)
            SELECT i.email, i.archive_title, i.item_title,
                   bool_and(i.kind <> 'u'),
                   coalesce(max(CASE WHEN i.kind = 'u' THEN i.value END)::jsonb, '{{}}'),
                   coalesce(max(CASE WHEN i.kind = 'r' THEN i.value END)::jsonb, '[]')
            FROM ({_PARSED_ITEMS}) i
            JOIN app.watch w ON w.email = i.email AND w.archive_title = i.archive_title
            GROUP BY i.email, i.archive_title, i.item_title
            ON CONFLICT (email, archive_title, item_title) DO NOTHING""",
        params)
    counts["items"] = cur.rowcount
    # a history-only row whose history is empty carries nothing
    cur.execute("DELETE FROM app.alert_state WHERE resolved AND history = '[]'")

    cur.execute(
        f"""SELECT count(*) FROM ({_PARSED_ITEMS}) i
            WHERE NOT EXISTS (SELECT 1 FROM app.watch w
                              WHERE w.email = i.email AND w.archive_title = i.archive_title)""",
        params)
    counts["orphaned_kv_rows"] = cur.fetchone()[0]

    cur.execute(
        """INSERT INTO app.kv (store, namespace, key, value, updated_at)
           SELECT %(copied)s, namespace, key, value, updated_at FROM app.kv WHERE store = %(store)s
           ON CONFLICT (store, namespace, key) DO UPDATE
           SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at""",
        {"store": store, "copied": store + _COPIED_SUFFIX})
    counts["kv_rows"] = cur.rowcount
    cur.execute("DELETE FROM app.kv WHERE store = %(store)s", params)
    return counts


# ----------------------------------------------------------------------------
# the store

class AlertStateStore:
    """The key-value interface watcher.py uses (see the module docstring),
    on app.watch.watcher and app.alert_state."""

    def __init__(self, legacy_store="bd_watchers"):
        self._legacy_store = legacy_store
        self._copied = False
        self._copy_lock = threading.Lock()

    # --- one-time copy ---------------------------------------------------

    def _ensure_copied(self):
        if self._copied:
            return
        with self._copy_lock:
            if self._copied:
                return
            with app_db.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT EXISTS (SELECT 1 FROM app.kv WHERE store = %s)", (self._legacy_store,))
                    pending = cur.fetchone()[0]
                if pending:
                    with conn, conn.cursor() as cur:   # one transaction
                        counts = copy_kv_state(cur, self._legacy_store)
                    _logger.info(f"alert store: copied watcher state from app.kv "
                                 f"{self._legacy_store!r}: {counts}")
                    if counts["orphaned_kv_rows"]:
                        _logger.warning(
                            f"alert store: {counts['orphaned_kv_rows']} kv row(s) belong to watches that are "
                            f"not on any watchlist and were not copied (kept in "
                            f"{self._legacy_store + _COPIED_SUFFIX!r})")
            self._copied = True

    def _cursor(self):
        self._ensure_copied()
        return app_db.cursor()

    # --- reads -----------------------------------------------------------

    def get(self, namespace, key):
        kind, email, archive_title = _parse_namespace(namespace)
        with LogService("KVStore", "get", path=namespace) as log:
            if kind == _ACTIVE_NS:
                found = {k: v for k, v in self.get_all(namespace)}
                if key not in found:
                    raise KeyError(f"{namespace}:{key} not found")
                return found[key]
            with self._cursor() as cur:
                if kind == "w":
                    cur.execute("SELECT watcher FROM app.watch WHERE email = %s AND archive_title = %s "
                                "AND watcher IS NOT NULL", (email, key))
                else:
                    cur.execute(f"SELECT {self._value_column(kind)} FROM app.alert_state "
                                f"WHERE email = %s AND archive_title = %s AND item_title = %s "
                                f"AND {self._present(kind)}", (email, archive_title, key))
                row = cur.fetchone()
            if row is None:
                raise KeyError(f"{namespace}:{key} not found")
            value = json.dumps(row[0])
            log.size = len(value)
        return value

    def get_all(self, namespace):
        kind, email, archive_title = _parse_namespace(namespace)
        with LogService("KVStore", "get_all", path=namespace) as log:
            with self._cursor() as cur:
                if kind == _ACTIVE_NS:
                    cur.execute("""SELECT email, archive_title FROM app.watch WHERE watcher IS NOT NULL
                                   ORDER BY email || ':' || archive_title COLLATE "C" """)
                    rows = [(f"{e}:{t}", json.dumps({"email": e, "title": t})) for e, t in cur.fetchall()]
                elif kind == "w":
                    cur.execute("""SELECT archive_title, watcher FROM app.watch
                                   WHERE email = %s AND watcher IS NOT NULL
                                   ORDER BY archive_title COLLATE "C" """, (email,))
                    rows = [(k, json.dumps(v)) for k, v in cur.fetchall()]
                else:
                    # COLLATE "C": byte order, like the KV stores
                    cur.execute(f"""SELECT item_title, {self._value_column(kind)} FROM app.alert_state
                                    WHERE email = %s AND archive_title = %s AND {self._present(kind)}
                                    ORDER BY item_title COLLATE "C" """, (email, archive_title))
                    rows = [(k, json.dumps(v)) for k, v in cur.fetchall()]
            log.size = json_size(rows)
        return rows

    def count(self, namespace):
        return len(self.get_all(namespace))

    def get_many(self, namespace, keys):
        if not keys:
            return {}
        kind, email, archive_title = _parse_namespace(namespace)
        if kind not in ("u", "r"):
            wanted = set(keys)
            return {k: v for k, v in self.get_all(namespace) if k in wanted}
        with LogService("KVStore", "get_many", path=namespace) as log:
            with self._cursor() as cur:
                cur.execute(f"""SELECT item_title, {self._value_column(kind)} FROM app.alert_state
                                WHERE email = %s AND archive_title = %s AND item_title = ANY(%s)
                                AND {self._present(kind)}""",
                            (email, archive_title, list(keys)))
                result = {k: json.dumps(v) for k, v in cur.fetchall()}
            log.size = json_size(result)
        return result

    @staticmethod
    def _value_column(kind):
        return "entry" if kind == "u" else "history"

    @staticmethod
    def _present(kind):
        # u: an unresolved entry; r: a non-empty history
        return "NOT resolved" if kind == "u" else "history <> '[]'::jsonb"

    # --- writes ----------------------------------------------------------

    def insert(self, namespace, key, value):
        if not isinstance(value, str):
            raise TypeError("value must be str")
        if not isinstance(key, str):
            raise TypeError("key must be str")
        self.insert_many(namespace, {key: value})

    def insert_many(self, namespace, items):
        if not items:
            return
        kind, email, archive_title = _parse_namespace(namespace)
        if kind == _ACTIVE_NS:
            return   # derived from the headers
        size = sum(len(k) + len(v) for k, v in items.items())
        with LogService("KVStore", "insert_many", path=namespace, size=size):
            with self._cursor() as cur:
                if kind == "w":
                    for title, header in items.items():
                        cur.execute(
                            """UPDATE app.watch SET watcher = %s::jsonb, moved_to = %s::jsonb ->> 'moved_to'
                               WHERE email = %s AND archive_title = %s""",
                            (header, header, email, title))
                    return
                from psycopg2.extras import execute_values
                if kind == "u":
                    columns, update = "entry, resolved", "entry = EXCLUDED.entry, resolved = false"
                    rows = [(email, archive_title, k, v, False) for k, v in items.items()]
                    template = "(%s, %s, %s, %s::jsonb, %s)"
                else:
                    columns, update = "history, resolved", "history = EXCLUDED.history"
                    rows = [(email, archive_title, k, v, True) for k, v in items.items()]
                    template = "(%s, %s, %s, %s::jsonb, %s)"
                # only for watches on the watchlist (see the module docstring)
                execute_values(
                    cur,
                    f"""INSERT INTO app.alert_state (email, archive_title, item_title, {columns})
                        SELECT v.* FROM (VALUES %s) AS v (email, archive_title, item_title, {columns})
                        JOIN app.watch w ON w.email = v.email AND w.archive_title = v.archive_title
                        ON CONFLICT (email, archive_title, item_title) DO UPDATE SET {update}""",
                    rows, template=template, page_size=1000)
                if kind == "r":
                    cur.execute("""DELETE FROM app.alert_state WHERE email = %s AND archive_title = %s
                                   AND item_title = ANY(%s) AND resolved AND history = '[]'::jsonb""",
                                (email, archive_title, list(items)))

    def update_if_exists(self, namespace, key, value):
        try:
            self.get(namespace, key)
        except KeyError:
            return
        self.insert(namespace, key, value)

    def _remove(self, namespace, keys, method):
        """Clears the partition's part of each row (all rows when keys is
        None); rows left with neither part are deleted. Returns the count."""
        kind, email, archive_title = _parse_namespace(namespace)
        if kind == _ACTIVE_NS:
            return 0   # derived from the headers
        with LogService("KVStore", method, path=namespace, size=len(keys) if keys else 0):
            with self._cursor() as cur:
                if kind == "w":
                    sql = ("UPDATE app.watch SET watcher = NULL, moved_to = NULL "
                           "WHERE email = %s AND watcher IS NOT NULL")
                    params = [email]
                    if keys is not None:
                        sql += " AND archive_title = ANY(%s)"
                        params.append(list(keys))
                    cur.execute(sql, params)
                    return cur.rowcount
                where = "email = %s AND archive_title = %s"
                params = [email, archive_title]
                if keys is not None:
                    where += " AND item_title = ANY(%s)"
                    params.append(list(keys))
                present = self._present(kind)
                if kind == "u":
                    other_empty, clear = "history = '[]'::jsonb", "resolved = true, entry = '{}'::jsonb"
                else:
                    other_empty, clear = "resolved", "history = '[]'::jsonb"
                # one statement: both parts see the same snapshot and touch disjoint rows
                cur.execute(
                    f"""WITH gone AS (DELETE FROM app.alert_state
                                      WHERE {where} AND {present} AND {other_empty} RETURNING 1),
                             cleared AS (UPDATE app.alert_state SET {clear}
                                         WHERE {where} AND {present} AND NOT ({other_empty}) RETURNING 1)
                        SELECT (SELECT count(*) FROM gone) + (SELECT count(*) FROM cleared)""",
                    params + params)
                return cur.fetchone()[0]

    def remove(self, namespace, key):
        self._remove(namespace, [key], "remove")

    def remove_if_exists(self, namespace, key):
        return self._remove(namespace, [key], "remove_if_exists") > 0

    def remove_many(self, namespace, keys):
        if keys:
            self._remove(namespace, list(keys), "remove_many")

    def remove_all(self, namespace):
        self._remove(namespace, None, "remove_all")
