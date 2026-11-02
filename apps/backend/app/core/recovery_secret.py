"""
An agent's recovery secret: its stored form and the check of a presented one.

Registration issues each agent a random secret once and stores only its digest
(`Agent.recovery_secret_hash`). The secret proves which agent is calling: with
the registration key on key recovery, and alone on a pending agent's heartbeat.
"""

import hashlib
import hmac

# Compared against when no digest is on record, so a refusal does the same work.
_NO_SECRET_DIGEST = "0" * 64


def recovery_secret_digest(secret: str) -> str:
    """The stored form of a recovery secret: its SHA-256 hex digest.

    A fast hash is right here: the secret is 256 random bits, not a password,
    so there is nothing for a slow hash to stretch.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def recovery_secret_matches(stored: str | None, presented: str | None) -> bool:
    """Whether `presented` is the secret whose digest is `stored`.

    Always one constant-time comparison, against a digest no secret has when
    nothing is stored, so every refusal takes the same path.
    """
    matches = hmac.compare_digest(
        recovery_secret_digest(presented or "").encode(),
        (stored or _NO_SECRET_DIGEST).encode(),
    )
    return matches and bool(stored) and bool(presented)
