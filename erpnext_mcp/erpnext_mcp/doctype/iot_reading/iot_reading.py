# SPDX-License-Identifier: MIT
"""Controller for IoT Reading — one number, and the context needed to read it later.

THE FIELD AND THE COMPANY ARE COPIED FROM THE DEVICE HERE, AT WRITE TIME, and
never resolved again. It looks like denormalisation for its own sake and it is
not: a probe moved from Block 3 to Block 7 in July would, under a resolved link,
retroactively move every June reading to Block 7 — and the June irrigation
decisions those readings justified would no longer be defensible from the
record. What block a reading came from is a fact about the past.

THE TIMESTAMP IS THE DEVICE'S, NOT THE SERVER'S. A device that buffered while
its radio was down posts a backlog hours later, and those buffered readings are
exactly the ones somebody wants during a frost event. So the timestamp is
required and is never defaulted to now: a reading whose time nobody knows is a
reading nobody can put in a series.

DUPLICATES ARE REFUSED ON (device, reading_type, timestamp), which is the same
constraint the SQLite table this replaced carried. Devices retry, gateways
replay, and a batch posted twice must not double every average computed off it.
"""

import frappe
from frappe import _
from frappe.model.document import Document

GOOD = "Good"
SUSPECT = "Suspect"
ERROR = "Error"
QUALITIES = (GOOD, SUSPECT, ERROR)

#: How far ahead of the server's clock a device's timestamp may be before it is
#: refused. Fifteen minutes: a field device's clock drifts and a little skew is
#: ordinary, but a reading dated next Tuesday is a clock that was never set, and
#: it would sit at the top of every "latest reading" query for ever.
FUTURE_TOLERANCE_SECONDS = 900


class IoTReading(Document):
	def validate(self):
		self.unit = str(self.unit or "").strip()

		if not self.device:
			frappe.throw(_("Device is required — a reading from nothing cannot be judged."))
		if not self.timestamp:
			frappe.throw(
				_(
					"Timestamp is required and is deliberately not defaulted to now. A device "
					"posting a buffered backlog would have every reading in it stamped with the "
					"moment the radio came back, which is the one time they did not happen."
				)
			)

		self._copy_from_device()

		if self.quality not in QUALITIES:
			self.quality = GOOD

		if not self.unit:
			frappe.throw(
				_(
					"Unit is required. The reading type implies one, but a device reporting "
					"Fahrenheit into an air_temp_c column is a real and quiet failure, and this "
					"is the only column it shows up in."
				)
			)

		self._check_timestamp()
		self._check_duplicate()

	def _copy_from_device(self) -> None:
		"""Take the block and the books off the device now, and keep them."""
		row = (
			frappe.db.get_value("IoT Device", self.device, ["field", "company", "enabled"], as_dict=True)
			or {}
		)
		if not row:
			frappe.throw(_("IoT Device {0} does not exist.").format(self.device))
		if not self.field:
			self.field = row.get("field")
		if not self.company:
			self.company = row.get("company")

	def _check_timestamp(self) -> None:
		ahead = frappe.utils.time_diff_in_seconds(str(self.timestamp), frappe.utils.now())
		if ahead > FUTURE_TOLERANCE_SECONDS:
			frappe.throw(
				_(
					"This reading is stamped {0}, which is {1} minutes ahead of the server. That "
					"is a device clock that was never set, not a measurement — and it would sort "
					"above every real reading for ever."
				).format(self.timestamp, int(ahead // 60)),
				title=_("Reading From The Future"),
			)

	def _check_duplicate(self) -> None:
		duplicate = frappe.db.get_value(
			"IoT Reading",
			{
				"device": self.device,
				"reading_type": self.reading_type,
				"timestamp": self.timestamp,
				"name": ("!=", self.name or ""),
			},
			"name",
		)
		if duplicate:
			frappe.throw(
				_(
					"{0} already holds this device's {1} at {2}. Devices retry and gateways "
					"replay; storing it twice would double every average computed off it."
				).format(duplicate, self.reading_type, self.timestamp),
				title=_("Duplicate Reading"),
			)
