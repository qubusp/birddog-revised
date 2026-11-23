"""The four steps every source runs: land -> transform -> verify -> expose.

  land(folder)       runs the fetcher and writes its records unchanged into
                     raw.source_record (one row per key, rewritten only when
                     the payload changed); logs the run in raw.source_run and
                     advances the source's watermark in raw.watermark
  transform(folder)  runs the source's transform SQL over the records landed
                     since the last successful transform (temp table `landed`)
  verify(folder)     runs the checks in source.yml; fails the run if any fails
  expose(folder)     (re)creates views.source__<name> for Mathesar

Each step takes the source folder (dags/sources/<name>) and uses
BIRDDOG_DATABASE_URL, so the same code runs in Airflow and from the CLI
(python -m birddog.sources run <name>)."""

import hashlib
import json
import logging
import re

from psycopg2 import sql as pgsql
from psycopg2.extras import execute_values

from birddog import pg
from birddog.sources.fetchers import FetchContext, canonical_json, dig, resolve
from birddog.sources.spec import load_spec

_logger = logging.getLogger(__name__)
CHUNK = 1000                 # records written per INSERT
KEY_SEPARATOR = "|"          # joins the parts of a multi-field key
_DONE = ("landed", "transformed", "verified")   # statuses of a completed land


def _spec(folder_or_spec):
    return folder_or_spec if hasattr(folder_or_spec, "dag_id") else load_spec(folder_or_spec)


def make_key(record, fields):
    parts = []
    for f in fields:
        v = dig(record, f)
        if v is None or v == "":
            raise ValueError(f"record has no value for key field {f!r}: {canonical_json(record)[:300]}")
        parts.append(str(v).strip())
    return KEY_SEPARATOR.join(parts)


def _wm_max(a, b):
    """The larger of two watermark values: numerically when both are
    numbers, else as text (ISO timestamps of one format sort correctly)."""
    if a is None:
        return b
    if b is None:
        return a
    try:
        return a if float(a) >= float(b) else b
    except (TypeError, ValueError):
        return a if str(a) >= str(b) else b


def _get_watermark(cur, name):
    cur.execute("SELECT value FROM raw.watermark WHERE source = %s", (name,))
    row = cur.fetchone()
    return row[0] if row else None


def _set_watermark(cur, name, value):
    cur.execute("""INSERT INTO raw.watermark (source, value) VALUES (%s, %s)
                   ON CONFLICT (source) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                (name, str(value)))


def _fail_run(batch_id, error):
    try:
        with pg.connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE raw.source_run SET status = 'failed', error = %s WHERE batch_id = %s",
                        (str(error)[:4000], batch_id))
    except Exception:
        _logger.exception("could not record the failure in raw.source_run")


def _write_chunk(cur, name, batch_id, chunk):
    rows = []
    for key, rec in chunk.items():
        text = canonical_json(rec)
        rows.append((name, key, text, hashlib.md5(text.encode("utf8")).hexdigest(),
                     batch_id, batch_id))
    result = execute_values(cur, """
        INSERT INTO raw.source_record AS r (source, key, payload, payload_hash, batch_id, seen_batch_id)
        VALUES %s
        ON CONFLICT (source, key) DO UPDATE SET
            seen_batch_id = EXCLUDED.seen_batch_id,
            payload   = CASE WHEN r.payload_hash = EXCLUDED.payload_hash THEN r.payload ELSE EXCLUDED.payload END,
            batch_id  = CASE WHEN r.payload_hash = EXCLUDED.payload_hash THEN r.batch_id ELSE EXCLUDED.batch_id END,
            landed_at = CASE WHEN r.payload_hash = EXCLUDED.payload_hash THEN r.landed_at ELSE now() END,
            payload_hash = EXCLUDED.payload_hash
        RETURNING r.key, r.batch_id""",
        rows, template="(%s, %s, %s::jsonb, %s, %s, %s)", page_size=CHUNK, fetch=True)
    return {k for k, b in result if b == batch_id}


# --- land ----------------------------------------------------------------------

def land(folder, run_id=None, full_refresh=False, records=None):
    """Fetches and lands one batch. `records` (an iterable) replaces the
    fetcher, for tests and one-off loads. Returns a summary with batch_id."""
    spec = _spec(folder)
    full_refresh = bool(full_refresh)
    with pg.connect() as conn, conn.cursor() as cur:
        stored = _get_watermark(cur, spec.watermark_name)
        cur.execute("""INSERT INTO raw.source_run (source, run_id, full_fetch, watermark_before)
                       VALUES (%s, %s, %s, %s) RETURNING batch_id""",
                    (spec.name, run_id, spec.mode == "full" or full_refresh, stored))
        batch_id = cur.fetchone()[0]

    watermark = None if full_refresh else (stored if stored is not None
                                           else (spec.watermark or {}).get("initial"))
    ctx = FetchContext(spec.name, spec.folder, dict(spec.fetch), watermark, full_refresh)
    _logger.info(f"source {spec.name}: batch {batch_id}, watermark {watermark!r}"
                 + (" (full refresh)" if full_refresh else ""))
    fetched, keys, changed, top = 0, set(), set(), None
    try:
        source = records if records is not None else resolve(spec.fetch, spec.folder)(ctx)
        chunk = {}
        for rec in source:
            if not isinstance(rec, dict):
                raise TypeError(f"fetcher yielded {type(rec).__name__}, expected a dict per record")
            key = make_key(rec, spec.key)
            chunk[key] = rec
            keys.add(key)
            fetched += 1
            if spec.watermark:
                top = _wm_max(top, dig(rec, spec.watermark["field"]))
            if len(chunk) >= CHUNK:
                with pg.connect() as conn, conn.cursor() as cur:
                    changed |= _write_chunk(cur, spec.name, batch_id, chunk)
                chunk = {}
        if chunk:
            with pg.connect() as conn, conn.cursor() as cur:
                changed |= _write_chunk(cur, spec.name, batch_id, chunk)

        new_wm = ctx.new_watermark if ctx.new_watermark is not None else top
        if new_wm is not None and not full_refresh:
            new_wm = _wm_max(stored, new_wm)
        with pg.connect() as conn, conn.cursor() as cur:
            if new_wm is not None:
                _set_watermark(cur, spec.watermark_name, new_wm)
            cur.execute("""UPDATE raw.source_run SET status = 'landed', landed_at = now(),
                               fetched = %s, changed = %s, watermark_after = %s
                           WHERE batch_id = %s""",
                        (fetched, len(changed), None if new_wm is None else str(new_wm), batch_id))
    except Exception as e:
        _fail_run(batch_id, f"land: {e}")
        raise
    summary = {"source": spec.name, "batch_id": batch_id, "fetched": fetched,
               "distinct_keys": len(keys), "changed": len(changed),
               "watermark": None if new_wm is None else str(new_wm)}
    _logger.info(f"source {spec.name}: landed {summary}")
    return summary


# --- transform ---------------------------------------------------------------

def _latest_full_batch(cur, name):
    cur.execute(f"""SELECT max(batch_id) FROM raw.source_run
                    WHERE source = %s AND full_fetch AND status IN {_DONE + ('failed',)}
                      AND landed_at IS NOT NULL""", (name,))
    return cur.fetchone()[0]


def prepare_session(cur, spec, upto=None):
    """Creates the temp objects a transform SQL reads:
      landed      records landed since the last transform (key, payload,
                  batch_id, landed_at, first_landed_at), one row per key
      landed_all  every current record of the source (same columns)
      gone        records a full fetch no longer returned (full mode only)
    and sets birddog.source / birddog.batch_id for current_setting().
    Returns (pending batch ids, number of landed records)."""
    if upto is None:
        cur.execute("SELECT max(batch_id) FROM raw.source_run WHERE source = %s", (spec.name,))
        upto = cur.fetchone()[0] or 0
    cur.execute("""SELECT coalesce(array_agg(batch_id ORDER BY batch_id), '{}') FROM raw.source_run
                   WHERE source = %s AND transformed_at IS NULL AND batch_id <= %s""",
                (spec.name, upto))
    pending = cur.fetchone()[0]
    full = _latest_full_batch(cur, spec.name) if spec.mode == "full" else None
    cur.execute("SELECT set_config('birddog.source', %s, true), set_config('birddog.batch_id', %s, true)",
                (spec.name, str(upto)))
    cols = "key, payload, batch_id, landed_at, first_landed_at"
    cur.execute(f"""
        DROP TABLE IF EXISTS pg_temp.landed;
        CREATE TEMP TABLE landed ON COMMIT DROP AS
            SELECT {cols} FROM raw.source_record
            WHERE source = %(s)s AND batch_id = ANY(%(p)s::bigint[]);
        CREATE INDEX ON landed (key);
        ANALYZE landed;
        CREATE OR REPLACE TEMP VIEW landed_all AS
            SELECT {cols} FROM raw.source_record WHERE source = %(s)s;
        CREATE OR REPLACE TEMP VIEW gone AS
            SELECT {cols} FROM raw.source_record
            WHERE source = %(s)s AND %(full)s::bigint IS NOT NULL AND seen_batch_id < %(full)s::bigint;
        """, {"s": spec.name, "p": list(pending), "full": full})
    cur.execute("SELECT count(*) FROM landed")
    return list(pending), cur.fetchone()[0], upto


def transform(folder, batch_id=None):
    """Runs transform.sql in one transaction over the not-yet-transformed
    batches up to batch_id (default: all). On success those batches are
    marked transformed; on failure nothing changes and the next run retries
    them along with its own."""
    spec = _spec(folder)
    sql_text = spec.transform.read_text(encoding="utf8")
    try:
        with pg.connect() as conn, conn.cursor() as cur:
            pending, n, upto = prepare_session(cur, spec, batch_id)
            _logger.info(f"source {spec.name}: transforming {n} records from batches {pending}")
            cur.execute(sql_text)
            cur.execute("""UPDATE raw.source_run SET transformed_at = now(),
                               status = CASE WHEN status = 'landed' THEN 'transformed' ELSE status END
                           WHERE batch_id = ANY(%s::bigint[])""", (pending,))
    except Exception as e:
        if batch_id is not None:
            _fail_run(batch_id, f"transform: {e}")
        raise
    return {"source": spec.name, "batch_id": upto, "batches": pending, "records": n}


# --- verify ------------------------------------------------------------------

def _norm(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).lower()
    s = str(v).strip()
    try:
        f = float(s)
        if f == int(f) and abs(f) < 1e15:
            return str(int(f))
        return repr(f)
    except (ValueError, OverflowError):
        return s


def _table(name):
    schema, table = name.split(".")
    return pgsql.Identifier(schema, table)


def _check(cur, spec, check, batch_id):
    kind = check["check"]
    where = pgsql.SQL(" WHERE " + check["where"]) if check.get("where") else pgsql.SQL("")

    if kind == "row_count":
        cur.execute(pgsql.SQL("SELECT count(*) FROM {}{}").format(_table(check["table"]), where))
        n = cur.fetchone()[0]
        ok = n >= check.get("min", 1) and ("max" not in check or n <= check["max"])
        return ok, {"rows": n}

    if kind == "null_rate":
        col = pgsql.Identifier(check["column"])
        cur.execute(pgsql.SQL("SELECT count(*), count(*) FILTER (WHERE {c} IS NULL OR {c}::text = '') FROM {t}{w}")
                    .format(c=col, t=_table(check["table"]), w=where))
        total, nulls = cur.fetchone()
        rate = nulls / total if total else 0.0
        return rate <= check["max"], {"rows": total, "nulls": nulls, "rate": round(rate, 4)}

    if kind == "sample_match":
        fields = check["fields"]
        size = int(check.get("size", 200))
        rwhere = pgsql.SQL(" AND (" + check["where"] + ")") if check.get("where") else pgsql.SQL("")
        # no query parameters here: `where` is SQL from source.yml and may hold a literal %
        cur.execute(pgsql.SQL("SELECT key, payload FROM raw.source_record WHERE source = {}{} "
                              "ORDER BY random() LIMIT {}").format(
            pgsql.Literal(spec.name), rwhere, pgsql.Literal(size)))
        sample = cur.fetchall()
        if not sample:
            return False, {"sampled": 0, "error": "no landed records to sample"}
        cols = pgsql.SQL(", ").join(pgsql.SQL("{}::text").format(pgsql.Identifier(c)) for c in fields.values())
        # the core row's key: the landing key, or the payload field key_field
        keys = [_norm(dig(p, check["key_field"])) if check.get("key_field") else k for k, p in sample]
        cur.execute(pgsql.SQL("SELECT {k}::text, {cols} FROM {t} WHERE {k}::text = ANY(%s)")
                    .format(k=pgsql.Identifier(check["key_column"]), cols=cols, t=_table(check["table"])),
                    (keys,))
        core = {r[0]: r[1:] for r in cur.fetchall()}
        matched, mismatches = 0, []
        for key, payload in sample:
            if check.get("key_field"):
                key = _norm(dig(payload, check["key_field"]))
            row = core.get(key)
            diffs = []
            if row is None:
                diffs.append("missing in core")
            else:
                for (path, col), value in zip(fields.items(), row):
                    if _norm(dig(payload, path)) != _norm(value):
                        diffs.append(f"{col}: core {value!r} != landed {dig(payload, path)!r}")
            if diffs:
                mismatches.append({"key": key, "diffs": diffs[:3]})
            else:
                matched += 1
        pct = 100.0 * matched / len(sample)
        return pct >= check.get("min_percent", 99), {
            "sampled": len(sample), "matched": matched, "match_percent": round(pct, 2),
            "examples": mismatches[:5]}

    if kind == "landed":
        cur.execute("SELECT fetched, full_fetch FROM raw.source_run WHERE batch_id = %s", (batch_id,))
        row = cur.fetchone()
        fetched, full = (row or (0, False))
        details, ok = {"fetched": fetched}, fetched >= check.get("min", 0)
        if "max_shrink" in check and full:
            cur.execute(f"""SELECT fetched FROM raw.source_run WHERE source = %s AND batch_id < %s
                              AND full_fetch AND status IN {_DONE} ORDER BY batch_id DESC LIMIT 1""",
                        (spec.name, batch_id))
            prev = cur.fetchone()
            if prev and prev[0]:
                details["previous"] = prev[0]
                ok = ok and fetched >= prev[0] * (1 - check["max_shrink"])
        return ok, details

    if kind == "sql":
        cur.execute(check["sql"])
        row = cur.fetchone()
        return bool(row and row[0]), {"result": None if not row else str(row[0])}

    raise ValueError(f"unknown check {kind}")


def verify(folder, batch_id=None):
    """Runs every check; records the results on the batch's raw.source_run
    row and raises ValueError if any check failed."""
    spec = _spec(folder)
    results, failed = [], []
    with pg.connect() as conn, conn.cursor() as cur:
        if batch_id is None:
            cur.execute("SELECT max(batch_id) FROM raw.source_run WHERE source = %s", (spec.name,))
            batch_id = cur.fetchone()[0]
        for i, check in enumerate(spec.verify):
            label = check.get("name") or f"{check['check']}" + (f" {check['table']}" if "table" in check else "") \
                + (f".{check['column']}" if "column" in check else "")
            try:
                ok, details = _check(cur, spec, check, batch_id)
            except Exception as e:     # a broken check is a failed check
                conn.rollback()
                ok, details = False, {"error": str(e)[:500]}
            results.append({"check": label, "ok": ok, **details})
            if not ok:
                failed.append(label)
        cur.execute("""UPDATE raw.source_run SET checks = %s, status = %s,
                           verified_at = CASE WHEN %s THEN now() END,
                           error = CASE WHEN %s THEN NULL ELSE %s END
                       WHERE batch_id = %s""",
                    (json.dumps(results, default=str), "failed" if failed else "verified",
                     not failed, not failed, "verify: " + ", ".join(failed), batch_id))
    summary = {"source": spec.name, "batch_id": batch_id, "checks": results}
    if failed:
        raise ValueError(f"source {spec.name}: checks failed: {', '.join(failed)}; {json.dumps(results, default=str)}")
    return summary


# --- expose ------------------------------------------------------------------

def _column_name(field, taken):
    name = re.sub(r"[^a-z0-9_]+", "_", field.lower()).strip("_") or "field"
    if name[0].isdigit():
        name = "f_" + name
    name = name[:55]
    base, n = name, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


def view_sql(cur, spec, max_columns=150):
    """SQL for views.source__<name>: the landing key, one text column per
    top-level payload field (key fields first, then alphabetical), whether
    the record is still in the source (full mode), and landing times."""
    cur.execute("""SELECT k FROM raw.source_record r, jsonb_object_keys(r.payload) k
                   WHERE r.source = %s GROUP BY k ORDER BY count(*) DESC, k LIMIT %s""",
                (spec.name, max_columns))
    fields = sorted(r[0] for r in cur.fetchall())
    first = [f for f in spec.key if f in fields]
    fields = first + [f for f in fields if f not in first]
    taken = {"key", "in_source", "landed_at", "first_landed_at", "batch_id"}
    cols = [pgsql.SQL("r.key")]
    for f in fields:
        cols.append(pgsql.SQL("r.payload ->> {} AS {}").format(
            pgsql.Literal(f), pgsql.Identifier(_column_name(f, taken))))
    if spec.mode == "full":
        cols.append(pgsql.SQL(
            "r.seen_batch_id >= coalesce((SELECT max(u.batch_id) FROM raw.source_run u "
            "WHERE u.source = r.source AND u.full_fetch AND u.status IN ('landed', 'transformed', 'verified')), 0) "
            "AS in_source"))
    cols += [pgsql.SQL("r.landed_at"), pgsql.SQL("r.first_landed_at"), pgsql.SQL("r.batch_id")]
    return pgsql.SQL("CREATE VIEW views.{v} AS SELECT {cols} FROM raw.source_record r WHERE r.source = {s}").format(
        v=pgsql.Identifier(spec.view_name), cols=pgsql.SQL(", ").join(cols), s=pgsql.Literal(spec.name))


def expose(folder):
    """(Re)creates views.source__<name> (generated, or the source's own
    view SQL) and lets the curator UI role read it."""
    spec = _spec(folder)
    if spec.view is False:
        return {"source": spec.name, "view": None}
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(pgsql.SQL("DROP VIEW IF EXISTS views.{}").format(pgsql.Identifier(spec.view_name)))
        if spec.view is True:
            cur.execute(view_sql(cur, spec))
        else:
            cur.execute(spec.view.read_text(encoding="utf8"))
        comment = f"Source {spec.name} ({spec.owner}): {spec.description} Landed records as fetched; " \
                  f"key = {', '.join(spec.key)}. Defined in dags/sources/{spec.name}/source.yml."
        cur.execute(pgsql.SQL("COMMENT ON VIEW views.{} IS {}").format(
            pgsql.Identifier(spec.view_name), pgsql.Literal(comment)))
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'birddog_ui'")
        if cur.fetchone():
            cur.execute(pgsql.SQL("GRANT SELECT ON views.{} TO birddog_ui").format(
                pgsql.Identifier(spec.view_name)))
    return {"source": spec.name, "view": f"views.{spec.view_name}"}


def run(folder, steps=("land", "transform", "verify", "expose"), full_refresh=False, records=None):
    """All steps in order, as the DAG runs them (for the CLI and tests)."""
    spec = _spec(folder)
    out, batch_id = {}, None
    if "land" in steps:
        out["land"] = land(spec, run_id="cli", full_refresh=full_refresh, records=records)
        batch_id = out["land"]["batch_id"]
    if "transform" in steps:
        out["transform"] = transform(spec, batch_id)
        batch_id = out["transform"]["batch_id"]
    if "verify" in steps:
        out["verify"] = verify(spec, batch_id)
    if "expose" in steps:
        out["expose"] = expose(spec)
    return out
