# SPDX-License-Identifier: MIT
"""Show the pesticide label fields on a Pest Control item too.

v0.201.0. A rodenticide is an EPA-registered pesticide, and Tim's correction is
that it is not a crop-protection product: it belongs under "Pest Control
Products". The Desk shows the label fields on an Item whose GROUP NAME matches
`compliance_fields.CHEMICAL_ITEM_DEPENDS_ON`, and that expression did not know
"pest control" or "rodent" — so a mouse bait filed where it belongs would have
hidden its own EPA number, signal word and PPE from whoever opened it.

`compliance_fields` only ever CREATES a field; one that exists is left as it is,
which is the right rule for a column and the wrong one for a display rule this
app wrote. So this moves the rule on the fields that still carry EXACTLY the
old expression, and on no others: a rule somebody edited in the Desk is theirs.
Idempotent — the second run finds nothing carrying the old expression.
"""

from __future__ import annotations

import frappe

from erpnext_mcp import compat, compliance_fields

CUSTOM_FIELD = "Custom Field"


def execute() -> None:
	report = widen_pesticide_field_visibility()
	print(f"erpnext_mcp: pesticide field visibility widened on {report['updated']} Item field(s)")


def widen_pesticide_field_visibility() -> dict:
	report = {"updated": 0, "skipped": ""}
	if not compat.doctype_exists(CUSTOM_FIELD):
		report["skipped"] = "no Custom Field doctype"
		return report
	rows = frappe.db.get_all(
		CUSTOM_FIELD,
		filters={"dt": "Item", "depends_on": compliance_fields.PREVIOUS_CHEMICAL_ITEM_DEPENDS_ON},
		fields=["name"],
		limit=100,
	)
	for row in rows:
		frappe.db.set_value(
			CUSTOM_FIELD,
			row["name"],
			"depends_on",
			compliance_fields.CHEMICAL_ITEM_DEPENDS_ON,
			update_modified=False,
		)
		report["updated"] += 1
	if report["updated"]:
		frappe.clear_cache(doctype="Item")
	return report
