# JupyterHub for Birddog: shared notebooks next to the data.
# See deploy/jupyterhub/README.md. Everything configurable comes from the
# environment set in deploy/docker-compose.yml (values live in deploy/.env).
import hmac
import os
import sys

from nativeauthenticator import NativeAuthenticator

c = get_config()  # noqa: F821  (injected by JupyterHub)

env = os.environ
ADMIN_USER = env.get("JUPYTERHUB_ADMIN_USER", "admin").strip().lower()
ADMIN_PASSWORD = env["JUPYTERHUB_ADMIN_PASSWORD"]

# --- hub ---------------------------------------------------------------------
c.JupyterHub.bind_url = "http://0.0.0.0:8000"
c.JupyterHub.hub_ip = "0.0.0.0"
c.JupyterHub.hub_connect_ip = "jupyterhub"  # compose service name
c.JupyterHub.db_url = "sqlite:////data/jupyterhub.sqlite"
c.JupyterHub.cookie_secret_file = "/data/jupyterhub_cookie_secret"


# --- auth: NativeAuthenticator ------------------------------------------------
# Users sign up at /hub/signup; the admin approves them at /hub/authorize.
# The admin account itself is not a signup: its password is
# JUPYTERHUB_ADMIN_PASSWORD from deploy/.env, so it always works and nobody can
# claim the name by signing up first.
class BirddogAuthenticator(NativeAuthenticator):
    async def authenticate(self, handler, data):
        name = self.normalize_username(data.get("username", ""))
        if name == ADMIN_USER:
            ok = hmac.compare_digest(
                data.get("password", "").encode(), ADMIN_PASSWORD.encode()
            )
            return {"name": ADMIN_USER, "admin": True} if ok else None
        return await super().authenticate(handler, data)


c.JupyterHub.authenticator_class = BirddogAuthenticator
c.Authenticator.admin_users = {ADMIN_USER}
# who may log in is decided by NativeAuthenticator's approval flag
c.Authenticator.allow_all = True
c.NativeAuthenticator.open_signup = False  # new accounts wait for approval
c.NativeAuthenticator.minimum_password_length = 10
c.NativeAuthenticator.check_common_password = True
c.NativeAuthenticator.allowed_failed_logins = 5
c.NativeAuthenticator.seconds_before_next_try = 300

# --- per-user servers: DockerSpawner -----------------------------------------
c.JupyterHub.spawner_class = "dockerspawner.DockerSpawner"
c.DockerSpawner.image = env.get("JUPYTER_SINGLEUSER_IMAGE", "birddog-jupyter-singleuser:local")
c.DockerSpawner.network_name = env.get("DOCKER_NETWORK_NAME", "deploy_default")
c.DockerSpawner.use_internal_ip = True
c.DockerSpawner.remove = True  # containers are disposable; work lives in the volume
c.DockerSpawner.name_template = "jupyter-{username}"
c.DockerSpawner.notebook_dir = "/home/jovyan/work"
c.Spawner.default_url = "/lab"

host_repo = env.get("BIRDDOG_HOST_REPO", "/home/administrator/birddog-revised")
c.DockerSpawner.volumes = {
    # per-user persistent work volume
    "jupyterhub-user-{username}": "/home/jovyan/work",
    # the birddog library (and the resources/ it loads), read-only
    f"{host_repo}/birddog": {"bind": "/home/jovyan/birddog-lib/birddog", "mode": "ro"},
    f"{host_repo}/resources": {"bind": "/home/jovyan/birddog-lib/resources", "mode": "ro"},
}

c.Spawner.mem_limit = "2G"
c.Spawner.cpu_limit = 1.0
c.Spawner.start_timeout = 120
c.Spawner.http_timeout = 60

reader_url = (
    f"postgresql://birddog_reader:{env['JUPYTER_DB_PASSWORD']}@postgres:5432/birddog"
)
c.Spawner.environment = {
    # read-only role: SELECT on core + raw, nothing in app
    "BIRDDOG_DATABASE_URL": reader_url,
    # SQLAlchemy form (psycopg 3 driver), e.g. for jupysql's %sql
    "DATABASE_URL": reader_url.replace("postgresql://", "postgresql+psycopg://", 1),
    "PYTHONPATH": "/home/jovyan/birddog-lib",
}

# --- idle culling ------------------------------------------------------------
c.JupyterHub.load_roles = [
    {
        "name": "jupyterhub-idle-culler-role",
        "scopes": [
            "list:users",
            "read:users:activity",
            "read:servers",
            "delete:servers",
        ],
        "services": ["jupyterhub-idle-culler-service"],
    }
]
c.JupyterHub.services = [
    {
        "name": "jupyterhub-idle-culler-service",
        "command": [sys.executable, "-m", "jupyterhub_idle_culler", "--timeout=3600"],
    }
]
