"""Genizah: the portal for Jewish genealogy and documentation work, listing
every tool of the stack.

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
         "Follows Ukrainian archival records on uk.wikisource.org.",
         "Browse archives, watch the files you research, get alerts when pages change.",
         "binoculars", login="birddog"),
    Tool("mathesar", "Mathesar", "data",
         "The catalog of documents, pages and places, as spreadsheets.",
         "Look up records and correct them: document types, places, transcription status.",
         "table"),
    Tool("jupyterhub", "JupyterHub", "hub",
         "Research notebooks on the whole catalog.",
         "Analyse records, review the places found in books, make exports.",
         "journal-code", login="birddog"),
    Tool("tooljet", "ToolJet", "apps",
         "Curation apps built on the catalog.",
         "Use or build screens for documentation work.",
         "grid-1x2"),
    Tool("saltcorn", "Saltcorn", "saltcorn",
         "Forms and simple public views.",
         "Collect and share records without code.",
         "ui-checks-grid"),
    Tool("grafana", "Grafana", "grafana",
         "Maps and dashboards of the archives and the books.",
         "See every community on the map, where the books lead, and how the collection grows.",
         "map"),
    Tool("marmot", "Marmot", "catalog",
         "What the data holds and where it comes from.",
         "Find which table has a given kind of record, and its source.",
         "diagram-3"),
    Tool("airflow", "Airflow", "airflow",
         "The pipelines that keep the catalog in step with the archives.",
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
