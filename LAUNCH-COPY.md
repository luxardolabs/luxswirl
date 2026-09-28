# LuxSwirl — Launch & Marketing Copy

Draft copy for the v1.0.21 public launch: social posts, the feature→benefit→value scaffold, and full website page copy. Written in the LuxSwirl docs voice — third person, value-first, no cost-comparisons or competitor scoreboards.

Repo: https://github.com/luxardolabs/luxswirl · Site: https://www.luxardolabs.com/

______________________________________________________________________

## 1. Social posts

### Facebook (casual, personal)

> 🚀 Two weeks heads-down and it's out: I just open-sourced **LuxSwirl** — a self-hosted platform for monitoring uptime and infrastructure from everywhere at once.
>
> Instead of checking your services from one server, LuxSwirl runs little agents wherever you want them — different regions, clouds, even private networks — all feeding one live dashboard. It watches basically everything (websites, APIs, databases, even full browser click-throughs), keeps the history, sends alerts, and gives you clean public status pages. Free, open source, and it runs on anything from a Raspberry Pi to a cloud box — and your data stays *yours*.
>
> Pretty proud of this one 👇 github.com/luxardolabs/luxswirl

### LinkedIn (professional, "why I built it")

> Today I'm releasing **LuxSwirl** as open source — a self-hosted, multi-agent uptime and infrastructure monitoring platform, under AGPLv3.
>
> **Why:** most monitoring watches your stack from a single vantage point. Real systems don't live in one place — they span regions, clouds, and private networks. LuxSwirl flips that around: you deploy lightweight agents wherever you need visibility, and they all report to one real-time server.
>
> **What it does:** • One agent, every check type — ping, HTTP, TCP, DNS, JSON APIs, MySQL/Postgres, and scripted browser flows. Servers, databases, and real user journeys in one tool. • TimescaleDB-backed history, alerting, public status pages, and native Prometheus export. • Self-hosted and open — your data stays on your hardware, from a Raspberry Pi to a cloud VM (multi-arch images).
>
> It also ships the parts that usually get skipped: role-based access, encrypted credentials, SSRF/CSRF protection, rate limiting, and a documented security model.
>
> If you run infrastructure and want monitoring you actually own, I'd love your feedback. 👉 github.com/luxardolabs/luxswirl
>
> #opensource #monitoring #observability #selfhosted #devops #sre #infrastructure

______________________________________________________________________

## 2. Feature → Benefit → Value scaffold

Reuse these rows as sections or feature cards.

| Feature (what it is)                                      | Benefit (what it does)                                        | Value (why it matters)                         |
| --------------------------------------------------------- | ------------------------------------------------------------- | ---------------------------------------------- |
| Distributed agents across regions / clouds / private LANs | Check from where users are; reach networks one host can't see | Real per-location truth, no blind spots        |
| One agent, every check type (ping → DBs → browser flows)  | Uptime, infra, databases, and user journeys in one tool       | Consolidate — fewer things to run and learn    |
| Self-hosted, open source, multi-arch (Pi → cloud)         | Your data on your hardware, runs anywhere                     | Ownership and control, no lock-in              |
| TimescaleDB history + status pages + Prometheus export    | Fast queries, public status, fits your stack                  | Polished *and* plugs into what you already use |

______________________________________________________________________

## 3. Website page copy

### Hero

# Monitoring you own — from everywhere your users are.

LuxSwirl is a self-hosted, multi-agent platform for uptime and infrastructure monitoring. Deploy lightweight agents across your regions, clouds, and private networks, and watch everything from one real-time dashboard. Open source, AGPLv3.

`[ Quick start ]` `[ View on GitHub ]` `[ Read the docs ]`

*Runs anywhere from a Raspberry Pi to a cloud VM. Your data never leaves your hardware.*

______________________________________________________________________

### Section 1 — See your stack from everywhere

## One vantage point isn't enough.

Your services don't live in one place — they span regions, cloud accounts, and private networks. LuxSwirl puts eyes wherever you need them. Deploy lightweight agents close to your users and deep inside isolated networks, and they all report back to a single server. You see real latency from each location, catch problems that only appear from certain regions, and reach systems no central host could touch — and when one agent goes offline, the rest keep watching.

- **Per-location latency** — see what users in each region actually experience.
- **Reach private networks** a central monitor can't see.
- **Resilient coverage** — lose an agent, keep monitoring everything else.

______________________________________________________________________

### Section 2 — Check everything, in one tool

## From a ping to a full browser journey.

Every LuxSwirl agent speaks the full range of checks, so you don't need a different tool for each layer of your stack. Confirm a host is up, an API returns the right JSON, a database answers a query, a certificate isn't about to expire, or that a real user can log in and finish a checkout — all from the same place.

- **Network & transport:** ping (ICMP), TCP port, DNS records.
- **Web & APIs:** HTTP/HTTPS, JSON validation with JSONata queries (Uptime Kuma–compatible).
- **Databases:** MySQL and PostgreSQL query checks.
- **Real user journeys:** scripted browser flows with Playwright (admin-only).

______________________________________________________________________

### Section 3 — Own your data

## Self-hosted, open, and yours.

LuxSwirl runs on your hardware, not someone else's cloud. Every result, every credential, every status page stays inside your infrastructure. It's open source under AGPLv3 — inspect it, extend it, run it however you like — with multi-architecture images that run the same from a Raspberry Pi to a cloud VM.

- **Self-hosted:** your monitoring data stays on your infrastructure.
- **Open source (AGPLv3):** inspect it, extend it, trust it.
- **Multi-arch (amd64 + arm64):** the same image, edge to cloud.

______________________________________________________________________

### Section 4 — Built in, not bolted on

## A platform, not a script.

LuxSwirl ships the parts that usually take months to add. Results stream into TimescaleDB for fast historical queries and continuous aggregates, so dashboards stay quick as your data grows. Turn any check into a public or private status page. Export every metric to Prometheus from a single endpoint. And security comes standard — not as an afterthought.

- **Time-series storage:** TimescaleDB with continuous aggregates, compression, and retention.
- **Status pages:** public or private, from any check.
- **Prometheus export:** one endpoint into your existing observability.
- **Secure by default:** role-based access, encrypted credentials, SSRF/CSRF protection, and rate limiting — with a documented security model.

______________________________________________________________________

### How it works

## Three moving parts. One clear picture.

1. **Agents run your checks.** Deploy them anywhere; each runs on its own schedule and buffers results locally if the server is unreachable.
1. **The server aggregates.** Results land in TimescaleDB, ready for queries, alerts, and rollups.
1. **You see and act.** A real-time dashboard, multi-channel alerts, and status pages keep everyone informed.

*(Place the agent → server → dashboard diagram here — adapt the ASCII one from the README.)*

______________________________________________________________________

### Where LuxSwirl fits

## Honest about what it is.

LuxSwirl is built for teams who want to own their monitoring. The server is a single point of failure — there's no automatic failover, so if it's down, real-time alerting pauses. Agents buffer their results and replay them when it recovers, so your history stays complete. For mission-critical paging, run LuxSwirl alongside a dedicated alerting service and monitor the server itself with an external check. We'd rather say so up front.

______________________________________________________________________

### Footer CTA

## Spin it up in five minutes.

Clone the repo, start the stack, point an agent at your services.

`[ Quick start ]` `[ GitHub ]` `[ Docs ]`

```
docker pull ghcr.io/luxardolabs/luxswirl-backend:1.0.21
docker pull ghcr.io/luxardolabs/luxswirl-agent:1.0.21
```
