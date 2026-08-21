# SPDX-License-Identifier: MIT
"""Controller for Crop — what is grown, as a record rather than as a string.

THE DOCNAME IS THE CROP NAME, AND RENAME IS OFF. `field:crop_name` with
`allow_rename: 0`. Every other record that names a crop spells this string, and
a master that can be re-keyed is a master whose name on last season's records
means something else this season. Renaming a crop is therefore not an edit; it
is a new crop and a migration of what pointed at the old one.

A HARVEST WINDOW IS BOTH ENDS OR NEITHER. A start with no end is a season
nothing closes, and every reader of it has to guess the other half. The window
is deliberately ALLOWED TO WRAP the year — November to February is a real
harvest, and a controller that insisted start <= end would be refusing the
southern hemisphere and the greenhouse both, which is a rule about integers
mistaken for a rule about farming.

`maturity_years` IS A CONTRADICTION ON AN ANNUAL, NOT A LONG NUMBER. Years to
first commercial crop only means anything where the planting lives across
seasons. A Crop Variety row claiming three years to maturity under an Annual
growth cycle is refused by name, because the alternative — storing it — puts a
number into the capitalisation of development cost for a planting that has no
development period at all.

FOUR UNIQUENESS RULES, ALL ABOUT DOUBLE-COUNTING. Two variety rows called
'Bing' are two rows about one tree, and every read that groups a packout or a
yield by variety silently doubles it. Two water rows for 'Bloom' are two answers
to one irrigation question, and which one wins depends on row order. The same
two rules again on the per-variety tables: one override per variety and stage,
and no protocol step entered twice. None of the four is a database nicety; all
four are wrong answers that look like right ones.

THE PER-VARIETY TABLES ARE OVERLAYS AND THEIR REAL RISK IS THE ROW THAT NEVER
FIRES (v0.114.0). `variety_water_requirements` and `variety_protocols` hang off
the CROP and name their variety as text, because Frappe has no nested child
tables and Crop Variety is itself a child. So a row can name a variety that is
not in the catalogue — a typo, a trailing space, a variety somebody removed —
and store perfectly well while resolving to nothing: the reader falls back to
the crop default and the form still shows what looks like a recorded decision.
That is invisible from both ends, so it is refused on save, and the same
reasoning refuses a water override carrying neither of its two numbers.

WHAT THIS CONTROLLER DOES NOT DO IS DECIDE A PHI. `default_phi_days` is the
crop's own floor for the case where nothing more specific is known. The binding
interval is on the label of the material actually applied, and a spray gate that
read this column and stopped reading would be a gate that clears fruit a label
would hold. See `tools/agronomy.get_crop`, which reports the number and says
what it is not.
"""

import frappe
from frappe import _
from frappe.model.document import Document

#: The growth cycles for which `maturity_years` is a meaningful number. An
#: annual and a biennial both finish inside the period they are planted for, so
#: "years until it bears" is not a question either of them has.
BEARING_CYCLES = ("Perennial",)

#: The highest crop coefficient this controller will store. Kc runs from about
#: 0.2 in dormancy to about 1.2 at full canopy; the ceiling is set well clear of
#: the real range so it only ever catches a decimal point in the wrong place,
#: which is the error that would otherwise multiply an irrigation set by ten.
MAX_KC = 1.5


def _row_index(row, position: int) -> int:
	"""The row number to put in a refusal: Frappe's `idx` where there is one.

	A child row that has never been through a save has no `idx` yet, and a
	message reading "Row None" is a message nobody can act on — so the position
	in the list stands in. Read with `.get` rather than attribute access because
	that is what every other controller in this app does with a child row, and
	because the two are not the same object at every point in a document's life.
	"""
	return int(row.get("idx") or position)


class Crop(Document):
	def validate(self):
		self.crop_name = str(self.crop_name or "").strip()
		if not self.crop_name:
			frappe.throw(_("Crop Name is required — it is the docname every other record spells."))
		self.scientific_name = str(self.scientific_name or "").strip()

		self._check_counts()
		self._check_share()
		self._check_harvest_window()
		# ORDER MATTERS FROM HERE DOWN. `_check_varieties` is what proves the
		# variety list is free of duplicates, and the two override checks below
		# index that list to resolve the variety each of their rows names. An
		# index built over a list with two 'Bing' rows in it would quietly answer
		# for whichever came last.
		self._check_varieties()
		self._check_water_requirements()
		self._check_variety_water_requirements()
		self._check_variety_protocols()

	def _check_counts(self) -> None:
		"""Days and intervals are counts, so negative ones are typos, not values."""
		for fieldname, label in (
			("days_to_harvest", "Days to Harvest"),
			("default_phi_days", "Default PHI"),
		):
			value = self.get(fieldname)
			if value in (None, ""):
				continue
			if int(value) < 0:
				frappe.throw(_("{0} cannot be negative.").format(label))

	def _check_share(self) -> None:
		"""A share of a whole runs 0 to 100, and anything else is a unit mistake.

		The error this catches is 0.35 typed for thirty-five percent, which stores
		without complaint and reads as a third of one percent — a survey line off by
		two orders of magnitude that looks entirely plausible on the page. The
		refusal names both readings rather than just the bound, because the number
		that was meant is usually the one nobody typed.
		"""
		value = self.get("pct_direct_marketed")
		if value in (None, ""):
			return
		share = float(value)
		if 0 <= share <= 100:
			return
		frappe.throw(
			_(
				"Direct-Marketed % is {0}. A share of a crop runs from 0 to 100 — {0} is "
				"either a fraction that wants multiplying by a hundred or a decimal point "
				"in the wrong place."
			).format(share),
			title=_("Share Out of Range"),
		)

	def _check_harvest_window(self) -> None:
		"""Both ends of the window or neither.

		Deliberately says nothing about the ORDER of the two months. A window
		that wraps the year end is a real harvest, and the obvious `start <= end`
		check would refuse it — which is a rule about integers wearing the
		costume of a rule about farming.
		"""
		start = str(self.harvest_window_start or "").strip()
		end = str(self.harvest_window_end or "").strip()
		if bool(start) == bool(end):
			return
		named, missing = ("start", "end") if start else ("end", "start")
		frappe.throw(
			_(
				"The harvest window has a {0} and no {1}. Half a window is a season nothing "
				"closes, and every reader of it has to guess the other month. Give both or "
				"neither — a window that wraps the year end (November to February) is accepted."
			).format(named, missing),
			title=_("Incomplete Harvest Window"),
		)

	def _check_varieties(self) -> None:
		"""No duplicate variety, and no years-to-maturity on something that has none."""
		seen: dict = {}
		for position, row in enumerate(self.varieties or [], start=1):
			index = _row_index(row, position)
			variety_name = str(row.get("variety_name") or "").strip()
			if not variety_name:
				frappe.throw(_("Row {0}: a variety needs a name.").format(index))

			key = variety_name.casefold()
			if key in seen:
				frappe.throw(
					_(
						"Rows {0} and {1} are both called {2}. Two rows about one tree double it "
						"in every read that groups a packout or a yield by variety — keep one."
					).format(seen[key], index, variety_name),
					title=_("Duplicate Variety"),
				)
			seen[key] = index

			maturity = int(row.get("maturity_years") or 0)
			if float(row.get("expected_yield_per_acre") or 0) < 0:
				frappe.throw(_("Row {0}: expected yield cannot be negative.").format(index))
			if maturity < 0:
				frappe.throw(_("Row {0}: years to maturity cannot be negative.").format(index))
			if maturity and self.growth_cycle not in BEARING_CYCLES:
				frappe.throw(
					_(
						"Row {0} ({1}) says it takes {2} year(s) to bear, but {3} is a {4} crop — "
						"it finishes inside the season it is planted for, so there are no "
						"non-bearing years to record. Either clear the years, or the growth "
						"cycle is wrong."
					).format(
						index,
						variety_name,
						maturity,
						self.crop_name,
						str(self.growth_cycle or "").lower(),
					),
					title=_("Maturity Years on a Non-Perennial"),
				)

	def _check_water_requirements(self) -> None:
		"""One row per growth stage, and a Kc inside the range a Kc can be."""
		seen: dict = {}
		for position, row in enumerate(self.water_requirements or [], start=1):
			index = _row_index(row, position)
			stage = str(row.get("growth_stage") or "").strip()
			if not stage:
				frappe.throw(_("Row {0}: a water requirement needs a growth stage.").format(index))
			if stage in seen:
				frappe.throw(
					_(
						"Rows {0} and {1} are both about {2}. Two answers to how much water this "
						"crop needs at one stage is the same as none — which one an irrigation "
						"plan reads would depend on row order."
					).format(seen[stage], index, stage),
					title=_("Duplicate Growth Stage"),
				)
			seen[stage] = index

			kc = float(row.get("crop_coefficient_kc") or 0)
			if kc < 0:
				frappe.throw(_("Row {0}: a crop coefficient cannot be negative.").format(index))
			if float(row.get("water_inches_per_week") or 0) < 0:
				frappe.throw(_("Row {0}: weekly water cannot be negative.").format(index))
			if kc > MAX_KC:
				frappe.throw(
					_(
						"Row {0} ({1}) has a crop coefficient of {2}. Kc runs from about 0.2 in "
						"dormancy to about 1.2 at full canopy — a value above {3} is a decimal "
						"point in the wrong place, and it would multiply an irrigation set by ten."
					).format(index, stage, kc, MAX_KC),
					title=_("Crop Coefficient Out of Range"),
				)

	# ── the two override tables ──────────────────────────────────────────
	#
	# WHAT BOTH OF THESE ARE REALLY CHECKING is that a row can ever fire. An
	# override names its variety as text, because Frappe has no nested child
	# tables and Crop Variety is itself a child — so 'Bing ' with a trailing
	# space, or 'bing', or a variety somebody removed from the catalogue last
	# season, all store perfectly well and all resolve to nothing. The row then
	# sits on the form looking like a recorded decision while the resolver
	# silently falls back to the crop default, which is the failure mode worth
	# spending a refusal on: it is invisible from both ends.

	def _variety_index(self) -> dict:
		"""Casefolded variety name → the name as the catalogue spells it.

		Built from `varieties`, which `_check_varieties` has already proved has
		no duplicates. Casefolded because 'bing' and 'Bing' are one tree and an
		override that missed on capitalisation would fall back to the crop
		figure with nothing to show it had.
		"""
		return {
			str(row.get("variety_name") or "").strip().casefold(): str(row.get("variety_name") or "").strip()
			for row in self.varieties or []
			if str(row.get("variety_name") or "").strip()
		}

	def _resolve_variety(self, row, index: int, known: dict, table_label: str) -> str:
		"""The catalogue spelling of the variety this row names, or a refusal."""
		named = str(row.get("variety") or "").strip()
		if not named:
			frappe.throw(_("Row {0}: a {1} row needs a variety.").format(index, table_label))
		found = known.get(named.casefold())
		if found:
			return found
		if not known:
			frappe.throw(
				_(
					"Row {0} is a {1} for {2}, but this crop has no varieties recorded at all. "
					"Add {2} to the Varieties table first — an override for a variety the "
					"catalogue does not have never fires, and nothing would ever show that."
				).format(index, table_label, named),
				title=_("No Such Variety"),
			)
		frappe.throw(
			_(
				"Row {0} is a {1} for {2}, which is not a variety of {3}. The varieties "
				"recorded are: {4}. An override naming a variety the catalogue does not have "
				"never fires and the reader falls back to the crop default — so this would "
				"look like a recorded decision and behave like an empty row."
			).format(index, table_label, named, self.crop_name, ", ".join(sorted(known.values()))),
			title=_("No Such Variety"),
		)

	def _check_variety_water_requirements(self) -> None:
		"""One row per variety and stage, a Kc in range, and a variety that exists."""
		known = self._variety_index()
		seen: dict = {}
		for position, row in enumerate(self.variety_water_requirements or [], start=1):
			index = _row_index(row, position)
			variety = self._resolve_variety(row, index, known, _("water override"))
			# Write the catalogue's spelling back, so the stored row and the
			# variety list agree exactly and the resolver's lookup is a plain
			# match rather than a second casefold at read time.
			row.variety = variety

			stage = str(row.get("growth_stage") or "").strip()
			if not stage:
				frappe.throw(_("Row {0}: a water override needs a growth stage.").format(index))

			key = (variety.casefold(), stage)
			if key in seen:
				frappe.throw(
					_(
						"Rows {0} and {1} both override {2} at {3}. Two answers to how much "
						"water one variety needs at one stage is the same as none — which one "
						"an irrigation plan read would depend on row order."
					).format(seen[key], index, variety, stage),
					title=_("Duplicate Variety Override"),
				)
			seen[key] = index

			kc = row.get("crop_coefficient_kc")
			inches = row.get("water_inches_per_week")
			if float(kc or 0) < 0:
				frappe.throw(_("Row {0}: a crop coefficient cannot be negative.").format(index))
			if float(inches or 0) < 0:
				frappe.throw(_("Row {0}: weekly water cannot be negative.").format(index))
			if float(kc or 0) > MAX_KC:
				frappe.throw(
					_(
						"Row {0} ({1} at {2}) has a crop coefficient of {3}. Kc runs from about "
						"0.2 in dormancy to about 1.2 at full canopy — a value above {4} is a "
						"decimal point in the wrong place, and it would multiply an irrigation "
						"set by ten."
					).format(index, variety, stage, float(kc or 0), MAX_KC),
					title=_("Crop Coefficient Out of Range"),
				)

			# An override that overrides nothing is the other invisible row: it
			# resolves, it matches a stage, and it hands the resolver two blanks,
			# so the crop figure stands and the row has done nothing at all.
			if kc in (None, "") and inches in (None, ""):
				frappe.throw(
					_(
						"Row {0} overrides {1} at {2} with neither a Kc nor a weekly depth, so it "
						"changes nothing — the crop's own figure would still be what an "
						"irrigation plan reads. Give one of the two numbers, or delete the row."
					).format(index, variety, stage),
					title=_("Override With Nothing In It"),
				)

	def _check_variety_protocols(self) -> None:
		"""A variety that exists, and no step entered twice.

		DELIBERATELY NOT UNIQUE ON (variety, practice). A GA program is two or
		three applications at different timings and a rule that refused the
		second one would refuse the commonest real recipe in the file. What is
		refused is the exact repeat — same variety, same practice, same stage,
		same product — which is double entry rather than a schedule.
		"""
		known = self._variety_index()
		seen: dict = {}
		for position, row in enumerate(self.variety_protocols or [], start=1):
			index = _row_index(row, position)
			variety = self._resolve_variety(row, index, known, _("protocol"))
			row.variety = variety

			practice = str(row.get("practice") or "").strip()
			if not practice:
				frappe.throw(_("Row {0}: a protocol step needs a practice.").format(index))

			stage = str(row.get("timing_stage") or "").strip()
			product = str(row.get("product") or "").strip()
			key = (variety.casefold(), practice, stage, product.casefold())
			if key in seen:
				frappe.throw(
					_(
						"Rows {0} and {1} are the same step: {2}, {3}, {4}, {5}. A program with "
						"two applications records them at two timings — two identical rows are "
						"one step entered twice, and anything reading this as a schedule would "
						"plan the pass twice."
					).format(
						seen[key],
						index,
						variety,
						practice,
						stage or _("no stage"),
						product or _("no product"),
					),
					title=_("Duplicate Protocol Step"),
				)
			seen[key] = index
