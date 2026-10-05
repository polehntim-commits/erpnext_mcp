# SPDX-License-Identifier: MIT
"""Controller for Farm Shift — the crew, the period, and the two facts that decide status.

WHAT IT DERIVES. The foreman's name and the company are snapshotted off the
Employee record on first save and never rewritten, for the same reason
`Employee Training Record` snapshots the farm name: a lookup at report time
answers with today's answer, and an inspector reading last season's shifts is
entitled to who the foreman was THEN. `status` is recomputed on every save from
`end_datetime` and `cancelled`, in that order — no end time means the shift is
still running whatever anybody ticked, because an open shift is what the v0.19.4
weather sweep walks and a shift marked Closed with no end would drop out of the
fetch while still being worked.

WHAT IT REFUSES, AND THE THREE ARE ALL THE SAME MISTAKE. A shift that ends before
it starts. A crew member who left before they joined. A crew member whose
`left_at` is after the shift itself ended. Every one of them is two timestamps
transposed, every one of them produces an Attendance row with a negative span
when the shift closes, and a negative span on a wage record is the kind of error
that gets found by the person disputing the wage.

Also refused: the same Employee twice on one crew. Not fussiness — the Attendance
bridge writes one row per crew row, so a duplicate is a duplicated day on
somebody's payroll register, and the duplicate is invisible on the shift form
where the two rows sit next to each other looking deliberate.

WHAT IT MERELY RECORDS. A shift with no location, no GPS, no crew at all. A
foreman opening a shift at five in the morning before the crew has arrived is
the ordinary case rather than an error, and a controller that demanded a crew
would push people into filing the shift afterwards from memory — which is the
one thing a contemporaneous record must not become.

THE CANCELLATION REASON IS REQUIRED AND THE CANCELLATION IS NOT. A shift can be
called off; a bare Cancelled tick with nothing beside it is a gap somebody will
be asked about, and 'crew stood down at 06:40, heat index already 94 °F' is the
answer. Same argument as `revoke_mobile_user`'s reason: the flag is reconstructable
and the sentence is not.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from erpnext_mcp import shifts

#: How far from a compliance event a weather reading may be and still describe
#: it. Half an hour, because the sweep writes one reading every fifteen minutes
#: and the archive writes one an hour: thirty minutes reaches the neighbouring
#: live reading in every direction and reaches an hourly backfill in one. Past it
#: the honest answer is a blank column — a temperature from an hour away is not
#: evidence about this moment, and a snapshot nobody can defend is worse than
#: none, because it is the number an inspector will quote back.
WEATHER_SNAPSHOT_WINDOW_SECONDS = 30 * 60


def _nearest_reading(timeline: list, when: str):
	"""The reading that was current at `when`, preferring earlier over later.

	`timeline` is already sorted by `reading_datetime`. Walks it once: the last
	reading at or before the instant wins outright, and the first one after it is
	the fallback for an event logged before the shift's first fetch landed. Both
	are bounded by `WEATHER_SNAPSHOT_WINDOW_SECONDS`.
	"""
	before = None
	after = None
	for row in timeline:
		stamp = str(row.get("reading_datetime") or "")
		if stamp <= str(when):
			before = row
		else:
			after = row
			break
	for candidate in (before, after):
		if candidate is None:
			continue
		try:
			gap = abs(
				float(frappe.utils.time_diff_in_seconds(str(when), str(candidate.get("reading_datetime"))))
			)
		except Exception:
			continue
		if gap <= WEATHER_SNAPSHOT_WINDOW_SECONDS:
			return candidate
	return None


class FarmShift(Document):
	def autoname(self):
		"""SHIFT-YYYY-0001, where YYYY is the year the shift STARTED.

		Not the year it was filed. A night shift that began on 31 December belongs
		to the year it began, and a series keyed off `today()` would file it under
		the following one for ever.
		"""
		year = str(self.start_datetime or frappe.utils.now())[:4]
		self.name = shifts.next_in_series(shifts.DOCTYPE, "SHIFT", year)

	def validate(self):
		self._require_the_two_facts()
		self._fill_from_the_foreman()
		self._check_the_period()
		self._check_the_crew()
		# v0.251.0. A reviewed punch is locked for payroll (`time_review`).
		from erpnext_mcp import time_review

		time_review.check_locked_rows(self)
		self._check_the_cancellation()
		self._denormalise_the_weather()
		self.status = shifts.status_for(self.end_datetime, self.cancelled)

	# ── the parts ───────────────────────────────────────────────────────────
	def _require_the_two_facts(self) -> None:
		if not self.foreman:
			frappe.throw(
				_(
					"A shift needs a foreman. OAR 437-004-1131 puts the water, shade, rest-cycle "
					"and observation obligations on a named responsible person, and FSMA "
					"§112.161(b) asks that person to sign — a shift with nobody's name on it "
					"cannot answer either."
				),
				title=_("No Responsible Party"),
			)
		if not self.start_datetime:
			frappe.throw(
				_(
					"A shift needs a start time. It is the left edge of the exposure period every "
					"compliance question about this shift is asked against."
				)
			)

	def _fill_from_the_foreman(self) -> None:
		row = frappe.db.get_value("Employee", self.foreman, ["employee_name", "company"], as_dict=True) or {}
		if not str(self.foreman_name or "").strip():
			self.foreman_name = row.get("employee_name") or self.foreman
		if not self.company:
			self.company = row.get("company")

	def _check_the_period(self) -> None:
		if self.end_datetime and shifts.to_the_second(self.end_datetime) < shifts.to_the_second(
			self.start_datetime
		):
			frappe.throw(
				_(
					"This shift ends at {0} and starts at {1} — it would have finished before it "
					"began. Two timestamps transposed, almost certainly."
				).format(self.end_datetime, self.start_datetime),
				title=_("Ends Before It Starts"),
			)

	def _check_the_crew(self) -> None:
		"""Every crew row's span, and no employee twice.

		The duplicate check is the one worth defending. Two rows for one person
		look deliberate on the form — they sit next to each other and somebody
		reads them as two spells of work — but the Attendance bridge writes one
		row per crew row, so the shift close produces two attendance days for one
		person and the payroll register carries the error rather than the shift.
		A worker who genuinely left and came back is one row whose span covers
		both, plus a note saying so; that is less precise and it is not wrong.
		"""
		seen = {}
		for row in self.crew or []:
			if not row.get("employee"):
				frappe.throw(
					_(
						"Every crew row needs an Employee. A name with no record behind it "
						"cannot be reconciled to a payroll register."
					)
				)
			if not row.get("joined_at"):
				row.joined_at = self.start_datetime
			if not row.get("employee_name"):
				row.employee_name = (
					frappe.db.get_value("Employee", row.employee, "employee_name") or row.employee
				)
			if shifts.to_the_second(row.joined_at) < shifts.to_the_second(self.start_datetime):
				frappe.throw(
					_(
						"{0} is recorded as joining at {1}, before the shift started at {2}. A crew "
						"member cannot be on a shift that has not begun."
					).format(row.employee_name or row.employee, row.joined_at, self.start_datetime),
					title=_("Joined Before the Shift"),
				)
			if row.get("left_at"):
				if shifts.to_the_second(row.left_at) < shifts.to_the_second(row.joined_at):
					frappe.throw(
						_(
							"{0} is recorded as leaving at {1} and joining at {2} — a negative span, "
							"which becomes a negative Attendance row when this shift closes."
						).format(row.employee_name or row.employee, row.left_at, row.joined_at),
						title=_("Left Before Joining"),
					)
				if self.end_datetime and shifts.to_the_second(row.left_at) > shifts.to_the_second(
					self.end_datetime
				):
					frappe.throw(
						_(
							"{0} is recorded as leaving at {1}, after the shift itself ended at {2}. "
							"Nobody is on a shift that is over."
						).format(row.employee_name or row.employee, row.left_at, self.end_datetime),
						title=_("Left After the Shift"),
					)
			if row.employee in seen:
				frappe.throw(
					_(
						"{0} is on this crew twice. Two rows look deliberate on this form and become "
						"two Attendance days for one person when the shift closes — a worker who "
						"left and came back is ONE row spanning both, with a note saying so."
					).format(row.employee_name or row.employee),
					title=_("On the Crew Twice"),
				)
			seen[row.employee] = True

	def _denormalise_the_weather(self) -> None:
		"""Copy the conditions at each compliance event's own instant onto its row.

		v0.19.4. THE SNAPSHOT IS A READING CONVENIENCE AND AN AUDIT NECESSITY. An
		inspector reads the event row — "water break at 09:15" — and a reader who
		has to work out which of ninety timeline readings was current at 09:15
		will not do it. So the row carries the number, denormalised, and the
		timeline behind it is what proves the number.

		EARLIER BEATS LATER, WITHIN HALF AN HOUR. The reading that was current at
		09:15 is the most recent one taken AT OR BEFORE 09:15 — that is the
		conditions the foreman was standing in when they called the break. A
		reading taken afterwards is used only when there is nothing before, which
		is the ordinary case for an event logged in the first minutes of a shift,
		and it is still the closest true statement available. Past thirty minutes
		nothing is copied at all: a temperature from an hour away is not evidence
		about this moment, and a blank column is honest where a stale one is not.

		IT NEVER OVERWRITES A VALUE SOMEBODY ENTERED. A foreman with a thermometer
		at the block knows something Open-Meteo's grid square does not, and a
		manual figure is the better evidence of the two. `log_shift_event` accepts
		both snapshot fields as arguments precisely so that can be recorded.

		THIS IS A CONTROLLER AND NOT A `doc_events` HOOK, deliberately. `hooks.py`
		promises this app installs no document hooks and `test_hooks.py` forbids
		the key by name, because a hook is how an app changes the behaviour of a
		doctype it does not own. Farm Shift IS this app's own doctype, so the rule
		belongs in its controller — where it also runs on a Desk edit, which a
		child-table hook would not reliably do.
		"""
		events = self.get("compliance_events") or []
		readings = self.get("weather_timeline") or []
		if not events or not readings:
			return
		timeline = sorted(
			(row for row in readings if str(row.get("reading_datetime") or "").strip()),
			key=lambda row: str(row.get("reading_datetime")),
		)
		if not timeline:
			return
		for event in events:
			when = str(event.get("event_datetime") or "").strip()
			if not when:
				continue
			if event.get("weather_snapshot_temp_f") not in (None, "") or event.get(
				"weather_snapshot_heat_index_f"
			) not in (None, ""):
				continue
			match = _nearest_reading(timeline, when)
			if match is None:
				continue
			event.weather_snapshot_temp_f = match.get("temp_f")
			event.weather_snapshot_heat_index_f = match.get("heat_index_f")

	def _check_the_cancellation(self) -> None:
		if frappe.utils.cint(self.cancelled) and not str(self.cancellation_reason or "").strip():
			frappe.throw(
				_(
					"A cancelled shift needs a reason. 'Crew stood down at 06:40, heat index "
					"already 94 °F' is a record; a bare Cancelled tick is a gap somebody will be "
					"asked about, and the flag is the part anybody could reconstruct."
				),
				title=_("Cancelled Without a Reason"),
			)
