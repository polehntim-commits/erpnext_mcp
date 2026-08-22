# SPDX-License-Identifier: MIT
"""Controller for Strategic Plan — superseded, never overwritten.

THE VERSION IS DERIVED FROM THE PLAN THIS ONE REPLACES and is read-only. A
version number somebody types is a version number that will eventually be typed
twice, and two plans both calling themselves v3 is a chain nobody can order.

A PLAN CANNOT SUPERSEDE ITSELF, and the check walks the whole chain rather than
comparing one link. A cycle — A replaces B replaces A — is not a data-entry
oddity: it makes `version` non-terminating and it makes the question "what did
we say before this" unanswerable, which is the question this doctype exists for.

SUPERSEDING A PLAN RETIRES IT, HERE, ON SAVE. The predecessor moves to Historical
and gets today's retired date if it has none. Doing it as a side effect is a real
decision and worth naming: the alternative is asking the author to go and edit
the old plan too, and the old plan then stays Implemented for ever on the
occasions they do not — which reads as two live strategies.
"""

import frappe
from frappe import _
from frappe.model.document import Document

DEVELOPING = "Developing"
DEVELOPED = "Developed"
IMPLEMENTED = "Implemented"
HISTORICAL = "Historical"
STATUSES = (DEVELOPING, DEVELOPED, IMPLEMENTED, HISTORICAL)

#: How far back the supersession chain is walked looking for a cycle. A farm
#: with more than this many generations of written strategy has other problems.
MAX_CHAIN = 50


class StrategicPlan(Document):
	def validate(self):
		self.plan_name = str(self.plan_name or "").strip()
		if not self.plan_name:
			frappe.throw(_("Plan Name is required."))
		if self.status not in STATUSES:
			self.status = DEVELOPING

		self._check_chain()
		self._version()
		self._check_dates()

	def _check_chain(self) -> None:
		if not self.previous_version:
			return
		if self.previous_version == self.name:
			frappe.throw(_("A plan cannot supersede itself."))

		seen = {self.name} if self.name else set()
		cursor = self.previous_version
		for _step in range(MAX_CHAIN):
			if not cursor:
				return
			if cursor in seen:
				frappe.throw(
					_(
						"This would make the supersession chain a loop, which leaves 'what did we "
						"say before this' with no answer. {0} is already somewhere behind this plan."
					).format(cursor),
					title=_("Circular Plan History"),
				)
			seen.add(cursor)
			cursor = frappe.db.get_value("Strategic Plan", cursor, "previous_version")

	def _version(self) -> None:
		if not self.previous_version:
			self.version = int(self.version or 1) or 1
			return
		previous = frappe.utils.cint(frappe.db.get_value("Strategic Plan", self.previous_version, "version"))
		self.version = previous + 1

	def _check_dates(self) -> None:
		if self.retired_date and self.effective_date:
			if str(self.retired_date) < str(self.effective_date):
				frappe.throw(
					_("This plan is retired ({0}) before it takes effect ({1}).").format(
						self.retired_date, self.effective_date
					)
				)
		if self.status == HISTORICAL and not self.retired_date:
			self.retired_date = frappe.utils.today()

	def on_update(self):
		"""Retire the plan this one replaces. See the module docstring."""
		if not self.previous_version or self.status == HISTORICAL:
			return
		row = (
			frappe.db.get_value(
				"Strategic Plan", self.previous_version, ["status", "retired_date"], as_dict=True
			)
			or {}
		)
		if not row or row.get("status") == HISTORICAL:
			return
		frappe.db.set_value(
			"Strategic Plan",
			self.previous_version,
			{"status": HISTORICAL, "retired_date": row.get("retired_date") or frappe.utils.today()},
			update_modified=False,
		)
