# SPDX-License-Identifier: MIT
"""What the Phase 1 financial controls actually decide.

Separated from `tools/controls.py` on the split this app keeps everywhere: the
tool module reads arguments, resolves names and shapes a response; this module
answers the question. The division earns its keep here more than most, because
these are the functions an auditor's question reduces to — "would this entry have
been stopped, and why" — and a function that takes a dict and returns a finding
can be tested exhaustively without a site.

THE FOUR JUDGEMENTS, AND THE ONE THING THEY SHARE. Approval authority, period
locks, duplicate detection and unusual amounts look like four different problems.
They are one: each compares a transaction somebody is about to write against a
statement the operation already made about itself, and each returns findings that
`enforcement.evaluate` then reports or refuses. NONE OF THEM DECIDES WHETHER TO
BLOCK. That decision is one switch in one place, and keeping it out of here is
what stops "Advisory" meaning four subtly different things by next season.

WHY DUPLICATE DETECTION IS A WINDOW AND NOT A HASH. Two entries are suspicious
when they carry the same company, the same total, the same accounts and a posting
date close together — not when they are byte-identical, which is a state the
commonest real duplicate never reaches (somebody retypes it, so the remark
differs). The window is deliberately generous and the finding is deliberately
advisory-shaped: this control's job is to put two documents in front of a person,
not to be right.

WHY THE UNUSUAL-AMOUNT TEST IS A MULTIPLE OF A MEDIAN AND NOT A STANDARD
DEVIATION. A farm's journal entries are not normally distributed and there are
rarely enough of them for a variance to mean anything — one lumpy month of
capital spend would widen the band until it caught nothing for a year. A median
is unmoved by exactly those outliers, and "this is twenty times the typical entry
on these accounts" is a sentence a person can act on, which a z-score is not.
`MIN_SAMPLE` is the floor below which the app declines to have an opinion at all:
a control that flags the third entry a new company ever wrote is a control that
gets turned off in week one.
"""

from __future__ import annotations

import datetime

#: How many days either side of a posting date another entry has to fall within
#: before the two are worth putting in front of somebody. Wide, because the
#: duplicate that matters is "we both posted the March accrual" and those land a
#: few days apart; narrow enough that a recurring monthly entry does not match
#: last month's.
DUPLICATE_WINDOW_DAYS = 7

#: How close two totals have to be to count as the same amount. Absolute, in the
#: company's currency, so a rounding difference of a cent does not hide a
#: duplicate — which is the failure mode of an exact-equality test on money.
DUPLICATE_AMOUNT_TOLERANCE = 0.01

#: How many comparable entries there have to be before "unusual" means anything.
#: Below this the app has no opinion and says so, rather than calling the third
#: entry a company ever wrote an outlier.
MIN_SAMPLE = 8

#: How many times the median an entry has to be before it is worth a second look.
#: Twenty is high on purpose. This control competes for attention with everything
#: else in the calendar, and a band that fires on a merely large entry trains
#: people to dismiss it — at which point it catches nothing, including the
#: transposed digit it exists for.
UNUSUAL_MULTIPLE = 20.0

#: Below this, an entry is never unusual however far above the median it sits. A
#: company whose typical entry is four dollars would otherwise have every hundred
#: dollar entry flagged, which is arithmetically true and useless.
UNUSUAL_FLOOR = 1000.0


def _money(value) -> float:
	try:
		return round(float(value or 0), 2)
	except (TypeError, ValueError):
		return 0.0


def _date(value):
	"""A `datetime.date` from whatever the caller has, or None.

	Tolerant on purpose: this module is handed dates that came from a tool
	argument, from `frappe.db.get_all`, and from a test — three shapes — and a
	control that raised on the third would be a control nobody could test.
	"""
	if isinstance(value, datetime.datetime):
		return value.date()
	if isinstance(value, datetime.date):
		return value
	text = str(value or "").strip()
	if not text:
		return None
	try:
		return datetime.date.fromisoformat(text[:10])
	except ValueError:
		return None


# ── approval authority ──────────────────────────────────────────────────────
def applicable_threshold(thresholds: list, document_type: str) -> dict:
	"""Which threshold governs this document type. Specific beats blanket.

	A site typically runs one `Any` table and overrides it for the one or two
	document types that deserve tighter authority. Returning the specific row when
	there is one, and the blanket row otherwise, is what makes that work without
	the two fighting — and it means an operation can tighten capital purchases
	without restating its whole authority table.

	`{}` when nothing applies, which callers read as "this operation has expressed
	no opinion about authority for this document type" and report rather than
	treat as unlimited or as forbidden.
	"""
	document_type = str(document_type or "").strip()
	enabled = [row for row in thresholds or [] if int(row.get("enabled") or 0)]
	for row in enabled:
		if str(row.get("document_type") or "") == document_type and document_type:
			return dict(row)
	for row in enabled:
		if str(row.get("document_type") or "") == "Any":
			return dict(row)
	return {}


def required_authority(threshold: dict, amount: float) -> dict:
	"""What releasing `amount` under `threshold` needs. THE CHAIN, EVALUATED.

	Returns a dict with `needed` (False when the amount is under the auto-approve
	floor), the rung that covers it, and the role that rung names. The rungs are
	sorted HERE rather than trusted in row order, which is the same reason the
	controller refuses two rungs with one ceiling: the answer to "who approves
	this" must not depend on how somebody dragged the grid.

	An uncapped rung is the top of the chain and sorts last however it was typed.
	"""
	amount = abs(_money(amount))
	floor = _money(threshold.get("auto_approve_below"))
	out = {
		"threshold": threshold.get("name"),
		"amount": amount,
		"auto_approve_below": floor,
		"needed": True,
		"approver_role": "",
		"rung": None,
		"uncapped": False,
	}
	if floor and amount < floor:
		out["needed"] = False
		out["reason"] = (
			f"{amount} is below this threshold's auto-approve floor of {floor}, so it needs no "
			"release and the control says nothing about it."
		)
		return out

	rungs = list(threshold.get("levels") or [])
	capped = sorted(
		(row for row in rungs if _money(row.get("up_to_amount"))),
		key=lambda row: _money(row.get("up_to_amount")),
	)
	uncapped = [row for row in rungs if not _money(row.get("up_to_amount"))]

	for row in capped:
		if amount <= _money(row.get("up_to_amount")):
			out["approver_role"] = str(row.get("approver_role") or "")
			out["rung"] = _money(row.get("up_to_amount"))
			return out

	if uncapped:
		out["approver_role"] = str(uncapped[0].get("approver_role") or "")
		out["uncapped"] = True
		return out

	# Past the top of a chain that has no uncapped rung. Nobody on this table may
	# release it — which is a real and reportable state, not an error: it is what
	# an operation looks like the first time it writes a transaction larger than
	# anything its authority table anticipated.
	out["above_chain"] = True
	out["ceiling"] = _money(capped[-1].get("up_to_amount")) if capped else 0.0
	return out


# ── period locks ────────────────────────────────────────────────────────────
def locked_period(periods: list, posting_date) -> dict:
	"""The locked period a posting date falls inside, or `{}`.

	Both bounds inclusive. A posting dated on the last day of a closed month is
	inside it — which is the case that matters, since the last day of the month is
	where a back-dated entry is most likely to be aimed.
	"""
	when = _date(posting_date)
	if not when:
		return {}
	for row in periods or []:
		if not int(row.get("locked") or 0):
			continue
		start = _date(row.get("period_start"))
		end = _date(row.get("period_end"))
		if start and end and start <= when <= end:
			return dict(row)
	return {}


def outstanding_steps(items: list) -> list:
	"""The required checklist steps that are not done, in sequence order.

	`required` is what makes a step able to hold a close. A step somebody wanted
	tracked but not gated is not returned here however incomplete — see the
	doctype's own description on why that distinction is worth a column.
	"""
	rows = [
		row for row in items or [] if int(row.get("required") or 0) and not int(row.get("completed") or 0)
	]
	return sorted(rows, key=lambda row: (int(row.get("sequence") or 0), str(row.get("step") or "")))


# ── duplicate detection ─────────────────────────────────────────────────────
def account_signature(lines: list) -> tuple:
	"""The set of accounts an entry touches, order-independent.

	A SET AND NOT A SEQUENCE, because the same entry written by two people has the
	same accounts in whatever order each of them typed, and a signature that
	depended on order would miss precisely the duplicate this control exists for.
	The debit/credit split is deliberately not part of it either: a duplicate
	posted with the sides reversed is a different and worse problem, and it is one
	this signature should still surface rather than hide.
	"""
	return tuple(
		sorted({str(line.get("account") or "").strip() for line in lines or [] if line.get("account")})
	)


def duplicate_findings(candidate: dict, existing: list) -> list:
	"""Entries already on the books that this one looks like. Most alike first.

	`candidate` is `{posting_date, total, accounts, company, cheque_no}`; `existing` is rows
	read from the site with the same shape plus a `name`. Returns a list of dicts
	each naming the match and WHY it matched, because "duplicate of ACC-2026-0031"
	is actionable and "possible duplicate" is not.

	THREE THINGS HAVE TO AGREE and the third is what keeps this quiet enough to
	live with. Same total, within a cent. Same accounts, as a set. Posting dates
	within a week. A recurring monthly accrual matches the first two every single
	month and fails the third, which is the whole reason the window is there.

	v0.184.0. TWO DIFFERENT BANK REFERENCES ARE TWO DIFFERENT TRANSACTIONS. Both
	sides may carry a `cheque_no` — on a bank-fed entry that is the Bank
	Transaction docname (`ACC-BTN-…`) the entry was booked from. When both are
	present and they differ, the pair is not a duplicate by definition, and it is
	not reported: that is the two $10 subscription charges in one week and the
	two transfers on one day, which were most of what this control filed. A
	pair where either side has no reference, or both carry the SAME one, is still
	compared as before — the same BTN booked twice is exactly the duplicate this
	exists for, and is named as such in `why`.
	"""
	when = _date(candidate.get("posting_date"))
	total = _money(candidate.get("total"))
	accounts = tuple(candidate.get("accounts") or ())
	reference = str(candidate.get("cheque_no") or "").strip()
	out = []
	if not when or not total:
		return out

	for row in existing or []:
		other_reference = str(row.get("cheque_no") or "").strip()
		if reference and other_reference and reference != other_reference:
			continue
		other_when = _date(row.get("posting_date"))
		if not other_when:
			continue
		gap = abs((other_when - when).days)
		if gap > DUPLICATE_WINDOW_DAYS:
			continue
		# ROUNDED BEFORE COMPARING, not compared and then trusted. `4200.005`
		# rounds to `4200.01`, and `4200.01 - 4200.00` is `0.010000000000218` in
		# binary floating point — fractionally over a tolerance of exactly one
		# cent. A duplicate hidden by the last bit of a float is precisely the
		# failure the tolerance was added to prevent.
		if round(abs(_money(row.get("total")) - total), 2) > DUPLICATE_AMOUNT_TOLERANCE:
			continue
		other_accounts = tuple(row.get("accounts") or ())
		if accounts and other_accounts and set(accounts) != set(other_accounts):
			continue
		why = (
			f"same total ({total}), same {len(accounts) or len(other_accounts)} account(s), "
			f"and posted {gap} day(s) apart"
		)
		if reference and reference == other_reference:
			why += f", and both reference bank transaction {reference}"
		out.append(
			{
				"name": row.get("name"),
				"posting_date": str(row.get("posting_date") or ""),
				"total": _money(row.get("total")),
				"days_apart": gap,
				"accounts": list(other_accounts),
				"docstatus": int(row.get("docstatus") or 0),
				"cheque_no": other_reference or None,
				"why": why,
			}
		)
	return sorted(out, key=lambda row: (row["days_apart"], str(row["name"])))


# ── unusual amounts ─────────────────────────────────────────────────────────
def median(values: list) -> float:
	numbers = sorted(_money(value) for value in values or [] if _money(value))
	if not numbers:
		return 0.0
	middle = len(numbers) // 2
	if len(numbers) % 2:
		return numbers[middle]
	return round((numbers[middle - 1] + numbers[middle]) / 2.0, 2)


def unusual_amount(total: float, history: list) -> dict:
	"""Is `total` far outside what these accounts normally carry?

	Returns a dict that ALWAYS says what it concluded and on what basis, including
	when it declines to conclude anything. A control that returns a bare False for
	both "this is normal" and "I have no idea" is a control whose silence cannot
	be interpreted — and on a young company the second case is the common one.
	"""
	total = abs(_money(total))
	sample = [abs(_money(value)) for value in history or [] if _money(value)]
	out = {
		"amount": total,
		"sample_size": len(sample),
		"median": 0.0,
		"multiple": None,
		"unusual": False,
		"basis": "",
	}
	if len(sample) < MIN_SAMPLE:
		out["basis"] = (
			f"only {len(sample)} comparable entr(ies) on these accounts, below the {MIN_SAMPLE} this "
			"app needs before it will call anything unusual. No opinion was formed — which is "
			"different from, and should not be read as, 'this entry is normal'."
		)
		return out

	typical = median(sample)
	out["median"] = typical
	if not typical:
		out["basis"] = (
			"every comparable entry on these accounts is zero, so there is no scale to judge against."
		)
		return out

	multiple = round(total / typical, 2)
	out["multiple"] = multiple
	if total < UNUSUAL_FLOOR:
		out["basis"] = (
			f"{total} is below the {UNUSUAL_FLOOR} floor under which nothing is flagged, whatever "
			f"its multiple of the median ({typical})."
		)
		return out
	if multiple < UNUSUAL_MULTIPLE:
		out["basis"] = (
			f"{total} is {multiple}x the median entry of {typical} on these accounts, inside the "
			f"{UNUSUAL_MULTIPLE}x band."
		)
		return out

	out["unusual"] = True
	out["basis"] = (
		f"{total} is {multiple}x the median entry of {typical} across {len(sample)} comparable "
		f"entries on these accounts, past the {UNUSUAL_MULTIPLE}x band. The commonest cause is a "
		"transposed digit, which produces an entry that is balanced, well-described and wrong by "
		"an order of magnitude."
	)
	return out


# ── segregation of duties ───────────────────────────────────────────────────
def same_hand(preparer: str, approver: str) -> bool:
	"""Did one person both write and release this? Case- and blank-insensitive.

	A blank on either side is NOT a match. An entry with no recorded approver has
	not been approved by anybody, which is a different finding from one approved
	by its own author, and collapsing the two would report every draft in the
	system as a segregation failure.
	"""
	left = str(preparer or "").strip().lower()
	right = str(approver or "").strip().lower()
	return bool(left) and bool(right) and left == right
