"""
LuxSwirl Server API - Main application entry point.

A production-quality SaaS observability monitoring platform for all types of metrics.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import SQLAlchemyError

from app.api import api_router
from app.api.v1.routers.metrics_router import router as metrics_router
from app.background import (
    start_database_maintenance_task,
    start_job_purge_task,
    start_session_cleanup_task,
    stop_database_maintenance_task,
    stop_job_purge_task,
    stop_session_cleanup_task,
)
from app.background.maintenance_handlers import (
    register_all as register_maintenance_handlers,
)
from app.background.maintenance_worker import (
    start_maintenance_worker,
    stop_maintenance_worker,
)
from app.cli import bootstrap_on_startup
from app.core.config import settings
from app.core.exceptions import LuxSwirlException, value_error_message
from app.core.logging_config import configure_logging
from app.core.logging_options import logging_options
from app.core.rate_limit import limiter
from app.core.same_origin import SameOriginMiddleware
from app.core.secrets import resolve_runtime_secrets
from app.core.security_headers import SecurityHeadersMiddleware
from app.core.synthetic_security import SyntheticSecurityError
from app.core.trusted_host import AllowedHostsMiddleware
from app.db import worker_session
from app.db.database import close_db, init_db
from app.db.scheduler_init import init_scheduler_defaults
from app.notifications.providers.email import EmailNotificationProvider
from app.notifications.providers.homeassistant import HomeAssistantNotificationProvider
from app.notifications.providers.webhook import WebhookNotificationProvider
from app.notifications.registry import NotificationRegistry
from app.schemas.health_schema import ApiInfoResponse, HealthResponse
from app.services.core.metrics_collector_core_service import MetricsCollectorCoreService
from app.services.core.scheduler_core_service import scheduler_service
from app.services.core.settings_core_service import SettingsCoreService
from app.shared.logger import get_logger
from app.web.render import render_error_response
from app.web.routers.admin_bootstrap_router import router as admin_bootstrap_router
from app.web.routers.agents_router import router as agents_router
from app.web.routers.alerts_router import router as alerts_router
from app.web.routers.artifacts_router import router as artifacts_router
from app.web.routers.auth_router import router as web_auth_router
from app.web.routers.check_router import router as check_router
from app.web.routers.checks_router import router as checks_router
from app.web.routers.database_health_router import router as database_health_router
from app.web.routers.import_export_router import router as import_export_router
from app.web.routers.jobs_router import router as jobs_router
from app.web.routers.maintenance_router import router as maintenance_router
from app.web.routers.notification_logs_router import router as notification_logs_router
from app.web.routers.notification_providers_router import (
    router as notification_providers_router,
)
from app.web.routers.profile_router import router as profile_router
from app.web.routers.registration_keys_router import router as registration_keys_router
from app.web.routers.scheduler_router import router as scheduler_router
from app.web.routers.settings_router import router as settings_router
from app.web.routers.status_pages_router import router as status_pages_router
from app.web.routers.status_router import router as status_router
from app.web.routers.users_router import router as web_users_router
from app.web.template_filters import update_settings_cache

# The fleet logging setup, once per process, before anything is served. At import
# time, so the uvicorn --reload worker (which re-imports this module) gets it too.
configure_logging(**logging_options())

# Create module-level logger for middleware (before lifespan)
logger = get_logger("luxswirl.server")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Application lifespan manager.

    Handles startup and shutdown events.
    """
    logger = get_logger("luxswirl.server")

    # Startup
    logger.info("Starting LuxSwirl Server API server...")
    logger.info("Environment", extra={"environment": settings.server.environment})
    logger.info("Database", extra={"database_url": settings.database.url})
    logger.info("CORS origins", extra={"cors_origins": settings.server.cors_origins})

    # Resolve runtime secrets (SECRET_KEY, field_encryption_key) before any code reads them.
    resolve_runtime_secrets(settings)

    # Initialize database
    await init_db()
    logger.info("Database initialized successfully")

    # The first admin (FLEET-ADMIN-BOOTSTRAP-STANDARD): seeded from INITIAL_ADMIN_*
    # only while the users table is empty, otherwise /setup opens behind a one-time
    # token printed in this log. It owns its own transaction.
    retired = sorted(
        k for k in os.environ if k.upper().startswith("SECURITY__INITIAL_ADMIN_")
    )
    if retired:
        logger.warning(
            "Retired settings are set and ignored: the first admin now comes from "
            "INITIAL_ADMIN_USERNAME / INITIAL_ADMIN_PASSWORD, and only on an empty users "
            "table. Remove them from the env file.",
            extra={"retired_keys": retired},
        )
    await bootstrap_on_startup()

    # Startup runs outside the request lifecycle, so worker_session() owns this
    # transaction: it commits the defaults on a clean exit, rolls back on error.
    async with worker_session() as db:
        # Ensure default settings exist
        await SettingsCoreService.ensure_default_settings(db)
        logger.info("Default settings initialized")

        # Load general settings and update template filter cache
        general_settings = await SettingsCoreService.get_general_settings(db)
        update_settings_cache(
            timezone=general_settings.get("timezone"),
            date_format=general_settings.get("date_format"),
            time_format=general_settings.get("time_format"),
        )
        logger.info(
            "Template filters configured",
            extra={"timezone": general_settings.get("timezone")},
        )

        # Rebuild Prometheus metrics from database (in-memory collector)
        await MetricsCollectorCoreService.rebuild_from_database(db, lookback_minutes=10)
        logger.info("Prometheus metrics rebuilt from database")

    # Register notification providers
    NotificationRegistry.register("email", EmailNotificationProvider)
    NotificationRegistry.register("webhook", WebhookNotificationProvider)
    NotificationRegistry.register("homeassistant", HomeAssistantNotificationProvider)
    logger.info(
        "Registered notification providers",
        extra={"count": NotificationRegistry.count()},
    )

    # Initialize scheduler default job configurations
    async with worker_session() as db:
        await init_scheduler_defaults(db)
    logger.info("Scheduler defaults initialized")

    # Start background tasks
    start_job_purge_task()
    start_session_cleanup_task()
    start_database_maintenance_task()
    register_maintenance_handlers()
    await start_maintenance_worker()

    # Start async scheduler
    await scheduler_service.start()

    yield

    # Shutdown
    logger.info("Shutting down LuxSwirl Server API server...")

    # Stop async scheduler
    await scheduler_service.stop()

    # Stop background tasks
    await stop_maintenance_worker()
    await stop_job_purge_task()
    await stop_session_cleanup_task()
    await stop_database_maintenance_task()

    await close_db()
    logger.info("Database connections closed")


# Create FastAPI application
app = FastAPI(
    title="LuxSwirl Server API",
    description="A production-quality SaaS observability monitoring platform for all types of metrics",
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.server.environment != "production" else None,
    redoc_url="/redoc" if settings.server.environment != "production" else None,
    openapi_url=(
        "/openapi.json" if settings.server.environment != "production" else None
    ),
)


# Rate Limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]


# CORS middleware
# Validate CORS configuration for production
if settings.server.environment == "production":
    if not settings.server.cors_origins:
        logger.error(
            "CORS CONFIGURATION REQUIRED: SERVER__CORS_ORIGINS must be set in production. "
            "Example: SERVER__CORS_ORIGINS='[\"https://your-domain.com:9000\"]'"
        )
        raise ValueError("CORS origins not configured for production")

    if "*" in settings.server.cors_origins and settings.server.cors_credentials:
        logger.error(
            "INSECURE CONFIGURATION: allow_origins=['*'] with allow_credentials=True "
            "is not allowed in production. Set SERVER__CORS_ORIGINS to specific domains."
        )
        raise ValueError("Insecure CORS configuration detected in production mode")

    if not settings.security.auth_enabled:
        logger.error(
            "INSECURE CONFIGURATION: auth_enabled=False disables ALL authentication "
            "(API, agent, registration). It must not be used in production."
        )
        raise ValueError("auth_enabled=False is not allowed in production")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.server.cors_origins,
    allow_credentials=settings.server.cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security headers middleware
app.add_middleware(SecurityHeadersMiddleware)

# Cross-site guard (FLEET-AUTH-SESSION-STANDARD §5): a browser POST/PUT/PATCH/DELETE from
# another site is refused before any route runs, so a hostile page cannot sign a visitor
# into another account or sign them out. The app's own hosts are ALLOWED_HOSTS plus the
# hosts the operator allowed as CORS origins (a web UI served from another domain).
# Requests without browser headers (the agent, webhooks, the CLI) pass.
app.add_middleware(
    SameOriginMiddleware,
    allowed_hosts=settings.same_origin_hosts,
)

# Trusted hosts: added last, so it is the outermost layer and a forged Host is refused with
# 400 before any other middleware or route sees the request (FLEET-BUILD-DEPLOY-STANDARD).
app.add_middleware(
    AllowedHostsMiddleware,
    allowed_hosts=settings.allowed_hosts,
    health_paths=("/health",),
    # No www redirect: it builds an http:// URL without the port, while nginx
    # serves TLS on 9000, so the redirect would land nowhere.
    www_redirect=False,
)


# Exception handlers
@app.exception_handler(LuxSwirlException)
async def luxswirl_exception_handler(
    request: Request, exc: LuxSwirlException
) -> Response:
    """Render a LuxSwirl domain exception (content-negotiated)."""
    get_logger("luxswirl.server").warning(
        "LuxSwirl exception",
        extra={"exception_message": exc.message, "details": exc.details},
    )
    return render_error_response(
        request,
        exc.status_code,
        exc.message,
        error_code=exc.error_code,
        detail=exc.details or None,
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> Response:
    """Service-layer validation raises bare ValueError — a client error (400).

    Web routers used to catch these and render a 400 partial themselves; now
    they propagate here so the handling lives in exactly one place.
    """
    message = value_error_message(exc)
    get_logger("luxswirl.server").warning(
        "Value error", extra={"error_message": message}
    )
    return render_error_response(
        request,
        status.HTTP_400_BAD_REQUEST,
        message,
        error_code="BAD_REQUEST",
    )


@app.exception_handler(SyntheticSecurityError)
async def synthetic_security_error_handler(
    request: Request, exc: SyntheticSecurityError
) -> Response:
    """A synthetic script the security validation refuses: a client error (422)."""
    return render_error_response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        str(exc),
        error_code="VALIDATION_ERROR",
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> Response:
    """Handle request validation errors.

    The errors carry the offending `input`, which for a body that is not valid
    JSON is raw bytes — not serializable. Rendering them straight made the
    handler itself raise, so a client sending the wrong content-type got a 500
    instead of the 422 that describes what was wrong.
    """
    errors = jsonable_encoder(exc.errors())
    get_logger("luxswirl.server").warning(
        "Validation error",
        extra={"validation_errors": errors},
    )
    return render_error_response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "Request validation failed",
        error_code="VALIDATION_ERROR",
        detail=errors,
    )


@app.exception_handler(SQLAlchemyError)
async def database_exception_handler(
    request: Request, exc: SQLAlchemyError
) -> Response:
    """Handle database errors."""
    get_logger("luxswirl.server").error("Database error", exc_info=True)
    detail = str(exc) if settings.server.environment != "production" else None
    return render_error_response(
        request,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "A database error occurred",
        error_code="DATABASE_ERROR",
        detail=detail,
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception) -> Response:
    """Handle all other exceptions."""
    get_logger("luxswirl.server").error("Unhandled exception", exc_info=True)
    detail = str(exc) if settings.server.environment != "production" else None
    return render_error_response(
        request,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "An internal server error occurred",
        error_code="INTERNAL_SERVER_ERROR",
        detail=detail,
    )


# Mount static files (package-relative so it resolves regardless of CWD).
# Fleet-canonical location: app/static/, a sibling of app/templates/.
_APP_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(_APP_DIR / "static")), name="static")

# Include routers
app.include_router(metrics_router)  # Prometheus metrics (root level /metrics)
app.include_router(api_router)  # JSON API routes
app.include_router(web_auth_router)  # Web UI - auth (login/logout)
app.include_router(admin_bootstrap_router)  # Web UI - first-admin /setup (token-gated)
app.include_router(profile_router)  # Web UI - user profile
app.include_router(status_router)  # Web UI - status page
app.include_router(check_router)  # Web UI - check details
app.include_router(agents_router)  # Web UI - agents
app.include_router(checks_router)  # Web UI - checks management
app.include_router(artifacts_router)  # Web UI - artifacts viewing
app.include_router(status_pages_router)  # Web UI - status pages management
app.include_router(import_export_router)  # Web UI - import/export
app.include_router(settings_router)  # Web UI - settings
app.include_router(database_health_router)  # Web UI - database health
app.include_router(web_users_router)  # Web UI - user management
app.include_router(notification_providers_router)  # Web UI - notification providers
app.include_router(registration_keys_router)  # Web UI - registration keys
app.include_router(alerts_router)  # Web UI - alerts
app.include_router(notification_logs_router)  # Web UI - notification logs
app.include_router(jobs_router)  # Web UI - jobs management
app.include_router(maintenance_router)  # Web UI - maintenance job status polling
app.include_router(scheduler_router)  # Web UI - scheduler admin


# Health check endpoint
@app.get("/health", tags=["Health"], response_model=HealthResponse, name="health_check")
async def health_check() -> HealthResponse:
    """
    Health check endpoint.

    Returns the current status of the API.
    """
    return HealthResponse(
        status="healthy",
        version=settings.app_version,
        commit=settings.build_commit,
        environment=settings.server.environment,
    )


# Root API endpoint (only for /api prefix)
@app.get("/api", tags=["Root"], response_model=ApiInfoResponse, name="api_root")
async def api_root() -> ApiInfoResponse:
    """
    API root endpoint.

    Returns basic API information.
    """
    return ApiInfoResponse(
        name="LuxSwirl Server API",
        description="A production-quality SaaS observability monitoring platform",
        version=settings.app_version,
        docs="/docs" if settings.server.environment != "production" else None,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.environment == "development",
        log_level=settings.logging.level.lower(),
    )
