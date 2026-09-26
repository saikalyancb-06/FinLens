# Container image for the FastAPI treasury backend.
#
# Built for a managed platform (Render) in front of managed Postgres and a
# managed Redis-compatible key-value store. The platform terminates TLS, sets
# X-Forwarded-For, and tells the container which port to listen on via $PORT.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Poppler and Tesseract are the OCR path for scanned statements
# (app/parsers/ocr_engine.py); libpq is psycopg2's runtime. gosu lets the
# entrypoint fix ownership of the mounted disk as root and then drop to an
# unprivileged user for the server itself.
#
# The cleanup path was `/var/lib/apt-get/lists/*`, which does not exist — the
# directory is `/var/lib/apt/lists`. Nothing was ever deleted, so every build
# carried the package index in the layer.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-eng \
    curl \
    gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first: this layer is cached and only rebuilds when
# requirements.txt changes, so an application edit does not reinstall pandas,
# scikit-learn and PyMuPDF.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# See .dockerignore for what this deliberately leaves behind — in particular
# .env, dist/KredoAgent.exe and the database snapshots.
COPY . .

# The service runs as a normal user, not root. Financial statements and OCR
# both mean parsing untrusted files in this process.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p uploads/email uploads/rpa_screens logs \
    && chown -R appuser:appuser /app

# A platform disk mounted at /app/uploads arrives owned by root and would be
# unwritable by appuser, which would fail every upload at runtime rather than
# at deploy time. The entrypoint runs as root purely to correct that, then
# hands the process to appuser via gosu — so nothing but the chown is
# privileged, and it works whether or not a disk is actually mounted.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/health" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# $PORT because the platform assigns it; the old CMD hardcoded 8000 and would
# have been unreachable wherever that assignment differs. --proxy-headers makes
# uvicorn read X-Forwarded-Proto/For from the platform's load balancer so
# request.url.scheme is https and the client IP is the real one; the
# application still gates its own use of the header on TRUST_PROXY_HEADERS
# (app/config.py, main.py::_client_ip).
#
# One worker, deliberately. File parsing is dispatched with FastAPI
# BackgroundTasks INSIDE this process (app/api/files.py), and the rate limiter
# is a module-level dict in main.py — a second worker would give each its own
# limiter bucket and its own view of in-flight parses.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --workers 1"]
