# SPDX-License-Identifier: MIT
"""One phone, one credential, one row — and one place to look it up. v0.175.0.

Until this release a phone's credential WAS its worker's Frappe `User.api_key`
and `api_secret`. That had three costs, and the third is the one that forced
the change:

  * **One credential per person.** A worker with a new phone and an old one in a
    drawer had one secret between them, so replacing the phone meant killing
    both, and "which handset made that call" had no answer.
  * **The QR carried the credential.** Enrolment printed the live `key:secret`
    into the symbol, so a photograph of the card over somebody's shoulder WAS the
    account until somebody rotated it.
  * **Frappe honours a User key everywhere.** `Authorization: token key:secret`
    is Frappe's own REST credential. A phone's secret on the User row opened
    `/api/resource/*` as that worker — every doctype their roles can read — and
    nothing in `api/guard.py` stands in front of that door.

WHAT REPLACED IT. Every per-device credential lives on a `Mobile Device
Enrollment` row, a child of the worker's `Mobile Access Grant`. Frappe's own auth
never reads that table, so a device secret opens exactly the surface this app
serves and nothing else. `verify` below is the ONE lookup both transports call
(`api/fallback_auth.verify_credential` delegates here, and `farmops_api.auth`
calls that), so there is still exactly one verifier and one failure meter.

────────────────────────────────────────────────────────────────────────────
THE ENROLMENT EXCHANGE
────────────────────────────────────────────────────────────────────────────

  1. The office opens a window (`open_enrollment`): a Pending row carrying the
     SHA-256 of a fresh 256-bit token and a deadline — 24 hours by default.
  2. The QR carries the token and the server URL. NO CREDENTIAL. A photograph of
     it is worth one enrolment, inside the window, and only if nobody scanned it
     first.
  3. The phone posts the token to `/farmops/api/mobile/enroll_device`
     (`exchange`). The server mints this device's own `api_key`/`api_secret`,
     stores the secret in a Password field — encrypted at rest in Frappe's
     `__Auth`, the same store the User secret used — clears the token hash so it
     cannot be spent twice, and returns the pair. THAT RESPONSE IS THE ONLY TIME
     THE SECRET EXISTS IN PLAINTEXT.

WHY THE TOKEN IS STORED HASHED. It is a bearer credential for the length of the
window. A plaintext column would let anybody who can read the grant — a CSV
export, a Version diff, a support screenshot — enrol a phone as that worker.

WHY A NEW QR SUPERSEDES AN OLDER PENDING ONE. "Show me the code again" is the
ordinary reason for a second QR, and the first is then a code nobody can
account for. Only the newest window for an account is open.

────────────────────────────────────────────────────────────────────────────
WHAT STILL MINTS DIRECTLY
────────────────────────────────────────────────────────────────────────────

`create_mobile_user`, `generate_mobile_login_qr` and `recover_mobile_access`
hand a phone a credential without an exchange, because the Farm Ops build in the
field today reads `api_key`/`api_secret` straight off a `farm_ops_login` card
and has no exchange step. They now mint a device row too (`issue_direct`) rather
than writing the User — so there is still one lookup path — and a direct issue
REPLACES the account's other enrolled devices, which is what those tools always
meant by issuing a token.

────────────────────────────────────────────────────────────────────────────
PHONES ENROLLED BEFORE THIS RELEASE
────────────────────────────────────────────────────────────────────────────

`patches/move_mobile_credentials_to_devices` copies each worker's existing
User pair onto an Enrolled row, so the phone in their pocket keeps working with
no re-scan. The User pair is deliberately LEFT where it is: whether anything
else — an MCP client, a script — presents that same key is not something a
migration can see, and clearing it would be an outage in somebody else's
integration. Revoking that migrated row clears the User pair too (`_retire`),
which is what makes per-device revocation true for it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

import frappe

GRANT = "Mobile Access Grant"
DEVICE = "Mobile Device Enrollment"
USER = "User"

#: The Table field on the grant.
TABLE_FIELD = "devices"

PENDING = "Pending"
ENROLLED = "Enrolled"
REVOKED = "Revoked"
STATUSES = (PENDING, ENROLLED, REVOKED)

#: How long an enrolment QR may be exchanged. The length of an onboarding
#: conversation plus a night — the same answer `generate_mobile_login_qr` gave.
DEFAULT_ENROLLMENT_HOURS = 24

#: The ceiling. A week, matching the login card: a window open for a season is
#: a live credential sitting in somebody's photo roll.
MAX_ENROLLMENT_HOURS = 168

#: 32 bytes of entropy — 256 bits, url-safe, 43 characters. Enough that guessing
#: is not a threat model, short enough that the QR stays a comfortable size.
TOKEN_BYTES = 32

#: The longest token string `exchange` will even hash. A real one is 43
#: characters; anything past this is not a token, it is a payload.
MAX_TOKEN_LENGTH = 128

#: What the enrolment QR says it is. A DIFFERENT type from the login card's
#: `farm_ops_login` on purpose: a build that only knows the login card must
#: refuse this as "a different kind of QR", not try to read credentials that are
#: not in it and fail as though the server were broken.
ENROLL_QR_TYPE = "farm_ops_enroll"

#: Where the phone exchanges the token. The same prefix every other phone call
#: uses — see `tools/mobile.API_BASE`.
ENROLL_PATH = "/farmops/api/mobile/enroll_device"

#: `last_seen_on` is written at most this often per device. The verifier runs on
#: every request, sixty a minute per busy phone; an idle sweep measured in days
#: does not need a write per call.
LAST_SEEN_INTERVAL_MINUTES = 60

#: The device name a direct issue records when the caller gives none.
ISSUED_DEVICE_NAME = "Issued credential"

#: The device name `generate_mobile_login_qr` records for the card it prints.
LOGIN_CARD_DEVICE_NAME = "Login card"

#: What the migration names the rows it creates.
MIGRATED_DEVICE_NAME = "Phone enrolled before v0.175.0"


class EnrollmentRefused(Exception):
	"""An exchange the server will not honour. `status` is the HTTP answer."""

	def __init__(self, message: str, status: int = 400):
		super().__init__(message)
		self.status = status


# ── small helpers ───────────────────────────────────────────────────────────
def hash_token(token: str) -> str:
	"""SHA-256 hex of an enrolment token. What the row stores instead of it."""
	return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _now() -> str:
	return str(frappe.utils.now())


def _get(row, key, default=None):
	"""A child row's field, whether the row is a Document (bench) or a dict (suite)."""
	value = row.get(key) if hasattr(row, "get") else getattr(row, key, default)
	return default if value is None else value


def _set(row, key, value) -> None:
	"""Set a child row's field. `Document.set` on a bench, item assignment in the suite."""
	setter = getattr(row, "set", None)
	if callable(setter) and not isinstance(row, dict):
		setter(key, value)
	else:
		row[key] = value


def _rows(grant) -> list:
	return list(grant.get(TABLE_FIELD) or [])


def _grant_doc(user: str):
	"""The grant document for `user`, or a refusal naming what to do instead."""
	if not frappe.db.exists("DocType", GRANT):
		raise EnrollmentRefused(
			"this site has no Mobile Access Grant doctype — run `bench migrate` to finish "
			"installing erpnext_mcp."
		)
	if not frappe.db.exists(GRANT, user):
		raise EnrollmentRefused(
			f"{user} has no Mobile Access Grant, so there is no account to enrol a phone on. "
			"Create the mobile account first — /app/mobile-onboarding, or create_mobile_user."
		)
	return frappe.get_doc(GRANT, user)


def _save(grant) -> None:
	grant.flags.ignore_permissions = True
	grant.save(ignore_permissions=True)


def _new_key() -> str:
	"""A fresh public key, unique across device rows AND User keys.

	Unique against User too because `verify` only ever consults device rows —
	a device key that happened to equal somebody's User key would make an access
	log line ambiguous about which credential made the call, and that is the
	one thing the public half is recorded for.
	"""
	for _attempt in range(8):
		key = secrets.token_hex(8)
		if frappe.db.exists(DEVICE, {"api_key": key}) or frappe.db.exists(USER, {"api_key": key}):
			continue
		return key
	raise RuntimeError("could not mint a unique device key in eight attempts")  # pragma: no cover


def _new_secret() -> str:
	"""160 bits, hex. Hex and not url-safe base64 so it can never contain the `:`
	that separates the pair in `X-FarmOps-Token` — nor anything else a header
	parser has an opinion about."""
	return secrets.token_hex(20)


def read_secret(row_name: str) -> str:
	"""A device row's stored secret in plaintext, or "". FRAPPE'S OWN ACCESSOR.

	`get_decrypted_password` is what Frappe's own api-key validator calls; a
	second reader of `__Auth` would be a second thing to keep in step with it.
	"""
	if not row_name:
		return ""
	try:
		from frappe.utils.password import get_decrypted_password

		return str(get_decrypted_password(DEVICE, row_name, "api_secret", raise_exception=False) or "")
	except Exception:
		return ""


def _clean_label(value, limit: int = 120) -> str:
	return " ".join(str(value or "").split())[:limit]


# ── the lookup every request goes through ───────────────────────────────────
def verify(api_key: str, api_secret: str) -> str:
	"""The User this device pair belongs to, or "". THE ONE LOOKUP PATH.

	Refuses — as "", indistinguishable from an unknown key — a row that is not
	Enrolled, a key that matches more than one row, a disabled login and a wrong
	secret. The grant's own state is NOT checked here: `guard.endpoint` requires
	an Active grant on every mobile method, and a second copy of that rule here
	would be the one that drifted.

	Stamps the row's `last_seen_on` on success, at most once an hour. Unmetered:
	`fallback_auth.verify_credential` owns the failure counter and calls this.
	"""
	api_key = str(api_key or "").strip()
	api_secret = str(api_secret or "").strip()
	if not (api_key and api_secret):
		return ""
	if not frappe.db.exists("DocType", DEVICE):
		return ""

	rows = (
		frappe.db.get_all(
			DEVICE,
			filters={"api_key": api_key, "parenttype": GRANT},
			fields=["name", "parent", "enrollment_status", "last_seen_on"],
			limit=2,
		)
		or []
	)
	if len(rows) != 1:
		return ""
	row = rows[0]
	if str(row.get("enrollment_status") or "") != ENROLLED:
		return ""

	user = str(frappe.db.get_value(GRANT, row.get("parent"), "user") or "")
	if not user:
		return ""
	if not int(frappe.db.get_value(USER, user, "enabled") or 0):
		return ""

	stored = read_secret(str(row.get("name")))
	if not stored or not hmac.compare_digest(stored, api_secret):
		return ""

	_stamp_last_seen(str(row.get("name")), row.get("last_seen_on"))
	return user


def _stamp_last_seen(row_name: str, previous) -> None:
	now = _now()
	try:
		threshold = str(frappe.utils.add_to_date(now, minutes=-LAST_SEEN_INTERVAL_MINUTES))
	except Exception:  # pragma: no cover - a frappe with no add_to_date
		threshold = ""
	if previous and str(previous) >= threshold:
		return
	try:
		frappe.db.set_value(DEVICE, row_name, "last_seen_on", now, update_modified=False)
	except Exception:  # pragma: no cover - a stamp must never refuse a good credential
		pass


# ── opening a window ────────────────────────────────────────────────────────
def open_enrollment(user: str, device_name: str = "", hours=None, issued_by: str = "") -> dict:
	"""Open a one-time enrolment window for `user`. Returns the token ONCE.

	`{"token", "device", "expires_at", "hours", "superseded"}`. The token is in
	no other place in plaintext: the row holds its hash, and the caller's job is
	to put it in a QR and let it go.
	"""
	user = str(user or "").strip()
	hours = DEFAULT_ENROLLMENT_HOURS if hours in (None, "") else int(hours)
	if hours <= 0 or hours > MAX_ENROLLMENT_HOURS:
		raise EnrollmentRefused(
			f"the enrolment window must be between 1 and {MAX_ENROLLMENT_HOURS} hours. A QR "
			"valid for a season is a live way in sitting in somebody's photo roll."
		)

	grant = _grant_doc(user)
	if str(grant.get("state") or "") != "Active":
		raise EnrollmentRefused(
			f"{user}'s Mobile Access Grant is {grant.get('state') or 'not Active'}, so a phone "
			"enrolled on it would be refused on its first call. Reactivate the account first "
			"(create_mobile_user with update_existing=true). Nothing was written."
		)
	if not int(frappe.db.get_value(USER, user, "enabled") or 0):
		raise EnrollmentRefused(f"the login {user} is disabled. Enable it before enrolling a phone.")

	now = _now()
	superseded = 0
	for row in _rows(grant):
		if _get(row, "enrollment_status") == PENDING:
			_retire(row, REVOKED, "superseded by a newer enrolment QR", issued_by or "Administrator", now)
			superseded += 1

	token = secrets.token_urlsafe(TOKEN_BYTES)
	expires_at = str(frappe.utils.add_to_date(now, hours=hours))
	grant.append(
		TABLE_FIELD,
		{
			"device_name": _clean_label(device_name) or "New phone",
			"enrollment_status": PENDING,
			"enrollment_token": hash_token(token),
			"enrollment_expires_at": expires_at,
			"issued_by": issued_by or frappe.session.user,
		},
	)
	_save(grant)
	device = _get(_rows(grant)[-1], "name")
	return {
		"user": user,
		"token": token,
		"device": device,
		"expires_at": expires_at,
		"hours": hours,
		"superseded": superseded,
	}


def enroll_payload(url: str, token: str) -> dict:
	"""What goes IN the enrolment QR: the type, the server, the token. Nothing else.

	No user, no key, no secret, no expiry. The token names the window, the server
	knows everything else, and every extra key is a larger symbol somebody has
	to hold a phone further back from. `api_base` is the prefix the phone joins
	to `url`, emitted for the reason the login card emits it: a site that ever
	moves the prefix moves it in one place.
	"""
	base = str(url or "").rstrip("/")
	return {
		"type": ENROLL_QR_TYPE,
		"v": 1,
		"url": base,
		"api_base": "/farmops/api",
		"token": str(token),
	}


# ── the exchange ────────────────────────────────────────────────────────────
def exchange(token: str, device_name: str = "", device_identifier: str = "") -> dict:
	"""Spend a one-time token: mint this device's credential and return it ONCE.

	Raises `EnrollmentRefused` with the HTTP status the phone should see.
	"""
	token = str(token or "").strip()
	if not token or len(token) > MAX_TOKEN_LENGTH:
		raise EnrollmentRefused("That is not an enrolment code. Scan the QR the office showed you.")
	if not frappe.db.exists("DocType", DEVICE):
		raise EnrollmentRefused("This server has not finished installing phone enrolment.", status=503)

	digest = hash_token(token)
	# FOR UPDATE: two phones scanning the same QR in the same second must not
	# both be handed a credential. The second waits on the row lock and then
	# finds the hash already cleared.
	found = frappe.db.get_value(
		DEVICE,
		{"enrollment_token": digest, "parenttype": GRANT},
		["name", "parent"],
		as_dict=True,
		for_update=True,
	)
	if not found:
		raise EnrollmentRefused(
			"This enrolment code was not recognised or has already been used. Ask the office "
			"to show a new one.",
			status=404,
		)

	grant = frappe.get_doc(GRANT, found.get("parent"))
	row = next((r for r in _rows(grant) if _get(r, "name") == found.get("name")), None)
	if row is None or _get(row, "enrollment_status") != PENDING:
		raise EnrollmentRefused(
			"This enrolment code has already been used. Ask the office to show a new one.", status=404
		)

	now = _now()
	if str(_get(row, "enrollment_expires_at") or "") <= now:
		# Refused and NOT rewritten. The caller rolls back on every refusal, and
		# an expired Pending row already reads as expired — `describe` reports
		# `window_expired` — so there is nothing a write here would add.
		raise EnrollmentRefused(
			"This enrolment code has expired. Ask the office to show a new one.", status=410
		)
	if str(grant.get("state") or "") != "Active":
		raise EnrollmentRefused("This account is not active. Ask the office.", status=403)
	user = str(grant.get("user") or "")
	if not int(frappe.db.get_value(USER, user, "enabled") or 0):
		raise EnrollmentRefused("This account is disabled. Ask the office.", status=403)

	api_key = _new_key()
	api_secret = _new_secret()
	_set(row, "api_key", api_key)
	_set(row, "api_secret", api_secret)
	_set(row, "enrollment_status", ENROLLED)
	_set(row, "enrollment_token", "")
	_set(row, "enrolled_at", now)
	_set(row, "last_seen_on", now)
	if _clean_label(device_name):
		_set(row, "device_name", _clean_label(device_name))
	if _clean_label(device_identifier):
		_set(row, "device_identifier", _clean_label(device_identifier))
	_save(grant)

	return {
		"user": user,
		"device": _get(row, "name"),
		"device_name": _get(row, "device_name"),
		"api_key": api_key,
		"api_secret": api_secret,
		"token": f"{api_key}:{api_secret}",
	}


# ── direct issue, for the tools that hand over a credential outright ────────
def issue_direct(user: str, device_name: str = "", issued_by: str = "", replace: bool = True) -> dict:
	"""Mint an Enrolled device credential with no exchange. Returns the pair ONCE.

	`replace=True` revokes every other Enrolled and Pending device on the account
	first — "issue a token" has always meant "and the previous one stops
	working" in this app, and a caller who wants a second concurrent phone uses
	an enrolment window instead.
	"""
	user = str(user or "").strip()
	grant = _grant_doc(user)
	now = _now()
	replaced = 0
	if replace:
		for row in _rows(grant):
			if _get(row, "enrollment_status") in (PENDING, ENROLLED):
				_retire(
					row, REVOKED, "replaced by a newly issued credential", issued_by or "Administrator", now
				)
				replaced += 1

	api_key = _new_key()
	api_secret = _new_secret()
	grant.append(
		TABLE_FIELD,
		{
			"device_name": _clean_label(device_name) or ISSUED_DEVICE_NAME,
			"enrollment_status": ENROLLED,
			"api_key": api_key,
			"api_secret": api_secret,
			"enrolled_at": now,
			"issued_by": issued_by or frappe.session.user,
		},
	)
	_save(grant)
	device = _get(_rows(grant)[-1], "name")
	return {"api_key": api_key, "api_secret": api_secret, "device": device, "replaced": replaced}


# ── revocation ──────────────────────────────────────────────────────────────
def _retire(row, status: str, reason: str, by: str, when: str) -> None:
	"""Close a row in place. The secret and the token hash go; the record stays."""
	legacy_key = str(_get(row, "api_key") or "")
	_set(row, "enrollment_status", status)
	_set(row, "enrollment_token", "")
	_set(row, "api_secret", "")
	_set(row, "revoked_on", when)
	_set(row, "revoked_by", by)
	_set(row, "revocation_reason", _clean_label(reason, 140))
	if legacy_key:
		_clear_matching_user_key(legacy_key)


def _clear_matching_user_key(api_key: str) -> None:
	"""A migrated row mirrors its worker's User pair; revoking it must end that too.

	Only a User whose key EQUALS this row's is touched, so a device minted by
	this release — whose key is unique against User by construction — never
	reaches a User row at all.
	"""
	name = frappe.db.get_value(USER, {"api_key": api_key}, "name")
	if not name:
		return
	doc = frappe.get_doc(USER, name)
	doc.api_key = ""
	doc.api_secret = ""
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)


def revoke(user: str, device: str, reason: str, revoked_by: str = "") -> dict:
	"""Revoke ONE device on an account. The account and its other devices are untouched."""
	user = str(user or "").strip()
	reason = _clean_label(reason, 140)
	if len(reason) < 4:
		raise EnrollmentRefused(
			"a reason is required — 'phone lost', 'replaced', 'left the crew'. It is the only "
			"thing on the row that says why this handset stopped working."
		)
	grant = _grant_doc(user)
	row = next((r for r in _rows(grant) if _get(r, "name") == device), None)
	if row is None:
		raise EnrollmentRefused(f"{user} has no device {device!r}. list_mobile_devices has the rows.")
	was = str(_get(row, "enrollment_status") or "")
	if was == REVOKED:
		return {
			"user": user,
			"device": device,
			"revoked": False,
			"was": was,
			"reason": _get(row, "revocation_reason"),
		}
	now = _now()
	_retire(row, REVOKED, reason, revoked_by or frappe.session.user, now)
	_save(grant)
	return {
		"user": user,
		"device": device,
		"device_name": _get(row, "device_name"),
		"revoked": True,
		"was": was,
		"reason": reason,
		"revoked_on": now,
		"revoked_by": revoked_by or frappe.session.user,
	}


def revoke_all(user: str, reason: str, revoked_by: str = "") -> int:
	"""Revoke every live device on an account. Returns how many were live."""
	user = str(user or "").strip()
	if not frappe.db.exists("DocType", GRANT) or not frappe.db.exists(GRANT, user):
		return 0
	grant = frappe.get_doc(GRANT, user)
	now = _now()
	count = 0
	for row in _rows(grant):
		if _get(row, "enrollment_status") in (PENDING, ENROLLED):
			_retire(row, REVOKED, reason, revoked_by or frappe.session.user, now)
			count += 1
	if count:
		_save(grant)
	return count


# ── reading ─────────────────────────────────────────────────────────────────
def describe(row, now: str = "") -> dict:
	"""A device row as a caller may see it. NEVER the secret, never the token hash."""
	now = now or _now()
	status = str(_get(row, "enrollment_status") or "")
	expires = str(_get(row, "enrollment_expires_at") or "")
	return {
		"device": _get(row, "name"),
		"device_name": _get(row, "device_name"),
		"status": status,
		"window_expired": bool(status == PENDING and expires and expires <= now),
		"device_identifier": _get(row, "device_identifier") or None,
		"api_key": _get(row, "api_key") or None,
		"enrollment_expires_at": expires or None,
		"enrolled_at": _get(row, "enrolled_at"),
		"last_seen_on": _get(row, "last_seen_on"),
		"issued_by": _get(row, "issued_by"),
		"revoked_on": _get(row, "revoked_on"),
		"revoked_by": _get(row, "revoked_by"),
		"revocation_reason": _get(row, "revocation_reason") or None,
	}


def devices_of(user: str, include_revoked: bool = True) -> list:
	"""Every device row on an account, newest last, as `describe` shows them."""
	user = str(user or "").strip()
	if not frappe.db.exists("DocType", GRANT) or not frappe.db.exists(GRANT, user):
		return []
	now = _now()
	out = []
	for row in _rows(frappe.get_doc(GRANT, user)):
		entry = describe(row, now)
		if include_revoked or entry["status"] != REVOKED:
			out.append(entry)
	return out


def live_devices(user: str) -> list:
	"""The Enrolled devices on an account — the credentials that work right now."""
	return [row for row in devices_of(user, include_revoked=False) if row["status"] == ENROLLED]


def has_live_credential(user: str) -> bool:
	return bool(live_devices(user))


def latest_issued(user: str) -> dict:
	"""`{"api_key", "api_secret"}` of the newest Enrolled device, or {}.

	For `generate_mobile_login_qr(rotate_token=false)` only: re-printing a card
	for a phone that is still working. Reads the secret back through Frappe's
	accessor; it is not a second copy of it.
	"""
	live = live_devices(user)
	if not live:
		return {}
	newest = live[-1]
	secret = read_secret(str(newest["device"]))
	if not secret or not newest.get("api_key"):
		return {}
	return {"api_key": newest["api_key"], "api_secret": secret, "device": newest["device"]}


def idle_devices(cutoff: str) -> list:
	"""`(user, device, clock)` for every Enrolled device not seen since `cutoff`.

	The clock is `last_seen_on`, or `enrolled_at` for a phone that enrolled and
	never called. A row with neither is not judged — there is nothing to judge.
	"""
	if not frappe.db.exists("DocType", DEVICE):
		return []
	rows = (
		frappe.db.get_all(
			DEVICE,
			filters={"parenttype": GRANT, "enrollment_status": ENROLLED},
			fields=["name", "parent", "last_seen_on", "enrolled_at"],
			limit=5000,
		)
		or []
	)
	out = []
	for row in rows:
		clock = str(row.get("last_seen_on") or "") or str(row.get("enrolled_at") or "")
		if clock and clock < str(cutoff):
			out.append((str(row.get("parent")), str(row.get("name")), clock))
	return out


def status_of(user: str, device: str) -> dict:
	"""One device's current state, for the Desk dialog's "has it scanned yet" poll."""
	for entry in devices_of(user):
		if entry["device"] == device:
			return entry
	return {}


__all__ = (
	"DEFAULT_ENROLLMENT_HOURS",
	"DEVICE",
	"ENROLLED",
	"ENROLL_PATH",
	"ENROLL_QR_TYPE",
	"MAX_ENROLLMENT_HOURS",
	"PENDING",
	"REVOKED",
	"EnrollmentRefused",
	"describe",
	"devices_of",
	"enroll_payload",
	"exchange",
	"has_live_credential",
	"hash_token",
	"idle_devices",
	"issue_direct",
	"latest_issued",
	"live_devices",
	"open_enrollment",
	"read_secret",
	"revoke",
	"revoke_all",
	"status_of",
	"verify",
)
