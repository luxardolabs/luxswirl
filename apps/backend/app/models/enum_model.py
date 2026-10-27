"""
Centralized string enums for LuxSwirl models.

Every value that's stored as a `String` column representing a finite set of
states or types is declared here as a `StrEnum`-compatible class. Database
columns remain `String(N)` for migration flexibility — the canonical mapping
between domain concepts and stored strings lives in this file.

Why centralize:
- Single source of truth for valid values per concept
- Renaming a value changes one line; mypy and grep find every consumer
- Pydantic schemas can use these directly for request/response validation
- Templates can iterate `MyEnum.__members__` to render dropdowns without drift

Convention:
- Class names are PascalCase, suffixed with their concept (Status, Type, Role)
- Members are SCREAMING_SNAKE_CASE
- String values are lowercase snake_case (matches DB storage)
- Each class inherits `StrEnum` for transparent string compatibility —
  `MyStatus.FOO == "foo"` is True, `str(MyStatus.FOO) == "foo"`, and JSON
  serializes as the string value.
"""

from enum import Enum, StrEnum
from typing import Any, Literal

# ============================================================================
# Notifications
# ============================================================================


class NotificationStatus(StrEnum):
    """Outcome of a notification delivery attempt (or non-attempt)."""

    SENT = "sent"
    FAILED = "failed"
    RETRYING = "retrying"
    RATE_LIMITED = "rate_limited"
    DEDUPLICATED = "deduplicated"
    SUPPRESSED = "suppressed"


class NotificationProviderType(StrEnum):
    """Built-in notification delivery channels."""

    EMAIL = "email"
    WEBHOOK = "webhook"
    HOMEASSISTANT = "homeassistant"


# ============================================================================
# Checks
# ============================================================================


class CheckType(StrEnum):
    """Health check type executed by an agent."""

    PING = "ping"
    HTTP = "http"
    TCP = "tcp"
    JSON = "json"
    DNS = "dns"
    MYSQL = "mysql"
    POSTGRES = "postgres"
    SYNTHETIC = "synthetic"


class CheckHealthStatus(StrEnum):
    """Derived health of a check, used by the status dashboard filter.

    Not stored — computed from a check's latest result(s): ``up`` (last result
    succeeded), ``down`` (last result failed), ``unknown`` (no recent result).
    This is the closed set the status-page status filter accepts.
    """

    UP = "up"
    DOWN = "down"
    UNKNOWN = "unknown"


class CheckErrorType(StrEnum):
    """Category of error recorded when a check fails — the values the agent
    actually emits (see apps/agent/app/checks/)."""

    TIMEOUT_ERROR = "timeout_error"
    CONNECTION_ERROR = "connection_error"
    DATABASE_ERROR = "database_error"
    QUERY_ERROR = "query_error"
    AUTHENTICATION_ERROR = "authentication_error"
    UNKNOWN_ERROR = "unknown_error"


class HttpMethod(StrEnum):
    """Request method an HTTP or JSON check sends: the RFC 9110 methods and
    PATCH, every one the agent can send (it passes the method through).

    Uppercase, unlike the other enums here: the value is the method on the wire
    and in stored check config.
    """

    GET = "GET"
    HEAD = "HEAD"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"
    OPTIONS = "OPTIONS"
    TRACE = "TRACE"
    CONNECT = "CONNECT"


class DnsRecordType(StrEnum):
    """Record type a DNS check queries — the set the agent's DNS check accepts
    (apps/agent/app/checks/dns.py VALID_RECORD_TYPES). Uppercase, as stored."""

    A = "A"
    AAAA = "AAAA"
    CNAME = "CNAME"
    MX = "MX"
    TXT = "TXT"
    NS = "NS"
    PTR = "PTR"
    SOA = "SOA"
    SRV = "SRV"
    CAA = "CAA"


class BulkCheckOutcome(StrEnum):
    """Outcome of one URL in a bulk check create."""

    SUCCESS = "success"
    FAILED = "failed"


class ArtifactContentType(StrEnum):
    """MIME type of a synthetic check's artifact — the types the agent uploads
    (screenshot PNG, Playwright trace zip). Closed on purpose: the console
    serves an artifact inline with this type, so an open set would let an agent
    upload ``text/html`` and run script on the console's origin."""

    PNG = "image/png"
    ZIP = "application/zip"


# What a check result reports as its status: the outcome, or a failure's error
# type (CheckResult.status).
CheckResultStatus = Literal["success", "failure"] | CheckErrorType


class CheckArtifactType(StrEnum):
    """Artifact captured during synthetic check execution."""

    SCREENSHOT = "screenshot"
    TRACE = "trace"
    VIDEO = "video"
    HAR = "har"


# ============================================================================
# Agents
# ============================================================================


class AgentStatus(StrEnum):
    """Runtime liveness state of an agent (computed, not stored long-term)."""

    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class AssignmentMode(StrEnum):
    """How a check is distributed to agents (Check.assignment_mode)."""

    MANUAL = "manual"
    REPLICATE = "replicate"
    DISTRIBUTE = "distribute"


class AgentApprovalStatus(StrEnum):
    """Operator-controlled lifecycle state of an agent registration."""

    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"
    # Out of use but kept: no checks, hidden from lists, history kept, restorable.
    ARCHIVED = "archived"
    REJECTED = "rejected"


# ============================================================================
# Jobs (server-dispatched work units)
# ============================================================================


class JobStatus(StrEnum):
    """Lifecycle state of a job from creation to terminal."""

    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    """Built-in job types dispatched to agents (or executed on the server)."""

    NETWORK_SCAN = "network_scan"
    NETWORK_DISCOVER = "network_discover"


class ScanCheckKind(StrEnum):
    """The checks a network scan's hosts can become, one kind per request."""

    PING = "ping"  # one ping check per host
    WEB = "web"  # one HTTP check per web port
    SSH = "ssh"  # one TCP check per SSH port
    DATABASE = "database"  # one TCP check per database port


SERVER_RUNNER = "server"
"""UI/URL token for the server runner — a job with no assigned agent.

A job runs on a runner: a specific agent, or the server itself. This is the
token that names the second case in a filter query string or a URL, so it lives
with the other closed-set tokens rather than on the ORM model — the web layer
reads it without importing a model to do so.
"""


# ============================================================================
# Maintenance jobs (backend-internal cascading mutations — distinct from `jobs`)
# ============================================================================


class MaintenanceJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class MaintenanceJobKind(StrEnum):
    AGENT_DELETE = "agent_delete"
    BULK_CHECK_DELETE = "bulk_check_delete"
    BULK_CHECK_TOGGLE = "bulk_check_toggle"
    BULK_CHECK_MODIFY = "bulk_check_modify"
    BULK_CHECK_IMPORT = "bulk_check_import"
    BULK_CHECK_CREATE = "bulk_check_create"
    STATUS_PAGE_DELETE = "status_page_delete"


# ============================================================================
# Alerts
# ============================================================================


class RegistrationKeyStatus(StrEnum):
    """Whether an agent registration key can still be used."""

    ACTIVE = "active"
    REVOKED = "revoked"


class AlertTriggerType(StrEnum):
    """Condition under which an alert rule fires."""

    STATUS_CHANGE = "status_change"
    THRESHOLD = "threshold"
    REPEATED_FAILURE = "repeated_failure"
    SSL_CERT_EXPIRY = "ssl_cert_expiry"
    # Evaluated on a schedule, not per result: they fire on the ABSENCE of data.
    NO_DATA = "no_data"
    AGENT_OFFLINE = "agent_offline"


# ============================================================================
# Scheduler (background-job framework)
# ============================================================================


class SchedulerJobCategory(StrEnum):
    """Grouping for scheduler-managed background jobs."""

    CLEANUP = "cleanup"
    MONITORING = "monitoring"
    SYSTEM = "system"


class SchedulerTriggerType(StrEnum):
    """How a scheduler job's next run is determined."""

    INTERVAL = "interval"
    CRON = "cron"
    MANUAL = "manual"


class SchedulerExecutionStatus(StrEnum):
    """Outcome of a single scheduler-job execution — the values the scheduler
    actually writes (running while in flight; then success/warning/failed)."""

    RUNNING = "running"
    SUCCESS = "success"
    WARNING = "warning"
    FAILED = "failed"


# ============================================================================
# Settings
# ============================================================================


class SettingCategory(StrEnum):
    """Top-level grouping for runtime settings."""

    CHECK = "check"
    ALERT = "alert"
    SYSTEM = "system"
    JOB = "job"
    METRICS = "metrics"
    DATABASE = "database"
    GENERAL = "general"
    SECURITY = "security"


# ============================================================================
# Users
# ============================================================================


class SettingValueType(StrEnum):
    """Python type a setting's stored value carries (Setting.set_typed_value)."""

    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    STRING = "string"
    LIST = "list"
    DICT = "dict"


class UserRole(StrEnum):
    """Role granted to a user account; controls authorization."""

    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


# ============================================================================
# Display labels
# ============================================================================
# Per-enum display labels for form dropdowns and other UX surfaces. Filter
# dropdowns can derive labels mechanically from the value (title-case), but
# forms often need richer text (e.g. "Viewer - Read-only access"). Keeping the
# labels next to the enum classes makes drift visible in code review and the
# architecture tests assert every member has a label entry.

_USER_ROLE_LABELS: dict[UserRole, str] = {
    UserRole.ADMIN: "Admin - Full system access",
    UserRole.EDITOR: "Editor - Can manage checks and agents",
    UserRole.VIEWER: "Viewer - Read-only access",
}

_CHECK_TYPE_LABELS: dict[CheckType, str] = {
    CheckType.PING: "Ping",
    CheckType.HTTP: "HTTP",
    CheckType.TCP: "TCP",
    CheckType.JSON: "JSON",
    CheckType.DNS: "DNS",
    CheckType.MYSQL: "MySQL",
    CheckType.POSTGRES: "PostgreSQL",
    CheckType.SYNTHETIC: "Synthetic (Playwright)",
}

_ENUM_LABEL_REGISTRY: dict[type[Enum], dict[Any, str]] = {
    UserRole: _USER_ROLE_LABELS,
    CheckType: _CHECK_TYPE_LABELS,
}


def label_for(member: Enum) -> str:
    """Return the display label for an enum member.

    Falls back to a title-cased version of the value if the enum class has no
    registered label dict. Always returns a non-empty string — never raises.
    """
    labels = _ENUM_LABEL_REGISTRY.get(type(member))
    if labels is not None and member in labels:
        return labels[member]
    # Fallback: take the string value, replace underscores, title-case.
    raw = member.value if isinstance(member.value, str) else str(member.value)
    return raw.replace("_", " ").title()


def options_for(enum_cls: type[Enum]) -> list[dict[str, str]]:
    """Build a list of {value, label} dicts for every member of an enum class.

    Designed to be called from view services and passed straight to a Jinja
    context for iteration in <select> dropdowns:

        return {..., "role_options": options_for(UserRole)}

    Then in the template:

        {% for opt in role_options %}
        <option value="{{ opt.value }}">{{ opt.label }}</option>
        {% endfor %}
    """
    return [{"value": m.value, "label": label_for(m)} for m in enum_cls]
