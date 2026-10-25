"""Results of the database maintenance cycle (`DatabaseMaintenanceCoreService`)."""

from datetime import datetime

from pydantic import Field

from app.schemas.base_schema import BaseSchema

_MB = 1024 * 1024


class TableBloat(BaseSchema):
    """One table's size and dead-tuple share."""

    table: str = Field(..., description="Table name")
    size_bytes: int = Field(..., description="Total on-disk size")
    row_count: int = Field(..., description="Exact row count")
    live_tuples: int = Field(..., description="Live tuples (pg_stat_user_tables)")
    dead_tuples: int = Field(..., description="Dead tuples (pg_stat_user_tables)")

    @property
    def bloat_percentage(self) -> float:
        """Dead tuples as a share of all tuples, in percent (0 for an empty table)."""
        total = self.live_tuples + self.dead_tuples
        return round(self.dead_tuples / total * 100, 2) if total else 0.0

    @property
    def size_mb(self) -> float:
        """`size_bytes` in MiB, two decimals."""
        return round(self.size_bytes / _MB, 2)


class VacuumResult(BaseSchema):
    """What one VACUUM did, or why it failed."""

    table: str = Field(..., description="Table name")
    full: bool = Field(..., description="VACUUM FULL rather than plain VACUUM")
    duration_seconds: float = Field(..., description="Wall time")
    size_before_bytes: int | None = Field(None, description="Size before")
    size_after_bytes: int | None = Field(None, description="Size after")
    error: str | None = Field(None, description="Exception type, when it failed")

    @property
    def space_reclaimed_mb(self) -> float:
        """MiB returned to the OS (0 when either size is unknown)."""
        if self.size_before_bytes is None or self.size_after_bytes is None:
            return 0.0
        return round((self.size_before_bytes - self.size_after_bytes) / _MB, 2)


class MaintenanceReport(BaseSchema):
    """The outcome of one cycle; every step's failures are named, not raised."""

    started_at: datetime = Field(..., description="When the cycle started")
    bloat: list[TableBloat] = Field(default_factory=list)
    vacuums: list[VacuumResult] = Field(default_factory=list)
    aggregates_refreshed: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def space_reclaimed_mb(self) -> float:
        """MiB reclaimed by the successful VACUUMs."""
        return round(
            sum(v.space_reclaimed_mb for v in self.vacuums if v.error is None), 2
        )
