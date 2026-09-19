# SPDX-License-Identifier: MIT
"""Controller for Mobile Device Enrollment — a child table, and empty on purpose.

Frappe imports one module per DocType, child tables included, and a folder with a
JSON and no module breaks `bench migrate` rather than degrading.

EVERY RULE ABOUT A DEVICE ROW LIVES IN `erpnext_mcp/device_enrollment.py`, which
is the only writer: minting, the one-time token exchange, revocation and the
lookup the phone's every request goes through. A rule enforced here as well
would be a second copy of the state machine, and the day the two disagreed a
phone would be refused by one and admitted by the other.
"""

from frappe.model.document import Document


class MobileDeviceEnrollment(Document):
	pass
