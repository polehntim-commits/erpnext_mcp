# SPDX-License-Identifier: MIT
"""PDF-only asset base URL, the PDF self-test, and outgoing mail sender policy. v0.216.1.

The bug: wkhtmltopdf, inside the container, fetched a print's stylesheet from
the site's PUBLIC address (`host_name`) and could not resolve it —
HostNotFoundError in Email Queue, blank Desk PDFs. These hold the fix to its two
promises: the PDF fetches from the internal base, and nothing a person reads or
clicks changes address.
"""

import re
import sys
import types

import frappe

from erpnext_mcp import mail_status, pdf_base
from erpnext_mcp.patches import zoho_accounts_send_as_account

from .fixtures import SeededTestCase
from .harness import STORE

INTERNAL = "http://127.0.0.1:8080"
PUBLIC = "http://100.69.162.122"
FUNNEL = "https://farm.tail1234.ts.net/erpnext"


def frappe_like_scrub(html):
	"""Frappe's expand_relative_urls, reduced: relative src/href/url() onto host_name."""
	base = str(frappe.local.conf.get("host_name") or PUBLIC).rstrip("/")
	html = re.sub(r'((?:src|href)=["\'])/(?!/)', lambda m: f"{m.group(1)}{base}/", html)
	return re.sub(r"(url\(['\"]?)/(?!/)", lambda m: f"{m.group(1)}{base}/", html)


def frappe_like_cookies():
	host = str(frappe.local.conf.get("host_name") or PUBLIC)
	return {"cookie-jar": f"/tmp/x.jar#{host.split('//')[-1].split(':')[0]}"}


class PdfBaseTestCase(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, pdf_base_url=INTERNAL, public_url=FUNNEL)
		frappe.local.conf["host_name"] = PUBLIC
		self.addCleanup(lambda: frappe.local.conf.pop("host_name", None))
		pdf_base._CACHE.clear()
		pdf_base._INSTALLED["done"] = False
		self.addCleanup(lambda: pdf_base._INSTALLED.update(done=False))


PRINT = (
	"<html><head><link rel='stylesheet' href='/assets/frappe/dist/css/print.bundle.css'>"
	"<style>.x{background:url('/files/letterhead.png')}</style></head><body>"
	"<img src='/files/logo.png'><img src='" + PUBLIC + "/files/badge.jpg'>"
	"<img src='" + FUNNEL + "/files/mark.png'><img src='https://cdn.example.com/x.png'>"
	"<a href='/app/employee/HR-EMP-0001'>Open</a>"
	"<a href='" + PUBLIC + "/update-password?key=abc'>Reset</a>"
	"</body></html>"
)


class ThePdfFetchesFromInside(PdfBaseTestCase):
	def scrub(self, html=PRINT):
		return pdf_base.pdf_scrub_urls(html, frappe_like_scrub)

	def test_relative_assets_go_to_the_internal_base(self):
		out = self.scrub()
		self.assertIn(f"href='{INTERNAL}/assets/frappe/dist/css/print.bundle.css'", out)
		self.assertIn(f"src='{INTERNAL}/files/logo.png'", out)
		self.assertIn(f"url('{INTERNAL}/files/letterhead.png')", out)

	def test_absolute_urls_on_the_sites_own_addresses_go_there_too(self):
		out = self.scrub()
		self.assertIn(f"src='{INTERNAL}/files/badge.jpg'", out)
		# The Funnel address with its /erpnext path, which the container cannot reach.
		self.assertIn(f"src='{INTERNAL}/files/mark.png'", out)

	def test_somebody_elses_host_is_left_alone(self):
		self.assertIn("src='https://cdn.example.com/x.png'", self.scrub())

	def test_links_a_person_clicks_keep_the_public_address(self):
		out = self.scrub()
		# `get_url()` as it stood before the PDF base was applied — on a real
		# site that is `host_name`; the double answers its own address.
		self.assertIn(f"href='{frappe.utils.get_url()}/app/employee/HR-EMP-0001'", out)
		self.assertIn(f"href='{PUBLIC}/update-password?key=abc'", out)
		self.assertNotIn(f"href='{INTERNAL}/app", out)
		self.assertNotIn(f"href='{INTERNAL}/update-password", out)

	def test_host_name_is_restored_afterwards(self):
		self.scrub()
		self.assertEqual(frappe.local.conf["host_name"], PUBLIC)
		self.assertEqual(frappe.utils.get_url(), frappe.utils.get_url())

	def test_host_name_is_restored_even_when_frappe_raises(self):
		def boom(html):
			raise RuntimeError("scrub failed")

		with self.assertRaises(RuntimeError):
			pdf_base.pdf_scrub_urls(PRINT, boom)
		self.assertEqual(frappe.local.conf["host_name"], PUBLIC)

	def test_a_site_with_no_host_name_is_left_with_none(self):
		frappe.local.conf.pop("host_name", None)
		pdf_base.pdf_scrub_urls(PRINT, frappe_like_scrub)
		self.assertNotIn("host_name", frappe.local.conf)

	def test_the_cookie_is_scoped_to_the_internal_host_and_missing_images_are_tolerated(self):
		options = pdf_base.pdf_cookie_options(frappe_like_cookies)
		self.assertTrue(options["cookie-jar"].endswith("#127.0.0.1"))
		self.assertEqual(options["load-media-error-handling"], "ignore")
		self.assertEqual(frappe.local.conf["host_name"], PUBLIC)

	def test_off_means_frappes_own_behaviour(self):
		self.configure(enabled=1, pdf_base_url="off", public_url=FUNNEL)
		self.assertEqual(self.scrub(), frappe_like_scrub(PRINT))
		self.assertNotIn("load-media-error-handling", pdf_base.pdf_cookie_options(frappe_like_cookies))
		self.assertEqual(pdf_base.source(), "off")

	def test_empty_detects_and_falls_back_to_frappe_when_nothing_answers(self):
		self.configure(enabled=1, pdf_base_url="", public_url=FUNNEL)
		original = pdf_base._answers
		pdf_base._answers = lambda url: False
		self.addCleanup(lambda: setattr(pdf_base, "_answers", original))
		self.assertEqual(pdf_base.internal_base(), "")
		self.assertEqual(pdf_base.source(), "none")
		self.assertEqual(self.scrub(), frappe_like_scrub(PRINT))

	def test_empty_detects_the_containers_own_nginx(self):
		self.configure(enabled=1, pdf_base_url="", public_url=FUNNEL)
		original = pdf_base._answers
		pdf_base._answers = lambda url: url == INTERNAL
		self.addCleanup(lambda: setattr(pdf_base, "_answers", original))
		self.assertEqual(pdf_base.internal_base(), INTERNAL)
		self.assertEqual(pdf_base.source(), "auto")


class InstallingIt(PdfBaseTestCase):
	def fake_module(self):
		module = types.SimpleNamespace(scrub_urls=frappe_like_scrub, get_cookie_options=frappe_like_cookies)
		return module

	def test_it_replaces_only_the_two_names_inside_the_pdf_module(self):
		module = self.fake_module()
		self.assertTrue(pdf_base.patch(module))
		self.assertIn(INTERNAL, module.scrub_urls("<img src='/files/a.png'>"))
		self.assertIn("load-media-error-handling", module.get_cookie_options())
		# frappe.utils itself — what email bodies use — is not touched.
		self.assertFalse(getattr(getattr(frappe.utils, "scrub_urls", None), pdf_base._MARK, False))

	def test_it_is_idempotent(self):
		module = self.fake_module()
		pdf_base.patch(module)
		first = module.scrub_urls
		pdf_base._INSTALLED["done"] = False
		pdf_base.patch(module)
		self.assertIs(module.scrub_urls, first)

	def test_install_takes_whatever_the_hooks_pass_and_never_raises(self):
		self.assertIn(pdf_base.install(method="x", kwargs={}, transaction_type="job"), (True, False))

	def test_the_hooks_install_it_in_web_and_queue_workers(self):
		from erpnext_mcp import hooks

		self.assertEqual(hooks.before_request, ["erpnext_mcp.pdf_base.install"])
		self.assertEqual(hooks.before_job, ["erpnext_mcp.pdf_base.install"])


class TheSelfTest(PdfBaseTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, pdf_base_url=INTERNAL, public_url=FUNNEL, allow_test_pdf_rendering=1)
		self.rendered = []
		module = types.ModuleType("frappe.utils.pdf")
		module.scrub_urls = frappe_like_scrub
		module.get_cookie_options = frappe_like_cookies
		module.get_wkhtmltopdf_version = lambda: "0.12.6"

		def get_pdf(html, options=None):
			html = module.scrub_urls(html)
			self.rendered.append(html)
			if self.fail:
				raise OSError(
					"wkhtmltopdf exited with non-zero code 1. error:\nExit with code 1 due to network error: HostNotFoundError"
				)
			return b"%PDF-1.4 test"

		module.get_pdf = get_pdf
		self.fail = False
		sys.modules["frappe.utils.pdf"] = module
		self.addCleanup(lambda: sys.modules.pop("frappe.utils.pdf", None))
		original = pdf_base._answers
		pdf_base._answers = lambda url: True
		self.addCleanup(lambda: setattr(pdf_base, "_answers", original))

	def test_it_renders_through_get_pdf_with_the_internal_base(self):
		data = self.tool_data("test_pdf_rendering", {})
		self.assertTrue(data["ok"], data)
		self.assertEqual(data["renders"][0]["bytes"], len(b"%PDF-1.4 test"))
		self.assertEqual(data["wkhtmltopdf_version"], "0.12.6")
		self.assertEqual(data["pdf_base"]["base_url"], INTERNAL)
		self.assertIn(INTERNAL + "/assets/", self.rendered[0])

	def test_a_failure_says_what_it_means(self):
		self.fail = True
		data = self.tool_data("test_pdf_rendering", {})
		self.assertFalse(data["ok"])
		self.assertIn("HostNotFoundError", data["renders"][0]["error"])
		self.assertIn("pdf_base_url", data["renders"][0]["diagnosis"])

	def test_doctype_and_name_go_together(self):
		self.assertIn("both", self.tool_error("test_pdf_rendering", {"doctype": "Employee"}))

	def test_it_writes_nothing(self):
		before = {doctype: len(STORE.rows(doctype)) for doctype in ("File", "Email Queue", "Comment")}
		self.tool_data("test_pdf_rendering", {})
		self.assertEqual(before, {doctype: len(STORE.rows(doctype)) for doctype in before})

	def test_status_reports_the_base_without_rendering(self):
		self.configure(enabled=1, pdf_base_url=INTERNAL)
		block = self.tool_data("get_server_status", {})["pdf"]
		self.assertEqual(block["base_url"], INTERNAL)
		self.assertEqual(block["source"], "setting")
		self.assertEqual(self.rendered, [])


ZOHO = {
	"name": "Farm Mail",
	"email_id": "office@farm.example",
	"smtp_server": "smtp.zoho.com",
	"enable_outgoing": 1,
	"default_outgoing": 1,
	"always_use_account_email_id_as_sender": 0,
	"always_use_account_name_as_sender_name": 0,
}
GMAIL = dict(ZOHO, name="Backup Mail", smtp_server="smtp.gmail.com", default_outgoing=0)


class ZohoSendsAsTheAccount(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1)
		STORE.seed("Email Account", [dict(ZOHO), dict(GMAIL)])

	def test_status_flags_both_and_says_zoho_will_refuse(self):
		block = mail_status.status()
		self.assertEqual({row["account"] for row in block["outgoing_accounts"]}, {"Farm Mail", "Backup Mail"})
		zoho = next(row for row in block["outgoing_accounts"] if row["account"] == "Farm Mail")
		self.assertTrue(zoho["provider_requires_account_sender"])
		self.assertFalse(zoho["always_use_account_email_as_sender"])
		self.assertTrue(any("refuses any other sender" in line for line in block["warnings"]))
		self.assertTrue(any("Backup Mail" in line for line in block["warnings"]))

	def test_the_patch_ticks_zoho_only_and_is_a_no_op_twice(self):
		zoho_accounts_send_as_account.execute()
		self.assertEqual(
			int(frappe.db.get_value("Email Account", "Farm Mail", "always_use_account_email_id_as_sender")), 1
		)
		self.assertEqual(
			int(
				frappe.db.get_value("Email Account", "Backup Mail", "always_use_account_email_id_as_sender")
				or 0
			),
			0,
		)
		self.assertEqual(mail_status.default_sender_policy(), [])

	def test_after_the_patch_zoho_has_no_warning(self):
		zoho_accounts_send_as_account.execute()
		self.assertFalse(any("Farm Mail" in line for line in mail_status.status()["warnings"]))

	def test_no_outgoing_account_is_said(self):
		for name in ("Farm Mail", "Backup Mail"):
			frappe.db.set_value("Email Account", name, "enable_outgoing", 0)
		self.assertIn("No enabled outgoing Email Account", " ".join(mail_status.status()["warnings"]))

	def test_get_server_status_carries_it(self):
		self.assertIn("outgoing_accounts", self.tool_data("get_server_status", {})["email"])
