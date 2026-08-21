# SPDX-License-Identifier: MIT
"""Controller for Soil Compaction Profile — the two hours that colour a block.

ONE RULE, AND IT IS WORTH A CONTROLLER BECAUSE GETTING IT WRONG IS SILENT.
`yellow_hours` must be strictly greater than `red_hours`. A profile with the two
the other way round — or with them equal — leaves an EMPTY caution band: the
overlay walks red, then yellow, then green, so a yellow that is not outside the
red is a band no block can ever land in. Every wet block goes straight from red
to green the moment the red hours pass, the drying-out warning that the whole
layer exists to draw is never drawn, and nothing anywhere reports an error. The
first symptom is a rutted block.

BOTH FIGURES MUST BE POSITIVE for the same reason, one step earlier. A red of
zero means no block is ever wet; a negative one is the same thing with a number
that looks deliberate. `reqd` on the DocType catches a blank, and a blank Float
in Frappe is `0.0` — which is exactly the value that has to be refused here,
because it is the one that arrives without anybody having typed it.

`bands` IS OWNED HERE AND IMPORTED, NOT COPIED. The overlay engine, any future
report and the Desk page all have to answer "which colour is this many hours"
the same way. A second copy of a two-branch comparison is a second chance to
write `<=` where `<` belongs, and the boundary case — a block at exactly the red
figure — is the one a grower will argue about.
"""

import frappe
from frappe import _
from frappe.model.document import Document

RED = "red"
YELLOW = "yellow"
GREEN = "green"

#: What the overlay reports for a zone whose valves have never been logged. NOT
#: a colour, because there is nothing to colour it from — an unlogged zone and a
#: zone irrigated last month are opposite facts and a map that drew them alike
#: would be inventing the more comforting one.
UNKNOWN = "unknown"

#: The band an actively-open valve lands in. Water on the ground right now is
#: further from driveable than water that came off an hour ago, and rolling it
#: into `red` would lose the one state a foreman can act on immediately — go and
#: shut the valve, or do not send the tractor at all today.
IRRIGATING = "irrigating"


def band(hours_since, red_hours, yellow_hours) -> str:
	"""Which colour `hours_since` falls in. The ONE implementation.

	`None` hours — nothing logged — is `UNKNOWN` and never green. A zone this app
	has no valve history for has not been proven dry; it has not been measured,
	and the two must not share a colour.

	THE BOUNDARIES ARE CLOSED ON THE LOWER SIDE: a block at exactly `red_hours`
	is YELLOW, not red, and one at exactly `yellow_hours` is GREEN. Stated here
	rather than discovered, because "is 24.0 hours still red" is the question
	somebody standing at a gate with a phone will ask out loud.
	"""
	if hours_since is None:
		return UNKNOWN
	try:
		elapsed = float(hours_since)
		red = float(red_hours)
		yellow = float(yellow_hours)
	except (TypeError, ValueError):  # pragma: no cover - a hand-edited Float column
		return UNKNOWN
	if elapsed < red:
		return RED
	if elapsed < yellow:
		return YELLOW
	return GREEN


class SoilCompactionProfile(Document):
	def validate(self):
		self.soil_type = (self.soil_type or "").strip()
		if not self.soil_type:
			frappe.throw(_("Soil Type is required — it is this profile's own name."))
		self._positive("red_hours", _("Red Under (Hours)"))
		self._positive("yellow_hours", _("Yellow Under (Hours)"))
		if float(self.yellow_hours or 0) <= float(self.red_hours or 0):
			frappe.throw(
				_(
					"Yellow Under ({0} hours) must be GREATER than Red Under ({1} hours). "
					"As written there is no caution band at all: a block would go straight "
					"from red to green the moment {1} hours had passed, and the drying-out "
					"warning this profile exists to draw would never appear on the map. "
					"Nothing was saved."
				).format(self.yellow_hours, self.red_hours)
			)

	def _positive(self, fieldname: str, label: str) -> None:
		try:
			value = float(self.get(fieldname) or 0)
		except (TypeError, ValueError):
			value = 0.0
		if value <= 0:
			frappe.throw(
				_(
					"{0} must be more than zero. A profile with {1} of {2} says this soil is "
					"never too wet to drive on, which is a claim no soil supports — and a "
					"blank Float arrives here as zero without anybody having typed it. "
					"Nothing was saved."
				).format(label, fieldname, value)
			)
