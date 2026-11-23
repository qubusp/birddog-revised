"""One-off copy of the old AWS web-app state into Postgres app.*.

    S3   birddog-data/[stage/]users/<email>.json  -> app.user_account
         (plus any legacy embedded "watchlist"/"preferences" dicts)
    DDB  birddog_key_value_store[_STAGE]
           wl:<email>    -> app.watch            (legacy keys coarsened like user.py)
           pref:<email>  -> app.user_preference
           anything else -> app.kv store "birddog_key_value_store"
    DDB  bd_watchers[_STAGE]                     -> app.kv store "bd_watchers"
    DDB  extra KV tables (kv_tables=...)         -> app.kv, one store per table
    DDB  birddog_request_logs / birddog_service_logs (optional)
                                                 -> app.request_log / app.service_log

Every write is an upsert (logs: insert-if-new keyed on the DynamoDB timestamp),
so re-running is safe. Dry run (the default) does the same writes inside a
transaction and rolls it back, so the counts it reports are exact; logs are
only counted in a dry run.

Needs: BIRDDOG_DATABASE_URL, and AWS credentials the usual boto3 way
(AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY [/ AWS_SESSION_TOKEN], region from
AWS_DEFAULT_REGION, default us-east-2). The base (PROD vs STAGE tables and S3
prefix) follows BIRDDOG_NOCODB_ENV like the old runtime, or --source-env.

    python -m birddog.dynamo_migrate                 # dry run, prints counts
    python -m birddog.dynamo_migrate --apply --logs  # write, including logs
"""

import argparse
import json
import os
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

S3_BUCKET = "birddog-data"
DEFAULT_KV_TABLE = "birddog_key_value_store"
WATCHER_TABLE = "bd_watchers"
REQUEST_LOG_TABLE = "birddog_request_logs"
SERVICE_LOG_TABLE = "birddog_service_logs"
DEFAULT_REGION = "us-east-2"
DEFAULT_LOG_DAYS = 30

_SUFFIX_BY_ENV = {"CLOUD_STAGE": "_STAGE"}
_S3_PREFIX_BY_ENV = {"CLOUD_STAGE": "stage/"}
_BATCH = 1000


class _DryRunRollback(Exception):
    pass


# --- AWS side --------------------------------------------------------------

def _region():
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def dynamodb_resource():
    import boto3
    return boto3.resource("dynamodb", region_name=_region())


def s3_client():
    import boto3
    return boto3.client("s3", region_name=_region())


def _source_env(source_env=None):
    return source_env if source_env is not None else os.environ.get("BIRDDOG_NOCODB_ENV", "")


def table_name(base, source_env=None):
    return base + _SUFFIX_BY_ENV.get(_source_env(source_env), "")


def _scan(dynamodb, name, **kwargs):
    table = dynamodb.Table(name)
    resp = table.scan(**kwargs)
    yield from resp.get("Items", [])
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"], **kwargs)
        yield from resp.get("Items", [])


def _table_exists(dynamodb, name):
    try:
        dynamodb.Table(name).load()
        return True
    except Exception as err:  # botocore ResourceNotFoundException
        if "ResourceNotFound" in type(err).__name__ or "ResourceNotFound" in str(err):
            return False
        raise


def read_kv_table(dynamodb, name):
    """[(namespace, key, value)] of a KeyValueStore table."""
    return [(it["namespace"], it["key"], _text(it.get("value", ""))) for it in _scan(dynamodb, name)]


def read_user_objects(s3=None, bucket=S3_BUCKET, prefix=None, users_dir=None, source_env=None):
    """{email: account dict} from the S3 object cache (or a local copy of it)."""
    users = {}
    if users_dir:
        for fn in sorted(os.listdir(users_dir)):
            if fn.endswith(".json"):
                with open(os.path.join(users_dir, fn), encoding="utf8") as f:
                    users[fn[:-len(".json")]] = json.load(f)
        return users
    if prefix is None:
        prefix = _S3_PREFIX_BY_ENV.get(_source_env(source_env), "")
    s3 = s3 or s3_client()
    root = f"{prefix}users/"
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=root):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rest = key[len(root):]
            if "/" in rest or not rest.endswith(".json"):
                continue  # only users/<email>.json, not sub-folders
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
            users[rest[:-len(".json")]] = json.loads(body)
    return users


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        return str(int(value)) if value == value.to_integral_value() else str(value)
    return "" if value is None else str(value)


# --- Postgres side ---------------------------------------------------------

@contextmanager
def _transaction(dry_run):
    """A connection whose work is rolled back at the end in a dry run."""
    from birddog import pg
    try:
        with pg.connect() as conn:
            yield conn
            if dry_run:
                raise _DryRunRollback()
    except _DryRunRollback:
        pass


def _upsert_counts(rows):
    """rows of RETURNING (xmax = 0) -> Counter(inserted/updated)."""
    c = Counter()
    for (inserted,) in rows:
        c["inserted" if inserted else "updated"] += 1
    return c


def _upsert_user(cur, email, record):
    cur.execute(
        """INSERT INTO app.user_account (email, name, password_hash, role)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (email) DO UPDATE
           SET name = EXCLUDED.name, password_hash = EXCLUDED.password_hash,
               role = EXCLUDED.role, updated_at = now()
           RETURNING (xmax = 0)""",
        (email, record["name"], record["password"], record.get("role") or "user"))
    return cur.fetchone()[0]


def _existing_users(cur):
    cur.execute("SELECT email FROM app.user_account")
    return {r[0] for r in cur.fetchall()}


def _put_watch(cur, email, title, item):
    cur.execute(
        """INSERT INTO app.watch (email, archive_title, include, exclude, cutoff_date, last_checked_date)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (email, archive_title) DO UPDATE
           SET include = EXCLUDED.include, exclude = EXCLUDED.exclude,
               cutoff_date = EXCLUDED.cutoff_date, last_checked_date = EXCLUDED.last_checked_date
           RETURNING (xmax = 0)""",
        (email, title, item.get("include") or [], item.get("exclude") or [],
         item.get("cutoff_date"), item.get("last_checked_date")))
    return cur.fetchone()[0]


def _put_preference(cur, email, key, json_text):
    cur.execute(
        """INSERT INTO app.user_preference (email, key, value) VALUES (%s, %s, %s::jsonb)
           ON CONFLICT (email, key) DO UPDATE SET value = EXCLUDED.value
           RETURNING (xmax = 0)""",
        (email, key, json_text))
    return cur.fetchone()[0]


def _put_kv(cur, store, rows):
    from psycopg2.extras import execute_values
    if not rows:
        return Counter()
    out = execute_values(
        cur,
        """INSERT INTO app.kv (store, namespace, key, value) VALUES %s
           ON CONFLICT (store, namespace, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
           RETURNING (xmax = 0)""",
        [(store, ns, k, v) for ns, k, v in rows], page_size=_BATCH, fetch=True)
    return _upsert_counts(out)


def _savepoint(cur, fn, *args):
    """Run one row write; a bad row (e.g. unparseable date) is rolled back and
    reported instead of failing the whole copy."""
    cur.execute("SAVEPOINT row_write")
    try:
        result = fn(cur, *args)
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT row_write")
        raise
    cur.execute("RELEASE SAVEPOINT row_write")
    return result


# --- steps -----------------------------------------------------------------

def _valid_account(record):
    return isinstance(record, dict) and record.get("name") and record.get("password")


def migrate_users(cur, s3=None, users_dir=None, source_env=None, bucket=S3_BUCKET, prefix=None):
    """Accounts (and legacy embedded watchlist/preferences) from the object cache."""
    from birddog.user import coarsen_watch_items, _parse_watch_item
    users = read_user_objects(s3=s3, bucket=bucket, prefix=prefix, users_dir=users_dir, source_env=source_env)
    counts = Counter(source_users=len(users))
    errors = []
    for email, record in sorted(users.items()):
        if not _valid_account(record):
            counts["users_skipped_invalid"] += 1
            errors.append(f"user {email}: missing name/password")
            continue
        inserted = _savepoint(cur, _upsert_user, email, record)
        counts["users_inserted" if inserted else "users_updated"] += 1
        # legacy: accounts never loaded since watchlists/preferences moved
        # to the KV store still carry them inline. The KV copy (step
        # "kv") runs later and wins where both exist.
        legacy_wl = record.get("watchlist")
        if isinstance(legacy_wl, dict) and legacy_wl:
            try:
                raw = {k: _parse_watch_item(json.dumps(v)) for k, v in legacy_wl.items() if isinstance(v, dict)}
                coarse, _, _ = coarsen_watch_items(raw)
            except Exception as err:
                counts["legacy_watches_failed"] += len(legacy_wl)
                errors.append(f"legacy watchlist {email}: {err}")
                coarse = {}
            for title, item in coarse.items():
                try:
                    inserted = _savepoint(cur, _put_watch, email, title, item)
                    counts["legacy_watches_inserted" if inserted else "legacy_watches_updated"] += 1
                except Exception as err:
                    counts["legacy_watches_failed"] += 1
                    errors.append(f"legacy watch {email} {title}: {err}")
        legacy_prefs = record.get("preferences")
        if isinstance(legacy_prefs, dict):
            for key, value in legacy_prefs.items():
                inserted = _savepoint(cur, _put_preference, email, key, json.dumps(value))
                counts["legacy_preferences_inserted" if inserted else "legacy_preferences_updated"] += 1
    return _result("users", counts, errors)


def migrate_default_kv(cur, dynamodb=None, source_env=None):
    """The default KV table: watchlists, preferences, and other app KV state."""
    from birddog.user import coarsen_watch_items, _parse_watch_item, _MIGRATION_MARKER_KEY
    dynamodb = dynamodb or dynamodb_resource()
    name = table_name(DEFAULT_KV_TABLE, source_env)
    rows = read_kv_table(dynamodb, name) if _table_exists(dynamodb, name) else []
    counts = Counter(source_rows=len(rows))
    errors = []
    if not rows:
        counts["source_table_missing_or_empty"] = 1

    watchlists = defaultdict(dict)   # email -> {kv key: parsed item}
    prefs = []                       # (email, key, json text)
    other = []                       # (namespace, key, value)
    for ns, key, value in rows:
        if ns.startswith("wl:"):
            if key != _MIGRATION_MARKER_KEY:
                try:
                    watchlists[ns[3:]][key] = _parse_watch_item(value)
                except Exception as err:
                    counts["watches_failed"] += 1
                    errors.append(f"watch {ns} {key}: {err}")
        elif ns.startswith("pref:"):
            try:
                json.loads(value)
                prefs.append((ns[5:], key, value))
            except ValueError as err:
                counts["preferences_failed"] += 1
                errors.append(f"preference {ns} {key}: {err}")
        else:
            other.append((ns, key, value))

    known = _existing_users(cur)
    for email, raw in sorted(watchlists.items()):
        if email not in known:
            counts["watches_skipped_no_account"] += len(raw)
            continue
        try:
            coarse, _, _ = coarsen_watch_items(raw)
        except Exception as err:
            counts["watches_failed"] += len(raw)
            errors.append(f"watchlist {email}: {err}")
            continue
        for title, item in coarse.items():
            try:
                inserted = _savepoint(cur, _put_watch, email, title, item)
                counts["watches_inserted" if inserted else "watches_updated"] += 1
            except Exception as err:
                counts["watches_failed"] += 1
                errors.append(f"watch {email} {title}: {err}")
    for email, key, value in prefs:
        if email not in known:
            counts["preferences_skipped_no_account"] += 1
            continue
        inserted = _savepoint(cur, _put_preference, email, key, value)
        counts["preferences_inserted" if inserted else "preferences_updated"] += 1
    kv = _put_kv(cur, DEFAULT_KV_TABLE, other)
    counts["kv_inserted"] += kv["inserted"]
    counts["kv_updated"] += kv["updated"]
    return _result("kv", counts, errors)


def migrate_kv_tables(cur, dynamodb=None, source_env=None, tables=(WATCHER_TABLE,)):
    """Whole KV tables copied as-is into app.kv (store = table name without suffix)."""
    dynamodb = dynamodb or dynamodb_resource()
    counts = Counter()
    for base in tables:
        name = table_name(base, source_env)
        if not _table_exists(dynamodb, name):
            counts[f"{base}_missing"] = 1
            continue
        rows = read_kv_table(dynamodb, name)
        counts[f"{base}_source_rows"] = len(rows)
        c = _put_kv(cur, base, rows)
        counts[f"{base}_inserted"] += c["inserted"]
        counts[f"{base}_updated"] += c["updated"]
    return _result("kv_tables", counts, [])


def _log_ts(value):
    # the DynamoDB loggers wrote naive datetime.utcnow().isoformat()
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def migrate_logs(dry_run=True, dynamodb=None, days=DEFAULT_LOG_DAYS):
    """Request/service logs of the last `days` days (the app keeps 30)."""
    from boto3.dynamodb.conditions import Key
    from psycopg2.extras import execute_values
    dynamodb = dynamodb or dynamodb_resource()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None).isoformat()
    specs = [
        (REQUEST_LOG_TABLE, "app.request_log", ("user_id", "method", "path", "status", "duration_ms"),
         lambda it: (it.get("user"), it.get("method"), it.get("path"),
                     int(it.get("status") or 0), int(it.get("duration") or 0))),
        (SERVICE_LOG_TABLE, "app.service_log", ("resource", "method", "path", "size", "duration_ms"),
         lambda it: (it.get("resource"), it.get("method"), it.get("path"),
                     int(it.get("size") or 0), int(it.get("duration") or 0))),
    ]
    counts = Counter()
    for source, target, columns, shape in specs:
        if not _table_exists(dynamodb, source):
            counts[f"{source}_missing"] = 1
            continue
        table = dynamodb.Table(source)
        kwargs = {"KeyConditionExpression": Key("PK").eq("LOG") & Key("timestamp").gte(since)}
        batch = []

        def flush():
            if not batch:
                return
            if not dry_run:
                from birddog import pg
                with pg.connect() as conn, conn.cursor() as cur:
                    out = execute_values(
                        cur,
                        f"INSERT INTO {target} (ts, source_key, {', '.join(columns)}) VALUES %s "
                        "ON CONFLICT (source_key) DO NOTHING RETURNING 1",
                        batch, page_size=_BATCH, fetch=True)
                    counts[f"{source}_inserted"] += len(out)
            batch.clear()

        resp = table.query(**kwargs)
        while True:
            for it in resp.get("Items", []):
                counts[f"{source}_source_rows"] += 1
                batch.append((_log_ts(it["timestamp"]), f"ddb:{it['timestamp']}", *shape(it)))
                if len(batch) >= _BATCH:
                    flush()
            if "LastEvaluatedKey" not in resp:
                break
            resp = table.query(ExclusiveStartKey=resp["LastEvaluatedKey"], **kwargs)
        flush()
    result = _result("logs", counts, [])
    result["dry_run"] = dry_run
    return result


def _result(step, counts, errors):
    return {"step": step, "counts": dict(sorted(counts.items())),
            "errors": errors[:50], "error_count": len(errors)}


def migrate_app_state(dry_run=True, dynamodb=None, s3=None, users_dir=None, source_env=None,
                      kv_tables=(WATCHER_TABLE,)):
    """Accounts, then watchlists/preferences/KV (which reference the
    accounts), then whole KV tables -- in ONE transaction: either everything
    lands or nothing does, and a dry run sees its own earlier steps."""
    dynamodb = dynamodb or dynamodb_resource()
    with _transaction(dry_run) as conn, conn.cursor() as cur:
        results = [
            migrate_users(cur, s3=s3, users_dir=users_dir, source_env=source_env),
            migrate_default_kv(cur, dynamodb=dynamodb, source_env=source_env),
            migrate_kv_tables(cur, dynamodb=dynamodb, source_env=source_env, tables=tuple(kv_tables)),
        ]
    for r in results:
        r["dry_run"] = dry_run
    return results


def migrate_all(dry_run=True, include_logs=False, dynamodb=None, s3=None, users_dir=None,
                source_env=None, kv_tables=(WATCHER_TABLE,), log_days=DEFAULT_LOG_DAYS):
    results = migrate_app_state(dry_run, dynamodb=dynamodb, s3=s3, users_dir=users_dir,
                                source_env=source_env, kv_tables=kv_tables)
    if include_logs:
        results.append(migrate_logs(dry_run, dynamodb=dynamodb, days=log_days))
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run, rolled back)")
    ap.add_argument("--logs", action="store_true", help="also copy request/service logs")
    ap.add_argument("--log-days", type=int, default=DEFAULT_LOG_DAYS)
    ap.add_argument("--source-env", help="CLOUD_PROD or CLOUD_STAGE (default: BIRDDOG_NOCODB_ENV)")
    ap.add_argument("--users-dir", help="read users/*.json from this local folder instead of S3")
    ap.add_argument("--kv-tables", default=WATCHER_TABLE,
                    help="comma-separated KV tables copied as-is (default: %(default)s)")
    args = ap.parse_args(argv)
    results = migrate_all(
        dry_run=not args.apply, include_logs=args.logs, users_dir=args.users_dir,
        source_env=args.source_env, log_days=args.log_days,
        kv_tables=[t for t in args.kv_tables.split(",") if t])
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
