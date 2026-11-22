"""Storage for the archive-root registry (see wiki.py).

Two implementations behind one small interface: PostgresArchiveRootStore
(core.archive_root) and KVArchiveRootStore, which wraps the legacy key-value
store so the old runtime and the existing tests keep working. Records are
{"label": str, "description": str} keyed by canonical title."""

import json

from birddog import pg

_KV_NAMESPACE = "archive_roots"


class PostgresArchiveRootStore:
    def all(self):
        with pg.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT title, label, description FROM core.archive_root")
            return {title: {"label": label, "description": description}
                    for title, label, description in cur.fetchall()}

    def put(self, title, record):
        with pg.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO core.archive_root (title, label, description)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (title) DO UPDATE
                   SET label = EXCLUDED.label, description = EXCLUDED.description""",
                (title, record["label"], record.get("description", "")),
            )

    def remove(self, title):
        with pg.connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM core.archive_root WHERE title = %s", (title,))


class KVArchiveRootStore:
    def __init__(self, kv=None):
        if kv is None:
            from birddog.store import KeyValueStore
            kv = KeyValueStore()
        self._kv = kv

    def all(self):
        return {title: json.loads(value) for title, value in self._kv.get_all(_KV_NAMESPACE)}

    def put(self, title, record):
        self._kv.insert(_KV_NAMESPACE, title, json.dumps(record))

    def remove(self, title):
        self._kv.remove_if_exists(_KV_NAMESPACE, title)


def default_store():
    return PostgresArchiveRootStore() if pg.enabled() else KVArchiveRootStore()
