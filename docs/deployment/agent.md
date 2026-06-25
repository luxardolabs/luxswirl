# LuxSwirl Agent - Docker Deployment Guide

## Overview

The LuxSwirl agent is a lightweight monitoring agent that executes health checks and reports results to the LuxSwirl server. This guide covers deploying the agent using Docker and Docker Compose.

## Prerequisites

- Docker Engine 20.10+ or Docker Desktop
- Docker Compose 2.0+ (optional, but recommended)
- Network access to LuxSwirl server (HTTPS required for external servers)
- Registration key from server (`LUXSWIRL_AUTH_KEY`)

## Quick Start

### 1. Create compose.yaml

The published agent image is `ghcr.io/luxardolabs/luxswirl-agent` (tagged `:latest` / `:<version>`). To build it from source, run `make build` at the repo root. Compose v2 does not use a `version:` key.

```yaml
services:
  luxswirl_agent:
    image: ghcr.io/luxardolabs/luxswirl-agent:latest
    container_name: luxswirl_agent
    command: ["python", "-m", "app.agent_main"]
    network_mode: host  # Recommended for stability
    restart: unless-stopped

    environment:
      # Required: full reports endpoint, ending in /api/v1/reports
      # (HTTPS for external servers, HTTP allowed for internal)
      LUXSWIRL_SERVER_URL: "https://server.example.com:9000/api/v1/reports"

      # Required: registration key from the server
      # (Settings → Registration Keys). The container will not start without it.
      LUXSWIRL_AUTH_KEY: "your-registration-key-here"

      # Optional: agent id (defaults to <hostname>-agent)
      # LUXSWIRL_AGENT_ID: "prod-web-01"

      # Optional: logging level
      # LOG_LEVEL: "INFO"

    volumes:
      # Persistent credential storage
      - agent_data:/app/data

      # Optional: Docker socket for container checks
      # - /var/run/docker.sock:/var/run/docker.sock:ro

volumes:
  agent_data:
    driver: local
```

### 2. Get a Registration Key

The agent authenticates with a registration key. Bring up the **server** first, log in (complete the first-run `/setup` wizard), then go to **Settings → Registration Keys → Create Key** and copy the value into the agent's `LUXSWIRL_AUTH_KEY`. The agent will not start without it.

### 3. Start the Agent

```bash
docker compose up -d
```

### 4. Check Logs

```bash
docker compose logs -f luxswirl_agent
```

The agent connects to the server using `LUXSWIRL_AUTH_KEY`, then fetches its assigned checks from `GET /api/v1/checks?agent_id=<agent-id>`. Once it appears **Online** on the server's **Agents** page, it is connected.

### 5. Add Checks

Via server UI:
1. Navigate to agent detail page
2. Click **Add Check**
3. Configure check type (HTTP, TCP, Ping, etc.)
4. Save

Via API (optional):
```bash
curl -X POST https://server.example.com:9000/api/v1/agents/{agent_id}/checks \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "check_type": "http",
    "display_name": "Website Health",
    "target": "https://example.com",
    "interval_seconds": 60,
    "enabled": true
  }'
```

## Deployment Scenarios

### Scenario 1: Host Networking (Recommended)

**Best for:** Production, when agent needs to monitor local services

```yaml
services:
  luxswirl_agent:
    image: ghcr.io/luxardolabs/luxswirl-agent:latest
    container_name: luxswirl_agent
    network_mode: host
    restart: unless-stopped

    environment:
      LUXSWIRL_SERVER_URL: "https://server.example.com:9000/api/v1/reports"
      LUXSWIRL_AUTH_KEY: "your-registration-key"

    volumes:
      - agent_data:/app/data
```

**Advantages:**
- ✅ Credentials persist across container rebuilds
- ✅ Can monitor services on host network (`localhost:80`)
- ✅ No port mapping needed
- ✅ Best credential encryption stability

**Disadvantages:**
- ⚠️ Agent uses host's network namespace
- ⚠️ Potential port conflicts with host services

### Scenario 2: Bridge Networking

**Best for:** Development, isolated environments

```yaml
services:
  luxswirl_agent:
    image: ghcr.io/luxardolabs/luxswirl-agent:latest
    container_name: luxswirl_agent_prod  # IMPORTANT: Use fixed name
    restart: unless-stopped

    environment:
      LUXSWIRL_SERVER_URL: "https://server.example.com:9000/api/v1/reports"
      LUXSWIRL_AUTH_KEY: "your-registration-key"

    volumes:
      - agent_data:/app/data

    # No network_mode specified = bridge (default)
```

**Advantages:**
- ✅ Network isolation from host
- ✅ No conflicts with host services
- ✅ Can use custom Docker networks

**Disadvantages:**
- ⚠️ Cannot monitor `localhost` services on host
- ⚠️ Credentials may break if `container_name` not specified
- ⚠️ Must use `container_name` for stability

### Scenario 3: Monitoring Docker Containers

**Best for:** Monitoring other containers, Docker health checks

```yaml
services:
  luxswirl_agent:
    image: ghcr.io/luxardolabs/luxswirl-agent:latest
    container_name: luxswirl_agent
    network_mode: host
    restart: unless-stopped

    environment:
      LUXSWIRL_SERVER_URL: "https://server.example.com:9000/api/v1/reports"
      LUXSWIRL_AUTH_KEY: "your-registration-key"

    volumes:
      - agent_data:/app/data
      - /var/run/docker.sock:/var/run/docker.sock:ro  # Read-only Docker socket

    # Optional: Security
    user: "1000:999"  # Run as non-root with docker group
```

**Check Configuration (in server UI):**
- Check Type: `http`
- Target: `http://container_name:8080/health` (if on same Docker network)
- Or use service discovery for dynamic containers

### Scenario 4: Multi-Agent Deployment

**Best for:** Monitoring multiple hosts/environments from single Docker host

```yaml
services:
  luxswirl_agent_web:
    image: ghcr.io/luxardolabs/luxswirl-agent:latest
    container_name: luxswirl_agent_web
    network_mode: bridge
    restart: unless-stopped

    environment:
      LUXSWIRL_SERVER_URL: "https://server.example.com:9000/api/v1/reports"
      LUXSWIRL_AUTH_KEY: "registration-key-web"
      LUXSWIRL_AGENT_ID: "web-cluster-01"

    volumes:
      - agent_web_data:/app/data

  luxswirl_agent_db:
    image: ghcr.io/luxardolabs/luxswirl-agent:latest
    container_name: luxswirl_agent_db
    network_mode: bridge
    restart: unless-stopped

    environment:
      LUXSWIRL_SERVER_URL: "https://server.example.com:9000/api/v1/reports"
      LUXSWIRL_AUTH_KEY: "registration-key-db"
      LUXSWIRL_AGENT_ID: "db-cluster-01"

    volumes:
      - agent_db_data:/app/data

volumes:
  agent_web_data:
  agent_db_data:
```

## Configuration Reference

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LUXSWIRL_SERVER_URL` | **Yes** | `http://luxswirl_server:9000/api/v1/reports` | Full reports endpoint, ending in `/api/v1/reports` (HTTPS for external servers) |
| `LUXSWIRL_AUTH_KEY` | **Yes** | - | Registration key from server (agent will not start without it) |
| `LUXSWIRL_AGENT_ID` | No | `<hostname>-agent` | Agent id / display name |
| `LOG_LEVEL` | No | `DEBUG` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LUXSWIRL_DISABLE_CREDENTIAL_ENCRYPTION` | No | `false` | Disable credential encryption (testing only) |
| `LUXSWIRL_ALLOW_INSECURE_HTTP` | No | `false` | Allow HTTP for external servers (testing only) |

### Volume Mounts

| Path | Purpose | Required |
|------|---------|----------|
| `/app/data` | Persistent credential storage | **Yes** |
| `/var/run/docker.sock` | Docker container monitoring | Optional |
| Custom config files | Future use | Optional |

### Networking Requirements

**Outbound (Agent → Server):**
- Port: Server port (default 9000)
- Protocol: HTTPS (or HTTP for internal networks)
- Firewall: Allow outbound to server IP/hostname

**Inbound:**
- None required (agent initiates all connections)

## Health Checks

### Docker Health Check (Built-in)

```yaml
services:
  luxswirl_agent:
    image: ghcr.io/luxardolabs/luxswirl-agent:latest
    container_name: luxswirl_agent
    network_mode: host
    restart: unless-stopped

    healthcheck:
      test: ["CMD", "python", "-c", "import sys; sys.exit(0)"]  # Basic Python check
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

    environment:
      LUXSWIRL_SERVER_URL: "https://server.example.com:9000/api/v1/reports"
      LUXSWIRL_AUTH_KEY: "your-registration-key"

    volumes:
      - agent_data:/app/data
```

### Monitor Agent Health

```bash
# Check container status
docker ps -f name=luxswirl_agent

# Check health status
docker inspect luxswirl_agent --format='{{.State.Health.Status}}'

# View logs
docker logs luxswirl_agent --tail 100 -f

# Check if agent is reporting
# (View in server UI /agents page)
```

## Logging

### View Logs

```bash
# Follow logs
docker compose logs -f luxswirl_agent

# Last 100 lines
docker logs luxswirl_agent --tail 100

# Since timestamp
docker logs luxswirl_agent --since 2024-01-01T00:00:00

# Export logs
docker logs luxswirl_agent > agent.log
```

### Log Levels

```yaml
environment:
  LOG_LEVEL: "DEBUG"  # Verbose output
  # LOG_LEVEL: "INFO"   # Default
  # LOG_LEVEL: "WARNING"  # Warnings and errors only
  # LOG_LEVEL: "ERROR"    # Errors only
```

### Log Rotation (Docker)

```yaml
services:
  luxswirl_agent:
    image: ghcr.io/luxardolabs/luxswirl-agent:latest
    container_name: luxswirl_agent
    network_mode: host
    restart: unless-stopped

    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

    environment:
      LUXSWIRL_SERVER_URL: "https://server.example.com:9000/api/v1/reports"
      LUXSWIRL_AUTH_KEY: "your-registration-key"

    volumes:
      - agent_data:/app/data
```

## Updates and Maintenance

### Updating to New Version

```bash
# Pull latest image
docker compose pull luxswirl_agent

# Recreate container with new image
docker compose up -d luxswirl_agent

# Verify update
docker compose logs luxswirl_agent | head -20
```

**Note:** Credentials persist across updates (if using host networking or fixed `container_name`)

### Backup and Restore

**Backup credentials:**
```bash
# Not necessary - agent can re-register automatically
# But if desired:
docker run --rm -v luxswirl_agent_data:/data -v $(pwd):/backup \
  alpine tar czf /backup/agent-credentials-backup.tar.gz -C /data .
```

**Restore credentials:**
```bash
docker run --rm -v luxswirl_agent_data:/data -v $(pwd):/backup \
  alpine tar xzf /backup/agent-credentials-backup.tar.gz -C /data
```

**Note:** Restored credentials only work on same machine (see [Agent Credential Encryption](../architecture/security-credentials.md))

### Reset Agent (Force Re-registration)

```bash
# Stop agent
docker compose stop luxswirl_agent

# Remove credentials
docker run --rm -v luxswirl_agent_data:/data alpine rm -f /data/agent_credentials.json

# Start agent (will re-register)
docker compose up -d luxswirl_agent

# Approve in server UI
```

## Troubleshooting

### Agent Not Starting

**Check logs:**
```bash
docker compose logs luxswirl_agent
```

**Common issues:**
- ❌ Invalid `LUXSWIRL_SERVER_URL` (typo, wrong port)
- ❌ Invalid `LUXSWIRL_AUTH_KEY` (expired, typo)
- ❌ Network connectivity (firewall, DNS)
- ❌ Certificate errors (self-signed certs)

**Solutions:**
```bash
# Test connectivity
docker exec luxswirl_agent curl -v https://server.example.com:9000/health

# Check environment
docker exec luxswirl_agent env | grep SWIRL

# Restart with fresh credentials
docker compose down
docker volume rm luxswirl_agent_data
docker compose up -d
```

### Agent Not Reporting

**Symptoms:**
- Agent shows "Offline" in server UI
- No check results appearing

**Diagnosis:**
```bash
# Check agent logs for errors
docker logs luxswirl_agent --tail 50

# Check network from container
docker exec luxswirl_agent curl https://server.example.com:9000/health

# Verify agent is running
docker ps | grep luxswirl_agent
```

**Common causes:**
- Network issues (firewall, routing)
- Server down or unreachable
- API key revoked/invalid
- Agent crashed (check logs)

### Credentials Failed to Decrypt

**Error message:**
```
Failed to decrypt credentials - encryption key may have changed
```

**Cause:**
- Container hostname changed
- Host machine-id changed
- Moved container to different machine

**Solution:**
```bash
# Delete credentials and re-register
docker exec luxswirl_agent rm /app/data/agent_credentials.json
docker compose restart luxswirl_agent

# Approve new registration in server UI
```

See [Agent Credential Encryption](../architecture/security-credentials.md) for details.

### High CPU/Memory Usage

**Check resource usage:**
```bash
docker stats luxswirl_agent
```

**Common causes:**
- Too many checks configured (>100)
- High check frequency (interval <10s)
- Large response bodies (JSON checks)
- Memory leak (report bug)

**Solutions:**
- Reduce check count per agent
- Increase check intervals
- Deploy multiple agents
- Update to latest version

### Port Conflicts (Host Mode)

**Error:**
```
Error starting userland proxy: listen tcp 0.0.0.0:9000: bind: address already in use
```

**Solution:** Switch to bridge mode if host network causes conflicts:
```yaml
services:
  luxswirl_agent:
    # Remove network_mode: host
    container_name: luxswirl_agent  # Keep fixed name
```

## Production Best Practices

### 1. Use Host Networking

```yaml
network_mode: host
```
- Best credential stability
- Survives container rebuilds
- Can monitor localhost services

### 2. Set Restart Policy

```yaml
restart: unless-stopped
```
- Agent auto-restarts on failure
- Survives host reboots
- Manual stops respected

### 3. Configure Logging

```yaml
logging:
  driver: "json-file"
  options:
    max-size: "10m"
    max-file: "3"
```
- Prevents disk fill
- Keeps recent logs
- Integrates with log aggregation

### 4. Use Persistent Volumes

```yaml
volumes:
  - agent_data:/app/data
```
- Credentials survive container removal
- Named volumes easier to manage
- Backup-friendly

### 5. Monitor Agent Health

- Set up server alerts for agent offline
- Monitor agent resource usage
- Review agent logs periodically
- Keep agent updated

### 6. Secure Docker Socket (If Used)

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock:ro  # Read-only
```
- Always mount read-only
- Consider rootless Docker
- Audit container access

### 7. Keep the Registration Key out of the Image

Pass `LUXSWIRL_AUTH_KEY` via an environment file or your orchestrator's secret store rather than hard-coding it in the compose file:

```yaml
# Read LUXSWIRL_AUTH_KEY (and others) from a .env file kept out of git.
services:
  luxswirl_agent:
    env_file:
      - .env
```

### 8. Resource Limits

```yaml
services:
  luxswirl_agent:
    image: ghcr.io/luxardolabs/luxswirl-agent:latest
    container_name: luxswirl_agent

    deploy:
      resources:
        limits:
          cpus: '0.5'
          memory: 256M
        reservations:
          cpus: '0.1'
          memory: 64M
```

## Advanced Configuration

### Custom DNS Servers

```yaml
services:
  luxswirl_agent:
    image: ghcr.io/luxardolabs/luxswirl-agent:latest
    container_name: luxswirl_agent

    dns:
      - 8.8.8.8
      - 8.8.4.4

    environment:
      LUXSWIRL_SERVER_URL: "https://server.example.com:9000/api/v1/reports"
      LUXSWIRL_AUTH_KEY: "your-registration-key"
```

### Custom Network

```yaml
networks:
  monitoring:
    driver: bridge

services:
  luxswirl_agent:
    image: ghcr.io/luxardolabs/luxswirl-agent:latest
    container_name: luxswirl_agent

    networks:
      - monitoring

    environment:
      LUXSWIRL_SERVER_URL: "https://server.example.com:9000/api/v1/reports"
      LUXSWIRL_AUTH_KEY: "your-registration-key"
```

### Multiple Servers (Failover)

Not currently supported. Agents connect to single server URL.

**Workaround:** Use load balancer in front of servers:
```yaml
environment:
  LUXSWIRL_SERVER_URL: "https://server-lb.example.com:9000/api/v1/reports"
```

## Docker Swarm / Kubernetes

### Docker Swarm

```yaml
services:
  luxswirl_agent:
    image: ghcr.io/luxardolabs/luxswirl-agent:latest

    deploy:
      mode: global  # One agent per node
      restart_policy:
        condition: on-failure
        delay: 5s
        max_attempts: 3

    environment:
      LUXSWIRL_SERVER_URL: "https://server.example.com:9000/api/v1/reports"
      LUXSWIRL_AUTH_KEY: "your-registration-key"

    volumes:
      - agent_data:/app/data

volumes:
  agent_data:
```

### Kubernetes

See separate Kubernetes deployment guide (coming soon).

## Migration from Uptime Kuma

LuxSwirl is compatible with Uptime Kuma check configurations:

1. **Export checks** from Uptime Kuma
2. **Import** via LuxSwirl server API or UI
3. **Deploy agent** with Docker Compose
4. **Approve agent** in server
5. **Verify checks** running

## Related Documentation

- [Agent Credential Encryption](../architecture/security-credentials.md)
- [Check Types Reference](../user-guide/checks.md)
- [Troubleshooting Guide](../guides/troubleshooting.md)
- [Security Policy](../../SECURITY.md)
- API documentation is auto-generated and available at `/docs` on a running server (Swagger UI)

## Support

For deployment issues:

1. Check logs: `docker logs luxswirl_agent`
2. Review this guide and troubleshooting section
3. Check server connectivity: `curl https://server.example.com:9000/health`
4. Search existing issues: https://github.com/luxardolabs/luxswirl/issues
5. Open new issue with logs and your compose file (redact secrets)

---

**Maintainer:** Luxardo Labs
