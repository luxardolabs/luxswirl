"""
Checking a caller's secret against a stored bcrypt hash.

bcrypt hashes at most 72 bytes and refuses to hash or check a longer secret, so
no stored hash is of one; nor of text UTF-8 cannot encode. Either is therefore a
wrong secret: it is refused as a mismatch before bcrypt sees it, the way any
wrong secret is.
"""

import bcrypt

# The most bytes bcrypt hashes.
MAX_SECRET_BYTES = 72


def bcrypt_matches(secret: str, secret_hash: str) -> bool:
    """
    Whether a secret is the one a bcrypt hash was made from.

    Args:
        secret: The plaintext a caller sent
        secret_hash: The stored bcrypt hash

    Returns:
        True if the secret matches the hash

    Raises:
        ValueError: If the stored hash is not a bcrypt hash
    """
    try:
        secret_bytes = secret.encode("utf-8")
    # No stored hash is of text UTF-8 cannot encode (a lone surrogate).
    except UnicodeEncodeError:
        return False
    if len(secret_bytes) > MAX_SECRET_BYTES:
        return False
    return bcrypt.checkpw(secret_bytes, secret_hash.encode("utf-8"))
