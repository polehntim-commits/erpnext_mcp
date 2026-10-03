# SPDX-License-Identifier: MIT
"""Who is calling this service. One header, one verifier, one answer.

    X-FarmOps-Token: <api_key>:<api_secret>

THE VERIFIER IS v0.17.2's, NOT A NEW ONE. `fallback_auth.verify_credential`
looks the `api_key` up on User, reads the stored `api_secret` back through
Frappe's own accessor, compares with `hmac.compare_digest`, refuses a disabled
account, and meters wrong answers on a counter shared with the whitelisted path.
That function is the whole of what this module trusts; everything below it is
about reading a header.

WHY THE FALLBACKS COME ALONG. The header is the documented carrier and the one
the shipped app already sends on every request. `Authorization: token …` is read
too because it costs one line and because the LAN path and every `curl` an
operator will type reach for it first. `_auth` in the JSON body is read last for
the reason v0.17.2 gave: a layer that eats one custom header may eat another,
and a layer that rewrites a request body is not a proxy, it is an attack.

Reading all three also means **the shipped iOS build needs no new auth work.**
It already sends the pair three ways; only the URL it sends them to changes.

A WRONG ANSWER IS ANONYMOUS, NOT AN ERROR — same as Frappe, same as v0.17.2.
`resolve` returns "" for a malformed header, an unknown key, a wrong secret and
a disabled account alike, and `app.py` turns all four into the identical 401.
A caller learns whether it is in, and nothing else.
"""

from __future__ import annotations

import json

from ..api import fallback_auth

#: The header the app sends. Same name and same format as the whitelisted path's
#: second door, deliberately: one credential, one spelling, wherever it lands.
HEADER = fallback_auth.HEADER

#: Frappe's own, read as well. See the module docstring.
AUTHORIZATION = "Authorization"

#: A body this large is not searched for `_auth`. `stage_file_chunk` sends 512 KB
#: slices base64-encoded, so the ceiling has to clear a real chunk with room to
#: spare without letting a caller make the server parse an unbounded document
#: before it has authenticated anybody.
MAX_BODY_BYTES = 4_000_000


def presented(headers, body) -> tuple:
	"""(api_key, api_secret, source) from the request. Header first, body last.

	`headers` is anything with a case-insensitive `.get` — a Werkzeug
	`EnvironHeaders` on a real request, a plain dict in the suite. `body` is the
	already-parsed JSON object, or `{}` for a body that was not JSON, which is
	itself a perfectly ordinary thing for an unauthenticated caller to send.
	"""
	raw = str(headers.get(HEADER) or "").strip()
	if raw:
		api_key, api_secret = fallback_auth.split_token(raw)
		if api_key and api_secret:
			return api_key, api_secret, fallback_auth.SOURCE_HEADER

	raw = str(headers.get(AUTHORIZATION) or "").strip()
	if raw:
		api_key, api_secret = fallback_auth.split_token(raw)
		if api_key and api_secret:
			return api_key, api_secret, "authorization"

	auth = body.get(fallback_auth.BODY_KEY) if isinstance(body, dict) else None
	if isinstance(auth, str):
		try:
			auth = json.loads(auth)
		except Exception:
			auth = None
	if isinstance(auth, dict):
		api_key = str(auth.get("api_key") or "").strip()
		api_secret = str(auth.get("api_secret") or "").strip()
		if not (api_key and api_secret):
			api_key, api_secret = fallback_auth.split_token(str(auth.get("token") or ""))
		if api_key and api_secret:
			return api_key, api_secret, fallback_auth.SOURCE_BODY

	return "", "", ""


def resolve(headers, body, method: str = "", path: str = "", raw: bytes = b"") -> tuple:
	"""(user, source). `("", "")` means nobody, whichever way it failed.

	Called with a live Frappe connection — the verifier reads `User` and the
	encrypted `api_secret` out of the database — and BEFORE any user has been
	set, so it runs as whatever `frappe.connect()` left the session as. That is
	fine and is what Frappe's own api-key validator does: reading a User row to
	decide who somebody is cannot itself be gated on knowing who they are.
	"""
	# v0.218.0. A key-bound request (`Authorization: FarmOps <token>` + `DPoP`).
	# Only when device keys are on; a Refused is anonymous, like every failure
	# here — except a provably revoked device, which the caller reads from
	# `revoked_device` to tell the phone to wipe itself (§4.3).
	# v0.220.0: also as `X-FarmOps-Authorization`, which no proxy strips.
	key_bound = str(headers.get(AUTHORIZATION) or "")
	if not key_bound.startswith("FarmOps "):
		key_bound = str(headers.get("X-FarmOps-Authorization") or "")
	if key_bound.startswith("FarmOps "):
		from .. import device_keys

		if not device_keys.enabled():
			return "", ""
		try:
			user, _device = device_keys.resolve(headers, method, path, raw)
			return user, "device_key"
		except device_keys.Refused as exc:
			if exc.revoked_device:
				return "", "revoked_device"
			return "", ""
	api_key, api_secret, source = presented(headers, body)
	if not (api_key and api_secret):
		return "", ""
	user = fallback_auth.verify_credential(api_key, api_secret)
	if not user:
		return "", ""
	return user, source
