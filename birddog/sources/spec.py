"""source.yml: loading and validation.

A source is one folder, dags/sources/<name>/, holding source.yml plus the
files it names (transform SQL, optional fetch.py / view.sql). load_spec()
reads and checks it and returns a Spec; every problem is reported at once
(SpecError lists them all), so a broken source.yml is fixed in one pass.

Nothing here imports Airflow or touches the database, so validation runs
anywhere (tests, the CLI, the DAG loader at parse time)."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SPEC_FILE = "source.yml"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,50}$")
TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
COLUMN_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
ASSET_PREFIX = "postgres://postgres:5432/birddog/"

FETCH_TYPES = ("python", "http_json", "csv", "xlsx", "gsheet", "sftp_listing",
               "ftp_listing", "html_listing")
MODES = ("full", "incremental")
CHECKS = {
    # check -> (required keys, optional keys)
    "row_count": ({"table"}, {"where", "min", "max"}),
    "null_rate": ({"table", "column", "max"}, {"where"}),
    "sample_match": ({"table", "key_column", "fields"}, {"size", "min_percent", "where", "key_field"}),
    "landed": (set(), {"min", "max_shrink"}),
    "sql": ({"sql"}, {"name"}),
}
TOP_KEYS = {"name", "description", "owner", "notes", "enabled", "schedule", "tags",
            "fetch", "key", "mode", "watermark", "transform", "verify", "produces",
            "view", "retries", "retry_delay_sec", "pool", "max_active_runs", "start_date"}


class SpecError(ValueError):
    def __init__(self, path, problems):
        self.path = str(path)
        self.problems = list(problems)
        super().__init__(f"{self.path}: " + "; ".join(self.problems))


@dataclass
class Spec:
    name: str
    folder: Path
    description: str
    owner: str
    notes: str
    enabled: bool
    schedule: object            # None | cron str | {"cron": str|None, "assets": [uri]}
    tags: list
    fetch: dict                 # {"type": ..., plus type options}
    key: list                   # payload field paths (dotted) that make up the key
    mode: str                   # full | incremental
    watermark: dict | None      # {"field": path, "initial": str} or None
    transform: Path
    verify: list
    produces: list              # asset URIs
    view: object                # False | True | Path (custom view SQL)
    retries: int = 2
    retry_delay_sec: int = 300
    pool: str = "default_pool"
    max_active_runs: int = 1
    start_date: str = "2026-11-01"
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def dag_id(self):
        return f"source_{self.name}"

    @property
    def watermark_name(self):
        return f"source:{self.name}"

    @property
    def view_name(self):
        return f"source__{self.name}"

    @property
    def landing_asset(self):
        return f"{ASSET_PREFIX}raw/source_record/{self.name}"


def asset_uri(value):
    """'core/location' or 'core.location' -> the full postgres:// asset URI
    the other DAGs use; full URIs pass through."""
    value = str(value).strip()
    if "://" in value:
        return value
    return ASSET_PREFIX + value.replace(".", "/")


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def load_spec(folder):
    """Reads and validates <folder>/source.yml. Raises SpecError."""
    folder = Path(folder).resolve()
    path = folder / SPEC_FILE
    try:
        with open(path, encoding="utf8") as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raise SpecError(path, ["file not found"])
    except yaml.YAMLError as e:
        raise SpecError(path, [f"not valid YAML: {e}"])
    if not isinstance(raw, dict):
        raise SpecError(path, ["top level must be a mapping"])
    return validate(raw, folder)


def validate(raw, folder):
    folder = Path(folder)
    path = folder / SPEC_FILE
    p = []   # problems

    unknown = set(raw) - TOP_KEYS
    if unknown:
        p.append(f"unknown keys: {', '.join(sorted(unknown))}")

    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        p.append("name: required, lowercase letters/digits/underscores, starting with a letter")
    elif name != folder.name:
        p.append(f"name: {name!r} must match the folder name {folder.name!r}")

    for k in ("description", "owner"):
        if not isinstance(raw.get(k), str) or not raw[k].strip():
            p.append(f"{k}: required (a short text)")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        p.append("enabled: must be true or false")

    schedule = _check_schedule(raw.get("schedule"), p)

    tags = _as_list(raw.get("tags"))
    if not all(isinstance(t, str) for t in tags):
        p.append("tags: must be a list of strings")

    fetch = raw.get("fetch")
    if not isinstance(fetch, dict) or fetch.get("type") not in FETCH_TYPES:
        p.append(f"fetch.type: required, one of {', '.join(FETCH_TYPES)}")
        fetch = fetch if isinstance(fetch, dict) else {}
    else:
        _check_fetch(fetch, folder, p)

    key = _as_list(raw.get("key"))
    if not key or not all(isinstance(k, str) and k for k in key):
        p.append("key: required, a payload field (dotted path) or a list of them")

    mode = raw.get("mode", "full")
    if mode not in MODES:
        p.append(f"mode: one of {', '.join(MODES)}")

    watermark = raw.get("watermark")
    if watermark is not None:
        if not isinstance(watermark, dict) or not isinstance(watermark.get("field"), str):
            p.append("watermark: a mapping with `field` (payload field whose maximum is kept)")
            watermark = None
        else:
            watermark = {"field": watermark["field"],
                         "initial": None if watermark.get("initial") is None
                         else str(watermark["initial"])}

    transform = raw.get("transform")
    transform_path = folder / str(transform) if transform else None
    if not transform:
        p.append("transform: required, the SQL file that turns landed records into core rows")
    elif not transform_path.is_file():
        p.append(f"transform: file {transform} not found in {folder.name}/")

    verify = raw.get("verify") or []
    if not isinstance(verify, list):
        p.append("verify: must be a list of checks")
        verify = []
    for i, check in enumerate(verify):
        _check_verify(i, check, p)
    if not verify:
        p.append("verify: at least one check is required (land -> transform -> verify)")

    produces = [asset_uri(a) for a in _as_list(raw.get("produces"))]
    if not produces:
        p.append("produces: list the core tables (assets) this source writes, e.g. core/location")

    view = raw.get("view", True)
    if isinstance(view, str):
        if not (folder / view).is_file():
            p.append(f"view: file {view} not found in {folder.name}/")
        view = folder / view
    elif not isinstance(view, bool):
        p.append("view: true (generated), false, or a SQL file name")

    ints = {}
    for k, default in (("retries", 2), ("retry_delay_sec", 300), ("max_active_runs", 1)):
        v = raw.get(k, default)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            p.append(f"{k}: must be a non-negative integer")
            v = default
        ints[k] = v

    pool = raw.get("pool", "default_pool")
    if not isinstance(pool, str) or not pool:
        p.append("pool: must be a pool name")

    if p:
        raise SpecError(path, p)

    return Spec(
        name=name, folder=folder, description=raw["description"].strip(),
        owner=raw["owner"].strip(), notes=str(raw.get("notes") or "").strip(),
        enabled=enabled, schedule=schedule, tags=tags, fetch=fetch, key=key, mode=mode,
        watermark=watermark, transform=transform_path, verify=verify, produces=produces,
        view=view, pool=pool, start_date=str(raw.get("start_date", "2026-11-01")),
        raw=raw, **ints)


def _check_schedule(value, p):
    if value is None or value == "@once":
        return value
    if isinstance(value, str):
        if len(value.split()) != 5 and not value.startswith("@"):
            p.append(f"schedule: {value!r} is not a 5-field cron expression or @preset")
        return value
    if isinstance(value, dict):
        unknown = set(value) - {"cron", "assets"}
        if unknown:
            p.append(f"schedule: unknown keys {', '.join(sorted(unknown))}")
        assets = [asset_uri(a) for a in _as_list(value.get("assets"))]
        if not assets:
            p.append("schedule.assets: list at least one asset (or use a plain cron string)")
        cron = value.get("cron")
        if cron is not None and (not isinstance(cron, str) or len(cron.split()) != 5):
            p.append("schedule.cron: must be a 5-field cron expression")
        return {"cron": cron, "assets": assets}
    p.append("schedule: null, a cron string, or {cron: ..., assets: [...]}")
    return None


_FETCH_REQUIRED = {
    "python": ("callable",),
    "http_json": ("url",),
    "gsheet": ("sheet_id",),
    "sftp_listing": ("host", "user", "password_env"),
    "ftp_listing": ("host",),
    "html_listing": ("url", "item_selector", "fields"),
}


def _check_fetch(fetch, folder, p):
    kind = fetch["type"]
    for k in _FETCH_REQUIRED.get(kind, ()):
        if not fetch.get(k):
            p.append(f"fetch.{k}: required for type {kind}")
    if kind in ("csv", "xlsx") and not (fetch.get("url") or fetch.get("path")):
        p.append(f"fetch: type {kind} needs `url` or `path`")
    if kind in ("csv", "xlsx") and fetch.get("path") and "://" not in str(fetch["path"]):
        if not (folder / fetch["path"]).is_file() and not Path(fetch["path"]).is_absolute():
            p.append(f"fetch.path: file {fetch['path']} not found in {folder.name}/")
    if kind == "python" and fetch.get("callable"):
        target = str(fetch["callable"])
        module, _, func = target.partition(":")
        if not func:
            p.append("fetch.callable: 'fetch.py:function' or 'package.module:function'")
        elif module.endswith(".py") and not (folder / module).is_file():
            p.append(f"fetch.callable: file {module} not found in {folder.name}/")
    for k, v in (fetch.get("headers") or {}).items():
        if not isinstance(v, str):
            p.append(f"fetch.headers.{k}: must be a string (use 'env:VAR' for secrets)")
    paging = fetch.get("paging")
    if paging is not None:
        if not isinstance(paging, dict) or paging.get("type") not in ("page", "offset", "cursor", "next_url"):
            p.append("fetch.paging.type: one of page, offset, cursor, next_url")
    # secrets never go in source.yml: name the environment variable instead
    for k in ("password", "token", "api_key", "secret"):
        if k in fetch:
            p.append(f"fetch.{k}: don't put secrets in source.yml; use {k}_env: VAR_NAME")


def _check_verify(i, check, p):
    where = f"verify[{i}]"
    if not isinstance(check, dict) or check.get("check") not in CHECKS:
        p.append(f"{where}.check: one of {', '.join(CHECKS)}")
        return
    kind = check["check"]
    required, optional = CHECKS[kind]
    missing = required - set(check)
    if missing:
        p.append(f"{where} ({kind}): missing {', '.join(sorted(missing))}")
    unknown = set(check) - required - optional - {"check"}
    if unknown:
        p.append(f"{where} ({kind}): unknown keys {', '.join(sorted(unknown))}")
    if "table" in check and not TABLE_RE.match(str(check["table"])):
        p.append(f"{where}.table: schema.table, lowercase (e.g. core.location)")
    for k in ("column", "key_column"):
        if k in check and not COLUMN_RE.match(str(check[k])):
            p.append(f"{where}.{k}: a plain lowercase column name")
    if kind == "sample_match":
        fields = check.get("fields")
        if not isinstance(fields, dict) or not fields or \
                not all(COLUMN_RE.match(str(c)) for c in fields.values()):
            p.append(f"{where}.fields: mapping of payload field -> core column")
    for k in ("max", "min_percent", "max_shrink"):
        if k in check and not isinstance(check[k], (int, float)):
            p.append(f"{where}.{k}: must be a number")


def discover(sources_dir):
    """Folders under sources_dir that hold a source.yml, sorted by name.
    Folders starting with '_' or '.' (templates, scratch) are skipped."""
    root = Path(sources_dir)
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir()
                  if d.is_dir() and not d.name.startswith(("_", ".")) and (d / SPEC_FILE).is_file())


def default_sources_dir():
    """dags/sources next to the birddog package (repo checkout), unless
    BIRDDOG_SOURCES_DIR says otherwise (Airflow: /opt/airflow/dags/sources)."""
    import os
    env = os.environ.get("BIRDDOG_SOURCES_DIR")
    if env:
        return Path(env)
    for candidate in (Path(__file__).resolve().parents[2] / "dags" / "sources",
                      Path("/opt/airflow/dags/sources")):
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parents[2] / "dags" / "sources"
