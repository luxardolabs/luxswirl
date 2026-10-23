"""
Agent Assignment Service - handles tag-based check assignment logic.

Implements REPLICATE and DISTRIBUTE modes for checks.
"""

import hashlib
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.core.exceptions import AgentNotFoundException
from app.core.liveness import agent_offline, offline_at
from app.crud.agent_crud import AgentCRUD
from app.crud.check_crud import CheckCRUD
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.agent_assignment")


class AgentAssignmentCoreService:
    """Service for agent assignment logic."""

    @staticmethod
    def agent_matches_selector(agent: Agent, selector: dict[str, Any] | None) -> bool:
        """
        Check if agent matches the given selector.

        Args:
            agent: Agent to check
            selector: Selector dict with "agent_ids" or "tags" keys

        Returns:
            True if agent matches selector

        Examples:
            {"agent_ids": ["agent1", "agent2"]} - matches specific agents
            {"tags": ["role:monitor", "region:us-east"]} - matches agents with ALL these tags
        """
        if not selector:
            return False

        # Match by specific agent names
        if "agent_ids" in selector:
            return agent.agent_name in selector["agent_ids"]

        # Match by tags
        if "tags" in selector:
            if not agent.tags:
                return False

            agent_tags = {tag.strip() for tag in agent.tags if tag and tag.strip()}

            # Required tags from selector
            required_tags = set(selector["tags"])

            # Get match mode (default to "all" for backward compatibility)
            match_mode = selector.get("match_mode", "all")

            if match_mode == "any":
                # ANY mode: Agent must have at least ONE of the required tags (OR logic)
                return bool(required_tags.intersection(agent_tags))
            else:
                # ALL mode: Agent must have ALL required tags (AND logic)
                return required_tags.issubset(agent_tags)

        return False

    @staticmethod
    def _hrw_score(check: Check, agent: Agent) -> tuple[int, str]:
        """`agent`'s rendezvous weight for `check`: the highest wins."""
        digest = hashlib.sha256(f"{check.id}:{agent.id}".encode()).digest()
        # Tie-break on the agent UUID (unique, stable) so the result is fully
        # deterministic even in the astronomically unlikely digest tie.
        return (int.from_bytes(digest, "big"), str(agent.id))

    @staticmethod
    def get_assigned_agent_for_check(
        check: Check,
        available_agents: Sequence[Agent],
    ) -> Agent | None:
        """
        Determine which agent should run a check in DISTRIBUTE mode.

        Uses rendezvous (highest-random-weight / HRW) hashing keyed on the
        immutable UUIDs: each candidate agent is scored by
        ``sha256("{check.id}:{agent.id}")`` and the highest score wins.

        Because sha256 is process-independent and the keys are the stable UUIDs
        (not the editable, non-unique ``display_name``), the same check
        deterministically maps to the same agent across server restarts, worker
        processes, and replicas. When the agent pool changes, only the checks
        owned by the departing agent move (the HRW property) — unlike modulo
        hashing, which reshuffles every check on any pool-size change.

        Args:
            check: The check to assign
            available_agents: List of agents matching the selector

        Returns:
            Agent that should run this check, or None if no agents available
        """
        if not available_agents:
            return None
        return max(
            available_agents,
            key=lambda agent: AgentAssignmentCoreService._hrw_score(check, agent),
        )

    @staticmethod
    def distribute_owner(
        check: Check, pool: Sequence[Agent], now: datetime
    ) -> Agent | None:
        """The agent of `pool` (the active agents matching the check's
        selector) that runs a distributed check.

        The highest-weighted agent that is online (`app.core.liveness`), so an
        agent that misses its heartbeats hands its checks to the others and
        takes them back at its next heartbeat. When the whole pool is offline
        the check stays with its first choice: nothing is running it, and the
        silence alerts name that agent's outage.
        """
        online = [agent for agent in pool if not agent_offline(agent, now)]
        return AgentAssignmentCoreService.get_assigned_agent_for_check(
            check, online or pool
        )

    @staticmethod
    def handed_over_at(
        check: Check, pool: Sequence[Agent], now: datetime
    ) -> datetime | None:
        """When a distributed check moved to the agent now running it, or None
        when it runs on its first choice.

        Every agent weighted above the current owner is offline; the check
        moved when the last of them went offline.
        """
        owner = AgentAssignmentCoreService.distribute_owner(check, pool, now)
        if owner is None:
            return None
        owner_score = AgentAssignmentCoreService._hrw_score(check, owner)
        above = [
            agent
            for agent in pool
            if AgentAssignmentCoreService._hrw_score(check, agent) > owner_score
        ]
        if not above:
            return None
        return max(offline_at(agent) for agent in above)

    @staticmethod
    def filter_agents_by_selector(
        agents: Sequence[Agent],
        selector: dict[str, Any] | None,
    ) -> Sequence[Agent]:
        """Filter an already-loaded agent list by a selector.

        The selector match is pure Python over the full agent list, so a caller
        resolving many checks loads the agents once and filters per selector
        rather than re-reading the table for each one.

        Args:
            agents: The full agent list.
            selector: Selector dict, or None to match nothing.

        Returns:
            The agents matching the selector.
        """
        if not selector:
            return []
        return [
            agent
            for agent in agents
            if AgentAssignmentCoreService.agent_matches_selector(agent, selector)
        ]

    @staticmethod
    async def may_report_for_check(
        db: AsyncSession, agent_id: UUID, check_id: UUID
    ) -> bool:
        """Whether `agent_id` may post data for `check_id`.

        The same answer `get_reportable_checks` gives a report.

        Raises:
            AgentNotFoundException: If the agent does not exist
        """
        agent = await AgentCRUD.get_by_id_with_checks(db, agent_id)
        if agent is None:
            raise AgentNotFoundException(str(agent_id))
        checks = await AgentAssignmentCoreService.get_reportable_checks(db, agent)
        return any(check.id == check_id for check in checks)

    @staticmethod
    async def get_checks_for_agent(
        db: AsyncSession,
        agent: Agent,
    ) -> list[Check]:
        """
        Get all checks that should run on a specific agent.

        Implements the assignment logic for manual/replicate/distribute modes:
        a distributed check goes to its `distribute_owner` alone.

        Args:
            db: Database session
            agent: The agent requesting checks

        Returns:
            List of checks this agent should run
        """
        checks_to_run = await AgentAssignmentCoreService._checks_for_agent(
            db, agent, owned_only=True
        )
        logger.debug(
            "Agent assigned checks (manual + replicate + distribute)",
            extra={
                "agent_name": agent.agent_name,
                "agent_id": str(agent.id),
                "check_count": len(checks_to_run),
            },
        )
        return checks_to_run

    @staticmethod
    async def get_reportable_checks(db: AsyncSession, agent: Agent) -> list[Check]:
        """The checks `agent` may post results and artifacts for.

        Its `get_checks_for_agent` checks, plus every distributed check whose
        pool it is in. A distributed check moves between the agents of its pool
        as they go offline and come back, and a result is measured before it
        reaches the server: the owner's results buffered through an outage, or
        the stand-in's last batch, arrive after the check has moved. Each of
        those agents is one the check may run on, so its results are the
        check's results.
        """
        return await AgentAssignmentCoreService._checks_for_agent(
            db, agent, owned_only=False
        )

    @staticmethod
    async def _checks_for_agent(
        db: AsyncSession, agent: Agent, *, owned_only: bool
    ) -> list[Check]:
        checks_to_run = []

        all_checks = await CheckCRUD.list_assignment_candidates(db, agent.id)

        # Distribute-mode checks each need the pool of agents matching their
        # selector. The pool is the ACTIVE agents only: a pending registration
        # (anyone with the shared registration key can make one), or a paused,
        # disabled or rejected agent, may not run checks, so it must never win
        # one. That set is the same table read every time (filtering is pure
        # Python), so it is read once here rather than per check.
        active_agents: Sequence[Agent] | None = None
        now = utc_now()

        for check in all_checks:
            if check.assignment_mode == "manual":
                # MANUAL: Check is assigned to specific agent
                if check.agent_id == agent.id:
                    checks_to_run.append(check)

            elif check.assignment_mode == "replicate":
                # REPLICATE: Check runs on ALL matching agents
                if AgentAssignmentCoreService.agent_matches_selector(
                    agent, check.agent_selector
                ):
                    checks_to_run.append(check)

            # DISTRIBUTE: Check assigned to ONE agent of its pool
            elif check.assignment_mode == "distribute" and (
                AgentAssignmentCoreService.agent_matches_selector(
                    agent, check.agent_selector
                )
            ):
                if active_agents is None:
                    active_agents = await AgentCRUD.list_active(db)
                pool = AgentAssignmentCoreService.filter_agents_by_selector(
                    active_agents, check.agent_selector
                )
                if not owned_only:
                    if any(member.id == agent.id for member in pool):
                        checks_to_run.append(check)
                    continue
                owner = AgentAssignmentCoreService.distribute_owner(check, pool, now)
                if owner and owner.id == agent.id:
                    checks_to_run.append(check)

        return checks_to_run

    @staticmethod
    async def config_version(db: AsyncSession, agent: Agent) -> str | None:
        """The version of `agent`'s check list, sent on every heartbeat.

        The agent fetches its checks again whenever this changes, so it covers
        both an edit to its checks (`checks_updated_at`) and a distributed
        check moving to or from it. None until the agent has checks.
        """
        checks = await AgentAssignmentCoreService.get_checks_for_agent(db, agent)
        if agent.checks_updated_at is None and not checks:
            return None
        stamp = agent.checks_updated_at.isoformat() if agent.checks_updated_at else ""
        ids = ",".join(sorted(str(check.id) for check in checks))
        return hashlib.sha256(f"{stamp}|{ids}".encode()).hexdigest()[:32]

    @staticmethod
    async def assigned_agents_by_check(
        db: AsyncSession,
    ) -> list[tuple[Check, list[Agent], list[Agent]]]:
        """Every enabled check with the active agents that are to run it, and
        the active agents it may run on.

        The same rules as `get_checks_for_agent`, read the other way round and
        limited to active agents: a check whose agents are all pending, paused,
        disabled or rejected has empty lists, since nothing is to run it. The
        second list differs from the first only for a distributed check: it is
        the check's whole pool, its owner among them.
        """
        checks = await CheckCRUD.list_enabled(db)
        active_agents = await AgentCRUD.list_active(db)
        by_id = {agent.id: agent for agent in active_agents}
        now = utc_now()

        assigned: list[tuple[Check, list[Agent], list[Agent]]] = []
        for check in checks:
            agents: list[Agent] = []
            pool: list[Agent] = []
            if check.assignment_mode == "manual":
                owner = by_id.get(check.agent_id)
                agents = [owner] if owner else []
                pool = agents
            elif check.assignment_mode == "replicate":
                agents = list(
                    AgentAssignmentCoreService.filter_agents_by_selector(
                        active_agents, check.agent_selector
                    )
                )
                pool = agents
            elif check.assignment_mode == "distribute":
                pool = list(
                    AgentAssignmentCoreService.filter_agents_by_selector(
                        active_agents, check.agent_selector
                    )
                )
                chosen = AgentAssignmentCoreService.distribute_owner(check, pool, now)
                agents = [chosen] if chosen else []
            assigned.append((check, agents, pool))
        return assigned
