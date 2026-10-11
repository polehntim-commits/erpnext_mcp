# SPDX-License-Identifier: MIT
"""The cherry degree-day stage table over MCP. v0.316.0.

Per crop and variety, BBCH stage → °F·days from the method's biofix and base, through the config lifecycle (read,
draft, preview over MCP; publish in the Desk). A stage may be named and left `to_calibrate`. The proposed sweet-cherry
table is seeded as a DRAFT — every BBCH stage, 'Van' full bloom at OSU 2014's 254 °C·day, the rest to calibrate.
"""

from unittest import mock

import frappe

from erpnext_mcp import degree_days, install, pest_dd

from .harness import STORE
from .test_degree_days import DegreeDayTestCase


class TheTable(DegreeDayTestCase):
	def test_stages_to_calibrate_are_named_not_estimated(self):
		with pest_dd.overlay(pest_dd.PHENOLOGY_KEY, pest_dd.proposed_stage_table()):
			van = degree_days.crop_entry("Sweet Cherry", "Van")
			bing = degree_days.crop_entry("Sweet Cherry", "Bing")
		self.assertEqual(van["stages"], [(457.2, "65")])
		self.assertIn("51", van["stages_to_calibrate"])
		self.assertNotIn("65", van["stages_to_calibrate"])
		self.assertEqual(bing.get("stages") or [], [], "nothing published for Bing yet")
		self.assertIn("65", bing["stages_to_calibrate"])

	def test_every_threshold_is_sourced(self):
		body = pest_dd.proposed_stage_table()
		methods = body["crops"]["sweet cherry"]["methods"]
		for name, method in methods.items():
			for stage in method["stages"]:
				self.assertTrue(stage["source"] and stage["to_calibrate"], name)
		van65 = next(s for s in methods["mar1_2c"]["stages_by_variety"]["Van"] if s["bbch"] == "65")
		self.assertIn("OSU 2014", van65["source"])
		self.assertEqual(pest_dd.validate(body, pest_dd.PHENOLOGY_KEY)["errors"], [])

	def test_a_measured_stage_out_of_order_is_refused(self):
		body = pest_dd.proposed_stage_table()
		body["crops"]["sweet cherry"]["methods"]["mar1_2c"]["stages_by_variety"]["Van"] = [
			{"bbch": "65", "dd": 457.2, "source": "x"}, {"bbch": "69", "dd": 400, "source": "x"}]
		self.assertTrue(pest_dd.validate(body, pest_dd.PHENOLOGY_KEY)["errors"])


class OverMcp(DegreeDayTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_config=1, allow_draft_config=1, allow_get_degree_days=1)
		frappe.db.set_value("Field", "B7", "variety", "Van")
		STORE.commit()

	def test_draft_then_preview_shows_each_blocks_method_biofix_and_stages(self):
		body = pest_dd.proposed_stage_table()
		self.tool_data("config", {"action": "draft", "kind": "crop_phenology", "key": "crop_phenology",
		                          "body": body})
		STORE.commit()
		with mock.patch.object(degree_days, "_crop_of", return_value="Sweet Cherry"):
			got = self.tool_data("config", {"action": "preview", "kind": "crop_phenology", "key": "crop_phenology",
			                                "body": body, "blocks": ["B7"]})
		row = got["preview"]["blocks"][0] if "preview" in got else got["blocks"][0]
		self.assertEqual((row["variety"], row["method_draft"], row["biofix_draft"]), ("Van", "mar1_2c", "03-01"))
		self.assertEqual(row["stages_draft"], [["65", 457.2]])
		self.assertIn("51", row["to_calibrate_draft"])

	def test_publish_stays_in_the_desk(self):
		draft = self.tool_data("config", {"action": "draft", "kind": "crop_phenology", "key": "crop_phenology",
		                                  "body": pest_dd.proposed_stage_table()})
		STORE.commit()
		self.configure(enabled=1, allow_config=1, allow_publish_config=1)
		error = self.tool_error("config", {"action": "publish", "kind": "crop_phenology", "key": "crop_phenology",
		                                   "version": draft["draft"]["version"]})
		self.assertIn("Desk", error)


class TheSeededDraft(DegreeDayTestCase):
	def test_install_drafts_it_once_and_never_publishes(self):
		install._crop_phenology_stage_proposal()
		STORE.commit()
		install._crop_phenology_stage_proposal()
		STORE.commit()
		rows = [r for r in STORE.rows("Farm Config Version") if r.get("config_key") == pest_dd.PHENOLOGY_KEY]
		self.assertEqual([r["status"] for r in rows], ["Draft"])
		self.assertEqual(rows[0].get("authored_by"), "AI-proposed")
		self.assertEqual(pest_dd.phenology()[1], "built-in", "nothing is published by the seed")
