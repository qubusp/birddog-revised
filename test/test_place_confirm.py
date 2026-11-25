"""The Confirm places blueprint (birddog/place_confirm.py) on a test app of its
own: login, JSON-only writes, input checks and page rendering. Database calls
are replaced; the rule itself (app.place_decide, migration 024) is SQL."""

from decimal import Decimal
from pathlib import Path

import pytest
from flask import Flask

from birddog import place_confirm as pc

REPO = Path(__file__).resolve().parent.parent
ME = "Ada@Example.org"
KEY = "канів|київська губернія"


@pytest.fixture
def app(monkeypatch):
    app = Flask("test_place_confirm", template_folder=str(REPO / "templates"),
                static_folder=str(REPO / "static"))
    app.secret_key = "test"
    app.config["TESTING"] = True
    app.register_blueprint(pc.bp)
    monkeypatch.setattr(pc, "books", lambda: [{"document_id": 2567, "title": "1848 год. Книга"}])
    return app


def _sign_in(client, monkeypatch, role="user"):
    monkeypatch.setattr(pc.user_module, "load_user_record",
                        lambda email: {"name": "Ada", "role": role, "password": "x"} if email == ME else None)
    with client.session_transaction() as s:
        s["user"] = {"name": "Ada", "email": ME}


def test_signed_out(app):
    client = app.test_client()
    assert client.get("/places/confirm").status_code == 302
    assert client.get("/api/places").status_code == 404
    assert client.post("/api/places/decision", json={}).json == {"error": "Not found"}


def test_unknown_account(app, monkeypatch):
    client = app.test_client()
    _sign_in(client, monkeypatch)
    with client.session_transaction() as s:
        s["user"] = {"email": "someone@else.org"}
    assert client.get("/api/places/books").status_code == 404


def test_page_renders(app, monkeypatch):
    client = app.test_client()
    _sign_in(client, monkeypatch, role="admin")
    body = client.get("/places/confirm").get_data(as_text=True)
    assert "Confirm places" in body
    assert '<label for="f-book"' in body and "1848 год. Книга" in body
    assert 'data-role="admin"' in body
    assert "js/place_confirm.js" in body


def test_list_passes_filters(app, monkeypatch):
    client = app.test_client()
    _sign_in(client, monkeypatch)
    seen = {}

    def fake(f, email):
        seen.update(f, email=email)
        return {"places": [], "total": 0, "page": f["page"], "page_size": pc.PAGE_SIZE}
    monkeypatch.setattr(pc, "list_places", fake)
    resp = client.get("/api/places?group=matched&source=bogus&book=2567&state=all&page=3")
    assert resp.json["success"] is True
    assert seen == {"group": "matched", "source": None, "document_id": 2567, "state": "all",
                    "page": 3, "email": ME.lower()}


def test_decision_is_json_only(app, monkeypatch):
    client = app.test_client()
    _sign_in(client, monkeypatch)
    monkeypatch.setattr(pc, "decide", lambda *a: pytest.fail("must not be called"))
    resp = client.post("/api/places/decision", data={"place_key": KEY, "document_id": 1, "choice": "unsure"})
    assert resp.status_code == 415


def test_decision_passes_role_from_account(app, monkeypatch):
    client = app.test_client()
    _sign_in(client, monkeypatch, role="admin")
    calls = []
    monkeypatch.setattr(pc, "decide", lambda *a: calls.append(a) or
                        {"status": "confirmed", "answer": "location", "location_id": "-1040899",
                         "confirmed_by": "admin", "linked": 3, "unlinked": 0, "votes": {"x": Decimal(1)}})
    resp = client.post("/api/places/decision",
                       json={"place_key": KEY, "document_id": 2567, "also_document_ids": [2562, "2567", 2563],
                             "choice": "location", "location_id": " -1040899 ",
                             "note": "  page 3 says Kaniv  "})
    assert resp.status_code == 200
    assert resp.json["result"]["linked"] == 3 and resp.json["result"]["votes"] == {"x": 1.0}
    assert len(resp.json["others"]) == 2
    # one decision per book: the card's book first, then the ticked ones (no repeat)
    assert calls == [(KEY, d, "location", "-1040899", "page 3 says Kaniv", ME.lower(), "admin")
                     for d in (2567, 2562, 2563)]


def test_decision_errors(app, monkeypatch):
    client = app.test_client()
    _sign_in(client, monkeypatch)

    class PgError(Exception):
        pgcode = "22023"
    monkeypatch.setattr(pc, "decide", lambda *a: (_ for _ in ()).throw(PgError("unknown location nope\nCONTEXT")))
    resp = client.post("/api/places/decision",
                       json={"place_key": KEY, "document_id": 1, "choice": "location", "location_id": "nope"})
    assert resp.status_code == 400 and resp.json["message"] == "unknown location nope"
    resp = client.post("/api/places/decision", json={"place_key": KEY, "document_id": 1, "choice": "maybe"})
    assert resp.status_code == 400


def test_other_book_refused_is_reported(app, monkeypatch):
    client = app.test_client()
    _sign_in(client, monkeypatch)

    class PgError(Exception):
        pgcode = "22023"

    def decide(key, doc, *rest):
        if doc == 9:
            raise PgError("book 9 does not name this place\nCONTEXT")
        return {"document_id": doc, "status": "pending"}
    monkeypatch.setattr(pc, "decide", decide)
    resp = client.post("/api/places/decision",
                       json={"place_key": KEY, "document_id": 1, "also_document_ids": [9, 2], "choice": "unsure"})
    assert resp.status_code == 200
    assert resp.json["others"] == [{"document_id": 9, "status": "error", "message": "book 9 does not name this place"},
                                   {"document_id": 2, "status": "pending"}]


@pytest.mark.parametrize("body, message", [
    (None, "JSON"),
    ({"choice": "unsure", "document_id": 1}, "place_key"),
    ({"place_key": "no-bar", "choice": "unsure", "document_id": 1}, "place_key"),
    ({"place_key": KEY, "choice": "unsure"}, "Which book"),
    ({"place_key": KEY, "choice": "unsure", "document_id": True}, "Which book"),
    ({"place_key": KEY, "choice": "unsure", "document_id": 1, "also_document_ids": "2"}, "list of books"),
    ({"place_key": KEY, "choice": "unsure", "document_id": 1, "also_document_ids": ["x"]}, "list of books"),
    ({"place_key": KEY, "choice": "yes", "document_id": 1}, "Choose"),
    ({"place_key": KEY, "choice": "location", "document_id": 1}, "Pick a location"),
    ({"place_key": KEY, "choice": "unsure", "document_id": 1, "note": 5}, "note"),
])
def test_decision_args_rejects(body, message):
    with pytest.raises(pc.BadRequest, match=message):
        pc.decision_args(body)


def test_decision_args_drops_location_unless_location():
    assert pc.decision_args({"place_key": KEY, "document_id": "7", "choice": "not_place", "location_id": "-1"}) == \
        (KEY, [7], "not_place", None, "")
    assert len(pc.decision_args({"place_key": KEY, "document_id": 7, "choice": "unsure",
                                 "note": "x" * 5000})[4]) == pc.NOTE_MAX


def test_list_args_defaults():
    assert pc.list_args({}) == {"group": None, "source": None, "state": "open", "document_id": None, "page": 1}
    assert pc.list_args({"page": "-4", "book": "abc", "state": "weird"})["page"] == 1


def test_plain():
    assert pc.plain({"a": [Decimal("1.5")], "b": None}) == {"a": [1.5], "b": None}
