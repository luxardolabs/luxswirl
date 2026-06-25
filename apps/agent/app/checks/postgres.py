"""
PostgreSQL Check Module - Implements PostgreSQL database health checks.
"""

from typing import Any
from urllib.parse import urlparse

from shared.ssrf import assert_target_allowed

from app.checks.base import BaseCheck

try:
    import asyncpg

    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False


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
        connection_string = self.config.get("connection_string") or self.config.get("target")
        query = self.config.get("query", "SELECT 1")  # Default health check query
        timeout = self.config.get("timeout", 5)
        retries = self.config.get("retries", 1)

        # Parse connection string to get host/database info for result data
        parsed = urlparse(connection_string)
        host = parsed.hostname or "unknown"
        port = parsed.port or 5432
        database = str(parsed.path).lstrip("/") if parsed.path else "unknown"

        # SSRF: block a connection host that resolves into the cloud-metadata range.
        if connection_string:
            assert_target_allowed(connection_string, block_cloud_metadata=True)

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
                error = f"Cannot connect to {host!s}:{port!s}"
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
            except Exception as e:
                error = f"Unexpected error: {str(e)}"
                error_type = "unknown_error"
                continue
            finally:
                # Always close the connection
                if conn:
                    try:
                        await conn.close()
                    except Exception:
                        pass

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
