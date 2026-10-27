# MCP server

LuxSwirl serves the [Model Context Protocol](https://modelcontextprotocol.io) at `/mcp`, so an AI agent (Claude, an IDE assistant, a script built on an MCP client) can ask what is down and why, read a check's history, and make the changes an operator would: create or edit a check, snooze an alert, run a network scan and turn its hosts into checks.

## Connecting

The server speaks Streamable HTTP at `https://<your LuxSwirl>/mcp/`. An agent authenticates with a personal API token, the same `lsw_` token the [admin API](admin-api.md) takes, created in **My Account** → **API Tokens**:

```json
{
  "mcpServers": {
    "luxswirl": {
      "type": "http",
      "url": "https://luxswirl.example.com/mcp/",
      "headers": { "Authorization": "Bearer lsw_..." }
    }
  }
}
```

The token can also be sent as `X-API-Key: lsw_...`, for clients that take no `Authorization` header.

The agent acts as the token's user, narrowed by the token's [scopes](admin-api.md#scopes): it lists and may call only the tools both allow. Give an agent a token made for it, with the least access it needs; **Read only** is enough to diagnose.

## Tools

Call `whoami` first: it names the token's user, role and scopes, and the tools it may call.

| Tool                                          | Scope                           | Role   | Does                                                                  |
| --------------------------------------------- | ------------------------------- | ------ | --------------------------------------------------------------------- |
| `whoami`                                      | none                            | any    | The token's user, role, scopes and callable tools                     |
| `list_checks`, `get_check`                    | `checks:read`                   | any    | Checks, filtered by agent, type, tag, enabled or search               |
| `get_check_results`                           | `checks:read`                   | any    | A check's raw results in a time window, newest first                  |
| `get_check_history`, `get_check_summary`      | `checks:read`                   | any    | A check's results over the last hours, bucketed or counted            |
| `create_check`, `update_check`, `clone_check` | `checks:write`                  | editor | Create, change or copy a check (a synthetic check is an admin's)      |
| `enable_check`, `disable_check`               | `checks:write`                  | editor | Turn a check on or off                                                |
| `list_agents`, `get_agent`                    | `agents:read`                   | any    | Agents, their status and last heartbeat                               |
| `list_alerts`, `get_alert`                    | `alerts:read`                   | any    | Alert rules, with their providers and checks                          |
| `snooze_alert_check`, `unsnooze_alert_check`  | `alerts:write`                  | any    | Hold back, or resume, an alert's notifications for one check          |
| `list_jobs`, `get_job`, `list_job_types`      | `jobs:read`                     | any    | Network scan and discover jobs, their results, and each type's params |
| `create_job`                                  | `jobs:write`                    | editor | Queue a scan or discovery on an agent                                 |
| `create_checks_from_job`                      | `jobs:write` and `checks:write` | editor | Make ping, web, ssh or database checks from a scan's hosts            |
| `get_maintenance_job`                         | `jobs:read`                     | any    | A background job's status and progress                                |

A tool that changes something takes the same body as its admin-API route, and is held to the same rules: a viewer reads every credential masked, a synthetic check is an admin's, an unknown id is an error. Deletes, users, settings, API and registration keys, import and the scheduler are not served over MCP; they stay in the web UI and the admin API.

A refused call comes back as a tool error whose text starts `FORBIDDEN:` and names the role or scope it needs; a bad argument comes back as an error naming what is valid.

## Limits

| Limit           | Value                                                                                                                     |
| --------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Calls per token | The admin API's per-token limit (`security.admin_api_rate_limit`, 300 a minute by default); then `429` with `Retry-After` |
| Failed tokens   | 10 a minute per client address, before the token is checked; a token proven in the last minute is not counted             |

A missing, unknown, revoked or expired token gets `401` with a `WWW-Authenticate: Bearer` challenge; the answer is the same for each, so it does not tell which.

## Audit log

Every call is recorded, whether it succeeded, failed or was refused: the tool, the user and token, the arguments with every credential masked (a synthetic script by its size alone), the outcome, why it was refused, how long it took, and the caller's address and client name. Calls are kept 90 days.
