from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import birddog.service as service
from birddog import genizah
from birddog.service import app

GENIZAH = "genizah.example.org"


@pytest.fixture
def client(monkeypatch):
    app.config["TESTING"] = True
    app.secret_key = "test_secret"
    monkeypatch.setattr(service, "runtime", SimpleNamespace(state="running", database_update_enabled=False))
    monkeypatch.setattr(service, "users", MagicMock())
    monkeypatch.delenv("GENIZAH_DOMAIN", raising=False)
    monkeypatch.delenv("GENIZAH_AIRFLOW_DOMAIN", raising=False)
    return app.test_client()


def _sign_in(client, role):
    service.users.lookup.return_value = SimpleNamespace(name="Ada", email="ada@example.org", role=role)
    with client.session_transaction(base_url=f"https://{GENIZAH}") as s:
        s["user"] = {"name": "Ada", "email": "ada@example.org"}


def test_signed_out_gets_sign_in_form_and_no_tools(client):
    body = client.get("/", base_url=f"https://{GENIZAH}").get_data(as_text=True)
    assert 'id="gz-signin"' in body
    assert "gz-grid" not in body
    assert "https://birddog.example.org/" in body  # password-reset pointer


def test_user_sees_tools_but_not_admin_only(client):
    _sign_in(client, "user")
    body = client.get("/", base_url=f"https://{GENIZAH}").get_data(as_text=True)
    assert "https://data.example.org/" in body
    assert "https://catalog.example.org/" in body
    assert "Airflow" not in body
    assert 'id="gz-signin"' not in body


def test_admin_sees_airflow_on_the_data_box_domain(client, monkeypatch):
    monkeypatch.setenv("GENIZAH_AIRFLOW_DOMAIN", "data-box.example.net")
    _sign_in(client, "admin")
    body = client.get("/", base_url=f"https://{GENIZAH}").get_data(as_text=True)
    assert "https://airflow.data-box.example.net/" in body


def test_birddog_host_still_gets_birddog(client):
    with patch("birddog.service.render_template", return_value="BIRDDOG") as render:
        client.get("/", base_url="https://birddog.example.org")
    assert render.call_args[0][0] == "index.html"


def test_configured_domain_beats_request_host(monkeypatch):
    monkeypatch.setenv("GENIZAH_DOMAIN", "apps.example.com")
    monkeypatch.delenv("GENIZAH_AIRFLOW_DOMAIN", raising=False)
    urls = {t["key"]: t["url"] for t in genizah.tools_for("admin", "localhost:8000")}
    assert urls["mathesar"] == "https://data.apps.example.com/"
    assert urls["airflow"] == "https://airflow.apps.example.com/"


def test_is_genizah_host():
    assert genizah.is_genizah_host("Genizah.example.org:443")
    assert not genizah.is_genizah_host("birddog.example.org")
    assert not genizah.is_genizah_host(None)


def test_cards_show_account_state(client, monkeypatch):
    monkeypatch.setattr("birddog.user_sync.status", lambda email: {
        "mathesar": {"state": "ok", "detail": "created", "synced_at": None},
        "marmot": {"state": "error", "detail": "Marmot needs a password of at least 8 characters",
                   "synced_at": None}})
    _sign_in(client, "user")
    body = client.get("/", base_url=f"https://{GENIZAH}").get_data(as_text=True)
    assert "Same password as Birddog" in body                 # mathesar
    assert "Account sync failed" in body                      # marmot
    assert "at least 8 characters" in body                    # its detail, as a tooltip
    assert "Account not set up yet" in body                   # saltcorn: no row
