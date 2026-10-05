"""Work-timing providers: weather per block and the latest stage. v0.239.0 (queue item 5)."""

from unittest import mock

from erpnext_mcp import ccf, ccf_providers

from .harness import STORE
from .test_compliance_rule_engine import RuleEngineTestCase

TODAY = "2026-01-15"
PAYLOAD = {
	"daily": {
		"time": ["2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15", "2026-01-16", "2026-01-17", "2026-01-18"],
		"precipitation_sum": [0.4, 0.0, 0.0, 0.0, 0.0, 0.3, 0.0],
		"precipitation_probability_max": [90, 5, 0, 10, 20, 70, 10],
		"temperature_2m_min": [30, 28, 29, 27, 31, 33, 30],
		"temperature_2m_max": [44, 46, 47, 48, 50, 45, 44],
		"wind_speed_10m_max": [12, 6, 5, 7, 9, 15, 8],
		"wind_gusts_10m_max": [20, 10, 9, 12, 15, 25, 13],
	},
	"hourly": {"time": [], "temperature_2m": [], "precipitation": [], "precipitation_probability": [], "wind_speed_10m": []},
}


class TheForecastIsNormalised(RuleEngineTestCase):
	def test_rain_risk_is_the_chance_of_at_least_one_wet_day(self):
		out = ccf_providers.normalise(PAYLOAD, "cell", today=TODAY)
		risk = [d["rain_risk_cum_pct"] for d in out["forecast"]["daily"]]
		# 10% today; 1-(.9×.8)=28%; then a 70% day → 1-(.9×.8×.3)=78.4%
		self.assertEqual(risk[:3], [10.0, 28.0, 78.4])
		self.assertEqual(out["forecast"]["daily"][0]["date"], TODAY, "past days are not 'ahead'")
		self.assertEqual(out["recent"]["dry_streak_days"], 2)

	def test_a_canker_style_rule_holds_on_rain_risk_and_clears_when_the_forecast_does(self):
		tree = {"all": [
			{"id": "dry_ahead", "path": "weather.forecast.daily[0..6].rain_risk_cum_pct", "agg": "max", "op": "lt",
			 "value": 40, "reason": {"en": "Rain likely this week — no pruning", "es": "Lluvia probable esta semana — no se poda"}},
			{"id": "no_freeze", "path": "weather.forecast.daily[0..1].tmin_f", "agg": "min", "op": "gt", "value": 28},
		]}
		ccf.parse_tree(tree)
		wet = ccf_providers.normalise(PAYLOAD, "cell", today=TODAY)
		with mock.patch.object(ccf_providers, "_weather", return_value=wet):
			result = ccf.evaluate(tree, ccf.build_context({}, as_of=TODAY, providers={"weather"}))
		self.assertEqual([f["id"] for f in result["failures"]], ["dry_ahead", "no_freeze"])
		dry_payload = {**PAYLOAD, "daily": {**PAYLOAD["daily"], "precipitation_probability_max": [0] * 7,
		                                     "precipitation_sum": [0] * 7, "temperature_2m_min": [35] * 7}}
		dry = ccf_providers.normalise(dry_payload, "cell", today=TODAY)
		with mock.patch.object(ccf_providers, "_weather", return_value=dry):
			self.assertTrue(ccf.evaluate(tree, ccf.build_context({}, as_of=TODAY, providers={"weather"}))["passed"])

	def test_with_forecasts_off_a_weather_check_is_a_hold_that_says_why(self):
		tree = {"id": "dry_ahead", "path": "weather.forecast.daily[0..6].rain_risk_cum_pct", "agg": "max", "op": "lt", "value": 40}
		result = ccf.evaluate(tree, ccf.build_context({"location_doctype": "Field", "location": "B7"}, providers={"weather"}))
		self.assertFalse(result["passed"])
		self.assertTrue(result["failures"][0]["missing"])

	def test_one_request_per_cell_then_the_cache(self):
		calls = []
		with mock.patch("erpnext_mcp.services.weather._get_json", side_effect=lambda *a, **k: calls.append(1) or PAYLOAD):
			ccf_providers._CELL_CACHE.clear()
			ccf_providers.fetch_forecast(45.6, -121.2)
			ccf_providers.fetch_forecast(45.6, -121.2)
		self.assertEqual(len(calls), 1)


class TheStageIsTheLatestObservation(RuleEngineTestCase):
	def test_bbch_from_the_blocks_latest_growth_stage(self):
		STORE.seed("Crop Observation", [
			{"name": "CO-1", "block_doctype": "Field", "block": "B7", "growth_stage_code": "53", "observed_at": "2026-03-02 09:00:00"},
			{"name": "CO-2", "block_doctype": "Field", "block": "B7", "growth_stage_code": "55", "observed_at": "2026-03-09 09:00:00"},
		])
		values = ccf.build_context({"location_doctype": "Field", "location": "B7"}, as_of="2026-03-10", providers={"phenology"})
		self.assertEqual(values["phenology"]["bbch"], "55")
		self.assertEqual(values["phenology"]["bbch_age_days"], 1)
		tree = {"id": "stage", "path": "phenology.bbch", "op": "lt", "value": "51", "reason": "Past bud burst — too late to prune for canker"}
		ccf.parse_tree(tree)
		self.assertFalse(ccf.evaluate(tree, values)["passed"])
