# SPDX-License-Identifier: MIT
"""The starting book of agricultural master data, and the seeder that lays it down.

WHAT THIS IS FOR. A fresh site with no crops, no markets and no units can hold a
spray record but cannot check one, can hold a settlement but cannot say what a
bin weighed, and can hold a breakeven whose packout assumption came from nowhere.
Seeding a starting book means the answer to "what does this site think a bin of
cherries weighs" is a row somebody can read and correct, rather than a number
whoever called the tool happened to pass.

IT ONLY EVER CREATES WHAT IS NOT THERE. Checked by docname, one record at a time,
on install AND after every migrate — the same contract as `_inspection_templates`
and `_compliance_rules`, and the same reason `test_hooks.py` forbids the word
`fixtures` by name. An operator who corrected the cherry bin weight to their own
weighed figure keeps it. One who deleted a market they do not sell into does NOT
get it back next migrate, because the check is by name across every row rather
than only the live ones. One who added a variety keeps it: nothing here ever
touches a record that already exists.

THE SEEDED NUMBERS ARE A STARTING POINT AND THEY SAY SO. Every conversion except
the three definitions is `Nominal` — the trade's rule of thumb, right enough to
plan with and not right enough to settle a dispute with. Every yield is an
expectation. The grade premiums are illustrative ladders with the right SHAPE,
not this season's prices, and an operation that leaves them untouched and quotes
them at a lender has misused them. `list_markets` reports which markets still
carry no grades precisely so the ones that were never reviewed stay visible.

NOTHING HERE FAILS AN INSTALL. Every insert is attempted alone and a failure is
printed and stepped over. A site without ERPNext has no UOM master, so the units
and the conversions that link to them are skipped by name rather than taking the
migrate down with them — and the crops and markets, which link to neither, are
seeded anyway.
"""

from __future__ import annotations

import frappe

from . import ag_uom
from .compat import doctype_exists

CROP = "Crop"
MARKET = "Market"
UOM_CONTEXT = "Agricultural UOM Context"
UOM_CONVERSION = "Agricultural UOM Conversion"

#: The three crops this app was built for, with the varieties actually planted in
#: the Pacific Northwest. Days to harvest are FROM BLOOM, these all being
#: perennials. The PHI figures are conservative crop-level floors and are not
#: label intervals — see `tools/agronomy.PHI_CAVEAT`, which every tool that
#: reports one carries.
SEED_CROPS = (
	{
		"crop_name": "Sweet Cherry",
		"scientific_name": "Prunus avium",
		"crop_type": "Stone Fruit",
		"growth_cycle": "Perennial",
		"days_to_harvest": 60,
		"harvest_window_start": "June",
		"harvest_window_end": "August",
		"default_phi_days": 3,
		"varieties": (
			{
				"variety_name": "Bing",
				"rootstock": "Mazzard",
				"pollination_group": "S3S4",
				"expected_yield_per_acre": 4.5,
				"maturity_years": 5,
			},
			{
				"variety_name": "Rainier",
				"rootstock": "Mazzard",
				"pollination_group": "S1S4",
				"expected_yield_per_acre": 4.0,
				"maturity_years": 5,
			},
			{
				"variety_name": "Sweetheart",
				"rootstock": "Gisela 6",
				"pollination_group": "Self-fertile",
				"expected_yield_per_acre": 5.5,
				"maturity_years": 4,
			},
			{
				"variety_name": "Skeena",
				"rootstock": "Gisela 6",
				"pollination_group": "Self-fertile",
				"expected_yield_per_acre": 5.0,
				"maturity_years": 4,
			},
			{
				"variety_name": "Lapin",
				"rootstock": "Gisela 6",
				"pollination_group": "Self-fertile",
				"expected_yield_per_acre": 5.0,
				"maturity_years": 4,
			},
		),
		"water_requirements": (
			{"growth_stage": "Dormant", "crop_coefficient_kc": 0.2, "water_inches_per_week": 0.0},
			{"growth_stage": "Bud Break", "crop_coefficient_kc": 0.35, "water_inches_per_week": 0.5},
			{"growth_stage": "Bloom", "crop_coefficient_kc": 0.55, "water_inches_per_week": 0.9},
			{"growth_stage": "Fruit Set", "crop_coefficient_kc": 0.75, "water_inches_per_week": 1.2},
			{
				"growth_stage": "Fruit Development",
				"crop_coefficient_kc": 1.0,
				"water_inches_per_week": 1.8,
			},
			{
				"growth_stage": "Harvest",
				"crop_coefficient_kc": 0.9,
				"water_inches_per_week": 1.4,
				"notes": (
					"Holding back before the pick is deliberate on cherries — it firms the fruit "
					"and reduces cracking. A block irrigated to the full figure here looks "
					"correctly watered and packs out worse."
				),
			},
			{
				"growth_stage": "Post-Harvest",
				"crop_coefficient_kc": 0.85,
				"water_inches_per_week": 1.5,
				"notes": (
					"Sets next year's fruit bud. The stage most often skipped, because the fruit "
					"is already sold and the consequence arrives eleven months later."
				),
			},
		),
	},
	{
		"crop_name": "Apple",
		"scientific_name": "Malus domestica",
		"crop_type": "Tree Fruit",
		"growth_cycle": "Perennial",
		"days_to_harvest": 150,
		"harvest_window_start": "August",
		"harvest_window_end": "October",
		"default_phi_days": 14,
		"varieties": (
			{
				"variety_name": "Fuji",
				"rootstock": "M.9",
				"pollination_group": "Group 4",
				"expected_yield_per_acre": 60.0,
				"maturity_years": 4,
			},
			{
				"variety_name": "Gala",
				"rootstock": "M.9",
				"pollination_group": "Group 3",
				"expected_yield_per_acre": 55.0,
				"maturity_years": 4,
			},
			{
				"variety_name": "Honeycrisp",
				"rootstock": "G.41",
				"pollination_group": "Group 4",
				"expected_yield_per_acre": 40.0,
				"maturity_years": 5,
			},
		),
		"water_requirements": (
			{"growth_stage": "Dormant", "crop_coefficient_kc": 0.2, "water_inches_per_week": 0.0},
			{"growth_stage": "Bloom", "crop_coefficient_kc": 0.5, "water_inches_per_week": 0.8},
			{"growth_stage": "Fruit Set", "crop_coefficient_kc": 0.8, "water_inches_per_week": 1.3},
			{
				"growth_stage": "Fruit Development",
				"crop_coefficient_kc": 1.1,
				"water_inches_per_week": 2.0,
			},
			{"growth_stage": "Harvest", "crop_coefficient_kc": 0.95, "water_inches_per_week": 1.6},
			{"growth_stage": "Post-Harvest", "crop_coefficient_kc": 0.8, "water_inches_per_week": 1.2},
		),
	},
	{
		"crop_name": "Pear",
		"scientific_name": "Pyrus communis",
		"crop_type": "Tree Fruit",
		"growth_cycle": "Perennial",
		"days_to_harvest": 140,
		"harvest_window_start": "August",
		"harvest_window_end": "September",
		"default_phi_days": 14,
		"varieties": (
			{
				"variety_name": "Bartlett",
				"rootstock": "OHxF 87",
				"pollination_group": "Group 3",
				"expected_yield_per_acre": 45.0,
				"maturity_years": 5,
			},
			{
				"variety_name": "Anjou",
				"rootstock": "OHxF 97",
				"pollination_group": "Group 4",
				"expected_yield_per_acre": 40.0,
				"maturity_years": 6,
			},
		),
		"water_requirements": (
			{"growth_stage": "Dormant", "crop_coefficient_kc": 0.2, "water_inches_per_week": 0.0},
			{"growth_stage": "Bloom", "crop_coefficient_kc": 0.5, "water_inches_per_week": 0.8},
			{"growth_stage": "Fruit Set", "crop_coefficient_kc": 0.8, "water_inches_per_week": 1.3},
			{
				"growth_stage": "Fruit Development",
				"crop_coefficient_kc": 1.05,
				"water_inches_per_week": 1.9,
			},
			{"growth_stage": "Harvest", "crop_coefficient_kc": 0.95, "water_inches_per_week": 1.5},
			{"growth_stage": "Post-Harvest", "crop_coefficient_kc": 0.8, "water_inches_per_week": 1.2},
		),
	},
)

#: The three outlets a Pacific Northwest tree fruit operation actually sells
#: into. THE GRADE PREMIUMS ARE ILLUSTRATIVE LADDERS, NOT PRICES. What is worth
#: keeping from them is the SHAPE — that fresh pays a large multiple of
#: processing, and that within fresh the spread between the top grade and orchard
#: run is the largest single assumption in a breakeven. The magnitudes are for an
#: operator to replace with their own contract.
SEED_MARKETS = (
	{
		"market_name": "Pacific Northwest Fresh Cherry",
		"market_type": "Fresh",
		"region": "Pacific Northwest",
		"country": "United States",
		"currency": "USD",
		"primary_commodity": "Sweet Cherry",
		"shipping_point": "Washington and Oregon Cherries",
		"notes": (
			"The shipping point is spelled as USDA Market News spells it, because that string is "
			"the entire join to their daily price series."
		),
		"grade_standards": (
			{
				"grade_name": "Washington Extra Fancy",
				"min_size_mm": 26.5,
				"max_defect_pct": 5.0,
				"pack_style": "15 lb clamshell",
				"premium_pct": 25.0,
			},
			{
				"grade_name": "Washington Fancy",
				"min_size_mm": 24.0,
				"max_defect_pct": 8.0,
				"pack_style": "20 lb bulk",
				"premium_pct": 0.0,
			},
			{
				"grade_name": "Orchard Run",
				"min_size_mm": 22.0,
				"max_defect_pct": 15.0,
				"pack_style": "20 lb bulk",
				"premium_pct": -30.0,
			},
		),
	},
	{
		"market_name": "Washington Processing",
		"market_type": "Processing",
		"region": "Columbia Basin",
		"country": "United States",
		"currency": "USD",
		"primary_commodity": "Apple",
		"shipping_point": "Washington Apples",
		"notes": (
			"Where fruit goes that the fresh line will not take. The premiums are deeply negative "
			"against the fresh base on purpose: that gap IS the reason a packout percentage is "
			"worth forecasting to the point."
		),
		"grade_standards": (
			{
				"grade_name": "Peeler",
				"min_size_mm": 60.0,
				"max_defect_pct": 20.0,
				"pack_style": "bulk bin",
				"premium_pct": -60.0,
			},
			{
				"grade_name": "Juice",
				"min_size_mm": 0.0,
				"max_defect_pct": 50.0,
				"pack_style": "bulk bin",
				"premium_pct": -80.0,
			},
		),
	},
	{
		"market_name": "Export - Asia/Pacific",
		"market_type": "Export",
		"region": "Asia/Pacific",
		"country": "",
		"currency": "USD",
		"primary_commodity": "Sweet Cherry",
		"shipping_point": "Washington and Oregon Cherries",
		"notes": (
			"No country recorded, deliberately: this is a programme covering several "
			"destinations. Split it into one Market per country the moment their grade or "
			"phytosanitary rules diverge — which they will."
		),
		"grade_standards": (
			{
				"grade_name": "Export Premium",
				"min_size_mm": 28.5,
				"max_defect_pct": 2.0,
				"pack_style": "2-layer tray",
				"premium_pct": 60.0,
			},
			{
				"grade_name": "Export Standard",
				"min_size_mm": 26.5,
				"max_defect_pct": 5.0,
				"pack_style": "5 kg carton",
				"premium_pct": 30.0,
			},
		),
	},
)


def seed_agricultural_masters() -> dict:
	"""Lay down the starting book. Idempotent; never touches an existing record.

	Returns a report of what was created and what could not be, so the installer
	can print the second and stay quiet about an unremarkable second run.
	"""
	report: dict = {"created": [], "skipped": [], "failed": []}
	_seed_uoms(report)
	_seed_crops(report)
	_seed_contexts(report)
	_seed_conversions(report)
	return report


def _note(report: dict, bucket: str, what: str, reason: str = "") -> None:
	report[bucket].append({"name": what, "reason": reason} if reason else {"name": what})


def _seed_uoms(report: dict) -> None:
	"""Make sure the nine agricultural units exist in ERPNext's own UOM register.

	SEEDING ROWS INTO A FOREIGN MASTER, WHICH THIS APP DOES — the same thing
	`company.ensure_party_types` does — as distinct from adding COLUMNS to one,
	which `hooks.py` promises it does not. A unit is a row; nothing about UOM's
	shape changes.

	Skipped entirely on a site with no UOM doctype. That is a Frappe bench
	without ERPNext, where the register these units would join does not exist and
	nothing downstream could store one anyway.
	"""
	if not doctype_exists("UOM"):
		_note(report, "skipped", "the agricultural units", "this site has no UOM doctype (no ERPNext)")
		return
	for spec in ag_uom.SEED_UOMS:
		name = spec["uom_name"]
		if frappe.db.exists("UOM", name):
			continue
		try:
			doc = frappe.new_doc("UOM")
			doc.uom_name = name
			#: Set unconditionally rather than behind a meta check. ERPNext's UOM
			#: has carried this column for many versions, and a Frappe document
			#: simply does not persist an attribute its meta does not know — so
			#: the check bought nothing and cost an interrogation of a foreign
			#: doctype's meta, which is not always loadable at install time.
			doc.must_be_whole_number = spec["must_be_whole_number"]
			doc.insert(ignore_permissions=True)
			_note(report, "created", f"UOM {name}")
		except Exception as exc:  # pragma: no cover - a site with a locked-down UOM
			_note(report, "failed", f"UOM {name}", f"{type(exc).__name__}: {exc}")


def _link_target(doctype: str, value: str) -> str:
	"""`value` if it is a real row of `doctype`, else "".

	Guards the two links into masters this app does not own — Country and
	Currency. A seeded market naming a country the site's own list does not carry
	would fail link validation and take the whole seed with it, and the market is
	worth more than the country column on it.
	"""
	value = str(value or "").strip()
	if not value or not doctype_exists(doctype):
		return ""
	try:
		return value if frappe.db.exists(doctype, value) else ""
	except Exception:  # pragma: no cover
		return ""


def _seed_crops(report: dict) -> None:
	if not doctype_exists(CROP):
		_note(report, "skipped", "the seeded crops", "this site has no Crop doctype — run `bench migrate`")
		return
	for spec in SEED_CROPS:
		name = spec["crop_name"]
		if frappe.db.exists(CROP, name):
			continue
		try:
			doc = frappe.new_doc(CROP)
			for key, value in spec.items():
				if key in ("varieties", "water_requirements"):
					continue
				doc.set(key, value)
			for row in spec["varieties"]:
				doc.append("varieties", dict(row))
			for row in spec["water_requirements"]:
				doc.append("water_requirements", dict(row))
			doc.insert(ignore_permissions=True)
			_note(report, "created", f"Crop {name}")
		except Exception as exc:  # pragma: no cover
			_note(report, "failed", f"Crop {name}", f"{type(exc).__name__}: {exc}")

	if not doctype_exists(MARKET):
		_note(
			report, "skipped", "the seeded markets", "this site has no Market doctype — run `bench migrate`"
		)
		return
	for spec in SEED_MARKETS:
		name = spec["market_name"]
		if frappe.db.exists(MARKET, name):
			continue
		try:
			doc = frappe.new_doc(MARKET)
			for key, value in spec.items():
				if key == "grade_standards":
					continue
				if key == "country":
					value = _link_target("Country", value)
				elif key == "currency":
					value = _link_target("Currency", value)
				elif key == "primary_commodity":
					value = value if frappe.db.exists(CROP, value) else ""
				doc.set(key, value)
			doc.is_active = 1
			for row in spec["grade_standards"]:
				doc.append("grade_standards", dict(row))
			doc.insert(ignore_permissions=True)
			_note(report, "created", f"Market {name}")
		except Exception as exc:  # pragma: no cover
			_note(report, "failed", f"Market {name}", f"{type(exc).__name__}: {exc}")


def _seed_contexts(report: dict) -> None:
	if not doctype_exists(UOM_CONTEXT):
		_note(report, "skipped", "the unit contexts", "this site has no Agricultural UOM Context doctype")
		return
	if not doctype_exists("UOM"):
		_note(report, "skipped", "the unit contexts", "this site has no UOM doctype (no ERPNext)")
		return
	for spec in ag_uom.SEED_CONTEXTS:
		name = spec["context_name"]
		if frappe.db.exists(UOM_CONTEXT, name):
			continue
		#: Every unit must exist before the context naming it is inserted — a
		#: context is refused when empty, and an allow-list half-populated
		#: because one Link failed would be an allow-list that silently forbids
		#: what it was written to permit.
		units = [row for row in spec["uoms"] if frappe.db.exists("UOM", row["uom"])]
		if not units:
			_note(report, "failed", f"context {name}", "none of its units exist in the UOM register")
			continue
		try:
			doc = frappe.new_doc(UOM_CONTEXT)
			doc.context_name = name
			doc.applies_to = spec["applies_to"]
			doc.description = spec["description"]
			doc.is_active = 1
			for row in units:
				doc.append("uoms", dict(row))
			doc.insert(ignore_permissions=True)
			_note(report, "created", f"context {name}")
		except Exception as exc:  # pragma: no cover
			_note(report, "failed", f"context {name}", f"{type(exc).__name__}: {exc}")


def _seed_conversions(report: dict) -> None:
	if not doctype_exists(UOM_CONVERSION):
		_note(
			report, "skipped", "the unit conversions", "this site has no Agricultural UOM Conversion doctype"
		)
		return
	if not doctype_exists("UOM"):
		_note(report, "skipped", "the unit conversions", "this site has no UOM doctype (no ERPNext)")
		return
	for spec in ag_uom.SEED_CONVERSIONS:
		crop = spec.get("crop") or ""
		if not frappe.db.exists("UOM", spec["from_uom"]) or not frappe.db.exists("UOM", spec["to_uom"]):
			continue
		if crop and not (doctype_exists(CROP) and frappe.db.exists(CROP, crop)):
			#: The crop-specific rows are skipped rather than genericised when
			#: their crop is absent. A bin-to-pound factor with the crop dropped
			#: is not a weaker version of the same fact — it is a claim about
			#: every fruit, which is the exact error this doctype exists to stop.
			continue
		name = f"{spec['from_uom']} to {spec['to_uom']}" + (f" - {crop}" if crop else "")
		if frappe.db.exists(UOM_CONVERSION, name):
			continue
		try:
			doc = frappe.new_doc(UOM_CONVERSION)
			doc.from_uom = spec["from_uom"]
			doc.to_uom = spec["to_uom"]
			doc.crop = crop
			doc.factor = spec["factor"]
			doc.basis = spec["basis"]
			doc.source = spec.get("source") or ""
			doc.notes = spec.get("notes") or ""
			doc.is_active = 1
			doc.insert(ignore_permissions=True)
			_note(report, "created", f"conversion {name}")
		except Exception as exc:  # pragma: no cover
			_note(report, "failed", f"conversion {name}", f"{type(exc).__name__}: {exc}")


# ── the soil book ───────────────────────────────────────────────────────────
#
# v0.116.0. How long the ground stays too wet to drive on after a set, by USDA
# textural class. THE SHAPE IS WHAT IS WORTH KEEPING, NOT THE FIGURES: a sand is
# back under a tractor before the shift ends and a clay is not for two and a half
# days, and that spread of nearly an order of magnitude is the whole reason a
# single hard-coded twenty-four hours could never have served both. The
# particular hours are drainage-class shapes and every one of them says
# `shipped default` in its own `source` column, so an operator reading the
# register can tell a number nobody has reviewed from one this farm measured.
#
# EIGHT CLASSES AND NOT TWELVE. The USDA texture triangle has twelve; four of
# them (silt, sandy clay, silty clay loam, sandy clay loam) sit between neighbours
# already here and would be four more rows nobody ever picks between. A farm that
# needs one adds it with `create_soil_compaction_profile` — the register is
# authorable precisely so the shipped list can be short.
#
# Loam is 24/48, which is deliberately the same pair as `overlays.DEFAULT_RED_HOURS`
# and `DEFAULT_YELLOW_HOURS`. A block with no profile is coloured as a loam, which
# is the middle of this book rather than a number invented for the fallback — and
# the overlay still reports `thresholds_source: default` so the two are never
# confused.
SOIL_PROFILE = "Soil Compaction Profile"

SEED_SOIL_PROFILES = (
	{"soil_type": "Sand", "drainage_class": "Rapid", "red_hours": 8, "yellow_hours": 16},
	{"soil_type": "Loamy Sand", "drainage_class": "Rapid", "red_hours": 10, "yellow_hours": 20},
	{"soil_type": "Sandy Loam", "drainage_class": "Well Drained", "red_hours": 16, "yellow_hours": 30},
	{"soil_type": "Loam", "drainage_class": "Well Drained", "red_hours": 24, "yellow_hours": 48},
	{
		"soil_type": "Silt Loam",
		"drainage_class": "Moderately Well Drained",
		"red_hours": 30,
		"yellow_hours": 60,
	},
	{
		"soil_type": "Clay Loam",
		"drainage_class": "Somewhat Poorly Drained",
		"red_hours": 40,
		"yellow_hours": 72,
	},
	{"soil_type": "Silty Clay", "drainage_class": "Poorly Drained", "red_hours": 48, "yellow_hours": 84},
	{"soil_type": "Clay", "drainage_class": "Poorly Drained", "red_hours": 60, "yellow_hours": 96},
)

#: What every seeded row carries in `source`. ONE STRING, so a listing can be
#: filtered on it and an operator can see at a glance which figures on their site
#: are still the ones the app shipped.
SHIPPED_SOURCE = "shipped default — drainage-class shape, not a measurement"


def seed_soil_profiles() -> dict:
	"""Lay down the soil book. Idempotent; never touches an existing record.

	Separate from `seed_agricultural_masters` and not folded into it, because the
	two answer to different things: that book is crops, markets and units, and
	this is one register behind one map layer. An operator reading the install
	log should be able to tell which of the two ran.
	"""
	report: dict = {"created": [], "skipped": [], "failed": []}
	if not doctype_exists(SOIL_PROFILE):
		_note(
			report,
			"skipped",
			"the soil compaction profiles",
			f"this site has no {SOIL_PROFILE} doctype — run `bench migrate`",
		)
		return report
	for spec in SEED_SOIL_PROFILES:
		name = spec["soil_type"]
		if frappe.db.exists(SOIL_PROFILE, name):
			continue
		try:
			doc = frappe.new_doc(SOIL_PROFILE)
			for key, value in spec.items():
				doc.set(key, value)
			doc.source = SHIPPED_SOURCE
			doc.enabled = 1
			doc.insert(ignore_permissions=True)
			_note(report, "created", f"{SOIL_PROFILE} {name}")
		except Exception as exc:  # pragma: no cover - a site with the register locked down
			_note(report, "failed", f"{SOIL_PROFILE} {name}", f"{type(exc).__name__}: {exc}")
	return report
