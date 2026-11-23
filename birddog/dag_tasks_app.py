"""Task callables for the on-demand DAGs the web app triggers (dags/yaml/app.yml):
refresh a page (optionally its whole subtree) from the wiki, translate a page,
and check watchlists for new changes.

Arguments arrive as rendered templates ("{{ params.x }}"), so flags are
strings here; _flag() reads them."""

import logging

from birddog import pg
from birddog.dag_tasks import TRANSLATE_BATCH

_logger = logging.getLogger(__name__)

# pages refreshed per pass while walking a subtree; each pass re-reads which
# descendants are still stale, so pages discovered on the way are included
SUBTREE_BATCH = 20
# safety cap for one deep refresh (the largest archives have ~100k pages; the
# regular refresh_pages DAG drains whatever is left stale)
SUBTREE_MAX_PAGES = 20000


def _flag(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _canonical(title):
    from birddog.wiki import canonicalize_title
    canonical = canonicalize_title(title)
    if not canonical:
        raise ValueError(f"not a page title: {title!r}")
    return canonical


def _page_url(title):
    from birddog.database_updater import normalize_url
    from birddog.wiki import page_url_from_title
    return normalize_url(page_url_from_title(title))


# --- refresh -----------------------------------------------------------------

_SUBTREE = """
WITH RECURSIVE subtree(id) AS (
    SELECT id FROM core.page WHERE url = %(url)s
    UNION
    SELECT p.id FROM core.page p JOIN subtree s ON p.parent_id = s.id
)
"""


def _queue_locations(cur, url, deep):
    """Puts the documents of the page (or its subtree) back on the location
    queue (core.v_docs_no_location)."""
    pages = (_SUBTREE + "SELECT id FROM subtree") if deep else \
        "SELECT id FROM core.page WHERE url = %(url)s"
    cur.execute(
        f"""UPDATE core.document SET location_status = 'pending'
            WHERE id IN (SELECT document_id FROM core.page_document
                         WHERE page_id IN ({pages}))""",
        {"url": url})
    return cur.rowcount


def refresh_page(title, update_locations=False):
    """Refreshes one page from the wiki (also discovering its children)."""
    from birddog.pipeline import refresh_pages
    title = _canonical(title)
    summary = refresh_pages([title])
    if _flag(update_locations):
        with pg.connect() as conn, conn.cursor() as cur:
            summary["location_queued"] = _queue_locations(cur, _page_url(title), deep=False)
    return summary


def refresh_subtree(title, deep=False, update_locations=False):
    """Refreshes every page below the given one: marks the known descendants
    stale, then refreshes stale descendants in passes until none are left
    (children found on the way are inserted stale, so they are picked up by
    a later pass)."""
    from birddog.pipeline import refresh_pages
    if not _flag(deep):
        return {"skipped": "not a deep refresh"}
    title = _canonical(title)
    url = _page_url(title)
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(_SUBTREE + """UPDATE core.page SET stale = true
                                  WHERE id IN (SELECT id FROM subtree) AND url <> %(url)s""",
                    {"url": url})
        marked = cur.rowcount

    summary = {"marked": marked, "refreshed": 0, "missing": 0, "errors": 0, "docs": 0}
    failed = set()  # still stale after a refresh attempt
    while summary["refreshed"] + summary["missing"] < SUBTREE_MAX_PAGES:
        with pg.connect() as conn, conn.cursor() as cur:
            cur.execute(_SUBTREE + """SELECT p.title FROM core.page p JOIN subtree s USING (id)
                                      WHERE p.stale AND p.url <> %(url)s
                                        AND NOT (p.title = ANY(%(failed)s))
                                      ORDER BY p.id LIMIT %(limit)s""",
                        {"url": url, "failed": list(failed), "limit": SUBTREE_BATCH})
            titles = [r[0] for r in cur.fetchall()]
        if not titles:
            break
        batch = refresh_pages(titles)
        for key in ("refreshed", "missing", "errors", "docs"):
            summary[key] += batch[key]
        # pages still stale after their refresh (transient wiki errors) are
        # left to the regular refresh_pages DAG rather than retried here
        with pg.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT title FROM core.page WHERE stale AND title = ANY(%s)", (titles,))
            failed.update(r[0] for r in cur.fetchall())

    if _flag(update_locations):
        with pg.connect() as conn, conn.cursor() as cur:
            summary["location_queued"] = _queue_locations(cur, url, deep=True)
    return summary


# --- translate ---------------------------------------------------------------

def translate_page(title):
    """Translates the page's own description and its children's (the rows the
    page shows), using the shared translation cache."""
    from birddog.pipeline import translate_pages
    title = _canonical(title)
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT u.id, u.native_description FROM core.v_untranslated u
               JOIN core.page p USING (id)
               WHERE p.url = %(url)s
                  OR p.parent_id = (SELECT id FROM core.page WHERE url = %(url)s)
               ORDER BY u.id""",
            {"url": _page_url(title)})
        pages = [{"id": r[0], "text": r[1]} for r in cur.fetchall()]

    summary = {"pages": 0, "translated": 0, "from_cache": 0}
    for i in range(0, len(pages), TRANSLATE_BATCH):
        batch = translate_pages(pages[i:i + TRANSLATE_BATCH])
        for key in summary:
            summary[key] += batch[key]
    return summary


# --- watchlists --------------------------------------------------------------

def check_watches(email="", archive_title=""):
    """Checks watchlist entries against the recorded wiki changes and stores
    the new alerts. One entry when email and archive_title are given (the web
    app's "check now"), otherwise every active watch."""
    from types import SimpleNamespace

    from birddog import user as users, watcher
    from birddog.change_feed import WikiChangeFeed

    # check_watcher() reads changes from runtime.update_manager
    runtime = SimpleNamespace(update_manager=WikiChangeFeed())
    if email and archive_title:
        watches = [(email, _canonical(archive_title))]
    else:
        watches = [(w["email"], w["title"]) for w in watcher.get_all_active_watches()]

    summary = {"watches": 0, "unresolved": 0, "missing": 0, "errors": 0}
    for watch_email, title in watches:
        try:
            unresolved = users.check_watch(watch_email, title, runtime)
        except KeyError:
            # no longer on the user's watchlist
            summary["missing"] += 1
            continue
        except Exception:
            if len(watches) == 1:
                raise
            # one broken watch must not stop the others
            _logger.exception(f"check_watches: {title}")
            summary["errors"] += 1
            continue
        summary["watches"] += 1
        summary["unresolved"] += len(unresolved)
    if email and summary["missing"]:
        raise KeyError(f"not on the watchlist: {archive_title}")
    return summary
