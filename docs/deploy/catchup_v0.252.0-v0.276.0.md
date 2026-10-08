**Deploy: v0.252.0 → v0.276.0 (OML today) — one build, one migrate; FarmOps 0.57.0 (build 53)**

For a server already on v0.252.0. Same image and commands as `catchup_v0.231.2-v0.276.0.md` and
`~/Desktop/deploy-catchup-latest.txt`; only the expectations differ.

- **Server**: erpnext_mcp main **`7e54e72`** (`__version__ = "0.276.0"`).
- **App**: fafo_ios main **`25de4f5`** — FarmOps **0.57.0 (build 53)**, scheme **FarmOps**.

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
| v0.262.0 | The IPM relationship graph ("mind map"): editable nodes and relationships seeded from the reference (106 / 157), vertebrates with exact MBTA status, twelve sweet-cherry starter thresholds seeded OFF and Proposed, lowest-impact-first options, phone tile IPM map, MCP read / write (writes OFF). Pairs with app 0.49.0. |
| v0.263.0 | The IPM Map in the Desk (Crop Protection → IPM Map): same graph and same edit rule as the phone; filters, side panel with observations, PNG / SVG / CSV export; library vendored (no CDN). |
| v0.264.0 | Pest degree days per block on the farm's own weather (Open-Meteo): models and block offsets (slope / aspect / elevation) as config a person publishes, calibration that only drafts; shown in the graph, threshold checks, Desk map, an opt-in map layer, Go / Hold presets (OFF). 4 of 28 pests have models, all marked verify. Pairs with app 0.50.0. |
| v0.264.1 | Tim's harvest windows (Mill Creek 24, 40 Acre 6) as calibration seed data; `propose_harvest_calibration` (off) drafts DD calibrations from them — nothing applied. |
| v0.265.0 | USDA AMS market prices: shipping point (primary), terminal (context, the cost of market access), movement. Idempotent Price Points, no carry-forward, gaps and format changes flagged not dropped. Any commodity as config: sweet cherries and cantaloupe published, ten tree fruit / melons seeded as drafts; the AMS catalog browsable, drafts built from observed data. Fetch once per report, fan out. Daily pull 05:30 only once the key is set. Phone market card and chart routes; MCP reads, writes OFF. |
| v0.266.0 | Desk **Market Prices** page (candles + volume, season over season, bars, overlays; vendored chart library, no CDN), the market in the pro forma's breakeven sensitivity, phone tile **Market prices** (app 0.51.0+). |
| v0.267.0 | Who sees what on the phone, as data: one Data Access policy (tiers, gates, restricted fields, all 428 routes) filtering every answer at one exit; seeded to today's behaviour (nothing changes until a System Manager publishes); gates may only narrow (private HR never back to Farm Manager); ten over-shares listed for Tim in `v0.267.0_data_access.md` §4. |
| v0.267.1 | Four access-audit fixes (before Constancy): routing number masked except payroll / HR; employee-file login ID and IPs to System Manager only; accident and leave lists and totals one company's; contacts by farm entity (older contacts stamped where unambiguous). |
| v0.268.0 | The phone's company switcher: a person in several companies picks one in the header and every company-scoped list follows it (accidents included); the server applies the choice to every route that takes a company and refuses a company the person is not in. No migrate. Pairs with app 0.53.0. |
| v0.268.1 | get_backup_status reads the backup kit's verdict words correctly (a restore's check, not the job's OK); extra files are not a failure; umbrel.local is judged on its own archive tests, not a peer restore it never gets; a Partial with nothing lost is a warning; own archive tests are recorded as Backup Record tests. No migrate. |
| v0.269.0 | Fields by the names people use (aliases, named groups like "Bing Block", rename keeping the old name); acreage follows the drawn outline on new blocks (a figure typed or from FSA is kept; changes logged; a gross redraw refused unless meant); field card and field history on phone, Desk and MCP; Hazard Marker pins; add task here. Migrate. Pairs with app 0.54.0. |
| v0.270.0 | Seasonal work lists: Fall winterize checklists per machine type and for the farm, raised one task per asset on Oct 25 or the first forecast hard freeze (28 °F, 10 days ahead), overdue alerts 3 days before; Spring start-up the same. Programs seeded as drafts and checklists disabled until Tim publishes. Today tile Seasonal work. Migrate. |
| v0.271.0 | Contractor and supplier job links: a work order / delivery / pickup from a template (orchard removal, supplier delivery, supplier pickup), blocks by name or alias, prep tasks raised per block (gather sprinklers, mark valves); a time-limited link shown once, a map page with only that job (blocks, valves, hazards, entrance, Directions), arrived / done / delivered / ticket photo, view log; sharing OFF per company until job_links_enabled. Migrate. Pairs with app 0.55.0. |
| v0.272.0 | Chemical receiving: a supplier delivery's lines from documents — the ticket read on the phone, the emailed invoice PDF, the portal CSV — through Supplier Connectors (config, seeded as drafts); matched to the PO and Items and reconciled (short / over / substitution / not ordered); a person settles the rest; a DRAFT Purchase Receipt into chemical storage with a Batch per lot; the check-in task (count, lots, labels, SDS, storage; restricted-use needs the Applicator License); lot → spray trace. OFF per company until chemical_receiving_enabled. Migrate. Pairs with app 0.56.0. |
| v0.273.0 | The shop queue: Shop work as a tick on a task template; the backlog is the open shop tasks (winterize first, machine repairs, shop templates, training) plus suggestions from what is due (service, low stock, tags); a weather day from the forecast (rain, wind, a freezing high) or declared; a list filled to the hours with learner pairing; Today tile Shop work today; an opt-in evening notice. Ten Mid-Columbia starter templates, disabled. Migrate. |
| v0.274.0 | The live budget: the lender pro forma imported as a draft Input Plan (Growing Budget lines, accounts and cost centres from the CoA Mapping, acres, the spray program); budget / actual / committed / forecast per account and cost centre with 90% / 100% alerts; spray materials still ahead; requests netted against the shed (draft Material Request); ERPNext Budgets drafted with Warn; spray cost per block. OFF per company until live_budget_enabled. Migrate. |
| v0.275.0 | Pollination: drop points inside the outlines clear of hazards (hives by acre on pallets), crew trips batched by the machine; the beekeeper's link (Delivered with counts, Picked up); distribution trips held until delivery, gather-up until petal fall; the pickup gate; five counts reconciled with flags; drops moved by drag; a bee-toxic spray hold from the IPM map (seeded OFF); the rental invoice check. OFF per company until pollination_enabled. Migrate. Pairs with app 0.57.0. |
| v0.276.0 | The packer portal (OVF): a read-only share of the committed blocks by ticker — sprays with MRL by market, IPM, projections, clear to harvest; CSV / XLSX / PDF / JSON and a Bearer feed; a named revocable credential per contact; every view and download logged; never costs, people or other blocks. OFF per company until packer_portal_enabled. Migrate. |
| app 0.43.0 | Spanish sweep: 133 longer explanations read in Spanish. |
| app 0.44.0 | A refusal from the farm shows the server's Spanish on a Spanish phone. |
| app 0.45.0 | Library documents on the asset screen and on a tag scan, kept on the phone. |
| app 0.46.0 | Replies to review: office@ drafts read, corrected and approved (Face ID) or discarded on the phone. |
| app 0.47.0 | Sign-out leaves nothing of the last person's on a shared phone; unsent queued work is counted in the sign-out warning. |
| app 0.48.0 | Claim a task with no signal ("Claimed on phone — will confirm when synced"); told, by name, when someone else's claim reached the farm first — your time and photos are kept. |
| app 0.49.0 | IPM map on the phone: the graph, node detail, log a pest (threshold status, lowest-impact option first), add / edit relationships for managers; works offline. |
| app 0.50.0 | Degree days per block on the IPM map: next-event dates on pests, a Degree days screen, the offset and its reasons. |
| app 0.51.0 | Market card ("Should I be picking today?") and a stock-style price chart; works offline from the saved copy; no quote never shows an old price. |
| app 0.52.0 | On-device protection: people/money caches locked when the phone is locked (background-sync stores stay readable after first unlock), all caches out of backups, no HTTP cache, sign-out now wipes four stores it missed. |
| app 0.53.0 | Company switcher chip on every company-scoped list (hidden for single-company users); the choice is remembered, sent with every request, and caches are kept per company. |
| app 0.53.1 | The Today map shows every asset on the farm as you pan (it showed only the five nearest you), the same as the All Assets map. |
| app 0.54.0 | Tap a block on the map for its card (aliases, acreage, open tasks, hazards, history with filters); long-press to add a task there (offline, no duplicates on retry); drop valve / hazard pins; draw, edit or walk a block's outline. |
| app 0.55.0 | Work → Contractor jobs (Foreman / Farm Manager): readiness, the contractor's page, links and what the contractor reported; a Farm Manager marks ready, shares a one-time link through the share sheet, or revokes it. |
| app 0.56.0 | Work → Deliveries (Foreman / Farm Manager): read the ticket with the camera (on-device), correct the lines, send; match badges and flags; a Farm Manager accepts / rejects lines and drafts the receipt. |
| app 0.57.0 | Contractor jobs → a Pollination job opens the hive map: blocks, hazards, the loading area, every drop, the trips and the five counts; a Farm Manager drags a drop to move it. |

**1. Build (Tim)**

Push erpnext_mcp main, build the fafo-erpnext image with `ERPNEXT_MCP_VERSION=0.276.0`. Push fafo_ios main and archive
FarmOps 0.57.0 (build 53).

**2. Pull, restart, ONE migrate** (commands in `~/Desktop/deploy-catchup-latest.txt`, section 3 for OML)

Expect from the migrate: "rain archive check rule seeded OFF: weather_check_pruning_rain.", "Go / Hold presets brought to
today's defaults (untouched, still OFF): go_hold_pruning_canker." the market commodity lines "Market Commodity sweet_cherries seeded.", "Market Commodity cantaloupe seeded." and "Market Commodity drafts seeded (unpublished): apples, pears, peaches, nectarines, apricots, plums, grapes, blueberries, watermelon, honeydew." and "contacts given a farm entity: N; left to their capturer only (several entities): M." and "seasonal programs seeded as drafts (publish in the Desk): fall_winterize, spring_startup" and "job templates seeded: orchard_removal, supplier_delivery, supplier_pickup", "supplier connectors seeded as drafts …", and `0.276.0`.

**3. Checks**

1. `get_server_status`: 0.276.0; the asset map's build stamp 0.276.0.
2. Desk → Compliance Rule: `weather_check_pruning_rain` present and disabled; `go_hold_pruning_canker` now reads the
   new defaults (chance_over 0.05 in, dry 48 h) — unless somebody had edited or enabled it, in which case it is as they
   left it.
3. Phone 0.45.0 in Spanish: claiming a task somebody else is doing reads "<nombre> ya está haciendo FT-…".
4. A receipt photo the phone cannot read is filed (Needs Review), not refused with "extracted_fields is required".
5. Put a well log on a well (`v0.255.0_asset_library.md` §3) and scan its tag: the log is under Documentation.
6. Desk → Market & Sales → **Market Prices** opens (empty until the USDA key is set and a backfill runs —
   `v0.265.0_market_prices.md`). Market Commodity: sweet_cherries and cantaloupe Published, ten Drafts.
7. Afterwards: resolve AFB-2026-00014 (weather-based tasks and holding periods) — it is v0.239.0–v0.253.0.

**4. Rollback**

The v0.252.0 image; the new rows, log entries and asset citations are ignored by it.
