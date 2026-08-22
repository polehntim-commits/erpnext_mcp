# SPDX-License-Identifier: MIT
"""Controller for MRL Record — one limit, one destination, and where it came from.

THE KEY IS (chemical, crop, market, company) AND A SECOND ROW ON IT IS REFUSED.
Two limits for the same ingredient on the same fruit going to the same place is
not extra information — it is two answers to a question that is asked once, at
the moment somebody decides whether a load can ship, and whichever row the query
happens to return is the one the decision rests on. A market that genuinely
revised its limit supersedes the old row rather than sitting beside it.

THE SOURCE IS REQUIRED AND THE ABSENCE OF ONE IS THE FAILURE THIS PREVENTS. A
load is rejected at a border against a named regulation on a named date, and 'we
had 0.5 written down' is not a defence. A tier-4 inferred limit with an honest
note is worth keeping; a bare number with no provenance is worse than nothing,
because it looks the same as a checked one.

A NEGATIVE LIMIT IS REFUSED AND ZERO IS ALLOWED. Zero is a real value — a
non-detect requirement — and treating it as missing would quietly convert the
strictest limit there is into no limit at all.
"""

import frappe
from frappe import _
from frappe.model.document import Document

BANNED = "Banned"
NOT_REGISTERED = "Not Registered"
REGISTERED = "Registered"
RESTRICTED = "Restricted"
UNKNOWN = "Unknown"
STATUSES = (REGISTERED, BANNED, NOT_REGISTERED, RESTRICTED, UNKNOWN)

HIGH = "High"
MEDIUM = "Medium"
LOW = "Low"
CONFIDENCES = (HIGH, MEDIUM, LOW)

#: Tiers that mean somebody read this off an official register rather than
#: inferring it. Used by the tools to say how far a figure can be leaned on.
OFFICIAL_TIERS = ("1", "2")


class MRLRecord(Document):
	def validate(self):
		self.chemical = str(self.chemical or "").strip()
		self.source = str(self.source or "").strip()

		if not self.chemical:
			frappe.throw(
				_(
					"Chemical is required, and it is the ACTIVE INGREDIENT rather than the trade "
					"name — several products share one active ingredient and the limit attaches "
					"to the ingredient."
				)
			)
		if not self.source:
			frappe.throw(
				_(
					"Source is required. A load is refused at a border against a named regulation "
					"on a named date; a limit with no provenance looks identical to a checked one "
					"and cannot be defended."
				),
				title=_("MRL With No Source"),
			)

		if self.mrl_ppm in (None, ""):
			frappe.throw(_("MRL (ppm) is required."))
		if float(self.mrl_ppm) < 0:
			frappe.throw(
				_("An MRL of {0} is negative. Zero is allowed and means non-detect.").format(self.mrl_ppm)
			)

		if self.confidence not in CONFIDENCES:
			self.confidence = HIGH
		if self.substance_status not in STATUSES:
			self.substance_status = REGISTERED

		self._check_duplicate()
		self._check_dates()

	def _check_duplicate(self) -> None:
		duplicate = frappe.db.get_value(
			"MRL Record",
			{
				"chemical": self.chemical,
				"crop": self.crop,
				"market": self.market,
				"company": self.company or "",
				"name": ("!=", self.name or ""),
			},
			"name",
		)
		if duplicate:
			frappe.throw(
				_(
					"{0} already records {1} on {2} into {3}. Two limits for one question means "
					"whichever row a query happens to return is the one a shipping decision rests "
					"on — correct that record instead."
				).format(duplicate, self.chemical, self.crop, self.market),
				title=_("Duplicate MRL"),
			)

	def _check_dates(self) -> None:
		if self.expiry_date and self.effective_date:
			if str(self.expiry_date) < str(self.effective_date):
				frappe.throw(
					_("This limit is due for re-check ({0}) before it takes effect ({1}).").format(
						self.expiry_date, self.effective_date
					)
				)

	def is_official(self) -> bool:
		"""Whether this figure was read off a register rather than inferred."""
		return str(self.source_tier or "") in OFFICIAL_TIERS
