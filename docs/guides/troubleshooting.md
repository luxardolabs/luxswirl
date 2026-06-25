# Troubleshooting Guide

**Quick diagnosis and solutions for common LuxSwirl issues.**


---

## Table of Contents

1. [Agent Issues](#agent-issues)
2. [Server Issues](#server-issues)
3. [Check Issues](#check-issues)
4. [Database Issues](#database-issues)
5. [Web UI Issues](#web-ui-issues)
6. [SSL/HTTPS Issues](#sslhttps-issues)
7. [Performance Issues](#performance-issues)
8. [Import/Export Issues](#importexport-issues)
9. [Notification Issues](#notification-issues)
10. [Docker Issues](#docker-issues)

---

## Agent Issues

### Agent Won't Connect to Server

**Symptoms**:
- Agent logs: `Failed to push report: Connection refused`
- Server logs: No agent heartbeat received
- Web UI: Agent shows as offline or not listed

**Diagnosis**:
```bash
# Check agent logs
docker logs luxswirl_agent | tail -50

# Test server from agent container
docker exec luxswirl_agent curl http://server:9000/health
```

**Common Causes & Solutions**:

#### 1. Server not running
```bash
# Check server status
docker ps | grep server

# If not running, start it
docker compose up -d server
```

#### 2. Wrong server URL
```bash
# Check agent's configured URL
docker exec luxswirl_agent env | grep LUXSWIRL_SERVER_URL

# Fix in compose.yaml or .env
LUXSWIRL_SERVER_URL=http://server:9000  # For Docker network
# OR
LUXSWIRL_SERVER_URL=https://server.example.com  # For external
```

#### 3. Network connectivity issue
```bash
# From agent host, test server
curl https://server.example.com/health

# Expected: {"status":"healthy"}
# If fails: Check firewall, DNS, routing
```

#### 4. Authentication failure
```bash
# Check agent credentials exist
docker exec luxswirl_agent ls -la /app/data/agent_credentials.json

# If missing or corrupted, delete and restart
docker exec luxswirl_agent rm /app/data/agent_credentials.json
docker compose restart agent
# Agent will re-register automatically
```

#### 5. HTTPS certificate error
```bash
# Agent logs show: "SSL certificate verify failed"

# Temporary workaround (testing only):
docker compose down
# Edit compose.yaml, add:
# environment:
#   LUXSWIRL_VERIFY_SSL: "false"
docker compose up -d

# Proper fix: Use valid SSL certificate (Let's Encrypt)
```

#### 6. Server requires HTTPS but agent using HTTP
```bash
# Agent logs: "HTTPS required for external servers"

# Fix: Update agent URL to HTTPS
LUXSWIRL_SERVER_URL=https://server.example.com
```

**Still not working?**
```bash
# Enable debug logging
docker compose down
# Edit compose.yaml, add:
# environment:
#   LOG_LEVEL: DEBUG
docker compose up -d

# Watch logs in real-time
docker logs -f luxswirl_agent
```

---

### Agent Shows as "Pending" or "Paused"

**Symptoms**:
- Agent connects but checks don't run
- Agent logs: `403 Forbidden - Agent is pending approval`

**Solution**:
1. Web UI → Agents page
2. Find agent in "Pending Agents" section
3. Click "Approve" button
4. Agent status changes to "Active" (green)
5. Checks begin running within 60 seconds

**Related statuses**:
- **Pending**: New agent awaiting admin approval → Click "Approve"
- **Paused**: Admin paused agent → Click "Resume"
- **Disabled**: Admin disabled agent → Click "Enable"
- **Rejected**: Admin rejected agent → Delete and re-register

---

### Agent Restarts Keep Failing Registration

**Symptoms**:
- Agent logs: `Failed to decrypt credentials - encryption key may have changed`
- Agent repeatedly tries to register on every restart

**Cause**: Container hostname or machine-id changed (credential decryption key derived from these)

**Solution**:
```bash
# Delete stored credentials (agent will re-register)
docker exec luxswirl_agent rm -f /app/data/agent_credentials.json

# Restart agent
docker compose restart agent

# Approve agent in web UI (if approval required)
```

---

### Agent Runs Out of Memory

**Symptoms**:
- Agent container killed by OOM (Out of Memory)
- Docker logs: `Killed`
- System logs: `Out of memory: Kill process`

**Diagnosis**:
```bash
# Check memory usage
docker stats luxswirl_agent

# Check number of checks assigned
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:9000/api/v1/agents/AGENT_ID/checks | jq '. | length'
```

**Solutions**:

**1. Too many checks (>500)**:
```bash
# Reduce checks per agent (recommended max: 500)
# Deploy additional agents and distribute checks
```

**2. Increase container memory limit**:
```yaml
# compose.yaml
services:
  agent:
    deploy:
      resources:
        limits:
          memory: 512M  # Increase from default 256M
```

**3. Reduce concurrent checks**:
```yaml
# compose.yaml
services:
  agent:
    environment:
      MAX_CONCURRENT_CHECKS: 100  # Reduce from default 200
```

---

## Server Issues

### Server Won't Start

**Symptoms**:
- `docker compose up` fails
- Server container exits immediately
- Error in logs

**Diagnosis**:
```bash
# View startup logs
docker compose logs server | tail -100
```

**Common Causes & Solutions**:

#### 1. Database connection failed
```bash
# Error: "could not connect to server: Connection refused"

# Check database is running
docker compose ps timescaledb

# If not running:
docker compose up -d timescaledb

# Wait for health check (10-20 seconds)
docker compose ps timescaledb
# STATUS should show (healthy)
```

#### 2. Database credentials wrong
```bash
# Error: "FATAL: password authentication failed"

# Check .env file credentials match
cat .env | grep DATABASE
# DATABASE__PASSWORD should match compose.yaml POSTGRES_PASSWORD
```

#### 3. Port 9000 already in use
```bash
# Error: "Address already in use"

# Find process using port 9000
sudo netstat -tlnp | grep :9000
# OR
sudo lsof -i :9000

# Kill process or change LuxSwirl port
# Edit compose.yaml:
# ports:
#   - "9001:9000"  # Use external port 9001 instead
```

#### 4. Missing environment variables
```bash
# Error: "SECRET_KEY is required"

# Generate secret key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Add to .env:
SECRET_KEY=generated_key_here
```

#### 5. Database schema not initialized
```bash
# Error: "relation 'agents' does not exist"

# The server runs `alembic upgrade head` automatically on boot. If migrations
# didn't run (e.g. the DB wasn't healthy yet), restart the server, or run them
# manually inside the container:
docker compose exec luxswirl_server alembic upgrade head
```

---

### Server Crashes Randomly

**Symptoms**:
- Server runs for hours/days then crashes
- No obvious error in logs
- Container restarts automatically

**Diagnosis**:
```bash
# Check for OOM (Out of Memory)
docker inspect luxswirl_server | grep OOMKilled

# Check memory usage before crash
docker stats luxswirl_server --no-stream
```

**Solutions**:

**1. Out of memory**:
```yaml
# compose.yaml - Increase memory
services:
  server:
    deploy:
      resources:
        limits:
          memory: 2G  # Increase from 1G
```

**2. Too many database connections**:
```yaml
# .env - Reduce connection pool
DATABASE__POOL_SIZE=10
DATABASE__MAX_OVERFLOW=5
```

**3. TimescaleDB compression job consuming resources**:
```sql
-- Check compression job status
SELECT * FROM timescaledb_information.jobs;

-- Reduce compression frequency if needed
SELECT alter_job(job_id, schedule_interval => INTERVAL '1 day')
FROM timescaledb_information.jobs
WHERE proc_name = 'policy_compression';
```

---

## Check Issues

### Checks Always Timing Out

**Symptoms**:
- Check results show "Timeout after 30s"
- Checks that should succeed are failing
- Latency increasing over time

**Diagnosis**:
```bash
# Test check manually from agent host
docker exec luxswirl_agent curl -v -m 30 https://api.example.com/health

# Check agent CPU/memory
docker stats luxswirl_agent
```

**Common Causes & Solutions**:

#### 1. Target is actually slow (>30s response)
```yaml
# Increase timeout for specific check
# Web UI → Checks → Edit Check → Timeout: 60 seconds
```

#### 2. Agent overloaded (too many concurrent checks)
```bash
# Check number of checks
curl -H "Authorization: Bearer TOKEN" \
  http://localhost:9000/api/v1/agents/AGENT_ID/checks | jq '. | length'

# If >500: Deploy additional agent, distribute checks
# OR reduce concurrency:
# compose.yaml
environment:
  MAX_CONCURRENT_CHECKS: 100  # Reduce from 200
```

#### 3. DNS resolution slow
```bash
# Test DNS from agent
docker exec luxswirl_agent dig api.example.com

# If slow, specify DNS servers:
# compose.yaml
dns:
  - 8.8.8.8
  - 8.8.4.4
```

#### 4. Network latency (agent far from target)
```bash
# Measure network latency
docker exec luxswirl_agent ping -c 4 api.example.com

# If high latency (>500ms):
# Deploy agent closer to target (same region)
```

---

### Check Shows "Down" but Target is Up

**Symptoms**:
- Check fails but manual test succeeds
- Error message unclear or generic

**Diagnosis**:
```bash
# View exact error from check detail
# Web UI → Dashboard → Click check → View error message

# Test manually with same parameters
docker exec luxswirl_agent curl -v \
  -H "Header: Value" \
  -X POST \
  -d '{"key":"value"}' \
  https://api.example.com/endpoint
```

**Common Causes & Solutions**:

#### 1. Wrong expected status code
```
Error: "Expected 200, got 201"
Solution: Update check config → Expected Status: 201
```

#### 2. SSL certificate validation failing
```
Error: "SSL certificate verify failed"
Solution:
- Fix certificate (recommended)
- OR disable verification (testing only):
  Check config → Verify SSL: No
```

#### 3. HTTP method mismatch
```
Error: "405 Method Not Allowed"
Solution: Check config → Method: POST (not GET)
```

#### 4. Missing authentication headers
```
Error: "401 Unauthorized"
Solution: Check config → Headers:
  Authorization: Bearer your_token_here
```

#### 5. JSON check query wrong
```
Error: "JSONata query returned null"
Solution: Test query at https://try.jsonata.org
  - Verify path is correct
  - Check for typos in field names
  - Use quotes for keys with dots: `printers."printer.with.dots".status`
```

---

### Check Results Not Appearing

**Symptoms**:
- Check configured but no results in dashboard
- No errors in agent logs
- Check shows as never executed

**Diagnosis**:
```bash
# Check agent logs for check execution
docker logs luxswirl_agent | grep "check-name"

# Check if results are being rejected
docker logs luxswirl_server | grep "rejected"
```

**Common Causes & Solutions**:

#### 1. Agent not approved
```
Solution: Web UI → Agents → Approve agent
```

#### 2. Check interval too long
```
Check config: Interval: 300 seconds (5 minutes)
Wait 5 minutes for first result
```

#### 3. Results failing validation
```bash
# Server logs: "Validation error: check_id required"
# Agent needs update (old version sending wrong format)
docker compose pull agent
docker compose up -d agent
```

#### 4. Check disabled
```
Web UI → Checks → Edit Check → Enabled: Yes
```

---

## Database Issues

### Database Growing Too Fast

**Symptoms**:
- Disk space alerts
- Database >100 GB after short time
- Queries slowing down

**Diagnosis**:
```bash
# Check database size
docker exec luxswirl_timescaledb psql -U luxswirl -c "
  SELECT pg_size_pretty(pg_database_size('luxswirl'));
"

# Check table sizes
docker exec luxswirl_timescaledb psql -U luxswirl -d luxswirl -c "
  SELECT
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename))
  FROM pg_tables
  WHERE schemaname = 'public'
  ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
"
```

**Solutions** (in order of impact):

#### 1. Enable compression (80-90% reduction)
```
Web UI → Settings → Database Health
Compress after: 7 days
Wait 24 hours for compression job to run
```

#### 2. Set retention policy (delete old data)
```
Web UI → Settings → Database Health
Retention period: 90 days
Old data deleted daily
```

#### 3. Reduce check frequency
```
Web UI → Checks → Edit checks
Increase interval: 60s → 120s
Halves data volume
```

#### 4. Delete unused checks
```
Web UI → Checks → Delete obsolete checks
```

**Manual compression** (if UI not working):
```bash
docker exec luxswirl_timescaledb psql -U luxswirl -d luxswirl -c "
  SELECT add_compression_policy('check_results', INTERVAL '7 days');
  SELECT compress_chunk(chunk)
  FROM show_chunks('check_results', older_than => INTERVAL '7 days') AS chunk;
"
```

---

### Queries Timing Out

**Symptoms**:
- Dashboard takes >10 seconds to load
- Charts don't render
- "504 Gateway Timeout" errors

**Diagnosis**:
```sql
-- Check for slow queries
docker exec luxswirl_timescaledb psql -U luxswirl -d luxswirl -c "
  SELECT query, calls, total_time, mean_time
  FROM pg_stat_statements
  ORDER BY mean_time DESC
  LIMIT 10;
"

-- Check database size
SELECT pg_size_pretty(pg_database_size('luxswirl'));
```

**Solutions**:

#### 1. Enable compression (faster queries)
```
Compressed data = less disk I/O = faster queries
Web UI → Settings → Database Health → Enable compression
```

#### 2. Increase database RAM
```yaml
# compose.yaml
services:
  timescaledb:
    environment:
      # Add more memory (25% of host RAM)
      shared_buffers: 2GB
      effective_cache_size: 6GB
```

#### 3. Reduce dashboard time range
```
Dashboard → Time selector → 4h (instead of 7d)
Fewer rows to query = faster load
```

#### 4. Vacuum and analyze
```bash
docker exec luxswirl_timescaledb psql -U luxswirl -d luxswirl -c "
  VACUUM ANALYZE;
"
```

---

### Database Connection Pool Exhausted

**Symptoms**:
- Errors: "FATAL: sorry, too many clients already"
- Intermittent 500 errors from API
- Server logs: "QueuePool limit exceeded"

**Solutions**:

#### 1. Increase PostgreSQL max_connections
```bash
# Edit postgresql.conf
docker exec luxswirl_timescaledb bash -c "
  echo 'max_connections = 200' >> /var/lib/postgresql/data/postgresql.conf
"

# Restart database
docker compose restart timescaledb
```

#### 2. Reduce server pool size
```yaml
# .env
DATABASE__POOL_SIZE=10  # Reduce from 20
DATABASE__MAX_OVERFLOW=5  # Reduce from 10
```

#### 3. Fix connection leaks
```bash
# Check for connections not being closed
docker exec luxswirl_timescaledb psql -U luxswirl -c "
  SELECT count(*) FROM pg_stat_activity WHERE datname='luxswirl';
"

# If consistently high (>50): Restart server
docker compose restart server
```

---

## Web UI Issues

### Dashboard Not Loading

**Symptoms**:
- Blank page or "Loading..." forever
- Browser console errors
- 500 errors in network tab

**Diagnosis**:
```bash
# Check server logs
docker logs luxswirl_server | tail -50

# Check browser console
# Press F12 → Console tab → Look for errors
```

**Common Causes & Solutions**:

#### 1. JavaScript error
```
Browser console: "Uncaught ReferenceError: Chart is not defined"
Solution: Hard refresh browser (Ctrl+Shift+R)
Clear cache and reload
```

#### 2. Database query timeout
```
Server logs: "query timeout"
Solution: Reduce dashboard time range
OR enable database compression
```

#### 3. Too many checks (>1,000)
```
Solution: Use filters (by agent, type, or status)
Pagination helps but filtering is better
```

#### 4. Session expired
```
Symptoms: Redirect to login page
Solution: Log in again
```

---

### Check Detail Panel Not Opening

**Symptoms**:
- Click check in dashboard, nothing happens
- Panel flickers but doesn't stay open
- JavaScript errors in console

**Diagnosis**:
```
Browser console (F12) → Look for HTMX errors
Network tab → Check for 404 or 500 responses
```

**Solutions**:

#### 1. HTMX request failed
```
Network tab shows: 500 Internal Server Error
Check server logs for error details
Usually database query issue
```

#### 2. Check has no history
```
Panel shows "No data available"
Normal for newly created checks
Wait for first check execution (interval time)
```

#### 3. Browser cache issue
```
Hard refresh: Ctrl+Shift+R
Clear site data: F12 → Application → Clear storage
```

---

### Status Page Not Displaying

**Symptoms**:
- Status page URL shows 404
- Status page blank or broken
- Checks not appearing on status page

**Solutions**:

#### 1. Wrong URL
```
Correct format: https://server.example.com/status/PAGE_SLUG
Not: /status-pages/PAGE_SLUG
Check exact slug in status page settings
```

#### 2. Status page has no checks
```
Web UI → Status Pages → Edit page
Add checks to display
Save
```

#### 3. All checks marked as hidden
```
Status page settings → Unhide checks
OR add new checks
```

---

## SSL/HTTPS Issues

### Let's Encrypt Certificate Renewal Failed

**Symptoms**:
- Certificate expired
- Browser shows "Not secure"
- Certbot logs show failure

**Diagnosis**:
```bash
# Check certificate expiry
sudo certbot certificates

# Check certbot logs
sudo journalctl -u certbot
```

**Common Causes & Solutions**:

#### 1. Port 80 blocked
```
Error: "Could not bind to port 80"
Solution: Ensure nginx allows certbot challenge
  # nginx config:
  location /.well-known/acme-challenge/ {
    root /var/www/html;
  }
```

#### 2. DNS not pointing to server
```
Error: "DNS lookup failed"
Solution: Verify A record
  dig luxswirl.example.com
```

#### 3. Rate limit hit
```
Error: "too many certificates"
Solution: Wait 7 days (Let's Encrypt limit: 5 certs/week)
OR use different subdomain
```

**Manual renewal**:
```bash
sudo certbot renew --force-renewal
sudo systemctl reload nginx
```

---

### Self-Signed Certificate Errors

**Symptoms**:
- Agent logs: "SSL certificate verify failed"
- Browser warning: "Your connection is not private"

**Solutions**:

#### 1. Use Let's Encrypt (recommended)
```bash
sudo certbot --nginx -d luxswirl.example.com
```

#### 2. Disable SSL verification (testing only)
```yaml
# compose.yaml (agent)
environment:
  LUXSWIRL_VERIFY_SSL: "false"
```

#### 3. Add self-signed cert to trust store
```bash
# On agent host
sudo cp server.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates
```

---

## Performance Issues

### High CPU Usage

**Symptoms**:
- CPU constantly >80%
- Slow response times
- Container throttling

**Diagnosis**:
```bash
# Check CPU usage
docker stats

# Check which service
top -c
```

**Solutions**:

#### Agent high CPU:
```
1. Reduce concurrent checks:
   MAX_CONCURRENT_CHECKS=50

2. Increase check intervals:
   60s → 120s

3. Deploy more agents (horizontal scaling)
```

#### Server high CPU:
```
1. Enable database compression (reduces query load)
2. Add database indexes (if needed)
3. Reduce dashboard refresh rate (10s → 30s)
```

#### Database high CPU:
```
1. Enable compression
2. Vacuum database
3. Increase shared_buffers
4. Add more RAM
```

---

### High Memory Usage

**Symptoms**:
- Memory usage >90%
- OOM kills
- Swapping

**Diagnosis**:
```bash
docker stats --no-stream
free -h
```

**Solutions**:

#### Agent:
```yaml
# Reduce batch size
environment:
  REPORT_BATCH_SIZE: 250  # From 500
```

#### Server:
```yaml
# Reduce connection pool
environment:
  DATABASE__POOL_SIZE: 5  # From 10
```

#### Database:
```yaml
# Reduce shared_buffers
environment:
  shared_buffers: 512MB  # From 2GB
```

---

## Import/Export Issues

### Import Fails with Validation Error

**Symptoms**:
- Error: "Invalid JSON format"
- Error: "Missing required field: check_type"
- Import button shows errors

**Diagnosis**:
```bash
# Validate JSON syntax
cat checks.json | jq .

# Check for required fields
cat checks.json | jq '.checks[0]'
```

**Common Causes & Solutions**:

#### 1. JSON syntax error
```
Error: "Unexpected token"
Solution: Validate at https://jsonlint.com
Common issues: Missing commas, trailing commas
```

#### 2. Missing required fields
```json
// Required fields:
{
  "display_name": "Check Name",
  "check_type": "http",
  "target": "https://example.com",
  "interval": 60
}
```

#### 3. Invalid agent_id (UUID)
```
Error: "Invalid UUID format"
Solution: Get valid agent_id from Web UI → Agents
OR remove agent_id (will use current agent)
```

---

### Export File Empty or Missing Checks

**Symptoms**:
- Downloaded JSON has `"checks": []`
- Some checks missing from export

**Solutions**:

#### 1. Agent has no checks
```
Add checks to agent first
Then export
```

#### 2. Filtered view active
```
Clear filters before exporting
Export includes all checks (not just visible)
```

#### 3. Wrong agent selected
```
Verify correct agent in dropdown
Each agent exports separately
```

---

## Notification Issues

### Email Notifications Not Sending

**Symptoms**:
- Alerts triggered but no email received
- Error in logs: "SMTP connection failed"

**Diagnosis**:
```bash
# Test SMTP manually
docker exec luxswirl_server python -c "
import smtplib
server = smtplib.SMTP('smtp.gmail.com', 587)
server.starttls()
server.login('user@example.com', 'password')
print('SMTP connection successful')
server.quit()
"
```

**Common Causes & Solutions**:

#### 1. Wrong SMTP credentials
```
Gmail: Use App Password, not account password
Generate at: https://myaccount.google.com/apppasswords
```

#### 2. Firewall blocking port 587
```bash
# Test from server host
telnet smtp.gmail.com 587
# Should connect (Ctrl+] then quit to exit)
```

#### 3. "Less secure apps" disabled (Gmail)
```
Gmail deprecated this
Must use App Passwords instead
```

#### 4. Email provider requires allowlist
```
Some providers block automated emails
Check provider's SMTP documentation
```

---

### Webhook Notifications Fail

**Symptoms**:
- Error: "Webhook delivery failed"
- 404 or 500 errors from webhook endpoint

**Diagnosis**:
```bash
# Test webhook manually
curl -X POST -H "Content-Type: application/json" \
  -d '{"test": "message"}' \
  https://hooks.slack.com/services/YOUR/WEBHOOK/URL
```

**Solutions**:

#### 1. Wrong webhook URL
```
Verify URL is correct
Test in Postman or curl first
```

#### 2. Missing Content-Type header
```
Add to notification provider config:
Headers: Content-Type: application/json
```

#### 3. Webhook expects specific format
```
Customize payload template to match
Example for Slack:
{"text": "Alert: {{check_name}} is {{status}}"}
```

---

## Docker Issues

### Cannot Remove Container (Device Busy)

**Symptoms**:
- `docker compose down` fails
- Error: "device or resource busy"

**Solution**:
```bash
# Force remove
docker compose down -v

# If still fails:
docker stop luxswirl_server
docker rm -f luxswirl_server

# Nuclear option (removes all volumes):
docker compose down -v --remove-orphans
```

---

### Volume Permissions Issue

**Symptoms**:
- Container fails to start
- Error: "Permission denied: '/app/data'"

**Solution**:
```bash
# Fix volume ownership
docker compose down
sudo chown -R 1000:1000 ./data
docker compose up -d
```

---

### Docker Build Fails

**Symptoms**:
- `docker compose build` fails
- Error during pip install

**Solutions**:

#### 1. Network issue
```bash
# Retry with --no-cache
docker compose build --no-cache
```

#### 2. Out of disk space
```bash
# Clean up
docker system prune -a
# Removes unused images, containers, volumes
```

---

## Still Need Help?

**If none of these solutions work**:

1. **Enable debug logging**:
```yaml
# compose.yaml
environment:
  LOG_LEVEL: DEBUG
```

2. **Collect diagnostic info**:
```bash
# System info
docker version
docker compose version
uname -a

# Container status
docker compose ps

# Logs (last 100 lines)
docker compose logs --tail=100 server > server.log
docker compose logs --tail=100 agent > agent.log
docker compose logs --tail=100 timescaledb > database.log

# Database info
docker exec luxswirl_timescaledb psql -U luxswirl -c "SELECT version();"
```

3. **Ask for help**:
- GitHub Discussions: https://github.com/luxardolabs/luxswirl/discussions
- GitHub Issues: https://github.com/luxardolabs/luxswirl/issues (for bugs)

**Include in your support request**:
- Symptom description
- What you tried
- Full error messages
- Logs (attached as files)
- Your environment (OS, Docker version, LuxSwirl version)

---

