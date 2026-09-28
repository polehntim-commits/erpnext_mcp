# Configure, not code: programs as data, and every field kind on the phone — contract (frozen, v0.205.0)

Tim approved this on 2026-09-28 ("yes please"). **Goal:** ERPNext templates built through MCP must be
usable on iOS with no code change, for any farm, crop or program. OML runs v0.204.0.

This release has three parts:

- **Part A** turns the rodent-specific settings into generic data. Rodent behaviour stays
  **identical**.
- **Part B** widens the form vocabulary (docs/design/form_schema_and_labels.md §1) so MCP rarely has
  to refuse a template, and adds capability negotiation with graceful degradation.
- **Part C** puts inspection sessions on the phone.

# Part A — programs are data

## A1. Required certification on any template

**`required_certification`** (Data) is added to **Farm Task Template**, **Inspection Template** and
**Farm Task**. A task snapshots it at creation.

**What satisfies it** (`qualifications.qualification(employee, requirement)`), either one of:
- a `Certification` held by the worker whose `cert_type` **or** `cert_name` equals the requirement
  (case-insensitive), with `status` Active and `expiration_date` empty or today or later; or
- the worker's Employee skill field (`fieldwork._SKILL_FIELDS`, where the site has one) listing it.

**Where it is enforced**, the same way for every template:
- `claim_farm_task`, `assign_farm_task`, `start_farm_task` and `resume_farm_task`;
- `start_inspection` and the mobile inspection submit, for a session that names a worker.

The refusal names the requirement and what would satisfy it.

**Rodent migration (identical behaviour).**
- The old rule enforced a `Certification` of type `pest_applicator_certification` for bait tasks
  whose skill was `pest_applicator_skill`, when `pest_require_applicator` was on.
- Patch `move_rodent_settings_to_data` writes that type into `required_certification` on:
  - every Farm Task Template whose `skill_required` equals the old applicator skill;
  - every open Farm Task raised from one of them.
- The skill-based check in `rodent_bait.refuse_unqualified` is removed. The generic check replaces it.
- A requirement nobody set enforces nothing, as before.

## A2. Alert routing lives only on the rule

- **Removed:** `pest_alert_role`.
- `extra_parameters.notify_roles` / `notify_severities` on each Compliance Rule (v0.203.0) is the
  only place alert routing lives.
- The seeded bait rules carry `["Farm Manager"]` literally.

## A3. "People live or work here" is a flag on the type

- **New field:** **`people_present`** (Check) on **Farm Asset Type**: "People live or work here —
  treated as occupied".
- **How occupancy reads it:**
  - An Asset Register row is occupied by type when its `asset_type` has `people_present`.
  - A Housing Unit is occupied by type when the Farm Asset Type **named the same as its
    `unit_type`** has `people_present`. `unit_type` is a fixed Select, and the register of types is
    the Farm Asset Type table.
- **Kept unchanged:** the per-place **`occupied`** flag, the per-place `people_work_here` flag, the
  asset state `occupied`, and the active Housing Assignment.
- **Migration:** `pest_people_work_here_types` → for each name:
  - an existing Farm Asset Type gets `people_present = 1`;
  - a missing one is **created disabled** with `people_present = 1`. It carries the flag for Housing
    Unit types (Barn, Shop, Kitchen, Bath House, Toilet-Shower) without adding a picker entry.
- **Types not in the old list** (Cabin, House, Housing Unit) stay unflagged. An empty cabin is
  still unoccupied.
- **Occupancy moves to `occupancy.py`**, a generic module. `rodent_bait.occupancy` stays as an alias.

## A4. Seasonal, tiered, state-driven cadence is a generic rule option

A declarative rule may carry **`extra_parameters.grouped_cadence`**:

```json
{"group_by": "location",
 "anchor_filters": [{"field": "template", "op": "in", "value": ["Rodent Bait Placement - Exterior", "Rodent Bait Placement - Interior", "Rodent Bait Check"]}, {"field": "state", "op": "eq", "value": "Completed"}],
 "start_filters":  [{"field": "template", "op": "in", "value": ["Rodent Bait Placement - Exterior", "Rodent Bait Placement - Interior"]}, {"field": "state", "op": "eq", "value": "Completed"}],
 "end_filters":    [{"field": "template", "op": "eq", "value": "Rodent Bait Removal and Clearance"}, {"field": "state", "op": "eq", "value": "Completed"}],
 "date_field": "completed_at",
 "state": {"field": "bait_activity", "active_values": ["Activity"], "blank_is_active": true},
 "knockdown_days": 10,
 "active_interval_days": 7,
 "tier": "occupancy",
 "intervals": {"occupied":   {"in_season_days": 7, "off_season_days": 30},
               "unoccupied": {"in_season_days": 7, "off_season_days": 30}},
 "season": "company"}
```

**Meaning.** The rule's target rows are grouped by `group_by`, meaning by the Dynamic Link pair
`location_doctype` / `location` when `group_by` is `location`. For each group:

1. **The round:** rows after the latest row matching `end_filters`.
2. **The anchor:** the round's latest row matching `anchor_filters` (by `date_field`). **The start:**
   the latest row matching `start_filters`. No start means nothing is out, and nothing is raised.
3. **The state** is **active** in either of two cases:
   - the anchor is within `knockdown_days` of the start;
   - the anchor's `state.field` is in `active_values` (or blank, with `blank_is_active`).

   Otherwise the state is **maintenance**.
4. **The interval:**

   | Condition | Interval |
   |---|---|
   | active | `active_interval_days` |
   | maintenance | `intervals[tier][in_season_days or off_season_days]` |

   `tier`:

   | value | tiers |
   |---|---|
   | `occupancy` | `occupied` / `unoccupied`, from `occupancy.occupancy()` today |
   | `none` | one tier, `default` |
   | `field:<name>` | the anchor row's value |

   `season: "company"` reads the anchor's company window (A5). `none` means always in season.
5. **It raises** at the anchor row when `anchor + interval ≤ today`, with a message context carrying
   `tier`, `in_season`, `phase` (knockdown/active/maintenance), `interval_days`, `due_date` and
   `days_overdue`. It is Warning when due, and Critical when more than `active_interval_days`
   overdue, unless the rule's severities say otherwise.

**The rodent seed.**
- `rodent_bait_check_overdue` becomes a **declarative** rule with exactly the block above.
- The builtin scanner name `rodent_bait_check_overdue` stays registered and runs the same primitive
  with those defaults, so a site whose row still names it behaves identically.
- Patch `move_rodent_settings_to_data` converts **System-authored** rows that name the builtin.
- OML's CRULE-2026-0048 is AI-proposed and left alone. Tim can switch it with `update_compliance_rule`.

**The location's `rodent_bait_state` mirror** (Active/Maintenance/Cleared) is still written for the
phone's display. It is generalised as `program_state`, keyed by program, in A6.

## A5. The season is the company's, generically named

- **New Company fields:** **`season_start` / `season_end`** (MM-DD).
- **Migration:** patch `move_rodent_settings_to_data` copies `pest_season_start` / `pest_season_end`
  into them, then removes the two old Custom Fields.
- `update_company` takes `season_start` / `season_end`. It still accepts the `pest_*` names as
  aliases.
- **Default:** 03-01 → 10-31.

## A6. A program is a bundle

A **program** is a JSON document:

```json
{"program": "rodent_bait", "title": {"en": "Rodent bait at housing and buildings", "es": "…"},
 "version": 1, "requires_app": "0.205.0",
 "task_templates": [ …full Farm Task Template bodies… ],
 "inspection_templates": [ …bodies… ],
 "compliance_rules": [ …rule specs… ],
 "uoms": [ {"uom_name": "Place Pac", "must_be_whole_number": 1, "aliases": ["pac", "pacs"]} ],
 "uom_contexts": [ {"context_name": "Bait", "applies_to": "Count", "uoms": [...] } ],
 "asset_types": [ {"type_name": "Cabin", "people_present": 0}, … ],
 "item_groups": [ "Pest Control Products" ]}
```

**Tools:**

| Tool | Kind | What it does |
|---|---|---|
| **`list_programs`** | read | The programs shipped with the app (`erpnext_mcp/programs/*.json`), and for each whether it is installed on this site: each part present, and whether any part has been edited since. |
| **`export_program`** | read | Builds a bundle from **this site's** records: `program`, `title`, and the part names (template names, rule_ids, UOM names, context names, asset type names, item groups). This is how the rodent program as tuned at OML is carried to another farm. |
| **`import_program`** | write, default off | Takes `bundle` (or `program` for a shipped one) and `dry_run` (default **true**). It is create-only: a part that exists is reported and left alone. Imported templates and rules arrive **disabled** and unapproved. It answers `{created, present, refused}`. |

**Shipped:** `programs/rodent_bait.json`, the rebuilt v0.204.0 templates plus the five rules, units,
the Bait context and the asset-type flags. The install-time seeders stay, and the program file is
generated from the same Python definitions, so they cannot drift (a test asserts it).

**The phone's display mirror:** Housing Unit and Asset Register gain `program_state` (JSON,
read-only) `{program: {state, since, last_start}}`. `rodent_bait_state` is kept and still written.

## A7. The settings section goes

The ERPNext MCP Settings fields `pest_applicator_skill`, `pest_applicator_certification`,
`pest_require_applicator`, `pest_crew_skill`, `pest_alert_role` and `pest_people_work_here_types`
are **removed**, along with their section. Patch `move_rodent_settings_to_data` reads their stored
values from `tabSingles` **before** anything else, then does A1, A3, A4 and A5, and is idempotent.

# Part B — every field kind on the phone

## B1. Kinds: Frappe types 1:1, plus farm kinds

**Schema version 2.** Every v1 kind stays valid, with the same meaning. The new kinds, with Frappe
fieldtype aliases accepted as `type` (case-insensitive):

| kind | Frappe alias | answer |
|---|---|---|
| `data` | Data | string |
| `small_text` | Small Text | string |
| `long_text` (v1) | Long Text, Text | string |
| `text_editor` | Text Editor | HTML string (sanitised on save) |
| `select` (v1) | Select | value |
| `link` (v1, widened) | Link | docname; **any doctype on the phone-searchable list** (B3) |
| `dynamic_link` | Dynamic Link | `{doctype, name}`; `link.doctype_from` names the answer that holds the doctype |
| `date` / `datetime` (v1) | Date / Datetime | ISO |
| `time` | Time | `HH:MM[:SS]` |
| `duration` | Duration | seconds (int) |
| `check` (v1) | Check | bool |
| `int` | Int | int |
| `float` | Float | number (`number` stays the v1 spelling) |
| `currency` | Currency | number; `currency` attr (ISO) optional |
| `percent` | Percent | 0–100 |
| `rating` | Rating | int 1…`max` (default 5) |
| `table` | Table | list of rows; `fields` are the columns; rendered as a grid (`group` stays the card-per-row spelling) |
| `attach` | Attach | file references (`min_count`/`max_count`) |
| `attach_image` | Attach Image | = `photo` |
| `signature` (v1) | Signature | file reference |
| `geolocation` | Geolocation | GeoJSON Point `{type: "Point", coordinates: [lon, lat]}`; `gps` stays the "lat,lon" v1 spelling |
| `barcode` | Barcode | `{code, symbology?}` |
| `color` | Color | `#RRGGBB` |
| `password` | Password | **refused**: a form never collects a secret |
| `html` | HTML | display only (`label`/`help` as sanitised HTML) |
| `read_only` | Read Only | display only; `source`: `answer:<key>` or `context:<key>` |

**Farm kinds:**

| kind | attrs | answer |
|---|---|---|
| `scan` | `scan.kinds`: any of `asset_tag`, `upc`, `badge`, `qr`, `any`; `link` optional (resolve to a record) | `{code, kind, doctype?, name?}`. The server resolves the record on completion when `link` is set |
| `map_area` | `min_points` (default 3) | GeoJSON Polygon |
| `timer` | `purpose` label (REI, runtime …) | `{started_at, stopped_at, seconds}` |
| `audio_note` | `max_seconds` | file reference |
| `document` | `document.source`: `item_label` (with `from_field`: a link to Item), `sop`, `file` (with `file`), `url` | display only; opening it is recorded where it is a label (§4 `record_label_viewed`) |
| `computed` | `formula`: arithmetic over answer keys (`+ - * / ( )`, numbers, `min`, `max`, `round`, `sum(<table>.<column>)`); `precision` | number, **recomputed by the server** and stored. The phone shows it live |

Every v1 attribute still applies (`required`, `show_if`, `required_if`, `min`/`max`/`step`, `uom`,
`min_count`/`max_count`, `help`, EN/ES labels). New attributes:

- **`safety_critical`** (bool). See B4.
- **`fallback`**: `text` (default) or `photo`. See B4.
- **`max`** on `rating`, and `currency` on `currency`.

## B2. One renderer

- Farm Task forms, inspection section forms and wizard step forms are the same vocabulary. The phone
  draws them with one renderer.
- The wizard `form` mapping (v0.204.0 §1.6) gains:

  | wizard type | §B1 kind |
  |---|---|
  | `qr_scan` | `scan` (any) |
  | `audio_note` | `audio_note` |
  | `datetime` | `datetime` (the v1 mapping lost the time) |

## B3. Search-link to any record

- **New setting:** **`phone_link_doctypes`** on ERPNext MCP Settings (Small Text, one per line). It
  is **security configuration**, not program configuration: the doctypes a phone form may link to
  and search.
  - **Default:** Item, Asset Register, Housing Unit, Employee, Field, Parcel, Irrigation Zone,
    Warehouse, Supplier, Customer, Crop, UOM, Farm Task, Certification.
  - A `link`/`dynamic_link` naming a doctype outside it is a validation **error**, naming the
    setting.
- **New mobile route `search_link(doctype, txt, filters, limit)`** (open on enrolment):
  - the doctype must be on the list;
  - `filters` are equality only;
  - results are entity-scoped where the doctype has a company column (`company` / `owning_entity`);
  - it answers `[{name, title, description}]`, using the doctype's `title_field` and
    `search_fields`;
  - Employee answers `employee_name` only.

## B4. Capability negotiation and graceful degradation

**Where the phone reports.** New mobile route **`report_device_capabilities(device_identifier,
app_version, schema_version, field_kinds)`**. It writes onto the caller's **Mobile Device
Enrollment** row:
- `app_version`, `form_schema_version`, `field_kinds` (JSON);
- `capabilities_reported_at`.

The phone calls it after sign-in and on each launch, and it is cheap.

**What the server assumes:** a device that never reported is **schema 1**, which is exactly the
v0.204.0 kinds.

**What MCP shows:**
- **`list_device_capabilities(company?)`** (read) lists every active device: user, device name, app
  version, schema version, and the kinds missing against schema 2.
- The template tools (`create_/update_farm_task_template`, `create_/update_inspection_template`,
  `preview_farm_task_template`, `preview_inspection_template`, `get_farm_task_template.problems`)
  **validate against the devices actually enrolled** for the template's company (every company when
  it has none). For each kind some device cannot render, they report which devices and app versions
  need updating.
- It is a **warning**, not a refusal, unless the field is **`safety_critical`**. Then the template is
  still saved, but the problem is marked `blocking_on: [devices]`.

**Graceful degradation on the phone.** An unknown kind renders as its `fallback`:
- `text`: a labelled text box;
- `photo`: a labelled photo capture;
- display-only kinds render as a note.

Each carries a "needs app update" note. The answer is sent as `{"fallback": "text", "value": …}` or
`{"fallback": "photo", "files": [...]}`. The server accepts a fallback answer for any
non-safety-critical field and stores it as given.

**Safety-critical fields.**
- `complete_task_via_mobile` and the mobile inspection submit accept **`client_capabilities`**
  `{schema_version, field_kinds}`.
- A completion is **refused** when a *visible* `safety_critical` field's kind is missing from them
  (or from schema 1, when they are absent). The sentence says "update the app".
- The phone also blocks it locally.

## B5. Validation in one place

- `form_schema.validate` understands B1.
- `formula` is parsed by a whitelist AST reader; anything else is an error.
- `password` is an error.
- A `document.from_field`, a `dynamic_link.doctype_from` or a `computed` reference must name an
  existing field.

# Part C — inspection sessions on the phone

New mobile routes:

- **`get_inspection(session)`** (the session's entity must be the caller's) returns the session plus
  **sections**:

  ```
  [{section_name, section_description, required, renderer_hint, evidence_contract,
    produces_record_doctype, form: [fields], checklist_items}]
  ```

  - `form` is the section's `field_prompts` in the §B1 vocabulary.
  - A legacy section with no prompts gets `form` built from its `evidence_contract.checklist_items`
    and measurements, as `check` / `float` fields.
  - It also returns `form_context`: location, asset type, occupancy.
- **`submit_inspection(session, section_submissions, client_capabilities)`** (the dispatch role, or
  the session's worker) runs the existing `submit_inspection_session`. Each section is
  `{section_name, answers, evidence_file_tokens, notes, skipped, signature_file}`, and the answers
  fill `checklist_values` / `measurements` by key.
- **`list_my_inspections(state?)`** lists the caller's open and recent sessions.

With these the rodent sections ("Rodent activity seen?", "Rodent bait cleared") work on the phone.

# Counts, routes, deploy

- **Tools:** 932 total, 461 read, 471 write. New: `list_programs`, `export_program`,
  `list_device_capabilities` (read), `import_program` (write).
- **Mobile routes:** 141. New: `search_link`, `report_device_capabilities`, `get_inspection`,
  `submit_inspection`, `list_my_inspections`.
- **Patches:** `move_rodent_settings_to_data`.
- **iOS:** SERVER_CHANGES §52.
