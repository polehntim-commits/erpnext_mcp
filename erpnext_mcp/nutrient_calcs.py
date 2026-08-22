# SPDX-License-Identifier: MIT
"""NPK: reading a fertiliser label, and turning a rate over an area into a nutrient load.

WHAT THIS IS FOR. A nutrient management plan — required by every state that
regulates nitrogen, and by every buyer running a sustainability programme — asks
one question per block per season: how many pounds of N, P and K went onto this
ground. The farm records applications as a PRODUCT and a RATE, and the product
carries a grade printed on the bag. This module is the arithmetic between those
two, and it is the arithmetic that gets done wrong on a spreadsheet every
spring.

THE OXIDE TRAP, WHICH IS THE REASON THIS MODULE IS LONGER THAN ITS FARM_APP
ANCESTOR. A fertiliser grade `10-20-10` does NOT mean 10% nitrogen, 20%
phosphorus, 10% potassium. It means 10% N, 20% **P₂O₅** and 10% **K₂O** — the
first figure is elemental and the other two are oxides, for historical reasons
that nobody defends and everybody follows. Elemental phosphorus is 43.6% of
P₂O₅ and elemental potassium is 83.0% of K₂O. A plan that reports the label
figures as elemental over-states phosphorus by a factor of 2.3, which matters
because phosphorus is the nutrient with the discharge limit on it.

So every function here says which basis it is on, and `nutrients()` returns BOTH:
`p2o5_lb` for the figure that goes on a state form written in oxide, and `p_lb`
for the figure an agronomist balances a crop removal against. Neither is
"correct" without the other, and a single number labelled `phosphorus` would be
read as whichever one the reader expected.

WHAT IT WILL NOT DO IS GUESS A GRADE. `parse_grade` reads what is written and
answers `None` when nothing is. A product whose grade cannot be read contributes
no nutrients and is REPORTED in the aggregate as unaccounted, rather than
counted as zero — a plan that silently treats an unreadable label as clean is a
plan that under-reports, and under-reporting is the direction that gets a farm
fined.

UNITS ARE CARRIED, NEVER ASSUMED. The farm works in pounds per acre and the
world's ag models work in kilograms per hectare, and every conversion bug this
module could have is a factor of 1.12 that looks plausible on a report. So the
rate carries its unit, the answer carries its unit, and `convert_rate` is the
one place a factor is written down.
"""

from __future__ import annotations

import re

#: Elemental fraction of the oxide forms a fertiliser label states. Both are
#: ratios of atomic weights and neither is an approximation anybody rounds
#: differently: P₂O₅ is 2×30.974 / 141.945, K₂O is 2×39.098 / 94.196.
P2O5_TO_P = 0.4364
K2O_TO_K = 0.8301

#: Conversions, each written once. The acre is the US survey-free international
#: acre, which is the one every deed in this county uses.
LB_PER_KG = 2.2046226218
M2_PER_ACRE = 4046.8564224
M2_PER_HECTARE = 10000.0

#: The rate units this understands, and what one unit of each is in kg/m². A
#: rate arriving in anything else is refused by name rather than coerced — see
#: the module docstring on why a silent factor is the failure mode here.
RATE_UNITS = {
	"kg/m2": 1.0,
	"kg/ha": 1.0 / M2_PER_HECTARE,
	"lb/acre": (1.0 / LB_PER_KG) / M2_PER_ACRE,
	"ton/acre": (2000.0 / LB_PER_KG) / M2_PER_ACRE,
	"gal/acre": None,  # needs a density; see `nutrients`
}

#: How the three figures in a grade are separated in the data: `10-10-10`,
#: `10 - 10 - 10`, `10/10/10`, `10,10,10`. Decimal grades are real — `0.5-0-0`
#: is a foliar — so the pattern takes them.
_GRADE = re.compile(
	r"(?<![\d.])(\d{1,2}(?:\.\d+)?)\s*[-/,]\s*(\d{1,2}(?:\.\d+)?)\s*[-/,]\s*(\d{1,2}(?:\.\d+)?)"
)


class NutrientError(ValueError):
	"""A rate whose unit is not one this can convert, or a density it needs and has not got."""


def parse_grade(text) -> tuple | None:
	"""`(N%, P₂O₅%, K₂O%)` from a label grade, or `None` if there is not one.

	Reads the grade out of a product NAME as well as a bare grade string, because
	that is where it lives on this app's Items: `"Urea 46-0-0 (50lb)"` and
	`"46-0-0"` both answer `(46.0, 0.0, 0.0)`. A fourth figure — sulphur, in
	`12-12-12-2S` — is ignored rather than refused; the grade's first three
	numbers are still the grade.

	`None` is returned for anything with no readable grade, INCLUDING text that
	contains three numbers meaning something else. A percentage above 100 is not
	a grade, and a date is not a grade: `2026-08-01` parses as three numbers and
	is rejected because 2026 has four digits, which is the whole reason the
	pattern bounds each figure at two.
	"""
	if text is None:
		return None
	match = _GRADE.search(str(text))
	if not match:
		return None
	try:
		figures = tuple(float(group) for group in match.groups())
	except (TypeError, ValueError):  # pragma: no cover - the pattern only matches floats
		return None
	if any(figure > 100.0 for figure in figures):
		return None
	if sum(figures) > 100.0:
		# A bag cannot be more than 100% fertiliser. Three numbers summing past
		# it are three numbers that are not a grade — most often a lot code.
		return None
	return figures


def grade_of(product) -> tuple | None:
	"""The grade of an Item row or dict, from its own fields then its name.

	Checked in the order an operator would expect to win: an explicit
	`npk_grade`/`npk_ratio` field a site has added, then the separate percent
	fields, then whatever is written in the item name. A site with no NPK fields
	at all — which is every site today — still gets a grade off `"Urea 46-0-0"`,
	which is what makes this usable before any schema change ships.
	"""
	if product is None:
		return None
	read = (
		product.get if isinstance(product, dict) else lambda key, default=None: getattr(product, key, default)
	)

	for key in ("npk_grade", "npk_ratio", "nutrient_grade"):
		parsed = parse_grade(read(key))
		if parsed:
			return parsed

	explicit = [read(key) for key in ("npk_nitrogen", "npk_phosphorus", "npk_potassium")]
	if any(value is not None for value in explicit):
		return tuple(_float(value, 0.0) for value in explicit)

	for key in ("item_name", "name", "description", "product_name"):
		parsed = parse_grade(read(key))
		if parsed:
			return parsed
	return None


def convert_rate(rate, unit: str, density_kg_per_l: float | None = None) -> float:
	"""A rate in any understood unit, as kg of product per m².

	`gal/acre` needs a density, because a gallon of a 32% UAN solution and a
	gallon of a foliar micronutrient are different masses of product. Rather
	than assume water, this refuses and says what it needs — a liquid rate
	converted at the density of water is wrong by however far the product is
	from water, which for UAN is 28%.
	"""
	key = str(unit or "").strip().lower().replace(" ", "").replace("²", "2")
	key = {"kg/m²": "kg/m2", "lbs/acre": "lb/acre", "lb/ac": "lb/acre", "kg/hectare": "kg/ha"}.get(key, key)
	if key not in RATE_UNITS:
		raise NutrientError(
			f"rate unit {unit!r} is not one this converts. Understood: {', '.join(sorted(RATE_UNITS))}. "
			"A unit converted by guess is a factor nobody can find later."
		)
	amount = _float(rate, None)
	if amount is None:
		raise NutrientError(f"rate {rate!r} is not a number")

	if key == "gal/acre":
		if not density_kg_per_l:
			raise NutrientError(
				"a gal/acre rate needs the product's density in kg/L — it is printed on the label, "
				"and a liquid fertiliser is not the density of water (32% UAN is 1.32)."
			)
		litres_per_m2 = 3.785411784 * amount / M2_PER_ACRE
		return litres_per_m2 * float(density_kg_per_l)
	return amount * RATE_UNITS[key]


def nutrients(rate, unit: str, grade, area_m2=None, density_kg_per_l=None) -> dict:
	"""What one application puts down: per area, and in total if an area is given.

	Returns both bases and both unit systems, because the two readers of this
	number want different ones:

	    n_kg_m2, p2o5_kg_m2, k2o_kg_m2   the rate, oxide basis
	    n_lb_acre, p2o5_lb_acre, k2o_lb_acre   the same rate, the way the farm reads it
	    p_kg_m2, k_kg_m2, p_lb_acre, k_lb_acre  elemental, for a removal balance
	    n_lb, p2o5_lb, k2o_lb, p_lb, k_lb   totals, present only with an area

	Nitrogen appears once, without an oxide twin, because the label figure for
	nitrogen already IS elemental.
	"""
	if not grade:
		raise NutrientError("no NPK grade — nothing can be computed from a product whose label was not read")
	nitrogen, p2o5, k2o = (_float(figure, 0.0) for figure in grade)
	product_kg_m2 = convert_rate(rate, unit, density_kg_per_l)

	per_area = {
		"product_kg_m2": product_kg_m2,
		"n_kg_m2": product_kg_m2 * nitrogen / 100.0,
		"p2o5_kg_m2": product_kg_m2 * p2o5 / 100.0,
		"k2o_kg_m2": product_kg_m2 * k2o / 100.0,
	}
	per_area["p_kg_m2"] = per_area["p2o5_kg_m2"] * P2O5_TO_P
	per_area["k_kg_m2"] = per_area["k2o_kg_m2"] * K2O_TO_K
	for key in ("n", "p2o5", "k2o", "p", "k", "product"):
		per_area[f"{key}_lb_acre"] = kg_m2_to_lb_acre(per_area[f"{key}_kg_m2"])

	out = {"grade": (nitrogen, p2o5, k2o), **{key: round(value, 8) for key, value in per_area.items()}}
	acres = _float(area_m2, None)
	if acres is None:
		return out
	out["area_m2"] = acres
	out["area_acres"] = acres / M2_PER_ACRE
	for key in ("n", "p2o5", "k2o", "p", "k", "product"):
		out[f"{key}_kg"] = round(per_area[f"{key}_kg_m2"] * acres, 6)
		out[f"{key}_lb"] = round(out[f"{key}_kg"] * LB_PER_KG, 6)
	return out


def aggregate(applications) -> dict:
	"""A season's nutrient budget from a list of applications.

	Each application is a dict with at least `rate`, `unit` and either `grade` or
	a `product` to read one off. `area_m2` may be per application or absent.

	THE UNACCOUNTED LIST IS THE POINT. An application whose grade could not be
	read, or whose unit is one this cannot convert, appears in `unaccounted` with
	its reason and contributes nothing to the totals. A nutrient plan that
	quietly counted it as zero would under-report, and the whole value of the
	report is that a regulator can trust the total — which they can only do if
	the report says what is missing from it.
	"""
	totals = dict.fromkeys(("n_kg", "p2o5_kg", "k2o_kg", "p_kg", "k_kg", "product_kg"), 0.0)
	area_total = 0.0
	lines, unaccounted = [], []

	for index, application in enumerate(applications or []):
		row = dict(application or {})
		grade = row.get("grade") or grade_of(row.get("product"))
		label = row.get("label") or row.get("product_name") or row.get("item") or f"application {index + 1}"
		if not grade:
			unaccounted.append({"label": label, "reason": "no readable NPK grade on the product"})
			continue
		try:
			computed = nutrients(
				row.get("rate"),
				row.get("unit") or "lb/acre",
				grade,
				row.get("area_m2"),
				row.get("density_kg_per_l"),
			)
		except NutrientError as problem:
			unaccounted.append({"label": label, "reason": str(problem)})
			continue

		line = {"label": label, "date": row.get("date"), **computed}
		lines.append(line)
		if "area_m2" in computed:
			area_total += computed["area_m2"]
			for key in totals:
				totals[key] += computed.get(key, 0.0)

	out = {
		"applications": lines,
		"unaccounted": unaccounted,
		"area_m2": round(area_total, 4),
		"area_acres": round(area_total / M2_PER_ACRE, 4) if area_total else 0.0,
		**{key: round(value, 6) for key, value in totals.items()},
		**{key.replace("_kg", "_lb"): round(value * LB_PER_KG, 4) for key, value in totals.items()},
	}
	for key in ("n", "p2o5", "k2o", "p", "k"):
		out[f"{key}_lb_acre"] = round(per_acre(totals[f"{key}_kg"], area_total), 4)
	return out


# ── conversions, each written once ──────────────────────────────────────────
def kg_to_lb(kg) -> float:
	return _float(kg, 0.0) * LB_PER_KG


def lb_to_kg(lb) -> float:
	return _float(lb, 0.0) / LB_PER_KG


def m2_to_acres(m2) -> float:
	return _float(m2, 0.0) / M2_PER_ACRE


def acres_to_m2(acres) -> float:
	return _float(acres, 0.0) * M2_PER_ACRE


def kg_m2_to_lb_acre(kg_m2) -> float:
	return _float(kg_m2, 0.0) * LB_PER_KG * M2_PER_ACRE


def per_acre(total_kg, area_m2) -> float:
	"""Pounds per acre from a total mass over an area. Zero area answers 0.0.

	Not an error, because an aggregate over applications that carried no area is
	a legitimate thing to ask for — the caller wanted the total and gets it, and
	the per-acre figure is honestly zero rather than a division nobody expected.
	"""
	acres = m2_to_acres(area_m2)
	return 0.0 if not acres else kg_to_lb(total_kg) / acres


def _float(value, fallback):
	"""A float, or the fallback. `0` survives — see the change-guard rule: a rate
	of zero is a caller stating that nothing went down, not a missing rate."""
	if value is None or isinstance(value, bool):
		return fallback
	try:
		return float(value)
	except (TypeError, ValueError):
		return fallback
