# SPDX-License-Identifier: MIT
"""A filed login card deletes itself. v0.223.0.

docs/design/employee_file.md §1. `generate_mobile_login_qr(archive=true)` used to
file the card PNG — a LIVE CREDENTIAL — on a Governance Document and leave it
there. From v0.223.0 filing is refused while device keys are on and off by
default otherwise (`login_card_archive_enabled`); a card that is filed is
purged on its first sign-in or when it expires, whichever comes first.

ONLY CARDS ARE EVER TOUCHED. A Governance Document is a card only if its title
carries this app's prefix AND every attachment on it is a `mobile-enrolment-*.png`.
Anything else — an operating agreement somebody titled oddly — is left alone.

The durable record is the Mobile Access Grant and its devices, which hold no
secret. The purge writes one MCP Action Log row naming the card and why; never
the image, never the secret.
"""

from __future__ import annotations

import frappe

from . import audit, compat

GOVERNANCE = "Governance Document"
GRANT = "Mobile Access Grant"
DEVICE = "Mobile Device Enrollment"
TITLE_PREFIX = "Farm Ops mobile enrolment card — "
FILE_PREFIX = "mobile-enrolment-"
LOGIN_CARD_DEVICE = "Login card"
#: The longest a card can be valid to enrol with (generate_mobile_login_qr caps it).
MAX_CARD_HOURS = 168


def _now() -> str:
	return str(frappe.utils.now())[:19]


def archive_enabled() -> bool:
	from . import settings

	return settings.as_bool(settings._value("login_card_archive_enabled"))


def _attachments(name: str) -> list:
	if not compat.doctype_exists("File"):
		return []
	return [
		dict(row)
		for row in frappe.db.get_all(
			"File",
			filters={"attached_to_doctype": GOVERNANCE, "attached_to_name": name},
			fields=["name", "file_name"],
			limit=20,
		)
	]


def is_card(name: str) -> bool:
	"""This app's own filed login card, and nothing else."""
	if not name or not compat.doctype_exists(GOVERNANCE):
		return False
	title = str(frappe.db.get_value(GOVERNANCE, name, "title") or "")
	if not title.startswith(TITLE_PREFIX):
		return False
	files = _attachments(name)
	return all(str(f.get("file_name") or "").startswith(FILE_PREFIX) for f in files)


def purge(name: str, reason: str) -> bool:
	"""Delete one filed card and its image; clear the grant's pointer; audit. Never raises."""
	try:
		if not is_card(name):
			return False
		for row in _attachments(name):
			frappe.delete_doc("File", row["name"], ignore_permissions=True, force=True)
		frappe.delete_doc(GOVERNANCE, name, ignore_permissions=True, force=True)
		if compat.doctype_exists(GRANT):
			for grant in frappe.db.get_all(GRANT, filters={"qr_document": name}, pluck="name", limit=5):
				frappe.db.set_value(GRANT, grant, "qr_document", None, update_modified=False)
		audit.record(
			"login_card:purged",
			{"governance_document": name},
			audit.STATUS_SUCCESS,
			f"filed login card {name} deleted: {reason}",
			commit=False,
		)
		return True
	except Exception:  # pragma: no cover - a purge must never break a sign-in or a job
		try:
			frappe.log_error(title=f"erpnext_mcp: login card purge failed ({name})")
		except Exception:
			pass
		return False


def on_sign_in(grant: str, device_name: str) -> bool:
	"""The login card was used: its filed copy has done its job. Called by device_enrollment."""
	if device_name != LOGIN_CARD_DEVICE or not grant or not compat.doctype_exists(GRANT):
		return False
	card = frappe.db.get_value(GRANT, grant, "qr_document")
	return purge(str(card), "the card was used to sign in") if card else False


def due() -> list:
	"""[(card, reason)] for every filed card that should not exist any more."""
	if not compat.doctype_exists(GOVERNANCE):
		return []
	now = _now()
	out, referenced = [], set()
	if compat.doctype_exists(GRANT):
		for grant in frappe.db.get_all(
			GRANT,
			filters={"qr_document": ["is", "set"]},
			fields=["name", "qr_document", "qr_expires_at", "last_qr_issued_on"],
			limit=5000,
		):
			card = str(grant.get("qr_document") or "")
			referenced.add(card)
			if str(grant.get("qr_expires_at") or "") and str(grant["qr_expires_at"])[:19] <= now:
				out.append((card, "the card expired"))
				continue
			issued = str(grant.get("last_qr_issued_on") or "")[:19]
			seen = frappe.db.get_all(
				DEVICE,
				filters={"parent": grant["name"], "parenttype": GRANT, "device_name": LOGIN_CARD_DEVICE},
				fields=["last_seen_on"],
				limit=5,
			)
			if any(
				str(row.get("last_seen_on") or "")[:19] >= issued and row.get("last_seen_on") for row in seen
			):
				out.append((card, "the card was used to sign in"))
	cutoff = str(frappe.utils.add_to_date(frappe.utils.now(), hours=-MAX_CARD_HOURS))[:19]
	for row in frappe.db.get_all(
		GOVERNANCE, filters={"title": ["like", f"{TITLE_PREFIX}%"]}, fields=["name", "creation"], limit=5000
	):
		if row["name"] not in referenced and str(row.get("creation") or "")[:19] <= cutoff:
			out.append((row["name"], "superseded by a newer card and past any card's life"))
	return out


def purge_due() -> list:
	"""The hourly job, and the v0.223.0 patch. Never raises."""
	try:
		done = [(card, reason) for card, reason in due() if purge(card, reason)]
		frappe.db.commit()
		return done
	except Exception:  # pragma: no cover
		try:
			frappe.log_error(title="erpnext_mcp: login card purge job failed")
		except Exception:
			pass
		return []


def backfill_grant_employees() -> int:
	"""Fill Mobile Access Grant.employee from Employee.user_id where empty. The patch's other half."""
	if not compat.doctype_exists(GRANT) or not compat.doctype_exists("Employee"):
		return 0
	count = 0
	for grant in frappe.db.get_all(
		GRANT, filters={"employee": ["is", "not set"]}, fields=["name", "user"], limit=5000
	):
		employee = frappe.db.get_value("Employee", {"user_id": grant.get("user")}, "name")
		if employee:
			frappe.db.set_value(GRANT, grant["name"], "employee", employee, update_modified=False)
			count += 1
	return count
