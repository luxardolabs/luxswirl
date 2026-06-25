# LuxSwirl Quick Start Guide

This guide walks you through deploying LuxSwirl with Docker Compose and creating your first health check.

---

## Prerequisites

**Required**:
- Docker 20.10+ and Docker Compose 2.0+
- 2 GB RAM minimum
- 10 GB disk space

**Optional**:
- Domain name (for production deployment)
- SMTP server (for email notifications)

**Check your Docker version**:
```bash
docker --version
# Expected: Docker version 20.10.0 or higher

docker compose version
# Expected: Docker Compose version 2.0.0 or higher
```

---

## Step 1: Clone the Repository

```bash
git clone https://github.com/luxardolabs/luxswirl.git
cd luxswirl
```

```
Cloning into 'luxswirl'...
```

---

## Step 2: Start the Server with Docker Compose

The published images are pulled from GitHub Container Registry:
- `ghcr.io/luxardolabs/luxswirl-backend` (the server)
- `ghcr.io/luxardolabs/luxswirl-agent` (the agent)

Bring up the server first (database + server). The agent is started later, once you have a registration key.

```bash
docker compose up -d timescaledb luxswirl_server
```

> The agent service requires a `LUXSWIRL_AUTH_KEY` and will refuse to start without one — you generate that key from the server UI in Step 4. A plain `docker compose up -d` would fail on the agent until then.

**Build from source instead** (no published image): the repo ships a Makefile that builds both images locally.
```bash
make build      # builds ghcr.io/luxardolabs/luxswirl-backend + -agent locally
```

**Expected output**:
```
[+] Running 3/3
 ✔ Network luxswirl-network        Created
 ✔ Container luxswirl_timescaledb  Started
 ✔ Container luxswirl_server       Started
```

**What this does**:
- Starts TimescaleDB (PostgreSQL/TimescaleDB) — internal only
- Starts LuxSwirl Server (API + web UI), exposed via nginx on port 9000
- The server auto-runs `alembic upgrade head` on boot, then launches `uvicorn app.main:app`
- Creates a persistent volume for database storage

**Verify services are running**:
```bash
docker compose ps
```

**Expected output**:
```
NAME                   STATUS
luxswirl_timescaledb   running (healthy)
luxswirl_server        running (healthy)
```

**Check logs** (if something went wrong):
```bash
# Server logs
docker compose logs luxswirl_server

# Database logs
docker compose logs timescaledb
```

---

## Step 3: Access the Web UI

Open your browser and navigate to:

```
http://localhost:9000
```

**First-time setup**:

There are **no default credentials**. On first launch (no admin exists yet) the app sends you to a setup page to create your administrator account:

1. Open the URL above — you'll be redirected to **`/setup`**.
2. Choose an admin **username** and **password**, then submit.
3. You're taken to the login page — sign in with the credentials you just set.

Accounts are username + password only (no email).

**Unattended / automation (optional)**: to skip the wizard for scripted deploys, set `SECURITY__INITIAL_ADMIN_PASSWORD` (and optionally `SECURITY__INITIAL_ADMIN_USERNAME`, default `admin`) in `.env` before first boot. The admin is then created automatically on boot and you log in with it — a password change is forced on first login.

---

## Step 4: Create a Registration Key and Start the Agent

The agent authenticates to the server with a **registration key**. You create that key in the UI after logging in, then hand it to the agent via the `LUXSWIRL_AUTH_KEY` environment variable. (The agent does not auto-connect on a plain `compose up` — the compose file hard-fails the agent service if this value is unset.)

1. In the web UI, go to **Settings → Registration Keys**.
2. Click **Create Key** and copy the generated key.
3. Provide it to the agent service (e.g. in a `.env` file at the repo root):
   ```bash
   echo 'LUXSWIRL_AUTH_KEY=<paste-your-key-here>' >> .env
   ```
4. Start the agent:
   ```bash
   docker compose up -d luxswirl_agent
   ```

The bundled agent reaches the server over the internal Docker network at `http://luxswirl_server:9000/api/v1/reports` (the `LUXSWIRL_SERVER_URL` default).

**Verify the agent connected**: navigate to the **Agents** page (sidebar).

**Expected state**:
- **Agent**: `docker-agent` (the bundled agent's `LUXSWIRL_AGENT_ID`)
- **Status**: 🟢 Online (green indicator)
- **Last Seen**: <10 seconds ago
- **Checks**: 0 (no checks created yet)

**If agent shows as offline**:
1. Check agent logs: `docker compose logs luxswirl_agent`
2. Look for connection errors or authentication failures (a bad/expired `LUXSWIRL_AUTH_KEY` is the most common cause)
3. Verify server is accessible: `curl http://localhost:9000/health`

---

## Step 5: Create Your First Check

Let's create a simple HTTP check to monitor a public API.

### Option A: Via Web UI (Recommended)

1. **Navigate to Checks** (sidebar → Checks)
2. Click **"Create Check"** button
3. Fill in the form:

**Basic Settings**:
- **Agent**: Select `docker-agent` (or your agent's name)
- **Display Name**: `GitHub API Health`
- **Check Type**: `HTTP`

**HTTP Settings**:
- **Target URL**: `https://api.github.com/zen`
- **Expected Status Code**: `200`
- **Timeout**: `30` seconds
- **Interval**: `60` seconds

**Advanced** (optional, leave defaults):
- **HTTP Method**: GET
- **Verify SSL**: Yes

4. Click **"Save"**

**Expected result**:
- Check created successfully
- Redirected to check detail page
- First result appears within 60 seconds

### Option B: Via API (Advanced)

```bash
# Get API token (web UI → Settings → API Tokens → Create Token)
export API_TOKEN="your_token_here"

# Create check via API. The check fields are FLAT (no nested "config").
# agent_id is the agent's UUID — copy it from the Agents page.
curl -X POST http://localhost:9000/api/v1/agents/<agent-uuid>/checks \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "GitHub API Health",
    "check_type": "http",
    "target": "https://api.github.com/zen",
    "interval_seconds": 60,
    "timeout_seconds": 30,
    "http_method": "GET",
    "expected_status": 200,
    "verify_ssl": true
  }'
```

---

## Step 6: View Check Results

After creating the check, wait 60 seconds for the first result.

### Dashboard View

1. Navigate to **Dashboard** (sidebar → Dashboard)
2. You should see:
   - **Summary stats**: 1 total check, 1 healthy, 0 down
   - **Check row**: GitHub API Health with 🟢 green status indicator
   - **Latest result**: "200 OK" with latency (e.g., 145ms)

### Check Detail Panel

1. Click on the **GitHub API Health** check row
2. Side panel opens with:
   - **Current status**: Success, 200ms latency
   - **30-minute status bar**: Visual timeline showing recent health
   - **Statistics**: Current response time, average, uptime %
   - **Performance chart**: Latency over last 4 hours
   - **Recent events**: Log of status changes

**What to look for**:
- 🟢 Green status indicator = check passing
- 🔴 Red status indicator = check failing
- Latency chart shows response times trending over time
- Uptime % should be near 100% (GitHub API is very reliable)

---

## Step 7: Create More Checks (Examples)

### Example 1: Ping Check (ICMP)

**Use case**: Monitor server reachability (network-level health)

```
Check Type: ping
Target: 8.8.8.8 (Google DNS)
Interval: 60 seconds
Timeout: 10 seconds
```

**Expected result**: <50ms latency for most locations

### Example 2: JSON API Check

**Use case**: Validate API response contains expected data

```
Check Type: json
Target: https://api.github.com/repos/luxardolabs/luxswirl
Interval: 300 seconds (5 minutes)
JSONata Query: name
Expected Value: luxswirl
```

**What this does**: Fetches GitHub repo info, extracts `name` field, validates it equals "luxswirl"

### Example 3: TCP Port Check

**Use case**: Monitor database port availability

```
Check Type: tcp
Target: database.example.com:5432 (PostgreSQL)
Interval: 60 seconds
Timeout: 5 seconds
```

**Expected result**: Port open = success, port closed = failure

### Example 4: DNS Check

**Use case**: Monitor DNS resolution and propagation

```
Check Type: dns
Target: example.com
Record Type: A
Expected Value: (optional — an IP the domain should resolve to)
Interval: 300 seconds
```

**What this does**: Queries DNS for A record, validates IP address matches expected value

---

## Step 8: Set Up Notifications (Optional)

Get alerted when checks fail.

### Email Notifications

1. Navigate to **Settings → Notifications**
2. Click **"Add Notification Provider"**
3. Select **"Email (SMTP)"**
4. Configure SMTP settings:

```
Provider Name: Gmail SMTP
SMTP Host: smtp.gmail.com
SMTP Port: 587
Username: your-email@gmail.com
Password: your-app-password (not your Gmail password)
From Address: alerts@example.com
To Address: ops-team@example.com
Use TLS: Yes
```

5. Click **"Test Connection"** (sends test email)
6. Click **"Save"**

**Google/Gmail users**: Use App Passwords (https://myaccount.google.com/apppasswords), not your regular password.

### Webhook Notifications

**Use case**: Send alerts to Slack, Discord, or custom endpoint

```
Provider Name: Slack Webhook
Webhook URL: https://hooks.slack.com/services/YOUR/WEBHOOK/URL
Method: POST
Headers: Content-Type: application/json
Body Template: (use default JSON payload)
```

---

## Step 9: Create a Public Status Page (Optional)

Share service health with customers.

1. Navigate to **Status Pages**
2. Click **"Create Status Page"**
3. Configure:

```
Page Name: Public Services Status
URL Slug: public
Description: Real-time status of our production services
```

4. **Add checks to display**:
   - Select "GitHub API Health" check
   - (Add more checks as you create them)

5. **Customize branding** (optional):
   - Upload logo
   - Set brand color (hex code)

6. Click **"Save"**

**Access status page**:
```
http://localhost:9000/status/public
```

**Share with users**: Add link to your website footer or help center

**Production tip**: Use reverse proxy (nginx, Cloudflare) to serve status page at custom domain (e.g., `status.example.com`)

---

## Step 10: Add More Agents (Optional)

Deploy agents in different regions or networks.

### Why Multiple Agents?

- **Geographic distribution**: Monitor from US, EU, Asia simultaneously
- **Network isolation**: Agent inside private network monitors internal services
- **Load distribution**: Spread 1,000 checks across 10 agents (100 each)

### Deploy Additional Agent

1. **Create a registration key**:
   - Navigate to **Settings → Registration Keys**
   - Click **Create Key** and copy it (used as `LUXSWIRL_AUTH_KEY`)

2. **Deploy agent on another server**:

```bash
# On remote server. Note: LUXSWIRL_SERVER_URL is the FULL reports endpoint,
# ending in /api/v1/reports.
docker run -d \
  --name luxswirl-agent-us-east \
  --restart unless-stopped \
  -e LUXSWIRL_AGENT_ID=us-east-agent \
  -e LUXSWIRL_SERVER_URL=https://server.example.com:9000/api/v1/reports \
  -e LUXSWIRL_AUTH_KEY=your_registration_key_here \
  -v luxswirl_agent_data:/app/data \
  ghcr.io/luxardolabs/luxswirl-agent:latest
```

3. **Verify connection**:
   - Web UI → Agents
   - `us-east-agent` (its `LUXSWIRL_AGENT_ID`) should show as 🟢 Online

4. **Assign checks**:
   - Create checks and select `us-east-agent` from agent dropdown
   - Or move existing checks via edit form

---

## Common Next Steps

After completing the quick start, explore these features:

### 1. Database Health Monitoring

- Navigate to **Settings → Database Health**
- Enable compression (80-90% storage savings)
- Set retention policy (auto-delete old data after 90 days)
- Monitor growth trends

### 2. Import/Export Configuration

- **Export**: Agents → [Agent] → Import/Export → Download JSON
- **Edit**: Modify JSON in text editor (bulk changes)
- **Import**: Upload JSON to create/update checks
- **Use case**: Backup, migration, templates, version control

### 3. User Management

- Navigate to **Settings → Users**
- Create additional users with limited permissions
- Assign roles: Admin (full access), Editor (manage checks and agents), or Viewer (read-only)

### 4. Alert Rules

- Navigate to **Settings → Alerts**
- Create alert rules (when to send notifications)
- Configure de-duplication (avoid alert spam)
- Set up snooze controls (maintenance windows)

### 5. Advanced Checks

- **MySQL/PostgreSQL**: Monitor database query performance
- **Synthetic checks**: Browser automation with Playwright (admin only)

### 6. Background Jobs

Jobs are one-time tasks dispatched to an agent that return structured results — distinct from checks, which run continuously on an interval. Two ship today:

- **Network Scan**: scan a subnet (CIDR, up to /16) to discover active hosts and their open ports — a quick way to find new monitoring targets.
- **Network Discovery**: enumerate an agent's network interfaces and local network details.

Run them from the **Jobs** page in the sidebar. See the [Jobs guide](../user-guide/jobs.md) for parameters, the status lifecycle, and result formats.

---

## Troubleshooting

### Agent Won't Connect

**Symptom**: Agent shows as offline in web UI

**Check**:
1. Verify server is running: `curl http://localhost:9000/health`
2. Check agent logs: `docker compose logs luxswirl_agent`
3. Look for authentication errors (usually a missing or invalid `LUXSWIRL_AUTH_KEY`) or network issues

**Fix**:
```bash
# Most failures are a bad/expired registration key. Create a fresh one in
# Settings → Registration Keys, update LUXSWIRL_AUTH_KEY (e.g. in .env), then:
docker compose up -d luxswirl_agent
```

### Check Always Failing

**Symptom**: Check shows 🔴 red status, error message

**Common causes**:
1. **Timeout too short**: Increase timeout (30s → 60s)
2. **Target unreachable**: Verify target URL/IP is correct
3. **SSL certificate issues**: Disable SSL verification (testing only)
4. **Firewall blocking**: Ensure agent can reach target

**Debug**:
```bash
# Test from agent container
docker exec luxswirl_agent curl -v https://api.github.com/zen

# Check agent logs
docker compose logs luxswirl_agent | grep "GitHub API"
```

### Database Growing Too Fast

**Symptom**: Disk space alerts, database >100 GB

**Fix**:
1. Enable compression: Settings → Database Health → Compress after 7 days
2. Set retention: Settings → Database Health → Retention period 90 days
3. Reduce check frequency: Edit checks, increase interval (60s → 120s)

See [FAQ.md](../user-guide/faq.md#database-is-growing-too-fast-how-do-i-reduce-storage) for detailed troubleshooting

### Web UI Slow

**Symptom**: Dashboard takes >5 seconds to load

**Fix**:
1. Enable database compression (faster queries)
2. Reduce time range (4h instead of 7d)
3. Increase database RAM (better caching)

See [Database Health guide](../user-guide/database-health.md) for optimization tips

---

## Production Deployment

This quick start uses Docker Compose on a single machine. For production deployments, see:

- **[Installation](../deployment/installation.md)** - Step-by-step production setup
- **[DEPLOYMENT.md](../guides/deployment.md)** - Cloud provider guides (AWS, GCP, Azure)
- **[SECURITY.md](../../SECURITY.md)** - Security hardening checklist

**Key differences** (quick start vs production):
- **HTTPS**: Use reverse proxy (nginx, Traefik) with Let's Encrypt SSL
- **Authentication**: Strong passwords, API key rotation, 2FA (not currently supported)
- **Database**: Separate database server with backups
- **Monitoring**: Monitor LuxSwirl itself (Prometheus metrics)
- **High availability**: Load balancer + multiple servers

---

## Next Steps

**Documentation**:
- [User Guide](../user-guide/) - Complete feature documentation
- [FAQ](../user-guide/faq.md) - Frequently asked questions
- API documentation is auto-generated and available at `/docs` on a running server (Swagger UI)

**Community**:
- GitHub Issues: https://github.com/luxardolabs/luxswirl/issues
- GitHub Discussions: https://github.com/luxardolabs/luxswirl/discussions

**Contributing**:
- [CONTRIBUTING.md](../../CONTRIBUTING.md) - How to contribute

---

## Summary

**What you accomplished**:
- ✅ Deployed LuxSwirl with Docker Compose
- ✅ Created first HTTP check (GitHub API)
- ✅ Viewed results in dashboard
- ✅ Explored check detail panel
- ✅ (Optional) Set up email notifications
- ✅ (Optional) Created public status page
- ✅ (Optional) Deployed additional agent


**You now have** a working deployment: the server and dashboard, an agent reporting results, and your first check running. From here, add more checks, set up notifications and alerts, and deploy additional agents where you need them.

---

