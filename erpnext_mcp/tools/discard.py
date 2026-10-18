# SPDX-License-Identifier: MIT
"""Discard a draft: set it aside, recoverably, instead of deleting it.

v0.334.0. Tim: "Yah lets do a discard please so its less confusing." A pipeline
that drafts duplicates leaves the office a queue in which the real drafts and the
dead ones look the same. Deleting the dead ones is final and loses the evidence;
leaving them makes every count wrong. A DISCARD is the middle: the draft stays
(docstatus 0, nothing deleted), carries who set it aside, when, why and — for a
duplicate — which entry it duplicates, and drops out of everything that counts or
posts drafts:

  * `bulk_submit_journal_entries` / `submit_journal_entry` will not post it;
  * `get_journal_entries` leaves it out unless asked;
  * the Desk list hides it by default (a toggle shows it again);
  * Bank Bridge's draft health stops counting it, and its dedupe still sees it —
    a discarded duplicate is an occupant, so the same duplicate is not drafted
    again (see `erpnext-bank-bridge` v1.0.11).

RESTORE puts it back exactly as it was. PURGE (Desk, System Manager / Accounts
Manager) deletes discarded drafts for good — and only those — after writing each
one to the Discarded Draft Log. That log is also what Bank Bridge reads after a
purge, so a duplicate that has been purged is still not re-created; its
`allow_recreate` box is the one way to let it be.

ONE DOCTYPE TODAY, A REGISTER FOR MORE. `DISCARDABLE` names what a doctype needs
(its amount, date and remark columns, and what counts as a blocking link).
Journal Entry is in it; Purchase Invoice and Payment Entry drafts opt in later
with an entry here and `ensure_fields(<doctype>)`, and every function below
already takes the doctype.

THE MARKER IS FIVE CUSTOM FIELDS on the drafted doctype, created at migrate and
on first use — the same way `banking_bridge.ensure_categorization_fields`
extends Bank Transaction. ERPNext's own code is not touched.
"""

from __future__ import annotations

import datetime
import json

import frappe

from .. import compat, roles, security, settings
from ..args import as_bool, as_limit, as_str
from ..errors import ToolError
from ..result import ToolResult

JOURNAL_ENTRY = "Journal Entry"
LOG = "Discarded Draft Log"
CUSTOM_FIELD = "Custom Field"

FLAG = "discarded"
REASON = "discard_reason"
BY = "discarded_by"
AT = "discarded_at"
DUPLICATE_OF = "duplicate_of"
FIELDS = (FLAG, REASON, BY, AT, DUPLICATE_OF)

#: Who may purge (delete for good). Discarding and restoring need only write access to the draft.
PURGE_ROLES = ("System Manager", "Accounts Manager")

#: Per call. A cleanup of a runaway pipeline was 112 drafts; this is above that.
MAX_BATCH = 500

#: What a doctype needs to be discardable. Purchase Invoice / Payment Entry opt in here.
DISCARDABLE = {
	JOURNAL_ENTRY: {
		"amount": "total_debit",
		"date": "posting_date",
		"remark": "user_remark",
		"insert_after": "user_remark",
	},
}


def _spec(doctype: str) -> dict:
	if doctype not in DISCARDABLE:
		raise ToolError(f"{doctype} drafts cannot be discarded yet; supported: {', '.join(DISCARDABLE)}.")
	return DISCARDABLE[doctype]


# ── the five columns ─────────────────────────────────────────────────────────


def installed(doctype: str = JOURNAL_ENTRY) -> bool:
	try:
		return all(compat.has_field(doctype, field) for field in FIELDS)
	except Exception:
		return False


def ensure_fields(doctype: str = JOURNAL_ENTRY) -> bool:
	"""Create the marker columns on `doctype`. Never raises; True when all five exist."""
	try:
		if installed(doctype):
			return True
		if not compat.doctype_exists(CUSTOM_FIELD) or not compat.doctype_exists(doctype):
			return False
	except Exception:
		return False
	after = _spec(doctype)["insert_after"]
	specification = (
		{
			"fieldname": FLAG,
			"label": "Discarded",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": after,
			"read_only": 1,
			"no_copy": 1,
			"description": (
				"Set aside: this draft will not be posted, counted or re-drafted. Nothing was deleted — "
				"Restore puts it back. Purge (System Manager / Accounts Manager) deletes discarded drafts "
				"for good and logs each one first."
			),
		},
		{
			"fieldname": REASON,
			"label": "Discard Reason",
			"fieldtype": "Small Text",
			"insert_after": FLAG,
			"read_only": 1,
			"no_copy": 1,
			"depends_on": f"eval:doc.{FLAG}",
		},
		{
			"fieldname": BY,
			"label": "Discarded By",
			"fieldtype": "Link",
			"options": "User",
			"insert_after": REASON,
			"read_only": 1,
			"no_copy": 1,
			"depends_on": f"eval:doc.{FLAG}",
		},
		{
			"fieldname": AT,
			"label": "Discarded At",
			"fieldtype": "Datetime",
			"insert_after": BY,
			"read_only": 1,
			"no_copy": 1,
			"depends_on": f"eval:doc.{FLAG}",
		},
		{
			"fieldname": DUPLICATE_OF,
			"label": "Duplicate Of",
			"fieldtype": "Link",
			"options": doctype,
			"insert_after": AT,
			"read_only": 1,
			"no_copy": 1,
			"depends_on": f"eval:doc.{FLAG}",
		},
	)
	created = False
	for field in specification:
		try:
			if compat.has_field(doctype, field["fieldname"]):
				continue
			if frappe.db.exists(CUSTOM_FIELD, {"dt": doctype, "fieldname": field["fieldname"]}):
				continue
			doc = frappe.new_doc(CUSTOM_FIELD)
			doc.dt = doctype
			for key, value in field.items():
				doc.set(key, value)
			doc.insert(ignore_permissions=True)
			created = True
		except Exception:
			frappe.log_error(
				title=f"erpnext_mcp: could not add {field['fieldname']} to {doctype}",
				message=compat.traceback_text(),
			)
			return False
	if created:
		try:
			frappe.clear_cache(doctype=doctype)
		except Exception:
			pass
	return installed(doctype)


def _require_installed(doctype: str, tail: str) -> None:
	if installed(doctype) or ensure_fields(doctype):
		return
	raise ToolError(
		f"this site's {doctype} has no discard columns and would not take them. Run `bench migrate` "
		f"after installing v0.334.0. {tail}"
	)


def is_discarded(doctype: str, name: str) -> bool:
	if not installed(doctype):
		return False
	return bool(frappe.db.get_value(doctype, name, FLAG))


def not_discarded_filter(doctype: str = JOURNAL_ENTRY) -> dict:
	"""`{discarded: 0}` when the column exists, else `{}` — for a query that counts live drafts."""
	return {FLAG: 0} if installed(doctype) else {}


# ── who is asking ────────────────────────────────────────────────────────────


def actor() -> str:
	return security.caller_identity() or str(getattr(frappe.session, "user", "") or "") or "Administrator"


def may_purge(user: str = "") -> bool:
	user = user or actor()
	if user == "Administrator":
		return True
	held = set(frappe.get_roles(user) or []) or set(roles.all_roles_of(user) or [])
	return bool(held & set(PURGE_ROLES))


def _now() -> str:
	return frappe.utils.now() if hasattr(frappe.utils, "now") else str(datetime.datetime.now())


# ── what a draft is tied to ──────────────────────────────────────────────────


def blocking_links(doctype: str, name: str) -> list:
	"""Why this draft may not be discarded: a submitted document or a bank match points at it."""
	reasons = []
	for row in frappe.db.get_all(
		"Bank Transaction Payments",
		filters={"payment_document": doctype, "payment_entry": name},
		fields=["parent"],
		limit=20,
	):
		reasons.append(f"it is matched to Bank Transaction {row.get('parent')} — unmatch it there first")
	for child, parent, ref_type, ref_name in (
		("Journal Entry Account", JOURNAL_ENTRY, "reference_type", "reference_name"),
		("Payment Entry Reference", "Payment Entry", "reference_doctype", "reference_name"),
	):
		if not compat.doctype_exists(child):
			continue
		for row in frappe.db.get_all(
			child, filters={ref_type: doctype, ref_name: name}, fields=["parent"], limit=20
		):
			if row.get("parent") == name:
				continue
			if frappe.db.get_value(parent, row.get("parent"), "docstatus") == 1:
				reasons.append(f"submitted {parent} {row.get('parent')} references it")
	return reasons


def _bank_transactions_on(doctype: str, name: str) -> list:
	"""Bank Transactions the draft's own lines reference (Bank Bridge's identity key)."""
	if doctype != JOURNAL_ENTRY:
		return []
	out = []
	for row in frappe.get_doc(doctype, name).get("accounts") or []:
		get = row.get if isinstance(row, dict) else (lambda key, row=row: getattr(row, key, None))
		if get("reference_type") == "Bank Transaction" and get("reference_name"):
			out.append(get("reference_name"))
	return sorted(set(out))


# ── discard / restore ────────────────────────────────────────────────────────


def discard(doctype: str, names: list, reason: str, duplicate_of: str = "", user: str = "") -> list:
	"""Mark drafts discarded; one result row per name. Writes only the rows that pass."""
	tail = "Nothing was discarded."
	_spec(doctype)
	_require_installed(doctype, tail)
	reason = str(reason or "").strip()
	if len(reason) < 4:
		raise ToolError(f"reason must say why — it is what the next person reads. {tail}")
	names = _names(names, tail)
	duplicate_of = str(duplicate_of or "").strip()
	user = user or actor()
	results = []
	for name in names:
		row = {"name": name, "ok": False, "skipped": "", "error": None}
		try:
			row.update(_discard_one(doctype, name, reason, duplicate_of, user))
		except ToolError as exc:
			row["error"] = str(exc)
		results.append(row)
	return results


def _discard_one(doctype: str, name: str, reason: str, duplicate_of: str, user: str) -> dict:
	if not frappe.db.exists(doctype, name):
		raise ToolError(f"no {doctype} named {name!r}")
	doc = frappe.db.get_value(doctype, name, ["docstatus", "company", FLAG], as_dict=True)
	if int(doc.get("docstatus") or 0) != 0:
		state = {1: "submitted — it has posted; reverse it with a cancel", 2: "cancelled"}[
			int(doc["docstatus"])
		]
		raise ToolError(f"{name} is {state}. Only a draft can be discarded")
	if doc.get(FLAG):
		return {"ok": True, "skipped": "already_discarded"}
	blocks = blocking_links(doctype, name)
	if blocks:
		raise ToolError(f"{name} cannot be discarded: " + "; ".join(blocks))
	if duplicate_of:
		if duplicate_of == name:
			raise ToolError(f"{name} cannot be a duplicate of itself")
		other = frappe.db.get_value(doctype, duplicate_of, ["docstatus", "company", FLAG], as_dict=True)
		if not other:
			raise ToolError(f"duplicate_of: no {doctype} named {duplicate_of!r}")
		if int(other.get("docstatus") or 0) == 2 or other.get(FLAG):
			raise ToolError(
				f"duplicate_of {duplicate_of} is cancelled or itself discarded; name the entry that stays"
			)
		if other.get("company") != doc.get("company"):
			raise ToolError(
				f"duplicate_of {duplicate_of} belongs to {other.get('company')}, not {doc.get('company')}"
			)
	frappe.db.set_value(
		doctype,
		name,
		{FLAG: 1, REASON: reason, BY: user, AT: _now(), DUPLICATE_OF: duplicate_of or None},
		update_modified=False,
	)
	_comment(
		doctype,
		name,
		f"Discarded by {user}: {reason}" + (f" (duplicate of {duplicate_of})" if duplicate_of else ""),
	)
	return {"ok": True, "duplicate_of": duplicate_of or None}


def restore(doctype: str, name: str, user: str = "") -> dict:
	tail = "Nothing was restored."
	_spec(doctype)
	_require_installed(doctype, tail)
	if not frappe.db.exists(doctype, name):
		raise ToolError(
			f"no {doctype} named {name!r}. If it was purged, the Discarded Draft Log has it. {tail}"
		)
	row = frappe.db.get_value(doctype, name, ["docstatus", *FIELDS], as_dict=True)
	if not row.get(FLAG):
		raise ToolError(f"{name} is not discarded. {tail}")
	frappe.db.set_value(
		doctype, name, {FLAG: 0, REASON: None, BY: None, AT: None, DUPLICATE_OF: None}, update_modified=False
	)
	user = user or actor()
	_comment(doctype, name, f"Restored by {user} (was discarded by {row.get(BY)}: {row.get(REASON)})")
	return {
		"name": name,
		"restored": True,
		"was": {key: _plain(row.get(key)) for key in FIELDS if key != FLAG},
	}


def _comment(doctype: str, name: str, text: str) -> None:
	try:
		frappe.get_doc(doctype, name).add_comment("Comment", text)
	except Exception:
		pass


def _plain(value):
	return str(value) if isinstance(value, (datetime.date, datetime.datetime)) else value


def _names(raw, tail: str) -> list:
	if isinstance(raw, str):
		raw = [part.strip() for part in raw.split(",")]
	if not isinstance(raw, list) or not [n for n in raw if str(n or "").strip()]:
		raise ToolError(f"names must be a non-empty list of docnames. {tail}")
	if len(raw) > MAX_BATCH:
		raise ToolError(f"names has {len(raw)} entries; the limit is {MAX_BATCH} per call. {tail}")
	out = []
	for value in raw:
		name = str(value or "").strip()
		if name and name not in out:
			out.append(name)
	return out


# ── reading them back ────────────────────────────────────────────────────────


def discarded_rows(
	doctype: str = JOURNAL_ENTRY, company: str = "", names=None, older_than=None, limit: int = 500
) -> list:
	if not installed(doctype):
		return []
	spec = _spec(doctype)
	filters = {FLAG: 1, "docstatus": 0}
	if company:
		filters["company"] = company
	if names:
		filters["name"] = ("in", list(names))
	if older_than:
		filters[AT] = ("<", older_than)
	fields = compat.existing_fields(
		doctype, ["name", "company", spec["date"], spec["amount"], spec["remark"], *FIELDS[1:]]
	)
	rows = frappe.db.get_all(doctype, filters=filters, fields=fields, order_by=f"{AT} asc", limit=limit)
	return [
		{
			"name": row.get("name"),
			"company": row.get("company"),
			"posting_date": _plain(row.get(spec["date"])),
			"amount": round(float(row.get(spec["amount"]) or 0), 2),
			"remark": row.get(spec["remark"]),
			"reason": row.get(REASON),
			"discarded_by": row.get(BY),
			"discarded_at": _plain(row.get(AT)),
			"duplicate_of": row.get(DUPLICATE_OF),
		}
		for row in rows
	]


def summary(doctype: str = JOURNAL_ENTRY, company: str = "") -> dict:
	rows = discarded_rows(doctype, company)
	return {
		"doctype": doctype,
		"company": company or None,
		"count": len(rows),
		"total": round(sum(row["amount"] for row in rows), 2),
		"may_purge": may_purge(),
	}


# ── purge ────────────────────────────────────────────────────────────────────


def purge(
	doctype: str = JOURNAL_ENTRY,
	names=None,
	company: str = "",
	user: str = "",
	trigger: str = "Manual",
	older_than=None,
) -> dict:
	"""Delete discarded drafts for good, each logged first, each in its own transaction.

	ONLY A DISCARDED DRAFT IS EVER DELETED: the set is read back from the marker,
	and each row is checked again just before its delete, so a draft restored in
	the meantime survives.
	"""
	tail = "Nothing was purged."
	_spec(doctype)
	user = user or actor()
	if trigger != "Auto" and not may_purge(user):
		raise ToolError(f"{user} may not purge: it takes {' or '.join(PURGE_ROLES)}. {tail}")
	if not compat.doctype_exists(LOG):
		raise ToolError(f"this site has no {LOG} yet — run `bench migrate`. {tail}")
	targets = discarded_rows(doctype, company, names=names, older_than=older_than, limit=MAX_BATCH)
	results = []
	for row in targets:
		results.append(_purge_one(doctype, row, user, trigger))
	done = [r for r in results if r["ok"]]
	return {
		"purged": len(done),
		"failed": len(results) - len(done),
		"total": round(sum(r["amount"] for r in done), 2),
		"results": results,
		"not_discarded": sorted(set(names or []) - {row["name"] for row in targets}),
	}


def _purge_one(doctype: str, row: dict, user: str, trigger: str) -> dict:
	name = row["name"]
	out = {"name": name, "amount": row["amount"], "ok": False, "log": None, "cleared": [], "error": None}
	try:
		current = frappe.db.get_value(doctype, name, ["docstatus", FLAG], as_dict=True)
		if not current or int(current.get("docstatus") or 0) != 0 or not current.get(FLAG):
			out["error"] = "no longer a discarded draft — left alone"
			return out
		doc = frappe.get_doc(doctype, name)
		snapshot = doc.as_dict() if hasattr(doc, "as_dict") else dict(doc)
		bank_transactions = _bank_transactions_on(doctype, name)
		cleared = _clear_soft_links(doctype, name)
		log = frappe.get_doc(
			{
				"doctype": LOG,
				"document_type": doctype,
				"document_name": name,
				"company": row.get("company"),
				"posting_date": row.get("posting_date"),
				"amount": row["amount"],
				"user_remark": row.get("remark"),
				"discard_reason": row.get("reason"),
				"duplicate_of": row.get("duplicate_of"),
				"discarded_by": row.get("discarded_by"),
				"discarded_at": row.get("discarded_at"),
				"purged_by": user,
				"purged_at": _now(),
				"purge_trigger": trigger,
				"bank_transactions": ", ".join(bank_transactions),
				"cleared_links": "; ".join(cleared),
				"document_json": json.dumps(snapshot, default=str)[:100000],
				"allow_recreate": 0,
			}
		)
		log.insert(ignore_permissions=True)
		# NOT force: Frappe's own "is it linked?" check still runs, so a document that started
		# pointing at this draft after it was discarded makes the delete fail, not vanish.
		frappe.delete_doc(doctype, name, ignore_permissions=True)
		frappe.db.commit()
		out.update(ok=True, log=log.name, cleared=cleared)
	except Exception as exc:
		frappe.db.rollback()
		out["error"] = f"{type(exc).__name__}: {exc}"
	return out


def _clear_soft_links(doctype: str, name: str) -> list:
	"""Unhook the two soft links that would block the delete — only where they point at this draft."""
	cleared = []
	if compat.doctype_exists("Expense Receipt") and compat.has_field("Expense Receipt", "linked_document"):
		for receipt in frappe.db.get_all(
			"Expense Receipt", filters={"linked_doctype": doctype, "linked_document": name}, pluck="name"
		):
			frappe.db.set_value("Expense Receipt", receipt, {"linked_doctype": None, "linked_document": None})
			cleared.append(f"Expense Receipt {receipt}")
	for payment in frappe.db.get_all(
		"Bank Transaction Payments",
		filters={"payment_document": doctype, "payment_entry": name},
		fields=["name", "parent"],
	):
		frappe.db.delete("Bank Transaction Payments", {"name": payment.get("name")})
		cleared.append(f"Bank Transaction {payment.get('parent')} payment row")
	return cleared


def auto_purge() -> dict | None:
	"""Daily: purge discarded drafts older than `discard_auto_purge_days`. OFF at 0 (the default). Never raises."""
	try:
		days = frappe.utils.cint(getattr(settings.get_settings(), "discard_auto_purge_days", None))
	except Exception:
		return None
	if days <= 0:
		return None
	try:
		cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
		out = {}
		for doctype in DISCARDABLE:
			if installed(doctype):
				out[doctype] = purge(doctype, user="Administrator", trigger="Auto", older_than=cutoff)
		return out
	except Exception:
		frappe.log_error(
			title="erpnext_mcp: auto-purge of discarded drafts failed", message=compat.traceback_text()
		)
		return None


# ── MCP tools ────────────────────────────────────────────────────────────────


def discard_draft_journal_entries(args: dict) -> ToolResult:
	"""Set draft Journal Entries aside, recoverably. One result per name."""
	results = discard(
		JOURNAL_ENTRY,
		args.get("names") if args.get("names") not in (None, "") else as_str(args, "name"),
		as_str(args, "reason"),
		as_str(args, "duplicate_of"),
	)
	done = [row for row in results if row["ok"] and not row["skipped"]]
	failed = [row for row in results if not row["ok"]]
	return ToolResult(
		{
			"total": len(results),
			"discarded": len(done),
			"already_discarded": sum(1 for row in results if row["skipped"]),
			"refused": len(failed),
			"results": results,
			"note": (
				"Nothing was deleted: each draft is still there (docstatus 0), out of bulk submit, the draft "
				"counts and the default Desk list. restore_discarded_draft puts one back; Purge in the Desk "
				"(System Manager / Accounts Manager) deletes discarded drafts for good, logging each first."
			),
		},
		f"discarded {len(done)} draft Journal Entr{'y' if len(done) == 1 else 'ies'}, "
		f"{len(failed)} refused, of {len(results)}",
		docstatus_delta="0 → 0 (discarded)",
	)


def restore_discarded_draft(args: dict) -> ToolResult:
	"""Put one discarded draft back exactly as it was."""
	name = as_str(args, "name", required=True)
	data = restore(as_str(args, "doctype") or JOURNAL_ENTRY, name)
	return ToolResult(data, f"restored discarded draft {name}", docstatus_delta="0 → 0 (restored)")


def list_discarded_drafts(args: dict) -> ToolResult:
	"""What has been set aside, with why, by whom, and the amount; plus what was purged."""
	doctype = as_str(args, "doctype") or JOURNAL_ENTRY
	_spec(doctype)
	company = as_str(args, "company")
	rows = discarded_rows(doctype, company, limit=as_limit(args))
	data = {
		"doctype": doctype,
		"discarded": rows,
		"count": len(rows),
		"total": round(sum(row["amount"] for row in rows), 2),
		"fields_installed": installed(doctype),
	}
	if as_bool(args, "include_purged", False):
		filters = {"document_type": doctype}
		if company:
			filters["company"] = company
		data["purged"] = (
			frappe.db.get_all(
				LOG,
				filters=filters,
				fields=compat.existing_fields(
					LOG,
					[
						"name",
						"document_name",
						"company",
						"amount",
						"discard_reason",
						"duplicate_of",
						"purged_by",
						"purged_at",
						"purge_trigger",
						"allow_recreate",
					],
				),
				order_by="purged_at desc",
				limit=as_limit(args),
			)
			if compat.doctype_exists(LOG)
			else []
		)
	return ToolResult(data, f"{len(rows)} discarded draft(s), {data['total']} in total")
