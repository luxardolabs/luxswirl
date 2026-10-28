"""
Check model - represents health check definitions.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import ARRAY, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.encrypted_types import EncryptedJSON, EncryptedString
from app.models.base_model import UUIDBaseModel, str_enum
from app.models.enum_model import AssignmentMode, CheckType, DnsRecordType, HttpMethod

if TYPE_CHECKING:
    from app.models.agent_model import Agent
    from app.models.alert_check_mapping_model import AlertCheckMapping
    from app.models.check_artifact_model import CheckArtifact
    from app.models.check_result_model import CheckResult


class Check(UUIDBaseModel):
    """
    Check model - stores health check definitions.

    Each check is uniquely identified by UUID (id).
    Contains the check configuration (type, target, etc.) and a friendly,
    editable display name.
    """

    __tablename__ = "checks"
    __table_args__ = (
        Index("idx_checks_agent_id", "agent_id"),
        Index("idx_checks_type", "check_type"),
        Index("idx_checks_depends_on_check_id", "depends_on_check_id"),
    )

    # Foreign key to agent (UUID)
    agent_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        comment="Foreign key to agents table (UUID)",
    )

    depends_on_check_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("checks.id", ondelete="SET NULL"),
        nullable=True,
        comment="Parent check this one depends on; notifications suppressed when parent is down",
    )

    # Friendly, editable display name
    display_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Friendly display name for the check (editable)",
    )

    check_type: Mapped[CheckType] = mapped_column(
        str_enum(CheckType, 50),
        nullable=False,
        comment="Type of check (ping, http, tcp, json, etc.)",
    )

    # Check target (encrypted - may contain credentials in URL)
    target: Mapped[str] = mapped_column(
        EncryptedString(1000),
        nullable=False,
        comment="Target of the check (URL, hostname, IP, etc.) - encrypted at rest",
    )

    # Optional check configuration
    interval_seconds: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="How often check runs (in seconds)",
    )

    timeout_seconds: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Check timeout (in seconds)",
    )

    description: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
        comment="Human-readable description of the check",
    )

    # Check-type-specific configuration (encrypted - may contain API keys, tokens, etc.)
    check_config: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedJSON,
        nullable=True,
        comment="Check-type-specific configuration - encrypted at rest (may contain sensitive data)",
    )

    # Retry configuration
    retry_attempts: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        server_default="2",
        comment="Number of retry attempts for a single check execution before marking as failed",
    )

    retry_interval_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=30,
        server_default="30",
        comment="Retry interval in seconds (Heartbeat Retry Interval)",
    )

    resend_notification_after: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Resend notification if down X times consecutively (NULL = disabled)",
    )

    # Organization
    tags: Mapped[list[str] | None] = mapped_column(
        ARRAY(String),
        nullable=True,
        comment="Tags for organizing/filtering checks",
    )

    # State tracking
    enabled: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
        server_default="true",
        comment="Whether check is enabled",
    )

    # Agent assignment strategy (Phase 2)
    assignment_mode: Mapped[AssignmentMode] = mapped_column(
        str_enum(AssignmentMode, 20),
        nullable=False,
        server_default="manual",
        comment="Assignment mode: manual, replicate, distribute",
    )

    agent_selector: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Agent selector for replicate/distribute modes (JSON: {tags: [...]} or {agent_ids: [...]})",
    )

    # Synthetic check script (Playwright Python code)
    script_code: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Python script code for synthetic checks (Playwright async)",
    )

    # Encrypted connection string (for database/MySQL/PostgreSQL checks)
    connection_string_encrypted: Mapped[str | None] = mapped_column(
        EncryptedString(1000),
        nullable=True,
        comment="Encrypted database connection string (automatically encrypted at rest)",
    )

    # Relationships
    agent: Mapped[Agent] = relationship(
        "Agent",
        back_populates="checks",
        lazy="selectin",
    )

    parent_check: Mapped[Check | None] = relationship(
        "Check",
        remote_side="Check.id",
        foreign_keys=[depends_on_check_id],
        back_populates="dependent_checks",
        lazy="selectin",
    )

    dependent_checks: Mapped[list[Check]] = relationship(
        "Check",
        back_populates="parent_check",
        foreign_keys=[depends_on_check_id],
        lazy="raise_on_sql",
    )

    check_results: Mapped[list[CheckResult]] = relationship(
        "CheckResult",
        back_populates="check",
        cascade="all, delete-orphan",
        lazy="raise_on_sql",  # refuse a lazy load — could be millions of rows
    )

    alert_mappings: Mapped[list[AlertCheckMapping]] = relationship(
        "AlertCheckMapping",
        back_populates="check",
        cascade="all, delete-orphan",
        lazy="raise_on_sql",
    )

    artifacts: Mapped[list[CheckArtifact]] = relationship(
        "CheckArtifact",
        back_populates="check",
        cascade="all, delete-orphan",
        lazy="raise_on_sql",
    )

    def get_config(self, key: str, default: Any = None) -> Any:
        """Get a value from check_config JSONB."""
        return self.check_config.get(key, default) if self.check_config else default

    # Typed reads of check_config. The column is JSON, so a value is only trusted
    # to be the type its accessor promises once it has been checked; anything
    # else reads as None, never as a wrongly-typed value.
    def _config_str(self, key: str) -> str | None:
        value = self.get_config(key)
        return value if isinstance(value, str) else None

    def _config_int(self, key: str) -> int | None:
        value = self.get_config(key)
        # bool is an int subclass; a flag is not a number.
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _config_bool(self, key: str) -> bool | None:
        value = self.get_config(key)
        return value if isinstance(value, bool) else None

    # Uppercased as the request schemas do (`get` is GET); a value outside the
    # set reads as None.
    def _config_http_method(self) -> HttpMethod | None:
        value = (self._config_str("http_method") or "").strip().upper()
        return HttpMethod(value) if value in set(HttpMethod) else None

    def _config_record_type(self) -> DnsRecordType | None:
        value = (self._config_str("record_type") or "").strip().upper()
        return DnsRecordType(value) if value in set(DnsRecordType) else None

    @property
    def fully_qualified_name(self) -> str:
        """Get fully qualified check name (agent_name:display_name)."""
        if hasattr(self, "agent") and self.agent:
            return f"{self.agent.agent_name}:{self.display_name}"
        return f"unknown:{self.display_name}"

    # Convenience properties - declared but NOT as mapped columns
    @property
    def http_method(self) -> HttpMethod | None:
        return self._config_http_method()

    @property
    def verify_ssl(self) -> bool | None:
        return self._config_bool("verify_ssl")

    @property
    def expected_status(self) -> int | None:
        return self._config_int("expected_status")

    @property
    def json_path(self) -> str | None:
        return self._config_str("json_path")

    @property
    def expected_value(self) -> str | None:
        return self._config_str("expected_value")

    @property
    def record_type(self) -> DnsRecordType | None:
        return self._config_record_type()

    @property
    def nameserver(self) -> str | None:
        return self._config_str("nameserver")

    @property
    def port(self) -> int | None:
        return self._config_int("port")

    @property
    def expect_value(self) -> str | None:
        return self._config_str("expect_value")

    @property
    def connection_string(self) -> str | None:
        """The database connection string, decrypted.

        Kept in `check_config` (encrypted at rest), where create and update
        write it and the agent reads it; a value only in
        `connection_string_encrypted` is still read.
        """
        return self._config_str("connection_string") or self.connection_string_encrypted

    @connection_string.setter
    def connection_string(self, value: str | None) -> None:
        """Set the connection string in `check_config` (encrypted at rest)."""
        config = dict(self.check_config or {})
        if value is None:
            config.pop("connection_string", None)
        else:
            config["connection_string"] = value
        self.check_config = config

    @property
    def query(self) -> str | None:
        return self._config_str("query")

    def __repr__(self) -> str:
        """Generate a helpful repr string."""
        return (
            f"<Check(id={self.id}, name={self.display_name!r}, "
            f"type={self.check_type!r}, target={self.target!r})>"
        )
