# SPDX-License-Identifier: MIT
"""Outgoing mail: who the site sends as, and whether the provider will take it. v0.216.1.

Frappe sends a user-initiated email (a print emailed from the Desk, a comment
notification) with `From:` set to the PERSON who triggered it unless the Email
Account says "Always use this email address as sender"
(`always_use_account_email_id_as_sender`). Zoho's SMTP — and most hosted SMTP —
refuses a `From:` that is not the authenticated mailbox (553 "Sender is not
allowed to relay emails"), so with the box unticked those emails sit in Email
Queue as errors while system emails, which already use the account address,
go through. That half-working state is the hard one to notice.

`status()` reports every outgoing account and flags it. `default_sender_policy`
(the v0.216.1 patch) ticks the box on enabled outgoing accounts whose SMTP
server is Zoho's and leaves every other account as it found it.
"""

from __future__ import annotations

import frappe

from . import compat, email_branding

ACCOUNT = "Email Account"
SENDER_FIELD = "always_use_account_email_id_as_sender"
NAME_FIELD = "always_use_account_name_as_sender_name"

#: SMTP hosts that refuse a From: other than the signed-in mailbox.
STRICT_SENDER_HOSTS = ("zoho",)


def _outgoing() -> list:
	if not compat.doctype_exists(ACCOUNT):
		return []
	fields = compat.existing_fields(
		ACCOUNT,
		(
			"name",
			"email_id",
			"smtp_server",
			"enable_outgoing",
			"default_outgoing",
			SENDER_FIELD,
			NAME_FIELD,
			"send_unsubscribe_message",
			"footer",
			"brand_logo",
		),
	)
	try:
		rows = frappe.db.get_all(ACCOUNT, filters={"enable_outgoing": 1}, fields=fields, limit=50)
	except Exception:  # pragma: no cover
		return []
	return [dict(row) for row in rows or []]


def is_strict(smtp_server) -> bool:
	host = str(smtp_server or "").lower()
	return any(marker in host for marker in STRICT_SENDER_HOSTS)


def status() -> dict:
	"""The `email` block of `get_server_status`. Read-only; never raises."""
	try:
		accounts, warnings = [], []
		for row in _outgoing():
			own_sender = bool(int(row.get(SENDER_FIELD) or 0))
			strict = is_strict(row.get("smtp_server"))
			accounts.append(
				{
					"account": row.get("name"),
					"email_id": row.get("email_id"),
					"smtp_server": row.get("smtp_server"),
					"default_outgoing": bool(int(row.get("default_outgoing") or 0)),
					"always_use_account_email_as_sender": own_sender,
					"always_use_account_name_as_sender_name": bool(int(row.get(NAME_FIELD) or 0)),
					"provider_requires_account_sender": strict,
					# v0.216.1: footer, logo and unsubscribe — `email_branding`.
					"branding": email_branding.account_status(row),
				}
			)
			warnings += [
				f"{row.get('name')}: {line}" for line in email_branding.account_status(row)["warnings"]
			]
			if not own_sender:
				warnings.append(
					f"{row.get('name')} ({row.get('smtp_server') or 'no SMTP server'}): "
					+ (
						"'Always use this email address as sender' is OFF and this provider refuses any other "
						"sender — emails a person sends from the Desk will fail in Email Queue. Tick it."
						if strict
						else "'Always use this email address as sender' is OFF, so emails a person sends go out "
						"with their own address as From; many SMTP providers refuse or spam-flag that."
					)
				)
		if not accounts:
			warnings.append("No enabled outgoing Email Account: nothing this site emails will be sent.")
		elif not any(account["default_outgoing"] for account in accounts):
			warnings.append("No outgoing Email Account is marked Default Outgoing.")
		return {
			"outgoing_accounts": accounts,
			"standard_footer_disabled": email_branding.standard_footer_disabled(),
			"warnings": warnings,
		}
	except Exception as exc:  # pragma: no cover
		return {"error": str(exc)}


def default_sender_policy() -> list:
	"""Tick 'always use account email as sender' on strict-provider accounts. Returns names changed."""
	if not compat.has_field(ACCOUNT, SENDER_FIELD):
		return []
	changed = []
	for row in _outgoing():
		if not is_strict(row.get("smtp_server")) or int(row.get(SENDER_FIELD) or 0):
			continue
		try:
			frappe.db.set_value(ACCOUNT, row["name"], SENDER_FIELD, 1, update_modified=False)
			changed.append(str(row["name"]))
		except Exception:  # pragma: no cover - a site mid-migrate
			continue
	return changed
