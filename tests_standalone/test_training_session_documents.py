# SPDX-License-Identifier: MIT
"""AFB-2026-00015 — "We should be able to pull up the pdf here please".

WHAT TIM SAW. A Training Session on the handset, two files filed against it in
the Desk — a community-college ticket and the autumn catalogue — and no way to
open either from the phone he was holding at the classroom door.

WHY IT WAS NOT AN APP BUG. `ATTACHMENT_PARENTS` is a closed list and
`Training Session` was not on it, so `list_attachments` refused the doctype by
name before any permission was consulted. The iOS app could have been written
perfectly and still shown an empty folder.

THE GATE IS THE WHOLE OF THE DESIGN HERE, and it is why this entry needed a
third answer beside `True` and `False`:

  * `True` — the HR gate — would be NARROWER THAN THE SESSION'S OWN READ.
    `get_training_session` takes `SHIFT_ROLES`, so a Foreman may open the sheet;
    under an HR gate that same Foreman would open the sheet and be refused the
    handout stapled to it. A gate narrower than the parent's reads as a missing
    file rather than as a refusal, which is the kind nobody reports.
  * `False` — no gate — would be WIDER. It would leave the folder to Frappe's
    DocPerm alone, which is a different question from "may this person run a
    session", and a Field Worker could walk in.

So the value is `SHIFT_GATE` and the claim under test is that the attachment
door and the parent's own door admit exactly the same people.

FOUR CLAIMS.

1. `TheDoorOpens` — a Farm Manager lists the folder and gets the bytes.
2. `TheGateMatchesTheParentsOwnRead` — a Foreman, who may read the session, may
   read its folder; a Field Worker may do neither.
3. `TheScopeStillHolds` — another entity's session reads as not found, through
   the docname and through the File handle both.
4. `TheSentinelIsMatchedExactly` — `SHIFT_GATE` is a truthy string, and the
   truthiness test it replaced would have run the HR gate on it.
"""

import frappe

from erpnext_mcp import roles
from erpnext_mcp.api import guard
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.errors import ToolError
from erpnext_mcp.tools import files as file_tools

from .fixtures import MAIN, OTHER, V12TestCase, install_hrms
from .harness import ROLES, STORE

MANAGER = "tim.polehn+mobile@example.test"
FOREMAN = "luis@example.test"
PICKER = "ana@example.test"

#: The session Tim was standing in front of, and its twin in the other entity.
SESSION = "TRNS-2026-0001"
OUTSIDER_SESSION = "TRNS-2026-0900"

TICKET = "file-trns-ticket"
TICKET_BYTES = b"a photograph of a CGCC applicator-licence ticket"
CATALOGUE = "file-trns-catalogue"
OUTSIDER_FILE = "file-trns-outsider"

ON = {
	f"allow_{name}": 1
	for name in ("create_mobile_user", "list_attachments", "get_attachment_content")
}


class TrainingSessionDocumentsTestCase(V12TestCase):
	"""A site with two sessions in two entities, three phone accounts, one folder."""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		install_hrms()
		self._roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(self._restore_roles)
		guard._BUCKETS.clear()
		roles.install_roles()
		STORE.seed(
			"Training Session",
			[
				{
					"name": SESSION,
					"training_type": "Applicator License Renewal",
					"session_date": "2026-10-28",
					"company": MAIN,
					"status": "Scheduled",
				},
				{
					"name": OUTSIDER_SESSION,
					"training_type": "Applicator License Renewal",
					"session_date": "2026-10-28",
					"company": OTHER,
					"status": "Scheduled",
				},
			],
		)
		STORE.seed(
			"File",
			[
				{
					"name": TICKET,
					"file_name": "WPS-Train-the-Trainer-Ticket011026.jpg",
					"file_url": "/private/files/WPS-Train-the-Trainer-Ticket011026.jpg",
					"file_size": len(TICKET_BYTES),
					"is_private": 1,
					"attached_to_doctype": "Training Session",
					"attached_to_name": SESSION,
					"owner": "Administrator",
				},
				{
					"name": CATALOGUE,
					"file_name": "CGCC-Fall-2026-Catalog3fa341.jpg",
					"file_url": "/private/files/CGCC-Fall-2026-Catalog3fa341.jpg",
					"file_size": 9,
					"is_private": 1,
					"attached_to_doctype": "Training Session",
					"attached_to_name": SESSION,
					"owner": "Administrator",
				},
				{
					"name": OUTSIDER_FILE,
					"file_name": "other-farm.jpg",
					"file_url": "/private/files/other-farm.jpg",
					"file_size": 4,
					"is_private": 1,
					"attached_to_doctype": "Training Session",
					"attached_to_name": OUTSIDER_SESSION,
					"owner": "Administrator",
				},
			],
		)
		STORE.file_contents[TICKET] = TICKET_BYTES
		STORE.file_contents[CATALOGUE] = b"catalogue"
		STORE.file_contents[OUTSIDER_FILE] = b"nope"
		self.enrol(MANAGER, "Tim Polehn", "Farm Manager")
		self.enrol(FOREMAN, "Luis Ortiz", "Foreman")
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

	def be(self, user, remote_addr="100.64.0.7"):
		"""Become one user, on a request that looks like a phone's."""
		self.request({}, headers={}, remote_addr=remote_addr)
		frappe.local.session.user = user
		return user

	def no_session_read(self):
		"""The bench condition: no Frappe read on Training Session.

		A denial rather than a role edit, because the harness's `has_permission`
		is default-allow and there is no set of roles that makes it say no. This
		is the ONE lever that models what a mobile account actually is — the
		doctype grants read to System Manager and Accounts Manager, and a phone
		holds neither.
		"""
		STORE.denied_permissions.add(("Training Session", "read"))
		self.addCleanup(STORE.denied_permissions.discard, ("Training Session", "read"))


# ── 0. the negative control, first ──────────────────────────────────────────
class TheDocPermIsReal(TrainingSessionDocumentsTestCase):
	"""**THE TEST THAT SHOULD HAVE EXISTED IN v0.175.1, AND ITS ABSENCE IS WHY
	TIM'S TICKET DID NOT APPEAR.**

	WHAT WENT WRONG. The allow-list entry passed this surface's three gates and
	then `tools/files._require_parent_read` refused on Frappe's own permission:
	"…is not permitted to read Training Session TRNS-2026-0001, so its
	attachments are not available." `training_session.json` ships DocPerms for
	System Manager and Accounts Manager; `roles.py` grants the phone roles
	nothing on that doctype; so every handset was refused.

	WHY THE ORIGINAL SUITE WENT GREEN ANYWAY. `STORE`'s `has_permission` is
	DEFAULT-ALLOW — `test_employee_documents` says so in its own header and warns
	that "a DocPerm mistake in this repo passes ten thousand tests and fails on
	the bench". Eleven tests passed against a double that answers yes.

	SO THE DENIAL IS MODELLED HERE, and every claim in the classes below is now
	made with it in place. If this class goes green for the wrong reason, the
	rest of this file is measuring nothing.
	"""

	def test_the_tool_refuses_without_the_brokering(self):
		"""The negative control: with the read denied, the TOOL still refuses.
		Delete the brokering and `TheDoorOpens` goes red; delete this denial and
		the whole file stops meaning anything."""
		self.no_session_read()
		with self.assertRaises(ToolError) as caught:
			file_tools.list_attachments({"doctype": "Training Session", "name": SESSION})
		self.assertIn("not permitted to read Training Session", str(caught.exception))

	def test_the_route_reads_it_anyway(self):
		"""The fix. Same denial, same session, and the mobile route lists the
		folder — because the three gates it ran are stricter than the DocPerm it
		skipped."""
		self.no_session_read()
		self.be(MANAGER)
		data = mobile_api.list_attachments(doctype="Training Session", docname=SESSION)
		self.assertEqual(data["count"], 2)

	def test_the_bytes_come_back_under_the_denial_too(self):
		"""The read side of the same brokering — a listing somebody cannot open
		is a list of filenames."""
		self.no_session_read()
		self.no_session_read()
		self.be(MANAGER)
		data = mobile_api.get_attachment_content(file=TICKET)
		self.assertEqual(data["encoding"], "base64")

	def test_the_gates_still_refuse_a_field_worker_under_the_denial(self):
		"""**THE BROKERING IS NOT A WIDENING.** Skipping Frappe's DocPerm did not
		skip `SHIFT_ROLES`: a picker is refused with or without it."""
		self.no_session_read()
		self.be(PICKER)
		with self.assertRaises(frappe.ValidationError):
			mobile_api.list_attachments(doctype="Training Session", docname=SESSION)

	def test_another_entitys_session_is_still_not_found_under_the_denial(self):
		"""And nor did it skip the company scope."""
		self.no_session_read()
		self.be(MANAGER)
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.list_attachments(doctype="Training Session", docname=OUTSIDER_SESSION)


# ── 1. the door opens ───────────────────────────────────────────────────────
class TheDoorOpens(TrainingSessionDocumentsTestCase):
	def test_the_folder_lists(self):
		self.no_session_read()
		self.be(MANAGER)
		data = mobile_api.list_attachments(doctype="Training Session", docname=SESSION)
		self.assertEqual(data["count"], 2)
		self.assertEqual(
			sorted(row["name"] for row in data["attachments"]), sorted([CATALOGUE, TICKET])
		)

	def test_the_ticket_comes_back_as_bytes(self):
		"""The whole point of the feature: the file itself, through the sidecar,
		because a private Frappe file cannot be fetched over its `file_url` from a
		handset — see `AttachmentAPI` on the iOS side."""
		self.no_session_read()
		self.be(MANAGER)
		data = mobile_api.get_attachment_content(file=TICKET)
		self.assertEqual(data["attached_to_doctype"], "Training Session")
		self.assertEqual(data["attached_to_name"], SESSION)
		self.assertEqual(data["encoding"], "base64")

		import base64 as b64

		self.assertEqual(b64.b64decode(data["content"]), TICKET_BYTES)

	def test_the_parent_is_named_in_the_refusal_list_no_longer(self):
		"""The sentence a phone used to get. It named every parent it DOES read,
		which is how this gap was found in the first place."""
		self.be(MANAGER)
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.list_attachments(doctype="Journal Entry", docname="ACC-JV-2026-00001")
		self.assertIn("Training Session", str(caught.exception))


# ── 2. the gate is the parent's own ─────────────────────────────────────────
class TheGateMatchesTheParentsOwnRead(TrainingSessionDocumentsTestCase):
	"""`SHIFT_ROLES` on both doors — no wider, and no narrower.

	THE FOREMAN IS THE WHOLE REASON THIS ENTRY IS NOT `True`. He is the person
	who holds the tailgate session, and `get_training_session` has admitted him
	since v0.92.2. A folder he cannot open is a folder that looks empty.
	"""

	def test_a_foreman_reads_the_folder_and_the_file(self):
		self.no_session_read()
		self.be(FOREMAN)
		data = mobile_api.list_attachments(doctype="Training Session", docname=SESSION)
		self.assertEqual(data["count"], 2)
		self.assertEqual(mobile_api.get_attachment_content(file=TICKET)["encoding"], "base64")

	def test_a_field_worker_is_refused_by_the_same_gate_the_session_uses(self):
		"""Not ungated. A picker may not read the session, and may not walk its
		folder either — with the roles named, because a refusal a supervisor can
		act on beats one that only says no."""
		self.be(PICKER)
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.list_attachments(doctype="Training Session", docname=SESSION)
		self.assertIn("Foreman", str(caught.exception))

	def test_a_field_worker_cannot_reach_the_file_by_its_handle_either(self):
		"""The same gate, reached through the File docname rather than the parent
		— the route derives the parent from the file and gates THAT."""
		self.be(PICKER)
		with self.assertRaises(frappe.ValidationError):
			mobile_api.get_attachment_content(file=TICKET)


# ── 3. the scope still holds ────────────────────────────────────────────────
class TheScopeStillHolds(TrainingSessionDocumentsTestCase):
	def test_another_entitys_session_reads_as_not_found(self):
		"""Not a permission error — the same 'not found' every scoped read gives,
		so a caller cannot map the site's docnames by watching which refusal
		comes back."""
		self.be(MANAGER)
		with self.assertRaises(frappe.DoesNotExistError) as caught:
			mobile_api.list_attachments(doctype="Training Session", docname=OUTSIDER_SESSION)
		self.assertIn("was not found", str(caught.exception))

	def test_another_entitys_file_cannot_be_opened_by_its_docname(self):
		self.be(MANAGER)
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.get_attachment_content(file=OUTSIDER_FILE)


# ── 4. the sentinel ─────────────────────────────────────────────────────────
class TheSentinelIsMatchedExactly(TrainingSessionDocumentsTestCase):
	"""`SHIFT_GATE` is a non-empty string, and the check it replaced was `if
	gate:`. Under that check every shift-gated parent would have run the HR gate
	— the Foreman above would be refused, and the entry would silently mean the
	opposite of what it says."""

	def test_the_training_session_carries_the_shift_gate(self):
		self.assertIs(
			mobile_api.ATTACHMENT_PARENTS["Training Session"], mobile_api.SHIFT_GATE
		)

	def test_the_sentinel_is_truthy_which_is_why_it_is_compared_by_identity(self):
		self.assertTrue(bool(mobile_api.SHIFT_GATE))
		self.assertIsNot(mobile_api.SHIFT_GATE, True)

	def test_every_other_parent_still_carries_a_bool(self):
		"""One parent needed a third answer. If a second one ever does, this is
		the test that makes somebody write down why."""
		others = {
			parent: gate
			for parent, gate in mobile_api.ATTACHMENT_PARENTS.items()
			if parent != "Training Session"
		}
		self.assertTrue(all(gate is True or gate is False for gate in others.values()))
