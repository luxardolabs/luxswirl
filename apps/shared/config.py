"""
Configuration module for the LuxSwirl application.

Provides both hardcoded default configurations and methods to load
configuration from files.
"""

import json
import os
import socket
from pathlib import Path
from typing import Any, cast

import yaml

from shared.url_security import validate_server_url

# Default agent configuration
DEFAULT_AGENT_CONFIG = {
    "agent_name": os.environ.get("LUXSWIRL_AGENT_ID", f"{socket.gethostname()}-agent"),
    "auth_key": os.environ.get("LUXSWIRL_AUTH_KEY", ""),
    # Server URL for check results submission
    # Format: http(s)://host:port/api/v1/reports
    # This URL is used as the base for both check results AND heartbeat endpoints:
    #   - Check results: {push_url} (POST)
    #   - Heartbeats: {base}/api/v1/agents/{agent_name}/heartbeat (POST)
    # Default uses luxswirl_server which is the service name in docker-compose
    "push_url": os.environ.get("LUXSWIRL_SERVER_URL", "http://luxswirl_server:9000/api/v1/reports"),
    # Reporting configuration
    "report_interval": 10,
    "report_batch_size": 5000,
    "report_max_retries": 3,
    "report_retry_delay": 2,
    "report_batch_timeout": 10,
    "report_max_queue_size": 5000,  # Maximum number of results to keep in memory
    "report_backpressure_threshold": 0.8,  # Apply backpressure when queue is 80% full
    "enable_local_storage": True,
    "report_storage_path": "reports",
    "report_process_interval": 10,  # Seconds between checking stored reports
    "report_max_files_per_batch": 5,  # Max files to process at once
    "report_max_stored_batches": 10000,  # Max batches to keep in SQLite before pruning oldest
    # Metrics and TTL configuration
    "metrics_ttl_seconds": 300,  # How long to keep check results in metrics (5 minutes)
    "metrics_cleanup_interval": 60,  # How often to cleanup stale metrics (1 minute)
    "metrics_include_stale": True,  # Include stale checks once with up=0 before removal
    # Agent performance tuning
    "max_concurrent_checks": 200,
    "result_queue_timeout": 1.0,  # Timeout when getting results from queue
    "result_processor_retry_delay": 1.0,  # Sleep time on errors in result processor
    "shutdown_timeout": 5.0,  # Timeout for tasks during shutdown
    "main_loop_sleep": 0.1,  # Sleep time in main agent loop
    "heartbeat_interval": 60,  # Interval for logging agent heartbeat
    "watchdog_enabled": True,  # Enable watchdog for monitoring processor health
    "watchdog_interval": 30,  # Interval for watchdog checks
    "watchdog_stall_threshold": 3,  # Number of intervals before forcing action
    # Global check settings
    "interval": 60,  # Default check interval in seconds
    "enable_self_monitoring": True,
    # Subprocess execution safety (SWIRL-57: prevent resource leaks)
    "subprocess_timeout_grace_seconds": 2,  # Extra time beyond check timeout before killing subprocess
    "subprocess_kill_timeout_seconds": 5,  # Max time to wait for process termination after kill
    "subprocess_command_timeout_seconds": 5,  # Timeout for system commands (ip, route, ping-discover)
    # Resource monitoring (SWIRL-57: detect file descriptor/subprocess leaks)
    "resource_monitoring_enabled": True,  # Enable FD/subprocess tracking
    "resource_check_interval_seconds": 30,  # How often to check resource usage
    "resource_fd_warning_percent": 80,  # Warn when FD usage exceeds this % of ulimit
    "resource_subprocess_warning_count": 50,  # Warn when subprocess count exceeds this
    # Logging
    "log_level": os.environ.get("LOG_LEVEL", "DEBUG"),
    "enable_file_log": True,
    "log_dir": "logs",
    # Checks are now dynamically managed via the server API
    "checks": [],
}

# Default server configuration
DEFAULT_SERVER_CONFIG = {
    "host": "0.0.0.0",
    "port": 9000,
    "log_level": os.environ.get("LOG_LEVEL", "INFO"),
    "auth_tokens": [os.environ.get("LUXSWIRL_AUTH_KEY", "")],
    "max_history_points": 1000,
    "enable_file_log": True,
    "log_dir": "logs",
    # Database configuration
    "database_url": os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://luxswirl:luxswirl@localhost:5432/luxswirl"
    ),
    "database_echo": os.environ.get("DATABASE_ECHO", "false").lower() == "true",
    # Query time windows
    "agent_active_window_minutes": 10,  # Consider agent active if seen in last 10 min
    "latest_results_window_minutes": 5,  # Show latest results from last 5 min
    "metrics_ttl_seconds": 300,  # Metrics stale after 5 min
    # Job defaults
    "job_defaults": {
        "network_scan": {
            "timeout": 10,  # Total timeout per host in seconds
            "max_concurrent": 100,  # Number of parallel host scans
            "ports": [22, 80, 443, 3306, 5432, 8080, 8443],  # Default ports to scan
        },
        "network_discover": {
            "timeout": 300,  # 5 minute timeout for discovery
        },
    },
}


def get_config(component: str = "agent") -> dict[str, Any]:
    """Get the default configuration for the specified component.

    Args:
        component: Which component's config to return ('agent' or 'server')

    Returns:
        Configuration dictionary

    Raises:
        ValueError: If the component name is unknown
    """
    if component.lower() == "agent":
        return DEFAULT_AGENT_CONFIG.copy()
    elif component.lower() == "server":
        return DEFAULT_SERVER_CONFIG.copy()
    else:
        raise ValueError(f"Unknown component: {component}")


def load_config_file(path: str | Path) -> dict[str, Any]:
    """Load configuration from a YAML or JSON file.

    Args:
        path: Path to the configuration file

    Returns:
        The loaded configuration as a dictionary

    Raises:
        FileNotFoundError: If the config file does not exist
        ValueError: If the file format is not supported
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    # Determine file type from extension
    ext = path.suffix.lower()

    # Load based on file extension
    if ext == ".yaml" or ext == ".yml":
        with open(path) as f:
            config = yaml.safe_load(f)
    elif ext == ".json":
        with open(path) as f:
            config = json.load(f)
    else:
        raise ValueError(f"Unsupported config file format: {ext}")

    # Expand environment variables in string values
    config = _expand_env_vars(config)

    return cast(dict[str, Any], config)


def _expand_env_vars(config_item: Any) -> Any:
    """Recursively expand environment variables in string values.

    Environment variables should be in the format ${VAR_NAME} or $VAR_NAME.

    Args:
        config_item: A configuration item (string, list, dict, etc.)

    Returns:
        The configuration with environment variables expanded
    """
    if isinstance(config_item, str):
        # Replace ${VAR} or $VAR with environment variable
        import re

        # First, handle ${VAR} format
        pattern = r"\${([a-zA-Z0-9_]+)}"
        matches = re.findall(pattern, config_item)
        result = config_item
        for var_name in matches:
            env_value = os.environ.get(var_name, "")
            result = result.replace(f"${{{var_name}}}", env_value)

        # Then, handle $VAR format
        pattern = r"\$([a-zA-Z0-9_]+)"
        matches = re.findall(pattern, result)
        for var_name in matches:
            env_value = os.environ.get(var_name, "")
            result = result.replace(f"${var_name}", env_value)

        return result
    elif isinstance(config_item, dict):
        return {k: _expand_env_vars(v) for k, v in config_item.items()}
    elif isinstance(config_item, list):
        return [_expand_env_vars(item) for item in config_item]
    else:
        return config_item


def validate_config(config: dict[str, Any], component: str = "agent") -> bool:
    """Validate the configuration for the specified component.

    Args:
        config: Configuration dictionary to validate
        component: Which component's config to validate ('agent' or 'server')

    Returns:
        True if the configuration is valid, False otherwise
    """
    try:
        if component.lower() == "agent":
            _validate_agent_config(config)
        elif component.lower() == "server":
            _validate_server_config(config)
        else:
            raise ValueError(f"Unknown component: {component}")

        return True
    except ValueError:
        from shared.logger import get_logger

        logger = get_logger("luxswirl.config")
        logger.error("Configuration validation error", exc_info=True)
        return False


def _validate_agent_config(config: dict[str, Any]) -> None:
    """Validate the agent configuration.

    Args:
        config: Configuration dictionary to validate

    Raises:
        ValueError: If the configuration is invalid
    """
    required_fields = ["agent_name", "push_url", "checks"]
    for field in required_fields:
        if field not in config:
            raise ValueError(f"Missing required field: {field}")

    # Validate push_url security
    validate_server_url(config["push_url"])

    # Validate checks
    if not isinstance(config["checks"], list):
        raise ValueError("'checks' must be a list")

    # Check for duplicate names
    check_names = [check.get("name") for check in config["checks"]]
    duplicates = {name for name in check_names if check_names.count(name) > 1}
    if duplicates:
        raise ValueError(f"Duplicate check names: {', '.join(duplicates)}")

    # Validate individual checks
    for check in config["checks"]:
        _validate_check_config(check)


def _validate_check_config(check: dict[str, Any]) -> None:
    """Validate a single check configuration.

    Args:
        check: Check configuration dictionary to validate

    Raises:
        ValueError: If the check configuration is invalid
    """
    required_fields = ["name", "check_type", "target"]
    for field in required_fields:
        if field not in check:
            raise ValueError(f"Missing required field in check: {field}")

    check_type = check["check_type"]

    # Type-specific validation
    if check_type == "http":
        if not check["target"].startswith(("http://", "https://")):
            raise ValueError(
                f"HTTP check target must start with http:// or https://: {check['target']}"
            )

    elif check_type == "tcp":
        if "port" not in check:
            raise ValueError(f"TCP check {check['name']} must have a 'port'")

        port = check["port"]
        if not isinstance(port, int) or port < 1 or port > 65535:
            raise ValueError(f"TCP port must be an integer between 1 and 65535: {port}")


def _validate_server_config(config: dict[str, Any]) -> None:
    """Validate the server configuration.

    Args:
        config: Configuration dictionary to validate

    Raises:
        ValueError: If the configuration is invalid
    """
    required_fields = ["host", "port", "auth_tokens"]
    for field in required_fields:
        if field not in config:
            raise ValueError(f"Missing required field: {field}")

    # Validate port
    port = config["port"]
    if not isinstance(port, int) or port < 1 or port > 65535:
        raise ValueError(f"Port must be an integer between 1 and 65535: {port}")

    # Validate auth tokens
    if not isinstance(config["auth_tokens"], list) or not config["auth_tokens"]:
        raise ValueError("'auth_tokens' must be a non-empty list")
