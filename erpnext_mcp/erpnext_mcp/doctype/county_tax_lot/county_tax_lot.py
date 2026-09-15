# SPDX-License-Identifier: MIT
"""Controller for County Tax Lot — a read-only reference the Desk cannot edit.

v0.169.0. A tax lot is the county's record, cached. Every field on the form is
read-only, and `validate` refuses any save that does not carry
`flags.county_refresh`, which only `land_adjustment.upsert_tax_lot` sets — the
writer behind `taxlot_lookup` and `taxlot_refresh`. A read-only flag on a field
stops the form; this stops an import, a data patch and a REST call as well.

THE STANDALONE SUITE DOES NOT RUN APP CONTROLLERS, so
`test_land_adjustment.TheCacheIsReadOnly` constructs this class and calls
`validate` itself. That proves the refusal and not that Frappe invokes it.
"""

import frappe
from frappe.model.document import Document


class CountyTaxLot(Document):
	def validate(self):
		if not self.flags.get("county_refresh"):
			frappe.throw(
				"County Tax Lot is a read-only copy of the county's record. It is written by "
				"taxlot_lookup and taxlot_refresh only; change the county's record, then refresh.",
				frappe.PermissionError,
			)
