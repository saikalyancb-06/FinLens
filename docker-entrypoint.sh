#!/bin/sh
# Fix ownership of the runtime directories, then drop privileges.
#
# Why this exists: on Render (and Docker generally) a persistent disk mounted
# at /app/uploads arrives owned by root. The image runs as `appuser`, so
# without this the very first statement upload would fail with EACCES at
# runtime — long after the deploy reported success, and only for the one code
# path that writes files.
#
# The chown is the only thing that happens as root. `exec gosu` replaces this
# shell with the server process, so PID 1 is uvicorn and it receives SIGTERM
# from the platform directly on shutdown.
set -e

for dir in /app/uploads /app/logs; do
    if [ -d "$dir" ]; then
        # `|| true`: a read-only or already-correct mount must not stop boot.
        chown -R appuser:appuser "$dir" 2>/dev/null || true
    fi
done

# Directories the application expects to exist (main.py creates these too, but
# it does so after import, and doing it here means a permission problem shows
# up in the entrypoint rather than half way through startup).
mkdir -p /app/uploads/email /app/uploads/rpa_screens 2>/dev/null || true
chown -R appuser:appuser /app/uploads 2>/dev/null || true

# If we are already unprivileged (some platforms pin the UID), just run.
if [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

exec gosu appuser "$@"
