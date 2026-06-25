"""
LuxSwirl Server API - Main application entry point.

A production-quality SaaS observability monitoring platform for all types of metrics.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from shared.logger import configure_logging, get_logger
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
from app.background.maintenance_handlers import register_all as register_maintenance_handlers
from app.background.maintenance_worker import (
    start_maintenance_worker,
    stop_maintenance_worker,
)
from app.core.config import settings
from app.core.exceptions import LuxSwirlException
from app.core.rate_limit import limiter
from app.core.secrets import resolve_runtime_secrets
from app.core.security_headers import SecurityHeadersMiddleware
from app.db import get_session_maker
from app.db.database import close_db, init_db
from app.db.scheduler_init import init_scheduler_defaults
from app.notifications.providers.email import EmailNotificationProvider
from app.notifications.providers.homeassistant import HomeAssistantNotificationProvider
from app.notifications.providers.webhook import WebhookNotificationProvider
from app.notifications.registry import NotificationRegistry
from app.services.core.metrics_collector_core_service import MetricsCollectorCoreService
from app.services.core.scheduler_core_service import scheduler_service
from app.services.core.settings_core_service import SettingsCoreService
from app.services.core.user_core_service import UserCoreService
from app.web.routers._render import render_error_response
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
from app.web.routers.setup_router import router as setup_router
from app.web.routers.status_pages_router import router as status_pages_router
from app.web.routers.status_router import router as status_router
from app.web.routers.users_router import router as web_users_router
from app.web.template_filters import register_filters, update_settings_cache

# Create module-level logger for middleware (before lifespan)
logger = get_logger("luxswirl.server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.

    Handles startup and shutdown events.
    """
    # Configure logging in lifespan so it works with uvicorn --reload
    configure_logging(
        {
            "log_level": settings.logging.level,
            "module_levels": settings.logging.module_levels,
        }
    )
    logger = get_logger("luxswirl.server")

    # Startup
    logger.info("Starting LuxSwirl Server API server...")
    logger.info("Environment", extra={"environment": settings.server.environment})
    logger.info("Database", extra={"database_url": settings.database.url})
    logger.info("CORS origins", extra={"cors_origins": settings.server.cors_origins})

    # Resolve runtime secrets (SECRET_KEY, auth_tokens) before any code reads them.
    resolve_runtime_secrets(settings)

    # Initialize database
    await init_db()
    logger.info("Database initialized successfully")

    # Ensure default admin user exists (first-run setup)
    session_maker = get_session_maker()
    async with session_maker() as db:
        user_service = UserCoreService()
        admin = await user_service.ensure_default_admin(db)
        if admin is not None:
            logger.info("Default admin check complete", extra={"username": admin.username})
        else:
            logger.info("No admin configured - first-run setup wizard enabled (/setup)")

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

        # Persist the admin seed + ensure_*_defaults written above. Startup runs
        # outside the request lifecycle, so this block owns its transaction
        # (get_db() only auto-commits request-scoped sessions). Without this, the
        # seeded admin and settings are rolled back when the session closes.
        await db.commit()

    # Register notification providers
    NotificationRegistry.register("email", EmailNotificationProvider)
    NotificationRegistry.register("webhook", WebhookNotificationProvider)
    NotificationRegistry.register("homeassistant", HomeAssistantNotificationProvider)
    logger.info(
        "Registered notification providers",
        extra={"count": NotificationRegistry.count()},
    )

    # Initialize scheduler default job configurations
    async with session_maker() as db:
        await init_scheduler_defaults(db)
        await db.commit()
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
    openapi_url=("/openapi.json" if settings.server.environment != "production" else None),
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

# Security headers middleware (SWIRL-43)
app.add_middleware(SecurityHeadersMiddleware)


# Exception handlers
@app.exception_handler(LuxSwirlException)
async def luxswirl_exception_handler(request: Request, exc: LuxSwirlException):
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
async def value_error_handler(request: Request, exc: ValueError):
    """Service-layer validation raises bare ValueError — a client error (400).

    Web routers used to catch these and render a 400 partial themselves; now
    they propagate here so the handling lives in exactly one place.
    """
    get_logger("luxswirl.server").warning("Value error", extra={"message": str(exc)})
    return render_error_response(
        request,
        status.HTTP_400_BAD_REQUEST,
        str(exc),
        error_code="BAD_REQUEST",
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle request validation errors."""
    get_logger("luxswirl.server").warning(
        "Validation error",
        extra={"validation_errors": exc.errors()},
    )
    return render_error_response(
        request,
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "Request validation failed",
        error_code="VALIDATION_ERROR",
        detail=exc.errors(),
    )


@app.exception_handler(SQLAlchemyError)
async def database_exception_handler(request: Request, exc: SQLAlchemyError):
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
async def general_exception_handler(request: Request, exc: Exception):
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


# Mount static files (package-relative so it resolves regardless of CWD)
_WEB_DIR = Path(__file__).parent / "web"
app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

# Configure Jinja2 templates with custom filters
templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))
register_filters(templates.env)

# Include routers
app.include_router(metrics_router)  # Prometheus metrics (root level /metrics)
app.include_router(api_router)  # JSON API routes
app.include_router(web_auth_router)  # Web UI - auth (login/logout)
app.include_router(setup_router)  # Web UI - first-run setup wizard
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
@app.get("/health", tags=["Health"])
async def health_check():
    """
    Health check endpoint.

    Returns the current status of the API.
    """
    return {
        "status": "healthy",
        "version": settings.app_version,
        "environment": settings.server.environment,
    }


# Root API endpoint (only for /api prefix)
@app.get("/api", tags=["Root"])
async def api_root():
    """
    API root endpoint.

    Returns basic API information.
    """
    return {
        "name": "LuxSwirl Server API",
        "description": "A production-quality SaaS observability monitoring platform",
        "version": settings.app_version,
        "docs": "/docs" if settings.server.environment != "production" else None,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.environment == "development",
        log_level=settings.logging.level.lower(),
    )
