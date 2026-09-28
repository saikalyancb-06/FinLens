# Development image for GitHub Codespaces / VS Code Dev Containers.
#
# Deliberately NOT the production Dockerfile at the repository root. That image
# copies the source in, runs as an unprivileged user with a read-only checkout
# and starts uvicorn as PID 1 — none of which suits an editor that bind-mounts
# the working tree and expects to run pytest, alembic and ad-hoc scripts inside
# the container. The two are kept separate so a change made for the convenience
# of the dev container can never weaken the deployed one.
FROM mcr.microsoft.com/devcontainers/python:3.11-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Same runtime dependencies the production image installs, for the same
# reasons: poppler-utils + tesseract are the OCR path for scanned statements
# (app/parsers/ocr_engine.py), libpq-dev is psycopg2's build/runtime
# requirement. postgresql-client is extra here — post-create.sh uses pg_isready
# to wait for the database, and `psql` is the obvious debugging tool to have in
# a development shell.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        postgresql-client \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        curl \
    && rm -rf /var/lib/apt/lists/*

# A virtualenv rather than the image's system site-packages: it is owned by the
# `vscode` user, so `pip install <something>` during a debugging session works
# without sudo and without polluting the base interpreter.
ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV" && chown -R vscode:vscode "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Dependencies are installed at BUILD time, not in postCreateCommand: pandas,
# scikit-learn, PyMuPDF and lxml take minutes to resolve, and doing it here
# means Codespaces prebuilds cache them instead of every new codespace paying
# for them again. Only requirements.txt is copied, so editing application code
# does not invalidate this layer.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt

# Playwright's browser binaries (~400 MB) are NOT downloaded here. Only the
# agent/RPA path in agent/ needs them; run `playwright install chromium` in the
# container if you are working on it.

WORKDIR /workspace
