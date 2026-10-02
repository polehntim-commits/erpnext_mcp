# SPDX-License-Identifier: MIT
"""The badge button on the Employee form. One whitelisted method. v0.56.0.

    POST /api/method/erpnext_mcp.api.badges.employee_badge_card

`badge_sheet.render_badge_sheet` answers the Employee LIST — thirty people
ticked, one sheet of cards. This answers the Employee FORM: one person open in
front of somebody, and the question "where is their badge".

────────────────────────────────────────────────────────────────────────────
WHY THERE IS A DESK ROUTE AT ALL WHEN THERE IS ALREADY AN MCP TOOL
────────────────────────────────────────────────────────────────────────────

`generate_employee_id_card` is reachable through the MCP endpoint, and that is
the wrong door for a person. The MCP endpoint takes a token an AI client holds,
answers JSON-RPC, and is gated by the `allow_<tool>` switches. An HR manager
with an Employee form open has a session, a role and a mouse.

THE `allow_<tool>` SWITCHES ARE DELIBERATELY NOT CONSULTED HERE, the same call
`api/gis.py` made for the map and `api/__init__.py` made for the phone, and for
the same reason. Those switches are the AI's leash:
`allow_generate_employee_id_card` off means "the model may not issue badges",
and reading it here would mean an operator who distrusts the model also loses
the button on their own form — which is not what they asked for and not what
the switch says.

WHAT GATES IT INSTEAD, and both gates are real:

  1. `frappe.has_permission("Employee", "write", doc=<name>, throw=True)` on the
     SPECIFIC record, so a User Permission scoping somebody to one company
     scopes this too. `write` and not `read`: issuing a badge writes a register
     row and attaches two files.
  2. `employee.require_hr_role()`, which the tool runs for itself. A badge is
     what attributes piece work to somebody on the payroll, and this app has
     never let that be issued by an account that cannot see the payroll.

────────────────────────────────────────────────────────────────────────────
IT RETURNS THE CARD'S HTML, NOT A FILE URL TO REDIRECT TO
────────────────────────────────────────────────────────────────────────────

The button shows the card in a dialog. Handing back a URL would mean the browser
fetching a private File through a second request with its own authentication —
and on a bench with no PDF renderer there is no file to fetch at all, which
would leave the button doing nothing on exactly the benches where the card
matters most. The HTML is the card; the attachment is the copy that stays.
"""

from __future__ import annotations

import frappe

from ..errors import ToolError
from ..tools import badges

EMPLOYEE = "Employee"


def _speaks_frappe(implementation, *args, **kwargs):
	"""Run the implementation, turning `ToolError` into a modal.

	A FUNCTION AND NOT A DECORATOR, for the reason `api/gis.speaks_frappe` gives
	at length: `frappe.call` reads a whitelisted callable's argument names with
	`inspect.getfullargspec`, which does not follow `functools.wraps`, so a
	decorated method presents as `(*args, **kwargs)` and Frappe answers that by
	forwarding the entire form dict — `cmd` and `csrf_token` included — into a
	function that never asked for them.

	A `ToolError` here is "this person has left", "their entity is not one you
	can reach", "this site has no QR encoder". Every one of those is a sentence
	somebody needs to read. Raised out of a whitelisted method it would be an
	HTTP 500 and a traceback in the console; anything that is NOT a ToolError is
	left alone and still reaches the Error Log, because that is a bug.
	"""
	try:
		return implementation(*args, **kwargs)
	except ToolError as error:
		frappe.throw(str(error), title=frappe._("Employee Badge"))


@frappe.whitelist(methods=["POST"])
def employee_badge_card(employee=None, regenerate=0):
	"""Issue or reprint one employee's badge and hand back the card. Desk only."""
	return _speaks_frappe(_employee_badge_card, employee=employee, regenerate=regenerate)


@frappe.whitelist(methods=["POST"])
def request_badge_photo(employee=None):
	"""Raise the "Badge photo" task for one Employee, from their Desk form. v0.214.0.

	The tool's own gate is the gate: a person's own, or HR / Farm Manager /
	Foreman for somebody else. This adds the per-record read check a Desk method
	owes, so a User Permission scoping somebody to one company scopes the button.
	"""
	return _speaks_frappe(_request_badge_photo, employee=employee)


def _request_badge_photo(employee=None):
	name = str(employee or "").strip()
	if not name:
		frappe.throw(frappe._("No employee was named."), title=frappe._("Badge photo"))
	frappe.has_permission(EMPLOYEE, "read", doc=name, throw=True)
	return badges.request_badge_photo({"employee": name}).data


def _employee_badge_card(employee=None, regenerate=0):
	name = str(employee or "").strip()
	if not name:
		frappe.throw(frappe._("No employee was named."), title=frappe._("Employee Badge"))

	# THE SPECIFIC RECORD, not the doctype. See the module docstring — this is
	# what makes a User Permission that scopes somebody to one company scope the
	# badge button too.
	frappe.has_permission(EMPLOYEE, "write", doc=name, throw=True)

	result = badges.generate_employee_id_card(
		{
			"employee": name,
			# `frappe.call` posts booleans as the strings "0" and "1", and "0" is
			# truthy in Python. `cint` is what Frappe itself uses to read one back.
			"regenerate": bool(frappe.utils.cint(regenerate)),
		}
	)
	data = result.data or {}
	qr = data.get("qr_attachment") or {}
	card = data.get("card_attachment") or {}
	return {
		"employee": data.get("employee"),
		"employee_name": data.get("employee_name"),
		"badge_id": data.get("badge_id"),
		"created": bool(data.get("created")),
		"reused": bool(data.get("reused")),
		"html": data.get("card_html"),
		# BOTH ATTACHMENTS, SEPARATELY. The QR needs a QR encoder and the card
		# needs a PDF binary; a bench can have one and not the other, and the
		# dialog says which of the two files actually landed in the sidebar.
		"qr_url": qr.get("file_url"),
		"card_url": card.get("file_url"),
		"attached": bool(card.get("attached")),
		"note": card.get("note") or qr.get("note") or "",
		"summary": result.summary,
	}
