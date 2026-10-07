# SPDX-License-Identifier: MIT
"""Server ↔ iOS / Desk contract fixtures for v0.265.0 — market prices. docs/contracts/market_prices_v0_265.yaml.

Synthetic MARS rows (shipping point with a Not Quoted day and a missing day, terminal, movement) go through the
real ingest and the real routes. Regenerate deliberately with FARM_CONTRACT_REGEN=1, then copy to the app."""

import datetime
import pathlib

from erpnext_mcp import market_prices as mp
from erpnext_mcp.api import mobile as mobile_api

from . import test_contract_v0_262_0 as v262
from .harness import set_roles
from .test_api_mobile import WORKER, MobileAPITestCase
from .test_market_prices import CHERRY_WEEK, FakeMARS, row

HERE = pathlib.Path(__file__).parent / "contract" / "v0_265_0"
D = datetime.date


def market_site():
	shipping = list(CHERRY_WEEK) + [row(D(2026, 6, 29) + datetime.timedelta(days=i), "10 row size", 50.0 - i, 60.0 - i,
	                                    52.0 - i, 56.0 - i) for i in range(5)]
	terminal = [{**row(D(2026, 7, 9), "10 row size", 60.0, 66.0), "market_location_name": "CHICAGO"}]
	movement = [{"report_date": d.strftime("%m/%d/%Y"), "commodity": "CHERRIES", "origin": "WA", "10000_lb_units": v,
	             "unit": "10,000 lb units"} for d, v in ((D(2026, 6, 30), 120.0), (D(2026, 7, 7), 150.0))]
	fake = FakeMARS({"2412": shipping, "2290": terminal, "3284": movement})
	mp.ingest("sweet_cherries", "2026-06-29", "2026-07-10", roles=["shipping_point", "terminal", "movement"], fetch=fake)


class ContractMarket(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		market_site()
		set_roles(WORKER, ["Field Worker"])
		self.be()

	def check(self, name, value):
		before = v262.HERE
		v262.HERE = HERE
		try:
			v262.check(self, name, value)
		finally:
			v262.HERE = before

	def test_the_two_answers(self):
		self.check("get_market_card", mobile_api.get_market_card(commodity="sweet_cherries"))
		self.check("get_market_chart", mobile_api.get_market_chart(commodity="sweet_cherries", interval="day",
		                                                           overlays="terminal,grower_return,breakeven"))
