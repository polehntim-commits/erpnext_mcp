# SPDX-License-Identifier: MIT
"""Who sees what on the phone, as data. v0.267.0 (docs/contracts/data_access_v0_267.yaml; Tim, 2026-10-06).

ONE POLICY, one Farm Config Version of kind "Data Access" (key `farm`), the same lifecycle as every other config:
a Draft, a preview, publish by a person. Until one is published the seed below is the policy, and the seed is TODAY'S
BEHAVIOUR — publishing it unchanged changes nothing a phone receives.

It holds four things:
  tiers      role → tier (worker, crew_lead, foreman, manager, hr, accounts); System Manager is every tier.
  gates      the role sets the phone's gates check (private_hr, compliance, receipt_review, dispatch, location,
             ipm_edit). `guard.require_*` reads them here. A gate may only NARROW within its CEILING: the private HR
             reads can never be widened back to Farm Manager (v0.260.0), receipts never beyond their reviewers.
  resources  the personal things answers carry (an I-9, a pay line, an accident report, a receipt, …): which fields
             are restricted and which tiers may see each, whose row it is (`owner_key`: your own row shows whole),
             and the row scope per tier (all | own | department | none).
  routes     route → the resources in its answer and where (`at`: "rows", "occupants", "" for the answer itself)
             and the gate it applies. Every phone route is listed; one carrying nothing personal says so.

ENFORCED IN ONE PLACE: `guard.endpoint` passes every answer through `apply` on the way out, after `strip_secrets`.
The filter can only REMOVE — a field or a row — never add, so a policy can tighten what the code returns and can
never loosen it: the route's own gates still run first.
"""

from __future__ import annotations

import copy
import json
import pathlib

import frappe

KIND = "Data Access"
KEY = "farm"
TIERS = ("worker", "crew_lead", "foreman", "manager", "hr", "accounts")
ADMIN_ROLE = "System Manager"
SCOPES = ("all", "own", "department", "none")
SEED_FILE = pathlib.Path(__file__).resolve().parent / "seed_data" / "data_access_policy.json"

#: What each gate may at most hold. A published policy may narrow a gate; widening past this is refused at
#: validation — the private HR reads stay off Farm Manager (Tim, v0.260.0), everybody's receipts stay with
#: the reviewers. The other gates may grow to any enrolled role.
_ENROLLED = frozenset({"Field Worker", "Farm Worker", "Foreman", "Crew Leader", "Farm Manager", "HR Manager", "HR User",
                       "Compliance Officer", "Accounts Manager", "Accounts User", ADMIN_ROLE})
CEILINGS = {
	"private_hr": frozenset({"HR Manager", "HR User", ADMIN_ROLE}),
	"receipt_review": frozenset({"Farm Manager", "Accounts Manager", "Accounts User", ADMIN_ROLE}),
	"compliance": _ENROLLED,
	"dispatch": _ENROLLED,
	"location": _ENROLLED,
	"ipm_edit": _ENROLLED,
}


# ── the policy ───────────────────────────────────────────────────────────────
def seed() -> dict:
	return json.loads(SEED_FILE.read_text())


def policy() -> dict:
	"""The published policy, else the seed. Cached per request; a broken published body falls back to the seed
	(and validation never lets one publish)."""
	cached = getattr(frappe.local, "farm_data_access", None) if hasattr(frappe, "local") else None
	if cached is not None:
		return cached
	body = None
	try:
		from . import phone_config

		doc = phone_config.doc_of(KIND, KEY, status=phone_config.PUBLISHED)
		if doc is not None:
			body = phone_config.body_of(doc)
			if validate(body, key=KEY)["errors"]:
				body = None
	except Exception:
		body = None
	result = body or seed()
	try:
		frappe.local.farm_data_access = result
	except Exception:
		pass
	return result


def forget() -> None:
	try:
		frappe.local.farm_data_access = None
	except Exception:
		pass


def gate_roles(gate: str, default) -> frozenset:
	"""The roles a gate admits: the policy's, never wider than its ceiling; else the code's default."""
	entry = (policy().get("gates") or {}).get(gate)
	if not isinstance(entry, dict) or not isinstance(entry.get("roles"), list):
		return frozenset(default)
	return frozenset(entry["roles"]) & CEILINGS.get(gate, frozenset(default))


def tiers_of(roles) -> set:
	held = set(roles or ())
	if ADMIN_ROLE in held:
		return set(TIERS)
	out = set()
	for row in policy().get("tiers") or []:
		if held & set(row.get("roles") or []):
			out.add(row["tier"])
	return out


# ── the filter ───────────────────────────────────────────────────────────────
def _caller(user: str) -> dict:
	row = {}
	try:
		row = frappe.db.get_value("Employee", {"user_id": user}, ["name", "department"], as_dict=True) or {}
	except Exception:
		row = {}
	return {"employee": row.get("name"), "department": row.get("department"), "user": user}


def _scope(resource: dict, tiers: set) -> str:
	scopes = resource.get("scope") or {}
	if not scopes:
		return "all"
	order = {"all": 3, "department": 2, "own": 1, "none": 0}
	best = "none"
	for tier in tiers:
		s = scopes.get(tier, "all")
		if order.get(s, 0) > order[best]:
			best = s
	return best


def _own(row: dict, resource: dict, caller: dict) -> bool:
	key = resource.get("owner_key")
	if not key or key not in row:
		return False
	value = row.get(key)
	return bool(value) and value in (caller.get("employee"), caller.get("user"))


def _in_scope(row: dict, resource: dict, scope: str, caller: dict) -> bool:
	if scope == "all":
		return True
	if scope == "none":
		return False
	if _own(row, resource, caller):
		return True
	if scope == "department":
		key = resource.get("department_key") or "department"
		return bool(row.get(key)) and row.get(key) == caller.get("department")
	return False


def _shape(row, resource: dict, tiers: set, caller: dict, removed: list):
	if not isinstance(row, dict) or _own(row, resource, caller):
		return row
	fields = resource.get("fields") or {}
	nulls = resource.get("hide") == "null"
	out = {}
	for key, value in row.items():
		allowed = fields.get(key)
		if allowed is not None and not (tiers & set(allowed)):
			removed.append(key)
			if nulls:
				out[key] = None
			continue
		out[key] = value
	return out


def _walk(value, parts: list, resource: dict, tiers: set, scope: str, caller: dict, removed: list, is_rows: bool):
	if not parts:
		if isinstance(value, list):
			kept = []
			for row in value:
				if isinstance(row, dict) and not _in_scope(row, resource, scope, caller):
					removed.append("<row>")
					continue
				kept.append(_shape(row, resource, tiers, caller, removed))
			return kept
		if isinstance(value, dict):
			if is_rows and not _in_scope(value, resource, scope, caller):
				removed.append("<row>")
				return None
			return _shape(value, resource, tiers, caller, removed)
		return value
	head, rest = parts[0], parts[1:]
	if isinstance(value, list):
		return [_walk(item, parts, resource, tiers, scope, caller, removed, is_rows) for item in value]
	if isinstance(value, dict) and head in value:
		out = dict(value)
		out[head] = _walk(value[head], rest, resource, tiers, scope, caller, removed, True)
		return out
	return value


def apply(method: str, user: str, answer, roles=None):
	"""The answer as this caller's tiers may see it. Unlisted routes and answers that are not JSON shapes pass."""
	pol = policy()
	route = (pol.get("routes") or {}).get(method) or {}
	uses = route.get("resources") or []
	if not uses or not isinstance(answer, (dict, list)):
		return answer
	if roles is None:
		from .api import guard

		roles = guard.roles_held(user)
	tiers = tiers_of(roles)
	if ADMIN_ROLE in set(roles or ()):
		return answer
	caller = _caller(user)
	removed: list = []
	result = answer
	for use in uses:
		resource = (pol.get("resources") or {}).get(use.get("resource")) or {}
		if not resource:
			continue
		at = str(use.get("at") or "")
		parts = [p for p in at.split(".") if p]
		result = _walk(result, parts, resource, tiers, _scope(resource, tiers), caller, removed, bool(parts))
	return result


# ── validation ───────────────────────────────────────────────────────────────
def validate(body: dict, *, key: str = "", for_publish: bool = False) -> dict:
	errors: list = []
	warnings: list = []
	if not isinstance(body, dict):
		return {"errors": ["the policy is a JSON object"], "warnings": []}
	if key and key != KEY:
		errors.append(f"the data access policy has one key, {KEY!r}")
	tiers = body.get("tiers")
	if not isinstance(tiers, list) or not tiers:
		errors.append("tiers: a list of {tier, roles}")
		tiers = []
	names = [str((t or {}).get("tier")) for t in tiers]
	for name in names:
		if name not in TIERS:
			errors.append(f"tiers: {name!r} is not one of {', '.join(TIERS)}")
	for gate, entry in (body.get("gates") or {}).items():
		if gate not in CEILINGS:
			errors.append(f"gates: {gate!r} is not a gate this app checks ({', '.join(sorted(CEILINGS))})")
			continue
		roles = set((entry or {}).get("roles") or [])
		wider = sorted(roles - CEILINGS[gate])
		if wider:
			errors.append(f"gates.{gate}: may not admit {', '.join(wider)} — a gate may only narrow"
			              + (" (the private HR reads stay off Farm Manager)" if gate == "private_hr" else ""))
		if not roles:
			warnings.append(f"gates.{gate}: admits nobody")
	resources = body.get("resources") or {}
	for name, res in resources.items():
		for field, allowed in ((res or {}).get("fields") or {}).items():
			bad = [t for t in (allowed or []) if t not in TIERS]
			if bad or not isinstance(allowed, list):
				errors.append(f"resources.{name}.fields.{field}: tiers must be from {', '.join(TIERS)}")
		for tier, scope in ((res or {}).get("scope") or {}).items():
			if tier not in TIERS or scope not in SCOPES:
				errors.append(f"resources.{name}.scope.{tier}: {scope!r} (scopes: {', '.join(SCOPES)})")
		if (res or {}).get("hide", "remove") not in ("remove", "null"):
			errors.append(f"resources.{name}.hide: remove (default) or null")
		if (res or {}).get("scope") and not (res or {}).get("owner_key"):
			errors.append(f"resources.{name}: a row scope needs owner_key")
	for method, route in (body.get("routes") or {}).items():
		for use in (route or {}).get("resources") or []:
			if use.get("resource") not in resources:
				errors.append(f"routes.{method}: unknown resource {use.get('resource')!r}")
		for gate in (route or {}).get("gates") or []:
			if gate not in CEILINGS:
				errors.append(f"routes.{method}: unknown gate {gate!r}")
	return {"errors": errors, "warnings": warnings}


# ── what a tier would see (preview; read-only) ───────────────────────────────
def describe(tier: str = "", route: str = "", body: dict | None = None) -> dict:
	pol = copy.deepcopy(body or policy())
	out = {"tiers": pol.get("tiers"), "gates": pol.get("gates")}
	if route:
		r = (pol.get("routes") or {}).get(route)
		if r is None:
			return {**out, "route": route, "listed": False}
		uses = []
		for use in r.get("resources") or []:
			res = (pol.get("resources") or {}).get(use.get("resource")) or {}
			hidden = sorted(f for f, allowed in (res.get("fields") or {}).items() if tier and tier not in allowed)
			uses.append({**use, "hidden_from_tier": hidden if tier else None,
			             "scope": (res.get("scope") or {}).get(tier, "all") if tier else res.get("scope")})
		return {**out, "route": route, "listed": True, "gates": r.get("gates") or [], "resources": uses}
	return {**out, "resources": pol.get("resources"), "routes": len(pol.get("routes") or {})}
