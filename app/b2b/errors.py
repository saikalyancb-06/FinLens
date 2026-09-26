"""One error shape, one place that maps a code to an HTTP status.

Clients switch on `error.code`, not on prose and not on the status line, so the
codes below are part of the public contract and must not be renamed. The
message is for a human reading a log; the code is for software.

Nothing here ever carries a traceback, an internal path, a SQL fragment or a
library name outward. `internal_error()` swallows the detail into the server
log under the request id and returns a fixed sentence — the request id is the
only thing the caller needs to quote for us to find the real cause.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# ------------------------------------------------------------------ 400 family
BAD_REQUEST = "BAD_REQUEST"
MISSING_FILE = "MISSING_FILE"
INVALID_PARAMETER = "INVALID_PARAMETER"
MALFORMED_REQUEST = "MALFORMED_REQUEST"

# ------------------------------------------------------------------ 401 / 403
MISSING_API_KEY = "MISSING_API_KEY"
INVALID_API_KEY = "INVALID_API_KEY"
REVOKED_API_KEY = "REVOKED_API_KEY"
EXPIRED_API_KEY = "EXPIRED_API_KEY"
INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"
CLIENT_DISABLED = "CLIENT_DISABLED"

# ------------------------------------------------------------------ 404 / 409
REQUEST_NOT_FOUND = "REQUEST_NOT_FOUND"
IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
REQUEST_IN_PROGRESS = "REQUEST_IN_PROGRESS"
# Creating something whose unique identifier is already taken. 409, not 400:
# the request is well-formed and would have been valid a moment earlier, so it
# is a conflict with current state rather than a malformed input. An integrator
# retrying a setup script needs to tell those apart — "already done" is
# recoverable, "you sent nonsense" is not.
RESOURCE_ALREADY_EXISTS = "RESOURCE_ALREADY_EXISTS"

# ------------------------------------------------------------------ 413 / 415
FILE_TOO_LARGE = "FILE_TOO_LARGE"
REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
UNSUPPORTED_FILE_FORMAT = "UNSUPPORTED_FILE_FORMAT"

# ------------------------------------------------------------------ 422 family
FILE_CORRUPT = "FILE_CORRUPT"
FILE_EMPTY = "FILE_EMPTY"
PARSE_FAILED = "PARSE_FAILED"
NO_TRANSACTIONS_FOUND = "NO_TRANSACTIONS_FOUND"
PDF_PASSWORD_REQUIRED = "PDF_PASSWORD_REQUIRED"
PDF_PASSWORD_INVALID = "PDF_PASSWORD_INVALID"
FORMAT_MISMATCH = "FORMAT_MISMATCH"
ANALYSIS_FAILED = "ANALYSIS_FAILED"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

# ------------------------------------------------------------------ 429 / 5xx
RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
INTERNAL_ERROR = "INTERNAL_ERROR"
SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"

STATUS_BY_CODE: Dict[str, int] = {
    BAD_REQUEST: 400, MISSING_FILE: 400, INVALID_PARAMETER: 400,
    MALFORMED_REQUEST: 400,
    MISSING_API_KEY: 401, INVALID_API_KEY: 401, REVOKED_API_KEY: 401,
    EXPIRED_API_KEY: 401,
    INSUFFICIENT_SCOPE: 403, CLIENT_DISABLED: 403,
    REQUEST_NOT_FOUND: 404,
    IDEMPOTENCY_KEY_REUSED: 409, REQUEST_IN_PROGRESS: 409,
    RESOURCE_ALREADY_EXISTS: 409,
    FILE_TOO_LARGE: 413, REQUEST_TOO_LARGE: 413,
    UNSUPPORTED_FILE_FORMAT: 415,
    FILE_CORRUPT: 422, FILE_EMPTY: 422, PARSE_FAILED: 422,
    NO_TRANSACTIONS_FOUND: 422, PDF_PASSWORD_REQUIRED: 422,
    PDF_PASSWORD_INVALID: 422, FORMAT_MISMATCH: 422, ANALYSIS_FAILED: 422,
    INSUFFICIENT_DATA: 422,
    RATE_LIMIT_EXCEEDED: 429, QUOTA_EXCEEDED: 429,
    INTERNAL_ERROR: 500, SERVICE_UNAVAILABLE: 503,
}


class ApiError(Exception):
    """Raise this anywhere below the router; the handler renders it."""

    def __init__(self, code: str, message: str,
                 detail: Optional[Dict[str, Any]] = None,
                 status_code: Optional[int] = None,
                 headers: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.status_code = status_code or STATUS_BY_CODE.get(code, 400)
        self.headers = headers or {}

    def to_api(self, request_id: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"code": self.code, "message": self.message}
        if request_id:
            body["request_id"] = request_id
        if self.detail:
            body["detail"] = self.detail
        return {"error": body}
