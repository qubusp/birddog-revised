# Per-user notebook server image (spawned by JupyterHub's DockerSpawner).
# The scipy-notebook tag ships JupyterHub 6.1.1, matching the hub.
FROM quay.io/jupyter/scipy-notebook:2026-11-02

# Data access: DuckDB (+ its postgres extension, pre-downloaded so notebooks
# don't need to fetch it), psycopg, SQLAlchemy, Polars, Arrow, %sql magic.
# The server's CPU (Ivy Bridge) has AVX but no AVX2/FMA: duckdb 2.0.0 and plain
# polars die with SIGILL there, so duckdb stays on 1.5.x and polars uses its
# compat runtime. Re-test before bumping either.
RUN pip install --no-cache-dir \
        duckdb==1.5.6 \
        "psycopg[binary]==3.3.8" \
        sqlalchemy==2.0.57 \
        "polars[rtcompat]==2.1.0" \
        pyarrow==26.0.1 \
        jupysql==0.11.1 \
 && python -c "import duckdb; duckdb.sql('INSTALL postgres'); duckdb.sql('LOAD postgres')" \
 && python -c "import polars, pyarrow; polars.DataFrame({'a': [1]}).to_arrow()"

# The birddog library's own dependencies, so `import birddog` works in
# notebooks (the package itself is bind-mounted read-only at spawn time).
# dag-factory is Airflow-only and would drag Airflow in, so it is skipped.
COPY --chown=${NB_UID}:${NB_GID} deploy/airflow/requirements.txt /tmp/birddog-requirements.txt
RUN grep -v '^dag-factory' /tmp/birddog-requirements.txt > /tmp/req.txt \
 && pip install --no-cache-dir -r /tmp/req.txt \
 && rm /tmp/req.txt /tmp/birddog-requirements.txt

# Example notebooks, copied into each user's work volume on start (never
# overwriting a user's edited copy).
COPY deploy/jupyterhub/examples/ /opt/birddog-examples/
COPY deploy/jupyterhub/copy-examples.sh /usr/local/bin/before-notebook.d/50-birddog-examples.sh

ENV PYTHONPATH=/home/jovyan/birddog-lib
