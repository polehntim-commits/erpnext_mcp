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

import datetime
import difflib
import json
import math
import re

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


#: Fieldtypes whose value is text. A number sent to one is written as its text;
#: anything else that is not a string is refused.
TEXT_TYPES = frozenset(
	{
		"Data",
		"Small Text",
		"Text",
		"Long Text",
		"Text Editor",
		"Code",
		"Markdown Editor",
		"HTML Editor",
		"Read Only",
		"Phone",
		"Autocomplete",
		"Barcode",
		"Color",
		"Attach",
		"Attach Image",
		"Signature",
		"Geolocation",
		"JSON",
		"Icon",
	}
)

#: Frappe stores a Data field in a varchar(140) unless the field sets a length.
DATA_LENGTH = 140

_INT = re.compile(r"^[+-]?\d+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?$")

_EXPECTED = {
	"Int": "a whole number, e.g. 42",
	"Duration": "a whole number of seconds, e.g. 3600",
	"Float": "a number, e.g. 12.5",
	"Currency": "a plain number with no currency symbol or thousands separator, e.g. 1200.50",
	"Percent": "a number, e.g. 12.5 for 12.5%",
	"Rating": "a number from 0 to 1, e.g. 0.8",
	"Check": "true/false or 1/0",
	"Date": "a date as YYYY-MM-DD, e.g. 2026-10-28",
	"Datetime": "a date and time as YYYY-MM-DD HH:MM:SS, e.g. 2026-10-28 07:30:00",
	"Time": "a time as HH:MM or HH:MM:SS, e.g. 07:30",
}


class _Bad(Exception):
	"""One value that does not fit its field. Carries what was expected."""

	def __init__(self, reason: str, expected: str = "", **extra):
		super().__init__(reason)
		self.reason = reason
		self.expected = expected
		self.extra = extra


def _attr(df, key: str, default=None):
	value = getattr(df, key, None)
	if value is None and hasattr(df, "get"):
		value = df.get(key)
	return default if value is None else value


def _select_options(df) -> list[str]:
	return [option for option in str(_attr(df, "options", "") or "").split("\n") if option != ""]


def field_info(df) -> dict:
	"""What a field expects, in the shape an error or a reply echoes back."""
	fieldtype = str(_attr(df, "fieldtype", "") or "")
	info = {
		"fieldname": _attr(df, "fieldname"),
		"label": _attr(df, "label") or None,
		"fieldtype": fieldtype,
		"reqd": bool(int(_attr(df, "reqd", 0) or 0)),
	}
	options = _attr(df, "options", "") or ""
	if fieldtype == "Select":
		info["options"] = _select_options(df)
	elif fieldtype == "Link":
		info["options"] = options
		info["links_to"] = options
	elif fieldtype == "Dynamic Link":
		info["options"] = options
		info["doctype_from_field"] = options
	elif options:
		info["options"] = options
	if int(_attr(df, "read_only", 0) or 0):
		info["read_only"] = True
	if _attr(df, "fetch_from"):
		info["fetch_from"] = _attr(df, "fetch_from")
	return info


def _suggest_fields(doctype: str, fieldname: str) -> list[str]:
	"""Fieldnames close to what was sent, matched on fieldname and on label."""
	try:
		fields = list(frappe.get_meta(doctype).fields)
	except Exception:
		return []
	by_key = {}
	for df in fields:
		name = _attr(df, "fieldname")
		if not name or str(_attr(df, "fieldtype", "")) in REFUSED_FIELDTYPES:
			continue
		by_key[str(name).lower()] = name
		label = _attr(df, "label")
		if label:
			by_key[str(label).lower()] = name
	wanted = str(fieldname).lower().strip()
	matches = difflib.get_close_matches(wanted, list(by_key), n=3, cutoff=0.6)
	out = []
	for match in matches:
		if by_key[match] not in out:
			out.append(by_key[match])
	return out


def _number(value, fieldtype: str) -> float:
	if isinstance(value, bool):
		raise _Bad(
			f"got {json.dumps(value)}, which is a true/false and this is a {fieldtype} field",
			_EXPECTED[fieldtype],
		)
	if isinstance(value, (int, float)):
		number = float(value)
	elif isinstance(value, str):
		text = value.strip()
		try:
			number = float(text)
		except ValueError:
			hint = " Drop the thousands separator." if "," in text else ""
			hint += " Drop the currency symbol." if text[:1] in "$€£" else ""
			raise _Bad(f"{value!r} is not a number.{hint}", _EXPECTED[fieldtype]) from None
	else:
		raise _Bad(f"got a {type(value).__name__}", _EXPECTED[fieldtype])
	if math.isnan(number) or math.isinf(number):
		raise _Bad(f"{value!r} is not a finite number", _EXPECTED[fieldtype])
	return number


def _coerce(doctype: str, docname: str, df, value, updates: dict):
	"""The value to write for this field, or raise _Bad saying why it cannot be."""
	fieldtype = str(_attr(df, "fieldtype", "") or "")
	fieldname = _attr(df, "fieldname")

	if value is None or (isinstance(value, str) and value.strip() == ""):
		if int(_attr(df, "reqd", 0) or 0):
			raise _Bad(
				f"{fieldname} is mandatory on {doctype} and cannot be cleared",
				_EXPECTED.get(fieldtype, "a non-empty value"),
			)
		return None

	if fieldtype in ("Int", "Duration"):
		number = _number(value, fieldtype)
		if not number.is_integer():
			raise _Bad(f"{value!r} is not a whole number", _EXPECTED[fieldtype])
		if fieldtype == "Duration" and number < 0:
			raise _Bad(f"{value!r} is negative", _EXPECTED[fieldtype])
		return int(number)

	if fieldtype in ("Float", "Currency", "Percent", "Rating"):
		number = _number(value, fieldtype)
		if fieldtype == "Rating" and not 0 <= number <= 1:
			raise _Bad(f"{value!r} is outside 0 to 1", _EXPECTED[fieldtype])
		return int(number) if isinstance(value, int) else number

	if fieldtype == "Check":
		if isinstance(value, bool):
			return int(value)
		if isinstance(value, (int, float)) and value in (0, 1):
			return int(value)
		if isinstance(value, str) and value.strip().lower() in ("1", "0", "true", "false", "yes", "no"):
			return 1 if value.strip().lower() in ("1", "true", "yes") else 0
		raise _Bad(f"{value!r} is not a yes/no value", _EXPECTED["Check"])

	if fieldtype == "Date":
		text = str(value).strip() if isinstance(value, str) else None
		if text is None or not _DATE.match(text):
			raise _Bad(f"{value!r} is not in YYYY-MM-DD form", _EXPECTED["Date"])
		try:
			datetime.date.fromisoformat(text)
		except ValueError:
			raise _Bad(f"{value!r} is not a real calendar date", _EXPECTED["Date"]) from None
		return text

	if fieldtype == "Datetime":
		match = _DATETIME.match(value.strip()) if isinstance(value, str) else None
		if not match:
			raise _Bad(f"{value!r} is not in YYYY-MM-DD HH:MM:SS form", _EXPECTED["Datetime"])
		text = f"{match.group(1)} {match.group(2)}"
		try:
			datetime.datetime.fromisoformat(text)
		except ValueError:
			raise _Bad(f"{value!r} is not a real date and time", _EXPECTED["Datetime"]) from None
		return text if len(match.group(2)) > 5 else text + ":00"

	if fieldtype == "Time":
		match = _TIME.match(value.strip()) if isinstance(value, str) else None
		if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59 or int(match.group(3) or 0) > 59:
			raise _Bad(f"{value!r} is not a time of day", _EXPECTED["Time"])
		return value.strip() if match.group(3) else value.strip() + ":00"

	if fieldtype in ("Link", "Dynamic Link"):
		if isinstance(value, bool) or not isinstance(value, (str, int)):
			raise _Bad(f"got a {type(value).__name__}", "the name of the linked document, as text")
		value = str(value).strip()
		if fieldtype == "Link":
			target = str(_attr(df, "options", "") or "")
		else:
			source = str(_attr(df, "options", "") or "")
			target = str(updates.get(source) or frappe.db.get_value(doctype, docname, source) or "")
			if not target:
				raise _Bad(
					f"{fieldname} is a Dynamic Link whose doctype is read from {source!r}, and "
					f"{source!r} is empty on this document. Send {source!r} in the same call",
					f"{source} set to a DocType name, and {fieldname} set to a document of it",
				)
		if target and not frappe.db.exists(target, value):
			raise _Bad(
				f"linked document {value!r} does not exist in doctype {target!r}",
				f"the name of an existing {target}",
				links_to=target,
				did_you_mean=_suggest_names(target, value),
			)
		return value

	if fieldtype == "Select":
		options = _select_options(df)
		if not isinstance(value, (str, int, float)) or isinstance(value, bool):
			raise _Bad(f"got a {type(value).__name__}", f"one of: {', '.join(options)}")
		text = str(value)
		if options and text not in options:
			folded = [option for option in options if option.strip().lower() == text.strip().lower()]
			raise _Bad(
				f"{text!r} is not one of the options",
				f"one of: {', '.join(options)}",
				did_you_mean=folded,
			)
		return text

	if fieldtype in TEXT_TYPES:
		if isinstance(value, bool):
			raise _Bad("got a true/false", "text")
		if isinstance(value, (int, float)):
			value = str(value)
		if not isinstance(value, str):
			raise _Bad(f"got a {type(value).__name__}", "text")
		# A DocField `length` of 0 is Frappe's "not set", which for Data means the
		# varchar(140) default — so here zero really is the absent case.
		declared = _attr(df, "length")
		if declared not in (None, "", 0, "0"):
			limit = int(declared)
		else:
			limit = DATA_LENGTH if fieldtype == "Data" else 0
		if limit and len(value) > limit:
			raise _Bad(f"is {len(value)} characters long", f"text of at most {limit} characters")
		return value

	return value


def _suggest_names(target: str, value: str) -> list[str]:
	"""Best effort: documents of `target` whose name matches `value` ignoring case."""
	try:
		rows = frappe.db.get_all(target, filters={"name": ["like", f"%{value}%"]}, pluck="name", limit=5)
	except Exception:
		return []
	lowered = value.lower()
	rows = [str(row) for row in rows or []]
	return sorted(rows, key=lambda name: (name.lower() != lowered, len(name)))[:3]


def _structural_refusal(doctype: str, fieldname, value, allowed: set[str], df) -> str:
	"""Why this field may not be written at all, whatever its value, or ""."""
	if not isinstance(fieldname, str) or not fieldname.strip():
		return "an empty fieldname"
	if fieldname in SYSTEM_FIELDS:
		return "it is a system field"
	if df is None:
		return f"{doctype} has no field called {fieldname!r} (use the fieldname, not the label)"
	why = REFUSED_FIELDTYPES.get(str(_attr(df, "fieldtype", "") or ""))
	if why:
		return why
	if fieldname not in allowed:
		return f"it is not whitelisted for {doctype} in ERPNext MCP Settings → Updatable Fields"
	if isinstance(value, (dict, list, tuple)):
		return "the value is an object or list; only a single value can be written to a field"
	return ""


def _refuse_fields(
	doctype: str, docname: str, updates: dict, rejected: dict, accepted: dict, allowed: set
) -> None:
	lines = []
	for fieldname, problem in rejected.items():
		line = f"- {fieldname}: {problem['reason']}."
		if problem.get("expected"):
			line += f" Expected {problem['expected']}."
		info = problem.get("field")
		if info:
			line += f" [{info['fieldtype']}"
			if info.get("links_to"):
				line += f" → {info['links_to']}"
			line += "]"
		if problem.get("did_you_mean"):
			line += f" Did you mean: {', '.join(repr(x) for x in problem['did_you_mean'])}?"
		lines.append(line)
	note = f"\nNo field on {doctype} is whitelisted at all." if not allowed else ""
	details = {
		"doctype": doctype,
		"docname": docname,
		"rejected": rejected,
		"accepted": accepted,
	}
	raise ToolError(
		f"update_document refused {len(rejected)} of {len(updates)} field(s) on {doctype} {docname}. "
		"Nothing was changed; one refused field refuses the whole call.\n"
		+ "\n".join(lines)
		+ note
		+ "\n\nDetails (JSON): "
		+ json.dumps(details, default=str, ensure_ascii=False)
	)


def _same(before, after) -> bool:
	if before in (None, "") and after in (None, ""):
		return True
	if any(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (before, after)):
		try:
			return float(before) == float(after)
		except (TypeError, ValueError):
			pass
	return str(before if before is not None else "") == str(after if after is not None else "")


def _plain(value):
	"""A stored value as JSON can carry it: dates and decimals as text."""
	if value is None or isinstance(value, (str, int, float, bool)):
		return value
	return str(value)


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

	# EVERY FIELD IS CHECKED BEFORE ANY IS REFUSED, so one reply names every
	# problem in the call — a caller fixing them one round trip at a time is the
	# failure this shape exists to prevent.
	allowed = whitelisted_fields(doctype)
	rejected: dict = {}
	accepted: dict = {}
	values: dict = {}
	for fieldname, value in updates.items():
		df = compat.field_meta(doctype, fieldname) if isinstance(fieldname, str) else None
		info = field_info(df) if df is not None else None
		reason = _structural_refusal(doctype, fieldname, value, allowed, df)
		if reason:
			problem = {"sent": value, "reason": reason}
			if info:
				problem["field"] = info
			elif isinstance(fieldname, str) and fieldname not in SYSTEM_FIELDS:
				problem["did_you_mean"] = _suggest_fields(doctype, fieldname)
			rejected[str(fieldname)] = problem
			continue
		try:
			values[fieldname] = _coerce(doctype, docname, df, value, updates)
		except _Bad as bad:
			problem = {"sent": value, "reason": bad.reason, "expected": bad.expected, "field": info}
			problem.update({key: val for key, val in bad.extra.items() if val})
			rejected[fieldname] = problem
			continue
		accepted[fieldname] = info
	if rejected:
		_refuse_fields(doctype, docname, updates, rejected, accepted, allowed)

	if not frappe.has_permission(doctype, "write", doc):
		raise ToolError(
			f"this account may not write {doctype} {docname}. The account is the one configured "
			"as `mcp_system_user` on ERPNext MCP Settings; the whitelist cannot widen its "
			"DocPerms. Nothing was changed."
		)

	changed: dict = {}
	unchanged: dict = {}
	for fieldname, value in values.items():
		before = doc.get(fieldname)
		if _same(before, value):
			unchanged[fieldname] = {"value": _plain(before), **_brief(accepted[fieldname])}
			continue
		changed[fieldname] = {
			"from": _plain(before),
			"sent": updates[fieldname],
			**_brief(accepted[fieldname]),
		}
		doc.set(fieldname, value)

	warnings = []
	if changed:
		try:
			doc.save()
		except frappe.PermissionError as exc:
			raise ToolError(
				f"this account may not write {doctype} {docname}: {exc}. Nothing was changed."
			) from exc
		except frappe.ValidationError as exc:
			raise ToolError(
				f"{doctype} {docname} refused the save: {type(exc).__name__}: {exc}. Every value "
				"passed update_document's own checks, so this is the document's own validation "
				"— usually a rule linking two fields, or a mandatory field elsewhere on the "
				"record that is already empty. Nothing was changed.\n\nDetails (JSON): "
				+ json.dumps(
					{"doctype": doctype, "docname": docname, "attempted": changed},
					default=str,
					ensure_ascii=False,
				)
			) from exc
		# Read back what the save kept: validation may normalise or overwrite a value.
		for fieldname, entry in changed.items():
			after = doc.get(fieldname)
			entry["to"] = _plain(after)
			entry["took_effect"] = _same(after, values[fieldname])
			if not entry["took_effect"]:
				warnings.append(
					f"{fieldname}: sent {updates[fieldname]!r} but the saved value is {_plain(after)!r} "
					"— the document's own save recomputed it (a fetched, computed or read-only field)."
				)

	summary = (
		f"updated {', '.join(changed)} on {doctype} {docname}"
		if changed
		else f"no change to {doctype} {docname}: every value already matched"
	)
	data = {
		"doctype": doctype,
		"docname": docname,
		"updated": changed,
		"unchanged": unchanged,
		"modified": _plain(doc.get("modified")) or None,
		"acting_user": str(getattr(frappe.session, "user", "") or "") or None,
	}
	if warnings:
		data["warnings"] = warnings
	return ToolResult(data=data, summary=summary)


def _brief(info: dict) -> dict:
	"""The part of a field's metadata a success reply echoes beside its value."""
	out = {"fieldtype": info["fieldtype"], "label": info.get("label")}
	if info.get("options"):
		out["options"] = info["options"]
	return out


# ---------------------------------------------------------------------------
# manage_updatable_fields — the whitelist above, managed over MCP.
#
# `update_document` still refuses to write MCP Update Document Field, and must:
# a generic writer that could widen its own whitelist would be a switch that
# turns every other switch on. This tool is the deliberate exception, and it is
# fenced three ways instead:
#
#   1. ITS OWN SWITCH. `allow_manage_updatable_fields` ships off, and turning it
#      on is a Desk decision on the same settings form — so an operator still
#      decides whether the whitelist can be widened remotely at all.
#   2. IT CANNOT WIDEN THE FENCES. An entry `update_document` would refuse
#      whatever the table said — a system column, a Password or Table field, a
#      child DocType, a credential store, this whitelist — is refused here too,
#      so the table only ever holds rows that mean something.
#   3. IT WRITES THROUGH THE SETTINGS DOCUMENT'S OWN SAVE, under the MCP user's
#      write permission on it, exactly as a Desk edit of the table does.
# ---------------------------------------------------------------------------

MANAGE_ACTIONS = ("add", "remove", "list")


def _entries(args: dict) -> list[tuple]:
	"""`entries` as (doctype, fieldname) pairs, in order, duplicates dropped."""
	raw = args.get("entries")
	if isinstance(raw, str) and raw.strip():
		try:
			raw = json.loads(raw)
		except ValueError:
			raise ToolError(
				'`entries` must be a list of {"doctype", "fieldname"} objects. Nothing was changed.'
			) from None
	if not isinstance(raw, list) or not raw:
		raise ToolError(
			'`entries` must be a non-empty list of {"doctype", "fieldname"} objects, e.g. '
			'[{"doctype": "Training Session", "fieldname": "expires_date"}]. Nothing was changed.'
		)
	pairs: list[tuple] = []
	problems = []
	for index, entry in enumerate(raw):
		if not isinstance(entry, dict):
			problems.append(f"- entries[{index}]: expected an object, got {type(entry).__name__}.")
			continue
		doctype = entry.get("doctype")
		fieldname = entry.get("fieldname")
		if not isinstance(doctype, str) or not doctype.strip():
			problems.append(f"- entries[{index}]: `doctype` is missing or empty.")
			continue
		if not isinstance(fieldname, str) or not fieldname.strip():
			problems.append(f"- entries[{index}]: `fieldname` is missing or empty.")
			continue
		pair = (doctype.strip(), fieldname.strip())
		if pair not in pairs:
			pairs.append(pair)
	if problems:
		raise ToolError(
			f"manage_updatable_fields refused {len(problems)} malformed entr"
			f"{'y' if len(problems) == 1 else 'ies'}. Nothing was changed.\n" + "\n".join(problems)
		)
	return pairs


def _whitelist_refusal(doctype: str, fieldname: str):
	"""Why update_document could never write this pair, whatever the table said.

	Returns (reason, field_info_or_None). An empty reason means the pair is one
	an operator may whitelist.
	"""
	why = _refusal_reason(doctype)
	if why:
		return f"{doctype} is never writable through update_document: {why}", None
	if not compat.doctype_exists(doctype):
		return (
			f"no DocType called {doctype!r} on this site (use the name the Desk shows, "
			"'Training Session', not 'training_session')",
			None,
		)
	if int(frappe.db.get_value("DocType", doctype, "istable") or 0):
		return f"{doctype} is a child table; update_document refuses child DocTypes", None
	if fieldname in SYSTEM_FIELDS:
		return f"{fieldname} is a system field and is never writable", None
	df = compat.field_meta(doctype, fieldname)
	if df is None:
		suggestions = _suggest_fields(doctype, fieldname)
		hint = f" Did you mean: {', '.join(repr(x) for x in suggestions)}?" if suggestions else ""
		return f"{doctype} has no field called {fieldname!r} (use the fieldname, not the label).{hint}", None
	info = field_info(df)
	why = REFUSED_FIELDTYPES.get(info["fieldtype"])
	if why:
		return f"{fieldname} is never writable: {why}", info
	return "", info


def _whitelist_rows(doc) -> list:
	return list(doc.get(WHITELIST_FIELD) or [])


def _row_pair(row) -> tuple:
	return (str(row.get("doctype_name") or "").strip(), str(row.get("field_name") or "").strip())


def _save_settings(doc) -> None:
	try:
		doc.save()
	except frappe.PermissionError as exc:
		raise ToolError(
			f"this account may not write {settings.SETTINGS_DOCTYPE}: {exc}. The whitelist lives on "
			"that document, so changing it needs write permission on it (System Manager). "
			"Nothing was changed."
		) from exc
	except frappe.ValidationError as exc:
		raise ToolError(
			f"{settings.SETTINGS_DOCTYPE} refused the save: {type(exc).__name__}: {exc}. This is the "
			"settings form's own validation, not the whitelist — fix it at the Desk. Nothing was changed."
		) from exc


def _add(doc, pairs: list[tuple]) -> dict:
	rejected = {}
	infos = {}
	for doctype, fieldname in pairs:
		reason, info = _whitelist_refusal(doctype, fieldname)
		if reason:
			rejected[f"{doctype}.{fieldname}"] = {
				"doctype": doctype,
				"fieldname": fieldname,
				"reason": reason,
			}
		else:
			infos[(doctype, fieldname)] = info
	if rejected:
		lines = [f"- {key}: {problem['reason']}" for key, problem in rejected.items()]
		raise ToolError(
			f"manage_updatable_fields refused {len(rejected)} of {len(pairs)} entr"
			f"{'y' if len(pairs) == 1 else 'ies'}; one refused entry refuses the whole call. "
			"Nothing was changed.\n"
			+ "\n".join(lines)
			+ "\n\nDetails (JSON): "
			+ json.dumps({"rejected": rejected}, default=str, ensure_ascii=False)
		)

	existing: dict = {}
	for row in _whitelist_rows(doc):
		existing.setdefault(_row_pair(row), []).append(row)

	added, enabled, skipped = [], [], []
	for pair in pairs:
		doctype, fieldname = pair
		entry = {"doctype": doctype, "fieldname": fieldname, **_brief(infos[pair])}
		rows = existing.get(pair)
		if not rows:
			doc.append(WHITELIST_FIELD, {"doctype_name": doctype, "field_name": fieldname, "enabled": 1})
			added.append(entry)
		elif any(settings.as_bool(row.get("enabled")) for row in rows):
			skipped.append(entry)
		else:
			# Present but unticked: asking to add it is asking for it to count.
			for row in rows:
				if hasattr(row, "set"):
					row.set("enabled", 1)
				else:
					row["enabled"] = 1
			enabled.append(entry)
	if added or enabled:
		_save_settings(doc)
	return {"added": added, "re_enabled": enabled, "already_present": skipped}


def _remove(doc, pairs: list[tuple]) -> dict:
	wanted = set(pairs)
	kept, removed_pairs = [], []
	for row in _whitelist_rows(doc):
		pair = _row_pair(row)
		if pair in wanted:
			if pair not in removed_pairs:
				removed_pairs.append(pair)
			continue
		kept.append(row)
	if removed_pairs:
		doc.set(WHITELIST_FIELD, kept)
		_save_settings(doc)
	removed = [{"doctype": d, "fieldname": f} for d, f in pairs if (d, f) in removed_pairs]
	not_found = [{"doctype": d, "fieldname": f} for d, f in pairs if (d, f) not in removed_pairs]
	return {"removed": removed, "not_found": not_found}


def _list(doc, doctype: str) -> dict:
	entries = []
	for row in _whitelist_rows(doc):
		row_doctype, fieldname = _row_pair(row)
		if doctype and row_doctype != doctype:
			continue
		entry = {
			"doctype": row_doctype,
			"fieldname": fieldname,
			"enabled": settings.as_bool(row.get("enabled")),
		}
		# Say which rows can never take effect, so a dead row is visible here
		# rather than as a refusal on the next update_document call.
		reason, info = _whitelist_refusal(row_doctype, fieldname)
		if info:
			entry.update(_brief(info))
		if reason:
			entry["problem"] = reason
		entries.append(entry)
	entries.sort(key=lambda e: (e["doctype"], e["fieldname"]))
	by_doctype: dict = {}
	for entry in entries:
		if entry["enabled"] and "problem" not in entry:
			by_doctype.setdefault(entry["doctype"], []).append(entry["fieldname"])
	return {
		"entries": entries,
		"count": len(entries),
		"enabled_by_doctype": by_doctype,
		"doctype_filter": doctype or None,
	}


def manage_updatable_fields(args: dict) -> ToolResult:
	"""MUTATING. Add, remove or list rows of update_document's whitelist."""
	if not settings.tool_enabled("manage_updatable_fields"):
		raise ToolError(
			"manage_updatable_fields is switched off. An operator must tick "
			"'allow_manage_updatable_fields' in ERPNext MCP Settings to enable it. Nothing was changed."
		)
	action = as_str(args, "action", required=True).strip().lower()
	if action not in MANAGE_ACTIONS:
		raise ToolError(
			f"`action` must be one of: {', '.join(MANAGE_ACTIONS)} — got {action!r}. Nothing was changed."
		)

	doc = frappe.get_single(settings.SETTINGS_DOCTYPE)

	if action == "list":
		doctype = as_str(args, "doctype").strip()
		data = _list(doc, doctype)
		scope = f" for {doctype}" if doctype else ""
		return ToolResult(data=data, summary=f"{data['count']} whitelisted field(s){scope}")

	pairs = _entries(args)
	if not frappe.has_permission(settings.SETTINGS_DOCTYPE, "write"):
		raise ToolError(
			f"this account may not write {settings.SETTINGS_DOCTYPE}, where the whitelist lives. "
			"Nothing was changed."
		)
	if action == "add":
		data = _add(doc, pairs)
		summary = (
			f"added {len(data['added'])}, re-enabled {len(data['re_enabled'])}, "
			f"already present {len(data['already_present'])}"
		)
	else:
		data = _remove(doc, pairs)
		summary = f"removed {len(data['removed'])}, not found {len(data['not_found'])}"
	data["action"] = action
	return ToolResult(data=data, summary=summary)
