# SPDX-License-Identifier: MIT
"""Controller for Field — one planted block, filed under the ground it is on.

THE DOCNAME IS `"<field name> - <parcel abbr>"`. Not the company's abbreviation:
a company has eight parcels and a parcel has eight blocks, and "Block 3 - HLD"
would be ambiguous eight ways over. The parcel's short key is what makes
`"Yellow Camp Block 3 - MC"` readable to somebody standing in it.

ACREAGE IS CHECKED AGAINST THE PARCEL, AND THAT IS THE ONE ARITHMETIC RULE HERE.
A parcel's blocks summing to more than the parcel is not a judgement about
somebody's records — it is two numbers that cannot both be true, and the one
place it always surfaces is a bad import. Everything softer than that is left
alone: blocks summing to *less* than the parcel is the normal case (roads,
ditches, headlands, the house), and a controller that complained about it would
complain about every real farm.

WHY THE COMPLIANCE FIELDS ARE ON THIS DOCTYPE. `last_spray_date` answers "can
the crew go in" before it answers anything an inspector asks;
`worker_hygiene_station_present` decides whether a block can legally be worked
at all. Removing either breaks the day's dispatch as surely as it breaks a WPS
report, which is the test for whether compliance is woven in or bolted on. A
separate "Field Compliance Log" would fail that test — nothing about picking
would stop if it disappeared.

THE BOUNDARY IS THE SAME KIND OF FIELD, AND THE STRONGEST EXAMPLE OF IT. A
polygon is what turns "sprayed Block 3" into something an auditor can check
against a GPS fix, and it is also what lets a crew's phone answer "am I in the
right block". Remove it and the WPS record loses its evidence AND the gate loses
its geofence. Everything derived from it — centroid, bounding box, H3 coverage,
computed area — is recomputed here on every save, because a derived figure a
person can edit independently is a figure that will disagree with the shape.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from erpnext_mcp import geo
from erpnext_mcp.abbr import parcel_abbr, suffixed

#: The one `organic_status` value that means the certificate is in hand. Named
#: rather than spelled out at the two places that compare against it, because a
#: block whose status is checked with a typo is a block that reports itself
#: conventional and looks fine.
CERTIFIED_ORGANIC = "Certified Organic"

#: How long a buyer-facing block ticker may be. Ten characters, because the
#: place it has to fit is a column on a printed settlement sheet, and a ticker
#: that has to be abbreviated by whoever types it is not the same ticker twice.
TICKER_MAX = 10

#: How many varieties one crop's catalogue is read for. A crop with more
#: cultivars than this is a data problem, not an orchard, in the same register
#: `Crop._variety_index` itself has no cap on because it is bounded by one
#: document's own child table — this reads the table directly instead.
_CROP_VARIETY_SCAN_CAP = 500


def _crop_variety_index(crop_name) -> dict:
	"""Casefolded variety name → the catalogue's own spelling, for one crop.

	Reads `Crop Variety` directly rather than loading the `Crop` document — the
	same "ask the table, not the parent" shape `_check_parcel_acreage` below uses
	for a field's siblings. Returns `{}` when no `Crop` record answers to
	`crop_name`, or that record names no varieties: the two cases
	`Field._check_varieties` treats as "nothing to check a spelling against."
	"""
	crop_name = str(crop_name or "").strip()
	if not crop_name or not frappe.db.exists("Crop", crop_name):
		return {}
	rows = frappe.db.get_all(
		"Crop Variety",
		filters={"parent": crop_name, "parenttype": "Crop"},
		pluck="variety_name",
		limit=_CROP_VARIETY_SCAN_CAP,
	)
	return {
		str(value).strip().casefold(): str(value).strip() for value in rows or [] if str(value or "").strip()
	}


class Field(Document):
	def autoname(self):
		self.name = suffixed(str(self.field_name or "").strip(), parcel_abbr(self.parcel))

	def validate(self):
		self.field_name = str(self.field_name or "").strip()
		if not self.field_name:
			frappe.throw(_("Field Name is required."))
		if not self.parcel:
			frappe.throw(_("Parcel is required — a block with no ground is not a field."))

		parcel = (
			frappe.db.get_value("Parcel", self.parcel, ["name", "owning_entity", "acreage"], as_dict=True)
			or {}
		)
		self.owning_entity = parcel.get("owning_entity") or self.owning_entity

		duplicate = frappe.db.get_value(
			"Field",
			{"field_name": self.field_name, "parcel": self.parcel, "name": ("!=", self.name or "")},
			"name",
		)
		if duplicate:
			frappe.throw(
				_(
					"Field {0} already records a block called {1} on {2}. One block per name "
					"per parcel — edit that one, or name this one so a crew can tell them apart."
				).format(duplicate, self.field_name, self.parcel),
				title=_("Duplicate Field"),
			)

		if float(self.acreage or 0) < 0:
			frappe.throw(_("Acreage cannot be negative."))
		if int(self.planting_density_per_acre or 0) < 0:
			frappe.throw(_("Planting Density cannot be negative."))

		self._check_parcel_acreage(parcel)
		self._check_block_ticker()
		self._check_varieties()
		self._derive_organic_certified()
		self._check_boundary()
		self._check_ndvi()

	def _check_varieties(self) -> None:
		"""Every `varieties` row must name this block's crop's own catalogue variety
		where that catalogue exists to check against, and the table cannot claim
		more than 100% of the block between its rows.

		`crop` HERE IS FREE TEXT, NOT A LINK to a `Crop` record — most blocks are
		registered before anybody has built the crop catalogue, and refusing every
		`varieties` row until it does would make this table unusable on exactly the
		farms that need it during onboarding. So the catalogue check applies ONLY
		when a `Crop` named `self.crop` exists AND lists at least one variety of its
		own; a block whose crop was never turned into a catalogue record keeps
		whatever spelling was typed, the same way `Field.crop` itself does.

		THE CATALOGUE'S OWN SPELLING IS WRITTEN BACK when a match is found, on the
		same reasoning `Crop._resolve_variety` writes it back onto an override row:
		so the stored row and the catalogue agree exactly, and a reader joining the
		two is a plain match rather than a second casefold.

		THE PERCENTAGE SUM IS THE SAME "CANNOT BOTH BE TRUE" RULE
		`_check_parcel_acreage` APPLIES TO ACREAGE, applied here to share of one
		block: more than 100% between the rows is not a judgement about anyone's
		records, it is an arithmetic impossibility. Less than 100 is the normal
		case — an unrecorded remainder, or simply not every row filled in.
		"""
		if not self.get("varieties"):
			return

		known = _crop_variety_index(self.crop)
		total_percentage = 0.0
		for index, row in enumerate(self.varieties, start=1):
			variety = str(row.get("variety") or "").strip()
			if not variety:
				frappe.throw(_("Row {0} of Varieties needs a variety.").format(index))
			if known:
				found = known.get(variety.casefold())
				if not found:
					frappe.throw(
						_(
							"Row {0} of Varieties names {1!r}, which is not among {2}'s own "
							"recorded varieties: {3}. Add it to the Crop's Varieties table first, "
							"or correct the spelling — a name the catalogue does not have is a "
							"row that looks recorded and links to nothing."
						).format(index, variety, self.crop, ", ".join(sorted(known.values()))),
						title=_("No Such Variety"),
					)
				row.variety = found
			else:
				row.variety = variety

			percentage = row.get("percentage")
			if percentage not in (None, ""):
				percentage = float(percentage)
				if not 0 <= percentage <= 100:
					frappe.throw(
						_("Row {0} of Varieties: Percentage of Block is {1}. It runs 0 to 100.").format(
							index, percentage
						)
					)
				total_percentage += percentage

		if total_percentage > 100.0001:
			frappe.throw(
				_(
					"The Varieties table totals {0}% of this block, and a block cannot be more "
					"than 100% covered. Either one row is overstated or the split between them "
					"is wrong."
				).format(round(total_percentage, 2)),
				title=_("Variety Percentage Exceeds 100%"),
			)

	def _check_block_ticker(self) -> None:
		"""Normalise the buyer-facing ticker, and refuse a second block claiming it.

		UNIQUE ACROSS THE COMPANY, not across the site and not across the parcel.
		Across the site is wrong because two entities on one bench are two
		businesses, and Highland's 'YC-3' is no business of Meadow's. Across the
		parcel is wrong for the opposite reason: the ticker's whole purpose is that
		a buyer can say it without knowing which parcel the block sits on, and two
		'YC-3's under one company make that sentence ambiguous exactly where it is
		being relied on.

		FOLDED TO UPPER CASE ON SAVE, and that is the substantive decision here. A
		ticker is copied off a purchase order by hand and typed back on a settlement
		by somebody else; 'yc-3' and 'YC-3' reaching this check as different strings
		would let both exist, and the duplicate would be discovered by a buyer
		receiving the wrong block's fruit rather than by this controller.

		EMPTY IS ALWAYS ALLOWED and is the normal state. Most blocks are never sold
		by name. The uniqueness check therefore runs only on a ticker that is set —
		treating '' as a value would let the first untickered block lock out every
		other one.
		"""
		ticker = str(self.block_ticker or "").strip().upper()
		self.block_ticker = ticker
		if not ticker:
			return

		if len(ticker) > TICKER_MAX:
			frappe.throw(
				_(
					"Block Ticker {0!r} is {1} characters. A ticker has to fit the column it is "
					"printed in — keep it to {2}."
				).format(ticker, len(ticker), TICKER_MAX),
				title=_("Block Ticker Too Long"),
			)

		filters = {"block_ticker": ticker, "name": ("!=", self.name or "")}
		if self.owning_entity:
			filters["owning_entity"] = self.owning_entity
		duplicate = frappe.db.get_value("Field", filters, "name")
		if duplicate:
			frappe.throw(
				_(
					"Block Ticker {0!r} is already on {1}. A ticker is what a buyer puts on an "
					"order, so two blocks answering to it under one company means somebody gets "
					"the wrong fruit. Give this block its own, or clear the other one first."
				).format(ticker, duplicate),
				title=_("Duplicate Block Ticker"),
			)

	def _derive_organic_certified(self) -> None:
		"""Rewrite the organic flag from the status, every save, without exception.

		THE SAME RULE AS THE BOUNDARY, for the same reason. `organic_certified` is
		read-only in the Desk and recomputed here, so it cannot drift from the
		status it comes from — a farm that ticks the box and leaves the status on
		Transitional has two answers to "are these acres certified" and the survey
		line sums the wrong one.

		Nothing else is refused. An agency named on a Conventional block is a
		contradiction worth reporting and not worth blocking: a block mid-application
		genuinely has a certifier and no certificate, and a controller that threw
		would make the honest record the unsaveable one. `list_fields` and
		`create_field` say so in their warnings instead.
		"""
		self.organic_certified = 1 if self.organic_status == CERTIFIED_ORGANIC else 0

	def _check_parcel_acreage(self, parcel: dict) -> None:
		"""Refuse blocks that between them are bigger than the ground they are on.

		Reported with both numbers and the shortfall, because the useful next
		question is "which of these two is wrong" and neither figure alone
		answers it. A parcel with no acreage recorded is not checked — an unknown
		is not a zero, and treating it as one would refuse every field on a
		parcel somebody has not measured yet.
		"""
		limit = float(parcel.get("acreage") or 0)
		if limit <= 0:
			return
		siblings = frappe.db.get_all(
			"Field",
			filters={"parcel": self.parcel, "name": ("!=", self.name or "")},
			fields=["name", "acreage"],
			limit=500,
		)
		others = sum(float(row.get("acreage") or 0) for row in siblings)
		total = round(others + float(self.acreage or 0), 2)
		if total > round(limit, 2):
			frappe.throw(
				_(
					"{0} is {1} acres, and its blocks would total {2} — {3} more than the "
					"parcel. Either the parcel's acreage is understated or one of the blocks "
					"is overstated; both cannot be right."
				).format(self.parcel, round(limit, 2), total, round(total - limit, 2)),
				title=_("Field Acreage Exceeds Parcel"),
			)

	def _check_boundary(self) -> None:
		"""Validate the polygon and rewrite everything derived from it.

		The structural checks — valid JSON, a Polygon or MultiPolygon, closed
		rings, coordinates on Earth — need no third-party library and therefore
		always run, so a bad boundary is refused on any site. The geometric ones
		(self-intersection, area, centroid, H3) need shapely and h3; where those
		are absent the shape is stored as given and the derived fields are left
		alone rather than being silently zeroed, because a zero centroid is a
		coordinate in the Gulf of Guinea and reads like an answer.
		"""
		if not str(self.boundary_geojson or "").strip():
			return
		geometry = geo.parse(self.boundary_geojson)
		if not geo.available():
			return
		derived = geo.derive(geometry)
		derived.pop("shape", None)
		for fieldname, value in derived.items():
			self.set(fieldname, value)

		_ratio, verdict = geo.area_disagreement(self.acreage, self.area_computed_acres)
		if verdict == "refuse":
			frappe.throw(
				_(
					"The boundary encloses {0} acres and this block is recorded as {1}. That is "
					"not a survey disagreement — one of the two is about a different piece of "
					"ground. Fix whichever is wrong before saving."
				).format(self.area_computed_acres, round(float(self.acreage or 0), 2)),
				title=_("Boundary Disagrees With Acreage"),
			)

	def _check_ndvi(self) -> None:
		"""NDVI is an index from -1 to 1, and a stored value outside that is noise."""
		for fieldname in ("last_ndvi_mean",):
			value = self.get(fieldname)
			if value in (None, ""):
				continue
			if not geo.NDVI_MIN <= float(value) <= geo.NDVI_MAX:
				frappe.throw(
					_("{0} is {1}. NDVI runs from {2} to {3}.").format(
						fieldname, value, geo.NDVI_MIN, geo.NDVI_MAX
					)
				)
		if self.last_ndvi_stddev not in (None, "") and float(self.last_ndvi_stddev) < 0:
			frappe.throw(_("A standard deviation cannot be negative."))
