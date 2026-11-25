"""Message Batches API mode for the expensive book calls (DAG param use_batch):
read_books' Sonnet read and Opus re-read, transcribe_books' Sonnet
transcription and Opus names re-read. Probe, assemble, index and guess_places
stay synchronous (they are cheap).

Batch requests cost half the standard price and finish within 24 hours (most
within one), so a run does each phase as submit -> wait -> collect, and the
wait is a rescheduling sensor that holds no worker slot:

  submit   picks the pages that need the phase and are not already in an open
           batch, estimates each request at batch prices (EST_PER_PAGE, the
           pilot's measured tokens per page) and submits only what fits in what
           is left of the run's budget_usd; the rest stays queued for the next
           run. Requests are the same as the synchronous calls (books.read_params,
           book_transcripts.transcribe_params) without the server-side fallbacks,
           which the Batches API rejects. Batches are split to stay well under
           the API's size limit (page images are inlined).
  wait     polls every open batch of the DAG until it has ended.
  collect  streams the results of every ended batch of the DAG and phase (any
           run, so a batch left by a run that died is still collected), keyed
           by custom_id, saves the pages exactly as the synchronous path does
           and records tokens and dollars (at batch prices) per request. A
           refused request is retried synchronously (with the fallbacks); a
           cut-off answer fails the book, as in the synchronous path; an
           errored or expired request leaves its pages for the next run.

The Opus phase depends on Sonnet's results, so a run has two rounds. Tables:
core.llm_batch, core.llm_batch_request (migration 027); progress in
views.books__batches. A run that dies after submit resubmits nothing: pages in
an open batch are skipped, and collect finds the batch by DAG and phase."""

import json
import logging
import re

from birddog import book_transcripts as bt
from birddog import books, pg

_logger = logging.getLogger(__name__)

DAGS = ("read_books", "transcribe_books")
PHASES = ("first", "reread")          # Sonnet, then Opus
BATCH_MAX_REQUESTS = 2000
BATCH_MAX_BYTES = 150_000_000         # the API allows 256 MB per batch; images are inlined
DEFAULT_TOKENS_PER_PAGE = 4175        # the pilot's mean probe tokens_per_page

# per page, beyond the image's tokens_per_page: (extra input tokens, output tokens),
# measured on the pilot (core.book_page, core.book_transcript, 2026-11-25)
EST_PER_PAGE = {
    ("read_books", "first"): (books.EST_PROMPT_PER_CALL / books.PAGES_PER_CALL, books.EST_OUTPUT_PER_PAGE),
    ("read_books", "reread"): (books.EST_PROMPT_PER_CALL / books.PAGES_PER_CALL, books.EST_REREAD_OUTPUT_PER_PAGE),
    ("transcribe_books", "first"): (1007, 1521),     # Sonnet: 5,182 in / 1,521 out per page
    ("transcribe_books", "reread"): (1007, 5248),    # Opus names re-read: 5,165 in / 5,248 out
}
PAGES_PER_REQUEST = {"read_books": books.PAGES_PER_CALL, "transcribe_books": bt.TRANSCRIBE_PAGES_PER_CALL}
MAX_TOKENS = {"read_books": books.READ_MAX_TOKENS, "transcribe_books": bt.TRANSCRIBE_MAX_TOKENS}
_CUSTOM_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def model_for(phase):
    return books.READ_MODEL if phase == "first" else books.REREAD_MODEL


# --- pure helpers --------------------------------------------------------------------

def custom_id(dag, document_id, pages, phase):
    """'r-2581-p3-4-first': unique per request, within the API's 64 [A-Za-z0-9_-]."""
    cid = f"{dag[0]}-{document_id}-p{pages[0]}-{pages[-1]}-{phase}"
    if not _CUSTOM_ID.match(cid):
        raise ValueError(cid)
    return cid


def est_cost(dag, phase, n_pages, tokens_per_page=None):
    """Estimated dollars of one request at batch prices."""
    extra_in, out = EST_PER_PAGE[(dag, phase)]
    tokens_in = n_pages * ((tokens_per_page or DEFAULT_TOKENS_PER_PAGE) + extra_in)
    tokens_out = n_pages * out
    return books.cost_usd(model_for(phase), tokens_in, tokens_out) * books.BATCH_PRICE_FACTOR


def chunk_pages(pages, size):
    """Consecutive runs of at most `size` pages: [1,2,3,4,5,9] -> [[1,2,3,4],[5],[9]] for size 4."""
    out, run = [], []
    for p in sorted(pages):
        if run and (p != run[-1] + 1 or len(run) >= size):
            out.append(run)
            run = []
        run.append(p)
    if run:
        out.append(run)
    return out


def plan_requests(dag, phase, work, remaining_usd):
    """Requests that fit the budget, in order: work is [(document_id, title, pages,
    tokens_per_page)]; returns ([(document_id, title, pages, est)], est_total, left_out)."""
    picked, total, left = [], 0.0, 0
    for document_id, title, pages, tpp in work:
        for chunk in chunk_pages(pages, PAGES_PER_REQUEST[dag]):
            est = est_cost(dag, phase, len(chunk), tpp)
            if total + est > remaining_usd:
                left += 1
                continue
            picked.append((document_id, title, chunk, est))
            total += est
    return picked, round(total, 4), left


def params_size(params):
    return len(json.dumps(params))


# --- which pages need a phase -----------------------------------------------------------

def open_pages(dag):
    """{(document_id, page_no, phase)} in batches of this DAG not yet collected."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT r.document_id, p.page_no, r.phase
                       FROM core.llm_batch_request r JOIN core.llm_batch b USING (batch_id)
                       CROSS JOIN LATERAL unnest(r.page_nos) AS p(page_no)
                       WHERE b.dag = %s AND b.status IN ('submitted', 'ended')""", (dag,))
        return set(cur.fetchall())


def _read_books_work(phase):
    out = []
    for doc_id, url, page_count in books.books_in(["approved", "reading"]):
        done = books.page_results(doc_id)
        if phase == "first":
            pages = [p for p in range(1, (page_count or 0) + 1) if books.READ_MODEL not in done.get(p, {})]
        else:
            pages = [p for p, r in sorted(done.items())
                     if books.REREAD_MODEL not in r and books._hard_page(r.get(books.READ_MODEL, {}))]
        out.append((doc_id, url, pages))
    return out


def _transcribe_work(phase):
    out = []
    for status in (["pending", "transcribing"], ["reread_names"]):
        for doc_id, url, page_count in bt.queued(status):
            done = bt.transcripts(doc_id)
            if status == ["reread_names"]:
                pages = bt.reread_pages(done, rerun=True) if phase == "reread" else []
            elif phase == "first":
                pages = [p for p in range(1, (page_count or 0) + 1) if books.READ_MODEL not in done.get(p, {})]
            else:
                pages = bt.reread_pages(done, rerun=False)
            out.append((doc_id, url, pages))
    return out


def _tokens_per_page():
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT document_id, tokens_per_page FROM core.book_read WHERE tokens_per_page > 0")
        return dict(cur.fetchall())


def pending_work(dag, phase):
    """[(document_id, url, pages, tokens_per_page)] needing the phase, minus open pages."""
    raw = _read_books_work(phase) if dag == "read_books" else _transcribe_work(phase)
    busy = open_pages(dag)
    tpp = _tokens_per_page()
    work = []
    for doc_id, url, pages in raw:
        pages = [p for p in pages if (doc_id, p, phase) not in busy]
        if pages:
            work.append((doc_id, url, pages, tpp.get(doc_id)))
    return work


# --- budget -------------------------------------------------------------------------------

def run_committed(dag, run_id):
    """Dollars this run has committed in batches: actual when collected, else estimated."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT coalesce(sum(CASE WHEN status = 'collected' THEN actual_cost_usd
                                                ELSE est_cost_usd END), 0)
                       FROM core.llm_batch WHERE dag = %s AND run_id = %s""", (dag, run_id))
        return float(cur.fetchone()[0])


# --- submit / wait / collect ------------------------------------------------------------------

def _request_params(dag, phase, title, pages, names):
    model = model_for(phase)
    if dag == "read_books":
        return books.read_params(title, pages, model)
    return bt.transcribe_params(title, pages, model, names=names)


def _names_books():
    return {d for d, _, _ in bt.queued(["reread_names"])}


def submit(dag, phase, run_id, budget_usd, client=None):
    """Submit the phase's pending pages that fit the budget. Returns a summary."""
    remaining = budget_usd - run_committed(dag, run_id)
    work = pending_work(dag, phase)
    picked, est_total, left = plan_requests(
        dag, phase, [(d, books.file_title(u), p, t) for d, u, p, t in work], max(0.0, remaining))
    summary = {"dag": dag, "phase": phase, "requests": len(picked), "left_for_next_run": left,
               "est_usd": est_total, "remaining_budget_usd": round(remaining, 4), "batches": []}
    if not picked:
        return summary
    client = client or books.client()
    if dag == "read_books":
        for doc_id in {d for d, _, _, _ in picked}:   # mark books in progress, as the sync path does
            books.update_book(doc_id, run_id, status="reading", error=None)
    names = _names_books() if (dag == "transcribe_books" and phase == "reread") else set()
    batch, size = [], 0

    def flush():
        nonlocal batch, size
        if batch:
            summary["batches"].append(_create(client, dag, phase, run_id, batch))
        batch, size = [], 0

    for doc_id, title, pages, est in picked:
        try:
            params = _request_params(dag, phase, title, pages,
                                     names=(phase == "reread" and dag == "transcribe_books"))
        except books.BookError as e:   # e.g. Commons gives no page render: fail the book, as sync does
            _fail_book(dag, doc_id, run_id, f"{phase}: {e}")
            continue
        n = params_size(params)
        if batch and (size + n > BATCH_MAX_BYTES or len(batch) >= BATCH_MAX_REQUESTS):
            flush()
        batch.append((custom_id(dag, doc_id, pages, phase), doc_id, pages, est, params))
        size += n
    flush()
    return summary


def _create(client, dag, phase, run_id, batch):
    created = client.messages.batches.create(
        requests=[{"custom_id": cid, "params": params} for cid, _, _, _, params in batch])
    est = round(sum(e for _, _, _, e, _ in batch), 4)
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO core.llm_batch (batch_id, dag, run_id, phase, model, status,
                                                   request_count, est_cost_usd)
                       VALUES (%s, %s, %s, %s, %s, 'submitted', %s, %s)""",
                    (created.id, dag, run_id, phase, model_for(phase), len(batch), est))
        for cid, doc_id, pages, e, _ in batch:
            cur.execute("""INSERT INTO core.llm_batch_request (batch_id, custom_id, document_id, page_nos,
                                                               model, phase, est_cost_usd)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (created.id, cid, doc_id, pages, model_for(phase), phase, round(e, 4)))
    _logger.info("submitted batch %s: %s %s, %d requests, est $%.2f", created.id, dag, phase, len(batch), est)
    return created.id


def poll(dag, client=None):
    """Update every open batch of the DAG; True when none is still processing."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT batch_id FROM core.llm_batch WHERE dag = %s AND status = 'submitted'", (dag,))
        open_ids = [r[0] for r in cur.fetchall()]
    if not open_ids:
        return True
    client = client or books.client()
    still = 0
    for batch_id in open_ids:
        b = client.messages.batches.retrieve(batch_id)
        if b.processing_status == "ended":
            with pg.connect() as conn, conn.cursor() as cur:
                cur.execute("""UPDATE core.llm_batch SET status = 'ended', ended_at = coalesce(%s, now())
                               WHERE batch_id = %s""", (getattr(b, "ended_at", None), batch_id))
        else:
            still += 1
            _logger.info("batch %s: %s (%s)", batch_id, b.processing_status, b.request_counts)
    return still == 0


def _fail_book(dag, doc_id, run_id, error):
    if dag == "read_books":
        books.update_book(doc_id, run_id, status="failed", error=error)
    else:
        bt.update_queue(doc_id, run_id, status="failed", error=error)


def _save(dag, phase, doc_id, model, by_page, t_in, t_out, dollars, run_id):
    if dag == "read_books":
        books.save_pages(doc_id, model, by_page, t_in, t_out, dollars)
        books.update_book(doc_id, run_id, (t_in, t_out, dollars))
    else:
        bt.save_transcripts(doc_id, model, by_page, t_in, t_out, dollars,
                            prompt=bt.NAMES_PROMPT_KEY if phase == "reread" else None)
        bt.update_queue(doc_id, run_id, (t_in, t_out, dollars))


def _sync_retry(dag, phase, doc_id, pages):
    """A refused batch request, once more synchronously with the server-side fallbacks."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT url FROM core.document WHERE id = %s", (doc_id,))
        title = books.file_title(cur.fetchone()[0])
    model = model_for(phase)
    if dag == "read_books":
        return books.read_pages(title, pages, model)
    return bt.transcribe_pages(title, pages, model, names=(phase == "reread"))


def collect(dag, phase, run_id, client=None):
    """Save the results of every ended batch of the DAG and phase."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT batch_id FROM core.llm_batch
                       WHERE dag = %s AND phase = %s AND status = 'ended' ORDER BY submitted_at""",
                    (dag, phase))
        ended = [r[0] for r in cur.fetchall()]
    summary = {"batches": len(ended), "succeeded": 0, "refused": 0, "failed": 0, "errored": 0,
               "expired": 0, "canceled": 0, "cost_usd": 0.0}
    if not ended:
        return summary
    client = client or books.client()
    for batch_id in ended:
        with pg.connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT custom_id, document_id, page_nos, model FROM core.llm_batch_request
                           WHERE batch_id = %s""", (batch_id,))
            requests = {cid: (d, list(p), m) for cid, d, p, m in cur.fetchall()}
        for result in client.messages.batches.results(batch_id):
            req = requests.get(result.custom_id)
            if req is None:
                _logger.warning("batch %s: unknown custom_id %s", batch_id, result.custom_id)
                continue
            status, error, cost = _handle(dag, phase, run_id, req, result)
            summary[status if status in summary else "failed"] += 1
            summary["cost_usd"] = round(summary["cost_usd"] + cost[2], 4)
            with pg.connect() as conn, conn.cursor() as cur:
                cur.execute("""UPDATE core.llm_batch_request
                               SET status = %s, error = %s, input_tokens = %s, output_tokens = %s,
                                   cost_usd = %s, updated_at = now()
                               WHERE batch_id = %s AND custom_id = %s""",
                            (status, error, cost[0], cost[1], round(cost[2], 4), batch_id, result.custom_id))
        with pg.connect() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE core.llm_batch SET status = 'collected', collected_at = now(),
                               actual_cost_usd = (SELECT coalesce(sum(cost_usd), 0)
                                                  FROM core.llm_batch_request WHERE batch_id = %s)
                           WHERE batch_id = %s""", (batch_id, batch_id))
    return summary


def _handle(dag, phase, run_id, req, result):
    """(status, error, (tokens_in, tokens_out, $)) for one batch result; saves the pages."""
    doc_id, pages, model = req
    kind = result.result.type
    if kind != "succeeded":
        error = getattr(getattr(result.result, "error", None), "error", None) or getattr(result.result, "error", None)
        if kind == "errored" and getattr(error, "type", "") == "invalid_request_error":
            _fail_book(dag, doc_id, run_id, f"{phase} batch: invalid request {getattr(error, 'message', '')}"[:500])
        return kind, (str(getattr(error, "message", "")) or None) if error else None, (0, 0, 0.0)
    missing = books.READ_MISSING if dag == "read_books" else bt.TRANSCRIBE_MISSING
    try:
        parsed, t_in, t_out, dollars = books.parse_message(model, result.result.message, MAX_TOKENS[dag],
                                                            price_factor=books.BATCH_PRICE_FACTOR)
    except books.Refused as e:
        spent = e.cost
        try:
            by_page, t_in, t_out, dollars = _sync_retry(dag, phase, doc_id, pages)
        except books.BookError as e2:
            _fail_book(dag, doc_id, run_id, f"{phase}: {e2}")
            if hasattr(e2, "cost"):
                spent = tuple(a + b for a, b in zip(spent, e2.cost))
            return "failed", f"refused, then {e2}", spent
        _save(dag, phase, doc_id, model, by_page, t_in, t_out, dollars, run_id)
        return "refused", "refused in batch; read synchronously", tuple(
            a + b for a, b in zip(spent, (t_in, t_out, dollars)))
    except books.BookError as e:   # cut off or empty: fail the book, as the sync path does
        _fail_book(dag, doc_id, run_id, f"{phase}: {e}")
        return "failed", str(e), getattr(e, "cost", (0, 0, 0.0))
    _save(dag, phase, doc_id, model, books.pages_from(parsed, pages, missing), t_in, t_out, dollars, run_id)
    return "succeeded", None, (t_in, t_out, dollars)


# --- finish -------------------------------------------------------------------------------

def finish(dag, run_id):
    """Move books whose pages are all done (and none open) to read / transcribed."""
    busy = {d for d, _, _ in open_pages(dag)}
    moved = 0
    if dag == "read_books":
        for doc_id, _, page_count in books.books_in(["reading"]):
            if doc_id in busy:
                continue
            done = books.page_results(doc_id)
            unread = [p for p in range(1, (page_count or 0) + 1) if books.READ_MODEL not in done.get(p, {})]
            hard = [p for p, r in done.items()
                    if books.REREAD_MODEL not in r and books._hard_page(r.get(books.READ_MODEL, {}))]
            if not unread and not hard:
                books.update_book(doc_id, run_id, status="read")
                moved += 1
    else:
        for status in (["pending", "transcribing"], ["reread_names"]):
            for doc_id, _, page_count in bt.queued(status):
                if doc_id in busy:
                    continue
                done = bt.transcripts(doc_id)
                if status == ["reread_names"]:
                    left = bt.reread_pages(done, rerun=True)
                else:
                    left = ([p for p in range(1, (page_count or 0) + 1) if books.READ_MODEL not in done.get(p, {})]
                            + bt.reread_pages(done, rerun=False))
                if not left:
                    bt.update_queue(doc_id, run_id, status="transcribed")
                    moved += 1
                elif status != ["reread_names"]:
                    bt.update_queue(doc_id, run_id, status="transcribing")
    return {"finished_books": moved}


# kept for the estimate of a whole run (pipelines.md, the trigger confs)
def est_book_cost(dag, n_pages, tokens_per_page=None, reread_share=None):
    """Estimated dollars of a whole book at batch prices: every page in the first
    phase plus the expected share in the re-read."""
    share = reread_share if reread_share is not None else (
        books.REREAD_SHARE if dag == "read_books" else 63 / 380)
    return est_cost(dag, "first", n_pages, tokens_per_page) + share * est_cost(dag, "reread", n_pages, tokens_per_page)

