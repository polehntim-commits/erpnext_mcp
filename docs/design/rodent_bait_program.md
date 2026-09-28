# Rodent bait at housing and buildings — contract (frozen, v0.203.0)

Tim approved this on 2026-09-27: pest control for all housing and buildings, with occupied buildings
in the strictest tier. Rodent bait is part of **camp and housing maintenance**. It is not a
standalone program.

**Starting point on OML.** Another session drafted the pieces below. They are all disabled, and this
release does not enable them:

- **Farm Task Templates:**
  - Rodent Bait Placement - Exterior
  - Rodent Bait Placement - Interior
  - Rodent Bait Check
  - Rodent Bait Removal and Clearance
  - Rodent Bait Occupant Notice (EN/ES)
- **Compliance Rules:**
  - CRULE-2026-0045 `rodent_bait_interior_placement`
  - CRULE-2026-0046 `rodent_bait_check_overdue`
  - CRULE-2026-0047 `pest_control_label_fields_missing`

This release ships the same templates and rules as **create-only seeds**, so a new site gets them
and OML keeps its own drafts. A seeded rule or template arrives **disabled**, the same as a draft.

Tim's decisions so far:

- Checks every 7 days in season and every 30 days off season (2026-09-27).
- The monthly interval applies only to maintenance stations with no activity.

Everything else he has not decided has a default and is configurable (§9).

## 1. Words

| Term | Meaning |
|---|---|
| **Bait task** | A Farm Task whose `template` is one of the five above. The names live in `rodent_bait.TEMPLATES`. |
| **Placement task** | A bait task from one of the two Placement templates. `bait_placement` is `Exterior` or `Interior`. |
| **Location** | The task's `location_doctype` + `location`. It is either a **Housing Unit** or an **Asset Register** row. Building asset types are Cabin, House, Housing Unit, Storage and Cold Storage (§9). |
| **Round** | Everything at one location from a placement up to the next *completed* Removal and Clearance. |

## 2. Occupancy (item 2)

`rodent_bait.occupancy(location_doctype, location, on=today)` returns
`{occupied: bool, source, detail}`.

The first source that matches wins:

1. **Housing Unit:** a Housing Assignment on the unit with `status = Current`, `assigned_date ≤ on`,
   and `end_date` empty or `≥ on`. The source is `housing_assignment`.
2. **Manual flag.** `occupied = 1` on the Housing Unit or on the Asset Register row gives source
   `manual_flag`.
   - This flag is **first-class**, not a fallback. At Mill Creek, four houses are occupied at
     takeover, and their tenants never come through a Housing Assignment.
3. **People work here.** Either the location has `people_work_here = 1`, or its type is in the
   people-work-here list (§9). The source is `people_work_here`.
4. Otherwise the location is **unoccupied** (source `none`).

The manual flag can only make a location occupied; it never makes one unoccupied. An active
assignment always counts, whatever the flag says.

New fields, all `Check` and default 0:

- `occupied` and `people_work_here` on **Housing Unit** and **Asset Register**.
- They are settable through `create_housing_unit` / `update_housing_unit`, `register_asset` /
  `update_registered_asset`, and the matching mobile routes.

**Snapshot on the task.** A bait task gets `occupancy_at_creation` (Select `Occupied` or `Unoccupied`)
and `occupancy_source` (Data).

- They are set in the Farm Task controller's `before_insert`, so every creation path stamps them.
- They are never recomputed. A read or a rule that needs today's value calls `occupancy()`.

**Asset types.** `Cabin` and `House` are seeded Farm Asset Types, create-only. No Cabin or House
asset is registered by this release.

## 3. The completion timestamp (item 6)

Farm Task gets **`completed_at`** (Datetime, read-only).

- It is set on the transition to `Completed`, from the assignment's `completed_at`, or else from now.
- It is set once and never changed by an edit. Every bait rule times from `completed_at`, never from
  `modified`.
- Patch `backfill_farm_task_completed_at` fills it on existing Completed tasks. It takes the latest
  Farm Task Assignment `completed_at`, or else the task's `modified`.

## 4. Triggers (items 1 and 9)

**4.1 A stock move or use of a Pest Control product to a place** raises the right placement task.

- **What counts as a Pest Control product:** an Item whose `item_group` is *Pest Control Products*
  (`masters.PEST_CONTROL_GROUP`).
- **What triggers it.** Two things, both in `tools/stock_inventory.create_stock_entry`, the path
  both MCP and the phone use:
  - `create_stock_entry` accepts optional **`bait_location_doctype`**, **`bait_location`** and
    **`bait_placement`** (`Exterior` by default, or `Interior`).
  - A line whose **target warehouse is linked to an Asset Register row** through
    `Asset Register.warehouse` is treated as a move to that asset.
- **Result.** For each location, one task is created from the Placement template for that side,
  with origin `field_reported`, `bait_product` = the item and `materials_used` = the lines.
- **Already under way.** If an open placement task for the same template and location exists (not
  in a terminal state), the entry is linked to it with a note, and nothing new is created.
- **Refusals and skips.** They never fail the stock entry. The answer's `bait_tasks` block reports
  each one:
  - The template is disabled or missing.
  - An **Interior** placement of a product whose `interior_use_allowed = "No"` is **refused**. The
    task is not created, and the answer says why.
- **Occupied location.** When a placement task is created at an occupied location, an **Occupant
  Notice** task is created with it for the same location, unless the round already has a live one.
  If the Notice template is disabled, the answer says so.

**4.2 Routine housing inspection: "rodent activity seen?"**

- Seeded Inspection Template **Mid-season Habitability** gets a section **Rodent activity** with
  checklist key `rodent_activity_seen`.
- On `submit_inspection_session`, a truthy `rodent_activity_seen` in *any* section of *any* session
  whose location is a Housing Unit or Asset Register row creates a **Placement - Exterior** task at
  that location. Its origin is `field_reported`, and it is linked to the session.
- The same "already under way" and disabled-template rules as 4.1 apply.

**4.3 Pre-occupancy inspection: "bait cleared / no rodent signs".**

- Seeded Inspection Template **Pre-season Cabin Opening** gets a section **Rodent bait cleared** with
  checklist key `rodent_bait_cleared`.
- On submit, the server evaluates the item. It **passes only if the location has no uncleared
  interior bait**, meaning every Interior placement round there ends in a *Completed* Removal and
  Clearance.
- If it fails, a sentence is added to the section's notes. The Housing Inspection is therefore
  written as *Corrective Action Required* whatever the worker ticked.
- The same server check runs for any section carrying that key, so an operator can add it to their
  own templates.

**4.4 Existing sites.** Patch `add_rodent_sections_to_inspection_templates` adds the two sections to
the *live* versions of those two seeded templates when they are missing. It edits the live version in
place, because a section added is not a change to anything already recorded. It never touches a
template an operator authored.

## 5. Before a placement starts (item 4)

In `dispatch.start_farm_task` and `resume_farm_task`, which both the MCP path and the phone go
through:

- A **placement** task whose location is occupied *now* **or** was occupied at creation is refused
  unless the location has a **Completed Occupant Notice** task.
- That notice must be completed at or after the round began, meaning after the location's last
  completed Removal and Clearance.
- The refusal names the Notice task to do, or says to create one.

## 6. Who may do it (item 9)

**Placement and Removal and Clearance** need the applicator qualification:

- **What qualifies:** the worker (Employee) holds a `Certification` with `cert_type` = the configured
  certification type (default **Applicator License**) and `status` Active. The `expiration_date` must
  be empty or today or later. `holder` is matched against the Employee's docname or name.
- **Alternative:** the worker's Employee skill field (if the site has one; see `fieldwork._SKILL_FIELDS`)
  lists the configured applicator skill.
- **Where it's checked:** `claim_farm_task`, `assign_farm_task` and `start_farm_task`, only for tasks
  whose `skill_required` equals the configured applicator skill *and* whose template is a bait
  template. Nothing else changes.
- **The switch:** `pest_require_applicator` (default on).

**Checks and Occupant Notices** are camp maintenance work: the seeded templates say
`camp_maintenance`, and nothing is enforced.

## 7. Bait state per location and the check interval (Tim's two refinements)

New fields on **Housing Unit** and **Asset Register**:

- **`rodent_bait_state`**, a Select: blank, `Active`, `Maintenance` or `Cleared`.
- **`rodent_bait_state_since`** (Datetime).
- **`rodent_bait_last_placement`** (Link Farm Task).

These are updated when a bait task **completes**:

| Completed task | New state |
|---|---|
| Placement | `Active` (a new placement always starts active), and `rodent_bait_last_placement` = the task |
| Check with activity | `Active` |
| Check with no activity | `Maintenance` |
| Removal and Clearance | `Cleared` |

**What counts as activity on a check:** consumption, fresh signs of feeding, or carcasses. The first
of these that is present decides:

1. The completion argument **`bait_activity`** (bool). It is accepted by `complete_farm_task`,
   `complete_task_via_mobile` and the phone route.
2. A ticked checklist item whose name starts with "Rodent activity found".
3. The checklist notes: a consumption note other than none/0, or a carcass count above 0.
4. If none of these says anything, it is **activity**. An unrecorded check keeps the 7-day
   interval.

The task stores what was found in **`bait_activity`** (Select `Activity` or `No activity`).

**Interval for the next check.** The anchor is the location's latest completed Placement or Check
(`completed_at`).

| Condition | Interval |
|---|---|
| **Knockdown:** the anchor is within `knockdown_days` (10) of the round's latest placement, or `rodent_bait_state` is `Active` | `active_interval_days` (7), in season and off season alike |
| **Maintenance**, in season | the tier's `in_season_days` (occupied 7, unoccupied 7) |
| **Maintenance**, off season | the tier's `off_season_days` (occupied 30, unoccupied 30) |

- **Tier:** the location's occupancy *today*.
- **In season or off season:** today falls inside the company's season (§9).
- **All numbers** live on the rule's `extra_parameters` (§8).

## 8. Rules (items 2, 3, 5, 7 and 8)

**Two generic engine additions** that any declarative rule can use:

- **`superseded_by_later_clean.clean_filters`**: a scope-filter list that a *clean* row must also
  match, together with a `date_field` on the clean row.
  - This fixes item 7. Only a **Completed** Removal and Clearance clears: `clean_filters =
    [{template eq …Removal…}, {state eq Completed}]`, `date_field = completed_at`.
  - It replaces the one-field `clean_state_field` match. That match still works when
    `clean_filters` is absent.
- **`notify_roles`** (list) and **`notify_severities`** (default `["Critical"]`) in a rule's
  definition.
  - When set, the alert's push goes to the users holding those roles in the alert's company, instead
    of the supervisor list.
  - The alert records `notify_roles` in its message context.
  - This is item 8: Farm Manager routing is a setting on the rule, not a field on a task.

**`rodent_bait_interior_placement`** (CRULE-2026-0045 on OML; a seed for new sites):

- Declarative, target Farm Task.
- Scope: template = Placement - Interior, state in (In-Progress, Awaiting-Review, Completed).
- Cleared by `clean_filters` = Removal and Clearance, Completed, with `completed_at` later than the
  placement's `completed_at` (or its `modified` while in progress).
- **Severity by tier:** new definition key **`severity_by_field`** =
  `{"field": "occupancy_at_creation", "map": {"Unoccupied": "Warning"}, "default": "Critical"}`.
- `notify_roles = ["Farm Manager"]`.

**`rodent_bait_check_overdue`** (CRULE-2026-0046):

- **Builtin scanner** `rodent_bait_check_overdue`, target Farm Task, implementing §7.
- `extra_parameters`:

```json
{"active_interval_days": 7, "knockdown_days": 10,
 "occupied":   {"in_season_days": 7, "off_season_days": 30},
 "unoccupied": {"in_season_days": 7, "off_season_days": 30}}
```

- It raises at the location's latest bait task. The message states the tier, the season and whether
  the location is in knockdown, active or maintenance. It is Warning when due and Critical when more
  than `active_interval_days` overdue.
- It is silenced by a later completed Check, or by a completed Removal and Clearance.
- `producer_task_template` = Rodent Bait Check. `notify_roles = ["Farm Manager"]`.

**`pest_control_label_fields_missing`** (CRULE-2026-0047). Unchanged in shape. It also reads the five
new label fields (§10) when an interior placement exists for the product.

**`rodent_bait_label_conformance`** (new, builtin scanner, target Farm Task). It checks each
placement against its product (`bait_product`, or else the Pest Control items in `materials_used`):

- An Interior placement of a product whose `interior_use_allowed = "No"` is **Critical**.
- A Removal and Clearance completed earlier than `min_bait_days` after the round's placement is a
  **Warning**. It only fires if no activity was recorded on the last check; otherwise the removal
  looks early for a reason.
- A product with `tamper_resistant_station_required = 1` placed at an occupied location, where the
  task's checklist has no ticked item containing "tamper-resistant", is a **Warning**.
- Distance from the structure and burrow baiting need structured evidence from the phone. They are
  printed on the task (§10) and are **not** machine-checked in this release.

**Pre-occupancy gate** (item 3): new control point **`housing_preoccupancy_bait_clearance`** in
`enforcement.CONTROL_POINTS`.

- It is evaluated in `create_housing_assignment` before anything is written.
- **Finding:** the unit has uncleared interior bait (§4.3's test).
- **Modes:** Advisory files an alert and allows the assignment. Enforced refuses it and names the
  Removal and Clearance task to complete.
- **Seed:** disabled (Off).
- `propose_compliance_rule` and `update_compliance_rule` now declare **`control_point`** and
  **`enforcement_mode`** in their schemas. The handler already read them. This is how Tim sets the
  gate.

## 9. Settings (the open decisions, each configurable)

**ERPNext MCP Settings**, section *Rodent Bait Program*:

| Field | Default | Meaning |
|---|---|---|
| `pest_applicator_skill` | `applicator` | `skill_required` value that means "needs the applicator qualification" |
| `pest_applicator_certification` | `Applicator License` | `Certification.cert_type` that qualifies |
| `pest_require_applicator` | 1 | §6 on/off |
| `pest_crew_skill` | `camp_maintenance` | skill written on seeded Check and Notice templates |
| `pest_people_work_here_types` | `Barn`, `Shop`, `Kitchen`, `Bath House`, `Toilet-Shower`, `Storage`, `Cold Storage` | Housing Unit `unit_type`s and asset types treated as people-work-here, one per line. **Strict by default** (Tim to confirm). |
| `pest_alert_role` | `Farm Manager` | Written into the seeded rules' `notify_roles`. The rules can also be edited directly. |

**Company** (Custom Fields, per company):

- `pest_season_start` and `pest_season_end`, Data, `MM-DD`, for example `03-01` and `10-31`.
- A window may wrap the year end.
- If either is blank, the season is **03-01 → 10-31**.
- Settable with `update_company`.

## 10. Per-product label fields (item 5)

New Item compliance fields, shown for pesticide groups:

| Field | Type |
|---|---|
| `tamper_resistant_station_required` | Check |
| `max_distance_from_structure_ft` | Float |
| `burrow_baiting_allowed` | Select: blank, Yes, No |
| `min_bait_days` | Int |
| `interior_use_allowed` | Select: blank, Yes, No |

- **Accepted by** `create_item`, `update_item` and the mobile `create_item` (fill-blanks rules as
  before).
- **Filled** by `register_product_label` from the phone's `extracted_fields`, or else from the
  **server's own reading of the OCR**: `document_intel.bait_label_facts(ocr_text)`. The server
  reading is:

| Label wording | Field value |
|---|---|
| "tamper-resistant bait station(s)" required where children, pets … may reach | `tamper_resistant_station_required = 1` |
| "within N feet of" a building or structure | `max_distance_from_structure_ft = N` (the smallest N) |
| "do not place … in burrows" / "burrow baiting" prohibited | `burrow_baiting_allowed = No`; "burrow" as an allowed placement gives `Yes` |
| "at least N days" / "N week(s)" / "for N days" near bait/fresh bait | `min_bait_days = N` (weeks × 7, the smallest) |
| "inside" or "in and around" buildings or structures | `interior_use_allowed = Yes`; "outdoor use only" / "exterior only" gives `No` |

- A placement task created with a product gets the product's constraints prepended to its
  instructions as one line, for example "Label (PROWLER Place Pacs): tamper-resistant stations
  required · within 100 ft of structures · no burrow baiting · keep bait out at least 7 days".

## 11. Camp maintenance filing (item 9)

- **`get_compliance_calendar`** returns a **`camp_maintenance`** block. It holds the Housing-category
  alerts, which now include the three bait rules (their category stays `Housing`), plus **open bait
  tasks** and the **next check due per location** from §7.
- **Audit packet**, `housing` section: a **`pest_control`** list of the bait tasks in the period.
  Each entry has template, location, state, `occupancy_at_creation`, `completed_at`, `bait_product`
  and `bait_activity`, and they are grouped by location.
- **The five seeded templates** have `task_type` values that file under housing maintenance:
  - Removal and Clearance: `Housing-Cleanup`
  - the others: `Other`
  - All five carry regimes `OR-OSHA` and `Internal`.

## 12. Phone (fafo_ios, SERVER_CHANGES §50)

1. **Rodent Bait Check completion:** a required **"Rodent activity found?"** Yes/No control. It is
   sent as `bait_activity`. An older server ignores it; the checklist and notes fallbacks still apply.
2. **Asset registration and editing** for building types (Cabin, House, Housing Unit, Storage, Cold
   Storage):
   - toggles **"Occupied"** and **"People work here"**, sent as `occupied` and `people_work_here`;
   - the asset card shows occupancy (with its source) and `rodent_bait_state` when present.
3. **Stock move or issue of a Pest Control product:** an optional **"Placing bait at"** location plus
   Exterior/Interior. It sends `bait_location_doctype`, `bait_location` and `bait_placement`, and
   shows the answer's `bait_tasks` block as plain sentences.
4. **Start refusals** (a missing notice, a missing qualification) are already shown as text. Nothing
   new is needed.

## 13. Not in this release

- Enabling any template or rule.
- Registering Mill Creek cabins or houses. Tim adds them as Cabin/House assets.
- Machine-checking distance from structures and burrow placement, which needs structured
  per-station evidence.
- Changing OML's drafted templates. Their Check template still says `applicator`; Tim or the office
  changes it to `camp_maintenance` with `update_farm_task_template`.

## 14. Settled during implementation (v0.203.0)

These points were decided while building and are recorded here so the contract matches the code.

- **§2 Occupancy.** An Asset Register row whose `current_state` is `occupied` (the asset's existing
  `mark_occupied` action) also counts, reported as source `manual_flag`. Cabin and House assets get
  the Housing Unit state machine.
- **§2 Editing the flags on a building asset.** New mobile route **`/mobile/set_building_occupancy`**
  takes `asset_name`, `occupied` and `people_work_here`.
  - Who: the location role.
  - Where: building asset types only.
  - Answer: `{asset_name, occupied, people_work_here, occupancy}`.
  - `get_asset_detail` returns `occupied`, `people_work_here`, `occupancy`, `rodent_bait_state` and
    `rodent_bait_state_since` for building types.
  - A Housing Unit's flags are set with `create_housing_unit` / `update_farm_location` (Housing Unit
    only).
- **§4.2–§4.3 Inspection sections.** Both sections are **optional**, so a phone that has never seen
  them still files the visit. The clearance is judged anyway: if the template carries
  `rodent_bait_cleared` and the phone did not send it, the finding goes onto the first submitted
  section that files a Housing Inspection.
- **§8 Where the routing and tier settings live.** `notify_roles`, `notify_severities` and
  `severity_by_field` live in the rule's **`extra_parameters`**, edited with
  `update_compliance_rule(extra_parameters=…)`.
- **§8 The check-overdue seed.** `rodent_bait_check_overdue` links `producer_task_template` only when
  the Check template exists at seed time.
- **§6 Where the applicator check runs.** It is also applied on `resume_farm_task`.

## 15. Superseded in v0.205.0 — the settings are data now

docs/design/programs_and_field_kinds.md Part A moved §9's settings onto records:

- Applicator skill and certification → `required_certification` on the template (and its tasks).
- People-work-here types → `people_present` on Farm Asset Type.
- Alert role → each rule's `notify_roles`.
- Season → Company `season_start` / `season_end`.
- Check cadence → the generic `grouped_cadence` rule option.

Behaviour is unchanged. Patch `move_rodent_settings_to_data` carries the stored values across.
