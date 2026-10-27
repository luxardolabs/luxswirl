"""The MCP server: LuxSwirl's surface for AI agents, mounted at /mcp.

Ported from LuxWX's `app/mcp/` (itself LuxPM's). An agent authenticates with a
user's `lsw_` API token and acts as that user, narrowed by the token's scopes.
"""
