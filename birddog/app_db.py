"""Pooled Postgres connections for the web app's own state (app.* schema).

The Postgres stores in store.py / log.py / user_store.py run on every request
(and LogService writes a row per store call), so they borrow connections from
a small per-process pool instead of opening one each time like pg.connect().
Callers block when the pool is busy rather than failing. Connections are in
autocommit mode (one round trip per call).

The catalog read layer (catalog.py) has its own pool of read-only
connections (read_cursor()): each borrow is one read-only transaction, so a
page payload's several queries see one snapshot, and a stray write fails."""

import os
import threading
from contextlib import contextmanager

from birddog import pg

_POOL_MAX = int(os.environ.get("BIRDDOG_DB_POOL_MAX", "8"))

_lock = threading.Lock()
_pool = None
_read_pool = None


class _Pool:
    """Lazy LIFO pool: at most `size` connections, opened on demand and kept
    open. (psycopg2.pool closes every connection above minconn on return, so
    with a lazy minconn of 0 it would reconnect on every call.)"""

    def __init__(self, url, size, readonly=False):
        self.url = url
        self.readonly = readonly
        self.pid = os.getpid()
        self._idle = []
        self._idle_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(size)

    def get(self):
        import psycopg2
        self._slots.acquire()
        try:
            with self._idle_lock:
                conn = self._idle.pop() if self._idle else None
            if conn is None or conn.closed:
                conn = psycopg2.connect(self.url)
                if self.readonly:
                    # one read-only transaction per borrow (committed or
                    # rolled back by connection())
                    conn.set_session(readonly=True, autocommit=False)
                else:
                    # every store call is a single statement, so autocommit
                    # saves the BEGIN/COMMIT round trips without losing atomicity
                    conn.autocommit = True
            return conn
        except Exception:
            self._slots.release()
            raise

    def put(self, conn, discard=False):
        try:
            if discard or conn.closed:
                if not conn.closed:
                    conn.close()
            else:
                with self._idle_lock:
                    self._idle.append(conn)
        finally:
            self._slots.release()

    def close_all(self):
        with self._idle_lock:
            idle, self._idle = self._idle, []
        for conn in idle:
            conn.close()


def _usable(pool, url):
    # a forked worker (gunicorn) must not share its parent's sockets
    return pool is not None and pool.pid == os.getpid() and pool.url == url


def _get_pool(readonly=False):
    global _pool, _read_pool
    url = pg.database_url()
    pool = _read_pool if readonly else _pool
    if _usable(pool, url):
        return pool
    with _lock:
        pool = _read_pool if readonly else _pool
        if not _usable(pool, url):
            if not url:
                raise RuntimeError("BIRDDOG_DATABASE_URL is not set (needed by the Postgres store backend)")
            if pool is not None and pool.pid == os.getpid():
                pool.close_all()   # BIRDDOG_DATABASE_URL changed
            pool = _Pool(url, _POOL_MAX, readonly=readonly)
            if readonly:
                _read_pool = pool
            else:
                _pool = pool
        return pool


def reset_pool():
    """Close every pooled connection (tests, or after BIRDDOG_DATABASE_URL changes)."""
    global _pool, _read_pool
    with _lock:
        for pool in (_pool, _read_pool):
            if pool is not None and pool.pid == os.getpid():
                pool.close_all()
        _pool = _read_pool = None


@contextmanager
def connection(readonly=False):
    """Yields a pooled connection. By default it is in autocommit mode: each
    statement commits on its own; use `with conn:` inside for a
    multi-statement transaction. readonly=True borrows from the read-only
    pool instead: the whole block is one read-only transaction."""
    import psycopg2
    pool = _get_pool(readonly)
    conn = pool.get()
    broken = False
    try:
        yield conn
        conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        broken = True
        raise
    except BaseException:
        if not conn.closed:
            try:
                conn.rollback()
            except Exception:
                broken = True
        raise
    finally:
        pool.put(conn, discard=broken)


@contextmanager
def cursor():
    with connection() as conn, conn.cursor() as cur:
        yield cur


@contextmanager
def read_cursor():
    """A cursor on a pooled read-only connection (one transaction)."""
    with connection(readonly=True) as conn, conn.cursor() as cur:
        yield cur
