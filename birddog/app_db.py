"""Pooled Postgres connections for the web app's own state (app.* schema).

The Postgres stores in store.py / log.py / user_store.py run on every request
(and LogService writes a row per store call), so they borrow connections from
a small per-process pool instead of opening one each time like pg.connect().
Callers block when the pool is busy rather than failing. Connections are in
autocommit mode (one round trip per call)."""

import os
import threading
from contextlib import contextmanager

from birddog import pg

_POOL_MAX = int(os.environ.get("BIRDDOG_DB_POOL_MAX", "8"))

_lock = threading.Lock()
_pool = None


class _Pool:
    """Lazy LIFO pool: at most `size` connections, opened on demand and kept
    open. (psycopg2.pool closes every connection above minconn on return, so
    with a lazy minconn of 0 it would reconnect on every call.)"""

    def __init__(self, url, size):
        self.url = url
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


def _get_pool():
    global _pool
    pool = _pool
    if pool is not None and pool.pid == os.getpid():
        return pool
    with _lock:
        # a forked worker (gunicorn) must not share its parent's sockets
        if _pool is None or _pool.pid != os.getpid():
            url = pg.database_url()
            if not url:
                raise RuntimeError("BIRDDOG_DATABASE_URL is not set (needed by the Postgres store backend)")
            _pool = _Pool(url, _POOL_MAX)
        return _pool


def reset_pool():
    """Close every pooled connection (tests, or after BIRDDOG_DATABASE_URL changes)."""
    global _pool
    with _lock:
        if _pool is not None and _pool.pid == os.getpid():
            _pool.close_all()
        _pool = None


@contextmanager
def connection():
    """Yields a pooled autocommit connection: each statement commits on its
    own. Use `with conn:` inside for a multi-statement transaction."""
    import psycopg2
    pool = _get_pool()
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
