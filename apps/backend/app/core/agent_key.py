"""The format of an agent's own API key.

A registration key has its own prefix (`RegistrationKeyCoreService.generate_key`), so
a token in this format is never a guess at one.
"""

import secrets

AGENT_KEY_PREFIX = "luxswirl_ak_"


def new_agent_key() -> str:
    """A new agent API key, in plaintext."""
    return f"{AGENT_KEY_PREFIX}{secrets.token_hex(16)}"


def bearer_holds_agent_key(authorization: str | None) -> bool:
    """Whether an Authorization header carries a token in the agent-key format."""
    parts = (authorization or "").split()
    return (
        len(parts) == 2
        and parts[0].lower() == "bearer"
        and parts[1].startswith(AGENT_KEY_PREFIX)
    )
