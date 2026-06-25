"""
Application configuration using Pydantic Settings.

This replaces the old config.py with a more structured approach.
"""

from functools import lru_cache
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Database configuration settings."""

    url: str = Field(
        default="postgresql+asyncpg://luxswirl:luxswirl@localhost:5432/luxswirl",
        description="Database connection URL",
    )
    echo: bool = Field(default=False, description="Echo SQL statements")
    pool_size: int = Field(default=20, ge=1, le=100, description="Connection pool size")
    max_overflow: int = Field(default=10, ge=0, le=50, description="Max overflow connections")
    pool_pre_ping: bool = Field(default=True, description="Verify connections before use")

    model_config = SettingsConfigDict(
        env_prefix="DATABASE_",
        extra="ignore",  # Ignore extra fields
    )


class ServerSettings(BaseSettings):
    """LuxSwirl server configuration."""

    host: str = Field(default="0.0.0.0", description="Host to bind to")
    port: int = Field(default=9000, ge=1, le=65535, description="Port to listen on")
    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Environment",
    )
    workers: int = Field(default=1, ge=1, le=16, description="Number of worker processes")
    reload: bool = Field(default=False, description="Enable auto-reload for development")

    # CORS settings
    # CRITICAL: Set this to your public-facing URL that users access in their browser
    # Example: If users access https://luxswirl.example.com:9000/, set to:
    #   SERVER__CORS_ORIGINS='["https://luxswirl.example.com:9000"]'
    # MUST be configured - no safe default exists
    cors_origins: list[str] = Field(
        default=[],  # Empty - MUST be set via environment or compose file
        description=(
            "Allowed CORS origins - MUST match the exact URL users access in browser. "
            "This is your nginx public domain (e.g., https://luxswirl.example.com:9000)"
        ),
    )
    cors_credentials: bool = Field(
        default=True,
        description="Allow credentials (cookies/auth headers) in CORS requests",
    )
    cors_methods: list[str] = Field(default=["*"], description="Allowed HTTP methods")
    cors_headers: list[str] = Field(default=["*"], description="Allowed headers")

    # Query time windows
    agent_active_window_minutes: int = Field(
        default=10,
        ge=1,
        le=1440,
        description="Consider agent active if seen within this many minutes",
    )
    latest_results_window_minutes: int = Field(
        default=5,
        ge=1,
        le=60,
        description="Show latest results from last N minutes",
    )
    metrics_ttl_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Metrics stale after N seconds",
    )

    # Retention
    default_retention_days: int = Field(
        default=90,
        ge=1,
        le=3650,
        description="Default data retention in days",
    )

    # Agent communication intervals (global defaults)
    default_heartbeat_interval: int = Field(
        default=5,
        ge=1,
        le=600,
        description="Default heartbeat interval in seconds",
    )
    default_check_sync_interval: int = Field(
        default=60,
        ge=10,
        le=600,
        description="Default check sync interval in seconds",
    )

    # Job system settings
    job_retention_days: int = Field(
        default=7,
        ge=1,
        le=365,
        description="How long to keep completed jobs before auto-purge (days)",
    )
    job_purge_interval_hours: int = Field(
        default=1,
        ge=1,
        le=24,
        description="How often to run job cleanup task (hours)",
    )
    job_max_dispatch_per_heartbeat: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum jobs to dispatch per heartbeat",
    )

    # Database maintenance settings
    database_maintenance_interval_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="How often to run database maintenance (VACUUM, bloat cleanup) in hours",
    )

    model_config = SettingsConfigDict(
        env_prefix="SERVER_",
        extra="ignore",  # Ignore extra fields
    )


class SecuritySettings(BaseSettings):
    """Security configuration."""

    auth_enabled: bool = Field(default=True, description="Enable authentication")
    auth_tokens: list[str] = Field(
        default_factory=list,
        description="Valid API tokens. Resolved at startup via env → /app/data/api_token → generate.",
    )
    secret_key: str = Field(
        default="",
        description="Secret key for JWT. Resolved at startup via env → /app/data/secret_key → generate.",
    )
    algorithm: str = Field(default="HS256", description="JWT algorithm")
    access_token_expire_minutes: int = Field(
        default=30,
        ge=5,
        le=43200,
        description="Access token expiration in minutes",
    )

    # Initial admin user (created on first run if no admin exists)
    initial_admin_username: str = Field(
        default="admin",
        description="Default admin username (created on first run)",
    )
    initial_admin_password: str = Field(
        default="",
        description=(
            "Admin password for unattended/automation setup. If set, the admin is "
            "seeded on first boot with must_change_password enforced. If empty, no "
            "default admin is created and the first-run /setup wizard handles "
            "interactive admin creation (no default credentials ship)."
        ),
    )

    # Rate Limiting
    rate_limit_enabled: bool = Field(
        default=True,
        description="Enable rate limiting on authentication endpoints",
    )
    login_rate_limit: str = Field(
        default="10/15minutes",
        description="Rate limit for login attempts per IP (format: count/period)",
    )
    api_rate_limit: str = Field(
        default="100/minute",
        description="Rate limit for general API requests per IP",
    )
    registration_rate_limit: str = Field(
        default="5/hour",
        description="Rate limit for agent registration per IP",
    )

    # Trusted proxy CIDR ranges for X-Forwarded-For handling.
    # When the direct TCP peer is in one of these networks, the leftmost-untrusted
    # X-Forwarded-For hop is used as the client IP for rate-limiting + audit logs.
    # Default covers Docker bridge / RFC 1918 / loopback. Tighten in production
    # if you know your reverse proxy's exact CIDR.
    trusted_proxy_networks: list[str] = Field(
        default=[
            "127.0.0.0/8",
            "::1/128",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
        ],
        description=(
            "CIDR ranges of trusted reverse proxies. X-Forwarded-For is honored only "
            "when the direct TCP peer is within one of these networks."
        ),
    )

    # Session Cookie Configuration
    session_cookie_name: str = Field(
        default="luxswirl_session",
        description="Name of session cookie (must be unique if multiple apps on same subdomain)",
    )
    session_cookie_httponly: bool = Field(
        default=True,
        description="HTTPOnly flag for session cookie (prevents JavaScript access)",
    )
    session_cookie_secure: bool = Field(
        default=True,
        description="Secure flag for session cookie (HTTPS only)",
    )
    session_cookie_samesite: str = Field(
        default="lax",
        description="SameSite attribute for session cookie (strict/lax/none)",
    )
    session_cookie_path: str = Field(
        default="/",
        description="Path for session cookie",
    )

    # Field-level encryption for sensitive database data.
    # Resolved at startup via env override → /app/data/field_encryption_key →
    # auto-generate-and-persist (see core/secrets.py:resolve_runtime_secrets).
    # Operator never has to type this value; existing deployments with the env
    # var set continue to work unchanged.
    field_encryption_key: str = Field(
        default="",
        description=(
            "Fernet key for encrypting sensitive database fields (check targets, "
            "check_config, connection strings). Auto-generated on first boot if unset; "
            "override with SECURITY__FIELD_ENCRYPTION_KEY env var if injecting from a "
            "secrets manager."
        ),
    )

    @field_validator("field_encryption_key")
    @classmethod
    def validate_encryption_key(cls, v: str) -> str:
        """Validate Fernet key format if a value is provided.

        Empty values are allowed at config-load time — `resolve_runtime_secrets()`
        will fill them in during lifespan startup before any DB query runs.
        """
        if not v or v.strip() == "":
            return ""

        try:
            Fernet(v.encode())  # raises if invalid format/length
        except Exception as e:
            raise ValueError(
                f"Invalid SECURITY__FIELD_ENCRYPTION_KEY format: {e}\n"
                "Key must be a valid Fernet key (base64-encoded 32 bytes)"
            ) from e

        return v

    model_config = SettingsConfigDict(env_prefix="SECURITY_")


class LoggingSettings(BaseSettings):
    """Logging configuration."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Log level",
    )
    format: str = Field(
        default="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        description="Log format",
    )
    enable_file_log: bool = Field(default=True, description="Enable file logging")
    log_dir: str = Field(default="logs", description="Log directory")
    max_bytes: int = Field(default=10485760, description="Max log file size (10MB)")
    backup_count: int = Field(default=5, description="Number of backup log files")

    # Module-specific log levels (format: "module.path=LEVEL")
    # Example: luxswirl.services.check=WARNING, luxswirl.services.check_result=WARNING
    module_levels: dict[str, str] = Field(
        default_factory=lambda: {
            "luxswirl.services.check": "ERROR",
            "luxswirl.services.check_result": "ERROR",
        },
        description="Module-specific log levels",
    )

    model_config = SettingsConfigDict(env_prefix="LOG_")


class Settings(BaseSettings):
    """Main application settings."""

    app_name: str = Field(default="LuxSwirl", description="Application name")
    app_version: str = Field(default="dev", description="Application version")
    build_timestamp: str = Field(default="unknown", description="Build timestamp (ISO 8601)")
    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Environment",
    )
    debug: bool = Field(default=False, description="Debug mode")

    # Sub-settings
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    # API versioning
    api_v1_prefix: str = Field(default="/api/v1", description="API v1 prefix")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
    )

    @property
    def is_production(self) -> bool:
        """Check if running in production."""
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development."""
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    """
    Get cached settings instance.

    Returns:
        Settings instance
    """
    return Settings()


# Convenience accessor
settings = get_settings()
