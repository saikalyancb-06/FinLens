import os
import sys
import time
import logging
from logging.handlers import RotatingFileHandler

from app.config import settings

LOG_DIR = os.path.abspath(settings.LOG_DIR)
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, settings.LOG_FILE)


class SafeRotatingFileHandler(RotatingFileHandler):
    """A RotatingFileHandler that survives a rollover it is not allowed to make.

    Windows refuses to rename a file while any other handle is open on it, and
    this application is routinely more than one process against one log
    directory — uvicorn --workers, the API alongside a worker, a test session
    that spawns a server. So the moment `app.log` crossed LOG_MAX_BYTES, the
    rename raised

        PermissionError: [WinError 32] The process cannot access the file
        because it is being used by another process

    from inside `logging` itself. Two things followed, both bad: a full
    traceback was printed to stderr for EVERY record from then on, drowning the
    console; and the file never rotated, so it grew without bound — the exact
    failure the size limit exists to prevent.

    Rotation is housekeeping. Losing the log because housekeeping failed is the
    worse outcome, so a blocked rollover is reported once, retried no more often
    than every `_RETRY_SECONDS`, and logging carries on to the existing file
    meanwhile. Whichever process does get the rename through rotates for all of
    them.
    """

    _RETRY_SECONDS = 60.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rollover_blocked_until = 0.0

    def shouldRollover(self, record):
        # While a rollover is known to be blocked, do not re-attempt the rename
        # on every single record — that is one failed syscall per log line.
        if time.time() < self._rollover_blocked_until:
            return False
        return super().shouldRollover(record)

    def doRollover(self):
        try:
            super().doRollover()
            self._rollover_blocked_until = 0.0
        except OSError as exc:
            self._rollover_blocked_until = time.time() + self._RETRY_SECONDS
            # The base class closes the stream before rotating, so reopen it
            # here rather than leaving the handler with nowhere to write.
            if self.stream is None:
                self.stream = self._open()
            print(
                f"[logger] log rotation deferred for {self.baseFilename}: {exc}",
                file=sys.stderr,
            )

def setup_logger():
    logger = logging.getLogger()
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(log_level)

    # Avoid duplicate handlers
    if logger.handlers:
        return logger

    # Log Formatter
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s [%(name)s:%(lineno)d] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Rotating File Handler (configured via settings)
    file_handler = SafeRotatingFileHandler(
        LOG_FILE,
        maxBytes=settings.LOG_MAX_BYTES,
        backupCount=settings.LOG_BACKUP_COUNT,
        encoding="utf-8"
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)

    # Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger

app_logger = setup_logger()
