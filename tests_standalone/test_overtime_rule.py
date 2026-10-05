"""Payroll settings as data: the overtime rule. v0.235.0 (Tim's decisions 9 and 10)."""

from erpnext_mcp import config_lifecycle, payroll_settings, phone_config
from erpnext_mcp.tools import payroll

from .fixtures import MAIN
from .harness import set_roles
from .test_payroll_integration import PERIOD_END, PERIOD_START, WORKER, IntegrationTestCase, member, shift


class TheOvertimeRuleIsData(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		# Forty-five hours in the first week: five hours of overtime.
		rows = []
		for index in range(5):
			day = f"2025-06-{2 + index:02d}"
			rows.append(shift(f"S{index}", day, state="OR", end="15:00:00", crew=[member(WORKER)]))
		self.seed_shifts(*rows)
		self.structure(WORKER, pay_type="Hourly", rate=20.0)
		self.args = {"company": MAIN, "pay_period_start": PERIOD_START, "pay_period_end": PERIOD_END}

	def run_period(self):
		_ctx, slips, _totals = payroll._period_run(dict(self.args), creating=False)
		return {s["employee"]: {k: s.get(k) for k in ("gross_pay", "net_pay", "overtime_hours")} for s in slips}, _ctx

	def test_with_nothing_published_it_is_todays_law(self):
		rule = payroll_settings.overtime()
		self.assertEqual((rule["weekly_threshold_hours"], rule["multiplier"], rule["version"]), (40.0, 1.5, "built-in"))

	def test_seeding_version_one_changes_no_pay(self):
		before, _ = self.run_period()
		phone_config.seed(payroll_settings.KIND, payroll_settings.OVERTIME_KEY, payroll_settings.seed_body(), "seed")
		after, context = self.run_period()
		self.assertEqual(before, after, "decision 9: identical output")
		self.assertEqual(context["config_versions"]["overtime_rule"], "payroll_setting:overtime_rule@1")

	def test_the_preview_reruns_the_period_and_flags_who_moves(self):
		identical = payroll_settings.preview(
			"overtime_rule", {"schema_version": 1, "key": "overtime_rule", **payroll_settings.DEFAULT_OVERTIME,
			                  "effective_from": "2020-01-01"}, self.args)
		self.assertTrue(identical["identical"])
		self.assertEqual(identical["flagged"], [])
		double = payroll_settings.preview(
			"overtime_rule", {"schema_version": 1, "key": "overtime_rule", "weekly_threshold_hours": 40,
			                  "multiplier": 2.0, "effective_from": "2020-01-01"}, self.args)
		self.assertFalse(double["identical"])
		self.assertIn(WORKER, [e["employee"] for e in double["employees"] if e["changes"]])
		self.assertEqual(double["flag_threshold_pct"], 2.0)
		self.assertEqual(payroll_settings.overtime()["multiplier"], 1.5, "the overlay ends with the preview")

	def test_a_payroll_setting_is_never_published_over_mcp(self):
		phone_config.save_draft(payroll_settings.KIND, "overtime_rule",
		                        {"schema_version": 1, "key": "overtime_rule", "weekly_threshold_hours": 40,
		                         "multiplier": 1.5, "effective_from": "2026-01-01"}, "test", "Operator")
		with self.assertRaisesRegex(Exception, "only in the Desk by a person"):
			config_lifecycle.refuse_payroll_publish("the overtime_rule")
		with config_lifecycle.desk_action():
			config_lifecycle.refuse_payroll_publish("the overtime_rule")  # no raise in the Desk

	def test_a_version_needs_a_date_and_sane_numbers(self):
		report = payroll_settings.validate({"weekly_threshold_hours": 0, "multiplier": 9}, "overtime_rule")
		self.assertEqual(len(report["errors"]), 3)
