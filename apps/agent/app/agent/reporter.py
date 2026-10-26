"""
Result reporter for the LuxSwirl agent.

Stores batches the server could not take yet in a local SQLite database with
gzip-compressed payloads, and replays them. A batch the server itself refuses
as invalid (a 400/422 in its JSON error envelope) goes to the `dead_letters`
table instead (see `dead_letter`), so it never blocks the queue behind it. Automatically migrates legacy JSON file
storage on first startup.
"""

import asyncio
import gzip
import json
import logging
import sqlite3
import time
from collections import deque
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from app.agent import server_api
from app.agent.dead_letter import (
    CREATE_DEAD_LETTERS,
    Delivery,
    classify_response,
    count_dead_letters,
    error_detail,
    insert_dead_letter,
    prune_dead_letters,
)
from app.agent.result_limits import fit_result
from app.shared.logger import get_logger

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
_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_created_at ON pending_batches(created_at)"
)


class StoredBatch(NamedTuple):
    """A pending batch read back from SQLite for replay."""

    row_id: int
    created_at: float
    checks: list[dict[str, Any]]


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
        self.reports_url = server_api.reports_url(config)
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
        # Stored rows a size refusal (413) left unchanged: replay reads past
        # them, so they never fill its read window, and retries them once
        # nothing else is left to read.
        self._size_held: set[int] = set()
        self.max_stored_batches = config.get("report_max_stored_batches", 10000)
        self.max_dead_letters = config.get("report_max_dead_letters", 1000)

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
            conn.execute(CREATE_DEAD_LETTERS)
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
            # swallowed-exceptions: the reporter retries and spools to disk; raising would drop results already collected
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

    async def connect(self) -> None:
        """Open the long-lived HTTP client (one per process, reused for its lifetime).

        Building a client per request pays DNS + TCP + TLS setup on every batch and
        heartbeat and defeats connection pooling; this one is opened once here and
        closed in `close()`.
        """
        if self.client is not None:
            return

        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "User-Agent": "LuxSwirl-Agent/1.0",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.auth_key}",
            },
        )

    async def close(self) -> None:
        """Close the long-lived HTTP client. Safe to call when never connected."""
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def start(self) -> None:
        """Start the reporter."""
        if self.running:
            return

        self.running = True
        self.last_sent_time = time.time()

        await self.connect()

        # Initialize SQLite storage
        if self.enable_local_storage:
            await self._init_storage()

        # Start the batch processor task
        self.batch_processor_task = asyncio.create_task(self.batch_processor())

        # Start the background task for processing stored reports
        if self.enable_local_storage:
            self.logger.info("Starting stored reports processor")
            self.stored_reports_task = asyncio.create_task(
                self.process_stored_reports()
            )

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
            await self.close()

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

        # swallowed-exceptions: the reporter retries and spools to disk; raising would drop results already collected
        except Exception:
            self.logger.exception("Error stopping reporter")

        self.logger.info("Reporter stopped")

    def _stopped(self) -> bool:
        """True once stop() has run.

        A loop re-reads this after every await: stop() runs on another task, so
        the flag can change while this one is suspended. A method call, not the
        bare attribute, so a type checker does not carry a `while self.running`
        narrowing across the await and call the exit branch unreachable.
        """
        return not self.running

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
                                "time_since_last_send_seconds": round(
                                    time_since_last_send, 1
                                ),
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
            # swallowed-exceptions: the reporter retries and spools to disk; raising would drop results already collected
            except Exception:
                self.logger.exception("Error in batch processor")

            # Short sleep to prevent CPU spinning
            await asyncio.sleep(0.1)

        self.logger.info("Batch processor stopped")

    async def add_result(self, result: dict[str, Any]) -> None:
        """Add a result to the current batch with graceful degradation.

        The result is first fitted to the server's per-result limits
        (`result_limits.fit_result`), so a long error or an odd HTTP status
        never costs the server-side result.

        Args:
            result: The check result to add
        """
        if not self.running:
            await self.start()

        fit_result(result)

        # Check if we need to apply backpressure
        async with self.batch_lock:
            current_batch_size = len(self.current_batch)

            # If we're getting overwhelmed, start dropping older results
            max_batch_before_drop = int(
                self.max_queue_size * self.backpressure_threshold
            )
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

        A transient failure stores the batch for replay; a permanent refusal
        (the server's own 400/422 envelope saying the payload is invalid, see
        `dead_letter.classify_response`) dead-letters it, since sending it
        again cannot succeed; a batch over the request size limit (413) is
        sent in halves (`_send_in_parts`).

        Args:
            batch: List of check results to send

        Returns:
            True if the whole batch was sent, False otherwise
        """
        if not batch:
            return True

        return await self._deliver(batch) is Delivery.SENT

    async def _deliver(self, batch: list[dict[str, Any]]) -> Delivery:
        """POST a batch; whatever the server does not take is stored for replay
        (as one batch) or dead-lettered, never dropped.

        Returns:
            SENT when all of it was taken, PERMANENT when some was dead-lettered
            and the rest taken, otherwise what kept the rest from going
            (TRANSIENT or TOO_LARGE).
        """
        outcome, status_code, detail = await self._post_batch(batch)
        if outcome is Delivery.SENT:
            return Delivery.SENT
        if outcome is Delivery.PERMANENT and status_code is not None:
            await self._dead_letter_batch(batch, status_code, detail)
            return Delivery.PERMANENT
        unsent = batch
        if outcome is Delivery.TOO_LARGE:
            outcome, unsent = await self._send_in_parts(batch)
        if unsent:
            await self._store_batch_to_db(unsent)
        return outcome

    async def _send_in_parts(
        self, batch: list[dict[str, Any]], created_at: float | None = None
    ) -> tuple[Delivery, list[dict[str, Any]]]:
        """Send a batch the request size limit refused (413) in halves, each
        split again while still refused, in order. Nothing is stored here.

        A single result the limit still refuses is kept and the split goes on
        past it, so it never holds back the results beside it. It is never
        dead-lettered on a proxy's word: `fit_result` caps a result's long text
        fields far below the body limit a deployment sets, so a 413 on one
        result usually says the route is wrong (a misrouted request, a
        misconfigured proxy), and dead-lettering would empty a misaddressed
        agent's queue. Two refused results with nothing sent say the same of
        the whole route, and the split stops there, as at a transient failure.

        The results not sent come back untried first and refused last, so the
        next try reaches the untried ones before the stop rule can meet the
        refused ones again.

        Args:
            batch: The refused batch.
            created_at: When the batch was first stored, kept on any part the
                server dead-letters; now when it is a fresh send.

        Returns:
            SENT, or PERMANENT (some parts dead-lettered by the server), when
            every result was dealt with; else TRANSIENT or TOO_LARGE. And the
            results not sent: untried, then refused, each in original order.
        """
        if len(batch) == 1:
            return Delivery.TOO_LARGE, list(batch)
        self.logger.warning(
            "Batch over the request size limit, sending it in parts",
            extra={"result_count": len(batch)},
        )
        work = deque(_halves(batch))
        untried: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        sent = 0
        stop: Delivery | None = None
        permanent = False
        while work:
            part = work.popleft()
            if stop is not None:
                untried.extend(part)
                continue
            outcome, status_code, detail = await self._post_batch(part)
            if outcome is Delivery.SENT:
                sent += len(part)
            elif outcome is Delivery.PERMANENT and status_code is not None:
                await self._dead_letter_batch(part, status_code, detail, created_at)
                permanent = True
            elif outcome is Delivery.TOO_LARGE and len(part) > 1:
                work.extendleft(reversed(_halves(part)))
            elif outcome is Delivery.TOO_LARGE:
                refused.extend(part)
                if len(refused) >= 2 and not sent:
                    stop = Delivery.TOO_LARGE
            else:
                untried.extend(part)
                stop = Delivery.TRANSIENT
        unsent = untried + refused
        if stop is Delivery.TRANSIENT:
            return Delivery.TRANSIENT, unsent
        if unsent:
            return Delivery.TOO_LARGE, unsent
        return (Delivery.PERMANENT if permanent else Delivery.SENT), unsent

    async def _post_batch(
        self, batch: list[dict[str, Any]]
    ) -> tuple[Delivery, int | None, str]:
        """POST a batch, retrying transient failures up to `max_retries`.

        A permanent refusal or a 413 returns at once: resending the same
        body cannot succeed.

        Returns:
            The outcome, the last HTTP status (None for a network error), and
            the server's error detail, scrubbed and truncated.
        """
        agent_id = self.config.get("agent_id", "unknown")
        payload = {
            "agent_id": str(agent_id) if agent_id != "unknown" else "unknown",
            "timestamp": time.time(),
            "checks": batch,
        }

        self.logger.info(
            "Attempting to send batch",
            extra={"batch_size": len(batch), "reports_url": self.reports_url},
        )

        status_code: int | None = None
        detail = ""
        for attempt in range(self.max_retries):
            if self._stopped():
                break
            if attempt > 0:
                self.logger.info(
                    "Retrying batch send",
                    extra={"attempt": attempt + 1, "max_retries": self.max_retries},
                )

            try:
                if not self.client:
                    await self.start()
                assert self.client is not None
                resp = await self.client.post(self.reports_url, json=payload)
            except httpx.RequestError:
                self.logger.warning(
                    "Connection error while sending results", exc_info=True
                )
                status_code, detail = None, ""
            # swallowed-exceptions: the reporter retries and spools to disk; raising would drop results already collected
            except Exception:
                self.logger.exception("Unexpected error while sending results")
                status_code, detail = None, ""
            else:
                status_code = resp.status_code
                outcome = classify_response(status_code, resp.text)
                if outcome is Delivery.SENT:
                    self.logger.info(
                        "Successfully sent batch", extra={"batch_size": len(batch)}
                    )
                    self.last_sent_time = time.time()
                    self.metrics["batches_sent"] += 1
                    self.metrics["results_sent"] += len(batch)
                    self.metrics["last_batch_size"] = len(batch)
                    return Delivery.SENT, status_code, ""

                detail = error_detail(resp.text)
                if outcome is Delivery.TOO_LARGE:
                    return outcome, status_code, detail
                self.metrics["send_failures"] += 1
                if outcome is Delivery.PERMANENT:
                    return outcome, status_code, detail
                self.logger.warning(
                    "Failed to send results",
                    extra={"status_code": status_code, "response_text": detail},
                )
                if self._stopped():
                    break
                await asyncio.sleep(self.retry_delay)
                continue

            self.metrics["send_failures"] += 1
            if self._stopped():
                break
            await asyncio.sleep(self.retry_delay)

        return Delivery.TRANSIENT, status_code, detail

    def _dead_letter_batch_sync(
        self,
        batch: list[dict[str, Any]],
        status_code: int,
        detail: str,
        created_at: float | None = None,
    ) -> None:
        """Insert a refused batch into dead_letters and enforce the cap (runs in thread).

        `created_at` is when the batch was first stored; a fresh one is now.
        """
        now = time.time()
        conn = self._get_db()
        try:
            insert_dead_letter(
                conn,
                created_at=now if created_at is None else created_at,
                failed_at=now,
                status_code=status_code,
                detail=detail,
                batch_size=len(batch),
                payload=gzip.compress(json.dumps(batch).encode()),
            )
            self._prune_dead_letters(conn)
            conn.commit()
        finally:
            conn.close()

    def _prune_dead_letters(self, conn: sqlite3.Connection) -> None:
        """Hold dead_letters to `report_max_dead_letters`, oldest pruned first."""
        pruned = prune_dead_letters(conn, self.max_dead_letters)
        if pruned:
            self.logger.warning(
                "Pruned oldest dead-lettered batches",
                extra={"pruned_count": pruned, "cap": self.max_dead_letters},
            )

    async def _dead_letter_batch(
        self,
        batch: list[dict[str, Any]],
        status_code: int,
        detail: str,
        created_at: float | None = None,
    ) -> None:
        """Dead-letter a batch the server refused for good, loudly.

        `created_at` is when the batch was first stored; a fresh one is now.
        Without local storage the batch is only logged: it is never retried.
        """
        self.logger.error(
            "Server refused batch permanently, dead-lettered",
            extra={
                "status_code": status_code,
                "result_count": len(batch),
                "error_detail": detail,
            },
        )
        if not self.enable_local_storage:
            return
        # swallowed-exceptions: the batch was already refused; raising here would take down the reporter loop and every batch after it
        try:
            await asyncio.to_thread(
                self._dead_letter_batch_sync, batch, status_code, detail, created_at
            )
        except Exception:
            self.logger.exception("Failed to store dead-lettered batch")

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

        # swallowed-exceptions: this IS the last resort — the server was already
        # unreachable, which is why the batch is being spooled. Raising here
        # would take down the reporter loop and lose every subsequent batch too,
        # so the results are dropped, loudly, and the agent keeps reporting.
        try:
            await asyncio.to_thread(self._store_batch_to_db_sync, batch)
        except Exception:
            self.logger.exception("Failed to store results to SQLite")

    def _process_stored_reports_sync(self) -> list[StoredBatch]:
        """Fetch pending batches from SQLite for replay (runs in thread).

        Returns:
            The oldest pending batches, ready to send.
        """
        conn = self._get_db()
        try:
            rows = self._read_pending(conn)
            if not rows and self._size_held:
                # Nothing left but rows a size refusal held: retry those.
                self._size_held.clear()
                rows = self._read_pending(conn)

            results = []
            for row_id, created_at, payload in rows:
                try:
                    checks = json.loads(gzip.decompress(payload))
                    results.append(StoredBatch(row_id, created_at, checks))
                # swallowed-exceptions: one corrupt row must not wedge replay of every row behind it
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

    def _read_pending(self, conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
        """The oldest pending rows not held by a size refusal."""
        return conn.execute(
            "SELECT id, created_at, payload FROM pending_batches "
            "WHERE id NOT IN (SELECT value FROM json_each(?)) "
            "ORDER BY created_at ASC LIMIT ?",
            (json.dumps(sorted(self._size_held)), self.max_files_per_batch),
        ).fetchall()

    def _rewrite_stored_batch_sync(
        self, row_id: int, checks: list[dict[str, Any]]
    ) -> None:
        """Replace a pending row's results with what is left of them, in place:
        same row, same created_at, so the row count never rises and the
        stored-batch cap never prunes another row for it (runs in thread)."""
        conn = self._get_db()
        try:
            conn.execute(
                "UPDATE pending_batches SET batch_size = ?, payload = ? WHERE id = ?",
                (len(checks), gzip.compress(json.dumps(checks).encode()), row_id),
            )
            conn.commit()
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

    def _move_to_dead_letters_sync(
        self, row_id: int, status_code: int, detail: str
    ) -> None:
        """Move a pending batch to dead_letters in one transaction (runs in thread).

        The row keeps its original created_at and its compressed payload as is.
        """
        conn = self._get_db()
        try:
            row = conn.execute(
                "SELECT created_at, batch_size, payload FROM pending_batches "
                "WHERE id = ?",
                (row_id,),
            ).fetchone()
            if row is None:
                return
            created_at, batch_size, payload = row
            insert_dead_letter(
                conn,
                created_at=created_at,
                failed_at=time.time(),
                status_code=status_code,
                detail=detail,
                batch_size=batch_size,
                payload=payload,
            )
            conn.execute("DELETE FROM pending_batches WHERE id = ?", (row_id,))
            self._prune_dead_letters(conn)
            conn.commit()
        finally:
            conn.close()

    async def _replay_group(self, group: list[StoredBatch]) -> bool:
        """Replay stored batches as one combined POST.

        On a permanent refusal one of them is bad, so each is retried alone:
        those the server takes are deleted, those it refuses for good move to
        dead_letters. A combined POST over the request size limit (413) is
        retried batch by batch too, and a single stored batch over it is sent
        in halves. Replay never stops at a refused or oversized batch.

        Returns:
            False when a transient failure should end this pass (the batches
            not yet sent stay pending), True otherwise.
        """
        combined = [check for stored in group for check in stored.checks]
        outcome, status_code, detail = await self._post_batch(combined)

        if outcome is Delivery.SENT:
            for stored in group:
                await asyncio.to_thread(self._delete_stored_batch_sync, stored.row_id)
            self.logger.info(
                "Drained stored batches in one POST",
                extra={"batch_count": len(group), "result_count": len(combined)},
            )
            return True

        if outcome is Delivery.TOO_LARGE and len(group) == 1:
            [stored] = group
            got, unsent = await self._send_in_parts(
                stored.checks, created_at=stored.created_at
            )
            if not unsent:
                await asyncio.to_thread(self._delete_stored_batch_sync, stored.row_id)
            elif unsent != stored.checks:
                # Fewer results, or the refused ones moved behind the untried.
                await asyncio.to_thread(
                    self._rewrite_stored_batch_sync, stored.row_id, unsent
                )
            if got is Delivery.TOO_LARGE:
                # Held: later reads go past it, so it never blocks the rows
                # behind it, and the pass goes on.
                self._size_held.add(stored.row_id)
            return got is not Delivery.TRANSIENT

        if outcome is Delivery.TRANSIENT or status_code is None:
            self.logger.warning(
                "Failed replay POST",
                extra={"batch_count": len(group), "result_count": len(combined)},
            )
            return False

        if len(group) > 1:
            self.logger.warning(
                "Replay POST refused, retrying its batches one by one",
                extra={"batch_count": len(group), "status_code": status_code},
            )
            for stored in group:
                if self._stopped():
                    return False
                if not await self._replay_group([stored]):
                    return False
            return True

        [stored] = group
        self.logger.error(
            "Server refused stored batch permanently, dead-lettered",
            extra={
                "status_code": status_code,
                "result_count": len(stored.checks),
                "error_detail": detail,
            },
        )
        await asyncio.to_thread(
            self._move_to_dead_letters_sync, stored.row_id, status_code, detail
        )
        return True

    async def _replay_pending(self, pending: list[StoredBatch]) -> None:
        """Replay one fetch of stored batches, combined up to `batch_size` per POST."""
        group: list[StoredBatch] = []
        size = 0
        for stored in pending:
            if self._stopped():
                return
            # Send before adding if appending would exceed the cap.
            if group and size + len(stored.checks) > self.batch_size:
                if not await self._replay_group(group):
                    return
                group, size = [], 0
            group.append(stored)
            size += len(stored.checks)

        if group and not self._stopped():
            await self._replay_group(group)

    async def process_stored_reports(self) -> None:
        """Process stored reports in the background."""
        self.logger.info(
            "Starting stored reports processor",
            extra={"interval_seconds": self.process_interval},
        )

        while self.running:
            try:
                pending = await asyncio.to_thread(self._process_stored_reports_sync)

                if pending and self.running:
                    self.logger.info(
                        "Found stored batches to process",
                        extra={"pending_count": len(pending)},
                    )
                    await self._replay_pending(pending)

                # Check if we should continue running
                if self._stopped():
                    break

                # Wait before checking again, with periodic checks of running status
                for _ in range(self.process_interval):
                    if self._stopped():
                        break
                    await asyncio.sleep(1)

            # swallowed-exceptions: the replay loop must outlive one bad pass, or stored batches never drain
            except Exception:
                self.logger.exception("Error in stored reports processor")

                # Check if we should continue running
                if self._stopped():
                    break

                # Shorter sleep on error
                await asyncio.sleep(5)

        self.logger.info("Stored reports processor stopped")

    def _get_metrics_sync(self) -> tuple[int, float | None, int]:
        """Get stored report metrics from SQLite (runs in thread).

        Returns:
            Tuple of (pending count, oldest pending timestamp, dead-letter count).
        """
        conn = self._get_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*), MIN(created_at) FROM pending_batches"
            ).fetchone()
            return (row[0], row[1], count_dead_letters(conn))
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
                count, oldest, dead = self._get_metrics_sync()
                metrics["stored_reports_count"] = count
                metrics["stored_reports_oldest_timestamp"] = oldest or 0.0
                metrics["dead_letter_count"] = dead
            # swallowed-exceptions: metrics are best-effort; a locked database must not break the heartbeat
            except Exception:
                self.logger.warning(
                    "Failed to get stored reports metrics", exc_info=True
                )
                metrics["stored_reports_count"] = 0
                metrics["stored_reports_oldest_timestamp"] = 0.0
                metrics["dead_letter_count"] = 0
        else:
            metrics["stored_reports_count"] = 0
            metrics["stored_reports_oldest_timestamp"] = 0.0
            metrics["dead_letter_count"] = 0

        return metrics


# Simplified function for backward compatibility
def _halves(batch: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """`batch` cut in two, in order."""
    mid = len(batch) // 2
    return [batch[:mid], batch[mid:]]


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
