# Patterns, tool consolidation, daily-pay policies, field fixes — plan for Tim's OK (2026-10-08)

Status: PLAN. Nothing in §2 (the consolidation refactor) starts until Tim says OK. The bug fixes in §4 can ship
first because they change no tool's shape.

## 1. The five patterns — build each once, then every feature is a template / config plus a thin UI

| # | Pattern | The one engine | Queue items on it |
|---|---|---|---|
| P1 | **Template → instance → checklist → evidence → history** | Farm Task Template → Farm Task (checklist, evidence, `creates_record`) → asset / field history | seasonal lists (v0.270), shop queue (v0.273), receiving check-in (v0.272), orchard-removal prep (v0.271), hive trips (v0.275), daily checks, the year schedule, scouting → Crop Observation |
| P2 | **Share link** | token (hashed, shown once) + scope resolver + expiry policy + allow-listed page sections + actions + view log + auth (path or Bearer) | contractor (v0.271), supplier delivery / pickup (v0.271), beekeeper (v0.275), packer portal (v0.276), upload links (v0.244) |
| P3 | **Map layer + pins on field outlines** | one pin record with a `kind` + one layer reader | hazards and valves (assets), hive drops, delivery spot, entrance, loading area, traps, future pins |
| P4 | **Plan → request → order → receive → use → budget** | Input Plan → Material Request → PO → delivery intake → Purchase Receipt + Batch → use (spray lot) → GL actual vs plan | chemical receiving (v0.272), live budget (v0.274), the pollination rental check (v0.275), fertilizer and parts later |
| P5 | **Config lifecycle** (draft → stage → preview → publish → rollback; validators per kind) | Farm Config Version + the generic config tools | every template and policy: job templates, connectors, input plans, pollination plans, seasonal programs, pay policies, the variety list, the DD stage table |

What changes to make P1–P3 real (all on code that is not yet deployed, so it is cheap now):

- **P1:** the template carries timing (date window, BBCH, DD, recurrence), skill level (any / learner OK /
  supervised / skilled — generalising v0.273's shop field) and a category. Go / Hold matches template or category,
  not words in the task name. Blank company means shared.
- **P2:** v0.271's Contractor Job Link and v0.276's packer credential become one **Share Link** record. The kind
  comes from config: what the token sees, for how long, and which actions it allows. One public route and one page
  script render any kind's sections.
- **P3:** hive drops, the delivery spot, the entrance and the loading area become pins of their kind. They are not
  JSON on the job or fields on Contractor Job. Valves and hazard markers stay assets and are read by the same
  layer.

**Items that don't fit a pattern:**
- **Daily pay:** payroll is special-purpose (§3). Its policies use P5.
- **The Field variety table and renames:** record editing, reached through `update_field` (§4).
- **The DD units check:** a bug fix (v0.276.1).
- **Bank, tax, I-9 and signing:** special-purpose, and they stay as they are.

## 2. Tool consolidation (needs Tim's OK)

**Today: 1,136 tools on main, 590 of them writes.** The switch-trim study counts 952 write tools ON across both
servers and 238 needed daily. Of 590 write tools, only about 130 were called at all since early August.

| Family | OML (v0.255) | umbrel (v0.269) | main (v0.276.1) | Proposed | Generic tool (what it replaces) |
|---|---:|---:|---:|---:|---|
| Readers | 363 | 375 | 389 | ~30 | **`get_record(type, name)`** and **`list_records(type, filters, fields, page)`** replace the plain `get_<x>` / `list_<x>`; **`report(kind, …)`** (extends `run_report`) replaces the `get_<x>_report` / `_summary` family; unique analyses stay (trace, breakeven, …) |
| Create / update / delete | 214 | 221 | 226 | ~25 | **`record(action=create\|update\|deactivate\|delete, type, …)`** (built on `update_document`'s whitelist) replaces `create_<x>` / `update_<x>` / `delete_<x>` for registers with no special rules; each kind calls the old function |
| Actions and computations | 174 | 188 | 202 | ~60 | **`generate(kind)`**, **`import_data(kind)`**, **`export_data(kind)`** replace the generate_* / import_* / export_* families; unique computations stay |
| Special: money, HR, signing, security | 172 | 173 | 175 | ~150 | **kept**: I-9, payroll, NACHA, bank, tax, W-4, garnishments, signing, sealing, tokens, roles |
| Config lifecycle and rules | 61 | 62 | 62 | 3 | **`config(action=list\|get\|diff\|preview\|draft\|stage\|publish\|rollback, kind)`** replaces the per-kind phone / extraction / label-profile / payroll-setting / commodity tools; plus `propose_rule`, `test_rule` |
| Submit / cancel / approve | 35 | 36 | 37 | 2 | **`submit_document(type, name)`** and **`cancel_document(type, name, reason)`**, with approve / reject / close via `transition(type, name, action)` on `advance_workflow`; type guards kept; bank and payroll submits stay separate |
| Templates | 28 | 28 | 28 | 2 | **`template(action=create\|update\|preview\|enable\|list\|get, kind=task\|inspection\|training\|trade_document\|wizard\|job\|…)`** replaces the create / update / preview / approve / deactivate tools of each template kind |
| Share and one-time links | 9 | 10 | 17 | 1 | **`share_link(action=create\|extend\|revoke\|list\|log, kind=contractor\|delivery\|pickup\|pollination\|packer\|upload\|enrollment)`** |
| **Total** | **1,056** | **1,093** | **1,136** | **~270** | first estimates; step 0 produces the exact old → new mapping file |

Write tools ON (the switch-trim study): 952 across both servers, 238 needed daily. Of main's 590 write tools, ~130
were called at all since early August.

**Rules that keep it safe**
1. **No behaviour lost.** A generic tool is a dispatcher. Each `type` / `kind` calls the function the old tool
   called, with the same validation, permissions, company scoping and answer shape.
2. **No permission widened.** Today's `allow_<tool>` checkbox becomes the per-kind gate (for example,
   `record(type="Supplier", action="create")` is allowed only where `allow_create_supplier` is). Switching on a
   generic tool switches on nothing new.
3. **Aliases.** Every old name stays callable for one deprecation window. It is hidden from `tools/list`, so it
   has no context cost. Every call is logged with the caller, so we learn what the 3:30 AM brief and other
   automations call; no scheduled task exists on this Mac, so it runs elsewhere. An alias is removed only after
   14 days with zero calls (the rule Tim set for config aliases).
4. **Exposure by group.** MCP Settings gets tool groups and presets: Daily ops, Setup, Finance, HR, Admin. A
   client sees only its switched-on groups. Per-tool checkboxes stay as overrides. "On when needed" uses the
   existing timed switches by default. This replaces ticking about 1,095 boxes in about 130 sections, and lines up
   with the switch-trim study's KEEP ON / ON WHEN NEEDED / KEEP OFF buckets.
5. **Attribution and the MCP System User.** Every call records the client and model and the human it acts for, not just Administrator.
   Calls run as a dedicated limited-role MCP System User (`plan_mcp_system_user` already lists the roles). The
   Action Log is paginated (it caps at 500 today).

**Migration order** (each step its own release, with a deploy file)
- **Step 0:** the mapping file, old tool → generic call with arguments, generated from the registry, plus a parity
  test harness. No user-visible change.
- **Step 1:** groups and presets, attribution, Action Log pagination, timed switches by default. The context-cost
  win lands here, before any tool is renamed.
- **Step 2:** `config`, `template` and `share_link`. These are the newest and least-called tools, and the v0.271
  and v0.276 share-link engines merge (P2).
- **Step 3:** readers (`get_record`, `list_records`, `report`).
- **Step 4:** `record` (CRUD).
- **Step 5:** `transition`.
- **Step 6:** generators and import / export.
- **Not touched:** special-purpose tools.

**Tests**
- Parity: every existing tool test also runs through its alias into the generic tool, and the answer is identical.
- Every old name is in the mapping.
- Each preset's `tools/list` size is pinned.
- The per-kind switch gates are proven: a disabled kind is refused through the generic tool.
- Attribution rows are written.
- Action Log paging returns everything past 500 rows.

**Risks**
- An automation calls an old name with odd arguments. Aliases and logging cover this.
- A per-register rule that lived only in the old tool's wrapper. The parity tests catch it.
- Clients cache the tool list. Presets change `tools/list`, and a client has to reconnect.

## 3. Daily pay: every decision is a policy, stored as data

- **The policy.** A config kind **Pay Policy**, with draft → stage → preview → publish → rollback. Preview reruns
  a past week under the new policy, per worker, to the cent, the same discipline as the v0.235 overtime move.
  Fields: the model (daily periods / weekly with advances), eligibility (employment types, signed opt-in, switch
  only at a week boundary), workweek start, the daily cutoff hour, the settlement payday, ACH timing (next day, or
  same day with the bank's cutoff), the overtime policy (state floor, or a farm threshold and multiplier never
  below the floor), workers without direct deposit (weekly / daily check), and the cost report on or off. The
  existing `payroll_setting:overtime_rule` folds into it.
- **The law.** The floors are a dated data table, a config kind **Wage Floor**, with a source on every row:
  - minimum wage by state and region with effective dates (Oregon standard / Portland metro / non-urban;
    Washington);
  - agricultural overtime thresholds by state and date (Oregon HB 4002: 55 h 2023–24, 48 h 2025–26, 40 h from
    2027; Washington: 40 h from 2024);
  - the pay-period basis for the minimum-wage top-up.
  
  A new legal rate is a new row, not a release.
- **The check.** A Pay Policy below the floor in effect on any date is refused at publish. A compliance rule
  re-checks each run, so a floor that rises later raises an alert before the run.
- **The defaults** are the conservative ones in the eight questions (A, field types opt-in, Sunday week, 6 p.m.,
  next-day ACH, 40 h, weekly without direct deposit, cost report on). Tim can change any of them later as data.

## 4. Field fixes and gaps from the year schedule and OML cleanup

| Item | Fix | Pattern / tool |
|---|---|---|
| Field variety-mix rows (Black / Ebony / Burgundy Pearl, Pearls 2018, Centerpiece Bing + Van) | `update_field(varieties=[{variety, percentage, planting_year}])` replaces the child table; percentages blank or summing to 100 | extends the existing `update_field` |
| Renames (Wind Machine, 40a Pearls 2013, MC Sweet Note, Centerpiece + the "Bing Block" group alias) | v0.269.0 already renames keeping the old name, with group aliases. **OML runs v0.255.0**: these work after the catch-up deploy | existing (v0.269) |
| Variety typos | a **Variety List** config per crop (names, aliases, pollinizer flag); `update_field` checks variety against it with "did you mean"; seeded from the varieties in use | P5 + `update_field` |
| Scouting doesn't write the Crop Observation | completing a template with `creates_record: Crop Observation` writes it (BBCH, pest counts, Brix) — verify end to end, fix the builder | P1 (`creates_record`) |
| Template timing (window, BBCH, DD, recurrence) | fields on Farm Task Template; one daily runner raises due instances through the CCF rule path (confirm or add the rule action) | P1 + P5 |
| Go / Hold matches words in task names | match `template` or `category`; presets migrated | P1 |
| Learner / supervised skill | `skill_level` on every template (generalises the shop field) | P1 |
| `go_hold_irrigation_rain` never fires | "Irrigation" is in the task-type list on main; check OML's list (v0.255) and the matcher; fix whichever is wrong | bug |
| `create_farm_task_template` stamps a company on blank | blank means shared | bug in existing tool |
| Task-type docs list fewer than the server accepts | generate the enum and docs from the doctype's options | bug |
| Cherry DD stage table | a proposal file: BBCH → °F·day from biofix, derived from OML's recorded stages against live GDD and cross-checked with WSU / OSU phenology, with the **base 40/41 °F vs 50 °F** question flagged; becomes a config kind (P5) rather than a Settings text box | P5 |
| Block-level forecasts | Desk-only toggle; ON on OML, OFF on umbrel.local — in the deploy notes | ops |
| Degree-day units | v0.276.1: the engine was already °F; reference made explicit; guard added; past-date series fixed | done |

## 5. Proposed order

1. **v0.276.1:** the degree-day units release, now.
2. **v0.277.0:** the §4 fix pack: variety rows and variety list, scouting writing the Crop Observation, template
   timing / skill / category, Go / Hold by template, blank company, the task-type enum, the irrigation preset.
   This adds no new tools; it extends existing ones.
3. **v0.278.0:** consolidation step 0 + step 1 (mapping and parity harness; groups, presets, attribution, Action
   Log paging, timed switches).
4. **v0.279.0:** P2 share-link engine and P3 field pins (folding v0.271 / v0.275 / v0.276) + consolidation step 2.
5. **v0.280+:** consolidation steps 3–6.
6. **Daily pay:** Pay Policy and Wage Floor first, then the runs.

**What each new feature plan will show** (Tim's standing rule): the existing tools reused or extended, the
genuinely new tools with a one-line justification each, and the net change in tool count.
