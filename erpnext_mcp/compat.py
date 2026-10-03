# SPDX-License-Identifier: MIT
"""Version tolerance: ask the site what it has instead of assuming.

This app has to work on whatever ERPNext the operator is running, and ERPNext
renames and reshapes fields between majors. Two examples this app actually
depends on:

  * Bank Transaction carried a single signed `amount` in older versions and
    split into `deposit` / `withdrawal` later, with `unallocated_amount` added
    to track reconciliation progress.
  * Company's default cost centre has been `cost_center` and `default_cost_center`
    at different points, and is absent on a Company created before the Chart of
    Accounts was built.

Rather than pin a version, every tool reads through the helpers here, which
consult `frappe.get_meta()` — the site's own schema — and degrade to a clearly
labelled null instead of raising. A field this app cannot find is reported as
absent in the response, so the model sees "this install does not track that"
rather than a traceback.

`frappe.get_meta` is cached per request by the framework, so `has_field` in a
loop is cheap.
"""

import frappe

from .errors import ToolError


def doctype_exists(doctype: str) -> bool:
	"""Whether `doctype` is installed on this site.

	Used for the genuinely optional doctypes (Bank Statement shipped later than
	Bank Transaction) so a tool can degrade politely instead of exploding.
	"""
	return bool(frappe.db.exists("DocType", doctype))


def require_doctype(doctype: str, hint: str = "") -> None:
	"""Raise a ToolError naming the missing doctype, with an actionable hint."""
	if not doctype_exists(doctype):
		suffix = f" {hint}" if hint else ""
		raise ToolError(f"the {doctype!r} DocType is not installed on this site.{suffix}")


def has_field(doctype: str, fieldname: str) -> bool:
	"""Whether `doctype` has `fieldname` on this site's schema."""
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return False
	if fieldname in {"name", "owner", "creation", "modified", "docstatus", "idx"}:
		return True
	return bool(meta.has_field(fieldname))


def field_meta(doctype: str, fieldname: str):
	"""One field's definition off this site's meta, or None.

	`has_field` answers "is it there"; this answers "what is it", which is what a
	tool needs before writing a value into a field it did not declare. The
	accounting-dimension path uses it to check that a per-line dimension value is
	a real record of the doctype the dimension links to, rather than a string that
	will fail ERPNext's own link validation several layers down.
	"""
	try:
		return frappe.get_meta(doctype).get_field(fieldname)
	except Exception:
		return None


def first_field(doctype: str, *candidates: str) -> str | None:
	"""The first of `candidates` that exists on `doctype`, else None.

	This is how a tool spells "whichever name this version uses" without
	branching on a version number.
	"""
	for name in candidates:
		if has_field(doctype, name):
			return name
	return None


def existing_fields(doctype: str, candidates) -> list[str]:
	"""Filter `candidates` down to the fields this site actually has.

	Pass the result straight to `frappe.db.get_all(fields=...)`: selecting a
	column that does not exist is a hard SQL error, and the point of this app
	is to not do that on somebody else's install.
	"""
	return [f for f in candidates if has_field(doctype, f)]


#: Field types that are child tables: the parent's table has no column for them.
TABLE_FIELDTYPES = frozenset({"Table", "Table MultiSelect"})


def column_fields(doctype: str, candidates) -> list[str]:
	"""`existing_fields`, minus child-table fields. v0.223.2.

	For `frappe.db.get_all(fields=...)`. A Table or Table MultiSelect field IS on
	the meta — so `existing_fields` keeps it — but it is a child doctype with no
	column on the parent's table, and selecting it is `OperationalError 1054` on
	every site. Read such a field from its child doctype instead.
	"""
	out = []
	for fieldname in existing_fields(doctype, candidates):
		field = field_meta(doctype, fieldname)
		if (
			field is not None
			and (field.get("fieldtype") if hasattr(field, "get") else getattr(field, "fieldtype", ""))
			in TABLE_FIELDTYPES
		):
			continue
		out.append(fieldname)
	return out


def checked(value) -> bool:
	"""Whether a Check field is ticked. NEVER use a bare `bool()` for this.

	`bool("0")` is True. A Check field does not always come back as an integer:
	`frappe.new_doc` copies the DocType's declared default onto the document
	verbatim, and in the DocType JSON that default is the *string* `"0"` — so a
	document read back before it has been through the database layer carries a
	string that Python calls true. A tool describing that document with `bool()`
	reports every unticked box as ticked.

	That is not hypothetical. It is the same failure `settings.as_bool` exists to
	prevent for the tool switches, where reading every disabled tool as enabled
	would be about the worst possible direction for a bug to fail in. Here the
	direction is only slightly better: it would report a block with no worker
	hygiene station as having one, and a housing unit outside the Produce Safety
	Rule as inside it. Both are answers somebody would act on.

	`settings.as_bool` is the same judgement for the settings Single; this is it
	for ordinary documents, and the two are deliberately identical in behaviour.
	"""
	if value is None or isinstance(value, bool):
		return bool(value)
	if isinstance(value, (int, float)):
		return bool(value)
	return str(value).strip().lower() not in ("", "0", "false", "no", "none")


def bank_transaction_amount_fields() -> dict:
	"""Describe how *this* site's Bank Transaction stores money.

	Returns the fieldnames to read, so callers never hardcode either layout:

	  {"style": "deposit_withdrawal" | "signed_amount",
	   "deposit": "deposit" | None, "withdrawal": "withdrawal" | None,
	   "amount": "amount" | None, "unallocated": "unallocated_amount" | None,
	   "allocated": "allocated_amount" | None}
	"""
	deposit = first_field("Bank Transaction", "deposit")
	withdrawal = first_field("Bank Transaction", "withdrawal")
	return {
		"style": "deposit_withdrawal" if (deposit and withdrawal) else "signed_amount",
		"deposit": deposit,
		"withdrawal": withdrawal,
		"amount": first_field("Bank Transaction", "amount"),
		"unallocated": first_field("Bank Transaction", "unallocated_amount"),
		"allocated": first_field("Bank Transaction", "allocated_amount"),
	}


def signed_amount(row: dict, fields: dict) -> float:
	"""Collapse either Bank Transaction layout to one signed number.

	Positive is money in, negative is money out — the convention every tool in
	this app reports, whichever way the site stores it.
	"""
	if fields["style"] == "deposit_withdrawal":
		return float(row.get(fields["deposit"]) or 0) - float(row.get(fields["withdrawal"]) or 0)
	return float(row.get(fields["amount"]) or 0)


def gross_amount(row: dict, fields: dict) -> float:
	"""The absolute size of a Bank Transaction, for comparing against an
	allocated amount (which ERPNext always stores unsigned)."""
	return abs(signed_amount(row, fields))


def traceback_text() -> str:
	"""`frappe.get_traceback()`, with context where the version supports it.

	`with_context=True` (which adds the request's form data and headers to the
	Error Log entry, and is what makes an MCP traceback actually diagnosable) was
	added mid-v14. Passing it to an older Frappe raises TypeError — from inside an
	`except` block, which would turn a handled failure into an unhandled one. So:
	try, and fall back.
	"""
	try:
		return frappe.get_traceback(with_context=True)
	except TypeError:
		return frappe.get_traceback()


def company_default_cost_center(company: str) -> str | None:
	"""This site's default Cost Center for `company`, under whichever fieldname.

	None when the Company predates its Chart of Accounts, which is a legitimate
	state on a half-set-up site rather than an error.
	"""
	field = first_field("Company", "cost_center", "default_cost_center")
	if not field:
		return None
	return frappe.db.get_value("Company", company, field) or None
