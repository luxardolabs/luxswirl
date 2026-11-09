"""What a re-encryption run did to the stored encrypted values."""

from pydantic import BaseModel, ConfigDict


class ReencryptionReport(BaseModel):
    """The stored encrypted values a re-encryption run judged, by outcome.

    Each value is named, where it is named, as ``table.column id=<row>``.
    """

    model_config = ConfigDict(frozen=True)

    already_readable: int
    """Values the configured key decrypted: left as they were."""
    reencrypted: list[str]
    """Values the other key wrote: re-encrypted under the configured key."""
    encrypted: list[str]
    """Values stored in plaintext: encrypted under the configured key."""
    left_too_long: list[str]
    """Plaintext values left as they were: encrypted, they would not fit their column."""
