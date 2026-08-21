# SPDX-License-Identifier: MIT
"""Controller for Crop Observation — what was seen, and nothing about what to do.

THE THRESHOLD EVALUATION IS NOT HERE. It lives in `tools/cropprotect.py`, and
the split is deliberate rather than incidental: evaluating a threshold means
resolving one (a query), upserting a Pest Pressure row (a write to another
doctype) and possibly generating a recommendation (a second write). A controller
that did all that would fire on every save from every surface — a Desk edit
correcting a typo in the notes would regenerate a recommendation somebody had
already declined.

So this validates the observation AS A MEASUREMENT and stops. The pipeline is
driven from the tool, once, at the moment the observation is filed.

WHAT IS VALIDATED IS ONLY WHAT MAKES THE NUMBER MEANINGLESS. A negative count, a
sample smaller than the count taken out of it, a percentage over a hundred, a
date in the future. Not a missing sample size — that is flagged downstream and
recorded, because a scout who saw something worth writing down should never be
arguing with a form.

v0.115.0: WHAT A ROUND HAS TO CARRY NOW DEPENDS ON WHAT IT WAS FOR. `threat`,
`threat_category` and `count_observed` were unconditionally mandatory, because
until now every observation was a pest count. A Harvest Readiness round is a
Brix reading and a growth stage with no organism in it, and there are three
honest ways to file one: refuse it (so a farm records maturity in a spreadsheet
and the map has nothing to colour), invent a threat for it (so a pest nobody
looked for acquires a season of sightings), or say what the round was for and
ask only for what that kind of round produces. The third is `observation_type`,
and this controller is where its consequences are enforced — the DocType's
`mandatory_depends_on` says the same thing to the Desk, and a rule stated only
in JSON is a rule every tool-side write goes around.

THE DEFAULT IS `Pest Scout`, SO NOTHING WRITTEN BEFORE v0.115.0 CHANGES MEANING.
Every existing row was a pest count and is stamped as one by
`backfill_observation_type`; every existing caller sends a threat and a count and
is unaffected. What is new is the round that could not be filed at all.

BRIX IS VALIDATED AS A MEASUREMENT, NOT AS A DECISION. A negative reading is
impossible and a reading above `BRIX_CEILING` is a decimal point in the wrong
place, and both are refused. Whether 19° is ripe is not this controller's
question — that is the crop's, the variety's and the buyer's. What IS refused is
a method with no reading behind it, and what is REQUIRED is a method where there
is one: a bare number is the one that later gets quoted as if it were
instrumented when somebody eyeballed it.
"""

import frappe
from frappe import _
from frappe.model.document import Document

THREAT_CATEGORIES = ("Insect", "Disease", "Weed", "Vertebrate", "Abiotic", "Nutrient")

#: What the round was for. `PEST_SCOUT` is the only one that carries a threat, a
#: count and a threshold evaluation — see the module docstring for why the other
#: three exist and why they are not pest counts with the pest left blank.
PEST_SCOUT = "Pest Scout"
HARVEST_READINESS = "Harvest Readiness"
GENERAL = "General"
GROWTH_STAGE = "Growth Stage"

OBSERVATION_TYPES = (PEST_SCOUT, HARVEST_READINESS, GENERAL, GROWTH_STAGE)

#: How a Brix reading was taken. Kept apart deliberately: a refractometer figure
#: is a measurement and an estimate is a recollection, and averaging the two
#: produces a number with a refractometer's authority and an eyeball's accuracy.
BRIX_METHODS = ("Refractometer", "Estimate")

#: The highest Brix this app will accept on a piece of fruit. Ripe sweet cherries
#: run 16-24°, table grapes to about 26°, and a raisin or a concentrate is not
#: what anybody is scouting. Forty is well clear of every fresh-market crop and
#: well under the 190 a misplaced decimal produces, so it catches the typo
#: without arguing with an unusually good block.
BRIX_CEILING = 40.0

#: Sample units where the count is a proportion rather than a tally, and so
#: cannot exceed one hundred. Kept as a set rather than a substring test on
#: "Percent" so that a unit added later has to be considered rather than
#: silently inheriting a rule written for a different shape of number.
PERCENT_UNITS = ("Percent Infested", "Percent Defoliation", "Percent Dry Weight")


class CropObservation(Document):
	def validate(self):
		self.observation_type = str(self.observation_type or "").strip() or PEST_SCOUT
		if self.observation_type not in OBSERVATION_TYPES:
			frappe.throw(_("Observation Type must be one of: {0}.").format(", ".join(OBSERVATION_TYPES)))
		self._validate_threat()
		self._validate_brix()

		count = _number(self.count_observed)
		if count < 0:
			frappe.throw(
				_(
					"Count Observed cannot be negative. A count of nothing is 0, which is a real "
					"observation and a useful one — it is how a block is shown to have been walked "
					"and found clean."
				)
			)
		if self.sample_size not in (None, ""):
			size = int(self.sample_size)
			if size < 0:
				frappe.throw(_("Sample Size cannot be negative."))
			if size and str(self.sample_unit or "") in PERCENT_UNITS and count > 100:
				frappe.throw(
					_("Count Observed is {0} on a percentage unit ({1}).").format(count, self.sample_unit)
				)
		if self.percent_affected not in (None, "") and not 0 <= _number(self.percent_affected) <= 100:
			frappe.throw(_("Percent Affected must be between 0 and 100."))
		if _number(self.beneficials_observed) < 0:
			frappe.throw(_("Beneficials Observed cannot be negative."))
		if self.observed_on and str(self.observed_on) > str(frappe.utils.nowdate()):
			frappe.throw(
				_(
					"Observed On is {0}, which is in the future. A scouting round is filed after it "
					"is walked; a future-dated observation puts a pressure trend ahead of the season "
					"and no later correction can tell it apart from a real one."
				).format(self.observed_on)
			)
		if not self.observed_at and self.observed_on:
			self.observed_at = f"{self.observed_on} 12:00:00"

	def _validate_threat(self) -> None:
		"""A Pest Scout names what it was counting. The other three do not have to.

		A ROUND THAT NAMES A THREAT KEEPS IT WHATEVER ITS TYPE, and the category
		is still checked when one is given — a Harvest Readiness walk that also
		noted cherry fruit fly is a better record than one that dropped it, and a
		miscategorised threat is as wrong on a maturity check as on a scout. What
		is relaxed is the OBLIGATION, not the vocabulary.
		"""
		category = str(self.threat_category or "").strip()
		threat = str(self.threat or "").strip()

		if category and category not in THREAT_CATEGORIES:
			frappe.throw(_("Threat Category must be one of: {0}.").format(", ".join(THREAT_CATEGORIES)))

		if self.observation_type != PEST_SCOUT:
			# A threat named on a non-scout round is kept; a threat named with no
			# category is not, because the category is what the threshold lookup
			# and the pressure register both key on.
			if threat and not category:
				frappe.throw(
					_(
						"This {0} observation names the threat {1!r} with no Threat Category. A "
						"threat with no category cannot be matched to a threshold or filed under "
						"a pressure record, so it would be a sighting nothing downstream can "
						"read. Give the category, or put the sighting in the notes."
					).format(self.observation_type, threat)
				)
			return

		if not category:
			frappe.throw(
				_(
					"Threat Category is required on a Pest Scout observation. It must be one of: "
					"{0}. If this round was not counting an organism, say so with Observation "
					"Type — a maturity check filed as a pest scout puts a walk that was not "
					"looking for the pest into that pest's pressure trend."
				).format(", ".join(THREAT_CATEGORIES))
			)
		if not threat:
			frappe.throw(
				_(
					"An observation is of something. Name the threat, or set Observation Type to "
					"{0}, {1} or {2} — those are the rounds that legitimately have no organism in "
					"them, and inventing one to satisfy this form is how a pest nobody saw "
					"acquires a season of sightings."
				).format(HARVEST_READINESS, GENERAL, GROWTH_STAGE)
			)
		if self.count_observed in (None, ""):
			frappe.throw(
				_(
					"Count Observed is required on a Pest Scout observation. Zero is a real and "
					"useful answer — it is how a block is shown to have been walked and found "
					"clean — but a blank is not, because nothing downstream can tell it from a "
					"count nobody took."
				)
			)

	def _validate_brix(self) -> None:
		"""The maturity reading as a MEASUREMENT. Whether it means ripe is not ours.

		See the module docstring. A method with no reading describes a
		measurement that was not taken, and a reading with no method is the one
		that gets quoted later as if a refractometer had produced it.
		"""
		method = str(self.brix_method or "").strip()
		has_reading = self.brix_reading not in (None, "")

		if method and method not in BRIX_METHODS:
			frappe.throw(_("Brix Method must be one of: {0}.").format(", ".join(BRIX_METHODS)))
		if method and not has_reading:
			frappe.throw(
				_(
					"Brix Method is {0} but there is no Brix reading. The method describes a "
					"measurement, and stating how a number was taken when there is no number is "
					"a record that reads as instrumented and holds nothing."
				).format(method)
			)
		if not has_reading:
			return
		if not method:
			frappe.throw(
				_(
					"A Brix reading of {0} was given with no Brix Method. Say whether it came off "
					"a refractometer or was estimated: the two are not the same measurement, and "
					"the number that gets quoted into a buyer's specification is the one nobody "
					"can tell apart afterwards."
				).format(_number(self.brix_reading))
			)

		reading = _number(self.brix_reading)
		if reading < 0:
			frappe.throw(_("Brix cannot be negative."))
		if reading > BRIX_CEILING:
			frappe.throw(
				_(
					"Brix is {0}, which is above {1} — the ceiling this app accepts on fruit. "
					"Ripe sweet cherries run 16-24 and table grapes to about 26, so a figure this "
					"high is almost always a decimal point in the wrong place. Nothing was saved."
				).format(f"{reading:g}", f"{BRIX_CEILING:g}")
			)


def _number(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0
