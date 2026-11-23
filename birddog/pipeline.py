"""Wiki pipeline steps that write to Postgres (core.*), called by the Airflow
DAGs in dags/. Each step is safe to re-run: pages are upserted by url and the
work queues are views over the tables, so a failed batch is simply picked up
again on the next run.

The parsing comes from the existing modules (wiki.py, database_updater.py);
only the storage is new."""

import logging
from datetime import datetime, timezone

from birddog import pg
from birddog.wiki import (
    ARCHIVE_BASE,
    WIKI_NAMESPACE_ID,
    get_all_pages,
    get_recent_changes,
    is_root_title,
    literal_parent_title,
    register_archive_root,
    remove_archive_root,
)
from birddog.database_updater import (
    _allowed_doc_link,
    _fetch_mediawiki_file_metadata,
    _form_page_info_from_title,
    _form_simple_page_record,
    _normalize_title,
    _source_from_url,
    form_document_record,
    normalize_url,
)
from birddog.wiki import page_url_from_title

_logger = logging.getLogger(__name__)

WIKI_SITE = "uk.wikisource.org"
_WATERMARK = f"{WIKI_SITE}:recentchanges:{WIKI_NAMESPACE_ID}"


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------
# wiki_changes

def get_watermark(cur, source):
    cur.execute("SELECT value FROM raw.watermark WHERE source = %s", (source,))
    row = cur.fetchone()
    return row[0] if row else None


def set_watermark(cur, source, value):
    cur.execute(
        """INSERT INTO raw.watermark (source, value) VALUES (%s, %s)
           ON CONFLICT (source) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        (source, value),
    )


def _mark_stale(cur, titles):
    """Insert pages that aren't known yet and flag all of them for refresh."""
    for title in titles:
        record = _form_simple_page_record(title)
        cur.execute(
            """INSERT INTO core.page (url, title, domain, label, root_label, seq_label,
                                      level, source_type, availability, stale)
               VALUES (%(url)s, %(title)s, %(domain)s, %(label)s, %(root_label)s, %(seq_label)s,
                       %(level)s, %(source_type)s, %(availability)s, true)
               ON CONFLICT (url) DO UPDATE SET stale = true""",
            record,
        )


def seed_pages(limit=None):
    """First run: every page in the archive namespace becomes a stale page.
    Returns the number of titles seeded."""
    titles = get_all_pages(namespace=WIKI_NAMESPACE_ID)
    if limit:
        titles = titles[:limit]
    with pg.connect() as conn, conn.cursor() as cur:
        _mark_stale(cur, titles)
        for title in titles:
            if is_root_title(title):
                register_archive_root(title)
    return len(titles)


def ingest_recent_changes(seed_limit=None):
    """Reads recent changes since the stored watermark into raw.wiki_change and
    applies them to core.page. Seeds the page list on the very first run.
    Returns a summary dict."""
    now = _utc_now()
    with pg.connect() as conn, conn.cursor() as cur:
        since = get_watermark(cur, _WATERMARK)
        if since is None:
            # set the watermark first, so changes made during a long seed are
            # picked up by the next run rather than lost
            set_watermark(cur, _WATERMARK, now)
            conn.commit()
            return {"seeded": seed_pages(limit=seed_limit)}

    changes = get_recent_changes(base=ARCHIVE_BASE, namespace=WIKI_NAMESPACE_ID,
                                 utc_start=since, utc_end=now)
    stale, deleted = set(), set()
    with pg.connect() as conn, conn.cursor() as cur:
        for title, change in changes.items():
            action = change.get("action", "edit")
            cur.execute(
                """INSERT INTO raw.wiki_change (site, title, action, target_title, changed_at, payload)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                   ON CONFLICT DO NOTHING""",
                (WIKI_SITE, title, action, change.get("target_title"),
                 change.get("timestamp"), _json(change)),
            )
            if action == "delete":
                deleted.add(title)
            elif action == "move":
                deleted.add(title)
                if is_root_title(title):
                    remove_archive_root(title)
                if change.get("target_title"):
                    stale.add(change["target_title"])
            else:
                stale.add(title)
            # a new or changed child can change the parent's child list
            parent = literal_parent_title(title)
            if parent:
                stale.add(parent)

        for title in deleted:
            cur.execute("DELETE FROM core.page WHERE url = %s",
                        (normalize_url(page_url_from_title(title)),))
        _mark_stale(cur, stale - deleted)
        set_watermark(cur, _WATERMARK, now)
    return {"changes": len(changes), "stale": len(stale - deleted), "deleted": len(deleted)}


def _json(value):
    import json
    return json.dumps(value, ensure_ascii=False, default=str)


# ----------------------------------------------------------------------------
# refresh_pages

def stale_page_titles(limit):
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT title FROM core.v_pages_stale ORDER BY id LIMIT %s", (limit,))
        return [r[0] for r in cur.fetchall()]


def classify_page_links(title, page_links):
    """Same link rules as DatabaseUpdater.update_page_records: returns the
    existing parent title, child titles (existing and red-linked) and the set
    of document urls the page links to."""
    parent = page_links.get("parent") or {}
    parent_title = _normalize_title(parent["title"]) if parent.get("exists") else None

    children, red_children = [], []
    for child in page_links.get("children") or []:
        (children if child.get("exists") else red_children).append(_normalize_title(child["title"]))

    doc_urls = set()
    for link in page_links.get("internal_links", []):
        if link.get("doc_type"):
            doc_urls.add(f"https://uk.wikisource.org/wiki/{_normalize_title(link.get('title'))}")
    for source in ("commons_links", "interwiki_links"):
        for link in page_links.get(source, []):
            if link.get("doc_type"):
                doc_urls.add(link.get("url").replace(" ", "_"))
    for url in page_links.get("external_links", []):
        if _allowed_doc_link(url):
            doc_urls.add(url)
    return parent_title, children, red_children, doc_urls


_PAGE_UPSERT = """
INSERT INTO core.page (url, title, domain, label, root_label, seq_label, level,
                       native_description, years, wiki_timestamp, availability,
                       source_type, stale, last_birddog_update)
VALUES (%(url)s, %(title)s, %(domain)s, %(label)s, %(root_label)s, %(seq_label)s, %(level)s,
        %(native_description)s, %(years)s, %(timestamp)s, %(availability)s,
        %(source_type)s, false, now())
ON CONFLICT (url) DO UPDATE SET
    title = EXCLUDED.title, domain = EXCLUDED.domain, label = EXCLUDED.label,
    root_label = EXCLUDED.root_label, seq_label = EXCLUDED.seq_label, level = EXCLUDED.level,
    -- a changed Ukrainian description invalidates its English translation; a
    -- first one doesn't (English imported from the spreadsheets has no
    -- Ukrainian source yet and must survive the first refresh)
    description_en = CASE WHEN core.page.native_description IS NOT NULL
                           AND core.page.native_description IS DISTINCT FROM EXCLUDED.native_description
                          THEN NULL ELSE core.page.description_en END,
    native_description = EXCLUDED.native_description, years = EXCLUDED.years,
    wiki_timestamp = EXCLUDED.wiki_timestamp, availability = EXCLUDED.availability,
    source_type = EXCLUDED.source_type, stale = false, last_birddog_update = now()
RETURNING id
"""


def refresh_pages(titles):
    """Fetches each page from the wiki and writes the page, its parent/child
    pages and its document links. Returns a summary dict."""
    summary = {"refreshed": 0, "missing": 0, "errors": 0, "docs": 0}
    for title in titles:
        info = _form_page_info_from_title(title)
        with pg.connect() as conn, conn.cursor() as cur:
            record = info["record"]
            if info.get("missing"):
                cur.execute(
                    """UPDATE core.page SET availability = 'unlinked', stale = false,
                              last_birddog_update = now() WHERE url = %s""",
                    (record["url"],))
                summary["missing"] += 1
                continue
            if info.get("error"):
                # transient (rate limit, network): leave it stale for the next run
                _logger.warning(f"refresh_pages: {title}: {info['error']}")
                summary["errors"] += 1
                continue

            record = {"native_description": "", "years": "", "timestamp": None, **record}
            record["timestamp"] = record["timestamp"] or None
            cur.execute(_PAGE_UPSERT, record)
            page_id = cur.fetchone()[0]
            if is_root_title(record["title"]):
                register_archive_root(record["title"])

            parent_title, children, red_children, doc_urls = classify_page_links(
                record["title"], info.get("links", {}))
            # new children are discovered here and refreshed on a later run
            for child in children:
                child_rec = _form_simple_page_record(child)
                cur.execute(
                    """INSERT INTO core.page (url, title, domain, label, root_label, seq_label,
                                              level, source_type, availability, stale)
                       VALUES (%(url)s, %(title)s, %(domain)s, %(label)s, %(root_label)s,
                               %(seq_label)s, %(level)s, %(source_type)s, %(availability)s, true)
                       ON CONFLICT (url) DO NOTHING""",
                    child_rec)
                cur.execute("UPDATE core.page SET parent_id = %s WHERE url = %s",
                            (page_id, child_rec["url"]))
            if parent_title:
                cur.execute(
                    """UPDATE core.page SET parent_id = (SELECT id FROM core.page WHERE url = %s)
                       WHERE id = %s""",
                    (normalize_url(page_url_from_title(parent_title)), page_id))

            # document links are replaced, so links removed on the wiki go away here too
            cur.execute("DELETE FROM core.page_document WHERE page_id = %s", (page_id,))
            for doc_url in doc_urls:
                doc = form_document_record(doc_url)
                cur.execute(
                    """INSERT INTO core.document (url, title, domain, wikisource_url)
                       VALUES (%(url)s, %(title)s, %(domain)s, %(wikisource_url)s)
                       ON CONFLICT (url) DO UPDATE SET title = EXCLUDED.title
                       RETURNING id""",
                    {"wikisource_url": None, **doc})
                cur.execute(
                    """INSERT INTO core.page_document (page_id, document_id) VALUES (%s, %s)
                       ON CONFLICT DO NOTHING""",
                    (page_id, cur.fetchone()[0]))
            summary["docs"] += len(doc_urls)
            summary["refreshed"] += 1
    return summary


# ----------------------------------------------------------------------------
# refresh_doc_metadata

_META_COLUMNS = ("byte_size", "mimetype", "mediatype", "width", "height",
                 "page_count", "sha1_hash", "description_url")


def docs_missing_metadata(limit):
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, url, title FROM core.v_docs_no_meta ORDER BY id LIMIT %s", (limit,))
        return [{"id": r[0], "url": r[1], "title": r[2]} for r in cur.fetchall()]


def refresh_doc_metadata(docs):
    """Fetches MediaWiki file metadata for wiki-hosted documents and writes it.
    Files the API can't resolve get availability 'error' so they leave the
    queue. Returns a summary dict."""
    by_source = {}
    for doc in docs:
        source = _source_from_url(doc["url"])
        if source:
            by_source.setdefault(source, []).append(doc)

    summary = {"updated": 0, "errors": 0}
    for source, source_docs in by_source.items():
        meta = _fetch_mediawiki_file_metadata([d["title"] for d in source_docs], source)
        with pg.connect() as conn, conn.cursor() as cur:
            for doc in source_docs:
                m = meta.get(doc["title"].strip()) or {"availability": "error"}
                if m.get("availability") == "error" or not m.get("sha1_hash"):
                    cur.execute("UPDATE core.document SET availability = 'error' WHERE id = %s",
                                (doc["id"],))
                    summary["errors"] += 1
                    continue
                values = {c: m.get(c) for c in _META_COLUMNS}
                values["wiki_timestamp"] = m.get("timestamp") or None
                values["id"] = doc["id"]
                cur.execute(
                    """UPDATE core.document SET
                         byte_size = %(byte_size)s, mimetype = %(mimetype)s,
                         mediatype = %(mediatype)s, width = %(width)s, height = %(height)s,
                         page_count = %(page_count)s, sha1_hash = %(sha1_hash)s,
                         description_url = %(description_url)s,
                         wiki_timestamp = %(wiki_timestamp)s, availability = 'linked',
                         location_status = COALESCE(location_status, 'pending'),
                         last_birddog_update = now()
                       WHERE id = %(id)s""",
                    values)
                summary["updated"] += 1
    return summary


# ----------------------------------------------------------------------------
# translate

def untranslated_pages(limit):
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, native_description FROM core.v_untranslated ORDER BY id LIMIT %s",
                    (limit,))
        return [{"id": r[0], "text": r[1]} for r in cur.fetchall()]


def _source_hash(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def translate_pages(pages):
    """Fills core.page.description_en, translating only texts that aren't in the
    core.translation cache yet (many pages share the same description).
    Returns a summary dict."""
    from birddog.translate import translation

    hashes = {p["id"]: _source_hash(p["text"]) for p in pages}
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT source_hash, translated_text FROM core.translation WHERE source_hash = ANY(%s)",
                    (list(set(hashes.values())),))
        cached = dict(cur.fetchall())

    todo = {}
    for p in pages:
        if hashes[p["id"]] not in cached:
            todo[hashes[p["id"]]] = p["text"]
    if todo:
        keys = list(todo)
        translated = translation([todo[k] for k in keys])
        with pg.connect() as conn, conn.cursor() as cur:
            for key, text in zip(keys, translated):
                cur.execute(
                    """INSERT INTO core.translation (source_hash, source_text, translated_text)
                       VALUES (%s, %s, %s) ON CONFLICT (source_hash) DO NOTHING""",
                    (key, todo[key], text))
                cached[key] = text

    with pg.connect() as conn, conn.cursor() as cur:
        for p in pages:
            cur.execute("UPDATE core.page SET description_en = %s WHERE id = %s",
                        (cached[hashes[p["id"]]], p["id"]))
    return {"pages": len(pages), "translated": len(todo), "from_cache": len(pages) - len(todo)}
