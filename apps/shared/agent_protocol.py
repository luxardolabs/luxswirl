"""Names the agent and the server agree on in the agent protocol.

Every refusal the server sends an agent, every 401 and 403 on every route of
the agent protocol, carries the refusal header. The agent believes only a
marked refusal: a 401 or 403 without it comes from something in between (a
proxy, a gateway, a misrouted service, a server from before the mark) and is
retried like any other temporary failure, never read as the server's word on
the agent's credentials. So an agent needs a server that marks its refusals.

The value names what was refused, where the agent acts on it:

- `recovery-secret`: the agent's recovery secret (a revoke voids it): the
  agent registers again.
- `agent-key`: a heartbeat's key, sent without the secret, in the agent-key
  format or from an agent not pending or rejected (a revoke clears the key):
  the agent proves its recovery secret next, to recover a new key or learn
  that a revoke voided it.
- `registration-key`: the shared registration key (register, recover-key, a
  heartbeat in any other format from a pending or rejected agent with no key):
  the agent needs a new LUXSWIRL_AUTH_KEY.

The one 403 on an agent route left unmarked is the same-origin guard's
refusal of a cross-site browser request, which an agent never sends.
- `refused`: any other refusal (an agent that is not active, a job or check
  that is not its own).
"""

REFUSAL_HEADER = "X-LuxSwirl-Refused"
RECOVERY_SECRET_REFUSED = "recovery-secret"
AGENT_KEY_REFUSED = "agent-key"
REGISTRATION_KEY_REFUSED = "registration-key"
REFUSED = "refused"

# The statuses that are refusals, and so always carry the header.
REFUSAL_STATUSES = frozenset({401, 403})
