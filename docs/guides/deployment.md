# LuxSwirl Deployment Guide

This document explains how to deploy LuxSwirl using Docker Compose with environment-specific overlays.

## Docker Compose Files

LuxSwirl uses a layered Docker Compose configuration:

- **`compose.yaml`** - Base configuration (shared across all environments)
- **`compose.dev.yaml`** - Development overlay (local testing with hot reload)
- **`compose.prod.yaml`** - Production overlay (your specific deployment)
- **`compose.agent.yaml`** - Standalone agent (runs separately, connects to server)

## Deployment Patterns

### Development

```bash
# Start with development overrides
docker compose -f compose.yaml -f compose.dev.yaml up

# Features:
# - Hot reload enabled (code changes apply immediately)
# - CORS: Set to your dev domain in compose.dev.yaml
# - PostgreSQL exposed on port 5432 for direct access
# - Debug logging enabled
# - Runs baked :dev images (built by `make dev-up`)
```

### Production (Your Deployment)

```bash
# Start with production overrides
docker compose -f compose.yaml -f compose.prod.yaml up -d

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

| Deployment Type | CORS Setting | Example |
|----------------|--------------|---------|
| **With custom port** | Your public domain + port | `https://luxswirl.example.com:9000` |
| **Standard HTTPS** | Domain on port 443 | `https://luxswirl.example.com` |
| **Multiple domains** | Array of domains | `["https://luxswirl.com","https://api.luxswirl.com"]` |

### Where to Set CORS

**For This Deployment:**
- **Development** (`compose.dev.yaml`): `https://dev.example.com:9000`
- **Production** (`compose.prod.yaml`): `https://luxswirl.example.com:9000`

**For Other Deployments:**

1. **Use existing overlays**: Edit the CORS URL in compose.dev.yaml or compose.prod.yaml

2. **Create custom overlay**: Create your own overlay file
   ```bash
   cp compose.prod.yaml compose.mycompany.yaml
   # Edit CORS setting to match your domain
   docker compose -f compose.yaml -f compose.mycompany.yaml up -d
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

```yaml
# compose.prod.yaml
environment:
  - SECURITY__SESSION_COOKIE_NAME=luxswirl_session  # Unique per app
```

**Default:** LuxSwirl uses `luxswirl_session` by default (safe for most deployments)

### When You DON'T Need to Change It

✅ Different subdomains: `dev.example.com` vs `dev2.example.com` (already isolated) ✅ Different domains: `luxswirl.example.com` vs `grafana.example.com` (already isolated)

### When You MUST Change It

❌ Same subdomain, different ports: `dev.example.com:9000` vs `dev.example.com:8000` ❌ Same subdomain, different paths: `dev.example.com/luxswirl` vs `dev.example.com/app2`

## Environment Variables

All settings can be overridden via environment variables using the nested delimiter `__`:

```bash
# CORS
SERVER__CORS_ORIGINS='["https://your-domain.com"]'

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
2. Set CORS to EXACTLY that URL in your compose override file
3. Restart: `docker compose -f compose.yaml -f compose.prod.yaml restart luxswirl_server`
4. Verify: `docker logs luxswirl_server | grep "CORS Origins"`

### Web UI Not Loading

**Symptom:** Blank page or connection refused

**Check:**
1. Nginx is running and accessible
2. Server is healthy: `docker exec luxswirl_server curl -f http://localhost:9000/health`
3. Logs: `docker logs luxswirl_server`

### Agent Not Connecting

**Symptom:** Agent logs show connection errors

**Check:**
1. Agent has correct server URL: `https://dev.example.com:9000/api/v1/reports`
2. Registration key is valid
3. Agent can reach server: `docker exec luxswirl_agent curl -f https://dev.example.com:9000/health`

## Creating Your Own Deployment

For a new deployment (not dev.example.com):

1. **Copy production overlay:**
   ```bash
   cp compose.prod.yaml compose.mycompany.yaml
   ```

2. **Edit CORS to match your domain:**
   ```yaml
   - SERVER__CORS_ORIGINS=["https://luxswirl.mycompany.com"]
   ```

3. **Update nginx config** (`nginx/conf.d/luxswirl.conf`):
   ```nginx
   server_name luxswirl.mycompany.com;
   ```

4. **Deploy:**
   ```bash
   docker compose -f compose.yaml -f compose.mycompany.yaml up -d
   ```

## See Also

- `ARCHITECTURE.md` - System architecture and design
- `BUILD.md` - Building Docker images
- `nginx-ssl.md` - Nginx SSL configuration
- `.env.example` - Complete configuration reference
