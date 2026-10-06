# SPDX-License-Identifier: MIT
"""Setting up a dedicated MCP System User — planned and dry-run here, applied by a person. v0.257.0.

docs/security.md's hardening checklist has said "create a dedicated MCP System User … do not leave mutations
running as Administrator" since the first release, and nothing helped anybody do it. Until it is done every MCP
call passes every permission check there is (`settings.effective_user` falls back to Administrator).

THIS MODULE CHANGES NOTHING. It reads the switches that are on, says which ERPNext roles those tools need, checks a
candidate account against them with Frappe's own permission check (a dry run: "with this account, these would be
refused"), and writes out the Desk steps. Creating the user, its Role Profile and setting `mcp_system_user` are a
person's acts in the Desk — the setting that bounds the AI is never set by the AI.

WHERE ROLES MATTER. Most of this app's tools read and write its own registers with permission checks bypassed by
design (the switches are the gate there). The MCP System User's roles bound three things: ERPNext's own documents
written without that bypass (journal entries, invoices, stock entries, employees), the generic readers
(`query_doctype`, `run_report`, attachments), and what an MCP-authored document is attributed to. `AREAS` maps the
tool modules that reach ERPNext's permission-checked doctypes to the standard role that covers them.
"""

from __future__ import annotations

import frappe

from . import settings

#: (area, handler modules, roles, read probes, write probes). Standard ERPNext / HRMS roles only.
AREAS = (
	("Ledger", {"accounts", "mutate", "banking", "banking_bridge", "payroll_gl", "opening", "fiscal", "budget",
	            "anchors", "dimensions", "tax_remittance", "ach", "coop_equity", "expenses", "receipts",
	            "reimbursements", "revenue"},
	 ("Accounts User",), (("Journal Entry", "read"), ("Account", "read"), ("GL Entry", "read")),
	 (("Journal Entry", "create"),)),
	("Purchasing", {"purchasing"}, ("Purchase User",),
	 (("Purchase Invoice", "read"), ("Supplier", "read")), (("Purchase Invoice", "create"),)),
	("Sales", {"sales"}, ("Sales User",), (("Sales Invoice", "read"), ("Customer", "read")),
	 (("Sales Invoice", "create"),)),
	("Stock", {"stock_inventory", "masters", "uoms", "lots"}, ("Stock User",),
	 (("Item", "read"), ("Warehouse", "read"), ("Stock Entry", "read")), (("Stock Entry", "create"),)),
	("Fixed assets", {"assets"}, ("Accounts User",), (("Asset", "read"),), (("Asset", "create"),)),
	("HR and payroll", {"hr", "employee", "payroll", "payroll_deductions", "garnishments", "state_tax", "taxforms",
	                    "newhire", "wagedefaults", "employee_file", "direct_deposit", "payroll_calendar"},
	 ("HR User",), (("Employee", "read"),), (("Employee", "write"),)),
	("Reports and generic reads", {"read", "reports", "files", "workflow", "generic_update", "meta"}, (),
	 (), ()),
)
#: Roles a dedicated MCP user should never hold.
NEVER = ("Administrator", "System Manager")
SUGGESTED_EMAIL_LOCAL = "mcp"


def _enabled_tools() -> list:
	from . import registry

	out = []
	for name, tool in registry.TOOLS.items():
		try:
			if settings.tool_enabled(name):
				out.append((name, tool["handler"].__module__.split(".")[-1], bool(tool.get("mutating"))))
		except Exception:
			continue
	return out


def _areas_in_use(enabled: list) -> list:
	used = []
	for area, modules, roles, reads, writes in AREAS:
		tools = [(name, mutating) for name, module, mutating in enabled if module in modules]
		if not tools:
			continue
		writing = [name for name, mutating in tools if mutating]
		used.append({
			"area": area,
			"roles": list(roles),
			"tools_on": len(tools),
			"write_tools_on": len(writing),
			"examples": sorted(name for name, _ in tools)[:6],
			"probes": list(reads) + (list(writes) if writing else []),
		})
	return used


def _roles_of(user: str) -> list:
	try:
		return sorted(frappe.get_roles(user) or [])
	except Exception:
		return []


def plan(candidate: str = "") -> dict:
	"""The plan, and — with a candidate — the dry run. Writes nothing."""
	current = str(settings._value("mcp_system_user") or "").strip()
	effective = settings.effective_user()
	used = _areas_in_use(_enabled_tools())
	recommended = sorted({role for area in used for role in area["roles"]})
	out = {
		"changed": "nothing — this is a plan and a dry run; a person applies it in the Desk.",
		"current": {
			"mcp_system_user": current or None,
			"runs_as": effective,
			"roles": _roles_of(effective) if effective != "Administrator" else ["Administrator (every permission)"],
			"dedicated": bool(current) and current == effective and effective != "Administrator",
		},
		"areas_in_use": [{k: v for k, v in area.items() if k != "probes"} for area in used],
		"recommended_roles": recommended,
		"never": list(NEVER),
	}
	if candidate:
		out["candidate"] = dry_run(candidate, used, recommended)
	out["steps"] = steps(candidate or f"{SUGGESTED_EMAIL_LOCAL}@<your domain>", recommended)
	return out


def dry_run(candidate: str, used: list, recommended: list) -> dict:
	"""What the enabled tools would meet with `candidate` as the MCP System User."""
	if not frappe.db.exists("User", candidate):
		return {"user": candidate, "exists": False,
		        "note": "No such User yet — create it (steps 1–3), then run this again with it."}
	row = frappe.db.get_value("User", candidate, ["enabled", "user_type"], as_dict=True) or {}
	roles = _roles_of(candidate)
	gaps = []
	for area in used:
		for doctype, ptype in area["probes"]:
			try:
				allowed = frappe.has_permission(doctype, ptype, user=candidate)
			except Exception:
				allowed = False
			if not allowed:
				gaps.append({"area": area["area"], "doctype": doctype, "permission": ptype,
				             "would_refuse": area["examples"][:3], "role_that_covers_it": area["roles"]})
	problems = []
	if not int(row.get("enabled") or 0):
		problems.append("the user is disabled")
	if str(row.get("user_type") or "") != "System User":
		problems.append(f"user type is {row.get('user_type') or 'unset'}; it must be System User")
	dangerous = [role for role in roles if role in NEVER]
	if dangerous:
		problems.append(f"holds {', '.join(dangerous)} — that is the Administrator problem again")
	extra = sorted(set(roles) - set(recommended) - {"All", "Guest", "Desk User"} - set(NEVER))
	return {
		"user": candidate,
		"exists": True,
		"enabled": bool(int(row.get("enabled") or 0)),
		"user_type": row.get("user_type"),
		"roles": roles,
		"missing_roles": sorted(set(recommended) - set(roles)),
		"extra_roles": extra,
		"gaps": gaps,
		"problems": problems,
		"ready": not gaps and not problems,
	}


def steps(user: str, roles: list) -> list:
	role_list = ", ".join(roles) or "(none beyond the defaults — only this app's own tools are on)"
	return [
		f"1. Desk → User → New: Email {user}, First Name 'MCP System', User Type System User. Untick 'Send Welcome "
		"Email'. It needs no password — nobody signs in as it.",
		f"2. Desk → Role Profile → New 'MCP System User' with exactly: {role_list}. Never System Manager.",
		"3. Open the new User → Role Profile: MCP System User → Save. (Remove any other roles it was given.)",
		f"4. Run plan_mcp_system_user with candidate '{user}' again: `ready: true`, no gaps.",
		f"5. ERPNext MCP Settings → MCP System User: {user}; Require User Context ticked. (OAuth clients: MCP OAuth "
		"User empty uses the same account.)",
		"6. get_security_status: the mcp_system_user check passes. MCP Action Log rows and MCP-made documents now "
		"show this user, not Administrator.",
		"Undo: clear MCP System User in ERPNext MCP Settings — calls fall back to Administrator at once.",
		"When a switch is turned on later, run this plan again: a new area may need a new role.",
	]
