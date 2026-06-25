# Known Limitations



---

## Critical Limitations

### 1. Server is a Single Point of Failure ⚠️

**Reality**:
- ❌ **NO high availability**
- ❌ **NO redundancy**
- ❌ **NO automatic failover**
- ❌ **If server goes down, NO alerts are sent**

**What happens when server fails**:
1. Agents continue executing checks
2. Agents buffer results locally (`reports/` directory)
3. **No alerts fire** (server is offline)
4. When server recovers, agents replay buffered results
5. Historical data is recovered, but **downtime alerts were missed**

**Impact**:
- **Missed alerts**: If your API goes down while server is down, you won't be alerted
- **Monitoring blind spot**: No visibility during server outage
- **Not suitable for critical alerting** without external monitoring of server itself

**Workaround**:
```
Monitor LuxSwirl itself with external tool:
- UptimeRobot monitoring https://server.example.com/health
- PagerDuty synthetic monitoring
- Separate Uptime Kuma instance
```

**Alternative approach** (under consideration):
- Agent-side emergency alerting: Agents could detect server unavailability and send alerts directly via configured notification providers
- See GitHub issue SWIRL-58 for discussion

---

### 2. No Agent Redundancy

**Reality**:
- ❌ Agents do NOT failover to backup servers
- ❌ If agent goes down, its checks stop running
- ❌ No automatic agent-level redundancy

**What happens when agent fails**:
1. Agent stops executing checks
2. Server detects agent offline (after heartbeat timeout)
3. Dashboard shows agent as offline
4. **Checks assigned to that agent do NOT run**

**Impact**:
- If agent monitoring critical service goes down, service is unmonitored
- No automatic check redistribution to other agents

**Workaround**:
```
Duplicate critical checks across multiple agents:
- Agent 1: us-east-agent → checks critical-api
- Agent 2: us-west-agent → ALSO checks critical-api
- If one agent fails, other still monitors
```

---

### 3. Local Buffering ≠ Real-Time Alerting

**Agents buffer results when server unavailable**:
- ✅ Results stored locally
- ✅ Replayed when server recovers
- ✅ No data loss (historical results preserved)

**BUT**:
- ❌ **Alerts only fire when results reach server**
- ❌ Buffered results processed as historical data (timestamps in past)
- ❌ Alert logic sees old timestamps, may not trigger

**Example failure scenario**:
```
2:00 AM - Server goes down (network issue)
2:15 AM - Your API goes down (real outage)
2:16 AM - Agent detects API down, buffers result locally
          → NO ALERT SENT (server offline)
2:30 AM - Server comes back online
2:31 AM - Agent replays buffered results
          → Historical data stored
          → Alert may NOT fire (result timestamped 2:16 AM, 15 minutes ago)
3:00 AM - You wake up, check dashboard, see outage happened
          → But you were never alerted
```

**Impact**:
- **Delayed alerting** or **no alerting** for incidents during server outages
- Historical data is complete, but real-time alerting is compromised

**Recommendation**:
- Use LuxSwirl for **uptime tracking and SLA monitoring** (data completeness)
- Use external tool for **critical production alerting** (PagerDuty, OpsGenie)
- OR monitor server itself with external service

---

## Scalability Limitations

### 4. Single Server Performance Limits

**Performance testing**: Not yet conducted. Specific limits to be documented after formal testing.

**Known architectural bottlenecks**:
1. **Database write throughput**: Single server writes all results
2. **Dashboard query performance**: May degrade with very large check counts
3. **Result ingestion**: Single endpoint handling all agent reports

**Workaround**:
- Enable database compression (reduces query load)
- Use pagination (reduce dashboard load)
- Deploy separate database server (offload I/O)
- Optimize check intervals (use longer intervals where high frequency isn't needed)
- Use continuous aggregates for dashboard queries

---

### 5. Database Size Growth

**Without compression/retention**:
- 1,000 checks @ 60s interval = 1.44M results/day
- Database growth: ~5 GB/day (uncompressed)
- 90 days = 450 GB database

**With compression (enabled by default)**:
- 80-90% reduction
- Same scenario: 45-90 GB database

**With retention (enabled by default)**:
- Auto-delete after 90 days
- Database size stabilizes at ~90 days × daily growth

**Limitation**:
- Even with compression + retention, large deployments (10,000+ checks) require significant storage
- 10,000 checks @ 30s interval ≈ 350 GB compressed (90-day retention) — provision disk accordingly

---

## Feature Limitations

### 6. Limited Notification Providers

**Currently supported** (3 shipped providers):
- ✅ Email (SMTP)
- ✅ Webhooks (generic HTTP POST)
- ✅ Home Assistant

**NOT supported**:
- ❌ Slack
- ❌ Discord
- ❌ Microsoft Teams
- ❌ Telegram
- ❌ PagerDuty
- ❌ OpsGenie

**Workaround**:
- Use webhooks for Slack/Discord (manual webhook configuration)
- Use email → email-to-SMS gateways
- Use webhooks → Zapier → any service

---

### 7. No 2FA (Two-Factor Authentication)

**Current authentication**:
- ✅ Username + password (bcrypt hashed)
- ✅ Session-based (secure cookies)
- ✅ Per-IP login rate limiting (default `10/15minutes`) to slow brute-force attempts
- ✅ Per-account lockout (default: 5 failed attempts → 30-minute lock); both values configurable in Settings → Security

**NOT supported**:
- ❌ TOTP 2FA (Google Authenticator)
- ❌ SMS 2FA
- ❌ Hardware tokens (YubiKey)

**Risk**:
- Password compromise = full account access
- No second factor protection

**Workaround**:
- Strong passwords (20+ characters, password manager)
- Restrict access to trusted networks (VPN, firewall)
- Monitor audit logs for suspicious logins

---

### 8. No Multi-Tenancy

**Designed for**:
- ✅ Single organization
- ✅ Trusted users
- ✅ Self-hosted deployments

**NOT designed for**:
- ❌ Multi-tenant SaaS
- ❌ Untrusted users
- ❌ Data isolation between tenants

**Why**:
- Synthetic checks execute arbitrary code (admin only, but still risky)
- No pod-level isolation
- Shared database (no tenant-level encryption)

**Risk**:
- Admin accounts can execute arbitrary Python (synthetic checks)
- Users can see all checks (no per-user data isolation)

**Recommendation**:
- Deploy separate LuxSwirl instance per organization

---

### 9. No Mobile App

**Web UI only**:
- ✅ Responsive design (works on mobile browsers)
- ❌ No native iOS/Android app

**Limitations**:
- No push notifications
- No offline access
- No native mobile UX

---

### 10. Check Interval Granularity

**No hard 30-second floor.** The check schema accepts any positive interval up to 86400 seconds (`interval_seconds` is validated as `gt=0, le=86400`). The configurable default-interval setting (`check.default_interval`) has a minimum of 10 seconds, not 30 — and that minimum applies only to the *default*, not to what an individual check may be set to.

**Practical considerations** (not enforced limits):
- Very short intervals increase agent load, database write volume, and load on the monitored target — tune accordingly.
- Sub-second monitoring (e.g., every 100ms) is not a design goal; for ultra-low-latency needs a purpose-built tool (e.g., Prometheus) is a better fit.

**Tips**:
- Stagger checks across multiple agents if you need frequent, redundant probing.
- Use longer intervals for low-churn targets to conserve storage.

---

## Security Limitations

### 11. Synthetic Checks: Arbitrary Code Execution

**Risk**: HIGH (by design)

**Reality**:
- Synthetic checks execute arbitrary Python code
- Admin-only feature (users cannot create)
- AST validation blocks obvious attacks
- BUT AST validation is bypassable

**NOT safe for**:
- ❌ Multi-tenant environments
- ❌ Untrusted administrators
- ❌ Public-facing instances

**Recommendation**:
- Only use in self-hosted, trusted environments
- Review all synthetic check code carefully
- Treat synthetic checks as "code deployment" not "configuration"

**Possible future work:**
- Kubernetes pod isolation
- WASM runtime (sandboxed execution)

---

### 12. Credentials in Environment Variables

**Current storage**:
- Database credentials in `.env` file or Docker environment
- Visible via `ps aux` or Docker inspect
- Encrypted agent credentials (Fernet AES-128)

**Risk**:
- User with shell access can read environment variables
- Container escape can expose credentials

**Workaround**:
- Use Docker secrets (better than environment variables)
- Restrict shell access
- Rotate credentials every 90 days

---

## Operational Limitations

### 13. No Automated Backups

**Current**:
- ❌ No built-in backup automation
- ❌ No point-in-time recovery
- ❌ No automated backup verification

**Recommendation**:
- Set up manual backup cron job (`pg_dump` daily)
- Store backups off-server (S3, NAS)
- Test restore procedure regularly

**Example backup script**: See [Installation](../deployment/installation.md#database-backups)

---

### 14. Partial Audit Logging (No Full Config-Change Trail)

**What IS logged**:
- ✅ **Authentication/session audit**: auth events are emitted as structured logs (`auth.failure.*`, login success/lockout, etc.), and the `sessions` table records `ip_address` and `user_agent` per session for security audit and user visibility.
- ✅ **Notification audit**: every notification send is persisted to the `notification_logs` hypertable (provider, status, error message, `sent_at`), viewable in the UI.
- ✅ Security-sensitive actions logged (e.g., synthetic check creation).

**What is NOT (yet) logged**:
- ❌ A unified, queryable change-log for routine config edits (who deleted a check, who changed which field) — config mutations are not captured in a dedicated audit table
- ❌ No audit-trail export/report

**Impact**:
- Auth and notification activity are auditable today.
- You generally cannot answer "who changed this specific check field, and when?" from a single audit trail.
- Not a complete compliance audit trail (SOC 2, ISO 27001) on its own.

**Workaround**:
- Use git for import/export (version control = config change history)

---

## Recommended Use Cases

### ✅ Good Fit

**1. SLA Monitoring & Reporting**:
- Historical uptime tracking
- Compliance reporting (99.9% uptime)
- Performance trending
- **Reason**: Data completeness matters more than real-time alerting

**2. Development/Staging Environments**:
- Monitor dev/staging services
- Acceptable if alerts are delayed
- **Reason**: Downtime in dev is low-impact

**3. Secondary/Backup Monitoring**:
- Use LuxSwirl alongside a primary alerting tool
- LuxSwirl provides historical data, primary tool provides alerting
- **Reason**: Redundancy across tools

**4. Internal Services (Non-Critical)**:
- Monitor internal tools, dashboards, CI/CD
- Acceptable if downtime detection is delayed by minutes
- **Reason**: Internal services have lower SLA requirements

**5. Small Teams/Startups**:
- <100 services to monitor
- Willing to monitor server externally
- **Reason**: Small scale and comfortable monitoring the server externally

### ❌ Poor Fit

**1. Critical Production Alerting (Sole Tool)**:
- 24/7 uptime requirement
- Need immediate alerts (PagerDuty-level SLA)
- **Reason**: Single server SPOF

**2. Multi-Tenant SaaS**:
- Hosting monitoring for untrusted customers
- **Reason**: Security model not designed for multi-tenancy

**3. Sub-Second / High-Frequency Monitoring**:
- Need sub-second probing or very high check frequency
- **Reason**: Not a design goal; high frequency drives agent load, DB writes, and target load (no hard interval floor, but a purpose-built tool fits better)

**4. Compliance-Heavy Environments**:
- SOC 2, ISO 27001, HIPAA
- Require a complete config-change audit trail and 2FA
- **Reason**: Auth/session and notification audit logging exist, but there is no full config-change audit trail and no 2FA

---

## Mitigation Strategies

### How to Use LuxSwirl Safely in Production

**1. Monitor the Server**:
```
Use external service to monitor https://server.example.com/health:
- UptimeRobot
- Pingdom
- Another LuxSwirl instance (monitor each other)
```

**2. Redundant Monitoring**:
```
Critical checks:
- Monitor with LuxSwirl (historical data, SLA tracking)
- AND monitor with external tool (real-time alerting)
```

**3. Agent Redundancy**:
```
Deploy 2+ agents in different regions checking same endpoints:
- us-east-agent → critical-api
- us-west-agent → critical-api
If one agent fails, other continues monitoring
```

**4. Automated Backups**:
```bash
# Cron job: Daily pg_dump backup
0 2 * * * pg_dump luxswirl | gzip > /backups/luxswirl_$(date +\%Y\%m\%d).sql.gz
```

**5. External Server Monitoring**:
```
Deploy watchdog script that:
1. Checks server health every 60s
2. If server down, sends PagerDuty alert
3. Provides real-time alerting layer on top of LuxSwirl
```

---

## Using this honestly

These limitations are documented so you can decide before deploying, not after. LuxSwirl is production-capable, but plan around the single-server SPOF: add server redundancy or external monitoring of the server itself, and pair it with a dedicated alerting tool for mission-critical monitoring.

**Questions about limitations?**
- GitHub Discussions: https://github.com/luxardolabs/luxswirl/discussions

---

