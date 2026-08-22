# SPDX-License-Identifier: MIT
"""Controller for IoT Device — the box, and whether its numbers can be trusted.

THE HARDWARE ID IS THE REAL KEY AND IT IS UNIQUE ACROSS THE SITE. Not per
company, because one physical box cannot be in two orchards at once: a duplicate
hardware id is either a typo or a device somebody moved between entities without
re-registering it, and both are worth stopping. The docname is a series because
a MAC address is not a thing anybody wants to type into a filter.

`last_seen` IS NEVER WRITTEN HERE AND THAT IS THE POINT. It is set by the path
that accepts readings, so it always means "this device actually spoke". A
last_seen somebody could type is the one field that would make a dead sensor look
alive, which is the exact failure this column exists to catch — a soil probe
silent for a week is not reading zero moisture, it is not reading, and a block
gets irrigated or does not on the difference.

WHAT IS REFUSED IS THE VALUE THAT CANNOT BE TRUE. A battery over 100% or below
zero, a config that is not JSON. Everything softer is left alone: a device with
no field, no zone and no calibration date is an ordinary new registration.
"""

import json

import frappe
from frappe import _
from frappe.model.document import Document

SENSOR = "Sensor"
ACTUATOR = "Actuator"
GATEWAY = "Gateway"

#: How stale `last_seen` may be before a device is reported offline. Ten minutes
#: matches the farm_app convention this replaced; it is generous for a mains
#: gateway and about right for a battery probe on a fifteen-minute duty cycle.
ONLINE_WINDOW_SECONDS = 600


class IoTDevice(Document):
	def validate(self):
		self.device_name = str(self.device_name or "").strip()
		self.hardware_id = str(self.hardware_id or "").strip()
		self.zone = str(self.zone or "").strip()

		if not self.device_name:
			frappe.throw(_("Device Name is required."))
		if not self.hardware_id:
			frappe.throw(
				_(
					"Hardware ID is required — it is the only identifier the device itself "
					"knows, and without it a reading arriving from the field cannot be matched "
					"to a record."
				)
			)

		duplicate = frappe.db.get_value(
			"IoT Device", {"hardware_id": self.hardware_id, "name": ("!=", self.name or "")}, "name"
		)
		if duplicate:
			frappe.throw(
				_(
					"Hardware ID {0} is already registered as {1}. One physical device cannot be "
					"in two places — if this box was moved, edit that record rather than opening "
					"a second one, or its readings will split across both."
				).format(self.hardware_id, duplicate),
				title=_("Duplicate Hardware ID"),
			)

		if self.device_class not in (SENSOR, ACTUATOR, GATEWAY):
			self.device_class = SENSOR

		if self.battery_level not in (None, ""):
			level = float(self.battery_level)
			if not 0 <= level <= 100:
				frappe.throw(_("Battery Level is {0}%. A percentage runs from 0 to 100.").format(level))

		self._check_config()

	def _check_config(self) -> None:
		"""A config that is not JSON is a config nothing downstream can read.

		Refused rather than stored, because the failure otherwise surfaces on the
		day somebody's calibration coefficients are needed and the parse throws
		inside whatever was reading them — which is a long way from the person who
		typed it.
		"""
		raw = str(self.device_config or "").strip()
		if not raw:
			return
		try:
			json.loads(raw)
		except ValueError as exc:
			frappe.throw(
				_("Device Config is not valid JSON: {0}").format(exc),
				title=_("Malformed Device Config"),
			)

	def is_online(self, now: str = "") -> bool:
		"""Whether this device has spoken recently enough to be believed.

		Computed and never stored. A stored online flag is wrong the moment the
		device goes quiet and nothing writes to it, which is precisely the moment
		it matters.
		"""
		if not self.last_seen:
			return False
		reference = now or frappe.utils.now()
		return frappe.utils.time_diff_in_seconds(reference, self.last_seen) <= ONLINE_WINDOW_SECONDS
