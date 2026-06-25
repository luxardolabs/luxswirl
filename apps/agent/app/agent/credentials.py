"""
Agent credentials manager.

Handles saving/loading agent UUID and API key to persistent storage.

Credentials are encrypted using Fernet (AES-128-CBC + HMAC) with a key derived
from container-specific data (hostname + machine-id). This provides protection
against casual file inspection while maintaining deterministic decryption across
container restarts.
"""

import base64
import hashlib
import json
import os
import socket
from pathlib import Path
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from shared.logger import get_logger

logger = get_logger("luxswirl.credentials")


class AgentCredentials:
    """
    Manages agent credentials (UUID and API key).

    Credentials are encrypted at rest using Fernet encryption with a key derived
    from container-specific data. This provides protection against file inspection
    while maintaining backward compatibility with plaintext files (auto-migration).
    """

    # Encryption salt (public, not secret - security comes from hostname+machine-id)
    ENCRYPTION_SALT = b"luxswirl-agent-credentials-v1"

    def __init__(self, credentials_file: str = "/app/data/agent_credentials.json"):
        """
        Initialize credentials manager.

        Args:
            credentials_file: Path to credentials file (must be in mounted volume)
        """
        self.credentials_file = Path(credentials_file)
        self.agent_id: UUID | None = None
        self.api_key: str | None = None
        self._encryption_enabled = True  # Can be disabled via env var for debugging

        # Check if encryption should be disabled (for testing/migration)
        if os.getenv("LUXSWIRL_DISABLE_CREDENTIAL_ENCRYPTION") == "true":
            logger.warning(
                "⚠️  Credential encryption DISABLED via LUXSWIRL_DISABLE_CREDENTIAL_ENCRYPTION"
            )
            self._encryption_enabled = False

    def _get_encryption_key(self) -> bytes:
        """
        Derive encryption key from container-specific data.

        Uses hostname + machine-id to create a deterministic key unique to this
        container but consistent across restarts. This allows credentials to be
        decrypted after container restart without external key storage.

        Returns:
            32-byte Fernet-compatible encryption key (base64-encoded)

        Security notes:
        - Key is derived from public data (hostname, machine-id)
        - Protects against casual file inspection
        - Does NOT protect if attacker has filesystem + can read hostname/machine-id
        - For stronger security, use OS keyring or external KMS (future enhancement)
        """
        # Get hostname (Docker container ID or system hostname)
        hostname = socket.gethostname()

        # Try to read machine-id (persistent across container restarts if in volume)
        machine_id = ""
        for path_str in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
            try:
                machine_id = Path(path_str).read_text().strip()
                break
            except FileNotFoundError, PermissionError:
                continue

        # Fallback: use hostname only (less secure but still better than plaintext)
        if not machine_id:
            logger.debug("machine-id not found, using hostname-only encryption key")
            machine_id = self.ENCRYPTION_SALT.decode()

        # Derive 32-byte key using PBKDF2-HMAC-SHA256
        key_material = f"{hostname}:{machine_id}".encode()
        key_bytes = hashlib.pbkdf2_hmac(
            "sha256",
            key_material,
            self.ENCRYPTION_SALT,
            100000,  # iterations
        )

        # Fernet requires base64-encoded 32-byte key
        return base64.urlsafe_b64encode(key_bytes)

    def load(self) -> bool:
        """
        Load credentials from file (supports both encrypted and plaintext).

        Automatically detects file format and migrates plaintext to encrypted.

        Returns:
            True if credentials loaded successfully, False if file doesn't exist
        """
        if not self.credentials_file.exists():
            logger.info(
                "Credentials file not found",
                extra={"credentials_file": str(self.credentials_file)},
            )
            return False

        try:
            # Read file content (binary mode to support both formats)
            content = self.credentials_file.read_bytes()

            # Try to parse as JSON first (plaintext legacy format)
            try:
                data = json.loads(content.decode())
                logger.info("Loaded plaintext credentials (legacy format)")

                # Migrate to encrypted format
                if self._encryption_enabled:
                    logger.info("Migrating to encrypted format...")
                    agent_id = UUID(data["agent_id"])
                    api_key = data.get("api_key")
                    self.save(agent_id, api_key)
                    logger.info("✅ Migration complete - credentials now encrypted")

            except json.JSONDecodeError, UnicodeDecodeError:
                # Not JSON - must be encrypted format
                if not self._encryption_enabled:
                    logger.error("Encrypted credentials found but encryption is disabled")
                    return False

                # Decrypt the content
                fernet = Fernet(self._get_encryption_key())
                try:
                    decrypted = fernet.decrypt(content)
                    data = json.loads(decrypted.decode())
                    logger.debug("Loaded encrypted credentials")
                except InvalidToken:
                    logger.error(
                        "Failed to decrypt credentials - encryption key may have changed "
                        "(hostname or machine-id changed). Delete credentials file to re-register."
                    )
                    return False

            # Parse credentials
            self.agent_id = UUID(data["agent_id"])
            self.api_key = data.get("api_key")  # Optional - may not be set yet

            logger.info(
                "Loaded agent credentials",
                extra={"agent_id": str(self.agent_id)},
            )
            return True

        except Exception:
            logger.error(
                "Failed to load credentials",
                extra={"credentials_file": str(self.credentials_file)},
                exc_info=True,
            )
            return False

    def save(self, agent_id: UUID, api_key: str | None = None) -> bool:
        """
        Save credentials to file (encrypted by default).

        Args:
            agent_id: Agent UUID from registration
            api_key: Optional API key from approval

        Returns:
            True if saved successfully
        """
        try:
            # Ensure directory exists
            self.credentials_file.parent.mkdir(parents=True, exist_ok=True)

            # Prepare credentials data
            data = {
                "agent_id": str(agent_id),
            }

            if api_key:
                data["api_key"] = api_key

            if self._encryption_enabled:
                # Encrypt credentials with Fernet
                fernet = Fernet(self._get_encryption_key())
                plaintext = json.dumps(data).encode()
                encrypted = fernet.encrypt(plaintext)

                # Write encrypted binary data
                self.credentials_file.write_bytes(encrypted)
                logger.debug("Saved encrypted credentials")
            else:
                # Write plaintext JSON (for debugging/testing only)
                with open(self.credentials_file, "w") as f:
                    json.dump(data, f, indent=2)
                logger.warning("Saved plaintext credentials (encryption disabled)")

            # Set restrictive file permissions (owner read/write only)
            try:
                self.credentials_file.chmod(0o600)
            except Exception:
                logger.warning(
                    "Failed to set 0600 permissions on credentials file",
                    exc_info=True,
                )

            # Update in-memory state
            self.agent_id = agent_id
            self.api_key = api_key

            logger.info(
                "Saved agent credentials",
                extra={"agent_id": str(agent_id)},
            )
            return True

        except Exception:
            logger.error(
                "Failed to save credentials",
                extra={"credentials_file": str(self.credentials_file)},
                exc_info=True,
            )
            return False

    def has_credentials(self) -> bool:
        """Check if credentials are loaded."""
        return self.agent_id is not None

    def clear(self) -> bool:
        """
        Clear credentials from memory and delete credentials file.

        This is used when credentials are detected as invalid (orphaned agent)
        so the agent can re-register with the server.

        Returns:
            True if cleared successfully
        """
        try:
            # Clear in-memory credentials
            self.agent_id = None
            self.api_key = None

            # Delete credentials file if it exists
            if self.credentials_file.exists():
                self.credentials_file.unlink()
                logger.info(
                    "Deleted credentials file",
                    extra={"credentials_file": str(self.credentials_file)},
                )
            else:
                logger.debug("No credentials file to delete")

            return True

        except Exception:
            logger.error("Failed to clear credentials", exc_info=True)
            return False
