"""The Map page (/map, data at /api/map): communities, the places the books name,
and Claude's rough positions for places not matched yet, on one Leaflet map
(static/js/place_map.js, static/js/birddog_map.js). Registered on the web app by
birddog/service.py; never import service here, it imports this module.

The same views the Grafana maps read (views.map__locations and
views.map__book_places, migration 021; birddog_web's grant is migration 025),
plus views.books__unmatched_places (024). Grafana stays the place for analysis.
This map is for finding your way: every marker links back into Birddog, the book
scan, or Confirm places.

Signed-in users only, like Confirm places: an unknown visitor gets the sign-in
page or a 404. Read-only; queries run as birddog_web through app_db."""

from flask import Blueprint, jsonify, redirect, render_template

from birddog import app_db
from birddog.place_confirm import current_user, plain

bp = Blueprint("place_map", __name__, url_prefix="")


def group_book_places(rows):
    """One entry per location from views.map__book_places rows (location_id,
    location, latitude, longitude, document_id, book, mentions, url): the books
    that name it, most mentions first, and the total."""
    places = {}
    for location_id, location, lat, lon, document_id, book, mentions, url in rows:
        place = places.setdefault(location_id, {"location_id": location_id, "name": location,
                                                "latitude": plain(lat), "longitude": plain(lon),
                                                "mentions": 0, "books": []})
        place["mentions"] += int(mentions or 0)
        place["books"].append({"document_id": document_id,
                               "title": (book or "").removeprefix("File:").replace("_", " "),
                               "mentions": int(mentions or 0), "url": url})
    for place in places.values():
        place["books"].sort(key=lambda b: (-b["mentions"], b["title"]))
    return sorted(places.values(), key=lambda p: -p["mentions"])


def map_data():
    with app_db.cursor() as cur:
        cur.execute("""SELECT location_id, name, alternate_names, c1900_district, c1900_province,
                              jewish_population, latitude, longitude, documents, books, book_mentions
                       FROM views.map__locations ORDER BY name""")
        locations = [{"location_id": i, "name": n, "alternate_names": alt, "district": dist,
                      "province": prov, "population": pop, "latitude": plain(lat), "longitude": plain(lon),
                      "documents": docs, "books": books, "mentions": int(mentions or 0)}
                     for i, n, alt, dist, prov, pop, lat, lon, docs, books, mentions in cur.fetchall()]
        cur.execute("""SELECT location_id, location, latitude, longitude, document_id, book, mentions, url
                       FROM views.map__book_places""")
        book_places = group_book_places(cur.fetchall())
        # not matched yet, with a position, and not already settled on the Confirm page
        cur.execute("""SELECT document_id, title, coalesce(nullif(normalized, ''), as_written), as_written,
                              admin_context, latitude, longitude, confidence, basis,
                              nearest_1_name, nearest_1_km, page_link
                       FROM views.books__unmatched_places
                       WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                         AND confirmation_status IS DISTINCT FROM 'confirmed'
                       ORDER BY confidence, title""")
        rough = [{"document_id": d, "title": (t or "").removeprefix("File:").replace("_", " "), "name": n,
                  "as_written": w, "admin_context": ctx, "latitude": plain(lat), "longitude": plain(lon),
                  "confidence": conf, "basis": basis, "nearest": near, "nearest_km": plain(km), "page_link": link}
                 for d, t, n, w, ctx, lat, lon, conf, basis, near, km, link in cur.fetchall()]
    return {"locations": locations, "book_places": book_places, "rough": rough}


@bp.route("/map", methods=["GET"])
def map_page():
    who = current_user()
    if not who:
        return redirect("/")
    return render_template("place_map.html", role=who[1])


@bp.route("/api/map", methods=["GET"])
def api_map():
    if not current_user():
        return jsonify({"error": "Not found"}), 404
    return jsonify(success=True, **map_data())
