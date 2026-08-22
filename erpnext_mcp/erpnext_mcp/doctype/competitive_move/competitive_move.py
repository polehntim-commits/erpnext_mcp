# SPDX-License-Identifier: MIT
"""Controller for Competitive Move — what somebody did, and what was done about it.

THE OBSERVED DATE MAY NOT BE IN THE FUTURE, and that is the only date rule that
is absolute. A move somebody expects is not an observation, and letting one in
would put speculation in the same register as fact — which is exactly the
register somebody consults to decide whether a pattern is real.

A RESPONSE DATE WITHOUT A RESPONSE IS REFUSED. The pair is the point of the
second half of this record: the gap between what was recommended and what was
actually done is the most instructive thing in the file, and a date with nothing
attached reads as a response that happened and cannot be read back.

NOTHING ELSE IS REFUSED. A move with no impact figures, no recommendation and no
outcome is an ordinary sighting on the day it was seen, and a controller that
wanted the whole story up front would ensure the sighting was never written down.
"""

import frappe
from frappe import _
from frappe.model.document import Document

LOW = "Low"
MEDIUM = "Medium"
HIGH = "High"
SEVERITIES = (LOW, MEDIUM, HIGH)
CONFIDENCES = (LOW, MEDIUM, HIGH)

NO_ACTION = "No Action"
MONITOR = "Monitor"
RESPOND = "Respond"
URGENT = "Urgent"
URGENCIES = (NO_ACTION, MONITOR, RESPOND, URGENT)


class CompetitiveMove(Document):
	def validate(self):
		self.description = str(self.description or "").strip()
		if not self.description:
			frappe.throw(_("Description is required — a move nobody described cannot be read back."))
		if not self.market_participant:
			frappe.throw(_("Market Participant is required. A move with no mover is an anecdote."))
		if not self.observed_date:
			frappe.throw(_("Observed On is required."))

		if self.severity not in SEVERITIES:
			self.severity = MEDIUM
		if self.confidence not in CONFIDENCES:
			self.confidence = MEDIUM
		if self.response_urgency not in URGENCIES:
			self.response_urgency = MONITOR

		if str(self.observed_date) > frappe.utils.today():
			frappe.throw(
				_(
					"Observed On is {0}, which is in the future. A move somebody expects is not "
					"an observation, and this register is what says whether a pattern is real."
				).format(self.observed_date),
				title=_("Observation In The Future"),
			)

		if self.response_date and not str(self.actual_response or "").strip():
			frappe.throw(
				_(
					"A response date was recorded with no actual response. The gap between what "
					"was recommended and what was done is the point of this half of the record — "
					"a date with nothing attached closes it without saying what happened."
				),
				title=_("Undescribed Response"),
			)
		if self.response_date and str(self.response_date) < str(self.observed_date):
			frappe.throw(
				_("The response ({0}) is dated before the move was observed ({1}).").format(
					self.response_date, self.observed_date
				)
			)
