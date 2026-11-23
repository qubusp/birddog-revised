"""Source kit against a real Postgres: land -> transform -> verify -> expose
on a scratch database that the fixture creates and drops.

Skipped unless SOURCEKIT_ADMIN_DSN points at a Postgres server as a role that
may create databases, e.g. through the SSH tunnel to the server:
    ssh -N -L 15434:127.0.0.1:5432 administrator@<server>
    SOURCEKIT_ADMIN_DSN=postgresql://postgres:<pw>@127.0.0.1:15434/postgres pytest test/test_sources_db.py
With SOURCEKIT_LIVE_LOCATIONS=1 the worked example also runs on the real
NocoDB locations already landed in the live birddog database (read only)."""

import os
import uuid
from pathlib import Path

import pytest
import yaml

psycopg2 = pytest.importorskip("psycopg2")

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "deploy" / "db" / "migrations"
SOURCES = REPO / "dags" / "sources"
ADMIN_DSN = os.environ.get("SOURCEKIT_ADMIN_DSN")
SCRATCH = "birddog_test_sourcekit"

pytestmark = pytest.mark.skipif(not ADMIN_DSN, reason="SOURCEKIT_ADMIN_DSN not set")


def _dsn(dbname):
    from psycopg2.extensions import make_dsn, parse_dsn
    return make_dsn(**{**parse_dsn(ADMIN_DSN), "dbname": dbname})


def _admin(sql):
    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.close()


@pytest.fixture(scope="module")
def scratch_db():
    _admin(f"DROP DATABASE IF EXISTS {SCRATCH}")
    _admin(f"CREATE DATABASE {SCRATCH}")
    try:
        conn = psycopg2.connect(_dsn(SCRATCH))
        conn.autocommit = True
        with conn.cursor() as cur:
            # the base schema plus the kit's migration (009-012 belong to other
            # streams) and 014 (fp_error, which the jewishgen_sftp transform resets)
            for f in sorted(MIGRATIONS.glob("*.sql")):
                if f.name[:3] <= "008" or f.name.startswith(("013", "014")):
                    cur.execute(f.read_text())
            cur.execute((MIGRATIONS / "013_source_kit.sql").read_text())   # re-runnable
        conn.close()
        yield _dsn(SCRATCH)
    finally:
        _admin(f"DROP DATABASE IF EXISTS {SCRATCH} WITH (FORCE)")


@pytest.fixture
def db(scratch_db, monkeypatch):
    monkeypatch.setenv("BIRDDOG_DATABASE_URL", scratch_db)
    conn = psycopg2.connect(scratch_db)
    conn.autocommit = True
    yield conn.cursor()
    conn.close()


def _one(cur, sql, args=None):
    cur.execute(sql, args)
    return cur.fetchone()[0]


def _locations(n=1100):
    return [{"nocodb_id": i, "location_id": f"L{i:05d}", "modern_location_name": f"Town {i}",
             "alternate_names": f"Alt {i}", "c1930_province": "Lwów" if i % 2 else None,
             "c2000_district": None, "region": "Ukraine", "latitude": 49.8 + i / 1e4, "documents": []}
            for i in range(1, n + 1)]


# --- worked example ------------------------------------------------------------

def test_worked_example_end_to_end(db):
    from birddog.sources import runner
    from birddog.sources.spec import load_spec
    spec = load_spec(SOURCES / "nocodb_locations")
    db.execute("DELETE FROM raw.source_record WHERE source = %s", (spec.name,))
    records = _locations()

    out = runner.run(spec, records=records)
    assert out["land"]["fetched"] == 1100 and out["land"]["changed"] == 1100
    assert out["land"]["watermark"] == "1100"
    assert out["transform"]["records"] == 1100
    assert all(c["ok"] for c in out["verify"]["checks"])
    assert _one(db, "SELECT count(*) FROM core.location") == 1100
    assert _one(db, "SELECT value FROM raw.watermark WHERE source = 'source:nocodb_locations'") == "1100"
    assert _one(db, "SELECT status FROM raw.source_run WHERE batch_id = %s",
                (out["land"]["batch_id"],)) == "verified"

    # the generated view: key first, payload fields as text, in_source flag
    db.execute("SELECT * FROM views.source__nocodb_locations WHERE key = 'L00003'")
    cols = [d.name for d in db.description]
    row = dict(zip(cols, db.fetchone()))
    assert cols[:2] == ["key", "location_id"] and row["region"] == "Ukraine" and row["in_source"] is True
    assert "Source nocodb_locations" in _one(
        db, "SELECT obj_description('views.source__nocodb_locations'::regclass)")

    # a curator edits a location in core; an unchanged re-run leaves the edit alone
    db.execute("UPDATE core.location SET modern_location_name = 'Edited' WHERE location_id = 'L00005'")
    again = runner.run(spec, records=records)
    assert again["land"]["changed"] == 0 and again["transform"]["records"] == 0
    assert _one(db, "SELECT modern_location_name FROM core.location WHERE location_id = 'L00005'") == "Edited"

    # one record changes at the source, one disappears: only the change reaches core
    changed = [dict(r) for r in records if r["location_id"] != "L00009"]
    changed[0]["modern_location_name"] = "Renamed"
    third = runner.run(spec, records=changed)
    assert third["land"]["changed"] == 1 and third["transform"]["records"] == 1
    assert _one(db, "SELECT modern_location_name FROM core.location WHERE location_id = 'L00001'") == "Renamed"
    assert _one(db, "SELECT in_source FROM views.source__nocodb_locations WHERE key = 'L00009'") is False
    assert _one(db, "SELECT count(*) FROM core.location WHERE location_id = 'L00009'") == 1   # kept

    # the source suddenly shrinks by a third: the landed check fails the run
    with pytest.raises(ValueError, match="landed"):
        runner.run(spec, records=records[:700])
    assert _one(db, "SELECT status FROM raw.source_run WHERE source = %s ORDER BY batch_id DESC LIMIT 1",
                (spec.name,)) == "failed"


@pytest.mark.skipif(os.environ.get("SOURCEKIT_LIVE_LOCATIONS") != "1",
                    reason="SOURCEKIT_LIVE_LOCATIONS=1 to use the live landed NocoDB locations")
def test_worked_example_on_live_locations(db):
    """The real 1,201 NocoDB locations (read from the live raw.nocodb_record,
    shaped as fetch.py yields them) through the kit; core.location must match
    what the one-off import produced."""
    from birddog.sources import runner
    from birddog.sources.spec import load_spec
    live = psycopg2.connect(_dsn("birddog"))
    live.set_session(readonly=True)
    with live.cursor() as cur:
        cur.execute("""SELECT nocodb_id, fields FROM raw.nocodb_record
                       WHERE table_name = 'locations' ORDER BY nocodb_id""")
        records = [{"nocodb_id": i, **f} for i, f in cur.fetchall()
                   if (f.get("location_id") or "").strip()]
        cur.execute("""SELECT location_id, modern_location_name, alternate_names, c1930_province,
                              c2000_district FROM core.location ORDER BY 1""")
        live_core = cur.fetchall()
    live.close()

    spec = load_spec(SOURCES / "nocodb_locations")
    db.execute("DELETE FROM core.location")
    db.execute("DELETE FROM raw.source_record WHERE source = %s", (spec.name,))
    db.execute("DELETE FROM raw.source_run WHERE source = %s", (spec.name,))
    out = runner.run(spec, records=records)
    assert out["land"]["fetched"] == len(records) >= 1000
    assert all(c["ok"] for c in out["verify"]["checks"]), out["verify"]
    db.execute("""SELECT location_id, modern_location_name, alternate_names, c1930_province,
                         c2000_district FROM core.location ORDER BY 1""")
    assert db.fetchall() == live_core


# --- kit mechanics on a throwaway source ------------------------------------------

def _make_source(tmp_path, name, transform, **overrides):
    folder = tmp_path / name
    folder.mkdir()
    spec = {"name": name, "description": "Test", "owner": "tests",
            "fetch": {"type": "python", "callable": "fetch.py:fetch"}, "key": "id",
            "transform": "transform.sql", "produces": [f"core/{name}"],
            "verify": [{"check": "row_count", "table": f"core.{name}", "min": 1}]}
    spec.update(overrides)
    (folder / "source.yml").write_text(yaml.safe_dump(spec))
    (folder / "transform.sql").write_text(transform)
    # yields ctx.options['rows'] newer than the watermark (an incremental API)
    (folder / "fetch.py").write_text(
        "def fetch(ctx):\n"
        "    for r in ctx.options['rows']:\n"
        "        if ctx.watermark is None or r['updated'] > ctx.watermark:\n"
        "            yield r\n")
    return folder


def _set_rows(folder, rows):
    spec = yaml.safe_load((folder / "source.yml").read_text())
    spec["fetch"]["rows"] = rows
    (folder / "source.yml").write_text(yaml.safe_dump(spec))


def test_incremental_watermark_and_failed_transform_retry(db, tmp_path):
    from birddog.sources import runner
    name = "sk_" + uuid.uuid4().hex[:8]
    db.execute(f"CREATE TABLE core.{name} (id text PRIMARY KEY, title text, n int)")
    folder = _make_source(tmp_path, name, f"""
        INSERT INTO core.{name} (id, title, n)
        SELECT key, payload->>'title', (payload->>'n')::int FROM landed
        ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, n = EXCLUDED.n;""",
        mode="incremental", watermark={"field": "updated", "initial": "2026-01-01"},
        verify=[{"check": "row_count", "table": f"core.{name}", "min": 2},
                {"check": "sample_match", "table": f"core.{name}", "key_column": "id",
                 "fields": {"title": "title", "n": "n"}, "min_percent": 100},
                {"check": "sql", "name": "no negative n", "sql": f"SELECT count(*) = 0 FROM core.{name} WHERE n < 0"}],
        view=False)
    try:
        rows = [{"id": "a", "title": "A", "n": 1, "updated": "2026-02-01"},
                {"id": "b", "title": "B", "n": 2, "updated": "2026-03-01"},
                {"id": "old", "title": "Old", "n": 0, "updated": "2025-12-01"}]   # before initial
        _set_rows(folder, rows)
        out = runner.run(folder)
        assert out["land"]["fetched"] == 2 and out["land"]["watermark"] == "2026-03-01"
        assert _one(db, f"SELECT count(*) FROM core.{name}") == 2

        # nothing new: nothing fetched, watermark unchanged
        out = runner.run(folder)
        assert out["land"]["fetched"] == 0 and out["land"]["watermark"] is None
        assert _one(db, "SELECT value FROM raw.watermark WHERE source = %s", (f"source:{name}",)) == "2026-03-01"

        # a batch whose transform fails stays pending and is picked up next time
        rows.append({"id": "c", "title": "C", "n": "not a number", "updated": "2026-04-01"})
        _set_rows(folder, rows)
        with pytest.raises(Exception):
            runner.run(folder)
        assert _one(db, f"SELECT count(*) FROM core.{name}") == 2
        rows.append({"id": "c", "title": "C", "n": 3, "updated": "2026-05-01"})
        rows.append({"id": "d", "title": "D", "n": 4, "updated": "2026-05-02"})
        _set_rows(folder, rows[:3] + rows[4:])
        out = runner.run(folder)
        assert len(out["transform"]["batches"]) == 2       # the failed batch and this one
        assert _one(db, f"SELECT n FROM core.{name} WHERE id = 'c'") == 3
        assert all(c["ok"] for c in out["verify"]["checks"])

        # full refresh ignores the watermark (and the initial one): all five, only 'old' is new
        out = runner.run(folder, full_refresh=True)
        assert out["land"]["fetched"] == 5 and out["land"]["changed"] == 1

        # a check that fails is recorded on the run and fails it
        db.execute(f"UPDATE core.{name} SET n = -1 WHERE id = 'a'")
        with pytest.raises(ValueError, match="no negative n"):
            runner.verify(folder)
        checks = _one(db, "SELECT checks FROM raw.source_run WHERE source = %s ORDER BY batch_id DESC LIMIT 1",
                      (name,))
        assert {c["check"]: c["ok"] for c in checks}["no negative n"] is False
    finally:
        db.execute(f"DROP TABLE IF EXISTS core.{name}")


def test_fetcher_error_marks_run_failed(db, tmp_path):
    from birddog.sources import runner
    name = "sk_" + uuid.uuid4().hex[:8]
    folder = _make_source(tmp_path, name, "SELECT 1;")
    (folder / "fetch.py").write_text("def fetch(ctx):\n    yield {'id': 1}\n    raise IOError('server went away')\n")
    with pytest.raises(IOError):
        runner.land(folder)
    db.execute("SELECT status, error FROM raw.source_run WHERE source = %s", (name,))
    status, error = db.fetchone()
    assert status == "failed" and "server went away" in error


def test_csv_source_end_to_end(db, tmp_path):
    """A built-in fetcher with no Python at all: a CSV file in the folder."""
    from birddog.sources import runner
    name = "sk_" + uuid.uuid4().hex[:8]
    db.execute(f"CREATE TABLE core.{name} (code text PRIMARY KEY, label text)")
    folder = _make_source(tmp_path, name, f"""
        INSERT INTO core.{name} SELECT key, payload->>'Label' FROM landed
        ON CONFLICT (code) DO UPDATE SET label = EXCLUDED.label;""",
        fetch={"type": "csv", "path": "items.csv"}, key="Code",
        verify=[{"check": "null_rate", "table": f"core.{name}", "column": "label", "max": 0.5},
                {"check": "sample_match", "table": f"core.{name}", "key_column": "code",
                 "fields": {"Label": "label"}}])
    (folder / "items.csv").write_text("Code,Label\nF1,Fond 1\nF2,\nF3,Fond 3\n")
    try:
        out = runner.run(folder)
        assert out["land"]["fetched"] == 3
        db.execute(f"SELECT * FROM views.source__{name} ORDER BY key")
        assert [d.name for d in db.description][:3] == ["key", "code", "label"]
        assert db.fetchall()[1][:3] == ("F2", "F2", "")
    finally:
        db.execute(f"DROP VIEW IF EXISTS views.source__{name}")
        db.execute(f"DROP TABLE IF EXISTS core.{name}")


def test_sftp_example_transform(db):
    """The disabled jewishgen_sftp source's transform, on listing records as
    the sftp_listing fetcher shapes them."""
    from birddog.sources import runner
    from birddog.sources.spec import load_spec
    spec = load_spec(SOURCES / "jewishgen_sftp")
    db.execute("DELETE FROM core.ftp_entry")
    db.execute("""INSERT INTO core.ftp_entry (path, filename, folder, suffix, type, size, mtime, depth,
                                               content_fp, match_checked, fp_error) VALUES
        ('/a/same.pdf', 'same.pdf', '/a', 'pdf', 'file', 10, '2026-01-01T00:00:00Z', 2, 'fp1', true, 'e1'),
        ('/a/grew.pdf', 'grew.pdf', '/a', 'pdf', 'file', 10, '2026-01-01T00:00:00Z', 2, 'fp2', true, 'e2')""")

    def rec(path, kind, size, mtime):
        folder, _, fname = path.rpartition("/")
        return {"path": path, "filename": fname, "folder": folder or "/", "suffix": "" if kind == "dir" else "pdf",
                "type": kind, "size": size, "mtime": mtime, "depth": path.count("/")}

    listing = [rec("/a", "dir", None, "2026-02-01T00:00:00+00:00"),
               rec("/a/same.pdf", "file", 10, "2026-01-01T00:00:00+00:00"),
               rec("/a/grew.pdf", "file", 99, "2026-02-01T00:00:00+00:00"),
               rec("/a/new.pdf", "file", 5, "2026-02-02T00:00:00+00:00")]
    runner.land(spec, records=listing)
    runner.transform(spec)
    db.execute("SELECT path, size, content_fp, match_checked, fp_error FROM core.ftp_entry ORDER BY path")
    assert db.fetchall() == [("/a", None, None, False, None),
                             ("/a/grew.pdf", 99, None, False, None),
                             ("/a/new.pdf", 5, None, False, None),
                             ("/a/same.pdf", 10, "fp1", True, "e1")]
