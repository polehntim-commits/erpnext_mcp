# SPDX-License-Identifier: MIT
"""Controller for Farm Access Request — "let this phone (or MCP client) in". v0.219.0.

docs/design/device_client_enrollment.md §6. A new phone's request waits here
for a manager's Face ID approval (`device_keys.approve`); from the OAuth phase,
an MCP client's registration waits here for its consent. The short code a
person reads is stored only as its hash. Written by erpnext_mcp; decided on a
manager's phone or by a System Manager in the Desk — never by an MCP tool.
"""

from frappe.model.document import Document


class FarmAccessRequest(Document):
	pass
