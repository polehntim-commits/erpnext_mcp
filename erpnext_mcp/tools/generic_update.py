# SPDX-License-Identifier: MIT
"""`update_document` — write whitelisted fields on any draft document.

WHY A GENERIC WRITER EXISTS BESIDE SEVENTY-SIX NAMED ONES. The named update tools
carry domain rules — a curriculum refuses a URL a phone cannot open, a journal
entry's party refuses an account that takes none — and they stay. But more than
fifty registers have a create tool and no update tool at all, so a typo on a
Training Session or a Farm Task could only be fixed at the Desk. This is the
door for those, and it is deliberately narrow:

  1. **THE OPERATOR NAMES EVERY FIELD.** `update_document_fields` on ERPNext MCP
     Settings lists (DocType, field) pairs. A field that is not on it, or is on it
     unticked, is refused — and one refused field refuses the whole call, so a
     caller never gets half of what it asked for and a reply that reads as done.

  2. **SOME THINGS ARE REFUSED WHATEVER THE TABLE SAYS.** Password fields, the
     framework's own columns, child tables and layout fields; submitted and
     cancelled documents; and the stores `query_doctype` already refuses to read,
     plus this app's own audit log and whitelist — a writer that could edit its
     own permission table would be a switch that turns every other switch on.

  3. **IT GOES THROUGH THE DOCUMENT'S OWN SAVE.** One `get_doc`, every field set,
     one `save()` — which is what `frappe.client.set_value` does with a dict, and
     it means the doctype's validation, link checks, `set_only_once` and hooks all
     run exactly as they do for a human at the Desk. One save rather than one per
     field, so the write is all-or-nothing.
"""

import json

import frappe

from .. import compat, settings
from ..args import as_str
from ..errors import ToolError
from ..result import ToolResult
from .diagnostics import REFUSED_DOCTYPES

#: The Table on ERPNext MCP Settings that holds the whitelist.
WHITELIST_FIELD = "update_document_fields"
WHITELIST_DOCTYPE = "MCP Update Document Field"

#: Framework columns. Never writable: changing any of them through a field write
#: either corrupts the record's identity (name, doctype, parent*), forges its
#: history (owner, creation, modified*), or skips the submit/cancel workflow
#: (docstatus, amended_from).
SYSTEM_FIELDS = frozenset(
	{
		"name",
		"docstatus",
		"creation",
		"modified",
		"modified_by",
		"owner",
		"idx",
		"doctype",
		"parent",
		"parentfield",
		"parenttype",
		"amended_from",
	}
)

#: Fieldtypes refused on any doctype. Password is encrypted at rest and is a
#: credential; Table fields replace every child row and bypass the per-row
#: whitelist; the rest hold no data at all.
REFUSED_FIELDTYPES = {
	"Password": "it is a Password field",
	"Table": "it is a child table — write its rows through a dedicated tool",
	"Table MultiSelect": "it is a child table — write its rows through a dedicated tool",
	"Section Break": "it is a layout field and holds no value",
	"Column Break": "it is a layout field and holds no value",
	"Tab Break": "it is a layout field and holds no value",
	"HTML": "it is a display field and holds no value",
	"Button": "it is a display field and holds no value",
	"Heading": "it is a display field and holds no value",
	"Fold": "it is a layout field and holds no value",
}

#: On top of `query_doctype`'s register: the audit trail and the whitelist itself.
_ALSO_REFUSED = {
	"MCP Action Log": "it is this endpoint's audit trail, and an audit trail its subject can edit is not one.",
	WHITELIST_DOCTYPE: "it is update_document's own whitelist; widening it is an operator's decision, made at the Desk.",
}


def _refusal_reason(doctype: str) -> str:
	return _ALSO_REFUSED.get(doctype) or REFUSED_DOCTYPES.get(doctype) or ""


def whitelisted_fields(doctype: str) -> set[str]:
	"""The fieldnames an operator has ticked for `doctype`. Empty when none."""
	rows = settings.get_settings().get(WHITELIST_FIELD) or []
	allowed = set()
	for row in rows:
		if str(row.get("doctype_name") or "").strip() != doctype:
			continue
		if not settings.as_bool(row.get("enabled")):
			continue
		field = str(row.get("field_name") or "").strip()
		if field:
			allowed.add(field)
	return allowed


def _updates(args: dict) -> dict:
	raw = args.get("updates")
	if isinstance(raw, str) and raw.strip():
		try:
			raw = json.loads(raw)
		except ValueError:
			raise ToolError(
				"`updates` must be an object of fieldname → value. Nothing was changed."
			) from None
	if not isinstance(raw, dict) or not raw:
		raise ToolError(
			"`updates` must be a non-empty object of fieldname → value, e.g. "
			'{"expires_date": "2027-03-01"}. Nothing was changed.'
		)
	return raw


def _field_refusal(doctype: str, fieldname: str, value, allowed: set[str]) -> str:
	"""Why this one field may not be written, or "" if it may."""
	if not isinstance(fieldname, str) or not fieldname.strip():
		return "an empty fieldname"
	if fieldname in SYSTEM_FIELDS:
		return "it is a system field"
	df = compat.field_meta(doctype, fieldname)
	if df is None:
		return f"{doctype} has no field called {fieldname!r} (use the fieldname, not the label)"
	why = REFUSED_FIELDTYPES.get(str(getattr(df, "fieldtype", "") or ""))
	if why:
		return why
	if fieldname not in allowed:
		return f"it is not whitelisted for {doctype} in ERPNext MCP Settings → Updatable Fields"
	if isinstance(value, (dict, list, tuple)):
		return "the value is an object or list; only a single value can be written to a field"
	return ""


def update_document(args: dict) -> ToolResult:
	"""MUTATING. Set whitelisted fields on one draft document, in one save."""
	# The registry refuses before this is reached when the switch is off; asked
	# again here so a direct import cannot skip it.
	if not settings.tool_enabled("update_document"):
		raise ToolError(
			"update_document is switched off. An operator must tick 'allow_update_document' "
			"in ERPNext MCP Settings to enable it. Nothing was changed."
		)

	doctype = as_str(args, "doctype", required=True).strip()
	docname = as_str(args, "docname", required=True).strip()
	updates = _updates(args)

	why = _refusal_reason(doctype)
	if why:
		raise ToolError(
			f"{doctype} cannot be written through update_document: {why} This does not "
			"depend on the whitelist. Nothing was changed."
		)
	if not compat.doctype_exists(doctype):
		raise ToolError(
			f"no DocType called {doctype!r} on this site. The name is the doctype's LABEL as "
			"the Desk shows it — 'Farm Task', not 'farm_task'. Nothing was changed."
		)
	if int(frappe.db.get_value("DocType", doctype, "istable") or 0):
		raise ToolError(
			f"{doctype} is a child table. Its rows belong to their parent document and are "
			"validated with it; update the parent instead. Nothing was changed."
		)

	allowed = whitelisted_fields(doctype)
	rejected = {}
	for fieldname, value in updates.items():
		reason = _field_refusal(doctype, fieldname, value, allowed)
		if reason:
			rejected[str(fieldname)] = reason
	if rejected:
		detail = "; ".join(f"{field}: {reason}" for field, reason in rejected.items())
		note = "" if allowed else f" No field on {doctype} is whitelisted at all."
		raise ToolError(
			f"update_document refused {len(rejected)} of {len(updates)} field(s) on {doctype} "
			f"— {detail}.{note} Nothing was changed; one refused field refuses the whole call."
		)

	if not frappe.db.exists(doctype, docname):
		raise ToolError(f"no {doctype} called {docname!r}. Nothing was changed.")
	doc = frappe.get_doc(doctype, docname)

	docstatus = int(doc.get("docstatus") or 0)
	if docstatus == 1:
		raise ToolError(
			f"{doctype} {docname} is submitted. A submitted document is a posted record; "
			"cancel and amend it, or use the dedicated tool for that register. Nothing was changed."
		)
	if docstatus == 2:
		raise ToolError(f"{doctype} {docname} is cancelled and cannot be edited. Nothing was changed.")

	if not frappe.has_permission(doctype, "write", doc):
		raise ToolError(
			f"this account may not write {doctype} {docname}. The account is the one configured "
			"as `mcp_system_user` on ERPNext MCP Settings; the whitelist cannot widen its "
			"DocPerms. Nothing was changed."
		)

	changed: dict = {}
	unchanged: list = []
	for fieldname, value in updates.items():
		if isinstance(value, bool):
			value = int(value)
		before = doc.get(fieldname)
		if str(before if before is not None else "") == str(value if value is not None else ""):
			unchanged.append(fieldname)
			continue
		changed[fieldname] = {"from": before}
		doc.set(fieldname, value)

	if changed:
		try:
			doc.save()
		except frappe.PermissionError as exc:
			raise ToolError(
				f"this account may not write {doctype} {docname}: {exc}. Nothing was changed."
			) from exc
		# Read back what the save kept: validation may normalise a value.
		for fieldname in changed:
			changed[fieldname]["to"] = doc.get(fieldname)

	summary = (
		f"updated {', '.join(changed)} on {doctype} {docname}"
		if changed
		else f"no change to {doctype} {docname}: every value already matched"
	)
	return ToolResult(
		data={
			"doctype": doctype,
			"docname": docname,
			"updated": changed,
			"unchanged": unchanged,
			"modified": str(doc.get("modified") or "") or None,
			"acting_user": str(getattr(frappe.session, "user", "") or "") or None,
		},
		summary=summary,
	)
