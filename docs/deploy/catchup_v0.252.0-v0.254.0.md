**Deploy: v0.252.0 → v0.254.0 (OML today) — one build, one migrate; FarmOps 0.44.0 (build 39)**

For a server already on v0.252.0. Same image and commands as `catchup_v0.231.2-v0.254.0.md` and
`~/Desktop/deploy-catchup-latest.txt`; only the expectations differ.

- **Server**: erpnext_mcp main **`dfedb8a`** (`__version__ = "0.254.0"`).
- **App**: fafo_ios main **`79f952d`** — FarmOps **0.44.0 (build 39)**, scheme **FarmOps**.

**What is new since v0.252.0**

| Release | What it adds |
|---|---|
| v0.253.0 | Rain risk from forecast chance AND amount, each rule's own threshold (0.05 in default); Holds clear within the hour when the forecast dries; pruning defaults 7 days ahead / dry 48 h; a daily "did it actually rain?" check with an alert rule (OFF) and `get_forecast_verification`. |
| v0.254.0 | Refusals reach a Spanish phone saying why, in tú; all phone Spanish in tú. Fix: receipt / signature / task-evidence checks with nothing extracted are filed, not refused. |
| app 0.43.0 | Spanish sweep: 133 longer explanations read in Spanish. |
| app 0.44.0 | A refusal from the farm shows the server's Spanish on a Spanish phone. |

**1. Build (Tim)**

Push erpnext_mcp main, build the fafo-erpnext image with `ERPNEXT_MCP_VERSION=0.254.0`. Push fafo_ios main and archive
FarmOps 0.44.0 (build 39).

**2. Pull, restart, ONE migrate** (commands in `~/Desktop/deploy-catchup-latest.txt`, section 3 for OML)

Expect from the migrate: "rain archive check rule seeded OFF: weather_check_pruning_rain." and `0.254.0`.

**3. Checks**

1. `get_server_status`: 0.254.0; the asset map's build stamp 0.254.0.
2. Desk → Compliance Rule: `weather_check_pruning_rain` present and disabled; `go_hold_pruning_canker` (seeded by
   v0.240.0 on this site) keeps its original tree — compare it with the new defaults in `v0.253.0_dry_day.md` before
   enabling it.
3. Phone 0.44.0 in Spanish: claiming a task somebody else is doing reads "<nombre> ya está haciendo FT-…".
4. A receipt photo the phone cannot read is filed (Needs Review), not refused with "extracted_fields is required".
5. Afterwards: resolve AFB-2026-00014 (weather-based tasks and holding periods) — it is v0.239.0–v0.253.0.

**4. Rollback**

The v0.252.0 image; the new rows and log entries are ignored by it.
