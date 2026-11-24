"""Genizah: the entry page listing every tool of the stack.

Served by birddog-web on the host genizah.<PUBLIC_DOMAIN> (Caddy sends that
host to the same app; service.py renders the page at "/" for it), behind the
Birddog login, so the people who may see it are Birddog's users.

Gotcha: the stack spans two domains. Airflow is on the data box
(GENIZAH_AIRFLOW_DOMAIN); everything else is on the apps box
(GENIZAH_DOMAIN). With neither set, links are built from the request's own
host, which is right for a single-box install.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    key: str
    name: str
    subdomain: str
    summary: str
    use_for: str
    icon: str              # Bootstrap Icons name
    data_box: bool = False  # served on the data box's domain
    admin_only: bool = False
    # how a Birddog user signs in there: "birddog" (checked against Birddog
    # itself), "synced" (account copied by user_sync.py) or "separate"
    login: str = "synced"


TOOLS = (
    Tool("birddog", "Birddog", "birddog",
         "Tracks archival documents on uk.wikisource.org.",
         "Browse archives, keep watchlists, get alerts on changes.",
         "binoculars", login="birddog"),
    Tool("mathesar", "Mathesar", "data",
         "The Birddog database as spreadsheets.",
         "Look up, filter and correct records in core tables.",
         "table"),
    Tool("jupyterhub", "JupyterHub", "hub",
         "Shared Python notebooks next to the database.",
         "Ad-hoc analysis, exports and one-off fixes.",
         "journal-code", login="birddog"),
    Tool("tooljet", "ToolJet", "apps",
         "Internal apps built on the database.",
         "Use or build curator screens and forms.",
         "grid-1x2", login="separate"),
    Tool("saltcorn", "Saltcorn", "saltcorn",
         "No-code apps and forms.",
         "Data entry forms and simple public views.",
         "ui-checks-grid"),
    Tool("marmot", "Marmot", "catalog",
         "Data catalog and lineage.",
         "Find which table holds what, and where it comes from.",
         "diagram-3"),
    Tool("airflow", "Airflow", "airflow",
         "The pipelines that keep the database current.",
         "Check pipeline runs, start or pause one.",
         "arrow-repeat", data_box=True, admin_only=True),
)


def _domain_from_host(host):
    # genizah.example.org -> example.org; keeps any :port out of the result
    host = (host or "").split(":", 1)[0]
    return host.split(".", 1)[1] if "." in host else host


def _url(tool, host):
    apps_domain = os.environ.get("GENIZAH_DOMAIN") or _domain_from_host(host)
    domain = (os.environ.get("GENIZAH_AIRFLOW_DOMAIN") or apps_domain) if tool.data_box else apps_domain
    return f"https://{tool.subdomain}.{domain}/"


def _account(tool, sync_status):
    """What the card says about the person's account in this tool."""
    if tool.login == "birddog":
        return {"state": "ok", "label": "Birddog sign-in"}
    if tool.login == "separate":
        return {"state": "none", "label": "Own sign-in"}
    row = (sync_status or {}).get(tool.key)
    if row is None:
        return {"state": "pending", "label": "Account not set up yet"}
    if row["state"] == "ok":
        return {"state": "ok", "label": "Same password as Birddog"}
    return {"state": "error", "label": "Account sync failed", "detail": row.get("detail")}


def tools_for(role, host, sync_status=None):
    """The tools to show a user of this role, each with its absolute URL and
    account state (sync_status: user_sync.status() for the person)."""
    return [{**tool.__dict__, "url": _url(tool, host), "account": _account(tool, sync_status)}
            for tool in TOOLS if role == "admin" or not tool.admin_only]


def tool_url(key, host):
    return next(_url(tool, host) for tool in TOOLS if tool.key == key)


def is_genizah_host(host):
    return (host or "").lower().startswith("genizah.")
