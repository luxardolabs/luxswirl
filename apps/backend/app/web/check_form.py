"""Form field sets for the check create / update / clone panels.

All three panels post the same wide field set — the previous shape declared it
three times as ~25 `Annotated[..., Form()]` parameters per handler, then
re-packed each into a dict by hand. Declaring it once here keeps the routers to
HTTP plumbing and makes it impossible for the three to drift apart.

Each dataclass is `kw_only` so a subclass can make an inherited field required
(a required field after a defaulted one is otherwise a dataclass error), and
`as_dict()` hands the view service the same mapping it already takes.
"""

from dataclasses import asdict, dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import Form
from pydantic import BeforeValidator

from app.core.query_params import empty_to_none

# An unselected dropdown posts "" — normalize to None before UUID validation,
# mirroring the query-param filters.
OptionalIdForm = Annotated[UUID | None, BeforeValidator(empty_to_none), Form()]


@dataclass(kw_only=True)
class CheckFormFields:
    """The fields that are optional on every check panel.

    The identifying fields (agent, type, name, target, interval) are NOT here:
    a dataclass subclass cannot make an inherited defaulted field required
    again — redeclaring it leaves the base's default in place — so each panel
    declares its own required set below.
    """

    enabled: Annotated[bool, Form()] = False
    tags: Annotated[str, Form()] = ""
    http_method: Annotated[str | None, Form()] = None
    expected_status: Annotated[int | None, Form()] = None
    json_path: Annotated[str | None, Form()] = None
    expected_value: Annotated[str | None, Form()] = None
    script_code: Annotated[str | None, Form()] = None
    # None, not a default_factory: FastAPI reads the dataclass default
    # verbatim, and a factory sentinel fails list validation. as_dict()
    # hands the view service the empty list it expects.
    alert_ids: Annotated[list[UUID] | None, Form()] = None
    assignment_mode: Annotated[str, Form()] = "manual"
    agent_selector: Annotated[str | None, Form()] = None
    record_type: Annotated[str | None, Form()] = None
    nameserver: Annotated[str | None, Form()] = None
    port: Annotated[str | None, Form()] = None
    expect_value: Annotated[str | None, Form()] = None
    connection_string: Annotated[str | None, Form()] = None
    query: Annotated[str | None, Form()] = None
    description: Annotated[str, Form()] = ""
    timeout_seconds: Annotated[str, Form()] = ""
    verify_ssl: Annotated[bool, Form()] = True
    retry_attempts: Annotated[str, Form()] = ""
    retry_interval_seconds: Annotated[str, Form()] = ""
    resend_notification_after: Annotated[str, Form()] = ""
    depends_on_check_id: OptionalIdForm = None

    def as_dict(self) -> dict[str, Any]:
        """The field mapping the view services take."""
        fields = asdict(self)
        fields["alert_ids"] = fields["alert_ids"] or []
        return fields


@dataclass(kw_only=True)
class CheckCreateFields(CheckFormFields):
    """POST /checks/create — the agent and type are required; name and target
    are filled in by the panel (a bulk import posts neither)."""

    agent_id: Annotated[UUID, Form()]
    check_type: Annotated[str, Form()]
    display_name: Annotated[str, Form()] = ""
    target: Annotated[str, Form()] = ""
    interval: Annotated[int, Form()] = 60


@dataclass(kw_only=True)
class CheckUpdateFields(CheckFormFields):
    """POST /checks/{id}/update — the panel always posts the identifying fields."""

    check_type: Annotated[str, Form()]
    display_name: Annotated[str, Form()]
    target: Annotated[str, Form()]
    interval: Annotated[int, Form()]


@dataclass(kw_only=True)
class CheckCloneFields(CheckFormFields):
    """POST /checks/{id}/clone — a clone names its target agent and new name."""

    agent_id: Annotated[UUID, Form()]
    check_type: Annotated[str, Form()]
    display_name: Annotated[str, Form()]
    target: Annotated[str, Form()]
    interval: Annotated[int, Form()] = 60
