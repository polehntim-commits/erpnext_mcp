# SPDX-License-Identifier: MIT
"""/app/ipm-map — the IPM graph in the Desk. v0.263.0.

The page reads the SAME ipm_graph.graph() as the phone and MCP, behind the SAME edit gate. Under test:
the filters, the flags the page draws (MBTA, products harming what is active), the side panel with
recent observations, the gate (a worker is view-only), the date → stage filter, and that the page, its
form buttons and its vendored library are shipped with no CDN.
"""

import json
import pathlib

import frappe

from erpnext_mcp import ipm_graph
from erpnext_mcp.api import ipm_map

from .fixtures import MAIN, V12TestCase
from .harness import ROLES, STORE

REPO = pathlib.Path(__file__).resolve().parent.parent
PAGE = REPO / "erpnext_mcp" / "erpnext_mcp" / "page" / "ipm_map"
BLOCK = "Block 7 Bing"


class DeskCase(V12TestCase):
	def setUp(self):
		super().setUp()
		ipm_graph.seed()
		STORE.commit()
		STORE.seed("Field", [{"name": BLOCK, "field_name": BLOCK, "crop": "Cherries", "owning_entity": MAIN}])
		self._roles = list(ROLES.get("Administrator", []))
		self.addCleanup(lambda: ROLES.__setitem__("Administrator", self._roles))

	def as_roles(self, *roles):
		ROLES["Administrator"] = list(roles)


class TheView(DeskCase):
	def test_it_is_the_same_graph_the_phone_reads(self):
		self.as_roles("System Manager")
		desk = ipm_map._view(crop="Cherries", stage=85)
		phone = ipm_graph.graph(crop="Cherries", stage=85, limit=ipm_graph.MAX_LIMIT)
		self.assertEqual({e["id"] for e in desk["edges"]}, {e["id"] for e in phone["edges"]})
		self.assertEqual(desk["graph_version"], phone["graph_version"])
		self.assertTrue(desk["can_edit"])

	def test_filters_narrow_the_same_answer(self):
		self.as_roles("System Manager")
		only = ipm_map._view(crop="Cherries", depth=3, relations=json.dumps(["harmed_by"]), kinds=json.dumps(["beneficial", "product"]))
		self.assertTrue(only["edges"])
		self.assertEqual({e["relation"] for e in only["edges"]}, {"harmed_by"})
		lit = ipm_map._view(crop="Cherries", provenance="User Entered")
		self.assertEqual(lit["edges"], [])

	def test_flags_mbta_and_products_that_harm_what_is_active(self):
		self.as_roles("System Manager")
		v = ipm_map._view(crop="Cherries", stage=75, depth=3)
		nodes = {n["id"]: n for n in v["nodes"]}
		self.assertTrue(nodes["american-robin"]["protected"])
		self.assertFalse(nodes["european-starling"]["protected"])
		self.assertIn("ladybug", nodes["warrior-ii"]["harms_active"])

	def test_a_date_on_a_block_reads_the_stage_seen_by_then(self):
		self.as_roles("System Manager")
		STORE.seed("Crop Observation", [
			{"name": "S1", "block_doctype": "Field", "block": BLOCK, "growth_stage_code": "65", "observed_on": "2026-05-01", "company": MAIN},
			{"name": "S2", "block_doctype": "Field", "block": BLOCK, "growth_stage_code": "85", "observed_on": "2026-06-20", "company": MAIN}])
		self.assertEqual(ipm_map._view(block=BLOCK, date="2026-05-15")["stage"]["bbch"], 65)
		self.assertEqual(ipm_map.stage_on(BLOCK, "2026-07-01"), 85)


class ThePanelAndTheGate(DeskCase):
	def test_the_panel_carries_recent_observations(self):
		self.as_roles("System Manager")
		STORE.seed("Crop Observation", [{"name": "O1", "block_doctype": "Field", "block": BLOCK, "threat": "Spider Mites",
		                                 "count_observed": 6, "observed_on": "2026-07-20", "company": MAIN}])
		panel = ipm_map._panel(organism="spider-mites")
		self.assertEqual([o["name"] for o in panel["observations"]], ["O1"])
		self.assertTrue(panel["thresholds"])

	def test_a_view_only_user_cannot_save(self):
		self.as_roles("Foreman")
		self.assertFalse(ipm_map._view(crop="Cherries")["can_edit"])
		with self.assertRaisesRegex(Exception, "restricted to"):
			ipm_map._save(subject="ladybug", relation="preys_on", object="cherry-slug")

	def test_a_manager_adds_disables_and_never_touches_literature(self):
		self.as_roles("Farm Manager")
		added = ipm_map._save(subject="ladybug", relation="preys_on", object="cherry-slug", weight=0.2)
		self.assertTrue(added["created"])
		lit = next(r for r in STORE.rows("IPM Relationship") if (r["subject"], r["relation"], r["object"]) == ("ladybug", "harmed_by", "warrior-ii"))
		off = ipm_map._save(relationship=lit["name"], enabled=0)
		self.assertTrue(off["created"], "the farm's copy, disabled")
		self.assertEqual(STORE.get_raw("IPM Relationship", lit["name"])["enabled"], 1)
		pair = [e for e in ipm_map._view(crop="Cherries", depth=3)["edges"]
		        if (e["subject"], e["relation"], e["object"]) == ("ladybug", "harmed_by", "warrior-ii")]
		self.assertEqual(pair, [], "the disabled farm copy hides the literature edge")

	def test_the_relation_ends_are_still_checked(self):
		self.as_roles("Farm Manager")
		with self.assertRaisesRegex(Exception, "must be"):
			ipm_map._save(subject="sweet-cherry", relation="preys_on", object="vole")


class Shipped(DeskCase):
	def test_page_files_and_no_cdn(self):
		for name in ("ipm_map.json", "ipm_map.html", "ipm_map.js"):
			self.assertTrue((PAGE / name).exists(), name)
		script = (PAGE / "ipm_map.js").read_text()
		self.assertIn("/assets/erpnext_mcp/vendor/cytoscape/cytoscape.min.js", script)
		for cdn in ("cdnjs", "jsdelivr", "unpkg", "http://", "https://"):
			self.assertNotIn(cdn, script.replace("http://www.w3.org/2000/svg", ""))
		vendor = REPO / "erpnext_mcp" / "public" / "vendor" / "cytoscape"
		self.assertTrue((vendor / "cytoscape.min.js").stat().st_size > 100_000)
		self.assertIn("Permission is hereby granted", (vendor / "LICENSE").read_text())
		html = (PAGE / "ipm_map.html").read_text()
		self.assertNotIn("'", html.split("-->", 1)[1], "a straight apostrophe breaks the compiled template")

	def test_reachable_from_the_workspace_and_both_forms(self):
		spec = json.loads((REPO / "erpnext_mcp" / "workspace_specs" / "crop_protection.json").read_text())
		self.assertIn("ipm-map", [s["link_to"] for s in spec["shortcuts"]])
		from erpnext_mcp import hooks

		self.assertIn("public/js/ipm_map_link.js", hooks.doctype_js["IPM Organism"])
		self.assertIn("public/js/ipm_map_link.js", hooks.doctype_js["IPM Relationship"])
