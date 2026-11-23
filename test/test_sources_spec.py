"""Source kit: source.yml validation, scaffolding and the built-in fetchers
(no database, no network)."""

import io
import json
from pathlib import Path

import pytest
import yaml

from birddog.sources import fetchers
from birddog.sources.fetchers import FetchContext
from birddog.sources.scaffold import new_source
from birddog.sources.spec import FETCH_TYPES, SpecError, asset_uri, discover, load_spec, validate

REPO = Path(__file__).resolve().parents[1]
SOURCES = REPO / "dags" / "sources"


def _write(folder, spec, files=("transform.sql",)):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "source.yml").write_text(yaml.safe_dump(spec), encoding="utf8")
    for f in files:
        (folder / f).write_text("SELECT 1;", encoding="utf8")
    return folder


def _minimal(name="demo"):
    return {
        "name": name, "description": "Demo", "owner": "me",
        "fetch": {"type": "csv", "url": "https://example.org/x.csv"},
        "key": "id", "transform": "transform.sql",
        "verify": [{"check": "row_count", "table": "core.page", "min": 1}],
        "produces": ["core/page"],
    }


# --- the shipped sources -------------------------------------------------------

def test_shipped_sources_validate():
    folders = discover(SOURCES)
    assert {f.name for f in folders} >= {"nocodb_locations", "jewishgen_sftp"}
    for folder in folders:
        load_spec(folder)   # raises on any problem


def test_worked_example_contents():
    spec = load_spec(SOURCES / "nocodb_locations")
    assert spec.enabled and spec.schedule is None
    assert spec.key == ["location_id"] and spec.mode == "full"
    assert spec.produces == ["postgres://postgres:5432/birddog/core/location"]
    assert spec.watermark == {"field": "nocodb_id", "initial": None}
    assert spec.dag_id == "source_nocodb_locations"
    assert {c["check"] for c in spec.verify} == {"landed", "row_count", "null_rate", "sample_match"}


def test_sftp_example_is_disabled():
    assert load_spec(SOURCES / "jewishgen_sftp").enabled is False


# --- validation ----------------------------------------------------------------

def test_minimal_spec_defaults(tmp_path):
    spec = load_spec(_write(tmp_path / "demo", _minimal()))
    assert spec.mode == "full" and spec.view is True and spec.pool == "default_pool"
    assert spec.retries == 2 and spec.enabled is True
    assert spec.transform == (tmp_path / "demo" / "transform.sql").resolve()


def test_all_problems_reported_at_once(tmp_path):
    bad = {"name": "Other", "fetch": {"type": "ftp"}, "verify": [{"check": "nope"}],
           "schedule": "every day", "surprise": 1, "retries": -1}
    with pytest.raises(SpecError) as e:
        load_spec(_write(tmp_path / "demo", bad, files=()))
    text = " ".join(e.value.problems)
    for expected in ("unknown keys: surprise", "name:", "description:", "owner:", "fetch.type",
                     "key:", "transform:", "verify[0].check", "produces:", "schedule:", "retries:"):
        assert expected in text, expected


def test_name_must_match_folder(tmp_path):
    with pytest.raises(SpecError, match="must match the folder"):
        load_spec(_write(tmp_path / "other", _minimal("demo")))


def test_secrets_rejected(tmp_path):
    spec = _minimal()
    spec["fetch"] = {"type": "sftp_listing", "host": "h", "user": "u", "password": "hunter2"}
    with pytest.raises(SpecError) as e:
        load_spec(_write(tmp_path / "demo", spec))
    assert any("password_env" in p for p in e.value.problems)
    assert any("don't put secrets" in p for p in e.value.problems)


@pytest.mark.parametrize("check,problem", [
    ({"check": "null_rate", "table": "core.page", "column": "title"}, "missing max"),
    ({"check": "row_count", "table": "Core.Page"}, "schema.table"),
    ({"check": "sample_match", "table": "core.page", "key_column": "url", "fields": {}}, "fields"),
    ({"check": "row_count", "table": "core.page", "bogus": 1}, "unknown keys bogus"),
])
def test_bad_checks(tmp_path, check, problem):
    spec = _minimal()
    spec["verify"] = [check]
    with pytest.raises(SpecError) as e:
        load_spec(_write(tmp_path / "demo", spec))
    assert any(problem in p for p in e.value.problems), e.value.problems


def test_schedule_forms(tmp_path):
    folder = tmp_path / "demo"
    spec = _minimal()
    spec["schedule"] = "0 4 * * *"
    assert load_spec(_write(folder, spec)).schedule == "0 4 * * *"
    spec["schedule"] = {"cron": "15 * * * *", "assets": ["core/page"]}
    assert load_spec(_write(folder, spec)).schedule == {
        "cron": "15 * * * *", "assets": ["postgres://postgres:5432/birddog/core/page"]}
    spec["schedule"] = {"assets": ["core.document"]}
    assert load_spec(_write(folder, spec)).schedule["cron"] is None


def test_python_fetch_file_must_exist(tmp_path):
    spec = _minimal()
    spec["fetch"] = {"type": "python", "callable": "fetch.py:fetch"}
    with pytest.raises(SpecError, match="fetch.py not found"):
        load_spec(_write(tmp_path / "demo", spec))


def test_asset_uri():
    assert asset_uri("core/location") == "postgres://postgres:5432/birddog/core/location"
    assert asset_uri("core.location") == "postgres://postgres:5432/birddog/core/location"
    assert asset_uri("s3://bucket/x") == "s3://bucket/x"


# --- scaffolding ---------------------------------------------------------------

@pytest.mark.parametrize("kind", FETCH_TYPES)
def test_scaffold_every_type(tmp_path, kind):
    folder, files = new_source("demo_src", kind, tmp_path)
    assert "source.yml" in files and "transform.sql" in files
    assert ("fetch.py" in files) == (kind == "python")
    # the template parses and only the deliberate TODO (core.TODO table) is left
    with pytest.raises(SpecError) as e:
        load_spec(folder)
    assert all("core.location" in p or "items.xlsx not found" in p for p in e.value.problems), e.value.problems
    text = (folder / "source.yml").read_text().replace("core.TODO", "core.page")
    (folder / "source.yml").write_text(text)
    if kind == "xlsx":
        (folder / "items.xlsx").write_bytes(b"")
    spec = load_spec(folder)
    assert spec.fetch["type"] == kind


def test_scaffold_refuses_to_overwrite(tmp_path):
    new_source("demo_src", "csv", tmp_path)
    with pytest.raises(FileExistsError):
        new_source("demo_src", "csv", tmp_path)
    with pytest.raises(ValueError):
        new_source("Bad-Name", "csv", tmp_path)


def test_cli_new_and_validate(tmp_path, capsys):
    from birddog.sources.__main__ import main
    assert main(["--dir", str(tmp_path), "new", "cli_src", "--type", "gsheet"]) == 0
    assert main(["--dir", str(tmp_path), "validate"]) == 1     # TODO table left in
    assert "INVALID" in capsys.readouterr().out


# --- fetchers ------------------------------------------------------------------

def _ctx(options, folder=Path("."), watermark=None):
    return FetchContext("demo", Path(folder), options, watermark)


def test_csv_from_local_file(tmp_path):
    (tmp_path / "x.csv").write_text("﻿id,name,,name\n1,a,x,b\n\n2,c,,d\n", encoding="utf8")
    rows = list(fetchers.csv_file(_ctx({"type": "csv", "path": "x.csv"}, tmp_path)))
    assert rows == [{"id": "1", "name": "a", "col_3": "x", "name_2": "b"},
                    {"id": "2", "name": "c", "col_3": "", "name_2": "d"}]


def test_xlsx_from_local_file(tmp_path):
    import datetime
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["title row"])
    ws.append(["id", "when", "n"])
    ws.append([1, datetime.date(2026, 1, 2), 2.5])
    wb.save(tmp_path / "x.xlsx")
    rows = list(fetchers.xlsx_file(_ctx({"type": "xlsx", "path": "x.xlsx", "skip_rows": 1}, tmp_path)))
    assert rows == [{"id": 1, "when": "2026-01-02T00:00:00", "n": 2.5}]


class _Resp:
    def __init__(self, data=None, text="", url="https://example.org/"):
        self._data, self.text, self.url = data, text, url
        self.status_code, self.headers = 200, {}
        self.content = text.encode()

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, pages):
        self.pages, self.calls, self.headers = list(pages), [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self.pages.pop(0)


def test_http_json_page_paging(monkeypatch):
    session = _Session([_Resp({"data": {"items": [{"id": 1}, {"id": 2}]}}),
                        _Resp({"data": {"items": [{"id": 3}]}})])
    monkeypatch.setattr(fetchers, "_session", lambda ctx: session)
    opts = {"type": "http_json", "url": "https://api/x", "records_path": "data.items",
            "watermark_param": "since",
            "paging": {"type": "page", "param": "p", "size_param": "n", "size": 2}}
    out = list(fetchers.http_json(_ctx(opts, watermark="2026-01-01")))
    assert [r["id"] for r in out] == [1, 2, 3]
    assert session.calls[0][1] == {"since": "2026-01-01", "n": 2, "p": 1}
    assert session.calls[1][1]["p"] == 2


def test_http_json_cursor_and_next_url(monkeypatch):
    session = _Session([_Resp({"items": [{"id": 1}], "next": "c2"}), _Resp({"items": [{"id": 2}], "next": None})])
    monkeypatch.setattr(fetchers, "_session", lambda ctx: session)
    opts = {"type": "http_json", "url": "u", "records_path": "items",
            "paging": {"type": "cursor", "param": "cursor", "cursor_path": "next"}}
    assert [r["id"] for r in fetchers.http_json(_ctx(opts))] == [1, 2]
    assert session.calls[1][1] == {"cursor": "c2"}

    session = _Session([_Resp([{"id": 1}]), ])
    monkeypatch.setattr(fetchers, "_session", lambda ctx: session)
    assert list(fetchers.http_json(_ctx({"type": "http_json", "url": "u"}))) == [{"id": 1}]


def test_html_listing(monkeypatch):
    page1 = """<table><tr class=item><td class=t><a href="/f/1">Fond 1</a></td><td class=y>1920</td></tr>
               <tr class=item><td class=t><a href="f/2">Fond 2</a></td></tr></table>
               <a class=next href="?page=2">next</a>"""
    page2 = """<table><tr class=item><td class=t><a href="/f/3">Fond 3</a></td><td class=y>1931</td></tr></table>"""
    session = _Session([_Resp(text=page1, url="https://a.org/list"), _Resp(text=page2, url="https://a.org/list?page=2")])
    monkeypatch.setattr(fetchers, "_session", lambda ctx: session)
    opts = {"type": "html_listing", "url": "https://a.org/list", "item_selector": "tr.item",
            "fields": {"title": "td.t", "url": {"selector": "td.t a", "attr": "href"}, "year": "td.y"},
            "next_selector": "a.next", "delay_sec": 0}
    rows = list(fetchers.html_listing(_ctx(opts)))
    assert rows == [
        {"title": "Fond 1", "url": "https://a.org/f/1", "year": "1920"},
        {"title": "Fond 2", "url": "https://a.org/f/2", "year": None},
        {"title": "Fond 3", "url": "https://a.org/f/3", "year": "1931"},
    ]


def test_listing_walk():
    tree = {
        "/": [("a", True, 0, 0), ("x.pdf", False, 10, 1_700_000_000), ("y.txt", False, 1, 0), (".hid", True, 0, 0)],
        "/a": [("b", True, 0, 0), ("z.PDF", False, 5, 1_700_000_000)],
        "/a/b": [("deep.pdf", False, 1, 0)],
    }
    ctx = _ctx({"suffixes": ["pdf"], "max_depth": 2})
    rows = list(fetchers._walk(ctx, lambda f: tree[f]))
    assert [r["path"] for r in rows] == ["/a", "/x.pdf", "/a/b", "/a/z.PDF"]
    z = rows[-1]
    assert z == {"path": "/a/z.PDF", "filename": "z.PDF", "folder": "/a", "suffix": "pdf",
                 "type": "file", "size": 5, "mtime": "2023-11-14T22:13:20+00:00", "depth": 2}


def test_python_callable_from_source_folder(tmp_path):
    (tmp_path / "fetch.py").write_text("def fetch(ctx):\n    yield {'id': ctx.options['n']}\n")
    fn = fetchers.resolve({"type": "python", "callable": "fetch.py:fetch"}, tmp_path)
    assert list(fn(_ctx({"n": 7}))) == [{"id": 7}]


def test_env_secrets(monkeypatch):
    ctx = _ctx({"password_env": "SK_TEST_PW", "headers": {"X-Key": "env:SK_TEST_KEY"}})
    with pytest.raises(ValueError, match="SK_TEST_PW"):
        ctx.env("password_env")
    monkeypatch.setenv("SK_TEST_PW", "pw")
    monkeypatch.setenv("SK_TEST_KEY", "k")
    assert ctx.env("password_env") == "pw"
    assert fetchers._headers(ctx)["X-Key"] == "k"


def test_dig_and_keys():
    from birddog.sources.runner import _wm_max, make_key
    rec = {"a": {"b": [{"c": 5}]}, "id": 3, "site": "x"}
    assert fetchers.dig(rec, "a.b.0.c") == 5 and fetchers.dig(rec, "a.z") is None
    assert make_key(rec, ["site", "id"]) == "x|3"
    with pytest.raises(ValueError, match="key field"):
        make_key(rec, ["missing"])
    assert _wm_max("9", "10") == "10" and _wm_max("2026-01-02", "2026-01-10") == "2026-01-10"
    assert _wm_max(None, 3) == 3


def test_canonical_json_is_order_independent():
    assert fetchers.canonical_json({"b": 1, "a": "й"}) == fetchers.canonical_json({"a": "й", "b": 1})
    assert json.loads(fetchers.canonical_json({"d": io.__name__})) == {"d": "io"}
