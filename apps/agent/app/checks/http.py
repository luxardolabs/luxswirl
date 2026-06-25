"""
HTTP Check Module - Implements HTTP/HTTPS health checks.
"""

import re
import socket
import ssl
import warnings
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.utils import CryptographyDeprecationWarning

from app.checks._ssrf_http import ssrf_guarded_send
from app.checks.base import BaseCheck


class HTTPCheck(BaseCheck):
    """Check for HTTP/HTTPS endpoints."""

    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate HTTP-specific configuration.

        Args:
            config: The check configuration to validate

        Raises:
            ValueError: If required configuration fields are missing or invalid
        """
        super().validate_config(config)

        # Check that target is a valid URL
        target = config.get("target", "")
        if not target.startswith(("http://", "https://")):
            raise ValueError(f"HTTP check target must start with http:// or https://: {target}")

    async def run(self) -> dict[str, Any]:
        """Execute the HTTP health check.

        Returns:
            A dictionary containing the check result
        """
        cfg = self.config
        target = cfg["target"]
        method = cfg.get("method", "GET")
        timeout = cfg.get("timeout", 3)
        retries = cfg.get("retries", 1)
        verify = cfg.get("verify_ssl", True)
        expected_status = cfg.get("expected_status", 200)

        # Optional request parameters
        headers = cfg.get("headers", {})
        body = cfg.get("body")

        # Additional validation options
        validate_options = {
            "status": expected_status,
            "content_regex": cfg.get("content_regex"),
            "header_checks": cfg.get("header_checks", {}),
            "max_response_time": cfg.get("max_response_time"),
        }

        http_response: httpx.Response | None = None
        last_error: Exception | None = None
        response_data: dict[str, Any] = {}

        # Attempt the request with retries
        for _attempt in range(retries):
            try:
                async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
                    start_time = self.start_timer()

                    # SSRF: validate the resolved IP at fetch time — on the initial
                    # URL AND every redirect hop — so a create→fetch DNS rebind or a
                    # 3xx to 169.254.169.254 can't reach the cloud metadata endpoint.
                    http_response = await ssrf_guarded_send(
                        client,
                        method,
                        target,
                        headers=headers,
                        body=body,
                        follow_redirects=cfg.get("follow_redirects", True),
                    )

                    latency = self.stop_timer(start_time)

                    # If we got a response, break out of retry loop
                    break

            except httpx.RequestError as e:
                last_error = e
                continue
            except Exception as e:
                last_error = e
                continue

        # If we have no response after all retries, return failure
        if http_response is None:
            error_message = (
                f"Failed after {retries} attempts: {str(last_error)}"
                if last_error
                else "Unknown error"
            )
            return self.create_result(success=False, latency_ms=None, error=error_message)

        # Collect response data
        response_data = {
            "status_code": http_response.status_code,
            "headers": dict(http_response.headers),
            "latency_ms": latency,
            "response_size": len(http_response.content),
        }

        # Validate response
        validation_result = self._validate_response(http_response, validate_options, latency)

        # Get SSL certificate info for HTTPS targets
        ssl_info = self._get_ssl_certificate_info(target, timeout=timeout)
        if ssl_info:
            response_data["ssl_certificate"] = ssl_info

        # Create and return the result with metrics
        return self.create_result(
            success=validation_result["success"],
            latency_ms=latency,
            error=validation_result.get("error"),
            http_status_code=http_response.status_code,
            http_response_time_ms=latency,
            metrics={"response": response_data},
        )

    def _validate_response(
        self, response: httpx.Response, options: dict[str, Any], latency: float
    ) -> dict[str, Any]:
        """Validate the HTTP response against the expected criteria.

        Args:
            response: The HTTP response object
            options: Validation options dictionary
            latency: The response latency in milliseconds

        Returns:
            A dictionary with success status and optional error message
        """
        result: dict[str, Any] = {"success": True}
        errors: list[str] = []

        # Check status code
        expected_status = options.get("status", 200)
        if isinstance(expected_status, list):
            if response.status_code not in expected_status:
                errors.append(
                    f"Status code {response.status_code} not in expected list: {expected_status}"
                )
        else:
            if response.status_code != expected_status:
                errors.append(f"Status code {response.status_code} != expected {expected_status}")

        # Check response time if max is specified
        max_time = options.get("max_response_time")
        if max_time and latency > max_time:
            errors.append(f"Response time {latency}ms > maximum {max_time}ms")

        # Check for content regex if specified
        content_regex = options.get("content_regex")
        if content_regex:
            content = response.text
            if not re.search(content_regex, content):
                errors.append(f"Content does not match regex: {content_regex}")

        # Check for required headers
        header_checks = options.get("header_checks", {})
        for header, expected_value in header_checks.items():
            if header.lower() not in {h.lower() for h in response.headers}:
                errors.append(f"Required header missing: {header}")
            else:
                actual_value = response.headers.get(header)
                if actual_value != expected_value:
                    errors.append(f"Header {header}: {actual_value} != expected {expected_value}")

        # If we have any errors, the check fails
        if errors:
            result["success"] = False
            result["error"] = "; ".join(errors)

        return result

    def _get_ssl_certificate_info(self, target: str, timeout: float = 5) -> dict[str, Any] | None:
        """
        Extract SSL certificate information from HTTPS target.

        Args:
            target: The HTTPS URL to check
            timeout: Connection timeout in seconds

        Returns:
            Dictionary with cert info or None if not HTTPS or error
        """
        try:
            # Only process HTTPS URLs
            if not target.startswith("https://"):
                return None

            # Parse URL to get hostname and port
            parsed = urlparse(target)
            hostname = parsed.hostname
            port = parsed.port or 443

            if not hostname:
                return None

            # Create SSL context
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE  # Don't verify for info gathering

            # Connect and get certificate
            with socket.create_connection((hostname, port), timeout=timeout) as sock:
                with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                    # Get cert in binary form (works even with CERT_NONE)
                    der_cert = ssock.getpeercert(binary_form=True)

                    if not der_cert:
                        return None

                    # Parse the DER certificate using cryptography library
                    # Suppress warning about NULL parameters in signature (common with IPMI/BMC certs)
                    with warnings.catch_warnings():
                        warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)
                        cert = x509.load_der_x509_certificate(der_cert, default_backend())

                    # Extract expiration dates (use UTC versions)
                    not_after = cert.not_valid_after_utc
                    not_before = cert.not_valid_before_utc

                    # Format dates to match expected format (e.g., "Nov 10 22:56:57 2025 GMT")
                    expiration_date = not_after.strftime("%b %d %H:%M:%S %Y GMT")
                    valid_from = not_before.strftime("%b %d %H:%M:%S %Y GMT")

                    # Extract subject Common Name
                    subject_cn = hostname
                    try:
                        cn_value = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[
                            0
                        ].value
                        subject_cn = str(cn_value) if cn_value is not None else hostname
                    except IndexError, AttributeError:
                        pass

                    # Extract issuer Organization or Common Name
                    issuer_name = "Unknown"
                    try:
                        # Try Organization first
                        org_value = cert.issuer.get_attributes_for_oid(
                            x509.NameOID.ORGANIZATION_NAME
                        )[0].value
                        issuer_name = str(org_value) if org_value is not None else "Unknown"
                    except IndexError, AttributeError:
                        try:
                            # Fall back to Common Name
                            cn_value = cert.issuer.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[
                                0
                            ].value
                            issuer_name = str(cn_value) if cn_value is not None else "Unknown"
                        except IndexError, AttributeError:
                            pass

                    return {
                        "expiration_date": expiration_date,
                        "valid_from": valid_from,
                        "issuer": issuer_name,
                        "subject": subject_cn,
                    }

        except Exception:
            # Don't fail the check if cert extraction fails
            # Just log and return None
            return None
