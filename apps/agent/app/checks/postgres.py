"""
PostgreSQL Check Module - Implements PostgreSQL database health checks.
"""

from typing import Any
from urllib.parse import urlparse

from app.checks._ssrf_policy import assert_target_allowed
from app.checks.base import BaseCheck, CheckConfigError
from app.shared import ssrf
from app.shared.logger import get_logger

try:
    import asyncpg

    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False


logger = get_logger("luxswirl.agent.checks.postgres")


class PostgreSQLCheck(BaseCheck):
    """Check for PostgreSQL database connectivity and query execution."""

    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate PostgreSQL-specific configuration.

        Args:
            config: The check configuration to validate

        Raises:
            ValueError: If required configuration fields are missing or invalid
        """
        super().validate_config(config)

        if not ASYNCPG_AVAILABLE:
            raise ValueError(
                "PostgreSQL check requires asyncpg library. Install with: pip install asyncpg"
            )

        if "connection_string" not in config and "target" not in config:
            raise ValueError(
                f"PostgreSQL check {config.get('name', 'unnamed')} must have a 'connection_string' or 'target'"
            )

        # Validate connection string format
        connection_string = config.get("connection_string") or config.get("target")
        if not isinstance(connection_string, str):
            raise ValueError(
                f"PostgreSQL connection string must be text, not "
                f"{type(connection_string).__name__}. "
                "Expected format: postgres://username:password@host:port/database"
            )
        try:
            parsed = urlparse(connection_string)
            if parsed.scheme not in ["postgres", "postgresql"]:
                raise ValueError(
                    f"Invalid PostgreSQL connection string scheme: {parsed.scheme!s}. "
                    "Must be 'postgres://' or 'postgresql://'"
                )
        except Exception as e:
            raise ValueError(
                f"Invalid PostgreSQL connection string format: {str(e)}. "
                "Expected format: postgres://username:password@host:port/database"
            ) from e

    async def run(self) -> dict[str, Any]:
        """Execute the PostgreSQL check.

        Returns:
            A dictionary containing the check result
        """
        # Get connection string from either 'connection_string' or 'target'
        connection_string = self.config.get("connection_string") or self.config.get(
            "target"
        )
        query = self.config.get("query", "SELECT 1")  # Default health check query
        timeout = self.config.get("timeout", 5)
        retries = self.config.get("retries", 1)
        if not isinstance(connection_string, str):
            raise CheckConfigError("The connection string must be text")

        # Parse connection string to get host/database info for result data
        parsed = urlparse(connection_string)
        host, port = self._endpoint(connection_string)
        where = host if port is None else f"{host}:{port}"
        database = str(parsed.path).lstrip("/") if parsed.path else "unknown"

        # SSRF: block a connection host that resolves into a blocked range.
        assert_target_allowed(connection_string)

        success = False
        connection_latency_ms = None
        query_latency_ms = None
        total_latency_ms = None
        error = None
        error_type = None
        additional_data = {
            "host": host,
            "port": port,
            "database": database,
            "query": query,
        }

        # Try check with retries
        for _attempt in range(retries):
            conn = None
            try:
                # Measure connection time
                start_time = self.start_timer()

                # Establish connection
                conn = await asyncpg.connect(
                    dsn=connection_string,
                    timeout=timeout,
                )

                connection_latency_ms = self.stop_timer(start_time)
                additional_data["connection_latency_ms"] = connection_latency_ms

                # Measure query time
                query_start_time = self.start_timer()

                # Execute query
                result = await conn.fetch(query)

                query_latency_ms = self.stop_timer(query_start_time)
                additional_data["query_latency_ms"] = query_latency_ms

                # Calculate total latency
                total_latency_ms = self.stop_timer(start_time)

                # Store query result info (row count, not actual data for security)
                row_count = len(result) if result else 0
                additional_data["row_count"] = row_count

                # Extract column names if available
                if result and len(result) > 0:
                    columns = list(result[0].keys())
                    additional_data["columns"] = columns

                # If we get here, the check succeeded
                success = True
                break

            except asyncpg.InvalidPasswordError:
                error = "Authentication failed - invalid username or password"
                error_type = "authentication_error"
                continue
            except asyncpg.InvalidCatalogNameError:
                error = f"Database does not exist: {database!s}"
                error_type = "connection_error"
                continue
            except asyncpg.CannotConnectNowError:
                error = "Database is not accepting connections"
                error_type = "connection_error"
                continue
            except asyncpg.ConnectionDoesNotExistError:
                error = f"Cannot connect to {where}"
                error_type = "connection_error"
                continue
            except asyncpg.PostgresSyntaxError as e:
                error = f"SQL syntax error in query: {str(e)}"
                error_type = "query_error"
                # Record connection time even if query failed
                if connection_latency_ms:
                    additional_data["connection_latency_ms"] = connection_latency_ms
                continue
            except asyncpg.PostgresError as e:
                # Generic PostgreSQL error
                error = f"PostgreSQL error: {str(e)}"
                error_type = "database_error"
                # Record connection time even if query failed
                if connection_latency_ms:
                    additional_data["connection_latency_ms"] = connection_latency_ms
                continue
            except asyncpg.exceptions.TooManyConnectionsError:
                error = "Too many connections to database"
                error_type = "connection_error"
                continue
            except TimeoutError:
                error = f"Connection/query timed out after {timeout}s"
                error_type = "timeout_error"
                continue
            except OSError as e:
                # Refused, unreachable, unresolvable, or a missing SSL file.
                error = f"Cannot connect to {where}: {e}"
                error_type = "connection_error"
                continue
            except asyncpg.exceptions.ClientConfigurationError as e:
                # The connection string's own options (sslmode, target_session_attrs).
                error = f"Connection settings refused: {e}"
                error_type = "connection_error"
                continue
            except asyncpg.InterfaceError as e:
                if conn is None:
                    # The server asks for a login method asyncpg does not
                    # support (an unknown authentication code, a SASL
                    # mechanism other than SCRAM).
                    error = f"Login method unavailable: {e}"
                    error_type = "connection_error"
                else:
                    # The client refused the exchange: a query that names a
                    # parameter ($1) the check cannot pass, for one.
                    error = f"PostgreSQL client error: {e}"
                    error_type = "query_error"
                continue
            except asyncpg.exceptions.InternalClientError as e:
                login_failure = self._login_failure(e) if conn is None else None
                if login_failure is None:
                    # Raised after the login: asyncpg's own fault, reported
                    # with its traceback at DEBUG like any unexpected error.
                    logger.debug(
                        "PostgreSQL check hit an unexpected error",
                        exc_info=True,
                        extra={"check_name": self.name},
                    )
                    error = f"Unexpected error: {str(e)}"
                    error_type = "unknown_error"
                else:
                    error, error_type = login_failure
                continue
            # swallowed-exceptions: the failure is the check's result, sent to the
            # server; its traceback goes to DEBUG so a failure that recurs every
            # interval cannot flood the log.
            except Exception as e:
                logger.debug(
                    "PostgreSQL check hit an unexpected error",
                    exc_info=True,
                    extra={"check_name": self.name},
                )
                error = f"Unexpected error: {str(e)}"
                error_type = "unknown_error"
                continue
            finally:
                # Always close the connection
                if conn:
                    try:
                        await conn.close()
                    # swallowed-exceptions: best-effort close after the result is already decided
                    except Exception as close_error:
                        # Best-effort close: a failure here must not mask the
                        # check result, but a connection we cannot close is a
                        # leak worth seeing in the log.
                        logger.warning(
                            "Failed to close PostgreSQL connection after check",
                            extra={"error": str(close_error)},
                        )

        # Create the result with PostgreSQL data in metrics
        return self.create_result(
            success=success,
            latency_ms=total_latency_ms,  # Total latency for overall metric
            error=error,
            metrics={
                "postgres": {
                    "host": host,
                    "port": port,
                    "database": database,
                    "query": query,
                    "connection_latency_ms": connection_latency_ms,
                    "query_latency_ms": query_latency_ms,
                    "row_count": additional_data.get("row_count"),
                    "columns": additional_data.get("columns"),
                    "error_type": error_type,
                }
            },
        )

    @staticmethod
    def _endpoint(connection_string: str) -> tuple[str, int | None]:
        """The host and port the URL names, for the result and its errors.

        A URL naming several hosts (``h1:5432,h2:5433``, as libpq and asyncpg
        accept) has the hosts joined and no single port.

        Raises:
            CheckConfigError: One host, with a port that is not a number in range.
        """
        parsed = urlparse(connection_string)
        try:
            return parsed.hostname or "unknown", parsed.port or 5432
        except ValueError as e:
            if "," not in parsed.netloc.rpartition("@")[2]:
                raise CheckConfigError(f"Invalid port: {e}") from e
        return ",".join(ssrf.extract_hosts(connection_string)) or "unknown", None

    @staticmethod
    def _login_failure(error: Exception) -> tuple[str, str] | None:
        """Name a failure inside asyncpg's login exchange by its cause.

        asyncpg reports any error its authentication code raises as an
        ``InternalClientError`` whose ``__cause__`` is that error; no other
        ``InternalClientError`` it raises carries a cause.

        Args:
            error: The ``InternalClientError`` raised while connecting.

        Returns:
            The check's error and error_type, or None when the error did not
            come from the login exchange.
        """
        cause = error.__cause__
        if cause is None:
            return None
        if isinstance(cause, asyncpg.InterfaceError):
            # GSSAPI or SSPI login, with asyncpg[gssauth] not installed.
            return f"Login method unavailable: {cause}", "connection_error"
        if isinstance(cause, AttributeError):
            # A cleartext password login, and no password in the connection
            # string, PGPASSWORD or the password file: asyncpg has none to send.
            return (
                "Authentication failed - the server asked for a password "
                "and the connection string gives none",
                "authentication_error",
            )
        return f"Authentication failed: {cause}", "authentication_error"
