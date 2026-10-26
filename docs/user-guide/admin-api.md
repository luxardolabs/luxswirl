# Admin API

The admin API lets scripts and tools manage LuxSwirl over HTTP, under `/api/v1/admin`. Each call carries a personal API token, and the token acts as the user who made it: it can do what that user can do in the web UI, and nothing more.

## API tokens

| Action | Where                                                      | Notes                                                                                  |
| ------ | ---------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Create | **My Account** panel → **API Tokens**                      | Give it a name (what it is for) and an expiry: 30 days, 90 days, 1 year or never.      |
| Copy   | Shown once, right after creating it                        | Only a hash is stored. A lost token cannot be recovered; revoke it and create another. |
| Revoke | **My Account** → **API Tokens** → **Revoke** (click twice) | Takes effect on the next request.                                                      |

The token list shows each token's name, its first characters (`lsw_` and 8 more), when it was created and last used, and when it expires.

A token stops working when any of these is true:

- it is revoked, or past its expiry;
- its user changes their password, or an admin resets it (every token of that user is revoked, as their sessions end);
- its user must change their password before using LuxSwirl again;
- its user is deactivated, or deleted (the token is deleted with them).

A login lockout (too many wrong passwords) does not stop a token: the lockout guards the password, and anyone who knows a username can trigger one.

A token carries its user's role at the time of each request, so changing a user's role changes what their tokens may do.

## Calling the API

Send the token as a bearer token:

```bash
curl -H "Authorization: Bearer lsw_..." https://luxswirl.example.com/api/v1/admin/me
```

```json
{
  "user_id": "3f6c…",
  "username": "alice",
  "role": "editor",
  "token_id": "9b21…",
  "token_name": "ci",
  "token_expires_at": "2027-01-24T10:00:00Z"
}
```

| Method | Path                         | Role | Returns                                     |
| ------ | ---------------------------- | ---- | ------------------------------------------- |
| `GET`  | `/api/v1/admin/me`           | any  | The token's user, their role, and the token |
| `GET`  | `/api/v1/admin/openapi.json` | any  | The admin API's OpenAPI document            |

A missing, unknown, revoked or expired token gets `401` with `WWW-Authenticate: Bearer`. A valid token whose user lacks the role a route needs gets `403`. A browser session cookie does not authenticate the admin API.

## Checks

A check is addressed by its own id. Reads take any token; writes take an editor's or an admin's. Any write to a synthetic check (create, change, clone, enable, disable, delete) is an admin's alone, and only a synthetic check keeps a `script_code`.

| Method   | Path                                | Role   | Does                                                                               |
| -------- | ----------------------------------- | ------ | ---------------------------------------------------------------------------------- |
| `GET`    | `/api/v1/admin/checks`              | any    | One page of checks (see below)                                                     |
| `GET`    | `/api/v1/admin/checks/{id}`         | any    | One check                                                                          |
| `POST`   | `/api/v1/admin/checks`              | editor | Create a check: the check's fields plus `agent_id`; `201`                          |
| `PATCH`  | `/api/v1/admin/checks/{id}`         | editor | Change the fields sent                                                             |
| `DELETE` | `/api/v1/admin/checks/{id}`         | editor | Delete it; `204`                                                                   |
| `POST`   | `/api/v1/admin/checks/{id}/clone`   | editor | Copy it; the body (optional) holds fields to change and a target `agent_id`; `201` |
| `POST`   | `/api/v1/admin/checks/{id}/enable`  | editor | Enable it                                                                          |
| `POST`   | `/api/v1/admin/checks/{id}/disable` | editor | Disable it                                                                         |

The list takes `page` (from 1) and `per_page` (1-200, default 50), and filters `agent_id`, `check_type`, `tag`, `search` (name or target) and `enabled_only`. It answers with the page envelope every admin-API list uses:

```json
{"items": [...], "total": 120, "page": 1, "per_page": 50, "total_pages": 3, "has_next": true}
```

Walk the pages with `page` until `has_next` is false.

A create or change refuses what the web UI refuses, with the same message: an address the network protection blocks, an unknown agent (`404`), an agent awaiting approval, rejected or archived, a synthetic script the security validation refuses, a field the API does not take, a required field set to `null`, or an invalid value (`422`). A clone keeps its source's dependency unless the body sets `depends_on_check_id`.

A viewer's token reads no credential in a check: the user and password of any URL in `target` or `connection_string` read `***` (`postgresql://***@db:5432/app`), as does any `password=` value, and a synthetic check's `script_code` is left out. An editor's or an admin's token reads them as stored.

### Bulk actions

Each takes `{"check_ids": [...]}`, 1 to 10,000 ids; a modify adds the fields to change.

| Method | Path                                | Role   | Does                                                                                                                      |
| ------ | ----------------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------- |
| `POST` | `/api/v1/admin/checks/bulk/enable`  | editor | Enable them now; returns `{"success_count", "failure_count", "errors"}`                                                   |
| `POST` | `/api/v1/admin/checks/bulk/disable` | editor | Disable them now; same answer                                                                                             |
| `POST` | `/api/v1/admin/checks/bulk/delete`  | editor | Delete them and their results in the background; `202` with the job                                                       |
| `POST` | `/api/v1/admin/checks/bulk/modify`  | editor | Set `interval_seconds`, `timeout_seconds`, `retry_attempts`, move to `agent_id`, or assign `alert_id`; `202` with the job |

The whole selection is judged before anything runs: an id no check has refuses it (`422`), and so does a synthetic check in the selection of anyone but an admin (`403`). Nothing is changed or queued. A modify must set at least one field, and none to `null`. An unknown `agent_id` or `alert_id` is a `404`, and moving checks to an agent awaiting approval, rejected or archived is a `422`.

### Background jobs

| Method | Path                                  | Role | Returns                           |
| ------ | ------------------------------------- | ---- | --------------------------------- |
| `GET`  | `/api/v1/admin/maintenance-jobs/{id}` | any  | The job's `status` and `progress` |

`status` goes `queued` → `running` → `done` or `failed`. Once a bulk job has run, done or failed, `progress` holds `success_count`, `failure_count` and the first 50 `errors`, so a check deleted before the job ran shows as a failure rather than disappearing. A job that changed no check is `failed`.

A job runs after the request that queued it, so its owner is judged again when it runs: if they are no longer an active editor or admin, the job fails and changes nothing. A failed job's `error` is a summary, never the database statement or a credential; the full error is in the server log.

### Dependents

A check can depend on one parent; while the parent is failing, the dependent's alerts are suppressed (see [Alerts → Dependency suppression](alerts.md#dependency-suppression)).

| Method | Path                                   | Role   | Does                                                                             |
| ------ | -------------------------------------- | ------ | -------------------------------------------------------------------------------- |
| `GET`  | `/api/v1/admin/checks/{id}/dependents` | any    | The checks that depend on this one (masked for a viewer, as above)               |
| `PUT`  | `/api/v1/admin/checks/{id}/dependents` | editor | `{"dependent_ids": [...]}` becomes the whole set; returns `{"added", "removed"}` |

A replacement refuses a parent that has its own parent, a dependent that is itself a parent, and a check depending on itself (`422`). Attaching or detaching a synthetic check is an admin's.

### Results, history and summary

Any token reads them.

| Method | Path                                         | Returns                                                                                             |
| ------ | -------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `GET`  | `/api/v1/admin/checks/{id}/results`          | One page of the check's results, newest first, in the page envelope                                 |
| `GET`  | `/api/v1/admin/checks/{id}/results/{result}` | One result whole: its `response_data` and its artifacts' metadata (name, type, size, not the bytes) |
| `GET`  | `/api/v1/admin/checks/{id}/history`          | `points`: the results over the last `hours` (default 24), in time buckets, at most 500              |
| `GET`  | `/api/v1/admin/checks/{id}/summary`          | `total`, `successful`, `uptime_percent` and latency over the last `hours`, and the `latest` result  |

The results page reads the last 24 hours unless `since` and `until` (ISO 8601; a time with no zone is UTC) say otherwise. The window must run forward and span at most 31 days (`422`). It takes `page` and `per_page` (1-200, default 50). `hours` runs from 1 to 720. A result is read through its own check: one of another check is a `404`.

Each result carries `success`, `latency_ms`, `error`, `error_type`, `http_status_code`, `http_response_time_ms` and the check type's own `metrics` (DNS records, row counts, response headers, ...). A viewer's token reads every `error` with its credentials masked, as above, and no `response_data`. Of `metrics` it reads only what the check detail panel shows (the `dns`, `mysql`, `postgres` and `synthetic` sections and `response.ssl_certificate`), every text in them masked the same way: an HTTP check's response headers, cookies among them, are an editor's to read.

## Agents

An agent is addressed by its own id. Reads take any token; every change takes an editor's or an admin's, as in the web UI. A change that moves a synthetic check to another host (a rename or retag into or out of its selector, an approval it already matches) or removes one (deleting the agent that runs it) is an admin's (`403`).

| Method   | Path                                       | Role   | Does                                                                                              |
| -------- | ------------------------------------------ | ------ | ------------------------------------------------------------------------------------------------- |
| `GET`    | `/api/v1/admin/agents`                     | any    | One page of agents; `search` (name, hostname, address), `archived`, `exclude_pending`             |
| `GET`    | `/api/v1/admin/agents/{id}`                | any    | One agent; `has_api_key` says whether it has a key, never the key or its hash                     |
| `PATCH`  | `/api/v1/admin/agents/{id}`                | editor | Change its name, tags and configuration: the web edit form's fields                               |
| `DELETE` | `/api/v1/admin/agents/{id}`                | editor | Delete it, its checks and their results in the background; `202` with the job                     |
| `POST`   | `/api/v1/admin/agents/{id}/{action}`       | editor | `approve`, `reject`, `pause`, `resume`, `disable`, `enable`, `archive`, `restore`, `force-reload` |
| `POST`   | `/api/v1/admin/agents/{id}/key`            | editor | A first key for an agent with none; `201` with `api_key`, shown this once                         |
| `POST`   | `/api/v1/admin/agents/{id}/key/regenerate` | editor | A new key in place of the current one, which stops working; `201`, shown once                     |
| `DELETE` | `/api/v1/admin/agents/{id}/key`            | editor | Revoke the key: the agent cannot connect until it has another; `204`                              |

The agent reports its own `agent_run_id`, `ip_address` and `version`: a change to them, or to any field the edit form does not have, is a `422`, and a name another agent already has is a `409`. `reject`, `pause` and `disable` take an optional `{"reason": "..."}`, kept as `status_reason`. An action the agent's status does not allow (approving an active agent, resuming one that is not paused) is a `422`. `approve` returns no key: the agent fetches its own with its recovery secret, as after a web approval. `key` on an agent that has one is a `400`; use `key/regenerate`.

A delete is refused when queued and judged again when it runs: if its owner is no longer an active editor or admin by then, the job fails and nothing is deleted. Poll it at `/api/v1/admin/maintenance-jobs/{id}`.

## OpenAPI document

`/api/v1/admin/openapi.json` describes every admin-API route, with the bearer scheme declared. It is served to any token holder in every environment, so a client generator or an AI agent can read it. The app-wide `/docs` and `/openapi.json` stay off in production.

## Rate limit

Each token may make `security.admin_api_rate_limit` requests (default `300/minute`, env `SECURITY__ADMIN_API_RATE_LIMIT`). The limit is per token, not per address, so several scripts behind one address do not share it. Past the limit, a request gets `429`.
