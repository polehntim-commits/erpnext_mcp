# SPDX-License-Identifier: MIT
"""Controller for Market Participant — somebody else in this market.

WHAT IS REFUSED HERE IS ONLY THE FIGURE THAT CANNOT BE TRUE. A market share over
100%, a negative acreage, a negative headcount. Nothing else, and that restraint
is deliberate: almost every column on this doctype is somebody's read of a
private business, and a controller that demanded internal consistency from an
estimate would refuse the honest record — the one where the revenue guess and the
acreage guess do not quite square, which is what a guess looks like.

THE NAME IS NOT UNIQUE AND THAT IS ON PURPOSE. Two entities on one bench may each
keep their own read of the same competitor, and merging them would mean one of
them lost their assessment. Uniqueness is per company only, so a single
operation cannot open the same competitor twice by accident.
"""

import frappe
from frappe import _
from frappe.model.document import Document

COMPETITOR = "Competitor"
SUPPLIER = "Supplier"
CUSTOMER = "Customer"
PARTNER = "Partner"
TARGET = "Target"
TYPES = (COMPETITOR, SUPPLIER, CUSTOMER, PARTNER, TARGET)


class MarketParticipant(Document):
	def validate(self):
		self.participant_name = str(self.participant_name or "").strip()
		if not self.participant_name:
			frappe.throw(_("Participant Name is required."))

		if self.participant_type not in TYPES:
			frappe.throw(_("Participant Type must be one of: {0}.").format(", ".join(TYPES)))

		duplicate = frappe.db.get_value(
			"Market Participant",
			{
				"participant_name": self.participant_name,
				"company": self.company,
				"name": ("!=", self.name or ""),
			},
			"name",
		)
		if duplicate:
			frappe.throw(
				_(
					"{0} already keeps a record for {1}. Two records for one organisation drift "
					"apart, and the assessment on the older one is the one that gets read."
				).format(duplicate, self.participant_name),
				title=_("Duplicate Participant"),
			)

		if self.market_share_pct not in (None, ""):
			share = float(self.market_share_pct)
			if not 0 <= share <= 100:
				frappe.throw(_("Market Share is {0}%. A share of a market runs from 0 to 100.").format(share))
		if float(self.estimated_acreage or 0) < 0:
			frappe.throw(_("Estimated Acreage cannot be negative."))
		if int(self.employee_count or 0) < 0:
			frappe.throw(_("Employee Count cannot be negative."))
