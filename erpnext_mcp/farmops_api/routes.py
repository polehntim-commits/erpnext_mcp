# SPDX-License-Identifier: MIT
"""The closed list of routes, and the argument filter Frappe was doing.

THERE IS NO DISPATCHER AND NO METHOD-NAME ARGUMENT, for the same reason
`api/mobile.py` has neither: a route exists in the table below or its path 404s,
so the entire reachable surface of this service is the lines an auditor can
read in one screen. This app has two hundred-odd MCP tools; `create_journal_entry`,
`convey_parcel`, `import_chart_of_accounts` and `disable_je_gate` are among them
and NONE of them is reachable from here at any path, under any argument, by any
caller.

`test_farmops_api.py` asserts that table against `api/mobile.py` and
`api/files.py` in both directions — every route resolves to a real guarded
function, and every guarded function has a route — so another method cannot
arrive quietly and a route cannot come to point at something ungated. v0.45.0
added nine at once (onboarding, the crew clock and the bucket sync) and that
assertion is what made adding them a two-file change rather than a 404 in a
field. v0.46.0 added the three the wizard reaches BEFORE any of those nine —
`create_employee`, `search_employees` and `reactivate_employee`, which were
still being asked for at Frappe's own `/api/resource/Employee` and are the
reason the onboarding flow got no further than its Identity step. v0.46.2 added
the fourth of that set, `get_employee`: the same 404 at the same doctype's
detail path, on the branch a returning seasonal worker takes.

────────────────────────────────────────────────────────────────────────────
WHY THE ARGUMENT FILTER IS HERE
────────────────────────────────────────────────────────────────────────────

Frappe's own handler binds a request body to a whitelisted method by KEEPING THE
KEYS THAT MATCH THE SIGNATURE AND DROPPING THE REST. Every one of the wrappers
was written against that behaviour — `api/mobile.py` says so at length, because
naming every accepted argument instead of taking `**kwargs` is what makes
`record_data`, `worker_id`, `foreman` and a W-4's `status` unreachable from a
phone.

This transport does not go through Frappe's handler, so that filter is not
happening any more, and `function(**body)` would be two different bugs at once:
a phone sending a key the server does not know would get a 500 instead of an
answer, and a wrapper that ever did grow a `**kwargs` would silently start
accepting whatever the body carried. Reproducing the filter keeps every
signature meaning exactly what it meant on the old path — which is also what
lets the byte-identical cross-check in the test suite be a real comparison
rather than a coincidence.

`user` is dropped even though it IS in every signature: `guard.endpoint` injects
the authenticated caller there, and an account that can name somebody else in a
request body is not scoped to anything. `guard` drops it too. Two locks on the
one argument that would matter.
"""

from __future__ import annotations

import inspect

from ..api import fallback_auth
from ..api import files as files_api
from ..api import mobile as mobile_api

#: The path prefix every route lives under. NOT under `/api/…`: the whole point
#: of this transport is to be somewhere Frappe's request handler is not, and a
#: prefix that collided with Frappe's would be handed straight back to it by any
#: proxy in front that routes on path before it forwards.
PREFIX = "/farmops/api"


class Route:
	"""One reachable path, and the guarded function behind it.

	`path` is under `PREFIX` — `/mobile/get_task`. `handler` is the
	`@guard.endpoint`-wrapped function in `api/mobile.py` or `api/files.py`, gates
	and all. `mutating` is read off the wrapper's own `farm_ops_mutating`
	attribute rather than restated here, so the route table and the endpoint
	cannot come to disagree about whether a call writes.

	The NAME comes off the wrapper too, for the same reason: `guard.endpoint` is
	given the method name the app calls it by, so a route cannot be spelled
	differently from the method it reaches.
	"""

	__slots__ = ("handler", "mutating", "path")

	def __init__(self, prefix, handler):
		self.path = f"{prefix}/{handler.farm_ops_method}"
		self.handler = handler
		self.mutating = bool(getattr(handler, "farm_ops_mutating", False))

	def __repr__(self):  # pragma: no cover - diagnostics
		return f"<Route {self.path}{' (mutating)' if self.mutating else ''}>"


#: The mobile methods and the two file methods. ORDER IS THE APP'S ORDER —
#: the caller, lists, detail, lifecycle, field reports, compliance, upload —
#: because this table is the first thing somebody reads to learn what a phone
#: can do.
ROUTES = (
	Route("/mobile", mobile_api.get_current_user_context),
	Route("/mobile", mobile_api.list_my_tasks),
	Route("/mobile", mobile_api.list_available_tasks),
	Route("/mobile", mobile_api.get_task),
	Route("/mobile", mobile_api.claim_task),
	Route("/mobile", mobile_api.start_task),
	Route("/mobile", mobile_api.complete_task_via_mobile),
	Route("/mobile", mobile_api.reject_task),
	Route("/mobile", mobile_api.report_field_task),
	Route("/mobile", mobile_api.list_compliance_alerts),
	# v0.57.0. The compliance tab stops being a noticeboard. `API_CONTRACT.md`
	# §8.2 — a row may now be closed from a handset, but only where the alert
	# itself says so, which is a box somebody ticks per alert and which defaults
	# off. The gate lives in the wrapper rather than in the app, because a
	# refusal that only exists in a client is not one.
	Route("/mobile", mobile_api.dismiss_compliance_alert),
	Route("/mobile", mobile_api.scan_asset),
	Route("/mobile", mobile_api.get_asset_detail),
	Route("/mobile", mobile_api.log_asset_state_change),
	Route("/mobile", mobile_api.get_available_actions),
	Route("/mobile", mobile_api.report_asset_issue),
	# v0.46.0. The Identity step, which comes before all of it and which the
	# wizard 404'd on: the foreman searches for somebody who has worked here
	# before, and either puts that record back on the payroll or creates a new
	# one. Every path below this line was unreachable in practice until these
	# three existed, because the flow stops at step 1.
	Route("/mobile", mobile_api.create_employee),
	Route("/mobile", mobile_api.search_employees),
	# v0.46.2. The step between the search and the rehire, and the last of the
	# Identity step's four calls to be answering at Frappe's own
	# `/api/resource/Employee/<name>` — which the funnel does not publish. It is
	# what tells the wizard which of its five steps a returning picker has already
	# done, and in tree fruit the returning picker is the common case.
	Route("/mobile", mobile_api.get_employee),
	Route("/mobile", mobile_api.reactivate_employee),
	# v0.45.0. Onboarding, in the order `OnboardingFlow` walks it: the I-9 is
	# opened, the worker fills their half, the employer verifies the documents,
	# the W-4 is signed, and the badge that will carry their piece-rate is mapped
	# to them last.
	Route("/mobile", mobile_api.create_i9_form),
	Route("/mobile", mobile_api.submit_i9_section_1),
	Route("/mobile", mobile_api.submit_i9_section_2),
	# v0.47.0. The two the I-9 half was missing. `list_i9_document_types` is the
	# only READ on the onboarding path and it replaces a Swift array: the server
	# has seeded all 24 USCIS-accepted documents since v0.27.0 and no phone could
	# ask for them. `reverify_i9` is Section 3 — the branch the wizard has been
	# able to SEE since v0.46.2, when `get_employee` began reporting a returning
	# picker's expired I-9 as expired, and has had no call to take.
	Route("/mobile", mobile_api.list_i9_document_types),
	Route("/mobile", mobile_api.reverify_i9),
	# v0.47.1. The I-9 as a document rather than as a doctype. `get_i9_form` is
	# the read the wizard never had — every other I-9 call hands back the record
	# it just wrote, so a foreman opening the flow on somebody already verified
	# could be told `Verified` and nothing else. `generate_i9_pdf` fills the
	# government's own fillable form and hands back a URL to print from, and
	# `upload_signed_i9` files the photograph of the signed sheet back against
	# the record. The three together are what turn four sprints of collected
	# fields into the thing an inspection under §1324a(b)(3) actually asks for.
	Route("/mobile", mobile_api.get_i9_form),
	Route("/mobile", mobile_api.generate_i9_pdf),
	Route("/mobile", mobile_api.upload_signed_i9),
	# v0.48.0. Who may put their name on Section 2, and the three calls that
	# maintain that list. `list_authorized_signers` is the one the wizard needs
	# on the Section 2 screen — v0.48.0 turned the verifier from a free-text box
	# into a roster lookup, and an app that could not read the roster would have
	# to learn whether its own account may sign by submitting a form in an
	# orchard and being refused. The other three are here because a roster that
	# can only be edited in the Desk is one nobody fixes at 6am on a hire day.
	Route("/mobile", mobile_api.list_authorized_signers),
	Route("/mobile", mobile_api.add_authorized_signer),
	Route("/mobile", mobile_api.update_authorized_signer),
	Route("/mobile", mobile_api.remove_authorized_signer),
	Route("/mobile", mobile_api.submit_w4),
	# v0.48.0. The W-4's own federal form, and the last artefact the onboarding
	# flow was missing: the wizard has collected withholding elections since
	# v0.45.0 and had nothing printable to show for them. The employer block it
	# fills needs nothing from the phone — see the wrapper.
	Route("/mobile", mobile_api.generate_w4_pdf),
	Route("/mobile", mobile_api.link_badge_to_employee),
	# v0.50.0. The read between a scan and a name. `add_worker_to_shift` takes
	# an Employee docname and a camera produces a badge string; until this route
	# existed the crew clock could scan a whole crew and roster none of it, and
	# the bucket loop could show a foreman a code but never the picker.
	Route("/mobile", mobile_api.resolve_badge),
	# v0.51.0. The other half of that gap. `generate_employee_badge_qr` has
	# minted readable `CF-0001` badges since v0.50.0 and was published only on
	# the MCP tool registry, which a handset does not speak — so the wizard's
	# badge step could map a card printed elsewhere and could not issue one, on
	# a hire day, to a worker standing there waiting for their number.
	# `set_employee_photo` is what makes the printed card carry a face: the
	# template reads `Employee.image` and nothing on this surface wrote it.
	Route("/mobile", mobile_api.generate_employee_badge_qr),
	Route("/mobile", mobile_api.set_employee_photo),
	# v0.53.0. The same badge, delivered to the wallet the worker already has.
	# `generate_employee_badge_qr` hands back a PNG somebody has to print and
	# laminate; this hands back a `.pkpass` the foreman AirDrops off the handset,
	# which opens straight into Apple Wallet on a device with nothing installed
	# on it — plus the Google Wallet save link for the Android half. The bytes
	# travel in the answer rather than as a `file_url`, because this door
	# authenticates with `X-FarmOps-Token` and a private File is a login page to
	# it.
	Route("/mobile", mobile_api.get_employee_badge_pass),
	# The capture queue and the crew clock.
	Route("/mobile", mobile_api.sync_bucket_entries),
	Route("/mobile", mobile_api.start_shift),
	Route("/mobile", mobile_api.add_worker_to_shift),
	Route("/mobile", mobile_api.end_shift),
	# v0.48.3. The second half of an onboarding upload, and the route whose
	# absence was sending the wizard's six photographs to Frappe's own
	# `/api/method/upload_file` — a path this app's auth hook does not look at, so
	# the funnel-stripped request arrived as Guest and got 200 and a login page.
	# `finalize_staged_file` commits evidence unattached on purpose; this is the
	# scoped call that files it against the Employee it belongs to.
	Route("/mobile", mobile_api.attach_onboarding_document),
	# v0.52.0. Models served from ERPNext, not Volume Vision directly:
	# `get_active_model` is what BucketLog/Farm Ops asks to find out which
	# model is deployed, `get_model_file_chunk` is how it then reads the
	# binary back — through the same door and the same credential as every
	# other call on this surface, rather than a second connection to Volume
	# Vision.
	Route("/mobile", mobile_api.get_active_model),
	Route("/mobile", mobile_api.get_model_file_chunk),
	# v0.54.0. The hiring wizard's Assignment and Housing steps.
	# `list_onboarding_reference_data` is the four dropdowns — Branch,
	# Department, Designation, Employment Type — read off the site instead of
	# compiled into the app, which is the same staleness `list_i9_document_types`
	# removed from the I-9's document picker. The camp pair is the read and the
	# write of one question: which cabin has a bed free, and put this person in
	# it. Routed here rather than left on `/api/method/…` alone for the reason
	# every method above is: the funnel publishes `/farmops/api/…` and nothing
	# else, and a method with no route is a 404 in somebody's hands.
	Route("/mobile", mobile_api.list_onboarding_reference_data),
	Route("/mobile", mobile_api.list_available_housing),
	Route("/mobile", mobile_api.assign_housing),
	# v0.55.0. The other end of the missing-signature rules. A Farm Task raised
	# by `i9_section_2_unsigned` carries the form that needs signing, the app
	# opens a signature pad over it, and this is where the pad posts. It is the
	# one call on this surface that takes IMAGE BYTES in the body rather than a
	# `file_token` — see `tools/signatures.py` for why a capture and a scan take
	# different doors — and it is routed here for the same reason everything
	# above it is: the funnel publishes `/farmops/api/…` and nothing else.
	Route("/mobile", mobile_api.collect_signature),
	# v0.57.0. The same write under the name and the argument spellings
	# `API_CONTRACT.md` §14.2 posts, and the reason both exist is this table's
	# own argument filter: the app sends `signature_field` and `signature_image`,
	# `collect_signature` declares `field` and `signature_base64`, and `bind`
	# drops what a signature does not name — so the contract's body arrived at
	# the v0.55.0 method with no field name and no picture in it. The older
	# spelling keeps its route; handsets already in the field are not asked to
	# change to get an answer.
	Route("/mobile", mobile_api.submit_form_signature),
	# v0.58.0. The break methods: log a break, end it, and read the policy that
	# says what is owed. `get_break_policy` is the read the handset's break coach
	# computes its countdown from — the schedule itself, fetched at shift start.
	Route("/mobile", mobile_api.log_shift_break),
	Route("/mobile", mobile_api.end_shift_break),
	Route("/mobile", mobile_api.get_break_policy),
	# v0.98.0, item 14. THE SCHEDULE, WHICH IS NOT THE POLICY. `get_break_policy`
	# above answers what the RULES are — the bands, the citations, whether a
	# human approved them. This answers what THIS shift owes and at what o'clock,
	# and the difference is the whole feature: a countdown each handset computes
	# for itself drifts by whatever the phones disagree about, so seven phones in
	# an orchard sound off at seven different moments. One computation, seven
	# readers, one instant. It takes `farm_shift` as well as `shift` — item 1 of
	# v0.96.0, applied before it could bite a second route.
	Route("/mobile", mobile_api.get_break_schedule),
	# v0.59.0. The foreman's day: clock somebody out, see production, read the
	# shift. `clock_out_worker` is the mobile name for `remove_worker_from_shift`.
	Route("/mobile", mobile_api.clock_out_worker),
	Route("/mobile", mobile_api.get_shift_production),
	Route("/mobile", mobile_api.get_shift),
	# v0.81.0. THE READ THAT MAKES AN OPEN SHIFT FINDABLE AGAIN. Every other
	# shift method on this table takes a docname, and the only place a docname
	# ever came from was `start_shift`'s answer, held in memory by the screen
	# that made the call. A dismissed sheet, a tab switch, a relaunch or a flat
	# battery lost it, and the shift then stayed open forever with nothing on the
	# handset able to name it — no Attendance rows for that crew, for that day,
	# ever. The app now writes the docname to disk as well (`OpenShiftRecord`),
	# and this is the other half: what a reinstalled app, or a handset that never
	# held the record, asks to find its way back. Defaults to the caller's own
	# shifts — see the wrapper for why that default is not a convenience.
	Route("/mobile", mobile_api.list_shifts),
	# THE THREE SHIFT TOOLS THE HANDSET COULD NOT REACH. Each has existed as an
	# MCP tool for releases — `log_shift_event` since v0.19.3, the two location
	# ones since v0.32.0, the crew timeline since v0.64.0 — and each was reachable
	# only from a Desk, which is the one place none of them is any use.
	#
	# A COMPLIANCE TIMELINE WRITTEN IN THE EVENING IS THE RECORD AN INVESTIGATOR
	# DISCOUNTS, and that is the whole argument for the first of these: OAR
	# 437-004-1131 asks what happened during the shift, and the answer is only
	# worth anything if it was logged when it happened — on the block, on the
	# phone in the supervisor's hand.
	#
	# `log_shift_location` IS THE ONE WRITE HERE A WORKER'S PHONE DRIVES rather
	# than the foreman's, and like every other route on this table it is LIVE
	# rather than held behind the per-tool switch — those govern the AI surface,
	# and this transport's gates are `guard`'s four. A track is still a record of
	# where people were: the controls that bound it here are the grant an
	# operator issues per person and revokes per person, and the handset's own
	# tracking setting.
	Route("/mobile", mobile_api.log_shift_event),
	Route("/mobile", mobile_api.log_shift_location),
	Route("/mobile", mobile_api.get_shift_track),
	Route("/mobile", mobile_api.get_shift_crew_timeline),
	# The QR valve workflow's one route: scan the tag, and — only when the body
	# sends `toggle: true` — open or shut the gate in the same POST, with the
	# action chosen from the state the phone cannot know.
	#
	# THE ARGUMENT FILTER IS WHY THE TOGGLE IS SAFE TO PUT ON THIS TABLE. `bind`
	# keeps only the keys the signature declares, and `scan_valve` declares
	# neither an action name nor a target state — so a body cannot ask for
	# `close_valve` on a valve that is shut, or reach past the toggle into
	# `log_asset_state_change`'s vocabulary. What it can ask for is one press of
	# the button the previous answer offered.
	Route("/mobile", mobile_api.scan_valve),
	# v0.62.0. THE SEVEN `MobileAPI.swift` NAMES AND THIS TABLE DID NOT CARRY.
	# Audited against v0.61.0 on 2026-08-12; see the block comment in
	# `api/mobile.py` that opens this set for the argument each one settles.
	#
	# THE FIRST THREE ARE THE SAME ACT AS THE ROUTE ABOVE THEM UNDER THE NAME AND
	# THE ARGUMENT SPELLINGS THE APP ACTUALLY POSTS, which is exactly why they are
	# routes of their own rather than renames of `list_onboarding_reference_data`,
	# `list_available_housing` and `assign_housing`. This table's own argument
	# filter is the reason a rename would not have been enough: `bind` keeps only
	# the keys a signature names, so `assignable_only` at a method declaring
	# `include_full` is a filter that vanishes and `unit`/`assigned_date` at one
	# declaring `housing_unit`/`check_in_date` is an assignment with no cabin and
	# no date in it. The older spellings keep their routes — a handset already in
	# an orchard is not asked to change to get an answer, which is the same
	# promise `collect_signature` kept when `submit_form_signature` arrived.
	#
	# v0.63.1: BOTH SPELLINGS NOW REACH BOTH DOORS, and this filter is still the
	# reason. Declaring one spelling per method fixed the crossing the app makes
	# and left the opposite crossing dropping arguments the same silent way — so
	# each of these two methods and each of the two above it declares its own
	# spelling AND the other's, reconciled in one place in `api/mobile.py`, with
	# each door keeping the default it answers for.
	Route("/mobile", mobile_api.list_org_reference_data),
	Route("/mobile", mobile_api.list_housing_units),
	Route("/mobile", mobile_api.create_housing_assignment),
	# The two writes the hiring wizard's Assignment and Contact steps have never
	# had. `list_onboarding_reference_data` has served the four dropdowns since
	# v0.54.0 with nowhere to put the answers, and the contact step collects a
	# number and an email a returning picker is reached on in October.
	Route("/mobile", mobile_api.set_employee_org_fields),
	Route("/mobile", mobile_api.set_employee_contact_fields),
	# Reading the folder back. Six routes above file documents against an Employee
	# and until now none could ask what was already there — so a badge issued on a
	# hire day was invisible from a handset ever after, and every private file this
	# app writes was unreachable from the device that collected it (the funnel
	# strips the credential a `/private/files/…` link would want, and answers with
	# a login page).
	Route("/mobile", mobile_api.list_attachments),
	Route("/mobile", mobile_api.get_attachment_content),
	# v0.63.0. THE TWO ENDS OF THE SIGNING FLOW THE SERVER NEVER SERVED.
	#
	# `get_document_preview` is step 1 of the evidence chain — the signer saw the
	# form — and `API_CONTRACT.md` §17.5 is the whole argument for it: both
	# renderers answer with a private `file_url`, this door authenticates with
	# `X-FarmOps-Token` rather than to Frappe, so the app could show the COMPLETED
	# page after signing and not the blank one before. §17.5 called that a
	# server-side gap and said the fix is one route. The bytes travel in the
	# answer, exactly as the signed page and the `.pkpass` do.
	#
	# `seal_signed_document` is step 5, published so a handset can take it for the
	# two cases `submit_form_signature` cannot — a form signed before v0.63.0, and
	# one whose second signature came through the Desk. The ordinary flow never
	# calls it: the signature route seals what it just signed.
	Route("/mobile", mobile_api.get_document_preview),
	Route("/mobile", mobile_api.seal_signed_document),
	# v0.65.0. The scanner screen's one call. Five routes above it — `scan_asset`,
	# `get_asset_detail`, `resolve_badge`, `list_housing_units` and the field
	# reads — each answer for one register and refuse everything else, so a phone
	# pointed at an unknown QR had to be told what it was looking at before it
	# could ask. This resolves the string first and answers second, and it is the
	# same code behind each branch: a badge read here and a badge read at
	# `resolve_badge` get the same refusals, and an asset scanned here gets the
	# same `last_scan_at` stamp `scan_asset` leaves.
	Route("/mobile", mobile_api.universal_scan),
	# v0.67.0. Receipt capture: one screen, four kinds of paper, and the branch
	# between them published as a route of its own. `classify_receipt` reads no
	# doctype and could have shipped as a keyword table inside the app — it is
	# here so that the table exists once rather than once per platform, because
	# two copies of a classifier are two answers to one question.
	#
	# Three of the nine receipt tools are deliberately absent. `submit_scale_ticket`
	# freezes a third party's weight record, and `create_settlement_statement`
	# and `submit_settlement_statement` are a multi-page document that arrives at
	# an office rather than a thing anybody photographs at a tailgate. A method
	# with no route 404s, which is the whole design of this table.
	Route("/mobile", mobile_api.classify_receipt),
	Route("/mobile", mobile_api.create_expense_receipt),
	Route("/mobile", mobile_api.create_scale_ticket),
	Route("/mobile", mobile_api.list_scale_tickets),
	# The three reads `create_expense_receipt`'s pickers and detail view need:
	# a cost center to code the receipt to, a supplier to link it against, and
	# the receipts already filed, for the screen that opens one back up.
	Route("/mobile", mobile_api.list_cost_centers),
	Route("/mobile", mobile_api.list_suppliers),
	Route("/mobile", mobile_api.list_expense_receipts),
	Route("/mobile", mobile_api.update_expense_receipt),
	# Sprint 3 (v0.68.0): compliance alert rectification — see api/rectify.py.
	# Five direct fixes, and the one route every task-shaped fix shares.
	Route("/mobile", mobile_api.renew_certification),
	Route("/mobile", mobile_api.record_training),
	Route("/mobile", mobile_api.sign_training_supervisor_review),
	Route("/mobile", mobile_api.update_regulatory_filing),
	Route("/mobile", mobile_api.advance_policy_review),
	Route("/mobile", mobile_api.rectify_alert),
	# Sprint 4 (v0.69.0): document intelligence. The phone reads a pesticide
	# label at a chemical shed; `validate_document` is what decides whether to
	# believe what it read, and `get_document_validation` reads one back.
	#
	# BOTH PATHS DIFFER FROM THE SPRINT 4 CONTRACT'S, AND THE WRAPPERS SAY SO AT
	# LENGTH. The contract wrote `POST /farmops/api/validate-document` and
	# `GET /farmops/api/document-validation/<name>`; this transport builds every
	# path from the method's own name under `/mobile` (see `Route`), takes POST
	# only, and matches whole paths rather than patterns. A hyphen cannot be a
	# Python method name and a path parameter has nowhere to land, so honouring
	# the contract's spelling would mean forking the router — and the closed,
	# readable-in-one-screen table is the entire design of this file. The bodies
	# and the answers are the contract's, unchanged.
	#
	# THE OTHER THREE TOOLS ARE DELIBERATELY ABSENT. `list_document_validations`
	# and `list_revalidation_due` are an office's registers rather than anything
	# a phone at a shed reads, and `revalidate_document` re-decides a stored
	# status — which is a supervisor's call at a desk. A method with no route
	# 404s, which is the whole design of this table.
	Route("/mobile", mobile_api.validate_document),
	Route("/mobile", mobile_api.get_document_validation),
	# Sprint 7 (v0.72.0): the foreman's crew-task dashboard. Five tools that have
	# existed since Sprint 8 and have never been reachable from a handset — the
	# board for somebody else's work, the dispatch that moves it, the task raised
	# on the spot, and the two ends of the template register.
	#
	# THESE FIVE ARE THE FIRST ROUTES ON THIS TABLE THAT A FIELD WORKER CANNOT
	# CALL. Every path above is a worker's own work and is gated on
	# `guard.FARM_OPS_ROLES`, which admits a picker; each of these calls
	# `guard.require_dispatch_role` in its own body — Foreman or Farm Manager,
	# the same two names `dispatch.py` already draws the line between for
	# Critical urgency. The gate is in the wrapper rather than in the tools
	# because the tools have none: on the MCP transport what stands in front of
	# them is the operator's enablement switch, and a phone does not go through
	# it.
	#
	# `list_dispatched_tasks` IS THE ONE WHOSE ARGUMENT CHANGED SHAPE. The tool
	# takes `worker_id` and will read anybody's board; the wrapper does not
	# declare that key at all — it computes the crew off the caller's own open
	# shifts and lets `employee` narrow that set and nothing else. This table's
	# own argument filter is what makes the undeclared key unreachable rather
	# than merely unused.
	#
	# `get_farm_task_template`, `create_farm_task_template` and
	# `update_farm_task_template` are deliberately absent. Reading one template
	# in full is what the list already carries enough of, and AUTHORING the shape
	# of a recurring job — its evidence contract, the record it builds, its
	# checklist — is a decision made at a desk with the regulation open, not at a
	# tailgate. A method with no route 404s, which is the whole design of this
	# table.
	Route("/mobile", mobile_api.list_dispatched_tasks),
	Route("/mobile", mobile_api.assign_farm_task),
	Route("/mobile", mobile_api.create_farm_task),
	Route("/mobile", mobile_api.list_farm_task_templates),
	Route("/mobile", mobile_api.create_task_from_template),
	# v0.98.0, item 11. THE FOUR REGISTERS A TASK CAN BE ROUTED TO, and the
	# largest gap on `SERVER_CHANGES.md`: the handset's "Where is it" picker
	# showed only "No location", because this table listed no place and created
	# none. Ten spellings were probed on 2026-08-18 and all ten 404'd. Every tool
	# behind these six has existed for releases and was reachable only from a
	# Desk, which is the one place a foreman standing in a block is not.
	#
	# THE READ IS OPEN ON ENROLMENT AND THE WRITES ARE NARROWER THAN DISPATCH.
	# `report_field_task` is open to every enrolled worker and takes a location,
	# so a picker gated on the dispatch role would leave the one call a field
	# worker makes about a place unable to name one. The creates carry
	# `guard.require_location_role` — Farm Manager, a strict subset of
	# `DISPATCH_ROLES` — because a register entry is permanent in a way a task is
	# not: every task, spray record and acre of cost allocation routes through
	# the docname, and a duplicate block created at a tailgate is one the farm's
	# own reports will never merge. `LocationRegistryAPI` asked which role is
	# real; `guard.LOCATION_ROLES` is the answer and says why the two names the
	# app guessed do not exist.
	#
	# FIVE WRITE ROUTES AND ONE WRITE. `create_farm_location` is the polymorphic
	# door the plan named and the four below it are the names the app already
	# prints in its own refusal — all five run `_create_one_location`, so there
	# is one gate and one argument map, and a rename cannot open a fifth way in.
	# IT IS NOT THE DISPATCHER THIS FILE REFUSES: nothing takes a method name
	# from a caller. `doctype` is matched against a closed four-entry table in
	# `api/mobile.py`, exactly as `attach_file_to_document` matches its allowlist
	# of parents.
	#
	# `convey_parcel`, `link_parcel_to_asset` and the three `update_*` tools are
	# DELIBERATELY ABSENT. Moving a title and repointing an asset are desk acts
	# with a document open, and a method with no route 404s — which is the whole
	# design of this table.
	Route("/mobile", mobile_api.list_farm_locations),
	# v0.116.0. THE OPERATIONAL MAP, on the phone that is standing in the block.
	# The read above answers "where can work be sent"; this answers "what is TRUE
	# of each of those places right now" — the restricted-entry countdown, the
	# pre-harvest date, the growth stage and Brix, whether the ground will take a
	# machine. Every fact on it is one somebody needs where the ground is.
	#
	# OPEN ON ENROLMENT LIKE THE READ ABOVE IT, and for a sharper reason. The
	# restricted-entry layer is the one every role sees — it is the layer that
	# keeps a worker out of a treated block — so gating it on the dispatch role
	# would have withheld a safety warning from exactly the people it is about.
	# What each role sees BEYOND that is narrowed by `overlays.layers_for`, and
	# what it held back is named in the answer rather than silently missing.
	Route("/mobile", mobile_api.get_map_overlays),
	# v0.125.0. THE POLYGON `get_map_overlays` DOES NOT CARRY. That route answers
	# what is true of a block; this answers what SHAPE it is, for the map view
	# that draws one `MapPolygon` per block. Fields always carry their boundary
	# — `list_fields` never strips it — and parcels and the overlay layers are
	# opt in, off the same two arguments `get_map_overlays` already takes. Open
	# on enrolment for the reason every other read on this line is: a map with
	# no blocks drawn on it is not a map, and every enrolled worker's handset
	# needs one, not only a foreman's.
	Route("/mobile", mobile_api.list_field_boundaries),
	Route("/mobile", mobile_api.create_farm_location),
	Route("/mobile", mobile_api.create_field),
	Route("/mobile", mobile_api.create_irrigation_zone),
	Route("/mobile", mobile_api.create_parcel),
	Route("/mobile", mobile_api.create_housing_unit),
	# v0.110.0. THE THREE BOUNDARY WRITES, WHICH THE PARAGRAPH ABOVE LISTED AS
	# DELIBERATELY ABSENT UNTIL THIS RELEASE. The sentence it gave was "drawing a
	# boundary … is a desk act with a document open", and that was true of the
	# only way of producing one that existed when it was written: a mouse, on
	# satellite imagery, on a Desk form.
	#
	# WALKING ONE IS NOT A DESK ACT. A boundary recorded by carrying a phone round
	# the edge of a block is a ring of GPS fixes taken by somebody standing on the
	# corner, rather than a guess at where the canopy ends in an image shot in a
	# different season — and over a farm the difference between those two comes to
	# acres. A block's shape is what every geofence answer, every "was the crew in
	# an authorised area" and every Worker Protection Standard answer about which
	# block was sprayed resolves through, so a better shape is worth a route.
	#
	# NOTHING IS RELAXED FOR THE PHONE. All three go through the same three tools
	# the AI and the Desk map call, with every check they have always made: a
	# self-intersection is refused, the enclosed area is compared against the
	# recorded acreage and a disagreement past a quarter is REFUSED, containment
	# is reported both ways, and every derived field is recomputed from the
	# polygon. The area check is the one that earns this route: a walk that cut a
	# corner or lost fixes in a pocket produces a valid polygon enclosing
	# noticeably less ground, and it is refused with both figures named.
	#
	# THE GATE IS `guard.require_location_role` — Farm Manager, the same gate as
	# the five creates above and narrower than dispatch. `owning_entity` and
	# `company` are ABSENT FROM ALL THREE SIGNATURES, so `bind` drops them and no
	# body can file a polygon against an entity the account is not scoped to; the
	# entity is read off the record the caller already proved they may reach.
	Route("/mobile", mobile_api.set_field_boundary),
	Route("/mobile", mobile_api.set_zone_boundary),
	Route("/mobile", mobile_api.set_parcel_boundary),
	# Sprint 8 (v0.78.0): field asset registration, three routes. The iOS
	# screens are built and the flow they perform — photograph the plate,
	# register the asset, print the tag, file the photograph — stopped at step
	# two, because `register_asset` and `generate_asset_qr` have been MCP tools
	# since v0.25.0 with no route and a phone does not speak that transport.
	#
	# `attach_file_to_document` IS THE ONE THAT CHANGED SHAPE ON THE WAY HERE,
	# and this table's own argument filter is half of how. The tool takes any
	# doctype on the site; the wrapper carries an allowlist of the registers a
	# field device already writes into and refuses the rest by name — and it
	# does not DECLARE `allow_cancelled`, so `bind` cannot deliver it and the
	# tool's own refusal of a cancelled parent stands whatever a body says.
	# Personnel evidence keeps its own door: `attach_onboarding_document` above
	# checks the HR role, and `Employee` is deliberately off the allowlist.
	Route("/mobile", mobile_api.register_asset),
	Route("/mobile", mobile_api.generate_asset_qr),
	Route("/mobile", mobile_api.attach_file_to_document),
	# Sprint 9 (v0.79.0): what the day actually looks like. Nineteen routes in
	# four groups, and the gate is different on each group for a stated reason.
	#
	# THE PAUSE PAIR IS A WORKER'S OWN WORK and is gated on nothing more than
	# enrolment, like `start_task` above it. `worker_id` is not on either
	# signature, so this table's argument filter is what stops an account
	# stopping a stranger's clock from across the farm.
	Route("/mobile", mobile_api.pause_task_via_mobile),
	Route("/mobile", mobile_api.resume_task_via_mobile),
	# LINKING IS AN OBSERVATION AND MERGING IS A DECISION, which is why they are
	# gated differently. Noticing that your job and somebody else's are the same
	# valve is what a worker in a block sees and a foreman at a desk does not;
	# folding one away takes somebody's work off the board under another name,
	# and that is Foreman-and-above.
	Route("/mobile", mobile_api.link_tasks_via_mobile),
	Route("/mobile", mobile_api.merge_task_via_mobile),
	# The narrative trio. `attach_audio_note` is the one this sprint exists for:
	# a foreman at an accident scene has a phone in one hand and somebody on the
	# ground, and typing is not what happens next. The handset transcribes
	# on-device and posts the words; the recording travels the chunked upload
	# path and is linked here.
	Route("/mobile", mobile_api.add_task_note_via_mobile),
	# v0.98.0, item 12. THE SAME WRITE UNDER THE NAME THE APP ASKS FOR. `Route`
	# builds every path off the wrapper's own name, so the method above published
	# `/mobile/add_task_note_via_mobile` and the handset — asking for the MCP
	# tool's name, which is what any reader would predict — got a 404, then six
	# more for `add_narrative`, `append_note`, `create_task_note`, `add_note`,
	# `create_narrative_note` and `log_task_note`. `list_task_notes` IS published
	# plainly, so a record's narrative could be read and never written: every
	# field note a foreman typed is still in `LocalNarrativeStore` on one phone.
	# The older spelling keeps its route.
	Route("/mobile", mobile_api.add_task_note),
	Route("/mobile", mobile_api.attach_audio_note),
	Route("/mobile", mobile_api.list_task_notes),
	# THE FIVE DISCIPLINE ROUTES CARRY AN HR GATE IN THEIR OWN BODIES, not the
	# field-role gate every route above them uses. A discipline record is a
	# personnel document; a picker holding a perfectly good field credential has
	# no business reading one, let alone writing one. `expire_incident_record`
	# is deliberately absent — ageing a step out of a chain is a policy decision
	# made at a desk with the handbook open, and a method with no route 404s.
	Route("/mobile", mobile_api.create_discipline_record),
	# v0.98.0, item 12. A WORKER'S GRIEVANCE, WHICH IS NOT A WARNING. The
	# register has carried both directions since v0.94.0 and neither the name nor
	# the argument was reachable from a handset, so the app filed a complaint
	# through `create_discipline_record` at the lowest step with `DISPUTE RAISED
	# BY …` typed into the description — which produces a step on a progressive-
	# discipline chain, in the file of the person who complained. It carries the
	# same shift-role gate as the create above it, and the argument is stronger:
	# the person who needs to raise a complaint is whoever has one, and a server
	# that refuses their credential receives no complaints and concludes there
	# are none. READING the register is still HR_ROLES.
	Route("/mobile", mobile_api.create_dispute),
	Route("/mobile", mobile_api.acknowledge_discipline_record),
	Route("/mobile", mobile_api.get_discipline_record),
	Route("/mobile", mobile_api.list_discipline_history),
	Route("/mobile", mobile_api.get_discipline_report),
	# THE ACCIDENT ROUTES SPLIT, and this is the most deliberate gate on the
	# table. `create_accident_report` and `get_accident_report` are open to any
	# enrolled worker: the person who finds somebody on the ground is whoever
	# finds them, and a server that refused their report because they are not a
	# foreman is a server people work around at the exact moment that matters.
	# Updating, closing and listing the register are the INVESTIGATION, and an
	# investigation is somebody's job — those take the dispatch role.
	Route("/mobile", mobile_api.create_accident_report),
	Route("/mobile", mobile_api.get_accident_report),
	Route("/mobile", mobile_api.update_accident_investigation),
	Route("/mobile", mobile_api.close_accident_investigation),
	Route("/mobile", mobile_api.list_accident_reports),
	# The wizard registry. Both reads, both gated on enrolment alone, and both
	# answer in the caller's own language off `Employee.preferred_language`.
	Route("/mobile", mobile_api.get_wizard_definition),
	Route("/mobile", mobile_api.list_wizard_definitions),
	# v0.85.0. The string bundle a handset pulls once at login instead of asking
	# for one label at a time. A read, gated on enrolment alone, and the one
	# route on this table whose answer depends on WHO IS ASKING rather than what
	# they asked for: `Employee.preferred_language` decides the language, with
	# the request's own `Accept-Language` as the fallback where that column is
	# empty. Never the other way round — see `tools/translations.py`.
	Route("/mobile", mobile_api.get_translation_bundle),
	# v0.80.0. Trade documentation: four reads and one write. The write is a
	# DRIVER'S CONFIRMATION and not the desk's tool — `confirm_shipment_movement`
	# takes 'departed' or 'delivered' and its signature carries neither `status`
	# nor `override_reason`, so `bind` cannot pass either. Releasing a shipment
	# and cancelling one stay on the MCP surface, where the trade-role gate is.
	Route("/mobile", mobile_api.list_shipments),
	Route("/mobile", mobile_api.get_shipment),
	Route("/mobile", mobile_api.get_shipment_readiness),
	Route("/mobile", mobile_api.list_trade_documents),
	Route("/mobile", mobile_api.confirm_shipment_movement),
	# v0.91.0. The shadow log feed, which has been an MCP tool since v0.85.0 and
	# has 404'd on this transport ever since — the same failure shape as the six
	# methods v0.58.1 unmounted: the server can do it, the phone cannot ask.
	#
	# ALL THREE ARE ADDRESSED TO THE CALLER AND ARE NOT A REGISTER. The tool's
	# own `employee` argument names the RECIPIENT of a copy, and none of the
	# three wrappers declares it — so this table's argument filter is what stops
	# an account reading a colleague's whole view of their crew, exactly as it
	# stops `worker_id` reaching the pause pair above.
	#
	# `acknowledge_shadow_log` IS THE WRITE, and the one place where scope is not
	# the whole gate: `shadow_key` is COMPOSED from the recipient's own Employee
	# ID, so a docname can be written down rather than discovered. The wrapper
	# re-checks the addressee and answers a miss in the same words as a row that
	# does not exist.
	#
	# THERE IS NO FOURTH ROUTE. Propagation is not a tool — it happens inside a
	# bucket sync, a shift close, an alert and a completion — so there is nothing
	# here that could write a copy, only read one and say it was read.
	Route("/mobile", mobile_api.list_shadow_log_entries),
	Route("/mobile", mobile_api.get_shadow_log_entry),
	Route("/mobile", mobile_api.acknowledge_shadow_log),
	# v0.91.0. The inventory tab, which has been calling five endpoints that were
	# never mounted. The tools have existed since v0.69.0; `FarmOps/Features/
	# Inventory` shipped against `sprint-4-api-contracts.md` § Workstream 1 and
	# every one of its four screens has shown the "is not a Farm Ops API method"
	# 404 in an error banner since.
	#
	# THE APP WAS ASKING AT THE WRONG SHAPE AS WELL AS THE WRONG PATH. The
	# contract describes hyphenated top-level GETs — `/farmops/api/stock-balance`,
	# and a warehouse as a PATH component — and this table cannot express either:
	# `Route` builds every path as `{prefix}/{farm_ops_method}`, off the wrapper's
	# own name. That is the same call v0.8x already made for Workstream 2 (see
	# `MobileAPI.swift`, "the hyphenated top-level path describes the intent, not
	# the transport"), and it is made the same way here rather than teaching this
	# table a second path grammar for five routes. The app moves to the namespace.
	#
	# FOUR READS AND ONE WRITE. `create_stock_entry` is `mutating=True` and comes
	# back a DRAFT: `submit_stock_entry` writes GL entries and is deliberately
	# absent from this table, so nothing a handset can reach posts to the ledger.
	Route("/mobile", mobile_api.get_stock_balance),
	Route("/mobile", mobile_api.get_warehouse_summary),
	Route("/mobile", mobile_api.get_stock_ledger),
	Route("/mobile", mobile_api.list_reorder_alerts),
	Route("/mobile", mobile_api.create_stock_entry),
	# v0.91.0. The last unrouted wizard submit target. A wizard names its own
	# `submit_method` and four of the five seeded ones already had a route here —
	# `create_employee`, `register_asset`, `create_accident_report` and
	# `create_discipline_record`. `inspection_session` named `start_inspection`,
	# which existed nowhere, so that form loaded and could not be filed.
	#
	Route("/mobile", mobile_api.start_inspection),
	# v0.91.0. THE ENVELOPE, AND WHY IT IS NOT THE DISPATCHER THIS FILE REFUSES.
	# `12f4e6f` said there must never be a `submit_wizard`, and there is not one:
	# nothing on this table takes a method name from a caller and forwards to it.
	# What the app posts is one envelope for every wizard — `{"wizard": …,
	# "answers": {…}}` — because it cannot know what an accident report's
	# parameters are called, and the argument filter below dropped both keys on
	# the floor, so `create_accident_report` was called with NOTHING and filed an
	# empty record without complaining. `submit_wizard_via_mobile` is the one
	# method that speaks that envelope.
	#
	# THE OBJECTION WAS THAT A DISPATCHER PUTS THE PERMISSION DECISION IN THE
	# WRONG PLACE, and it still stands — so this makes no decision. The target is
	# read off the Wizard Definition's `submit_method`, which only Desk access
	# sets; it is resolved against THIS TABLE, so the reachable set is exactly
	# what a phone could already post to directly; the target runs its OWN
	# `guard.endpoint` and its own `accepted_arguments` filter. A Housing
	# Inspection and a Farm Incident Record are still guarded by their own routes,
	# because they are still reached through their own routes.
	Route("/mobile", mobile_api.submit_wizard_via_mobile),
	# v0.91.0. The two payroll outputs. THE ONLY ROUTES ON THIS TABLE THAT REACH
	# WAGES, and the only two whose wrappers gate on `HR_ROLES` rather than on
	# the field roles the surface is built for — a register is what everybody was
	# paid and a stub is what one person was paid, and neither is a picker's or a
	# foreman's to read. `DISPATCH_ROLES` would have been the reflex and would
	# have put a crew's wages in front of every foreman on the site.
	#
	# THE READ IS COMPANY-SCOPED AND THE WRITE IS EMPLOYEE-SCOPED. The register
	# declares `company` and `guard.require_company` refuses an entity this
	# account cannot reach; the stub runs its employee through
	# `_employee_argument`, so a docname from outside the caller's own crew reads
	# as not found. Without that second check an HR account could have walked the
	# holding company's payroll one stub at a time.
	#
	# `show_employer_contributions` IS NOT ON THE STUB'S SIGNATURE, so this
	# table's argument filter is what keeps it off the handset — whether a farm
	# shows its own FICA on a worker's statement is one operator policy, not a
	# checkbox on the phone of whoever printed it.
	Route("/mobile", mobile_api.get_payroll_register),
	Route("/mobile", mobile_api.render_pay_stub),
	# v0.92.0. The five tax remittance reads, on the same footing as the register
	# above and gated the same way: every one carries `require_hr_role` in its own
	# body and every one is company-scoped through `guard.require_company`. They
	# are aggregates of what the whole crew was paid, which is what makes the HR
	# gate the right one and `DISPATCH_ROLES` the wrong one.
	#
	# NONE OF THEM WRITES ANYTHING, including the two named after returns.
	# `get_941_prefill` and `get_futa_summary` compute a form and record nothing;
	# generating a Tax Form that survives a later payroll correction is
	# `generate_tax_form`, which is a mutating tool on the MCP surface and is
	# deliberately NOT on this table. A phone can read what is owed; committing a
	# figure as the thing an agency was told is a desk act.
	#
	# THE CORRECTION ARGUMENTS ARE ON THE SCHEDULE ROUTE AND NOWHERE ELSE.
	# `lookback_total`, `schedule` and `payday_offset_days` are on the deposit
	# schedule's signature because without them it assumes a monthly depositor
	# paying on the day a period closes, and both assumptions produce dates that
	# are early. The 941's adjustment lines are NOT on its signature, so this
	# table's argument filter keeps them off the handset — a sick-pay adjustment
	# is settled with an accountant against the books, not typed into a phone.
	Route("/mobile", mobile_api.get_tax_remittance_summary),
	Route("/mobile", mobile_api.get_941_prefill),
	Route("/mobile", mobile_api.get_state_tax_remittance),
	Route("/mobile", mobile_api.get_tax_deposit_schedule),
	Route("/mobile", mobile_api.get_futa_summary),
	# The three compliance reports, and the only AGGREGATE reads on this table.
	# Everything above answers a question about one document or about the
	# caller's own work; these answer one about a whole crew, a whole calendar
	# year or a whole season. They are here because the person who needs them is
	# usually standing in front of the person asking for them, and "I will email
	# it when I am back at the office" is the answer that becomes a finding.
	#
	# ALL FOUR TAKE A ROLE; none is open on enrolment alone. The training matrix
	# is a personnel document — it names who has had no training at all — and
	# carries the HR gate in its own body like the five discipline routes above.
	# The two OSHA reads take the dispatch role for the same reason
	# `list_accident_reports` does: filing a report is open to whoever finds
	# somebody on the ground, but the register is somebody's job. The spray
	# report is the operation's pesticide use record; `get_active_rei` is
	# already the read a picker actually needs.
	#
	# `get_osha_300a_summary`'s TWO OVERRIDE ARGUMENTS ARE UNREACHABLE HERE. The
	# tool takes `total_hours_worked` and `average_employees` so a desk can
	# supply a denominator from payroll; the wrapper declares neither, so this
	# table's argument filter drops both — a handset that could set the
	# denominator of a TRIR could set the TRIR, and that number goes on a form a
	# regulator reads.
	Route("/mobile", mobile_api.get_training_compliance_report),
	Route("/mobile", mobile_api.get_osha_300_log),
	Route("/mobile", mobile_api.get_osha_300a_summary),
	Route("/mobile", mobile_api.get_spray_application_report),
	# The curriculum and the afternoon. THE ONLY SET ON THIS TABLE WHOSE WHOLE
	# POINT IS THAT IT HAPPENS WITH A PHONE IN ONE HAND: a crew leader scans
	# twelve badges at a shed door, takes twelve signatures an hour later, and
	# completes the session before anybody has driven anywhere. The alternative
	# this replaces is a paper sheet that reaches a desk on Thursday and becomes
	# twelve typed forms that disagree by the third.
	#
	# `get_training_curriculum` IS THE ONE OPEN ON ENROLMENT ALONE. It returns
	# what a COURSE is — a video link, a materials list, a duration — and none of
	# it is a fact about a person. The picker whose WPS card the compliance tab
	# has just told them has lapsed is exactly who should be able to open the
	# film. Every other route here carries the HR gate the training matrix
	# carries, in its own body, because a session names by name who was taught
	# what.
	#
	# `update_training_type`'s `regimes` AND `retention_years` ARE UNREACHABLE
	# HERE. The tool takes both so a desk can correct a curriculum; the wrapper
	# declares neither, so this table's argument filter drops them — which audits
	# a course answers and how long its records are kept are decisions with a
	# citation behind them, not corrections typed into a phone in a shed.
	Route("/mobile", mobile_api.get_training_curriculum),
	Route("/mobile", mobile_api.update_training_type),
	Route("/mobile", mobile_api.create_training_session),
	Route("/mobile", mobile_api.add_session_attendee),
	Route("/mobile", mobile_api.sign_session_attendance),
	Route("/mobile", mobile_api.complete_training_session),
	Route("/mobile", mobile_api.get_training_session),
	Route("/mobile", mobile_api.list_training_sessions),
	Route("/mobile", mobile_api.render_training_sign_in_sheet),
	Route("/files", files_api.stage_file_chunk),
	Route("/files", files_api.finalize_staged_file),
	# Direct deposit. The worker's OWN bank details and nobody else's —
	# these three resolve the subject from the caller's login rather than
	# from an `employee` argument, because company scope is shared by
	# everybody enrolled and would let one picker repoint another's wages.
	Route("/mobile", mobile_api.list_my_bank_accounts),
	Route("/mobile", mobile_api.add_my_bank_account),
	Route("/mobile", mobile_api.update_my_bank_account),
	# Employee self-service. THE SAME ARGUMENT THE THREE ABOVE MAKE, applied to
	# the four records a worker most often needs: their withholding elections,
	# what they were paid, what they have been trained in, and where their I-9
	# stands. NONE OF THE FIVE DECLARES AN `employee` ARGUMENT, so this table's
	# filter has nothing to drop and there is no key a body could carry that
	# would point one of them at a colleague — the subject is `_employee(user)`,
	# resolved from the login the seven gates already established.
	#
	# THEY ARE THE ONLY ROUTES HERE THAT REACH WAGES WITHOUT AN HR ROLE, and the
	# distinction that makes that right is one person versus a crew.
	# `get_payroll_register` is what everybody was paid and `render_pay_stub`
	# draws anybody's statement; both take HR_ROLES and both stay where they are.
	# These answer only about the caller, which is why a picker may call them and
	# why neither of the HR pair could be relaxed to do the same job.
	#
	# `get_my_pay_stub_pdf` IS THE ONE MUTATING ROUTE IN THE SET, and it is
	# mutating for one reason: a period whose stub has never been drawn is drawn
	# on demand, which attaches a File to the run. `overwrite` IS NOT ON ITS
	# SIGNATURE, so this table's filter is what makes a redraw unreachable rather
	# than merely refused — replacing a statement a worker was already handed is
	# a correction, and a correction is made by whoever answers for the payroll.
	#
	# v0.92.2: IT CARRIES THE PDF IN THE ANSWER, like `get_document_preview` and
	# the `.pkpass` above and for the identical reason — the funnel strips the
	# credential a private `file_url` wants. `get_attachment_content` cannot serve
	# this one: it asks Frappe for `read` on the PARENT, and the parent is a
	# payroll run holding the whole crew's slips. There is no switch to turn the
	# page off: every argument here is a key `bind` would deliver, and the screen
	# that wants envelopes without pages is `list_my_pay_stubs`.
	Route("/mobile", mobile_api.get_my_w4),
	Route("/mobile", mobile_api.list_my_pay_stubs),
	Route("/mobile", mobile_api.get_my_pay_stub_pdf),
	Route("/mobile", mobile_api.list_my_trainings),
	Route("/mobile", mobile_api.get_my_i9),
	# The payroll deduction register. THE THIRD SET OF ROUTES ON THIS TABLE THAT
	# REACHES WAGES, and like the register and the stub above they gate on
	# `HR_ROLES` in their own bodies rather than on the field roles this surface
	# is built for — TRUE OF ALL FIVE SINCE v0.94.0, AND TRUE OF ONLY THE TWO
	# WRITES BEFORE IT. The three reads were scope-only while this comment said
	# otherwise, which is the failure mode this file is most prone to: a claim
	# about a gate, sitting next to the route, read as if it were the gate. What a person's pay is garnished for is among the most
	# sensitive facts this app holds, and a foreman has no business reading it.
	#
	# THE READS ARE SCOPED TWO DIFFERENT WAYS because they ask two different
	# questions. `list_payroll_deductions` declares `company` and
	# `guard.require_company` refuses an entity this account cannot reach;
	# `get_payroll_deduction` runs its docname through `require_scoped_doc`, so a
	# row belonging to another entity reads as NOT FOUND rather than as refused
	# and the register cannot be mapped by watching which error comes back.
	# `list_employee_deductions` takes the employee through
	# `_employee_argument`, the same check that stops an HR account walking the
	# holding company's payroll one stub at a time.
	#
	# THE WRITES ARE HERE BECAUSE OF WHEN AN ORDER ARRIVES. Withholding on a
	# support order is required from the first pay period after service, so the
	# gap between the envelope being opened in a yard and somebody reaching a
	# Desk is a gap with a liability in it.
	#
	# `employee` AND `company` ARE ABSENT FROM `update_payroll_deduction`'s
	# SIGNATURE, so this table's argument filter is what makes the tool's refusal
	# unreachable rather than merely enforced: there is no key to send. Moving a
	# deduction to another worker would apply an order made against one person to
	# somebody else.
	Route("/mobile", mobile_api.list_payroll_deductions),
	Route("/mobile", mobile_api.get_payroll_deduction),
	Route("/mobile", mobile_api.list_employee_deductions),
	Route("/mobile", mobile_api.create_payroll_deduction),
	Route("/mobile", mobile_api.update_payroll_deduction),
	# v0.98.0. Bin sealing — `PieceTallyViewModel.sealBin(tag:)`. THE LAST
	# MOMENT ANYBODY KNOWS THE ANSWER: a bin leaves the orchard carrying a tag
	# and nothing else, and every question the packing house asks afterwards is a
	# join from that tag back to an hour that was never written down. This route
	# is what writes it down, at the instant the checker taps Seal.
	#
	# `company` AND `source` ARE ABSENT FROM ITS SIGNATURE, so this table's
	# argument filter is what makes them unreachable rather than merely refused.
	# The first would let a phone file another farm's harvest against this one's
	# crew; the second would let a handset disguise a typed record as a scanned
	# one, and the register has to be able to tell those apart.
	Route("/mobile", mobile_api.seal_bin),
	# v0.99.0. WHERE A BREAK HORN IS DELIVERED. The two routes that make
	# `log_shift_break` reach the crew's phones rather than only the foreman's:
	# the handset enrols its APNs device token on login, and retires it on
	# logout.
	#
	# NEITHER DECLARES `user` OR `employee`, which is the whole of their
	# security. Both are resolved from the caller's own login, so `bind` has
	# nothing to drop and no body can enrol a device against somebody else's
	# name — which would be a way to have another worker's break horns and
	# dispatch pings delivered to a phone of your choosing. The same property
	# the three direct-deposit routes above have, for the same reason.
	#
	# `unregister_push_token` DOES NOT DECLARE `token` either, and that absence
	# is load-bearing rather than tidy: the device is the identity, and a logout
	# that had to present the current token would fail exactly when it matters —
	# a phone whose token Apple rotated between login and logout would go on
	# receiving another shift's break horns forever.
	Route("/mobile", mobile_api.register_push_token),
	Route("/mobile", mobile_api.unregister_push_token),
	# v0.105.0. SERVER_CHANGES #24 — the in-app feedback bubble's one call, and
	# the route whose absence has been parking every note the farm has written.
	# A 404 does not FAIL a note on this client, it parks it: retried every six
	# hours forever and never counted against the give-up bound. So publishing
	# this path does not start collecting feedback, it collects the backlog —
	# which is why `entry_uuid` is unique on the doctype and a resend answers
	# success with the record already held.
	#
	# `user` IS ABSENT FROM ITS SIGNATURE and the app sends one on every call.
	# `bind` drops it and `guard.endpoint` injects the authenticated caller, so
	# the login on a filed note is the one that was proved rather than the one a
	# shared handset reported. `screenshot_filename` and `screenshot_content_type`
	# are absent for a different reason: the extension is sniffed off the first
	# bytes and the stored filename is composed from the docname, so there is
	# nowhere for a caller-supplied name to land.
	Route("/mobile", mobile_api.submit_app_feedback),
	# v0.106.0. The compliance-alert-to-task feature's three missing paths.
	#
	# `materialize_task_for_alert` IS THE ONE THE APP HAS BEEN CALLING SINCE THE
	# FEATURE SHIPPED. `MobileAPI.swift` names it, `ComplianceAPI.createTaskFromAlert`
	# tries it on every raise, and it has answered 404 on every farm — so the app
	# has been falling back to composing a task out of the alert's prose and
	# assigning it in a second call. That fallback's task is not linked to the
	# alert: `source_alert` is not on `create_farm_task`'s signature, so nothing
	# closes the alert when the work is done and the next sweep raises it again
	# beside the task somebody is already holding. This is the path that draws the
	# edge.
	#
	# THE TWO CERTIFICATE READS ARE THE OTHER HALF OF THE SAME SHEET. "Who may I
	# hand this pesticide job to" is answered by the licence register and by
	# nothing else — a training record says somebody sat through the course, a
	# certificate says the state issued them a licence, and on a real farm those
	# are different sets of people. Both carry `require_dispatch_role` in their own
	# bodies: a register that names everybody whose licence has lapsed is a
	# personnel document, not a field read.
	Route("/mobile", mobile_api.materialize_task_for_alert),
	Route("/mobile", mobile_api.list_certifications),
	Route("/mobile", mobile_api.get_certification),
	# v0.113.0. THE OTHER HALF OF ITEM 11. The block above says the three
	# `update_*` tools are "DELIBERATELY ABSENT" because moving a title is a desk
	# act with a document open. That sentence was about `convey_parcel` and it was
	# never true of the acreage: the block registered at six in the morning with
	# its size guessed is corrected by the person who guessed it, standing on it.
	#
	# `update_farm_location` IS THE CREATE SHEET REOPENED — every argument on it is
	# one `CreateLocationSheet` already collects — and it is polymorphic for the
	# same reason the create is: the handset has ONE sheet for four registers, and
	# `tools/locations.py` is where that uniform sheet becomes four different
	# documents. The register's own `update_` tool does the write, so the parcel
	# acreage rule, the zone-number clash and the derived `organic_certified` all
	# still hold. NEITHER ROUTE RENAMES ANYTHING: all four registers build the
	# docname from the name column and all four tools refuse to re-key.
	#
	# `delete_farm_location` IS THE ONLY IRREVERSIBLE ROUTE ON THIS SURFACE, and it
	# is here because the duplicate is MADE here. v0.98.0 put a location register in
	# the hands of everybody with the role and gave nobody, on any transport, a way
	# to take a row out of it — so "Block 3" and "Block 3 " are in every picker on
	# the farm forever. FOUR CHECKS AND NOTHING RELAXED FOR THE PHONE: a child
	# register, a plain Link, ANY task/spray record/observation/inspection naming it
	# through a dynamic link, or an attached file, and it refuses with the count and
	# eight examples. In practice it can only remove a row nothing has touched.
	#
	# THE `force_check_…` FLAGS ARE ABSENT FROM THE WRAPPER'S SIGNATURE, so `bind`
	# drops them and no body can turn a check off. They exist on the MCP tool, for
	# an operator with the Desk open. The one that matters is `force_check_activity`:
	# a dynamic link is two plain columns to a database, so Frappe's own link
	# integrity does not see it and that check is the only thing standing there.
	#
	# BOTH CARRY `guard.require_location_role` — Farm Manager, the same gate as the
	# five creates — and both prove the record's entity with `_scoped_location`
	# rather than `guard.require_scoped_doc`, which reads `company` and would have
	# passed every docname on the bench.
	Route("/mobile", mobile_api.update_farm_location),
	Route("/mobile", mobile_api.delete_farm_location),
	# v0.113.0. THE FIVE ORG MASTERS, fifteen routes. `tools/org.py` has had
	# create/list/update for all five since it was written and NOT ONE of them had a
	# route, so the hiring wizard could offer the site's five designations and, the
	# day the farm hired its first mechanic, had no way to add a sixth —
	# `create_employee` refuses a designation naming no record, which is right, and
	# the register it refuses against was unreachable from the only device in the
	# orchard.
	#
	# THE READS ARE OPEN ON ENROLMENT AND THE WRITES ARE HR. That split is
	# `list_onboarding_reference_data`'s, argued there at length: a job title, a
	# camp name and an employment class are not facts about a person, and a wizard
	# has to be able to OFFER the list it is about to refuse a value against. What
	# these add beyond that call is the live headcount, which is what makes "is this
	# safe to rename" answerable.
	#
	# `personnel.require_hr_role` RUNS IN THE WRAPPER AS WELL AS IN THE TOOL. The
	# wrapper's runs before anything is read and is what the audit row records; the
	# tool's is what protects the MCP transport, where there is no wrapper.
	#
	# THE PAY COLUMNS ON `Employee Grade` ARE ABSENT FROM EVERY SIGNATURE, so this
	# table's argument filter makes `default_base_pay`, `default_salary_structure`
	# and `default_leave_policy` unreachable rather than merely refused. One value
	# there reaches everybody on the band.
	#
	# NO `delete_` FOR ANY OF THE FIVE, and not as an omission: none of the five has
	# a delete tool at all. Frappe HR's own answer for a master nobody should pick
	# any more is `disabled` on Department and a rename on the rest, and `update_`
	# carries both.
	Route("/mobile", mobile_api.list_designations),
	Route("/mobile", mobile_api.create_designation),
	Route("/mobile", mobile_api.update_designation),
	Route("/mobile", mobile_api.list_departments),
	Route("/mobile", mobile_api.create_department),
	Route("/mobile", mobile_api.update_department),
	Route("/mobile", mobile_api.list_branches),
	Route("/mobile", mobile_api.create_branch),
	Route("/mobile", mobile_api.update_branch),
	Route("/mobile", mobile_api.list_employment_types),
	Route("/mobile", mobile_api.create_employment_type),
	Route("/mobile", mobile_api.update_employment_type),
	Route("/mobile", mobile_api.list_employee_grades),
	Route("/mobile", mobile_api.create_employee_grade),
	Route("/mobile", mobile_api.update_employee_grade),
	# v0.123.0. The FSMA/HACCP registers. The monitoring log is the one a phone
	# is actually standing in front of — a CCP check is written at the line, on
	# the hour, by whoever is there. Reads open on enrolment; every write and
	# the recall drill carry `guard.require_dispatch_role`.
	Route("/mobile", mobile_api.get_corrective_action_record),
	Route("/mobile", mobile_api.get_food_safety_dashboard),
	Route("/mobile", mobile_api.get_food_safety_plan),
	Route("/mobile", mobile_api.get_hazard_analysis),
	Route("/mobile", mobile_api.get_monitoring_record),
	Route("/mobile", mobile_api.get_preventive_control),
	Route("/mobile", mobile_api.get_recall_plan),
	Route("/mobile", mobile_api.get_supplier_verification),
	Route("/mobile", mobile_api.get_verification_record),
	Route("/mobile", mobile_api.list_corrective_action_records),
	Route("/mobile", mobile_api.list_food_safety_plans),
	Route("/mobile", mobile_api.list_hazard_analyses),
	Route("/mobile", mobile_api.list_monitoring_records),
	Route("/mobile", mobile_api.list_preventive_controls),
	Route("/mobile", mobile_api.list_recall_plans),
	Route("/mobile", mobile_api.list_supplier_verifications),
	Route("/mobile", mobile_api.list_verification_records),
	Route("/mobile", mobile_api.create_corrective_action_record),
	Route("/mobile", mobile_api.create_food_safety_plan),
	Route("/mobile", mobile_api.create_hazard_analysis),
	Route("/mobile", mobile_api.create_monitoring_record),
	Route("/mobile", mobile_api.create_preventive_control),
	Route("/mobile", mobile_api.create_recall_plan),
	Route("/mobile", mobile_api.create_supplier_verification),
	Route("/mobile", mobile_api.create_verification_record),
	Route("/mobile", mobile_api.update_corrective_action_record),
	Route("/mobile", mobile_api.update_food_safety_plan),
	Route("/mobile", mobile_api.update_hazard_analysis),
	Route("/mobile", mobile_api.update_preventive_control),
	Route("/mobile", mobile_api.update_recall_plan),
	Route("/mobile", mobile_api.update_supplier_verification),
	# v0.123.0. The lot register and its critical tracking events. The lot code
	# is on the bin in front of the picker, so the reads are open; creating a
	# lot or filing a CTE is a foreman's act.
	Route("/mobile", mobile_api.get_lot_timeline),
	Route("/mobile", mobile_api.get_traceability_lot),
	Route("/mobile", mobile_api.list_traceability_lots),
	Route("/mobile", mobile_api.recall_drill),
	Route("/mobile", mobile_api.trace_lot_backward),
	Route("/mobile", mobile_api.trace_lot_forward),
	Route("/mobile", mobile_api.create_traceability_lot),
	Route("/mobile", mobile_api.index_lot_events),
	Route("/mobile", mobile_api.record_cte),
	# v0.123.0. Both directions of the trace, open on enrolment: 'where did this
	# come from' is the question asked with the box in hand.
	Route("/mobile", mobile_api.trace_backward),
	Route("/mobile", mobile_api.trace_forward),
	# v0.123.0. The sensor register. A phone standing in the block is the thing
	# that notices a dead battery, and `create_iot_reading` is how a manual
	# reading gets filed when the radio did not make it.
	Route("/mobile", mobile_api.get_device_readings),
	Route("/mobile", mobile_api.get_iot_device),
	Route("/mobile", mobile_api.list_iot_devices),
	Route("/mobile", mobile_api.list_iot_readings),
	Route("/mobile", mobile_api.create_iot_device),
	Route("/mobile", mobile_api.create_iot_reading),
	Route("/mobile", mobile_api.update_iot_device),
	# v0.123.0. Residue limits and the IPM lookup. `get_mrl_for_chemical_crop_market`
	# is the one that decides whether a load may ship to a market at all.
	Route("/mobile", mobile_api.get_ipm_reference),
	Route("/mobile", mobile_api.get_mrl_for_chemical_crop_market),
	Route("/mobile", mobile_api.get_mrl_record),
	Route("/mobile", mobile_api.list_mrl_records),
	Route("/mobile", mobile_api.create_mrl_record),
	Route("/mobile", mobile_api.update_mrl_record),
	# v0.123.0. The compaction reference behind the map's red and green. The
	# profile table is site-wide and carries no `company` column; the docstrings
	# say so rather than implying a scope that is not there.
	Route("/mobile", mobile_api.list_soil_compaction_profiles),
	Route("/mobile", mobile_api.assign_soil_profile),
	Route("/mobile", mobile_api.create_soil_compaction_profile),
	Route("/mobile", mobile_api.update_soil_compaction_profile),
	# v0.123.0. The variety care recipe, keyed on crop rather than on a document.
	Route("/mobile", mobile_api.get_variety_care_recipe),
	# v0.123.0. The competitive registers, and the only block here gated on
	# `personnel.require_hr_role()` at BOTH ends. What this farm thinks a rival is
	# worth, and where it is weak, is a holding-company fact; a field worker's
	# handset is enrolled for their own work and not for that.
	Route("/mobile", mobile_api.get_acquisition_target),
	Route("/mobile", mobile_api.get_competitive_move),
	Route("/mobile", mobile_api.get_market_participant),
	Route("/mobile", mobile_api.list_acquisition_targets),
	Route("/mobile", mobile_api.list_competitive_moves),
	Route("/mobile", mobile_api.list_market_participants),
	Route("/mobile", mobile_api.create_acquisition_target),
	Route("/mobile", mobile_api.create_competitive_move),
	Route("/mobile", mobile_api.create_market_participant),
	Route("/mobile", mobile_api.update_acquisition_target),
	Route("/mobile", mobile_api.update_competitive_move),
	Route("/mobile", mobile_api.update_market_participant),
	# v0.124.0. `list_tasks_by_location` shipped as a tool in v0.122.0 and was
	# missed by v0.123.0's routing pass, which swept the HACCP, traceability,
	# sensor and market registers and not `fieldwork`. It is open on enrolment
	# for the reason its wrapper gives: it is a third reader of `list_my_tasks`
	# and `list_available_for_me`, both of which are already mounted open, and
	# it answers for the authenticated caller alone.
	Route("/mobile", mobile_api.list_tasks_by_location),
	# v0.130.0. THE STRATEGY REGISTER, which v0.123.0's sweep left behind when it
	# took the competitive ones beside it. `tools/strategy.py` was the last whole
	# module in this app with no door onto this transport at all.
	#
	# EVERY ONE OF THE EIGHT CARRIES THE HR GATE, reads included, and that is a
	# departure from the rule that reads open on enrolment. The rule is about the
	# CALLER'S OWN WORK — a task list, a shift, a re-entry interval — and a
	# strategic plan is not that. It is the vision, the SWOT, the command
	# structure and the exit strategy, which is to say what the owners intend to
	# do with the business, and a picker is entitled to their own work rather
	# than to that. The competitive registers directly above take the same gate
	# for the same reason and this is the other half of that decision.
	#
	# THE SWITCHES DO NOT REACH THIS TRANSPORT. `allow_list_strategic_plans` and
	# its seven siblings gate `registry.dispatch`, and nothing on this path goes
	# through `dispatch` — `api/mobile.py` calls the tool functions directly, so
	# the switch is never consulted here. The gate above IS the control on this
	# door, which is why it is an HR role and not enrolment.
	Route("/mobile", mobile_api.list_strategic_plans),
	Route("/mobile", mobile_api.get_strategic_plan),
	Route("/mobile", mobile_api.list_strategic_objectives),
	Route("/mobile", mobile_api.get_strategic_objective),
	Route("/mobile", mobile_api.create_strategic_plan),
	Route("/mobile", mobile_api.update_strategic_plan),
	Route("/mobile", mobile_api.create_strategic_objective),
	Route("/mobile", mobile_api.update_strategic_objective),
	# v0.131.0. THE CREW DRILL-DOWN — the three calls behind "the farm today".
	#
	# THE READS CARRY THE DISPATCH GATE AND THAT IS THE DEPARTURE. Everywhere
	# else on this transport a read opens on enrolment, because the reads are the
	# CALLER'S OWN WORK — their tasks, their shift, the re-entry interval on the
	# block they are walking into. These two are not: they name other people,
	# where they are, who they are under and what they have picked. A picker's
	# own day is still answered by `list_my_tasks` and `get_shift_crew_timeline`
	# and neither of those moves.
	#
	# THEY ARE NOT HR-GATED, WHICH IS THE OTHER HALF OF THE SAME DECISION.
	# Running the crew IS the Foreman's job on this farm — there is no personnel
	# office to refer them to — so the gate that fits is the one that already
	# means "foreman or above" and not the one that means "the office". The
	# strategy register directly above takes the HR gate because it is what the
	# OWNERS intend to do with the business; a crew list is what the foreman is
	# holding a phone to find out.
	#
	# THE SECOND SCOPE IS A DEPARTMENT AND IT IS NEW ON THIS SURFACE. Entity
	# scoping is the whole of what every other shift route applies, and on a
	# single-company farm with four departments it separates nothing — all four
	# foremen can reach every crew. `crew_view._visible_departments` draws the
	# line: a Foreman sees their own department and its children, a Farm Manager
	# sees every department in the entities they may reach, and an account with
	# no linked Employee gets an EMPTY answer with the sentence saying why rather
	# than the whole farm.
	Route("/mobile", mobile_api.get_crew_overview),
	Route("/mobile", mobile_api.get_worker_detail),
	# The light read beside the nested one. `get_crew_overview` carries every
	# worker on every crew with their task and their bucket count, which is what
	# a drill-down screen wants and NOT what a phone polling "what is running"
	# every two minutes should be pulling. Both go through one shared read inside
	# `tools/crew_view.py`, so the board and the drill-down cannot come to
	# disagree about which shifts are open or who may see them.
	Route("/mobile", mobile_api.list_active_shifts),
	# The write. It ends somebody else's shift and pays their crew, so the
	# dispatch gate is the least it could carry — and it is fenced by a staleness
	# threshold as well, so it cannot become a signature-free `end_shift`.
	Route("/mobile", mobile_api.end_stale_shift),
	# v0.141.0, `SERVER_CHANGES.md` item 36. THE THIRD ENDING, AND THE ONE A
	# PHONE COULD NOT REACH. `cancel_shift` has been in `tools/shifts.py` for
	# many releases and on the MCP registry only: probed against the deployed
	# sidecar, `/mobile/end_shift` and `/mobile/clock_out_worker` answer 401 with
	# no credential and `/mobile/cancel_shift` answered the sidecar's own 404.
	# `MobileAPI.cancelShift` was bound in the app the whole time and the crew
	# drill-down's long-press menu offered the row against it.
	#
	# THE MISSING ROUTE WAS NOT A MISSING BUTTON, IT WAS PAYROLL. A shift formed
	# and then not worked has one correct ending, and a foreman who cannot find
	# it either leaves the shift open — which holds its whole crew off every
	# other roster, because `start_shift` refuses a second open shift for the
	# same person — or closes it with `end_shift`, which attests a day happened
	# and writes an Attendance row per crew member for a day nobody worked.
	#
	# Dispatch-gated beside `end_stale_shift` for the same reason: it ends a
	# shift a whole crew is rostered on.
	Route("/mobile", mobile_api.cancel_shift),
	# v0.143.0. THE FOUR IRRIGATION READS, AND UNTIL THIS RELEASE THE ONLY WAY A
	# PHONE COULD LEARN ANYTHING ABOUT A VALVE WAS TO POINT A CAMERA AT ITS TAG.
	# `scan_valve` has been on this table since v0.117.0 and answers the whole
	# valve screen; `tools/valves.py` has carried the other three since the same
	# release and `tools/farm.py` has carried `get_irrigation_zone` since v0.9.0,
	# all four on the MCP registry alone. Probed against the deployed sidecar on
	# 2026-08-29: `/mobile/scan_valve` and `/mobile/set_zone_boundary` answer 401
	# with no credential, and all four paths below answered the sidecar's own 404.
	#
	# THE MISSING ROUTES WERE THE SET-WALK. An irrigator opens a line of laterals
	# in order and comes back hours later to shut them, and the question between
	# those two acts — "which of these is still open" — is a list rather than a
	# scan. A tag that has weathered, been painted over or gone under a season's
	# growth is exactly the valve they need to read and the one the camera cannot
	# give them. So is a valve somebody scanned an hour ago and now wants to check
	# from the truck.
	#
	# OPEN ON ENROLMENT, ALL FOUR, WHICH IS THIS SURFACE'S RULE FOR A READ ABOUT
	# THE GROUND SOMEBODY IS STANDING IN. This table already says of the route
	# above them that a valve "is what a worker in a block sees and a foreman at a
	# desk does not"; a read gated on dispatch would take the irrigator's own work
	# off the handset of the person doing it. Nothing here writes — `scan_valve`'s
	# opt-in toggle remains the only way a phone moves a valve, and it keeps its
	# `WRITE_LIMIT`.
	#
	# `set_zone_boundary` IS NOT ON THIS LIST BECAUSE IT WAS NEVER MISSING. It has
	# been mounted since v0.110.0, `MobileAPI.setZoneBoundary` names it, and the
	# same probe got 401 rather than 404 from it. A 404 reported against that path
	# is a GET — the sidecar answers 405 to those and says why — or a deployment
	# behind this branch.
	Route("/mobile", mobile_api.list_irrigation_valves),
	Route("/mobile", mobile_api.get_irrigation_valve),
	Route("/mobile", mobile_api.get_valve_runtime),
	Route("/mobile", mobile_api.get_irrigation_zone),
)

#: Path → Route. Built once at import; there is nothing dynamic about it.
BY_PATH = {route.path: route for route in ROUTES}


def accepted_arguments(handler) -> set:
	"""The body keys this method will accept. Everything else is dropped.

	Read off the signature with `inspect`, which follows `functools.wraps`
	through `guard.endpoint` to the wrapper's own parameter list — so the answer
	is always what the function actually declares, and a signature change cannot
	leave a stale allowlist behind it.

	A `**kwargs` in one of them would make this return the empty set and take
	every argument away, which is the safe direction to fail and is asserted as
	a property of the surface in `test_farmops_api.py`. None of them has one, on
	purpose.
	"""
	try:
		parameters = inspect.signature(handler).parameters
	except (TypeError, ValueError):  # pragma: no cover - a builtin, not one of ours
		return set()
	if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
		return set()
	return {
		name
		for name, parameter in parameters.items()
		if name != "user"
		and parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
	}


def bind(route, body: dict) -> dict:
	"""The request body, reduced to the keys this method declares.

	`_auth` goes whatever else happens: it is envelope rather than argument, it
	is the one key in a mobile body that carries a live credential, and a call
	that reached this transport did not need it — but the app sends it on every
	request (v0.17.2 carries the pair three ways and does not know which door it
	came in), so it arrives here on every single call.
	"""
	accepted = accepted_arguments(route.handler)
	return {
		key: value for key, value in (body or {}).items() if key in accepted and key != fallback_auth.BODY_KEY
	}
