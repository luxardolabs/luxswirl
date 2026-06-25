# LuxSwirl

<div align="center">

**Self-hosted, multi-agent uptime and infrastructure monitoring.**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0) [![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/) [![FastAPI](https://img.shields.io/badge/FastAPI-0.104+-green.svg)](https://fastapi.tiangolo.com) [![TimescaleDB](https://img.shields.io/badge/TimescaleDB-2.x-orange.svg)](https://www.timescale.com)

[Features](#features) • [Quick start](#quick-start) • [Documentation](#documentation) • [License](#license)

</div>

---

## What is LuxSwirl?

LuxSwirl is a self-hosted, open-source uptime and infrastructure monitoring platform. It runs checks from one or more agents, stores results in TimescaleDB, and presents them on a real-time dashboard with alerting and public status pages.

A Luxardo Labs project, released as an open-source give-back under AGPLv3.

### A note on scope

The server is a single point of failure: there's no high availability or automatic failover, so if the server is down, no alerts fire. Agents buffer their results locally and replay them when the server recovers, so historical data stays complete — but real-time alerting during a server outage isn't guaranteed. For mission-critical alerting, run LuxSwirl alongside a tool built for that, and/or monitor the server itself with an external service. The [Limitations](docs/reference/limitations.md) doc has the full, honest rundown.

---

## Features

### Distributed multi-agent monitoring

Run checks from agents deployed across networks, regions, and private LANs, all reporting to one server.

- Monitor services from where your users actually are, with real per-location latency.
- Reach inside isolated networks that no single central host could see.
- Keep monitoring the rest of your estate when any one agent goes offline.

```
┌──────────────────────────────┐
│  LuxSwirl Server             │
│  - Aggregates agent results  │
│  - TimescaleDB storage       │
│  - Web UI + REST API         │
│  - Alerting engine           │
└──────────────────────────────┘
     ▲        ▲        ▲
     │        │        │
┌────┴───┐ ┌──┴───┐ ┌──┴─────┐
│ Agent  │ │ Agent│ │ Agent  │
│ (cloud)│ │ (LAN)│ │ (edge) │
└────────┘ └──────┘ └────────┘
```

### Eight check types

One platform covers web endpoints, APIs, network reachability, ports, DNS, databases, and full browser flows — so most of what you run is monitored in one place instead of stitched across tools.

| Type | What it does |
|------|--------------|
| **HTTP/HTTPS** | Status code, latency, optional content checks |
| **JSON** | Validate a JSON response with a JSONata query |
| **Ping (ICMP)** | Network reachability |
| **TCP** | Port connectivity |
| **DNS** | Record lookups (A, AAAA, MX, TXT, etc.) |
| **MySQL** | Run a query against MySQL/MariaDB |
| **PostgreSQL** | Run a query against PostgreSQL |
| **Synthetic (Playwright)** | Browser automation (admin-only — see below) |

> ⚠️ **Synthetic checks execute arbitrary Python on agent hosts.** They are admin-role-only by design and intended for trusted, self-hosted deployments. Treat them like cron jobs — anyone who can create one can run code as the agent user. AST validation blocks obvious attacks but is not a sandbox. Vet admin accounts carefully and review synthetic check code before deploying it. See [SECURITY.md](SECURITY.md) for the full trust model.

### Time-series storage on TimescaleDB

Every result is stored as time-series data with native compression, configurable retention, and continuous aggregates (5-minute, hourly, and daily rollups).

- Keep long-horizon history without unbounded disk growth.
- Dashboards and charts stay fast even over large result volumes, because they read pre-rolled aggregates.

### Real-time dashboard

- Live HTMX updates — no page refresh — so status changes show up as they happen.
- Per-check status timelines and latency charts; click any check to drill into its history and metrics.
- Filter by agent, type, status, and tags; paginated for large fleets.

### Alerting that stays quiet until it matters

- De-duplicated: one DOWN alert per incident and one UP on recovery — not one per failed poll.
- Consecutive-failure thresholds ride out transient blips.
- Dependency-aware suppression: a check can declare a parent, and while the parent is down its children's alerts are suppressed — so one upstream outage doesn't fan out into noise.
- SSL-expiry warnings and snooze windows for planned maintenance.

### Notifications

Email (SMTP), generic webhooks, and Home Assistant out of the box. Webhooks bridge to anything that accepts an HTTP POST — Slack, Discord, or a custom endpoint.

### Public status pages

Slug-based, customizable pages with service grouping and per-page visibility — share live service health with users without exposing your dashboard.

### Background jobs

Run one-time tasks on an agent that return structured results — distinct from checks, which run continuously. Scan a subnet to discover active hosts and their open ports, or enumerate an agent's network interfaces, to turn up new monitoring targets without hunting for them by hand.

### Configuration as data

Import and export checks as JSON: version-control your monitoring config, back it up, or promote it from one environment to the next.

### Built to operate

Prometheus metrics at `/metrics`, an OpenAPI-documented REST API (`/docs`, `/redoc`), role-based access (admin/editor/viewer), agent registration keys, and agent credentials encrypted at rest (Fernet).

---

## Quick start

**Prerequisites:** Docker 20.10+ and Docker Compose 2.0+, ~2 GB RAM, ~10 GB disk.

```bash
git clone https://github.com/luxardolabs/luxswirl.git
cd luxswirl

# Start the database + server. The agent needs a registration key first,
# so it's started separately in the next step.
docker compose up -d timescaledb luxswirl_server
```

Open `http://localhost:9000`. There are **no default credentials** — on first launch you're redirected to **`/setup`** to create your admin account (username + password). Then sign in.

Next, create a registration key (**Settings → Registration Keys → Create Key**), hand it to the agent, and start it:

```bash
echo 'LUXSWIRL_AUTH_KEY=<paste-your-key-here>' >> .env
docker compose up -d luxswirl_agent
```

Approve the agent on the **Agents** page, then create your first check. The full walkthrough — including creating checks via the UI or API, notifications, and status pages — is in the [Quickstart](docs/quickstart/quickstart.md).

> Prefer to build from source instead of pulling published images? `make build` builds the backend and agent images locally.

---

## Documentation

- **[Quickstart](docs/quickstart/quickstart.md)** — Docker Compose to first check.
- **[Installation](docs/deployment/installation.md)** — production deployment.
- **[User guide](docs/user-guide/)** — one document per feature: [Dashboard](docs/user-guide/dashboard.md), [Agents](docs/user-guide/agents.md), [Checks](docs/user-guide/checks.md), [Jobs](docs/user-guide/jobs.md), [Status Pages](docs/user-guide/status-pages.md), [Alerts](docs/user-guide/alerts.md), [Notifications](docs/user-guide/notifications.md), [Settings](docs/user-guide/settings.md), [Database Health](docs/user-guide/database-health.md), [Import/Export](docs/user-guide/import-export.md).
- **[Architecture](docs/architecture/overview.md)** — system design, agent model, and database schema.
- **[FAQ](docs/user-guide/faq.md)** and **[Limitations](docs/reference/limitations.md)** — honest answers about what it does and doesn't do.
- **API reference** — auto-generated Swagger UI at `/docs` and ReDoc at `/redoc` on a running server.

---

## Contributing

LuxSwirl is released as an open-source give-back, so there's no roadmap commitment and no expectation that you contribute. If you find it useful and want to send a fix or an improvement, you're welcome to — see [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports with clear reproduction steps are always helpful.

Questions and ideas are welcome in [GitHub Discussions](https://github.com/luxardolabs/luxswirl/discussions); bugs go in [GitHub Issues](https://github.com/luxardolabs/luxswirl/issues).

---

## License

LuxSwirl is licensed under the **GNU Affero General Public License v3.0 (AGPLv3)** — see [LICENSE](LICENSE). Use it, self-host it, modify it, and fork it freely. AGPL's network-copyleft is the one catch: if you run a *modified* version as a service for others, you have to make that source available too, and you can't take LuxSwirl closed-source and resell it as a hosted product. It's an open-source give-back — more in [Licensing](docs/reference/licensing.md).

---

## Built with

[FastAPI](https://fastapi.tiangolo.com) · [TimescaleDB](https://www.timescale.com) · [HTMX](https://htmx.org) · [Tailwind CSS](https://tailwindcss.com) · [Chart.js](https://www.chartjs.org) · [Playwright](https://playwright.dev)

Built by [Luxardo Labs](https://www.luxardolabs.com).
