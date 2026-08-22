# SPDX-License-Identifier: MIT
"""What was carried out of the Flask app that could not be re-derived.

These two modules are the salvage half of the farm_app retirement. Everything
else that came across — the models, the endpoints, the utilities — has a
successor that a reader could reconstruct from the schema. These do not: the
prompts are two years of somebody discovering, one bad answer at a time, what a
model has to be told before it cites an MRL instead of inventing one, and the
reference tables are assembled out of UC IPM, the PNW handbooks, the IRAC and
FRAC code lists and the Koppert side-effects database.

1. **EVERY TEMPLATE RENDERS WITHOUT A KeyError.** `EveryTemplateRenders`. The
   placeholder list is DERIVED from the template text rather than maintained
   beside it, so it cannot go stale; this asserts the derivation actually holds
   for all of them, including the ones whose system prompt is mostly JSON.

2. **THE JSON IN A PROMPT SURVIVES `format`.** `EveryTemplateRenders`. Braces
   that are part of the demanded output are doubled in the template. A regression
   that un-doubles one shows up as `{{` in the rendered text, and this file is
   what sees it.

3. **A MISSING PLACEHOLDER IS REPORTED, NOT RAISED.** `TheMissingHalfIsNamed`.
   Several of these have genuinely optional context, and a caller forced to pass
   six empty strings will eventually pass five.

4. **THE REFERENCE BOOK IS INTACT.** `TheReferenceBookIsIntact`. Row counts and
   spot-checked figures, so a mangled port fails here rather than in a spray
   recommendation.

5. **NOTHING IN EITHER MODULE CALLS ANYTHING.** `NeitherModuleCallsOut`. There is
   no provider, no key and no HTTP: an MCP server is already on the other end of
   a model, so a tool that renders one of these and hands it back is the whole
   integration.
"""

import unittest

from erpnext_mcp import ipm_reference
from erpnext_mcp.erpnext_mcp import prompt_templates


class EveryTemplateRenders(unittest.TestCase):
	def test_the_expected_templates_are_all_present(self):
		self.assertIn("mrl_research_single", prompt_templates.PROMPTS)
		self.assertIn("mrl_research_batch", prompt_templates.PROMPTS)
		self.assertIn("pest_emergence_model", prompt_templates.PROMPTS)
		self.assertIn("pesticide_ipm_profile", prompt_templates.PROMPTS)
		self.assertIn("ipm_recommendation", prompt_templates.PROMPTS)
		self.assertIn("strategic_plan_draft", prompt_templates.PROMPTS)
		self.assertGreaterEqual(len(prompt_templates.PROMPTS), 13)

	def test_every_template_carries_its_metadata(self):
		for key in prompt_templates.names():
			with self.subTest(template=key):
				template = prompt_templates.PROMPTS[key]
				self.assertTrue(template["description"])
				self.assertTrue(template["system"].strip())
				self.assertTrue(template["user"].strip())
				self.assertTrue(template["returns"])
				# The provenance. A prompt whose origin is lost cannot be checked
				# against the thing it was tuned on.
				self.assertTrue(template["source"])

	def test_every_template_renders_with_every_placeholder_filled(self):
		for key in prompt_templates.names():
			with self.subTest(template=key):
				values = {field: f"<{field}>" for field in prompt_templates.PLACEHOLDERS[key]}
				rendered = prompt_templates.render(key, **values)
				self.assertEqual(rendered["missing"], [])
				for part in ("system", "user"):
					self.assertTrue(rendered[part].strip())

	def test_every_template_renders_with_nothing_filled(self):
		"""A caller that passes nothing gets empty context, never an exception."""
		for key in prompt_templates.names():
			with self.subTest(template=key):
				rendered = prompt_templates.render(key)
				self.assertTrue(rendered["system"].strip())

	def test_no_doubled_opening_brace_survives_into_the_rendered_text(self):
		"""The JSON schemas inside these prompts have to come out as JSON.

		THE CHECK IS ON `{{` AND DELIBERATELY NOT ON `}}`, which looks like an
		oversight and is not. A closing `}}` is ORDINARY in correct output — it is
		what the end of a nested object looks like, as in
		`{"emergence_logic": {"model": "degree_day"}}`. A doubled OPENING brace has
		no such innocent form: `{"a": {"b": 1}}` opens with `{"` twice and never
		with `{{`, so a `{{` in rendered text can only mean a template escaped one
		level too far and the prompt now demands malformed JSON.
		"""
		for key in prompt_templates.names():
			with self.subTest(template=key):
				rendered = prompt_templates.render(key)
				self.assertNotIn("{{", rendered["system"])
				self.assertNotIn("{{", rendered["user"])

	def test_the_braces_in_every_rendered_prompt_balance(self):
		"""The other half of the escaping question, and the one `{{` cannot see.

		An under-escaped template loses braces to `format` rather than doubling
		them, and the symptom is a JSON schema with more closing braces than
		opening ones — which reads as fine until a model tries to follow it.
		"""
		for key in prompt_templates.names():
			with self.subTest(template=key):
				rendered = prompt_templates.render(key)
				for part in ("system", "user"):
					text = rendered[part]
					self.assertEqual(text.count("{"), text.count("}"), f"{key}.{part} has unbalanced braces")

	def test_an_unknown_template_names_the_ones_that_exist(self):
		with self.assertRaises(KeyError) as caught:
			prompt_templates.render("no_such_prompt")
		self.assertIn("mrl_research_single", str(caught.exception))

	def test_describe_returns_the_placeholders_without_rendering(self):
		described = prompt_templates.describe("pest_emergence_model")
		self.assertEqual(described["name"], "pest_emergence_model")
		self.assertIn("pest", described["placeholders"])
		self.assertNotIn("system", described)


class TheMRLPromptKeptItsLadder(unittest.TestCase):
	"""The single most-worked prompt in the farm_app, and why it is shaped so."""

	def setUp(self):
		self.rendered = prompt_templates.render(
			"mrl_research_single",
			active_ingredient="spinetoram",
			crop="Sweet Cherry",
			market="Japan",
		)

	def test_the_four_tier_source_ladder_survived(self):
		system = self.rendered["system"]
		for tier in ("TIER 1", "TIER 2", "TIER 3", "TIER 4"):
			self.assertIn(tier, system)

	def test_the_regulators_are_named_rather_than_gestured_at(self):
		system = self.rendered["system"]
		for regulator in ("Codex Alimentarius", "40 CFR Part 180", "APVMA", "GB 2763", "FSSAI"):
			self.assertIn(regulator, system)

	def test_not_found_is_told_to_be_a_last_resort(self):
		"""The behaviour the ladder exists to produce."""
		self.assertIn("LAST RESORT", self.rendered["system"])

	def test_the_response_fields_match_the_mrl_record_columns(self):
		system = self.rendered["system"]
		for column in (
			"mrl_value",
			"source_tier",
			"confidence",
			"is_default_mrl",
			"crop_group_match",
			"substance_status",
		):
			self.assertIn(column, system)

	def test_the_arguments_reach_the_user_prompt(self):
		self.assertIn("spinetoram", self.rendered["user"])
		self.assertIn("Sweet Cherry", self.rendered["user"])
		self.assertIn("Japan", self.rendered["user"])


class TheMissingHalfIsNamed(unittest.TestCase):
	def test_an_omitted_placeholder_is_reported_rather_than_raised(self):
		rendered = prompt_templates.render(
			"mrl_research_single", active_ingredient="spinetoram", crop="Sweet Cherry"
		)
		self.assertIn("market", rendered["missing"])
		self.assertIn("market_context", rendered["missing"])

	def test_a_blank_string_counts_as_missing(self):
		rendered = prompt_templates.render("pest_emergence_model", pest="Codling Moth", scientific_name="   ")
		self.assertEqual(rendered["missing"], ["scientific_name"])

	def test_nothing_missing_is_an_empty_list_and_not_a_null(self):
		rendered = prompt_templates.render(
			"pest_emergence_model", pest="Codling Moth", scientific_name="Cydia pomonella"
		)
		self.assertEqual(rendered["missing"], [])


class TheIPMPromptsCopyNamesExactly(unittest.TestCase):
	"""The recurring device in the pest family, and it is not decoration.

	Every one of these writes into a table keyed by an existing pest, beneficial
	or product name. A model that returns "codling moths" for "Codling Moth"
	produces a row that silently attaches to nothing.
	"""

	def test_the_beneficial_prompt_demands_exact_copying(self):
		rendered = prompt_templates.render(
			"beneficial_organism_profile",
			pest_list='  1. "Codling Moth"',
			beneficial="Trichogramma platneri",
			scientific_name="Trichogramma platneri",
		)
		self.assertIn("EXACTLY", rendered["system"])
		self.assertIn('"Codling Moth"', rendered["system"])

	def test_the_pesticide_prompt_asks_for_both_halves(self):
		rendered = prompt_templates.render(
			"pesticide_ipm_profile",
			pest_list='  1. "Codling Moth"',
			beneficial_list='  1. "Ladybug"',
			product="Delegate 25WG",
			active_ingredient="spinetoram",
			epa_reg_number="62719-541",
			crop="cherry",
		)
		self.assertIn("target_pests", rendered["system"])
		self.assertIn("beneficial_toxicity", rendered["system"])
		self.assertIn("IRAC or FRAC", rendered["system"])

	def test_the_recommendation_prompt_refuses_to_manufacture_a_spray(self):
		rendered = prompt_templates.render(
			"ipm_recommendation",
			threat="Spotted Wing Drosophila",
			crop="Sweet Cherry",
			crop_stage="81",
			count_observed="3",
			sample_unit="trap",
			sample_size="10",
			action_threshold="5 per trap",
			beneficials="none",
			recent_applications="none",
			days_to_harvest="12",
		)
		self.assertIn("BELOW the stated action threshold", rendered["system"])
		self.assertIn("Do not manufacture a reason to spray", rendered["system"])
		self.assertIn("never the only option listed", rendered["system"])


class TheReferenceBookIsIntact(unittest.TestCase):
	def test_every_table_carries_rows(self):
		for name, rows in ipm_reference.TABLES.items():
			with self.subTest(table=name):
				self.assertTrue(rows, f"{name} is empty")

	def test_the_row_counts_are_what_came_across(self):
		summary = ipm_reference.summary()
		self.assertEqual(summary["pest_models"], 28)
		self.assertEqual(summary["beneficials"], 19)
		self.assertEqual(summary["pest_damage"], 8)
		self.assertEqual(summary["beneficial_activity"], 10)
		self.assertEqual(summary["pesticide_efficacy"], 24)
		self.assertEqual(summary["beneficial_toxicity"], 80)
		self.assertEqual(summary["pesticide_products"], 46)
		self.assertEqual(summary["pesticide_labels"], 190)

	def test_a_spot_checked_degree_day_model_survived_the_port(self):
		model = ipm_reference.pest_model("Western Cherry Fruit Fly")["emergence_logic"]
		self.assertEqual(model["base_temp_c"], 5.0)
		self.assertEqual(model["first_flight_dd"], 950)

	def test_a_spot_checked_label_survived_the_port(self):
		label = ipm_reference.labels_for("66222-58", "Cherries")[0]
		self.assertEqual(label["phi_days"], 0)
		self.assertEqual(label["rei_hours"], 24)
		self.assertEqual(label["max_rate"], 6.25)
		self.assertEqual(label["ipm_rate"], 5.0)

	def test_every_efficacy_row_names_its_mode_of_action_group(self):
		"""`moa_group` is what a resistance rotation is built on."""
		for row in ipm_reference.PESTICIDE_EFFICACY:
			with self.subTest(product=row["product"], pest=row["pest"]):
				self.assertTrue(str(row["moa_group"]).strip())

	def test_every_toxicity_row_cites_where_it_came_from(self):
		for row in ipm_reference.BENEFICIAL_TOXICITY:
			with self.subTest(product=row["product"], beneficial=row["beneficial"]):
				self.assertTrue(str(row["data_source"]).strip())

	def test_a_rotation_never_offers_the_same_chemistry(self):
		partners = ipm_reference.rotation_partners("Warrior II", "Spotted Wing Drosophila")
		self.assertTrue(partners)
		self.assertNotIn("3A", [row["moa_group"] for row in partners])

	def test_a_product_with_no_recorded_group_gets_no_rotation(self):
		"""A rotation cannot be checked against an unknown, so it says nothing."""
		self.assertEqual(ipm_reference.rotation_partners("Not A Product", "Codling Moth"), [])

	def test_a_miss_returns_empty_rather_than_a_near_match(self):
		self.assertEqual(ipm_reference.pest_model("Codling Moths"), {})
		self.assertEqual(ipm_reference.beneficial("Ladybugs"), {})

	def test_matching_tolerates_case_and_whitespace_only(self):
		self.assertTrue(ipm_reference.pest_model("  spotted   wing drosophila "))

	def test_the_sources_are_named(self):
		self.assertTrue(any("UC IPM" in source for source in ipm_reference.SOURCES))
		self.assertTrue(any("IRAC" in source for source in ipm_reference.SOURCES))
		self.assertTrue(any("Koppert" in source for source in ipm_reference.SOURCES))

	def test_the_caveat_says_the_label_governs(self):
		self.assertIn("label in the applicator's hand governs", ipm_reference.LABEL_CAVEAT)


class NeitherModuleCallsOut(unittest.TestCase):
	"""No provider, no key, no HTTP. The farm_app's `ai_call.py` is not ported."""

	def _source(self, module):
		import inspect

		return inspect.getsource(module)

	def test_the_prompt_module_imports_no_transport(self):
		source = self._source(prompt_templates)
		for forbidden in ("import requests", "urllib", "api_key", "http://", "https://api"):
			self.assertNotIn(forbidden, source)

	def test_the_reference_module_imports_no_transport(self):
		source = self._source(ipm_reference)
		for forbidden in ("import requests", "urllib", "api_key"):
			self.assertNotIn(forbidden, source)

	def test_the_reference_module_touches_no_doctype(self):
		"""It is literature. It works on a bench with nothing installed."""
		source = self._source(ipm_reference)
		self.assertNotIn("frappe", source)
