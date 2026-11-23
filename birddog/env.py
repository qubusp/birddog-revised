"""
deployment environment sniffer
"""

import os

def detect_environment():
    if os.environ.get("BIRDDOG_AWS_ENVIRONMENT"):
        return "aws"
    return "local"

# NocoDB base whose per-deployment DynamoDB tables carry a suffix. STAGE and PROD
# share one AWS account (and, today, one EB environment), so state that belongs
# to a base -- task queues/state and wiki-tracker watermarks -- would otherwise
# be shared: tasks queued against one base would run against the other after a
# repoint, and tracker watermarks advanced on STAGE would make PROD skip
# changes. Also applied to user-facing state (watchlists, preferences, watcher
# items) so STAGE and PROD don't cross over. PROD and LOCAL keep the original,
# unsuffixed table names. The suffix
# keeps names inside the EB role's "bd_*" IAM resource pattern.
_TABLE_SUFFIX_BY_NOCODB_ENV = {
    "CLOUD_STAGE": "_STAGE",
}

def env_table_name(name):
    """Return the KV/queue table name to use for the current NocoDB base
    (BIRDDOG_NOCODB_ENV), e.g. "bd_task_queue" -> "bd_task_queue_STAGE" on
    CLOUD_STAGE. Evaluated at call time so a notebook that sets the variable
    before constructing a store gets the matching table."""
    return name + _TABLE_SUFFIX_BY_NOCODB_ENV.get(os.environ.get("BIRDDOG_NOCODB_ENV", ""), "")

def base_table_name(name):
    """Inverse of env_table_name: strip a per-base suffix. On the Postgres stack
    each base has its own database, so stores there use the plain name."""
    for suffix in _TABLE_SUFFIX_BY_NOCODB_ENV.values():
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name

_STORE_BACKENDS = ("postgres", "dynamodb", "sqlite")

def store_backend():
    """Where the app keeps its own state (accounts, watchlists, preferences,
    key-value stores, queues, request/service logs).

    BIRDDOG_STORE_BACKEND (postgres | dynamodb | sqlite) wins when set;
    otherwise Postgres when BIRDDOG_DATABASE_URL is set (the new stack),
    DynamoDB on AWS, SQLite locally."""
    backend = os.environ.get("BIRDDOG_STORE_BACKEND", "").strip().lower()
    if backend:
        if backend not in _STORE_BACKENDS:
            raise ValueError(f"BIRDDOG_STORE_BACKEND must be one of {', '.join(_STORE_BACKENDS)}, got {backend!r}")
        return backend
    if os.environ.get("BIRDDOG_DATABASE_URL"):
        return "postgres"
    return "dynamodb" if detect_environment() == "aws" else "sqlite"


def mode_mismatch():
    """A warning message when the two mode switches disagree, else None.

    pg.enabled() (BIRDDOG_DATABASE_URL) decides where page and archive data
    is read from and whether background work runs in Airflow;
    store_backend() decides where the app keeps its own state. With
    BIRDDOG_STORE_BACKEND overriding one of them, the app runs half on each
    stack, which is rarely what was meant."""
    database = bool(os.environ.get("BIRDDOG_DATABASE_URL"))
    backend = store_backend()
    if database and backend != "postgres":
        return (f"BIRDDOG_DATABASE_URL is set but BIRDDOG_STORE_BACKEND={backend}: catalog reads and "
                f"background jobs use Postgres, while accounts, watchlists, alerts and logs are kept "
                f"in {backend}. Unset BIRDDOG_STORE_BACKEND to keep everything in Postgres.")
    if not database and backend == "postgres":
        return ("BIRDDOG_STORE_BACKEND=postgres but BIRDDOG_DATABASE_URL is not set: app state has "
                "no database to use and every store call will fail. Set BIRDDOG_DATABASE_URL, or "
                "unset BIRDDOG_STORE_BACKEND for the legacy stores.")
    return None
