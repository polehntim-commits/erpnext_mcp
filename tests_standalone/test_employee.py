# SPDX-License-Identifier: MIT
"""The personnel register — v0.18.1's three tools, and the gap they close.

THE CLAIM BEHIND THE WHOLE RELEASE is that a mobile credential which enrols
perfectly and then shows an empty screen is a bug in this app and not in the
phone. v0.18.0 could create the User, the role, the entity scoping, the Mobile
Access Grant, the credential and the QR — six things — and could not create or
edit the ONE record every Farm Ops method scopes work by. `list_my_tasks` refused
every account, correctly, and there was no way to fix it from here.

SIX CLAIMS.

1. `CreatingTheRecord` — the fourteen fields go in, the defaults fill themselves,
   and what this site's schema does not have is REPORTED rather than silently
   dropped. A tool that swallowed a value it never wrote would report success for
   nothing having happened.

2. `TheSchemaIsTheArbiter` — every Link is checked against this site's own
   records, every Select against this site's own options, and both refusals list
   what is actually available. A caller that is a language model can act on
   "Known Department: Administration, Operations" and cannot act on a controller
   traceback.

3. `WhatItRefusesToWrite` — payroll, tax and banking fields get their own
   refusal, and a real-but-not-writable field gets a different one from a field
   that does not exist. Those are different mistakes and deserve different
   sentences.

4. `TheGuards` — the role gate and the company scope, both on the principal this
   app acts as. Creating an Employee for an entity you cannot see would put a
   person on a payroll register you cannot read.

5. `Linking` — one person, one login, in every direction: refused when the User
   belongs to somebody else, refused when the Employee does, a NO-OP when the link
   already says what was asked for, and reporting whether the PHONE WILL NOW WORK
   rather than merely whether the field was written.

6. `OnboardingEndToEnd` — the orchestrator produces the Employee, the grant, the
   link and the QR in one call, in the only order that works, and a second run
   with the same arguments duplicates none of them.
"""

import json

import frappe

from erpnext_mcp import compat, roles
from erpnext_mcp.errors import ToolError
from erpnext_mcp.tools import employee as employee_tool

from .fixtures import MAIN, OTHER, V12TestCase, install_hrms
from .harness import ROLES, STORE, set_roles

FUNNEL = "https://umbrel.tail4a2b.ts.net"

ON = {
	f"allow_{name}": 1
	for name in (
		"create_employee",
		"get_employee",
		"update_employee",
		"link_employee_to_user",
		"onboard_employee",
		"create_mobile_user",
		"generate_mobile_login_qr",
		"list_employees",
		"attach_file_to_document",
		"create_farm_task",
	)
}

WORKER = "ana@example.test"
STRANGER = "nobody@example.test"


class EmployeeTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, public_url=FUNNEL, **ON)
		# The double ships without an Employee register — Frappe HR is a separate
		# app, which is the whole reason these tools have an availability
		# predicate — and `install_hrms` also seeds the Department, Designation,
		# Employment Type and Gender masters the Employee's Links point at.
		install_hrms()
		self._roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(self._restore_roles)
		roles.install_roles()

	def _restore_roles(self):
		ROLES.clear()
		ROLES.update(self._roles_before)

	# -- helpers -------------------------------------------------------------
	def create(self, **overrides):
		payload = {"employee_name": "Ana Ramos", "company": MAIN}
		payload.update(overrides)
		return self.tool_data("create_employee", payload)

	def create_error(self, **overrides):
		payload = {"employee_name": "Ana Ramos", "company": MAIN}
		payload.update(overrides)
		return self.tool_error("create_employee", payload)

	def enrolled(self, email=WORKER, name="Ana Ramos", entities=None):
		"""A login with a Farm Ops role and an Active Mobile Access Grant."""
		return self.tool_data(
			"create_mobile_user",
			{
				"email": email,
				"full_name": name,
				"role": "Field Worker",
				"entity_access": entities or [MAIN],
			},
		)

	def plain_user(self, email=STRANGER, name="Nobody At All"):
		"""A User with no Farm Ops role and no grant. Nothing to link to."""
		STORE.seed("User", [{"name": email, "enabled": 1, "full_name": name, "user_type": "System User"}])
		return email

	def scope_actor_to(self, company):
		"""Give the principal this app acts as a Company User Permission.

		Frappe's rule is that NO permission means unrestricted, so a scope test has
		to add one before it can test anything — which is also why
		`require_company_scope` is written the way it is.
		"""
		STORE.seed(
			"User Permission",
			[
				{
					"name": f"UP-SCOPE-{company}",
					"user": "Administrator",
					"allow": "Company",
					"for_value": company,
					"apply_to_all_doctypes": 1,
					"is_default": 1,
				}
			],
		)


# ── 1 ───────────────────────────────────────────────────────────────────────
class CreatingTheRecord(EmployeeTestCase):
	def test_every_field_it_takes_is_written(self):
		data = self.create(
			first_name="Ana",
			last_name="Ramos",
			date_of_joining="2026-07-01",
			date_of_birth="1994-02-17",
			gender="Female",
			department="Operations",
			designation="Picker",
			employment_type="Seasonal Worker",
			status="Active",
			personal_email="ana.ramos@example.test",
			cell_number="+1 509 555 0142",
		)
		row = frappe.db.get_value(
			"Employee",
			data["employee"],
			[
				"employee_name",
				"first_name",
				"last_name",
				"company",
				"date_of_joining",
				"date_of_birth",
				"gender",
				"department",
				"designation",
				"employment_type",
				"status",
				"personal_email",
				"cell_number",
			],
			as_dict=True,
		)
		self.assertEqual(row["employee_name"], "Ana Ramos")
		self.assertEqual(row["company"], MAIN)
		self.assertEqual(row["date_of_joining"], "2026-07-01")
		self.assertEqual(row["date_of_birth"], "1994-02-17")
		self.assertEqual(row["gender"], "Female")
		self.assertEqual(row["department"], "Operations")
		self.assertEqual(row["designation"], "Picker")
		self.assertEqual(row["employment_type"], "Seasonal Worker")
		self.assertEqual(row["status"], "Active")
		self.assertEqual(row["personal_email"], "ana.ramos@example.test")
		self.assertEqual(row["cell_number"], "+1 509 555 0142")

	def test_the_minimum_is_a_name_and_a_company(self):
		data = self.create()
		row = frappe.db.get_value(
			"Employee",
			data["employee"],
			["first_name", "last_name", "status", "date_of_joining"],
			as_dict=True,
		)
		self.assertEqual(row["first_name"], "Ana")
		self.assertEqual(row["last_name"], "Ramos")
		self.assertEqual(row["status"], "Active")
		self.assertEqual(row["date_of_joining"], frappe.utils.today())

	def test_it_names_which_defaults_it_applied(self):
		"""A caller has to be able to tell what it chose from what they chose."""
		data = self.create()
		self.assertEqual(
			sorted(data["defaults_applied"]),
			["date_of_joining", "first_name", "last_name", "status"],
		)

	def test_a_name_given_explicitly_is_not_reported_as_a_default(self):
		data = self.create(first_name="Anastasia", status="Inactive")
		self.assertNotIn("first_name", data["defaults_applied"])
		self.assertNotIn("status", data["defaults_applied"])
		self.assertEqual(frappe.db.get_value("Employee", data["employee"], "first_name"), "Anastasia")

	def test_a_three_part_name_splits_at_the_ends(self):
		data = self.create(employee_name="Ana Maria Ramos")
		row = frappe.db.get_value("Employee", data["employee"], ["first_name", "last_name"], as_dict=True)
		self.assertEqual(row["first_name"], "Ana")
		self.assertEqual(row["last_name"], "Ramos")

	def test_a_one_word_name_is_refused(self):
		"""An I-9, a payroll register and a dispatch board all name the same
		person, and one word names nobody findable."""
		self.assertIn("names nobody findable", self.create_error(employee_name="Ana"))

	def test_a_field_this_site_does_not_have_is_reported_not_swallowed(self):
		"""A tool that quietly dropped a value would report success for nothing
		having happened."""
		from .harness import META

		meta = META["Employee"]
		removed = meta._by_name.pop("cell_number")
		meta.fields = [field for field in meta.fields if field["fieldname"] != "cell_number"]
		try:
			data = self.create(cell_number="+1 509 555 0142")
		finally:
			meta.add(removed)
		self.assertIn("cell_number", data["fields_not_on_this_site"])
		self.assertIn("cell_number", data["note"])

	def test_a_second_record_for_the_same_person_at_the_same_company_is_refused(self):
		"""Two Employee records for one person puts them on the dispatch board
		twice and in the payroll register once."""
		first = self.create()
		message = self.create_error()
		self.assertIn(first["employee"], message)
		self.assertIn("dispatch board twice", message)

	def test_two_real_people_with_one_name_are_allowed_when_said_so(self):
		first = self.create()
		second = self.create(allow_duplicate_name=True)
		self.assertNotEqual(first["employee"], second["employee"])

	def test_the_same_name_at_a_different_entity_is_a_second_record_on_purpose(self):
		"""One person genuinely can be employed by two entities."""
		self.create()
		second = self.create(company=OTHER)
		self.assertTrue(second["employee"])

	def test_it_reports_the_docname_and_what_it_set(self):
		data = self.create(designation="Picker")
		self.assertTrue(data["employee"])
		self.assertEqual(data["fields_set"]["designation"], "Picker")
		self.assertEqual(data["fields_set"]["company"], MAIN)

	def test_a_mandatory_field_this_site_wants_is_named_rather_than_thrown(self):
		"""Stock Frappe HR marks `gender` and `date_of_birth` mandatory and plenty
		of operators do not. Which fields are required is the site's decision, so
		the refusal reads it off the meta instead of carrying its own list."""
		from .harness import META

		field = META["Employee"].get_field("gender")
		field["reqd"] = 1
		try:
			message = self.create_error()
		finally:
			field["reqd"] = 0
		self.assertIn("gender", message)
		self.assertIn("mandatory", message)
		self.assertIn("Nothing was created", message)


# ── 2 ───────────────────────────────────────────────────────────────────────
class TheSchemaIsTheArbiter(EmployeeTestCase):
	def test_a_department_that_does_not_exist_is_refused_with_the_ones_that_do(self):
		message = self.create_error(department="Packing Line")
		self.assertIn("Packing Line", message)
		self.assertIn("Operations", message)

	def test_a_designation_that_does_not_exist_is_refused(self):
		self.assertIn("Combine Driver", self.create_error(designation="Combine Driver"))

	def test_an_employment_type_that_does_not_exist_is_refused(self):
		message = self.create_error(employment_type="Indentured")
		self.assertIn("Indentured", message)
		self.assertIn("Seasonal Worker", message)

	def test_a_gender_that_is_not_a_record_on_this_site_is_refused(self):
		message = self.create_error(gender="Yes")
		self.assertIn("Gender", message)

	def test_a_status_outside_the_sites_own_options_is_refused_with_them(self):
		message = self.create_error(status="Probationary")
		self.assertIn("Probationary", message)
		self.assertIn("Active", message)
		self.assertIn("Suspended", message)

	def test_a_status_is_matched_case_insensitively_and_stored_in_the_sites_casing(self):
		"""What is stored has to match what a list-view filter looks for."""
		data = self.create(status="active")
		self.assertEqual(frappe.db.get_value("Employee", data["employee"], "status"), "Active")

	def test_a_date_that_is_not_a_date_is_refused(self):
		self.assertIn("date_of_joining", self.create_error(date_of_joining="last Tuesday"))

	def test_a_link_whose_target_doctype_is_absent_is_not_validated(self):
		"""The value cannot be checked against a schema that is not here, and
		refusing would refuse a good value on a site that never installed HR's
		masters. Frappe does not validate that link either."""
		from .harness import INSTALLED_DOCTYPES

		INSTALLED_DOCTYPES.discard("Designation")
		try:
			data = self.create(designation="Combine Driver")
		finally:
			INSTALLED_DOCTYPES.add("Designation")
		self.assertEqual(data["fields_set"]["designation"], "Combine Driver")

	def test_a_site_with_no_records_of_that_master_says_so(self):
		"""A site that has the doctype and has never made a record of it. The
		refusal has to say that rather than list nothing."""
		STORE.tables["Department"] = {}
		self.assertIn("no Department records at all", self.create_error(department="Operations"))


# ── 2b ──────────────────────────────────────────────────────────────────────
class TheFieldsThisAppMadeMandatory(EmployeeTestCase):
	"""v0.46.1. The wall the iOS wizard hit on step one, and whose it was.

	`compliance_fields.py` installs `i9_status`, `w4_status` and `jurisdiction` on
	Employee with `reqd=True`. `_mandatory_gaps` then refused every create that did
	not supply them — the MCP tool, `onboard_employee` and the wizard alike — with
	a message blaming "this site's Frappe HR", which had nothing to do with it.
	This app required the fields, so this app has to have an answer for them.
	"""

	def setUp(self):
		super().setUp()
		self.install_compliance_fields()

	def install_compliance_fields(self):
		"""The three Employee columns as `compliance_fields.py` really leaves them.

		Built from that module's own specs rather than restated here, so a fieldtype,
		an option or a `reqd` flag that changes there cannot pass this suite unnoticed.
		"""
		from erpnext_mcp import compliance_fields

		from .harness import add_field

		for spec in compliance_fields.targets_by_doctype()["Employee"].fields:
			add_field(
				"Employee",
				spec.fieldname,
				fieldtype=spec.fieldtype,
				options=spec.options or None,
				label=spec.label,
				reqd=1 if spec.reqd else 0,
			)

	def test_a_hire_that_supplies_none_of_them_is_created_rather_than_refused(self):
		"""The bug itself: nothing the wizard sends names these three, and step one
		404'd its way to a 'mandatory' refusal instead of a person."""
		data = self.create()
		self.assertTrue(data["employee"])
		self.assertEqual(data["fields_set"]["i9_status"], "Pending")
		self.assertEqual(data["fields_set"]["w4_status"], "Missing")
		self.assertEqual(data["fields_set"]["jurisdiction"], "OR")

	def test_every_default_it_invented_is_named_in_the_result(self):
		"""A record that quietly acquired an I-9 status is the record nobody goes
		back to fix."""
		data = self.create()
		self.assertEqual(
			sorted(set(data["defaults_applied"]) & {"i9_status", "w4_status", "jurisdiction"}),
			["i9_status", "jurisdiction", "w4_status"],
		)
		self.assertIn("i9_status=Pending", data["note"])
		self.assertIn("w4_status=Missing", data["note"])
		self.assertIn("starting values, not findings", data["note"])

	def test_what_the_caller_passed_wins_and_is_not_reported_as_a_default(self):
		data = self.create(i9_status="Verified", w4_status="On-File", jurisdiction="WA")
		self.assertEqual(data["fields_set"]["i9_status"], "Verified")
		self.assertEqual(data["fields_set"]["w4_status"], "On-File")
		self.assertEqual(data["fields_set"]["jurisdiction"], "WA")
		self.assertNotIn("i9_status", data["defaults_applied"])
		self.assertNotIn("w4_status", data["defaults_applied"])
		self.assertNotIn("jurisdiction", data["defaults_applied"])
		self.assertNotIn("i9_status=", data["note"])

	def test_a_status_this_site_does_not_offer_is_still_refused(self):
		"""They are on the allowlist, not exempt from it. `_clean` checks a Select
		against the site's own options exactly as it does for `status`."""
		message = self.create_error(i9_status="Probationary")
		self.assertIn("Probationary", message)
		self.assertIn("Verified", message)

	def test_every_default_is_an_option_the_installer_actually_offers(self):
		"""The drift guard, and the reason `w4_status` defaults to Missing rather
		than the Pending somebody will reach for by analogy with `i9_status`: that
		field's three options are On-File, Missing and Requires-Update, and Pending
		is not among them."""
		from erpnext_mcp import compliance_fields
		from erpnext_mcp.args import select_options

		specs = {spec.fieldname: spec for spec in compliance_fields.targets_by_doctype()["Employee"].fields}
		for fieldname, default in employee_tool.COMPLIANCE_DEFAULTS.items():
			self.assertIn(default, select_options("Employee", fieldname))
			self.assertIn(default, specs[fieldname].options.split("\n"))

	def test_the_three_are_exactly_the_three_the_installer_marks_required(self):
		"""Read off `compliance_fields.py` rather than agreed by hand. A fourth
		field marked `reqd` there and not defaulted here would rebuild the wall."""
		from erpnext_mcp import compliance_fields

		required = tuple(
			spec.fieldname for spec in compliance_fields.targets_by_doctype()["Employee"].fields if spec.reqd
		)
		self.assertEqual(required, employee_tool.COMPLIANCE_FIELDS)

	def test_a_field_this_site_does_not_carry_gets_no_default(self):
		"""`_supported` would drop it a moment later, and a default that is
		silently discarded is worse than no default at all."""
		from .harness import META

		META["Employee"].fields = [
			field for field in META["Employee"].fields if field.fieldname != "jurisdiction"
		]
		META["Employee"]._by_name.pop("jurisdiction", None)
		data = self.create()
		self.assertNotIn("jurisdiction", data["fields_set"])
		self.assertNotIn("jurisdiction", data["defaults_applied"])

	def address_for(self, company, state):
		"""ERPNext's address linkage, which is an Address joined through a Dynamic
		Link rather than a field on the Company. Registered per test — neither
		doctype is in the standalone double, which is itself a real configuration:
		a Frappe site that never installed ERPNext's address module."""
		from .harness import register_doctype

		register_doctype("Address", [{"fieldname": name} for name in ("name", "state", "address_title")])
		register_doctype(
			"Dynamic Link",
			[{"fieldname": name} for name in ("name", "parent", "parenttype", "link_doctype", "link_name")],
		)
		STORE.seed("Address", [{"name": "ADDR-1", "state": state, "address_title": company}])
		STORE.seed(
			"Dynamic Link",
			[
				{
					"name": "DL-1",
					"parent": "ADDR-1",
					"parenttype": "Address",
					"link_doctype": "Company",
					"link_name": company,
				}
			],
		)

	def test_the_jurisdiction_follows_the_hiring_entitys_own_address(self):
		"""Wage law follows where the work is, and the entity's address is the
		closest a site gets to knowing that at hire time. An entity in Washington
		must not start under ORS 653 — the two states differ on agricultural
		overtime, on rest breaks and on minimum wage regions."""
		self.address_for(MAIN, "Washington")
		self.assertEqual(self.create()["fields_set"]["jurisdiction"], "WA")

	def test_a_two_letter_state_is_taken_as_written(self):
		"""ERPNext stores whichever form the operator typed."""
		self.address_for(MAIN, "id")
		self.assertEqual(self.create()["fields_set"]["jurisdiction"], "ID")

	def test_an_address_that_names_no_state_falls_back_rather_than_writing_blank(self):
		"""A blank is what the create was refusing on in the first place."""
		self.address_for(MAIN, "")
		self.assertEqual(self.create()["fields_set"]["jurisdiction"], "OR")

	def test_a_site_with_no_address_schema_at_all_still_hires(self):
		"""The standalone double has neither Address nor Dynamic Link, which is
		also a real Frappe site that never installed ERPNext's address module. The
		fallback is a value an operator can see and correct, not an exception."""
		self.assertEqual(self.create()["fields_set"]["jurisdiction"], "OR")

	def test_a_field_the_operator_made_mandatory_is_still_refused(self):
		"""The safety net is not removed, and this is the line it holds. Nobody can
		default a date of birth, and inventing one would be worse than the refusal."""
		from .harness import META

		field = META["Employee"].get_field("date_of_birth")
		field["reqd"] = 1
		try:
			message = self.create_error()
		finally:
			field["reqd"] = 0
		self.assertIn("date_of_birth", message)
		self.assertIn("Frappe HR", message)
		self.assertIn("Nothing was created", message)

	def test_the_refusal_no_longer_blames_frappe_hr_for_this_apps_own_field(self):
		"""The sentence the wizard actually got. `jurisdiction` is a Custom Field
		erpnext_mcp installs; sending the caller to argue with their operator about
		it was this app pointing at itself."""
		message = employee_tool._mandatory_message(["jurisdiction"])
		self.assertIn("erpnext_mcp installs jurisdiction", message)
		self.assertNotIn("Frappe HR", message)
		both = employee_tool._mandatory_message(["gender", "jurisdiction"])
		self.assertIn("Frappe HR marks gender", both)
		self.assertIn("erpnext_mcp installs jurisdiction", both)

	def test_update_employee_is_how_a_pending_i9_becomes_a_verified_one(self):
		"""They are on `WRITABLE`, so the field a hire starts at Pending is the
		field the I-9 step can move without leaving this app."""
		created = self.create()
		data = self.tool_data(
			"update_employee", {"name": created["employee"], "i9_status": "Verified", "jurisdiction": "WA"}
		)
		changed = {entry["field"]: entry for entry in data["changed"]}
		self.assertEqual(changed["i9_status"]["to"], "Verified")
		self.assertEqual(changed["jurisdiction"]["to"], "WA")


# ── 2b ──────────────────────────────────────────────────────────────────────
class ReadingOneRecord(EmployeeTestCase):
	"""v0.46.2. `get_employee`, and the disagreement it exists to settle.

	THE ENDPOINT IS FOR THE RETURNING SEASONAL WORKER, which in tree fruit is the
	common case rather than the exception: the same pickers come back each June,
	and the wizard has to know which of its five steps they have already done.

	It cannot know that from `Employee.i9_status` and `Employee.w4_status` alone,
	and this class is where that is pinned down. Those two columns are Custom
	Fields THIS APP installs; `create_employee` sets them to Pending/Missing and
	nothing in the app ever moves them again — `submit_i9_section_2` writes
	`I-9 Form.status` and `submit_w4` writes `W-4 Form.status`, each on its own
	doctype. So the column and the record disagree for every worker who has ever
	completed a form, and `EmployeeDetail.satisfiedSteps` on the handset branches
	on the COLUMN.

	The reconciliation is therefore load-bearing, and so is its one limit: a live
	Complete record fills a column still at its hire-time default and NOTHING
	else. `Expired` is the case §1324a actually cares about, and the form that
	says Complete is the very one that expired.
	"""

	def setUp(self):
		super().setUp()
		from erpnext_mcp import compliance_fields

		from .harness import add_field

		for spec in compliance_fields.targets_by_doctype()["Employee"].fields:
			add_field(
				"Employee",
				spec.fieldname,
				fieldtype=spec.fieldtype,
				options=spec.options or None,
				label=spec.label,
				reqd=1 if spec.reqd else 0,
			)

	def documented(self, employee, i9="Complete", w4="Active"):
		"""The records a worker who did a season leaves behind."""
		STORE.seed(
			"I-9 Form",
			[
				{
					"name": "I9-LAST-SEASON",
					"employee": employee,
					"company": MAIN,
					"status": i9,
					"hire_date": "2025-06-02",
				}
			],
		)
		STORE.seed(
			"W-4 Form",
			[
				{
					"name": "W4-LAST-SEASON",
					"employee": employee,
					"company": MAIN,
					"status": w4,
					"tax_year": "2025",
					"effective_date": "2025-06-02",
				}
			],
		)

	def test_it_reads_the_identity_and_assignment_facts_off_the_record(self):
		created = self.create(
			first_name="Ana",
			last_name="Ramos",
			date_of_birth="1990-05-04",
			gender="Female",
			designation="Picker",
			employment_type="Seasonal Worker",
			cell_number="555-0100",
		)
		data = self.tool_data("get_employee", {"employee": created["employee"]})
		self.assertEqual(data["name"], created["employee"])
		self.assertEqual(data["employee_name"], "Ana Ramos")
		self.assertEqual(data["first_name"], "Ana")
		self.assertEqual(data["date_of_birth"], "1990-05-04")
		self.assertEqual(data["gender"], "Female")
		self.assertEqual(data["designation"], "Picker")
		self.assertEqual(data["company"], MAIN)

	def test_a_hire_with_no_forms_reads_exactly_what_create_employee_wrote(self):
		"""Nothing to reconcile against, so nothing is reconciled — a new hire
		genuinely does need all five steps."""
		created = self.create()
		data = self.tool_data("get_employee", {"employee": created["employee"]})
		self.assertEqual(data["i9_status"], "Pending")
		self.assertEqual(data["w4_status"], "Missing")
		self.assertFalse(data["i9_on_file"])
		self.assertFalse(data["w4_on_file"])
		self.assertIsNone(data["i9"])
		self.assertIsNone(data["w4"])
		self.assertEqual(data["reconciled"], [])

	def test_a_completed_form_fills_the_column_nobody_ever_wrote_back_to(self):
		"""THE BUG THIS ENDPOINT WOULD HAVE SHIPPED. The columns still read
		Pending/Missing because nothing in the app moves them; handing those over
		raw takes a fully documented picker through a fresh I-9 and a fresh W-4."""
		created = self.create()
		self.documented(created["employee"])
		data = self.tool_data("get_employee", {"employee": created["employee"]})

		self.assertEqual(data["i9_status"], "Verified")
		self.assertEqual(data["w4_status"], "On-File")
		self.assertEqual(sorted(data["reconciled"]), ["i9_status", "w4_status"])
		# And the stored values are still on the wire, because an operator and an
		# alert rule reading the column will disagree with this and need to see why.
		self.assertEqual(data["i9_status_recorded"], "Pending")
		self.assertEqual(data["w4_status_recorded"], "Missing")
		self.assertEqual(data["i9"]["name"], "I9-LAST-SEASON")
		self.assertEqual(data["w4"]["status"], "Active")

	def test_an_expired_i9_stands_against_a_complete_record(self):
		"""The limit of the reconciliation, and the one with a statute behind it."""
		created = self.create()
		self.documented(created["employee"])
		self.tool_data("update_employee", {"name": created["employee"], "i9_status": "Expired"})

		data = self.tool_data("get_employee", {"employee": created["employee"]})
		self.assertEqual(data["i9_status"], "Expired")
		self.assertNotIn("i9_status", data["reconciled"])
		self.assertTrue(data["i9_on_file"], "the record is still reported — it is the column that wins")

	def test_a_requires_update_w4_stands_too(self):
		created = self.create()
		self.documented(created["employee"])
		self.tool_data("update_employee", {"name": created["employee"], "w4_status": "Requires-Update"})
		data = self.tool_data("get_employee", {"employee": created["employee"]})
		self.assertEqual(data["w4_status"], "Requires-Update")
		self.assertNotIn("w4_status", data["reconciled"])
		# The I-9 beside it is still reconciled — one refusal is not a blanket one.
		self.assertEqual(data["i9_status"], "Verified")

	def test_a_draft_form_is_not_a_completed_one(self):
		created = self.create()
		self.documented(created["employee"], i9="Section 1 Complete", w4="Draft")
		data = self.tool_data("get_employee", {"employee": created["employee"]})
		self.assertEqual(data["i9_status"], "Pending")
		self.assertEqual(data["w4_status"], "Missing")
		self.assertEqual(data["reconciled"], [])

	def test_the_badge_comes_off_the_map_and_only_while_it_is_active(self):
		created = self.create()
		STORE.seed(
			"Bucket Log Badge Map",
			[
				{
					"name": "BADGE-9",
					"badge_id": "BADGE-9",
					"employee": created["employee"],
					"company": MAIN,
					"active": 1,
				}
			],
		)
		self.assertEqual(
			self.tool_data("get_employee", {"employee": created["employee"]})["badge_id"], "BADGE-9"
		)

		frappe.db.set_value("Bucket Log Badge Map", "BADGE-9", "active", 0)
		self.assertIsNone(self.tool_data("get_employee", {"employee": created["employee"]})["badge_id"])

	def test_the_badge_doctype_is_the_one_the_badge_tool_actually_writes(self):
		"""`tools/employee.py` names the doctype rather than importing it, so that
		the personnel register does not depend on the bucket queue. This is the
		assertion that keeps the two spellings the same string."""
		from erpnext_mcp.tools import bucket_log

		self.assertEqual(employee_tool.BADGE_MAP, bucket_log.BADGE_DOCTYPE)

	def test_it_resolves_the_four_things_somebody_calls_the_employee(self):
		created = self.create()
		# Not through `create_employee` — `employee_number` is outside the seventeen
		# it writes on purpose. `resolve_employee` still has to find a record by it.
		frappe.db.set_value("Employee", created["employee"], "employee_number", "1042")
		for spelling in (created["employee"], "1042", "Ana Ramos"):
			with self.subTest(spelling=spelling):
				self.assertEqual(
					self.tool_data("get_employee", {"employee": spelling})["name"], created["employee"]
				)

	def test_it_wants_the_hr_role_like_everything_else_in_this_file(self):
		created = self.create()
		set_roles("Administrator", ["Accounts User"])
		self.assertIn(
			"may not change the personnel register",
			self.tool_error("get_employee", {"employee": created["employee"]}),
		)

	def test_it_refuses_an_employee_of_an_entity_this_principal_cannot_see(self):
		created = self.create()
		self.scope_actor_to(OTHER)
		self.assertIn(
			"has no access to company",
			self.tool_error("get_employee", {"employee": created["employee"]}),
		)


# ── 3 ───────────────────────────────────────────────────────────────────────
class WhatItRefusesToWrite(EmployeeTestCase):
	def test_a_payroll_field_is_refused_with_its_own_sentence(self):
		"""A salary structure has a form, an approval and a retention rule this
		app knows nothing about."""
		message = self.create_error(ctc=48000)
		self.assertIn("payroll, tax or banking", message)
		self.assertIn("Desk", message)

	def test_a_bank_account_number_is_refused(self):
		self.assertIn("payroll, tax or banking", self.create_error(bank_ac_no="123456789"))

	def test_an_income_tax_slab_is_refused(self):
		self.assertIn("payroll, tax or banking", self.create_error(income_tax_slab="Slab A"))

	def test_a_real_employee_field_outside_the_twenty_is_refused_differently(self):
		"""'Real but not mine' and 'not a field at all' are different mistakes.

		THIS EXAMPLE HAS NOW MOVED TWICE, and both moves are the same story: it
		was `branch` until v0.54.0 and `reports_to` until v0.68.1, and each time
		the field stopped being an example because this app started writing it.
		`relieving_date` takes the place and is the better one for the long run —
		it is a real column on this site's Employee, and the date somebody left
		is the OUTPUT of a process (a final Attendance, a last payroll, a revoked
		credential) rather than a value a hiring form types in, which is why it
		is not on the list and is not likely to join it."""
		message = self.create_error(relieving_date="2025-01-31")
		self.assertIn("is not one this tool writes", message)
		self.assertIn("employee_name", message)

	def test_a_field_that_is_not_on_the_doctype_at_all_is_refused_by_name(self):
		message = self.create_error(favourite_colour="green")
		self.assertIn("favourite_colour", message)
		self.assertIn("not a field on this site's Employee doctype", message)

	def test_none_of_the_refusals_create_anything(self):
		before = len(STORE.rows("Employee"))
		for payload in ({"ctc": 1}, {"relieving_date": "x"}, {"nonsense": "y"}, {"employee_name": "Ana"}):
			self.create_error(**payload)
		self.assertEqual(len(STORE.rows("Employee")), before)


# ── 4 ───────────────────────────────────────────────────────────────────────
class TheGuards(EmployeeTestCase):
	def test_a_principal_with_no_hiring_role_may_not_hire(self):
		"""v0.94.0 moved this gate from `HR_ROLES` to `HIRING_ROLES` and this is
		the assertion that the gate still EXISTS. Accounts User is on neither
		list, so it is refused by both — which is what makes it the right control
		for a widening: it fails the same way before and after."""
		set_roles("Administrator", ["Accounts User"])
		message = self.create_error()
		self.assertIn("may not bring a person onto the farm", message)
		self.assertIn("Farm Manager", message)
		self.assertIn("Foreman", message)

	def test_a_foreman_may_now_hire_and_that_is_the_point_of_the_release(self):
		"""THE WIDENING, ASSERTED POSITIVELY. Most of v0.94.0 opens paths rather
		than closing them, so the failure mode this suite has to catch is "the
		foreman is still refused" — a test that only checked refusals would pass
		on a release that did nothing."""
		set_roles("Administrator", ["Foreman"])
		self.assertTrue(self.create()["employee"])

	def test_a_crew_leader_may_hire_too(self):
		"""`HIRING_ROLES` is `SHIFT_ROLES`, which carries both supervisor roles.
		Asserted separately from the Foreman because the two coincide BY POLICY —
		the day somebody splits them, exactly one of these two goes red."""
		set_roles("Administrator", ["Crew Leader"])
		self.assertTrue(self.create()["employee"])

	def test_a_field_worker_still_may_not_hire(self):
		"""The negative control for the two above. A picker is on neither list,
		and 'the gate widened' must not be readable as 'the gate dissolved'."""
		set_roles("Administrator", ["Field Worker"])
		message = self.create_error()
		self.assertIn("may not bring a person onto the farm", message)

	def test_the_register_itself_did_not_widen_with_the_hire(self):
		"""THE BOUNDARY MOVED RATHER THAN DISSOLVING, which is the single claim
		this release most needs held down. A Foreman may create an Employee and
		may NOT edit one that exists — `update_employee` keeps `require_hr_role`
		because a department, a supervisor, a birth date and a login on somebody
		already hired are that person's PII rather than a hire."""
		set_roles("Administrator", ["Farm Manager"])
		created = self.create()["employee"]
		set_roles("Administrator", ["Foreman"])
		with self.assertRaises(ToolError) as caught:
			employee_tool.update_employee({"employee": created, "department": "Harvest"})
		self.assertIn("may not change the personnel register", str(caught.exception))

	def test_but_a_foreman_may_rehire_the_person_they_hired_last_season(self):
		"""The one narrow exception, and why it is a separate tool rather than a
		hole in `update_employee`: refusing here pushes the field toward a
		DUPLICATE Employee, two I-9s and a tenure history that starts over."""
		set_roles("Administrator", ["Farm Manager"])
		created = self.create()["employee"]
		set_roles("Administrator", ["Foreman"])
		result = employee_tool.reactivate_employee({"employee": created})
		self.assertEqual(frappe.db.get_value("Employee", created, "status"), "Active")
		self.assertTrue(result.data["changed"] or result.data["unchanged"])

	def test_the_rehire_door_carries_no_other_field(self):
		"""What stops the narrow exception being a wide one. `reactivate_employee`
		takes no writable arguments at all, so a caller holding only a hiring role
		cannot reach the department or the login THROUGH it — the refusal is by
		absence of an argument, not by a second check that could be forgotten."""
		set_roles("Administrator", ["Farm Manager"])
		created = self.create()["employee"]
		set_roles("Administrator", ["Foreman"])
		with self.assertRaises(ToolError) as caught:
			employee_tool.reactivate_employee({"employee": created, "department": "Harvest"})
		self.assertIn("department", str(caught.exception))
		self.assertEqual(frappe.db.get_value("Employee", created, "department"), None)

	def test_the_refusal_names_the_account_so_the_fix_is_one_line(self):
		"""Permission denied on a principal the operator chose themselves is a
		one-line fix they cannot make without knowing which line."""
		set_roles("Administrator", [])
		message = self.create_error()
		self.assertIn("Administrator", message)
		self.assertIn("mcp_system_user", message)

	def test_a_farm_manager_may_hire(self):
		set_roles("Administrator", ["Farm Manager"])
		self.assertTrue(self.create()["employee"])

	def test_an_hr_user_may_hire(self):
		set_roles("Administrator", ["HR User"])
		self.assertTrue(self.create()["employee"])

	def test_a_company_the_caller_cannot_see_is_refused(self):
		"""Creating an Employee for an entity you cannot see would put a person on
		a payroll register you cannot read."""
		self.scope_actor_to(OTHER)
		message = self.create_error(company=MAIN)
		self.assertIn(MAIN, message)
		self.assertIn("no access to company", message)

	def test_the_company_the_caller_can_see_still_works(self):
		self.scope_actor_to(OTHER)
		self.assertTrue(self.create(company=OTHER)["employee"])

	def test_no_user_permission_at_all_means_unrestricted_as_frappe_says(self):
		"""api/guard.py inverts this for the mobile surface on purpose. A personnel
		tool that refused every correctly-configured operator would be switched off
		within the hour."""
		self.assertEqual(roles.companies_for("Administrator"), [])
		self.assertTrue(self.create()["employee"])

	def test_the_refused_company_creates_nothing(self):
		self.scope_actor_to(OTHER)
		before = len(STORE.rows("Employee"))
		self.create_error(company=MAIN)
		self.assertEqual(len(STORE.rows("Employee")), before)


# ── 5 ───────────────────────────────────────────────────────────────────────
class Updating(EmployeeTestCase):
	def setUp(self):
		super().setUp()
		self.employee = self.create()["employee"]

	def change(self, **fields):
		return self.tool_data("update_employee", {"name": self.employee, **fields})

	def change_error(self, **fields):
		return self.tool_error("update_employee", {"name": self.employee, **fields})

	def test_it_changes_what_it_was_asked_to(self):
		self.change(designation="Picker", department="Operations")
		row = frappe.db.get_value("Employee", self.employee, ["designation", "department"], as_dict=True)
		self.assertEqual(row["designation"], "Picker")
		self.assertEqual(row["department"], "Operations")

	def test_it_changes_nothing_it_was_not_asked_to(self):
		before = dict(frappe.db.get_value("Employee", self.employee, ["status", "company"], as_dict=True))
		self.change(designation="Picker")
		after = dict(frappe.db.get_value("Employee", self.employee, ["status", "company"], as_dict=True))
		self.assertEqual(before, after)

	def test_it_reports_each_change_with_the_previous_value(self):
		data = self.change(status="Suspended")
		entry = next(row for row in data["changed"] if row["field"] == "status")
		self.assertEqual(entry["from"], "Active")
		self.assertEqual(entry["to"], "Suspended")

	def test_a_value_that_is_already_what_you_asked_for_is_not_reported_as_a_write(self):
		data = self.change(status="Active")
		self.assertEqual(data["changed"], [])
		self.assertIn("status", data["unchanged"])

	def test_a_payroll_field_is_refused_here_too(self):
		self.assertIn("payroll, tax or banking", self.change_error(salary_mode="Bank"))

	def test_an_update_with_nothing_to_change_is_refused(self):
		self.assertIn("nothing to change", self.tool_error("update_employee", {"name": self.employee}))

	def test_the_employee_can_be_named_by_its_number(self):
		frappe.db.set_value("Employee", self.employee, "employee_number", "E-900")
		data = self.tool_data("update_employee", {"name": "E-900", "designation": "Picker"})
		self.assertEqual(data["employee"], self.employee)

	def test_the_employee_can_be_named_by_its_login(self):
		user = self.enrolled()["user"]
		self.tool_data("link_employee_to_user", {"employee": self.employee, "user_id": user})
		data = self.tool_data("update_employee", {"name": user, "designation": "Picker"})
		self.assertEqual(data["employee"], self.employee)

	def test_an_employee_that_does_not_exist_is_refused(self):
		self.assertIn("EMP-NOPE", self.tool_error("update_employee", {"name": "EMP-NOPE", "status": "Left"}))

	def test_setting_the_login_after_the_fact_is_the_whole_point(self):
		"""This is the call that fixes an Employee created before its login was."""
		user = self.enrolled()["user"]
		self.change(user_id=user)
		self.assertEqual(frappe.db.get_value("Employee", self.employee, "user_id"), user)

	def test_re_pointing_an_existing_login_needs_saying_so(self):
		first = self.enrolled()["user"]
		self.change(user_id=first)
		second = self.enrolled(email="beto@example.test", name="Beto Cruz")["user"]
		message = self.change_error(user_id=second)
		self.assertIn("already linked", message)
		self.assertIn("replace_user", message)
		self.assertEqual(frappe.db.get_value("Employee", self.employee, "user_id"), first)

	def test_re_pointing_works_when_it_is_said(self):
		first = self.enrolled()["user"]
		self.change(user_id=first)
		second = self.enrolled(email="beto@example.test", name="Beto Cruz")["user"]
		self.change(user_id=second, replace_user=True)
		self.assertEqual(frappe.db.get_value("Employee", self.employee, "user_id"), second)

	def test_a_company_the_caller_cannot_see_is_refused(self):
		self.scope_actor_to(OTHER)
		self.assertIn("no access to company", self.change_error(status="Left"))


# ── 6 ───────────────────────────────────────────────────────────────────────
class Linking(EmployeeTestCase):
	def setUp(self):
		super().setUp()
		self.employee = self.create()["employee"]

	def link(self, user, **extra):
		return self.tool_data(
			"link_employee_to_user", {"employee_name": self.employee, "user_id": user, **extra}
		)

	def link_error(self, user, **extra):
		return self.tool_error(
			"link_employee_to_user", {"employee_name": self.employee, "user_id": user, **extra}
		)

	def test_both_sides_end_up_linked(self):
		user = self.enrolled()["user"]
		data = self.link(user)
		self.assertEqual(frappe.db.get_value("Employee", self.employee, "user_id"), user)
		self.assertEqual(data["employee"], self.employee)
		self.assertEqual(data["user_id"], user)
		self.assertEqual(data["action"], "linked")

	def test_it_says_the_phone_will_now_work(self):
		"""'The link was written' is not the fact anybody wanted."""
		self.link(self.enrolled()["user"])
		data = self.link(WORKER)
		self.assertTrue(data["linkage"]["farm_ops_ready"])
		self.assertIn("list_my_tasks", data["note"])

	def test_it_reports_the_entity_access_the_login_carries(self):
		self.link(self.enrolled()["user"])
		self.assertEqual(self.link(WORKER)["linkage"]["entity_access"], [MAIN])

	def test_a_user_with_no_grant_and_no_role_is_refused_with_the_reason(self):
		"""A link that changes nothing today would silently grant a task board on
		the day somebody grants that account a role for an unrelated reason."""
		message = self.link_error(self.plain_user())
		self.assertIn("no Farm Ops role and no Mobile Access Grant", message)
		self.assertIn("create_mobile_user", message)
		self.assertIsNone(frappe.db.get_value("Employee", self.employee, "user_id"))

	def test_linking_ahead_of_the_grant_is_allowed_when_said_deliberately(self):
		user = self.plain_user()
		data = self.link(user, allow_unenrolled_user=True)
		self.assertEqual(frappe.db.get_value("Employee", self.employee, "user_id"), user)
		self.assertFalse(data["linkage"]["farm_ops_ready"])
		self.assertIn("WILL STILL REFUSE", data["note"])

	def test_a_user_that_does_not_exist_is_refused(self):
		message = self.link_error("ghost@example.test")
		self.assertIn("ghost@example.test", message)
		self.assertIn("create_mobile_user", message)

	def test_an_employee_that_does_not_exist_is_refused(self):
		self.assertIn(
			"EMP-NOPE",
			self.tool_error(
				"link_employee_to_user", {"employee_name": "EMP-NOPE", "user_id": self.enrolled()["user"]}
			),
		)

	def test_a_login_already_belonging_to_somebody_else_is_refused(self):
		"""Two Employee records naming one login gives list_my_tasks two answers
		where it needs one."""
		user = self.enrolled()["user"]
		self.link(user)
		other = self.create(employee_name="Beto Cruz")["employee"]
		message = self.tool_error("link_employee_to_user", {"employee_name": other, "user_id": user})
		self.assertIn(self.employee, message)
		self.assertIn("one-to-one", message)

	def test_the_same_link_twice_is_a_no_op_that_says_so(self):
		user = self.enrolled()["user"]
		self.link(user)
		data = self.link(user)
		self.assertEqual(data["action"], "already linked")
		self.assertEqual(frappe.db.get_value("Employee", self.employee, "user_id"), user)

	def test_a_different_login_on_this_employee_needs_replace_user(self):
		self.link(self.enrolled()["user"])
		second = self.enrolled(email="beto@example.test", name="Beto Cruz")["user"]
		message = self.link_error(second)
		self.assertIn("whole task history", message)
		self.assertIn("replace_user", message)

	def test_replacing_reports_what_it_displaced(self):
		first = self.enrolled()["user"]
		self.link(first)
		second = self.enrolled(email="beto@example.test", name="Beto Cruz")["user"]
		data = self.link(second, replace_user=True)
		self.assertEqual(data["action"], "relinked")
		self.assertEqual(data["previous_user_id"], first)

	def test_an_inactive_employee_is_linked_and_flagged(self):
		"""The mobile methods answer for Active employees, and a link on a Left
		employee is a link that will not produce a task board."""
		self.tool_data("update_employee", {"name": self.employee, "status": "Left"})
		data = self.link(self.enrolled()["user"])
		self.assertIn("THIS EMPLOYEE IS Left", data["note"])

	def test_a_company_the_caller_cannot_see_is_refused(self):
		user = self.enrolled()["user"]
		self.scope_actor_to(OTHER)
		self.assertIn("no access to company", self.link_error(user))


# ── 7 ───────────────────────────────────────────────────────────────────────
class OnboardingEndToEnd(EmployeeTestCase):
	def hire(self, **overrides):
		payload = {"full_name": "Ana Ramos", "company": MAIN, "email": WORKER}
		payload.update(overrides)
		return self.tool_data("onboard_employee", payload)

	def test_it_produces_the_employee_the_grant_and_the_link(self):
		data = self.hire()
		self.assertTrue(data["employee"])
		self.assertEqual(data["mobile"]["user"], WORKER)
		self.assertTrue(data["mobile"]["grant"])
		self.assertEqual(data["link"]["user_id"], WORKER)
		self.assertEqual(frappe.db.get_value("Employee", data["employee"], "user_id"), WORKER)

	def test_the_employee_is_created_before_the_login_and_linked_after(self):
		"""THE ORDERING BUG. Employee.user_id is a Link to User, so creating the
		Employee with the login already on it refuses on a real bench — the User
		does not exist yet."""
		data = self.hire()
		order = [row["step"] for row in data["steps"]]
		self.assertLess(order.index("employee"), order.index("mobile access"))
		self.assertLess(order.index("mobile access"), order.index("link"))

	def test_the_qr_comes_back_in_the_same_response_when_asked_for(self):
		data = self.hire(issue_qr=True)
		self.assertTrue(data["qr"]["png_base64"])
		self.assertEqual(data["qr"]["mime_type"], "image/png")
		self.assertTrue(data["qr"]["expires_at"])
		self.assertTrue(data["qr"]["endpoint"])

	def test_the_qr_is_opt_in(self):
		"""Minting one rotates the account's secret. A default-true would mean
		re-running an onboarding to add a W-4 knocked a live phone offline."""
		self.assertIsNone(self.hire()["qr"])

	def test_the_plaintext_credential_is_still_not_in_the_result(self):
		"""THE ASSERTION IS AGAINST THE LIVE SECRET, not against the string
		'api_secret' — the notes in this payload name that key in order to say it
		is deliberately absent, and a substring test cannot tell a value from a
		prohibition.

		The PNG encodes the secret, unavoidably: that is what enrolment by QR IS,
		and it is why png_base64 is excluded here rather than pretended about. What
		must not appear is the decoded payload, which carries the same secret as
		readable text into somewhere far more pasteable."""
		from erpnext_mcp.tools import mobile

		data = self.hire(issue_qr=True)
		secret = mobile.read_api_secret(WORKER)
		self.assertTrue(secret, "the fixture did not actually issue a credential")
		scrubbed = {**data, "qr": {**data["qr"], "png_base64": "<image>"}}
		self.assertNotIn(secret, json.dumps(scrubbed, default=str))
		self.assertNotIn("payload", data["qr"])

	def test_a_second_run_with_the_same_arguments_duplicates_nothing(self):
		first = self.hire()
		employees = len(STORE.rows("Employee"))
		grants = len(STORE.rows("Mobile Access Grant"))
		second = self.hire()
		self.assertEqual(second["employee"], first["employee"])
		self.assertEqual(len(STORE.rows("Employee")), employees)
		self.assertEqual(len(STORE.rows("Mobile Access Grant")), grants)

	def test_a_second_run_reports_the_link_as_already_made(self):
		self.hire()
		self.assertEqual(self.hire()["link"]["action"], "already linked")

	def test_a_second_run_with_no_email_still_finds_the_person_by_name(self):
		"""The lookup that covers a re-run where there is no login to match on."""
		first = self.tool_data("onboard_employee", {"full_name": "Beto Cruz", "company": MAIN})
		second = self.tool_data("onboard_employee", {"full_name": "Beto Cruz", "company": MAIN})
		self.assertEqual(second["employee"], first["employee"])
		self.assertEqual(
			next(row for row in second["steps"] if row["step"] == "employee")["action"], "reused"
		)

	def test_the_next_step_names_one_thing(self):
		self.assertIn("generate_mobile_login_qr", self.hire()["next_step"])
		self.assertIn("curl", self.hire(issue_qr=True)["next_step"])

	def test_a_link_that_cannot_be_made_does_not_undo_the_rest(self):
		"""An Employee that exists and a login that exists are both worth keeping
		when only the join between them failed.

		The failure staged here is the real one: this person's record is already
		pointed at a different login, and re-pointing it would move their whole
		task history, which the orchestrator is not allowed to decide."""
		existing = self.create()["employee"]
		self.tool_data(
			"link_employee_to_user",
			{"employee_name": existing, "user_id": self.enrolled()["user"]},
		)
		data = self.hire(email="beto@example.test")
		self.assertEqual(data["employee"], existing)
		self.assertEqual(data["mobile"]["user"], "beto@example.test")
		self.assertIsNone(data["link"])
		self.assertTrue(any(row["step"] == "link" for row in data["skipped"]))

	def test_it_still_refuses_a_site_with_no_employee_register(self):
		"""The refusal arrives from the tool's availability predicate before the
		handler runs at all, which is why it names the DocType."""
		from .harness import INSTALLED_DOCTYPES

		INSTALLED_DOCTYPES.discard("Employee")
		try:
			message = self.tool_error("create_employee", {"employee_name": "Ana Ramos", "company": MAIN})
		finally:
			INSTALLED_DOCTYPES.add("Employee")
		self.assertIn("Employee", message)


# ── 8 ───────────────────────────────────────────────────────────────────────
class TheEmploymentTypesTheInstallerSeeds(EmployeeTestCase):
	"""A Link is only as good as its master, and this app never seeded this one.

	`employment_type` is checked against the site's own `Employment Type` records
	and a value naming none is refused — right, and staying. But a stock Frappe HR
	ships Full-time, Part-time, Probation, Contract, Commission, Piecework, Intern
	and Apprentice, an office's categories with no Hourly on them, so `Hourly` was
	refused on every site and the majority of a farm's workforce could not be
	onboarded through the tool at all. `install._employment_types` seeds the two a
	farm needs; the refusal is unchanged for everything else.

	`install_hrms` in this suite models the stock masters — Full-time, Part-time,
	Seasonal Worker — which is exactly the site the bug was reported from, so the
	first test here is the negative control and it must keep failing to hire.
	"""

	def hire(self, **overrides):
		payload = {"full_name": "Ana Ramos", "company": MAIN, "email": WORKER}
		payload.update(overrides)
		return payload

	def test_a_stock_hr_has_no_hourly_and_that_is_the_bug(self):
		self.assertFalse(frappe.db.exists("Employment Type", "Hourly"))
		message = self.create_error(employment_type="Hourly")
		self.assertIn("Hourly", message)
		self.assertIn("Employment Type", message)

	def test_after_migrate_seeds_them(self):
		from erpnext_mcp import install

		install.after_migrate()
		for name in install.FARM_EMPLOYMENT_TYPES:
			with self.subTest(employment_type=name):
				self.assertTrue(frappe.db.exists("Employment Type", name))

	def test_the_seeded_type_is_named_after_itself(self):
		"""`frappe.db.exists("Employment Type", "Hourly")` is the check on both
		sides — the seeder's idempotence and `create_employee`'s Link — so a row
		that came back serial-named would satisfy neither."""
		from erpnext_mcp import install

		install.after_migrate()
		row = STORE.get_raw("Employment Type", "Hourly")
		self.assertEqual(row["employee_type_name"], "Hourly")

	def test_an_hourly_hire_goes_through_once_they_are_seeded(self):
		from erpnext_mcp import install

		install.after_migrate()
		data = self.create(employment_type="Hourly")
		self.assertEqual(frappe.db.get_value("Employee", data["employee"], "employment_type"), "Hourly")

	def test_onboard_employee_takes_hourly_too(self):
		"""The tool the bug was reported against. It writes the Employee through
		`create_employee`, so it is the same allowlist and the same Link check —
		asserted here anyway, because that is the call that was blocked."""
		from erpnext_mcp import install

		install.after_migrate()
		data = self.tool_data("onboard_employee", self.hire(employment_type="Hourly"))
		self.assertEqual(frappe.db.get_value("Employee", data["employee"], "employment_type"), "Hourly")

	def test_a_second_migrate_creates_nothing(self):
		from erpnext_mcp import install

		install.after_migrate()
		install.after_migrate()
		rows = [row for row in STORE.rows("Employment Type") if row["name"] == "Hourly"]
		self.assertEqual(len(rows), 1)

	def test_a_type_the_site_already_has_is_left_exactly_as_it_was(self):
		"""Seasonal Worker is on this site before the seeder runs. An operator who
		edited one keeps their row — the seeder adds options and rewrites none.

		v0.209.1 adds one column, `badge_category`, and fills it where it is BLANK
		(a payroll type is "Employee" on an ID card). Nothing that was there
		changes, and a category an operator chose survives the next migrate."""
		from erpnext_mcp import install

		before = dict(STORE.get_raw("Employment Type", "Seasonal Worker"))
		install.after_migrate()
		after = dict(STORE.get_raw("Employment Type", "Seasonal Worker"))
		self.assertEqual(after.pop("badge_category", None), "Employee")
		before.pop("badge_category", None)
		for volatile in ("modified", "modified_by"):
			after.pop(volatile, None)
			before.pop(volatile, None)
		self.assertEqual(after, before)
		frappe.db.set_value("Employment Type", "Seasonal Worker", "badge_category", "Contractor")
		install.after_migrate()
		self.assertEqual(
			frappe.db.get_value("Employment Type", "Seasonal Worker", "badge_category"), "Contractor"
		)

	def test_an_employment_type_nobody_seeded_is_still_refused(self):
		"""The seed adds two options. It does not turn the Link check off."""
		from erpnext_mcp import install

		install.after_migrate()
		self.assertIn("Indentured", self.create_error(employment_type="Indentured"))


# ── the module's own claims ─────────────────────────────────────────────────
class TheAllowlistIsClosed(EmployeeTestCase):
	def test_the_nineteen_are_the_nineteen(self):
		"""Asserted by name. `WRITABLE` is what every refusal message lists, and a
		field added to it without a decision is a field this app writes without
		one.

		Fourteen until v0.46.1. Three of the last four are the Custom Fields
		`compliance_fields.py` installs with `reqd=True` — this app is the reason
		the site has them and the reason they are mandatory, so a create that could
		not write them was this app refusing its own schema on every path,
		`onboard_employee` and the iOS wizard's first step included.

		`middle_name` is the eighteenth, added in v0.51.0. It is an identity fact
		like the two names either side of it, the handset has read one off every
		AAMVA licence barcode since the ID scanner shipped, and this list is
		where it was being dropped — silently taking the I-9's Legal Middle Name
		with it, because `submit_i9_section_1` fills that box from
		`Employee.middle_name` when the caller sends none.

		The last three are v0.62.0's, and they close the same shape of gap one
		release later: `set_employee_contact_fields` on the mobile surface names
		five contact fields and this list carried two of them, so a hire could
		record a phone number and an email and could not record who to ring if
		that picker went down on a block. `person_to_be_contacted` and
		`emergency_phone_number` are Frappe HR's own spellings of "Emergency
		Contact Name" and "Emergency Phone"; the mobile wrapper takes the labels
		and maps them, because the docname of a column is not a thing a handset
		should have to know. None of the three is payroll, tax or banking, which
		is the boundary this list actually defends.

		`branch` is the nineteenth, added in v0.54.0. It is Frappe HR's own
		operating-unit dimension and the last field the hiring wizard's Assignment
		step asked for that this list did not carry — so a crew hired for a
		particular camp recorded the COMPANY that employs them and nothing about
		where they report. It sits beside `department`, `designation` and
		`employment_type` because it is the same kind of fact and is checked the
		same way: a Link, against this site's own Branch records.

		`reports_to` IS THE TWENTIETH, added in v0.68.1, AND IT REVERSES A
		DECISION THIS TEST USED TO RECORD. It was the example of a real column
		this tool deliberately would not write, on the argument that a reporting
		line is an org-chart decision rather than an identity fact. What made
		that wrong is that two of this app's own surfaces already READ it and
		neither could get it filled in: `escalate_farm_task` refuses with "no
		reports_to on their Employee record, so this app does not know who their
		supervisor is", and `get_shadow_log_entry` walks the chain to build a
		review ladder. A column this app depends on and cannot write is a column
		that stays empty, and the only editor left was the Desk. It is still not
		payroll, tax or banking — it sets no rate, no structure and no approval
		limit — which is the boundary this list actually defends."""
		self.assertEqual(
			employee_tool.WRITABLE,
			(
				"employee_name",
				"first_name",
				"middle_name",
				"last_name",
				"company",
				"date_of_joining",
				"date_of_birth",
				"gender",
				"department",
				"designation",
				"employment_type",
				"badge_title",
				"badge_category",
				"branch",
				"reports_to",
				"status",
				"user_id",
				"personal_email",
				"cell_number",
				"current_address",
				"person_to_be_contacted",
				"emergency_phone_number",
				"i9_status",
				"w4_status",
				"jurisdiction",
			),
		)

	def test_every_field_on_the_allowlist_is_a_field_create_actually_writes(self):
		"""The bug this exists for shipped once and was invisible.

		`WRITABLE` is what `_reject_unknown` ACCEPTS. What `create_employee`
		actually writes is a separate tuple inside the function — the derived
		fields, then `optional`. v0.54.0 added `branch` to the first and not the
		second, and the result was the worst shape a failure can take: the tool
		took the argument without a word of complaint, reported success, and
		dropped the value. No refusal, no warning, nothing in
		`fields_not_on_this_site` — just a hire whose camp was blank.

		Asserted through the tool rather than by reading the tuple, because the
		tuple is a local and because what matters is the value landing on the
		record. Every field gets a value this site's schema accepts; the two
		derived off the full name and the ones with their own tests are covered
		here anyway, since the point is that NOTHING on the list is silently
		dropped."""
		STORE.seed("Branch", [{"name": "Mill Creek Camp", "branch": "Mill Creek Camp"}])
		values = {
			"employee_name": "Ana Ramos",
			"first_name": "Ana",
			"middle_name": "Luz",
			"last_name": "Ramos",
			"company": MAIN,
			"date_of_joining": "2026-07-01",
			"date_of_birth": "1994-03-12",
			"gender": "Female",
			"department": "Operations",
			"designation": "Picker",
			"employment_type": "Seasonal Worker",
			"badge_title": "Crew Lead",
			"badge_category": "Management",
			"branch": "Mill Creek Camp",
			# The seeded supervisor's DOCNAME rather than their name, because
			# this test compares the value it sent with the value that landed
			# and `_clean` deliberately resolves the other three spellings —
			# employee number, name, login — down to this one. `ReportsTo` in
			# `test_org.py` is where the resolution itself is asserted.
			"reports_to": "HR-EMP-00001",
			"status": "Active",
			"personal_email": "ana@example.test",
			"cell_number": "5415550143",
			"current_address": "144 Orchard Lane, The Dalles OR",
			"person_to_be_contacted": "Marisol Ramos",
			"emergency_phone_number": "5415550188",
			"i9_status": "Verified",
			"w4_status": "On-File",
			"jurisdiction": "WA",
		}
		# `user_id` is the one field left out, and it is not an oversight: it has
		# to name a User that exists and is enrolled, which is a fixture rather
		# than a value, and `TheLinkage` covers it at length.
		self.assertEqual(
			set(employee_tool.WRITABLE) - set(values),
			{"user_id"},
			"a field joined WRITABLE and this test does not exercise it",
		)

		created = self.create(**{key: value for key, value in values.items() if key != "employee_name"})
		row = frappe.db.get_value("Employee", created["employee"], list(values), as_dict=True)
		for field, expected in values.items():
			# A field this site's Employee does not carry is a different fact and
			# already has its own contract: `_supported` splits it out and reports
			# it in `fields_not_on_this_site` rather than dropping it quietly.
			# The three `compliance_fields.py` installs are Custom Fields, and
			# this fixture is a site that has not installed them.
			if not compat.has_field("Employee", field):
				self.assertIn(field, created["fields_not_on_this_site"])
				continue
			with self.subTest(field=field):
				self.assertEqual(
					str(row.get(field) or ""),
					str(expected),
					f"create_employee accepted {field} and did not write it",
				)

	def test_no_sensitive_field_is_also_writable(self):
		self.assertFalse(set(employee_tool.WRITABLE) & employee_tool.SENSITIVE_FIELDS)

	def test_the_link_gate_is_the_mobile_surfaces_own_role_set(self):
		"""Read from api/guard rather than re-listed, so the set this file refuses
		against and the set the eleven methods gate on cannot drift apart."""
		from erpnext_mcp.api import guard

		self.assertIs(employee_tool._farm_ops_roles(), guard.FARM_OPS_ROLES)

	def test_all_three_tools_are_mutating_and_default_off(self):
		from erpnext_mcp import registry

		for name in ("create_employee", "update_employee", "link_employee_to_user"):
			with self.subTest(tool=name):
				self.assertIn(name, registry.MUTATING_TOOLS)
				self.assertNotIn(name, registry.DEFAULT_ON_MUTATING_TOOLS)

	def test_each_one_says_which_switch_turns_it_on(self):
		self.configure(enabled=1)
		for name in ("create_employee", "update_employee", "link_employee_to_user"):
			with self.subTest(tool=name):
				self.assertIn(f"allow_{name}", self.tool_error(name, {}))

	def test_every_mutation_leaves_an_audit_row(self):
		data = self.create()
		rows = [row for row in STORE.rows("MCP Action Log") if row["tool_name"] == "create_employee"]
		self.assertEqual(len(rows), 1)
		self.assertIn(data["employee"], rows[0]["result_summary"])
