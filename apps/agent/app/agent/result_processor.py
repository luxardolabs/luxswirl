"""
Agent result processing and artifact management.

Handles processing check results from the queue including:
- Batching results for efficient reporting
- Uploading artifacts for synthetic checks
- Logging check results
- Coordinating result and artifact uploads
"""

import asyncio
import base64
import time
from datetime import datetime
from typing import Any

import httpx
from shared.logger import get_logger

logger = get_logger("luxswirl.agent.result_processor")


class ResultProcessor:
    """Manages processing of check results and artifact uploads."""

    def __init__(
        self,
        config: dict,
        reporter,
        result_queue: asyncio.Queue,
        result_queue_timeout: float = 1.0,
        result_processor_retry_delay: float = 1.0,
    ):
        """
        Initialize result processor.

        Args:
            config: Agent configuration
            reporter: Reporter instance for batching results
            result_queue: Queue containing check results
            result_queue_timeout: Timeout for queue.get() in seconds
            result_processor_retry_delay: Delay between retries on errors
        """
        self.config = config
        self.reporter = reporter
        self.result_queue = result_queue
        self.result_queue_timeout = result_queue_timeout
        self.result_processor_retry_delay = result_processor_retry_delay
        self.logger = logger

        # Control flag (set by parent)
        self.running = False

    async def process_results(self) -> None:
        """Process and report results from the queue."""
        self.logger.info("Starting result processor")
        last_stats_time = time.time()

        try:
            # Start the reporter
            await self.reporter.start()

            while self.running:
                try:
                    # Get a result with timeout
                    try:
                        result = await asyncio.wait_for(
                            self.result_queue.get(), timeout=self.result_queue_timeout
                        )
                    except TimeoutError:
                        # No results available, check if we should continue
                        if not self.running:
                            break

                        # Periodically log stats (DEBUG — pure heartbeat noise at INFO)
                        current_time = time.time()
                        if current_time - last_stats_time > 30:
                            self.logger.debug(
                                "Queue stats",
                                extra={
                                    "queue_size": self.result_queue.qsize(),
                                    "reporter_batch": len(self.reporter.current_batch),
                                },
                            )
                            last_stats_time = current_time

                        continue

                    # Per-result outcome — DEBUG. At hundreds of checks/min this floods INFO.
                    status = "PASS" if result["success"] else "FAIL"
                    self.logger.debug(
                        "Check result",
                        extra={
                            "check_name": result.get("display_name", "unknown"),
                            "status": status,
                            "latency_ms": result.get("latency_ms"),
                            "error": result.get("error"),
                        },
                    )

                    # For synthetic checks with artifacts, send result immediately and wait
                    # Otherwise use normal batching
                    artifacts = result.get("artifacts")
                    if artifacts:
                        # Remove artifacts from result before sending (can't serialize binary data)
                        result_without_artifacts = {
                            k: v for k, v in result.items() if k != "artifacts"
                        }
                        # Add to batch
                        await self.reporter.add_result(result_without_artifacts)
                        # Force immediate flush to ensure result is in DB before artifact upload
                        await self.reporter.flush()
                        # Small delay to ensure DB commit completes
                        await asyncio.sleep(0.5)
                        # Upload artifacts AFTER result is in database
                        await self.upload_artifacts(result)
                    else:
                        # Normal flow: just add to batch (no artifacts to wait for)
                        await self.reporter.add_result(result)

                    # Mark as done
                    self.result_queue.task_done()

                except asyncio.CancelledError:
                    break
                except Exception:
                    self.logger.error("Error processing result", exc_info=True)
                    if not self.running:
                        break
                    await asyncio.sleep(self.result_processor_retry_delay)
        except Exception:
            self.logger.error("Unhandled exception in result processor", exc_info=True)

        self.logger.info("Result processor stopped")

    async def upload_artifacts(self, result: dict[str, Any]) -> None:
        """
        Upload artifacts from synthetic checks to server.

        Args:
            result: Check result dictionary that may contain artifacts

        This method extracts artifacts from the result and uploads them
        to the server via the artifacts API endpoint.

        IMPORTANT: This method ALWAYS removes artifacts from the result,
        regardless of upload success/failure, to prevent JSON serialization errors.
        """
        # Check if result has artifacts
        artifacts = result.get("artifacts")
        if not artifacts:
            return

        # IMPORTANT: Use try/finally to ensure artifacts are ALWAYS removed
        try:
            # Get server URL and auth
            push_url = self.config.get("push_url", "http://localhost:9000")
            if not push_url:
                self.logger.warning("No push_url configured, cannot upload artifacts")
                return  # artifacts will be removed in finally block below

            # Extract base URL
            if "/api/v1" in push_url:
                base_url = push_url.split("/api/v1")[0]
            else:
                base_url = push_url.rstrip("/")

            artifact_url = f"{base_url}/api/v1/artifacts"
            auth_key = self.config.get("auth_key")

            # Prepare headers
            headers = {}
            if auth_key:
                headers["Authorization"] = f"Bearer {auth_key}"

            check_id = result.get("check_id")
            check_result_id = result.get("result_id")  # UUID generated before check execution
            timestamp: float = result.get("timestamp", time.time())
            check_result_timestamp = datetime.fromtimestamp(timestamp).isoformat()

            # Upload each artifact
            for artifact in artifacts:
                try:
                    # Base64 encode binary data for JSON transmission
                    data_base64 = base64.b64encode(artifact["data"]).decode("utf-8")

                    # Prepare artifact data
                    artifact_data = {
                        "check_id": check_id,
                        "check_result_id": check_result_id,
                        "check_result_timestamp": check_result_timestamp,
                        "artifact_type": artifact["type"],
                        "content_type": artifact["content_type"],
                        "filename": artifact["filename"],
                        "data_base64": data_base64,
                    }

                    # Upload to server
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        response = await client.post(
                            artifact_url,
                            json=artifact_data,
                            headers=headers,
                        )

                        if response.status_code == 201:
                            self.logger.info(
                                "Uploaded artifact",
                                extra={
                                    "artifact_filename": artifact["filename"],
                                    "size_bytes": artifact["size_bytes"],
                                    "artifact_type": artifact["type"],
                                },
                            )
                        else:
                            self.logger.error(
                                "Failed to upload artifact",
                                extra={
                                    "artifact_filename": artifact["filename"],
                                    "status_code": response.status_code,
                                    "response_text": response.text,
                                },
                            )

                except Exception:
                    # Log but continue with other artifacts
                    self.logger.error(
                        "Exception uploading artifact",
                        extra={
                            "artifact_filename": artifact.get("filename", "unknown"),
                        },
                        exc_info=True,
                    )
        except Exception:
            # Log outer exception but ensure artifacts are still removed
            self.logger.error("Error in artifact upload process", exc_info=True)

        finally:
            # ALWAYS remove artifacts from result to avoid sending them in the batch report
            # This must happen regardless of upload success/failure
            had_artifacts = "artifacts" in result
            result.pop("artifacts", None)
            if had_artifacts:
                self.logger.debug(
                    "Removed artifacts from result",
                    extra={"display_name": result.get("display_name", "unknown")},
                )

    def start(self) -> None:
        """Start the result processor."""
        self.running = True

    def stop(self) -> None:
        """Stop the result processor."""
        self.running = False
