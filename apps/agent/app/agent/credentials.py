"""
Agent credentials manager.

Handles saving/loading the agent UUID, its API key and its recovery secret to
persistent storage.

Credentials are encrypted using Fernet (AES-128-CBC + HMAC) with a key derived
from container-specific data (hostname + machine-id). This provides protection
against casual file inspection while maintaining deterministic decryption across
container restarts.
"""

import base64
import errno
import hashlib
import json
import os
import socket
import tempfile
from pathlib import Path
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken

from app.shared.logger import get_logger

logger = get_logger("luxswirl.credentials")

# Why a rename cannot replace the credentials file: it is a mount of its own
# (EBUSY, EXDEV), or its directory takes no new file (EACCES, EPERM).
_NO_RENAME_ERRNOS = frozenset({errno.EBUSY, errno.EXDEV, errno.EACCES, errno.EPERM})


def _write_in_place(target: Path, content: bytes, reason: OSError) -> None:
    """Rewrite `target` in place, where no rename can replace it.

    Opened without truncating, written, then cut to length and flushed: a
    write that fails part way can still leave the file damaged, which is why
    this is only the fallback.
    """
    logger.warning(
        "Credentials file cannot be replaced atomically: rewriting it in place",
        extra={"credentials_file": str(target), "reason": reason.strerror},
    )
    fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory(directory: Path) -> None:
    """Flush a rename in `directory` to disk, where the filesystem can.

    The file is already replaced when this runs, so a filesystem that cannot
    flush a directory (EINVAL on some network and FUSE mounts) only loses the
    guarantee that the rename survives a power cut; the save still stands.
    """
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        logger.warning(
            "Could not flush the credentials directory after saving",
            extra={"directory": str(directory), "reason": exc.strerror},
        )


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
        # Issued once at registration; recover-key needs it with the
        # registration key. None for credentials saved before it existed.
        self.recovery_secret: str | None = None
        # True while api_key is a recovered key the file does not hold yet
        # (its save failed): retry_unsaved() writes it once it can.
        self.unsaved = False
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
                    self.save(agent_id, api_key, data.get("recovery_secret"))
                    logger.info("✅ Migration complete - credentials now encrypted")

            except json.JSONDecodeError, UnicodeDecodeError:
                # Not JSON - must be encrypted format
                if not self._encryption_enabled:
                    logger.exception(
                        "Encrypted credentials found but encryption is disabled"
                    )
                    return False

                # Decrypt the content
                fernet = Fernet(self._get_encryption_key())
                try:
                    decrypted = fernet.decrypt(content)
                    data = json.loads(decrypted.decode())
                    logger.debug("Loaded encrypted credentials")
                except InvalidToken:
                    logger.exception(
                        "Failed to decrypt credentials - encryption key may have changed "
                        "(hostname or machine-id changed). Delete credentials file to re-register."
                    )
                    return False

            # Parse credentials
            self.agent_id = UUID(data["agent_id"])
            self.api_key = data.get("api_key")  # Optional - may not be set yet
            self.recovery_secret = data.get("recovery_secret")  # Optional (legacy)

            logger.info(
                "Loaded agent credentials",
                extra={"agent_id": str(self.agent_id)},
            )
            return True

        # swallowed-exceptions: credential load/save degrades rather than aborting startup; the agent re-registers when it cannot read what it stored
        except Exception:
            logger.exception(
                "Failed to load credentials",
                extra={"credentials_file": str(self.credentials_file)},
            )
            return False

    def save(
        self,
        agent_id: UUID,
        api_key: str | None = None,
        recovery_secret: str | None = None,
    ) -> bool:
        """
        Save credentials to file (encrypted by default).

        A save for the agent already held keeps its recovery secret when none
        is passed: saving the key after approval must not drop it.

        Args:
            agent_id: Agent UUID from registration
            api_key: Optional API key from approval
            recovery_secret: The recovery secret registration returned

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

            if recovery_secret is None and str(agent_id) == str(self.agent_id):
                recovery_secret = self.recovery_secret
            if recovery_secret:
                data["recovery_secret"] = recovery_secret

            if self._encryption_enabled:
                # Encrypt credentials with Fernet
                fernet = Fernet(self._get_encryption_key())
                plaintext = json.dumps(data).encode()
                encrypted = fernet.encrypt(plaintext)

                # Write encrypted binary data
                self._write_atomically(encrypted)
                logger.debug("Saved encrypted credentials")
            else:
                # Write plaintext JSON (for debugging/testing only)
                self._write_atomically(json.dumps(data, indent=2).encode())
                logger.warning("Saved plaintext credentials (encryption disabled)")

            # Set restrictive file permissions (owner read/write only)
            try:
                self.credentials_file.chmod(0o600)
            # swallowed-exceptions: credential load/save degrades rather than aborting startup; the agent re-registers when it cannot read what it stored
            except Exception:
                logger.warning(
                    "Failed to set 0600 permissions on credentials file",
                    exc_info=True,
                )

            # Update in-memory state
            self.agent_id = agent_id
            self.api_key = api_key
            self.recovery_secret = recovery_secret
            self.unsaved = False

            logger.info(
                "Saved agent credentials",
                extra={"agent_id": str(agent_id)},
            )
            return True

        # swallowed-exceptions: credential load/save degrades rather than aborting startup; the agent re-registers when it cannot read what it stored
        except Exception:
            logger.exception(
                "Failed to save credentials",
                extra={"credentials_file": str(self.credentials_file)},
            )
            return False

    def _write_atomically(self, content: bytes) -> None:
        """Replace the credentials file with `content`, all or nothing.

        Written to a temporary file beside it (owner-only from the start),
        flushed to disk, then renamed over it: a save that fails part way (a
        full disk) leaves the old file whole, with the agent's id and recovery
        secret, instead of an empty one. A symlinked path is written through
        to its target. Where no rename can land (the file is a mount of its
        own, or its directory takes no new files) the file is rewritten in
        place, as it was before saves were atomic.
        """
        target = self.credentials_file.resolve()
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
            )
        except OSError as exc:
            if exc.errno not in _NO_RENAME_ERRNOS:
                raise
            _write_in_place(target, content, exc)
            return
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(content)
                tmp.flush()
                os.fsync(tmp.fileno())
            try:
                os.replace(tmp_path, target)
            except OSError as exc:
                if exc.errno not in _NO_RENAME_ERRNOS:
                    raise
                tmp_path.unlink(missing_ok=True)
                _write_in_place(target, content, exc)
                return
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        _sync_directory(target.parent)

    def hold_unsaved(self, api_key: str) -> None:
        """Use `api_key` though it could not be saved, and save it later
        (`retry_unsaved`): the server has already replaced the old key."""
        self.api_key = api_key
        self.unsaved = True

    def retry_unsaved(self) -> None:
        """Save a key `hold_unsaved` kept in memory only, if there is one."""
        if not self.unsaved or self.agent_id is None or not self.api_key:
            return
        if self.save(self.agent_id, self.api_key):
            self.unsaved = False
            logger.info("Saved the recovered agent key that could not be saved before")

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
            self.recovery_secret = None
            self.unsaved = False

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

        # swallowed-exceptions: credential load/save degrades rather than aborting startup; the agent re-registers when it cannot read what it stored
        except Exception:
            logger.exception("Failed to clear credentials")
            return False
