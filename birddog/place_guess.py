"""Rough positions for book places that matched nothing in core.location.

Claude (Sonnet, low effort) gives an approximate latitude/longitude with a
confidence and a short basis for each unmatched place in
views.books__unmatched_places (read_books and transcribe_books), about
GUESS_BATCH places per call. Answers land in core.place_guess, keyed by
core.place_key(normalized, admin_context) (migration 024), so a place is
guessed once however many books name it; a place Claude could not place is
stored with confidence 'none'. The view adds the 3 nearest core.location rows.

Run by the task guess_places at the end of read_books and transcribe_books,
and by the DAG guess_unmatched_places (dags/yaml/books.yml), each with a small
budget. These are guesses for a person to check, never links: nothing here
writes core.document_location."""

import logging

from birddog import books, pg

_logger = logging.getLogger(__name__)

GUESS_MODEL = books.READ_MODEL      # Sonnet 5.5
GUESS_BATCH = 25
CONFIDENCE = ["high", "medium", "low"]

GUESS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["places"],
    "properties": {"places": {"type": "array", "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "found", "latitude", "longitude", "confidence", "basis"],
        "properties": {
            "id": {"type": "integer"},
            "found": {"type": "boolean", "description": "false when you cannot place it at all"},
            "latitude": {"type": "number", "description": "decimal degrees; 0 when not found"},
            "longitude": {"type": "number", "description": "decimal degrees; 0 when not found"},
            "confidence": {"type": "string", "enum": CONFIDENCE},
            "basis": {"type": "string",
                      "description": "One short line: which place you take it to be and why"},
        },
    }}},
}

GUESS_PROMPT = """These are place names read from 19th-century Jewish metric books (births, \
marriages, deaths) of the Russian Empire and Poland, mostly in today's Ukraine (Kyiv, \
Podolia, Volhynia, Kherson provinces). None matched our gazetteer. For each, give the \
approximate latitude and longitude of the place it most likely is, a confidence, and a \
short basis (the modern name you identify it with and why).

Hints: "admin" is the province or district written with the name; "book" is the title of \
the book it was read from, which names the town and district of the register (a place \
is often nearby). Names may be in old orthography or an inflected case ("Каневскаго" = \
Kaniv). If a name is a region rather than a town (a province or district), give its \
administrative centre and say so. If it is not a place or you cannot tell, set found to \
false. Answer once per id.

{lines}"""


# --- pure helpers ---------------------------------------------------------------

def prompt_lines(places):
    """One JSON line per place, numbered by position in `places`."""
    import json
    return "\n".join(json.dumps({"id": i, "name": p["as_written"], "normalized": p["normalized"],
                                 "admin": p["admin_context"], "book": p["hint"]},
                                ensure_ascii=False) for i, p in enumerate(places))


def parse_answers(places, parsed):
    """[(place, latitude, longitude, confidence, basis)] for every place: an
    answer out of range, not found, or missing becomes a no-answer ('none')."""
    by_id = {}
    for a in parsed.get("places", []) if isinstance(parsed, dict) else []:
        if isinstance(a, dict) and isinstance(a.get("id"), int):
            by_id.setdefault(a["id"], a)
    out = []
    for i, place in enumerate(places):
        a = by_id.get(i)
        if a is None:
            out.append((place, None, None, "none", "not returned"))
            continue
        lat, lon = a.get("latitude"), a.get("longitude")
        basis = (a.get("basis") or "").strip()[:500]
        ok = (a.get("found") and isinstance(lat, (int, float)) and isinstance(lon, (int, float))
              and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0))
        if not ok:
            out.append((place, None, None, "none", basis or "not found"))
            continue
        conf = a.get("confidence") if a.get("confidence") in CONFIDENCE else "low"
        out.append((place, round(float(lat), 5), round(float(lon), 5), conf, basis))
    return out


# --- Postgres -------------------------------------------------------------------

def pending(limit=None):
    """Unmatched places not guessed yet: [{key, as_written, normalized, admin_context, hint}]."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT u.place_key, min(u.as_written), min(u.normalized), min(u.admin_context),
                   string_agg(DISTINCT replace(replace(u.title, 'File:', ''), '_', ' '), ' | ')
            FROM views.books__unmatched_places u
            WHERE NOT EXISTS (SELECT 1 FROM core.place_guess g WHERE g.key = u.place_key)
            GROUP BY u.place_key
            ORDER BY count(*) DESC, u.place_key
            LIMIT %s""", (limit,))
        return [{"key": k, "as_written": aw or "", "normalized": n or "", "admin_context": ac or "",
                 "hint": (h or "")[:300]} for k, aw, n, ac, h in cur.fetchall()]


def save(answers, run_id, tokens_in, tokens_out, dollars):
    n = max(1, len(answers))
    with pg.connect() as conn, conn.cursor() as cur:
        for place, lat, lon, conf, basis in answers:
            cur.execute("""
                INSERT INTO core.place_guess (key, as_written, normalized, admin_context, hint,
                    latitude, longitude, confidence, basis, model, input_tokens, output_tokens,
                    cost_usd, run_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (key) DO NOTHING""",
                        (place["key"], place["as_written"], place["normalized"],
                         place["admin_context"], place["hint"], lat, lon, conf, basis, GUESS_MODEL,
                         tokens_in // n, tokens_out // n, round(dollars / n, 4), run_id))


def guess_unmatched(run_id, budget_usd, limit=None):
    """Guess positions for unmatched places, a batch per call, until done or out of budget."""
    places = pending(limit)
    summary = {"places": len(places), "guessed": 0, "found": 0, "spent_usd": 0.0,
               "stopped_for_budget": False, "failed_batches": 0}
    for i in range(0, len(places), GUESS_BATCH):
        if summary["spent_usd"] >= budget_usd:
            summary["stopped_for_budget"] = True
            break
        batch = places[i:i + GUESS_BATCH]
        try:
            parsed, t_in, t_out, dollars = books.ask(
                GUESS_MODEL, [{"type": "text", "text": GUESS_PROMPT.format(lines=prompt_lines(batch))}],
                GUESS_SCHEMA, effort="low", max_tokens=16000)
        except books.BookError as e:   # refusal or cut off: try these places next run
            _logger.warning("guess batch failed: %s", e)
            summary["failed_batches"] += 1
            continue
        answers = parse_answers(batch, parsed)
        save(answers, run_id, t_in, t_out, dollars)
        summary["spent_usd"] = round(summary["spent_usd"] + dollars, 4)
        summary["guessed"] += len(answers)
        summary["found"] += sum(1 for a in answers if a[1] is not None)
    return summary
