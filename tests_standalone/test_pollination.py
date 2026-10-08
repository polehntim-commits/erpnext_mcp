# SPDX-License-Identifier: MIT
"""Pollination. v0.275.0 (docs/contracts/pollination_v0_275.yaml).

Mill Creek's Bing Block (both blocks, by the group alias): drops inside the outlines and clear of the valve, hives by
acre on pallets of four, trips batched by the machine; a draft plan, published; the season's job off until switched
on; the crew's trips held until the beekeeper taps Delivered (with the count) and the gather-up until petal fall;
the pickup refused until the gather-up is done; the five counts and their flags, resolved with a reason; drops moved
inside the blocks only and never once distribution has started; the bee hold from the IPM graph; the rental invoice
check; the phone's gates and frozen answers.
"""

import json
from unittest import mock

import frappe

from erpnext_mcp import (
	config_lifecycle,
	flags,
	go_hold,
	job_links,
	phone_config,
	pollination,
	task_templates,
)
from erpnext_mcp.api import mobile as mobile_api

from . import test_contract_v0_262_0 as v262
from .harness import STORE, set_roles
from .test_api_mobile import MAIN, WORKER
from .test_farmops_api import FarmOpsAPITestCase
from .test_field_self_service import CENTER, WIND, mill_creek

AREA = (45.5850, -121.2295)
HERE = v262.HERE.parent / "v0_275_0"


def flag(key, value=True):
	flags.upsert(key, "Flag", value, company=MAIN, description="t", owner_area="pollination", active=True)
	STORE.commit()


class PollinationCase(FarmOpsAPITestCase):
	def setUp(self):
		super().setUp()
		mill_creek(self)
		task_templates.seed_farm_task_templates()
		job_links.seed()
		STORE.commit()
		patcher = mock.patch.object(frappe.utils, "today", lambda: "2027-03-20")
		patcher.start()
		self.addCleanup(patcher.stop)

	def published(self):
		out = pollination.place(MAIN, 2027, ["Bing Block"], loading_area=AREA, actor="tim", rental={"rate_per_hive": 85})
		with config_lifecycle.desk_action():
			phone_config.publish(pollination.KIND, out["plan"], out["version"], "test", "Administrator")
		STORE.commit()
		return out

	def season(self):
		self.published()
		flag(pollination.FLAG)
		job = pollination.create_job(MAIN, 2027, supplier="", start_date="2027-04-01", end_date="2027-05-15")["job"]
		STORE.commit()
		return job

	def link(self, job):
		flag(job_links.FLAG)
		job_links.mark_ready(job, "tim", override_reason="no prep")
		token = job_links.issue_link(job, "tim")["url"].rsplit("/", 1)[1]
		STORE.commit()
		return job_links.find(token)

	def finish(self, trips):
		for trip in trips:
			frappe.db.set_value("Farm Task", trip["task"], "state", "Completed")
		STORE.commit()

	def op(self, job):
		return json.loads(frappe.db.get_value("Contractor Job", job, "operation"))


class ThePlacement(PollinationCase):
	def test_drops_inside_clear_by_acre_and_batched(self):
		from erpnext_mcp import field_card

		out = pollination.place(MAIN, 2027, ["Bing Block"], loading_area=AREA, save=False)
		by_block = {}
		for drop in out["drops"]:
			ring = frappe.db.get_value("Field", drop["block"], "boundary_geojson")
			self.assertTrue(field_card.contains(ring, drop["lat"], drop["lon"]), drop["id"])
			self.assertGreaterEqual(pollination.distance_ft((drop["lat"], drop["lon"]), (45.5852, -121.2310)), 30)
			by_block[drop["block"]] = by_block.get(drop["block"], 0) + drop["hives"]
		self.assertEqual(by_block, {CENTER: 11, WIND: 13}, "acres × 1 hive")
		self.assertEqual(out["pallets"], 7)
		self.assertEqual([t["pallets"] for t in out["trips"]], [6, 1], "six pallets a trip")
		self.assertTrue(all(t["minutes"] > 10 for t in out["trips"]))

	def test_saved_as_a_draft_and_the_job_is_off_until_switched_on(self):
		out = pollination.place(MAIN, 2027, ["Bing Block"], loading_area=AREA)
		self.assertEqual(out["status"], "Draft")
		with self.assertRaisesRegex(pollination.PollinationError, "no published"):
			pollination.plan(MAIN, 2027)
		with config_lifecycle.desk_action():
			phone_config.publish(pollination.KIND, out["plan"], out["version"], "test", "Administrator")
		with self.assertRaisesRegex(pollination.PollinationError, "pollination_enabled"):
			pollination.create_job(MAIN, 2027)


class TheSeason(PollinationCase):
	def test_held_trips_counts_flags_and_the_pickup_gate(self):
		job = self.season()
		op = self.op(job)
		self.assertEqual(op["expected"], 24)
		states = {frappe.db.get_value("Farm Task", t["task"], "state") for t in op["distribute"] + op["gather"]}
		self.assertEqual(states, {"Draft"}, "nothing on the board until the beekeeper delivers")
		link = self.link(job)
		job_links.record_event(link, "Delivered", counts={"hives": 22, "pallets": 7, "frame_strength": 8.5})
		STORE.commit()
		self.assertEqual({frappe.db.get_value("Farm Task", t["task"], "state") for t in op["distribute"]}, {"Available"})
		self.assertEqual(frappe.db.get_value("Contractor Job", job, "status"), "In Progress")
		flags_now = pollination.reconcile(job)["flags"]
		self.assertEqual([f["flag"] for f in flags_now], ["expected_vs_delivered"])
		self.assertEqual(flags_now[0]["difference"], -2)
		self.finish(op["distribute"])
		with self.assertRaisesRegex(job_links.JobError, "Not yet"):
			job_links.record_event(link, "Picked Up", counts={"hives": 22})
		self.assertEqual(pollination.release_gather(job)["released"], [t["task"] for t in op["gather"]])
		self.finish(op["gather"])
		job_links.record_event(link, "Picked Up", counts={"hives": 22})
		STORE.commit()
		got = pollination.counts(job)
		self.assertEqual(got, {"expected": 24, "delivered": 22, "placed": 24, "gathered": 24, "picked_up": 22})
		status = pollination.resolve_flag(job, "expected_vs_delivered", "two short on the truck — billed for 22",
		                                  actor="tim")
		resolved = {f["flag"]: f["resolved"] for f in status["flags"]}
		self.assertTrue(resolved["expected_vs_delivered"])
		self.assertIn("delivered_vs_placed", resolved, "the crew placed 24 of 22 delivered — counted wrong somewhere")
		with self.assertRaisesRegex(pollination.PollinationError, "say why"):
			pollination.resolve_flag(job, "delivered_vs_placed", "")

	def test_petal_fall_releases_the_gather_up_once(self):
		job = self.season()
		link = self.link(job)
		job_links.record_event(link, "Delivered", counts={"hives": 24})
		STORE.seed("Crop Observation", [{"name": "OBS-PF", "block_doctype": "Field", "block": WIND,
		                                 "observation_type": "Growth Stage", "growth_stage_code": "69",
		                                 "observed_on": "2027-04-28"}])
		STORE.commit()
		with mock.patch.object(frappe.utils, "today", lambda: "2027-04-28"):
			out = pollination.daily()
			pollination.daily()
		self.assertEqual(len(out[job]["released"]), 2)
		alerts = [a for a in STORE.rows("Compliance Alert") if a.get("alert_type") == "pollination_petal_fall"]
		self.assertEqual(len(alerts), 1)
		self.assertIn("Wind Mecine", alerts[0]["alert_message"])


class MovingDrops(PollinationCase):
	def test_inside_only_and_never_once_started(self):
		job = self.season()
		drop = self.op(job)["drops"][0]
		with self.assertRaisesRegex(pollination.PollinationError, "outside"):
			pollination.update_drops(job, [{"id": drop["id"], "lat": 45.60, "lon": -121.20}])
		out = pollination.update_drops(job, [{"id": drop["id"], "lat": drop["lat"] + 0.00005, "lon": drop["lon"]}], "tim")
		self.assertEqual(out["drops"][0]["lat"], round(drop["lat"] + 0.00005, 6))
		STORE.commit()
		first = self.op(job)["distribute"][0]["task"]
		frappe.db.set_value("Farm Task", first, "state", "In-Progress")
		STORE.commit()
		with self.assertRaisesRegex(pollination.PollinationError, "started"):
			pollination.update_drops(job, [{"id": drop["id"], "lat": drop["lat"], "lon": drop["lon"]}])


class BeesAndSprays(PollinationCase):
	def test_the_rule_engine_sees_hives_out_and_bee_toxic_products(self):
		STORE.seed("Item", [{"name": "WARRIOR-II", "item_code": "WARRIOR-II", "item_name": "Warrior II", "stock_uom": "Gal",
		                     "item_defaults": [], "reorder_levels": []}])
		STORE.seed("IPM Organism", [{"name": "Honeybee", "organism_name": "Honeybee", "kind": "Pollinator"},
		                            {"name": "Warrior II", "organism_name": "Warrior II", "kind": "Product",
		                             "item": "WARRIOR-II"}])
		STORE.seed("IPM Relationship", [{"name": "E1", "subject": "Honeybee", "relation": "harmed_by",
		                                 "object": "Warrior II", "weight": 0.9, "enabled": 1}])
		job = self.season()
		task = {"name": "FT-SPRAY", "task_type": "Spray", "location_doctype": "Field", "location": WIND,
		        "company": MAIN, "materials_used": json.dumps([{"item_code": "WARRIOR-II", "qty": 1}])}
		before = pollination.provider_values(task, {})
		self.assertEqual((before["hives_out"], before["bee_toxic"]), (False, True), "no hives yet")
		job_links.record_event(self.link(job), "Delivered", counts={"hives": 24})
		STORE.commit()
		after = pollination.provider_values(task, {})
		self.assertEqual((after["hives_out"], after["bee_toxic"], after["bee_toxic_products"]), (True, True, "Warrior II"))
		spec = next(s for s in go_hold.preset_specs() if s["rule_id"] == "go_hold_spray_bees_out")
		self.assertEqual(spec["enabled"], 0, "seeded off")


class TheInvoice(PollinationCase):
	def test_billed_against_delivered_at_the_rate(self):
		job = self.season()
		job_links.record_event(self.link(job), "Delivered", counts={"hives": 22})
		STORE.seed("Supplier", [{"name": "Hood River Bees", "supplier_name": "Hood River Bees"}])
		STORE.seed("Purchase Invoice", [{"name": "PINV-BEES", "supplier": "Hood River Bees", "company": MAIN,
		                                 "grand_total": 2040.0, "docstatus": 0,
		                                 "items": [{"item_code": "BEES", "qty": 24, "rate": 85}]}])
		STORE.commit()
		out = pollination.link_invoice(job, "PINV-BEES", "tim")
		self.assertFalse(out["matches"])
		self.assertEqual(out["expected_amount"], 1870.0)
		self.assertTrue(any("24 hives, 22 delivered" in i for i in out["issues"]))
		self.assertEqual(frappe.db.get_value("Purchase Invoice", "PINV-BEES", "grand_total"), 2040.0, "never changed")


class FromThePhone(PollinationCase):
	def test_foreman_reads_manager_moves(self):
		job = self.season()
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.be(WORKER)
		data = mobile_api.get_hive_map(job=job)
		self.assertFalse(data["may_move"])
		with self.assertRaises(frappe.PermissionError):
			mobile_api.update_hive_drops(job=job, moves="[]")
		self.assertEqual(mobile_api.get_pollination_status(job=job)["counts"]["expected"], 24)
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be(WORKER)
		drop = data["drops"][0]
		moved = mobile_api.update_hive_drops(job=job, moves=json.dumps([{"id": drop["id"], "lat": drop["lat"],
		                                                                "lon": drop["lon"]}]))
		self.assertEqual(moved["expected"], 24)


class Contract(PollinationCase):
	def check(self, name, value):
		before = v262.HERE
		v262.HERE = HERE
		try:
			v262.check(self, name, value)
		finally:
			v262.HERE = before

	def test_the_phone_answers(self):
		job = self.season()
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be(WORKER)
		self.check("get_hive_map", mobile_api.get_hive_map(job=job))
		self.check("get_pollination_status", mobile_api.get_pollination_status(job=job))
