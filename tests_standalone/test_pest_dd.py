# SPDX-License-Identifier: MIT
"""Pest degree days on the farm's weather, per block. v0.264.0 (Tim, 2026-10-06).

The weather is mocked at `degree_days.daily_temps` — the one door to Open-Meteo — with a constant
70 / 50 °F day, so every date below is arithmetic a reader can check: 60 °F mean, base 41 °F → 19 °F·day.

1. `FromTheFarmsWeather` — accumulated DD from the biofix, events reached and projected ±, never a
   literature date; provenance names the source, the cell, the offset and the citation.
2. `TheBlockOffset` — aspect × slope, capped; a pinned (calibrated) block value wins; reasons stated.
3. `Calibration` — suggests the offset that would have dated an observed event; writes only a DRAFT.
4. `EverywhereElse` — the graph's pest nodes, the threshold status, the CCF provider, the opt-in map
   layer, the phone route and the config kinds.
"""

import datetime
from unittest import mock

from erpnext_mcp import ccf_providers, degree_days, ipm_graph, overlays, pest_dd, phone_config

from .fixtures import MAIN, V12TestCase
from .harness import ROLES, STORE

BLOCK = "Block 7 Bing"
AS_OF = "2026-04-15"


def weather(lat, lon, start, end):
	out, day = {}, start
	while day <= end:
		out[day.isoformat()] = (70.0, 50.0)
		day += datetime.timedelta(days=1)
	return out


def dd_site(case) -> None:
	"""The seeded graph, one block with a centroid, and the weather / today / aspect layer mocked."""
	ipm_graph.seed()
	STORE.commit()
	STORE.seed("Field", [{"name": BLOCK, "field_name": BLOCK, "crop": "Cherries", "owning_entity": MAIN,
	                      "boundary_centroid_lat": 45.6, "boundary_centroid_lon": -121.2}])
	for patcher in (mock.patch.object(degree_days, "daily_temps", side_effect=weather),
	                mock.patch.object(ccf_providers, "forecast_enabled", return_value=True),
	                mock.patch.object(pest_dd, "_aspect_summary", return_value=None),
	                mock.patch.object(pest_dd.frappe.utils, "today", return_value=AS_OF),
	                # The H3 cell id needs the h3 package, which one venv lacks ("lat,lon" there): pinned, so the
	                # contract fixture is the same on every interpreter.
	                mock.patch.object(ccf_providers, "_cell", return_value="8728f66e4ffffff")):
		patcher.start()
		case.addCleanup(patcher.stop)


class DDCase(V12TestCase):
	def setUp(self):
		super().setUp()
		dd_site(self)


class FromTheFarmsWeather(DDCase):
	def test_fruit_fly_accumulates_from_its_biofix_and_projects_emergence(self):
		st = pest_dd.status(BLOCK, "western-cherry-fruit-fly", AS_OF)
		self.assertTrue(st["available"])
		self.assertEqual(st["biofix_date"], "2026-03-01")
		self.assertEqual(st["dd_to_date"], 46 * 19.0)  # 1 Mar – 15 Apr inclusive
		first = st["events"][0]
		self.assertEqual((first["name"], first["status"], first["date"]), ("first emergence", "projected", "2026-04-19"))
		self.assertIn("expected ~Apr 19", first["text"])
		self.assertGreaterEqual(first["plus_minus_days"], 1)
		self.assertFalse(st["window_open"])
		self.assertIn("Open-Meteo", st["weather"]["source"])
		self.assertTrue(st["model"]["verify"])
		self.assertIn("citation", st["model"])

	def test_a_reached_event_is_dated_from_the_record_and_opens_the_window(self):
		st = pest_dd.status(BLOCK, "spotted-wing-drosophila", AS_OF)  # base 45: 15 °F·day/day from 1 Jan
		self.assertEqual(st["events"][0]["status"], "reached")
		self.assertEqual(st["events"][0]["date"], "2026-01-10")  # 150 / 15 = day 10
		self.assertTrue(st["window_open"])

	def test_first_catch_biofix_waits_for_a_logged_catch(self):
		st = pest_dd.status(BLOCK, "codling-moth", AS_OF)
		self.assertIsNone(st["biofix_date"])
		self.assertIn("waiting for the first trap catch", st["summary"])
		STORE.seed("Crop Observation", [{"name": "CM1", "block": BLOCK, "block_doctype": "Field", "threat": "Codling Moth",
		                                 "count_observed": 2, "observed_on": "2026-04-01", "company": MAIN}])
		st = pest_dd.status(BLOCK, "codling-moth", AS_OF)
		self.assertEqual(st["biofix_date"], "2026-04-01")
		self.assertEqual(st["dd_to_date"], 15 * 10.0)  # base 50

	def test_a_pest_with_no_model_says_so(self):
		st = pest_dd.status(BLOCK, "powdery-mildew", AS_OF)
		self.assertFalse(st["available"])
		self.assertIn("No degree-day model", st["reason"])


class TheBlockOffset(DDCase):
	def test_a_south_slope_runs_warmer_and_says_why(self):
		with mock.patch.object(pest_dd, "_aspect_summary", return_value={"mean_aspect": "S", "mean_slope_degrees": 15}):
			off = pest_dd.block_offset(BLOCK)
			self.assertEqual(off["offset_f"], 1.5)
			self.assertIn("faces S", off["reasons"][0])
			st = pest_dd.status(BLOCK, "western-cherry-fruit-fly", AS_OF)
		self.assertEqual(st["dd_to_date"], round(46 * 20.5, 1))
		self.assertEqual(st["offset"]["offset_f"], 1.5)

	def test_a_gentle_north_slope_is_scaled_and_capped(self):
		with mock.patch.object(pest_dd, "_aspect_summary", return_value={"mean_aspect": "N", "mean_slope_degrees": 7.5}):
			self.assertEqual(pest_dd.block_offset(BLOCK)["offset_f"], -0.5)

	def test_a_calibrated_block_value_wins(self):
		body = {**pest_dd.DEFAULT_OFFSETS, "blocks": {BLOCK: {"offset_f": 2.2, "reason": "calibration 2027"}}}
		with pest_dd.overlay(pest_dd.OFFSETS_KEY, body):
			off = pest_dd.block_offset(BLOCK)
		self.assertEqual((off["offset_f"], off["source"], off["reasons"]), (2.2, "block", ["calibration 2027"]))

	def test_no_layer_no_offset_and_it_says_so(self):
		off = pest_dd.block_offset(BLOCK)
		self.assertEqual(off["offset_f"], 0.0)
		self.assertIn("build_slope_aspect_layer", off["reasons"][0])


class Calibration(DDCase):
	def test_an_observed_emergence_suggests_the_offset_that_dates_it(self):
		# Emergence seen on 9 Apr: 40 days from 1 Mar → 950/40 = 23.75 °F·day/day → +4.75 °F.
		s = pest_dd.suggest_calibration(BLOCK, "western-cherry-fruit-fly", "2026-04-09", "first emergence")
		self.assertAlmostEqual(s["suggested_offset_f"], 4.75, delta=0.05)
		self.assertEqual(s["current_offset_f"], 0.0)

	def test_calibration_writes_only_a_draft(self):
		out = pest_dd.calibrate(BLOCK, [{"pest": "western-cherry-fruit-fly", "observed_on": "2026-04-17"},
		                                {"pest": "western-cherry-fruit-fly", "observed_on": "2026-04-19"}], "tim", draft=True)
		self.assertIn("draft", out)
		self.assertEqual(out["observations_used"], 2)
		self.assertEqual(out["confidence"], "low")
		rows = phone_config.rows(pest_dd.KIND, pest_dd.OFFSETS_KEY)
		self.assertTrue(rows)
		self.assertFalse([r for r in rows if r["status"] == phone_config.PUBLISHED and int(r.get("version") or 0) > 1])
		self.assertEqual(pest_dd.block_offset(BLOCK)["offset_f"], 0.0, "nothing applied")

	def test_logged_first_catches_are_the_default_observations(self):
		STORE.seed("Crop Observation", [{"name": "F1", "block": BLOCK, "block_doctype": "Field",
		                                 "threat": "Western Cherry Fruit Fly", "count_observed": 1,
		                                 "observed_on": "2026-04-12", "company": MAIN}])
		self.assertEqual(pest_dd.logged_observations(BLOCK),
		                 [{"pest": "western-cherry-fruit-fly", "event": "first emergence", "observed_on": "2026-04-12"}])

	def test_the_models_validate(self):
		self.assertEqual(pest_dd.validate(pest_dd.DEFAULT_MODELS, pest_dd.MODELS_KEY)["errors"], [])
		bad = {"models": {"x": {"base_f": 50, "upper_f": 40, "biofix": "jan1", "events": [{"name": "a", "dd": 5}, {"name": "b", "dd": 4}]}}}
		errors = pest_dd.validate(bad, pest_dd.MODELS_KEY)["errors"]
		self.assertTrue(any("upper_f" in e for e in errors) and any("rise" in e for e in errors))


class EverywhereElse(DDCase):
	def test_the_graph_carries_dd_status_on_pest_nodes_for_a_block(self):
		g = ipm_graph.graph(block=BLOCK, stage=75, with_dd=True, limit=2000)
		wcff = next(n for n in g["nodes"] if n["id"] == "western-cherry-fruit-fly")
		self.assertEqual(wcff["dd_status"]["next_event"]["date"], "2026-04-19")
		self.assertFalse(wcff["active_now"], "the block's DD window decides active_now")
		plain = ipm_graph.graph(block=BLOCK, stage=75, limit=2000)
		self.assertNotIn("dd_status", next(n for n in plain["nodes"] if n["id"] == "western-cherry-fruit-fly"))

	def test_the_threshold_status_reads_the_block_window(self):
		row = next(r for r in STORE.rows("Pest Action Threshold") if r.get("threat") == "Western Cherry Fruit Fly")
		ipm_graph.decide([row["name"]], "approve", "tim")
		st = ipm_graph.threshold_status("western-cherry-fruit-fly", block=BLOCK, count=2)
		self.assertEqual(st["status"], "action")
		self.assertIn("not out yet on this block", st["message"])
		self.assertEqual(st["dd_status"]["next_event"]["date"], "2026-04-19")

	def test_the_ccf_provider(self):
		values = pest_dd.provider_values({"name": BLOCK}, {"doctype": "Field", "as_of_date": AS_OF})
		self.assertEqual(values["western_cherry_fruit_fly_days_to_next"], 4)
		self.assertFalse(values["western_cherry_fruit_fly_window_open"])
		self.assertTrue(values["spotted_wing_drosophila_window_open"])

	def test_the_map_layer_is_opt_in(self):
		ROLES["Administrator"] = ["System Manager"]
		keep, refused = overlays.requested_layers(["pest_dd"], list(overlays.LAYER_KEYS))
		self.assertEqual((keep, refused), (["pest_dd"], []))
		self.assertNotIn("pest_dd", overlays.LAYER_KEYS, "never in a default answer")
		layer = overlays.pest_dd_overlay(BLOCK)
		self.assertEqual(layer["status"], "open")
		self.assertIn("Apr 19", layer["headline"])

	def test_the_go_hold_presets_ship_off(self):
		from erpnext_mcp import go_hold

		ids = {s["rule_id"]: s for s in go_hold.preset_specs()}
		for rid in ("go_hold_spray_fruit_fly_window", "go_hold_spray_swd_window"):
			self.assertEqual(ids[rid]["enabled"], 0)


class TheToolsAndTheConfig(DDCase):
	ON = {"allow_get_pest_dd_status": 1, "allow_calibrate_pest_dd": 1, "allow_list_configs": 1, "allow_get_config": 1,
	      "allow_draft_config": 1, "allow_publish_config": 1, "allow_preview_config": 1}

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **self.ON)
		from erpnext_mcp import install

		install._pest_dd_seed()
		STORE.commit()

	def test_status_over_mcp(self):
		data = self.tool_data("get_pest_dd_status", {"block": BLOCK, "pest": "western-cherry-fruit-fly", "as_of": AS_OF})
		self.assertEqual(data["statuses"][0]["events"][0]["date"], "2026-04-19")
		self.assertTrue(data["models_config"].startswith("FCV") or data["models_config"] != "built-in")

	def test_calibration_over_mcp_reports_and_drafts_but_never_publishes(self):
		out = self.tool_data("calibrate_pest_dd", {"block": BLOCK, "draft": True, "observations": [
			{"pest": "western-cherry-fruit-fly", "observed_on": "2026-04-18"}]})
		self.assertIn("draft", out)
		self.assertIn("never", self.tool_error("publish_config", {"kind": "pest_dd_offsets", "key": "pest_dd_offsets",
		                                                       "change_note": "x"}).lower())

	def test_the_config_kinds(self):
		listed = self.tool_data("list_configs", {"kind": "pest_dd_models"})
		self.assertEqual(listed["kind"], "pest_dd_models")
		got = self.tool_data("get_config", {"kind": "pest_dd_offsets", "key": "pest_dd_offsets"})
		self.assertIn("config", got)
		body = {**pest_dd.DEFAULT_OFFSETS, "blocks": {BLOCK: {"offset_f": 1.0, "reason": "trial"}}}
		prev = self.tool_data("preview_config", {"kind": "pest_dd_offsets", "key": "pest_dd_offsets", "body": body,
		                                         "blocks": [BLOCK]})
		row = next(r for r in prev["preview"]["blocks"] if r["pest"] == "western-cherry-fruit-fly")
		self.assertEqual((row["offset_now"], row["offset_draft"]), (0.0, 1.0))
		self.assertLessEqual(row["next_draft"], row["next_now"])


class HarvestCalibration(DDCase):
	"""v0.264.1. Tim's harvest windows → proposals. Weather: 70 / 50 °F, base 40 → 20 °F·day a day."""

	def setUp(self):
		super().setUp()
		STORE.seed("Field", [{"name": "Gib Fred 40", "field_name": "Gib Fred 40", "crop": "Cherries", "owning_entity": MAIN,
		                      "boundary_centroid_lat": 45.61, "boundary_centroid_lon": -121.21}])
		self.map = {"Mill Creek": BLOCK, "40 Acre": "Gib Fred 40"}

	def test_the_seed_is_tims_file(self):
		data = pest_dd.harvest_windows()
		self.assertEqual(len(data["windows"]), 30)
		chelan = [w for w in data["windows"] if w["variety"] == "Chelan" and w["year"] == 2026][0]
		self.assertEqual((chelan["harvest_start"], chelan["harvest_end"]), ("2026-06-05", "2026-06-07"))

	def test_variety_models_are_fitted_and_offsets_are_relative(self):
		out = pest_dd.propose_harvest_calibration(self.map)
		models = out["proposed_models"]
		self.assertEqual(len(models), 9)
		chelan = models["sweet-cherry-chelan"]
		# 1 Mar → 5 Jun 2026 = 97 days × 20 = 1940; 2025: 103 d = 2060; 2023: 114 d = 2280 (weight 0.3).
		expected = round((1940 + 2060 + 2280 * 0.3) / 2.3)
		self.assertEqual(chelan["events"][0]["dd"], expected)
		self.assertTrue(chelan["verify"])
		offs = [v["offset_f"] for v in out["proposed_block_offsets"].values()]
		self.assertAlmostEqual(sum(offs), 0.0, places=1)
		self.assertEqual(out["skipped"], [])

	def test_drafts_only(self):
		out = pest_dd.propose_harvest_calibration(self.map, draft=True)
		self.assertIn("models_draft", out)
		self.assertIn("offsets_draft", out)
		self.assertNotIn("sweet-cherry-chelan", pest_dd.models()[0], "nothing published")
		self.assertEqual(pest_dd.block_offset(BLOCK)["offset_f"], 0.0)

	def test_an_unmapped_parcel_is_skipped_and_said(self):
		out = pest_dd.propose_harvest_calibration({"Mill Creek": BLOCK})
		self.assertEqual(len(out["skipped"]), 6)
		self.assertIn("40 Acre", out["skipped"][0]["why"])


class Units(DDCase):
	"""v0.276.1. Every pest model is °F base and °F·day totals — the same basis as get_degree_days."""

	def test_the_engine_sums_fahrenheit_days(self):
		# 70 / 50 °F at base 41 °F is 19 °F·day a day; summed as °C·day at base 5 °C it would be 10.6, and the
		# fruit fly's 950 would land five weeks later. The emergence date is the °F one.
		st = pest_dd.status(BLOCK, "western-cherry-fruit-fly", AS_OF)
		self.assertEqual(st["dd_to_date"], 46 * 19.0)
		self.assertEqual(st["events"][0]["date"], "2026-04-19")

	def test_every_reference_model_states_its_units_and_matches_the_engine(self):
		from erpnext_mcp import ipm_reference

		engine = pest_dd.DEFAULT_MODELS["models"]
		for pest in ipm_reference.PEST_MODELS:
			logic = pest.get("emergence_logic") or {}
			if logic.get("model") != "degree_day":
				continue
			self.assertEqual(logic["dd_unit"], "°F·day", pest["name"])
			self.assertAlmostEqual(logic["base_f"], round(logic["base_temp_c"] * 1.8 + 32), delta=0.5, msg=pest["name"])
			key = pest["name"].lower().replace(" ", "-")
			if key in engine:
				self.assertEqual(engine[key]["base_f"], logic["base_f"], key)

	def test_a_celsius_model_is_refused(self):
		body = {"key": pest_dd.MODELS_KEY, "models": {
			"x": {"base_f": 5, "biofix": "mar1", "events": [{"name": "e", "dd": 950}], "citation": "c"},
			"y": {"base_f": 41, "dd_unit": "°C·day", "biofix": "mar1", "events": [{"name": "e", "dd": 528}], "citation": "c"}}}
		errors = pest_dd.validate(body)["errors"]
		self.assertTrue(any("x: base_f 5 is not a plausible °F base" in e for e in errors), errors)
		self.assertTrue(any("y: dd_unit" in e for e in errors), errors)
		self.assertEqual(pest_dd.validate(pest_dd.DEFAULT_MODELS)["errors"], [])


class OnlyTheDaysAsked(V12TestCase):
	def test_a_past_window_gets_no_forecast_days(self):
		from erpnext_mcp.services import weather as svc

		def answer(url, params, label):
			days = ["2026-07-30", "2026-07-31", "2026-10-07", "2026-10-08"]
			return {"daily": {"time": days, "temperature_2m_max": [80.0] * 4, "temperature_2m_min": [55.0] * 4}}

		degree_days._CACHE.clear()
		with mock.patch.object(svc, "_get_json", side_effect=answer), \
				mock.patch.object(degree_days, "_today", return_value=datetime.date(2026, 10, 8)), \
				mock.patch.object(ccf_providers, "_cell", return_value="cell"):
			temps = degree_days.daily_temps(45.6, -121.2, datetime.date(2026, 7, 1), datetime.date(2026, 7, 31))
		self.assertEqual(sorted(temps), ["2026-07-30", "2026-07-31"])
