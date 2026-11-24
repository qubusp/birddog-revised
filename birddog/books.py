"""Book reading: Claude reads the page images of multi-page archive documents
and lists the places they name (DAG dags/yaml/books.yml, tables in
deploy/db/migrations/017_book_reading.sql).

Per book, in order:
  probe     Haiku looks at a few sample pages: readable? which script and
            languages? It also measures what a page costs in tokens, which
            gives the estimate for the whole book. Over the auto limit the book
            waits in `needs_approval` for a person (Mathesar).
  read      Sonnet reads every page, a few pages per call, and lists the places
            with the words they were read from. Pages Sonnet marks illegible or
            low-confidence are read again by Opus.
  assemble  Haiku groups the spellings of the same place across pages; a fuzzy
            match links each group to core.location where it can.

Page images come from Wikimedia Commons' renderer (one page per request), so a
300 MB PDF is never downloaded. Every call's tokens and dollars are stored on
the book and the page, and a run stops reading once it has spent its budget.
Results stay in core.book_read.places: nothing writes core.document_location
until the pilot's output has been checked by hand.

The API key comes from ANTHROPIC_API_KEY or, in Airflow, the connection
`anthropic` (its password), which needs no container restart to change."""

import base64
import json
import logging
import math
import os
from urllib.parse import unquote

from birddog import pg

_logger = logging.getLogger(__name__)

PROBE_MODEL = "claude-haiku-5-5"
READ_MODEL = "claude-sonnet-5-5"
REREAD_MODEL = "claude-opus-5-5"
ASSEMBLE_MODEL = "claude-haiku-5-5"

# $ per million tokens (input, output); Anthropic list prices, 2026-11
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-4-8": (5.00, 25.00),
}
# models that get the server-side refusal fallback
_FALLBACK_MODELS = {READ_MODEL, REREAD_MODEL}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

AUTO_LIMIT_TOKENS = 500_000    # a book estimated above this waits for approval
PAGES_PER_CALL = 4             # page images per extraction call
PAGE_WIDTH = 1600              # px, Commons renders the page at (about) this width
SAMPLE_PAGES = 3               # probe sample
# estimate per page beyond its image: output incl. thinking, and a share of
# the prompt; refined once the pilot has real numbers
EST_OUTPUT_PER_PAGE = 500
EST_PROMPT_PER_CALL = 1500
MATCH_THRESHOLD = 0.91         # Jaro-Winkler, as birddog.locations.matcher

COMMONS_API = "https://commons.wikimedia.org/w/api.php"


class BookError(RuntimeError):
    """A book-level failure worth recording on the book (then move on)."""


class BudgetExhausted(RuntimeError):
    """This run spent its budget; remaining work waits for the next run."""


# --- Claude ------------------------------------------------------------------

def api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        try:
            from airflow.sdk import BaseHook
        except ImportError:
            from airflow.hooks.base import BaseHook
        return BaseHook.get_connection("anthropic").password
    except Exception as e:  # no Airflow, or no such connection
        raise BookError("no Anthropic key: set ANTHROPIC_API_KEY or the Airflow connection 'anthropic'") from e


_client = None


def client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=api_key(), max_retries=4, timeout=600)
    return _client


def cost_usd(model, input_tokens, output_tokens):
    price_in, price_out = PRICES.get(model, PRICES[REREAD_MODEL])  # unknown: assume the dearest
    return (input_tokens * price_in + output_tokens * price_out) / 1e6


def _input_tokens(usage):
    return ((usage.input_tokens or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0))


def ask(model, content, schema, effort, max_tokens=16000):
    """One structured call. Returns (parsed JSON, input tokens, output tokens, $).
    Raises BookError on a refusal or a cut-off answer."""
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    if model in _FALLBACK_MODELS:
        response = client().beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **kwargs)
    else:
        response = client().messages.create(**kwargs)
    tokens_in, tokens_out = _input_tokens(response.usage), response.usage.output_tokens or 0
    # the fallback may have answered on another model: price what ran
    dollars = cost_usd(response.model or model, tokens_in, tokens_out)
    if response.stop_reason == "refusal":
        raise BookError(f"{model} declined the request")
    if response.stop_reason == "max_tokens":
        raise BookError(f"{model} answer cut off at max_tokens={max_tokens}")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise BookError(f"{model} returned no text block")
    return json.loads(text), tokens_in, tokens_out, dollars


def _image_block(jpeg):
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.standard_b64encode(jpeg).decode("ascii")}}


def _pages_content(images, prompt):
    """[label, image, label, image, ..., prompt]: each image after its page label."""
    content = []
    for page_no, jpeg in images:
        content.append({"type": "text", "text": f"Page {page_no}:"})
        content.append(_image_block(jpeg))
    content.append({"type": "text", "text": prompt})
    return content


# --- Commons -------------------------------------------------------------------

def file_title(url):
    """'File:...' from a Commons file page URL (/wiki/File:... or, in some
    imported records, /File:... without /wiki/)."""
    path = url.split("/wiki/", 1)[1] if "/wiki/" in url else url.split("://", 1)[-1].split("/", 1)[-1]
    title = unquote(path).split("?", 1)[0].split("#", 1)[0]
    if not title.startswith("File:"):
        raise BookError(f"not a wiki file URL: {url}")
    return title


def page_image(title, page_no, width=PAGE_WIDTH):
    """JPEG bytes of one rendered page, via Commons' thumbnail renderer."""
    from birddog.fetch import fetch_url
    data = fetch_url(COMMONS_API, params={
        "action": "query", "format": "json", "prop": "imageinfo", "titles": title,
        "iiprop": "url|size", "iiurlwidth": width, "iiurlparam": f"page{page_no}-{width}px",
    }, return_json=True)
    pages = (data.get("query") or {}).get("pages") or {}
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("thumburl"):
            return fetch_url(info["thumburl"], content=True, timeout=60)
        if "missing" in page:
            raise BookError(f"{title} is missing on Commons")
    raise BookError(f"Commons gave no page render for {title} page {page_no}")


# --- probe -------------------------------------------------------------------

PROBE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["readable", "legibility", "script", "languages", "document_kind",
                 "has_place_names", "notes"],
    "properties": {
        "readable": {"type": "boolean",
                     "description": "Can a careful reader make out most of the text?"},
        "legibility": {"type": "integer",
                       "description": "1 = unusable, 2 = mostly unreadable, 3 = hard, 4 = fair, 5 = clear"},
        "script": {"type": "string", "enum": ["printed", "typed", "handwritten", "mixed"]},
        "languages": {"type": "array", "items": {"type": "string"}},
        "document_kind": {"type": "string"},
        "has_place_names": {"type": "boolean"},
        "notes": {"type": "string", "description": "One or two sentences for the person deciding."},
    },
}

PROBE_PROMPT = """These are sample page scans from one archival book (a scanned PDF from \
Ukrainian archives on Wikimedia Commons; title: {title}). Judge whether the book is worth \
reading page by page to collect the place names it mentions (villages, towns, districts, \
provinces). Look at scan quality and handwriting. Answer in the required JSON."""


def sample_pages(page_count, n=SAMPLE_PAGES):
    """n distinct pages spread through the book (skipping the cover when possible)."""
    if page_count <= n:
        return list(range(1, page_count + 1))
    picks = {min(page_count, max(2, round(page_count * f))) for f in (0.15, 0.5, 0.85)[:n]}
    for p in range(2, page_count + 1):
        if len(picks) >= n:
            break
        picks.add(p)
    return sorted(picks)[:n]


def estimate(page_count, tokens_per_page):
    """(tokens, dollars) to read a book with READ_MODEL."""
    calls = math.ceil(page_count / PAGES_PER_CALL)
    tokens_in = page_count * tokens_per_page + calls * EST_PROMPT_PER_CALL
    tokens_out = page_count * EST_OUTPUT_PER_PAGE
    return tokens_in + tokens_out, round(cost_usd(READ_MODEL, tokens_in, tokens_out), 2)


def decide(probe, est_tokens, auto_limit=AUTO_LIMIT_TOKENS):
    """The book's next status after the probe."""
    if not probe.get("readable") or int(probe.get("legibility") or 0) <= 1:
        return "unreadable"
    return "approved" if est_tokens <= auto_limit else "needs_approval"


def probe_book(doc_id, title_url, page_count, auto_limit=AUTO_LIMIT_TOKENS):
    """Probe one book. Returns a dict of the book_read columns to set."""
    title = file_title(title_url)
    pages = sample_pages(page_count)
    images = [(p, page_image(title, p)) for p in pages]
    prompt = PROBE_PROMPT.format(title=title)
    probe, tokens_in, tokens_out, dollars = ask(
        PROBE_MODEL, _pages_content(images, prompt), PROBE_SCHEMA, effort="low", max_tokens=4000)
    # what the text alone costs, to isolate the images' share
    text_only = client().messages.count_tokens(
        model=PROBE_MODEL, messages=[{"role": "user", "content": [
            {"type": "text", "text": f"Page {p}:"} for p in pages] + [{"type": "text", "text": prompt}]}])
    tokens_per_page = max(1, (tokens_in - text_only.input_tokens) // len(pages))
    est_tokens, est_dollars = estimate(page_count, tokens_per_page)
    status = decide(probe, est_tokens, auto_limit)
    _logger.info("probe %s: %s, %d pages x %d tok, est %d tok ($%.2f)",
                 doc_id, status, page_count, tokens_per_page, est_tokens, est_dollars)
    return {"status": status, "probe": probe, "tokens_per_page": tokens_per_page,
            "est_tokens": est_tokens, "est_cost_usd": est_dollars,
            "input_tokens": tokens_in, "output_tokens": tokens_out, "cost_usd": dollars}


# --- read ----------------------------------------------------------------------

READ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pages"],
    "properties": {
        "pages": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["page", "illegible", "places", "notes"],
            "properties": {
                "page": {"type": "integer"},
                "illegible": {"type": "boolean",
                              "description": "true when most of the page cannot be read"},
                "notes": {"type": "string"},
                "places": {"type": "array", "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["as_written", "normalized", "kind", "admin_context", "quote",
                                 "confidence"],
                    "properties": {
                        "as_written": {"type": "string",
                                       "description": "The place name exactly as on the page"},
                        "normalized": {"type": "string",
                                       "description": "Standard modern spelling (Ukrainian if in Ukraine), nominative case"},
                        "kind": {"type": "string", "enum": [
                            "event_place", "origin_or_registration", "residence",
                            "institution", "other"]},
                        "admin_context": {"type": "string",
                                          "description": "Province/district given with it, or empty"},
                        "quote": {"type": "string",
                                  "description": "The few words around it, transcribed"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                }},
            },
        }},
    },
}

READ_PROMPT = """These page scans come from one archival book: {title}. It is probably a \
19th or early 20th century register from the Russian Empire or Poland, in Russian, \
Ukrainian, Polish, Latin, Yiddish or Hebrew, handwritten into printed forms.

For every page shown, list each place name written on it: where an event happened, where \
a person is from or registered (e.g. "Житомирской губ. Острожской мещанинъ" means a burgher \
registered in Ostroh, Zhytomyr province), where people live, and named institutions tied \
to a place. Skip personal names, dates and printed form headings unless the heading names \
a place. Give the spelling exactly as written and a normalized modern spelling, the \
administrative context written with it, a short transcribed quote, and your confidence. \
If the same place appears several times on a page, list it once per distinct role. Mark \
a page illegible when most of it cannot be read. Answer in the required JSON, one entry \
per page shown, using the page numbers given."""


def _hard_page(result):
    return result.get("illegible") or any(p.get("confidence") == "low" for p in result.get("places", []))


def read_pages(title, page_numbers, model, effort):
    """One call over a few pages. Returns ({page_no: result}, tokens_in, tokens_out, $)."""
    images = [(p, page_image(title, p)) for p in page_numbers]
    parsed, tokens_in, tokens_out, dollars = ask(
        model, _pages_content(images, READ_PROMPT.format(title=title)), READ_SCHEMA,
        effort=effort, max_tokens=32000)
    by_page = {}
    for entry in parsed.get("pages", []):
        if entry.get("page") in page_numbers:
            by_page[entry["page"]] = entry
    for p in page_numbers:  # a page the model skipped counts as illegible, not as empty
        by_page.setdefault(p, {"page": p, "illegible": True, "places": [], "notes": "not returned"})
    return by_page, tokens_in, tokens_out, dollars


# --- assemble --------------------------------------------------------------------

ASSEMBLE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["places"],
    "properties": {"places": {"type": "array", "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["name_uk", "name_en", "admin_context", "mention_ids"],
        "properties": {
            "name_uk": {"type": "string", "description": "Modern Ukrainian name (or the local official name outside Ukraine)"},
            "name_en": {"type": "string", "description": "Modern English/Latin-script name"},
            "admin_context": {"type": "string", "description": "Historical province/district if known"},
            "mention_ids": {"type": "array", "items": {"type": "integer"}},
        },
    }}},
}

ASSEMBLE_PROMPT = """Below are place-name mentions read from one archival book ({title}), \
as JSON lines: id, spelling as written, normalized spelling, administrative context, count. \
Group the mentions that name the same real place (different cases, old orthography, \
Russian/Ukrainian/Polish/Yiddish variants, abbreviations). Every id must appear in exactly \
one group. For each group give the modern Ukrainian name, a modern English name, and the \
historical administrative context. Do not invent places that are not in the list.

{lines}"""


def collect_mentions(page_results):
    """Distinct mentions across pages: [{id, as_written, normalized, admin_context,
    kinds, pages, count}], from {page_no: result} (one reading per page)."""
    merged = {}
    for page_no in sorted(page_results):
        for place in page_results[page_no].get("places", []):
            key = (place.get("as_written", "").strip(), place.get("normalized", "").strip(),
                   place.get("admin_context", "").strip())
            if not any(key):
                continue
            entry = merged.setdefault(key, {"as_written": key[0], "normalized": key[1],
                                            "admin_context": key[2], "kinds": set(),
                                            "pages": set(), "count": 0})
            entry["kinds"].add(place.get("kind", "other"))
            entry["pages"].add(page_no)
            entry["count"] += 1
    mentions = []
    for i, entry in enumerate(merged.values()):
        mentions.append({"id": i, **entry, "kinds": sorted(entry["kinds"]),
                         "pages": sorted(entry["pages"])})
    return mentions


def group_mentions(mentions, groups):
    """Places from Haiku's groups; a mention it left out becomes its own place."""
    by_id = {m["id"]: m for m in mentions}
    places, used = [], set()
    for group in groups:
        members = [by_id[i] for i in group.get("mention_ids", []) if i in by_id and i not in used]
        if not members:
            continue
        used.update(m["id"] for m in members)
        places.append(_place(group.get("name_uk", ""), group.get("name_en", ""),
                             group.get("admin_context", ""), members))
    for m in mentions:
        if m["id"] not in used:
            places.append(_place(m["normalized"] or m["as_written"], "", m["admin_context"], [m]))
    places.sort(key=lambda p: -p["mentions"])
    return places


def _place(name_uk, name_en, admin_context, members):
    return {
        "name_uk": name_uk,
        "name_en": name_en,
        "admin_context": admin_context,
        "variants": sorted({m["as_written"] for m in members if m["as_written"]}),
        "kinds": sorted({k for m in members for k in m["kinds"]}),
        "pages": sorted({p for m in members for p in m["pages"]}),
        "mentions": sum(m["count"] for m in members),
    }


def load_locations(cur):
    """[(normalized name, location_id, modern name)] for every name variant in core.location."""
    import re
    from birddog.locations.matcher import normalize_name
    cur.execute("SELECT location_id, modern_location_name, alternate_names FROM core.location")
    names = []
    for location_id, modern, alternates in cur.fetchall():
        # alternate_names reads "Okhtyrka [Ukr], Akhtyrka [Rus], Akhtirka": drop the tags
        alternates = re.sub(r"\[[^\]]*\]", "", alternates or "")
        variants = [modern or ""] + alternates.replace(";", ",").split(",")
        for v in variants:
            n = normalize_name(v) if v and v.strip() else ""
            if n:
                names.append((n, location_id, modern))
    return names


def match_places(places, location_names, threshold=MATCH_THRESHOLD):
    """Adds location_id / location_name / match_score to each place in place."""
    if not location_names:
        return places
    from rapidfuzz import process
    from rapidfuzz.distance import JaroWinkler
    from birddog.locations.matcher import normalize_name
    choices = [n for n, _, _ in location_names]
    for place in places:
        best = None
        for candidate in [place["name_en"], place["name_uk"], *place["variants"]]:
            if not candidate:
                continue
            hit = process.extractOne(normalize_name(candidate), choices,
                                     scorer=JaroWinkler.similarity, score_cutoff=threshold)
            if hit and (best is None or hit[1] > best[1]):
                best = hit
        if best:
            _, location_id, modern = location_names[best[2]]
            place.update(location_id=location_id, location_name=modern, match_score=round(best[1], 3))
        else:
            place.update(location_id=None, location_name=None, match_score=None)
    return places


def assemble(title, page_results, location_names):
    """({places, summary}, tokens_in, tokens_out, $) for a fully read book."""
    mentions = collect_mentions(page_results)
    tokens_in = tokens_out = 0
    dollars = 0.0
    if mentions:
        lines = "\n".join(json.dumps({"id": m["id"], "as_written": m["as_written"],
                                      "normalized": m["normalized"],
                                      "admin_context": m["admin_context"], "count": m["count"]},
                                     ensure_ascii=False) for m in mentions)
        parsed, tokens_in, tokens_out, dollars = ask(
            ASSEMBLE_MODEL, [{"type": "text", "text": ASSEMBLE_PROMPT.format(title=title, lines=lines)}],
            ASSEMBLE_SCHEMA, effort="low", max_tokens=32000)
        places = group_mentions(mentions, parsed.get("places", []))
    else:
        places = []
    match_places(places, location_names)
    illegible = sorted(p for p, r in page_results.items() if r.get("illegible"))
    summary = {"pages_read": len(page_results), "illegible_pages": illegible,
               "mentions": sum(m["count"] for m in mentions), "places": len(places),
               "matched": sum(1 for p in places if p.get("location_id"))}
    return {"places": places, "summary": summary}, tokens_in, tokens_out, dollars


# --- Postgres: queue and bookkeeping ----------------------------------------------

def enqueue(limit, doc_type="Vitals", min_pages=2, max_pages=None):
    """Queue up to `limit` PDF books of `doc_type` that were never queued."""
    if not limit:
        return 0
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO core.book_read (document_id, page_count)
            SELECT d.id, d.page_count FROM core.document d
            WHERE d.mimetype = 'application/pdf'
              AND d.domain = 'commons.wikimedia.org'
              AND %(doc_type)s = ANY(d.doc_type)
              AND d.page_count >= %(min_pages)s
              AND (%(max_pages)s::int IS NULL OR d.page_count <= %(max_pages)s::int)
              AND NOT EXISTS (SELECT 1 FROM core.book_read b WHERE b.document_id = d.id)
            ORDER BY d.id
            LIMIT %(limit)s
            ON CONFLICT DO NOTHING""",
                    {"doc_type": doc_type, "min_pages": min_pages, "max_pages": max_pages,
                     "limit": limit})
        return cur.rowcount


def books_in(status, limit=None):
    """[(document_id, url, page_count)] in `status`, oldest first."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT b.document_id, d.url, coalesce(b.page_count, d.page_count)
                       FROM core.book_read b JOIN core.document d ON d.id = b.document_id
                       WHERE b.status = ANY(%s) ORDER BY b.queued_at, b.document_id
                       LIMIT %s""",
                    (list(status) if isinstance(status, (list, tuple)) else [status], limit))
        return cur.fetchall()


def update_book(doc_id, run_id=None, add_tokens=(0, 0, 0.0), **columns):
    """Set columns on a book and add to its token and dollar totals."""
    sets, values = ["input_tokens = input_tokens + %s", "output_tokens = output_tokens + %s",
                    "cost_usd = cost_usd + %s"], list(add_tokens)
    for name, value in columns.items():
        sets.append(f"{name} = %s")
        values.append(json.dumps(value, ensure_ascii=False) if name in ("probe", "places") and value is not None else value)
    if run_id is not None:
        sets.append("run_id = %s")
        values.append(run_id)
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE core.book_read SET {', '.join(sets)} WHERE document_id = %s",
                    values + [doc_id])


def save_pages(doc_id, model, by_page, tokens_in, tokens_out, dollars):
    """Store one call's pages; the call's cost is split evenly across them."""
    n = max(1, len(by_page))
    with pg.connect() as conn, conn.cursor() as cur:
        for page_no, result in by_page.items():
            cur.execute("""INSERT INTO core.book_page (document_id, page_no, model, illegible, result,
                                                       input_tokens, output_tokens, cost_usd)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (document_id, page_no, model) DO UPDATE
                           SET illegible = EXCLUDED.illegible, result = EXCLUDED.result,
                               input_tokens = EXCLUDED.input_tokens,
                               output_tokens = EXCLUDED.output_tokens,
                               cost_usd = EXCLUDED.cost_usd, created_at = now()""",
                        (doc_id, page_no, model, bool(result.get("illegible")),
                         json.dumps(result, ensure_ascii=False),
                         tokens_in // n, tokens_out // n, round(dollars / n, 4)))


def page_results(doc_id):
    """{page_no: {model: result}}"""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT page_no, model, result FROM core.book_page WHERE document_id = %s",
                    (doc_id,))
        out = {}
        for page_no, model, result in cur.fetchall():
            out.setdefault(page_no, {})[model] = result
        return out


def best_reading(readings):
    """The re-read wins over the first reading."""
    return readings.get(REREAD_MODEL) or readings.get(READ_MODEL) or next(iter(readings.values()))


def run_spend(run_id):
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT coalesce(sum(cost_usd), 0) FROM core.book_read WHERE run_id = %s",
                    (run_id,))
        return float(cur.fetchone()[0])


# --- the three steps, over the queue ---------------------------------------------

def probe_pending(run_id, limit=None, auto_limit=AUTO_LIMIT_TOKENS):
    summary = {"probed": 0, "approved": 0, "needs_approval": 0, "unreadable": 0, "failed": 0}
    for doc_id, url, page_count in books_in("pending", limit):
        try:
            if not page_count:
                raise BookError("page_count unknown")
            result = probe_book(doc_id, url, page_count, auto_limit)
            tokens = (result.pop("input_tokens"), result.pop("output_tokens"), result.pop("cost_usd"))
            update_book(doc_id, run_id, tokens, error=None, **result)
            summary[result["status"]] += 1
        except BookError as e:
            update_book(doc_id, run_id, status="failed", error=f"probe: {e}")
            summary["failed"] += 1
        summary["probed"] += 1
    return summary


def read_approved(run_id, budget_usd):
    """Read approved books (and resume interrupted ones) until done or out of budget."""
    summary = {"books_read": 0, "pages": 0, "reread_pages": 0, "failed": 0, "stopped_for_budget": False}
    for doc_id, url, page_count in books_in(["approved", "reading"]):
        try:
            update_book(doc_id, run_id, status="reading", error=None)
            title = file_title(url)
            done = page_results(doc_id)
            todo = [p for p in range(1, page_count + 1) if READ_MODEL not in done.get(p, {})]
            for i in range(0, len(todo), PAGES_PER_CALL):
                if run_spend(run_id) >= budget_usd:
                    raise BudgetExhausted()
                chunk = todo[i:i + PAGES_PER_CALL]
                by_page, t_in, t_out, dollars = read_pages(title, chunk, READ_MODEL, effort="medium")
                save_pages(doc_id, READ_MODEL, by_page, t_in, t_out, dollars)
                update_book(doc_id, run_id, (t_in, t_out, dollars))
                summary["pages"] += len(chunk)
            done = page_results(doc_id)
            hard = [p for p, r in sorted(done.items())
                    if REREAD_MODEL not in r and _hard_page(r.get(READ_MODEL, {}))]
            for i in range(0, len(hard), PAGES_PER_CALL):
                if run_spend(run_id) >= budget_usd:
                    raise BudgetExhausted()
                chunk = hard[i:i + PAGES_PER_CALL]
                by_page, t_in, t_out, dollars = read_pages(title, chunk, REREAD_MODEL, effort="high")
                save_pages(doc_id, REREAD_MODEL, by_page, t_in, t_out, dollars)
                update_book(doc_id, run_id, (t_in, t_out, dollars))
                summary["reread_pages"] += len(chunk)
            update_book(doc_id, run_id, status="read")
            summary["books_read"] += 1
        except BudgetExhausted:
            summary["stopped_for_budget"] = True
            _logger.info("run %s reached its $%.2f budget; book %s resumes next run",
                         run_id, budget_usd, doc_id)
            break
        except BookError as e:
            update_book(doc_id, run_id, status="failed", error=f"read: {e}")
            summary["failed"] += 1
    return summary


def assemble_read(run_id):
    summary = {"assembled": 0, "failed": 0}
    with pg.connect() as conn, conn.cursor() as cur:
        location_names = load_locations(cur)
    for doc_id, url, _ in books_in("read"):
        try:
            readings = {p: best_reading(r) for p, r in page_results(doc_id).items()}
            result, t_in, t_out, dollars = assemble(file_title(url), readings, location_names)
            update_book(doc_id, run_id, (t_in, t_out, dollars), status="done", places=result, error=None)
            summary["assembled"] += 1
        except BookError as e:
            update_book(doc_id, run_id, status="failed", error=f"assemble: {e}")
            summary["failed"] += 1
    return summary
