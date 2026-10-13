# LuxSwirl Deployment Guide

This document explains how to deploy LuxSwirl using Docker Compose with environment-specific overlays.

## Docker Compose Files

LuxSwirl ships one compose file. Environments differ by the environment file they are given, not by an overlay:

- **`compose.yml`** - The stack. Builds nothing; runs the tag `TAG` names.
- **`.env.dev` / `.env.prod`** - What differs per environment: the image tag, the database host binding, the TLS certificate directory, CORS, log level and secrets. Copy `.env.example` and fill it in; neither is committed.
- **`compose.agent.yaml`** - Standalone agent (runs separately, connects to server)

## Deployment Patterns

### Development

```bash
docker compose --env-file .env.dev up -d     # or: make dev-up

# What .env.dev sets:
# - SERVER_IMAGE / AGENT_IMAGE and TAG=sha-<commit> — the published commit (make dev-deploy writes TAG)
# - DB_BIND=0.0.0.0 — PostgreSQL reachable on port 5432 for direct access
# - LOG__LEVEL=DEBUG
# - CORS and the TLS certificate directory for your dev domain
```

### Production (Your Deployment)

```bash
docker compose --env-file .env.prod up -d    # or: make prod-up

# Features:
# - CORS: https://dev.example.com:9000 (your public-facing URL)
# - INFO level logging
# - Uses registry images
# - Runs behind nginx for SSL termination
```

### Standalone Agent

```bash
# Start just the agent (connects to existing server)
docker compose -f compose.agent.yaml up -d

# Features:
# - Connects to https://dev.example.com:9000/api/v1/reports
# - Uses registration key for initial auth
# - Network mode: host (can ping local services)
# - Persistent data storage
```

## CORS Configuration

**CRITICAL:** CORS origins MUST match the exact URL users type in their browser.

All deployments use external domains (nginx SSL termination).

| Deployment Type      | CORS Setting              | Example                                               |
| -------------------- | ------------------------- | ----------------------------------------------------- |
| **With custom port** | Your public domain + port | `https://luxswirl.example.com:9000`                   |
| **Standard HTTPS**   | Domain on port 443        | `https://luxswirl.example.com`                        |
| **Multiple domains** | Array of domains          | `["https://luxswirl.com","https://api.luxswirl.com"]` |

### Where to Set CORS

**For This Deployment:**

- **Development** (`.env.dev`): `https://dev.example.com:9000`
- **Production** (`.env.prod`): `https://luxswirl.example.com:9000`

**For Other Deployments:**

Set `SERVER__CORS_ORIGINS` in the environment file for that deployment. A new environment is a new `.env.<name>`, never a new compose file:

```bash
cp .env.example .env.mycompany
# set ENV=mycompany, TAG=<version>, ALLOWED_HOSTS and SERVER__CORS_ORIGINS for your domain
docker compose --env-file .env.mycompany up -d
```

### Verifying CORS Configuration

Check the server logs on startup:

```bash
docker logs luxswirl_server | grep "CORS Origins"
# DEV should output: CORS Origins: ['https://dev.example.com:9000']
# PROD should output: CORS Origins: ['https://luxswirl.example.com:9000']
```

## Architecture

```
User Browser (https://your-domain.com:9000)
    ↓
Nginx (SSL termination, port 9000)
    ↓
luxswirl_server:9000 (internal Docker network)
    ↓
timescaledb:5432 (internal Docker network)
```

**Key Point:** Everything is accessed via external domain through nginx. CORS must match the public-facing URL.

## Cookie Configuration (Critical for Multi-App Setups)

### The Cookie Collision Problem

**Important:** If you run multiple applications on the same subdomain with different ports, they will share the same cookie namespace.

**Example Collision:**

```
dev.example.com:9000  (LuxSwirl)      → Uses "luxswirl_session" cookie
dev.example.com:8000  (Grafana)    → Uses "grafana_session" cookie
dev.example.com:3000  (Other app)  → Uses "app3_session" cookie
```

**Why:** Cookies are scoped by **hostname only**, not by port. All apps on `dev.example.com` (regardless of port) share cookies.

### Solution: Unique Cookie Names

Each application MUST use a unique session cookie name:

```bash
# .env.prod
SECURITY__SESSION_COOKIE_NAME=luxswirl_session  # Unique per app
```

**Default:** LuxSwirl uses `luxswirl_session` by default (safe for most deployments)

### When You DON'T Need to Change It

✅ Different subdomains: `dev.example.com` vs `dev2.example.com` (already isolated) ✅ Different domains: `luxswirl.example.com` vs `grafana.example.com` (already isolated)

### When You MUST Change It

❌ Same subdomain, different ports: `dev.example.com:9000` vs `dev.example.com:8000` ❌ Same subdomain, different paths: `dev.example.com/luxswirl` vs `dev.example.com/app2`

## Environment Variables

All settings can be overridden via environment variables using the nested delimiter `__`:

```bash
# Trusted hosts (required) and CORS
ALLOWED_HOSTS=your-domain.com,luxswirl_server
SERVER__CORS_ORIGINS=["https://your-domain.com"]

# Database
DATABASE__URL=postgresql+asyncpg://user:pass@host:5432/db
DATABASE__POOL_SIZE=20

# Security
SECURITY__SECRET_KEY=your-secret-key-min-32-chars
SECURITY__CSRF_ENABLED=true

# Logging
LOG__LEVEL=INFO
```

See `.env.example` for a complete list of configuration options.

## Nginx SSL Termination

Nginx handles SSL and forwards to the server:

```nginx
# User request: https://dev.example.com:9000/
# Nginx proxies to: http://luxswirl_server:9000/
```

See `nginx/conf.d/luxswirl.conf` for the full nginx configuration.

## Troubleshooting

### CORS Errors in Browser Console

**Symptom:** `Access-Control-Allow-Origin` errors when accessing web UI

**Cause:** CORS origins don't match the URL in the browser

**Fix:**

1. Check what URL you're accessing in browser (e.g., `https://dev.example.com:9000`)
1. Set CORS to EXACTLY that URL in your compose override file
1. Restart: `docker compose --env-file .env.prod restart luxswirl_server`
1. Verify: `docker logs luxswirl_server | grep "CORS Origins"`

### Web UI Not Loading

**Symptom:** Blank page or connection refused

**Check:**

1. Nginx is running and accessible
1. Server is healthy: `docker exec luxswirl_server curl -f http://localhost:9000/health`
1. Logs: `docker logs luxswirl_server`

### Agent Not Connecting

**Symptom:** Agent logs show connection errors

**Check:**

1. Agent has correct server URL: `https://dev.example.com:9000/api/v1/reports`
1. Registration key is valid
1. Agent can reach server: `docker exec luxswirl_agent curl -f https://dev.example.com:9000/health`

## Creating Your Own Deployment

For a new deployment (not dev.example.com):

1. **Copy the environment template:**

   ```bash
   cp .env.example .env.mycompany   # set ENV=mycompany and TAG=<version>
   ```

1. **Edit CORS to match your domain:**

   ```bash
   SERVER__CORS_ORIGINS=["https://luxswirl.mycompany.com"]
   ALLOWED_HOSTS=luxswirl.mycompany.com,luxswirl_server
   ```

1. **Update nginx config** (`nginx/conf.d/luxswirl.conf`):

   ```nginx
   server_name luxswirl.mycompany.com;
   ```

1. **Deploy:**

   ```bash
   docker compose --env-file .env.mycompany up -d
   ```

## See Also

- `ARCHITECTURE.md` - System architecture and design
- `BUILD.md` - Building Docker images
- `nginx-ssl.md` - Nginx SSL configuration
- `.env.example` - Complete configuration reference
