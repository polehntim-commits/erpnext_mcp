# Renderable templates and product labels on the phone — contract (frozen, v0.204.0)

Tim approved two items on 2026-09-27.

- **Renderable templates** ("That would be awsome"). Any Farm Task Template or Inspection Template
  created through MCP must render correctly on the iPhone with **no code change**, for every
  template, not only rodent control.
- **Labels on the phone.** "So an applicator can look at the label of the product they are
  handling. This would allso work when we are spraying as well."
  - A product's label must be one tap away wherever a worker handles it: the EPA PDF, the label
    photos and the key fields.
  - It must be cached on the device.
  - The task must record that the label was available and whether it was viewed.

Today neither works. The phone ignores a task's checklist, and the mobile complete route drops
`checklist`, so a template with required items cannot be completed from a phone. `Item` is not on
the phone's attachment list. The drafted OML template "Rodent Bait Placement - Interior" shows
everything a template could not express:

- Its branching is written as prose ("N/A only if UNOCCUPIED").
- Its product is free text.
- It takes one photo for every station.
- It is English only, and its `task_type` is `Other`.
- Its approval is a Text item, and its safety attestation has evidence type `None`.
- It creates no record.

## 1. The form schema: one field vocabulary, one renderer

A **form** is an ordered list of **fields**. The same field vocabulary is used by three things:

- a Farm Task Template's `form_schema`;
- an Inspection Template section's `field_prompts` (as a list);
- the wizards (§1.6).

The phone has **one** renderer for it.

### 1.1 Field

| key | type | meaning |
|---|---|---|
| `key` | string | unique within the form (a group's children are unique within the group); `^[a-z][a-z0-9_]{0,59}$` |
| `type` | string | one of §1.2 |
| `label` | `{en, es}` | shown text; `en` required |
| `help` | `{en, es}` | optional help under the field |
| `required` | bool | default false |
| `show_if` / `required_if` | condition | §1.3 |
| `options` | `[{value, label:{en,es}}]` | `select`, `multi_select` |
| `min` / `max` / `step` | number | `number`, `measurement`; `min`/`max` also bound `date` |
| `uom` | `{fixed: "<UOM>"}` or `{from_field: "<link key>"}` | `measurement`: a unit, or the linked Item's `stock_uom` (Place Pac, Block …) |
| `min_count` / `max_count` | int | `photo` (default 1/10), `group` (default 1/50) |
| `link` | `{doctype, filters}` | `link`: doctype one of `Item`, `Asset Register`, `Housing Unit`, `Employee`; `filters` equality only, e.g. `{"item_group": "Pest Control Products"}` |
| `fields` | `[field]` | `group`: the child fields of ONE repeat |
| `role` | string | `approval`: the role whose holder signs |
| `before_start` | bool | `approval`: the task cannot START until it is signed (default true) |
| `statement` | `{en, es}` | `attestation`: the sentence the worker attests to |

### 1.2 Types

| type | answer value |
|---|---|
| `select`, `multi_select` | option value / list of values |
| `check` | bool — a tick; an unticked REQUIRED check fails the form |
| `attestation` | bool; the worker attests to `statement`. A required one must be `true`, which is how a safety statement stops being "evidence_type None" |
| `text`, `long_text` | string |
| `number` | number |
| `measurement` | `{value, uom}` — `uom` is the resolved unit and the phone sends it back |
| `date`, `datetime` | ISO string |
| `photo` | list of evidence file references (the existing staged-upload tokens), `min_count`…`max_count` |
| `signature` | one file reference |
| `gps` | `"lat,lon"` |
| `link` | the docname |
| `group` | list of objects, each `{child_key: value}` — a repeatable group, e.g. a **station** of photo + room/asset tag + count |
| `approval` | server-owned — see §3.3; the phone shows status and never sends a value |
| `info` | no answer; a paragraph of instructions (`label` / `help`) |

A `link` to `Item` automatically carries that item's **label** (§4). The phone shows "View label" next to it.

### 1.3 Conditions

```json
{"field": "occupancy_tier", "equals": "Occupied"}
{"context": "occupancy_at_creation", "equals": "Occupied"}
{"all": [cond, cond]}   {"any": [cond, cond]}   {"not": cond}
```

- **Operators:** `equals`, `not_equals`, `in`, `not_in`, `truthy`, `falsy`.
- **`field`** reads another answer in the same form. Inside a group it first reads the same repeat.
- **`context`** reads a fact about the task: `occupancy_at_creation`, `bait_placement`,
  `location_doctype`, `asset_type`, `task_type`, `template`, `language`.
- **The server evaluates the same conditions** when it validates a completion. A field hidden by
  `show_if` is neither required nor stored.

### 1.4 Validation and "renders on the phone"

`form_schema.validate(schema)` returns `{errors: [...], warnings: [...]}`. Each finding has
`{path, code, message}`.

**Errors** are refused by `create_farm_task_template`, `update_farm_task_template`,
`create_inspection_template` and `update_inspection_template`:
- an unknown type or key;
- a duplicate or malformed key;
- a condition naming a field or context that does not exist, or a malformed condition;
- `select` with no options;
- a `link` doctype outside the list;
- `uom.from_field` not naming a `link` to Item;
- a `group` with no fields, or nested more than one level;
- `approval` with no `role`;
- a missing `label.en`.

**Warnings** are allowed, and reported by `get_farm_task_template.problems` and the preview tool:
- **English only:** a label or help with no `es`.
- **Prose branching:** a label matching `/N\/A (only )?if|if (UN)?OCCUPIED|only if/i`. Use `show_if`.
- **Free-text product:** a `text` field whose label says product/EPA. Use a `link` to Item.
- **One photo for many things:** a `photo` whose label says each/every station, room or placement,
  outside a group. Use a `group`.
- **Approval as text:** a `text` field whose label says approval or approved. Use `approval`.
- **Safety statement with no answer:** a legacy checklist item with `evidence_type` None whose
  label reads as a safety statement. Use `attestation`.
- `task_type` is `Other`.
- `creates_record` is empty on a template whose `task_type` produces a regulated record
  (`Pest Control`, `Spray`).

### 1.5 Backward compatibility

- **Legacy templates.** A template with no `form_schema` renders from its legacy checklist. Each
  item becomes a field with `key` = a slug of the name. Evidence types map to field types:

  | legacy `evidence_type` | field type |
  |---|---|
  | None | `check` |
  | Photo | `photo` (min 1) |
  | Text | `text` |
  | Measurement | `measurement` with no unit |

  The completion's answers are written back as the legacy ticks, so everything that reads
  `checklist_status` is unchanged.
- **Tasks.** A task snapshots `form_schema` at creation, the same way it snapshots the checklist
  (Farm Task field `form_schema`). Editing the template never changes a task already raised.
- **The evidence contract** (`evidence_required`) is unchanged and still applies.

### 1.6 Wizards share the vocabulary

- The mobile wizard spec gains, per step, **`form`**: the step's fields in the §1 vocabulary. Types
  map as follows (the mapping is §1.2's):

  | wizard type | §1 type |
  |---|---|
  | `checkbox` | `check` |
  | `employee_select` | `link` to Employee |
  | `asset_select` | `link` to Asset Register |
  | `qr_scan` | `text` |
  | `audio_note` | `long_text` |

- `visible_if` becomes `show_if`.
- The existing wizard fields stay for older app builds. A new app renders `form`.

### 1.7 Inspection Template sections

- `field_prompts` may be a **list of §1 fields**. The legacy dict `{key: {type, label, label_es}}`
  is still read, as fields.
- A section's `evidence_contract.checklist_items` keys are satisfied by answers to fields with
  those keys.
- `submit_inspection_session` accepts `answers` per section alongside `checklist_values`, and
  validates them like §3.2.

## 2. Template metadata

- **`task_type`** gains **`Pest Control`** and **`Maintenance`**, on Farm Task and on Farm Task
  Template.
- **Farm Task Template gains:**
  - `form_schema` (JSON);
  - **`applies_to_asset_types`** (Small Text, one per line): the asset types whose scan screen
    offers this template;
  - **`title_es`** and **`instructions_es`**.
- **The asset scan** (`scan_asset` / `get_asset_detail`) returns **`available_templates`**: the
  enabled templates bound to the asset's type, each `{template, title: {en, es}, task_type}`. It
  also returns any open task at the asset (unchanged).
- **The phone** can raise one of those templates at the asset with the existing
  `create_task_from_template`, where the dispatch role allows.

## 3. Completion and approval

### 3.1 What the phone receives

`get_task` adds:

- **`form`**: the resolved fields for the task, from the snapshot or the legacy checklist, with
  `uom.from_field` resolved where the linked Item is already known.
- **`form_answers`**: saved so far.
- **`approvals`**: `[{key, role, before_start, status: pending|approved, approved_by, approved_at}]`.
- **`products`**: every Item the task handles — `bait_product`, every `link` answer to Item, and
  `materials_used` — each `{item_code, item_name, label_available}`.
- **`sop`**: `{en, es}` document references, from the template.

### 3.2 What the phone sends

`complete_task_via_mobile` and `complete_farm_task` accept:

- **`form_answers`**: `{key: value}` per §1.2.
- **`checklist`**: finally forwarded by the mobile route.

The server does the following:

- It evaluates the conditions and refuses a completion that is missing a required answer, or
  carries an out-of-range or wrong-typed one. It names the field, in the worker's language.
- It stores the answers on the assignment (`form_answers`) and on the task.
- It turns `check` and `attestation` answers into the legacy ticks.
- It fills `bait_product` from the first `link` to a Pest Control Item, and `bait_activity` from a
  field with key `rodent_activity_found` when present.

### 3.3 Approval steps

- **New mobile route `approve_task_step(task, key, signature)`** and **MCP tool
  `approve_task_step`** (mutating, default off). The caller must hold the field's `role`. The route
  records `{approved_by, approved_at, signature}` on the task.
- **The phone of the approver** sees the pending approval on the task.
- **Enforcement:**
  - `start_farm_task` is refused while a `before_start` approval is pending.
  - `complete_farm_task` is refused while any approval is pending.

## 4. Product labels on the phone (Tim's intent)

**New mobile routes, open on enrolment:**

- **`get_item_label(item_code)`** returns:
  - `{item_code, item_name, item_group}`, and
  - the key fields: `epa_registration_number`, `signal_word`, `restricted_use`,
    `active_ingredients`, `ppe_requirements`, `rei_hours`, `phi_days`, `phi_crop`,
    `application_rate`, `application_rate_uom`, `pesticide_use_scope`, `storage_disposal`,
    `product_form`, `package_size`, and the five rodent label facts;
  - `files: [{file, file_name, kind: epa_label_pdf|label_photo|other, content_type, file_size,
    modified}]`;
  - `label_available`: true when an EPA label PDF is attached.
- **`get_item_label_file(item_code, file, max_bytes)`** serves base64 content, only for files
  attached to that Item, up to the existing 8 MiB ceiling.
- **`record_label_viewed(task, item_code)`** is for the task's holder. It appends `{item_code, user,
  viewed_at}` to Farm Task **`label_views`** (JSON). It is idempotent per user, item and day.

**Label in possession at application:**

- When a task that handles a product (§3.1 `products`) is **started** and **completed**, the server
  stamps Farm Task **`label_available`**: all handled products have an EPA label PDF on file.
- It also stamps **`label_snapshot`**: the EPA registration number and PDF file name per product.
- A completion with a product that has **no** label on file is **not refused**. It carries a
  warning and `label_available = 0`, and rule `pesticide_label_unavailable` (seeded **disabled**)
  raises it.

**Where the phone shows "View label":**

- task detail, for every entry in `products` (rodent bait placement, check and removal);
- the spray record screen, one entry per product in the tank;
- the product card (barcode or search) and inventory product rows;
- the asset scan, for a building with open bait tasks, from those tasks' products.

**On the phone:** the EPA PDF and photos are **cached on the device**, keyed by item, file and
`modified`, the same way training documents are cached, so they open offline in the field. Opening
the label on a task calls `record_label_viewed`, and queues the call when offline.

## 5. The pest control application record (`creates_record`)

Spray Application does not fit non-crop bait:
- it refuses a Housing Unit as a block;
- it computes acres and gallons per acre;
- it opens REI and PHI windows.

So a new register, **`Pest Control Application`**, is added. It is a regulated record (who applied
what, where, how much, under which licence) and is the non-crop sibling of Spray Application.

Fields:
- `company`, `source_task`, `applied_at`;
- `location_doctype` / `location`, `placement` (Exterior/Interior), `occupancy` (Occupied/Unoccupied);
- `product` (Item), `epa_registration_number`, `quantity`, `uom`, `stations` (Int);
- `applicator` (Employee), `applicator_certification` (Certification);
- `label_available`, `label_viewed` (Check);
- `notes`, `docstatus` none.

- A builder in `inspections.BUILDERS` writes it on completion of a task whose `creates_record` is
  `Pest Control Application`.
- It maps from the task (location, occupancy, `bait_placement`, `bait_product`), from the answers
  (`quantity` from a `measurement` field keyed `quantity`, or the sum of a `stations` group's
  `count`; `stations` = the group's length), and from the applicator's qualifying certification.
- The rodent Placement and Removal templates set `creates_record: Pest Control Application`.
- The record is listed in the audit packet's `pest_control` block and read by
  `list_pest_control_applications` (read tool).

## 6. MCP tools

- **`preview_farm_task_template(template, language?, context?)`** (read). It returns exactly what
  the phone would receive for a task raised from the template (§3.1 `form` and more), after
  conditions are resolved for the given `context`, with the validation findings. It also takes an
  unsaved template body (`template_body`), so a model can preview before creating.
- **`preview_inspection_template(template, language?)`** (read), in the same way.
- `create_/update_farm_task_template` take `form_schema`, `applies_to_asset_types`, `title_es` and
  `instructions_es`, and refuse on errors. `create_/update_inspection_template` validate
  `field_prompts` lists.
- `get_farm_task_template.problems` includes the render warnings.
- **`approve_task_step`** (mutating, default off; role-gated).
- **`list_pest_control_applications`** (read).

## 7. The five rodent templates, rebuilt (seeded disabled)

- They are `task_type: Pest Control`, with `applies_to_asset_types`: Cabin, House, Housing Unit,
  Storage, Cold Storage.
- They carry EN/ES titles, instructions and labels.
- **The product** is a `link` to Item filtered to Pest Control Products, which exposes its label.
- **Quantities** are `measurement` fields with `uom.from_field: product` (Place Pac or Block from the
  item).
- **Occupied branching** is `show_if {context: occupancy_at_creation, equals: Occupied}`, used for
  the notice confirmation and for locked stations only.
- **Stations** are a `group` of photo, room or asset tag, and count.
- **Interior** has an `approval` step, `role: Farm Manager`, `before_start: true`.
- **Safety statements** are `attestation`s.
- **Check** has `rodent_activity_found` as a `select` (Yes/No).
- **Placement and Removal** create a `Pest Control Application`.
- **OML's drafts are not touched.** Patch `rebuild_rodent_templates` updates only templates this
  app authored and nobody edited (`authored_by System`, no `tasks_raised`). OML's drafts were
  authored by another session and are left alone; `preview_farm_task_template` shows Tim what the
  rebuilt version looks like.

## 8. Phone (fafo_ios, SERVER_CHANGES §51)

- **One generic `FormRenderer`**, a SwiftUI view driven by §1 fields. It is used by task
  completion, inspection sections and wizards. It implements:
  - every §1.2 type, `show_if` and `required_if`;
  - EN/ES by the app's language setting;
  - repeatable groups;
  - `uom.from_field` resolution from the chosen Item;
  - an approval status row;
  - the unknown-type rule below.
- **An unknown type** renders as a "needs a newer app" row. If it is required, completion is
  blocked with that sentence.
- **A label viewer** (EPA PDF plus photos plus key fields), with an on-device cache and
  `record_label_viewed`, opened from every place in §4.
- **SOPs** are read from `sop.en` / `sop.es`.
- **Old servers:** with no `form` in `get_task`, the phone falls back to the evidence contract as
  today; with no label routes, "View label" is hidden.
