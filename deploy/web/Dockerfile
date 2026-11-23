# Birddog web app (Flask under gunicorn) for the Postgres + Airflow stack.
# Build context is the repo root (see docker-compose.yml, service birddog-web).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/birddog

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# the app loads resources/*.json and templates/static relative to /opt/birddog
COPY birddog ./birddog
COPY templates ./templates
COPY static ./static
COPY resources ./resources
COPY application.py ./
COPY deploy/web/gunicorn.conf.py ./

# same uid as the Airflow containers; .cache is the local object cache
# (a named volume in compose, which inherits this ownership)
RUN useradd --uid 50000 --gid 0 --no-create-home --home-dir /opt/birddog birddog \
 && mkdir -p .cache && chown -R 50000:0 .cache
USER 50000:0

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4).status == 200 else 1)"
CMD ["gunicorn", "--config", "gunicorn.conf.py", "application:application"]
