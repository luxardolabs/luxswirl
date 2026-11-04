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
from app.agent.http_client import (
    is_marked_refusal,
    refuses_recovery_secret,
    refuses_registration_key,
    server_client,
)
from app.shared.logger import get_logger

logger = get_logger("luxswirl.agent.registration")


# Seconds to wait after a 429 that names no usable Retry-After.
_DEFAULT_RETRY_AFTER = 60.0


# How soon a recovery may follow the one before it: a key recovered again
# within this many seconds of the last was taken by another copy of this agent
# (a cloned host holding the same credentials), and each recovery regenerates
# the key the other copy holds. The wait doubles each time, from the first
# to the last value, so two copies settle into one recovery an hour.
_RECOVERY_REPEAT_WINDOW = 3600.0
_MIN_RECOVERY_GAP = 60.0

# How long a failed re-registration (after a revoke) waits before the next.
_REGISTER_AGAIN_WAIT = 300.0


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
        # A recovery whose registration key the server refused (its marked
        # refusal) cannot succeed until the agent is reconfigured and
        # restarted; one it rate-limited (429) waits out its Retry-After, and
        # one refused unmarked (not the server's) waits a minute. Either way the agent stops asking every heartbeat, so it
        # never spends the recover-key budget its address shares with others.
        self._recovery_refused = False
        self._recovery_not_before = 0.0
        # When the last recovery succeeded (monotonic), and the wait it set
        # before the next: see _RECOVERY_REPEAT_WINDOW.
        self._last_recovered: float | None = None
        self._recovery_gap = 0.0
        # Set when recover-key, asked after a heartbeat refused the secret,
        # was refused by the server itself; and when registering again may
        # next be tried after a failed attempt.
        self._secret_refused = False
        self._register_again_not_before = 0.0
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

    def _hold_back_repeated_recovery(self) -> None:
        """Hold the next recovery back when this one followed another soon.

        A key recovered again within `_RECOVERY_REPEAT_WINDOW` of the last was
        most likely taken by another copy of this agent; each recovery
        replaces the other copy's key, so without a wait the two would trade
        it at every heartbeat. The wait starts at `_MIN_RECOVERY_GAP` and
        doubles up to the window.
        """
        now = time.monotonic()
        if (
            self._last_recovered is not None
            and now - self._last_recovered < _RECOVERY_REPEAT_WINDOW
        ):
            self._recovery_gap = min(
                max(self._recovery_gap * 2, _MIN_RECOVERY_GAP),
                _RECOVERY_REPEAT_WINDOW,
            )
            self._recovery_not_before = now + self._recovery_gap
            self.logger.warning(
                "Agent key recovered again soon after the last recovery: another "
                "copy of this agent (a cloned host with the same credentials) "
                "may be using its key. Waiting before recovering again",
                extra={"wait_seconds": self._recovery_gap},
            )
        else:
            self._recovery_gap = 0.0
        self._last_recovered = now

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
            # The server has created the agent already, so one it cannot save
            # is used all the same: registering again would create another
            # pending agent for this one install at every try.
            if self.credentials.save(agent_id, recovery_secret=recovery_secret):
                self.logger.info(
                    "Saved agent credentials",
                    extra={"agent_id": str(agent_id)},
                )
            else:
                self.credentials.hold_unsaved_registration(agent_id, recovery_secret)
                self.logger.error(
                    "Failed to save the new agent's credentials: using them, "
                    "and saving them at each heartbeat until the save succeeds",
                    extra={"agent_id": str(agent_id)},
                )
            self.config["agent_id"] = agent_id
            # A new agent and secret: a refusal of the old ones holds
            # nothing back from these.
            self._recovery_refused = False
            self._recovery_not_before = 0.0
            self._last_recovered = None
            self._recovery_gap = 0.0
            return True

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

    async def register_again_after_revoke(self) -> bool:
        """Answer a heartbeat that refused the recovery secret as well as the key.

        A Revoke voids the secret, so this install can never recover a key.
        The agent asks recover-key once to be sure; refused there by the
        server itself (its marked refusal of the secret) it registers again as
        a new pending agent, for an operator to approve. The old credentials
        are replaced only once the server has taken the new registration: a
        registration that fails (no active registration key) keeps them and
        is tried again after
        `_REGISTER_AGAIN_WAIT`, without asking recover-key again, never at
        every heartbeat. The heartbeat calls this at each beat until it
        returns True or the server accepts a credential again, so the retry
        does not wait for another 401 (past the pending budget the server
        answers 429).

        Returns:
            True if the agent registered again
        """
        if time.monotonic() < self._register_again_not_before:
            return False
        if not self._secret_refused:
            if await self.recover_agent_key():
                return False
            if not self._secret_refused:
                return False
        self.logger.warning(
            "The server refuses this agent's recovery secret (its key was "
            "revoked): registering again; approve the new agent"
        )
        if not await self.register_with_server():
            self._register_again_not_before = time.monotonic() + _REGISTER_AGAIN_WAIT
            return False
        self._secret_refused = False
        # The new agent proves itself with the registration key and its new
        # secret until it is approved and recovers its own key.
        self.config["auth_key"] = self.registration_key()
        return True

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

        A 401 the server marks as refusing the recovery secret (a revoke
        voided it) is left to `register_again_after_revoke`.

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

            self._hold_back_repeated_recovery()
            # Save agent-specific key to credentials file. The server has
            # already replaced the old key with this one, so it is used even
            # when it cannot be saved: dropping it would only ask again, and
            # replace it again, at every heartbeat.
            if self.credentials.save(agent_id, api_key):
                self.logger.info("Agent-specific key saved and activated")
            else:
                self.credentials.hold_unsaved(api_key)
                self.logger.error(
                    "Failed to save agent-specific key: using it, and saving "
                    "it at each heartbeat until the save succeeds"
                )
            # Update config to use agent-specific key for future requests
            self.config["auth_key"] = api_key
            return True

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code in (401, 403) and not is_marked_refusal(e.response):
                # Not the server's refusal (a proxy's, a gateway's): temporary,
                # asked again later.
                self._recovery_not_before = time.monotonic() + _DEFAULT_RETRY_AFTER
                self.logger.warning(
                    "Key recovery got a refusal the server did not mark: "
                    "asking again later",
                    extra={"status_code": status_code},
                )
                return False
            if refuses_recovery_secret(e.response):
                self._secret_refused = True
                self.logger.warning(
                    "Key recovery refused: the recovery secret is void",
                    extra={"status_code": status_code},
                )
                return False
            if refuses_registration_key(e.response):
                self._recovery_refused = True
                self.logger.exception(
                    "Key recovery refused: the server does not accept this "
                    "registration key. Set LUXSWIRL_AUTH_KEY to an active "
                    "registration key and restart the agent; it will not ask "
                    "again until then",
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
