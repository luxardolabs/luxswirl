"""
MySQL/MariaDB Check Module - Implements MySQL/MariaDB database health checks.
"""

from typing import Any
from urllib.parse import parse_qs, urlparse

from shared.ssrf import assert_target_allowed

from app.checks.base import BaseCheck

try:
    import aiomysql

    AIOMYSQL_AVAILABLE = True
except ImportError:
    AIOMYSQL_AVAILABLE = False


class MySQLCheck(BaseCheck):
    """Check for MySQL/MariaDB database connectivity and query execution."""

    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate MySQL-specific configuration.

        Args:
            config: The check configuration to validate

        Raises:
            ValueError: If required configuration fields are missing or invalid
        """
        super().validate_config(config)

        if not AIOMYSQL_AVAILABLE:
            raise ValueError(
                "MySQL check requires aiomysql library. Install with: pip install aiomysql"
            )

        if "connection_string" not in config and "target" not in config:
            raise ValueError(
                f"MySQL check {config.get('name', 'unnamed')} must have a 'connection_string' or 'target'"
            )

        # Validate connection string format
        connection_string = config.get("connection_string") or config.get("target")
        try:
            parsed = urlparse(connection_string)
            if parsed.scheme not in ["mysql", "mariadb"]:
                raise ValueError(
                    f"Invalid MySQL connection string scheme: {parsed.scheme!s}. "
                    "Must be 'mysql://' or 'mariadb://'"
                )
        except Exception as e:
            raise ValueError(
                f"Invalid MySQL connection string format: {str(e)}. "
                "Expected format: mysql://username:password@host:port/database"
            ) from e

    def _parse_connection_string(self, connection_string: str) -> dict[str, str | int | None]:
        """Parse MySQL connection string into components.

        Args:
            connection_string: Connection string in format mysql://user:pass@host:port/db

        Returns:
            Dictionary with host, port, user, password, database keys
        """
        parsed = urlparse(connection_string)

        # Parse query parameters for additional options
        query_params = parse_qs(parsed.query) if parsed.query else {}

        return {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 3306,
            "user": parsed.username or "",
            "password": parsed.password or "",
            "database": parsed.path.lstrip("/") if parsed.path else "",
            # Optional SSL/TLS parameters
            "ssl": query_params.get("ssl", [None])[0],
        }

    async def run(self) -> dict[str, Any]:
        """Execute the MySQL check.

        Returns:
            A dictionary containing the check result
        """
        # Get connection string from either 'connection_string' or 'target'
        connection_string = self.config.get("connection_string") or self.config.get("target")
        query = self.config.get("query", "SELECT 1")  # Default health check query
        timeout = self.config.get("timeout", 5)
        retries = self.config.get("retries", 1)

        # Parse connection string
        if not isinstance(connection_string, str):
            raise ValueError("Connection string must be provided")
        conn_params = self._parse_connection_string(connection_string)
        host = conn_params["host"]
        port = conn_params["port"]
        database = conn_params["database"]

        # SSRF: block a connection host that resolves into the cloud-metadata range.
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
                conn = await aiomysql.connect(
                    host=conn_params["host"],
                    port=conn_params["port"],
                    user=conn_params["user"],
                    password=conn_params["password"],
                    db=conn_params["database"],
                    connect_timeout=timeout,
                )

                connection_latency_ms = self.stop_timer(start_time)
                additional_data["connection_latency_ms"] = connection_latency_ms

                # Measure query time
                query_start_time = self.start_timer()

                # Execute query
                async with conn.cursor() as cursor:
                    await cursor.execute(query)
                    result = await cursor.fetchall()

                    query_latency_ms = self.stop_timer(query_start_time)
                    additional_data["query_latency_ms"] = query_latency_ms

                    # Calculate total latency
                    total_latency_ms = self.stop_timer(start_time)

                    # Store query result info (row count, not actual data for security)
                    row_count = len(result) if result else 0
                    additional_data["row_count"] = row_count

                    # Extract column names if available
                    if cursor.description:
                        columns = [desc[0] for desc in cursor.description]
                        additional_data["columns"] = columns

                # If we get here, the check succeeded
                success = True
                break

            except aiomysql.OperationalError as e:
                # Connection or operational errors
                error_code = e.args[0] if e.args else None

                if error_code == 1045:
                    error = "Authentication failed - invalid username or password"
                    error_type = "authentication_error"
                elif error_code == 1049:
                    error = f"Database does not exist: {database}"
                    error_type = "connection_error"
                elif error_code == 2003:
                    error = f"Cannot connect to {host}:{port} - connection refused"
                    error_type = "connection_error"
                elif error_code == 2005:
                    error = f"Unknown MySQL server host: {host}"
                    error_type = "connection_error"
                elif error_code == 2006:
                    error = "MySQL server has gone away"
                    error_type = "connection_error"
                elif error_code == 2013:
                    error = "Lost connection to MySQL server during query"
                    error_type = "connection_error"
                else:
                    error = f"MySQL operational error: {str(e)}"
                    error_type = "connection_error"
                continue

            except aiomysql.ProgrammingError as e:
                # SQL syntax or programming errors
                error = f"SQL syntax error in query: {str(e)}"
                error_type = "query_error"
                # Record connection time even if query failed
                if connection_latency_ms:
                    additional_data["connection_latency_ms"] = connection_latency_ms
                continue

            except aiomysql.IntegrityError as e:
                # Integrity constraint violations
                error = f"Database integrity error: {str(e)}"
                error_type = "query_error"
                # Record connection time even if query failed
                if connection_latency_ms:
                    additional_data["connection_latency_ms"] = connection_latency_ms
                continue

            except aiomysql.DatabaseError as e:
                # Generic database errors
                error = f"MySQL database error: {str(e)}"
                error_type = "database_error"
                # Record connection time even if query failed
                if connection_latency_ms:
                    additional_data["connection_latency_ms"] = connection_latency_ms
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
                        conn.close()
                    except Exception:
                        pass

        # Create the result with MySQL data in metrics
        return self.create_result(
            success=success,
            latency_ms=total_latency_ms,  # Total latency for overall metric
            error=error,
            metrics={
                "mysql": {
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
