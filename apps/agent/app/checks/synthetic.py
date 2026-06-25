"""
Synthetic Check Module - Implements Playwright-based synthetic monitoring.

This check type executes user-provided Python scripts using Playwright
for browser automation. It captures screenshots, traces, and browser metrics.
"""

import asyncio
import time
import traceback
from typing import Any

from playwright.async_api import async_playwright

from app.checks.base import BaseCheck


class SyntheticCheck(BaseCheck):
    """Synthetic monitoring check using Playwright browser automation."""

    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate synthetic check configuration.

        Args:
            config: The check configuration to validate

        Raises:
            ValueError: If required configuration fields are missing or invalid
        """
        super().validate_config(config)

        # Synthetic checks require script_code
        if "script_code" not in config or not config["script_code"]:
            raise ValueError(
                f"Synthetic check {config.get('name', 'unknown')} must have 'script_code'"
            )

    async def run(self) -> dict[str, Any]:
        """Execute the synthetic check.

        Returns:
            A dictionary containing the check result with artifacts
        """
        cfg = self.config
        script_code = cfg["script_code"]
        timeout = cfg.get("timeout", 30)  # Default 30s for synthetic checks
        headless = cfg.get("headless", True)

        result_data: dict[str, Any] = {
            "status": "unknown",
            "steps": [],
            "errors": [],
            "console_errors": [],
            "request_failures": [],
            "browser_timing": {},
            "artifacts": [],  # Will contain screenshot and trace data
        }

        start_time = self.start_timer()
        screenshot_bytes: bytes | None = None
        trace_bytes: bytes | None = None

        try:
            async with async_playwright() as p:
                # Launch browser (use defaults like in standalone synthetic)
                browser = await p.chromium.launch(headless=headless)
                context = await browser.new_context()
                page = await context.new_page()

                # Capture console errors
                page.on(
                    "console",
                    lambda msg: (
                        result_data["console_errors"].append(msg.text)
                        if msg.type == "error"
                        else None
                    ),
                )

                # Capture request failures
                page.on(
                    "requestfailed",
                    lambda req: result_data["request_failures"].append(
                        {"url": req.url, "failure": req.failure}
                    ),
                )

                try:
                    # Start tracing with screenshots and snapshots
                    await context.tracing.start(screenshots=True, snapshots=True)

                    # Execute the user's script with timeout (time only the script, not Playwright overhead)
                    script_start = self.start_timer()
                    check_result = await asyncio.wait_for(
                        self._execute_user_script(script_code, page), timeout=timeout
                    )
                    script_execution_time_ms = self.stop_timer(script_start)

                    # Extract results from user script
                    result_data["status"] = check_result.get("status", "success")
                    result_data["steps"] = check_result.get("steps", [])
                    result_data["errors"].extend(check_result.get("errors", []))
                    result_data["script_execution_time_ms"] = script_execution_time_ms

                    # Add any custom fields from user script
                    for key, value in check_result.items():
                        if key not in ["status", "steps", "errors"]:
                            result_data[key] = value

                except TimeoutError:
                    result_data["status"] = "failure"
                    result_data["errors"].append(f"Script execution timed out after {timeout}s")
                except Exception as e:
                    result_data["status"] = "failure"
                    result_data["errors"].append(f"Script execution failed: {str(e)}")
                    result_data["errors"].append(traceback.format_exc())

                finally:
                    # Capture browser timing metrics
                    try:
                        result_data["browser_timing"] = await page.evaluate(
                            "() => JSON.parse(JSON.stringify(window.performance.timing))"
                        )
                    except Exception:
                        pass

                    # Capture screenshot
                    try:
                        screenshot_bytes = await page.screenshot(type="png", full_page=True)
                        result_data["artifacts"].append(
                            {
                                "type": "screenshot",
                                "content_type": "image/png",
                                "filename": f"{self.name}_screenshot.png",
                                "data": screenshot_bytes,
                                "size_bytes": len(screenshot_bytes),
                            }
                        )
                    except Exception as e:
                        result_data["errors"].append(f"Failed to capture screenshot: {str(e)}")

                    # Capture trace
                    try:
                        # Stop tracing and get bytes
                        trace_path = f"/tmp/trace_{self.check_id}_{int(time.time())}.zip"
                        await context.tracing.stop(path=trace_path)

                        # Read the trace file
                        with open(trace_path, "rb") as f:
                            trace_bytes = f.read()

                        result_data["artifacts"].append(
                            {
                                "type": "trace",
                                "content_type": "application/zip",
                                "filename": f"{self.name}_trace.zip",
                                "data": trace_bytes,
                                "size_bytes": len(trace_bytes),
                            }
                        )

                        # Clean up trace file
                        import os

                        os.remove(trace_path)
                    except Exception as e:
                        result_data["errors"].append(f"Failed to capture trace: {str(e)}")

                    # Close browser
                    await browser.close()

        except Exception as e:
            result_data["status"] = "failure"
            result_data["errors"].append(f"Playwright initialization failed: {str(e)}")
            result_data["errors"].append(traceback.format_exc())

        # Calculate total execution time (Playwright overhead + page load)
        total_execution_time = self.stop_timer(start_time)

        # Determine success based on status
        success = result_data["status"] == "success"

        # Create error message if failed
        error_message = None
        if not success and result_data["errors"]:
            error_message = "; ".join(result_data["errors"][:3])  # First 3 errors

        # Extract artifacts for separate upload (remove from result_data for metrics)
        artifacts = result_data.pop("artifacts", [])

        # For synthetic checks, use script execution time as primary metric
        # This measures the actual test duration (first page to last page)
        # Excludes Playwright overhead (browser launch, screenshot, trace, shutdown)
        script_execution_ms = result_data.get("script_execution_time_ms")

        # Capture browser timing for reference (first page load only)
        browser_latency_ms = None
        browser_timing = result_data.get("browser_timing")
        if browser_timing and isinstance(browser_timing, dict):
            try:
                load_end = browser_timing.get("loadEventEnd")
                nav_start = browser_timing.get("navigationStart")
                if load_end and nav_start:
                    browser_latency_ms = load_end - nav_start
            except Exception:
                pass

        # Primary metric: script execution time (what users care about)
        latency_ms = script_execution_ms if script_execution_ms else total_execution_time

        # Store all timing metrics for reference in UI
        result_data["total_execution_time_ms"] = total_execution_time
        result_data["browser_load_time_ms"] = browser_latency_ms

        # Return standardized result
        return self.create_result(
            success=success,
            latency_ms=latency_ms,
            error=error_message,
            synthetic_status=result_data["status"],
            steps=result_data["steps"],
            console_errors=result_data["console_errors"],
            request_failures=result_data["request_failures"],
            browser_timing=result_data["browser_timing"],
            artifacts=artifacts,  # Binary artifacts for upload (top-level only)
            metrics={"synthetic": result_data},  # result_data no longer has artifacts
        )

    async def _execute_user_script(self, script_code: str, page) -> dict[str, Any]:
        """Execute user-provided script code in a controlled environment.

        Args:
            script_code: Python code to execute
            page: Playwright Page object

        Returns:
            Dictionary with check results from user script

        The user script must define an async function called `run_check(page)`
        that accepts a Playwright Page object and returns a dict with:
            - status: "success" or "failure"
            - steps: List of step descriptions (optional)
            - errors: List of error messages (optional)
            - Any other custom fields
        """
        # Create a restricted globals dict for user script (matching standalone synthetic)
        script_globals = {
            "__builtins__": __builtins__,
            "page": page,
            "time": time,
            # Add safe modules user might need
            "re": __import__("re"),
        }

        # Execute the user script to define run_check function
        try:
            exec(script_code, script_globals)
        except Exception as e:
            return {
                "status": "failure",
                "steps": [],
                "errors": [
                    f"Script compilation failed: {str(e)}",
                    traceback.format_exc(),
                ],
            }

        # Check if run_check function exists
        if "run_check" not in script_globals:
            return {
                "status": "failure",
                "steps": [],
                "errors": ["Script must define an async function called 'run_check(page)'"],
            }

        # Execute the run_check function
        try:
            run_check_func = script_globals["run_check"]
            result = await run_check_func(page)

            # Validate result structure
            if not isinstance(result, dict):
                return {
                    "status": "failure",
                    "steps": [],
                    "errors": ["run_check() must return a dictionary"],
                }

            return result

        except Exception as e:
            return {
                "status": "failure",
                "steps": [],
                "errors": [
                    f"Script execution failed: {str(e)}",
                    traceback.format_exc(),
                ],
            }
