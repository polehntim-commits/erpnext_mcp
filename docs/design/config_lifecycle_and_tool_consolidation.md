# One config lifecycle and fewer MCP tools

**Status: PROPOSED (2026-10-03). Not approved; nothing built.** Goal (Tim): every "flow data, not code"
feature behaves the same way — staged → previewed → published → rollback, versioned, AI proposes and a
human publishes, preview on real data — and the tool surface shrinks. Built in step 1 of the queue, with the
CCF core (`ccf_core_work_timing.md`), after the current offline work.

**At a glance**

- **Effort:** L — one patch copies existing versions into Farm Config Version; generic tools land beside the old ones before any are removed.
- **Risks:** Moving Compliance Rule / Inspection Template / Extraction Config history must not change what the sweep or phones serve (regression test asserts identical results); removing tools can break saved MCP prompts — old names stay as deprecated aliases for a release first.
- **Open questions for Tim:** Approve the replacement map and the switch trim; payroll settings preview by rerunning a recent payroll — which run is the reference? (Payroll transactions stay out of scope.).

## 1. The shared mechanism already exists

`Farm Config Version` (`phone_config.py`, v0.207.0) holds every version of every Wizard, Tile and Label
Profile: `Draft → (Staged →) Published → Superseded`, `Retired`, rollback; body hash fixed from Staged on;
staged rollout to users / roles / companies through a rollout flag; `authored_by`, `change_note`,
staged/published/retired by and on, `validation_json`; per-kind validators plus shared checks (size, schema
version, real audience, Spanish present); `seed()`.

| Kind | Store today | Missing vs Farm Config Version |
|---|---|---|
| Wizard, Tile, Label Profile | Farm Config Version | — |
| Extraction Config | own doctype (Draft/Staged/Published/Superseded, `staged_users`) | rollback, retire, hash, change note |
| Compliance Rule | own doctype (version + `superseded_by`, approve) | staging, rollback, hash |
| Inspection Template | own doctype (version + `superseded_by`, approve) | staging, rollback, hash; approval not enforced on phones |

## 2. Plan

- Farm Config Version becomes the single history and lifecycle record for **every** kind.
- For kinds whose engine reads its own doctype (rule sweep, inspection sessions, intake), **publish writes the
  version into that live row** through the existing supersede path — engines, alert docnames and references
  unchanged.
- Existing versions are copied into Farm Config Version as Published/Superseded history by an idempotent patch;
  live rows do not move; a regression test asserts identical sweep and serving results.

**Shared (one implementation)**: states and transitions; immutability; stamps + `change_note` (≥ 10 chars) +
`audit.record` on every transition; publish refuses an unvalidated body; preview arguments (`version` or unsaved
`patch`, `as_user`, `as_of`/`days`, `compare_to` live) on real data, writing nothing; staging audience (users,
roles, companies, **blocks**, **crews**); Spanish completeness where workers read it; System presets via
`seed()`.

**Per kind**:

| Kind | Preview runs | "Staged" means | Publish |
|---|---|---|---|
| wizard / tile / label_profile | render as a user / labels against real products | served to the staged audience | served to all |
| extraction_config | against a stored document | applied to staged users' documents | applied to all |
| inspection_template | render as a user | startable by the staged audience | live template (enforces approval on phones) |
| task_template / checklist | render form as a user, check answers | startable by staged audience | live template |
| compliance_rule / trigger_rule | `test` over one day / range / replay, vs live; loop check | **shadow mode**: runs on real data at its real times and logs what it would do (no push, block or task); or real only on staged blocks/crews | written into the live rule row; sweep picks it up |
| payroll settings (§4) | rerun a recent payroll live vs staged, per employee | never used by a real pay run; previews only | human-only, with `effective_from` |

Rollback keeps history attributable: alerts and `go_hold_log` keep the rule version; sessions keep their
template version; extracted documents keep their config version; pay runs keep their config versions.

## 3. Generic tools and the replacement map

**Eight generic tools, keyed by `kind`:**

| Tool | Type | Does |
|---|---|---|
| `list_configs` | read | versions and status by kind / key |
| `get_config` | read | one version: body, provenance, validation |
| `diff_config` | read | field diff of two versions, or draft vs live |
| `preview_config` | read | run on real data, write nothing (render / document / label / rule over days / loop / payroll rerun) |
| `draft_config` | write | create or patch a draft (JSON merge patch), `copy_from`; over MCP always AI-proposed |
| `stage_config` | write | staged audience / shadow mode |
| `publish_config` | write | publish under the kind's existing publish policy |
| `rollback_config` | write | back to a previous published version; `to: none` retires / deactivates |

Kinds: `extraction_config`, `wizard`, `tile`, `label_profile`, `inspection_template`, `task_template`,
`compliance_rule`, `trigger_rule` (a compliance rule in category Work Timing), `checklist`, `quiz`, and the
payroll settings kinds in §4.

| Family | Old tools | n |
|---|---|---|
| Phone config | `list_phone_configs`, `get_phone_config`, `stage_`/`publish_`/`rollback_`/`retire_phone_config` | 6 |
| Extraction | `list_extraction_configs`, `get_`/`stage_`/`preview_`/`publish_`/`update_extraction_config` | 6 |
| Wizard | `create_`/`update_wizard_definition`, `get_wizard_definition`, `list_wizard_definitions`, `preview_wizard` | 5 |
| Tile | `create_tile`, `update_tile`, `preview_tiles` | 3 |
| Inspection template | `create_`/`update_`/`approve_`/`deactivate_`/`preview_`/`get_inspection_template`, `list_inspection_templates`, `propose_inspection_template_from_regulation` | 8 |
| Compliance rule | `create_`/`propose_`/`approve_`/`update_`/`deactivate_`/`test_`/`get_compliance_rule`, `list_compliance_rules`, `preview_compliance_loop` | 9 |
| Label profile | `update_label_profile`, `preview_label_profile` | 2 |
| Farm Task Template | `create_`/`update_`/`get_`/`list_`/`preview_farm_task_template` | 5 |
| Payroll settings (§4) | 11 read + 11 write | 22 |
| **Total** | 31 read + 35 write | **66** |

**Kept** (not configuration): `get_compliance_field_map`, `audit_compliance_loop`, `approve_`/`reject_`/
`list_label_compliance` (product decisions), `create_task_from_template` and inspection-session tools, every
payroll transaction tool (§4), and **all mobile routes** (the app calls routes such as
`get_inspection_template`, the wizard, tile and remote-config routes — not MCP tools).

**Counts** (registry today: 1012 tools — 492 read, 456 write, 41 financial, 17 destructive, 6 credential):

| | Before | After |
|---|---|---|
| Tools | 1012 | **954** (−58) |
| Config write switches | 35 | **4** action switches + per-kind allow lists |
| Dangerous switches (credential/financial/destructive) | 64 | 64 — none of the replaced tools is in a dangerous tier; payroll transaction tools are untouched |

New kinds (checklist, trigger_rule, quiz, SOP gate, …) add **zero** tools.

## 4. Payroll

**Out of scope — transactions keep their dedicated tools and stricter controls**: `calculate_payroll`,
`calculate_payroll_taxes`, `preview_payroll`, `preview_payroll_for_period`, `preview_payroll_gl`,
`preview_total_payroll_taxes`, `preview_federal_withholding`, `preview_state_withholding`,
`run_payroll_for_period`, `submit_payroll`, `post_payroll_to_gl`, `generate_nacha_file`,
`generate_prenote_file`, `generate_tax_form`, `regenerate_tax_form`, `render_tax_form_pdf`,
`bulk_render_tax_form_pdfs`, `mark_tax_form_filed`; per-employee records (salary structures, W-4,
garnishments, deduction enrolments) and `configure_payroll_accounts`. A person releases funds and submits
filings.

**In scope — settings as kinds:**

| Kind | Store | Tools replaced |
|---|---|---|
| `federal_tax_table` | Federal Tax Table | `import_`/`get_federal_tax_table` |
| `state_tax_table` | State Tax Table | `import_`/`get_state_tax_table` |
| `state_tax_config` (incl. minimum wage) | State Tax Configuration | `create_`/`update_`/`get_`/`list_state_tax_config(s)` |
| `fica_config` | FICA Configuration | `get_`/`update_fica_config` |
| `piece_rate` | Piecework Rate | `create_`/`update_`/`get_`/`list_piecework_rate(s)` |
| `position_wage_default` | Position Wage Default | `create_`/`update_`/`get_`/`list_position_wage_default(s)` |
| `deduction_type` | Farm Payroll Deduction (definitions) | `create_`/`update_`/`get_`/`list_payroll_deduction(s)` |
| `overtime_rule` | **code today** (`payroll_calc.py`: 40 h/week, 1.5×, OR HB 4002 / WA SB 5172) | none — moves to data seeded with today's values; test asserts identical pay |

Payroll kinds are stricter:
- **Preview** = rerun a recent completed period (default latest) **twice**, live and staged, through the
  read-only `_period_run(creating=False)`; per employee hours, gross, OT premium, minimum-wage make-up, each
  withholding, deductions, net and employer taxes as live → staged (difference); totals; flag net changes over a
  threshold. Names and employee ids only; bank details masked to the last 4.
- New code: a config overlay (context variable, like `_CORPUS_EXCLUDE`) so payroll readers take staged values
  during a preview only.
- **Staged never touches a real pay run.** Every version carries `effective_from`; publish never changes a
  calculated or submitted payroll and is refused for an effective date inside a submitted period. Pay runs
  stamp the config versions used.
- **Publish is human-only** (Desk or phone, HR Manager / System Manager) whoever drafted; drafts may come over
  MCP (off by default, AI-proposed).

## 5. Switches

`allow_draft_config`, `allow_stage_config`, `allow_publish_config`, `allow_rollback_config` — off by default,
each with a per-kind allow list set in the Desk. Migration fills each list from the old per-tool switches that
were on, so access never widens. Payroll kinds can never be in the MCP publish list. Time-limited switches and
security alerts carry over.

## 6. Publish policy

`publish_config` dispatches to each kind's existing publish rule; **wherever publishing is human-only today it
stays human-only**. Work Timing rules and payroll kinds are human-only from the start. Whether every
AI-proposed version (wizards, tiles, labels included) should publish only in the Desk/phone is a separate open
decision.

## 7. Safe deprecation

1. Release N: generic tools ship; the 66 old names become thin aliases, descriptions prefixed
   "DEPRECATED — use `<generic>(kind=…)`"; every alias call is audit-logged.
2. Weekly alias-call report; removal after **14 days with no calls** and Tim's OK (release N+1 or N+2).
3. Callers checked 2026-10-03: iOS uses mobile routes only; no references in sidecars or other local repos
   (docs to update in N: README, CHANGELOG, tool catalogue, CCF doc, task-workflow doc, route mapping); the
   claude.ai routine **OML Nightly Receipt Processing** (22:00 PT) uses receipt and bank tools only; the
   "3:30 AM compliance brief" was **not found** (not in local scheduled tasks, routines, crontab, launchd; the
   server's 03:00 job is the KPI cache refresh) — to locate before removal. `prompt_templates.py` and server
   instructions are checked at build.
4. Remove aliases; update tool-count pins and the catalogue.
