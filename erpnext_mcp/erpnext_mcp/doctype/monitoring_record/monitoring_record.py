# SPDX-License-Identifier: MIT
"""Controller for Monitoring Record — actual monitoring log entries.

Each Monitoring Record captures a single measurement taken against a Preventive
Control's monitoring specification. The controller auto-computes whether the
measured value falls within the control's critical limit, so the field worker
recording the measurement does not have to look up the limit and decide — the
system makes the determination, and deviations are immediately visible.
"""

import frappe
from frappe import _
from frappe.model.document import Document

#: Operator functions for critical limit comparison.
OPERATORS = {
	"<=": lambda v, lim: v <= lim,
	">=": lambda v, lim: v >= lim,
	"<": lambda v, lim: v < lim,
	">": lambda v, lim: v > lim,
	"=": lambda v, lim: abs(v - lim) < 1e-9,
}


class MonitoringRecord(Document):
	def validate(self):
		if not self.food_safety_plan:
			frappe.throw(_("Food Safety Plan is required."))
		if not self.preventive_control:
			frappe.throw(_("Preventive Control is required."))
		if self.monitoring_date and str(self.monitoring_date) > frappe.utils.today():
			frappe.throw(_("Monitoring Date {0} is in the future.").format(self.monitoring_date))

		self._compute_within_limit()

	def _compute_within_limit(self):
		"""Look up the preventive control's critical limit and check the measured value."""
		if self.measured_value is None or not self.preventive_control:
			return

		pc = frappe.db.get_value(
			"Preventive Control",
			self.preventive_control,
			["critical_limit", "critical_limit_operator"],
			as_dict=True,
		)
		if not pc or pc.critical_limit is None or not pc.critical_limit_operator:
			return

		op_fn = OPERATORS.get(str(pc.critical_limit_operator).strip())
		if not op_fn:
			return

		self.is_within_limit = 1 if op_fn(self.measured_value, pc.critical_limit) else 0
