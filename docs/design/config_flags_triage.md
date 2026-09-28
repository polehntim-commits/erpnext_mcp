# From hours to moments: extraction config, feature flags, feedback triage — contract (frozen, v0.206.0)

Tim, 2026-09-28: "From hours to moments, and yes add 1-3 please."

**Goal:** most field feedback is fixed with a few MCP calls and no iOS release.

**Rules:**
- The phone downloads **configuration, never code** (App Store 2.5.2).
- It stays on Apple's stack: Vision OCR and FoundationModels.
- Nothing is applied without a person approving it.

## 1. Server-driven extraction config

### 1.1 The record

New DocType **`Extraction Config`**, versioned by copy the way Compliance Rules are:

| field | meaning |
|---|---|
| `document_type` | Pesticide Label, Receipt, I-9 Document, … (matches Document Validation's `document_type`) |
| `version` | int. `document_type` + `version` is unique |
| `status` | `Draft` / `Published` / `Superseded` |
| `config_json` | the body (§1.2) |
| `notes`, `authored_by` (System / Operator / AI-proposed), `published_by`, `published_on` | provenance |

- **Exactly one Published row per `document_type`.**
- **Publishing a draft** supersedes the previous Published row. The old row stays, so every result
  can be traced to the config that read it.
- **`config_version`** is the string `"<document_type>@<version>"`. Built-in defaults are
  `"<document_type>@builtin-<n>"`.

### 1.2 The body (schema version 1)

```json
{"schema_version": 1,
 "instructions": {"en": "You read US EPA pesticide labels …"},
 "fields": [
   {"name": "epa_registration_number", "type": "string", "description": "…", "guide": null},
   {"name": "signal_word", "type": "string", "guide": {"any_of": ["Danger", "Warning", "Caution", "None"]}},
   {"name": "application_rate", "type": "string", "section": "rate"},
   {"name": "active_ingredients", "type": "array", "items": {"name": "string", "concentration": "number", "unit": "string"}}
 ],
 "sections": {
   "rate": {"start_headings": ["APPLICATION DIRECTIONS", "DIRECTIONS FOR USE"],
            "end_headings": ["STORAGE AND DISPOSAL", "PRECAUTIONARY STATEMENTS", "FIRST AID"],
            "exclude_patterns": ["(?i)launder", "(?i)first aid"]},
   "package": {"start_patterns": ["(?i)\\b\\d+\\s*[x×]\\s*\\d+(\\.\\d+)?\\s*(oz|lb|g)\\b"], "lines": 1}},
 "extractors": [
   {"field": "unit_hint", "section": "package", "pattern": "(?i)(place pacs?|blocks?|pouches?|bait stations?)", "group": 1},
   {"field": "epa_registration_number", "pattern": "(?i)EPA\\s*Reg\\.?\\s*No\\.?\\s*([0-9]+-[0-9]+(?:-[0-9]+)?)", "group": 1}
 ],
 "rules": [
   {"code": "epa_format", "field": "epa_registration_number", "kind": "pattern",
    "pattern": "^[0-9]+-[0-9]+(-[0-9]+)?$", "severity": "error",
    "message": {"en": "EPA Reg. No. is not in the NNNN-NN or NNNN-NN-NNNN form"}},
   {"code": "ingredients_total", "field": "active_ingredients.concentration", "kind": "sum_max",
    "max": 100.0, "severity": "error", "message": {"en": "Active ingredients total more than 100%"}},
   {"code": "non_crop_na", "kind": "not_applicable", "fields": ["rei_hours", "phi_days", "phi_crop"],
    "when": {"field": "pesticide_use_scope", "equals": "Non-crop"}}
 ],
 "advisory_drop": [
   {"field": "epa_registration_number", "unless_rule_failed": "epa_format"},
   {"fields": ["rei_hours", "phi_days", "phi_crop"], "when": {"field": "pesticide_use_scope", "equals": "Non-crop"}},
   {"field": "active_ingredients", "message_pattern": "(?i)100\\s*%"}
 ],
 "llm": {"max_message_chars": 240, "temperature": 0.0}}
```

**A portable regex subset.** Patterns use only features ICU (NSRegularExpression) and Python `re`
share:
- allowed: literals, classes, `\d \s \w \b`, groups (numbered, and `(?:…)`), `? * + {m,n}`,
  alternation, anchors, and a leading `(?i)`;
- refused: look-behind, named groups and backreferences.

**Meaning of each part:**
- **`instructions`, `fields` and `guide`** feed the on-device FoundationModels `DynamicGenerationSchema`:
  - `guide.any_of` becomes an `anyOf` choice;
  - `type` is one of `string`, `number`, `integer`, `boolean`, `array`;
  - `items` gives the object fields of an array.
- **`sections`** carve OCR text:
  - **heading form:** from the first line matching any `start_headings` (case-insensitive, line
    start) up to the next `end_headings`, dropping lines that match `exclude_patterns`;
  - **pattern form:** `start_patterns` plus `lines` (the matching line and N−1 after it).

  A field with `section` is read from that section only.
- **`extractors`** are deterministic reads. The phone and the server run them identically. The
  first match wins, taking `group`.
- **`rules`** are the on-device check. The phone runs them, and the server re-runs them in preview.
  Kinds:
  - `pattern`: the field matches;
  - `required`;
  - `sum_max` / `sum_min`: the sum over `a.b` across list items;
  - `range` (`min` / `max`);
  - `one_of` (`values`);
  - `not_applicable`: the fields are ignored and any finding on them is dropped;
  - `when` limits a rule to cases where a field matches.
- **`advisory_drop`**: the on-device model findings to drop. The server applies the same list
  (replacing the hard-coded v0.202.0 §8.3 filter, which becomes the built-in default).

### 1.3 Built-ins, caching, auditing

- **Built-in defaults** ship in the app: `erpnext_mcp/extraction/<slug>.json` on the server, and
  the same files bundled in the iOS app.
  - One exists per supported type: Pesticide Label (what v0.201.0–v0.204.0 hard-coded), Receipt
    and I-9 Document.
  - `seed` writes each as version 1 **Published** where a type has no row. It is create-only.
- **The phone** fetches the Published config at launch and before a scan (§1.4), and keeps the
  **last good** copy.
  - It falls back to the bundled default when offline or on first run.
  - A config whose `schema_version` it doesn't know, or that fails its checks, is ignored and the
    last good one stays in use.
- **Every Document Validation records `config_version`** (new field). The phone sends it with
  `validate_document_extraction` / `register_product_label`, and a server-side validation writes
  the one it used.

### 1.4 Routes and tools

**Mobile route:** **`get_extraction_config(document_type, known_version?)`**, open on enrolment.
It answers `{document_type, config_version, config}`, or `{not_modified: true, config_version}`.

**MCP tools:**

| tool | kind | what it does |
|---|---|---|
| `list_extraction_configs(document_type?)` | read | every version, with its status |
| `get_extraction_config(document_type, version?)` | read | Published by default |
| `update_extraction_config(document_type, config, notes)` | write, default off | validates, then writes a **Draft** at max version + 1. Never touches Published |
| `preview_extraction_config(document_type, version? \| config?, validation)` | read | runs sections, extractors and rules against a stored **Document Validation's** OCR text and extraction; returns the carved sections, the extracted values, rule results, the advisory findings that would be dropped, and a diff against what the validation recorded |
| `publish_extraction_config(document_type, version)` | write, default off | Draft → Published; the previous one → Superseded |

**What `update` checks before it writes:**
- the regexes compile, and use no refused feature;
- field names are unique;
- every section referenced exists;
- every rule kind is known.

## 2. Feature flags and thresholds as data

**New DocType `Farm Feature Flag`** (one table for flags and thresholds):

| field | meaning |
|---|---|
| `flag_key` | lower_snake_case, e.g. `label_capture_v2`, `bait_check_active_days` |
| `kind` | `Flag` (bool) / `Threshold` (number) / `Text` |
| `enabled` (Flag), `number_value` (Threshold), `text_value` (Text) | the value |
| `company` | empty = every company |
| `roles` | optional; one per line; the row applies only to users holding one |
| `min_app_version` / `max_app_version` | optional; a phone row applies only in range (semver compare) |
| `description`, `owner_area` | why it exists and what reads it |
| `active` | a row switched off is ignored |

**Resolution** (`flags.value(key, company, roles, app_version, default)`):
- The most specific applicable active row wins: company-and-role, then company, then global-and-role,
  then global.
- Rows that fail the version range are skipped.
- No row gives the caller's default. **Everything ships dark.**

**Where flags are read and recorded:**
- **Mobile route** **`get_feature_flags(app_version?)`**, open on enrolment, returns the resolved
  `{key: value}` for the caller's company, roles and app version, plus `evaluated_at`. The phone
  caches it and reads it through one accessor.
- **Recorded on the records they affect:**
  - Document Validation gets `feature_flags` (JSON): the flags the phone sent (`feature_flags`
    argument), plus any the server read while writing it.
  - Farm Task completion accepts `feature_flags` and stores them on the assignment.
  - `flags.stamp(doc, key, value)` is the server helper.
- **MCP:** `list_feature_flags(key?, company?)` (read); `set_feature_flag(flag_key, kind, value,
  company?, roles?, min_app_version?, max_app_version?, description?, active?)` (write, default off,
  System Manager or Farm Manager) upserts on (key, company, roles, versions).
- **Seeded rows:** none. Every existing behaviour stays hard-coded until a flag is introduced by name.

## 3. Tell the Farm triage

**New fields on App Feedback:**

| field | meaning |
|---|---|
| `triage_class` | Select: blank / `Data or config` / `Code bug` / `Feature request` / `Question` / `Duplicate` |
| `triage_state` | Select: blank / `Auto-classified` / `Proposed` / `Approved` / `Applied` / `Rejected` / `Ticketed` |
| `triage_summary` | Small Text |
| `evidence_json` | `{screen, reference, screenshot, linked_records: [{doctype, name}], app_version}` |
| `proposal_json` | `{calls: [{tool, arguments, why}], proposed_by, proposed_at, authored_by: AI-proposed\|Operator}` |
| `ticket_json` | `{title, repro_steps, expected, actual, suspected_area, affected_records, app_version, severity}` |
| `applied_json` | `{approved_by, approved_at, results: [{tool, ok, summary, action_log}], note}` |

1. **On arrival** (`submit_app_feedback`): a deterministic classifier fills `triage_class`,
   `triage_state = Auto-classified`, `triage_summary` and `evidence_json`. It links every record
   named in the text or reference (FT-, DVAL-, AFB-, item codes, asset tags) and the screenshot.
   It never proposes changes.
2. **`propose_triage_fix(feedback, triage_class, summary, calls?, ticket?)`** (write, default off)
   is how an MCP client (Claude) attaches a fix.
   - **Checks:**
     - each call must name an **existing tool**;
     - its arguments must fit that tool's input schema;
     - read tools are refused in a proposal, since there is nothing to approve;
     - `calls` is for `Data or config`, and `ticket` is for `Code bug` / `Feature request`.
   - **Result:** `triage_state = Proposed` (or `Ticketed` for tickets).
   - **Never applied.**
3. **`approve_triage_proposal(feedback, note)`** (write, default off; System Manager or Farm Manager)
   applies the fix.
   - **How:** it runs each call through the **normal dispatcher** (`registry.dispatch`), so every
     tool's own switch, role gate and audit log apply.
   - **If a call fails:** it stops at the first failure and reports what ran.
   - **On full success:** it writes `applied_json`, adds a feedback **reply** ("Fixed: …" plus the
     note), resolves the feedback with the note, and sets `triage_state = Applied`.
4. **`reject_triage_proposal(feedback, reason)`**: `triage_state = Rejected`, with the reason
   recorded. The feedback itself stays open.
5. **`list_triage_queue(state?, triage_class?, company?)`** (read): feedback by triage state, each
   with summary, evidence links, proposal and ticket.
6. **Desk:** the App Feedback form gets **Approve proposal** and **Reject proposal** buttons (for
   System Manager or Farm Manager). They call whitelisted methods that run the same functions.

## 4. Counts, routes, deploy

- **Tools:** 943 total, 466 read, 477 write.
  - New read tools: `list_extraction_configs`, `get_extraction_config`, `preview_extraction_config`,
    `list_feature_flags`, `list_triage_queue`.
  - New write tools: `update_extraction_config`, `publish_extraction_config`, `set_feature_flag`,
    `propose_triage_fix`, `approve_triage_proposal`, `reject_triage_proposal`.
- **Mobile routes:** 143. New: `get_extraction_config`, `get_feature_flags`. `feature_flags` and
  `config_version` arguments are added to the validation, label and completion routes.
- **Migrate:**
  - new doctypes Extraction Config and Farm Feature Flag;
  - new fields on App Feedback, Document Validation and Farm Task Assignment;
  - seeds the three built-in configs as version 1 Published;
  - patch `triage_existing_feedback` auto-classifies open App Feedback.
- **iOS:** SERVER_CHANGES §53.
