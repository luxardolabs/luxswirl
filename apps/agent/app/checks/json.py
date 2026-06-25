"""
JSON Check Module - Implements HTTP checks with JSON response validation.

Uses JSONata (jsonata.org) for JSON path queries, providing full Uptime Kuma compatibility.
"""

import json
import re
from typing import Any

import httpx
import jsonata

from app.checks._ssrf_http import ssrf_guarded_send
from app.checks.base import BaseCheck


class JSONPathError(Exception):
    """Exception for JSON path resolution errors."""


class JSONCheck(BaseCheck):
    """Check for HTTP/HTTPS endpoints with JSON response validation."""

    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate JSON-specific configuration.

        Args:
            config: The check configuration to validate

        Raises:
            ValueError: If required configuration fields are missing or invalid
        """
        super().validate_config(config)

        # Check that target is a valid URL
        target = config.get("target", "")
        if not target.startswith(("http://", "https://")):
            raise ValueError(f"JSON check target must start with http:// or https://: {target}")

        # Check for JSON path
        if "json_path" not in config:
            raise ValueError(f"JSON check {config.get('name')} must have a 'json_path'")

        # Check for expected value
        if "expected_value" not in config:
            raise ValueError(f"JSON check {config.get('name')} must have an 'expected_value'")

    async def run(self) -> dict[str, Any]:
        """Execute the JSON health check.

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

        # JSON-specific configuration
        json_path = cfg["json_path"]
        expected_value = cfg["expected_value"]
        comparison_type = cfg.get(
            "comparison_type", "equals"
        )  # equals, contains, regex, gt, lt, etc.

        # Optional request parameters
        headers = cfg.get("headers", {})

        # Set content type to application/json if not specified
        if "content-type" not in {h.lower() for h in headers}:
            headers["Content-Type"] = "application/json"

        # Handle body - convert dict to JSON string if needed
        body = cfg.get("body")
        if isinstance(body, dict):
            body = json.dumps(body)

        http_response: httpx.Response | None = None
        last_error: Exception | None = None
        response_data: dict[str, Any] = {}
        parsed_json: Any | None = None

        # Attempt the request with retries
        for _attempt in range(retries):
            try:
                async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
                    start_time = self.start_timer()

                    # SSRF: validate the resolved IP at fetch time on the initial
                    # URL and every redirect hop (blocks 3xx/rebind to metadata).
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

        # Check status code first
        if http_response.status_code != expected_status:
            return self.create_result(
                success=False,
                latency_ms=latency,
                error=f"Status code {http_response.status_code} != expected {expected_status}",
                response={
                    "status_code": http_response.status_code,
                    "headers": dict(http_response.headers),
                    "latency_ms": latency,
                    "response_size": len(http_response.content),
                },
            )

        # Try to parse response as JSON
        try:
            parsed_json = http_response.json()
        except Exception as e:
            return self.create_result(
                success=False,
                latency_ms=latency,
                error=f"Failed to parse response as JSON: {str(e)}",
                response={
                    "status_code": http_response.status_code,
                    "headers": dict(http_response.headers),
                    "latency_ms": latency,
                    "response_size": len(http_response.content),
                    "response_text": http_response.text[
                        :1000
                    ],  # Include part of the response for debugging
                },
            )

        # Extract value using JSON path
        try:
            actual_value = self._resolve_json_path(parsed_json, json_path)
        except JSONPathError as e:
            return self.create_result(
                success=False,
                latency_ms=latency,
                error=f"JSON path error: {str(e)}",
                response={
                    "status_code": http_response.status_code,
                    "json": parsed_json,
                    "latency_ms": latency,
                },
            )

        # Prepare response data
        response_data = {
            "status_code": http_response.status_code,
            "headers": dict(http_response.headers),
            "latency_ms": latency,
            "json_path": json_path,
            "actual_value": actual_value,
            "expected_value": expected_value,
        }

        # Compare actual value with expected value
        success, error = self._compare_values(actual_value, expected_value, comparison_type)

        # Create and return the result
        return self.create_result(
            success=success,
            latency_ms=latency,
            error=error,
            response=response_data,
        )

    def _resolve_json_path(self, json_data: Any, path: str) -> Any:
        """Resolve a JSONata query expression to a value.

        Uses JSONata (jsonata.org) query language for full Uptime Kuma compatibility.

        Supports:
        - Simple paths: data.users.name
        - Array indexing: data.users[0].name
        - Quoted keys: printers."printer.with.dots".status
        - Wildcards: printers.*.status
        - Filters: printers[status="online"]
        - Functions: $count(printers), $sum(printers.*.page_count)
        - Predicates: printers[page_count > 1000]

        See https://jsonata.org for full query syntax documentation.

        Args:
            json_data: The parsed JSON data
            path: The JSONata query expression

        Returns:
            The value(s) returned by the query

        Raises:
            JSONPathError: If the query cannot be resolved or is invalid
        """
        # Handle empty path - return entire document
        if not path:
            return json_data

        try:
            # Compile and evaluate the JSONata expression
            expr = jsonata.Jsonata(path)
            result = expr.evaluate(json_data)
            return result

        except Exception as e:
            # JSONata exceptions may contain technical details, make them user-friendly
            error_msg = str(e)

            # Check for common errors and provide helpful messages
            if "undefined" in error_msg.lower():
                raise JSONPathError(
                    f"Path '{path}' not found in JSON response. "
                    f"Check that the path exists and is spelled correctly."
                ) from e
            elif "syntax" in error_msg.lower():
                raise JSONPathError(
                    f"Invalid JSONata syntax in path '{path}': {error_msg}. "
                    f"See https://jsonata.org for syntax reference."
                ) from e
            else:
                raise JSONPathError(f"JSONata query error: {error_msg}") from e

    def _compare_values(
        self, actual: Any, expected: Any, comparison_type: str
    ) -> tuple[bool, str | None]:
        """Compare actual and expected values using the specified comparison type.

        Args:
            actual: The actual value from the JSON response
            expected: The expected value from configuration
            comparison_type: Type of comparison to perform

        Returns:
            Tuple of (success, error_message)
        """
        # Convert expected value to appropriate type if needed
        try:
            if isinstance(actual, bool):
                # Handle boolean values
                if isinstance(expected, str):
                    expected = expected.lower() == "true"
            elif isinstance(actual, int):
                # Handle integer values
                expected = int(expected)
            elif isinstance(actual, float):
                # Handle float values
                expected = float(expected)
        except ValueError, TypeError:
            # If conversion fails, use the original expected value
            pass

        # Perform comparison based on comparison_type
        if comparison_type == "equals":
            if actual == expected:
                return True, None
            return False, f"Value '{actual}' does not equal expected '{expected}'"

        elif comparison_type == "contains":
            # Check if actual contains expected
            if isinstance(actual, str) and isinstance(expected, str):
                if expected in actual:
                    return True, None
                return False, f"Value '{actual}' does not contain '{expected}'"
            elif isinstance(actual, (list, tuple)):
                if expected in actual:
                    return True, None
                return False, f"List '{actual}' does not contain '{expected}'"
            return (
                False,
                f"Cannot perform 'contains' comparison on {type(actual).__name__}",
            )

        elif comparison_type == "regex":
            # Match using regular expression
            if isinstance(actual, str) and isinstance(expected, str):
                if re.search(expected, actual):
                    return True, None
                return False, f"Value '{actual}' does not match regex '{expected}'"
            return False, f"Cannot perform regex comparison on {type(actual).__name__}"

        elif comparison_type == "greater_than" or comparison_type == "gt":
            # Compare if actual > expected
            if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
                if actual > expected:
                    return True, None
                return False, f"Value {actual} is not greater than {expected}"
            return (
                False,
                f"Cannot perform 'greater than' comparison on {type(actual).__name__}",
            )

        elif comparison_type == "less_than" or comparison_type == "lt":
            # Compare if actual < expected
            if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
                if actual < expected:
                    return True, None
                return False, f"Value {actual} is not less than {expected}"
            return (
                False,
                f"Cannot perform 'less than' comparison on {type(actual).__name__}",
            )

        elif comparison_type == "not_equals" or comparison_type == "ne":
            # Compare if actual != expected
            if actual != expected:
                return True, None
            return False, f"Value '{actual}' equals '{expected}' when it should not"

        else:
            # Unknown comparison type
            return False, f"Unknown comparison type: {comparison_type}"
