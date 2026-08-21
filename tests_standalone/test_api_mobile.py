# SPDX-License-Identifier: MIT
"""The Farm Ops mobile HTTP surface — v0.17.1.

These are the tests for a transport that has NO transport gates. `mcp.handle`
runs `security.authorize()` — master switch, shared token, CIDR allowlist —
before it looks a tool up; a `@frappe.whitelist()` method reached directly runs
none of it. So every check that used to be somebody else's job is now this
package's, and every one of them is asserted here BY ITS ABSENCE FIRST: the test
that a gate works is a test that the call fails without it.

SEVEN CLAIMS.

1. **THE SURFACE IS ELEVEN METHODS AND CANNOT BE MORE.** `TheSurfaceIsClosed`.
   There is no dispatcher, so `create_journal_entry` and `convey_parcel` are not
   reachable at any path — asserted by enumerating what the modules actually
   export rather than by trusting the docstring.

2. **A LOGIN IS NOT ENOUGH.** `TheGatesRefuseWhatTheyShould`. Guest, a user with
   no field role, and an enrolled account whose grant was revoked are all
   refused with the SAME message, so a caller cannot learn which gate it failed.

3. **THE ADMIN IS NOT EXEMPT — THE ADMIN IS THE POINT.**
   `AnAdminIsNotEnrolled`. Administrator holds every role on the site, so the
   role gate alone would let the operator's own account drive the field API.
   The grant check is what stops it, and Tim's own account cannot call these
   until he grants himself mobile access on purpose.

4. **ENTITY SCOPING IS ENFORCED HERE, NOT INHERITED.** `TheScopingIsThisApps`.
   The tools read through `frappe.db.get_all`, which does not consult User
   Permissions, so a wrapper that trusted the framework would return the
   holding company's task board to an operating company's picker.

5. **THE SWITCHES AND LIMITS ARE REAL.** `TheKillSwitchStops` and
   `TheRateLimitTrips` drive them until they refuse, and check the refusals are
   503 and 429 rather than a 401 that would sign forty phones out.

6. **EVERY CALL LEAVES A ROW AND NO CALL LEAVES A SECRET.**
   `EveryCallIsAudited`, `NoSecretReachesThePhone`.

7. **THE PAYLOAD IS WHAT THE APP DECODES.** `TheAppCanDecodeThis` re-implements
   `LoginQRParser` and `FarmTask`'s decoder in Python and runs the server's real
   output through them. v0.17.0 shipped a QR that failed at `type` and eleven
   endpoints that 404'd; a test that only checked the server against itself is
   exactly what did not catch either.
"""

import base64
import hashlib
import inspect
import json
from typing import ClassVar
from unittest import mock

import frappe

from erpnext_mcp import audit, compat, roles, settings
from erpnext_mcp.api import files as files_api
from erpnext_mcp.api import guard, shape
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.farmops_api import routes as farmops_routes
from erpnext_mcp.tools import mobile as mobile_tools
from erpnext_mcp.tools import shifts as shift_tools
from erpnext_mcp.tools import wizards as wizard_tools

from .fixtures import (
	MAIN,
	OTHER,
	OTHER_STORES,
	SHOP,
	SPRAY,
	STORES,
	V12TestCase,
	install_hrms,
	seed_masters,
	seed_stock,
)
from .harness import ROLES, STORE, set_roles
from .test_dispatch import WALK

WORKER = "ana@example.test"
WORKER_EMPLOYEE = "EMP-ANA"
OUTSIDER = "ben@example.test"
OUTSIDER_EMPLOYEE = "EMP-BEN"

ON = {
	f"allow_{name}": 1
	for name in (
		"create_mobile_user",
		"revoke_mobile_user",
		"generate_mobile_login_qr",
		"get_current_user_context",
		"create_parcel",
		"create_housing_unit",
		"create_farm_task",
		"assign_farm_task",
		"claim_farm_task",
		"refresh_compliance_alerts",
		"get_compliance_calendar",
	)
}


class MobileAPITestCase(V12TestCase):
	"""A site with one enrolled worker, and a way to call the API as them."""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, public_url="https://umbrel.tail4a2b.ts.net", **ON)
		# `ROLES` is a module-level dict the whole suite shares, and these tests
		# rewrite Administrator's own role set to prove the grant gate holds
		# against an account that holds everything. Restoring it is not tidiness:
		# leaving it rewritten silently broke twelve workflow tests two files
		# away, which is the worst kind of failure to debug.
		self._roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(self._restore_roles)
		guard._BUCKETS.clear()
		roles.install_roles()
		STORE.seed(
			"Employee",
			[
				{
					"name": WORKER_EMPLOYEE,
					"employee_name": "Ana Ramos",
					"user_id": WORKER,
					"company": MAIN,
					"status": "Active",
				},
				{
					"name": OUTSIDER_EMPLOYEE,
					"employee_name": "Ben Ortiz",
					"user_id": OUTSIDER,
					"company": OTHER,
					"status": "Active",
				},
			],
		)
		self.enrol()

	def _restore_roles(self):
		ROLES.clear()
		ROLES.update(self._roles_before)

	# ── enrolment and impersonation ─────────────────────────────────────────
	def enrol(self, email=WORKER, name="Ana Ramos", role="Field Worker", entities=None):
		return self.tool_data(
			"create_mobile_user",
			{
				"email": email,
				"full_name": name,
				"role": role,
				"entity_access": entities or [MAIN],
			},
		)

	def be(self, user=WORKER, remote_addr="100.64.0.7"):
		"""Become one user, on a request that looks like a phone's.

		Sets the session directly rather than through an api-key header: Frappe
		has already authenticated by the time a whitelisted method runs, and what
		reaches this code is a session, not a header. Driving it any other way
		would be testing the double's own auth reproduction rather than the gates.
		"""
		self.request({}, headers={}, remote_addr=remote_addr)
		frappe.local.session.user = user
		return user

	# ── the site's furniture ────────────────────────────────────────────────
	def a_camp(self, unit_name="MC-Cabin-01"):
		if not STORE.rows("Parcel"):
			self.tool_data(
				"create_parcel", {"owning_entity": MAIN, "parcel_name": "Mill Creek", "acreage": 131.43}
			)
		self.unit = self.tool_data(
			"create_housing_unit",
			{
				"parcel": "Mill Creek",
				"unit_name": unit_name,
				"unit_type": "Cabin",
				"capacity": 4,
				"fsma_worker_facility": True,
			},
		)["name"]
		return self.unit

	def a_task(self, **overrides):
		payload = {
			"task_name": "Habitability walk — MC-Cabin-01",
			"task_type": "Inspection",
			"evidence_required": dict(WALK),
			"skill_required": "camp_maintenance",
			"company": MAIN,
		}
		payload.update(overrides)
		return self.tool_data("create_farm_task", payload)["name"]

	def audit_rows(self, method=None):
		rows = [
			row for row in STORE.rows("MCP Action Log") if str(row.get("tool_name", "")).startswith("mobile:")
		]
		if method:
			rows = [row for row in rows if row["tool_name"] == f"mobile:{method}"]
		return rows


# ── 1. the surface is closed ────────────────────────────────────────────────
class TheSurfaceIsClosed(MobileAPITestCase):
	#: Exactly what `MobileAPI.swift` names, and nothing else.
	MOBILE: ClassVar[set[str]] = {
		"get_current_user_context",
		"list_my_tasks",
		"list_available_tasks",
		"get_task",
		"claim_task",
		"start_task",
		"complete_task_via_mobile",
		"reject_task",
		"report_field_task",
		"list_compliance_alerts",
		"scan_asset",
		"get_asset_detail",
		"log_asset_state_change",
		"get_available_actions",
		"report_asset_issue",
		# v0.46.0 — the Identity step the wizard 404'd on.
		"create_employee",
		"search_employees",
		"reactivate_employee",
		# v0.46.2 — the returning worker's branch, between the search and the rehire.
		"get_employee",
		# v0.45.0 — onboarding, the bucket sync and the crew clock.
		"create_i9_form",
		"submit_i9_section_1",
		"submit_i9_section_2",
		# v0.47.0 — the I-9's document lookup, replacing a hardcoded Swift array,
		# and Section 3, which is the branch a returning worker's expired I-9 takes.
		"list_i9_document_types",
		"reverify_i9",
		# v0.47.1 — the I-9 read the wizard never had, the federal form filled from
		# the record, and the signed sheet photographed back onto it.
		"get_i9_form",
		"generate_i9_pdf",
		"upload_signed_i9",
		# v0.48.0 — the authorized signer roster the Section 2 screen reads
		# before it offers a name, and the three calls that maintain it.
		"list_authorized_signers",
		"add_authorized_signer",
		"update_authorized_signer",
		"remove_authorized_signer",
		"submit_w4",
		# v0.48.0 — the W-4 as a federal form rather than a doctype.
		"generate_w4_pdf",
		"link_badge_to_employee",
		# v0.50.0 — the read between a scan and a name. `add_worker_to_shift`
		# takes an Employee docname and a camera produces a badge string, so
		# until this existed the crew clock could scan a crew and roster none of
		# it, and the capture loop could show a foreman a code but never a name.
		"resolve_badge",
		"generate_employee_badge_qr",
		"set_employee_photo",
		"sync_bucket_entries",
		"start_shift",
		"add_worker_to_shift",
		"end_shift",
		# v0.81.0 — how a handset finds an open shift whose docname it lost. Not
		# in PENDING_IOS_INTEGRATION with the rest of the shift reads: this one
		# has a Swift Codable already (`ShiftAPI.ShiftRegisterPage`) and
		# `test_ios_contract` transcribes it.
		"list_shifts",
		# v0.48.3 — the second half of an onboarding upload. Without it the
		# wizard's six photographs and signatures went to Frappe's own
		# `/api/method/upload_file`, which this app's auth hook does not look at,
		# and every one of them was lost against a 200 and a login page.
		"attach_onboarding_document",
		# v0.57.0 — the compliance tab stops being a noticeboard.
		# `MobileAPI.swift` names both of these already: `dismissComplianceAlert`
		# is the gated close, and `submitFormSignature` is the pad's own call,
		# which is `collect_signature`'s write under the argument names
		# `API_CONTRACT.md` §14.2 posts. The app has never named
		# `collect_signature`, which is why that one stays below.
		"dismiss_compliance_alert",
		"submit_form_signature",
		# v0.62.0 — the seven `MobileAPI.swift` names and this surface answered
		# with a 404. Three are the methods below under the name and the argument
		# spellings the app actually posts; four had no method here at all. Every
		# one of the seven is named by a constant in `MobileAPI.swift` today,
		# which is what puts them in THIS set rather than in the pending one.
		"list_org_reference_data",
		"list_housing_units",
		"create_housing_assignment",
		"set_employee_org_fields",
		"set_employee_contact_fields",
		"list_attachments",
		"get_attachment_content",
		# v0.105.0 — `SERVER_CHANGES.md` item 24. HERE RATHER THAN IN
		# `PENDING_IOS_INTEGRATION` because `MobileAPI.swift` already names it:
		# `submitAppFeedback = path("submit_app_feedback")`, and `FeedbackAPI`
		# has been posting to it and parking every note on a 404 since the
		# bubble shipped. This is the release that answers.
		#
		# `user` IS ABSENT FROM ITS SIGNATURE and the app sends one on every
		# call, which is the property to check if this ever grows an argument.
		# The login on a filed note is resolved from the caller, not reported by
		# a handset that several people share.
		"submit_app_feedback",
		# v0.106.0. HERE RATHER THAN IN `PENDING_IOS_INTEGRATION` because
		# `MobileAPI.swift` names it — `materializeTaskForAlert =
		# path("materialize_task_for_alert")` — and `ComplianceAPI.createTaskFromAlert`
		# has been calling it on every raise and falling back on the 404 since
		# the compliance-to-task sheet shipped. This is the release that answers.
		"materialize_task_for_alert",
	}
	FILES: ClassVar[set[str]] = {"stage_file_chunk", "finalize_staged_file"}

	#: v0.52.0. `get_active_model` / `get_model_file_chunk` ship server-side
	#: ahead of the client that will call them — `ModelDownloadService.swift`'s
	#: own header still says "ERPNext's MCP server is only reachable by Claude
	#: tooling, not by the iOS app at runtime", which is exactly the
	#: assumption this route removes, and the Swift-side cutover from querying
	#: Volume Vision directly to calling this route is separate, tracked work.
	#: Listed by name, not folded into MOBILE, so this file does not claim
	#: `MobileAPI.swift` names them until it actually does.
	#:
	#: v0.53.0 adds `get_employee_badge_pass` on the same footing and for the
	#: same reason: the server builds the `.pkpass` and the Google save link, and
	#: the handset side — a share sheet that writes the bytes to a temporary file
	#: with the `com.apple.pkpass` type on it so AirDrop opens it in Wallet — is
	#: separate, tracked work. Listing it here rather than in `MOBILE` keeps this
	#: file from claiming `MobileAPI.swift` names a method it does not.
	#: v0.54.0 adds the hiring wizard's Assignment and Housing steps on the same
	#: footing again. The handset already scans a licence and matches a name; the
	#: four Assignment dropdowns are still a Swift array compiled into the app,
	#: and there is no Housing step at all. These three are the server half, and
	#: `MobileAPI.swift` names none of them yet — so they are listed HERE rather
	#: than in `MOBILE`, and `test_ios_contract` transcribes no mirror for them,
	#: because a mirror of a Codable that does not exist would invent the
	#: contract instead of copying it. They move up to `MOBILE` in the release
	#: that lands the Swift side.
	#: v0.55.0 adds `collect_signature` on the same footing. The server side is
	#: complete — the alert finds the empty box, the task routes it, and this
	#: method files the capture — and the handset side is a signature pad opened
	#: over a task's `subject_docname`, which `MobileAPI.swift` does not name
	#: yet. Listed here rather than in `MOBILE` so this file keeps claiming only
	#: what the Swift actually calls.
	#: v0.63.0 adds `get_document_preview` and `seal_signed_document` on the same
	#: footing, and the first of the two is unusual in that the CONTRACT asked for
	#: it before the client could call it: `API_CONTRACT.md` §17.5 names the
	#: presentation step as a server-side gap and says "the fix is one route".
	#: This is that route, and `MobileAPI.swift` does not name it yet — the
	#: handset side is a viewer on the `.reviewing` step, which is separate,
	#: tracked work. `seal_signed_document` may never need naming at all: the
	#: ordinary flow gets its seal from `submit_form_signature`, and this method
	#: exists for a form signed before v0.63.0 or one signed in the Desk.
	#: v0.65.0 adds `universal_scan` on the same footing. The server side is the
	#: whole of it — one string in, whichever register holds it out — and the
	#: handset side is a scanner screen that stops asking the worker what they
	#: are about to scan, which `MobileAPI.swift` does not name yet. Listed here
	#: rather than in `MOBILE` so this file keeps claiming only what the Swift
	#: actually calls.
	#: v0.67.0 adds the four receipt-capture methods on the same footing. This is
	#: Sprint 2's SERVER side: the two new registers exist, the classifier that
	#: decides which one a photograph belongs in exists, and the capture screen
	#: that would call them is the iOS half of the same sprint. Listed here
	#: rather than in `MOBILE` so this file keeps claiming only what
	#: `MobileAPI.swift` actually names — and note that `create_expense_receipt`
	#: is among them even though the app has photographed receipts since v0.31.0:
	#: it reached `submit_expense_receipt` through the MCP surface, and this is
	#: the first time the flow has a route of its own.
	#: Sprint 3 (v0.68.0) adds the six compliance-alert-rectification methods on
	#: the same footing. The server side is the whole of it — every alert now
	#: names a `rectification` object and there is a route behind every one of
	#: them — and the handset side is the button `ComplianceAlertDetailView.swift`
	#: does not draw yet, which is separate, tracked work per the sprint's own
	#: scope: server-side sidecar first, the phone reads it and draws a button
	#: after. Listed here rather than in `MOBILE` so this file keeps claiming
	#: only what `MobileAPI.swift` actually names.
	#: v0.98.0 adds `create_farm_location` on the same footing, and it is the one
	#: Wave 2 method the Swift does not name. `LocationRegistryAPI.route(for:)`
	#: returns the four per-register names and the app builds those requests; the
	#: polymorphic door is the one the implementation plan asked for and is
	#: published so a client that would rather send `doctype` than choose a path
	#: has one. Listed here rather than in `MOBILE` so this file keeps claiming
	#: only what the compiled Swift actually names.
	PENDING_IOS_INTEGRATION: ClassVar[set[str]] = {
		# v0.106.0. The certificate register's two reads, HERE FOR THIS SET'S
		# ORDINARY REASON — `MobileAPI.swift` names neither yet. The server side
		# is published so the iOS half is a client change rather than a release
		# of both: "who holds a current applicator licence" is the question the
		# compliance-to-task sheet's picker actually wants answered, and the
		# training matrix it uses instead answers a different one (somebody sat
		# through the course, which is not the same as the state issuing them a
		# licence). They move up when the constants land.
		"list_certifications",
		"get_certification",
		"create_farm_location",
		# v0.110.0. The three boundary writes. HERE RATHER THAN IN `MOBILE`
		# because `MobileAPI.swift` names none of them yet: the server half is
		# published first so the iOS half is a client change rather than a
		# release of both, which is the same order item 11's location routes
		# landed in. A walked boundary is a ring of GPS fixes the handset already
		# knows how to collect; what it had nowhere to send them.
		"set_field_boundary",
		"set_zone_boundary",
		"set_parcel_boundary",
		# v0.116.0. The operational map, HERE FOR THIS SET'S ORDINARY REASON —
		# `MobileAPI.swift` does not name it yet, so the server half is published
		# first and the iOS map mirror is a client change rather than a release
		# of both. That is the same order the boundary writes above landed in,
		# and it is deliberate for this route in particular: the layer the phone
		# most needs is the restricted-entry countdown, and a server that can
		# answer it before the screen exists is a server the screen can be
		# written against.
		"get_map_overlays",
		# v0.113.0. THE OTHER HALF OF ITEM 11, AND THE ORG CHART. Seventeen
		# methods, HERE FOR THIS SET'S ORDINARY REASON — `MobileAPI.swift` names
		# none of them, so the server half is published first and the iOS half is a
		# client change rather than a release of both. `test_ios_contract`
		# transcribes no mirror for a Codable that does not exist.
		#
		# `update_farm_location` IS THE CREATE SHEET REOPENED and takes only
		# arguments `CreateLocationSheet` already collects. `delete_farm_location`
		# is THE ONLY IRREVERSIBLE METHOD ON THIS SURFACE and the property to check
		# if it ever appears in an app build is its ABSENT arguments: the four
		# `force_check_…` flags are on the tool and not on the wrapper, so `bind`
		# drops them and no body can turn a safety check off. Both carry
		# `guard.require_location_role`.
		#
		# The fifteen org methods are the five masters `create_employee` refuses an
		# unknown value against. THE READS ARE OPEN ON ENROLMENT and the writes
		# carry `personnel.require_hr_role`, which is
		# `list_onboarding_reference_data`'s split. The Employee Grade pay columns
		# are absent from every signature here, so `default_base_pay` is
		# unreachable rather than merely refused.
		"update_farm_location",
		"delete_farm_location",
		"list_designations",
		"create_designation",
		"update_designation",
		"list_departments",
		"create_department",
		"update_department",
		"list_branches",
		"create_branch",
		"update_branch",
		"list_employment_types",
		"create_employment_type",
		"update_employment_type",
		"list_employee_grades",
		"create_employee_grade",
		"update_employee_grade",
		"universal_scan",
		"classify_receipt",
		"create_expense_receipt",
		"create_scale_ticket",
		"list_scale_tickets",
		"collect_signature",
		"get_document_preview",
		"seal_signed_document",
		"get_active_model",
		"get_model_file_chunk",
		"get_employee_badge_pass",
		"list_onboarding_reference_data",
		"list_available_housing",
		"assign_housing",
		"log_shift_break",
		"end_shift_break",
		"get_break_policy",
		"clock_out_worker",
		"get_shift_production",
		"get_shift",
		"renew_certification",
		"record_training",
		"sign_training_supervisor_review",
		"update_regulatory_filing",
		"advance_policy_review",
		"rectify_alert",
		# Sprint 4 (v0.69.0). Document intelligence: the two halves a phone at a
		# chemical shed needs — check what was read off a label, and read one
		# stored answer back. The other three tools in the set stay off this
		# surface (see `test_farmops_api.py`).
		"validate_document",
		"get_document_validation",
		# Sprint 7 (v0.72.0). The foreman's crew-task dashboard: the board for
		# the crew on a foreman's own open shift, the dispatch that moves a job
		# between people, the task raised on the spot, and the two ends of the
		# template register. The server side is the whole of it — the handset's
		# dashboard is the iOS half of the same sprint — and these five are the
		# first methods on this surface a Field Worker cannot call at all: each
		# carries `guard.require_dispatch_role` in its own body.
		"list_dispatched_tasks",
		"assign_farm_task",
		"create_farm_task",
		"list_farm_task_templates",
		"create_task_from_template",
		# v0.67.0's receipt reads. Three pickers and the detail-view write that
		# `create_expense_receipt`'s screen needs — a cost center to code the
		# receipt to, a supplier to link it against, the receipts already filed,
		# and the recode of one that was coded wrong. They belong here rather
		# than in `MOBILE` for the reason the four capture methods above them do:
		# this is Sprint 2's SERVER side and `MobileAPI.swift` does not name them
		# yet. (Listed from v0.78.0, which is when the omission was found: they
		# had routes and were on neither set, so this file's two assertions were
		# both red on `main`.)
		"list_cost_centers",
		"list_suppliers",
		"list_expense_receipts",
		"update_expense_receipt",
		# Sprint 8 (v0.78.0). Field asset registration: register the machine,
		# get its printable tag back, file the photograph against it. The Swift
		# screens for this ARE built — this is the rare set where the client is
		# ahead of the server — but `MobileAPI.swift` reaches them through a
		# generic request builder rather than through a named constant, so they
		# are listed here rather than in `MOBILE`, which claims only what the app
		# names. They move up when the constants land.
		"register_asset",
		"generate_asset_qr",
		"attach_file_to_document",
		# v0.80.0. Trade documentation: four reads a driver or a desk on a tablet
		# wants — what is going out, one load in full, what is still missing, and
		# the paperwork on it — plus the one write, which is a driver confirming a
		# load left and arrived. The server side is the whole of it and
		# `MobileAPI.swift` names none of them yet.
		#
		# `confirm_shipment_movement` is the interesting name on this list. It
		# delegates to `update_shipment_status`, and it is NOT that tool: the tool
		# can release a shipment to Ready to Ship — the module's one gate — can
		# cancel one, and takes an `override_reason` that walks past an incomplete
		# document checklist. None of the three is in this wrapper's signature, so
		# none can be bound from a body. A release is an assertion that the
		# paperwork is in order, made at a desk by somebody with a trade role; an
		# account that could make it from a yard would make the gate worth
		# nothing. Same argument that keeps `cancel=true` off `reject_farm_task`.
		"list_shipments",
		"get_shipment",
		"get_shipment_readiness",
		"list_trade_documents",
		"confirm_shipment_movement",
		# Sprint 9 (v0.79.0). Nineteen methods for the four things a day
		# actually contains: being interrupted, finding that somebody else
		# already raised your job, an investigation that runs for a week, and a
		# discipline chain. The server side is the whole of it; the iOS side is
		# the same sprint's other half — the pause banner, the duplicate sheet,
		# the investigation screen, and a wizard renderer that draws whatever
		# `get_wizard_definition` returns. Listed here rather than in `MOBILE`
		# so this file keeps claiming only what `MobileAPI.swift` names.
		"pause_task_via_mobile",
		"resume_task_via_mobile",
		"link_tasks_via_mobile",
		"merge_task_via_mobile",
		"add_task_note_via_mobile",
		"attach_audio_note",
		"list_task_notes",
		"create_discipline_record",
		"acknowledge_discipline_record",
		"get_discipline_record",
		"list_discipline_history",
		"get_discipline_report",
		"create_accident_report",
		"get_accident_report",
		"update_accident_investigation",
		"close_accident_investigation",
		"list_accident_reports",
		"get_wizard_definition",
		"list_wizard_definitions",
		# The three shift tools that had no route. `log_shift_event` is the
		# compliance timeline — the record OAR 437-004-1131 actually asks for, and
		# the one thing on the shift surface that is worth nothing written in the
		# evening from a Desk. `log_shift_location`/`get_shift_track` are the crew
		# track, and `get_shift_crew_timeline` is the per-worker envelope the close
		# screen wants: what happened to ANA, not to the shift.
		#
		# HERE RATHER THAN IN `MOBILE` BECAUSE `MobileAPI.swift` NAMES NONE OF THEM
		# YET, which is this set's whole rule. The server side is published so the
		# iOS half is a client change and not a release of both; they move up when
		# the constants land.
		"log_shift_event",
		"log_shift_location",
		"get_shift_track",
		"get_shift_crew_timeline",
		# The QR valve workflow's one route. Scan-to-action: it resolves a valve
		# tag, records the scan, and — only when the body asks for it — opens or
		# shuts the gate in the same POST, picking the action from the state the
		# phone cannot know. `toggle` defaults to false, which is the whole safety
		# of it: a camera that fired on recognition would water a block because
		# somebody walked past with a phone.
		#
		# HERE RATHER THAN IN `MOBILE` because `MobileAPI.swift` does not name it
		# yet, which is this set's rule. It moves up when the constant lands.
		"scan_valve",
		# v0.85.0. The string bundle a handset pulls once at login rather than
		# asking for one label at a time. HERE FOR THIS SET'S ORDINARY REASON —
		# `MobileAPI.swift` does not name it yet — and it is worth saying what
		# that costs, because it is not nothing: until the constant lands, a
		# Spanish-reading picker still sees whatever strings are compiled into
		# the app. The server half is published first so the iOS half is a client
		# change and not a release of both.
		"get_translation_bundle",
		# v0.91.0. The shadow log feed: two reads and the acknowledgement. HERE
		# RATHER THAN IN `MOBILE` for this set's ordinary reason — `MobileAPI.swift`
		# names none of the three yet — and the server half is published first so
		# the iOS half is a client change rather than a release of both.
		#
		# THE RECIPIENT IS NEVER A BODY ARGUMENT on any of the three, which is
		# what makes them the caller's own feed rather than a register. The
		# signature assertions in `TheShadowFeedIsAddressed` are what hold that.
		"list_shadow_log_entries",
		"get_shadow_log_entry",
		"acknowledge_shadow_log",
		# v0.91.0. The inventory tab's five, and the wizard submit target that
		# completes the set of five the seeded wizards name.
		#
		# HERE FOR A SHARPER REASON THAN THE REST OF THIS SET, AND IT IS WORTH
		# STATING. Everything above is "the Swift does not name it yet".
		# `MobileAPI.swift` DOES name these five — at
		# `farmops/api/stock-balance` and its four neighbours, the hyphenated
		# top-level GETs `sprint-4-api-contracts.md` § Workstream 1 specifies.
		# This route table cannot publish that shape (see `routes.py`), so the
		# app is moving to the namespace instead, which is a client change to
		# `MobileAPI.swift` and `InventoryAPI.swift`. Until it lands the app is
		# calling paths that do not exist and these methods are unreached, which
		# is what this set means. They move up with that change.
		#
		# `start_inspection` is here for a different reason again: nothing in
		# `MobileAPI.swift` will ever name it. It is reached through the
		# `submit_endpoint` its wizard hands the renderer, which is the whole
		# point of that field — see `_with_submit_endpoint`.
		"get_stock_balance",
		"get_warehouse_summary",
		"get_stock_ledger",
		"list_reorder_alerts",
		"create_stock_entry",
		"start_inspection",
		# `submit_wizard_via_mobile` is here for `start_inspection`'s reason and
		# then some: `MobileAPI.swift` will never name it either, because
		# `WizardAPI.submit` posts to the `submit_endpoint` the SPEC handed it.
		# The app already sends this method's exact envelope — `{"wizard",
		# "answers"}` — and had it dropped at the door by `routes.bind`; what
		# changed in v0.91.0 is that a method now declares those two names.
		"submit_wizard_via_mobile",
		# v0.91.0. The two payroll outputs. `MobileAPI.swift` does not name
		# either yet — there is no payroll screen in the app — and they are here
		# rather than in `PENDING_IOS_INTEGRATION` because they are published on
		# purpose for the Desk-less operator: an office manager with a phone and
		# an HR role is the person who runs a register and hands out stubs, and
		# on this farm that person is not sitting at a Desk on payday.
		#
		# BOTH ARE HR-ONLY IN THEIR OWN BODIES, which is the thing to check if
		# either ever appears in an app build — see `ThePayrollRoutesAreHROnly`.
		"get_payroll_register",
		"render_pay_stub",
		# The three compliance reports: the training matrix, the OSHA 300 log
		# with its 300A summary, and the spray application report. HERE FOR THIS
		# SET'S ORDINARY REASON — `MobileAPI.swift` names none of the four yet,
		# there being no compliance-report screen in the app — and the server
		# half is published first so the iOS half is a client change rather than
		# a release of both.
		#
		# THEY ARE NOT IN `MOBILE` AND THAT IS DELIBERATE rather than pending
		# paperwork: `test_ios_contract` requires a mirror of the Swift Codable
		# for everything in that set, and there is no Codable to mirror. Writing
		# one here would invent the contract rather than transcribe it, and an
		# invented contract is worse than an absent one — the app would be built
		# against a shape nothing on either side agreed to.
		"get_training_compliance_report",
		"get_osha_300_log",
		"get_osha_300a_summary",
		"get_spray_application_report",
		# The curriculum and the group training session — eight methods, and the
		# same reason as every entry above: `MobileAPI.swift` names none of them
		# yet, there being no training screen in the app, and the server half is
		# published first so the iOS half is a client change rather than a
		# release of both. They are not in `MOBILE` because `test_ios_contract`
		# requires a mirror of the Swift Codable for everything in that set, and
		# there is no Codable to mirror; writing one here would invent the
		# contract rather than transcribe it.
		"get_training_curriculum",
		"update_training_type",
		"create_training_session",
		"add_session_attendee",
		"sign_session_attendance",
		"complete_training_session",
		"get_training_session",
		"list_training_sessions",
		"render_training_sign_in_sheet",
		# The payroll deduction register: three reads and two writes.
		# `MobileAPI.swift` names none of the five — there is no deductions
		# screen in the app — so the server half is published first and the iOS
		# half is a client change rather than a release of both.
		#
		# THEY ARE HERE RATHER THAN IN `MOBILE` FOR THIS SET'S ORDINARY REASON
		# and not as paperwork: `test_ios_contract` requires a mirror of the
		# Swift Codable for everything in that set, and there is no Codable to
		# mirror. Writing one would invent the contract rather than transcribe
		# it, and an invented contract is worse than an absent one — the app
		# would be built against a shape nothing on either side agreed to.
		#
		# ALL FIVE ARE HR-ONLY IN THEIR OWN BODIES, which is the thing to check
		# if any appears in an app build. What a person's wages are garnished
		# for is among the most sensitive facts this app holds.
		#
		# THAT SENTENCE WAS FALSE UNTIL v0.94.0 AND IS LEFT STANDING ABOVE ON
		# PURPOSE, because what it describes is now true and how it became true is
		# worth keeping. Only the two WRITES carried the gate; the three reads were
		# scope-only — so this comment, `farmops_api/routes.py:751` and the module's
		# own prose all asserted a protection none of them had. The lesson is the
		# one the F1 invariant test encodes: a sentence claiming a gate is not a
		# gate, and the three that said this were written by people reading each
		# other rather than reading the code.
		"list_payroll_deductions",
		"get_payroll_deduction",
		"list_employee_deductions",
		"create_payroll_deduction",
		"update_payroll_deduction",
		# v0.99.0. Where a break horn is delivered. `MobileAPI.swift` names
		# neither yet — `SERVER_CHANGES.md` item 16 asks for `register_push_token`
		# and the iOS half is the client change that follows this one — so they
		# sit here rather than in `MOBILE`, and `test_ios_contract` transcribes
		# no mirror for a Codable that does not exist.
		#
		# NEITHER TAKES A SUBJECT FROM THE BODY, which is the property to check
		# if either ever appears in an app build. A phone enrols ITSELF: `user`
		# and `employee` are resolved from the caller's own login, so `bind` has
		# nothing to drop and no body can point a registration at somebody else
		# and have their break horns, heat alerts and dispatch pings delivered to
		# a handset of its choosing. The same shape the three direct-deposit
		# methods have, for the same reason.
		"register_push_token",
		"unregister_push_token",
		# v0.91.0. Direct deposit, entered by the worker whose wages it is.
		# `MobileAPI.swift` names none of the three yet — the handset half is
		# separate work — so they sit here rather than in `MOBILE`.
		#
		# THESE ARE THE ONLY THREE METHODS ON THIS SURFACE THAT TAKE NO SUBJECT
		# FROM THE BODY. Every other write that names a person accepts an
		# `employee` docname and checks it against the caller's COMPANY scope,
		# which is right for onboarding and wrong for a payment instruction:
		# company scope is shared by everybody enrolled, so it would let one
		# picker repoint another's wages. These resolve the subject from the
		# caller's own login instead, and `update_my_bank_account` proves the
		# docname belongs to that employee before it writes.
		"list_my_bank_accounts",
		"add_my_bank_account",
		"update_my_bank_account",
		# Employee self-service: the four records a worker may read about
		# themselves. HERE FOR THIS SET'S ORDINARY REASON — `MobileAPI.swift`
		# names none of the five yet, there being no self-service tab in the app
		# — and the server half is published first so the iOS half is a client
		# change rather than a release of both.
		#
		# THEY JOIN THE DIRECT DEPOSIT THREE ABOVE AS THE ONLY METHODS ON THIS
		# SURFACE THAT TAKE NO SUBJECT FROM THE BODY, which is the property
		# `TheSelfServiceReadsAreAddressed` holds: an `employee` argument checked
		# against company scope would be checked against a scope every enrolled
		# worker at that company shares, so the subject comes from the login.
		#
		# THEY ARE ALSO THE ONLY ROUTES REACHING WAGES WITHOUT AN HR ROLE. The
		# distinction is one person versus a crew, and it is asserted rather than
		# asserted about: `ThePayrollRoutesAreHROnly` still holds for the two
		# HR-gated payroll routes, which are unchanged.
		"get_my_w4",
		"list_my_pay_stubs",
		"get_my_pay_stub_pdf",
		"list_my_trainings",
		"get_my_i9",
		# The payroll deduction register: three reads and two writes.
		# `MobileAPI.swift` names none of the five — there is no deductions
		# screen in the app — so the server half is published first and the iOS
		# half is a client change rather than a release of both.
		#
		# THEY ARE HERE RATHER THAN IN `MOBILE` FOR THIS SET'S ORDINARY REASON
		# and not as paperwork: `test_ios_contract` requires a mirror of the
		# Swift Codable for everything in that set, and there is no Codable to
		# mirror. Writing one would invent the contract rather than transcribe
		# it, and an invented contract is worse than an absent one — the app
		# would be built against a shape nothing on either side agreed to.
		#
		# ALL FIVE ARE HR-ONLY IN THEIR OWN BODIES, which is the thing to check
		# if any appears in an app build. What a person's wages are garnished
		# for is among the most sensitive facts this app holds.
		# v0.92.0. The five tax remittance reads: what is owed to the IRS, Oregon
		# and Washington, when each deposit is due, and the two annual returns.
		# Every one carries `personnel.require_hr_role` in its own body, on the
		# same footing as `get_payroll_register` — these are the whole crew's
		# wages rolled into what the farm remits, and no version of that is a
		# picker's to read.
		#
		# HERE RATHER THAN IN `MOBILE` because `MobileAPI.swift` names none of
		# them yet, which is this set's whole rule. The server side is published
		# so the iOS half is a client change rather than a release of both.
		"get_tax_remittance_summary",
		"get_941_prefill",
		"get_state_tax_remittance",
		"get_tax_deposit_schedule",
		"get_futa_summary",
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
		"seal_bin",
		# v0.98.0 — Wave 2 of `fafo_ios/SERVER_CHANGES.md`. Every one of these
		# eight is NAMED IN THE COMPILED SWIFT, which is what earns a place in
		# this set rather than in `PENDING_IOS_INTEGRATION` below.
		#
		# `add_task_note` is `TaskNotesAPI`'s call and was seven 404s: the write
		# has been mounted since v0.79.0 under `add_task_note_via_mobile`,
		# because `Route` builds the path off the wrapper's own name.
		#
		# `create_dispute` is item 12's other half. The app has been filing a
		# worker's grievance as a discipline record with `DISPUTE RAISED BY …`
		# in the description, which puts a complaint on the complainant's own
		# progressive-discipline chain.
		#
		# `get_break_schedule` is what `BreakSchedule` computes locally today,
		# from the state statutory minimum whenever `get_break_policy` did not
		# answer — honest, and not synchronised across a crew.
		#
		# The five location methods are item 11. `LocationRegistryAPI.route(for:)`
		# NAMES the four creates in its own refusal text, and the type's doc
		# comment lists `list_farm_locations` among the ten paths it probed.
		"add_task_note",
		"create_dispute",
		"get_break_schedule",
		"list_farm_locations",
		"create_field",
		"create_irrigation_zone",
		"create_parcel",
		"create_housing_unit",
	}

	def _whitelisted(self, module):
		return {
			name
			for name in dir(module)
			if not name.startswith("_") and getattr(getattr(module, name), "farm_ops_method", None)
		}

	def test_the_mobile_module_publishes_exactly_the_ten_the_app_calls(self):
		self.assertEqual(self._whitelisted(mobile_api) - self.PENDING_IOS_INTEGRATION, self.MOBILE)

	def test_the_files_module_publishes_exactly_the_two_the_app_calls(self):
		self.assertEqual(self._whitelisted(files_api), self.FILES)

	def test_no_admin_tool_is_reachable_at_any_path_in_this_package(self):
		"""The one that matters. A generic dispatcher would have published these."""
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
			self.assertFalse(hasattr(mobile_api, dangerous), dangerous)
			self.assertFalse(hasattr(files_api, dangerous), dangerous)

	def test_there_is_no_generic_dispatcher_taking_a_tool_name(self):
		for module in (mobile_api, files_api):
			for suspect in ("call", "invoke", "dispatch_tool", "handle", "run"):
				attribute = getattr(module, suspect, None)
				self.assertFalse(
					callable(attribute) and getattr(attribute, "farm_ops_method", None),
					f"{module.__name__}.{suspect} is published",
				)


# ── 2. the gates ────────────────────────────────────────────────────────────
class TheGatesRefuseWhatTheyShould(MobileAPITestCase):
	REFUSAL = "enrolled Farm Ops credential"

	def test_guest_is_refused_before_anything_is_read(self):
		self.be("Guest")
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.get_current_user_context()
		self.assertIn(self.REFUSAL, str(caught.exception))

	def test_a_login_with_no_field_role_is_refused(self):
		"""A Family Member and an Advisor are real logins on this site."""
		STORE.seed("User", [{"name": "aunt@example.test", "enabled": 1, "full_name": "Aunt"}])
		set_roles("aunt@example.test", ["Family Member", "Advisor"])
		self.be("aunt@example.test")
		with self.assertRaises(frappe.PermissionError):
			mobile_api.list_my_tasks()

	def test_a_field_role_with_no_grant_is_refused(self):
		"""Holding the role is not being enrolled. The grant is the enrolment."""
		STORE.seed("User", [{"name": "casual@example.test", "enabled": 1, "full_name": "Casual"}])
		set_roles("casual@example.test", ["Field Worker"])
		self.be("casual@example.test")
		with self.assertRaises(frappe.PermissionError):
			mobile_api.list_my_tasks()

	def test_a_revoked_grant_closes_the_door_on_the_very_next_call(self):
		self.be()
		self.assertTrue(mobile_api.get_current_user_context()["user"])

		frappe.local.session.user = "Administrator"
		self.tool_data("revoke_mobile_user", {"email": WORKER, "reason": "left at the end of harvest"})
		self.assertEqual(frappe.db.get_value("Mobile Access Grant", WORKER, "state"), "Revoked")

		self.be()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.get_current_user_context()

	def test_every_refusal_reads_the_same_so_nothing_is_learned_from_probing(self):
		"""Telling a caller which gate it failed hands it a free oracle."""
		messages = set()
		for user, roles_held in (
			("Guest", []),
			("noroles@example.test", []),
			("norole2@example.test", ["Family Member"]),
			("nogrant@example.test", ["Foreman"]),
		):
			if user != "Guest":
				STORE.seed("User", [{"name": user, "enabled": 1, "full_name": user}])
				set_roles(user, roles_held)
			self.be(user)
			with self.assertRaises(frappe.PermissionError) as caught:
				mobile_api.list_my_tasks()
			messages.add(str(caught.exception))
		self.assertEqual(len(messages), 1, messages)


class ThePersonnelRegisterIsNotAPickersToRead(MobileAPITestCase):
	"""v0.46.0. `search_employees` is the only READ on this surface with a role
	gate of its own, and the reason is worth a test rather than a comment: every
	other read is field work a picker is entitled to, and this one is the entity's
	whole personnel register — names, hire dates, employment types, and the people
	who have left. The writing methods inherit the same gate from
	`tools/employee.py`; this one applies it by hand, so it is the one that can
	quietly stop applying."""

	def test_a_field_worker_with_a_perfectly_good_grant_is_still_refused(self):
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.search_employees(query="Ramos")
		self.assertIn("personnel register", str(caught.exception))

	def test_and_a_farm_manager_is_not(self):
		"""The role an operator actually enrols an onboarding phone as."""
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be()
		self.assertIn("employees", mobile_api.search_employees(query="Ramos"))

	def test_a_field_worker_cannot_create_or_reactivate_one_either(self):
		"""v0.94.0 moved both of these from `HR_ROLES` to `HIRING_ROLES`, so the
		refusal now names the hire rather than the register — but a PICKER is on
		neither list and is refused by both. That is the whole shape of this
		release: the gate moved, it did not dissolve."""
		self.be()
		for call in (
			lambda: mobile_api.create_employee(first_name="Elena", last_name="Marquez", company=MAIN),
			lambda: mobile_api.reactivate_employee(employee="EMP-ANA"),
		):
			with self.assertRaises(Exception) as caught:
				call()
			self.assertIn("may not bring a person onto the farm", str(caught.exception))

	def test_but_a_foreman_can_do_both(self):
		"""THE POSITIVE HALF, and the one a release of widenings actually turns
		on. `create_employee` and the rehire are steps 1 and 1b of a hire, and a
		foreman refused at step 1 never reaches the other ten."""
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.be()
		self.assertTrue(mobile_api.create_employee(first_name="Elena", last_name="Marquez", company=MAIN))
		self.assertTrue(mobile_api.reactivate_employee(employee=WORKER_EMPLOYEE))

	def test_and_the_register_read_did_not_widen_with_them(self):
		"""The boundary, asserted at the one place it is easiest to lose. A
		Foreman may now hire and may STILL not browse the entity's personnel
		register — names, hire dates and the people who have left are somebody
		else's PII whoever is holding the phone."""
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.search_employees(query="Ramos")
		self.assertIn("personnel register", str(caught.exception))

	def test_a_field_worker_cannot_read_somebody_elses_record(self):
		"""v0.46.2. `get_employee` is the one read here whose gate has a hole in
		it, so this is the half of the hole that must stay shut."""
		STORE.seed(
			"Employee",
			[
				{
					"name": "EMP-COLLEAGUE",
					"employee_name": "Rosa Aguilar",
					"company": MAIN,
					"status": "Active",
				}
			],
		)
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.get_employee(employee="EMP-COLLEAGUE")
		self.assertIn("personnel register", str(caught.exception))

	def test_and_can_read_their_own_without_the_hr_role(self):
		"""The other half, and the reason the exception is there: a picker checking
		what their own onboarding still needs is not browsing the register."""
		self.be()
		row = mobile_api.get_employee(employee=WORKER_EMPLOYEE)
		self.assertEqual(row["name"], WORKER_EMPLOYEE)
		self.assertEqual(row["employee_name"], "Ana Ramos")

	def test_the_self_exception_cannot_be_claimed_by_naming_somebody(self):
		"""The caller's own record is resolved through `Employee.user_id` and never
		from the body, so there is nothing in a request that can assert it.

		Ben is enrolled into MAIN here, so entity scoping is NOT what refuses him —
		he can reach Ana's company perfectly well. The only thing between him and
		her record is the HR role he does not hold."""
		self.enrol(email=OUTSIDER, name="Ben Ortiz", entities=[MAIN])
		self.be(OUTSIDER)
		with self.assertRaises(Exception) as caught:
			mobile_api.get_employee(employee=WORKER_EMPLOYEE)
		self.assertIn("personnel register", str(caught.exception))

	def test_a_farm_manager_reads_anybody_in_their_own_entities(self):
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		STORE.seed(
			"Employee",
			[
				{
					"name": "EMP-COLLEAGUE",
					"employee_name": "Rosa Aguilar",
					"company": MAIN,
					"status": "Active",
				}
			],
		)
		self.be()
		self.assertEqual(mobile_api.get_employee(employee="EMP-COLLEAGUE")["name"], "EMP-COLLEAGUE")

	def test_but_not_outside_them_even_holding_every_hr_role(self):
		"""Entity scoping is not the role gate and does not bend to it."""
		set_roles(WORKER, ["Field Worker", "Farm Manager", "HR Manager"])
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.get_employee(employee=OUTSIDER_EMPLOYEE)
		self.assertIn("not found", str(caught.exception).lower())


class AnAdminIsNotEnrolled(MobileAPITestCase):
	def test_administrator_holds_every_role_and_still_cannot_call_the_field_api(self):
		"""The reason the grant check exists. Tim's own account is not exempt."""
		set_roles("Administrator", ["System Manager", "Farm Manager", "Foreman", "Field Worker"])
		self.be("Administrator")
		with self.assertRaises(frappe.PermissionError):
			mobile_api.list_available_tasks()

	def test_and_can_once_somebody_deliberately_enrols_it(self):
		"""The gate is a decision, not a wall: enrolling on purpose opens it."""
		self.enrol(email="tim@example.test", name="Tim Polehn", role="Farm Manager")
		set_roles("tim@example.test", ["System Manager", "Farm Manager"])
		self.be("tim@example.test")
		self.assertEqual(mobile_api.get_current_user_context()["user"], "tim@example.test")


# ── 3. entity scoping ───────────────────────────────────────────────────────
class TheScopingIsThisApps(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		self.a_camp()
		self.mine = self.a_task()
		self.theirs = self.a_task(task_name="Highland walk", company=OTHER)

	def test_a_company_the_caller_cannot_reach_is_refused_not_quietly_emptied(self):
		self.be()
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.list_my_tasks(company=OTHER)
		self.assertIn(OTHER, str(caught.exception))

	def test_the_pool_never_carries_another_entitys_work(self):
		self.be()
		names = {row["name"] for row in mobile_api.list_available_tasks()["tasks"]}
		self.assertIn(self.mine, names)
		self.assertNotIn(self.theirs, names)

	def test_a_task_in_another_entity_is_not_found_rather_than_forbidden(self):
		"""Both refusals read the same, so docnames cannot be mapped by probing."""
		self.be()
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.get_task(task=self.theirs)

	def test_another_entitys_task_cannot_be_claimed(self):
		self.be()
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.claim_task(task=self.theirs)

	def test_another_entitys_task_cannot_be_completed(self):
		self.be()
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.complete_task_via_mobile(task=self.theirs, clean_pass=True)

	def test_an_account_with_no_entity_access_is_refused_rather_than_shown_everything(self):
		"""Frappe's rule is that no User Permission means UNRESTRICTED. On an
		endpoint reachable from the internet that default is exactly backwards."""
		for row in list(STORE.rows("User Permission")):
			if row.get("user") == WORKER:
				frappe.delete_doc("User Permission", row["name"], force=True, ignore_permissions=True)
		self.be()
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.list_my_tasks()
		self.assertIn("no entity access", str(caught.exception))

	def test_the_scoped_filter_drops_a_row_the_tool_layer_let_through(self):
		"""The belt to the braces, driven directly: a row that escapes the query
		filter through some future code path still must not leave the building."""
		rows = [{"name": "FT-1", "company": MAIN}, {"name": "FT-2", "company": OTHER}, {"name": "FT-3"}]
		kept = {row["name"] for row in guard.scoped(rows, [MAIN])}
		self.assertEqual(kept, {"FT-1", "FT-3"})


# ── 4. input validation ─────────────────────────────────────────────────────
class NothingIsPassedThroughBlind(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		self.a_camp()
		self.task = self.a_task()

	def test_a_docname_that_does_not_exist_is_refused_before_delegation(self):
		self.be()
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.get_task(task="FT-does-not-exist")

	def test_a_missing_docname_is_refused_by_name(self):
		self.be()
		with self.assertRaises(frappe.ValidationError):
			mobile_api.get_task(task="")

	def test_an_assignment_from_another_task_cannot_be_smuggled_in(self):
		"""The one argument that could otherwise move work between records."""
		other = self.a_task(task_name="Second walk")
		self.be()
		mobile_api.claim_task(task=self.task)
		mine = frappe.db.get_value("Farm Task Assignment", {"task": self.task}, "name")
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.start_task(task=other, task_assignment=mine)
		self.assertIn("does not belong to", str(caught.exception))

	def test_a_rejection_always_hands_the_task_back_and_never_cancels_it(self):
		"""`reject_farm_task` takes cancel=true, which would delete the work."""
		self.be()
		mobile_api.claim_task(task=self.task)
		mobile_api.reject_task(task=self.task, reason="the ladder is broken")
		self.assertEqual(frappe.db.get_value("Farm Task", self.task, "state"), "Available")

	def test_the_cancel_argument_is_not_even_in_the_wrappers_signature(self):
		"""Frappe drops a body key a whitelisted method does not declare, so an
		argument that is absent from the signature is one no phone can send."""
		import inspect

		accepted = set(inspect.signature(mobile_api.reject_task).parameters)
		self.assertEqual(accepted, {"user", "task", "task_assignment", "reason"})
		for wrapper, forbidden in (
			(mobile_api.complete_task_via_mobile, ("record_data", "worker_id", "signature_file")),
			(mobile_api.list_my_tasks, ("worker_id", "user_id")),
			(files_api.finalize_staged_file, ("attach_to_doctype", "attach_to_name", "is_private")),
			(files_api.stage_file_chunk, ("attach_to_doctype", "governance_document")),
		):
			accepted = set(inspect.signature(wrapper).parameters)
			for name in forbidden:
				self.assertNotIn(name, accepted, f"{wrapper.__name__} accepts {name}")

	def test_a_rejection_with_no_reason_is_refused(self):
		self.be()
		mobile_api.claim_task(task=self.task)
		with self.assertRaises(frappe.ValidationError):
			mobile_api.reject_task(task=self.task, reason="   ")

	def test_an_evidence_file_type_that_is_not_evidence_is_refused(self):
		self.be()
		for name in ("payload.html", "logo.svg", "run.sh", "note.php"):
			with self.assertRaises(frappe.ValidationError, msg=name):
				files_api.stage_file_chunk(
					upload_id="u1", file_name=name, chunk_index=0, chunk_count=1, total_bytes=3, data="YWJj"
				)

	def test_a_filename_cannot_carry_a_path(self):
		self.be()
		result = files_api.stage_file_chunk(
			upload_id="u2",
			file_name="../../../etc/passwd/north-wall.jpg",
			chunk_index=0,
			chunk_count=1,
			total_bytes=3,
			data="YWJj",
		)
		self.assertEqual(result["file_name"], "north-wall.jpg")


# ── 5. the switches and the limits ──────────────────────────────────────────
class TheKillSwitchStops(MobileAPITestCase):
	def test_the_settings_field_stops_every_method(self):
		self.configure(enabled=1, farm_ops_mobile_enabled=0, **ON)
		self.be()
		for call in (
			mobile_api.get_current_user_context,
			mobile_api.list_my_tasks,
			mobile_api.list_available_tasks,
			mobile_api.list_compliance_alerts,
		):
			with self.assertRaises(guard.MobileDisabled):
				call()

	def test_site_config_can_stop_it_without_the_desk(self):
		frappe.conf["farm_ops_mobile_enabled"] = 0
		self.be()
		with self.assertRaises(guard.MobileDisabled):
			mobile_api.list_my_tasks()

	def test_it_answers_503_and_not_401_so_no_phone_is_signed_out(self):
		"""FarmOpsKit treats 401 as 'credential dead, sign out and re-scan'. A
		kill switch that answered 401 would lose every queued completion."""
		self.assertEqual(guard.MobileDisabled.http_status_code, 503)
		self.assertEqual(guard.RateLimited.http_status_code, 429)

	def test_it_is_a_separate_switch_from_the_mcp_master_one(self):
		"""Stopping the AI and stopping the phones are different decisions."""
		self.configure(enabled=0, farm_ops_mobile_enabled=1, **ON)
		self.be()
		self.assertTrue(mobile_api.get_current_user_context()["user"])

	def test_it_ships_on(self):
		self.assertTrue(settings.farm_ops_mobile_enabled())


class TheRateLimitTrips(MobileAPITestCase):
	def test_a_read_survives_a_pull_to_refresh_and_stops_at_the_limit(self):
		self.be()
		for _ in range(guard.READ_LIMIT):
			mobile_api.get_current_user_context()
		with self.assertRaises(guard.RateLimited):
			mobile_api.get_current_user_context()

	def test_a_state_change_gets_a_much_tighter_limit(self):
		self.a_camp()
		tasks = [self.a_task(task_name=f"walk {index}") for index in range(guard.WRITE_LIMIT + 1)]
		self.be()
		for name in tasks[:-1]:
			try:
				mobile_api.claim_task(task=name)
			except guard.RateLimited:
				raise
			except Exception:
				# The concurrent-claim limit refuses long before the rate limit
				# does. That refusal is Sprint 8's and is tested there; what
				# matters here is that it still COUNTS against the window.
				pass
		with self.assertRaises(guard.RateLimited):
			mobile_api.claim_task(task=tasks[-1])

	def test_the_limit_is_per_user_not_per_site(self):
		"""One worker burning their allowance must not lock the crew out."""
		self.enrol(email=OUTSIDER, name="Ben Ortiz", entities=[MAIN])
		self.be()
		for _ in range(guard.READ_LIMIT):
			mobile_api.get_current_user_context()
		self.be(OUTSIDER)
		self.assertEqual(mobile_api.get_current_user_context()["user"], OUTSIDER)


# ── 6. the audit trail and the secrets ──────────────────────────────────────
class EveryCallIsAudited(MobileAPITestCase):
	def test_a_successful_call_writes_one_row_naming_the_caller_and_the_ip(self):
		self.be(remote_addr="100.64.0.7")
		mobile_api.get_current_user_context()
		rows = self.audit_rows("get_current_user_context")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["result_status"], audit.STATUS_SUCCESS)
		self.assertIn(WORKER, rows[0]["result_summary"])
		self.assertEqual(rows[0]["caller_ip"], "100.64.0.7")

	def test_a_refused_call_is_logged_too_and_says_it_was_a_permission_failure(self):
		STORE.seed("User", [{"name": "casual@example.test", "enabled": 1, "full_name": "Casual"}])
		set_roles("casual@example.test", ["Field Worker"])
		self.be("casual@example.test")
		with self.assertRaises(frappe.PermissionError):
			mobile_api.list_my_tasks()
		rows = self.audit_rows("list_my_tasks")
		self.assertEqual(rows[0]["result_status"], audit.STATUS_UNAUTHORIZED)
		self.assertIn("permission_error", rows[0]["result_summary"])

	def test_a_rate_limited_call_is_logged_as_blocked(self):
		self.be()
		for _ in range(guard.READ_LIMIT + 1):
			try:
				mobile_api.get_current_user_context()
			except guard.RateLimited:
				break
		blocked = [row for row in self.audit_rows() if row["result_status"] == audit.STATUS_BLOCKED]
		self.assertTrue(blocked)
		self.assertIn("rate_limited", blocked[-1]["result_summary"])

	def test_a_kill_switched_call_is_logged_as_blocked(self):
		self.configure(enabled=1, farm_ops_mobile_enabled=0, **ON)
		self.be()
		with self.assertRaises(guard.MobileDisabled):
			mobile_api.list_my_tasks()
		rows = self.audit_rows("list_my_tasks")
		self.assertEqual(rows[0]["result_status"], audit.STATUS_BLOCKED)
		self.assertIn("disabled", rows[0]["result_summary"])

	def test_the_rows_go_to_the_action_log_and_not_into_the_compliance_register(self):
		"""Forty phones polling would bury a real auditor's Audit Event list."""
		before = len(STORE.rows("Audit Event"))
		self.be()
		mobile_api.get_current_user_context()
		mobile_api.list_my_tasks()
		self.assertEqual(len(STORE.rows("Audit Event")), before)
		self.assertEqual(len(self.audit_rows()), 2)

	def test_the_arguments_are_recorded_so_a_completion_can_be_reconstructed(self):
		self.a_camp()
		task = self.a_task()
		self.be()

		try:
			mobile_api.complete_task_via_mobile(task=task, clean_pass=True, latitude=45.67, longitude=-121.17)
		except Exception:
			pass
		row = self.audit_rows("complete_task_via_mobile")[0]
		recorded = json.loads(row["arguments_json"])
		self.assertEqual(recorded["task"], task)
		self.assertEqual(recorded["latitude"], 45.67)


class NoSecretReachesThePhone(MobileAPITestCase):
	def test_a_credential_shaped_key_is_stripped_at_any_depth(self):
		payload = {
			"user": WORKER,
			"api_key": "live",
			"api_secret": "live",
			"auth_header": "token a:b",
			"grant": {"api_key": "live", "state": "Active", "nested": [{"password": "x", "ok": 1}]},
		}
		cleaned = guard.strip_secrets(payload)
		self.assertNotIn("api_key", json.dumps(cleaned))
		self.assertNotIn("api_secret", json.dumps(cleaned))
		self.assertNotIn("auth_header", cleaned)
		self.assertEqual(cleaned["grant"]["state"], "Active")
		self.assertEqual(cleaned["grant"]["nested"][0]["ok"], 1)

	def test_the_file_handle_the_app_needs_survives_the_strip(self):
		"""`file_token` trips the substring rule and carries no secret."""
		self.assertEqual(guard.strip_secrets({"file_token": "abc"}), {"file_token": "abc"})

	def test_the_user_context_a_phone_receives_carries_no_credential(self):
		self.be()
		text = json.dumps(mobile_api.get_current_user_context(), default=str)
		for hint in ("api_secret", "api_key", "auth_header"):
			self.assertNotIn(hint, text)


# ── 7. the app can actually decode what the server sends ────────────────────
def parse_login_qr(raw: str, allow_insecure_http: bool = False) -> dict:
	"""`FarmOpsKit.LoginQRParser.parse`, re-implemented line for line.

	NOT a paraphrase of what the Swift is believed to do — a transcription of
	`FarmOpsKit/Sources/FarmOpsKit/Auth/ServerCredentials.swift`, in the same
	order, with the same refusals. v0.17.0 shipped a payload the real parser
	rejected at its first check while every server-side test passed, because
	every server-side test asked the server whether it agreed with itself.

	Raises ValueError carrying the parser's own error case name.
	"""
	try:
		payload = json.loads(raw)
	except (ValueError, TypeError):
		raise ValueError("notJSON") from None
	if not isinstance(payload, dict):
		raise ValueError("notJSON")

	if payload.get("type") != "farm_ops_login":
		raise ValueError(f"wrongType({payload.get('type') or ''})")
	if int(payload.get("v") or 1) > 1:
		raise ValueError(f"unsupportedVersion({payload.get('v')})")

	missing = [
		label
		for key, label in (
			("url", "server address"),
			("user", "user"),
			("api_key", "API key"),
			("api_secret", "API secret"),
		)
		if not str(payload.get(key) or "")
	]
	if missing:
		raise ValueError(f"missingFields({', '.join(missing)})")

	trimmed = str(payload["url"]).strip(" /")
	if "://" not in trimmed or not trimmed.split("://", 1)[1]:
		raise ValueError(f"badURL({payload['url']})")
	if trimmed.split("://", 1)[0].lower() != "https" and not allow_insecure_http:
		raise ValueError("insecureURL")

	return {
		"baseURL": trimmed,
		"user": payload["user"],
		"apiKey": payload["api_key"],
		"apiSecret": payload["api_secret"],
		"authorizationHeader": f"token {payload['api_key']}:{payload['api_secret']}",
	}


class TheAppCanDecodeThis(MobileAPITestCase):
	def a_qr(self, **overrides):
		payload = dict(self.tool_data("generate_mobile_login_qr", {"user": WORKER, **overrides})["payload"])
		return json.dumps(payload, separators=(",", ":"), sort_keys=True)

	def test_a_real_login_qr_passes_the_real_parser(self):
		"""The v0.17.0 bug, asserted as fixed at the point it actually broke."""
		credentials = parse_login_qr(self.a_qr())
		self.assertEqual(credentials["user"], WORKER)
		self.assertEqual(credentials["baseURL"], "https://umbrel.tail4a2b.ts.net")
		self.assertTrue(credentials["apiKey"])
		self.assertTrue(credentials["apiSecret"])
		self.assertEqual(
			credentials["authorizationHeader"],
			f"token {credentials['apiKey']}:{credentials['apiSecret']}",
		)

	def test_the_type_is_the_constant_the_app_checks_for_by_name(self):
		payload = json.loads(self.a_qr())
		self.assertEqual(payload["type"], "farm_ops_login")
		self.assertEqual(payload["type"], mobile_tools.LOGIN_QR_TYPE)

	def test_the_parser_this_test_uses_is_strict_enough_to_have_caught_v0_17_0(self):
		"""A transcription that accepted the old payload would prove nothing."""
		old = json.loads(self.a_qr())
		old.pop("type")
		with self.assertRaises(ValueError) as caught:
			parse_login_qr(json.dumps(old))
		self.assertIn("wrongType", str(caught.exception))

	def test_a_farmcore_onboarding_code_is_still_refused_by_name(self):
		"""The two apps must never cross-sign."""
		with self.assertRaises(ValueError) as caught:
			parse_login_qr(json.dumps({"type": "farm_app_nostr_link", "v": 1, "url": "https://x.test"}))
		self.assertIn("farm_app_nostr_link", str(caught.exception))

	def test_a_plain_http_endpoint_is_still_refused_at_both_ends(self):
		self.configure(enabled=1, public_url="http://umbrel.local", **ON)
		message = self.tool_error("generate_mobile_login_qr", {"user": WORKER})
		self.assertIn("not HTTPS", message)

	def test_the_task_payload_carries_every_key_the_ios_model_decodes(self):
		"""`FarmTask`'s CodingKeys, checked against what the server emits."""
		self.a_camp()
		task = self.a_task()
		self.be()
		row = mobile_api.get_task(task=task)
		for key in (
			"name",
			"task_name",
			"task_type",
			"state",
			"urgency",
			"dispatch_mode",
			"estimated_duration_minutes",
			"skill_required",
			"notes",
			"company",
			"location",
			"location_type",
			"evidence_required",
			"creates_record",
			"source_alert",
			"assignment",
			"assigned_to",
			"claimed_at",
			"started_at",
		):
			self.assertIn(key, row, f"FarmTask decodes {key} and the server does not emit it")

	def test_location_type_is_emitted_alongside_the_doctypes_own_spelling(self):
		unit = self.a_camp()
		task = self.a_task(location_doctype="Housing Unit", location=unit)
		self.be()
		row = mobile_api.get_task(task=task)
		self.assertEqual(row["location_type"], "Housing Unit")
		self.assertEqual(row["location_doctype"], "Housing Unit")

	def test_coordinates_are_omitted_rather_than_zeroed_when_the_site_has_none(self):
		"""0,0 is a real place in the Gulf of Guinea."""
		unit = self.a_camp()
		task = self.a_task(location_doctype="Housing Unit", location=unit)
		self.be()
		row = mobile_api.get_task(task=task)
		self.assertNotIn("latitude", row)
		self.assertNotIn("longitude", row)

	def test_the_user_context_carries_every_key_the_ios_model_decodes(self):
		self.be()
		context = mobile_api.get_current_user_context()
		for key in ("user", "full_name", "employee", "roles", "companies", "default_company", "skills"):
			self.assertIn(key, context, f"UserContext decodes {key}")
		self.assertEqual(context["default_company"], MAIN)
		self.assertEqual([entry["name"] for entry in context["companies"]], [MAIN])
		self.assertIn("abbr", context["companies"][0])
		self.assertIn("Field Worker", context["roles"])

	def test_a_task_raised_by_an_alert_explains_itself_verbatim(self):
		"""The app hides the 'Why this task exists' card without this, and an
		inspection with no stated reason is worse than none."""
		self.a_camp()
		self.tool_data("refresh_compliance_alerts", {"company": MAIN})
		alert = next(iter(STORE.rows("Compliance Alert")), None)
		if alert is None:
			self.skipTest("this fixture raised no compliance alert")
		task = self.a_task(source_alert=alert["name"])
		self.be()
		row = mobile_api.get_task(task=task)
		self.assertEqual(row["source_alert"], alert["name"])
		self.assertEqual(row["source_alert_explanation"], alert["alert_message"])


# ── 8. the whole flow, end to end, through the transport the app uses ───────
class TheWholeFlowWorks(MobileAPITestCase):
	"""Scan to compliance record, over the eleven whitelisted methods only.

	This is the test v0.17.0 did not have. Every capability it shipped worked
	when driven through `mcp.handle`, and every one of them 404'd for the app,
	because nothing anywhere exercised the transport the app actually speaks.
	"""

	def setUp(self):
		super().setUp()
		self.unit = self.a_camp()
		self.task = self.a_task(
			creates_record="Housing Inspection",
			location_doctype="Housing Unit",
			location=self.unit,
		)

	def upload(self, kind, name):
		"""One photograph, in slices, exactly as the app sends it."""
		payload = base64.b64encode(f"{kind}-bytes".encode()).decode()
		digest = hashlib.sha256(base64.b64decode(payload)).hexdigest()
		upload_id = f"{kind}-0001"
		files_api.stage_file_chunk(
			upload_id=upload_id,
			file_name=name,
			chunk_index=0,
			chunk_count=1,
			total_bytes=len(base64.b64decode(payload)),
			data=payload,
		)
		return files_api.finalize_staged_file(
			upload_id=upload_id,
			file_name=name,
			sha256=digest,
			total_bytes=len(base64.b64decode(payload)),
		)

	def test_scan_to_compliance_record_over_the_mobile_api_alone(self):
		self.be()

		context = mobile_api.get_current_user_context()
		self.assertEqual(context["user"], WORKER)
		self.assertEqual(context["default_company"], MAIN)

		pool = mobile_api.list_available_tasks()["tasks"]
		self.assertIn(self.task, {row["name"] for row in pool})

		claimed = mobile_api.claim_task(task=self.task)
		self.assertTrue(claimed["assignment"])
		self.assertTrue(claimed["claimed_at"])

		started = mobile_api.start_task(task=self.task)
		self.assertTrue(started["started_at"])

		mine = mobile_api.list_my_tasks()["tasks"]
		self.assertEqual({row["name"] for row in mine}, {self.task})

		photo = self.upload("photo", "FT_photo.jpg")
		signature = self.upload("signature", "FT_signature.png")
		self.assertTrue(photo["file_token"])
		self.assertTrue(photo["sha256_verified"])

		done = mobile_api.complete_task_via_mobile(
			task=self.task,
			task_assignment=claimed["assignment"],
			findings_text="",
			clean_pass=True,
			completion_narrative="walked it",
			actual_duration_minutes=22,
			latitude=45.6721,
			longitude=-121.1787,
			evidence_files=[
				{"file_token": photo["file_token"], "file_name": "FT_photo.jpg", "kind": "photo"},
				{
					"file_token": signature["file_token"],
					"file_name": "FT_signature.png",
					"kind": "signature",
				},
			],
		)
		self.assertEqual(done["created_record_doctype"], "Housing Inspection")
		self.assertTrue(done["created_record_name"])
		self.assertFalse(done["corrective_action_opened"])
		self.assertEqual(done["evidence_filed"], 2)
		self.assertEqual(frappe.db.get_value("Farm Task", self.task, "state"), "Completed")

	def test_a_mismatched_hash_is_refused_and_the_pieces_are_kept(self):
		"""The app's contract asks for this by name: an audit trail that records
		evidence hashes it never checked is recording a claim, not a fact."""
		self.be()
		payload = base64.b64encode(b"north-wall").decode()
		files_api.stage_file_chunk(
			upload_id="u9",
			file_name="north-wall.jpg",
			chunk_index=0,
			chunk_count=1,
			total_bytes=10,
			data=payload,
		)
		with self.assertRaises(frappe.ValidationError) as caught:
			files_api.finalize_staged_file(
				upload_id="u9", file_name="north-wall.jpg", sha256="0" * 64, total_bytes=10
			)
		self.assertIn("not the bytes that were sent", str(caught.exception))

	def test_one_workers_upload_cannot_be_finalised_by_another(self):
		self.enrol(email=OUTSIDER, name="Ben Ortiz", entities=[MAIN])
		self.be()
		files_api.stage_file_chunk(
			upload_id="shared-id",
			file_name="mine.jpg",
			chunk_index=0,
			chunk_count=1,
			total_bytes=3,
			data=base64.b64encode(b"abc").decode(),
		)
		self.be(OUTSIDER)
		with self.assertRaises(frappe.ValidationError) as caught:
			files_api.finalize_staged_file(
				upload_id="shared-id",
				file_name="mine.jpg",
				sha256=hashlib.sha256(b"abc").hexdigest(),
				total_bytes=3,
			)
		self.assertIn("belongs to", str(caught.exception))

	def test_the_evidence_file_is_private(self):
		self.be()
		handle = self.upload("photo", "north-wall.jpg")
		self.assertTrue(frappe.db.get_value("File", handle["file_token"], "is_private"))

	def test_an_upload_cannot_be_attached_to_an_arbitrary_document(self):
		"""`commit_staged_file` takes an attach target. This never forwards one."""
		self.be()
		handle = self.upload("photo", "north-wall.jpg")
		row = frappe.db.get_value(
			"File", handle["file_token"], ["attached_to_doctype", "attached_to_name"], as_dict=True
		)
		self.assertFalse(row.get("attached_to_doctype"))
		self.assertFalse(row.get("attached_to_name"))


# ── 9. the two things a hostile body could otherwise reach ──────────────────
class TheHandsetsFixReachesTheRecord(MobileAPITestCase):
	"""v0.19.1. The shipped app has been sending `latitude`/`longitude` since
	v0.18 and they went only to the audit row, because Farm Task Assignment had
	no column to put them in. It has one now, so they land on the record — which
	is the location half of §112.161(a)(1)(i) arriving without an app release."""

	def _complete(self, **kwargs):
		self.a_camp()
		task = self.a_task(evidence_required={"findings_text": True})
		self.be()
		mobile_api.claim_task(task=task)
		mobile_api.start_task(task=task)
		mobile_api.complete_task_via_mobile(task=task, findings_text="", **kwargs)
		return STORE.rows("Farm Task Assignment")[0]

	def test_a_coordinate_pair_becomes_the_location(self):
		row = self._complete(latitude=45.6721, longitude=-121.1787)
		self.assertEqual(row["farm_location_gps"], "45.6721000,-121.1787000")

	def test_an_explicit_place_name_beats_the_handsets_fix(self):
		"""A worker who typed a name did so where the fix was absent or wrong.
		Overwriting it with whatever the GPS settled on outside would replace a
		fact with a guess."""
		row = self._complete(farm_location_gps="MC-Cabin-01", latitude=45.6721, longitude=-121.1787)
		self.assertEqual(row["farm_location_gps"], "MC-Cabin-01")

	def test_a_malformed_pair_is_dropped_rather_than_failing_the_completion(self):
		"""THE ONE THAT MATTERS. The completion carries photographs, a signature
		and a compliance record; refusing all of it over an unparseable coordinate
		would trade the record for its least important field. The pair as sent is
		still in the audit row, which is where a bad one is worth looking at."""
		row = self._complete(latitude="not-a-number", longitude="also-not")
		self.assertFalse(row.get("farm_location_gps"))
		recorded = json.loads(self.audit_rows("complete_task_via_mobile")[0]["arguments_json"])
		self.assertEqual(recorded["latitude"], "not-a-number")

	def test_a_completion_carrying_no_location_at_all_still_lands(self):
		row = self._complete()
		self.assertFalse(row.get("farm_location_gps"))
		self.assertEqual(row["state"], "Completed")


class TheBodyCannotNameTheCaller(MobileAPITestCase):
	"""Frappe binds body keys that match a method's signature, and `user` is in
	every one of these signatures because the guard injects it."""

	def setUp(self):
		super().setUp()
		self.enrol(email=OUTSIDER, name="Ben Ortiz", entities=[MAIN])

	def test_a_user_key_in_the_body_is_dropped_not_honoured(self):
		self.be(WORKER)
		context = mobile_api.get_current_user_context(user=OUTSIDER)
		self.assertEqual(context["user"], WORKER)

	def test_and_it_does_not_crash_the_call_either(self):
		"""The collision would raise rather than escalate, so the bug this
		prevents is loud. It should also simply not happen."""
		self.be(WORKER)
		for call in (mobile_api.list_my_tasks, mobile_api.list_available_tasks):
			self.assertIsInstance(call(user="attacker@example.test"), dict)

	def test_the_audit_row_names_the_authenticated_caller_not_the_claimed_one(self):
		self.be(WORKER)
		mobile_api.get_current_user_context(user=OUTSIDER)
		summary = self.audit_rows("get_current_user_context")[0]["result_summary"]
		self.assertIn(WORKER, summary)
		self.assertNotIn(OUTSIDER, summary)


class RefusalsAreMeteredToo(MobileAPITestCase):
	def test_an_ungranted_account_cannot_grow_the_audit_log_without_bound(self):
		"""Every refusal writes a row. A caller holding a valid token but no
		grant is never allowed to do anything — and is exactly the one worth
		metering, or the log is a free write primitive."""
		STORE.seed("User", [{"name": "prober@example.test", "enabled": 1, "full_name": "Prober"}])
		set_roles("prober@example.test", ["Field Worker"])
		self.be("prober@example.test")

		refused = 0
		for _ in range(guard.READ_LIMIT * 2):
			try:
				mobile_api.list_my_tasks()
			except guard.RateLimited:
				break
			except frappe.PermissionError:
				refused += 1
		else:
			self.fail("the refusal path was never rate limited")

		self.assertLessEqual(refused, guard.READ_LIMIT)
		self.assertLessEqual(len(self.audit_rows("list_my_tasks")), guard.READ_LIMIT + 1)

	def test_a_guest_is_metered_by_the_address_it_arrives_from(self):
		"""Not by the name "Guest", which every anonymous caller shares — one
		bucket for all of them would let a caller who has hit the limit go quiet
		by suppressing the next unrelated one's audit rows."""
		self.be("Guest", remote_addr="203.0.113.9")
		for _ in range(guard.READ_LIMIT):
			with self.assertRaises(frappe.PermissionError):
				mobile_api.list_my_tasks()
		with self.assertRaises(guard.RateLimited):
			mobile_api.list_my_tasks()

		# A different address is a different bucket, and is still refused on its
		# own merits rather than on somebody else's spending.
		self.be("Guest", remote_addr="198.51.100.4")
		with self.assertRaises(frappe.PermissionError):
			mobile_api.list_my_tasks()


# ── 8. the hiring wizard's Assignment and Housing steps ─────────────────────
class TheWizardsDropdownsComeFromTheSite(MobileAPITestCase):
	"""v0.54.0. `list_onboarding_reference_data`.

	The four masters an operator maintains in the Desk, read by the phone instead
	of compiled into it. The failure this prevents is the one
	`list_i9_document_types` prevented for the I-9's document picker: a Swift
	array that goes stale silently, and a wizard whose Assignment step fails at
	the END of a hire with "not a Designation on this site" and no way to find
	out what is.
	"""

	def setUp(self):
		super().setUp()
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		STORE.seed("Branch", [{"name": "Mill Creek Camp", "branch": "Mill Creek Camp"}])
		STORE.seed("Designation", [{"name": "Picker", "designation_name": "Picker"}])
		STORE.seed(
			"Employment Type",
			[
				{"name": "Seasonal", "employee_type_name": "Seasonal"},
				{"name": "Permanent", "employee_type_name": "Permanent"},
				{"name": "Contract", "employee_type_name": "Contract"},
			],
		)
		STORE.seed(
			"Department",
			[
				{"name": "Harvest", "department_name": "Harvest", "company": MAIN, "is_group": 0},
				{"name": "All Departments", "department_name": "All", "company": MAIN, "is_group": 1},
				{"name": "Packing", "department_name": "Packing", "company": OTHER, "is_group": 0},
			],
		)

	def test_all_four_masters_come_back_in_one_call(self):
		"""One call, because they are read together once when the step opens and
		four round trips over a tailgate LTE connection is four chances to
		half-populate a form."""
		self.be()
		answer = mobile_api.list_onboarding_reference_data()
		for key in ("branches", "departments", "designations", "employment_types"):
			with self.subTest(key=key):
				self.assertTrue(answer[key], key)
		self.assertEqual(answer["counts"]["employment_types"], 3)
		self.assertEqual(answer["masters_absent"], [])

	def test_the_three_employment_types_the_wizard_offers_are_the_sites_own(self):
		self.be()
		names = {row["name"] for row in mobile_api.list_onboarding_reference_data()["employment_types"]}
		self.assertEqual(names, {"Seasonal", "Permanent", "Contract"})

	def test_a_branch_with_no_second_column_still_has_a_label(self):
		"""Frappe HR's Branch is a docname and nothing else on some sites, and a
		dropdown row with a null label is a blank line somebody has to pick."""
		STORE.seed("Branch", [{"name": "Bare Camp"}])
		self.be()
		labels = {
			row["name"]: row["label"] for row in mobile_api.list_onboarding_reference_data()["branches"]
		}
		self.assertEqual(labels["Bare Camp"], "Bare Camp")
		self.assertEqual(labels["Mill Creek Camp"], "Mill Creek Camp")

	def test_another_entitys_department_is_not_offered(self):
		"""Department is the only one of the four that carries a company, and the
		scoping rule the rest of this surface follows applies to it too."""
		self.be()
		names = {row["name"] for row in mobile_api.list_onboarding_reference_data()["departments"]}
		self.assertIn("Harvest", names)
		self.assertNotIn("Packing", names)

	def test_a_group_department_is_not_offered(self):
		"""`is_group` marks a node in the tree rather than somewhere a person is
		assigned, and an Employee pointed at one double-counts in every report."""
		self.be()
		names = {row["name"] for row in mobile_api.list_onboarding_reference_data()["departments"]}
		self.assertNotIn("All Departments", names)

	def test_a_master_this_site_does_not_have_is_empty_and_named_not_an_error(self):
		"""A site without Frappe HR has none of the four, and the honest answer is
		a wizard that offers no choices for that field rather than a hire that
		cannot start. `create_employee` agrees: `_clean` does not check a Link
		whose target doctype is absent."""
		from .harness import INSTALLED_DOCTYPES

		# Not seeded away — removed from the site's installed set, which is what
		# "this bench has no hrms" actually looks like to `compat.doctype_exists`.
		INSTALLED_DOCTYPES.discard("Branch")
		self.addCleanup(INSTALLED_DOCTYPES.add, "Branch")

		self.be()
		answer = mobile_api.list_onboarding_reference_data()
		self.assertEqual(answer["branches"], [])
		self.assertIn("Branch", answer["masters_absent"])
		self.assertTrue(answer["designations"])

	def test_a_field_worker_may_read_it_because_nothing_on_it_is_about_a_person(self):
		"""Deliberately unlike `search_employees`. A job title and a camp name are
		not a personnel record, and gating them would mean the wizard's own
		dropdowns needed a role the rest of the surface does not."""
		set_roles(WORKER, ["Field Worker"])
		self.be()
		self.assertTrue(mobile_api.list_onboarding_reference_data()["designations"])

	def test_it_refuses_an_entity_this_phone_cannot_reach(self):
		self.be()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.list_onboarding_reference_data(company=OTHER)


class TheCampHasARead(MobileAPITestCase):
	"""v0.54.0. `list_available_housing` — beds and bodies, and no names."""

	def setUp(self):
		super().setUp()
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.a_camp("MC-Cabin-01")

	def _units(self, **kwargs):
		return {row["name"]: row for row in mobile_api.list_available_housing(**kwargs)["units"]}

	def test_an_empty_cabin_reports_its_capacity_and_no_occupants(self):
		self.be()
		unit = self._units()[self.unit]
		self.assertEqual(unit["capacity"], 4)
		self.assertEqual(unit["current_occupants"], 0)
		self.assertEqual(unit["open_beds"], 4)
		self.assertEqual(unit["status"], "Available")
		self.assertTrue(unit["assignable"])

	def test_it_counts_who_is_in_a_cabin_and_never_names_them(self):
		"""`list_housing_units` returns an `occupants` list of employee names.
		Who sleeps in which cabin is a personnel fact, and it has no business on
		a picker's phone merely because the vacancy count does."""
		self.be()
		mobile_api.assign_housing(
			employee=WORKER_EMPLOYEE, housing_unit=self.unit, check_in_date="2026-08-01"
		)

		unit = self._units()[self.unit]
		self.assertEqual(unit["current_occupants"], 1)
		self.assertEqual(unit["open_beds"], 3)
		self.assertNotIn("occupants", unit)
		self.assertNotIn("Ana Ramos", json.dumps(unit))

	def test_a_full_cabin_is_dropped_by_default_and_kept_with_a_reason_on_request(self):
		self.be()
		for index in range(4):
			STORE.seed(
				"Employee",
				[
					{
						"name": f"EMP-FILL-{index}",
						"employee_name": f"Filler {index}",
						"company": MAIN,
						"status": "Active",
					}
				],
			)
			mobile_api.assign_housing(
				employee=f"EMP-FILL-{index}", housing_unit=self.unit, check_in_date="2026-08-01"
			)

		self.assertNotIn(self.unit, self._units())

		unit = self._units(include_full=True)[self.unit]
		self.assertEqual(unit["status"], "Full")
		self.assertFalse(unit["assignable"])
		self.assertIn("4 bed(s) are taken", unit["unassignable_reason"])

	def test_a_shower_block_is_never_offered_at_all(self):
		"""`create_housing_assignment` refuses one by name, and a dropdown that
		offers it is a dropdown whose next screen is a refusal."""
		self.tool_data(
			"create_housing_unit",
			{"parcel": "Mill Creek", "unit_name": "MC-Bath", "unit_type": "Toilet-Shower"},
		)
		self.be()
		self.assertNotIn("MC-Bath - MC", self._units())

	def test_a_condemned_cabin_is_hidden_by_default_and_says_why_when_shown(self):
		"""A foreman who cannot find the cabin they expected needs to be told it
		is condemned, not shown a shorter list."""
		frappe.db.set_value("Housing Unit", self.unit, "condition", "Uninhabitable")
		self.be()
		self.assertNotIn(self.unit, self._units())

		unit = self._units(include_full=True)[self.unit]
		self.assertEqual(unit["status"], "Uninhabitable")
		self.assertFalse(unit["assignable"])
		self.assertIn("Uninhabitable", unit["unassignable_reason"])

	def test_a_cabin_nobody_has_measured_is_not_reported_as_full(self):
		"""Capacity zero means unmeasured, not "no beds" — a camp whose
		capacities were never entered would otherwise come back with every bed
		taken and nothing saying why."""
		self.tool_data(
			"create_housing_unit",
			{"parcel": "Mill Creek", "unit_name": "MC-Cabin-09", "unit_type": "Cabin"},
		)
		self.be()
		unit = self._units()["MC-Cabin-09 - MC"]
		self.assertIsNone(unit["capacity"])
		self.assertIsNone(unit["open_beds"])
		self.assertEqual(unit["status"], "Available")
		self.assertTrue(unit["assignable"])

	def test_it_refuses_an_entity_this_phone_cannot_reach(self):
		self.be()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.list_available_housing(company=OTHER)


class ABranchResolvesToItsGround(MobileAPITestCase):
	"""v0.54.0. `Parcel.branch`, and the join it exists to make.

	An Employee carries a Branch and a Housing Unit stands on a Parcel. Before
	this column there was no join between them at all, so a wizard that had just
	asked which camp somebody was hired to could not then show that camp's
	cabins — the iOS side would have had to fetch every parcel, fetch every unit,
	and work the mapping out itself.

	The resolution happens server-side through one function that both
	`list_onboarding_reference_data` and `list_available_housing` call, so the
	mapping the wizard was SHOWN and the mapping the housing list FILTERS ON
	cannot come apart.
	"""

	def setUp(self):
		super().setUp()
		# `update_parcel` is how a branch is put on a parcel, and it is a mutating
		# tool an operator switches on by hand like every other one.
		self.configure(enabled=1, public_url="https://umbrel.tail4a2b.ts.net", **ON, allow_update_parcel=1)
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		STORE.seed(
			"Branch",
			[
				{"name": "Mill Creek Camp", "branch": "Mill Creek Camp"},
				{"name": "Grande Camp", "branch": "Grande Camp"},
				{"name": "Packhouse", "branch": "Packhouse"},
			],
		)
		# Mill Creek Camp grew across a fence line: two parcels, one camp. Grande
		# Camp is the ordinary single-parcel case. Packhouse is a real operating
		# unit with no ground tagged to it at all, which is its own answer.
		for parcel_name in ("Mill Creek", "Mill Creek North", "Grande Ronde"):
			self.tool_data(
				"create_parcel",
				{"owning_entity": MAIN, "parcel_name": parcel_name, "acreage": 40.0},
			)
		for parcel_name, branch in (
			("Mill Creek", "Mill Creek Camp"),
			("Mill Creek North", "Mill Creek Camp"),
			("Grande Ronde", "Grande Camp"),
		):
			self.tool_data("update_parcel", {"parcel": parcel_name, "branch": branch})

		self.cabins = {}
		for parcel_name, unit_name in (
			("Mill Creek", "MC-Cabin-01"),
			("Mill Creek North", "MCN-Cabin-01"),
			("Grande Ronde", "GR-Cabin-01"),
		):
			self.cabins[unit_name] = self.tool_data(
				"create_housing_unit",
				{"parcel": parcel_name, "unit_name": unit_name, "unit_type": "Cabin", "capacity": 4},
			)["name"]

	def _names(self, **kwargs):
		return {row["name"] for row in mobile_api.list_available_housing(**kwargs)["units"]}

	# ── the reference read ──────────────────────────────────────────────────
	def test_every_branch_row_carries_the_parcels_it_holds(self):
		self.be()
		rows = {row["name"]: row for row in mobile_api.list_onboarding_reference_data()["branches"]}
		self.assertEqual(rows["Mill Creek Camp"]["parcels"], ["Mill Creek - ETC", "Mill Creek North - ETC"])
		self.assertEqual(rows["Grande Camp"]["parcels"], ["Grande Ronde - ETC"])
		self.assertEqual(rows["Packhouse"]["parcels"], [])

	def test_the_scalar_parcel_is_set_only_when_there_is_exactly_one(self):
		"""Null for none AND for several. A scalar that silently picked the first
		of two parcels would send half a camp's cabins missing, and a client
		reading only this field has to fall back to asking the server."""
		self.be()
		rows = {row["name"]: row for row in mobile_api.list_onboarding_reference_data()["branches"]}
		self.assertEqual(rows["Grande Camp"]["parcel"], "Grande Ronde - ETC")
		self.assertIsNone(rows["Mill Creek Camp"]["parcel"])
		self.assertEqual(rows["Mill Creek Camp"]["parcel_count"], 2)
		self.assertIsNone(rows["Packhouse"]["parcel"])

	def test_a_branch_with_no_ground_is_listed_and_called_out_rather_than_hidden(self):
		"""It is a real operating unit somebody may legitimately hire into. What
		it is not is a camp with housing."""
		self.be()
		answer = mobile_api.list_onboarding_reference_data()
		self.assertIn("Packhouse", {row["name"] for row in answer["branches"]})
		self.assertEqual(answer["branches_without_parcels"], ["Packhouse"])

	# ── the housing read ────────────────────────────────────────────────────
	def test_passing_a_branch_returns_that_camps_cabins_and_no_others(self):
		"""The whole point: the phone passes the branch it just hired somebody
		into and does no parcel lookup of its own."""
		self.be()
		self.assertEqual(self._names(branch="Grande Camp"), {self.cabins["GR-Cabin-01"]})

	def test_a_camp_spanning_two_parcels_returns_both(self):
		"""A filter that took only the first parcel would hide half the beds on
		exactly the operations big enough to have the problem."""
		self.be()
		self.assertEqual(
			self._names(branch="Mill Creek Camp"),
			{self.cabins["MC-Cabin-01"], self.cabins["MCN-Cabin-01"]},
		)

	def test_the_parcels_searched_are_echoed_back(self):
		"""A foreman looking at an unexpectedly short list needs to see which
		ground was searched."""
		self.be()
		answer = mobile_api.list_available_housing(branch="Mill Creek Camp")
		self.assertTrue(answer["branch_filter_applied"])
		self.assertEqual(answer["branch_parcels"], ["Mill Creek - ETC", "Mill Creek North - ETC"])
		self.assertIsNone(answer["branch_note"])

	def test_a_branch_that_names_nothing_is_refused_rather_than_answered_empty(self):
		"""A typo resolves to no parcels, and "no parcels" and "no beds" produce
		the same empty list — so the mistake has to be caught while it can still
		be told apart from an answer."""
		self.be()
		with self.assertRaises(frappe.DoesNotExistError) as caught:
			mobile_api.list_available_housing(branch="Mil Creek Camp")
		self.assertIn("list_onboarding_reference_data", str(caught.exception))

	def test_a_real_branch_with_no_ground_lists_everything_and_says_why(self):
		"""Never a silent empty list. An empty camp reads on a phone as "no
		room", which is the one wrong answer this endpoint can give."""
		self.be()
		answer = mobile_api.list_available_housing(branch="Packhouse")
		self.assertFalse(answer["branch_filter_applied"])
		self.assertEqual(answer["branch_parcels"], [])
		self.assertIn("No parcel is tagged with branch Packhouse", answer["branch_note"])
		self.assertIn("update_parcel", answer["branch_note"])
		self.assertEqual(len(answer["units"]), 3)

	def test_a_site_that_has_not_migrated_the_column_says_so_and_lists_everything(self):
		"""The other way this can fail, and it is a different sentence: the
		column is not there rather than nothing being tagged with it."""
		real = compat.has_field

		# Wraps rather than replaces: every other field answers truthfully, so
		# `existing_fields` still builds a real column list and only the one
		# column this site would be missing is missing.
		def unmigrated(doctype, field):
			return False if (doctype == "Parcel" and field == "branch") else real(doctype, field)

		with mock.patch.object(compat, "has_field", unmigrated):
			self.be()
			answer = mobile_api.list_available_housing(branch="Mill Creek Camp")
		self.assertFalse(answer["branch_filter_applied"])
		self.assertIn("no branch column", answer["branch_note"])
		self.assertIn("migrate", answer["branch_note"])
		self.assertEqual(len(answer["units"]), 3)

	def test_a_branch_and_a_parcel_together_intersect(self):
		"""Which is what somebody asking for one parcel of a two-parcel camp
		means."""
		self.be()
		self.assertEqual(
			self._names(branch="Mill Creek Camp", parcel="Mill Creek"),
			{self.cabins["MC-Cabin-01"]},
		)

	def test_the_branch_the_wizard_was_shown_is_the_one_the_housing_read_filters_on(self):
		"""The two halves asserted together. If these ever disagree the wizard
		shows a camp and then shows none of its cabins."""
		self.be()
		for row in mobile_api.list_onboarding_reference_data()["branches"]:
			if not row["parcels"]:
				continue
			with self.subTest(branch=row["name"]):
				answer = mobile_api.list_available_housing(branch=row["name"])
				self.assertEqual(answer["branch_parcels"], row["parcels"])

	# ── the mapping itself ──────────────────────────────────────────────────
	def test_a_parcel_cannot_be_tagged_with_a_branch_that_does_not_exist(self):
		"""The refusal is at the record, so nothing downstream has to defend
		against a branch nobody can resolve."""
		message = self.tool_error("update_parcel", {"parcel": "Grande Ronde", "branch": "Nowhere"})
		self.assertIn("Nowhere", message)
		self.assertIn("Branch", message)

	def test_clearing_a_branch_unassigns_the_ground(self):
		"""How a camp folded into another one is recorded."""
		self.tool_data("update_parcel", {"parcel": "Grande Ronde", "branch": ""})
		self.be()
		answer = mobile_api.list_available_housing(branch="Grande Camp")
		self.assertFalse(answer["branch_filter_applied"])
		self.assertIn("No parcel is tagged", answer["branch_note"])

	def test_another_entitys_ground_is_not_in_a_branchs_parcels(self):
		"""The scoping rule applied to the JOIN rather than only to the rows —
		a branch with parcels under a company this caller cannot see reports
		only the ones they can."""
		self.tool_data(
			"create_parcel",
			{"owning_entity": OTHER, "parcel_name": "Far Side", "acreage": 10.0},
		)
		self.tool_data("update_parcel", {"parcel": "Far Side", "branch": "Grande Camp"})
		self.be()
		rows = {row["name"]: row for row in mobile_api.list_onboarding_reference_data()["branches"]}
		self.assertEqual(rows["Grande Camp"]["parcels"], ["Grande Ronde - ETC"])


class TheCampHasAWrite(MobileAPITestCase):
	"""v0.54.0. `assign_housing` — the wizard's Housing step."""

	def setUp(self):
		super().setUp()
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.a_camp("MC-Cabin-01")

	def test_it_puts_one_person_in_one_cabin_from_one_date(self):
		self.be()
		answer = mobile_api.assign_housing(
			employee=WORKER_EMPLOYEE, housing_unit=self.unit, check_in_date="2026-08-01"
		)
		self.assertTrue(answer["assignment"])
		self.assertEqual(answer["employee"], WORKER_EMPLOYEE)
		self.assertEqual(answer["unit"], self.unit)
		self.assertEqual(answer["check_in_date"], "2026-08-01")
		self.assertEqual(answer["status"], "Current")
		self.assertEqual(answer["current_occupants"], 1)
		self.assertEqual(answer["open_beds"], 3)
		self.assertEqual(answer["company"], MAIN)

	def test_a_second_person_into_a_four_bunk_cabin_is_the_ordinary_case(self):
		"""The tool refuses an overlap without `allow_multi_occupancy` and this
		method passes it under capacity on the caller's behalf — a shared cabin
		is what a cabin IS, and a wizard that refused the second picker into a
		four-bunk unit would be unusable in July."""
		STORE.seed(
			"Employee",
			[
				{
					"name": "EMP-LUZ",
					"employee_name": "Luz Herrera",
					"company": MAIN,
					"status": "Active",
				}
			],
		)
		self.be()
		mobile_api.assign_housing(
			employee=WORKER_EMPLOYEE, housing_unit=self.unit, check_in_date="2026-08-01"
		)
		answer = mobile_api.assign_housing(
			employee="EMP-LUZ", housing_unit=self.unit, check_in_date="2026-08-02"
		)
		self.assertEqual(answer["current_occupants"], 2)

	def test_it_refuses_to_overfill_a_cabin_where_the_tool_only_warns(self):
		"""The difference is deliberate. A warning is right on a console where an
		operator can weigh it; on a phone nothing displays it, the foreman has
		walked away, and a bed that does not exist becomes somebody sleeping in a
		truck."""
		self.be()
		for index in range(4):
			STORE.seed(
				"Employee",
				[
					{
						"name": f"EMP-FILL-{index}",
						"employee_name": f"Filler {index}",
						"company": MAIN,
						"status": "Active",
					}
				],
			)
			mobile_api.assign_housing(
				employee=f"EMP-FILL-{index}", housing_unit=self.unit, check_in_date="2026-08-01"
			)

		with self.assertRaises(Exception) as caught:
			mobile_api.assign_housing(
				employee=WORKER_EMPLOYEE, housing_unit=self.unit, check_in_date="2026-08-01"
			)
		message = str(caught.exception)
		self.assertIn("holds 4", message)
		self.assertIn("list_available_housing", message)

		# Asserted on the PERSON rather than on a row count. `guard._record`
		# rolls back on every refusal — deliberately, so a failed call leaves
		# nothing behind but its audit row — and in this suite the four fills
		# above share that uncommitted transaction, so counting rows here would
		# be measuring the rollback rather than the refusal.
		self.assertFalse(
			[row for row in STORE.rows("Housing Assignment") if row.get("employee") == WORKER_EMPLOYEE]
		)

	def test_the_phone_cannot_send_the_flag_that_turns_the_capacity_check_off(self):
		"""`allow_multi_occupancy` is the one argument that would undo the check
		above, so it is not in the signature at all — Frappe's own argument
		filter drops a body key that matches nothing."""
		self.be()
		with self.assertRaises(TypeError):
			mobile_api.assign_housing(
				employee=WORKER_EMPLOYEE,
				housing_unit=self.unit,
				check_in_date="2026-08-01",
				allow_multi_occupancy=True,
			)

	def test_a_field_worker_may_not_write_one(self):
		"""A Housing Assignment names a person, a building and the dates between
		them. It is the audit trail defending a Section 119 exclusion and the
		answer to an ORS 653 wage claim.

		v0.94.0 moved the gate from `HR_ROLES` to `HIRING_ROLES`; a picker is on
		neither, so this refusal is unchanged in substance and only in wording."""
		set_roles(WORKER, ["Field Worker"])
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.assign_housing(
				employee=WORKER_EMPLOYEE, housing_unit=self.unit, check_in_date="2026-08-01"
			)
		self.assertIn("may not bring a person onto the farm", str(caught.exception))

	def test_but_a_foreman_may_assign_a_bunk(self):
		"""THE WIDENING. Assigning housing is step 8 of a hire, and the rules that
		actually protect a camp are not this gate: the overlap refusal, lawful
		occupancy, the capacity ceiling and the condemned-unit rule all run
		whoever is calling, so a foreman still cannot overfill a cabin."""
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.be()
		self.assertTrue(
			mobile_api.assign_housing(
				employee=WORKER_EMPLOYEE, housing_unit=self.unit, check_in_date="2026-08-01"
			)
		)

	def test_a_check_in_date_is_required(self):
		self.be()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.assign_housing(employee=WORKER_EMPLOYEE, housing_unit=self.unit)
		self.assertIn("check_in_date", str(caught.exception))

	def test_an_employee_of_another_entity_is_not_found_rather_than_refused(self):
		"""The same wording a docname that does not exist gets, so a caller
		cannot map the site's employees by watching which error comes back."""
		self.be()
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.assign_housing(
				employee=OUTSIDER_EMPLOYEE, housing_unit=self.unit, check_in_date="2026-08-01"
			)

	def test_a_cabin_belonging_to_another_entity_is_not_found_either(self):
		"""A Housing Unit calls its company `owning_entity`, so
		`require_scoped_doc` reads a field that is not there and the check is
		made by hand. This is the test that it is made at all."""
		frappe.db.set_value("Housing Unit", self.unit, "owning_entity", OTHER)
		self.be()
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.assign_housing(
				employee=WORKER_EMPLOYEE, housing_unit=self.unit, check_in_date="2026-08-01"
			)

	def test_it_carries_the_section_119_note_and_the_unrecorded_deduction_warning(self):
		"""Whether a housing charge came out of wages is the question ORS 653
		constrains, and Unknown is the answer that cannot be defended."""
		self.be()
		answer = mobile_api.assign_housing(
			employee=WORKER_EMPLOYEE, housing_unit=self.unit, check_in_date="2026-08-01"
		)
		self.assertEqual(answer["housing_deduction_from_wages"], "Unknown")
		self.assertIn("Section 119", answer["section_119_note"])
		self.assertTrue(any("ORS 653" in warning for warning in answer["warnings"]))

	def test_the_deduction_answer_is_forwarded_when_the_wizard_asks_it(self):
		self.be()
		answer = mobile_api.assign_housing(
			employee=WORKER_EMPLOYEE,
			housing_unit=self.unit,
			check_in_date="2026-08-01",
			housing_deduction_from_wages="No",
			deposit_paid=150,
		)
		self.assertEqual(answer["housing_deduction_from_wages"], "No")
		self.assertEqual(answer["deposit_paid"], 150.0)

	def test_every_call_leaves_an_audit_row(self):
		self.be()
		mobile_api.assign_housing(
			employee=WORKER_EMPLOYEE, housing_unit=self.unit, check_in_date="2026-08-01"
		)
		self.assertEqual(len(self.audit_rows("assign_housing")), 1)


class TheAssignmentStepCanRecordWhereSomebodyWorks(MobileAPITestCase):
	"""v0.54.0. `create_employee` on the phone, and the field that had nowhere
	to land.

	`tools/employee.WRITABLE` carried designation, department and employment type
	and not `branch`, so the wizard's Assignment step could ask which camp
	somebody reports to and could not record the answer. The wrapper forwards it
	like the other three and adds no rule of its own — the allowlist and the Link
	check stay in `tools/employee.py`, which is why this asserts through the
	published endpoint rather than around it.
	"""

	def setUp(self):
		super().setUp()
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		STORE.seed("Branch", [{"name": "Mill Creek Camp", "branch": "Mill Creek Camp"}])
		STORE.seed("Designation", [{"name": "Picker", "designation_name": "Picker"}])
		STORE.seed("Employment Type", [{"name": "Seasonal", "employee_type_name": "Seasonal"}])
		STORE.seed(
			"Department",
			[{"name": "Harvest", "department_name": "Harvest", "company": MAIN, "is_group": 0}],
		)

	def test_the_four_assignment_dropdowns_all_reach_the_record(self):
		self.be()
		created = mobile_api.create_employee(
			first_name="Elena",
			last_name="Marquez",
			company=MAIN,
			designation="Picker",
			department="Harvest",
			employment_type="Seasonal",
			branch="Mill Creek Camp",
		)
		row = frappe.db.get_value(
			"Employee",
			created["name"],
			["designation", "department", "employment_type", "branch"],
			as_dict=True,
		)
		self.assertEqual(row["designation"], "Picker")
		self.assertEqual(row["department"], "Harvest")
		self.assertEqual(row["employment_type"], "Seasonal")
		self.assertEqual(row["branch"], "Mill Creek Camp")

	def test_a_branch_that_names_nothing_is_refused_by_the_tool_not_the_wrapper(self):
		"""The refusal lists what this site has, which is the whole reason the
		wizard reads its dropdowns from `list_onboarding_reference_data` rather
		than from an array compiled into the app."""
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.create_employee(
				first_name="Elena", last_name="Marquez", company=MAIN, branch="Nowhere Camp"
			)
		message = str(caught.exception)
		self.assertIn("Nowhere Camp", message)
		self.assertIn("Mill Creek Camp", message)

	def test_the_branch_the_wizard_offers_is_one_the_reference_read_returned(self):
		"""The two halves of the same step, asserted together: a value that came
		out of the dropdown read is a value the create accepts. If these ever
		disagree the wizard fails on its last screen."""
		self.be()
		offered = mobile_api.list_onboarding_reference_data()["branches"][0]["name"]
		created = mobile_api.create_employee(
			first_name="Elena", last_name="Marquez", company=MAIN, branch=offered
		)
		self.assertEqual(frappe.db.get_value("Employee", created["name"], "branch"), offered)


class TheReturningWorkersCabin(MobileAPITestCase):
	"""v0.54.0. `list_available_housing(employee=…)` and `previous_assignment`.

	A picker who worked last season had a cabin, and usually wants it again. The
	wizard shows "Last year: MC-Cabin-07" at the top of the list so a returning
	worker is one tap rather than a scroll through forty units nobody remembers
	the numbers of.

	IT IS THE ONE ARGUMENT ON THIS ENDPOINT THAT NAMES A PERSON, which is why it
	carries a gate the rest of the method does not.
	"""

	def setUp(self):
		super().setUp()
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.a_camp("MC-Cabin-01")
		self.second = self.tool_data(
			"create_housing_unit",
			{"parcel": "Mill Creek", "unit_name": "MC-Cabin-07", "unit_type": "Cabin", "capacity": 2},
		)["name"]

	def _previous(self, **kwargs):
		return mobile_api.list_available_housing(employee=WORKER_EMPLOYEE, **kwargs)["previous_assignment"]

	def _stay(self, unit, start, end, employee=WORKER_EMPLOYEE):
		"""One finished stay, written the way a season that ended looks."""
		self.be()
		created = mobile_api.assign_housing(employee=employee, housing_unit=unit, check_in_date=start)
		frappe.db.set_value("Housing Assignment", created["assignment"], "end_date", end)
		frappe.db.set_value("Housing Assignment", created["assignment"], "status", "Ended")
		return created["assignment"]

	# ── what it returns ─────────────────────────────────────────────────────
	def test_it_surfaces_last_seasons_cabin_with_both_dates(self):
		self._stay(self.second, "2025-06-01", "2025-10-15")
		self.be()
		previous = self._previous()
		self.assertEqual(previous["unit"], self.second)
		self.assertEqual(previous["unit_name"], "MC-Cabin-07")
		self.assertEqual(previous["check_in_date"], "2025-06-01")
		self.assertEqual(previous["check_out_date"], "2025-10-15")
		self.assertTrue(previous["available"])
		self.assertIsNone(previous["unavailable_reason"])

	def test_the_unit_name_is_what_is_painted_on_the_door(self):
		"""Not the docname, which carries the parcel key on the end of it. A row
		reading "MC-Cabin-07 - MC" is a row a foreman has to parse."""
		self._stay(self.second, "2025-06-01", "2025-10-15")
		self.be()
		self.assertEqual(self._previous()["unit_name"], "MC-Cabin-07")
		self.assertIn(" - ", self._previous()["unit"])

	def test_the_most_recent_ended_stay_wins(self):
		"""Three seasons in two cabins: the answer is the one they left last."""
		self._stay(self.unit, "2023-06-01", "2023-10-01")
		self._stay(self.second, "2024-06-01", "2024-10-01")
		self._stay(self.unit, "2025-06-01", "2025-10-01")
		self.be()
		self.assertEqual(self._previous()["unit"], self.unit)
		self.assertEqual(self._previous()["check_out_date"], "2025-10-01")

	def test_a_first_season_hire_has_no_previous_assignment_and_that_is_not_an_error(self):
		self.be()
		self.assertIsNone(self._previous())

	def test_it_is_absent_unless_an_employee_is_named(self):
		"""The vacancy read is unchanged for every caller that does not ask."""
		self.be()
		self.assertIsNone(mobile_api.list_available_housing()["previous_assignment"])

	# ── whether the cabin can actually be had ───────────────────────────────
	def test_a_cabin_that_filled_up_since_is_reported_unavailable_with_the_reason(self):
		"""The point of the field. Offering a one-tap re-assignment into a full
		cabin is an offer whose next screen is a refusal."""
		self._stay(self.second, "2025-06-01", "2025-10-15")
		for index in range(2):
			STORE.seed(
				"Employee",
				[
					{
						"name": f"EMP-NEW-{index}",
						"employee_name": f"New {index}",
						"company": MAIN,
						"status": "Active",
					}
				],
			)
			self.be()
			mobile_api.assign_housing(
				employee=f"EMP-NEW-{index}",
				housing_unit=self.second,
				check_in_date=frappe.utils.today(),
			)

		self.be()
		previous = self._previous()
		self.assertEqual(previous["unit"], self.second)
		self.assertFalse(previous["available"])
		self.assertIn("2 bed(s) are taken", previous["unavailable_reason"])
		self.assertEqual(previous["open_beds"], 0)

	def test_a_cabin_condemned_since_they_left_says_so(self):
		self._stay(self.second, "2025-06-01", "2025-10-15")
		frappe.db.set_value("Housing Unit", self.second, "condition", "Uninhabitable")
		self.be()
		previous = self._previous()
		self.assertFalse(previous["available"])
		self.assertIn("Uninhabitable", previous["unavailable_reason"])

	def test_availability_is_computed_even_when_the_list_filtered_that_cabin_out(self):
		"""The field is computed for the unit itself rather than looked up in the
		list beside it — that list drops full and condemned units by default, so
		a lookup there would report every full cabin as available."""
		self._stay(self.second, "2025-06-01", "2025-10-15")
		frappe.db.set_value("Housing Unit", self.second, "condition", "Uninhabitable")
		self.be()
		answer = mobile_api.list_available_housing(employee=WORKER_EMPLOYEE)
		self.assertNotIn(self.second, {row["name"] for row in answer["units"]})
		self.assertEqual(answer["previous_assignment"]["unit"], self.second)
		self.assertFalse(answer["previous_assignment"]["available"])

	def test_somebody_still_housed_is_told_so_rather_than_offered_their_own_bed(self):
		"""An open assignment means they are housed RIGHT NOW. Offering "last
		year: Cabin 7" to somebody currently in Cabin 7 is an offer to
		double-book them."""
		self.be()
		mobile_api.assign_housing(
			employee=WORKER_EMPLOYEE, housing_unit=self.second, check_in_date="2026-06-01"
		)
		previous = self._previous()
		self.assertTrue(previous["currently_housed"])
		self.assertFalse(previous["available"])
		self.assertIsNone(previous["check_out_date"])
		self.assertIn("where they are housed now", previous["unavailable_reason"])

	def test_an_open_stay_wins_over_a_finished_one_however_many_seasons_deep(self):
		"""Somebody who had MC-Cabin-07 last season and is in MC-Cabin-01 tonight
		has BOTH. Answering with last year's cabin would offer a one-tap
		re-assignment to a person who already has a bed, so the open row wins —
		"they are already housed" is true regardless of the history behind it."""
		self._stay(self.second, "2025-06-01", "2025-10-15")
		self.be()
		mobile_api.assign_housing(
			employee=WORKER_EMPLOYEE, housing_unit=self.unit, check_in_date="2026-06-01"
		)

		previous = self._previous()
		self.assertTrue(previous["currently_housed"])
		self.assertEqual(previous["unit"], self.unit)
		self.assertFalse(previous["available"])

	# ── the gate ────────────────────────────────────────────────────────────
	def test_a_field_worker_may_read_vacancies_and_may_not_name_a_person(self):
		"""The split this endpoint is built on, asserted in one test: the same
		caller, the same method, one argument apart."""
		self._stay(self.second, "2025-06-01", "2025-10-15")
		set_roles(WORKER, ["Field Worker"])
		self.be()

		self.assertTrue(mobile_api.list_available_housing()["units"])
		with self.assertRaises(Exception) as caught:
			mobile_api.list_available_housing(employee=WORKER_EMPLOYEE)
		self.assertIn("may not bring a person onto the farm", str(caught.exception))

	def test_and_a_foreman_may_name_one_because_that_is_the_returning_pickers_tap(self):
		"""v0.94.0. The whole value of the `employee` argument is offering "Last
		year: MC-Cabin-07" at the top of the list — on the phone of the person
		actually walking the returning picker to a cabin. The split the class is
		named for survives: the vacancy read stays open because it names nobody,
		and naming somebody still takes a gate."""
		self._stay(self.second, "2025-06-01", "2025-10-15")
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.be()
		self.assertIn("units", mobile_api.list_available_housing(employee=WORKER_EMPLOYEE))

	def test_an_employee_of_another_entity_is_not_found(self):
		self.be()
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.list_available_housing(employee=OUTSIDER_EMPLOYEE)

	def test_a_cabin_of_an_entity_this_phone_cannot_reach_is_not_reported(self):
		"""`guard.scoped` cannot do this one — a Housing Unit calls its company
		`owning_entity` — so the check is made by hand and this is the test that
		it is made at all."""
		self._stay(self.second, "2025-06-01", "2025-10-15")
		frappe.db.set_value("Housing Unit", self.second, "owning_entity", OTHER)
		self.be()
		self.assertIsNone(self._previous())

	def test_naming_a_person_still_leaves_one_audit_row_like_any_other_call(self):
		self._stay(self.second, "2025-06-01", "2025-10-15")
		self.be()
		before = len(self.audit_rows("list_available_housing"))
		mobile_api.list_available_housing(employee=WORKER_EMPLOYEE)
		self.assertEqual(len(self.audit_rows("list_available_housing")), before + 1)

	# ── it composes with the branch filter ──────────────────────────────────
	def test_it_comes_back_alongside_a_branch_filtered_list(self):
		"""The two the wizard uses together: the camp's cabins, and the one this
		person had last year."""
		STORE.seed("Branch", [{"name": "Mill Creek Camp", "branch": "Mill Creek Camp"}])
		self.configure(enabled=1, public_url="https://umbrel.tail4a2b.ts.net", **ON, allow_update_parcel=1)
		self.tool_data("update_parcel", {"parcel": "Mill Creek", "branch": "Mill Creek Camp"})
		self._stay(self.second, "2025-06-01", "2025-10-15")

		self.be()
		answer = mobile_api.list_available_housing(branch="Mill Creek Camp", employee=WORKER_EMPLOYEE)
		self.assertTrue(answer["branch_filter_applied"])
		self.assertEqual(answer["previous_assignment"]["unit"], self.second)
		self.assertIn(self.second, {row["name"] for row in answer["units"]})


# ── 14. universal_scan ──────────────────────────────────────────────────────
class TheScannerScreenHasOneCall(MobileAPITestCase):
	"""v0.65.0. The route behind "point the camera at it and see what it is".

	The cascade itself is `test_universal_scan.py`'s subject. What is asserted
	HERE is only what this transport adds: the gates, the entity scoping, the
	audit row, and the argument spellings — because this door's own filter keeps
	only the keys the signature declares, and a scan posted under a name the
	signature does not carry arrives empty.
	"""

	VALVE = "MC-Valve-05"

	def setUp(self):
		super().setUp()
		self.configure(
			enabled=1,
			public_url="https://umbrel.tail4a2b.ts.net",
			**ON,
			allow_register_asset=1,
			allow_universal_scan=1,
		)

	def a_valve(self, name=VALVE, company=MAIN):
		return self.tool_data(
			"register_asset", {"name": name, "asset_type": "Irrigation Valve", "company": company}
		)["name"]

	def test_a_tag_resolves_and_the_scan_is_recorded_against_the_caller(self):
		self.a_valve()
		self.be()
		answer = mobile_api.universal_scan(content=self.VALVE)
		self.assertEqual(answer["entity_type"], "asset")
		self.assertEqual(answer["entity_name"], self.VALVE)
		self.assertTrue(answer["scan_recorded"])
		self.assertEqual(frappe.db.get_value("Asset Register", self.VALVE, "last_scan_by"), WORKER)

	def test_a_stranger_comes_back_as_unknown_rather_than_as_an_error(self):
		self.be()
		answer = mobile_api.universal_scan(content="0123456789012")
		self.assertEqual(answer["entity_type"], "unknown")
		self.assertEqual(answer["available_actions"], ["create_task"])
		self.assertFalse(answer["scan_recorded"])

	def test_every_spelling_of_the_scan_argument_reaches_the_tool(self):
		"""`bind` drops what a signature does not name, so a handset posting
		`code` at a method declaring only `content` would be told the field is
		required while holding a perfectly good scan."""
		self.a_valve()
		for key in ("content", "scan", "raw", "code"):
			with self.subTest(argument=key):
				self.be()
				answer = mobile_api.universal_scan(**{key: self.VALVE})
				self.assertEqual(answer["entity_name"], self.VALVE)

	def test_an_empty_scan_is_refused_before_anything_is_read(self):
		self.be()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.universal_scan(content="   ")
		self.assertIn("content is required", str(caught.exception))

	def test_guest_is_refused_like_every_other_method_here(self):
		self.be("Guest")
		with self.assertRaises(frappe.PermissionError):
			mobile_api.universal_scan(content=self.VALVE)

	def test_a_tag_belonging_to_another_entity_is_not_scanned(self):
		"""The scoping that matters on a scan: a phone cannot stamp, or read,
		the register of a farm this account was never given."""
		self.a_valve(name="SEL-Valve-01", company=OTHER)
		self.be()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.universal_scan(content="SEL-Valve-01")
		self.assertIn(OTHER, str(caught.exception))
		self.assertFalse(frappe.db.get_value("Asset Register", "SEL-Valve-01", "last_scan_at"))

	def test_the_company_argument_may_narrow_and_may_not_widen(self):
		self.a_valve()
		self.be()
		self.assertEqual(mobile_api.universal_scan(content=self.VALVE, company=MAIN)["entity_type"], "asset")
		with self.assertRaises(frappe.PermissionError):
			mobile_api.universal_scan(content=self.VALVE, company=OTHER)

	# ── the audit row ───────────────────────────────────────────────────────
	def test_one_audit_row_lands_for_the_scan(self):
		self.a_valve()
		self.be()
		before = len(self.audit_rows("universal_scan"))
		mobile_api.universal_scan(content=self.VALVE)
		self.assertEqual(len(self.audit_rows("universal_scan")), before + 1)

	def test_a_login_payload_scanned_by_mistake_is_refused_and_redacted(self):
		"""The one scan that must not be echoed — and the audit row is where it
		would have been kept. `guard.redact_payloads` is what stops that, and this
		is the test that both halves hold on this route."""
		payload = '{"url":"https://x","api_key":"k-abc","api_secret":"s3cr3t-nobody-should-see"}'
		self.be()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.universal_scan(content=payload)
		self.assertIn("credential document", str(caught.exception))
		self.assertNotIn("s3cr3t-nobody-should-see", str(caught.exception))
		row = self.audit_rows("universal_scan")[-1]
		self.assertNotIn("s3cr3t-nobody-should-see", json.dumps(row))


# ── receipt capture (v0.67.0) ───────────────────────────────────────────────
class ReceiptCaptureFromAPhone(MobileAPITestCase):
	"""The four routes Sprint 2 publishes, and the three arguments they refuse.

	The interesting assertions here are not that the endpoints work — the tools
	are tested in `test_receipts.py` and `test_expenses.py`. They are that this
	transport does not hand a phone anything the tool would otherwise accept:
	`submitted_by` comes from the authenticated account, the company comes from
	the caller's scope, and there is no way to submit a scale ticket at all.
	"""

	PACKER = "Blue Ridge Packing"

	def setUp(self):
		super().setUp()
		STORE.seed(
			"Customer",
			[{"name": self.PACKER, "customer_name": self.PACKER, "customer_group": "Packers"}],
		)
		self.be()

	def ticket(self, **overrides):
		payload = {
			"ticket_number": "44718",
			"date": "2026-09-14",
			"customer": self.PACKER,
			"gross_weight": 18400,
			"tare_weight": 6200,
			"weight_uom": "Lb",
		}
		payload.update(overrides)
		return mobile_api.create_scale_ticket(**payload)

	# ── classify_receipt ────────────────────────────────────────────────────
	def test_the_classifier_answers_and_shows_its_working(self):
		data = mobile_api.classify_receipt(text="SCALE TICKET 44718 GROSS WT 18400 TARE WT 6200")
		self.assertEqual(data["receipt_type"], "scale_ticket")
		self.assertIn("scale ticket", data["matched_signals"])

	def test_the_classifier_still_needs_an_enrolled_credential(self):
		self.be("Guest")
		with self.assertRaises(frappe.PermissionError):
			mobile_api.classify_receipt(text="anything")

	# ── create_scale_ticket ─────────────────────────────────────────────────
	def test_a_ticket_captured_from_a_phone_arrives_as_a_draft(self):
		data = self.ticket()
		self.assertEqual(data["status"], "Draft")
		self.assertEqual(data["docstatus"], 0)
		self.assertEqual(data["net_weight"], 12200.0)

	def test_the_company_comes_from_the_callers_scope_when_none_is_sent(self):
		self.assertEqual(self.ticket()["company"], MAIN)

	def test_a_company_this_account_cannot_reach_is_refused_not_quietly_swapped(self):
		with self.assertRaises(frappe.PermissionError):
			self.ticket(company=OTHER)

	def test_there_is_no_way_to_submit_a_ticket_from_a_phone(self):
		"""Submitting freezes a third party's weight record. The tool exists for
		somebody who can see the settlement it will be checked against."""
		self.assertFalse(hasattr(mobile_api, "submit_scale_ticket"))

	def test_a_phone_cannot_assert_a_net_weight(self):
		"""Not by being refused — by the argument not existing. `bind` keeps only
		the keys a signature declares, so a body carrying one is dropped."""
		self.assertNotIn("net_weight", inspect.signature(mobile_api.create_scale_ticket).parameters)

	# ── list_scale_tickets ──────────────────────────────────────────────────
	def test_the_back_button_list_shows_what_this_crew_just_filed(self):
		self.ticket()
		self.ticket(ticket_number="44719")
		data = mobile_api.list_scale_tickets()
		self.assertEqual(data["count"], 2)
		self.assertEqual(data["total_net_weight"], 24400.0)

	def test_the_list_is_scoped_to_the_callers_entities(self):
		"""A ticket filed by MAIN's crew is not another entity's to read, and the
		filter runs twice — once in the tool, once on the way out."""
		self.ticket()
		self.enrol(email=OUTSIDER, name="Ben Ortiz", entities=[OTHER])
		self.be(OUTSIDER)
		self.assertEqual(mobile_api.list_scale_tickets()["count"], 0)

	def test_the_per_unit_count_survives_the_trip_to_the_phone(self):
		self.ticket()
		self.ticket(ticket_number="44719", weight_uom="Bin", gross_weight=40, tare_weight=0)
		self.assertEqual(mobile_api.list_scale_tickets()["by_weight_uom"], {"Lb": 1, "Bin": 1})

	# ── create_expense_receipt ──────────────────────────────────────────────
	def receipt(self, **overrides):
		payload = {
			"merchant": "Valley Co-op Fuel",
			"amount": 184.62,
			"receipt_date": "2026-06-14",
			"category": "Fuel",
		}
		payload.update(overrides)
		return mobile_api.create_expense_receipt(**payload)

	def test_a_receipt_captured_from_a_phone_is_filed_against_the_caller(self):
		self.assertEqual(self.receipt()["submitted_by"], WORKER_EMPLOYEE)

	def test_a_phone_cannot_file_an_expense_against_somebody_else(self):
		"""A reimbursement claim with the wrong person's signature on it."""
		for argument in ("submitted_by", "employee"):
			with self.subTest(argument=argument):
				self.assertNotIn(argument, inspect.signature(mobile_api.create_expense_receipt).parameters)

	def test_an_offline_queue_can_post_a_draft(self):
		self.assertEqual(self.receipt(status="Draft")["status"], "Draft")

	def test_a_phone_cannot_post_an_already_approved_receipt(self):
		"""Refused by the tool's own `CREATABLE_STATUSES`, not by this signature —
		which is the right place for it, because the MCP surface needs the same
		refusal, and `guard` turns it into the ValidationError a phone reads."""
		with self.assertRaises(frappe.ValidationError) as caught:
			self.receipt(status="Approved")
		self.assertIn("Approval and rejection are separate tools", str(caught.exception))

	def test_approval_is_not_reachable_from_a_phone_at_all(self):
		for method in ("approve_expense_receipt", "reject_expense_receipt"):
			with self.subTest(method=method):
				self.assertFalse(hasattr(mobile_api, method))

	def test_the_supplier_and_item_links_are_forwarded(self):
		STORE.seed(
			"Supplier",
			[{"name": "Valley Co-operative", "supplier_name": "Valley Co-operative"}],
		)
		STORE.seed("Item", [{"name": "HOSE-050", "item_name": "Hydraulic hose 1/2in"}])
		data = self.receipt(
			supplier="Valley Co-operative",
			items=[{"description": "HYD HOSE 1/2", "item": "HOSE-050", "quantity": 1, "unit_price": 31.25}],
		)
		self.assertEqual(data["supplier"], "Valley Co-operative")
		self.assertEqual(data["items"][0]["item"], "HOSE-050")

	def test_a_json_encoded_items_array_is_accepted(self):
		"""`URLSession` posting JSON and a multipart retry do not agree about
		nested arrays, and the intent is unambiguous either way."""
		data = self.receipt(items=json.dumps([{"description": "Nozzle", "quantity": 2, "unit_price": 4.5}]))
		self.assertEqual(data["items"][0]["line_total"], 9.0)

	def test_a_malformed_items_body_is_refused_rather_than_500ing(self):
		with self.assertRaises(frappe.ValidationError):
			self.receipt(items="not json at all")

	def test_settlements_are_not_reachable_from_a_phone(self):
		"""A settlement is a multi-page document that arrives at an office, not a
		thing anybody photographs at a tailgate."""
		for method in ("create_settlement_statement", "submit_settlement_statement"):
			with self.subTest(method=method):
				self.assertFalse(hasattr(mobile_api, method))


# ── the timer the handset stops ─────────────────────────────────────────────
class TheFinishingTimestampsReachThePhone(MobileAPITestCase):
	"""v0.76.0. `FarmTask.completedAt` and `.actualDurationMinutes` were nil.

	`complete_task_via_mobile` has written both onto the Farm Task Assignment
	since v0.16.0 and `_describe_assignment` has reported them ever since, but
	`shape.task` never carried them up onto the task — so `elapsedMinutes` on
	the handset fell through to counting from `startedAt` to NOW. A job closed
	at four in the afternoon read as eleven hours' work when somebody opened it
	the next morning. The doctype had the answer the whole time.
	"""

	def setUp(self):
		super().setUp()
		self.unit = self.a_camp()
		# A contract of findings only, so these tests turn on the TIMESTAMPS
		# rather than on filing a photograph and a signature — which
		# `TheWholeFlowWorks` already covers end to end.
		self.task = self.a_task(
			evidence_required={"findings_text": True},
			location_doctype="Housing Unit",
			location=self.unit,
		)

	def finish(self, **overrides):
		self.be()
		claimed = mobile_api.claim_task(task=self.task)
		mobile_api.start_task(task=self.task)
		payload = {"task": self.task, "findings_text": "", "clean_pass": True}
		payload.update(overrides)
		mobile_api.complete_task_via_mobile(**payload)
		return claimed

	def test_a_completed_task_comes_back_with_both(self):
		self.finish(actual_duration_minutes=22, completed_at="2026-07-24 16:00:00")
		row = mobile_api.get_task(task=self.task)

		self.assertEqual(row["actual_duration_minutes"], 22)
		self.assertTrue(row["completed_at"], "the app has nothing to stop its timer against")
		self.assertIn("2026-07-24 16:00:00", str(row["completed_at"]))

	def test_the_keys_are_present_on_a_task_nobody_has_finished(self):
		"""Always emitted, like `claimed_at` and `started_at`. A client that has
		to test for the key before reading it has two paths where it needs one."""
		self.be()
		row = mobile_api.get_task(task=self.task)
		self.assertIn("completed_at", row)
		self.assertIn("actual_duration_minutes", row)
		self.assertIsNone(row["completed_at"])
		self.assertIsNone(row["actual_duration_minutes"])

	def test_a_finished_task_still_carries_the_time_it_was_started(self):
		"""`live_assignment` is Claimed-or-In-Progress by definition, so a
		completed task used to come back with every assignment field null —
		which is the same open-ended timer by another route."""
		self.finish(actual_duration_minutes=22)
		row = mobile_api.get_task(task=self.task)

		self.assertTrue(row["started_at"])
		self.assertTrue(row["claimed_at"])
		self.assertTrue(row["assignment"])

	def test_a_task_in_progress_is_still_shaped_against_the_live_assignment(self):
		"""The live one wins. Only a task with none falls through to history."""
		self.be()
		claimed = mobile_api.claim_task(task=self.task)
		mobile_api.start_task(task=self.task)
		row = mobile_api.get_task(task=self.task)

		self.assertEqual(row["assignment"], claimed["assignment"])
		self.assertIsNone(row["completed_at"])

	def test_a_rejected_assignment_is_not_used_to_shape_the_task(self):
		"""A worker who handed the job back has a `started_at` and no
		completion, and shaping against theirs is the open-ended timer again."""
		self.be()
		mobile_api.claim_task(task=self.task)
		mobile_api.start_task(task=self.task)
		mobile_api.reject_task(task=self.task, reason="the ladder is broken")
		row = mobile_api.get_task(task=self.task)

		self.assertIsNone(row["completed_at"])
		self.assertIsNone(row["started_at"])

	def test_my_tasks_is_shaped_by_the_same_function_and_so_carries_them_too(self):
		"""One fix serves both reads, asserted rather than assumed.

		`list_my_tasks` publishes what a worker is HOLDING — claimed and in
		progress — so a finished task is not in it to assert against over the
		transport. What both reads share is `shape.task`, and this drives it with
		the assignment shape `list_my_tasks` passes it.
		"""
		rows = shape.tasks(
			[{"name": self.task, "task_name": "Habitability walk"}],
			{self.task: {"completed_at": "2026-07-24 16:00:00", "actual_duration_minutes": 22}},
		)
		self.assertEqual(rows[0]["completed_at"], "2026-07-24 16:00:00")
		self.assertEqual(rows[0]["actual_duration_minutes"], 22)

	def test_a_duration_the_handset_sent_as_a_string_still_decodes_as_a_number(self):
		"""`API_CONTRACT.md` §"Client tolerance" runs both ways: a number that
		arrived as text must not leave as text and land in an Int decoder."""
		row = shape.task({}, {"actual_duration_minutes": "22"})
		self.assertEqual(row["actual_duration_minutes"], 22)

	def test_the_completion_is_the_one_the_handset_sent(self):
		"""Not the row's creation time: a queued completion reaching the server
		an hour late must not report the hour as work."""
		self.finish(completed_at="2026-07-24 16:00:00", actual_duration_minutes=22)
		row = mobile_api.get_task(task=self.task)
		assignment = frappe.db.get_value("Farm Task Assignment", row["assignment"], "completed_at")
		self.assertEqual(str(row["completed_at"]), str(assignment))


# ── which six o'clock, on a phone ───────────────────────────────────────────
class TheTaskTimesSayWhichClock(MobileAPITestCase):
	"""v0.77.0. A worker reading "finished 16:04" should not have to convert it."""

	def setUp(self):
		super().setUp()
		STORE.singles["System Settings"] = {"time_zone": "America/Los_Angeles"}
		self.unit = self.a_camp()
		self.task = self.a_task(
			evidence_required={"findings_text": True},
			location_doctype="Housing Unit",
			location=self.unit,
		)

	def finish(self):
		self.be()
		mobile_api.claim_task(task=self.task)
		mobile_api.start_task(task=self.task)
		mobile_api.complete_task_via_mobile(
			task=self.task,
			findings_text="",
			clean_pass=True,
			completed_at="2026-07-24 16:04:00",
			actual_duration_minutes=22,
		)

	def test_a_completed_task_carries_the_offset_beside_the_stored_value(self):
		self.finish()
		row = mobile_api.get_task(task=self.task)
		self.assertEqual(row["completed_at"], "2026-07-24 16:04:00")
		self.assertEqual(row["completed_at_local"], "2026-07-24T16:04:00.000-07:00")
		self.assertEqual(row["timezone"], "America/Los_Angeles")

	def test_the_claim_and_start_times_get_the_same_treatment(self):
		self.finish()
		row = mobile_api.get_task(task=self.task)
		self.assertTrue(row["claimed_at_local"].endswith(("-07:00", "-08:00")))
		self.assertTrue(row["started_at_local"].endswith(("-07:00", "-08:00")))

	def test_a_requested_zone_moves_the_clock_on_the_read(self):
		self.finish()
		row = mobile_api.get_task(task=self.task, timezone="America/Denver")
		self.assertEqual(row["timezone"], "America/Denver")
		self.assertEqual(row["completed_at_local"], "2026-07-24T17:04:00.000-06:00")
		self.assertEqual(row["completed_at"], "2026-07-24 16:04:00")

	def test_an_unknown_zone_is_refused_rather_than_answered_in_utc(self):
		self.be()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.get_task(task=self.task, timezone="America/Yakima")
		self.assertIn("America/Yakima", str(caught.exception))

	def test_my_tasks_carries_the_block_once_rather_than_per_task(self):
		"""Three keys on each of forty tasks is forty copies of one fact."""
		self.be()
		mobile_api.claim_task(task=self.task)
		found = mobile_api.list_my_tasks()
		self.assertEqual(found["timezone"], "America/Los_Angeles")
		self.assertTrue(found["tasks"])
		for row in found["tasks"]:
			self.assertNotIn("timezone", row)
			self.assertIn("claimed_at_local", row)

	def test_the_milliseconds_survive_because_the_handset_needs_them(self):
		"""`FrappeDate.parse` uses ISO8601DateFormatter with
		`.withFractionalSeconds`, which is a requirement and not a tolerance."""
		self.finish()
		row = mobile_api.get_task(task=self.task)
		self.assertRegex(row["completed_at_local"], r"\.\d{3}[+-]\d{2}:\d{2}$")


class TheHandsetConfirmsMovementAndNothingElse(MobileAPITestCase):
	"""v0.80.0. The five trade-documentation routes.

	THE INTERESTING ONE IS WHAT IS NOT PUBLISHED. `update_shipment_status` can
	release a shipment to Ready to Ship — the module's one gate — can cancel one,
	and takes an `override_reason` that walks past an incomplete document
	checklist. None of the three is reachable from a phone.

	A release is an assertion that the paperwork is in order, made by somebody
	with a trade role at a desk. An account that could make it from a yard would
	make the gate worth nothing. A driver saying "I have left" and "I have
	arrived" is a different act and it is the only one here — which is why the
	wrapper is named `confirm_shipment_movement` rather than after the tool it
	delegates to.
	"""

	def setUp(self):
		super().setUp()
		from erpnext_mcp.tools import shipments

		self.trade = shipments
		shipments.install_trade_documents()
		self.shipment = shipments.create_shipment({"destination_tier": "Local", "company": MAIN}).data[
			"shipment"
		]

	def _released(self):
		self.trade.update_shipment_status({"shipment": self.shipment, "status": "Ready to Ship"})

	def test_a_driver_can_read_what_is_going_out(self):
		self.be()
		answer = mobile_api.list_shipments()
		self.assertEqual(answer["count"], 1)
		self.assertEqual(answer["shipments"][0]["name"], self.shipment)

	def test_a_driver_can_read_what_is_still_missing(self):
		"""The question somebody standing next to a truck actually has."""
		self.be()
		answer = mobile_api.get_shipment_readiness(shipment=self.shipment)
		self.assertFalse(answer["ready"])
		self.assertEqual(len(answer["blocking"]), 4)

	def test_a_driver_can_read_the_paperwork_on_a_load(self):
		self.be()
		answer = mobile_api.list_trade_documents(shipment=self.shipment)
		self.assertEqual(answer["count"], 0)

	def test_departed_and_delivered_are_the_two_words_it_takes(self):
		self._released()
		self.be()
		moved = mobile_api.confirm_shipment_movement(shipment=self.shipment, movement="departed")
		self.assertEqual(moved["status"], "In Transit")
		arrived = mobile_api.confirm_shipment_movement(shipment=self.shipment, movement="delivered")
		self.assertEqual(arrived["status"], "Delivered")

	def test_a_phone_cannot_release_a_shipment(self):
		"""The gate would be worth nothing if a yard could open it."""
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.confirm_shipment_movement(shipment=self.shipment, movement="Ready to Ship")
		self.assertIn("desk acts", str(caught.exception))

	def test_a_phone_cannot_cancel_a_shipment(self):
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.confirm_shipment_movement(shipment=self.shipment, movement="cancelled")
		self.assertIn("desk acts", str(caught.exception))

	def test_an_override_reason_is_not_in_the_signature_so_it_cannot_be_sent(self):
		"""Frappe binds body keys that match the signature; a key that is not
		there cannot reach the tool."""
		import inspect

		signature = inspect.signature(mobile_api.confirm_shipment_movement)
		self.assertNotIn("override_reason", signature.parameters)
		self.assertNotIn("status", signature.parameters)

	def test_another_entitys_shipment_is_not_reachable(self):
		other = self.trade.create_shipment({"destination_tier": "Local", "company": OTHER}).data["shipment"]
		self.be()
		with self.assertRaises(Exception):
			mobile_api.get_shipment(shipment=other)

	def test_the_register_is_scoped_to_what_this_caller_may_reach(self):
		self.trade.create_shipment({"destination_tier": "Local", "company": OTHER})
		self.be()
		answer = mobile_api.list_shipments()
		self.assertEqual([row["name"] for row in answer["shipments"]], [self.shipment])


class TheShiftTimelineReachesThePhone(MobileAPITestCase):
	"""The three shift tools that had a switch, a doctype and no route.

	`log_shift_event` has existed since v0.19.3, the location pair since v0.32.0
	and the crew timeline since v0.64.0, and every one of them was reachable only
	from a Desk. For the compliance timeline that is the same as not existing: OAR
	437-004-1131 asks what happened DURING the shift, and a water break typed into
	a browser that evening is the record an investigator discounts.
	"""

	def setUp(self):
		super().setUp()
		from erpnext_mcp import compliance_fields

		install_hrms()
		compliance_fields.install_compliance_fields(respect_switch=False)
		# The supervisor's own role, which is the other half of this release: a
		# Foreman is who -1131 names, and until now the tools refused them —
		# `require_hr_role` would have needed Ana made a Farm Manager, which is a
		# very different set of keys to hand somebody who runs a crew.
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.be()
		self.shift = mobile_api.start_shift(location="Block 7 North", shift_type="Harvest", company=MAIN)[
			"name"
		]

	def test_a_water_break_is_logged_from_the_block_it_happened_on(self):
		answer = mobile_api.log_shift_event(shift=self.shift, event_type="Water Break")
		self.assertEqual(answer["logged"]["event_type"], "Water Break")
		self.assertEqual(answer["events_of_this_type"], 1)

	def test_the_producer_reference_is_not_in_the_signature_so_it_cannot_be_sent(self):
		"""It points one compliance record at another and is how a packet builder
		follows a trail. A body that could set it could file an event as the
		product of a record it had nothing to do with."""
		signature = inspect.signature(mobile_api.log_shift_event)
		self.assertNotIn("producer_record_doctype", signature.parameters)
		self.assertNotIn("producer_record_name", signature.parameters)

	def test_the_crew_timeline_answers_for_one_worker_and_not_for_the_shift(self):
		mobile_api.add_worker_to_shift(shift=self.shift, employee=WORKER_EMPLOYEE)
		answer = mobile_api.get_shift_crew_timeline(shift=self.shift)
		self.assertEqual([row["employee"] for row in answer["crew"]], [WORKER_EMPLOYEE])

	def test_a_track_is_read_even_where_no_fix_has_been_posted(self):
		"""An empty track is an answer — 'nobody logged one' — and the close
		screen has to be able to draw it."""
		answer = mobile_api.get_shift_track(shift=self.shift)
		self.assertEqual(answer["count"], 0)
		self.assertFalse(answer["truncated"])

	def test_a_fix_is_logged_and_read_back_on_the_track(self):
		"""`lat`/`lon` as well as the full spellings, because that is what a
		phone's location API calls them."""
		answer = mobile_api.log_shift_location(shift=self.shift, lat=45.52, lon=-122.68)
		self.assertEqual(answer["logs_on_this_shift"], 1)
		self.assertEqual(mobile_api.get_shift_track(shift=self.shift)["count"], 1)

	def test_the_route_does_not_consult_the_per_tool_switch(self):
		"""LIVE, like every other method on this transport. The `allow_` switches
		govern the AI surface; the gates that hold here are `guard`'s four, and
		what bounds a crew track is the grant an operator issues per person."""
		self.configure(enabled=1, **ON, allow_log_shift_location=0)
		self.assertEqual(
			mobile_api.log_shift_location(shift=self.shift, lat=45.52, lon=-122.68)["logs_on_this_shift"],
			1,
		)

	def test_another_entitys_shift_is_not_reachable_by_any_of_them(self):
		# Formed by an unrestricted account, because the point of the assertion
		# is what Ana's credential can REACH rather than what it can create.
		# `caller_identity` is what the tool layer scopes on, and `be()` captured
		# Ana into it — so both have to be put back for this one call.
		self.request({}, headers={})
		frappe.local.session.user = "Administrator"
		other = shift_tools.start_shift(
			{"foreman": OUTSIDER_EMPLOYEE, "location": "Elsewhere", "company": OTHER}
		).data["name"]
		self.be()
		for method, arguments in (
			("log_shift_event", {"event_type": "Water Break"}),
			("get_shift_track", {}),
			("get_shift_crew_timeline", {}),
		):
			with self.subTest(method=method):
				with self.assertRaises(Exception):
					getattr(mobile_api, method)(shift=other, **arguments)


class TheShadowFeedIsAddressed(MobileAPITestCase):
	"""v0.91.0. The three shadow log routes, and the one property that gates them.

	THESE THREE WERE MCP TOOLS WITH NO ROUTE, which is the failure shape v0.58.1
	spent a release on: the server could do it and the phone could not ask. The
	tools have existed since v0.85.0.

	THE FEED IS ADDRESSED, NOT PUBLISHED. Every row names a `recipient_employee`
	— it is one supervisor's copy of what happened below them, and the tool's
	`employee` filter takes that recipient as an argument. None of the three
	wrappers declares it, so the recipient is always the authenticated caller.

	AND THE DOCNAME IS COMPOSABLE, which is why scope alone is not the gate on
	the detail pair. `shadow_key` is built from the event, the source and the
	recipient's own Employee ID, so a worker who knows a colleague's number can
	WRITE a docname rather than discover one. Both entries in this fixture sit in
	MAIN, so `guard.require_scoped_doc` passes on both and the addressee check is
	the only thing standing between Ana and Luis's feed.

	EVERY TEST HERE INVOKES THE WRAPPER. That is the lesson of bd66550: the
	pause pair were listed in this file's registry set, asserted to be published,
	and never once called — so a four-argument call to a three-argument helper
	shipped and answered 500 to every phone on the farm.
	"""

	SUBORDINATE = "EMP-CARL"
	COLLEAGUE = "EMP-LUIS"

	def setUp(self):
		super().setUp()
		from erpnext_mcp.tools import shadow_log

		self.shadow_log = shadow_log
		# Carl reports to Ana (the caller), and to Luis in the same entity.
		# Luis's copies are the ones Ana must not be able to read.
		STORE.seed(
			"Employee",
			[
				{
					"name": self.COLLEAGUE,
					"employee_name": "Luis Ortega",
					"company": MAIN,
					"status": "Active",
				},
				{
					"name": self.SUBORDINATE,
					"employee_name": "Carl Mendez",
					"company": MAIN,
					"status": "Active",
					"reports_to": WORKER_EMPLOYEE,
				},
			],
		)

	def raise_for(self, subject=None, source_name="BLS-0001", **overrides):
		payload = {
			"event_type": self.shadow_log.EVENT_BUCKET_SESSION,
			"source_doctype": "Bucket Log Session",
			"source_name": source_name,
			"subject_employee": subject or self.SUBORDINATE,
			"company": MAIN,
			"occurred_at": "2026-08-16 14:00:00",
			"summary": "Carl Mendez synced a picking session: 412 accepted.",
			"snapshot": {"total_accepted": 412, "employee": self.SUBORDINATE},
		}
		payload.update(overrides)
		return self.shadow_log.propagate(**payload)

	def a_copy_for_ana(self):
		"""One level-1 copy addressed to the caller, and its docname."""
		self.raise_for()
		return self.shadow_log.shadow_key(
			self.shadow_log.EVENT_BUCKET_SESSION, "Bucket Log Session", "BLS-0001", WORKER_EMPLOYEE
		)

	def a_copy_for_the_colleague(self):
		"""One level-1 copy addressed to Luis, in the caller's OWN entity.

		Same company on purpose: `guard.require_scoped_doc` passes on this row,
		so anything that refuses it is refusing on the addressee and not on scope.
		"""
		frappe.db.set_value("Employee", self.SUBORDINATE, "reports_to", self.COLLEAGUE)
		self.raise_for(source_name="BLS-0002")
		frappe.db.set_value("Employee", self.SUBORDINATE, "reports_to", WORKER_EMPLOYEE)
		return self.shadow_log.shadow_key(
			self.shadow_log.EVENT_BUCKET_SESSION, "Bucket Log Session", "BLS-0002", self.COLLEAGUE
		)

	# ── the feed reads ──────────────────────────────────────────────────────
	def test_a_supervisor_reads_their_own_feed(self):
		self.a_copy_for_ana()
		self.be()
		answer = mobile_api.list_shadow_log_entries()
		self.assertEqual(answer["count"], 1)
		self.assertEqual(answer["entries"][0]["recipient_employee"], WORKER_EMPLOYEE)
		self.assertEqual(answer["entries"][0]["subject_employee"], self.SUBORDINATE)
		self.assertEqual(answer["unacknowledged_count"], 1)

	def test_a_colleagues_copies_are_not_in_it(self):
		"""The recipient is the session's, so another supervisor's feed is not
		reachable by asking — there is nowhere to put the request."""
		self.a_copy_for_ana()
		self.a_copy_for_the_colleague()
		self.be()
		answer = mobile_api.list_shadow_log_entries()
		self.assertEqual(answer["count"], 1)
		self.assertEqual({row["recipient_employee"] for row in answer["entries"]}, {WORKER_EMPLOYEE})

	def test_the_recipient_is_not_a_body_argument_on_any_of_the_three(self):
		"""`routes.bind` keeps body keys that match the signature. A key that is
		not on it cannot be delivered, which is what makes this the caller's own
		feed rather than a register anybody may read."""
		for method in ("list_shadow_log_entries", "get_shadow_log_entry", "acknowledge_shadow_log"):
			with self.subTest(method=method):
				accepted = farmops_routes.accepted_arguments(getattr(mobile_api, method))
				self.assertNotIn("employee", accepted)
				self.assertNotIn("recipient_employee", accepted)

	def test_an_empty_feed_is_a_real_answer_and_not_an_error(self):
		"""A handset that drew this as a fault would send foremen looking for
		one that is not there."""
		self.be()
		answer = mobile_api.list_shadow_log_entries()
		self.assertEqual(answer["count"], 0)
		self.assertIn("EMPTY FEED IS A REAL ANSWER", answer["empty_note"])

	def test_the_unread_filter_is_the_call_the_badge_makes(self):
		name = self.a_copy_for_ana()
		self.be()
		self.assertEqual(mobile_api.list_shadow_log_entries(acknowledged=False)["count"], 1)
		mobile_api.acknowledge_shadow_log(name=name)
		self.assertEqual(mobile_api.list_shadow_log_entries(acknowledged=False)["count"], 0)
		self.assertEqual(mobile_api.list_shadow_log_entries(acknowledged=True)["count"], 1)

	def test_a_bad_event_type_is_refused_by_name(self):
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.list_shadow_log_entries(event_type="Something Else")
		self.assertIn("is not an event this feed carries", str(caught.exception))

	# ── the detail read ─────────────────────────────────────────────────────
	def test_one_copy_comes_back_with_its_frozen_snapshot(self):
		name = self.a_copy_for_ana()
		self.be()
		answer = mobile_api.get_shadow_log_entry(name=name)
		self.assertEqual(answer["recipient_employee"], WORKER_EMPLOYEE)
		self.assertEqual(answer["snapshot"]["total_accepted"], 412)
		self.assertTrue(answer["snapshot_intact"])

	def test_a_colleagues_copy_reads_as_not_found_though_it_is_in_scope(self):
		"""THE ASSERTION THIS WRAPPER EXISTS FOR. Both rows are in MAIN, so
		`guard.require_scoped_doc` passes on both; the addressee check is the
		only thing left. And the refusal is a MISS rather than a permission
		error, because a composed docname that drew a different error from a
		nonexistent one would have learned the row is there."""
		name = self.a_copy_for_the_colleague()
		self.assertEqual(
			frappe.db.get_value("Shadow Log Entry", name, "company"), MAIN, "fixture must be in scope"
		)
		self.be()
		with self.assertRaises(frappe.DoesNotExistError) as caught:
			mobile_api.get_shadow_log_entry(name=name)
		self.assertIn("was not found", str(caught.exception))

	def test_a_composed_docname_that_never_existed_reads_the_same_way(self):
		"""The two refusals are worded identically, so probing learns nothing."""
		self.be()
		with self.assertRaises(frappe.DoesNotExistError) as caught:
			mobile_api.get_shadow_log_entry(
				name=self.shadow_log.shadow_key(
					self.shadow_log.EVENT_SHIFT_CLOSED, "Farm Shift", "SHIFT-NOPE", self.COLLEAGUE
				)
			)
		self.assertIn("was not found", str(caught.exception))

	# ── the acknowledgement ─────────────────────────────────────────────────
	def test_the_recipient_can_say_they_saw_it(self):
		name = self.a_copy_for_ana()
		self.be()
		answer = mobile_api.acknowledge_shadow_log(name=name, note="Spoke to Carl.")
		self.assertTrue(answer["acknowledged"])
		self.assertEqual(answer["acknowledged_by"], WORKER)
		self.assertEqual(answer["acknowledged_note"], "Spoke to Carl.")
		self.assertFalse(answer["x_idempotent"])

	def test_a_second_call_changes_nothing_and_says_so(self):
		"""A phone that lost its response in a dead spot must not have to choose
		between retrying and being correct."""
		name = self.a_copy_for_ana()
		self.be()
		mobile_api.acknowledge_shadow_log(name=name)
		again = mobile_api.acknowledge_shadow_log(name=name)
		self.assertTrue(again["acknowledged"])
		self.assertTrue(again["x_idempotent"])

	def test_nobody_acknowledges_a_copy_addressed_to_somebody_else(self):
		"""'I saw this' is a statement about oneself. An account that could make
		it on another person's behalf could clear a supervisor's unread feed from
		across the farm and leave the record asserting they had read it."""
		name = self.a_copy_for_the_colleague()
		self.be()
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.acknowledge_shadow_log(name=name)
		self.assertFalse(
			compat.checked(frappe.db.get_value("Shadow Log Entry", name, "acknowledged")),
			"the colleague's copy must still read as unacknowledged",
		)

	def test_the_write_is_declared_mutating_and_the_reads_are_not(self):
		"""The route table reads this off the endpoint rather than restating it,
		so the two cannot come to disagree about whether a call writes."""
		self.assertTrue(mobile_api.acknowledge_shadow_log.farm_ops_mutating)
		self.assertFalse(mobile_api.list_shadow_log_entries.farm_ops_mutating)
		self.assertFalse(mobile_api.get_shadow_log_entry.farm_ops_mutating)
		by_path = {route.path: route for route in farmops_routes.ROUTES}
		self.assertTrue(by_path["/mobile/acknowledge_shadow_log"].mutating)
		self.assertFalse(by_path["/mobile/list_shadow_log_entries"].mutating)

	def test_all_three_routes_exist_which_is_the_whole_point_of_the_release(self):
		"""They answered 404 on this transport for six releases."""
		for path in (
			"/mobile/list_shadow_log_entries",
			"/mobile/get_shadow_log_entry",
			"/mobile/acknowledge_shadow_log",
		):
			with self.subTest(path=path):
				self.assertIn(path, farmops_routes.BY_PATH)

	def test_the_routes_do_not_consult_the_per_tool_switch(self):
		"""LIVE, like every other method here. The `allow_` switches govern the
		AI surface; what bounds this one is the grant an operator issues."""
		name = self.a_copy_for_ana()
		self.configure(enabled=1, **ON, allow_list_shadow_log_entries=0, allow_acknowledge_shadow_log=0)
		self.be()
		self.assertEqual(mobile_api.list_shadow_log_entries()["count"], 1)
		self.assertTrue(mobile_api.acknowledge_shadow_log(name=name)["acknowledged"])

	def test_a_login_with_no_employee_record_is_told_how_to_fix_it(self):
		"""The feed is addressed to an Employee, so a login that resolves to none
		has no feed rather than an empty one."""
		frappe.db.set_value("Employee", WORKER_EMPLOYEE, "user_id", "")
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.list_shadow_log_entries()
		self.assertIn("has no Employee record", str(caught.exception))


class TheInventoryTabReachesTheServer(MobileAPITestCase):
	"""v0.91.0. The five stock routes, and the entity filter on each shape.

	THE TOOLS HAVE EXISTED SINCE v0.69.0 AND NONE OF THEM HAD A ROUTE. Every
	screen under `FarmOps/Features/Inventory` shipped against a contract whose
	server half was never mounted, so all four showed the sidecar's own "is not
	a Farm Ops API method" 404 in an error banner.

	THE SCOPING IS DIFFERENT ON EVERY ONE OF THE THREE ROW SHAPES, which is what
	these tests are really about. `get_stock_balance` rows carry `company` and
	`guard.scoped` handles them; `get_warehouse_summary` is one shed whose rows
	carry no company at all; `get_stock_ledger` and `list_reorder_alerts` carry
	a warehouse and no company, and the ledger tool TAKES NO COMPANY ARGUMENT —
	there is no filter to ask it for, so the wrapper resolves the caller's
	entities to a warehouse set itself.

	THE FIXTURE IS BUILT FOR EXACTLY THIS. SPRAY sits in two MAIN warehouses
	(80 at STORES, 45 at SHOP) and one OTHER warehouse (500 at OTHER_STORES),
	and Ana is scoped to MAIN alone. So 125 is the right answer and 625 is the
	leak, on every read below.
	"""

	def setUp(self):
		super().setUp()
		seed_masters()
		seed_stock()

	# ── get_stock_balance ───────────────────────────────────────────────────
	def test_a_balance_covers_the_callers_entities_and_no_others(self):
		self.be()
		answer = mobile_api.get_stock_balance(item_code=SPRAY)
		self.assertEqual({row["warehouse"] for row in answer["balances"]}, {STORES, SHOP})
		self.assertEqual(answer["warehouse_count"], 2)

	def test_the_balance_totals_are_recomputed_after_the_filter(self):
		"""THE LEAK THAT SURVIVES ITS OWN ROWS. The tool sums before the wrapper
		drops anything, so passing its totals through would report the other
		entity's 500 units as a number after its row had gone — the whole point
		of scoping defeated by an aggregate nobody looked at."""
		self.be()
		answer = mobile_api.get_stock_balance(item_code=SPRAY)
		self.assertEqual(answer["total_qty"], 125.0)
		self.assertEqual(answer["total_value"], 312.5)

	def test_naming_another_entity_is_refused_rather_than_emptied(self):
		self.be()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.get_stock_balance(item_code=SPRAY, company=OTHER)

	# ── get_warehouse_summary ───────────────────────────────────────────────
	def test_a_warehouse_summary_answers_for_the_callers_own_shed(self):
		self.be()
		answer = mobile_api.get_warehouse_summary(warehouse=STORES)
		self.assertEqual(answer["warehouse"], STORES)
		self.assertEqual(answer["company"], MAIN)
		self.assertEqual({row["item_code"] for row in answer["items"]}, {SPRAY})

	def test_another_entitys_warehouse_is_refused_by_docname(self):
		"""`guard.scoped` IS NO USE ON THIS SHAPE — the rows are items in one
		shed and carry no company — so the whole answer is refused on the
		warehouse's own entity, or a docname somebody typed reads another
		farm's shelves."""
		self.be()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.get_warehouse_summary(warehouse=OTHER_STORES)

	# ── get_stock_ledger ────────────────────────────────────────────────────
	def test_the_ledger_is_filtered_by_warehouse_because_the_tool_cannot_be(self):
		"""`get_stock_ledger` has no company argument at all, so without the
		wrapper's own filter an account scoped to one entity reads every
		movement on the site."""
		self.be()
		answer = mobile_api.get_stock_ledger(item_code=SPRAY)
		self.assertTrue(answer["movements"], "the fixture has MAIN movements to find")
		self.assertNotIn(OTHER_STORES, {row["warehouse"] for row in answer["movements"]})
		self.assertEqual(answer["count"], len(answer["movements"]))

	# ── list_reorder_alerts ─────────────────────────────────────────────────
	def test_the_reorder_alerts_reach_the_phone(self):
		self.be()
		answer = mobile_api.list_reorder_alerts()
		self.assertNotIn(OTHER_STORES, {row["warehouse"] for row in answer["alerts"] if row["warehouse"]})
		self.assertEqual(answer["count"], len(answer["alerts"]))

	# ── create_stock_entry ──────────────────────────────────────────────────
	def test_a_stock_entry_comes_back_a_draft(self):
		"""`submit_stock_entry` IS NOT ROUTED AND MUST NOT BE. Submitting writes
		GL entries, and a posting to the general ledger does not originate on a
		handset in a chemical shed."""
		self.be()
		answer = mobile_api.create_stock_entry(
			entry_type="Material Receipt",
			items=[{"item_code": SPRAY, "qty": 40, "warehouse": STORES, "basic_rate": 2.5}],
		)
		self.assertEqual(answer["docstatus"], 0)
		self.assertEqual(answer["status"], "Draft")

	def test_the_write_is_declared_mutating_on_the_wrapper_and_the_route(self):
		self.assertTrue(mobile_api.create_stock_entry.farm_ops_mutating)
		by_path = {route.path: route for route in farmops_routes.ROUTES}
		self.assertTrue(by_path["/mobile/create_stock_entry"].mutating)
		self.assertFalse(by_path["/mobile/get_stock_balance"].mutating)

	def test_the_submit_is_not_reachable_at_any_path(self):
		self.assertNotIn(
			"submit_stock_entry", {route.path.rsplit("/", 1)[-1] for route in farmops_routes.ROUTES}
		)

	def test_a_line_in_another_entitys_warehouse_is_refused(self):
		"""The company is scoped here and the tool checks every line's warehouse
		against it, so scoping the entity scopes the whole entry."""
		self.be()
		with self.assertRaises(Exception):
			mobile_api.create_stock_entry(
				entry_type="Material Receipt",
				items=[{"item_code": SPRAY, "qty": 5, "warehouse": OTHER_STORES}],
			)


class TheWizardKnowsWhereToPostItsAnswers(MobileAPITestCase):
	"""v0.91.0. `submit_endpoint`, and the flow that could not be filed.

	THE READ SHIPPED WITHOUT THE HALF THAT MAKES IT USABLE. A Wizard Definition
	carries `submit_method` — a TOOL name, `create_accident_report` — and the
	app decodes `submit_endpoint`, a path. Nothing was translating one into the
	other, so a server-authored spec arrived with an empty endpoint and
	`WizardDefinition.isRenderable` was false for every one of them.

	THERE IS STILL NO `submit_wizard` AND THERE MUST NOT BE. One route that
	looked up a spec's submit method and forwarded to it is the dispatcher
	`routes.py` opens by refusing — the permission decision belongs per route.
	"""

	def setUp(self):
		super().setUp()
		from erpnext_mcp.tools import wizards as wizard_tools

		# The five shipped specs, seeded as records — the same ones `migrate`
		# installs. Asserting against the real register rather than a fixture of
		# my own is the point of `test_every_seeded_wizard_now_has_a_route`:
		# a sixth spec added later with an unrouted submit_method fails there.
		wizard_tools.install_wizard_definitions()

	def test_a_wizard_names_a_path_the_app_can_post_to(self):
		self.be()
		answer = mobile_api.get_wizard_definition(wizard="accident_investigation")
		# THE METHOD IS THE TARGET AND THE ENDPOINT IS THE ENVELOPE, and v0.91.0
		# separated them. The app posts `{"wizard", "answers"}`, which
		# `create_accident_report` declares neither of and `routes.bind` therefore
		# dropped whole; `submit_wizard_via_mobile` unpacks it and calls the
		# target named here, through the target's own guard.
		self.assertEqual(answer["submit_method"], "create_accident_report")
		self.assertEqual(answer["submit_endpoint"], "farmops/api/mobile/submit_wizard_via_mobile")
		self.assertEqual(answer["submit_context"], {})

	def test_every_seeded_wizard_now_has_a_route_behind_it(self):
		"""The `inspection_session` flow is why `start_inspection` exists: it
		named a method that was on no table, so a worker could load the form and
		not file it."""
		self.be()
		published = {route.path.rsplit("/", 1)[-1] for route in farmops_routes.ROUTES}
		for key in mobile_api.list_wizard_definitions()["wizards"]:
			with self.subTest(wizard=key["wizard_key"]):
				answer = mobile_api.get_wizard_definition(wizard=key["wizard_key"])
				self.assertIn(answer["submit_method"], published)
				self.assertTrue(answer["submit_endpoint"])

	def test_an_unroutable_wizard_is_blanked_rather_than_pointed_at_a_404(self):
		"""THE APP REFUSES TO DRAW A SPEC WITH NO ENDPOINT, and that is the
		failure worth having. A worker never shown the form has lost nothing; a
		worker who fills in three steps and a signature before the post 404s has
		lost the thing this surface exists to collect."""
		self.be()
		frappe.db.set_value("Wizard Definition", "accident_investigation", "submit_method", "no_such_tool")
		answer = mobile_api.get_wizard_definition(wizard="accident_investigation")
		self.assertEqual(answer["submit_endpoint"], "")
		self.assertIn("does not publish", answer["submit_unavailable"])

	def test_there_is_no_submit_wizard_route(self):
		self.assertNotIn("submit_wizard", {route.path.rsplit("/", 1)[-1] for route in farmops_routes.ROUTES})

	def test_start_inspection_cannot_file_against_a_colleague(self):
		"""`worker` and `foreman` are not on the signature, so `routes.bind`
		cannot deliver either. The person opening the visit is the caller."""
		accepted = farmops_routes.accepted_arguments(mobile_api.start_inspection)
		self.assertNotIn("worker", accepted)
		self.assertNotIn("foreman", accepted)


# ── 9. the spec is in the shape the handset decodes ─────────────────────────
#: The seven controls `WizardFieldType` draws, and the case it decodes anything
#: else into. Transcribed from FarmOpsKit's `WizardDefinition.swift`.
IOS_FIELD_TYPES = frozenset({"text", "number", "date", "select", "photo", "signature", "qr"})


def decode_wizard_definition(payload: dict) -> dict:
	"""`WizardDefinition.init(from:)`, in Python, against the real payload.

	THE POINT OF TRANSCRIBING IT IS THAT THE SERVER CANNOT BE CHECKED AGAINST
	ITSELF HERE. `describe()` answers `wizard_key`, one `title`, `step_key` and
	fourteen field types, all of which are correct and none of which the app
	reads — so a test asserting the server emits what the server emits passes on
	the exact payload that rendered as an empty form on a phone. This decoder
	looks for the keys `CodingKeys` names and nothing else, which is the only way
	a missing translation shows up as a failure rather than as a shrug.

	It is DELIBERATELY as lenient as the Swift is: an absent key is an empty
	string or an empty list, never an exception, because that leniency is what
	turned a shape mismatch into a blank screen instead of an error somebody
	would have seen.
	"""

	def text(row, key):
		value = row.get(key)
		return value if isinstance(value, str) else ""

	def option(row):
		return {
			"value": text(row, "value"),
			"label_en": text(row, "label_en") or text(row, "value"),
			"label_es": text(row, "label_es") or None,
		}

	def field(row):
		raw = text(row, "type").lower()
		return {
			"key": text(row, "key"),
			# An unknown type is a named refusal rather than a throw — and a
			# REQUIRED one blocks submission, which is why a wrong mapping here
			# is not a cosmetic problem.
			"type": raw if raw in IOS_FIELD_TYPES else "unsupported",
			"label_en": text(row, "label_en"),
			"label_es": text(row, "label_es") or None,
			"required": bool(row.get("required")),
			"options": [option(o) for o in (row.get("options") or [])],
			"placeholder_en": text(row, "placeholder_en") or None,
		}

	def step(row):
		return {
			"key": text(row, "key"),
			"title_en": text(row, "title_en"),
			"title_es": text(row, "title_es") or None,
			"help_en": text(row, "help_en") or None,
			"fields": [field(f) for f in (row.get("fields") or [])],
		}

	steps = [step(s) for s in (payload.get("steps") or [])]
	definition = {
		"name": text(payload, "name"),
		"title_en": text(payload, "title_en"),
		"title_es": text(payload, "title_es") or None,
		"steps": steps,
		"submit_endpoint": text(payload, "submit_endpoint"),
		"submit_context": payload.get("submit_context") or {},
		"submit_unavailable": text(payload, "submit_unavailable") or None,
	}
	# `unrenderableReason`, in the order the Swift checks it.
	if not definition["name"] or not steps or any(not s["fields"] for s in steps):
		definition["unrenderable_reason"] = "nothingToFill"
	elif not definition["submit_endpoint"]:
		definition["unrenderable_reason"] = "noSubmitRoute"
	else:
		definition["unrenderable_reason"] = None
	return definition


class TheWizardArrivesInTheShapeTheHandsetDecodes(MobileAPITestCase):
	"""v0.91.0. The translation from `describe()`'s spec to `WizardDefinition`.

	NOT ONE KEY THE APP LOOKS FOR WAS ON THE WIRE. The server answered
	`wizard_key`, a single resolved `title`, `steps[].step_key`,
	`fields[].fieldname` and fourteen field types; the app decodes `name`,
	`title_en`/`title_es`, `key`, `key` and seven. Every lookup missed, so a
	server-authored spec decoded to a nameless definition with no steps — which
	is byte-for-byte what a Wizard Definition nobody filled in would decode to.
	That is why `12f4e6f`'s `submit_endpoint` fix did not make these render: the
	endpoint was the second thing wrong with them.

	THE SEVEN ARE NOT A SUBSET OF THE FOURTEEN AND THE GAP IS NOT ALL FALLBACK.
	Three server types collect an answer no iOS control can — a roster pick, an
	asset pick, several answers where the seven give one — and those pass through
	under their own names so the app draws its "needs a newer app" row and
	refuses to submit a required one. Calling them `select` would draw an empty
	picker instead, which is the same screen with the honesty taken out.
	"""

	#: One of each of the fourteen `field_type` options on Wizard Field, so the
	#: mapping is asserted against the whole doctype rather than against the
	#: subset the five shipped specs happen to use. A fifteenth added to the
	#: doctype and not to this list fails `test_every_type_the_doctype_offers`.
	MIXED = (
		("plain", "text"),
		("story", "long_text"),
		("count", "number"),
		("day", "date"),
		("moment", "datetime"),
		("choice", "select"),
		("several", "multi_select"),
		("ticked", "checkbox"),
		("snap", "photo"),
		("signed", "signature"),
		("scanned", "qr_scan"),
		("spoken", "audio_note"),
		("who", "employee_select"),
		("which", "asset_select"),
	)

	def setUp(self):
		super().setUp()
		self.a_mixed_wizard()

	def a_mixed_wizard(self, key="mixed_types", field_overrides=None, **overrides):
		"""A spec carrying every field type the doctype offers, on a real route.

		`create_accident_report` is a published method, so `submit_endpoint`
		resolves and `unrenderableReason` has only the shape left to complain
		about — which is the thing under test.
		"""
		doc = frappe.new_doc("Wizard Definition")
		doc.wizard_key = key
		doc.__newname = key
		doc.title_en = "Mixed Types"
		doc.title_es = "Tipos Mezclados"
		doc.enabled = 1
		doc.submit_method = overrides.get("submit_method", "create_accident_report")
		step = doc.append(
			"steps",
			{
				"step_key": "everything",
				"title_en": "Everything",
				"title_es": "Todo",
				"description_en": "One of each.",
				"description_es": "Uno de cada uno.",
			},
		)
		for fieldname, field_type in self.MIXED:
			row = {
				"fieldname": fieldname,
				"field_type": field_type,
				"label_en": fieldname.title(),
				"label_es": f"{fieldname.title()} ES",
			}
			if field_type == "select":
				row["options"] = json.dumps(
					[
						{"value": "Pass", "label_en": "Pass", "label_es": "Aprobó"},
						{"value": "Fail", "label_en": "Fail", "label_es": "Falló"},
					]
				)
			# The conditional logic the handset has no evaluator for.
			row["visible_if"] = json.dumps({"field": "choice", "equals": "Fail"})
			row.update((field_overrides or {}).get(fieldname, {}))
			step.append("fields", row)
		for attribute, value in overrides.items():
			setattr(doc, attribute, value)
		doc.insert()
		# `insert()` DOES NOT STORE THE FIELDS AND NEVER DID. They are a
		# grandchild — `Wizard Field` hangs off `Wizard Step`, which hangs off
		# the definition — and Frappe writes one level. This fixture authored a
		# wizard exactly the way the seeder did, which is why it agreed with the
		# seeder about a payload no site ever produced.
		wizard_tools.write_wizard_fields(doc)
		return doc

	def retype(self, doc, fieldname, field_type):
		"""Put a type on a STORED field that this build's Select does not offer.

		A `geo` cannot be inserted: `Wizard Field.field_type` is a Select and
		Frappe refuses a value it does not list, which the double now does too.
		The case under test is not an insert — it is a site whose doctype grew a
		fifteenth option after this build shipped, so the column holds a word
		this build's mapping table has never heard of. A column write is what
		that looks like from here.
		"""
		step = str((doc.get("steps") or [])[0].get("name"))
		row = frappe.db.get_all(
			"Wizard Field", filters={"parent": step, "fieldname": fieldname}, pluck="name"
		)[0]
		frappe.db.set_value("Wizard Field", row, "field_type", field_type)

	def a_spec(self, wizard="mixed_types"):
		self.be()
		return mobile_api.get_wizard_definition(wizard=wizard)

	def fields_by_name(self, spec):
		return {field["key"]: field for step in spec["steps"] for field in step["fields"]}

	# ── the shape ───────────────────────────────────────────────────────────
	def test_a_server_authored_spec_is_renderable_at_all(self):
		"""The whole bug in one assertion: before the translation this decoded
		to `nothingToFill`, which is what an empty record decodes to, so the
		screen blamed whoever authored the form."""
		definition = decode_wizard_definition(self.a_spec())
		self.assertIsNone(definition["unrenderable_reason"])
		self.assertEqual(definition["name"], "mixed_types")
		self.assertEqual(definition["title_en"], "Mixed Types")
		self.assertEqual(len(definition["steps"]), 1)
		self.assertEqual(len(definition["steps"][0]["fields"]), len(self.MIXED))

	def test_the_keys_the_server_already_answered_are_still_there(self):
		"""The translation is ADDITIVE. `wizard_key` is what
		`_with_submit_endpoint` reads to name a wizard in its refusal, and the
		MCP tool answers the same spec to a client with no handset."""
		spec = self.a_spec()
		self.assertEqual(spec["wizard_key"], spec["name"])
		self.assertEqual(spec["steps"][0]["step_key"], spec["steps"][0]["key"])
		self.assertEqual(self.fields_by_name(spec)["plain"]["fieldname"], "plain")

	def test_every_seeded_wizard_decodes_as_renderable_too(self):
		"""The five shipped specs, through the same decoder. A translation that
		only worked on the fixture in this file would be worth nothing."""
		from erpnext_mcp.tools import wizards as wizard_tools

		wizard_tools.install_wizard_definitions()
		self.be()
		for row in mobile_api.list_wizard_definitions()["wizards"]:
			with self.subTest(wizard=row["wizard_key"]):
				definition = decode_wizard_definition(
					mobile_api.get_wizard_definition(wizard=row["wizard_key"])
				)
				self.assertIsNone(definition["unrenderable_reason"])
				self.assertTrue(definition["title_en"])

	# ── the language ────────────────────────────────────────────────────────
	def test_each_language_slot_carries_that_language(self):
		"""v0.92.1. EACH SLOT MEANS WHAT ITS NAME SAYS.

		Until this release both slots got the ONE string the server had resolved,
		which was right on a handset set to the language the Employee record
		named and a lie on every other one. `WizardLabel.pick` prefers `_es`, so
		a picker whose phone said Spanish read the English sentence out of
		`label_es` with nothing marking it as English — worse than the blank the
		duplication was introduced to avoid, because a blank is visible.
		"""
		spec = self.a_spec(wizard="mixed_types")
		self.assertEqual(spec["title_en"], "Mixed Types")
		self.assertEqual(spec["title_es"], "Tipos Mezclados")
		step = spec["steps"][0]
		self.assertEqual(step["title_en"], "Everything")
		self.assertEqual(step["help_en"], "One of each.")
		self.assertEqual(step["help_es"], "Uno de cada uno.")

	def test_the_slots_do_not_move_when_the_caller_reads_spanish(self):
		"""THE SLOTS ARE THE LANGUAGE, NOT THE READER. `language=es` changes
		which language the tool RESOLVES for `title`, `language` and
		`untranslated` — the compliance answer about this worker — and must not
		change which words land in `title_en`."""
		english = self.a_spec(wizard="mixed_types")
		spanish = mobile_api.get_wizard_definition(wizard="mixed_types", language="es")

		self.assertEqual(spanish["title_en"], english["title_en"])
		self.assertEqual(spanish["title_es"], english["title_es"])
		# What the caller was determined to read still travels, and still moves.
		self.assertEqual(spanish["title"], "Tipos Mezclados")
		self.assertEqual(spanish["language"], "es")

	def test_a_field_label_carries_a_real_translation(self):
		field = self.fields_by_name(self.a_spec())["plain"]
		self.assertEqual(field["label_en"], "Plain")
		self.assertEqual(field["label_es"], "Plain ES")

	def test_an_untranslated_string_is_null_rather_than_english(self):
		"""A SPANISH SLOT HOLDING ENGLISH CLAIMS A TRANSLATION NOBODY WROTE.
		`describe` falls back to English when there is none, so copying that into
		`_es` would make every field on the site look translated — and would
		disagree with `untranslated`, which is how a gap gets found and filled.
		`null` is what the app's own fallback already handles."""
		self.a_mixed_wizard(key="half_translated", field_overrides={"plain": {"label_es": ""}})
		fields = self.fields_by_name(self.a_spec(wizard="half_translated"))
		self.assertEqual(fields["plain"]["label_en"], "Plain")
		self.assertIsNone(fields["plain"]["label_es"])
		# Its neighbour on the same step still carries one, so this is the gap
		# being reported rather than the Spanish pass having failed wholesale.
		self.assertEqual(fields["count"]["label_es"], "Count ES")

	# ── the field types ─────────────────────────────────────────────────────
	def test_the_ten_types_an_ios_control_collects_are_translated(self):
		fields = self.fields_by_name(self.a_spec())
		for fieldname, expected in (
			("plain", "text"),
			("story", "text"),
			("count", "number"),
			("day", "date"),
			("moment", "date"),
			("choice", "select"),
			("ticked", "select"),
			("snap", "photo"),
			("signed", "signature"),
			("scanned", "qr"),
			("spoken", "text"),
		):
			with self.subTest(field=fieldname):
				self.assertEqual(fields[fieldname]["type"], expected)

	def test_the_three_types_no_ios_control_collects_are_left_to_be_refused(self):
		"""A roster pick, an asset pick and several-of-many. Mapped to `select`
		they would draw an EMPTY picker — no options travel with any of them —
		and a required one would strand somebody on the step with no way to
		answer and no sentence saying why. Left alone, the app draws its "needs
		a newer app" row and `missingRequired` blocks the submit."""
		fields = self.fields_by_name(self.a_spec())
		definition = decode_wizard_definition(self.a_spec())
		decoded = {field["key"]: field for field in definition["steps"][0]["fields"]}
		for fieldname in ("who", "which", "several"):
			with self.subTest(field=fieldname):
				self.assertNotIn(fields[fieldname]["type"], IOS_FIELD_TYPES)
				self.assertEqual(decoded[fieldname]["type"], "unsupported")

	def test_every_type_the_doctype_offers_is_accounted_for(self):
		"""The list this class mixes IS the doctype's Select options. A
		fifteenth type added to Wizard Field and not translated here fails at
		this line rather than on a phone."""
		meta = frappe.get_meta("Wizard Field")
		offered = {
			option.strip()
			for option in (meta.get_field("field_type").options or "").split("\n")
			if option.strip()
		}
		self.assertEqual(offered, {field_type for _, field_type in self.MIXED})

	def test_a_type_this_table_has_never_heard_of_is_refused_rather_than_guessed(self):
		"""A FIELD WITH NO TYPE AND A FIELD WITH AN UNKNOWN TYPE ARE DIFFERENT
		FACTS. `describe()` already defaults an EMPTY type to `text`, which is
		right — a record somebody left blank is most likely a text box. A type
		this server does not recognise is the opposite: a `geo` added to the
		doctype after this build shipped is the case `WizardFieldType.unsupported`
		was written for, and drawing it as a text box asks a worker to TYPE a
		location and posts the sentence they typed where coordinates belong.
		Nobody finds out; the record just has the wrong thing in it."""
		self.retype(self.a_mixed_wizard(key="odd_one"), "plain", "geo")
		spec = self.a_spec(wizard="odd_one")
		self.assertEqual(self.fields_by_name(spec)["plain"]["type"], "geo")
		decoded = decode_wizard_definition(spec)["steps"][0]["fields"]
		self.assertEqual(next(f for f in decoded if f["key"] == "plain")["type"], "unsupported")

	def test_a_field_with_no_type_at_all_is_still_a_text_box(self):
		"""The other half, and the one where guessing IS right. `describe()`
		makes the call before this translation sees it; asserted here because
		the two halves only make sense read together."""
		self.a_mixed_wizard(key="untyped", field_overrides={"plain": {"field_type": ""}})
		fields = self.fields_by_name(self.a_spec(wizard="untyped"))
		self.assertEqual(fields["plain"]["type"], "text")

	def test_the_original_type_travels_beside_the_translated_one(self):
		"""So a spec can be debugged against the record without reading this
		table, and so a later build that grows a roster picker can find the
		fields it should be drawing."""
		fields = self.fields_by_name(self.a_spec())
		self.assertEqual(fields["story"]["server_field_type"], "long_text")
		self.assertEqual(fields["who"]["server_field_type"], "employee_select")

	# ── options ─────────────────────────────────────────────────────────────
	def test_a_select_carries_its_choices_in_the_shape_the_app_decodes(self):
		definition = decode_wizard_definition(self.a_spec())
		choice = next(field for field in definition["steps"][0]["fields"] if field["key"] == "choice")
		self.assertEqual(
			[(option["value"], option["label_en"]) for option in choice["options"]],
			[("Pass", "Pass"), ("Fail", "Fail")],
		)

	def test_a_checkbox_becomes_a_two_choice_picker_the_endpoint_can_read(self):
		"""THE VALUES ARE `1` AND `0`, NOT `Yes` AND `No`. The answer is posted
		straight through to a method that reads it with `cint`, and `Yes` reads
		as zero there — a tick that files as unticked is worse than a field the
		app refuses to draw."""
		fields = self.fields_by_name(self.a_spec())
		self.assertEqual(fields["ticked"]["type"], "select")
		self.assertEqual(
			[(option["value"], option["label_en"]) for option in fields["ticked"]["options"]],
			[("1", "Yes"), ("0", "No")],
		)

	def test_a_field_with_no_choices_carries_an_empty_list_not_a_missing_key(self):
		fields = self.fields_by_name(self.a_spec())
		self.assertEqual(fields["plain"]["options"], [])

	# ── what is deliberately withheld ───────────────────────────────────────
	def test_the_conditional_logic_is_stripped_from_every_level(self):
		"""iOS HAS NO EVALUATOR AND IS NOT BEING SENT A RULE. Every field in the
		fixture carries a `visible_if`; none of it reaches the wire, so a later
		build cannot half-implement it against a spec nobody validated."""
		spec = self.a_spec()
		self.assertNotIn("visible_if", json.dumps(spec))

	def test_the_key_is_what_the_answer_is_posted_under(self):
		"""`target_field` is what Wizard Field carries for the case where the
		question's name and the column it lands in differ, and `key` is what the
		app posts under. A wizard that set one and got its answers keyed by the
		other files every record with the field it cares about empty."""
		self.a_mixed_wizard(key="retargeted", field_overrides={"plain": {"target_field": "description"}})
		fields = self.fields_by_name(self.a_spec(wizard="retargeted"))
		self.assertIn("description", fields)
		self.assertEqual(fields["description"]["fieldname"], "plain")

	# ── and the endpoint is still resolved after all of it ──────────────────
	def test_the_submit_endpoint_survives_the_translation(self):
		"""`_with_submit_endpoint` runs AFTER the reshape and reads `wizard_key`
		and `submit_method` off it — both of which the reshape leaves alone."""
		spec = self.a_spec()
		self.assertEqual(spec["submit_endpoint"], "farmops/api/mobile/submit_wizard_via_mobile")
		self.assertEqual(spec["submit_method"], "create_accident_report")
		self.assertEqual(spec["submit_context"], {})
		self.assertIsNone(decode_wizard_definition(spec)["unrenderable_reason"])

	def test_a_reshaped_spec_with_no_route_is_still_blanked_and_explained(self):
		"""The two refusals stay distinguishable: this one is `noSubmitRoute`,
		not `nothingToFill`, so the screen sends somebody to the office rather
		than to whoever wrote the form."""
		self.a_mixed_wizard(key="unrouted", submit_method="no_such_tool")
		definition = decode_wizard_definition(self.a_spec(wizard="unrouted"))
		self.assertEqual(definition["unrenderable_reason"], "noSubmitRoute")
		self.assertEqual(definition["submit_endpoint"], "")
		self.assertIn("does not publish", definition["submit_unavailable"])

	def test_the_mcp_tool_still_answers_the_servers_own_shape(self):
		"""THE RESHAPE IS THE SIDECAR'S, and an MCP client has no handset. If
		this starts failing, the translation has leaked out of `api/mobile.py`
		and into the tool every other caller reads."""
		from erpnext_mcp.tools import wizards as wizard_tools

		data = wizard_tools.get_wizard_definition({"wizard": "mixed_types"}).data
		self.assertNotIn("name", data)
		self.assertNotIn("title_en", data)
		self.assertEqual(data["wizard_key"], "mixed_types")
		self.assertEqual(data["steps"][0]["fields"][0]["type"], "text")
		self.assertEqual(data["steps"][0]["fields"][1]["type"], "long_text")
		self.assertIsNotNone(data["steps"][0]["fields"][0]["visible_if"])


# ── 10. the answers reach the endpoint that was named ───────────────────────
class TheWizardFilesWhatWasFilledIn(MobileAPITestCase):
	"""v0.91.0. `submit_wizard_via_mobile`, and the envelope nothing accepted.

	THE POST SUCCEEDED AND FILED NOTHING. `WizardAPI.submit` sends one shape for
	every wizard — `{"wizard": "accident_investigation", "answers": {…}}` —
	because the app cannot know what an accident report's parameters are called.
	`routes.bind` keeps the body keys that match the HANDLER'S SIGNATURE and
	drops the rest, and `create_accident_report` declares neither `wizard` nor
	`answers`, so every answer a worker gave was dropped at the door and the
	target was called with nothing at all. Not a 404 and not a refusal — a 200
	over a record with nothing in it.

	AND IT IS STILL NOT A DISPATCHER. The four properties that make it one are
	each asserted below: the caller does not name the target, the target must be
	on the route table, the target's own guard runs, and the target's own
	argument filter runs.
	"""

	OCCURRED = "2026-07-24 08:00:00"

	def setUp(self):
		super().setUp()
		wizard_tools.install_wizard_definitions()

	def file(self, wizard="accident_investigation", **answers):
		self.be()
		return mobile_api.submit_wizard_via_mobile(wizard=wizard, answers=answers)

	def an_accident(self, **extra):
		answers = {
			"occurred_at": self.OCCURRED,
			"incident_description": "Fell from the third ladder on the north end.",
			"severity": "First Aid",
		}
		answers.update(extra)
		return self.file(**answers)

	# ── the bug ─────────────────────────────────────────────────────────────
	def test_the_answers_reach_the_record_rather_than_the_bin(self):
		"""The whole failure in one assertion. Before this method existed the
		same call filed a report with no description, no severity and no time."""
		answer = self.an_accident()
		self.assertTrue(answer["filed"])
		self.assertEqual(answer["submit_method"], "create_accident_report")
		report = frappe.get_doc("Accident Report", answer["result"]["name"])
		self.assertEqual(report.severity, "First Aid")
		self.assertIn("third ladder", report.incident_description)
		self.assertEqual(str(report.occurred_at), self.OCCURRED)

	def test_the_envelope_the_app_sends_is_declared_here_and_nowhere_else(self):
		"""Which is exactly why it was being dropped. `accepted_arguments` reads
		the signature, and only one method on the table declares these two."""
		self.assertNotIn("answers", farmops_routes.accepted_arguments(mobile_api.create_accident_report))
		self.assertNotIn("wizard", farmops_routes.accepted_arguments(mobile_api.create_accident_report))
		accepted = farmops_routes.accepted_arguments(mobile_api.submit_wizard_via_mobile)
		self.assertEqual(accepted, {"wizard", "wizard_key", "answers"})

	def test_the_endpoint_the_spec_hands_the_renderer_is_this_one(self):
		"""The read and the write have to agree about where a form goes, and the
		app posts to whatever `submit_endpoint` says."""
		self.be()
		spec = mobile_api.get_wizard_definition(wizard="accident_investigation")
		self.assertEqual(spec["submit_endpoint"], "farmops/api/mobile/submit_wizard_via_mobile")
		self.assertIn(
			spec["submit_endpoint"].rsplit("/", 1)[-1],
			{r.path.rsplit("/", 1)[-1] for r in farmops_routes.ROUTES},
		)

	# ── it is not a dispatcher ──────────────────────────────────────────────
	def test_the_caller_does_not_get_to_name_the_target(self):
		"""THE ONE PROPERTY THAT WOULD MAKE THIS A DISPATCHER. The method comes
		off the Wizard Definition, which only Desk access writes; a body key
		naming a destination is not on the signature and cannot become one."""
		accepted = farmops_routes.accepted_arguments(mobile_api.submit_wizard_via_mobile)
		self.assertNotIn("submit_method", accepted)
		self.assertNotIn("method", accepted)
		# And an answer trying to name one is an answer like any other: it is
		# not a parameter of the target either, so it is reported and dropped.
		answer = self.an_accident(submit_method="create_journal_entry")
		self.assertIn("submit_method", answer["ignored"])
		self.assertEqual(answer["submit_method"], "create_accident_report")

	def test_a_target_that_is_not_on_the_route_table_is_refused(self):
		"""The reachable set is exactly what a phone could already post to
		directly. A `submit_method` naming an unrouted tool — or an MCP tool that
		is deliberately not published, which is most of them — files nothing."""
		frappe.db.set_value(
			"Wizard Definition", "accident_investigation", "submit_method", "create_journal_entry"
		)
		with self.assertRaises(Exception) as caught:
			self.an_accident()
		self.assertIn("does not publish", str(caught.exception))
		self.assertIn("Nothing was written", str(caught.exception))
		self.assertEqual(frappe.db.count("Accident Report"), 0)

	def test_the_targets_own_guard_still_runs(self):
		"""`route.handler` is the `@guard.endpoint`-wrapped function, gates and
		all — so a company this caller is not scoped to is refused by the
		target's own scope check and not by anything here."""
		with self.assertRaises(Exception) as caught:
			self.an_accident(company=OTHER)
		self.assertIn("not one of this account's entities", str(caught.exception))

	def test_the_targets_own_argument_filter_still_runs(self):
		"""`worker`, `foreman`, `record_data` and a W-4's `status` are
		unreachable from a phone because they are not on the target's signature.
		Routing the answers through here must not change that by one name."""
		answer = self.an_accident(reported_by=OUTSIDER_EMPLOYEE)
		self.assertIn("reported_by", answer["ignored"])
		report = frappe.get_doc("Accident Report", answer["result"]["name"])
		self.assertEqual(report.reported_by, WORKER_EMPLOYEE)

	def test_an_answer_cannot_name_a_different_caller(self):
		"""Two locks already — `accepted_arguments` excludes `user` and
		`guard.endpoint` pops it — and this is the third, so it never even
		reaches `ignored` where it would read as an authoring mistake."""
		answer = self.an_accident(user=OUTSIDER)
		self.assertNotIn("user", answer["ignored"])
		report = frappe.get_doc("Accident Report", answer["result"]["name"])
		self.assertEqual(report.reported_by, WORKER_EMPLOYEE)

	# ── what the target cannot take ─────────────────────────────────────────
	def test_an_answer_the_target_has_no_parameter_for_is_named(self):
		"""NAMED, NOT COUNTED, AND NOT SILENT. `scene_photo` is a real question
		on the accident wizard and `create_accident_report` has nowhere to put
		it; that was true before this method existed and the answer was going in
		the bin unremarked. Now it is in the response."""
		answer = self.an_accident(scene_photo={"file_name": "a.jpg", "sha256": "x", "byte_count": 1})
		self.assertEqual(answer["ignored"], ["scene_photo"])
		self.assertEqual(answer["accepted_count"], 3)

	def test_the_spec_says_so_before_a_worker_fills_anything_in(self):
		"""Which is the moment somebody can still do something about it. Three
		of the five shipped wizards ask for something their endpoint cannot
		take, and `progressive_discipline` asks for two signatures."""
		self.be()
		spec = mobile_api.get_wizard_definition(wizard="progressive_discipline")
		self.assertIn("manager_signature", spec["submit_unmapped"])
		self.assertIn("employee_signature", spec["submit_unmapped"])
		self.assertNotIn("incident_description", spec["submit_unmapped"])

	def test_filing_the_rest_beats_filing_nothing(self):
		"""THE DECISION THIS METHOD MAKES, STATED. Refusing a wizard whose spec
		asks for one thing too many would take three of the five shipped flows
		away; a discipline record with no signature attached is worth more than
		no record and a foreman who typed it twice."""
		answer = self.an_accident(scene_photo="x", narrative_audio="y")
		self.assertTrue(answer["filed"])
		self.assertEqual(answer["ignored"], ["narrative_audio", "scene_photo"])

	# ── the envelope itself ─────────────────────────────────────────────────
	def test_the_answers_may_arrive_as_a_json_string(self):
		"""A JSON body arrives as a dict and a form-encoded one as a string, and
		both reach this transport — which is why `fallback_auth` exists."""
		self.be()
		answer = mobile_api.submit_wizard_via_mobile(
			wizard="accident_investigation",
			answers=json.dumps(
				{
					"occurred_at": self.OCCURRED,
					"incident_description": "Filed from a form-encoded body.",
				}
			),
		)
		self.assertTrue(answer["filed"])

	def test_answers_that_are_not_an_object_are_refused_rather_than_emptied(self):
		"""A list has no keys to unpack, and coercing it to `{}` would file the
		empty record this whole method exists to stop."""
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.submit_wizard_via_mobile(wizard="accident_investigation", answers=["a", "b"])
		self.assertIn("Nothing was written", str(caught.exception))
		self.assertEqual(frappe.db.count("Accident Report"), 0)

	def test_a_wizard_nobody_named_is_refused(self):
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.submit_wizard_via_mobile(answers={"a": 1})
		self.assertIn("needs a wizard", str(caught.exception))

	def test_a_wizard_that_does_not_exist_is_refused_in_the_reads_own_sentence(self):
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.submit_wizard_via_mobile(wizard="nope", answers={"a": 1})
		self.assertIn("no wizard called", str(caught.exception))

	def test_a_withdrawn_wizard_is_refused_rather_than_filed(self):
		"""A worker whose form was withdrawn between opening it and finishing it
		should be told, not have it filed against a spec an operator pulled."""
		frappe.db.set_value("Wizard Definition", "accident_investigation", "enabled", 0)
		self.be()
		with self.assertRaises(Exception) as caught:
			self.an_accident()
		self.assertIn("is disabled", str(caught.exception))
		self.assertEqual(frappe.db.count("Accident Report"), 0)

	def test_wizard_key_is_accepted_as_well_as_wizard(self):
		"""`describe()` answers `wizard_key` and the app decodes `name`; both
		spellings arrive in the wild and neither should be a 500."""
		self.be()
		answer = mobile_api.submit_wizard_via_mobile(
			wizard_key="accident_investigation",
			answers={"occurred_at": self.OCCURRED, "incident_description": "By key."},
		)
		self.assertEqual(answer["wizard"], "accident_investigation")

	def test_submit_wizard_is_still_not_a_route(self):
		"""The refusal `12f4e6f` wrote down, and the one `WizardAPI.swift`'s
		header repeats. Nothing on this table takes a method name from a body."""
		self.assertNotIn("submit_wizard", {route.path.rsplit("/", 1)[-1] for route in farmops_routes.ROUTES})


# ── v0.91.0: the two payroll outputs ────────────────────────────────────────


class ThePayrollRoutesAreHROnly(MobileAPITestCase):
	"""The only two routes on this surface that reach wages.

	EVERY OTHER READ HERE IS THE CALLER'S OWN WORK OR A BOARD A FOREMAN NEEDS.
	A register is what everybody on the farm was paid, name by name, and a stub
	is what one person was paid; neither is a picker's and neither is a
	foreman's. `DISPATCH_ROLES` would have been the reflex and would have put a
	crew's wages in front of every foreman on the site, so both wrappers gate on
	`HR_ROLES` in their own bodies — which is what these tests are for.
	"""

	def setUp(self):
		super().setUp()
		self.configure(
			enabled=1,
			public_url="https://umbrel.tail4a2b.ts.net",
			**ON,
			allow_get_payroll_register=1,
			allow_render_pay_stub=1,
		)
		from .test_payroll_register import entry, slip

		STORE.seed(
			"Farm Payroll Entry",
			[
				entry(
					"PAY-2026-0001",
					"2026-06-01",
					"2026-06-14",
					[
						slip(WORKER_EMPLOYEE, name="Ana Ramos", gross=1000.0),
					],
				),
				entry(
					"PAY-2026-0090",
					"2026-06-01",
					"2026-06-14",
					[
						slip(OUTSIDER_EMPLOYEE, name="Ben Ortiz", gross=4000.0),
					],
					company=OTHER,
				),
			],
		)

	def as_hr(self):
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		return self.be()

	# ── the register ────────────────────────────────────────────────────────
	def test_a_field_worker_cannot_read_the_register(self):
		set_roles(WORKER, ["Field Worker"])
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.get_payroll_register(date_from="2026-06-01", date_to="2026-06-30")
		self.assertIn("personnel", str(caught.exception).lower())

	def test_a_foreman_cannot_read_the_register_either(self):
		"""Foreman is on `DISPATCH_ROLES` and not on `HR_ROLES`, and this is the
		distinction the whole gate turns on. A foreman runs a crew; a crew's
		wages are not part of running one."""
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.get_payroll_register(date_from="2026-06-01", date_to="2026-06-30")
		self.assertIn("personnel", str(caught.exception).lower())

	def test_an_hr_account_reads_its_own_entity(self):
		self.as_hr()
		answer = mobile_api.get_payroll_register(date_from="2026-06-01", date_to="2026-06-30")
		self.assertEqual(answer["company"], MAIN)
		self.assertEqual(answer["totals"]["gross_pay"], 1000.0)
		self.assertEqual(
			[row["employee_id"] for row in answer["employees"]],
			[WORKER_EMPLOYEE],
		)

	def test_naming_another_entity_does_not_reach_its_payroll(self):
		"""`guard.require_company` is the check, and a register is exactly the
		read where it matters: the holding company's payroll is not readable by
		naming it."""
		self.as_hr()
		with self.assertRaises(Exception):
			mobile_api.get_payroll_register(
				company=OTHER,
				date_from="2026-06-01",
				date_to="2026-06-30",
			)

	def test_the_register_wrapper_declares_no_employee_argument(self):
		"""A register IS the whole crew. A one-person view of it is
		`get_payroll_entry`, and an `employee` key that `bind` could deliver
		would make this two tools wearing one gate."""
		accepted = farmops_routes.accepted_arguments(mobile_api.get_payroll_register)
		self.assertNotIn("employee", accepted)
		self.assertIn("date_from", accepted)
		self.assertIn("include_drafts", accepted)

	# ── the stub ────────────────────────────────────────────────────────────
	def test_a_field_worker_cannot_render_a_stub(self):
		set_roles(WORKER, ["Field Worker"])
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.render_pay_stub(
				payroll_entry="PAY-2026-0001",
				employee=WORKER_EMPLOYEE,
			)
		self.assertIn("personnel", str(caught.exception).lower())

	def test_an_employee_outside_the_callers_crew_reads_as_not_found(self):
		"""Without this an HR account could have walked the holding company's
		payroll one stub at a time."""
		self.as_hr()
		with self.assertRaises(Exception) as caught:
			mobile_api.render_pay_stub(
				payroll_entry="PAY-2026-0090",
				employee=OUTSIDER_EMPLOYEE,
			)
		self.assertIn("not found", str(caught.exception).lower())

	def test_a_run_in_another_entity_reads_as_not_found(self):
		"""`guard.require_scoped_doc` on the run, for the same reason and worded
		the same way — a caller cannot map the site's docnames by watching which
		refusal comes back."""
		self.as_hr()
		with self.assertRaises(Exception) as caught:
			mobile_api.render_pay_stub(
				payroll_entry="PAY-2026-0090",
				employee=WORKER_EMPLOYEE,
			)
		self.assertIn("not found", str(caught.exception).lower())

	def test_show_employer_contributions_cannot_be_sent_from_a_handset(self):
		"""Whether a farm shows its own FICA on a worker's statement is one
		operator policy for the whole operation, not a checkbox on the phone of
		whoever printed it — two workers on one crew getting differently-shaped
		stubs on the same afternoon is a wage-claim exhibit."""
		accepted = farmops_routes.accepted_arguments(mobile_api.render_pay_stub)
		self.assertNotIn("show_employer_contributions", accepted)
		self.assertIn("payroll_entry", accepted)
		self.assertIn("overwrite", accepted)

	def test_the_two_routes_are_mounted_and_only_the_stub_is_mutating(self):
		by_path = {route.path: route for route in farmops_routes.ROUTES}
		self.assertIn("/mobile/get_payroll_register", by_path)
		self.assertIn("/mobile/render_pay_stub", by_path)
		self.assertFalse(by_path["/mobile/get_payroll_register"].mutating)
		self.assertTrue(by_path["/mobile/render_pay_stub"].mutating)


class ReportingAnIncidentIsNotAdministration(MobileAPITestCase):
	"""v0.94.0, F8. The three-way split that used to be one HR gate.

	All five methods carried `personnel.require_hr_role`, on the reasoning that a
	discipline record is a personnel document. Half of that is right and the other
	half was gating the wrong act — and the codebase already contained the correct
	argument, forty lines away in the same file, about `create_accident_report`:
	the person who finds somebody on the ground is whoever finds them.
	"""

	def _record(self, subject=WORKER_EMPLOYEE):
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be()
		return mobile_api.create_discipline_record(
			employee=subject,
			discipline_type="Verbal Warning",
			incident_date=str(frappe.utils.add_days(frappe.utils.today(), -3)),
			incident_description="Arrived 40 minutes late without notice.",
			expected_improvement="Clock in by 06:00 for 60 days.",
			followup_date=str(frappe.utils.add_days(frappe.utils.today(), 60)),
			company=MAIN,
		)

	# ── reporting ───────────────────────────────────────────────────────────
	def test_a_foreman_may_file_the_incident_he_watched(self):
		"""A supervisor who cannot file either does not file, or dictates it to
		somebody who did not see it. Both are worse records than the one the old
		gate was protecting."""
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.be()
		self.assertTrue(
			mobile_api.create_discipline_record(
				employee=WORKER_EMPLOYEE,
				discipline_type="Verbal Warning",
				incident_date=str(frappe.utils.add_days(frappe.utils.today(), -3)),
				incident_description="Arrived 40 minutes late without notice.",
				expected_improvement="Clock in by 06:00 for 60 days.",
				followup_date=str(frappe.utils.add_days(frappe.utils.today(), 60)),
				company=MAIN,
			)
		)

	def test_a_picker_still_may_not(self):
		"""RELAXED IS NOT OPEN. `SHIFT_ROLES` gained the report; a plain field
		worker is not on that list, and filing a warning about a coworker is not
		the same act as reporting that somebody is on the ground."""
		set_roles(WORKER, ["Field Worker"])
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.create_discipline_record(
				employee=WORKER_EMPLOYEE,
				discipline_type="Verbal Warning",
				incident_date=str(frappe.utils.add_days(frappe.utils.today(), -3)),
				incident_description="x",
				expected_improvement="y",
				followup_date=str(frappe.utils.add_days(frappe.utils.today(), 60)),
				company=MAIN,
			)
		self.assertIn("form or close a crew shift", str(caught.exception))

	# ── the subject's own record ────────────────────────────────────────────
	def test_the_subject_may_read_their_own_warning(self):
		"""This was the one personnel record with no `get_my_*` equivalent beside
		`get_my_w4`, `get_my_i9`, `list_my_pay_stubs` and `list_my_trainings`."""
		record = self._record()["name"]
		set_roles(WORKER, ["Field Worker"])
		self.be()
		self.assertEqual(mobile_api.get_discipline_record(record=record)["name"], record)

	def test_and_acknowledge_it_on_their_own_phone(self):
		"""Under the old gate an HR account had to be holding the pad for a worker
		to sign an acknowledgment about themselves."""
		record = self._record()["name"]
		set_roles(WORKER, ["Field Worker"])
		self.be()
		self.assertTrue(
			mobile_api.acknowledge_discipline_record(
				record=record, employee_signature="data:image/png;base64,iVBORw0KGgo="
			)
		)

	def test_and_read_their_own_history(self):
		self._record()
		set_roles(WORKER, ["Field Worker"])
		self.be()
		self.assertTrue(mobile_api.list_discipline_history(employee=WORKER_EMPLOYEE))

	# ── and nobody else's ───────────────────────────────────────────────────
	def test_a_picker_may_not_read_a_colleagues_record(self):
		"""THE HALF THAT DID NOT MOVE. Reading somebody else's file is register 3
		and stays `HR_ROLES` — the self-service branch is for the subject only."""
		STORE.seed(
			"Employee",
			[{"name": "EMP-ROSA", "employee_name": "Rosa Aguilar", "company": MAIN, "status": "Active"}],
		)
		record = self._record(subject="EMP-ROSA")["name"]
		set_roles(WORKER, ["Field Worker"])
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.get_discipline_record(record=record)
		self.assertIn("personnel register", str(caught.exception))

	def test_the_self_exception_cannot_be_claimed_by_naming_somebody(self):
		"""The subject is read OFF THE RECORD and the caller off their login, so
		there is nothing in a request that can assert the exception — the same
		construction `get_i9_form` uses."""
		STORE.seed(
			"Employee",
			[{"name": "EMP-ROSA2", "employee_name": "Rosa Aguilar", "company": MAIN, "status": "Active"}],
		)
		record = self._record(subject="EMP-ROSA2")["name"]
		set_roles(WORKER, ["Field Worker"])
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.acknowledge_discipline_record(
				record=record, employee_signature="data:image/png;base64,iVBORw0KGgo="
			)
		self.assertIn("personnel register", str(caught.exception))

	def test_the_register_across_everybody_is_still_HRs(self):
		"""`get_discipline_report` gained no self-service branch, deliberately: it
		is not somebody reading their own record, it is the document the module
		docstring calls "what an HR manager hands a lawyer"."""
		self._record()
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.be()
		with self.assertRaises(Exception) as caught:
			mobile_api.get_discipline_report(employee=WORKER_EMPLOYEE)
		self.assertIn("personnel register", str(caught.exception))


class ThePickerCanSeeWhoIsQualified(MobileAPITestCase):
	"""v0.106.0. Roles on a roster row, and why a designation could not stand in.

	THE PICKER WAS FILTERING ON NOTHING. Every "who should hold this" screen in
	the app builds its candidate list from `search_employees`, and until this
	release the only role the mobile surface reported was the CALLER's, from
	`get_current_user_context`. So a sheet asking who may approve a compliance
	task offered the whole crew, the foreman picked a picker, and the refusal
	arrived after the choice — a 403 about somebody else's roles, which reads as
	the feature being broken rather than as a permission working.

	A COURTESY AND NOT THE BOUNDARY, which is the property the last test here
	pins. `guard.require_dispatch_role` still runs on every dispatching call.
	"""

	def a_colleague(self, docname, full_name, login=None, designation="", role=None):
		"""One person on the register, enrolled for real where they have a login.

		ENROLLED THROUGH `create_mobile_user` RATHER THAN `set_roles`, and the
		difference is the whole point of these tests. `set_roles` writes the
		double's `ROLES` dict, which is what `frappe.get_roles` reads; a role
		on a real bench is a `Has Role` ROW, which is what `roles.roles_of`
		reads and what this feature reports. Faking the first and asserting the
		second would test the double.
		"""
		if login and role:
			self.enrol(email=login, name=full_name, role=role)
			frappe.local.session.user = "Administrator"
		row = {
			"name": docname,
			"employee_name": full_name,
			"company": MAIN,
			"status": "Active",
		}
		if login:
			row["user_id"] = login
		if designation:
			row["designation"] = designation
		STORE.seed("Employee", [row])
		return docname

	def search(self, query):
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be()
		return {row["name"]: row for row in mobile_api.search_employees(query=query)["employees"]}

	def test_a_foreman_comes_back_marked_as_one(self):
		self.a_colleague("EMP-FOREMAN", "Sol Herrera", "sol@example.test", role="Foreman")
		row = self.search("Herrera")["EMP-FOREMAN"]
		self.assertEqual(row["mobile_roles"], ["Foreman"])
		self.assertEqual(row["primary_role"], "Foreman")
		self.assertTrue(row["can_dispatch"])

	def test_a_picker_comes_back_marked_as_not_one(self):
		self.a_colleague("EMP-PICKER", "Nilo Cruz", "nilo@example.test", role="Field Worker")
		row = self.search("Cruz")["EMP-PICKER"]
		self.assertEqual(row["mobile_roles"], ["Field Worker"])
		self.assertFalse(row["can_dispatch"])

	def test_somebody_with_no_login_has_no_roles_and_says_which_it_is(self):
		"""MOST OF A PICKING CREW. "This person may not dispatch" and "this
		person has no account on this system" are different sentences to put in
		front of a foreman, and a bare empty list says only the first."""
		self.a_colleague("EMP-NOLOGIN", "Pilar Vega")
		row = self.search("Vega")["EMP-NOLOGIN"]
		self.assertEqual(row["mobile_roles"], [])
		self.assertFalse(row["can_dispatch"])

	def test_a_designation_is_not_a_role_and_both_are_returned(self):
		"""A Checker is a designation and carries no permission at all; a Foreman
		is a role that several designations hold. Neither is derived from the
		other, which is why both are on the row."""
		self.a_colleague(
			"EMP-CHECKER", "Rita Salas", "rita@example.test", designation="Checker", role="Field Worker"
		)
		row = self.search("Salas")["EMP-CHECKER"]
		self.assertEqual(row["designation"], "Checker")
		self.assertFalse(row["can_dispatch"])

	def test_the_login_is_read_and_never_returned(self):
		"""`user_id` has to be fetched — a role hangs off a login — and handing it
		back would publish the login of every person on the register, which is the
		identifier an attacker needs before a password is worth guessing."""
		self.a_colleague("EMP-FOREMAN", "Sol Herrera", "sol@example.test", role="Foreman")
		row = self.search("Herrera")["EMP-FOREMAN"]
		self.assertNotIn("user_id", row)
		self.assertNotIn("sol@example.test", str(row))

	def test_get_employee_carries_the_same_answer(self):
		"""One record and the register have to agree, or a detail screen and the
		picker behind it disagree about the same person."""
		self.a_colleague("EMP-FOREMAN", "Sol Herrera", "sol@example.test", role="Foreman")
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be()
		detail = mobile_api.get_employee(employee="EMP-FOREMAN")
		self.assertTrue(detail["can_dispatch"])
		self.assertEqual(detail["mobile_roles"], ["Foreman"])
		self.assertTrue(detail["capability"]["has_login"])

	def test_the_flag_and_the_gate_cannot_disagree(self):
		"""THE PROPERTY THAT MAKES THE COURTESY SAFE. `can_dispatch` is computed
		from the same frozenset `guard.require_dispatch_role` refuses on, so a
		row the picker greyed out is a row the server would have refused, and a
		row it offered is one the server accepts."""
		self.a_colleague("EMP-PICKER", "Nilo Cruz", "nilo@example.test", role="Field Worker")
		self.a_colleague("EMP-FOREMAN", "Sol Herrera", "sol@example.test", role="Foreman")
		found = {**self.search("Cruz"), **self.search("Herrera")}

		for docname, login in (("EMP-PICKER", "nilo@example.test"), ("EMP-FOREMAN", "sol@example.test")):
			with self.subTest(employee=docname):
				refused = False
				try:
					guard.require_dispatch_role(login, "Raising a farm task")
				except frappe.PermissionError:
					refused = True
				self.assertEqual(found[docname]["can_dispatch"], not refused)


class TheCertificateRegisterReachesThePhone(MobileAPITestCase):
	"""v0.106.0. `list_certifications` / `get_certification`, which 404'd.

	WHY A PHONE WANTS THE LICENCE REGISTER AT ALL. "Who may I hand this pesticide
	job to" is answered by who holds a current applicator licence, and by nothing
	else. The app had been inferring it from the training matrix, which answers a
	different question: a training record says somebody sat through the course
	and a certificate says the state issued them a licence, and on a real farm
	those are not the same set of people.
	"""

	def a_certificate(self, name, **overrides):
		payload = {
			"cert_name": name,
			"cert_type": "Applicator License",
			"company": MAIN,
			"holder": "Sol Herrera",
			"issuing_body": "Oregon Department of Agriculture",
			"issued_date": frappe.utils.add_days(frappe.utils.today(), -300),
			"expiration_date": frappe.utils.add_days(frappe.utils.today(), 40),
			"renewal_window_days": 90,
			"status": "Active",
		}
		payload.update(overrides)
		STORE.seed("Certification", [{**payload, "name": name}])
		return name

	def as_foreman(self):
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.be()

	def test_a_foreman_reads_the_register(self):
		self.a_certificate("Applicator — Sol Herrera 2026")
		self.as_foreman()
		data = mobile_api.list_certifications()
		self.assertEqual([row["name"] for row in data["certifications"]], ["Applicator — Sol Herrera 2026"])
		self.assertEqual(data["certification_count"], 1)

	def test_a_picker_does_not(self):
		"""THE DISPATCH GATE, matching `list_farm_task_templates`. A register that
		names everybody whose licence has lapsed is a personnel document, not a
		field read."""
		self.a_certificate("Applicator — Sol Herrera 2026")
		self.be()
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.list_certifications()
		self.assertIn("certificate register", str(caught.exception))

	def test_expiry_is_read_from_the_date_and_not_from_the_status_column(self):
		"""Nothing rewrites a status when a date passes, so a client filtering on
		`status` would show a lapsed licence as Active. `expired` is the answer."""
		self.a_certificate(
			"Applicator — lapsed",
			expiration_date=frappe.utils.add_days(frappe.utils.today(), -15),
		)
		self.as_foreman()
		row = mobile_api.list_certifications()["certifications"][0]
		self.assertEqual(row["status"], "Active")
		self.assertTrue(row["expired"])

	def test_the_detail_carries_the_lapse_history(self):
		self.a_certificate("Applicator — Sol Herrera 2026")
		self.as_foreman()
		detail = mobile_api.get_certification(certification="Applicator — Sol Herrera 2026")
		self.assertEqual(detail["name"], "Applicator — Sol Herrera 2026")
		self.assertIn("renewals", detail)
		self.assertIn("lapses", detail)

	def test_another_entitys_certificate_is_not_found_rather_than_refused(self):
		"""Scoped like every other docname here, so guessing docnames cannot be
		used to learn whether another farm holds a particular licence."""
		self.a_certificate("Applicator — elsewhere", company=OTHER)
		self.as_foreman()
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.get_certification(certification="Applicator — elsewhere")

	def test_the_list_is_scoped_on_the_way_out_too(self):
		self.a_certificate("Applicator — Sol Herrera 2026")
		self.a_certificate("Applicator — elsewhere", company=OTHER)
		self.as_foreman()
		found = {row["name"] for row in mobile_api.list_certifications()["certifications"]}
		self.assertEqual(found, {"Applicator — Sol Herrera 2026"})
