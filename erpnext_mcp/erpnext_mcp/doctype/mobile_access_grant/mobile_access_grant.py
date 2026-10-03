# SPDX-License-Identifier: MIT
"""Controller for Mobile Access Grant — the story around an access Frappe keeps.

Frappe already knows who has a login, which roles they hold and which companies
their User Permissions allow. It knows none of the things an audit actually
asks: who decided this person should have a phone, what it was for, when the
credential was issued, whether anybody has looked at it since, and — the one
that matters most — why it was taken away.

THE DOCNAME IS THE EMAIL, WHICH MAKES "ONE GRANT PER PERSON" TRUE BY
CONSTRUCTION. Re-issuing a token updates this row. A second row for the same
worker cannot exist, so there is no version of "check the other one" and no way
for two grants to disagree about whether somebody still works here. The history
is in the Version trail (`track_changes`), which is where a history belongs.

A REVOKED GRANT NEEDS A REASON. The controller refuses without one, for exactly
the reason `reject_farm_task` refuses a rejection without one: "access ended"
and "access ended because they left at the end of harvest" are the same fact
with and without the part somebody will need. Six months later the reason is the
only thing on the row that cannot be reconstructed from anywhere else.

THE SECRET IS NOT HERE. `api_key` — the public half, the part before the colon —
is recorded so an operator reading an access log can tell whose key it was.
The secret lives encrypted in Frappe's own password store and exists in
plaintext for exactly the length of one `generate_api_token` response. There is
no field for it on this doctype and there must never be one: a credential that
sits in a readable column is a credential that leaks with the first CSV export
somebody takes of this list.

`token_expires_on` IS A REVIEW DATE AND THIS APP SAYS SO EVERYWHERE. Frappe API
secrets do not expire on their own. This app installs no scheduled job that
revokes one, because such a job would write another app's User records on a
timer with nobody watching, and that is not a thing this app does — see
`hooks.py`, which declares exactly two scheduled jobs and argues for both.
`list_mobile_users` flags an overdue grant loudly and `revoke_api_token` is what
actually ends it. Saying "expiry" and meaning "reminder" would be the kind of
false assurance that is worse than no assurance at all.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from erpnext_mcp import roles

ACTIVE = "Active"
EXPIRED = "Expired"
REVOKED = "Revoked"
STATES = (ACTIVE, EXPIRED, REVOKED)


class MobileAccessGrant(Document):
	def autoname(self):
		self.name = str(self.user or "").strip()

	def validate(self):
		self.user = str(self.user or "").strip()
		if not self.user:
			frappe.throw(_("A Mobile Access Grant with no user is a record about nobody."))

		self.mobile_role = str(self.mobile_role or "").strip()
		if not self.mobile_role:
			frappe.throw(
				_(
					"Mobile Role is required. An account nobody can say the purpose of is an "
					"account nobody can decide to keep or end."
				)
			)

		self.state = str(self.state or ACTIVE).strip() or ACTIVE
		if self.state not in STATES:
			frappe.throw(
				_("State {0} is not one of: {1}.").format(self.state, ", ".join(STATES)),
				title=_("Unknown grant state"),
			)

		reason = str(self.revocation_reason or "").strip()
		if self.state == REVOKED and not reason:
			frappe.throw(
				_(
					"A revoked grant needs a revocation reason. 'Left at the end of harvest', "
					"'phone lost in the orchard' and 'dismissed for cause' are three different "
					"answers to the same auditor question, and a blank field is none of them."
				),
				title=_("No Revocation Reason"),
			)
		if self.state == REVOKED and not self.revoked_on:
			self.revoked_on = frappe.utils.now()

		if self.state != REVOKED and reason and not self.revoked_on:
			# Somebody typed a reason and left the state alone. That is almost
			# certainly a revocation half-made, and silently keeping the account
			# active would be the worst of the three possible readings.
			frappe.throw(
				_(
					"This grant carries a revocation reason but its state is {0}. Set the state "
					"to Revoked, or clear the reason — an account that reads as active and "
					"explains why it ended is a record nobody can act on."
				).format(self.state)
			)

		if int(self.token_issue_count or 0) < 0:
			self.token_issue_count = 0

		self.entity_access = _tidy_lines(self.entity_access)

		# v0.223.0. The durable record of a phone's access belongs on the person's
		# Employee file: filled from Employee.user_id on every write.
		if not self.get("employee"):
			try:
				if frappe.db.exists("DocType", "Employee"):
					self.employee = frappe.db.get_value("Employee", {"user_id": self.user}, "name") or None
			except Exception:  # pragma: no cover - a site mid-migrate
				pass


def _tidy_lines(raw) -> str:
	"""One entity per line, in order, with the blanks and duplicates gone.

	THE COMMA IS NOT A SEPARATOR ANY MORE — not unconditionally. This function
	used to do `str(raw).replace(",", "\\n")`, which is correct for the "A, B"
	somebody types into a form and catastrophic for "Orchard Meadow, LLC", which
	it filed as two entities that scope nothing. `roles.split_entity_names` tries
	the comma and then CHECKS it against the Company register, so a name that
	contains one survives and a line that really was two companies still splits.
	Argued at length where it lives; both readers of this column call it, so the
	way in and the way out cannot disagree.
	"""
	if not raw:
		return ""
	return roles.tidy_entity_access(raw)


def entity_lines(raw) -> list:
	"""The entity_access field as a list. The inverse of `_tidy_lines`."""
	return roles.parse_entity_access(raw)
