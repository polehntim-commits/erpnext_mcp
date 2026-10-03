# SPDX-License-Identifier: MIT
"""Where security alerts are emailed. v0.216.1.

`settings.security_alert_email` (Security Alert Recipients), split on commas
and semicolons; when it is empty, every enabled System Manager except
Administrator and Guest. The same rule `drift.recipients` applies to the drift
report — written out again rather than shared so that changing who hears about
the ledger can never change who hears about sign-ins, or the other way round.

`send` never raises: a site with no outgoing mail account is an ordinary state,
and the alert's audit row is written before this is called.
"""

from __future__ import annotations

import frappe

from . import settings

FALLBACK_ROLE = "System Manager"

#: Accounts that are never a person reading mail.
_NOT_PEOPLE = ("Administrator", "Guest")


def recipients() -> list:
	"""The configured addresses, else the enabled System Managers. Deduplicated, in order."""
	configured = settings.security_alert_email()
	if configured:
		out = []
		for part in configured.replace(";", ",").split(","):
			address = part.strip()
			if address and address not in out:
				out.append(address)
		return out
	try:
		users = frappe.db.get_all(
			"Has Role",
			filters={"role": FALLBACK_ROLE, "parenttype": "User"},
			pluck="parent",
			limit=50,
		)
	except Exception:  # pragma: no cover
		return []
	out = []
	for user in users or []:
		if user in _NOT_PEOPLE or user in out:
			continue
		if frappe.db.get_value("User", user, "enabled"):
			out.append(str(user))
	return out


def send(subject: str, message: str) -> list:
	"""Email `recipients()`. Returns who it was sent to; [] when nobody or mail failed."""
	to = recipients()
	if not to:
		return []
	try:
		frappe.sendmail(recipients=to, subject=subject, message=message, now=False)
	except Exception:
		try:
			frappe.log_error(title=subject, message=message)
		except Exception:  # pragma: no cover
			pass
		return []
	return to
