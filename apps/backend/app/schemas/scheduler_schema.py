"""Scheduler schemas."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SchedulerJobUrls:
    """Finished hrefs for one scheduler job row (fw.url_assembly_in_view)."""

    history: str
    run: str
    reset: str
    toggle: str
