# gunicorn settings for the birddog-web container.
#
# One worker process: the app starts its Runtime (page cache, wiki change
# poller, significance sweep) once per process, so more processes would only
# duplicate that work. Requests are served by threads instead; DAG-backed jobs
# return immediately, so nothing holds a thread for long.
import os

bind = "0.0.0.0:8000"
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("BIRDDOG_WEB_THREADS", "8"))
# exports and comparisons with old revisions fetch from the wiki in-request
timeout = 120
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
# behind Caddy: trust its X-Forwarded-* headers
forwarded_allow_ips = "*"
