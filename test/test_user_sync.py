from unittest.mock import MagicMock, patch

import bcrypt
import pytest

from birddog import user_sync
from birddog.user_sync import Account, Airflow, Saltcorn, SyncError

ADA = Account("ada@example.org", "Ada Lovelace", "user")


class FakeTool:
    def __init__(self, key, fail=False):
        self.key = key
        self.fail = fail
        self.calls = []

    def upsert(self, account, password):
        self.calls.append((account.email, password))
        if self.fail:
            raise SyncError(f"{self.key} is down")
        return "updated"


@pytest.fixture
def store(monkeypatch):
    """app.user_sync in memory: {(email, tool): (state, version)}."""
    rows = {}
    monkeypatch.setattr(user_sync, "_load", lambda email: (
        ADA, "v2", {tool: value for (e, tool), value in rows.items() if e == email}))
    monkeypatch.setattr(user_sync, "_record", lambda email, tool, state, detail, version:
                        rows.__setitem__((email, tool), (state, version or rows.get((email, tool), (None, None))[1])))
    monkeypatch.setattr("birddog.pg.enabled", lambda: True)
    return rows


def test_login_resyncs_only_tools_that_are_behind(store):
    store[(ADA.email, "airflow")] = ("ok", "v2")      # current
    store[(ADA.email, "mathesar")] = ("ok", "v1")     # old password
    store[(ADA.email, "marmot")] = ("error", "v2")    # failed last time
    tools = [FakeTool("airflow"), FakeTool("mathesar"), FakeTool("marmot"), FakeTool("saltcorn")]
    results = user_sync.sync_account(ADA.email, "pw-123456", only_behind=True, tools=tools)
    assert set(results) == {"mathesar", "marmot", "saltcorn"}
    assert tools[0].calls == []
    assert store[(ADA.email, "mathesar")] == ("ok", "v2")


def test_one_failing_tool_does_not_stop_the_others(store):
    tools = [FakeTool("airflow", fail=True), FakeTool("saltcorn")]
    results = user_sync.sync_account(ADA.email, "pw-123456", tools=tools)
    assert results["airflow"][0] == "error"
    assert results["saltcorn"] == ("ok", "updated")
    assert store[(ADA.email, "airflow")][0] == "error"


def test_without_password_the_version_is_kept(store):
    store[(ADA.email, "saltcorn")] = ("ok", "v1")
    user_sync.sync_account(ADA.email, None, tools=[FakeTool("saltcorn")])
    assert store[(ADA.email, "saltcorn")] == ("ok", "v1")


def test_nothing_configured_does_nothing(monkeypatch):
    monkeypatch.setattr(user_sync, "configured_tools", lambda env=None: [])
    assert user_sync.sync_account(ADA.email, "x") == {}
    assert user_sync.sync_in_background(ADA.email, "x") is None


def test_configured_tools_follow_settings():
    env = {"BIRDDOG_SYNC_SALTCORN_DB_URL": "postgresql://x", "BIRDDOG_SYNC_MARMOT_URL": "http://m"}
    assert [t.key for t in user_sync.configured_tools(env)] == ["saltcorn"]


def test_saltcorn_hash_is_bcryptjs_compatible():
    digest = Saltcorn.hash_password("correct horse")
    assert digest.startswith("$2a$10$")
    assert bcrypt.checkpw(b"correct horse", ("$2b$" + digest[4:]).encode())


def test_first_last_name():
    assert ADA.first_last == ("Ada", "Lovelace")
    assert Account("bo@x.org", "", "user").first_last == ("bo", "-")


def _response(status, body=None):
    resp = MagicMock(status_code=status, text="")
    resp.json.return_value = body or {}
    return resp


def test_airflow_creates_missing_user_with_email_as_username():
    session = MagicMock()
    session.post.side_effect = [_response(201, {"access_token": "t"}), _response(200)]
    session.get.return_value = _response(404)
    tool = Airflow({"AIRFLOW_API_URL": "http://af:8080", "BIRDDOG_SYNC_AIRFLOW_USER": "sync",
                    "BIRDDOG_SYNC_AIRFLOW_PASSWORD": "s"})
    with patch("birddog.user_sync.requests.Session", return_value=session):
        assert tool.upsert(ADA, "pw-123456") == "created"
    assert session.get.call_args[0][0] == "http://af:8080/auth/fab/v1/users/ada%40example.org"
    body = session.post.call_args_list[1].kwargs["json"]
    assert body["username"] == ADA.email and body["roles"] == [{"name": "User"}]
    assert body["password"] == "pw-123456"


def test_airflow_role_change_without_password_patches_roles_only():
    session = MagicMock()
    session.post.return_value = _response(201, {"access_token": "t"})
    session.get.return_value = _response(200)
    session.patch.return_value = _response(200)
    tool = Airflow({"AIRFLOW_API_URL": "http://af:8080", "BIRDDOG_SYNC_AIRFLOW_USER": "sync",
                    "BIRDDOG_SYNC_AIRFLOW_PASSWORD": "s"})
    with patch("birddog.user_sync.requests.Session", return_value=session):
        tool.upsert(Account(ADA.email, ADA.name, "admin"), None)
    kwargs = session.patch.call_args.kwargs
    assert "password" not in kwargs["params"]["update_mask"]
    assert kwargs["json"]["roles"] == [{"name": "Admin"}]


def test_airflow_does_not_create_without_password():
    session = MagicMock()
    session.post.return_value = _response(201, {"access_token": "t"})
    session.get.return_value = _response(404)
    tool = Airflow({"AIRFLOW_API_URL": "http://af", "BIRDDOG_SYNC_AIRFLOW_USER": "s",
                    "BIRDDOG_SYNC_AIRFLOW_PASSWORD": "s"})
    with patch("birddog.user_sync.requests.Session", return_value=session):
        assert "next login" in tool.upsert(ADA, None)
    assert session.post.call_count == 1   # the token only
