# Notifications

The notification system has two parts:

- **Notification providers** — the destinations notifications are sent to (Email, Webhook, Home Assistant). Configured once and attached to alerts.
- **Notification logs** — a complete audit trail of every notification LuxSwirl has sent.

When an alert fires, notifications are sent through all of its attached providers in parallel; if one provider fails, the others still receive theirs.

**Access:** providers under **Settings → Notifications**; logs via **Notifications** in the sidebar (`/notification-logs`).

| Provider | Sends | Typical use |
|----------|-------|-------------|
| **Email (SMTP)** | Email (HTML with a plain-text fallback) | Engineering teams, on-call, stakeholders |
| **Webhook (HTTP POST)** | JSON or form POST to any URL | Slack, Discord, PagerDuty, custom systems |
| **Home Assistant** | Webhook shaped for HA automations | HA dashboards, mobile push, smart-home actions |

---

## Email (SMTP)

Sends notifications via an SMTP server.

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| **Hostname** | Yes | — | SMTP server, e.g. `smtp.gmail.com`, `smtp.office365.com`, or `localhost` |
| **Port** | Yes | 587 | 25 (none), 587 (STARTTLS), 465 (SSL/TLS) |
| **Security** | Yes | STARTTLS | `None` (no encryption — not for external SMTP), `STARTTLS` (upgrade to TLS), `SSL/TLS` (TLS from the start) |
| **From Email** | Yes | — | Sender address |
| **To Email** | Yes | — | Recipient address |
| **Username** | No | — | SMTP auth user (usually the email). Leave blank if the server needs no auth |
| **Password** | No | — | SMTP auth password. For Gmail use an **app password**, not your account password |
| **CC** / **BCC** | No | — | Additional / hidden recipients |
| **Ignore TLS Error** | No | false | Skips TLS cert validation — testing with self-signed certs only |
| **Custom Subject** | No | — | Overrides the default subject template |
| **Timeout (seconds)** | No | 30 | Range 1–300 |

Emails are sent as **HTML with a plain-text fallback**. The HTML version has a status-colored header (green for UP, red for DOWN), an organized field layout, monospace technical details, and a mobile-responsive design.

**Default subject template:**

```
[{{STATUS}}] {{NAME}} - {{HOSTNAME_OR_URL}}
```

**Default message template:**

```
Check: {{NAME}}
Type: {{TYPE}}
Target: {{HOSTNAME_OR_URL}}
Status: {{STATUS}}
Latency: {{LATENCY}}
Agent: {{AGENT}}
Timestamp: {{TIMESTAMP}}
Error: {{ERROR_MESSAGE}}
```

**Gmail example** (create an [app password](https://myaccount.google.com/apppasswords) first):

```
Hostname: smtp.gmail.com      Username: your-email@gmail.com
Port:     587                 Password: <app-specific password>
Security: STARTTLS            From Email: your-email@gmail.com
                              To Email:   recipient@example.com
```

**Office 365 example:**

```
Hostname: smtp.office365.com  Username: your-email@company.com
Port:     587                 Password: <your password>
Security: STARTTLS            From Email: your-email@company.com
                              To Email:   team@company.com
```

---

## Webhook (HTTP POST)

Sends an HTTP POST with the full check/alert context — the bridge to Slack, Discord, PagerDuty, or any custom endpoint.

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| **Post URL** | Yes | — | Must start with `http://` or `https://` |
| **Request Body Preset** | No | `json` | `json` (`application/json`) or `form` (`application/x-www-form-urlencoded`) |
| **Additional Headers** | No | — | JSON object of header key/values, e.g. `{"Authorization": "Bearer token123"}` |
| **Timeout (seconds)** | No | 10 | Range 1–60 |
| **Verify SSL** | No | true | Set false only for testing with self-signed certs |

**Payload (JSON preset):**

```json
{
  "check_name": "Production API",
  "check_type": "http",
  "target": "https://api.example.com/health",
  "agent_id": "abc-123-def",
  "agent_name": "prod-agent-01",
  "status": "error",
  "success": false,
  "previous_status": "success",
  "latency_ms": 2345.67,
  "timestamp": "2024-01-15T14:32:01Z",
  "error_message": "Connection timeout after 30 seconds",
  "error_type": "timeout",
  "http_status_code": null,
  "alert_name": "API Down",
  "alert_description": "Alerts when production API fails",
  "is_recovery": false,
  "consecutive_failures": 3
}
```

**Examples:**

```
Slack:     Post URL: https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXX
           Request Body Preset: json
PagerDuty: Post URL: https://events.pagerduty.com/v2/enqueue
           Additional Headers: {"Content-Type": "application/json"}
Discord:   Post URL: https://discord.com/api/webhooks/123456789/abcdefg
           Request Body Preset: json
```

---

## Home Assistant

Sends a webhook to Home Assistant for use in automations, dashboards, and the companion app. Same fields as Webhook, but the **Post URL** must contain `/api/webhook/` (e.g. `https://your-ha.com/api/webhook/luxswirl_monitor`).

**Payload** (nested for easy use in automations):

```json
{
  "event_type": "luxswirl_monitor_alert",
  "data": {
    "check":   { "name": "Production API", "type": "http", "target": "https://api.example.com/health" },
    "agent":   { "id": "abc-123-def", "name": "prod-agent-01" },
    "status":  { "current": "error", "previous": "success", "success": false, "is_recovery": false },
    "performance": { "latency_ms": 2345.67, "http_status_code": null },
    "error":   { "message": "Connection timeout after 30 seconds", "type": "timeout" },
    "alert":   { "name": "API Down", "description": "Alerts when production API fails" },
    "metadata": { "timestamp": "2024-01-15T14:32:01Z", "consecutive_failures": 3 }
  }
}
```

**Automation example:**

```yaml
- alias: "LuxSwirl Monitor Alert"
  trigger:
    - platform: webhook
      webhook_id: "luxswirl_monitor"        # → Post URL: https://your-ha.com/api/webhook/luxswirl_monitor
  condition:
    - condition: template
      value_template: "{{ trigger.json.data.status.current == 'error' }}"
  action:
    - service: notify.mobile_app
      data:
        title: "{{ trigger.json.data.alert.name }}"
        message: "{{ trigger.json.data.check.name }} is down: {{ trigger.json.data.error.message }}"
    - service: persistent_notification.create
      data:
        title: "LuxSwirl Alert"
        message: |
          Check: {{ trigger.json.data.check.name }}
          Status: {{ trigger.json.data.status.current }}
          Error: {{ trigger.json.data.error.message }}
```

---

## Managing providers

**Create:** Settings → Notifications → **Add Notification Provider** → pick a type → fill in the fields, plus:

- **Friendly Name** — display name used throughout the UI (e.g. "On-Call Email", "Slack #alerts").
- **Is Default Enabled** (default false) — when true, this provider is pre-checked on new alerts.
- **Is Enabled** (default true) — a disabled provider sends nothing.

Then **Create Provider** and **Test** it to verify the configuration.

**Edit:** changes apply to every alert using the provider.

**Delete:** removes the provider configuration and its alert mappings. Notification logs and the alerts themselves are **not** deleted (the alerts just lose this provider).

**Enable/disable:** the toggle on each provider card suppresses or resumes sending without losing configuration — handy during maintenance or while testing.

---

## Templates and variables

Subjects and messages support `{{VARIABLE}}` substitution:

| Variable | Value | | Variable | Value |
|----------|-------|---|----------|-------|
| `{{NAME}}` | Check display name | | `{{AGENT}}` | Agent name/ID |
| `{{HOSTNAME_OR_URL}}` | Check target | | `{{TIMESTAMP}}` | Execution time |
| `{{STATUS}}` | "UP" / "DOWN" | | `{{TYPE}}` | Check type |
| `{{LATENCY}}` | Response time | | `{{ALERT}}` | Alert name |
| `{{ERROR_MESSAGE}}` | Error details | | `{{HTTP_STATUS}}` | HTTP status code |

**Substitution rules:** variables are **case-sensitive** (`{{NAME}}`, not `{{name}}`); a null/missing value renders as `N/A` / `No error` / `Unknown` as appropriate; everything is coerced to string; values are **not** HTML-escaped.

**Precedence (most specific wins):** alert custom template → provider custom template → built-in default.

```
Default:  [{{STATUS}}] {{NAME}} - {{HOSTNAME_OR_URL}}
Provider: Alert: {{ALERT}} - {{NAME}} is {{STATUS}}
Alert:    [CRITICAL] {{NAME}} DOWN
Result for that alert → [CRITICAL] Production API DOWN
```

**Subject examples:**

```
{{ALERT}}: {{NAME}} is {{STATUS}}        → Critical Services Down: Production API is DOWN
{{NAME}} {{STATUS}} ({{LATENCY}})         → Production API DOWN (2345.67ms)
[{{TIMESTAMP}}] {{NAME}} {{STATUS}}       → [2024-01-15T14:32:01Z] Production API DOWN
```

**Message examples:**

```
# Detailed
Service: {{NAME}}
URL: {{HOSTNAME_OR_URL}}
Current Status: {{STATUS}}
Response Time: {{LATENCY}}
Monitored By: {{AGENT}}
Check Time: {{TIMESTAMP}}
Error Details: {{ERROR_MESSAGE}}

# Concise
{{NAME}} is {{STATUS}}
{{ERROR_MESSAGE}}
Time: {{TIMESTAMP}}

# With a runbook link
Alert: {{ALERT}}
Check: {{NAME}} ({{TYPE}})
Status: {{STATUS}}
Latency: {{LATENCY}}
Error: {{ERROR_MESSAGE}}

Runbook: https://wiki.example.com/runbooks/{{NAME}}
```

---

## Notification logs

The logs page summarizes **Total / Sent Successfully / Failed** and lists every notification attempt. Each row shows timestamp, alert, provider, check type, check name, target, check result (Up/Down), latency, notification status, any error, and actions (view check, snooze/un-snooze). Filter by **Status**, **Alert**, **Provider**, and **Per Page**; filters persist in the URL for sharing.

| Status | Meaning |
|--------|---------|
| **Sent** | Delivered — provider returned success (2xx / SMTP OK) |
| **Failed** | Delivery failed (4xx/5xx or SMTP error); the Error column has the message. Common causes: bad config, network, auth failure, timeout, invalid recipient |
| **Rate Limited** | Suppressed by the provider's rate limit (see below) |
| **Deduplicated** | Suppressed by alert de-duplication (same status within the resend window) |
| **Suppressed** | Not delivered for an audited reason in the Error column — either **"Notification provider is disabled"** (re-enable it), or **"Suppressed: parent check '\<name\>' is down"** (cascade suppression — resumes automatically when the parent recovers; see [Checks → Depends On](checks.md)) |

**Snooze** pauses notifications for one **alert–check relationship**. From a log row, click **Snooze** (adds 15 minutes; click again to extend; the button shows remaining time like `1h 30m`); **un-snooze** to resume immediately. Snooze is scoped to that pair only — the same alert on other checks, and other alerts on the same check, keep notifying. Monitoring and data collection continue while snoozed.

```
Alert "API Down" on check "Production API", snoozed 15m:
  • "API Down" for "Production API"  → snoozed
  • "API Down" for other checks      → still notify
  • other alerts for "Production API"→ still notify
```

The **view check** action opens that check's detail panel (30-minute status bar, latency chart, stats) without leaving the page.

---

## Rate limiting

Each provider can cap its own volume to prevent spam:

| Field | Default | Notes |
|-------|---------|-------|
| **Rate Limit Count** | null | Max notifications per window. `null` = unlimited |
| **Rate Limit Window (minutes)** | 60 | The counting window |

When a notification is about to send, LuxSwirl counts how many were sent via that provider within the window; if the count has reached the limit, it suppresses the send and logs it as `rate_limited` until the window rolls forward:

```
sent_in_window = COUNT(logs WHERE provider = X AND status = 'sent'
                       AND sent_at >= NOW() - window_minutes)
if sent_in_window >= rate_limit_count:  suppress (log "rate_limited")
else:                                   send
```

Rate limiting is **per provider**, so different destinations can have different limits. Rough starting points: email 10–20/hour (respect Gmail/Office 365 limits); Slack ~1/second; Home Assistant usually needs none. If important notifications get suppressed, raise the count or window, or split critical alerts onto a separate higher-limit provider.

---

## Common workflows

**Email for all alerts.** Settings → Notifications → Add Provider → **Email**. Configure SMTP (see the Gmail example above), set a Friendly Name, toggle **Is Default Enabled**, **Create**, then **Test**. New alerts now pre-select it.

**Send to Slack.** Create an [incoming webhook](https://api.slack.com/messaging/webhooks) in Slack and copy its URL. Add a **Webhook** provider with that Post URL and `json` preset. Attach it to the alerts you want in Slack. (For custom formatting, shape the message in Slack's Block Kit on their side.)

**Integrate Home Assistant.** In HA, create an automation with a **Webhook** trigger and note its `webhook_id`. In LuxSwirl, add a **Home Assistant** provider with Post URL `https://your-ha.com/api/webhook/<webhook_id>`. Attach it to alerts. Read the nested `data` payload in the automation (see the example above).

**Tiered alerting.** Create providers per channel (e.g. Email = team list, Slack = #alerts, PagerDuty = webhook), then attach different combinations per alert:

```
Staging API Down     → Email only,                 5 failures,  resend 120m
Production API Down  → Email + Slack + PagerDuty,   1 failure,   resend 15m, max 5
SSL Expiring         → Email only,                  [30,14,7]d,  resend 1440m
```

Critical production issues escalate to multiple channels immediately; lower-priority alerts stay on email.

---

## Troubleshooting

| Symptom | Causes and fixes |
|---------|------------------|
| **Alert fires, no notification** | Provider disabled (re-enable); provider not attached to the alert (edit alert); alert disabled; relationship snoozed (un-snooze); deduplicated (adjust resend interval); rate-limited (raise limit). Check the log status to tell which |
| **Email "Failed" / connection error** | Wrong username/password (Gmail needs an app password); wrong host/port; try a different Security option; firewall blocking SMTP from the server; TLS cert error → temporarily enable **Ignore TLS Error** to confirm, then fix the cert |
| **Webhook 4xx/5xx** | `400` wrong body preset (try json vs form); `401` missing/incorrect auth header (`{"Authorization": "Bearer …"}`); `404` wrong URL (test with `curl`); `500` destination error (check its logs); timeout → raise the timeout |
| **Snooze not stopping notifications** | A different alert on the same check is still active (snooze each); the 15-minute snooze expired (extend); a different check with a similar name is firing (verify the check ID in the log) |
| **Rate limit too aggressive** | Raise `rate_limit_count` or the window; or create a separate high-limit provider for critical alerts only; or set the count to `null` (unlimited) |
| **HTML email shows tags** | The client doesn't render HTML — the plain-text fallback is also sent; use a modern client or whitelist the sender |

---

## API

All endpoints require `Authorization: Bearer {token}`; full schemas are at `/docs`.

**List providers** — `GET /api/v1/notification-providers`

```json
{
  "items": [
    {
      "id": "uuid",
      "provider_type": "email",
      "friendly_name": "Team Email",
      "config": { "hostname": "smtp.gmail.com", "port": 587,
                  "from_email": "alerts@example.com", "to_email": "team@example.com" },
      "is_enabled": true,
      "is_default_enabled": true,
      "rate_limit_count": 10,
      "rate_limit_window_minutes": 60,
      "created_at": "2024-01-15T10:00:00Z"
    }
  ],
  "total": 3
}
```

**Create provider** — `POST /api/v1/notification-providers`

```json
{
  "provider_type": "webhook",
  "friendly_name": "Slack #alerts",
  "config": { "post_url": "https://hooks.slack.com/services/YOUR/WEBHOOK",
              "request_body_preset": "json", "timeout": 10 },
  "is_enabled": true,
  "is_default_enabled": false,
  "rate_limit_count": 60,
  "rate_limit_window_minutes": 60
}
```

**Test provider** — `POST /api/v1/notification-providers/{id}/test` → `{ "success": true, "message": "Test notification sent successfully" }`

**List logs** — `GET /api/v1/notification-logs` (query: `skip`, `limit`, `status`, `alert_id`, `provider_id`)

```json
{
  "items": [
    {
      "id": "uuid", "alert_id": "uuid", "notification_provider_id": "uuid",
      "check_result_id": "uuid", "check_result_timestamp": "2024-01-15T14:32:01Z",
      "status": "sent", "error_message": null, "sent_at": "2024-01-15T14:32:05Z",
      "is_resend": false, "resend_count": 0
    }
  ],
  "total": 1250, "skip": 0, "limit": 50
}
```

---

## Security

**Credential storage.** Provider credentials — SMTP passwords and webhook auth tokens — are stored in the provider's `config` (JSONB) and are **not currently encrypted at rest** (unlike agent credentials and check database credentials, which are). Keep database access controlled, use app-specific passwords and short-lived webhook tokens, and rotate them regularly.

**SMTP.** Always use STARTTLS or SSL/TLS for external servers — never `None` over the internet — and authenticate with a username/password (an app password for Gmail). Only disable TLS verification for testing.

**Webhooks.** Always use HTTPS URLs and carry auth tokens in headers; verify the destination is trusted before saving.

**Notification content.** Notifications include the check target and error text, so avoid leaking internal hostnames to external channels — use generic check names for public destinations and sanitize error messages that could contain sensitive data.

---

## Related

- [Alerts](alerts.md) — the alerts that fire these notifications
- [Checks](checks.md) — the **Depends On** field behind cascade suppression
- [Settings](settings.md) — where providers live
