**Deploy: catch-up v0.231.2 → v0.268.0 — one build, ONE migrate; FarmOps 0.38.7 → 0.53.0 (build 48)**

For both servers, which run v0.231.2 with FarmOps 0.38.7 (build 32). One image covers v0.231.3 through v0.268.0, and
a single `bench migrate` runs everything those releases need, in order. Every step is idempotent; everything new
ships **OFF** unless it is a read. Pay is unchanged (v0.235.0 moved the overtime rule into data with identical
output, proven on the payroll suite).

- **Server**: erpnext_mcp main **`5ab9b1b`** (`__version__ = "0.268.0"`).
- **App**: fafo_ios main **`a06adc8`** — FarmOps **0.53.0 (build 48)**, scheme **FarmOps**. It needs v0.252.0's
  routes, so deploy the server first.
- v0.232.0 was never released: it shipped as v0.244.0 (upload links).

**What each release added, in plain words**

| Release | What it adds |
|---|---|
| v0.231.3 | Polish: the inbox counts per company correctly; spray, alert and task lists load in far fewer queries. |
| v0.233.0 | Rules can hold a "condition tree" (all / any / not over named values) evaluated by one generic engine. |
| v0.234.0 | One set of read tools for every kind of configuration: list, get, compare versions, preview. |
| v0.234.1 | The matching write tools (draft, stage, publish, roll back). AI-drafted versions are published only by a person. "Work Timing" rule category. Foremen may scan business cards. |
| v0.235.0 | The overtime rule (40 h/week, 1.5×) is stored as a setting with versions and a pay preview; pay output identical. |
| v0.236.0 | Tasks get start and due dates and "blocked by"; overdue list; an overdue rule (seeded OFF). |
| v0.237.0 | Daily equipment checks: the operator checks the machine before use (three check templates; switch OFF). |
| v0.238.0 | The Reference Library: PDFs searchable to the page and citable from SOPs, templates, training and rules. |
| v0.239.0 | Rules can read each block's weather forecast (rain risk this week, frost, wind) and its latest crop stage. Off until "Block Forecasts for Work Timing". |
| v0.240.0 | Go / Hold on tasks: checked at 06:00 and at start, clears itself, Advisory by default; a supervisor can override an Enforced Hold with a reason. Presets seeded OFF. |
| v0.241.0 | Record a block's crop stage (BBCH) and see its timeline; missing, unreadable or backwards stages are flagged, not refused. |
| v0.242.0 | Suggested tasks: Go-today work, with "window closing" (Go today, Hold tomorrow) first. |
| v0.243.0 | No-work notices: drafted the evening before for the supervisor, escalated to management if unanswered, never sent automatically. Switch OFF. |
| v0.244.0 | One-time upload links for large files (PDFs, photos) onto one record; no video; files private unless an admin allows public. Master switch OFF. |
| v0.245.0 | SOP review and approval: submit, approvers by the work or position the SOP covers, approval with a signature, new versions replace old ones only when approved. |
| v0.246.0 | Course videos per training course, and how much each person watched (approximate). |
| v0.247.0 | Knowledge checks (quizzes) per course, graded on the server; a trainer signs off and the training record is filed. Switch OFF. |
| v0.248.0 | Irrigation schedule per zone; each day's sets become Irrigation tasks (switch OFF); planned vs what the valves actually ran. |
| v0.249.0 | Growing degree days per block, and a stage estimate once a stage table is written in settings (none built in). |
| v0.250.0 | The phone's doors to all of the above (14 routes); tasks carry Go / Hold to the phone. |
| v0.251.0 | Punch review is a compliance item (AFB-2026-00032): missing clock-outs, late offline punches, edits, GPS outside the block, short breaks and Oregon ag overtime raise compliance alerts; a supervisor approves or fixes each punch or the whole period; reviewed punches lock for payroll; payroll preview warns on unreviewed ones; the DOL packet lists every review. Eight rules seeded ON. |
| v0.252.0 | Each person's own Start of Day and End of Day check (End of Day files hour-meter readings and crop stages; switch OFF). Fix: merging a business card into an existing contact now updates where and when you met and the company; the contact's address is reused and linked to the new Supplier. Phone routes to find a contact and correct where you met. |
| v0.253.0 | Rain risk from forecast chance AND amount, with each rule's own rain threshold (0.05 in by default); Holds clear within the hour when the forecast dries; pruning defaults 7 days ahead / dry 48 h; a daily check of whether it actually rained after weather-gated work, with an alert rule (OFF) and a forecast score. |
| v0.254.0 | Refusals reach a Spanish phone saying why, in tú (about 50 worker-facing messages); all phone Spanish brought to tú. Fix: receipt, signature and task-evidence checks where the phone extracted nothing were refused — they are now filed. |
| v0.255.0 | A well's logs on the well (AFB-2026-00021): Library documents cited on an asset show where its tag is scanned and open on the phone; type "Record" for the farm's own records. The pruning preset seeded by an earlier release takes the new defaults if nobody touched it. |
| v0.256.0 | `relink_expense_receipt` (off): point a filed receipt at an invoice or JE entered elsewhere, move it, or clear it with a reason. |
| v0.257.0 | `plan_mcp_system_user` (read): the roles a dedicated MCP System User needs, a dry run for a candidate account and the Desk steps — changes nothing. |
| v0.258.0 | office@ replies reviewed on the phone: tile **Replies to review** (hidden until office@ is on) — read, correct, attach, approve and send with Face ID, or discard. Pairs with app 0.46.0. |
| v0.259.0 | Form 940 and W-3 via `generate_tax_form` (940 = the FUTA summary's walk; W-3 = the W-2s totalled; `kind_of_payer: 943` for an agricultural filer). |
| v0.260.0 | **Least-privilege hotfix.** HR reads (others' I-9, discipline, everyone's pay, deductions, personnel files) need HR Manager / HR User — the Farm Owner profile carries HR Manager; accident reports, the compliance calendar, receipts (own only), housing occupants and search filters narrowed. **Check Tim holds HR Manager after the migrate.** |
| v0.261.0 | Claim a task with no signal: the first claim to reach the server holds it; a later one is kept as a second claim with the worker's time and evidence, flagged "claimed offline by two people" (rule task_claimed_offline_twice ON) for a supervisor to review. Online claims unchanged. Pairs with app 0.48.0. |
| v0.262.0 | The IPM relationship graph ("mind map"): editable nodes and relationships seeded from the reference (106 / 157), vertebrates with exact MBTA status, twelve sweet-cherry starter thresholds seeded OFF and Proposed, lowest-impact-first options, phone tile IPM map, MCP read / write (writes OFF). Pairs with app 0.49.0. |
| v0.263.0 | The IPM Map in the Desk (Crop Protection → IPM Map): same graph and same edit rule as the phone; filters, side panel with observations, PNG / SVG / CSV export; library vendored (no CDN). |
| v0.264.0 | Pest degree days per block on the farm's own weather (Open-Meteo): models and block offsets (slope / aspect / elevation) as config a person publishes, calibration that only drafts; shown in the graph, threshold checks, Desk map, an opt-in map layer, Go / Hold presets (OFF). 4 of 28 pests have models, all marked verify. Pairs with app 0.50.0. |
| v0.264.1 | Tim's harvest windows (Mill Creek 24, 40 Acre 6) as calibration seed data; `propose_harvest_calibration` (off) drafts DD calibrations from them — nothing applied. |
| v0.265.0 | USDA AMS market prices: shipping point (primary), terminal (context, the cost of market access), movement. Idempotent Price Points, no carry-forward, gaps and format changes flagged not dropped. Any commodity as config: sweet cherries and cantaloupe published, ten tree fruit / melons seeded as drafts; the AMS catalog browsable, drafts built from observed data. Fetch once per report, fan out. Daily pull 05:30 only once the key is set. Phone market card and chart routes; MCP reads, writes OFF. |
| v0.266.0 | Desk **Market Prices** page (candles + volume, season over season, bars, overlays; vendored chart library, no CDN), the market in the pro forma's breakeven sensitivity, phone tile **Market prices** (app 0.51.0+). |
| v0.267.0 | Who sees what on the phone, as data: one Data Access policy (tiers, gates, restricted fields, all 428 routes) filtering every answer at one exit; seeded to today's behaviour (nothing changes until a System Manager publishes); gates may only narrow (private HR never back to Farm Manager); ten over-shares listed for Tim in `v0.267.0_data_access.md` §4. |
| v0.267.1 | Four access-audit fixes (before Constancy): routing number masked except payroll / HR; employee-file login ID and IPs to System Manager only; accident and leave lists and totals one company's; contacts by farm entity (older contacts stamped where unambiguous). |
| v0.268.0 | The phone's company switcher: a person in several companies picks one in the header and every company-scoped list follows it (accidents included); the server applies the choice to every route that takes a company and refuses a company the person is not in. No migrate. Pairs with app 0.53.0. |
| app 0.38.8 | Due dates and overdue on task rows and a Due section on Today, computed on the phone (works offline). |
| app 0.39.0 | Go / Hold chips and card, supervisor override, crop-stage picker (offline), Today: work notices, notices to decide, "Good to do today", SOPs to review; approved SOPs in "How this job is done". Spanish (tú). |
| app 0.40.0 | The course player: course videos with how much was watched, the knowledge check, trainer sign-off. |
| app 0.41.0 | Punch review for supervisors: this week's punches, why each is flagged, approve or fix with a reason; "Punches to review" on Today. |
| app 0.42.0 | My day: Start / End of Day on Today. Contacts: find a contact and correct where you met (Foreman+). |
| app 0.43.0 | Spanish sweep: 133 longer explanations that always showed in English now read in Spanish (tú). |
| app 0.44.0 | A refusal from the farm shows the server's Spanish when the phone is in Spanish. |
| app 0.45.0 | Library documents on the asset screen and on a tag scan, kept on the phone. |
| app 0.46.0 | Replies to review: office@ drafts read, corrected and approved (Face ID) or discarded on the phone. |
| app 0.47.0 | Sign-out leaves nothing of the last person's on a shared phone; unsent queued work is counted in the sign-out warning. |
| app 0.48.0 | Claim a task with no signal ("Claimed on phone — will confirm when synced"); told, by name, when someone else's claim reached the farm first — your time and photos are kept. |
| app 0.49.0 | IPM map on the phone: the graph, node detail, log a pest (threshold status, lowest-impact option first), add / edit relationships for managers; works offline. |
| app 0.50.0 | Degree days per block on the IPM map: next-event dates on pests, a Degree days screen, the offset and its reasons. |
| app 0.51.0 | Market card ("Should I be picking today?") and a stock-style price chart; works offline from the saved copy; no quote never shows an old price. |
| app 0.52.0 | On-device protection: people/money caches locked when the phone is locked (background-sync stores stay readable after first unlock), all caches out of backups, no HTTP cache, sign-out now wipes four stores it missed. |
| app 0.53.0 | Company switcher chip on every company-scoped list (hidden for single-company users); the choice is remembered, sent with every request, and caches are kept per company. |

**1. Build (Tim)**

Push erpnext_mcp main (`5ab9b1b`), build the fafo-erpnext image with `ERPNEXT_MCP_VERSION=0.268.0`. Push fafo_ios main
(`a06adc8`) and archive FarmOps 0.53.0 (build 48), scheme **FarmOps**.

**2. Each server: pull, restart, ONE migrate** — umbrel.local first, then OML (orchardmeadow-umbrel) when Tim
chooses. The full command list is in `~/Desktop/deploy-catchup-latest.txt`.

Expect from the migrate (first run only):

1. "payroll setting payroll_setting:overtime_rule@1 seeded — 40 hours a workweek, 1.5×, as before."
2. "rule farm_task_due seeded OFF …"
3. "daily check templates seeded: Daily equipment check, Daily check add-on — Tractor, Daily check add-on — Mini
   Excavator, Start of Day — me, End of Day — me …"
4. "Go / Hold presets seeded OFF: go_hold_pruning_canker, go_hold_spray_wind, go_hold_burn, go_hold_sop_approved,
   go_hold_irrigation_rain, go_hold_harvest_heat."
5. "course videos moved in from video_url: …" only where a course had a video URL.
6. "punch review rules seeded: punch_missing_clock_out, punch_runaway, punch_offline_window, punch_edited,
   punch_outside_block, punch_breaks_short, overtime_week_approaching, overtime_week_exceeded."
7. "rain archive check rule seeded OFF: weather_check_pruning_rain."
8. "Market Commodity sweet_cherries seeded.", "Market Commodity cantaloupe seeded." and "Market Commodity drafts seeded
   (unpublished): apples, pears, peaches, nectarines, apricots, plums, grapes, blueberries, watermelon, honeydew." and "contacts given a farm entity: N; left to their capturer only (several entities): M."
9. `__version__ = "0.268.0"`.

**3. Checks after the migrate**

1. `get_server_status`: 0.268.0. Desk → any Asset Register form: the map's build stamp reads 0.268.0.
2. Payroll: `preview_payroll_for_period` on last week matches the posted run (identical pay).
3. Desk → Compliance Rule: six Go / Hold presets and weather_check_pruning_rain, all disabled. ERPNext MCP Settings: every new write switch unticked;
   No-Work Notices, Knowledge Checks, Irrigation Schedule, Upload Links Enabled, Daily Equipment Checks, Personal Day Checks unticked.
4. Phone 0.53.0: Today (a Market prices tile) and a task open normally; a task with no rule shows no Go / Hold chip. Business card scan
   works for a Foreman.
5. Punch review (`v0.251.0_punch_review.md`): this week's flagged punches appear as compliance alerts and under
   "Punches to review" on Today for a Foreman / Farm Manager; `preview_payroll_for_period` warns on unreviewed punches
   with the pay figures unchanged.
6. Business-card merge (`v0.252.0_day_checks.md` §5): on OML, re-run the Ben Sheppard merge with Met At
   "Sheppard's, 440 Riverside Dr, Hood River, OR 97031" (or correct it from the phone: Contacts → Where met); Met At
   now reads that, and Sheppards-Office-1 links to Supplier Sheppard's.
7. Per feature, when Tim turns it on: the release's own file (`v0.236.0_task_dates.md` … `v0.252.0_day_checks.md`, `v0.253.0_dry_day.md`, `v0.254.0_spanish_errors.md`, `v0.255.0_asset_library.md`, `v0.256.0_receipt_relink.md`, `v0.257.0_mcp_system_user.md`, `v0.258.0_replies_to_review.md`, `v0.268.0_company_switcher.md`, `v0.267.1_access_audit_fixes.md`, `v0.267.0_data_access.md`, `v0.266.0_market_dashboard.md`, `v0.265.0_market_prices.md`, `v0.264.1_harvest_calibration.md`, `v0.264.0_pest_dd.md`, `v0.263.0_ipm_desk_map.md`, `v0.262.0_ipm_graph.md`, `v0.261.0_offline_claim.md`, `v0.260.0_data_access_hotfix.md`, `v0.259.0_940_w3.md`,
   `app_0.39.0_work_timing.md`, `app_0.40.0_course_player.md`, `app_0.41.0_punch_review.md`,
   `app_0.42.0_my_day_contacts.md`, `app_0.43.0_spanish_sweep.md`, `app_0.44.0_server_spanish.md`, `app_0.45.0_asset_library.md`, `app_0.46.0_replies_to_review.md`, `app_0.47.0_signout.md`, `app_0.48.0_offline_claim.md`, `v0.262.0_ipm_graph.md` (app 0.49.0), `v0.264.0_pest_dd.md` (app 0.50.0), `v0.265.0_market_prices.md` (app 0.51.0)).

**4. Rollback**

Everything is additive and off by default. An older image ignores the new doctypes and fields; FarmOps 0.38.7 ignores
the new routes. Punch-review rules switch off as data (Desk → Compliance Rule).
