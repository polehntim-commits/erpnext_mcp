# SPDX-License-Identifier: MIT
"""Rename records through MCP — `record action=rename`. v0.327.0.

WHY. Tim, 2026-10-13: "Code tool please, via MCP all the way." The Mill Creek cabins were registered from the phone
as MC-CABIN-6&6, 16&17, 18&19 … and the doors say 5&6, 16&16, 18&18. `update_housing_unit` refuses a new
`unit_name` (the docname is built from it) and nothing else renamed a record, so the fix had to be a Desk session.

WHAT IT DOES
- **Frappe's `rename_doc`** (force, never merge): the docname moves and every Link and Dynamic Link to it follows —
  assignments, tasks, inspections, files and photos, print jobs, the timeline.
- **A Housing Unit and its Asset Register tag are renamed together** in one call (`asset_tag` pairs them); naming
  either one renames both and re-pairs them. A unit's `unit_name` follows its docname (`<unit_name> - <parcel abbr>`).
- **The old name is kept** in `old_names` on both, and `current_name()` answers it — the scanner and search use it, so
  a printed tag or a note with the old name still lands on the record.
- **Dry run by default:** every link that would move, per field, the paired record, printed tags, and the order.
- **Refuses** a name that is taken (by anything not itself being renamed away in the same batch), a submitted or
  cancelled document, the money / HR / security registers, and a record this account may not write.
- **A batch is ordered** so nothing collides (16&17 → 16&16 before 15&16 → 16&17); a cycle goes through a temporary
  name. The whole batch is checked before the first rename.
- **Printed tags:** a record with a printed (or downloaded) Card Print Job is flagged "needs a new tag";
  `queue_reprint` puts it on the print queue.
"""

from __future__ import annotations

import frappe

from . import compat

HOUSING = "Housing Unit"
ASSET = "Asset Register"
PAIR_LINK = "asset_tag"
OLD_NAMES = "old_names"
PRINT_JOB = "Card Print Job"
PRINTED = ("Printed", "Downloaded")
MAX_BATCH = 100
#: Never renamed here: money, HR, identity and security keep their own tools (and most of them are submitted).
REFUSED = ("Account", "GL Entry", "Journal Entry", "Payment Entry", "Sales Invoice", "Purchase Invoice", "Employee",
           "User", "Company", "Salary Slip", "Bank Account", "Bank Transaction", "I-9 Form", "W-4 Form",
           "Farm Payroll Entry", "DocType", "Role", "File", "Fiscal Year", "Cost Center")


class RenameError(ValueError):
	pass


def doctype_for(type_: str) -> str:
	"""`housing_unit` → "Housing Unit"; a DocType name is taken as given."""
	text = str(type_ or "").strip()
	if not text:
		raise RenameError("type is required (e.g. housing_unit, asset_register, field, item).")
	from .tools import record_tool

	key = text.lower().replace(" ", "_")
	candidates = [text, record_tool.GENERIC.get(key, ""), " ".join(w.capitalize() for w in key.split("_"))]
	if key in ("asset", "asset_tag", "tag"):
		candidates.insert(0, ASSET)
	for name in candidates:
		if name and compat.doctype_exists(name):
			return name
	raise RenameError(f"no record type {type_!r} on this site.")


def _aliases(raw) -> list:
	return [a.strip() for a in str(raw or "").replace(",", "\n").splitlines() if a.strip()]


def current_name(old: str) -> tuple:
	"""`(doctype, name)` a renamed Housing Unit / Asset Register now carries for an old name, or `("", "")`."""
	text = str(old or "").strip()
	if not text:
		return "", ""
	for doctype in (HOUSING, ASSET):
		if not compat.has_field(doctype, OLD_NAMES):
			continue
		for row in frappe.db.get_all(doctype, filters={OLD_NAMES: ("like", f"%{text}%")},
		                             fields=["name", OLD_NAMES], limit=5) or []:
			if text.casefold() in (a.casefold() for a in _aliases(row.get(OLD_NAMES))):
				return doctype, str(row["name"])
	return "", ""


# ── the plan ────────────────────────────────────────────────────────────────
def _housing_docname(doctype: str, name: str, new: str) -> tuple:
	"""(new docname, new unit_name) — a unit's docname is `<unit_name> - <abbr>`; either spelling may be given."""
	if doctype != HOUSING:
		return new, None
	from .abbr import parcel_abbr, suffixed

	parcel = frappe.db.get_value(HOUSING, name, "parcel")
	abbr = parcel_abbr(parcel) if parcel else ""
	unit_name = new[: -len(f" - {abbr}")] if abbr and new.endswith(f" - {abbr}") else new
	return suffixed(unit_name, abbr), unit_name


def _partner(doctype: str, name: str) -> tuple:
	"""The other half of a cabin: (doctype, name) or ("", "")."""
	if doctype == HOUSING and compat.has_field(HOUSING, PAIR_LINK):
		tag = frappe.db.get_value(HOUSING, name, PAIR_LINK)
		return (ASSET, str(tag)) if tag and frappe.db.exists(ASSET, tag) else ("", "")
	if doctype == ASSET and compat.has_field(HOUSING, PAIR_LINK):
		unit = frappe.db.get_value(HOUSING, {PAIR_LINK: name}, "name")
		return (HOUSING, str(unit)) if unit else ("", "")
	return "", ""


def _partner_new(old_self: str, new_self: str, partner_old: str, partner_doctype: str) -> str:
	"""The partner's new name: the same string where they shared one (MC-CABIN-6&6 - MC on both), else its own
	name with the unit's old part replaced; unchanged where neither applies."""
	if partner_old == old_self:
		return new_self
	if old_self and old_self in partner_old:
		return partner_old.replace(old_self, new_self)
	base_old = old_self.split(" - ")[0]
	base_new = new_self.split(" - ")[0]
	if base_old and base_old in partner_old:
		return partner_old.replace(base_old, base_new)
	return partner_old


def _link_fields(doctype: str) -> list:
	"""(parent doctype, fieldname, kind) for every Link / Dynamic Link that can point at `doctype`."""
	out = []
	try:
		for row in frappe.get_all("DocField", filters={"fieldtype": "Link", "options": doctype},
		                          fields=["parent", "fieldname"]) or []:
			out.append((row["parent"], row["fieldname"], "link"))
		for row in frappe.get_all("Custom Field", filters={"fieldtype": "Link", "options": doctype},
		                          fields=["dt", "fieldname"]) or []:
			out.append((row["dt"], row["fieldname"], "link"))
	except Exception:  # noqa: BLE001 — a double with no meta tables: the counts below say "unknown"
		pass
	return out


def _links(doctype: str, name: str) -> list:
	"""Every row that names this record and would be repointed — the dry run's evidence."""
	found = []
	for parent, field, _kind in _link_fields(doctype):
		try:
			count = frappe.db.count(parent, {field: name})
		except Exception:  # noqa: BLE001 — a child table or a missing column: skip, never fail a dry run
			continue
		if count:
			found.append({"doctype": parent, "field": field, "rows": int(count)})
	for parent, field in (("File", "attached_to_name"), (PRINT_JOB, "reference_name"), ("Comment", "reference_name")):
		try:
			if compat.doctype_exists(parent):
				typed = {"File": "attached_to_doctype", PRINT_JOB: "reference_doctype", "Comment": "reference_doctype"}
				count = frappe.db.count(parent, {field: name, typed[parent]: doctype})
				if count:
					found.append({"doctype": parent, "field": field, "rows": int(count)})
		except Exception:  # noqa: BLE001
			continue
	return found


def _printed(doctype: str, name: str) -> dict | None:
	if not compat.doctype_exists(PRINT_JOB):
		return None
	rows = frappe.db.get_all(PRINT_JOB, filters={"reference_doctype": doctype, "reference_name": name,
	                                             "status": ("in", list(PRINTED))},
	                         fields=["name", "printed_at", "job_type"], order_by="printed_at desc", limit=1) or []
	return {"job": rows[0]["name"], "printed_at": str(rows[0].get("printed_at") or "")[:10] or None} if rows else None


def _check_one(doctype: str, name: str) -> None:
	if doctype in REFUSED:
		raise RenameError(f"{doctype} is not renamed here — it keeps its own tools.")
	if not frappe.db.exists(doctype, name):
		raise RenameError(f"no {doctype} called {name!r}.")
	docstatus = int(frappe.db.get_value(doctype, name, "docstatus") or 0)
	if docstatus != 0:
		raise RenameError(f"{doctype} {name} is {'submitted' if int(docstatus) == 1 else 'cancelled'}; a filed "
		                  "document keeps its name.")
	if not frappe.has_permission(doctype, "write", name):
		raise RenameError(f"this account may not change {doctype} {name}.")


def plan(type_: str, renames: list) -> dict:
	"""Check every rename in the batch and order them. Nothing is written."""
	doctype = doctype_for(type_)
	if not isinstance(renames, list) or not renames:
		raise RenameError("give name and new_name, or renames=[{name, new_name}, …].")
	if len(renames) > MAX_BATCH:
		raise RenameError(f"at most {MAX_BATCH} renames per call.")
	steps = []
	for entry in renames:
		old = str((entry or {}).get("name") or "").strip()
		new_raw = str((entry or {}).get("new_name") or "").strip()
		if not old or not new_raw:
			raise RenameError("every rename needs name and new_name.")
		_check_one(doctype, old)
		new, unit_name = _housing_docname(doctype, old, new_raw)
		step = {"doctype": doctype, "name": old, "new_name": new, "unit_name": unit_name,
		        "door_label": (entry or {}).get("door_label"), "room_labels": (entry or {}).get("room_labels")}
		steps.append(step)
		p_doctype, p_old = _partner(doctype, old)
		if p_doctype:
			_check_one(p_doctype, p_old)
			if p_doctype == HOUSING:
				p_new, p_unit = _housing_docname(HOUSING, p_old, _partner_new(old, new_raw, p_old, p_doctype))
			else:
				p_new, p_unit = _partner_new(old, new, p_old, p_doctype), None
			step["partner"] = {"doctype": p_doctype, "name": p_old, "new_name": p_new, "unit_name": p_unit}
	# Every (doctype, name) leaving, every (doctype, new) arriving.
	leaving = {(s["doctype"], s["name"]) for s in steps} | {(s["partner"]["doctype"], s["partner"]["name"])
	                                                       for s in steps if s.get("partner")}
	arriving: dict = {}
	for s in steps:
		for d, o, n in [(s["doctype"], s["name"], s["new_name"])] + (
				[(s["partner"]["doctype"], s["partner"]["name"], s["partner"]["new_name"])] if s.get("partner") else []):
			if n == o:
				continue
			if (d, n) in arriving:
				raise RenameError(f"two renames in this batch both want {d} {n!r}.")
			arriving[(d, n)] = o
			if frappe.db.exists(d, n) and (d, n) not in leaving:
				raise RenameError(f"{d} {n!r} already exists — a rename never merges two records. Rename that one "
				                  "first (in the same batch) or choose another name.")
	ordered, temporary = _order(steps)
	for s in steps:
		s["links"] = _links(s["doctype"], s["name"])
		s["printed_tag"] = _printed(s["doctype"], s["name"])
		if s.get("partner"):
			s["partner"]["links"] = _links(s["partner"]["doctype"], s["partner"]["name"])
			s["partner"]["printed_tag"] = _printed(s["partner"]["doctype"], s["partner"]["name"])
	return {"doctype": doctype, "steps": ordered, "temporary": temporary,
	        "needs_new_tag": [x for s in ordered for x in ([s] + ([s["partner"]] if s.get("partner") else []))
	                          if x.get("printed_tag")]}


def _order(steps: list) -> tuple:
	"""Order so no rename lands on a name still in use; break cycles through a temporary name."""
	pending = list(steps)
	ordered, temporary = [], []
	names_in_use = lambda: {s["name"] for s in pending}  # noqa: E731
	while pending:
		free = [s for s in pending if s["new_name"] == s["name"] or s["new_name"] not in names_in_use() - {s["name"]}]
		if free:
			step = free[0]
			pending.remove(step)
			ordered.append(step)
			continue
		# A cycle (A → B, B → A): move one aside first.
		step = pending[0]
		temp = f"{step['name']}~renaming"
		temporary.append({"name": step["name"], "temporary": temp})
		partner = step.get("partner")
		p_temp = {**partner, "new_name": f"{partner['name']}~renaming"} if partner else None
		ordered.append({**step, "new_name": temp, "unit_name": None, "is_temporary": True,
		                "partner": p_temp, "links": [], "printed_tag": None})
		pending[0] = {**step, "name": temp, "from_temporary": step["name"],
		              "partner": ({**partner, "name": p_temp["new_name"], "from_temporary": partner["name"]}
		                          if partner else None)}
	return ordered, temporary


# ── doing it ────────────────────────────────────────────────────────────────
def _remember(doctype: str, name: str, old: str) -> None:
	if not compat.has_field(doctype, OLD_NAMES) or not old or old.endswith("~renaming"):
		return
	known = _aliases(frappe.db.get_value(doctype, name, OLD_NAMES))
	if old not in known:
		known.append(old)
		frappe.db.set_value(doctype, name, OLD_NAMES, "\n".join(known), update_modified=False)


def _rename(doctype: str, old: str, new: str) -> str:
	if new == old:
		return old
	return str(frappe.rename_doc(doctype, old, new, force=True, merge=False) or new)


def apply(planned: dict, *, queue_reprint: bool = False, reason: str = "") -> list:
	"""Run a checked plan. Each step: the record, its partner, unit_name, old names, the pairing, labels."""
	done = []
	for step in planned["steps"]:
		original = step.get("from_temporary") or step["name"]
		new = _rename(step["doctype"], step["name"], step["new_name"])
		row = {"doctype": step["doctype"], "from": original, "to": new}
		partner = step.get("partner")
		if step.get("is_temporary"):
			if partner:
				_rename(partner["doctype"], partner["name"], partner["new_name"])
			continue
		_remember(step["doctype"], new, original)
		if partner:
			p_original = partner.get("from_temporary") or partner["name"]
			p_new = _rename(partner["doctype"], partner["name"], partner["new_name"])
			_remember(partner["doctype"], p_new, p_original)
			row["partner"] = {"doctype": partner["doctype"], "from": p_original, "to": p_new}
			unit, tag = (new, p_new) if step["doctype"] == HOUSING else (p_new, new)
			if compat.has_field(HOUSING, PAIR_LINK):
				frappe.db.set_value(HOUSING, unit, PAIR_LINK, tag, update_modified=False)
			unit_name = step.get("unit_name") if step["doctype"] == HOUSING else partner.get("unit_name")
		else:
			unit_name = step.get("unit_name")
			unit = new if step["doctype"] == HOUSING else ""
		if unit and unit_name:
			frappe.db.set_value(HOUSING, unit, "unit_name", unit_name, update_modified=False)
		for target_doctype, target in ((HOUSING, unit), (ASSET, row.get("partner", {}).get("to") if unit else "")):
			if not target:
				continue
			for field in ("door_label", "room_labels"):
				if step.get(field) is not None and compat.has_field(target_doctype, field):
					frappe.db.set_value(target_doctype, target, field, str(step[field] or "") or None,
					                    update_modified=False)
		for doctype, name, before in [(row["doctype"], row["to"], row["from"])] + (
				[(row["partner"]["doctype"], row["partner"]["to"], row["partner"]["from"])] if partner else []):
			try:
				frappe.get_doc(doctype, name).add_comment(
					"Info", f"Renamed from {before} (MCP record rename){': ' + reason if reason else ''}")
			except Exception:  # noqa: BLE001 — the old_names column carries the same fact
				pass
		if queue_reprint:
			row["reprint"] = _queue_reprint(step, row)
		done.append(row)
	return done


def _queue_reprint(step: dict, row: dict) -> list:
	"""Queue a new tag for each half that had one printed. Never raises; says what it could not do."""
	import uuid

	queued = []
	for half, renamed in ((step, row), (step.get("partner") or {}, row.get("partner") or {})):
		if not half.get("printed_tag") or not renamed:
			continue
		try:
			from . import card_print

			answer = card_print.request(str(frappe.session.user or "Administrator"), "Asset Tag", renamed["to"],
			                            client_request_id=str(uuid.uuid4()), reprint_reason="Renamed",
			                            requested_from="API")
			queued.append({"record": renamed["to"], "job": (answer or {}).get("name") or (answer or {}).get("job")})
		except Exception as exc:  # noqa: BLE001
			queued.append({"record": renamed["to"], "error": str(exc)[:200]})
	return queued
