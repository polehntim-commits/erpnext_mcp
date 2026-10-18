# SPDX-License-Identifier: MIT
"""Who owns this, what happened to their interest, and which paper says so.

Three doctypes and ten tools, and one idea holding them together: **the ledger
stays anonymous and the register carries the names**.

WHY THAT SPLIT. A chart of accounts and a cost center tree are read by everyone
who touches the books — a bookkeeper, a lender, an auditor, a model summarising
the year. Family names sitting in either one leak into every report that is
exported, and cannot be taken out again once a statement has been sent. So a
posting is tagged with an anonymous Member accounting dimension value
(`Member-01`), and exactly one place on the site — the Cap Table Entry — maps
that value to a legal entity, an admission date and a percentage. Anyone who
needs the mapping can be given read access to one doctype; nobody needs it to
read the ledger.

WHY THE COST CENTER IS OPTIONAL AND THE DIMENSION IS NOT. Members are a
*dimension*, not a segment of the business: cost centers answer "which part of
the operation did this belong to", and a member is not a part of the operation.
The Cap Table Entry keeps a `member_cost_center` field anyway, because a site
whose convention already gives each member a cost center should not have to
abandon it — but every tool here files by the dimension, and the cost center is
carried along rather than relied upon.

WHY EVERY EVENT HAS A NARRATIVE, AND IT IS MANDATORY. A Journal Entry survives
on its own. The reason for it does not: "why did Member-02 take 40,000 in March
2031" is the question that is asked once the people who knew have gone. So
`record_member_event` refuses an event without one, in the same way
`cancel_journal_entry` refuses a cancellation without a reason.

WHAT THESE TOOLS DO NOT DO. They do not submit anything. A member event that
needs a Journal Entry gets a DRAFT one, exactly as `create_journal_entry` does,
and posting it is `submit_member_event` — which checks the *submit_journal_entry*
switch as well as its own, because posting to the general ledger is a permission
an operator grants once, in one place, whatever route reaches it.
"""

import hashlib

import frappe

from .. import compat, convert, roles, security, settings
from ..args import (
	as_bool,
	as_choice,
	as_date,
	as_float,
	as_limit,
	as_str,
	resolve_account,
	resolve_company,
	resolve_cost_center,
	select_options,
)
from ..errors import ToolError
from ..result import ToolResult
from . import expenses, files, mutate

CAP_TABLE = "Cap Table Entry"
MEMBER_EVENT = "Member Event"
GOVERNANCE_DOCUMENT = "Governance Document"

#: Fields read off a Cap Table Entry. Filtered through `compat` before any
#: query, like every other field list in this app, so a site mid-migration gets a
#: degraded answer rather than a SQL error.
_CAP_TABLE_FIELDS = (
	"name",
	"member_id",
	"company",
	"legal_entity_name",
	"entity_type",
	"admission_date",
	"withdrawal_date",
	"ownership_percentage",
	"retired",
	"member_cost_center",
	"member_dimension_doctype",
	# v0.332.0. Who this member captures receipts as, and the draw account a
	# purchase they cover personally is booked to.
	"employee",
	"draw_account",
	"notes",
)

_MEMBER_EVENT_FIELDS = (
	"name",
	"event_type",
	"effective_date",
	"amount",
	"member",
	"counterparty_member",
	"company",
	"offset_je",
	"superseded_by",
	"narrative",
	"creation",
	"owner",
)

_GOVERNANCE_FIELDS = (
	"name",
	"title",
	"category",
	"company",
	"effective_date",
	"execution_date",
	"supersedes",
	"superseded_by",
	"attached_file",
	"parties",
	"notes",
	"creation",
	"owner",
)

#: Event types that book money and therefore get a Journal Entry when the caller
#: does not supply one. `Admission` is the odd one out on purpose: admitting a
#: member with no capital movement is a real event that belongs in the trail and
#: has nothing to post.
POSTING_EVENTS = ("Contribution", "Distribution", "Withdrawal", "Transfer", "Reallocation")

#: Event types that move an interest between two members.
PAIRED_EVENTS = ("Transfer", "Reallocation")

#: The dimension label this app looks for when nothing is said. Overridable per
#: call, because a site may have named it "Owner" or "Partner".
DEFAULT_MEMBER_DIMENSION = "Member"

#: How an equity account is recognised when the caller does not name one. Matched
#: against the account NAME, case-insensitively, over the company's leaf Equity
#: accounts only. Anything other than exactly one match is refused with the
#: candidates listed — this is a shortlist, never a guess.
_CAPITAL_KEYWORDS = (
	"member capital",
	"members capital",
	"members' capital",
	"partner capital",
	"partners capital",
	"owner capital",
	"capital contribution",
	"capital account",
)
_DISTRIBUTION_KEYWORDS = (
	"member distribution",
	"members distribution",
	"partner distribution",
	"distributions",
	"distribution",
	"member draw",
	"owner draw",
	"drawings",
)


# ── shared lookups ──────────────────────────────────────────────────────────
def _require_cap_table() -> None:
	compat.require_doctype(
		CAP_TABLE,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _company_abbr(company: str) -> str:
	return frappe.db.get_value("Company", company, "abbr") or ""


#: The entity types and event types are declared in the shipped DocType JSON and
#: read off the site at call time. Both helpers moved to `args` in v0.11.0 when
#: the real-estate and related-party tools needed the same rule; these aliases
#: stay so the reading of this module does not change.
_select_options = select_options
_validated_choice = as_choice


def cap_table_entry(member: str, company: str = "") -> dict:
	"""One Cap Table Entry as a dict, from its docname or its member id.

	Two ways because a caller who created the entry knows it by the member id it
	asked for (`Member-01`), while everything that links to it holds the docname
	(`Member-01 - OML`). Resolving both is what stops a model having to remember
	which of the two a given tool wanted.
	"""
	member = (member or "").strip()
	if not member:
		raise ToolError("member is required (a Cap Table Entry docname, or a member_id such as 'Member-01')")
	fields = compat.existing_fields(CAP_TABLE, _CAP_TABLE_FIELDS)

	if frappe.db.exists(CAP_TABLE, member):
		row = frappe.db.get_value(CAP_TABLE, member, fields, as_dict=True)
		if company and row and row.get("company") != company:
			raise ToolError(
				f"Cap Table Entry {member!r} belongs to company {row.get('company')!r}, not {company!r}"
			)
		return dict(row)

	filters = {"member_id": member}
	if company:
		filters["company"] = company
	matches = frappe.db.get_all(CAP_TABLE, filters=filters, fields=fields, limit=25)
	if len(matches) == 1:
		return dict(matches[0])
	if len(matches) > 1:
		raise ToolError(
			f"member_id {member!r} exists in {len(matches)} companies "
			f"({', '.join(sorted(str(row['company']) for row in matches))}). Pass company to narrow it."
		)
	scope = f" in {company}" if company else ""
	known = frappe.db.get_all(
		CAP_TABLE, filters={"company": company} if company else None, pluck="member_id", limit=25
	)
	raise ToolError(
		f"no Cap Table Entry for {member!r}{scope}. "
		f"Registered members: {', '.join(sorted(str(name) for name in known)) or '<none>'}. "
		"Create one with create_cap_table_entry."
	)


def _describe_entry(row: dict) -> dict:
	"""The cap table shape every tool in this module returns."""
	return {
		"name": row.get("name"),
		"member_id": row.get("member_id"),
		"company": row.get("company"),
		"legal_entity_name": row.get("legal_entity_name"),
		"entity_type": row.get("entity_type"),
		"admission_date": str(row.get("admission_date") or "") or None,
		"withdrawal_date": str(row.get("withdrawal_date") or "") or None,
		"ownership_percentage": float(row.get("ownership_percentage") or 0),
		"retired": bool(int(row.get("retired") or 0)),
		"member_cost_center": row.get("member_cost_center") or None,
		"employee": row.get("employee") or None,
		"draw_account": row.get("draw_account") or None,
	}


def _member_dimension(dimension_name: str = "") -> dict:
	"""The Member accounting dimension: its fieldname and the DocType holding values.

	Returns `{}` when the site has no such dimension, rather than raising — three
	of the tools here work perfectly well without one, and only the ones that
	write a Journal Entry line need it. Those refuse for themselves, with the
	remedy named.
	"""
	label = dimension_name or DEFAULT_MEMBER_DIMENSION
	if not compat.doctype_exists("Accounting Dimension"):
		return {}
	fields = compat.existing_fields(
		"Accounting Dimension", ("name", "label", "fieldname", "document_type", "disabled")
	)
	rows = [dict(row) for row in frappe.db.get_all("Accounting Dimension", fields=fields, limit=500)]
	for key in ("label", "document_type", "name"):
		for row in rows:
			if str(row.get(key) or "").lower() == label.lower():
				return {
					"name": row.get("name"),
					"label": row.get("label") or row.get("name"),
					"fieldname": row.get("fieldname") or frappe.scrub(str(row.get("label") or label)),
					"master": row.get("document_type"),
					"disabled": bool(int(row.get("disabled") or 0)),
				}
	return {}


def _require_member_dimension(dimension_name: str = "") -> dict:
	dimension = _member_dimension(dimension_name)
	label = dimension_name or DEFAULT_MEMBER_DIMENSION
	if not dimension:
		raise ToolError(
			f"this site has no {label!r} accounting dimension, so a member posting cannot be "
			"tagged with the member it belongs to — and an untagged equity entry is one "
			"nobody can attribute later. Create it with "
			f"create_accounting_dimension(dimension_name={label!r}, create_master_if_missing=true), "
			"then add each member with create_dimension_value. Nothing was created."
		)
	if dimension.get("disabled"):
		raise ToolError(
			f"the {dimension['label']!r} accounting dimension is disabled on this site, so "
			"ERPNext ignores it and the tag would not be read by any report. Re-enable it "
			"in the Desk. Nothing was created."
		)
	if not compat.has_field("Journal Entry Account", dimension["fieldname"]):
		raise ToolError(
			f"the {dimension['label']!r} dimension exists but Journal Entry Account has no "
			f"{dimension['fieldname']!r} field on this site, so the tag would be written to "
			"nothing. ERPNext carries dimensions on the journal entry LINE; re-run "
			"create_accounting_dimension including 'Journal Entry' in document_types. "
			"Nothing was created."
		)
	return dimension


def _dimension_value_for(entry: dict, dimension: dict) -> str:
	"""The dimension record that stands for this member, checked before it is used.

	The member id IS the dimension value — that is the whole convention, and it is
	why `create_accounting_dimension` names its generated masters after their own
	value field. Checking it here rather than letting ERPNext's link validation
	catch it on submit matters: a draft with a bad dimension value is a draft that
	cannot be posted, discovered by whoever tries to post it.
	"""
	master = dimension.get("master")
	member_id = str(entry.get("member_id") or "")
	if master and compat.doctype_exists(master) and not frappe.db.exists(master, member_id):
		raise ToolError(
			f"{member_id!r} is not a {master} on this site, so it is not yet a value of the "
			f"{dimension['label']} dimension and a journal entry line tagged with it could "
			f"not be posted. Add it with create_dimension_value(dimension_name="
			f"{dimension['label']!r}, value_name={member_id!r}). Nothing was created."
		)
	return member_id


def _leaf_equity_accounts(company: str) -> list[dict]:
	fields = compat.existing_fields(
		"Account", ("name", "account_name", "account_number", "is_group", "disabled", "root_type")
	)
	rows = frappe.db.get_all(
		"Account",
		filters={"company": company, "root_type": "Equity"},
		fields=fields,
		order_by="name asc",
		limit=500,
	)
	return [
		dict(row) for row in rows if not int(row.get("is_group") or 0) and not int(row.get("disabled") or 0)
	]


def _match_equity_account(company: str, keywords, what: str) -> str:
	"""One leaf Equity account whose name contains one of `keywords`.

	Refuses on zero matches and on more than one, listing the company's leaf
	equity accounts either way. Picking "the first one" would post a member's
	capital to whichever account happened to sort first, and the error would be
	invisible until somebody read an equity statement.
	"""
	candidates = _leaf_equity_accounts(company)
	matches = [
		row
		for row in candidates
		if any(keyword in str(row.get("account_name") or "").lower() for keyword in keywords)
	]
	if len(matches) == 1:
		return matches[0]["name"]
	listing = ", ".join(row["name"] for row in candidates) or "<none>"
	if not matches:
		raise ToolError(
			f"could not find the {what} account for {company}: no leaf Equity account is named "
			f"after one of {', '.join(keywords[:4])}. Name it explicitly with the "
			f"capital_account / counter_account arguments, or create one with create_account. "
			f"Leaf equity accounts on this company: {listing}. Nothing was created."
		)
	raise ToolError(
		f"{len(matches)} leaf Equity accounts could be the {what} account for {company}: "
		f"{', '.join(row['name'] for row in matches)}. Name the one you mean with "
		"capital_account. Nothing was created."
	)


def _counter_account(args: dict, company: str) -> str:
	"""The cash side of a member event: named by the caller, or the company default."""
	explicit = as_str(args, "counter_account")
	if explicit:
		return resolve_account(explicit, company)
	for field in ("default_bank_account", "default_cash_account"):
		if not compat.has_field("Company", field):
			continue
		account = frappe.db.get_value("Company", company, field)
		if account and frappe.db.exists("Account", account):
			return account
	raise ToolError(
		f"{company} has no default bank or cash account set, so there is no account for the "
		"money side of this event. Pass counter_account explicitly, or set the company "
		"defaults with set_company_defaults. Nothing was created."
	)


# ── create_owner_draw ────────────────────────────────────────────────────────
#
# v0.68.0. `record_member_event` already books a Distribution or a Withdrawal
# against exactly this kind of equity account — `_DISTRIBUTION_KEYWORDS` above
# has named "owner draw" and "member draw" since it was written. What it does
# NOT have is a role gate of its own, or a door that does not first require a
# Cap Table Entry to exist. `create_owner_draw` is that door: a narrower tool,
# for the specific case of "a receipt came in tagged Owner Draw, or somebody
# with authority over the company's equity says money left as a draw" — gated
# on a role rather than on membership in the cap table, and usable whether or
# not this site has adopted the cap table machinery at all.
#
# IT IS NOT A NEW DOCTYPE. Per the gap-closure plan: this is a Journal Entry
# with specific routing — debit an equity "draw" account, credit the bank or
# cash it left from — and nothing else. No new register, no new table.

#: `System Manager` is the universal escape every role gate in this app keeps
#: (see `employee.HR_ROLES`). `Member Manager` is this tool's own: an operator
#: creates the Role in the Desk — typing a new name into a Role field creates
#: it — and assigns it to whoever is actually authorised to move the company's
#: equity. Deliberately NOT one of the six roles `roles.install_roles` ships:
#: those are job descriptions for running the farm, and "may declare an owner
#: draw" is a narrower, higher-stakes permission than any of the six grants.
OWNER_DRAW_ROLES = ("System Manager", "Member Manager")


def _require_member_manager() -> str:
	"""The principal this call is attributed to, once it has proved it may draw equity.

	Same shape as `employee.require_hr_role`, and for the same reason:
	`security.caller_identity()` is whoever Frappe authenticated THIS request —
	a phone or a Desk session presenting its own credential — and is empty on
	the ordinary MCP path, where the operator's client presents a shared token
	and no human identity exists to read. There the principal is
	`frappe.session.user`, which by the time any tool runs is the MCP System
	User the operator configured. Raises rather than returning a flag: there is
	no half-permitted path through this check.
	"""
	actor = security.caller_identity() or str(getattr(frappe.session, "user", "") or "")
	if not actor or actor == "Guest":
		raise ToolError("this call has no identity to attribute an owner draw to. Nothing was created.")
	held = set(frappe.get_roles(actor) or []) or set(roles.all_roles_of(actor) or [])
	if not held & set(OWNER_DRAW_ROLES):
		raise ToolError(
			f"{actor} may not record an owner draw: it holds none of {', '.join(OWNER_DRAW_ROLES)}. "
			"This is the account this app acts as — an operator sets it with `mcp_system_user` on "
			"ERPNext MCP Settings, and grants it the Member Manager role in the Desk. Nothing was "
			"created."
		)
	return actor


def create_owner_draw(args: dict) -> ToolResult:
	"""Record an owner draw / member distribution as a DRAFT Journal Entry.

	REQUIRES THE Member Manager ROLE (or System Manager). Checked before
	anything else runs — an owner draw is equity leaving the company on
	somebody's say-so, and this tool is the say-so.

	NOT AN EXPENSE. Debits an equity "draw" account — matched from the
	company's leaf Equity accounts by the same keyword table
	`record_member_event` uses for a Distribution or a Withdrawal
	(`_DISTRIBUTION_KEYWORDS`: "owner draw", "member draw", "drawings",
	"distribution" …), or named explicitly with `draw_account` — and credits
	the bank or cash account it left from. Never touches an Expense account,
	whatever category a receipt behind this call carried.

	OPTIONALLY LINKED TO A RECEIPT. `receipt` names an Expense Receipt whose
	category is `Owner Draw` — the case `create_purchase_invoice_from_receipt`
	refuses and points here instead. The receipt must not already be linked to
	another document, and this call links it to the Journal Entry it creates,
	the same `linked_doctype`/`linked_document` pair a Purchase Invoice uses.
	The tool works with no receipt at all: an owner draw is frequently just a
	transfer nobody photographed.

	ALWAYS A DRAFT, never submitted. Post it with `submit_journal_entry`, which
	needs its own switch on as well as this tool's — same two-lock shape as
	`record_member_event`.
	"""
	actor = _require_member_manager()
	# v0.332.0. A receipt the submitter marked "I'm covering this" carries its own
	# amount, date, person and card, so each becomes a default rather than a demand.
	covered = _covered_receipt(as_str(args, "receipt") or as_str(args, "expense_receipt"))
	company = resolve_company(as_str(args, "company") or (covered or {}).get("company") or "", required=True)
	if covered and args.get("amount") in (None, ""):
		args = {**args, "amount": covered.get("amount")}
	if covered and not (as_str(args, "date") or as_str(args, "effective_date")):
		args = {**args, "date": str(covered.get("receipt_date") or "")}
	if covered and not (as_str(args, "narrative") or as_str(args, "reason") or as_str(args, "notes")):
		args = {**args, "narrative": _covered_narrative(covered)}

	amount = as_float(args.get("amount"), "amount")
	if args.get("amount") in (None, ""):
		raise ToolError("amount is required. Nothing was created.")
	if amount <= 0:
		raise ToolError(f"amount must be positive — got {amount}. Nothing was created.")

	date_value = as_date(args, "date") or as_date(args, "effective_date")
	if not date_value:
		raise ToolError("date (the posting date for this draw) is required. Nothing was created.")

	narrative = as_str(args, "narrative") or as_str(args, "reason") or as_str(args, "notes")
	if len(narrative) < 8:
		raise ToolError(
			"narrative must be a real explanation — what this draw was for and who authorised it. "
			"It is the part of this record that cannot be reconstructed later. Nothing was created."
		)

	explicit_draw_account = as_str(args, "draw_account") or as_str(args, "equity_account")
	draw_resolved_by = "argument" if explicit_draw_account else "name match"
	if explicit_draw_account:
		draw_account = resolve_account(explicit_draw_account, company)
	elif covered and covered.get("member_draw_account"):
		draw_account = resolve_account(covered["member_draw_account"], company)
		draw_resolved_by = f"Cap Table Entry {covered['member']} (the submitter's draw account)"
	elif covered:
		raise ToolError(
			f'{covered["name"]} is marked "I\'m covering this" by {covered.get("submitted_by") or "nobody"}, '
			"and no draw account is mapped for them: set employee and draw_account on their Cap Table "
			"Entry (update_cap_table_entry), or pass draw_account. Nothing was created."
		)
	else:
		draw_account = _match_equity_account(company, _DISTRIBUTION_KEYWORDS, "owner draw")
	counter_account = (
		covered["card_gl_account"]
		if covered and covered.get("card_gl_account") and not as_str(args, "counter_account")
		else _counter_account(args, company)
	)

	cost_center = as_str(args, "cost_center")
	if cost_center:
		cost_center = resolve_cost_center(cost_center, company)

	party_type = as_str(args, "party_type")
	party = as_str(args, "party")
	if bool(party_type) != bool(party):
		raise ToolError(
			f"party_type is {party_type!r} and party is {party!r}. Set both to attribute the draw "
			"to somebody, or leave both empty. Nothing was created."
		)

	receipt_name = as_str(args, "receipt") or as_str(args, "expense_receipt")
	if receipt_name:
		if not frappe.db.exists(expenses.EXPENSE_RECEIPT, receipt_name):
			raise ToolError(f"no Expense Receipt called {receipt_name!r} on this site. Nothing was created.")
		receipt = frappe.db.get_value(
			expenses.EXPENSE_RECEIPT,
			receipt_name,
			["category", "linked_doctype", "linked_document", "company"],
			as_dict=True,
		)
		if receipt.get("category") != expenses.OWNER_DRAW_CATEGORY and not covered:
			raise ToolError(
				f"expense receipt {receipt_name} is categorised {receipt.get('category')!r}, not "
				f"{expenses.OWNER_DRAW_CATEGORY!r}. Recategorise it with update_expense_receipt "
				"first, or omit receipt if this draw is not from a captured receipt. "
				"Nothing was created."
			)
		if receipt.get("linked_document"):
			raise ToolError(
				f"expense receipt {receipt_name} is already linked to "
				f"{receipt.get('linked_doctype')} {receipt.get('linked_document')}. Nothing was created."
			)
		if receipt.get("company") != company:
			raise ToolError(
				f"expense receipt {receipt_name} belongs to {receipt.get('company')!r}, not "
				f"{company!r}. Nothing was created."
			)

	raw_lines = [
		{
			"account": draw_account,
			"debit": amount,
			"cost_center": cost_center or None,
			"user_remark": "Owner draw",
			"party_type": party_type or None,
			"party": party or None,
		},
		{
			"account": counter_account,
			"credit": amount,
			"cost_center": cost_center or None,
			"user_remark": "Owner draw",
		},
	]
	raw_lines = [{key: value for key, value in line.items() if value is not None} for line in raw_lines]
	lines = mutate.validated_journal_lines(raw_lines, company)
	remark = f"Owner Draw — {narrative}"
	doc = mutate.insert_draft_journal_entry(company, date_value, lines, remark)

	if receipt_name:
		frappe.db.set_value(
			expenses.EXPENSE_RECEIPT,
			receipt_name,
			{"linked_doctype": "Journal Entry", "linked_document": doc.name},
		)

	data = {
		"name": doc.name,
		"docstatus": 0,
		"docstatus_label": "draft",
		"company": company,
		"date": date_value,
		"amount": amount,
		"draw_account": draw_account,
		"draw_account_resolved_by": draw_resolved_by,
		"covered_by_submitter": bool(covered),
		"counter_account": counter_account,
		"cost_center": cost_center or None,
		"party_type": party_type or None,
		"party": party or None,
		"expense_receipt": receipt_name or None,
		"recorded_by": actor,
		"narrative": narrative,
		"next_step": (
			f"Journal Entry {doc.name} is a DRAFT and has moved no balance. Post it with "
			"submit_journal_entry, which needs its own switch on as well as this tool's."
		),
	}
	return ToolResult(
		data,
		f"recorded owner draw of {amount} for {company} on {date_value} by {actor}; "
		f"draft Journal Entry {doc.name}"
		+ (f", linked to expense receipt {receipt_name}" if receipt_name else ""),
		docstatus_delta="none → 0 (draft)",
	)


def _covered_receipt(name: str) -> dict | None:
	"""v0.332.0. An Expense Receipt marked "I'm covering this", with what booking it needs."""
	from . import paid_with

	if not name or not compat.has_field(expenses.EXPENSE_RECEIPT, paid_with.COVERED_FIELD):
		return None
	row = frappe.db.get_value(
		expenses.EXPENSE_RECEIPT,
		name,
		compat.existing_fields(
			expenses.EXPENSE_RECEIPT,
			(
				"name",
				"company",
				"amount",
				"receipt_date",
				"merchant",
				"submitted_by",
				"status",
				paid_with.COVERED_FIELD,
				paid_with.ACCOUNT_FIELD,
				"card_alias",
			),
		),
		as_dict=True,
	)
	if not row or not int(row.get(paid_with.COVERED_FIELD) or 0):
		return None
	row = dict(row)
	if row.get("status") != expenses.APPROVED:
		raise ToolError(
			f"{name} is {row.get('status')!r}, not Approved. A covered purchase is booked once it is "
			"approved (approve_expense_receipt). Nothing was created."
		)
	member = paid_with.member_for(row.get("submitted_by") or "", row.get("company") or "")
	row["member"] = (member or {}).get("name")
	row["member_draw_account"] = (member or {}).get("draw_account")
	bank_account = row.get(paid_with.ACCOUNT_FIELD) or (
		frappe.db.get_value("Bank Card Alias", row["card_alias"], "bank_account")
		if row.get("card_alias")
		else None
	)
	row["card_gl_account"] = (
		frappe.db.get_value("Bank Account", bank_account, "account") if bank_account else None
	)
	return row


def _covered_narrative(row: dict) -> str:
	who = (
		frappe.db.get_value("Employee", row.get("submitted_by"), "employee_name")
		or row.get("submitted_by")
		or "the submitter"
	)
	return (
		f"Covered personally by {who}: {row.get('merchant') or 'purchase'} on {row.get('receipt_date')} "
		f"({row['name']}). The company paid; booked to their draw, not an expense."
	)


# ── 50. create_cap_table_entry ──────────────────────────────────────────────
def create_cap_table_entry(args: dict) -> ToolResult:
	"""Register one member: the anonymous id, and the legal entity behind it."""
	_require_cap_table()
	company = resolve_company(as_str(args, "company"), required=True)
	member_id = as_str(args, "member_id", required=True)
	legal_entity_name = as_str(args, "legal_entity_name", required=True)
	entity_type = _validated_choice(
		CAP_TABLE, "entity_type", as_str(args, "entity_type", required=True), "entity_type"
	)
	admission_date = as_date(args, "admission_date", required=True)
	ownership_percentage = as_float(args.get("ownership_percentage"), "ownership_percentage")
	notes = as_str(args, "notes")
	member_cost_center = as_str(args, "member_cost_center")

	if "retired" in args or "withdrawal_date" in args:
		raise ToolError(
			"a Cap Table Entry cannot be created already retired. Create the member, then "
			"retire them with close_cap_table_entry, which records the exit as a Member "
			"Event rather than silently flipping a flag. Nothing was created."
		)
	if ownership_percentage < 0 or ownership_percentage > 100:
		raise ToolError(f"ownership_percentage must be between 0 and 100, got {ownership_percentage}")

	existing = frappe.db.get_value(CAP_TABLE, {"member_id": member_id, "company": company}, "name")
	if existing:
		raise ToolError(
			f"member {member_id!r} is already registered for {company} as {existing!r}. One entry "
			"per member per company — change it with update_cap_table_entry, or retire it with "
			"close_cap_table_entry. Nothing was created."
		)

	cost_center = ""
	if member_cost_center:
		cost_center = resolve_cost_center(member_cost_center, company)

	dimension = _member_dimension(as_str(args, "member_dimension"))
	dimension_doctype = ""
	dimension_note = (
		f"This site has no {DEFAULT_MEMBER_DIMENSION!r} accounting dimension yet, so nothing "
		"was checked against one. Member postings cannot be tagged until it exists."
	)
	if dimension:
		master = dimension.get("master") or ""
		if master and compat.doctype_exists(master) and not frappe.db.exists(master, member_id):
			raise ToolError(
				f"{member_id!r} is not yet a value of the {dimension['label']} dimension "
				f"(no {master} record of that name), and the cap table exists to name a "
				"member the ledger can already refer to. Create the value first with "
				f"create_dimension_value(dimension_name={dimension['label']!r}, "
				f"value_name={member_id!r}). Nothing was created."
			)
		dimension_doctype = master
		dimension_note = f"Matched against the {dimension['label']} dimension ({master})."

	doc = frappe.new_doc(CAP_TABLE)
	doc.member_id = member_id
	doc.company = company
	doc.legal_entity_name = legal_entity_name
	doc.entity_type = entity_type
	doc.admission_date = admission_date
	doc.ownership_percentage = ownership_percentage
	doc.retired = 0
	if cost_center:
		doc.member_cost_center = cost_center
	if dimension_doctype:
		doc.member_dimension_doctype = dimension_doctype
	if notes:
		doc.notes = notes
	for field, value in _member_mapping(args, company).items():
		doc.set(field, value)
	doc.insert()

	total = _active_ownership_total(company)
	data = {
		**_describe_entry(
			frappe.db.get_value(
				CAP_TABLE, doc.name, compat.existing_fields(CAP_TABLE, _CAP_TABLE_FIELDS), as_dict=True
			)
		),
		"member_dimension_doctype": dimension_doctype or None,
		"active_ownership_total": total,
		"note": dimension_note,
		"next_step": (
			"Record contributions, distributions and transfers against this member with "
			"record_member_event. The legal name lives here and nowhere else — postings are "
			f"tagged {member_id!r}."
		),
	}
	if abs(total - 100.0) > 0.01:
		data["warning"] = (
			f"Active members' ownership now totals {total}%, not 100%. That is legitimate "
			"mid-transition and is not refused, but it is worth resolving before a statement "
			"is produced."
		)
	return ToolResult(
		data,
		f"registered {member_id} ({legal_entity_name}, {entity_type}) in the {company} cap table "
		f"at {ownership_percentage}%",
		docstatus_delta="none → 0 (created)",
	)


def _member_mapping(args: dict, company: str) -> dict:
	"""v0.332.0. `employee` and `draw_account`, checked; only the keys the caller sent.

	'' clears one. The draw account must be a leaf Equity account of this company —
	the point of it is that a covered purchase never lands on the P&L.
	"""
	out = {}
	if "employee" in args:
		employee = as_str(args, "employee")
		if employee:
			if not compat.doctype_exists("Employee") or not frappe.db.exists("Employee", employee):
				raise ToolError(f"no Employee called {employee!r} on this site. Nothing was changed.")
			if not compat.has_field(CAP_TABLE, "employee"):
				raise ToolError("this site's Cap Table Entry has no employee column yet — run bench migrate.")
		out["employee"] = employee
	if "draw_account" in args:
		value = as_str(args, "draw_account")
		account = resolve_account(value, company) if value else ""
		if account:
			row = frappe.db.get_value("Account", account, ["root_type", "is_group"], as_dict=True) or {}
			if row.get("root_type") != "Equity" or int(row.get("is_group") or 0):
				raise ToolError(
					f"draw_account {account!r} must be a leaf Equity account (a member draw), not "
					f"{row.get('root_type') or 'unknown'}{' group' if int(row.get('is_group') or 0) else ''}. "
					"Nothing was changed."
				)
		out["draw_account"] = account
	return out


def _active_ownership_total(company: str) -> float:
	rows = frappe.db.get_all(
		CAP_TABLE,
		filters={"company": company, "retired": 0},
		fields=["ownership_percentage"],
		limit=500,
	)
	return round(sum(float(row.get("ownership_percentage") or 0) for row in rows), 4)


# ── 51. update_cap_table_entry ──────────────────────────────────────────────
def update_cap_table_entry(args: dict) -> ToolResult:
	"""Change a registered member's details. Cannot retire, and cannot re-key."""
	_require_cap_table()
	company = as_str(args, "company")
	entry = cap_table_entry(as_str(args, "member", required=True), company)
	company = entry["company"]

	if "retired" in args or "withdrawal_date" in args:
		raise ToolError(
			"update_cap_table_entry cannot retire a member. Use close_cap_table_entry: it "
			"takes the withdrawal date and a narrative, and writes a Member Event so the exit "
			"appears in the trail instead of only as a changed checkbox. Nothing was changed."
		)
	if "member_id" in args or "new_member_id" in args:
		raise ToolError(
			"the member_id cannot be changed. It is the key every posting on this site is "
			"tagged with, and the docname is built from it — changing it here would leave "
			"every journal entry line pointing at a member that no longer exists. Nothing "
			"was changed."
		)

	changes = {}
	updates = {}

	for field, label in (
		("legal_entity_name", "legal_entity_name"),
		("notes", "notes"),
	):
		value = as_str(args, label)
		if value and value != str(entry.get(field) or ""):
			updates[field] = value
			changes[field] = [entry.get(field) or "", value]

	if as_str(args, "entity_type"):
		entity_type = _validated_choice(CAP_TABLE, "entity_type", as_str(args, "entity_type"), "entity_type")
		if entity_type != entry.get("entity_type"):
			updates["entity_type"] = entity_type
			changes["entity_type"] = [entry.get("entity_type"), entity_type]

	admission_date = as_date(args, "admission_date")
	if admission_date and admission_date != str(entry.get("admission_date") or ""):
		updates["admission_date"] = admission_date
		changes["admission_date"] = [str(entry.get("admission_date") or ""), admission_date]

	if args.get("ownership_percentage") not in (None, ""):
		percentage = as_float(args.get("ownership_percentage"), "ownership_percentage")
		if percentage < 0 or percentage > 100:
			raise ToolError(f"ownership_percentage must be between 0 and 100, got {percentage}")
		if abs(percentage - float(entry.get("ownership_percentage") or 0)) > 1e-9:
			updates["ownership_percentage"] = percentage
			changes["ownership_percentage"] = [float(entry.get("ownership_percentage") or 0), percentage]

	if "member_cost_center" in args:
		requested = as_str(args, "member_cost_center")
		cost_center = resolve_cost_center(requested, company) if requested else ""
		if cost_center != str(entry.get("member_cost_center") or ""):
			updates["member_cost_center"] = cost_center or None
			changes["member_cost_center"] = [entry.get("member_cost_center") or "", cost_center]

	for field, value in _member_mapping(args, company).items():
		if str(value or "") != str(entry.get(field) or ""):
			updates[field] = value or None
			changes[field] = [entry.get(field) or "", value or ""]

	if not changes:
		raise ToolError(
			f"nothing to change on {entry['name']}. Pass at least one of legal_entity_name, "
			"entity_type, admission_date, ownership_percentage, member_cost_center, employee, "
			"draw_account or notes, "
			"with a value that differs from the current one."
		)

	doc = frappe.get_doc(CAP_TABLE, entry["name"])
	for field, value in updates.items():
		doc.set(field, value)
	doc.save()

	after = frappe.db.get_value(
		CAP_TABLE, entry["name"], compat.existing_fields(CAP_TABLE, _CAP_TABLE_FIELDS), as_dict=True
	)
	total = _active_ownership_total(company)
	data = {
		**_describe_entry(dict(after)),
		"changes": changes,
		"active_ownership_total": total,
		"note": (
			"Historical postings are untouched: they are tagged with the member id, which "
			"this tool cannot change."
		),
	}
	if abs(total - 100.0) > 0.01 and "ownership_percentage" in changes:
		data["warning"] = f"Active members' ownership now totals {total}%, not 100%."
	summary = ", ".join(f"{field} {before!r} → {value!r}" for field, (before, value) in changes.items())
	return ToolResult(data, f"updated Cap Table Entry {entry['name']}: {summary}", docstatus_delta="")


# ── 52. list_cap_table ──────────────────────────────────────────────────────
def list_cap_table(args: dict) -> ToolResult:
	"""One company's member register, retired members included by default."""
	_require_cap_table()
	company = resolve_company(as_str(args, "company"), required=True)
	include_retired = as_bool(args, "include_retired", True)

	filters = {"company": company}
	if not include_retired:
		filters["retired"] = 0
	fields = compat.existing_fields(CAP_TABLE, _CAP_TABLE_FIELDS)
	rows = frappe.db.get_all(CAP_TABLE, filters=filters, fields=fields, order_by="member_id asc", limit=500)

	members = [_describe_entry(dict(row)) for row in rows]
	active = [member for member in members if not member["retired"]]
	total = round(sum(member["ownership_percentage"] for member in active), 4)
	retired_count = frappe.db.count(CAP_TABLE, {"company": company, "retired": 1})

	data = {
		"company": company,
		"members": members,
		"count": len(members),
		"active_count": len(active),
		"retired_count": retired_count,
		"retired_included": bool(include_retired),
		"active_ownership_total": total,
		"ownership_balances": abs(total - 100.0) <= 0.01,
		"member_dimension": (_member_dimension() or {}).get("label"),
		"note": (
			"This is the only place on the site that maps an anonymous member id to a legal "
			"name. Retired members are listed by default: the postings they are tagged on do "
			"not disappear when they leave, so neither should the row that explains them."
		),
	}
	if not data["ownership_balances"]:
		data["warning"] = (
			f"Active ownership totals {total}%, not 100%. Check for a member admitted without "
			"a percentage, or one retired without their share being reallocated."
		)
	return ToolResult(
		data,
		f"{len(members)} member(s) in the {company} cap table "
		f"({len(active)} active, {retired_count} retired, {total}% allocated)",
	)


# ── 53. close_cap_table_entry ───────────────────────────────────────────────
def close_cap_table_entry(args: dict) -> ToolResult:
	"""Retire a member: set the exit date, and write the exit into the event trail.

	Deliberately does NOT move money. A member leaving usually involves a final
	distribution, and that is `record_member_event` — a separate call with its own
	amount, its own accounts and its own narrative. Bundling the two would mean
	the tool that closes a member is also a tool that can pay one.
	"""
	_require_cap_table()
	company = as_str(args, "company")
	entry = cap_table_entry(as_str(args, "member", required=True), company)
	company = entry["company"]
	withdrawal_date = as_date(args, "withdrawal_date", required=True)
	notes = as_str(args, "notes", required=True)
	if len(notes) < 4:
		raise ToolError("notes must be a real explanation of the exit, not a placeholder")

	if bool(int(entry.get("retired") or 0)):
		raise ToolError(
			f"{entry['name']} is already retired (withdrawal date "
			f"{entry.get('withdrawal_date') or 'not recorded'}). Nothing was changed."
		)
	admission = str(entry.get("admission_date") or "")
	if admission and withdrawal_date < admission:
		raise ToolError(
			f"withdrawal_date {withdrawal_date} is before this member's admission date "
			f"{admission}. Nothing was changed."
		)

	doc = frappe.get_doc(CAP_TABLE, entry["name"])
	doc.retired = 1
	doc.withdrawal_date = withdrawal_date
	doc.notes = _append_note(doc.get("notes"), f"Retired {withdrawal_date}: {notes}")
	doc.save()

	event = frappe.new_doc(MEMBER_EVENT)
	event.event_type = "Withdrawal"
	event.effective_date = withdrawal_date
	event.amount = 0
	event.member = entry["name"]
	event.company = company
	event.narrative = notes
	event.insert()

	total = _active_ownership_total(company)
	data = {
		**_describe_entry(
			dict(
				frappe.db.get_value(
					CAP_TABLE,
					entry["name"],
					compat.existing_fields(CAP_TABLE, _CAP_TABLE_FIELDS),
					as_dict=True,
				)
			)
		),
		"member_event": event.name,
		"active_ownership_total": total,
		"note": (
			"Nothing was deleted and no money moved. The entry stays in the register, every "
			"posting tagged with this member id stays exactly as it was, and the exit is now "
			"in the event trail. A final distribution, if there is one, is a separate "
			"record_member_event call."
		),
		"next_step": (
			f"Active ownership now totals {total}%. If this member's share passes to another, "
			"record it as a Transfer with record_member_event and update the receiving "
			"member's percentage."
		),
	}
	return ToolResult(
		data,
		f"retired {entry['member_id']} ({entry.get('legal_entity_name')}) from the {company} "
		f"cap table as of {withdrawal_date}; Member Event {event.name}",
		docstatus_delta="",
	)


def _append_note(existing, addition: str) -> str:
	existing = str(existing or "").strip()
	return f"{existing}\n\n{addition}".strip() if existing else addition


# ── 54. record_member_event ─────────────────────────────────────────────────
def record_member_event(args: dict) -> ToolResult:
	"""Write one member event, and the DRAFT Journal Entry that books it.

	The Journal Entry is created here rather than left to `create_journal_entry`
	because getting it right means knowing the event type: which side the equity
	account is on, which member each line is tagged with, and — for a transfer —
	that there are two members and the money never leaves the company. Enabling
	this tool grants the creation of that draft; it does not grant posting it.
	"""
	_require_cap_table()
	company = resolve_company(as_str(args, "company"), required=True)
	event_type = _validated_choice(
		MEMBER_EVENT, "event_type", as_str(args, "event_type", required=True), "event_type"
	)
	effective_date = as_date(args, "effective_date", required=True)
	narrative = as_str(args, "narrative", required=True)
	if len(narrative) < 8:
		raise ToolError(
			"narrative must be a real explanation — what happened and what authorises it. "
			"It is the part of this record that cannot be reconstructed later. Nothing was created."
		)
	amount = as_float(args.get("amount"), "amount")
	if amount < 0:
		raise ToolError(
			"amount cannot be negative. A distribution is its own event_type, not a "
			"contribution with a minus sign. Nothing was created."
		)

	entry = cap_table_entry(as_str(args, "member", required=True), company)
	counterparty = None
	if as_str(args, "counterparty_member"):
		counterparty = cap_table_entry(as_str(args, "counterparty_member"), company)
		if counterparty["name"] == entry["name"]:
			raise ToolError("counterparty_member is the same member. Nothing was created.")
	if event_type in PAIRED_EVENTS and counterparty is None:
		raise ToolError(
			f"a {event_type} moves an interest between two members, so counterparty_member is "
			"required — the member it moves TO. `member` is the one it moves FROM. Nothing "
			"was created."
		)
	if event_type in POSTING_EVENTS and amount <= 0 and not as_str(args, "offset_je"):
		# Reallocation is the exception: moving percentages between two members
		# without moving capital is a real event, and it books nothing. Every other
		# posting type with no amount is a caller who forgot one.
		if event_type != "Reallocation":
			raise ToolError(
				f"a {event_type} needs a positive amount, or an offset_je naming the Journal "
				"Entry that already books it. Nothing was created."
			)

	offset_je = as_str(args, "offset_je")
	if offset_je:
		if not frappe.db.exists("Journal Entry", offset_je):
			raise ToolError(f"no Journal Entry named {offset_je!r}. Nothing was created.")
		je_company = frappe.db.get_value("Journal Entry", offset_je, "company")
		if je_company != company:
			raise ToolError(
				f"Journal Entry {offset_je} belongs to {je_company!r}, not {company!r}. Nothing was created."
			)

	created_je = None
	je_lines = []
	accounts_used = {}
	if not offset_je and event_type in POSTING_EVENTS and amount > 0:
		dimension = _require_member_dimension(as_str(args, "member_dimension"))
		created_je, je_lines, accounts_used = _post_member_event(
			args, company, event_type, effective_date, amount, entry, counterparty, dimension, narrative
		)

	event = frappe.new_doc(MEMBER_EVENT)
	event.event_type = event_type
	event.effective_date = effective_date
	event.amount = amount
	event.member = entry["name"]
	if counterparty:
		event.counterparty_member = counterparty["name"]
	event.company = company
	event.narrative = narrative
	if offset_je or created_je:
		event.offset_je = offset_je or created_je.name
	event.insert()

	data = {
		"name": event.name,
		"event_type": event_type,
		"effective_date": effective_date,
		"amount": amount,
		"member": entry["name"],
		"member_id": entry["member_id"],
		"counterparty_member": (counterparty or {}).get("name"),
		"company": company,
		"offset_je": event.get("offset_je"),
		"journal_entry_created": bool(created_je),
		"journal_entry_lines": je_lines,
		"accounts_used": accounts_used,
		"narrative": narrative,
		"next_step": (
			f"The Journal Entry {created_je.name} is a DRAFT and has moved no balance. "
			"Post it with submit_member_event, which needs the submit_journal_entry switch on "
			"as well as this one."
			if created_je
			else (
				"No Journal Entry was written for this event."
				+ (f" It is booked by {offset_je}." if offset_je else " Nothing was posted.")
			)
		),
	}
	summary = (
		f"recorded {event_type} of {amount} for {entry['member_id']} on {effective_date}"
		+ (f" → {counterparty['member_id']}" if counterparty else "")
		+ (f"; draft Journal Entry {created_je.name}" if created_je else "")
	)
	return ToolResult(data, summary, docstatus_delta="none → 0 (created)")


def _post_member_event(
	args, company, event_type, effective_date, amount, entry, counterparty, dimension, narrative
):
	"""Build and insert the DRAFT Journal Entry for one member event.

	Every line carries the member dimension, including the cash side. That is a
	choice: tagging only the equity line makes a balance sheet filtered by member
	fail to balance, and the first person to notice is usually an auditor.
	"""
	fieldname = dimension["fieldname"]
	member_value = _dimension_value_for(entry, dimension)
	cost_center = entry.get("member_cost_center") or None

	explicit_capital = as_str(args, "capital_account")
	if event_type in ("Distribution", "Withdrawal"):
		capital_account = (
			resolve_account(explicit_capital, company)
			if explicit_capital
			else _match_equity_account(company, _DISTRIBUTION_KEYWORDS, "member distribution")
		)
	else:
		capital_account = (
			resolve_account(explicit_capital, company)
			if explicit_capital
			else _match_equity_account(company, _CAPITAL_KEYWORDS, "member capital")
		)

	accounts_used = {
		"capital_account": capital_account,
		"resolved_by": "argument" if explicit_capital else "name match",
	}

	if event_type in PAIRED_EVENTS:
		counter_value = _dimension_value_for(counterparty, dimension)
		raw_lines = [
			{
				"account": capital_account,
				"debit": amount,
				"cost_center": cost_center,
				"user_remark": f"{event_type} from {entry['member_id']}",
				"dimensions": {fieldname: member_value},
			},
			{
				"account": capital_account,
				"credit": amount,
				"cost_center": counterparty.get("member_cost_center") or None,
				"user_remark": f"{event_type} to {counterparty['member_id']}",
				"dimensions": {fieldname: counter_value},
			},
		]
	else:
		counter_account = _counter_account(args, company)
		accounts_used["counter_account"] = counter_account
		if event_type == "Contribution":
			raw_lines = [
				{
					"account": counter_account,
					"debit": amount,
					"cost_center": cost_center,
					"user_remark": f"Contribution from {entry['member_id']}",
					"dimensions": {fieldname: member_value},
				},
				{
					"account": capital_account,
					"credit": amount,
					"cost_center": cost_center,
					"user_remark": f"Capital of {entry['member_id']}",
					"dimensions": {fieldname: member_value},
				},
			]
		else:  # Distribution / Withdrawal
			raw_lines = [
				{
					"account": capital_account,
					"debit": amount,
					"cost_center": cost_center,
					"user_remark": f"{event_type} to {entry['member_id']}",
					"dimensions": {fieldname: member_value},
				},
				{
					"account": counter_account,
					"credit": amount,
					"cost_center": cost_center,
					"user_remark": f"{event_type} to {entry['member_id']}",
					"dimensions": {fieldname: member_value},
				},
			]

	raw_lines = [{key: value for key, value in line.items() if value is not None} for line in raw_lines]
	lines = mutate.validated_journal_lines(raw_lines, company)
	remark = f"{event_type} — {entry['member_id']}: {narrative}"
	doc = mutate.insert_draft_journal_entry(company, effective_date, lines, remark)
	described = [
		{
			"account": line["account"],
			"debit": line.get("debit") or 0,
			"credit": line.get("credit") or 0,
			fieldname: line.get(fieldname),
		}
		for line in lines
	]
	return doc, described, accounts_used


# ── 55. list_member_events ──────────────────────────────────────────────────
def list_member_events(args: dict) -> ToolResult:
	"""The equity trail: what happened to whose interest, and why."""
	_require_cap_table()
	company = resolve_company(as_str(args, "company"), required=True)
	limit = as_limit(args)
	filters = {"company": company}

	member = as_str(args, "member")
	entry = cap_table_entry(member, company) if member else None
	if entry:
		filters["member"] = entry["name"]

	event_type = as_str(args, "event_type")
	if event_type:
		filters["event_type"] = _validated_choice(MEMBER_EVENT, "event_type", event_type, "event_type")

	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters["effective_date"] = ("between", [from_date, to_date])
	elif from_date:
		filters["effective_date"] = (">=", from_date)
	elif to_date:
		filters["effective_date"] = ("<=", to_date)

	include_superseded = as_bool(args, "include_superseded", True)

	fields = compat.existing_fields(MEMBER_EVENT, _MEMBER_EVENT_FIELDS)
	rows = frappe.db.get_all(
		MEMBER_EVENT, filters=filters, fields=fields, order_by="effective_date desc", limit=limit + 1
	)
	truncated = len(rows) > limit
	rows = rows[:limit]

	names = {row.get("member") for row in rows} | {row.get("counterparty_member") for row in rows}
	identities = _member_identities([name for name in names if name])

	events = []
	for row in rows:
		superseded_by = row.get("superseded_by") or None
		if superseded_by and not include_superseded:
			continue
		events.append(
			{
				"name": row.get("name"),
				"event_type": row.get("event_type"),
				"effective_date": str(row.get("effective_date") or "") or None,
				"amount": float(row.get("amount") or 0),
				"member": row.get("member"),
				"member_id": identities.get(row.get("member"), {}).get("member_id"),
				"legal_entity_name": identities.get(row.get("member"), {}).get("legal_entity_name"),
				"counterparty_member": row.get("counterparty_member") or None,
				"counterparty_member_id": identities.get(row.get("counterparty_member"), {}).get("member_id"),
				"offset_je": row.get("offset_je") or None,
				"superseded_by": superseded_by,
				"narrative": row.get("narrative"),
				"recorded_on": str(row.get("creation") or "") or None,
			}
		)

	totals = {}
	for event in events:
		totals[event["event_type"]] = round(totals.get(event["event_type"], 0.0) + event["amount"], 2)

	data = {
		"company": company,
		"events": events,
		"count": len(events),
		"totals_by_event_type": totals,
		"limit": limit,
		"truncated": truncated,
		"filters": {
			"member": entry["name"] if entry else None,
			"event_type": event_type or None,
			"from_date": from_date,
			"to_date": to_date,
			"include_superseded": bool(include_superseded),
		},
		"note": (
			"Legal names come from the Cap Table Entry each event links to; the events "
			"themselves hold only the anonymous member id. An event with superseded_by set "
			"has been corrected by a later one and should not be totalled twice."
		),
	}
	return ToolResult(data, f"{len(events)} member event(s) for {company}")


def _member_identities(names: list[str]) -> dict:
	if not names:
		return {}
	fields = compat.existing_fields(CAP_TABLE, ("name", "member_id", "legal_entity_name"))
	rows = frappe.db.get_all(CAP_TABLE, filters={"name": ("in", names)}, fields=fields, limit=500)
	return {row["name"]: dict(row) for row in rows}


# ── 56. submit_member_event ─────────────────────────────────────────────────
def submit_member_event(args: dict) -> ToolResult:
	"""Post the draft Journal Entry a member event is waiting on.

	Checks TWO switches. Its own, like every tool, and `submit_journal_entry`'s —
	because that switch is where an operator decided whether an AI client may move
	a balance at all, and a second door into the same room with a different lock
	would make that decision meaningless.
	"""
	_require_cap_table()
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(MEMBER_EVENT, name):
		raise ToolError(f"no Member Event named {name!r}. list_member_events has the names.")
	event = frappe.db.get_value(
		MEMBER_EVENT, name, compat.existing_fields(MEMBER_EVENT, _MEMBER_EVENT_FIELDS), as_dict=True
	)
	offset_je = event.get("offset_je")
	if not offset_je:
		raise ToolError(
			f"Member Event {name} has no Journal Entry to post — it books no money, which is "
			"normal for an admission or a reallocation of percentages. Nothing was changed."
		)
	if not settings.tool_enabled("submit_journal_entry"):
		raise ToolError(
			"posting this event means submitting Journal Entry "
			f"{offset_je}, and the submit_journal_entry tool is switched off on this site. "
			"That switch is where an operator decides whether an AI client may move a "
			"balance, so this tool honours it too. An operator must tick "
			"'allow_submit_journal_entry' in ERPNext MCP Settings. Nothing was changed."
		)

	result = mutate.submit_journal_entry({"name": offset_je})
	data = {
		"name": name,
		"event_type": event.get("event_type"),
		"member": event.get("member"),
		"effective_date": str(event.get("effective_date") or ""),
		"amount": float(event.get("amount") or 0),
		"journal_entry": result.data,
		"note": (
			"The Member Event itself is a record, not a submittable document; what moved is "
			"the Journal Entry it links to."
		),
	}
	return ToolResult(
		data,
		f"posted Member Event {name} ({event.get('event_type')}) via Journal Entry {offset_je}",
		docstatus_delta=result.docstatus_delta,
	)


# ── 57. attach_governance_document ──────────────────────────────────────────
def attach_governance_document(args: dict) -> ToolResult:
	"""File a governing document in the archive, with its content attached."""
	file_url = as_str(args, "file_url")
	file_content = as_str(args, "file_content")
	file_name = as_str(args, "file_name")

	if file_content and file_url:
		raise ToolError(
			"pass file_content (the document itself, base64) or file_url (where it already "
			"lives), not both. Nothing was created."
		)
	if file_content and not file_name:
		raise ToolError(
			"file_name is required with file_content — an archive entry whose file has no "
			"name is one nobody can identify later. Nothing was created."
		)

	return file_governance_document(
		args,
		content=_decoded_file_content(file_content) if file_content else b"",
		file_name=file_name,
		file_url=file_url,
	)


def file_governance_document(
	args: dict,
	*,
	content: bytes = b"",
	file_name: str = "",
	file_url: str = "",
	tail: str = "Nothing was created.",
) -> ToolResult:
	"""Create one Governance Document and attach `content` to it, if there is any.

	Public and taking BYTES rather than base64, because `commit_staged_file` files
	governance documents too and its bytes never travelled through a single tool
	call. Routing it back through `attach_governance_document` would have meant
	re-encoding a reassembled 5 MB PDF to base64 only to have
	`decode_base64_content` refuse it against a ceiling that describes what fits
	in one JSON call — a limit that is simply not a fact about a chunked upload.

	Everything an archive entry has to be right about is decided here, so both
	doors enforce the same rules: the category has to be one this site's DocType
	offers, one company cannot hold two documents of the same category and title,
	and a superseded document has to exist, belong to the same company and not
	already have been superseded. That last one is why amendments chain rather
	than fork.
	"""
	compat.require_doctype(
		GOVERNANCE_DOCUMENT,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)
	company = resolve_company(as_str(args, "company"), required=True)
	title = as_str(args, "title", required=True)
	category = _validated_choice(
		GOVERNANCE_DOCUMENT, "category", as_str(args, "category", required=True), "category"
	)
	effective_date = as_date(args, "effective_date")
	execution_date = as_date(args, "execution_date")
	parties = as_str(args, "parties")
	notes = as_str(args, "notes")

	duplicate = frappe.db.get_value(
		GOVERNANCE_DOCUMENT, {"company": company, "category": category, "title": title}, "name"
	)
	if duplicate:
		raise ToolError(
			f"{company} already has a {category} titled {title!r} ({duplicate}). Filing it twice "
			"would leave two documents claiming to be the same one. Use a title that includes "
			"the version or date, or amend the existing entry by filing the new document with "
			f"supersedes={duplicate!r}. {tail}"
		)

	supersedes = as_str(args, "supersedes")
	if supersedes:
		if not frappe.db.exists(GOVERNANCE_DOCUMENT, supersedes):
			raise ToolError(f"no Governance Document named {supersedes!r}. {tail}")
		superseded_row = frappe.db.get_value(
			GOVERNANCE_DOCUMENT, supersedes, ["company", "superseded_by", "title"], as_dict=True
		)
		if superseded_row.get("company") != company:
			raise ToolError(
				f"{supersedes} belongs to {superseded_row.get('company')!r}, not {company!r}. {tail}"
			)
		if superseded_row.get("superseded_by"):
			raise ToolError(
				f"{supersedes} ({superseded_row.get('title')}) has already been superseded by "
				f"{superseded_row['superseded_by']}. Chain the new document onto the end of "
				f"the chain, not the middle of it. {tail}"
			)

	doc = frappe.new_doc(GOVERNANCE_DOCUMENT)
	doc.title = title
	doc.category = category
	doc.company = company
	if effective_date:
		doc.effective_date = effective_date
	if execution_date:
		doc.execution_date = execution_date
	if supersedes:
		doc.supersedes = supersedes
	if parties:
		doc.parties = parties
	if notes:
		doc.notes = notes
	if file_url:
		doc.attached_file = file_url
	doc.insert()

	attachment = None
	if content:
		attachment = files.insert_attachment(
			file_name,
			content,
			is_private=True,
			doctype=GOVERNANCE_DOCUMENT,
			name=doc.name,
			attached_to_field="attached_file",
		)
		doc.db_set("attached_file", attachment.get("file_url"))

	if supersedes:
		frappe.db.set_value(GOVERNANCE_DOCUMENT, supersedes, "superseded_by", doc.name)

	data = {
		"name": doc.name,
		"title": title,
		"category": category,
		"company": company,
		"effective_date": effective_date,
		"execution_date": execution_date,
		"supersedes": supersedes or None,
		"attached_file": attachment.get("file_url") if attachment else (file_url or None),
		"attachment": (
			{
				"name": attachment.name,
				"file_name": attachment.get("file_name"),
				"file_size": len(content),
				"is_private": True,
			}
			if attachment
			else None
		),
		"note": (
			"The uploaded file is stored PRIVATE and attached to this record; reading it back "
			"goes through get_governance_document_content, which enforces read permission on "
			"this document."
			if attachment
			else (
				"No content was uploaded. The entry records where the document lives; whoever "
				"needs it has to be able to reach that URL."
				if file_url
				else "No file is attached yet. Attach one in the Desk, or file it again with "
				"file_content once you have it."
			)
		),
		"next_step": (
			f"{supersedes} is now marked superseded by this document, so a reader following "
			"the chain forward lands here."
			if supersedes
			else "Later amendments should name this document in `supersedes`."
		),
	}
	return ToolResult(
		data,
		f"filed {category} {title!r} for {company} as {doc.name}"
		+ (f", superseding {supersedes}" if supersedes else "")
		+ (f" with {file_name} ({len(content)} bytes)" if content else ""),
		docstatus_delta="none → 0 (created)",
	)


def _decoded_file_content(file_content: str) -> bytes:
	"""Base64 in, bytes out, refusing anything too large to be sane in a JSON call.

	The rule lives in `files.decode_base64_content` because `attach_file_to_document`
	has to apply exactly the same one, and a second copy would be a second ceiling
	to forget to raise.
	"""
	return files.decode_base64_content(file_content, tail="Nothing was created.")


# ── 58. list_governance_documents ───────────────────────────────────────────
def list_governance_documents(args: dict) -> ToolResult:
	"""What is in the archive, and which entries are still the operative ones."""
	compat.require_doctype(GOVERNANCE_DOCUMENT, "It ships with erpnext_mcp.")
	company = resolve_company(as_str(args, "company"), required=True)
	limit = as_limit(args)
	include_superseded = as_bool(args, "include_superseded", True)

	filters = {"company": company}
	category = as_str(args, "category")
	if category:
		filters["category"] = _validated_choice(GOVERNANCE_DOCUMENT, "category", category, "category")

	fields = compat.existing_fields(GOVERNANCE_DOCUMENT, _GOVERNANCE_FIELDS)
	rows = frappe.db.get_all(
		GOVERNANCE_DOCUMENT, filters=filters, fields=fields, order_by="effective_date desc", limit=limit
	)

	documents = []
	for row in rows:
		superseded_by = row.get("superseded_by") or None
		if superseded_by and not include_superseded:
			continue
		documents.append(
			{
				"name": row.get("name"),
				"title": row.get("title"),
				"category": row.get("category"),
				"effective_date": str(row.get("effective_date") or "") or None,
				"execution_date": str(row.get("execution_date") or "") or None,
				"supersedes": row.get("supersedes") or None,
				"superseded_by": superseded_by,
				"operative": not superseded_by,
				"attached_file": row.get("attached_file") or None,
				"attachment_count": frappe.db.count(
					"File",
					{"attached_to_doctype": GOVERNANCE_DOCUMENT, "attached_to_name": row.get("name")},
				),
				"parties": row.get("parties"),
				"filed_on": str(row.get("creation") or "") or None,
			}
		)

	by_category = {}
	for document in documents:
		by_category[document["category"]] = by_category.get(document["category"], 0) + 1

	data = {
		"company": company,
		"documents": documents,
		"count": len(documents),
		"operative_count": len([document for document in documents if document["operative"]]),
		"by_category": by_category,
		"superseded_included": bool(include_superseded),
		"limit": limit,
		"note": (
			"`operative` is true for a document nothing has superseded — those are the ones "
			"in force. Read a document's content with get_governance_document_content."
		),
	}
	return ToolResult(data, f"{len(documents)} governance document(s) for {company}")


# ── 59. get_governance_document_content ─────────────────────────────────────
def get_governance_document_content(args: dict) -> ToolResult:
	"""One archived document's metadata, and the bytes of its attachment.

	The content goes through the same path `get_attachment_content` uses, which
	means the same permission check on the parent document and the same size cap.
	A governing document is exactly the kind of file the attachment tools exist to
	be careful with.
	"""
	compat.require_doctype(GOVERNANCE_DOCUMENT, "It ships with erpnext_mcp.")
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(GOVERNANCE_DOCUMENT, name):
		raise ToolError(f"no Governance Document named {name!r}. list_governance_documents has the names.")
	row = frappe.db.get_value(
		GOVERNANCE_DOCUMENT,
		name,
		compat.existing_fields(GOVERNANCE_DOCUMENT, _GOVERNANCE_FIELDS),
		as_dict=True,
	)

	attachments = frappe.db.get_all(
		"File",
		filters={"attached_to_doctype": GOVERNANCE_DOCUMENT, "attached_to_name": name},
		fields=compat.existing_fields("File", ("name", "file_name", "file_url", "file_size", "is_private")),
		order_by="creation asc",
		limit=50,
	)

	metadata = {
		"name": row.get("name"),
		"title": row.get("title"),
		"category": row.get("category"),
		"company": row.get("company"),
		"effective_date": str(row.get("effective_date") or "") or None,
		"execution_date": str(row.get("execution_date") or "") or None,
		"supersedes": row.get("supersedes") or None,
		"superseded_by": row.get("superseded_by") or None,
		"operative": not row.get("superseded_by"),
		"parties": row.get("parties"),
		"notes": row.get("notes"),
		"attachments": [dict(attachment) for attachment in attachments],
	}

	requested = as_str(args, "file")
	if not attachments:
		metadata["content"] = None
		metadata["note"] = "This entry has no attached file" + (
			f"; the document is recorded as living at {row.get('attached_file')}."
			if row.get("attached_file")
			else "."
		)
		return ToolResult(metadata, f"governance document {name} ({row.get('title')}): no attachment")

	chosen = None
	for attachment in attachments:
		if requested and requested not in (attachment.get("name"), attachment.get("file_name")):
			continue
		chosen = attachment
		break
	if chosen is None:
		raise ToolError(
			f"{name} has no attachment called {requested!r}. It has: "
			f"{', '.join(str(attachment.get('file_name')) for attachment in attachments)}."
		)

	content = files.get_attachment_content(
		{"name": chosen["name"], **({"max_bytes": args["max_bytes"]} if args.get("max_bytes") else {})}
	)
	metadata["content"] = content.data
	metadata["note"] = (
		"Content is base64. Read permission on the Governance Document was enforced before "
		"it was returned, as it is for every attachment this app hands over."
	)
	if len(attachments) > 1 and not requested:
		metadata["note"] += (
			f" This entry has {len(attachments)} attachments and the first was returned; "
			"pass `file` to choose another."
		)
	return ToolResult(
		metadata,
		f"read governance document {name} ({row.get('title')}) — {content.data.get('file_name')}",
	)


# ── 136. regenerate_governance_document_pdf ─────────────────────────────────
def regenerate_governance_document_pdf(args: dict) -> ToolResult:
	"""Convert a governance document's .docx attachment to PDF and file it beside it.

	THE PROBLEM IT SOLVES, WHICH IS SMALLER AND MORE ANNOYING THAN IT SOUNDS.
	Several archive entries landed as .docx only — the Q3 25 and Q1/Q2 26
	quarterlies, the 2025 annual. A .docx is an editing format: it renders
	differently in different applications, some of them refuse to open it at all
	(Tim's Pages did), and "the copy on file" stops being one thing the moment two
	people open it in two programs. A governance document's primary format is a
	PDF. The .docx is the version somebody amends.

	SO THE .docx IS KEPT. This adds a PDF; it never removes the source. An archive
	that threw away the editable original to gain a fixed one would have traded a
	problem for a worse one.

	IT REPOINTS `attached_file` AT THE PDF, and says so. That field is what a
	reader following the archive lands on, and the whole point of the exercise is
	that they land on something that opens.

	IT DEPENDS ON A BINARY THIS APP DOES NOT SHIP. Converting a .docx means
	LibreOffice; see `erpnext_mcp/convert.py` for why reimplementing it is not on
	the table. A container without it gets a refusal naming the package to
	install, checked BEFORE anything is read, and nothing is installed at runtime.

	REFUSES: an entry with no .docx; a `source_docx_file` that is not attached to
	this entry or is not a .docx; an entry that already has a PDF, unless
	`overwrite=true` — and then it names the File it deleted, because deleting an
	attachment from a governance archive is not something to do quietly.
	"""
	compat.require_doctype(GOVERNANCE_DOCUMENT, "It ships with erpnext_mcp.")
	name = as_str(args, "governance_document", required=True)
	overwrite = bool(as_bool(args, "overwrite", False))
	dry_run = bool(as_bool(args, "dry_run", False))
	tail = "Nothing was written and no attachment was removed."

	row = frappe.db.get_value(
		GOVERNANCE_DOCUMENT,
		name,
		compat.existing_fields(
			GOVERNANCE_DOCUMENT, ("name", "title", "company", "category", "attached_file")
		),
		as_dict=True,
	)
	if not row:
		raise ToolError(
			f"no Governance Document named {name!r}. list_governance_documents has the names. {tail}"
		)
	if not frappe.has_permission(GOVERNANCE_DOCUMENT, "write", doc=name):
		raise ToolError(
			f"{frappe.session.user} is not permitted to write Governance Document {name}. "
			f"Attachment tools honour Frappe permissions — see docs/security.md. {tail}"
		)

	attachments = _governance_attachments(name)
	source = _source_docx(attachments, as_str(args, "source_docx_file"), name, tail)
	pdf_name = f"{_stem(source.get('file_name'))}.pdf"
	clash = next(
		(row for row in attachments if str(row.get("file_name") or "").lower() == pdf_name.lower()), None
	)
	existing_pdfs = [row for row in attachments if str(row.get("file_name") or "").lower().endswith(".pdf")]
	if existing_pdfs and not overwrite:
		raise ToolError(
			f"Governance Document {name} already has {len(existing_pdfs)} PDF attachment(s): "
			f"{', '.join(str(row.get('file_name')) for row in existing_pdfs)}. Regenerating would "
			"put a second copy of the same document in the archive and leave nobody able to say "
			"which is authoritative. Pass overwrite=true to replace "
			f"{pdf_name!r} — the .docx is kept either way. {tail}"
		)

	route = convert.available()
	if not route["ready"]:
		raise ToolError(f"this container cannot convert a .docx: {route['detail']} {tail}")

	plan = {
		"governance_document": name,
		"title": row.get("title"),
		"company": row.get("company"),
		"source_file": source["name"],
		"source_file_name": source.get("file_name"),
		"source_size": int(source.get("file_size") or 0),
		"pdf_file_name": pdf_name,
		"converter": route["via"],
		"converter_detail": route["detail"],
		"would_replace": [row["name"] for row in existing_pdfs] if overwrite else [],
		"attached_file_was": row.get("attached_file") or None,
		"dry_run": dry_run,
	}

	if dry_run:
		return ToolResult(
			{
				**plan,
				"regenerated": False,
				"note": (
					f"Nothing was written. {source.get('file_name')} would be converted with "
					f"{route['detail']} and attached as {pdf_name!r}"
					+ (
						f", replacing File(s) {', '.join(plan['would_replace'])}."
						if plan["would_replace"]
						else "."
					)
					+ " The .docx is kept either way."
				),
			},
			f"dry run: would convert {source.get('file_name')} on {name} to {pdf_name} via {route['via']}",
		)

	content = files.read_file_bytes(source["name"])
	try:
		pdf = convert.docx_to_pdf(content, str(source.get("file_name") or "document.docx"))
	except convert.ConversionError as exc:
		raise ToolError(f"{exc} {tail}") from None

	removed = []
	if overwrite and clash:
		frappe.delete_doc("File", clash["name"], force=True, ignore_permissions=True)
		removed.append({"file": clash["name"], "file_name": clash.get("file_name")})

	attachment = files.insert_attachment(
		pdf_name,
		pdf,
		is_private=True,
		doctype=GOVERNANCE_DOCUMENT,
		name=name,
		attached_to_field="attached_file",
		parent_authorized=True,  # the has_permission(write) above is this call's gate
	)
	frappe.db.set_value(GOVERNANCE_DOCUMENT, name, "attached_file", attachment.get("file_url"))

	digest = hashlib.sha256(pdf).hexdigest()
	data = {
		**plan,
		"regenerated": True,
		"file": attachment.name,
		"file_url": attachment.get("file_url"),
		"file_size": len(pdf),
		"size_human": files.human_size(len(pdf)),
		"sha256": digest,
		"is_private": True,
		"removed": removed,
		"attached_file_now": attachment.get("file_url"),
		"source_kept": True,
		"note": (
			f"The PDF is attached PRIVATE and `attached_file` now points at it, so a reader "
			f"following the archive lands on something that opens. {source.get('file_name')} is "
			"still attached — this adds a fixed copy, it does not replace the editable one."
			+ (
				f" File(s) {', '.join(entry['file'] for entry in removed)} were DELETED because "
				"overwrite=true asked for a replacement rather than a second copy."
				if removed
				else ""
			)
		),
		"next_step": (
			"get_governance_document_content reads it back. If several entries landed .docx-only, "
			"list_governance_documents shows which — this tool takes one at a time on purpose, so "
			"a conversion that goes wrong goes wrong once."
		),
	}
	return ToolResult(
		data,
		f"converted {source.get('file_name')} on {name} to {pdf_name} "
		f"({files.human_size(len(pdf))}, sha256 {digest[:12]}) via {route['via']}"
		+ (f", replacing {len(removed)} File(s)" if removed else ""),
		docstatus_delta="none → 0 (File created)",
	)


def _governance_attachments(name: str) -> list:
	return [
		dict(row)
		for row in frappe.db.get_all(
			"File",
			filters={"attached_to_doctype": GOVERNANCE_DOCUMENT, "attached_to_name": name},
			fields=compat.existing_fields(
				"File", ("name", "file_name", "file_url", "file_size", "is_private")
			),
			order_by="creation asc",
			limit=200,
		)
		or []
	]


def _stem(file_name) -> str:
	text = str(file_name or "document")
	return text[: text.rfind(".")] if "." in text else text


def _source_docx(attachments: list, requested: str, name: str, tail: str) -> dict:
	"""The .docx to convert: the one asked for, or the first one attached.

	"The FIRST .docx" is ordered by creation, which is what `_governance_attachments`
	asks the database for. An entry with two of them is a real thing — an original
	and an amendment filed together — and the answer there is to name the one you
	mean rather than to have this guess and be right half the time.
	"""
	docx = [row for row in attachments if str(row.get("file_name") or "").lower().endswith(".docx")]
	if requested:
		chosen = next(
			(row for row in attachments if requested in (row.get("name"), row.get("file_name"))),
			None,
		)
		if chosen is None:
			raise ToolError(
				f"Governance Document {name} has no attachment called {requested!r}. It has: "
				f"{', '.join(str(row.get('file_name')) for row in attachments) or '<none>'}. {tail}"
			)
		if not str(chosen.get("file_name") or "").lower().endswith(".docx"):
			raise ToolError(
				f"{chosen.get('file_name')!r} is not a .docx, so there is nothing to convert. This "
				"tool exists for archive entries that landed as an editing format and need a fixed "
				f"one beside them. {tail}"
			)
		return chosen
	if not docx:
		raise ToolError(
			f"Governance Document {name} has no .docx attachment. It has: "
			f"{', '.join(str(row.get('file_name')) for row in attachments) or '<nothing attached>'}. "
			f"There is nothing to convert. {tail}"
		)
	if len(docx) > 1:
		raise ToolError(
			f"Governance Document {name} has {len(docx)} .docx attachments: "
			f"{', '.join(str(row.get('file_name')) for row in docx)}. Name the one to convert in "
			f"`source_docx_file` — guessing between an original and an amendment and being right "
			f"half the time is worse than asking. {tail}"
		)
	return docx[0]
