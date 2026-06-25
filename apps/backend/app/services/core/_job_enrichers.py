"""
Job enrichment registry.

Maps job types to their server-side enrichment functions. Pluggable —
add a new job type by:
  1. Creating a new core service (e.g., my_job_core_service.py)
  2. Implementing enrich_result(result: dict) -> dict on it
  3. Registering it in JOB_ENRICHERS below

"""

from collections.abc import Callable
from typing import Any

from app.services.core.network_discover_core_service import NetworkDiscoverCoreService
from app.services.core.network_scan_core_service import NetworkScanCoreService

# Registry: job_type -> enrichment function
JOB_ENRICHERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "network_discover": NetworkDiscoverCoreService.enrich_result,
    "network_scan": NetworkScanCoreService.enrich_result,
    # Add more job enrichers here as you create them:
    # "port_scan": PortScanService.enrich_result,
    # "dns_lookup": DNSLookupService.enrich_result,
    # "mtr_trace": MTRTraceService.enrich_result,
}


def enrich_job_result(job_type: str, result: dict[str, Any]) -> dict[str, Any]:
    """
    Enrich a job result based on its type.

    Looks up the job type in the registry and calls the appropriate
    enrichment function. If no enricher is registered, returns result unchanged.

    Args:
        job_type: Type of job (e.g., "network_discover")
        result: Raw result from agent

    Returns:
        Enriched result with suggestions, warnings, and intelligence added
    """
    enricher = JOB_ENRICHERS.get(job_type)

    if enricher:
        return enricher(result)
    # No enricher registered - return result unchanged
    return result
