"""birddog.doc_matching against a scratch Postgres database (see
test/pg_scratch.py; skipped without BIRDDOG_TEST_DATABASE_URL). The FTP side
runs against a real local SFTP server (test/sftp_stub.py) or a fake client;
the wiki side uses a fake fingerprint function. No network.

The tests TRUNCATE core.document and core.ftp_entry: use a database of your own."""

import hashlib
import os
import random
import tempfile
import unittest
from datetime import datetime, timezone

import pytest

from test import pg_scratch

paramiko = pytest.importorskip("paramiko")

IMPORTED = datetime(2025, 1, 1, tzinfo=timezone.utc)
WIKI = "https://commons.wikimedia.org/wiki/File:"


def _blob(seed, size):
    return random.Random(seed).randbytes(size)


def _fp(blob):
    from birddog.ftp import _FP_CHUNK, _content_fp
    if len(blob) <= 2 * _FP_CHUNK:
        return _content_fp(len(blob), blob, blob)
    return _content_fp(len(blob), blob[:_FP_CHUNK], blob[-_FP_CHUNK:])


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg_scratch.require_test_database()

    def setUp(self):
        self.sql("TRUNCATE core.document, core.ftp_entry RESTART IDENTITY CASCADE")

    _conn = None

    def sql(self, query, args=None, fetch=False):
        # one autocommit connection for the fixtures: connecting is the slow part
        import psycopg2
        if _Base._conn is None or _Base._conn.closed:
            _Base._conn = psycopg2.connect(os.environ["BIRDDOG_DATABASE_URL"])
            _Base._conn.autocommit = True
        with _Base._conn.cursor() as cur:
            cur.execute(query, args)
            return cur.fetchall() if fetch else None

    def add_doc(self, name, **cols):
        cols = {"url": WIKI + name, "title": "File:" + name, "domain": "commons.wikimedia.org",
                **cols}
        keys = ", ".join(cols)
        marks = ", ".join(["%s"] * len(cols))
        return self.sql(f"INSERT INTO core.document ({keys}) VALUES ({marks}) RETURNING id",
                        list(cols.values()), fetch=True)[0][0]

    def doc(self, doc_id):
        rows = self.sql("""SELECT process_code, anomaly, anomaly_comment, dupes_checked,
                                  content_fp, fp_error FROM core.document WHERE id = %s""",
                        (doc_id,), fetch=True)
        return dict(zip(["process_code", "anomaly", "anomaly_comment", "dupes_checked",
                         "content_fp", "fp_error"], rows[0]))


# ----------------------------------------------------------------------------

class DuplicateTests(_Base):
    def run_all(self):
        from birddog.doc_matching import dupe_check_hashes, link_duplicates
        return link_duplicates(dupe_check_hashes(1000))

    def links(self):
        return set(self.sql("SELECT prevailing_id, duplicate_id FROM core.document_duplicate",
                            fetch=True))

    def test_group_linked_and_marked(self):
        new = self.add_doc("a.pdf", sha1_hash="h1")                                    # birddog-found
        imp = self.add_doc("b.pdf", sha1_hash="h1", last_imported=IMPORTED, process_code="save")
        imp2 = self.add_doc("c.pdf", sha1_hash="h1", last_imported=IMPORTED, process_code="check")
        lone = self.add_doc("d.pdf", sha1_hash="h2")
        summary = self.run_all()
        # imported and not "duplicate" prevails, even though it isn't the lowest id
        self.assertEqual(self.links(), {(imp, new), (imp, imp2)})
        self.assertEqual(self.doc(new)["process_code"], "duplicate")
        self.assertEqual(self.doc(imp2)["process_code"], "check")          # curated code kept
        self.assertTrue(self.doc(imp2)["anomaly"])
        self.assertEqual(self.doc(imp2)["anomaly_comment"], f"hash duplicate of {WIKI}b.pdf")
        self.assertEqual(self.doc(imp)["process_code"], "save")
        for d in (new, imp, imp2, lone):
            self.assertTrue(self.doc(d)["dupes_checked"])
        self.assertEqual((summary["groups"], summary["links_added"], summary["checked"]), (1, 2, 4))

    def test_idempotent(self):
        a = self.add_doc("a.pdf", sha1_hash="h1", last_imported=IMPORTED, anomaly_comment="old")
        b = self.add_doc("b.pdf", sha1_hash="h1", last_imported=IMPORTED)
        self.run_all()
        first = self.doc(b)
        self.assertEqual(self.run_all()["hashes"], 0)                      # queue is empty
        self.sql("UPDATE core.document SET dupes_checked = false")
        again = self.run_all()
        self.assertEqual((again["links_added"], again["flagged"]), (0, 0))
        self.assertEqual(self.doc(b), first)
        self.assertEqual(self.links(), {(a, b)})

    def test_existing_prevailing_kept_when_member_joins(self):
        a = self.add_doc("a.pdf", sha1_hash="h1")
        b = self.add_doc("b.pdf", sha1_hash="h1")
        self.run_all()
        self.assertEqual(self.links(), {(a, b)})
        # a later, imported copy would normally prevail; the group keeps its prevailing doc
        c = self.add_doc("c.pdf", sha1_hash="h1", last_imported=IMPORTED)
        self.run_all()
        self.assertEqual(self.links(), {(a, b), (a, c)})
        self.assertTrue(self.doc(c)["anomaly"])
        self.assertEqual(self.doc(b)["process_code"], "duplicate")

    def test_no_sha1_not_queued(self):
        self.add_doc("a.pdf")
        self.assertEqual(self.run_all()["hashes"], 0)

    def test_batches_by_hash(self):
        from birddog import dag_tasks_docs
        for i in range(5):
            self.add_doc(f"{i}a.pdf", sha1_hash=f"h{i}")
            self.add_doc(f"{i}b.pdf", sha1_hash=f"h{i}")
        old = dag_tasks_docs.DUPE_HASH_BATCH
        dag_tasks_docs.DUPE_HASH_BATCH = 2
        try:
            batches = dag_tasks_docs.dupe_hash_batches()
        finally:
            dag_tasks_docs.DUPE_HASH_BATCH = old
        self.assertEqual(batches, [["h0", "h1"], ["h2", "h3"], ["h4"]])
        for batch in batches:
            dag_tasks_docs.link_duplicate_batch(batch)
        self.assertEqual(len(self.links()), 5)


# ----------------------------------------------------------------------------

class _FTPBase(_Base):
    """A tree on the "server": /A/same.pdf matches doc same.pdf, /A/other.pdf has
    the size of other.pdf but different bytes, /B/lonely.pdf has no same-size
    document, /B/gone.pdf is listed but missing on the server, and /B/small.pdf
    (under 128 KB, hashed whole) matches small.pdf."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.blobs = {
            "/A/same.pdf": _blob(1, 300_000),
            "/A/other.pdf": _blob(2, 400_000),
            "/B/lonely.pdf": _blob(3, 500_000),
            "/B/small.pdf": _blob(4, 1000),
        }
        for path, blob in self.blobs.items():
            local = os.path.join(self.root, path.lstrip("/"))
            os.makedirs(os.path.dirname(local), exist_ok=True)
            with open(local, "wb") as f:
                f.write(blob)
        listing = dict(self.blobs)
        listing["/B/gone.pdf"] = _blob(5, 600_000)
        for path, blob in listing.items():
            folder, name = path.rsplit("/", 1)
            self.sql("""INSERT INTO core.ftp_entry (path, filename, folder, suffix, type, size, depth)
                        VALUES (%s, %s, %s, 'pdf', 'file', %s, 1)""", (path, name, folder, len(blob)))
        self.wiki = {}      # url -> bytes, for the fake wiki fingerprint
        self.docs = {
            "same": self.wiki_doc("same.pdf", self.blobs["/A/same.pdf"]),
            "other": self.wiki_doc("other.pdf", _blob(22, 400_000)),
            "gone": self.wiki_doc("gonewiki.pdf", _blob(5, 600_000), on_wiki=False),
            "small": self.wiki_doc("small.pdf", self.blobs["/B/small.pdf"]),
            "nosize": self.add_doc("nosize.pdf"),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def wiki_doc(self, name, blob, on_wiki=True):
        doc_id = self.add_doc(name, byte_size=len(blob),
                              sha1_hash=hashlib.sha1(blob).hexdigest())
        if on_wiki:
            self.wiki[WIKI + name] = blob
        return doc_id

    def fake_fp_wiki(self, url):
        blob = self.wiki.get(url)
        return None if blob is None else _fp(blob)

    def matches(self):
        return set(self.sql("SELECT ftp_path, document_id FROM core.ftp_document", fetch=True))

    def checked(self):
        return {r[0] for r in self.sql("""SELECT path FROM core.ftp_entry
                                           WHERE match_checked""", fetch=True)}

    def run_pass(self, session=None, sftp=True, fp_wiki=None):
        from birddog import doc_matching as m
        out = {}
        if sftp:
            out["ftp"] = m.fingerprint_ftp_files(m.ftp_files_to_fingerprint(1000), session=session)
        out["docs"] = m.fingerprint_documents(m.documents_to_fingerprint(1000),
                                              fp_wiki=fp_wiki or self.fake_fp_wiki)
        out["reconcile"] = m.reconcile_ftp_matches()
        return out


class SFTPServerTests(_FTPBase):
    """The real paramiko client path, against a local SFTP server."""

    def setUp(self):
        super().setUp()
        from test.sftp_stub import SFTPStub, USER
        self.server = SFTPStub(self.root, password="secret-for-tests").__enter__()
        self.env = {"SFTP_HOST": "127.0.0.1", "SFTP_PORT": str(self.server.port),
                    "SFTP_USER": USER, "SFTP_PASSWORD": "secret-for-tests"}
        self._old_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        self.server.__exit__(None, None, None)
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def test_full_pass(self):
        out = self.run_pass()
        self.assertEqual(out["ftp"]["fingerprinted"], 3)        # same, other, small
        self.assertEqual(out["ftp"]["unreadable"], 1)           # gone
        self.assertEqual(self.matches(), {("/A/same.pdf", self.docs["same"]),
                                          ("/B/small.pdf", self.docs["small"])})
        # every file is settled: matched, compared and different, no peer, unreadable
        self.assertEqual(self.checked(), set(self.blobs) | {"/B/gone.pdf"})
        self.assertIsNone(self.sql("SELECT content_fp FROM core.ftp_entry WHERE path = '/B/lonely.pdf'",
                                   fetch=True)[0][0])          # never read: nothing to compare
        self.assertEqual(self.doc(self.docs["same"])["content_fp"], _fp(self.blobs["/A/same.pdf"]))
        self.assertIsNotNone(self.sql("SELECT fp_error FROM core.ftp_entry WHERE path = '/B/gone.pdf'",
                                      fetch=True)[0][0])
        # the second pass has nothing to do and changes nothing
        again = self.run_pass()
        self.assertEqual(again["ftp"]["files"], 0)
        self.assertEqual(again["docs"]["docs"], 0)
        self.assertEqual((again["reconcile"]["requeued"], again["reconcile"]["links_added"],
                          again["reconcile"]["links_removed"]), (0, 0, 0))

    def test_wrong_password_fails(self):
        from birddog import doc_matching as m
        os.environ["SFTP_PASSWORD"] = "wrong"
        session = m.SFTPSession(m.sftp_config(), sleep=lambda s: None, attempts=2)
        with self.assertRaises(paramiko.AuthenticationException):
            m.fingerprint_ftp_files([{"path": "/A/same.pdf", "size": 300_000}], session=session)

    def test_new_document_requeues_checked_file(self):
        self.run_pass()
        # the same file shows up again as a Wikisource document
        copy = self.wiki_doc("same-copy.pdf", self.blobs["/A/same.pdf"])
        out = self.run_pass()
        self.assertEqual(out["reconcile"]["requeued"], 1)
        self.assertEqual(self.matches(), {("/A/same.pdf", self.docs["same"]),
                                          ("/A/same.pdf", copy),
                                          ("/B/small.pdf", self.docs["small"])})
        # lonely.pdf gets a same-size document later: it is read and compared
        self.wiki_doc("lonely-other.pdf", _blob(33, 500_000))
        self.run_pass()
        self.assertIsNotNone(self.sql("SELECT content_fp FROM core.ftp_entry WHERE path = '/B/lonely.pdf'",
                                      fetch=True)[0][0])
        self.assertIn("/B/lonely.pdf", self.checked())


class FakeClientTests(_FTPBase):
    """Matching logic and failure handling with an in-process SFTP client."""

    def session(self, client):
        from birddog.doc_matching import SFTPSession
        return SFTPSession({}, open_client=lambda cfg: client, sleep=lambda s: None)

    def local_client(self):
        root = self.root

        class Client:
            def open_sftp(self):
                return LocalSFTP()

            def close(self):
                pass

        class LocalSFTP:
            def stat(self, path):
                return os.stat(os.path.join(root, path.lstrip("/")))

            def open(self, path, mode="rb"):
                return open(os.path.join(root, path.lstrip("/")), mode)

            def close(self):
                pass

        return Client()

    def test_without_sftp_uses_stored_fingerprints(self):
        # legacy-imported fingerprint on the FTP side, no SFTP access at all
        self.sql("UPDATE core.ftp_entry SET content_fp = %s, match_checked = true WHERE path = '/A/same.pdf'",
                 (_fp(self.blobs["/A/same.pdf"]),))
        out = self.run_pass(sftp=False)
        self.assertEqual(out["docs"]["docs"], 1)        # only same.pdf has a peer with a fingerprint
        self.assertEqual(self.matches(), {("/A/same.pdf", self.docs["same"])})
        # files with candidates but no fingerprint wait for SFTP
        self.assertEqual(self.checked(), {"/A/same.pdf", "/B/lonely.pdf"})
        self.assertGreaterEqual(out["reconcile"]["waiting_ftp_fp"], 2)

    def test_transient_wiki_failure_keeps_file_queued_and_links(self):
        from birddog.ftp import _FingerprintUnavailable
        self.run_pass(session=self.session(self.local_client()))
        self.assertIn(("/A/same.pdf", self.docs["same"]), self.matches())
        # a new same-size document can't be fingerprinted right now
        self.wiki_doc("same-copy.pdf", self.blobs["/A/same.pdf"])

        def down(url):
            raise _FingerprintUnavailable("429")
        out = self.run_pass(session=self.session(self.local_client()), fp_wiki=down)
        self.assertEqual(out["docs"]["deferred"], 1)
        self.assertNotIn("/A/same.pdf", self.checked())
        self.assertIn(("/A/same.pdf", self.docs["same"]), self.matches())   # not dropped

    def test_changed_content_drops_link(self):
        self.run_pass(session=self.session(self.local_client()))
        # the wiki file was replaced by one of the same size
        new = _blob(99, 300_000)
        self.wiki[WIKI + "same.pdf"] = new
        self.sql("UPDATE core.document SET content_fp = NULL, sha1_hash = %s WHERE id = %s",
                 (hashlib.sha1(new).hexdigest(), self.docs["same"]))
        out = self.run_pass(session=self.session(self.local_client()))
        self.assertEqual(out["reconcile"]["links_removed"], 1)
        self.assertNotIn(("/A/same.pdf", self.docs["same"]), self.matches())
        self.assertIn("/A/same.pdf", self.checked())

    def test_missing_wiki_file_gets_fp_error(self):
        self.run_pass(session=self.session(self.local_client()))
        # gone.pdf is unreadable on SFTP, so its peer is never fetched
        self.assertIsNone(self.doc(self.docs["gone"])["fp_error"])
        # make the FTP side readable: then the wiki side is tried and is missing
        with open(os.path.join(self.root, "B", "gone.pdf"), "wb") as f:
            f.write(_blob(5, 600_000))
        self.sql("UPDATE core.ftp_entry SET fp_error = NULL, match_checked = false WHERE path = '/B/gone.pdf'")
        self.run_pass(session=self.session(self.local_client()))
        self.assertIn("missing", self.doc(self.docs["gone"])["fp_error"])
        self.assertIn("/B/gone.pdf", self.checked())
        self.assertNotIn("/B/gone.pdf", {p for p, _ in self.matches()})

    def test_size_changed_on_server_is_left_for_listing(self):
        with open(os.path.join(self.root, "A", "same.pdf"), "ab") as f:
            f.write(b"x")
        out = self.run_pass(session=self.session(self.local_client()))
        self.assertEqual(out["ftp"]["size_changed"], 1)
        self.assertNotIn("/A/same.pdf", self.checked())

    def test_dropped_connection_reconnects(self):
        from birddog.doc_matching import SFTPSession, fingerprint_ftp_files
        root = self.root
        state = {"opens": 0, "dead": False}

        class Flaky:
            def stat(self, path):
                if state["dead"]:
                    raise OSError("Socket is closed")
                return os.stat(os.path.join(root, path.lstrip("/")))

            def open(self, path, mode="rb"):
                if state["opens"] == 0:
                    state["opens"] += 1
                    state["dead"] = True
                    raise OSError("Socket is closed")
                state["opens"] += 1
                return open(os.path.join(root, path.lstrip("/")), mode)

            def close(self):
                pass

        class Client:
            def open_sftp(self):
                state["dead"] = False
                return Flaky()

            def close(self):
                pass

        session = SFTPSession({}, open_client=lambda cfg: Client(), sleep=lambda s: None)
        files = [{"path": "/A/same.pdf", "size": 300_000}, {"path": "/A/other.pdf", "size": 400_000}]
        out = fingerprint_ftp_files(files, session=session)
        self.assertEqual((out["reconnects"], out["deferred"], out["fingerprinted"]), (1, 1, 1))


class SessionTests(unittest.TestCase):
    def test_connect_retries_with_backoff(self):
        from birddog.doc_matching import SFTPSession
        calls, sleeps = [], []

        class Client:
            def open_sftp(self):
                return self

            def stat(self, path):
                return None

            def close(self):
                pass

        def open_client(cfg):
            calls.append(1)
            if len(calls) < 3:
                raise paramiko.SSHException("Error reading SSH protocol banner")
            return Client()

        session = SFTPSession({}, open_client=open_client, sleep=sleeps.append, warmup_sec=0.5)
        self.assertIsNotNone(session.connect())
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [2, 4, 0.5])

    def test_config_needs_password(self):
        from birddog.doc_matching import SFTPNotConfigured, sftp_config
        old = os.environ.pop("SFTP_PASSWORD", None)
        try:
            with self.assertRaises(SFTPNotConfigured):
                sftp_config()
            os.environ["SFTP_PASSWORD"] = "x"
            cfg = sftp_config()
            self.assertEqual((cfg["host"], cfg["port"]), ("sftp.jewishgen.org", 22))
        finally:
            os.environ.pop("SFTP_PASSWORD", None)
            if old is not None:
                os.environ["SFTP_PASSWORD"] = old
