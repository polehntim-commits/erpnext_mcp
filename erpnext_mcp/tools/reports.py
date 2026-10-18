# SPDX-License-Identifier: MIT
"""Report tools: find a site's saved reports and run one.

`run_report` is the highest-leverage tool in the catalogue, because an ERPNext
site's reports are where the accounting questions have *already been answered*
correctly by somebody who knew the schema. Reconstructing "Accounts Receivable
Summary" out of primitive queries is how you get a number that is nearly right.
Running the report the operator already trusts is how you get the number they
would have got themselves.

THREE REPORT TYPES, THREE CODE PATHS. Frappe's Report doctype covers three quite
different things and there is no single API that runs all of them:

  * **Query Report** — a stored SQL statement.
  * **Script Report** — a Python module in an app.
    Both of these run through `frappe.desk.query_report.run`, which is the same
    entry point the Desk uses, so prepared-report handling, column metadata and
    the report's own permission check all come along.
  * **Report Builder** — a saved list view: columns, filters and a sort order
    stored as JSON against a DocType. There is no server-side "run" for these;
    the Desk renders them through `frappe.desk.reportview.get`. This module calls
    that with a scoped `form_dict`, and falls back to `frappe.get_list` built
    from the same saved configuration if the call is unavailable.

The response always names the path in `executed_via`, because "which engine
produced these rows" changes how much you should trust an unexpected number.

PERMISSIONS APPLY HERE. Unlike the other read tools — see docs/security.md —
reports run through Frappe's own APIs, which check the acting user's permission
on the report's `ref_doctype`. A report an MCP System User may not read fails
with a permission error rather than returning rows.
"""

import json

import frappe

from .. import compat
from ..args import as_bool, as_date, as_limit, as_str
from ..errors import ToolError
from ..result import ToolResult

QUERY_TYPES = ("Query Report", "Script Report")


# ── 21. list_reports ────────────────────────────────────────────────────────
def list_reports(args: dict) -> ToolResult:
	"""Every Report on the site, with the DocType it reports on and its type."""
	module = as_str(args, "module")
	raw_standard = args.get("is_standard")

	filters = {}
	if module:
		filters["module"] = module
	if raw_standard is not None and raw_standard != "":
		filters["is_standard"] = _standard_value(raw_standard)

	fields = compat.existing_fields(
		"Report",
		[
			"name",
			"report_name",
			"ref_doctype",
			"report_type",
			"module",
			"is_standard",
			"disabled",
			"prepared_report",
			"add_total_row",
			"letter_head",
		],
	)
	rows = frappe.db.get_all("Report", filters=filters, fields=fields, order_by="module asc, name asc")
	for row in rows:
		row["runnable_by"] = (
			"run_report" if row.get("report_type") in QUERY_TYPES else "run_report (Report Builder)"
		)

	by_type = {}
	for row in rows:
		by_type[row.get("report_type") or "unknown"] = by_type.get(row.get("report_type") or "unknown", 0) + 1

	data = {
		"reports": rows,
		"count": len(rows),
		"by_report_type": by_type,
		"filters": {"module": module or None, "is_standard": filters.get("is_standard")},
		"note": (
			"ref_doctype is what the report reports on, and is what a permission "
			"check runs against when you call run_report."
		),
	}
	return ToolResult(data, f"{len(rows)} report(s)")


def _standard_value(raw):
	"""Report.is_standard is a Select of 'Yes'/'No', but a model will send a bool."""
	if isinstance(raw, bool):
		return "Yes" if raw else "No"
	text = str(raw).strip().lower()
	if text in ("yes", "true", "1"):
		return "Yes"
	if text in ("no", "false", "0"):
		return "No"
	raise ToolError(f"is_standard must be Yes or No, got {raw!r}")


# ── 22. run_report ──────────────────────────────────────────────────────────
def run_report(args: dict) -> ToolResult:
	"""Run a saved report and return its columns and rows."""
	name = as_str(args, "name", required=True)
	user = as_str(args, "user")
	limit = as_limit(args)
	filters = _filters(args.get("filters"))

	if not frappe.db.exists("Report", name):
		raise ToolError(f"no Report named {name!r}. Use list_reports to see what this site has.")
	report = frappe.db.get_value(
		"Report",
		name,
		compat.existing_fields(
			"Report", ["name", "report_type", "ref_doctype", "disabled", "module", "json"]
		),
		as_dict=True,
	)
	if report.get("disabled"):
		raise ToolError(f"Report {name!r} is disabled on this site")
	if user and not frappe.db.exists("User", user):
		raise ToolError(f"no User named {user!r}")

	report_type = report.get("report_type")
	ttm = _ttm_request(args, name)
	try:
		if ttm:
			columns, rows, extras, via, filters = _run_ttm(name, filters, user, ttm)
		elif report_type in QUERY_TYPES:
			columns, rows, extras, via = _run_query_report(name, filters, user)
		elif report_type == "Report Builder":
			columns, rows, extras, via = _run_report_builder(report, filters, limit)
		else:
			raise ToolError(
				f"Report {name!r} has report_type {report_type!r}, which this tool "
				f"does not know how to run. Supported: "
				f"{', '.join([*QUERY_TYPES, 'Report Builder'])}."
			)
	except frappe.PermissionError as exc:
		raise ToolError(
			f"{user or frappe.session.user} is not permitted to run Report {name!r} "
			f"(it reports on {report.get('ref_doctype')}). {exc}"
		) from exc

	total = len(rows)
	truncated = total > limit
	data = {
		"report": name,
		"report_type": report_type,
		"ref_doctype": report.get("ref_doctype"),
		"executed_as": user or frappe.session.user,
		"executed_via": via,
		"filters_applied": filters,
		"columns": columns,
		"columns_normalised": [_normalise_column(column) for column in columns],
		"rows": rows[:limit],
		"row_count": min(total, limit),
		"total_rows": total,
		"truncated": truncated,
		"limit": limit,
		"row_format": _row_format(rows),
		**extras,
	}
	return ToolResult(
		data,
		f"ran report {name} ({report_type}) as {data['executed_as']}: {data['row_count']} of {total} row(s)",
	)


# ── v0.333.0: period="TTM" on a financial statement ───────────────────────────
#: This app's own Desk report, which takes the TTM options as filters itself.
TTM_REPORT = "Financial Statements TTM"


def _ttm_request(args: dict, name: str) -> dict | None:
	"""The TTM options when `period` asks for them; None when it does not."""
	from ..services import ttm_statements

	period = as_str(args, "period")
	if not period:
		return None
	if period.strip().upper() != "TTM":
		raise ToolError(
			f"period takes 'TTM' (the twelve full months to the last month-end); got {period!r}. "
			"For a fiscal year or a date range, pass the report's own filters instead."
		)
	if name != TTM_REPORT and not ttm_statements.statement_name(name):
		raise ToolError(
			f"period='TTM' applies to {', '.join(ttm_statements.STATEMENTS)} and {TTM_REPORT}, not "
			f"{name!r}. A windowed KPI takes it on get_windowed_report."
		)
	return {
		"as_of": as_date(args, "as_of") or None,
		"monthly": bool(as_bool(args, "monthly_columns", False)),
		"compare": bool(as_bool(args, "compare_prior", False)),
	}


def _run_ttm(name: str, filters: dict, user: str, ttm: dict):
	"""One statement over the TTM window, every run through the Desk's own runner."""
	from ..services import ttm_statements

	if not filters.get("company"):
		raise ToolError('period="TTM" needs filters.company — a statement is always one company\'s.')
	if name == TTM_REPORT:
		mine = {
			**filters,
			"as_of": ttm["as_of"],
			"monthly_columns": 1 if ttm["monthly"] else 0,
			"compare_prior": 1 if ttm["compare"] else 0,
		}
		columns, rows, extras, via = _run_query_report(name, mine, user)
		return columns, rows, extras, via, mine

	def runner(statement, erpnext_filters):
		columns, rows, extras, _via = _run_query_report(statement, erpnext_filters, user)
		return {"columns": columns, "result": rows, **extras}

	try:
		result = ttm_statements.run(
			name, filters, as_of=ttm["as_of"], monthly=ttm["monthly"], compare=ttm["compare"], runner=runner
		)
	except frappe.PermissionError:
		raise
	extras = {
		"ttm": result["window"],
		"message": result["message"],
		"ttm_warnings": result["warnings"],
	}
	if result.get("chart"):
		extras["chart"] = result["chart"]
	if result.get("report_summary"):
		extras["report_summary"] = result["report_summary"]
	via = "frappe.desk.query_report.run, TTM window" + (" + prior TTM" if ttm["compare"] else "")
	return result["columns"], result["data"], extras, via, result["erpnext_filters"]


def _filters(raw) -> dict:
	if raw in (None, ""):
		return {}
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			raise ToolError(
				'filters must be an object, e.g. {"company": "Example Trading Co", "from_date": "2026-01-01"}'
			) from None
	if not isinstance(raw, dict):
		raise ToolError(f"filters must be an object, got {type(raw).__name__}")
	return raw


def _run_query_report(name: str, filters: dict, user: str):
	"""Query and Script reports, through the Desk's own runner."""
	from frappe.desk import query_report

	kwargs = {"report_name": name, "filters": filters, "user": user or None}
	try:
		# Without this a report configured as a Prepared Report queues a
		# background job and returns a job id instead of rows, which is useless
		# to a caller that cannot poll.
		result = query_report.run(**kwargs, ignore_prepared_report=True)
		via = "frappe.desk.query_report.run"
	except TypeError:
		# Older signature with no ignore_prepared_report.
		result = query_report.run(**kwargs)
		via = "frappe.desk.query_report.run (legacy signature)"

	result = result or {}
	extras = {}
	for key in ("message", "chart", "report_summary", "skip_total_row", "status"):
		if result.get(key) not in (None, "", []):
			extras[key] = result[key]
	return list(result.get("columns") or []), list(result.get("result") or []), extras, via


def _run_report_builder(report: dict, filters: dict, limit: int):
	"""A saved list view, materialised from its stored configuration."""
	doctype = report.get("ref_doctype")
	if not doctype or not compat.doctype_exists(doctype):
		raise ToolError(
			f"Report {report['name']!r} is a Report Builder report on "
			f"{doctype!r}, which is not installed on this site"
		)
	config = _builder_config(report)
	columns = _builder_columns(config, doctype)
	conditions = _builder_filters(config, doctype) + [
		[doctype, field, "=", value] for field, value in filters.items()
	]
	order_by = _builder_order(config)

	try:
		keys, values = _via_reportview(doctype, columns, conditions, order_by, limit)
		return (
			[{"fieldname": key, "label": key} for key in keys],
			values,
			{"saved_filters": config.get("filters") or []},
			"frappe.desk.reportview.get",
		)
	except frappe.PermissionError:
		raise
	except Exception:
		# reportview reads request state and has moved between versions; the
		# saved configuration is the same either way, so fall back to running it
		# through the ORM. get_list applies permissions, so this is not a
		# privilege escalation — only a different query builder.
		rows = frappe.get_list(
			doctype,
			fields=columns,
			filters=[condition[1:] for condition in conditions],
			order_by=order_by,
			limit=limit,
		)
		return (
			[{"fieldname": column, "label": column} for column in columns],
			[dict(row) for row in rows],
			{"saved_filters": config.get("filters") or []},
			"frappe.get_list (reportview unavailable on this version)",
		)


def _builder_config(report: dict) -> dict:
	raw = report.get("json")
	if not raw:
		return {}
	try:
		return json.loads(raw) if isinstance(raw, str) else dict(raw)
	except ValueError:
		return {}


def _builder_columns(config: dict, doctype: str) -> list[str]:
	"""Fieldnames from the saved column config, in either stored shape.

	Report Builder has stored columns as `[fieldname, parent_doctype]` pairs and,
	in newer versions, as objects. `name` is forced in first because a row a
	caller cannot identify is not much use.
	"""
	columns = []
	for entry in config.get("columns") or []:
		if isinstance(entry, (list, tuple)) and entry:
			columns.append(str(entry[0]))
		elif isinstance(entry, dict) and entry.get("fieldname"):
			columns.append(str(entry["fieldname"]))
	columns = [column for column in columns if compat.has_field(doctype, column)]
	if "name" not in columns:
		columns.insert(0, "name")
	return columns


def _builder_filters(config: dict, doctype: str) -> list[list]:
	out = []
	for entry in config.get("filters") or []:
		if isinstance(entry, (list, tuple)) and len(entry) >= 4:
			out.append([entry[0] or doctype, entry[1], entry[2], entry[3]])
	return out


def _builder_order(config: dict) -> str:
	sort_by = config.get("sort_by") or "modified"
	sort_order = (config.get("sort_order") or "desc").lower()
	if sort_order not in ("asc", "desc"):
		sort_order = "desc"
	return f"`{sort_by}` {sort_order}"


def _via_reportview(doctype, columns, conditions, order_by, limit):
	"""Call the Desk list API with a form_dict scoped to this call.

	`reportview.get` reads its parameters out of `frappe.local.form_dict` rather
	than taking them as arguments, so the only way to call it from here is to
	swap that in and put it back. The swap is restored in `finally`, so a raising
	report cannot leave the request holding someone else's parameters.
	"""
	from frappe.desk import reportview

	saved = getattr(frappe.local, "form_dict", None)
	frappe.local.form_dict = frappe._dict(
		{
			"doctype": doctype,
			"fields": json.dumps(columns),
			"filters": json.dumps(conditions),
			"order_by": order_by,
			"start": 0,
			"page_length": limit,
		}
	)
	try:
		result = reportview.get(doctype) or {}
	finally:
		frappe.local.form_dict = saved
	return list(result.get("keys") or []), [list(row) for row in result.get("values") or []]


def _normalise_column(column):
	"""One column as `{fieldname, label, fieldtype, options, width}`.

	Query Reports may hand back a dict, or the old colon-delimited string form
	`"Label:Fieldtype/Options:Width"`. A model should not have to parse that, so
	this does.
	"""
	if isinstance(column, dict):
		return {
			"fieldname": column.get("fieldname") or column.get("field") or column.get("label"),
			"label": column.get("label") or column.get("fieldname"),
			"fieldtype": column.get("fieldtype"),
			"options": column.get("options"),
			"width": column.get("width"),
		}
	text = str(column)
	parts = text.split(":")
	label = parts[0] or text
	fieldtype, options, width = None, None, None
	if len(parts) > 1 and parts[1]:
		fieldtype, _, options = parts[1].partition("/")
	if len(parts) > 2 and parts[2]:
		try:
			width = int(parts[2])
		except ValueError:
			width = None
	return {
		"fieldname": frappe.scrub(label) if hasattr(frappe, "scrub") else label,
		"label": label,
		"fieldtype": fieldtype or None,
		"options": options or None,
		"width": width,
	}


def _row_format(rows) -> str:
	if not rows:
		return "empty"
	return "objects" if isinstance(rows[0], dict) else "arrays (order matches columns)"
