# SPDX-License-Identifier: MIT
"""Controller for Satellite Metric — the small permanent thing a big download leaves behind.

WHY THE NUMBER IS KEPT AND THE IMAGE IS NOT. A Sentinel-2 raster for one block is
megabytes, is re-downloadable from the provider's archive for years, and is
useful for about as long as it takes to compute a mean from it. The mean is
eight bytes, is not re-derivable once the farm stops paying for the pull, and is
what every downstream question actually reads: the anomaly detector compares two
of these, the season chart plots a year of them, and the irrigation argument
cites one. So this doctype holds the numbers, and the imagery stays where
imagery belongs.

THE COMPANY IS COPIED FROM THE BLOCK AT WRITE TIME, exactly as `IoT Reading`
copies it from the device, and for the same reason: a block sold or transferred
in July must not retroactively move June's readings onto the buyer's books. What
company a reading belonged to is a fact about the past.

`indexed_value` IS DERIVED HERE AND NEVER TYPED. It is the raw value rescaled
0-100 against the index's own physical range, and it exists because a relative
comparison made on raw NDVI is unsound near zero and meaningless across it —
0.05 to 0.01 is an 80% "drop" on bare ground where nothing happened. Deriving it
on save rather than at read time means a stored series is comparable without
every reader knowing which index it is looking at. A caller that supplies one is
overridden, because two fields that can disagree will.

DUPLICATES ARE REFUSED ON (field, metric_type, timestamp, h3_index). A scheduler
that retried, a backfill that overlapped a forward sweep, and an operator
re-running a pull by hand all produce the same pass twice, and a doubled reading
does not look wrong — it looks like a slightly noisier series. The h3 cell is
part of the key because a whole-block mean and a per-hex mean for the same pass
are different readings of the same moment, and both are legitimate.
"""

import frappe
from frappe import _
from frappe.model.document import Document

#: The physical range of each index, for the 0-100 rescale. Mirrors
#: `erpnext_mcp.satellite.METRICS`; kept here as a plain literal so the controller
#: does not import the tools layer on every save.
RAW_RANGES = {
	"ndvi": (-1.0, 1.0),
	"evi": (-1.0, 1.0),
	"ndre": (-1.0, 1.0),
	"ndwi": (-1.0, 1.0),
	"ndmi": (-1.0, 1.0),
	"savi": (-1.5, 1.5),
}

#: How far ahead of the server's clock an acquisition may be stamped before it is
#: refused. A satellite pass dated next week is a clock or a timezone that was
#: never set, and it would sit at the top of every "latest reading" query for ever.
FUTURE_TOLERANCE_SECONDS = 900


class SatelliteMetric(Document):
	def validate(self):
		self.source = str(self.source or "").strip()
		self.h3_index = str(self.h3_index or "").strip()

		if not self.field:
			frappe.throw(
				_("Block is required — a satellite metric with no block is a number with no ground.")
			)
		if not self.timestamp:
			frappe.throw(
				_(
					"Acquired At is required and is deliberately not defaulted to now. A backfill "
					"run in August writes rows for passes that happened in May, and stamping them "
					"with the run time is the one date they did not happen."
				)
			)
		if self.value is None:
			frappe.throw(_("Raw Value is required. A metric row with no reading is not a reading."))

		self._copy_from_block()
		self._check_range()
		self._derive_index()
		self._check_timestamp()
		self._check_duplicate()

	def _copy_from_block(self) -> None:
		"""Take the books off the block now, and keep them. See the docstring."""
		if self.company:
			return
		self.company = frappe.db.get_value("Field", self.field, "owning_entity")

	def _check_range(self) -> None:
		"""A reading outside its index's physical range is a decode that went wrong.

		Refused rather than clamped: a UINT16 raster decoded with the wrong range,
		or a percentage stored where an index belongs, produces numbers like 87 in
		a column that runs -1 to 1. Clamping would file it as a very healthy block.
		"""
		low, high = RAW_RANGES.get(self.metric_type, (-1.0, 1.0))
		value = float(self.value)
		if not low <= value <= high:
			frappe.throw(
				_(
					"{0} is outside the range {1} runs in ({2} to {3}). That is a decode with the "
					"wrong scale or a percentage in an index column, not a reading — and clamped "
					"it would file as a very healthy block."
				).format(value, self.metric_type, low, high),
				title=_("Reading Outside The Index"),
			)

	def _derive_index(self) -> None:
		low, high = RAW_RANGES.get(self.metric_type, (-1.0, 1.0))
		self.indexed_value = round((float(self.value) - low) / (high - low) * 100.0, 3)

	def _check_timestamp(self) -> None:
		ahead = frappe.utils.time_diff_in_seconds(str(self.timestamp), frappe.utils.now())
		if ahead > FUTURE_TOLERANCE_SECONDS:
			frappe.throw(
				_(
					"This pass is stamped {0}, which is {1} minutes ahead of the server. A "
					"satellite cannot have flown over yet, and the row would sort above every "
					"real reading for ever."
				).format(self.timestamp, int(ahead // 60)),
				title=_("Acquisition From The Future"),
			)

	def _check_duplicate(self) -> None:
		duplicate = frappe.db.get_value(
			"Satellite Metric",
			{
				"field": self.field,
				"metric_type": self.metric_type,
				"timestamp": self.timestamp,
				"h3_index": self.h3_index or "",
				"name": ("!=", self.name or ""),
			},
			"name",
		)
		if duplicate:
			frappe.throw(
				_(
					"{0} already holds {1} for this block at {2}. A backfill overlapping a forward "
					"sweep produces the same pass twice, and a doubled reading does not look "
					"wrong — it looks like a slightly noisier series."
				).format(duplicate, self.metric_type, self.timestamp),
				title=_("Duplicate Pass"),
			)
