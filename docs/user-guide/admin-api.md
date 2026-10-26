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

## OpenAPI document

`/api/v1/admin/openapi.json` describes every admin-API route, with the bearer scheme declared. It is served to any token holder in every environment, so a client generator or an AI agent can read it. The app-wide `/docs` and `/openapi.json` stay off in production.

## Rate limit

Each token may make `security.admin_api_rate_limit` requests (default `300/minute`, env `SECURITY__ADMIN_API_RATE_LIMIT`). The limit is per token, not per address, so several scripts behind one address do not share it. Past the limit, a request gets `429`.
