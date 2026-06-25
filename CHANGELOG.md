# Changelog

All notable changes to the LuxSwirl monitoring platform will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.21] - 2026-06-25

Initial public release of **LuxSwirl** — a self-hosted observability and uptime-monitoring platform.

### Added

- **Server** (FastAPI + HTMX): versioned REST API (`/api/v1`), a real-time HTMX dashboard, public and private status pages, Prometheus metrics (`/metrics`), and session-based authentication with role-based access control (viewer / editor / admin).
- **Agent**: distributed health checks — ping (ICMP), HTTP/HTTPS, TCP, JSON (JSONata queries, Uptime Kuma compatible), DNS, MySQL, PostgreSQL, and synthetic browser checks (Playwright, admin-only).
- **Time-series storage**: TimescaleDB hypertables with continuous aggregates, compression, and configurable retention.
- **Alerting & notifications**: multi-channel notifications, SSL-certificate-expiry monitoring with threshold escalation, and parent/child check-dependency suppression.
- **Security**: encrypted credentials at rest, SSRF protection on check targets and webhooks, CSRF protection, rate limiting, and password-complexity enforcement. See [`SECURITY.md`](SECURITY.md).

See the [README](README.md) and [`docs/`](docs/) for full features, configuration, and deployment.
