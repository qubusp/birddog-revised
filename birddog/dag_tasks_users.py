"""Task callables for dags/yaml/migration_users.yml (dag-factory).

Kept apart from birddog/dag_tasks.py. The work lives in birddog.dynamo_migrate;
these only read the run's params (Airflow passes `params` because it is in the
signature) and return the counts for the task log / XCom."""

import logging

_log = logging.getLogger(__name__)


def _report(results):
    for r in results:
        _log.info("%s (dry_run=%s): %s", r["step"], r.get("dry_run"), r["counts"])
        for err in r["errors"]:
            _log.warning("%s: %s", r["step"], err)
    return results


def migrate_app_state(params=None):
    """Accounts, watchlists, preferences and KV state, in one transaction."""
    from birddog.dynamo_migrate import migrate_app_state as run, WATCHER_TABLE
    params = params or {}
    tables = [t for t in (params.get("kv_tables") or [WATCHER_TABLE]) if t]
    return _report(run(
        dry_run=not params.get("apply", False),
        source_env=params.get("source_env") or None,
        kv_tables=tables))


def migrate_logs(params=None):
    from birddog.dynamo_migrate import migrate_logs as run, DEFAULT_LOG_DAYS
    params = params or {}
    if not params.get("include_logs", False):
        _log.info("include_logs is off; skipping")
        return {"step": "logs", "skipped": True}
    return _report([run(
        dry_run=not params.get("apply", False),
        days=int(params.get("log_days") or DEFAULT_LOG_DAYS))])[0]
