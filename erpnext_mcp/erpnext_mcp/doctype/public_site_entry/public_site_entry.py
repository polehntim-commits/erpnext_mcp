# SPDX-License-Identifier: MIT
"""Controller for Public Site Entry — what came in through the public website. v0.335.0.

Created only by `public_site.submit` / `record_scan` (guest forms and QR scans); see
`erpnext_mcp/public_site.py` for every check a submission passes first.
"""

from frappe.model.document import Document


class PublicSiteEntry(Document):
	pass
