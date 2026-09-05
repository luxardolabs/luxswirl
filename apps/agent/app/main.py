"""
Entrypoint for the LuxSwirl Agent (`python -m app.main`).

Luxardo Labs LuxSwirl - A distributed health monitoring system.
"""

import argparse
import asyncio
import contextlib
import logging
import os
import re
import signal
import socket
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from shared.config import get_config, load_config_file
from shared.jobs.network_discover import NetworkDiscoverJob
from shared.jobs.network_scan import NetworkScanJob
from shared.logger import get_logger

from app.agent.core import LuxSwirlAgent
from app.checks.dns import DNSCheck
from app.checks.http import HTTPCheck
from app.checks.json import JSONCheck
from app.checks.mysql import MySQLCheck
from app.checks.ping import PingCheck
from app.checks.postgres import PostgreSQLCheck
from app.checks.synthetic import SyntheticCheck
from app.checks.tcp import TCPCheck


def _resolve_version() -> str:
    """Resolve the agent version from the single source of truth.

    The repo-root VERSION file is the only version literal (repo.version_single_source):
    it is baked into the image as APP_VERSION at build time, and is readable from the
    installed distribution metadata otherwise. Never a hardcoded literal, which drifts.
    """
    stamped = os.environ.get("APP_VERSION")
    if stamped:
        return stamped
    try:
        return package_version("luxswirl-agent")
    except PackageNotFoundError:
        return "dev"


__version__ = _resolve_version()


async def main():
    """Main entry point for the LuxSwirl Agent application."""
    # Set up argument parser
    parser = argparse.ArgumentParser(description="Luxardo Labs LuxSwirl Agent")
    parser.add_argument("-c", "--config", help="Path to config file (JSON or YAML)")
    parser.add_argument(
        "-v", "--version", action="store_true", help="Show version and exit"
    )
    parser.add_argument(
        "--validate-only", action="store_true", help="Validate config and exit"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    # Get logger
    logger = get_logger("luxswirl")

    # Set debug level if requested
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled")

    # Check for random Docker hostname (critical for credential encryption)
    hostname = socket.gethostname()
    # Docker generates random 12-character hex hostnames like "abf8f0f9deb7"
    if re.match(r"^[0-9a-f]{12}$", hostname):
        logger.warning("")
        logger.warning("=" * 80)
        logger.warning("⚠️  CRITICAL: Random Docker hostname detected!")
        logger.warning("Current hostname", extra={"hostname": hostname})
        logger.warning("")
        logger.warning(
            "   This will cause credential encryption to BREAK on container restart!"
        )
        logger.warning("")
        logger.warning("   FIX: Add 'hostname: luxswirl_agent' to your compose.yaml:")
        logger.warning("   services:")
        logger.warning("     luxswirl_agent:")
        logger.warning("       hostname: luxswirl_agent  # <-- Add this line")
        logger.warning("")
        logger.warning("   Without this fix:")
        logger.warning("   - Each restart generates a NEW random hostname")
        logger.warning("   - Credentials encrypted with OLD hostname cannot decrypt")
        logger.warning("   - Agent will re-register and lose its identity")
        logger.warning("=" * 80)
        logger.warning("")
    else:
        logger.info("Hostname configured correctly", extra={"hostname": hostname})

    # Show version and exit if requested
    if args.version:
        print(f"Luxardo Labs LuxSwirl Agent v{__version__}")
        return 0

    # Create agent variable in outer scope for shutdown handling
    agent = None

    try:
        # Determine which configuration to use
        if args.config:
            # Load from specified file
            config_path = Path(args.config)
            if not config_path.exists():
                logger.error(
                    "Config file not found",
                    extra={"config_path": str(config_path)},
                )
                return 1

            logger.info(
                "Loading configuration",
                extra={"config_path": str(config_path)},
            )
            config = load_config_file(config_path)
        else:
            # Use default configuration
            logger.info("Using default configuration")
            config = get_config("agent")

            # If in validate only mode, exit after validation
            if args.validate_only:
                logger.info("Configuration validated successfully")
                return 0

        # Create agent instance
        agent = LuxSwirlAgent(config)

        # Register check types
        agent.register_check_type("dns", DNSCheck)
        agent.register_check_type("http", HTTPCheck)
        agent.register_check_type("mysql", MySQLCheck)
        agent.register_check_type("ping", PingCheck)
        agent.register_check_type("postgres", PostgreSQLCheck)
        agent.register_check_type("tcp", TCPCheck)
        agent.register_check_type("json", JSONCheck)
        agent.register_check_type("synthetic", SyntheticCheck)

        # Register job types
        agent.register_job_type(NetworkScanJob)
        agent.register_job_type(NetworkDiscoverJob)

        # Run the agent
        logger.info(
            "Starting LuxSwirl Agent",
            extra={"version": __version__},
        )
        _install_signal_handlers(agent, logger)
        await agent.run()

    except Exception:
        logger.error("Unhandled exception", exc_info=True)
        if agent:
            try:
                await agent.shutdown()
            except Exception:
                pass
        return 1

    return 0


def _install_signal_handlers(agent: LuxSwirlAgent, logger: Any) -> None:
    """Install SIGTERM/SIGINT on the RUNNING loop so shutdown drains cleanly.

    `signal.signal` raising KeyboardInterrupt from a sync handler interrupts asyncio at
    an arbitrary await point, so in-flight checks and queued results are lost rather than
    drained. `loop.add_signal_handler` schedules the shutdown ON the loop: it clears the
    agent's `running` flag, the main loop exits within one tick, and the reporter flushes.
    Windows' proactor loop implements no add_signal_handler, hence the suppress.
    """
    loop = asyncio.get_running_loop()
    # Hold a strong reference: a bare create_task() is only weakly referenced by the loop,
    # so the shutdown task can be garbage-collected mid-drain.
    shutdown_tasks: set[asyncio.Task[None]] = set()

    def request_shutdown(signame: str) -> None:
        logger.info("Shutdown signal received, draining", extra={"signal": signame})
        task = loop.create_task(agent.shutdown())
        shutdown_tasks.add(task)
        task.add_done_callback(shutdown_tasks.discard)

    for sig in (signal.SIGTERM, signal.SIGINT):
        # add_signal_handler is unavailable on some platforms (e.g. Windows)
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, request_shutdown, sig.name)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
