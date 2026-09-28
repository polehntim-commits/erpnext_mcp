# SPDX-License-Identifier: MIT
"""The unit register: ERPNext's UOM master, its global conversion factors, and
this app's agricultural unit contexts — readable and, switch by switch, writable.

v0.197.0. OML App Feedback AFB-2026-00023. A bait product counted in blocks
could not be registered because nothing on the site called a unit "Block", and
nothing this app exposed could add one: `list_ag_uom_contexts` and
`get_uom_conversions` only read, and `update_document` cannot create. Every fix
went through the Desk. Now the whole register is here — list, read, resolve,
create, update, disable, the factors between units, and which units each kind
of work accepts.

THE WRITES ARE GATED TWICE, AS EVERY WRITE IN THIS APP IS. Each is its own
switch on ERPNext MCP Settings, off out of the box, and each also needs the
account this app acts as to hold one of `UOM_ROLES`. A unit is shared by every
Item, every price and every stock row on the site, so the gate is the people
who own the item master, not everybody who can file a receipt.

NOTHING IS RENAMED AND NOTHING THAT IS IN USE IS DELETED. A UOM is named after
itself and is the value on every row that counts in it; renaming one rewrites
history. So a wrong unit is disabled and a right one created. The one delete
here is a conversion FACTOR, which is a number between two units rather than a
thing anything else points at — and it has its own switch.

`ag_uom`'s rule still holds: a CONTEXT measures one thing. A bait block is
counted and a dry product is weighed, and the context controller refuses a list
mixing the two. `add_uom_to_context` asks the same question first so the
refusal says which unit measures what.
"""

from __future__ import annotations

import frappe

from .. import ag_uom, compat, roles, security, uom_resolve
from ..args import as_bool, as_choice, as_float, as_limit, as_str
from ..errors import ToolError
from ..result import ToolResult

UOM = "UOM"
UOM_CATEGORY = "UOM Category"
CONVERSION_FACTOR = "UOM Conversion Factor"
UOM_CONTEXT = "Agricultural UOM Context"
CONTEXT_ENTRY = "Agricultural UOM Context Entry"
AG_CONVERSION = "Agricultural UOM Conversion"
ITEM = "Item"
RATE_UOM_FIELD = "application_rate_uom"
CUSTOM_FIELD = "Custom Field"
ALIAS_FIELD = uom_resolve.ALIAS_FIELD

#: Who may change the unit register. The item-master owners ERPNext itself
#: names (Stock Manager, Item Manager), the site administrator, and the farm's
#: manager, who is the person in the building when a new product arrives.
UOM_ROLES = ("System Manager", "Stock Manager", "Item Manager", "Farm Manager")

_ERPNEXT_HINT = "It ships with ERPNext's Stock module."
_APP_HINT = "It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app."


# ── gates and lookups ───────────────────────────────────────────────────────
def require_uom_role(action: str, tail: str) -> str:
	"""The principal, once it holds one of `UOM_ROLES`. Same shape as `kpi.require_kpi_role`."""
	actor = security.caller_identity() or str(getattr(frappe.session, "user", "") or "")
	if not actor or actor == "Guest":
		raise ToolError(f"this call has no identity to {action} as. {tail}")
	held = set(frappe.get_roles(actor) or []) or set(roles.all_roles_of(actor) or [])
	if not held & set(UOM_ROLES):
		raise ToolError(
			f"{actor} may not {action}: it holds none of {', '.join(UOM_ROLES)}. A unit is shared by "
			"every Item, price and stock row on the site, so changing the register is the item "
			"master's owners' call. Grant one of those roles in the Desk to the account this app "
			f"acts as (`mcp_system_user` on ERPNext MCP Settings). {tail}"
		)
	return actor


def _require_uom() -> None:
	compat.require_doctype(UOM, _ERPNEXT_HINT)


def _uom_row(name: str) -> dict | None:
	fields = compat.existing_fields(UOM, ("name", "enabled", "must_be_whole_number"))
	row = frappe.db.get_value(UOM, name, fields, as_dict=True)
	return dict(row) if row else None


def _existing_uom(value, label: str, tail: str) -> str:
	"""A UOM docname — exact, or the one this text resolves to — or a refusal with candidates.

	Disabled units count as existing here: disabling a unit is not deleting it,
	and a person managing the register must still be able to name it.
	"""
	name = str(value or "").strip()
	if not name:
		raise ToolError(f"{label} is required. {tail}")
	if frappe.db.exists(UOM, name):
		return name
	every = frappe.db.get_all(UOM, pluck="name", limit=5000)
	for other in every:
		if other.lower() == name.lower():
			return other
	answer = uom_resolve.resolve_unit(name)
	if answer["uom"]:
		return answer["uom"]
	raise ToolError(
		f"no UOM called {name!r} on this site. Closest: {', '.join(answer['candidates']) or '<none>'}. "
		f"list_uoms has the register; create_uom adds a missing unit. {tail}"
	)


def _checked(value) -> bool:
	return compat.checked(value)


def ensure_uom_alias_field() -> bool:
	"""Give UOM the column a site keeps its own spellings of a unit in. v0.202.0.

	Tim, on PROWLER® Place Pacs: "pacs" did not resolve, and the fix should not
	need a deploy. One Small Text on the unit — one spelling per line — read by
	every resolution (`uom_resolve._index`) and written by `set_uom_aliases`.

	Same arrangement as `masters.ensure_sales_channel_field`: created at migrate
	time by `install.py` and here on first use. NEVER RAISES — a site that will
	not take the field resolves with the built-in table alone.
	"""
	try:
		if compat.has_field(UOM, ALIAS_FIELD):
			return True
		if not compat.doctype_exists(CUSTOM_FIELD) or not compat.doctype_exists(UOM):
			return False
		if frappe.db.exists(CUSTOM_FIELD, {"dt": UOM, "fieldname": ALIAS_FIELD}):
			return True
	except Exception:
		return False
	try:
		doc = frappe.new_doc(CUSTOM_FIELD)
		doc.dt = UOM
		doc.fieldname = ALIAS_FIELD
		doc.label = "Also Written As"
		doc.fieldtype = "Small Text"
		doc.insert_after = "must_be_whole_number"
		doc.module = "ERPNext MCP"
		doc.description = (
			"Other spellings of this unit on labels, receipts and the phone, one per line — "
			"'pacs' for Place Pac. A plural of a line is read too. Managed by set_uom_aliases, "
			"which refuses a spelling another unit already answers to."
		)
		doc.insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(
			title=f"erpnext_mcp: could not add {ALIAS_FIELD} to UOM", message=compat.traceback_text()
		)
		return False
	try:
		frappe.clear_cache(doctype=UOM)
	except Exception:
		pass
	return compat.has_field(UOM, ALIAS_FIELD)


def _aliases_by_uom() -> dict:
	"""`{uom: [spelling, ...]}` for every unit with any, or {} before the field exists."""
	if not compat.has_field(UOM, ALIAS_FIELD):
		return {}
	out = {}
	for row in frappe.db.get_all(UOM, fields=["name", ALIAS_FIELD], limit=5000):
		spellings = uom_resolve.parse_aliases(row.get(ALIAS_FIELD))
		if spellings:
			out[row["name"]] = spellings
	return out


def _contexts_by_uom() -> dict:
	"""`{uom: [{context, is_default, is_active}]}` across every context, in one query each."""
	if not compat.doctype_exists(UOM_CONTEXT):
		return {}
	active = {
		row["name"]: _checked(row.get("is_active"))
		for row in frappe.db.get_all(UOM_CONTEXT, fields=["name", "is_active"], limit=500)
	}
	out: dict = {}
	if not active:
		return out
	for entry in frappe.db.get_all(
		CONTEXT_ENTRY,
		filters={"parenttype": UOM_CONTEXT, "parent": ("in", list(active))},
		fields=["parent", "uom", "is_default"],
		limit=5000,
	):
		out.setdefault(entry["uom"], []).append(
			{
				"context": entry["parent"],
				"is_default": _checked(entry.get("is_default")),
				"is_active": active.get(entry["parent"], False),
			}
		)
	return out


def _count(doctype: str, filters: dict) -> int | None:
	try:
		return int(frappe.db.count(doctype, filters))
	except Exception:  # pragma: no cover - a site whose Item lacks the column
		return None


# ── 1. list_uoms ────────────────────────────────────────────────────────────
def list_uoms(args: dict) -> ToolResult:
	"""The unit register, with what each unit measures and which contexts list it."""
	_require_uom()
	filters: dict = {}
	if "enabled" in args:
		filters["enabled"] = 1 if as_bool(args, "enabled") else 0
	if "must_be_whole_number" in args:
		filters["must_be_whole_number"] = 1 if as_bool(args, "must_be_whole_number") else 0
	search = as_str(args, "search")
	if search:
		filters["name"] = ("like", f"%{search}%")
	limit = as_limit(args)

	contexts = _contexts_by_uom()
	context = as_str(args, "context")
	only = None
	if context:
		compat.require_doctype(UOM_CONTEXT, _APP_HINT)
		if not frappe.db.exists(UOM_CONTEXT, context):
			known = frappe.db.get_all(UOM_CONTEXT, pluck="name", limit=50)
			raise ToolError(
				f"no unit context called {context!r}. Known: {', '.join(sorted(known)) or '<none>'}."
			)
		only = {uom for uom, rows in contexts.items() if any(r["context"] == context for r in rows)}

	fields = compat.existing_fields(UOM, ("name", "enabled", "must_be_whole_number"))
	rows = frappe.db.get_all(UOM, filters=filters, fields=fields, order_by="name asc", limit=5000)
	if only is not None:
		rows = [row for row in rows if row["name"] in only]
	truncated = len(rows) > limit
	aliases = _aliases_by_uom()
	uoms = [
		{
			"name": row["name"],
			"enabled": _checked(row.get("enabled", 1)),
			"must_be_whole_number": _checked(row.get("must_be_whole_number")),
			"measures": ag_uom.dimension_of(row["name"]) or None,
			"contexts": [entry["context"] for entry in contexts.get(row["name"], [])],
			"aliases": aliases.get(row["name"], []),
		}
		for row in rows[:limit]
	]
	return ToolResult(
		data={"count": len(uoms), "truncated": truncated, "uoms": uoms},
		summary=f"{len(uoms)} unit(s)" + (" (truncated)" if truncated else ""),
	)


# ── 2. get_uom ──────────────────────────────────────────────────────────────
def get_uom(args: dict) -> ToolResult:
	"""One unit: its flags, its contexts, every factor touching it, and what uses it."""
	_require_uom()
	name = _existing_uom(as_str(args, "uom") or as_str(args, "name"), "uom", "")
	row = _uom_row(name) or {"name": name}

	factors = []
	if compat.doctype_exists(CONVERSION_FACTOR):
		fields = compat.existing_fields(
			CONVERSION_FACTOR, ("name", "category", "from_uom", "to_uom", "value")
		)
		for side in ("from_uom", "to_uom"):
			for factor in frappe.db.get_all(
				CONVERSION_FACTOR, filters={side: name}, fields=fields, limit=500
			):
				factors.append(
					{
						"from_uom": factor["from_uom"],
						"to_uom": factor["to_uom"],
						"value": float(factor.get("value") or 0),
						"category": factor.get("category") or None,
					}
				)

	ag_rows = []
	if compat.doctype_exists(AG_CONVERSION):
		for side in ("from_uom", "to_uom"):
			for conv in frappe.db.get_all(
				AG_CONVERSION,
				filters={side: name},
				fields=["name", "from_uom", "to_uom", "crop", "factor", "basis", "is_active"],
				limit=500,
			):
				ag_rows.append(
					{
						"name": conv["name"],
						"from_uom": conv["from_uom"],
						"to_uom": conv["to_uom"],
						"crop": conv.get("crop") or None,
						"factor": float(conv.get("factor") or 0),
						"basis": conv.get("basis"),
						"is_active": _checked(conv.get("is_active")),
					}
				)

	usage = {"items_stocked_in": _count(ITEM, {"stock_uom": name}) if compat.doctype_exists(ITEM) else None}
	if compat.doctype_exists(ITEM) and compat.has_field(ITEM, RATE_UOM_FIELD):
		usage["items_rated_in"] = _count(ITEM, {RATE_UOM_FIELD: name})

	data = {
		"name": name,
		"enabled": _checked(row.get("enabled", 1)),
		"must_be_whole_number": _checked(row.get("must_be_whole_number")),
		"measures": ag_uom.dimension_of(name) or None,
		"contexts": _contexts_by_uom().get(name, []),
		"aliases": _aliases_by_uom().get(name, []),
		"conversion_factors": factors,
		"agricultural_conversions": ag_rows,
		"usage": usage,
	}
	return ToolResult(data, f"UOM {name}")


# ── 3. resolve_uom ──────────────────────────────────────────────────────────
def resolve_uom(args: dict) -> ToolResult:
	"""Which site unit some printed words mean. Never raises on an unknown unit — it says so."""
	_require_uom()
	text = as_str(args, "text", required=True)
	kind = (as_str(args, "kind") or "").lower() or ("rate" if any(ch.isdigit() for ch in text) else "unit")
	if kind not in ("rate", "unit"):
		raise ToolError(f"kind must be 'rate' or 'unit', got {kind!r}.")
	answer = uom_resolve.resolve_rate(text) if kind == "rate" else uom_resolve.resolve_unit(text)
	if answer["uom"]:
		summary = f"{answer['phrase']!r} is {answer['uom']} ({answer['matched_by']})"
	elif answer["status"] == "unresolved":
		summary = f"{answer['phrase']!r} matches no unit on this site"
	else:
		summary = "no unit in that text"
	return ToolResult(answer, summary)


# ── 4. create_uom ───────────────────────────────────────────────────────────
def create_uom(args: dict) -> ToolResult:
	"""Add a unit to ERPNext's register. Refuses a name the register already answers to."""
	_require_uom()
	tail = "Nothing was created."
	require_uom_role("add a unit of measure", tail)
	name = " ".join(as_str(args, "uom_name", required=True).split())
	whole = bool(as_bool(args, "must_be_whole_number", False))
	enabled = as_bool(args, "enabled", True)

	every = frappe.db.get_all(UOM, pluck="name", limit=5000)
	for other in every:
		if other.lower() == name.lower():
			raise ToolError(
				f"a UOM called {other!r} already exists"
				+ ("" if other == name else f" (the register ignores case, so {name!r} is the same unit)")
				+ f". update_uom changes its flags; enabled=true brings back a disabled one. {tail}"
			)
	# A name that the resolver already sends to another unit would be a second
	# spelling of that unit — "Blocks" beside "Block" — and every label read
	# afterwards would pick whichever the resolver tried first.
	answer = uom_resolve.resolve_unit(name, [{"name": n, "must_be_whole_number": False} for n in every])
	if answer["uom"] and answer["matched_by"] in ("alias", "plural"):
		raise ToolError(
			f"{name!r} already means {answer['uom']} on this site — the resolver reads it that way "
			f"({answer['matched_by']}). Use {answer['uom']}, or disable it first if it is genuinely "
			f"the wrong unit. {tail}"
		)

	doc = frappe.new_doc(UOM)
	doc.uom_name = name
	doc.must_be_whole_number = 1 if whole else 0
	doc.enabled = 1 if enabled else 0
	doc.insert()
	data = {
		"name": doc.name,
		"must_be_whole_number": whole,
		"enabled": bool(enabled),
		"measures": ag_uom.dimension_of(doc.name) or None,
		"note": (
			"Add it to the context it belongs in (add_uom_to_context) so forms that offer units "
			"for that work list it, and record any factor to an existing unit with "
			"set_uom_conversion_factor."
		),
	}
	return ToolResult(
		data,
		f"created UOM {doc.name}" + (" (whole numbers)" if whole else ""),
		docstatus_delta="none → created",
	)


# ── 5. update_uom ───────────────────────────────────────────────────────────
def update_uom(args: dict) -> ToolResult:
	"""Change a unit's whole-number or enabled flag. Never renames it."""
	_require_uom()
	tail = "Nothing was changed."
	require_uom_role("change a unit of measure", tail)
	name = _existing_uom(as_str(args, "uom"), "uom", tail)
	if as_str(args, "uom_name") and as_str(args, "uom_name") != name:
		raise ToolError(
			f"a UOM is named after itself, and {name!r} is the value on every row that counts in it — "
			"renaming it would rewrite that history. Disable it (disable_uom) and create the right "
			f"one (create_uom). {tail}"
		)
	whole = as_bool(args, "must_be_whole_number")
	enabled = as_bool(args, "enabled")
	if whole is None and enabled is None:
		raise ToolError(f"nothing to change. Pass must_be_whole_number or enabled. {tail}")

	doc = frappe.get_doc(UOM, name)
	changes: dict = {}
	for key, wanted in (("must_be_whole_number", whole), ("enabled", enabled)):
		if wanted is None:
			continue
		current = _checked(doc.get(key))
		if current != wanted:
			changes[key] = [current, wanted]
			doc.set(key, 1 if wanted else 0)
	if not changes:
		return ToolResult({"name": name, "changed": {}}, f"UOM {name} already matched; nothing changed")
	doc.save()
	return ToolResult(
		{"name": name, "changed": changes},
		f"updated UOM {name}: {', '.join(sorted(changes))}",
		docstatus_delta="unchanged (UOM has no docstatus)",
	)


# ── 6. disable_uom ──────────────────────────────────────────────────────────
def disable_uom(args: dict) -> ToolResult:
	"""Take a unit out of use. Refused while an active context still offers it."""
	_require_uom()
	tail = "Nothing was changed."
	require_uom_role("disable a unit of measure", tail)
	name = _existing_uom(as_str(args, "uom"), "uom", tail)
	offering = [row["context"] for row in _contexts_by_uom().get(name, []) if row["is_active"]]
	if offering:
		raise ToolError(
			f"{name} is offered by the active unit context(s) {', '.join(sorted(offering))}. A form "
			"for that work would list a unit nobody can pick. Remove it first "
			f"(remove_uom_from_context), or switch the context off. {tail}"
		)
	doc = frappe.get_doc(UOM, name)
	stocked = _count(ITEM, {"stock_uom": name}) if compat.doctype_exists(ITEM) else None
	already = not _checked(doc.get("enabled", 1))
	if not already:
		doc.enabled = 0
		doc.save()
	data = {
		"name": name,
		"enabled": False,
		"already_disabled": already,
		"items_stocked_in": stocked,
		"note": (
			"Disabled, not deleted: every existing row that counts in it keeps it. Items already "
			"stocked in it keep it too — change those with update_item before their first stock "
			"transaction, or add a conversion."
		),
	}
	return ToolResult(data, f"UOM {name} " + ("was already disabled" if already else "disabled"))


# ── 7. set_uom_conversion_factor ────────────────────────────────────────────
def set_uom_conversion_factor(args: dict) -> ToolResult:
	"""How many `to_uom` are in one `from_uom`, in ERPNext's global factor table. Upserts."""
	_require_uom()
	compat.require_doctype(CONVERSION_FACTOR, _ERPNEXT_HINT)
	tail = "Nothing was changed."
	require_uom_role("set a unit conversion factor", tail)
	from_uom = _existing_uom(as_str(args, "from_uom"), "from_uom", tail)
	to_uom = _existing_uom(as_str(args, "to_uom"), "to_uom", tail)
	if from_uom == to_uom:
		raise ToolError(f"{from_uom} to {from_uom} is 1 by definition and is not stored. {tail}")
	value = as_float(args.get("value"), "value")
	if value is None or value <= 0:
		raise ToolError(
			f"value must be a number greater than 0 — how many {to_uom} in one {from_uom}. {tail}"
		)

	fields = compat.existing_fields(CONVERSION_FACTOR, ("name", "category", "from_uom", "to_uom", "value"))
	reverse = frappe.db.get_value(
		CONVERSION_FACTOR, {"from_uom": to_uom, "to_uom": from_uom}, fields, as_dict=True
	)
	if reverse:
		implied = 1.0 / float(reverse.get("value") or 0) if float(reverse.get("value") or 0) else None
		if implied is None or abs(implied - value) > 1e-6 * max(1.0, value):
			raise ToolError(
				f"the site already records 1 {to_uom} = {reverse.get('value')} {from_uom}, which makes "
				f"1 {from_uom} = {implied!r} {to_uom}, not {value}. ERPNext reads a pair either way round, "
				f"so two rows that disagree make the answer depend on which one it finds first. Correct "
				f"that row (set_uom_conversion_factor with from_uom={to_uom}) or delete it first. {tail}"
			)
		return ToolResult(
			{"from_uom": from_uom, "to_uom": to_uom, "value": value, "already_recorded": "reversed"},
			f"1 {from_uom} = {value} {to_uom} is already recorded the other way round",
		)

	existing = frappe.db.get_value(
		CONVERSION_FACTOR, {"from_uom": from_uom, "to_uom": to_uom}, fields, as_dict=True
	)
	if existing:
		old = float(existing.get("value") or 0)
		if abs(old - value) <= 1e-9:
			return ToolResult(
				{
					"name": existing["name"],
					"from_uom": from_uom,
					"to_uom": to_uom,
					"value": value,
					"changed": {},
				},
				f"1 {from_uom} = {value} {to_uom} already",
			)
		doc = frappe.get_doc(CONVERSION_FACTOR, existing["name"])
		doc.value = value
		doc.save()
		return ToolResult(
			{
				"name": doc.name,
				"from_uom": from_uom,
				"to_uom": to_uom,
				"value": value,
				"changed": {"value": [old, value]},
			},
			f"1 {from_uom} = {value} {to_uom} (was {old})",
		)

	category, category_created = _category_for(as_str(args, "category"), from_uom, to_uom, fields, tail)
	doc = frappe.new_doc(CONVERSION_FACTOR)
	doc.from_uom = from_uom
	doc.to_uom = to_uom
	doc.value = value
	if category:
		doc.category = category
	doc.insert()
	data = {"name": doc.name, "from_uom": from_uom, "to_uom": to_uom, "value": value, "category": category}
	if category_created:
		data["category_created"] = True
	return ToolResult(data, f"recorded 1 {from_uom} = {value} {to_uom}", docstatus_delta="none → created")


def _category_for(given: str, from_uom: str, to_uom: str, fields: list, tail: str) -> tuple[str, bool]:
	"""The UOM Category a new factor files under: the one named, or one either unit already uses."""
	if "category" not in fields:
		return "", False
	if given:
		if compat.doctype_exists(UOM_CATEGORY) and not frappe.db.exists(UOM_CATEGORY, given):
			category = frappe.new_doc(UOM_CATEGORY)
			category.category_name = given
			category.insert()
			return category.name, True
		return given, False
	for side in ("from_uom", "to_uom"):
		for unit in (from_uom, to_uom):
			found = frappe.db.get_value(CONVERSION_FACTOR, {side: unit}, "category")
			if found:
				return found, False
	known = (
		frappe.db.get_all(UOM_CATEGORY, pluck="name", limit=50) if compat.doctype_exists(UOM_CATEGORY) else []
	)
	raise ToolError(
		f"ERPNext files every factor under a UOM Category, and neither {from_uom} nor {to_uom} has a "
		f"factor to take one from. Pass category — one of {', '.join(sorted(known)) or '<none yet>'}, or "
		f"a new name, which is created. {tail}"
	)


# ── 8. delete_uom_conversion_factor ─────────────────────────────────────────
def delete_uom_conversion_factor(args: dict) -> ToolResult:
	"""Remove one global factor. The units themselves are untouched."""
	_require_uom()
	compat.require_doctype(CONVERSION_FACTOR, _ERPNEXT_HINT)
	tail = "Nothing was deleted."
	require_uom_role("delete a unit conversion factor", tail)
	from_uom = _existing_uom(as_str(args, "from_uom"), "from_uom", tail)
	to_uom = _existing_uom(as_str(args, "to_uom"), "to_uom", tail)
	name = frappe.db.get_value(CONVERSION_FACTOR, {"from_uom": from_uom, "to_uom": to_uom}, "name")
	if not name:
		flipped = frappe.db.get_value(CONVERSION_FACTOR, {"from_uom": to_uom, "to_uom": from_uom}, "name")
		hint = (
			f" It is recorded the other way round — pass from_uom={to_uom}, to_uom={from_uom}."
			if flipped
			else ""
		)
		raise ToolError(f"no factor from {from_uom} to {to_uom} on this site.{hint} {tail}")
	value = frappe.db.get_value(CONVERSION_FACTOR, name, "value")
	frappe.delete_doc(CONVERSION_FACTOR, name)
	return ToolResult(
		{"deleted": name, "from_uom": from_uom, "to_uom": to_uom, "value": float(value or 0)},
		f"deleted factor 1 {from_uom} = {value} {to_uom}",
		docstatus_delta="deleted",
	)


# ── 9–12. the agricultural unit contexts ────────────────────────────────────
def _context_doc(value, tail: str):
	compat.require_doctype(UOM_CONTEXT, _APP_HINT)
	name = str(value or "").strip()
	if not name:
		raise ToolError(f"context is required. {tail}")
	if not frappe.db.exists(UOM_CONTEXT, name):
		for other in frappe.db.get_all(UOM_CONTEXT, pluck="name", limit=500):
			if other.lower() == name.lower():
				name = other
				break
		else:
			known = frappe.db.get_all(UOM_CONTEXT, pluck="name", limit=50)
			raise ToolError(
				f"no unit context called {name!r}. Known: {', '.join(sorted(known)) or '<none>'}; "
				f"create_ag_uom_context adds one. {tail}"
			)
	return frappe.get_doc(UOM_CONTEXT, name)


def _rows(doc) -> list[dict]:
	return [
		{
			"uom": row.get("uom"),
			"is_default": 1 if _checked(row.get("is_default")) else 0,
			"notes": row.get("notes") or "",
		}
		for row in (doc.get("uoms") or [])
	]


def _check_measure(context: str, applies_to: str, uom: str, tail: str) -> None:
	measures = ag_uom.dimension_of(uom)
	if applies_to and measures and measures != applies_to:
		raise ToolError(
			f"{uom} measures {measures} and {context} measures {applies_to}. A list mixing two "
			"measurements lets '2' mean either, which is the error these contexts exist to refuse. "
			f"Put {uom} in a {measures} context instead. {tail}"
		)


def _save_context(doc, tail: str, insert: bool = False) -> None:
	"""Save through the controller, turning its refusal into this app's sentence."""
	try:
		doc.insert() if insert else doc.save()
	except frappe.ValidationError as exc:
		raise ToolError(f"{exc} {tail}") from None


def _context_answer(doc) -> dict:
	rows = _rows(doc)
	return {
		"name": doc.name,
		"applies_to": doc.get("applies_to"),
		"is_active": _checked(doc.get("is_active")),
		"description": doc.get("description") or None,
		"default_uom": next((row["uom"] for row in rows if row["is_default"]), None),
		"valid_uoms": [row["uom"] for row in rows],
	}


def create_ag_uom_context(args: dict) -> ToolResult:
	"""A new list of the units one kind of work is measured in."""
	compat.require_doctype(UOM_CONTEXT, _APP_HINT)
	tail = "Nothing was created."
	require_uom_role("create a unit context", tail)
	name = " ".join(as_str(args, "context_name", required=True).split())
	if frappe.db.exists(UOM_CONTEXT, name):
		raise ToolError(
			f"a unit context called {name!r} already exists; add_uom_to_context extends it. {tail}"
		)
	applies_to = as_choice(UOM_CONTEXT, "applies_to", as_str(args, "applies_to", required=True), "applies_to")
	raw = args.get("uoms")
	if not isinstance(raw, list) or not raw:
		raise ToolError(
			"uoms must be a non-empty list of {uom, is_default, notes}. An empty allow-list forbids "
			f"everything or permits everything depending on who reads it. {tail}"
		)
	doc = frappe.new_doc(UOM_CONTEXT)
	doc.context_name = name
	doc.applies_to = applies_to
	doc.description = as_str(args, "description")
	doc.is_active = 1
	for position, entry in enumerate(raw, start=1):
		entry = entry if isinstance(entry, dict) else {"uom": entry}
		uom = _existing_uom(entry.get("uom"), f"uoms entry {position}", tail)
		_check_measure(name, applies_to, uom, tail)
		doc.append(
			"uoms",
			{
				"uom": uom,
				"is_default": 1 if compat.checked(entry.get("is_default")) else 0,
				"notes": str(entry.get("notes") or ""),
			},
		)
	_save_context(doc, tail, insert=True)
	return ToolResult(
		_context_answer(doc), f"created unit context {doc.name}", docstatus_delta="none → created"
	)


def update_ag_uom_context(args: dict) -> ToolResult:
	"""Switch a context on or off, reword it, or change its default unit."""
	tail = "Nothing was changed."
	require_uom_role("change a unit context", tail)
	doc = _context_doc(as_str(args, "context"), tail)
	changes: dict = {}
	active = as_bool(args, "is_active")
	if active is not None and active != _checked(doc.get("is_active")):
		changes["is_active"] = [not active, active]
		doc.is_active = 1 if active else 0
	if args.get("description") is not None and str(args["description"]) != (doc.get("description") or ""):
		changes["description"] = [doc.get("description"), str(args["description"])]
		doc.description = str(args["description"])
	if args.get("default_uom") is not None:
		wanted = str(args["default_uom"]).strip()
		rows = _rows(doc)
		if wanted:
			wanted = _existing_uom(wanted, "default_uom", tail)
			if wanted not in [row["uom"] for row in rows]:
				raise ToolError(
					f"{wanted} is not one of {doc.name}'s units ({', '.join(r['uom'] for r in rows)}). "
					f"add_uom_to_context with is_default=true adds it and makes it the default. {tail}"
				)
		before = next((row["uom"] for row in rows if row["is_default"]), None)
		if before != (wanted or None):
			changes["default_uom"] = [before, wanted or None]
			for row in rows:
				row["is_default"] = 1 if row["uom"] == wanted else 0
			doc.set("uoms", rows)
	if not changes:
		if active is None and args.get("description") is None and args.get("default_uom") is None:
			raise ToolError(f"nothing to change. Pass is_active, description or default_uom. {tail}")
		return ToolResult({**_context_answer(doc), "changed": {}}, f"{doc.name} already matched")
	_save_context(doc, tail)
	return ToolResult(
		{**_context_answer(doc), "changed": changes},
		f"updated unit context {doc.name}: {', '.join(sorted(changes))}",
	)


def add_uom_to_context(args: dict) -> ToolResult:
	"""Offer one more unit for a kind of work."""
	tail = "Nothing was changed."
	require_uom_role("change a unit context", tail)
	doc = _context_doc(as_str(args, "context"), tail)
	uom = _existing_uom(as_str(args, "uom"), "uom", tail)
	rows = _rows(doc)
	if uom in [row["uom"] for row in rows]:
		raise ToolError(f"{doc.name} already lists {uom}. update_ag_uom_context changes its default. {tail}")
	_check_measure(doc.name, doc.get("applies_to"), uom, tail)
	default = bool(as_bool(args, "is_default", False))
	if default:
		for row in rows:
			row["is_default"] = 0
	rows.append({"uom": uom, "is_default": 1 if default else 0, "notes": as_str(args, "notes")})
	doc.set("uoms", rows)
	_save_context(doc, tail)
	return ToolResult(
		_context_answer(doc), f"{doc.name} now offers {uom}" + (" (default)" if default else "")
	)


def remove_uom_from_context(args: dict) -> ToolResult:
	"""Stop offering one unit for a kind of work. The unit itself is untouched."""
	tail = "Nothing was changed."
	require_uom_role("change a unit context", tail)
	doc = _context_doc(as_str(args, "context"), tail)
	uom = _existing_uom(as_str(args, "uom"), "uom", tail)
	rows = _rows(doc)
	kept = [row for row in rows if row["uom"] != uom]
	if len(kept) == len(rows):
		raise ToolError(f"{doc.name} does not list {uom} ({', '.join(r['uom'] for r in rows)}). {tail}")
	if not kept:
		raise ToolError(
			f"{uom} is {doc.name}'s only unit, and a context with none is refused — an empty list "
			"forbids everything or permits everything depending on who reads it. Switch the context "
			f"off instead (update_ag_uom_context is_active=false). {tail}"
		)
	doc.set("uoms", kept)
	_save_context(doc, tail)
	return ToolResult(_context_answer(doc), f"{doc.name} no longer offers {uom}")


# ── 12. set_uom_aliases ─────────────────────────────────────────────────────
def _spellings(args: dict, key: str) -> list[str] | None:
	value = args.get(key)
	if value is None:
		return None
	if isinstance(value, str):
		value = [value]
	if not isinstance(value, (list, tuple)):
		raise ToolError(f"{key} must be a list of spellings, got {type(value).__name__}.")
	return uom_resolve.parse_aliases("\n".join(str(item) for item in value))


def set_uom_aliases(args: dict) -> ToolResult:
	"""Add, remove or replace the spellings a site accepts for one unit. No deploy needed.

	REFUSES A SPELLING ANOTHER UNIT ANSWERS TO — its name, one of its aliases, or
	what the built-in table sends there — because the resolver would then read
	the same printed word two ways depending on which it tried first.
	"""
	_require_uom()
	tail = "Nothing was changed."
	require_uom_role("change a unit's spellings", tail)
	name = _existing_uom(as_str(args, "uom"), "uom", tail)
	add, remove, replace = (_spellings(args, key) for key in ("add", "remove", "replace"))
	if add is None and remove is None and replace is None:
		raise ToolError(f"nothing to change. Pass add, remove or replace (a list of spellings). {tail}")
	if replace is not None and (add or remove):
		raise ToolError(f"replace sets the whole list; pass it alone, not with add or remove. {tail}")
	if not ensure_uom_alias_field():
		raise ToolError(
			f"this site's UOM has no {ALIAS_FIELD} column and would not take one. {_APP_HINT} {tail}"
		)

	doc = frappe.get_doc(UOM, name)
	before = uom_resolve.parse_aliases(doc.get(ALIAS_FIELD))
	if replace is not None:
		after = list(replace)
	else:
		dropping = {spelling.lower() for spelling in remove or ()}
		after = [spelling for spelling in before if spelling.lower() not in dropping]
		for spelling in add or ():
			if spelling.lower() not in {entry.lower() for entry in after}:
				after.append(spelling)

	others = [
		dict(row, aliases=spellings if row["name"] != name else [])
		for row in uom_resolve.site_uoms()
		for spellings in [row.get("aliases") or []]
	]
	if not any(row["name"] == name for row in others):  # a disabled unit is still its own
		others.append({"name": name, "must_be_whole_number": False, "aliases": []})
	conflicts = []
	for spelling in after:
		if spelling.lower() == name.lower():
			continue
		answer = uom_resolve.resolve_unit(spelling, others)
		if answer["uom"] and answer["uom"] != name:
			conflicts.append(f"{spelling!r} already means {answer['uom']} ({answer['matched_by']})")
	if conflicts:
		raise ToolError(
			f"{'; '.join(conflicts)}. A printed word may mean one unit only — remove it from the "
			f"other unit first (set_uom_aliases remove), or use that unit. {tail}"
		)

	after = [spelling for spelling in after if spelling.lower() != name.lower()]
	if after == before:
		return ToolResult(
			{"name": name, "aliases": after, "changed": False}, f"{name}'s spellings already matched"
		)
	doc.set(ALIAS_FIELD, "\n".join(after))
	doc.save()
	return ToolResult(
		{
			"name": name,
			"aliases": after,
			"added": [s for s in after if s.lower() not in {b.lower() for b in before}],
			"removed": [s for s in before if s.lower() not in {a.lower() for a in after}],
			"changed": True,
		},
		f"{name} is also written as: {', '.join(after) or '<nothing>'}",
		docstatus_delta="unchanged (UOM has no docstatus)",
	)
