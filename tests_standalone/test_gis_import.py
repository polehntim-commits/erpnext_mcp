# SPDX-License-Identifier: MIT
"""The county lookup and the Desk save path — v0.33.0's two whitelisted methods.

These are the tests for a surface with NO transport gates, which is the same
thing `test_api_mobile.py` says about the phone API and is true here for a
different reason. `mcp.handle` runs `security.authorize()` — master switch,
shared token, CIDR allowlist — before it looks a tool up. A `@frappe.whitelist()`
method reached from a Desk form runs none of that. So the checks that used to be
somebody else's job are this module's, and each is asserted BY ITS ABSENCE
FIRST: the test that a gate works is a test that the call fails without it.

SIX CLAIMS.

1. **THE SURFACE IS TWO METHODS AND THREE DOCTYPES.** `TheSaveSurfaceIsClosed`.
   There is no dispatcher and no method-name argument, so `create_journal_entry`
   is not reachable and neither is Housing Unit — asserted by enumerating what
   the module exports rather than by trusting the docstring.

2. **PERMISSION IS CHECKED HERE, NOT INHERITED.** `TheGatesRefuseWhatTheyShould`.
   The boundary tools end in `doc.save(ignore_permissions=True)`, which is right
   for them and would have handed every signed-in account a write to every
   parcel if this wrapper had trusted it. Guest, and a user denied write on the
   specific document, are both refused.

3. **A TAX LOT IS AN ALLOWLIST, NOT AN ESCAPE.** `TheTaxLotIsValidated`. The
   value goes into an ArcGIS `where` clause evaluated by somebody else's server,
   so a quote, a space, a semicolon or an `OR 1=1` is refused rather than
   neutralised in a dialect this app does not implement.

4. **x IS LONGITUDE AND y IS LATITUDE.** `TheSpatialQueryIsBuiltRight`. Getting
   that pair round the wrong way asks about a point in the Southern Ocean, which
   comes back EMPTY rather than wrong — so nothing would ever have said what
   happened. It is pinned by reading the parameters the fetch was given.

5. **AN ArcGIS ERROR IS AN HTTP 200.** `WhatTheCountyCanSendBack`. The service
   answers `{"error": {...}}` with a 200 and a JSON content type, so a client
   that checks only the status reads a malformed query as "the county has never
   heard of your parcel". It is checked by name, first.

6. **SAVING GOES THROUGH THE BOUNDARY TOOLS, NOT ROUND THEM.**
   `SavingGoesThroughTheTools`. The area disagreement still refuses, the
   containment warnings still arrive, the derived fields are still recomputed —
   asserted against the stored document, because a wrapper that wrote
   `boundary_geojson` directly would pass every other test in this file.
"""

import inspect
import json
import pathlib
import re
import unittest

import frappe

from erpnext_mcp import geo
from erpnext_mcp.api import gis
from erpnext_mcp.errors import ToolError

from .fixtures import MAIN
from .harness import STORE
from .test_geo import BLOCK, PARCEL_OUTLINE, ZONE_INSIDE, GeoTestCase

#: The docnames `test_geo`'s own fixtures produce. Spelled out because
#: `save_boundary` takes a DOCNAME and not a friendly name — the map has the
#: record open, so it always knows the real one, and accepting anything looser
#: here would test a leniency the browser never needs.
PARCEL_DOCNAME = "Mill Creek - ETC"
FIELD_DOCNAME = "Yellow Camp Block 3 - MC"
ZONE_DOCNAME = "YC3-Zone2 - MC"

#: What Wasco County's FeatureServer actually answers with, trimmed to the keys
#: this app reads. Field names, casing AND SPELLING are the county's own — see
#: `COUNTIES["wasco"]["properties"]`, which is a list of spellings for exactly
#: this reason.
#:
#: `MapTaxlot` IS SPACE-DELIMITED AND UNPADDED, and this fixture said
#: `2N11E35BA-01600` until v0.126.0. That was the compact ORMAP spelling off a
#: deed, which is what a person types and NOT what the layer stores — so the
#: fixture agreed with the bug rather than with the county, and the suite was
#: green while no tax lot search could ever return a parcel. It was read off the
#: live layer this time: 15,516 rows, none padded, none hyphenated.
#:
#: `AccountNum` IS AN INTEGER, not a string. It is `esriFieldTypeInteger` on the
#: layer and arrives as a JSON number, which is the shape `_text` has to survive
#: for the account to reach the preview at all.
COUNTY_ANSWER = {
	"type": "FeatureCollection",
	"features": [
		{
			"type": "Feature",
			"properties": {
				"MapTaxlot": "1N 13E 7 200",
				"Taxpayer": "HIGHLAND LTD LIABILITY CO",
				"CalculatedAcres": 330.4,
				"AccountNum": 7503,
				"Shape__Area": 14389472.5,
			},
			"geometry": PARCEL_OUTLINE,
		}
	],
}

#: The compact spelling of the same lot, which is what is printed on the deed and
#: what is already sitting in `parcel_id` on parcels imported before v0.126.0.
#: `canonical_tax_lot` turns this into the fixture's `MapTaxlot` above.
COMPACT_SPELLING = "1N13E0700200"


class GISTestCase(GeoTestCase):
	"""A site with a parcel on it, and a fetch that never leaves the process."""

	def setUp(self):
		super().setUp()
		self.fetched = []
		self._fetch_before = gis._fetch
		self.addCleanup(self._restore_fetch)

	def _restore_fetch(self):
		gis._fetch = self._fetch_before

	def answer_with(self, payload):
		"""Replace the one outbound call with a recorder. Returns nothing."""

		def fake_fetch(url, params):
			self.fetched.append({"url": url, "params": dict(params)})
			if isinstance(payload, Exception):
				raise payload
			return payload

		gis._fetch = fake_fetch

	def as_user(self, user):
		frappe.local.session.user = user
		self.addCleanup(lambda: setattr(frappe.local.session, "user", "Administrator"))


# ── the closed surface ──────────────────────────────────────────────────────
class TheSaveSurfaceIsClosed(unittest.TestCase):
	"""Two methods, three doctypes, and no way to name a fourth of either."""

	def test_exactly_two_methods_are_whitelisted(self):
		exported = {
			name
			for name in dir(gis)
			if not name.startswith("_") and getattr(getattr(gis, name), "__wrapped_whitelisted__", False)
		}
		self.assertEqual(exported, {"query_county_parcels", "save_boundary"})

	def test_the_saveable_doctypes_are_the_three_that_carry_a_polygon(self):
		self.assertEqual(set(gis.SAVEABLE), {"Parcel", "Field", "Irrigation Zone"})

	def test_each_entry_names_a_real_tool_and_its_real_argument(self):
		"""A table of `(argument_name, function)` is only as good as the argument
		name, and a typo there is a `field is required` at the far end rather than
		an import error here."""
		expected = {
			"Parcel": "set_parcel_boundary",
			"Field": "set_field_boundary",
			"Irrigation Zone": "set_zone_boundary",
		}
		arguments = {"Parcel": "parcel", "Field": "field", "Irrigation Zone": "zone"}
		for doctype, (argument, tool) in gis.SAVEABLE.items():
			with self.subTest(doctype=doctype):
				self.assertEqual(tool.__name__, expected[doctype])
				self.assertEqual(argument, arguments[doctype])

	def test_the_only_hostname_is_the_countys(self):
		"""NOTHING HERE IS A GENERAL HTTP PROXY. If a URL could ever come from an
		argument, this file would be a way to make a farm's server fetch anything
		at all — so the hosts it can reach are a literal, and this is the test that
		notices one arriving from somewhere else."""
		for key, config in gis.COUNTIES.items():
			with self.subTest(county=key):
				self.assertTrue(config["url"].startswith("https://"))
				self.assertIn(".or.us/", config["url"])


# ── the map widget and the method it calls ──────────────────────────────────
class TheWidgetAndTheMethodAgree(unittest.TestCase):
	"""v0.126.0. A SEARCH BOX THAT SENDS AN ARGUMENT NOBODY READS.

	`query_county_parcels` is a `@frappe.whitelist()` method, and Frappe forwards
	the form dict to whatever names it declares. An argument the browser sends
	that the method does not name is a TypeError in the browser console; an
	argument the method grew that the browser never sends is a feature that
	shipped and does nothing. Neither is visible from either file alone, and
	neither is visible to any test that reads only Python — which is how the
	account box could have been added to the dialog and wired to nothing.

	READ OUT OF THE JAVASCRIPT, not asserted as a substring of it. A `assertIn`
	on the whole file passes on a mention in a comment; this pulls the keys out of
	the object literals actually passed to `query_county` and compares them with
	the method's real signature.
	"""

	WIDGET = (
		pathlib.Path(__file__).resolve().parent.parent / "erpnext_mcp" / "public" / "js" / "geo_map_widget.js"
	)

	def widget_source(self) -> str:
		self.assertTrue(self.WIDGET.exists(), f"{self.WIDGET} is gone")
		return self.WIDGET.read_text(encoding="utf-8")

	def arguments_the_browser_sends(self) -> set:
		"""Every key in every object literal handed to `query_county(...)`."""
		source = self.widget_source()
		sent = set()
		for match in re.finditer(r"\bquery_county\(", source):
			start = match.end()
			depth = 1
			index = start
			while index < len(source) and depth:
				depth += {"(": 1, ")": -1}.get(source[index], 0)
				index += 1
			sent.update(re.findall(r"\{[^{}]*?\b(\w+)\s*:", source[start : index - 1]))
			sent.update(re.findall(r"[,{]\s*(\w+)\s*:", source[start : index - 1]))
		return sent

	def test_the_browser_sends_nothing_the_method_does_not_name(self):
		accepted = set(inspect.signature(gis.query_county_parcels).parameters)
		self.assertTrue(self.arguments_the_browser_sends(), "no query_county call sites were found")
		self.assertLessEqual(self.arguments_the_browser_sends(), accepted)

	def test_every_way_of_asking_has_a_caller(self):
		"""The other direction: an argument the server grew and the dialog never
		offers is a feature nobody can reach. `county` is the exception and is
		deliberate — one county is configured, and the browser does not choose."""
		accepted = set(inspect.signature(gis.query_county_parcels).parameters) - {"county"}
		self.assertEqual(accepted, self.arguments_the_browser_sends())

	def test_the_dialog_offers_a_box_for_each_of_the_two_searches(self):
		"""v0.126.0 added the account box. A `fieldname` is what `values.account`
		in the primary action reads, so a box named anything else is a search that
		always sends an empty string."""
		source = self.widget_source()
		dialog = source[source.index("function open_county_import") :]
		dialog = dialog[: dialog.index("\n\tfunction ", 1)]
		for fieldname in ("tax_lot", "account"):
			with self.subTest(fieldname=fieldname):
				self.assertIn(f'fieldname: "{fieldname}"', dialog)

	def test_the_preview_prints_the_account_number_it_extracts(self):
		"""`feature.account` HAS BEEN EXTRACTED SINCE v0.33.0 AND SHOWN SINCE
		v0.126.0. It is the county's own key for the parcel and, from this
		release, the thing somebody types back in to find it again — a value
		parsed, carried across the wire and then dropped on the floor is the
		easiest kind of dead code to keep."""
		source = self.widget_source()
		describe = source[source.index("function describe_feature") :]
		describe = describe[: describe.index("\n\t}")]
		self.assertIn("feature.account", describe)

	def test_the_widget_no_longer_tells_people_the_spelling_that_never_matched(self):
		"""The dialog's own description asked for `2N11E35BA-01600` and nothing
		else, which is the compact ORMAP spelling the layer does not store. It is
		still accepted — it is what is on the deed — but the shape shown FIRST has
		to be the one the county holds."""
		source = self.widget_source()
		dialog = source[source.index("function open_county_import") :]
		dialog = dialog[: dialog.index("\n\tfunction ", 1)]
		self.assertIn("2N 11E 1 CC 4039", dialog)
		self.assertLess(dialog.index("2N 11E 1 CC 4039"), dialog.index("2N11E35BA-01600"))


# ── the gates ───────────────────────────────────────────────────────────────
class TheGatesRefuseWhatTheyShould(GISTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel(acreage=330.0)

	def test_guest_cannot_save_a_boundary(self):
		self.as_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			gis.save_boundary("Parcel", PARCEL_DOCNAME, json.dumps(PARCEL_OUTLINE))

	def test_guest_cannot_drive_the_county_lookup(self):
		"""A whitelisted method that makes an outbound request is not something to
		leave open to an unauthenticated caller, whatever it returns."""
		self.answer_with(COUNTY_ANSWER)
		self.as_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			gis.query_county_parcels(tax_lot="2N11E35BA-01600")
		self.assertEqual(self.fetched, [], "the county was asked before the caller was checked")

	def test_a_user_who_cannot_write_this_parcel_is_refused(self):
		"""THE CHECK THAT WOULD HAVE BEEN EASY TO SKIP. The boundary tools save
		with `ignore_permissions=True` — correct for them, since the MCP transport
		authorised three layers earlier — so a wrapper that trusted the framework
		would have handed every signed-in account on the site a write to every
		parcel on it."""
		STORE.denied_permissions.add(("Parcel", PARCEL_DOCNAME))
		self.addCleanup(STORE.denied_permissions.discard, ("Parcel", PARCEL_DOCNAME))
		with self.assertRaises(frappe.PermissionError):
			gis.save_boundary("Parcel", PARCEL_DOCNAME, json.dumps(PARCEL_OUTLINE))
		self.assertFalse(
			frappe.db.get_value("Parcel", PARCEL_DOCNAME, "boundary_geojson"),
			"the refusal came after the write",
		)

	def test_the_county_lookup_wants_write_on_Parcel_not_merely_read(self):
		"""The only thing an imported polygon is for is setting a parcel boundary.
		Gating on `read` would leave the site hosting an outbound fetch that a
		Family Member or an Advisor could drive."""
		self.answer_with(COUNTY_ANSWER)
		STORE.denied_permissions.add(("Parcel", "write"))
		self.addCleanup(STORE.denied_permissions.discard, ("Parcel", "write"))
		with self.assertRaises(frappe.PermissionError):
			gis.query_county_parcels(tax_lot="2N11E35BA-01600")

	def test_the_account_search_is_behind_the_same_two_gates(self):
		"""v0.126.0 ADDED AN ARGUMENT, NOT A DOOR. A new way to ask that skipped
		either gate would be a way for a Family Member to drive the farm's server
		at the county — so both are asserted against the new parameter rather than
		assumed to still hold because the old one is gated."""
		self.answer_with(COUNTY_ANSWER)
		self.as_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			gis.query_county_parcels(account="7503")
		frappe.local.session.user = "Administrator"
		STORE.denied_permissions.add(("Parcel", "write"))
		self.addCleanup(STORE.denied_permissions.discard, ("Parcel", "write"))
		with self.assertRaises(frappe.PermissionError):
			gis.query_county_parcels(account="7503")
		self.assertEqual(self.fetched, [], "the county was asked before the caller was checked")

	def test_an_unknown_doctype_is_refused_by_name(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			gis.save_boundary("Housing Unit", "HU-0001", json.dumps(PARCEL_OUTLINE))
		self.assertIn("Housing Unit", str(caught.exception))
		self.assertIn("Parcel", str(caught.exception))

	def test_a_record_that_does_not_exist_is_refused_before_anything_else(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			gis.save_boundary("Parcel", "No Such Parcel", json.dumps(PARCEL_OUTLINE))
		self.assertIn("No Such Parcel", str(caught.exception))


# ── the tax lot, and the where clause it lands in ───────────────────────────
class TheTaxLotIsTranslatedAndValidated(unittest.TestCase):
	"""THE COUNTY'S SPELLING IS NOT ANYBODY'S SPELLING, and until v0.126.0 this
	class asserted the wrong one.

	`MapTaxlot` on the live layer is space-delimited and unpadded —
	`2N 11E 1 CC 4039` — and v0.33.0 sent the compact ORMAP spelling off a deed,
	`MapTaxlot='2N11E35BA-01600'`. Those are never equal, so every tax lot search
	matched nothing, and an ArcGIS query that matches nothing is an HTTP 200 with
	an empty feature list: the form said the county had no such parcel and the
	suite agreed, because THE FIXTURE HAD THE SAME MISTAKE IN IT.

	The old allowlist refused a space, so the county's own spelling could not be
	typed in either. Both directions were shut and neither said so.

	STILL AN ALLOWLIST AND NOT AN ESCAPE, and now a tighter one: each part is
	matched against digits and a known letter before it is formatted, so the
	clause cannot carry a character no branch allowed.
	"""

	CONFIG = gis.COUNTIES["wasco"]

	def test_the_countys_own_spelling_goes_through_unchanged(self):
		"""The thing v0.33.0 refused outright, because it has spaces in it."""
		self.assertEqual(
			gis._tax_lot_clause(self.CONFIG, "2N 11E 1 CC 4039"),
			"MapTaxlot='2N 11E 1 CC 4039'",
		)

	def test_the_deeds_compact_spelling_is_translated_into_the_countys(self):
		"""THE FIX, IN ONE ASSERTION. `2N11E35BA-01600` is what is printed on the
		deed, what the form's own description asked for and what is already in
		`parcel_id` on every parcel imported before v0.126.0. It is not what the
		layer stores, and sending it verbatim is why nothing ever matched."""
		self.assertEqual(
			gis._tax_lot_clause(self.CONFIG, "2n11e35ba-01600"),
			"MapTaxlot='2N 11E 35 BA 1600'",
		)

	def test_the_padding_a_deed_carries_is_stripped_because_the_layer_has_none(self):
		"""Section `07` and lot `00200` are the same lot as `7` and `200`, and
		only one of the two spellings is on the server."""
		self.assertEqual(gis.canonical_tax_lot("1N 13E 07 0200"), "1N 13E 7 200")
		self.assertEqual(gis.canonical_tax_lot(COMPACT_SPELLING), "1N 13E 7 200")

	def test_a_lot_with_no_quarter_keeps_its_four_parts(self):
		"""Roughly a third of the county's lots have no quarter at all. A
		normaliser that inserted an empty one would produce a double space and
		match nothing."""
		self.assertEqual(gis.canonical_tax_lot("1N 11E 0 100"), "1N 11E 0 100")
		self.assertEqual(gis.canonical_tax_lot("1N11E0000100"), "1N 11E 0 100")

	def test_whatever_a_person_puts_between_the_parts_is_accepted(self):
		"""A space, a hyphen, a dot, a slash, several of each. They are separators
		and none of them is ever sent."""
		for spelling in (
			"2N 11E 1 CC 4039",
			"2N-11E-1-CC-4039",
			"2N.11E.1.CC.4039",
			"2N/11E/1/CC/4039",
			"  2n   11e   01   cc   04039  ",
		):
			with self.subTest(spelling=spelling):
				self.assertEqual(gis.canonical_tax_lot(spelling), "2N 11E 1 CC 4039")

	def test_a_run_together_lot_that_is_not_padded_is_refused_rather_than_guessed(self):
		"""`2N11E7200` IS GENUINELY AMBIGUOUS — section 7 lot 200, or section 72
		lot 00? The compact spelling only parses because the deed pads the section
		to two digits and the lot to five, and guessing at an unpadded one would
		import a real parcel somewhere else in the county."""
		with self.assertRaises(ToolError) as caught:
			gis.canonical_tax_lot("2N11E7200")
		self.assertIn("section 7 lot 200", str(caught.exception))

	def test_every_shape_that_is_not_a_tax_lot_is_refused(self):
		for value in (
			"2N11E35BA-01600' OR '1'='1",
			"'; DROP TABLE taxlots; --",
			"2N11E35BA%",
			'2N11"E',
			"",
			"   ",
			None,
			"-01600",
			"x" * 41,
			"2N 11E 1 CC",  # four parts short one
			"2N 11E 1 CC 4039 9",  # six
			"2X 11E 7 200",  # a township is N or S
			"2N 11Q 7 200",  # a range is E or W
			"2N 11E 123 200",  # sections stop at 36
			"2N 11E 7 123456",  # lots stop at five digits
			"2N 11E 7 CCCC 200",  # quarters are one or two letters
		):
			with self.subTest(value=value):
				with self.assertRaises(ToolError):
					gis._tax_lot_clause(self.CONFIG, value)

	def test_the_refusal_says_what_yes_looks_like(self):
		"""A person who has just been told no is looking at a form with a number
		on it that they believe is correct. The sentence has to show the shape
		that works and name the other way to search."""
		with self.assertRaises(ToolError) as caught:
			gis.canonical_tax_lot("not a lot")
		message = str(caught.exception)
		self.assertIn("2N 11E 1 CC 4039", message)
		self.assertIn("account number", message)

	def test_the_clause_never_contains_a_quote_it_did_not_write(self):
		"""Belt to the brace above: whatever survives validation, the clause it
		produces has exactly two apostrophes in it."""
		for spelling in ("2N11E35BA-01600", "2N 11E 1 CC 4039", "1N13E0700200"):
			with self.subTest(spelling=spelling):
				clause = gis._tax_lot_clause(self.CONFIG, spelling)
				self.assertEqual(clause.count("'"), 2)

	def test_no_clause_is_longer_than_the_column_it_is_compared_against(self):
		"""`MapTaxlot` is 25 characters on the layer. Every part is length-bounded
		by its own regex, so the longest clause this can build is well inside
		that — a value that could not possibly match is a value that should have
		been refused."""
		longest = gis.canonical_tax_lot("22N 11E 36 ABC 99999")
		self.assertLessEqual(len(longest), 25)


class TheAccountNumberIsAnInteger(unittest.TestCase):
	"""v0.126.0. THE SEARCH THAT CANNOT BE MISTYPED INTO A DIFFERENT FARM.

	A tax lot is five fields in a fixed order and a character wrong in any one of
	them returns a real parcel somewhere else, with plausible numbers on it. The
	assessor's account number is four digits off the top of a tax statement and
	the layer's own integer key.

	`AccountNum` IS `esriFieldTypeInteger`, so the clause carries NO QUOTES —
	which is a stronger guarantee than the tax lot's allowlist, because the value
	is formatted from an `int()` and there is no path from the caller's string to
	the string that is sent.
	"""

	CONFIG = gis.COUNTIES["wasco"]

	def test_an_account_number_becomes_an_unquoted_clause(self):
		self.assertEqual(gis._account_clause(self.CONFIG, "7503"), "AccountNum=7503")
		self.assertEqual(gis._account_clause(self.CONFIG, 7503), "AccountNum=7503")

	def test_the_clause_has_no_quotes_at_all(self):
		"""Quoting an integer column is the kind of thing an ArcGIS backend
		answers with a type error rather than a match."""
		self.assertNotIn("'", gis._account_clause(self.CONFIG, "7503"))
		self.assertNotIn('"', gis._account_clause(self.CONFIG, "7503"))

	def test_padding_and_surrounding_space_are_the_same_account(self):
		self.assertEqual(gis._account_clause(self.CONFIG, " 007503 "), "AccountNum=7503")

	def test_anything_that_is_not_digits_is_refused(self):
		for value in ("", "   ", None, "75-03", "abc", "7503 OR 1=1", "7503;--", "1" * 10, "7.5"):
			with self.subTest(value=value):
				with self.assertRaises(ToolError):
					gis._account_clause(self.CONFIG, value)

	def test_zero_is_refused_by_name_rather_than_read_as_empty(self):
		"""`str(x or "")` would turn a 0 into an empty string and report it as
		"which account?", which is a different and wronger sentence. The county
		issues no account 0, and that is what the refusal says."""
		with self.assertRaises(ToolError) as caught:
			gis._account_clause(self.CONFIG, 0)
		self.assertIn("start at 1", str(caught.exception))

	def test_a_hyphenated_number_is_pointed_at_the_other_box(self):
		"""Somebody with a tax lot in their hand who typed it into the wrong
		field needs to be told which field it belongs in."""
		with self.assertRaises(ToolError) as caught:
			gis._account_clause(self.CONFIG, "2N11E35BA-01600")
		self.assertIn("tax lot", str(caught.exception))


class TheCountyRegistry(unittest.TestCase):
	def test_no_county_named_means_wasco(self):
		key, config = gis._county(None)
		self.assertEqual(key, "wasco")
		self.assertEqual(config["label"], "Wasco County, Oregon")

	def test_the_way_a_person_writes_it_resolves(self):
		for spelling in ("Wasco", "wasco", "  WASCO  ", "Wasco County"):
			with self.subTest(spelling=spelling):
				self.assertEqual(gis._county(spelling)[0], "wasco")

	def test_an_unknown_county_says_which_ones_are_known(self):
		"""A county's parcel layer is a different server and a different schema
		for every county. Guessing at a URL is not a thing this can do, so the
		refusal has to say what it CAN do."""
		with self.assertRaises(ToolError) as caught:
			gis._county("Sherman")
		self.assertIn("Sherman", str(caught.exception))
		self.assertIn("wasco", str(caught.exception))


class DegreesAreDegrees(unittest.TestCase):
	def test_a_real_coordinate_passes(self):
		self.assertEqual(gis._degrees("45.6", "lat", 90.0), 45.6)
		self.assertEqual(gis._degrees(-121.18, "lon", 180.0), -121.18)

	def test_anything_off_the_globe_is_refused(self):
		for value, limit in ((91, 90.0), (-91, 90.0), (181, 180.0), (-181, 180.0)):
			with self.subTest(value=value):
				with self.assertRaises(ToolError):
					gis._degrees(value, "lat", limit)

	def test_a_word_is_refused_rather_than_coerced(self):
		for value in ("north", "", None, "NaN", "inf"):
			with self.subTest(value=value):
				with self.assertRaises(ToolError):
					gis._degrees(value, "lat", 90.0)


# ── what comes back off the wire ────────────────────────────────────────────
class WhatTheCountyCanSendBack(unittest.TestCase):
	CONFIG = gis.COUNTIES["wasco"]

	def test_a_normal_answer_becomes_this_apps_vocabulary(self):
		features, warnings = gis.parse_features(COUNTY_ANSWER, self.CONFIG)
		self.assertEqual(warnings, [])
		self.assertEqual(len(features), 1)
		one = features[0]
		self.assertEqual(one["tax_lot"], "1N 13E 7 200")
		self.assertEqual(one["taxpayer"], "HIGHLAND LTD LIABILITY CO")
		self.assertEqual(one["county_acres"], 330.4)
		self.assertEqual(one["geometry"]["type"], "Polygon")

	def test_an_integer_account_number_survives_into_the_preview(self):
		"""`AccountNum` IS AN INTEGER COLUMN and arrives as a JSON number. It is
		carried as text because the only thing the form does with it is print it
		next to the parcel — but a `_text` that dropped a non-string would have
		lost it silently, and from v0.126.0 it is also the value somebody types
		back in to find this parcel again."""
		one = gis.parse_features(COUNTY_ANSWER, self.CONFIG)[0][0]
		self.assertEqual(one["account"], "7503")

	@unittest.skipUnless(geo.available(), "needs shapely>=2.0 and h3>=4.0.0")
	def test_both_acreages_are_reported_and_neither_replaces_the_other(self):
		"""TWO MEASUREMENTS, NOT ONE FACT. The county's figure is computed on its
		own projected grid and this app's is spherical; they agree to a fraction
		of a percent when the import is right, and a reader who can see both can
		tell a projection difference from the wrong parcel."""
		one = gis.parse_features(COUNTY_ANSWER, self.CONFIG)[0][0]
		self.assertIsNotNone(one["area_computed_acres"])
		self.assertAlmostEqual(one["area_computed_acres"], one["county_acres"], delta=5.0)

	def test_an_arcgis_error_is_an_http_200_and_is_caught_by_name(self):
		"""THE FAILURE THAT LOOKS LIKE AN EMPTY RESULT. A malformed query answers
		200 with `{"error": …}` and no `features` key at all, so a reader that
		checked only the status would tell somebody the county has never heard of
		their parcel."""
		with self.assertRaises(ToolError) as caught:
			gis.parse_features(
				{
					"error": {
						"code": 400,
						"message": "Unable to complete operation.",
						"details": ["bad where"],
					}
				},
				self.CONFIG,
			)
		self.assertIn("Unable to complete operation", str(caught.exception))
		self.assertIn("bad where", str(caught.exception))

	def test_something_that_is_not_a_feature_collection_is_refused(self):
		for payload in ({}, {"type": "Feature"}, {"features": "lots"}):
			with self.subTest(payload=payload):
				with self.assertRaises(ToolError):
					gis.parse_features(payload, self.CONFIG)

	def test_a_shape_that_is_not_an_area_is_dropped_and_said_so(self):
		"""Some parcel layers carry annotation geometry beside the lots. A
		boundary is an area, the three boundary tools refuse anything else, and
		handing the form a shape it will only be refused for later is worse than
		saying so here."""
		payload = {
			"type": "FeatureCollection",
			"features": [
				{
					"properties": {"MapTaxlot": "A"},
					"geometry": {"type": "Point", "coordinates": [-121.18, 45.6]},
				},
				COUNTY_ANSWER["features"][0],
			],
		}
		features, warnings = gis.parse_features(payload, self.CONFIG)
		self.assertEqual(len(features), 1)
		self.assertEqual(features[0]["tax_lot"], "1N 13E 7 200")
		self.assertTrue(any("not areas" in line for line in warnings))

	def test_a_field_name_in_another_case_is_still_found(self):
		"""An ArcGIS layer's field names are whatever the person who published it
		typed, and a county that re-publishes in upper case must not silently
		return a parcel with no tax lot number on it."""
		payload = {
			"type": "FeatureCollection",
			"features": [
				{
					"properties": {"MAPTAXLOT": "1N 13E 7 200", "CALCULATEDACRES": 12.5},
					"geometry": PARCEL_OUTLINE,
				}
			],
		}
		one = gis.parse_features(payload, self.CONFIG)[0][0]
		self.assertEqual(one["tax_lot"], "1N 13E 7 200")
		self.assertEqual(one["county_acres"], 12.5)

	def test_the_esri_json_spelling_of_properties_is_read_too(self):
		"""`f=geojson` gives `properties`; `f=json` gives `attributes`. This app
		asks for the first, and reading both costs one line against the day a
		county's server ignores the parameter."""
		payload = {
			"type": "FeatureCollection",
			"features": [{"attributes": {"MapTaxlot": "1N 13E 7 200"}, "geometry": PARCEL_OUTLINE}],
		}
		self.assertEqual(gis.parse_features(payload, self.CONFIG)[0][0]["tax_lot"], "1N 13E 7 200")

	def test_a_flood_of_matches_is_capped_and_the_cap_is_reported(self):
		"""A SILENT TRUNCATION READS AS 'that is all there was'. If the answer is
		cut, the person choosing between the shapes has to be told."""
		payload = {
			"type": "FeatureCollection",
			"features": [COUNTY_ANSWER["features"][0]] * (gis._MAX_FEATURES + 5),
		}
		features, warnings = gis.parse_features(payload, self.CONFIG)
		self.assertEqual(len(features), gis._MAX_FEATURES)
		self.assertTrue(any(str(gis._MAX_FEATURES) in line for line in warnings))

	def test_an_acreage_that_is_not_a_number_is_dropped_rather_than_guessed(self):
		payload = {
			"type": "FeatureCollection",
			"features": [
				{"properties": {"MapTaxlot": "A", "CalculatedAcres": "unknown"}, "geometry": PARCEL_OUTLINE}
			],
		}
		self.assertIsNone(gis.parse_features(payload, self.CONFIG)[0][0]["county_acres"])


# ── the query, end to end, with the wire replaced ───────────────────────────
class TheSpatialQueryIsBuiltRight(GISTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel(acreage=330.0)
		self.answer_with(COUNTY_ANSWER)

	def test_a_tax_lot_query_asks_for_geojson_in_wgs84(self):
		"""`outSR=4326` IS THE WHOLE INTEGRATION. The layer stores Oregon
		Stateplane North in FEET (WKID 2913), and a polygon in feet parses as
		perfectly valid GeoJSON whose coordinates are somewhere near longitude
		7,600,000 — which `check_coordinates_look_like_degrees` would flag and
		nothing would fix."""
		gis.query_county_parcels(tax_lot="2N11E35BA-01600")
		params = self.fetched[0]["params"]
		self.assertEqual(params["where"], "MapTaxlot='2N 11E 35 BA 1600'")
		self.assertEqual(params["outSR"], 4326)
		self.assertEqual(params["f"], "geojson")
		self.assertEqual(params["outFields"], "*")

	def test_x_is_longitude_and_y_is_latitude(self):
		"""THE MISTAKE THAT COMES BACK EMPTY RATHER THAN WRONG. Swapping them asks
		about 45.6°E, -121.18°N — a point in the Southern Ocean — so the answer is
		"no parcel here" and nothing ever says what actually happened."""
		gis.query_county_parcels(lat=45.6015, lon=-121.178)
		params = self.fetched[0]["params"]
		self.assertEqual(json.loads(params["geometry"]), {"x": -121.178, "y": 45.6015})
		self.assertEqual(params["geometryType"], "esriGeometryPoint")
		self.assertEqual(params["inSR"], 4326)
		self.assertEqual(params["spatialRel"], "esriSpatialRelIntersects")
		self.assertNotIn("where", params)

	def test_an_account_query_asks_the_integer_column_without_quotes(self):
		"""v0.126.0. `AccountNum` is `esriFieldTypeInteger`, and this is the search
		an operator can run off a tax statement without transcribing five fields
		in the right order."""
		gis.query_county_parcels(account="7503")
		params = self.fetched[0]["params"]
		self.assertEqual(params["where"], "AccountNum=7503")
		self.assertEqual(params["outSR"], 4326)
		self.assertEqual(params["f"], "geojson")
		self.assertNotIn("geometry", params)

	def test_the_url_comes_from_the_registry_and_not_from_an_argument(self):
		gis.query_county_parcels(tax_lot="2N11E35BA-01600")
		self.assertEqual(self.fetched[0]["url"], gis.COUNTIES["wasco"]["url"])

	def test_only_one_of_the_three_ways_to_ask_per_call(self):
		"""Three different questions. Answering two together would hide which one
		matched, and the whole value of the preview is knowing what you asked."""
		for kwargs in (
			{"tax_lot": "2N11E35BA-01600", "lat": 45.6, "lon": -121.18},
			{"account": "7503", "lat": 45.6, "lon": -121.18},
			{"tax_lot": "2N11E35BA-01600", "account": "7503"},
			{"tax_lot": "2N11E35BA-01600", "account": "7503", "lat": 45.6, "lon": -121.18},
		):
			with self.subTest(**kwargs):
				with self.assertRaises(frappe.ValidationError):
					gis.query_county_parcels(**kwargs)
				self.assertEqual(self.fetched, [])

	def test_none_of_the_three_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			gis.query_county_parcels()
		self.assertEqual(self.fetched, [])

	def test_a_lat_with_no_lon_is_not_half_a_query(self):
		with self.assertRaises(frappe.ValidationError):
			gis.query_county_parcels(lat=45.6)

	def test_nothing_found_is_an_answer_and_not_an_error(self):
		"""A tax lot that is not on the roll is an ordinary outcome — the number
		was mistyped, or the parcel is in the next county. It gets a sentence, not
		a traceback."""
		self.answer_with({"type": "FeatureCollection", "features": []})
		result = gis.query_county_parcels(tax_lot="2N11E35BA-99999")
		self.assertEqual(result["count"], 0)
		self.assertEqual(result["features"], [])
		self.assertTrue(any("no parcel matching" in line for line in result["warnings"]))

	def test_an_empty_answer_names_the_way_it_was_asked(self):
		""" "No parcel matching that point" and "no parcel matching that account
		number" send somebody to look in two different places. Before v0.126.0 the
		sentence said "tax lot number" or "point" and there was no third."""
		self.answer_with({"type": "FeatureCollection", "features": []})
		for kwargs, expected in (
			({"tax_lot": "2N11E35BA-99999"}, "tax lot number"),
			({"account": "999999"}, "account number"),
			({"lat": 45.6, "lon": -121.18}, "point"),
		):
			with self.subTest(**kwargs):
				result = gis.query_county_parcels(**kwargs)
				self.assertTrue(
					any(f"matching that {expected}" in line for line in result["warnings"]),
					result["warnings"],
				)

	def test_the_answer_names_the_county_it_came_from(self):
		result = gis.query_county_parcels(tax_lot="2N11E35BA-01600")
		self.assertEqual(result["county"], "wasco")
		self.assertEqual(result["label"], "Wasco County, Oregon")

	def test_the_answer_reports_the_spelling_the_county_was_actually_asked(self):
		"""NOT WHAT WAS TYPED. A person who typed the deed's spelling and got a
		parcel back wants to see the county's, because that is the string that
		matched and the one to search with next time."""
		result = gis.query_county_parcels(tax_lot="2N11E35BA-01600")
		self.assertEqual(result["query"], {"tax_lot": "2N 11E 35 BA 1600"})

	def test_an_account_answer_reports_the_number_as_a_number(self):
		result = gis.query_county_parcels(account=" 07503 ")
		self.assertEqual(result["query"], {"account": 7503})

	def test_a_lookup_leaves_an_audit_row(self):
		"""An outbound request made by the farm's server on somebody's behalf is
		exactly the kind of thing an operator later wants a record of."""
		gis.query_county_parcels(tax_lot="2N11E35BA-01600")
		rows = [
			row for row in STORE.rows("MCP Action Log") if row.get("tool_name") == "desk:query_county_parcels"
		]
		self.assertEqual(len(rows), 1)
		self.assertIn("2N 11E 35 BA 1600", rows[0]["arguments_json"])

	def test_an_account_lookup_leaves_an_audit_row_too(self):
		gis.query_county_parcels(account="7503")
		rows = [
			row for row in STORE.rows("MCP Action Log") if row.get("tool_name") == "desk:query_county_parcels"
		]
		self.assertEqual(len(rows), 1)
		self.assertIn("7503", rows[0]["arguments_json"])


# ── saving, which is the part that must not be a shortcut ───────────────────
class SavingGoesThroughTheTools(GISTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel(acreage=330.0)

	def test_a_parcel_boundary_is_stored_with_everything_derived(self):
		"""A WRAPPER THAT WROTE THE FIELD DIRECTLY WOULD PASS EVERY OTHER TEST IN
		THIS FILE. This one reads the document afterwards: the centroid, the
		bounding box, the H3 coverage and the computed acreage are all functions
		of the polygon, and only the boundary tool produces them."""
		result = gis.save_boundary("Parcel", PARCEL_DOCNAME, json.dumps(PARCEL_OUTLINE))
		self.assertTrue(result["changed"])
		row = frappe.db.get_value(
			"Parcel",
			PARCEL_DOCNAME,
			[
				"boundary_geojson",
				"boundary_centroid_lat",
				"boundary_centroid_lon",
				"boundary_bbox_geojson",
				"h3_cells",
				"area_computed_acres",
			],
			as_dict=True,
		)
		self.assertEqual(json.loads(row["boundary_geojson"])["type"], "Polygon")
		self.assertAlmostEqual(row["boundary_centroid_lat"], 45.6005, places=2)
		self.assertAlmostEqual(row["boundary_centroid_lon"], -121.178, places=2)
		self.assertTrue(row["boundary_bbox_geojson"])
		self.assertTrue(json.loads(row["h3_cells"]))
		self.assertGreater(row["area_computed_acres"], 0)

	def test_a_geometry_dict_and_a_geojson_string_are_the_same_call(self):
		"""`frappe.call` hands JSON through as a parsed object when the browser
		sent one and as a string when it sent a string. `geo.parse` takes both,
		and this is what would notice a wrapper that stringified once too often."""
		gis.save_boundary("Parcel", PARCEL_DOCNAME, PARCEL_OUTLINE)
		self.assertTrue(frappe.db.get_value("Parcel", PARCEL_DOCNAME, "boundary_geojson"))

	def test_an_area_that_disagrees_with_the_acreage_is_still_refused(self):
		"""THE CHECK THE MAP MUST NOT BE A WAY ROUND. A parcel recorded at 330
		acres and a polygon enclosing four is one of the two figures being about a
		different piece of ground — usually the wrong tax lot was imported — and
		the tool refuses it. The Desk path inherits that, unchanged."""
		with self.assertRaises(frappe.ValidationError) as caught:
			gis.save_boundary("Parcel", PARCEL_DOCNAME, json.dumps(BLOCK))
		self.assertIn("330", str(caught.exception))
		self.assertFalse(frappe.db.get_value("Parcel", PARCEL_DOCNAME, "boundary_geojson"))

	def test_a_self_intersecting_polygon_is_refused(self):
		bowtie = {
			"type": "Polygon",
			"coordinates": [
				[
					[-121.1850, 45.5950],
					[-121.1710, 45.6060],
					[-121.1710, 45.5950],
					[-121.1850, 45.6060],
					[-121.1850, 45.5950],
				]
			],
		}
		with self.assertRaises(frappe.ValidationError):
			gis.save_boundary("Parcel", PARCEL_DOCNAME, json.dumps(bowtie))

	def test_a_dry_run_changes_nothing_and_says_what_it_would_do(self):
		result = gis.save_boundary("Parcel", PARCEL_DOCNAME, json.dumps(PARCEL_OUTLINE), dry_run=1)
		self.assertTrue(result["dry_run"])
		self.assertFalse(result["changed"])
		self.assertFalse(frappe.db.get_value("Parcel", PARCEL_DOCNAME, "boundary_geojson"))

	def test_the_warnings_reach_the_form_rather_than_the_log(self):
		"""Every warning the boundary tools emit is a thing somebody has to decide
		about — a block that now hangs outside its parcel, three zones left
		outside the shape. A wrapper that dropped them would turn a decision into
		a silence."""
		self.a_field(acreage=25.7)
		self.tool_data(
			"set_field_boundary",
			{"field": "Yellow Camp Block 3", "boundary_geojson": json.dumps(BLOCK)},
		)
		# A parcel outline shifted a long way east: the block it carries is now
		# nowhere near it, which is exactly what the containment check reports.
		elsewhere = {
			"type": "Polygon",
			"coordinates": [[[lon + 0.05, lat] for lon, lat in PARCEL_OUTLINE["coordinates"][0]]],
		}
		result = gis.save_boundary("Parcel", PARCEL_DOCNAME, json.dumps(elsewhere))
		self.assertTrue(result["warnings"])
		self.assertTrue(any("Yellow Camp Block 3" in line for line in result["warnings"]))

	def test_a_block_is_saved_through_its_own_tool(self):
		self.map_parcel()
		self.a_field(acreage=25.7)
		result = gis.save_boundary("Field", FIELD_DOCNAME, json.dumps(BLOCK))
		self.assertTrue(result["changed"])
		self.assertTrue(frappe.db.get_value("Field", FIELD_DOCNAME, "boundary_geojson"))

	def test_a_zone_is_saved_through_its_own_tool(self):
		self.map_parcel()
		self.a_field(acreage=25.7)
		self.tool_data(
			"set_field_boundary",
			{"field": "Yellow Camp Block 3", "boundary_geojson": json.dumps(BLOCK)},
		)
		self.a_zone(area_sq_ft=int(geo.area_acres(ZONE_INSIDE) * 43560))
		result = gis.save_boundary("Irrigation Zone", ZONE_DOCNAME, json.dumps(ZONE_INSIDE))
		self.assertTrue(result["changed"])
		self.assertTrue(frappe.db.get_value("Irrigation Zone", ZONE_DOCNAME, "boundary_geojson"))

	def test_the_company_comes_off_the_record_and_is_never_asked_for(self):
		"""THE FIXTURE IS A TWO-COMPANY SITE ON PURPOSE, and the boundary tools
		refuse to guess a company on one. A person who has the form open should
		never be asked which of their companies the parcel they are looking at is
		on — so the wrapper reads `owning_entity` off the record."""
		self.assertGreater(len(frappe.db.get_all("Company")), 1)
		gis.save_boundary("Parcel", PARCEL_DOCNAME, json.dumps(PARCEL_OUTLINE))
		self.assertEqual(frappe.db.get_value("Parcel", PARCEL_DOCNAME, "owning_entity"), MAIN)

	def test_a_save_leaves_an_audit_row_naming_the_tool_that_ran(self):
		gis.save_boundary("Parcel", PARCEL_DOCNAME, json.dumps(PARCEL_OUTLINE))
		rows = [
			row for row in STORE.rows("MCP Action Log") if row.get("tool_name") == "desk:set_parcel_boundary"
		]
		self.assertEqual(len(rows), 1)
		self.assertIn(PARCEL_DOCNAME, rows[0]["arguments_json"])


class TheMcpSwitchesAreNotTheDeskGate(GISTestCase):
	"""`allow_set_parcel_boundary` is the MODEL's leash, and reading it here would
	mean an operator who distrusts the AI also loses the ability to trace a parcel
	by hand — which is not what the switch says and not what they asked for.

	This is the same call `api/__init__.py` made for the phone, asserted rather
	than described because it is the kind of decision a later refactor "fixes".
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_set_parcel_boundary=0, allow_create_parcel=1)
		self.a_parcel(acreage=330.0)

	def test_a_person_can_draw_a_boundary_the_model_may_not_set(self):
		self.assertIn(
			"switched off on this site",
			self.tool_error(
				"set_parcel_boundary",
				{"parcel": "Mill Creek", "boundary_geojson": json.dumps(PARCEL_OUTLINE)},
			),
		)
		result = gis.save_boundary("Parcel", PARCEL_DOCNAME, json.dumps(PARCEL_OUTLINE))
		self.assertTrue(result["changed"])
