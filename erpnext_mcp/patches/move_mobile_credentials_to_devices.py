# SPDX-License-Identifier: MIT
"""Give every phone enrolled before v0.175.0 a device row, so it keeps working.

WHAT BREAKS WITHOUT THIS. v0.175.0 moved the phone verifier off `User.api_key`
and onto `Mobile Device Enrollment` rows — see `device_enrollment`. Every phone
already in a pocket holds the pair that was minted onto its worker's User row,
and after the upgrade the verifier no longer looks there. Without a row carrying
that same pair, every enrolled phone in the field would answer its next call
with 401, which the app reads as "sign out" — and signing out discards its queue
of unsynced work.

WHAT IT WRITES. For each Mobile Access Grant whose User holds an API key and a
readable secret, one Enrolled row on the grant carrying THAT SAME pair: the key
copied, the secret copied through Frappe's own accessor and re-encrypted into
the row's Password field. `enrolled_at` is the grant's `token_issued_on` and
`last_seen_on` is the grant's own, so the per-device idle sweep judges the phone
by the same clock it was judged by yesterday.

WHICH GRANTS. Active and Expired ones. A Revoked grant's credential was cleared
when it was revoked — `revoke_mobile_user` does that — so a Revoked grant still
holding a live User pair is drift the roster already flags, and bringing it
forward would make a revoked phone look enrolled.

THE USER PAIR IS LEFT WHERE IT IS, and that is deliberate. Whether something
other than the phone presents the same key — an MCP client's identity header, a
script — is not a thing a migration can see, and clearing it would be an outage
in somebody else's integration discovered at the worst moment. The pair is
harmless to the phone surface now (nothing there reads it), `list_mobile_users`
flags it, and revoking the migrated device clears it (`device_enrollment._retire`
clears a User pair whose key matches the row's).

IDEMPOTENT. A grant that already has a row carrying the User's key is skipped,
so a second migrate — or a migrate on a site that enrolled a phone after the
upgrade — writes nothing.
"""

import frappe

GRANT = "Mobile Access Grant"
DEVICE = "Mobile Device Enrollment"
USER = "User"


def execute():
	if not (frappe.db.exists("DocType", GRANT) and frappe.db.exists("DocType", DEVICE)):
		return
	from erpnext_mcp import device_enrollment

	grants = frappe.db.get_all(
		GRANT,
		filters={"state": ("in", ["Active", "Expired"])},
		fields=["name", "user", "token_issued_on", "last_seen_on"],
		limit=0,
	)
	moved = 0
	for grant_row in grants or []:
		user = str(grant_row.get("user") or grant_row.get("name") or "")
		if not user or not frappe.db.exists(USER, user):
			continue
		api_key = str(frappe.db.get_value(USER, user, "api_key") or "").strip()
		if not api_key:
			continue
		try:
			secret = str(frappe.get_doc(USER, user).get_password("api_secret", raise_exception=False) or "")
		except Exception:
			secret = ""
		if not secret:
			continue

		grant = frappe.get_doc(GRANT, grant_row["name"])
		if any(
			str(device_enrollment._get(row, "api_key") or "") == api_key for row in grant.get("devices") or []
		):
			continue
		grant.append(
			"devices",
			{
				"device_name": device_enrollment.MIGRATED_DEVICE_NAME,
				"enrollment_status": device_enrollment.ENROLLED,
				"api_key": api_key,
				"api_secret": secret,
				"enrolled_at": grant_row.get("token_issued_on") or frappe.utils.now(),
				"last_seen_on": grant_row.get("last_seen_on"),
				"issued_by": "Administrator",
			},
		)
		grant.flags.ignore_permissions = True
		grant.save(ignore_permissions=True)
		moved += 1

	if moved:
		print(
			f"erpnext_mcp: moved {moved} enrolled phone credential(s) onto Mobile Device Enrollment "
			"rows. Those phones keep working with no re-scan; the matching Frappe API key is left "
			"on each User row, and list_mobile_users flags it."
		)
