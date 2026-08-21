# SPDX-License-Identifier: MIT
"""Controller for Traceability Lot Source — one edge of the transformation graph.

NO VALIDATION OF ITS OWN, AND THAT IS DELIBERATE. Everything worth refusing about
a source row is a fact about the row's PLACE IN THE PARENT — the same lot twice,
a lot that is its own source — and a child controller cannot see the parent's
other rows in the order Frappe validates them. `Traceability Lot Code.validate`
does the whole check in one place, which is also where the error message can name
the parent the operator is actually looking at.

The class exists because Frappe's `get_controller` asks for the class named after
the DocType and silently falls back to the base `Document` when there is none —
so a child table with no module here is a child table that would go on working
until somebody added a rule to it and could not see why it never ran.
"""

from frappe.model.document import Document


class TraceabilityLotSource(Document):
	pass
