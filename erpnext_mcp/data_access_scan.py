# SPDX-License-Identifier: MIT
"""Which gate each phone route applies today, read from the source. v0.267.0.

Used by the seed generator and by `test_data_access`, which fails when a route's declared gate in the policy and the
gate its code calls disagree — so the policy cannot drift from what the code enforces, and a new route cannot ship
unclassified.
"""

from __future__ import annotations

import inspect
import re

#: Call in the route body → the gate name in the policy.
CALLS = {
	"require_private_hr": "private_hr",
	"require_compliance_role": "compliance",
	"reviews_receipts": "receipt_review",
	"require_dispatch_role": "dispatch",
	"require_location_role": "location",
	"require_ipm_editor": "ipm_edit",
	"may_edit_ipm": "ipm_edit",
}
_CALL = re.compile(r"\b(" + "|".join(CALLS) + r")\(")


def handlers() -> dict:
	"""{method: guarded function} for every phone route (api/mobile.py and api/files.py)."""
	from .api import files, mobile

	out = {}
	for module in (mobile, files):
		for value in vars(module).values():
			method = getattr(value, "farm_ops_method", None)
			if method and callable(value):
				out[method] = value
	return out


def gates_called(function) -> list:
	source = inspect.getsource(inspect.unwrap(function))
	return sorted({CALLS[m.group(1)] for m in _CALL.finditer(source)})


def routes() -> dict:
	"""{method: sorted gate names its body calls} for every guarded phone route."""
	return {method: gates_called(fn) for method, fn in sorted(handlers().items())}


def build_seed(resources: dict, uses: dict) -> dict:
	"""The seed policy: tiers, the gates as the code holds them today, `resources`, and every route with the gates
	its code calls and the resources (`uses`: {method: [{resource, at}]}) its answer carries."""
	from .api import guard

	tiers = [
		{"tier": "worker", "roles": ["Field Worker", "Farm Worker"]},
		{"tier": "crew_lead", "roles": ["Crew Leader"]},
		{"tier": "foreman", "roles": ["Foreman"]},
		{"tier": "manager", "roles": ["Farm Manager", "Compliance Officer"]},
		{"tier": "hr", "roles": ["HR Manager", "HR User"]},
		{"tier": "accounts", "roles": ["Accounts Manager", "Accounts User"]},
	]
	gates = {
		"private_hr": guard.PRIVATE_HR_ROLES,
		"compliance": guard.COMPLIANCE_ROLES,
		"receipt_review": guard.RECEIPT_REVIEW_ROLES,
		"dispatch": guard.DISPATCH_ROLES,
		"location": guard.LOCATION_ROLES,
		"ipm_edit": guard.IPM_EDIT_ROLES,
	}
	routes_out = {}
	for method, called in routes().items():
		entry = {}
		if called:
			entry["gates"] = called
		if uses.get(method):
			entry["resources"] = uses[method]
		routes_out[method] = entry
	return {
		"schema_version": 1,
		"tiers": tiers,
		"gates": {name: {"roles": sorted(roles)} for name, roles in gates.items()},
		"resources": resources,
		"routes": routes_out,
	}
