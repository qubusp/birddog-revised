"""Coordinates for the Confirm places card maps (place_confirm.location_ids,
add_coordinates): the view leaves them out, list_places adds them."""

from decimal import Decimal

from birddog import place_confirm as pc


def _card(**kw):
    base = {"auto_location_id": None, "confirmed_location_id": None, "nearest": None}
    return {**base, **kw}


def test_location_ids_collects_match_confirmed_and_nearest():
    cards = [_card(auto_location_id="a", nearest=[{"location_id": "n1"}, {"location_id": "n2"}]),
             _card(confirmed_location_id="c", nearest=[{"location_id": "a"}, {"name": "no id"}]),
             _card()]
    assert pc.location_ids(cards) == ["a", "c", "n1", "n2"]


def test_add_coordinates_sets_positions_and_leaves_unknown_empty():
    cards = [_card(auto_location_id="a", confirmed_location_id="gone",
                   nearest=[{"location_id": "n1", "km": 3.2}])]
    pc.add_coordinates(cards, {"a": (Decimal("49.5"), Decimal("31.25")), "n1": (50.0, 30.0)})
    card = cards[0]
    assert (card["auto_latitude"], card["auto_longitude"]) == (49.5, 31.25)
    assert isinstance(card["auto_latitude"], float)
    assert (card["confirmed_latitude"], card["confirmed_longitude"]) == (None, None)
    assert card["nearest"][0] == {"location_id": "n1", "km": 3.2, "latitude": 50.0, "longitude": 30.0}


def test_add_coordinates_without_ids():
    cards = [_card()]
    pc.add_coordinates(cards, {})
    assert cards[0]["auto_latitude"] is None and cards[0]["confirmed_longitude"] is None


# --- the Map page (birddog/place_map.py) ------------------------------------------

from pathlib import Path

import pytest
from flask import Flask

from birddog import place_map as pm

REPO = Path(__file__).resolve().parent.parent
ME = "ada@example.org"


@pytest.fixture
def app():
    app = Flask("test_place_map", template_folder=str(REPO / "templates"), static_folder=str(REPO / "static"))
    app.secret_key = "test"
    app.config["TESTING"] = True
    app.register_blueprint(pm.bp)
    return app


def _sign_in(client, monkeypatch):
    monkeypatch.setattr(pc.user_module, "load_user_record",
                        lambda email: {"name": "Ada", "role": "user"} if email == ME else None)
    with client.session_transaction() as s:
        s["user"] = {"name": "Ada", "email": ME}


def test_map_signed_out(app):
    client = app.test_client()
    assert client.get("/map").status_code == 302
    assert client.get("/api/map").status_code == 404


def test_map_page_and_data(app, monkeypatch):
    client = app.test_client()
    _sign_in(client, monkeypatch)
    page = client.get("/map")
    assert page.status_code == 200
    assert b"leaflet@1.9.4" in page.data and b"place_map.js" in page.data
    monkeypatch.setattr(pm, "map_data", lambda: {"locations": [], "book_places": [], "rough": []})
    assert client.get("/api/map").json == {"success": True, "locations": [], "book_places": [], "rough": []}


def test_group_book_places_one_entry_per_location():
    rows = [("a", "Kaniv", Decimal("49.75"), Decimal("31.46"), 1, "File:Book_one.pdf", 2, "u1"),
            ("b", "Smila", 49.2, 31.9, 1, "File:Book_one.pdf", 1, "u1"),
            ("a", "Kaniv", Decimal("49.75"), Decimal("31.46"), 2, "File:Book_two.pdf", 5, "u2")]
    places = pm.group_book_places(rows)
    assert [p["name"] for p in places] == ["Kaniv", "Smila"]
    kaniv = places[0]
    assert kaniv["mentions"] == 7 and kaniv["latitude"] == 49.75
    assert [b["title"] for b in kaniv["books"]] == ["Book two.pdf", "Book one.pdf"]
