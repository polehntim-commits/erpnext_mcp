# SPDX-License-Identifier: MIT
"""The axes a posting is filed under: cost centers, accounting dimensions, and
the company defaults that decide which account a document reaches for.

WHY THESE THREE LIVE TOGETHER. `accounts.py` owns *what* a posting hits — the
chart. This module owns *how it is classified once it gets there*. A cost center
says which part of the business the cost belongs to; an accounting dimension is
a second, third or fourth such axis the operator invents (a member, a crop
stage, a block); and a company default is which account a document picks when
nobody says. All three are structural configuration rather than transactions,
all three are read by every report, and none of them moves a balance on its own.

THE ACCOUNTING DIMENSION IS NOT A LIST OF VALUES. This is the thing to
understand before editing `create_accounting_dimension`. ERPNext's Accounting
Dimension record does not hold its own values: it points at a DocType, and every
record of that DocType is a value. So "add a Member dimension with three
members" is three separate ideas —

  1. a DocType to hold members (`create_accounting_dimension` can make a simple
     one on demand, behind `create_master_if_missing`),
  2. the Accounting Dimension record that points at it, plus the Link custom
     field it puts on each accounting doctype, and
  3. one record per member (`create_dimension_value`).

WHY THE CUSTOM FIELDS ARE WRITTEN HERE RATHER THAN LEFT TO ERPNEXT. Inserting an
Accounting Dimension makes ERPNext enqueue `make_dimension_in_accounting_doctypes`
as a *background job* over its own fixed hook list. Both halves of that are wrong
for an MCP caller: the caller's next call is usually a Journal Entry that needs
the field to exist *now*, and the caller asked for a specific set of doctypes.
So this module creates the Custom Fields synchronously, for the doctypes it was
given. ERPNext's background job still runs and still creates the rest of its
list; both paths check for an existing field first, so they do not collide.

JOURNAL ENTRY IS THE ODD ONE OUT. ERPNext carries accounting dimensions on the
Journal Entry *line* (`Journal Entry Account`), never on the header — a single
entry books to several cost centers, and the dimension has to move with the
line, not the voucher. A caller who says "Journal Entry" means that, so this
module redirects it and says so in the response rather than putting a field on a
header where nothing would ever read it.

DEFAULTS ARE TYPE-CHECKED, NOT JUST EXISTENCE-CHECKED. `set_company_defaults`
refuses a Receivable field pointed at a non-Receivable account. ERPNext will
accept that save and then produce party ledgers that do not reconcile, months
later, with nothing to point at. The check is cheap and the failure it prevents
is not.
"""

import frappe

from .. import charts, compat
from ..args import (
	as_bool,
	as_str,
	resolve_account,
	resolve_company,
	resolve_cost_center,
)
from ..errors import ToolError
from ..result import ToolResult

#: Fields this module reads off a Cost Center, filtered through `compat` before
#: any query: `cost_center_number` and `disabled` are both younger than the
#: doctype, and an operator on an old ERPNext should get a degraded answer
#: rather than a SQL error.
_COST_CENTER_FIELDS = (
	"name",
	"cost_center_name",
	"cost_center_number",
	"parent_cost_center",
	"is_group",
	"disabled",
	"company",
	"lft",
	"rgt",
)

#: Where an accounting dimension actually has to live for a given document.
#: Anything not named here takes the field on the document itself.
_DIMENSION_TARGETS = {
	# ERPNext puts dimensions on the line, not the voucher. See the module
	# docstring — a field on the Journal Entry header is read by nothing.
	"Journal Entry": ("Journal Entry Account",),
}

#: What `create_accounting_dimension` wires the dimension into when the caller
#: does not say. The four documents an accountant actually posts through.
DEFAULT_DIMENSION_DOCTYPES = (
	"Journal Entry",
	"Sales Invoice",
	"Purchase Invoice",
	"Payment Entry",
)

#: DocTypes an Accounting Dimension must never point at. ERPNext refuses most of
#: these itself; naming them here means the refusal says why instead of arriving
#: as a framework traceback halfway through creating custom fields.
_FORBIDDEN_DIMENSION_MASTERS = {
	"Accounting Dimension",
	"Company",
	"Account",
	"DocType",
	"DocField",
	"DocPerm",
	"User",
	"Role",
	"Custom Field",
	"Property Setter",
	"GL Entry",
	"Journal Entry",
	"Journal Entry Account",
}

#: Every Company default this tool will set, and what an account has to be for
#: that field to mean what its name says.
#:
#: `account_type` is a tuple of acceptable values with `""` meaning "no account
#: type set is also fine"; an empty tuple means the field does not constrain the
#: account type at all. `root_type` is never empty — a write-off account under
#: Assets is always a mistake, whatever it is called.
#:
#: The Receivable and Payable rows are the ones that matter most. ERPNext keys
#: party ledgers and every ageing report off `account_type`, so a
#: `default_receivable_account` pointed at a plain Asset account produces
#: invoices that post but never age, and the symptom shows up a quarter later.
_ACCOUNT_DEFAULTS = {
	"default_receivable_account": {
		"account_type": ("Receivable",),
		"root_type": ("Asset",),
		"what": "customer balances",
	},
	"default_payable_account": {
		"account_type": ("Payable",),
		"root_type": ("Liability",),
		"what": "supplier balances",
	},
	"default_cash_account": {
		"account_type": ("Cash",),
		"root_type": ("Asset",),
		"what": "cash payments",
	},
	"default_bank_account": {
		"account_type": ("Bank",),
		"root_type": ("Asset",),
		"what": "bank payments",
	},
	"default_income_account": {
		"account_type": ("Income Account", "Direct Income", "Indirect Income", ""),
		"root_type": ("Income",),
		"what": "sales lines with no account of their own",
	},
	"default_expense_account": {
		"account_type": (
			"Expense Account",
			"Direct Expense",
			"Indirect Expense",
			"Cost of Goods Sold",
			"",
		),
		"root_type": ("Expense",),
		"what": "purchase lines with no account of their own",
	},
	"cost_of_goods_sold_account": {
		"account_type": ("Cost of Goods Sold", "Expense Account", ""),
		"root_type": ("Expense",),
		"what": "stock consumed against a sale",
	},
	"round_off_account": {
		"account_type": ("Round Off", "Expense Account", ""),
		"root_type": ("Expense", "Income"),
		"what": "the cent a rounded total leaves behind",
	},
	"exchange_gain_loss_account": {
		"account_type": (),
		"root_type": ("Expense", "Income"),
		"what": "movement between a foreign-currency booking and its settlement",
	},
	"write_off_account": {
		"account_type": (),
		"root_type": ("Expense", "Income"),
		"what": "balances written off rather than collected",
	},
	"default_deferred_revenue_account": {
		"account_type": (),
		"root_type": ("Liability", "Income"),
		"what": "revenue billed but not yet earned",
	},
	"default_deferred_expense_account": {
		"account_type": (),
		"root_type": ("Asset", "Expense"),
		"what": "cost paid but not yet incurred",
	},
	# ── v0.8.0 ──────────────────────────────────────────────────────────────
	# The fields a Company needs before the Assets and Stock modules will save a
	# document at all. `disposal_account` is first because it is the one that
	# actually bit: ERPNext requires it on the Company before an Asset can be
	# scrapped or sold, and the refusal arrives from the Asset, not the Company,
	# which is not where anybody looks.
	"disposal_account": {
		"account_type": (),
		"root_type": ("Income", "Expense"),
		"what": "the gain or loss when an asset is sold or scrapped",
	},
	"capital_work_in_progress_account": {
		"account_type": ("Capital Work in Progress", ""),
		"root_type": ("Asset",),
		"what": "an asset being built, before it is in service",
	},
	"expenses_included_in_asset_valuation": {
		"account_type": (
			"Expenses Included In Asset Valuation",
			"Expenses Included In Valuation",
			"",
		),
		"root_type": ("Expense",),
		"what": "freight and duty that belong in an asset's cost rather than in the period",
	},
	"asset_received_but_not_billed": {
		"account_type": ("Asset Received But Not Billed", ""),
		"root_type": ("Liability",),
		"what": "an asset delivered before its invoice arrived",
	},
	"stock_adjustment_account": {
		"account_type": ("Stock Adjustment", "Expense Account", ""),
		"root_type": ("Expense",),
		"what": "the difference a stock count found",
	},
	# v0.200.0, AFB-2026-00025. Under perpetual inventory every warehouse without
	# an account of its own values its stock into this one, and a company with
	# neither cannot take a stock receipt at all.
	"default_inventory_account": {
		"account_type": ("Stock",),
		"root_type": ("Asset",),
		"what": "the value of stock held in a warehouse with no account of its own",
	},
	"stock_received_but_not_billed": {
		"account_type": ("Stock Received But Not Billed", ""),
		"root_type": ("Liability",),
		"what": "stock delivered before its invoice arrived",
	},
	"unrealized_exchange_gain_loss_account": {
		"account_type": (),
		"root_type": ("Income", "Expense"),
		"what": "the movement on a foreign-currency balance nobody has settled yet",
	},
	"unrealized_profit_loss_account": {
		"account_type": (),
		"root_type": ("Income", "Expense"),
		"what": "profit on a transfer between companies in the same group, eliminated on consolidation",
	},
	"default_advance_received_account": {
		# ERPNext's own filter on this field, and the reason it looks wrong: an
		# advance received is money held for a customer, so the account is a
		# LIABILITY that ERPNext keys as Receivable so the party ledger picks it up.
		"account_type": ("Receivable",),
		"root_type": ("Liability",),
		"what": "money taken before the work was done",
	},
	"default_advance_paid_account": {
		"account_type": ("Payable",),
		"root_type": ("Asset",),
		"what": "money paid before the goods arrived",
	},
	"default_operating_cost_account": {
		"account_type": (),
		"root_type": ("Expense",),
		"what": "the operating cost a production order adds to what it makes",
	},
}

#: Company defaults that are a Cost Center rather than an Account.
_COST_CENTER_DEFAULTS = {
	"round_off_cost_center": "where the rounding difference is filed",
	"default_selling_cost_center": "where a sale is filed when the document does not say",
	"default_buying_cost_center": "where a purchase is filed when the document does not say",
}

#: Just the ones that take an Account. Public because `accounts.delete_account`
#: needs to know which Company fields could be pointing at an account it is about
#: to remove, and a site whose Company meta does not describe its Link options is
#: one where asking the meta answers nothing.
ACCOUNT_COMPANY_DEFAULTS = tuple(_ACCOUNT_DEFAULTS)

SUPPORTED_COMPANY_DEFAULTS = ACCOUNT_COMPANY_DEFAULTS + tuple(_COST_CENTER_DEFAULTS)


# ── shared lookups ──────────────────────────────────────────────────────────
def _company_abbr(company: str) -> str:
	return frappe.db.get_value("Company", company, "abbr") or ""


def cost_center_docname(cost_center_number: str, cost_center_name: str, abbr: str) -> str:
	"""ERPNext's Cost Center autoname, reproduced.

	`"<number> - <name> - <abbr>"`, or `"<name> - <abbr>"` when unnumbered —
	character for character the rule Account uses, because ERPNext builds both
	through the same `get_autoname_with_number`.

	Not imported from ERPNext, deliberately. That helper's signature changed
	between majors (it took a fourth `name` argument in v12 and lost it later),
	so importing it would trade a stable local rule for a call that raises
	TypeError on somebody's install. The rule itself has not moved, and the real
	insert still names itself — this is only used to *predict* a docname, which
	is what a uniqueness check and a rename need.
	"""
	return charts.account_docname(cost_center_number, cost_center_name, abbr)


def _cost_center(name: str, company: str = "") -> dict:
	"""One Cost Center as a dict, resolved from a docname, number or name."""
	docname = resolve_cost_center(name, company)
	row = frappe.db.get_value(
		"Cost Center",
		docname,
		compat.existing_fields("Cost Center", _COST_CENTER_FIELDS),
		as_dict=True,
	)
	if not row:  # pragma: no cover - resolve_cost_center already proved it exists
		raise ToolError(f"no Cost Center named {docname!r}")
	return dict(row)


def _describe_cost_center(row: dict) -> dict:
	"""The cost center shape every tool in this module returns."""
	return {
		"name": row.get("name"),
		"cost_center_number": row.get("cost_center_number") or "",
		"cost_center_name": row.get("cost_center_name"),
		"parent_cost_center": row.get("parent_cost_center") or "",
		"is_group": bool(int(row.get("is_group") or 0)),
		"disabled": bool(int(row.get("disabled") or 0)),
		"company": row.get("company"),
	}


def _cost_center_number_holder(company: str, number: str, exclude: str = "") -> str:
	"""Which cost center already holds this number in this company, if any."""
	number = str(number or "").strip()
	if not number or not compat.has_field("Cost Center", "cost_center_number"):
		return ""
	for name in frappe.db.get_all(
		"Cost Center",
		filters={"company": company, "cost_center_number": number},
		pluck="name",
		limit=5,
	):
		if name != exclude:
			return name
	return ""


def _root_cost_center(company: str) -> str:
	"""The company's root Cost Center docname, or "" if it has none yet."""
	rows = frappe.db.get_all(
		"Cost Center",
		filters={"company": company, "parent_cost_center": ("in", ["", None])},
		pluck="name",
		limit=1,
	)
	return rows[0] if rows else ""


def _cost_center_gl_count(cost_center: str) -> int:
	if not compat.has_field("GL Entry", "cost_center"):
		return 0
	filters = {"cost_center": cost_center}
	if compat.has_field("GL Entry", "is_cancelled"):
		filters["is_cancelled"] = 0
	return frappe.db.count("GL Entry", filters)


# ── 44. create_cost_center ──────────────────────────────────────────────────
def create_cost_center(args: dict) -> ToolResult:
	"""Create one Cost Center under an existing group."""
	compat.require_doctype("Cost Center", "It ships with ERPNext's Accounts module.")
	company = resolve_company(as_str(args, "company"), required=True)
	cost_center_name = as_str(args, "cost_center_name", required=True)
	cost_center_number = as_str(args, "cost_center_number")
	parent_input = as_str(args, "parent_cost_center")
	is_group = bool(as_bool(args, "is_group", False))

	parent = _validated_cost_center_parent(parent_input, company, cost_center_name)

	if cost_center_number:
		holder = _cost_center_number_holder(company, cost_center_number)
		if holder:
			raise ToolError(
				f"cost_center_number {cost_center_number!r} is already used by {holder!r} in "
				f"{company}. Cost center numbers are unique per company; pick another, or "
				"use update_cost_center to renumber the existing one. Nothing was created."
			)

	abbr = _company_abbr(company)
	predicted = cost_center_docname(cost_center_number, cost_center_name, abbr)
	if frappe.db.exists("Cost Center", predicted):
		raise ToolError(
			f"a Cost Center named {predicted!r} already exists. ERPNext names cost "
			"centers '<number> - <name> - <abbr>', so this number-and-name pair is "
			"taken. Nothing was created."
		)

	doc = frappe.new_doc("Cost Center")
	doc.company = company
	doc.cost_center_name = cost_center_name
	doc.parent_cost_center = parent or None
	doc.is_group = 1 if is_group else 0
	if cost_center_number and compat.has_field("Cost Center", "cost_center_number"):
		doc.cost_center_number = cost_center_number
	doc.insert()

	row = _cost_center(doc.name, company)
	data = {
		**_describe_cost_center(row),
		"next_step": (
			"A group cost center cannot be posted to; add leaf cost centers under it."
			if is_group
			else "Leaf cost center, ready to be filed against on a posting."
		),
	}
	kind = "group" if is_group else "leaf"
	where = f"under {parent}" if parent else f"as the root cost center for {company}"
	return ToolResult(
		data,
		f"created {kind} Cost Center {doc.name} {where}",
		docstatus_delta="none → 0 (created)",
	)


def _validated_cost_center_parent(parent_input: str, company: str, cost_center_name: str) -> str:
	"""Resolve the parent, or vet an attempt to create a root. Returns a docname or "".

	ERPNext's own rule, from `CostCenter.validate_mandatory`, is that a cost
	center with no parent must be named exactly after its company — the root of
	the tree *is* the company. Every company gets one when its chart of accounts
	is built, so in practice a rootless create is a caller who forgot the parent,
	and saying that is more useful than letting ERPNext throw "Please enter parent
	cost center" from three frames down.
	"""
	if parent_input:
		parent = _cost_center(parent_input, company)
		if parent["company"] != company:
			raise ToolError(
				f"parent_cost_center {parent['name']!r} belongs to company "
				f"{parent['company']!r}, not {company!r}. A cost center cannot be "
				"parented across companies."
			)
		if not int(parent.get("is_group") or 0):
			raise ToolError(
				f"parent_cost_center {parent['name']!r} is a leaf cost center, not a "
				"group. In ERPNext only a group can have children — and a cost center "
				"that has children can no longer be posted to, which is why this is not "
				"done automatically."
			)
		return parent["name"]

	existing_root = _root_cost_center(company)
	if existing_root:
		raise ToolError(
			f"parent_cost_center is required: {company} already has a root cost center "
			f"({existing_root!r}), and ERPNext allows exactly one. Pass a group cost "
			"center to hang this under — list_cost_centers shows the tree. Nothing was "
			"created."
		)
	if cost_center_name != company:
		raise ToolError(
			f"a root cost center must be named exactly after its company, so "
			f"cost_center_name would have to be {company!r}, not {cost_center_name!r} "
			"(ERPNext's own rule, in CostCenter.validate_mandatory). Nothing was created."
		)
	return ""


# ── 45. update_cost_center ──────────────────────────────────────────────────
def update_cost_center(args: dict) -> ToolResult:
	"""Rename, renumber or disable/enable one Cost Center in place.

	Never changes the parent, for the same reason `update_account` does not:
	reparenting moves no posting but changes what every subtotal built on the
	tree has meant, retroactively. That would be `move_cost_center`, which this
	app does not ship.
	"""
	compat.require_doctype("Cost Center", "It ships with ERPNext's Accounts module.")
	name = as_str(args, "name", required=True)
	company = as_str(args, "company")
	row = _cost_center(name, company)
	company = row["company"]

	new_name = as_str(args, "new_cost_center_name")
	new_number = as_str(args, "new_cost_center_number")
	disabled = as_bool(args, "disabled")

	# Checked before "nothing to change", so a caller who passed only a parent
	# is told the thing they need to hear rather than being asked for a field.
	if "parent_cost_center" in args or "new_parent_cost_center" in args:
		raise ToolError(
			"update_cost_center cannot reparent a cost center. This app ships no tool "
			"that does — reparenting changes what every subtotal built on the tree has "
			"meant, for periods already reported, and is a Desk decision."
		)
	if not new_name and not new_number and disabled is None:
		raise ToolError(
			"nothing to change. Pass at least one of new_cost_center_name, "
			"new_cost_center_number or disabled."
		)

	is_root = not str(row.get("parent_cost_center") or "").strip()
	if is_root and new_name:
		raise ToolError(
			f"{row['name']} is the root cost center for {company}, and ERPNext requires "
			f"a root to be named exactly after its company ({company!r}). Renaming it "
			"would make every later save of it fail. Nothing was changed."
		)

	changes = {}

	# -- disabled: an ordinary save, so the doctype validates it ---------------
	if disabled is not None:
		if not compat.has_field("Cost Center", "disabled"):
			raise ToolError(
				"this site's Cost Center doctype has no `disabled` field, so it cannot "
				"be disabled or re-enabled. Nothing was changed."
			)
		if bool(int(row.get("disabled") or 0)) != disabled:
			doc = frappe.get_doc("Cost Center", row["name"])
			doc.disabled = 1 if disabled else 0
			doc.save()
			changes["disabled"] = [bool(int(row.get("disabled") or 0)), disabled]

	# -- name and number: the docname has to move with them -------------------
	final_name = row["name"]
	if new_name or new_number:
		current_name = row["cost_center_name"]
		current_number = row.get("cost_center_number") or ""
		target_name = new_name or current_name
		target_number = new_number if new_number else current_number
		# "The value it already has" is not a change, and must not become one: a
		# rename that moved nothing would still write two fields and report a
		# success, which is how a caller convinces itself something happened.
		name_moves = target_name != current_name
		number_moves = target_number != current_number

		if number_moves:
			holder = _cost_center_number_holder(company, target_number, exclude=row["name"])
			if holder:
				raise ToolError(
					f"cost_center_number {target_number!r} is already used by {holder!r} in "
					f"{company}. Nothing was renamed."
				)
		if name_moves or number_moves:
			predicted = cost_center_docname(target_number, target_name, _company_abbr(company))
			if predicted != row["name"] and frappe.db.exists("Cost Center", predicted):
				raise ToolError(f"a Cost Center named {predicted!r} already exists. Nothing was renamed.")

			final_name = _rename_cost_center(row["name"], target_name, target_number, predicted)
			if name_moves:
				changes["cost_center_name"] = [current_name, target_name]
			if number_moves:
				changes["cost_center_number"] = [current_number, target_number]

	if not changes:
		raise ToolError(
			f"{row['name']} already has those values; nothing to change. Pass a value "
			"that differs from the current one."
		)

	after = _cost_center(final_name, company)
	entries = _cost_center_gl_count(final_name)
	data = {
		**_describe_cost_center(after),
		"previous_name": row["name"],
		"renamed": final_name != row["name"],
		"changes": changes,
		"gl_entries_on_this_cost_center": entries,
		"note": (
			"The docname changed, so links, reports and GL Entry references were "
			"updated by ERPNext's rename. Any docname held outside ERPNext is stale."
			if final_name != row["name"]
			else "The docname is unchanged."
		),
	}
	if changes.get("disabled") == [False, True]:
		children = frappe.db.count("Cost Center", {"parent_cost_center": final_name})
		data["warning"] = (
			f"Nothing was deleted: the cost center and its {entries} GL Entry row(s) "
			"remain and still appear in reports covering them. It drops out of pickers, "
			"so a correction that needs to file against it has to re-enable it first."
			+ (
				f" It is a group with {children} child cost center(s), which are NOT "
				"disabled by this call and remain postable."
				if children
				else ""
			)
		)
	summary = ", ".join(
		f"{field} {before!r} → {after_value!r}" for field, (before, after_value) in changes.items()
	)
	return ToolResult(
		data,
		f"updated Cost Center {row['name']}: {summary}"
		+ (f" (now {final_name})" if final_name != row["name"] else ""),
		docstatus_delta="",
	)


def _rename_cost_center(name: str, cost_center_name: str, cost_center_number: str, predicted: str) -> str:
	"""Change a Cost Center's name and/or number, keeping the docname in step.

	Two writes, fields first and then the document, for exactly the reason spelled
	out at the top of `tools/accounts.py`: a Cost Center's docname encodes two of
	its own fields and is built once by `autoname`, so setting the fields alone
	leaves the tree showing one thing and reporting another, and renaming alone
	leaves the fields holding the old text forever.

	Unlike Account this is hand-rolled rather than delegated. ERPNext's own
	helper (`accounts.utils.update_number_field`) only handles the *number*, and
	the Account helper's compensating behaviour — syncing the change down into
	child companies — has no cost-center equivalent to reproduce. So there is
	nothing here that ERPNext would do differently.
	"""
	frappe.db.set_value("Cost Center", name, "cost_center_name", cost_center_name)
	if compat.has_field("Cost Center", "cost_center_number"):
		frappe.db.set_value("Cost Center", name, "cost_center_number", cost_center_number or None)
	if predicted != name:
		frappe.rename_doc("Cost Center", name, predicted, force=True)
		return predicted
	return name


# ── 46. list_cost_centers ───────────────────────────────────────────────────
def list_cost_centers(args: dict) -> ToolResult:
	"""One company's cost centers as a nested tree.

	Built in one query and assembled in Python, the same way
	`get_chart_of_accounts` is and for the same reason: walking the parent link
	with a query per node is the obvious implementation and is also how you make
	a large tree take seconds.
	"""
	compat.require_doctype("Cost Center", "It ships with ERPNext's Accounts module.")
	company = resolve_company(as_str(args, "company"), required=True)
	include_disabled = bool(as_bool(args, "include_disabled", False))

	filters = {"company": company}
	if not include_disabled and compat.has_field("Cost Center", "disabled"):
		filters["disabled"] = 0

	fields = compat.existing_fields("Cost Center", _COST_CENTER_FIELDS)
	# `lft` is the nested-set left bound, so ordering by it yields parents before
	# children and siblings in tree order. A site without it falls back to name
	# order, which still assembles correctly: the node map is built before
	# anything is linked.
	order_by = "lft asc" if compat.has_field("Cost Center", "lft") else "name asc"
	rows = frappe.db.get_all("Cost Center", filters=filters, fields=fields, order_by=order_by)

	nodes = {row["name"]: {**row, "children": []} for row in rows}
	roots = []
	for row in rows:
		parent = row.get("parent_cost_center")
		if parent and parent in nodes:
			nodes[parent]["children"].append(nodes[row["name"]])
		else:
			# A real root, or a node whose parent was filtered out by
			# include_disabled — both belong at the top of *this* response.
			roots.append(nodes[row["name"]])

	hidden = 0
	if not include_disabled and compat.has_field("Cost Center", "disabled"):
		hidden = frappe.db.count("Cost Center", {"company": company, "disabled": 1})

	data = {
		"company": company,
		"cost_centers": roots,
		"flat_count": len(rows),
		"disabled_count_excluded": hidden,
		"include_disabled": include_disabled,
		"default_cost_center": compat.company_default_cost_center(company),
		"note": (
			"children[] is nested; flat_count is every cost center in the response. "
			"Only leaf cost centers can be posted to."
			+ (
				f" {hidden} disabled cost center(s) were left out — pass include_disabled=true to see them."
				if hidden
				else ""
			)
		),
	}
	return ToolResult(data, f"{len(rows)} cost center(s) for {company}")


# ── 47. create_accounting_dimension ─────────────────────────────────────────
def create_accounting_dimension(args: dict) -> ToolResult:
	"""Create an Accounting Dimension and wire it into the documents that use it.

	Three writes, in this order, any of which failing rolls back all of them:
	the master DocType (only when asked for and only when missing), the
	Accounting Dimension record, and one Link Custom Field per target doctype.
	"""
	compat.require_doctype(
		"Accounting Dimension",
		"It ships with ERPNext's Accounts module (v12 and later).",
	)
	label = as_str(args, "dimension_name", required=True)
	master_doctype = as_str(args, "master_doctype") or label
	create_master = bool(as_bool(args, "create_master_if_missing", False))
	disabled = bool(as_bool(args, "disabled", False))
	requested = _requested_doctypes(args.get("document_types"))

	fieldname = frappe.scrub(label)
	if not fieldname:
		raise ToolError(f"dimension_name {label!r} does not scrub to a usable fieldname.")

	_refuse_duplicate_dimension(label, master_doctype)

	master_created = False
	if not compat.doctype_exists(master_doctype):
		if not create_master:
			raise ToolError(
				f"no DocType named {master_doctype!r} on this site, and an Accounting "
				"Dimension is a pointer at a DocType rather than a list of values — "
				"every record of that DocType becomes a value of the dimension. Pass "
				"create_master_if_missing=true to have a simple one created (a value "
				"field, a description and a disabled flag), or name an existing DocType "
				"with master_doctype. Nothing was created."
			)
		_validate_new_master_name(master_doctype)
	else:
		_validate_existing_master(master_doctype)

	# Every target has to be checkable before anything is written: a half-wired
	# dimension, with the record created and the field missing on one doctype, is
	# worse than none at all because it looks configured.
	targets = _resolve_dimension_targets(requested)
	for doctype in targets:
		_validate_dimension_field_slot(doctype, fieldname, master_doctype, label)

	if not compat.doctype_exists(master_doctype):
		_create_master_doctype(master_doctype, label)
		master_created = True

	doc = frappe.new_doc("Accounting Dimension")
	doc.document_type = master_doctype
	if compat.has_field("Accounting Dimension", "label"):
		doc.label = label
	if compat.has_field("Accounting Dimension", "fieldname"):
		doc.fieldname = fieldname
	if compat.has_field("Accounting Dimension", "disabled"):
		doc.disabled = 1 if disabled else 0
	doc.insert()

	created, existing = [], []
	for doctype in targets:
		if compat.has_field(doctype, fieldname):
			existing.append(doctype)
			continue
		_create_dimension_custom_field(doctype, fieldname, label, master_doctype)
		created.append(doctype)

	data = {
		"name": doc.name,
		"label": label,
		"fieldname": fieldname,
		"master_doctype": master_doctype,
		"master_doctype_created": master_created,
		"disabled": disabled,
		"document_types_requested": list(requested),
		"document_types_applied": list(targets),
		"custom_fields_created": created,
		"custom_fields_already_present": existing,
		"redirected": {
			asked: list(_DIMENSION_TARGETS[asked]) for asked in requested if asked in _DIMENSION_TARGETS
		},
		"next_step": (
			f"Add values with create_dimension_value(dimension_name={label!r}, "
			f"value_name=…) — each one is a {master_doctype} record. Then set "
			f"{fieldname!r} in a journal entry line's `dimensions` object."
			if master_created
			else (
				f"Existing {master_doctype} records are already values of this "
				f"dimension. Set {fieldname!r} in a journal entry line's `dimensions` "
				"object to file a posting under one."
			)
		),
		"note": (
			"ERPNext will separately, in a background job, add the same field to every "
			"doctype in its own accounting_dimension_doctypes list. That is additive: "
			"it checks for an existing field first, so it will not disturb these."
		),
	}
	return ToolResult(
		data,
		f"created Accounting Dimension {doc.name} ({label} → {master_doctype}) "
		f"with {len(created)} custom field(s) on {', '.join(targets)}",
		docstatus_delta="none → 0 (created)",
	)


def _requested_doctypes(raw) -> tuple:
	"""The `document_types` argument, defaulted and checked."""
	if raw in (None, ""):
		return DEFAULT_DIMENSION_DOCTYPES
	if isinstance(raw, str):
		raw = [raw]
	if not isinstance(raw, list) or not raw:
		raise ToolError(
			"document_types must be a non-empty list of DocType names, e.g. "
			'["Journal Entry", "Sales Invoice"]. Omit it for the default set: '
			f"{', '.join(DEFAULT_DIMENSION_DOCTYPES)}."
		)
	out = []
	for entry in raw:
		doctype = str(entry or "").strip()
		if not doctype:
			raise ToolError("document_types contains an empty entry")
		if not compat.doctype_exists(doctype):
			raise ToolError(f"no DocType named {doctype!r} on this site. Nothing was created.")
		if doctype not in out:
			out.append(doctype)
	return tuple(out)


def _resolve_dimension_targets(requested) -> tuple:
	"""Turn what the caller asked for into where the field actually has to go."""
	out = []
	for doctype in requested:
		for target in _DIMENSION_TARGETS.get(doctype, (doctype,)):
			if not compat.doctype_exists(target):
				raise ToolError(
					f"{doctype!r} carries accounting dimensions on {target!r}, which this "
					"site does not have. Nothing was created."
				)
			if target not in out:
				out.append(target)
	return tuple(out)


def _refuse_duplicate_dimension(label: str, master_doctype: str) -> None:
	"""ERPNext allows one Accounting Dimension per DocType, and one per label."""
	fields = compat.existing_fields("Accounting Dimension", ("name", "label", "document_type"))
	for row in frappe.db.get_all("Accounting Dimension", fields=fields, limit=500):
		if str(row.get("document_type") or "") == master_doctype:
			raise ToolError(
				f"an Accounting Dimension ({row['name']!r}) already points at "
				f"{master_doctype!r}. ERPNext allows one dimension per DocType — its "
				"values are that DocType's records, so a second one would be the same "
				"dimension twice. Add values with create_dimension_value instead. "
				"Nothing was created."
			)
		if str(row.get("label") or "") == label:
			raise ToolError(
				f"an Accounting Dimension labelled {label!r} already exists "
				f"({row['name']!r}, pointing at {row.get('document_type')!r}). "
				"Nothing was created."
			)


def _validate_existing_master(master_doctype: str) -> None:
	"""Vet a DocType the caller named as the dimension's value list."""
	if master_doctype in _FORBIDDEN_DIMENSION_MASTERS:
		raise ToolError(
			f"{master_doctype!r} cannot back an accounting dimension: it is a core or "
			"accounting DocType, and ERPNext refuses those. Name a master that holds "
			"the values themselves. Nothing was created."
		)
	row = frappe.db.get_value("DocType", master_doctype, ["issingle", "istable"], as_dict=True) or {}
	if int(row.get("issingle") or 0):
		raise ToolError(
			f"{master_doctype!r} is a Single DocType — it has exactly one record, so it "
			"cannot be a list of dimension values. Nothing was created."
		)
	if int(row.get("istable") or 0):
		raise ToolError(
			f"{master_doctype!r} is a child table. Its rows only exist inside a parent "
			"document, so they cannot be linked to as dimension values. Nothing was "
			"created."
		)


def _validate_new_master_name(master_doctype: str) -> None:
	if master_doctype in _FORBIDDEN_DIMENSION_MASTERS:
		raise ToolError(f"{master_doctype!r} is a reserved DocType name. Nothing was created.")
	if len(master_doctype) > 61:
		# Frappe builds `tab<DocType>` and MariaDB caps a table name at 64.
		raise ToolError(
			f"master_doctype {master_doctype!r} is too long: Frappe creates a table "
			"named 'tab<DocType>' and MariaDB caps table names at 64 characters. "
			"Nothing was created."
		)


def _validate_dimension_field_slot(doctype: str, fieldname: str, master_doctype: str, label: str) -> None:
	"""Refuse now if the field this dimension needs is already taken by something else."""
	field = compat.field_meta(doctype, fieldname)
	if field is None:
		return
	fieldtype = str(field.get("fieldtype") or "")
	options = str(field.get("options") or "")
	if fieldtype == "Link" and options == master_doctype:
		return
	raise ToolError(
		f"{doctype} already has a field named {fieldname!r} (a {fieldtype or 'field'}"
		+ (f" to {options!r}" if options else "")
		+ f"), and the dimension {label!r} needs it to be a Link to {master_doctype!r}. "
		"Rename the dimension, or point it at that DocType with master_doctype. "
		"Nothing was created."
	)


def _dimension_master_module() -> str:
	"""Which module a generated master DocType belongs to.

	`Accounts` where ERPNext is installed, so the DocType turns up beside the
	accounting doctypes it exists to classify; `Custom` — Frappe's own catch-all
	module — otherwise.
	"""
	for module in ("Accounts", "Custom"):
		if frappe.db.exists("Module Def", module):
			return module
	raise ToolError(
		"this site has neither an 'Accounts' nor a 'Custom' Module Def, so there is "
		"nowhere to put a generated DocType. Create the master DocType yourself and "
		"pass it as master_doctype."
	)


def _create_master_doctype(master_doctype: str, label: str) -> None:
	"""A minimal, custom DocType whose records are the dimension's values.

	`custom: 1` matters: a custom DocType lives entirely in the database, so this
	writes no files into an app and needs no developer mode, and an operator can
	delete it from the Desk. Named by its own value field, so the docname *is*
	the value — which is what makes `Member-01` read as `Member-01` everywhere it
	is linked, instead of as `MEM-00001`.
	"""
	payload = {
		"doctype": "DocType",
		"name": master_doctype,
		"module": _dimension_master_module(),
		"custom": 1,
		"autoname": "field:dimension_value",
		"track_changes": 1,
		"fields": [
			{
				"fieldname": "dimension_value",
				"label": label,
				"fieldtype": "Data",
				"reqd": 1,
				"in_list_view": 1,
			},
			{
				"fieldname": "description",
				"label": "Description",
				"fieldtype": "Small Text",
			},
			{
				"fieldname": "disabled",
				"label": "Disabled",
				"fieldtype": "Check",
				"default": "0",
			},
		],
		"permissions": [
			{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
			{"role": "Accounts Manager", "read": 1, "write": 1, "create": 1},
		],
	}
	if compat.has_field("DocType", "naming_rule"):
		# v14 added an explicit naming_rule alongside autoname; older Frappe
		# derives it, and setting a field that version has never heard of throws.
		payload["naming_rule"] = "By fieldname"
	frappe.get_doc(payload).insert()


def _create_dimension_custom_field(doctype: str, fieldname: str, label: str, master_doctype: str) -> None:
	frappe.get_doc(
		{
			"doctype": "Custom Field",
			"dt": doctype,
			"fieldname": fieldname,
			"label": label,
			"fieldtype": "Link",
			"options": master_doctype,
		}
	).insert()
	try:
		frappe.clear_cache(doctype=doctype)
	except Exception:
		# The field is written and will be visible on the next request whatever
		# happens here; a cache that did not clear is not a reason to roll the
		# whole dimension back.
		frappe.log_error(
			title="erpnext_mcp: could not clear meta cache after adding a dimension field",
			message=compat.traceback_text(),
		)


# ── 48. create_dimension_value ──────────────────────────────────────────────
def create_dimension_value(args: dict) -> ToolResult:
	"""Create one record in the DocType an Accounting Dimension points at."""
	compat.require_doctype(
		"Accounting Dimension",
		"It ships with ERPNext's Accounts module (v12 and later).",
	)
	dimension_name = as_str(args, "dimension_name", required=True)
	value_name = as_str(args, "value_name", required=True)
	extra_fields = args.get("extra_fields")
	if extra_fields in (None, ""):
		extra_fields = {}
	if not isinstance(extra_fields, dict):
		raise ToolError(
			f"extra_fields must be an object of fieldname → value, got {type(extra_fields).__name__}"
		)

	dimension = _accounting_dimension(dimension_name)
	master = str(dimension.get("document_type") or "")
	if not master or not compat.doctype_exists(master):
		raise ToolError(
			f"Accounting Dimension {dimension['name']!r} points at DocType {master!r}, "
			"which this site does not have. Nothing was created."
		)

	if frappe.db.exists(master, value_name):
		raise ToolError(
			f"a {master} named {value_name!r} already exists, so it is already a value "
			f"of the {dimension.get('label') or dimension['name']} dimension. Nothing "
			"was created."
		)

	for key in sorted(extra_fields):
		if not compat.has_field(master, key):
			raise ToolError(
				f"extra_fields has no field {key!r} on {master}. Fields are set on the "
				"master DocType verbatim, so an unknown one is a typo rather than "
				"something to ignore. Nothing was created."
			)

	payload = {"doctype": master, **{k: v for k, v in extra_fields.items()}}
	naming_field = _naming_field(master)
	if naming_field:
		payload[naming_field] = value_name
	else:
		payload["name"] = value_name

	doc = frappe.get_doc(payload)
	doc.insert()

	renamed = doc.name != value_name
	data = {
		"name": doc.name,
		"requested_name": value_name,
		"dimension": dimension.get("label") or dimension["name"],
		"dimension_record": dimension["name"],
		"master_doctype": master,
		"named_by": f"field:{naming_field}" if naming_field else "the name given",
		"extra_fields": {key: doc.get(key) for key in sorted(extra_fields)},
		"note": (
			f"{master} names itself rather than taking the name given, so this value is "
			f"{doc.name!r}, not {value_name!r}. Use the docname when setting the "
			"dimension on a posting."
			if renamed
			else "Ready to use: set it on a journal entry line's `dimensions` object."
		),
	}
	return ToolResult(
		data,
		f"created {master} {doc.name} as a value of the "
		f"{dimension.get('label') or dimension['name']} dimension",
		docstatus_delta="none → 0 (created)",
	)


def _accounting_dimension(dimension_name: str) -> dict:
	"""One Accounting Dimension, resolved from its label, its DocType or its docname.

	Three ways because the record's own docname is a version detail — ERPNext has
	named it from the label and from the document type at different points — and a
	caller who created the dimension through this app knows it by the label it
	asked for.
	"""
	fields = compat.existing_fields("Accounting Dimension", ("name", "label", "document_type", "fieldname"))
	rows = [dict(row) for row in frappe.db.get_all("Accounting Dimension", fields=fields, limit=500)]
	for key in ("label", "name", "document_type"):
		matches = [row for row in rows if str(row.get(key) or "") == dimension_name]
		if len(matches) == 1:
			return matches[0]
		if len(matches) > 1:  # pragma: no cover - creation refuses duplicates
			raise ToolError(
				f"{dimension_name!r} matches {len(matches)} accounting dimensions by "
				f"{key}: {', '.join(sorted(row['name'] for row in matches))}"
			)
	known = ", ".join(sorted(str(row.get("label") or row["name"]) for row in rows)) or "<none>"
	raise ToolError(
		f"no Accounting Dimension called {dimension_name!r} on this site. Known "
		f"dimensions: {known}. Create it with create_accounting_dimension."
	)


def _naming_field(doctype: str) -> str:
	"""The field a doctype names itself from, or "" when it names itself some other way.

	`field:<fieldname>` is the naming rule this app's own generated masters use
	and the one ERPNext's dimension masters (Department, Branch, Project) use, so
	it covers the realistic cases. Anything else — a naming series, a prompt, a
	hash — falls back to passing `name` and reporting what the document actually
	got called, because a value whose docname is `MEM-00001` still works as a
	dimension; it just is not what the caller typed.
	"""
	try:
		autoname = str(getattr(frappe.get_meta(doctype), "autoname", "") or "")
	except Exception:
		return ""
	if not autoname.startswith("field:"):
		return ""
	fieldname = autoname.split(":", 1)[1].strip()
	return fieldname if compat.has_field(doctype, fieldname) else ""


# ── 49. set_company_defaults ────────────────────────────────────────────────
def set_company_defaults(args: dict) -> ToolResult:
	"""Point a Company's default account and cost center fields at real accounts.

	Idempotent by construction: every field is compared before it is written, so
	a re-run of the same call changes nothing and says so. That matters more than
	usual here because Company.save is not a cheap write — ERPNext's `on_update`
	walks the company tree and can build default records — and because these are
	the fields a caller is most likely to set twice while working out a chart.
	"""
	company = resolve_company(as_str(args, "company"), required=True)
	defaults = args.get("defaults")
	if not isinstance(defaults, dict) or not defaults:
		raise ToolError(
			"defaults must be a non-empty object of company field → value, e.g. "
			'{"default_receivable_account": "1200", "round_off_account": "6900"}. '
			f"Supported keys: {', '.join(SUPPORTED_COMPANY_DEFAULTS)}."
		)

	unknown = sorted(set(defaults) - set(SUPPORTED_COMPANY_DEFAULTS))
	if unknown:
		raise ToolError(
			f"unsupported default(s): {', '.join(unknown)}. Supported keys: "
			f"{', '.join(SUPPORTED_COMPANY_DEFAULTS)}. Nothing was changed."
		)

	# Before reading anything: a field this ERPNext does not have cannot be
	# selected either, so the check has to come ahead of the query rather than
	# inside the loop that follows it.
	for field in sorted(defaults):
		if not compat.has_field("Company", field):
			raise ToolError(
				f"this site's Company doctype has no field {field!r} — it belongs to a "
				"different ERPNext version. Nothing was changed."
			)

	current = frappe.db.get_value("Company", company, sorted(defaults), as_dict=True) or {}

	# Resolve and vet everything before writing anything: half a set of defaults
	# is a company that books some documents correctly and some silently wrong.
	resolved, changes, unchanged = {}, {}, []
	for field in SUPPORTED_COMPANY_DEFAULTS:
		if field not in defaults:
			continue
		wanted = _resolve_company_default(field, defaults[field], company)
		resolved[field] = wanted
		before = str(current.get(field) or "")
		if before == wanted:
			unchanged.append(field)
		else:
			changes[field] = [before, wanted]

	if changes:
		doc = frappe.get_doc("Company", company)
		for field, (_before, after) in changes.items():
			doc.set(field, after or None)
		doc.save()

	data = {
		"company": company,
		"changed": changes,
		"unchanged": unchanged,
		"defaults_now": {field: (resolved.get(field) or None) for field in resolved},
		"idempotent": True,
		"note": (
			"Company defaults decide which account a document reaches for when nothing "
			"on the document says. They do not touch a single existing posting — every "
			"document already written keeps the accounts it was written with."
		),
	}
	if not changes:
		return ToolResult(
			data,
			f"{company} already has those {len(unchanged)} default(s); nothing was changed",
			docstatus_delta="",
		)
	summary = ", ".join(f"{field}={after!r}" for field, (_before, after) in changes.items())
	return ToolResult(
		data,
		f"set {len(changes)} default(s) on {company}: {summary}",
		docstatus_delta="",
	)


def _resolve_company_default(field: str, value, company: str) -> str:
	"""One default resolved to a docname, or "" to clear it. Raises if it cannot be."""
	text = str(value or "").strip()
	if not text:
		return ""

	if field in _COST_CENTER_DEFAULTS:
		row = _cost_center(text, company)
		if row["company"] != company:  # pragma: no cover - resolve_cost_center refuses first
			raise ToolError(f"{field}: cost center {row['name']!r} belongs to {row['company']!r}")
		if int(row.get("is_group") or 0):
			raise ToolError(
				f"{field}: {row['name']!r} is a group cost center, and ERPNext files "
				"postings against leaf cost centers only. Nothing was changed."
			)
		return row["name"]

	rule = _ACCOUNT_DEFAULTS[field]
	docname = resolve_account(text, company)
	account = (
		frappe.db.get_value(
			"Account",
			docname,
			compat.existing_fields(
				"Account", ("name", "company", "is_group", "root_type", "account_type", "disabled")
			),
			as_dict=True,
		)
		or {}
	)

	if str(account.get("company") or "") != company:
		raise ToolError(
			f"{field}: account {docname!r} belongs to company {account.get('company')!r}, "
			f"not {company!r}. Nothing was changed."
		)
	if int(account.get("is_group") or 0):
		raise ToolError(
			f"{field}: {docname!r} is a group account, and ERPNext posts only to leaf "
			"accounts. Nothing was changed."
		)
	if int(account.get("disabled") or 0):
		raise ToolError(
			f"{field}: {docname!r} is disabled. A company default has to point at an "
			"account documents can actually reach. Nothing was changed."
		)

	root_type = str(account.get("root_type") or "")
	if rule["root_type"] and root_type not in rule["root_type"]:
		raise ToolError(
			f"{field} is {rule['what']}, so it has to point at "
			f"{_or_list(rule['root_type'])} account; {docname!r} is {root_type or 'untyped'}. "
			"Nothing was changed."
		)

	account_type = str(account.get("account_type") or "")
	if rule["account_type"] and account_type not in rule["account_type"]:
		wanted = _or_list([value for value in rule["account_type"] if value])
		raise ToolError(
			f"{field} has to point at an account whose account_type is {wanted}; "
			f"{docname!r} is {account_type or 'untyped'}. ERPNext keys "
			f"{rule['what']} off that flag, not off the account's name or number, so "
			"this would post and then fail to reconcile. Fix it with "
			"update_account(new_account_type=…) first. Nothing was changed."
		)
	return docname


def _or_list(values) -> str:
	values = [str(value) for value in values if str(value)]
	if len(values) == 1:
		return f"an {values[0]}" if values[0][0] in "AEIOU" else f"a {values[0]}"
	return " or ".join([", ".join(values[:-1]), values[-1]])


# ── 134. bulk_wire_default_accounts ─────────────────────────────────────────
#: The defaults a company needs before it can transact at all, and where to look
#: for each one. `numbers` are ERPNext's own account numbers in the "Standard
#: with Numbers" chart template; `account_types` is the fallback for every other
#: chart, and it is the one that keeps working on a chart somebody wrote by hand.
#:
#: DELIBERATELY NOT EVERY SUPPORTED DEFAULT. `set_company_defaults` accepts
#: twenty-five fields and most of them are for modules a farm does not run —
#: stock valuation, capital work in progress, deferred revenue. Guessing at those
#: is how a company ends up with a plausible-looking default pointing at the
#: wrong account and nobody noticing until a document posts through it. These ten
#: are the ones whose absence stops a document being saved.
#:
#: `exchange_gain_loss_account` is NOT here, and its absence is the rule this
#: table is built on. Its only constraint is a root type of Income or Expense, so
#: the only way to "find" it is to take the first expense leaf — which is exactly
#: the plausible-looking guess this tool exists not to make. A field with no
#: honest search stays `set_company_defaults`' job.
#:
#: `names` matches on the account's own name, and it earns its place on the three
#: rows where it appears: an account literally called "Write Off" is real
#: evidence, not a guess, and it is what makes this work on a chart somebody
#: numbered differently. It is tried after account_type and before root_type.
_WIRING_PLAN = {
	"default_receivable_account": {
		"numbers": ("1310",),
		"account_types": ("Receivable",),
		"names": (),
		"root_types": (),
	},
	"default_payable_account": {
		"numbers": ("2110",),
		"account_types": ("Payable",),
		"names": (),
		"root_types": (),
	},
	"default_cash_account": {
		"numbers": ("1140",),
		"account_types": ("Cash",),
		"names": (),
		"root_types": (),
	},
	"default_bank_account": {
		"numbers": ("1110",),
		"account_types": ("Bank",),
		"names": (),
		"root_types": (),
	},
	"default_income_account": {
		"numbers": ("4100",),
		"account_types": ("Income Account", "Direct Income", "Indirect Income"),
		"names": (),
		"root_types": ("Income",),
	},
	"default_expense_account": {
		"numbers": ("5100",),
		"account_types": ("Expense Account", "Direct Expense", "Indirect Expense"),
		"names": (),
		"root_types": ("Expense",),
	},
	"cost_of_goods_sold_account": {
		"numbers": ("5111",),
		"account_types": ("Cost of Goods Sold",),
		"names": ("cost of goods sold",),
		"root_types": (),
	},
	"round_off_account": {
		"numbers": ("5212",),
		"account_types": ("Round Off",),
		"names": ("round off",),
		"root_types": (),
	},
	"write_off_account": {
		"numbers": ("5218",),
		"account_types": (),
		"names": ("write off", "bad debt"),
		"root_types": (),
	},
	"round_off_cost_center": {
		"numbers": (),
		"account_types": (),
		"names": (),
		"root_types": (),
	},
}

#: Just the field names. Public because `registry` lists them in the tool's own
#: argument description and a second hand-maintained copy there would drift.
WIRED_COMPANY_DEFAULTS = tuple(_WIRING_PLAN)

#: How a strategy decides where to look. `auto` reads the company's own
#: `chart_of_accounts` and picks one of the other two, which is what a caller who
#: does not know what chart a company was created from actually wants.
WIRING_STRATEGIES = ("standard_with_numbers", "account_type", "auto")


def bulk_wire_default_accounts(args: dict) -> ToolResult:
	"""Wire a company's default accounts by finding them, rather than by being told.

	THE GAP THIS FILLS. `create_company` sets four defaults and
	`set_company_defaults` sets any of twenty-five — but only ones the caller
	already knows the docnames of. Running `set_company_defaults` against four
	freshly-created companies on 2026-07-30 came back "idempotent" for receivable,
	payable, round-off and write-off, and said nothing at all about cash, bank,
	income and expense, because nobody had passed them. A company with no
	`default_income_account` does not fail loudly; it fails the first time
	somebody saves a Sales Invoice with no account on the line, which is weeks
	later and nowhere near the setup that caused it.

	IT REFUSES TO GUESS, WHICH IS NOT THE SAME AS REFUSING TO WORK. Each field is
	looked for in a fixed order — the caller's own override, then the well-known
	account number for this chart template, then an account whose `account_type`
	means the right thing, then (only where the field's rule permits an untyped
	account) the first leaf of the right root type. If none of those finds an
	account that PASSES `set_company_defaults`' own type checks, the field is
	reported unresolved with what was looked for. It is never filled with
	something merely plausible.

	AND IT DOES NOT REFUSE THE WHOLE CALL FOR ONE MISSING ACCOUNT. A company with
	nine of ten defaults wired is strictly better off than one with none, and a
	chart with no Cost of Goods Sold account is an ordinary chart rather than a
	broken one. So the unresolved fields are named, loudly, in the summary and in
	`unresolved` — and `strict=true` is there for the caller who wants
	all-or-nothing. An OVERRIDE that cannot be resolved is always a hard refusal,
	strict or not: an explicit instruction that cannot be honoured is a different
	thing from a search that came up empty.

	Idempotent. Every field is compared before it is written, so a re-run reports
	everything unchanged and saves nothing — which matters here because
	`Company.save` is not a cheap write.
	"""
	company = resolve_company(as_str(args, "company"), required=True)
	strategy = as_str(args, "strategy") or "standard_with_numbers"
	if strategy not in WIRING_STRATEGIES:
		raise ToolError(
			f"unknown strategy {strategy!r}. Known: {', '.join(WIRING_STRATEGIES)}. "
			"'standard_with_numbers' looks for ERPNext's own account numbers first, "
			"'account_type' skips straight to matching on what each account IS, and 'auto' "
			"reads the company's chart template and picks. Nothing was changed."
		)
	overrides = args.get("overrides")
	if overrides in (None, ""):
		overrides = {}
	if not isinstance(overrides, dict):
		raise ToolError(
			"overrides must be an object of company field → account, e.g. "
			'{"default_bank_account": "1112"}. Nothing was changed.'
		)
	unknown = sorted(set(overrides) - set(_WIRING_PLAN))
	if unknown:
		raise ToolError(
			f"overrides names field(s) this tool does not wire: {', '.join(unknown)}. It wires "
			f"{', '.join(_WIRING_PLAN)}. For anything else use set_company_defaults, which takes "
			"any of the twenty-five supported defaults by name. Nothing was changed."
		)
	dry_run = bool(as_bool(args, "dry_run", False))
	strict = bool(as_bool(args, "strict", False))

	resolved_strategy = _resolved_strategy(strategy, company)
	accounts = _company_accounts(company)

	picks, unresolved, absent = {}, {}, []
	for field, spec in _WIRING_PLAN.items():
		if not compat.has_field("Company", field):
			# A supported default this ERPNext does not have. Not an error and not
			# a silence: the caller is told the field is absent from their version.
			absent.append(field)
			continue
		if field in overrides:
			picks[field] = {
				"account": _resolve_company_default(field, overrides[field], company),
				"picked_by": "override",
			}
			continue
		pick = _pick_default(field, spec, company, resolved_strategy, accounts)
		if pick["account"]:
			picks[field] = pick
		else:
			unresolved[field] = pick["reason"]

	if unresolved and strict:
		raise ToolError(
			f"strict=true and {len(unresolved)} field(s) could not be resolved on {company}: "
			+ "; ".join(f"{field} — {reason}" for field, reason in sorted(unresolved.items()))
			+ ". Pass them in `overrides`, create the missing accounts with create_account, or "
			"drop strict to wire the rest. Nothing was changed."
		)

	current = frappe.db.get_value("Company", company, sorted(picks), as_dict=True) if picks else {}
	changes, unchanged = {}, []
	for field, pick in picks.items():
		before = str((current or {}).get(field) or "")
		if before == pick["account"]:
			unchanged.append(field)
		else:
			changes[field] = [before, pick["account"]]

	data = {
		"company": company,
		"strategy": strategy,
		"strategy_used": resolved_strategy,
		"changed": changes,
		"unchanged": sorted(unchanged),
		"defaults_now": {field: pick["account"] for field, pick in sorted(picks.items())},
		"picked_by": {field: pick["picked_by"] for field, pick in sorted(picks.items())},
		"unresolved": unresolved,
		"fields_absent_on_this_erpnext": sorted(absent),
		"idempotent": True,
		"dry_run": dry_run,
		"note": (
			"Company defaults decide which account a document reaches for when nothing on the "
			"document says. They touch no existing posting — every document already written "
			"keeps the accounts it was written with. Nothing here was guessed: each field was "
			"looked for by number, then by account type, then by root type, and left unresolved "
			"rather than filled with something merely plausible."
		),
	}
	if unresolved:
		data["next_step"] = (
			f"{len(unresolved)} field(s) are still empty on {company}: "
			f"{', '.join(sorted(unresolved))}. Create the accounts with create_account and re-run, "
			"or pass them explicitly in `overrides`. get_chart_of_accounts shows what this company "
			"actually has."
		)

	if dry_run:
		data["note"] = "Nothing was written. " + data["note"]
		return ToolResult(
			data,
			f"dry run: would set {len(changes)} default(s) on {company} "
			f"({resolved_strategy}), leave {len(unchanged)} unchanged"
			+ (f", {len(unresolved)} unresolved: {', '.join(sorted(unresolved))}" if unresolved else ""),
			docstatus_delta="",
		)

	if changes:
		doc = frappe.get_doc("Company", company)
		for field, (_before, after) in changes.items():
			doc.set(field, after or None)
		doc.save()

	summary = ", ".join(f"{field}={after!r}" for field, (_before, after) in sorted(changes.items()))
	return ToolResult(
		data,
		(
			f"wired {len(changes)} default(s) on {company} ({resolved_strategy}): {summary}"
			if changes
			else f"{company} already had all {len(unchanged)} resolvable default(s); nothing changed"
		)
		+ (f" — UNRESOLVED: {', '.join(sorted(unresolved))}" if unresolved else ""),
		docstatus_delta="",
	)


def _resolved_strategy(strategy: str, company: str) -> str:
	"""`auto` turned into a real strategy by reading the company's own chart template."""
	if strategy != "auto":
		return strategy
	template = str(frappe.db.get_value("Company", company, "chart_of_accounts") or "")
	return "standard_with_numbers" if "number" in template.lower() else "account_type"


#: Fields on `_WIRING_PLAN` that take a Cost Center rather than an Account.
_WIRED_COST_CENTERS = ("round_off_cost_center",)


def _company_accounts(company: str) -> list:
	"""Every account of this company, groups and disabled ones included.

	Read once and filtered in Python rather than queried per field: ten fields
	against one company is ten round trips for a table that is a few hundred
	rows, and having the whole list in hand is what lets a refusal say what WAS
	there instead of only what was missing.

	Groups are kept because the search has to be able to WALK them — ERPNext's
	1110 is "Bank Accounts", a group nothing can post to, and the account a
	default belongs on is the one underneath it.
	"""
	fields = compat.existing_fields(
		"Account",
		(
			"name",
			"account_number",
			"account_name",
			"root_type",
			"account_type",
			"is_group",
			"disabled",
			"parent_account",
		),
	)
	rows = frappe.db.get_all("Account", filters={"company": company}, fields=fields, limit=2000)
	return [dict(row) for row in rows or []]


def _postable(rows: list) -> list:
	"""Just the accounts a document could actually post to.

	Group and disabled accounts are excluded here rather than checked later,
	because a company default has to point at something a posting can reach and
	neither of those is.
	"""
	return [row for row in rows if not int(row.get("is_group") or 0) and not int(row.get("disabled") or 0)]


def _under(group: dict, rows: list) -> list:
	"""Every postable account below a group, breadth-first.

	Breadth-first and not by number prefix, which was the obvious shortcut and is
	wrong: ERPNext's 1110 group holds 1111 and 1112, and neither string starts
	with "1110". The parent link is the only thing that actually says what is
	inside what.
	"""
	by_parent: dict = {}
	for row in rows:
		by_parent.setdefault(str(row.get("parent_account") or ""), []).append(row)
	found, frontier, seen = [], [group], {group["name"]}
	while frontier:
		nxt = []
		for parent in frontier:
			for child in by_parent.get(parent["name"], []):
				if child["name"] in seen:  # pragma: no cover - a chart is a tree
					continue
				seen.add(child["name"])
				(nxt if int(child.get("is_group") or 0) else found).append(child)
		frontier = nxt
	return _postable(found)


def _plain_or(values) -> str:
	"""`a, b or c` with no article in front. `_or_list` adds one, which reads
	right inside a sentence about ONE account and wrong inside a list of things
	that were looked for."""
	values = [str(value) for value in values if str(value)]
	if len(values) < 2:
		return values[0] if values else ""
	return " or ".join([", ".join(values[:-1]), values[-1]])


def _pick_default(field: str, spec: dict, company: str, strategy: str, rows: list) -> dict:
	"""One default resolved by search, or a sentence saying what was looked for."""
	if field in _WIRED_COST_CENTERS:
		return _pick_cost_center(field, company)

	rule = _ACCOUNT_DEFAULTS[field]
	leaves = _postable(rows)
	tried = []

	if strategy == "standard_with_numbers" and spec["numbers"]:
		tried.append(f"account number {_plain_or(spec['numbers'])}")
		for number in spec["numbers"]:
			numbered = [row for row in rows if str(row.get("account_number") or "") == number]
			match = _first_passing(_postable(numbered), field, company)
			if match:
				return {"account": match, "picked_by": f"account number {number}"}
			for group in [row for row in numbered if int(row.get("is_group") or 0)]:
				descended = _first_passing(_under(group, rows), field, company)
				if descended:
					return {"account": descended, "picked_by": f"a sub-ledger under {number}"}

	if spec["account_types"]:
		tried.append(f"account_type {_plain_or(spec['account_types'])}")
		for account_type in spec["account_types"]:
			match = _first_passing(
				[row for row in leaves if str(row.get("account_type") or "") == account_type],
				field,
				company,
			)
			if match:
				return {"account": match, "picked_by": f"account_type {account_type}"}

	if spec["names"]:
		tried.append("an account named " + _plain_or([f"'{name}'" for name in spec["names"]]))
		for keyword in spec["names"]:
			match = _first_passing(
				[row for row in leaves if keyword in str(row.get("account_name") or "").lower()],
				field,
				company,
			)
			if match:
				return {"account": match, "picked_by": f"an account named like {keyword!r}"}

	if spec["root_types"]:
		tried.append(f"the first {_plain_or(spec['root_types'])} leaf")
		for root_type in spec["root_types"]:
			match = _first_passing(
				[row for row in leaves if str(row.get("root_type") or "") == root_type],
				field,
				company,
			)
			if match:
				return {"account": match, "picked_by": f"the first {root_type} leaf"}

	if not tried:  # pragma: no cover - every account field in the plan has a rule
		tried.append("nothing — this field has no search rule and must be given in `overrides`")
	wanted = _plain_or(rule["root_type"]) if rule["root_type"] else "any root type"
	return {
		"account": "",
		"picked_by": "",
		"reason": (
			f"no {wanted} leaf account on {company} matched. Looked for: {'; then '.join(tried)}. "
			f"This field is {rule['what']}. Create the account with create_account, or pass it in "
			"`overrides`."
		),
	}


def _first_passing(candidates: list, field: str, company: str) -> str:
	"""The first candidate `set_company_defaults` would accept, in number order.

	Sorted so the answer is deterministic: a chart with two Bank accounts must
	wire the same one on every run, or "idempotent" would be a lie the second time
	somebody called this.

	`_resolve_company_default` is reused rather than reimplemented, which is the
	whole point — the search is allowed to propose, and the same type checks that
	guard a hand-written default decide. A candidate it refuses is skipped rather
	than raised on: a search that hits a Receivable-shaped account with the wrong
	account_type should move on to the next one, not fail the call.
	"""
	for row in sorted(candidates, key=lambda row: (str(row.get("account_number") or "~"), row["name"])):
		try:
			return _resolve_company_default(field, row["name"], company)
		except ToolError:
			continue
	return ""


def _pick_cost_center(field: str, company: str) -> dict:
	"""The company's own default cost center, if it has one and it is a leaf."""
	current = compat.company_default_cost_center(company)
	if current:
		try:
			return {
				"account": _resolve_company_default(field, current, company),
				"picked_by": "the company's default cost center",
			}
		except ToolError:
			pass
	rows = frappe.db.get_all(
		"Cost Center",
		filters={"company": company, "is_group": 0},
		fields=compat.existing_fields("Cost Center", ("name", "cost_center_number", "disabled")),
		limit=500,
	)
	for row in sorted(
		(row for row in rows or [] if not int(row.get("disabled") or 0)),
		key=lambda row: (str(row.get("cost_center_number") or "~"), row["name"]),
	):
		try:
			return {
				"account": _resolve_company_default(field, row["name"], company),
				"picked_by": "the first leaf cost center",
			}
		except ToolError:
			continue
	return {
		"account": "",
		"picked_by": "",
		"reason": (
			f"{company} has no leaf cost center to file a rounding difference against. Create one "
			"with create_cost_center, or pass it in `overrides`."
		),
	}
