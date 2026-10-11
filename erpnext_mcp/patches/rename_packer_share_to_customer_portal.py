# SPDX-License-Identifier: MIT
"""v0.313.0. Rename `Packer Share` → `Customer Portal` and `Packer Share Block` → `Customer Portal Block`, in place —
the packer portal is now the generic customer / first-handler portal (docs/design/customer_portal_plan.md).

Runs BEFORE the model sync, so the sync finds the new names already there. Idempotent and never raises: a site
without the old doctypes (both Tim's servers, which are on v0.269.0) has nothing to rename and says so. The data
follow-ups and the tool switches are `customer_portal_followups`, after the sync.
"""

import frappe

RENAMES = (("Packer Share", "Customer Portal"), ("Packer Share Block", "Customer Portal Block"))


def execute():
	for old, new in RENAMES:
		if not frappe.db.exists("DocType", old):
			print(f"erpnext_mcp: no {old!r} on this site — nothing to rename ({new} arrives with this migrate).")
			continue
		if frappe.db.exists("DocType", new):
			print(f"erpnext_mcp: {new!r} already exists — {old!r} left as it is; move its rows by hand if any.")
			continue
		try:
			frappe.rename_doc("DocType", old, new, force=True)
			frappe.db.commit()
			print(f"erpnext_mcp: renamed DocType {old!r} → {new!r}.")
		except Exception as exc:  # pragma: no cover - reported, never fatal to a migrate
			print(f"erpnext_mcp: could not rename {old!r} to {new!r}: {type(exc).__name__}: {exc}. The rows are "
			      "untouched and readable under the old name; re-run `bench migrate` once the cause is cleared.")
