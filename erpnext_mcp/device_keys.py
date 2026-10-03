# SPDX-License-Identifier: MIT
"""Device-bound keys: pickup links, signed requests, short-lived tokens. v0.218.0.

docs/design/device_client_enrollment.md §3–§5, §7, §8. OFF until
`device_keys_enabled` is ticked on ERPNext MCP Settings; until then nothing here
is reachable and the phones keep their api_key/api_secret pairs.

THE PHONE HOLDS TWO P-256 KEYS AND NO SECRET THE SERVER KNOWS.

* The **unlock key** (Secure Enclave, Face ID / Touch ID to use) signs a pickup,
  a sign-in challenge and an approval. Its public half is on the device row.
* The **proof key** (Secure Enclave, no prompt) signs a DPoP proof on every
  request (RFC 9449, plus `bh`, the body's hash). Its thumbprint is on the row
  and every access token is bound to it, so a token copied off the phone is
  worth nothing without the phone.

Access tokens are random, short-lived (the re-auth interval) and stored only as
their SHA-256 (`Farm Access Token`). Everything that fails says nothing about
why to an anonymous caller: the sidecar answers its uniform 401/404.

`htu` IS COMPARED BY PATH. The phone signs the URL it called
(`https://<host>/farmops/api/...`); behind Tailscale and the forwarder the
sidecar sees `http://127.0.0.1:5250/farmops/api/...`. Scheme and host are
rewritten by every proxy on the way, so the path is what is bound; the token and
the key binding are what make a stolen proof useless.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlsplit

import frappe

from . import compat, device_enrollment, settings

TOKEN_DOCTYPE = "Farm Access Token"
GRANT = device_enrollment.GRANT
DEVICE = device_enrollment.DEVICE

#: ±seconds a proof's `iat` may differ from the server clock (§8.3).
SKEW_SECONDS = 120
#: How long a proof's `jti` is remembered (replay window).
JTI_SECONDS = 300
#: How long a sign-in challenge lives.
CHALLENGE_SECONDS = 60
#: Pickup link lifetime bounds, minutes (§3.1).
LINK_MIN, LINK_MAX, LINK_DEFAULT = 2, 60, 10
#: Re-auth interval bounds, minutes (§4.2).
REAUTH_MIN, REAUTH_MAX, REAUTH_DEFAULT = 5, 1440, 480

PICKUP_PATH = "/farmops/api/enroll/"
KEY_PROTECTIONS = ("secure_enclave", "strongbox", "tee", "software")
PLATFORMS = ("ios", "android")

_JTI_SEEN: dict = {}


class Refused(Exception):
	"""A request that does not authenticate. The reason is for the log, never the caller."""

	def __init__(self, reason: str, revoked_device: bool = False):
		super().__init__(reason)
		self.revoked_device = revoked_device


# ── settings ────────────────────────────────────────────────────────────────
def _setting(name, default):
	value = settings._value(name)
	return default if value in (None, "") else value


def enabled() -> bool:
	return settings.as_bool(_setting("device_keys_enabled", 0))


def legacy_secrets_allowed() -> bool:
	return settings.as_bool(_setting("legacy_device_secrets", 1))


def link_minutes(value=None) -> int:
	try:
		minutes = int(value if value not in (None, "") else _setting("enrollment_link_minutes", LINK_DEFAULT))
	except (TypeError, ValueError):
		minutes = LINK_DEFAULT
	return max(LINK_MIN, min(LINK_MAX, minutes))


def reauth_minutes() -> int:
	try:
		minutes = int(_setting("device_reauth_minutes", REAUTH_DEFAULT))
	except (TypeError, ValueError):
		minutes = REAUTH_DEFAULT
	return max(REAUTH_MIN, min(REAUTH_MAX, minutes))


# ── encoding ────────────────────────────────────────────────────────────────
def b64u(data: bytes) -> str:
	return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
	text = str(text or "")
	return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sha256_b64u(data: bytes) -> str:
	return b64u(hashlib.sha256(data).digest())


def hash_token(token: str) -> str:
	return hashlib.sha256(str(token or "").encode()).hexdigest()


def jwk_of(value) -> dict:
	"""A P-256 public JWK from a dict or its JSON text. Raises Refused."""
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			raise Refused("key is not JSON") from None
	if not isinstance(value, dict):
		raise Refused("key is not a JWK")
	if value.get("kty") != "EC" or value.get("crv") != "P-256":
		raise Refused("key is not P-256")
	try:
		x, y = b64u_decode(value["x"]), b64u_decode(value["y"])
	except Exception:
		raise Refused("key coordinates do not decode") from None
	if len(x) != 32 or len(y) != 32:
		raise Refused("key coordinates are the wrong length")
	return {"kty": "EC", "crv": "P-256", "x": value["x"], "y": value["y"]}


def thumbprint(jwk: dict) -> str:
	"""RFC 7638."""
	canonical = json.dumps(
		{"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]},
		separators=(",", ":"),
		sort_keys=True,
	)
	return sha256_b64u(canonical.encode())


def _public_key(jwk: dict):
	from cryptography.hazmat.primitives.asymmetric import ec

	x = int.from_bytes(b64u_decode(jwk["x"]), "big")
	y = int.from_bytes(b64u_decode(jwk["y"]), "big")
	return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()


def verify_es256(jwk: dict, message: bytes, signature: str) -> bool:
	"""A raw r||s (64-byte, base64url) ECDSA P-256 / SHA-256 signature — what CryptoKit makes."""
	from cryptography.exceptions import InvalidSignature
	from cryptography.hazmat.primitives import hashes
	from cryptography.hazmat.primitives.asymmetric import ec
	from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

	try:
		raw = b64u_decode(signature)
		if len(raw) != 64:
			return False
		der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
		_public_key(jwk).verify(der, message, ec.ECDSA(hashes.SHA256()))
		return True
	except (InvalidSignature, ValueError, Exception):
		return False


# ── DPoP ────────────────────────────────────────────────────────────────────
def _now_epoch() -> float:
	return time.time()


def _seen(jti: str) -> bool:
	"""True if this jti was already used inside the window; records it otherwise."""
	key = f"erpnext_mcp:dpop_jti:{hashlib.sha256(jti.encode()).hexdigest()}"
	cache = getattr(frappe, "cache", None)
	if callable(cache):
		try:
			client = cache()
			if client.get(key):
				return True
			client.set(key, 1)
			client.expire(key, JTI_SECONDS)
			return False
		except Exception:
			pass
	now = _now_epoch()
	for stale in [k for k, at in _JTI_SEEN.items() if now - at > JTI_SECONDS]:
		_JTI_SEEN.pop(stale, None)
	if key in _JTI_SEEN:
		return True
	_JTI_SEEN[key] = now
	return False


def verify_dpop(proof: str, method: str, path: str, body: bytes, access_token: str = "") -> dict:
	"""Check one DPoP proof. Returns the proof key's JWK. Raises Refused."""
	parts = str(proof or "").split(".")
	if len(parts) != 3:
		raise Refused("no DPoP proof")
	try:
		header = json.loads(b64u_decode(parts[0]))
		claims = json.loads(b64u_decode(parts[1]))
	except Exception:
		raise Refused("DPoP proof does not decode") from None
	if header.get("typ") != "dpop+jwt" or header.get("alg") != "ES256":
		raise Refused("DPoP header is not ES256 dpop+jwt")
	jwk = jwk_of(header.get("jwk"))
	if not verify_es256(jwk, f"{parts[0]}.{parts[1]}".encode(), parts[2]):
		raise Refused("DPoP signature does not verify")
	if str(claims.get("htm") or "").upper() != str(method or "").upper():
		raise Refused("DPoP htm mismatch")
	if urlsplit(str(claims.get("htu") or "")).path.rstrip("/") != str(path or "").rstrip("/"):
		raise Refused("DPoP htu mismatch")
	try:
		iat = float(claims.get("iat"))
	except (TypeError, ValueError):
		raise Refused("DPoP iat missing") from None
	if abs(_now_epoch() - iat) > SKEW_SECONDS:
		raise Refused("DPoP iat outside the clock window")
	if str(claims.get("bh") or "") != sha256_b64u(body or b""):
		raise Refused("DPoP body hash mismatch")
	if access_token and str(claims.get("ath") or "") != sha256_b64u(access_token.encode()):
		raise Refused("DPoP ath mismatch")
	jti = str(claims.get("jti") or "")
	if len(jti) < 16:
		raise Refused("DPoP jti missing")
	if _seen(jti):
		raise Refused("DPoP jti replayed")
	return jwk


# ── tokens ──────────────────────────────────────────────────────────────────
def _stamp(seconds: int = 0) -> str:
	return str(frappe.utils.add_to_date(frappe.utils.now(), seconds=seconds))[:19]


def issue_token(
	kind: str,
	user: str,
	*,
	seconds: int,
	device: str = "",
	jkt: str = "",
	client: str = "",
	scopes: str = "",
	family: str = "",
) -> str:
	"""Store the hash of a fresh token; return the token ONCE."""
	token = secrets.token_urlsafe(32)
	doc = frappe.get_doc(
		{
			"doctype": TOKEN_DOCTYPE,
			"token_hash": hash_token(token),
			"kind": kind,
			"user": user or None,
			"device": device or None,
			"client": client or None,
			"jkt": jkt or None,
			"scopes": scopes or None,
			"family": family or None,
			"issued_at": _stamp(),
			"expires_at": _stamp(seconds),
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return token


def find_token(token: str, kind: str):
	"""The live row for a token of this kind, or None: unexpired, unrevoked, unused."""
	if not token or not compat.doctype_exists(TOKEN_DOCTYPE):
		return None
	row = frappe.db.get_value(
		TOKEN_DOCTYPE,
		{"token_hash": hash_token(token), "kind": kind},
		[
			"name",
			"user",
			"device",
			"client",
			"jkt",
			"scopes",
			"family",
			"expires_at",
			"used_at",
			"revoked_at",
		],
		as_dict=True,
	)
	if not row or row.get("revoked_at") or row.get("used_at"):
		return None
	if str(row.get("expires_at") or "") <= _stamp():
		return None
	return row


def spend(row) -> None:
	frappe.db.set_value(TOKEN_DOCTYPE, row["name"], "used_at", _stamp(), update_modified=False)


def revoke_tokens(*, device: str = "", client: str = "", family: str = "", reason: str = "") -> int:
	if not compat.doctype_exists(TOKEN_DOCTYPE):
		return 0
	filters = {"revoked_at": ["is", "not set"]}
	if device:
		filters["device"] = device
	elif client:
		filters["client"] = client
	elif family:
		filters["family"] = family
	else:
		return 0
	names = frappe.db.get_all(TOKEN_DOCTYPE, filters=filters, pluck="name", limit=5000)
	for name in names:
		frappe.db.set_value(
			TOKEN_DOCTYPE,
			name,
			{"revoked_at": _stamp(), "revoked_reason": reason[:140]},
			update_modified=False,
		)
	return len(names)


# ── device rows ─────────────────────────────────────────────────────────────
def _row(device: str) -> dict | None:
	if not device or not frappe.db.exists("DocType", DEVICE):
		return None
	fields = [
		"name",
		"parent",
		"enrollment_status",
		"key_thumbprint",
		"unlock_public_key",
		"proof_public_key",
		"device_name",
	]
	return frappe.db.get_value(DEVICE, {"name": device, "parenttype": GRANT}, fields, as_dict=True)


def _user_of(row) -> str:
	return str(frappe.db.get_value(GRANT, row.get("parent"), "user") or "")


def _live(row) -> str:
	"""The user if this device may act now: Enrolled, grant Active, login enabled. Else ""."""
	if not row or str(row.get("enrollment_status") or "") != device_enrollment.ENROLLED:
		return ""
	if str(frappe.db.get_value(GRANT, row.get("parent"), "state") or "") != "Active":
		return ""
	user = _user_of(row)
	if not user or not int(frappe.db.get_value("User", user, "enabled") or 0):
		return ""
	return user


def _set_row(row_name: str, values: dict) -> None:
	for key, value in values.items():
		if compat.has_field(DEVICE, key):
			frappe.db.set_value(DEVICE, row_name, key, value, update_modified=False)


def _bind_keys(row_name: str, body: dict, proof_jwk: dict, approval_method: str, by: str = "") -> dict:
	unlock = jwk_of(body.get("unlock_public_key"))
	proof = jwk_of(body.get("proof_public_key"))
	if thumbprint(proof) != thumbprint(proof_jwk):
		raise Refused("proof key in the body is not the key that signed the request")
	protection = str(body.get("key_protection") or "").strip().lower()
	platform = str(body.get("platform") or "").strip().lower()
	values = {
		"unlock_public_key": json.dumps(unlock, sort_keys=True),
		"proof_public_key": json.dumps(proof, sort_keys=True),
		"key_thumbprint": thumbprint(proof),
		"key_protection": protection if protection in KEY_PROTECTIONS else "software",
		"platform": platform if platform in PLATFORMS else None,
		"os_version": str(body.get("os_version") or "")[:60] or None,
		"app_version": str(body.get("app_version") or "")[:60] or None,
		"approval_method": approval_method,
		"approved_by": by or None,
		"approved_at": _stamp(),
	}
	_set_row(row_name, values)
	return values


def _answer(user: str, device: str, jkt: str, extra: dict | None = None) -> dict:
	seconds = reauth_minutes() * 60
	token = issue_token("access", user, seconds=seconds, device=device, jkt=jkt)
	return {
		"user": user,
		"device": device,
		"access_token": token,
		"token_type": "FarmOps",
		"expires_in": seconds,
		"reauth_seconds": seconds,
		"server_time": int(_now_epoch()),
		**(extra or {}),
	}


# ── §3 pickup links ─────────────────────────────────────────────────────────
def issue_link(user: str, device_name: str = "", minutes=None, issued_by: str = "") -> dict:
	"""Open a single-use pickup link. The nonce exists in plaintext only in this answer."""
	if not enabled():
		raise device_enrollment.EnrollmentRefused(
			"device keys are off on this site (ERPNext MCP Settings → device_keys_enabled). "
			"Use open_device_enrollment or a login card until they are turned on. Nothing was written."
		)
	minutes = link_minutes(minutes)
	opened = device_enrollment.open_enrollment(user, device_name=device_name, hours=1, issued_by=issued_by)
	expires = _stamp(minutes * 60)
	_set_row(opened["device"], {"enrollment_expires_at": expires, "approval_method": "pickup_link"})
	base = settings.mobile_base_url() if hasattr(settings, "mobile_base_url") else ""
	link = f"{(base or '').rstrip('/')}{PICKUP_PATH}{opened['token']}"
	return {"user": user, "device": opened["device"], "link": link, "expires_at": expires, "minutes": minutes}


def redeem(nonce: str, body: dict, proof_jwk: dict) -> dict:
	"""Spend a pickup nonce with the phone's two public keys. Raises Refused (uniform 404)."""
	nonce = str(nonce or "").strip()
	if not nonce or len(nonce) > device_enrollment.MAX_TOKEN_LENGTH:
		raise Refused("no nonce")
	found = frappe.db.get_value(
		DEVICE,
		{"enrollment_token": device_enrollment.hash_token(nonce), "parenttype": GRANT},
		["name", "parent", "enrollment_status", "enrollment_expires_at"],
		as_dict=True,
		for_update=True,
	)
	if not found or str(found.get("enrollment_status") or "") != device_enrollment.PENDING:
		raise Refused("unknown or used nonce")
	if str(found.get("enrollment_expires_at") or "") <= _stamp():
		raise Refused("expired nonce")
	jkt = thumbprint(proof_jwk)
	unlock = jwk_of(body.get("unlock_public_key"))
	if not verify_es256(
		unlock, f"farmops-enroll|{nonce}|{jkt}".encode(), str(body.get("unlock_signature") or "")
	):
		raise Refused("unlock signature does not verify")
	grant_state = str(frappe.db.get_value(GRANT, found["parent"], "state") or "")
	user = str(frappe.db.get_value(GRANT, found["parent"], "user") or "")
	if grant_state != "Active" or not user or not int(frappe.db.get_value("User", user, "enabled") or 0):
		raise Refused("account not active")
	_bind_keys(found["name"], body, proof_jwk, "pickup_link")
	now = _stamp()
	_set_row(
		found["name"],
		{
			"enrollment_status": device_enrollment.ENROLLED,
			"enrollment_token": "",
			"enrolled_at": now,
			"last_seen_on": now,
			"device_name": str(body.get("device_name") or "")[:120] or None,
		},
	)
	return _answer(user, found["name"], jkt, {"base_url": settings.mobile_base_url() or None})


# ── §4.2 challenge and token ────────────────────────────────────────────────
def challenge(device: str, proof_jwk: dict) -> dict:
	row = _row(device)
	user = _live(row)
	if not user or str(row.get("key_thumbprint") or "") != thumbprint(proof_jwk):
		raise Refused("device not live or not this key", revoked_device=_revoked_by_key(row, proof_jwk))
	value = issue_token(
		"challenge", user, seconds=CHALLENGE_SECONDS, device=device, jkt=thumbprint(proof_jwk)
	)
	return {"challenge": value, "server_time": int(_now_epoch()), "expires_in": CHALLENGE_SECONDS}


def mint(device: str, value: str, signature: str, proof_jwk: dict) -> dict:
	row = _row(device)
	user = _live(row)
	jkt = thumbprint(proof_jwk)
	if not user or str(row.get("key_thumbprint") or "") != jkt:
		raise Refused("device not live or not this key", revoked_device=_revoked_by_key(row, proof_jwk))
	found = find_token(value, "challenge")
	if not found or found.get("device") != device:
		raise Refused("challenge unknown, used or expired")
	spend(found)
	unlock = jwk_of(row.get("unlock_public_key"))
	if not verify_es256(unlock, f"farmops-token|{value}|{device}|{jkt}".encode(), signature):
		raise Refused("unlock signature does not verify")
	return _answer(user, device, jkt)


def _revoked_by_key(row, proof_jwk) -> bool:
	"""Only a caller holding the device's own proof key learns it was revoked (§4.3)."""
	return bool(
		row
		and str(row.get("enrollment_status") or "") == device_enrollment.REVOKED
		and str(row.get("key_thumbprint") or "") == thumbprint(proof_jwk)
	)


# ── §4.3 every request ──────────────────────────────────────────────────────
def resolve(headers, method: str, path: str, body: bytes) -> tuple:
	"""(user, device) for a key-bound request, or raises Refused. Revocation read every call."""
	authorization = str(headers.get("Authorization") or "")
	if not authorization.startswith("FarmOps "):
		raise Refused("not a key-bound request")
	token = authorization[len("FarmOps ") :].strip()
	proof_jwk = verify_dpop(str(headers.get("DPoP") or ""), method, path, body, access_token=token)
	jkt = thumbprint(proof_jwk)
	found = find_token(token, "access")
	if not found or str(found.get("jkt") or "") != jkt:
		raise Refused("token unknown, expired, revoked or bound to another key")
	row = _row(str(found.get("device") or ""))
	user = _live(row)
	if not user:
		raise Refused("device no longer live", revoked_device=_revoked_by_key(row, proof_jwk))
	try:
		frappe.local.erpnext_mcp_device = row["name"]
	except Exception:  # pragma: no cover
		pass
	return user, row["name"]


# ── §4.5 the silent upgrade ─────────────────────────────────────────────────
def upgrade(user: str, device: str, body: dict) -> dict:
	"""Bind keys to the legacy device row that authenticated this call; destroy its secret."""
	if not enabled():
		raise device_enrollment.EnrollmentRefused("device keys are off on this site. Nothing was changed.")
	row = _row(device)
	if not row or _user_of(row) != user or not _live(row):
		raise device_enrollment.EnrollmentRefused(
			"this call did not come from a live device of yours. Nothing was changed."
		)
	proof = jwk_of(body.get("proof_public_key"))
	jkt = thumbprint(proof)
	# Signed over the api_key the phone holds: it knows that, and not the name
	# of the row its key lives on.
	api_key = str(frappe.db.get_value(DEVICE, device, "api_key") or "")
	if not api_key:
		raise device_enrollment.EnrollmentRefused(
			"this phone has no legacy key to move from. Nothing was changed."
		)
	message = f"farmops-upgrade|{api_key}|{jkt}".encode()
	if not verify_es256(proof, message, str(body.get("proof_signature") or "")):
		raise device_enrollment.EnrollmentRefused(
			"the proof key signature does not verify. Nothing was changed."
		)
	unlock = jwk_of(body.get("unlock_public_key"))
	if not verify_es256(unlock, message, str(body.get("unlock_signature") or "")):
		raise device_enrollment.EnrollmentRefused(
			"the unlock key signature does not verify. Nothing was changed."
		)
	_bind_keys(device, body, proof, "upgraded", by=user)
	# The secret is destroyed: from here this phone can only sign.
	grant = frappe.get_doc(GRANT, row["parent"])
	for child in grant.get(device_enrollment.TABLE_FIELD) or []:
		if child.name == device:
			legacy_key = str(child.get("api_key") or "")
			child.api_secret = ""
			child.api_key = ""
			if legacy_key:
				device_enrollment._clear_matching_user_key(legacy_key)
	grant.flags.ignore_permissions = True
	grant.save(ignore_permissions=True)
	# No token in this answer: the mobile transport strips every token-shaped
	# key on the way out, on purpose. The phone signs in next with
	# /auth/challenge and /auth/token, inside the same Face ID context.
	return {"upgraded": True, "user": user, "device": device, "next": "/farmops/api/auth/challenge"}


# ── §7 inventory and lost devices ───────────────────────────────────────────
def inventory(user: str = "", include_revoked: bool = False) -> list:
	if not frappe.db.exists("DocType", DEVICE):
		return []
	fields = compat.existing_fields(
		DEVICE,
		(
			"name",
			"parent",
			"device_name",
			"enrollment_status",
			"enrolled_at",
			"last_seen_on",
			"app_version",
			"api_base_mode",
			"platform",
			"os_version",
			"key_protection",
			"approval_method",
			"approved_by",
			"approved_at",
			"last_ip",
			"revoked_on",
			"revocation_reason",
		),
	)
	filters = {"parenttype": GRANT}
	if user:
		filters["parent"] = user
	if not include_revoked:
		filters["enrollment_status"] = ["!=", device_enrollment.REVOKED]
	rows = frappe.db.get_all(
		DEVICE, filters=filters, fields=[*fields, "api_key", "key_thumbprint"], limit=2000
	)
	out = []
	for row in rows:
		item = {key: row.get(key) for key in fields if key not in ("parent",)}
		item["user"] = frappe.db.get_value(GRANT, row.get("parent"), "user") or row.get("parent")
		item["kind"] = "phone"
		item["key_bound"] = bool(row.get("key_thumbprint"))
		item["legacy_secret"] = bool(row.get("api_key")) and not row.get("key_thumbprint")
		out.append(item)
	out.sort(key=lambda item: (str(item["user"]), str(item.get("device_name") or "")))
	return out


def report_lost(user: str, device: str = "", by: str = "", note: str = "") -> dict:
	"""Revoke one device (or every device of a user) as LOST, kill its tokens, alert."""
	from . import security_alerts

	reason = ("lost" + (f": {note}" if note else ""))[:140]
	if device:
		answer = device_enrollment.revoke(user, device, reason, revoked_by=by)
		devices = [device] if answer.get("revoked") else []
	else:
		before = [
			item["name"]
			for item in inventory(user)
			if item.get("enrollment_status") != device_enrollment.REVOKED
		]
		device_enrollment.revoke_all(user, reason, revoked_by=by)
		devices = before
	tokens = sum(revoke_tokens(device=name, reason="device lost") for name in devices)
	for name in devices:
		_set_row(name, {"revocation_kind": "lost"})
	security_alerts.send(
		"Farm Ops: a phone was reported lost",
		f"{by or 'Somebody'} reported {len(devices)} device(s) of {user} lost and revoked them ({tokens} token(s) ended).",
	)
	return {"user": user, "revoked_devices": devices, "tokens_revoked": tokens}
