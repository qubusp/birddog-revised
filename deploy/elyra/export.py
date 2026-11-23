"""Export an Elyra .pipeline to an Airflow DAG with the repo's Elyra config.

`elyra-pipeline export` on its own ignores Jupyter config files, so this
wrapper loads deploy/elyra/jupyter_server_config.py first and then hands
over to the normal CLI. Usage (from anywhere):

    .venv-elyra/bin/python deploy/elyra/export.py pipelines/hello.pipeline

Writes dags/elyra/<pipeline file stem>.py (overwriting), then fixes the
parts of Elyra's DAG template that are hard-coded for "submit and run once":
  - dag_id "<name>-<timestamp>"   -> "<name>" (stable across re-exports)
  - is_paused_upon_creation=False -> True (stack convention: new DAGs start paused)
  - schedule="@once"              -> None (manual trigger; Elyra has no schedule field)
  - from airflow import DAG       -> from airflow.sdk import DAG (Airflow 3 import;
    Elyra only emits it when Airflow itself is importable, which it isn't here)
  - drops the absolute path of this laptop from the "Operator source" comments
"""

import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
os.environ.setdefault("JUPYTER_DATA_DIR", str(REPO / ".venv-elyra" / "jupyter-data"))

from traitlets.config import Configurable  # noqa: E402
from traitlets.config.loader import PyFileConfigLoader  # noqa: E402

from elyra.cli.pipeline_app import pipeline  # noqa: E402
from elyra.pipeline.registry import PipelineProcessorRegistry  # noqa: E402

config = PyFileConfigLoader("jupyter_server_config.py", path=str(HERE)).load_config()
PipelineProcessorRegistry.instance(root_dir=str(REPO), parent=Configurable(config=config))

if len(sys.argv) != 2:
    sys.exit(__doc__)
src = Path(sys.argv[1]).resolve()
out = REPO / "dags" / "elyra" / f"{src.stem}.py"
out.parent.mkdir(parents=True, exist_ok=True)
os.chdir(REPO)
sys.argv = [
    "elyra-pipeline", "export", str(src),
    "--runtime-config", "birddog_airflow",
    "--output", str(out),
    "--overwrite",
]
try:
    pipeline()
except SystemExit as e:
    if e.code not in (0, None):
        raise

dag = out.read_text()
dag, n = re.subn(r'"([A-Za-z0-9_.-]+)-\d{10}"', r'"\1"', dag)
assert n == 2, f"expected dag_id + project_id with timestamp suffix, got {n}"
for old, new in [
    ("is_paused_upon_creation=False", "is_paused_upon_creation=True"),
    ('schedule="@once"', "schedule=None"),
    ("from airflow import DAG\n", "from airflow.sdk import DAG\n"),
]:
    assert old in dag, f"template changed? {old!r} not found in {out}"
    dag = dag.replace(old, new)
dag = dag.replace(f'"base_dir": "{HERE}/components", ', "")
out.write_text(dag)
print(f"post-processed {out.relative_to(REPO)}")
