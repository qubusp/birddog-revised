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
