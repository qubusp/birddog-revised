"""Duplicate linking and FTP-to-document matching over Postgres, the work the
legacy runtime did in DatabaseUpdater.link_hash_duplicates and
FTPSiteManager._drain_match_queue. Called by the DAGs in dags/yaml/documents.yml
(birddog/dag_tasks_docs.py).

Duplicates: documents with the same sha1_hash form a group. One member
prevails (same rule as the legacy code, database_updater._choose_prevailing)
and every other member gets a core.document_duplicate row pointing at it.
A birddog-created duplicate gets process_code "duplicate"; an imported one keeps
its curated code and is flagged as an anomaly instead (_mark_hash_duplicate).

FTP matching: a file on the JewishGen SFTP server is the same as a wiki file
when their content fingerprints are equal (size + sha1 of the first and last
64 KB, birddog.ftp._content_fp). Only files whose byte size equals a
document's are fingerprinted, so the expensive reads are few. Three steps, each
safe to re-run:

1. fingerprint_ftp_files: reads FTP files over SFTP (needs SFTP_PASSWORD).
2. fingerprint_documents: reads the same-size wiki files with HTTP Range.
3. reconcile_ftp_matches: pure SQL. Links equal fingerprints, drops links that
   no longer match, and marks a file match_checked once every comparison it
   needs has been made. A checked file goes back on the queue when a new
   same-size document turns up.

A fingerprint is only trusted when it starts with the row's current size
("<size>:..."), so a file that changed size is fingerprinted again without
anything having to clear the old value."""

import json
import logging
import os
import time
from pathlib import Path

from birddog import pg

_logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# duplicates

def dupe_check_hashes(limit):
    """Distinct sha1 hashes that have at least one document not yet
    dupe-checked (core.v_docs_need_dupe_check). Batching by hash, not by
    document, keeps a whole group in one task, so parallel batches can't pick
    different prevailing documents for the same group."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT sha1_hash FROM core.v_docs_need_dupe_check
                       ORDER BY 1 LIMIT %s""", (limit,))
        return [r[0] for r in cur.fetchall()]


def link_duplicates(hashes):
    """Links each hash group, marks the non-prevailing members and sets
    dupes_checked on every member. Returns a summary dict."""
    from birddog.database_updater import _choose_prevailing, _mark_hash_duplicate

    summary = {"hashes": len(hashes), "groups": 0, "links_added": 0,
               "marked_duplicate": 0, "flagged": 0, "checked": 0}
    if not hashes:
        return summary
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT d.id, d.url, d.sha1_hash, d.last_imported, d.process_code,
                              d.anomaly_comment, NOT d.dupes_checked,
                              (SELECT count(*) FROM core.document_duplicate dd
                                WHERE dd.prevailing_id = d.id)
                         FROM core.document d
                        WHERE d.sha1_hash = ANY(%s)
                        ORDER BY d.id
                          FOR UPDATE OF d""", (list(hashes),))
        groups = {}
        for (doc_id, url, sha1, last_imported, process_code, comment,
             unchecked, num_duplicates) in cur.fetchall():
            groups.setdefault(sha1, {})[doc_id] = {
                "Id": doc_id, "url": url, "sha1_hash": sha1, "last_imported": last_imported,
                "process_code": process_code, "anomaly_comment": comment,
                "num_duplicates": num_duplicates, "unchecked": unchecked}

        for members in groups.values():
            if len(members) < 2 or not any(m["unchecked"] for m in members.values()):
                continue
            prevailing_id = _choose_prevailing(members)
            prevailing = members[prevailing_id]
            for other_id in sorted(set(members) - {prevailing_id}):
                cur.execute("""INSERT INTO core.document_duplicate (prevailing_id, duplicate_id)
                               VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                            (prevailing_id, other_id))
                summary["links_added"] += cur.rowcount
                fields = _mark_hash_duplicate(members[other_id], prevailing)
                if "process_code" in fields:
                    cur.execute("UPDATE core.document SET process_code = %s WHERE id = %s",
                                (fields["process_code"], other_id))
                    summary["marked_duplicate"] += 1
                if fields.get("anomaly"):
                    cur.execute("""UPDATE core.document SET anomaly = true, anomaly_comment = %s
                                   WHERE id = %s""", (fields["anomaly_comment"], other_id))
                    summary["flagged"] += 1
            summary["groups"] += 1

        cur.execute("""UPDATE core.document SET dupes_checked = true
                       WHERE sha1_hash = ANY(%s) AND NOT dupes_checked""", (list(hashes),))
        summary["checked"] = cur.rowcount
    _logger.info(f"link_duplicates: {summary}")
    return summary


# ----------------------------------------------------------------------------
# FTP matching: shared SQL

# a fingerprint is valid only for the size it was taken at. "%%" is a LIKE
# wildcard either way: psycopg2 turns it into "%" when it interpolates
# parameters, and two wildcards in a row match the same as one when it doesn't.
_VALID_FP = "({t}.content_fp LIKE {t}.{size} || ':%%')"
_DOC_FP_OK = _VALID_FP.format(t="d", size="byte_size")
_FTP_FP_OK = _VALID_FP.format(t="f", size="size")
# documents an FTP file can be compared with: same size, a wiki file page
# (byte_size only ever comes from MediaWiki), not known to be unreadable
_CANDIDATE = """d.byte_size = f.size AND d.fp_error IS NULL AND d.url LIKE '%%/wiki/%%'"""


class SFTPNotConfigured(Exception):
    """SFTP_PASSWORD is not set, so FTP files can't be read."""


def ftp_files_to_fingerprint(limit):
    """FTP files with no valid fingerprint that have at least one same-size
    document to compare with. Checked files count too: a new same-size document
    puts the file back on the queue, but only at the reconcile step that runs
    after this one."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT f.path, f.size FROM core.ftp_entry f
             WHERE f.type = 'file' AND f.fp_error IS NULL AND f.size IS NOT NULL
               AND NOT COALESCE({_FTP_FP_OK}, false)
               AND EXISTS (SELECT FROM core.document d WHERE {_CANDIDATE})
             ORDER BY f.path LIMIT %s""", (limit,))
        return [{"path": r[0], "size": r[1]} for r in cur.fetchall()]


def documents_to_fingerprint(limit):
    """Documents with no valid fingerprint that share their size with an FTP
    file that has one (the FTP side is read first, so a wiki file is only
    fetched when there is something to compare it with)."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.id, d.url, d.byte_size FROM core.document d
             WHERE d.byte_size IS NOT NULL AND d.fp_error IS NULL AND d.url LIKE '%%/wiki/%%'
               AND NOT COALESCE({_DOC_FP_OK}, false)
               AND EXISTS (SELECT FROM core.ftp_entry f
                            WHERE f.type = 'file' AND f.size = d.byte_size AND {_FTP_FP_OK})
             ORDER BY d.id LIMIT %s""", (limit,))
        return [{"id": r[0], "url": r[1], "byte_size": r[2]} for r in cur.fetchall()]


# ----------------------------------------------------------------------------
# FTP side: SFTP reads

_FTP_CONFIG_PATH = Path(__file__).resolve().parent.parent / "resources" / "ftp_config.json"
_CONNECT_TIMEOUT = 30
_MAX_RECONNECTS = 8
_FLUSH_EVERY = 25


def sftp_config():
    """Host, port, user from resources/ftp_config.json (overridable with
    SFTP_HOST / SFTP_PORT / SFTP_USER); the password from the environment
    variable the file names (SFTP_PASSWORD). Raises SFTPNotConfigured without it."""
    with open(_FTP_CONFIG_PATH, encoding="utf8") as f:
        cfg = json.load(f)
    password = os.environ.get(cfg.get("password") or "SFTP_PASSWORD")
    if not password:
        raise SFTPNotConfigured(f"{cfg.get('password') or 'SFTP_PASSWORD'} is not set")
    return {
        "host": os.environ.get("SFTP_HOST") or cfg["host"],
        "port": int(os.environ.get("SFTP_PORT") or cfg.get("port", 22)),
        "user": os.environ.get("SFTP_USER") or cfg["user"],
        "password": password,
    }


def _open_paramiko(cfg):
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # keep paramiko away from ssh-agent and ~/.ssh (see FTPSiteManager._open_client)
    client.connect(cfg["host"], port=cfg["port"], username=cfg["user"],
                   password=cfg["password"], timeout=_CONNECT_TIMEOUT,
                   allow_agent=False, look_for_keys=False)
    return client


class SFTPSession:
    """One SFTP connection with the legacy connect discipline: JewishGen
    rate-limits connection setup and resets a channel opened too soon, so a
    connection is retried with backoff and only accepted once it survives a
    warm-up (stat, pause, stat). open_client and sleep are injectable for tests."""

    def __init__(self, cfg, open_client=_open_paramiko, sleep=time.sleep,
                 attempts=6, warmup_sec=1.0):
        self._cfg = cfg
        self._open_client = open_client
        self._sleep = sleep
        self._attempts = attempts
        self._warmup = warmup_sec
        self._client = None
        self.sftp = None

    def connect(self):
        import paramiko
        last = None
        for attempt in range(self._attempts):
            if attempt:
                self._sleep(min(2 ** attempt, 20))
            client = None
            try:
                client = self._open_client(self._cfg)
                sftp = client.open_sftp()
                sftp.stat("/")
                self._sleep(self._warmup)
                sftp.stat("/")
            except (OSError, paramiko.SSHException, EOFError) as e:
                last = e
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
                continue
            self._client, self.sftp = client, sftp
            return sftp
        raise last if last is not None else paramiko.SSHException("connect failed")

    def alive(self):
        if self.sftp is None:
            return False
        try:
            self.sftp.stat("/")
            return True
        except Exception:
            return False

    def close(self):
        for obj in (self.sftp, self._client):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self.sftp = self._client = None


def fingerprint_ftp_files(files, session=None):
    """Fingerprints FTP files over SFTP and stores content_fp. A file whose size
    on the server differs from the listing is left for the next listing run; a
    path the server refuses (missing, forbidden, or listed but unopenable, as
    JewishGen does for names its filesystem mangled) gets fp_error. A dropped
    connection is rebuilt; the file in hand is retried next run."""
    import paramiko
    from birddog.ftp import _fp_ftp

    summary = {"files": len(files), "fingerprinted": 0, "unreadable": 0,
               "size_changed": 0, "deferred": 0, "reconnects": 0}
    if not files:
        return summary
    own_session = session is None
    if own_session:
        session = SFTPSession(sftp_config())
    pending = []    # (sql, args)

    def flush():
        if pending:
            with pg.connect() as conn, conn.cursor() as cur:
                for sql, args in pending:
                    cur.execute(sql, args)
            pending.clear()

    try:
        sftp = session.sftp or session.connect()
        for i, item in enumerate(files):
            path, size = item["path"], item["size"]
            try:
                actual = sftp.stat(path).st_size
                if actual != size:
                    summary["size_changed"] += 1
                    continue
                fp = _fp_ftp(sftp, path, size)
                pending.append(("""UPDATE core.ftp_entry SET content_fp = %s, fp_error = NULL
                                   WHERE path = %s AND size = %s""", (fp, path, size)))
                summary["fingerprinted"] += 1
            except (FileNotFoundError, PermissionError) as e:
                pending.append(("UPDATE core.ftp_entry SET fp_error = %s WHERE path = %s",
                                (f"sftp: {e!r}"[:500], path)))
                summary["unreadable"] += 1
            except (OSError, paramiko.SSHException, EOFError) as e:
                if session.alive():
                    # the connection is fine, so this path is the problem
                    pending.append(("UPDATE core.ftp_entry SET fp_error = %s WHERE path = %s",
                                    (f"sftp: {e!r}"[:500], path)))
                    summary["unreadable"] += 1
                    continue
                summary["deferred"] += 1
                summary["reconnects"] += 1
                flush()
                if summary["reconnects"] > _MAX_RECONNECTS:
                    _logger.warning("fingerprint_ftp_files: SFTP keeps dropping, stopping early")
                    summary["deferred"] += len(files) - i - 1
                    break
                _logger.warning(f"fingerprint_ftp_files: connection lost ({e!r}), reconnecting")
                session.close()
                sftp = session.connect()
            if len(pending) >= _FLUSH_EVERY:
                flush()
        flush()
    finally:
        if own_session:
            session.close()
    _logger.info(f"fingerprint_ftp_files: {summary}")
    return summary


# ----------------------------------------------------------------------------
# wiki side

_WIKI_DOWN_STREAK = 5


def fingerprint_documents(docs, fp_wiki=None):
    """Fingerprints wiki files (imageinfo + two HTTP Range reads each) and
    stores content_fp. A file the wiki reports missing, or whose size there no
    longer matches byte_size, gets fp_error; a transient failure leaves the
    document for the next run. Stops after a run of failures (rate limiting)."""
    from birddog.ftp import _FingerprintUnavailable
    if fp_wiki is None:
        from birddog.ftp import _fp_wiki as fp_wiki

    summary = {"docs": len(docs), "fingerprinted": 0, "missing": 0, "deferred": 0}
    updates = []
    streak = 0
    for i, doc in enumerate(docs):
        try:
            fp = fp_wiki(doc["url"])
        except _FingerprintUnavailable as e:
            _logger.warning(f"fingerprint_documents: {doc['url']}: {e}")
            summary["deferred"] += 1
            streak += 1
            if streak >= _WIKI_DOWN_STREAK:
                _logger.warning("fingerprint_documents: wiki unresponsive, stopping early")
                summary["deferred"] += len(docs) - i - 1
                break
            continue
        streak = 0
        if fp is None:
            updates.append((None, "wiki: file missing or not a file page", doc["id"]))
            summary["missing"] += 1
        elif not fp.startswith(f"{doc['byte_size']}:"):
            updates.append((None, f"wiki: size {fp.split(':', 1)[0]} differs from byte_size",
                            doc["id"]))
            summary["missing"] += 1
        else:
            updates.append((fp, None, doc["id"]))
            summary["fingerprinted"] += 1
    if updates:
        with pg.connect() as conn, conn.cursor() as cur:
            for fp, error, doc_id in updates:
                if fp:
                    cur.execute("""UPDATE core.document SET content_fp = %s, fp_error = NULL
                                   WHERE id = %s""", (fp, doc_id))
                else:
                    cur.execute("UPDATE core.document SET fp_error = %s WHERE id = %s",
                                (error, doc_id))
    _logger.info(f"fingerprint_documents: {summary}")
    return summary


# ----------------------------------------------------------------------------
# reconcile

def reconcile_ftp_matches():
    """Set-based pass over the match queue. Returns a summary dict."""
    with pg.connect() as conn, conn.cursor() as cur:
        # a checked file goes back on the queue when a same-size document has
        # not been compared yet, or a comparison disagrees with its links
        cur.execute(f"""
            UPDATE core.ftp_entry f SET match_checked = false
             WHERE f.match_checked AND f.type = 'file' AND f.fp_error IS NULL
               AND (EXISTS (SELECT FROM core.document d
                             WHERE {_CANDIDATE}
                               AND (NOT COALESCE({_DOC_FP_OK}, false)
                                    OR NOT COALESCE({_FTP_FP_OK}, false)
                                    OR (d.content_fp = f.content_fp
                                        AND NOT EXISTS (SELECT FROM core.ftp_document fd
                                                         WHERE fd.ftp_path = f.path
                                                           AND fd.document_id = d.id))))
                    OR EXISTS (SELECT FROM core.ftp_document fd
                                 JOIN core.document d ON d.id = fd.document_id
                                WHERE fd.ftp_path = f.path
                                  AND d.content_fp IS DISTINCT FROM f.content_fp))""")
        requeued = cur.rowcount

        cur.execute(f"""
            CREATE TEMP TABLE q ON COMMIT DROP AS
            SELECT f.path, f.size, f.fp_error IS NOT NULL AS unreadable,
                   CASE WHEN {_FTP_FP_OK} THEN f.content_fp END AS fp
              FROM core.ftp_entry f
             WHERE f.type = 'file' AND NOT f.match_checked""")
        cur.execute(f"""
            CREATE TEMP TABLE cand ON COMMIT DROP AS
            SELECT q.path, d.id, q.fp AS ftp_fp,
                   CASE WHEN {_DOC_FP_OK} THEN d.content_fp END AS doc_fp
              FROM q JOIN core.ftp_entry f ON f.path = q.path
              JOIN core.document d ON {_CANDIDATE}
             WHERE NOT q.unreadable""")
        # complete: nothing to compare with, unreadable, or every comparison made
        cur.execute("""
            CREATE TEMP TABLE done ON COMMIT DROP AS
            SELECT q.path FROM q
             WHERE q.unreadable
                OR NOT EXISTS (SELECT FROM cand c WHERE c.path = q.path
                                 AND (c.ftp_fp IS NULL OR c.doc_fp IS NULL))""")
        cur.execute("""INSERT INTO core.ftp_document (ftp_path, document_id)
                       SELECT path, id FROM cand WHERE ftp_fp = doc_fp
                       ON CONFLICT DO NOTHING""")
        added = cur.rowcount
        # drop links only on complete information
        cur.execute("""DELETE FROM core.ftp_document fd USING done
                        WHERE fd.ftp_path = done.path
                          AND NOT EXISTS (SELECT FROM cand c
                                           WHERE c.path = fd.ftp_path AND c.id = fd.document_id
                                             AND c.ftp_fp = c.doc_fp)""")
        removed = cur.rowcount
        cur.execute("""UPDATE core.ftp_entry f SET match_checked = true
                         FROM done WHERE f.path = done.path""")
        checked = cur.rowcount
        cur.execute("SELECT count(*) FROM q")
        queued = cur.fetchone()[0]
        cur.execute("""SELECT count(DISTINCT path) FILTER (WHERE ftp_fp = doc_fp),
                              count(DISTINCT path) FILTER (WHERE ftp_fp IS NULL),
                              count(DISTINCT path) FILTER (WHERE ftp_fp IS NOT NULL AND doc_fp IS NULL)
                         FROM cand""")
        matched, waiting_ftp_fp, waiting_doc_fp = cur.fetchone()
    summary = {"requeued": requeued, "queued": queued, "checked": checked,
               "links_added": added, "links_removed": removed, "files_matched": matched,
               "waiting_ftp_fp": waiting_ftp_fp, "waiting_doc_fp": waiting_doc_fp}
    _logger.info(f"reconcile_ftp_matches: {summary}")
    return summary
