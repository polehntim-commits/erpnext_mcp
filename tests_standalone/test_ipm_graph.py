# SPDX-License-Identifier: MIT
"""The IPM relationship graph. v0.262.0 (Tim, 2026-10-06). Contract: docs/contracts/ipm_graph_v0_262.yaml.

WHAT IS UNDER TEST, CLASS BY CLASS:

1. `TheSeed` — the literature becomes nodes and edges; a re-seed never touches the farm's rows; the
   cherry starter thresholds land disabled and Proposed, every one with recommended methods.
2. `TheGraph` — a crop's neighbourhood at a stage, active flags, paging with no cap, the farm's copy of a
   literature edge standing in for it.
3. `LowestImpactFirst` — options rank what harms nothing working now first; MBTA species get non-lethal
   options only; a rodenticide carries the rodent-bait gate.
4. `ThePhone` — anyone enrolled views and logs; only a manager / compliance role edits; a resend is the
   same save; an observation comes back with the threshold status.
5. `ImportExportPropose` — dry run writes nothing and shows the diff; proposals land Proposed and off
   until a person approves them.
"""

import frappe

from erpnext_mcp import ipm_graph, ipm_seed_data
from erpnext_mcp.api import mobile as mobile_api

from .fixtures import MAIN, V12TestCase
from .harness import STORE, set_roles
from .test_api_mobile import WORKER, MobileAPITestCase

BLOCK = "Block 7 Bing"


def rel(subject, relation, obj):
	return [r for r in STORE.rows("IPM Relationship")
	        if (r["subject"], r["relation"], r["object"]) == (subject, relation, obj)]


class SeedCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.report = ipm_graph.seed()
		STORE.commit()  # a migrate commits the seed; a refused tool call rolls back only its own writes


class TheSeed(SeedCase):
	def test_the_literature_becomes_nodes_and_edges(self):
		self.assertFalse(self.report["errors"], self.report["errors"])
		kinds = {r["kind"] for r in STORE.rows("IPM Organism")}
		for kind in ("Crop", "Insect Pest", "Mite Pest", "Disease", "Weed", "Vertebrate Pest", "Beneficial Insect",
		             "Beneficial Mite", "Beneficial Microbe", "Beneficial Vertebrate", "Pollinator", "Product"):
			self.assertIn(kind, kinds)
		self.assertTrue(rel("predatory-mite", "preys_on", "spider-mites"))
		self.assertTrue(rel("ladybug", "harmed_by", "warrior-ii"))
		self.assertTrue(rel("spotted-wing-drosophila", "attacks", "sweet-cherry"))
		self.assertTrue(all(r["provenance"] == "Literature" for r in STORE.rows("IPM Relationship")))

	def test_vertebrates_carry_their_exact_legal_status(self):
		status = {r["name"]: r.get("protected_status") or "" for r in STORE.rows("IPM Organism")}
		self.assertEqual(status["european-starling"], "")
		self.assertEqual(status["american-robin"], "MBTA")
		self.assertEqual(status["cedar-waxwing"], "MBTA")
		self.assertEqual(status["american-crow"], "MBTA Depredation Order")
		self.assertEqual(status["barn-owl"], "MBTA")
		self.assertEqual(status["mule-deer"], "State Protected")

	def test_a_reseed_never_touches_the_farms_rows_and_keeps_a_disabled_literature_row_off(self):
		mine = STORE.get_raw("IPM Organism", "vole")
		mine["provenance"], mine["description"] = "User Entered", "Our voles"
		lit = rel("predatory-mite", "preys_on", "spider-mites")[0]
		lit["enabled"] = 0
		again = ipm_graph.seed()
		self.assertEqual(again["created"], 0)
		self.assertEqual(STORE.get_raw("IPM Organism", "vole")["description"], "Our voles")
		self.assertEqual(rel("predatory-mite", "preys_on", "spider-mites")[0]["enabled"], 0)

	def test_the_starter_thresholds_are_off_proposed_and_say_what_to_do(self):
		seeded = [r for r in STORE.rows("Pest Action Threshold") if r.get("seed_key")]
		self.assertEqual(len(seeded), len(ipm_seed_data.THRESHOLDS))
		for row in seeded:
			self.assertEqual((row["disabled"], row["status"]), (1, "Proposed"), row["threat"])
			self.assertTrue(row["recommended_methods"])
		self.assertEqual(ipm_graph.thresholds_without_methods("Cherries"), [])
		self.assertTrue(any(r["threat"] == "Vole" for r in seeded))


class TheGraph(SeedCase):
	def test_a_crop_at_a_stage(self):
		g = ipm_graph.graph(crop="Cherries", stage=85)
		self.assertEqual(g["crop"]["id"], "sweet-cherry")
		ids = {n["id"] for n in g["nodes"]}
		self.assertIn("spotted-wing-drosophila", ids)
		self.assertIn("american-robin", ids)
		swd = next(n for n in g["nodes"] if n["id"] == "spotted-wing-drosophila")
		self.assertTrue(swd["active_now"])
		attack = next(e for e in g["edges"] if e["subject"] == "spotted-wing-drosophila" and e["relation"] == "attacks")
		self.assertTrue(attack["active_now"])
		self.assertEqual(g["stage"], {"bbch": 85, "source": "given", "label": "BBCH 85"})

	def test_edges_page_with_no_cap(self):
		first = ipm_graph.graph(crop="sweet-cherry", depth=3, limit=10)
		self.assertEqual(len(first["edges"]), 10)
		self.assertEqual(first["next_start"], 10)
		seen, start = [], 0
		while start is not None:
			page = ipm_graph.graph(crop="sweet-cherry", depth=3, start=start, limit=25)
			seen += [e["id"] for e in page["edges"]]
			start = page["next_start"]
		self.assertEqual(len(seen), first["total_edges"])
		self.assertEqual(len(set(seen)), len(seen))

	def test_the_farms_copy_of_a_literature_edge_stands_in_for_it(self):
		lit = rel("ladybug", "harmed_by", "warrior-ii")[0]
		edge, created = ipm_graph.save_relationship({"weight": 0.5, "notes": "our trial"}, "tim@example.com",
		                                            relationship=lit["name"])
		self.assertTrue(created)
		self.assertEqual(STORE.get_raw("IPM Relationship", lit["name"])["weight"], lit["weight"], "literature untouched")
		g = ipm_graph.graph(crop="sweet-cherry", depth=3, limit=2000)
		pair = [e for e in g["edges"] if e["subject"] == "ladybug" and e["relation"] == "harmed_by" and e["object"] == "warrior-ii"]
		self.assertEqual([e["provenance"] for e in pair], ["User Entered"])

	def test_an_unknown_crop_is_named(self):
		with self.assertRaisesRegex(ValueError, "no crop 'Kiwi'"):
			ipm_graph.graph(crop="Kiwi")


class LowestImpactFirst(SeedCase):
	def test_products_that_harm_nothing_working_now_come_first(self):
		options, note = ipm_graph.options_for("spider-mites", "sweet-cherry", 75)
		self.assertIsNone(note)
		kinds = [o["kind"] for o in options]
		self.assertEqual(kinds[0], "biological")
		impacts = [o["impact"] for o in options if o["kind"] == "product"]
		self.assertEqual(impacts, sorted(impacts))

	def test_an_mbta_bird_gets_non_lethal_options_only(self):
		options, note = ipm_graph.options_for("american-robin", "sweet-cherry", 85)
		self.assertIn("MBTA", note)
		self.assertTrue(options)
		self.assertNotIn("product", {o["kind"] for o in options})
		self.assertIn("exclusion", {o["kind"] for o in options})

	def test_a_rodenticide_carries_the_rodent_bait_gate(self):
		STORE.seed("IPM Organism", [{"name": "zp-bait", "organism_key": "zp-bait", "organism_name": "ZP Bait",
		                             "kind": "Product", "ccf_gate": "rodent_bait", "enabled": 1, "status": "Active",
		                             "provenance": "User Entered"}])
		ipm_graph.save_relationship({"subject": "zp-bait", "relation": "controls", "object": "vole", "weight": 0.8},
		                            "tim@example.com")
		options, _ = ipm_graph.options_for("vole", "sweet-cherry", None)
		bait = next(o for o in options if o["product"] == "zp-bait")
		self.assertEqual(bait["ccf_gate"], "rodent_bait")
		self.assertEqual(options[0]["impact"], 0.0)
		self.assertNotEqual(options[0]["kind"], "product", "non-lethal options lead")

	def test_status_reports_a_proposed_threshold_as_not_approved(self):
		status = ipm_graph.threshold_status("spider-mites", crop="Cherries", count=8)
		self.assertEqual(status["status"], "not_approved")
		row = next(r for r in STORE.rows("Pest Action Threshold") if r.get("threat") == "Spider Mites")
		ipm_graph.decide([row["name"]], "approve", "tim@example.com")
		self.assertEqual(ipm_graph.threshold_status("spider-mites", crop="Cherries", count=8)["status"], "action")
		self.assertEqual(ipm_graph.threshold_status("spider-mites", crop="Cherries", count=3)["status"], "warning")
		self.assertEqual(ipm_graph.threshold_status("spider-mites", crop="Cherries", count=1)["status"], "below")
		self.assertEqual(ipm_graph.threshold_status("spider-mites", crop="Cherries")["status"], "unmeasured")


class ImportExportPropose(SeedCase):
	DATA = {"nodes": [{"organism_name": "Brown Marmorated Stink Bug", "kind": "Insect Pest",
	                   "scientific_name": "Halyomorpha halys"}],
	        "edges": [{"subject": "Brown Marmorated Stink Bug", "relation": "attacks", "object": "Sweet Cherry",
	                   "weight": 0.4}, {"subject": "nobody", "relation": "attacks", "object": "Sweet Cherry"}]}

	def test_a_dry_run_writes_nothing_and_shows_the_diff(self):
		before = (len(STORE.rows("IPM Organism")), len(STORE.rows("IPM Relationship")))
		out = ipm_graph.import_graph(self.DATA, dry_run=True)
		self.assertEqual(out["counts"], {"add": 2, "change": 0, "skip": 0, "error": 1})
		self.assertEqual((len(STORE.rows("IPM Organism")), len(STORE.rows("IPM Relationship"))), before)

	def test_an_import_lands_proposed_and_off_then_a_person_approves(self):
		out = ipm_graph.import_graph(self.DATA, dry_run=False)
		edge = next(e for e in out["diff"]["add"] if e["record"] == "edge")
		row = STORE.get_raw("IPM Relationship", edge["id"])
		self.assertEqual((row["status"], row["enabled"], row["provenance"]), ("Proposed", 0, "Imported"))
		ipm_graph.decide([edge["id"], "brown-marmorated-stink-bug"], "approve", "tim@example.com")
		self.assertEqual((STORE.get_raw("IPM Relationship", edge["id"])["status"]), "Active")

	def test_csv_round_trip(self):
		out = ipm_graph.export_graph("csv", include_literature=False)
		self.assertIn("record,organism_name", out["csv"])
		csv_in = "record,organism_name,kind,subject,relation,object\nnode,Gall Midge,Insect Pest,,,\nedge,,,Gall Midge,attacks,Sweet Cherry\n"
		self.assertEqual(ipm_graph.import_graph(csv_in, fmt="csv", dry_run=True)["counts"]["add"], 2)

	def test_an_ai_proposal_is_off_until_approved(self):
		made = ipm_graph.propose({"edges": [{"subject": "Green Lacewing", "relation": "preys_on", "object": "Cherry Slug"}],
		                          "thresholds": [{"crop": "Cherries", "threat": "Cherry Slug", "sample_unit": "Per Leaf",
		                                          "comparison": "Greater Than", "action_threshold": 2,
		                                          "recommended_methods": "Spinosad or kaolin when slugs exceed 2 per leaf"}]},
		                         "PNW Handbook, cherry slug", "agent", "tim@example.com")
		self.assertFalse(made["errors"])
		row = STORE.get_raw("IPM Relationship", made["edges"][0])
		self.assertEqual((row["provenance"], row["status"], row["enabled"]), ("AI Proposed", "Proposed", 0))
		th = STORE.get_raw("Pest Action Threshold", made["thresholds"][0])
		self.assertEqual((th["disabled"], th["status"]), (1, "Proposed"))
		bad = ipm_graph.propose({"thresholds": [{"crop": "Cherries", "threat": "Cherry Slug"}]}, "x", "agent", "u")
		self.assertIn("recommended_methods", bad["errors"][0]["why"])


class ThePhone(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		ipm_graph.seed()
		STORE.seed("Field", [{"name": BLOCK, "field_name": BLOCK, "crop": "Cherries", "owning_entity": MAIN}])
		STORE.seed("Crop Observation", [{"name": "OBS-STAGE", "block_doctype": "Field", "block": BLOCK,
		                                 "observation_type": "Growth Stage", "growth_stage_code": "85",
		                                 "observed_on": "2026-07-20", "company": MAIN}])

	def as_roles(self, *roles):
		set_roles(WORKER, ["Field Worker", *roles])
		self.be()

	def test_a_worker_views_the_blocks_graph_but_cannot_edit(self):
		self.as_roles()
		g = mobile_api.get_ipm_graph(block=BLOCK)
		self.assertEqual(g["crop"]["id"], "sweet-cherry")
		self.assertEqual(g["stage"]["bbch"], 85)
		self.assertEqual(g["stage"]["source"], "observed")
		self.assertFalse(g["can_edit"])
		with self.assertRaisesRegex(frappe.PermissionError, "is restricted to"):
			mobile_api.save_ipm_relationship(subject="ladybug", relation="preys_on", object="cherry-slug")

	def test_a_manager_adds_one_and_a_resend_is_the_same_save(self):
		self.as_roles("Farm Manager")
		first = mobile_api.save_ipm_relationship(subject="ladybug", relation="preys_on", object="cherry-slug",
		                                         weight=0.2, client_request_id="ipm-1")
		self.assertTrue(first["created"])
		self.assertEqual(first["edge"]["provenance"], "User Entered")
		again = mobile_api.save_ipm_relationship(subject="ladybug", relation="preys_on", object="cherry-slug",
		                                         weight=0.2, client_request_id="ipm-1")
		self.assertTrue(again["replayed"])
		self.assertEqual(len(rel("ladybug", "preys_on", "cherry-slug")), 1)

	def test_a_foreman_cannot_edit(self):
		self.as_roles("Foreman")
		with self.assertRaisesRegex(frappe.PermissionError, "is restricted to"):
			mobile_api.save_ipm_relationship(subject="ladybug", relation="preys_on", object="cherry-slug")

	def test_logging_a_pest_answers_with_the_threshold_and_the_options(self):
		self.as_roles()
		out = mobile_api.record_pest_observation(block=BLOCK, organism="spotted-wing-drosophila", count=3,
		                                         sample_unit="Per Trap Per Week", client_request_id="obs-1")
		obs = STORE.get_raw("Crop Observation", out["observation"])
		self.assertEqual(obs["threat"], "Spotted Wing Drosophila")
		self.assertEqual(obs["threat_category"], "Insect")
		self.assertEqual(out["threshold_status"]["status"], "not_approved")
		self.assertTrue(out["threshold_status"]["options"])

	def test_a_beneficial_is_not_logged_as_a_pest(self):
		self.as_roles()
		with self.assertRaisesRegex(frappe.ValidationError, "not a pest"):
			mobile_api.record_pest_observation(block=BLOCK, organism="ladybug", count=3)

	def test_an_organism_detail(self):
		self.as_roles()
		detail = mobile_api.get_ipm_organism(organism="american-robin")
		self.assertEqual(detail["node"]["protected_status"], "MBTA")
		self.assertTrue(detail["edges"])
		self.assertTrue(detail["thresholds"])


class TheTools(SeedCase):
	ON = {f"allow_{t}": 1 for t in ("list_ipm_organisms", "get_ipm_organism", "list_ipm_relationships", "get_ipm_graph",
	                                "export_ipm_graph", "create_ipm_organism", "update_ipm_organism",
	                                "create_ipm_relationship", "update_ipm_relationship", "import_ipm_graph",
	                                "propose_ipm_relationships", "approve_ipm_proposal", "approve_pest_action_threshold",
	                                "list_pest_action_thresholds", "set_pest_action_threshold")}

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **self.ON)

	def test_reads_page(self):
		first = self.tool_data("list_ipm_organisms", {"kind": "Vertebrate Pest", "limit": 3})
		self.assertEqual(len(first["organisms"]), 3)
		self.assertIsNotNone(first["next_start"])
		self.assertTrue(self.tool_data("get_ipm_graph", {"crop": "Cherries", "stage": 75})["edges"])
		self.assertTrue(self.tool_data("list_ipm_relationships", {"organism": "vole"})["relationships"])

	def test_the_writes_are_off_by_default(self):
		self.configure(enabled=1, **{**self.ON, "allow_create_ipm_relationship": 0})
		self.tool_error("create_ipm_relationship", {"subject": "ladybug", "relation": "preys_on", "object": "cherry-slug"})

	def test_a_proposal_must_cite_and_cannot_approve_itself(self):
		self.assertIn("where it was read", self.tool_error("propose_ipm_relationships", {"edges": [
			{"subject": "ladybug", "relation": "preys_on", "object": "cherry-slug"}]}))
		self.assertIn("cannot fill in", self.tool_error("propose_ipm_relationships", {
			"source_citation": "PNW Handbook", "approved_by": "me",
			"edges": [{"subject": "ladybug", "relation": "preys_on", "object": "cherry-slug"}]}))
		made = self.tool_data("propose_ipm_relationships", {"source_url": "https://pnwhandbooks.org/insect/tree-fruit/cherry",
		                                                    "edges": [{"subject": "ladybug", "relation": "preys_on", "object": "cherry-slug"}]})
		self.assertTrue(made["edges"], made)
		edge = made["edges"][0]
		self.tool_data("approve_ipm_proposal", {"names": [edge], "decision": "approve", "note": "seen it"})
		self.assertEqual(STORE.get_raw("IPM Relationship", edge)["status"], "Active")

	def test_the_proposed_thresholds_are_listed_and_approved(self):
		listed = self.tool_data("list_pest_action_thresholds", {"status": "Proposed"})
		self.assertEqual(listed["count"], len(ipm_seed_data.THRESHOLDS))
		self.assertEqual(listed["thresholds_without_recommended_methods"], [])
		one = listed["thresholds"][0]["name"]
		self.tool_data("approve_pest_action_threshold", {"threshold": one})
		self.assertEqual(STORE.get_raw("Pest Action Threshold", one)["disabled"], 0)

	def test_import_dry_run_by_default(self):
		out = self.tool_data("import_ipm_graph", {"data": ImportExportPropose.DATA})
		self.assertTrue(out["dry_run"])
		self.assertFalse(frappe.db.exists("IPM Organism", "brown-marmorated-stink-bug"))
