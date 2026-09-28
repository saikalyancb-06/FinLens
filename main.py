import asyncio
import os
from contextlib import asynccontextmanager
import time
import logging
from collections import defaultdict
from fastapi import Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import text

from app.config import settings
from app.database.session import engine, Base
import app.models
import app.aa.models

# Importing this registers SQLAlchemy session listeners that invalidate a user's
# cached figures whenever a write touches the tables those figures are built
# from. It is imported here, at the top level, so the listeners are installed
# before any request can be served — an endpoint that wrote through an
# unregistered session would leave the cache serving figures for data that no
# longer exists.
from app.services import cache_invalidation
cache_invalidation.install()
from app.utils.logger import setup_logger
from app.utils.metrics import metrics_collector
from app.utils.security import get_current_user

from app.api.transactions import router as transactions_router
from app.api.auth import router as auth_router
from app.api.files import router as files_router
from app.api.dashboard import router as dashboard_router
from app.api.reports import router as reports_router
from app.email.routes import router as email_router
from app.rpa.routes import router as rpa_router
from app.aa.routes import router as aa_router
from app.api.reconciliation import router as reconciliation_router
from app.api.deduplication import router as deduplication_router
from app.api.bank_master import router as bank_master_router
from app.api.settings import router as settings_router
from app.api.review_queue import router as review_queue_router
from app.api.compliance import router as compliance_router
from app.api.currency import router as currency_router
from app.api.categories import router as categories_router

# The B2B analysis API. Mounted on the same process as the internal application
# deliberately: it reuses the same parsers, the same categorisation stack and
# the same analysis kernels, and splitting it into a second service before
# there is a scaling reason to would mean shipping that code twice.
from app.b2b.router import router as b2b_router
from app.b2b.admin import router as b2b_admin_router
from app.b2b.consolidate_router import router as b2b_consolidate_router
import app.b2b.models  # noqa: F401 — registers the API tables on Base.metadata

# Honours DB_AUTO_CREATE, exactly as app/database/session.py does. This call used
# to run unconditionally, which defeated the setting entirely: in production,
# where Alembic owns the schema and DB_AUTO_CREATE defaults to false, importing
# main still built tables straight from the models. A model that had drifted
# ahead of the migrations would then quietly create the table it expected, and
# the next `alembic upgrade` would collide with it.
if settings.DB_AUTO_CREATE:
    Base.metadata.create_all(bind=engine)

# Ensure uploads/email directory exists
os.makedirs(os.path.join(settings.UPLOAD_DIR, "email"), exist_ok=True)
os.makedirs(os.path.join(settings.UPLOAD_DIR, "rpa_screens"), exist_ok=True)

logger = setup_logger()

# Background FX rate refresh.
#
# Run inside the application's lifespan rather than as a separate process: this
# is a single-node local deployment, and a second process to poll two URLs would
# be more moving parts than the job needs. The consequence is worth stating -
# rates refresh only while the application is running, so a machine that has
# been off for a week comes back with week-old rates until the first poll.
#
# `lifespan` rather than @app.on_event: the latter is deprecated and this
# codebase runs current FastAPI.
@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event = None
    task = None

    # Compare the models against the live database before serving anything. A
    # column that exists on a model but not in the database fails at query time,
    # deep inside an endpoint, as a 500 the traceback blames on the query rather
    # than on the missing migration. Reporting it here puts the cause where
    # somebody will see it, with the SQL to fix it.
    #
    # Reports only — it never alters the schema. A process that rewrites its own
    # tables on boot is how two servers sharing one database corrupt each other.
    try:
        from app.database.schema_check import log_schema_drift
        from app.database.session import Base, engine
        import app.models  # noqa: F401 - registers every table on Base.metadata
        log_schema_drift(engine, Base)
    except Exception:
        logger.exception("[Schema] drift check failed; startup continues")

    # The Bank Master dropdown is populated from the `banks` table, which nothing
    # used to fill: a freshly created database rendered an empty selector with no
    # way to recover from the UI. Seeding here is idempotent and never edits or
    # removes an existing row.
    try:
        from app.database.session import SessionLocal
        from app.services.bank_seeder import seed_banks

        _db = SessionLocal()
        try:
            seed_banks(_db)
        finally:
            _db.close()
    except Exception:
        logger.exception("[Bank Master] seeding failed; startup continues")

    # Mailbox scans run in a thread pool inside this process, so a scan that was
    # in flight when the process last stopped is gone but its row still says
    # RUNNING. Left alone, the UI polls that row forever. Marking them failed at
    # startup turns an invisible hang into a message the user can act on.
    try:
        from app.database.session import SessionLocal
        from app.statements.scan_runner import reap_stale_scans

        _db = SessionLocal()
        try:
            _reaped = reap_stale_scans(_db, older_than_minutes=0)
            if _reaped:
                logger.info("[Mailbox] marked %s interrupted scan(s) as failed", _reaped)
        finally:
            _db.close()
    except Exception:
        logger.exception("[Mailbox] stale-scan reaping failed; startup continues")

    # Microsoft connector startup status check
    if not (settings.MICROSOFT_CLIENT_ID and settings.MICROSOFT_CLIENT_SECRET):
        logger.warning(
            "[Microsoft] Microsoft connector disabled: set MICROSOFT_CLIENT_ID / MICROSOFT_CLIENT_SECRET "
            "to enable Microsoft 365 / Outlook integration."
        )
    else:
        logger.info(
            "[Microsoft] Microsoft connector enabled for tenant '%s' (redirect_uri: %s)",
            settings.MICROSOFT_TENANT,
            settings.MICROSOFT_REDIRECT_URI,
        )

    # Google connector startup status check
    if not (settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET):
        logger.warning(
            "[Google] Gmail connector disabled: set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET "
            "to enable Google / Gmail integration."
        )
    else:
        logger.info(
            "[Google] Gmail connector enabled (redirect_uri: %s)",
            settings.GOOGLE_REDIRECT_URI,
        )

    if settings.FX_REFRESH_ENABLED:
        from app.currency.refresher import refresh_loop

        stop_event = asyncio.Event()
        task = asyncio.create_task(refresh_loop(stop_event))
        logger.info("[FX] rate refresh every %s minutes (first poll in %ss)",
                    settings.FX_REFRESH_MINUTES, settings.FX_REFRESH_STARTUP_DELAY)
    else:
        logger.info("[FX] rate refresh disabled (FX_REFRESH_ENABLED=false)")

    # B2B housekeeping: result retention, webhook retries, stuck-request
    # reaping. See app/b2b/maintenance.py for why each exists.
    b2b_stop = None
    b2b_task = None
    if os.getenv("B2B_MAINTENANCE_ENABLED", "true").lower() == "true":
        from app.b2b.maintenance import maintenance_loop

        b2b_stop = asyncio.Event()
        b2b_task = asyncio.create_task(maintenance_loop(b2b_stop))
        logger.info("[B2B] maintenance loop started")

    try:
        yield
    finally:
        if b2b_stop is not None and b2b_task is not None:
            b2b_stop.set()
            try:
                await asyncio.wait_for(b2b_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                b2b_task.cancel()
        if stop_event is not None and task is not None:
            stop_event.set()
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # A refresher mid-request must not hold up shutdown. The work
                # lost is one rate fetch, recovered on the next poll.
                task.cancel()


# Root Application
#
# The interactive docs are open to anyone who can reach the service — they need
# no token, because they are generated before any dependency runs. In
# development that is exactly what you want. In production it publishes the
# complete endpoint map of a treasury system, including every request schema, to
# unauthenticated callers, which is free reconnaissance and buys the operator
# nothing: the people who need the schema can read this repository.
#
# Set ENABLE_API_DOCS=true to put them back in production (behind a private
# network, or temporarily while debugging an integration). The OpenAPI document
# itself is withheld with them — leaving /openapi.json up would hand over the
# same information without the UI.
_docs_enabled = settings.ENVIRONMENT != "production" or settings.ENABLE_API_DOCS

app = FastAPI(
    title="Financial Statement Classifier & Treasury Intelligence API",
    version="1.0.0",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    lifespan=lifespan,
)

# 0. Response compression
#
# index.html is a single 620 KB file: the whole UI, including the ~610 KB JSX
# block, is inline. It is deliberately served no-store (see the FileResponse
# headers further down) so a deploy is picked up immediately, which means those
# 620 KB crossed the wire on EVERY page load and every hard navigation. The
# browser-side compile cache added in static/boot.js removes the cost of
# *compiling* that source repeatedly, but it cannot avoid re-downloading it —
# it has to hash the source to know its cache is still valid.
#
# gzip takes that payload to roughly 90 KB. It also compresses the JSON
# analytics responses, several of which are tens of kilobytes.
#
# minimum_size skips the compression round-trip for small bodies, where the CPU
# cost outweighs the saving. Registered first so it wraps every response,
# including the static mount.
app.add_middleware(GZipMiddleware, minimum_size=1000)

# 1. CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# 2. Rate Limiting Middleware (In-memory sliding window per client IP)
#
# NOTE: this state is per-process. Running N uvicorn workers effectively allows
# N x RATE_LIMIT_PER_MINUTE. A shared store (Redis) is required for a global limit.
rate_limit_records = defaultdict(list)
_last_rate_limit_sweep = time.time()
RATE_LIMIT_SWEEP_INTERVAL = 300.0


def _client_ip(request: Request) -> str:
    """Resolve the client IP, honouring X-Forwarded-For only behind a trusted proxy.

    X-Forwarded-For is caller-controlled: trusting it unconditionally lets anyone
    bypass the limit by rotating the header. It is only consulted when the
    deployment declares it sits behind a proxy.
    """
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Left-most entry is the original client.
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


# The B2B API's own routes. Not simply "/v1/": the internal UI's categories,
# bank-master, review-queue, reconciliation and deduplication routers live under
# /v1 too, and their callers expect the {"detail": ...} shape.
_B2B_PATH_PREFIXES = (
    "/v1/analyze", "/v1/usage", "/v1/health", "/v1/ready", "/v1/version",
    "/v1/formats", "/v1/statements", "/internal/clients",
)


@app.middleware("http")
async def rate_limiting_middleware(request: Request, call_next):
    global _last_rate_limit_sweep

    if request.url.path in ["/health", "/readiness", "/dashboard-ui", "/", "/static"]:
        return await call_next(request)

    client_ip = _client_ip(request)
    now = time.time()
    window_start = now - 60.0

    # Evict IPs with no recent activity. Without this the dict grows without bound
    # (one permanent entry per IP ever seen), which a scan turns into a slow leak.
    if now - _last_rate_limit_sweep > RATE_LIMIT_SWEEP_INTERVAL:
        for ip in [ip for ip, hits in rate_limit_records.items() if not hits or hits[-1] <= window_start]:
            del rate_limit_records[ip]
        _last_rate_limit_sweep = now

    rate_limit_records[client_ip] = [t for t in rate_limit_records[client_ip] if t > window_start]

    if len(rate_limit_records[client_ip]) >= settings.RATE_LIMIT_PER_MINUTE:
        logger.warning(f"[Rate Limit Exceeded] Client IP {client_ip} reached limit.")
        if request.url.path.startswith(_B2B_PATH_PREFIXES):
            # B2B callers are promised one error shape (docs/B2B_API.md §4):
            # switch on error.code, quote request_id. This per-IP limiter runs
            # before the API's own per-client limiter, so without this branch
            # an integrator saw {"detail": ...} with no code, no request_id and
            # no Retry-After — the one 429 their client could not parse.
            rid = getattr(request.state, "b2b_request_id", None)
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"error": {
                    "code": "RATE_LIMIT_EXCEEDED",
                    "message": "Too many requests from this IP address. "
                               "Retry after 60 seconds.",
                    "request_id": rid}},
                headers={"Retry-After": "60"},
            )
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Too many requests. Rate limit exceeded. Please try again in a minute."}
        )

    rate_limit_records[client_ip].append(now)
    return await call_next(request)

# 3. Security Headers Middleware
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    return response

# 3b. SPA route disambiguation
#
# `/transactions` is claimed twice: once by the transactions API router and once
# by the single-page app below. FastAPI matches in registration order, and the
# router is registered first, so typing /transactions into the address bar - or
# refreshing the tab, or opening a bookmark - returned {"detail":"Not
# authenticated"} instead of the application. Every other tab worked, because no
# other tab's path collides with an API prefix.
#
# Reordering is not available: whichever is registered first shadows the other,
# and shadowing the API would break every caller. The two requests are however
# trivially distinguishable - a browser navigating to a page sends
# `Accept: text/html` and no bearer token, while the front-end's own fetch sends
# neither - so this intercepts exactly that case and serves the app.
SPA_PATHS = {
    "/", "/home", "/pricing", "/contact", "/login", "/dashboard", "/dashboard-ui",
    "/transactions", "/categories", "/ingestion", "/reconciliation", "/review-queue",
    "/reports", "/settings", "/bank-master",
}


@app.middleware("http")
async def spa_navigation_middleware(request: Request, call_next):
    if (request.method == "GET"
            and request.url.path in SPA_PATHS
            and "text/html" in request.headers.get("accept", "")
            and not request.headers.get("authorization")):
        index_path = os.path.join(
            os.path.abspath(os.path.join(os.path.dirname(__file__), "app", "static")),
            "index.html",
        )
        if os.path.exists(index_path):
            return FileResponse(
                index_path,
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
    return await call_next(request)


# 4. Include API Routers
app.include_router(auth_router)
app.include_router(files_router)
app.include_router(transactions_router)
app.include_router(dashboard_router)
app.include_router(reports_router)
app.include_router(email_router)
app.include_router(rpa_router)
app.include_router(aa_router)
app.include_router(reconciliation_router)
app.include_router(deduplication_router)
app.include_router(bank_master_router)
app.include_router(settings_router)
app.include_router(review_queue_router)
app.include_router(compliance_router)
app.include_router(categories_router)

# /v1/* — the external B2B surface. Authenticated by API key, not by the
# session cookie the internal UI uses, so it is registered with no dependency
# override and does its own auth per route.
app.include_router(b2b_router)
app.include_router(b2b_consolidate_router)
app.include_router(b2b_admin_router)


# ---------------------------------------------------------------- B2B plumbing
#
# Two pieces the /v1 API needs and the internal UI does not.
#
# 1. Every request gets an id before anything can fail, so the same string
#    appears in the response, the log line, the usage record and any webhook.
#    Generated here rather than in the route because a request rejected by auth
#    or by the rate limiter never reaches a route and still needs one.
# 2. One handler renders every ApiError. Without it FastAPI would return a bare
#    500 and, worse, the exception text — which is exactly what must not cross
#    the boundary.
import uuid as _uuid

from app.b2b.errors import ApiError as _ApiError


@app.middleware("http")
async def b2b_request_id_middleware(request: Request, call_next):
    rid = request.headers.get("X-Request-Id") or f"req_{_uuid.uuid4().hex[:24]}"
    request.state.b2b_request_id = rid
    response = await call_next(request)
    response.headers["X-Request-Id"] = rid
    return response


@app.exception_handler(_ApiError)
async def b2b_api_error_handler(request: Request, exc: _ApiError):
    rid = getattr(request.state, "b2b_request_id", None)
    # 5xx means we broke, so it is worth a stack trace in our log. 4xx is the
    # caller being told something true about their request and is not an
    # incident; logging those at error level would bury the real ones.
    if exc.status_code >= 500:
        logger.exception("[%s] %s: %s", rid, exc.code, exc.message)
    else:
        logger.info("[%s] %s: %s", rid, exc.code, exc.message)
    return JSONResponse(status_code=exc.status_code,
                        content=exc.to_api(rid),
                        headers=exc.headers or None)
app.include_router(currency_router)


# 5. Serve Treasury Intelligence Dashboard UI
static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "app", "static"))
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/", tags=["UI"])
@app.get("/home", tags=["UI"])
@app.get("/pricing", tags=["UI"])
@app.get("/contact", tags=["UI"])
@app.get("/login", tags=["UI"])
@app.get("/dashboard", tags=["UI"])
@app.get("/dashboard-ui", tags=["UI"])
@app.get("/transactions", tags=["UI"])
@app.get("/categories", tags=["UI"])
@app.get("/ingestion", tags=["UI"])
@app.get("/reconciliation", tags=["UI"])
@app.get("/review-queue", tags=["UI"])
@app.get("/reports", tags=["UI"])
@app.get("/settings", tags=["UI"])
@app.get("/bank-master", tags=["UI"])
def get_dashboard_ui():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return JSONResponse(status_code=404, content={"message": f"UI index.html not found at {index_path}"})

# 5b. Background FX rate refresh — see the lifespan handler defined above.

# 6. Health & Readiness Operations
@app.get("/health", tags=["Operations"])
def health_check():
    return {"status": "healthy", "service": "financial-backend-api", "timestamp": time.time()}

@app.get("/readiness", tags=["Operations"])
def readiness_check():
    db_ok = False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        logger.error(f"[Readiness Failure] Database connectivity check failed: {e}")
        db_ok = False

    if db_ok:
        return {"status": "ready", "database": "connected", "timestamp": time.time()}
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"status": "not_ready"})

@app.get("/metrics", tags=["Operations"])
def get_system_metrics(current_user=Depends(get_current_user)):
    """Operational metrics. Authenticated: the summary exposes upload volumes,
    error counts and processing internals that should not be public."""
    return metrics_collector.get_metrics_summary()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
