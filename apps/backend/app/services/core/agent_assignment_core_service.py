"""
Agent Assignment Service - handles tag-based check assignment logic.

Implements REPLICATE and DISTRIBUTE modes for checks.
"""

import hashlib
from collections.abc import Sequence

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.agent_crud import AgentCRUD
from app.crud.check_crud import CheckCRUD
from app.models.agent_model import Agent
from app.models.check_model import Check

logger = get_logger("luxswirl.services.agent_assignment")


class AgentAssignmentCoreService:
    """Service for agent assignment logic."""

    @staticmethod
    def agent_matches_selector(agent: Agent, selector: dict | None) -> bool:
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

        def _score(agent: Agent) -> tuple[int, str]:
            digest = hashlib.sha256(f"{check.id}:{agent.id}".encode()).digest()
            # Tie-break on the agent UUID (unique, stable) so the result is
            # fully deterministic even in the astronomically unlikely digest tie.
            return (int.from_bytes(digest, "big"), str(agent.id))

        return max(available_agents, key=_score)

    @staticmethod
    async def get_matching_agents(
        db: AsyncSession,
        selector: dict | None,
    ) -> Sequence[Agent]:
        """
        Get all agents that match the given selector.

        Args:
            db: Database session
            selector: Selector dict

        Returns:
            List of matching agents
        """
        if not selector:
            return []

        # Get all agents (unfiltered for selector matching)
        all_agents = await AgentCRUD.list_all(db)

        # Filter by selector
        matching_agents = [
            agent
            for agent in all_agents
            if AgentAssignmentCoreService.agent_matches_selector(agent, selector)
        ]

        return matching_agents

    @staticmethod
    async def get_checks_for_agent(
        db: AsyncSession,
        agent: Agent,
    ) -> list[Check]:
        """
        Get all checks that should run on a specific agent.

        Implements the assignment logic for manual/replicate/distribute modes.

        Args:
            db: Database session
            agent: The agent requesting checks

        Returns:
            List of checks this agent should run
        """
        checks_to_run = []

        # Get all checks
        all_checks = await CheckCRUD.list_all(db)

        for check in all_checks:
            if check.assignment_mode == "manual":
                # MANUAL: Check is assigned to specific agent
                if check.agent_id == agent.id:
                    checks_to_run.append(check)

            elif check.assignment_mode == "replicate":
                # REPLICATE: Check runs on ALL matching agents
                if AgentAssignmentCoreService.agent_matches_selector(agent, check.agent_selector):
                    checks_to_run.append(check)

            elif check.assignment_mode == "distribute":
                # DISTRIBUTE: Check assigned to ONE agent via hash
                if AgentAssignmentCoreService.agent_matches_selector(agent, check.agent_selector):
                    # Get all matching agents for this check
                    matching_agents = await AgentAssignmentCoreService.get_matching_agents(
                        db, check.agent_selector
                    )

                    # Determine assigned agent via hash
                    assigned_agent = AgentAssignmentCoreService.get_assigned_agent_for_check(
                        check, matching_agents
                    )

                    # Add check if this agent was assigned
                    if assigned_agent and assigned_agent.id == agent.id:
                        checks_to_run.append(check)

        logger.debug(
            "Agent assigned checks (manual + replicate + distribute)",
            extra={
                "agent_name": agent.agent_name,
                "agent_id": str(agent.id),
                "check_count": len(checks_to_run),
            },
        )

        return checks_to_run
