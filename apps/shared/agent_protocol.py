"""Names the agent and the server agree on in the agent protocol.

The server marks its refusal of an agent's recovery secret with a response
header, so the agent tells that refusal (a revoke voided the secret: register
again) apart from any other 401, a proxy's, a gateway's or a misrouted
service's, which must never make it give up its credentials.
"""

REFUSAL_HEADER = "X-LuxSwirl-Refused"
RECOVERY_SECRET_REFUSED = "recovery-secret"
