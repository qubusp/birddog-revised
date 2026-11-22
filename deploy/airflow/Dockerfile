FROM apache/airflow:3.3.2-python3.12

# Birddog's domain library dependencies (wiki parsing, fetching, enrichment).
# The birddog package itself is mounted at /opt/birddog by docker-compose.
COPY deploy/airflow/requirements.txt /tmp/birddog-requirements.txt
RUN pip install --no-cache-dir -r /tmp/birddog-requirements.txt

# birddog's local KV store writes under ./.cache (until it moves to Postgres);
# pre-create it so the named volume mounted here inherits airflow's ownership.
USER root
RUN mkdir -p /opt/birddog/.cache && chown -R airflow:0 /opt/birddog/.cache
USER airflow
