# SPDX-License-Identifier: MIT
"""Controller for Housing Unit — one building on a camp.

THE DOCNAME IS `"<unit name> - <parcel abbr>"`, the same shape a Field takes,
because every camp numbers its cabins from one and "Cabin-01" alone is three
different buildings on this operation.

THE OCCUPANCY LIMIT IS COMPUTED ONCE AND THEN LEFT ALONE. Fifty square feet of
sleeping area per occupant is the federal temporary-labor-camp figure Oregon's
rules are built on, so a unit with a floor area has an answer without anybody
typing one. But it is a *default*, not a derivation: a cabin with a fixed bunk
layout, or one under a local code that says otherwise, gets the number somebody
worked out, and recomputing over the top of that would quietly replace a
considered answer with an arithmetic one.

WHAT THIS REFUSES IS SMALL AND THE REASONS ARE ARITHMETIC. A duplicate unit name
on one parcel, because the docname is built from it. Negative area, capacity or
year. Nothing about condition, nothing about inspection dates — a camp that has
not been walked since 2019 is a fact worth recording, and a controller that
refused to record it would be a controller that guaranteed the record stayed
empty.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from erpnext_mcp.abbr import parcel_abbr, suffixed

#: Square feet of sleeping area per occupant. 29 CFR 1910.142(b)(1) sets 50 for
#: a temporary labor camp, and Oregon's agricultural labor housing rules are
#: written against the same figure. Named, because a bare 50 in a division is a
#: number nobody can check.
SQ_FT_PER_OCCUPANT = 50

#: Unit types that hold nobody: shared facilities and farm buildings. Used to
#: decide whether a capacity of zero is a gap or the correct answer, and to
#: refuse an assignment into a shower block.
NON_RESIDENTIAL_TYPES = ("Toilet-Shower", "Kitchen", "Bath House", "Barn", "Shop")

#: Capacity above which a unit that is not a Multi-Unit Building is unusual
#: enough to say so. A twenty-person cabin is barracks by another name; it is
#: still allowed, because some of them really are.
BARRACKS_CAPACITY = 20


class HousingUnit(Document):
	def autoname(self):
		self.name = suffixed(str(self.unit_name or "").strip(), parcel_abbr(self.parcel))

	def validate(self):
		self.unit_name = str(self.unit_name or "").strip()
		if not self.unit_name:
			frappe.throw(_("Unit Name is required."))
		if not self.parcel:
			frappe.throw(_("Parcel is required — a building stands on ground somebody owns."))

		self.owning_entity = frappe.db.get_value("Parcel", self.parcel, "owning_entity") or self.owning_entity

		duplicate = frappe.db.get_value(
			"Housing Unit",
			{"unit_name": self.unit_name, "parcel": self.parcel, "name": ("!=", self.name or "")},
			"name",
		)
		if duplicate:
			frappe.throw(
				_(
					"Housing Unit {0} on {1} is already called {2}. One unit per name per "
					"parcel — every camp numbers its cabins from one, which is why the parcel "
					"is part of the docname and the name has to be unique inside it."
				).format(duplicate, self.parcel, self.unit_name),
				title=_("Duplicate Housing Unit"),
			)

		if float(self.square_footage or 0) < 0:
			frappe.throw(_("Square Footage cannot be negative."))
		if int(self.capacity or 0) < 0:
			frappe.throw(_("Capacity cannot be negative."))
		if int(self.max_occupants_per_or_law or 0) < 0:
			frappe.throw(_("Max Occupants per OR Law cannot be negative."))
		if self.year_built and int(self.year_built) < 0:
			frappe.throw(_("Year Built cannot be negative."))

		if not self.max_occupants_per_or_law:
			self.max_occupants_per_or_law = lawful_occupancy(self.square_footage)

		# v0.323.0: who moved the pin, when, and from where.
		from erpnext_mcp import pin_moves

		pin_moves.stamp_position(self)

	def on_update(self):
		# v0.322.0: every unit has its tag on the Asset Register, whichever door made it (Desk, MCP, phone) —
		# the map and asset lists read that register. Runs after insert as well. Never blocks the save.
		from erpnext_mcp import housing_assets

		result = housing_assets.ensure(self.name)
		if result.get("asset"):
			self.set(housing_assets.LINK, result["asset"])

	def on_trash(self):
		from erpnext_mcp import housing_assets

		housing_assets.retire(self.name, self.get(housing_assets.LINK))


def lawful_occupancy(square_footage) -> int:
	"""How many people the floor area allows, at 50 sq ft each.

	Rounds down, because the last partial fifty square feet is not a person. Zero
	square feet gives zero, which reads correctly as "nobody has measured it" —
	the tools report it as unknown rather than as a limit of none.
	"""
	return int(float(square_footage or 0) // SQ_FT_PER_OCCUPANT)
