# SPDX-License-Identifier: MIT
"""Controller for Upload Link. v0.244.0. docs/design/upload_links.md.

Every field is written by `erpnext_mcp.upload_links` and is read-only in the Desk:
a link is issued, used once and finished by code, never edited by hand.
"""

from frappe.model.document import Document


class UploadLink(Document):
	pass
