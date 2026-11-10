"""Where a check's blocking network calls run: a thread pool of their own, each bounded.

A name lookup (`socket.getaddrinfo`) blocks its thread for as long as the resolver
takes, which for a site whose DNS is failing is many seconds. On the event loop it
would stop every task, heartbeat and reporter included; on the loop's default
executor it would queue the agent's own connection to its server behind a slow
site's lookups. So every check runs its lookups (and the certificate probe, which
resolves and connects with blocking sockets) here, on threads only checks use, and
waits for one at most its `seconds`. A call already running with the same function
and arguments is shared by every caller that asks meanwhile, so a slow site holds
one thread, not one per check.
"""

import asyncio
import contextvars
from collections.abc import Callable, Hashable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# Threads for checks' blocking calls. A thread runs on after its caller stops waiting
# (a thread cannot be cancelled), so there are enough for many slow sites at once.
LOOKUP_WORKERS = 64

# How long a check waits for one lookup.
LOOKUP_SECONDS = 10.0

TARGET_LOOKUPS = ThreadPoolExecutor(
    max_workers=LOOKUP_WORKERS, thread_name_prefix="target-lookup"
)

_in_flight: dict[tuple[Any, ...], asyncio.Future[Any]] = {}


class LookupTimeoutError(OSError):
    """A blocking call on the lookup pool did not finish within its time."""


async def run_blocking[T](
    fn: Callable[..., T], *args: Hashable, seconds: float | None = None
) -> T:
    """`fn(*args)` on the lookup pool, waited for at most `seconds`
    (`LOOKUP_SECONDS` when none is given).

    Raises:
        LookupTimeoutError: It did not finish in time (it runs on, and a caller
            asking for the same call meanwhile waits on it).
        Exception: Whatever `fn` raises.
    """
    wait = LOOKUP_SECONDS if seconds is None else seconds
    loop = asyncio.get_running_loop()
    key = (loop, fn, args)
    future = _in_flight.get(key)
    if future is None:
        future = loop.run_in_executor(
            TARGET_LOOKUPS, contextvars.copy_context().run, fn, *args
        )
        _in_flight[key] = future

        def _forget(done: asyncio.Future[Any]) -> None:
            if _in_flight.get(key) is done:
                del _in_flight[key]
            # Read here, so a failure no caller waited for is not logged as lost.
            if not done.cancelled():
                done.exception()

        future.add_done_callback(_forget)
    try:
        return await asyncio.wait_for(asyncio.shield(future), wait)
    except TimeoutError:
        raise LookupTimeoutError(f"it took over {wait:g} s") from None
