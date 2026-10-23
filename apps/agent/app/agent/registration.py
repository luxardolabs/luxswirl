"""
Agent registration and authentication management.

Handles agent registration with the server and API key recovery.
"""

import os
from typing import Any
from uuid import UUID

import httpx

from app.agent.credentials import AgentCredentials
from app.agent.http_client import server_client
from app.shared.logger import get_logger
from app.shared.url_security import validate_server_url

logger = get_logger("luxswirl.agent.registration")


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
        self.hostname: str | None = None  # Will be set by caller

    async def register_with_server(self) -> bool:
        """Register this agent with the server and save credentials.

        Uses registration key (LUXSWIRL_AUTH_KEY) to register. After approval, agent will
        retrieve its agent-specific key via the recovery endpoint.

        Returns:
            True if registration successful, False otherwise
        """
        push_url = self.config.get("push_url", "http://localhost:9000")

        # Validate URL security (enforce HTTPS for external servers)
        try:
            validate_server_url(push_url)
        except ValueError:
            self.logger.exception("URL validation failed")
            return False

        auth_key = self.config.get("auth_key")

        # Extract base URL
        if "/api/v1" in push_url:
            base_url = push_url.split("/api/v1")[0]
        else:
            base_url = push_url.rstrip("/")

        register_url = f"{base_url}/api/v1/agents/register"

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
        recovery_secret = self.credentials.recovery_secret
        if not recovery_secret:
            self.logger.warning(
                "No recovery secret stored (registered before recovery secrets "
                "existed): registering again; approve the new agent"
            )
            await self._register_afresh()
            return False

        push_url = self.config.get("push_url", "http://localhost:9000")
        agent_id = self.config.get("agent_id")
        registration_key = self.config.get(
            "auth_key"
        )  # This should be the registration key

        if not agent_id:
            self.logger.error("Cannot recover key: no agent_id")
            return False

        if not registration_key:
            self.logger.error("Cannot recover key: no registration key")
            return False

        # Extract base URL
        if "/api/v1" in push_url:
            base_url = push_url.split("/api/v1")[0]
        else:
            base_url = push_url.rstrip("/")

        recovery_url = f"{base_url}/api/v1/agents/{agent_id}/recover-key"

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
            self.logger.exception(
                "Key recovery failed",
                extra={
                    "status_code": e.response.status_code,
                    "response_text": e.response.text,
                },
            )
            return False
        # swallowed-exceptions: registration returns failure so the caller retries with backoff
        except Exception:
            self.logger.exception("Key recovery failed")
            return False
