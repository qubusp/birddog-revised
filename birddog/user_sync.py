"""Copies Birddog accounts (app.user_account) into the other tools: Airflow,
Mathesar, Marmot, Saltcorn, ToolJet and Grafana.

Birddog is the one place accounts are made. Whenever birddog-web or the
user_admin CLI sees an account's plain password (creation, login, password
change or reset), it hands it to every configured tool's admin API, so a person
has one email and one password everywhere. Role changes and removals go out
without a password. JupyterHub needs none of this: its authenticator asks
birddog-web's /login directly (deploy/jupyterhub/jupyterhub_config.py).

Each tool's username is the Birddog email. Only accounts whose username is a
Birddog email are touched, so each tool's own break-glass admin (`admin`, ...)
stays as it is.

app.user_sync (migration 018) records, per account and tool, the last result
and a fingerprint of the Birddog password hash the tool's copy came from. A
login re-syncs only the tools that are behind or failed, so the usual login
costs one query.

Gotchas:
- Runs only on the apps box: it reaches Mathesar, Marmot and Saltcorn's
  database by their compose names, and Airflow over WireGuard. A tool counts as
  configured when its BIRDDOG_SYNC_* settings are present; the rest are skipped.
- Mathesar marks a password set by an admin as "must change", which would make
  people pick a different one there. After setting it, the sync signs in as the
  person and submits Mathesar's change-password form with the same password,
  which clears the flag. Mathesar's password rules apply to that form.
- Marmot refuses passwords shorter than 8 characters; that tool then shows an
  error until the person picks a longer one.
"""

import hashlib
import logging
import os
import threading
from dataclasses import dataclass

import requests
from requests.utils import quote

_logger = logging.getLogger(__name__)
_TIMEOUT = 20


@dataclass(frozen=True)
class Account:
    email: str
    name: str
    role: str            # 'admin' or 'user'

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def first_last(self):
        parts = (self.name or self.email.split("@")[0]).split(None, 1)
        return parts[0], (parts[1] if len(parts) > 1 else "-")


class SyncError(Exception):
    pass


def _bcrypt(password):
    import bcrypt
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt(10)).decode()


def _check(resp, what):
    if resp.status_code >= 400:
        raise SyncError(f"{what}: HTTP {resp.status_code}: {resp.text[:200]}")
    return resp


# --- tools ----------------------------------------------------------------------

class Airflow:
    """Airflow's FAB auth manager: /auth/fab/v1/users, as an Admin service user."""
    key = "airflow"
    ROLES = {"admin": "Admin", "user": "User"}

    def __init__(self, env):
        self.base = (env.get("BIRDDOG_SYNC_AIRFLOW_URL") or env.get("AIRFLOW_API_URL") or "").rstrip("/")
        self.user = env.get("BIRDDOG_SYNC_AIRFLOW_USER")
        self.password = env.get("BIRDDOG_SYNC_AIRFLOW_PASSWORD")

    @property
    def configured(self):
        return bool(self.base and self.user and self.password)

    def _session(self):
        s = requests.Session()
        resp = _check(s.post(f"{self.base}/auth/token", timeout=_TIMEOUT,
                             json={"username": self.user, "password": self.password}),
                      "Airflow login")
        s.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        return s

    def upsert(self, account, password):
        s = self._session()
        url = f"{self.base}/auth/fab/v1/users/{quote(account.email, safe='')}"
        first, last = account.first_last
        body = {"username": account.email, "email": account.email,
                "first_name": first, "last_name": last,
                "roles": [{"name": self.ROLES[account.role]}]}
        if password:
            body["password"] = password
        resp = s.get(url, timeout=_TIMEOUT)
        if resp.status_code == 404:
            if not password:
                return "no account yet (made at next login)"
            _check(s.post(f"{self.base}/auth/fab/v1/users", json=body, timeout=_TIMEOUT), "Airflow create")
            return "created"
        _check(resp, "Airflow lookup")
        mask = ["first_name", "last_name", "email", "roles"] + (["password"] if password else [])
        _check(s.patch(url, params={"update_mask": ",".join(mask)}, json=body, timeout=_TIMEOUT),
               "Airflow update")
        return "updated"

    def remove(self, email):
        resp = self._session().delete(f"{self.base}/auth/fab/v1/users/{quote(email, safe='')}", timeout=_TIMEOUT)
        if resp.status_code != 404:
            _check(resp, "Airflow delete")


class Mathesar:
    """Mathesar's JSON-RPC API (/api/rpc/v0/) with a Django session."""
    key = "mathesar"
    DB_NAME = "birddog"
    DB_ROLE = "birddog_ui"   # the only role curators get (deploy/ui/README.md)

    def __init__(self, env):
        self.base = (env.get("BIRDDOG_SYNC_MATHESAR_URL") or "").rstrip("/")
        self.admin = env.get("MATHESAR_ADMIN_USER") or "admin"
        self.admin_password = env.get("MATHESAR_ADMIN_PASSWORD")

    @property
    def configured(self):
        return bool(self.base and self.admin_password)

    def _session(self, username, password):
        s = requests.Session()
        login = f"{self.base}/auth/login/"
        _check(s.get(login, timeout=_TIMEOUT), "Mathesar login page")
        resp = s.post(login, timeout=_TIMEOUT, allow_redirects=False,
                      data={"username": username, "password": password,
                            "csrfmiddlewaretoken": s.cookies.get("csrftoken", "")},
                      headers={"Referer": login})
        if "sessionid" not in s.cookies:
            raise SyncError(f"Mathesar login as {username} failed (HTTP {resp.status_code})")
        return s

    def _rpc(self, s, method, **params):
        resp = _check(s.post(f"{self.base}/api/rpc/v0/", timeout=_TIMEOUT,
                             json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                             headers={"X-CSRFToken": s.cookies.get("csrftoken", ""),
                                      "Referer": f"{self.base}/"}),
                      f"Mathesar {method}")
        body = resp.json()
        if body.get("error"):
            raise SyncError(f"Mathesar {method}: {body['error'].get('message', body['error'])}")
        return body.get("result")

    def _find(self, s, email):
        return next((u for u in self._rpc(s, "users.list") if u["username"] == email), None)

    def _grant_database(self, s, user_id):
        db = next((d for d in self._rpc(s, "databases.configured.list") if d["name"] == self.DB_NAME), None)
        if db is None:
            raise SyncError(f"Mathesar has no '{self.DB_NAME}' database connected")
        role = next((r for r in self._rpc(s, "roles.configured.list", database_id=db["id"],
                                          server_id=db["server_id"]) if r["name"] == self.DB_ROLE), None)
        if role is None:
            raise SyncError(f"Mathesar has no configured role '{self.DB_ROLE}'")
        if not any(c["user_id"] == user_id for c in self._rpc(s, "collaborators.list", database_id=db["id"])):
            self._rpc(s, "collaborators.add", database_id=db["id"], user_id=user_id,
                      configured_role_id=role["id"])

    def upsert(self, account, password):
        if account.email == self.admin:
            raise SyncError("Mathesar: this email is the break-glass admin's username")
        s = self._session(self.admin, self.admin_password)
        user = self._find(s, account.email)
        if user is None and not password:
            return "no account yet (made at next login)"
        if user is None:
            user = self._rpc(s, "users.add", user_def={
                "username": account.email, "password": password, "is_superuser": account.is_admin,
                "email": account.email, "full_name": account.name or ""})
            result = "created"
        else:
            self._rpc(s, "users.patch_other", user_id=user["id"], username=account.email,
                      is_superuser=account.is_admin, email=account.email,
                      full_name=account.name or "", display_language=user.get("display_language") or "en")
            if password:
                self._rpc(s, "users.password.revoke", user_id=user["id"], new_password=password)
            result = "updated"
        self._grant_database(s, user["id"])
        if password:
            self._clear_change_needed(account.email, password)
        return result

    def _clear_change_needed(self, username, password):
        """An admin-set password is "must change": Mathesar then sends every
        request to its change-password form. Submit that form as the person
        with the same password, which clears the flag."""
        s = self._session(username, password)
        url = f"{self.base}/auth/password_reset_confirm/"
        _check(s.get(url, timeout=_TIMEOUT), "Mathesar password form")
        resp = s.post(url, timeout=_TIMEOUT, allow_redirects=False,
                      data={"new_password1": password, "new_password2": password,
                            "csrfmiddlewaretoken": s.cookies.get("csrftoken", "")},
                      headers={"Referer": url})
        if resp.status_code != 302:
            # the form came back: Mathesar's password rules refused it
            raise SyncError("Mathesar refused this password (too short or too common?); "
                            "the account exists but asks for a new password")

    def remove(self, email):
        s = self._session(self.admin, self.admin_password)
        user = self._find(s, email)
        if user:
            self._rpc(s, "users.delete", user_id=user["id"])


class Marmot:
    """Marmot's REST API with an API key that has users:manage."""
    key = "marmot"
    ROLES = {"admin": ["admin"], "user": ["user"]}

    def __init__(self, env):
        self.base = (env.get("BIRDDOG_SYNC_MARMOT_URL") or "").rstrip("/")
        self.api_key = env.get("BIRDDOG_SYNC_MARMOT_API_KEY")

    @property
    def configured(self):
        return bool(self.base and self.api_key)

    def _session(self):
        s = requests.Session()
        s.headers["X-API-Key"] = self.api_key
        return s

    def _find(self, s, email):
        resp = _check(s.get(f"{self.base}/api/v1/users", params={"query": email, "limit": 100},
                            timeout=_TIMEOUT), "Marmot lookup")
        return next((u for u in resp.json().get("users") or [] if u["username"] == email), None)

    def upsert(self, account, password):
        if password is not None and len(password) < 8:
            raise SyncError("Marmot needs a password of at least 8 characters")
        s = self._session()
        user = self._find(s, account.email)
        if user is None:
            if not password:
                return "no account yet (made at next login)"
            _check(s.post(f"{self.base}/api/v1/users", timeout=_TIMEOUT, json={
                "username": account.email, "name": account.name or account.email,
                "password": password, "role_names": self.ROLES[account.role]}), "Marmot create")
            return "created"
        body = {"name": account.name or account.email, "active": True,
                "role_names": self.ROLES[account.role]}
        if password:
            body["password"] = password
        _check(s.put(f"{self.base}/api/v1/users/{user['id']}", json=body, timeout=_TIMEOUT), "Marmot update")
        return "updated"

    def remove(self, email):
        s = self._session()
        user = self._find(s, email)
        if user:
            _check(s.put(f"{self.base}/api/v1/users/{user['id']}", json={"active": False},
                         timeout=_TIMEOUT), "Marmot deactivate")


class Saltcorn:
    """Saltcorn's own users table, written directly (no user API in Saltcorn)."""
    key = "saltcorn"
    ROLES = {"admin": 1, "user": 80}   # _sc_roles: 1 admin, 40 staff, 80 user

    def __init__(self, env):
        self.db_url = env.get("BIRDDOG_SYNC_SALTCORN_DB_URL")

    @property
    def configured(self):
        return bool(self.db_url)

    @staticmethod
    def hash_password(password):
        # Saltcorn checks with bcryptjs; $2a$ and $2b$ are the same algorithm
        return "$2a$" + _bcrypt(password)[4:]

    def _connect(self):
        import psycopg2
        conn = psycopg2.connect(self.db_url, connect_timeout=10)
        conn.autocommit = True
        return conn

    def upsert(self, account, password):
        role_id = self.ROLES[account.role]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE lower(email) = %s", (account.email,))
            row = cur.fetchone()
            if row is None:
                if not password:
                    return "no account yet (made at next login)"
                cur.execute("INSERT INTO users (email, password, role_id, disabled, verified_on) "
                            "VALUES (%s, %s, %s, false, now())",
                            (account.email, self.hash_password(password), role_id))
                return "created"
            if password:
                cur.execute("UPDATE users SET password = %s, role_id = %s, disabled = false WHERE id = %s",
                            (self.hash_password(password), role_id, row[0]))
            else:
                cur.execute("UPDATE users SET role_id = %s, disabled = false WHERE id = %s", (role_id, row[0]))
            return "updated"

    def remove(self, email):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE users SET disabled = true WHERE lower(email) = %s", (email,))


class ToolJet:
    """ToolJet CE: its web API as a workspace admin, plus its users table.

    New people are invited and their account set up at once with the
    account-setup and invitation tokens the admin's user list carries (no
    email involved). The CE API cannot set someone else's password, so a
    password change writes users.password_digest (bcrypt) directly.
    """
    key = "tooljet"
    ROLES = {"admin": "admin", "user": "end-user"}

    def __init__(self, env):
        self.base = (env.get("BIRDDOG_SYNC_TOOLJET_URL") or "").rstrip("/")
        self.admin = (env.get("BIRDDOG_SYNC_TOOLJET_EMAIL") or "").lower()
        self.admin_password = env.get("BIRDDOG_SYNC_TOOLJET_PASSWORD")
        self.db_url = env.get("BIRDDOG_SYNC_TOOLJET_DB_URL")

    @property
    def configured(self):
        return bool(self.base and self.admin and self.admin_password and self.db_url)

    def _session(self):
        s = requests.Session()
        resp = _check(s.post(f"{self.base}/api/authenticate", timeout=_TIMEOUT,
                             json={"email": self.admin, "password": self.admin_password}),
                      "ToolJet login")
        s.headers["tj-workspace-id"] = resp.json()["current_organization_id"]
        return s

    def _find(self, s, email):
        resp = _check(s.get(f"{self.base}/api/organization-users", timeout=_TIMEOUT,
                            params={"searchText": email, "page": 1}), "ToolJet lookup")
        return next((u for u in resp.json().get("users") or [] if (u.get("email") or "").lower() == email), None)

    def _activate(self, s, account, password):
        """Finish an invite: set up the account, or accept for an existing one.

        These calls act as the invitee and replace the session's login with
        theirs, so they go through a fresh session: s stays the admin's.
        """
        user = self._find(s, account.email)
        first, last = account.first_last
        invitee = requests.Session()
        if user and user.get("account_setup_token"):
            _check(invitee.post(f"{self.base}/api/onboarding/setup-account-from-token", timeout=_TIMEOUT, json={
                "first_name": first, "last_name": last, "password": password,
                "token": user["account_setup_token"], "organizationToken": user.get("invitation_token")}),
                "ToolJet account setup")
        elif user and user.get("invitation_token"):
            _check(invitee.post(f"{self.base}/api/onboarding/accept-invite", timeout=_TIMEOUT,
                                json={"token": user["invitation_token"]}), "ToolJet accept invite")

    def _set_password(self, email, password):
        import psycopg2
        with psycopg2.connect(self.db_url, connect_timeout=10) as conn, conn.cursor() as cur:
            cur.execute("UPDATE users SET password_digest = %s, password_retry_count = 0 WHERE lower(email) = %s",
                        (_bcrypt(password), email))
            if cur.rowcount != 1:
                raise SyncError("ToolJet: no users row to set the password on")

    def upsert(self, account, password):
        if account.email == self.admin:
            raise SyncError("ToolJet: this email is the sync's own admin account")
        if password is not None and len(password) < 5:
            raise SyncError("ToolJet needs a password of at least 5 characters")
        s = self._session()
        first, last = account.first_last
        user = self._find(s, account.email)
        if user is None:
            if not password:
                return "no account yet (made at next login)"
            _check(s.post(f"{self.base}/api/organization-users", timeout=_TIMEOUT, json={
                "firstName": first, "lastName": last, "email": account.email,
                "groups": [], "role": self.ROLES[account.role]}), "ToolJet invite")
            self._activate(s, account, password)
            self._set_password(account.email, password)   # an existing ToolJet user keeps theirs otherwise
            return "created"
        if user["status"] == "archived":
            _check(s.post(f"{self.base}/api/organization-users/{user['id']}/unarchive", timeout=_TIMEOUT,
                          json={}), "ToolJet unarchive")
        if user["status"] != "active" and password:
            self._activate(s, account, password)
        if password:
            self._set_password(account.email, password)
        # the list's "role" is a legacy field ("all-users"); the role is the default group
        current_role = next((g["name"] for g in user.get("role_group") or []), None)
        if current_role != self.ROLES[account.role] or user.get("first_name") != first:
            # addGroups is required (the handler iterates it) even when unchanged
            _check(s.put(f"{self.base}/api/organization-users/{user['id']}", timeout=_TIMEOUT, json={
                "firstName": first, "lastName": last, "role": self.ROLES[account.role], "addGroups": []}),
                "ToolJet update")
        return "updated"

    def remove(self, email):
        s = self._session()
        user = self._find(s, email)
        if user and user["status"] != "archived":
            _check(s.post(f"{self.base}/api/organization-users/{user['id']}/archive", timeout=_TIMEOUT,
                          json={}), "ToolJet archive")


class Grafana:
    """Grafana's admin HTTP API, as the built-in admin (basic auth).

    Birddog admins are org Admins and server admins; everyone else is a
    Viewer. Dashboards come from the repo (deploy/grafana/dashboards); admins
    can edit and save them in Grafana until the file changes.
    """
    key = "grafana"
    ROLES = {"admin": "Admin", "user": "Viewer"}

    def __init__(self, env):
        self.base = (env.get("BIRDDOG_SYNC_GRAFANA_URL") or "").rstrip("/")
        self.admin = env.get("GRAFANA_ADMIN_USER") or "admin"
        self.admin_password = env.get("GRAFANA_ADMIN_PASSWORD")

    @property
    def configured(self):
        return bool(self.base and self.admin_password)

    def _session(self):
        s = requests.Session()
        s.auth = (self.admin, self.admin_password)
        return s

    def _find(self, s, email):
        resp = s.get(f"{self.base}/api/users/lookup", params={"loginOrEmail": email}, timeout=_TIMEOUT)
        if resp.status_code == 404:
            return None
        return _check(resp, "Grafana lookup").json()

    def upsert(self, account, password):
        if account.email == self.admin:
            raise SyncError("Grafana: this email is the break-glass admin's login")
        s = self._session()
        user = self._find(s, account.email)
        name = account.name or account.email
        if user is None:
            if not password:
                return "no account yet (made at next login)"
            resp = _check(s.post(f"{self.base}/api/admin/users", timeout=_TIMEOUT, json={
                "name": name, "email": account.email, "login": account.email, "password": password}),
                "Grafana create")
            user_id, result = resp.json()["id"], "created"
        else:
            user_id, result = user["id"], "updated"
            if user.get("isDisabled"):
                _check(s.post(f"{self.base}/api/admin/users/{user_id}/enable", timeout=_TIMEOUT), "Grafana enable")
            if user.get("name") != name:
                _check(s.put(f"{self.base}/api/users/{user_id}", timeout=_TIMEOUT, json={
                    "name": name, "email": account.email, "login": account.email}), "Grafana update")
            if password:
                _check(s.put(f"{self.base}/api/admin/users/{user_id}/password", timeout=_TIMEOUT,
                             json={"password": password}), "Grafana password")
        _check(s.patch(f"{self.base}/api/org/users/{user_id}", timeout=_TIMEOUT,
                       json={"role": self.ROLES[account.role]}), "Grafana role")
        # Birddog admins are also Grafana server admins (Administration menu)
        _check(s.put(f"{self.base}/api/admin/users/{user_id}/permissions", timeout=_TIMEOUT,
                     json={"isGrafanaAdmin": account.is_admin}), "Grafana server admin")
        return result

    def remove(self, email):
        s = self._session()
        user = self._find(s, email)
        if user:
            _check(s.delete(f"{self.base}/api/admin/users/{user['id']}", timeout=_TIMEOUT), "Grafana delete")


TOOL_CLASSES = (Airflow, Mathesar, Marmot, Saltcorn, ToolJet, Grafana)


def configured_tools(env=None):
    env = os.environ if env is None else env
    return [tool for tool in (cls(env) for cls in TOOL_CLASSES) if tool.configured]


# --- bookkeeping (app.user_sync) --------------------------------------------------

def password_version(password_hash):
    return hashlib.sha256((password_hash or "").encode()).hexdigest()[:16]


def _load(email):
    from birddog import app_db
    with app_db.cursor() as cur:
        cur.execute("SELECT name, role, password_hash FROM app.user_account WHERE email = %s", (email,))
        row = cur.fetchone()
        if row is None:
            return None, None, {}
        cur.execute("SELECT tool, state, password_version FROM app.user_sync WHERE email = %s", (email,))
        status = {tool: (state, version) for tool, state, version in cur.fetchall()}
    return Account(email, row[0], row[1] or "user"), password_version(row[2]), status


def _record(email, tool, state, detail, version):
    from birddog import app_db
    with app_db.cursor() as cur:
        cur.execute(
            "INSERT INTO app.user_sync (email, tool, state, detail, password_version, synced_at) "
            "VALUES (%s, %s, %s, %s, %s, now()) ON CONFLICT (email, tool) DO UPDATE SET "
            "state = EXCLUDED.state, detail = EXCLUDED.detail, "
            "password_version = COALESCE(EXCLUDED.password_version, app.user_sync.password_version), "
            "synced_at = now()",
            (email, tool, state, detail[:500] if detail else None, version))


def status(email):
    """{tool: {'state', 'detail', 'synced_at'}} for Genizah; {} without Postgres."""
    from birddog import pg
    if not pg.enabled():
        return {}
    from birddog import app_db
    with app_db.cursor() as cur:
        cur.execute("SELECT tool, state, detail, synced_at FROM app.user_sync WHERE email = %s", (email,))
        return {t: {"state": s, "detail": d, "synced_at": at} for t, s, d, at in cur.fetchall()}


# --- entry points -----------------------------------------------------------------

def sync_account(email, password=None, only_behind=False, tools=None):
    """Bring one account up to date in every configured tool.

    password: the plain password, when the caller has it; without it, accounts
    are updated (name, role, active) but not created. only_behind: skip tools
    that already hold this password version (the login path).
    Returns {tool: (state, detail)}. Never raises: a failure is recorded.
    """
    from birddog import pg
    tools = configured_tools() if tools is None else tools
    if not tools or not pg.enabled():
        return {}
    email = email.strip().lower()
    account, version, known = _load(email)
    if account is None:
        return {}
    results = {}
    for tool in tools:
        if only_behind and known.get(tool.key) == ("ok", version):
            continue
        try:
            detail = tool.upsert(account, password)
            results[tool.key] = ("ok", detail)
            _record(email, tool.key, "ok", detail, version if password else None)
        except Exception as err:   # one tool's failure must not stop the others
            _logger.warning("user sync to %s failed: %s", tool.key, err)
            results[tool.key] = ("error", str(err))
            _record(email, tool.key, "error", str(err), None)
    return results


def sync_in_background(email, password=None, only_behind=False):
    """sync_account in a daemon thread, so a login never waits on other tools."""
    if not configured_tools():
        return None
    thread = threading.Thread(target=sync_account, args=(email, password, only_behind),
                              name="user-sync", daemon=True)
    thread.start()
    return thread


def remove_account(email, tools=None):
    """Take an account out of every configured tool (delete or deactivate)."""
    tools = configured_tools() if tools is None else tools
    results = {}
    for tool in tools:
        try:
            tool.remove(email.strip().lower())
            results[tool.key] = ("ok", "removed")
        except Exception as err:
            results[tool.key] = ("error", str(err))
    return results
