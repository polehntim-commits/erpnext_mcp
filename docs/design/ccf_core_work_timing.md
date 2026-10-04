# CCF as the core: generic rules for Go/Hold, dates, checklists and no-work notices

**Status: PROPOSED (2026-10-03). Not approved; nothing built.** Tim approved the direction ("maximize reuse,
keep the Configurable Compliance Framework as the core; grow it with data, not code"). This note is the
contract to approve before any code. Companion: `config_lifecycle_and_tool_consolidation.md` (how rules are
staged, previewed and published). Decisions for Tim are collected in `open_decisions_2026_10.md`.

**At a glance**

- **Effort:** L — one server release for the core evaluator + providers, then mostly rules and phone screens per feature.
- **Risks:** A rule bug can wrongly Hold (or wrongly Go) real work — mitigated by shadow mode and a regression test that the 14 existing rules alert identically; forecast skill drops past ~7 days, so the 14-day strict variant is advisory; the 48 h dry-since figure is local judgment, not published.
- **Open questions for Tim:** Default pruning-window values OK as written? Enforced or Advisory at launch? Who may override a Hold (supervisor only?) — see open_decisions_2026_10.md.

## 0. The boundary

**Data only** (through the existing MCP rule tools, no release): new rules, thresholds, windows, schedules,
recipients, messages (EN/ES), which templates / crops / varieties / blocks / companies a rule applies to,
new task types, crops, regions and farms (the OR/WA sister farm is company scope + regimes + copied rules).

**Code only when adding** (1) a context provider (a new data source), (2) an action type, (3) an evaluation
moment, or (4) an operator or aggregate. Everything below is the one-time code that makes the boundary true.

## 1. What the CCF already has (reused unchanged)

| Piece | Today (`docs/configurable_compliance_framework.md`) |
|---|---|
| Rule record | Compliance Rule: `rule_id`, `version`, `superseded_by`, one live row per `rule_id`, track changes, `enabled`, `enforcement_mode` (Advisory/Enforced), company + `regimes`, `regulation_citations`, provenance (`authored_by`, `ai_source_citation`, `ai_review_flags`, `human_approved_by/on`) |
| Lifecycle tools | `create_` (draft), `propose_` (AI draft lands off, field diff, never touches the live row), `update_` (supersede, reason ≥ 10 chars), `approve_` (records the authenticated user; explicit acceptance of AI-written code), `deactivate_compliance_rule` |
| Dry run | `test_compliance_rule` — same code path as the sweep, writes nothing (today only) |
| Loop checks | `preview_compliance_loop` / `audit_compliance_loop` (alert → task → evidence → record) |
| Field map | `get_compliance_field_map` (compliance fields per doctype) |
| Operators | `gte gt lte lt eq ne`, `in nin`, `isnull isnotnull istrue isfalse`, an `any` group, defaults, four template variables |
| Settings references | `threshold_source` (Weather Settings heat / wind, per company) |
| Actions | alert (idempotent docname, auto-dismiss), produce task (`producer_task_template`, `producer_assigned_to_expression`, `evidence_contract_json`), `audit_packet_types`, `message_template` |
| Weather on shifts | `latest_child_field_threshold_json` over `Farm Shift Weather Reading` (`shift_heat_threshold_crossed`) |
| Engine | `alerts/base.py` sweep: deterministic, no model at sweep time; `custom_python` sandbox as escape hatch |

## 2. The generic evaluator (new)

Three new fields on Compliance Rule. Existing rules keep their primitive fields and run as today; a rule with
`condition_tree_json` set runs on the new evaluator. A regression test asserts identical alerts for the 14
existing rules before and after.

### 2.1 `condition_tree_json`
Grammar: `all` / `any` / `not` nodes; leaves `{id, path, op, value | value_source, agg?, reason{en,es}, basis,
source}`.

- **Operators**: the existing set plus `between`; type-aware — BBCH codes compare by the crop scale's order,
  dates and month-days as dates; units are fixed per path (in, °F, mph, h, days).
- **Aggregates over series**: `max min sum count_where any all` over index windows (`daily[0..6]`,
  `hourly[0..47]`).
- **Explains itself**: returns the failing leaf ids with actual value and threshold; Hold text is built from
  the leaves' `reason`.
- **Safe**: no `eval`; paths must be published by a provider; size and depth capped.
- **Validated** at create / propose / update: unknown path, type/operator mismatch, missing unit, bad
  `value_source` → refused, nothing written.
- **Basis on every number**: `basis: published | local_judgment`, `source` (citation or Reference Document id).

### 2.2 Context providers
Registered in code. Each declares its paths (type, unit, description, example), `resolve(subject, as_of)`,
cache policy, and whether it supports past days.

| Provider | Paths (examples) | Built from |
|---|---|---|
| `weather` | `forecast.daily[].{precip_in, precip_prob_pct, tmin_f, tmax_f, wind_mph, gust_mph, soil_temp_f}`, `forecast.hourly[]…`, `recent.hours_since_rain`, `recent.last_rain_end`, `recent.dry_streak_days`, `meta.{source, fetched_at, h3}` | `services/weather.py` + block forecast (§4) |
| `phenology` | `bbch`, `bbch_observed_at`, `bbch_age_days`, `variety`, `gdd` (phase 2) | Crop Observation + crop `bbch_scale` |
| `calendar` | `date mmdd day_of_year weekday days_to(mmdd)` | clock, site timezone |
| `task` | `state template start_date due_date blocked_by_open location assignee urgency` | Farm Task, Farm Task Link, template |
| `asset` | `type engine_hours last_service status` | Asset Register |
| `crew` | `members[] shift.open shift.start preferred_language[]` | Farm Shift, crew task members, Employee |
| `person` | `training.valid(type) certification.valid(name) quiz.passed(course) video.watched_pct(video)` | Training, Certification, Training Evidence |
| `sop` | `status version approved_on acknowledged(person)` | Compliance Policy, Signing Evidence |
| `checklist` | `today.answers.* today.done today.not_ok[]` | the Start/End of Day Farm Task's `form_answers` |
| `settings` | `weather.*` per company | existing `threshold_source` |

`get_compliance_field_map` gains a **`context`** section listing every path with type, unit, description,
example, whether the provider is enabled here, and whether it can be judged for a past day.

### 2.3 `evaluation_json`
`target` (existing `target_doctype` + scope filters) · `for_each` (fan-out per crew member / assignee) ·
`when` from a fixed set: `sweep`, `day_start`, `task_start`, `evening_cutoff`, `forecast_refresh` ·
`as_of`: today or tomorrow.

### 2.4 `actions_json` — the action vocabulary

| Action | Status |
|---|---|
| alert (always) | existing |
| `create_farm_task` | existing producer recipe |
| `add_to_audit_packet` | existing `audit_packet_types` / retention |
| `set_go_hold` | new — task Go/Hold + per-leaf reasons, phone and Desk |
| `block_start` | new — one guard in the start, claim and mobile-start paths |
| `require_override_reason` | new — reason + supervisor; written to `go_hold_log` with rule version and context snapshot |
| `require_checklist_item` | new — adds an item to that day's Start/End of Day check |
| `notify_workers` | new — no-work / resume notice, EN/ES, logged in Work Notice |
| `notify_supervisor` | new — push, and the evening "send / assign other work / hold" choice |

### 2.5 Testing on past days
`test_compliance_rule` gains `as_of`, `days`, a draft or unsaved `patch`, and `compare_to` live. "Would last
Tuesday have been Go?" returns Go/Hold per task and block with failing leaves. Weather replays **as decided**
(saved snapshots, default) or **as it turned out** (archive). Task/crew state for past days comes from
Version history and is marked approximate.

## 3. Go/Hold: calendar, weather and stage with equal weight

Each timing rule (category **Work Timing**) is a Compliance Rule on Farm Task. All checks a rule uses must
pass for **Go**; any failure is **Hold**, naming every failed check in a fixed order, e.g. "Hold: weather —
rain Thu–Sat (70%)", "Hold: stage — not yet at BBCH 55 (last seen 53 on Mar 2)". Missing data is Hold and
says so ("Hold: stage — no observation for Block 7 since Feb 10, record stage"). Several rules on one task:
all must pass; an Advisory rule warns only.

**Example — preset "Wounding work — bacterial canker"** (all values editable as rule data):

```jsonc
{"all": [
  {"id":"in_season","path":"calendar.mmdd","op":"between","value":["01-01","03-15"]},
  {"id":"dry_ahead","not":{"any":[
     {"path":"weather.forecast.daily[0..6].precip_in","agg":"max","op":"gte","value":0.01},
     {"path":"weather.forecast.daily[0..6].precip_prob_pct","agg":"max","op":"gte","value":40}]},
   "basis":"published","source":"PNW Plant Disease Management Handbook: no rain for at least a week after pruning",
   "reason":{"en":"Rain forecast — no pruning to protect the trees","es":"Pronóstico de lluvia — no se poda para proteger los árboles"}},
  {"id":"dry_since","path":"weather.recent.hours_since_rain","op":"gte","value":48,"basis":"local_judgment"},
  {"id":"no_freeze","path":"weather.forecast.hourly[0..47].temp_f","agg":"min","op":"gt","value":28},
  {"id":"stage","path":"phenology.bbch","op":"lt","value":"51"},
  {"id":"wood_dry","path":"checklist.today.answers.wood_dry","op":"eq","value":true},
  {"id":"no_overhead","path":"task.overhead_water_next_48h","op":"isfalse"}
]}
```

Strict variant (heading cuts on young trees): copy with `dry_ahead` at 14 days — basis published, Purdue
(dry weather predicted for at least two weeks); note that forecast skill drops past ~7 days.
Actions: `set_go_hold`, `block_start`, `require_override_reason` (reason + supervisor, flagged in the audit
packet), `require_checklist_item` (wood dry; overhead water), `notify_supervisor` ("Pruning window opens
[date]", "Window closing — rain expected [date]"), `notify_workers`.

**Other presets** (shipped as disabled System rules, copied and tuned; "verify" values need Tim):
Spraying (wind 2–10 mph verify; rain-fast hours from label else 4 h verify; 40–85°F verify; REI/PHI),
Burning (worker confirm "burn day confirmed with fire district"; wind ≤ 10 mph verify; outside fire season,
Wasco dates verify), Harvest heat (Hold at ≥ 85°F local judgment verify; Go 05:00–11:00; Jun–Aug).

**Overhead water**: an open task on the block from a template flagged `overhead_water` overlapping the next
48 h on a zone whose `sprinkler_type` is impact or gun; fallback a Start of Day question.

**Compliance snapshot**: at task start, each day's start, and the evening cutoff, `go_hold_log` (JSON on the
task) stores the forecast values used (source, fetch time, H3 cell), last rain end, BBCH with its
observation, overhead-water status, wood-dry answer, result and failed checks, any override (reason,
supervisor, who, when), the rule id and **version**, and a hash of the entry.

## 4. Weather source: reuse Open-Meteo through Weather Settings

Reused: `services/weather.py` (forecast, archive and geocoding endpoints from Weather Settings, timeouts,
per-location back-off, source labels), the 15-minute open-shift sweep (unchanged), `thresholds_for(company)`
(OML: heat 80°F, spray-block wind 15 mph), `Field.h3_cells`, `geo.py`.

New, inside the same service: `fetch_forecast(lat, lon)` on the same `/v1/forecast` URL with daily + hourly
precipitation, probability, min/max temperature, wind, gusts, soil temperature and `past_days` for recent
rain; a block sweep every 1–3 h (and before each evening cutoff) **only for blocks with an open task under an
enabled Work Timing rule**; a shared site cache keyed by H3 cell (resolution 7, ~5 km²); a season-clock JSON
summary on Field (last rain end, dry streak, 7-day outlook, fetched at). **No new tables.**

The archive runs about 5 days behind, so "hours since rain" comes from `past_days`; the archive fills older
gaps, history rebuilds, and an optional "what actually happened" check a week after wounding work.

**Two homes for thresholds:** farm-wide safety/ops limits (heat, spray wind, per-company overrides, the new
fetch settings, forecast sweep on/off — **off by default**) stay in Weather Settings, **Desk only, no
`update_weather_settings` tool**; agronomic values live in the rule and go through propose → test → approve.
A rule can point at a Weather Settings value with `value_source`.

## 5. Stage (BBCH) capture — one timeline per block and variety

Reused: Crop Observation (`observation_type` incl. Growth Stage, `growth_stage_code`, `crop_stage`, `source_task`,
photo, `observed_at`), `bbch.py` (parse, validate, picker from a crop scale), `overlays.bbch_band`, the
completion-flow scouting picker, `create_crop_observation`.

New: `bbch_scale` JSON on Crop (the field `bbch.py` already expects; seed a sweet-cherry scale, EN/ES names,
optional reference photo, named stages such as "bud swell", "straw color"); Crop Observation `variety`,
`stage_context` (Scouting / Task start / Task end of day / Stage check), `client_request_id`, phone time +
receive time; `create_crop_observation` stops requiring `threat` for General / Growth Stage.

- **Every observation** carries a stage on the phone (pre-filled with the block's last stage, one tap to
  confirm). Over MCP a missing stage is accepted but flagged.
- **Work tasks** from templates with `records_stage` ask at start and in the End of Day check; each confirmation
  files a Crop Observation with `source_task`; the task keeps a `stage_readings` JSON.
- **Picker**: server scale, grouped by principal stage, code + plain name (EN/ES) + photo, "Same as last time"
  on top; cached by Prepare for offline; saves queue offline.
- **Timeline**: `get_block_stage_timeline(block, variety, season)` (MCP read + phone route). A backwards stage is
  kept and flagged "check". Feeds the `phenology` provider, the season clock, a "current stage" map layer per
  variety, the task record, block history and the audit packet.
- Optional degree-day forecast (phase 2) predicts arrival only; only an observed stage passes the check.

## 6. Start/due dates, overdue, dependencies, recurrence — as rules

| Feature | Rule | New code |
|---|---|---|
| Dates | — | `start_date`, `due_date`, `starts_after` (text; "Waiting: …" chip) on Farm Task; `default_start_after_days`, `default_due_after_days` on templates |
| Overdue + reminders | Farm Task, `date_field = due_date`, Clock role, warning band = lead days (default 2), critical at 0 → alert + `notify_*` (assignee, supervisor) at 06:00 site time | Overdue badge computed on the phone from cached dates (offline); Today "Overdue / due soon"; Desk indicator; tile; `list_overdue_tasks` read |
| Blocked-by | `related_latest` on `Farm Task Link` `blocked_by` with blocker state ≠ Completed → `block_start` | `shift_dates` action; `shift_dates`, `lag_days` on the link |
| Recurring | existing `cadence_days` + `create_farm_task`, one open instance | none |
| Trigger windows | §3 rules fill estimated `start_date` / `due_date`; a missed window shows overdue | — |

## 7. Daily Start/End of Day checklists — checks are Farm Tasks

- Rule: `for_each` crew member on an in-progress task or open shift without today's Start of Day check →
  `create_farm_task` (Start of Day template) + `require_checklist_item` (from other rules) + optional
  `block_start`. End of Day at close-out / end of shift. Once per person per day.
- The check **is a Farm Task** completed with form_schema v2 (EN/ES, photo, `show_if`/`required_if`, server
  `check_answers`), so offline, idempotent submit and per-photo durability are reused. No Daily Check doctype.
- "All OK" sets every item; Not OK needs a note and a photo. A second rule on the completed check: any Not OK
  → `create_farm_task` (field-initiated) + `notify_supervisor`. No daily sign-off.
- Composition (new code): base template + add-ons by asset type (e.g. Crawler Dozer for the D-6C) + the work
  template's add-ons. End of Day records engine hours per asset through the existing `creates_record` path.
- Records roll into asset history and the audit packet.

## 8. No-work notices

- Evening run at each crew's cutoff (default 18:00) judges **tomorrow**; only an **Enforced** Work Timing Hold
  sends anything (Advisory never does).
- Supervisor first (default one hour earlier): send the notice / assign other Go work ("No pruning; brush pickup
  available, Block 9, 7 am") / hold. No answer → the crew's setting (auto-send default).
- Recipients: assignee, crew task members, tomorrow's dispatched workers; language from
  `Employee.preferred_language` and the translation strings. "Next possible" = first forecast day where all
  rules pass; "we'll confirm" names the evening before. Reopen notice when Hold → Go.
- Today screen card (cached offline). **Work Notice** doctype + recipient rows (worker, language, text, sent,
  delivered, opened, rule id + version, task, crew, shift, supervisor's choice) — labor evidence, in the audit
  packet. Workers with no push device are listed to the supervisor as "not reached — call".
- Settings: off by default; cutoff, supervisor lead, auto-send; per-crew overrides on the crew task.
- MCP: `list_work_notices` (read), `send_work_notice` (write, off).

## 9. Other features mapped (built later, listed for completeness)

SOP gating (`sop` provider → `block_start`; see `sop_review_and_approval.md`); training prerequisites
(`person` provider → `block_start` on claim/start + `create_farm_task` "schedule training"; see
`training_quiz_and_video.md`); Reference Library citations as a leaf's `source`.

## 10. Build order (each step after the core is mostly rules + phone screens)

1. CCF generic core (§2) + shared config lifecycle and tool consolidation.
2. Dates, overdue, blocked-by, recurring (§6).
3. Daily checklists (§7).
4. Reference Library.
5. Triggers phase 1: Go/Hold, weather fetch, stage capture, presets, no-work notices (§3–5, §8) — live by late
   December for dormant pruning.
6. SOP approval and gating. 7. Training quiz and video. 8. Upload links. 9. Degree-day prediction.
