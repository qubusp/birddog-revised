"""Read side of the web app: archive, page, document and translation data from
Postgres (core.*), which the Airflow pipeline keeps fresh.

Replaces the app's reads through NocoDB and most of its live wiki fetches.
Everything here is read-only (the connection is opened read-only); the
writers are birddog/pipeline.py and the DAGs.

page_payload() builds the same JSON the browse view (static/js/birddog.js)
renders from a live wiki page (core.Page.page): description, dates,
doc_link, lastmod, link and tables of rows of cells
{"text": {"uk", "en"}, "link", "exists"}. The tables are built from the
database rather than copied from the wiki page: one "Contents" table with
the page's children (code, description, dates, number of documents) and,
when the page links more than one document, a "Documents" table.
"""

import hashlib
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from birddog import pg
from birddog.utility import form_text_item
from birddog.wiki import (
    ARCHIVE_BASE,
    ROOT_HUB_TITLE,
    WIKI_NAMESPACE,
    canonicalize_title,
    classify_page,
    page_label,
    page_url_from_title,
)
from birddog.log import get_logger

_logger = get_logger()

WIKI_DOMAIN = "uk.wikisource.org"

# Header cells of the generated tables, as multilingual text items.
CONTENTS_TABLE = "Contents"
DOCUMENTS_TABLE = "Documents"
_CONTENTS_HEADER = [
    {"uk": "Шифр", "en": "Code"},
    {"uk": "Назва", "en": "Description"},
    {"uk": "Дати", "en": "Dates"},
    {"uk": "Документи", "en": "Documents"},
]
_DOCUMENTS_HEADER = [
    {"uk": "Документ", "en": "Document"},
    {"uk": "Опис", "en": "Description"},
]
# a few pages link thousands of documents; the browse view doesn't need them all
MAX_DOCUMENT_ROWS = 1000


def enabled():
    """True when the app reads from Postgres (BIRDDOG_DATABASE_URL is set)."""
    return pg.enabled()


@contextmanager
def _cursor():
    with pg.connect() as conn:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            yield cur


def _rows(cur):
    columns = [c[0] for c in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _iso(value):
    """timestamptz -> the wiki's UTC ISO8601 form ("2025-10-09T10:36:46Z"),
    which the frontend formats and compares as strings."""
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def page_url(title):
    """core.page.url for a wiki title, normalized the way the pipeline writes it."""
    from birddog.database_updater import normalize_url
    return normalize_url(page_url_from_title(title))


def wiki_title(row):
    """Canonical wiki title ("Архів:...") of a core.page row, or None when the
    row isn't a wiki page (an archive-website or Drive entry from the imports)."""
    if row.get("domain") != WIKI_DOMAIN:
        return None
    return canonicalize_title(row["title"])


# ----------------------------------------------------------------------------
# translations

def _source_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cached_translations(cur, texts):
    """English for each text found in the core.translation cache, as {text: en}."""
    texts = {t for t in texts if t}
    if not texts:
        return {}
    by_hash = {_source_hash(t): t for t in texts}
    cur.execute(
        "SELECT source_hash, translated_text FROM core.translation WHERE source_hash = ANY(%s)",
        (list(by_hash),))
    return {by_hash[h]: en for h, en in cur.fetchall()}


def _text_item(native, english):
    """Multilingual text item. 'en' is left out when there's no English, so
    the frontend shows the Ukrainian (get_text() falls back to 'uk')."""
    native = native or ""
    if english:
        return {"uk": native, "en": english}
    return form_text_item(native)   # numbers, codes and English need no translation


# ----------------------------------------------------------------------------
# pages

_PAGE_COLUMNS = """p.id, p.url, p.title, p.domain, p.parent_id, p.label, p.root_label,
    p.seq_label, p.level, p.native_description, p.description_en, p.years,
    p.wiki_timestamp, p.availability, p.source_type, p.stale, p.last_birddog_update"""


def get_page(title, cur=None):
    """The core.page row of a wiki title, or None."""
    if cur is None:
        with _cursor() as cur:
            return get_page(title, cur)
    cur.execute(f"SELECT {_PAGE_COLUMNS} FROM core.page p WHERE p.url = %s", (page_url(title),))
    rows = _rows(cur)
    if not rows:
        # same page under a url spelled differently (older imports)
        cur.execute(
            f"""SELECT {_PAGE_COLUMNS} FROM core.page p
                WHERE p.title = %s AND p.domain = %s ORDER BY p.id LIMIT 1""",
            (canonicalize_title(title, include_namespace=False), WIKI_DOMAIN))
        rows = _rows(cur)
    return rows[0] if rows else None


def get_children(page_id, cur=None):
    """Child pages of a page in archive order, each with its document count."""
    if cur is None:
        with _cursor() as cur:
            return get_children(page_id, cur)
    cur.execute(
        f"""SELECT {_PAGE_COLUMNS},
                   (SELECT count(*) FROM core.page_document pd WHERE pd.page_id = p.id) AS doc_count
            FROM core.page p
            WHERE p.parent_id = %s
            ORDER BY p.seq_label NULLS LAST, p.title, p.id""",
        (page_id,))
    return _rows(cur)


def get_documents(page_id, cur=None, limit=MAX_DOCUMENT_ROWS):
    """Documents a page links to: wiki-hosted files first, then by id."""
    if cur is None:
        with _cursor() as cur:
            return get_documents(page_id, cur, limit)
    cur.execute(
        """SELECT d.id, d.url, d.title, d.domain, d.filename, d.description, d.availability,
                  d.mimetype, d.page_count, d.byte_size, d.wiki_timestamp
           FROM core.page_document pd JOIN core.document d ON d.id = pd.document_id
           WHERE pd.page_id = %s
           ORDER BY (d.domain IN ('commons.wikimedia.org', 'uk.wikisource.org')) DESC, d.id
           LIMIT %s""",
        (page_id, limit))
    return _rows(cur)


def page_kind(title, has_children):
    # same rule as core.Page.kind
    kind = classify_page(title)
    if kind == "case" and has_children:
        return "opus"
    return kind


def _child_row(child, translations):
    title = wiki_title(child)
    code = child["title"].rsplit("/", 1)[-1]
    if title:
        link = f"/wiki/{title}"
        exists = child.get("availability") != "redlinked"
    else:
        # not a wiki page: listed, but the browse view can't open it
        link, exists = child["url"], False
    native = child.get("native_description") or ""
    english = child.get("description_en") or translations.get(native)
    count = child.get("doc_count") or 0
    return [
        {"text": form_text_item(code), "link": link, "exists": exists},
        {"text": _text_item(native, english)},
        {"text": form_text_item(child.get("years") or "")},
        {"text": form_text_item(str(count) if count else "")},
    ]


def _document_row(doc):
    name = doc.get("title") or doc.get("filename") or doc["url"]
    description = doc.get("description") or ""
    return [
        {"text": {"uk": name, "en": name}, "link": doc["url"],
         "exists": doc.get("availability") != "error"},
        {"text": {"uk": description, "en": description}},
    ]


def page_payload(title):
    """The browse-view JSON for a wiki title, built from the database; None
    when the page isn't in the database yet. The caller adds the fields that
    don't come from the page itself (lineage, history, ...)."""
    title = canonicalize_title(title)
    with _cursor() as cur:
        page = get_page(title, cur)
        if page is None:
            return None
        children = get_children(page["id"], cur)
        documents = get_documents(page["id"], cur)
        native = page.get("native_description") or ""
        wanted = [native] if not page.get("description_en") else []
        wanted += [c.get("native_description") for c in children if not c.get("description_en")]
        translations = cached_translations(cur, wanted)

    english = page.get("description_en") or translations.get(native)
    tables = []
    if children:
        tables.append({
            "name": CONTENTS_TABLE,
            "header": [dict(h) for h in _CONTENTS_HEADER],
            "children": [_child_row(c, translations) for c in children],
        })
    if len(documents) > 1:
        tables.append({
            "name": DOCUMENTS_TABLE,
            "header": [dict(h) for h in _DOCUMENTS_HEADER],
            "children": [_document_row(d) for d in documents],
        })

    return {
        "title": title,
        "name": page_label(title),
        "link": f"{ARCHIVE_BASE}/wiki/{title}",
        "description": _text_item(native, english),
        "dates": form_text_item(page.get("years") or ""),
        "doc_link": documents[0]["url"] if documents else "",
        "lastmod": _iso(page.get("wiki_timestamp")),
        "tables": tables,
        "kind": page_kind(title, bool(children)),
        "needs_translation": bool(native) and not english,
        "level": page.get("level"),
        "availability": page.get("availability"),
        # where the data came from and how current it is
        "source": "database",
        "db_stale": bool(page.get("stale")),
        "db_updated": _iso(page.get("last_birddog_update")),
    }


def has_content(page_row):
    """False for a row the pipeline only queued (seeded, never fetched)."""
    return bool(page_row.get("wiki_timestamp") or page_row.get("last_birddog_update"))


# ----------------------------------------------------------------------------
# archive roots

_ROOTS_MAX_AGE = 5 * 60  # seconds
_roots_lock = threading.Lock()
_roots_loaded_at = 0.0


def archive_roots():
    """All archive roots as {title, label, description, url}, sorted by label
    (the /archives contract). Excludes the root hub page, like
    wiki.all_archive_roots()."""
    with _cursor() as cur:
        cur.execute("SELECT title, label, description FROM core.archive_root")
        rows = cur.fetchall()
    entries = [
        {"title": title, "label": label, "description": description or "",
         "url": page_url_from_title(title)}
        for title, label, description in rows
        if title != ROOT_HUB_TITLE
    ]
    return sorted(entries, key=lambda e: e["label"])


def refresh_archive_root_labels(max_age=_ROOTS_MAX_AGE, force=False):
    """Reloads wiki.py's in-process archive-root cache (used for page labels
    and breadcrumbs) when it is older than max_age, so roots the pipeline
    registered since the app started get their labels."""
    global _roots_loaded_at
    if not force and time.time() - _roots_loaded_at < max_age:
        return
    with _roots_lock:
        if not force and time.time() - _roots_loaded_at < max_age:
            return
        from birddog import wiki
        try:
            wiki._load_archive_roots()
            _roots_loaded_at = time.time()
        except Exception as err:   # labels fall back to the cached ones
            _logger.warning(f"catalog: archive-root reload failed: {err}")


# ----------------------------------------------------------------------------
# search

def search(query, limit=50):
    """core.search() hits, each with what the UI needs to open it:
    page_title is the wiki title to browse to (for a document, its owning
    wiki page with the lowest id), or None."""
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit), 200))
    with _cursor() as cur:
        cur.execute("SELECT kind, id, title, url, snippet, score FROM core.search(%s, %s)",
                    (query, limit))
        hits = _rows(cur)
        doc_ids = [h["id"] for h in hits if h["kind"] == "document"]
        owners = {}
        if doc_ids:
            cur.execute(
                """SELECT DISTINCT ON (pd.document_id) pd.document_id, p.title, p.domain
                   FROM core.page_document pd JOIN core.page p ON p.id = pd.page_id
                   WHERE pd.document_id = ANY(%s) AND p.domain = %s
                   ORDER BY pd.document_id, p.id""",
                (doc_ids, WIKI_DOMAIN))
            owners = {doc_id: canonicalize_title(t) for doc_id, t, _ in cur.fetchall()}

    results = []
    wiki_prefix = f"{ARCHIVE_BASE}/wiki/{WIKI_NAMESPACE}:"
    for hit in hits:
        if hit["kind"] == "page":
            page_title = canonicalize_title(hit["title"]) if hit["url"].startswith(wiki_prefix) else None
        else:
            page_title = owners.get(hit["id"])
        results.append({
            "kind": hit["kind"],
            "id": hit["id"],
            "title": hit["title"],
            "url": hit["url"],
            "snippet": hit["snippet"] or "",
            "score": float(hit["score"] or 0),
            "page_title": page_title,
            "label": page_label(page_title) if page_title else None,
        })
    return results
