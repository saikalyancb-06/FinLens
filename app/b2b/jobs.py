"""A cap on how many statement analyses run at once in this process.

PDF extraction is CPU- and memory-heavy (tens to hundreds of MB per large
statement). The service runs as one instance with one worker (render.yaml), so
without a cap, a burst of large uploads would all extract at once and could
exhaust the instance's memory, taking every in-flight request down with it.
Excess work waits for a slot; if none frees up within the wait limit the
caller gets a clean 503 SERVICE_UNAVAILABLE with Retry-After instead.
"""
from __future__ import annotations

import contextlib
import os
import threading

from app.b2b import errors
from app.b2b.errors import ApiError

MAX_CONCURRENT = max(1, int(os.getenv("B2B_MAX_CONCURRENT_JOBS", "2")))
WAIT_SECONDS = float(os.getenv("B2B_JOB_WAIT_SECONDS", "120"))

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


@contextlib.contextmanager
def heavy_slot():
    if not _slots.acquire(timeout=WAIT_SECONDS):
        raise ApiError(errors.SERVICE_UNAVAILABLE,
                       "The service is busy processing other statements. Retry shortly.",
                       headers={"Retry-After": "30"})
    try:
        yield
    finally:
        _slots.release()
