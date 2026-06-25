"""
Agent check configuration management.

Handles loading, caching, and managing check configurations including:
- Fetching checks from server API
- Offline caching for resilience (with encryption)
- Check type registration
- Configuration transformation (server format -> agent format)

Security: Checks cache is encrypted using Fernet to protect database passwords
and other sensitive configuration data stored in check definitions.
"""

import base64
import hashlib
import json
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from shared.logger import get_logger

from app.agent.credentials import AgentCredentials
from app.checks.base import BaseCheck

logger = get_logger("luxswirl.agent.check_manager")


class CheckManager:
    """Manages check configurations and registration."""

    # Encryption salt for checks cache (public, not secret)
    ENCRYPTION_SALT = b"luxswirl-checks-cache-v1"

    def __init__(
        self,
        config: dict,
        cache_file: Path,
        credentials: AgentCredentials,
        # Callback for re-registration when agent becomes orphaned
        on_orphaned_agent,
    ):
        """
        Initialize check manager.

        Args:
            config: Agent configuration
            cache_file: Path to checks cache file
            credentials: AgentCredentials instance
            on_orphaned_agent: Callback for re-registration when agent is orphaned
        """
        self.config = config
        self.cache_file = cache_file
        self.credentials = credentials
        self.on_orphaned_agent = on_orphaned_agent
        self.logger = logger

        # Check type registry
        self.check_registry: dict[str, type[BaseCheck]] = {}

        # Check stats tracking
        self.check_stats: dict[str, dict] = {}

        # Config version tracking
        self.config_version: str | None = None

    def _get_encryption_key(self) -> bytes:
        """
        Derive encryption key for checks cache.

        Uses same approach as AgentCredentials - derives key from hostname + machine-id.
        This ensures checks cache can be decrypted after container restarts.

        Returns:
            32-byte Fernet-compatible encryption key (base64-encoded)
        """
        hostname = socket.gethostname()

        # Try to read machine-id
        machine_id = ""
        for path_str in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
            try:
                machine_id = Path(path_str).read_text().strip()
                break
            except FileNotFoundError, PermissionError:
                continue

        # Fallback: use salt only
        if not machine_id:
            machine_id = self.ENCRYPTION_SALT.decode()

        # Derive key using PBKDF2
        key_material = f"{hostname}:{machine_id}".encode()
        key_bytes = hashlib.pbkdf2_hmac("sha256", key_material, self.ENCRYPTION_SALT, 100000)

        return base64.urlsafe_b64encode(key_bytes)

    def register_check_type(self, name: str, check_class: type[BaseCheck]) -> None:
        """
        Register a check type with the agent.

        Args:
            name: The name of the check type
            check_class: The class implementing the check
        """
        self.check_registry[name] = check_class
        self.logger.info(
            "Registered check type",
            extra={"check_type": name},
        )

    def save_checks_cache(self, checks: list[dict[str, Any]], version: str | None = None) -> None:
        """
        Save checks to encrypted cache file for offline capability.

        Checks contain sensitive data (database passwords, API keys) so they
        are encrypted using Fernet before writing to disk.

        Args:
            checks: List of check configurations
            version: Config version to cache
        """
        try:
            agent_id = self.config.get("agent_id", "unknown")
            cache_data = {
                "checks": checks,
                "config_version": version or self.config_version,
                "cached_at": datetime.utcnow().isoformat(),
                "agent_id": str(agent_id) if agent_id != "unknown" else "unknown",
                "check_count": len(checks),
            }

            # Encrypt the cache data
            fernet = Fernet(self._get_encryption_key())
            plaintext = json.dumps(cache_data).encode()
            encrypted = fernet.encrypt(plaintext)

            # Write encrypted binary data
            self.cache_file.write_bytes(encrypted)

            # Set restrictive permissions
            try:
                self.cache_file.chmod(0o600)
            except Exception:
                self.logger.warning(
                    "Failed to set 0600 permissions on cache file",
                    exc_info=True,
                )

            self.logger.debug(
                "Saved checks to encrypted cache",
                extra={
                    "check_count": len(checks),
                    "cache_file": str(self.cache_file),
                    "version": version,
                },
            )
        except Exception:
            self.logger.warning("Failed to save checks cache", exc_info=True)

    def load_checks_cache(self) -> tuple[list[dict[str, Any]], str | None]:
        """
        Load checks from encrypted cache file (with auto-migration from plaintext).

        Returns:
            Tuple of (checks, config_version)
        """
        try:
            if not self.cache_file.exists():
                return [], None

            # Read file content as binary to support both formats
            content = self.cache_file.read_bytes()

            # Try plaintext JSON first (legacy format)
            try:
                cache_data = json.loads(content.decode())
                self.logger.info("Loaded plaintext checks cache (legacy format)")

                # Auto-migrate to encrypted format
                checks = cache_data.get("checks", [])
                version = cache_data.get("config_version")
                if checks:
                    self.logger.info("Migrating checks cache to encrypted format...")
                    self.save_checks_cache(checks, version)
                    self.logger.info("✅ Migration complete - checks cache now encrypted")

            except json.JSONDecodeError, UnicodeDecodeError:
                # Not JSON - must be encrypted format
                fernet = Fernet(self._get_encryption_key())
                try:
                    decrypted = fernet.decrypt(content)
                    cache_data = json.loads(decrypted.decode())
                    self.logger.debug("Loaded encrypted checks cache")
                except InvalidToken:
                    self.logger.error(
                        "Failed to decrypt checks cache - encryption key may have changed "
                        "(hostname or machine-id changed). Cache will be refreshed from server."
                    )
                    return [], None

            checks = cache_data.get("checks", [])
            version = cache_data.get("config_version")
            cached_at = cache_data.get("cached_at")

            self.logger.info(
                "Loaded checks from cache",
                extra={
                    "check_count": len(checks),
                    "cached_at": cached_at,
                    "version": version,
                },
            )
            return checks, version

        except Exception:
            self.logger.warning("Failed to load checks cache", exc_info=True)
            return [], None

    async def fetch_checks_from_server(self) -> list[dict[str, Any]]:
        """
        Fetch checks from the server API.

        Returns:
            List of check configurations from the server
        """
        agent_id = self.config.get("agent_id")
        if not agent_id:
            self.logger.warning("No agent_id configured, cannot fetch checks")
            return []

        push_url = self.config.get("push_url", "http://localhost:9000")
        auth_key = self.config.get("auth_key")

        # Extract base URL
        if "/api/v1" in push_url:
            base_url = push_url.split("/api/v1")[0]
        else:
            base_url = push_url.rstrip("/")

        # Build API URL with agent_id as query parameter
        api_url = f"{base_url}/api/v1/checks?agent_id={agent_id}"

        headers = {}
        if auth_key:
            headers["Authorization"] = f"Bearer {auth_key}"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(api_url, headers=headers, timeout=10.0)
                response.raise_for_status()
                data = response.json()

                checks: list[dict[str, Any]] = data.get("checks", [])
                self.logger.info(
                    "Fetched checks from server",
                    extra={"check_count": len(checks)},
                )
                return checks

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                # Agent not found - credentials are invalid (orphaned agent)
                self.logger.warning(
                    "Agent not found in server (orphaned - likely database reset)",
                    extra={"agent_id": str(agent_id)},
                )
                self.logger.info("Clearing invalid credentials and attempting re-registration")

                # Clear invalid credentials
                if not self.credentials.clear():
                    self.logger.error("Failed to clear invalid credentials")
                    raise Exception("Cannot recover from orphaned state") from e

                # Call callback for re-registration
                if self.on_orphaned_agent:
                    if await self.on_orphaned_agent():
                        self.logger.info(
                            "Re-registration successful",
                            extra={"agent_id": str(self.config.get("agent_id"))},
                        )
                        # Retry fetching checks with new credentials
                        return await self.fetch_checks_from_server()
                    else:
                        self.logger.error("Re-registration failed - cannot recover")
                        raise Exception("Cannot recover from orphaned state") from e
                else:
                    raise Exception("Cannot recover from orphaned state") from e
            else:
                self.logger.error(
                    "Failed to fetch checks from server",
                    extra={"status_code": e.response.status_code},
                )
                raise
        except Exception:
            self.logger.error("Failed to fetch checks from server", exc_info=True)
            raise

    async def load_checks(self) -> list[BaseCheck]:
        """
        Load checks from server API (or fallback to cache/local config).

        Returns:
            List of instantiated check objects
        """
        checks = []
        check_configs = []

        # Try to fetch from server first
        try:
            server_checks = await self.fetch_checks_from_server()
            check_configs = server_checks
            self.logger.info(
                "Loaded checks from server",
                extra={"check_count": len(check_configs)},
            )
            # Save to cache for offline capability
            self.save_checks_cache(check_configs, self.config_version)
        except Exception:
            # Fallback to cache if server unavailable
            self.logger.warning("Could not fetch from server", exc_info=True)
            cached_checks, cached_version = self.load_checks_cache()

            if cached_checks:
                check_configs = cached_checks
                # Restore the cached version so we stay in sync
                self.config_version = cached_version
                self.logger.info(
                    "Using cached checks",
                    extra={"cached_version": cached_version},
                )
            else:
                # No fallback to local config - server is source of truth
                self.logger.warning("No cache available and server unreachable - no checks to load")
                check_configs = []

        # Process check configurations
        for check_cfg in check_configs:
            check_type = check_cfg.get("check_type")
            check_name = check_cfg.get("display_name", "unknown")
            if not check_type:
                self.logger.warning(
                    "Check missing check_type",
                    extra={"check_name": check_name},
                )
                continue

            # Skip internal checks (agent self-monitoring)
            if check_type == "internal":
                continue

            check_class = self.check_registry.get(check_type)
            if not check_class:
                self.logger.warning(
                    "Unknown check type",
                    extra={"check_type": check_type},
                )
                continue

            try:
                # Transform server format to agent format
                agent_check_cfg = self._transform_check_config(check_cfg, check_type, check_name)

                # Skip disabled checks
                if not agent_check_cfg["enabled"]:
                    self.logger.debug(
                        "Skipping disabled check",
                        extra={"check_name": agent_check_cfg["name"]},
                    )
                    continue

                check = check_class(agent_check_cfg)
                checks.append(check)
                self.logger.info(
                    "Loaded check",
                    extra={"check_name": check.name, "check_type": check_type},
                )

                # Initialize stats for this check
                self.check_stats[check.name] = {
                    "total_runs": 0,
                    "successes": 0,
                    "failures": 0,
                    "total_latency": 0,
                }
            except Exception:
                self.logger.error(
                    "Failed to load check",
                    extra={"check_name": check_name},
                    exc_info=True,
                )

        return checks

    def _transform_check_config(
        self, check_cfg: dict, check_type: str, check_name: str
    ) -> dict[str, Any]:
        """
        Transform server check format to agent check format.

        Args:
            check_cfg: Check configuration from server
            check_type: Type of check
            check_name: Name of check (for logging)

        Returns:
            Transformed check configuration for agent
        """
        agent_check_cfg = {
            "check_id": check_cfg.get("id"),  # UUID from server
            "name": check_cfg.get("display_name"),  # For logging only
            "check_type": check_type,
            "target": check_cfg.get("target"),
            "interval": check_cfg.get("interval_seconds", 60),
            "timeout": check_cfg.get("timeout_seconds", 5),
            "enabled": check_cfg.get("enabled", True),
        }

        # Only add type-specific fields if they exist (not None)
        # HTTP/JSON check fields
        if check_cfg.get("http_method") is not None:
            agent_check_cfg["http_method"] = check_cfg["http_method"]
        if check_cfg.get("expected_status") is not None:
            agent_check_cfg["expected_status"] = check_cfg["expected_status"]
        if check_cfg.get("json_path") is not None:
            agent_check_cfg["json_path"] = check_cfg["json_path"]
        if check_cfg.get("expected_value") is not None:
            agent_check_cfg["expected_value"] = check_cfg["expected_value"]

        # DNS check fields
        if check_cfg.get("record_type") is not None:
            agent_check_cfg["record_type"] = check_cfg["record_type"]
        if check_cfg.get("nameserver") is not None:
            agent_check_cfg["nameserver"] = check_cfg["nameserver"]
        if check_cfg.get("expect_value") is not None:
            agent_check_cfg["expect_value"] = check_cfg["expect_value"]

        # MySQL/Postgres check fields
        if check_cfg.get("connection_string") is not None:
            agent_check_cfg["connection_string"] = check_cfg["connection_string"]
        if check_cfg.get("query") is not None:
            agent_check_cfg["query"] = check_cfg["query"]

        # Synthetic check fields
        if check_cfg.get("script_code") is not None:
            agent_check_cfg["script_code"] = check_cfg["script_code"]

        # For TCP checks, parse port from target (format: "hostname:port")
        if check_type == "tcp":
            target = check_cfg.get("target", "")
            if ":" in target:
                host, port_str = target.rsplit(":", 1)
                try:
                    port = int(port_str)
                    agent_check_cfg["target"] = host
                    agent_check_cfg["port"] = port
                except ValueError:
                    self.logger.warning(
                        "Invalid port in TCP check",
                        extra={"check_name": check_name, "target": target},
                    )
            else:
                self.logger.warning(
                    "TCP check missing port in target",
                    extra={"check_name": check_name, "target": target},
                )

        # For HTTP checks, pass verify_ssl setting (defaults to True if not specified)
        if check_type == "http":
            verify_ssl = check_cfg.get("verify_ssl")
            if verify_ssl is not None:
                agent_check_cfg["verify_ssl"] = verify_ssl

        # For DNS checks, ensure port is set (may come from check_config)
        if check_type == "dns":
            dns_port: int | None = check_cfg.get("port")
            if dns_port is not None:
                agent_check_cfg["port"] = dns_port

        return agent_check_cfg
