# Alerts

Alerts define when and how notifications are triggered. When a check fails, exceeds a threshold, has an SSL certificate expiring, or stops reporting, or when an agent goes offline, alerts evaluate the condition and send notifications through configured providers.

Alerts provide trigger conditions (status changes, latency thresholds, SSL-expiry warnings, missing data, offline agents), intelligent de-duplication, recovery notifications, global or per-check scope, custom message templates, and snooze.

**Access:** **Alerts** in the sidebar, or `/alerts`. Each alert shows as a card with a status badge, trigger-type badge, scope indicator, provider count, and toggle/edit/delete actions.

______________________________________________________________________

## Trigger types

A trigger type is **immutable** after creation — to use a different one, create a new alert. ("Repeated Failure" is a legacy alias for Status Change, functionally identical.)

| Trigger                    | Fires when                                 | Key configuration                                        |
| -------------------------- | ------------------------------------------ | -------------------------------------------------------- |
| **Status Change**          | A check fails N times in a row             | `consecutive_failures` (1–100, default 3)                |
| **Latency Threshold**      | A result's latency meets the comparison    | `operator` (`>`, `>=`, `<`, `<=`, `==`) + threshold (ms) |
| **SSL Certificate Expiry** | An HTTPS cert falls within a day-threshold | One or more of 7, 14, 21, 30, 60, 90 days                |
| **No Data**                | A check stops reporting                    | `missed_intervals` (1–100, default 3)                    |
| **Agent Offline**          | An agent stops sending heartbeats          | `missed_intervals` (1–100, default 3)                    |

### Status Change (check down)

Triggers when a check fails (goes down).

- **Consecutive Failures** (1–100, default 3): how many failures in a row before triggering. Setting 1 fires on the first failure; 3 requires three in a row.
- **Behavior:** counts consecutive failures only and resets the counter on any success — so fail → succeed → fail restarts the count. A recovery notification is sent when the check comes back up (if enabled).

```
Alert: "Production API Down"   Trigger: Status Change   Consecutive Failures: 3
→ fires after 3 consecutive failures of the Production API check
```

### Latency Threshold

Triggers when response time crosses a millisecond threshold.

- **Operator** (default `>`): `>`, `>=`, `<`, `<=`, `==`.
- **Threshold (ms)**: minimum 1 (1000 = 1 second).
- **Behavior:** evaluates every result and fires on each one that meets the comparison (subject to de-duplication); a recovery notification fires when latency returns to the acceptable side.

```
Alert: "API Slow Response"        Operator: >   Threshold: 1000ms
→ fires when the API responds slower than 1 second

Alert: "Query Suspiciously Fast"  Operator: <   Threshold: 10ms
→ fires when a query completes in under 10ms (possible caching bug or wrong query)
```

### SSL Certificate Expiry

Triggers when an SSL certificate expires within a selected number of days. Applies only to HTTPS checks that report certificate data.

- **Day thresholds** (checkboxes, multi-select): 7, 14, 21, 30, 60, 90. Fires when the cert is within ANY selected threshold.
- **Escalation bands:** each selected threshold is a band; a new notification fires the moment the cert crosses into a *tighter* band, regardless of `resend_interval`. Within a band, `resend_interval` controls re-notification. One recovery notification fires when the cert is renewed past all thresholds (`notify_on_recovery`, default true).
- **Unreadable expiry:** when a certificate is reported but its expiry date cannot be read, the alert fires once (state `ssl:unknown`) saying so and quoting the date it got, since it cannot watch that certificate. A recovery fires when the expiry can be read again and is outside every band.
- **What it says:** every SSL notification carries a detail line ("SSL certificate expires in 5 days", "expired 3 days ago", "could not be read"), shown in the email, sent as `detail` to webhooks and Home Assistant, and available to templates as `{{DETAIL}}`. Only the renewal is a recovery; a warning while the HTTPS check itself succeeds is not.

With `thresholds: [7, 14, 30]` and no resend interval, a cert aging from 60 days to expiry produces:

| Days remaining | Band                     | Notification                 |
| -------------- | ------------------------ | ---------------------------- |
| 60             | safe (`ssl:ok`)          | —                            |
| 30             | crossed into 30-day band | **fires** (state transition) |
| 25             | still in 30-day band     | skip (same state)            |
| 14             | crossed into 14-day band | **fires** (tighter band)     |
| 10             | still in 14-day band     | skip                         |
| 7              | crossed into 7-day band  | **fires** (critical)         |
| 3              | still in 7-day band      | skip                         |
| renewed to 90  | back to safe             | **fires** (recovery)         |
| unreadable     | `ssl:unknown`            | **fires** once               |

If you set `resend_interval_minutes: 1440` (one day), the operator also gets a reminder every 24 hours within the current band. **Best practice:** select multiple thresholds (30, 14, 7) for escalating signals; add a resend interval only if you want daily/weekly nags between escalations.

```
Alert: "SSL Certificate Expiring Soon"   Trigger: SSL Certificate Expiry
Thresholds: [7, 14, 30] days   Resend Interval: 1440 minutes
→ fires at 30 days (first warning), 14 days (escalation), 7 days (critical),
  with a daily reminder in the current band, and one recovery notice on renewal
```

### No Data (check stopped reporting)

Triggers when a check that should be running has reported nothing for a while. The other triggers evaluate each incoming result, so none of them can notice that results stopped arriving. This one is evaluated by the server every minute (the **Evaluate Silence Alerts** scheduler job).

- **Missed Intervals** (1–100, default 3): a check is silent once its newest result is older than this many of its intervals plus one minute's grace for the agent's report batching. A 60-second check with the default is silent after 4 minutes; a check that has never reported counts from when it was created.
- **Which checks:** only a check that is enabled and that an active agent is to run. A check whose agents are all paused, disabled, archived, pending or rejected is not expected to report and never fires. A replicate check is silent only when no agent reports it. A distribute check that moved to another agent because its agent went offline or shut down counts from the new agent's first heartbeat after the move (when it learns of the check), so it has a full threshold to report it.
- **Behavior:** one notification per silence, then the resend settings apply; a recovery notification fires when results arrive again (if enabled). Snoozing the alert for a check silences it like any other trigger.
- **Agent offline:** when every agent that runs the check is offline, the silence belongs to the agent. The check's notification is recorded as **suppressed** (visible in the notification log) instead of sent, so an agent with a hundred checks produces one Agent Offline notification, not a hundred No Data ones. Once the agent is back, a check that still reports nothing notifies as its own silence. A silence already notified stays notified while its agent restarts, so its recovery still goes out.
- **Without providers:** an alert with no notification provider attached is not evaluated.
- **An alert that fails to evaluate** is skipped for that run while the others go ahead. The error is logged, and the job's run in the **Scheduler** page's Execution History counts it under `failed`.

### Agent Offline

Triggers when an active agent stops sending heartbeats. Evaluated every minute alongside No Data.

- **Missed Intervals** (1–100, default 3): an agent is offline once its newest heartbeat is older than this many of its heartbeat intervals plus one minute's grace. With the default 5-second heartbeat that is 75 seconds.
- **Which agents:** the active agents that run any of the alert's checks, or that a distribute check may run on. A global alert therefore covers every active agent that has checks, and an agent whose distribute checks moved to another while it is offline is still watched.
- **Behavior:** one notification per outage (status **OFFLINE**), the resend settings while it lasts, and a recovery notification on the next heartbeat (if enabled). The notification log lists it under the agent's name.

```
Global: "Agent Offline"  · Agent Offline, 3 missed  · Email
Global: "Check Silent"   · No Data, 3 missed        · Email
→ an agent that dies sends one OFFLINE notification; its checks' silences are suppressed
→ a check whose target hangs the agent, while the agent heartbeats, sends NO DATA
```

______________________________________________________________________

## Scope: global vs specific

Scope is **immutable** after creation.

|            | Global                                                              | Specific                                                     |
| ---------- | ------------------------------------------------------------------- | ------------------------------------------------------------ |
| Applies to | **All** checks in LuxSwirl                                          | Only the checks you select                                   |
| New checks | Assigned automatically                                              | **Not** assigned automatically                               |
| Pros       | No manual assignment; new checks covered; simple; nothing forgotten | Precise control; less noise; per-team/per-tier alerts        |
| Cons       | Can generate more notifications; less granular                      | Must update when adding checks; risk of forgetting to assign |

```
Global:   "Any Service Down"      · Status Change, 3 failures · Email, Slack
          → every check triggers it after 3 consecutive failures

Specific: "Customer Portal Down"  · Status Change, 1 failure  · PagerDuty, Email
          → fires only for customer-portal-http, customer-api-endpoint, customer-login-service
```

| Use case                                   | Recommended scope |
| ------------------------------------------ | ----------------- |
| All services must alert on failure         | Global            |
| Different teams monitor different services | Specific          |
| New checks added frequently                | Global            |
| Different SLAs per service tier            | Specific          |
| Simple, catch-all monitoring               | Global            |
| Granular, targeted alerting                | Specific          |

A **specific alert with zero checks** is valid — it won't fire until checks are assigned (via the alert form or the Checks page). Only an admin can give an alert a synthetic check: an editor's alert that names one is refused (`403`) and nothing is created. Everything else about an alert (editing, disabling, deleting, a global alert's reach) stays with editors, whatever its checks. **Common pattern:** one global baseline ("any check down after 5 failures") plus specific alerts for critical services ("production API down after 1 failure").

______________________________________________________________________

## Creating an alert

Click **New Alert** to open the creation panel.

**Basic information:** **Name** (required, 3–255 chars) and **Description** (optional, ≤500 — document intent and team ownership).

**Trigger configuration:** pick the **Trigger Type** (immutable) and its fields — Consecutive Failures (Status Change), Operator + Threshold (Latency), Day Thresholds (SSL), or Missed Intervals (No Data, Agent Offline).

**Scope:** **Global** (default, all checks) or **Specific** + a **Select Checks** list (checkboxes, format `{agent_id}:{check_display_name}`). A specific alert with no checks is allowed.

**Notification providers:** check one or more configured providers (each shown with its friendly name and type). Providers marked default-enabled are pre-checked. Notifications go to **all** selected providers in parallel; if one fails, the others still send. If none exist, a link points to Settings → Notifications.

**Notification settings:**

| Setting                       | Default | Behavior                                                                                                       |
| ----------------------------- | ------- | -------------------------------------------------------------------------------------------------------------- |
| **Resend Interval (minutes)** | blank   | Blank = notify only on status change (never resend). Set = resend every N minutes while the condition persists |
| **Max Resends**               | blank   | Blank/0 = unlimited (respecting the resend interval). Set = stop after N resends                               |
| **Notify on Recovery**        | checked | When checked, also notify when the check recovers (down → up)                                                  |

```
Resend Interval: 30m   Max Resends: 4   Notify on Recovery: true
10:00 fail    → notification #1
10:30 down    → #2 (resend 1)
11:00 down    → #3 (resend 2)
11:30 down    → #4 (resend 3)
12:00 down    → #5 (resend 4, MAX REACHED)
12:30 down    → (no notification, max reached)
13:00 recover → recovery notification
```

**Custom templates (optional):** a **Custom Subject** and **Custom Message** with variables — `{{NAME}}` (check name), `{{STATUS}}` (`success`/`error`), `{{HOSTNAME_OR_URL}}` (target), `{{LATENCY}}` (ms). Blank uses the defaults — subject `[Alert] {check_name} is {status}`, and a message including name, status, target, latency, error, and timestamp. (Providers support additional variables — see [Notifications → Templates](notifications.md#templates-and-variables).) Example subject: `[{{STATUS}}] {{NAME}} - {{HOSTNAME_OR_URL}}`.

**Enable this alert:** checked by default; a disabled alert exists but doesn't evaluate or send.

After creation, a global alert is assigned to all existing checks, a specific alert to the selected ones, and it begins evaluating on the next result.

______________________________________________________________________

## Editing and deleting

**Editable:** name, description, trigger configuration values (failures count, threshold, SSL days), notification settings, custom templates, enabled state. Changes take effect on the next check result.

**Not editable** (the edit form shows these as read-only): **trigger type** (it fundamentally changes evaluation), **scope**, **check assignments** (manage on the Checks page), and **notification providers** (manage in Settings → Notifications).

**Deleting** an alert (confirmation required) removes the alert configuration, its alert–check mappings, and its alert–provider mappings. It does **not** delete the checks, the providers, or the historical notification logs (kept for audit).

**Enable/disable** via the card toggle is instant (no confirmation): a disabled alert keeps its configuration but doesn't evaluate or send — useful during maintenance, while investigating a noisy alert, or while testing providers.

______________________________________________________________________

## Evaluation and de-duplication

Every time the server receives a check result, it evaluates all applicable alerts: all enabled global alerts, plus enabled specific alerts assigned to that check, where the alert–check mapping is enabled and not snoozed. De-duplication then decides whether to actually send.

**Rules:**

1. **Always send on a status change** (down → up or up → down).
1. **For the same status:** never resend if `resend_interval_minutes` is blank; otherwise resend after the interval elapses. `max_resends` caps the resends.

```
Scenario 1 — status change
  10:00 fail    → notification
  10:05 down    → none (same status, within window)
  10:10 recover → recovery notification

Scenario 2 — resend interval = 30m
  10:00 fail → #1   10:05 → none   10:30 → #2   11:00 → #3   11:10 recover → recovery

Scenario 3 — no resend (interval = NULL)
  10:00 fail → notify   10:05/10:30/11:00 down → none (never resends)   11:10 recover → recovery

Scenario 4 — resend = 15m, max_resends = 3
  10:00 #1   10:15 #2   10:30 #3   10:45 #4 (max)   11:00 none   11:30 recover → recovery
```

### Snooze

Snooze temporarily pauses notifications for one **alert–check relationship** — other alerts watching the same check, and the same alert on other checks, are unaffected. Monitoring and data collection continue. From the **Notifications** logs page: click **Snooze** (adds 15 minutes; the button shows remaining time like `1h 30m`); click again to extend by 15 more; **un-snooze** to resume immediately. There's no maximum (keep extending).

```
Alert "API Down" assigned to api-prod-01, api-prod-02, api-staging
  snooze "API Down" for api-prod-01 → that pair is silenced
  api-prod-02 fails → still notifies (different check)
  another alert on api-prod-01 → still notifies (different alert)
```

### Dependency suppression

If a check declares a **Depends On** parent, then while the parent's latest result is failing, the child's alerts are written to the log as `suppressed` ("Suppressed: parent check '\<name>' is down") instead of delivered — one root-cause alert replaces a storm of downstream ones. Configure it on the check (see [Checks → Depends On](checks.md)); alerts resume automatically when the parent recovers.

______________________________________________________________________

## Notification content

When an alert fires it sends:

- **Check:** name, type (ping/http/tcp/…), target, agent id and name.
- **Status:** current status (success/error), success flag, error message and type, HTTP status code (HTTP checks).
- **Performance:** latency (ms), execution timestamp.
- **Alert:** name, description, recovery flag.
- **Custom templates:** custom subject/message if configured.

Formatting is per provider (email HTML, Slack rich, webhook JSON, SMS plain text). Example email:

```
Subject: [Alert] Production API is error

Alert: Critical Services Down
Check: Production API (http)
Target: https://api.example.com/health
Agent: prod-agent-01
Status: error
Latency: 2,345 ms
Error: Connection timeout after 30 seconds
Timestamp: 2024-01-15 14:32:01 UTC
```

It **never** includes credentials, API keys, or passwords. Because targets and error text are included, avoid exposing internal hostnames in checks whose alerts go to external channels (use generic names for public-facing notifications, and sanitize error messages that could carry sensitive data).

______________________________________________________________________

## Filters

Filter the Alerts list by **Status** (All / Enabled / Disabled), **Scope** (All / Global / Specific Checks), and **Per Page** (10/25/50/100, default 50). Filters persist in the URL (`/alerts?page=1&per_page=50&is_enabled=true&is_global=false`) so filtered views are bookmarkable and shareable.

______________________________________________________________________

## Common workflows

### Set up basic alerting

1. Settings → Notifications → create at least one provider (Email, Slack, Webhook…).
1. Alerts → **New Alert**: Name "Any Service Down", Trigger Status Change, Consecutive Failures 3, Scope Global, select your provider(s), Resend Interval 60, Max Resends blank, Notify on Recovery checked → **Create**.
1. → You're notified when any check fails 3× in a row, with hourly reminders while it stays down.

### Alert on critical services only

1. Tag critical checks with `critical` (Checks page).
1. Create a Specific alert: Status Change, 1 failure, scope = the critical checks, provider = PagerDuty, Resend 15m, recovery on.
1. Create a separate Global alert for the rest: Status Change, 5 failures, provider = Email, Resend 120m, recovery on.
1. → Critical services page immediately via PagerDuty; everything else emails after 5 failures.

### SSL certificate monitoring

1. Ensure HTTPS checks exist.
1. Create an alert: SSL Certificate Expiry, thresholds 30/14/7, Scope Global, provider Email, Resend 1440m, recovery on.
1. → First warning at 30 days, daily reminders as expiry nears, stops on renewal.

### Performance-degradation monitoring

1. Identify performance-sensitive checks.
1. Create a Specific alert: Latency Threshold, Operator `>`, Threshold 1000ms, scope = customer-facing APIs, provider Slack #engineering-alerts, Resend 30m.
1. → Slack alert when those APIs respond slower than 1s, with 30-minute reminders.

### Maintenance-window silencing

- **Disable the alert** (toggle on the card) to silence it for ALL checks; re-enable after.
- **Or snooze specific checks** (Notifications page) to silence only those while keeping the alert active for everything else.

### Team-specific alerting

1. Tag checks by team (`team-platform`, `team-data`, …).
1. Create a provider per team (team email, team Slack).
1. Create a Specific alert per team scoped to its tagged checks → each team only receives its own alerts.

______________________________________________________________________

## Troubleshooting

| Symptom                                                   | Causes and fixes                                                                                                                                                                                                                                                                                                                           |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Alert not firing**                                      | Alert disabled; alert–check mapping disabled / check not assigned (specific); snoozed; `consecutive_failures` not yet reached; no providers attached; provider disabled or deleted; de-duplication within the resend window; **parent check down** (log shows `suppressed`); or the check isn't actually failing (verify on the Dashboard) |
| **Too many notifications**                                | Raise `consecutive_failures` (1→3/5); increase the resend interval (15→60/120m); set `max_resends`; disable Notify on Recovery; snooze acknowledged issues; configure **Depends On** for groups behind a shared upstream; review/narrow global alerts; delete redundant overlapping alerts                                                 |
| **Notifications delayed**                                 | Long check interval (shorten for critical); agent batches reports (`report_interval`, ~10s); `consecutive_failures` adds `N × interval` before firing; network or provider-API latency (check the log send times)                                                                                                                          |
| **Recovery notification not sending**                     | `notify_on_recovery` is off; alert evaluated before the check succeeded (wait a cycle); or the check is flapping and recovery was de-duplicated                                                                                                                                                                                            |
| **Template variables literal** (`{{NAME}}` shows as text) | Use double curly braces, exact case; confirm the template was saved                                                                                                                                                                                                                                                                        |
| **Snooze not working**                                    | Snoozed only one of several alerts on the check (snooze each); the 15-minute snooze expired (extend); a global alert is still firing; verify the check ID in the log                                                                                                                                                                       |

______________________________________________________________________

## Performance

Every check result evaluates all applicable alerts, and consecutive-failure triggers query history — so prefer specific alerts over many broad globals, avoid overly complex logic, and consolidate redundant alerts. Notifications to multiple providers are sent **in parallel**, and one slow/failed provider doesn't block the others (watch the Notification Logs for slow providers). Notification logs grow over time (with automatic retention) — see [Database Health](database-health.md).

______________________________________________________________________

## Permissions

Creating, editing, and deleting alerts require an **Editor** or **Admin** account; **Viewers** have read-only access.

______________________________________________________________________

## Related

- [Checks](checks.md) — the checks that trigger alerts, and the **Depends On** dependency field
- [Notifications](notifications.md) — managing providers, logs, templates, and snoozing
- [Settings](settings.md) — default per-page and alert settings
- [Dashboard](dashboard.md) — viewing check status and the conditions that trigger alerts
