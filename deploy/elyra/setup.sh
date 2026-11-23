#!/usr/bin/env bash
# One-time local setup of JupyterLab + Elyra (visual pipeline editor) on a
# laptop. Creates the venv at <repo>/.venv-elyra (gitignored), installs the
# pinned packages, and registers:
#   - runtime config "birddog_airflow"  (export-only; the git/S3 fields are
#     placeholders Elyra's schema insists on -- they are never contacted when
#     a pipeline uses only Airflow operators)
#   - component catalog "airflow_standard" = deploy/elyra/components/*.py, operator
#     sources copied verbatim from apache-airflow-providers-standard 1.19.0 (the
#     version on the server). Elyra 4.2.0's "provider package" catalog finds 0
#     operators in Airflow 3 provider wheels (it only recognises
#     `from airflow.models import BaseOperator`), hence the local copies.
# Safe to re-run.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="$REPO/.venv-elyra"
export JUPYTER_DATA_DIR="$VENV/jupyter-data"   # keep Elyra metadata out of ~/Library/Jupyter

if [ ! -x "$VENV/bin/python" ]; then
  uv venv --python 3.12 "$VENV"
fi
uv pip install --python "$VENV/bin/python" -r "$REPO/deploy/elyra/requirements.lock"

EM="$VENV/bin/elyra-metadata"

"$EM" remove runtimes --name=birddog_airflow >/dev/null 2>&1 || true
"$EM" create runtimes \
  --schema_name=airflow \
  --name=birddog_airflow \
  --display_name="Birddog Airflow (export only)" \
  --api_endpoint="https://airflow.204-12-218-112.sslip.io" \
  --git_type=GITHUB \
  --github_api_endpoint="https://api.github.com" \
  --github_repo="unused/unused" \
  --github_branch="unused" \
  --github_repo_token="unused" \
  --cos_endpoint="http://unused.invalid" \
  --cos_auth_type=USER_CREDENTIALS \
  --cos_username=unused \
  --cos_password=unused-placeholder \
  --cos_bucket=unused

"$EM" remove component-catalogs --name=airflow_standard >/dev/null 2>&1 || true
"$EM" create component-catalogs \
  --schema_name=local-directory-catalog \
  --name=airflow_standard \
  --display_name="Airflow 3 standard operators" \
  --runtime_type=APACHE_AIRFLOW \
  --categories='["airflow standard"]' \
  --paths="['$REPO/deploy/elyra/components']"

echo "Elyra ready. Start it with: deploy/elyra/start.sh"
