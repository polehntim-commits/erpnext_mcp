# SPDX-License-Identifier: MIT
"""Garnishments and voluntary deductions. PURE FUNCTIONS.

No database reads, no side effects, fully testable with deterministic inputs.
Same contract as `payroll_calc.py`, `payroll_integration.py` and `withholding.py`:
everything arrives as an argument.

THE GAP THIS CLOSES. Every release through this one computed a slip whose only
deductions were the ones the government requires — federal withholding, the two
halves of FICA, and whatever the state takes. That is the easy half of a payroll
run and it is not the half that gets a farm sued. A court sends a child support
order to the employer, not to the worker; an employer served with an order and
paying the worker in full is liable for the money it failed to withhold, and in
most states for the arrears on top. The 401(k) half fails quietly rather than
loudly: an elective deferral that never reduces the wage base is a worker
over-withheld all year and a plan out of compliance.

────────────────────────────────────────────────────────────────────────────
THE ORDER IS NOT A PREFERENCE, IT IS THE LAW'S OWN ORDER
────────────────────────────────────────────────────────────────────────────

  1. Gross pay, including any minimum wage makeup. `payroll_calc` decided it.
  2. PRE-TAX voluntary deductions come out of the WAGE BASE before withholding
     is computed. This is the step that makes a 401(k) a 401(k).
  3. Taxes, on the reduced base.
  4. DISPOSABLE EARNINGS = gross − the taxes actually withheld. Note what is
     NOT subtracted: voluntary deductions, pre-tax ones included. 29 CFR 870.10
     defines disposable earnings as pay less amounts required BY LAW to be
     withheld, and a 401(k) election is not required by law. Subtracting it
     would shrink the base a garnishment is measured against, which is the
     employee choosing how much of a court order to obey.
  5. GARNISHMENTS, in legal priority order, against the CCPA ceiling.
  6. POST-TAX voluntary deductions, out of whatever cash is left.

Steps 5 and 6 are in that order for one reason: a garnishment outranks a union
due. When there is not enough money, the voluntary deduction is the one that
gets cut.

────────────────────────────────────────────────────────────────────────────
PRE-TAX IS TWO DIFFERENT ANSWERS AND TREATING IT AS ONE IS THE CLASSIC BUG
────────────────────────────────────────────────────────────────────────────

"Pre-tax" names two distinct exemptions and the categories do not agree on
which they get:

  * A SECTION 125 CAFETERIA PLAN benefit — health, dental and vision premiums,
    HSA and FSA contributions run through the plan — is exempt from federal
    income tax AND from Social Security, Medicare and FUTA. IRC §125(a),
    §3121(a)(5)(G).
  * A TRADITIONAL 401(k) ELECTIVE DEFERRAL is exempt from federal income tax
    and is NOT exempt from FICA or FUTA. IRC §402(e)(3) defers the income tax;
    §3121(v)(1)(A) keeps the deferral in the Social Security and Medicare wage
    base. It is taxed for FICA in the year deferred and never again.

So there are two reduced bases, not one: `federal_taxable_gross` and
`fica_taxable_gross`, and on a slip with a 401(k) they differ. Running FICA on
the income-tax base under-withholds Social Security and Medicare on every
deferral — an error that reconciles cleanly all year and surfaces on a W-2,
where Box 1 and Box 3 are supposed to differ by exactly the deferral and
instead agree.

────────────────────────────────────────────────────────────────────────────
THE CCPA CEILING, AND WHY THERE ARE FOUR OF THEM
────────────────────────────────────────────────────────────────────────────

Title III of the Consumer Credit Protection Act (15 U.S.C. §1673, 29 CFR 870)
caps what may be taken, and the cap depends on what is being collected:

  ORDINARY GARNISHMENT (a creditor, a judgment, a court-ordered wage
  garnishment) — the LESSER of 25% of disposable earnings, or the amount by
  which disposable earnings exceed 30 × the federal minimum wage for the week.
  At $7.25 that floor is $217.50 a week, and the regulation gives the longer
  periods their own multipliers rather than asking anybody to annualise:
  60× biweekly, 65× semimonthly, 130× monthly. Whichever number is smaller
  wins, and on a short week the second one is frequently zero — which is the
  point of it. Somebody earning less than thirty hours at the federal minimum
  keeps all of it.

  CHILD SUPPORT — §1673(b)(2), a different and much higher ceiling: 50% of
  disposable earnings where the employee supports another spouse or child,
  60% where they do not, plus 5 more where the arrears run past twelve weeks.
  So 50, 55, 60 or 65, and the two facts that pick it are properties of the
  ORDER, which is why they are fields on the deduction and not settings.

  FEDERAL AND STATE TAX LEVIES — 29 CFR 870.11(b)(2): the Title III
  restrictions do not apply. A levy is bounded instead by the exempt amount
  IRC §6334(d) leaves the taxpayer, which the IRS publishes as Publication
  1494 and computes from filing status and dependents. This app does not carry
  Pub 1494, so a levy takes what the notice says to take and the exempt amount
  is a figure somebody enters on the row. That is honest; deriving it from a
  table this app does not have would not be.

  STUDENT LOAN — administrative wage garnishment under 20 U.S.C. §1095a(a)(1),
  capped at 15% of disposable pay, and still inside the ordinary 25% pool when
  it competes with a creditor garnishment.

WHEN SEVERAL COMPETE, THE POOL IS SHARED. 29 CFR 870.11(b)(1): where support is
withheld under §1673(b) and an ordinary garnishment is also served, the ordinary
one gets only what is left of the 25% after the support came out — not its own
fresh 25%. So the ordinary pool is computed and then reduced by whatever support
took, which is frequently to zero, and that is the correct answer rather than a
failure to collect.

TWO ORDERS OF THE SAME KIND THAT WILL NOT BOTH FIT ARE PRORATED by their ordered
amounts. Some states specify equal division and some first-in-time; proration is
the majority federal rule for support and it is what is done here, reported as
its own flag on each line so nobody has to infer it from the arithmetic.

────────────────────────────────────────────────────────────────────────────
WHAT THIS MODULE REFUSES TO DO
────────────────────────────────────────────────────────────────────────────

IT NEVER PRODUCES A NEGATIVE NET. Where the elections and the orders together
come to more than the pay, the deductions are cut in reverse priority — the
voluntary ones first, the lowest priority of those first — and every cut is
reported as a shortfall line with what was asked and what was taken. A payroll
run that hands somebody a negative cheque is a payroll run nobody looks at
twice, and the shortfall is the fact the payroll clerk has to act on.

ARREARS (v0.292.0, Tim's middle path). The module stays stateless: the caller passes each order's
`arrears_balance` (from the order and its ledger) and the policy, and every line reports its current amount,
catch-up, cap and the arrears after — which the caller writes to the ledger. What a period could not take is CARRIED
FORWARD, never dropped and never taken beyond a cap.

IT GIVES NO LEGAL ADVICE AND MAKES NO DETERMINATION. The state rule that beats
the federal one is not here — several states cap ordinary garnishment below 25%,
and a few (Texas, Pennsylvania, North Carolina, South Carolina) bar creditor
garnishment of wages almost entirely. `state_cap_rate` is the hook: pass one and
it is applied as the tighter of the two, because Title III is a floor of
protection and never a ceiling on it.
"""

from __future__ import annotations

#: The federal minimum wage the CCPA floor is measured in. 29 CFR 870.10 fixes
#: the multiplier at thirty hours and leaves the rate to FLSA §6, which has been
#: $7.25 since 2009. NOT the state minimum wage: where a state's is higher its
#: own garnishment law usually says so, and that is `state_cap_rate`'s business.
#: The federal floor is computed from the federal figure.
FEDERAL_MINIMUM_WAGE = 7.25

#: Hours of federal minimum wage protected from an ordinary garnishment, per
#: pay period. 29 CFR 870.10(b) gives the longer periods their own multipliers
#: rather than a weekly figure somebody multiplies, and they are not simple
#: multiples of 30: semimonthly is 65, not 65.0-ish, and monthly is 130.
CCPA_PERIOD_MULTIPLIERS = {
	"Weekly": 30.0,
	"Biweekly": 60.0,
	"Semimonthly": 65.0,
	"Monthly": 130.0,
}

#: The ordinary garnishment ceiling as a share of disposable earnings.
CCPA_ORDINARY_RATE = 0.25

#: §1673(b)(2). The support ceiling where the employee supports another spouse
#: or dependent child, and where they do not.
CHILD_SUPPORT_RATE_SUPPORTING_OTHERS = 0.50
CHILD_SUPPORT_RATE_NOT_SUPPORTING_OTHERS = 0.60

#: Added to either of the above where support is more than twelve weeks in
#: arrears. §1673(b)(2)'s final clause.
CHILD_SUPPORT_ARREARS_INCREMENT = 0.05

#: 20 U.S.C. §1095a(a)(1). A single federal student loan AWG takes at most this
#: share of disposable pay, inside the ordinary pool rather than beside it.
STUDENT_LOAN_RATE = 0.15

#: What a row's `deduction_type` may say. The row's own value wins over the
#: category default, so a creditor garnishment filed under `other` is still
#: treated as a garnishment.
DEDUCTION_TYPES = ("garnishment", "voluntary")

#: What a row's `amount_type` may say.
AMOUNT_TYPES = ("fixed", "percentage")

#: What a percentage is a percentage OF. `net_after_tax` means DISPOSABLE
#: EARNINGS — gross less the legally required withholding — and it means the
#: same figure for every row on the slip rather than a running balance that
#: shrinks as each deduction is taken. A running basis would make each amount
#: depend on the order the rows happened to be processed in, so two orders for
#: the same percentage would collect different money, and nobody reading the
#: slip could reproduce either.
BASES = ("gross_pay", "net_after_tax")

#: What a row's `status` may say. Only `active` is ever withheld.
STATUSES = ("active", "suspended", "completed")

#: Every category, with what the law says about it. `priority` is the default
#: processing order and is overridable per row — a court order that specifies
#: its own rank is the more specific record. Lower numbers are taken first.
#:
#: `pre_tax` and `fica_exempt` are the two-answer distinction the module docstring
#: opens on: a 401(k) is pre-tax for income tax and NOT exempt from FICA, while a
#: Section 125 benefit is exempt from both. Getting these two columns right is
#: most of what this table is for.
CATEGORY_SPECS = {
	# ── Garnishments, in the order they are satisfied ────────────────────────
	"child_support": {
		"label": "Child Support",
		"deduction_type": "garnishment",
		"priority": 10,
		"pre_tax": False,
		"fica_exempt": False,
		"limit": "child_support",
	},
	"tax_levy": {
		"label": "Tax Levy",
		"deduction_type": "garnishment",
		"priority": 20,
		"pre_tax": False,
		"fica_exempt": False,
		# 29 CFR 870.11(b)(2): Title III does not reach a tax levy at all.
		"limit": "exempt_from_ccpa",
	},
	"student_loan": {
		"label": "Student Loan",
		"deduction_type": "garnishment",
		"priority": 30,
		"pre_tax": False,
		"fica_exempt": False,
		"limit": "student_loan",
	},
	"wage_garnishment": {
		"label": "Wage Garnishment",
		"deduction_type": "garnishment",
		"priority": 40,
		"pre_tax": False,
		"fica_exempt": False,
		"limit": "ordinary",
	},
	# ── Voluntary, pre-tax ───────────────────────────────────────────────────
	"retirement_401k": {
		"label": "401(k)",
		"deduction_type": "voluntary",
		"priority": 100,
		# IRC §402(e)(3) defers the income tax; §3121(v)(1)(A) keeps the deferral
		# in the FICA wage base. Both halves of that, in two flags.
		"pre_tax": True,
		"fica_exempt": False,
		"limit": "none",
	},
	"health_insurance": {
		"label": "Health Insurance",
		"deduction_type": "voluntary",
		"priority": 110,
		"pre_tax": True,
		"fica_exempt": True,
		"limit": "none",
	},
	"dental_vision": {
		"label": "Dental / Vision",
		"deduction_type": "voluntary",
		"priority": 120,
		"pre_tax": True,
		"fica_exempt": True,
		"limit": "none",
	},
	"hsa": {
		"label": "HSA",
		"deduction_type": "voluntary",
		"priority": 130,
		"pre_tax": True,
		"fica_exempt": True,
		"limit": "none",
	},
	"fsa": {
		"label": "FSA",
		"deduction_type": "voluntary",
		"priority": 140,
		"pre_tax": True,
		"fica_exempt": True,
		"limit": "none",
	},
	# ── Voluntary, post-tax ──────────────────────────────────────────────────
	"life_insurance": {
		"label": "Life Insurance",
		"deduction_type": "voluntary",
		"priority": 150,
		# Group term life is ordinarily paid with after-tax money, and the part
		# of it that IS a tax question — coverage over $50,000 — is imputed
		# INCOME under IRC §79 rather than a deduction, so it does not belong
		# here at all and is not modelled as one.
		"pre_tax": False,
		"fica_exempt": False,
		"limit": "none",
	},
	"union_dues": {
		"label": "Union Dues",
		"deduction_type": "voluntary",
		"priority": 160,
		"pre_tax": False,
		"fica_exempt": False,
		"limit": "none",
	},
	"other": {
		"label": "Other",
		"deduction_type": "voluntary",
		"priority": 170,
		"pre_tax": False,
		"fica_exempt": False,
		"limit": "none",
	},
}

#: The category names, for a Select's options and for validation.
CATEGORIES = tuple(CATEGORY_SPECS)

#: The ones that default to a garnishment, and the ones that default to
#: voluntary. Derived rather than restated, so a category added above cannot
#: land in one list and be forgotten by the other.
GARNISHMENT_CATEGORIES = tuple(
	name for name, spec in CATEGORY_SPECS.items() if spec["deduction_type"] == "garnishment"
)
VOLUNTARY_CATEGORIES = tuple(
	name for name, spec in CATEGORY_SPECS.items() if spec["deduction_type"] == "voluntary"
)

#: The categories whose contributions come out of the wage base before
#: withholding, and of those, the ones that also leave the FICA base.
PRE_TAX_CATEGORIES = tuple(name for name, spec in CATEGORY_SPECS.items() if spec["pre_tax"])
FICA_EXEMPT_CATEGORIES = tuple(name for name, spec in CATEGORY_SPECS.items() if spec["fica_exempt"])


#: Which reduced base a state's own employee withholding is computed on.
#:
#: "federal" means the state taxes what the federal income tax taxes, so a
#: 401(k) deferral is out of the base along with the Section 125 benefits.
#: Oregon is one of these: ORS 316.022 defines taxable income by reference to
#: federal taxable income, so anything §402(e)(3) defers federally is deferred
#: in Oregon too. Most states with an income tax work this way, which is why it
#: is the DEFAULT for a state not named here.
#:
#: "fica" means the state's employee-side items are payroll ASSESSMENTS rather
#: than an income tax, and follow the FICA definition of wages — Section 125
#: benefits are out, a 401(k) deferral is in. Washington is the case in hand:
#: it levies no income tax at all, and what it does take from an employee is
#: Paid Family & Medical Leave and WA Cares premiums, both assessed on wages in
#: the Social Security sense. Sending them the income tax base would exempt
#: every 401(k) deferral from a premium that is owed on it.
STATE_TAXABLE_BASIS = {
	"OR": "federal",
	"WA": "fica",
}


def _as_float(value, default: float = 0.0) -> float:
	try:
		if value is None or value == "":
			return default
		return float(value)
	except (TypeError, ValueError):
		return default


def _as_bool(value) -> bool:
	"""Frappe Checks arrive as 0/1, JSON as true/false, a fixture as a string."""
	if isinstance(value, str):
		return value.strip().casefold() in ("1", "true", "yes", "y", "on")
	return bool(value)


def _key(value, allowed: tuple, default: str) -> str:
	"""Normalise a Select's human spelling to the key this module switches on.

	`"Child Support"` → `"child_support"`, `"Net After Tax"` → `"net_after_tax"`.
	The doctype spells its options the way a person reads them and this module is
	keyed the way a dict is; one mapping rather than two spellings kept in step by
	hand, which is the same choice `payroll_calc.normalise_min_wage_region` made
	for the wage regions and for the same reason.

	FALLS BACK RATHER THAN RAISING, because the caller is a payroll run and the
	value came off a Select on somebody's deduction row. A category this app does
	not recognise should not take a whole company's payroll down.
	"""
	raw = str(value or "").strip().casefold().replace(" ", "_").replace("-", "_")
	raw = raw.replace("(", "").replace(")", "").replace("/", "_").replace("__", "_")
	return raw if raw in allowed else default


def category_spec(category: str) -> dict:
	"""What the law says about one category. Unknown falls back to `other`."""
	return CATEGORY_SPECS.get(_key(category, CATEGORIES, "other"), CATEGORY_SPECS["other"])


def ccpa_exempt_floor(pay_frequency: str, minimum_wage: float = FEDERAL_MINIMUM_WAGE) -> float:
	"""The pay a creditor garnishment may not reach: 30 × minimum wage a week.

	29 CFR 870.10(b). An unknown frequency is treated as weekly, which is the
	most protective reading and the one the regulation itself is written in.
	"""
	multiplier = CCPA_PERIOD_MULTIPLIERS.get(pay_frequency, CCPA_PERIOD_MULTIPLIERS["Weekly"])
	return round(multiplier * max(_as_float(minimum_wage), 0.0), 2)


def ordinary_garnishment_ceiling(
	disposable: float,
	pay_frequency: str,
	minimum_wage: float = FEDERAL_MINIMUM_WAGE,
	state_cap_rate: float | None = None,
) -> dict:
	"""The most an ordinary garnishment may take, and which of the two rules bound it.

	The LESSER of 25% of disposable earnings and the amount by which disposable
	earnings exceed the floor. Returned with both candidates and the name of the
	binding one, because "why did only $12 come out of a $400 cheque" is a
	question a payroll clerk is asked and the answer is one of two sentences.

	`state_cap_rate` replaces the 25% where a state is stricter. Title III sets a
	floor under the worker's protection and never a ceiling on it — 15 U.S.C.
	§1677 — so the tighter rule always wins and passing a LOOSER state rate here
	does not loosen anything.
	"""
	disposable = max(_as_float(disposable), 0.0)
	rate = CCPA_ORDINARY_RATE
	state_rate = _as_float(state_cap_rate, -1.0)
	if 0.0 <= state_rate < rate:
		rate = state_rate

	floor = ccpa_exempt_floor(pay_frequency, minimum_wage)
	by_rate = round(disposable * rate, 2)
	above_floor = round(max(disposable - floor, 0.0), 2)
	ceiling = min(by_rate, above_floor)
	return {
		"ceiling": round(ceiling, 2),
		"rate": rate,
		"disposable_earnings": round(disposable, 2),
		"by_percentage": by_rate,
		"by_minimum_wage_floor": above_floor,
		"exempt_floor": floor,
		"binding_rule": (
			"30x federal minimum wage floor"
			if above_floor <= by_rate
			else f"{rate:.0%} of disposable earnings"
		),
	}


def child_support_ceiling(disposable: float, rows: list[dict]) -> dict:
	"""The support ceiling, taken from the most permissive order on the slip.

	§1673(b)(2) picks the rate from two facts about the EMPLOYEE — do they support
	another spouse or child, and is this order more than twelve weeks in arrears —
	so where two orders disagree about the first, one of them is wrong about a
	fact that is not a property of the order. The higher ceiling is taken and both
	inputs are reported, because refusing to compute would stop the payroll and
	taking the lower one would under-collect a support order, which is the failure
	mode with a penalty attached.
	"""
	supports_others = True
	arrears = False
	for row in rows or []:
		if not _as_bool(row.get("supports_other_dependents", True)):
			supports_others = False
		if _as_bool(row.get("arrears_over_12_weeks")):
			arrears = True

	rate = (
		CHILD_SUPPORT_RATE_SUPPORTING_OTHERS if supports_others else CHILD_SUPPORT_RATE_NOT_SUPPORTING_OTHERS
	)
	if arrears:
		rate += CHILD_SUPPORT_ARREARS_INCREMENT

	disposable = max(_as_float(disposable), 0.0)
	return {
		"ceiling": round(disposable * rate, 2),
		"rate": rate,
		"supports_other_dependents": supports_others,
		"arrears_over_12_weeks": arrears,
		"basis": "15 U.S.C. 1673(b)(2)",
	}


def is_active(row: dict, on_date: str = "") -> bool:
	"""Is this deduction in force on `on_date`?

	Status `active`, and the date inside `[effective_from, effective_to]` where
	either is set. An empty `on_date` skips the date test, which is what a caller
	that has already filtered by date wants and what a unit test writes.

	Dates compare as ISO strings deliberately: they arrive from Frappe as `date`
	objects and from a fixture as `"2026-03-01"`, and `str()` makes both sortable
	without importing a parser into a module whose whole contract is that it does
	no work the caller could not see.
	"""
	if _key(row.get("status"), STATUSES, "active") != "active":
		return False
	if not on_date:
		return True
	when = str(on_date)[:10]
	start = str(row.get("effective_from") or "")[:10]
	end = str(row.get("effective_to") or "")[:10]
	if start and when < start:
		return False
	if end and when > end:
		return False
	return True


def active_deductions(rows: list[dict], on_date: str = "") -> list[dict]:
	"""Every deduction in force on a date, in the order they will be taken.

	Sorted by effective priority, then by `effective_from` so that of two orders
	of the same rank the older one is satisfied first — first in time is the rule
	almost everywhere for creditor garnishments — and finally by name so two runs
	of the same period produce the same slip.
	"""
	live = [dict(row) for row in rows or [] if is_active(row, on_date)]
	return sorted(
		live,
		key=lambda row: (
			row_priority(row),
			str(row.get("effective_from") or ""),
			str(row.get("name") or ""),
		),
	)


def row_priority(row: dict) -> int:
	"""Where this row sits in the queue. Its own `priority`, else its category's.

	Zero is not a priority, it is an empty Int field: Frappe defaults every Int to
	0, so treating 0 as "first" would silently promote every row somebody never
	filled in ahead of a child support order.
	"""
	explicit = row.get("priority")
	try:
		value = int(explicit)
		if value > 0:
			return value
	except (TypeError, ValueError):
		pass
	return int(category_spec(row.get("deduction_category")).get("priority", 500))


def row_type(row: dict) -> str:
	"""Garnishment or voluntary. The row's own answer beats the category's.

	Both exist because `other` is a legitimate category on either side of the
	line: a creditor garnishment nobody had a better word for is still a
	garnishment and still outranks a union due.
	"""
	spec = category_spec(row.get("deduction_category"))
	return _key(row.get("deduction_type"), DEDUCTION_TYPES, spec["deduction_type"])


def row_is_pre_tax(row: dict) -> bool:
	"""Does this come out of the wage base? The row may override its category.

	A garnishment is NEVER pre-tax whatever the row says — money taken under a
	court order is wages the worker earned and was taxed on, and the order is
	served on what is left. Letting a row assert otherwise would under-report
	taxable wages on a W-2.
	"""
	if row_type(row) == "garnishment":
		return False
	spec = category_spec(row.get("deduction_category"))
	override = row.get("pre_tax")
	if override in (None, ""):
		return bool(spec["pre_tax"])
	return _as_bool(override)


def row_is_fica_exempt(row: dict) -> bool:
	"""Does it leave the Social Security and Medicare base too?

	Only meaningful where the row is pre-tax at all. This is the flag that
	separates a Section 125 premium from a 401(k) deferral — see the module
	docstring, this is the whole of that distinction in code.
	"""
	if not row_is_pre_tax(row):
		return False
	spec = category_spec(row.get("deduction_category"))
	override = row.get("fica_exempt")
	if override in (None, ""):
		return bool(spec["fica_exempt"])
	return _as_bool(override)


def row_label(row: dict) -> str:
	"""What this line is called on a pay stub."""
	explicit = str(row.get("label") or row.get("description") or "").strip()
	if explicit:
		return explicit
	return str(category_spec(row.get("deduction_category")).get("label", "Deduction"))


def scheduled_amount(row: dict, gross_pay: float, disposable: float) -> dict:
	"""What this row ASKS for, before any legal ceiling is applied.

	A fixed amount is the amount. A percentage is of `gross_pay` or of
	`net_after_tax`, which is the disposable earnings figure — one figure for the
	whole slip rather than a running balance, so two rows asking for the same
	percentage always collect the same money whatever order they are in.

	`max_per_period` caps the result. It is the field that makes a percentage
	safe: a 401(k) at 6% of a harvest week with heavy overtime can exceed a plan's
	per-period limit, and the cap is what a plan administrator sets against that.
	A cap of zero is NOT a cap of nothing — currency fields default to 0 on every
	row of every doctype, so a zero means the field was never filled in, exactly
	as `payroll_calc.state_min_wage_rates` reads a zero minimum wage.
	"""
	amount_type = _key(row.get("amount_type"), AMOUNT_TYPES, "fixed")
	raw = max(_as_float(row.get("amount")), 0.0)

	if amount_type == "percentage":
		basis_key = _key(row.get("basis"), BASES, _default_basis(row))
		basis_amount = gross_pay if basis_key == "gross_pay" else disposable
		requested = max(_as_float(basis_amount), 0.0) * raw / 100.0
	else:
		basis_key = ""
		basis_amount = 0.0
		requested = raw

	requested = round(requested, 2)
	capped_by = ""
	cap = _as_float(row.get("max_per_period"))
	if cap > 0 and requested > cap:
		requested = round(cap, 2)
		capped_by = "max_per_period"

	return {
		"amount_type": amount_type,
		"rate": raw if amount_type == "percentage" else 0.0,
		"basis": basis_key,
		"basis_amount": round(_as_float(basis_amount), 2),
		"requested": requested,
		"max_per_period": round(cap, 2) if cap > 0 else 0.0,
		"capped_by": capped_by,
	}


def _default_basis(row: dict) -> str:
	"""What a percentage means where the row does not say.

	Disposable earnings for a garnishment, because that is the figure every court
	order and every withholding notice is written against. Gross for a voluntary
	election, because that is how a 401(k) deferral rate is expressed and what a
	plan document means by "6%".
	"""
	return "net_after_tax" if row_type(row) == "garnishment" else "gross_pay"


def calculate_pre_tax_deductions(gross_pay: float, rows: list[dict]) -> dict:
	"""The elections that come out of the wage base, and the two bases they leave.

	Returns the lines, the total, and `federal_taxable_gross` /
	`fica_taxable_gross` — which differ from each other on any slip carrying a
	401(k), because a deferral leaves the income tax base and stays in the FICA
	one. See the module docstring; this function is where that becomes two
	numbers.

	NOTHING HERE MAY DRIVE A BASE BELOW ZERO. Elections totalling more than the
	pay are cut in reverse priority and reported, rather than producing a negative
	wage base that would make the withholding engines compute a refund.
	"""
	gross_pay = max(_as_float(gross_pay), 0.0)
	available = gross_pay
	lines = []
	shortfalls = []
	total = fica_exempt_total = 0.0

	for row in rows:
		if not row_is_pre_tax(row):
			continue
		schedule = scheduled_amount(row, gross_pay, gross_pay)
		requested = schedule["requested"]
		taken = round(min(requested, available), 2)
		available = round(available - taken, 2)

		fica_exempt = row_is_fica_exempt(row)
		total += taken
		if fica_exempt:
			fica_exempt_total += taken

		line = _line(row, schedule, taken, pre_tax=True, fica_exempt=fica_exempt)
		if taken < requested - 0.005:
			line["shortfall"] = round(requested - taken, 2)
			line["shortfall_reason"] = "gross pay exhausted before this election"
			shortfalls.append(dict(line))
		lines.append(line)

	total = round(total, 2)
	fica_exempt_total = round(fica_exempt_total, 2)
	return {
		"lines": lines,
		"total": total,
		"fica_exempt_total": fica_exempt_total,
		# A 401(k) deferral is in this figure and not in the one above it. The
		# gap between these two bases is exactly the deferral, and on a W-2 it is
		# the gap between Box 1 and Box 3.
		"federal_taxable_gross": round(max(gross_pay - total, 0.0), 2),
		"fica_taxable_gross": round(max(gross_pay - fica_exempt_total, 0.0), 2),
		"shortfalls": shortfalls,
	}


def disposable_earnings(gross_pay: float, statutory_withholding: float) -> float:
	"""29 CFR 870.10(a): pay less the amounts the law REQUIRES to be withheld.

	Federal and state income tax, Social Security and Medicare — and nothing
	else. Not the 401(k), not the health premium, however "pre-tax" they are:
	those are the employee's own elections, and letting them shrink this figure
	would let the employee choose how much of a court order to obey.
	"""
	return round(max(_as_float(gross_pay) - _as_float(statutory_withholding), 0.0), 2)


def _line(row: dict, schedule: dict, taken: float, pre_tax: bool, fica_exempt: bool) -> dict:
	"""One deduction, as it appears on a slip and on a pay stub."""
	return {
		"deduction": str(row.get("name") or ""),
		"label": row_label(row),
		"deduction_type": row_type(row),
		"deduction_category": _key(row.get("deduction_category"), CATEGORIES, "other"),
		"amount_type": schedule["amount_type"],
		"rate": schedule["rate"],
		"basis": schedule["basis"],
		"basis_amount": schedule["basis_amount"],
		"priority": row_priority(row),
		"requested": schedule["requested"],
		"amount": round(taken, 2),
		"pre_tax": pre_tax,
		"fica_exempt": fica_exempt,
		"max_per_period": schedule["max_per_period"],
		"capped_by": schedule["capped_by"],
		"reference": str(row.get("reference") or ""),
		"shortfall": 0.0,
	}


#: v0.292.0. The default catch-up policy when the caller passes none (payroll setting `garnishment_policy` is the
#: live one): the middle path — the current amount, then as much of the arrears as still fits under the cap.
DEFAULT_GARNISHMENT_POLICY = {"catch_up": "middle_path", "carry_forward": True, "current_before_arrears": True}


def _arrears_wanted(row: dict, policy: dict) -> float:
	"""How much of this order's arrears to try for this period, before any cap.

	The ORDER'S OWN TERMS WIN: an order that sets an arrears amount per period gets that (never more than the balance).
	Otherwise the policy decides — `middle_path` tries for the whole balance and lets the cap say how much fits;
	`none` takes the current amount only and lets the arrears stand.
	"""
	balance = max(_as_float(row.get("arrears_balance")), 0.0)
	if balance <= 0:
		return 0.0
	ordered = max(_as_float(row.get("arrears_per_period")), 0.0)
	if ordered > 0:
		return round(min(ordered, balance), 2)
	if (policy or {}).get("catch_up", "middle_path") == "none":
		return 0.0
	return round(balance, 2)


def _limits(limits: dict | None, pay_frequency: str, minimum_wage: float, state_cap_rate) -> dict:
	"""The ordinary-garnishment rule for this period: the STRICTER of federal and state (15 U.S.C. §1677)."""
	federal = dict((limits or {}).get("federal") or {})
	state = dict((limits or {}).get("state") or {})
	raw_wage = federal.get("federal_minimum_wage")
	fmw = _as_float(raw_wage) if raw_wage not in (None, "") else minimum_wage
	multipliers = federal.get("period_multipliers") or CCPA_PERIOD_MULTIPLIERS
	fed_floor = round(_as_float(multipliers.get(pay_frequency, multipliers.get("Weekly", 30.0))) * fmw, 2)
	rate = _as_float(federal.get("ordinary_rate"), CCPA_ORDINARY_RATE)
	floor, floor_source = fed_floor, f"30x federal minimum wage (${fmw:.2f})"
	if state:
		state_rate = _as_float(state.get("ordinary_rate"), rate)
		if 0.0 <= state_rate < rate:
			rate = state_rate
		state_floor = _as_float((state.get("floors") or {}).get(pay_frequency), 0.0)
		if state_floor > floor:
			floor, floor_source = state_floor, f"{state.get('state')} minimum exemption (${state_floor:,.2f})"
	legacy = _as_float(state_cap_rate, -1.0)
	if 0.0 <= legacy < rate:
		rate = legacy
	return {"rate": rate, "floor": floor, "floor_source": floor_source, "federal": federal, "state": state,
	        "support_rates": federal.get("support_rates") or {},
	        "student_loan_rate": _as_float(federal.get("student_loan_rate"), STUDENT_LOAN_RATE)}


def _ordinary_ceiling(disposable: float, rule: dict) -> dict:
	by_rate = round(disposable * rule["rate"], 2)
	above_floor = round(max(disposable - rule["floor"], 0.0), 2)
	ceiling = min(by_rate, above_floor)
	return {"ceiling": round(ceiling, 2), "rate": rule["rate"], "disposable_earnings": round(disposable, 2),
	        "by_percentage": by_rate, "by_minimum_wage_floor": above_floor, "exempt_floor": rule["floor"],
	        "binding_rule": rule["floor_source"] if above_floor <= by_rate else f"{rule['rate']:.0%} of disposable earnings"}


def _support_rate(rows: list, rates: dict) -> tuple:
	supports_others = all(_as_bool(r.get("supports_other_dependents", True)) for r in rows or [])
	arrears = any(_as_bool(r.get("arrears_over_12_weeks")) for r in rows or [])
	key = ("supporting" if supports_others else "not_supporting") + ("_arrears" if arrears else "")
	default = {"supporting": 0.50, "supporting_arrears": 0.55, "not_supporting": 0.60, "not_supporting_arrears": 0.65}
	return _as_float((rates or {}).get(key), default[key]), supports_others, arrears


def _finish(line: dict, current: float, catch_up: float, current_taken: float, catch_up_taken: float, row: dict,
            rule: str, cap: float, reason: str, shortfalls: list) -> dict:
	"""The line's catch-up and carry-forward book-keeping — what the ledger, the stub and the report read."""
	before = round(max(_as_float(row.get("arrears_balance")), 0.0), 2)
	carried = round(max(current - current_taken, 0.0), 2)
	line.update({"amount": round(current_taken + catch_up_taken, 2), "requested": round(current + catch_up, 2),
	             "current_requested": round(current, 2), "current_taken": round(current_taken, 2),
	             "catch_up_requested": round(catch_up, 2), "catch_up_taken": round(catch_up_taken, 2),
	             "arrears_before": before, "carried_forward": carried,
	             "arrears_after": round(before - catch_up_taken + carried, 2),
	             "limit_rule": rule, "limit_ceiling": round(cap, 2), "cap_amount": round(cap, 2),
	             "arrears_per_period": round(max(_as_float(row.get("arrears_per_period")), 0.0), 2)})
	if carried > 0.005:
		line["shortfall"] = carried
		line["shortfall_reason"] = reason + " — carried forward to the next period"
		shortfalls.append(dict(line))
	return line


def take_less(line: dict, take: float, reason: str) -> dict:
	"""A garnishment line cut to `take` when the cash is not there (a daily-pay settlement's net). v0.292.0.

	The arrears catch-up gives way first, then the current amount; what the current amount loses is carried forward
	exactly as a cap would carry it, so the ledger and the next period see the same arithmetic either way.
	"""
	line = dict(line)
	take = round(max(min(_as_float(take), _as_float(line.get("amount"))), 0.0), 2)
	cut = round(_as_float(line.get("amount")) - take, 2)
	if cut <= 0:
		return line
	catch_up = _as_float(line.get("catch_up_taken"))
	catch_cut = min(catch_up, cut)
	current_taken = round(_as_float(line.get("current_taken", line.get("amount"))) - (cut - catch_cut), 2)
	catch_up = round(catch_up - catch_cut, 2)
	current = _as_float(line.get("current_requested", line.get("requested")))
	carried = round(max(current - current_taken, 0.0), 2)
	before = _as_float(line.get("arrears_before"))
	line.update({"amount": take, "current_taken": current_taken, "catch_up_taken": catch_up,
	             "carried_forward": carried, "arrears_after": round(before - catch_up + carried, 2),
	             "shortfall": carried, "shortfall_reason": reason + (" — carried forward to the next period"
	                                                                  if carried > 0.005 else "")})
	return line


#: v0.300.0. ORS 652.610(3): what may lawfully come out of wages.
AUTHORITY_LAW, AUTHORITY_WRITTEN, AUTHORITY_CBA = ("Required by law", "Employee written authorization",
                                                   "Collective bargaining agreement")


def authorization_missing(row: dict) -> str:
	"""Why a deduction may NOT be taken, or "" (ORS 652.610(3)). Garnishments are required by law; an election needs
	the worker's signed written authorization or a collective bargaining agreement."""
	if row_type(row) == "garnishment":
		return ""
	authority = str(row.get("legal_authority") or "")
	if authority in (AUTHORITY_LAW, AUTHORITY_CBA):
		return ""
	if row.get("authorization_signature") or row.get("authorization_document") or row.get("authorized_on"):
		return ""
	return ("no signed written authorization on file — ORS 652.610(3) allows a voluntary deduction only with the "
	        "employee's written authorization (or a collective bargaining agreement). Collect it "
	        "(collect_form_signature doctype='Farm Payroll Deduction' field='authorization_signature', or attach the "
	        "signed form) and it is taken from the next run.")


def apply_garnishments(
	disposable: float,
	rows: list[dict],
	pay_frequency: str = "Biweekly",
	minimum_wage: float = FEDERAL_MINIMUM_WAGE,
	state_cap_rate: float | None = None,
	limits: dict | None = None,
	policy: dict | None = None,
) -> dict:
	"""Court orders against the caps, in legal priority order, with arrears catch-up and carry-forward. v0.292.0.

	PRIORITY (the policy's `priority`, checked by its validator; sources there): 1. child support, against its own
	§1673(b)(2) ceiling of 50-65% — several orders prorated by amount, every order's CURRENT support before any order's
	arrears (42 U.S.C. §666(b)(5); ORS 25.378); 2. federal and state tax levies, outside Title III (29 CFR
	870.11(b)(2)); 3. student loans at 15%, inside the ordinary pool; 4. creditor orders out of what is left of the
	ordinary pool after support (29 CFR 870.11(b)(1); ORS 18.385(5)). The ordinary pool is the STRICTER of federal (25%
	/ 30× federal minimum wage) and the state's dated rule (`limits`) — Oregon's ORS 18.385 floor, Washington's RCW
	6.27.150 — and a consumer-debt order in a state with its own rule gets that ceiling too.

	CATCH-UP AND CARRY-FORWARD (`policy`, default the middle path): each order takes its current amount, then as much
	of its arrears (`arrears_balance`, kept by the caller's ledger) as still fits — or the order's own
	`arrears_per_period` where it sets one, still capped. What the current amount could not take is CARRIED FORWARD
	into the arrears and reported. Nothing here ever exceeds a cap; every line says which cap bound it.
	"""
	policy = {**DEFAULT_GARNISHMENT_POLICY, **(policy or {})}
	disposable = max(_as_float(disposable), 0.0)
	rule = _limits(limits, pay_frequency, minimum_wage, state_cap_rate)
	ordinary = _ordinary_ceiling(disposable, rule)

	garnishments = [row for row in rows if row_type(row) == "garnishment"]
	support_rows = [row for row in garnishments if _limit_of(row) == "child_support"]
	levy_rows = [row for row in garnishments if _limit_of(row) == "exempt_from_ccpa"]
	loan_rows = [row for row in garnishments if _limit_of(row) == "student_loan"]
	other_rows = [row for row in garnishments
	              if _limit_of(row) not in ("child_support", "exempt_from_ccpa", "student_loan")]
	lines, shortfalls = [], []

	# ── 1. Child support: every order's current support, then arrears, under one ceiling ─────────────
	rate, supports_others, arrears_flag = _support_rate(support_rows, rule["support_rates"])
	support_ceiling = round(disposable * rate, 2)
	support = {"ceiling": support_ceiling, "rate": rate, "supports_other_dependents": supports_others,
	           "arrears_over_12_weeks": arrears_flag, "basis": "15 U.S.C. 1673(b)(2)"}
	support_taken = 0.0
	if support_rows:
		current = [scheduled_amount(row, disposable, disposable) for row in support_rows]
		wanted_current = [c["requested"] for c in current]
		room = support_ceiling
		taken_current = _share(wanted_current, room)
		room = round(room - sum(taken_current), 2)
		wanted_arrears = [_arrears_wanted(row, policy) for row in support_rows]
		taken_arrears = _share(wanted_arrears, room)
		prorated = sum(wanted_current) > support_ceiling + 0.005
		for row, schedule, cur, cur_t, arr, arr_t in zip(support_rows, current, wanted_current, taken_current,
		                                                 wanted_arrears, taken_arrears, strict=True):
			line = _line(row, schedule, 0.0, pre_tax=False, fica_exempt=False)
			line["prorated"] = bool(prorated)
			reason = (f"child support ceiling of {rate:.0%} of disposable earnings (${support_ceiling:,.2f})"
			          + (" shared pro rata across the orders on file" if prorated else ""))
			lines.append(_finish(line, cur, arr, cur_t, arr_t, row, "child_support", support_ceiling, reason, shortfalls))
			support_taken += cur_t + arr_t
	support_taken = round(support_taken, 2)

	# ── 2. Tax levies, outside Title III ─────────────────────────────────────
	levy_taken = 0.0
	for row in levy_rows:
		schedule = scheduled_amount(row, disposable, disposable)
		exempt = max(_as_float(row.get("exempt_amount")), 0.0)
		room = round(max(disposable - support_taken - levy_taken - exempt, 0.0), 2)
		cur = schedule["requested"]
		cur_t = round(min(cur, room), 2)
		arr = _arrears_wanted(row, policy)
		arr_t = round(min(arr, room - cur_t), 2)
		levy_taken = round(levy_taken + cur_t + arr_t, 2)
		line = _line(row, schedule, 0.0, pre_tax=False, fica_exempt=False)
		line["exempt_amount"] = round(exempt, 2)
		reason = ("disposable earnings exhausted; a tax levy is not limited by the CCPA (29 CFR 870.11(b)(2)) but "
		          "cannot exceed the pay" + (f", and ${exempt:,.2f} is exempt under IRC 6334(d)" if exempt else ""))
		lines.append(_finish(line, cur, arr, cur_t, arr_t, row, "exempt_from_ccpa", room, reason, shortfalls))

	# ── 3 & 4. The shared ordinary pool: student loans, then creditor orders ───────────────
	pool = round(max(ordinary["ceiling"] - support_taken, 0.0), 2)
	pool_opening = pool
	consumer = (rule["state"].get("consumer") or {}) if rule["state"] else {}
	for row in loan_rows + other_rows:
		schedule = scheduled_amount(row, disposable, disposable)
		is_loan = row in loan_rows
		own = pool
		rule_name, reason = "ordinary", f"ordinary garnishment ceiling: {ordinary['binding_rule']} (${ordinary['ceiling']:,.2f})"
		if is_loan:
			own = round(min(disposable * rule["student_loan_rate"], pool), 2)
			rule_name = "student_loan"
			reason = (f"student loan garnishment is capped at {rule['student_loan_rate']:.0%} of disposable earnings "
			          "and shares the CCPA pool")
		elif _as_bool(row.get("consumer_debt")) and consumer:
			c_floor = _as_float((consumer.get("floors") or {}).get(pay_frequency), 0.0)
			c_cap = min(disposable * _as_float(consumer.get("rate"), rule["rate"]), max(disposable - c_floor, 0.0))
			own = round(min(c_cap, pool), 2)
			rule_name = "consumer_debt"
			reason = f"{rule['state'].get('state')} consumer-debt ceiling ({consumer.get('source')})"
		if support_taken > 0 and not is_loan:
			reason += f", of which ${support_taken:,.2f} was taken by support orders (29 CFR 870.11(b)(1))"
		cur = schedule["requested"]
		cur_t = round(max(min(cur, own), 0.0), 2)
		arr = _arrears_wanted(row, policy)
		arr_t = round(max(min(arr, own - cur_t), 0.0), 2)
		pool = round(max(pool - cur_t - arr_t, 0.0), 2)
		line = _line(row, schedule, 0.0, pre_tax=False, fica_exempt=False)
		lines.append(_finish(line, cur, arr, cur_t, arr_t, row, rule_name, own, reason, shortfalls))

	total = round(sum(line["amount"] for line in lines), 2)
	return {
		"lines": lines,
		"total": total,
		"shortfalls": shortfalls,
		"disposable_earnings": round(disposable, 2),
		"ccpa": ordinary,
		"child_support": support,
		"child_support_withheld": support_taken,
		"tax_levy_withheld": round(levy_taken, 2),
		"ordinary_pool": pool_opening,
		"ordinary_pool_remaining": pool,
		"catch_up_taken": round(sum(line.get("catch_up_taken", 0) for line in lines), 2),
		"carried_forward": round(sum(line.get("carried_forward", 0) for line in lines), 2),
		"policy": policy,
		"limits_applied": {"rate": rule["rate"], "floor": rule["floor"], "floor_source": rule["floor_source"],
		                   "state": (rule["state"] or {}).get("state")},
	}


def _share(wanted: list, room: float) -> list:
	"""Give each request its amount, or — when they will not all fit — a share in proportion, never above `room`."""
	total = sum(wanted)
	room = max(room, 0.0)
	if total <= room + 0.005:
		return [round(w, 2) for w in wanted]
	if total <= 0:
		return [0.0 for _ in wanted]
	shares = [round(room * (w / total), 2) for w in wanted]
	excess = round(sum(shares) - room, 2)
	if excess > 0 and shares:  # rounding never takes a cent over the ceiling
		i = shares.index(max(shares))
		shares[i] = round(shares[i] - excess, 2)
	return shares


def _limit_of(row: dict) -> str:
	"""Which ceiling governs this row. The category decides; `other` is ordinary.

	A garnishment filed under the `other` category has no special statute behind
	it, so it gets the ordinary creditor treatment — the most restrictive of the
	four and the right default for something nobody classified.
	"""
	spec = category_spec(row.get("deduction_category"))
	limit = spec.get("limit", "ordinary")
	return "ordinary" if limit == "none" else limit


def apply_post_tax_deductions(
	available: float, rows: list[dict], gross_pay: float, disposable: float
) -> dict:
	"""Voluntary after-tax elections, out of whatever cash the orders left.

	LAST, and that ordering is the point of the function: a garnishment outranks
	a union due, so when the money runs out it is the union due that goes short
	and never the court order. Each line takes what it can and reports what it
	could not, in priority order.
	"""
	available = max(_as_float(available), 0.0)
	lines = []
	shortfalls = []

	for row in rows:
		if row_type(row) == "garnishment" or row_is_pre_tax(row):
			continue
		schedule = scheduled_amount(row, gross_pay, disposable)
		requested = schedule["requested"]
		taken = round(max(min(requested, available), 0.0), 2)
		available = round(max(available - taken, 0.0), 2)

		line = _line(row, schedule, taken, pre_tax=False, fica_exempt=False)
		if taken < requested - 0.005:
			line["shortfall"] = round(requested - taken, 2)
			line["shortfall_reason"] = (
				"net pay exhausted; garnishments and higher-priority deductions come first"
			)
			shortfalls.append(dict(line))
		lines.append(line)

	return {
		"lines": lines,
		"total": round(sum(line["amount"] for line in lines), 2),
		"shortfalls": shortfalls,
		"remaining": available,
	}


def summarize_deductions(lines: list[dict]) -> dict:
	"""Totals a slip and a pay stub want, off one pass of the lines."""
	by_category: dict[str, float] = {}
	pre_tax = post_tax = garnishment = voluntary = 0.0
	for line in lines or []:
		amount = _as_float(line.get("amount"))
		by_category[line.get("deduction_category", "other")] = round(
			by_category.get(line.get("deduction_category", "other"), 0.0) + amount, 2
		)
		if line.get("deduction_type") == "garnishment":
			garnishment += amount
		else:
			voluntary += amount
		if line.get("pre_tax"):
			pre_tax += amount
		else:
			post_tax += amount

	return {
		"total": round(pre_tax + post_tax, 2),
		"pre_tax_total": round(pre_tax, 2),
		"post_tax_total": round(post_tax, 2),
		"garnishment_total": round(garnishment, 2),
		"voluntary_total": round(voluntary, 2),
		"by_category": by_category,
		"line_count": len(lines or []),
	}
