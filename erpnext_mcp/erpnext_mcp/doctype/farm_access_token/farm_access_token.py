# SPDX-License-Identifier: MIT
"""Controller for Farm Access Token — one short-lived credential, stored as its hash. v0.218.0.

docs/design/device_client_enrollment.md §4, §6.3, §9. Access tokens for phones
(bound to the device's proof key), challenges, and — from the OAuth phase —
MCP access/refresh tokens and authorization codes. THE TOKEN ITSELF IS NEVER
STORED: `token_hash` is its SHA-256, and the plaintext exists only in the one
answer that handed it out. Rows are written and read by `device_keys` and
`mcp_oauth`; nothing here is meant to be edited by hand.
"""

from frappe.model.document import Document


class FarmAccessToken(Document):
	pass
