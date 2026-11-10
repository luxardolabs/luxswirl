"""Where a check's blocking network calls run: a thread pool of their own, each bounded.

A name lookup (`socket.getaddrinfo`) blocks its thread for as long as the resolver
takes, which for a site whose DNS is failing is many seconds. On the event loop it
would stop every task, heartbeat and reporter included; on the loop's default
executor it would queue the agent's own connection to its server behind a slow
site's lookups. So every check runs its lookups (and the certificate probe, which
resolves and connects with blocking sockets) here, on threads only checks use, and
waits for one at most its `seconds`. A call already running with the same function
and arguments is shared by every caller that asks meanwhile, so a slow site holds
one thread per kind of lookup, not one per check.

Stated limit: past `LOOKUP_WORKERS` distinct slow calls at once, a further call
queues, and its wait counts toward its `seconds`.
"""

import asyncio
import contextvars
import threading
from collections.abc import Callable, Hashable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

# Threads for checks' blocking calls. A thread runs on after its caller stops waiting
# (a thread cannot be cancelled), so there are enough for many slow sites at once;
# each is one pid of the agent container's `pids_limit`.
LOOKUP_WORKERS = 128

# How long a check waits for one lookup.
LOOKUP_SECONDS = 10.0

TARGET_LOOKUPS = ThreadPoolExecutor(
    max_workers=LOOKUP_WORKERS, thread_name_prefix="target-lookup"
)

# The calls running now, by function and arguments. Read and written from the
# loop and from pool threads, so under the lock.
_in_flight: dict[tuple[Any, ...], Future[Any]] = {}
_in_flight_lock = threading.Lock()


class LookupTimeoutError(OSError):
    """A blocking call on the lookup pool did not finish within its time."""


def _shared_call(fn: Callable[..., Any], args: tuple[Hashable, ...]) -> Future[Any]:
    """The pool's future for `fn(*args)`: the one running, else a new one."""
    key = (fn, args)
    with _in_flight_lock:
        running = _in_flight.get(key)
        if running is not None:
            return running
        call = TARGET_LOOKUPS.submit(contextvars.copy_context().run, fn, *args)
        _in_flight[key] = call

    def _forget(done: Future[Any]) -> None:
        # In the pool's thread: no event loop is needed, so a call that outlives
        # the loop that asked for it is still forgotten.
        with _in_flight_lock:
            if _in_flight.get(key) is done:
                del _in_flight[key]

    call.add_done_callback(_forget)
    return call


async def run_blocking[T](
    fn: Callable[..., T], *args: Hashable, seconds: float | None = None
) -> T:
    """`fn(*args)` on the lookup pool, waited for at most `seconds`
    (`LOOKUP_SECONDS` when none is given).

    Each caller waits on a future of its own, fed from the shared call, so a caller
    that stops waiting (its time is up, or its check is cancelled) leaves the call
    and every other caller as they were.

    Raises:
        LookupTimeoutError: It did not finish in time (it runs on, and a caller
            asking for the same call meanwhile waits on it).
        Exception: Whatever `fn` raises.
    """
    wait = LOOKUP_SECONDS if seconds is None else seconds
    loop = asyncio.get_running_loop()
    mine: asyncio.Future[T] = loop.create_future()

    def _settle(call: Future[Any]) -> None:
        if mine.done():
            return
        error = call.exception()
        if error is not None:
            mine.set_exception(error)
        else:
            mine.set_result(call.result())

    def _hand_over(call: Future[Any]) -> None:
        # In the pool's thread, or here when the call had already finished.
        try:
            loop.call_soon_threadsafe(_settle, call)
        # swallowed-exceptions: the loop has closed, so no caller is left to tell.
        except RuntimeError:
            return

    _shared_call(fn, args).add_done_callback(_hand_over)
    try:
        return await asyncio.wait_for(mine, wait)
    except TimeoutError:
        raise LookupTimeoutError(f"the name lookup took over {wait:g} s") from None
