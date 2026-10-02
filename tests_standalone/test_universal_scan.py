# SPDX-License-Identifier: MIT
"""One scan, whatever was on the tag. v0.65.0.

WHAT THESE TESTS ARE REALLY ABOUT. `universal_scan` is the only tool in the
catalogue that does not know what it is answering about until it has read its
argument, so almost every claim worth making is about the CASCADE rather than
about any one branch — the branches themselves are `resolve_badge`, `scan_asset`,
`get_housing_unit` and `get_field`, each of which already has its own file.

Six claims.

1. `TheCascadeResolves` — each of the four registers is reached, in order, on the
   exact docname, and the branch that wins is the branch whose register holds the
   string.

2. `TheOrderIsLoadBearing` — a string that is in two registers resolves to the
   earlier one. The badge/asset collision is the one with a payroll consequence
   and it has a test of its own.

3. `OnlyTheAssetBranchWrites` — `scan_recorded` is true exactly when
   `last_scan_at` moved, and the other three branches leave the record alone.

4. `TagURLsAreUnwrapped` — a printed asset QR encodes `<site>/scan/<name>`, which
   is what a camera actually produces, and the register holds the bare docname.

5. `UnknownIsAnAnswer` — a supplier's barcode comes back as a shape a client can
   render, with `create_task` still offered, rather than as a refusal. A
   credential payload is the ONE scan that is refused instead, and is not quoted
   back.

6. `WhatIsOutstanding` — the tasks, the alerts, and the rule that `overdue` means
   "the compliance alert this task answers was due before today", because a Farm
   Task has no due date of its own.
"""

import frappe

from erpnext_mcp import bucket_bridge
from erpnext_mcp.tools import universal_scan

from .fixtures import MAIN, OTHER, V12TestCase, install_hrms
from .harness import STORE
from .test_dispatch import WALK

ASSET_REGISTER = "Asset Register"
BADGE_DOCTYPE = "Bucket Log Badge Map"
ALERT = "Compliance Alert"

EMP = "HR-EMP-00001"
EMP_NAME = "Ana Reyes"

VALVE = "MC-Valve-05"
CABIN = "MC-Cabin-01"
BLOCK = "Yellow Camp Block 3"

ON = {
	f"allow_{name}": 1
	for name in (
		"universal_scan",
		"register_asset",
		"scan_asset",
		"generate_employee_badge_qr",
		"resolve_badge",
		"create_parcel",
		"create_housing_unit",
		"create_field",
		"create_farm_task",
		"assign_farm_task",
		"get_housing_unit",
		"get_field",
		"get_asset_detail",
	)
}


class ScanTestCase(V12TestCase):
	"""A farm with one of each thing a camera can be pointed at."""

	def setUp(self):
		super().setUp()
		install_hrms()
		self.configure(enabled=1, **ON)
		STORE.seed(
			"Employee",
			[
				{
					"name": EMP,
					"employee_name": EMP_NAME,
					"first_name": "Ana",
					"last_name": "Reyes",
					"company": MAIN,
					"status": "Active",
					"designation": "Picker",
					"date_of_joining": "2025-01-15",
				}
			],
		)

	# ── the furniture ───────────────────────────────────────────────────────
	def a_badge(self, employee=EMP, **overrides):
		payload = {"employee": employee, "company": MAIN}
		payload.update(overrides)
		return self.tool_data("generate_employee_badge_qr", payload)["badge_id"]

	def an_asset(self, name=VALVE, asset_type="Irrigation Valve", company=MAIN, **overrides):
		payload = {"name": name, "asset_type": asset_type, "company": company}
		payload.update(overrides)
		return self.tool_data("register_asset", payload)["name"]

	def a_parcel(self, parcel_name="Mill Creek", company=MAIN):
		if not STORE.rows("Parcel"):
			self.tool_data(
				"create_parcel",
				{"owning_entity": company, "parcel_name": parcel_name, "acreage": 131.43},
			)
		return parcel_name

	def a_cabin(self, unit_name=CABIN, **overrides):
		self.a_parcel()
		payload = {
			"parcel": "Mill Creek",
			"unit_name": unit_name,
			"unit_type": "Cabin",
			"square_footage": 384,
			"capacity": 4,
		}
		payload.update(overrides)
		return self.tool_data("create_housing_unit", payload)["name"]

	def a_block(self, field_name=BLOCK, **overrides):
		self.a_parcel()
		payload = {
			"parcel": "Mill Creek",
			"field_name": field_name,
			"acreage": 12.5,
			"variety": "Bing",
			"planting_year": 1998,
		}
		payload.update(overrides)
		return self.tool_data("create_field", payload)["name"]

	def a_task(self, **overrides):
		payload = {
			"task_name": "Walk it",
			"task_type": "Inspection",
			"company": MAIN,
			"skill_required": "camp_maintenance",
			"evidence_required": dict(WALK),
		}
		payload.update(overrides)
		return self.tool_data("create_farm_task", payload)["name"]

	def an_asset_task(self, asset=VALVE, **overrides):
		"""A task pointing at an asset through `Farm Task.asset`.

		Set directly rather than through `create_farm_task`, which does not take
		the column: the link is written by `report_asset_issue` and by the alert
		generator, and what is under test here is that a scan READS both routes to
		an asset — the dedicated column and the dynamic location — rather than
		which tool writes them.
		"""
		task = self.a_task(**overrides)
		frappe.db.set_value("Farm Task", task, "asset", asset)
		return task

	def an_alert(self, source_doctype, source_docname, due_date, **overrides):
		"""One Compliance Alert, seeded directly.

		Seeded rather than raised through `refresh_compliance_alerts` because
		what is being tested is what a scan DOES with a due date, not whether a
		rule fires — and a rule that fires is a moving target with a calendar in
		it.
		"""
		row = {
			"name": overrides.pop("name", f"CA-{source_docname}-{due_date}"),
			"alert_key": f"{source_doctype}:{source_docname}",
			"alert_type": "housing_inspection_overdue",
			"severity": "Warning",
			"category": "Housing",
			"company": MAIN,
			"source_doctype": source_doctype,
			"source_docname": source_docname,
			"alert_message": "It has been a year.",
			"due_date": due_date,
			"first_seen": "2026-01-01",
			"dismissed": 0,
		}
		row.update(overrides)
		STORE.seed(ALERT, [row])
		return row["name"]

	def scan(self, content, **overrides):
		payload = {"content": content}
		payload.update(overrides)
		return self.tool_data("universal_scan", payload)


# ── 1. the cascade resolves ─────────────────────────────────────────────────
class TheCascadeResolves(ScanTestCase):
	def test_a_badge_resolves_to_the_person_holding_it(self):
		badge = self.a_badge()
		data = self.scan(badge)
		self.assertEqual(data["entity_type"], "employee")
		self.assertEqual(data["entity_name"], EMP)
		self.assertEqual(data["entity"]["employee_name"], EMP_NAME)
		self.assertEqual(data["available_actions"], ["create_task", "view_compliance", "view_i9"])

	def test_an_asset_tag_resolves_to_the_asset(self):
		self.an_asset()
		data = self.scan(VALVE)
		self.assertEqual(data["entity_type"], "asset")
		self.assertEqual(data["entity_name"], VALVE)
		self.assertEqual(data["entity"]["asset_type"], "Irrigation Valve")
		self.assertEqual(data["available_actions"], ["create_task", "log_state_change", "report_issue"])

	def test_a_housing_unit_resolves_to_the_cabin_with_its_occupancy(self):
		unit = self.a_cabin()
		data = self.scan(unit)
		self.assertEqual(data["entity_type"], "housing_unit")
		self.assertEqual(data["entity_name"], unit)
		self.assertEqual(data["entity"]["capacity"], 4)
		self.assertEqual(data["entity"]["open_beds"], 4)
		self.assertEqual(data["available_actions"], ["create_task", "start_inspection", "log_state_change"])

	def test_a_block_resolves_to_the_field_with_its_irrigation(self):
		block = self.a_block()
		data = self.scan(block)
		self.assertEqual(data["entity_type"], "field")
		self.assertEqual(data["entity_name"], block)
		self.assertEqual(data["entity"]["variety"], "Bing")
		self.assertIn("zones", data["entity"])
		self.assertEqual(data["available_actions"], ["create_task", "view_irrigation"])

	def test_it_matches_the_exact_docname_and_not_a_fragment_of_one(self):
		"""`asset_row` falls back to a LIKE search, which is right for an operator
		typing half a name and wrong for a cascade: a partial match at step two
		would resolve a cabin's sticker to whichever valve shared its prefix."""
		self.an_asset()
		self.assertEqual(self.scan("MC-Valve")["entity_type"], "unknown")

	def test_a_refusal_from_a_branch_is_not_demoted_to_unknown(self):
		"""A retired badge IS a badge. Telling the person holding it that their
		card means nothing is the wrong sentence — `resolve_badge` has a right
		one and this passes it through."""
		first = self.a_badge()
		self.a_badge(regenerate=True)
		error = self.tool_error("universal_scan", {"content": first})
		self.assertIn("retired", error)
		self.assertIn(EMP, error)


# ── 2. the order is load-bearing ────────────────────────────────────────────
class TheOrderIsLoadBearing(ScanTestCase):
	def test_a_badge_wins_over_an_asset_with_the_same_name(self):
		"""THE ONE COLLISION WITH A PAYROLL CONSEQUENCE. A string in both
		registers is a person before it is a sprayer, because attributing
		somebody's piece work to a machine is the failure that cannot be
		unpicked later."""
		badge = self.a_badge()
		self.an_asset(name=badge, asset_type="Sprayer")
		data = self.scan(badge)
		self.assertEqual(data["entity_type"], "employee")
		self.assertFalse(data["scan_recorded"])

	def test_an_asset_wins_over_a_housing_unit_with_the_same_name(self):
		"""A cabin with a tag on it is scanned as the tag it carries — which is
		the branch that records the scan, and the one an asset history reads."""
		self.a_cabin()
		self.an_asset(name=CABIN, asset_type="Housing Unit")
		data = self.scan(CABIN)
		self.assertEqual(data["entity_type"], "asset")
		self.assertTrue(data["scan_recorded"])

	def test_the_registers_are_searched_in_the_documented_order(self):
		self.assertEqual(
			[probe.__name__ for probe in universal_scan._CASCADE],
			["_employee_branch", "_asset_branch", "_housing_branch", "_field_branch"],
		)


# ── 3. only the asset branch writes ─────────────────────────────────────────
class OnlyTheAssetBranchWrites(ScanTestCase):
	def test_an_asset_scan_stamps_last_scan_at_and_says_so(self):
		self.an_asset()
		self.assertFalse(frappe.db.get_value(ASSET_REGISTER, VALVE, "last_scan_at"))
		data = self.scan(VALVE, scanned_by="Administrator")
		self.assertTrue(data["scan_recorded"])
		self.assertTrue(frappe.db.get_value(ASSET_REGISTER, VALVE, "last_scan_at"))
		self.assertEqual(frappe.db.get_value(ASSET_REGISTER, VALVE, "last_scan_by"), "Administrator")

	def test_a_gps_fix_is_where_the_phone_was_and_a_valve_stays_put(self):
		"""v0.214.0: a fixed asset is never moved by a scan (`test_asset_moves`)."""
		self.an_asset()
		self.scan(VALVE, gps_lat=45.6, gps_lon=-121.2)
		self.assertFalse(float(frappe.db.get_value(ASSET_REGISTER, VALVE, "gps_latitude") or 0))
		self.assertAlmostEqual(
			float(frappe.db.get_value(ASSET_REGISTER, VALVE, "last_scan_latitude")), 45.6, places=4
		)

	def test_a_badge_a_cabin_and_a_block_record_nothing(self):
		badge = self.a_badge()
		unit = self.a_cabin()
		block = self.a_block()
		for content in (badge, unit, block):
			with self.subTest(content=content):
				self.assertFalse(self.scan(content)["scan_recorded"])

	def test_the_write_is_declared_in_the_catalogue(self):
		"""A tool that writes and advertises `readOnlyHint` is a tool a client is
		entitled to call speculatively."""
		from erpnext_mcp import registry

		spec = registry.TOOLS["universal_scan"]
		self.assertTrue(spec["mutating"])
		self.assertFalse(spec["annotations"]["readOnlyHint"])


# ── 4. a printed tag is a URL ───────────────────────────────────────────────
class TagURLsAreUnwrapped(ScanTestCase):
	def test_a_scan_url_resolves_to_the_docname_it_carries(self):
		self.an_asset()
		data = self.scan(f"https://erp.example.com/scan/{VALVE}")
		self.assertEqual(data["entity_type"], "asset")
		self.assertEqual(data["resolved_from"], VALVE)

	def test_a_percent_encoded_docname_is_decoded(self):
		self.an_asset(name="MC Valve 06")
		self.assertEqual(
			self.scan("https://erp.example.com/scan/MC%20Valve%2006")["entity_name"], "MC Valve 06"
		)

	def test_a_query_string_the_scanner_appended_is_dropped(self):
		self.an_asset()
		self.assertEqual(self.scan(f"https://erp.example.com/scan/{VALVE}?src=qr")["entity_name"], VALVE)

	def test_a_bare_docname_is_left_exactly_as_it_arrived(self):
		self.assertEqual(universal_scan.scan_target("MC-Valve-05"), "MC-Valve-05")
		self.assertEqual(universal_scan.scan_target("  ETC-0001  "), "ETC-0001")


# ── 5. unknown is an answer; a credential is not ────────────────────────────
class UnknownIsAnAnswer(ScanTestCase):
	def test_a_stranger_comes_back_as_a_shape_rather_than_a_refusal(self):
		data = self.scan("0123456789012")
		self.assertEqual(data["entity_type"], "unknown")
		self.assertIsNone(data["entity"])
		self.assertEqual(data["content"], "0123456789012")
		self.assertEqual(data["available_actions"], ["create_task"])
		self.assertFalse(data["scan_recorded"])

	def test_every_key_a_resolved_scan_carries_is_present_and_empty(self):
		"""One client struct, five entity types. A screen that changes shape
		between scans is a screen that crashes on the fifth one."""
		self.an_asset()
		resolved = set(self.scan(VALVE))
		unresolved = set(self.scan("nothing-here-at-all"))
		self.assertEqual(resolved - unresolved, set())

	def test_it_names_the_registers_it_actually_searched(self):
		data = self.scan("nothing-here-at-all")
		self.assertEqual(data["searched"], ["employee", "asset", "housing_unit", "field"])

	def test_a_login_payload_is_refused_and_is_not_quoted_back(self):
		"""The one scan that is refused. What a camera reads by accident at a
		scan step is whichever QR is nearest, and this app mints one that carries
		a live credential."""
		payload = (
			'{"url":"https://erp.example.com","api_key":"abc123def456",'
			'"api_secret":"s3cr3t-value-nobody-should-see"}'
		)
		error = self.tool_error("universal_scan", {"content": payload})
		self.assertIn("credential document", error)
		self.assertNotIn("s3cr3t-value-nobody-should-see", error)
		self.assertNotIn("abc123def456", error)

	def test_an_ordinary_tag_with_a_brace_in_it_is_not_mistaken_for_one(self):
		"""Narrow on purpose: a scanner that refused anything unusual would be a
		scanner nobody could use."""
		self.assertEqual(self.scan("{not-json-at-all}")["entity_type"], "unknown")

	def test_the_note_quotes_a_long_scan_only_as_far_as_the_badge_step_does(self):
		long_tag = "X" * 80
		note = self.scan(long_tag)["note"]
		self.assertIn(bucket_bridge._display(long_tag), note)
		self.assertNotIn(long_tag, note)
		# ...and `content` still carries it whole, because the only useful thing
		# to do with an unresolved tag is raise a task naming it.
		self.assertEqual(self.scan(long_tag)["content"], long_tag)


# ── 6. what is outstanding on it ────────────────────────────────────────────
class WhatIsOutstanding(ScanTestCase):
	def test_open_tasks_on_the_asset_come_back_with_it(self):
		self.an_asset()
		task = self.an_asset_task(task_name="Valve leaks")
		data = self.scan(VALVE)
		self.assertEqual(data["pending_task_count"], 1)
		self.assertEqual(data["pending_tasks"][0]["name"], task)
		self.assertEqual(data["pending_tasks"][0]["task_name"], "Valve leaks")

	def test_a_task_reaching_the_asset_through_location_is_found_too(self):
		"""One entity, two columns. A scan that read `asset` and not `location`
		would say 'nothing outstanding' in front of outstanding work."""
		self.an_asset()
		self.a_task(location_doctype=ASSET_REGISTER, location=VALVE, task_name="From a rule")
		self.assertEqual(self.scan(VALVE)["pending_task_count"], 1)

	def test_a_task_matching_twice_is_reported_once(self):
		self.an_asset()
		self.an_asset_task(location_doctype=ASSET_REGISTER, location=VALVE)
		self.assertEqual(self.scan(VALVE)["pending_task_count"], 1)

	def test_a_finished_task_is_not_outstanding(self):
		self.an_asset()
		task = self.an_asset_task()
		frappe.db.set_value("Farm Task", task, "state", "Completed")
		self.assertEqual(self.scan(VALVE)["pending_task_count"], 0)

	def test_a_cabins_tasks_are_found_through_its_location_link(self):
		unit = self.a_cabin()
		self.a_task(location_doctype="Housing Unit", location=unit)
		self.assertEqual(self.scan(unit)["pending_task_count"], 1)

	def test_a_workers_own_tasks_come_back_with_their_badge(self):
		badge = self.a_badge()
		self.a_task(task_name="Ana's job", assigned_to=EMP, assigned_to_name=EMP_NAME)
		data = self.scan(badge)
		self.assertEqual(data["pending_task_count"], 1)
		self.assertEqual(data["pending_tasks"][0]["assigned_to"], EMP)

	def test_overdue_means_the_alert_the_task_answers_was_due_before_today(self):
		"""A Farm Task has no due date of its own, so `overdue` is derived from
		the obligation it answers rather than invented here."""
		unit = self.a_cabin()
		alert = self.an_alert("Housing Unit", unit, "2026-01-01")
		self.a_task(location_doctype="Housing Unit", location=unit, source_alert=alert)
		data = self.scan(unit)
		self.assertEqual(data["overdue_task_count"], 1)
		self.assertEqual(data["overdue_tasks"][0]["due_date"], "2026-01-01")
		self.assertTrue(data["pending_tasks"][0]["overdue"])

	def test_overdue_tasks_are_a_subset_rather_than_a_partition(self):
		"""A client that renders only `pending_tasks` still shows the late work."""
		unit = self.a_cabin()
		alert = self.an_alert("Housing Unit", unit, "2026-01-01")
		self.a_task(location_doctype="Housing Unit", location=unit, source_alert=alert)
		data = self.scan(unit)
		self.assertEqual(data["pending_task_count"], 1)
		self.assertEqual(data["overdue_task_count"], 1)
		self.assertEqual(data["overdue_tasks"][0]["name"], data["pending_tasks"][0]["name"])

	def test_a_hand_raised_task_carries_no_due_date_and_is_never_overdue(self):
		unit = self.a_cabin()
		self.a_task(location_doctype="Housing Unit", location=unit)
		data = self.scan(unit)
		self.assertIsNone(data["pending_tasks"][0]["due_date"])
		self.assertFalse(data["pending_tasks"][0]["overdue"])
		self.assertEqual(data["overdue_task_count"], 0)

	def test_open_compliance_alerts_on_the_record_come_back_with_it(self):
		unit = self.a_cabin()
		self.an_alert("Housing Unit", unit, "2026-01-01")
		data = self.scan(unit)
		self.assertEqual(data["due_compliance_count"], 1)
		self.assertTrue(data["due_compliance"][0]["overdue"])
		self.assertEqual(data["due_compliance"][0]["severity"], "Warning")

	def test_a_dismissed_alert_is_not_repeated_to_a_worker_in_front_of_the_cabin(self):
		"""A dismissal is somebody with the whole picture saying this one does
		not need doing. Showing it again re-opens a decision already made."""
		unit = self.a_cabin()
		self.an_alert("Housing Unit", unit, "2026-01-01", dismissed=1)
		self.assertEqual(self.scan(unit)["due_compliance_count"], 0)

	def test_the_history_is_the_timeline_for_whichever_thing_was_scanned(self):
		unit = self.a_cabin()
		STORE.seed(
			"Housing Inspection",
			[
				{
					"name": "HI-0001",
					"unit": unit,
					"company": MAIN,
					"inspection_date": "2026-05-01",
					"inspector_name": "Sam Ortiz",
					"workflow_state": "Recorded",
				}
			],
		)
		history = self.scan(unit)["recent_history"]
		self.assertEqual(len(history), 1)
		self.assertEqual(history[0]["doctype"], "Housing Inspection")
		self.assertEqual(history[0]["docname"], "HI-0001")

	def test_the_history_length_is_capped_and_the_cap_is_movable(self):
		self.an_asset()
		STORE.seed(
			"Asset State Log",
			[
				{
					"name": f"ASL-{index:04d}",
					"asset_name": VALVE,
					"asset_type": "Irrigation Valve",
					"action": "open_valve",
					"from_state": "closed",
					"to_state": "open",
				}
				for index in range(15)
			],
		)
		self.assertEqual(len(self.scan(VALVE)["recent_history"]), 10)
		self.assertEqual(len(self.scan(VALVE, history_limit=3)["recent_history"]), 3)


# ── 7. entity scoping ───────────────────────────────────────────────────────
class ItAnswersWithinOneCompany(ScanTestCase):
	def test_an_asset_in_another_entity_is_refused_by_name_rather_than_scanned(self):
		self.an_asset(company=OTHER)
		error = self.tool_error("universal_scan", {"content": VALVE, "company": MAIN})
		self.assertIn(OTHER, error)

	def test_a_badge_from_another_entity_reads_as_no_badge(self):
		"""Confirming a card exists somewhere else on the site is a fact a scan
		has no reason to hand to whoever is holding it."""
		badge = self.a_badge()
		error = self.tool_error("universal_scan", {"content": badge, "company": OTHER})
		self.assertIn("no badge", error)


# ── 8. the switch ───────────────────────────────────────────────────────────
class TheSwitchIsHonoured(ScanTestCase):
	def test_it_ships_off_like_every_other_write(self):
		self.configure(enabled=1, **{**ON, "allow_universal_scan": 0})
		self.an_asset()
		result = self.tool("universal_scan", {"content": VALVE})
		self.assertTrue(result.get("isError"))

	def test_the_switch_exists_on_the_settings_form(self):
		from .harness import _load_app_doctype

		fields = {field["fieldname"] for field in _load_app_doctype("erpnext_mcp_settings")["fields"]}
		self.assertIn("allow_universal_scan", fields)


# ── 10. what the valve can actually be told to do ───────────────────────────
class TheScanCarriesTheStateMachine(ScanTestCase):
	"""v0.76.0. `available_actions` says a state change is offered; these say which.

	The handset's `ScanAction` enum is five fixed strings — `log_state_change` is
	one of them, and it names a SCREEN. A worker standing at a valve is choosing
	between "open" and "close", and a scan that returned only the button sent the
	phone back for a second round trip to `get_available_actions` before it could
	draw the card the scan exists to draw.
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_log_asset_state_change=1, **ON)

	def test_a_valve_scan_carries_its_current_state(self):
		self.an_asset()
		data = self.scan(VALVE)
		self.assertEqual(data["current_state"], "closed")
		self.assertEqual(data["state_asset"], VALVE)

	def test_a_closed_valve_offers_opening_and_not_closing(self):
		self.an_asset()
		actions = [entry["action"] for entry in self.scan(VALVE)["state_actions"]]
		self.assertIn("open_valve", actions)
		self.assertNotIn("close_valve", actions)

	def test_an_open_valve_offers_closing_and_not_opening(self):
		self.an_asset()
		self.tool_data("log_asset_state_change", {"asset_name": VALVE, "action": "open_valve"})
		data = self.scan(VALVE)

		self.assertEqual(data["current_state"], "open")
		actions = [entry["action"] for entry in data["state_actions"]]
		self.assertIn("close_valve", actions)
		self.assertNotIn("open_valve", actions)

	def test_the_actions_carry_the_transition_they_would_make(self):
		self.an_asset()
		action = next(entry for entry in self.scan(VALVE)["state_actions"] if entry["action"] == "open_valve")
		self.assertEqual(action["from_state"], "closed")
		self.assertEqual(action["to_state"], "open")

	def test_the_shape_is_the_one_get_available_actions_returns(self):
		"""Same fact, same state machine — a screen that renders one renders the
		other, and an action offered here is one the log tool will accept."""
		self.configure(enabled=1, allow_get_available_actions=1, **ON)
		self.an_asset()
		scanned = self.scan(VALVE)["state_actions"]
		asked = self.tool_data("get_available_actions", {"asset_name": VALVE})["available_actions"]
		self.assertEqual(scanned, asked)

	def test_an_offered_action_is_one_the_log_tool_accepts(self):
		self.an_asset()
		offered = self.scan(VALVE)["state_actions"][0]["action"]
		data = self.tool_data("log_asset_state_change", {"asset_name": VALVE, "action": offered})
		self.assertEqual(data["action"], offered)

	def test_the_state_is_read_after_the_scans_own_write(self):
		"""The asset branch stamps `last_scan_at` before this is read. A state
		read first would be the one this scan replaced."""
		self.an_asset()
		self.tool_data("log_asset_state_change", {"asset_name": VALVE, "action": "open_valve"})
		self.assertEqual(self.scan(VALVE)["current_state"], "open")

	def test_the_button_vocabulary_is_left_alone(self):
		"""`available_actions` is the client's five strings and unknown values are
		dropped by its decoder. Putting `open_valve` in there would be a button
		that silently vanishes on the handset."""
		self.an_asset()
		data = self.scan(VALVE)
		self.assertEqual(data["available_actions"], ["create_task", "log_state_change", "report_issue"])

	def test_a_cabin_carries_no_actions_because_nothing_links_it_to_the_register(self):
		"""A Housing Unit has states, but they live on an Asset Register row and
		`related_asset` points at ERPNext's fixed-Asset doctype instead. Empty is
		the honest answer; resolving a docname out of the wrong register is not."""
		unit = self.a_cabin()
		data = self.scan(unit)
		self.assertIsNone(data["state_asset"])
		self.assertEqual(data["state_actions"], [])

	def test_the_cabins_own_asset_tag_is_what_carries_them(self):
		self.an_asset(name="MC-Cabin-Asset", asset_type="Housing Unit")
		data = self.scan("MC-Cabin-Asset")
		self.assertEqual(data["current_state"], "vacant")
		self.assertIn("mark_occupied", [entry["action"] for entry in data["state_actions"]])

	def test_every_branch_carries_the_three_keys(self):
		"""A client renders one card for five entity types rather than testing
		for keys that are only on two of them."""
		self.an_asset()
		badge = self.a_badge()
		block = self.a_block()
		for content in (VALVE, badge, block, "a supplier's carton barcode"):
			with self.subTest(scan=content):
				data = self.scan(content)
				for key in ("state_asset", "current_state", "state_actions"):
					self.assertIn(key, data)

	def test_a_block_scan_carries_no_state_machine(self):
		block = self.a_block()
		data = self.scan(block)
		self.assertIsNone(data["current_state"])
		self.assertEqual(data["state_actions"], [])
