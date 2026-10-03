# SPDX-License-Identifier: MIT
"""Zoho outgoing accounts send as the account. v0.216.1.

See `mail_status`: Zoho refuses a From: other than the signed-in mailbox, so
an outgoing Email Account on smtp.zoho.* with "Always use this email address as
sender" unticked fails every email a person sends from the Desk. Ticks it on
those accounts only; every other account is left as it is. Runs once.
"""

from __future__ import annotations

from erpnext_mcp import mail_status


def execute() -> None:
	changed = mail_status.default_sender_policy()
	if changed:
		print(
			f"erpnext_mcp: 'Always use this email address as sender' turned on for {', '.join(changed)} "
			"(Zoho refuses any other sender)."
		)
