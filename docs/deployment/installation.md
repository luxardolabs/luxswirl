# LuxSwirl Installation Guide

**Production-ready deployment guide for self-hosted environments.**

This guide covers installing LuxSwirl on Ubuntu/Debian servers, configuring HTTPS with reverse proxy, database setup, and security hardening.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Architecture Overview](#architecture-overview)
3. [Option 1: Docker Compose (Recommended)](#option-1-docker-compose-recommended)
4. [Option 2: Manual Installation](#option-2-manual-installation)
5. [Reverse Proxy Setup (HTTPS)](#reverse-proxy-setup-https)
6. [Database Configuration](#database-configuration)
7. [Security Hardening](#security-hardening)
8. [Monitoring LuxSwirl Itself](#monitoring-luxswirl-itself)
9. [Backup and Disaster Recovery](#backup-and-disaster-recovery)
10. [Upgrading](#upgrading)
11. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### Hardware Requirements

**Minimum** (small deployment, <500 checks):
- **CPU**: 2 cores
- **RAM**: 4 GB
- **Disk**: 50 GB SSD
- **Network**: 100 Mbps

**Recommended** (medium deployment, 500-2,000 checks):
- **CPU**: 4 cores
- **RAM**: 8 GB
- **Disk**: 200 GB SSD (NVMe preferred)
- **Network**: 1 Gbps

**Large scale** (2,000+ checks):
- **CPU**: 8+ cores
- **RAM**: 16+ GB
- **Disk**: 500 GB NVMe SSD
- **Network**: 1 Gbps
- **Database**: Separate server (dedicated TimescaleDB instance)

### Software Requirements

**Operating System** (tested):
- Ubuntu 22.04 LTS or 24.04 LTS (recommended)
- Debian 12 (Bookworm)
- RHEL 9 / Rocky Linux 9 / AlmaLinux 9
- Docker-compatible Linux kernel 3.10+

**Dependencies**:
- Docker 20.10+ and Docker Compose 2.0+
- OR Python 3.14+ and PostgreSQL 14+ with TimescaleDB 2.11+

**Network**:
- Inbound: HTTPS (443), HTTP (80 for redirect)
- Outbound: HTTPS (443) for agent check targets
- Internal: PostgreSQL (5432) if using separate database server

### Domain and SSL

**Production requirements**:
- Domain name (e.g., `luxswirl.example.com`)
- DNS A record pointing to server IP
- SSL certificate (Let's Encrypt recommended, free)

**Optional**:
- Separate domain for status pages (e.g., `status.example.com`)
- Wildcard certificate (e.g., `*.example.com`)

---

## Architecture Overview

**Three-component architecture**:

```
┌─────────────────────────────────────────────────────────────┐
│                         Internet                            │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTPS (443)
                            ▼
                   ┌────────────────┐
                   │  Reverse Proxy │  (nginx, Traefik, Cloudflare)
                   │  SSL Termination│
                   └────────┬───────┘
                            │ HTTP (9000)
                            ▼
                   ┌────────────────┐
                   │   Server    │  FastAPI + Web UI
                   │  (Port 9000)   │  Receives agent reports
                   └────────┬───────┘
                            │
                ┌───────────┴───────────┐
                │                       │
                ▼                       ▼
       ┌────────────────┐      ┌───────────────┐
       │  TimescaleDB   │      │   Agent(s)    │
       │  (Port 5432)   │      │  Check        │
       │  Time-series   │      │  Executors    │
       │  Storage       │      └───────────────┘
       └────────────────┘
```

**Deployment models**:

**Single server** (small scale):
- All components on one machine
- Docker Compose for orchestration
- 50-500 checks, 1-5 agents

**Multi-server** (medium scale):
- Server + database on separate servers
- Agents distributed globally
- 500-5,000 checks, 5-50 agents

**High availability** (large scale):
- Load balancer + multiple servers
- PostgreSQL replication (primary + read replicas)
- Agents distributed across regions
- 5,000+ checks, 50+ agents

---

## Option 1: Docker Compose (Recommended)

**Advantages**:
- ✅ Fastest deployment (15-30 minutes)
- ✅ Isolated containers (no dependency conflicts)
- ✅ Easy upgrades (`docker compose pull && docker compose up -d`)
- ✅ Portable across servers

**Disadvantages**:
- ❌ Requires Docker knowledge
- ❌ Less control over system-level tuning

### Step 1: Install Docker

**Ubuntu/Debian**:
```bash
# Update package index
sudo apt update

# Install dependencies
sudo apt install -y apt-transport-https ca-certificates curl software-properties-common

# Add Docker GPG key
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg

# Add Docker repository
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Verify installation
docker --version
docker compose version
```

**RHEL/Rocky/AlmaLinux**:
```bash
# Install Docker
sudo yum install -y yum-utils
sudo yum-config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Start Docker
sudo systemctl start docker
sudo systemctl enable docker

# Verify
docker --version
```

**Add user to docker group** (avoid sudo):
```bash
sudo usermod -aG docker $USER
# Log out and back in for group change to take effect
```

### Step 2: Clone Repository

```bash
cd /opt
sudo git clone https://github.com/luxardolabs/luxswirl.git
cd luxswirl
```

### Step 3: Configure Environment Variables

Create `.env` file:
```bash
sudo nano .env
```

**Production `.env` template**:
```env
# Database. The app uses a single DATABASE__URL (SQLAlchemy/asyncpg DSN).
# POSTGRES_PASSWORD is consumed by the timescaledb container AND interpolated
# into DATABASE__URL — keep the two in sync.
POSTGRES_PASSWORD=CHANGE_THIS_SECURE_PASSWORD_123
DATABASE__URL=postgresql+asyncpg://luxswirl:CHANGE_THIS_SECURE_PASSWORD_123@timescaledb:5432/luxswirl

# CORS — MUST be the exact URL users type in the browser (JSON array).
SERVER__CORS_ORIGINS='["https://luxswirl.example.com:9000"]'

# Environment & logging
SERVER__ENVIRONMENT=production
LOG__LEVEL=INFO

# Security. SECURITY__SECRET_KEY (JWT) and SECURITY__FIELD_ENCRYPTION_KEY are
# auto-generated on first boot and persisted under /app/data on the server
# volume — operators do not need to set them. Override only to inject from a
# secrets manager:
#   SECURITY__SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(64))")
#   SECURITY__FIELD_ENCRYPTION_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')

# Agent registration key — generate in the UI (Settings → Registration Keys)
# after first login, then set this before starting the agent service.
LUXSWIRL_AUTH_KEY=

# Optional: SMTP for email notifications
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=alerts@example.com
SMTP_PASSWORD=smtp_password_here
SMTP_FROM=noreply@example.com
```

**Generate secure secrets**:
```bash
# POSTGRES_PASSWORD: required, you choose the value
openssl rand -base64 32

# SECURITY__SECRET_KEY: optional. The server auto-generates one on first boot
# and persists it to ./data/server. Generate manually only if injecting from a
# secrets manager:
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```

### Step 4: Review the Compose Files (Optional)

The repo ships a base `compose.yaml` plus environment overlays (`compose.prod.yaml`, `compose.dev.yaml`). The base file already defines `timescaledb`, `luxswirl_server`, `luxswirl_agent`, and an `nginx` reverse proxy. The published images are:

- `ghcr.io/luxardolabs/luxswirl-backend` — server (`uvicorn app.main:app`)
- `ghcr.io/luxardolabs/luxswirl-agent` — agent (`python -m app.agent_main`)

Both are tagged `:latest` and `:<version>`. If you prefer not to pull, build both locally with the Makefile: `make build`.

A trimmed view of the base `compose.yaml` (no obsolete `version:` key — Compose v2 doesn't use one):

```yaml
services:
  timescaledb:
    image: timescale/timescaledb:latest-pg16
    container_name: luxswirl_timescaledb
    restart: unless-stopped
    # PostgreSQL needs more than Docker's default 64MB /dev/shm for sort
    # buffers, hash joins, and parallel query workers. Without this, larger
    # queries throw `could not resize shared memory segment`.
    shm_size: '1gb'
    environment:
      - POSTGRES_DB=luxswirl
      - POSTGRES_USER=luxswirl
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
    volumes:
      - luxswirl_db_data:/var/lib/postgresql/data
    expose:
      - "5432"   # Internal only; exposed to host in compose.dev.yaml
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U luxswirl"]
      interval: 10s
      timeout: 5s
      retries: 5

  luxswirl_server:
    image: ghcr.io/luxardolabs/luxswirl-backend:latest
    container_name: luxswirl_server
    restart: unless-stopped
    # Runs DB migrations on boot, then starts uvicorn.
    command: ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 9000"]
    depends_on:
      timescaledb:
        condition: service_healthy
    environment:
      - DATABASE__URL=postgresql+asyncpg://luxswirl:${POSTGRES_PASSWORD}@timescaledb:5432/luxswirl
      - SERVER__ENVIRONMENT=production
      - LOG__LEVEL=INFO
      # SERVER__CORS_ORIGINS / SECURITY__* come from your .env file.
    expose:
      - "9000"   # Internal only; reach it via nginx
    volumes:
      - ./data/server:/app/data  # Persists auto-generated secret + encryption key

  luxswirl_agent:
    image: ghcr.io/luxardolabs/luxswirl-agent:latest
    container_name: luxswirl_agent
    hostname: luxswirl_agent  # Keep stable: used in credential encryption
    command: ["python", "-m", "app.agent_main"]
    restart: unless-stopped
    depends_on:
      luxswirl_server:
        condition: service_healthy
    environment:
      - LUXSWIRL_AGENT_ID=docker-agent
      # Required — generated via Settings → Registration Keys after first login.
      # Compose hard-fails this service if unset.
      - LUXSWIRL_AUTH_KEY=${LUXSWIRL_AUTH_KEY:?must be set — generate via the server UI}
      # Full reports endpoint, ending in /api/v1/reports.
      - LUXSWIRL_SERVER_URL=http://luxswirl_server:9000/api/v1/reports
    volumes:
      - ./data/agent:/app/data

  nginx:
    image: nginx:alpine
    container_name: luxswirl_nginx
    restart: unless-stopped
    depends_on:
      luxswirl_server:
        condition: service_healthy
    ports:
      - "9000:9000"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./nginx/conf.d:/etc/nginx/conf.d:ro
      - /etc/ssl/certs/your-domain:/etc/nginx/ssl:ro

volumes:
  luxswirl_db_data:
    name: luxswirl_db_data

networks:
  luxswirl-network:
    driver: bridge
```

**Security notes**:
- TimescaleDB and the server use `expose:` (internal-only); only nginx publishes a host port. The agent talks to the server over the internal Docker network.
- Reverse proxy (next step) handles public HTTPS.

### Step 5: Start Services

Bring up the server first, then add the agent once you have a registration key (the agent service refuses to start without `LUXSWIRL_AUTH_KEY`):

```bash
# Start DB + server (+ nginx). Use the prod overlay for production CORS.
sudo docker compose -f compose.yaml -f compose.prod.yaml up -d timescaledb luxswirl_server nginx
# Or via the Makefile (runs the same overlay): sudo make prod-up

# Verify services are running
sudo docker compose -f compose.yaml -f compose.prod.yaml ps

# Check logs
sudo docker compose -f compose.yaml -f compose.prod.yaml logs -f luxswirl_server
```

**Expected output**:
```
[+] Running 3/3
 ✔ Container luxswirl_timescaledb    Healthy
 ✔ Container luxswirl_server         Healthy
 ✔ Container luxswirl_nginx          Running
```

Then log in (Step below / setup wizard), create a registration key in **Settings → Registration Keys**, set `LUXSWIRL_AUTH_KEY` in `.env`, and start the agent:
```bash
sudo docker compose -f compose.yaml -f compose.prod.yaml up -d luxswirl_agent
```

### Step 6: Verify Installation

```bash
# Check server health
curl http://localhost:9000/health
# Expected: {"status":"healthy"}

# Check agent connection (view logs)
sudo docker compose -f compose.yaml -f compose.prod.yaml logs luxswirl_agent
# The agent fetches its checks from GET /api/v1/checks?agent_id=<id>
```

**Next step**: [Configure reverse proxy](#reverse-proxy-setup-https) for HTTPS access.

---

## Option 2: Manual Installation

**Advantages**:
- ✅ Full control over system configuration
- ✅ Better integration with system services (systemd)
- ✅ Easier performance tuning

**Disadvantages**:
- ❌ Complex setup (1-2 hours)
- ❌ Manual dependency management
- ❌ More difficult upgrades

### Step 1: Install System Dependencies

**Ubuntu/Debian**:
```bash
# Update package index
sudo apt update && sudo apt upgrade -y

# Install Python 3.14+
sudo apt install -y python3.14 python3.14-venv python3-pip

# Install PostgreSQL 14
sudo apt install -y postgresql-14 postgresql-client-14

# Install TimescaleDB
sudo sh -c "echo 'deb [signed-by=/usr/share/keyrings/timescale.keyring] https://packagecloud.io/timescale/timescaledb/ubuntu/ $(lsb_release -c -s) main' > /etc/apt/sources.list.d/timescaledb.list"
wget --quiet -O - https://packagecloud.io/timescale/timescaledb/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/timescale.keyring
sudo apt update
sudo apt install -y timescaledb-2-postgresql-14

# Tune TimescaleDB (recommended)
sudo timescaledb-tune --quiet --yes

# Restart PostgreSQL
sudo systemctl restart postgresql
```

### Step 2: Create Database and User

```bash
# Switch to postgres user
sudo -u postgres psql

-- Create database and user
CREATE DATABASE luxswirl;
CREATE USER luxswirl WITH ENCRYPTED PASSWORD 'CHANGE_THIS_SECURE_PASSWORD';
GRANT ALL PRIVILEGES ON DATABASE luxswirl TO luxswirl;

-- Connect to luxswirl database
\c luxswirl

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Exit psql
\q
```

### Step 3: Create LuxSwirl User

```bash
# Create system user for LuxSwirl
sudo useradd -r -s /bin/bash -d /opt/luxswirl -m luxswirl

# Switch to luxswirl user
sudo su - luxswirl
```

### Step 4: Clone Repository and Install Dependencies

```bash
# Clone repository
cd /opt/luxswirl
git clone https://github.com/luxardolabs/luxswirl.git .

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies (Poetry, per component)
pip install --upgrade pip poetry
cd apps/backend && poetry install --only main && cd ../..
cd apps/agent && poetry install --only main && cd ../..
```

### Step 5: Configure Environment

Create `.env` file:
```bash
nano /opt/luxswirl/.env
```

**Production `.env`**:
```env
# Database — single connection URL (SQLAlchemy/asyncpg DSN)
DATABASE__URL=postgresql+asyncpg://luxswirl:CHANGE_THIS_SECURE_PASSWORD@localhost:5432/luxswirl

# Security
# SECURITY__SECRET_KEY: optional. If unset, the server auto-generates one on
# first boot and persists to ./data/server. Set only if injecting from a
# secrets manager. SECURITY__FIELD_ENCRYPTION_KEY is handled the same way.
# SECURITY__SECRET_KEY=
SERVER__CORS_ORIGINS='["https://luxswirl.example.com"]'

# Server
SERVER__PORT=9000
SERVER__HOST=127.0.0.1  # Bind to localhost only (reverse proxy handles public)

# Logging
LOG__LEVEL=INFO
```

### Step 6: Initialize Database Schema

```bash
# Apply database migrations. Note: when run via Docker the server applies these
# automatically on boot (alembic upgrade head); for manual installs run it once
# yourself.
cd /opt/luxswirl/apps/backend
PYTHONPATH=.:.. python -m alembic upgrade head
```

### Step 7: Create systemd Services

**Server service** (`/etc/systemd/system/luxswirl_server.service`):
```ini
[Unit]
Description=LuxSwirl Server
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=luxswirl
WorkingDirectory=/opt/luxswirl/apps/backend
Environment="PATH=/opt/luxswirl/venv/bin"
Environment="PYTHONPATH=/opt/luxswirl/apps/backend:/opt/luxswirl/apps"
EnvironmentFile=/opt/luxswirl/.env
ExecStart=/opt/luxswirl/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 9000
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

**Agent service** (`/etc/systemd/system/luxswirl-agent.service`):
```ini
[Unit]
Description=LuxSwirl Agent
After=network.target luxswirl_server.service
Requires=luxswirl_server.service

[Service]
Type=simple
User=luxswirl
WorkingDirectory=/opt/luxswirl/apps/agent
Environment="PATH=/opt/luxswirl/venv/bin"
Environment="PYTHONPATH=/opt/luxswirl/apps/agent:/opt/luxswirl/apps"
EnvironmentFile=/opt/luxswirl/.env
# Full reports endpoint, ending in /api/v1/reports.
Environment="LUXSWIRL_SERVER_URL=http://localhost:9000/api/v1/reports"
Environment="LUXSWIRL_AGENT_ID=default-agent"
# LUXSWIRL_AUTH_KEY must be set (in .env) — generate it via Settings →
# Registration Keys in the server UI after first login.
ExecStart=/opt/luxswirl/venv/bin/python -m app.agent_main
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

### Step 8: Start Services

```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable services (start on boot)
sudo systemctl enable luxswirl_server
sudo systemctl enable luxswirl-agent

# Start services
sudo systemctl start luxswirl_server
sudo systemctl start luxswirl-agent

# Check status
sudo systemctl status luxswirl_server
sudo systemctl status luxswirl-agent

# View logs
sudo journalctl -u luxswirl_server -f
sudo journalctl -u luxswirl-agent -f
```

---

## Reverse Proxy Setup (HTTPS)

**Why reverse proxy?**
- SSL/TLS termination (HTTPS)
- Let's Encrypt certificate automation
- Rate limiting and DDoS protection
- Load balancing (future)

### Option A: nginx (Recommended)

**Install nginx**:
```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

**Create nginx config** (`/etc/nginx/sites-available/luxswirl`):
```nginx
# Redirect HTTP to HTTPS
server {
    listen 80;
    listen [::]:80;
    server_name luxswirl.example.com;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://$server_name$request_uri;
    }
}

# HTTPS server
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name luxswirl.example.com;

    # SSL certificates (managed by certbot)
    ssl_certificate /etc/letsencrypt/live/luxswirl.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/luxswirl.example.com/privkey.pem;
    ssl_trusted_certificate /etc/letsencrypt/live/luxswirl.example.com/chain.pem;

    # SSL configuration (Mozilla Intermediate)
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers 'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384';
    ssl_prefer_server_ciphers off;
    ssl_session_timeout 1d;
    ssl_session_cache shared:SSL:10m;
    ssl_session_tickets off;

    # Security headers
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    # Proxy settings
    location / {
        proxy_pass http://127.0.0.1:9000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket support (for HTMX live updates)
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Timeouts
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    # Rate limiting (optional)
    limit_req_zone $binary_remote_addr zone=luxswirl_limit:10m rate=10r/s;
    limit_req zone=luxswirl_limit burst=20 nodelay;

    # Access logs
    access_log /var/log/nginx/luxswirl-access.log;
    error_log /var/log/nginx/luxswirl-error.log;
}
```

**Enable site and obtain SSL certificate**:
```bash
# Enable site
sudo ln -s /etc/nginx/sites-available/luxswirl /etc/nginx/sites-enabled/

# Test nginx config
sudo nginx -t

# Reload nginx
sudo systemctl reload nginx

# Obtain Let's Encrypt certificate
sudo certbot --nginx -d luxswirl.example.com

# Test certificate renewal
sudo certbot renew --dry-run
```

**Auto-renewal** (certbot creates cron job automatically):
```bash
# Verify cron job exists
sudo systemctl list-timers | grep certbot
```

### Option B: Traefik (Alternative)

**Compose with Traefik** (no `version:` key needed):
```yaml
services:
  traefik:
    image: traefik:v2.10
    command:
      - "--api.insecure=false"
      - "--providers.docker=true"
      - "--providers.docker.exposedbydefault=false"
      - "--entrypoints.web.address=:80"
      - "--entrypoints.websecure.address=:443"
      - "--certificatesresolvers.letsencrypt.acme.tlschallenge=true"
      - "--certificatesresolvers.letsencrypt.acme.email=admin@example.com"
      - "--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json"
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - "/var/run/docker.sock:/var/run/docker.sock:ro"
      - "traefik_letsencrypt:/letsencrypt"
    restart: unless-stopped

  luxswirl_server:
    image: ghcr.io/luxardolabs/luxswirl-backend:latest
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.luxswirl.rule=Host(`luxswirl.example.com`)"
      - "traefik.http.routers.luxswirl.entrypoints=websecure"
      - "traefik.http.routers.luxswirl.tls.certresolver=letsencrypt"
      - "traefik.http.services.luxswirl.loadbalancer.server.port=9000"
    # ... rest of server config

volumes:
  traefik_letsencrypt:
```

---

## Database Configuration

### PostgreSQL Tuning

**Edit PostgreSQL config** (`/etc/postgresql/14/main/postgresql.conf`):
```conf
# Memory settings (adjust based on server RAM)
shared_buffers = 2GB                    # 25% of total RAM
effective_cache_size = 6GB              # 75% of total RAM
maintenance_work_mem = 512MB
work_mem = 50MB

# Checkpoint settings
checkpoint_completion_target = 0.9
wal_buffers = 16MB
default_statistics_target = 100

# Query planner
random_page_cost = 1.1                  # For SSD (default 4.0 is for HDD)
effective_io_concurrency = 200          # For SSD

# Parallel query
max_parallel_workers_per_gather = 2
max_parallel_workers = 4

# Connection settings
max_connections = 200

# Logging
logging_collector = on
log_directory = 'log'
log_filename = 'postgresql-%Y-%m-%d.log'
log_rotation_age = 1d
log_min_duration_statement = 1000       # Log slow queries (>1s)
```

**Restart PostgreSQL**:
```bash
sudo systemctl restart postgresql
```

### TimescaleDB-Specific Configuration

**Enable compression** (via LuxSwirl web UI):
1. Navigate to Settings → Database Health
2. Set "Compress after": 7 days
3. Compression runs automatically (background job)

**Set retention policy** (via LuxSwirl web UI):
1. Settings → Database Health
2. Set "Retention period": 90 days (or 30/60/180/365)
3. Old data deleted automatically

**Manual configuration** (advanced):
```sql
-- Connect to database
sudo -u postgres psql -d luxswirl

-- Enable compression on check_results hypertable
SELECT add_compression_policy('check_results', INTERVAL '7 days');

-- Set retention policy (delete data older than 90 days)
SELECT add_retention_policy('check_results', INTERVAL '90 days');

-- View compression stats
SELECT * FROM timescaledb_information.compressed_chunk_stats;
```

### Database Backups

**Automated pg_dump backup** (cron job):

Create backup script (`/opt/luxswirl/backup.sh`):
```bash
#!/bin/bash
set -e

BACKUP_DIR="/var/backups/swirl"
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/luxswirl_backup_$DATE.sql.gz"

# Create backup directory
mkdir -p $BACKUP_DIR

# Run pg_dump (compressed)
pg_dump -h localhost -U luxswirl -d luxswirl | gzip > $BACKUP_FILE

# Keep only last 7 days of backups
find $BACKUP_DIR -name "luxswirl_backup_*.sql.gz" -mtime +7 -delete

# Upload to S3 (optional)
# aws s3 cp $BACKUP_FILE s3://my-bucket/luxswirl-backups/

echo "Backup completed: $BACKUP_FILE"
```

**Schedule daily backups** (cron):
```bash
# Edit crontab
sudo crontab -e

# Add daily backup at 2 AM
0 2 * * * /opt/luxswirl/backup.sh >> /var/log/luxswirl-backup.log 2>&1
```

**Restore from backup**:
```bash
# Stop services
sudo systemctl stop luxswirl_server luxswirl-agent

# Drop and recreate database
sudo -u postgres psql -c "DROP DATABASE luxswirl;"
sudo -u postgres psql -c "CREATE DATABASE luxswirl OWNER luxswirl;"

# Restore from backup
gunzip -c /var/backups/swirl/luxswirl_backup_20250115_020000.sql.gz | sudo -u postgres psql -d luxswirl

# Restart services
sudo systemctl start luxswirl_server luxswirl-agent
```

---

## Security Hardening

### 1. Firewall Configuration

**UFW (Ubuntu/Debian)**:
```bash
# Install UFW
sudo apt install -y ufw

# Default policies
sudo ufw default deny incoming
sudo ufw default allow outgoing

# Allow SSH (important: do this first!)
sudo ufw allow 22/tcp

# Allow HTTP/HTTPS
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Enable firewall
sudo ufw enable

# Check status
sudo ufw status verbose
```

### 2. Fail2Ban (Brute Force Protection)

```bash
# Install fail2ban
sudo apt install -y fail2ban

# Create LuxSwirl filter
sudo nano /etc/fail2ban/filter.d/luxswirl.conf
```

**LuxSwirl fail2ban filter**:
```ini
[Definition]
failregex = ^.*Failed login attempt from <HOST>.*$
ignoreregex =
```

**Enable LuxSwirl jail** (`/etc/fail2ban/jail.local`):
```ini
[luxswirl]
enabled = true
port = http,https
filter = luxswirl
logpath = /var/log/nginx/luxswirl-access.log
maxretry = 5
bantime = 3600
findtime = 600
```

**Restart fail2ban**:
```bash
sudo systemctl restart fail2ban
sudo fail2ban-client status luxswirl
```

### 3. Database Security

**Restrict PostgreSQL to localhost** (`/etc/postgresql/14/main/pg_hba.conf`):
```conf
# Only allow local connections
local   all             all                                     peer
host    all             all             127.0.0.1/32            scram-sha-256
host    all             all             ::1/128                 scram-sha-256

# Deny all other connections
host    all             all             0.0.0.0/0               reject
```

**Restart PostgreSQL**:
```bash
sudo systemctl restart postgresql
```

### 4. Regular Updates

**Enable unattended upgrades** (Ubuntu/Debian):
```bash
# Install
sudo apt install -y unattended-upgrades

# Configure
sudo dpkg-reconfigure -plow unattended-upgrades

# Test
sudo unattended-upgrades --dry-run --debug
```

### 5. Monitoring and Alerting

**Monitor disk space**:
```bash
# Install monitoring tools
sudo apt install -y sysstat ncdu

# Add to cron (daily check)
0 6 * * * df -h | grep -E '9[0-9]%|100%' && echo "Disk space critical" | mail -s "LuxSwirl Disk Alert" admin@example.com
```

---

## Monitoring LuxSwirl Itself

**Use LuxSwirl to monitor LuxSwirl** (dogfooding):

### Check 1: Server Health Endpoint

```
Check Type: http
Target: http://localhost:9000/health
Expected Status: 200
Interval: 60 seconds
```

### Check 2: Database Connection

```
Check Type: postgres
Target: postgresql://luxswirl:password@localhost:5432/luxswirl
Query: SELECT COUNT(*) FROM check_results WHERE timestamp > NOW() - INTERVAL '5 minutes'
Interval: 300 seconds
```

### Check 3: Agent Connectivity

```
Check Type: http
Target: http://localhost:9000/api/v1/agents
Expected: 200 status (validates API is responding)
Interval: 120 seconds
```

### External Monitoring (Recommended)

**Use separate monitoring tool** to monitor LuxSwirl itself:
- Uptime Kuma (simple)
- Prometheus + Grafana (advanced)
- Third-party service (UptimeRobot, Pingdom)

**Why**: Avoid single point of failure (if LuxSwirl is down, it can't alert you).

---

## Backup and Disaster Recovery

### Backup Strategy

**What to backup**:
1. **Database**: PostgreSQL full backup (pg_dump)
2. **Configuration**: `.env` file, compose files (`compose.yaml` + overlays)
3. **Agent credentials**: `/app/data/agent_credentials.json`
4. **SSL certificates**: `/etc/letsencrypt` (if using Let's Encrypt)

**Backup frequency**:
- **Daily**: Database (incremental)
- **Weekly**: Full database dump
- **On change**: Configuration files

### Disaster Recovery Plan

**Scenario: Complete server failure**

**Recovery steps**:
1. **Provision new server** (same specs or better)
2. **Install LuxSwirl** (follow installation guide)
3. **Restore database**:
   ```bash
   gunzip -c backup.sql.gz | sudo -u postgres psql -d luxswirl
   ```
4. **Restore configuration**: Copy `.env` and the compose files
5. **Restore SSL certificates**: Copy `/etc/letsencrypt` directory
6. **Start services**: `sudo docker compose up -d`
7. **Verify**: Check web UI and agent connections

**Recovery Time Objective (RTO)**: 30-60 minutes with good backups

**Recovery Point Objective (RPO)**: 24 hours (with daily backups)

---

## Upgrading

### Docker Compose Upgrades

```bash
# Stop services
cd /opt/luxswirl
sudo docker compose down

# Backup database first (important!)
sudo docker compose exec timescaledb pg_dump -U luxswirl luxswirl | gzip > backup_pre_upgrade.sql.gz

# Pull latest images
sudo docker compose pull

# Start services with new images
sudo docker compose up -d

# Verify upgrade
sudo docker compose logs -f server
```

**Rollback** (if upgrade fails):
```bash
# Stop services
sudo docker compose down

# Restore database
gunzip -c backup_pre_upgrade.sql.gz | sudo docker compose exec -T timescaledb psql -U luxswirl luxswirl

# Use previous image version
# Edit your compose file, pin a specific version tag:
# image: ghcr.io/luxardolabs/luxswirl-backend:v1.0.0

# Start with old version
sudo docker compose up -d
```

### Manual Installation Upgrades

```bash
# Stop services
sudo systemctl stop luxswirl_server luxswirl-agent

# Backup database
sudo -u postgres pg_dump swirl | gzip > /var/backups/swirl/pre_upgrade.sql.gz

# Pull latest code
cd /opt/luxswirl
sudo -u luxswirl git pull origin main

# Activate virtualenv
sudo -u luxswirl -i
cd /opt/luxswirl
source venv/bin/activate

# Upgrade dependencies
cd apps/backend && poetry update && cd ../..

# Run migrations (if available)
cd apps/backend
PYTHONPATH=.:.. python -m alembic upgrade head

# Start services
sudo systemctl start luxswirl_server luxswirl-agent

# Verify
sudo systemctl status luxswirl_server
sudo journalctl -u luxswirl_server -f
```

---

## Troubleshooting

### Server Won't Start

**Check logs**:
```bash
# Docker Compose
sudo docker compose logs server

# systemd
sudo journalctl -u luxswirl_server -f
```

**Common issues**:
1. **Database connection failed**: Verify database is running and credentials are correct
2. **Port 9000 already in use**: Check if another process is using port 9000 (`sudo netstat -tlnp | grep 9000`)
3. **Permission denied**: Ensure data directories have correct ownership (`sudo chown -R luxswirl:luxswirl /opt/luxswirl`)

### Agent Won't Connect

**Debug agent**:
```bash
# View agent logs
sudo docker compose logs luxswirl_agent

# Test server from agent container
sudo docker compose exec luxswirl_agent curl http://luxswirl_server:9000/health
```

**Common issues**:
1. **Authentication failed**: `LUXSWIRL_AUTH_KEY` is missing, mistyped, or expired. Create a fresh key in **Settings → Registration Keys**, update it in `.env`, and restart the agent.
2. **Wrong server URL**: `LUXSWIRL_SERVER_URL` must be the full reports endpoint ending in `/api/v1/reports`.
3. **Network isolation**: Ensure the agent can reach the server (check firewall, DNS).

### Database Performance Issues

**Check slow queries**:
```sql
-- Connect to database
sudo -u postgres psql -d luxswirl

-- Enable pg_stat_statements (if not already)
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- View slow queries
SELECT query, calls, total_time, mean_time
FROM pg_stat_statements
ORDER BY mean_time DESC
LIMIT 10;
```

**Solutions**:
1. Enable compression (Settings → Database Health)
2. Set retention policy (delete old data)
3. Increase database RAM (shared_buffers, effective_cache_size)
4. Use SSD instead of HDD

### SSL Certificate Issues

**Renewal failed**:
```bash
# Check certbot logs
sudo journalctl -u certbot

# Manual renewal attempt
sudo certbot renew --dry-run

# Force renewal (if within 30 days of expiry)
sudo certbot renew --force-renewal
```

**Common issues**:
1. **Port 80 blocked**: Let's Encrypt requires port 80 for HTTP-01 challenge
2. **DNS not pointing to server**: Verify A record: `dig luxswirl.example.com`
3. **Rate limit hit**: Let's Encrypt has rate limits (5 certificates per week per domain)

---

## Next Steps

**After successful installation**:

1. **Create your admin account**: on first launch the app redirects to `/setup` to create the admin (username + password — no default credentials). For unattended installs, set `SECURITY__INITIAL_ADMIN_PASSWORD` before first boot instead; a password change is forced on first login.
2. **Create checks**: Dashboard → Create Check
3. **Set up notifications**: Settings → Notifications
4. **Deploy additional agents**: See [Quickstart](../quickstart/quickstart.md#step-10-add-more-agents-optional)
5. **Configure database optimization**: Settings → Database Health
6. **Create status pages**: Status Pages → Create Status Page

**Documentation**:
- [User Guide](../user-guide/) - Complete feature documentation
- [FAQ](../user-guide/faq.md) - Frequently asked questions
- [SECURITY.md](../../SECURITY.md) - Security policy

**Support**:
- GitHub Issues: https://github.com/luxardolabs/luxswirl/issues
- GitHub Discussions: https://github.com/luxardolabs/luxswirl/discussions

---

