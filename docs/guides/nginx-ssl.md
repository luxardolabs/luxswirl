# Nginx SSL Termination Setup

## Overview

LuxSwirl uses Nginx for SSL termination. This provides:
- SSL/TLS encryption (HTTPS)
- Security headers (HSTS, X-Frame-Options, etc.)
- Proper proxy configuration
- Real IP forwarding
- Gzip compression
- Health check endpoints

## Architecture

```
Internet (HTTPS, 443)
    ↓
Nginx (SSL termination)
    ↓
LuxSwirl Server API (internal port 9000)
    ↓
TimescaleDB (internal port 5432)
```

## Configuration Files

### 1. Main Nginx Config (`nginx/nginx.conf`)
- Worker process configuration
- Logging format
- Gzip compression
- Includes site-specific configs from `conf.d/`

### 2. Site Config (`nginx/conf.d/luxswirl.conf`)
- **HTTPS** on port 9000 (nginx terminates SSL directly on 9000)
- SSL certificate configuration
- Security headers (HSTS, X-Frame-Options, etc.)
- Upstream definition for server
- Proxy configuration for all API endpoints

## SSL Certificates

Certificates are mounted from the host:
```yaml
volumes:
  - /etc/ssl/certs/your-domain:/etc/nginx/ssl:ro
```

The nginx config expects:
- `/etc/nginx/ssl/fullchain.pem` - Full certificate chain
- `/etc/nginx/ssl/privkey.pem` - Private key

Point these at your own TLS certificate and key (e.g. from Let's Encrypt or your CA).

## Port Configuration

**External access**: `https://luxswirl.example.com` (standard HTTPS on port 443).

**Internal ports** (within the Docker network):
- Nginx container: 9000 (HTTPS / SSL termination)
- Server container: 9000 (HTTP, internal only)
- TimescaleDB: 5432 (internal only)

**Host port mapping** — map the host's standard HTTPS port to the nginx container so users reach it at `https://your-domain` with no port suffix:
- `443:9000` — standard HTTPS (recommended for a dedicated host)

> The bundled `compose.yaml` maps `9000:9000` by default so it doesn't collide with other services already using 443 on a busy host. For a standard public deployment, change the host side to 443 (`443:9000`); access then drops the `:9000`.

## Endpoints

All endpoints are accessible via `https://luxswirl.example.com`:

### API Endpoints
- `GET /api/v1/agents` - List agents
- `POST /api/v1/reports` - Submit check results
- `GET /api/v1/agents/{agent_id}/results` - Get results
- Full API documentation at `/docs`

### Monitoring Endpoints
- `GET /metrics` - Prometheus metrics
- `GET /health` - Health check
- `GET /` - API information

## Security Features

### SSL/TLS Configuration
- TLS 1.2 and 1.3 only
- Modern cipher suites
- 10-minute session timeout
- 10MB shared session cache

### Security Headers
```nginx
X-Frame-Options: SAMEORIGIN
X-Content-Type-Options: nosniff
X-XSS-Protection: 1; mode=block
Referrer-Policy: no-referrer-when-downgrade
Strict-Transport-Security: max-age=31536000; includeSubDomains
```

### Real IP Forwarding
Nginx properly forwards the client's real IP to the backend:
```nginx
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

## Agent Configuration

Agents should connect to the server via:
```bash
LUXSWIRL_SERVER_URL=https://luxswirl.example.com/api/v1/reports
```

**Note**: Internal Docker agents use the internal URL:
```bash
LUXSWIRL_SERVER_URL=http://luxswirl_server:9000/api/v1/reports
```

## Deployment

### Start Services
```bash
docker compose up -d
```

### Check Nginx Status
```bash
docker logs luxswirl_nginx
```

### Test SSL Connection
```bash
curl -v https://luxswirl.example.com/health
```

### Reload Nginx Config (without downtime)
```bash
docker exec luxswirl_nginx nginx -s reload
```

### View Nginx Access Logs
```bash
docker exec luxswirl_nginx tail -f /var/log/nginx/access.log
```

## Troubleshooting

### Certificate Issues
If you see SSL errors, verify certificates exist:
```bash
ls -la /etc/ssl/certs/your-domain/
```

Expected files:
- `fullchain.pem`
- `privkey.pem`

### Nginx Won't Start
Check configuration syntax:
```bash
docker exec luxswirl_nginx nginx -t
```

### Backend Connection Issues
Verify server is healthy:
```bash
docker exec luxswirl_server curl http://localhost:9000/health
```

### Port Conflicts
If port 9000 is in use, update compose.yaml:
```yaml
ports:
  - "9001:9000"  # Use different host port
```
