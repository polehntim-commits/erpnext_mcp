# SPDX-License-Identifier: MIT
"""Who paid for a receipt: the company's card, the submitter's own, or the company
paying for something the submitter is covering.

v0.332.0. Three answers, three different books:

  * COMPANY CARD — an ordinary expense. When the card on the slip is not a known
    company number (Bank Card Alias), the phone asks which account it was and
    the number is learned once (`learn_card`), so the next slip from that phone
    is recognised and matched to the bank line.
  * PERSONAL CARD — the submitter paid and is owed the money. Nothing is booked
    at capture; once approved, `post_reimbursement_receipt` books it as a DRAFT
    Journal Entry (Dr the expense, Cr what the farm owes the submitter).
  * I'M COVERING THIS (`covered_by_submitter`) — the company paid, the submitter
    is personally covering it. Once approved, `create_owner_draw` books it as a
    DRAFT Journal Entry to the submitter's member draw account (Cap Table Entry →
    Draw Account, per person), not to an expense.

CAPTURE NEVER REFUSES FOR WANT OF A MAPPING. A phone queue drains hours later on
a truck's hotspot; a receipt bounced because nobody has set a draw account yet
would be lost work. The gaps are reported on the result and refused only at
posting, where a person is at a desk.

LEARNING A CARD IS GATED BY ROLE (`may_learn`). A number registered on the
wrong account moves every later slip's match, so the request of a caller without
an accounting or farm-management role is kept on the receipt (`card_bank_account`)
for a bookkeeper to confirm from the Desk form, where saving the receipt learns it.
"""

from __future__ import annotations

import frappe

from .. import compat, roles, security
from ..args import as_bool, as_str
from ..errors import ToolError
from . import card_aliases

EXPENSE_RECEIPT = "Expense Receipt"
CAP_TABLE = "Cap Table Entry"

PAID_WITH_FIELD = "paid_with"
ACCOUNT_FIELD = "card_bank_account"
COVERED_FIELD = "covered_by_submitter"

COMPANY_CARD = "Company card"
PERSONAL_CARD = "Personal card"
#: Mirrored from the Expense Receipt JSON's Select (blank first); a test holds them equal.
PAID_WITH = (COMPANY_CARD, PERSONAL_CARD)

#: Who may teach the register a card number from a receipt.
LEARN_ROLES = ("System Manager", "Accounts Manager", "Accounts User", "Farm Manager", "Member Manager")


def installed() -> bool:
	try:
		return compat.has_field(EXPENSE_RECEIPT, PAID_WITH_FIELD)
	except Exception:
		return False


def actor() -> str:
	return security.caller_identity() or str(getattr(frappe.session, "user", "") or "")


def may_learn(user: str = "") -> bool:
	user = user or actor()
	if not user or user == "Guest":
		return False
	held = set(frappe.get_roles(user) or []) or set(roles.all_roles_of(user) or [])
	return bool(held & set(LEARN_ROLES))


# ── the per-person mapping ───────────────────────────────────────────────────


def member_for(employee: str, company: str) -> dict | None:
	"""The active Cap Table Entry an Employee captures receipts as, in one company."""
	if not employee or not compat.doctype_exists(CAP_TABLE) or not compat.has_field(CAP_TABLE, "employee"):
		return None
	rows = frappe.db.get_all(
		CAP_TABLE,
		filters={"employee": employee, "company": company, "retired": 0},
		fields=compat.existing_fields(CAP_TABLE, ("name", "member_id", "legal_entity_name", "draw_account")),
		limit=2,
	)
	return dict(rows[0]) if len(rows) == 1 else None


def draw_account_for(employee: str, company: str) -> str:
	row = member_for(employee, company)
	return str((row or {}).get("draw_account") or "")


# ── capture ──────────────────────────────────────────────────────────────────


def capture_arguments(args: dict, *, category: str, company: str, tail: str) -> dict:
	"""The who-paid columns for a new receipt, checked; plus the learning request.

	Returns {"columns": {...}, "learn": {...} | None}. Refuses only combinations
	that cannot be true whatever is configured: a personal card and a company
	account, covering a purchase the submitter paid for themselves, and the
	choice on a receipt that is not a purchase.
	"""
	paid_with = as_str(args, PAID_WITH_FIELD)
	account = as_str(args, ACCOUNT_FIELD)
	covered = bool(as_bool(args, COVERED_FIELD, False))
	if not (paid_with or account or covered or as_bool(args, "save_card_alias", False)):
		return {"columns": {}, "learn": None}
	if not installed():
		raise ToolError(
			"this site's Expense Receipt has no paid_with column yet. Run `bench migrate` after "
			f"installing v0.332.0. {tail}"
		)
	_check_category(category, covered, paid_with, tail)
	if paid_with and paid_with not in PAID_WITH:
		raise ToolError(f"paid_with must be one of: {', '.join(PAID_WITH)} — got {paid_with!r}. {tail}")
	if account:
		account = _company_account(account, company, tail)
		if paid_with == PERSONAL_CARD:
			raise ToolError(
				f"{account} is a company account, and paid_with says a personal card. Send one or the "
				f"other. {tail}"
			)
		paid_with = paid_with or COMPANY_CARD
	if covered and paid_with == PERSONAL_CARD:
		raise ToolError(
			'"I\'m covering this" is for a purchase the company paid; a personal-card purchase is '
			f"already the submitter's own money. Untick one. {tail}"
		)
	columns = {COVERED_FIELD: 1 if covered else 0}
	if paid_with:
		columns[PAID_WITH_FIELD] = paid_with
	if account:
		columns[ACCOUNT_FIELD] = account
	learn = None
	if as_bool(args, "save_card_alias", bool(account)) and account:
		learn = {
			"bank_account": account,
			"card_last_four": as_str(args, "card_alias_card"),
			"kind": as_str(args, "card_alias_kind"),
			"label": as_str(args, "card_alias_label"),
		}
	return {"columns": columns, "learn": learn}


def _check_category(category: str, covered: bool, paid_with: str, tail: str) -> None:
	from . import expenses

	not_purchases = (
		*expenses.NON_EXPENSE_CATEGORIES,
		expenses.OWNER_DRAW_CATEGORY,
	)
	if category in not_purchases and (covered or paid_with):
		raise ToolError(
			f"who paid applies to a purchase, and this receipt is categorised {category!r}. Leave "
			f"paid_with and covered_by_submitter out. {tail}"
		)


def _company_account(value: str, company: str, tail: str) -> str:
	from . import read

	compat.require_doctype("Bank Account", "It ships with ERPNext's Accounts module.")
	name = read._resolve_bank_account(value)
	row = frappe.db.get_value("Bank Account", name, ["company", "is_company_account"], as_dict=True) or {}
	if row.get("company") and company and row["company"] != company:
		raise ToolError(f"{name} belongs to {row['company']}, and this receipt to {company}. {tail}")
	if not int(row.get("is_company_account") or 0):
		raise ToolError(f"{name} is not a company bank account. {tail}")
	return name


def learn_card(receipt: str, number: str, request: dict | None, user: str = "") -> dict | None:
	"""Register an unknown card number from a receipt the submitter said a company card paid.

	Never raises — the receipt is already saved, and a learning refusal is a
	sentence on the result, not a failed capture. `pending` means the request is
	kept on the receipt for a bookkeeper (see the module docstring).
	"""
	if not request or not number:
		return None
	account = request["bank_account"]
	company = frappe.db.get_value("Bank Account", account, "company") or ""
	try:
		known = card_aliases.resolve(number, card_aliases.load_index(company), company)
	except Exception:
		known = []
	if any(entry["bank_account"] == account for entry in known):
		return {"action": "known", "bank_account": account, "number": number}
	if not card_aliases.installed():
		return {"action": "skipped", "why": "this site has no Bank Card Alias register — run bench migrate"}
	if not may_learn(user):
		return {
			"action": "pending",
			"bank_account": account,
			"number": number,
			"why": (
				"kept on the receipt for a bookkeeper: saving it from the Desk form registers the "
				"number on this account"
			),
		}
	card = request.get("card_last_four") or ""
	try:
		card = card_aliases._digits(card or number, "card_alias_card")
		data, summary = card_aliases.register(
			account, card, number, kind=request.get("kind") or "", label=request.get("label") or ""
		)
	except ToolError as error:
		return {"action": "refused", "bank_account": account, "number": number, "why": str(error)}
	except Exception:
		frappe.log_error(title="erpnext_mcp: could not learn a card number", message=compat.traceback_text())
		return {"action": "failed", "bank_account": account, "number": number}
	return {
		"action": "created",
		"alias": data["alias"],
		"receipts_tagged": data["receipts_tagged"],
		"summary": summary,
	}


def learn_from_saved_receipt(doc) -> dict | None:
	"""The Desk path: a receipt saved with Company card + an account and an unknown number."""
	if doc.get(PAID_WITH_FIELD) != COMPANY_CARD or not doc.get(ACCOUNT_FIELD):
		return None
	number = str(doc.get("card_last_four") or "").strip()
	if not number or doc.get(card_aliases.RECEIPT_FIELD):
		return None
	result = learn_card(doc.name, number, {"bank_account": doc.get(ACCOUNT_FIELD)})
	return result


def reported(
	columns: dict, learned: dict | None, *, card_last_four: str, company: str, employee: str
) -> dict:
	"""What a capture tells the caller about who paid."""
	out = {
		"paid_with": columns.get(PAID_WITH_FIELD) or None,
		"card_bank_account": columns.get(ACCOUNT_FIELD) or None,
		"covered_by_submitter": bool(columns.get(COVERED_FIELD)),
		"card_known": bool(
			card_last_four and card_aliases.resolve(card_last_four, card_aliases.load_index(company), company)
		),
		"card_learned": learned,
	}
	if out["covered_by_submitter"] and not draw_account_for(employee, company):
		out["covering_note"] = (
			"No draw account is mapped for this person yet (Cap Table Entry → Employee and Draw "
			"Account). The receipt is kept; create_owner_draw needs the mapping, or draw_account, "
			"when it is booked."
		)
	return out


# ── update (MCP and the Desk) ──────────────────────────────────────────────


def update_value(key: str, args: dict, doc) -> object:
	"""One who-paid field's new value for `update_expense_receipt`, checked on its own."""
	tail = "Nothing was changed."
	if not installed():
		raise ToolError(f"this site has no paid_with column yet. Run `bench migrate` after v0.332.0. {tail}")
	if key == COVERED_FIELD:
		return 1 if as_bool(args, COVERED_FIELD, False) else 0
	value = as_str(args, key)
	if key == PAID_WITH_FIELD:
		if value and value not in PAID_WITH:
			raise ToolError(f"paid_with must be one of: {', '.join(PAID_WITH)}, or ''. {tail}")
		return value
	return _company_account(value, doc.get("company"), tail) if value else ""


def follow_update(doc, after: dict) -> None:
	"""The combination after this update, checked; a company account implies a company card.

	Refused once the receipt is booked: who paid decides which entry it got, and
	changing it afterwards would leave that entry saying something else.
	"""
	keys = (PAID_WITH_FIELD, ACCOUNT_FIELD, COVERED_FIELD)
	if not any(key in after for key in keys):
		return
	tail = "Nothing was changed."
	if doc.get("linked_document"):
		raise ToolError(
			f"{doc.name} is already booked as {doc.get('linked_doctype')} {doc.get('linked_document')}. "
			f"Who paid decided that entry; cancel it first to change the answer. {tail}"
		)
	state = {key: after[key] if key in after else doc.get(key) for key in keys}
	if PAID_WITH_FIELD in after and after[PAID_WITH_FIELD] == PERSONAL_CARD and state[ACCOUNT_FIELD]:
		after[ACCOUNT_FIELD] = None
		state[ACCOUNT_FIELD] = None
	if state[ACCOUNT_FIELD] and not state[PAID_WITH_FIELD]:
		after[PAID_WITH_FIELD] = COMPANY_CARD
		state[PAID_WITH_FIELD] = COMPANY_CARD
	if state[ACCOUNT_FIELD] and state[PAID_WITH_FIELD] == PERSONAL_CARD:
		raise ToolError(
			f"a personal card does not draw on {state[ACCOUNT_FIELD]}. Send one or the other. {tail}"
		)
	if int(state[COVERED_FIELD] or 0) and state[PAID_WITH_FIELD] == PERSONAL_CARD:
		raise ToolError(
			'"I\'m covering this" is for a purchase the company paid; a personal-card purchase is '
			f"already the submitter's own money. Untick one. {tail}"
		)
	_check_category(
		after.get("category") or doc.get("category"),
		bool(int(state[COVERED_FIELD] or 0)),
		state[PAID_WITH_FIELD],
		tail,
	)


# ── the phone's offline list ─────────────────────────────────────────────────


def company_cards(companies: list, employee: str = "") -> dict:
	"""Every company bank account, with the card numbers it knows — the phone caches this.

	Only last-four digits and account labels: enough to decide, with no signal, whether
	to ask "company card or personal card?", and nothing an account could be reached by.
	"""
	accounts = []
	if compat.doctype_exists("Bank Account"):
		filters = {"is_company_account": 1}
		if companies:
			filters["company"] = ("in", list(companies))
		rows = frappe.db.get_all(
			"Bank Account",
			filters=filters,
			fields=compat.existing_fields("Bank Account", ("name", "account_name", "company", "disabled")),
			order_by="company asc, account_name asc",
			limit=200,
		)
		index = {}
		for company in sorted({row.get("company") for row in rows if row.get("company")}):
			for number, entries in card_aliases.load_index(company).items():
				for entry in entries:
					index.setdefault(entry["bank_account"], {}).setdefault(
						entry["card_last_four"], {"card_last_four": entry["card_last_four"], "numbers": []}
					)["numbers"].append({"number": number, "kind": entry["kind"], "label": entry["label"]})
		for row in rows:
			if int(row.get("disabled") or 0):
				continue
			cards = sorted(index.get(row["name"], {}).values(), key=lambda card: card["card_last_four"])
			for card in cards:
				card["numbers"].sort(key=lambda entry: entry["number"])
			accounts.append(
				{
					"bank_account": row["name"],
					"label": row.get("account_name") or row["name"],
					"company": row.get("company"),
					"cards": cards,
				}
			)
	numbers = sorted(
		{entry["number"] for account in accounts for card in account["cards"] for entry in card["numbers"]}
	)
	return {
		"accounts": accounts,
		"known_numbers": numbers,
		"may_save_card": may_learn(),
		"draw_mapped": {company: bool(draw_account_for(employee, company)) for company in companies},
	}
