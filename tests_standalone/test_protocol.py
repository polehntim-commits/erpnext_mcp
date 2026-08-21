# SPDX-License-Identifier: MIT
"""JSON-RPC and MCP handshake behaviour."""

import json

from erpnext_mcp import protocol, registry

from .fixtures import SeededTestCase
from .harness import STORE


class Handshake(SeededTestCase):
	def test_initialize_echoes_a_supported_version(self):
		for version in protocol.SUPPORTED_PROTOCOL_VERSIONS:
			body, _ = self.call("initialize", {"protocolVersion": version})
			self.assertEqual(body["result"]["protocolVersion"], version)

	def test_initialize_answers_with_its_preferred_version_when_unknown(self):
		body, _ = self.call("initialize", {"protocolVersion": "1999-01-01"})
		self.assertEqual(body["result"]["protocolVersion"], protocol.PREFERRED_PROTOCOL_VERSION)

	def test_initialize_advertises_tools_only(self):
		body, _ = self.call("initialize", {"protocolVersion": "2025-06-18"})
		self.assertEqual(list(body["result"]["capabilities"]), ["tools"])

	def test_initialize_carries_usable_instructions(self):
		"""The instructions are how a client learns to orient itself on an
		unfamiliar site, so they must name the tool that does that."""
		body, _ = self.call("initialize", {"protocolVersion": "2025-06-18"})
		self.assertIn("get_company_topology", body["result"]["instructions"])

	def test_server_info_reports_the_app_version(self):
		from erpnext_mcp import __version__

		body, _ = self.call("initialize", {})
		self.assertEqual(body["result"]["serverInfo"]["version"], __version__)

	def test_the_app_version_matches_the_changelog(self):
		"""v0.2.0 tagged and shipped with `__version__` still reading "0.1.0", so
		every client's handshake reported the wrong server version. Comparing the
		two things a release has to keep in step costs one test.

		THE `v?` IS LOAD-BEARING. v0.69.0 wrote its heading as `## v0.69.0`
		while every earlier one read `## 0.68.0`, so the pattern skipped it and
		matched the release before — and this test went on comparing
		`__version__` against a two-releases-old number instead of failing. A
		guard that can be switched off by a typo in the document it guards is
		not a guard, so the prefix is now optional and the newest heading is
		always the one read."""
		import pathlib
		import re

		from erpnext_mcp import __version__

		changelog = pathlib.Path(__file__).resolve().parents[1] / "CHANGELOG.md"
		latest = re.search(r"^## v?(\d+\.\d+\.\d+)", changelog.read_text(), re.M)
		self.assertIsNotNone(latest, "no version heading in CHANGELOG.md")
		self.assertEqual(
			__version__,
			latest.group(1),
			"erpnext_mcp.__version__ and the newest CHANGELOG heading disagree",
		)


class Methods(SeededTestCase):
	def test_unknown_method_is_32601(self):
		body, _ = self.call("wat")
		self.assertEqual(body["error"]["code"], protocol.METHOD_NOT_FOUND)

	def test_unadvertised_capabilities_say_so(self):
		for method in ("resources/list", "prompts/list"):
			body, _ = self.call(method)
			self.assertEqual(body["error"]["code"], protocol.METHOD_NOT_FOUND)
			self.assertIn("tools only", body["error"]["message"])

	def test_notification_for_an_unknown_method_is_silent(self):
		"""A notification has no id, so there is nobody to answer."""
		from erpnext_mcp import mcp

		self.request({"jsonrpc": "2.0", "method": "wat"})
		self.assertEqual(mcp.handle().status_code, 202)

	def test_missing_method_is_invalid_request(self):
		body, _ = self.call(None)
		self.assertEqual(body["error"]["code"], protocol.INVALID_REQUEST)

	def test_non_object_params_is_invalid_params(self):
		from erpnext_mcp import mcp

		self.request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []})
		body = json.loads(mcp.handle().get_data(as_text=True))
		self.assertEqual(body["error"]["code"], protocol.INVALID_PARAMS)

	def test_tools_call_without_a_name_is_invalid_params(self):
		body, _ = self.call("tools/call", {"arguments": {}})
		self.assertEqual(body["error"]["code"], protocol.INVALID_PARAMS)

	def test_tools_call_with_non_object_arguments_is_invalid_params(self):
		body, _ = self.call("tools/call", {"name": "search_accounts", "arguments": "cash"})
		self.assertEqual(body["error"]["code"], protocol.INVALID_PARAMS)

	def test_request_id_is_echoed_including_a_string_id(self):
		body, _ = self.call("ping", request_id="abc-1")
		self.assertEqual(body["id"], "abc-1")

	def test_tool_failure_is_a_result_not_a_jsonrpc_error(self):
		"""A model needs to tell "you called this wrong" from "the call itself was
		malformed" — only the second is a JSON-RPC error."""
		body, status = self.call(
			"tools/call", {"name": "get_account_balance", "arguments": {"account": "nope"}}
		)
		self.assertEqual(status, 200)
		self.assertNotIn("error", body)
		self.assertTrue(body["result"]["isError"])


class Catalogue(SeededTestCase):
	def test_lists_every_available_tool_when_all_are_enabled(self):
		self.configure(**{f"allow_{name}": 1 for name in registry.TOOLS}, enabled=1)
		body, _ = self.call("tools/list")
		self.assertEqual(
			sorted(tool["name"] for tool in body["result"]["tools"]),
			sorted(name for name in registry.TOOLS if registry.is_available(name)),
		)

	def test_default_catalogue_is_every_available_read_tool_plus_the_one_installer(self):
		"""Straight out of the box: everything readable, and one thing writable.

		The one is `install_compliance_fields`, which ships enabled — the single
		exception to "mutating tools default off", named and argued for in
		`registry.DEFAULT_ON_MUTATING_TOOLS`. It is asserted here EXPLICITLY rather
		than filtered out, because the catalogue a fresh install advertises is the
		thing this test exists to pin down, and a second default-on write tool
		should break it.
		"""
		body, _ = self.call("tools/list")
		names = sorted(tool["name"] for tool in body["result"]["tools"])
		expected = sorted(
			[name for name in registry.READ_TOOLS if registry.is_available(name)]
			+ [name for name in registry.DEFAULT_ON_MUTATING_TOOLS if registry.is_available(name)]
		)
		self.assertEqual(names, expected)
		self.assertEqual(
			set(names) & set(registry.MUTATING_TOOLS),
			{"install_compliance_fields"},
		)

	def test_hr_tools_are_absent_without_the_hrms_app(self):
		"""A tool that can never work here is not a tool that fails — it is a tool
		that does not exist, and the catalogue should say so."""
		self.configure(**{f"allow_{name}": 1 for name in registry.TOOLS}, enabled=1)
		body, _ = self.call("tools/list")
		names = {tool["name"] for tool in body["result"]["tools"]}
		self.assertNotIn("get_leave_balance", names)
		self.assertNotIn("list_employees", names)

	def test_hr_tools_appear_once_hrms_is_installed(self):
		STORE.installed_apps.append("hrms")
		self.configure(**{f"allow_{name}": 1 for name in registry.TOOLS}, enabled=1)
		body, _ = self.call("tools/list")
		names = {tool["name"] for tool in body["result"]["tools"]}
		self.assertIn("get_leave_balance", names)
		self.assertIn("get_attendance_summary", names)

	def test_an_unavailable_tool_says_it_cannot_be_switched_on(self):
		message = self.tool_error("list_employees")
		self.assertIn("not available on this site", message)
		self.assertIn("hrms", message)
		self.assertIn("not something an operator can switch on", message)

	def test_disabled_tool_is_not_advertised_at_all(self):
		self.configure(enabled=1, allow_search_accounts=0)
		body, _ = self.call("tools/list")
		names = [tool["name"] for tool in body["result"]["tools"]]
		self.assertNotIn("search_accounts", names)

	def test_every_tool_has_a_schema_and_annotations(self):
		self.configure(**{f"allow_{name}": 1 for name in registry.TOOLS}, enabled=1)
		body, _ = self.call("tools/list")
		for tool in body["result"]["tools"]:
			with self.subTest(tool=tool["name"]):
				self.assertEqual(tool["inputSchema"]["type"], "object")
				self.assertFalse(tool["inputSchema"]["additionalProperties"])
				self.assertTrue(tool["description"])
				self.assertIn("readOnlyHint", tool["annotations"])

	def test_read_only_hint_matches_the_mutating_flag(self):
		"""Derived, not hand-written, so a write tool cannot advertise itself as
		safe."""
		for name, spec in registry.TOOLS.items():
			with self.subTest(tool=name):
				self.assertEqual(spec["annotations"]["readOnlyHint"], not spec["mutating"])

	def test_mutating_descriptions_announce_themselves(self):
		for name in registry.MUTATING_TOOLS:
			with self.subTest(tool=name):
				self.assertIn("MUTATING", registry.TOOLS[name]["description"])

	def test_required_arguments_are_declared_in_the_schema(self):
		self.assertEqual(
			registry.TOOLS["create_journal_entry"]["inputSchema"]["required"],
			["company", "posting_date", "accounts", "user_remark"],
		)

	def test_catalogue_is_three_hundred_sixty_two_tools_one_hundred_sixty_seven_read_one_hundred_ninety_five_write(
		self,
	):
		"""v0.13.0 added two writes: convey_parcel and update_journal_entry_party,
		both corrections to records that already exist, which is why neither is a
		read. v0.14.0 added eight — six writes and two reads.

		The six writes: three that move a file onto the site a piece at a time
		(stage_file_chunk, commit_staged_file, cancel_staged_upload),
		bulk_wire_default_accounts, create_check_print_format, and
		regenerate_governance_document_pdf. The two reads are the ones you call
		when something has gone wrong and you need to see rather than change it:
		list_staged_uploads for an upload that died partway, and
		investigate_je_gl_link for a voucher and a ledger that disagree.

		v0.15.0 ADDED THIRTY-TWO, and they are the compliance framework:

		  * two for the fields that go ON the operational doctypes —
		    get_compliance_field_map reads the table, install_compliance_fields
		    writes the columns;
		  * nineteen over the four external-evidence doctypes (Compliance Policy,
		    Certification, Regulatory Filing, Audit Event), eight of which are
		    reads;
		  * seven for the kairotic compliance calendar —
		    get_compliance_calendar, list_compliance_rules and get_audit_readiness
		    read it, refresh_compliance_alerts rebuilds it, and snooze_alert,
		    dismiss_alert and dismiss_alert_bulk are the three different ways
		    something comes off it;
		  * two for audit packets, one of which assembles a PDF and files it;
		  * two for the Journal Entry attribution drift v0.13.0 left behind —
		    find_drifted_je_attributions reads it, repair_drifted_je_attributions
		    fixes it in a batch.

		v0.16.0 ADDED TWENTY-THREE, and they are the operational half of the same
		framework — the half that lets somebody be SENT to fix what Sprint 7 could
		only report:

		  * eleven for Farm Task Dispatch — four reads (the pool, one worker's
		    load, the board, one task in full), six writes for the state machine
		    (create, assign, claim, start, complete, reject), and
		    generate_tasks_from_compliance_alerts, which is the bridge that turns
		    open alerts into dispatchable work;
		  * twelve over the three compliance records a completion produces
		    (Housing Inspection, Detector Test, Water Test) — list, get, create
		    and update apiece, six of which are reads.

		v0.17.0 ADDED SIXTEEN, and they are what makes a phone outside the LAN a
		safe thing to point at all of the above:

		  * three for mobile accounts — list_mobile_users reads the roster and
		    every way it has drifted, create_mobile_user and revoke_mobile_user
		    write it. The role says what kind of work; the Company User
		    Permissions this app writes say whose;
		  * four for the credential — generate_api_token and revoke_api_token
		    mint and destroy it, generate_mobile_login_qr puts it on a scannable
		    card, and get_current_user_context is what the phone calls first to
		    find out who it is;
		  * two for the transport, both READ, and there is deliberately no third
		    that flips it: validate_public_endpoint asks from outside whether the
		    Funnel is up and the token gate is holding, get_tailscale_funnel_config
		    asks from inside what this machine is serving;
		  * seven mobile-ergonomic wrappers over Sprint 8's dispatch tools — four
		    reads and three writes — which add the worker resolved from the
		    authenticated request, a screen-shaped payload, and NOTHING ELSE. Every
		    refusal in them comes from the tool underneath, because it IS the tool
		    underneath.

		v0.17.1 ADDED ONE, and only one, because a hotfix that grew the catalogue
		would be a release nobody could review. `onboard_employee` is an
		ORCHESTRATOR over tools that already exist — the Employee, the paperwork
		through attach_file_to_document, the login through create_mobile_user, the
		first-day tasks through create_farm_task. It adds no rule any of them does
		not already enforce. Its whole reason to exist is that the paperwork has to
		land ON THE EMPLOYEE RECORD and not in the governance archive, and a
		checklist cannot make that mistake impossible.

		v0.18.1 ADDED THREE, ALL WRITES, AND THEY CLOSE THE GAP v0.18.0 OPENED. The
		mobile app worked end to end and then `list_my_tasks` refused every account
		— correctly — because the Farm Ops methods scope work by EMPLOYEE and this
		app could not create, edit or link one. It could make the User, the role,
		the entity scoping, the grant, the credential and the QR: six things, and
		not the one that makes the other six useful.

		  * `create_employee` writes fourteen identity and assignment fields and
		    refuses everything else by name — payroll, tax and banking with their
		    own message, because each has a form, an approval and a retention rule
		    this app knows nothing about;
		  * `update_employee` changes the same fourteen on a record that exists, and
		    reports field by field what actually moved;
		  * `link_employee_to_user` sets the one field that turns a working
		    credential into a working task board, and reports whether the phone will
		    NOW work rather than merely whether the field was written.

		`onboard_employee` grew no siblings — it gained the link step it was
		missing, and delegates its creation to `create_employee`, so there is still
		exactly one implementation of what an Employee record may contain.

		v0.19.0 ADDED FOUR — two reads, two writes — and they are the first pull
		from the HR roadmap. Eleven compliance rules watched certificates,
		policies, cabins, water, filings and audits, and not one of them watched
		TRAINING: what WPS asks for every twelve months, what Oregon's heat rule
		asks for annually before the first hot shift, what FSMA Subpart C asks for
		on hiring and periodically, and what a GAP auditor asks for by name with
		the signature attached. All of it lived in a binder.

		  * `record_training` files one event tagged with every regime it answers,
		    because one afternoon in a shed can satisfy four audits and filing it
		    four times produces four records that disagree by August;
		  * `list_trainings` filters by regime, which is how an audit packet is
		    assembled, and reports the §112.161 elements each record is missing;
		  * `get_training` answers the retention question — five years where any
		    tag is NOP, three for OR-OSHA, two for FSMA and WPS, longest governs —
		    with the citation beside the number;
		  * `sign_training_supervisor_review` is a SEPARATE write because
		    §112.161(b) asks for a review "after the record is made", and it is
		    the requirement USDA GAP does not have and FDA cites most.

		The twelfth compliance rule (`training_expiring`) and the training section
		on every audit packet came with them and are not tools, which is why the
		catalogue grew by four and the release by considerably more.

		v0.19.3 ADDED TEN — six writes and four reads — and they are one workflow
		with one actor. Compliance anchors to a SHIFT rather than to a task,
		because Oregon OSHA does not ask what the temperature was when one job
		closed, it asks whether the July 15 shift complied with OAR 437-004-1131
		from start to finish, and only a record spanning the exposure period can
		answer that.

		  * `start_shift` forms the crew at a place and starts the period.
		    THE FOREMAN FORMS IT and there is deliberately no clock-in tool: the
		    rule puts the water, shade, rest-cycle and observation obligations on
		    a NAMED responsible person, and a crew of thirty each clocking
		    themselves in is a shift with nobody responsible for the record;
		  * `add_worker_to_shift` and `remove_worker_from_shift` amend it. The
		    second SETS `left_at` rather than deleting the row, because the row is
		    the only record that this person was on the shift at all — which is
		    what a wage claim turns on;
		  * `log_shift_event` records what the foreman did about the conditions,
		    at the moment it happened. The timeline is the evidence; the heat
		    record is only the claim;
		  * `end_shift` closes it with a signature that is REQUIRED — an unsigned
		    close is an UPDATE setting a timestamp, and §112.161(b) asks for a
		    review dated AND signed — and writes one Attendance record per crew
		    member for that person's OWN span;
		  * `create_heat_exposure_event` is the -1131 record, one per shift,
		    checked against the training register as of the day of the shift;
		  * `list_shifts`, `get_shift`, `list_heat_exposure_events` and
		    `get_heat_exposure_event` read it all back.

		The thirteenth compliance rule (`supervisor_review_lapsed`), the
		Attendance bridge and the four new doctypes came with them and are not
		tools, which is again why the catalogue grew by ten and the release by
		considerably more.

		v0.19.4 ADDED FIVE — two writes and three reads — and they are the hands
		to a mechanism that is mostly a schedule. A fifteen-minute cron documents
		every open shift without anybody asking, because a timeline is only
		evidence if it was written while things were happening. What the cron
		cannot do is the rest:

		  * `fetch_weather_now` is for the foreman standing in a block on a day
		    that turned, who wants the conditions on the record this minute
		    rather than in eleven. It bypasses the cache, which is the point;
		  * `backfill_weather_for_shift` documents every shift that ran before
		    the service was switched on, from Open-Meteo's archive, idempotently
		    and at the archive's own hourly granularity;
		  * `list_shifts_missing_weather` is the worklist for the second one;
		  * `get_weather_timeline` answers 'how hot was it when the break was
		    called' without returning the whole shift;
		  * `get_weather_settings` reads the thresholds back — and there is no
		    write counterpart on purpose, because a model that could raise the
		    heat threshold past anything Oregon produces would leave a site that
		    behaves normally and never says anything is wrong.

		The Threshold Crossed events, the Heat Exposure Event maxima computed off
		the timeline, the compliance-event snapshots and the Weather Settings
		doctype came with them and are not tools — which is why the catalogue grew
		by five and the release by considerably more.

		v0.19.5 ADDED SIX — four writes and two reads — and they are the first
		tools in this run that no regulator asked for. Sustainable CF/Acre is
		(normalized operating cash flow − maintenance capex) ÷ productive acres,
		and it exists because headline OCF lies in two directions at once: it is
		flattered by money that will not come in again, and flattered AGAIN by
		maintenance that was not done.

		  * `create_normalization_adjustment` proposes one add-back or
		    subtraction and CREATES A DRAFT, always. Nothing in it can make the
		    adjustment count. That is the whole compliance posture: finding a
		    non-recurring item in a ledger nobody reads line by line is worth a
		    great deal and is something a model is good at, and deciding that it
		    will not recur is a judgement with a lender on the other end of it;
		  * `approve_normalization_adjustment` is the human half, with a
		    signature that has no bypass and an approval timestamp WRITTEN rather
		    than taken as input — an approval date somebody can set is one they
		    can set to before the quarter closed;
		  * `reject_normalization_adjustment` refuses one on the record with the
		    reason attached, and the rejection is KEPT, because a register with
		    only its successes in it says nothing about how hard they were to get;
		  * `backfill_asset_capex_type` classifies the history in bulk, dry-run by
		    default, never overwriting a classification somebody made — so a
		    second run finds nothing to do;
		  * `list_normalization_adjustments` reads the register and says which
		    rows actually count;
		  * `get_sustainable_cf_per_acre` returns the KPI WITH EVERY INGREDIENT
		    ITEMIZED, because a normalized figure nobody can inspect is
		    indistinguishable from an arranged one.

		The doctype, the four Asset capex columns, the three Field productive-date
		columns, the direct-method cash flow service and the quarterly dashboard
		chart came with them and are not tools — which is why the catalogue grew
		by six and the release by considerably more.

		v0.19.6 ADDED THREE — two reads and one write — and retrofitted a fourth
		without adding to the count. The release is the WINDOW STANDARD: every
		financial report now defaults to a trailing twelve months, because
		agricultural revenue is aggressively seasonal and two single periods set
		against each other say the operation collapsed in January and recovered
		in September, every year, on every farm.

		  * `get_windowed_report` is the generic entry point and the reason the
		    standard generalizes — a report registered in
		    services/financial_reports.py is reachable through it without another
		    tool, another switch and another catalogue section. A framework whose
		    every KPI costs a tool is a framework with six KPIs in it;
		  * `list_financial_kpi_history` reads the precomputed cache as a plain
		    series, for drawing a line, and reports what is NOT there — a gap is
		    a window nobody has computed, not a period the business earned
		    nothing in;
		  * `recompute_kpi_history` rebuilds that cache, and is the mildest
		    mutating tool in the catalogue: every row it writes is derivable and
		    every row it deletes comes back, so the worst outcome of running it
		    at the wrong moment is time spent;
		  * `get_sustainable_cf_per_acre` is the RETROFIT and adds nothing to the
		    count. It now defaults to TTM; passing v0.19.5's period_start and
		    period_end still returns v0.19.5's exact payload, because that figure
		    is quoted in packs that were sent before the window existed.

		The Financial KPI History doctype, the windowed-report engine, the two
		demonstration computers, the overnight sweep and the retrofitted chart
		came with them and are not tools — which is why the catalogue grew by
		three and the release by considerably more.

		v0.20.1 ADDED EXACTLY ONE, and it is a read:

		  * `list_visits` groups completed task assignments into the trips their
		    handsets recorded. Five cabins closed on one walk is one visit and
		    five completions, and the grouping is the phone's rather than a guess
		    from how close the timestamps are — no threshold gets both an
		    unhurried walk and two fast jobs at opposite ends of a property right.

		THE REST OF THAT RELEASE IS NOT A TOOL AND THAT IS THE POINT OF IT.
		`complete_task_via_mobile` became idempotent — a resubmission whose
		signature matches the completion already on record returns it with
		`x_idempotent: true` instead of a hard error — because a client cannot
		know whether its request landed before the connection dropped, and an
		iPad in an orchard proved it by showing three Failed entries per task on
		work that had been filed the first time. A fix that arrived as a NEW
		tool would have left the broken one in the catalogue for every client
		already calling it. The two new columns on Farm Task Assignment
		(`completion_signature`, `visit_id`) and the backfill patch came with it
		and are not tools either.

		v0.21.0 ADDED TEN — four reads and six writes — and the whole of the
		release is one claim: THE SHAPE OF A VISIT IS DATA. An Inspection Template
		says which sections a worker works through in one trip to one place, what
		evidence each needs and which compliance record each produces, and it is a
		ROW. Adding one is not a release.

		  * `create_inspection_template` authors one, live on the next fetch;
		  * `update_inspection_template` changes one by SUPERSEDING it — a new row
		    at version+1, the old one deactivated and pointing at it, never
		    edited. That is what makes a session from April readable in November,
		    and why a session started against v1 while v2 is being authored is
		    unaffected;
		  * `deactivate_inspection_template` withdraws one with a reason and
		    destroys nothing. There is deliberately no delete;
		  * `start_inspection_session` opens one visit and PINS the version;
		  * `submit_inspection_session` is the one with teeth: it files every
		    section against the pinned version's contract and writes the
		    compliance records the sections promise — separately, at their own
		    cadences, from one walk with one signature;
		  * `list_inspection_templates`, `get_inspection_template`,
		    `list_inspection_sessions` and `get_inspection_session` read it all
		    back. The second is what a handset renders a sectioned form from,
		    which is why a new template needs no app update;
		  * `propose_inspection_template_from_regulation` WAS DECLARED AND
		    REFUSED. It was the surface an AI template proposer would occupy,
		    reserved so the shape was fixed before anything filled it, and inert
		    because at runtime this app is deterministic. It counted as a tool
		    because it had a switch, a schema and a catalogue entry — everything a
		    tool has except an implementation — and a surface an operator cannot
		    see in the settings form is a surface nobody can refuse. v0.37.0
		    filled it, and the count did not move.

		The five doctypes, the Farm Task link, the four seeded templates and the
		rule engine's bundling — several overdue things at one cabin become ONE
		visit rather than three trips — came with them and are not tools, which is
		why the catalogue grew by ten and the release by considerably more.

		v0.22.0 ADDED SEVEN — two reads and five writes — and the release is the
		same claim as v0.21.0's, made about the thing underneath it: THE RULES
		THEMSELVES ARE DATA. A compliance rule used to be a Python function, so
		moving a threshold, correcting a citation or switching a rule off for a
		season was a code change, a release and a deploy. Regulations do not move
		on a release cadence — OR-OSHA renumbered heat illness from -1130 to
		-1131 — and now neither does this.

		  * `create_compliance_rule` authors one. It arrives as a DRAFT and fires
		    nothing, because the approval gate is not a default anybody can pass
		    past;
		  * `approve_compliance_rule` is the only way a rule can be enabled — the
		    DocType refuses `enabled` without an approver and a date — so "a model
		    wrote a rule and it started firing" is a sentence that cannot be true
		    about this app;
		  * `update_compliance_rule` changes one by SUPERSEDING it, exactly as a
		    template is superseded: a new row at version+1, the old one disabled
		    and pointing at it, never edited. A sweep that started against v1
		    finishes against v1, and an alert from April is still explicable in
		    November;
		  * `deactivate_compliance_rule` switches one off with a reason and
		    DISMISSES NOTHING — every alert it raised stays exactly as it was,
		    because switching a rule off is not evidence that anybody did the
		    work. There is deliberately no delete;
		  * `test_compliance_rule` is the read between authoring and approving:
		    it runs the rule down the same code path the sweep takes and reports
		    what it WOULD raise, writing nothing;
		  * `get_compliance_rule` reads one definition in full, including a
		    superseded one — which is how the rule an old alert was raised under
		    stays inspectable;
		  * `propose_compliance_rule` WAS DECLARED AND REFUSED, for the same
		    reasons and on the same terms as v0.21.0's template proposer. AI
		    belongs at authoring time behind a human approval and never in the
		    trigger path, and a surface an operator cannot see in the settings
		    form is a surface nobody can refuse. v0.37.0 filled it, and the count
		    did not move.

		`list_compliance_rules` was RETROFITTED rather than replaced: it now reads
		the records and takes filters, and every key it returned before means what
		it always meant. The Compliance Rule DocType, the declarative evaluator,
		the restricted-Python sandbox and the migration of the thirteen shipped
		rules into records came with them and are not tools, which is why the
		catalogue grew by seven and the release by a great deal more.

		v0.25.0 added three for asset state-change actions: get_available_actions
		and list_asset_state_history are reads, log_asset_state_change is a write.

		v0.26.0 added one mutating tool: report_asset_issue.

		v0.27.0 ADDED FOURTEEN — eight reads and six writes — for the
		structured I-9 workflow: get_i9_settings, get_i9_form, list_i9_forms,
		list_pending_i9_verifications, get_i9_audit_log, list_i9_document_types,
		get_i9_retention_report and list_expiring_work_authorizations read;
		create_i9_form, submit_i9_section_1, submit_i9_section_2,
		update_i9_settings, flag_i9_reverification and destroy_i9 write.

		v0.28.0 ADDED TEN — seven reads and three writes — for the
		W-4 / Federal Withholding Engine: get_w4, list_w4_forms, get_fica_config,
		get_federal_tax_table, preview_federal_withholding,
		list_employees_missing_w4 and calculate_payroll_taxes read;
		submit_w4, update_fica_config and import_federal_tax_table write.

		v0.29.0 ADDED NINE — six reads and three writes — for the
		State Tax Engines (Oregon + Washington): get_state_tax_config,
		list_state_tax_configs, get_state_tax_table, preview_state_withholding,
		preview_total_payroll_taxes and list_employees_by_work_state read;
		create_state_tax_config, update_state_tax_config and
		import_state_tax_table write.

		v0.30.0 ADDED NINE — five reads and four writes — for salary structures
		and the payroll engine: get_salary_structure, list_salary_structures,
		preview_payroll, get_payroll_entry and list_payroll_entries read;
		create_salary_structure, deactivate_salary_structure, calculate_payroll
		and submit_payroll write.

		v0.31.0 ADDED FIVE — two reads and three writes — for expense receipt
		capture: list_expense_receipts and get_expense_receipt read;
		submit_expense_receipt, approve_expense_receipt and
		reject_expense_receipt write. Approval and rejection are two tools rather
		than one verdict argument because they are two switches an operator sets
		independently, and that is a difference a single tool cannot express.

		v0.32.0 ADDED THREE — one read and two writes — for geography and crew
		tracking: get_shift_track reads; log_shift_location and
		set_parcel_boundary write. The last of those closes a gap
		set_field_boundary had been apologising for since v0.12.0, in a warning
		on every single call: a parcel had no boundary, so nothing checked that
		the block sat inside its parcel.

		v0.34.0 ADDED FIVE — two reads and three writes — for the tax form
		generators: list_tax_forms and get_tax_form read; generate_tax_form,
		regenerate_tax_form and mark_tax_form_filed write. `regenerate` is a
		separate tool from `generate` rather than an argument on it because
		recomputing a form REPLACES what an employer told an agency, and a
		switch an operator can leave off is the only honest way to express
		that.

		v0.35.0 ADDED THREE — two reads and one write — for payroll off the shift
		register: get_employee_timesheet_summary and preview_payroll_for_period
		read; run_payroll_for_period writes. The timesheet summary is its own
		tool rather than a field on the preview because the hours are not the
		payroll: "why is my cheque this?" is answered by somebody's own spans and
		their own overtime, and answering it should not require the switches that
		let a caller see everybody's pay.

		v0.36.0 ADDED TWO, BOTH WRITES — render_tax_form_pdf and
		bulk_render_tax_form_pdfs — which draw a Tax Form's already-stored
		values on the face of the form and attach the PDF. They are writes
		because they attach a file, not because they compute anything: the page
		is a rendering of `form_data_json` and recomputes nothing, so it cannot
		disagree with the record it claims to render. Both go unavailable by
		name on a bench without reportlab, and the numbers stay readable through
		`get_tax_form` there — the arithmetic is the deliverable and the page is
		a convenience.

		v0.37.0 ADDED ONE, A WRITE — approve_inspection_template — and FILLED THE
		TWO SURFACES v0.21.0 AND v0.22.0 HAD DECLARED AND LEFT REFUSING.
		`propose_compliance_rule` and
		`propose_inspection_template_from_regulation` now write records instead of
		sentences, and the catalogue count did not move for either of them: they
		were always tools, because they always had a switch, a schema and an
		entry, and a surface an operator cannot see in the settings form is a
		surface nobody can refuse. That is the whole argument for counting a
		declared-and-refusing tool, tested by the fact that filling one changes no
		number here.

		Neither of them calls a model. THE PROPOSER IS THE CLIENT: an AI reads a
		regulation and hands over a drafted record, and the tool is the validator
		and the gate — it lands the draft DISABLED, stamps `AI-proposed` with the
		source, refuses to sign its own approval, and flags model-written code for
		an acknowledgement the approver has to make by name. The runtime is
		exactly as deterministic as it was.

		The one NEW tool is the counterpart the templates half was missing.
		`approve_compliance_rule` had existed since v0.22.0; there was no way to
		turn an inactive template on with somebody's name against it, which is
		fine while every template is typed by the person who wants it and is not
		fine the moment a model can draft one.

		v0.38.0 ADDED SEVEN — three reads and four writes — and they are the
		layer the two proposal tools were missing. A rule drafted from a
		regulation says which regulation it was drafted from; nothing before this
		release watched that regulation afterwards. A Regulation Feed is the
		pointer: a URL, a hash of its normalised text, and a daily sweep. THE
		FOUR WRITES DETECT AND DO NOT REMEDIATE — the two check tools fetch,
		compare and log, and no Compliance Rule is modified by any of them. That
		is the property that makes putting a change detector on a timer safe at
		all, and it is why the count moved by seven rather than by eight: there is
		no auto-update tool here and there is not going to be one.

		v0.39.0 ADDED SEVEN — four reads and three writes — and they are the
		Financial KPI Framework. v0.19.6 made the window standard generalize
		across three SHIPPED reports; this makes the KPI itself a record, so an
		operation can add the ratio its own lender asks about without a release,
		a deploy or an engineer.

		NONE OF THE SEVEN CAN RUN CODE, and that is the property worth counting.
		`create_financial_kpi_definition` and its update take a built-in
		computer's NAME or an ARITHMETIC EXPRESSION, and the expression is parsed
		to an AST and checked against an allowlist before it is stored — no
		imports, no attribute access, no subscripts, no comprehensions, no calls
		except min, max, abs and round. That is a deliberate difference from
		`create_compliance_rule`, which does have a `custom_python` field: a
		compliance rule can need to express a shape no set of fields captures,
		and a financial KPI is a number divided by another number.

		v0.40.0 ADDED FOUR — two reads and two writes — and they close the
		accounting loop. v0.30.0 computed payroll, v0.35.0 fed it the shift
		register's hours and v0.36.0 drew the tax forms, and a completed run
		still produced no Journal Entries: wages were the largest number on the
		income statement and the one number somebody keyed into the ledger by
		hand every fortnight.

		FOUR RATHER THAN THREE BECAUSE THE MAPPING IS A RECORD. No account name
		ships with this app — a default would be right on the chart of accounts
		it was written against and quietly wrong everywhere else — so
		`configure_payroll_accounts` and `get_payroll_account_mapping` exist
		beside the posting itself. And the posting produces DRAFTS: there is no
		`submit_payroll_journal_entries` here and there is not going to be one,
		for the same reason there is no `post_journal_entry`.

		v0.41.0 ADDED FIVE — one read and four writes — for Farm Task Templates:
		the shape of one recurring job, as a record, with a five-tool surface
		matching the thirteen-recipe `ALERT_TASK_MAP` it now sits beside rather
		than replaces.

		v0.42.0 ADDED SEVEN — three reads and four writes — for Budget +
		Variance Alerts. THE ARITHMETIC IS PURE, same discipline as `payroll_gl.py`:
		`budget_engine.py` reads no database, and `tools/budget.py` is the only
		place that reads GL Entry or the KPI cache. `refresh_budget` writes no
		Compliance Alert directly — it saves the computed fields, and the
		`budget_variance_breach` rule reads them the way `financial_kpi_threshold_breach`
		reads the KPI cache, so ONE alerting engine still decides what reaches the
		calendar.

		v0.43.0 ADDED SEVEN — three reads and four writes — for the ML Model
		Registry: which trained model Volume Vision produced is DEPLOYED for
		which company and which piecework activity, which is the one fact
		Volume Vision itself has no reason to hold. `model_registry.py` is pure,
		the same discipline as `budget_engine.py`; `tools/ml_model.py` is the
		only place that reads or writes an ML Model document. `get_active_model`
		is what an iOS app queries to find out what to pull, and
		`activate_model` is the only door that reaches Active — activating one
		model AUTO-DEPRECATES whichever other model held Active for the same
		(company, piecework_activity), enforced twice: reported by the tool via
		`check_model_conflicts`, and held true in the database regardless of
		which door a save came through, by the DocType controller.

		v0.44.0 ADDED EIGHT — five reads and three writes — for the BucketLog →
		ERPNext Piecework Bridge. `payroll_integration.py` has read a
		`bucket_logs` row off a shift since v0.35.0 and `tools/payroll.py` has
		speculatively queried a doctype called "Bucket Log Entry" since the same
		release; this is what finally creates it, as erpnext_mcp's OWN doctype
		rather than a hypothetical external app's — `compliance_fields.py`'s
		Target entry for it moves from `mode="extend"` to `mode="verify"`
		accordingly. `bucket_bridge.py` is pure, the same discipline as
		`model_registry.py`: `entries_to_payroll_shape` reshapes synced
		captures into exactly the row `_piece_units_for` already reads, and
		ONLY an Accepted verdict earns a unit. `sync_bucket_entries` dedupes a
		resynced batch by `entry_uuid` rather than failing it.

		THE THREE NUMBERS BELOW WERE RED FOR A RELEASE. v0.30.0 shipped its nine
		tools without touching them, so this test, `test_read_tools.py`'s copy of
		the read count and the three in `test_tool_catalog_count.py` all failed on
		main until v0.31.0. The counts are cheap to update and the failure is
		loud; what it cost was the signal — six red tests that everybody had
		learned to expect are six tests that cannot tell you about the seventh.

		v0.47.0 ADDED ONE — `reverify_i9`, Section 3 of Form I-9. It is a write
		and there is no matching read, because `get_i9_form` already returns the
		reverification history and a second read would be a second answer to the
		same question.

		v0.47.1 ADDED TWO, BOTH WRITES, AND THE READ COUNT IS UNCHANGED FOR THE
		SAME REASON. `render_i9_pdf` fills the USCIS form from a record and attaches
		the page; `attach_signed_i9` files the scan that comes back signed. Both
		write an Attach column and a File, so neither is a read however much
		"render" sounds like one — and the values on the page are already readable
		through `get_i9_form`, which is what makes a `preview_i9_pdf` unnecessary
		rather than missing.

		v0.48.0 ADDED FIVE — one read and four writes. The read is
		`list_authorized_signers`, and it has to be a read that anybody can call:
		an EMPTY roster means federal-form signing is unrestricted, and a caller
		with no way to ask which case a site is in would have to discover it by
		being refused mid-Section-2. The four writes are the roster's three
		maintenance calls — `add_authorized_signer`, `update_authorized_signer`,
		`remove_authorized_signer`, all of which change who may make a federal
		attestation — and `render_w4_pdf`, which is a write for exactly the
		reason `render_i9_pdf` is: it attaches a File and sets an Attach column.

		THERE IS NO `delete_authorized_signer` AND THERE WILL NOT BE. A form
		signed in a prior season was signed by whoever was authorised then;
		`remove_authorized_signer` clears a flag and keeps the row.

		v0.50.0 ADDED THREE — one read and two writes — and closed the gap that
		made a badge a piece of paper this app had never seen. The read is
		`resolve_badge`, the call between a scan and a name: `add_worker_to_shift`
		takes an Employee docname and a camera produces a badge string, so a crew
		clock could scan a whole crew and roster none of it. The two writes ISSUE
		a badge — `generate_employee_badge_qr` and `generate_employee_badge_sheet`
		— which nothing in this app did before: `link_badge_to_employee` recorded
		a string somebody else had printed.

		v0.52.0 ADDED TWO — one read, one write — so an ML Model record can own
		its binary and serve it from ERPNext instead of an iOS app reaching
		Volume Vision directly. `attach_model_file` is the write: an operator
		uploads the model once, straight or through the staged-chunk machinery
		for anything too large for one call. `get_model_file_chunk` is the read
		that serves it back, base64, in the same chunked shape `stage_file_chunk`
		already takes uploads in.

		v0.53.0 ADDED ONE WRITE — `generate_employee_badge_pass`, the badge in
		the wallet the worker already carries. It is a WRITE for two reasons that
		both matter: it issues a badge to somebody who has none, through
		`generate_employee_badge_qr`'s own minting path, and it attaches the
		`.pkpass` to the Employee as a File. There is no read counterpart and
		there should not be — a pass is a derived artefact rebuilt
		byte-identically from the register on demand, so "read the pass" and
		"build the pass" are the same call.

		v0.57.0 ADDED ONE WRITE — `dismiss_compliance_alert`, which is
		`dismiss_alert`'s verb with a gate in front of it. It reads the alert's
		own `can_dismiss` and refuses everything else, which is what makes the
		verb safe to publish to a handset and to a model: whether an obligation
		may be closed without being met is a judgement somebody records in
		advance, per alert, rather than one the caller makes on the spot.

		v0.59.0 ADDED ONE WRITE — `pull_model_from_vv`, and it is the only tool
		in this app that fetches a file from another server. It asks Volume
		Vision's NEW `/training/models/<uuid>/bundle` for the zip that carries
		`manifest.json` beside the weights, falls back to the original
		`/download` when that endpoint is not deployed yet and says so, and
		attaches what it got. There is no read counterpart: `get_model` already
		returns everything the pull wrote, and `get_model_file_chunk` already
		serves the bytes.

		v0.60.0 ADDED TWO READS AND NO WRITE — `list_signing_evidence` and
		`get_signing_evidence`, over the register of who signed what and how
		anybody knows. THE ABSENCE OF A WRITE IS THE POINT rather than an
		omission: a Signing Evidence row is created by the signature path and by
		nothing else, the doctype is append-only, and a tool that could add one
		would be a tool that could manufacture an identity check that never
		happened. `collect_form_signature` grew the arguments instead.

		v0.61.0 ADDED EIGHT — four reads and four writes — over the two
		company-wide wage tables: what the OPERATION pays for a bucket and for an
		hour of a job title, as opposed to what one named person earns. Four each
		and NO DELETE on either: a rate that paid a period is the record of what
		that period paid, so `update_*` can clear `is_active` and nothing can
		remove the row — the posture `remove_authorized_signer` takes for the same
		reason.

		THE TWO TABLES ARE READ AT OPPOSITE MOMENTS and that asymmetry is the
		design rather than an inconsistency. A Piecework Rate is read on EVERY
		payroll run, for every worker whose structure names no rate, which is what
		makes a mid-season raise one row instead of a hundred edits. A Position
		Wage Default is read ONCE, when a salary structure is created, and never
		reaches back through it: a piece rate is a property of the work, and an
		hourly wage is what a person was hired at.

		`wage_defaults.py` is pure — the lookup order takes rows and returns
		answers, so it is testable without a bench — and `tools/wagedefaults.py`
		is the only place that reads or writes either doctype.

		v0.63.0 adds the two ends of the signing flow: `get_document_preview`,
		which hands the page a signer has to be SHOWN back as bytes because the
		handset authenticates to the sidecar and cannot follow a private
		`file_url`, and `seal_signed_document`, which appends the verification
		page and hashes the finished file. One read, one write.

		v0.65.0 adds one write, `universal_scan`, and it is the only tool here
		that does not know what it is about until it has read the string it was
		given: it resolves a scanned tag against the badge register, the Asset
		Register, Housing Unit and Field in that order and answers for whichever
		holds it. It counts as MUTATING because one of those four branches is
		`scan_asset`, which stamps `last_scan_at` — the other three are reads,
		and `scan_recorded` in the answer says which happened.

		v0.66.0 adds NINETEEN — ten reads and nine writes — over the master data
		every other document points at: Item, Item Group, Supplier, Customer,
		Warehouse and Item Price. The largest single jump in the catalogue, and
		the least novel: they wrap stock ERPNext doctypes, and the work is in the
		refusals rather than in the reads. The one thing to know before adding to
		them is that `company` means a DIFFERENT THING on each of the three party
		shapes — a Warehouse is company-scoped, an Item only through its default
		row, a Supplier and a Customer not at all — and each tool reports which of
		the three it applied rather than letting a shorter list speak for itself.

		v0.67.0 adds NINE — five reads and four writes — and they are the first
		tools here over registers this app invented rather than wrapped: Scale
		Ticket and Settlement Statement. The pair is one idea. A scale ticket is
		what the GROWER's copy says was delivered; a settlement is what the PACKER
		says was delivered, packed and paid for. Nothing in the nine reconciles
		them — `get_settlement_statement` reports both figures and names the
		variance, because the variance is the answer and a tool that agreed them
		would delete the only audit either document has.

		`classify_receipt` is the odd one and the one to read the source of: it
		touches NO doctype, holds no state, and decides from a keyword table which
		of the four registers a photograph belongs in. It is the branch in "the
		receipt is the financial atom" — one capture flow, four destinations — and
		it returns the keywords that produced its answer, because a classifier
		nobody can argue with is a classifier nobody will correct.

		v0.67.1 adds ONE write, `patch_i9_section_1` — the only I-9 tool that
		moves a form sideways instead of forward. Every other one advances a
		status, so a Section 1 that was filed with a blank date of birth could
		not be given one afterwards on any status, and the form still read
		Complete.

		v0.68.0 adds SIX — four reads and two writes — the Container-Agnostic
		Fill Pipeline: `get_fill_determination` explains one capture's fill
		percentage against its threshold, `get_fill_thresholds` and
		`update_fill_threshold` are the band a foreman controls per container
		type, and `list_fill_threshold_changes` /
		`acknowledge_threshold_update` / `list_pending_threshold_acknowledgments`
		are the loop that makes a threshold change something a checker in the
		field is known to have seen.

		v0.68.0 also adds SEVEN over expense-receipt capture — three writes and
		four reads: `create_owner_draw` records a distribution as a draft
		Journal Entry rather than an expense, because equity leaving the company
		is not a bill; `update_expense_receipt` corrects cost_center, supplier,
		category or notes at a desk after the phone that captured the receipt
		has moved on; `create_purchase_invoice_from_receipt` turns one APPROVED
		receipt into a draft Purchase Invoice by calling
		`purchasing.create_purchase_invoice` rather than writing the document
		itself; `normalize_merchant` and `list_merchant_aliases` are the fuzzy
		match and the register behind that tool's automatic Supplier
		resolution; `get_expense_summary` and `get_expense_report` are the
		bookkeeper's dashboard and export over the same receipts.

		v0.68.0 also adds SIXTEEN over the rest of the purchasing pipeline —
		eight reads and eight writes — Sprint 3 of the Gap Closure Plan:
		`create_purchase_order` / `get_purchase_order` / `submit_purchase_order`
		close the loop `list_purchase_orders` (v0.66.0) started;
		`create_purchase_receipt` / `get_purchase_receipt` /
		`list_purchase_receipts` / `submit_purchase_receipt` are goods received
		against a supplier;
		`create_purchase_invoice` / `get_purchase_invoice` /
		`list_purchase_invoices` / `submit_purchase_invoice` are the bill, and
		it is `create_purchase_invoice` the receipt-capture tool above builds
		on; `create_payment_entry` / `get_payment_entry` / `list_payment_entries`
		/ `submit_payment_entry` pay it, partial amounts allowed; and
		`get_ap_aging` closes the sprint — a supplier's true balance from GL
		Entry against every account typed Payable, aged per open invoice from
		Purchase Invoice's own outstanding_amount and due_date, with a `drift`
		field where the two disagree.

		v0.68.0 also adds THREE over the ML model registry — two reads and one
		write — for the records three earlier releases each left in a different
		shape: `list_models_needing_migration` is the register of records not in
		the current manifest schema, `validate_model_bundle` is the deep
		single-record check that opens the attached bytes, and
		`migrate_model_format` is the only one that writes. It moves METADATA
		ONLY — no upload, no download, no re-attach — and a record that never
		had a bundle gets a manifest built from its own fields that SAYS so,
		rather than one claiming labels somebody typed came out of a training
		run.

		v0.69.0 adds NINE over stock and inventory — six reads and three writes
		— Sprint 4 of the Gap Closure Plan, and the answer to a question
		v0.66.0's masters could only pose: `list_warehouses` and `get_item` can
		name a shed and a chemical, and nothing until now could say how much of
		the chemical is in the shed. `create_stock_entry` /
		`submit_stock_entry` / `get_stock_entry` / `list_stock_entries` are the
		movement, split draft-from-post the same way purchasing is;
		`get_stock_balance` and `get_warehouse_summary` read Bin, the balance
		ERPNext maintains; `get_stock_ledger` reads Stock Ledger Entry, the
		history that produced it; and `set_reorder_level` /
		`list_reorder_alerts` are the rule that turns a balance into a
		purchase — with an item that has a rule and no Bin row at all reported
		at zero rather than skipped, because never having arrived is the
		strongest possible reason to buy.

		v0.69.0 (Sprint 4) adds FIVE for Document Intelligence — three reads and
		two writes. `validate_document_extraction` is the one that matters: it
		takes what on-device OCR extraction read off a photographed document and
		runs the rules a model is bad at (an EPA registration number's shape, a
		restricted-entry interval against the active ingredient it names, a
		pre-harvest interval that cannot be true beside its own REI, a licence
		expiry, a holder's name against the record it is filed against), then
		merges the CALLER's own assessment on top — this app still makes no
		model call. `revalidate_document` re-runs those checks against the
		stored extraction rather than against a fresh photograph, which is why
		the record keeps the OCR text at all; `get_document_validation`,
		`list_document_validations` and `list_revalidation_due` read the
		register.

		v0.70.0 adds TWELVE over sales and settlements — six reads and six
		writes — Sprint 5 of the Gap Closure Plan, and the far end of the
		pipeline Sprints 2 and 3 opened. `create_sales_invoice` and
		`create_sales_invoice_from_settlement` turn a submitted packer
		settlement into a DRAFT invoice: each priced line becomes a line against
		a shared non-stock Item per variety and grade, each deduction a negative
		Actual charge row, so revenue is recognised gross and the receivable is
		the net. `submit_sales_invoice` recognises it and reads the GL rows back
		rather than computing them; `receive_payment` collects, allocating
		oldest-first when no advice came with the cheque; `post_settlement_to_gl`
		is the journal-entry ALTERNATIVE, refused on a settlement already
		invoiced and refusing one already posted, because two revenue postings
		for one statement is a double count nobody finds until the year end; and
		`reconcile_settlement_to_tickets` attaches a stub that turned up after
		the settlement was filed, reporting how far the variance with the packer
		moved. `get_settlement_shrink`, `get_packout_summary`, `get_ar_aging`
		and `get_season_summary` are the reads — and each of them says where its
		numbers came from and returns null rather than allocating one that
		cannot be attributed.

		There is deliberately NO Delivery Note tool. The packer owns the scale,
		so the Scale Ticket is the delivery evidence, and a second record of one
		delivery would disagree with the first with nothing to say which is
		right.

		v0.71.0 adds TEN over CFL Banking — six reads and four writes — Sprint 6
		and the capstone of the Gap Closure Plan: the bridge from the paper this
		app has been collecting since Sprint 2 to the bank's own record of the
		same money. `match_receipt_to_bank_transaction` links one slip to the
		withdrawal it explains, or ranks the candidates and writes nothing;
		`auto_match_receipts` is the batch half and is a READ tool on purpose,
		because a wrong link between a slip and a withdrawal is invisible
		afterwards — both documents exist and both amounts are right — so a
		person accepts each one and the record says a machine proposed it.
		`create_bank_categorization_rule`, `list_bank_categorization_rules`,
		`apply_categorization_rules` and `seed_farm_categorization_rules` make
		the dictionary a farm reads its own statement with a RECORD rather than
		code, and the categorisation is allowed to write in bulk where the
		matching is not, because a rule is deterministic and names itself in its
		own output. `get_bank_reconciliation_status` answers the three
		reconciliation questions — ledger allocation, receipt evidence,
		categorisation — SEPARATELY and never adds them together;
		`list_unmatched_receipts` and `list_unmatched_bank_transactions` are the
		two worklists; `get_cash_flow_summary` reports the bank statement apart
		from the documents and deduplicates a receipt against the withdrawal it
		is matched to.

		NOTHING in Sprint 6 posts to the ledger. Not one of the ten writes a GL
		Entry, a Journal Entry or an allocation.

		v0.73.0 adds FOURTEEN over the Bank Bridge consolidation — eight reads
		and six writes — and what they consolidate is AUTHORITY. A sidecar Flask
		app held the statement anchor chain in its own database, which meant two
		systems held reconciliation truth and nothing said which was right when
		they disagreed. `get_statement_anchor_chain`, `list_unreconciled_anchors`
		and `get_anchor_variance_breakdown` answer the question a transaction
		list cannot — whether a year of bank data is COMPLETE — because a
		movement the feed never delivered leaves no row to inspect and shows up
		only as opening plus everything on file not equalling closing.
		`list_unmatched_statement_lines` is the one list that NAMES a missing
		movement rather than its size, and it says so plainly when no statement
		lines are on file, because "nothing is missing" and "nothing to check
		against" are opposite answers. `get_statement_recon_report` puts the
		statement, the feed and the ledger side by side and never sums them.
		`set_anchor_variance_reason` records why a period does not tie out and
		does NOT mark it reconciled; `rebuild_anchor_chain` recomputes only what
		is derived, because rewriting the anchored numbers from the transaction
		feed would make every period tie out perfectly and prove nothing.
		`get_account_pairing` and `pair_bank_accounts` make a brokerage and its
		cash-services companion one relationship stored on both sides.
		`create_advisory_agreement`, `update_advisory_agreement`,
		`get_advisory_agreement_summary` and `list_advisory_agreements` make an
		advisory fee — the one recurring cost that arrives already deducted —
		checkable against the terms it was charged under, with amendment as
		versioning rather than editing. `create_bank_categorization_rules` is
		the fourteenth: a whole book of rules vetted as a set, because a
		single-rule call can only see the rules that already exist.

		NOTHING in the consolidation posts to the ledger either.

		v0.78.0 adds ten, six read and four write, and they are the asset
		register finally answering what its own state log has been accumulating
		since v0.25.0. `get_asset_status_report` is the whole picture for one
		machine on one call — the seven round trips a scan used to cost.
		`get_engine_hours_summary` and `record_service` are the two ends of a
		meter reading; `check_maintenance_due` and `trigger_maintenance_tasks`
		are the schedule and the work it raises, and the second defaults to a
		dry run because it makes work for other people.
		`get_water_usage_report` rolls the valve log up by zone, block, week or
		month and prices it per valve at that valve's own zone rate — never
		guessed, and unpriced valves are NAMED rather than dropped from a figure
		somebody files with a district.

		THE FOUR REI TOOLS ARE THE COMPLIANCE-CRITICAL ONES.
		`record_spray_application` opens one restricted-entry window PER BLOCK
		rather than one per task, which is what makes `get_active_rei` a single
		indexed query at a gate; `list_active_reis` is the board a foreman reads
		before sending anybody anywhere; `cancel_spray_rei` withdraws one with a
		required reason and never deletes it. The two reads default ON — a
		restricted-entry answer an operator has to switch on is one a worker
		does not get.

		v0.84.0 IS THE ACTIVITY-BASED COSTING ENGINE, ten tools over six new
		doctypes. `create_cost_activity` and `create_activity_cost_pool` build
		the register and gather each activity's money for a year;
		`compute_abc_allocation` pushes every Ready pool out to the blocks that
		consumed it and stores the whole run, intermediates included, because a
		per-acre cost is a quotient of two numbers that both moved during the
		year. `get_abc_report` and `get_phase_waterfall` are the two management
		reads. THE ENGINE NEVER ESTIMATES A DRIVER: an activity whose driver
		quantities nobody supplied is reported unallocated with its full amount
		rather than spread evenly across blocks, because an even spread is
		indistinguishable in the output from a measured one.

		v0.68.1 IS THE ORG STRUCTURE, fifteen tools over five masters that were
		already Link targets on Employee and had no CRUD anywhere: Designation,
		Department, Branch, Employment Type and Employee Grade. `create_employee`
		has always refused a value naming no record and listed the site's own
		choices; until now that refusal named an answer nobody could act on. The
		five updates all carry `new_name`, because three of the five masters hold
		nothing but their name and a correction is the only edit there is — and a
		rename repoints every Employee already on it.

		v0.98.0 IS BIN SEALING, four tools over one new register: `seal_bin` writes
		what a checker's phone makes when it closes a bin, `get_bin_seal` and
		`list_bin_seals` read it back, and `trace_bin` answers the only question a
		packing house ever asks — given this tag, whose buckets are in this bin.
		Three reads and one write, which is why the write count moves by one and
		the read count by three.

		v0.101.0 IS BREAK POLICY MANAGEMENT, two mutating tools:
		`create_break_policy` writes a new Labor Break Policy and
		`update_break_policy` amends an existing one. Both are writes, so the
		mutating count moves by two and the read count stays.

		v0.101.0 ALSO ADDS GARNISHMENT COMPLIANCE, five tools over the new Farm
		Garnishment doctype: `list_garnishments` and `get_garnishment` read the
		file of court orders, `create_garnishment` files one AND creates the
		payroll deduction that honours it, `update_garnishment` posts what was
		withheld against the balance, and `render_garnishment_response` draws the
		employer's answer back to the issuing court. Two reads and three writes.

		v0.114.0 ADDS ONE READ, `get_variety_care_recipe`: one variety's water
		schedule resolved against its crop's per field and labelled with each
		number's source, plus its cultural practice protocol — GA timings, PGR
		program, thinning, pruning — grouped by practice. The two child tables
		behind it carry no tools of their own.

		v0.116.0 ADDS FIVE — Cycle 5, the operational map overlays. Two reads:
		`get_map_overlays` returns what is true of every block and irrigation
		zone right now across five layers, role-filtered, and
		`list_soil_compaction_profiles` returns the hour figures behind the
		compaction colours with the count of blocks still on the shipped default.
		Three writes over the new Soil Compaction Profile register:
		`create_soil_compaction_profile`, `update_soil_compaction_profile` and
		`assign_soil_profile`, which is its own tool for the reason
		`link_field_to_cost_center` is rather than a new argument on
		`update_field`.
		"""
		self.assertEqual(len(registry.TOOLS), 775)
		self.assertEqual(len(registry.READ_TOOLS), 386)
		self.assertEqual(len(registry.MUTATING_TOOLS), 389)

	def test_every_tool_declares_why_it_might_be_unavailable(self):
		"""A predicate with no `requires` sentence produces a refusal that says
		nothing a caller can act on."""
		for name, spec in registry.TOOLS.items():
			with self.subTest(tool=name):
				if spec["available"] is not registry._always:
					self.assertTrue(spec["requires"], f"{name} has a predicate but no reason")
