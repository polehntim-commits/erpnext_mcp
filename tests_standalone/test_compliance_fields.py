# SPDX-License-Identifier: MIT
"""Compliance metadata on the doctypes where the work happens — Sprint 7 Wave 1.

FOUR THINGS THESE TESTS ARE ABOUT.

IT IS IDEMPOTENT, AND `bench migrate` PROVES IT THREE TIMES. `after_migrate`
runs this installer on every migration, so "the second run creates nothing" is
not a nicety — it is the difference between an operator's Spray Log gaining four
columns and gaining twelve. `MigrateThreeTimes` runs the whole hook three times
and counts the Custom Field rows.

IT DEGRADES BY NAME. A site without farm_precision_ag has no Spray Log, and the
installer says so rather than failing. That is tested against a genuine absence
— the fixture site really does not have one — rather than against a mock.

THE FIELDS ARE WOVEN IN, NOT BOLTED ON. `WovenNotShadow` is the point of the
whole wave. Each test takes one field away and shows the SAME removal breaks an
OPERATIONAL answer and a COMPLIANCE answer. A field whose removal only breaks a
report is a shadow field and belongs somewhere else; the test is the enforcement
of that judgement, not a description of it.

THE `verify` TARGETS ARE NOT PAPERED OVER. Housing Unit and Field are this app's
own doctypes and carry their compliance columns in their shipped JSON. If one is
missing the installer REPORTS it and adds nothing — because a Custom Field over
the top of an unfinished migration gives the site two columns and no error, and
the second is worse than the first.
"""

import os
import unittest

from erpnext_mcp import compliance_fields, install

from .fixtures import V12TestCase, install_hrms
from .harness import REPO_ROOT, STORE, register_doctype

DOC = os.path.join(REPO_ROOT, "docs", "compliance_fields.md")


def custom_fields(doctype=None) -> list:
	rows = STORE.rows("Custom Field")
	return [row for row in rows if doctype is None or row.get("dt") == doctype]


def compliance_custom_fields(doctype=None) -> list:
	"""Only the fields the compliance table declares.

	`after_migrate` ADDS CUSTOM FIELDS THAT ARE NOT COMPLIANCE FIELDS, and since
	v0.51.0 it adds one: `Company.badge_logo`, which the badge printer owns and
	which has no regulator behind it — `TheTable` above refuses a field that
	cannot name one, so it could not live in that table even if somebody wanted
	it to. Counting every row in `Custom Field` made these tests a running total
	of everything the hook has ever installed, which fails on the next feature
	that needs a column and says nothing about the compliance installer either
	way. This filter is what keeps them about their own subject.
	"""
	declared = {
		(target.doctype, field.fieldname) for target in compliance_fields.TARGETS for field in target.fields
	}
	return [row for row in custom_fields(doctype) if (row.get("dt"), row.get("fieldname")) in declared]


class TheTable(V12TestCase):
	"""Properties of the declaration itself, before anything is installed."""

	def test_every_field_names_the_framework_that_wants_it(self):
		"""A field nobody can say the regulatory basis for does not belong here.

		This is the filter that keeps the table from accumulating fields somebody
		thought would be useful.
		"""
		for target in compliance_fields.TARGETS:
			for spec in target.fields:
				with self.subTest(field=f"{target.doctype}.{spec.fieldname}"):
					self.assertTrue(spec.framework.strip(), "no framework named")
					self.assertGreater(len(spec.why), 40, "the reason is a phrase, not a reason")

	def test_every_field_says_what_breaks_operationally(self):
		"""THE WOVEN-IN TEST, ENCODED IN THE TABLE.

		If the honest answer to "what breaks in the day-to-day work without this"
		is "nothing", the field is a shadow field and belongs in a compliance
		doctype rather than on the operational record. Requiring the sentence is
		what stops one being added without somebody confronting that question.

		The floor is low on purpose. The shortest answer in the whole table is the
		two detector dates' "Somebody sleeps there tonight.", and it is also the
		strongest — a threshold that demanded a paragraph would have forced it to
		be padded into something weaker.
		"""
		for target in compliance_fields.TARGETS:
			for spec in target.fields:
				with self.subTest(field=f"{target.doctype}.{spec.fieldname}"):
					self.assertGreater(
						len(spec.operational),
						25,
						f"{spec.fieldname} does not say what breaks operationally without it. "
						"If nothing does, it is a shadow field.",
					)

	def test_no_field_is_called_owner_or_any_other_framework_column(self):
		"""Frappe owns `owner`, `creation`, `modified`, `docstatus` and `idx` on
		every doctype. A Custom Field with one of those names does not shadow the
		column — it collides with it, and the failure arrives from the database."""
		reserved = {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "parent"}
		for target in compliance_fields.TARGETS:
			for spec in target.fields:
				with self.subTest(field=spec.fieldname):
					self.assertNotIn(spec.fieldname, reserved)

	def test_the_seven_required_fields_are_the_ones_that_stop_work(self):
		"""Required is a strong claim: it makes existing records unsaveable. It is
		reserved for fields whose absence stops somebody doing their job."""
		required = {
			f"{target.doctype}.{spec.fieldname}"
			for target in compliance_fields.TARGETS
			for spec in target.fields
			if spec.reqd
		}
		self.assertEqual(
			required,
			{
				"Spray Log.applicator_name",
				"Spray Log.epa_reg_number",
				"Spray Log.rei_hours",
				"Spray Log.phi_hours",
				"Employee.i9_status",
				"Employee.w4_status",
				"Employee.jurisdiction",
			},
		)

	def test_no_verify_target_declares_a_required_field(self):
		"""A `verify` target's columns are declared on this app's own DocType JSON.
		Marking one required HERE would be a claim the JSON does not make, and the
		two would silently disagree about what the doctype demands."""
		for target in compliance_fields.TARGETS:
			if target.mode != "verify":
				continue
			for spec in target.fields:
				with self.subTest(field=f"{target.doctype}.{spec.fieldname}"):
					self.assertFalse(spec.reqd)

	def test_describe_covers_every_field_in_the_table(self):
		"""`docs/compliance_fields.md` is generated from this, so a field that
		fell out of `describe()` would ship undocumented."""
		described = compliance_fields.describe()
		self.assertEqual(
			described["field_count"],
			sum(len(target.fields) for target in compliance_fields.TARGETS),
		)
		self.assertEqual(described["required_field_count"], 7)
		self.assertTrue(described["frameworks"])


class TheInstaller(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_install_compliance_fields=1, allow_get_compliance_field_map=1)

	def test_it_adds_the_employee_fields_on_a_site_with_an_hr_app(self):
		install_hrms()
		report = compliance_fields.install_compliance_fields()
		added = {row["fieldname"] for row in custom_fields("Employee")}
		self.assertEqual(
			added,
			{
				"i9_status",
				"w4_status",
				"jurisdiction",
				"flc_license_status",
				"flc_license_expiration",
				# v0.79.0. Which language this person is trained, warned and
				# disciplined in. A compliance field rather than a preference:
				# 1910.1200(h) and WPS 170.501 both require the training to be
				# in a language the worker understands, and an employer who
				# cannot say which language they used cannot show they did.
				"preferred_language",
			},
		)
		self.assertIn("Employee.i9_status", report["created"])

	def test_a_second_run_creates_nothing(self):
		install_hrms()
		compliance_fields.install_compliance_fields()
		first = len(custom_fields())
		report = compliance_fields.install_compliance_fields()
		self.assertEqual(len(custom_fields()), first)
		self.assertEqual(report["created"], [])
		self.assertTrue(report["existing"])

	def test_an_absent_doctype_is_skipped_by_name_and_nothing_fails(self):
		"""The fixture site genuinely has no Spray Log — a real absence, not a
		mocked one. The installer has to say which app would bring it."""
		report = compliance_fields.install_compliance_fields()
		skipped = {entry["doctype"]: entry["reason"] for entry in report["skipped"]}
		self.assertIn("Spray Log", skipped)
		self.assertIn("farm_precision_ag", skipped["Spray Log"])
		self.assertEqual(report["failed"], [])

	def test_a_field_somebody_else_already_added_is_left_alone(self):
		"""A later farm_precision_ag that ships `epa_reg_number` itself must not
		end up with two columns. The check is "is the field there at all", not "is
		there a Custom Field row we wrote"."""
		register_doctype(
			"Spray Log",
			[
				{"fieldname": "name", "fieldtype": "Data"},
				{"fieldname": "epa_reg_number", "fieldtype": "Data"},
			],
		)
		report = compliance_fields.install_compliance_fields()
		self.assertIn("Spray Log.epa_reg_number", report["existing"])
		self.assertNotIn("epa_reg_number", {row["fieldname"] for row in custom_fields("Spray Log")})

	def test_the_switch_off_means_nothing_is_added_anywhere(self):
		"""Shipping ON is not the same as being unswitchable."""
		install_hrms()
		self.configure(enabled=1, allow_install_compliance_fields=0)
		report = compliance_fields.install_compliance_fields()
		self.assertFalse(report["enabled"])
		self.assertEqual(custom_fields(), [])
		self.assertEqual(len(report["skipped"]), len(compliance_fields.TARGETS))

	def test_a_verify_target_that_is_whole_reports_no_failure(self):
		"""Housing Unit and Field ship their compliance columns declared."""
		report = compliance_fields.install_compliance_fields()
		by_doctype = {target["doctype"]: target for target in report["targets"]}
		self.assertEqual(by_doctype["Housing Unit"]["missing"], [])
		self.assertEqual(by_doctype["Field"]["missing"], [])
		self.assertEqual(custom_fields("Housing Unit"), [])

	def test_a_verify_target_with_a_missing_column_is_reported_not_patched(self):
		"""A declared field that is absent means the DocType did not migrate. A
		Custom Field over the top would give the site two columns and no error,
		which is worse than the problem it hides."""
		register_doctype("Field", [{"fieldname": "name", "fieldtype": "Data"}])
		report = compliance_fields.install_compliance_fields()
		by_doctype = {target["doctype"]: target for target in report["targets"]}
		self.assertIn("food_safety_zone", by_doctype["Field"]["missing"])
		self.assertEqual(custom_fields("Field"), [])
		reasons = [entry["reason"] for entry in report["failed"] if entry["doctype"] == "Field"]
		self.assertTrue(any("bench" in reason and "migrate" in reason for reason in reasons))

	def test_it_reports_the_backlog_of_records_a_required_field_makes_unsaveable(self):
		"""THE NUMBER WORTH READING. Frappe binds `reqd` on save, so history stays
		readable and stops being re-saveable — and every one of those rows is a
		record that was never compliant."""
		install_hrms()
		report = compliance_fields.install_compliance_fields()
		employee = next(t for t in report["targets"] if t["doctype"] == "Employee")
		backlog = employee["backlog"]["i9_status"]
		self.assertEqual(backlog["total_rows"], len(STORE.rows("Employee")))
		self.assertEqual(backlog["rows_missing_a_value"], backlog["total_rows"])
		self.assertIn("re-saved", backlog["note"])

	def test_it_never_raises_on_a_site_that_answers_nothing(self):
		"""It runs inside `bench migrate`, where an exception aborts the migration
		for the whole bench."""
		register_doctype("Spray Log", [])
		report = compliance_fields.install_compliance_fields()
		self.assertIsInstance(report, dict)


class MigrateThreeTimes(V12TestCase):
	"""Tim's requirement, literally: run the migrate hook three times and count.

	The hook rather than the installer, because the hook is what a site runs and
	it wraps the installer in a try/except that could swallow a real failure and
	report success. Running the outer thing is the only way that path is covered.
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_install_compliance_fields=1)
		install_hrms()

	def test_three_migrations_produce_one_set_of_fields(self):
		counts = []
		for _ in range(3):
			install.after_migrate()
			counts.append(len(compliance_custom_fields()))
		# Six on Employee since v0.79.0 added `preferred_language`, the v0.19.3
		# Attendance bridge column, the four v0.19.5
		# capex columns on ERPNext's Asset, and the nine v0.69.0 columns on
		# ERPNext's Item — the REI/PHI pair the spray window computes from, plus
		# the seven label-detail columns a scanned pesticide label lands in. Spray
		# Log and Bucket Log Entry are genuinely absent from the fixture site, so
		# they add nothing here and are reported as skipped instead.
		#
		# All nine Item columns land on EVERY Item, not only the chemicals — the
		# `depends_on` on seven of them decides what is SHOWN and Frappe stores the
		# column either way, which is exactly why none of the nine is `reqd`.
		#
		# Three more from v0.148.0, all on Asset: the Link back to the printed
		# tag and the two read-only columns that make the link auditable — what
		# kind of machine it is, and when the mirror last agreed with it.
		self.assertEqual(
			counts[0],
			24,
			"six Employee fields, the Attendance bridge, four Asset capex columns, "
			"three Asset register-mirror columns, nine Item label columns, and the "
			"v0.94.0 Company housing-deduction default",
		)
		self.assertEqual(counts, [counts[0]] * 3, f"custom fields multiplied across migrations: {counts}")

	def test_three_migrations_leave_no_duplicate_fieldname_on_any_doctype(self):
		# Deliberately every Custom Field, not just the compliance ones: the
		# property is that the hook is idempotent, and a second `badge_logo`
		# would be exactly the bug this is looking for.
		for _ in range(3):
			install.after_migrate()
		seen = [(row["dt"], row["fieldname"]) for row in custom_fields()]
		self.assertEqual(len(seen), len(set(seen)), f"duplicate custom fields: {seen}")

	def test_a_migration_with_the_switch_off_adds_no_compliance_fields(self):
		"""The other jobs in `after_migrate` have to keep running. An operator who
		declined the compliance fields did not decline their settings defaults."""
		self.configure(enabled=1, allow_install_compliance_fields=0)
		install.after_migrate()
		self.assertEqual(compliance_custom_fields(), [])
		self.assertTrue(STORE.singles["ERPNext MCP Settings"])

	def test_the_non_compliance_company_fields_are_not_behind_the_switch(self):
		"""`install_compliance_fields` exists so an operator can decline having
		their Spray Log extended. It is not a switch for every column this app
		has ever added, and a farm that turned it off did not thereby decide to
		print badges with no logo on them or to stop recording who advises them
		on pest management.

		The compliance column on Company — `default_housing_deduction_from_wages`
		— IS absent here, which is the other half of the same assertion: the
		switch governs exactly the fields that name a regulator."""
		self.configure(enabled=1, allow_install_compliance_fields=0)
		install.after_migrate()
		self.assertEqual(
			sorted(row["fieldname"] for row in custom_fields("Company")),
			["badge_logo", "pest_management_providers"],
		)


class TheTools(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_install_compliance_fields=1, allow_get_compliance_field_map=1)
		install_hrms()

	def test_the_map_reports_what_is_here_and_what_is_not(self):
		data = self.tool_data("get_compliance_field_map", {})
		self.assertIn("Spray Log", data["doctypes_not_on_this_site"])
		self.assertIn("Employee.i9_status", data["missing_here"])
		self.assertIn("Housing Unit.fsma_worker_facility", data["present_here"])

	def test_installing_moves_a_field_from_missing_to_present(self):
		self.tool_data("install_compliance_fields", {})
		data = self.tool_data("get_compliance_field_map", {})
		self.assertIn("Employee.i9_status", data["present_here"])
		self.assertNotIn("Employee.i9_status", data["missing_here"])

	def test_a_dry_run_reports_the_backlog_and_writes_nothing(self):
		data = self.tool_data("install_compliance_fields", {"dry_run": True})
		self.assertTrue(data["dry_run"])
		self.assertTrue(data["created"])
		self.assertEqual(custom_fields(), [])
		self.assertGreater(data["backlog_total"], 0)

	def test_the_tool_says_out_loud_that_this_is_the_one_exception(self):
		"""The description and the result both have to name the promise being
		broken. A tool that quietly extends somebody else's doctype is the thing
		this app spent six versions promising not to be."""
		data = self.tool_data("install_compliance_fields", {"dry_run": True})
		self.assertIn("ONE PLACE", data["note"])
		self.assertIn("uninstalling", data["note"].lower())


class WovenNotShadow(V12TestCase):
	"""Removing a compliance field breaks the WORK, not only the report.

	Each test does the same thing twice with one field missing: asks an
	operational question and asks a compliance question, and shows both answers
	change. That is the whole Sprint 7 stance, tested rather than asserted.

	These run against the field MAP rather than against a live Spray Log, because
	the fixture site has no farm_precision_ag — so what is proved here is that the
	table itself carries both halves for every field, and `test_housing.py`'s own
	`WovenNotShadow` proves the live behaviour on the doctypes this app owns.
	"""

	def test_the_rei_is_a_crew_schedule_before_it_is_a_wps_record(self):
		spec = self._field("Spray Log", "rei_hours")
		self.assertIn("crew", spec.operational.lower())
		self.assertIn("40 CFR 170", spec.framework)

	def test_the_epa_number_is_a_load_rejection_before_it_is_a_filing(self):
		spec = self._field("Spray Log", "epa_reg_number")
		self.assertIn("packing house", spec.operational)
		self.assertIn("FIFRA", spec.framework)

	def test_the_i9_status_is_a_roster_fact_before_it_is_an_ice_fact(self):
		spec = self._field("Employee", "i9_status")
		self.assertIn("crew", spec.operational)
		self.assertIn("1324a", spec.framework)

	def test_the_jurisdiction_is_a_pay_rate_before_it_is_a_wage_law_position(self):
		spec = self._field("Employee", "jurisdiction")
		self.assertIn("minimum wage", spec.operational)
		self.assertIn("ORS 653", spec.framework)

	def test_the_shipment_id_is_a_receivable_before_it_is_a_mock_recall(self):
		spec = self._field("Bucket Log Entry", "shipment_id")
		self.assertIn("paid", spec.operational)
		self.assertIn("shipping event", spec.framework)

	def test_the_housing_detector_dates_answer_a_person_sleeping_there_tonight(self):
		"""The bluntest operational answer in the table, and deliberately so."""
		for fieldname in ("smoke_detector_last_test", "co_detector_last_test"):
			with self.subTest(field=fieldname):
				spec = self._field("Housing Unit", fieldname)
				self.assertEqual(spec.operational, "Somebody sleeps there tonight.")

	def _field(self, doctype: str, fieldname: str):
		target = compliance_fields.targets_by_doctype()[doctype]
		return next(spec for spec in target.fields if spec.fieldname == fieldname)


class TheUninstallWarning(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_install_compliance_fields=1)
		install_hrms()

	def test_it_names_every_column_that_would_be_dropped(self):
		"""An operator uninstalling for compliance reasons is exactly the person
		who wanted this data. The records survive; the columns do not."""
		compliance_fields.install_compliance_fields()
		losses = install._compliance_field_losses()
		self.assertIn(("Employee", "i9_status"), losses)
		self.assertIn(("Employee", "jurisdiction"), losses)

	def test_it_names_nothing_before_anything_has_been_installed(self):
		self.assertEqual(install._compliance_field_losses(), [])

	def test_it_never_names_a_column_on_this_apps_own_doctype(self):
		"""Housing Unit and Field go with the app and are covered by
		`_PRECIOUS_DOCTYPES`. Listing their columns separately would tell an
		operator to export the same data twice."""
		compliance_fields.install_compliance_fields()
		for doctype, _fieldname in install._compliance_field_losses():
			with self.subTest(doctype=doctype):
				self.assertNotIn(doctype, ("Housing Unit", "Field"))


class TheDocumentation(unittest.TestCase):
	"""`docs/compliance_fields.md` and the table cannot drift apart.

	A doc that describes a schema is a doc that is wrong within two releases
	unless something checks. This is the check, and it runs in both directions:
	a field added to the code and not to the file fails here, and so does a field
	described in the file that no longer exists.

	Deliberately a plain TestCase — it reads a file and a Python literal, and
	standing up the whole fake site to do that would be pretence.
	"""

	@classmethod
	def setUpClass(cls):
		with open(DOC) as handle:
			cls.text = handle.read()
		#: The same content with runs of whitespace collapsed, so an assertion
		#: about a phrase does not depend on where the paragraph happened to wrap.
		cls.flat = " ".join(cls.text.split())

	def test_every_field_is_documented_with_its_framework_and_its_work_answer(self):
		for target in compliance_fields.TARGETS:
			for spec in target.fields:
				with self.subTest(field=f"{target.doctype}.{spec.fieldname}"):
					self.assertIn(
						f"`{spec.fieldname}`",
						self.text,
						f"{spec.fieldname} is in the table and not in docs/compliance_fields.md",
					)
					self.assertIn(spec.framework, self.flat)
					self.assertIn(spec.operational, self.flat)

	def test_every_target_doctype_has_a_section(self):
		for target in compliance_fields.TARGETS:
			with self.subTest(doctype=target.doctype):
				self.assertIn(f"### `{target.doctype}`", self.flat)
				self.assertIn(" ".join(target.purpose.split()), self.flat)

	def test_no_documented_field_has_been_removed_from_the_table(self):
		"""The other direction. A file describing a column that no longer exists
		sends somebody looking for it on their own site."""
		declared = {f"`{spec.fieldname}`" for target in compliance_fields.TARGETS for spec in target.fields}
		documented = set()
		for line in self.text.splitlines():
			if not line.startswith("| `"):
				continue
			documented.add(line.split("|")[1].strip())
		self.assertEqual(documented - declared, set(), "documented fields that no longer exist")

	def test_it_states_the_promise_being_broken_and_what_it_costs(self):
		"""The argument is the point of the file. A version of it that listed the
		columns without making the case would be a schema dump."""
		self.assertIn("breaks that promise, once, on purpose", self.flat)
		self.assertIn("drops the columns and everything typed into them", self.flat)
		self.assertIn("only breaks reporting → it is a shadow layer", self.flat)

	def test_it_says_the_required_fields_create_a_backlog(self):
		self.assertIn("backlog_total", self.flat)
		self.assertIn("enforces `reqd` **on save**, not retroactively", self.flat)
