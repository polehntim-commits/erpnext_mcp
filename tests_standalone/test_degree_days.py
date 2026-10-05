"""Growing degree days and the stage they suggest. v0.249.0 (approved queue item 10; decision 12)."""

import datetime
from unittest import mock

from erpnext_mcp import ccf_providers, degree_days, go_hold

from .harness import STORE
from .test_go_hold import ON, TODAY, GoHoldTestCase

TABLE = "Sweet Cherry: base = 50\nSweet Cherry: 51 = 100\nSweet Cherry: 55 = 200\nSweet Cherry: 57 = 300"


def temps(days_before=40, days_after=16, high=70, low=40):
	"""A flat spring: 10 degree days a day (base 50)."""
	today = datetime.date.fromisoformat(TODAY)
	out = {}
	for offset in range(-days_before, days_after + 1):
		out[(today + datetime.timedelta(days=offset)).isoformat()] = (high, low)
	return out


class DegreeDayTestCase(GoHoldTestCase):
	def setUp(self):
		super().setUp()
		biofix = (datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=15)).strftime("%m-%d")
		if biofix > TODAY[5:]:  # the test date is early January: keep the biofix in this year
			biofix = "01-01"
		self.biofix = biofix
		self.configure(enabled=1, **{**ON, "allow_get_degree_days": 1, "degree_day_stage_table": TABLE,
		                             "degree_day_biofix": biofix})
		STORE.seed("Field", [{"name": "B7", "field_name": "B7"}])
		self.patches = [
			mock.patch.object(ccf_providers, "block_point", return_value=(45.6, -121.2, "B7")),
			mock.patch.object(ccf_providers, "forecast_enabled", return_value=True),
			mock.patch.object(degree_days, "daily_temps", return_value=temps()),
		]
		for patcher in self.patches:
			patcher.start()
			self.addCleanup(patcher.stop)


class TheArithmetic(DegreeDayTestCase):
	def test_simple_average_with_a_floor_and_an_upper_cutoff(self):
		self.assertEqual(degree_days.gdd(70, 40, 50, 86), 10.0)
		self.assertEqual(degree_days.gdd(95, 60, 50, 86), 23.0)
		self.assertEqual(degree_days.gdd(45, 30, 50, 86), 0.0)

	def test_the_table_parses_and_names_what_it_cannot_read(self):
		table, problems = degree_days.parse_table(TABLE + "\nSweet Cherry: bloom = 150\nnonsense")
		self.assertEqual(table["sweet cherry"]["stages"], [(100.0, "51"), (200.0, "55"), (300.0, "57")])
		self.assertEqual(len(problems), 2)


class ForABlock(DegreeDayTestCase):
	def test_season_total_estimate_and_when_the_next_stage_is_expected(self):
		data = self.tool_data("get_degree_days", {"block": "B7", "crop": "Sweet Cherry"})
		days = (datetime.date.fromisoformat(TODAY) - datetime.date.fromisoformat(data["biofix"])).days + 1
		self.assertEqual(data["gdd_season"], 10.0 * days)
		self.assertEqual(data["gdd_next_7"], 70.0)
		self.assertTrue(data["note"].startswith("An estimate"))
		reached = [code for need, code in ((100, "51"), (200, "55"), (300, "57")) if data["gdd_season"] >= need]
		self.assertEqual(data["estimated_bbch"], reached[-1] if reached else None)

	def test_no_table_for_the_crop_means_no_estimate_and_says_so(self):
		data = self.tool_data("get_degree_days", {"block": "B7", "crop": "Apple"})
		self.assertIsNone(data["estimated_bbch"])
		self.assertIn("No degree-day stage table for Apple", data["note"])


class AnEstimateNeverPasses(DegreeDayTestCase):
	def test_verify_stage_names_the_estimate_and_stays_verify(self):
		self.a_rule(condition_tree={"id": "before_bud_burst", "path": "phenology.bbch", "op": "lt", "value": "51"})
		task = self.pruning()
		STORE.get_raw("Farm Task", task).update(location_doctype="Field", location="B7")
		with mock.patch.object(degree_days, "_crop_of", return_value="Sweet Cherry"):
			verdict = go_hold.check(task)
		self.assertEqual(verdict["status"], go_hold.VERIFY)
		self.assertIn("Degree days", verdict["reasons"][0])
		self.assertIn("suggest about BBCH", verdict["reasons"][0])
