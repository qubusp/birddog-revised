"""Wiki page changes as recorded by the wiki_changes DAG (raw.wiki_change), in
the shape the watchers expect from the old in-memory PageTracker:

    {title: {"timestamp": "YYYY-MM-DDTHH:MM:SSZ", "user": ..., "action": ...,
             "target_title": ... (moves only)}}

one entry per page, its latest change."""

from birddog import pg
from birddog.wiki import canonicalize_title, title_in_scope

WIKI_SITE = "uk.wikisource.org"


def _format_ts(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class WikiChangeFeed:
    def get_updates(self, include, exclude=None, cutoff_date=None):
        """Latest change per page in scope, strictly newer than cutoff_date
        (an ISO timestamp string; None or "0" means all recorded changes)."""
        sql = """SELECT title, action, target_title, changed_at AT TIME ZONE 'UTC',
                        payload->>'user'
                 FROM raw.wiki_change WHERE site = %s"""
        args = [WIKI_SITE]
        if cutoff_date and cutoff_date != "0":
            sql += " AND changed_at > %s::timestamptz"
            args.append(cutoff_date)
        sql += " ORDER BY changed_at, id"
        with pg.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()

        result = {}
        for title, action, target_title, changed_at, user in rows:
            title = canonicalize_title(title)
            if not title_in_scope(title, include, exclude):
                continue
            update = {"timestamp": _format_ts(changed_at), "user": user or "", "action": action}
            if target_title:
                update["target_title"] = canonicalize_title(target_title)
            # rows are in time order, so the last one wins
            result[title] = update
        return result
