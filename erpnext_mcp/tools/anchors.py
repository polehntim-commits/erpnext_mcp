# SPDX-License-Identifier: MIT
"""The statement anchor chain: whether a year of bank data is COMPLETE.

v0.73.0, the Bank Bridge consolidation. Everything the app already had about a
bank account answers questions about individual lines — is this withdrawal
allocated, is there a receipt behind it, what kind of expense was it. Not one of
them can answer the question an accountant asks first, which is whether the lines
are ALL OF THEM. A transaction the feed never delivered leaves no row to inspect,
no gap in a sequence, and no trace of any kind in a system that holds only what
arrived.

The statement anchor is the answer, and it is arithmetic rather than inspection:
the bank's opening balance, plus everything on file for the period, should be the
bank's closing balance. Where it is not, the difference IS the missing movement,
to the cent, without anybody knowing what it was. A chain of periods with no gaps
between them extends that from one month to a whole year.

WHY THIS LIVES IN ERPNEXT AND NOT IN THE PIPE THAT PARSES THE STATEMENTS. Two
systems that both hold reconciliation truth is two systems that can disagree, and
when they do there is nothing to say which is right. The pipe keeps doing the
work only it can do — talking to the bank, reading PDFs — and pushes the result
here, where the anchor sits beside the Bank Transactions, the GL entries and the
company it belongs to, and gets exported with them when an entity moves.

THE SIGN CONVENTION IS THE SAME EVERYWHERE IN THIS FILE AND IN THE DOCTYPE.
Positive is money IN. `computed_closing = anchored_opening + transaction_sum`;
`variance = anchored_closing - computed_closing`. ERPNext's Bank Transaction
already signs that way round, so nothing here flips anything.

WHAT NONE OF THESE TOOLS DO. Not one of them posts, allocates, or writes a GL
Entry — the same promise `banking_bridge.py` makes, for the same reason. An
anchor says whether the books tie out. Making them tie out is a Journal Entry
somebody decided on, with its own tool and its own switch.

THE THREE THAT WRITE, and what each is allowed to touch:

  * `set_anchor_variance_reason` writes one prose field. An explained variance is
    worth more than a widened tolerance, because next quarter's advisory fee will
    be a different number and the sentence still fits.
  * `rebuild_anchor_chain` recomputes what is DERIVED — the closing, the
    variance, the reconciled flag, the chain gaps, the line matches. It leaves
    the three ANCHORED numbers alone unless explicitly told otherwise, because
    those came off a statement and this app did not read the statement.
  * `pair_bank_accounts` sets one Link on each of two Bank Accounts.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import getdate

from .. import compat
from ..args import (
	as_bool,
	as_date,
	as_float,
	as_int,
	as_limit,
	as_str,
	resolve_company,
)
from ..errors import ToolError
from ..result import ToolResult

ANCHOR = "Statement Anchor"
ANCHOR_LINE = "Statement Anchor Line"
BANK_ACCOUNT = "Bank Account"
BANK_TRANSACTION = "Bank Transaction"
ADVISORY_AGREEMENT = "Advisory Agreement"
CUSTOM_FIELD = "Custom Field"
GL_ENTRY = "GL Entry"

#: The seven columns this module adds to ERPNext's Bank Account. Custom Fields
#: rather than a doctype of our own, and that is the whole "Account Pairing"
#: design: a pairing is a PROPERTY of an account, not an entity. Two Bank
#: Accounts pointing at each other is the entire relationship, and a separate
#: register of pairs would be a second place for the same fact to be wrong.
PAIRED_FIELD = "paired_bank_account"
PAIRING_TYPE_FIELD = "pairing_type"
PLAID_ID_FIELD = "plaid_account_id"
PLAID_MASK_FIELD = "plaid_account_mask"
PLAID_TYPE_FIELD = "plaid_account_type"
PLAID_SUBTYPE_FIELD = "plaid_account_subtype"
SYNC_FIELD = "sync_enabled"

#: The eighth column, added in v0.74.0, and deliberately NOT part of the gate
#: below. An aggregator issues new account ids when a bank connection is
#: re-linked, so the id on file goes dead and the pipe pushes a different one for
#: the same account; this holds the ids that came before, oldest first. A site
#: that will not take this one column still pairs and still records metadata —
#: it just cannot answer "was this account once known as X", which is worth less
#: than the writes it would otherwise block.
PLAID_ID_HISTORY_FIELD = "plaid_account_id_history"

PAIRING_FIELDS = (
	PAIRED_FIELD,
	PAIRING_TYPE_FIELD,
	PLAID_ID_FIELD,
	PLAID_MASK_FIELD,
	PLAID_TYPE_FIELD,
	PLAID_SUBTYPE_FIELD,
	SYNC_FIELD,
)

#: Written when the column is there and skipped when it is not. See above.
OPTIONAL_PAIRING_FIELDS = (PLAID_ID_HISTORY_FIELD,)

PAIRING_TYPES = ("Brokerage", "Cash Services")

PLAID_ACCOUNT_TYPES = ("investment", "depository", "credit", "loan")

#: How many superseded aggregator ids one account keeps. A re-link happens when a
#: bank changes its auth, which is a handful of times a decade per account, so
#: this is not a number anybody reaches honestly — it is a ceiling on a Small
#: Text column in the case where something re-links in a loop. The OLDEST are
#: dropped: the recent ids are the ones a feed or a support ticket still names.
MAX_ID_HISTORY = 100

#: How far off a period may be and still count as tied out, when nothing says
#: otherwise. A cent. See the doctype field description for why widening this is
#: the wrong fix for a fee that is genuinely not in the feed.
DEFAULT_TOLERANCE = 0.01

#: Ceiling on any single scan. A decade of monthly anchors on fifty accounts is
#: six thousand rows, so this is not a limit anybody reaches by accident.
MAX_SCAN = 5000

#: How close in days and in money a statement line and a Bank Transaction have to
#: be before they are the same movement. A statement prints the posting date and
#: a feed reports the transaction date, and they differ by a day over a weekend —
#: which is why this is three and not zero.
DEFAULT_LINE_DAY_WINDOW = 3
DEFAULT_LINE_AMOUNT_TOLERANCE = 0.01

_ANCHOR_FIELDS = (
	"name",
	"bank_account",
	"company",
	"plaid_account_mask",
	"period_start",
	"period_end",
	"anchored_opening",
	"anchored_closing",
	"transaction_sum",
	"computed_closing",
	"variance",
	"variance_tolerance",
	"variance_reason",
	"reconciled",
	"chain_gap_from_prior",
	"parser_version",
	"mark_to_market_delta",
	"portfolio_opening_value",
	"portfolio_closing_value",
	"statement_pdf",
	"source_statement_id",
	"amended_from",
)

_BANK_ACCOUNT_FIELDS = (
	"name",
	"account_name",
	"bank",
	"company",
	"account",
	"disabled",
	"is_company_account",
	*PAIRING_FIELDS,
	*OPTIONAL_PAIRING_FIELDS,
)

_ANCHOR_HINT = "It ships with erpnext_mcp — run `bench migrate` after installing v0.73.0."


# ── schema: the pairing columns, made to exist ───────────────────────────────


def ensure_pairing_fields() -> bool:
	"""Give Bank Account the eight columns a pairing and a Plaid identity need.

	Same mechanism and same argument as `banking_bridge.ensure_categorization_fields`
	— Custom Fields on somebody else's doctype, which is Frappe's supported way
	for one app to extend another's. The alternative here is an "Account Pairing"
	doctype with one row per pair, which is a shadow of a relationship that is
	already expressible as a Link, and shadows drift.

	Created on first use as well as at install, so a bench that pulled the code
	without running the installer works the first time somebody pairs two
	accounts.

	NEVER RAISES. A site that will not take the fields loses pairing and keeps
	everything else; the tools report `fields_installed: false` with the reason
	rather than pretending the columns are there.

	THE PRESENCE CHECK COVERS EVERY COLUMN THIS FUNCTION CREATES, including the
	optional one. A check that only asked about the gate's seven would return
	early on every site installed before v0.74.0 — which is every site that has
	the id history to keep — and the column would never appear.
	"""
	try:
		if _pairing_fields_present() and _history_field_present():
			return True
		if not compat.doctype_exists(CUSTOM_FIELD) or not compat.doctype_exists(BANK_ACCOUNT):
			return False
	except Exception:
		return False

	specification = (
		{
			"fieldname": PAIRED_FIELD,
			"label": "Paired Bank Account",
			"fieldtype": "Link",
			"options": BANK_ACCOUNT,
			"insert_after": "company",
			"description": (
				"The companion account — a brokerage and the cash-services account its trades settle "
				"through are one relationship viewed from two sides. Both sides carry the link, so "
				"either one answers the question."
			),
		},
		{
			"fieldname": PAIRING_TYPE_FIELD,
			"label": "Pairing Type",
			"fieldtype": "Select",
			"options": "\n" + "\n".join(PAIRING_TYPES),
			"insert_after": PAIRED_FIELD,
			"description": (
				"Which side of the pair this account is. It is what stops a sweep being reconciled as "
				"though it were the trade that caused it."
			),
		},
		{
			"fieldname": PLAID_ID_FIELD,
			"label": "Plaid Account ID",
			"fieldtype": "Data",
			"insert_after": PAIRING_TYPE_FIELD,
			"description": (
				"The aggregator's own identifier for this account. Opaque, and the only thing that "
				"reliably matches a feed to a Bank Account when two accounts share a mask."
			),
		},
		{
			"fieldname": PLAID_MASK_FIELD,
			"label": "Plaid Account Mask",
			"fieldtype": "Data",
			"insert_after": PLAID_ID_FIELD,
			"description": (
				"The last four digits. Not unique and not trusted as an identifier — it is here "
				"because it is what a person has when they ask whether ••6030 tied out."
			),
		},
		{
			"fieldname": PLAID_TYPE_FIELD,
			"label": "Plaid Account Type",
			"fieldtype": "Select",
			"options": "\n" + "\n".join(PLAID_ACCOUNT_TYPES),
			"insert_after": PLAID_MASK_FIELD,
			"description": (
				"investment, depository, credit or loan, as the aggregator classifies it. An "
				"investment account's anchor chain reconciles CASH AND SWEEP only; its portfolio "
				"value moves for reasons no transaction explains."
			),
		},
		{
			"fieldname": PLAID_SUBTYPE_FIELD,
			"label": "Plaid Account Subtype",
			"fieldtype": "Data",
			"insert_after": PLAID_TYPE_FIELD,
			"description": "brokerage, checking, savings — the aggregator's own word for it.",
		},
		{
			"fieldname": SYNC_FIELD,
			"label": "Sync Enabled",
			"fieldtype": "Check",
			"insert_after": PLAID_SUBTYPE_FIELD,
			"description": (
				"Whether the pipe pulls this account. Recorded here so the question 'why has this "
				"account no transactions since March' has an answer that is not a shrug."
			),
		},
		{
			"fieldname": PLAID_ID_HISTORY_FIELD,
			"label": "Plaid Account ID History",
			"fieldtype": "Small Text",
			"insert_after": SYNC_FIELD,
			# Read-only AND hidden, which the other seven are not. This one is
			# machine-maintained bookkeeping rather than something an operator
			# sets: a hand-edited array silently changes what the next push
			# thinks it replaced. It stays readable through `get_account_pairing`
			# and the API, which is where the question "was this account once
			# known as X" actually gets asked from.
			"read_only": 1,
			"hidden": 1,
			"description": (
				"A JSON array of the aggregator ids this account used to have, oldest first, appended "
				"automatically whenever a push carries a different one. A re-link issues new ids for "
				"the same real account, and without this the old id is simply overwritten — after "
				"which nothing connects a year of feed rows, a support ticket or an aggregator's own "
				"logs to the account they belong to. Maintained by the push endpoints; edit it by "
				"hand and the next push will not know what it replaced."
			),
		},
	)

	created = False
	for field in specification:
		try:
			if compat.has_field(BANK_ACCOUNT, field["fieldname"]):
				continue
			if frappe.db.exists(CUSTOM_FIELD, {"dt": BANK_ACCOUNT, "fieldname": field["fieldname"]}):
				continue
			doc = frappe.new_doc(CUSTOM_FIELD)
			doc.dt = BANK_ACCOUNT
			for key, value in field.items():
				doc.set(key, value)
			doc.insert(ignore_permissions=True)
			created = True
		except Exception:
			frappe.log_error(
				title=f"erpnext_mcp: could not add {field['fieldname']} to Bank Account",
				message=compat.traceback_text(),
			)
			return False

	if created:
		try:
			frappe.clear_cache(doctype=BANK_ACCOUNT)
		except Exception:
			pass
	return _pairing_fields_present()


def _pairing_fields_present() -> bool:
	try:
		return all(compat.has_field(BANK_ACCOUNT, fieldname) for fieldname in PAIRING_FIELDS)
	except Exception:
		return False


def _history_field_present() -> bool:
	try:
		return compat.has_field(BANK_ACCOUNT, PLAID_ID_HISTORY_FIELD)
	except Exception:
		return False


# ── schema: the aggregator ids this account used to have ────────────────────


def id_history(value) -> list:
	"""The stored id history as a list of ids, whatever shape it is in on a site.

	TOLERANT ON THE WAY IN, because the alternative to tolerance in this one
	place is deleting the record the column exists to keep. A value that is not
	JSON is read as a single legacy id rather than as nothing: somebody who typed
	a dead id into the field by hand answered the same question this field asks,
	and parsing it to `[]` would erase that on the next push.

	Public because `_pairing_out` reads it and the push endpoints write it, and a
	second parser for the same column would eventually disagree with this one.
	"""
	if value in (None, ""):
		return []
	if isinstance(value, (list, tuple)):
		items = list(value)
	else:
		text = str(value).strip()
		if not text:
			return []
		try:
			parsed = json.loads(text)
		except ValueError:
			parsed = [text]
		items = parsed if isinstance(parsed, list) else [parsed]

	out: list = []
	for item in items:
		entry = str(item or "").strip()
		if entry and entry not in out:
			out.append(entry)
	return out


def id_history_update(current: dict, new_id: str, provided=None) -> dict:
	"""`{field: json}` when the history on file should change, `{}` otherwise.

	WHAT THIS IS FOR. An aggregator issues fresh account ids when a bank
	connection is re-linked, so the id ERPNext holds goes dead and the next sync
	pushes a different one for the same real account. Overwriting is correct —
	the new id is the live one — but overwriting ALONE loses the only handle that
	ties a year of already-stored feed rows, an aggregator's support logs and the
	pipe's own history to this account. So the id it replaces is appended here
	first, in the SAME write, because a push interrupted between two writes would
	leave the new id recorded and its predecessor gone.

	TWO SOURCES, AND NEITHER IS TRUSTED TO BE COMPLETE. `provided` is the chain
	the pipe believes in, which reaches back before this site was ever told about
	the account and is the only place the early ids can come from; the observed
	half is what this site watched happen and is the only thing that still works
	when the pipe sends nothing. They are UNIONED rather than one replacing the
	other — a pipe pushing a short chain must not truncate ids this site saw
	itself, and a site that missed a re-link (it happened between syncs, or while
	the column did not exist) must not stay ignorant of ids the pipe kept. Order
	is oldest-first with `provided` laid down first, since the pipe's chain
	predates anything observed here.

	Nothing is recorded when the result matches what is already stored — which
	covers the first push ever, every ordinary sync, and a pipe re-sending the
	same chain — or when the site has no history column. The result is
	idempotent: pushing the same id and the same chain all day appends once.
	"""
	if not _history_field_present():
		return {}

	stored = id_history(current.get(PLAID_ID_HISTORY_FIELD))
	previous = str(current.get(PLAID_ID_FIELD) or "").strip()
	live = str(new_id or "").strip() or previous

	history = id_history(provided) if provided not in (None, "") else []
	for entry in stored:
		if entry not in history:
			history.append(entry)
	# The id being retired by THIS push, appended last because it is the most
	# recent thing to stop being current.
	if previous and live and previous != live and previous not in history:
		history.append(previous)

	# A re-link can land back on a connection this account already had, which
	# makes an id in the history current again. It comes out of the history when
	# that happens: an id that is both current and superseded reads as two
	# accounts to anything matching on it.
	history = [entry for entry in history if entry != live]
	if len(history) > MAX_ID_HISTORY:
		history = history[-MAX_ID_HISTORY:]

	if history == stored:
		return {}
	return {PLAID_ID_HISTORY_FIELD: json.dumps(history)}


# ── shared resolvers and shapes ──────────────────────────────────────────────


def _require_anchors() -> None:
	compat.require_doctype(ANCHOR, _ANCHOR_HINT)


def _require_bank_account() -> None:
	compat.require_doctype(BANK_ACCOUNT, "It ships with ERPNext's Accounts module.")


def _tolerance(args: dict, fallback: float = DEFAULT_TOLERANCE) -> float:
	value = args.get("tolerance")
	if value in (None, ""):
		return fallback
	return abs(as_float(value, "tolerance"))


def _resolve_bank_account(args: dict, *, required: bool = False, key: str = "bank_account") -> str:
	"""One Bank Account, named directly or found by its four-digit mask.

	THE MASK IS A CONVENIENCE AND IS REFUSED WHEN IT IS AMBIGUOUS. Two accounts
	at two banks can end in the same four digits, and answering "does ••6030
	reconcile" from whichever one sorted first would be a wrong answer that looks
	exactly like a right one. Where the mask matches more than one account the
	refusal names them all, so the caller can pass a docname.
	"""
	named = as_str(args, key)
	if named:
		if not frappe.db.exists(BANK_ACCOUNT, named):
			mask_hit = accounts_by_mask(named, resolve_company(as_str(args, "company")))
			if len(mask_hit) == 1:
				return mask_hit[0]
			if len(mask_hit) > 1:
				raise ToolError(
					f"{named!r} is not a Bank Account docname, and {len(mask_hit)} accounts carry it as "
					f"a mask: {', '.join(mask_hit)}. Name one of them."
				)
			raise ToolError(f"no Bank Account named {named!r} on this site, and no account has that mask.")
		return named

	# The mask fallback belongs to the PRIMARY account argument only. A caller
	# who left `paired_bank_account` empty has named one account, and resolving
	# the second from the first's mask would pair an account with itself under a
	# different name.
	mask = (as_str(args, "plaid_account_mask") or as_str(args, "mask")) if key == "bank_account" else ""
	if mask:
		hits = accounts_by_mask(mask, resolve_company(as_str(args, "company")))
		if not hits:
			raise ToolError(
				f"no Bank Account carries the mask {mask!r}. get_account_pairing lists the accounts "
				"this site knows about, with their masks."
			)
		if len(hits) > 1:
			raise ToolError(
				f"{len(hits)} accounts carry the mask {mask!r}: {', '.join(hits)}. Name one of them as "
				"bank_account — a reconciliation answer for the wrong account looks exactly like a "
				"right one."
			)
		return hits[0]

	if required:
		raise ToolError(
			"bank_account is required — a docname, or a four-digit mask as plaid_account_mask. "
			"get_account_pairing lists what this site has."
		)
	return ""


def accounts_by_mask(mask: str, company: str = "") -> list:
	"""Bank Accounts whose stored mask is `mask`. Empty when the column is absent.

	Public because `advisory.create_advisory_agreement` resolves an account the
	same way this module does, and two implementations of "which account is
	••6030" is two answers to it.
	"""
	wanted = str(mask or "").strip()
	if not wanted or not compat.has_field(BANK_ACCOUNT, PLAID_MASK_FIELD):
		return []
	filters = {PLAID_MASK_FIELD: wanted}
	if company:
		filters["company"] = company
	rows = frappe.db.get_all(BANK_ACCOUNT, filters=filters, fields=["name"], limit=MAX_SCAN)
	return [row["name"] for row in rows]


def _bank_account_row(name: str) -> dict:
	fields = compat.existing_fields(BANK_ACCOUNT, list(_BANK_ACCOUNT_FIELDS))
	row = frappe.db.get_value(BANK_ACCOUNT, name, fields, as_dict=True)
	return dict(row) if row else {}


def _anchor_rows(filters: dict, *, order_by: str = "period_start asc", limit: int = MAX_SCAN) -> list:
	fields = compat.existing_fields(ANCHOR, list(_ANCHOR_FIELDS))
	rows = frappe.db.get_all(ANCHOR, filters=filters, fields=fields, order_by=order_by, limit=limit)
	return [dict(row) for row in rows]


def _anchor_out(row: dict) -> dict:
	"""One anchor as a caller reads it, with every derived number spelled out."""
	return {
		"name": row.get("name"),
		"bank_account": row.get("bank_account"),
		"company": row.get("company"),
		"plaid_account_mask": row.get("plaid_account_mask") or None,
		"period_start": _date_text(row.get("period_start")),
		"period_end": _date_text(row.get("period_end")),
		"anchored_opening": _money(row.get("anchored_opening")),
		"transaction_sum": _money(row.get("transaction_sum")),
		"computed_closing": _money(row.get("computed_closing")),
		"anchored_closing": _money(row.get("anchored_closing")),
		"variance": _money(row.get("variance")),
		"variance_tolerance": _money(row.get("variance_tolerance") or DEFAULT_TOLERANCE),
		"variance_reason": row.get("variance_reason") or None,
		"reconciled": bool(int(row.get("reconciled") or 0)),
		"chain_gap_from_prior": bool(int(row.get("chain_gap_from_prior") or 0)),
		"parser_version": row.get("parser_version") or None,
		"mark_to_market_delta": _optional_money(row.get("mark_to_market_delta")),
		"portfolio_opening_value": _optional_money(row.get("portfolio_opening_value")),
		"portfolio_closing_value": _optional_money(row.get("portfolio_closing_value")),
		"statement_pdf": row.get("statement_pdf") or None,
		"source_statement_id": int(row.get("source_statement_id") or 0) or None,
		"amended_from": row.get("amended_from") or None,
	}


def _money(value) -> float:
	return round(float(value or 0), 2)


def _optional_money(value):
	"""Null stays null. A brokerage figure of zero and one nobody supplied are
	different facts, and reporting the second as 0.00 invents a flat quarter."""
	if value in (None, ""):
		return None
	return round(float(value), 2)


def _date_text(value) -> str | None:
	if not value:
		return None
	if hasattr(value, "isoformat"):
		return value.isoformat()[:10]
	return str(value)[:10]


def _apply_date_range(filters: dict, fieldname: str, from_date, to_date) -> None:
	if from_date and to_date:
		filters[fieldname] = ("between", [from_date, to_date])
	elif from_date:
		filters[fieldname] = (">=", from_date)
	elif to_date:
		filters[fieldname] = ("<=", to_date)


def _anchor_filters(args: dict, *, bank_account: str = "", company: str = "") -> dict:
	filters = {}
	if bank_account:
		filters["bank_account"] = bank_account
	elif company and compat.has_field(ANCHOR, "company"):
		filters["company"] = company
	_apply_date_range(filters, "period_start", as_date(args, "from_date"), as_date(args, "to_date"))
	return filters


def _resolve_anchor(args: dict) -> dict:
	"""One anchor, by docname or by (account, period). Refused when it is neither."""
	named = as_str(args, "anchor") or as_str(args, "statement_anchor")
	if named:
		row = frappe.db.get_value(
			ANCHOR, named, compat.existing_fields(ANCHOR, list(_ANCHOR_FIELDS)), as_dict=True
		)
		if not row:
			raise ToolError(f"no Statement Anchor named {named!r} on this site.")
		return dict(row)

	bank_account = _resolve_bank_account(args, required=True)
	period_start = as_date(args, "period_start")
	period_end = as_date(args, "period_end")
	if not period_start and not period_end:
		raise ToolError(
			"name an anchor, or give bank_account with period_start and period_end. "
			"get_statement_anchor_chain lists the periods this account has."
		)
	filters = {"bank_account": bank_account}
	if period_start:
		filters["period_start"] = period_start
	if period_end:
		filters["period_end"] = period_end
	rows = _anchor_rows(filters, limit=5)
	if not rows:
		raise ToolError(
			f"{bank_account} has no anchor for that period. get_statement_anchor_chain lists the "
			"periods it does have."
		)
	if len(rows) > 1:
		raise ToolError(
			f"{len(rows)} anchors on {bank_account} match that period. Give both period_start and "
			"period_end, or name the anchor."
		)
	return rows[0]


# ── transactions inside a period ─────────────────────────────────────────────

_TRANSACTION_FIELDS = (
	"name",
	"date",
	"description",
	"reference_number",
	"bank_party_name",
	"party_type",
	"party",
	"status",
	"docstatus",
	# Both money layouts are asked for and filtered out by `existing_fields`
	# where they are absent, so one query serves a site that stores a signed
	# amount and one that stores a deposit/withdrawal pair.
	"deposit",
	"withdrawal",
	"amount",
	"allocated_amount",
	"unallocated_amount",
)


def _period_transactions(bank_account: str, period_start, period_end) -> list:
	"""Every Bank Transaction on the account inside the period, signed.

	Cancelled transactions are excluded (`docstatus < 2`), because a cancelled
	movement did not happen and counting it would produce a variance that
	describes nothing.
	"""
	if not compat.doctype_exists(BANK_TRANSACTION):
		return []
	amount_fields = compat.bank_transaction_amount_fields()
	fields = compat.existing_fields(BANK_TRANSACTION, list(_TRANSACTION_FIELDS))
	extra = compat.existing_fields(BANK_TRANSACTION, ["farm_category", "farm_expense_account"])
	filters = {"bank_account": bank_account, "docstatus": ("<", 2)}
	_apply_date_range(filters, "date", period_start, period_end)
	rows = frappe.db.get_all(
		BANK_TRANSACTION,
		filters=filters,
		fields=sorted(set(fields + extra)),
		order_by="date asc, name asc",
		limit=MAX_SCAN,
	)
	out = []
	for row in rows:
		row = dict(row)
		signed = compat.signed_amount(row, amount_fields)
		out.append(
			{
				"name": row.get("name"),
				"date": _date_text(row.get("date")),
				"description": row.get("description"),
				"reference_number": row.get("reference_number") or None,
				"party": row.get("party") or None,
				"amount_signed": round(signed, 2),
				"gross_amount": round(abs(signed), 2),
				"direction": "Deposit" if signed >= 0 else "Withdrawal",
				"category": row.get("farm_category") or None,
				"expense_account": row.get("farm_expense_account") or None,
				"allocated_amount": _money(row.get("allocated_amount")),
				"unallocated_amount": _money(row.get("unallocated_amount")),
			}
		)
	return out


def _statement_lines(anchor_name: str) -> list:
	"""The statement's own lines on one anchor, or an empty list when it has none."""
	if not compat.doctype_exists(ANCHOR):
		return []
	try:
		doc = frappe.get_doc(ANCHOR, anchor_name)
	except Exception:
		return []
	out = []
	for row in doc.get("statement_lines") or []:
		row = dict(row)
		amount = round(float(row.get("amount") or 0), 2)
		out.append(
			{
				"idx": int(row.get("idx") or 0),
				"row_name": row.get("name"),
				"line_date": _date_text(row.get("line_date")),
				"description": row.get("description"),
				"reference": row.get("reference") or None,
				"amount": amount,
				"line_type": row.get("line_type") or None,
				"matched_bank_transaction": row.get("matched_bank_transaction") or None,
			}
		)
	return out


def _match_lines(lines: list, transactions: list, *, day_window: int, amount_tolerance: float) -> dict:
	"""Pair each statement line with at most one Bank Transaction. Greedy, by design.

	NEAREST-IN-TIME AMONG EXACT-ENOUGH AMOUNTS, and each transaction is consumed
	once. Two identical $184.62 fuel purchases in one week are the ordinary case
	on a farm, and a matcher that let one transaction satisfy both lines would
	report the account complete while a movement was genuinely missing — which is
	the one failure this whole module exists to catch.

	It writes nothing. A match here is an OPINION about which line is which
	transaction; the only fact it produces is the count of lines that no
	transaction can explain.
	"""
	available = {row["name"]: row for row in transactions}
	matched, unmatched = [], []
	for line in lines:
		candidates = []
		for name, row in available.items():
			if abs(round(row["amount_signed"] - line["amount"], 2)) > amount_tolerance:
				continue
			gap = _day_gap(line.get("line_date"), row.get("date"))
			if gap is None or gap > day_window:
				continue
			candidates.append((gap, name))
		if not candidates:
			unmatched.append(line)
			continue
		candidates.sort()
		_, winner = candidates[0]
		matched.append({**line, "bank_transaction": winner, "day_gap": candidates[0][0]})
		available.pop(winner, None)
	return {
		"matched": matched,
		"unmatched_lines": unmatched,
		"transactions_without_a_line": list(available.values()) if lines else [],
	}


def _set_on_row(row, values: dict) -> None:
	"""Write fields onto a child row, whichever shape the framework handed us.

	A child row is a Document with `.set()` on a real bench and a plain mapping
	on some code paths. Assigning attributes works on one and silently fails on
	the other — silently, because the failure is an AttributeError inside a
	`try` that exists to keep a match from taking a rebuild down. So: `.set()`
	where it exists, `update` where it does not.
	"""
	setter = getattr(row, "set", None)
	if callable(setter):
		for key, value in values.items():
			setter(key, value)
		return
	row.update(values)


def _day_gap(left, right):
	if not left or not right:
		return None
	try:
		return abs((getdate(left) - getdate(right)).days)
	except Exception:
		return None


# ── 1. get_statement_anchor_chain ────────────────────────────────────────────


def get_statement_anchor_chain(args: dict) -> ToolResult:
	"""Every anchored period on one account, in order, with the chain checked.

	IN PERIOD ORDER RATHER THAN BY VARIANCE, deliberately: the list IS the chain,
	and reading it top to bottom is how somebody finds the month where it broke.
	`list_unreconciled_anchors` is the other ordering, for the other question.

	`cumulative_variance` is the number worth reading first. A chain whose periods
	each vary by a few hundred dollars in alternating directions is a timing
	difference; one whose cumulative variance grows every month is a recurring
	charge nobody has booked — and the two look identical period by period.
	"""
	_require_anchors()
	company = resolve_company(as_str(args, "company"))
	bank_account = _resolve_bank_account(args, required=False)
	if not bank_account and not company:
		raise ToolError(
			"name a bank_account (or a plaid_account_mask), or a company to read every account's "
			"chain. Reading every anchor on the site at once is not a reconciliation question."
		)

	filters = _anchor_filters(args, bank_account=bank_account, company=company or "")
	limit = as_limit(args)
	rows = _anchor_rows(filters, order_by="bank_account asc, period_start asc", limit=limit)
	anchors = [_anchor_out(row) for row in rows]

	cumulative = 0.0
	for anchor in anchors:
		cumulative = round(cumulative + anchor["variance"], 2)
		anchor["cumulative_variance"] = cumulative

	gaps = [anchor for anchor in anchors if anchor["chain_gap_from_prior"]]
	unreconciled = [anchor for anchor in anchors if not anchor["reconciled"]]
	unexplained = [anchor for anchor in unreconciled if not anchor["variance_reason"]]

	data = {
		"bank_account": bank_account or None,
		"company": company,
		"account": _pairing_out(_bank_account_row(bank_account)) if bank_account else None,
		"from_date": as_date(args, "from_date"),
		"to_date": as_date(args, "to_date"),
		"anchors": anchors,
		"count": len(anchors),
		"limit": limit,
		"truncated": len(anchors) == limit,
		"cumulative_variance": cumulative,
		"reconciled_count": len(anchors) - len(unreconciled),
		"unreconciled_count": len(unreconciled),
		"unexplained_count": len(unexplained),
		"chain_gaps": [
			{
				"anchor": anchor["name"],
				"period_start": anchor["period_start"],
				"anchored_opening": anchor["anchored_opening"],
			}
			for anchor in gaps
		],
		"chain_gap_count": len(gaps),
		"sign_convention": (
			"Positive is money IN. computed_closing = anchored_opening + transaction_sum; "
			"variance = anchored_closing - computed_closing. A POSITIVE variance means the bank has "
			"more than the transactions on file account for, so something that came IN is missing."
		),
		"note": (
			"Listed in period order, because the list is the chain. A CHAIN GAP is a period whose "
			"opening balance is not the prior period's closing balance — that is a missing STATEMENT, "
			"which no amount of per-transaction checking will find."
		),
	}
	if gaps:
		data["warning"] = (
			f"{len(gaps)} period(s) do not follow the one before them. Load the missing statement "
			"before reading anything into the variances after the gap — a chain that is broken at "
			"March says nothing reliable about April."
		)
	if unexplained:
		data["next_step"] = (
			f"{len(unexplained)} period(s) are out of tolerance with no explanation on file. "
			"get_anchor_variance_breakdown says what moved in one of them; "
			"set_anchor_variance_reason records the answer so nobody works it out twice."
		)

	label = bank_account or company or "every account"
	return ToolResult(
		data, f"{len(anchors)} anchored period(s) for {label}, cumulative variance {cumulative}"
	)


# ── 2. list_unreconciled_anchors ─────────────────────────────────────────────


def list_unreconciled_anchors(args: dict) -> ToolResult:
	"""Every period that does not tie out, worst first.

	ORDERED BY THE SIZE OF THE VARIANCE AND NOT BY DATE, which is the opposite of
	the chain view and is the point: this is a worklist. The sort happens in
	Python because the ordering is on the ABSOLUTE variance and a database that
	sorted on the signed one would put the largest overstatement and the largest
	understatement at opposite ends of the list.

	`explained` and `unexplained` are counted separately and both are returned. A
	period with a variance_reason is not a problem — it is a known, recorded fact
	about an account, and hiding it would make somebody rediscover it every
	quarter.
	"""
	_require_anchors()
	company = resolve_company(as_str(args, "company"))
	bank_account = _resolve_bank_account(args, required=False)
	tolerance = args.get("tolerance")
	explained = as_bool(args, "include_explained", True)

	filters = _anchor_filters(args, bank_account=bank_account, company=company or "")
	rows = _anchor_rows(filters, order_by="period_start asc", limit=MAX_SCAN)

	out = []
	for row in rows:
		anchor = _anchor_out(row)
		# An explicit tolerance re-judges the row rather than filtering on the
		# stored flag: an operator asking "what is off by more than a hundred
		# dollars" is asking a different question from the one the record was
		# saved with, and answering it from `reconciled` would answer the first.
		if tolerance not in (None, ""):
			ceiling = abs(as_float(tolerance, "tolerance"))
			if abs(anchor["variance"]) <= ceiling:
				continue
		elif anchor["reconciled"]:
			continue
		if not explained and anchor["variance_reason"]:
			continue
		out.append(anchor)

	out.sort(key=lambda anchor: (-abs(anchor["variance"]), anchor["period_start"] or ""))
	limit = as_limit(args)
	shown = out[:limit]

	total = round(sum(anchor["variance"] for anchor in out), 2)
	without_reason = [anchor for anchor in out if not anchor["variance_reason"]]
	by_account: dict = {}
	for anchor in out:
		bucket = by_account.setdefault(
			anchor["bank_account"], {"periods": 0, "total_variance": 0.0, "unexplained": 0}
		)
		bucket["periods"] += 1
		bucket["total_variance"] = round(bucket["total_variance"] + anchor["variance"], 2)
		bucket["unexplained"] += 0 if anchor["variance_reason"] else 1

	data = {
		"company": company,
		"bank_account": bank_account or None,
		"tolerance": abs(as_float(tolerance, "tolerance")) if tolerance not in (None, "") else None,
		"anchors": shown,
		"count": len(shown),
		"matching": len(out),
		"truncated": len(out) > len(shown),
		"total_variance": total,
		"unexplained_count": len(without_reason),
		"by_account": by_account,
		"note": (
			"Worst first by ABSOLUTE variance, which is the worklist ordering — "
			"get_statement_anchor_chain is the same data in period order, which is how you find the "
			"month it broke. A period with a variance_reason is not a problem: it is a recorded fact, "
			"and it is listed so nobody works it out a second time."
		),
	}
	if without_reason:
		data["next_step"] = (
			f"{len(without_reason)} period(s) have no explanation. Run "
			"get_anchor_variance_breakdown on the largest, then record what you find with "
			"set_anchor_variance_reason."
		)

	return ToolResult(
		data,
		f"{len(out)} unreconciled period(s)" + (f" for {company}" if company else "") + f", net {total}",
	)


# ── 3. get_anchor_variance_breakdown ─────────────────────────────────────────


def get_anchor_variance_breakdown(args: dict) -> ToolResult:
	"""One period, taken apart: what the statement says, what is on file, and the gap.

	  THREE NUMBERS THAT ARE ROUTINELY CONFUSED, reported separately and never
	  added:

	* `transaction_sum` — what the ANCHOR says moved, off the statement.
	* `ledger_transaction_sum` — what the Bank Transactions on file actually
	  add up to for the same period.
	* `variance` — the gap between the statement's own opening and closing
	  balances and its own transaction sum.

	  When the first two disagree, the feed and the statement disagree about the
	  period and the anchor is only as good as whichever produced it. When they
	  agree and the variance is still non-zero, the statement disagrees with
	  ITSELF — which is what an advisory fee deducted outside the transaction list
	  looks like.
	"""
	_require_anchors()
	anchor_row = _resolve_anchor(args)
	anchor = _anchor_out(anchor_row)
	transactions = _period_transactions(anchor["bank_account"], anchor["period_start"], anchor["period_end"])
	ledger_sum = round(sum(row["amount_signed"] for row in transactions), 2)
	feed_gap = round(anchor["transaction_sum"] - ledger_sum, 2)

	deposits = [row for row in transactions if row["amount_signed"] >= 0]
	withdrawals = [row for row in transactions if row["amount_signed"] < 0]

	lines = _statement_lines(anchor["name"])
	line_report = _match_lines(
		lines,
		transactions,
		day_window=as_int(args, "day_window", DEFAULT_LINE_DAY_WINDOW),
		amount_tolerance=_tolerance(args, DEFAULT_LINE_AMOUNT_TOLERANCE),
	)

	limit = as_limit(args)
	data = {
		"anchor": anchor,
		"transactions": transactions[:limit],
		"transaction_count": len(transactions),
		"truncated": len(transactions) > limit,
		"ledger_transaction_sum": ledger_sum,
		"anchored_transaction_sum": anchor["transaction_sum"],
		"feed_vs_statement_gap": feed_gap,
		"deposit_total": round(sum(row["amount_signed"] for row in deposits), 2),
		"withdrawal_total": round(sum(row["amount_signed"] for row in withdrawals), 2),
		"deposit_count": len(deposits),
		"withdrawal_count": len(withdrawals),
		"statement_lines_on_file": len(lines),
		"unmatched_statement_lines": line_report["unmatched_lines"],
		"unmatched_statement_line_count": len(line_report["unmatched_lines"]),
		"largest_transactions": sorted(transactions, key=lambda row: -row["gross_amount"])[:5],
		"diagnosis": _diagnose(anchor, feed_gap, line_report["unmatched_lines"]),
		"note": (
			"Three sums, never added together. `anchored_transaction_sum` is what the statement said "
			"moved; `ledger_transaction_sum` is what the Bank Transactions on file add up to; "
			"`variance` is the statement disagreeing with its own opening and closing balances. They "
			"fail for different reasons and only one of them is a missing transaction."
		),
	}
	if not transactions:
		data["warning"] = (
			f"No Bank Transactions at all on {anchor['bank_account']} between {anchor['period_start']} "
			f"and {anchor['period_end']}. Either the feed has not run for this period or the account "
			"is not the one the statement belongs to."
		)

	return ToolResult(
		data,
		f"{anchor['bank_account']} {anchor['period_start']}–{anchor['period_end']}: variance "
		f"{anchor['variance']}, {len(transactions)} transaction(s) on file",
	)


def _diagnose(anchor: dict, feed_gap: float, unmatched_lines: list) -> str:
	"""A sentence saying which of the failures this period looks like.

	PROSE AND NOT A CODE, because it is a hypothesis rather than a finding. Every
	number it is drawn from is in the payload beside it, so a reader who
	disagrees has everything needed to say so.
	"""
	tolerance = anchor.get("variance_tolerance") or DEFAULT_TOLERANCE
	if abs(anchor["variance"]) <= tolerance and abs(feed_gap) <= tolerance:
		return "This period ties out, and the transactions on file agree with the statement's own sum."
	if abs(feed_gap) > tolerance:
		direction = "more" if feed_gap > 0 else "less"
		return (
			f"The statement says {abs(feed_gap)} {direction} moved than the Bank Transactions on file "
			"add up to. That is a FEED problem, not a bookkeeping one: the two records of the same "
			"period disagree before any variance is computed."
		)
	if unmatched_lines:
		return (
			f"{len(unmatched_lines)} line(s) printed on the statement have no Bank Transaction behind "
			"them. Those are the missing movements, by name and amount."
		)
	if anchor["variance"] > 0:
		return (
			"The bank holds more than the transactions on file account for, so something that came IN "
			"is missing — interest, a dividend, or a deposit the feed did not carry."
		)
	return (
		"The bank holds less than the transactions on file account for, so something that went OUT is "
		"missing — a fee deducted outside the transaction list is the usual one on a managed account."
	)


# ── 4. list_unmatched_statement_lines ────────────────────────────────────────


def list_unmatched_statement_lines(args: dict) -> ToolResult:
	"""Statement lines with no Bank Transaction behind them. Read-only.

	THE ONE LIST THAT NAMES A MISSING TRANSACTION. A variance says how much is
	missing; this says what it was — the date the bank printed, the memo, the
	amount. It can only answer where a parser has pushed the statement's own
	lines onto the anchor; where it has not, the count of anchors without lines
	is reported rather than an empty list, because "nothing is missing" and "we
	have nothing to check against" are opposite answers.

	It writes nothing, including the match it works out. See `rebuild_anchor_chain`
	for the tool that persists a match onto the line.
	"""
	_require_anchors()
	company = resolve_company(as_str(args, "company"))
	bank_account = _resolve_bank_account(args, required=False)
	if not bank_account and not company:
		raise ToolError("name a bank_account (or a plaid_account_mask), or a company.")

	filters = _anchor_filters(args, bank_account=bank_account, company=company or "")
	rows = _anchor_rows(filters, order_by="period_start asc", limit=MAX_SCAN)
	day_window = as_int(args, "day_window", DEFAULT_LINE_DAY_WINDOW)
	amount_tolerance = _tolerance(args, DEFAULT_LINE_AMOUNT_TOLERANCE)

	unmatched, orphan_transactions = [], []
	anchors_with_lines, anchors_without_lines = 0, 0
	for row in rows:
		anchor = _anchor_out(row)
		lines = _statement_lines(anchor["name"])
		if not lines:
			anchors_without_lines += 1
			continue
		anchors_with_lines += 1
		transactions = _period_transactions(
			anchor["bank_account"], anchor["period_start"], anchor["period_end"]
		)
		report = _match_lines(lines, transactions, day_window=day_window, amount_tolerance=amount_tolerance)
		for line in report["unmatched_lines"]:
			unmatched.append(
				{
					**line,
					"anchor": anchor["name"],
					"bank_account": anchor["bank_account"],
					"period_start": anchor["period_start"],
					"period_end": anchor["period_end"],
				}
			)
		for transaction in report["transactions_without_a_line"]:
			orphan_transactions.append(
				{
					**transaction,
					"anchor": anchor["name"],
					"bank_account": anchor["bank_account"],
				}
			)

	unmatched.sort(key=lambda line: -abs(line["amount"]))
	limit = as_limit(args)
	data = {
		"company": company,
		"bank_account": bank_account or None,
		"unmatched_lines": unmatched[:limit],
		"count": min(len(unmatched), limit),
		"matching": len(unmatched),
		"truncated": len(unmatched) > limit,
		"total_unmatched_amount": round(sum(line["amount"] for line in unmatched), 2),
		"transactions_without_a_statement_line": orphan_transactions[:limit],
		"transactions_without_a_statement_line_count": len(orphan_transactions),
		"anchors_with_lines": anchors_with_lines,
		"anchors_without_lines": anchors_without_lines,
		"day_window": day_window,
		"amount_tolerance": amount_tolerance,
		"note": (
			"A line here is a movement the BANK printed and the feed never delivered. The reverse "
			"list — transactions with no statement line — is the other failure and is reported "
			"apart from it: one is a gap in the feed, the other is a transaction that should not "
			"be on the account at all."
		),
	}
	if anchors_without_lines and not anchors_with_lines:
		data["warning"] = (
			f"None of the {anchors_without_lines} anchor(s) in scope carry statement lines, so "
			"nothing here was checked against anything. An empty list is NOT a clean result. Push "
			"parsed statement lines with push_statement_anchor, or read the variance instead — "
			"get_anchor_variance_breakdown says how much is missing without saying what."
		)

	return ToolResult(
		data,
		f"{len(unmatched)} statement line(s) with no transaction behind them across "
		f"{anchors_with_lines} anchored period(s)",
	)


# ── 5. set_anchor_variance_reason ────────────────────────────────────────────


def set_anchor_variance_reason(args: dict) -> ToolResult:
	"""Record why a period does not tie out. MUTATING, and only this one field.

	WHY A SENTENCE BEATS A WIDER TOLERANCE. A managed account whose quarterly
	advisory fee never appears in the bank feed is out by a different number every
	quarter. A tolerance wide enough to swallow this quarter's fee is wide enough
	to swallow next quarter's genuinely missing deposit; the sentence stays true
	and stops hiding nothing.

	`db.set_value` rather than a save, so recording an explanation cannot fail on
	an anchor whose Bank Account somebody has since disabled.
	"""
	_require_anchors()
	anchor = _anchor_out(_resolve_anchor(args))
	reason = as_str(args, "variance_reason") or as_str(args, "reason")
	clear = bool(as_bool(args, "clear", False))
	if not reason and not clear:
		raise ToolError(
			"variance_reason is required — a sentence a person can read, e.g. 'Quarterly advisory "
			"fee 3774.81, deducted outside the transaction feed'. Pass clear=true to remove an "
			"explanation instead. Nothing was written."
		)
	if reason and clear:
		raise ToolError("clear=true removes the explanation; passing one as well is a contradiction.")

	previous = anchor["variance_reason"]
	frappe.db.set_value(ANCHOR, anchor["name"], "variance_reason", None if clear else reason)

	data = {
		"anchor": anchor["name"],
		"bank_account": anchor["bank_account"],
		"period_start": anchor["period_start"],
		"period_end": anchor["period_end"],
		"variance": anchor["variance"],
		"variance_reason": None if clear else reason,
		"previous_variance_reason": previous,
		"reconciled": anchor["reconciled"],
		"note": (
			"An explanation does not make a period reconciled and is not meant to. `reconciled` is "
			"arithmetic — abs(variance) within tolerance — and this is the record of a human "
			"judgement beside it. Both are reported; neither overwrites the other."
		),
	}
	if previous and not clear:
		data["warning"] = f"replaced the previous explanation: {previous!r}"

	return ToolResult(
		data,
		("cleared the explanation on " if clear else "explained ")
		+ f"{anchor['name']} ({anchor['bank_account']} {anchor['period_start']}–{anchor['period_end']}, "
		f"variance {anchor['variance']})",
		docstatus_delta="none (one field on an existing anchor)",
	)


# ── 6. rebuild_anchor_chain ──────────────────────────────────────────────────


def rebuild_anchor_chain(args: dict) -> ToolResult:
	"""Recompute every DERIVED number on an account's chain, in period order.

	WHAT IT RECOMPUTES: `computed_closing`, `variance`, `reconciled`,
	`chain_gap_from_prior`, and the Bank Transaction each statement line matches.
	All five are functions of data already on the record, so recomputing them
	cannot lose anything — and a chain built one anchor at a time gets the gap
	flags wrong whenever a statement arrives out of order, which is what this
	exists to fix.

	WHAT IT LEAVES ALONE: `anchored_opening`, `anchored_closing` and
	`transaction_sum`. Those three came off a bank statement that this app did not
	read, and overwriting them from the transaction feed would replace the
	independent record with a restatement of the thing it is meant to check —
	after which every period ties out perfectly and the chain proves nothing.

	`recompute_transaction_sum=true` overrides that for the case it is right for:
	an account whose anchors were created from the feed to begin with. It is
	explicit, it is reported per period with the before and after, and it is never
	the default.

	`dry_run` does the whole computation and writes nothing.
	"""
	_require_anchors()
	bank_account = _resolve_bank_account(args, required=True)
	dry_run = bool(as_bool(args, "dry_run", False))
	from_feed = bool(as_bool(args, "recompute_transaction_sum", False))

	filters = _anchor_filters(args, bank_account=bank_account)
	rows = _anchor_rows(filters, order_by="period_start asc", limit=MAX_SCAN)
	if not rows:
		raise ToolError(
			f"{bank_account} has no Statement Anchors in that range, so there is no chain to rebuild. "
			"push_statement_anchor is how they arrive."
		)

	changes, unchanged = [], 0
	prior_closing = None
	lines_matched, lines_unmatched = 0, 0
	for row in rows:
		before = _anchor_out(row)
		tolerance = before["variance_tolerance"] or DEFAULT_TOLERANCE

		transaction_sum = before["transaction_sum"]
		feed_sum = None
		if from_feed:
			transactions = _period_transactions(bank_account, before["period_start"], before["period_end"])
			feed_sum = round(sum(item["amount_signed"] for item in transactions), 2)
			transaction_sum = feed_sum

		computed_closing = round(before["anchored_opening"] + transaction_sum, 2)
		variance = round(before["anchored_closing"] - computed_closing, 2)
		reconciled = 1 if abs(variance) <= tolerance else 0
		gap = 0
		if (
			prior_closing is not None
			and abs(round(before["anchored_opening"] - prior_closing, 2)) > tolerance
		):
			gap = 1
		prior_closing = before["anchored_closing"]

		payload = {
			"transaction_sum": transaction_sum,
			"computed_closing": computed_closing,
			"variance": variance,
			"reconciled": reconciled,
			"chain_gap_from_prior": gap,
		}
		moved = {
			key: {"was": before[key] if key in before else None, "now": value}
			for key, value in payload.items()
			if _differs(before.get(key), value)
		}
		matched, missing = _rebuild_lines(before["name"], bank_account, before, args, dry_run)
		lines_matched += matched
		lines_unmatched += missing

		if not moved and not matched:
			unchanged += 1
			continue
		if not dry_run and moved:
			frappe.db.set_value(ANCHOR, before["name"], payload)
		changes.append(
			{
				"anchor": before["name"],
				"period_start": before["period_start"],
				"period_end": before["period_end"],
				"changed": moved,
				"feed_transaction_sum": feed_sum,
				"statement_lines_matched": matched,
				"statement_lines_unmatched": missing,
			}
		)

	data = {
		"bank_account": bank_account,
		"dry_run": dry_run,
		"recompute_transaction_sum": from_feed,
		"anchors_examined": len(rows),
		"anchors_changed": len(changes),
		"anchors_unchanged": unchanged,
		"changes": changes,
		"statement_lines_matched": lines_matched,
		"statement_lines_unmatched": lines_unmatched,
		"note": (
			"Only DERIVED values were recomputed. anchored_opening, anchored_closing and "
			"transaction_sum came off a bank statement this app did not read, and rewriting them "
			"from the transaction feed would replace the independent record with a restatement of "
			"the thing it exists to check."
		),
	}
	if from_feed:
		data["warning"] = (
			"recompute_transaction_sum=true REPLACED each period's statement transaction sum with "
			"the sum of the Bank Transactions on file. Every period that previously varied because a "
			"movement was missing now ties out — the variance did not go away, the record of it did. "
			"Read `feed_transaction_sum` on each change for what was there before."
		)
	if dry_run:
		data["warning_dry_run"] = "dry_run=true — NOTHING was written."

	return ToolResult(
		data,
		("would rebuild" if dry_run else "rebuilt")
		+ f" {len(changes)} of {len(rows)} anchored period(s) on {bank_account}",
		docstatus_delta="none (derived fields on existing anchors)" if not dry_run else "",
	)


def _differs(was, now) -> bool:
	if isinstance(was, bool) or isinstance(now, bool):
		return bool(was) != bool(now)
	try:
		return round(float(was or 0), 2) != round(float(now or 0), 2)
	except (TypeError, ValueError):
		return was != now


def _rebuild_lines(anchor_name: str, bank_account: str, anchor: dict, args: dict, dry_run: bool):
	"""Match this anchor's statement lines and, unless dry, write the pairing down.

	Returns (matched, unmatched). The write goes through the parent document
	rather than `db.set_value` because these are child rows, and Frappe has no
	other supported way to update one.
	"""
	lines = _statement_lines(anchor_name)
	if not lines:
		return 0, 0
	transactions = _period_transactions(bank_account, anchor["period_start"], anchor["period_end"])
	report = _match_lines(
		lines,
		transactions,
		day_window=as_int(args, "day_window", DEFAULT_LINE_DAY_WINDOW),
		amount_tolerance=_tolerance(args, DEFAULT_LINE_AMOUNT_TOLERANCE),
	)
	matched = {row["idx"]: row for row in report["matched"]}
	if not dry_run and matched:
		try:
			doc = frappe.get_doc(ANCHOR, anchor_name)
			for row in doc.get("statement_lines") or []:
				hit = matched.get(int(row.get("idx") or 0))
				if not hit:
					continue
				_set_on_row(
					row,
					{
						"matched_bank_transaction": hit["bank_transaction"],
						"match_note": f"amount and date within {hit['day_gap']} day(s)",
					},
				)
			doc.save(ignore_permissions=True)
		except Exception:
			frappe.log_error(
				title=f"erpnext_mcp: could not write line matches onto {anchor_name}",
				message=compat.traceback_text(),
			)
	return len(report["matched"]), len(report["unmatched_lines"])


# ── 7. get_account_pairing ───────────────────────────────────────────────────


def get_account_pairing(args: dict) -> ToolResult:
	"""Every bank account with its companion and its aggregator identity. Read-only.

	WHY THE ONE-SIDED PAIRINGS ARE CALLED OUT SEPARATELY. A pairing is a fact
	about two accounts and is stored on both, so a link that exists in one
	direction only is a half-written change — and it reads as a working pairing
	from whichever end has the link. `pair_bank_accounts` writes both sides; a
	one-sided pair usually means somebody set the field in the Desk.
	"""
	_require_bank_account()
	installed = _pairing_fields_present() or ensure_pairing_fields()
	company = resolve_company(as_str(args, "company"))
	filters = {}
	if company:
		filters["company"] = company
	named = as_str(args, "bank_account")
	if named:
		filters["name"] = _resolve_bank_account(args, required=True)
	limit = as_limit(args)

	fields = compat.existing_fields(BANK_ACCOUNT, list(_BANK_ACCOUNT_FIELDS))
	rows = frappe.db.get_all(
		BANK_ACCOUNT, filters=filters, fields=fields, order_by="company asc, name asc", limit=limit
	)
	accounts = [_pairing_out(dict(row)) for row in rows]
	by_name = {account["name"]: account for account in accounts}

	one_sided, mismatched = [], []
	for account in accounts:
		partner_name = account["paired_bank_account"]
		if not partner_name:
			continue
		partner = by_name.get(partner_name) or _pairing_out(_bank_account_row(partner_name))
		if not partner.get("name"):
			one_sided.append(
				{"bank_account": account["name"], "points_at": partner_name, "why": "no such account"}
			)
			continue
		if partner.get("paired_bank_account") != account["name"]:
			one_sided.append(
				{
					"bank_account": account["name"],
					"points_at": partner_name,
					"why": "the other account does not point back",
				}
			)
		if (
			account["pairing_type"]
			and partner.get("pairing_type")
			and account["pairing_type"] == partner.get("pairing_type")
		):
			mismatched.append(
				{
					"bank_account": account["name"],
					"paired_bank_account": partner_name,
					"pairing_type": account["pairing_type"],
					"why": "both sides claim the same role, so neither says which is which",
				}
			)

	paired = [account for account in accounts if account["paired_bank_account"]]
	anchored = _anchor_counts([account["name"] for account in accounts])
	for account in accounts:
		account["anchored_periods"] = anchored.get(account["name"], 0)

	data = {
		"company": company,
		"accounts": accounts,
		"count": len(accounts),
		"limit": limit,
		"truncated": len(accounts) == limit,
		"paired_count": len(paired),
		"unpaired_count": len(accounts) - len(paired),
		"sync_enabled_count": sum(1 for account in accounts if account["sync_enabled"]),
		"one_sided_pairings": one_sided,
		"pairing_type_conflicts": mismatched,
		"fields_installed": installed,
		"note": (
			"A pairing is a property of an account, not a record of its own, and it is stored on "
			"BOTH accounts — a brokerage and the cash-services account its trades settle through are "
			"one relationship seen from two sides. An anchor chain reconciles cash and sweep only; "
			"the securities leg lives on the companion."
		),
	}
	if not installed:
		data["warning"] = (
			"This site's Bank Account has no pairing columns, so every pairing reads as absent. They "
			"ship with erpnext_mcp v0.73.0 — run `bench migrate`, or call pair_bank_accounts, which "
			"creates them on first use."
		)
	elif one_sided:
		data["warning"] = (
			f"{len(one_sided)} pairing(s) exist in one direction only. Re-run pair_bank_accounts on "
			"the pair to write both sides — a half-written pairing reads as working from one end."
		)

	return ToolResult(
		data,
		f"{len(accounts)} bank account(s), {len(paired)} paired" + (f" for {company}" if company else ""),
	)


def _pairing_out(row: dict) -> dict:
	if not row:
		return {}
	return {
		"name": row.get("name"),
		"account_name": row.get("account_name"),
		"bank": row.get("bank"),
		"company": row.get("company"),
		"gl_account": row.get("account") or None,
		"disabled": bool(int(row.get("disabled") or 0)),
		"paired_bank_account": row.get(PAIRED_FIELD) or None,
		"pairing_type": row.get(PAIRING_TYPE_FIELD) or None,
		"plaid_account_id": row.get(PLAID_ID_FIELD) or None,
		"plaid_account_id_history": id_history(row.get(PLAID_ID_HISTORY_FIELD)),
		"plaid_account_mask": row.get(PLAID_MASK_FIELD) or None,
		"plaid_account_type": row.get(PLAID_TYPE_FIELD) or None,
		"plaid_account_subtype": row.get(PLAID_SUBTYPE_FIELD) or None,
		"sync_enabled": bool(int(row.get(SYNC_FIELD) or 0)),
	}


def _anchor_counts(names: list) -> dict:
	"""How many anchored periods each account has. One query, not one per account."""
	if not names or not compat.doctype_exists(ANCHOR):
		return {}
	rows = frappe.db.get_all(
		ANCHOR, filters={"bank_account": ("in", names)}, fields=["bank_account"], limit=MAX_SCAN
	)
	out: dict = {}
	for row in rows:
		out[row["bank_account"]] = out.get(row["bank_account"], 0) + 1
	return out


# ── 8. pair_bank_accounts ────────────────────────────────────────────────────


def pair_bank_accounts(args: dict) -> ToolResult:
	"""Link two Bank Accounts as companions. MUTATING; writes both sides.

	BOTH SIDES, ALWAYS, which is the whole reason this is a tool rather than a
	note telling somebody to set a field. A pairing written from one end reads as
	a working pairing from that end and as no pairing at all from the other, and
	the half that is missing is the half a reconciliation run happens to start
	from.

	It refuses to break an existing pairing silently: an account already paired to
	somebody else has to be unpaired first, or `replace=true` passed, and either
	way the account that gets orphaned is named in the result.
	"""
	_require_bank_account()
	installed = _pairing_fields_present() or ensure_pairing_fields()
	if not installed:
		raise ToolError(
			"this site's Bank Account will not take the pairing columns, so a pairing has nowhere to "
			"go. Check the Error Log for the reason. Nothing was written."
		)

	first = _resolve_bank_account(args, required=True)
	second = _resolve_bank_account(args, required=True, key="paired_bank_account")
	if first == second:
		raise ToolError("an account cannot be its own companion. Nothing was written.")

	rows = {name: _bank_account_row(name) for name in (first, second)}
	companies = {name: row.get("company") for name, row in rows.items()}
	if companies[first] and companies[second] and companies[first] != companies[second]:
		raise ToolError(
			f"{first} belongs to {companies[first]!r} and {second} to {companies[second]!r}. A "
			"brokerage and its cash-services companion are one relationship inside one entity; "
			"pairing across companies would put one company's sweep in another's reconciliation. "
			"Nothing was written."
		)

	replace = bool(as_bool(args, "replace", False))
	orphaned = []
	for name in (first, second):
		current = rows[name].get(PAIRED_FIELD)
		partner = second if name == first else first
		if not current or current == partner:
			continue
		if not replace:
			raise ToolError(
				f"{name} is already paired with {current}. Pass replace=true to repoint it — which "
				f"leaves {current} pointing at nothing until it is paired again. Nothing was written."
			)
		orphaned.append({"bank_account": current, "was_paired_with": name})

	first_type = _pairing_type(args, "pairing_type")
	second_type = _pairing_type(args, "paired_pairing_type")
	if first_type and not second_type:
		# The pair has exactly two roles, so naming one names the other. Inferred
		# rather than left blank because a pairing where only one side says what
		# it is cannot answer "which of these is the brokerage" from the other end.
		second_type = "Cash Services" if first_type == "Brokerage" else "Brokerage"
	if second_type and not first_type:
		first_type = "Cash Services" if second_type == "Brokerage" else "Brokerage"
	if first_type and second_type and first_type == second_type:
		raise ToolError(
			f"both accounts cannot be {first_type!r} — the pair has two roles and the point of "
			"recording them is to say which account is which. Nothing was written."
		)

	for name, partner, role in ((first, second, first_type), (second, first, second_type)):
		payload = {PAIRED_FIELD: partner}
		if role:
			payload[PAIRING_TYPE_FIELD] = role
		frappe.db.set_value(BANK_ACCOUNT, name, payload)

	for orphan in orphaned:
		frappe.db.set_value(BANK_ACCOUNT, orphan["bank_account"], {PAIRED_FIELD: None})

	data = {
		"bank_account": _pairing_out(_bank_account_row(first)),
		"paired_bank_account": _pairing_out(_bank_account_row(second)),
		"company": companies[first] or companies[second],
		"orphaned": orphaned,
		"note": (
			"Both records now point at each other. Pairing changes no transaction and no anchor: it "
			"is what tells a reconciliation run that the securities leg of a trade settles on the "
			"companion account, so a brokerage chain that reconciles cash and sweep only is not "
			"missing anything."
		),
	}
	if orphaned:
		data["warning"] = (
			f"{len(orphaned)} account(s) were left with no companion: "
			f"{', '.join(row['bank_account'] for row in orphaned)}. Pair them again or their side of "
			"the relationship is now silent."
		)

	return ToolResult(
		data,
		f"paired {first} with {second}"
		+ (f" ({first_type} ↔ {second_type})" if first_type and second_type else ""),
		docstatus_delta="none (link fields on two existing bank accounts)",
	)


def _pairing_type(args: dict, key: str) -> str:
	value = as_str(args, key)
	if not value:
		return ""
	for option in PAIRING_TYPES:
		if value.strip().lower() == option.lower():
			return option
	raise ToolError(f"{key} must be one of: {', '.join(PAIRING_TYPES)} — got {value!r}.")


# ── 9. get_statement_recon_report ────────────────────────────────────────────


def get_statement_recon_report(args: dict) -> ToolResult:
	"""The statement, the feed and the LEDGER for the same periods, side by side.

	THREE RECORDS OF ONE MONTH, and the comparison nobody can do from any single
	one of them:

	  * the STATEMENT — `transaction_sum` off the anchor, which is the bank's own
	    account of what moved;
	  * the FEED — the Bank Transactions on file for the period;
	  * the LEDGER — the GL movement on the account the Bank Account posts to.

	The feed and the ledger disagreeing is the ordinary state of an unbooked
	month: transactions have arrived and nobody has posted them yet. The STATEMENT
	and the feed disagreeing is the serious one, because it means the two
	independent records of the same period do not match and every categorisation
	built on the feed inherits the difference.

	`by_category` is the ledger side broken out by the category rules assigned,
	which is what makes "we booked 40,000 of fuel and the bank moved 52,000"
	answerable. It is REPORTED, not reconciled: a category is a label on a
	transaction, and the GL total beside it is a different measurement of the same
	money.

	Read-only. It posts nothing and proposes nothing.
	"""
	_require_anchors()
	company = resolve_company(as_str(args, "company"))
	bank_account = _resolve_bank_account(args, required=False)
	if not bank_account and not company:
		raise ToolError("name a bank_account (or a plaid_account_mask), or a company.")

	filters = _anchor_filters(args, bank_account=bank_account, company=company or "")
	rows = _anchor_rows(filters, order_by="bank_account asc, period_start asc", limit=MAX_SCAN)
	if not rows:
		raise ToolError(
			"no Statement Anchors in that range, so there is no statement to compare the ledger "
			"against. push_statement_anchor is how they arrive."
		)

	periods, categories = [], {}
	statement_total = feed_total = ledger_total = 0.0
	for row in rows:
		anchor = _anchor_out(row)
		transactions = _period_transactions(
			anchor["bank_account"], anchor["period_start"], anchor["period_end"]
		)
		feed_sum = round(sum(item["amount_signed"] for item in transactions), 2)
		gl = _gl_movement(anchor["bank_account"], anchor["period_start"], anchor["period_end"])

		statement_total = round(statement_total + anchor["transaction_sum"], 2)
		feed_total = round(feed_total + feed_sum, 2)
		ledger_total = round(ledger_total + (gl["movement"] or 0), 2)

		for item in transactions:
			label = item["category"] or "(uncategorised)"
			bucket = categories.setdefault(label, {"count": 0, "amount_signed": 0.0, "expense_accounts": []})
			bucket["count"] += 1
			bucket["amount_signed"] = round(bucket["amount_signed"] + item["amount_signed"], 2)
			if item["expense_account"] and item["expense_account"] not in bucket["expense_accounts"]:
				bucket["expense_accounts"].append(item["expense_account"])

		periods.append(
			{
				"anchor": anchor["name"],
				"bank_account": anchor["bank_account"],
				"period_start": anchor["period_start"],
				"period_end": anchor["period_end"],
				"statement_transaction_sum": anchor["transaction_sum"],
				"feed_transaction_sum": feed_sum,
				"ledger_movement": gl["movement"],
				"gl_account": gl["account"],
				"gl_entry_count": gl["entries"],
				"statement_vs_feed": round(anchor["transaction_sum"] - feed_sum, 2),
				"feed_vs_ledger": round(feed_sum - (gl["movement"] or 0), 2) if gl["account"] else None,
				"variance": anchor["variance"],
				"variance_reason": anchor["variance_reason"],
				"reconciled": anchor["reconciled"],
			}
		)

	unbooked = [row for row in periods if row["feed_vs_ledger"] not in (None, 0)]
	feed_breaks = [row for row in periods if row["statement_vs_feed"]]
	limit = as_limit(args)

	data = {
		"company": company,
		"bank_account": bank_account or None,
		"periods": periods[:limit],
		"period_count": len(periods),
		"truncated": len(periods) > limit,
		"statement_total": statement_total,
		"feed_total": feed_total,
		"ledger_total": ledger_total,
		"statement_vs_feed_total": round(statement_total - feed_total, 2),
		"feed_vs_ledger_total": round(feed_total - ledger_total, 2),
		"by_category": dict(sorted(categories.items(), key=lambda item: item[1]["amount_signed"])),
		"periods_with_unbooked_movement": len(unbooked),
		"periods_where_statement_and_feed_disagree": len(feed_breaks),
		"sign_convention": (
			"Positive is money IN, on all three. `ledger_movement` is debit minus credit on the Bank "
			"Account's GL account, which is the same direction for an asset."
		),
		"note": (
			"Three measurements of one month, never summed. Feed against ledger is the ordinary "
			"backlog — transactions on file that nobody has posted yet. STATEMENT against feed is "
			"the serious one: two independent records of the same period that do not agree, which "
			"every categorisation built on the feed inherits."
		),
	}
	if feed_breaks:
		data["warning"] = (
			f"{len(feed_breaks)} period(s) have a statement sum that the Bank Transactions on file do "
			"not add up to. get_anchor_variance_breakdown takes one of them apart; "
			"list_unmatched_statement_lines names the missing movements where statement lines are on "
			"file."
		)

	return ToolResult(
		data,
		f"{len(periods)} period(s): statement {statement_total}, feed {feed_total}, ledger {ledger_total}",
	)


def _gl_movement(bank_account: str, from_date, to_date) -> dict:
	"""Debit minus credit on the GL account this Bank Account posts to.

	Returns `account: None` when the Bank Account names no GL account, which is a
	real and ordinary state — a bank account can be registered for a feed before
	anybody decides where it posts — and reporting a movement of zero for it would
	claim the ledger disagrees with the feed by the whole month.
	"""
	account = frappe.db.get_value(BANK_ACCOUNT, bank_account, "account")
	if not account or not compat.doctype_exists(GL_ENTRY):
		return {"account": None, "movement": None, "entries": 0}
	filters = {"account": account}
	_apply_date_range(filters, "posting_date", from_date, to_date)
	if compat.has_field(GL_ENTRY, "is_cancelled"):
		filters["is_cancelled"] = 0
	totals = frappe.db.get_all(
		GL_ENTRY,
		filters=filters,
		fields=["sum(debit) as debit", "sum(credit) as credit", "count(name) as entries"],
	)
	row = (totals or [{}])[0] or {}
	movement = round(float(row.get("debit") or 0) - float(row.get("credit") or 0), 2)
	return {"account": account, "movement": movement, "entries": int(row.get("entries") or 0)}


# ── used by the push endpoint ────────────────────────────────────────────────


def upsert_anchor(payload: dict) -> dict:
	"""Create or update one Statement Anchor from a pushed payload. Idempotent.

	THE IDENTITY IS (bank_account, period_start, period_end) AND NOTHING ELSE,
	which is what makes a pipe safe to re-run. A sync that pushed the same month
	twice — because it retried, because a parse was repeated, because somebody ran
	it by hand — must produce one anchor with the later numbers, not two anchors
	that disagree.

	Shared by `erpnext_mcp.bank.push_statement_anchor` and the tests. It is here
	rather than in the endpoint module because the endpoint's job is
	authentication and shape-checking, and an upsert that lived there could not be
	reached any other way.
	"""
	_require_anchors()
	bank_account = str(payload.get("bank_account") or "").strip()
	if not bank_account:
		raise ToolError("bank_account is required.")
	if not frappe.db.exists(BANK_ACCOUNT, bank_account):
		raise ToolError(f"no Bank Account named {bank_account!r} on this site.")
	period_start = payload.get("period_start")
	period_end = payload.get("period_end")
	if not period_start or not period_end:
		raise ToolError("period_start and period_end are both required — an anchor IS a period.")

	writable = (
		"plaid_account_mask",
		"anchored_opening",
		"anchored_closing",
		"transaction_sum",
		"variance_tolerance",
		"parser_version",
		"mark_to_market_delta",
		"portfolio_opening_value",
		"portfolio_closing_value",
		"statement_pdf",
		"source_statement_id",
	)

	existing = frappe.db.get_value(
		ANCHOR,
		{"bank_account": bank_account, "period_start": period_start, "period_end": period_end},
		"name",
	)
	doc = frappe.get_doc(ANCHOR, existing) if existing else frappe.new_doc(ANCHOR)
	doc.bank_account = bank_account
	doc.period_start = period_start
	doc.period_end = period_end
	for fieldname in writable:
		if fieldname in payload and payload[fieldname] not in (None, ""):
			doc.set(fieldname, payload[fieldname])

	# THE EXPLANATION IS NOT OVERWRITTEN BY A LATER PUSH, and that is why it is
	# not in `writable`. A pushed reason is accepted onto an anchor that has
	# none — which is what makes a one-time migration of a pipe's own tags work
	# — and never over one that does. After consolidation the sentence is a
	# person's, written here through `set_anchor_variance_reason`, and a sync
	# running every night would otherwise erase it on the next run.
	pushed_reason = str(payload.get("variance_reason") or "").strip()
	if pushed_reason and not (doc.get("variance_reason") or "").strip():
		doc.variance_reason = pushed_reason

	lines = payload.get("statement_lines")
	replaced_lines = 0
	if isinstance(lines, list):
		# REPLACED WHOLESALE, NOT APPENDED. A re-parse of the same statement
		# produces the same lines again, and appending would double a month of
		# them — after which every line matches a transaction and the count of
		# unmatched ones, which is the entire product, is silently wrong.
		doc.set("statement_lines", [])
		for line in lines:
			if not isinstance(line, dict):
				continue
			doc.append(
				"statement_lines",
				{
					"line_date": line.get("line_date") or line.get("date"),
					"description": str(line.get("description") or "")[:140],
					"amount": line.get("amount"),
					"line_type": line.get("line_type") or None,
					"reference": line.get("reference") or None,
				},
			)
			replaced_lines += 1

	if existing:
		doc.save(ignore_permissions=True)
	else:
		doc.insert(ignore_permissions=True)

	return {
		"name": doc.name,
		"created": not existing,
		"updated": bool(existing),
		"statement_lines": replaced_lines,
		"anchor": _anchor_out(
			frappe.db.get_value(
				ANCHOR, doc.name, compat.existing_fields(ANCHOR, list(_ANCHOR_FIELDS)), as_dict=True
			)
			or {}
		),
	}


def upsert_pairing(payload: dict) -> dict:
	"""Write the pushed Plaid identity, and the pairing, onto a Bank Account.

	The metadata half of what a sync knows and ERPNext cannot work out for itself:
	which aggregator account this is, what kind it is, and whether the pipe is
	pulling it. Only keys actually present are written, so a pipe that knows the
	mask and not the subtype does not blank the subtype somebody typed.

	THE DOCNAME IS THE IDENTIFIER AND THE AGGREGATOR ID IS NOT, which is the
	whole reason this keys off `bank_account`. A re-linked connection issues a
	new id for the same real account, so a push that found its target by id would
	stop finding it precisely when the record most needs correcting. The id it
	replaces is appended to `plaid_account_id_history` in the same write — see
	`id_history_update` — so the chain survives any number of reconnections.
	"""
	_require_bank_account()
	# The docname, or the mask when that is all the pipe has — resolved the same
	# way every other tool in this module resolves an account, and refused rather
	# than guessed when two accounts share four digits.
	bank_account = _resolve_bank_account(payload, required=True)
	if not (_pairing_fields_present() or ensure_pairing_fields()):
		raise ToolError(
			"this site's Bank Account will not take the pairing columns, so there is nowhere to put "
			"this. Check the Error Log. Nothing was written."
		)

	updates = {}
	for key, fieldname in (
		("plaid_account_id", PLAID_ID_FIELD),
		("plaid_account_mask", PLAID_MASK_FIELD),
		("plaid_account_type", PLAID_TYPE_FIELD),
		("plaid_account_subtype", PLAID_SUBTYPE_FIELD),
	):
		if key in payload and payload[key] not in (None, ""):
			updates[fieldname] = str(payload[key]).strip()
	kind = updates.get(PLAID_TYPE_FIELD)
	if kind and kind not in PLAID_ACCOUNT_TYPES:
		raise ToolError(
			f"plaid_account_type must be one of: {', '.join(PLAID_ACCOUNT_TYPES)} — got {kind!r}."
		)
	if "sync_enabled" in payload and payload["sync_enabled"] is not None:
		updates[SYNC_FIELD] = 1 if payload["sync_enabled"] in (1, True, "1", "true", "True") else 0

	# The id chain across re-links. Read before anything is written, folded into
	# the same write, so the superseded id cannot be lost between two statements.
	# A pushed `plaid_account_id_history` is merged in rather than trusted as the
	# whole truth — see `id_history_update` — so this works whether the pipe
	# tracks the chain itself or leaves it entirely to what this site observes.
	before = _bank_account_row(bank_account)
	history_update = id_history_update(
		before, updates.get(PLAID_ID_FIELD, ""), payload.get("plaid_account_id_history")
	)
	if history_update:
		updates.update(history_update)

	# Reported only for an id this push actually retired. A history that merely
	# grew because the pipe sent ids this site had not heard of is not a
	# repointing, and logging it as one would put a re-link in the sync log on
	# the day somebody backfilled.
	live = updates.get(PLAID_ID_FIELD, "")
	was = str(before.get(PLAID_ID_FIELD) or "")
	superseded = was if (live and was and was != live) else ""

	paired = str(payload.get("paired_bank_account") or "").strip()
	pairing_type = str(payload.get("pairing_type") or "").strip()
	paired_result = None
	if paired:
		paired_result = pair_bank_accounts(
			{
				"bank_account": bank_account,
				"paired_bank_account": paired,
				"pairing_type": pairing_type,
				"replace": True,
			}
		).data
	elif pairing_type:
		if pairing_type not in PAIRING_TYPES:
			raise ToolError(
				f"pairing_type must be one of: {', '.join(PAIRING_TYPES)} — got {pairing_type!r}."
			)
		updates[PAIRING_TYPE_FIELD] = pairing_type

	if updates:
		frappe.db.set_value(BANK_ACCOUNT, bank_account, updates)

	return {
		"bank_account": bank_account,
		"updated_fields": sorted(updates.keys()),
		"pairing": paired_result,
		# Named rather than left to be inferred from the history array, because
		# "this push repointed the account" is the event an operator wants in the
		# sync log, and diffing two arrays to discover it is not the same thing.
		"plaid_account_id_superseded": superseded or None,
		"account": _pairing_out(_bank_account_row(bank_account)),
	}
