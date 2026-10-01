# SPDX-License-Identifier: MIT
"""What a tap on one compliance alert should do, and where it should go.

THE GAP THIS CLOSES. `ComplianceAlertDetailView.swift` opened a signature pad
for the four alerts carrying a `signature_request` and a task screen for the
handful with a `linked_task`, and told the worker there was nothing else it
could do for every other alert type — correctly, because nothing else told it
what to do. This module is the missing map: one function, `describe_rectification`,
answering "what fixes THIS alert, and what do I call to start it" for every
alert type the app raises.

FOUR SHAPES OF ANSWER, in the order a reader should trust them:

  1. **A named endpoint that already exists and already works** — `submit_w4`,
     `collect_signature`, `submit_i9_section_2`, `reverify_i9`, `claim_task`.
     These alerts already carry everything a wizard needs (a `signature_request`,
     an employee to resolve), and routing straight at the shipped, tested, gated
     route is the whole fix. Building a second door to the same room would be a
     second set of rules to keep in step with the one `tools/w4.py` and
     `tools/signatures.py` already enforce.

     A DIRECT ROUTE OUTRANKS A TASK RECIPE FOR THE SAME ALERT, and seven alert
     types have both. `i9_expired` sits in `ALERT_TASK_MAP`, so the nightly
     sweep still raises an hr_admin task for it — right for a batch, wrong for a
     tap, and the difference is that `reverify_i9` IS Section 3 rather than a
     reminder to go and do Section 3. Where a direct route exists a tap takes
     it; the sweep is unchanged, and the two surfaces are allowed to differ
     because a list somebody works through and a button somebody presses are
     answering different questions.

  2. **A named endpoint built for this** — `renew_certification`,
     `record_training`, `sign_training_supervisor_review`,
     `update_regulatory_filing`, `advance_policy_review`. Five alert types whose
     fix is genuinely one small form (a new expiration, a completed course, a
     signature, a docket response, a review date) and nothing physical to walk
     to first. New mobile.py wrappers, one call, done.

  3. **`rectify_alert`, which raises the task the fix actually is** — every
     alert whose fix is real-world work first (walk the cabin, sample the
     water, test the detector, document the heat break) and a compliance
     record second. `materialize_task_for_alert` is the one new mechanism this
     needs; everything downstream — claiming, completing, filing the evidence,
     writing the record — is `complete_task_via_mobile`, which has shipped
     since Sprint 8. This module's job for that whole family is naming the
     verb a worker recognises ("Test the detectors") and pointing it at the
     one endpoint that raises the task.

  4. **A refusal that says which kind of refusal it is.** Five alert types are
     answered "no, and here is why" rather than left to fall through:
     `i9_retention_destruction_eligible`, whose fix is irreversible and gets
     reviewed before it is taken; the two threshold breaches
     (`financial_kpi_threshold_breach`, `budget_variance_breach`), which report
     a computed number crossing a line rather than a missing record; and, from
     v0.69.0, the two spray intervals (`rei_active_block_entry`,
     `phi_harvest_window`), whose fix is time passing. "A lawyer signs off on
     this", "this is a number, not a missing record", "this ends by itself at
     six o'clock" and "nobody has written this yet" are four different facts,
     and only the last invites somebody to go looking for the button.

AN ALERT TYPE WITH NEITHER — no explicit recipe here and no task recipe on
`tools/dispatch.ALERT_TASK_MAP` or its declarative twin — is answered honestly:
`can_rectify_mobile: false`, with the same sentence the desk-side tools already
give: it clears when the record behind it changes, on its own schedule.

THE MAP IS CLOSED AND `tests_standalone/test_rectify.py` KEEPS IT CLOSED, in
both directions: every rule this app seeds has an entry here, and every entry
here names a rule this app seeds. A rule shipped in a later release without a
rectification fails that file rather than reaching a handset as a dead end.
"""

from __future__ import annotations

import frappe

from ..tools import dispatch

#: Sidecar routes, exactly as `farmops_api/routes.py` mounts them. Used
#: verbatim as `action_endpoint` — never composed from a tool name, because
#: this table IS the map of which alert types may call which route, and a
#: composed path would let a typo in an alert_type silently open a different
#: door.
_SUBMIT_W4 = "/farmops/api/mobile/submit_w4"
_COLLECT_SIGNATURE = "/farmops/api/mobile/collect_signature"
_SUBMIT_I9_SECTION_2 = "/farmops/api/mobile/submit_i9_section_2"
_REVERIFY_I9 = "/farmops/api/mobile/reverify_i9"
_CLAIM_TASK = "/farmops/api/mobile/claim_task"
_RENEW_CERTIFICATION = "/farmops/api/mobile/renew_certification"
_RECORD_TRAINING = "/farmops/api/mobile/record_training"
_SIGN_SUPERVISOR_REVIEW = "/farmops/api/mobile/sign_training_supervisor_review"
_UPDATE_REGULATORY_FILING = "/farmops/api/mobile/update_regulatory_filing"
_ADVANCE_POLICY_REVIEW = "/farmops/api/mobile/advance_policy_review"
_RECTIFY_ALERT = "/farmops/api/mobile/rectify_alert"

#: What a caller is told when nothing here answers for an alert type. The same
#: fact `generate_tasks_from_compliance_alerts` reports per row and
#: `materialize_task_for_alert` refuses with — kept as one sentence here so the
#: three surfaces cannot drift onto three different explanations of one fact.
_NO_MOBILE_FIX = (
	"This clears when the record behind it changes, on the sweep's own schedule. There is "
	"no step this app can start from a phone for it yet."
)


def _clean(params: dict) -> dict:
	return {key: value for key, value in params.items() if value is not None}


def _no_fix(explanation: str = _NO_MOBILE_FIX) -> dict:
	"""The refusal shape, with a reason.

	ALWAYS AN OBJECT, NEVER A MISSING KEY, for the reason `shape.alert` states:
	a phone has to be able to tell "this app has no fix for that" from "this app
	did not decode the row", and only one of those two is worth a support call.
	The default sentence is the generic one; a builder that knows something more
	specific about WHY its alert type has no phone-side fix passes it, because
	"a lawyer signs off on destroying an I-9" and "nobody has written this yet"
	are different facts and the second wrongly invites somebody to go looking.
	"""
	return {
		"action_type": None,
		"action_label": None,
		"action_endpoint": None,
		"action_params": {},
		"can_rectify_mobile": False,
		"explanation": explanation,
	}


def _linked_field(doctype: str, docname, field: str) -> str | None:
	"""One column off one record, or None. Never raises — a deleted or unreadable
	source record is a reason to omit a prefill, not a reason to break the alert
	list it is one row of.
	"""
	docname = str(docname or "").strip()
	if not docname:
		return None
	try:
		return frappe.db.get_value(doctype, docname, field) or None
	except Exception:  # pragma: no cover - a site mid-migrate, a renamed field
		return None


def _w4_action(row: dict, employee: str | None, label: str) -> dict:
	return {
		"action_type": "submit_w4",
		"action_label": label,
		"action_endpoint": _SUBMIT_W4,
		"action_params": _clean(
			{
				"employee": employee,
				"company": row.get("company"),
				"tax_year": str(frappe.utils.getdate(frappe.utils.today()).year),
			}
		),
		"can_rectify_mobile": True,
	}


def _signature_action(row: dict, label: str) -> dict | None:
	request = row.get("signature_request") or {}
	docname = request.get("docname")
	field = request.get("signature_field")
	if not (docname and field):
		return None
	return {
		"action_type": "collect_form_signature",
		"action_label": label,
		"action_endpoint": _COLLECT_SIGNATURE,
		"action_params": _clean(
			{
				"doctype": request.get("doctype"),
				"docname": docname,
				"field": field,
			}
		),
		"can_rectify_mobile": True,
	}


def _task_action(action_type: str, label: str) -> dict:
	return {
		"action_type": action_type,
		"action_label": label,
		"action_endpoint": _RECTIFY_ALERT,
		"action_params": {},
		"can_rectify_mobile": True,
	}


# ── explicit builders, one per alert type this release names ────────────────
def _employee_missing_w4(row: dict) -> dict:
	employee = str(row.get("source_docname") or "").strip() or None
	return _w4_action(row, employee, "Submit a new W-4")


def _w4_tax_year_outdated(row: dict) -> dict:
	employee = _linked_field("W-4 Form", row.get("source_docname"), "employee")
	return _w4_action(row, employee, "Submit this year's W-4")


def _w4_signature_missing(row: dict) -> dict | None:
	return _signature_action(row, "Sign the W-4")


def _i9_section_1_unsigned(row: dict) -> dict | None:
	return _signature_action(row, "Sign I-9 Section 1")


def _i9_section_2_unsigned(row: dict) -> dict | None:
	return _signature_action(row, "Sign I-9 Section 2")


def _tax_form_signature_missing(row: dict) -> dict | None:
	return _signature_action(row, "Sign the tax return")


def _i9_verification_overdue(row: dict) -> dict:
	employee = _linked_field("I-9 Form", row.get("source_docname"), "employee")
	return {
		"action_type": "submit_i9_section_2",
		"action_label": "Complete I-9 Section 2",
		"action_endpoint": _SUBMIT_I9_SECTION_2,
		"action_params": _clean({"employee": employee}),
		"can_rectify_mobile": True,
	}


def _i9_supplement_b_unsigned(row: dict) -> dict | None:
	return _signature_action(row, "Sign I-9 Supplement B")


def _reverify_action(employee: str | None, label: str) -> dict:
	return {
		"action_type": "reverify_i9",
		"action_label": label,
		"action_endpoint": _REVERIFY_I9,
		"action_params": _clean({"employee": employee}),
		"can_rectify_mobile": True,
	}


def _i9_expired(row: dict) -> dict:
	"""`i9_expired` reads the status column ON THE EMPLOYEE, so `source_docname`
	IS the employee — no lookup, unlike `work_authorization_expiring` below,
	whose row is an I-9 Form. Two alert types, one endpoint, two different
	things in the same column; resolving each from its own rule's target
	doctype is why these are separate builders rather than one shared one.
	"""
	employee = str(row.get("source_docname") or "").strip() or None
	return _reverify_action(employee, "Re-verify work authorization")


def _work_authorization_expiring(row: dict) -> dict:
	"""Section 3 BEFORE the expiry, not after it — which is the whole point of a
	rule with a 90-day warning threshold. Same endpoint as `i9_expired`; the
	label is the difference, because a worker still authorized today should not
	be told their authorization has run out.
	"""
	employee = _linked_field("I-9 Form", row.get("source_docname"), "employee")
	return _reverify_action(employee, "Re-verify before the authorization expires")


def _field_flag_awaiting_dispatch(row: dict) -> dict:
	"""THE TASK ALREADY EXISTS — that is what the alert is complaining about.

	Every other task-shaped alert here raises work that has not been raised yet.
	This one fires on a field-reported Farm Task that has sat in Available for
	more than a day, so routing it at `rectify_alert` would answer an unclaimed
	task with a second unclaimed task. `source_docname` is the Farm Task itself,
	and claiming it is the fix.
	"""
	task = str(row.get("source_docname") or "").strip() or None
	return {
		"action_type": "claim_task",
		"action_label": "Claim this task",
		"action_endpoint": _CLAIM_TASK,
		"action_params": _clean({"task": task}),
		"can_rectify_mobile": True,
	}


def _flc_license_expiring(row: dict) -> dict:
	return _task_action("create_task", "Renew the labor contractor licence")


def _audit_action_overdue(row: dict) -> dict:
	return _task_action("create_task", "Close the auditor's corrective action")


def _i9_retention_destruction_eligible(row: dict) -> dict:
	"""NOT A PHONE ACTION, and this one is a deliberate refusal rather than a gap.

	The alert says an I-9 is now past both retention clocks and MAY be destroyed.
	Destroying it is `destroy_i9`, which is irreversible, role-gated, and the
	kind of decision that gets made once against a reviewed list — not tapped
	through on a handset by whoever happened to open the calendar.
	"""
	return _no_fix(
		"This one is cleared from a desk, on purpose. It reports that an I-9 is past its "
		"retention period and may now be destroyed — an irreversible, role-gated step that "
		"is reviewed before it is taken, not tapped through on a phone."
	)


def _financial_threshold_breach(row: dict) -> dict:
	"""A NUMBER MOVED, and no single act moves it back.

	`financial_kpi_threshold_breach` and `budget_variance_breach` both report a
	computed figure crossing a line an operator set. There is no form that fixes
	one, which is different from there being a form nobody has written — so it
	says which it is.
	"""
	return _no_fix(
		"This reports a computed figure crossing a threshold, not a missing record. It "
		"clears when the underlying numbers move or the threshold is re-set from the desk — "
		"there is no single step, on a phone or anywhere else, that answers it."
	)


def _certification_expiring(row: dict) -> dict:
	return {
		"action_type": "renew_certification",
		"action_label": "Renew the certificate",
		"action_endpoint": _RENEW_CERTIFICATION,
		"action_params": _clean({"certification": row.get("source_docname")}),
		"can_rectify_mobile": True,
	}


def _training_expiring(row: dict) -> dict:
	employee = _linked_field("Employee Training Record", row.get("source_docname"), "employee")
	return {
		"action_type": "record_training",
		"action_label": "Record the retraining",
		"action_endpoint": _RECORD_TRAINING,
		"action_params": _clean({"employee": employee, "company": row.get("company")}),
		"can_rectify_mobile": True,
	}


def _supervisor_review_lapsed(row: dict) -> dict:
	return {
		"action_type": "sign_training_supervisor_review",
		"action_label": "Sign the supervisor review",
		"action_endpoint": _SIGN_SUPERVISOR_REVIEW,
		"action_params": _clean({"training_record": row.get("source_docname")}),
		"can_rectify_mobile": True,
	}


def _training_day_missed(row: dict) -> dict:
	"""A MAKE-UP DAY IS BOOKED, NOT TAPPED. v0.212.0.

	The alert clears when the person attends a later session of the same course,
	and arranging that is the office's: there is no step on a phone that puts
	somebody in a class that has not been scheduled."""
	return _no_fix(
		"This clears when they attend a make-up day — a later session of the same course. "
		"Booking one is done from the training register, not from this alert."
	)


def _filing_response_due(row: dict) -> dict:
	return {
		"action_type": "update_regulatory_filing",
		"action_label": "File the agency's response",
		"action_endpoint": _UPDATE_REGULATORY_FILING,
		"action_params": _clean({"filing": row.get("source_docname")}),
		"can_rectify_mobile": True,
	}


def _policy_review_overdue(row: dict) -> dict:
	return {
		"action_type": "advance_policy_review",
		"action_label": "Record the policy review",
		"action_endpoint": _ADVANCE_POLICY_REVIEW,
		"action_params": _clean({"policy": row.get("source_docname")}),
		"can_rectify_mobile": True,
	}


def _housing_inspection_overdue(row: dict) -> dict:
	return _task_action("start_inspection_session", "Walk the cabin and record an inspection")


def _housing_detector_test_stale(row: dict) -> dict:
	return _task_action("create_detector_test", "Test the smoke and CO detectors")


def _housing_corrective_action_open(row: dict) -> dict:
	return _task_action("create_task", "Fix what the inspection found")


def _water_test_stale(row: dict) -> dict:
	return _task_action("create_water_test", "Take a water sample")


def _water_test_contamination(row: dict) -> dict:
	return _task_action("create_water_test", "Re-test the water source")


def _shift_heat_threshold_crossed(row: dict) -> dict:
	return _task_action("log_shift_event", "Document the water, shade and rest cycle")


# ── v0.69.0: two clocks nobody can shorten, and one shelf somebody can fill ──
#
# THE TWO INTERVAL RULES ARE DELIBERATE REFUSALS, and they are the clearest case
# in this module for why the honest "no" has its own sentence. An REI is over
# when it is over. There is no form to fill in, no signature to collect and no
# task to raise — a task asking somebody to make four hours elapse is exactly
# the item that teaches a crew to stop reading the board. What a phone SHOULD do
# with these is show them, which it already does; what it must not do is offer a
# button, because a button implies the worker can end the interval and the entire
# value of the alert is that they cannot.
def _rei_active_block_entry(row: dict) -> dict:
	return _no_fix(
		"Nothing ends a restricted-entry interval except the interval running out — there is "
		"no form here and no task worth raising. This alert clears BY ITSELF, to the hour, at "
		"the expiry in its message. Until then the block is posted and nobody goes in without "
		"PPE (40 CFR 170.407)."
	)


def _phi_harvest_window(row: dict) -> dict:
	return _no_fix(
		"A pre-harvest interval is not a task, it is a date: this clears by itself the day "
		"after the one in its message. What it is FOR is the picking plan — move the block "
		"back, and pick something else first. A pick inside the interval is a residue "
		"violation on a shipped load, and no action on this screen can shorten it."
	)


def _item_below_reorder(row: dict) -> dict:
	return _task_action("create_task", "Raise a task to reorder this item")


def _minor_hours_approaching(row: dict) -> dict:
	return _no_fix(
		"What answers this is a rostering decision about tomorrow — do not put this person on "
		"the crew, or send them home early — and there is no form on a phone for either. A Farm "
		"Task saying 'do not schedule somebody' is a card nobody can complete and evidence of "
		"nothing, which is why this rule ships without a task recipe. It clears BY ITSELF when "
		"the workweek turns, because the figure it counts resets. Until then: "
		"add_worker_to_shift will refuse to roster them past the ceiling, and clock_out_worker "
		"is how a day that is already close gets stopped."
	)


#: THE CLOSED MAP. Every key is an alert_type this release names a rectification
#: for; adding one is a code change on purpose, same reasoning as
#: `tools/signatures.SIGNATURE_BOXES` — this table is a claim about which route
#: a phone may call for which alert, and a caller-supplied mapping would let an
#: alert route itself somewhere this module never reviewed.
_BUILDERS = {
	"employee_missing_w4": _employee_missing_w4,
	"w4_tax_year_outdated": _w4_tax_year_outdated,
	"w4_signature_missing": _w4_signature_missing,
	"i9_section_1_unsigned": _i9_section_1_unsigned,
	"i9_section_2_unsigned": _i9_section_2_unsigned,
	"tax_form_signature_missing": _tax_form_signature_missing,
	"i9_supplement_b_unsigned": _i9_supplement_b_unsigned,
	"i9_verification_overdue": _i9_verification_overdue,
	"i9_expired": _i9_expired,
	"work_authorization_expiring": _work_authorization_expiring,
	"i9_retention_destruction_eligible": _i9_retention_destruction_eligible,
	"certification_expiring": _certification_expiring,
	"training_expiring": _training_expiring,
	"supervisor_review_lapsed": _supervisor_review_lapsed,
	"training_day_missed": _training_day_missed,
	"filing_response_due": _filing_response_due,
	"flc_license_expiring": _flc_license_expiring,
	"audit_action_overdue": _audit_action_overdue,
	"field_flag_awaiting_dispatch": _field_flag_awaiting_dispatch,
	"financial_kpi_threshold_breach": _financial_threshold_breach,
	"budget_variance_breach": _financial_threshold_breach,
	"policy_review_overdue": _policy_review_overdue,
	"housing_inspection_overdue": _housing_inspection_overdue,
	"housing_detector_test_stale": _housing_detector_test_stale,
	"housing_corrective_action_open": _housing_corrective_action_open,
	"water_test_stale": _water_test_stale,
	"water_test_contamination": _water_test_contamination,
	"shift_heat_threshold_crossed": _shift_heat_threshold_crossed,
	"rei_active_block_entry": _rei_active_block_entry,
	"phi_harvest_window": _phi_harvest_window,
	"item_below_reorder": _item_below_reorder,
	# v0.98.0. AN EXPLICIT REFUSAL RATHER THAN AN ABSENCE, which is what this map
	# is for: an alert type with no entry would fall through to the generic
	# "raise a task" fallback and produce a card reading "do not schedule this
	# person", and nobody can complete that. See `_minor_hours_approaching`.
	"minor_hours_approaching": _minor_hours_approaching,
}


def describe_rectification(row: dict) -> dict | None:
	"""The `rectification` object one alert row carries, or None for no row at all.

	Tried in the order the module docstring describes: this alert type's own
	named builder first, a generic "raise the task" fallback second for any
	alert type `tools/dispatch.py` already knows how to turn into work, and an
	honest refusal last. NEVER RAISES — a row this cannot describe gets the
	refusal shape rather than breaking the alert list it is one row of.
	"""
	if not row:
		return None
	alert_type = str(row.get("alert_type") or "").strip()
	builder = _BUILDERS.get(alert_type)
	if builder is not None:
		try:
			built = builder(row)
		except Exception:  # pragma: no cover - a source record this row's builder cannot read
			built = None
		if built:
			return built

	if dispatch.has_task_recipe(alert_type):
		return _task_action("create_task", "Raise a task to fix this")

	return _no_fix()
