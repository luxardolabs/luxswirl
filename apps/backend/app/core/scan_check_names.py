"""The check names a network scan would create, and the ports that drive them.

The scan detail panel needs to answer "is this host already fully monitored?",
which means deriving the same names the create-checks path derives. That
derivation lived in three places — the creation service, the categorisation
service, and a 25-line block repeated twice inside a template. It lives here.
"""

from typing import Any

# Port → check-type prefix. Kept together so a port added for creation is also a
# port the panel counts as monitored.
PORT_CATEGORIES: dict[str, list[int]] = {
    "web": [80, 443, 8080, 8443, 8000, 3000, 5000],
    "ssh": [22, 2222],
    "database": [3306, 5432, 5433, 27017, 6379, 1433, 1521],
}

# The name prefix each category's checks are created under.
CATEGORY_PREFIX: dict[str, str] = {
    "web": "http",
    "ssh": "ssh",
    "database": "db",
}


def check_name(prefix: str, host: str, port: int | None = None) -> str:
    """The display name a generated check gets.

    Args:
        prefix: The check-type prefix ("ping", "http", "ssh", "db").
        host: Hostname when the scan resolved one, else the IP.
        port: The port, for the per-service checks.

    Returns:
        The name, with dots replaced so it reads as one token.
    """
    suffix = f"_{port}" if port is not None else ""
    return f"{prefix}_{host}{suffix}".replace(".", "_")


def expected_check_names(host: dict[str, Any]) -> list[str]:
    """Every check name a scan would create for one discovered host.

    Args:
        host: A discovered host, with `ip`, optional `hostname` and `open_ports`.

    Returns:
        The ping check plus one per recognised open port.
    """
    target = host.get("hostname") or host["ip"]
    names = [check_name("ping", target)]
    for port in host.get("open_ports") or []:
        for category, ports in PORT_CATEGORIES.items():
            if port in ports:
                names.append(check_name(CATEGORY_PREFIX[category], target, port))
    return names
