# SPDX-License-Identifier: MIT
"""The breakeven calculator: what price does this crop have to make?

FIVE TOOLS OVER ONE RECORD, AND THE RECORD IS A PERSPECTIVE ON THE LEDGER THE
FARM ALREADY KEEPS. Nothing here posts, nothing here is a journal entry, and no
number produced by any of it changes what the financial statements say. What a
Breakeven Analysis adds to the chart of accounts is the one thing a chart of
accounts cannot hold: which accounts stop mattering when the crop gets bigger.

────────────────────────────────────────────────────────────────────────────
THE MODEL, IN THE ORDER SOMEBODY WOULD ARGUE IT
────────────────────────────────────────────────────────────────────────────

A textbook breakeven has one volume. A fruit farm has two, and every interesting
property of this module follows from that.

Picking, hauling and field bins are bought for EVERYTHING THAT COMES OFF THE
TREES. Cartons, packing labour, freight and commission are bought only for WHAT
PACKS OUT. When packout falls from 85% to 60%, the second pile falls with it and
the first does not — it simply spreads over fewer sellable boxes. So a cost line
carries a `volume_basis`, and the per-unit arithmetic is:

    variable cost per sellable unit  =  vh / p  +  vs
    cull credit per sellable unit    =  c · (1 − p) / p
    contribution margin per unit     =  P + cull credit − variable cost

where `vh` is the harvest-basis variable cost per HARVESTED unit, `vs` the
sellable-basis variable cost per SELLABLE unit, `p` the packout, `c` what a cull
returns and `P` the expected net price. Every packed box carries 1/p harvested
units of picking with it, and brings (1−p)/p culls' worth of juice money along.

A model with a single variable pile gets the DIRECTION of a packout change right
and the magnitude wrong, which is the more dangerous of the two errors: it looks
like an answer.

THE CULL CREDIT IS NOT DECORATION. Thirty percent culls at juice price is not
thirty percent of the crop earning nothing, and a model that treated it that way
would make every light-packout scenario look worse than it is — which is the
direction that gets fruit left on the tree that should have been picked.

────────────────────────────────────────────────────────────────────────────
THE BASELINE, WHICH IS WHAT MAKES THE SLIDER AND THE SCENARIOS AGREE
────────────────────────────────────────────────────────────────────────────

A ledger gives TOTALS for a window; a model needs RATES. Turning one into the
other means dividing by the volume those totals correspond to, and the whole
correctness of a packout slider turns on which volume that is.

Get it wrong and the two-pile model quietly collapses. Re-derive both rates at
whatever the slider currently says and `vh/p + vs` becomes `(Vh + Vs) / S` — the
harvest/sellable split cancels out completely, and the module's entire reason for
existing evaporates while every number still looks plausible.

So the volume the rates are derived at is STORED, on the analysis, as
`baseline_harvest_units` and `baseline_packout_pct`:

    vh = harvest-basis total  /  baseline harvest
    vs = sellable-basis total / (baseline harvest × baseline packout)

Both are set on the first computation and are NOT moved by the slider. Sliding
the packout to 62% therefore means "the same money per carton, over fewer
cartons" — which is what a grower means — and `get_breakeven_sensitivity` at
-22.5% packout returns exactly the figure `compute_breakeven(packout_pct=62)`
stores. TWO TOOLS THAT ANSWERED ONE QUESTION WITH TWO NUMBERS would be worse
than either of them being slightly wrong.

`rebase_costs` is how a baseline is deliberately moved, once the season's real
crop and real packout are known. It is an argument rather than an automatic
behaviour because re-basing changes every historical comparison this record is
part of, and that is a decision rather than a side effect.

────────────────────────────────────────────────────────────────────────────
WHERE THE CLASSIFICATION COMES FROM, AND WHY EVERY LINE SAYS
────────────────────────────────────────────────────────────────────────────

Three sources, best evidence first, and the source is stored on every line:

  * ACCOUNT — an operator classified the account itself, on the Account, with
    `breakeven_cost_behavior`. Said once, true for every analysis afterwards.
  * OVERRIDE — this analysis was told to treat one account differently, once.
  * HEURISTIC — nobody has said, so this app read the account's ERPNext type and
    its name and guessed.

The heuristic is genuinely useful and it is genuinely a guess, and the whole
design turns on holding both of those at once. A first run on a real chart of
accounts classifies most of it correctly and hands back a number in a minute
rather than an afternoon; the same run reports how many lines it guessed at, and
every read repeats the count. A breakeven resting on forty guessed
classifications is a different object from one resting on none, and the person
about to quote it to a lender is entitled to know which they have.

WHAT IS NEVER GUESSED: income tax. It is excluded by rule, not by heuristic,
because at breakeven there is no pre-tax income to tax, and a model that carried
the tax line would demand the farm cover a liability it does not have.

────────────────────────────────────────────────────────────────────────────
WHAT THE READS REFUSE TO DO
────────────────────────────────────────────────────────────────────────────

NO BREAKEVEN QUANTITY WHERE THE CONTRIBUTION MARGIN IS NOT POSITIVE. There is no
such quantity — every additional box loses money — and reporting the arithmetic
limit (a very large number) would render as a hard target rather than as the
impossibility it is. The breakeven PRICE is still reported in that case, and it
is the number that matters most there: it says how far the price has to come up
before any volume helps at all.

NO CONVERSION BETWEEN PACKAGES ON THE MARKET OVERLAY. A breakeven per 40-lb box
compared against a USDA quotation per 20-lb carton is out by a factor of two and
looks entirely plausible. This app reports both packages next to the spread and
converts neither, because pack style is a judgement it has no basis to make.

NO WRITING FROM A READ. `get_breakeven_sensitivity` computes any band it is
asked for and stores nothing. `compute_breakeven` stores a standard band, so the
register's contents depend on who ran a computation rather than on who had been
browsing.
"""

from __future__ import annotations

import frappe

from .. import compat
from ..args import as_bool, as_date, as_limit, as_str, resolve_company, resolve_cost_center
from ..errors import ToolError
from ..result import ToolResult
from ..services import usda_prices

ANALYSIS = "Breakeven Analysis"
COST_LINE = "Breakeven Cost Line"
SCENARIO = "Breakeven Scenario"

CUSTOM_FIELD = "Custom Field"

_HINT = "It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app."

RECORD_CAP = 500

#: A chart of accounts is hundreds of rows, not tens of thousands. The cap is a
#: runaway guard rather than a real limit, and a run that hits it says so.
ACCOUNT_CAP = 2000
GL_ROW_CAP = 100000

BEHAVIORS = ("Fixed", "Variable", "Mixed", "Excluded")
VOLUME_BASES = ("Harvested", "Sellable")
COST_SOURCES = ("Ledger Actuals", "Budget")
UNIT_LABELS = ("Box", "Bin", "Carton", "Ton", "Pound", "Case", "Unit")

SENSITIVITY_VARIABLES = ("price", "yield", "packout", "fixed_cost", "variable_cost")

#: The band `compute_breakeven` stores without being asked. Chosen to bracket a
#: normal season's disappointments: a fifth off the price, a fifth off the crop,
#: a fifth off the packout, a fifth onto each cost pile.
STANDARD_BAND = (-20.0, -10.0, 10.0, 20.0)

#: The three columns this app adds to Account. Custom Fields rather than a
#: fixture on somebody else's doctype, for the reason `receipts.py` gives about
#: its own seven: a Custom Field is something an operator can remove, and a
#: shipped field is something only a release can.
BEHAVIOR_FIELD = "breakeven_cost_behavior"
BASIS_FIELD = "breakeven_volume_basis"
VARIABLE_PCT_FIELD = "breakeven_variable_pct"
ACCOUNT_FIELDS = (BEHAVIOR_FIELD, BASIS_FIELD, VARIABLE_PCT_FIELD)

#: Name fragments that say what an account's money does, checked in this order.
#: Lower-case, matched as substrings of the account name, and deliberately
#: SHORT — 'pick' catches "Picking Labor", "Pick Crew" and "Harvest Picking",
#: and a longer pattern would catch one of the three.
_HARVEST_WORDS = (
	"pick",
	"harvest",
	"haul",
	"field labor",
	"field labour",
	"field bin",
	"orchard bin",
	"bin rental",
	"thinning",
	"prop",
	"tote",
)
_SELLABLE_WORDS = (
	"pack",
	"carton",
	"box ",
	"boxes",
	"tray",
	"bag",
	"label",
	"plu",
	"freight",
	"commission",
	"broker",
	"storage",
	"cold storage",
	"cooling",
	"inspection",
	"assessment",
	"marketing order",
	"pallet",
	"shipping",
)
_FIXED_WORDS = (
	"rent",
	"lease",
	"insurance",
	"depreciation",
	"amortization",
	"amortisation",
	"property tax",
	"office",
	"salaries",
	"salary",
	"management",
	"professional",
	"legal",
	"accounting",
	"dues",
	"subscription",
	"license",
	"licence",
	"bank charge",
	"interest",
	"telephone",
	"internet",
	"security",
)
#: Split accounts. The share is the classic mixed-cost convention and is
#: reported as a guess like everything else the heuristic does.
_MIXED_WORDS = (
	("utilit", 60.0),
	("power", 60.0),
	("electric", 60.0),
	("water", 70.0),
	("irrigation", 70.0),
	("fuel", 70.0),
	("repair", 50.0),
	("maintenance", 50.0),
	("supplies", 50.0),
)
#: Never in a breakeven, by rule rather than by guess.
_EXCLUDED_WORDS = ("income tax", "corporate tax", "deferred tax")


def _get(row, key, default=None):
	if isinstance(row, dict):
		return row.get(key, default)
	return getattr(row, key, default)


def _require() -> None:
	compat.require_doctype(ANALYSIS, _HINT)


# ── the Account columns ─────────────────────────────────────────────────────
def ensure_account_behavior_fields() -> bool:
	"""Give Account the three columns a classification lives in. Never raises.

	Created at install AND on first use, so a bench that pulled the code without
	running the installer classifies an account the first time somebody says so.

	A SITE THAT WILL NOT TAKE THEM LOSES ONE THING AND KEEPS EVERYTHING ELSE:
	every analysis still computes, every heuristic still fires, and the only
	casualty is the ability to make a classification STICK across analyses — an
	override still works, per analysis, and every result says
	`account_classification_available: false` rather than pretending otherwise.
	"""
	try:
		if _behavior_fields_present():
			return True
		if not compat.doctype_exists(CUSTOM_FIELD) or not compat.doctype_exists("Account"):
			return False
	except Exception:
		return False

	specification = (
		{
			"fieldname": BEHAVIOR_FIELD,
			"label": "Breakeven Cost Behavior",
			"fieldtype": "Select",
			"options": "\n" + "\n".join(BEHAVIORS),
			"insert_after": "account_type",
			"description": (
				"What this account's money does when the crop gets bigger. Fixed — it does not "
				"move. Variable — it moves with volume. Mixed — both, split by the percentage "
				"below. Excluded — leave it out of every breakeven, which is what income tax "
				"needs. Set here, this is true for every analysis afterwards; leave it blank and "
				"each analysis guesses from the account's type and name and says that it guessed."
			),
		},
		{
			"fieldname": BASIS_FIELD,
			"label": "Breakeven Volume Basis",
			"fieldtype": "Select",
			"options": "\n" + "\n".join(VOLUME_BASES),
			"insert_after": BEHAVIOR_FIELD,
			"depends_on": f"eval:['Variable','Mixed'].includes(doc.{BEHAVIOR_FIELD})",
			"description": (
				"Which volume the variable part follows. Harvested — everything off the trees, so "
				"picking, hauling and field bins; this pile does NOT shrink when packout falls. "
				"Sellable — only what packs out, so cartons, packing, freight and commission."
			),
		},
		{
			"fieldname": VARIABLE_PCT_FIELD,
			"label": "Breakeven Variable %",
			"fieldtype": "Percent",
			"insert_after": BASIS_FIELD,
			"depends_on": f"eval:doc.{BEHAVIOR_FIELD}=='Mixed'",
			"description": (
				"For a Mixed account: what share of it behaves variably. The rest is treated as "
				"fixed. A repair account carrying both a service contract and per-hour work is "
				"the case this exists for."
			),
		},
	)

	created = False
	for field in specification:
		try:
			if compat.has_field("Account", field["fieldname"]):
				continue
			if frappe.db.exists(CUSTOM_FIELD, {"dt": "Account", "fieldname": field["fieldname"]}):
				continue
			doc = frappe.new_doc(CUSTOM_FIELD)
			doc.dt = "Account"
			for key, value in field.items():
				doc.set(key, value)
			doc.insert(ignore_permissions=True)
			created = True
		except Exception:
			frappe.log_error(
				title=f"erpnext_mcp: could not add {field['fieldname']} to Account",
				message=compat.traceback_text(),
			)
			return False

	if created:
		try:
			frappe.clear_cache(doctype="Account")
		except Exception:
			pass
	return _behavior_fields_present()


def _behavior_fields_present() -> bool:
	try:
		return all(compat.has_field("Account", name) for name in ACCOUNT_FIELDS)
	except Exception:
		return False


def account_classification_available() -> bool:
	"""Whether a classification can be made to stick, installing the columns if not."""
	return ensure_account_behavior_fields()


# ── classification ──────────────────────────────────────────────────────────
def classify_account(account: dict, override: dict | None = None) -> dict:
	"""Which pile this account goes in, and on whose authority.

	Order is best evidence first and never changes: an explicit override for
	this analysis, then the account's own stored classification, then the
	heuristic. Returning the SOURCE alongside the answer is the whole point —
	the answer alone is a number, and the pair is a number somebody can decide
	whether to trust.
	"""
	name = str(account.get("account_name") or account.get("name") or "")
	lowered = name.lower()
	account_type = str(account.get("account_type") or "")

	if override:
		behavior = str(override.get("cost_behavior") or "").strip().title()
		if behavior not in BEHAVIORS:
			raise ToolError(
				f"cost_behavior for {name!r} must be one of: {', '.join(BEHAVIORS)}. "
				f"Got {override.get('cost_behavior')!r}."
			)
		basis = str(override.get("volume_basis") or "").strip().title() or "Harvested"
		if basis not in VOLUME_BASES:
			raise ToolError(
				f"volume_basis for {name!r} must be one of: {', '.join(VOLUME_BASES)}. "
				f"Got {override.get('volume_basis')!r}."
			)
		return {
			"cost_behavior": behavior,
			"volume_basis": basis,
			"variable_pct": _pct(override.get("variable_pct"), name),
			"classification_source": "Override",
			"basis_note": str(override.get("reason") or "").strip()
			or "classified for this analysis only; nothing was written to the Account.",
		}

	stored = str(account.get(BEHAVIOR_FIELD) or "").strip()
	if stored in BEHAVIORS:
		return {
			"cost_behavior": stored,
			"volume_basis": str(account.get(BASIS_FIELD) or "").strip() or "Harvested",
			"variable_pct": _pct(account.get(VARIABLE_PCT_FIELD), name),
			"classification_source": "Account",
			"basis_note": f"classified on the Account itself as {stored}.",
		}

	return _guess(lowered, account_type, name)


def _guess(lowered: str, account_type: str, name: str) -> dict:
	"""The heuristic. Every answer it gives is labelled as one.

	RULES BEFORE GUESSES. Income tax and round-off are excluded by rule, because
	at breakeven there is no pre-tax income to tax and a rounding account is not
	a cost of growing anything.
	"""

	def out(behavior, basis, pct, note):
		return {
			"cost_behavior": behavior,
			"volume_basis": basis,
			"variable_pct": pct,
			"classification_source": "Heuristic",
			"basis_note": f"GUESSED: {note}. Nobody has classified this account — set "
			f"{BEHAVIOR_FIELD} on it, or pass a cost override, to make it a decision.",
		}

	for word in _EXCLUDED_WORDS:
		if word in lowered:
			return {
				"cost_behavior": "Excluded",
				"volume_basis": None,
				"variable_pct": 0.0,
				"classification_source": "Account",
				"basis_note": (
					"excluded BY RULE, not by guess: at breakeven there is no pre-tax income to "
					"tax, so a model carrying the tax line would demand the farm cover a "
					"liability it does not have."
				),
			}
	if account_type == "Round Off":
		return {
			"cost_behavior": "Excluded",
			"volume_basis": None,
			"variable_pct": 0.0,
			"classification_source": "Account",
			"basis_note": "excluded BY RULE: a rounding account is not a cost of growing anything.",
		}

	if account_type == "Depreciation":
		return out(
			"Fixed", None, 0.0, "ERPNext account type is Depreciation, which does not move with volume"
		)

	for word in _HARVEST_WORDS:
		if word in lowered:
			return out(
				"Variable",
				"Harvested",
				0.0,
				f"the account name contains {word!r}, which is bought for everything that comes "
				"off the trees rather than for what packs out",
			)
	for word in _SELLABLE_WORDS:
		if word in lowered:
			return out(
				"Variable",
				"Sellable",
				0.0,
				f"the account name contains {word!r}, which is bought only for fruit that packs out",
			)
	for word, share in _MIXED_WORDS:
		if word in lowered:
			return out(
				"Mixed",
				"Harvested",
				share,
				f"the account name contains {word!r}, which is normally part standing charge and "
				f"part per-unit; {share:.0f}% is the conventional split and not a measurement",
			)
	for word in _FIXED_WORDS:
		if word in lowered:
			return out(
				"Fixed", None, 0.0, f"the account name contains {word!r}, which does not move with volume"
			)

	if account_type == "Cost of Goods Sold":
		return out(
			"Variable",
			"Sellable",
			0.0,
			"ERPNext account type is Cost of Goods Sold, which by definition follows what was sold",
		)

	return out(
		"Fixed",
		None,
		0.0,
		f"nothing in {name!r} or its ERPNext account type says what this money does, so it was "
		"treated as fixed — the assumption that leaves the contribution margin unflattered",
	)


def _pct(value, label: str) -> float:
	if value in (None, ""):
		return 0.0
	try:
		pct = float(value)
	except (TypeError, ValueError):
		raise ToolError(f"variable_pct for {label!r} must be a number. Got {value!r}.") from None
	if pct < 0 or pct > 100:
		raise ToolError(f"variable_pct for {label!r} must be between 0 and 100. Got {pct}.")
	return pct


# ── reading the costs ───────────────────────────────────────────────────────
def _expense_accounts(company: str) -> list[dict]:
	fields = compat.existing_fields(
		"Account",
		("name", "account_name", "account_number", "account_type", "root_type", *ACCOUNT_FIELDS),
	)
	rows = frappe.db.get_all(
		"Account",
		filters={"company": company, "root_type": "Expense", "is_group": 0},
		fields=fields,
		limit=ACCOUNT_CAP,
	)
	return [dict(row) for row in rows or []]


def _cost_center_tree(cost_center: str, company: str) -> list[str]:
	"""A cost center and every cost center under it.

	DESCENDANTS INCLUDED, because a farm that files 'Southgate Block' under
	'Orchard' and posts to both would otherwise read a breakeven for the block
	that was missing every cost booked at the division above it — an omission
	that makes the crop look cheaper than it is, which is the direction that
	does damage.
	"""
	names = [cost_center]
	try:
		row = frappe.db.get_value("Cost Center", cost_center, ["lft", "rgt"], as_dict=True)
		if row and row.get("lft") is not None and row.get("rgt") is not None:
			descendants = frappe.db.get_all(
				"Cost Center",
				filters={
					"company": company,
					"lft": (">=", row["lft"]),
					"rgt": ("<=", row["rgt"]),
				},
				pluck="name",
				limit=RECORD_CAP,
			)
			if descendants:
				return list(dict.fromkeys(descendants))
	except Exception:
		# A site whose Cost Center is not a nested set — or a double that does
		# not model one — still gets the cost center it asked for, and the
		# warning below is not raised because nothing was silently dropped: the
		# named cost center's own postings are read either way.
		pass
	return names


def _ledger_amounts(
	company: str, from_date: str, to_date: str, accounts: list[dict], cost_center: str
) -> tuple:
	"""Net expense per account over the window, from GL Entry. Debits less credits.

	DEBITS LESS CREDITS, because Expense is a debit-balance root: a refund, a
	credit note or a reversal is a credit on the same account and correctly
	reduces the cost. A figure computed the other way round would come out
	negative on every well-kept set of books.

	SUBMITTED VOUCHERS ONLY, which is not a filter but a property: GL Entry does
	not exist for drafts. A draft purchase invoice cannot move a breakeven.
	"""
	warnings: list[str] = []
	amounts: dict = {}
	if not compat.doctype_exists("GL Entry"):
		warnings.append(
			"this site has no GL Entry doctype, so no cost could be read. Every total below is "
			"the absence of a ledger rather than a season that cost nothing."
		)
		return amounts, warnings
	if not accounts:
		return amounts, warnings

	filters = {
		"company": company,
		"account": ("in", [row["name"] for row in accounts]),
		"posting_date": ("between", [from_date, to_date]),
		"is_cancelled": 0,
	}
	if cost_center:
		filters["cost_center"] = ("in", _cost_center_tree(cost_center, company))

	rows = frappe.db.get_all(
		"GL Entry",
		filters=filters,
		fields=compat.existing_fields("GL Entry", ("name", "account", "debit", "credit")),
		limit=GL_ROW_CAP,
	)
	for row in rows or []:
		account = row.get("account")
		amounts[account] = (
			amounts.get(account, 0.0) + float(row.get("debit") or 0) - float(row.get("credit") or 0)
		)
	if len(rows or []) >= GL_ROW_CAP:
		warnings.append(
			f"the window returned {GL_ROW_CAP} ledger rows, which is this run's cap — the totals "
			"are therefore INCOMPLETE and every figure below understates the cost. Narrow the "
			"window or the cost center."
		)
	return amounts, warnings


def _budget_amounts(company: str, fiscal_year: str) -> tuple:
	"""Budgeted expense per account for the year, from this app's Budget register."""
	warnings: list[str] = []
	amounts: dict = {}
	if not compat.doctype_exists("Budget"):
		warnings.append(
			"this site has no Budget doctype, so a budget-based breakeven has nothing to read. "
			"Recompute with cost_source 'Ledger Actuals', or create a budget first."
		)
		return amounts, warnings

	budgets = frappe.db.get_all(
		"Budget",
		filters={"company": company, "fiscal_year": fiscal_year},
		pluck="name",
		limit=RECORD_CAP,
	)
	if not budgets:
		warnings.append(
			f"no Budget exists for {company} in {fiscal_year}, so every cost below is zero. That "
			"is a missing budget rather than a costless season — list_budgets shows what the "
			"company does have."
		)
		return amounts, warnings
	if len(budgets) > 1:
		warnings.append(
			f"{len(budgets)} budgets exist for {fiscal_year} and ALL of them were added together. "
			f"If they are alternatives rather than parts, the cost here is double-counted: {', '.join(sorted(budgets))}."
		)

	for name in budgets:
		doc = frappe.get_doc("Budget", name)
		for row in doc.get("line_items") or []:
			account = _get(row, "account")
			if not account:
				continue
			amounts[account] = amounts.get(account, 0.0) + float(_get(row, "budgeted_amount") or 0)
	return amounts, warnings


# ── the arithmetic ──────────────────────────────────────────────────────────
def compute_model(
	*,
	fixed: float,
	rate_harvest: float,
	rate_sellable: float,
	harvest_units: float,
	packout_pct: float,
	price: float,
	cull_credit: float,
) -> dict:
	"""The whole model, as pure arithmetic on seven numbers.

	SEPARATED FROM EVERY DOCUMENT AND EVERY QUERY ON PURPOSE. The sensitivity
	tool runs this function dozens of times against modified inputs, and a
	version of it that read a document would either be re-reading one per
	scenario or quietly sharing mutable state between them. Pure in, pure out
	also means the interesting cases — a negative contribution margin, a
	hundred-percent packout, a zero price — are testable without a site.

	IT TAKES RATES, NOT TOTALS, and that signature is load-bearing. `rate_harvest`
	is per HARVESTED unit and `rate_sellable` per SELLABLE unit; the totals are
	derived from them at whatever volumes are passed. A version taking totals
	would have to re-derive both rates against the volume it was called with,
	which silently cancels the harvest/sellable split out of the arithmetic — see
	the module docstring. Callers turn ledger totals into rates ONCE, against the
	analysis's stored baseline.
	"""
	harvest_units = float(harvest_units or 0)
	packout = float(packout_pct or 0) / 100.0
	price = float(price or 0)
	cull_credit = float(cull_credit or 0)
	fixed = float(fixed or 0)
	rate_harvest = float(rate_harvest or 0)
	rate_sellable = float(rate_sellable or 0)

	if harvest_units <= 0 or packout <= 0:
		# Guarded rather than divided: every per-unit figure below is a division
		# by one of these, and an infinity here renders downstream as a large
		# confident number.
		return {
			"sellable_units": 0.0,
			"variable_cost_per_sellable_unit": 0.0,
			"cull_credit_per_sellable_unit": 0.0,
			"contribution_margin_per_unit": 0.0,
			"contribution_margin_ratio": 0.0,
			"breakeven_units": None,
			"breakeven_harvest_units": None,
			"breakeven_revenue": None,
			"breakeven_price": None,
			"breakeven_packout_pct": None,
			"projected_profit": -fixed,
			"margin_of_safety_pct": None,
			"total_fixed_cost": round(fixed, 2),
			"total_variable_cost": 0.0,
			"total_variable_harvest_cost": 0.0,
			"total_variable_sellable_cost": 0.0,
			"cull_credit_total": 0.0,
			"impossible": (
				"no crop and no packout means no per-unit figure exists. The loss is the fixed "
				"cost, which is a different question from the one a breakeven answers."
			),
		}

	sellable_units = harvest_units * packout

	# Each packed unit carries 1/p harvested units of the harvest-basis pile with
	# it, which is the whole asymmetry: at 60% packout every box has paid to pick
	# 1.67 boxes' worth of fruit.
	variable_per_unit = rate_harvest / packout + rate_sellable
	cull_per_unit = cull_credit * (1.0 - packout) / packout
	contribution = price + cull_per_unit - variable_per_unit

	variable_harvest = rate_harvest * harvest_units
	variable_sellable = rate_sellable * sellable_units
	variable_total = variable_harvest + variable_sellable
	cull_total = cull_credit * (harvest_units - sellable_units)
	profit = price * sellable_units + cull_total - fixed - variable_total

	out = {
		"sellable_units": round(sellable_units, 4),
		"variable_cost_per_sellable_unit": round(variable_per_unit, 4),
		"cull_credit_per_sellable_unit": round(cull_per_unit, 4),
		"contribution_margin_per_unit": round(contribution, 4),
		"contribution_margin_ratio": round(contribution / price * 100.0, 4) if price else None,
		"total_fixed_cost": round(fixed, 2),
		"total_variable_cost": round(variable_total, 2),
		"total_variable_harvest_cost": round(variable_harvest, 2),
		"total_variable_sellable_cost": round(variable_sellable, 2),
		"cull_credit_total": round(cull_total, 2),
		"projected_profit": round(profit, 2),
		"impossible": "",
	}

	if contribution > 0:
		breakeven_units = fixed / contribution
		out["breakeven_units"] = round(breakeven_units, 4)
		out["breakeven_harvest_units"] = round(breakeven_units / packout, 4)
		out["breakeven_revenue"] = round(
			breakeven_units * price + cull_credit * (breakeven_units / packout - breakeven_units), 2
		)
		out["margin_of_safety_pct"] = (
			round((sellable_units - breakeven_units) / sellable_units * 100.0, 4) if sellable_units else None
		)
	else:
		# There is no such quantity. The arithmetic limit is an enormous number
		# and would read as a hard target rather than as an impossibility.
		out["breakeven_units"] = None
		out["breakeven_harvest_units"] = None
		out["breakeven_revenue"] = None
		out["margin_of_safety_pct"] = None
		out["impossible"] = (
			f"the contribution margin is {contribution:.4f} per unit, so every additional unit "
			"loses money and NO VOLUME EVER BREAKS EVEN. The breakeven PRICE below is the number "
			"that matters here: it says how far the price has to come up before volume helps."
		)

	# The breakeven price exists whatever the contribution margin does, which is
	# exactly why it is computed separately rather than derived from the units.
	out["breakeven_price"] = round((fixed + variable_total - cull_total) / sellable_units, 4)

	# The packout that would break even at this price and this harvest. The
	# harvest-basis pile does not move with it; the sellable-basis pile does.
	denominator = harvest_units * (price - cull_credit - rate_sellable)
	if denominator > 0:
		required = (fixed + float(variable_harvest or 0) - cull_credit * harvest_units) / denominator
		out["breakeven_packout_pct"] = round(required * 100.0, 4) if 0 < required <= 1 else None
		if required <= 0:
			# Every packout covers costs, including none of it — the culls alone
			# pay the bill. Rare, real, and worth saying rather than blanking.
			out["breakeven_packout_pct"] = 0.0
	else:
		out["breakeven_packout_pct"] = None

	return out


# ── describing ──────────────────────────────────────────────────────────────
def _describe_line(row) -> dict:
	return {
		"account": _get(row, "account"),
		"account_name": _get(row, "account_name"),
		"amount": round(float(_get(row, "amount") or 0), 2),
		"cost_behavior": _get(row, "cost_behavior"),
		"volume_basis": _get(row, "volume_basis"),
		"variable_pct": float(_get(row, "variable_pct") or 0),
		"fixed_amount": round(float(_get(row, "fixed_amount") or 0), 2),
		"variable_amount": round(float(_get(row, "variable_amount") or 0), 2),
		"classification_source": _get(row, "classification_source"),
		"basis_note": _get(row, "basis_note"),
	}


def _describe_scenario(row) -> dict:
	return {
		"variable": _get(row, "variable"),
		"change_pct": float(_get(row, "change_pct") or 0),
		"scenario_value": float(_get(row, "scenario_value") or 0),
		"contribution_margin_per_unit": _number(_get(row, "contribution_margin_per_unit")),
		"breakeven_units": _number(_get(row, "breakeven_units")),
		"breakeven_price": _number(_get(row, "breakeven_price")),
		"projected_profit": _number(_get(row, "projected_profit")),
		"verdict": _get(row, "verdict"),
	}


def _number(value):
	if value in (None, ""):
		return None
	try:
		return round(float(value), 4)
	except (TypeError, ValueError):
		return None


def _describe(doc, *, with_lines: bool = True) -> dict:
	lines = [_describe_line(row) for row in doc.get("cost_lines") or []]
	guessed = [line for line in lines if line["classification_source"] == "Heuristic"]
	out = {
		"name": doc.name,
		"analysis_name": doc.analysis_name,
		"company": doc.company,
		"fiscal_year": doc.fiscal_year,
		"crop_type": doc.crop_type,
		"unit_label": doc.unit_label or "Unit",
		"currency": doc.currency or None,
		"status": doc.status or "Draft",
		"cost_center": doc.cost_center or None,
		"cost_source": doc.cost_source or "Ledger Actuals",
		"from_date": str(doc.from_date or "") or None,
		"to_date": str(doc.to_date or "") or None,
		"expected_harvest_units": float(doc.expected_harvest_units or 0),
		"packout_pct": float(doc.packout_pct or 0),
		"expected_price": float(doc.expected_price or 0),
		"cull_credit_per_unit": float(doc.cull_credit_per_unit or 0),
		"sellable_units": float(doc.sellable_units or 0),
		"total_fixed_cost": float(doc.total_fixed_cost or 0),
		"total_variable_cost": float(doc.total_variable_cost or 0),
		"total_variable_harvest_cost": float(doc.total_variable_harvest_cost or 0),
		"total_variable_sellable_cost": float(doc.total_variable_sellable_cost or 0),
		"rate_harvest": _number(doc.rate_harvest),
		"rate_sellable": _number(doc.rate_sellable),
		"baseline_harvest_units": float(doc.baseline_harvest_units or 0),
		"baseline_packout_pct": float(doc.baseline_packout_pct or 0),
		"total_excluded_cost": float(doc.total_excluded_cost or 0),
		"cull_credit_total": float(doc.cull_credit_total or 0),
		"variable_cost_per_sellable_unit": float(doc.variable_cost_per_sellable_unit or 0),
		"contribution_margin_per_unit": float(doc.contribution_margin_per_unit or 0),
		"contribution_margin_ratio": _number(doc.contribution_margin_ratio),
		"breakeven_units": _number(doc.breakeven_units),
		"breakeven_harvest_units": _number(doc.breakeven_harvest_units),
		"breakeven_revenue": _number(doc.breakeven_revenue),
		"breakeven_price": _number(doc.breakeven_price),
		"breakeven_packout_pct": _number(doc.breakeven_packout_pct),
		"projected_profit": _number(doc.projected_profit),
		"margin_of_safety_pct": _number(doc.margin_of_safety_pct),
		"computed_on": str(doc.computed_on or "") or None,
		"computed_by": doc.computed_by or None,
		"computation_warnings": doc.computation_warnings or "",
		"notes": doc.notes or None,
		"cost_line_count": len(lines),
		"guessed_classification_count": len(guessed),
		"market_overlay": {
			"usda_commodity": doc.usda_commodity or None,
			"usda_variety": doc.usda_variety or None,
			"usda_market": doc.usda_market or None,
			"quote": doc.usda_quote or None,
			"market_price": _number(doc.usda_price),
			"market_package": doc.usda_package or None,
			"quote_date": str(doc.usda_report_date or "") or None,
			"spread_vs_breakeven": _number(doc.usda_spread),
			"verdict": doc.usda_verdict or "",
		},
	}
	if with_lines:
		out["cost_lines"] = lines
		out["scenarios"] = [_describe_scenario(row) for row in doc.get("scenarios") or []]
		out["guessed_accounts"] = [line["account"] for line in guessed]
	if guessed:
		out["classification_note"] = (
			f"{len(guessed)} of {len(lines)} cost line(s) were CLASSIFIED BY GUESS from the "
			"account's name and ERPNext type. The heuristic is usually right and is never a "
			f"decision — set {BEHAVIOR_FIELD} on those accounts, or pass cost_overrides, before "
			"quoting this breakeven to anybody who will hold you to it."
		)
	return out


def _doc(reference: str, company: str = ""):
	reference = (reference or "").strip()
	if not reference:
		raise ToolError("name is required — the analysis's docname, or its analysis_name.")
	if frappe.db.exists(ANALYSIS, reference):
		return frappe.get_doc(ANALYSIS, reference)
	filters = {"analysis_name": reference}
	if company:
		filters["company"] = company
	matches = frappe.db.get_all(ANALYSIS, filters=filters, pluck="name", limit=10)
	if len(matches) == 1:
		return frappe.get_doc(ANALYSIS, matches[0])
	if len(matches) > 1:
		raise ToolError(
			f"{reference!r} matches {len(matches)} analyses: {', '.join(sorted(matches))}. An "
			"analysis name is usually reused across seasons — pass the docname, or set company "
			"to narrow it."
		)
	raise ToolError(
		f"no Breakeven Analysis matching {reference!r} on this site. list_breakeven_analyses has "
		"the register."
	)


# ── 1. create ───────────────────────────────────────────────────────────────
def create_breakeven_analysis(args: dict) -> ToolResult:
	"""Register a crop, a volume and a price to model. MUTATING.

	CREATES BUT DOES NOT COMPUTE, deliberately. `compute_breakeven` has its own
	switch, and a create that ran the computation would let an operator who
	enabled only one of the two get both — which is the whole thing this app's
	per-tool switches exist to prevent.
	"""
	_require()
	company = resolve_company(as_str(args, "company"), required=True)
	fiscal_year = as_str(args, "fiscal_year", required=True)
	crop_type = as_str(args, "crop_type", required=True)

	unit_label = as_str(args, "unit_label") or "Box"
	if unit_label not in UNIT_LABELS:
		raise ToolError(f"unit_label must be one of: {', '.join(UNIT_LABELS)}. Got {unit_label!r}.")
	cost_source = as_str(args, "cost_source") or "Ledger Actuals"
	if cost_source not in COST_SOURCES:
		raise ToolError(f"cost_source must be one of: {', '.join(COST_SOURCES)}. Got {cost_source!r}.")

	harvest = args.get("expected_harvest_units")
	if harvest is None:
		raise ToolError(
			"expected_harvest_units is required — what the block is expected to make BEFORE "
			f"packout, in {unit_label.lower()}-equivalents."
		)

	doc = frappe.new_doc(ANALYSIS)
	doc.analysis_name = as_str(args, "analysis_name") or f"{crop_type} {fiscal_year}"
	doc.company = company
	doc.fiscal_year = fiscal_year
	doc.crop_type = crop_type
	doc.unit_label = unit_label
	doc.cost_source = cost_source
	doc.expected_harvest_units = float(harvest)
	packout = args.get("packout_pct")
	doc.packout_pct = float(packout) if packout is not None else 100.0
	doc.expected_price = float(args.get("expected_price") or 0)
	doc.cull_credit_per_unit = float(args.get("cull_credit_per_unit") or 0)
	doc.from_date = as_date(args, "from_date")
	doc.to_date = as_date(args, "to_date")
	cost_center = as_str(args, "cost_center")
	if cost_center:
		doc.cost_center = resolve_cost_center(cost_center, company)
	for key in ("currency", "usda_commodity", "usda_variety", "usda_market", "notes"):
		value = as_str(args, key)
		if value:
			doc.set(key, value)
	doc.status = "Draft"

	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	data = {"analysis": _describe(doc)}
	data["next_step"] = (
		f"nothing has been computed yet. Run compute_breakeven('{doc.name}') to read this "
		"company's expense accounts over the window, classify them, and produce the breakeven "
		"price. Creation does not compute on purpose: the two tools have separate switches, and "
		"a create that computed would hand an operator a tool they had not enabled."
	)
	if not doc.expected_price:
		data["price_note"] = (
			"no expected_price was given. The breakeven PRICE will still be computed — it does "
			"not depend on one — but the breakeven quantity, the contribution margin and the "
			"margin of safety all do, and will read as though the fruit were free."
		)
	return ToolResult(
		data=data,
		summary=(
			f"created Breakeven Analysis {doc.name}: {crop_type}, {doc.expected_harvest_units:g} "
			f"{unit_label.lower()}(s) at {doc.packout_pct:g}% packout"
		),
		docstatus_delta="none → 0 (draft)",
	)


# ── 2. compute ──────────────────────────────────────────────────────────────
def compute_breakeven(args: dict) -> ToolResult:
	"""Read the chart of accounts, classify it, and answer the question. MUTATING.

	MUTATING BECAUSE IT STORES, not because it posts. Nothing here touches the
	ledger; what it writes is the cost lines, the results and the standard
	scenario band onto the analysis itself. That storage is the point — see the
	doctype's own description for why a breakeven whose intermediates were
	thrown away can never be compared with next season's.

	THE PACKOUT SLIDER IS THIS TOOL'S `packout_pct` ARGUMENT. Pass it and the
	analysis is recomputed at that packout and keeps it; leave it out and the
	stored figure is used. Same for the price and the harvest, so "what if we
	only get 62% out of this block" is one call with one number in it.
	"""
	_require()
	doc = _doc(as_str(args, "name", required=True), resolve_company(as_str(args, "company")) or "")

	# The slider, and its two companions. Applied before anything is read, so the
	# whole computation runs against the numbers the caller asked about.
	for key in ("packout_pct", "expected_price", "expected_harvest_units", "cull_credit_per_unit"):
		if args.get(key) is not None:
			doc.set(key, float(args[key]))
	cost_source = as_str(args, "cost_source")
	if cost_source:
		if cost_source not in COST_SOURCES:
			raise ToolError(f"cost_source must be one of: {', '.join(COST_SOURCES)}. Got {cost_source!r}.")
		doc.cost_source = cost_source
	for key in ("usda_commodity", "usda_variety", "usda_market"):
		if as_str(args, key):
			doc.set(key, as_str(args, key))

	overrides = _overrides(args.get("cost_overrides"), doc.company)
	warnings: list[str] = []

	fields_available = account_classification_available()
	if not fields_available:
		warnings.append(
			"this site would not take the three breakeven columns on Account, so a classification "
			"cannot be made to stick between analyses. Everything below still computed; overrides "
			"still work, per analysis."
		)

	accounts = _expense_accounts(doc.company)
	if not accounts:
		warnings.append(
			f"{doc.company} has no ledger account with root_type Expense, so there is nowhere for "
			"a cost to have been booked. Every total below is a chart-of-accounts gap rather than "
			"a season that cost nothing — get_chart_of_accounts shows what the company does have."
		)

	if doc.cost_source == "Budget":
		amounts, source_warnings = _budget_amounts(doc.company, doc.fiscal_year)
		source_text = f"Budget rows for {doc.fiscal_year}"
	else:
		if not doc.from_date or not doc.to_date:
			raise ToolError(
				f"{doc.name} has no cost window and its fiscal year {doc.fiscal_year!r} did not "
				"supply one. Set from_date and to_date, or point the analysis at a fiscal year "
				"that exists — reading the whole ledger instead would silently mix seasons."
			)
		amounts, source_warnings = _ledger_amounts(
			doc.company, str(doc.from_date), str(doc.to_date), accounts, doc.cost_center or ""
		)
		source_text = f"GL Entry, {doc.from_date} to {doc.to_date}"
	warnings.extend(source_warnings)

	unmatched = [account for account in overrides if account not in {row["name"] for row in accounts}]
	if unmatched:
		raise ToolError(
			f"cost_overrides names {len(unmatched)} account(s) that are not expense accounts of "
			f"{doc.company}: {', '.join(sorted(unmatched))}. An override that matched nothing "
			"would be silently ignored and the account would go on being guessed at."
		)

	# ── build the lines ──────────────────────────────────────────────────────
	doc.set("cost_lines", [])
	fixed_total = 0.0
	harvest_total = 0.0
	sellable_total = 0.0
	excluded_total = 0.0
	guessed = 0

	for account in sorted(accounts, key=lambda row: str(row.get("account_name") or row["name"])):
		amount = round(float(amounts.get(account["name"], 0.0)), 6)
		if not amount:
			# An account with no money in the window is not a cost line. Keeping
			# it would put a hundred zero rows in front of the eight that matter,
			# which is how a usable form becomes an unreadable one.
			continue
		verdict = classify_account(account, overrides.get(account["name"]))
		behavior = verdict["cost_behavior"]
		basis = verdict["volume_basis"]
		share = float(verdict["variable_pct"] or 0) / 100.0

		if behavior == "Excluded":
			fixed_part, variable_part = 0.0, 0.0
			excluded_total += amount
		elif behavior == "Fixed":
			fixed_part, variable_part = amount, 0.0
		elif behavior == "Variable":
			fixed_part, variable_part = 0.0, amount
		else:
			variable_part = round(amount * share, 6)
			fixed_part = round(amount - variable_part, 6)

		fixed_total += fixed_part
		if variable_part:
			if (basis or "Harvested") == "Sellable":
				sellable_total += variable_part
			else:
				harvest_total += variable_part
		if verdict["classification_source"] == "Heuristic":
			guessed += 1

		doc.append(
			"cost_lines",
			{
				"account": account["name"],
				"account_name": account.get("account_name") or account["name"],
				"amount": amount,
				"cost_behavior": behavior,
				"volume_basis": basis if behavior in ("Variable", "Mixed") else None,
				"variable_pct": verdict["variable_pct"],
				"fixed_amount": fixed_part,
				"variable_amount": variable_part,
				"classification_source": verdict["classification_source"],
				"basis_note": verdict["basis_note"],
			},
		)

	if not doc.get("cost_lines"):
		warnings.append(
			f"no expense account carried a balance from {source_text}, so the fixed cost is zero "
			"and the breakeven price is whatever the culls do not cover. A breakeven of zero is "
			"almost always an empty window rather than a free season — check the dates and the "
			"cost center."
		)

	# ── turn the totals into rates, ONCE, against the baseline ───────────────
	# See the module docstring: deriving the rates at whatever the slider
	# currently says cancels the harvest/sellable split out of the arithmetic
	# entirely, and every figure downstream would still look plausible.
	rate_harvest, rate_sellable, baseline_note = _rates(doc, harvest_total, sellable_total, args)
	if baseline_note:
		warnings.append(baseline_note)

	# ── run the model ────────────────────────────────────────────────────────
	model = compute_model(
		fixed=fixed_total,
		rate_harvest=rate_harvest,
		rate_sellable=rate_sellable,
		harvest_units=float(doc.expected_harvest_units or 0),
		packout_pct=float(doc.packout_pct or 0),
		price=float(doc.expected_price or 0),
		cull_credit=float(doc.cull_credit_per_unit or 0),
	)

	doc.total_fixed_cost = model["total_fixed_cost"]
	doc.total_variable_cost = model["total_variable_cost"]
	doc.total_variable_harvest_cost = model["total_variable_harvest_cost"]
	doc.total_variable_sellable_cost = model["total_variable_sellable_cost"]
	doc.rate_harvest = round(rate_harvest, 6)
	doc.rate_sellable = round(rate_sellable, 6)
	doc.total_excluded_cost = round(excluded_total, 2)
	doc.cull_credit_total = model["cull_credit_total"]
	doc.sellable_units = model["sellable_units"]
	doc.variable_cost_per_sellable_unit = model["variable_cost_per_sellable_unit"]
	doc.contribution_margin_per_unit = model["contribution_margin_per_unit"]
	doc.contribution_margin_ratio = model["contribution_margin_ratio"]
	doc.breakeven_units = model["breakeven_units"]
	doc.breakeven_harvest_units = model["breakeven_harvest_units"]
	doc.breakeven_revenue = model["breakeven_revenue"]
	doc.breakeven_price = model["breakeven_price"]
	doc.breakeven_packout_pct = model["breakeven_packout_pct"]
	doc.projected_profit = model["projected_profit"]
	doc.margin_of_safety_pct = model["margin_of_safety_pct"]

	if model["impossible"]:
		warnings.append(model["impossible"])
	if guessed:
		warnings.append(
			f"{guessed} of {len(doc.get('cost_lines'))} cost line(s) were classified BY GUESS from "
			f"the account name and ERPNext type. Set {BEHAVIOR_FIELD} on those accounts to turn a "
			"guess into a decision that holds for every analysis afterwards."
		)

	# ── the standard scenario band ───────────────────────────────────────────
	doc.set("scenarios", [])
	stamp = frappe.utils.now()
	for variable in SENSITIVITY_VARIABLES:
		for change in STANDARD_BAND:
			row = _scenario(
				variable,
				change,
				fixed=fixed_total,
				rate_harvest=rate_harvest,
				rate_sellable=rate_sellable,
				doc=doc,
			)
			row["computed_on"] = stamp
			doc.append("scenarios", row)

	# ── the market overlay ───────────────────────────────────────────────────
	overlay_warnings = _apply_overlay(doc, args)
	warnings.extend(overlay_warnings)

	doc.status = "Computed"
	doc.computed_on = stamp
	doc.computed_by = frappe.session.user if getattr(frappe, "session", None) else "erpnext_mcp"
	doc.computation_warnings = "\n".join(warnings)
	doc.flags.breakeven_computed = True
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	described = _describe(doc)
	described["cost_basis"] = source_text
	described["account_classification_available"] = fields_available
	described["computation_warnings_list"] = warnings

	unit = (doc.unit_label or "unit").lower()
	if model["breakeven_units"] is not None:
		headline = (
			f"needs {model['breakeven_units']:,.0f} {unit}(s) sold at {doc.expected_price:g}, or "
			f"{model['breakeven_price']:,.2f} per {unit} at the expected crop"
		)
	else:
		headline = (
			f"no volume breaks even at {doc.expected_price:g} per {unit} — the price would have to "
			f"reach {model['breakeven_price']:,.2f}"
		)

	return ToolResult(
		data=described,
		summary=f"computed {doc.name} at {doc.packout_pct:g}% packout: {headline}",
		docstatus_delta="0 → 0 (results stored)",
	)


def _overrides(raw, company: str) -> dict:
	"""`cost_overrides` as {account: spec}, refusing a malformed list outright.

	REFUSED RATHER THAN SKIPPED, because an override that was quietly dropped
	leaves the account being GUESSED at while the caller believes they have
	classified it — and the result would look identical to one where the
	override had taken.
	"""
	if raw in (None, "", []):
		return {}
	if not isinstance(raw, list):
		raise ToolError(
			'cost_overrides must be a list of objects: [{"account": "5110 - Picking - OML", '
			'"cost_behavior": "Variable", "volume_basis": "Harvested"}]. '
			f"Got {type(raw).__name__}."
		)
	out: dict = {}
	for index, entry in enumerate(raw):
		if not isinstance(entry, dict):
			raise ToolError(f"cost_overrides[{index}] is {type(entry).__name__}, not an object.")
		account = str(entry.get("account") or "").strip()
		if not account:
			raise ToolError(f"cost_overrides[{index}] names no account.")
		if account in out:
			raise ToolError(
				f"cost_overrides names {account!r} twice. Two classifications for one account "
				"would make the result depend on which was read last."
			)
		out[account] = entry
	return out


def _rates(doc, harvest_total: float, sellable_total: float, args: dict) -> tuple:
	"""The two variable rates, and the baseline they were derived at.

	SET ON THE FIRST COMPUTATION AND NOT MOVED BY THE SLIDER. That is the whole
	mechanism: rates fixed at a stated volume are what make a packout of 62%
	mean "the same money per carton over fewer cartons" rather than "the same
	money over fewer cartons", and what makes `compute_breakeven(packout_pct=62)`
	agree with the -22.5% packout row of `get_breakeven_sensitivity`.

	`rebase_costs` moves it, once the season's real crop and real packout are
	known. An argument rather than an automatic behaviour, because re-basing
	changes every comparison this record is part of.
	"""
	note = ""
	baseline_harvest = float(doc.baseline_harvest_units or 0)
	baseline_packout = float(doc.baseline_packout_pct or 0)
	rebase = as_bool(args, "rebase_costs", default=False)

	if rebase or baseline_harvest <= 0 or baseline_packout <= 0:
		baseline_harvest = float(doc.expected_harvest_units or 0)
		baseline_packout = float(doc.packout_pct or 0)
		if rebase and doc.baseline_harvest_units:
			note = (
				f"REBASED: the cost rates are now derived at {baseline_harvest:g} units and "
				f"{baseline_packout:g}% packout, replacing {float(doc.baseline_harvest_units):g} "
				f"and {float(doc.baseline_packout_pct or 0):g}%. Every per-unit figure on this "
				"record has moved as a result, so a comparison against an earlier run of it is "
				"comparing two different models."
			)
		doc.baseline_harvest_units = baseline_harvest
		doc.baseline_packout_pct = baseline_packout

	baseline_sellable = baseline_harvest * baseline_packout / 100.0
	rate_harvest = harvest_total / baseline_harvest if baseline_harvest > 0 else 0.0
	rate_sellable = sellable_total / baseline_sellable if baseline_sellable > 0 else 0.0
	return rate_harvest, rate_sellable, note


def _scenario(
	variable: str, change_pct: float, *, fixed: float, rate_harvest: float, rate_sellable: float, doc
) -> dict:
	"""One what-if, computed against the analysis's own figures.

	ONE VARIABLE AT A TIME. A row that moved two would not say which of them the
	breakeven was sensitive to, which is the only thing anybody reads a
	sensitivity table for.

	IT MOVES RATES AND VOLUMES, NEVER TOTALS. A bigger crop costs more to pick
	AND more to pack, because both rates apply to more units; a lighter packout
	leaves the picking bill alone and shrinks the packing bill with the boxes.
	Both fall straight out of holding the rates and moving the volumes, which is
	exactly what the stored computation does — so a scenario row and the
	analysis's own figures are the same model read at two points.
	"""
	factor = 1.0 + float(change_pct) / 100.0
	price = float(doc.expected_price or 0)
	harvest_units = float(doc.expected_harvest_units or 0)
	packout = float(doc.packout_pct or 0)
	cull = float(doc.cull_credit_per_unit or 0)
	fixed_cost = fixed
	harvest_rate, sellable_rate = rate_harvest, rate_sellable

	if variable == "price":
		price = round(price * factor, 6)
		value = price
		label = "Price"
	elif variable == "yield":
		harvest_units = round(harvest_units * factor, 6)
		value = harvest_units
		label = "Yield"
	elif variable == "packout":
		# Clamped at 100: more fruit cannot pack out than came off the trees, and
		# a +20% row on an analysis already at 90% would otherwise model 108%.
		packout = min(100.0, max(0.0001, round(packout * factor, 6)))
		value = packout
		label = "Packout"
	elif variable == "fixed_cost":
		fixed_cost = fixed * factor
		value = round(fixed_cost, 4)
		label = "Fixed Cost"
	else:
		harvest_rate = rate_harvest * factor
		sellable_rate = rate_sellable * factor
		value = round(harvest_rate * harvest_units + sellable_rate * harvest_units * packout / 100.0, 4)
		label = "Variable Cost"

	model = compute_model(
		fixed=fixed_cost,
		rate_harvest=harvest_rate,
		rate_sellable=sellable_rate,
		harvest_units=harvest_units,
		packout_pct=packout,
		price=price,
		cull_credit=cull,
	)
	return {
		"variable": label,
		"change_pct": float(change_pct),
		"scenario_value": value,
		"contribution_margin_per_unit": model["contribution_margin_per_unit"],
		"breakeven_units": model["breakeven_units"],
		"breakeven_price": model["breakeven_price"],
		"projected_profit": model["projected_profit"],
		"verdict": _verdict(model, price),
	}


def _verdict(model: dict, price: float) -> str:
	if model["contribution_margin_per_unit"] <= 0:
		return "loses money at any volume"
	profit = model["projected_profit"]
	safety = model["margin_of_safety_pct"]
	if profit is None:
		return "no result"
	if profit >= 0:
		room = f" with {safety:.0f}% room" if safety is not None else ""
		return f"covers costs{room}"
	needed = model["breakeven_price"]
	short = f", needs {needed:,.2f} against {price:,.2f}" if price else ""
	return f"loses {abs(profit):,.0f}{short}"


def _apply_overlay(doc, args: dict) -> list:
	"""Put a market price next to the breakeven price, or say why there is none.

	THE OVERLAY IS NEVER LOAD-BEARING. Every figure on the analysis is complete
	without it; the overlay answers a second question — 'is the market currently
	paying that?' — and a failure to answer it must never take the first answer
	down with it. So this returns warnings and writes a verdict; it does not
	raise.
	"""
	warnings: list[str] = []

	manual_price = args.get("market_price")
	if manual_price is not None:
		# A grower with a broker's bid in hand has a better number than any
		# district average. It is stored as a quotation of its own kind so the
		# overlay reads one register, and labelled so nobody mistakes the two.
		try:
			usda_prices.record_manual_quote(
				doc.usda_commodity or doc.crop_type,
				float(manual_price),
				str(as_date(args, "market_price_date") or frappe.utils.nowdate()),
				variety=doc.usda_variety or "",
				market=doc.usda_market or "",
				package=as_str(args, "market_package") or (doc.unit_label or ""),
				source=as_str(args, "market_price_source") or "Manual",
				notes=f"recorded while computing {doc.name}",
			)
			if not doc.usda_commodity:
				doc.usda_commodity = doc.crop_type
		except Exception as exc:
			warnings.append(
				f"the market price given was not stored: {type(exc).__name__}: {exc}. The overlay "
				"below reads whatever quotations were already on the site."
			)

	if as_bool(args, "refresh_usda_prices", default=False):
		slug = as_str(args, "usda_report_slug")
		if not slug:
			warnings.append(
				"refresh_usda_prices was set but no usda_report_slug was given, so nothing was "
				"fetched. AMS identifies its reports by slug and this app ships none, because a "
				"slug invented here would be right for one district and a nightly 404 everywhere else."
			)
		else:
			report = usda_prices.refresh_quotes(slug, doc.usda_commodity or "")
			warnings.extend(f"USDA report {slug}: {line}" for line in report["warnings"])
			if report["stored"] or report["updated"]:
				warnings.append(
					f"USDA report {slug}: stored {len(report['stored'])} and updated "
					f"{len(report['updated'])} quotation(s)."
				)

	if not doc.usda_commodity:
		doc.usda_quote = None
		doc.usda_price = None
		doc.usda_package = None
		doc.usda_report_date = None
		doc.usda_spread = None
		doc.usda_verdict = (
			"No market overlay: usda_commodity is not set on this analysis, so no quotation was "
			"looked for. The breakeven above does not depend on it."
		)
		return warnings

	quote, reason = usda_prices.latest_quote(
		doc.usda_commodity, doc.usda_variety or "", doc.usda_market or ""
	)
	if not quote:
		doc.usda_quote = None
		doc.usda_price = None
		doc.usda_package = None
		doc.usda_report_date = None
		doc.usda_spread = None
		doc.usda_verdict = f"No market overlay: {reason}"
		warnings.append(reason)
		return warnings

	price = usda_prices.reference_price(quote)
	breakeven_price = float(doc.breakeven_price or 0)
	spread = round(price - breakeven_price, 4)
	unit = (doc.unit_label or "unit").lower()

	doc.usda_quote = quote["name"]
	doc.usda_price = price
	doc.usda_package = quote.get("package") or None
	doc.usda_report_date = quote.get("report_date")
	doc.usda_spread = spread

	package = quote.get("package") or "an unstated package"
	direction = "ABOVE" if spread >= 0 else "BELOW"
	doc.usda_verdict = (
		f"{quote.get('source')} quote of {price:,.2f} per {package} on {quote.get('report_date')} "
		f"({quote.get('match_precision')}) sits {abs(spread):,.2f} {direction} this crop's "
		f"breakeven price of {breakeven_price:,.2f} per {unit}. "
		"THE TWO PACKAGES ARE NOT CONVERTED: if they are not the same thing, the spread is not "
		"either. Pack style is a judgement this app has no basis to make."
	)
	if quote.get("match_precision") != "exact":
		warnings.append(
			f"the market overlay had to widen its search to '{quote.get('match_precision')}' to "
			f"find a quotation for {doc.usda_commodity}. The price shown is for a broader market "
			"than the one asked about."
		)
	return warnings


# ── 3. get ──────────────────────────────────────────────────────────────────
def get_breakeven_analysis(args: dict) -> ToolResult:
	"""One analysis in full, with its cost lines, scenarios and overlay. Read-only."""
	_require()
	doc = _doc(as_str(args, "name", required=True), resolve_company(as_str(args, "company")) or "")
	described = _describe(doc)

	unit = (doc.unit_label or "unit").lower()
	if str(doc.status or "") == "Draft":
		described["status_note"] = (
			"never computed. Every result below is zero because nothing has been read yet, not "
			"because this crop costs nothing — run compute_breakeven."
		)
		summary = f"{doc.name}: {doc.crop_type}, never computed"
	elif str(doc.status or "") == "Stale":
		described["status_note"] = (
			"STALE: an input changed after the last computation, so the results below answer the "
			"OLD inputs. compute_breakeven brings them forward."
		)
		summary = f"{doc.name}: {doc.crop_type}, results are stale"
	else:
		summary = (
			f"{doc.name}: {doc.crop_type} breaks even at {float(doc.breakeven_price or 0):,.2f} "
			f"per {unit} ({doc.packout_pct:g}% packout)"
		)
	return ToolResult(data=described, summary=summary)


# ── 4. list ─────────────────────────────────────────────────────────────────
_LIST_FIELDS = (
	"name",
	"analysis_name",
	"company",
	"fiscal_year",
	"crop_type",
	"unit_label",
	"currency",
	"status",
	"cost_source",
	"expected_harvest_units",
	"packout_pct",
	"expected_price",
	"sellable_units",
	"total_fixed_cost",
	"total_variable_cost",
	"contribution_margin_per_unit",
	"breakeven_units",
	"breakeven_price",
	"breakeven_packout_pct",
	"projected_profit",
	"margin_of_safety_pct",
	"usda_price",
	"usda_spread",
	"computed_on",
	"modified",
	"owner",
)


def list_breakeven_analyses(args: dict) -> ToolResult:
	"""The register, with the headline number on every row. Read-only.

	NAMES THE ONES THAT ARE NOT ANSWERS. A Draft has never been computed and a
	Stale one answers inputs somebody has since changed; both carry a full set of
	numeric columns that read exactly like a computed row. Reporting them
	separately is what stops a list being scanned as though every row were live.
	"""
	_require()
	limit = min(as_limit(args), RECORD_CAP)
	filters: dict = {}
	company = resolve_company(as_str(args, "company"), required=False)
	if company:
		filters["company"] = company
	for key in ("fiscal_year", "crop_type", "status", "cost_source"):
		value = as_str(args, key)
		if value:
			filters[key] = value

	rows = [
		dict(row)
		for row in frappe.db.get_all(
			ANALYSIS,
			filters=filters,
			fields=compat.existing_fields(ANALYSIS, _LIST_FIELDS),
			order_by="modified desc",
			limit=limit,
		)
		or []
	]

	stale = [row["name"] for row in rows if row.get("status") == "Stale"]
	draft = [row["name"] for row in rows if row.get("status") == "Draft"]
	losing = [
		row["name"]
		for row in rows
		if row.get("status") == "Computed" and float(row.get("contribution_margin_per_unit") or 0) <= 0
	]

	note = ""
	if losing:
		note = (
			f"{len(losing)} computed analysis/analyses have a contribution margin at or below zero, "
			"which means no volume breaks even and the breakeven UNITS column is empty for them by "
			"design rather than by omission: there is no such quantity."
		)

	return ToolResult(
		data={
			"analyses": rows,
			"analysis_count": len(rows),
			"stale": stale,
			"never_computed": draft,
			"no_volume_breaks_even": losing,
			"note": note,
			"status_note": (
				f"{len(draft)} never computed, {len(stale)} stale. Both carry a full set of numeric "
				"columns that read like a live result — a Draft's are zeros and a Stale one's "
				"answer inputs somebody has since changed."
			)
			if (draft or stale)
			else "",
		},
		summary=(
			f"{len(rows)} breakeven analysis/analyses"
			+ (f" for {company}" if company else "")
			+ (f", {len(stale)} stale" if stale else "")
		),
	)


# ── 5. sensitivity ──────────────────────────────────────────────────────────
def _market_reference(doc):
	try:
		from .. import market_prices

		return market_prices.reference_for_crop(str(doc.crop_type or ""), str(doc.unit_label or ""))
	except Exception:  # pragma: no cover - market data is context; it never fails the sensitivity
		return None


def get_breakeven_sensitivity(args: dict) -> ToolResult:
	"""What-if over one variable, across a range. Read-only.

	STORES NOTHING. `compute_breakeven` writes a standard band onto the record;
	this one answers whatever band was asked for and leaves the register alone,
	so what is stored depends on who ran a computation rather than on who had
	been browsing.

	READS THE STORED PILES rather than the ledger, which is what makes a
	twenty-point sweep instant. The piles were written by the last computation,
	so an analysis that has never been computed is refused rather than answered
	from zeros — a sensitivity table over a fixed cost of zero is twenty rows of
	the same wrong number.
	"""
	_require()
	doc = _doc(as_str(args, "name", required=True), resolve_company(as_str(args, "company")) or "")
	if str(doc.status or "") == "Draft":
		raise ToolError(
			f"{doc.name} has never been computed, so there are no cost piles to move. A "
			"sensitivity table over a fixed cost of zero would be a page of the same wrong "
			"number — run compute_breakeven first."
		)

	variable = (as_str(args, "variable") or "price").strip().lower().replace(" ", "_")
	if variable not in SENSITIVITY_VARIABLES:
		raise ToolError(f"variable must be one of: {', '.join(SENSITIVITY_VARIABLES)}. Got {variable!r}.")

	changes = _range(args.get("range"))
	fixed = float(doc.total_fixed_cost or 0)
	# The RATES, not the totals — stored by the last computation at the record's
	# own baseline. Re-deriving them here against the current volumes would
	# cancel the harvest/sellable split out of every row; see the module docstring.
	rate_harvest = float(doc.rate_harvest or 0)
	rate_sellable = float(doc.rate_sellable or 0)

	rows = []
	for change in changes:
		row = _scenario(
			variable, change, fixed=fixed, rate_harvest=rate_harvest, rate_sellable=rate_sellable, doc=doc
		)
		rows.append(row)

	# The point of a sensitivity table is the derivative, so it is computed here
	# rather than left for a reader to eyeball off the rows.
	base = float(doc.breakeven_price or 0)
	swing = [row["breakeven_price"] for row in rows if row["breakeven_price"] is not None]
	sensitivity = ""
	if swing and base:
		spread = max(swing) - min(swing)
		sensitivity = (
			f"across this range the breakeven price moves {spread:,.2f} — {spread / base * 100:,.1f}% "
			f"of its base of {base:,.2f}. That ratio, not the rows, is what says whether "
			f"{variable.replace('_', ' ')} is the term worth managing."
		)

	flips = [row for row in rows if row["contribution_margin_per_unit"] <= 0]
	return ToolResult(
		data={
			"analysis": doc.name,
			"crop_type": doc.crop_type,
			"unit_label": doc.unit_label or "Unit",
			"variable": variable,
			"base": {
				"expected_price": float(doc.expected_price or 0),
				"expected_harvest_units": float(doc.expected_harvest_units or 0),
				"packout_pct": float(doc.packout_pct or 0),
				"total_fixed_cost": fixed,
				"total_variable_harvest_cost": float(doc.total_variable_harvest_cost or 0),
				"total_variable_sellable_cost": float(doc.total_variable_sellable_cost or 0),
				"rate_harvest": rate_harvest,
				"rate_sellable": rate_sellable,
				"baseline_harvest_units": float(doc.baseline_harvest_units or 0),
				"baseline_packout_pct": float(doc.baseline_packout_pct or 0),
				"breakeven_price": base,
				"breakeven_units": _number(doc.breakeven_units),
			},
			"scenarios": rows,
			"scenario_count": len(rows),
			"sensitivity": sensitivity,
			# v0.266.0. What the market has actually paid, for the price assumption's range (USDA AMS history).
			"market_reference": _market_reference(doc),
			"no_volume_breaks_even_at": [row["change_pct"] for row in flips],
			"note": (
				f"{len(flips)} scenario(s) have a contribution margin at or below zero: at those "
				"levels no volume breaks even, so their breakeven UNITS is null by design. The "
				"breakeven PRICE is still reported there and is the number that matters."
			)
			if flips
			else "",
			"stored": False,
			"storage_note": (
				"nothing was written. compute_breakeven stores a standard band on the analysis; "
				"this read leaves the register alone."
			),
		},
		summary=(
			f"{doc.name}: {len(rows)} {variable} scenario(s) from {min(changes):g}% to {max(changes):g}%"
		),
	)


def _range(raw) -> list:
	"""The requested band as a list of percentage changes.

	ACCEPTS A LIST OR A SINGLE SPREAD. `[-20, -10, 0, 10, 20]` is the explicit
	form; `20` means the five-point band from -20 to +20, which is what somebody
	asking "how sensitive is this to price?" usually means and saves them
	typing it.
	"""
	if raw in (None, "", []):
		return [-20.0, -10.0, 0.0, 10.0, 20.0]
	if isinstance(raw, (int, float)):
		spread = abs(float(raw))
		if spread <= 0:
			raise ToolError("range as a single number must be greater than zero.")
		return [-spread, -spread / 2, 0.0, spread / 2, spread]
	if not isinstance(raw, list):
		raise ToolError(
			"range must be a list of percentage changes — [-20, -10, 0, 10, 20] — or a single "
			f"number meaning that spread either side of zero. Got {type(raw).__name__}."
		)
	out = []
	for index, entry in enumerate(raw):
		try:
			out.append(float(entry))
		except (TypeError, ValueError):
			raise ToolError(f"range[{index}] is {entry!r}, which is not a number.") from None
	if not out:
		raise ToolError("range is empty.")
	if len(out) > 50:
		raise ToolError(f"range has {len(out)} points, which is more than the 50 this answers in one call.")
	return out
