# SPDX-License-Identifier: MIT
"""W-4 Form controller.

Dependents credits are auto-calculated on every save:
  under_17 × the year's credit + other × $500 = total_dependents_credit.

THE UNDER-17 CREDIT IS $2,200 ON THE 2026 FORM. The 2020-2025 editions of Step 3
said "Multiply the number of qualifying children under age 17 by $2,000"; the
2026 edition says $2,200. This controller computed a flat $2,000 until v0.48.1,
which under-credited every 2026 filing by $200 a child — and `w4_pdf.py` prints
whatever the row holds, so the government form went out with the wrong number in
box 3 as well as the withholding engine reading it out of `withholding.py`.

THE AMOUNT IS KEYED TO THE ROW'S OWN `tax_year`, NOT TO TODAY'S. A W-4 is filed
against an edition of the form, and this app keeps one Active W-4 per employee
per tax year — so a 2025 form that gets re-saved in 2026 (a corrected count, a
superseding edit) must still restate itself at 2025's $2,000. A flat constant
would rewrite history on the next save.
"""

import frappe
from frappe.model.document import Document

#: (first tax year the amount applied, credit), newest first. A year later than
#: every entry takes the newest, so a 2027 form needs no edit here unless the IRS
#: moves the number again.
UNDER_17_CREDIT = ((2026, 2200), (0, 2000))

#: Unchanged since the 2020 redesign — the 2026 form still says "$500".
OTHER_DEPENDENT_CREDIT = ((0, 500),)


def credit_for(schedule, tax_year) -> int:
	"""The per-dependent credit a form of `tax_year` claims.

	A missing year takes the NEWEST amount rather than the oldest: `tax_year` is
	mandatory, so the only row that reaches here without one is being created
	right now, and today's edition is the honest guess for it.
	"""
	year = int(tax_year or 0)
	if not year:
		return schedule[0][1]
	for first_year, amount in schedule:
		if year >= first_year:
			return amount
	return schedule[-1][1]


#: v0.308.0. The elections each signature attests to. Once that box is signed they are locked — the signature is the
#: final entry (26 CFR 31.3402(f)(5)-1(c)); a change is a new form, signed again.
FEDERAL_ENTRIES = ("employee", "tax_year", "filing_status", "multiple_jobs", "additional_income_from_other_jobs",
                   "dependents_under_17_count", "other_dependents_count", "other_income", "deductions",
                   "extra_withholding_per_period")
OREGON_ENTRIES = ("or_filing_status", "or_allowances", "or_additional_withholding", "or_exempt",
                  "or_federally_exempt", "or_exemption_code", "or_redetermination")


class W4Form(Document):
	def validate(self):
		self._compute_dependents()
		self._signature_is_the_final_entry()

	def _signature_is_the_final_entry(self):
		from erpnext_mcp import compat

		before = self.get_doc_before_save() if hasattr(self, "get_doc_before_save") else None
		if not before:
			return
		try:
			from erpnext_mcp.tools.w4 import esignature_rule

			if not esignature_rule().get("lock_after_signature", True):
				return
		except Exception:  # pragma: no cover - the lock holds when the policy cannot be read
			pass
		for box, entries, form in (("signature", FEDERAL_ENTRIES, "Form W-4"),
		                           ("or_signature", OREGON_ENTRIES, "Form OR-W-4")):
			if not before.get(box):
				continue
			changed = [f for f in entries if compat.has_field("W-4 Form", f) and _norm(before.get(f)) != _norm(self.get(f))]
			if changed:
				frappe.throw(
					f"{self.name}'s {form} is signed, and the signature is the final entry (26 CFR "
					f"31.3402(f)(5)-1(c)) — {', '.join(changed)} cannot change after it. File a new {form} and have "
					f"it signed.",
					title="Signed W-4 Is Locked")

	def _compute_dependents(self):
		under_17 = int(self.dependents_under_17_count or 0)
		other = int(self.other_dependents_count or 0)
		self.dependents_under_17_amount = under_17 * credit_for(UNDER_17_CREDIT, self.tax_year)
		self.other_dependents_amount = other * credit_for(OTHER_DEPENDENT_CREDIT, self.tax_year)
		self.total_dependents_credit = self.dependents_under_17_amount + self.other_dependents_amount


def _norm(value):
	"""Blank, None and 0 read the same; numbers compare as numbers."""
	if value in (None, ""):
		return ""
	try:
		return float(value)
	except (TypeError, ValueError):
		return str(value)
