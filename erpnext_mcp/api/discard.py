# SPDX-License-Identifier: MIT
"""The Desk's Discard / Restore / Purge buttons on Journal Entry. v0.334.0.

Called by the two Client Scripts in `journal_entry_discard_action.py`. Each method
acts as the signed-in Desk user: discarding and restoring need write access to the
draft; purging needs System Manager or Accounts Manager (checked in
`tools/discard.purge`). The work itself is `tools/discard.py`, shared with MCP.
"""

import json

import frappe

from ..errors import ToolError
from ..tools import discard as discard_tools


def _doctype(doctype) -> str:
	doctype = str(doctype or discard_tools.JOURNAL_ENTRY)
	if doctype not in discard_tools.DISCARDABLE:
		frappe.throw(f"{doctype} drafts cannot be discarded.", frappe.ValidationError)
	return doctype


def _names(names) -> list:
	if isinstance(names, str):
		try:
			names = json.loads(names)
		except ValueError:
			names = [part.strip() for part in names.split(",") if part.strip()]
	return [str(name) for name in (names or []) if str(name or "").strip()]


def _run(fn, *args, **kwargs):
	try:
		return fn(*args, **kwargs)
	except ToolError as exc:
		frappe.throw(str(exc), frappe.ValidationError)


@frappe.whitelist(methods=["GET", "POST"])
def summary(doctype=None, company=None):
	doctype = _doctype(doctype)
	if not frappe.has_permission(doctype, "read"):
		raise frappe.PermissionError(f"no read access to {doctype}")
	return discard_tools.summary(doctype, company or "")


@frappe.whitelist(methods=["POST"])
def discard(doctype=None, names=None, reason=None, duplicate_of=None):
	doctype = _doctype(doctype)
	names = _names(names)
	for name in names:
		if frappe.db.exists(doctype, name) and not frappe.has_permission(doctype, "write", name):
			raise frappe.PermissionError(f"no write access to {doctype} {name}")
	results = _run(discard_tools.discard, doctype, names, reason, duplicate_of or "", frappe.session.user)
	return {
		"results": results,
		"discarded": sum(1 for row in results if row["ok"] and not row["skipped"]),
	}


@frappe.whitelist(methods=["POST"])
def restore(doctype=None, name=None):
	doctype = _doctype(doctype)
	if not frappe.has_permission(doctype, "write", name):
		raise frappe.PermissionError(f"no write access to {doctype} {name}")
	return _run(discard_tools.restore, doctype, str(name or ""), frappe.session.user)


@frappe.whitelist(methods=["POST"])
def purge(doctype=None, names=None, company=None):
	doctype = _doctype(doctype)
	return _run(discard_tools.purge, doctype, _names(names) or None, company or "", frappe.session.user)
