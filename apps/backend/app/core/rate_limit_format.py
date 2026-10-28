"""What a rate-limit string must be before slowapi may count with it.

slowapi parses a route's limit on every request it checks, so a limit it cannot
parse, or one its storage cannot hold, fails every request to that route. Both
the environment's limits and the ones set in Settings pass through here.
"""

from limits import parse_many

# Longer is never a limit anyone means, and a huge period overflows the storage.
MAX_LENGTH = 100
MAX_PERIOD_SECONDS = 366 * 24 * 60 * 60


def check_rate_limit(value: str) -> str:
    """`value`, once it reads as limits of at least one request per period of at most a year.

    Raises:
        ValueError: `value` is not such a limit.
    """
    if len(value) > MAX_LENGTH:
        raise ValueError(f"a rate limit is at most {MAX_LENGTH} characters")
    try:
        items = parse_many(value)
    except ValueError:
        raise ValueError(
            f"{value!r} is not a rate limit: use count/period, such as 10/15minutes"
        ) from None
    if not items or any(item.amount < 1 for item in items):
        raise ValueError(f"{value!r} must allow at least one request per period")
    if any(item.get_expiry() > MAX_PERIOD_SECONDS for item in items):
        raise ValueError(f"{value!r} has a period longer than a year")
    return value
