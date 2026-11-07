"""
MCP audit core service - the log of every tool call an AI agent makes over /mcp.

Arguments are kept with every credential masked (a check's target or connection
string carries them), each string cut to a length, and a synthetic script by its size
alone, since the log is for seeing what an agent did, not for replaying it. Rows older
than the retention are pruned by the job purge task.
"""

import asyncio
import ipaddress
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.credential_mask import (
    KeyNames,
    mask_credentials,
    names_a_secret,
)
from app.core.datetime_utils import utc_now
from app.crud.mcp_tool_call_crud import MCPToolCallCRUD
from app.models.enum_model import MCPDenialCode
from app.models.mcp_tool_call_model import MCPToolCall
from app.schemas.api_activity_schema import ActivityCaller, ActivityFilters, McpCallRow
from app.schemas.api_token_schema import ApiPrincipal

# How long a tool call stays in the audit log.
AUDIT_RETENTION = timedelta(days=90)
# The longest tool name the audit log keeps (the column's width); an agent may send any.
TOOL_NAME_LENGTH = 100
# The longest client name kept, by the MCP and admin API logs alike: enough to tell
# clients apart, never a payload.
USER_AGENT_LENGTH = 256

# The longest argument string kept. An argument can reach 4 MiB, and what is kept is
# masked again each time a row is opened.
ARGUMENT_LENGTH = 4096
# Longer than this, a string is kept by its size alone: what decides how it is masked can
# lie past any cut, so a piece of it cannot be masked safely.
MASKED_LENGTH = 64 * 1024
# What a string already cut ends with, and the room it takes. A read keeps it as it is, so
# it says the length that was sent.
_CUT_NOTE = re.compile(r"\.\.\. <\d+ characters>\Z")
_NOTE_ROOM = 256
# Masking a large call takes seconds. One thread of its own keeps that off the event loop
# and out of the default executor, which name lookups share.
_MASKING = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-audit-mask")

# What a synthetic script is stored as: its size alone.
_SCRIPT_SIZE = re.compile(r"<\d+ characters>")


def masked_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """A tool call's arguments as the audit log keeps them: every credential masked,
    keys included, every string under a key naming a secret hidden, each string cut to
    `ARGUMENT_LENGTH`, a synthetic script by its size alone.

    This can take seconds over 4 MiB of short strings: callers on the event loop run it
    through `_masked_off_the_loop`.
    """
    names = KeyNames()
    return {
        names.of(key, _kept(key)): _masked(key, value, names, False)
        for key, value in arguments.items()
    }


async def _masked_off_the_loop(arguments: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.get_running_loop().run_in_executor(
        _MASKING, masked_arguments, arguments
    )


def _whole_tokens(text: str, limit: int) -> str:
    """`text` to at most `limit`, without the token the limit falls in: a secret cut in
    two no longer reads as one, so its first half would be shown."""
    if len(text) <= limit:
        return text
    parts = text[:limit].rsplit(None, 1)
    return parts[0] if len(parts) == 2 else ""


def _kept(text: str) -> str:
    """`text` as a row keeps it: storable, masked, at most `ARGUMENT_LENGTH`, saying how
    long it was."""
    storable = storable_text(text)
    if len(storable) <= ARGUMENT_LENGTH + _NOTE_ROOM and _CUT_NOTE.search(
        storable[-64:]
    ):
        return mask_credentials(storable)
    if len(storable) > MASKED_LENGTH:
        return f"<{len(storable)} characters>"
    masked = mask_credentials(storable)
    if len(masked) <= ARGUMENT_LENGTH:
        return masked
    return f"{_whole_tokens(masked, ARGUMENT_LENGTH)}... <{len(storable)} characters>"


def _masked(key: object, value: object, names: KeyNames, hidden: bool) -> object:
    name = storable_text(key) if isinstance(key, str) else ""
    if name == "script_code":
        # Masked again on read: a size already recorded is kept, not re-measured.
        if isinstance(value, str):
            if _SCRIPT_SIZE.fullmatch(value):
                return value
            return f"<{len(value)} characters>"
        return "<not text>"
    hidden = hidden or names_a_secret(name)
    if isinstance(value, dict):
        return {
            names.of(str(k), _kept(str(k))): _masked(k, v, names, hidden)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_masked(None, item, names, hidden) for item in value]
    if isinstance(value, str):
        return "***" if hidden and value else _kept(value)
    return value


def storable_text(text: str) -> str:
    """`text` without what Postgres refuses in text and JSON: NUL characters and lone
    surrogates, either of which would lose the whole row."""
    without_nul = text.replace("\x00", "")
    if without_nul.isascii():
        return without_nul
    return without_nul.encode("utf-8", "ignore").decode("utf-8")


def storable_user_agent(user_agent: str | None) -> str | None:
    """`user_agent` as an audit row keeps it: cut to `USER_AGENT_LENGTH`."""
    return storable_text(user_agent)[:USER_AGENT_LENGTH] if user_agent else None


def storable_address(value: str | None) -> str | None:
    """`value` when it is an IP address; None otherwise (no peer, a unix socket)."""
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


class MCPAuditCoreService:
    """Record and prune MCP tool calls."""

    @staticmethod
    async def log_call(
        db: AsyncSession,
        *,
        principal: ApiPrincipal | None,
        tool_name: str,
        arguments: dict[str, Any],
        success: bool,
        error_message: str | None,
        duration_ms: int,
        denial_code: MCPDenialCode | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        """Append one audit row for a call `principal` made."""
        await MCPToolCallCRUD.log_call(
            db,
            user_id=principal.user.id if principal else None,
            api_token_id=principal.token_id if principal else None,
            tool_name=storable_text(tool_name)[:TOOL_NAME_LENGTH],
            arguments=await _masked_off_the_loop(arguments),
            success=success,
            error_message=(_kept(error_message) if error_message else None),
            duration_ms=duration_ms,
            ip_address=storable_address(ip_address),
            user_agent=storable_user_agent(user_agent),
            denial_code=denial_code,
        )

    @staticmethod
    async def purge_expired(db: AsyncSession) -> int:
        """Delete the calls older than the retention; how many were deleted."""
        return await MCPToolCallCRUD.delete_older_than(db, utc_now() - AUDIT_RETENTION)

    # ── Reads (the activity screens) ──────────────────────────────────────────

    @staticmethod
    async def list_calls(
        db: AsyncSession, filters: ActivityFilters, limit: int
    ) -> list[McpCallRow]:
        """The newest calls matching `filters` (one user's when `filters.user_id`),
        without their arguments, which only a single call's panel shows."""
        rows = await MCPToolCallCRUD.list_calls(
            db, window_start(filters.days), filters, limit
        )
        errors = [call.error_message for call, _, _ in rows]
        kept = await asyncio.get_running_loop().run_in_executor(
            _MASKING, lambda: [_kept(error) if error else None for error in errors]
        )
        return [
            _row(call, username, token_name, {}, error)
            for (call, username, token_name), error in zip(rows, kept, strict=True)
        ]

    @staticmethod
    async def get_call(db: AsyncSession, call_id: UUID) -> McpCallRow | None:
        """One call, its arguments masked again on the way out; None when none."""
        found = await MCPToolCallCRUD.get_call(db, call_id)
        if found is None:
            return None
        call, username, token_name = found
        arguments = await _masked_off_the_loop(call.arguments or {})
        error = call.error_message
        kept = _kept(error) if error else None
        return _row(call, username, token_name, arguments, kept)

    @staticmethod
    async def tool_names(
        db: AsyncSession, days: int, user_id: UUID | None
    ) -> list[str]:
        """Every tool called in the window (by `user_id`'s tokens, when set)."""
        return list(await MCPToolCallCRUD.tool_names(db, window_start(days), user_id))

    @staticmethod
    async def callers(db: AsyncSession, days: int) -> list[ActivityCaller]:
        """Each user and token pair with calls in the window."""
        return [
            ActivityCaller(
                user_id=user_id,
                username=username,
                api_token_id=token_id,
                token_name=token_name,
            )
            for user_id, username, token_id, token_name in await MCPToolCallCRUD.callers(
                db, window_start(days)
            )
        ]


def window_start(days: int) -> datetime:
    """When an activity window of `days` days opens."""
    return utc_now() - timedelta(days=days)


def _row(
    call: MCPToolCall,
    username: str | None,
    token_name: str | None,
    arguments: dict[str, Any],
    error_message: str | None,
) -> McpCallRow:
    return McpCallRow(
        id=call.id,
        created_at=call.created_at,
        user_id=call.user_id,
        username=username,
        api_token_id=call.api_token_id,
        token_name=token_name,
        tool_name=call.tool_name,
        success=call.success,
        denial_code=call.denial_code,
        error_message=error_message,
        duration_ms=call.duration_ms,
        ip_address=str(call.ip_address) if call.ip_address is not None else None,
        user_agent=call.user_agent,
        # Masked when written; masked again here, so a row written before a masking
        # rule existed never shows a credential.
        arguments=arguments,
    )
