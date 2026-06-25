"""
Result reporter for the LuxSwirl agent.

Stores failed batches in a local SQLite database with gzip-compressed payloads.
Automatically migrates legacy JSON file storage on first startup.
"""

import asyncio
import gzip
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
from shared.logger import get_logger

# SQL constants
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS pending_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    batch_size INTEGER NOT NULL,
    payload BLOB NOT NULL
)
"""
_CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_created_at ON pending_batches(created_at)"


class Reporter:
    """Reporter class for sending check results to the server."""

    def __init__(self, config: dict[str, Any]):
        """Initialize the reporter.

        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.logger = get_logger("luxswirl.reporter")

        # Get reporter-specific config
        self.push_url = config.get("push_url")
        self.auth_key = config.get("auth_key")
        # Note: agent_id read from config dynamically to support late registration

        # Retry configuration
        self.max_retries = config.get("report_max_retries", 3)
        self.retry_delay = config.get("report_retry_delay", 2)

        # Local storage for failed reports
        self.storage_path = Path(config.get("report_storage_path", "reports"))
        self.enable_local_storage = config.get("enable_local_storage", True)
        self.process_interval = config.get("report_process_interval", 10)
        self.max_files_per_batch = config.get("report_max_files_per_batch", 5)
        self.max_stored_batches = config.get("report_max_stored_batches", 10000)

        # Create storage directory if needed
        if self.enable_local_storage:
            self.storage_path.mkdir(parents=True, exist_ok=True)

        # Batching configuration
        self.batch_size = config.get("report_batch_size", 500)
        self.report_interval = config.get("report_interval", 10)
        self.batch_timeout = config.get("report_batch_timeout", 10)

        # Queue limits
        self.max_queue_size = config.get("report_max_queue_size", 5000)
        self.backpressure_threshold = config.get("report_backpressure_threshold", 0.8)

        # Current batch
        self.current_batch: list[dict[str, Any]] = []
        self.batch_lock = asyncio.Lock()
        self.last_sent_time = time.time()  # Initialize to current time

        # Event for signaling flushes
        self.flush_event = asyncio.Event()

        # Client session
        self.client: httpx.AsyncClient | None = None

        # Flag to control background tasks
        self.running = False

        # Background tasks
        self.stored_reports_task: asyncio.Task[None] | None = None
        self.periodic_flush_task: asyncio.Task[None] | None = None
        self.batch_processor_task: asyncio.Task[None] | None = None

        # SQLite connection (used from background thread via to_thread)
        self._db_path = self.storage_path / "pending_reports.db"
        self._db_initialized = False

        # Metrics
        self.metrics = {
            "batches_sent": 0,
            "results_sent": 0,
            "send_failures": 0,
            "dropped_results": 0,
            "flush_count": 0,
            "flush_time_ms": 0,
            "last_batch_size": 0,
            "max_batch_size": 0,
            "last_backlog": 0,
            "start_time": time.time(),
        }

    def _get_db(self) -> sqlite3.Connection:
        """Get a SQLite connection for the current thread.

        Returns:
            A sqlite3.Connection with WAL mode enabled.
        """
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_storage_sync(self) -> None:
        """Create the SQLite database and schema (runs in thread)."""
        conn = self._get_db()
        try:
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_INDEX)
            conn.commit()
        finally:
            conn.close()
        self._db_initialized = True

    async def _init_storage(self) -> None:
        """Initialize SQLite storage and migrate any legacy JSON files."""
        if self._db_initialized:
            return
        await asyncio.to_thread(self._init_storage_sync)
        await self._migrate_json_files()

    def _migrate_json_files_sync(self) -> None:
        """Migrate existing report_*.json files into SQLite (runs in thread)."""
        json_files = sorted(
            self.storage_path.glob("report_*.json"),
            key=lambda p: p.stat().st_ctime,
        )
        if not json_files:
            return

        total = len(json_files)
        self.logger.info(
            "Found legacy JSON report files to migrate",
            extra={"total": total},
        )

        conn = self._get_db()
        migrated = 0
        try:
            batch_of_files = []
            for f in json_files:
                batch_of_files.append(f)
                if len(batch_of_files) >= 100:
                    migrated += self._migrate_file_batch(conn, batch_of_files)
                    self.logger.info(
                        "Migrated stored reports to SQLite",
                        extra={"migrated": migrated, "total": total},
                    )
                    batch_of_files = []

            if batch_of_files:
                migrated += self._migrate_file_batch(conn, batch_of_files)

            self.logger.info(
                "Migration complete",
                extra={"migrated": migrated, "total": total},
            )
        finally:
            conn.close()

    def _migrate_file_batch(self, conn: sqlite3.Connection, files: list[Path]) -> int:
        """Migrate a batch of JSON files into SQLite.

        Returns:
            Number of files successfully migrated.
        """
        count = 0
        for f in files:
            try:
                content = f.read_text()
                report = json.loads(content)
                agent_id = report.get("agent_id", "unknown")
                created_at = report.get("timestamp", f.stat().st_ctime)
                checks = report.get("checks", [])
                compressed = gzip.compress(json.dumps(checks).encode())

                conn.execute(
                    "INSERT INTO pending_batches (agent_id, created_at, batch_size, payload) VALUES (?, ?, ?, ?)",
                    (agent_id, float(created_at), len(checks), compressed),
                )
                conn.commit()
                f.unlink()
                count += 1
            except Exception:
                self.logger.warning(
                    "Failed to migrate file",
                    extra={"file_name": f.name},
                    exc_info=True,
                )
        return count

    async def _migrate_json_files(self) -> None:
        """Migrate legacy JSON files into SQLite (async wrapper)."""
        await asyncio.to_thread(self._migrate_json_files_sync)

    async def start(self) -> None:
        """Start the reporter."""
        if self.running:
            return

        self.running = True
        self.last_sent_time = time.time()

        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "User-Agent": "LuxSwirl-Agent/1.0",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.auth_key}",
            },
        )

        # Initialize SQLite storage
        if self.enable_local_storage:
            await self._init_storage()

        # Start the batch processor task
        self.batch_processor_task = asyncio.create_task(self.batch_processor())

        # Start the background task for processing stored reports
        if self.enable_local_storage:
            self.logger.info("Starting stored reports processor")
            self.stored_reports_task = asyncio.create_task(self.process_stored_reports())

    async def stop(self) -> None:
        """Stop the reporter and clean up."""
        if not self.running:
            return

        self.logger.info("Stopping reporter")
        self.running = False

        # Signal any waiting tasks
        self.flush_event.set()

        try:
            # Flush any pending reports
            if self.current_batch:
                await self.flush()

            # Close the client
            if self.client:
                await self.client.aclose()
                self.client = None

            # Wait for background tasks to complete with timeout
            tasks: list[asyncio.Task[None]] = []
            if self.stored_reports_task and not self.stored_reports_task.done():
                tasks.append(self.stored_reports_task)

            if self.batch_processor_task and not self.batch_processor_task.done():
                tasks.append(self.batch_processor_task)

            if tasks:
                try:
                    # Wait for tasks with timeout
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True), timeout=5.0
                    )
                except TimeoutError:
                    self.logger.warning("Timed out waiting for reporter tasks to stop")

        except Exception:
            self.logger.error("Error stopping reporter", exc_info=True)

        self.logger.info("Reporter stopped")

    async def batch_processor(self) -> None:
        """Process batches asynchronously to avoid blocking result handling."""
        self.logger.info(
            "Starting batch processor",
            extra={"interval_seconds": self.report_interval},
        )

        while self.running:
            try:
                # Wait for flush notification or timeout for periodic flush
                try:
                    await asyncio.wait_for(
                        self.flush_event.wait(), timeout=self.report_interval / 2
                    )
                    self.flush_event.clear()
                except TimeoutError:
                    pass

                # Check if we need to flush based on time or batch size
                current_time = time.time()
                time_since_last_send = current_time - self.last_sent_time
                needs_flush = False

                async with self.batch_lock:
                    batch_size = len(self.current_batch)
                    if batch_size >= self.batch_size or (
                        batch_size > 0 and time_since_last_send >= self.report_interval
                    ):
                        needs_flush = True
                        self.logger.debug(
                            "Batch processor triggering flush",
                            extra={
                                "batch_size": batch_size,
                                "time_since_last_send_seconds": round(time_since_last_send, 1),
                            },
                        )

                if needs_flush:
                    start_time = time.perf_counter()
                    await self.flush()
                    elapsed = (time.perf_counter() - start_time) * 1000
                    self.metrics["flush_time_ms"] = elapsed
                    self.metrics["flush_count"] += 1

            except asyncio.CancelledError:
                break
            except Exception:
                self.logger.error("Error in batch processor", exc_info=True)

            # Short sleep to prevent CPU spinning
            await asyncio.sleep(0.1)

        self.logger.info("Batch processor stopped")

    async def add_result(self, result: dict[str, Any]) -> None:
        """Add a result to the current batch with graceful degradation.

        Args:
            result: The check result to add
        """
        if not self.running:
            await self.start()

        # Check if we need to apply backpressure
        async with self.batch_lock:
            current_batch_size = len(self.current_batch)

            # If we're getting overwhelmed, start dropping older results
            max_batch_before_drop = int(self.max_queue_size * self.backpressure_threshold)
            if current_batch_size > max_batch_before_drop:
                self.logger.warning(
                    "Reporter nearing capacity",
                    extra={
                        "current_batch_size": current_batch_size,
                        "max_queue_size": self.max_queue_size,
                    },
                )

            if current_batch_size >= self.max_queue_size:
                self.logger.warning(
                    "Reporter overwhelmed - dropping oldest results",
                    extra={
                        "current_batch_size": current_batch_size,
                        "max_queue_size": self.max_queue_size,
                    },
                )
                # Keep only newest results
                self.current_batch = self.current_batch[-(self.max_queue_size - 1) :]
                self.metrics["dropped_results"] += 1

            # Add the new result
            self.current_batch.append(result)

            # Update metrics
            if current_batch_size > self.metrics["max_batch_size"]:
                self.metrics["max_batch_size"] = current_batch_size
            self.metrics["last_backlog"] = current_batch_size

            # Signal if we've reached batch threshold
            if len(self.current_batch) >= self.batch_size:
                self.logger.debug(
                    "Batch size threshold reached, signaling flush",
                    extra={"batch_size": self.batch_size},
                )
                self.flush_event.set()

    async def flush(self) -> bool:
        """Flush the current batch of results.

        Returns:
            True if the batch was sent successfully, False otherwise
        """
        # Take a snapshot of the current batch under the lock
        async with self.batch_lock:
            if not self.current_batch:
                return True

            # Take a copy of the batch and clear it
            batch = self.current_batch.copy()
            self.current_batch = []

        # Process the batch outside the lock
        self.logger.info(
            "Flushing batch",
            extra={"batch_size": len(batch)},
        )
        success = await self.send_batch(batch)

        return success

    async def send_batch(self, batch: list[dict[str, Any]]) -> bool:
        """Send a batch of results to the server.

        Args:
            batch: List of check results to send

        Returns:
            True if the batch was sent successfully, False otherwise
        """
        if not batch:
            return True

        if not self.push_url:
            self.logger.warning("No push URL configured, not sending results")
            return False

        # Prepare the payload
        agent_id = self.config.get("agent_id", "unknown")
        payload = {
            "agent_id": str(agent_id) if agent_id != "unknown" else "unknown",
            "timestamp": time.time(),
            "checks": batch,
        }

        success = False
        retry_count = 0

        self.logger.info(
            "Attempting to send batch",
            extra={"batch_size": len(batch), "push_url": self.push_url},
        )

        while retry_count < self.max_retries and not success and self.running:
            try:
                if retry_count > 0:
                    self.logger.info(
                        "Retrying batch send",
                        extra={
                            "attempt": retry_count + 1,
                            "max_retries": self.max_retries,
                        },
                    )

                # Send the request
                if not self.client:
                    await self.start()
                assert self.client is not None

                resp = await self.client.post(self.push_url, json=payload)

                if resp.status_code < 300:
                    self.logger.info(
                        "Successfully sent batch",
                        extra={"batch_size": len(batch)},
                    )
                    self.last_sent_time = time.time()
                    success = True

                    # Update metrics
                    self.metrics["batches_sent"] += 1
                    self.metrics["results_sent"] += len(batch)
                    self.metrics["last_batch_size"] = len(batch)

                else:
                    self.logger.warning(
                        "Failed to send results",
                        extra={
                            "status_code": resp.status_code,
                            "response_text": resp.text,
                        },
                    )
                    retry_count += 1
                    self.metrics["send_failures"] += 1

                    if not self.running:
                        break

                    await asyncio.sleep(self.retry_delay)

            except httpx.RequestError:
                self.logger.warning("Connection error while sending results", exc_info=True)
                retry_count += 1
                self.metrics["send_failures"] += 1

                if not self.running:
                    break

                await asyncio.sleep(self.retry_delay)

            except Exception:
                self.logger.error("Unexpected error while sending results", exc_info=True)
                retry_count += 1
                self.metrics["send_failures"] += 1

                if not self.running:
                    break

                await asyncio.sleep(self.retry_delay)

        # If we couldn't send the results, store them locally
        if not success and self.enable_local_storage:
            await self._store_batch_to_db(batch)

        return success

    def _store_batch_to_db_sync(self, batch: list[dict[str, Any]]) -> None:
        """Store a failed batch into SQLite with gzip compression (runs in thread)."""
        agent_id = self.config.get("agent_id", "unknown")
        agent_id_str = str(agent_id) if agent_id != "unknown" else "unknown"
        created_at = time.time()
        compressed = gzip.compress(json.dumps(batch).encode())

        conn = self._get_db()
        try:
            conn.execute(
                "INSERT INTO pending_batches (agent_id, created_at, batch_size, payload) VALUES (?, ?, ?, ?)",
                (agent_id_str, created_at, len(batch), compressed),
            )
            conn.commit()

            # Enforce disk cap — prune oldest batches if over limit
            row = conn.execute("SELECT COUNT(*) FROM pending_batches").fetchone()
            total = row[0]
            if total > self.max_stored_batches:
                excess = total - self.max_stored_batches
                conn.execute(
                    "DELETE FROM pending_batches WHERE id IN "
                    "(SELECT id FROM pending_batches ORDER BY created_at ASC LIMIT ?)",
                    (excess,),
                )
                conn.commit()
                self.logger.warning(
                    "Pruned oldest stored batches",
                    extra={
                        "pruned_count": excess,
                        "cap": self.max_stored_batches,
                    },
                )

            self.logger.info(
                "Stored results to SQLite",
                extra={
                    "result_count": len(batch),
                    "compressed_bytes": len(compressed),
                },
            )
        finally:
            conn.close()

    async def _store_batch_to_db(self, batch: list[dict[str, Any]]) -> None:
        """Store a batch of results in SQLite for later sending.

        Args:
            batch: List of check results to store
        """
        if not self.enable_local_storage:
            return

        self.logger.info(
            "Attempting to store batch locally",
            extra={"db_path": str(self._db_path)},
        )

        try:
            await asyncio.to_thread(self._store_batch_to_db_sync, batch)
        except Exception:
            self.logger.error("Failed to store results to SQLite", exc_info=True)
            import traceback

            self.logger.error(traceback.format_exc())

    def _process_stored_reports_sync(self) -> list[tuple[int, list[dict[str, Any]]]]:
        """Fetch pending batches from SQLite for replay (runs in thread).

        Returns:
            List of (row_id, checks) tuples ready to send.
        """
        conn = self._get_db()
        try:
            rows = conn.execute(
                "SELECT id, payload FROM pending_batches ORDER BY created_at ASC LIMIT ?",
                (self.max_files_per_batch,),
            ).fetchall()

            results = []
            for row_id, payload in rows:
                try:
                    checks = json.loads(gzip.decompress(payload))
                    results.append((row_id, checks))
                except Exception:
                    self.logger.warning(
                        "Corrupted stored batch, deleting",
                        extra={"row_id": row_id},
                        exc_info=True,
                    )
                    conn.execute("DELETE FROM pending_batches WHERE id = ?", (row_id,))
                    conn.commit()

            return results
        finally:
            conn.close()

    def _delete_stored_batch_sync(self, row_id: int) -> None:
        """Delete a successfully replayed batch from SQLite (runs in thread)."""
        conn = self._get_db()
        try:
            conn.execute("DELETE FROM pending_batches WHERE id = ?", (row_id,))
            conn.commit()
        finally:
            conn.close()

    async def process_stored_reports(self) -> None:
        """Process stored reports in the background."""
        self.logger.info(
            "Starting stored reports processor",
            extra={"interval_seconds": self.process_interval},
        )

        while self.running:
            try:
                # Fetch pending batches from SQLite
                pending = await asyncio.to_thread(self._process_stored_reports_sync)

                if pending and self.running:
                    self.logger.info(
                        "Found stored batches to process",
                        extra={"pending_count": len(pending)},
                    )

                    combined: list[dict[str, Any]] = []
                    consumed_ids: list[int] = []
                    aborted = False

                    for row_id, checks in pending:
                        if not self.running:
                            break

                        # Flush before adding if appending would exceed the cap.
                        if combined and len(combined) + len(checks) > self.batch_size:
                            if await self.send_batch(combined):
                                for cid in consumed_ids:
                                    await asyncio.to_thread(self._delete_stored_batch_sync, cid)
                                self.logger.info(
                                    "Drained stored batches in one POST",
                                    extra={
                                        "batch_count": len(consumed_ids),
                                        "result_count": len(combined),
                                    },
                                )
                                combined = []
                                consumed_ids = []
                            else:
                                self.logger.warning(
                                    "Failed replay POST",
                                    extra={
                                        "batch_count": len(consumed_ids),
                                        "result_count": len(combined),
                                    },
                                )
                                aborted = True
                                break

                        combined.extend(checks)
                        consumed_ids.append(row_id)

                    # Flush any remainder
                    if combined and self.running and not aborted:
                        if await self.send_batch(combined):
                            for cid in consumed_ids:
                                await asyncio.to_thread(self._delete_stored_batch_sync, cid)
                            self.logger.info(
                                "Drained stored batches in one POST",
                                extra={
                                    "batch_count": len(consumed_ids),
                                    "result_count": len(combined),
                                },
                            )
                        else:
                            self.logger.warning(
                                "Failed final replay POST",
                                extra={
                                    "batch_count": len(consumed_ids),
                                    "result_count": len(combined),
                                },
                            )

                # Check if we should continue running
                if not self.running:
                    break

                # Wait before checking again, with periodic checks of running status
                for _ in range(self.process_interval):
                    if not self.running:
                        break
                    await asyncio.sleep(1)

            except Exception:
                self.logger.error("Error in stored reports processor", exc_info=True)

                # Check if we should continue running
                if not self.running:
                    break

                # Shorter sleep on error
                await asyncio.sleep(5)

        self.logger.info("Stored reports processor stopped")

    def _get_metrics_sync(self) -> tuple[int, float | None]:
        """Get stored report metrics from SQLite (runs in thread).

        Returns:
            Tuple of (count, oldest_timestamp).
        """
        conn = self._get_db()
        try:
            row = conn.execute("SELECT COUNT(*), MIN(created_at) FROM pending_batches").fetchone()
            return (row[0], row[1])
        finally:
            conn.close()

    def get_metrics(self) -> dict[str, Any]:
        """Get current metrics for the reporter.

        Returns:
            Dictionary of reporter metrics
        """
        metrics = self.metrics.copy()
        metrics["uptime"] = time.time() - metrics["start_time"]
        metrics["current_batch_size"] = len(self.current_batch)
        metrics["report_interval"] = self.report_interval
        metrics["batch_size"] = self.batch_size

        # Add stored reports metrics if local storage is enabled
        if self.enable_local_storage and self._db_initialized:
            try:
                # Synchronous call — fast single-row query, safe from main thread
                count, oldest = self._get_metrics_sync()
                metrics["stored_reports_count"] = count
                metrics["stored_reports_oldest_timestamp"] = oldest or 0.0
            except Exception:
                self.logger.warning("Failed to get stored reports metrics", exc_info=True)
                metrics["stored_reports_count"] = 0
                metrics["stored_reports_oldest_timestamp"] = 0.0
        else:
            metrics["stored_reports_count"] = 0
            metrics["stored_reports_oldest_timestamp"] = 0.0

        return metrics


# Simplified function for backward compatibility
async def push_results(
    config: dict[str, Any], payload: list[dict[str, Any]], logger: logging.Logger
) -> bool:
    """Push results to the server (legacy function).

    Args:
        config: Configuration dictionary
        payload: List of check results
        logger: Logger instance

    Returns:
        True if the results were sent successfully, False otherwise
    """
    reporter = Reporter(config)
    await reporter.start()

    success = await reporter.send_batch(payload)

    await reporter.stop()
    return success
