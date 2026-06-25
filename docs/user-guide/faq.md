# Frequently Asked Questions (FAQ)

## What is LuxSwirl?

LuxSwirl is a self-hosted, open-source uptime and infrastructure monitoring tool. It runs checks (HTTP, JSON, ping, TCP, DNS, database queries, and Playwright browser flows) from one or more agents, stores results in TimescaleDB, and shows them on a dashboard with alerting and public status pages.

A Luxardo Labs project, released as an open-source give-back under AGPLv3.

## What check types are supported?

Eight functional check types:

| Type | What it does |
|------|--------------|
| **ping** | ICMP reachability |
| **http** | HTTP/HTTPS status, latency, optional content checks |
| **tcp** | TCP port connectivity |
| **json** | Validate a JSON response with a JSONata query |
| **dns** | DNS record lookups (A, AAAA, MX, TXT, etc.) |
| **mysql** | Run a query against MySQL/MariaDB |
| **postgres** | Run a query against PostgreSQL |
| **synthetic** | Playwright browser automation (admin-only — see security below) |

See the [Checks guide](checks.md) for details.

## How do I install it?

Docker Compose is the quickest path:

```bash
git clone https://github.com/luxardolabs/luxswirl.git
cd luxswirl
docker compose up -d
```

This starts TimescaleDB, the server (UI + API on port 9000), and one agent. Open http://localhost:9000.

See the [Quickstart](../quickstart/quickstart.md) for the full walkthrough.

## Can I run it without Docker?

Yes, but it's more work. You'll need Python 3.14+ and PostgreSQL with the TimescaleDB extension, then install each component with Poetry and run the server and agent processes yourself. Docker Compose is recommended. See the [Installation guide](../deployment/installation.md).

## Do I need to know Python?

No. All configuration is done through the web UI. Python is only relevant if you want to contribute code or write custom check types. (Synthetic checks let you write Playwright scripts, but those are an admin-only feature, not required for normal use.)

## How do alerts and notifications work?

Alerts fire on status changes (UP → DOWN, DOWN → UP) and are de-duplicated, so a check that fails 50 times in a row produces one DOWN alert and one UP alert when it recovers — not 50.

Dependency-aware suppression: if a check declares a parent and the parent is down, the child's alerts are suppressed until the parent recovers.

Three notification providers are built in:

- **Email** (SMTP)
- **Webhook** (generic HTTP POST — use this to bridge to anything else)
- **Home Assistant**

See the [Notifications guide](notifications.md).

## Is it secure?

A few things worth knowing:

- **Synthetic checks run arbitrary Python**, so they're **admin-only**. Scripts are **AST-validated** before they're allowed (blocking obvious things like `eval`, `exec`, and dangerous imports). This validation is a guardrail, not a sandbox — treat the ability to create synthetic checks as equivalent to host access, and only grant it to trusted admins. LuxSwirl is designed for self-hosted, single-organization use, not untrusted multi-tenancy.
- **Credentials are encrypted at rest** (Fernet) — database passwords, agent credentials, and similar secrets are not stored in plaintext.
- Standard web-app practices apply: hashed passwords, session auth, role-based access (admin/editor/viewer), and bearer-token auth between agents and server.

For production, put the server behind HTTPS (reverse proxy) and keep the database off the public internet.

## What's the license?

AGPLv3, free and open source. Use it anywhere — home, company, commercial — and self-host and modify it freely. The only real restriction is AGPL's network-copyleft: you can't take it closed-source and resell it as a hosted/SaaS product. See [Licensing](../reference/licensing.md).

---

**Still have questions?** Open a GitHub Discussion: https://github.com/luxardolabs/luxswirl/discussions
