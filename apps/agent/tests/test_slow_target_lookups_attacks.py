"""Ways the checks' lookup pool (`checks._lookups`) could go wrong, each held to.

An HTTPS check's certificate probe never fails a check whose request answered in
time; the http transport's lookups are shared and bounded like every other, so a
slow domain cannot starve a healthy check; tcp names a lookup timeout as one; an
address resolved under one SSRF policy is judged again under the policy in force
when it returns; and a lookup a caller stopped waiting for logs nothing and leaves
nothing behind, even past the loop that asked for it.
"""

from __future__ import annotations

import asyncio
import gc
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from app.agent import http_client
from app.agent.check_executor import CheckExecutor
from app.checks import _lookups, _ssrf_policy, http, ping
from app.checks.dns import DNSCheck
from app.checks.http import HTTPCheck
from app.checks.ping import PingCheck
from app.checks.tcp import TCPCheck
from app.shared.ssrf import SsrfBlockedError

pytestmark = pytest.mark.unit

SLOW = "slow.test"


def _executor() -> CheckExecutor:
    return CheckExecutor(
        config={"agent_id": "a1"},
        metrics=defaultdict(int),
        check_stats=defaultdict(dict),
        semaphore=asyncio.Semaphore(10),
        result_queue=asyncio.Queue(),
        last_state={},
        run_id="r1",
    )


def _cfg(check_type: str, target: str, **kw: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "check_id": str(uuid4()),
        "name": f"{check_type}-adv",
        "check_type": check_type,
        "target": target,
        "retries": 1,
    }
    cfg.update(kw)
    return cfg


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """slow.test: sleeps `slow` s then fails; intranet.test: sleeps `slow` s then 10.0.0.5."""
    real = socket.getaddrinfo
    state: dict[str, Any] = {"slow": 1.5, "asked": []}
    lock = threading.Lock()

    def fake(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else host
        if name == SLOW:
            with lock:
                state["asked"].append(name)
            time.sleep(state["slow"])
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure")
        if name == "intranet.test":
            time.sleep(state["slow"])
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return state


# --- An HTTPS check's certificate probe is enrichment, within the check's time ---


def _https_check(
    monkeypatch: pytest.MonkeyPatch, request_s: float, probe_s: float
) -> HTTPCheck:
    target = "https://site.test/"

    async def send(*_a: Any, **_k: Any) -> httpx.Response:
        await asyncio.sleep(request_s)  # the request itself answers in time
        return httpx.Response(200, request=httpx.Request("GET", target))

    def probe(
        hostname: str,
        port: int,
        addresses: tuple[str, ...],
        timeout: float,
        deadline: float | None = None,
    ) -> dict[str, Any] | None:
        time.sleep(probe_s)  # fresh TCP + TLS on its own socket
        return {"expiration_date": "Nov 10 22:56:57 2030 GMT"}

    async def resolved(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(http, "ssrf_guarded_send", send)
    monkeypatch.setattr(http, "lookup_allowed_addresses", resolved)
    monkeypatch.setattr(HTTPCheck, "_read_certificate", staticmethod(probe))
    return HTTPCheck(_cfg("http", target, timeout=1.0))


async def test_a_slow_certificate_probe_never_fails_an_answered_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = _https_check(monkeypatch, request_s=0.6, probe_s=0.6)
    result, _ = await _executor()._run(check, timeout=1.0, longest=0)
    # The site answered 200 in 0.6 s: certificate details are enrichment.
    assert result["success"] is True, result


async def test_a_fast_certificate_probe_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = _https_check(monkeypatch, request_s=0.6, probe_s=0.05)
    result, _ = await _executor()._run(check, timeout=1.0, longest=0)
    assert result["success"] is True, result
    assert result["metrics"]["response"]["ssl_certificate"], result


# --- A slow domain's http lookups are shared, so they starve no other check -----


async def _healthy_lookup_while_http_load(
    fake_dns: dict[str, Any], monkeypatch: pytest.MonkeyPatch, n_http: int
) -> Any:
    monkeypatch.setattr(_lookups, "LOOKUP_SECONDS", 0.5)
    monkeypatch.setattr(http_client, "_check_clients", {})
    checks = [
        HTTPCheck(_cfg("http", f"http://{SLOW}/", timeout=5)) for _ in range(n_http)
    ]
    running = [asyncio.create_task(c.run()) for c in checks]
    await asyncio.sleep(0.2)
    try:
        return await PingCheck(_cfg("ping", "localhost")).run()
    finally:
        await asyncio.gather(*running, return_exceptions=True)
        for client in http_client._check_clients.values():
            await client.aclose()


async def test_http_checks_on_a_slow_domain_leave_a_healthy_ping_resolving(
    fake_dns: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _healthy_lookup_while_http_load(
        fake_dns, monkeypatch, _lookups.LOOKUP_WORKERS
    )
    assert "took over" not in (result.get("error") or ""), result


async def test_a_healthy_ping_resolves_without_http_load(
    fake_dns: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _healthy_lookup_while_http_load(fake_dns, monkeypatch, 0)
    assert "took over" not in (result.get("error") or ""), result


# --- A lookup timeout is named as one, by tcp and ping alike --------------------


async def test_a_tcp_lookup_timeout_is_named_and_not_retried(
    fake_dns: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_lookups, "LOOKUP_SECONDS", 0.2)
    fake_dns["slow"] = 1.0
    check = TCPCheck(_cfg("tcp", SLOW, port=443, timeout=5, retries=3))
    begun = time.monotonic()
    result, _ = await _executor()._run(check, timeout=5, longest=0)
    assert time.monotonic() - begun < 0.5
    assert result["error"] == (
        f"Could not resolve host: {SLOW} (the name lookup took over 0.2 s)"
    )


async def test_a_ping_lookup_timeout_is_named(
    fake_dns: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_lookups, "LOOKUP_SECONDS", 0.2)
    fake_dns["slow"] = 1.0
    result, _ = await _executor()._run(
        PingCheck(_cfg("ping", SLOW)), timeout=5, longest=0
    )
    assert (
        result["error"]
        == f"Could not resolve host: {SLOW} (the name lookup took over 0.2 s)"
    )


# --- A policy turned on while a ping's lookup runs holds for its answer ---------


async def test_a_policy_turned_on_mid_lookup_refuses_the_private_answer(
    fake_dns: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_dns["slow"] = 0.5
    monkeypatch.setattr(_ssrf_policy, "_policy", {"block_private_networks": False})
    pinged: list[str] = []

    async def fake_ping(*args: Any, **_k: Any) -> tuple[int, bytes, bytes]:
        pinged.extend(a for a in args if a == "10.0.0.5")
        return 0, b"", b""

    monkeypatch.setattr(ping, "run_subprocess_safely", fake_ping)
    task = asyncio.create_task(PingCheck(_cfg("ping", "intranet.test")).run())
    await asyncio.sleep(0.1)
    _ssrf_policy.apply_server_policy({"block_private_networks": True})
    with pytest.raises(SsrfBlockedError):
        await task
    assert pinged == [], "pinged a private address after the policy blocked it"


async def test_a_ping_under_the_policy_is_refused(
    fake_dns: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_dns["slow"] = 0.05
    monkeypatch.setattr(_ssrf_policy, "_policy", {"block_private_networks": True})
    with pytest.raises(SsrfBlockedError):
        await PingCheck(_cfg("ping", "intranet.test")).run()


# --- A lookup outliving its loop leaves no entry and no noise -------------------

_EXIT_SCRIPT = textwrap.dedent(
    """
    import asyncio, sys, time
    from app.checks import _lookups

    SLEEP = float(sys.argv[1])

    def slow(x):
        time.sleep(SLEEP)
        raise OSError("resolver gave up")

    async def main():
        try:
            await _lookups.run_blocking(slow, "h", seconds=0.1)
        except OSError:
            pass

    begun = time.monotonic()
    asyncio.run(main())
    print("RUN_RETURNED", round(time.monotonic() - begun, 2), flush=True)
    time.sleep(SLEEP + 0.5)
    print("IN_FLIGHT_AFTER", len(_lookups._in_flight), flush=True)
    """
)


def _exit_probe(sleep: float) -> tuple[float, str, str]:
    agent_root = Path(__file__).resolve().parents[1]
    begun = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", _EXIT_SCRIPT, str(sleep)],
        cwd=agent_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return time.monotonic() - begun, proc.stdout, proc.stderr


def test_a_lookup_outliving_its_loop_is_forgotten() -> None:
    took, out, err = _exit_probe(2.0)
    assert err == "", err
    assert "IN_FLIGHT_AFTER 0" in out, out


def test_a_lookup_finishing_inside_its_loop_is_forgotten() -> None:
    took, out, err = _exit_probe(0.01)
    assert "IN_FLIGHT_AFTER 0" in out, out


# --- A caller that stops waiting leaves no unretrieved error --------------------


async def test_a_cancelled_caller_then_a_failing_lookup_logs_nothing() -> None:
    loop = asyncio.get_running_loop()
    seen: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _l, ctx: seen.append(ctx))

    def slow(x: str) -> None:
        time.sleep(0.5)
        raise OSError("late failure")

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_lookups.run_blocking(slow, "c", seconds=5), 0.1)
    await asyncio.sleep(0.8)
    gc.collect()
    await asyncio.sleep(0)
    assert seen == [], seen
    assert not _lookups._in_flight


# --- Config errors and SSRF refusals read as before ------------------------------


async def test_ping_null_host_is_still_a_config_error() -> None:
    result, _ = await _executor()._run(
        PingCheck(_cfg("ping", "a\x00b")), timeout=5, longest=0
    )
    assert result["error"].startswith("Invalid configuration: Invalid host name"), (
        result
    )


async def test_tcp_unresolvable_is_still_could_not_resolve(
    fake_dns: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_dns["slow"] = 0.01
    result, _ = await _executor()._run(
        TCPCheck(_cfg("tcp", SLOW, port=443, timeout=5)), timeout=5, longest=0
    )
    assert result["error"] == f"Could not resolve host: {SLOW}", result


async def test_dns_nameserver_in_metadata_range_still_refused() -> None:
    check = DNSCheck(
        _cfg("dns", "example.com", record_type="A", nameserver="169.254.169.254")
    )
    result, _ = await _executor()._run(check, timeout=5, longest=0)
    assert "blocked" in result["error"], result


async def test_a_timed_out_then_failing_lookup_logs_nothing() -> None:
    """run_blocking's own bound: the exact slow-DNS case (lookup times out, then fails)."""
    loop = asyncio.get_running_loop()
    seen: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _l, ctx: seen.append(ctx))

    def slow(x: str) -> None:
        time.sleep(0.5)
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure")

    with pytest.raises(_lookups.LookupTimeoutError):
        await _lookups.run_blocking(slow, "own", seconds=0.1)
    await asyncio.sleep(0.8)
    assert seen == [], seen


async def test_a_lookup_failing_in_time_logs_nothing() -> None:
    loop = asyncio.get_running_loop()
    seen: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _l, ctx: seen.append(ctx))

    def quick(x: str) -> None:
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure")

    with pytest.raises(socket.gaierror):
        await _lookups.run_blocking(quick, "quick", seconds=1)
    await asyncio.sleep(0.2)
    assert seen == [], seen
