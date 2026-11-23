# Elyra: drawing Airflow DAGs locally

Elyra 4.2.0 is a visual pipeline editor that runs inside JupyterLab. It runs only
on your laptop. You draw a pipeline from Airflow operators, export it to a DAG
`.py` file in `dags/elyra/`, and the normal deploy (`sh/deploy`) ships that file
to the server's Airflow (3.3.2, LocalExecutor). The server has no Elyra, no
Kubernetes and no S3.

## Files

| Path | What it is |
|---|---|
| `setup.sh` | One-time setup. Creates `.venv-elyra/` (gitignored), installs `requirements.lock`, and registers the runtime config and component catalog. Safe to re-run. |
| `start.sh` | Starts JupyterLab with Elyra on `http://127.0.0.1:8888`. |
| `export.py` | Exports a `.pipeline` file to `dags/elyra/<name>.py`. |
| `jupyter_server_config.py` | Tells Elyra which import line to use for each operator, using the Airflow 3 module paths. |
| `components/` | Operator source files copied unchanged from `apache-airflow-providers-standard` 1.19.0 (the version on the server). These files only tell the editor which fields to show. |
| `pipelines/` | Pipeline sources (`.pipeline` JSON). `elyra_hello.pipeline` is the proof pipeline. |
| `requirements.lock` | `elyra[airflow]==4.2.0` and every dependency it installs, pinned. All were released before 2026-11-09. |

## Setup and start

Requires `uv` and Python 3.12 (`brew install uv python@3.12`).

```sh
deploy/elyra/setup.sh      # once
deploy/elyra/start.sh      # opens http://127.0.0.1:8888/lab?token=...
```

The server listens only on localhost. To open it, use the tokenised URL that Jupyter prints and opens.
To pick your own token, set `JUPYTER_TOKEN=... deploy/elyra/start.sh`. To use a different port, set `ELYRA_PORT=8899`.

## Drawing a pipeline

1. In the Launcher, choose **Apache Airflow Pipeline Editor**. Save the file under `deploy/elyra/pipelines/`.
2. In the palette, open the **airflow standard** category. Drag **BashOperator** or
   **TriggerDagRunOperator** nodes onto the canvas and connect them. In each node's properties panel, fill in
   `bash_command` and the other fields.
3. In the pipeline properties, set the runtime config to **Birddog Airflow (export only)**.
4. **Do not use the Notebook, Python Script or R Script nodes.** See Limitations.

## Exporting to a DAG

```sh
.venv-elyra/bin/python deploy/elyra/export.py deploy/elyra/pipelines/<name>.pipeline
# -> dags/elyra/<name>.py   (dag_id = <name>)
```

Use this wrapper, not the editor's own Export button or `elyra-pipeline export`. Plain Elyra
produces a DAG that does not fit this stack:

- The dag_id gets a timestamp (`<name>-1123010551`), so every export creates a new DAG.
- The DAG has `schedule='@once'` and `is_paused_upon_creation=False`, so it runs as soon as it is deployed.
- It writes `from airflow import DAG` and operator imports from Airflow 1/2 (`airflow.operators.bash_operator`).

The wrapper fixes all of these: stable dag_id, `schedule=None`, starts paused, and imports from `airflow.sdk` and
`airflow.providers.standard`. Commit the `.pipeline` file and the generated `.py` file together. Don't hand-edit
the `.py` file, because the next export overwrites it.

To check a DAG on the server before a full deploy, copy just that file:

```sh
scp dags/elyra/<name>.py administrator@<host>:birddog-revised/dags/elyra/
ssh administrator@<host> 'cd ~/birddog-revised/deploy &&
  docker compose exec -T airflow-scheduler airflow dags list-import-errors &&
  docker compose exec -T airflow-scheduler airflow dags test <name>'
```

`elyra_hello` passed this check against Airflow 3.3.2: no import errors, and the `dags test` run
succeeded (`say_hello` printed "hello from Elyra", then `python_step` ran).

## Limitations

- **Only Airflow operator nodes ("custom components") work.** Elyra turns Notebook, Python Script and R Script
  nodes into `KubernetesPodOperator` tasks. Those tasks need Kubernetes, and they read and write their inputs
  and outputs through S3-compatible storage (see `elyra/templates/airflow/airflow_template.jinja2` and the
  [Airflow runtime prerequisites](https://elyra.readthedocs.io/en/latest/recipes/configure-airflow-as-a-runtime.html)).
  This stack has neither, so such DAGs would fail at run time.
- **The editor's Run/Submit button won't work.** Submitting pushes the DAG to a GitHub, GitLab or Gitea repository
  and expects Airflow to pull it with git-sync. The runtime config's git and S3 fields are placeholders that
  Elyra's schema requires. Always export instead.
- **No schedule field.** Exported DAGs are manual-trigger only (`schedule=None`). A DAG that needs a schedule
  should be written in Python.
- **Operators are limited to the copied files.** `PythonOperator` is left out on purpose: Elyra can only pass
  strings, and `PythonOperator` needs a Python callable. In Elyra 4.2.0 the "Airflow provider package catalog"
  finds 0 operators in Airflow 3 provider wheels, because it only recognises `from airflow.models import
  BaseOperator`. That is why `components/` holds copied files. To add an operator, copy its module from the
  provider wheel into `components/`, add its full import path to `jupyter_server_config.py`, then re-run `setup.sh`.
- Values in node properties are rendered as Python literals. Nodes don't pass data to each other, except through
  the `inputpath` XCom widget.
- The `start_date` is relative (`pendulum.today().add(days=-1)`). That is harmless with `schedule=None`.
