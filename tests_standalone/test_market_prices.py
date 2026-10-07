# SPDX-License-Identifier: MIT
"""USDA AMS market prices, done right. v0.265.0 (Tim, 2026-10-06). Contract: docs/contracts/market_prices_v0_265.yaml.

The USDA API is replaced by `fetch` — the one door `ingest` takes — answering MARS-shaped Report Details rows,
so every rule is driven through the real parse → store → read path. THE TWO BUGS TIM NAMED ARE THE FIRST TWO
CLASSES:

1. `NoCarryForward` — a size with no quote on a day has NO point that day; a row the report lists without a
   price is stored Not Quoted, told apart from a day the item was not in the report; nothing is ever filled.
2. `TheSeasonLineConnects` — the quoted days of a season come back as one ordered series per size (the line the
   old app never drew), daily and weekly.
3. `ShippingPointComesThrough` — the rows that never came through in the Farm App: Report Details rows, no
   terminal package filter, idempotent upsert, sizes kept apart.
4. `FlaggedNotDropped` — unknown sizes / packs, field-set changes, HTTP errors, missing in-season days.
5. `Candles` — the documented derivation.
6. `GrowerFraming` — grower return $/lb, breakeven from config / analysis, terminal as cost of market access,
   the signal, percentiles.
7. `AnyCommodity` — cantaloupe counts (9s / 12s / 15s) through the same code.
"""

import datetime

from erpnext_mcp import market_prices as mp

from .fixtures import V12TestCase
from .harness import STORE


def row(day, size, low=None, high=None, ml=None, mh=None, commodity="CHERRIES", pkg="18 lb cartons bagged",
        district="YAKIMA VALLEY AND WENATCHEE DISTRICT WASHINGTON", var="VARIOUS RED SWEET VARIETIES", cmt=None):
	out = {"report_date": day.strftime("%m/%d/%Y"), "commodity": commodity, "var": var, "pkg": pkg, "item_size": size,
	       "district": district, "grade": "WA One", "low_price": low, "high_price": high, "mostly_low_price": ml,
	       "mostly_high_price": mh, "slug_id": "2412", "market_type": "Shipping Point"}
	if cmt:
		out["rep_cmt"] = cmt
	return out


D = datetime.date


class FakeMARS:
	"""Answers MARS-shaped rows for whichever report and window is asked."""

	def __init__(self, by_slug=None, errors=()):
		self.by_slug = by_slug or {}
		self.errors = set(errors)
		self.calls = []

	def __call__(self, slug, start, end, all_sections=False):
		self.calls.append((slug, start, end, all_sections))
		if slug in self.errors:
			return None, f"HTTP 500 from MARS for report {slug}"
		rows = [r for r in self.by_slug.get(slug, [])
		        if start <= datetime.datetime.strptime(r["report_date"], "%m/%d/%Y").date() <= end]
		return rows, None


CHERRY_WEEK = [
	row(D(2026, 7, 6), "10 row size", 40.0, 50.0, 42.0, 46.0),
	row(D(2026, 7, 6), "9 1/2 row size", 36.0, 44.0, 38.0, 42.0),
	row(D(2026, 7, 7), "10 row size", 42.0, 52.0, 44.0, 48.0),
	row(D(2026, 7, 7), "9 1/2 row size", None, None, cmt="supplies insufficient to establish a market"),
	# 8 Jul: the 9 1/2 row size is not in the report at all.
	row(D(2026, 7, 8), "10 row size", 41.0, 51.0, 43.0, 47.0),
	row(D(2026, 7, 9), "10 row size", 38.0, 55.0, None, None),
	row(D(2026, 7, 9), "9 1/2 row size", 34.0, 40.0, 36.0, 38.0),
]


class MarketCase(V12TestCase):
	def ingest(self, rows=CHERRY_WEEK, start="2026-07-06", end="2026-07-10", extra=None, errors=(), key="sweet_cherries", roles=None):
		fake = FakeMARS({"2412": list(rows), **(extra or {})}, errors)
		self.fake = fake
		return mp.ingest(key, start, end, roles=roles or ["shipping_point"], fetch=fake)

	def size_rows(self, size):
		return sorted((r for r in STORE.rows("USDA Price Quote") if r.get("size") == size), key=lambda r: str(r["report_date"]))


class NoCarryForward(MarketCase):
	def test_a_day_with_no_quote_has_no_point_and_a_listed_unpriced_row_is_not_quoted(self):
		self.ingest()
		rows = self.size_rows("9 1/2 row")
		self.assertEqual([str(r["report_date"])[:10] for r in rows], ["2026-07-06", "2026-07-07", "2026-07-09"],
		                 "8 Jul is absent — never a carried-forward copy of 7 Jul")
		unpriced = rows[1]
		self.assertEqual(unpriced["quote_status"], "Not Quoted")
		self.assertIsNone(unpriced.get("low_price"))
		self.assertIn("insufficient", unpriced["comment"])

	def test_the_candles_leave_gaps_and_mark_not_quoted(self):
		self.ingest()
		chart = mp.chart("sweet_cherries", size="9 1/2 row")
		series = chart["series"][0]
		self.assertEqual([c["time"] for c in series["candles"]], ["2026-07-06", "2026-07-09"])
		self.assertEqual(series["not_quoted_dates"], ["2026-07-07"])
		self.assertNotIn("2026-07-08", [c["time"] for c in series["candles"]] + series["not_quoted_dates"])

	def test_the_card_shows_no_price_for_a_size_not_quoted_today(self):
		self.ingest(rows=CHERRY_WEEK[:4], end="2026-07-07")
		card = mp.card("sweet_cherries")
		nine = next(s for s in card["sizes"] if s["size"] == "9 1/2 row")
		self.assertEqual(nine["quote_status"], "Not Quoted")
		self.assertIsNone(nine["mid"], "no yesterday's number standing in")


class TheSeasonLineConnects(MarketCase):
	def test_every_quoted_day_is_one_ordered_series(self):
		self.ingest()
		ten = mp.chart("sweet_cherries", size="10 row")["series"][0]["candles"]
		self.assertEqual([c["time"] for c in ten], ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"])
		self.assertEqual([c["close"] for c in ten], [44.0, 46.0, 45.0, 46.5])

	def test_weekly_candles_and_the_season_overlay(self):
		self.ingest()
		week = mp.chart("sweet_cherries", size="10 row", interval="week")
		self.assertEqual(len(week["series"][0]["candles"]), 1)
		self.assertEqual(week["seasons"][0]["season"], 2026)
		self.assertEqual(week["seasons"][0]["week_of_season"][0]["week"], 1)


class ShippingPointComesThrough(MarketCase):
	def test_rows_are_stored_sizes_apart_and_idempotent(self):
		first = self.ingest()
		self.assertEqual(first["stored"], 7)
		self.assertEqual(first["not_quoted"], 1)
		again = self.ingest()
		self.assertEqual((again["stored"], again["unchanged"]), (0, 7))
		self.assertEqual({r["market_type"] for r in STORE.rows("USDA Price Quote")}, {"Shipping Point"})
		self.assertEqual({r["size"] for r in STORE.rows("USDA Price Quote")}, {"10 row", "9 1/2 row"})

	def test_no_package_filter_drops_shipping_rows(self):
		odd = [row(D(2026, 7, 6), "10 row size", 40.0, 50.0, pkg="18 lb cartons loose")]
		self.ingest(rows=odd)
		self.assertEqual(len(STORE.rows("USDA Price Quote")), 1, "a pack variant is stored (and flagged), never dropped")

	def test_windows_cover_the_whole_range(self):
		self.ingest(start="2026-05-01", end="2026-07-10")
		self.assertEqual(len(self.fake.calls), 3)  # 31 + 31 + 9 days


class FlaggedNotDropped(MarketCase):
	def issues(self, kind):
		return [r for r in STORE.rows("Market Data Issue") if r["kind"] == kind]

	def test_unknown_size_and_pack_are_flagged_and_kept(self):
		self.ingest(rows=[row(D(2026, 7, 6), "assorted mix", 30.0, 35.0, pkg="bushel bins")])
		kept = STORE.rows("USDA Price Quote")
		self.assertEqual([r["size"] for r in kept], ["assorted mix"], "shown as-is, not dropped")
		self.assertTrue(self.issues("unknown_size"))
		self.assertTrue(self.issues("unknown_pack"))

	def test_a_size_the_vocabulary_lacks_but_the_generic_reading_knows_is_kept_unflagged(self):
		self.ingest(rows=[row(D(2026, 7, 6), "13 row size", 30.0, 35.0, pkg="5 kg boxes")])
		kept = STORE.rows("USDA Price Quote")[0]
		self.assertEqual((kept["size"], kept["pack_net_lb"]), ("13 row", 11.02))
		self.assertFalse(self.issues("unknown_size"))
		self.assertFalse(self.issues("unknown_pack"))

	def test_an_http_error_is_an_issue_not_silence(self):
		result = self.ingest(errors=["2412"])
		self.assertEqual(result["stored"], 0)
		self.assertTrue(self.issues("http_error"))

	def test_a_changed_field_set_is_flagged(self):
		self.ingest(rows=CHERRY_WEEK[:1], end="2026-07-06")
		renamed = [{**{("size" if k == "item_size" else k): v for k, v in CHERRY_WEEK[2].items()}}]
		self.ingest(rows=renamed, start="2026-07-07", end="2026-07-07")
		self.assertTrue(self.issues("field_set_changed"))

	def test_an_in_season_weekday_without_a_report_is_missing(self):
		self.ingest(rows=[row(D(2026, 7, 6), "10 row size", 40.0, 50.0), row(D(2026, 7, 9), "10 row size", 40.0, 50.0)],
		            start="2026-07-06", end="2026-07-09")
		missing = sorted(str(r["report_date"])[:10] for r in self.issues("report_missing"))
		self.assertEqual(missing, ["2026-07-07", "2026-07-08"])


class Candles(MarketCase):
	def test_the_documented_derivation(self):
		rows = [{"report_date": "2026-07-06", "low_price": 40, "high_price": 50, "mostly_low": 42, "mostly_high": 46, "quote_status": "Priced"},
		        {"report_date": "2026-07-08", "low_price": 38, "high_price": 55, "quote_status": "Priced"},
		        {"report_date": "2026-07-09", "quote_status": "Not Quoted"}]
		week, nq = mp.candles(rows, "week")
		self.assertEqual(week, [{"time": "2026-07-06", "open": 44.0, "high": 55.0, "low": 38.0, "close": 46.5, "quotes": 2, "not_quoted": 1}])
		self.assertEqual(nq, ["2026-07-09"])
		day, _ = mp.candles(rows[:1], "day")
		self.assertEqual((day[0]["open"], day[0]["close"], day[0]["low"], day[0]["high"]), (44.0, 44.0, 40.0, 50.0))


class GrowerFraming(MarketCase):
	def test_grower_return_and_breakeven_from_config(self):
		self.ingest()
		card = mp.card("sweet_cherries")
		self.assertEqual(card["headline_size"], "10 row")
		# 9 Jul 10 row: no mostly → (38 + 55) / 2 = 46.5 / 18 lb − 0.60 = 1.9833
		self.assertAlmostEqual(card["grower_return_per_lb"], 46.5 / 18 - 0.60, places=3)
		self.assertEqual(card["breakeven"], {"per_lb": 1.22, "label": "Constancy 2027 draft pro forma", "source": "config"})
		self.assertAlmostEqual(card["above_breakeven_per_lb"], 46.5 / 18 - 0.60 - 1.22, places=3)
		self.assertIn("cost of market access", card["caveat"])
		self.assertNotIn("margin", card["caveat"].replace("not margin", ""))

	def test_terminal_is_context_with_the_cost_of_market_access(self):
		terminal = [{**row(D(2026, 7, 9), "10 row size", 60.0, 66.0), "market_location_name": "CHICAGO"}]
		self.ingest(extra={"2290": terminal}, roles=["shipping_point", "terminal"])
		card = mp.card("sweet_cherries")
		self.assertEqual(card["terminal"]["mid"], 63.0)
		self.assertEqual(card["terminal"]["cost_of_market_access"], round(63.0 - 46.5, 4))

	def test_the_signal(self):
		self.assertEqual(mp.signal(5.0, 2.0)["label"], "demand building")
		self.assertEqual(mp.signal(-6.0, 20.0)["label"], "supply pressure")
		self.assertEqual(mp.signal(-6.0, None)["label"], "softening")
		self.assertEqual(mp.signal(None, None)["label"], "not enough data")

	def test_breakeven_prefers_a_pound_analysis(self):
		STORE.seed("Breakeven Analysis", [{"name": "BE-2027", "analysis_name": "Constancy 2027", "breakeven_price": 1.31, "unit_label": "Pound"}])
		cfg = {**mp.seed_body("sweet_cherries"), "breakeven": {"analysis": "BE-2027", "per_lb": 1.22, "label": "x"}}
		self.assertEqual(mp.breakeven(cfg), {"per_lb": 1.31, "label": "Constancy 2027", "source": "analysis"})

	def test_percentiles_for_the_pro_forma(self):
		days = [D(2026, 6, 1) + datetime.timedelta(days=i) for i in range(20)]
		self.ingest(rows=[row(d, "10 row size", 30.0 + i, 40.0 + i) for i, d in enumerate(days)], start="2026-06-01", end="2026-06-20")
		p = mp.percentiles("sweet_cherries", "10 row")
		self.assertEqual(p[0]["season"], 2026)
		self.assertEqual(p[0]["quotes"], 20)
		self.assertLess(p[0]["p10"], p[0]["p50"])
		self.assertLess(p[0]["p50"], p[0]["p90"])


class AnyCommodity(MarketCase):
	def test_cantaloupe_counts_through_the_same_code(self):
		melons = [row(D(2026, 7, 6), "9s (6 size)", 18.0, 20.0, commodity="CANTALOUPS", pkg="1/2 cartons",
		              district="SAN JOAQUIN VALLEY CALIFORNIA"),
		          row(D(2026, 7, 6), "12s", 16.0, 18.0, commodity="CANTALOUPS", pkg="1/2 cartons", district="SAN JOAQUIN VALLEY CALIFORNIA"),
		          row(D(2026, 7, 6), "10 row size", 40.0, 50.0)]  # a cherry row in the same report is not a melon
		fake = FakeMARS({"2402": melons})
		result = mp.ingest("cantaloupe", "2026-07-06", "2026-07-06", roles=["shipping_point"], fetch=fake)
		self.assertEqual(result["stored"], 2)
		sizes = {r["size"]: r["size_rank"] for r in STORE.rows("USDA Price Quote")}
		self.assertEqual(sizes, {"9s": 1, "12s": 2})
		self.assertEqual({r["pack_net_lb"] for r in STORE.rows("USDA Price Quote")}, {40.0})

	def test_size_normalisation(self):
		cherry, melon = mp.seed_body("sweet_cherries"), mp.seed_body("cantaloupe")
		self.assertEqual(mp.normalise_size("8 1/2 row size", cherry)[:2], ("8 1/2 row", 1))
		self.assertEqual(mp.normalise_size("10.5 row", cherry)[:2], ("10 1/2 row", 5))
		self.assertEqual(mp.normalise_size("15's", melon)[:2], ("15s", 3))
		self.assertEqual(mp.normalise_size("jumbo", melon)[0], "jumbo")
		self.assertFalse(mp.normalise_size("assorted mix", melon)[2])

	def test_generic_sizes_for_any_commodity(self):
		order = ["3 inch", "2 1/2 inch"]
		self.assertEqual(sorted(order, key=lambda s: mp.size_order(s)), ["3 inch", "2 1/2 inch"])
		self.assertEqual(sorted(["88s", "72s", "100s"], key=mp.size_order), ["72s", "88s", "100s"])
		self.assertEqual(sorted(["11 row", "8 1/2 row", "10 row"], key=mp.size_order), ["8 1/2 row", "10 row", "11 row"])
		self.assertEqual(sorted(["small", "jumbo", "large"], key=mp.size_order), ["jumbo", "large", "small"])
		self.assertEqual(mp.generic_size("2 layer tray pack")["kind"], "other")
		self.assertEqual(mp.generic_pack("40 lb cartons tray pack")["net_lb"], 40.0)
		self.assertEqual(mp.generic_pack("per lb")["net_lb"], 1.0)
		self.assertIsNone(mp.generic_pack("24 inch bins")["net_lb"])

	def test_the_seeds_validate(self):
		for key in mp.SEED:
			self.assertEqual(mp.validate(mp.seed_body(key), key)["errors"], [], key)


# ── v0.265.0 scope: every AMS commodity, discovered as data ─────────────────
INDEX = [
	{"id": 2412, "reportTitle": "YAKIMA Shipping Point Fruit Prices (YA_FV110)", "publishedDate": "2026-10-06 12:00:00 MDT"},
	{"id": 2402, "reportTitle": "Phoenix Shipping Point Fruit Prices (IX_FV110)", "publishedDate": "2026-10-06 12:00:00 MDT"},
	{"id": 2290, "reportTitle": "Chicago Terminal Market Fruit Prices (HX_FV010)", "publishedDate": "2026-10-06 12:00:00 MDT"},
	{"id": 3284, "reportTitle": "NATIONAL TRUCK, AIR, AND BOAT Daily Movement Report", "publishedDate": "2026-10-06 12:00:00 MDT"},
	{"id": 3258, "reportTitle": "Cherries Shipments (Movement) Weekly (WA_FV415)", "publishedDate": "2025-04-30 12:00:00 MDT"},
	{"id": 2117, "reportTitle": "Brookhaven Stockyard - Brookhaven, MS", "publishedDate": "2026-10-06 12:00:00 MDT"},
	{"id": 3333, "reportTitle": "Yakima Shipping Point Prices (SX_FV195)", "publishedDate": "2022-08-01 12:00:00 MDT"},
]

APPLES = [row(D(2026, 7, 6), "88s", 28.0, 32.0, commodity="APPLES", pkg="cartons tray pack 40 lb", var="GALA"),
          row(D(2026, 7, 6), "72s", 30.0, 34.0, commodity="APPLES", pkg="cartons tray pack 40 lb", var="GALA"),
          row(D(2026, 7, 7), "100s", 26.0, 30.0, commodity="APPLES", pkg="cartons tray pack 40 lb", var="FUJI"),
          row(D(2026, 7, 7), "extra fancy mixed", None, None, commodity="APPLES", pkg="bins", var="FUJI")]


class Discovery(MarketCase):
	def test_the_catalog_is_classified(self):
		out = mp.refresh_catalog(lambda: (INDEX, None))
		self.assertEqual(out["created"], len(INDEX))
		role = {r["name"]: r["role"] for r in STORE.rows("Market Report")}
		self.assertEqual((role["2412"], role["2290"], role["3284"], role["3258"], role["2117"]),
		                 ("shipping_point", "terminal", "movement", "movement_weekly", "other"))
		self.assertEqual(STORE.get_raw("Market Report", "3333")["stale"], 1)
		self.assertEqual(STORE.get_raw("Market Report", "2412")["slug_name"], "YA_FV110")

	def test_observing_builds_the_available_index_and_a_draft(self):
		mp.refresh_catalog(lambda: (INDEX, None))
		fake = FakeMARS({"2412": CHERRY_WEEK + APPLES})
		mp.observe(["2412"], "2026-07-06", "2026-07-10", fetch=fake)
		avail = {e["commodity"]: e for e in mp.available()}
		self.assertEqual(set(avail), {"CHERRIES", "APPLES"})
		self.assertEqual(avail["CHERRIES"]["config"], "sweet_cherries")
		self.assertTrue(avail["CHERRIES"]["published"])
		self.assertEqual(avail["APPLES"]["config"], "apples")
		self.assertFalse(avail["APPLES"]["published"], "a seeded draft is not live")
		out = mp.draft_from_observations("APPLES")
		body = out["body"]
		self.assertEqual([v["label"] for v in body["size"]["vocabulary"]], ["72s", "88s", "100s", "extra fancy mixed"])
		self.assertEqual(body["packs"][0]["net_lb"], 40.0)
		self.assertIsNone(next(p for p in body["packs"] if p["label"] == "bins")["net_lb"])
		self.assertEqual(out["draft"]["status"], "Draft")
		self.assertNotIn("apples", mp.commodities_published())

	def test_a_commodity_seen_only_in_terminals_is_not_drafted(self):
		mp.refresh_catalog(lambda: (INDEX, None))
		mp.observe(["2290"], "2026-07-06", "2026-07-06", fetch=FakeMARS({"2290": [row(D(2026, 7, 6), "12s", 20, 22, commodity="KIWIFRUIT")]}))
		with self.assertRaisesRegex(ValueError, "shipping point is the primary line"):
			mp.draft_from_observations("KIWIFRUIT")


class FetchOnceFanOut(MarketCase):
	def test_one_request_per_report_window_for_every_commodity(self):
		fake = FakeMARS({"2412": CHERRY_WEEK + APPLES})
		apples = mp.draft_seed_body("apples")
		apples["reports"] = [r for r in apples["reports"] if r["slug"] == "2412"]
		with mp_published({"apples": apples}):
			out = mp.ingest_all("2026-07-06", "2026-07-10", keys=["sweet_cherries", "apples"], roles=["shipping_point"], fetch=fake)
		self.assertEqual([c[0] for c in fake.calls].count("2412"), 1, "2412 fetched once for both commodities")
		self.assertEqual(out["commodities"]["sweet_cherries"]["stored"], 7)
		self.assertEqual(out["commodities"]["apples"]["stored"], 4)
		self.assertEqual(out["commodities"]["apples"]["not_quoted"], 1)

	def test_a_window_at_the_row_ceiling_is_split_not_truncated(self):
		calls = []

		def capped(slug, start, end, all_sections=False):
			calls.append((start, end))
			return ([{"x": 1}] * (mp.ROW_LIMIT if start < end else 3)), None

		rows, error = mp.fetch_window("2412", D(2026, 7, 1), D(2026, 7, 4), capped)
		self.assertIsNone(error)
		self.assertEqual(len(rows), 12, "four single days of 3 rows, after splitting")

	def test_a_429_waits_and_retries(self):
		from unittest import mock

		class R:
			def __init__(self, code, body=None):
				self.status_code, self.headers, self._body = code, {"Retry-After": "0"}, body

			def json(self):
				return self._body

		import sys
		import types

		answers = [R(429), R(200, {"results": [{"commodity": "CHERRIES"}]})]
		fake_requests = types.SimpleNamespace(get=lambda *a, **k: answers.pop(0))
		with mock.patch.dict(sys.modules, {"requests": fake_requests}), \
		     mock.patch("erpnext_mcp.services.usda_prices.api_key", return_value="k"), \
		     mock.patch("time.sleep"):
			rows, error = mp.http_get("2412", D(2026, 7, 1), D(2026, 7, 1))
		self.assertIsNone(error)
		self.assertEqual(rows, [{"commodity": "CHERRIES"}])

	def test_the_drafts_are_seeded_unpublished(self):
		from erpnext_mcp import phone_config

		from erpnext_mcp import install

		install._market_commodity_seed()  # what the migrate does: two published, the rest drafts
		made = [k for k in mp.DRAFT_SEEDS if phone_config.rows(mp.KIND, k)]
		self.assertEqual(set(made), set(mp.DRAFT_SEEDS))
		for key in mp.DRAFT_SEEDS:
			rows = phone_config.rows(mp.KIND, key)
			self.assertEqual([r["status"] for r in rows], ["Draft"], key)
		self.assertEqual(sorted(mp.commodities_published()), ["cantaloupe", "sweet_cherries"])


import contextlib  # noqa: E402


@contextlib.contextmanager
def mp_published(extra: dict):
	"""Treat extra config bodies as published for one test."""
	from unittest import mock

	real = mp.config

	def config(key):
		return {**extra[key], "_version": "test"} if key in extra else real(key)

	with mock.patch.object(mp, "config", side_effect=config):
		yield
