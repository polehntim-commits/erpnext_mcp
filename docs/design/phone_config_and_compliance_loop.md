# Phone config and the compliance loop — contract (frozen, v0.207.0)

Tim, 2026-09-28:

> "Yes please. And make sure it will be bullet prof. Also Would this help us close
> the loop with compliance tasks that pop up later?"

**Answer to the question: yes, and §4 is how.**
- Every rule that raises work has to name a way for a phone to open that work, and a place where
  the right person sees it.
- MCP flags every rule that is missing either one.
- Each person gets a **Compliance inbox** of due, overdue and blocked items.

**Scope:**
- wizards as versioned data (§2);
- server-driven tiles (§3);
- the compliance loop (§4);
- label-driven compliance (§5);
- the earlier phone items (§6).

**Rules for everything here:**
- The phone downloads configuration, never code.
- It uses Apple's on-device stack only.
- Nothing publishes itself.

---

## 1. One lifecycle for phone configuration

### 1.1 The record

New DocType **`Farm Config Version`** holds three kinds of body: **Wizard**, **Tile** and
**Label Profile**. It is one table, not three, to avoid table sprawl. Each row is one immutable
version of one key.

| field | meaning |
|---|---|
| `config_kind` | `Wizard` / `Tile` / `Label Profile` |
| `config_key` | lower_snake_case, unique within a kind (`accident_investigation`, `compliance_inbox`) |
| `version` | int. The docname is `"<kind_slug>:<key>@<version>"` (`wizard:accident_investigation@2`), and that string is the **`config_version`** everything records |
| `status` | `Draft` / `Staged` / `Published` / `Superseded` / `Retired` |
| `schema_version` | the body schema (1) |
| `body_json` | the body (§2.1, §3.1, §5.1) |
| `body_hash` | sha256 of the canonical body. Written at insert; checked on every save |
| `title` | from the body, for lists |
| `rollout_flag` | the Farm Feature Flag that gates a Staged version (§1.3) |
| `validation_json` | the last validator report: `{errors, warnings, checked_at}` |
| `notes` | why this version exists |
| `authored_by` | Operator / AI-proposed / System |
| `staged_by/on`, `published_by/on`, `retired_by/on` | who changed the status, and when |
| `change_note` | the reason given for the last status change (required) |

`track_changes = 1`, so Frappe keeps a **Version** row for every save. Every MCP action also writes
an MCP Action Log row. Together these are the audit of who changed what.

### 1.2 Immutability

- **A Draft is the only row whose body may change.** From `Staged` on, the controller refuses any
  change to `body_json`, `config_key`, `version` or `config_kind`, and `body_hash` must still match.
- **To change a published thing,** create a new Draft at the next version.
- **Allowed status moves:**
  - Draft → Staged → Published;
  - Draft → Published, which is allowed but still passes every check;
  - Published → Superseded, done automatically when another version is published;
  - any status → Retired;
  - a Superseded or Retired version → Published again. Its content is unchanged, so this is safe;
    it is how rollback works.
- **A Draft cannot be served.** It can be previewed.

### 1.3 Serving and staged rollout

The version a user gets for a key:
1. the **Staged** version, if one exists and its `rollout_flag` resolves to true for that user;
2. otherwise the **Published** version;
3. otherwise nothing. A retired key is gone.

**How staging works:**
- `stage_phone_config` creates or updates the flag `rollout_<kind_slug>_<key>`, with the given
  users, roles or companies. For example `users: [tim@…]` puts Tim first.
- Widening the rollout is just changing the flag (`set_feature_flag`).
- `publish_phone_config` then makes it Published for everyone and switches the flag off.

**Flags gain a user target.** Farm Feature Flag gets a **`users`** field, one per line. Resolution
becomes, most specific first:

> user, then company+role, then company, then global+role, then global

### 1.4 Validation (on create, update, stage and publish)

Every kind runs the same pipeline. Its result is stored in `validation_json`.

| check | error / warning |
|---|---|
| body JSON parses, is ≤ 64 KB, and `schema_version` is known | error |
| forms pass `form_schema.require_valid` (v2), with every condition parsed by the whitelisted parser | error |
| **device capability**: `device_capabilities.problems(fields, company)` over the enrolled devices of the audience | a missing kind is a warning (it falls back). A missing **safety-critical** kind is an error on publish |
| **audience resolves to real people**: at least one active user with a live Mobile Access Grant matches | error (a tile nobody can see is a mistake) |
| targets exist and are enabled (wizard key, templates, rule, report id, query id, doctype) | error |
| queries and handlers come from the allowlists (§2.3, §3.3) | error |
| icons come from the allowlist (§3.4) | error |
| EN and ES: every visible title, label and option has `es` | warning on a Draft, **error on publish** unless `allow_english_only: true` (recorded) |
| limits (§1.6) | error |

`preview_*` runs the validator and returns the report without writing anything.

### 1.5 Generic lifecycle tools (MCP)

| tool | kind | what it does |
|---|---|---|
| `list_phone_configs(kind?, key?, status?)` | read | versions, with status, audience, rollout and the last validation |
| `get_phone_config(kind, key, version?)` | read | the body. Default: the version in force |
| `stage_phone_config(kind, key, version, users?, roles?, companies?, change_note)` | write | Draft → Staged. Sets the rollout flag |
| `publish_phone_config(kind, key, version, change_note)` | write | → Published. The previous Published version becomes Superseded; the rollout flag goes off |
| `rollback_phone_config(kind, key, change_note)` | write | **one step.** The Published version becomes Superseded, and the most recently Superseded one becomes Published again |
| `retire_phone_config(kind, key, change_note)` | write | this is "disable": every version of the key is Retired and it stops being served. Re-publishing a version enables it again |

- **Every write** is off by default and needs System Manager or Farm Manager.
- **`change_note`** is required.
- **Idempotency:** a repeated call with the same arguments is a success that says so.

### 1.6 Limits

| thing | limit |
|---|---|
| body size | ≤ 64 KB |
| wizard | ≤ 20 steps, ≤ 40 fields a step, ≤ 150 fields in total |
| tiles | ≤ 24 served per surface |
| lists | badge and list query rows ≤ 50 |
| submitted answers | ≤ 256 KB (files go through `stage_file_chunk`) |
| mobile routes | new reads use `READ_LIMIT`; new writes use `WRITE_LIMIT` |
| MCP config writes | ≤ 30 a minute per caller |
| a user's tile badges | cached for 60 s |

---

## 2. Wizards as versioned data

### 2.1 The body (Wizard, schema 1)

```json
{"schema_version": 1, "key": "near_miss",
 "title": {"en": "Report a near miss", "es": "Reportar un casi accidente"},
 "description": {"en": "…", "es": "…"}, "category": "Safety", "icon": "exclamationmark.shield",
 "required_roles": [],
 "steps": [
  {"key": "what", "title": {"en": "What happened", "es": "Qué pasó"},
   "form": [ <form_schema v2 fields> ],
   "next": [{"if": {"field": "injury", "equals": true}, "go": "injury"}, {"go": "where"}]},
  {"key": "injury", "show_if": {"field": "injury", "equals": true}, "form": [...], "next": [{"go": "where"}]},
  {"key": "where", "form": [...], "next": []}],
 "submit": {"handler": "create_accident_report",
            "map": {"what_happened": "description", "when": "incident_datetime"},
            "context": {"incident_type": "Near Miss"}},
 "success": {"en": "Thanks — filed.", "es": "Gracias — enviado."}}
```

- **Forms:** each step's `form` is the **same form_schema v2** that task and inspection templates
  use, rendered by the same phone `FormRenderer`. The legacy 14-type Wizard Field vocabulary is
  retired for new wizards.
- **Branching:** `next` is an ordered list of `{if?, go}`. The first `if` that holds wins, and an
  entry with no `if` is the default. `[]` means this is the last step. A step's `show_if` hides it.
- **Conditions** are form_schema conditions, evaluated by the whitelisted parser.
- **Field keys are unique across the whole wizard**, because the answers are one flat object.
- **Validation also checks:**
  - every `go` names a step;
  - there are no cycles;
  - every step is reachable;
  - the first step is `steps[0]`.

### 2.2 Serving, snapshots and submitting

- **`get_wizard_definition(wizard, version?)`** (existing route, same response shape) serves the
  version in force (§1.3) from Farm Config Version, adding `config_version`. With `version`, it
  serves that exact version, provided it is not Retired, so a wizard the phone started keeps its
  definition.
  - The legacy per-field shape is still generated for 0.20.x apps.
  - A key with no config row falls back to the legacy Wizard Definition, until the patch migrates
    it (§7).
- **`list_wizard_definitions`** is unchanged in shape, and includes `config_version`.
- **`submit_wizard_via_mobile(wizard, answers, config_version?, client_reference?, context?)`**:
  1. Loads **the version the phone started with** (`config_version`), else the one in force.
  2. Refuses a Retired key: "this form was withdrawn — nothing was filed".
  3. **Enforces `required_roles`.** Today's `required_role` is never enforced; that stops.
  4. Re-runs `form_schema.check_answers` for the steps on the path the answers take. A missing
     `safety_critical` answer is refused in a sentence.
  5. Maps the answers through `submit.map` onto the **allowlisted handler** (§2.3).
  6. **Idempotent on `client_reference`** (a UUID; required when the phone queues a submit): a
     repeat returns the first result, with `duplicate: true`.
  7. `context` may carry only `source_alert` and `location_doctype` / `location` (§4). Any other key
     is dropped.
  8. The result records `config_version`.

### 2.3 Submit handlers — an allowlist, not the route table

`wizard_handlers.HANDLERS` maps a name to `{route, params, required, roles?}`.

| handler | notes |
|---|---|
| `create_accident_report` | |
| `create_discipline_record` | HR roles |
| `register_asset` | |
| `create_employee` | HR roles |
| `start_inspection` | |
| `report_field_task` | |
| `report_asset_issue` | |
| `start_template_task` | §4.4 |

**What the validator checks:**
- the handler is on the list;
- every required parameter is covered by `map`, a field key of the same name, or `context`;
- a field that maps to nothing the handler accepts is a warning ("will not be filed").

No wizard can name arbitrary code or an arbitrary route.

### 2.4 Wizard MCP tools

| tool | kind | what it does |
|---|---|---|
| `create_wizard_definition(key, body, notes)` | write | a Draft at version 1 (refused if the key exists) |
| `update_wizard_definition(key, body, notes)` | write | a Draft at max+1. Replaces an existing Draft's body in place, so there is at most one Draft |
| `preview_wizard(key?, version?, body?, answers?, language?, as_user?)` | read | the validator report, each step as the phone renders it (EN and ES, `for_phone`), the path `answers` would take, what the handler would receive, and device problems for the audience |

Enable is `publish_phone_config`; disable is `retire_phone_config`.

---

## 3. Server-driven tiles

### 3.1 The body (Tile, schema 1)

```json
{"schema_version": 1, "key": "compliance_inbox", "surface": "today",
 "title": {"en": "Compliance inbox", "es": "Pendientes de cumplimiento"},
 "subtitle": {"en": "Due, overdue, blocked", "es": "Por vencer, vencidos, bloqueados"},
 "icon": "tray.full", "order": 10,
 "target": {"kind": "report", "report": "compliance_inbox"},
 "audience": {"roles": [], "companies": [], "skills": [], "certifications": [], "users": []},
 "badge": {"query": "compliance_inbox", "params": {}},
 "show_if": {"flag": null, "season": null, "occupancy": null, "asset_types": []},
 "min_app_version": "0.21.0"}
```

**Fields:**
- `surface` is `today`, `work` or `asset_scan`.
- `order` is ascending. Ties sort by key.
- **Audience:** an empty list means no restriction on that axis, and the axes AND together. The
  whole audience must still resolve to at least one real person (§1.4).
- **`show_if` conditions:**
  - `flag` is a flag key that must resolve true;
  - `season` is `in` or `off`, from `occupancy.in_season` for the user's company;
  - `occupancy` (`Occupied` / `Unoccupied`) and `asset_types` apply to `asset_scan` only, and are
    evaluated against the scanned asset.
- `min_app_version` is compared as semver. A phone below it never receives the tile.

### 3.2 Targets

| `target.kind` | keys | what the phone does |
|---|---|---|
| `wizard` | `wizard` | opens the wizard (§2) |
| `task_template` | `template` | `start_template_task` (§4.4), then opens the completion |
| `inspection_template` | `template` | `start_inspection`, at the scanned asset on `asset_scan`, else asks for a location |
| `report` | `report` ∈ `compliance_inbox`, `my_tasks`, `available_tasks`, `my_inspections`, `compliance`, `farm_dashboard`, `my_feedback` | opens that native screen |
| `list_query` | `query` (§3.3) plus `params` | a list of the query's rows; each row opens its document |
| `document` | `doctype` ∈ Farm Task, Inspection Session, Asset Register, Item (label), App Feedback; plus `name` | opens that record's screen |

**Target validation:** the target exists and is enabled; its form is renderable; and the tile's
audience may use it (a template's `required_certification` is held by someone in the audience).

### 3.3 Queries — an allowlist (`tile_queries.QUERIES`)

Each query:
- is a function `(user, company, params) → {count, rows≤50, tone}`;
- has a declared params schema;
- may carry a role gate.

`tone` is `neutral`, `attention` or `critical`.

| query | rows | gate |
|---|---|---|
| `my_tasks_open` | my open tasks | |
| `my_tasks_overdue` | my overdue tasks | |
| `available_tasks` | tasks I may claim | |
| `compliance_inbox` | §4.3: due + overdue + blocked | |
| `compliance_overdue` | the overdue part of the inbox | |
| `compliance_blocked` | the blocked part of the inbox | |
| `my_inspections_open` | my open inspection sessions | |
| `my_feedback_answered` | my notes answered in the last 14 days | |
| `triage_queue` | notes awaiting triage | System Manager or Farm Manager |

### 3.4 Icons — an allowlist

`tiles.ICONS` holds 48 SF Symbols. The same list is compiled into the phone, and the phone falls
back to `square.grid.2x2` for anything it doesn't know. The list is the symbols the app already
uses, plus:

- `tray.full`
- `exclamationmark.shield`
- `checklist`
- `list.bullet.clipboard`
- `doc.text.magnifyingglass`
- `calendar.badge.exclamationmark`
- `hammer`
- `wrench.and.screwdriver`
- `drop`
- `leaf`
- `ant`
- `hare`
- `testtube.2`
- `person.badge.shield.checkmark`
- `graduationcap`
- `bubble.left.and.text.bubble.right`
- `shippingbox`
- `basket`
- `tractor.fill`
- `map`
- `qrcode.viewfinder`

### 3.5 The phone route

**`get_tiles(surface, app_version?, asset?, company?)`** returns:

```
{surface, tiles: [{key, config_version, title{en,es}, subtitle, icon, order, target,
                   badge: {count, tone} | null, stale: false}],
 evaluated_at, etag}
```

- The server applies audience, `show_if`, `min_app_version`, staged rollout (§1.3) and the
  24-tile cap.
- A badge query that fails comes back as `badge: null`. It never fails the route.
- **The phone caches the last good answer per surface, and shows it offline** with badges marked
  stale.

### 3.6 Tile MCP tools

| tool | kind | what it does |
|---|---|---|
| `create_tile(key, body, notes)` | write | a Draft |
| `update_tile(key, body, notes)` | write | a Draft at max+1 |
| `preview_tiles(surface, as_user?, version?, body?, asset?)` | read | exactly what `get_tiles` would return for that user, with each tile's validator report and the reason a tile is hidden (audience, show_if, version, rollout) |

---

## 4. Closing the compliance loop

### 4.1 Every rule that raises work has a phone path

**The path is resolved in this order:**
1. the rule's `producer_task_template`;
2. the new **`producer_inspection_template`** (Link);
3. the new **`producer_wizard`** (a Wizard key);
4. `ALERT_TASK_MAP`;
5. the inline `producer_*` fields.

**It is *renderable* when all of these hold:**
- its form validates;
- no enrolled device in its audience is missing a safety-critical kind;
- a wizard path's handler accepts `source_alert`.

**Its audience** is who can act on it:
- the subject employee;
- holders of the path's skill and certification;
- the rule's `notify_roles`.

**It has an entry point for that audience when any of these holds:**
- a task lands in My tasks or Available, which is always visible;
- the **Compliance inbox** tile reaches those people;
- a tile targets the inspection template or wizard.

**Rule enable is gated.** `approve_compliance_rule` refuses to enable a rule whose loop has a gap,
naming the gap, unless `accept_loop_gap` (a reason) is given. The reason is recorded on the rule as
`loop_gap_accepted`.

**Already-enabled rules are not touched;** the audit reports them.

> **Amendment (implementation, v0.207.0):** only *structural* gaps gate enabling — no path target, a disabled or missing template/inspection/wizard, a form that does not render, or a safety-critical kind an enrolled device cannot render. *Reach* gaps (nobody with a phone can act; no tile reaches them) are reported by `audit_compliance_loop` as `gaps` but do not gate, so a farm with no enrolled phone can still enable rules and dispatch from the Desk. The audit returns both `gaps` and `blocking_gaps`.

### 4.2 MCP

| tool | kind | what it does |
|---|---|---|
| `audit_compliance_loop(company?, rule?)` | read | per enabled rule (or one): `{rule, path{kind, ref, version}, renderable, device_problems, audience{people, sample}, entry_points[], dismissal, gaps[], ok}`, plus totals |
| `preview_compliance_loop(rule, alert?, as_user?, language?)` | read | **end to end, writes nothing**: (1) the alert (an open one, else a synthetic one from the rule's message); (2) the work path and the version it would snapshot; (3) the form as the phone renders it, EN and ES; (4) who sees it and through which entry point; (5) what completion produces (`creates_record` doctype); (6) how the alert clears (the rule re-scan after completion, i.e. auto-dismiss, or "manual only") |

### 4.3 The Compliance inbox

**Route:** `get_compliance_inbox(company?)` returns
`{due[], overdue[], blocked[], counts, evaluated_at}` for the caller.

**What is in it** — undismissed, unsnoozed alerts in the caller's companies where any of these is
true:
- the caller is the `subject_employee`;
- a task with `source_alert` is assigned to the caller, or the caller may claim it;
- the caller holds one of the rule's `notify_roles`.

**The three states:**
- **overdue:** `due_date` < today.
- **due:** due within the `compliance_inbox_due_days` flag (default 14), or no due date.
- **blocked:** for this caller, something stands in the way. Possible reasons:
  - a missing certification;
  - a paused task;
  - an approval step still pending;
  - the rule's loop has a gap;
  - the caller's device can't render a safety-critical field.

  Each blocked item carries `blocked_reason` in EN and ES.

**Each row:**
```
{alert, title, due_date, state, blocked_reason?, regulation, subject,
 action: {kind: task|inspection|wizard|task_template, ref, context: {source_alert}}}
```

**Where the action comes from:**
- an existing linked task gives `task`;
- otherwise the rule's path: `inspection` with template, `wizard` with key, or `task_template`,
  which is raised on tap through `start_template_task(source_alert=…)`.

**The seeded tile:** `compliance_inbox` (§3.1) is seeded at version 1 **Published** for every mobile
user, with the `compliance_inbox` badge.

### 4.4 Starting work from the phone

**`start_template_task(template, location_doctype?, location?, source_alert?, client_reference?)`**
raises a Farm Task from the template.

- **What it creates:** a task assigned to and claimed by the caller, carrying the template
  snapshot, its `template_version`, and `source_alert` (one task per alert, as
  `materialize_task_for_alert` already does).
- **Refusals:** it refuses a disabled template, a caller without the required certification, and a
  template not reachable by a tile or inbox row for the caller.
- **Idempotent** on `client_reference`.

**Completion** writes the record. The existing post-completion re-scan (`_evaluate_compliance_after`)
**auto-dismisses** the alert. A wizard submitted with `source_alert` triggers the same re-scan for
that alert's rule.

---

## 5. Label-driven compliance

### 5.1 Label Profiles (a Farm Config Version kind — versioned, previewed and rolled out like the rest)

```json
{"schema_version": 1, "key": "rodenticide_bait_station",
 "title": {"en": "Rodenticide in bait stations", "es": "Rodenticida en estaciones"},
 "when": {"all": [{"fact": "pesticide_use_scope", "op": "equals", "value": "Non-crop"},
                  {"any": [{"fact": "tamper_resistant_station_required", "op": "truthy"},
                           {"fact": "product_form", "op": "contains", "value": "bait"}]}]},
 "attach": {"programs": ["rodent_bait"], "task_templates": [], "inspection_templates": [],
            "compliance_rules": [], "requirements": {"certifications": [], "ppe_attestation": false}},
 "priority": 10}
```

**`when` conditions:**
- `fact` is one of the Item label fields (`product_labels.KEY_FIELDS`);
- `op` is one of `equals`, `in`, `truthy`, `falsy`, `gte`, `lte` or `contains`;
- conditions nest with `all` and `any`.

**Seeded (version 1, Published):**

| profile | when | attaches |
|---|---|---|
| `rodenticide_bait_station` | as above | the rodent program |
| `restricted_use_pesticide` | `restricted_use` truthy | certification **Applicator License** |
| `danger_signal_word` | `signal_word` equals Danger | a PPE attestation |

### 5.2 What happens when a label is registered

After `register_product_label` validates (the validation is not Rejected and carries no error):
1. The Item's facts are matched against every Published profile.
2. For each matched profile's parts, the server asks: **is it installed and enabled?**
   - **Everything already active:** the attachment is written straight to the Item —
     `compliance_state = Active`, and `compliance_profiles_json` =
     `[{profile, config_version, matched_facts, parts}]`.
   - **Anything new** (a program not installed, a template or rule disabled or unapproved):
     `compliance_state = Proposed`, and `compliance_proposal_json` holds MCP calls
     (`import_program`, `approve_compliance_rule`, `update_farm_task_template` with enabled) to be
     approved in one step. The approval runs through `registry.dispatch`, exactly like triage.
     **Nothing is activated without a person.**
3. The route's answer gains `compliance: {state, attached[], proposed_calls[], matched[]}`.
4. **Publishing a profile version re-matches** every label-validated Item through the same
   active-or-proposal path.

**Runtime effect** (no rule edits per product):
- When a task's product has Active profiles, their `requirements.certifications` join the
  qualification check on claim, assign and start (`qualifications.py`).
- The product is identified by `bait_product`, a `materials_used` item or a form `link`→Item
  answer.
- `ppe_attestation` makes completion require a PPE attestation answer. The phone shows the label's
  PPE list.

### 5.3 MCP

| tool | kind | what it does |
|---|---|---|
| `update_label_profile(key, body, notes)` | write | a Draft, created or at max+1 |
| `preview_label_profile(key?, version?, body?, item?)` | read | the validator report, the Items that would match and why, and for one `item`, what would attach or be proposed |
| `list_label_compliance(item?, state?)` | read | Items with their attached profiles, pending proposals, and stale attachments (an older profile version) |
| `approve_label_compliance(item, note?)` | write | runs the proposal's calls, then attaches. System Manager or Farm Manager |
| `reject_label_compliance(item, reason)` | write | declines the proposal, with the reason recorded |

---

## 6. The earlier phone items

1. **App version:** 0.21.0 (build 2), bumped on every release from now on.
   - **Blocked:** it lives in `project.pbxproj`, which holds another session's uncommitted work.
   - It is bumped as soon as that file is free, and the report says so.
2. **"Start Inspection" from an asset scan** opens a session directly:
   - new route **`list_startable_inspections(location_doctype, location)`**, returning
     `[{template, title{en,es}, version}]` (active, approved templates applicable to the place);
   - then `start_inspection`;
   - one template goes straight in; more than one gives a picker.
3. **Inspection submits queue offline.**
   - `submit_inspection` gains `client_reference`, stored as Inspection Session `submit_reference`.
   - A repeat returns the first result.
   - The phone queues through `SyncManager`.
4. **Login QR enrolment:**
   - A `farm_ops_enroll` QR (`{type, v, url, api_base, token}`) is exchanged through
     `POST /farmops/api/mobile/enroll_device` with `{token, device_name, device_identifier}`.
     `device_identifier` is the push registrar's stable ID.
   - The `farm_ops_login` QR keeps working.
   - `list_my_inspections` rows gain `farm_task`.
5. **Triage replies in the app:**
   - Today gets a `my_feedback` report tile with the `my_feedback_answered` badge (seeded).
   - A note's "Fixed" status and replies show on `MyFeedbackView`.
6. **Triage classifier:**
   - Desire phrasing counts as **Feature request** even as a question: "can we / can you / could
     it / is it possible / would it be possible / any way to / I want / we need / I'd like",
     "¿se puede", "quisiera", "necesitamos".
   - Feature is checked before Question.
   - Question is only how-to phrasing with no desire words.
   - The patch **`reclassify_feature_requests`** re-sorts Auto-classified Question notes only.

---

## 7. Snapshots, fallbacks, security

**Snapshots — work keeps the version it started with:**
- **Tasks:** Farm Task Template gains `version`, bumped when the form, checklist, evidence or
  certification changes. Farm Task gains `template_version` next to the existing form snapshot.
- **Inspection Sessions** already record `template_version`.
- **Wizards:** the phone keeps the definition it started with, and submits against it (§2.2).
- **Tiles:** served per version, and the tapped tile's `config_version` goes into the audit row.

**Fallbacks:**
- An unknown field kind falls back to text or photo.
- A safety-critical unknown kind blocks, with "update the app to do this step" (v0.205.0 rules).
- An unknown tile target, report id or icon: the tile is hidden (or the icon falls back) and the
  phone logs it once.
- A wizard or tile the phone cannot decode keeps the last good copy.

**No remote code:**
- bodies are data;
- conditions go through the whitelisted AST parser;
- queries, handlers, reports, icons and doctypes come from allowlists;
- there is no `custom_python` in any of these kinds.

**Rate and size limits:** §1.6.

**Tests:**
- **Server:**
  - validator unit tests, one per rule in §1.4, §2.1, §3.1–3.4 and §5.1;
  - lifecycle tests (immutability, stage/publish/rollback/retire, rollout by user);
  - loop audit and preview;
  - the inbox;
  - label attach and proposal.
- **Contract fixtures:**
  - The server generates `tests_standalone/contract/v0_207_0/*.json` from real functions: a
    wizard, a `get_tiles` answer, an inbox, a scan with startable inspections, and a submit result.
  - A server test asserts they still match.
  - **The phone bundles byte-identical copies** in its kit tests and decodes every one.
- **Phone:**
  - Kit "render plan" snapshot tests: a pure function from every field kind × capability state,
    and every tile target type, to a JSON golden file.
  - UI tests that open a DEBUG gallery of every kind and every tile type.
  - There are no third-party snapshot libraries, per the kit's zero-dependency rule.

---

## 8. Counts, routes, migrate

**Tools:** 962 in total — 474 read, 488 write.

| | new tools |
|---|---|
| **Read (8)** | `list_phone_configs`, `get_phone_config`, `preview_wizard`, `preview_tiles`, `audit_compliance_loop`, `preview_compliance_loop`, `preview_label_profile`, `list_label_compliance` |
| **Write (11)** | `stage_phone_config`, `publish_phone_config`, `rollback_phone_config`, `retire_phone_config`, `create_wizard_definition`, `update_wizard_definition`, `create_tile`, `update_tile`, `update_label_profile`, `approve_label_compliance`, `reject_label_compliance` |

**Mobile routes:** 147.
- **New:** `get_tiles`, `get_compliance_inbox`, `start_template_task`, `list_startable_inspections`.
- **Changed:**
  - `get_wizard_definition` gains `version`;
  - `submit_wizard_via_mobile` gains `config_version`, `client_reference` and `context`;
  - `submit_inspection` gains `client_reference`;
  - `list_my_inspections` rows gain `farm_task`;
  - `register_product_label`'s answer gains `compliance`.

**Migrate:**
- the doctype Farm Config Version;
- new fields:
  - Farm Feature Flag `users`;
  - Compliance Rule `producer_inspection_template`, `producer_wizard`, `loop_gap_accepted`;
  - Farm Task Template `version`;
  - Farm Task `template_version`;
  - Inspection Session `submit_reference`;
  - Item `compliance_state`, `compliance_profiles_json`, `compliance_proposal_json`;
- seeds, create-only, as version 1 Published:
  - the tiles `compliance_inbox` and `my_feedback`;
  - the three label profiles;
- patches:
  - `wizards_to_config_versions`: each Wizard Definition becomes version 1 Published of its key,
    converted to form_schema v2 with `from_wizard_fields`; the legacy row stays as the fallback;
  - `reclassify_feature_requests`.

**iOS:** SERVER_CHANGES §54.
