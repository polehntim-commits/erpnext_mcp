# SPDX-License-Identifier: MIT
"""Item 17 — the farm owner's own handset could not open an employee's folder.

WHAT THE FARM OWNER SAW. Documents section, one employee, and this back:

    tim.polehn+mobile@gmail.com is not permitted to read Employee HR-EMP-00011,
    so its attachments are not available.

That sentence is `tools/files._require_parent_read`, and it was CORRECT. The
account holds `Farm Manager`, which `employee.HR_ROLES` accepts and which this
app's own gates pass; what it does not hold is a Frappe DocPerm on `Employee`,
because `Employee` belongs to Frappe HR and `roles.py` rule 1 forbids this app
writing a Custom DocPerm on another app's doctype. v0.62.0 answered that by
assigning `HR User` as a COMPANION ROLE at enrolment — and a companion role
cannot be assigned on a bench with no `hrms` (there is no such role to assign)
and was never assigned to an account enrolled before that release.

SO THE TESTS HERE ARE WRITTEN AGAINST THE DENIAL RATHER THAN AGAINST THE ROLE.
`STORE.denied_permissions` is the harness's only way to make `frappe.has_permission`
say no — the double is DEFAULT-ALLOW, which is exactly why a DocPerm mistake in
this repo passes ten thousand tests and fails on the bench. Every claim below
that matters is paired with a NEGATIVE CONTROL proving the denial is real: if
`ATheToolStillRefuses` ever goes green for the wrong reason, the rest of this
file is measuring nothing.

FIVE CLAIMS.

1. `TheDenialIsReal` — the negative control, first, on purpose. With the Employee
   read denied, `files.list_attachments` and `files.get_attachment_content` still
   refuse in the tool's own voice. Delete the brokering and this class is the one
   that stays green; delete the DENIAL and this class is the one that goes red.

2. `TheOwnerReadsTheFolder` — the fix. The same denial, the same employee, and
   the mobile route lists the folder and hands back the bytes.

3. `TheBrokerIsNotAWidening` — the four gates that still refuse. Wrong company,
   wrong role, a parent off the list, and an unattached file.

4. `TheOtherPersonnelParentsAreNotBrokered` — `Farm Payroll Entry` and
   `Farm Incident Record` are still decided by Frappe, because one holds the
   whole crew's wages and the other is somebody's disciplinary file.

5. `TheReaderCannotBeWalked` — `attachment_content_on_authorized_parent` refuses
   a File that hangs off a parent other than the one it was given, which is the
   property that keeps a global File handle from being a way round gate 3.
"""

import frappe

from erpnext_mcp import roles
from erpnext_mcp.api import guard
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.errors import ToolError
from erpnext_mcp.tools import discipline as discipline_tools
from erpnext_mcp.tools import files as file_tools
from erpnext_mcp.tools import payroll as payroll_tools

from .fixtures import MAIN, OTHER, V12TestCase, install_hrms
from .harness import ROLES, STORE

OWNER = "tim.polehn+mobile@example.test"
OWNER_EMPLOYEE = "EMP-OWNER"
PICKER = "ana@example.test"
PICKER_EMPLOYEE = "EMP-ANA"

#: The employee whose Documents section is the whole of item 17. In MAIN, which
#: is the company the owner's grant names.
SUBJECT = "HR-EMP-00011"
#: The same person's record in the OTHER entity. Nothing about the fix may make
#: this one reachable.
OUTSIDER = "HR-EMP-00012"

LICENCE = "file-emp-licence"
LICENCE_BYTES = b"a photograph of a driving licence"
OUTSIDER_FILE = "file-outsider-licence"

ON = {f"allow_{name}": 1 for name in ("create_mobile_user", "list_attachments", "get_attachment_content")}


class EmployeeDocumentsTestCase(V12TestCase):
	"""A site with an owner, a picker, two employees in two entities, and a folder."""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		install_hrms()
		self._roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(self._restore_roles)
		guard._BUCKETS.clear()
		roles.install_roles()
		STORE.seed(
			"Employee",
			[
				{
					"name": OWNER_EMPLOYEE,
					"employee_name": "Tim Polehn",
					"user_id": OWNER,
					"company": MAIN,
					"status": "Active",
				},
				{
					"name": PICKER_EMPLOYEE,
					"employee_name": "Ana Ramos",
					"user_id": PICKER,
					"company": MAIN,
					"status": "Active",
				},
				{
					"name": SUBJECT,
					"employee_name": "Rosa Delgado",
					"company": MAIN,
					"status": "Active",
				},
				{
					"name": OUTSIDER,
					"employee_name": "Marco Delgado",
					"company": OTHER,
					"status": "Active",
				},
			],
		)
		STORE.seed(
			"File",
			[
				{
					"name": LICENCE,
					"file_name": "licence.jpg",
					"file_url": "/private/files/licence.jpg",
					"file_size": len(LICENCE_BYTES),
					"is_private": 1,
					"attached_to_doctype": "Employee",
					"attached_to_name": SUBJECT,
					"owner": OWNER,
				},
				{
					"name": OUTSIDER_FILE,
					"file_name": "outsider.jpg",
					"file_url": "/private/files/outsider.jpg",
					"file_size": 4,
					"is_private": 1,
					"attached_to_doctype": "Employee",
					"attached_to_name": OUTSIDER,
					"owner": OWNER,
				},
			],
		)
		STORE.file_contents[LICENCE] = LICENCE_BYTES
		STORE.file_contents[OUTSIDER_FILE] = b"nope"
		self.enrol(OWNER, "Tim Polehn", "Farm Manager")
		self.enrol(PICKER, "Ana Ramos", "Field Worker")

	def _restore_roles(self):
		ROLES.clear()
		ROLES.update(self._roles_before)

	def enrol(self, email, name, role, entities=None):
		return self.tool_data(
			"create_mobile_user",
			{
				"email": email,
				"full_name": name,
				"role": role,
				"entity_access": entities or [MAIN],
			},
		)

	def be(self, user=OWNER, remote_addr="100.64.0.7"):
		"""Become one user, on a request that looks like a phone's."""
		self.request({}, headers={}, remote_addr=remote_addr)
		frappe.local.session.user = user
		return user

	def no_employee_read(self):
		"""The bench condition: this account holds no Frappe read on Employee.

		A denial rather than a role edit, because the harness's `has_permission`
		is default-allow and there is no set of roles that makes it say no. This
		is the ONE lever that models what the owner's account actually is.
		"""
		STORE.denied_permissions.add(("Employee", "read"))
		self.addCleanup(STORE.denied_permissions.discard, ("Employee", "read"))


# ── 1. the negative control ─────────────────────────────────────────────────
class TheDenialIsReal(EmployeeDocumentsTestCase):
	"""Before anything claims to have got round the DocPerm, prove there is one.

	`green-proves-passing-not-why`: the double answers yes to
	`frappe.has_permission` unless something put the pair in `denied_permissions`,
	so a brokering test that forgot the denial would pass identically against the
	unbrokered code. These two assertions are what make the next class mean
	something, and they are deliberately written against the TOOL rather than
	against the route — the tool is the layer that still refuses.
	"""

	def test_the_tool_refuses_to_list_the_folder(self):
		self.no_employee_read()
		with self.assertRaises(ToolError) as caught:
			file_tools.list_attachments({"doctype": "Employee", "name": SUBJECT})
		self.assertIn("not permitted to read Employee", str(caught.exception))
		self.assertIn("docs/security.md", str(caught.exception))

	def test_the_tool_refuses_to_open_the_file(self):
		self.no_employee_read()
		with self.assertRaises(ToolError) as caught:
			file_tools.get_attachment_content({"name": LICENCE})
		self.assertIn("not permitted to read", str(caught.exception))

	def test_without_the_denial_the_tool_allows_it(self):
		"""The other half of the control: the denial is what refuses, not the fixture.

		If this ever fails, the folder was empty or the seed was wrong and the two
		refusals above were about something other than the permission.
		"""
		data = file_tools.list_attachments({"doctype": "Employee", "name": SUBJECT}).data
		self.assertEqual(data["count"], 1)


# ── 2. the fix ──────────────────────────────────────────────────────────────
class TheOwnerReadsTheFolder(EmployeeDocumentsTestCase):
	"""The Documents section opens, on the account that could not open it."""

	def test_the_folder_lists(self):
		self.no_employee_read()
		self.be(OWNER)
		data = mobile_api.list_attachments(doctype="Employee", docname=SUBJECT)
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["attachments"][0]["name"], LICENCE)
		self.assertEqual(data["attachments"][0]["file_name"], "licence.jpg")
		self.assertTrue(data["attachments"][0]["is_private"])
		self.assertTrue(data["attachments"][0]["retrievable"])

	def test_the_file_opens(self):
		import base64

		self.no_employee_read()
		self.be(OWNER)
		data = mobile_api.get_attachment_content(file=LICENCE)
		self.assertEqual(base64.b64decode(data["content"]), LICENCE_BYTES)
		self.assertEqual(data["attached_to_doctype"], "Employee")
		self.assertEqual(data["attached_to_name"], SUBJECT)
		# Three spellings of two facts, unchanged by the brokering. A client
		# written against the contract's keys reads the same bytes off either path.
		self.assertEqual(data["content"], data["content_base64"])
		self.assertEqual(data["encoding"], "base64")

	def test_the_default_spelling_of_the_parent_is_employee(self):
		"""`_attachment_parent` falls back to Employee, so the phone may omit it —
		and the brokering has to be keyed off the RESOLVED parent rather than off
		the argument, or the fallback path would go through the tool and refuse."""
		self.no_employee_read()
		self.be(OWNER)
		data = mobile_api.list_attachments(docname=SUBJECT)
		self.assertEqual(data["doctype"], "Employee")
		self.assertEqual(data["count"], 1)

	def test_the_brokered_set_is_two_entries_and_both_are_parents(self):
		"""The set is short and every entry has to be a parent this surface
		already reads. A doctype brokered but not on `ATTACHMENT_PARENTS` would be
		unreachable code that looks like a permission.

		v0.176.2 ADDS `Training Session` — the same bug this file is about, found
		on a farm for the same reason: its DocPerms are System Manager and
		Accounts Manager, and a phone account holds neither. See
		`test_training_session_documents.TheDocPermIsReal`."""
		self.assertEqual(mobile_api.BROKERED_PARENTS,
		                 frozenset({"Employee", "Training Session"}))
		self.assertTrue(mobile_api.BROKERED_PARENTS <= set(mobile_api.ATTACHMENT_PARENTS))


# ── 3. what still refuses ───────────────────────────────────────────────────
class TheBrokerIsNotAWidening(EmployeeDocumentsTestCase):
	"""Skipping Frappe's DocPerm did not skip this surface's own three gates.

	Each of these was refused before the change and is refused after it, and each
	is refused by a DIFFERENT gate — which is the claim worth pinning, because a
	fix that moved the refusal onto one gate would look identical from a handset
	right up until that gate was the one edited.
	"""

	def test_an_employee_in_another_entity_reads_as_not_found(self):
		"""Gate 3, `require_scoped_doc`. Not a permission error — the same 'not
		found' every scoped read gives, so a caller cannot map the site's docnames
		by watching which refusal comes back."""
		self.no_employee_read()
		self.be(OWNER)
		with self.assertRaises(frappe.DoesNotExistError) as caught:
			mobile_api.list_attachments(doctype="Employee", docname=OUTSIDER)
		self.assertIn("was not found", str(caught.exception))

	def test_another_entitys_file_cannot_be_opened_by_its_docname(self):
		"""The same gate, reached through the File handle rather than the parent.
		The route derives the parent from the file and scopes THAT."""
		self.no_employee_read()
		self.be(OWNER)
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.get_attachment_content(file=OUTSIDER_FILE)

	def test_a_field_worker_is_still_refused(self):
		"""Gate 2, `employee.HR_ROLES`. A picker holding a phone may not read
		somebody else's personnel folder, brokered or not — and this is the gate
		the brokering leans on hardest, because it is now the only thing between a
		phone role and an Employee's identity documents."""
		self.no_employee_read()
		self.be(PICKER)
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.list_attachments(doctype="Employee", docname=SUBJECT)
		# `require_hr_role` raises a `ToolError`, which `guard.endpoint` turns into
		# a validation error so the sentence reaches the phone in `_server_messages`.
		# The four role names are in it, which is what makes it actionable.
		self.assertIn("personnel register", str(caught.exception))
		self.assertIn("Farm Manager", str(caught.exception))

	def test_a_parent_off_the_list_is_still_refused_by_name(self):
		"""Gate 1, `ATTACHMENT_PARENTS`. The brokering changed nothing about which
		doctypes this door opens at all."""
		self.be(OWNER)
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.list_attachments(doctype="Journal Entry", docname="ACC-JV-2026-00001")
		self.assertIn("not a record this surface reads attachments from", str(caught.exception))

	def test_an_unattached_file_is_still_refused(self):
		"""There is no parent to broker against, so there is nothing to broker."""
		STORE.seed(
			"File",
			[{"name": "file-loose", "file_name": "loose.jpg", "file_size": 3, "is_private": 1}],
		)
		STORE.file_contents["file-loose"] = b"abc"
		self.no_employee_read()
		self.be(OWNER)
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.get_attachment_content(file="file-loose")
		self.assertIn("attached to no document", str(caught.exception))

	def test_a_docname_that_does_not_exist_is_not_an_empty_folder(self):
		"""`_require_parent_exists` is split out of `_require_parent_read` for
		this: a brokered read that skipped the existence check too would answer a
		cheerful empty list for a record that was never on the site."""
		self.be(OWNER)
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.list_attachments(doctype="Employee", docname="HR-EMP-NOPE")


# ── 4. the parents that are deliberately not brokered ───────────────────────
class TheOtherPersonnelParentsAreNotBrokered(EmployeeDocumentsTestCase):
	"""Two doctypes whose DocPerm is the gate ON PURPOSE, and stays the gate.

	`Farm Payroll Entry` holds one slip per person on the run, so brokering it
	would put the crew's wages in front of every Farm Manager — the exact reflex
	`routes.py` says to avoid. `Farm Incident Record` is somebody's disciplinary
	file, and v0.96.0 put it on `ATTACHMENT_PARENTS` with the explicit promise
	that the entry "does not manufacture a permission for anybody else".

	BOTH ARE FLAGGED `True` ON `ATTACHMENT_PARENTS`, so both pass the HR gate for
	a Farm Manager. Frappe's DocPerm is the ONLY thing refusing them, which is
	what makes them the right test of whether the brokering leaked.
	"""

	def a_payroll_run(self):
		STORE.seed(
			payroll_tools.PAYROLL_ENTRY,
			[{"name": "FPE-2026-0001", "company": MAIN, "docstatus": 1}],
		)
		STORE.seed(
			"File",
			[
				{
					"name": "file-stub",
					"file_name": "stub.pdf",
					"file_size": 4,
					"is_private": 1,
					"attached_to_doctype": payroll_tools.PAYROLL_ENTRY,
					"attached_to_name": "FPE-2026-0001",
					"owner": OWNER,
				}
			],
		)
		STORE.file_contents["file-stub"] = b"pay!"
		return "FPE-2026-0001"

	def a_warning(self):
		STORE.seed(
			discipline_tools.DISCIPLINE,
			[{"name": "FIR-2026-0001", "company": MAIN, "employee": SUBJECT}],
		)
		return "FIR-2026-0001"

	def test_a_payroll_run_is_still_decided_by_frappe(self):
		self.a_payroll_run()
		STORE.denied_permissions.add((payroll_tools.PAYROLL_ENTRY, "read"))
		self.addCleanup(STORE.denied_permissions.discard, (payroll_tools.PAYROLL_ENTRY, "read"))
		self.be(OWNER)
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.list_attachments(doctype=payroll_tools.PAYROLL_ENTRY, docname="FPE-2026-0001")
		self.assertIn("not permitted to read", str(caught.exception))

	def test_a_crews_stub_cannot_be_opened_through_the_folder(self):
		self.a_payroll_run()
		STORE.denied_permissions.add((payroll_tools.PAYROLL_ENTRY, "read"))
		self.addCleanup(STORE.denied_permissions.discard, (payroll_tools.PAYROLL_ENTRY, "read"))
		self.be(OWNER)
		with self.assertRaises(frappe.ValidationError):
			mobile_api.get_attachment_content(file="file-stub")

	def test_a_disciplinary_file_is_still_decided_by_frappe(self):
		self.a_warning()
		STORE.denied_permissions.add((discipline_tools.DISCIPLINE, "read"))
		self.addCleanup(STORE.denied_permissions.discard, (discipline_tools.DISCIPLINE, "read"))
		self.be(OWNER)
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.list_attachments(doctype=discipline_tools.DISCIPLINE, docname="FIR-2026-0001")
		self.assertIn("not permitted to read", str(caught.exception))

	def test_neither_is_on_the_brokered_set(self):
		"""The durable form of the two claims above. Add either name to
		`BROKERED_PARENTS` and this fails by name rather than three classes away."""
		self.assertNotIn(payroll_tools.PAYROLL_ENTRY, mobile_api.BROKERED_PARENTS)
		self.assertNotIn(discipline_tools.DISCIPLINE, mobile_api.BROKERED_PARENTS)
		self.assertNotIn("I-9 Form", mobile_api.BROKERED_PARENTS)


# ── 5. the reader's own signature ───────────────────────────────────────────
class TheReaderCannotBeWalked(EmployeeDocumentsTestCase):
	"""A File docname is a global handle, and the brokered reader takes a parent too.

	This is the property that keeps the fix from being a hole. The mobile route
	derives the parent from the file before it scopes anything, so this pairing
	is not reachable from a handset today — the test is here because the day a
	second caller appears it will not necessarily have done it in that order, and
	the refusal has to be the function's own rather than its caller's.
	"""

	def test_a_file_on_another_parent_is_refused(self):
		with self.assertRaises(ToolError) as caught:
			file_tools.attachment_content_on_authorized_parent("Employee", SUBJECT, OUTSIDER_FILE)
		self.assertIn("is not attached to Employee", str(caught.exception))
		self.assertIn("Nothing was read", str(caught.exception))

	def test_the_matching_pair_is_read(self):
		import base64

		result = file_tools.attachment_content_on_authorized_parent("Employee", SUBJECT, LICENCE)
		self.assertEqual(base64.b64decode(result.data["content_base64"]), LICENCE_BYTES)

	def test_the_size_ceiling_still_applies(self):
		"""`_resolve_max_bytes` is shared with the tool, so a brokered read is not
		a way past the cap the tool enforces."""
		with self.assertRaises(ToolError) as caught:
			file_tools.attachment_content_on_authorized_parent("Employee", SUBJECT, LICENCE, max_bytes=4)
		self.assertIn("over the", str(caught.exception))

	def test_neither_brokered_reader_is_a_published_tool(self):
		"""They are library functions for a caller in this repo that has done the
		check itself, and `registry.py` must never publish either — an MCP client
		holding a bearer token would be exactly the caller they are not for."""
		from erpnext_mcp import registry

		self.assertNotIn("list_attachments_on_authorized_parent", registry.TOOLS)
		self.assertNotIn("attachment_content_on_authorized_parent", registry.TOOLS)
