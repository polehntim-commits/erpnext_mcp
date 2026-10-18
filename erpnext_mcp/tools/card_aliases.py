# SPDX-License-Identifier: MIT
"""Which card a receipt's last four digits stand for — Apple Pay included.

v0.331.0. The card fingerprint (`banking_bridge.card_fingerprint`, v0.75.0)
compares the last four printed on a slip with the last four the bank prints on
the statement line. That works for a swiped card and fails for every phone
payment: Apple Pay, Google Pay and Samsung Pay hand the merchant a DEVICE
ACCOUNT NUMBER, so the slip reads ••7637 while the bank line reads CARD 0845,
and the matcher saw two different cards. On OML two iPhones carry the Wells
Fargo business card ending 0845, and their receipts matched on amount, date and
merchant alone.

ONE ROW PER NUMBER A RECEIPT CAN PRINT. The card's own row (`kind` Card,
`number` = `card_last_four`) and one row for each phone or watch that carries
it, all under the bank account the card draws on. A standalone register keyed
by bank account rather than a child table on ERPNext's Bank Account: the
matcher needs every row for a company in one query, and a child table on
somebody else's doctype would need a Custom Table Field to exist before the
first row could be written.

WHAT THE MATCHER DOES WITH IT (`banking_bridge.score_match`):

  * ALIAS HIT — the receipt's digits resolve to a card, and the bank line names
    that card (or another number of the same card) within a day. Scored exactly
    as a direct fingerprint: same bonus, same tie-break in a contest.
  * ACCOUNT MISMATCH — the receipt's digits resolve ONLY to cards on other bank
    accounts than the line's. A mild penalty, never a blocker: four digits can
    collide, and a person naming both documents still outranks the register.

Unregistered digits change nothing — an absent alias is silence, exactly like
an absent card.

`remove_bank_card_alias` DEACTIVATES. Receipts tagged with a row keep the link,
so "which card paid for this" stays answerable after a phone is replaced.
"""

from __future__ import annotations

import re

import frappe

from .. import compat
from ..args import as_bool, as_limit, as_str
from ..errors import ToolError
from ..result import ToolResult

DOCTYPE = "Bank Card Alias"
BANK_ACCOUNT = "Bank Account"
EXPENSE_RECEIPT = "Expense Receipt"

#: The Expense Receipt column a capture tags with the resolved row.
RECEIPT_FIELD = "card_alias"

KIND_CARD = "Card"
#: Mirrored from the doctype JSON's Select options; a test holds them equal.
KINDS = (KIND_CARD, "Apple Pay", "Google Pay", "Samsung Pay", "Other Device")

_FIELDS = ("name", "bank_account", "company", "card_last_four", "number", "kind", "label", "active")

_HINT = "It ships with erpnext_mcp — run `bench migrate` after installing v0.331.0."


def installed() -> bool:
	try:
		return compat.doctype_exists(DOCTYPE)
	except Exception:
		return False


def _digits(value, field: str) -> str:
	"""Four digits from `••7637`, `xxxx-7637` or `7637`; refuses anything else."""
	text = str(value or "").strip()
	digits = re.sub(r"\D", "", text)
	if len(digits) > 4:
		raise ToolError(
			f"{field} takes the LAST FOUR digits only and got {len(digits)}. Never send a full "
			"card number. Nothing was written."
		)
	if len(digits) != 4:
		raise ToolError(f"{field} must be exactly 4 digits — got {text!r}. Nothing was written.")
	return digits


# ── the index the matcher reads ──────────────────────────────────────────────


def load_index(company: str = "") -> dict:
	"""{number → [entry]} for every ACTIVE row, each entry carrying its card's numbers.

	`entry["numbers"]` is every number of the same physical card on the same
	bank account — the card's own last four and each device's — because a bank
	line names the card, and occasionally the device, and either confirms it.

	NEVER RAISES. The matcher calls this once per tool call; a site without the
	register, or with one it cannot read, scores exactly as before.
	"""
	if not installed():
		return {}
	try:
		filters = {"active": 1}
		if company:
			filters["company"] = company
		rows = frappe.db.get_all(
			DOCTYPE,
			filters=filters,
			fields=compat.existing_fields(DOCTYPE, _FIELDS),
			limit=5000,
		)
	except Exception:
		return {}
	cards = {}
	for row in rows:
		key = (row.get("bank_account"), str(row.get("card_last_four") or ""))
		cards.setdefault(key, set()).update(
			{str(row.get("card_last_four") or ""), str(row.get("number") or "")}
		)
	index = {}
	for row in rows:
		key = (row.get("bank_account"), str(row.get("card_last_four") or ""))
		entry = {
			"name": row.get("name"),
			"bank_account": row.get("bank_account"),
			"company": row.get("company"),
			"card_last_four": str(row.get("card_last_four") or ""),
			"number": str(row.get("number") or ""),
			"kind": row.get("kind") or KIND_CARD,
			"label": row.get("label") or None,
			"numbers": sorted(number for number in cards[key] if number),
		}
		index.setdefault(entry["number"], []).append(entry)
	return index


def resolve(number: str, index: dict, company: str = "") -> list:
	"""The active entries a receipt's digits stand for, scoped to a company when given."""
	digits = re.sub(r"\D", "", str(number or ""))
	if len(digits) != 4:
		return []
	entries = index.get(digits) or []
	if company:
		entries = [entry for entry in entries if not entry.get("company") or entry["company"] == company]
	return entries


def resolve_unique(number: str, company: str = "", index: dict | None = None) -> dict | None:
	"""The one entry these digits stand for, or None when unknown or ambiguous."""
	if not str(number or "").strip():
		return None
	entries = resolve(number, load_index(company) if index is None else index, company)
	cards = {(entry["bank_account"], entry["card_last_four"]) for entry in entries}
	return entries[0] if len(cards) == 1 else None


def describe(entry: dict | None) -> dict | None:
	if not entry:
		return None
	return {
		"alias": entry.get("name"),
		"bank_account": entry.get("bank_account"),
		"card_last_four": entry.get("card_last_four"),
		"number": entry.get("number"),
		"kind": entry.get("kind"),
		"label": entry.get("label"),
	}


# ── tools ────────────────────────────────────────────────────────────────────


def _row_out(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"bank_account": row.get("bank_account"),
		"company": row.get("company"),
		"card_last_four": row.get("card_last_four"),
		"number": row.get("number"),
		"kind": row.get("kind"),
		"label": row.get("label") or None,
		"active": bool(row.get("active")),
		"is_device": (row.get("kind") or KIND_CARD) != KIND_CARD,
	}


def _bank_account(args: dict, *, required: bool) -> str:
	from . import read

	value = as_str(args, "bank_account") or as_str(args, "account")
	if not value:
		if required:
			raise ToolError("bank_account is required — a card always draws on one account.")
		return ""
	compat.require_doctype(BANK_ACCOUNT, "It ships with ERPNext's Accounts module.")
	return read._resolve_bank_account(value)


def list_bank_card_aliases(args: dict) -> ToolResult:
	"""Every card and device number registered, grouped by the card it stands for."""
	compat.require_doctype(DOCTYPE, _HINT)
	filters = {}
	bank_account = _bank_account(args, required=False)
	if bank_account:
		filters["bank_account"] = bank_account
	company = as_str(args, "company")
	if company:
		filters["company"] = company
	if not as_bool(args, "include_inactive", False):
		filters["active"] = 1
	rows = frappe.db.get_all(
		DOCTYPE,
		filters=filters,
		fields=compat.existing_fields(DOCTYPE, _FIELDS),
		order_by="bank_account asc, card_last_four asc, number asc",
		limit=as_limit(args),
	)
	cards = {}
	for row in rows:
		key = (row.get("bank_account"), row.get("card_last_four"))
		card = cards.setdefault(
			key,
			{"bank_account": key[0], "card_last_four": key[1], "company": row.get("company"), "numbers": []},
		)
		card["numbers"].append(_row_out(row))
	data = {
		"cards": list(cards.values()),
		"aliases": [_row_out(row) for row in rows],
		"count": len(rows),
		"note": (
			"A receipt printing any of a card's numbers is matched as that card: the bank line "
			"names the physical card, and an Apple Pay / Google Pay slip prints the device's "
			"number instead. Unregistered digits change nothing."
		),
	}
	return ToolResult(data, f"{len(rows)} card number(s) on {len(cards)} card(s)")


def add_bank_card_alias(args: dict) -> ToolResult:
	"""Register a card, or a device number that stands for one.

	Re-adding a removed row reactivates it rather than writing a second one.
	Untagged receipts already carrying the number are tagged in the same call,
	unless `tag_existing` is false, so a register filled after the receipts were
	scanned still answers "which card paid".
	"""
	compat.require_doctype(DOCTYPE, _HINT)
	bank_account = _bank_account(args, required=True)
	card = _digits(as_str(args, "card_last_four") or as_str(args, "card"), "card_last_four")
	number = _digits(as_str(args, "number") or card, "number")
	kind = as_str(args, "kind") or (KIND_CARD if number == card else "Apple Pay")
	if kind not in KINDS:
		raise ToolError(f"kind must be one of: {', '.join(KINDS)} — got {kind!r}. Nothing was written.")
	if kind == KIND_CARD and number != card:
		raise ToolError(
			f"a {KIND_CARD} row is the card itself, so its number is the card's own last four "
			f"({card}), not {number}. A phone's number is kind Apple Pay, Google Pay, Samsung Pay "
			"or Other Device. Nothing was written."
		)
	label = as_str(args, "label")
	company = frappe.db.get_value(BANK_ACCOUNT, bank_account, "company") or ""

	existing = frappe.db.get_all(
		DOCTYPE,
		filters={"bank_account": bank_account, "number": number},
		fields=compat.existing_fields(DOCTYPE, _FIELDS),
		limit=5,
	)
	for row in existing:
		if str(row.get("card_last_four")) != card and row.get("active"):
			raise ToolError(
				f"{number} is already registered on {bank_account} as a number of card "
				f"{row.get('card_last_four')} ({row.get('name')}). Remove that one first if the "
				"phone moved to a different card. Nothing was written."
			)

	same = next((row for row in existing if str(row.get("card_last_four")) == card), None)
	if same and same.get("active"):
		raise ToolError(
			f"{number} is already registered on {bank_account} for card {card} "
			f"({same.get('name')}). Nothing was written."
		)

	values = {"kind": kind, "active": 1, "company": company or None}
	if label:
		values["label"] = label
	if same:
		frappe.db.set_value(DOCTYPE, same["name"], values)
		name, reactivated = same["name"], True
	else:
		doc = frappe.get_doc(
			{
				"doctype": DOCTYPE,
				"bank_account": bank_account,
				"card_last_four": card,
				"number": number,
				"label": label or None,
				**values,
			}
		)
		doc.insert(ignore_permissions=True)
		name, reactivated = doc.name, False

	elsewhere = sorted(
		{
			entry["bank_account"]
			for entry in resolve(number, load_index(company), company)
			if entry["bank_account"] != bank_account
		}
	)
	tagged = []
	if as_bool(args, "tag_existing", True) and not elsewhere:
		tagged = _tag_existing(number, company, name)

	row = frappe.db.get_value(DOCTYPE, name, compat.existing_fields(DOCTYPE, _FIELDS), as_dict=True)
	data = {
		"alias": _row_out(dict(row)),
		"reactivated": reactivated,
		"receipts_tagged": tagged,
		"receipts_tagged_count": len(tagged),
	}
	if elsewhere:
		data["warning"] = (
			f"{number} is also registered on {', '.join(elsewhere)}. Four digits can collide; "
			"while they do, a receipt printing them is not tagged and earns no account penalty."
		)
	what = "card" if kind == KIND_CARD else f"{kind} number {number} for card"
	return ToolResult(
		data,
		f"{'reactivated' if reactivated else 'registered'} {what} {card} on {bank_account}"
		+ (f"; tagged {len(tagged)} receipt(s)" if tagged else ""),
	)


def _tag_existing(number: str, company: str, alias: str) -> list:
	if not compat.has_field(EXPENSE_RECEIPT, RECEIPT_FIELD) or not compat.has_field(
		EXPENSE_RECEIPT, "card_last_four"
	):
		return []
	filters = {"card_last_four": number}
	if company:
		filters["company"] = company
	rows = frappe.db.get_all(EXPENSE_RECEIPT, filters=filters, fields=["name", RECEIPT_FIELD], limit=5000)
	tagged = []
	for row in rows:
		if row.get(RECEIPT_FIELD):
			continue
		frappe.db.set_value(EXPENSE_RECEIPT, row["name"], RECEIPT_FIELD, alias, update_modified=False)
		tagged.append(row["name"])
	return sorted(tagged)


def remove_bank_card_alias(args: dict) -> ToolResult:
	"""Deactivate one row. Tagged receipts keep their link; the matcher stops using it."""
	compat.require_doctype(DOCTYPE, _HINT)
	name = as_str(args, "name") or as_str(args, "alias")
	if not name:
		bank_account = _bank_account(args, required=False)
		number = as_str(args, "number")
		if not (bank_account and number):
			raise ToolError("name the row (name), or give bank_account and number. Nothing was written.")
		matches = frappe.db.get_all(
			DOCTYPE,
			filters={"bank_account": bank_account, "number": _digits(number, "number"), "active": 1},
			pluck="name",
		)
		if not matches:
			raise ToolError(f"no active alias {number} on {bank_account}. Nothing was written.")
		name = matches[0]
	row = frappe.db.get_value(DOCTYPE, name, compat.existing_fields(DOCTYPE, _FIELDS), as_dict=True)
	if not row:
		raise ToolError(f"no {DOCTYPE} named {name!r}. list_bank_card_aliases has them. Nothing was written.")
	if not row.get("active"):
		raise ToolError(
			f"{name} is already inactive. add_bank_card_alias brings it back. Nothing was written."
		)
	frappe.db.set_value(DOCTYPE, name, "active", 0)
	return ToolResult(
		{
			"alias": _row_out({**dict(row), "active": 0}),
			"deleted": False,
			"note": (
				"The row was kept and deactivated: receipts tagged with it still name the card "
				"that paid, and the matcher no longer treats the number as this card."
			),
		},
		f"deactivated {row.get('kind')} number {row.get('number')} for card {row.get('card_last_four')}",
	)
