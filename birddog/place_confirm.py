"""Confirm places: volunteers and admins check where a book place is (page
/places/confirm, JSON under /api/places/...). Registered on the web app by
birddog/service.py (register_blueprint); never import service here, it imports
this module.

Places come from views.places__to_confirm (migration 024): every place read in
a book by read_books or transcribe_books, one card per place (core.place_key)
and book, since the same name in two books can be two villages. Three groups: unmatched (with Claude's rough position, core.place_guess), matched
(read_books' automatic match) and entry (places in transcribed register
entries). For each card a person confirms the current match, picks one of the
3 nearest known locations or any core.location row, says it is not a place,
or that they can't tell.

The rule and the linking live in the database, in app.place_decide(), per
place and book: every decision is stored; it is confirmed by the latest admin
decision or by two people agreeing; on confirmation the location is linked to
that book (core.document_location), and a link made for an earlier answer is
removed. One SQL statement under a lock on that place and book, so concurrent
volunteers cannot double-link. "Same answer for the other books" records one
more decision per ticked book; each still needs its own confirmation.

Location search accepts Cyrillic: birddog/translit.py turns it into the Latin
spellings core.location uses (Ukrainian and Russian styles).

Writes are JSON only (fetch with Content-Type application/json, as /users):
there is no CSRF token, so form posts are refused. Queries run as birddog_web
through app_db's pooled autocommit connections."""

import logging
from decimal import Decimal

from flask import Blueprint, jsonify, redirect, render_template, request, session

from birddog import app_db, translit, user as user_module

_logger = logging.getLogger(__name__)

bp = Blueprint("place_confirm", __name__, url_prefix="")

PAGE_SIZE = 20
GROUPS = ("unmatched", "matched", "entry")
SOURCES = ("read_books", "transcribe_books")
STATES = ("open", "pending", "confirmed", "all")   # open = not confirmed yet
CHOICES = ("location", "not_place", "unsure")
NOTE_MAX = 1000
OTHER_BOOKS_MAX = 100


class BadRequest(ValueError):
    """Input the API refuses with a 400 and this message."""


# --- who is asking ---------------------------------------------------------------

def current_user():
    """(email, role) of the signed-in person, or None."""
    email = (session.get("user") or {}).get("email")
    if not email:
        return None
    record = user_module.load_user_record(email)
    if not record:
        return None
    return email.lower(), "admin" if record.get("role") == "admin" else "user"


def _not_found():
    return jsonify({"error": "Not found"}), 404


# --- pure helpers (tested without a database) ---------------------------------------

def list_args(args):
    """Filters and page from the query string, with safe defaults."""
    group = args.get("group", "")
    source = args.get("source", "")
    state = args.get("state", "open")
    try:
        document_id = int(args.get("book", "")) if args.get("book", "").strip() else None
    except ValueError:
        document_id = None
    try:
        page = max(1, int(args.get("page", "1")))
    except ValueError:
        page = 1
    return {"group": group if group in GROUPS else None,
            "source": source if source in SOURCES else None,
            "state": state if state in STATES else "open",
            "document_id": document_id,
            "page": page}


def _doc_id(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        n = int(value)
    except ValueError:
        return None
    return n if n > 0 else None


def decision_args(data):
    """(place_key, document_ids, choice, location_id, note) from a decision body;
    BadRequest if invalid. document_ids is the card's book first, then the other
    books the person ticked for the same answer."""
    if not isinstance(data, dict):
        raise BadRequest("Send the decision as JSON.")
    key = data.get("place_key")
    choice = data.get("choice")
    location_id = data.get("location_id")
    note = data.get("note") or ""
    if not isinstance(key, str) or not key.strip() or "|" not in key or len(key) > 500:
        raise BadRequest("Which place? place_key is missing.")
    document_id = _doc_id(data.get("document_id"))
    if document_id is None:
        raise BadRequest("Which book? document_id is missing.")
    others = data.get("also_document_ids") or []
    if not isinstance(others, list) or len(others) > OTHER_BOOKS_MAX:
        raise BadRequest("also_document_ids must be a list of books.")
    document_ids = [document_id]
    for value in others:
        other = _doc_id(value)
        if other is None:
            raise BadRequest("also_document_ids must be a list of books.")
        if other not in document_ids:
            document_ids.append(other)
    if choice not in CHOICES:
        raise BadRequest("Choose a location, 'not a place' or 'can't tell'.")
    if choice == "location":
        if not isinstance(location_id, str) or not location_id.strip():
            raise BadRequest("Pick a location.")
        location_id = location_id.strip()
    else:
        location_id = None
    if not isinstance(note, str):
        raise BadRequest("The note must be text.")
    return key, document_ids, choice, location_id, note.strip()[:NOTE_MAX]


def plain(value):
    """JSON-friendly copy of a DB value (Decimal, nested json)."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def location_ids(cards):
    """core.location ids a page of cards shows (current match, confirmed answer,
    nearest towns), for the card maps."""
    ids = set()
    for card in cards:
        ids.update(i for i in (card.get("auto_location_id"), card.get("confirmed_location_id")) if i)
        ids.update(n["location_id"] for n in card.get("nearest") or [] if n.get("location_id"))
    return sorted(ids)


def add_coordinates(cards, coords):
    """Puts latitude/longitude from `coords` ({location_id: (lat, lon)}) on the
    current match, the confirmed answer and each nearest town; the view leaves
    them out and the card map needs them."""
    def at(location_id):
        lat, lon = coords.get(location_id) or (None, None)
        return plain(lat), plain(lon)
    for card in cards:
        card["auto_latitude"], card["auto_longitude"] = at(card.get("auto_location_id"))
        card["confirmed_latitude"], card["confirmed_longitude"] = at(card.get("confirmed_location_id"))
        for n in card.get("nearest") or []:
            n["latitude"], n["longitude"] = at(n.get("location_id"))
    return cards


# --- database ---------------------------------------------------------------------

_CARD_COLUMNS = ("place_key", "document_id", "title", "place_group", "sources", "mentions", "as_written",
                 "normalized", "admin_context", "auto_location_id", "auto_location_name",
                 "match_score", "guess_latitude", "guess_longitude", "guess_confidence",
                 "guess_basis", "examples", "nearest", "other_books", "confirmation_status", "confirmed_answer",
                 "confirmed_location_id", "confirmed_location_name", "confirmed_by", "votes",
                 "decisions")


def list_places(f, email):
    """One page of cards for the filters `f`, with the caller's own latest decision."""
    where, params = [], {}
    if f["group"]:
        where.append("place_group = %(group)s")
        params["group"] = f["group"]
    if f["source"]:
        where.append("%(source)s = ANY(sources)")
        params["source"] = f["source"]
    if f["document_id"]:
        where.append("document_id = %(doc)s")
        params["doc"] = f["document_id"]
    if f["state"] == "open":
        where.append("confirmation_status <> 'confirmed'")
    elif f["state"] in ("pending", "confirmed"):
        where.append("confirmation_status = %(state)s")
        params["state"] = f["state"]
    params.update(limit=PAGE_SIZE, offset=(f["page"] - 1) * PAGE_SIZE)
    sql = (f"SELECT {', '.join(_CARD_COLUMNS)}, count(*) OVER () AS total "
           f"FROM views.places__to_confirm {'WHERE ' + ' AND '.join(where) if where else ''} "
           "ORDER BY mentions DESC, place_key, document_id LIMIT %(limit)s OFFSET %(offset)s")
    with app_db.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        total = rows[0][-1] if rows else 0
        cards = [dict(zip(_CARD_COLUMNS, map(plain, row[:-1]))) for row in rows]
        keys = [c["place_key"] for c in cards]
        mine = {}
        if keys:
            cur.execute("""SELECT DISTINCT ON (place_key, document_id) place_key, document_id,
                                  choice, location_id, note
                           FROM app.place_confirmation
                           WHERE user_email = %s AND place_key = ANY(%s)
                           ORDER BY place_key, document_id, created_at DESC, id DESC""", (email, keys))
            mine = {(k, d): {"choice": c, "location_id": l, "note": n} for k, d, c, l, n in cur.fetchall()}
        ids = location_ids(cards)
        coords = {}
        if ids:
            cur.execute("SELECT location_id, latitude, longitude FROM core.location WHERE location_id = ANY(%s)",
                        (ids,))
            coords = {i: (lat, lon) for i, lat, lon in cur.fetchall()}
        add_coordinates(cards, coords)
    for card in cards:
        card["mine"] = mine.get((card["place_key"], card["document_id"]))
    return {"places": cards, "total": total, "page": f["page"], "page_size": PAGE_SIZE}


def books():
    with app_db.cursor() as cur:
        cur.execute("""SELECT d.id, d.title FROM core.document d
                       WHERE d.id IN (SELECT DISTINCT document_id FROM views.places__mentions)
                       ORDER BY d.title""")
        return [{"document_id": i, "title": (t or "").removeprefix("File:").replace("_", " ")}
                for i, t in cur.fetchall()]


def search_locations(q, limit=12):
    """core.location rows by name, best first. Cyrillic input is searched by
    its Latin spellings (translit.candidates), against the modern name and the
    alternate names, with trigram similarity."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    spellings = translit.candidates(q)[:8]
    with app_db.cursor() as cur:
        cur.execute("""
            SELECT l.location_id, l.modern_location_name, l.c1900_district, l.c1900_province,
                   l.latitude, l.longitude, max(s.score) AS score
            FROM core.location l
            CROSS JOIN unnest(%(spellings)s::text[]) AS c(q)
            CROSS JOIN LATERAL (SELECT greatest(
                     similarity(l.modern_location_name, c.q),
                     word_similarity(c.q, coalesce(l.alternate_names, '')),
                     CASE WHEN l.modern_location_name ILIKE c.q || '%%' THEN 1 ELSE 0 END,
                     CASE WHEN l.alternate_names ILIKE '%%' || c.q || '%%' THEN 0.9 ELSE 0 END) AS score) s
            WHERE s.score >= 0.6
            GROUP BY l.location_id, l.modern_location_name, l.c1900_district, l.c1900_province,
                     l.latitude, l.longitude
            ORDER BY max(s.score) DESC, l.modern_location_name
            LIMIT %(limit)s""", {"spellings": spellings, "limit": limit})
        return [{"location_id": i, "name": n, "district": dist, "province": prov,
                 "latitude": plain(lat), "longitude": plain(lon), "score": round(float(score), 2)}
                for i, n, dist, prov, lat, lon, score in cur.fetchall()]


def decide(key, document_id, choice, location_id, note, email, role):
    """One decision on one place in one book (its own statement and lock)."""
    with app_db.cursor() as cur:
        cur.execute("SELECT app.place_decide(%s, %s, %s, %s, %s, %s, %s)",
                    (key, document_id, choice, location_id, email, role, note))
        return cur.fetchone()[0]


# --- routes -----------------------------------------------------------------------

@bp.route("/places/confirm", methods=["GET"])
def confirm_page():
    who = current_user()
    if not who:
        return redirect("/")
    return render_template("place_confirm.html", me=who[0], role=who[1], books=books(),
                           page_size=PAGE_SIZE)


@bp.route("/api/places", methods=["GET"])
def api_places():
    who = current_user()
    if not who:
        return _not_found()
    return jsonify(success=True, **list_places(list_args(request.args), who[0]))


@bp.route("/api/places/books", methods=["GET"])
def api_books():
    if not current_user():
        return _not_found()
    return jsonify(success=True, books=books())


@bp.route("/api/places/locations", methods=["GET"])
def api_locations():
    if not current_user():
        return _not_found()
    return jsonify(success=True, locations=search_locations(request.args.get("q", "")))


@bp.route("/api/places/decision", methods=["POST"])
def api_decision():
    who = current_user()
    if not who:
        return _not_found()
    if not request.is_json:
        return jsonify(success=False, message="Send JSON (Content-Type: application/json)."), 415
    try:
        key, document_ids, choice, location_id, note = decision_args(request.get_json(silent=True))
    except BadRequest as err:
        return jsonify(success=False, message=str(err)), 400
    results = []
    for i, document_id in enumerate(document_ids):
        try:
            results.append(decide(key, document_id, choice, location_id, note, *who))
        except Exception as err:   # e.g. unknown location (22023) from app.place_decide
            if getattr(err, "pgcode", None) != "22023":
                raise
            message = str(err).split("\n")[0]
            if i == 0:
                return jsonify(success=False, message=message), 400
            results.append({"document_id": document_id, "status": "error", "message": message})
    _logger.info(f"place decision: {key} {choice} {location_id or ''} in {len(document_ids)} book(s)")
    return jsonify(success=True, result=plain(results[0]), others=plain(results[1:]))
