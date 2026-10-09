# SPDX-License-Identifier: MIT
"""Points on the map that belong to a piece of work. v0.286.0 (pattern P3).

A job's entrance and delivery spot, a season's loading area and hive drops are Field Pins — one table, one map
layer that also reads the valves and hazard markers (which stay assets). Setting a pin twice moves it; the job page
answers exactly as before.
"""

import json

from erpnext_mcp import job_links, pins

from .harness import STORE, frappe
from .test_job_links import JobCase


class ThePins(JobCase):
	def test_the_job_keeps_its_points_as_pins_not_columns(self):
		meta = json.load(open("erpnext_mcp/erpnext_mcp/doctype/contractor_job/contractor_job.json"))
		names = {f["fieldname"] for f in meta["fields"]}
		self.assertFalse(names & {"entrance_lat", "entrance_lon", "delivery_spot_lat", "delivery_spot_lon"})
		entrance = pins.one("Contractor Job", self.job, "Entrance")
		self.assertEqual((entrance["latitude"], entrance["longitude"]), (45.5838, -121.2276))
		self.assertEqual(job_links.page_data(self.job)["entrance"], {"lat": 45.5838, "lon": -121.2276})
		self.assertIsNone(job_links.page_data(self.job)["delivery_spot"])

	def test_setting_a_pin_again_moves_it(self):
		job_links.set_point(self.job, "Delivery Spot", (45.5841, -121.2280), label="Mix-load pad")
		job_links.set_point(self.job, "Delivery Spot", (45.5842, -121.2281), label="Mix-load pad")
		STORE.commit()
		self.assertEqual(len(pins.of("Contractor Job", self.job, "Delivery Spot")), 1)
		self.assertEqual(job_links.page_data(self.job)["delivery_spot"],
		                 {"lat": 45.5842, "lon": -121.2281, "label": "Mix-load pad"})

	def test_a_bad_point_or_kind_is_refused(self):
		with self.assertRaisesRegex(job_links.JobError, "not a point"):
			job_links.set_point(self.job, "Entrance", (95, 10))
		with self.assertRaisesRegex(pins.PinError, "kind is one of"):
			pins.put("Contractor Job", self.job, "Bonfire", 45.5, -121.2)

	def test_one_layer_reads_pins_and_the_assets(self):
		blocks = [r.get("field") for r in frappe.get_doc("Contractor Job", self.job).get("fields")]
		layer = pins.layer(blocks=blocks, source_doctype="Contractor Job", source=self.job)
		kinds = {item["kind"] for item in layer}
		self.assertIn("Entrance", kinds)
		self.assertTrue(kinds & {"valve", "hazard"} or not any(
			pins.layer(blocks=blocks, include=("valves", "hazards"))), "assets come through the same layer")
		self.assertEqual(pins.layer(blocks=blocks, source_doctype="Contractor Job", source=self.job, include=("pins",))[0]["kind"],
		                 "Entrance")
