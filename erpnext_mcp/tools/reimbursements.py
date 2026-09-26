# SPDX-License-Identifier: MIT
"""Reimbursement Received — somebody's check paying back their share of an expense.

v0.186.0. The farm fronts a shared expense (a Coastal run, say) and a family
member pays their portion back with a personal check days or weeks later. The
check is photographed through the same receipt flow as everything else, as
category `Reimbursement Received`:

  * `merchant` is the PAYER — whoever the paper is from, as on every receipt;
  * `amount` is the check;
  * `receipt_image` is the photograph of it;
  * `reimburses_receipt` optionally names the expense it pays back.

IT IS MONEY IN, SO IT IS ALWAYS `is_return`. That one flag already does the two
things a deposit needs: the bank matcher looks at credits for it, and the expense
summary nets it out of spend. Nothing downstream needed a new branch.

TWO TOOLS' WORTH OF HOOKS AND ONE TOOL. `capture_arguments`, `checked_link` and
`follow_update` are called from `submit_expense_receipt` and
`update_expense_receipt`. `post_reimbursement_receipt` books an APPROVED
reimbursement as a DRAFT Journal Entry — the same approve-then-post shape as
`post_coop_receipt` and `create_owner_draw`, chosen by Tim on 2026-09-26 over
booking at phone submit: the phone never writes to the ledger, and approval
stays a desk act (see `api/mobile.py` on why it is not a phone route).

THE ENTRY. Dr the bank the check went into (the company's default bank, or
`counter_account`). Cr, in order of preference:

  1. `credit_account`, when the caller names one — an expense account, or an
     asset account such as a "Due From" the farm keeps for the person;
  2. the ORIGINAL expense's own account, when the receipt is linked: read off
     the Purchase Invoice the original became, or matched from its category
     exactly as `create_purchase_invoice_from_receipt` would have;
  3. otherwise it refuses, naming both ways to say which account.

Crediting the original expense account is what makes the farm's books show its
own share of the cost and nothing more — which is the point of being paid back.

NOTHING HERE IS SUBMITTED. `submit_journal_entry` posts the draft, behind its
own switch.
"""

import frappe

from .. import compat
from ..args import as_date, as_str, resolve_account
from ..errors import ToolError
from ..result import ToolResult
from . import expenses, governance, mutate

EXPENSE_RECEIPT = expenses.EXPENSE_RECEIPT
REIMBURSEMENT_CATEGORY = expenses.REIMBURSEMENT_CATEGORY
LINK_FIELD = "reimburses_receipt"

#: What a reimbursement may be credited to when the caller names the account. An
#: expense account (the usual case) or an asset one (a "Due From" the farm keeps).
#: Income is allowed for a farm that books shared-cost recoveries as other income.
CREDIT_ROOT_TYPES = ("Expense", "Asset", "Income")

#: Account types ERPNext will not post without a party. The payer is a person,
#: not a Customer, so these are refused with the reason rather than failing at
#: submit.
PARTY_ACCOUNT_TYPES = ("Receivable", "Payable")

#: Half a cent, so a float sum of several checks that exactly equals the expense
#: is not refused over its last bit.
_TOLERANCE = 0.005


def _installed() -> bool:
	return compat.has_field(EXPENSE_RECEIPT, LINK_FIELD)


def _reimbursed_so_far(original: str, exclude: str = "") -> float:
	"""What the other live reimbursements linked to `original` already add up to."""
	filters = {
		"category": REIMBURSEMENT_CATEGORY,
		LINK_FIELD: original,
		"status": ("!=", expenses.REJECTED),
	}
	if exclude:
		filters["name"] = ("!=", exclude)
	rows = frappe.db.get_all(EXPENSE_RECEIPT, filters=filters, fields=["amount"], limit_page_length=0)
	return round(sum(float(row.get("amount") or 0) for row in rows), 2)


def checked_link(value: str, *, category: str, company: str, amount: float, receipt: str, tail: str) -> str:
	"""The original expense a reimbursement names, checked; `""` to unlink.

	REFUSED: on any other category; an original that does not exist, is in another
	company, is Rejected, or is itself a reimbursement; and a link that would make
	the checks paid back against one expense add up to MORE than it cost — that is
	either a typo in an amount or the wrong expense, and both are worth a sentence
	now rather than a credit to the wrong account later.
	"""
	value = str(value or "").strip()
	if not value:
		return ""
	if category != REIMBURSEMENT_CATEGORY:
		raise ToolError(
			f"reimburses_receipt belongs to a {REIMBURSEMENT_CATEGORY} receipt, and this one is "
			f"categorised {category!r}. {tail}"
		)
	if not _installed():
		raise ToolError(
			"this site's Expense Receipt has no reimburses_receipt column yet. Run `bench --site "
			f"<site> migrate` after installing v0.186.0. {tail}"
		)
	if value == receipt:
		raise ToolError(f"a receipt cannot reimburse itself. {tail}")
	original = frappe.db.get_value(
		EXPENSE_RECEIPT, value, ["name", "company", "category", "status", "amount"], as_dict=True
	)
	if not original:
		raise ToolError(f"reimburses_receipt: no Expense Receipt called {value!r} on this site. {tail}")
	if original.get("category") == REIMBURSEMENT_CATEGORY:
		raise ToolError(
			f"{value} is itself a {REIMBURSEMENT_CATEGORY} receipt. Link the expense the check pays "
			f"back, not another check. {tail}"
		)
	if original.get("company") != company:
		raise ToolError(
			f"{value} belongs to {original.get('company')}, and this reimbursement to {company}. The "
			f"money has to come back to the company that spent it. {tail}"
		)
	if original.get("status") == expenses.REJECTED:
		raise ToolError(f"{value} was Rejected, so there is no expense for this check to pay back. {tail}")
	cost = float(original.get("amount") or 0)
	already = _reimbursed_so_far(value, exclude=receipt)
	if already + float(amount or 0) > cost + _TOLERANCE:
		raise ToolError(
			f"{value} cost {cost:.2f} and {already:.2f} of it has already been paid back, so a "
			f"{float(amount or 0):.2f} check would repay more than was spent. Check the amount, or "
			f"link the right expense. {tail}"
		)
	return value


def capture_arguments(args: dict, category: str, company: str, amount: float) -> dict:
	"""The reimbursement columns for `submit_expense_receipt`, checked. `{}` otherwise."""
	tail = "Nothing was created."
	link = as_str(args, LINK_FIELD)
	if category != REIMBURSEMENT_CATEGORY:
		if link:
			raise ToolError(
				f"reimburses_receipt belongs to a {REIMBURSEMENT_CATEGORY} receipt, and this receipt "
				f"is categorised {category!r}. {tail}"
			)
		return {}
	if amount <= 0:
		raise ToolError(f"a {REIMBURSEMENT_CATEGORY} receipt needs the check's positive amount. {tail}")
	columns = {}
	if link:
		columns[LINK_FIELD] = checked_link(
			link, category=category, company=company, amount=amount, receipt="", tail=tail
		)
	return columns


def follow_update(doc, before: dict, after: dict) -> None:
	"""What `update_expense_receipt` must carry or refuse for a reimbursement.

	INTO the category: `is_return` is ticked in the same write. ON the category: it
	may not be unticked — a reimbursement is money in by definition. OUT of it: a
	receipt still naming an original is refused, so a Fuel slip never carries a
	dangling link; pass reimburses_receipt as '' in the same call.
	"""
	tail = "Nothing was changed."
	old = doc.get("category")
	new = after.get("category", old)
	link = after[LINK_FIELD] if LINK_FIELD in after else doc.get(LINK_FIELD)
	if new == REIMBURSEMENT_CATEGORY:
		if not compat.has_field(EXPENSE_RECEIPT, "is_return"):
			return
		if "is_return" in after and not after["is_return"]:
			raise ToolError(
				f"a {REIMBURSEMENT_CATEGORY} receipt is money coming in, so is_return stays ticked. {tail}"
			)
		if not int(doc.get("is_return") or 0) and "is_return" not in after:
			before["is_return"] = 0
			after["is_return"] = 1
	elif old == REIMBURSEMENT_CATEGORY and link:
		raise ToolError(
			f"this receipt still names {link} as the expense it reimburses. Pass reimburses_receipt "
			f"as '' in the same call to recategorise it as {new!r}. {tail}"
		)


# ── post_reimbursement_receipt ──────────────────────────────────────────────
def _account_row(account: str) -> dict:
	return (
		frappe.db.get_value(
			"Account", account, ["root_type", "account_type", "is_group", "disabled"], as_dict=True
		)
		or {}
	)


def _check_credit(account: str, label: str, tail: str) -> dict:
	row = _account_row(account)
	if row.get("root_type") not in CREDIT_ROOT_TYPES:
		raise ToolError(
			f"{label} {account!r} has root type {row.get('root_type') or 'none'}; a reimbursement "
			f"credits an {', '.join(CREDIT_ROOT_TYPES[:-1])} or {CREDIT_ROOT_TYPES[-1]} account. {tail}"
		)
	if row.get("account_type") in PARTY_ACCOUNT_TYPES:
		raise ToolError(
			f"{label} {account!r} is a {row['account_type']} account, which ERPNext will not post "
			"without a Customer or Supplier, and the payer is neither. Use a plain asset account "
			f"(a 'Due From' ledger) or the expense account. {tail}"
		)
	if int(row.get("is_group") or 0):
		raise ToolError(f"{label} {account!r} is a group, and a group cannot be posted to. {tail}")
	if int(row.get("disabled") or 0):
		raise ToolError(f"{label} {account!r} is disabled. {tail}")
	return row


def _original_account(original: dict, company: str, tail: str) -> tuple:
	"""The account the original expense was booked to, and how that was found.

	THE PURCHASE INVOICE IS THE FACT when there is one: it is what the ledger
	actually says. Read through the parent, since a child table filtered by
	`parent` answers nothing in the test double. One distinct expense account is
	the answer; several are a split bill this tool will not guess a share of.
	Without an invoice, the category is matched to an account exactly as
	`create_purchase_invoice_from_receipt` would match it.
	"""
	from . import receipts  # imports `expenses`; local for the same reason it is there

	name = original["name"]
	if original.get("category") in (
		*expenses.NON_EXPENSE_CATEGORIES,
		expenses.OWNER_DRAW_CATEGORY,
	):
		raise ToolError(
			f"{name} is categorised {original['category']!r}, which is not an expense, so there is "
			f"no expense account to credit. Name the account with credit_account. {tail}"
		)
	if original.get("linked_doctype") == "Purchase Invoice" and original.get("linked_document"):
		invoice = frappe.get_doc("Purchase Invoice", original["linked_document"])
		if int(invoice.get("docstatus") or 0) == 2:
			raise ToolError(
				f"{name}'s Purchase Invoice {invoice.name} is cancelled, so it no longer says where the "
				f"expense was booked. Name the account with credit_account. {tail}"
			)
		found = []
		for item in invoice.get("items") or []:
			account = item.get("expense_account") if isinstance(item, dict) else item.expense_account
			if account and account not in found:
				found.append(account)
		if len(found) == 1:
			return found[0], f"Purchase Invoice {invoice.name}"
		if found:
			raise ToolError(
				f"{name}'s Purchase Invoice {invoice.name} is split across {', '.join(found)}, and "
				f"which one this check pays back is not on the check. Name it with credit_account. "
				f"{tail}"
			)
	try:
		return receipts._match_expense_account(company, original.get("category") or "Other"), (
			f"matched from {name}'s category {original.get('category')!r}"
		)
	except ToolError:
		# Not the matcher's own sentence: it names `expense_account`, which is the
		# invoice tool's argument and not this one's.
		raise ToolError(
			f"{name} has not been billed, and its category {original.get('category')!r} does not "
			f"match exactly one leaf Expense account in {company}. Name the account with "
			f"credit_account. {tail}"
		) from None


def post_reimbursement_receipt(args: dict) -> ToolResult:
	"""Book one Approved Reimbursement Received receipt as a DRAFT Journal Entry."""
	tail = "Nothing was created."
	receipt = as_str(args, "receipt") or as_str(args, "expense_receipt") or as_str(args, "name")
	if not receipt:
		raise ToolError(f"receipt (the Expense Receipt docname) is required. {tail}")
	if not frappe.db.exists(EXPENSE_RECEIPT, receipt):
		raise ToolError(f"no Expense Receipt called {receipt!r} on this site. {tail}")
	row = frappe.db.get_value(
		EXPENSE_RECEIPT,
		receipt,
		[
			"category",
			"status",
			"amount",
			"company",
			"merchant",
			"receipt_date",
			"cost_center",
			"linked_doctype",
			"linked_document",
			*compat.existing_fields(EXPENSE_RECEIPT, (LINK_FIELD, "bank_transaction")),
		],
		as_dict=True,
	)
	if row.get("category") != REIMBURSEMENT_CATEGORY:
		raise ToolError(
			f"{receipt} is categorised {row.get('category')!r}, and this tool books only a "
			f"{REIMBURSEMENT_CATEGORY} receipt. {tail}"
		)
	if row.get("status") != expenses.APPROVED:
		raise ToolError(
			f"{receipt} is {row.get('status')!r}, not Approved. Approve it with "
			f"approve_expense_receipt first. {tail}"
		)
	if row.get("linked_document"):
		raise ToolError(
			f"{receipt} is already linked to {row['linked_doctype']} {row['linked_document']}. {tail}"
		)
	amount = float(row.get("amount") or 0)
	if amount <= 0:
		raise ToolError(f"{receipt} has amount {amount}; there is nothing to book. {tail}")

	company = row["company"]
	payer = row.get("merchant") or ""
	original_name = row.get(LINK_FIELD) or ""
	original = (
		frappe.db.get_value(
			EXPENSE_RECEIPT,
			original_name,
			["name", "category", "cost_center", "linked_doctype", "linked_document"],
			as_dict=True,
		)
		if original_name
		else None
	)

	named = as_str(args, "credit_account")
	if named:
		credit = resolve_account(named, company)
		credit_resolved_by = "argument"
	elif original:
		credit, credit_resolved_by = _original_account(original, company, tail)
	else:
		raise ToolError(
			f"{receipt} does not name the expense it pays back, so there is no account to credit. "
			"Link it with update_expense_receipt(reimburses_receipt=...) to credit that expense's own "
			f"account, or pass credit_account. {tail}"
		)
	credit_row = _check_credit(credit, "credit_account" if named else "the original's account", tail)

	bank = governance._counter_account(args, company)
	posting_date = as_date(args, "posting_date") or str(row.get("receipt_date"))

	# A PROFIT-AND-LOSS LINE NEEDS A COST CENTER at submit, and the one the expense
	# was coded to is the right one to take the money back off.
	cost_center = None
	if credit_row.get("root_type") in ("Expense", "Income"):
		cost_center = (
			as_str(args, "cost_center")
			or row.get("cost_center")
			or (original or {}).get("cost_center")
			or frappe.db.get_value("Company", company, "cost_center")
		)
		if not cost_center:
			raise ToolError(
				f"{company} has no default cost center, and ERPNext refuses an {credit_row['root_type']} "
				f"line without one. Pass cost_center. {tail}"
			)
		if not frappe.db.exists("Cost Center", cost_center):
			raise ToolError(f"no Cost Center called {cost_center!r} on this site. {tail}")

	remark = f"{REIMBURSEMENT_CATEGORY} — {payer} ({receipt})" + (
		f", paying back {original_name}" if original_name else ""
	)
	credit_line = {"account": credit, "credit": amount, "user_remark": remark}
	if cost_center:
		credit_line["cost_center"] = cost_center
	raw = [{"account": bank, "debit": amount, "user_remark": remark}, credit_line]

	# THE BANK TRANSACTION RIDES IN cheque_no WHEN THE DEPOSIT IS ALREADY MATCHED.
	# That is where every bank-fed entry on this site carries its BTN, and v0.184.0's
	# duplicate control reads it: two reimbursements of the same amount on one day
	# with different deposits are then never reported as duplicates of each other.
	extras = {}
	if row.get("bank_transaction"):
		extras = {"cheque_no": row["bank_transaction"], "cheque_date": posting_date}

	lines = mutate.validated_journal_lines(raw, company)
	doc = mutate.insert_draft_journal_entry(company, posting_date, lines, remark, extras)
	frappe.db.set_value(
		EXPENSE_RECEIPT, receipt, {"linked_doctype": "Journal Entry", "linked_document": doc.name}
	)

	return ToolResult(
		data={
			"journal_entry": doc.name,
			"docstatus": 0,
			"receipt": receipt,
			"company": company,
			"payer": payer,
			"amount": amount,
			"posting_date": posting_date,
			"reimburses_receipt": original_name or None,
			"bank_account": bank,
			"credit_account": credit,
			"credit_account_resolved_by": credit_resolved_by,
			"cost_center": cost_center,
			"cheque_no": extras.get("cheque_no"),
			"lines": [
				{
					"account": line["account"],
					"debit": line.get("debit") or 0,
					"credit": line.get("credit") or 0,
				}
				for line in raw
			],
		},
		summary=f"draft Journal Entry {doc.name} for {receipt}: {payer} paid back {amount}"
		+ (f" of {original_name}" if original_name else "")
		+ f", Cr {credit}",
		docstatus_delta="none → 0 (draft Journal Entry)",
	)
