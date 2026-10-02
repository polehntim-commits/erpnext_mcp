# SPDX-License-Identifier: MIT
"""Universal asset tags, from the Desk. v0.83.0.

`generate_asset_qr` and `generate_asset_qr_sheet` have drawn the symbols since
v0.17.0 and there has never been a way to get one onto paper without going
through an MCP client. This release is the two buttons and the page they print.

SEVEN CLAIMS.

1. `TheGeometry` — the label grid is measured from the manufacturer's own
   template and the arithmetic is checked against `TEMPLATES` rather than
   asserted as magic numbers. A template name this app has never heard of falls
   back and SAYS SO on the page, rather than throwing at somebody holding a roll
   of labels.
2. `ThePagination` — thirty-one tags are two sheets, and the last one does not
   force a blank.
3. `TheTagMarkup` — every value a person typed is escaped, the docname is printed
   as text as well as encoded in the symbol, and a tag with no symbol still
   prints its docname. That last one is the whole outdoor-durability argument in
   `asset_tag_sheet`'s docstring.
4. `TheWholeSheet` — the skipped assets are ON THE PAGE, which is the sheet's
   half of "one bad docname does not lose the run".
5. `TheEndpoints` — the two whitelisted methods are gated on the SESSION's read
   permission per record, refuse politely, and delegate to the tools rather than
   reimplementing them. The permission gate is the one thing a Desk route adds
   that the MCP tool does not have.
6. `TheFormAction` and `TheListAction` — both buttons are RECORDS and not hooks,
   created only when absent, an operator's edit survives every future migrate,
   and `before_uninstall` takes them away again.
7. `TheScriptsAndTheServerAgree` — the dotted path each script calls is a
   function that actually exists and is whitelisted. A button calling a method
   nobody wrote is the failure mode that no other test in this file would catch.
"""

import unittest

import frappe

from erpnext_mcp import asset_tag_form_action, asset_tag_list_action, asset_tag_sheet

from .fixtures import MAIN, V12TestCase
from .harness import STORE

VALVE = "MC-Valve-05"

ALL_ON = {
	"allow_register_asset": 1,
	"allow_generate_asset_qr": 1,
	"allow_generate_asset_qr_sheet": 1,
}


def _tag(name=VALVE, **overrides) -> dict:
	"""One tag in the shape `generate_asset_qr_sheet` returns."""
	base = {
		"asset_name": name,
		"asset_type": "Irrigation Valve",
		"location": "MC-Block-A",
		# A one-pixel PNG is enough: nothing here decodes it, and a real symbol
		# would put six kilobytes of base64 in a test file for no claim.
		"png_base64": "iVBORw0KGgo=",
	}
	base.update(overrides)
	return base


# ── 1 ───────────────────────────────────────────────────────────────────────
class TheGeometry(unittest.TestCase):
	"""PURE FUNCTIONS, NO SITE. `asset_tag_sheet`'s layout half never touches
	`frappe.db`, which is what lets these run without standing a fixture up."""

	def test_avery_5160_is_thirty_to_a_sheet(self):
		spec = asset_tag_sheet.template_spec("avery_5160")
		self.assertEqual(asset_tag_sheet.labels_per_page(spec), 30)

	def test_the_grid_fits_inside_letter(self):
		"""DERIVED, NOT ASSERTED. A margin somebody widens has to keep the last
		column on the page, and this is what notices when it does not."""
		for key in asset_tag_sheet.TEMPLATES:
			spec = asset_tag_sheet.template_spec(key)
			with self.subTest(template=key):
				right = spec["margin_left_mm"] + (spec["across"] - 1) * spec["pitch_x_mm"] + spec["width_mm"]
				bottom = spec["margin_top_mm"] + (spec["down"] - 1) * spec["pitch_y_mm"] + spec["height_mm"]
				self.assertLessEqual(right, asset_tag_sheet.PAGE["width_mm"])
				self.assertLessEqual(bottom, asset_tag_sheet.PAGE["height_mm"])

	def test_the_pitch_is_never_narrower_than_the_label(self):
		"""Centre-to-centre smaller than the label itself means labels that
		overlap, which prints as a smear rather than as an error."""
		for key in asset_tag_sheet.TEMPLATES:
			spec = asset_tag_sheet.template_spec(key)
			with self.subTest(template=key):
				self.assertGreaterEqual(spec["pitch_x_mm"], spec["width_mm"])
				self.assertGreaterEqual(spec["pitch_y_mm"], spec["height_mm"])

	def test_an_unknown_template_falls_back_rather_than_throwing(self):
		"""The tool takes `template` as free text and always has. Somebody with a
		roll of labels in the printer needs paper, not a dialog."""
		spec = asset_tag_sheet.template_spec("avery_9999")
		self.assertEqual(spec["key"], asset_tag_sheet.DEFAULT_TEMPLATE)
		self.assertTrue(spec["substituted"])

	def test_the_substitution_is_stated_on_the_page(self):
		"""A silent substitution is thirty labels printed on the wrong stock."""
		html = asset_tag_sheet.sheet_html([_tag()], [], "avery_9999")
		self.assertIn("avery_9999", html)
		self.assertIn("Check the stock in the printer", html)

	def test_a_known_template_is_not_reported_as_substituted(self):
		spec = asset_tag_sheet.template_spec("avery_5163")
		self.assertFalse(spec["substituted"])
		self.assertEqual(spec["key"], "avery_5163")


# ── 2 ───────────────────────────────────────────────────────────────────────
class ThePagination(unittest.TestCase):
	def setUp(self):
		self.spec = asset_tag_sheet.template_spec("avery_5160")

	def test_thirty_tags_are_one_sheet(self):
		self.assertEqual(len(asset_tag_sheet.paginate([_tag()] * 30, self.spec)), 1)

	def test_thirty_one_tags_are_two(self):
		pages = asset_tag_sheet.paginate([_tag()] * 31, self.spec)
		self.assertEqual(len(pages), 2)
		self.assertEqual(len(pages[1]), 1)

	def test_nothing_in_is_nothing_out(self):
		"""And NOT one blank page, which is what a naive range() produces."""
		self.assertEqual(asset_tag_sheet.paginate([], self.spec), [])

	def test_every_tag_survives_the_split(self):
		pages = asset_tag_sheet.paginate([_tag(name=f"A-{i}") for i in range(45)], self.spec)
		flat = [tag["asset_name"] for page in pages for tag in page]
		self.assertEqual(flat, [f"A-{i}" for i in range(45)])


# ── 3 ───────────────────────────────────────────────────────────────────────
class TheTagMarkup(unittest.TestCase):
	def setUp(self):
		self.spec = asset_tag_sheet.template_spec("avery_5160")

	def test_the_docname_is_printed_as_text(self):
		"""THE OUTDOOR ARGUMENT. A symbol that no longer decodes on a label still
		firmly attached to a pump degrades into something somebody can type."""
		html = asset_tag_sheet.tag_html(_tag(), self.spec, 0)
		self.assertIn(VALVE, html)

	def test_a_tag_with_no_symbol_still_prints_its_docname(self):
		html = asset_tag_sheet.tag_html(_tag(png_base64=""), self.spec, 0)
		self.assertIn(VALVE, html)
		self.assertIn("tag-nosymbol", html)
		self.assertNotIn("<img", html)

	def test_the_symbol_is_embedded_rather_than_fetched(self):
		"""A `/files/…` URL would need a second authenticated request from a tab
		showing a blob: document, which is the bug the badge sheet had to fix."""
		html = asset_tag_sheet.tag_html(_tag(), self.spec, 0)
		self.assertIn("data:image/png;base64,iVBORw0KGgo=", html)

	def test_every_value_is_escaped(self):
		html = asset_tag_sheet.tag_html(_tag(name='Pump "A" <script>', location="Block & Co"), self.spec, 0)
		self.assertNotIn("<script>", html)
		self.assertIn("&lt;script&gt;", html)
		self.assertIn("&amp;", html)

	def test_the_slot_moves_across_then_down(self):
		"""Index is the position WITHIN THE PAGE and the grid fills left to right.
		A layout that filled columns first would print a sheet nobody can read
		against the register."""
		first = asset_tag_sheet.tag_html(_tag(), self.spec, 0)
		second = asset_tag_sheet.tag_html(_tag(), self.spec, 1)
		fourth = asset_tag_sheet.tag_html(_tag(), self.spec, 3)
		self.assertIn(f"left:{round(self.spec['margin_left_mm'], 3)}mm", first)
		self.assertIn(f"top:{round(self.spec['margin_top_mm'], 3)}mm", first)
		# Second is one pitch to the RIGHT, same row.
		self.assertIn(f"top:{round(self.spec['margin_top_mm'], 3)}mm", second)
		self.assertNotIn(f"left:{round(self.spec['margin_left_mm'], 3)}mm", second)
		# Fourth wraps to the next row, back at the left margin.
		self.assertIn(f"left:{round(self.spec['margin_left_mm'], 3)}mm", fourth)
		self.assertIn(f"top:{round(self.spec['margin_top_mm'] + self.spec['pitch_y_mm'], 3)}mm", fourth)


# ── 4 ───────────────────────────────────────────────────────────────────────
class TheWholeSheet(unittest.TestCase):
	def test_the_skipped_assets_are_on_the_page(self):
		"""THE SHEET'S HALF OF THE PROMISE. Thirty assets that quietly came back
		as twenty-eight is two machines that stay untagged for a season."""
		html = asset_tag_sheet.sheet_html([_tag()], [{"asset_name": "MC-Pump-09", "error": "not found"}])
		self.assertIn("MC-Pump-09", html)
		self.assertIn("not found", html)
		self.assertIn("1 skipped", html)

	def test_an_error_message_is_escaped_too(self):
		html = asset_tag_sheet.sheet_html([], [{"asset_name": "<b>x</b>", "error": "&"}])
		self.assertNotIn("<b>x</b>", html)
		self.assertIn("&lt;b&gt;", html)

	def test_an_empty_run_says_so_rather_than_printing_a_blank_page(self):
		html = asset_tag_sheet.sheet_html([], [])
		self.assertIn("No asset tags were produced", html)

	def test_the_print_button_does_not_print_itself(self):
		html = asset_tag_sheet.sheet_html([_tag()], [])
		self.assertIn("no-print", html)
		self.assertIn("window.print()", html)

	def test_it_warns_against_fit_to_page(self):
		"""Every label lands off its die cut if the browser rescales the grid, and
		that is the single commonest way a label sheet comes out wrong."""
		self.assertIn("100%", asset_tag_sheet.sheet_html([_tag()], []))


# ── 5 ───────────────────────────────────────────────────────────────────────
class TheEndpoints(V12TestCase):
	def setUp(self):
		super().setUp()
		# v0.213.0. These pin the LEGACY layout, which still draws for a site that
		# sets `legacy_badge_layouts`; the CR80 redirect is `test_card_redirect`.
		from unittest import mock

		from erpnext_mcp import card_redirect

		patcher = mock.patch.object(card_redirect, "legacy", return_value=True)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.configure(enabled=1, **ALL_ON)
		self.tool_data("register_asset", {"name": VALVE, "asset_type": "Irrigation Valve", "company": MAIN})

	def tearDown(self):
		frappe.has_permission = self._real_has_permission
		super().tearDown()

	@property
	def _real_has_permission(self):
		return getattr(self, "_saved_has_permission", frappe.has_permission)

	def deny(self, *denied):
		"""Make `has_permission` refuse the named records, as a User Permission does."""
		self._saved_has_permission = frappe.has_permission
		original = frappe.has_permission

		def gate(doctype, ptype=None, doc=None, throw=False, **kwargs):
			if doc is not None and str(doc) in denied:
				if throw:
					raise frappe.PermissionError(f"no access to {doc}")
				return False
			return original(doctype, ptype, doc=doc, throw=throw, **kwargs)

		frappe.has_permission = gate

	# -- the form endpoint --------------------------------------------------
	def test_it_returns_the_symbol_and_a_printable_label(self):
		from erpnext_mcp.api import asset_tags as api

		answer = api.asset_qr_tag(asset_name=VALVE)
		self.assertEqual(answer["asset_name"], VALVE)
		self.assertTrue(answer["png_data_uri"].startswith("data:image/png;base64,"))
		self.assertIn("<!doctype html>", answer["html"])
		self.assertIn(VALVE, answer["html"])

	def test_the_label_it_returns_is_the_sheet_layout(self):
		"""ONE LAYOUT, NOT TWO. A label drawn separately for the single-asset
		button would drift from the sheet's, and the drift prints as tags inside
		the die cut from one button and across the perforation from the other."""
		from erpnext_mcp.api import asset_tags as api

		answer = api.asset_qr_tag(asset_name=VALVE)
		self.assertIn("tag-cell", answer["html"])
		self.assertIn("tag-page", answer["html"])

	def test_it_carries_the_register_row_not_just_the_docname(self):
		from erpnext_mcp.api import asset_tags as api

		answer = api.asset_qr_tag(asset_name=VALVE)
		self.assertEqual(answer["asset_type"], "Irrigation Valve")
		self.assertIn(f"/scan/{VALVE}", answer["qr_url"])

	def test_an_unnamed_asset_is_refused_politely(self):
		from erpnext_mcp.api import asset_tags as api

		with self.assertRaises(frappe.ValidationError):
			api.asset_qr_tag(asset_name="")

	def test_a_docname_that_is_not_there_is_a_sentence_and_not_a_500(self):
		"""`ToolError` becomes a modal. Raised out of a whitelisted method it
		would be an HTTP 500 and the sentence would never reach anybody."""
		from erpnext_mcp.api import asset_tags as api

		with self.assertRaises(frappe.ValidationError) as caught:
			api.asset_qr_tag(asset_name="MC-Nothing-99")
		self.assertIn("MC-Nothing-99", str(caught.exception))

	def test_the_form_endpoint_is_gated_on_the_specific_record(self):
		"""THE ONE THING A DESK ROUTE ADDS. The MCP tool is gated by the
		`allow_<tool>` switch and a token; this has a session, so a User
		Permission scoping somebody to one company has to scope the button."""
		from erpnext_mcp.api import asset_tags as api

		self.deny(VALVE)
		with self.assertRaises(frappe.PermissionError):
			api.asset_qr_tag(asset_name=VALVE)

	def test_printing_a_tag_writes_nothing(self):
		"""Printing a label is not a sighting of the machine — `scan_asset` stamps
		`last_scan_at` and this deliberately does not."""
		from erpnext_mcp.api import asset_tags as api

		before = dict(STORE.get_raw("Asset Register", VALVE))
		api.asset_qr_tag(asset_name=VALVE)
		self.assertEqual(dict(STORE.get_raw("Asset Register", VALVE)), before)

	# -- the sheet endpoint -------------------------------------------------
	def test_the_sheet_renders_the_selected_assets(self):
		answer = asset_tag_sheet.render_asset_qr_sheet(assets=[VALVE])
		self.assertEqual(answer["label_count"], 1)
		self.assertIn(VALVE, answer["html"])

	def test_it_reads_a_json_array_the_way_frappe_posts_one(self):
		"""`frappe.call` posts a JS array as a JSON string, which is the shape the
		Client Script actually sends."""
		answer = asset_tag_sheet.render_asset_qr_sheet(assets=f'["{VALVE}"]')
		self.assertEqual(answer["label_count"], 1)

	def test_it_reads_a_comma_separated_string_too(self):
		answer = asset_tag_sheet.render_asset_qr_sheet(assets=f"{VALVE}, {VALVE}")
		self.assertEqual(answer["label_count"], 2)

	def test_naming_nothing_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			asset_tag_sheet.render_asset_qr_sheet(assets=[])

	def test_one_bad_docname_does_not_lose_the_sheet(self):
		answer = asset_tag_sheet.render_asset_qr_sheet(assets=[VALVE, "MC-Nothing-99"])
		self.assertEqual(answer["label_count"], 1)
		self.assertEqual(len(answer["errors"]), 1)
		self.assertIn("MC-Nothing-99", answer["html"])

	def test_an_asset_the_session_cannot_read_is_an_error_not_a_throw(self):
		"""One asset in another company must not lose the other twenty-nine
		labels — but it must not print either."""
		second = "MC-Valve-06"
		self.tool_data("register_asset", {"name": second, "asset_type": "Irrigation Valve", "company": MAIN})
		self.deny(second)
		answer = asset_tag_sheet.render_asset_qr_sheet(assets=[VALVE, second])
		self.assertEqual(answer["label_count"], 1)
		self.assertIn(second, answer["html"])
		self.assertTrue(any(second in str(e.get("asset_name")) for e in answer["errors"]))

	def test_no_readable_assets_at_all_is_refused(self):
		self.deny(VALVE)
		with self.assertRaises(frappe.ValidationError):
			asset_tag_sheet.render_asset_qr_sheet(assets=[VALVE])


# ── 6 ───────────────────────────────────────────────────────────────────────
class TheFormAction(V12TestCase):
	def setUp(self):
		super().setUp()
		STORE.rows("Client Script").clear()

	def test_it_creates_the_button(self):
		report = asset_tag_form_action.seed_asset_tag_form_action()
		self.assertTrue(report["created"])
		self.assertTrue(asset_tag_form_action._existing())

	def test_a_second_migrate_creates_nothing(self):
		asset_tag_form_action.seed_asset_tag_form_action()
		report = asset_tag_form_action.seed_asset_tag_form_action()
		self.assertFalse(report["created"])
		self.assertEqual(report["reason"], "already present")
		self.assertEqual(len(STORE.rows("Client Script")), 1)

	def test_it_is_bound_to_the_asset_register_form(self):
		asset_tag_form_action.seed_asset_tag_form_action()
		row = STORE.rows("Client Script")[0]
		self.assertEqual(row["dt"], "Asset Register")
		self.assertEqual(row["view"], "Form")
		self.assertEqual(row["enabled"], 1)

	def test_an_operators_edit_survives_every_future_migrate(self):
		"""A customisation that reappears at every `bench migrate` is one nobody
		can decline, and one that is silently overwritten is worse.

		AN APPEND IS LEFT ALONE BY THE STAMP RATHER THAN BY THE FINGERPRINT, and
		that is worth pinning: the operator's line comes after a source that still
		carries `SCRIPT_STAMP`, so the seeder reads "this site is on the current
		revision" and stops before it ever looks at `PRIOR_REVISIONS`. Same
		outcome, earlier branch."""
		asset_tag_form_action.seed_asset_tag_form_action()
		name = asset_tag_form_action._existing()
		edited = asset_tag_form_action.SCRIPT_SOURCE + "\n// mine\n"
		frappe.db.set_value("Client Script", name, "script", edited)

		asset_tag_form_action.seed_asset_tag_form_action()
		self.assertEqual(frappe.db.get_value("Client Script", name, "script"), edited)

	def test_an_edit_that_removes_the_stamp_is_reported_as_stale(self):
		"""THE OTHER BRANCH, and the one an operator needs told about. A copy
		whose stamp has gone fingerprints as nothing this app ever shipped, so it
		is left exactly as it is AND said out loud — an operator whose edit is
		silently kept and silently stale has been told nothing."""
		asset_tag_form_action.seed_asset_tag_form_action()
		name = asset_tag_form_action._existing()
		mine = f"// {asset_tag_form_action.SCRIPT_MARKER}\n// entirely my own\n"
		frappe.db.set_value("Client Script", name, "script", mine)

		report = asset_tag_form_action.seed_asset_tag_form_action()
		self.assertEqual(frappe.db.get_value("Client Script", name, "script"), mine)
		self.assertTrue(report["reason"].startswith("left alone"))
		self.assertIn(asset_tag_form_action.SCRIPT_REVISION, report["reason"])

	def test_uninstall_takes_it_away(self):
		asset_tag_form_action.seed_asset_tag_form_action()
		report = asset_tag_form_action.remove_asset_tag_form_action()
		self.assertTrue(report["removed"])
		self.assertEqual(asset_tag_form_action._existing(), "")

	def test_removing_what_is_not_there_is_not_an_error(self):
		report = asset_tag_form_action.remove_asset_tag_form_action()
		self.assertFalse(report["removed"])
		self.assertEqual(report["reason"], "not present")

	def test_it_never_raises_when_the_insert_fails(self):
		"""A seeder that raised would take `bench migrate` down with it."""
		original = frappe.get_doc

		def explode(*args, **kwargs):
			if args and isinstance(args[0], dict) and args[0].get("doctype") == "Client Script":
				raise RuntimeError("no")
			return original(*args, **kwargs)

		frappe.get_doc = explode
		try:
			report = asset_tag_form_action.seed_asset_tag_form_action()
		finally:
			frappe.get_doc = original
		self.assertFalse(report["created"])
		self.assertIn("RuntimeError", report["reason"])


# ── 6b ──────────────────────────────────────────────────────────────────────
class TheListAction(V12TestCase):
	def setUp(self):
		super().setUp()
		STORE.rows("Client Script").clear()

	def test_it_creates_the_action(self):
		report = asset_tag_list_action.seed_asset_tag_list_action()
		self.assertTrue(report["created"])
		self.assertTrue(asset_tag_list_action._existing())

	def test_it_is_bound_to_the_asset_register_list(self):
		asset_tag_list_action.seed_asset_tag_list_action()
		row = STORE.rows("Client Script")[0]
		self.assertEqual(row["dt"], "Asset Register")
		self.assertEqual(row["view"], "List")

	def test_the_two_rows_do_not_collide(self):
		"""Form and list are two rows on the same doctype, and each seeder has to
		find its OWN. A marker match that was only on `dt` would make the second
		seeder think the first's row was its own and rewrite it."""
		asset_tag_form_action.seed_asset_tag_form_action()
		asset_tag_list_action.seed_asset_tag_list_action()
		self.assertEqual(len(STORE.rows("Client Script")), 2)
		self.assertNotEqual(asset_tag_form_action._existing(), asset_tag_list_action._existing())

	def test_it_does_not_overwrite_the_list_settings_object(self):
		"""ERPNext and this app both write `frappe.listview_settings`. Assigning a
		fresh object over the top takes away whatever else set one."""
		source = asset_tag_list_action.SCRIPT_SOURCE
		self.assertIn('frappe.listview_settings["Asset Register"] || {}', source)
		self.assertIn("previous_onload", source)

	def test_an_operators_edit_survives_every_future_migrate(self):
		asset_tag_list_action.seed_asset_tag_list_action()
		name = asset_tag_list_action._existing()
		edited = asset_tag_list_action.SCRIPT_SOURCE + "\n// mine\n"
		frappe.db.set_value("Client Script", name, "script", edited)

		asset_tag_list_action.seed_asset_tag_list_action()
		self.assertEqual(frappe.db.get_value("Client Script", name, "script"), edited)

	def test_an_edit_that_removes_the_stamp_is_reported_as_stale(self):
		asset_tag_list_action.seed_asset_tag_list_action()
		name = asset_tag_list_action._existing()
		mine = f"// {asset_tag_list_action.SCRIPT_MARKER}\n// entirely my own\n"
		frappe.db.set_value("Client Script", name, "script", mine)

		report = asset_tag_list_action.seed_asset_tag_list_action()
		self.assertEqual(frappe.db.get_value("Client Script", name, "script"), mine)
		self.assertTrue(report["reason"].startswith("left alone"))

	def test_uninstall_takes_it_away(self):
		asset_tag_list_action.seed_asset_tag_list_action()
		self.assertTrue(asset_tag_list_action.remove_asset_tag_list_action()["removed"])
		self.assertEqual(asset_tag_list_action._existing(), "")


# ── 7 ───────────────────────────────────────────────────────────────────────
class TheScriptsAndTheServerAgree(unittest.TestCase):
	"""THE FAILURE NO OTHER TEST HERE WOULD CATCH: a button whose dotted path
	names a function nobody wrote. Every other test in this file exercises the
	server and the script separately."""

	def resolve(self, dotted: str):
		module_path, _, attribute = dotted.rpartition(".")
		module = __import__(module_path, fromlist=[attribute])
		return getattr(module, attribute, None)

	def test_the_form_button_calls_something_that_exists(self):
		self.assertTrue(callable(self.resolve(asset_tag_form_action.TAG_METHOD)))

	def test_the_list_action_calls_something_that_exists(self):
		self.assertTrue(callable(self.resolve(asset_tag_list_action.SHEET_METHOD)))

	def test_both_targets_are_whitelisted(self):
		"""A method that is not whitelisted answers 403 to the button and nothing
		about the Desk says why."""
		for dotted in (asset_tag_form_action.TAG_METHOD, asset_tag_list_action.SHEET_METHOD):
			with self.subTest(method=dotted):
				self.assertTrue(getattr(self.resolve(dotted), "__wrapped_whitelisted__", False))

	def test_each_script_names_its_own_method(self):
		self.assertIn(asset_tag_form_action.TAG_METHOD, asset_tag_form_action.SCRIPT_SOURCE)
		self.assertIn(asset_tag_list_action.SHEET_METHOD, asset_tag_list_action.SCRIPT_SOURCE)

	def test_each_script_carries_its_stamp(self):
		"""Identity and revision on line one, so an operator reading the row in the
		Desk can see which one they are looking at without diffing anything."""
		self.assertIn(asset_tag_form_action.SCRIPT_STAMP, asset_tag_form_action.SCRIPT_SOURCE)
		self.assertIn(asset_tag_list_action.SCRIPT_STAMP, asset_tag_list_action.SCRIPT_SOURCE)

	def test_the_two_markers_are_different(self):
		"""They are the identity each seeder matches on. Equal markers would make
		the two seeders fight over one row at every migrate."""
		self.assertNotEqual(asset_tag_form_action.SCRIPT_MARKER, asset_tag_list_action.SCRIPT_MARKER)

	def test_neither_script_writes_the_sheet_into_about_blank(self):
		"""v0.56.1's bug, which the asset sheet ships already fixed: a document
		written into `about:blank` prints as an empty page."""
		for source in (asset_tag_form_action.SCRIPT_SOURCE, asset_tag_list_action.SCRIPT_SOURCE):
			with self.subTest():
				self.assertIn("createObjectURL", source)
				self.assertIn("revokeObjectURL", source)
