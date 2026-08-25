# SPDX-License-Identifier: MIT
"""The mutating tools. Every one is off until an operator turns it on.

THE SHAPE OF THE PERMISSION. There is no `post_journal_entry` here. There is
`create_journal_entry`, which can only ever produce a draft, and a separate
`submit_journal_entry`, which can only act on a draft that already exists. An
operator who enables the first and not the second has given an AI a scratchpad
in the general ledger and nothing more: it can propose entries all day and not
one of them touches a balance. That split is the whole point, and it is why
`create_journal_entry` will not accept a `docstatus` argument and cannot be
talked into submitting.

WHAT THESE TOOLS DO NOT DO. They do not bypass ERPNext. Every write goes through
`frappe.get_doc(...).insert()` / `.submit()` / `.cancel()`, so the doctype's own
validation, the fiscal-year check, period-closing vouchers, account freezing,
mandatory dimensions and every `on_submit` hook all run exactly as they do for a
human in the UI. There is no raw SQL in this file, and there should never be:
the day an MCP tool writes a GL Entry directly is the day this app can corrupt a
ledger.

THE ONE FIELD-LEVEL EXCEPTION, AND THE FENCE AROUND IT.
`update_journal_entry_party` sets `party_type` and `party` on a *submitted*
entry's line with `frappe.db.set_value`, on the line and on its GL Entry rows.
That is a deliberate exception to the paragraph above and it is fenced on three
sides. It is still not raw SQL — the ORM's db layer, one field at a time, no
`frappe.db.sql` anywhere in this app. It cannot move a balance: `party` is an
attribution column, and every debit, credit, account and date is refused as an
argument, so the trial balance after the call is arithmetically identical to the
one before. And there is no supported alternative — ERPNext marks `party` as not
allowed on submit, so the only other routes are cancel-and-repost, which destroys
the evidence trail for a clerical correction, or the Desk, which is what an MCP
server exists to make unnecessary. The rule that stands is the one that matters:
no tool here writes an *amount* to a GL Entry.

DOUBLE ENTRY IS CHECKED HERE ANYWAY. ERPNext validates that debits equal credits
on submit, but `create_journal_entry` checks before insert so an unbalanced
proposal fails with an arithmetic message the model can act on, instead of
leaving a broken draft behind for a human to find.

EVERY LINE CARRIES BOTH AMOUNT COLUMNS. A Journal Entry Account row stores each
amount twice — `debit` in the company's currency and `debit_in_account_currency`
in the account's — and ERPNext's `set_amounts_in_account_currency` derives the
first FROM the second on every validate, multiplying by the line's exchange rate.
A line built with `debit` alone therefore inserts looking correct and is written
to the database with its debit silently zeroed, and the entry is refused on
submit with *"Row N: Both Debit and Credit values cannot be zero"* — a draft that
cannot be posted and does not say why. That shipped in v0.8.0 and cost a day of
somebody's life posting opening balances by hand. `validated_journal_lines` now
fills both columns for every line it returns, which is why the fix lives here
rather than in the tool that surfaced it: every Journal Entry this app writes —
opening balances, member events, depreciation, loan payments — comes through this
one function, and one of them getting it right would have left the rest wrong.
"""

import frappe

from .. import compat, settings
from ..args import as_bool, as_date, as_float, as_int, as_str, resolve_account, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from .read import _resolve_bank_account

#: Rounding tolerance for the double-entry check. ERPNext stores currency to the
#: precision configured on the site (2 by default); half a cent is comfortably
#: below one unit of the smallest tracked amount and above float noise.
BALANCE_TOLERANCE = 0.005

#: Per-line fields a caller may set on a Journal Entry Account row. Anything
#: else is rejected by name rather than silently dropped — a model that thought
#: it was setting `amount` should be told the field is `debit` or `credit`, not
#: get back a zero-value entry.
_LINE_FIELDS = (
	"account",
	"debit",
	"credit",
	"debit_in_account_currency",
	"credit_in_account_currency",
	"party_type",
	"party",
	"cost_center",
	"project",
	"against_account",
	"reference_type",
	"reference_name",
	"reference_due_date",
	"user_remark",
	"exchange_rate",
	"account_currency",
	"is_advance",
	"bank_account",
)

#: The per-line escape hatch for accounting dimensions. Kept out of
#: `_LINE_FIELDS` on purpose: `dimensions` is not itself a field on Journal Entry
#: Account, it is an object whose *keys* are, and every key is checked against
#: this site's own meta before it is written.
#:
#: Why an object rather than more allowlisted keys: a dimension's fieldname is
#: invented by whoever created it (`member`, `bbch_stage`, `block`), so there is
#: no list this app could ship. Why not simply stop rejecting unknown keys:
#: because then `amount` — which a model will send, meaning `debit` — would be
#: silently dropped instead of corrected. Unknown keys stay refused; dimensions
#: get their own door, and going through it is an assertion that the caller meant
#: a dimension rather than mistyped a field.
_DIMENSIONS_KEY = "dimensions"

#: Journal Entry lines are `Journal Entry Account` rows, and that is the doctype
#: ERPNext puts accounting dimension fields on. See `tools/dimensions.py`.
_LINE_DOCTYPE = "Journal Entry Account"

#: How many entries `bulk_submit_journal_entries` will take in one call. Not a
#: performance limit — each submit is its own transaction and the loop would run
#: all day. It is a legibility limit: a batch whose failures a human has to read
#: through should fit on a screen, and a caller with two thousand drafts is
#: better served by four calls it can check between than by one it cannot.
_MAX_BULK = 500


# ── 11. create_journal_entry ────────────────────────────────────────────────
def create_journal_entry(args: dict) -> ToolResult:
	"""Create a DRAFT Journal Entry. Never submits, whatever it is asked."""
	company = resolve_company(as_str(args, "company"), required=True)
	posting_date = as_date(args, "posting_date", required=True)
	user_remark = as_str(args, "user_remark", required=True)
	lines = validated_journal_lines(args.get("accounts"), company)
	total_debit, total_credit = assert_balanced(lines)
	dimension_fields = sorted({key for line in lines for key in line if key not in _LINE_FIELDS})

	extras = {}
	if as_str(args, "voucher_type"):
		extras["voucher_type"] = as_str(args, "voucher_type")
	for field in ("cheque_no", "cheque_date", "bill_no"):
		value = as_str(args, field)
		if value:
			extras[field] = as_date(args, field) if field.endswith("_date") else value

	# v0.80.0. THE FINANCIAL CONTROLS RUN HERE, BEFORE THE INSERT, and the
	# placement is the whole of what makes an enforced control mean anything: a
	# control consulted after the write can report and cannot refuse. In Advisory
	# — which is what every control ships as — this files its findings against the
	# entry and returns them, and the entry is written exactly as it always was.
	# In Enforced it raises, and nothing below this line runs.
	#
	# `preparer` is the effective user rather than `frappe.session.user`: with
	# `require_user_context` on, mutations run as the configured MCP System User
	# and that is the principal the entry will be attributed to in `owner`, so it
	# is also the one segregation of duties has to be asked about.
	gate = controls_gate(company, posting_date, total_debit, lines, args)

	doc = insert_draft_journal_entry(company, posting_date, lines, user_remark, extras)

	data = {
		"name": doc.name,
		"docstatus": 0,
		"docstatus_label": "draft",
		"company": company,
		"posting_date": posting_date,
		"total_debit": round(total_debit, 2),
		"total_credit": round(total_credit, 2),
		"line_count": len(lines),
		"user_remark": user_remark,
		"dimension_fields_set": dimension_fields,
		"next_step": (
			"This is a draft and affects no balance. Submit it in ERPNext, or via "
			"submit_journal_entry if that tool is enabled."
		),
		# v0.80.0. What the financial controls made of this entry. Present on
		# every response, including the clean case: a caller has to be able to tell
		# "the controls looked and found nothing" from "the controls did not run",
		# and a key that appeared only on findings would collapse the two.
		"controls": gate,
	}
	summary = (
		f"created draft Journal Entry {doc.name} for {company} on {posting_date}: "
		f"{len(lines)} line(s), {round(total_debit, 2)} debit = "
		f"{round(total_credit, 2)} credit"
	)
	findings = gate.get("findings") or []
	if findings:
		# Said in the SUMMARY and not only in the payload, because the summary is
		# what lands in MCP Action Log and what an operator scans a month later. An
		# advisory finding that was only ever in `data` would be a finding nobody
		# reads, which is the failure mode Advisory mode exists to avoid.
		data["controls_note"] = (
			f"{len(findings)} financial control finding(s) were raised against this entry and it was "
			"WRITTEN ANYWAY, because the control(s) concerned are Advisory on this site. Each one is "
			"filed as a compliance alert against the entry. Under enforcement this call would have "
			"been refused — list_control_points shows which controls are which."
		)
		summary += f" — {len(findings)} advisory control finding(s)"
	return ToolResult(data, summary, docstatus_delta="none → 0 (draft)")


def controls_gate(company: str, posting_date, total_debit: float, lines: list, args: dict) -> dict:
	"""Run the Phase 1 financial controls over an entry about to be written.

	A THIN SEAM, ON PURPOSE. Everything it knows is in `tools/controls.py`; what
	lives here is the decision to CALL it, before the insert, from the one
	function every Journal Entry this app writes goes through. Keeping the seam
	this small is what lets `mutate.py` go on being the file a reviewer reads to
	answer "can this change the ledger?" without also having to hold four control
	definitions in their head.

	`approved_by` is an argument rather than something read off the document
	because ERPNext's Journal Entry has no approver column — the release is a
	docstatus transition, and who made it is in the version history rather than on
	the row. A caller booking an entry on somebody else's authority says so here,
	and segregation of duties is then a real check rather than a comparison of a
	field against itself.

	A LATE IMPORT. `tools/controls.py` reaches back into `enforcement`, which
	reads the Compliance Rule table; importing it at module load would put that
	chain in front of every `bench migrate` that touches this module. It costs
	nothing here and cannot break a migration.
	"""
	from . import controls as control_tools

	accounts = tuple(sorted({str(line.get("account") or "") for line in lines or [] if line.get("account")}))
	preparer = as_str(args, "prepared_by") or settings.effective_user()
	approver = as_str(args, "approved_by")
	return control_tools.journal_entry_gate(
		company,
		posting_date,
		total_debit,
		accounts,
		preparer=preparer,
		approver=approver,
	)


def insert_draft_journal_entry(
	company: str,
	posting_date: str,
	lines: list[dict],
	user_remark: str,
	extras: dict | None = None,
):
	"""Insert one balanced DRAFT Journal Entry. The only place this app writes one.

	Public, and used by `tools/governance.py` and `tools/assets.py` as well as by
	`create_journal_entry`, so that every Journal Entry this app produces —
	whatever asked for it — goes through the same insert and the same
	never-submitted assertion below. A second implementation elsewhere would be a
	second chance to ship one that posts.

	`lines` must already have been through `validated_journal_lines`.
	"""
	assert_balanced(lines)
	doc = frappe.new_doc("Journal Entry")
	doc.company = company
	doc.posting_date = posting_date
	doc.user_remark = user_remark
	for field, value in (extras or {}).items():
		doc.set(field, value)
	for line in lines:
		doc.append("accounts", line)

	doc.insert()
	# Belt to the "never submits" braces: if a future ERPNext hook ever
	# submitted on insert, this turns a silent posting into a loud failure.
	if int(doc.docstatus or 0) != 0:
		raise ToolError(
			f"Journal Entry {doc.name} was created with docstatus {doc.docstatus}, "
			"but this app only ever produces draft journal entries. Refusing to "
			"report success — inspect the site's Journal Entry hooks."
		)
	return doc


def validated_journal_lines(raw, company: str) -> list[dict]:
	"""Coerce and check the `accounts` argument into Journal Entry Account rows.

	Public for the same reason `insert_draft_journal_entry` is: the member-event
	and depreciation tools build their own lines and must have them checked by
	exactly the code that checks a hand-written entry's — group accounts refused,
	dimensions verified against this site's meta, one side per line.
	"""
	if not isinstance(raw, list) or not raw:
		raise ToolError(
			"accounts must be a non-empty list of objects, each with an account "
			"and a debit or a credit, e.g. "
			'[{"account": "1100 - Cash", "debit": 100}, '
			'{"account": "4100 - Sales", "credit": 100}]'
		)
	if len(raw) < 2:
		raise ToolError(
			"a Journal Entry needs at least two lines — one account cannot balance against itself"
		)

	lines = []
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"accounts[{index}] must be an object, got {type(entry).__name__}")
		unknown = sorted(set(entry) - set(_LINE_FIELDS) - {_DIMENSIONS_KEY})
		if unknown:
			raise ToolError(
				f"accounts[{index}] has unsupported field(s): {', '.join(unknown)}. "
				f"Supported: {', '.join(_LINE_FIELDS)}. A custom accounting dimension "
				f"goes in the per-line `{_DIMENSIONS_KEY}` object, not alongside these."
			)
		account = resolve_account(str(entry.get("account") or ""), company)
		debit = as_float(entry.get("debit"), f"accounts[{index}].debit")
		credit = as_float(entry.get("credit"), f"accounts[{index}].credit")
		# Not `as_float(...) or 1.0`: `as_float` cannot distinguish an absent rate from
		# an explicit 0, so that idiom turned a stated zero into 1.0 on the line above
		# the refusal that names it — and the `<= 0` half below could never fire for the
		# value it exists to catch. Branch on the raw value; a stated 0 now reaches the
		# guard and is refused rather than silently becoming par.
		raw_rate = entry.get("exchange_rate")
		rate = as_float(raw_rate, f"accounts[{index}].exchange_rate") if raw_rate not in (None, "") else 1.0
		if rate <= 0:
			raise ToolError(
				f"accounts[{index}] ({account}) has exchange_rate {rate}; a rate must be "
				"positive. Nothing was created."
			)
		debit_in_account_currency = as_float(
			entry.get("debit_in_account_currency"), f"accounts[{index}].debit_in_account_currency"
		)
		credit_in_account_currency = as_float(
			entry.get("credit_in_account_currency"), f"accounts[{index}].credit_in_account_currency"
		)
		# A caller who gave only the account-currency side means the same line as
		# one who gave only the company-currency side. Filling the other in here
		# rather than refusing keeps `debit`/`credit` the field pair every message
		# in this module talks about, while accepting the pair ERPNext actually
		# reads.
		#
		# And where both were given, the account-currency one wins the arithmetic,
		# because it is the one ERPNext multiplies out on validate. Letting the
		# two disagree would mean this module's double-entry check ran on one set
		# of numbers and the posting on another — an entry that balances here and
		# does not on the site.
		debit = _reconciled_amount(debit, debit_in_account_currency, rate, account, index, "debit")
		credit = _reconciled_amount(credit, credit_in_account_currency, rate, account, index, "credit")
		if debit and credit:
			raise ToolError(
				f"accounts[{index}] ({account}) sets both debit and credit; a "
				"Journal Entry line is one or the other"
			)
		if not debit and not credit:
			raise ToolError(f"accounts[{index}] ({account}) has neither a debit nor a credit")
		if debit < 0 or credit < 0:
			raise ToolError(
				f"accounts[{index}] ({account}) has a negative amount. Move the sign "
				"to the other side of the entry instead."
			)
		if frappe.db.get_value("Account", account, "is_group"):
			raise ToolError(
				f"accounts[{index}] ({account}) is a group account; ERPNext posts only to leaf accounts"
			)
		if not entry.get("exchange_rate"):
			_assert_company_currency(account, company, index)
		line = {key: value for key, value in entry.items() if key in _LINE_FIELDS}
		line["account"] = account
		line["debit"] = debit
		line["credit"] = credit
		line.update(
			_account_currency_amounts(
				debit, credit, rate, debit_in_account_currency, credit_in_account_currency
			)
		)
		line.update(_validated_dimensions(entry.get(_DIMENSIONS_KEY), index))
		lines.append(line)
	return lines


def _reconciled_amount(
	amount: float, in_account_currency: float, rate: float, account: str, index: int, side: str
) -> float:
	"""One side's company-currency figure, agreed with its account-currency twin.

	Absent either way it stays absent — a line has one side, and the other's zero
	is not a disagreement. Given only in the account's currency it is multiplied
	out. Given both ways and they differ, it is a refusal rather than a choice:
	the caller sent two numbers that cannot both be true, and the one this module
	would have to discard is the one the ledger would actually have posted.
	"""
	if not in_account_currency:
		return amount
	converted = round(in_account_currency * rate, 2)
	if amount and round(amount, 2) != converted:
		raise ToolError(
			f"accounts[{index}] ({account}) says {side} {amount} and "
			f"{side}_in_account_currency {in_account_currency} at exchange_rate {rate}, which "
			f"comes to {converted}. ERPNext posts the account-currency figure times the rate, so "
			f"these two cannot both be right. Send one of them, or send a pair that agrees. "
			"Nothing was created."
		)
	return converted


def _assert_company_currency(account: str, company: str, index: int) -> None:
	"""Refuse a foreign-currency line that came without an exchange rate.

	The `*_in_account_currency` columns this module now fills in are only equal to
	`debit`/`credit` when the account is in the company's own currency. On a
	foreign account with no rate to divide by, the honest answer is that this app
	does not know what the amount is in that currency — and ERPNext, which looks a
	rate up for itself when it sees `exchange_rate` of 1, would multiply that
	guess back out and post a converted figure nobody chose. Refusing costs a
	round trip; the alternative is a posting that is wrong in a currency the
	caller never mentioned.

	Silent where the site does not have the fields to answer with, because a
	check that cannot be made must not become a check that always fails.
	"""
	if not compat.has_field("Account", "account_currency"):
		return
	account_currency = str(frappe.db.get_value("Account", account, "account_currency") or "").strip()
	if not account_currency:
		return
	company_currency = str(frappe.db.get_value("Company", company, "default_currency") or "").strip()
	if not company_currency or account_currency == company_currency:
		return
	raise ToolError(
		f"accounts[{index}] ({account}) is held in {account_currency} and {company} keeps its "
		f"books in {company_currency}, so this line needs an exchange_rate — the rate that "
		f"turns {account_currency} into {company_currency} on the posting date. Pass it, "
		"together with the amount in the account's own currency as "
		"debit_in_account_currency or credit_in_account_currency. Nothing was created."
	)


def _account_currency_amounts(
	debit: float,
	credit: float,
	rate: float,
	debit_in_account_currency: float,
	credit_in_account_currency: float,
) -> dict:
	"""The `*_in_account_currency` pair for one line. Never omitted. See the module docstring.

	At rate 1 — every line on a single-currency site, which is almost every line —
	the account-currency amount IS the company-currency amount, and it is copied
	rather than computed so no rounding can put a cent between the two columns of
	the same number. Only a genuine foreign-currency line divides, and a caller
	who supplied the account-currency figure themselves keeps it: they know what
	the invoice said, and this does not.
	"""
	if not debit_in_account_currency:
		debit_in_account_currency = debit if rate == 1 else round(debit / rate, 2) if debit else 0.0
	if not credit_in_account_currency:
		credit_in_account_currency = credit if rate == 1 else round(credit / rate, 2) if credit else 0.0
	return {
		"debit_in_account_currency": debit_in_account_currency,
		"credit_in_account_currency": credit_in_account_currency,
		"exchange_rate": rate,
	}


def _validated_dimensions(raw, index: int) -> dict:
	"""Check a line's `dimensions` object against this site's own meta.

	Two checks, both of which exist because the failure they prevent is silent.
	The field has to exist on Journal Entry Account — a dimension the operator has
	not created yet would otherwise be written to a Document attribute that never
	reaches a column, and the entry would look filed and not be. And where the
	field is a Link, the value has to be a record of what it links to, because
	ERPNext's own link validation runs on *submit*, so a bad value here produces a
	draft that cannot be posted rather than a call that failed.
	"""
	if raw in (None, ""):
		return {}
	if not isinstance(raw, dict):
		raise ToolError(
			f"accounts[{index}].{_DIMENSIONS_KEY} must be an object of fieldname → "
			f'value, e.g. {{"member": "Member-01", "bbch_stage": "BBCH-8"}}; got '
			f"{type(raw).__name__}"
		)

	out = {}
	for key in sorted(raw):
		fieldname = str(key or "").strip()
		field = compat.field_meta(_LINE_DOCTYPE, fieldname)
		if field is None:
			raise ToolError(
				f"accounts[{index}].{_DIMENSIONS_KEY} has no field {fieldname!r} on "
				f"{_LINE_DOCTYPE}. An accounting dimension only becomes a field once it "
				"exists — create it with create_accounting_dimension, which adds the "
				"Link field to this doctype. Nothing was created."
			)
		value = raw[key]
		text = "" if value is None else str(value).strip()
		options = str(field.get("options") or "")
		if text and str(field.get("fieldtype") or "") == "Link" and options:
			if not frappe.db.exists(options, text):
				raise ToolError(
					f"accounts[{index}].{_DIMENSIONS_KEY}.{fieldname} is {text!r}, which "
					f"is not a {options} on this site. Dimension values are {options} "
					"records — create one with create_dimension_value. Nothing was created."
				)
		out[fieldname] = text or None
	return out


def assert_balanced(lines: list[dict]) -> tuple[float, float]:
	total_debit = sum(float(line["debit"]) for line in lines)
	total_credit = sum(float(line["credit"]) for line in lines)
	if abs(total_debit - total_credit) > BALANCE_TOLERANCE:
		raise ToolError(
			f"debits ({round(total_debit, 2)}) do not equal credits "
			f"({round(total_credit, 2)}); difference "
			f"{round(total_debit - total_credit, 2)}. Nothing was created."
		)
	return total_debit, total_credit


# ── 12. submit_journal_entry ────────────────────────────────────────────────
def submit_journal_entry(args: dict) -> ToolResult:
	"""Submit an existing DRAFT Journal Entry (docstatus 0 → 1).

	This is the tool that moves a balance, and it is the narrowest possible
	version of that power: it takes a name, not a document. It cannot create the
	entry it submits, so enabling it means "post things a human or an earlier
	tool call already wrote down", never "post something new right now".
	"""
	name = as_str(args, "name", required=True)
	doc = _journal_entry(name)
	if int(doc.docstatus or 0) == 1:
		raise ToolError(f"Journal Entry {name} is already submitted")
	if int(doc.docstatus or 0) == 2:
		raise ToolError(f"Journal Entry {name} is cancelled and cannot be submitted")

	doc.submit()
	data = {
		"name": doc.name,
		"docstatus": 1,
		"docstatus_label": "submitted",
		"company": doc.company,
		"posting_date": str(doc.posting_date),
		"total_debit": doc.total_debit,
		"total_credit": doc.total_credit,
		"gl_entries_created": frappe.db.count(
			"GL Entry", {"voucher_type": "Journal Entry", "voucher_no": doc.name}
		),
	}
	return ToolResult(
		data,
		f"submitted Journal Entry {doc.name} ({doc.company}, {doc.posting_date}, {doc.total_debit})",
		docstatus_delta="0 → 1 (submitted)",
	)


# ── 75. bulk_submit_journal_entries ─────────────────────────────────────────
def bulk_submit_journal_entries(args: dict) -> ToolResult:
	"""Submit many draft Journal Entries in one call, one document at a time.

	WHY THIS EXISTS AND `submit_journal_entry` IS NOT ENOUGH. A migration weekend
	produces drafts in the hundreds — a chart imported, a year of history keyed in,
	an opening balance per account. Posting them one MCP round trip at a time is
	not a different amount of work, it is a different *kind* of work, and the thing
	that actually goes wrong is a human losing track at entry four hundred and
	stopping without knowing which ones went.

	IT CHECKS `submit_journal_entry`'s SWITCH TOO, and fails before touching
	anything if that one is off. That switch is where an operator decided whether
	an AI client may move a balance at all; a second door into the same room with
	its own lock would make the decision meaningless. Same reasoning as
	`submit_member_event`.

	ONE DOCUMENT'S FAILURE IS NOT THE BATCH'S. Each submit runs in its own
	transaction: committed on success, rolled back on failure, then the loop moves
	on. This is the one place in this app that commits mid-call, and it is
	deliberate — the alternative is a batch of five hundred where number four
	hundred fails and the request rolls back the three hundred and ninety-nine
	postings that were fine, which is both surprising and much harder to recover
	from than a report saying which one to look at. It is also what Frappe's own
	bulk submit does.

	NOTHING IS RE-SUBMITTED. An already-submitted entry is reported `ok` and
	`skipped`, not an error: a caller retrying a batch that half-succeeded wants
	the rest posted, not four hundred error messages about work that is already
	done.
	"""
	if not settings.tool_enabled("submit_journal_entry"):
		raise ToolError(
			"this posts Journal Entries, and the submit_journal_entry tool is switched off on "
			"this site. That switch is where an operator decides whether an AI client may move "
			"a balance, so this tool honours it too. An operator must tick "
			"'allow_submit_journal_entry' in ERPNext MCP Settings. Nothing was submitted."
		)
	names = _validated_docnames(args.get("names"))

	results = []
	for name in names:
		results.append(_submit_one(name))

	submitted = [row for row in results if row["ok"] and not row["skipped"]]
	skipped = [row for row in results if row["skipped"]]
	failed = [row for row in results if not row["ok"]]
	data = {
		"total": len(results),
		"submitted": len(submitted),
		"skipped": len(skipped),
		"failed": len(failed),
		"results": results,
		"submitted_names": [row["name"] for row in submitted],
		"failed_names": [row["name"] for row in failed],
		"note": (
			"Each entry was submitted in its own transaction, so the ones that posted have "
			"posted whatever happened to the rest. Nothing here was rolled back by a later "
			"failure, and nothing that failed left a partial posting behind."
		),
		"next_step": (
			f"{len(failed)} entr{'y' if len(failed) == 1 else 'ies'} failed — read `results` for "
			"the reason each gave, fix them, and call this again with just those names. Names "
			"that already posted are safe to include; they come back skipped."
			if failed
			else "Every entry in the batch is posted or was already posted. Nothing is outstanding."
		),
	}
	return ToolResult(
		data,
		f"bulk submit: {len(submitted)} posted, {len(skipped)} already submitted, "
		f"{len(failed)} failed, of {len(results)} asked for",
		docstatus_delta=f"0 → 1 (submitted) × {len(submitted)}",
	)


def _validated_docnames(raw) -> list[str]:
	"""The `names` argument: a non-empty list of docnames, de-duplicated in order."""
	if not isinstance(raw, list) or not raw:
		raise ToolError(
			"names must be a non-empty list of Journal Entry docnames, e.g. "
			'["ACC-JV-2026-00042", "ACC-JV-2026-00043"]. get_journal_entries with '
			"docstatus='draft' lists the ones waiting to be posted."
		)
	if len(raw) > _MAX_BULK:
		raise ToolError(
			f"names has {len(raw)} entries and the limit is {_MAX_BULK} per call. A batch this "
			"size is worth splitting anyway: one failure in the middle is easier to find in "
			f"{_MAX_BULK} results than in {len(raw)}. Nothing was submitted."
		)
	out = []
	for index, value in enumerate(raw, start=1):
		name = str(value or "").strip()
		if not name:
			raise ToolError(f"names[{index}] is empty. Nothing was submitted.")
		if name not in out:
			out.append(name)
	return out


def _submit_one(name: str) -> dict:
	"""Submit one entry, returning a row rather than raising. See `bulk_submit_journal_entries`."""
	row = {"name": name, "ok": False, "skipped": "", "error": None, "docstatus": None}
	try:
		if not frappe.db.exists("Journal Entry", name):
			row["error"] = f"no Journal Entry named {name!r}"
			return row
		doc = frappe.get_doc("Journal Entry", name)
		docstatus = int(doc.docstatus or 0)
		if docstatus == 1:
			row.update(ok=True, skipped="already_submitted", docstatus=1)
			return row
		if docstatus == 2:
			row.update(docstatus=2, error=f"Journal Entry {name} is cancelled and cannot be submitted")
			return row

		doc.submit()
		frappe.db.commit()
		row.update(ok=True, docstatus=1)
		return row
	except Exception as exc:
		# Rolling back here is what makes the next document's submit run against a
		# clean transaction rather than one poisoned by this one's half-write.
		frappe.db.rollback()
		row["error"] = f"{type(exc).__name__}: {exc}"
		return row


# ── 76. delete_draft_journal_entry ──────────────────────────────────────────
def delete_draft_journal_entry(args: dict) -> ToolResult:
	"""Delete a DRAFT Journal Entry outright. Drafts only, and it says what it deleted.

	THE GAP THIS FILLS. `cancel_journal_entry` refuses a draft, correctly — there
	is nothing to reverse, because a draft has moved no balance. But that left an
	unwanted draft with no MCP path at all, and a tool that produced four hundred
	drafts and no way to withdraw them is a tool that makes work rather than doing
	it. So: drafts, and only drafts. A submitted entry is a posting and gets
	cancelled, never deleted, whatever a caller asks for.

	IT IS A REAL DELETE. `frappe.delete_doc`, no soft-delete flag, nothing left in
	the table. What survives is the audit row, which is why `reason` is mandatory
	and why the response — and therefore the log's summary — carries the entry's
	company, date, totals and line count. Once this returns, that row is the only
	description of the document that ever existed, so it has to be one somebody
	can read a year later.
	"""
	name = as_str(args, "name", required=True)
	reason = as_str(args, "reason", required=True)
	if len(reason) < 4:
		raise ToolError("reason must be a real explanation, not a placeholder. Nothing was deleted.")
	doc = _journal_entry(name)
	docstatus = int(doc.docstatus or 0)
	if docstatus == 1:
		raise ToolError(
			f"Journal Entry {name} is submitted: it has written GL Entries and moved balances, "
			"and deleting it would take those balances with it and leave nothing saying why. "
			"Reverse it with cancel_journal_entry, which writes the reversing entries and keeps "
			"the record. Nothing was deleted."
		)
	if docstatus == 2:
		raise ToolError(
			f"Journal Entry {name} is cancelled. ERPNext keeps cancelled entries and their "
			"reversing GL rows on purpose — the pair is the evidence that a posting was made "
			"and undone. Deleting one leaves an audit trail with a hole in it. Nothing was "
			"deleted."
		)

	# Read everything worth keeping BEFORE the document stops existing.
	deleted = {
		"name": doc.name,
		"company": doc.company,
		"posting_date": str(doc.posting_date or ""),
		"voucher_type": doc.get("voucher_type"),
		"user_remark": doc.get("user_remark"),
		"total_debit": float(doc.get("total_debit") or 0),
		"total_credit": float(doc.get("total_credit") or 0),
		"line_count": len(doc.get("accounts") or []),
		"accounts": [
			{
				"account": row.get("account"),
				"debit": float(row.get("debit") or 0),
				"credit": float(row.get("credit") or 0),
			}
			for row in (doc.get("accounts") or [])
		],
	}
	frappe.delete_doc("Journal Entry", name, ignore_permissions=False)

	data = {
		"deleted": deleted,
		"reason": reason,
		"gl_entries_removed": 0,
		"note": (
			"A draft writes no GL Entries, so no balance changed when this was created and none "
			"changed when it was deleted. The MCP Action Log row for this call is now the only "
			"record that the entry existed; it carries the lines above and this reason."
		),
	}
	return ToolResult(
		data,
		f"deleted draft Journal Entry {name} ({deleted['company']}, {deleted['posting_date']}, "
		f"{deleted['line_count']} line(s), {deleted['total_debit']} debit) — {reason}",
		docstatus_delta="0 (draft) → deleted",
	)


# ── 13. cancel_journal_entry ────────────────────────────────────────────────
def cancel_journal_entry(args: dict) -> ToolResult:
	"""Cancel a submitted Journal Entry (docstatus 1 → 2), recording why.

	`reason` is mandatory and is written twice: to the document's own comment
	thread, where an accountant looking at the JE will see it, and to the audit
	log, where it survives even if the document is later deleted. A cancellation
	nobody can explain is the thing that makes a year-end close miserable.
	"""
	name = as_str(args, "name", required=True)
	reason = as_str(args, "reason", required=True)
	if len(reason) < 4:
		raise ToolError("reason must be a real explanation, not a placeholder")
	doc = _journal_entry(name)
	if int(doc.docstatus or 0) == 0:
		raise ToolError(
			f"Journal Entry {name} is a draft, not a submitted entry. Drafts affect "
			"no balance; delete it in ERPNext if it is not wanted."
		)
	if int(doc.docstatus or 0) == 2:
		raise ToolError(f"Journal Entry {name} is already cancelled")

	doc.cancel()
	try:
		doc.add_comment("Comment", f"Cancelled via MCP (erpnext_mcp). Reason: {reason}")
	except Exception:
		# The cancellation itself succeeded and is the operation the caller
		# asked for; a failed comment must not report it as a failure. The
		# reason is in the audit log regardless.
		frappe.log_error(
			title="erpnext_mcp: could not attach cancellation reason comment",
			message=compat.traceback_text(),
		)

	data = {
		"name": doc.name,
		"docstatus": 2,
		"docstatus_label": "cancelled",
		"company": doc.company,
		"posting_date": str(doc.posting_date),
		"reason": reason,
		"note": ("ERPNext keeps cancelled entries and their reversing GL rows; nothing was deleted."),
	}
	return ToolResult(
		data,
		f"cancelled Journal Entry {doc.name} — {reason}",
		docstatus_delta="1 → 2 (cancelled)",
	)


def _journal_entry(name: str):
	if not frappe.db.exists("Journal Entry", name):
		raise ToolError(f"no Journal Entry named {name!r}")
	return frappe.get_doc("Journal Entry", name)


# ── 126. update_journal_entry_party ─────────────────────────────────────────
#: Account types on which naming a party is meaningful. Receivable and Payable
#: are the two ERPNext itself resolves a party against; Equity is the one an
#: owner draw or a member distribution lands on and is the whole reason a family
#: transfer has a party at all; blank is by far the commonest — an ordinary
#: expense or income account usually carries no `account_type`, and refusing
#: those would refuse the case this tool was built for.
PARTY_BEARING_ACCOUNT_TYPES = ("", "Receivable", "Payable", "Equity")

#: Account types where a party is not merely unusual but wrong, and which no
#: escape hatch opens. `Round Off` and the rounding/write-off accounts a company
#: nominates are written by ERPNext itself to absorb a fraction of a cent; a
#: party on one attributes a rounding artefact to a person. `Bank` and `Cash` are
#: the operation's own money, and a party there is a reconciliation error that
#: will make an ageing report claim a person owes the bank balance.
PARTY_FORBIDDEN_ACCOUNT_TYPES = ("Round Off", "Bank", "Cash")

#: Party types this app itself registers, named here only so the refusal for an
#: unregistered one can point at the tool that registers them.
_CUSTOM_PARTY_TYPES = ("Family", "Contact")


def update_journal_entry_party(args: dict) -> ToolResult:
	"""Set or change `party_type` and `party` on ONE line of a Journal Entry.

	WHY THIS EXISTS. A payment goes out of a shared account and only afterwards
	does anybody establish which of two sons it was for. The posting is right —
	right account, right amount, right date — and one attribution column is empty
	or wrong. The alternatives are cancel-and-repost, which replaces a clerical
	correction with a cancelled voucher, a reversing pair and a new number that no
	statement reconciles against; or the Desk, which is the thing an MCP server
	exists so nobody has to open. So: one line, two columns, a mandatory reason.

	WHAT IT WILL NOT TOUCH. Account, debit, credit, date, cost center, remark.
	They are not arguments. Every balance in the ledger is arithmetically
	identical after this call, which is what makes editing a submitted document
	defensible at all — this is attribution, not restatement.

	IT WRITES IN TWO PLACES BECAUSE THE PARTY LIVES IN TWO PLACES. `tabJournal
	Entry Account` is what the voucher shows; `tabGL Entry` is what every ageing
	report, party ledger and statement of account reads. Updating only the first
	produces a voucher that says one thing and a report that says another, which
	is worse than not having edited it — the two disagree and nothing says which
	is right. So a submitted entry whose GL rows cannot be matched to the line
	with certainty is REFUSED before anything is written, rather than half-done
	with a warning. `gl_link_for_line` explains how the match is made and every
	way it declines to make one.

	v0.13.0 DID THIS WRONG AND v0.14.0 FIXES IT. It looked the GL rows up by
	`voucher_detail_no == line.name`, which is right for a Sales Invoice Item and
	wrong for a Journal Entry: ERPNext fills that column from the line's
	`reference_detail_no`, which is empty on an ordinary line. Every call against
	a submitted entry therefore matched zero rows, updated the voucher only, and
	returned a warning blaming the site. If you ran v0.13.0's version of this tool
	against a submitted entry, the ledger still says what it said before —
	investigate_je_gl_link will show you which entries are in that state.

	v0.15.0 FIXES WHAT v0.14.0'S FIX LEFT BEHIND. v0.14.0 corrected the matcher but
	kept an idempotence check that read only the VOUCHER: if the line already said
	what was asked for, it refused with "nothing to change". On an entry damaged by
	v0.13.0 that is precisely wrong — the voucher was updated, the ledger was not,
	and the one state the tool most needed to repair was the one it declined to
	look at, while telling the caller everything was fine. The check now asks BOTH
	tables. Nothing to change means nothing to change anywhere. A voucher that
	agrees over a ledger that does not is a GL-only repair, it proceeds, and the
	result says `gl_only_update: true` so nobody mistakes it for a fresh
	attribution. `force_gl_sync=true` writes the GL rows even where nothing
	disagrees, for an operator who wants the write to be an explicit act rather
	than a consequence of a comparison.

	A DRAFT GOES THROUGH THE DOCUMENT INSTEAD. A draft has written no GL Entries
	and can still be saved normally, so it is saved normally and every validation
	on the doctype runs. The direct write is reserved for the case that has no
	other door.

	REFUSES: a cancelled entry (evidence with a hole in it); a line index outside
	the entry; a rounding or write-off line ERPNext wrote itself; a bank or cash
	line; a party type this site has not registered; a party that is not a record
	in it; a change that changes nothing; and a submitted line whose GL rows are
	ambiguous, merged or missing. Refuses an account whose type does not normally
	carry a party unless `allow_non_party_account=true` says the caller meant it,
	and refuses an unmatchable GL unless `allow_unmatched_gl=true` says the caller
	will accept a voucher and a ledger that disagree — both refusals exist to
	catch a mistake, not to become one.

	Writes NO journal entry and reverses nothing.
	"""
	journal_entry = as_str(args, "journal_entry", required=True)
	reason = as_str(args, "reason", required=True)
	if len(reason) < 8:
		raise ToolError(
			"reason must be a real explanation of why the attribution is being changed — what "
			"establishes that this line belongs to this party. It is the part of this record "
			"nobody can reconstruct later, and it is the only thing distinguishing a correction "
			"from a rewrite. Nothing was changed."
		)

	line_index = as_int(args, "line_index")
	if line_index is None:
		raise ToolError(
			"line_index is required: which line of the entry to attribute, counting from 1 the "
			"way ERPNext numbers them. get_journal_entry lists them. Nothing was changed."
		)

	if "party_type" not in args or "party" not in args:
		raise ToolError(
			"party_type and party must BOTH be given. ERPNext resolves `party` as a Dynamic Link "
			"through `party_type`, so one without the other is not half an answer — it is an "
			"unresolvable reference. Pass both, or pass both as empty strings to clear the "
			"attribution. Nothing was changed."
		)
	party_type = as_str(args, "party_type")
	party = as_str(args, "party")
	if bool(party_type) != bool(party):
		raise ToolError(
			f"party_type is {party_type!r} and party is {party!r}. Set both to name somebody, or "
			"both to empty strings to clear the attribution. Nothing was changed."
		)

	doc = _journal_entry(journal_entry)
	docstatus = int(doc.get("docstatus") or 0)
	if docstatus == 2:
		raise ToolError(
			f"Journal Entry {doc.name} is cancelled. A cancelled entry and its reversing GL rows "
			"are the evidence that a posting was made and undone, and editing one leaves that "
			"evidence saying something that never happened. If the correct attribution matters, "
			"it belongs on the entry that replaced this one. Nothing was changed."
		)

	rows = list(doc.get("accounts") or [])
	if line_index < 1 or line_index > len(rows):
		raise ToolError(
			f"line_index {line_index} is outside Journal Entry {doc.name}, which has {len(rows)} "
			f"line(s). They are numbered from 1. get_journal_entry lists them with their accounts "
			"and amounts. Nothing was changed."
		)
	line = rows[line_index - 1]
	account = str(line.get("account") or "")

	_assert_line_takes_a_party(doc, line, line_index, account, party_type, args)
	if party_type:
		_assert_party_exists(party_type, party)

	before = {
		"party_type": str(line.get("party_type") or "") or None,
		"party": str(line.get("party") or "") or None,
	}
	after = {"party_type": party_type or None, "party": party or None}

	link = (
		gl_link_for_line(doc.name, rows, line_index)
		if docstatus == 1
		else {"rows": [], "basis": "draft — no GL Entry rows exist yet", "exact": False, "blocker": ""}
	)
	gl_rows = link["rows"]

	# ── the v0.15.0 idempotence fix ─────────────────────────────────────────
	#
	# v0.14.0 refused here whenever the VOUCHER already read what was asked for,
	# on the reasonable-sounding grounds that there was nothing to change. That
	# was wrong in exactly the case this tool most needs to handle, and the case
	# v0.13.0 created: a line whose voucher was updated and whose GL rows were
	# not. On one of those, the voucher already reads the requested party, the
	# ledger still reads the old one, and "nothing to change" was the one answer
	# that left the ledger wrong AND told the caller it was fine.
	#
	# So the check now asks both tables. Nothing to change means nothing to change
	# ANYWHERE — voucher and ledger agreeing with the request. A voucher that
	# agrees over a ledger that does not is a GL-only repair, and it proceeds.
	gl_drifted = [row for row in gl_rows if _party_of(row) != after]
	gl_only_update = before == after and bool(gl_drifted)
	forced = as_bool(args, "force_gl_sync", False)

	if before == after and not gl_drifted and not forced:
		raise ToolError(
			f"line {line_index} of {doc.name} already reads "
			f"{before['party_type'] or 'no party type'} / {before['party'] or 'no party'}"
			+ (
				f", and so do the {len(gl_rows)} GL Entry row(s) it posted. "
				if gl_rows
				else ". It has posted no GL Entry rows to disagree with it. "
			)
			+ "Nothing to change, and nothing was changed."
		)

	accepted_unmatched = False
	if link["blocker"]:
		if not as_bool(args, "allow_unmatched_gl", False):
			raise ToolError(
				f"{link['blocker']} The voucher and the general ledger would end up saying "
				"different things about who this line belongs to, which is worse than leaving "
				"it as it is. Run investigate_je_gl_link on "
				f"{doc.name} to see every line beside every GL row. If you have read that and "
				"still want the voucher changed on its own, pass allow_unmatched_gl=true — the "
				"result will carry the disagreement as a warning. Nothing was changed."
			)
		accepted_unmatched = True

	plan = {
		"journal_entry": doc.name,
		"company": doc.get("company"),
		"posting_date": str(doc.get("posting_date") or ""),
		"docstatus": docstatus,
		"docstatus_label": "submitted" if docstatus == 1 else "draft",
		"line_index": line_index,
		"line_name": line.get("name"),
		"account": account,
		"debit": round(float(line.get("debit") or 0), 2),
		"credit": round(float(line.get("credit") or 0), 2),
		"before": before,
		"after": after,
		"reason": reason,
		"gl_entries_matched": len(gl_rows),
		"gl_entries_drifted": len(gl_drifted),
		"gl_match_basis": link["basis"],
		"gl_match_exact": bool(link["exact"]),
		"gl_only_update": bool(gl_only_update),
		"force_gl_sync": bool(forced),
		"tables": (
			["Journal Entry Account", "GL Entry"] if docstatus == 1 and gl_rows else ["Journal Entry"]
		),
	}
	if accepted_unmatched:
		plan["gl_match_problem"] = link["blocker"]
	if gl_drifted:
		plan["gl_drift"] = [
			{
				"gl_entry": row.get("name"),
				"account": row.get("account"),
				"party_type": str(row.get("party_type") or "") or None,
				"party": str(row.get("party") or "") or None,
			}
			for row in gl_drifted
		]

	if as_bool(args, "dry_run", False):
		return ToolResult(
			{
				**plan,
				"dry_run": True,
				"updated": False,
				"note": (
					"Nothing was written. Call again without dry_run to make the change. No "
					"balance would move either way: only party_type and party are touched."
					+ (
						" The voucher ALREADY reads what was asked for and "
						f"{len(gl_drifted)} GL Entry row(s) do not — this would be a GL-only "
						"repair, which is exactly the damage class v0.13.0 left behind."
						if gl_only_update
						else ""
					)
				),
			},
			f"dry run: would attribute line {line_index} of {doc.name} ({account}) to "
			f"{after['party_type'] or 'nobody'} / {after['party'] or 'nobody'}"
			+ (f", and {len(gl_rows)} GL Entry row(s) with it" if gl_rows else ""),
		)

	if docstatus == 0:
		# A draft can still be saved, so it is saved — the doctype's own validation
		# runs and there are no GL rows to keep in step.
		doc.accounts[line_index - 1].party_type = party_type or None
		doc.accounts[line_index - 1].party = party or None
		doc.save()
		updated_gl = 0
	else:
		frappe.db.set_value(
			_LINE_DOCTYPE,
			line.get("name"),
			{"party_type": party_type or None, "party": party or None},
		)
		for gl_row in gl_rows:
			frappe.db.set_value(
				"GL Entry",
				gl_row.get("name"),
				{"party_type": party_type or None, "party": party or None},
			)
		updated_gl = len(gl_rows)

	comment = _record_party_change(doc, line_index, before, after, reason)

	data = {
		**plan,
		"updated": True,
		"gl_entries_updated": updated_gl,
		"comment_added": comment,
		"note": (
			(
				"THE VOUCHER ALREADY SAID THIS AND THE LEDGER DID NOT. This was a GL-only "
				f"repair: {len(gl_drifted)} GL Entry row(s) that read "
				f"{gl_drifted[0].get('party') or 'no party'} now read "
				f"{after['party'] or 'no party'}, and the voucher was not touched because it was "
				"already right. That state is the signature of v0.13.0's broken party tool, "
				"which wrote the voucher and silently failed to write the ledger. "
				if gl_only_update
				else ""
			)
			+ "NO balance moved. Account, debit, credit and date are not arguments to this tool, "
			"so the trial balance after this call is arithmetically identical to the one before "
			"it. What changed is who the line is attributed to"
			+ (
				f", in both the voucher and the {updated_gl} GL Entry row(s) every ageing report reads."
				if updated_gl
				else (
					" on the voucher ONLY — see the warning."
					if accepted_unmatched
					else " on a draft, which has written no GL Entries yet."
				)
			)
		),
		"next_step": (
			"get_journal_entry shows the line as it now reads. A Family party stays excluded "
			"from generate_1099_prefill and is reported in its excluded counts — a transfer to a "
			"relative is not compensation for services, and attributing one correctly does not "
			"make it reportable."
			if party_type == "Family"
			else "get_journal_entry shows the line as it now reads."
		),
	}
	if gl_only_update:
		data["repaired_drift"] = True
	if accepted_unmatched:
		data["warning"] = (
			f"THE VOUCHER AND THE LEDGER NOW DISAGREE, and you asked for that with "
			f"allow_unmatched_gl=true. {link['blocker']} Journal Entry {doc.name} line "
			f"{line_index} now reads {after['party'] or 'no party'}; every ageing report, party "
			f"ledger and statement of account still reads {before['party'] or 'no party'}, "
			"because no GL Entry row was written. Whoever reconciles this next needs to know."
		)
	return ToolResult(
		data,
		(
			f"re-synced the ledger for line {line_index} of {doc.name} ({account}) to "
			f"{after['party_type'] or 'nobody'} / {after['party'] or 'nobody'} — the voucher "
			f"already said so and {len(gl_drifted)} GL Entry row(s) did not"
			if gl_only_update
			else (
				f"attributed line {line_index} of {doc.name} ({account}) to "
				f"{after['party_type'] or 'nobody'} / {after['party'] or 'nobody'}"
				f" — was {before['party_type'] or 'nobody'} / {before['party'] or 'nobody'}"
				f", {updated_gl} GL Entry row(s) updated"
			)
		)
		+ f" — {reason}",
		docstatus_delta="",
	)


def _party_of(row) -> dict:
	"""One row's attribution, in the shape `before`/`after` use.

	Shared so the drift comparison and the idempotence check cannot disagree about
	whether an empty string and a None are the same attribution. They are.
	"""
	return {
		"party_type": str(row.get("party_type") or "") or None,
		"party": str(row.get("party") or "") or None,
	}


def _assert_line_takes_a_party(doc, line, line_index: int, account: str, party_type: str, args: dict) -> None:
	"""Refuse a line that is not somebody's, and one nobody wrote on purpose.

	Two separate refusals with two different characters. The forbidden types are
	absolute: a party on a Round Off line attributes a fraction of a cent that
	ERPNext invented to a person, and a party on a bank line makes an ageing
	report claim they owe the account balance. Neither has a legitimate reading,
	so neither has an escape hatch.

	The account-type check is the softer one, and it is soft on purpose. Naming a
	party is *normal* on Receivable, Payable, Equity and untyped accounts and
	*unusual* everywhere else — but unusual is not wrong, and a refusal a caller
	cannot get past is how a safety gate turns into the failure. So it names the
	argument that says "I meant it", and the result carries a warning rather than
	pretending the unusual case is ordinary.

	Clearing an attribution (`party_type=""`) skips both: taking a party OFF a
	line that should never have had one is the correction, not the mistake.
	"""
	if _is_round_off_line(doc, line):
		raise ToolError(
			f"line {line_index} of {doc.name} is the rounding/write-off line on {account}. "
			"ERPNext wrote it itself to absorb a fraction of a cent, and attributing that "
			"fraction to a person is not a fact about the person. If the entry is genuinely "
			"somebody's, the line that says so is the one carrying the amount. Nothing was "
			"changed."
		)
	if not party_type:
		return

	account_type = str(frappe.db.get_value("Account", account, "account_type") or "")
	if account_type in PARTY_FORBIDDEN_ACCOUNT_TYPES:
		raise ToolError(
			f"line {line_index} of {doc.name} posts to {account}, whose account type is "
			f"{account_type!r}. That account is the operation's own money, and a party on it "
			"makes every ageing report claim the party owes its balance. The party belongs on "
			"the line facing it. Nothing was changed."
		)
	if account_type not in PARTY_BEARING_ACCOUNT_TYPES and not as_bool(
		args, "allow_non_party_account", False
	):
		raise ToolError(
			f"line {line_index} of {doc.name} posts to {account}, whose account type is "
			f"{account_type!r}. A party is normally carried on a Receivable, Payable or Equity "
			f"account, or on one with no account type at all — {', '.join(PARTY_BEARING_ACCOUNT_TYPES[1:])} "
			"or blank. This may still be what you mean: an expense attributed to the person it "
			"was incurred for is a real thing to record. Pass allow_non_party_account=true to "
			"say so, and the result will carry a warning rather than a refusal. Nothing was "
			"changed."
		)


def _is_round_off_line(doc, line) -> bool:
	"""Is this the line ERPNext wrote itself, rather than one somebody meant?

	Three tests because three ERPNext versions answer differently and any one of
	them alone lets the line through on a site that spells it another way: the
	account's own `account_type`, the company's nominated round-off and write-off
	accounts, and the account name. The last is the weakest and is checked last.
	"""
	account = str(line.get("account") or "")
	if not account:  # pragma: no cover - a line with no account cannot be indexed to
		return False
	if str(frappe.db.get_value("Account", account, "account_type") or "") == "Round Off":
		return True

	company = str(doc.get("company") or "")
	if company:
		nominated = [
			str(frappe.db.get_value("Company", company, field) or "")
			for field in ("round_off_account", "write_off_account")
			if compat.has_field("Company", field)
		]
		if account in [entry for entry in nominated if entry]:
			return True
	name = str(frappe.db.get_value("Account", account, "account_name") or account).lower()
	return "round off" in name or "write off" in name


def _assert_party_exists(party_type: str, party: str) -> None:
	"""A party type this site has registered, and a party that is a record in it.

	ERPNext resolves `party` as a Dynamic Link THROUGH `party_type`: the party
	type's name has to be a DocType and the party has to be a row in it. Both
	halves are checked here rather than left to the insert, because the framework
	error for the second is "Could not find Party: Alex Polehn" with no clue that
	the register it looked in was the one this app ships.
	"""
	if compat.doctype_exists("Party Type") and not frappe.db.exists("Party Type", party_type):
		registered = sorted(frappe.db.get_all("Party Type", pluck="name", limit=50) or [])
		extra = (
			" register_custom_party_types adds Family and Contact."
			if party_type in _CUSTOM_PARTY_TYPES
			else ""
		)
		raise ToolError(
			f"{party_type!r} is not a Party Type on this site. Registered: "
			f"{', '.join(registered) or '<none>'}.{extra} Nothing was changed."
		)
	if not compat.doctype_exists(party_type):
		raise ToolError(
			f"there is no {party_type} DocType on this site, so no posting can resolve a party "
			"through it — ERPNext reads `party` as a Dynamic Link through `party_type`, and a "
			"party type whose name is not a DocType resolves to nothing. Nothing was changed."
		)
	if not frappe.db.exists(party_type, party):
		hint = (
			" list_family_members has the register, and create_family_member adds to it."
			if party_type == "Family"
			else ""
		)
		raise ToolError(
			f"there is no {party_type} called {party!r}. A party has to be a record in the "
			f"register its party type names, or the posting points at nothing.{hint} Nothing "
			"was changed."
		)


#: The GL Entry columns every JE↔GL question here is answered from.
_GL_FIELDS = (
	"name",
	"account",
	"party_type",
	"party",
	"debit",
	"credit",
	"cost_center",
	"voucher_type",
	"voucher_no",
	"voucher_detail_no",
	"posting_date",
	"against_voucher_type",
	"against_voucher",
	"is_opening",
)


def voucher_gl_rows(name: str) -> list[dict]:
	"""Every live GL Entry row one Journal Entry posted, cancelled ones excluded.

	Cancelled rows are the reversal of a posting that has already been undone.
	Rewriting their attribution would change what the record says was undone, and
	counting them would make a two-line entry look like it posted four rows.
	"""
	if not compat.doctype_exists("GL Entry"):  # pragma: no cover - ERPNext always ships it
		return []
	filters = {"voucher_type": "Journal Entry", "voucher_no": name}
	if compat.has_field("GL Entry", "is_cancelled"):
		filters["is_cancelled"] = 0
	return [
		dict(row)
		for row in frappe.db.get_all(
			"GL Entry",
			filters=filters,
			fields=compat.existing_fields("GL Entry", _GL_FIELDS),
			order_by="name asc",
			limit=500,
		)
		or []
	]


def _amounts(row) -> tuple:
	return (round(float(row.get("debit") or 0), 2), round(float(row.get("credit") or 0), 2))


def gl_link_for_line(name: str, lines: list, line_index: int, gl_rows: list | None = None) -> dict:
	"""Which GL Entry rows belong to line `line_index` of a Journal Entry, and how sure.

	THIS IS THE FUNCTION v0.13.0 GOT WRONG, and the way it was wrong is worth
	writing down because it is a trap anybody would fall into twice.

	`GL Entry.voucher_detail_no` holds the child-row docname for Sales Invoice
	Item, Purchase Invoice Item and the other line-item doctypes. It does NOT for
	a Journal Entry. ERPNext's `JournalEntry.get_gl_entries` fills that column
	from the line's **`reference_detail_no`** — a field that names a payment
	schedule row on the invoice being settled, and which is empty on every
	ordinary line. So a lookup keyed on `voucher_detail_no == line.name` matches
	nothing on a real site, every time, for every account type. v0.13.0's
	`update_journal_entry_party` therefore updated the voucher, silently failed to
	update the ledger, and reported `gl_entries_matched: 0` with a warning
	suggesting the site was unusual. The site was not unusual. This was.

	It surfaced on ACC-JV-2026-00073, a $10 member distribution against an Equity
	account, which made it look like an Equity quirk. It is not: the same zero
	comes back for an expense line, a payable line and every other line on every
	Journal Entry ERPNext has ever posted.

	SO THE MATCH IS MADE THE WAY THE LEDGER ACTUALLY IDENTIFIES A LINE — account
	plus debit plus credit — and every way that can be wrong is checked BEFORE a
	caller is told the match is good:

	  * `voucher_detail_no` is still tried first. Some sites do carry it (a JE
	    written against a payment schedule), and where it is there it is exact.
	  * Two lines of the same entry with the same account and the same amounts
	    are indistinguishable in the GL. Ambiguous — refuse rather than guess.
	  * More GL rows than expected match the signature. Ambiguous.
	  * No row matches the amounts but the account has rows on this voucher:
	    ERPNext's `merge_similar_entries` combines lines sharing an account, a
	    party and a cost center into ONE summed row, so the row that exists covers
	    this line AND another. Writing a party onto it would attribute the other
	    line's money to this line's party. Refuse.
	  * Nothing at all on that account: the line posted no GL row, which is a
	    fact worth reporting rather than a count of zero to shrug at.

	Returns `{"rows", "basis", "exact", "blocker"}`. `blocker` is "" when the
	rows may be written to and a whole sentence naming the problem when they may
	not. Nothing here writes anything.
	"""
	rows = voucher_gl_rows(name) if gl_rows is None else list(gl_rows)
	line = lines[line_index - 1]
	empty = {"rows": [], "basis": "none", "exact": False, "blocker": ""}
	if not rows:
		return {
			**empty,
			"blocker": (
				f"Journal Entry {name} has no live GL Entry rows at all. A submitted entry "
				"normally posts one per line, so either this entry was posted by something "
				"that does not write a general ledger or its rows were removed."
			),
		}

	detail_no = str(line.get("name") or "")
	if detail_no:
		exact = [row for row in rows if str(row.get("voucher_detail_no") or "") == detail_no]
		if exact:
			return {"rows": exact, "basis": "voucher_detail_no", "exact": True, "blocker": ""}

	account = str(line.get("account") or "")
	signature = _amounts(line)
	twins = [
		index
		for index, other in enumerate(lines, start=1)
		if index != line_index and str(other.get("account") or "") == account and _amounts(other) == signature
	]
	if twins:
		return {
			**empty,
			"basis": "account and amount",
			"blocker": (
				f"line {line_index} and line(s) {', '.join(str(i) for i in twins)} of {name} post "
				f"the same amount to {account}, and this site's GL Entry rows do not carry the "
				"account line's docname (ERPNext fills `voucher_detail_no` from the line's "
				"`reference_detail_no`, which is empty on an ordinary line). There is nothing in "
				"the ledger that tells the two apart, so choosing one would be a coin toss."
			),
		}

	on_account = [row for row in rows if str(row.get("account") or "") == account]
	matched = [row for row in on_account if _amounts(row) == signature]
	if len(matched) == 1:
		return {"rows": matched, "basis": "account and amount", "exact": False, "blocker": ""}
	if len(matched) > 1:
		return {
			**empty,
			"basis": "account and amount",
			"blocker": (
				f"{len(matched)} GL Entry rows on {name} post {signature[0]} / {signature[1]} to "
				f"{account} and only one line of the voucher does, so the ledger holds more rows "
				"than the voucher explains. Read them with investigate_je_gl_link before changing "
				"anything."
			),
		}
	if on_account:
		return {
			**empty,
			"basis": "account and amount",
			"blocker": (
				f"line {line_index} posts {signature[0]} / {signature[1]} to {account}, and the "
				f"{len(on_account)} GL Entry row(s) on that account for {name} carry different "
				"amounts. ERPNext merges lines that share an account, a party and a cost center "
				"into one summed GL row, so the row that exists covers this line and at least one "
				"other — writing a party onto it would attribute somebody else's money to this "
				"party."
			),
		}
	return {
		**empty,
		"basis": "account and amount",
		"blocker": (
			f"no GL Entry row on {name} posts to {account} at all, so line {line_index} reached "
			"the voucher but not the ledger."
		),
	}


def _record_party_change(doc, line_index: int, before: dict, after: dict, reason: str):
	"""Write the change onto the entry's own timeline, and never fail the call for it.

	The reason is in the MCP Action Log whatever happens here. The comment is for
	the other reader — an accountant with the voucher open in the Desk, who will
	never see the action log and for whom an attribution that changed after
	submission with no note beside it is exactly the thing that makes a year-end
	close miserable.
	"""
	text = (
		f"Party updated on line {line_index} via MCP (erpnext_mcp): "
		f"{before['party_type'] or 'none'} / {before['party'] or 'none'} → "
		f"{after['party_type'] or 'none'} / {after['party'] or 'none'}. "
		f"Reason: {reason}. By {frappe.session.user} at {frappe.utils.now()}."
	)
	try:
		comment = doc.add_comment("Comment", text)
	except Exception:
		frappe.log_error(
			title="erpnext_mcp: could not attach party-change comment",
			message=compat.traceback_text(),
		)
		return None
	return getattr(comment, "name", None) or (comment.get("name") if isinstance(comment, dict) else None)


# ── 14. create_bank_transaction ─────────────────────────────────────────────
def create_bank_transaction(args: dict) -> ToolResult:
	"""Insert a Bank Transaction as a DRAFT.

	`amount` is signed the way a human reads a statement — positive is money in,
	negative is money out — and is mapped onto whichever columns this ERPNext
	version has (`deposit`/`withdrawal`, or a single signed `amount`).

	Left as a draft for the same reason a Journal Entry is: a submitted Bank
	Transaction is eligible for reconciliation and will start matching against
	payments. Submitting is a human step in ERPNext; this app deliberately ships
	no tool for it.
	"""
	compat.require_doctype("Bank Transaction", "It ships with ERPNext's Accounts module.")
	bank_account = _resolve_bank_account(as_str(args, "bank_account", required=True))
	date = as_date(args, "date", required=True)
	description = as_str(args, "description", required=True)
	reference_no = as_str(args, "reference_no")
	amount = as_float(args.get("amount"), "amount")
	if not amount:
		raise ToolError("amount must be non-zero (positive = money in, negative = money out)")

	money = compat.bank_transaction_amount_fields()
	doc = frappe.new_doc("Bank Transaction")
	doc.bank_account = bank_account
	doc.date = date
	doc.description = description
	if money["style"] == "deposit_withdrawal":
		doc.set(money["deposit"], amount if amount > 0 else 0)
		doc.set(money["withdrawal"], -amount if amount < 0 else 0)
	else:
		doc.set(money["amount"], amount)
	if reference_no and compat.has_field("Bank Transaction", "reference_number"):
		doc.reference_number = reference_no

	if compat.has_field("Bank Transaction", "company"):
		company = frappe.db.get_value("Bank Account", bank_account, "company")
		doc.company = company or resolve_company(as_str(args, "company"), required=True)
	if compat.has_field("Bank Transaction", "currency") and not doc.get("currency"):
		account = frappe.db.get_value("Bank Account", bank_account, "account")
		currency = frappe.db.get_value("Account", account, "account_currency") if account else None
		if currency:
			doc.currency = currency

	doc.insert()
	data = {
		"name": doc.name,
		"docstatus": int(doc.docstatus or 0),
		"docstatus_label": "draft",
		"bank_account": bank_account,
		"date": date,
		"amount_signed": amount,
		"amount_layout": money["style"],
		"description": description,
		"reference_no": reference_no or None,
		"next_step": (
			"Draft Bank Transactions are not reconcilable. Submit it in ERPNext to "
			"include it in bank reconciliation."
		),
	}
	return ToolResult(
		data,
		f"created draft Bank Transaction {doc.name} on {bank_account}: {amount} on {date}",
		docstatus_delta="none → 0 (draft)",
	)


# ── 15. reconcile_bank_transaction ──────────────────────────────────────────
def reconcile_bank_transaction(args: dict) -> ToolResult:
	"""Attach payment vouchers to a Bank Transaction.

	Hands the work to ERPNext's own `BankTransaction.add_payment_entries` when
	the site's version has it, because that method is where clearance dates,
	allocation arithmetic and the transaction's status live. Reimplementing that
	here would mean guessing at the parts of reconciliation ERPNext does after
	the child row is written — and getting one of them wrong leaves a
	transaction that looks reconciled and isn't. The append-and-save fallback is
	only for versions predating the method.
	"""
	compat.require_doctype("Bank Transaction", "It ships with ERPNext's Accounts module.")
	name = as_str(args, "name", required=True)
	if not frappe.db.exists("Bank Transaction", name):
		raise ToolError(f"no Bank Transaction named {name!r}")
	doc = frappe.get_doc("Bank Transaction", name)
	if int(doc.docstatus or 0) == 2:
		raise ToolError(f"Bank Transaction {name} is cancelled")

	vouchers = _validated_vouchers(args.get("payment_entries"))
	money = compat.bank_transaction_amount_fields()
	gross = round(compat.gross_amount(doc.as_dict(), money), 2)
	already = round(float(doc.get(money["allocated"]) or 0), 2) if money["allocated"] else 0.0
	requested = round(sum(v["allocated_amount"] for v in vouchers), 2)
	if requested - (gross - already) > 0.005:
		raise ToolError(
			f"allocating {requested} would exceed Bank Transaction {name}'s "
			f"remaining {round(gross - already, 2)} (gross {gross}, already "
			f"allocated {already}). Nothing was changed."
		)

	before = int(doc.docstatus or 0)
	# `callable(getattr(...))` rather than `hasattr`: a Frappe Document that
	# resolves unknown attributes to None (which several dict-backed subclasses
	# do) would pass hasattr and then fail on the call.
	delegate = getattr(doc, "add_payment_entries", None)
	if callable(delegate):
		delegate(vouchers)
		doc.reload()
	else:
		for voucher in vouchers:
			doc.append("payment_entries", voucher)
		doc.save()

	unallocated = round(float(doc.get(money["unallocated"]) or 0), 2) if money["unallocated"] else None
	data = {
		"name": doc.name,
		"bank_account": doc.get("bank_account"),
		"gross_amount": gross,
		"allocated_now": requested,
		"allocated_total": (
			round(float(doc.get(money["allocated"]) or 0), 2) if money["allocated"] else None
		),
		"unallocated_amount": unallocated,
		"status": doc.get("status"),
		"payment_entries": [
			{
				"payment_document": row.get("payment_document"),
				"payment_entry": row.get("payment_entry"),
				"allocated_amount": row.get("allocated_amount"),
			}
			for row in (doc.get("payment_entries") or [])
		],
		"applied_via": (
			"ERPNext add_payment_entries" if callable(delegate) else "append + save (legacy ERPNext)"
		),
	}
	after = int(doc.docstatus or 0)
	return ToolResult(
		data,
		f"reconciled {requested} against Bank Transaction {doc.name} "
		f"({len(vouchers)} voucher(s)); status {doc.get('status')}",
		docstatus_delta=f"{before} → {after}" if before != after else "",
	)


def _validated_vouchers(raw) -> list[dict]:
	"""Check the `payment_entries` argument and confirm each voucher exists."""
	if not isinstance(raw, list) or not raw:
		raise ToolError(
			"payment_entries must be a non-empty list, e.g. "
			'[{"payment_document": "Payment Entry", "payment_entry": "PE-0001", '
			'"allocated_amount": 250.00}]'
		)
	out = []
	for index, entry in enumerate(raw, start=1):
		if not isinstance(entry, dict):
			raise ToolError(f"payment_entries[{index}] must be an object, got {type(entry).__name__}")
		doctype = str(entry.get("payment_document") or "").strip()
		docname = str(entry.get("payment_entry") or "").strip()
		if not doctype and entry.get("payment_doctype"):
			# `payment_doctype` is the name the field has almost everywhere else in
			# Frappe, so it is the one a model reaches for first. Naming the right
			# field costs one round trip; the alternative — accepting both — would
			# make this the only tool in the app where a misspelt key is guessed at
			# rather than reported, and the guess would eventually be wrong.
			raise ToolError(
				f"payment_entries[{index}] uses payment_doctype; on ERPNext's Bank Transaction "
				"Payments table the field is called payment_document. Resend it under that name, "
				f'e.g. {{"payment_document": {str(entry["payment_doctype"])!r}, "payment_entry": '
				'"PE-0001", "allocated_amount": 250.00}. Nothing was changed.'
			)
		if not doctype or not docname:
			raise ToolError(
				f"payment_entries[{index}] needs both payment_document (the voucher "
				"doctype, e.g. 'Payment Entry') and payment_entry (its name)"
			)
		if not compat.doctype_exists(doctype):
			raise ToolError(f"payment_entries[{index}]: no such DocType {doctype!r} on this site")
		if not frappe.db.exists(doctype, docname):
			raise ToolError(f"payment_entries[{index}]: no {doctype} named {docname!r}")
		allocated = as_float(entry.get("allocated_amount"), f"payment_entries[{index}].allocated_amount")
		if allocated <= 0:
			raise ToolError(f"payment_entries[{index}].allocated_amount must be positive")
		out.append(
			{
				"payment_document": doctype,
				"payment_entry": docname,
				"allocated_amount": allocated,
			}
		)
	return out


# ── 151. repair_drifted_je_attributions ─────────────────────────────────────
#: Most lines one repair run will touch. Deliberately smaller than the scan's
#: cap: reading five hundred findings is a report, writing to five hundred GL
#: rows is an event, and the two want different ceilings.
REPAIR_CAP = 200


def repair_drifted_je_attributions(args: dict) -> ToolResult:
	"""Bring drifted GL Entry rows back into step with their vouchers, in a batch.

	TAKES `repair_input` FROM `find_drifted_je_attributions` VERBATIM. That is the
	whole ergonomic point: the diagnostic already knows which line of which entry
	should read what, and re-deriving it here would be a second implementation of
	the matcher with its own opportunity to be wrong. Each item is
	`{journal_entry, line_index, party_type, party}`.

	IT REPAIRS IN ONE DIRECTION, AND THE DIRECTION IS A JUDGEMENT WORTH STATING.
	Every item brings the LEDGER into line with the VOUCHER. For the v0.13.0
	damage class that is right by construction — the broken tool wrote the voucher
	and failed to write the ledger, so the voucher holds the attribution somebody
	actually intended and the ledger holds the stale one. For drift from some
	other cause it may not be, which is why the diagnostic reports the vintage and
	why a line whose LEDGER is the correct side should go through
	`update_journal_entry_party` individually with the party you want on both.

	MOVES NO BALANCE, EVER. `party` is an attribution column. Every debit, credit,
	account and date is untouched, so the trial balance after a repair of two
	hundred lines is arithmetically identical to the one before it. That is what
	makes a batch write to submitted vouchers defensible at all.

	IT DOES NOT STOP ON THE FIRST FAILURE. Each item is independent — a different
	voucher, a different line — and a run that aborted half way would leave the
	ledger in a state neither the report before it nor the report after it
	describes. Every item is attempted, and every outcome is reported per item.
	The other half of that trade is that a partial run is a real outcome, which is
	why `failed` is in the summary line and not only in the payload.

	DRY RUN DEFAULTS TRUE.
	"""
	reason = as_str(args, "reason", required=True)
	if len(reason) < 8:
		raise ToolError(
			"reason must be a real explanation of why these attributions are being repaired, and "
			"it is written onto every entry this touches. 'Repairing drift left by v0.13.0's "
			"update_journal_entry_party, per find_drifted_je_attributions on <date>' is the kind "
			"of sentence that answers the question in two years. Nothing was changed."
		)

	items = args.get("repairs")
	if items is None:
		items = args.get("repair_input")
	if not isinstance(items, list) or not items:
		raise ToolError(
			"repairs must be a non-empty list of "
			'{"journal_entry", "line_index", "party_type", "party"} objects — which is exactly '
			"what find_drifted_je_attributions returns under `repair_input`. Pass that through. "
			"Nothing was changed."
		)
	if len(items) > REPAIR_CAP:
		raise ToolError(
			f"{len(items)} repairs were passed, over the {REPAIR_CAP} this tool will take in one "
			"run. Reading five hundred findings is a report; writing to five hundred ledger rows "
			"is an event, and the two deserve different ceilings. Split the list. Nothing was "
			"changed."
		)

	plan = []
	for index, item in enumerate(items, start=1):
		if not isinstance(item, dict):
			raise ToolError(f"repairs[{index}] must be an object, got {type(item).__name__}.")
		unknown = sorted(set(item) - {"journal_entry", "line_index", "party_type", "party"})
		if unknown:
			raise ToolError(
				f"repairs[{index}] has unsupported field(s): {', '.join(unknown)}. Each item is "
				"{journal_entry, line_index, party_type, party}. Nothing was changed."
			)
		journal_entry = str(item.get("journal_entry") or "").strip()
		if not journal_entry:
			raise ToolError(f"repairs[{index}] has no journal_entry. Nothing was changed.")
		line_index = item.get("line_index")
		try:
			line_index = int(line_index)
		except (TypeError, ValueError):
			raise ToolError(
				f"repairs[{index}].line_index must be a whole number counting from 1, got "
				f"{line_index!r}. Nothing was changed."
			) from None
		plan.append(
			{
				"journal_entry": journal_entry,
				"line_index": line_index,
				"party_type": str(item.get("party_type") or ""),
				"party": str(item.get("party") or ""),
			}
		)

	dry_run = as_bool(args, "dry_run", True)
	results, repaired, failed, unchanged = [], 0, 0, 0

	for item in plan:
		call = {
			"journal_entry": item["journal_entry"],
			"line_index": item["line_index"],
			"party_type": item["party_type"],
			"party": item["party"],
			"reason": reason,
			# The whole point of the batch: write the ledger even where the voucher
			# already agrees, which on the v0.13.0 damage class is every item.
			"force_gl_sync": True,
			"dry_run": dry_run,
		}
		try:
			result = update_journal_entry_party(call)
			data = result.data
			outcome = "would_repair" if dry_run else "repaired"
			if not dry_run and not data.get("gl_entries_updated"):
				outcome = "nothing_written"
				unchanged += 1
			else:
				repaired += 1
			results.append(
				{
					"journal_entry": item["journal_entry"],
					"line_index": item["line_index"],
					"outcome": outcome,
					"account": data.get("account"),
					"party_type": item["party_type"] or None,
					"party": item["party"] or None,
					"gl_entries_updated": data.get("gl_entries_updated", 0),
					"gl_entries_drifted": data.get("gl_entries_drifted", 0),
					"gl_only_update": bool(data.get("gl_only_update")),
					"summary": result.summary,
				}
			)
		except ToolError as exc:
			failed += 1
			results.append(
				{
					"journal_entry": item["journal_entry"],
					"line_index": item["line_index"],
					"outcome": "refused",
					"party_type": item["party_type"] or None,
					"party": item["party"] or None,
					"error": str(exc),
				}
			)
		except Exception as exc:  # pragma: no cover - a bug, reported per item rather than aborting
			failed += 1
			results.append(
				{
					"journal_entry": item["journal_entry"],
					"line_index": item["line_index"],
					"outcome": "failed",
					"error": f"{type(exc).__name__}: {exc}",
				}
			)

	entries = sorted({item["journal_entry"] for item in plan})
	data = {
		"dry_run": bool(dry_run),
		"reason": reason,
		"requested": len(plan),
		"journal_entries": entries,
		"repaired": repaired,
		"unchanged": unchanged,
		"failed": failed,
		"results": results,
		"note": (
			(
				"NOTHING WAS WRITTEN. Every item above was checked against the live voucher and "
				"its GL rows, so an item reported as would_repair is one that will, and an item "
				"reported as refused says why. Call again with dry_run=false and the same list."
				if dry_run
				else (
					f"{repaired} line(s) had their ledger rows brought into step with their "
					f"voucher. NO BALANCE MOVED: party is an attribution column, and the trial "
					"balance after this run is arithmetically identical to the one before it. "
					"Every entry touched carries a comment saying what changed and why."
				)
			)
			+ (
				f" {failed} item(s) were refused or failed and are reported individually — the "
				"run did NOT abort on them, because each item is a different voucher and a run "
				"that stopped half way would leave the ledger in a state neither report "
				"describes."
				if failed
				else ""
			)
		),
		"next_step": (
			"Run find_drifted_je_attributions over the same range again. A clean second scan is "
			"the only proof the repair worked, and it is cheaper than reading this list twice."
		),
	}
	return ToolResult(
		data,
		(
			f"dry run: would repair {repaired} of {len(plan)} drifted line(s) across "
			f"{len(entries)} entr(y/ies)"
			if dry_run
			else f"repaired {repaired} of {len(plan)} drifted line(s) across {len(entries)} entr(y/ies)"
		)
		+ (f", {failed} refused or failed" if failed else "")
		+ f" — {reason}",
		docstatus_delta="",
	)
