# SPDX-License-Identifier: MIT
"""Controller for Acquisition Target — four scores, and the composite it derives.

THE COMPOSITE IS RECOMPUTED ON EVERY SAVE AND IS READ-ONLY, for the same reason
the Field's boundary-derived acreage is: a summary figure somebody can edit
independently of the figures it summarises is a figure that will eventually
disagree with them, and the disagreement is discovered by whoever is reading the
summary to make a decision.

THE WEIGHTS ARE STATED IN CODE RATHER THAN CONFIGURED, and they are equal. Any
weighting is an argument, and an equal one is at least an argument a reader can
see and correct in one place. What the weights must NOT do is let a strong
strategic fit hide a cultural fit of zero — so `accretive_score` is a mean and
`weakest_dimension` names the lowest input beside it, because the mean is what
gets sorted on and the weakest score is what kills deals.

A TARGET WITH NO SCORES SCORES NOTHING. `accretive_score` is left empty rather
than set to zero: an unassessed target and a target assessed as worthless must
not sort together, and zero is an answer.
"""

import frappe
from frappe import _
from frappe.model.document import Document

CLOSED = "Closed"
PASSED = "Passed"

#: The four dimensions, in the order they are read on the form. Equal weight —
#: see the module docstring.
FIT_FIELDS = (
	"strategic_fit_score",
	"financial_health_score",
	"synergy_score",
	"cultural_fit_score",
)


class AcquisitionTarget(Document):
	def validate(self):
		self.entity_name = str(self.entity_name or "").strip()
		if not self.entity_name:
			frappe.throw(_("Entity Name is required."))

		for fieldname in FIT_FIELDS:
			value = self.get(fieldname)
			if value in (None, ""):
				continue
			if not 0 <= float(value) <= 1:
				frappe.throw(
					_(
						"{0} is {1}. The fit scores run from 0 to 1 — a percentage typed here "
						"reads as sixty times better than perfect."
					).format(fieldname, value)
				)

		if int(self.intergenerational_horizon_years or 0) < 0:
			frappe.throw(_("The intergenerational horizon cannot be negative."))
		if float(self.acreage or 0) < 0:
			frappe.throw(_("Acreage cannot be negative."))

		self._check_dates()
		self._score()

	def _check_dates(self) -> None:
		if self.target_close_date and self.identified_date:
			if str(self.target_close_date) < str(self.identified_date):
				frappe.throw(
					_("The target close ({0}) is before the target was identified ({1}).").format(
						self.target_close_date, self.identified_date
					)
				)
		if self.status == CLOSED and not self.actual_close_date:
			frappe.throw(
				_(
					"This target is marked Closed with no actual close date. A deal that closed "
					"closed on a day, and that date is what every holding-period and return "
					"figure is measured from."
				),
				title=_("Closed With No Close Date"),
			)

	def _score(self) -> None:
		"""The mean of whichever fit scores were actually filled in."""
		scored = [float(self.get(f)) for f in FIT_FIELDS if self.get(f) not in (None, "")]
		self.accretive_score = round(sum(scored) / len(scored), 3) if scored else None

	def weakest_dimension(self) -> str:
		"""Which fit score is lowest — the one that kills a deal the mean hides."""
		scored = {f: float(self.get(f)) for f in FIT_FIELDS if self.get(f) not in (None, "")}
		return min(scored, key=scored.get) if scored else ""
