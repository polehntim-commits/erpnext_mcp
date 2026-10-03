# SPDX-License-Identifier: MIT
"""Controller for Office Mail — one received office@ email, triaged. v0.221.0.

docs/design/office_reply_drafts.md §7. Written by `erpnext_mcp.office_mail`;
a reply is sent only by `approve_mail_draft` with a named person approving.
"""

from frappe.model.document import Document


class OfficeMail(Document):
	pass
