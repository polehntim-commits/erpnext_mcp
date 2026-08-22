# SPDX-License-Identifier: MIT
"""Controller for Strategic Objective — one measurable promise from a plan.

AN ACTUAL WITH NO DATE IS REFUSED, and it is the one rule here worth arguing for.
A KPI actual is read to answer "how are we doing", and a figure with no date
cannot be told from one recorded eighteen months ago — so it is either believed
and wrong, or discounted and useless. Recording when it was taken costs nothing
at the moment somebody has the number in front of them and is unrecoverable
afterwards.

ACHIEVED WITHOUT AN ACTUAL IS ALSO REFUSED, for the matching reason: an objective
marked achieved with nothing in the actual column is the single most flattering
row a plan can contain and the one nobody can check.

THE COMPANY AND PLAN ARE CHECKED AGAINST EACH OTHER. An objective filed under one
entity against another entity's plan would appear in neither's rollup correctly,
and on a bench carrying two operations that is a silent miscount rather than an
error anybody sees.
"""

import frappe
from frappe import _
from frappe.model.document import Document

PENDING = "Pending"
IN_PROGRESS = "In Progress"
ACHIEVED = "Achieved"
FAILED = "Failed"
STATUSES = (PENDING, IN_PROGRESS, ACHIEVED, FAILED)

#: The statuses that mean somebody has finished with this objective. Both are
#: terminal and both are kept — see the doctype description on why `Failed` is
#: not deleted.
SETTLED = (ACHIEVED, FAILED)


class StrategicObjective(Document):
	def validate(self):
		self.objective = str(self.objective or "").strip()
		if not self.objective:
			frappe.throw(_("Objective is required."))
		if not self.strategic_plan:
			frappe.throw(_("Strategic Plan is required — a loose objective belongs to no strategy."))
		if self.status not in STATUSES:
			self.status = PENDING

		plan_company = frappe.db.get_value("Strategic Plan", self.strategic_plan, "company")
		if plan_company and self.company and plan_company != self.company:
			frappe.throw(
				_(
					"This objective is filed under {0} against {1}, which is {2}'s plan. It would "
					"appear in neither operation's rollup correctly."
				).format(self.company, self.strategic_plan, plan_company),
				title=_("Objective And Plan Disagree"),
			)

		actual = str(self.kpi_actual or "").strip()
		self.kpi_actual = actual
		if actual and not self.measured_on:
			frappe.throw(
				_(
					"A KPI actual of {0!r} was recorded with no measurement date. An undated "
					"actual cannot be told from one taken eighteen months ago, so it is either "
					"believed and wrong or discounted and useless."
				).format(actual),
				title=_("Undated Measurement"),
			)
		if self.status == ACHIEVED and not actual:
			frappe.throw(
				_(
					"This objective is marked Achieved with nothing in the actual column — the "
					"most flattering row a plan can carry and the one nobody can check. Record "
					"what was achieved."
				),
				title=_("Achieved With No Measurement"),
			)
