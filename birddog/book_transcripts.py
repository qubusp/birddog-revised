"""Book transcription: Claude transcribes every page of a book in full (DAG
transcribe_books in dags/yaml/books.yml, tables in
deploy/db/migrations/023_book_transcripts.sql).

Same models and rules as the place reading in birddog/books.py, whose helpers
this module reuses (page images, ask, the re-read rule, place matching):

  transcribe  Sonnet transcribes every page, TRANSCRIBE_PAGES_PER_CALL pages
              per call: the full text as written, one record per register
              entry (people, dates, places) and the place list of read_books.
              Opus transcribes a page again, with NAMES_PROMPT added, when
              Sonnet marked it illegible or left its names unreadable
              (needs_reread). Stored per page and model in
              core.book_transcript as they finish, so a stopped book resumes.
  index       Flattens each page's best reading (Opus over Sonnet) into
              core.book_entry and core.book_entry_place, matching entry places
              to core.location. No API calls; rebuilt per book on each run.

The queue is core.book_transcript_queue, separate from core.book_read, so the
place pilot's rows stay as they are. Status reread_names (DAG param
reread_names) re-runs only the Opus step on a finished book's pages whose best
reading still needs it and was not yet read with NAMES_PROMPT; then index.

Why the names rule: in the pilot Sonnet left 332 of 2,315 entries (14%) with
no readable name. On a test page (book 2581, page 3) Sonnet read 0 of 11 names
at every resolution, and Opus with NAMES_PROMPT read 11 of 11 on the same
1600 px Commons render ($0.14): the model and the prompt matter, not the scan. A run stops transcribing once it has spent
its budget; the next run resumes.

Output is much longer than the place reading (362 output tokens per page in the
pilot): the transcription and the entries run to a few thousand tokens per
page, so calls stream and carry fewer pages."""

import json
import logging
import re

from birddog import books, pg

_logger = logging.getLogger(__name__)

TRANSCRIBE_PAGES_PER_CALL = 2   # output per page is ~10x the place reading's
TRANSCRIBE_MAX_TOKENS = 64000   # streamed; a dense two-page call stays well under
YEAR_MIN, YEAR_MAX = 1700, 1950

# Opus re-reads a page for its names when at least NAMES_REREAD_MIN_ENTRIES of
# its entries, or at least NAMES_REREAD_MIN_SHARE of them, have no readable
# person. On the pilot's 380 pages this picks 63 (17%), holding 291 of the 332
# unreadable entries; "any unreadable entry" would pick 104 (27%).
NAMES_REREAD_MIN_ENTRIES = 2
NAMES_REREAD_MIN_SHARE = 0.5
NAMES_PROMPT_KEY = "names"      # core.book_transcript.prompt for pages read with it

EVENTS = ["birth", "marriage", "death", "divorce", "other"]

_PLACE = books.READ_SCHEMA["properties"]["pages"]["items"]["properties"]["places"]

TRANSCRIBE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pages"],
    "properties": {
        "pages": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["page", "illegible", "text", "entries", "places", "notes"],
            "properties": {
                "page": {"type": "integer"},
                "illegible": {"type": "boolean",
                              "description": "true when most of the page cannot be read"},
                "text": {"type": "string",
                         "description": "Full transcription as written, original script and spelling, line breaks kept, [...] for illegible spans"},
                "entries": {"type": "array", "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["entry_no", "event", "date_text", "date_iso", "person",
                                 "father", "mother", "others", "places", "note"],
                    "properties": {
                        "entry_no": {"type": "string", "description": "Entry number as written, or empty"},
                        "event": {"type": "string", "enum": EVENTS},
                        "date_text": {"type": "string", "description": "The date as written"},
                        "date_iso": {"type": "string",
                                     "description": "YYYY-MM-DD, YYYY-MM or YYYY when certain (Julian as written), else empty"},
                        "person": {"type": "string",
                                   "description": "The person the entry is about (child, deceased, groom and bride as 'A & B'), as written"},
                        "father": {"type": "string"},
                        "mother": {"type": "string"},
                        "others": {"type": "array", "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name", "role"],
                            "properties": {
                                "name": {"type": "string"},
                                "role": {"type": "string",
                                         "description": "spouse, witness, rabbi, mohel, godparent, informant, ..."},
                            },
                        }},
                        "places": {"type": "array", "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["as_written", "normalized", "name_en", "role"],
                            "properties": {
                                "as_written": {"type": "string"},
                                "normalized": {"type": "string",
                                               "description": "Modern spelling, Ukrainian if in Ukraine, nominative case"},
                                "name_en": {"type": "string",
                                            "description": "English/Latin spelling as in gazetteers (e.g. Kaniv), or empty"},
                                "role": {"type": "string",
                                         "description": "event_place, origin_or_registration, residence, burial, other"},
                            },
                        }},
                        "note": {"type": "string",
                                 "description": "One short line: cause of death, age, remarks"},
                    },
                }},
                "places": _PLACE,
                "notes": {"type": "string"},
            },
        }},
    },
}

TRANSCRIBE_PROMPT = """These page scans come from one archival book: {title}. It is probably a \
19th or early 20th century metric register from the Russian Empire or Poland (births, \
marriages, deaths, divorces), in Russian, Ukrainian, Polish, Latin, Hebrew or Yiddish, \
handwritten into printed forms.

For every page shown:
1. text: transcribe the whole page as written. Keep the original script and spelling, \
including pre-1918 letters (ѣ, і, ъ, ѳ) and Hebrew or Yiddish as written; do not translate \
or modernize. Follow the reading order of the form across a two-page spread, keep line \
breaks, separate columns with " | ", and write [...] for a span you cannot read. Include \
printed headings once.
2. entries: one record per register entry: its number as written, the kind of event, the \
date as written plus an ISO date only when you are certain of it, the person the entry \
is about, father, mother, everyone else named with their role, the places named with \
their role, and a short note (age, cause of death, remarks). Names as written.
3. places: each place name on the page, as for a gazetteer: spelling as written, a \
normalized modern spelling, the administrative context written with it, a short quote, \
your confidence; once per distinct role.
Mark a page illegible when most of it cannot be read. Answer in the required JSON, one \
entry per page shown, using the page numbers given."""

# added for the Opus re-read (the experiment on book 2581: 11 of 11 names, Sonnet 0)
NAMES_PROMPT = """

Names matter most. For every name, give your BEST reading even when unsure, marking each \
uncertain letter or word with ? (e.g. "Сав? Хаим?въ"), instead of [...]. Use [...] only \
for a word you cannot read at all. Many registers repeat each entry in Hebrew or Yiddish \
on the facing page: use it to check and complete the Russian names, and give the Hebrew \
form in the entry's note."""


# --- Claude --------------------------------------------------------------------

def transcribe_pages(title, page_numbers, model, effort, names=False):
    """One call over a few pages. Returns ({page_no: result}, tokens_in, tokens_out, $).
    `names` adds NAMES_PROMPT (best reading of every name, uncertain letters marked ?)."""
    images = [(p, books.page_image(title, p)) for p in page_numbers]
    prompt = TRANSCRIBE_PROMPT.format(title=title) + (NAMES_PROMPT if names else "")
    parsed, tokens_in, tokens_out, dollars = books.ask(
        model, books._pages_content(images, prompt),
        TRANSCRIBE_SCHEMA, effort=effort, max_tokens=TRANSCRIBE_MAX_TOKENS, stream=True)
    by_page = {}
    for entry in parsed.get("pages", []):
        if entry.get("page") in page_numbers:
            by_page[entry["page"]] = entry
    for p in page_numbers:  # a page the model skipped counts as illegible, not as empty
        by_page.setdefault(p, {"page": p, "illegible": True, "text": "", "entries": [],
                               "places": [], "notes": "not returned"})
    return by_page, tokens_in, tokens_out, dollars


# --- which pages Opus re-reads (pure) ------------------------------------------------

_UNREADABLE = re.compile(r"\s*((\[(?:\.\.\.|…)\]|\?+)\s*)*")


def unreadable_name(person):
    """True when nothing of the name was read: empty, or only [...] gaps or ?.
    "Сура?" and "Аврум Бей?[...]" are readable (uncertain). Same rule as
    core.name_unreadable() (migration 026)."""
    return _UNREADABLE.fullmatch(person if isinstance(person, str) else "") is not None


def unreadable_entries(result):
    """(entries, entries whose person is unreadable) on one page reading."""
    entries = [e for e in (result.get("entries") or []) if isinstance(e, dict)]
    return len(entries), sum(1 for e in entries if unreadable_name(e.get("person")))


def names_unreadable(result):
    n, bad = unreadable_entries(result)
    return bad >= NAMES_REREAD_MIN_ENTRIES or (n > 0 and bad / n >= NAMES_REREAD_MIN_SHARE)


def needs_reread(result):
    """Does Opus transcribe this page again? Illegible (books._hard_page), or
    its names came back unreadable (names_unreadable)."""
    return bool(books._hard_page(result) or names_unreadable(result))


def reread_pages(page_results, rerun=False):
    """Pages for the Opus step, from {page_no: {model: {illegible, entries, prompt}}}.
    Normal run: Sonnet's reading needs it and Opus has not read the page.
    Re-run (rerun=True): the best reading still needs it and Opus has not yet
    read the page with NAMES_PROMPT."""
    pages = []
    for page_no, readings in sorted(page_results.items()):
        opus = readings.get(books.REREAD_MODEL)
        if rerun:
            if opus and opus.get("prompt") == NAMES_PROMPT_KEY:
                continue
            best = opus or readings.get(books.READ_MODEL) or {}
        else:
            if opus:
                continue
            best = readings.get(books.READ_MODEL) or {}
        if needs_reread(best):
            pages.append(page_no)
    return pages


# --- flattening (pure) ------------------------------------------------------------

_ISO = re.compile(r"^(\d{4})(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?$")
_YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def clean_iso(value):
    """The ISO date if it is a well-formed YYYY, YYYY-MM or YYYY-MM-DD in range, else None."""
    value = (value or "").strip()
    m = _ISO.match(value)
    if not m or not YEAR_MIN <= int(m.group(1)) <= YEAR_MAX:
        return None
    return value


def entry_year(date_iso, date_text):
    """From the ISO date, else from date_text when it names exactly one plausible year."""
    iso = clean_iso(date_iso)
    if iso:
        return int(iso[:4])
    years = {int(y) for y in _YEAR.findall(date_text or "") if YEAR_MIN <= int(y) <= YEAR_MAX}
    return years.pop() if len(years) == 1 else None


def best_readings(page_results):
    """{page_no: (model, result)}: the re-read wins over the first reading."""
    out = {}
    for page_no, readings in page_results.items():
        for model in (books.REREAD_MODEL, books.READ_MODEL):
            if model in readings:
                out[page_no] = (model, readings[model])
                break
        else:
            model = next(iter(readings))
            out[page_no] = (model, readings[model])
    return out


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def flatten_entries(page_results):
    """Rows for core.book_entry (each with its `places`) from {page_no: {model: result}}."""
    rows = []
    for page_no, (model, result) in sorted(best_readings(page_results).items()):
        for idx, entry in enumerate(result.get("entries") or [], start=1):
            if not isinstance(entry, dict):
                continue
            event = entry.get("event") if entry.get("event") in EVENTS else "other"
            places = [{"as_written": _text(p.get("as_written")), "normalized": _text(p.get("normalized")),
                       "name_en": _text(p.get("name_en")), "role": _text(p.get("role"))}
                      for p in entry.get("places") or [] if isinstance(p, dict)]
            places = [p for p in places if p["as_written"] or p["normalized"] or p["name_en"]]
            others = [{"name": _text(o.get("name")), "role": _text(o.get("role"))}
                      for o in entry.get("others") or [] if isinstance(o, dict) and _text(o.get("name"))]
            rows.append({
                "page_no": page_no, "entry_idx": idx, "model": model,
                "entry_no": _text(entry.get("entry_no")) or None,
                "event": event,
                "date_text": _text(entry.get("date_text")) or None,
                "date_iso": clean_iso(entry.get("date_iso")),
                "year": entry_year(entry.get("date_iso"), entry.get("date_text")),
                "person": _text(entry.get("person")) or None,
                "father": _text(entry.get("father")) or None,
                "mother": _text(entry.get("mother")) or None,
                "others": others,
                "places": places,
                "note": _text(entry.get("note")) or None,
            })
    return rows


def match_entry_places(rows, location_names):
    """Adds location_id / match_score to each entry place, with books.match_places."""
    places = [p for row in rows for p in row["places"]]
    shaped = [{"name_en": p["name_en"], "name_uk": p["normalized"],
               "variants": [p["as_written"]] if p["as_written"] else []} for p in places]
    books.match_places(shaped, location_names)
    for p, s in zip(places, shaped):
        p["location_id"] = s.get("location_id")
        p["match_score"] = s.get("match_score")
    return rows


# --- Postgres -------------------------------------------------------------------

def enqueue(limit=0, document_ids=()):
    """Queue books for transcription: `document_ids`, plus up to `limit` books the
    place reading finished (core.book_read status done) that were never queued."""
    n = 0
    with pg.connect() as conn, conn.cursor() as cur:
        if document_ids:
            cur.execute("""
                INSERT INTO core.book_transcript_queue (document_id, page_count)
                SELECT d.id, coalesce(b.page_count, d.page_count) FROM core.document d
                LEFT JOIN core.book_read b ON b.document_id = d.id
                WHERE d.id = ANY(%s) AND coalesce(b.page_count, d.page_count) > 0
                ON CONFLICT DO NOTHING""", (list(document_ids),))
            n += cur.rowcount
        if limit:
            cur.execute("""
                INSERT INTO core.book_transcript_queue (document_id, page_count)
                SELECT b.document_id, b.page_count FROM core.book_read b
                WHERE b.status = 'done' AND b.page_count > 0
                  AND NOT EXISTS (SELECT 1 FROM core.book_transcript_queue q
                                  WHERE q.document_id = b.document_id)
                ORDER BY b.document_id
                LIMIT %s
                ON CONFLICT DO NOTHING""", (limit,))
            n += cur.rowcount
    return n


def enqueue_names_reread(document_ids):
    """Put finished books back for the names re-run (status reread_names).
    Books not transcribed yet (no queue row, or pending/transcribing/failed)
    are left alone: a normal run already applies the names rule."""
    if not document_ids:
        return 0
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE core.book_transcript_queue SET status = 'reread_names', error = NULL
                       WHERE document_id = ANY(%s) AND status IN ('transcribed', 'done', 'reread_names')""",
                    (list(document_ids),))
        return cur.rowcount


def queued(status):
    """[(document_id, url, page_count)] in `status`, oldest first."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT q.document_id, d.url, q.page_count
                       FROM core.book_transcript_queue q JOIN core.document d ON d.id = q.document_id
                       WHERE q.status = ANY(%s) ORDER BY q.queued_at, q.document_id""",
                    (list(status),))
        return cur.fetchall()


def update_queue(doc_id, run_id=None, add_tokens=(0, 0, 0.0), **columns):
    sets, values = ["input_tokens = input_tokens + %s", "output_tokens = output_tokens + %s",
                    "cost_usd = cost_usd + %s"], list(add_tokens)
    for name, value in columns.items():
        sets.append(f"{name} = %s")
        values.append(value)
    if run_id is not None:
        sets.append("run_id = %s")
        values.append(run_id)
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE core.book_transcript_queue SET {', '.join(sets)} WHERE document_id = %s",
                    values + [doc_id])


def save_transcripts(doc_id, model, by_page, tokens_in, tokens_out, dollars, prompt=None):
    """Store one call's pages; the call's cost is split evenly across them."""
    n = max(1, len(by_page))
    with pg.connect() as conn, conn.cursor() as cur:
        for page_no, r in by_page.items():
            cur.execute("""
                INSERT INTO core.book_transcript (document_id, page_no, model, illegible, text, entries,
                                                  places, notes, input_tokens, output_tokens, cost_usd,
                                                  prompt)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (document_id, page_no, model) DO UPDATE
                SET illegible = EXCLUDED.illegible, text = EXCLUDED.text, entries = EXCLUDED.entries,
                    places = EXCLUDED.places, notes = EXCLUDED.notes,
                    input_tokens = EXCLUDED.input_tokens, output_tokens = EXCLUDED.output_tokens,
                    cost_usd = EXCLUDED.cost_usd, prompt = EXCLUDED.prompt, created_at = now()""",
                        (doc_id, page_no, model, bool(r.get("illegible")), r.get("text") or "",
                         json.dumps(r.get("entries") or [], ensure_ascii=False),
                         json.dumps(r.get("places") or [], ensure_ascii=False), r.get("notes"),
                         tokens_in // n, tokens_out // n, round(dollars / n, 4), prompt))


def transcripts(doc_id):
    """{page_no: {model: {"illegible", "entries", "prompt"}}} (text left out: not needed to decide)."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT page_no, model, illegible, entries, prompt FROM core.book_transcript
                       WHERE document_id = %s""", (doc_id,))
        out = {}
        for page_no, model, illegible, entries, prompt in cur.fetchall():
            out.setdefault(page_no, {})[model] = {"illegible": illegible, "entries": entries,
                                                  "prompt": prompt}
        return out


def save_entries(doc_id, rows):
    """Replace the book's entries and their places."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM core.book_entry WHERE document_id = %s", (doc_id,))
        for row in rows:
            cur.execute("""
                INSERT INTO core.book_entry (document_id, page_no, entry_idx, model, entry_no, event,
                                             date_text, date_iso, year, person, father, mother, others,
                                             places, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                        (doc_id, row["page_no"], row["entry_idx"], row["model"], row["entry_no"],
                         row["event"], row["date_text"], row["date_iso"], row["year"], row["person"],
                         row["father"], row["mother"], json.dumps(row["others"], ensure_ascii=False),
                         json.dumps(row["places"], ensure_ascii=False), row["note"]))
            entry_id = cur.fetchone()[0]
            for i, p in enumerate(row["places"], start=1):
                cur.execute("""
                    INSERT INTO core.book_entry_place (entry_id, place_idx, as_written, normalized,
                                                       name_en, role, location_id, match_score)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                            (entry_id, i, p["as_written"], p["normalized"], p["name_en"], p["role"],
                             p.get("location_id"), p.get("match_score")))


# --- the two steps, over the queue -------------------------------------------------

def transcribe_queued(run_id, budget_usd):
    """Transcribe queued books (and resume interrupted ones), then run the
    names re-run for books in reread_names, until done or out of budget."""
    summary = {"books": 0, "pages": 0, "reread_pages": 0, "names_books": 0, "failed": 0,
               "spent_usd": 0.0, "stopped_for_budget": False}

    # this run's spend, counted here: a resumed book's queue row also holds
    # what earlier runs spent on it
    def chunks(doc_id, title, pages, model, effort, names=False):
        for i in range(0, len(pages), TRANSCRIBE_PAGES_PER_CALL):
            if summary["spent_usd"] >= budget_usd:
                raise books.BudgetExhausted()
            chunk = pages[i:i + TRANSCRIBE_PAGES_PER_CALL]
            by_page, t_in, t_out, dollars = transcribe_pages(title, chunk, model, effort, names=names)
            save_transcripts(doc_id, model, by_page, t_in, t_out, dollars,
                             prompt=NAMES_PROMPT_KEY if names else None)
            update_queue(doc_id, run_id, (t_in, t_out, dollars))
            summary["spent_usd"] = round(summary["spent_usd"] + dollars, 4)
            yield len(chunk)

    def opus_pass(doc_id, title, rerun):
        pages = reread_pages(transcripts(doc_id), rerun=rerun)
        for n in chunks(doc_id, title, pages, books.REREAD_MODEL, "high", names=True):
            summary["reread_pages"] += n

    rerun_ids = {d for d, _, _ in queued(["reread_names"])}
    for doc_id, url, page_count in queued(["pending", "transcribing", "reread_names"]):
        try:
            title = books.file_title(url)
            if doc_id in rerun_ids:
                update_queue(doc_id, run_id, error=None)   # stays reread_names until done, so it resumes
                opus_pass(doc_id, title, rerun=True)
                summary["names_books"] += 1
            else:
                update_queue(doc_id, run_id, status="transcribing", error=None)
                done = transcripts(doc_id)
                todo = [p for p in range(1, page_count + 1) if books.READ_MODEL not in done.get(p, {})]
                for n in chunks(doc_id, title, todo, books.READ_MODEL, "medium"):
                    summary["pages"] += n
                opus_pass(doc_id, title, rerun=False)
                summary["books"] += 1
            update_queue(doc_id, run_id, status="transcribed")
        except books.BudgetExhausted:
            summary["stopped_for_budget"] = True
            _logger.info("run %s reached its $%.2f budget; book %s resumes next run",
                         run_id, budget_usd, doc_id)
            break
        except books.BookError as e:
            update_queue(doc_id, run_id, status="failed", error=f"transcribe: {e}")
            summary["failed"] += 1
    return summary


def index_transcribed(run_id):
    """Flatten every transcribed book's entries into core.book_entry(_place)."""
    summary = {"books": 0, "entries": 0, "places": 0, "matched": 0}
    with pg.connect() as conn, conn.cursor() as cur:
        location_names = books.load_locations(cur)
    for doc_id, _, _ in queued(["transcribed"]):
        rows = match_entry_places(flatten_entries(transcripts(doc_id)), location_names)
        save_entries(doc_id, rows)
        update_queue(doc_id, run_id, status="done")
        summary["books"] += 1
        summary["entries"] += len(rows)
        summary["places"] += sum(len(r["places"]) for r in rows)
        summary["matched"] += sum(1 for r in rows for p in r["places"] if p.get("location_id"))
    return summary
