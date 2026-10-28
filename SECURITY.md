# Security Policy

This document outlines LuxSwirl's security model, known vulnerabilities, and responsible disclosure policy.

______________________________________________________________________

## Reporting a Vulnerability

### Responsible Disclosure

**Please report security vulnerabilities privately**. Do NOT open public GitHub issues for security concerns.

**Contact**: reach the maintainers privately through [www.luxardolabs.com](https://www.luxardolabs.com).

**Include in your report**:

1. **Vulnerability description**: What is the security issue?
1. **Impact assessment**: What can an attacker do?
1. **Steps to reproduce**: Detailed reproduction steps
1. **Proof of concept**: Code, screenshots, or video
1. **Suggested fix**: If you have a proposed solution (optional)

### Response

LuxSwirl is open source and maintained on a best-effort basis — there's no guaranteed response SLA. Reports are acknowledged and triaged as soon as the maintainers can, and a fix is released and disclosed once one is available.

______________________________________________________________________

## Supported Versions

| Version | Supported              |
| ------- | ---------------------- |
| 1.0.x   | ✅ Yes (current)       |
| 0.9.x   | ⚠️ Security fixes only |
| < 0.9   | ❌ No                  |

______________________________________________________________________

## Security Model

### Designed For

- ✅ Self-hosted deployments (single organization)
- ✅ Trusted environments (internal networks)
- ✅ Known users (vetted team members)

### NOT Designed For

- ❌ Multi-tenant SaaS
- ❌ Public-facing deployments
- ❌ Untrusted users

______________________________________________________________________

## Known Security Considerations

### 1. Synthetic Checks (Arbitrary Code Execution)

**Risk**: HIGH\
**Status**: ⚠️ BY DESIGN

Synthetic checks execute arbitrary Python code. Administrators can run any code on agent hosts.

**Mitigations**:

- Admin role required — enforced in the core service (`CheckCoreService.create_check`/`update_check`/`clone_check`), not only in the web layer.
- AST validation (blocks obvious attacks) — hardening, **not** the access control.
- Audit logging
- UI warning banner

**Authorization model**: Synthetic checks are created and edited only through the web UI, which enforces the user's RBAC role (`viewer`/`editor`/`admin`). The JSON API's agent protocol (`/api/v1/agent`) offers no route that creates or edits checks.

**Recommendations**:

- Only use in trusted, self-hosted environments
- Vet admin accounts carefully
- Review synthetic check code before deployment

### 2. Status Page Custom CSS

**Risk**: MEDIUM\
**Status**: ⚠️ BY DESIGN

Public status pages support administrator-supplied custom CSS for branding (`status_page.config.custom_css`). This CSS is rendered into a `<style>` block on the public status page without server-side sanitization. A malicious or compromised admin could inject markup that breaks out of the style block and executes script in visitors' browsers.

**Trust model**: same as synthetic checks. Anyone with the admin role can already run arbitrary code via synthetic checks; custom CSS is no different in scope. The audience is self-hosted single-organization deployments where the admin and operator are the same trust principal.

**Mitigations**:

- Admin role required to modify status page config
- Status pages must be explicitly published before being publicly visible
- All admin actions logged

**Recommendations**:

- Vet admin accounts carefully (same advice as synthetic checks)
- Treat custom CSS like custom JavaScript — review before deployment

### 3. Data Protection

**At Rest**:

- ✅ Password hashing (bcrypt)
- ✅ Admin-API tokens stored as SHA-256 hashes only, shown once at creation
- ✅ Check credentials kept from viewers: the check forms (which edit the stored connection string and synthetic script) are editor-only, every page that displays a check's target masks any credential in it (URL userinfo, `password=`/`pwd=`/`token=`/`api_key=` values), and so do the `/metrics` `target` label and alert notifications (email, webhook); the per-agent check export, which keeps every stored value so an import recreates the checks, is admin-only (`app/core/credential_mask.py`)
- ✅ Credential encryption (Fernet AES-128)

**In Transit**:

- ✅ HTTPS required for external agents
- ⚠️ Internal Docker network unencrypted (localhost only)

### 4. CSRF Protection

LuxSwirl does **not** use CSRF tokens. Cross-site request forgery is prevented by browser-level cookie controls instead:

- Session cookies are set with `SameSite=Lax` — modern browsers (Chrome 80+, Firefox 79+, Safari 13+, all 2020+) refuse to attach the cookie to cross-site `POST` / `PUT` / `PATCH` / `DELETE` requests, defeating CSRF before the request reaches our backend.
- Session cookies are set with `Secure=True` in production, restricting them to HTTPS.
- Session cookies are `HttpOnly`, so JavaScript on a compromised page cannot read or exfiltrate them.
- Agent-protocol endpoints (`/api/v1/agent/*`) authenticate via `Authorization: Bearer <key>` headers, not cookies — browsers never auto-attach those cross-site, so those endpoints are CSRF-immune by design. The admin API (`/api/v1/admin/*`) is the same: it takes a user's API token as a bearer header and ignores the session cookie.
- Content-Security-Policy includes `frame-ancestors 'none'`, preventing the application from being embedded in malicious iframes.

This is the same model used across Luxardo Labs FastAPI portals. Token-based CSRF (the older pattern) added significant code complexity without meaningfully improving on what browser cookie attributes already provide.

**Operator responsibility:** make sure your reverse proxy terminates HTTPS in production. Without `Secure=True` cookies, an attacker on the same network could downgrade the connection. The `session_cookie_secure` setting must be `True` for any deployment exposed beyond localhost.

### 5. Content-Security-Policy (`unsafe-eval`)

**Status**: ⚠️ BY DESIGN

The CSP `script-src` is `'self' 'unsafe-eval'`: scripts load only from the app's own files, and an inline `<script>` or `on*=` handler is refused, so an injected one does not run. `'unsafe-eval'` remains because Alpine.js (standard build) and htmx `hx-on` evaluate expressions written in template attributes, so a template injection that reaches one of those attributes can still execute script. `style-src` keeps `'unsafe-inline'` for the `style="--var: …"` custom properties that carry data values into CSS. The primary XSS defense remains Jinja2 auto-escaping and not rendering untrusted HTML.

### 6. Network Protection (SSRF)

**Status**: ✅ ENFORCED

Health checks and webhook notifications make outbound requests to operator-supplied targets, which is a Server-Side Request Forgery surface. The most sensitive target is the cloud-metadata endpoint (`169.254.169.254`), which on AWS/GCP/Azure serves the instance's IAM credentials — the classic SSRF prize (e.g. the 2019 Capital One breach).

- **Cloud-metadata / link-local is blocked by default** (`169.254.0.0/16`, IPv6 `fe80::/10`, AWS's IPv6 metadata address `fd00:ec2::254` and Alibaba Cloud's `100.100.100.200`), including the IPv6 forms that carry it (IPv4-mapped, 6to4, NAT64 `64:ff9b::/96`) and every host a database URL connects to (a PostgreSQL comma host list, `host` and `hostaddr` parameters, and localhost for a URL that names no host). On the agent this range is always enforced regardless of the server toggle.
- **Private and internal networks are *allowed* by default** (`security.block_private_networks`, default off): RFC 1918, IPv6 unique-local, loopback, 0.0.0.0 and CGNAT, because monitoring LAN hosts is the intended use. Operators in hardened environments can switch this on; the agents enforce it too, from the checks list and every heartbeat response.
- **Validation runs in two places**: on the server when a check or webhook is created/updated (every address a check connects to: its target, a DNS nameserver, a database connection string), and **again on the agent at fetch time** — immediately before each connection, and on every redirect hop for HTTP/JSON checks. The fetch-time check is what defeats DNS-rebinding and HTTP-redirect bypasses: a target that resolves to a safe IP at create time but later resolves (or 3xx-redirects) to the metadata endpoint is blocked at the moment the agent would connect. Ping, TCP and HTTP/JSON checks connect to the exact address they vetted (HTTPS still verifies the name), so nothing resolves the name again in between, and a host with both an allowed and a blocked address is reached on the allowed one; DNS and database checks, whose drivers resolve the name themselves, are refused when any address is blocked. Check traffic never uses a proxy from the agent's environment, which would resolve the name out of the policy's reach.
- **Network scan jobs** sweep a subnet the operator chooses, so they are held to a different line. A scan never reaches the cloud-metadata / link-local range: a subnet that overlaps it, directly or through an IPv6 form that carries it (IPv4-mapped, 6to4, NAT64), is refused when the job is created and again on the agent before any probe. A scan is not held to `security.block_private_networks`, because sweeping private networks is what it is for. Network discover jobs read the agent's own interfaces and gateway; they take no target.
- **Trust note**: like synthetic checks, an admin can disable the toggle or run a synthetic check to reach any address — SSRF protection is a guardrail against *editors* (and against the rebinding/redirect bypass), not against the trusted admin.

**Residual**: the agent validates the resolved IP immediately before connecting, closing the create→fetch window. A sub-millisecond TTL-0 rebind between that check and the socket's own resolution is not closed (it would require pinning the validated IP through TLS/SNI) and is tracked as future hardening. See `docs/reference/settings-reference.md` → Network Protection (SSRF).

______________________________________________________________________

## Security Best Practices

### For Administrators

- ✅ Use strong passwords (20+ characters)
- ✅ Deploy behind HTTPS reverse proxy
- ✅ Enable firewall (restrict to trusted IPs)
- ✅ Rotate agent and registration keys every 90 days
- ✅ Review synthetic check code carefully

### For Deployments

- ✅ Use Docker secrets for credentials
- ✅ Restrict database to localhost
- ✅ Enable PostgreSQL SSL (if remote database)
- ✅ Regular backups
- ✅ Keep LuxSwirl updated

______________________________________________________________________

## Database Migrations

Schema is managed by Alembic. The server container runs `alembic upgrade head` before starting uvicorn, so migrations apply automatically on every deploy.

**For new deployments:** `docker compose up -d` creates an empty database, the server container's CMD chain runs the baseline migration to populate the schema, TimescaleDB-specific setup (hypertables, compression, retention) follows, and the app starts. No manual operator steps.

**Failed migrations** exit the container loudly. The app process never starts on a half-migrated database. Operators see the failure in `docker logs luxswirl_server` and can roll back the image to a previous release.

**Backups before upgrades:** any LuxSwirl release that ships a new migration should be tested against a copy of your database before applying to production.

______________________________________________________________________

## Contact

- **Security issues**: report privately through [www.luxardolabs.com](https://www.luxardolabs.com) — please don't open a public issue.
- **General questions**: [GitHub Discussions](https://github.com/luxardolabs/luxswirl/discussions)
