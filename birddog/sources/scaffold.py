"""`python -m birddog.sources new <name> --type <type>`: creates
dags/sources/<name>/ with a commented source.yml, transform.sql and (for
--type python) fetch.py, ready to fill in."""

from pathlib import Path

from birddog.sources.spec import FETCH_TYPES, NAME_RE

FETCH_BLOCKS = {
    "http_json": """\
fetch:
  type: http_json
  url: https://api.example.org/v1/items        # TODO
  params: {status: published}                  # fixed query parameters
  headers:
    Authorization: env:EXAMPLE_API_TOKEN        # 'env:VAR' reads a secret from the environment
  records_path: data.items                     # where the list is in each response (omit if top level)
  paging:                                      # omit for a single request
    type: page                                 # page | offset | cursor | next_url
    param: page
    start: 1
    size_param: per_page
    size: 100
  # watermark_param: updated_since             # send the stored watermark (changes since)
  delay_sec: 0.5                               # pause between requests
""",
    "csv": """\
fetch:
  type: csv
  url: https://example.org/export/items.csv     # TODO, or `path: items.csv` (a file in this folder)
  delimiter: ","
  encoding: utf-8-sig
  skip_rows: 0                                 # rows above the header
""",
    "xlsx": """\
fetch:
  type: xlsx
  path: items.xlsx                             # TODO: a file in this folder, or `url: https://...`
  sheet: Sheet1                                # default: the first sheet
  skip_rows: 0
""",
    "gsheet": """\
fetch:
  type: gsheet
  sheet_id: 1AbC...                            # TODO: from the sheet's URL (.../spreadsheets/d/<id>/edit)
  gid: 0                                       # the tab (#gid=... in the URL)
  # the sheet must be shared "anyone with the link can view"
""",
    "sftp_listing": """\
fetch:
  type: sftp_listing
  host: sftp.example.org                       # TODO
  port: 22
  user: example
  password_env: EXAMPLE_SFTP_PASSWORD          # name of the environment variable, never the password
  roots: ["/"]
  # max_depth: 3
  suffixes: [pdf]                              # files to keep (directories are always kept)
  delay_sec: 0.2
""",
    "ftp_listing": """\
fetch:
  type: ftp_listing
  host: ftp.example.org                        # TODO
  port: 21
  user: anonymous
  # password_env: EXAMPLE_FTP_PASSWORD
  tls: false
  roots: ["/"]
  suffixes: [pdf]
""",
    "html_listing": """\
fetch:
  type: html_listing
  url: https://archive.example.org/fonds       # TODO
  item_selector: "table.listing tr.item"       # CSS: one match per record
  fields:
    title: "td.title"                          # text of the first match inside the item
    url: {selector: "td.title a", attr: href}  # an attribute (links are made absolute)
    year: "td.year"
  next_selector: "a.next"                      # link to the next page (omit for one page)
  delay_sec: 1.0                               # be polite: pause between pages
""",
    "python": """\
fetch:
  type: python
  callable: fetch.py:fetch                     # fetch(ctx) in this folder's fetch.py
  # any other keys here reach the function as ctx.options
""",
}

KEY_HINT = {
    "sftp_listing": "path", "ftp_listing": "path", "html_listing": "url",
}

SOURCE_YML = """\
# Source kit: dags/sources/README.md. Validate with
#   python -m birddog.sources validate {name}
# and run against a database with
#   BIRDDOG_DATABASE_URL=... python -m birddog.sources run {name}
name: {name}                     # = folder name; the DAG is source_{name}
description: TODO one line - what this source is and where it lands
owner: TODO who to ask about it
notes: |
  TODO anything the next person needs: access, rate limits, quirks.
enabled: true
schedule: null                   # null (manual), a cron string like "0 4 * * *", or
                                 # {{cron: "0 * * * *", assets: [core/page]}} (run on asset updates, cron as fallback)
tags: []

{fetch_block}
key: {key}                         # TODO: payload field(s) that identify a record; a list for compound keys
mode: full                       # full: every run returns the whole source (deletions show up as `gone`)
                                 # incremental: only new/changed records (use with a watermark)
# watermark:
#   field: updated_at            # the maximum of this field is stored in raw.watermark as 'source:{name}'
#   initial: "2026-01-01T00:00:00Z"

transform: transform.sql         # landed records -> core rows (runs in one transaction)

verify:                          # all must pass or the run fails and downstream DAGs don't fire
  - check: landed                # the fetch returned enough records
    min: 1
    # max_shrink: 0.2            # full mode: fail if 20% fewer records than last time
  - check: row_count
    table: core.TODO
    min: 1
  # - check: null_rate
  #   table: core.TODO
  #   column: TODO
  #   max: 0.05
  # - check: sample_match        # random landed records vs their core rows
  #   table: core.TODO
  #   key_column: TODO           # core column holding the landing key
  #   fields: {{payload_field: core_column}}
  #   size: 200
  #   min_percent: 99
  # - check: sql                 # anything else: one row, one boolean
  #   name: no orphans
  #   sql: SELECT count(*) = 0 FROM core.TODO WHERE ...

produces:                        # assets other DAGs can schedule on
  - core/TODO

view: true                       # views.source__{name} for Mathesar (false, or a SQL file of your own)
pool: default_pool               # wiki_api for anything calling the wiki
retries: 2
retry_delay_sec: 300
"""

TRANSFORM_SQL = """\
-- {name}: landed records -> core.
-- Available in this transaction:
--   landed      records new or changed since the last transform:
--               key, payload (jsonb, as fetched), batch_id, landed_at, first_landed_at
--   landed_all  every current record of this source (same columns)
--   gone        records the last full fetch no longer returned (mode: full)
--   current_setting('birddog.source'), current_setting('birddog.batch_id')
-- Write idempotent SQL (INSERT ... ON CONFLICT DO UPDATE): a failed run is
-- retried with the same records plus any newer ones.

-- TODO
-- INSERT INTO core.example (id, title, year)
-- SELECT key, payload->>'title', NULLIF(payload->>'year', '')::int
-- FROM landed
-- ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, year = EXCLUDED.year;
SELECT 1;
"""

FETCH_PY = '''\
"""Fetch for {name}. fetch(ctx) yields one dict per record; the kit lands
them unchanged in raw.source_record (key = source.yml `key`).

ctx.options      the fetch: block of source.yml
ctx.watermark    the stored high-water mark (None on the first run / a full refresh)
ctx.set_watermark(v)  choose the next watermark yourself (else the kit keeps
                 the maximum of `watermark.field` over what you yielded)
ctx.env("token_env")  the secret named by an option ending in _env
ctx.log          a logger
"""

import requests


def fetch(ctx):
    # TODO: replace with the real thing
    since = ctx.watermark or "1970-01-01T00:00:00Z"
    resp = requests.get("https://api.example.org/items", params={{"since": since}}, timeout=60)
    resp.raise_for_status()
    for item in resp.json()["items"]:
        yield item
'''


def new_source(name, kind, sources_dir, force=False):
    if not NAME_RE.match(name):
        raise ValueError("name: lowercase letters, digits and underscores, starting with a letter")
    if kind not in FETCH_TYPES:
        raise ValueError(f"--type must be one of {', '.join(FETCH_TYPES)}")
    folder = Path(sources_dir) / name
    if folder.exists() and not force:
        raise FileExistsError(f"{folder} exists (use --force to overwrite the templates)")
    folder.mkdir(parents=True, exist_ok=True)
    files = {
        "source.yml": SOURCE_YML.format(name=name, fetch_block=FETCH_BLOCKS[kind],
                                        key=KEY_HINT.get(kind, "id")),
        "transform.sql": TRANSFORM_SQL.format(name=name),
    }
    if kind == "python":
        files["fetch.py"] = FETCH_PY.format(name=name)
    for fname, text in files.items():
        (folder / fname).write_text(text, encoding="utf8")
    return folder, sorted(files)
