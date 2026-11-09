"""
Import/Export schemas — extracted from import_export_router.

Pydantic models for the bulk import/export endpoints. Lives in schemas/ per
the layered architecture standard (no BaseModel subclasses inside routers).
"""

from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enum_model import CheckType
from app.schemas.base_schema import EncryptedText, StorableText, TagList
from app.schemas.check_schema import DnsRecordTypeField, HttpMethodField

# The settings an entry may leave out, kept as they are when it does: an export
# made before they were in it still imports, and changes only what it names.
OPTIONAL_SETTINGS = (
    "retry_interval_seconds",
    "resend_notification_after",
    "verify_ssl",
    "record_type",
    "nameserver",
    "port",
    "expect_value",
    "connection_string",
    "query",
    "script_code",
)


class CheckExport(BaseModel):
    """Check export format.

    A field it does not name is refused, so an entry carrying a setting the
    import cannot take is that entry's error, never a check made without it.
    """

    # Stripped as CheckCreate strips: a surrogate in any text is refused here.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: StorableText
    check_type: CheckType
    target: EncryptedText
    interval: int = Field(default=60, description="Interval in seconds")
    timeout: int = Field(default=5, description="Timeout in seconds")
    retry_attempts: int = Field(default=2, description="Number of retry attempts")
    enabled: bool = Field(default=True, description="Whether check is enabled")
    description: StorableText | None = Field(default=None, max_length=1000)
    http_method: HttpMethodField = None
    expected_status: int | None = None
    json_path: StorableText | None = None
    expected_value: StorableText | None = None
    tags: TagList | None = None
    retry_interval_seconds: int | None = Field(
        default=None, description="Seconds between retries"
    )
    resend_notification_after: int | None = Field(
        default=None, description="Failures before a notification is sent again"
    )
    verify_ssl: bool | None = Field(
        default=None, description="Verify SSL certificates (http, json)"
    )
    record_type: DnsRecordTypeField = None
    nameserver: StorableText | None = Field(default=None, description="DNS nameserver")
    port: int | None = Field(default=None, description="DNS port")
    expect_value: StorableText | None = Field(
        default=None, description="Expected DNS answer"
    )
    connection_string: EncryptedText | None = Field(
        default=None, description="Database connection string (mysql, postgres)"
    )
    query: StorableText | None = Field(
        default=None, description="SQL query (mysql, postgres)"
    )
    script_code: StorableText | None = Field(
        default=None, description="Synthetic script"
    )
    depends_on: StorableText | None = Field(
        default=None,
        description="Name of the check, on the agent imported onto, this one depends on",
    )
    depends_on_check_id: UUID | None = Field(
        default=None, description="Id of another agent's check this one depends on"
    )

    @model_validator(mode="after")
    def _one_parent(self) -> Self:
        if self.depends_on is not None and self.depends_on_check_id is not None:
            raise ValueError("Name depends_on or depends_on_check_id, not both")
        return self

    def names_parent(self) -> bool:
        """Whether the entry says what its check depends on (nothing included)."""
        return bool({"depends_on", "depends_on_check_id"} & self.model_fields_set)

    def settings(self) -> dict[str, Any]:
        """The check's settings, by their create and update names.

        An optional setting the entry leaves out or sends as null is not in it.
        """
        settings: dict[str, Any] = {
            "check_type": self.check_type,
            "target": self.target,
            "interval_seconds": self.interval,
            "timeout_seconds": self.timeout,
            "retry_attempts": self.retry_attempts,
            "enabled": self.enabled,
            "description": self.description,
            "http_method": self.http_method,
            "expected_status": self.expected_status,
            "json_path": self.json_path,
            "expected_value": self.expected_value,
            "tags": self.tags,
        }
        for name in OPTIONAL_SETTINGS:
            value = getattr(self, name)
            if value is not None:
                settings[name] = value
        return settings


class CheckExportDocument(BaseModel):
    """An agent's checks, exported: the document an import takes back."""

    agent_id: UUID
    agent_name: str | None
    agent_hostname: str | None
    total_checks: int
    checks: list[CheckExport]
