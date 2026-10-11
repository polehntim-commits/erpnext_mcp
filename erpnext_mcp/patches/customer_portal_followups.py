# SPDX-License-Identifier: MIT
"""v0.313.0. After `rename_packer_share_to_customer_portal` and the model sync:

1. A renamed portal's name shown carries over (`packer_name` → `customer_name`) and its role is Packer.
2. THE TOOL SWITCHES CARRY ACROSS (the lesson `rename_discipline_record` wrote down): each generic tool's
   `allow_<new>` takes the operator's stored `allow_<old>` value where the old one was ON, so a site that had the
   packer portal's writes switched on keeps them on under the new names. A site with them OFF stays OFF. The old
   names stay callable under their own switches either way.

Idempotent and never raises.
"""

import frappe

SWITCHES = (("create_packer_share", "create_customer_portal"), ("issue_packer_credential", "issue_portal_credential"),
            ("revoke_packer_access", "revoke_portal_access"), ("get_packer_share", "get_customer_portal"),
            ("get_packer_pack", "get_customer_portal_pack"), ("list_packer_access_log", "list_portal_access_log"))
SETTINGS = "ERPNext MCP Settings"


def execute():
	_names()
	_switches()


def _names():
	try:
		columns = set(frappe.db.get_table_columns("Customer Portal") or [])
	except Exception:  # pragma: no cover - no table yet
		return
	try:
		if "packer_name" in columns and "customer_name" in columns:
			frappe.db.sql("""UPDATE `tabCustomer Portal` SET customer_name = packer_name
			                 WHERE (customer_name IS NULL OR customer_name = '') AND packer_name IS NOT NULL""")
		if "customer_role" in columns:
			frappe.db.sql("""UPDATE `tabCustomer Portal` SET customer_role = 'Packer'
			                 WHERE customer_role IS NULL OR customer_role = ''""")
		frappe.db.commit()
	except Exception as exc:  # pragma: no cover
		print(f"erpnext_mcp: could not carry the portal names across: {type(exc).__name__}: {exc}")


def _switches():
	try:
		doc = frappe.get_single(SETTINGS)
	except Exception as exc:  # pragma: no cover
		print(f"erpnext_mcp: could not read {SETTINGS}: {exc}")
		return
	changed = []
	for old, new in SWITCHES:
		if int(doc.get(f"allow_{old}") or 0) and not int(doc.get(f"allow_{new}") or 0):
			doc.set(f"allow_{new}", 1)
			changed.append(new)
	if changed:
		doc.flags.ignore_permissions = True
		doc.save()
		frappe.db.commit()
		print(f"erpnext_mcp: switched on {', '.join(changed)} — their packer-named switches were on.")
	else:
		print("erpnext_mcp: customer portal switches — nothing to carry across.")
