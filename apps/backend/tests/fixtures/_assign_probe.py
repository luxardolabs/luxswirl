"""Subprocess probe for the cross-hash-seed determinism test (LUXSWIRL-183).

Run as a standalone script with a fixed check id followed by agent ids on
argv; prints the chosen agent id. Used by
``tests/agents/test_agent_assignment.py`` to assert the DISTRIBUTE assignment
is independent of ``PYTHONHASHSEED``. Kept as a real module with top-level
imports so it runs under a fresh interpreter (the caller sets PYTHONPATH to the
backend + tests roots).
"""

from __future__ import annotations

import sys
from uuid import UUID

from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from fixtures.factories import make_agent, make_check


def main() -> None:
    check_id = UUID(sys.argv[1])
    agent_ids = [UUID(a) for a in sys.argv[2:]]
    check = make_check(agent_id=check_id, id=check_id)
    agents = [make_agent(id=aid, agent_name=str(aid)) for aid in agent_ids]
    chosen = AgentAssignmentCoreService.get_assigned_agent_for_check(check, agents)
    print(chosen.id)


if __name__ == "__main__":
    main()
