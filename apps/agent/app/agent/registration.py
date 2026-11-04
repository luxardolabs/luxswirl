"""
Agent registration and authentication management.

Handles agent registration with the server and API key recovery.
"""

import math
import os
import time
from typing import Any
from uuid import UUID

import httpx

from app.agent import server_api
from app.agent.credentials import AgentCredentials
from app.agent.http_client import server_client
from app.shared.logger import get_logger

logger = get_logger("luxswirl.agent.registration")


# Seconds to wait after a 429 that names no usable Retry-After.
_DEFAULT_RETRY_AFTER = 60.0


# The longest a 429 can hold recovery back: a value past it (or one that is
# not a finite number) comes from something other than this server, and must
# not strand the agent until it restarts.
_MAX_RETRY_AFTER = 3600.0


def _retry_after(response: httpx.Response) -> float:
    """The seconds a 429 asks the agent to wait: its Retry-After in seconds,
    between 1 and an hour, or a minute when it names no usable number."""
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return _DEFAULT_RETRY_AFTER
    if not math.isfinite(seconds):
        return _DEFAULT_RETRY_AFTER
    return min(max(1.0, seconds), _MAX_RETRY_AFTER)


class AgentRegistration:
    """Handles agent registration and key management."""

    def __init__(self, config: dict[str, Any], credentials: AgentCredentials):
        """
        Initialize registration manager.

        Args:
            config: Agent configuration
            credentials: Agent credentials manager
        """
        self.config = config
        self.credentials = credentials
        self.logger = logger
        # A recovery the server refused (401) cannot succeed until the agent is
        # reconfigured and restarted; one it rate-limited (429) waits out its
        # Retry-After. Either way the agent stops asking every heartbeat, so it
        # never spends the recover-key budget its address shares with others.
        self._recovery_refused = False
        self._recovery_not_before = 0.0
        self.hostname: str | None = None  # Will be set by caller

    def registration_key(self) -> str | None:
        """The registration key: `registration_key`, kept apart by the agent
        because `auth_key` becomes its own key once it has one, else
        `auth_key` (a config that never held an agent key)."""
        if "registration_key" in self.config:
            key: str | None = self.config["registration_key"]
            return key
        auth_key: str | None = self.config.get("auth_key")
        return auth_key

    async def register_with_server(self) -> bool:
        """Register this agent with the server and save credentials.

        Uses registration key (LUXSWIRL_AUTH_KEY) to register. After approval, agent will
        retrieve its agent-specific key via the recovery endpoint.

        Returns:
            True if registration successful, False otherwise
        """
        auth_key = self.registration_key()
        register_url = server_api.register_url(self.config)

        # Build registration payload
        payload = {
            "hostname": self.hostname,
            "ip_address": self.config.get("ip_address"),
            "version": os.getenv("APP_VERSION", "dev"),
            "tags": self.config.get("tags", []),
        }

        headers = {}
        if auth_key:
            headers["Authorization"] = f"Bearer {auth_key}"

        try:
            client = server_client()
            self.logger.info(
                "Registering with server (using registration key)",
                extra={"register_url": register_url},
            )
            response = await client.post(
                register_url, json=payload, headers=headers, timeout=10.0
            )
            response.raise_for_status()

            data = response.json()
            agent_id = UUID(data["agent_id"])
            recovery_secret = data.get("recovery_secret")
            status = data.get("status")
            message = data.get("message")

            self.logger.info(
                "Registration successful",
                extra={"server_message": message, "status": status},
            )

            # Save credentials (without agent-specific key yet - will get it
            # after approval) and the recovery secret that key recovery needs.
            if self.credentials.save(agent_id, recovery_secret=recovery_secret):
                self.config["agent_id"] = agent_id
                # A new agent and secret: a refusal of the old ones holds
                # nothing back from these.
                self._recovery_refused = False
                self._recovery_not_before = 0.0
                self.logger.info(
                    "Saved agent credentials",
                    extra={"agent_id": str(agent_id)},
                )
                return True
            else:
                self.logger.error("Failed to save credentials")
                return False

        except httpx.HTTPStatusError as e:
            self.logger.exception(
                "Registration failed",
                extra={
                    "status_code": e.response.status_code,
                    "response_text": e.response.text,
                },
            )
            return False
        # swallowed-exceptions: registration returns failure so the caller retries with backoff
        except Exception:
            self.logger.exception("Registration failed")
            return False

    async def ensure_recoverable(self) -> bool:
        """Re-register credentials that could never be completed.

        Credentials with no agent key yet must recover one after approval, and
        that needs the recovery secret. Without one (saved before it existed)
        the agent registers afresh now, so the operator approves an agent that
        can finish. Credentials that hold their key are left alone.

        Returns:
            False only if a needed re-registration failed
        """
        if self.credentials.api_key or self.credentials.recovery_secret:
            return True
        self.logger.warning(
            "Stored credentials have neither an agent key nor a recovery secret: "
            "registering again; approve the new agent"
        )
        return await self._register_afresh()

    async def _register_afresh(self) -> bool:
        """Drop the stored credentials and register as a new agent."""
        self.credentials.clear()
        self.config.pop("agent_id", None)
        return await self.register_with_server()

    async def recover_agent_key(self) -> bool:
        """
        Recover/retrieve agent-specific API key from server.

        Called after agent approval to get unique agent-specific key.
        Authenticates with the registration key AND this agent's recovery
        secret (header X-Agent-Recovery-Secret). Credentials saved before the
        secret existed cannot recover: the agent registers afresh instead, and
        an operator approves the new registration.

        Returns:
            True if key retrieved and saved successfully
        """
        if self._recovery_refused or time.monotonic() < self._recovery_not_before:
            return False
        recovery_secret = self.credentials.recovery_secret
        if not recovery_secret:
            self.logger.warning(
                "No recovery secret stored (registered before recovery secrets "
                "existed): registering again; approve the new agent"
            )
            await self._register_afresh()
            return False

        agent_id = self.config.get("agent_id")
        registration_key = self.registration_key()

        if not agent_id:
            self.logger.error("Cannot recover key: no agent_id")
            return False

        if not registration_key:
            self.logger.error("Cannot recover key: no registration key")
            return False

        recovery_url = server_api.recover_key_url(self.config, agent_id)

        headers = {
            "Authorization": f"Bearer {registration_key}",
            "X-Agent-Recovery-Secret": recovery_secret,
        }

        try:
            client = server_client()
            self.logger.info(
                "Recovering agent-specific key",
                extra={"recovery_url": recovery_url},
            )
            response = await client.post(recovery_url, headers=headers, timeout=10.0)
            response.raise_for_status()

            data = response.json()
            api_key = data.get("api_key")
            message = data.get("message", "")

            if not api_key:
                self.logger.error("Recovery response missing api_key")
                return False

            self.logger.info(
                "Successfully retrieved agent-specific key",
                extra={"server_message": message},
            )

            # Save agent-specific key to credentials file
            if self.credentials.save(agent_id, api_key):
                # Update config to use agent-specific key for future requests
                self.config["auth_key"] = api_key
                self.logger.info("Agent-specific key saved and activated")
                return True
            else:
                self.logger.error("Failed to save agent-specific key")
                return False

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code == 401:
                self._recovery_refused = True
                self.logger.exception(
                    "Key recovery refused: the server accepts neither this "
                    "registration key nor this recovery secret. Set "
                    "LUXSWIRL_AUTH_KEY to an active registration key and restart "
                    "the agent; it will not ask again until then",
                    extra={"status_code": status_code},
                )
                return False
            if status_code == 429:
                self._recovery_not_before = time.monotonic() + _retry_after(e.response)
            self.logger.exception(
                "Key recovery failed",
                extra={
                    "status_code": status_code,
                    "response_text": e.response.text,
                },
            )
            return False
        # swallowed-exceptions: registration returns failure so the caller retries with backoff
        except Exception:
            self.logger.exception("Key recovery failed")
            return False
