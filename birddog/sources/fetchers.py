"""Fetchers: where a source's records come from.

Every fetcher is a function fetch(ctx) -> iterable of dicts (one dict per
record, JSON-able). The kit lands what it yields unchanged; turning it into
core rows is the transform SQL's job, so fetchers don't clean or rename.

Built-in types (source.yml `fetch.type`):
  http_json     JSON API, optionally paged (page / offset / cursor / next_url)
  csv           CSV file: local path (in the source folder) or URL
  xlsx          Excel sheet: local path or URL
  gsheet        Google Sheet, through its CSV export URL (sheet must be link-shared)
  sftp_listing  recursive listing of an SFTP server (paths, sizes, mtimes)
  ftp_listing   the same over plain FTP (MLSD)
  html_listing  rows scraped from an HTML page with CSS selectors, optionally paged
  python        your own fetch(ctx) function: 'fetch.py:fetch' in the source
                folder, or 'package.module:function'

Secrets are never written in source.yml: options ending in _env name an
environment variable, and header values may be 'env:VAR'."""

import csv
import importlib
import importlib.util
import io
import json
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

_logger = logging.getLogger(__name__)
_TIMEOUT = 60
_USER_AGENT = "birddog-source-kit (https://github.com/jbrandt130/birddog)"


@dataclass
class FetchContext:
    """What a fetcher gets. `options` is the fetch: block of source.yml.
    `watermark` is the stored high-water mark (None on the first run or a
    full refresh); a fetcher may call set_watermark() to choose the next one
    itself instead of the kit taking the maximum of `watermark.field`."""
    name: str
    folder: Path
    options: dict
    watermark: str | None = None
    full_refresh: bool = False
    new_watermark: str | None = field(default=None, init=False)
    log: logging.Logger = field(default=_logger, repr=False)

    def set_watermark(self, value):
        self.new_watermark = None if value is None else str(value)

    def env(self, key):
        """Value of the environment variable named by option `key` (e.g.
        password_env: FTP_PASSWORD). Raises if the option or variable is missing."""
        var = self.options.get(key)
        if not var:
            raise ValueError(f"{self.name}: fetch.{key} is not set")
        value = os.environ.get(var)
        if value is None:
            raise ValueError(f"{self.name}: environment variable {var} (fetch.{key}) is not set")
        return value


def resolve(options, folder):
    """The fetch function for a fetch: block."""
    kind = options["type"]
    if kind == "python":
        return load_callable(options["callable"], folder)
    return BUILTIN[kind]


def load_callable(target, folder):
    """'fetch.py:fetch' (a file in the source folder) or 'pkg.mod:func'."""
    module_name, _, func = target.partition(":")
    if module_name.endswith(".py"):
        path = Path(folder) / module_name
        spec = importlib.util.spec_from_file_location(
            f"birddog_source_{Path(folder).name}_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(module_name)
    return getattr(module, func)


# --- helpers -----------------------------------------------------------------

def dig(obj, path):
    """obj['a']['b'] for path 'a.b'; list indexes allowed ('items.0.id').
    Returns None when anything along the way is missing."""
    if path in (None, ""):
        return obj
    for part in str(path).split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.lstrip("-").isdigit():
            i = int(part)
            obj = obj[i] if -len(obj) <= i < len(obj) else None
        else:
            return None
        if obj is None:
            return None
    return obj


def _headers(ctx):
    headers = {"User-Agent": _USER_AGENT}
    for k, v in (ctx.options.get("headers") or {}).items():
        if isinstance(v, str) and v.startswith("env:"):
            var = v[4:]
            if var not in os.environ:
                raise ValueError(f"{ctx.name}: header {k} needs environment variable {var}")
            v = os.environ[var]
        headers[k] = v
    return headers


def _session(ctx):
    import requests
    s = requests.Session()
    s.headers.update(_headers(ctx))
    return s


def _get(session, url, params=None, delay=0.0, retries=4):
    """GET with backoff on 429/5xx (honours Retry-After)."""
    for attempt in range(retries + 1):
        if delay:
            time.sleep(delay)
        r = session.get(url, params=params, timeout=_TIMEOUT)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            wait = float(r.headers.get("Retry-After") or 2 ** (attempt + 1))
            _logger.warning(f"GET {url}: {r.status_code}, retrying in {wait:.0f}s")
            time.sleep(min(wait, 120))
            continue
        r.raise_for_status()
        return r
    raise RuntimeError("unreachable")


def _read_bytes(ctx):
    """Bytes of options.path (relative to the source folder) or options.url."""
    opts = ctx.options
    if opts.get("path") and "://" not in str(opts["path"]):
        return (ctx.folder / opts["path"]).read_bytes()
    url = opts.get("url") or opts.get("path")
    return _get(_session(ctx), url, opts.get("params")).content


# --- http_json ---------------------------------------------------------------

def http_json(ctx):
    """options: url, params, headers, records_path (where the list of records
    is in each response; omit if the response is the list), delay_sec,
    max_pages, watermark_param (query parameter that receives the stored
    watermark, for "changes since" APIs), and paging:
      {type: page,     param: page, start: 1, size_param: per_page, size: 100}
      {type: offset,   param: offset, size_param: limit, size: 100}
      {type: cursor,   param: cursor, cursor_path: meta.next_cursor}
      {type: next_url, next_path: links.next}
    Paging stops at an empty page, a page shorter than `size`, or a missing
    cursor / next url."""
    opts = ctx.options
    session = _session(ctx)
    params = dict(opts.get("params") or {})
    if opts.get("watermark_param") and ctx.watermark and not ctx.full_refresh:
        params[opts["watermark_param"]] = ctx.watermark
    paging = opts.get("paging") or {}
    kind = paging.get("type")
    size = paging.get("size")
    if size and paging.get("size_param"):
        params[paging["size_param"]] = size
    if kind == "page":
        params[paging.get("param", "page")] = paging.get("start", 1)
    elif kind == "offset":
        params[paging.get("param", "offset")] = paging.get("start", 0)
    delay = float(opts.get("delay_sec", 0))
    max_pages = int(opts.get("max_pages", 10000))

    url = opts["url"]
    for page in range(max_pages):
        data = _get(session, url, params, delay=delay if page else 0).json()
        records = dig(data, opts.get("records_path"))
        if records is None:
            records = []
        if isinstance(records, dict):
            records = [records]
        yield from records
        if not kind or not records or (size and len(records) < size):
            return
        if kind == "page":
            params[paging.get("param", "page")] += 1
        elif kind == "offset":
            params[paging.get("param", "offset")] += len(records)
        elif kind == "cursor":
            cursor = dig(data, paging["cursor_path"])
            if not cursor:
                return
            params[paging.get("param", "cursor")] = cursor
        elif kind == "next_url":
            nxt = dig(data, paging["next_path"])
            if not nxt:
                return
            url, params = nxt, {}
    raise RuntimeError(f"{ctx.name}: stopped after max_pages={max_pages}; raise it if that's expected")


# --- files and sheets --------------------------------------------------------

def _rows_from_csv_text(text, opts):
    reader = csv.reader(io.StringIO(text), delimiter=opts.get("delimiter", ","))
    rows = list(reader)[int(opts.get("skip_rows", 0)):]
    return _rows_to_dicts(rows)


def _rows_to_dicts(rows):
    """First row is the header. Blank header cells become col_<n>, repeated
    names get _2, _3; fully empty rows are dropped."""
    if not rows:
        return
    header, seen = [], {}
    for i, h in enumerate(rows[0]):
        h = str(h).strip() if h is not None else ""
        h = h or f"col_{i + 1}"
        n = seen.get(h, 0) + 1
        seen[h] = n
        header.append(h if n == 1 else f"{h}_{n}")
    for row in rows[1:]:
        values = [None if v is None else v for v in row]
        if all(v in (None, "") for v in values):
            continue
        values += [None] * (len(header) - len(values))
        yield {h: _jsonable(v) for h, v in zip(header, values)}


def _jsonable(v):
    import datetime
    import decimal
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return str(v)
    return v


def csv_file(ctx):
    """options: path (in the source folder) or url, delimiter, encoding
    (default utf-8-sig, which also strips Excel's BOM), skip_rows."""
    data = _read_bytes(ctx)
    yield from _rows_from_csv_text(data.decode(ctx.options.get("encoding", "utf-8-sig")), ctx.options)


def xlsx_file(ctx):
    """options: path or url, sheet (name; default the first), skip_rows."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(_read_bytes(ctx)), read_only=True, data_only=True)
    ws = wb[ctx.options["sheet"]] if ctx.options.get("sheet") else wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)][int(ctx.options.get("skip_rows", 0)):]
    yield from _rows_to_dicts(rows)


def gsheet(ctx):
    """options: sheet_id (from the sheet's URL), gid (tab id, default 0),
    skip_rows. The sheet must be shared as "anyone with the link can view";
    private sheets need a python fetcher using the Google API."""
    opts = dict(ctx.options)
    opts["url"] = (f"https://docs.google.com/spreadsheets/d/{opts['sheet_id']}"
                   f"/export?format=csv&gid={opts.get('gid', 0)}")
    opts.pop("path", None)
    sub = FetchContext(ctx.name, ctx.folder, opts, ctx.watermark, ctx.full_refresh)
    yield from csv_file(sub)


# --- server listings ---------------------------------------------------------

def _listing_record(path, is_dir, size, mtime_epoch):
    p = PurePosixPath(path)
    import datetime
    return {
        "path": path,
        "filename": p.name,
        "folder": None if path == "/" else str(p.parent),
        "suffix": "" if is_dir else p.suffix.lstrip(".").lower(),
        "type": "dir" if is_dir else "file",
        "size": None if is_dir else size,
        "mtime": None if mtime_epoch is None else
        datetime.datetime.fromtimestamp(mtime_epoch, datetime.timezone.utc).isoformat(),
        "depth": len(p.parts) - 1,
    }


def _walk(ctx, listdir):
    """Breadth-first walk from options.roots (default ['/']) down to
    options.max_depth, yielding listing records. listdir(path) returns
    (name, is_dir, size, mtime_epoch) tuples. options.suffixes keeps only
    files with those suffixes (directories are always kept)."""
    opts = ctx.options
    suffixes = {s.lower().lstrip(".") for s in opts.get("suffixes") or []}
    max_depth = opts.get("max_depth")
    queue = list(opts.get("roots") or ["/"])
    seen = set()
    while queue:
        folder = queue.pop(0)
        if folder in seen:
            continue
        seen.add(folder)
        for name, is_dir, size, mtime in listdir(folder):
            if name in (".", "..") or name.startswith("."):
                continue
            path = folder.rstrip("/") + "/" + name
            rec = _listing_record(path, is_dir, size, mtime)
            if not is_dir and suffixes and rec["suffix"] not in suffixes:
                continue
            yield rec
            if is_dir and (max_depth is None or rec["depth"] < max_depth):
                queue.append(path)


def sftp_listing(ctx):
    """options: host, port (22), user, password_env, roots, max_depth,
    suffixes, delay_sec (pause between directory listings)."""
    import paramiko
    opts = ctx.options
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(opts["host"], port=int(opts.get("port", 22)), username=opts["user"],
                   password=ctx.env("password_env"), timeout=30,
                   allow_agent=False, look_for_keys=False)
    delay = float(opts.get("delay_sec", 0))
    try:
        sftp = client.open_sftp()

        def listdir(folder):
            if delay:
                time.sleep(delay)
            for e in sftp.listdir_attr(folder):
                if stat.S_ISLNK(e.st_mode or 0):
                    continue
                yield e.filename, stat.S_ISDIR(e.st_mode or 0), e.st_size, e.st_mtime

        yield from _walk(ctx, lambda f: list(listdir(f)))
    finally:
        client.close()


def ftp_listing(ctx):
    """options: host, port (21), user (anonymous), password_env (optional),
    tls (false), roots, max_depth, suffixes. Needs MLSD support."""
    import datetime
    import ftplib
    opts = ctx.options
    ftp = ftplib.FTP_TLS() if opts.get("tls") else ftplib.FTP()
    ftp.connect(opts["host"], int(opts.get("port", 21)), timeout=60)
    ftp.login(opts.get("user", "anonymous"),
              ctx.env("password_env") if opts.get("password_env") else "")
    if opts.get("tls"):
        ftp.prot_p()
    try:
        def listdir(folder):
            out = []
            for name, facts in ftp.mlsd(folder, facts=["type", "size", "modify"]):
                kind = facts.get("type")
                if kind not in ("dir", "file"):
                    continue
                mtime = None
                if facts.get("modify"):
                    mtime = datetime.datetime.strptime(facts["modify"][:14], "%Y%m%d%H%M%S") \
                        .replace(tzinfo=datetime.timezone.utc).timestamp()
                out.append((name, kind == "dir", int(facts.get("size", 0) or 0), mtime))
            return out

        yield from _walk(ctx, listdir)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


# --- html --------------------------------------------------------------------

def html_listing(ctx):
    """options: url, item_selector (CSS, one match per record), fields
    ({name: selector} or {name: {selector: ..., attr: href}}; selector '.'
    means the item itself), next_selector (link to the next page),
    max_pages, delay_sec, params, headers. Relative links in attr values are
    made absolute."""
    from urllib.parse import urljoin
    from bs4 import BeautifulSoup
    opts = ctx.options
    session = _session(ctx)
    url, params = opts["url"], opts.get("params")
    delay = float(opts.get("delay_sec", 1.0))
    for page in range(int(opts.get("max_pages", 1000))):
        r = _get(session, url, params, delay=delay if page else 0)
        soup = BeautifulSoup(r.text, "lxml")
        for item in soup.select(opts["item_selector"]):
            rec = {}
            for name, spec in opts["fields"].items():
                if isinstance(spec, str):
                    spec = {"selector": spec}
                el = item if spec.get("selector", ".") == "." else item.select_one(spec["selector"])
                if el is None:
                    rec[name] = None
                elif spec.get("attr"):
                    v = el.get(spec["attr"])
                    if isinstance(v, list):
                        v = " ".join(v)
                    if v and spec["attr"] in ("href", "src"):
                        v = urljoin(r.url, v)
                    rec[name] = v
                else:
                    rec[name] = el.get_text(" ", strip=True)
            yield rec
        nxt = soup.select_one(opts["next_selector"]) if opts.get("next_selector") else None
        if nxt is None or not nxt.get("href"):
            return
        url, params = urljoin(r.url, nxt["href"]), None
    raise RuntimeError(f"{ctx.name}: stopped after max_pages")


BUILTIN = {
    "http_json": http_json,
    "csv": csv_file,
    "xlsx": xlsx_file,
    "gsheet": gsheet,
    "sftp_listing": sftp_listing,
    "ftp_listing": ftp_listing,
    "html_listing": html_listing,
}


def canonical_json(record):
    """Stable JSON text of a record (sorted keys), the basis of payload_hash."""
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=str)
