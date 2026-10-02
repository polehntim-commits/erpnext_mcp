# SPDX-License-Identifier: MIT
"""Every ID and asset print draws the CR80 card (v0.213.0). `card_redirect`."""

from unittest import mock

import frappe

from erpnext_mcp import asset_tag_sheet, badge_print_format, badge_sheet, card_art, card_redirect
from erpnext_mcp.api import asset_tags as asset_tag_api
from erpnext_mcp.api import badges as badge_api
from erpnext_mcp.patches import legacy_badge_prints_use_cr80 as patch

from .fixtures import MAIN
from .harness import STORE
from .test_api_mobile import WORKER_EMPLOYEE
from .test_card_print import NEEDS_QR, CardPrintCase

VALVE = "MC-Valve-05"
FORMAT = badge_print_format.FORMAT_NAME
EXTRA = {
	f"allow_{name}": 1
	for name in (
		"generate_employee_badge_qr",
		"generate_employee_id_card",
		"generate_employee_badge_sheet",
		"generate_asset_qr",
		"generate_asset_qr_sheet",
		"register_asset",
	)
}


class RedirectCase(CardPrintCase):
	def setUp(self):
		super().setUp()
		from .test_card_print import ON

		self.configure(enabled=1, **{**ON, **EXTRA})
		self.be("Administrator")
		self.tool_data("register_asset", {"name": VALVE, "asset_type": "Irrigation Valve", "company": MAIN})
		STORE.commit()

	def assert_cr80(self, html, cards=1):
		self.assertIn("@page { size: 85.6mm 54mm; margin: 0; }", html)
		self.assertEqual(html.count('<div class="cr80"><svg'), 2 * cards, "a front and a back per card")
		self.assertNotIn("badge-card", html, "the old badge markup is gone")
		self.assertNotIn("qr-tag", html)


@NEEDS_QR
class TheFourLegacyButtons(RedirectCase):
	def test_print_badge_sheet_issues_the_badges_and_prints_cr80_cards(self):
		answer = badge_sheet.render_badge_sheet(employees=[WORKER_EMPLOYEE])
		self.assertEqual((answer["card_count"], answer["issued_count"]), (1, 1))
		self.assert_cr80(answer["html"])
		self.assertEqual(len(STORE.rows("Bucket Log Badge Map")), 1, "the badge is still issued")

	def test_somebody_skipped_is_printed_on_the_last_card(self):
		answer = badge_sheet.render_badge_sheet(employees=[WORKER_EMPLOYEE, "EMP-NOBODY"])
		self.assert_cr80(answer["html"])
		self.assertIn("No card for", answer["html"])
		self.assertIn("EMP-NOBODY", answer["html"])

	def test_the_id_card_button_shows_the_cr80_card_and_attaches_its_pdf(self):
		answer = badge_api.employee_badge_card(employee=WORKER_EMPLOYEE)
		self.assert_cr80(answer["html"])
		self.assertTrue(answer["badge_id"])
		data = self.tool_data("generate_employee_id_card", {"employee": WORKER_EMPLOYEE, "attach": False})
		self.assertEqual(data["print_format"], "Employee ID Card (CR80)")
		self.assertGreater(data["pdf_bytes"], 0, "drawn by card_art: no wkhtmltopdf needed")
		pdf, note = card_redirect.pdf("Employee", WORKER_EMPLOYEE)
		self.assertEqual((card_art.page_sizes_mm(pdf), note), ([(85.6, 54.0), (85.6, 54.0)], ""))

	def test_generate_qr_sheet_and_qr_tag_print_the_cr80_asset_tag(self):
		sheet = asset_tag_sheet.render_asset_qr_sheet(assets=[VALVE])
		self.assert_cr80(sheet["html"])
		self.assertEqual(sheet["label_count"], 1)
		tag = asset_tag_api.asset_qr_tag(asset_name=VALVE)
		self.assert_cr80(tag["html"])
		self.assertTrue(tag["png_data_uri"], "the QR itself is still handed back")

	def test_the_flag_brings_the_old_layouts_back(self):
		with mock.patch.object(card_redirect, "legacy", return_value=True):
			self.assertIn("badge-card", badge_sheet.render_badge_sheet(employees=[WORKER_EMPLOYEE])["html"])
			self.assertNotIn('class="cr80"', asset_tag_sheet.render_asset_qr_sheet(assets=[VALVE])["html"])
		self.assertFalse(card_redirect.legacy(), "off unless a site sets legacy_badge_layouts")


class TheBadgeRowPrintFormat(RedirectCase):
	def seed_legacy(self, html):
		STORE.rows("Print Format")[:] = [r for r in STORE.rows("Print Format") if r["name"] != FORMAT]
		fields = badge_print_format.print_format_fields()
		fields["html"] = html
		frappe.get_doc(fields).insert()
		STORE.commit()

	def test_a_new_site_gets_the_cr80_card_on_it(self):
		html = badge_print_format.print_format_fields()["html"]
		self.assertIn('erpnext_mcp_card_svg("Employee", doc.employee)', html)
		self.assertIn("85.6mm 54mm", html)

	def test_the_patch_repoints_an_untouched_format_once(self):
		self.seed_legacy(badge_print_format.LEGACY_TEMPLATE)
		report = patch.run()["badge_format"]
		self.assertTrue(report["changed"])
		self.assertIn("erpnext_mcp_card_svg", STORE.get_raw("Print Format", FORMAT)["html"])
		again = patch.run()["badge_format"]
		self.assertEqual((again["changed"], again["reason"]), (False, "already draws the CR80 card"))

	def test_an_edited_format_is_left_alone_and_said_so(self):
		mine = badge_print_format.LEGACY_TEMPLATE + "<!-- our own nudge for the lanyard slot -->"
		self.seed_legacy(mine)
		report = patch.run()["badge_format"]
		self.assertFalse(report["changed"])
		self.assertIn("edited on this site", report["reason"])
		self.assertEqual(STORE.get_raw("Print Format", FORMAT)["html"], mine)

	def test_the_patch_is_registered_and_makes_sure_both_card_formats_exist(self):
		import pathlib

		listed = (pathlib.Path(patch.__file__).parents[1] / "patches.txt").read_text()
		self.assertIn("erpnext_mcp.patches.legacy_badge_prints_use_cr80", listed)
		formats = {r["format"] for r in patch.run()["card_formats"]}
		self.assertEqual(formats, {"Employee ID Card (CR80)", "Asset Tag Card (CR80)"})
		patch.execute()
