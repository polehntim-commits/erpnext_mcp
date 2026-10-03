# SPDX-License-Identifier: MIT
"""No "Leave this conversation" link on transactional mail. v0.216.1.

Unticks Email Account → "Send unsubscribe message in email" on every enabled
outgoing account. That setting is the only thing that adds the link to
Communication replies, notifications and document emails. Newsletters (and
Email Group mailings) keep their unsubscribe link: Frappe always adds it when
the reference is a Newsletter, whatever this setting says. Runs once; an
operator who ticks it again keeps their choice.
"""

from __future__ import annotations

import frappe

from erpnext_mcp import compat, email_branding


def execute() -> None:
	changed = run()
	if changed:
		print(
			"erpnext_mcp: the 'Leave this conversation' unsubscribe link is off for "
			f"{', '.join(changed)} (newsletters keep theirs)."
		)


def run() -> list:
	if not compat.doctype_exists(email_branding.ACCOUNT) or not compat.has_field(
		email_branding.ACCOUNT, "send_unsubscribe_message"
	):
		return []
	changed = []
	for row in frappe.db.get_all(
		email_branding.ACCOUNT,
		filters={"enable_outgoing": 1},
		fields=["name", "send_unsubscribe_message"],
		limit=50,
	):
		if not email_branding.unsubscribe_on(dict(row)):
			continue
		frappe.db.set_value(
			email_branding.ACCOUNT, row["name"], "send_unsubscribe_message", 0, update_modified=False
		)
		changed.append(str(row["name"]))
	return changed
