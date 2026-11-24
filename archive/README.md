# Archive

Files from the upstream Birddog repo (jbrandt130/birddog) that the rebuilt
stack (Postgres, Airflow, birddog-web) neither imports nor runs. Kept for
reference, with their git history (`git log --follow archive/<path>`).
Nothing here is deployed (`sh/lib/deploy-common.sh` excludes `/archive`),
and nothing here is tested.

| Path | Was | Why archived |
|---|---|---|
| `birddog/` | `__main__.py` (legacy CLI), `database_importer.py`, `doc_compare/`, `download_convert_to_pdf.py`, `google_drive_procs.py` | Not imported by the DAGs or the web app |
| `test/` | Tests for the modules above, `test_add_field.py`, two unused images | Test archived code |
| `notebooks/`, `research/` | Upstream exploration notebooks, triage and location research | Never deployed; JupyterHub ships its own examples from `deploy/jupyterhub/` |
| `utility/` | One-off NocoDB field scripts and specs | NocoDB-era migrations, done |
| `mockups/` | Early HTML mockups | Superseded by the web app |
| `nocodb-dev/` | Local NocoDB compose file | Legacy-mode dev only |
| `.ebextensions/`, `ebignore`, `sh/aws_run` | Elastic Beanstalk deploy | Replaced by `sh/deploy-data` / `sh/deploy-apps` |
| `sh/lab` | `jupyter lab` launcher | Use JupyterHub |
| `docs/*.pptx` | Project presentations (2025-2026) | Reference only |

To restore something: `git mv archive/<path> <path>`.
