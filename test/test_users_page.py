from datetime import datetime, UTC
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import birddog.service as service
from birddog import user_admin
from birddog.service import app

ME = "admin@example.org"


@pytest.fixture
def client(monkeypatch):
    app.config["TESTING"] = True
    app.secret_key = "test_secret"
    monkeypatch.setattr(service, "runtime", SimpleNamespace(state="running", database_update_enabled=False))
    monkeypatch.setattr(service, "users", MagicMock())
    monkeypatch.setattr("birddog.pg.enabled", lambda: True)
    monkeypatch.setattr(service.user_sync, "configured_tools", lambda env=None: [SimpleNamespace(key="grafana")])
    return app.test_client()


def _sign_in(client, role="admin"):
    service.users.lookup.return_value = SimpleNamespace(name="Ada", email=ME, role=role)
    with client.session_transaction() as s:
        s["user"] = {"name": "Ada", "email": ME}


def test_users_page_is_admin_only(client):
    _sign_in(client, role="user")
    assert client.get("/users").status_code == 403
    assert client.post("/users", json={}).status_code == 403


def test_users_page_lists_accounts_with_sync_state(client, monkeypatch):
    _sign_in(client)
    monkeypatch.setattr(user_admin, "accounts", lambda: [
        {"email": ME, "name": "Ada", "role": "admin", "created_at": datetime(2026, 11, 1, tzinfo=UTC),
         "sync": {"grafana": {"state": "ok", "detail": "created"}}},
        {"email": "bo@example.org", "name": "Bo", "role": "user", "created_at": None,
         "sync": {"grafana": {"state": "error", "detail": "HTTP 500"}}}])
    body = client.get("/users").get_data(as_text=True)
    assert 'data-email="bo@example.org"' in body
    assert "grafana: HTTP 500" in body
    assert body.count("bd-users-remove-btn\"") == 1          # not for yourself


def test_create_returns_password_and_sync(client, monkeypatch):
    _sign_in(client)
    calls = []
    monkeypatch.setattr(user_admin, "create_account",
                        lambda e, n, a: calls.append((e, n, a)) or ("pw-xyz", {"grafana": ("ok", "created")}))
    resp = client.post("/users", json={"email": "bo@example.org", "name": "Bo", "admin": True})
    assert resp.status_code == 200
    assert resp.json == {"success": True, "password": "pw-xyz",
                         "sync": [{"tool": "grafana", "state": "ok", "detail": "created"}]}
    assert calls == [("bo@example.org", "Bo", True)]


def test_create_error_message_is_shown(client, monkeypatch):
    _sign_in(client)
    def refuse(*args):
        raise user_admin.AccountError("bo@example.org already has an account")
    monkeypatch.setattr(user_admin, "create_account", refuse)
    resp = client.post("/users", json={"email": "bo@example.org", "name": "Bo"})
    assert resp.status_code == 400 and resp.json["message"] == "bo@example.org already has an account"


def test_cannot_demote_or_remove_yourself(client, monkeypatch):
    _sign_in(client)
    monkeypatch.setattr(user_admin, "change_role", lambda *a: pytest.fail("must not be called"))
    monkeypatch.setattr(user_admin, "delete_account", lambda *a: pytest.fail("must not be called"))
    assert client.post(f"/users/{ME}/role", json={"role": "user"}).status_code == 400
    assert client.delete(f"/users/{ME.upper()}").status_code == 400


def test_create_account_validates_input(monkeypatch):
    monkeypatch.setattr(user_admin, "load_user_record", lambda email: None)
    with pytest.raises(user_admin.AccountError, match="valid email"):
        user_admin.create_account("not-an-email", "Bo")
    with pytest.raises(user_admin.AccountError, match="name"):
        user_admin.create_account("bo@example.org", "  ")
