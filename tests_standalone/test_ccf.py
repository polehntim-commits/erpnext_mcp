"""The CCF generic evaluator. v0.233.0. docs/design/ccf_core_work_timing.md §2."""

import unittest
from unittest import mock

import frappe

from erpnext_mcp import ccf, compliance_rules
from erpnext_mcp.alerts import engine

from .test_alerts import TODAY
from .test_compliance_rule_engine import RuleEngineTestCase


class _SeriesProvider:
	"""A stand-in weather-shaped provider, registered for the length of a test."""

	def __enter__(self):
		self.saved = dict(ccf.PROVIDERS)
		ccf.register(
			ccf.Provider(
				"testwx",
				{
					"daily[].rain_in": ccf._p("number", "rain", "in", 0.1),
					"daily[].prob": ccf._p("number", "chance", "%", 40),
					"hours_since_rain": ccf._p("number", "hours", "h", 12),
				},
				lambda subject, ctx: subject.get("_wx") or {},
			)
		)
		return self

	def __exit__(self, *exc):
		ccf.PROVIDERS.clear()
		ccf.PROVIDERS.update(self.saved)


class TheTreeIsCheckedAtTheDoor(unittest.TestCase):
	def refused(self, tree, text):
		with self.assertRaisesRegex(ccf.TreeError, text):
			ccf.parse_tree(tree)

	def test_what_cannot_be_evaluated_is_refused_before_it_is_stored(self):
		self.refused({"path": "nowhere.x", "op": "eq", "value": 1}, "no provider 'nowhere'")
		self.refused({"path": "calendar.nope", "op": "eq", "value": 1}, "does not publish")
		self.refused({"path": "calendar.mmdd", "op": "approx", "value": 1}, "op 'approx'")
		self.refused({"path": "calendar.mmdd", "op": "between", "value": "01-01"}, r"between takes \[low, high\]")
		self.refused({"path": "calendar.mmdd", "op": "gte", "value": "Jan 1"}, "MM-DD")
		self.refused({"path": "calendar.month", "op": "gte", "value": "three"}, "is a number")
		self.refused({"path": "calendar.month", "op": "isnull", "value": 1}, "takes no value")
		self.refused({"all": []}, "non-empty list")
		self.refused({"all": [{"id": "a", "path": "calendar.month", "op": "eq", "value": 1},
		                      {"id": "a", "path": "calendar.month", "op": "eq", "value": 2}]}, "used twice")
		deep = {"path": "calendar.month", "op": "eq", "value": 1}
		for _ in range(ccf.MAX_DEPTH + 1):
			deep = {"not": deep}
		self.refused(deep, "deeper than")
		with _SeriesProvider():
			self.refused({"path": "testwx.daily[0..6].rain_in", "op": "gte", "value": 0.05}, "needs agg")
			self.refused({"path": "testwx.hours_since_rain", "op": "gte", "value": 48, "agg": "max"}, "agg applies only")

	def test_the_evaluation_and_actions_are_checked_too(self):
		with self.assertRaisesRegex(ccf.TreeError, "raise_when"):
			ccf.parse_evaluation({"raise_when": "sometimes"})
		with self.assertRaisesRegex(ccf.TreeError, "when is a list"):
			ccf.parse_evaluation({"when": ["midnight"]})
		with self.assertRaisesRegex(ccf.TreeError, "type is one of"):
			ccf.parse_actions([{"type": "launch_rocket"}])
		self.assertEqual(ccf.parse_actions([{"type": "set_go_hold"}]), [{"type": "set_go_hold"}])


class TheTreeExplainsItself(unittest.TestCase):
	def test_rain_ahead_holds_and_says_which_check_failed(self):
		tree = {
			"all": [
				{"id": "in_season", "path": "calendar.mmdd", "op": "between", "value": ["01-01", "03-15"]},
				{
					"id": "dry_ahead",
					"reason": {"en": "Rain forecast — no pruning", "es": "Pronóstico de lluvia — no se poda"},
					"basis": "published",
					"not": {
						"any": [
							{"path": "testwx.daily[0..6].rain_in", "agg": "max", "op": "gt", "value": 0.05},
							{"path": "testwx.daily[0..6].prob", "agg": "max", "op": "gte", "value": 40},
						]
					},
				},
				{"id": "dry_since", "path": "testwx.hours_since_rain", "op": "gte", "value": 48, "basis": "local_judgment"},
			]
		}
		with _SeriesProvider():
			ccf.parse_tree(tree)
			wet = {"_wx": {"daily": [{"rain_in": 0, "prob": 10}, {"rain_in": 0.2, "prob": 70}], "hours_since_rain": 60}}
			values = ccf.build_context(wet, as_of="2026-01-20")
			result = ccf.evaluate(tree, values)
			self.assertFalse(result["passed"])
			self.assertEqual([f["id"] for f in result["failures"]], ["dry_ahead"])
			self.assertEqual(result["hold"], "Hold: dry_ahead — Rain forecast — no pruning")
			self.assertIn(0.2, [leaf["actual"] for leaf in result["failures"][0]["leaves"]])
			self.assertEqual(ccf.evaluate(tree, values, "es")["hold"], "Espera: dry_ahead — Pronóstico de lluvia — no se poda")

			dry = {"_wx": {"daily": [{"rain_in": 0, "prob": 10}] * 7, "hours_since_rain": 72}}
			self.assertTrue(ccf.evaluate(tree, ccf.build_context(dry, as_of="2026-01-20"))["passed"])
			out_of_season = ccf.evaluate(tree, ccf.build_context(dry, as_of="2026-06-01"))
			self.assertEqual([f["id"] for f in out_of_season["failures"]], ["in_season"])

	def test_missing_data_is_a_hold_that_says_so(self):
		with _SeriesProvider():
			tree = {"id": "dry_since", "path": "testwx.hours_since_rain", "op": "gte", "value": 48}
			result = ccf.evaluate(tree, ccf.build_context({"_wx": {}}, as_of="2026-01-20"))
			self.assertFalse(result["passed"])
			self.assertTrue(result["failures"][0]["missing"])
			self.assertIn("no data", result["hold"])

	def test_value_source_reads_a_farm_setting(self):
		with mock.patch.object(compliance_rules, "threshold_from_source", return_value=10.0):
			tree = {"path": "record.wind", "op": "lte", "value_source": "settings.weather.wind_threshold_mph_spray_block"}
			ccf.parse_tree(tree)
			self.assertTrue(ccf.evaluate(tree, ccf.build_context({"wind": 8}))["passed"])
			self.assertFalse(ccf.evaluate(tree, ccf.build_context({"wind": 12}))["passed"])


class ARuleWithATreeRunsOnTheEvaluator(RuleEngineTestCase):
	def a_tree_rule(self, tree, **overrides):
		spec = {
			"rule_id": "policy_tree",
			"title": "Policy under review",
			"category": "Records",
			"target_doctype": "Compliance Policy",
			"kairotic_gate_description": "A tree over the policy.",
			"regimes": ["Internal"],
			"enabled": 1,
			"condition_tree": tree,
			"human_approved_by": "Administrator",
			"human_approved_on": frappe.utils.now(),
		}
		spec.update(overrides)
		doc = compliance_rules.build_rule(spec)
		doc.insert(ignore_permissions=True)
		return compliance_rules.rule_row(doc.name)

	def test_a_failing_tree_raises_one_observation_per_record_with_the_hold(self):
		self.a_policy(review_in_days=10)
		row = self.a_tree_rule({"id": "far_off", "path": "record.review_due_date", "op": "gt", "value": "2099-01-01",
		                        "reason": "Review is not far off"})
		result = engine.preview(row, {"today": TODAY, "company": ""})
		self.assertEqual(result["observed"], 1)
		self.assertEqual(result["observations"][0]["message"], "Hold: far_off — Review is not far off")

	def test_raise_when_pass_inverts_it(self):
		self.a_policy(review_in_days=10)
		row = self.a_tree_rule({"path": "record.review_due_date", "op": "gt", "value": "2099-01-01"},
		                       evaluation={"raise_when": "pass"})
		self.assertEqual(engine.preview(row, {"today": TODAY, "company": ""})["observed"], 0)

	def test_the_tools_refuse_a_bad_tree_and_nothing_is_written(self):
		before = len(compliance_rules.rule_rows(include_inactive=True))
		error = self.tool_error(
			"create_compliance_rule",
			{
				"rule_id": "bad_tree",
				"title": "Bad tree",
				"target_doctype": "Compliance Policy",
				"kairotic_gate_description": "Never.",
				"condition_tree": {"path": "weather_typo.x", "op": "eq", "value": 1},
			},
		)
		self.assertIn("no provider 'weather_typo'", str(error))
		self.assertEqual(len(compliance_rules.rule_rows(include_inactive=True)), before)

	def test_the_shipped_rules_never_touch_the_evaluator(self):
		compliance_rules.seed_compliance_rules()
		with mock.patch.object(ccf, "scan_for") as scan_for:
			rules, _notes = engine.rule_set()
		self.assertTrue(rules)
		scan_for.assert_not_called()

	def test_the_field_map_lists_every_context_path(self):
		data = self.tool_data("get_compliance_field_map", {})
		paths = {entry["path"] for entry in data["context"]}
		self.assertTrue({"calendar.mmdd", "task.state", "asset.engine_hours", "record.*"} <= paths)
