# Security status, switch inventory, alerts, client IP, time-boxed switches — contract

Server `erpnext_mcp` **v0.217.0**. No app change. Frozen before code. The OML security review
(2026-10-02) §7 items 1, 2, 5, 6 and 9 — the ones that need no decision from Tim to build.
**Deploying changes nothing that is not already on:** the two new tools are reads; the alert job
only reads and sends to Security Alert Recipients; the client-IP rule is inert until a proxy range is
entered; time-boxing is a Desk action somebody has to take.

## 1. `list_mcp_switches` (read)

Every `allow_<tool>` switch: `tool`, `enabled`, `default`, `tier`, `available`, `expires_at` (when
time-boxed, §5). Filters: `tier`, `enabled`, `contains`. Summary counts per tier, and
`dangerous_enabled` — the switches of the three dangerous tiers that are on.

**Tiers** (derived from the registry, never hand-kept per tool):

| tier | rule |
|---|---|
| `read` | not mutating |
| `credential` | mints or hands out a credential: `generate_api_token`, `generate_mobile_login_qr`, `create_mobile_user`, `recover_mobile_access`, `open_device_enrollment`, `issue_*` |
| `financial` | posts or moves money/ledger: `submit_*`, `post_*`, `bulk_submit_*`, `cancel_*`, `close_*`, `reopen_*`, `run_payroll*`, `submit_payroll`, `generate_nacha_file`, `generate_prenote_file`, `receive_payment`, `record_loan_payment`, `create_payment_entry`, `convey_parcel` |
| `destructive` | the registry's `destructive` hint, or `delete_*`, `destroy_*`, `revoke_*` |
| `write` | every other mutating tool |

## 2. `get_security_status` (read)

One scored checklist. Each check: `{key, status: pass|warn|fail|info, finding, fix}`; `score` = the
share of weighted checks passing; `summary` names the fails.

| key | what |
|---|---|
| `two_factor` | System Settings two-factor auth on, and its method |
| `password_policy` | password policy on, minimum score ≥ 3 |
| `login_attempts` | consecutive-failure lockout configured |
| `email_link_login` | login with email link off (it bypasses 2FA) |
| `session_expiry` | session expiry set and not over 24 h |
| `mcp_system_user` | set, and not Administrator; its roles listed |
| `mcp_switches` | dangerous-tier switches that are on (from §1) |
| `static_mcp_token` | a site token is configured (informational until OAuth ships) |
| `api_keys` | users holding a Frappe API key — names only, never a key |
| `administrator_logins` | Administrator logins in the last 30 days (Activity Log) |
| `failed_logins` | users with more than 5 failed logins in an hour, last 7 days |
| `updatable_fields` | MCP-updatable fields on doctypes holding PII or money |
| `security_alert_recipients` | set, or falling back to System Managers |
| `client_ip` | trusted proxy range set (§4); the address the current request resolved to |
| `log_retention` | Log Settings retention for Error Log, Activity Log, Access Log |
| `frappe_version` | the version, with a warning when it is in the range of the v15.118.0 Administrator-2FA lockout regression (frappe/frappe#42852) and 2FA is on for Administrator |
| `public_surface` | optional `probe_public: true`: unauthenticated GETs of `/erpnext/login`, `/erpnext/api/method/ping`, `/`, `/bankbridge` (expect 404) and a POST to one farmops route (expect 401) on the Farm Ops address |

Read-only; nothing is returned that is a secret.

## 3. Security alerts (§7 item 6)

A scheduled job (every 5 minutes) reads what Frappe already records and sends one alert per event to
**Security Alert Recipients** (v0.216.1; empty → System Managers), writes one `security:alert` row in
MCP Action Log, and calls every method in the hook `erpnext_mcp_security_alert`. It writes nothing
else. A watermark (a global default) means each event is alerted once.

| Event | Source |
|---|---|
| Administrator logged in | Activity Log: user Administrator, operation Login, status Success |
| A user failed to log in more than 5 times in an hour | Activity Log: Login, Failed — once per user per hour |
| ERPNext MCP Settings changed (which fields; values of `allow_*` switches, never secrets) | Version rows (ERPNext MCP Settings now tracks changes) |
| System Settings changed (which fields) | Version rows |
| A Frappe API key was generated for a user | Version rows on User touching `api_key` |

A test pins that no secret value (token, password, api_secret) ever appears in an alert.

## 4. Real client IP (§7 item 5)

Setting **Trusted Proxy Ranges** (`trusted_proxy_cidrs`, CIDRs, default **empty**). Empty: exactly
today's rule (rightmost `X-Forwarded-For` hop, else the socket peer). Set: walk the hops from the
right, skipping every address inside a trusted range; the first address outside is the client. Used
by the MCP allowlist, every audit row, and the sidecar's per-address limits.

**Before setting it**: the MCP allowlist (`allowed_cidrs`) then sees the real client — on OML a
tailnet address (`100.64.0.0/10`) rather than the Docker gateway — so add `100.64.0.0/10` to
`allowed_cidrs` first or the MCP refuses. `get_security_status.client_ip` shows what the current
request resolves to both ways.

## 5. Time-boxed switches (§7 item 9)

A Desk action on ERPNext MCP Settings, **Enable a tool for N minutes** (System Manager only; whitelisted
method `erpnext_mcp.api.switches.enable_for`): turns one switch on that is currently off, for 1–240
minutes, records the expiry, writes an audit row and sends an alert. A job every 5 minutes turns
expired switches off again (audit + alert). `list_mcp_switches` shows `expires_at`. A switch turned
on by hand in the form is permanent, as today, and is never turned off by this. **No MCP tool can
turn a switch on** — that stays a person's act in the Desk.

## Surfaces

MCP tools 985 → **987** (+2 read: `get_security_status`, `list_mcp_switches`). Settings:
`trusted_proxy_cidrs`, `switch_expiry` (hidden). ERPNext MCP Settings gains `track_changes`. Scheduled
jobs: `security_watch.scan` and `switch_timer.revert_expired` (every 5 minutes). Hook (for other apps):
`erpnext_mcp_security_alert`. Desk method: `erpnext_mcp.api.switches.enable_for`.

## Decisions for Tim

1. Set **Trusted Proxy Ranges** only after adding the tailnet range to `allowed_cidrs` (§4).
2. The failed-login threshold is 5 per user per hour, as the review proposed.
3. The tier lists in §1 are derived by name; a tool that should be dangerous and is not caught by its
   name is a one-line addition.
