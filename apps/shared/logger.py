"""Structured logging configuration for LuxSwirl.

Single setup_logging() call at process start configures the `luxswirl` namespace
root logger. All child loggers inherit handlers via propagation — no per-logger
handler configuration. JSON output by default; LOG__FORMAT=text for plaintext.
"""

import json
import logging
import logging.handlers
import os
import re
import sys
from typing import Any

from pythonjsonlogger import jsonlogger

_ROOT_NAMESPACE = "luxswirl"
_INITIALIZED = False


class CredentialFilter(logging.Filter):
    """Scrubs sensitive credentials from log messages and args."""

    PATTERNS = [
        # Database connection strings: mysql://user:PASSWORD@host -> mysql://user:***@host
        (re.compile(r"://([^:/@]+):([^@]+)@"), r"://\1:***@"),
        # JSON password fields
        (re.compile(r'"password"\s*:\s*"[^"]+"'), '"password":"***"'),
        (re.compile(r"'password'\s*:\s*'[^']+'"), "'password':'***'"),
        # Bearer tokens
        (re.compile(r"Bearer\s+[\w\-\.]+"), "Bearer ***"),
        # API keys and auth keys
        (re.compile(r"api_key[=:]\s*[\w\-]+"), "api_key=***"),
        (re.compile(r"auth_key[=:]\s*[\w\-]+"), "auth_key=***"),
        (re.compile(r"password[=:]\s*[\w\-]+"), "password=***"),
    ]

    # Standard LogRecord attributes — never treat as user data
    _RESERVED_ATTRS = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "taskName",
            "message",
            "asctime",
        }
    )

    # Extra keys whose values should always be scrubbed regardless of content
    _SENSITIVE_KEYS = frozenset({"password", "api_key", "auth_key", "bearer", "token", "secret"})

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            for pattern, replacement in self.PATTERNS:
                record.msg = pattern.sub(replacement, record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = self._scrub_dict(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._scrub_value(arg) if isinstance(arg, str) else arg for arg in record.args
                )
        # Scrub structured extras attached to the record (logger.info(..., extra={...}))
        for attr_name, attr_value in list(record.__dict__.items()):
            if attr_name in self._RESERVED_ATTRS or attr_name.startswith("_"):
                continue
            # Sensitive key name → mask outright
            if any(key in attr_name.lower() for key in self._SENSITIVE_KEYS):
                record.__dict__[attr_name] = "***"
                continue
            # String value → run through patterns
            if isinstance(attr_value, str):
                record.__dict__[attr_name] = self._scrub_value(attr_value)
            elif isinstance(attr_value, dict):
                record.__dict__[attr_name] = self._scrub_dict(attr_value)
        return True

    def _scrub_value(self, value: str) -> str:
        for pattern, replacement in self.PATTERNS:
            value = pattern.sub(replacement, value)
        return value

    def _scrub_dict(self, data: dict) -> dict:
        out: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str):
                out[key] = self._scrub_value(value)
            elif isinstance(value, dict):
                out[key] = self._scrub_dict(value)
            else:
                out[key] = value
        return out


class _LuxswirlJsonFormatter(jsonlogger.JsonFormatter):
    """JSON formatter with explicit standard fields."""

    def add_fields(self, log_record: dict, record: logging.LogRecord, message_dict: dict) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record["timestamp"] = self.formatTime(record, self.datefmt)
        log_record["level"] = record.levelname
        log_record["logger"] = record.name
        log_record["module"] = record.module
        log_record["function"] = record.funcName
        log_record["line"] = record.lineno
        if record.exc_info:
            log_record["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "traceback": self.formatException(record.exc_info) if record.exc_info else None,
            }


# Third-party libraries that default to noisy levels — force quieter unless overridden.
_THIRD_PARTY_DEFAULTS: dict[str, int] = {
    "uvicorn.access": logging.WARNING,
    "sqlalchemy": logging.WARNING,
    "sqlalchemy.engine": logging.WARNING,
    "sqlalchemy.engine.Engine": logging.WARNING,
    "sqlalchemy.pool": logging.WARNING,
    "asyncpg": logging.WARNING,
    "aiomysql": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "watchfiles": logging.WARNING,
    "alembic.runtime.migration": logging.WARNING,
}


def _parse_level(level_str: str) -> int:
    return getattr(logging, level_str.upper(), logging.INFO)


def _parse_module_levels(value: Any) -> dict[str, str]:
    """Accepts a dict or a JSON string; returns {module_name: level_name}."""
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except ValueError, TypeError:
            pass
    return {}


def setup_logging(config: dict[str, Any] | None = None) -> None:
    """Configure logging for the entire process. Idempotent — safe to call twice.

    Configures handlers ONLY on the `luxswirl` namespace root logger. All
    child loggers (luxswirl.agent, luxswirl.services.x, etc.) inherit through
    propagation, eliminating the multi-handler duplication that the previous
    per-logger setup produced.

    Args:
        config: Optional dict with keys:
            log_level: "DEBUG"|"INFO"|"WARNING"|"ERROR" (env: LOG__LEVEL)
            log_format: "json"|"text" (env: LOG__FORMAT)
            module_levels: dict[str,str] (env: LOG__MODULE_LEVELS as JSON)
            service_name: identifier for the service (defaults to "luxswirl")
    """
    global _INITIALIZED
    config = config or {}

    level_name = (config.get("log_level") or os.environ.get("LOG__LEVEL", "INFO")).upper()
    fmt = (config.get("log_format") or os.environ.get("LOG__FORMAT", "json")).lower()
    service_name = config.get("service_name", "luxswirl")

    module_levels = _parse_module_levels(
        config.get("module_levels") or os.environ.get("LOG__MODULE_LEVELS")
    )

    level = _parse_level(level_name)

    # Build the formatter
    if fmt == "text":
        formatter: logging.Formatter = logging.Formatter(
            "[%(asctime)s] %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    else:
        # python-json-logger picks up these field names from the format string
        formatter = _LuxswirlJsonFormatter(
            "%(timestamp)s %(level)s %(logger)s %(module)s %(function)s %(line)d %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )

    # Console handler — single, attached to the namespace root
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)  # handler accepts everything; loggers filter
    console_handler.setFormatter(formatter)

    # Configure the namespace root logger ONCE
    root = logging.getLogger(_ROOT_NAMESPACE)
    root.setLevel(level)
    # Replace any existing handlers (idempotent re-runs)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(console_handler)
    # Attach the credential filter once at the namespace root; all child records
    # inherit handlers and pass through this filter.
    if not any(isinstance(f, CredentialFilter) for f in root.filters):
        root.addFilter(CredentialFilter())
    # Don't propagate to the actual root logger — keeps our output isolated
    # from anything else that may have configured the global root.
    root.propagate = False

    # Suppress third-party noise unless explicitly overridden
    for module, default_level in _THIRD_PARTY_DEFAULTS.items():
        if module in module_levels:
            continue  # operator overrode it
        logging.getLogger(module).setLevel(default_level)

    # Apply per-module overrides
    for module, override_level_str in module_levels.items():
        logging.getLogger(module).setLevel(_parse_level(override_level_str))

    _INITIALIZED = True
    root.info(
        "Structured logging initialized",
        extra={
            "service": service_name,
            "configured_level": level_name,
            "format": fmt,
            "module_overrides": list(module_levels.keys()),
        },
    )


def configure_logging(config: dict[str, Any]) -> None:
    """Backward-compatible alias for setup_logging."""
    setup_logging(config)


def get_logger(name: str = _ROOT_NAMESPACE, config: dict[str, Any] | None = None) -> logging.Logger:
    """Return a logger. Lazy-initializes setup if not yet done.

    Args:
        name: Logger name. Conventional: "luxswirl.<area>" so messages inherit
              from the namespace root.
        config: Optional one-shot config (used only on first call).

    Returns:
        Configured logger instance. Child loggers have no handlers of their
        own — they propagate to the `luxswirl` root.
    """
    if not _INITIALIZED:
        setup_logging(config)
    return logging.getLogger(name)
