# SPDX-License-Identifier: MIT
"""ERPNext MCP Settings: the shipped defaults, the form's guardrails, the token.

The first class here is the one that matters if you only read one: it checks the
DocType JSON against the tool registry, so a tool added to `registry.py` without
a switch on the form — which would ship with no way to turn it off — fails the
build.
"""

import json

from erpnext_mcp import form_pdf_renderer, geo, i9_pdf, packets, registry, settings, w4_pdf
from erpnext_mcp.erpnext_mcp.doctype.erpnext_mcp_settings.erpnext_mcp_settings import (
	TOKEN_LENGTH,
)

from .fixtures import SeededTestCase
from .harness import META, STORE, _load_app_doctype, frappe

#: Tools the default fixture site cannot run, because it has no `hrms`.
#:
#: v0.160.0 ADDED THE FIVE LEAVE TOOLS, which is a correction rather than a
#: feature: they shipped in v0.158.0 gated on `hrms`'s Leave Application and
#: Leave Type exactly like the three above, and this list was not extended with
#: them — so this test has been failing on every run since that release, naming
#: five tools that are correctly unavailable. A permanently red test is how the
#: next real failure goes unnoticed.
HR_TOOLS = (
	"get_attendance_summary",
	"get_leave_balance",
	"list_employees",
	"approve_leave_request",
	"create_leave_request",
	"list_leave_requests",
	"list_leave_types",
	"reject_leave_request",
)

#: Tools that need shapely and h3. Declared dependencies, so a normal install has
#: them — but the suite has to pass on a bench that does not, because that is the
#: whole point of the `available` predicate on them.
GEO_TOOLS = (
	"set_field_boundary",
	"set_zone_boundary",
	# v0.32.0. Six now, not five: a parcel carries a polygon too, and it goes
	# unavailable on a bench with no shapely and h3 for exactly the same reason
	# the other five do.
	"set_parcel_boundary",
	"find_fields_containing_point",
	"find_fields_by_h3_cell",
	"import_field_boundary_geojson",
)

#: v0.36.0. Tools that need reportlab, for the same reason and with the same
#: promise: a declared dependency a normal install has, and two tools that go
#: quietly unavailable on a bench that does not. Every other tax form tool keeps
#: working there — the computed values are the deliverable and the page is a
#: convenience — which is what makes this a two-name list rather than a section.
PDF_TOOLS = (
	"render_tax_form_pdf",
	"bulk_render_tax_form_pdfs",
	# Both gate on `form_pdf_renderer.available()`, exactly as the two above
	# do — `render_pay_stub` since v0.72.0 and `render_garnishment_response`
	# with the garnishment release. Neither was added here when it landed,
	# so this list was right only on a bench that had reportlab.
	"render_pay_stub",
	"render_garnishment_response",
)

#: v0.47.1. The tool that needs pypdf AND the shipped USCIS template. A separate
#: list from `PDF_TOOLS` because it is a separate dependency answering a separate
#: question — reportlab DRAWS a page, pypdf FILLS the government's own — and a
#: bench can have either without the other. `attach_signed_i9` is deliberately
#: absent: filing a scan somebody else produced needs no PDF library at all.
I9_PDF_TOOLS = ("render_i9_pdf",)

#: Listed apart from `PDF_TOOLS` for the same reason `I9_PDF_TOOLS` is: it
#: answers a different `available()` — `w4_pdf` needs the shipped IRS
#: template as well as reportlab, so a bench can have one and not the other.
W4_PDF_TOOLS = ("render_w4_pdf",)


class ShippedDefaults(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.json = _load_app_doctype("erpnext_mcp_settings")
		self.by_name = {field["fieldname"]: field for field in self.json["fields"]}

	def test_every_tool_has_a_switch(self):
		for name in registry.TOOLS:
			with self.subTest(tool=name):
				self.assertIn(
					f"allow_{name}",
					self.by_name,
					f"tool {name} has no allow_ field, so an operator cannot turn it off",
				)

	def test_every_packet_type_has_a_switch(self):
		"""A packet type is gated separately from the tool that builds it, so an
		operator can expose a reconciliation packet without exposing a payroll
		one. A type with no switch is one they cannot refuse."""
		for name in packets.names():
			with self.subTest(packet=name):
				self.assertIn(
					f"allow_{name}",
					self.by_name,
					f"packet type {name} has no allow_ field",
				)

	def test_every_switch_has_a_tool_or_a_packet_type(self):
		"""The other direction: a leftover switch is a control that does nothing."""
		known = set(registry.TOOLS) | set(packets.names())
		for fieldname in self.by_name:
			if fieldname.startswith("allow_"):
				with self.subTest(field=fieldname):
					self.assertIn(fieldname[len("allow_") :], known)

	def test_packet_types_default_on(self):
		"""Packets are read-only artefacts — the gate that matters is the tool
		switch above them, and defaulting the types off would mean an operator who
		enabled the tool still got an empty catalogue."""
		for name in packets.names():
			with self.subTest(packet=name):
				self.assertEqual(self.by_name[f"allow_{name}"]["default"], "1")

	def test_read_tools_default_on(self):
		for name in registry.READ_TOOLS:
			with self.subTest(tool=name):
				self.assertEqual(self.by_name[f"allow_{name}"]["default"], "1")

	def test_mutating_tools_default_off(self):
		for name in registry.MUTATING_TOOLS:
			if name in registry.DEFAULT_ON_MUTATING_TOOLS:
				continue
			with self.subTest(tool=name):
				self.assertEqual(self.by_name[f"allow_{name}"]["default"], "0")

	def test_the_default_on_exceptions_are_named_and_argued_for(self):
		"""A promise with an undocumented exception is not a promise.

		`DEFAULT_ON_MUTATING_TOOLS` is the one place an exception to "mutating
		tools ship off" may live, and it has to carry a real sentence saying why —
		so a new exception cannot arrive without somebody writing the argument for
		it, and a reader has one place to check.
		"""
		for name, why in registry.DEFAULT_ON_MUTATING_TOOLS.items():
			with self.subTest(tool=name):
				self.assertIn(name, registry.MUTATING_TOOLS)
				self.assertEqual(self.by_name[f"allow_{name}"]["default"], "1")
				self.assertGreater(
					len(why),
					120,
					f"{name} is a mutating tool that ships ON and its justification is a "
					"phrase. Write the argument.",
				)

	def test_the_only_default_on_exception_is_the_compliance_field_installer(self):
		"""Asserted as an exact set rather than a membership check.

		The whole value of "mutating tools ship off" is that it is true without
		qualification, and every exception erodes it. A second one has to fail here
		and be argued for in a review rather than added quietly beside the first.
		"""
		self.assertEqual(set(registry.DEFAULT_ON_MUTATING_TOOLS), {"install_compliance_fields"})

	def test_the_master_switch_defaults_off(self):
		self.assertEqual(self.by_name["enabled"]["default"], "0")

	def test_the_token_is_a_password_field(self):
		self.assertEqual(self.by_name["auth_token"]["fieldtype"], "Password")

	def test_the_default_cidrs_are_lan_only(self):
		default = self.by_name["allowed_cidrs"]["default"]
		self.assertEqual(default, settings.DEFAULT_CIDRS)
		self.assertNotIn("0.0.0.0/0", default)

	def test_user_attribution_defaults_on(self):
		self.assertEqual(self.by_name["require_user_context"]["default"], "1")

	def test_only_system_manager_can_read_the_settings(self):
		self.assertEqual([p["role"] for p in self.json["permissions"]], ["System Manager"])

	def test_settings_changes_are_version_tracked(self):
		"""Who turned write access on, and when, is exactly the change worth a
		Version row."""
		self.assertTrue(self.json.get("track_changes"))

	def test_every_switch_carries_a_description(self):
		"""The description is the only explanation an operator gets before ticking
		a box that lets an AI post to the ledger."""
		for fieldname, field in self.by_name.items():
			if fieldname.startswith("allow_"):
				with self.subTest(field=fieldname):
					self.assertTrue(field.get("description"))


class SettingsFormJS(SeededTestCase):
	"""The form's JavaScript must not keep its own copy of the catalogue."""

	def js(self):
		import os

		from .harness import DOCTYPE_DIR

		path = os.path.join(DOCTYPE_DIR, "erpnext_mcp_settings", "erpnext_mcp_settings.js")
		with open(path) as handle:
			return handle.read()

	def test_it_asks_the_server_which_tools_write(self):
		self.assertIn("erpnext_mcp.mcp.mutating_tool_names", self.js())

	def test_it_does_not_hardcode_the_write_tool_list(self):
		"""A hardcoded copy is how the banner ends up saying "read-only" while a
		tool added in a later version is live."""
		body = self.js()
		hardcoded = [name for name in registry.MUTATING_TOOLS if f'"{name}"' in body or f"'{name}'" in body]
		self.assertEqual(hardcoded, [], f"write tool names hardcoded in the JS: {hardcoded}")

	def test_the_whitelisted_helper_returns_the_registry_list(self):
		from erpnext_mcp import mcp

		self.assertEqual(mcp.mutating_tool_names(), list(registry.MUTATING_TOOLS))


class BooleanCasting(SeededTestCase):
	def test_the_string_zero_reads_as_off(self):
		"""`tabSingles.value` is text, so a Check comes back as "0" — which Python
		calls truthy. Getting this wrong turns every switch on."""
		self.assertFalse(settings.as_bool("0"))
		self.assertFalse(settings.as_bool(""))
		self.assertFalse(settings.as_bool(None))
		self.assertFalse(settings.as_bool(0))

	def test_the_string_one_reads_as_on(self):
		self.assertTrue(settings.as_bool("1"))
		self.assertTrue(settings.as_bool(1))
		self.assertTrue(settings.as_bool(True))

	def test_a_string_zero_master_switch_disables_the_endpoint(self):
		self.configure(enabled="0")
		_, status = self.call("tools/list")
		self.assertEqual(status, 404)

	def test_a_string_zero_tool_switch_hides_the_tool(self):
		self.configure(enabled=1, allow_search_accounts="0")
		body, _ = self.call("tools/list")
		self.assertNotIn("search_accounts", [tool["name"] for tool in body["result"]["tools"]])


class MissingValues(SeededTestCase):
	def test_an_unset_field_falls_back_to_its_declared_default(self):
		"""A fresh install has no rows in tabSingles at all, and the read tools
		have to still be on."""
		STORE.singles["ERPNext MCP Settings"] = {"doctype": "ERPNext MCP Settings"}
		self.assertTrue(settings.tool_enabled("get_company_topology"))
		self.assertFalse(settings.tool_enabled("create_journal_entry"))
		self.assertEqual(settings.allowed_cidrs()[0], "127.0.0.1/32")

	def test_an_unset_master_switch_is_off(self):
		STORE.singles["ERPNext MCP Settings"] = {"doctype": "ERPNext MCP Settings"}
		self.assertFalse(settings.is_enabled())

	def test_a_tool_with_no_field_at_all_is_off(self):
		self.assertFalse(settings.tool_enabled("tool_from_a_future_version"))

	def test_seed_defaults_writes_the_declared_values(self):
		STORE.singles["ERPNext MCP Settings"] = {"doctype": "ERPNext MCP Settings"}
		settings.seed_defaults()
		stored = STORE.singles["ERPNext MCP Settings"]
		self.assertEqual(stored["allow_get_company_topology"], "1")
		self.assertEqual(stored["allow_create_journal_entry"], "0")
		self.assertEqual(stored["enabled"], "0")

	def test_seed_defaults_does_not_overwrite_an_operators_choice(self):
		"""Including a deliberate "off" — that is the whole point of "only fill in
		what is missing"."""
		self.configure(enabled=1, allow_search_accounts=0, allowed_cidrs="10.1.0.0/16")
		settings.seed_defaults()
		self.assertFalse(settings.tool_enabled("search_accounts"))
		self.assertEqual(settings.allowed_cidrs(), ["10.1.0.0/16"])

	def test_seed_defaults_is_idempotent(self):
		STORE.singles["ERPNext MCP Settings"] = {"doctype": "ERPNext MCP Settings"}
		settings.seed_defaults()
		first = dict(STORE.singles["ERPNext MCP Settings"])
		settings.seed_defaults()
		self.assertEqual(first, STORE.singles["ERPNext MCP Settings"])

	def test_settings_reads_fail_closed_when_the_doctype_is_missing(self):
		"""A request landing mid-migrate must not answer as if enabled."""
		removed = META.pop("ERPNext MCP Settings")
		try:
			self.assertFalse(settings.is_enabled())
			self.assertFalse(settings.tool_enabled("get_company_topology"))
		finally:
			META["ERPNext MCP Settings"] = removed


class SinglesTableQueries(SeededTestCase):
	"""Regression: v0.2.0's `after_migrate` crashed `bench migrate` on a real site.

	`tabSingles` has three columns — doctype, field, value. It is not a DocType
	table and has none of the framework columns. `frappe.db.get_value` and
	`get_values` both default to ordering by `modified`, so querying tabSingles
	through either without an explicit `order_by` is
	`Unknown column 'modified' in 'ORDER BY'`.

	v0.2.0 shipped that in `settings.seed_defaults`, wired to `after_migrate`, so
	every `bench migrate` on an installed site died. The standalone double
	answered the query happily, which is the reason it got out — so the double
	now refuses it too, and these tests are what would have caught it.
	"""

	def test_seed_defaults_does_not_query_singles_with_a_default_order_by(self):
		"""The regression itself: this raised OperationalError on v0.2.0."""
		STORE.singles["ERPNext MCP Settings"] = {"doctype": "ERPNext MCP Settings"}
		settings.seed_defaults()
		self.assertEqual(STORE.singles["ERPNext MCP Settings"]["allow_get_company_topology"], "1")

	def test_after_migrate_runs_clean(self):
		"""The hook as `hooks.py` calls it, not just the function underneath."""
		from erpnext_mcp import install

		STORE.singles["ERPNext MCP Settings"] = {"doctype": "ERPNext MCP Settings"}
		install.after_migrate()
		self.assertFalse(settings.is_enabled())
		self.assertTrue(settings.tool_enabled("get_company_topology"))

	def test_after_install_runs_clean(self):
		from erpnext_mcp import install

		STORE.singles["ERPNext MCP Settings"] = {"doctype": "ERPNext MCP Settings"}
		install.after_install()
		self.assertEqual(STORE.singles["ERPNext MCP Settings"]["enabled"], "0")

	def test_stored_fieldnames_reports_what_is_set(self):
		self.configure(enabled=1, allow_search_accounts=0)
		stored = settings.stored_fieldnames()
		self.assertIn("enabled", stored)
		self.assertIn("allow_search_accounts", stored)
		self.assertNotIn("doctype", stored, "doctype is the filter, not a field")

	def test_the_double_refuses_the_query_that_broke_production(self):
		"""Belt to the braces: if somebody reintroduces the pattern anywhere, the
		standalone suite fails instead of a customer's `bench migrate`."""
		with self.assertRaises(Exception) as caught:
			frappe.db.get_values("Singles", {"doctype": settings.SETTINGS_DOCTYPE}, ["field", "value"])
		self.assertIn("Unknown column 'modified'", str(caught.exception))

	def test_the_double_refuses_get_value_on_singles_too(self):
		"""get_value is get_values(limit=1) underneath, so it has the same bug —
		and v0.2.0 had a second instance of it in the in-bench suite."""
		with self.assertRaises(Exception) as caught:
			frappe.db.get_value(
				"Singles",
				{"doctype": settings.SETTINGS_DOCTYPE, "field": "auth_token"},
				"value",
			)
		self.assertIn("Unknown column 'modified'", str(caught.exception))

	def test_an_explicit_order_by_none_is_allowed(self):
		"""The default is the bug, not the table. A caller who says what they mean
		is fine."""
		rows = frappe.db.get_values(
			"Singles",
			{"doctype": settings.SETTINGS_DOCTYPE},
			["field", "value"],
			order_by=None,
		)
		self.assertTrue(rows)

	def test_normal_doctypes_are_unaffected(self):
		"""Every real DocType table has `modified`, so the default is fine there —
		the refusal must not become a blanket ban."""
		self.assertIsNotNone(frappe.db.get_value("Company", {"abbr": "ETC"}, "name"))

	def test_no_source_file_queries_singles_through_the_generic_helpers(self):
		"""A grep, as a test — the cheapest guard against this coming back.

		Comments are stripped first, because this file and several others discuss
		the bad pattern by name and the point is to catch calls, not prose.
		"""
		import ast
		import pathlib
		import re

		app = pathlib.Path(__file__).resolve().parents[1] / "erpnext_mcp"
		unsafe = re.compile(r"""get_(?:value|values|all|list)\s*\(\s*["']Singles["']""")
		offenders = []
		for path in sorted(app.rglob("*.py")):
			source = path.read_text()
			# ast.unparse round-trips the code without comments.
			code = ast.unparse(ast.parse(source))
			if unsafe.search(code):
				offenders.append(str(path.relative_to(app.parent)))
		self.assertEqual(
			offenders,
			[],
			f"tabSingles queried through a helper that defaults to ORDER BY modified: {offenders}. "
			"Use frappe.db.get_singles_dict.",
		)


class CidrParsing(SeededTestCase):
	def test_newlines_and_commas_both_separate(self):
		self.configure(enabled=1, allowed_cidrs="10.0.0.0/8\n192.168.0.0/16, 127.0.0.1/32")
		self.assertEqual(settings.allowed_cidrs(), ["10.0.0.0/8", "192.168.0.0/16", "127.0.0.1/32"])

	def test_trailing_comments_are_stripped(self):
		self.configure(enabled=1, allowed_cidrs="10.0.0.0/8 # the office\n127.0.0.1/32")
		self.assertEqual(settings.allowed_cidrs(), ["10.0.0.0/8", "127.0.0.1/32"])

	def test_blank_entries_are_dropped(self):
		self.configure(enabled=1, allowed_cidrs="10.0.0.0/8,,  ,127.0.0.1/32")
		self.assertEqual(settings.allowed_cidrs(), ["10.0.0.0/8", "127.0.0.1/32"])


class FormValidation(SeededTestCase):
	def doc(self, **overrides):
		doc = frappe.get_doc("ERPNext MCP Settings")
		for key, value in overrides.items():
			doc.set(key, value)
		return doc

	def test_an_invalid_cidr_is_refused_with_the_bad_entry_named(self):
		with self.assertRaises(Exception) as caught:
			self.doc(allowed_cidrs="10.0.0.0/8,192.168.1.999/24").save()
		self.assertIn("192.168.1.999/24", str(caught.exception))

	def test_a_valid_list_saves(self):
		self.doc(allowed_cidrs="10.0.0.0/8").save()
		self.assertEqual(settings.allowed_cidrs(), ["10.0.0.0/8"])

	def test_enabling_with_an_empty_allowlist_is_refused(self):
		with self.assertRaises(Exception) as caught:
			self.doc(enabled=1, allowed_cidrs="").save()
		self.assertIn("denies every caller", str(caught.exception))

	def test_an_empty_allowlist_is_allowed_while_disabled(self):
		"""Not a trap to walk into while tidying up a switched-off server."""
		self.doc(enabled=0, allowed_cidrs="").save()

	def test_enabling_without_a_token_is_refused(self):
		self.set_token("")
		with self.assertRaises(Exception) as caught:
			self.doc(enabled=1).save()
		self.assertIn("Generate an auth token", str(caught.exception))

	def test_a_disabled_mcp_system_user_is_refused(self):
		STORE.tables["User"]["mcp@example.test"]["enabled"] = 0
		with self.assertRaises(Exception) as caught:
			self.doc(require_user_context=1, mcp_system_user="mcp@example.test").save()
		self.assertIn("is disabled", str(caught.exception))

	def test_enabling_a_write_tool_warns_out_loud(self):
		self.doc(enabled=1, allow_submit_journal_entry=1).save()
		self.assertTrue(
			any(
				"submit_journal_entry" in comment.get("text", "")
				for comment in STORE.comments
				if comment.get("type") == "msgprint"
			)
		)

	def test_no_warning_when_only_read_tools_are_on(self):
		self.doc(enabled=1).save()
		self.assertFalse([c for c in STORE.comments if c.get("type") == "msgprint"])


class TokenGeneration(SeededTestCase):
	def test_generating_returns_the_token_once_and_stores_it(self):
		doc = frappe.get_doc("ERPNext MCP Settings")
		result = doc.generate_token()
		self.assertEqual(len(result["token"]), TOKEN_LENGTH)
		self.assertEqual(settings.auth_token(), result["token"])

	def test_the_stored_row_does_not_hold_the_plaintext(self):
		doc = frappe.get_doc("ERPNext MCP Settings")
		token = doc.generate_token()["token"]
		self.assertNotIn(token, json.dumps(STORE.singles["ERPNext MCP Settings"]))

	def test_the_new_token_authenticates_and_the_old_one_stops(self):
		old = self.TOKEN
		new = frappe.get_doc("ERPNext MCP Settings").generate_token()["token"]
		_, status = self.call("ping", token=new)
		self.assertEqual(status, 200)
		_, status = self.call("ping", token=old)
		self.assertEqual(status, 401)

	def test_it_stamps_when_the_token_was_generated(self):
		result = frappe.get_doc("ERPNext MCP Settings").generate_token()
		self.assertTrue(result["generated_on"])
		self.assertEqual(
			STORE.singles["ERPNext MCP Settings"]["token_generated_on"],
			result["generated_on"],
		)

	def test_it_tells_the_operator_it_cannot_be_shown_again(self):
		result = frappe.get_doc("ERPNext MCP Settings").generate_token()
		self.assertIn("cannot be shown again", result["note"])

	def test_it_requires_system_manager(self):
		frappe.local.session.user = "mcp@example.test"
		with self.assertRaises(Exception) as caught:
			frappe.get_doc("ERPNext MCP Settings").generate_token()
		self.assertIn("System Manager", str(caught.exception))

	def test_two_tokens_are_never_the_same(self):
		first = frappe.get_doc("ERPNext MCP Settings").generate_token()["token"]
		second = frappe.get_doc("ERPNext MCP Settings").generate_token()["token"]
		self.assertNotEqual(first, second)

	def test_an_undecryptable_token_fails_closed(self):
		"""A site restored without its encryption key must not become an open
		endpoint."""
		STORE.passwords.clear()
		self.assertEqual(settings.auth_token(), "")
		_, status = self.call("ping")
		self.assertEqual(status, 404)


class SelfTest(SeededTestCase):
	def test_it_reports_readiness_without_revealing_the_token(self):
		from erpnext_mcp import mcp

		report = mcp.selftest()
		self.assertTrue(report["ready"])
		self.assertTrue(report["token_configured"])
		self.assertNotIn(self.TOKEN, json.dumps(report))

	def test_it_lists_the_enabled_tools_and_flags_write_access(self):
		from erpnext_mcp import mcp

		self.configure(enabled=1, allow_create_journal_entry=1)
		report = mcp.selftest()
		# `install_compliance_fields` is here because it genuinely IS on — it is
		# the one mutating tool that ships enabled, and a diagnostic that hid that
		# would be lying about the site's write surface to the one person who
		# asked. See registry.DEFAULT_ON_MUTATING_TOOLS. The settings FORM skips it
		# in its warning banner, which is a different judgement about a different
		# audience: a banner that fires every time is a banner nobody reads.
		self.assertEqual(
			sorted(report["mutating_tools_enabled"]),
			["create_journal_entry", "install_compliance_fields"],
		)
		self.assertEqual(report["tools_total"], len(registry.TOOLS))
		# Every read tool this site can actually run, plus the two write tools that
		# are on. Computed rather than hardcoded, because what is runnable depends
		# on what is pip-installed: the three HR tools need hrms, and the five
		# geospatial ones need shapely and h3. A hardcoded number would pass on the
		# developer's bench and fail in CI, or the reverse.
		runnable = [name for name in registry.READ_TOOLS if registry.is_available(name)]
		self.assertEqual(len(report["tools_enabled"]), len(runnable) + 2)
		self.assertEqual(sorted(report["tools_unavailable"]), sorted(self.expected_unavailable()))

	def expected_unavailable(self) -> list:
		"""Exactly which tools this fixture site cannot run, and why each one."""
		out = list(HR_TOOLS)
		if not geo.available():
			out += list(GEO_TOOLS)
		if not form_pdf_renderer.available():
			out += list(PDF_TOOLS)
		if not i9_pdf.available():
			out += list(I9_PDF_TOOLS)
		if not w4_pdf.available():
			out += list(W4_PDF_TOOLS)
		# v0.68.0. This fixture is a plain `SeededTestCase` site: it never
		# registers the Purchase Invoice doctype (only `PurchasingTestCase`
		# does, for the tests that need to insert one), so the one tool that
		# gates on it being present is correctly unavailable here.
		out.append("create_purchase_invoice_from_receipt")
		return out

	def test_it_reports_not_ready_when_disabled(self):
		from erpnext_mcp import mcp

		self.configure(enabled=0)
		self.assertFalse(mcp.selftest()["ready"])

	def test_it_names_the_endpoint_the_docs_promise(self):
		from erpnext_mcp import mcp

		self.assertEqual(mcp.selftest()["endpoint"], "/api/method/erpnext_mcp.mcp.handle")

	def test_it_requires_system_manager(self):
		from erpnext_mcp import mcp

		frappe.local.session.user = "mcp@example.test"
		with self.assertRaises(Exception):
			mcp.selftest()
