# SPDX-License-Identifier: MIT
"""The agricultural unit catalogue: the nine units, what they measure, and the
factors between them.

WHY THIS IS A MODULE AND NOT THREE COPIES. `install.py` seeds these units and
these conversions, `agricultural_uom_context.py` checks a context's units
against what they measure, and `tools/agronomy.py` reads both to answer a
conversion. Three copies of "a bin is a container, not a weight" is how two of
them come to disagree, and the disagreement surfaces as a settlement that is out
by a factor nobody can trace.

A CONTAINER IS NOT A WEIGHT, AND THAT DISTINCTION IS THE POINT OF `DIMENSION`.
Bins, lugs, buckets and bushels are *counts of containers*. They convert to a
weight only through a nominal figure that depends on the fruit, the fill and the
season — which is exactly why `Agricultural UOM Conversion` carries a crop and
a basis, and exactly why ERPNext's own crop-blind `UOM Conversion Factor` cannot
hold these numbers without being wrong about one crop or the other.

WHAT ERPNext ALREADY SHIPS IS NOT ASSUMED. ERPNext's UOM list is long and its
contents vary by version; whether it has a "Bin" is not a thing this app should
guess. `SEED_UOMS` is therefore written as "make sure these exist", checked one
at a time, and the installer creates only what is missing. That is the same
shape as `company.ensure_party_types` — seeding ROWS into a foreign master,
which this app does, as distinct from adding COLUMNS to one, which it does not.

THE SEEDED FACTORS ARE A STARTING BOOK, NOT THE TRUTH ABOUT ANY FARM. Every one
of them is `Nominal` except the three that are definitions. An operation that
weighs its own bins should record what it measured as `Operation Average` with a
source, at which point its own number wins the lookup and the trade rule of
thumb stops being consulted. Nothing here overwrites a row an operator has
edited; see `install._agricultural_uom_conversions`.
"""

from __future__ import annotations

#: What each unit measures. CONTAINERS ARE `Count`, deliberately and against the
#: intuition that a bin "is" a weight: a bin is a box, and the weight of what is
#: in it is a separate, crop-dependent, seasonal fact. Modelling it as a weight
#: is what lets a site add bins of cherries to bins of apples and get a number.
#:
#: A unit absent from this map is not checked by anything here. That is
#: deliberate: an operator adding their own unit to a context should not have to
#: teach this module about it first.
DIMENSION = {
	"Bin": "Count",
	"Lug": "Count",
	"Bucket": "Count",
	"Bushel": "Count",
	"Pound": "Weight",
	"Ton": "Weight",
	"Gallon": "Volume",
	"Fluid Ounce": "Volume",
	"Acre": "Area",
	"Square Foot": "Area",
	# v0.197.0. A bait block is counted, not weighed: the label says "1 or 2
	# blocks", and a half block in a station is not a thing anybody places.
	"Block": "Count",
	"Ounce": "Weight",
}

#: The units the installer makes sure exist, with the `must_be_whole_number`
#: flag ERPNext's UOM carries. Containers are whole-number units — half a bin is
#: not a thing anybody hands to a checker — and the continuous measures are not.
SEED_UOMS = (
	{"uom_name": "Bin", "must_be_whole_number": 1},
	{"uom_name": "Lug", "must_be_whole_number": 1},
	{"uom_name": "Bucket", "must_be_whole_number": 1},
	{"uom_name": "Bushel", "must_be_whole_number": 0},
	{"uom_name": "Pound", "must_be_whole_number": 0},
	{"uom_name": "Ton", "must_be_whole_number": 0},
	{"uom_name": "Gallon", "must_be_whole_number": 0},
	{"uom_name": "Fluid Ounce", "must_be_whole_number": 0},
	{"uom_name": "Acre", "must_be_whole_number": 0},
	{"uom_name": "Square Foot", "must_be_whole_number": 0},
	# v0.197.0, AFB-2026-00023. The crop-protection units a label rate is
	# written in and ERPNext's own list does not reliably carry: Block for bait
	# (whole), Ounce for a dry product's rate by weight.
	{"uom_name": "Block", "must_be_whole_number": 1},
	{"uom_name": "Ounce", "must_be_whole_number": 0},
)

#: The contexts, and which units each one accepts. NOTE WHAT IS *NOT* MIXED:
#: "Harvest" counts containers and "Scale Ticket" weighs, and they are two
#: contexts rather than one list containing both. A field crew hands in bins and
#: the shed reports pounds; those are two measurements of one delivery, and a
#: single list that accepted either is a list that lets them be summed.
SEED_CONTEXTS = (
	{
		"context_name": "Harvest",
		"applies_to": "Count",
		"description": (
			"What a picking crew hands in and what a field tally counts. Containers only — "
			"what they weigh is the shed's measurement, recorded on a Scale Ticket, and "
			"converted through Agricultural UOM Conversion rather than assumed here."
		),
		"uoms": (
			{"uom": "Bin", "is_default": 1, "notes": "The orchard bin the crew fills and the truck carries."},
			{"uom": "Lug", "is_default": 0, "notes": "The field box; several to a bin."},
			{"uom": "Bucket", "is_default": 0, "notes": "What a picker carries and a checker tallies."},
		),
	},
	{
		"context_name": "Spray",
		"applies_to": "Volume",
		"description": (
			"Tank volumes and product measures. A rate written in the wrong unit is the "
			"error this list exists to refuse: an ounce read as a gallon is a 128-fold "
			"overdose, and the label it violates is the law."
		),
		"uoms": (
			{"uom": "Gallon", "is_default": 1, "notes": "Tank capacity and water volume."},
			{"uom": "Fluid Ounce", "is_default": 0, "notes": "Product measured into the tank."},
		),
	},
	{
		"context_name": "Field Area",
		"applies_to": "Area",
		"description": (
			"Ground. Acres are how every rate, every lease and every cost-per-unit in this "
			"app is stated; square feet exist here so an irrigation zone measured off a "
			"design drawing has somewhere to be converted from."
		),
		"uoms": (
			{"uom": "Acre", "is_default": 1, "notes": "The unit every per-acre figure is stated in."},
			{
				"uom": "Square Foot",
				"is_default": 0,
				"notes": "As measured off a drawing; 43,560 to the acre.",
			},
		),
	},
	{
		"context_name": "Scale Ticket",
		"applies_to": "Weight",
		"description": (
			"What a certified scale reports. Weights only, and containers deliberately "
			"absent: a scale weighs, and a ticket that could be denominated in bins would "
			"let a counted delivery and a weighed one be added together."
		),
		"uoms": (
			{"uom": "Pound", "is_default": 1, "notes": "The unit US packers settle in."},
			{"uom": "Ton", "is_default": 0, "notes": "Processing loads and orchard totals."},
		),
	},
	# v0.197.0. TWO CONTEXTS AND NOT ONE "CROP PROTECTION" LIST, because the
	# controller refuses a list that mixes measurements: a bait block is counted
	# and a dry product is weighed, and one list offering both is a list that
	# lets "2" mean either.
	{
		"context_name": "Bait",
		"applies_to": "Count",
		"description": (
			"Rodent and insect bait placed by the piece. A bait label's rate is a count per "
			'station or per placement — "1 or 2 blocks of bait" — and is stocked the same way.'
		),
		"uoms": ({"uom": "Block", "is_default": 1, "notes": "One bait block, as the label counts it."},),
	},
	{
		"context_name": "Dry Product",
		"applies_to": "Weight",
		"description": (
			"Dry crop-protection products measured by weight into the tank or the spreader — "
			"wettable powders, granules, dusts. Liquids are the Spray context."
		),
		"uoms": (
			{"uom": "Pound", "is_default": 1, "notes": "How dry product rates are usually printed."},
			{"uom": "Ounce", "is_default": 0, "notes": "Sixteen to the pound; small-block rates."},
		),
	},
)

#: The starting book of factors. `factor` is always "how many `to_uom` in ONE
#: `from_uom`", which is the direction the doctype states in a sentence rather
#: than leaving to the reader — getting it backwards produces a number, not an
#: error.
#:
#: THE FOUR `Exact` ROWS ARE DEFINITIONS and carry no crop, because a quantity
#: that varies by crop is not a definition. Everything else is `Nominal`: the
#: trade's rule of thumb, right enough to plan with and not right enough to
#: settle a dispute with. A farm that weighs its own containers should replace
#: the relevant row with an `Operation Average` carrying a source.
SEED_CONVERSIONS = (
	{
		"from_uom": "Gallon",
		"to_uom": "Fluid Ounce",
		"crop": "",
		"factor": 128.0,
		"basis": "Exact",
		"source": "US customary definition",
	},
	{
		"from_uom": "Ton",
		"to_uom": "Pound",
		"crop": "",
		"factor": 2000.0,
		"basis": "Exact",
		"source": "US short ton definition",
	},
	{
		"from_uom": "Pound",
		"to_uom": "Ounce",
		"crop": "",
		"factor": 16.0,
		"basis": "Exact",
		"source": "US avoirdupois definition",
	},
	{
		"from_uom": "Acre",
		"to_uom": "Square Foot",
		"crop": "",
		"factor": 43560.0,
		"basis": "Exact",
		"source": "US survey acre definition",
	},
	{
		"from_uom": "Lug",
		"to_uom": "Pound",
		"crop": "",
		"factor": 18.0,
		"basis": "Nominal",
		"source": "Trade rule of thumb",
		"notes": (
			"Carried WITHOUT a crop because 'a lug is eighteen pounds' is what the trade "
			"means by a lug with nothing else said, and it is the cherry lug they mean. An "
			"operation packing apples in lugs should add a crop-specific row — see the "
			"Apple one beside this — which the lookup will prefer."
		),
	},
	{
		"from_uom": "Bin",
		"to_uom": "Pound",
		"crop": "Sweet Cherry",
		"factor": 800.0,
		"basis": "Nominal",
		"source": "Trade rule of thumb",
	},
	{
		"from_uom": "Bin",
		"to_uom": "Pound",
		"crop": "Apple",
		"factor": 900.0,
		"basis": "Nominal",
		"source": "Trade rule of thumb",
		"notes": (
			"A hundred pounds more than the cherry bin out of the same stack of boxes. This "
			"single pair of rows is why this doctype exists instead of ERPNext's own "
			"UOM Conversion Factor, which holds one global number per unit pair."
		),
	},
	{
		"from_uom": "Bin",
		"to_uom": "Pound",
		"crop": "Pear",
		"factor": 900.0,
		"basis": "Nominal",
		"source": "Trade rule of thumb",
	},
	{
		"from_uom": "Lug",
		"to_uom": "Pound",
		"crop": "Apple",
		"factor": 40.0,
		"basis": "Nominal",
		"source": "Standard apple box",
	},
	{
		"from_uom": "Bucket",
		"to_uom": "Pound",
		"crop": "Sweet Cherry",
		"factor": 25.0,
		"basis": "Nominal",
		"source": "Trade rule of thumb",
	},
	{
		"from_uom": "Bushel",
		"to_uom": "Pound",
		"crop": "Apple",
		"factor": 42.0,
		"basis": "Nominal",
		"source": "USDA bushel weight",
	},
	{
		"from_uom": "Bushel",
		"to_uom": "Pound",
		"crop": "Pear",
		"factor": 50.0,
		"basis": "Nominal",
		"source": "USDA bushel weight",
	},
)


def dimension_of(uom: str) -> str:
	"""What `uom` measures, or "" for a unit this module has no opinion about.

	The empty answer is a real answer and callers must treat it as "not checked"
	rather than "mismatched": an operator's own unit is not a schema error.
	"""
	return DIMENSION.get(str(uom or "").strip(), "")
