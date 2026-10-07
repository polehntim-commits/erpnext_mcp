**Deploy: v0.252.0 → v0.261.0 (OML today) — one build, one migrate; FarmOps 0.48.0 (build 43)**

For a server already on v0.252.0. Same image and commands as `catchup_v0.231.2-v0.261.0.md` and
`~/Desktop/deploy-catchup-latest.txt`; only the expectations differ.

- **Server**: erpnext_mcp main **`2b93e73`** (`__version__ = "0.261.0"`).
- **App**: fafo_ios main **`fb26f3e`** — FarmOps **0.47.0 (build 42)**, scheme **FarmOps**.

**What is new since v0.252.0**

| Release | What it adds |
|---|---|
| v0.253.0 | Rain risk from forecast chance AND amount, each rule's own threshold (0.05 in default); Holds clear within the hour when the forecast dries; pruning defaults 7 days ahead / dry 48 h; a daily "did it actually rain?" check with an alert rule (OFF) and `get_forecast_verification`. |
| v0.254.0 | Refusals reach a Spanish phone saying why, in tú; all phone Spanish in tú. Fix: receipt / signature / task-evidence checks with nothing extracted are filed, not refused. |
| v0.255.0 | A well's logs on the well (AFB-2026-00021): Library documents cited on an asset show where its tag is scanned and open on the phone; type "Record" for the farm's own records. The pruning preset seeded by an earlier release takes the new defaults if nobody touched it. |
| v0.256.0 | `relink_expense_receipt` (off): point a filed receipt at an invoice or JE entered elsewhere, move it, or clear it with a reason. |
| v0.257.0 | `plan_mcp_system_user` (read): the roles a dedicated MCP System User needs, a dry run for a candidate account and the Desk steps — changes nothing. |
| v0.258.0 | office@ replies reviewed on the phone: tile **Replies to review** (hidden until office@ is on) — read, correct, attach, approve and send with Face ID, or discard. Pairs with app 0.46.0. |
| v0.259.0 | Form 940 and W-3 via `generate_tax_form` (940 = the FUTA summary's walk; W-3 = the W-2s totalled; `kind_of_payer: 943` for an agricultural filer). |
| v0.260.0 | **Least-privilege hotfix.** HR reads (others' I-9, discipline, everyone's pay, deductions, personnel files) need HR Manager / HR User — the Farm Owner profile carries HR Manager; accident reports, the compliance calendar, receipts (own only), housing occupants and search filters narrowed. **Check Tim holds HR Manager after the migrate.** |
| v0.261.0 | Claim a task with no signal: the first claim to reach the server holds it; a later one is kept as a second claim with the worker's time and evidence, flagged "claimed offline by two people" (rule task_claimed_offline_twice ON) for a supervisor to review. Online claims unchanged. Pairs with app 0.48.0. |
| app 0.43.0 | Spanish sweep: 133 longer explanations read in Spanish. |
| app 0.44.0 | A refusal from the farm shows the server's Spanish on a Spanish phone. |
| app 0.45.0 | Library documents on the asset screen and on a tag scan, kept on the phone. |
| app 0.46.0 | Replies to review: office@ drafts read, corrected and approved (Face ID) or discarded on the phone. |
| app 0.47.0 | Sign-out leaves nothing of the last person's on a shared phone; unsent queued work is counted in the sign-out warning. |
| app 0.48.0 | Claim a task with no signal ("Claimed on phone — will confirm when synced"); told, by name, when someone else's claim reached the farm first — your time and photos are kept. |

**1. Build (Tim)**

Push erpnext_mcp main, build the fafo-erpnext image with `ERPNEXT_MCP_VERSION=0.261.0`. Push fafo_ios main and archive
FarmOps 0.48.0 (build 43).

**2. Pull, restart, ONE migrate** (commands in `~/Desktop/deploy-catchup-latest.txt`, section 3 for OML)

Expect from the migrate: "rain archive check rule seeded OFF: weather_check_pruning_rain.", "Go / Hold presets brought to
today's defaults (untouched, still OFF): go_hold_pruning_canker." and `0.261.0`.

**3. Checks**

1. `get_server_status`: 0.261.0; the asset map's build stamp 0.261.0.
2. Desk → Compliance Rule: `weather_check_pruning_rain` present and disabled; `go_hold_pruning_canker` now reads the
   new defaults (chance_over 0.05 in, dry 48 h) — unless somebody had edited or enabled it, in which case it is as they
   left it.
3. Phone 0.45.0 in Spanish: claiming a task somebody else is doing reads "<nombre> ya está haciendo FT-…".
4. A receipt photo the phone cannot read is filed (Needs Review), not refused with "extracted_fields is required".
5. Put a well log on a well (`v0.255.0_asset_library.md` §3) and scan its tag: the log is under Documentation.
6. Afterwards: resolve AFB-2026-00014 (weather-based tasks and holding periods) — it is v0.239.0–v0.253.0.

**4. Rollback**

The v0.252.0 image; the new rows, log entries and asset citations are ignored by it.
