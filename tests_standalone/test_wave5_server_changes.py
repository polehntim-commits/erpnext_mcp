# SPDX-License-Identifier: MIT
"""v0.195.0 — fafo_ios SERVER_CHANGES §42, §43 and §44, over the phone's transport.

§44  `designation` on `get_current_user_context`: a Checker holds Field Worker,
     so only the job title tells them from a picker.
§42  `get_my_housing`: the caller's own current assignment, never anyone else's.
§43  `record_spray_application` files a SPRAY APPLICATION (not only Spray REI
     rows) through a Spray Farm Task, and `list_spray_applications` /
     `get_spray_application` read it back in the phone's history shape. One
     spray is one REI per block and ONE PHI window per block — the task the
     application cites is not counted a second time.

The answers are asserted key by key against the SERVER_CHANGES payloads, because
fafo_ios names these paths and has no Codable for them yet to mirror.
"""

from erpnext_mcp import compliance_fields
from erpnext_mcp.tools import spray as spray_tools
from erpnext_mcp.tools import spray_rei

from .fixtures import MAIN, OTHER, SPRAY, seed_masters, seed_stock
from .harness import STORE
from .test_api_mobile import OUTSIDER_EMPLOYEE, WORKER_EMPLOYEE
from .test_farmops_api import CONTEXT, PREFIX, FarmOpsAPITestCase

HOUSING = f"{PREFIX}/mobile/get_my_housing"
RECORD = f"{PREFIX}/mobile/record_spray_application"
HISTORY = f"{PREFIX}/mobile/list_spray_applications"
ONE = f"{PREFIX}/mobile/get_spray_application"

BLOCK = "Yellow Camp Block 3 - MC"
BLOCK_TWO = "Yellow Camp Block 4 - MC"


# ── §44 ─────────────────────────────────────────────────────────────────────
class TheJobTitleIsOnTheContext(FarmOpsAPITestCase):
	def test_a_checker_is_told_apart_from_a_picker(self):
		STORE.get_raw("Employee", WORKER_EMPLOYEE)["designation"] = "Checker"
		context = self.message(CONTEXT)
		self.assertEqual(context["designation"], "Checker")
		self.assertIn("Field Worker", context["mobile_roles"], "a designation adds no role")

	def test_no_designation_is_null_not_empty(self):
		self.assertIsNone(self.message(CONTEXT)["designation"])


# ── §42 ─────────────────────────────────────────────────────────────────────
class MyOwnBed(FarmOpsAPITestCase):
	def setUp(self):
		super().setUp()
		self.unit = self.a_camp()
		self.parcel = STORE.get_raw("Housing Unit", self.unit)["parcel"]
		STORE.get_raw("Parcel", self.parcel)["address"] = "4000 Mill Creek Rd, The Dalles OR"
		STORE.get_raw("Housing Unit", self.unit)["last_habitability_inspection"] = "2026-06-01"

	def assign(self, employee, name, end_date=None):
		STORE.seed(
			"Housing Assignment",
			[
				{
					"name": name,
					"unit": self.unit,
					"employee": employee,
					"employee_name": employee,
					"parcel": self.parcel,
					"assigned_date": "2026-06-01",
					"end_date": end_date,
					"status": "Ended" if end_date else "Current",
				}
			],
		)

	def test_no_assignment_is_two_nulls(self):
		self.assertEqual(self.message(HOUSING), {"assignment": None, "unit": None})

	def test_the_workers_own_assignment_in_the_phones_shape(self):
		self.assign(WORKER_EMPLOYEE, "HA-1")
		self.assign("EMP-SOMEONE", "HA-2")
		data = self.message(HOUSING)
		self.assertEqual(
			data["assignment"],
			{
				"name": "HA-1",
				"housing_unit": self.unit,
				"unit_label": "MC-Cabin-01",
				"bed": None,
				"camp": self.parcel,
				"start_date": "2026-06-01",
				"end_date": None,
				"status": "Current",
			},
		)
		self.assertEqual(
			data["unit"],
			{
				"address": "4000 Mill Creek Rd, The Dalles OR",
				"occupancy": 2,
				"capacity": 4,
				"last_habitability_inspection": "2026-06-01",
			},
		)

	def test_an_ended_assignment_is_not_a_bed(self):
		self.assign(WORKER_EMPLOYEE, "HA-1", end_date="2026-07-01")
		self.assertIsNone(self.message(HOUSING)["assignment"])

	def test_the_body_cannot_name_another_worker(self):
		self.assign(OUTSIDER_EMPLOYEE, "HA-9")
		self.assertIsNone(self.message(HOUSING, {"employee": OUTSIDER_EMPLOYEE})["assignment"])


# ── §43 ─────────────────────────────────────────────────────────────────────
class SprayTestCase(FarmOpsAPITestCase):
	def setUp(self):
		super().setUp()
		seed_masters()
		seed_stock()
		self.configure(
			enabled=1,
			allow_create_parcel=1,
			allow_create_field=1,
			allow_create_farm_task=1,
			allow_claim_farm_task=1,
		)
		compliance_fields.install_compliance_fields()
		item = STORE.get_raw("Item", SPRAY)
		item["rei_hours"] = 4
		item["phi_days"] = 14
		self.tool_data(
			"create_parcel", {"owning_entity": MAIN, "parcel_name": "Mill Creek", "acreage": 131.43}
		)
		for name in ("Yellow Camp Block 3", "Yellow Camp Block 4"):
			self.tool_data(
				"create_field",
				{"parcel": "Mill Creek", "field_name": name, "acreage": 12.5, "variety": "Bing"},
			)

	def spray(self, blocks=(BLOCK,), qty=5, **extra):
		body = {"blocks": list(blocks), "materials_used": [{"item_code": SPRAY, "qty": qty, "uom": "Lb"}]}
		body.update(extra)
		return self.message(RECORD, body)

	def reis(self, block=BLOCK):
		return [row for row in STORE.rows("Spray REI") if row["block"] == block]


class ASprayFromThePhone(SprayTestCase):
	def test_it_files_a_spray_application_through_a_completed_spray_task(self):
		data = self.spray()
		application = data["spray_application"]
		self.assertTrue(STORE.get_raw("Spray Application", application["name"]))
		self.assertEqual(application["status"], "Applied")
		task = STORE.get_raw("Farm Task", data["task"]["name"])
		self.assertEqual(task["task_type"], "Spray")
		self.assertEqual(task["state"], "Completed")
		self.assertEqual(application["source_task"], task["name"])
		self.assertEqual(data["reis"], [{"block": BLOCK, "expires_at": application["rei_expires_at"]}])

	def test_the_tank_mix_leaves_the_shed(self):
		self.spray(qty=5)
		issues = [row for row in STORE.rows("Stock Entry") if row.get("purpose") == "Material Issue"]
		self.assertEqual(len(issues), 1)
		self.assertAlmostEqual(float(issues[0]["items"][0]["qty"]), 5.0)

	def test_one_spray_is_one_rei_and_one_phi_per_block(self):
		self.spray(blocks=(BLOCK, BLOCK_TWO))
		self.assertEqual(len(self.reis(BLOCK)), 1)
		self.assertEqual(len(self.reis(BLOCK_TWO)), 1)
		self.assertEqual(len(spray_tools.phi_windows_for_blocks([BLOCK], MAIN)), 1)
		self.assertEqual(len(spray_rei.active_for_blocks([BLOCK], MAIN)), 1)

	def test_a_single_block_task_is_not_counted_twice_either(self):
		"""One Field → the task names it as its location and is stamped with PHI.
		Without the dedupe the task and the application are two windows."""
		data = self.spray()
		self.assertEqual(STORE.get_raw("Farm Task", data["task"]["name"])["location"], BLOCK)
		windows = spray_tools.phi_windows_for_blocks([BLOCK], MAIN)
		self.assertEqual([w["source_doctype"] for w in windows], ["Spray Application"])

	def test_the_tank_total_becomes_a_per_acre_rate(self):
		application = self.spray(qty=5)["spray_application"]
		line = application["products_applied"][0]
		self.assertAlmostEqual(float(line["rate_per_acre"]), 0.4)
		self.assertAlmostEqual(float(line["total_applied"]), 5.0)

	def test_the_weather_lands_in_its_columns(self):
		application = self.spray(
			wind_speed_mph=4, wind_direction="NW", temperature_f=71, relative_humidity=38
		)["spray_application"]
		self.assertEqual(application["weather"]["humidity_pct"], 38.0)
		self.assertEqual(application["weather"]["wind_direction"], "NW")
		self.assertEqual(application["weather"]["source"], "Observed")

	def test_the_applicator_is_the_login(self):
		application = self.spray(applicator="someone@else.test")["spray_application"]
		self.assertEqual(application["applicator"], "ana@example.test")

	def test_a_task_somebody_else_holds_cannot_be_closed_by_it(self):
		task = self.tool_data(
			"create_farm_task",
			{
				"task_name": "Spray 3",
				"task_type": "Spray",
				"company": MAIN,
				"evidence_required": {"findings_text": True},
				"assigned_to": OUTSIDER_EMPLOYEE,
			},
		)["name"]
		_status, body = self.refusal(
			RECORD,
			{"blocks": [BLOCK], "materials_used": [{"item_code": SPRAY, "qty": 5}], "source_task": task},
		)
		self.assertIn("not a task you are holding", body["error"])
		self.assertEqual(STORE.rows("Spray Application"), [])

	def test_blocks_and_materials_are_required(self):
		_status, body = self.refusal(RECORD, {"materials_used": [{"item_code": SPRAY, "qty": 5}]})
		self.assertIn("blocks is required", body["error"])
		_status, body = self.refusal(RECORD, {"blocks": [BLOCK]})
		self.assertIn("materials_used is required", body["error"])


class TheSprayHistory(SprayTestCase):
	def test_the_history_row_in_the_phones_shape(self):
		name = self.spray(wind_speed_mph=3)["spray_application"]["name"]
		data = self.message(HISTORY)
		self.assertFalse(data["truncated"])
		row = data["applications"][0]
		self.assertEqual(row["name"], name)
		self.assertEqual(row["blocks"], [BLOCK])
		self.assertEqual(row["products"][0]["item_code"], SPRAY)
		self.assertAlmostEqual(float(row["products"][0]["qty"]), 5.0)
		self.assertEqual(row["products"][0]["uom"], "Lb")
		self.assertEqual(row["wind_speed_mph"], 3.0)
		for key in ("completed_at", "sprayer", "applicator_name", "rei_expires_at", "phi_clears_on"):
			self.assertIn(key, row)

	def test_the_block_filter(self):
		self.spray(blocks=(BLOCK,))
		self.spray(blocks=(BLOCK_TWO,))
		rows = self.message(HISTORY, {"block": BLOCK_TWO})["applications"]
		self.assertEqual([row["blocks"] for row in rows], [[BLOCK_TWO]])

	def test_one_application_by_name(self):
		name = self.spray()["spray_application"]["name"]
		self.assertEqual(self.message(ONE, {"name": name})["blocks"], [BLOCK])

	def test_another_entitys_application_reads_as_not_found(self):
		name = self.spray()["spray_application"]["name"]
		STORE.get_raw("Spray Application", name)["company"] = OTHER
		status, _body = self.refusal(ONE, {"name": name})
		self.assertGreaterEqual(status, 400)
		self.assertEqual(self.message(HISTORY)["applications"], [])


class TheSprayHistoryIsReadInOneGo(SprayTestCase):
	"""v0.231.3. The history page reads every application, block, REI and mix in a
	handful of queries; each row must equal the one-at-a-time read exactly."""

	def test_every_row_equals_the_single_read(self):
		names = [
			self.spray(blocks=(BLOCK,), wind_speed_mph=2)["spray_application"]["name"],
			self.spray(blocks=(BLOCK, BLOCK_TWO), qty=7)["spray_application"]["name"],
			self.spray(blocks=(BLOCK_TWO,), notes="second pass")["spray_application"]["name"],
		]
		def steady(value):
			# The countdowns are read against the clock and move between two reads.
			if isinstance(value, dict):
				return {k: steady(v) for k, v in value.items() if k not in ("hours_remaining", "minutes_remaining")}
			if isinstance(value, list):
				return [steady(v) for v in value]
			return value

		rows = {row["name"]: row for row in self.message(HISTORY)["applications"]}
		self.assertEqual(set(rows), set(names))
		for name in names:
			with self.subTest(name=name):
				self.assertEqual(steady(rows[name]), steady(self.message(ONE, {"name": name})))

	def test_the_page_does_not_query_per_row(self):
		from unittest import mock

		from erpnext_mcp.tools import spray_rei

		for _ in range(4):
			self.spray()
		with mock.patch.object(spray_rei, "active_for_blocks", wraps=spray_rei.active_for_blocks) as reis:
			self.message(HISTORY)
		self.assertEqual(reis.call_count, 1, "one REI read per company, not one per application")
