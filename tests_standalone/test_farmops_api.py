# SPDX-License-Identifier: MIT
"""farmops-api — the third transport, v0.18.0.

`test_api_mobile.py` drives the eleven methods as Python functions with a
session already set, which is faithful to the whitelisted path: Frappe
authenticates before a whitelisted method runs, so what reaches that code IS a
session. `test_fallback_auth.py` drives the same methods from headers and a body
with the session at Guest, which is faithful to what the funnel delivers.

**This file drives them over HTTP, through a real WSGI stack, with no Frappe
request handler anywhere in the picture** — because that is what v0.18.0 puts in
front of a phone, and a transport that is only ever called in-process is a
transport nobody has tested.

EIGHT CLAIMS.

1. **THE SURFACE IS THE SAME ELEVEN AND CANNOT BE MORE.** `TheSurfaceIsClosed`.
   The route table is checked against `api/mobile.py` and `api/files.py` in BOTH
   directions, so neither a twelfth route nor a twelfth method can arrive
   quietly, and no route can come to point at something ungated.

2. **A CREDENTIAL GETS IN AND NOTHING ELSE DOES.** `TheDoorOpens`,
   `TheDoorStaysShut`. Valid token, missing header, malformed header, unknown
   key, wrong secret, disabled account — one 200 and five identical 401s.

3. **THE FALLBACK IS A DOOR, NOT A BYPASS — AGAIN, ON A NEW TRANSPORT.**
   `TheSevenGatesStillRun`. Role gate, Mobile Access Grant, entity scoping, kill
   switch and rate limit are each asserted against a credential that is
   otherwise perfectly valid, because "it delegates to the guarded functions" is
   a claim and this is the check.

4. **THE ANSWER IS ALWAYS JSON.** `ItIsAlwaysJson`. Every status this service
   can produce — 200, 401, 403, 404, 405, 429, 500 — is asserted to carry
   `Content-Type: application/json` and to parse. The v0.17.x failure was an
   HTML login page where the app expected JSON; a 500 that rendered Werkzeug's
   own HTML page would reproduce it exactly.

5. **THE STATUS CODES ARE THE APP'S CONTRACT.** `TheStatusCodesMatterToThePhone`.
   401 signs a phone out and loses its offline queue, so the kill switch answers
   503 and the rate limit answers 429 and neither is allowed to drift into 401.

6. **THE RESPONSE IS BYTE-IDENTICAL TO THE OLD PATH'S.** `ByteIdentical`. The
   whole security argument of this release is "it is the same code" — this is
   the property that makes that checkable rather than claimed, run over all
   eleven methods with shared fixtures.

7. **THE ARGUMENT FILTER FRAPPE WAS DOING IS STILL BEING DONE.**
   `TheArgumentFilter`. `record_data`, `worker_id`, `cancel` and `user` are the
   four arguments the wrappers deliberately do not accept; a body carrying them
   must reach the tool layer without them rather than 500 on an unexpected
   keyword.

8. **EVERY CALL LEAVES A ROW AND NO CALL LEAVES A SECRET.** `TheAuditRow`,
   `NoSecretReachesThePhone`.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import ClassVar

import frappe
from werkzeug.test import Client
from werkzeug.wrappers import Response

from erpnext_mcp import audit, task_templates
from erpnext_mcp.api import fallback_auth, guard
from erpnext_mcp.api import files as files_api
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.farmops_api import PREFIX, ROUTES
from erpnext_mcp.farmops_api import app as farmops_app
from erpnext_mcp.farmops_api import routes as farmops_routes
from erpnext_mcp.farmops_api import session as farmops_session

from .fixtures import MAIN, OTHER, install_hrms
from .harness import ROLES, STORE, set_roles
from .test_api_mobile import (
	OUTSIDER,
	OUTSIDER_EMPLOYEE,
	WORKER,
	WORKER_EMPLOYEE,
	MobileAPITestCase,
)
from .test_dispatch import WALK

#: What the phone will call after the v0.18.0 iOS change.
CONTEXT = f"{PREFIX}/mobile/get_current_user_context"
MY_TASKS = f"{PREFIX}/mobile/list_my_tasks"


class FarmOpsAPITestCase(MobileAPITestCase):
	"""One enrolled worker, and a WSGI client that calls the service as them."""

	def setUp(self):
		super().setUp()
		self.client = Client(farmops_app.application, Response)
		fallback_auth._FAILURES.clear()
		self.addCleanup(fallback_auth._FAILURES.clear)

	def enrol(self, email=WORKER, name="Ana Ramos", role="Field Worker", entities=None):
		"""Enrol, and KEEP THE PAIR — it is readable exactly once, at enrolment."""
		data = super().enrol(email=email, name=name, role=role, entities=entities)
		credential = {"api_key": data["api_key"], "api_secret": data["api_secret"]}
		if email == WORKER:
			self.credential = credential
		return credential

	# ── calling it ──────────────────────────────────────────────────────────
	def post(self, path, body=None, credential=None, token=None, headers=None, method="POST"):
		"""One HTTP call at the service. Returns the werkzeug response.

		The session is set to Guest first, every time, because that is the state
		a request arrives in when nothing has authenticated it — which is every
		request to this service, since Frappe's auth layer is not in the path at
		all. A test that inherited the previous call's user would be testing
		nothing.
		"""
		frappe.local.session = frappe._dict(user="Guest", data=frappe._dict())
		sent = {"Content-Type": "application/json"}
		if token is None and credential is not False:
			pair = credential or self.credential
			token = f"{pair['api_key']}:{pair['api_secret']}"
		if token:
			sent[fallback_auth.HEADER] = token
		sent.update(headers or {})
		return self.client.open(
			path,
			method=method,
			data=json.dumps(body or {}),
			headers=sent,
			environ_base={"REMOTE_ADDR": "100.64.0.7"},
		)

	def payload(self, response):
		self.assertEqual(response.headers["Content-Type"], "application/json")
		return json.loads(response.get_data(as_text=True))

	def message(self, path, body=None, **kwargs):
		"""Call, assert 200, and return the unwrapped `message` — what iOS decodes."""
		response = self.post(path, body, **kwargs)
		body_json = self.payload(response)
		self.assertEqual(response.status_code, 200, body_json)
		self.assertIn("message", body_json)
		return body_json["message"]

	def refusal(self, path, body=None, **kwargs):
		"""Call, assert it failed, and return (status, the sentence the phone shows)."""
		response = self.post(path, body, **kwargs)
		parsed = self.payload(response)
		self.assertGreaterEqual(response.status_code, 400, parsed)
		return response.status_code, parsed


# ── 1. the surface is closed ────────────────────────────────────────────────
class TheSurfaceIsClosed(FarmOpsAPITestCase):
	EXPECTED: ClassVar[set[str]] = {
		"/mobile/get_current_user_context",
		"/mobile/list_my_tasks",
		"/mobile/list_available_tasks",
		"/mobile/get_task",
		"/mobile/claim_task",
		"/mobile/start_task",
		"/mobile/complete_task_via_mobile",
		"/mobile/reject_task",
		"/mobile/report_field_task",
		"/mobile/list_compliance_alerts",
		"/mobile/scan_asset",
		"/mobile/get_asset_detail",
		"/mobile/log_asset_state_change",
		"/mobile/get_available_actions",
		"/mobile/report_asset_issue",
		# v0.46.0 — the Identity step the wizard 404'd on.
		"/mobile/create_employee",
		"/mobile/search_employees",
		"/mobile/reactivate_employee",
		# v0.46.2 — the returning worker's branch, between the search and the rehire.
		"/mobile/get_employee",
		# v0.45.0 — onboarding, the bucket sync and the crew clock.
		"/mobile/create_i9_form",
		"/mobile/submit_i9_section_1",
		"/mobile/submit_i9_section_2",
		# v0.47.0 — the I-9's document lookup and its Section 3.
		"/mobile/list_i9_document_types",
		"/mobile/reverify_i9",
		# v0.47.1 — the I-9 read, the federal PDF, and the signed copy coming back.
		"/mobile/get_i9_form",
		"/mobile/generate_i9_pdf",
		"/mobile/upload_signed_i9",
		# v0.48.0 — who may put their name on Section 2, and the three calls
		# that maintain that list.
		"/mobile/list_authorized_signers",
		"/mobile/add_authorized_signer",
		"/mobile/update_authorized_signer",
		"/mobile/remove_authorized_signer",
		"/mobile/submit_w4",
		# v0.48.0 — the W-4 as a federal form rather than a doctype.
		"/mobile/generate_w4_pdf",
		"/mobile/link_badge_to_employee",
		# v0.50.0 — the read between a scan and a name.
		"/mobile/resolve_badge",
		# v0.51.0. Issuing a badge from the handset, and the headshot that makes
		# the printed card carry a face instead of two initials.
		"/mobile/generate_employee_badge_qr",
		"/mobile/set_employee_photo",
		# v0.53.0. The same badge delivered to the wallet the worker already
		# carries: a `.pkpass` the foreman AirDrops off the handset, and the
		# Google Wallet save link for the Android half.
		"/mobile/get_employee_badge_pass",
		"/mobile/sync_bucket_entries",
		"/mobile/start_shift",
		"/mobile/add_worker_to_shift",
		"/mobile/end_shift",
		# v0.48.3 — the second half of an onboarding upload, and the route whose
		# absence sent the wizard's evidence to a Frappe path the funnel strips
		# the credential from.
		"/mobile/attach_onboarding_document",
		# v0.52.0 — models served from ERPNext, not Volume Vision directly.
		"/mobile/get_active_model",
		"/mobile/get_model_file_chunk",
		# v0.54.0 — the hiring wizard's Assignment and Housing steps. The four
		# dropdowns read off the site rather than compiled into the app, and the
		# read and write of one question: which cabin has a bed free, and put
		# this person in it.
		"/mobile/list_onboarding_reference_data",
		"/mobile/list_available_housing",
		"/mobile/assign_housing",
		# v0.55.0. Where the signature pad posts what a finger drew, for the
		# box a missing-signature alert found empty.
		"/mobile/collect_signature",
		# v0.57.0. The compliance calendar becomes a place work can be finished:
		# a row may be closed where the alert itself says it may be, and the pad
		# has the route and the argument spellings `API_CONTRACT.md` §14.2 posts.
		# `collect_signature` above is the same write under v0.55.0's names and
		# keeps its route, because `bind` reduces a body to the keys a signature
		# declares and a handset already in the field must not have to change to
		# get an answer.
		"/mobile/dismiss_compliance_alert",
		"/mobile/submit_form_signature",
		# v0.58.0. The break methods: log, end, and the policy the coach counts from.
		"/mobile/log_shift_break",
		"/mobile/end_shift_break",
		"/mobile/get_break_policy",
		# v0.59.0. The foreman's day: clock somebody out, production, shift detail.
		"/mobile/clock_out_worker",
		"/mobile/get_shift_production",
		"/mobile/get_shift",
		# v0.81.0. The register read, defaulted to the caller's own shifts — how a
		# handset that lost the docname finds the shift it left open.
		"/mobile/list_shifts",
		# The compliance timeline, the crew track and the per-worker envelope —
		# tools since v0.19.3, v0.32.0 and v0.64.0, reachable until now only from a
		# Desk, which is the one place a break that happened on the block cannot be
		# logged at the moment it happened.
		"/mobile/log_shift_event",
		"/mobile/log_shift_location",
		"/mobile/get_shift_track",
		"/mobile/get_shift_crew_timeline",
		# The QR valve workflow. One route: resolve the tag, record the scan, and
		# open or shut the gate in the same POST only when the body asks for it.
		# `toggle` defaults to false — a camera that acted on recognition would
		# water a block because somebody walked past with a phone.
		"/mobile/scan_valve",
		# v0.62.0. The seven the handset names and this table did not carry. The
		# first three reach the same functions as `list_onboarding_reference_data`,
		# `list_available_housing` and `assign_housing` above; they are separate
		# paths because `bind` reduces a body to the keys a signature declares, so
		# the argument spellings the app posts have to exist in a signature or
		# they are dropped on the floor. The last four had no method at all.
		"/mobile/list_org_reference_data",
		"/mobile/list_housing_units",
		"/mobile/create_housing_assignment",
		"/mobile/set_employee_org_fields",
		"/mobile/set_employee_contact_fields",
		"/mobile/list_attachments",
		"/mobile/get_attachment_content",
		# v0.63.0. The two ends of the signing flow. `get_document_preview` is the
		# route `API_CONTRACT.md` §17.5 asked for by name — the presentation step
		# had no way to render the page, because both renderers answer with a
		# private `file_url` and this door cannot follow one. `seal_signed_document`
		# is step 5 of the same chain, published for the forms the automatic seal
		# on `submit_form_signature` cannot reach.
		"/mobile/get_document_preview",
		"/mobile/seal_signed_document",
		# v0.65.0. The scanner screen's one call. Five routes above answer for
		# one register each and refuse everything else, so a phone pointed at an
		# unfamiliar sticker had to be told what it was looking at before it
		# could ask. This resolves the string first and answers second.
		"/mobile/universal_scan",
		# v0.67.0. Receipt capture, four routes: the branch, and the three
		# registers a phone may write into. `submit_scale_ticket`,
		# `create_settlement_statement` and `submit_settlement_statement` are
		# tools with NO route here on purpose — the first freezes a third party's
		# weight record and the other two are an office document, not a
		# photograph — and the assertion below in the other direction is what
		# keeps that a decision rather than an omission.
		"/mobile/classify_receipt",
		"/mobile/create_expense_receipt",
		"/mobile/create_scale_ticket",
		"/mobile/list_scale_tickets",
		"/mobile/list_cost_centers",
		"/mobile/list_suppliers",
		"/mobile/list_expense_receipts",
		"/mobile/update_expense_receipt",
		# Sprint 3 (v0.68.0). Compliance alert rectification — see api/rectify.py.
		# Five direct fixes, and the one route every task-shaped fix shares.
		"/mobile/renew_certification",
		"/mobile/record_training",
		"/mobile/sign_training_supervisor_review",
		"/mobile/update_regulatory_filing",
		"/mobile/advance_policy_review",
		"/mobile/rectify_alert",
		# Sprint 4 (v0.69.0). Document intelligence, two routes: the phone reads
		# a pesticide label at a chemical shed and this decides whether to
		# believe what it read, and one stored validation read back.
		#
		# BOTH PATHS DIFFER FROM THE SPRINT 4 CONTRACT'S SPELLING — it named
		# `/farmops/api/validate-document` and a GET at
		# `/farmops/api/document-validation/<name>`. This transport builds every
		# path from the method's own name, takes POST only, and matches whole
		# paths rather than patterns; a hyphen is not a Python identifier and a
		# path parameter has nowhere to land. The bodies and answers are the
		# contract's.
		#
		# `list_document_validations`, `list_revalidation_due` and
		# `revalidate_document` are tools with NO route here on purpose — two
		# registers an office reads and one status a supervisor re-decides — and
		# the assertion below in the other direction keeps that a decision
		# rather than an omission.
		"/mobile/validate_document",
		"/mobile/get_document_validation",
		# Sprint 7 (v0.72.0). The foreman's crew-task dashboard, five routes: the
		# board for the crew on this foreman's own open shift, the dispatch that
		# moves a job between people, the task raised on the spot, and the two
		# ends of the template register.
		#
		# THE FIRST FIVE PATHS HERE THAT A FIELD WORKER CANNOT CALL. Every route
		# above is a worker's own work; each of these calls
		# `guard.require_dispatch_role` in its own body, because the tools behind
		# them have no role check of their own — on the MCP transport the
		# operator's enablement switch is what stands in front of them, and a
		# phone does not go through it.
		#
		# `get_farm_task_template`, `create_farm_task_template` and
		# `update_farm_task_template` are tools with NO route here on purpose —
		# authoring the shape of a recurring job is a desk decision with the
		# regulation open — and the assertion below in the other direction is
		# what keeps that a decision rather than an omission.
		"/mobile/list_dispatched_tasks",
		"/mobile/assign_farm_task",
		"/mobile/create_farm_task",
		"/mobile/list_farm_task_templates",
		"/mobile/create_task_from_template",
		# Sprint 8 (v0.78.0). Field asset registration, three routes: register
		# the machine, get the printable tag back, file the photograph against
		# it. The first two are MCP tools from v0.25.0 that never had a route,
		# so the iOS flow stopped at step two with a 404.
		#
		# `attach_file_to_document` IS THE ONE THAT IS NARROWED HERE RATHER THAN
		# PUBLISHED WHOLE. The tool attaches to any doctype; the wrapper carries
		# an allowlist of the registers this surface already writes into and
		# refuses the rest by name, and it does not declare `allow_cancelled` at
		# all — so this table's own argument filter is what keeps a cancelled
		# parent unreachable from a phone whatever a body says.
		#
		# `bulk_create_assets`, `retire_asset` and `update_registered_asset` are
		# tools with NO route here on purpose. Retiring a machine and rewriting
		# a register are desk decisions, and a five-hundred-asset bulk load is a
		# rollout rather than anything anybody does at a tailgate. The assertion
		# below in the other direction is what keeps that a decision rather than
		# an omission.
		"/mobile/register_asset",
		"/mobile/generate_asset_qr",
		"/mobile/attach_file_to_document",
		# Sprint 9 (v0.79.0). Nineteen routes in four groups, gated differently
		# on purpose — see the block comment beside them in `routes.py`. The
		# pause pair is a worker's own work; linking is an observation and
		# merging is a decision; the five discipline routes carry an HR gate in
		# their own bodies; and the accident group SPLITS, because the person
		# who finds somebody on the ground is whoever finds them.
		#
		# `expire_incident_record` is a tool with NO route here on purpose:
		# ageing a step out of a chain is a policy decision made at a desk with
		# the handbook open. The assertion below in the other direction is what
		# keeps that a decision rather than an omission.
		"/mobile/pause_task_via_mobile",
		"/mobile/resume_task_via_mobile",
		"/mobile/link_tasks_via_mobile",
		"/mobile/merge_task_via_mobile",
		"/mobile/add_task_note_via_mobile",
		"/mobile/attach_audio_note",
		"/mobile/list_task_notes",
		"/mobile/create_discipline_record",
		"/mobile/acknowledge_discipline_record",
		"/mobile/get_discipline_record",
		"/mobile/list_discipline_history",
		"/mobile/get_discipline_report",
		"/mobile/create_accident_report",
		"/mobile/get_accident_report",
		"/mobile/update_accident_investigation",
		"/mobile/close_accident_investigation",
		"/mobile/list_accident_reports",
		"/mobile/get_wizard_definition",
		"/mobile/list_wizard_definitions",
		# v0.85.0. The one route on this table whose ANSWER depends on who is
		# asking rather than on what they asked for: the language comes from
		# `Employee.preferred_language`, with the request's own `Accept-Language`
		# as the fallback where that column is empty — never the other way round.
		"/mobile/get_translation_bundle",
		# v0.80.0. Four trade-documentation reads and one write. The write is
		# `/mobile/confirm_shipment_movement` and NOT `/mobile/update_shipment_status`,
		# which is the whole point: the tool behind it can release a shipment to
		# Ready to Ship — the module's one gate — can cancel one, and takes an
		# `override_reason` that walks past an incomplete document checklist. The
		# wrapper's signature carries none of the three, so `bind` cannot pass
		# them. A driver confirming a load left and arrived is a different act
		# from a desk asserting the paperwork is in order.
		"/mobile/list_shipments",
		"/mobile/get_shipment",
		"/mobile/get_shipment_readiness",
		"/mobile/list_trade_documents",
		"/mobile/confirm_shipment_movement",
		# v0.91.0. The RACI feed, reachable from a handset at last. All three are
		# ADDRESSED reads and writes rather than register ones: the tool's
		# `employee` argument names the recipient of a copy and none of the three
		# wrappers declares it, so `bind` cannot deliver a colleague's feed.
		"/mobile/list_shadow_log_entries",
		"/mobile/get_shadow_log_entry",
		"/mobile/acknowledge_shadow_log",
		# v0.91.0. The inventory tab's four reads and one draft-only write. The
		# app has been calling these at hyphenated top-level paths this table
		# cannot express — `Route` builds every path off the wrapper's own name —
		# so the client moves to the namespace rather than this file growing a
		# second path grammar.
		#
		# `submit_stock_entry` IS ABSENT AND THAT IS THE POINT OF THE PAIR: the
		# create writes a draft, the submit writes GL entries, and only the first
		# is reachable from a handset.
		"/mobile/get_stock_balance",
		"/mobile/get_warehouse_summary",
		"/mobile/get_stock_ledger",
		"/mobile/list_reorder_alerts",
		"/mobile/create_stock_entry",
		# v0.91.0. The fifth wizard submit target. The other four —
		# `create_employee`, `register_asset`, `create_accident_report`,
		# `create_discipline_record` — were already on this table; the
		# `inspection_session` wizard named one that existed nowhere, so the form
		# loaded and could not be filed. There is still no `submit_wizard`.
		"/mobile/start_inspection",
		# v0.91.0. The envelope every wizard posts, and NOT the dispatcher this
		# table refuses: it takes no method name from a caller, resolves its
		# target against this very table, and calls it through the target's own
		# guard and argument filter. `submit_wizard` is still absent.
		"/mobile/submit_wizard_via_mobile",
		# v0.91.0. The two payroll outputs, and the only routes on this table
		# that reach wages. Both wrappers gate on `HR_ROLES` in their own bodies
		# rather than on the field roles this surface is built for — a register
		# is what everybody was paid and a stub is what one person was paid, and
		# `DISPATCH_ROLES` would have put a crew's wages in front of every
		# foreman on the site.
		#
		# The register is company-scoped through `guard.require_company`; the
		# stub is ALSO employee-scoped through `_employee_argument`, without
		# which an HR account could have walked another entity's payroll one
		# stub at a time. `show_employer_contributions` is deliberately absent
		# from the stub wrapper's signature, so `bind` drops it: whether a farm
		# shows its own FICA on a worker's statement is one operator policy for
		# the whole operation, not a checkbox on the handset of whoever printed
		# it.
		"/mobile/get_payroll_register",
		"/mobile/render_pay_stub",
		"/mobile/get_tax_remittance_summary",
		"/mobile/get_941_prefill",
		"/mobile/get_state_tax_remittance",
		"/mobile/get_tax_deposit_schedule",
		"/mobile/get_futa_summary",
		# The three compliance reports — the only aggregate reads on the table.
		# `get_osha_300a_summary` is the one whose ABSENT arguments matter:
		# `total_hours_worked` and `average_employees` are on the tool and not on
		# the wrapper, so `bind` drops them and a handset cannot choose the
		# denominator of a rate a regulator reads.
		"/mobile/get_training_compliance_report",
		"/mobile/get_osha_300_log",
		"/mobile/get_osha_300a_summary",
		"/mobile/get_spray_application_report",
		# The curriculum and the group training session. `update_training_type`
		# is the one whose ABSENT arguments matter: `regimes` and
		# `retention_years` are on the tool and not on the wrapper, so `bind`
		# drops them and a handset cannot repoint which audits a course answers.
		"/mobile/get_training_curriculum",
		"/mobile/update_training_type",
		"/mobile/create_training_session",
		"/mobile/add_session_attendee",
		"/mobile/sign_session_attendance",
		"/mobile/complete_training_session",
		"/mobile/get_training_session",
		"/mobile/list_training_sessions",
		"/mobile/render_training_sign_in_sheet",
		"/files/stage_file_chunk",
		"/files/finalize_staged_file",
		# v0.91.0. Direct deposit. The read is the caller's own accounts and the
		# two writes are their own account — no `employee` argument on any of
		# the three, which is what keeps a shared company scope from becoming a
		# way to redirect a colleague's wages.
		"/mobile/list_my_bank_accounts",
		"/mobile/add_my_bank_account",
		"/mobile/update_my_bank_account",
		# Employee self-service: the W-4, the pay stubs and the one that draws
		# them, the training card list and the I-9. NONE OF THE FIVE DECLARES AN
		# `employee` ARGUMENT, so `bind` has nothing to drop and no body can
		# repoint one at a colleague — the same property the three above have and
		# for the same reason.
		#
		# `get_my_pay_stub_pdf` is the one whose ABSENT argument matters:
		# `overwrite` is on the tool and not on the wrapper, so `bind` drops it
		# and a handset cannot replace a statement somebody was already handed.
		"/mobile/get_my_w4",
		"/mobile/list_my_pay_stubs",
		"/mobile/get_my_pay_stub_pdf",
		"/mobile/list_my_trainings",
		"/mobile/get_my_i9",
		# The payroll deduction register: three reads and two writes, all
		# gated on HR_ROLES in their own bodies. `update_payroll_deduction`
		# is the one whose ABSENT arguments matter — `employee` and `company`
		# are on the tool and not on the wrapper, so `bind` drops them and a
		# handset cannot move an order made against one person to another.
		# v0.98.0 — Wave 2 of `fafo_ios/SERVER_CHANGES.md`, nine routes.
		#
		# `add_task_note` (item 12) is the same write as
		# `add_task_note_via_mobile` above under the name the app asks for; the
		# older spelling keeps its route, as `collect_signature` did when
		# `submit_form_signature` arrived. `create_dispute` is item 12's other
		# half — a worker's grievance filed as a Worker Report rather than as a
		# step on their own discipline chain — and `discipline_type` is
		# deliberately absent from its signature, so `bind` drops it and a
		# complaint cannot be given a warning level by the body that raises it.
		#
		# `get_break_schedule` (item 14) is the schedule and not the policy: the
		# instants this shift's breaks fall due, computed once so a crew's phones
		# count down together. It declares `farm_shift` as well as `shift`.
		#
		# The six location routes are item 11, the largest gap on the document.
		# The read is open on enrolment — `report_field_task` is open to every
		# worker and takes a location — and all five writes carry
		# `guard.require_location_role` (Farm Manager, a strict subset of
		# `DISPATCH_ROLES`) inside one shared implementation. `create_parcel`'s
		# ABSENT arguments are the ones that matter: `title_holder`,
		# `appraised_value`, `appraiser` and `appraisal_document` are on the tool
		# and not on the wrapper, so `bind` drops them and a handset cannot put a
		# number on a piece of ground that reaches a financial statement.
		"/mobile/add_task_note",
		"/mobile/create_dispute",
		"/mobile/get_break_schedule",
		"/mobile/list_farm_locations",
		"/mobile/create_farm_location",
		"/mobile/create_field",
		"/mobile/create_irrigation_zone",
		"/mobile/create_parcel",
		"/mobile/create_housing_unit",
		# v0.110.0. The three boundary writes, which the location block above
		# listed as deliberately absent until this release — see `routes.py` for
		# why walking a boundary is not the desk act that drawing one is. All
		# three carry `guard.require_location_role`, and `owning_entity` and
		# `company` are ABSENT from every one of the three signatures, so `bind`
		# drops them and no body can file a polygon against another entity.
		"/mobile/set_field_boundary",
		"/mobile/set_zone_boundary",
		"/mobile/set_parcel_boundary",
		"/mobile/list_payroll_deductions",
		"/mobile/get_payroll_deduction",
		"/mobile/list_employee_deductions",
		"/mobile/create_payroll_deduction",
		"/mobile/update_payroll_deduction",
		# v0.98.0. Bin sealing — `PieceTallyViewModel.sealBin(tag:)` and the
		# kind-4004 event beside it. THE LAST MOMENT ANYBODY KNOWS THE ANSWER: a
		# bin leaves the orchard carrying a tag and nothing else, the buckets are
		# tipped and mixed, and the badge scans live on the handset. Everything
		# the packing house asks afterwards is a join from that tag back to an
		# hour that was never written down.
		#
		# `company` AND `source` ARE ABSENT FROM ITS SIGNATURE, so this surface's
		# argument filter makes them unreachable rather than merely refused. The
		# first would let a phone file another farm's harvest against this one's
		# crew; the second would let a handset pass a typed record off as a
		# scanned one, and the register has to tell those apart.
		"/mobile/seal_bin",
		# v0.99.0. The push register: the handset enrols its APNs device token
		# on login and retires it on logout. `unregister_push_token` is the one
		# whose ABSENT argument matters — `token` is on the tool and not on the
		# wrapper, so `bind` drops it and a logout cannot be made to fail because
		# Apple rotated the token since the phone signed in. Neither declares
		# `user` or `employee`, so no body can enrol a device against another
		# worker's name.
		"/mobile/register_push_token",
		"/mobile/unregister_push_token",
		# v0.105.0. SERVER_CHANGES #24 — the in-app feedback bubble. The route
		# whose absence parked every note the farm wrote, since a 404 on this
		# client is a park rather than a failure. `user` is absent from its
		# signature, so no body can file a note under a colleague's login.
		"/mobile/submit_app_feedback",
		# v0.106.0. The compliance-alert-to-task feature's three missing paths.
		#
		# `materialize_task_for_alert` is the one the app has been CALLING and
		# getting a 404 from since the feature shipped, so it falls back to
		# composing a task out of the alert's prose with `create_farm_task` —
		# whose signature does not declare `source_alert`, so the task and the
		# alert it answers are two records with no edge between them. Nothing
		# closes the alert when the work is done and the sweep raises it again
		# the same night. It declares `urgency` and `assigned_to` because those
		# are the two decisions a foreman standing in an orchard makes; it does
		# NOT declare the task type, the evidence contract or `source_alert`,
		# which stay the compliance rule's.
		#
		# The two certificate reads carry `require_dispatch_role` in their own
		# bodies. A register that names everybody whose licence has lapsed is a
		# personnel document, not a field read — the same argument the training
		# matrix's HR gate makes.
		"/mobile/materialize_task_for_alert",
		"/mobile/list_certifications",
		"/mobile/get_certification",
		# v0.113.0. The location pair and the five org masters. The location block
		# above listed the `update_*` tools as deliberately absent; that sentence
		# was about `convey_parcel` and was never true of the acreage, and the
		# delete had no door anywhere in this app on any transport. Both carry
		# `guard.require_location_role` and both prove the record's entity with
		# `_scoped_location`. The four `force_check_…` flags are ABSENT from
		# `delete_farm_location`'s signature, so `bind` drops them and no body can
		# turn off the one check Frappe's own link integrity does not make.
		#
		# The fifteen org paths are `tools/org.py`'s whole surface, which had no
		# route at all — so the hiring wizard could OFFER the site's designations
		# and never add one. Reads open on enrolment, writes HR, and the Employee
		# Grade pay columns absent from every signature.
		"/mobile/update_farm_location",
		"/mobile/delete_farm_location",
		"/mobile/list_designations",
		"/mobile/create_designation",
		"/mobile/update_designation",
		"/mobile/list_departments",
		"/mobile/create_department",
		"/mobile/update_department",
		"/mobile/list_branches",
		"/mobile/create_branch",
		"/mobile/update_branch",
		"/mobile/list_employment_types",
		"/mobile/create_employment_type",
		"/mobile/update_employment_type",
		"/mobile/list_employee_grades",
		"/mobile/create_employee_grade",
		"/mobile/update_employee_grade",
	}

	def test_the_route_table_is_exactly_the_twelve_the_app_calls(self):
		self.assertEqual({route.path for route in ROUTES}, self.EXPECTED)

	def test_every_guarded_method_has_a_route_so_none_is_stranded(self):
		"""The other direction. A method the app calls with no route is a 404 in
		a field, which is the failure v0.17.1 shipped and v0.17.2 could not fix."""
		guarded = set()
		for module in (mobile_api, files_api):
			for name in dir(module):
				attribute = getattr(module, name)
				if getattr(attribute, "farm_ops_method", None):
					guarded.add(attribute.farm_ops_method)
		self.assertEqual({route.handler.farm_ops_method for route in ROUTES}, guarded)

	def test_every_route_reaches_something_that_wears_the_guard(self):
		"""A route pointing at an unguarded function would publish it ungated."""
		for route in ROUTES:
			with self.subTest(path=route.path):
				self.assertTrue(getattr(route.handler, "farm_ops_method", None))

	#: `--set-path=<path> <target>` off one line of the mount script's --dry-run.
	MOUNT_LINE = re.compile(r"--set-path=(\S+)\s+(\S+)\s*$")

	def mounts(self):
		"""Every (path, target) `scripts/mount_farmops_funnel.sh` would publish.

		Run rather than read. The script builds each target from its mount path
		so the two cannot drift, and a test that re-parsed the source would be
		asserting against the lists rather than against what the operator's shell
		is actually handed. `--dry-run` prints the commands and executes nothing,
		so this touches no docker and no tailnet.
		"""
		script = Path(__file__).resolve().parent.parent / "scripts" / "mount_farmops_funnel.sh"
		completed = subprocess.run(
			["sh", str(script), "--dry-run"],
			capture_output=True,
			text=True,
			timeout=30,
			check=False,
		)
		self.assertEqual(completed.returncode, 0, completed.stderr)
		found = {}
		for line in completed.stdout.splitlines():
			if " funnel " not in line or "--set-path" not in line or line.rstrip().endswith(" off"):
				continue
			match = self.MOUNT_LINE.search(line)
			self.assertIsNotNone(match, f"unparsed mount line: {line!r}")
			found[match.group(1)] = match.group(2)
		self.assertTrue(found, "the mount script emitted no mounts at all")
		return found

	def test_no_mount_forwards_to_a_bare_origin(self):
		"""THE ASSERTION THAT WOULD HAVE CAUGHT v0.58.0's SILENT 404 ON EVERY ROUTE.

		`--set-path` STRIPS THE PATH IT MATCHED before it forwards. The obvious
		mount — `--set-path=/farmops/api/health http://127.0.0.1:5250` — reads as
		"publish that path" and does not do that: what reached gunicorn was `/`,
		for all fifty-six of them, and what came back was `farmops_api`'s own
		refusal for a path outside its prefix:

		    {"error": "/ is not a Farm Ops API path."}

		The phone showed that as its generic miss, exactly as it had shown
		v0.57.1's proxy 404, and `tailscale serve status` listed fifty-six mounts
		that all looked right. THE TARGET IS WHERE THE BUG LIVES, so the target
		is what this reads: every mount must forward to its own path, so that the
		strip and the target's path cancel out.
		"""
		for path, target in sorted(self.mounts().items()):
			with self.subTest(path=path):
				self.assertTrue(
					target.endswith(path),
					f"--set-path={path} forwards to {target}, which does not carry {path}. "
					f"Tailscale strips the matched path, so this mount delivers the request "
					f"to the origin's root and the service answers 404 to everything.",
				)
				# And the path exactly once: an origin that already carried it
				# would double it into `/farmops/farmops/api/...`.
				self.assertEqual(target.count(path), 1, target)

	def test_every_route_is_covered_by_a_mount(self):
		"""THE ASSERTION THAT WOULD HAVE CAUGHT THREE RELEASES OF SILENT 404s.

		v0.54.0, v0.55.0 and v0.57.0 each added routes to `routes.py` and none of
		them was mounted, because the funnel was fifty-six exact mount points and
		a route is not published by having been added. Six methods — the housing
		pair, the onboarding dropdowns, both signature routes and the alert
		dismissal — answered Tailscale's own plain-text 404 to every phone on the
		farm while every test in this file passed, because these tests call the
		service and a handset calls the funnel. Nothing about it was visible from
		the server: the request never arrived, so there was no log line, no audit
		row and no traceback.

		v0.58.1 mounts `/farmops` as ONE PREFIX, which is what makes a route
		published by having been added. This asserts the property that buys —
		every route, and the health path, under a mount — rather than the list
		that used to stand in for it. It still fails loudly if somebody narrows
		the prefix back to something that leaves a route uncovered.
		"""
		mounts = self.mounts()
		covered = {path for path in mounts if path == PREFIX or PREFIX.startswith(f"{path}/")}
		self.assertTrue(
			covered,
			f"nothing in the mount script covers {PREFIX} — every route a phone calls is a "
			f"404 in somebody's hand. Mounts: {sorted(mounts)}",
		)
		for route in ROUTES:
			with self.subTest(path=route.path):
				full = f"{PREFIX}{route.path}"
				self.assertTrue(
					any(full == mount or full.startswith(f"{mount}/") for mount in covered),
					f"{full} is not under any mount in scripts/mount_farmops_funnel.sh",
				)
		self.assertTrue(
			any(farmops_app.HEALTH_PATH.startswith(f"{mount}/") for mount in covered),
			f"{farmops_app.HEALTH_PATH} is not mounted, and it is what makes a failed check "
			f"diagnostic rather than binary",
		)

	def test_the_other_two_ports_are_mounted_one_exact_path_at_a_time(self):
		"""A PREFIX MOUNT ON EITHER OF THEM PUBLISHES SOMETHING NOBODY MEANT TO.

		The `/farmops` prefix is safe because the service behind it 404s anything
		that is not in `ROUTES` and every route in `ROUTES` runs `guard.endpoint`
		— the mount is not the boundary, the route table is. Neither other port
		has that property:

		  * `/api/method` on 5300 is Frappe's whitelisted-method router. A prefix
		    there publishes every `@frappe.whitelist()` on the site, in every
		    installed app, to the public internet.
		  * `/bankbridge` on 5202 fronts an admin UI that is unauthenticated by
		    design, plus four unauthenticated Plaid write endpoints. Bank Bridge's
		    SECURITY.md says publish the OAuth callback and nothing else.

		So this asserts the shape rather than the contents: anything mounted off
		those two ports is a leaf path, never a prefix somebody could grow into.
		"""
		for path in self.mounts():
			if path == PREFIX or PREFIX.startswith(f"{path}/"):
				continue
			with self.subTest(path=path):
				self.assertNotIn(
					path,
					("/", "/api", "/api/method", "/bankbridge", "/bankbridge/plaid"),
					f"{path} is a prefix mount over a surface whose boundary is not a route "
					f"table. See this test's docstring for what it publishes.",
				)
				self.assertTrue(
					path.startswith("/api/method/erpnext_mcp.") or path.startswith("/bankbridge/"),
					f"{path} is neither a farmops route nor one of the two exact paths this "
					f"script is allowed to publish",
				)

	def test_no_admin_tool_is_reachable_at_any_path(self):
		"""The one that matters. Two hundred MCP tools; none of them is here."""
		for dangerous in (
			"create_journal_entry",
			"submit_journal_entry",
			"convey_parcel",
			"import_chart_of_accounts",
			"create_mobile_user",
			"generate_mobile_login_qr",
			"revoke_api_token",
			"run_report",
		):
			for prefix in ("/mobile", "/files"):
				response = self.post(f"{PREFIX}{prefix}/{dangerous}")
				self.assertEqual(response.status_code, 404, dangerous)

	def test_there_is_no_dispatcher_taking_a_method_name(self):
		"""A `call(tool_name, args)` would have published everything at one path."""
		for suspect in ("call", "invoke", "handle", "run", "dispatch_tool"):
			for prefix in ("/mobile", "/files"):
				self.assertEqual(
					self.post(f"{PREFIX}{prefix}/{suspect}", {"name": "create_journal_entry"}).status_code,
					404,
				)

	def test_a_path_outside_the_prefix_is_refused_without_touching_frappe(self):
		for path in ("/", "/app", "/api/method/erpnext_mcp.mcp.handle", "/farmops", "/farmopsx/api"):
			with self.subTest(path=path):
				self.assertEqual(self.post(path).status_code, 404)

	def test_none_of_the_twelve_takes_kwargs(self):
		"""`**kwargs` on a wrapper would forward the phone's whole body into a
		tool — which is exactly how `record_data` and `worker_id` would become
		reachable. `api/mobile.py` names every accepted argument for that reason;
		this is the check that it still does.

		Asserted against the signature directly rather than through
		`accepted_arguments`, because that function answers the empty set for BOTH
		"takes `**kwargs`" and "takes nothing at all" — and
		`get_current_user_context` legitimately takes nothing.
		"""
		import inspect

		for route in ROUTES:
			with self.subTest(path=route.path):
				kinds = [p.kind for p in inspect.signature(route.handler).parameters.values()]
				self.assertNotIn(inspect.Parameter.VAR_KEYWORD, kinds)
				self.assertNotIn(inspect.Parameter.VAR_POSITIONAL, kinds)

	def test_a_kwargs_signature_would_take_every_argument_away(self):
		"""The safe direction to fail. If one of the eleven ever grew a
		`**kwargs`, this transport stops forwarding anything rather than
		forwarding everything."""

		def greedy(user, **kwargs):
			pass

		self.assertEqual(farmops_routes.accepted_arguments(greedy), set())


# ── 2. the door ─────────────────────────────────────────────────────────────
class TheDoorOpens(FarmOpsAPITestCase):
	def test_a_valid_token_gets_json_and_the_right_user(self):
		"""THE WHOLE POINT OF THE RELEASE, in one assertion."""
		self.assertEqual(self.message(CONTEXT)["user"], WORKER)

	def test_the_authorization_header_works_too_for_the_lan_and_for_curl(self):
		pair = f"token {self.credential['api_key']}:{self.credential['api_secret']}"
		body = self.message(CONTEXT, credential=False, headers={"Authorization": pair})
		self.assertEqual(body["user"], WORKER)

	def test_the_auth_body_works_when_no_header_survives(self):
		body = self.message(CONTEXT, {"_auth": dict(self.credential)}, credential=False)
		self.assertEqual(body["user"], WORKER)

	def test_a_leading_token_word_in_the_farmops_header_is_tolerated(self):
		"""The likeliest mistake in a two-line client change is copying the word
		as well as the value. Accepting it costs one comparison."""
		pair = f"token {self.credential['api_key']}:{self.credential['api_secret']}"
		self.assertEqual(self.message(CONTEXT, token=pair)["user"], WORKER)

	def test_health_answers_without_a_credential_and_says_nothing_about_the_site(self):
		"""So that 'the funnel path is wrong' and 'the credential is wrong' are
		two different answers to somebody standing in a field."""
		response = self.post(f"{PREFIX}/health", credential=False, method="GET")
		body = self.payload(response)
		self.assertEqual(response.status_code, 200)
		self.assertTrue(body["ok"])
		self.assertEqual(body["service"], "farmops-api")
		self.assertNotIn("site", json.dumps(body).lower())


class TheDoorStaysShut(FarmOpsAPITestCase):
	"""Five ways to fail, one answer. A caller learns whether it is in, and
	nothing else — telling it which fact was wrong hands it a free oracle."""

	def _refused(self, **kwargs):
		status, body = self.refusal(CONTEXT, **kwargs)
		self.assertEqual(status, 401)
		return body["error"]

	def test_no_header_at_all(self):
		self.assertIn("no usable Farm Ops credential", self._refused(credential=False))

	def test_a_malformed_header_with_no_separator(self):
		self._refused(token="not-a-pair")

	def test_an_unknown_api_key(self):
		self._refused(credential={"api_key": "nope", "api_secret": "x" * 56})

	def test_the_right_key_with_the_wrong_secret(self):
		self._refused(credential={"api_key": self.credential["api_key"], "api_secret": "w" * 56})

	def test_a_disabled_account_is_refused_even_with_a_perfect_credential(self):
		"""The fastest way to stop one person without touching anybody else."""
		frappe.db.set_value("User", WORKER, "enabled", 0)
		self._refused()

	def test_all_five_read_identically(self):
		frappe.db.set_value("User", "nobody@example.test", "enabled", 0)
		messages = {
			self._refused(credential=False),
			self._refused(token="not-a-pair"),
			self._refused(credential={"api_key": "nope", "api_secret": "x" * 56}),
			self._refused(credential={"api_key": self.credential["api_key"], "api_secret": "w" * 56}),
		}
		self.assertEqual(len(messages), 1, messages)

	def test_a_wrong_secret_is_metered_on_the_counter_the_other_door_uses(self):
		"""One credential, one failure budget, however many transports. A guesser
		must not get ten fresh attempts a minute per door."""
		wrong = {"api_key": self.credential["api_key"], "api_secret": "w" * 56}
		for _ in range(fallback_auth.FAILURE_LIMIT):
			self._refused(credential=wrong)
		# The counter is keyed on the key, so the RIGHT secret is now refused too
		# until the window rolls — that is the point of metering the key rather
		# than the address, and it is bounded to a minute.
		self._refused()

	def test_a_working_phone_is_never_metered_by_somebody_elses_wrong_answers(self):
		"""Forty phones arrive from one funnel address. An address-keyed limit
		would let one of them lock the other thirty-nine out."""
		other = self.enrol(email=OUTSIDER, name="Ben Ortiz", role="Foreman", entities=[OTHER])
		for _ in range(fallback_auth.FAILURE_LIMIT + 5):
			self._refused(credential={"api_key": other["api_key"], "api_secret": "w" * 56})
		self.assertEqual(self.message(CONTEXT)["user"], WORKER)


# ── 3. the seven gates still run ────────────────────────────────────────────
class TheSevenGatesStillRun(FarmOpsAPITestCase):
	"""Every one of them, against a credential that is otherwise perfectly valid.

	"It delegates to the guarded functions" is a claim about wiring. These are
	the checks. A gate that stopped applying on this transport would be a field
	worker reading another entity's board through a URL nobody audited.
	"""

	def test_the_role_gate_refuses_a_real_login_with_no_field_role(self):
		"""A Family Member and an Advisor are real accounts on this site."""
		aunt = self.enrol(email="aunt@example.test", name="Aunt", role="Field Worker")
		set_roles("aunt@example.test", ["Family Member", "Advisor"])
		status, body = self.refusal(MY_TASKS, credential=aunt)
		self.assertEqual(status, 403)
		self.assertIn("enrolled Farm Ops credential", body["error"])

	def test_the_grant_gate_refuses_a_field_role_that_was_never_enrolled(self):
		"""Holding the role is not being enrolled. The grant IS the enrolment."""
		STORE.seed("User", [{"name": "casual@example.test", "enabled": 1, "full_name": "Casual"}])
		set_roles("casual@example.test", ["Field Worker"])
		frappe.db.set_value("User", "casual@example.test", "api_key", "casualkey")
		STORE.passwords[("User", "casual@example.test", "api_secret")] = "c" * 56
		status, _ = self.refusal(MY_TASKS, credential={"api_key": "casualkey", "api_secret": "c" * 56})
		self.assertEqual(status, 403)

	def test_a_grant_that_is_no_longer_active_closes_this_door_on_the_next_call(self):
		"""THE GRANT GATE ON ITS OWN, with the credential left live on purpose.

		`revoke_mobile_user` clears the api_key as well as ending the grant, so
		revoking through the tool would refuse at the door and never reach this
		gate — see the test below, which is the one that checks the tool. Setting
		the state directly is what isolates the check that matters here: an
		account holding a PERFECTLY VALID credential is still refused the moment
		its enrolment stops being Active.
		"""
		self.assertEqual(self.message(CONTEXT)["user"], WORKER)
		frappe.db.set_value("Mobile Access Grant", WORKER, "state", "Revoked")
		status, body = self.refusal(CONTEXT)
		self.assertEqual(status, 403)
		self.assertIn("enrolled Farm Ops credential", body["error"])

	def test_revoking_a_user_also_kills_the_credential_outright(self):
		"""Belt to the gate's braces: the tool takes the token away too, so a
		revoked phone is refused at the door rather than one gate further in."""
		self.assertEqual(self.message(CONTEXT)["user"], WORKER)
		frappe.local.session.user = "Administrator"
		self.tool_data("revoke_mobile_user", {"email": WORKER, "reason": "left after harvest"})
		self.assertEqual(frappe.db.get_value("Mobile Access Grant", WORKER, "state"), "Revoked")
		self.assertEqual(self.refusal(CONTEXT)[0], 401)

	def test_administrator_holds_every_role_and_still_cannot_call_this(self):
		"""The reason the grant gate exists, re-run against the new entry point."""
		frappe.db.set_value("User", "Administrator", "api_key", "adminkey")
		STORE.passwords[("User", "Administrator", "api_secret")] = "a" * 56
		status, _ = self.refusal(MY_TASKS, credential={"api_key": "adminkey", "api_secret": "a" * 56})
		self.assertEqual(status, 403)

	def test_a_company_the_caller_cannot_reach_is_refused_not_quietly_emptied(self):
		"""An empty list reads on a phone as a quiet day. 'Not yours' is a
		different fact and somebody can act on it."""
		status, body = self.refusal(MY_TASKS, {"company": OTHER})
		self.assertEqual(status, 403)
		self.assertIn(OTHER, body["error"])

	def test_the_kill_switch_stops_everything_and_does_not_sign_the_phone_out(self):
		self.configure(enabled=1, farm_ops_mobile_enabled=0)
		status, body = self.refusal(CONTEXT)
		self.assertEqual(status, 503)
		self.assertIn("switched off", body["error"])

	def test_the_rate_limit_trips_and_answers_429(self):
		guard._BUCKETS.clear()
		for _ in range(guard.READ_LIMIT):
			self.assertEqual(self.post(CONTEXT).status_code, 200)
		status, body = self.refusal(CONTEXT)
		self.assertEqual(status, 429)
		self.assertIn("limit is", body["error"])

	def test_a_task_belonging_to_another_entity_reads_as_not_found(self):
		"""Split from 'does not exist' deliberately: the two refusals mean
		different things and are worded the same, so a caller cannot map the
		site's docnames by watching which error comes back."""
		self.a_camp()
		mine = self.a_task()
		theirs = self.a_task(company=OTHER, task_name="Somebody else's walk")
		self.assertEqual(self.refusal(f"{PREFIX}/mobile/get_task", {"task": theirs})[0], 404)
		self.assertEqual(self.refusal(f"{PREFIX}/mobile/get_task", {"task": "FT-NOPE"})[0], 404)
		self.assertTrue(self.message(f"{PREFIX}/mobile/get_task", {"task": mine}))


# ── 4. it is always JSON ────────────────────────────────────────────────────
class ItIsAlwaysJson(FarmOpsAPITestCase):
	"""v0.17.x's failure was HTTP 200 carrying the Desk's HTML login page, and
	the app reporting it as a decoding error. Every exit from this service is
	asserted to be JSON, INCLUDING the ones nobody planned for."""

	def _assert_json(self, response):
		self.assertEqual(response.headers["Content-Type"], "application/json")
		body = response.get_data(as_text=True)
		self.assertNotIn("<html", body.lower())
		return json.loads(body)

	def test_a_success_is_json(self):
		self._assert_json(self.post(CONTEXT))

	def test_a_401_is_json(self):
		self._assert_json(self.post(CONTEXT, credential=False))

	def test_a_404_is_json(self):
		self._assert_json(self.post(f"{PREFIX}/mobile/no_such_method"))

	def test_a_405_is_json_and_says_post(self):
		body = self._assert_json(self.post(CONTEXT, method="GET"))
		self.assertIn("POST only", body["error"])

	def test_a_body_that_is_not_json_does_not_produce_a_parser_page(self):
		response = self.client.open(
			CONTEXT,
			method="POST",
			data="this is not json at all",
			headers={fallback_auth.HEADER: "nope:nope"},
		)
		self._assert_json(response)

	def test_an_unexpected_exception_in_a_tool_is_json_and_not_a_traceback_page(self):
		"""Werkzeug's own 500 page is HTML. Reintroducing it here would rebuild
		the exact failure this release exists to end."""
		original = mobile_api.mobile_tools.get_current_user_context

		def boom(args):
			raise RuntimeError("the database fell over")

		mobile_api.mobile_tools.get_current_user_context = boom
		self.addCleanup(setattr, mobile_api.mobile_tools, "get_current_user_context", original)

		response = self.post(CONTEXT)
		body = self._assert_json(response)
		self.assertEqual(response.status_code, 500)
		self.assertIn("has been logged", body["error"])

	def test_an_internal_failure_does_not_put_its_own_words_on_the_phone(self):
		"""A 500's message is where table names and query fragments leak out."""
		original = mobile_api.mobile_tools.get_current_user_context

		def boom(args):
			raise RuntimeError("Table 'frontend.tabSecretLedger' doesn't exist")

		mobile_api.mobile_tools.get_current_user_context = boom
		self.addCleanup(setattr, mobile_api.mobile_tools, "get_current_user_context", original)

		body = json.loads(self.post(CONTEXT).get_data(as_text=True))
		self.assertNotIn("tabSecretLedger", json.dumps(body))

	def test_nothing_this_service_answers_is_a_redirect(self):
		"""A 302 to a login page is how the old path failed. This one has no
		login page to redirect to, and must never grow one."""
		for path, kwargs in (
			(CONTEXT, {}),
			(CONTEXT, {"credential": False}),
			(f"{PREFIX}/mobile/nope", {}),
			("/app", {}),
			("/", {}),
		):
			with self.subTest(path=path):
				response = self.post(path, **kwargs)
				self.assertFalse(300 <= response.status_code < 400, response.status_code)
				self.assertNotIn("Location", response.headers)


class TheRefusalReachesThePhone(FarmOpsAPITestCase):
	"""`FrappeClient.serverMessage` reads `_server_messages` first — a JSON
	string holding an array of JSON strings, each an object with a `message`.
	It is nested twice, which looks like a mistake and is not. A flat array
	produces a phone that shows "Something went wrong" instead of the sentence
	somebody wrote to be read in a field."""

	def _server_message(self, body):
		outer = json.loads(body["_server_messages"])
		self.assertIsInstance(outer, list)
		return [json.loads(entry)["message"] for entry in outer]

	def test_the_sentence_survives_the_envelope_the_shipped_app_decodes(self):
		self.a_camp()
		task = self.a_task()
		_, body = self.refusal(f"{PREFIX}/mobile/reject_task", {"task": task, "reason": "   "})
		self.assertIn("A reason is required", self._server_message(body)[0])

	def test_the_same_sentence_is_in_all_three_places_the_app_looks(self):
		_, body = self.refusal(MY_TASKS, {"company": OTHER})
		self.assertIn(OTHER, body["error"])
		self.assertIn(OTHER, self._server_message(body)[0])
		self.assertIn("PermissionError", body["exception"])


# ── 5. the status codes are the app's contract ──────────────────────────────
class TheStatusCodesMatterToThePhone(FarmOpsAPITestCase):
	"""`FarmOpsKit` treats 401 as "this credential is dead — sign out", which
	DISCARDS THE OFFLINE QUEUE. So 401 is reserved for exactly one thing, and
	everything else that refuses has to be something else."""

	def test_only_a_bad_credential_is_ever_401(self):
		self.configure(enabled=1, farm_ops_mobile_enabled=0)
		self.assertEqual(self.refusal(CONTEXT)[0], 503)

		self.configure(enabled=1, farm_ops_mobile_enabled=1)
		guard._BUCKETS.clear()
		for _ in range(guard.WRITE_LIMIT + 1):
			last = self.post(f"{PREFIX}/mobile/claim_task", {"task": "FT-NOPE"})
		self.assertEqual(last.status_code, 429)

		self.assertEqual(self.refusal(MY_TASKS, {"company": OTHER})[0], 403)
		self.assertEqual(self.refusal(CONTEXT, credential=False)[0], 401)

	def test_the_kill_switch_status_comes_through_even_as_the_switch_set_it(self):
		"""`guard._set_status` writes it on `frappe.local.response` because on the
		whitelisted path that dict IS the response. Reading it back means the two
		statuses the app's behaviour turns on cannot drift."""
		self.configure(enabled=1, farm_ops_mobile_enabled=0)
		self.assertEqual(self.post(CONTEXT).status_code, 503)


# ── 6. byte-identical to the old path ───────────────────────────────────────
class ByteIdentical(FarmOpsAPITestCase):
	"""THE CENTRAL CLAIM OF v0.18.0, CHECKED RATHER THAN ASSERTED.

	The whole security argument for a new transport to a compliance system is
	"the gates did not move, because it is the same code". That is only worth
	anything if somebody checks — the obvious implementation copies the gates
	into the new service, the copies drift, and the drift is invisible until an
	auditor finds a worker reading another entity's board.

	So every read is run BOTH ways against the same fixture and the two answers
	are compared as serialised JSON. A difference of any kind — a missing key, a
	reordered list, a stringified number — fails here.
	"""

	def both_ways(self, path, method, body=None):
		body = body or {}
		over_http = self.message(f"{PREFIX}{path}", dict(body))
		frappe.local.session = frappe._dict(user=WORKER, data=frappe._dict())
		self.request({}, headers={}, remote_addr="100.64.0.7")
		frappe.local.session.user = WORKER
		in_process = method(**body)
		self.assertEqual(
			json.dumps(over_http, sort_keys=True, default=str),
			json.dumps(in_process, sort_keys=True, default=str),
			f"{path} differs between the two transports",
		)
		return over_http

	def test_get_current_user_context(self):
		self.both_ways("/mobile/get_current_user_context", mobile_api.get_current_user_context)

	def test_list_my_tasks(self):
		self.a_camp()
		self.a_task()
		self.both_ways("/mobile/list_my_tasks", mobile_api.list_my_tasks)

	def test_list_my_tasks_scoped_to_one_company(self):
		self.a_camp()
		self.a_task()
		self.both_ways("/mobile/list_my_tasks", mobile_api.list_my_tasks, {"company": MAIN})

	def test_list_available_tasks(self):
		self.a_camp()
		self.a_task()
		self.both_ways("/mobile/list_available_tasks", mobile_api.list_available_tasks)

	def test_get_task(self):
		self.a_camp()
		task = self.a_task()
		self.both_ways("/mobile/get_task", mobile_api.get_task, {"task": task})

	def test_list_compliance_alerts(self):
		self.both_ways("/mobile/list_compliance_alerts", mobile_api.list_compliance_alerts)

	def test_a_write_produces_the_same_shape_over_either_transport(self):
		"""Claims cannot be run twice against one task, so the two runs get one
		task each and the SHAPES are compared rather than the docnames."""
		self.a_camp()
		first, second = self.a_task(), self.a_task(task_name="A second walk")

		over_http = self.message(f"{PREFIX}/mobile/claim_task", {"task": first})

		self.request({}, headers={}, remote_addr="100.64.0.7")
		frappe.local.session.user = WORKER
		in_process = mobile_api.claim_task(task=second)

		self.assertEqual(sorted(over_http), sorted(in_process))
		self.assertEqual(over_http["state"], in_process["state"])
		self.assertEqual(over_http["assigned_to"], in_process["assigned_to"])

	def test_a_refusal_carries_the_same_sentence_over_either_transport(self):
		_, body = self.refusal(MY_TASKS, {"company": OTHER})

		self.request({}, headers={}, remote_addr="100.64.0.7")
		frappe.local.session.user = WORKER
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.list_my_tasks(company=OTHER)

		self.assertEqual(body["error"], str(caught.exception))


# ── 7. the argument filter Frappe was doing ─────────────────────────────────
class TheArgumentFilter(FarmOpsAPITestCase):
	"""Frappe's handler binds a body to a whitelisted method by KEEPING THE KEYS
	THAT MATCH THE SIGNATURE. All eleven wrappers were written against that —
	naming every accepted argument instead of taking `**kwargs` is exactly what
	makes `record_data` and `worker_id` unreachable from a phone. This transport
	does not go through that handler, so it does the filtering itself."""

	def test_an_unknown_key_is_dropped_rather_than_crashing_the_call(self):
		"""`function(**body)` would 500 on a client that sent one extra field —
		which is what a newer app talking to an older server looks like."""
		body = self.message(CONTEXT, {"unexpected": 1, "client_version": "2.0", "extra": [1, 2]})
		self.assertEqual(body["user"], WORKER)

	def test_the_arguments_the_wrappers_refuse_are_still_unreachable(self):
		"""The four that are deliberately NOT forwarded: `cancel` would let a
		worker delete work instead of handing it back, `record_data` would let a
		phone compose a compliance record, `worker_id` would let an account name
		somebody else, `user` would let it BE somebody else."""
		for refused in ("cancel", "record_data", "worker_id", "user"):
			for route in ROUTES:
				with self.subTest(argument=refused, path=route.path):
					self.assertNotIn(refused, farmops_routes.accepted_arguments(route.handler))

	def test_the_status_the_identity_step_sends_never_reaches_the_personnel_register(self):
		"""`OnboardingIdentity.employeePayload` sends `"status": "Active"` on every
		hire, which is what `create_employee` writes anyway. What the argument would
		ALSO buy is a phone that can file somebody as Left on the day they started,
		so it is dropped here rather than forwarded — and `user_id`, which would
		point somebody else's task history at an account the body names, with it."""
		route = farmops_routes.BY_PATH["/mobile/create_employee"]
		for refused in ("status", "user_id"):
			with self.subTest(argument=refused):
				self.assertNotIn(refused, farmops_routes.accepted_arguments(route.handler))
				self.assertEqual(farmops_routes.bind(route, {refused: "Left"}), {})

	def test_both_housing_doors_accept_both_spellings_of_the_same_argument(self):
		"""v0.63.1. This filter is why the four aliases exist, and it is also why
		one spelling each was not enough: a body crossing from either name to the
		other lost the argument it was carrying, silently. `list_available_housing`
		and `list_housing_units` answer one question with the filter spelled two
		opposite ways; `assign_housing` and `create_housing_assignment` name one
		cabin and one date two ways. Every pair must survive `bind` at BOTH doors —
		a dropped filter here is a list of cabins nobody can be put in, and a
		dropped cabin is a hire refused for want of a field the phone sent."""
		pairs = {
			"list_available_housing": ("include_full", "assignable_only"),
			"list_housing_units": ("include_full", "assignable_only"),
			"assign_housing": ("unit", "housing_unit", "assigned_date", "check_in_date"),
			"create_housing_assignment": ("unit", "housing_unit", "assigned_date", "check_in_date"),
		}
		for method, spellings in pairs.items():
			route = farmops_routes.BY_PATH[f"/mobile/{method}"]
			accepted = farmops_routes.accepted_arguments(route.handler)
			for spelling in spellings:
				with self.subTest(method=method, argument=spelling):
					self.assertIn(spelling, accepted)
					self.assertEqual(farmops_routes.bind(route, {spelling: "kept"})[spelling], "kept")

	def test_the_barracks_flag_still_cannot_be_sent_to_the_older_housing_door(self):
		"""The one argument v0.63.1 did NOT alias across, and the reason both
		doors exist. `assign_housing` passes the flag as true on the caller's
		behalf under capacity and the capacity check refuses at it; declaring it
		there would hand a phone the argument that changes that answer. `company`
		stays off it for the same reason it always was — that door narrows by the
		caller's own entities and nothing else."""
		route = farmops_routes.BY_PATH["/mobile/assign_housing"]
		for refused in ("allow_multi_occupancy", "company"):
			with self.subTest(argument=refused):
				self.assertNotIn(refused, farmops_routes.accepted_arguments(route.handler))
				self.assertEqual(farmops_routes.bind(route, {refused: True}), {})

	def test_a_body_naming_another_user_is_answered_as_the_caller(self):
		"""An account that can name somebody else in a request body is not
		scoped to anything. Dropped here AND in `guard` — two locks, one door."""
		self.assertEqual(self.message(CONTEXT, {"user": OUTSIDER})["user"], WORKER)

	def test_a_rejection_cannot_smuggle_cancel_through_to_the_tool(self):
		self.a_camp()
		task = self.a_task()
		self.message(f"{PREFIX}/mobile/claim_task", {"task": task})
		self.message(
			f"{PREFIX}/mobile/reject_task",
			{"task": task, "reason": "The ladder is broken", "cancel": True},
		)
		self.assertNotEqual(frappe.db.get_value("Farm Task", task, "status"), "Cancelled")

	def test_the_auth_envelope_never_reaches_a_method_as_an_argument(self):
		"""`_auth` carries a live credential and arrives on every single call,
		because the app does not know which door it came in through."""
		self.assertEqual(
			self.message(CONTEXT, {"_auth": dict(self.credential)}, credential=False)["user"],
			WORKER,
		)
		for route in ROUTES:
			with self.subTest(path=route.path):
				self.assertNotIn(
					fallback_auth.BODY_KEY,
					farmops_routes.bind(route, {fallback_auth.BODY_KEY: dict(self.credential)}),
				)


class TheIdentityStepAnswersOverTheFunnel(FarmOpsAPITestCase):
	"""v0.46.0, and the whole of it: these three paths, over this transport, 200.

	The wizard asked Frappe's own `/api/resource/Employee` for all three, the
	funnel publishes `/farmops/api/…` and nothing else, and step 1 of five 404'd —
	so the nine methods v0.45.0 published were never reached from a phone either.
	A test that only checked the route table would have passed on the day the bug
	shipped; this one calls them the way the handset does.
	"""

	def setUp(self):
		super().setUp()
		install_hrms()
		# The role an operator has to grant before a phone can touch the personnel
		# register. See `api/mobile.py` — Farm Manager is the only role in both
		# `guard.FARM_OPS_ROLES` and `employee.HR_ROLES`.
		set_roles(WORKER, ["Field Worker", "Farm Manager"])

	def test_the_three_paths_the_wizard_opens_with_are_reachable(self):
		created = self.message(
			f"{PREFIX}/mobile/create_employee",
			{
				"first_name": "Elena",
				"last_name": "Marquez",
				"employee_name": "Elena Marquez",
				"gender": "Female",
				"date_of_birth": "1994-03-11",
				"company": MAIN,
				"status": "Active",
			},
		)
		self.assertTrue(created["name"])
		self.assertEqual(frappe.db.get_value("Employee", created["name"], "company"), MAIN)

		found = self.message(f"{PREFIX}/mobile/search_employees", {"query": "Marquez", "company": MAIN})
		self.assertEqual([row["name"] for row in found["employees"]], [created["name"]])

		frappe.db.set_value("Employee", created["name"], "status", "Left")
		self.message(f"{PREFIX}/mobile/reactivate_employee", {"employee": created["name"]})
		self.assertEqual(frappe.db.get_value("Employee", created["name"], "status"), "Active")

	def test_the_docname_spelling_the_swift_function_uses_works_too(self):
		created = self.message(
			f"{PREFIX}/mobile/create_employee",
			{"first_name": "Elena", "last_name": "Marquez", "company": MAIN},
		)
		frappe.db.set_value("Employee", created["name"], "status", "Inactive")
		self.message(f"{PREFIX}/mobile/reactivate_employee", {"docname": created["name"]})
		self.assertEqual(frappe.db.get_value("Employee", created["name"], "status"), "Active")

	def install_compliance_fields(self):
		"""Employee as `compliance_fields.py` really leaves a migrated site: three
		of its five columns MANDATORY. Built from that module's own specs so a flag
		changed there cannot pass unnoticed here."""
		from erpnext_mcp import compliance_fields

		from .harness import add_field

		for spec in compliance_fields.targets_by_doctype()["Employee"].fields:
			add_field(
				"Employee",
				spec.fieldname,
				fieldtype=spec.fieldtype,
				options=spec.options or None,
				label=spec.label,
				reqd=1 if spec.reqd else 0,
			)

	def test_the_wizards_own_payload_hires_somebody_on_a_site_with_the_compliance_fields(self):
		"""v0.46.1, and the second wall in front of step one. The path 404'd until
		v0.46.0; then it answered, and refused with 'this site's Frappe HR marks
		i9_status, w4_status, jurisdiction mandatory on Employee' — which Frappe HR
		had nothing to do with. Those are erpnext_mcp's own Custom Fields, installed
		`reqd=True` by its own `after_migrate`, so the app was refusing its own
		schema on every path it has: this one, `onboard_employee` and the MCP tool.

		The payload is `OnboardingIdentity.employeePayload` verbatim, which names
		none of the three and never will — the I-9 and the W-4 are steps 3 and 4 of
		the same wizard."""
		self.install_compliance_fields()
		created = self.message(
			f"{PREFIX}/mobile/create_employee",
			{
				"first_name": "Elena",
				"last_name": "Marquez",
				"employee_name": "Elena Marquez",
				"gender": "Female",
				"date_of_birth": "1994-03-11",
				"date_of_joining": "2026-08-07",
				"employment_type": "Seasonal Worker",
				"company": MAIN,
				"status": "Active",
			},
		)
		self.assertTrue(created["name"])
		row = frappe.db.get_value(
			"Employee", created["name"], ["i9_status", "w4_status", "jurisdiction"], as_dict=True
		)
		self.assertEqual(row["i9_status"], "Pending")
		self.assertEqual(row["w4_status"], "Missing")
		self.assertEqual(row["jurisdiction"], "OR")

	def test_a_build_that_does_ask_the_foreman_which_state_is_honoured(self):
		"""The three are forwarded rather than defaulted in the wrapper, so a later
		handset build that asks where the crew is working needs no server change."""
		self.install_compliance_fields()
		created = self.message(
			f"{PREFIX}/mobile/create_employee",
			{"first_name": "Elena", "last_name": "Marquez", "company": MAIN, "jurisdiction": "WA"},
		)
		self.assertEqual(frappe.db.get_value("Employee", created["name"], "jurisdiction"), "WA")

	def test_an_employee_of_another_entity_reads_as_not_found_rather_than_refused(self):
		"""The rule every docname argument on this surface follows. A phone that
		could tell "no such record" from "not yours" could enumerate the holding
		company's payroll one docname at a time."""
		STORE.seed(
			"Employee",
			[{"name": "EMP-ELSEWHERE", "employee_name": "Ben Ortiz", "company": OTHER, "status": "Left"}],
		)
		status, body = self.refusal(f"{PREFIX}/mobile/reactivate_employee", {"employee": "EMP-ELSEWHERE"})
		self.assertEqual(status, 404)
		self.assertIn("was not found", body["error"].lower())
		self.assertEqual(frappe.db.get_value("Employee", "EMP-ELSEWHERE", "status"), "Left")

	def test_a_search_never_answers_with_an_entity_this_caller_cannot_reach(self):
		STORE.seed(
			"Employee",
			[{"name": "EMP-ELSEWHERE", "employee_name": "Elena Ortiz", "company": OTHER}],
		)
		found = self.message(f"{PREFIX}/mobile/search_employees", {"query": "Elena"})
		self.assertNotIn("EMP-ELSEWHERE", [row["name"] for row in found["employees"]])

	def test_the_returning_workers_own_path_is_reachable_over_this_transport(self):
		"""v0.46.2. `getEmployeeDetail` was still asking Frappe's
		`/api/resource/Employee/<name>`, which the funnel does not carry, so the
		wizard could find a returning picker and then had nothing to decide with."""
		self.install_compliance_fields()
		created = self.message(
			f"{PREFIX}/mobile/create_employee",
			{"first_name": "Elena", "last_name": "Marquez", "company": MAIN},
		)
		detail = self.message(f"{PREFIX}/mobile/get_employee", {"employee": created["name"]})
		self.assertEqual(detail["name"], created["name"])
		self.assertEqual(detail["employee_name"], "Elena Marquez")
		# A brand-new hire: the columns are their hire-time defaults and there is
		# nothing on file to reconcile them against, so every step still needs doing.
		self.assertEqual(detail["i9_status"], "Pending")
		self.assertEqual(detail["w4_status"], "Missing")
		self.assertEqual(detail["jurisdiction"], "OR")
		self.assertIsNone(detail["badge_id"])
		self.assertEqual(detail["reconciled"], [])

	def test_the_docname_spelling_works_on_the_detail_call_too(self):
		created = self.message(
			f"{PREFIX}/mobile/create_employee",
			{"first_name": "Elena", "last_name": "Marquez", "company": MAIN},
		)
		detail = self.message(f"{PREFIX}/mobile/get_employee", {"docname": created["name"]})
		self.assertEqual(detail["name"], created["name"])

	def test_a_detail_read_never_answers_for_an_entity_this_caller_cannot_reach(self):
		STORE.seed(
			"Employee",
			[{"name": "EMP-ELSEWHERE", "employee_name": "Elena Ortiz", "company": OTHER}],
		)
		status, body = self.refusal(f"{PREFIX}/mobile/get_employee", {"employee": "EMP-ELSEWHERE"})
		self.assertEqual(status, 404)
		self.assertIn("was not found", body["error"].lower())

	def test_the_badge_step_is_answered_from_the_map_rather_than_a_column(self):
		"""`link_badge_to_employee` writes a Bucket Log Badge Map row; there is no
		`badge_id` on Employee to read, and an INACTIVE mapping is a badge that has
		to be issued again."""
		created = self.message(
			f"{PREFIX}/mobile/create_employee",
			{"first_name": "Elena", "last_name": "Marquez", "company": MAIN},
		)
		self.message(
			f"{PREFIX}/mobile/link_badge_to_employee",
			{"badge_id": "BADGE-77", "employee": created["name"], "company": MAIN},
		)
		self.assertEqual(
			self.message(f"{PREFIX}/mobile/get_employee", {"employee": created["name"]})["badge_id"],
			"BADGE-77",
		)

		frappe.db.set_value("Bucket Log Badge Map", "BADGE-77", "active", 0)
		self.assertIsNone(
			self.message(f"{PREFIX}/mobile/get_employee", {"employee": created["name"]})["badge_id"]
		)


#: The foreman's dashboard, Sprint 7 (v0.72.0).
DISPATCHED = f"{PREFIX}/mobile/list_dispatched_tasks"
ASSIGN = f"{PREFIX}/mobile/assign_farm_task"
RAISE = f"{PREFIX}/mobile/create_farm_task"
TEMPLATES = f"{PREFIX}/mobile/list_farm_task_templates"
FROM_TEMPLATE = f"{PREFIX}/mobile/create_task_from_template"

FOREMAN = "flor@example.test"
FOREMAN_EMPLOYEE = "EMP-FLOR"
STRANGER_EMPLOYEE = "EMP-DIEGO"
A_SHIFT = "SHIFT-2026-0001"


class TheForemanDashboard(FarmOpsAPITestCase):
	"""The five Sprint 5 routes, over the transport a handset actually uses.

	THE CLAIM UNDER TEST IS THAT DISPATCH IS NOT A PICKER'S. Every route above
	these is a worker's own work and is reachable by anybody enrolled; these five
	read somebody else's board and move somebody else's afternoon, and the whole
	of what stands between an enrolled Field Worker and them is
	`guard.require_dispatch_role` in each wrapper's own body. A test that only
	drove them as a foreman would assert the feature and none of the gate.

	THE SECOND CLAIM IS THE CREW SCOPE. `dispatch.list_dispatched_tasks` will read
	any named worker's board, so a wrapper that forwarded the name would publish
	"what is everybody on this farm doing today" to one enrolled handset. The
	workers are computed off the caller's own open shifts instead, and the tests
	below drive both halves: a name on the crew narrows, a name that is not on it
	is refused, and the tool's own `worker_id` spelling never arrives at all.
	"""

	def setUp(self):
		super().setUp()
		STORE.seed(
			"Employee",
			[
				{
					"name": FOREMAN_EMPLOYEE,
					"employee_name": "Flor Diaz",
					"user_id": FOREMAN,
					"company": MAIN,
					"status": "Active",
				},
				{
					"name": STRANGER_EMPLOYEE,
					"employee_name": "Diego Salas",
					"company": MAIN,
					"status": "Active",
				},
			],
		)
		self.foreman = self.enrol(email=FOREMAN, name="Flor Diaz", role="Foreman")
		self.a_camp()
		# The five templates this app ships. Seeded rather than hand-built so the
		# register a foreman scrolls in these tests is the register a foreman
		# scrolls on a site that has only ever been migrated.
		task_templates.seed_farm_task_templates()

	# ── the site's furniture ────────────────────────────────────────────────
	def an_open_shift(self, crew=(WORKER_EMPLOYEE,), foreman=FOREMAN_EMPLOYEE, name=A_SHIFT):
		"""One shift with no end time — which is what `shifts.status_for` calls open.

		The crew goes on the parent as `crew`, because that is where it lives: the
		double flattens the child table out of its parent exactly as
		`shifts.crew_of` reads it back off the site.
		"""
		STORE.seed(
			"Farm Shift",
			[
				{
					"name": name,
					"foreman": foreman,
					"foreman_name": "Flor Diaz",
					"company": MAIN,
					"location": "Block 7 North",
					"shift_type": "Harvest",
					"start_datetime": f"{frappe.utils.today()} 06:00:00",
					"end_datetime": "",
					"cancelled": 0,
					"status": "Active",
					"crew": [
						{
							"name": f"{name}-CREW-{index}",
							"idx": index,
							"employee": person,
							"employee_name": str(
								frappe.db.get_value("Employee", person, "employee_name") or ""
							),
							"role": "Worker",
							"joined_at": f"{frappe.utils.today()} 06:00:00",
							"left_at": "",
						}
						for index, person in enumerate(crew, start=1)
					],
				}
			],
		)
		return name

	def a_dispatched_task(self, worker=WORKER_EMPLOYEE, **overrides):
		"""A task already in somebody's hands, so there is a board to read."""
		return self.a_task(assigned_to=worker, **overrides)

	# ── the gate ────────────────────────────────────────────────────────────
	def test_an_enrolled_field_worker_is_refused_every_one_of_the_five(self):
		"""Ana holds a perfectly good credential, an Active grant and MAIN. What
		she does not hold is Foreman, and that is the whole of the refusal."""
		for path, body in (
			(DISPATCHED, {}),
			(ASSIGN, {"task": "FT-WHATEVER", "assigned_to": WORKER_EMPLOYEE}),
			(RAISE, {"task_name": "x", "task_type": "Inspection", "evidence_required": dict(WALK)}),
			(TEMPLATES, {}),
			(FROM_TEMPLATE, {"template": "whatever"}),
		):
			with self.subTest(path=path):
				status, parsed = self.refusal(path, body)
				self.assertEqual(status, 403)
				self.assertIn("Foreman", parsed["error"])

	def test_the_refusal_names_what_a_picker_may_still_do(self):
		"""A field worker told only 'no' taps the button again. The sentence lists
		the seven routes that ARE theirs, which is a thing they can act on."""
		_status, parsed = self.refusal(DISPATCHED)
		self.assertIn("claim_task", parsed["error"])
		self.assertIn("Nothing was read and nothing was changed.", parsed["error"])

	def test_a_foreman_holding_the_role_gets_through(self):
		self.an_open_shift()
		answer = self.message(DISPATCHED, credential=self.foreman)
		self.assertEqual(answer["shifts"], [A_SHIFT])

	# ── the crew scope ──────────────────────────────────────────────────────
	def test_the_board_is_the_crew_on_this_foremans_own_open_shift(self):
		self.an_open_shift()
		task = self.a_dispatched_task()
		answer = self.message(DISPATCHED, credential=self.foreman)

		crew = {entry["employee"]: entry for entry in answer["crew"]}
		# Ana is on the crew; the foreman is in the answer whether or not they
		# rostered themselves; Diego is on neither shift nor answer.
		self.assertIn(WORKER_EMPLOYEE, crew)
		self.assertIn(FOREMAN_EMPLOYEE, crew)
		self.assertNotIn(STRANGER_EMPLOYEE, crew)
		self.assertEqual([row["name"] for row in crew[WORKER_EMPLOYEE]["tasks"]], [task])
		self.assertEqual(answer["count"], 1)

	def test_a_foreman_with_no_open_shift_gets_their_own_board_and_is_told_why(self):
		"""Not an empty answer and not an unscoped one. A dashboard that showed
		nothing before roll call would read as 'no work today'."""
		self.a_dispatched_task()
		answer = self.message(DISPATCHED, credential=self.foreman)
		self.assertEqual(answer["shifts"], [])
		self.assertEqual([entry["employee"] for entry in answer["crew"]], [FOREMAN_EMPLOYEE])
		self.assertIn("no open shift", answer["note"])

	def test_a_name_on_the_crew_narrows_the_board(self):
		self.an_open_shift()
		task = self.a_dispatched_task()
		answer = self.message(DISPATCHED, {"employee": WORKER_EMPLOYEE}, credential=self.foreman)
		self.assertEqual([entry["employee"] for entry in answer["crew"]], [WORKER_EMPLOYEE])
		self.assertEqual([row["name"] for row in answer["crew"][0]["tasks"]], [task])

	def test_a_name_that_is_not_on_the_crew_is_refused_rather_than_answered(self):
		"""The check this whole wrapper exists for. Without it, one enrolled
		handset could walk the payroll a docname at a time."""
		self.an_open_shift()
		self.a_dispatched_task(worker=STRANGER_EMPLOYEE)
		status, parsed = self.refusal(DISPATCHED, {"employee": STRANGER_EMPLOYEE}, credential=self.foreman)
		self.assertEqual(status, 403)
		self.assertIn("not on the crew", parsed["error"])

	def test_the_tools_own_worker_id_spelling_never_reaches_it(self):
		"""`worker_id` is undeclared on purpose, so `bind` drops it. The board comes
		back as the whole crew rather than as the one worker the body named."""
		self.an_open_shift()
		self.a_dispatched_task(worker=STRANGER_EMPLOYEE)
		answer = self.message(DISPATCHED, {"worker_id": STRANGER_EMPLOYEE}, credential=self.foreman)
		self.assertNotIn("worker_id", farmops_routes.accepted_arguments(mobile_api.list_dispatched_tasks))
		self.assertEqual(
			sorted(entry["employee"] for entry in answer["crew"]),
			sorted([FOREMAN_EMPLOYEE, WORKER_EMPLOYEE]),
		)

	def test_another_foremans_shift_is_not_this_foremans_board(self):
		self.an_open_shift(crew=(STRANGER_EMPLOYEE,), foreman=WORKER_EMPLOYEE, name="SHIFT-2026-0009")
		status, parsed = self.refusal(DISPATCHED, {"shift": "SHIFT-2026-0009"}, credential=self.foreman)
		self.assertEqual(status, 403)
		self.assertIn("not a shift you have open", parsed["error"])

	# ── the dispatch ────────────────────────────────────────────────────────
	def test_a_foreman_dispatches_an_unclaimed_task(self):
		task = self.a_task()
		answer = self.message(ASSIGN, {"task": task, "assigned_to": WORKER_EMPLOYEE}, credential=self.foreman)
		self.assertEqual(answer["name"], task)
		self.assertEqual(answer["state"], "Claimed")
		self.assertEqual(answer["assigned_to"], WORKER_EMPLOYEE)
		self.assertIsNone(answer["reassigned_from"])

	def test_taking_work_off_somebody_needs_reassign_and_then_needs_a_reason(self):
		"""Both halves of `dispatch.assign_farm_task`'s refusal, forwarded intact.
		The wrapper does not restate the rule — it makes sure the two arguments
		that satisfy it can arrive."""
		task = self.a_dispatched_task()
		status, parsed = self.refusal(
			ASSIGN, {"task": task, "assigned_to": STRANGER_EMPLOYEE}, credential=self.foreman
		)
		self.assertEqual(status, 400)
		self.assertIn("reassign=true", parsed["error"])

		status, parsed = self.refusal(
			ASSIGN,
			{"task": task, "assigned_to": STRANGER_EMPLOYEE, "reassign": True},
			credential=self.foreman,
		)
		self.assertEqual(status, 400)
		self.assertIn("needs a reason", parsed["error"])

		answer = self.message(
			ASSIGN,
			{
				"task": task,
				"assigned_to": STRANGER_EMPLOYEE,
				"reassign": True,
				"reason": "Ana is on the packing line all afternoon",
			},
			credential=self.foreman,
		)
		self.assertEqual(answer["assigned_to"], STRANGER_EMPLOYEE)
		self.assertEqual(answer["reassigned_from"], "Ana Ramos")

	def test_the_dispatched_workers_name_cannot_be_written_from_the_body(self):
		"""`assigned_to_name` is undeclared, so a dispatch record cannot be made to
		say somebody was sent who was not. The register supplies the name."""
		task = self.a_task()
		answer = self.message(
			ASSIGN,
			{"task": task, "assigned_to": WORKER_EMPLOYEE, "assigned_to_name": "Somebody Else"},
			credential=self.foreman,
		)
		self.assertEqual(answer["assigned_to_name"], "Ana Ramos")
		self.assertNotIn("assigned_to_name", farmops_routes.accepted_arguments(mobile_api.assign_farm_task))

	def test_an_employee_of_another_entity_reads_as_not_found(self):
		task = self.a_task()
		status, _ = self.refusal(
			ASSIGN, {"task": task, "assigned_to": OUTSIDER_EMPLOYEE}, credential=self.foreman
		)
		self.assertEqual(status, 404)

	# ── the task raised on the spot ─────────────────────────────────────────
	def test_a_task_raised_from_the_handset_is_published_and_carries_its_contract(self):
		answer = self.message(
			RAISE,
			{
				"task_name": "Fix the gate latch — Block 7",
				"task_type": "Repair",
				"evidence_required": dict(WALK),
				"urgency": "High",
				"skill_required": "camp_maintenance",
			},
			credential=self.foreman,
		)
		self.assertEqual(answer["state"], "Available")
		self.assertEqual(answer["urgency"], "High")
		self.assertEqual(answer["company"], MAIN)
		self.assertTrue(answer["evidence_required"])

	def test_the_four_arguments_a_handset_may_not_compose_are_undeclared(self):
		"""`creates_record` and `creates_record_data` write a compliance record and
		its fields; `draft` hides the work from every other handset; `source_alert`
		is `rectify_alert`'s to link. None of them has a parameter to land in."""
		accepted = farmops_routes.accepted_arguments(mobile_api.create_farm_task)
		for argument in ("creates_record", "creates_record_data", "draft", "source_alert", "materials_used"):
			self.assertNotIn(argument, accepted)

	def test_a_task_raised_with_no_evidence_contract_is_refused_by_name(self):
		status, parsed = self.refusal(
			RAISE, {"task_name": "x", "task_type": "Inspection"}, credential=self.foreman
		)
		self.assertEqual(status, 400)
		self.assertIn("evidence_required", parsed["error"])

	# ── the template register ───────────────────────────────────────────────
	def test_the_template_register_answers_and_says_which_may_raise_work(self):
		answer = self.message(TEMPLATES, credential=self.foreman)
		self.assertTrue(answer["templates"])
		self.assertTrue(answer["enabled_templates"])
		self.assertEqual(answer["count"], len(answer["templates"]))

	def test_one_task_raised_from_a_template_carries_the_templates_shape(self):
		listed = self.message(TEMPLATES, credential=self.foreman)
		template = listed["enabled_templates"][0]
		shaped = {entry["name"]: entry for entry in listed["templates"]}[template]

		answer = self.message(
			FROM_TEMPLATE,
			{"template": template, "location_doctype": "Housing Unit", "location": self.unit},
			credential=self.foreman,
		)
		self.assertEqual(answer["template"], template)
		self.assertEqual(answer["task_type"], shaped["task_type"])
		self.assertEqual(answer["state"], "Available")
		self.assertEqual(answer["location"], self.unit)

	def test_the_authoring_calls_are_not_on_this_surface_at_all(self):
		"""Reading the register is a foreman's; deciding what a recurring job asks
		for is a desk decision with the regulation open."""
		for absent in (
			"create_farm_task_template",
			"update_farm_task_template",
			"get_farm_task_template",
		):
			self.assertFalse(hasattr(mobile_api, absent), absent)


# ── 8. the row and the secrets ──────────────────────────────────────────────
class TheOnboardingEvidenceActuallyLands(FarmOpsAPITestCase):
	"""v0.48.3, over HTTP, because in-process the bug was invisible.

	THE DEFECT WAS A PATH, NOT A FUNCTION. `OnboardingAPI.attachDocument` posted
	multipart to Frappe's own `/api/method/upload_file`. Every server-side test in
	this repo would have passed on the day that shipped, because no server-side
	test can see which URL a phone chose — and `fallback_auth._is_mobile_path`
	matches `/api/method/erpnext_mcp.api.` and nothing else, so the
	`X-FarmOps-Token` header was never read on that path. Behind the Tailscale
	funnel, which strips `Authorization`, the request arrived as Guest, Frappe
	answered HTTP 200 with the Desk login page, and the app returned on any 2xx
	without reading the body. Six pieces of I-9 and W-4 evidence per hire were
	reported filed and stored nowhere.

	SO THE CLAIM HERE IS THE ONE THAT MATTERS LEGALLY: the whole path a phone now
	takes — stage, finalize, attach — works over this transport with a
	credential, and every step of it is refused without one. Not "the function
	returns a dict": a File on the site, private, attached to the Employee, and
	an unauthenticated caller who gets 401 rather than a cheerful nothing.
	"""

	EMPLOYEE = "EMP-EVIDENCE"

	def setUp(self):
		super().setUp()
		install_hrms()
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		STORE.seed(
			"Employee",
			[
				{
					"name": self.EMPLOYEE,
					"employee_name": "Rosa Delgado",
					"first_name": "Rosa",
					"last_name": "Delgado",
					"company": MAIN,
					"status": "Active",
					"date_of_joining": frappe.utils.today(),
				}
			],
		)

	def stage_and_finalize(self, name="i9_list_b_doc.jpg", **kwargs):
		"""The two file calls, over HTTP, exactly as `ChunkUploader` makes them."""
		import base64
		import hashlib

		body = b"evidence-bytes"
		upload_id = f"funnel-{name}"
		self.message(
			f"{PREFIX}/files/stage_file_chunk",
			{
				"upload_id": upload_id,
				"file_name": name,
				"chunk_index": 0,
				"chunk_count": 1,
				"total_bytes": len(body),
				"data": base64.b64encode(body).decode(),
			},
			**kwargs,
		)
		return self.message(
			f"{PREFIX}/files/finalize_staged_file",
			{
				"upload_id": upload_id,
				"file_name": name,
				"sha256": hashlib.sha256(body).hexdigest(),
				"total_bytes": len(body),
			},
			**kwargs,
		)

	def test_the_whole_upload_path_works_over_the_funnel_with_a_credential(self):
		finalized = self.stage_and_finalize()
		attached = self.message(
			f"{PREFIX}/mobile/attach_onboarding_document",
			{
				"employee": self.EMPLOYEE,
				"file_token": finalized["file_token"],
				"document_kind": "i9_list_b_document",
			},
		)
		self.assertEqual(attached["file_token"], finalized["file_token"])

		row = frappe.db.get_value(
			"File",
			finalized["file_token"],
			["attached_to_doctype", "attached_to_name", "is_private"],
			as_dict=True,
		)
		self.assertEqual(row["attached_to_doctype"], "Employee")
		self.assertEqual(row["attached_to_name"], self.EMPLOYEE)
		self.assertEqual(int(row["is_private"] or 0), 1)

	def test_an_upload_with_no_credential_is_refused_rather_than_silently_accepted(self):
		"""THE ASSERTION THIS RELEASE IS ABOUT. The old path answered 200 to
		exactly this request. Every step of the new one answers 401, and 401 is
		what the app treats as "this credential is dead" rather than as success."""
		for path, body in (
			(f"{PREFIX}/files/stage_file_chunk", {"upload_id": "x", "file_name": "a.jpg"}),
			(f"{PREFIX}/files/finalize_staged_file", {"upload_id": "x", "file_name": "a.jpg"}),
			(
				f"{PREFIX}/mobile/attach_onboarding_document",
				{"employee": self.EMPLOYEE, "file_token": "whatever"},
			),
		):
			with self.subTest(path=path):
				status, parsed = self.refusal(path, body, credential=False)
				self.assertEqual(status, 401)
				# And it is JSON, not a login page. The v0.17.x symptom in one line.
				self.assertIn("error", parsed)

	def test_nothing_is_attached_when_the_credential_is_refused(self):
		"""A 401 that had already written would be worse than the silent success
		it replaces. The staged upload happens as the worker; the unauthenticated
		attach must not move the File it produced."""
		finalized = self.stage_and_finalize()
		status, _ = self.refusal(
			f"{PREFIX}/mobile/attach_onboarding_document",
			{"employee": self.EMPLOYEE, "file_token": finalized["file_token"]},
			credential=False,
		)
		self.assertEqual(status, 401)
		self.assertFalse(frappe.db.get_value("File", finalized["file_token"], "attached_to_name"))

	def test_a_worker_without_a_hiring_role_cannot_file_onboarding_evidence(self):
		"""These are the photographs an employer is inspected on, and a picker is
		not who files them.

		v0.94.0 MOVED THIS GATE FROM `HR_ROLES` TO `HIRING_ROLES` and a Field
		Worker is on neither, so the refusal survives the widening — it just names
		the hire now instead of the register.

		400 AND NOT 401, which is the part that matters to a handset:
		`employee.require_hiring_role` raises `ToolError`, `guard.endpoint` turns
		that into a `frappe.ValidationError` so its sentence reaches the phone in
		`_server_messages`, and `app._status_for` answers 400. The credential is
		real, the app stays signed in, and the day's queued work is not discarded
		— which is what a 401 would do.
		"""
		finalized = self.stage_and_finalize()
		set_roles(WORKER, ["Field Worker"])
		status, parsed = self.refusal(
			f"{PREFIX}/mobile/attach_onboarding_document",
			{"employee": self.EMPLOYEE, "file_token": finalized["file_token"]},
		)
		self.assertEqual(status, 400)
		self.assertIn("may not bring a person onto the farm", parsed["error"])
		self.assertFalse(frappe.db.get_value("File", finalized["file_token"], "attached_to_name"))

	def test_but_a_foreman_files_them_over_the_wire(self):
		"""THE WIDENING, ON THE TRANSPORT THE PHONE ACTUALLY USES.

		An in-process tool test would not prove the sidecar route accepts this —
		and step 7 of a hire is where v0.48.3's whole argument lands: the person
		holding the phone that photographed the licence is the person sitting with
		the new hire. This is the assertion that the evidence now lands from that
		phone rather than from a desk that does not exist on this farm.
		"""
		finalized = self.stage_and_finalize()
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.post(
			f"{PREFIX}/mobile/attach_onboarding_document",
			{"employee": self.EMPLOYEE, "file_token": finalized["file_token"]},
		)
		self.assertEqual(
			frappe.db.get_value("File", finalized["file_token"], "attached_to_name"),
			self.EMPLOYEE,
		)

	def test_an_employee_outside_the_callers_entities_is_not_found(self):
		"""`require_scoped_doc` makes "does not exist" and "not yours" the same
		answer, and a file cannot be hung on another entity's personnel record."""
		STORE.seed(
			"Employee",
			[
				{
					"name": "EMP-ELSEWHERE",
					"employee_name": "Somebody Else",
					"company": OTHER,
					"status": "Active",
				}
			],
		)
		finalized = self.stage_and_finalize()
		status, _ = self.refusal(
			f"{PREFIX}/mobile/attach_onboarding_document",
			{"employee": "EMP-ELSEWHERE", "file_token": finalized["file_token"]},
		)
		self.assertEqual(status, 404)


class TheAuditRow(FarmOpsAPITestCase):
	def test_a_call_over_this_transport_writes_the_same_mobile_row(self):
		self.message(CONTEXT)
		row = self.audit_rows("get_current_user_context")[-1]
		self.assertEqual(row["result_status"], audit.STATUS_SUCCESS)
		self.assertIn(WORKER, row["result_summary"])

	def test_a_refusal_is_audited_too(self):
		self.refusal(MY_TASKS, {"company": OTHER})
		row = self.audit_rows("list_my_tasks")[-1]
		self.assertEqual(row["result_status"], audit.STATUS_UNAUTHORIZED)

	def test_the_row_records_the_ip_the_proxy_appended_and_not_the_one_claimed(self):
		"""Rightmost `X-Forwarded-For` hop. An audit log that records an
		attacker's chosen address is worse than one that records none, because it
		looks like evidence."""
		self.post(CONTEXT, headers={"X-Forwarded-For": "1.2.3.4, 100.64.0.9"})
		self.assertEqual(self.audit_rows("get_current_user_context")[-1]["caller_ip"], "100.64.0.9")

	def test_an_unauthenticated_flood_cannot_grow_the_log(self):
		"""The refusal happens before any method, so there is no method to audit
		— and a caller who can write rows without a credential can push an
		operator's real evidence out of view."""
		before = len(STORE.rows("MCP Action Log"))
		for _ in range(20):
			self.post(CONTEXT, credential=False)
		self.assertEqual(len(STORE.rows("MCP Action Log")), before)


class NoSecretReachesThePhone(FarmOpsAPITestCase):
	def test_no_response_from_any_route_carries_a_credential_shaped_key(self):
		self.a_camp()
		task = self.a_task()
		for path, body in (
			("/mobile/get_current_user_context", {}),
			("/mobile/list_my_tasks", {}),
			("/mobile/list_available_tasks", {}),
			("/mobile/get_task", {"task": task}),
			("/mobile/list_compliance_alerts", {}),
		):
			with self.subTest(path=path):
				text = json.dumps(self.message(f"{PREFIX}{path}", body)).lower()
				for hint in ("api_secret", "password", "auth_header"):
					self.assertNotIn(hint, text)

	def test_the_credential_the_caller_presented_is_never_echoed_back(self):
		text = json.dumps(self.payload(self.post(CONTEXT, {"_auth": dict(self.credential)})))
		self.assertNotIn(self.credential["api_secret"], text)

	def test_a_refusal_does_not_echo_the_credential_either(self):
		response = self.post(CONTEXT, {"_auth": dict(self.credential)}, token="bad:bad")
		self.assertNotIn(self.credential["api_secret"], response.get_data(as_text=True))


# ── the session lifecycle ───────────────────────────────────────────────────
class TheSessionLifecycle(FarmOpsAPITestCase):
	"""Frappe's own WSGI app opens, authenticates, dispatches, commits and
	destroys. This service replaces one of those five and has to do the rest."""

	def test_a_write_is_committed_or_the_worker_loses_the_task_on_refresh(self):
		"""Without the commit a worker claims a task, gets a 200, and finds it
		unclaimed on the next pull-to-refresh."""
		self.a_camp()
		task = self.a_task()
		committed = []
		original = STORE.commit
		STORE.commit = lambda: (committed.append(True), original())[1]
		self.addCleanup(setattr, STORE, "commit", original)

		self.message(f"{PREFIX}/mobile/claim_task", {"task": task})
		self.assertTrue(committed, "a mutating call did not commit")

	def test_a_read_commits_too_so_the_idle_grant_sweep_still_has_a_stamp(self):
		"""`_stamp_last_seen` writes from inside a read, and that stamp is the
		only thing `sweep_idle_grants` has to go on when it decides whether a
		token belongs to a phone lost in an orchard a month ago."""
		frappe.db.set_value("Mobile Access Grant", WORKER, "last_seen_on", None)
		self.message(CONTEXT)
		self.assertTrue(frappe.db.get_value("Mobile Access Grant", WORKER, "last_seen_on"))

	def test_one_callers_identity_does_not_survive_into_the_next_call(self):
		"""`frappe.local` is per-request scratch on a real site. A worker-lifetime
		one would let a previous caller's answer be read in this caller's row."""
		self.message(CONTEXT)
		other = self.enrol(email=OUTSIDER, name="Ben Ortiz", role="Foreman", entities=[OTHER])
		self.assertEqual(self.message(CONTEXT, credential=other)["user"], OUTSIDER)
		self.assertIn(OUTSIDER, self.audit_rows("get_current_user_context")[-1]["result_summary"])

	def test_the_site_comes_from_the_environment_the_container_already_sets(self):
		"""`SITE_NAME: frontend` is on the ERPNext service in the Umbrel compose.
		Reading it means the sidecar and the site cannot be configured apart."""
		import os

		self.assertEqual(farmops_session.site_name(), "frontend")
		os.environ["FARMOPS_API_SITE"] = "somewhere.else"
		self.addCleanup(os.environ.pop, "FARMOPS_API_SITE", None)
		self.assertEqual(farmops_session.site_name(), "somewhere.else")


class TheOldPathIsUntouched(FarmOpsAPITestCase):
	"""v0.18.0 adds a transport. It does not take one away — the whitelisted
	path works on the LAN, from inside the container, and is the fallback on the
	day this service is the thing that is down."""

	def test_the_whitelisted_methods_still_answer_in_process(self):
		self.request({}, headers={}, remote_addr="100.64.0.7")
		frappe.local.session.user = WORKER
		self.assertEqual(mobile_api.get_current_user_context()["user"], WORKER)

	def test_the_auth_hook_still_resolves_the_header_on_the_old_path(self):
		"""`fallback_auth.resolve` was refactored to share one verifier with the
		sidecar. It has to still do what v0.17.2 shipped it to do."""
		self.request(
			{},
			token=False,
			headers={fallback_auth.HEADER: f"{self.credential['api_key']}:{self.credential['api_secret']}"},
			remote_addr="100.64.0.7",
			path="/api/method/erpnext_mcp.api.mobile.get_current_user_context",
		)
		frappe.local.session.user = "Guest"
		self.assertEqual(mobile_api.get_current_user_context()["user"], WORKER)
		self.assertEqual(fallback_auth.source(), fallback_auth.SOURCE_HEADER)

	def test_the_shared_verifier_answers_the_same_for_both_doors(self):
		self.assertEqual(
			fallback_auth.verify_credential(self.credential["api_key"], self.credential["api_secret"]),
			WORKER,
		)
		self.assertEqual(fallback_auth.verify_credential("nope", "x" * 56), "")
		self.assertEqual(fallback_auth.verify_credential("", ""), "")


class ROLESIsRestored(FarmOpsAPITestCase):
	"""The base class rewrites Administrator's roles to prove the grant gate
	holds. Leaving them rewritten silently broke twelve workflow tests two files
	away once already; this is the guard against it happening from here."""

	def test_the_shared_role_map_is_the_one_the_suite_started_with(self):
		self.assertIn("Administrator", ROLES)


# ── 16. field asset registration (v0.78.0) ──────────────────────────────────
class TheFieldRegistrationFlow(FarmOpsAPITestCase):
	"""The three Sprint 8 routes, over the transport a handset actually uses.

	THE FIRST CLAIM IS THAT THE FLOW COMPLETES. Photograph the plate, register
	the asset, get the printable tag back, file the photograph against it. Each
	of those was an MCP tool with no route, so the iOS screens — which are
	already built — stopped at step two with a 404.

	THE SECOND CLAIM IS THE ONE THAT MATTERS MORE. `attach_file_to_document`
	will attach a file to ANY document on this site, and publishing it unmodified
	would put "grow the evidence on a submitted Journal Entry" and "add a page to
	a verified I-9" inside a picker's credential. Half the tests below drive the
	allowlist, the scope check and the argument that is deliberately not on the
	signature at all.
	"""

	def setUp(self):
		super().setUp()
		self.configure(
			enabled=1,
			public_url="https://umbrel.tail4a2b.ts.net",
			allow_register_asset=1,
			allow_generate_asset_qr=1,
			allow_attach_file_to_document=1,
		)

	def register(self, **overrides):
		body = {"name": "MC-Tractor-07", "asset_type": "Tractor", "company": MAIN}
		body.update(overrides)
		return self.message(f"{PREFIX}/mobile/register_asset", body)

	def a_file(self, name="FILE-PLATE-01", private=1):
		STORE.seed(
			"File",
			[
				{
					"name": name,
					"file_name": "plate.jpg",
					"file_url": f"/private/files/{name}.jpg",
					"is_private": private,
					"attached_to_doctype": None,
					"attached_to_name": None,
				}
			],
		)
		return name

	# ── the flow ─────────────────────────────────────────────────────────────
	def test_a_worker_can_register_an_asset_from_the_field(self):
		data = self.register()
		self.assertEqual(data["name"], "MC-Tractor-07")
		self.assertEqual(data["asset_type"], "Tractor")

	def test_the_docname_is_the_printed_tag_and_the_qr_encodes_it(self):
		self.register()
		qr = self.message(f"{PREFIX}/mobile/generate_asset_qr", {"asset_name": "MC-Tractor-07"})
		self.assertIn("MC-Tractor-07", qr["qr_url"])
		self.assertTrue(qr["png_base64"])

	def test_the_qr_bytes_travel_in_the_answer_and_not_as_a_file_url(self):
		"""This door authenticates with `X-FarmOps-Token`, and a private File URL
		is a login page to it — the same reason the badge PNG and the `.pkpass`
		come back inline."""
		self.register()
		qr = self.message(f"{PREFIX}/mobile/generate_asset_qr", {"asset_name": "MC-Tractor-07"})
		self.assertNotIn("file_url", qr)
		self.assertGreater(qr["png_bytes"], 0)

	def test_the_photograph_can_be_filed_against_the_asset_it_registered(self):
		self.register()
		data = self.message(
			f"{PREFIX}/mobile/attach_file_to_document",
			{
				"doctype": "Asset Register",
				"name": "MC-Tractor-07",
				"file_name": "plate.jpg",
				"file_url": "/private/files/plate.jpg",
			},
		)
		self.assertEqual(data["attached_to_name"], "MC-Tractor-07")
		self.assertEqual(data["attached_to_doctype"], "Asset Register")

	def test_registration_carries_the_capital_columns_an_adjuster_asks_for(self):
		data = self.register(serial_number="1M0R4045ABC123456", model="John Deere 5075E")
		self.assertEqual(data["serial_number"], "1M0R4045ABC123456")
		self.assertEqual(data["model"], "John Deere 5075E")

	def test_registration_carries_the_service_schedule(self):
		"""The one moment somebody has the manual open is the moment they are
		entering the machine."""
		data = self.register(service_interval_hours=250)
		self.assertEqual(data["service_interval_hours"], 250.0)

	# ── the scope ────────────────────────────────────────────────────────────
	def test_the_company_is_the_callers_and_not_the_bodys(self):
		"""An account that can register a tractor into somebody else's entity is
		not scoped to anything, and an asset is what every later inspection,
		spray log and insurance line hangs off."""
		status, _body = self.refusal(
			f"{PREFIX}/mobile/register_asset",
			{"name": "MC-Tractor-08", "asset_type": "Tractor", "company": OTHER},
		)
		self.assertEqual(status, 403)

	def test_a_qr_for_another_entitys_machine_is_refused(self):
		STORE.seed(
			"Asset Register",
			[{"name": "SEL-Tractor-01", "asset_type": "Tractor", "company": OTHER}],
		)
		status, _ = self.refusal(f"{PREFIX}/mobile/generate_asset_qr", {"asset_name": "SEL-Tractor-01"})
		self.assertGreaterEqual(status, 400)

	def test_an_attach_against_another_entitys_record_is_refused(self):
		STORE.seed(
			"Asset Register",
			[{"name": "SEL-Tractor-02", "asset_type": "Tractor", "company": OTHER}],
		)
		status, _ = self.refusal(
			f"{PREFIX}/mobile/attach_file_to_document",
			{
				"doctype": "Asset Register",
				"name": "SEL-Tractor-02",
				"file_name": "plate.jpg",
				"file_url": "/private/files/plate.jpg",
			},
		)
		self.assertGreaterEqual(status, 400)

	# ── the allowlist ────────────────────────────────────────────────────────
	def test_a_doctype_off_the_allowlist_is_refused_by_name(self):
		status, refused = self.refusal(
			f"{PREFIX}/mobile/attach_file_to_document",
			{
				"doctype": "Journal Entry",
				"name": "JE-0001",
				"file_name": "statement.pdf",
				"file_url": "/private/files/statement.pdf",
			},
		)
		self.assertEqual(status, 403)
		self.assertIn("Journal Entry", json.dumps(refused))

	def test_the_refusal_lists_what_a_handset_may_attach_to(self):
		_, body = self.refusal(
			f"{PREFIX}/mobile/attach_file_to_document",
			{
				"doctype": "Journal Entry",
				"name": "JE-0001",
				"file_name": "statement.pdf",
				"file_url": "/private/files/x.pdf",
			},
		)
		self.assertIn("Asset Register", json.dumps(body))

	def test_employee_is_deliberately_off_the_allowlist(self):
		"""`attach_onboarding_document` is the door onto personnel evidence and
		it checks the HR role. A second door with one fewer gate on it is the
		thing this list exists to prevent."""
		_, body = self.refusal(
			f"{PREFIX}/mobile/attach_file_to_document",
			{
				"doctype": "Employee",
				"name": WORKER_EMPLOYEE,
				"file_name": "passport.jpg",
				"file_url": "/private/files/passport.jpg",
			},
		)
		self.assertIn("attach_onboarding_document", json.dumps(body))

	def test_allow_cancelled_is_not_on_the_signature_so_bind_cannot_deliver_it(self):
		"""The table's own argument filter is what makes it unreachable — not a
		check inside the wrapper that a later edit could drop."""
		accepted = farmops_routes.accepted_arguments(mobile_api.attach_file_to_document)
		self.assertNotIn("allow_cancelled", accepted)
		self.assertIn("doctype", accepted)

	def test_the_asset_registration_wrapper_takes_both_parent_spellings(self):
		"""`bind` keeps only the keys a signature names, and `asset_tags._parent`
		refuses the two only when they DISAGREE — so dropping either spelling
		here would silently unparent a valve."""
		accepted = farmops_routes.accepted_arguments(mobile_api.register_asset)
		self.assertIn("parent_asset", accepted)
		self.assertIn("location", accepted)

	def test_the_registration_wrapper_declares_the_tag_id_as_name(self):
		"""Renaming it to `asset_name` would make `bind` drop the tag ID off
		every registration, because the tool declares `name`."""
		self.assertIn("name", farmops_routes.accepted_arguments(mobile_api.register_asset))

	def test_every_allowlisted_doctype_carries_the_column_the_scope_check_reads(self):
		"""`guard.require_scoped_doc` decides reachability by reading `company`.
		A doctype on this list without that column would be scoped by nothing at
		all — which is how `Asset State Log` came off it."""
		from .harness import META

		for doctype in mobile_api.ATTACHABLE_DOCTYPES:
			with self.subTest(doctype=doctype):
				self.assertIsNotNone(
					META[doctype].get_field("company"),
					f"{doctype} is attachable from a handset and has no company column",
				)

	def test_the_append_only_state_log_is_not_attachable(self):
		"""It has no company of its own, and a photograph taken at a state change
		already has a home in `log_asset_state_change`'s `photo_file_token`."""
		self.assertNotIn("Asset State Log", mobile_api.ATTACHABLE_DOCTYPES)
