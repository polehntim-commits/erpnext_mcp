# SPDX-License-Identifier: MIT
"""/app/market-prices and the pro forma / harvest-timing feeds. v0.266.0.

The dashboard answers from the same market_prices module as the phone; it adds filters, the season curve and the
percentiles. The breakeven sensitivity gains what the market actually paid. Vendored library, no CDN."""

import datetime
import json
import pathlib

import frappe

from erpnext_mcp import market_prices as mp
from erpnext_mcp.api import market_dashboard

from .harness import ROLES, STORE
from .test_market_prices import CHERRY_WEEK, MarketCase, row

REPO = pathlib.Path(__file__).resolve().parent.parent
PAGE = REPO / "erpnext_mcp" / "erpnext_mcp" / "page" / "market_prices"
D = datetime.date


class Dashboard(MarketCase):
	def setUp(self):
		super().setUp()
		self.ingest()
		self._roles = list(ROLES.get("Administrator", []))
		self.addCleanup(lambda: ROLES.__setitem__("Administrator", self._roles))
		ROLES["Administrator"] = ["Farm Manager"]

	def test_the_view_is_the_phones_chart_and_card(self):
		view = market_dashboard._view(commodity="sweet_cherries", size="10 row", overlays="breakeven")
		self.assertEqual(view["chart"], mp.chart("sweet_cherries", size="10 row", overlays=["breakeven"]))
		self.assertEqual(view["card"]["headline_size"], "10 row")
		self.assertIn("season_curve", view)

	def test_choices(self):
		c = market_dashboard._choices("sweet_cherries")
		self.assertEqual(c["sizes"], ["9 1/2 row", "10 row"])
		self.assertIn({"key": "cantaloupe", "title": "Cantaloupe"}, c["commodities"])

	def test_a_worker_without_read_is_refused(self):
		STORE.denied_permissions.add(("USDA Price Quote", "read"))
		self.addCleanup(lambda: STORE.denied_permissions.discard(("USDA Price Quote", "read")))
		with self.assertRaisesRegex(Exception, "read access"):
			market_dashboard._view(commodity="sweet_cherries")


class Feeds(MarketCase):
	def test_the_breakeven_sensitivity_gets_the_markets_range(self):
		days = [D(2025, 6, 1) + datetime.timedelta(days=i) for i in range(20)]
		self.ingest(rows=[row(d, "10 row size", 30.0 + i, 40.0 + i) for i, d in enumerate(days)], start="2025-06-01", end="2025-06-20")
		ref = mp.reference_for_crop("Cherries", "Pound")
		self.assertEqual(ref["commodity"], "sweet_cherries")
		self.assertEqual(ref["seasons"][0]["season"], 2025)
		self.assertAlmostEqual(ref["seasons"][0]["grower_p50_per_lb"], ref["seasons"][0]["p50"] / 18 - 0.60, places=3)
		self.assertIsNone(mp.reference_for_crop("Hazelnuts"))

	def test_the_season_curve(self):
		self.ingest()
		curve = mp.season_curve("sweet_cherries")
		self.assertEqual(curve["seasons"], 1)
		self.assertEqual(curve["peak_week"], 1)


class Shipped(MarketCase):
	def test_page_vendored_and_linked(self):
		for name in ("market_prices.json", "market_prices.html", "market_prices.js"):
			self.assertTrue((PAGE / name).exists())
		script = (PAGE / "market_prices.js").read_text()
		self.assertIn("/assets/erpnext_mcp/vendor/lightweight-charts/", script)
		for cdn in ("cdnjs", "jsdelivr", "unpkg", "https://", "http://"):
			self.assertNotIn(cdn, script)
		self.assertIn("cost of market access", script.lower())
		vendor = REPO / "erpnext_mcp" / "public" / "vendor" / "lightweight-charts"
		self.assertIn("Apache License", (vendor / "LICENSE").read_text())
		self.assertIn("TradingView", (vendor / "README.txt").read_text())
		spec = json.loads((REPO / "erpnext_mcp" / "workspace_specs" / "market_sales.json").read_text())
		self.assertIn("market-prices", [s["link_to"] for s in spec["shortcuts"]])
		html = (PAGE / "market_prices.html").read_text()
		self.assertNotIn("'", html.split("-->", 1)[1])


class Tile(MarketCase):
	def test_the_market_card_tile_is_seeded_and_every_seed_publishes(self):
		from erpnext_mcp import tiles

		tile = tiles.SEEDS["market_card"]
		self.assertEqual(tile["target"], {"kind": "report", "report": "market_card"})
		self.assertEqual(tile["icon"], "chart.line.uptrend.xyaxis")
		self.assertEqual(tile["min_app_version"], "0.51.0")
		for key, body in tiles.SEEDS.items():  # ipm_map once pointed at a report key the validator refused
			self.assertEqual(tiles.target_problems(body["target"], body["audience"]), [], key)
			self.assertIn(body["target"]["report"], tiles.REPORTS, key)
			self.assertIn(body["icon"], tiles.ICONS, key)


class SizeOrder(MarketCase):
	def test_the_size_filter_uses_the_one_size_order(self):
		saved = list(ROLES.get("Administrator", []))
		self.addCleanup(lambda: ROLES.__setitem__("Administrator", saved))
		ROLES["Administrator"] = ["Farm Manager"]
		self.ingest()
		sizes = market_dashboard._choices("sweet_cherries")["sizes"]
		rows = frappe.db.get_all(mp.QUOTE, filters={"commodity_key": "sweet_cherries"}, fields=["size", "size_rank"])
		ranks = {r["size"]: r.get("size_rank") for r in rows if r.get("size")}
		self.assertTrue(sizes)
		self.assertEqual(sizes, sorted(ranks, key=lambda s: mp.size_order(s, ranks[s])))
