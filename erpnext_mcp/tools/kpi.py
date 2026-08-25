# SPDX-License-Identifier: MIT
"""The nine tools behind Sustainable CF/Acre and the window standard.

v0.19.5 brought six — propose, approve, refuse, classify, list, read — and
v0.19.6 brings three more: the generic windowed entry point, the cache reader
and the cache rebuilder. They share this module because they share the gate: the
same three roles, the same company scope, and the same argument about who is
allowed to move a figure a lender reads.

FIVE OF THE ORIGINAL SIX EXIST TO KEEP AN AI ON THE PROPOSING SIDE OF A LINE.

An AI is genuinely good at the first half of a normalization. The items are
scattered — an insurance recovery three months after the hailstorm, a settlement
booked to a miscellaneous income account, a working-capital swing that is really
a customer paying two days either side of a quarter end — and nobody reads a
ledger line by line looking for them. Finding them is worth a great deal.

Deciding is a different act. Whether a hailstorm in a region that hails every
third year is non-recurring is a judgement with a lender on the other end of it,
and it will be argued across a table by a person who has to defend it. So
`create_normalization_adjustment` produces a DRAFT and can produce nothing else;
`approve_normalization_adjustment` is a separate tool, with a separate switch,
that takes a signature and writes a timestamp it will not take as input. It is
the same split `record_training` makes between filing the record and signing the
supervisor's review, and it is this app's standing position: AI proposes, human
approves.

────────────────────────────────────────────────────────────────────────────
THE ROLE GATE IS NOT THE HR ONE
────────────────────────────────────────────────────────────────────────────

`employee.require_hr_role` gates the personnel register on HR Manager, HR User
and Farm Manager, and none of those is who signs off a normalization. This gate
is Accounts Manager, Farm Manager and System Manager: the accountant who has to
defend the add-back, the manager who knows whether the pump was a replacement,
and the operator. An HR User who can file a training record has no business
moving the number a lender reads, and the two lists are separate so that stays
true when either one is widened.

Company scope is the shared one — Frappe's rule, where a principal with no
Company User Permission is unrestricted, and a scoped principal is held to it.
Every tool here checks it, because a normalization on the wrong entity flatters
one set of books with another entity's insurance claim.

────────────────────────────────────────────────────────────────────────────
`backfill_asset_capex_type` IS DRY BY DEFAULT AND SAYS WHAT IT WOULD DO
────────────────────────────────────────────────────────────────────────────

Classifying the history is a real problem — an operation adopting this in year
nine has nine years of assets with no `capex_type` — and the honest heuristic is
narrow: everything bought before somebody started tracking is generally
maintenance, because it is the existing productive plant carrying on. The tool
applies exactly that heuristic, only to rows with NO classification, and never
overwrites one somebody made. It is idempotent by construction: a second run
finds nothing to do.

IT IS A STARTING POSITION, NOT AN ANSWER. The report says so, and the new block
planted in year six was growth and will be wrong until somebody fixes it in the
Desk. That is a better place to start from than nine years of nulls, and worse
than nine years of real classifications — both of which are true and both of
which the result says.

────────────────────────────────────────────────────────────────────────────
v0.19.6: THE WINDOW IS NOW THE DEFAULT, AND THE OLD SHAPE STILL ANSWERS
────────────────────────────────────────────────────────────────────────────

`get_sustainable_cf_per_acre` used to require a period and return one. It now
returns the trailing twelve months by default, with the period just finished
beside it and five years of history under both. A caller that passes
`period_start` and `period_end` — v0.19.5's signature — still gets v0.19.5's
payload, exactly, with a deprecation sentence added to its warnings.

THAT COMPATIBILITY IS NOT POLITENESS. This figure is quoted in lender packs and
pasted into spreadsheets, and a release that changed what an unchanged call
returned would silently alter a number somebody had already sent to a bank.

`recompute_kpi_history` IS THE THIRD MUTATING TOOL HERE AND THE MILDEST THING IN
THE FILE. It writes only a cache, every row of which is derivable from the
ledger by rerunning the computer that wrote it, so the worst outcome of running
it at the wrong moment is time spent. It is behind the same role gate as the
rest anyway — a KPI history somebody can rebuild is a KPI history somebody can
rebuild while a pack is being read off it.
"""

from __future__ import annotations

import json

import frappe

from .. import compat, roles, security
from .. import kpi as kpi_module
from ..args import as_bool, as_choice, as_date, as_int, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult

# Imported for its side effect as well as its names: importing it REGISTERS the
# three windowed computers. A tool module that reached `windowed_reports.run`
# without this import would find an empty registry and refuse every report on
# the grounds that none is registered, which is a confusing way to spell "the
# module that registers them was never imported".
from ..services import financial_reports  # noqa: F401
from ..services import sustainable_cf_per_acre as service
from ..services import windowed_reports as windows
from . import employee as employee_tool
from . import shifts as shift_tools

DOCTYPE = kpi_module.DOCTYPE

RECORD_CAP = 500

#: Most Assets one backfill call will classify. Past this the caller is
#: reclassifying a database rather than a farm's history, and should be doing it
#: in batches whose report they can actually read.
BACKFILL_CAP = 5000

#: Who may propose, approve or refuse a normalization, and who may reclassify
#: capex. NOT the HR list — see the module docstring. `Administrator` holds every
#: role Frappe has, so the default configuration passes; an operator who pointed
#: `mcp_system_user` at a purpose-built account gets a refusal naming the account
#: and these three, because "permission denied" on a principal the operator chose
#: is a one-line fix they cannot make without knowing which line.
KPI_ROLES = ("System Manager", "Accounts Manager", "Farm Manager")


def _require() -> None:
	compat.require_doctype(
		DOCTYPE,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def require_kpi_role() -> str:
	"""The principal this call is attributed to, once it has proved it may sign off money.

	Same shape and same identity resolution as `employee.require_hr_role` —
	whichever of the request's authenticated caller and the session user is
	present — against a different list. See the module docstring on why the two
	lists are separate rather than one widened.
	"""
	actor = security.caller_identity() or str(getattr(frappe.session, "user", "") or "")
	if not actor or actor == "Guest":
		raise ToolError(
			"this call has no identity to attribute a normalization to. A normalization is a "
			"judgement somebody signs, and an unattributable one is the thing this doctype "
			"exists to prevent. Nothing was changed."
		)
	held = set(frappe.get_roles(actor) or []) or set(roles.all_roles_of(actor) or [])
	if not held & set(KPI_ROLES):
		raise ToolError(
			f"{actor} may not touch the normalization register or the capex classification: it "
			f"holds none of {', '.join(KPI_ROLES)}. These adjustments move the cash flow figure "
			"a lender reads, so the gate is the accountant, the farm manager and the operator "
			"rather than the wider personnel list. This is the account this app acts as — an "
			"operator sets it with `mcp_system_user` on ERPNext MCP Settings, and grants it a "
			"role in the Desk. Nothing was changed."
		)
	return actor


def _resolve_adjustment(args: dict) -> dict:
	name = (
		as_str(args, "name")
		or as_str(args, "normalization_adjustment")
		or as_str(args, "adjustment", required=True)
	).strip()
	if not frappe.db.exists(DOCTYPE, name):
		raise ToolError(
			f"no {DOCTYPE} called {name!r} on this site. list_normalization_adjustments has the "
			"register; a docname looks like NADJ-2026-0001. Nothing was changed."
		)
	return dict(
		frappe.db.get_value(DOCTYPE, name, compat.existing_fields(DOCTYPE, kpi_module.FIELDS), as_dict=True)
		or {}
	)


def _scoped_companies(actor: str, args: dict) -> dict:
	"""Company filter for a read: the one asked for, or every one this actor may see."""
	filters: dict = {}
	company = resolve_company(as_str(args, "company"), required=False)
	if company:
		employee_tool.require_company_scope(actor, company)
		filters["company"] = company
	else:
		allowed = shift_tools._readable_companies(actor)
		if allowed:
			filters["company"] = ("in", allowed)
	return {"company": company, "filters": filters}


# ── 1. create_normalization_adjustment ──────────────────────────────────────
def create_normalization_adjustment(args: dict) -> ToolResult:
	"""Propose one add-back or subtraction on operating cash flow. Draft, always."""
	_require()
	actor = require_kpi_role()

	company = resolve_company(as_str(args, "company"), required=True)
	employee_tool.require_company_scope(actor, company)

	fiscal_year = as_str(args, "fiscal_year", required=True)
	if not frappe.db.exists("Fiscal Year", fiscal_year):
		known = frappe.db.get_all("Fiscal Year", pluck="name", limit=25)
		raise ToolError(
			f"no Fiscal Year named {fiscal_year!r} on this site. This site has: "
			f"{', '.join(sorted(str(name) for name in known)) or '<none>'}. A normalization is "
			"defended inside a closed set of books, so it has to name the year it belongs to. "
			"Nothing was created."
		)

	period_start = as_date(args, "period_start", required=True)
	period_end = as_date(args, "period_end", required=True)
	if period_end < period_start:
		raise ToolError(
			f"period_end ({period_end}) is before period_start ({period_start}). Nothing was created."
		)

	amount = args.get("amount")
	try:
		amount = float(amount)
	except (TypeError, ValueError):
		raise ToolError("amount is required and must be a number. Nothing was created.") from None
	if amount <= 0:
		raise ToolError(
			"amount must be POSITIVE. The direction of the adjustment is the `direction` "
			"argument — 'Add-back to OCF' or 'Subtract from OCF' — and keeping the sign out of "
			"the amount is deliberate: a negative amount beside a Subtract is a double negative, "
			"and a double negative is how an adjustment ends up moving the number the wrong way "
			"in a pack somebody is borrowing against. Nothing was created."
		)

	direction = as_choice(DOCTYPE, "direction", as_str(args, "direction", required=True), "direction")
	category = as_choice(DOCTYPE, "category", as_str(args, "category", required=True), "category")

	justification = as_str(args, "justification", required=True)
	if len(justification.strip()) < kpi_module.MIN_JUSTIFICATION:
		raise ToolError(
			f"the justification is {len(justification.strip())} character(s) and has to be at "
			f"least {kpi_module.MIN_JUSTIFICATION}. THIS IS NOT A LENGTH REQUIREMENT DRESSED UP "
			"AS A QUALITY ONE — no character count is a quality bar. It is a floor under "
			"'one-time' and 'per Tim', which are what gets written when the field is merely "
			"required, and both of which an auditor reads as an admission that nobody thought "
			"about it. What the sentence has to answer is the question every buyer asks: WHY "
			"WILL THIS NOT HAPPEN AGAIN? A hailstorm in a region that hails every third year is "
			"not non-recurring, and this is where that gets settled before the number reaches a "
			"lender. Nothing was created."
		)

	supporting = shift_tools.file_reference(
		as_str(args, "supporting_document_file_token") or as_str(args, "supporting_document"),
		"supporting_document_file_token",
	)

	doc = frappe.new_doc(DOCTYPE)
	doc.company = company
	doc.fiscal_year = fiscal_year
	doc.period_start = period_start
	doc.period_end = period_end
	doc.amount = amount
	doc.direction = direction
	doc.category = category
	doc.justification = justification
	doc.supporting_document = supporting
	doc.status = kpi_module.STATUS_DRAFT
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	described = kpi_module.describe(dict(doc.as_dict()))
	competing = [
		row["name"]
		for row in kpi_module.rows(
			{
				"company": company,
				"period_start": period_start,
				"period_end": period_end,
				"category": category,
				"status": kpi_module.STATUS_APPROVED,
				"name": ("!=", doc.name),
			},
			limit=5,
		)
	]

	data = {
		**described,
		"actor": actor,
		"note": (
			f"{doc.name} is a DRAFT and does NOT count towards Sustainable CF/Acre. Only an "
			"Approved adjustment does. That split is the whole compliance posture of this "
			"doctype: finding a non-recurring item in a ledger nobody reads line by line is "
			"worth a great deal and is something an AI is good at; deciding that it will not "
			"recur is a judgement with a lender on the other end of it. "
			"approve_normalization_adjustment takes the signature."
		),
		"next_step": (
			f"approve_normalization_adjustment(name={doc.name!r}, approver_signature_file_token=…) "
			f"to accept the justification, or reject_normalization_adjustment(name={doc.name!r}, "
			"rejection_reason=…) to refuse it — a rejection with a reason is worth keeping and "
			"teaches the next proposal."
		),
	}
	if competing:
		data["duplicate_warning"] = (
			f"{competing[0]} is ALREADY an approved {category} adjustment for {company} covering "
			f"{period_start} to {period_end}. This draft can be created, and approving it will "
			f"be refused: two approved adjustments for one company, period and category are two "
			"answers to one question. If this one corrects the other, approve this and then set "
			f"{competing[0]}'s Superseded By to this record with status Superseded — that leaves "
			"the trail of what was believed before, which is the half of a restatement anybody "
			"actually wants."
		)
	if not supporting:
		data["evidence_note"] = (
			"No supporting document is attached. Not required — the paper does not always exist "
			"on the day — and it is the single most persuasive thing in the record when it does. "
			"The insurance claim determination, the settlement agreement, the board minute."
		)
	return ToolResult(
		data=data,
		summary=(
			f"drafted {doc.name}: {direction} of {amount} for {company}, {category}, "
			f"{period_start} to {period_end} (NOT counted until approved)"
		),
		docstatus_delta="none → draft (status Draft)",
	)


# ── 2. list_normalization_adjustments ───────────────────────────────────────
def list_normalization_adjustments(args: dict) -> ToolResult:
	"""The normalization register: what has been proposed, approved and refused."""
	_require()
	actor = require_kpi_role()
	limit = min(as_limit(args), RECORD_CAP)

	scope = _scoped_companies(actor, args)
	filters = scope["filters"]

	fiscal_year = as_str(args, "fiscal_year")
	if fiscal_year:
		filters["fiscal_year"] = fiscal_year
	status = as_str(args, "status")
	if status:
		filters["status"] = as_choice(DOCTYPE, "status", status, "status")

	found = kpi_module.rows(filters, limit=max(limit * 2, limit))
	described = [kpi_module.describe(row) for row in found]
	truncated = len(described) > limit
	described = described[:limit]

	approved = [entry for entry in described if entry["status"] == kpi_module.STATUS_APPROVED]
	unsigned = [entry["name"] for entry in approved if not entry["has_approver_signature"]]
	awaiting = [
		entry["name"]
		for entry in described
		if entry["status"] in (kpi_module.STATUS_DRAFT, kpi_module.STATUS_PENDING)
	]

	data = {
		"company": scope["company"],
		"count": len(described),
		"limit": limit,
		"truncated": truncated,
		"records": described,
		"counted_in_the_kpi": [entry["name"] for entry in approved],
		"awaiting_a_decision": awaiting,
		"totals_of_approved": kpi_module.signed_total(approved),
		"note": (
			f"{len(approved)} of {len(described)} adjustment(s) here are Approved and therefore "
			"count towards Sustainable CF/Acre. Drafts, pending proposals, rejections and "
			"superseded rows are all in the register and none of them moves the number."
		),
	}
	if awaiting:
		data["awaiting_note"] = (
			f"{len(awaiting)} adjustment(s) are waiting on somebody. A proposal nobody has "
			"decided is not a neutral state at quarter end — it is a figure that will change "
			"after the pack goes out."
		)
	if unsigned:  # pragma: no cover - the doctype refuses this on save
		data["signature_note"] = (
			f"{len(unsigned)} approved adjustment(s) carry no approver signature: "
			+ ", ".join(unsigned)
			+ ". These predate the signature rule or were written directly to the database."
		)
	if truncated:
		data["truncation_note"] = (
			f"More than {limit} record(s) matched and this is the first {limit}. Narrow by "
			"company, fiscal year or status before relying on the totals above."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{len(described)} normalization adjustment(s)"
			+ (f" for {scope['company']}" if scope["company"] else "")
			+ f"; {len(approved)} approved, {len(awaiting)} awaiting a decision"
		),
	)


# ── 3. approve_normalization_adjustment ─────────────────────────────────────
def approve_normalization_adjustment(args: dict) -> ToolResult:
	"""Accept the justification, with a signature and a timestamp nobody typed."""
	_require()
	actor = require_kpi_role()
	row = _resolve_adjustment(args)
	company = str(row.get("company") or "")
	employee_tool.require_company_scope(actor, company)

	if row.get("status") == kpi_module.STATUS_APPROVED:
		raise ToolError(
			f"{row['name']} is already Approved (on {row.get('approved_on')}). Approving twice "
			"would rewrite the timestamp on a decision somebody already made. Nothing was changed."
		)
	if row.get("status") == kpi_module.STATUS_SUPERSEDED:
		raise ToolError(
			f"{row['name']} has been superseded by {row.get('superseded_by')} and is the record "
			"of what was believed before. Approve the correction instead. Nothing was changed."
		)

	signature = shift_tools.file_reference(
		as_str(args, "approver_signature_file_token") or as_str(args, "approver_signature"),
		"approver_signature_file_token",
	)
	if not signature:
		raise ToolError(
			"approver_signature_file_token is required. THE SIGNATURE IS WHAT APPROVAL MEANS — "
			"the entire argument for this doctype is that a normalization is a judgement with "
			"somebody's name against it, and a status set without one is the status without the "
			"name. Every buyer and lender who reads the resulting figure will test the "
			"adjustments one at a time, and an unsigned add-back is the one they stop at. Upload "
			"the signature with stage_file_chunk and commit_staged_file, then pass its File "
			"docname. Nothing was changed."
		)

	approver = as_str(args, "approver_employee")
	employee = employee_tool.resolve_employee(approver) if approver else _employee_of(actor)

	duplicate = kpi_module.rows(
		{
			"company": company,
			"period_start": row.get("period_start"),
			"period_end": row.get("period_end"),
			"category": row.get("category"),
			"status": kpi_module.STATUS_APPROVED,
			"name": ("!=", row["name"]),
		},
		limit=2,
	)
	if duplicate:
		raise ToolError(
			f"{duplicate[0]['name']} is already an approved {row.get('category')} adjustment for "
			f"{company} covering {row.get('period_start')} to {row.get('period_end')}. TWO "
			"APPROVED ADJUSTMENTS FOR ONE COMPANY, PERIOD AND CATEGORY ARE TWO ANSWERS TO ONE "
			"QUESTION, and the one a reader finds will be whichever sorted first. If this one "
			f"corrects {duplicate[0]['name']}, supersede it: set its Superseded By to "
			f"{row['name']} and its status to {kpi_module.STATUS_SUPERSEDED}, which leaves the "
			"trail of what was believed before. Nothing was changed."
		)

	doc = frappe.get_doc(DOCTYPE, row["name"])
	doc.status = kpi_module.STATUS_APPROVED
	doc.approver_signature = signature
	doc.approved_by = employee or None
	# Never taken as input. An approval date somebody can set is an approval date
	# somebody can set to before the quarter closed, which is exactly the thing the
	# timestamp exists to prove.
	doc.approved_on = frappe.utils.now()
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	# v0.19.6. THE APPROVAL IS RETROACTIVE AND THE CACHE HAS TO KNOW. Every cached
	# window overlapping this adjustment's period was computed without it and is
	# now wrong by exactly this amount — so those snapshots are DELETED and the
	# next read or the next overnight sweep rebuilds them. Deleted rather than
	# flagged, because a cached row carries the components list as well as the
	# figure, and a components list that no longer produces the number above it is
	# worse than a missing one. It never raises: an approval is a compliance act
	# and losing one to housekeeping would be indefensible.
	invalidated = windows.invalidate_overlapping(company, row.get("period_start"), row.get("period_end"))

	described = kpi_module.describe(dict(doc.as_dict()))
	data = {
		**described,
		"actor": actor,
		"note": (
			f"{doc.name} now counts towards Sustainable CF/Acre for {company} over "
			f"{described['period_start']} to {described['period_end']}: "
			f"{described['signed_effect_on_ocf']} on operating cash flow. The justification "
			"and the signature travel with it into every computation — "
			"get_sustainable_cf_per_acre itemizes both, because a figure whose adjustments "
			"cannot be inspected is indistinguishable from one somebody arranged."
		),
		"justification_on_the_record": described["justification"],
		"cache_invalidation": invalidated,
	}
	if invalidated.get("deleted"):
		data["cache_note"] = (
			f"{invalidated['deleted']} cached KPI snapshot(s) covering this period were "
			"INVALIDATED. Each was computed before this approval existed and is now wrong by "
			f"exactly {described['signed_effect_on_ocf']}, so they are dropped rather than left to "
			"be read — a stale figure with an itemized ingredient list is the most expensive kind "
			"of wrong. They rebuild on the next read of a windowed report, or overnight; "
			f"recompute_kpi_history(kpi_key='sustainable_cf_per_acre', company={company!r}) "
			"rebuilds them now if a pack is going out today."
		)
	return ToolResult(
		data=data,
		summary=(
			f"approved {doc.name} — {described['direction']} of {described['amount']} for "
			f"{company}, signed by {employee or actor}"
			+ (
				f"; {invalidated['deleted']} cached snapshot(s) invalidated"
				if invalidated.get("deleted")
				else ""
			)
		),
		docstatus_delta=f"{row.get('status') or kpi_module.STATUS_DRAFT} → {kpi_module.STATUS_APPROVED}",
	)


def _employee_of(actor: str) -> str:
	"""The Employee record linked to the acting user, or "" where there is none.

	Empty is a working answer rather than a failure. On the ordinary MCP path the
	actor is the MCP System User, which is a service principal and has no Employee
	row by design — and refusing an approval because the app's own account is not
	on the payroll would make the tool unusable in exactly its normal
	configuration. The signature is the identity that matters; `approved_by` is
	the convenience, and a caller who wants it filled passes `approver_employee`.
	"""
	if not compat.doctype_exists("Employee") or not compat.has_field("Employee", "user_id"):
		return ""
	try:
		return str(frappe.db.get_value("Employee", {"user_id": actor}, "name") or "")
	except Exception:
		return ""


# ── 4. reject_normalization_adjustment ──────────────────────────────────────
def reject_normalization_adjustment(args: dict) -> ToolResult:
	"""Refuse the justification, on the record, with the reason attached."""
	_require()
	actor = require_kpi_role()
	row = _resolve_adjustment(args)
	employee_tool.require_company_scope(actor, str(row.get("company") or ""))

	reason = as_str(args, "rejection_reason", required=True)
	if not reason.strip():
		raise ToolError("rejection_reason is required. Nothing was changed.")

	if row.get("status") == kpi_module.STATUS_APPROVED:
		raise ToolError(
			f"{row['name']} is Approved and has been counted. Rejecting it now would rewrite a "
			"decision rather than record one. Supersede it instead: create the corrected "
			"adjustment, approve that, and set this one's Superseded By to it with status "
			f"{kpi_module.STATUS_SUPERSEDED} — which leaves the trail of what was believed "
			"before. Nothing was changed."
		)

	doc = frappe.get_doc(DOCTYPE, row["name"])
	doc.status = kpi_module.STATUS_REJECTED
	doc.rejection_reason = reason
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	described = kpi_module.describe(dict(doc.as_dict()))
	return ToolResult(
		data={
			**described,
			"actor": actor,
			"note": (
				f"{doc.name} is Rejected and does not count towards Sustainable CF/Acre. It is "
				"KEPT rather than deleted, for the same reason a rejected insurance claim is "
				"kept: a refusal with a reason teaches the next proposal, and a register with "
				"only its successes in it tells a reader nothing about how hard the successes "
				"were to get."
			),
		},
		summary=f"rejected {doc.name} — {reason[:80]}",
		docstatus_delta=f"{row.get('status') or kpi_module.STATUS_DRAFT} → {kpi_module.STATUS_REJECTED}",
	)


# ── 5. backfill_asset_capex_type ────────────────────────────────────────────
def backfill_asset_capex_type(args: dict) -> ToolResult:
	"""Classify unclassified Assets in bulk. Dry by default, idempotent, never overwrites."""
	_require()
	actor = require_kpi_role()

	if not compat.doctype_exists(kpi_module.ASSET_DOCTYPE):
		raise ToolError(
			"this site has no Asset doctype, so there is no fixed-asset register to classify. "
			"Nothing was changed."
		)
	if not kpi_module.capex_installed():
		raise ToolError(
			f"this site's Asset has no {kpi_module.CAPEX_TYPE_FIELD} column yet. It arrives with "
			"the `bench migrate` that installs v0.19.5 — run that first, then this. Nothing was "
			"changed."
		)

	default = as_str(args, "default_capex_type") or kpi_module.CAPEX_MAINTENANCE
	capex_type = next((option for option in kpi_module.CAPEX_TYPES if option.lower() == default.lower()), "")
	if not capex_type:
		raise ToolError(
			f"default_capex_type must be one of: {', '.join(kpi_module.CAPEX_TYPES)}. Got "
			f"{default!r}. Nothing was changed."
		)
	if capex_type == kpi_module.CAPEX_MIXED:
		raise ToolError(
			"Mixed cannot be a bulk default: the split between maintenance and growth is a "
			"per-purchase judgement about one invoice, and applying one to a hundred assets "
			"would be inventing a hundred splits. Backfill as Maintenance or Growth and correct "
			"the mixed purchases individually. Nothing was changed."
		)

	cutoff = as_date(args, "cutoff_purchase_date")
	dry_run = as_bool(args, "dry_run", True)

	company = resolve_company(as_str(args, "company"), required=False)
	filters: dict = {kpi_module.CAPEX_TYPE_FIELD: ("in", (None, ""))}
	if company:
		employee_tool.require_company_scope(actor, company)
		filters["company"] = company
	else:
		allowed = shift_tools._readable_companies(actor)
		if allowed:
			filters["company"] = ("in", allowed)
	if cutoff:
		filters["purchase_date"] = ("<", cutoff)

	rows = frappe.db.get_all(
		kpi_module.ASSET_DOCTYPE,
		filters=filters,
		fields=compat.existing_fields(
			kpi_module.ASSET_DOCTYPE,
			("name", "asset_name", "company", "purchase_date", "gross_purchase_amount"),
		),
		order_by="purchase_date asc, name asc",
		limit=BACKFILL_CAP,
	)

	classified, failed = [], []
	total = 0.0
	for row in rows or []:
		gross = round(float(row.get("gross_purchase_amount") or 0), 2)
		split = kpi_module.split_for(capex_type, gross, None, None)
		entry = {
			"asset": row.get("name"),
			"asset_name": row.get("asset_name"),
			"company": row.get("company"),
			"purchase_date": str(row.get("purchase_date") or "") or None,
			"gross_purchase_amount": gross,
			"capex_type": capex_type,
			"maintenance_portion": split["maintenance"],
			"growth_portion": split["growth"],
		}
		if not dry_run:
			try:
				_write_capex(row.get("name"), capex_type, split)
			except Exception as exc:
				failed.append({"asset": row.get("name"), "reason": f"{type(exc).__name__}: {exc}"})
				continue
		classified.append(entry)
		if capex_type == kpi_module.CAPEX_MAINTENANCE:
			total += split["maintenance"]

	data = {
		"actor": actor,
		"dry_run": bool(dry_run),
		"default_capex_type": capex_type,
		"cutoff_purchase_date": cutoff,
		"company": company,
		"considered": len(rows or []),
		"classified": len(classified),
		"failed": failed,
		"maintenance_capex_added": round(total, 2),
		"assets": classified[:200],
		"truncated": len(classified) > 200,
		"heuristic": (
			"Everything bought before the operation started tracking is generally MAINTENANCE: "
			"it is the existing productive plant carrying on. That is the whole of the rule, it "
			"is applied only to assets with NO classification, and it never overwrites one "
			"somebody made — so a second run finds nothing to do."
		),
		"note": (
			"A STARTING POSITION, NOT AN ANSWER. The new block planted in year six was growth "
			"and is now recorded as maintenance, which understates Sustainable CF/Acre until "
			"somebody fixes it on the Asset in the Desk. That is a better place to start from "
			"than a register of nulls — where every purchase is excluded from the KPI and the "
			"figure is overstated instead — and it is worse than real classifications. Both are "
			"true; the first is why this tool exists and the second is why it says so."
		),
	}
	if dry_run:
		data["dry_run_note"] = (
			f"NOTHING WAS WRITTEN. {len(classified)} asset(s) would be classified as "
			f"{capex_type}. Re-run with dry_run=false to apply."
		)
	if len(rows or []) >= BACKFILL_CAP:
		data["cap_note"] = (
			f"{BACKFILL_CAP} is the most this call classifies and that many matched, so there "
			"are probably more. Run again — it is idempotent — or narrow with company and "
			"cutoff_purchase_date."
		)
	if not rows:
		data["empty_note"] = (
			"No unclassified assets matched. Either everything is already classified — which is "
			"the goal — or the filters are narrower than the register."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{'would classify' if dry_run else 'classified'} {len(classified)} asset(s) as "
			f"{capex_type}"
			+ (f" purchased before {cutoff}" if cutoff else "")
			+ (f"; {len(failed)} failed" if failed else "")
		),
		docstatus_delta="none" if dry_run else f"{len(classified)} Asset row(s) classified",
	)


def _write_capex(asset: str, capex_type: str, split: dict) -> None:
	"""Set the four columns on one Asset, without running ERPNext's own validation.

	`db_set` rather than `save`, and the reason is the same one `create_asset`
	makes about `reqd`: a submitted Asset cannot be re-saved through the ordinary
	path, and most of the register this tool exists to classify is submitted.
	Classifying a purchase is metadata about a decision already made — it changes
	no amount, no account, no schedule and no docstatus — so writing the columns
	directly is exactly proportionate, and going through `save` would refuse the
	rows that most need it.
	"""
	values = {
		kpi_module.CAPEX_TYPE_FIELD: capex_type,
		kpi_module.MAINTENANCE_PORTION_FIELD: split["maintenance"],
		kpi_module.GROWTH_PORTION_FIELD: split["growth"],
	}
	for fieldname, value in values.items():
		if compat.has_field(kpi_module.ASSET_DOCTYPE, fieldname):
			frappe.db.set_value(kpi_module.ASSET_DOCTYPE, asset, fieldname, value)


# ── 6. get_sustainable_cf_per_acre ──────────────────────────────────────────
def get_sustainable_cf_per_acre(args: dict) -> ToolResult:
	"""The KPI, TTM by default — or over a typed period, for a v0.19.5 caller.

	TWO SHAPES OUT OF ONE TOOL, AND THE OLD ONE IS REACHED ONLY BY ASKING FOR IT.
	A caller that passes `period_start` and `period_end` — the v0.19.5 signature —
	gets v0.19.5's point-in-time payload back, with a deprecation sentence in
	`computation_warnings`. Everybody else gets the windowed shape with a trailing
	twelve months as the default.

	The compatibility path is not politeness. v0.19.5's payload is quoted in
	lender packs, saved in scripts and pasted into spreadsheets, and a release
	that changed what those callers got back without their asking would silently
	alter a figure somebody had already sent to a bank. So the old shape is exact
	rather than approximated, and the warning is the only thing added to it.
	"""
	actor = require_kpi_role()
	company = resolve_company(as_str(args, "company"), required=True)
	employee_tool.require_company_scope(actor, company)

	legacy_start = as_date(args, "period_start")
	legacy_end = as_date(args, "period_end")
	if legacy_start and legacy_end:
		return _legacy_point_in_time(actor, company, legacy_start, legacy_end)
	if legacy_start or legacy_end:
		raise ToolError(
			"period_start and period_end are the v0.19.5 signature and go TOGETHER — one without "
			"the other is neither a period nor a window. Pass both to get the old point-in-time "
			"payload for exactly that period, or pass neither and get the TTM window ending at "
			"the last completed month, which is what this tool now returns by default."
		)

	report = _run_windowed("sustainable_cf_per_acre", actor, company, args)
	return ToolResult(data=report["data"], summary=report["summary"])


def _legacy_point_in_time(actor: str, company: str, period_start: str, period_end: str) -> ToolResult:
	"""v0.19.5's payload, unchanged except for the sentence saying it is v0.19.5's."""
	if period_end < period_start:
		raise ToolError(f"period_end ({period_end}) is before period_start ({period_start}).")

	report = service.compute(company, period_start, period_end)
	per_acre = report["sustainable_cf_per_acre"]

	warnings = list(report["computation_warnings"])
	warnings.insert(
		0,
		"DEPRECATED CALL SHAPE. period_start and period_end are the v0.19.5 signature and this is "
		"v0.19.5's point-in-time payload: one period, no trailing twelve months, no historical "
		"averages. It still works and still returns exactly what it always did — that is "
		"deliberate, because this figure is quoted in packs that were sent before v0.19.6 existed. "
		"But a single agricultural period is the comparison the window standard exists to stop "
		"anybody making: Q3 is harvest and Q1 is pruning, and setting them beside each other says "
		"the farm collapsed in January and recovered in September, every year, on every farm. Drop "
		"both arguments to get the TTM window plus five years of history, or pass "
		"as_of=<date> to place the window at a moment other than today.",
	)

	data = {
		**report,
		"computation_warnings": warnings,
		"actor": actor,
		"call_shape": "v0.19.5 point-in-time (deprecated)",
		"reading_it": (
			"Read the components before the figure. Sustainable CF/Acre is a NORMALIZED number, "
			"which means somebody has decided that money which really did move should not be "
			"read as recurring — and a normalized number nobody can inspect is indistinguishable "
			"from an arranged one. `normalization_adjustments` is every one of those decisions "
			"with its justification and the name behind it; `maintenance_capex.itemized` is what "
			"was actually spent replacing what wore out, never a percentage of revenue; "
			"`productive_acres.itemized` is which blocks were in the denominator and for how "
			"many days of the period."
		),
	}
	if per_acre is None:
		summary = (
			f"{company} {period_start}-{period_end}: no productive acres, so no per-acre figure "
			f"(normalized OCF {report['normalized_ocf']}, maintenance capex "
			f"{report['maintenance_capex']['total']})"
		)
	else:
		summary = (
			f"{company} {period_start}-{period_end}: {per_acre} per acre "
			f"(({report['normalized_ocf']} normalized OCF - "
			f"{report['maintenance_capex']['total']} maintenance capex) ÷ "
			f"{report['productive_acres']['time_weighted']} time-weighted acres), "
			f"{len(report['normalization_adjustments'])} adjustment(s), "
			f"{len(warnings)} warning(s)"
		)
	return ToolResult(data=data, summary=summary)


# ── 7. get_windowed_report ──────────────────────────────────────────────────
#
# v0.19.6. The generic entry point, and the reason the standard is a standard: a
# report is registered once in `services/financial_reports.py` and is reachable
# here without another tool, another switch and another catalogue entry. A
# framework whose every KPI costs a tool is a framework with six KPIs in it.
def _window_args(args: dict) -> dict:
	"""The five window arguments, validated once for every tool that takes them."""
	window_type = as_str(args, "window_type") or windows.WINDOW_TTM
	matched = next((option for option in windows.WINDOW_TYPES if option.lower() == window_type.lower()), "")
	if not matched:
		raise ToolError(
			f"window_type must be one of {', '.join(windows.WINDOW_TYPES)}; got {window_type!r}. "
			"TTM is the default and the standard: twelve rolling months, so pruning, thinning, "
			"harvest and the winter are each in the figure exactly once no matter when it is read."
		)

	step = as_str(args, "computation_step") or windows.STEP_MONTHLY
	matched_step = next((option for option in windows.STEPS if option.lower() == step.lower()), "")
	if not matched_step:
		raise ToolError(
			f"computation_step must be one of {', '.join(windows.STEPS)}; got {step!r}. Monthly is "
			"the default: Daily is cheap for one report and ruinous across a framework, so it is "
			"opted into per KPI rather than reached by accident."
		)

	# No `or DEFAULT_WINDOW_MONTHS`: it reinstated the default above the `< 1` refusal
	# below, so `window_months: 0` reported a TTM window instead of being refused.
	months = as_int(args, "window_months", windows.DEFAULT_WINDOW_MONTHS)
	if months < 1 or months > 120:
		raise ToolError(
			f"window_months must be between 1 and 120; got {months}. TTM is 12, which is what the "
			"T and the M mean."
		)

	lookback = as_int(args, "historical_lookback_years", windows.DEFAULT_LOOKBACK_YEARS)
	if lookback is None:
		lookback = windows.DEFAULT_LOOKBACK_YEARS
	if lookback < 0 or lookback > windows.MAX_LOOKBACK_YEARS:
		raise ToolError(
			f"historical_lookback_years must be between 0 and {windows.MAX_LOOKBACK_YEARS}; got "
			f"{lookback}. Past that, every extra entry is a row saying 'no ledger', which is a "
			"slower way of saying nothing."
		)

	return {
		"as_of": as_date(args, "as_of") or frappe.utils.today(),
		"window_type": matched,
		"window_months": months,
		"computation_step": matched_step,
		"historical_lookback_years": lookback,
		"historical_averaging_enabled": as_bool(args, "include_historical_averages", True),
	}


def _run_windowed(report_name: str, actor: str, company: str, args: dict) -> dict:
	"""One registered report, windowed, with the sentences a reader needs around it."""
	try:
		entry = windows.registered(report_name)
	except ValueError as exc:
		raise ToolError(f"{exc} Nothing was computed.") from None
	if entry.get("available") and not entry["available"]():
		raise ToolError(
			f"{report_name!r} cannot be computed on this site: it needs a doctype that is not "
			"installed. For sustainable_cf_per_acre that is Normalization Adjustment, which ships "
			"with erpnext_mcp — run `bench --site <site> migrate`. Nothing was computed."
		)

	options = _window_args(args)
	report = windows.compute_windowed(entry["computer"], company, entry=entry, **options)

	window = report.get("window") or {}
	value = window.get("value")
	averages = report.get("historical_averages") or {}
	data = {
		**report,
		"actor": actor,
		"reading_it": (
			"THREE BLOCKS, AND EACH IS THE CORRECTION FOR THE OTHER TWO. `point_in_time` is the "
			"period just finished, which on a farm flatters harvest and demonizes pruning. "
			"`window` (`ttm` by default) is the same figure over twelve rolling months, so the "
			"whole annual cycle is in it exactly once however it is read. `historical_averages` is "
			"what that window has been worth for this operation before, which is the only thing "
			"that says whether the current number is good — a TTM figure means one thing against a "
			"five-year mean below it and the opposite against one above. Read "
			"`computation_warnings` before quoting any of them: a partial window and a full one "
			"look identical in the value and are not the same claim."
		),
	}
	label = entry["label"]
	if value is None:
		summary = (
			f"{company} {label} {options['window_type']} to {window.get('period_end')}: no "
			f"defensible value ({len(report.get('computation_warnings') or [])} warning(s)) — read "
			"computation_warnings"
		)
	else:
		mean = averages.get("prior_ttm_mean")
		delta = averages.get("current_vs_mean_pct_delta")
		against = (
			f", {delta:+.1f}% vs a {averages.get('prior_ttm_count')}-entry mean of {mean}"
			if mean is not None and delta is not None
			else ""
		)
		summary = (
			f"{company} {label} {options['window_type']} {window.get('period_start')} to "
			f"{window.get('period_end')}: {value}{against}; "
			f"{len(report.get('computation_warnings') or [])} warning(s)"
		)
	return {"data": data, "summary": summary}


def get_windowed_report(args: dict) -> ToolResult:
	"""Any registered financial report, over a window, with its own history beside it."""
	actor = require_kpi_role()
	company = resolve_company(as_str(args, "company"), required=True)
	employee_tool.require_company_scope(actor, company)

	report_name = as_str(args, "report_name") or as_str(args, "report", required=True)
	report = _run_windowed(report_name, actor, company, args)
	report["data"]["available_reports"] = sorted(windows.COMPUTERS)
	return ToolResult(data=report["data"], summary=report["summary"])


# ── 8. list_financial_kpi_history ───────────────────────────────────────────
def list_financial_kpi_history(args: dict) -> ToolResult:
	"""The cache, read directly — the time series without the apparatus around it.

	SEPARATE FROM `get_windowed_report` BECAUSE THE QUESTIONS ARE DIFFERENT. That
	tool answers "what is this worth now, and is that good"; this one answers
	"show me the line", and a caller drawing a chart or exporting a series does
	not want sixty copies of the components dict to get sixty numbers.

	It reports what is NOT there as well as what is. A gap in a cached series is
	not a gap in the business — it is a window nobody has computed yet, or one
	that was invalidated by a retroactively approved adjustment and not yet
	rebuilt — and a series with a hole in it plotted as a continuous line is a
	trend that did not happen.
	"""
	actor = require_kpi_role()
	limit = min(as_limit(args), RECORD_CAP)

	if not compat.doctype_exists(windows.DOCTYPE):
		raise ToolError(
			f"this site has no {windows.DOCTYPE} doctype yet — it ships with erpnext_mcp and "
			"arrives with `bench --site <site> migrate`. Until it does, every windowed report "
			"still answers; it just recomputes its history on every call and says so."
		)

	scope = _scoped_companies(actor, args)
	filters = dict(scope["filters"])

	kpi_key = as_str(args, "kpi_key")
	if kpi_key:
		filters["kpi_key"] = kpi_key
	step = as_str(args, "computation_step")
	if step:
		matched = next((option for option in windows.STEPS if option.lower() == step.lower()), "")
		if not matched:
			raise ToolError(f"computation_step must be one of {', '.join(windows.STEPS)}; got {step!r}.")
		filters["computation_step"] = matched
	window_type = as_str(args, "window_type")
	if window_type:
		matched = next(
			(option for option in windows.WINDOW_TYPES if option.lower() == window_type.lower()), ""
		)
		if not matched:
			raise ToolError(
				f"window_type must be one of {', '.join(windows.WINDOW_TYPES)}; got {window_type!r}."
			)
		filters["window_type"] = matched

	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters["as_of"] = ("between", [from_date, to_date])
	elif from_date:
		filters["as_of"] = (">=", from_date)
	elif to_date:
		filters["as_of"] = ("<=", to_date)

	rows = frappe.db.get_all(
		windows.DOCTYPE,
		filters=filters,
		fields=compat.existing_fields(
			windows.DOCTYPE,
			(
				"name",
				"kpi_key",
				"company",
				"computation_step",
				"window_type",
				"window_months",
				"as_of",
				"period_start",
				"period_end",
				"value",
				"computation_warnings_json",
				"computed_at",
				"source_version",
			),
		),
		order_by="as_of desc, name desc",
		limit=limit + 1,
	)
	truncated = len(rows or []) > limit
	rows = (rows or [])[:limit]

	records = []
	versions: set = set()
	unresolved = 0
	for row in rows:
		warnings = []
		try:
			warnings = json.loads(row.get("computation_warnings_json") or "[]")
		except Exception:  # pragma: no cover - a row somebody edited
			warnings = []
		if row.get("value") is None:
			unresolved += 1
		if row.get("source_version"):
			versions.add(str(row["source_version"]))
		records.append(
			{
				"name": row.get("name"),
				"kpi_key": row.get("kpi_key"),
				"company": row.get("company"),
				"computation_step": row.get("computation_step"),
				"window_type": row.get("window_type"),
				"window_months": row.get("window_months"),
				"as_of": str(row.get("as_of") or "") or None,
				"period_start": str(row.get("period_start") or "") or None,
				"period_end": str(row.get("period_end") or "") or None,
				"value": row.get("value"),
				"warning_count": len(warnings),
				"computation_warnings": warnings,
				"computed_at": str(row.get("computed_at") or "") or None,
				"source_version": row.get("source_version"),
			}
		)

	# v0.39.0. Every kpi_key in the result joined to the DEFINITION that owns it,
	# where there is one. A cached series is a column of numbers and a date, and
	# the thing a reader needs beside it is what the number IS: 0.42 is a
	# catastrophe as a current ratio, a fine margin as a percentage and a rounding
	# error as dollars, and before the framework the only place the unit lived was
	# a Python constant on a registered computer. Sites that have not migrated the
	# definition doctype get exactly what they got in v0.38.0.
	definitions = _definitions_for({entry["kpi_key"] for entry in records if entry.get("kpi_key")})

	values = [entry["value"] for entry in records if entry["value"] is not None]
	data = {
		"actor": actor,
		"company": scope["company"],
		"kpi_key": kpi_key or None,
		"definitions": definitions,
		"count": len(records),
		"limit": limit,
		"truncated": truncated,
		"records": records,
		"series": [
			{"as_of": entry["as_of"], "value": entry["value"], "kpi_key": entry["kpi_key"]}
			for entry in records
		],
		"value_count": len(values),
		"min": round(min(values), 4) if values else None,
		"max": round(max(values), 4) if values else None,
		"source_versions": sorted(versions),
		"note": (
			"THIS IS A CACHE AND NOT A LEDGER. Every row here is derivable by rerunning the "
			"computer that wrote it, which is what makes it safe for an approved normalization "
			"adjustment to delete the snapshots whose window it changed. A gap in this series is a "
			"window nobody has computed yet or one that was invalidated and not yet rebuilt — it "
			"is NOT a period in which the business earned nothing, and plotting it as a continuous "
			"line draws a trend that did not happen. recompute_kpi_history fills a gap; the "
			"overnight sweep fills it by itself."
		),
	}
	if unresolved:
		data["null_value_note"] = (
			f"{unresolved} row(s) have no value. Null is an answer here rather than a failure — a "
			"per-acre figure with no productive acres in the denominator is a division nobody "
			"performed — and each row's computation_warnings says which reason applies."
		)
	if len(versions) > 1:
		data["version_note"] = (
			f"these rows were computed by {len(versions)} different versions of erpnext_mcp: "
			+ ", ".join(sorted(versions))
			+ ". Where a release changed how a figure is computed, a series spanning the change is "
			"two definitions of one KPI plotted on one line with nothing marking the join. "
			"recompute_kpi_history(force=true) rebuilds the whole series under the current one."
		)
	if truncated:
		data["truncation_note"] = (
			f"More than {limit} row(s) matched and this is the newest {limit}. Narrow with "
			"kpi_key, company, computation_step or a from_date/to_date range before treating the "
			"series above as complete."
		)
	orphaned = sorted({entry["kpi_key"] for entry in records if entry.get("kpi_key")} - set(definitions))
	if orphaned and definitions:
		data["orphan_note"] = (
			f"{len(orphaned)} kpi_key(s) in this series have no Financial KPI Definition on this "
			f"site: {', '.join(orphaned)}. That is not a broken series — the three reports this "
			"app ships have always cached under their own keys and still do — but where the key "
			"was a definition that has since been renamed, these rows are the orphaned half and "
			"nothing will ever extend them. list_financial_kpi_definitions has the register."
		)
	if not records:
		data["empty_note"] = (
			"Nothing is cached for these filters. That is the ordinary state of a site that has "
			"not run the overnight sweep yet, and it costs nothing but speed: every windowed "
			"report still answers, computing what it needs live and saying in its warnings how "
			"much history it had to leave out."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{len(records)} cached KPI snapshot(s)"
			+ (f" for {kpi_key}" if kpi_key else "")
			+ (f" on {scope['company']}" if scope["company"] else "")
			+ (f"; {unresolved} with no value" if unresolved else "")
		),
	)


# ── 9. recompute_kpi_history ────────────────────────────────────────────────
def recompute_kpi_history(args: dict) -> ToolResult:
	"""Rebuild the cache for one KPI. Idempotent unless `force`, which clears first.

	MUTATING, AND THE ONLY THING IT CAN MUTATE IS A CACHE. Every row it writes is
	derivable from the ledger and every row it deletes comes back — so the worst
	outcome of running it at the wrong moment is time spent, which is why it is
	one of the few mutating tools in this app whose failure mode is boredom.

	IT IS THE ANSWER TO A RETROACTIVE APPROVAL. Approving a normalization
	adjustment for a period history already covers deletes the snapshots whose
	window overlapped it, and the next read or the next sweep rebuilds them; this
	is how somebody rebuilds them NOW, with the result in front of them, before
	sending the pack. The other case is a Field productive-date backfill, which
	changes the denominator of every window containing the corrected block.

	`force` CLEARS AND REBUILDS rather than filling gaps. That is the heavier
	operation and it exists for the case the incremental path cannot reach: a
	release that changed how a figure is computed leaves a series holding two
	definitions of one KPI, plotted on one line, with nothing marking the join.
	"""
	actor = require_kpi_role()

	if not compat.doctype_exists(windows.DOCTYPE):
		raise ToolError(
			f"this site has no {windows.DOCTYPE} doctype yet — it ships with erpnext_mcp and "
			"arrives with `bench --site <site> migrate`. Nothing was changed."
		)

	kpi_key = as_str(args, "kpi_key", required=True)
	entry = next(
		(item for item in windows.COMPUTERS.values() if item["kpi_key"] == kpi_key),
		None,
	)
	if not entry:
		# v0.39.0. A kpi_key that is not a SHIPPED computer may still be a KPI
		# DEFINITION, and this tool is the one somebody already knows the name of.
		# Delegating rather than refusing is what makes the framework reachable
		# from the tool that was here first — the alternative is a caller told
		# "no registered report computes this" about a KPI that is on their own
		# dashboard, which is true and useless.
		from ..services import kpi_engine

		if kpi_engine.definition_row(kpi_key):
			return _refresh_defined_kpi(actor, kpi_key, args)
		raise ToolError(
			f"no registered report and no KPI definition computes {kpi_key!r}. The shipped "
			f"reports are: "
			f"{', '.join(sorted(item['kpi_key'] for item in windows.COMPUTERS.values())) or '<none>'}"
			+ (
				f"; the defined KPIs are: {', '.join(sorted(str(row.get('kpi_id')) for row in kpi_engine.rows())) or '<none>'}"
				if kpi_engine.available()
				else ""
			)
			+ ". Nothing was changed."
		)

	back_years = as_int(args, "back_years", windows.DEFAULT_LOOKBACK_YEARS)
	if back_years is None:
		back_years = windows.DEFAULT_LOOKBACK_YEARS
	if back_years < 1 or back_years > windows.MAX_LOOKBACK_YEARS:
		raise ToolError(
			f"back_years must be between 1 and {windows.MAX_LOOKBACK_YEARS}; got {back_years}. "
			"Nothing was changed."
		)
	force = as_bool(args, "force", False)

	company = resolve_company(as_str(args, "company"), required=False)
	if company:
		employee_tool.require_company_scope(actor, company)
		companies = [company]
	else:
		allowed = shift_tools._readable_companies(actor)
		companies = (
			list(allowed) if allowed else (frappe.db.get_all("Company", pluck="name", limit=200) or [])
		)

	cleared = 0
	written = 0
	per_company = []
	for name in companies:
		before = windows.clear(kpi_key, str(name)) if force else 0
		cleared += before
		count = windows._sweep_one(entry, str(name), lookback_years=back_years)
		written += count
		per_company.append({"company": str(name), "cleared": before, "written": count})

	data = {
		"actor": actor,
		"kpi_key": kpi_key,
		"report_name": entry["report_name"],
		"companies": [str(name) for name in companies],
		"back_years": back_years,
		"force": bool(force),
		"snapshots_cleared": cleared,
		"snapshots_written": written,
		"per_company": per_company,
		"computation_step": entry["default_step"],
		"note": (
			"THE CACHE IS DERIVABLE AND THAT IS WHY THIS IS SAFE. Nothing here is the only copy of "
			"anything: every snapshot written is what the live computation would have produced for "
			"that window, and every snapshot cleared comes back on the next read or the next "
			"overnight sweep. The cost of running this at the wrong moment is time."
		),
	}
	if force:
		data["force_note"] = (
			f"force=true, so {cleared} existing snapshot(s) were DELETED and rebuilt rather than "
			"filled around. Use it after a release changes how a figure is computed — an "
			"incremental fill leaves the old rows in place, and a series holding two definitions "
			"of one KPI is a line with an unmarked join in it."
		)
	else:
		data["idempotent_note"] = (
			"force=false, so only missing snapshots were computed and a second run finds nothing "
			"to do. That is the ordinary use: filling the gap a retroactively approved adjustment "
			"left, without touching rows that are still correct."
		)
	if not written and not cleared:
		data["empty_note"] = (
			"Nothing was written. Either the cache is already complete for this KPI and window — "
			"which is what a second run looks like — or these companies have no submitted GL "
			"postings at all, in which case there is no ledger to compute a history from."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{'rebuilt' if force else 'filled'} {written} cached snapshot(s) of {kpi_key} across "
			f"{len(companies)} company(ies), {back_years} year(s) back"
			+ (f"; {cleared} cleared first" if cleared else "")
		),
		docstatus_delta=(
			f"{cleared} cached snapshot(s) deleted, {written} written"
			if cleared
			else f"{written} cached snapshot(s) written"
		),
	)


def _refresh_defined_kpi(actor: str, kpi_id: str, args: dict) -> ToolResult:
	"""`recompute_kpi_history` for a kpi_key that names a Financial KPI Definition.

	v0.39.0, and it is a delegation rather than a second implementation. The
	framework's own `refresh_kpi_cache` does the work — it reads the definition's
	own window type, window length and step, which a shipped report does not
	have and which `windows._sweep_one` therefore hardcodes. Reimplementing that
	here would be two functions that fill one cache and could disagree about
	which windows belong in it.
	"""
	from ..services import kpi_engine

	back_years = as_int(args, "back_years", windows.DEFAULT_LOOKBACK_YEARS)
	if back_years is None:
		back_years = windows.DEFAULT_LOOKBACK_YEARS
	if back_years < 1 or back_years > windows.MAX_LOOKBACK_YEARS:
		raise ToolError(
			f"back_years must be between 1 and {windows.MAX_LOOKBACK_YEARS}; got {back_years}. "
			"Nothing was changed."
		)
	force = as_bool(args, "force", False)
	company = resolve_company(as_str(args, "company"), required=False)
	if company:
		employee_tool.require_company_scope(actor, company)

	report = kpi_engine.refresh_kpi_cache(
		kpi_id=kpi_id, company=company or "", back_years=back_years, force=bool(force)
	)
	return ToolResult(
		data={
			"actor": actor,
			"kpi_key": kpi_id,
			"defined_kpi": True,
			**report,
			"note": (
				f"{kpi_id} is a Financial KPI Definition rather than one of the three reports this "
				"app ships, so this call was handled by the framework — which reads the "
				"definition's own window type, window length and step rather than assuming a "
				"monthly TTM. refresh_kpi_cache is the same operation named for what it does."
			),
		},
		summary=(
			f"{'rebuilt' if force else 'filled'} {report['written']} cached snapshot(s) of the "
			f"defined KPI {kpi_id}, {back_years} year(s) back"
			+ (f"; {report['cleared']} cleared first" if report["cleared"] else "")
		),
		docstatus_delta=(
			f"{report['cleared']} cached snapshot(s) deleted, {report['written']} written"
			if report["cleared"]
			else f"{report['written']} cached snapshot(s) written"
		),
	)


def _definitions_for(kpi_keys) -> dict:
	"""`{kpi_id: headline}` for the keys in a cached series that have a definition.

	v0.39.0. What a reader needs beside a column of numbers is what the number
	IS — its title, its unit, its category and whether anything is watching it —
	and until the framework the only place the unit lived was a Python constant
	on a registered computer.

	NEVER RAISES AND RETURNS `{}` ON A SITE WITHOUT THE DOCTYPE, so a bench
	between the app landing and `bench migrate` finishing gets exactly the payload
	v0.38.0 gave rather than an error about a doctype it has not got yet.
	"""
	try:
		from ..services import kpi_engine

		if not kpi_engine.available():
			return {}
		out = {}
		for key in sorted(kpi_keys or ()):
			row = kpi_engine.definition_row(str(key))
			if not row:
				continue
			described = kpi_engine.describe(row)
			out[str(key)] = {
				"name": described["name"],
				"title": described["title"],
				"unit": described["unit"],
				"category": described["category"],
				"enabled": described["enabled"],
				"formula_type": described["formula_type"],
				"thresholds": described["thresholds"],
			}
		return out
	except Exception:  # pragma: no cover - an annotation is never worth failing a read over
		return {}
