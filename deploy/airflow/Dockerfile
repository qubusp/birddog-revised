FROM apache/airflow:3.3.2-python3.12

# pg_dump/pg_restore matching the Postgres 16 server (Debian's own client is
# older) and rclone, for the backup DAGs.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg unzip \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 \
 && curl -fsSL https://rclone.org/install.sh | bash \
 && rm -rf /var/lib/apt/lists/*

# birddog's local KV store writes under ./.cache (until it moves to Postgres);
# pre-create it so the named volume mounted here inherits airflow's ownership.
RUN mkdir -p /opt/birddog/.cache && chown -R airflow:0 /opt/birddog/.cache
USER airflow

# Birddog's domain library dependencies (wiki parsing, fetching, enrichment).
# The birddog package itself is mounted at /opt/birddog by docker-compose.
COPY deploy/airflow/requirements.txt /tmp/birddog-requirements.txt
RUN pip install --no-cache-dir -r /tmp/birddog-requirements.txt
