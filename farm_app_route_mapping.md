# farm_app → ERPNext route mapping

**Purpose.** The reference for the iOS cutover: what the phone calls today, what
answers it after `fafo-farm-app` stops, and what has no equivalent at all.

**Status vocabulary**, used in every table below:

| Status | Meaning |
| --- | --- |
| `ready` | An ERPNext endpoint or tool exists and is published today |
| `missing` | No ERPNext equivalent is published; something would break or stay broken |
| `n-a` | No migration needed — excluded by decision, or Frappe provides it natively |

**Measured, not estimated.** Every count here came from the trees on this
machine on 2026-08-22, not from a guess:

- `farm_app` defines **1,051** route decorators under `app/` (excluding
  `.claude/worktrees/` copies).
- `fafo_ios` calls **144** distinct mobile methods, all built through one
  constant — `MobileAPI.namespace = "farmops/api/mobile"` in
  `FarmOpsKit/Sources/FarmOpsKit/Networking/MobileAPI.swift`.
- `erpnext_mcp` publishes **202** `/mobile` routes in
  `erpnext_mcp/farmops_api/routes.py`, plus 2 under `/files`.

---

## 1. What iOS actually calls — the cutover-critical table

This is the part that decides whether the phone keeps working. iOS reaches four
distinct surfaces, and **only two of them are `farm_app`**.

| Surface | Host today | Calls | ERPNext equivalent | Status |
| --- | --- | --- | --- | --- |
| `/farmops/api/mobile/<method>` | ERPNext (`erpnext_mcp`) | 144 methods | Already ERPNext — same host, same path | `ready` |
| `/farmops/api/files/stage_file_chunk` | ERPNext (`erpnext_mcp`) | 1 | Already ERPNext | `ready` |
| `/api/resource/<doctype>`, `/api/method/upload_file` | ERPNext (Frappe native) | 3 | Already ERPNext | `ready` |
| `/api/wallet/*` | **farm_app** (Tor + NIP-98) | 5 | None — excluded by decision | `n-a` |
| `/api/field-ops/company/logo` | **farm_app** (Tor + NIP-98) | 1 | None — excluded by decision | `n-a` |

### The base URL does not change for the mobile API

iOS already points its mobile traffic at ERPNext. `MobileAPI.namespace` is
`farmops/api/mobile`, served by `erpnext_mcp/farmops_api/routes.py` — not by the
Flask app. **No iOS base-URL edit is required to retire `farm_app`**, which is
the single most important fact in this document.

### The two farm_app calls that remain, and why they are `n-a`

Both are Tor-only, both authenticate with a NIP-98 signed event, and both sit
squarely inside the excluded set (Nostr-tied wallet pass; Tor; NIP-44/NIP-98
crypto):

| iOS call site | Path | Behaviour when farm_app stops |
| --- | --- | --- |
| `FarmCore/.../Wallet/WalletPassService.swift` | `/api/wallet/config`, `/pass/<employeeId>`, `/status`, `/status/<employeeId>`, `/revoke/<passId>` | Guarded by `guard let baseURL = torBaseURL else { return }` — no onion, no call, no error |
| `FarmCore/.../Reference/ReferenceDataStore.swift` | `/api/field-ops/company/logo` | Guarded on `TorService.onionAddress`; logo falls back to the on-disk cache |

**Neither throws in a worker's face.** Both already return early when no onion
address is configured, so on a clearnet install they are dead code today. The
Apple Wallet badge pass and the server-fetched company logo are the two features
that go dark at cutover; the cached logo persists.

### iOS methods with no published route — pre-existing gaps, not cutover risk

Ten of the 144 methods iOS calls have **no** route in either
`erpnext_mcp/api/mobile.py` or `farmops_api/routes.py`, so they 404 today and
will 404 after cutover. Retirement neither causes nor fixes these.

| Method | iOS handling | Status |
| --- | --- | --- |
| `trace_forward`, `trace_backward` | `TraceAPI.swift` documents both as probed-404 on 2026-08-18; the MCP tools `trace_lot_forward`/`trace_lot_backward` exist but are not wrapped onto the sidecar | `missing` |
| `create_heat_exposure_event`, `list_heat_exposure_events` | `SafetyAPI.swift` states the reason at length: the routes were never wrapped, and `create_heat_exposure_event` still requires an HR role and keys on a Farm Shift docname | `missing` |
| `create_detector_test` | No wrapper | `missing` |
| `create_housing_inspection` | No wrapper | `missing` |
| `get_inspection_template`, `list_inspection_templates` | No wrapper (the MCP tools exist) | `missing` |
| `get_active_rei` | No wrapper (the MCP tool exists) | `missing` |
| `generate_mobile_login_qr` | No wrapper (the MCP tool exists) | `missing` |

iOS translates a 404 into a typed `Unavailable` state rather than an error
banner — see `SafetyAPI.Unavailable` and its siblings, which deliberately
translate 404 and **only** 404, leaving a 403 as a 403. Closing these is a
separate piece of work from retiring `farm_app`; most are a wrapper in
`api/mobile.py` plus a line in `routes.py`.

---

## 2. Food safety / HACCP — built in Cycle 3

`farm_app`'s `food_safety` blueprint (`url_prefix='/food-safety'`) is **24 route
decorators** of server-rendered forms — not a REST API. iOS never called any of
them; they were reached through the Flask web UI. The Cycle 3 tools cover the
same eight tables.

| farm_app route | Method | ERPNext equivalent | Status |
| --- | --- | --- | --- |
| `/food-safety/` | GET | `list_food_safety_plans` | `ready` |
| `/food-safety/create` | GET, POST | `create_food_safety_plan` | `ready` |
| `/food-safety/<plan_id>` | GET | `get_food_safety_plan` | `ready` |
| `/food-safety/<plan_id>/edit` | GET, POST | `update_food_safety_plan` | `ready` |
| `/food-safety/<plan_id>/export-pdf` | GET | None | `missing` |
| `/food-safety/<plan_id>/hazards` | GET | `list_hazard_analyses` | `ready` |
| `/food-safety/<plan_id>/hazards/create` | GET, POST | `create_hazard_analysis` | `ready` |
| `/food-safety/<plan_id>/hazards/<hazard_id>/edit` | GET, POST | `update_hazard_analysis` | `ready` |
| `/food-safety/<plan_id>/hazards/<hazard_id>/delete` | POST | None — deletion is a Desk action | `n-a` |
| `/food-safety/<plan_id>/controls` | GET | `list_preventive_controls` | `ready` |
| `/food-safety/<plan_id>/controls/create` | GET, POST | `create_preventive_control` | `ready` |
| `/food-safety/<plan_id>/controls/<control_id>/edit` | GET, POST | `update_preventive_control` | `ready` |
| `/food-safety/<plan_id>/monitoring` | GET | `list_monitoring_records` | `ready` |
| `/food-safety/<plan_id>/monitoring/create` | GET, POST | `create_monitoring_record` | `ready` |
| `/food-safety/<plan_id>/corrective-actions` | GET | `list_corrective_action_records` | `ready` |
| `/food-safety/<plan_id>/corrective-actions/create` | GET, POST | `create_corrective_action_record` | `ready` |
| `/food-safety/<plan_id>/corrective-actions/<ca_id>/close` | POST | `update_corrective_action_record` (status → Closed) | `ready` |
| `/food-safety/<plan_id>/verification` | GET | `list_verification_records` | `ready` |
| `/food-safety/<plan_id>/verification/create` | GET, POST | `create_verification_record` | `ready` |
| `/food-safety/<plan_id>/recall-plan` | GET, POST | `get_recall_plan`, `create_recall_plan`, `update_recall_plan` | `ready` |
| `/food-safety/<plan_id>/suppliers` | GET | `list_supplier_verifications` | `ready` |
| `/food-safety/<plan_id>/suppliers/create` | GET, POST | `create_supplier_verification` | `ready` |
| `/food-safety/<plan_id>/dashboard` | GET | `get_food_safety_dashboard` | `ready` |
| `/food-safety/<plan_id>/mrl-status` | GET | `list_mrl_records`, `get_mrl_for_chemical_crop_market` (Cycle 2) | `ready` |

**Two deliberate non-goals.** There is no plan-PDF tool — an audit packet is its
own concern and this app already has `generate_audit_packet` and
`generate_compliance_packet` to model it on, so an FSMA plan export belongs
there rather than bolted to a CRUD module. There is no delete tool: none of this
app's registers expose one, and a deleted hazard row is exactly the thing an
auditor's copy of the plan is supposed to still contain.

Additionally, `get_supplier_verification`, `update_supplier_verification` and
`update_recall_plan` exist as tools without a matching single-purpose Flask
route — the ERPNext surface is slightly wider than the one being retired.

---

## 3. Applicant tracking — mostly covered by Frappe HR

| farm_app route | ERPNext equivalent | Status |
| --- | --- | --- |
| `/applicants/apply` | Frappe HR **Job Applicant** DocType | `ready` |
| `/applicants/manage-roles` | Frappe role management (Desk) | `ready` |
| `/job-titles/` | **Designation** DocType (`create_designation`, `list_designations`) | `ready` |
| `/job-titles/<id>` | `update_designation`, Desk CRUD | `ready` |
| `/job-titles/<id>/members` | `list_employees` filtered by designation | `ready` |
| `/hiring-cohorts/*` | None — H-2A batch onboarding has no Frappe HR analogue | `missing` |
| `/api/tiers` (FeatureTier) | Frappe's own role/permission system supersedes it | `n-a` |

**Notes.**

- Frappe HR ships `hrms`, and `Job Applicant` → `Job Offer` → `Employee` already
  models the pipeline. Confirmed: `create_employee`, `onboard_employee` and the
  Employee register are live in this app today.
- The `Application` model in `farm_app` stores PII in the **vault**, which is
  excluded from migration by decision. Any future applicant import must
  re-collect or re-key that data rather than carry the vault across.
- **HiringCohort is a real gap.** It batches H-2A workers through onboarding
  together, and nothing in Frappe HR groups applicants that way. It is deferred,
  not covered — call it out before anyone assumes parity.
- `JobTitle`'s extra columns (PPE requirements, training prerequisites, pay
  range, safety protocols) would be custom fields on Designation. Not done.

---

## 4. Everything else, by blueprint class

`farm_app`'s remaining ~980 routes are overwhelmingly server-rendered web pages
for a UI that the iOS-first architecture replaces. Grouped by what they do:

| farm_app area | ERPNext equivalent | Status |
| --- | --- | --- |
| Employees | `create_employee`, `get_employee`, `update_employee`, `list_employees`, `onboard_employee` | `ready` |
| Clock / shifts | `start_shift`, `end_shift`, `get_shift`, `list_shifts`, `add_worker_to_shift`, `log_shift_break` | `ready` |
| Payroll / tax | `run_payroll_for_period`, `preview_payroll`, `generate_tax_form`, `get_941_prefill`, NACHA tools | `ready` |
| Accounting / banking | Full GL, JE, bank reconciliation and statement tool set | `ready` |
| Compliance | `list_compliance_rules`, `refresh_compliance_alerts`, `get_compliance_calendar` | `ready` |
| Training | `create_training_session`, `record_training`, `get_training_compliance_report` | `ready` |
| Assets | `create_asset`, `register_asset`, `scan_asset`, `get_asset_detail` | `ready` |
| Fields / crops | `create_field`, `list_fields`, `create_crop_observation`, boundary tools | `ready` |
| Spray / pesticide | `create_spray_application`, `get_spray_application_report`, `get_active_rei` | `ready` |
| Housing | `create_housing_unit`, `create_housing_assignment`, `create_housing_inspection` | `ready` |
| Inspections | `start_inspection_session`, `submit_inspection_session` | `ready` |
| Receipts / scale tickets | `submit_expense_receipt`, `create_scale_ticket` | `ready` |
| Weather | `fetch_weather_now`, `get_weather_timeline` | `ready` |
| Traceability | `trace_lot_forward`, `trace_lot_backward`, `get_lot_timeline` (MCP only — see §1) | `ready` |
| IoT | `create_iot_device`, `list_iot_readings`, `get_device_readings` (v0.118.0) | `ready` |
| Competitive intel / strategy | `create_competitive_move`, `create_strategic_plan`, acquisition-target tools (v0.118.0) | `ready` |
| MRL | `list_mrl_records`, `get_mrl_for_chemical_crop_market` (v0.118.0–0.121.0) | `ready` |
| Satellite / phenology | Satellite Metric tools, BBCH, NDVI anomaly (v0.119.0–0.121.0) | `ready` |
| `/auth/*` | Frappe native authentication | `n-a` |
| `/admin/*` | ERPNext Desk | `n-a` |
| `/portal/*`, templates, `/static/*` | Not needed — iOS-first, Frappe serves its own assets | `n-a` |
| `vision_labeling` (18 routes) | Volume Vision's own app | `n-a` |
| `nostr_api` (15 routes) | Excluded by decision | `n-a` |
| Vault / encryption | Excluded by decision | `n-a` |
| Merkle proofs, Tor backup sharding | Excluded by decision | `n-a` |

---

## 5. Summary

| Category | Count | Status |
| --- | --- | --- |
| iOS mobile methods already served by ERPNext | 144 | `ready` |
| iOS methods that 404 today and still will | 10 | `missing` (pre-existing) |
| iOS calls still reaching farm_app (Tor + NIP-98) | 6 | `n-a` (excluded; go dark) |
| farm_app food-safety routes | 24 | 22 `ready`, 1 `missing` (PDF), 1 `n-a` (delete) |
| Applicant tracking | 9 | 5 `ready`, 1 `missing` (H-2A cohorts), 1 `n-a` |
| farm_app total route decorators | 1,051 | — |

**The retirement verdict: iOS needs no URL change.** Its mobile API already
points at ERPNext. What is lost at cutover is the Apple Wallet pass and the
server-fetched company logo, both Tor-only, both excluded by decision, both
already written to fail silently. The two things worth deciding *before* the
day are the H-2A hiring-cohort gap and whether the FSMA plan PDF matters.
