"""The bulk-import preview: the check each URL would become, held to the SSRF settings."""

import asyncio
import ssl
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.check_target_validator import (
    CheckTargetBlockedError,
    validate_check_target,
)
from app.schemas.setting_schema import NetworkProtection
from app.services.core._check_bulk_names import bulk_display_name, unused_name
from app.services.core.check_core_service import CheckCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.shared.ssrf import SsrfBlockedError, allowed_addresses, extract_hosts
from app.shared.ssrf_http import NO_ADDRESS, VettedTransport

PROBED_URLS = 100
PROBES_AT_ONCE = 20
PROBE_SECONDS = 5
# Each of a name's addresses but its last gets this long, to leave time for the next.
ADDRESS_SECONDS = 1.5
# A deadline stops waiting for a row and not its lookups, which run host by host, so a
# URL naming more (a PostgreSQL host list) is not looked up at all.
HOSTS_PER_URL = 8
INVALID_NAME = "invalid-url"
# A preview's lookups: a deadline cannot stop one waiting on a slow nameserver, so they
# stay out of the default executor, which name lookups and other work share.
_LOOKUPS = ThreadPoolExecutor(
    max_workers=PROBES_AT_ONCE, thread_name_prefix="bulk-preview-lookup"
)


class CheckPreviewCoreService:
    """The preview a bulk import shows before any check is saved."""

    @staticmethod
    async def bulk_preview_checks(
        db: AsyncSession, urls: list[str]
    ) -> list[dict[str, Any]]:
        """Preview bulk check URLs: the check each becomes, held to the SSRF settings.

        Each URL is judged as saving its check would judge it, and an http one is
        then fetched once, on an address those settings allow. The first
        `PROBED_URLS` get this, `PROBES_AT_ONCE` at a time; the
        rest are named only, since one request would otherwise open as many
        connections as the form holds lines.

        Args:
            db: Database session, to read the SSRF settings
            urls: List of URL strings to preview

        Returns:
            List of dicts with url, generated_name, check_type, validation_status, etc.
        """
        protection = await SettingsCoreService.get_network_protection(db)
        turns = asyncio.Semaphore(PROBES_AT_ONCE)
        transport = VettedTransport(
            verify=True,
            allowed=partial(
                allowed_addresses,
                block_cloud_metadata=protection.block_cloud_metadata,
                block_private_networks=protection.block_private_networks,
            ),
            lookups=_LOOKUPS,
            address_seconds=ADDRESS_SECONDS,
        )
        async with httpx.AsyncClient(
            transport=transport, timeout=PROBE_SECONDS
        ) as client:

            async def judged(url: str) -> dict[str, Any]:
                async with turns:
                    return await CheckPreviewCoreService._preview_bulk_url(
                        url, protection, client
                    )

            previews = await asyncio.gather(
                *[judged(url) for url in urls[:PROBED_URLS]],
                *[
                    CheckPreviewCoreService._preview_bulk_url(url, None, None)
                    for url in urls[PROBED_URLS:]
                ],
            )
        taken: set[str] = set()
        numbers: dict[str, int] = {}
        for preview in previews:
            preview["generated_name"] = unused_name(
                preview["generated_name"], taken, numbers
            )
            taken.add(preview["generated_name"])
        return previews

    @staticmethod
    async def _preview_bulk_url(
        url_str: str,
        protection: NetworkProtection | None,
        client: httpx.AsyncClient | None,
    ) -> dict[str, Any]:
        """One bulk-import preview row: the check the URL would become and, given
        the settings and a client, whether they allow it and whether an http target
        answers. A row given neither is named only."""
        try:
            check_type, full_url, parsed = CheckCoreService.classify_bulk_url(url_str)
            display_name = bulk_display_name(parsed)

            validation_status = "unknown"
            validation_message = (
                f"Not checked: a preview checks the first {PROBED_URLS} URLs"
            )
            status_code = None
            if protection and client:
                (
                    validation_status,
                    validation_message,
                    status_code,
                ) = await CheckPreviewCoreService._probe_target(
                    check_type, full_url, protection, client
                )

            return {
                "url": full_url,
                "generated_name": display_name,
                "check_type": check_type,
                "validation_status": validation_status,
                "validation_message": validation_message,
                "status_code": status_code,
            }
        # swallowed-exceptions: per-URL bulk preview — the failure IS the
        # result, returned as an error row the operator sees before importing.
        # The name is fixed: a URL that does not parse is no shape a mask can read.
        except Exception as e:
            return {
                "url": url_str,
                "generated_name": INVALID_NAME,
                "check_type": "http",
                "validation_status": "error",
                "validation_message": f"Parse error: {str(e)[:100]}",
            }

    @staticmethod
    async def _probe_target(
        check_type: str,
        full_url: str,
        protection: NetworkProtection,
        client: httpx.AsyncClient,
    ) -> tuple[str, str, int | None]:
        """A target's (validation_status, validation_message, status_code).

        The target is judged as saving the check judges it, so a row the save would
        refuse says so. An http target is then fetched through `client`, whose
        connections open only on an allowed address, so a name that resolves
        differently the second time is held to the same settings. A redirect is
        reported, not followed. Only the status reaches the row: the deadline covers
        the lookups and the response head, the body is never fetched, and no failure
        quotes what the target sent, which on a port that does not speak HTTP is that
        service's own text.

        Certificates ARE verified here, because the agent's http check defaults
        to verify_ssl=True. A cert problem is reported as its own warning so the
        operator can set verify_ssl=False deliberately.
        """
        if len(extract_hosts(full_url)) > HOSTS_PER_URL:
            return (
                "unknown",
                f"Not checked: this URL names over {HOSTS_PER_URL} hosts",
                None,
            )
        # swallowed-exceptions: every failure here is the probe's answer, shown
        # to the operator as the preview row's status.
        try:
            async with asyncio.timeout(PROBE_SECONDS):
                await asyncio.get_running_loop().run_in_executor(
                    _LOOKUPS,
                    partial(
                        validate_check_target,
                        full_url,
                        block_cloud_metadata=protection.block_cloud_metadata,
                        block_private_networks=protection.block_private_networks,
                    ),
                )
                if check_type != "http":
                    return "unknown", "", None
                async with client.stream("GET", full_url) as response:
                    status_code = response.status_code
        except CheckTargetBlockedError as e:
            return "error", str(e), None
        except SsrfBlockedError as e:
            return (
                "error",
                f"{e} Adjust SSRF protection under Settings > Security to allow it.",
                None,
            )
        except TimeoutError, httpx.TimeoutException:
            return "error", f"Timeout ({PROBE_SECONDS}s)", None
        except httpx.ConnectError as e:
            return CheckPreviewCoreService._connect_failure(e)
        except httpx.ProtocolError:
            return "error", "The target did not answer HTTP", None
        except httpx.InvalidURL, httpx.UnsupportedProtocol, UnicodeError:
            return "error", "The URL could not be parsed", None
        except httpx.TransportError:
            return "error", "Connection failed", None
        except Exception as e:
            return "error", f"The probe failed ({type(e).__name__})", None

        status = "success" if 200 <= status_code < 400 else "warning"
        return status, f"HTTP {status_code}", status_code

    @staticmethod
    def _connect_failure(error: httpx.ConnectError) -> tuple[str, str, None]:
        """A preview row's answer to a failed connection. A TLS failure lies down the
        exception's chain: httpx and httpcore each raise from the one before."""
        cause: BaseException | None = error
        while cause is not None and not isinstance(cause, ssl.SSLError):
            cause = cause.__cause__ or cause.__context__
        if isinstance(cause, ssl.SSLCertVerificationError):
            return (
                "warning",
                "TLS certificate not trusted — set verify_ssl=False "
                "on the check to monitor it anyway",
                None,
            )
        if cause is not None:
            return "error", "TLS handshake failed", None
        if str(error).startswith(NO_ADDRESS):
            return "error", "Could not resolve the host", None
        return "error", "Connection failed", None
