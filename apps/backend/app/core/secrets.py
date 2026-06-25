"""
Runtime secret resolution.

Three-tier resolution for system secrets the operator should never have to type:

    env var (explicit override)  →  /app/data/<name>  →  generate + persist

Used for SECRET_KEY (JWT signing) and the short-term auth_tokens fallback. The
admin password follows a different path (random + log-once banner) handled in
`services/core/user_core_service.py`.

Files are written mode 0600 in a directory that must be operator-mounted as a
persistent volume. If the directory is missing or unwritable, startup fails —
running without persistence would invalidate every session/agent on restart.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet
from shared.logger import get_logger

if TYPE_CHECKING:
    from app.core.config import Settings

logger = get_logger("luxswirl.secrets")

SECRETS_DIR = Path("/app/data")


def _read_file(path: Path) -> str | None:
    if not path.exists():
        return None
    value = path.read_text().strip()
    return value or None


def _write_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    path.chmod(0o600)


def load_or_generate(
    name: str,
    env_value: str | None,
    *,
    nbytes: int = 64,
    generator: Callable[[], str] | None = None,
    secrets_dir: Path | None = None,
) -> tuple[str, bool]:
    """Resolve a secret via env → file → generate.

    Returns (value, was_generated). was_generated is True only on the first-boot
    path so callers can decide whether to emit a one-time banner.

    Pass `generator` to override the default `secrets.token_urlsafe(nbytes)` —
    useful for secrets that need a specific format (e.g., Fernet keys).
    """
    if env_value:
        return env_value, False

    directory = secrets_dir or SECRETS_DIR
    path = directory / name

    existing = _read_file(path)
    if existing:
        return existing, False

    value = generator() if generator else secrets.token_urlsafe(nbytes)
    _write_file(path, value)
    logger.info("Generated and persisted secret on first boot", extra={"secret_name": name})
    return value, True


def _generate_fernet_key() -> str:
    """Generate a fresh Fernet key (base64-encoded 32 random bytes)."""
    return Fernet.generate_key().decode()


def resolve_runtime_secrets(settings: Settings) -> None:
    """Resolve SECRET_KEY, auth_tokens, and field_encryption_key.

    Mutates `settings.security` in place. Must run during lifespan startup,
    before any request handler or DB query reads these values. SECRET_KEY and
    field_encryption_key are silent. auth_tokens prints a one-time banner when
    freshly generated so the operator can copy it into scripts.
    """
    security = settings.security

    secret_key, _ = load_or_generate("secret_key", security.secret_key)
    security.secret_key = secret_key

    field_key, _ = load_or_generate(
        "field_encryption_key",
        security.field_encryption_key,
        generator=_generate_fernet_key,
    )
    security.field_encryption_key = field_key

    if not security.auth_tokens:
        api_token, generated = load_or_generate("api_token", None, nbytes=32)
        security.auth_tokens = [api_token]

        if generated:
            banner = (
                "\n"
                "================================================================\n"
                "  GENERATED INITIAL API TOKEN (first boot only)\n"
                "  Use this as the Bearer token for /api/v1/* requests:\n"
                f"      {api_token}\n"
                "  Stored at /app/data/api_token (mode 0600).\n"
                "  Override anytime with SECURITY__AUTH_TOKENS env var.\n"
                "================================================================"
            )
            logger.warning(banner)
