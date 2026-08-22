# SPDX-License-Identifier: MIT
"""Controller for Planting Season — one block, one crop, one year of it.

THE ANNUAL RULE IS THE ONE RULE HERE WORTH ARGUING ABOUT, so: an Annual's season
year must equal its plant year. That is not a tidiness rule. An annual IS its
planting — it goes in, it produces, it comes out, and the whole cost of putting
it there belongs to the crop it produced. A row claiming to be Annual with a
plant year of 2024 and a season year of 2026 is describing a perennial, and if
it is allowed to save then two years of costs land against one year's revenue
and the block reads as catastrophically unprofitable in 2026 and free in 2024
and 2025.

The perennial case is left deliberately open. A 2021 planting may legitimately
carry season records for 2021 through 2035, and this record cannot know which
year is the last one — that is the `productive_through` estimate, and it is an
estimate rather than a rule.

TREES PER ACRE IS COMPUTED FROM WHICHEVER PAIR IS AVAILABLE, spacing first. Both
routes are honest and they disagree: spacing gives the DESIGN density, and trees
over acres gives what actually went in the ground after the misses, the skips
and the corner nobody could get a tractor into. The design figure is preferred
because it is the one that stays true when six trees die, and the actual is the
fallback for a block nobody recorded a spacing for.

STATUS IS NOT DERIVED FROM THE DATES, and it is worth saying why, because
deriving it looks obviously correct: a block is Productive if today is between
`productive_from` and `productive_through`. It is wrong because those two are
ESTIMATES made at planting, and the transition is a judgement somebody makes
standing in the block. A fourth-leaf planting that is not carrying a commercial
crop yet is still Establishing whatever the plan said in 2021, and a farm that
had its status flipped underneath it by a date it typed four years ago would
stop trusting the column.
"""

import frappe
from frappe import _
from frappe.model.document import Document

ESTABLISHING = "Establishing"
PRODUCTIVE = "Productive"
DECLINING = "Declining"
REMOVED = "Removed"
STATUSES = (ESTABLISHING, PRODUCTIVE, DECLINING, REMOVED)

PERENNIAL = "Perennial"
ANNUAL = "Annual"
LIFECYCLES = (PERENNIAL, ANNUAL)

SQ_FT_PER_ACRE = 43560.0

#: A sanity ceiling on the plant year, expressed as years ahead of today rather
#: than as a literal. A planting ordered for next spring is real; one in 2190 is
#: a typo, and it would sort to the top of every list for ever.
MAX_YEARS_AHEAD = 2


class PlantingSeason(Document):
	def validate(self):
		if self.status not in STATUSES:
			self.status = ESTABLISHING
		if self.lifecycle not in LIFECYCLES:
			self.lifecycle = PERENNIAL
		self._copy_block_ticker()
		self._check_years()
		self._check_dates()
		self._check_removal()
		self._trees_per_acre()
		self._label()

	def _copy_block_ticker(self) -> None:
		"""Take the block's buyer-facing ticker at save time and keep it.

		COPIED RATHER THAN FETCHED, which is the whole point. A `fetch_from` shows
		whatever the Field says TODAY, so re-tickering a block in 2027 would silently
		relabel its 2024 season — and the 2024 settlements that quoted the old ticker
		would no longer agree with the season record they were settled against. A
		season is a closed year; what the buyer called the block that year is part of
		what happened.

		A SEASON WITH NO FIELD KEEPS WHATEVER IT HAS. The link is optional on this
		doctype, and blanking a stored ticker because the link is empty would lose
		the record for no gain.
		"""
		if not self.field:
			return
		ticker = frappe.db.get_value("Field", self.field, "block_ticker")
		if ticker:
			self.block_ticker = str(ticker).strip().upper()

	def _check_years(self):
		if not self.plant_year:
			frappe.throw(_("Plant Year is required — it is what a block's age is measured from."))
		year = int(self.plant_year)
		this_year = int(str(frappe.utils.nowdate())[:4])
		if year < 1800 or year > this_year + MAX_YEARS_AHEAD:
			frappe.throw(
				_(
					"Plant Year {0} is not a plausible year. A planting ordered for next spring is "
					"real; one decades out is a typo, and it would sort to the top of every list "
					"for ever."
				).format(year)
			)
		if self.season_year and int(self.season_year) < year:
			frappe.throw(
				_(
					"Season Year {0} is before Plant Year {1}. A planting cannot have produced a "
					"crop in a year before it went in the ground."
				).format(self.season_year, year)
			)
		if self.lifecycle == ANNUAL and self.season_year and int(self.season_year) != year:
			frappe.throw(
				_(
					"This is marked Annual with plant year {0} and season year {1}. An annual IS "
					"its planting — it goes in, produces, and comes out, so the two years are the "
					"same one. A planting that spans years is Perennial; change the lifecycle "
					"rather than the years. Left as written, several years of establishment cost "
					"would land against a single year's revenue and this block would read as "
					"ruinous in one season and free in the others."
				).format(year, self.season_year)
			)

	def _check_dates(self):
		if (
			self.productive_from
			and self.productive_through
			and str(self.productive_through) < str(self.productive_from)
		):
			frappe.throw(
				_("Productive Through ({0}) is before Productive From ({1}).").format(
					self.productive_through, self.productive_from
				)
			)
		if self.productive_from and int(str(self.productive_from)[:4]) < int(self.plant_year):
			frappe.throw(
				_(
					"Productive From ({0}) is before the plant year ({1}). A block does not bear "
					"before it is planted."
				).format(self.productive_from, self.plant_year)
			)

	def _check_removal(self):
		if self.status == REMOVED and not self.removed_on:
			frappe.throw(
				_(
					"A removed planting needs the date it came out. It is what closes the block's "
					"cost history and what a replant is dated from; without it the ground reads as "
					"still carrying trees that are not there."
				)
			)
		if self.removed_on and str(self.removed_on) > str(frappe.utils.nowdate()):
			frappe.throw(_("Removed On is in the future."))
		if self.replaced_by and self.replaced_by == self.name:
			frappe.throw(_("A planting cannot replace itself."))

	def _trees_per_acre(self):
		"""Design density where the spacings are known, actual otherwise."""
		in_row = _number(self.spacing_in_row_ft)
		between = _number(self.spacing_between_rows_ft)
		if in_row > 0 and between > 0:
			self.trees_per_acre = round(SQ_FT_PER_ACRE / (in_row * between), 1)
			return
		acres = _number(self.acres)
		trees = _number(self.trees_planted)
		self.trees_per_acre = round(trees / acres, 1) if acres > 0 and trees > 0 else 0.0

	def _label(self):
		block = str(self.block_name or "").strip() or str(self.field or "")
		parts = [block, str(self.crop or "").strip()]
		variety = str(self.variety or "").strip()
		if variety:
			parts.append(variety)
		year = self.season_year or self.plant_year
		parts.append(str(year))
		self.season_label = " · ".join(part for part in parts if part)


def _number(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0
