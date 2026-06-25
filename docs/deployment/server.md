# LuxSwirl Server - Docker Deployment Guide

## Overview

The LuxSwirl server is a FastAPI-based REST API server that receives check results from agents, stores them in TimescaleDB, and provides a web UI for monitoring and management. This guide covers deploying the server using Docker and Docker Compose.

## Architecture

```
┌─────────────┐
│   Agents    │───┐
│ (Multiple)  │   │
└─────────────┘   │
                  │ HTTPS (9000)
┌─────────────┐   │
│   Agents    │───┼──────► ┌──────────────┐      ┌──────────────┐
│ (Multiple)  │   │        │  Server   │◄────►│ TimescaleDB  │
└─────────────┘   │        │  (FastAPI)   │      │ (PostgreSQL) │
                  │        └──────────────┘      └──────────────┘
┌─────────────┐   │               │
│   Browser   │───┘               │
│   (Web UI)  │                   │
└─────────────┘                   ▼
                          ┌──────────────┐
                          │  Prometheus  │
                          │  (Scrape)    │
                          └──────────────┘
```

## Prerequisites

- Docker Engine 20.10+ or Docker Desktop
- Docker Compose 2.0+
- 2GB+ RAM (4GB recommended)
- 10GB+ disk space (for TimescaleDB)
- SSL certificate (for production)
- Ports 9000 and 5432 available

## Quick Start

### 1. Create compose.yaml

The published server image is `ghcr.io/luxardolabs/luxswirl-backend` (tagged `:latest` / `:<version>`). To build it from source instead, run `make build` at the repo root. Compose v2 does not use a `version:` key.

```yaml
services:
  timescaledb:
    image: timescale/timescaledb:latest-pg16
    container_name: luxswirl_timescaledb
    restart: unless-stopped

    # PostgreSQL uses /dev/shm for parallel query workers, sort buffers, and
    # hash joins. Docker's default is 64MB — too small for any non-trivial
    # query (you'll see `could not resize shared memory segment` in the server
    # logs when it happens). 1GB is a conservative floor.
    shm_size: '1gb'

    environment:
      POSTGRES_DB: luxswirl
      POSTGRES_USER: luxswirl
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}

    volumes:
      - timescale_data:/var/lib/postgresql/data

    expose:
      - "5432"   # Internal only

    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U luxswirl"]
      interval: 10s
      timeout: 5s
      retries: 5

  luxswirl_server:
    image: ghcr.io/luxardolabs/luxswirl-backend:latest
    container_name: luxswirl_server
    restart: unless-stopped

    # Apply DB migrations on boot, then start uvicorn.
    command: ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 9000"]

    depends_on:
      timescaledb:
        condition: service_healthy

    environment:
      # Database — a single connection URL (SQLAlchemy/asyncpg DSN). Keep the
      # password in sync with POSTGRES_PASSWORD above.
      DATABASE__URL: postgresql+asyncpg://luxswirl:${POSTGRES_PASSWORD}@timescaledb:5432/luxswirl

      # Server settings
      SERVER__ENVIRONMENT: production

      # CORS (REQUIRED in production) — the exact URL users type in the browser
      SERVER__CORS_ORIGINS: '["https://your-domain.com:9000"]'
      SERVER__CORS_CREDENTIALS: "true"

      # Security
      SECURITY__RATE_LIMIT_ENABLED: "true"

      # Logging
      LOG__LEVEL: INFO

    ports:
      - "9000:9000"

    volumes:
      - server_data:/app/data  # Persists auto-generated secret + encryption key

    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s

volumes:
  timescale_data:
    driver: local
  server_data:
    driver: local
```

> `POSTGRES_PASSWORD` is read from a `.env` file (or the shell). The server's `SECURITY__SECRET_KEY` (JWT) and `SECURITY__FIELD_ENCRYPTION_KEY` are auto-generated on first boot and persisted under `/app/data` — you do not need to set them unless injecting from a secrets manager.

### 2. Start Services

```bash
# Start in background
docker compose up -d

# View logs
docker compose logs -f
```

> In the upstream repo, the same stack is also driven by the Makefile: `make prod-up` runs `docker compose -f compose.yaml -f compose.prod.yaml up -d`.

### 3. Access Web UI

Navigate to: `http://localhost:9000`

**First login:**

There are **no default credentials**. On first launch the app redirects you to a setup page to create the admin account:

1. Open the URL above — you'll be redirected to **`/setup`**.
2. Choose an admin **username** and **password**, submit, then sign in. (Accounts are username + password only — no email.)

**Unattended / automation (optional):** set `SECURITY__INITIAL_ADMIN_PASSWORD` (and optionally `SECURITY__INITIAL_ADMIN_USERNAME`, default `admin`) in your `.env` before first boot. The admin is created automatically and you log in with it; a password change is forced on first login.

### 4. Create Registration Key

1. Login to web UI
2. Navigate to **Settings → Registration Keys**
3. Click **Create Key**
4. Copy key for agent deployment

### 5. Configure First Agent

Use the registration key from step 4 in agent's `LUXSWIRL_AUTH_KEY` environment variable.

See [Agent Docker Deployment](agent.md) for details.

## Environment Variables Reference

### Database Configuration

The database is configured with a **single** connection URL — there are no separate host/port/name/user/password variables. `POSTGRES_PASSWORD` is consumed by the TimescaleDB container and is typically interpolated into `DATABASE__URL`.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE__URL` | **Yes** | local default | SQLAlchemy/asyncpg DSN, e.g. `postgresql+asyncpg://luxswirl:PW@timescaledb:5432/luxswirl` |
| `DATABASE__ECHO` | No | `false` | Log SQL queries (debug) |
| `DATABASE__POOL_SIZE` | No | `20` | Connection pool size |
| `DATABASE__MAX_OVERFLOW` | No | `10` | Max overflow connections |
| `POSTGRES_PASSWORD` | **Yes** | - | Password for the TimescaleDB container (sync with `DATABASE__URL`) |

### Server Settings

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SERVER__HOST` | No | `0.0.0.0` | Listen address |
| `SERVER__PORT` | No | `9000` | Listen port |
| `SERVER__ENVIRONMENT` | No | `development` | Environment (`development`, `staging`, `production`) |

### CORS Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SERVER__CORS_ORIGINS` | **Yes*** | `[]` | Allowed origins JSON array |
| `SERVER__CORS_CREDENTIALS` | No | `true` | Allow credentials |

\* Required in production mode

**Example:**
```yaml
SERVER__CORS_ORIGINS: '["https://server.example.com:9000", "https://monitoring.example.com"]'
```

### Security Settings

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SECURITY__SECRET_KEY` | No | Auto-generated | JWT signing key. Auto-generated + persisted to `/app/data` on first boot; set only to inject from a secrets manager |
| `SECURITY__FIELD_ENCRYPTION_KEY` | No | Auto-generated | Fernet key for encrypting sensitive DB fields. Auto-generated + persisted on first boot |
| `SECURITY__RATE_LIMIT_ENABLED` | No | `true` | Enable rate limiting |
| `SECURITY__INITIAL_ADMIN_USERNAME` | No | `admin` | First-run admin username (unattended setup) |
| `SECURITY__INITIAL_ADMIN_PASSWORD` | No | (empty) | First-run admin password (unattended setup; empty → /setup wizard) |

### Logging Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LOG__LEVEL` | No | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |
| `LOG__MODULE_LEVELS` | No | (defaults) | Per-module log levels (JSON) |

**Example:**
```yaml
LOG__MODULE_LEVELS: '{"luxswirl.services.check": "WARNING", "sqlalchemy": "WARNING"}'
```

## Production Deployment

### Complete compose.yaml for Production

(Compose v2 — no `version:` key. The upstream repo ships this as `compose.yaml` + `compose.prod.yaml`; run it with `make prod-up`.)

```yaml
services:
  timescaledb:
    image: timescale/timescaledb:latest-pg16
    container_name: luxswirl_timescaledb
    restart: unless-stopped
    shm_size: '1gb'

    environment:
      POSTGRES_DB: luxswirl
      POSTGRES_USER: luxswirl
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}  # Use .env file

    volumes:
      - timescale_data:/var/lib/postgresql/data

    # Only expose to internal network
    networks:
      - luxswirl_internal

    # Remove port exposure for security
    # ports:
    #   - "5432:5432"

    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U luxswirl"]
      interval: 10s
      timeout: 5s
      retries: 5

    # Resource limits
    deploy:
      resources:
        limits:
          cpus: '2.0'
          memory: 2G
        reservations:
          cpus: '0.5'
          memory: 512M

  luxswirl_server:
    image: ghcr.io/luxardolabs/luxswirl-backend:latest
    container_name: luxswirl_server
    restart: unless-stopped

    # Apply DB migrations on boot, then start uvicorn.
    command: ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 9000"]

    depends_on:
      timescaledb:
        condition: service_healthy

    environment:
      # Database — single DSN (sync the password with POSTGRES_PASSWORD)
      DATABASE__URL: postgresql+asyncpg://luxswirl:${POSTGRES_PASSWORD}@timescaledb:5432/luxswirl

      # Server
      SERVER__ENVIRONMENT: production

      # CORS (YOUR DOMAIN!)
      SERVER__CORS_ORIGINS: '["https://monitoring.yourdomain.com:9000"]'
      SERVER__CORS_CREDENTIALS: "true"

      # Security. SECRET_KEY + FIELD_ENCRYPTION_KEY are auto-generated and
      # persisted to /app/data on first boot; override only from a secrets
      # manager. Pass them explicitly only if you manage them externally:
      #   SECURITY__SECRET_KEY: ${SECRET_KEY}
      #   SECURITY__FIELD_ENCRYPTION_KEY: ${FIELD_ENCRYPTION_KEY}
      SECURITY__RATE_LIMIT_ENABLED: "true"

      # Logging
      LOG__LEVEL: INFO

    ports:
      - "9000:9000"

    volumes:
      - server_data:/app/data  # Persists auto-generated secret + encryption key

    networks:
      - luxswirl_internal
      - luxswirl_external

    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s

    # Resource limits
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 1G
        reservations:
          cpus: '0.25'
          memory: 256M

    # Log rotation
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

  # Optional: Nginx reverse proxy for SSL termination
  nginx:
    image: nginx:alpine
    container_name: luxswirl_nginx
    restart: unless-stopped

    ports:
      - "443:443"
      - "80:80"

    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./ssl:/etc/nginx/ssl:ro

    networks:
      - luxswirl_external

    depends_on:
      - luxswirl_server

volumes:
  timescale_data:
    driver: local
  server_data:
    driver: local

networks:
  luxswirl_internal:
    driver: bridge
  luxswirl_external:
    driver: bridge
```

### .env File (Production Secrets)

```bash
# .env - DO NOT COMMIT TO GIT!

# Database password (used by the timescaledb container + DATABASE__URL)
POSTGRES_PASSWORD=your_secure_db_password_here

# Optional: only if you inject secrets yourself instead of letting the server
# auto-generate + persist them on first boot.
# SECURITY__SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(64))")
# SECURITY__FIELD_ENCRYPTION_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
```

### SSL/TLS Setup with Nginx

**nginx.conf:**
```nginx
events {
    worker_connections 1024;
}

http {
    upstream luxswirl_server {
        server luxswirl_server:9000;
    }

    # Redirect HTTP to HTTPS
    server {
        listen 80;
        server_name monitoring.yourdomain.com;
        return 301 https://$server_name$request_uri;
    }

    # HTTPS server
    server {
        listen 443 ssl http2;
        server_name monitoring.yourdomain.com;

        # SSL certificates
        ssl_certificate /etc/nginx/ssl/fullchain.pem;
        ssl_certificate_key /etc/nginx/ssl/privkey.pem;

        # SSL security settings
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_ciphers HIGH:!aNULL:!MD5;
        ssl_prefer_server_ciphers on;

        # Security headers
        add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
        add_header X-Frame-Options "SAMEORIGIN" always;
        add_header X-Content-Type-Options "nosniff" always;

        # Proxy settings
        location / {
            proxy_pass http://luxswirl_server;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;

            # WebSocket support (if needed)
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
        }

        # Increase timeouts for long-running requests
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
```

**Trusted proxy configuration (required for accurate rate limiting and audit logs):**

When the LuxSwirl server runs behind a reverse proxy, set `SECURITY__TRUSTED_PROXY_NETWORKS` to the CIDR of your proxy. Without it, every external client shares one rate-limit bucket (the proxy's IP), and audit logs record the proxy's address for every event.

The default covers Docker bridge networks and RFC 1918 ranges, which works for most compose-based deployments without changes:
```yaml
# compose.yaml — luxswirl_server service environment:
environment:
  # Defaults shown; override only if your proxy is on a non-standard network.
  SECURITY__TRUSTED_PROXY_NETWORKS: '["127.0.0.0/8","::1/128","10.0.0.0/8","172.16.0.0/12","192.168.0.0/16"]'
```

For a fixed-IP proxy (e.g., AWS ALB on a known subnet), tighten to just that range. If LuxSwirl is exposed directly with no proxy in front, set to `[]` — X-Forwarded-For will be ignored entirely. See `docs/reference/settings-reference.md` for full details.

**Directory structure:**
```
.
├── compose.yaml
├── .env
├── nginx.conf
└── ssl/
    ├── fullchain.pem
    └── privkey.pem
```

## Database Management

### Initial Setup

On first boot the server applies Alembic migrations (`alembic upgrade head`) and sets up TimescaleDB automatically — hypertables, continuous aggregates, compression, and retention. There's no manual database setup.

The admin account is created through the `/setup` wizard on first launch (or automatically when `SECURITY__INITIAL_ADMIN_PASSWORD` is set, for unattended installs) — there is **no** default admin user.

### Database Backup

**Automated backup script:**
```bash
#!/bin/bash
# backup-server-db.sh

BACKUP_DIR="/backups/luxswirl"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/luxswirl_backup_$TIMESTAMP.sql"

mkdir -p $BACKUP_DIR

# Backup database
docker exec luxswirl_timescaledb pg_dump -U luxswirl luxswirl > $BACKUP_FILE

# Compress
gzip $BACKUP_FILE

# Keep last 7 days
find $BACKUP_DIR -name "luxswirl_backup_*.sql.gz" -mtime +7 -delete

echo "Backup completed: ${BACKUP_FILE}.gz"
```

**Schedule with cron:**
```bash
# Run daily at 2 AM
0 2 * * * /path/to/backup-server-db.sh
```

### Database Restore

```bash
# Stop server
docker compose stop luxswirl_server

# Restore database
gunzip -c luxswirl_backup_20250111_020000.sql.gz | \
  docker exec -i luxswirl_timescaledb psql -U luxswirl luxswirl

# Start server
docker compose start luxswirl_server
```

### Database Maintenance

**Check database size:**
```bash
docker exec luxswirl_timescaledb psql -U luxswirl luxswirl -c "
  SELECT pg_size_pretty(pg_database_size('luxswirl')) AS size;
"
```

**Check TimescaleDB hypertables:**
```bash
docker exec luxswirl_timescaledb psql -U luxswirl luxswirl -c "
  SELECT * FROM timescaledb_information.hypertables;
"
```

**Check retention policies:**
```bash
docker exec luxswirl_timescaledb psql -U luxswirl luxswirl -c "
  SELECT * FROM timescaledb_information.jobs WHERE proc_name = 'policy_retention';
"
```

**Manually compress chunks (optional):**
```bash
docker exec luxswirl_timescaledb psql -U luxswirl luxswirl -c "
  SELECT compress_chunk(c) FROM show_chunks('check_results') c;
"
```

## User Management

### Change Your Password

**Via Web UI:**
1. Log in, then open **Profile** (top right)
2. Click **Change Password**
3. Enter your current password and a new one (min 8 characters: at least one uppercase, one lowercase, and one digit)
4. Save

**Via API:**
```bash
curl -X POST https://server.example.com:9000/api/v1/auth/change-password \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "current_password": "<your-current-password>",
    "new_password": "NewSecurePassword123"
  }'
```

### Create Additional Users

**Via Web UI:**
1. Login as admin
2. Navigate to **Settings → Users**
3. Click **Create User**
4. Fill in details (username, role)
5. Set a temporary password
6. Send credentials to user (they must change on first login)

**Roles:**
- `admin` - Full system access
- `editor` - Manage checks and agents
- `viewer` - Read-only access

### Reset Forgotten Password

```bash
# Access database
docker exec -it luxswirl_timescaledb psql -U luxswirl luxswirl

# Update password (hashed automatically by application)
# For now, reset admin password to 'admin':
UPDATE users SET password_hash = '$2b$12$...' WHERE username = 'admin';

# Or use Python to generate bcrypt hash
docker exec -it luxswirl_server python -c "
from passlib.context import CryptContext
pwd_context = CryptContext(schemes=['bcrypt'], deprecated='auto')
print(pwd_context.hash('new_password_here'))
"
```

## Monitoring and Health Checks

### Health Endpoints

**Server health:**
```bash
curl http://localhost:9000/health
# Response: {"status":"healthy","version":"1.0.0","environment":"production"}
```

**Database connectivity:**
```bash
# Via web UI: Settings → Database Health
# Shows connection status, table counts, disk usage
```

### Prometheus Metrics

**Metrics endpoint:**
```
http://localhost:9000/metrics
```

**Available metrics:**
- `luxswirl_check_success` - Check success/failure (0/1)
- `luxswirl_check_up` - Check still reporting (0/1)
- `luxswirl_check_latency_seconds` - Check latency
- `luxswirl_check_last_execution_time` - Last execution timestamp
- `luxswirl_agent_up` - Agent online status (0/1)

**Prometheus configuration:**
```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'luxswirl_server'
    static_configs:
      - targets: ['server.example.com:9000']
    metrics_path: '/metrics'
    scheme: https
```

### Grafana Dashboard

Import dashboard JSON (coming soon) or create custom:

**Example queries:**
```promql
# Check success ratio (luxswirl_check_success is a 1/0 gauge per check)
avg(luxswirl_check_success)

# Agent count
count(luxswirl_agent_up == 1)

# Check latency (95th percentile)
histogram_quantile(0.95, rate(luxswirl_check_latency_seconds_bucket[5m]))
```

## Logging

### View Logs

```bash
# Server logs
docker compose logs -f luxswirl_server

# Database logs
docker compose logs -f timescaledb

# Last 100 lines
docker compose logs --tail 100 server

# Since timestamp
docker compose logs --since 2024-01-01T00:00:00 server
```

### Log Levels

**Change log level (restart required):**
```yaml
environment:
  LOG__LEVEL: DEBUG
```

**Per-module logging:**
```yaml
environment:
  LOG__MODULE_LEVELS: '{
    "luxswirl.server": "DEBUG",
    "luxswirl.services": "INFO",
    "sqlalchemy": "WARNING",
    "uvicorn": "INFO"
  }'
```

### External Log Aggregation

**Syslog driver:**
```yaml
services:
  luxswirl_server:
    logging:
      driver: syslog
      options:
        syslog-address: "tcp://syslog.example.com:514"
        tag: "luxswirl_server"
```

**Fluentd/Loki:**
```yaml
services:
  luxswirl_server:
    logging:
      driver: fluentd
      options:
        fluentd-address: "localhost:24224"
        tag: "luxswirl.server"
```

## Updates and Maintenance

### Update Server

```bash
# Pull latest images
docker compose pull

# Recreate containers
docker compose up -d

# Verify update
docker compose logs luxswirl_server | head -20

# Check version
curl http://localhost:9000/health | jq '.version'
```

### Database Migration

**Automatic migrations:**
- Server runs Alembic migrations on startup
- No manual intervention required
- Check logs for migration status

**Manual migration (if needed):**
```bash
docker exec luxswirl_server alembic upgrade head
```

### Rollback

**Server rollback:**
```bash
# Pin a specific version tag in your compose file, then:
docker compose down
docker pull ghcr.io/luxardolabs/luxswirl-backend:1.0.5
docker compose up -d
```

**Database rollback:**
```bash
# Restore from backup
docker compose stop luxswirl_server
docker exec -i luxswirl_timescaledb psql -U luxswirl luxswirl < backup.sql
docker compose start luxswirl_server
```

## Troubleshooting

### Server Won't Start

**Check logs:**
```bash
docker compose logs luxswirl_server
```

**Common issues:**

1. **Database connection failed:**
   ```
   ERROR: could not connect to server
   ```
   - Verify `DATABASE__URL` (host, port, credentials, db name)
   - Check database is running: `docker compose ps timescaledb`
   - Check database health: `docker compose exec timescaledb pg_isready`

2. **CORS configuration error:**
   ```
   ValueError: CORS origins not configured for production
   ```
   - Set `SERVER__CORS_ORIGINS` in production mode
   - Use JSON array format: `'["https://domain.com"]'`

3. **Port already in use:**
   ```
   Error starting userland proxy: listen tcp 0.0.0.0:9000: bind: address already in use
   ```
   - Check what's using port: `lsof -i :9000`
   - Change port: `SERVER__PORT: 9001`

### Agents Not Connecting

**Symptoms:**
- Agents show "Registration failed"
- 401/403 errors in agent logs

**Diagnosis:**
```bash
# Test from agent host
curl -v https://server.example.com:9000/health

# Check server logs for incoming requests
docker compose logs luxswirl_server | grep "POST /api/v1/reports"

# Verify registration key
# Settings → Registration Keys in web UI
```

**Solutions:**
- Check firewall rules (allow inbound 9000)
- Verify SSL certificate (self-signed certs need agent config)
- Check CORS settings
- Verify registration key is active

### High CPU/Memory Usage

**Check resource usage:**
```bash
docker stats luxswirl_server luxswirl_timescaledb
```

**Common causes:**
- Too many agents (>100)
- High check frequency (thousands of checks/min)
- Large database (>100GB)
- Inefficient queries (check slow query log)

**Solutions:**
- Enable compression: See database maintenance
- Adjust retention: Default 90 days
- Add more CPU/RAM: Update your compose file
- Tune PostgreSQL: See database tuning guide

### Database Performance Issues

**Check database size:**
```bash
docker exec luxswirl_timescaledb psql -U luxswirl luxswirl -c "
  SELECT pg_size_pretty(pg_database_size('luxswirl'));
"
```

**Check slow queries:**
```bash
docker exec luxswirl_timescaledb psql -U luxswirl luxswirl -c "
  SELECT pid, query, query_start, state
  FROM pg_stat_activity
  WHERE state != 'idle'
  ORDER BY query_start;
"
```

**Enable compression (recommended):**
```bash
docker exec luxswirl_timescaledb psql -U luxswirl luxswirl -c "
  ALTER TABLE check_results SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'check_id',
    timescaledb.compress_orderby = 'timestamp DESC'
  );

  SELECT add_compression_policy('check_results', INTERVAL '7 days');
"
```

### Web UI Not Loading

**Symptoms:**
- Blank page
- CSS/JS not loading
- 404 errors

**Check:**
```bash
# Verify static files mounted correctly
docker exec luxswirl_server ls /app/web/static/

# Check nginx configuration (if using)
docker exec luxswirl_nginx nginx -t

# Browser developer console for errors
# F12 → Console tab
```

**Solutions:**
- Clear browser cache (Ctrl+Shift+R)
- Check CORS settings
- Verify reverse proxy configuration
- Check server logs for errors

## Production Best Practices

### 1. Use SSL/TLS

- ✅ Use Nginx/Caddy for SSL termination
- ✅ Use Let's Encrypt for free certificates
- ✅ Enforce HTTPS redirect
- ✅ Enable HSTS headers

### 2. Secure Database

- ✅ Do NOT expose port 5432 externally
- ✅ Use strong passwords (16+ chars)
- ✅ Use internal Docker network
- ✅ Enable connection encryption (SSL)

### 3. Configure CORS Properly

```yaml
# Specific domains only (NOT '*')
SERVER__CORS_ORIGINS: '["https://monitoring.yourdomain.com:9000"]'
```

### 4. Enable Security Features

```yaml
SECURITY__RATE_LIMIT_ENABLED: "true"
# SECRET_KEY (JWT) and FIELD_ENCRYPTION_KEY are auto-generated + persisted to
# /app/data on first boot. Override only to inject from a secrets manager:
# SECURITY__SECRET_KEY: "<token-urlsafe-64>"
# SECURITY__FIELD_ENCRYPTION_KEY: "<fernet-key>"
```

### 5. Set Resource Limits

```yaml
deploy:
  resources:
    limits:
      cpus: '2.0'
      memory: 2G
```

### 6. Configure Log Rotation

```yaml
logging:
  driver: "json-file"
  options:
    max-size: "10m"
    max-file: "3"
```

### 7. Automated Backups

- Daily database backups
- Retain 7-30 days
- Test restore procedure
- Store off-site

### 8. Monitoring

- Monitor server health endpoint
- Alert on database issues
- Track resource usage
- Monitor agent connectivity

### 9. Update Strategy

- Test updates in staging
- Backup before updating
- Schedule maintenance windows
- Have rollback plan

### 10. Documentation

- Document your deployment
- Keep secrets in .env (not git)
- Document custom configurations
- Maintain runbooks

## Scaling Considerations

### Vertical Scaling (Single Instance)

**Server:**
- 2 CPU cores → ~50 agents, ~500 checks
- 4 CPU cores → ~150 agents, ~1500 checks
- 8 CPU cores → ~300 agents, ~3000 checks

**Database:**
- 2GB RAM → ~100K results/day
- 4GB RAM → ~500K results/day
- 8GB RAM → ~1M+ results/day

### Horizontal Scaling (Future)

Not currently supported:
- Multiple server instances
- Load balancer
- Database read replicas
- Redis for session storage

## Related Documentation

- [Agent Docker Deployment](agent.md)
- [Agent Credential Encryption](../architecture/security-credentials.md)
- [Check Types Reference](../user-guide/checks.md)
- [Security Policy](../../SECURITY.md)
- API documentation is auto-generated and available at `/docs` on a running server (Swagger UI)

## Support

For server deployment issues:

1. Check logs: `docker compose logs luxswirl_server`
2. Review troubleshooting section
3. Check database connectivity and health
4. Search existing issues: https://github.com/luxardolabs/luxswirl/issues
5. Open new issue with logs and configuration (redact secrets)

---

**Maintainer:** Luxardo Labs
