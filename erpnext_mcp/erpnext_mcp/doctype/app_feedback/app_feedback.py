# SPDX-License-Identifier: MIT
"""Controller for App Feedback — one note a worker wrote from inside the app.

THE CONTROLLER OWNS `has_screenshot`, AND A CALLER NEVER DOES. The flag exists
so an owner can filter a feed down to the notes that came with a picture, and a
flag a client sets is a flag that disagrees with the file the moment a screenshot
fails to upload — which is exactly the note somebody would then never find. It is
recomputed from `screenshot` on every save, so the column and the attachment
cannot drift apart, and `submit_app_feedback` sets neither.

THE YEAR IN THE SERIES IS THE SUBMISSION'S YEAR, not the arrival's, and that is
the whole reason `autoname` is here rather than left to the `naming_series:`
hook. Frappe expands `.YYYY.` from the date of the INSERT, and this register is
the one where those two dates routinely differ by weeks: a phone with no signal
in the blocks holds a note until it finds the yard's wifi. A note written on 31
December and drained on 2 January belongs to the season it was written in, so
the series is expanded by `shifts.next_in_series` off `timestamp` — the same
function and the same argument Bucket Log Entry and Farm Shift use, so a docname
is the same string on a bench, in a patch and in the standalone suite.

WHY NOTHING HERE VALIDATES THE IDENTITY FIELDS. `employee`, `employee_name` and
`user` are written by `tools/app_feedback.py` from the authenticated caller and
are read-only on the form; `role`, `designation` and `claimed_employee` are the
handset's own claims and are stored as sent. A controller that checked a claim
against the site would either refuse a note over a shared handset's honest
mismatch, or quietly rewrite what somebody reported — and the mismatch is the
fact worth keeping. See the doctype's own field descriptions.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from erpnext_mcp import shifts

DOCTYPE = "App Feedback"

#: The longest note this record will hold. A worker typing or dictating in a row
#: does not produce ten kilobytes, and the cap is here so a client bug cannot put
#: a log file in a text column somebody has to read.
MAX_FEEDBACK = 8000


class AppFeedback(Document):
	def autoname(self):
		year = str(self.timestamp or frappe.utils.today())[:4]
		self.name = shifts.next_in_series(DOCTYPE, "AFB", year, width=5)

	def validate(self):
		self.entry_uuid = str(self.entry_uuid or "").strip()
		if not self.entry_uuid:
			frappe.throw(
				_(
					"entry_uuid is required. It is what stops a queued note being filed twice "
					"when the handset drains its backlog, and a record without one cannot be "
					"deduplicated by anything."
				)
			)

		self.feedback_text = str(self.feedback_text or "").strip()
		if not self.feedback_text:
			frappe.throw(_("There is no note here. Feedback is the one thing the worker typed."))
		if len(self.feedback_text) > MAX_FEEDBACK:
			frappe.throw(
				_("The note is {0} characters; the maximum is {1}.").format(
					len(self.feedback_text), MAX_FEEDBACK
				)
			)

		self.screen_name = str(self.screen_name or "").strip()
		self.role = str(self.role or "").strip()

		if self.employee and not self.employee_name:
			self.employee_name = frappe.db.get_value("Employee", self.employee, "employee_name") or None

		# The claim is only worth storing where it disagrees with what the login
		# proved. Two identical columns on every row would make the disagreement
		# — the shared handset, which is the thing worth seeing — invisible.
		if self.claimed_employee and self.claimed_employee == self.employee:
			self.claimed_employee = None
			self.claimed_employee_name = None

		if not self.timestamp:
			self.timestamp = frappe.utils.now()
		if not self.received_at:
			self.received_at = frappe.utils.now()

		# Recomputed rather than trusted — see the module docstring.
		self.has_screenshot = 1 if str(self.screenshot or "").strip() else 0

	# ── triage, from the Desk ── v0.206.0 ───────────────────────────────────
	#
	# The App Feedback form's Approve proposal / Reject proposal buttons. They
	# run the same functions as the MCP tools, behind the same switches and the
	# same System Manager / Farm Manager gate, and every proposed call still
	# goes through `registry.dispatch`. docs/design/config_flags_triage.md §3.

	@frappe.whitelist()
	def approve_triage_proposal(self, note=""):
		from erpnext_mcp.tools import moments

		return _desk_triage("approve_triage_proposal", lambda: moments.approve(self.name, str(note or "")))

	@frappe.whitelist()
	def reject_triage_proposal(self, reason=""):
		from erpnext_mcp.tools import moments

		return _desk_triage("reject_triage_proposal", lambda: moments.reject(self.name, str(reason or "")))


def _desk_triage(tool, run):
	from erpnext_mcp import settings
	from erpnext_mcp.errors import ToolError

	if not settings.tool_enabled(tool):
		frappe.throw(
			_(
				"{0} is switched off on this site. An operator must tick 'allow_{0}' in ERPNext MCP "
				"Settings to enable it."
			).format(tool)
		)
	try:
		return run()
	except ToolError as exc:
		frappe.throw(str(exc))
