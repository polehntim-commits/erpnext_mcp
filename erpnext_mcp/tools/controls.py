# SPDX-License-Identifier: MIT
"""Phase 1: the financial controls, and the gate every one of them runs through.

WHAT AN OPERATION GETS OUT OF THIS MODULE. Four controls an auditor tests first
and a small finance function usually cannot evidence: who may release a spend of
this size, whether a closed month is actually closed, whether the close was done
in full, and whether an entry about to be written is a duplicate, a transposed
digit, or being approved by the person who wrote it.

EVERY ONE OF THEM IS BYPASSABLE, AND THAT IS THE DESIGN RATHER THAN A CONCESSION.
`erpnext_mcp/enforcement.py` holds the argument in full; the short version is
that a control which cannot be turned down is a control that gets turned OFF, and
an operation with the module off gets no warnings either. So each control ships
Advisory: it evaluates, it files what it finds against the record, and it lets
the work through. Turning it up is one field on its Compliance Rule, and by the
time somebody turns it up they are looking at a season's worth of exactly the
findings it will start refusing.

THE JOURNAL ENTRY CONTROLS RUN FROM INSIDE `create_journal_entry`, not beside it.
`journal_entry_gate` is called by `tools/mutate.py` before the insert, which is
the only placement that makes the enforced mode mean anything — a control that
runs after the write can report and cannot refuse. `check_journal_entry_controls`
is the same evaluation exposed as a read, so a caller can ask "what would happen
if I booked this" and get the findings with nothing written and nothing refused.

WHY THE PERIOD LOCK LIVES ON Closing Checklist RATHER THAN ON Fiscal Year. Three
reasons, and the third decided it. ERPNext's Fiscal Year is a year and the thing
people close is a month. Extending somebody else's doctype with custom fields is
something this app does exactly once and documents heavily (`compliance_fields.py`
makes that argument). And the checklist and the lock are the same object seen
from two sides — what has to be true before a period is finished, and the
statement that it now is — so keeping them on one row makes "locked with steps
outstanding" a state the schema can describe and the control can find, rather
than a join somebody has to write a report for.
"""

import frappe

from .. import compat, controls, enforcement
from ..args import as_bool, as_date, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult

THRESHOLD = "Approval Threshold"
THRESHOLD_LEVEL = "Approval Threshold Level"
CHECKLIST = "Closing Checklist"
CHECKLIST_ITEM = "Closing Checklist Item"
JOURNAL_ENTRY = "Journal Entry"

_HINT = "It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app."

#: Hard ceiling on rows any register read returns, under `as_limit`'s own cap.
RECORD_CAP = 500

#: How many historical entries the unusual-amount test reads to build its median.
#: Enough for the median to mean something, bounded so the control cannot turn a
#: journal entry into a table scan on a site with a decade of history.
HISTORY_CAP = 200

_THRESHOLD_FIELDS = (
	"name",
	"threshold_name",
	"company",
	"document_type",
	"enabled",
	"currency",
	"auto_approve_below",
	"notes",
	"modified",
	"owner",
)

_CHECKLIST_FIELDS = (
	"name",
	"company",
	"period_type",
	"period_start",
	"period_end",
	"fiscal_year",
	"status",
	"locked",
	"locked_on",
	"locked_by",
	"item_count",
	"outstanding_count",
	"notes",
	"modified",
	"owner",
)

_DOCUMENT_TYPES = (
	"Any",
	"Journal Entry",
	"Purchase Order",
	"Purchase Invoice",
	"Payment Entry",
	"Expense Report",
	"Stock Entry",
)


def _require_thresholds() -> None:
	compat.require_doctype(THRESHOLD, _HINT)


def _require_checklists() -> None:
	compat.require_doctype(CHECKLIST, _HINT)


# ── approval thresholds ─────────────────────────────────────────────────────
def _threshold_doc(reference: str, company: str = ""):
	"""One Approval Threshold from its docname or its threshold_name.

	Two ways to name it for the reason every register in this app takes two: the
	caller who created it knows the name it asked for, and everything that links
	to it holds the docname with the company abbreviation on the end.
	"""
	reference = (reference or "").strip()
	if not reference:
		raise ToolError("name is required — the threshold's docname or its threshold_name.")
	if frappe.db.exists(THRESHOLD, reference):
		return frappe.get_doc(THRESHOLD, reference)
	filters = {"threshold_name": reference}
	if company:
		filters["company"] = company
	matches = frappe.db.get_all(THRESHOLD, filters=filters, pluck="name", limit=5)
	if len(matches) == 1:
		return frappe.get_doc(THRESHOLD, matches[0])
	if len(matches) > 1:
		raise ToolError(
			f"{reference!r} names {len(matches)} thresholds: {', '.join(sorted(matches))}. "
			"Pass the docname, or set company to narrow it."
		)
	raise ToolError(
		f"no Approval Threshold called {reference!r} on this site. list_approval_thresholds has the register."
	)


def _describe_threshold(doc) -> dict:
	levels = []
	for row in doc.get("levels") or []:
		levels.append(
			{
				"level": int(row.get("level") or 0),
				"up_to_amount": float(row.get("up_to_amount") or 0) or None,
				"approver_role": row.get("approver_role"),
				"notes": row.get("notes") or None,
			}
		)
	# Sorted the way the chain is EVALUATED rather than the way the grid was
	# typed, so a reader and the control see one order. Uncapped last.
	levels.sort(key=lambda row: (row["up_to_amount"] is None, row["up_to_amount"] or 0))
	return {
		"name": doc.name,
		"threshold_name": doc.threshold_name,
		"company": doc.company,
		"document_type": doc.document_type,
		"enabled": bool(compat.checked(doc.enabled)),
		"currency": doc.currency or None,
		"auto_approve_below": float(doc.auto_approve_below or 0) or None,
		"levels": levels,
		"level_count": len(levels),
		"notes": doc.notes or None,
	}


def _requested_levels(raw) -> list[dict]:
	"""Coerce the `levels` argument into child rows, or refuse with the shape."""
	if raw is None:
		return []
	if not isinstance(raw, list):
		raise ToolError(
			'levels must be a list of objects, e.g. [{"approver_role": "Farm Manager", '
			'"up_to_amount": 5000}, {"approver_role": "Accounts Manager"}]. The row with no '
			"up_to_amount is the top of the chain."
		)
	rows = []
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"levels[{index}] must be an object, got {type(entry).__name__}")
		role = str(entry.get("approver_role") or "").strip()
		if not role:
			raise ToolError(
				f"levels[{index}] needs an approver_role — a rung with no approver cannot release anything."
			)
		if not frappe.db.exists("Role", role):
			raise ToolError(
				f"levels[{index}] names role {role!r}, which is not a Role on this site. Create the "
				"role first — a chain pointing at a role nobody can hold refuses everything at that "
				"rung, silently."
			)
		ceiling = entry.get("up_to_amount")
		rows.append(
			{
				"level": int(entry.get("level") or index),
				"up_to_amount": float(ceiling or 0),
				"approver_role": role,
				"notes": str(entry.get("notes") or "").strip(),
			}
		)
	return rows


def create_approval_threshold(args: dict) -> ToolResult:
	"""Define who may release a transaction of a given size. MUTATING."""
	_require_thresholds()
	company = resolve_company(as_str(args, "company"), required=True)
	threshold_name = as_str(args, "threshold_name", required=True)

	if frappe.db.exists(THRESHOLD, {"threshold_name": threshold_name, "company": company}):
		raise ToolError(
			f"{threshold_name!r} already names an Approval Threshold for {company}. "
			"update_approval_threshold edits it; a genuinely different table wants a different "
			"threshold_name. Nothing was created."
		)

	document_type = as_str(args, "document_type") or "Any"
	if document_type not in _DOCUMENT_TYPES:
		raise ToolError(
			f"document_type must be one of: {', '.join(_DOCUMENT_TYPES)}. Got {document_type!r}. "
			"Nothing was created."
		)

	doc = frappe.new_doc(THRESHOLD)
	doc.threshold_name = threshold_name
	doc.company = company
	doc.document_type = document_type
	doc.enabled = 1 if as_bool(args, "enabled", True) else 0
	currency = as_str(args, "currency")
	if currency:
		doc.currency = currency
	floor = args.get("auto_approve_below")
	if floor is not None:
		doc.auto_approve_below = float(floor or 0)
	notes = as_str(args, "notes")
	if notes:
		doc.notes = notes
	for row in _requested_levels(args.get("levels")):
		doc.append("levels", row)

	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	described = _describe_threshold(doc)
	data = {"threshold": described, "control": enforcement.status("approval_threshold")}
	return ToolResult(
		data=data,
		summary=(
			f"created Approval Threshold {doc.name} for {company} covering {document_type}: "
			f"{described['level_count']} level(s)"
			+ (
				f", auto-approve below {described['auto_approve_below']}"
				if described["auto_approve_below"]
				else ""
			)
		),
		docstatus_delta="none → 0 (draft)",
	)


def get_approval_threshold(args: dict) -> ToolResult:
	"""One approval threshold with its chain, in evaluation order. Read-only."""
	_require_thresholds()
	company = resolve_company(as_str(args, "company"), required=False) or ""
	doc = _threshold_doc(as_str(args, "name", required=True), company)
	described = _describe_threshold(doc)
	described["control"] = enforcement.status("approval_threshold")

	# Answer the question a caller actually has, when they have one.
	amount = args.get("amount")
	if amount is not None:
		described["authority_for_amount"] = controls.required_authority(
			{**described, "levels": described["levels"]}, amount
		)

	return ToolResult(
		data=described,
		summary=(
			f"{doc.threshold_name} ({doc.company}, {doc.document_type}), "
			f"{'enabled' if described['enabled'] else 'disabled'}, "
			f"{described['level_count']} level(s)"
		),
	)


def list_approval_thresholds(args: dict) -> ToolResult:
	"""The authority register: every threshold matching the filters. Read-only."""
	_require_thresholds()
	limit = min(as_limit(args), RECORD_CAP)
	filters: dict = {}
	company = resolve_company(as_str(args, "company"), required=False)
	if company:
		filters["company"] = company
	document_type = as_str(args, "document_type")
	if document_type:
		filters["document_type"] = document_type
	enabled = as_bool(args, "enabled", None)
	if enabled is not None:
		filters["enabled"] = 1 if enabled else 0

	rows = frappe.db.get_all(
		THRESHOLD,
		filters=filters,
		fields=compat.existing_fields(THRESHOLD, _THRESHOLD_FIELDS),
		order_by="company asc, threshold_name asc",
		limit=limit,
	)
	out = []
	for row in rows:
		described = _describe_threshold(frappe.get_doc(THRESHOLD, row["name"]))
		out.append(described)

	return ToolResult(
		data={
			"thresholds": out,
			"threshold_count": len(out),
			"control": enforcement.status("approval_threshold"),
			"note": (
				"A threshold is CONSULTED, not applied — it says who must release a transaction of "
				"this size, and the `approval_threshold` control decides whether exceeding it is "
				"reported or refused. A site with no threshold for a document type has expressed no "
				"opinion about authority for it, which the control reports as such rather than "
				"treating as unlimited."
			),
		},
		summary=f"{len(out)} approval threshold(s)"
		+ (f" for {company}" if company else "")
		+ (f" covering {document_type}" if document_type else ""),
	)


def update_approval_threshold(args: dict) -> ToolResult:
	"""Change a threshold's floor, chain or enabled state. MUTATING."""
	_require_thresholds()
	company = resolve_company(as_str(args, "company"), required=False) or ""
	doc = _threshold_doc(as_str(args, "name", required=True), company)
	before = _describe_threshold(doc)
	changed = []

	document_type = as_str(args, "document_type")
	if document_type:
		if document_type not in _DOCUMENT_TYPES:
			raise ToolError(
				f"document_type must be one of: {', '.join(_DOCUMENT_TYPES)}. Got {document_type!r}. "
				"Nothing was changed."
			)
		doc.document_type = document_type
		changed.append("document_type")

	enabled = as_bool(args, "enabled", None)
	if enabled is not None:
		doc.enabled = 1 if enabled else 0
		changed.append("enabled")

	if args.get("auto_approve_below") is not None:
		doc.auto_approve_below = float(args.get("auto_approve_below") or 0)
		changed.append("auto_approve_below")

	currency = as_str(args, "currency")
	if currency:
		doc.currency = currency
		changed.append("currency")

	notes = as_str(args, "notes")
	if notes:
		doc.notes = notes
		changed.append("notes")

	if args.get("levels") is not None:
		# REPLACES THE CHAIN RATHER THAN MERGING INTO IT. A partial update of an
		# authority table is the one edit shape that can silently leave a rung
		# nobody meant to keep — and a stale rung in an approval chain authorises
		# spending. Passing `levels` means "the chain is now this".
		doc.set("levels", [])
		for row in _requested_levels(args.get("levels")):
			doc.append("levels", row)
		changed.append("levels")

	if not changed:
		raise ToolError(
			"nothing to update. Pass at least one of: document_type, enabled, auto_approve_below, "
			"currency, notes, levels."
		)

	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	after = _describe_threshold(doc)
	return ToolResult(
		data={
			"threshold": after,
			"changed": changed,
			"was": before,
			"control": enforcement.status("approval_threshold"),
		},
		summary=f"updated Approval Threshold {doc.name}: {', '.join(changed)}",
		docstatus_delta="0 → 0 (draft, edited)",
	)


# ── closing checklists and the period lock ──────────────────────────────────
def _checklist_doc(reference: str, company: str = ""):
	reference = (reference or "").strip()
	if not reference:
		raise ToolError("name is required — the checklist's docname.")
	if frappe.db.exists(CHECKLIST, reference):
		return frappe.get_doc(CHECKLIST, reference)
	raise ToolError(
		f"no Closing Checklist called {reference!r} on this site. list_closing_checklists has the "
		"register, and it filters by company, period type and status."
	)


def _describe_checklist(doc) -> dict:
	items = []
	for row in doc.get("items") or []:
		items.append(
			{
				"idx": int(row.get("idx") or 0),
				"step": row.get("step"),
				"step_es": row.get("step_es") or None,
				"sequence": int(row.get("sequence") or 0),
				"required": bool(compat.checked(row.get("required"))),
				"completed": bool(compat.checked(row.get("completed"))),
				"completed_by": row.get("completed_by") or None,
				"completed_on": str(row.get("completed_on") or "") or None,
				"evidence": row.get("evidence") or None,
				"notes": row.get("notes") or None,
			}
		)
	items.sort(key=lambda row: (row["sequence"], row["idx"]))
	outstanding = [row for row in items if row["required"] and not row["completed"]]
	return {
		"name": doc.name,
		"company": doc.company,
		"period_type": doc.period_type,
		"period_start": str(doc.period_start or ""),
		"period_end": str(doc.period_end or ""),
		"fiscal_year": doc.fiscal_year or None,
		"status": doc.status,
		"locked": bool(compat.checked(doc.locked)),
		"locked_on": str(doc.locked_on or "") or None,
		"locked_by": doc.locked_by or None,
		"items": items,
		"item_count": len(items),
		"outstanding": outstanding,
		"outstanding_count": len(outstanding),
		"notes": doc.notes or None,
	}


def _requested_items(raw) -> list[dict]:
	if raw is None:
		return []
	if not isinstance(raw, list):
		raise ToolError(
			'items must be a list of objects, e.g. [{"step": "Reconcile every bank account", '
			'"step_es": "Conciliar cada cuenta bancaria", "sequence": 1, "required": true}]'
		)
	rows = []
	for index, entry in enumerate(raw, start=1):
		if isinstance(entry, str):
			# A caller who sends a bare list of strings means a list of steps, and
			# reading it that way costs nothing and saves a round trip.
			entry = {"step": entry}
		if not isinstance(entry, dict):
			raise ToolError(f"items[{index}] must be an object or a string, got {type(entry).__name__}")
		step = str(entry.get("step") or "").strip()
		if not step:
			raise ToolError(f"items[{index}] needs a step — what has to be done.")
		required = entry.get("required")
		rows.append(
			{
				"step": step,
				"step_es": str(entry.get("step_es") or "").strip(),
				"sequence": int(entry.get("sequence") or index),
				"required": 1 if (required is None or as_bool({"v": required}, "v", True)) else 0,
				"completed": 1 if entry.get("completed") else 0,
				"evidence": str(entry.get("evidence") or "").strip(),
				"notes": str(entry.get("notes") or "").strip(),
			}
		)
	return rows


#: The steps a month-end close almost always has, seeded into a new checklist
#: when the caller names none. Bilingual, because the person ticking them is as
#: likely to be reading Spanish as English — see the doctype on why the
#: translation is a column rather than a PO file.
DEFAULT_STEPS = (
	("Reconcile every bank account", "Conciliar cada cuenta bancaria", 1),
	("Post accruals and prepayments", "Registrar devengos y pagos anticipados", 2),
	("Run depreciation for the period", "Ejecutar la depreciación del período", 3),
	("Review and clear suspense accounts", "Revisar y liquidar cuentas transitorias", 4),
	("Reconcile inventory to the stock ledger", "Conciliar el inventario con el libro de existencias", 5),
	("Review accounts receivable ageing", "Revisar la antigüedad de las cuentas por cobrar", 6),
	("Review accounts payable ageing", "Revisar la antigüedad de las cuentas por pagar", 7),
	("Reconcile payroll to the general ledger", "Conciliar la nómina con el libro mayor", 8),
)


def create_closing_checklist(args: dict) -> ToolResult:
	"""Open a period with the steps that have to be finished before it closes. MUTATING."""
	_require_checklists()
	company = resolve_company(as_str(args, "company"), required=True)
	period_start = as_date(args, "period_start", required=True)
	period_end = as_date(args, "period_end", required=True)
	period_type = as_str(args, "period_type") or "Month"
	if period_type not in ("Month", "Quarter", "Year"):
		raise ToolError(
			f"period_type must be Month, Quarter or Year. Got {period_type!r}. Nothing was created."
		)

	doc = frappe.new_doc(CHECKLIST)
	doc.company = company
	doc.period_type = period_type
	doc.period_start = period_start
	doc.period_end = period_end
	fiscal_year = as_str(args, "fiscal_year")
	if fiscal_year:
		doc.fiscal_year = fiscal_year
	doc.status = "Open"
	notes = as_str(args, "notes")
	if notes:
		doc.notes = notes

	requested = _requested_items(args.get("items"))
	used_defaults = False
	if not requested:
		used_defaults = True
		requested = [
			{
				"step": step,
				"step_es": step_es,
				"sequence": sequence,
				"required": 1,
				"completed": 0,
				"evidence": "",
				"notes": "",
			}
			for step, step_es, sequence in DEFAULT_STEPS
		]
	for row in requested:
		doc.append("items", row)

	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	described = _describe_checklist(doc)
	data = {"checklist": described, "control": enforcement.status("closing_checklist")}
	if used_defaults:
		data["defaults_note"] = (
			f"No steps were given, so the {len(DEFAULT_STEPS)} steps a month-end close almost always "
			"has were seeded — every one of them REQUIRED, and every one editable with "
			"update_closing_checklist. They are a starting point, not this operation's close: a "
			"checklist nobody has adapted is a checklist somebody ticks without reading."
		)
	return ToolResult(
		data=data,
		summary=(
			f"opened {period_type} close {doc.name} for {company} covering {period_start} to "
			f"{period_end}: {described['item_count']} step(s), {described['outstanding_count']} required"
		),
		docstatus_delta="none → 0 (draft)",
	)


def get_closing_checklist(args: dict) -> ToolResult:
	"""One period: its steps, what is outstanding, and whether it is locked. Read-only."""
	_require_checklists()
	doc = _checklist_doc(as_str(args, "name", required=True))
	described = _describe_checklist(doc)
	described["control"] = enforcement.status("closing_checklist")
	described["lockdown_control"] = enforcement.status("period_close_lockdown")

	summary = (
		f"{doc.period_type} close {doc.name} ({doc.company}), {doc.status}, "
		f"{described['outstanding_count']} of {described['item_count']} required step(s) outstanding"
	)
	if described["locked"]:
		summary += " — LOCKED against posting"
	return ToolResult(data=described, summary=summary)


def list_closing_checklists(args: dict) -> ToolResult:
	"""The period register, most recent first. Read-only."""
	_require_checklists()
	limit = min(as_limit(args), RECORD_CAP)
	filters: dict = {}
	company = resolve_company(as_str(args, "company"), required=False)
	if company:
		filters["company"] = company
	period_type = as_str(args, "period_type")
	if period_type:
		filters["period_type"] = period_type
	status = as_str(args, "status")
	if status:
		filters["status"] = status
	locked = as_bool(args, "locked", None)
	if locked is not None:
		filters["locked"] = 1 if locked else 0

	rows = frappe.db.get_all(
		CHECKLIST,
		filters=filters,
		fields=compat.existing_fields(CHECKLIST, _CHECKLIST_FIELDS),
		order_by="period_start desc",
		limit=limit,
	)
	out = []
	for row in rows:
		row = dict(row)
		row["locked"] = bool(compat.checked(row.get("locked")))
		row["period_start"] = str(row.get("period_start") or "")
		row["period_end"] = str(row.get("period_end") or "")
		out.append(row)

	return ToolResult(
		data={
			"periods": out,
			"period_count": len(out),
			"locked_count": sum(1 for row in out if row["locked"]),
			"control": enforcement.status("closing_checklist"),
			"lockdown_control": enforcement.status("period_close_lockdown"),
		},
		summary=f"{len(out)} closing checklist(s)" + (f" for {company}" if company else ""),
	)


def update_closing_checklist(args: dict) -> ToolResult:
	"""Edit a period's steps, dates or notes. MUTATING.

	DOES NOT LOCK OR UNLOCK. `close_accounting_period` and
	`reopen_accounting_period` do that, because both are decisions with a control
	attached and a reason to record, and burying either in a general-purpose edit
	would mean a period could be locked as a side effect of fixing a typo.
	"""
	_require_checklists()
	doc = _checklist_doc(as_str(args, "name", required=True))
	changed = []

	period_start = as_date(args, "period_start")
	if period_start:
		doc.period_start = period_start
		changed.append("period_start")
	period_end = as_date(args, "period_end")
	if period_end:
		doc.period_end = period_end
		changed.append("period_end")
	period_type = as_str(args, "period_type")
	if period_type:
		if period_type not in ("Month", "Quarter", "Year"):
			raise ToolError(f"period_type must be Month, Quarter or Year. Got {period_type!r}.")
		doc.period_type = period_type
		changed.append("period_type")
	fiscal_year = as_str(args, "fiscal_year")
	if fiscal_year:
		doc.fiscal_year = fiscal_year
		changed.append("fiscal_year")
	status = as_str(args, "status")
	if status:
		if status not in ("Open", "In Progress", "Closed", "Reopened"):
			raise ToolError("status must be one of: Open, In Progress, Closed, Reopened.")
		doc.status = status
		changed.append("status")
	notes = as_str(args, "notes")
	if notes:
		doc.notes = notes
		changed.append("notes")

	if args.get("items") is not None:
		# Replaces the step list, for the same reason `update_approval_threshold`
		# replaces the chain: a merge would leave steps nobody meant to keep, and a
		# stale REQUIRED step holds a close for a reason nobody can find.
		# COMPLETIONS ARE CARRIED ACROSS BY STEP TEXT, because losing the record
		# that somebody reconciled the bank — and when — merely because a later step
		# was reworded is a data loss the caller did not ask for.
		done = {
			str(row.get("step") or ""): row
			for row in doc.get("items") or []
			if compat.checked(row.get("completed"))
		}
		doc.set("items", [])
		for row in _requested_items(args.get("items")):
			previous = done.get(row["step"])
			if previous is not None and not row["completed"]:
				row["completed"] = 1
				row["completed_by"] = previous.get("completed_by")
				row["completed_on"] = previous.get("completed_on")
				row["evidence"] = row["evidence"] or previous.get("evidence") or ""
			doc.append("items", row)
		changed.append("items")

	if not changed:
		raise ToolError(
			"nothing to update. Pass at least one of: period_start, period_end, period_type, "
			"fiscal_year, status, notes, items. To lock or unlock the period use "
			"close_accounting_period / reopen_accounting_period."
		)

	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return ToolResult(
		data={"checklist": _describe_checklist(doc), "changed": changed},
		summary=f"updated Closing Checklist {doc.name}: {', '.join(changed)}",
		docstatus_delta="0 → 0 (draft, edited)",
	)


def complete_checklist_item(args: dict) -> ToolResult:
	"""Tick one close step, with who and when. MUTATING."""
	_require_checklists()
	doc = _checklist_doc(as_str(args, "name", required=True))
	step = as_str(args, "step", required=True)

	rows = list(doc.get("items") or [])
	matches = [row for row in rows if str(row.get("step") or "").strip().lower() == step.strip().lower()]
	if not matches:
		known = [str(row.get("step") or "") for row in rows]
		raise ToolError(
			f"no step called {step!r} on Closing Checklist {doc.name}. Its steps are: "
			f"{'; '.join(known) or '<none>'}. Nothing was changed."
		)
	if len(matches) > 1:
		raise ToolError(
			f"{step!r} matches {len(matches)} steps on {doc.name}, so which one to tick is ambiguous. "
			"Give the steps distinct wording with update_closing_checklist. Nothing was changed."
		)

	row = matches[0]
	uncomplete = as_bool(args, "uncomplete", False)
	already = bool(compat.checked(row.get("completed")))
	# `.update()` RATHER THAN ATTRIBUTE ASSIGNMENT, because a child row is a
	# Frappe Document on a bench and a plain dict under the standalone double, and
	# `update` is the one mutation both answer to. `row.completed = 1` works on a
	# site and raises `AttributeError` in the suite — which would make this the
	# one path in the module that could only be tested by running a bench.
	if uncomplete:
		row.update({"completed": 0, "completed_by": None, "completed_on": None})
	else:
		if already:
			return ToolResult(
				data={"checklist": _describe_checklist(doc), "step": step, "already_done": True},
				summary=f"{step!r} on {doc.name} was already done — nothing changed",
			)
		changes = {
			"completed": 1,
			"completed_by": frappe.session.user,
			"completed_on": frappe.utils.now(),
		}
		evidence = as_str(args, "evidence")
		if evidence:
			changes["evidence"] = evidence
		notes = as_str(args, "notes")
		if notes:
			changes["notes"] = notes
		row.update(changes)

	if doc.status == "Open":
		doc.status = "In Progress"
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	described = _describe_checklist(doc)
	return ToolResult(
		data={"checklist": described, "step": step, "completed": not uncomplete},
		summary=(
			f"{'un-ticked' if uncomplete else 'completed'} {step!r} on {doc.name}; "
			f"{described['outstanding_count']} required step(s) still outstanding"
		),
		docstatus_delta="0 → 0 (draft, edited)",
	)


def close_accounting_period(args: dict) -> ToolResult:
	"""Lock a period against posting. MUTATING, and the control runs here.

	THE CONTROL IS `closing_checklist`, and what it does depends on the mode an
	operator set. Advisory: an outstanding required step is reported, filed as an
	alert against the period, and THE PERIOD STILL LOCKS. Enforced: the same
	finding refuses the close and nothing is written.
	"""
	_require_checklists()
	doc = _checklist_doc(as_str(args, "name", required=True))
	described = _describe_checklist(doc)

	if described["locked"]:
		raise ToolError(
			f"Closing Checklist {doc.name} is already locked (since {described['locked_on']}, by "
			f"{described['locked_by']}). reopen_accounting_period unlocks it. Nothing was changed."
		)

	findings = []
	for row in described["outstanding"]:
		findings.append(
			enforcement.Finding(
				control_point="closing_checklist",
				message=(
					f"{doc.period_type} close {doc.name} ({described['period_start']} to "
					f"{described['period_end']}) is being closed with the required step "
					f"{row['step']!r} outstanding."
				),
				remedy=(
					f"Do the step and record it with complete_checklist_item(name={doc.name!r}, "
					f"step={row['step']!r}), or mark it not required with update_closing_checklist "
					"if this operation genuinely does not do it."
				),
				source_doctype=CHECKLIST,
				source_docname=doc.name,
				company=doc.company,
				detail={"step": row["step"], "sequence": row["sequence"]},
			)
		)

	# Raises here in Enforced mode, before anything is written. In Advisory it
	# files the alerts and returns.
	control = enforcement.evaluate("closing_checklist", findings, company=doc.company)

	doc.locked = 1
	doc.locked_on = frappe.utils.now()
	doc.locked_by = frappe.session.user
	doc.status = "Closed"
	reason = as_str(args, "reason")
	if reason:
		doc.notes = f"{doc.notes}\n\nClosed: {reason}".strip() if doc.notes else f"Closed: {reason}"
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	after = _describe_checklist(doc)
	after["control"] = control
	after["lockdown_control"] = enforcement.status("period_close_lockdown")
	return ToolResult(
		data=after,
		summary=(
			f"locked {doc.period_type} close {doc.name} ({doc.company}) covering "
			f"{after['period_start']} to {after['period_end']}"
			+ (
				f" — {len(findings)} required step(s) were still outstanding"
				if findings
				else " — checklist clean"
			)
		),
		docstatus_delta="0 → 0 (draft, locked)",
	)


def reopen_accounting_period(args: dict) -> ToolResult:
	"""Unlock a closed period. MUTATING, and it demands a reason.

	NO CONTROL GATES THIS, deliberately. Reopening is not a thing to make hard —
	a period locked by mistake, or one that has to take a correcting entry an
	auditor asked for, is a normal event. What matters is that it is RECORDED:
	the status becomes `Reopened` rather than `Open`, which is a different word so
	that a month closed once and opened again is visible without diffing a version
	history, and the reason goes on the record.
	"""
	_require_checklists()
	doc = _checklist_doc(as_str(args, "name", required=True))
	reason = as_str(args, "reason", required=True)

	if not compat.checked(doc.locked):
		raise ToolError(
			f"Closing Checklist {doc.name} is not locked, so there is nothing to reopen. Nothing was changed."
		)

	was_locked_on = str(doc.locked_on or "")
	was_locked_by = doc.locked_by
	doc.locked = 0
	doc.status = "Reopened"
	doc.notes = f"{doc.notes}\n\nReopened: {reason}".strip() if doc.notes else f"Reopened: {reason}"
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	described = _describe_checklist(doc)
	return ToolResult(
		data={
			"checklist": described,
			"was_locked_on": was_locked_on or None,
			"was_locked_by": was_locked_by,
			"reason": reason,
			"note": (
				"The status is `Reopened` rather than `Open`. A period that has been closed once and "
				"opened again is a fact an auditor asks about, and it should be readable off the row "
				"rather than reconstructed from a version history."
			),
		},
		summary=f"reopened {doc.period_type} close {doc.name} ({doc.company}): {reason}",
		docstatus_delta="0 → 0 (draft, unlocked)",
	)


# ── the journal entry controls ──────────────────────────────────────────────
def _locked_periods(company: str) -> list[dict]:
	if not compat.doctype_exists(CHECKLIST):
		return []
	return [
		dict(row)
		for row in frappe.db.get_all(
			CHECKLIST,
			filters={"company": company, "locked": 1},
			fields=["name", "period_type", "period_start", "period_end", "locked", "locked_on", "locked_by"],
			limit=RECORD_CAP,
		)
		or []
	]


def _entry_accounts(name: str) -> list[str]:
	rows = frappe.db.get_all("Journal Entry Account", filters={"parent": name}, fields=["account"], limit=200)
	return sorted({str(row.get("account") or "") for row in rows or [] if row.get("account")})


def _comparable_entries(company: str, accounts: tuple, exclude: str = "") -> list[dict]:
	"""Recent entries on this company, with their accounts, for both JE controls.

	One read serving duplicate detection and the unusual-amount median, because
	they want the same rows and a journal entry is not a place to run two table
	scans. Cancelled entries are excluded — a cancelled entry is not on the books
	and is not something a new one can duplicate.
	"""
	filters = {"company": company, "docstatus": ("!=", 2)}
	if exclude:
		filters["name"] = ("!=", exclude)
	rows = frappe.db.get_all(
		"Journal Entry",
		filters=filters,
		fields=["name", "posting_date", "total_debit", "docstatus", "cheque_no"],
		order_by="posting_date desc",
		limit=HISTORY_CAP,
	)
	out = []
	for row in rows or []:
		out.append(
			{
				"name": row["name"],
				"posting_date": row.get("posting_date"),
				"total": float(row.get("total_debit") or 0),
				"docstatus": int(row.get("docstatus") or 0),
				"cheque_no": row.get("cheque_no") or "",
				"accounts": _entry_accounts(row["name"]),
			}
		)
	return out


def journal_entry_findings(
	company: str,
	posting_date,
	total: float,
	accounts: tuple,
	*,
	preparer: str = "",
	approver: str = "",
	exclude: str = "",
	source_docname: str = "",
	cheque_no: str = "",
) -> dict:
	"""Run all four journal entry controls. Returns `{control_point: [Finding]}`.

	Shared by `create_journal_entry`'s gate and by `check_journal_entry_controls`,
	so what a caller is told WOULD happen and what actually happens are computed
	by one function. A preview that used a second implementation would be a preview
	that could disagree with the thing it previews, which is the one property a
	preview must not have.
	"""
	out = {
		"period_close_lockdown": [],
		"journal_entry_duplicate": [],
		"journal_entry_unusual_amount": [],
		"segregation_of_duties": [],
		"approval_threshold": [],
	}

	# ── the closed period ───────────────────────────────────────────────────
	period = controls.locked_period(_locked_periods(company), posting_date)
	if period:
		out["period_close_lockdown"].append(
			enforcement.Finding(
				control_point="period_close_lockdown",
				message=(
					f"This posting is dated {posting_date}, inside {period['period_type']} period "
					f"{period['name']} ({period['period_start']} to {period['period_end']}), which was "
					f"locked on {period.get('locked_on')} by {period.get('locked_by')}."
				),
				remedy=(
					"Date the posting into an open period, or reopen that one with "
					f"reopen_accounting_period(name={period['name']!r}, reason=...) — which records "
					"that it was reopened and why."
				),
				source_doctype=JOURNAL_ENTRY,
				source_docname=source_docname,
				company=company,
				detail={
					"period": period["name"],
					"period_start": str(period["period_start"]),
					"period_end": str(period["period_end"]),
					"posting_date": str(posting_date),
				},
			)
		)

	# ── duplicates and unusual amounts share one read ───────────────────────
	history = _comparable_entries(company, accounts, exclude=exclude)

	duplicates = controls.duplicate_findings(
		{
			"posting_date": posting_date,
			"total": total,
			"accounts": accounts,
			"company": company,
			"cheque_no": cheque_no,
		},
		history,
	)
	for match in duplicates[:5]:
		out["journal_entry_duplicate"].append(
			enforcement.Finding(
				control_point="journal_entry_duplicate",
				message=(
					f"This entry looks like Journal Entry {match['name']}, already on the books: "
					f"{match['why']}."
				),
				remedy=(
					f"Open {match['name']} and confirm the two are different work. If they are the "
					"same, this one should not be written; if they are genuinely both real, say so "
					"in the remark so the next person does not have to work it out."
				),
				source_doctype=JOURNAL_ENTRY,
				source_docname=source_docname or match["name"],
				company=company,
				detail=match,
			)
		)

	# The median is taken over entries touching THE SAME ACCOUNTS. A median over
	# every entry on the company would compare a payroll accrual against a fuel
	# receipt and call one of them unusual every month.
	same_accounts = [row["total"] for row in history if accounts and set(row["accounts"]) == set(accounts)]
	verdict = controls.unusual_amount(total, same_accounts)
	if verdict["unusual"]:
		out["journal_entry_unusual_amount"].append(
			enforcement.Finding(
				control_point="journal_entry_unusual_amount",
				message=f"This entry's total of {verdict['amount']} is unusual: {verdict['basis']}",
				remedy=(
					"Check the decimal point and the units before this goes further. If the amount "
					"is right, nothing needs doing — the finding is a second pair of eyes, not an "
					"accusation."
				),
				source_doctype=JOURNAL_ENTRY,
				source_docname=source_docname,
				company=company,
				detail=verdict,
			)
		)

	# ── segregation of duties ───────────────────────────────────────────────
	if controls.same_hand(preparer, approver):
		out["segregation_of_duties"].append(
			enforcement.Finding(
				control_point="segregation_of_duties",
				message=(
					f"{preparer} both prepared and approved this transaction. One person who can "
					"write an entry and release it is the whole of what segregation of duties exists "
					"to prevent."
				),
				remedy=(
					"Have somebody else release it. Where the operation is genuinely too small for "
					"that — which is common and is not a failure — record the compensating control "
					"(a review of all entries by an owner, say) so the exception is a known one "
					"rather than a gap somebody finds."
				),
				source_doctype=JOURNAL_ENTRY,
				source_docname=source_docname,
				company=company,
				detail={"preparer": preparer, "approver": approver},
			)
		)

	# ── approval authority ──────────────────────────────────────────────────
	out["approval_threshold"].extend(
		approval_findings(company, "Journal Entry", total, source_docname=source_docname, actor=preparer)
	)
	return out


def approval_findings(
	company: str, document_type: str, amount: float, *, source_docname: str = "", actor: str = ""
) -> list:
	"""Findings from the approval chain for one transaction. Empty when it is covered.

	`actor` is who is booking it. When the site can tell us their roles we check
	whether they hold the one the chain names; when it cannot, the finding says
	what authority is REQUIRED rather than claiming the person lacks it — which is
	the honest reading, and a control that guessed would be wrong on every site
	with an unusual role layout.
	"""
	if not compat.doctype_exists(THRESHOLD):
		return []
	rows = frappe.db.get_all(
		THRESHOLD,
		filters={"company": company, "enabled": 1},
		fields=compat.existing_fields(THRESHOLD, _THRESHOLD_FIELDS),
		limit=RECORD_CAP,
	)
	if not rows:
		return []
	tables = []
	for row in rows or []:
		table = dict(row)
		table["levels"] = frappe.db.get_all(
			THRESHOLD_LEVEL,
			filters={"parent": row["name"]},
			fields=["level", "up_to_amount", "approver_role"],
			limit=50,
		)
		tables.append(table)

	threshold = controls.applicable_threshold(tables, document_type)
	if not threshold:
		return []

	verdict = controls.required_authority(threshold, amount)
	if not verdict["needed"]:
		return []

	held = _roles_of(actor) if actor else None

	if verdict.get("above_chain"):
		return [
			enforcement.Finding(
				control_point="approval_threshold",
				message=(
					f"{verdict['amount']} is above every rung of approval threshold "
					f"{threshold.get('threshold_name')} ({threshold.get('name')}), whose highest "
					f"ceiling is {verdict.get('ceiling')}. Nobody on that table may release it."
				),
				remedy=(
					"Add an uncapped top rung with update_approval_threshold, or raise the highest "
					"ceiling. A chain that stops below what the operation actually spends is a chain "
					"somebody will route around."
				),
				source_doctype=document_type,
				source_docname=source_docname,
				company=company,
				detail=verdict,
			)
		]

	role = verdict["approver_role"]
	if held is not None and role and role in held:
		return []

	if held is not None and role:
		message = (
			f"{verdict['amount']} needs release by {role!r} under approval threshold "
			f"{threshold.get('threshold_name')}, and {actor} does not hold that role."
		)
	else:
		message = (
			f"{verdict['amount']} needs release by {role!r} under approval threshold "
			f"{threshold.get('threshold_name')}, and this transaction carries no record of that "
			"release."
		)
	return [
		enforcement.Finding(
			control_point="approval_threshold",
			message=message,
			remedy=(
				f"Have somebody holding {role!r} release it, or — if the limit is wrong for this "
				"operation — move it with update_approval_threshold rather than working around it."
			),
			source_doctype=document_type,
			source_docname=source_docname,
			company=company,
			detail={**verdict, "actor": actor or None, "actor_roles": sorted(held) if held else None},
		)
	]


def _roles_of(user: str):
	"""The roles a user holds, or None when the site cannot say.

	None and an empty set mean different things and the caller branches on it:
	None is "we could not read the roles", an empty set is "this account holds
	none". Reporting the first as the second would accuse every user on a site
	whose role table this app cannot read.
	"""
	user = str(user or "").strip()
	if not user:
		return None
	try:
		rows = frappe.db.get_all("Has Role", filters={"parent": user}, fields=["role"], limit=100)
	except Exception:
		return None
	if rows is None:
		return None
	return {str(row.get("role") or "") for row in rows if row.get("role")}


def journal_entry_gate(
	company: str,
	posting_date,
	total: float,
	accounts: tuple,
	*,
	preparer: str = "",
	approver: str = "",
	source_docname: str = "",
	cheque_no: str = "",
) -> dict:
	"""Run every journal entry control. THE CALL `create_journal_entry` MAKES.

	Each control is evaluated through its OWN switch, so an operation can enforce
	period locks while leaving duplicate detection advisory — which is the normal
	shape of a company growing into this, and would be impossible if the five
	controls shared one toggle.

	Raises `ToolError` at the first ENFORCED control with a finding. Ordered so
	that the refusal a caller gets is the most fundamental one: there is no point
	telling somebody their entry might be a duplicate if it is also dated into a
	period that is closed.

	NEVER FAILS THE CALLER ON ITS OWN BUG. A control that cannot read the site is
	reported as unevaluated and the work goes through. That is the same trade
	`_file_alerts` makes and for the same reason — an app whose ledger locks up
	because its own control raised is worse than the risk the control watches for.
	"""
	try:
		findings = journal_entry_findings(
			company,
			posting_date,
			total,
			accounts,
			preparer=preparer,
			approver=approver,
			source_docname=source_docname,
			cheque_no=cheque_no,
		)
	except ToolError:
		raise
	except Exception as exc:  # pragma: no cover - a site whose control tables are unreadable
		return {
			"evaluated": False,
			"note": (
				f"The financial controls could not be evaluated ({type(exc).__name__}: {exc}) and the "
				"work was allowed through. This is a fault in the controls, not a finding about the "
				"entry — but it does mean this entry passed no control, which is worth knowing."
			),
		}

	out = {"evaluated": True, "controls": {}, "findings": [], "blocked_by": None}
	for control_point in (
		"period_close_lockdown",
		"approval_threshold",
		"segregation_of_duties",
		"journal_entry_duplicate",
		"journal_entry_unusual_amount",
	):
		block = enforcement.evaluate(control_point, findings.get(control_point) or [], company=company)
		out["controls"][control_point] = block
		out["findings"].extend(block.get("findings") or [])
	out["finding_count"] = len(out["findings"])
	out["clear"] = not out["findings"]
	return out


def check_journal_entry_controls(args: dict) -> ToolResult:
	"""What the controls would say about an entry. Read-only — nothing is written.

	THE PREVIEW, and it exists because the honest way to turn a control up is to
	look first. It runs the same evaluation `create_journal_entry` runs, against
	the same switches, and reports what each control found — but it refuses
	nothing and files no alert, so asking is free.
	"""
	company = resolve_company(as_str(args, "company"), required=True)
	posting_date = as_date(args, "posting_date", required=True)

	name = as_str(args, "journal_entry")
	if name:
		if not frappe.db.exists(JOURNAL_ENTRY, name):
			raise ToolError(f"no Journal Entry called {name!r} on this site.")
		row = frappe.db.get_value(
			JOURNAL_ENTRY,
			name,
			["company", "posting_date", "total_debit", "owner", "cheque_no"],
			as_dict=True,
		)
		company = row["company"]
		posting_date = str(row["posting_date"])
		total = float(row["total_debit"] or 0)
		accounts = tuple(_entry_accounts(name))
		preparer = as_str(args, "preparer") or row.get("owner") or ""
		cheque_no = as_str(args, "cheque_no") or row.get("cheque_no") or ""
	else:
		total = float(args.get("total") or 0)
		if not total:
			raise ToolError(
				"pass either journal_entry (to check one already on the site) or total plus "
				"accounts (to check one before it is written)."
			)
		raw = args.get("accounts") or []
		if not isinstance(raw, list):
			raise ToolError('accounts must be a list of account names, e.g. ["1100 - Cash - ETC"]')
		accounts = tuple(sorted({str(entry).strip() for entry in raw if str(entry).strip()}))
		preparer = as_str(args, "preparer")
		cheque_no = as_str(args, "cheque_no")

	approver = as_str(args, "approver")

	findings = journal_entry_findings(
		company,
		posting_date,
		total,
		accounts,
		preparer=preparer,
		approver=approver,
		exclude=name,
		source_docname=name,
		cheque_no=cheque_no,
	)

	blocks = {}
	would_block = []
	for control_point, rows in findings.items():
		# `raise_on_enforced=False` is the whole point of this tool: the same
		# evaluation, reported rather than acted on.
		block = enforcement.evaluate(control_point, [], company=company, raise_on_enforced=False)
		block["findings"] = [finding.as_dict() for finding in rows]
		block["finding_count"] = len(rows)
		block["clear"] = not rows
		block["would_block"] = bool(rows) and block["mode"] == enforcement.ENFORCED
		if block["would_block"]:
			would_block.append(control_point)
		blocks[control_point] = block

	total_findings = sum(len(rows) for rows in findings.values())
	return ToolResult(
		data={
			"company": company,
			"posting_date": str(posting_date),
			"total": total,
			"accounts": list(accounts),
			"preparer": preparer or None,
			"approver": approver or None,
			"controls": blocks,
			"finding_count": total_findings,
			"would_block": would_block,
			"clear": not total_findings,
			"note": (
				"NOTHING WAS WRITTEN AND NO ALERT WAS FILED. This is the same evaluation "
				"create_journal_entry runs, read-only — which is what makes it the right way to find "
				"out what turning a control up would cost before turning it up."
			),
		},
		summary=(
			f"{total_findings} control finding(s) for an entry of {total} on {posting_date} ({company})"
			+ (
				f"; {len(would_block)} control(s) would refuse it"
				if would_block
				else "; nothing would be refused"
			)
		),
	)


def list_control_points(args: dict) -> ToolResult:
	"""Every control this app implements, and how hard each one pushes here. Read-only.

	THE REGISTER AN OPERATOR READS TO ANSWER 'what does this system stop me
	doing'. It is generated from `enforcement.CONTROL_POINTS` joined to the live
	Compliance Rule rows, so the answer here and the behaviour at the moment of a
	transaction cannot drift.
	"""
	rows = enforcement.describe_all()
	enforced = [row for row in rows if row["enforced"]]
	advisory = [row for row in rows if row["mode"] == enforcement.ADVISORY]
	off = [row for row in rows if row["mode"] == enforcement.OFF]
	return ToolResult(
		data={
			"controls": rows,
			"control_count": len(rows),
			"enforced": [row["control_point"] for row in enforced],
			"advisory": [row["control_point"] for row in advisory],
			"off": [row["control_point"] for row in off],
			"note": (
				"ADVISORY is the shipped default for every control and is not a weaker version of "
				"the same thing — it is the SAME evaluation, filing the SAME finding to the same "
				"calendar, without the refusal. An operation running Advisory for a season ends it "
				"holding exactly the register of findings it would hold had it been enforcing, which "
				"is what makes turning a control up a decision with evidence behind it. Flip one "
				"with update_compliance_rule(name=<rule>, enforcement_mode='Enforced'), which "
				"version-copies the rule and therefore records the date enforcement began and who "
				"decided it."
			),
		},
		summary=(
			f"{len(rows)} control point(s): {len(enforced)} enforced, {len(advisory)} advisory, "
			f"{len(off)} off"
		),
	)
