# SPDX-License-Identifier: MIT
"""The WSGI application: read the request, authenticate, delegate, answer JSON.

    POST /farmops/api/mobile/<method>
    POST /farmops/api/files/<method>
    X-FarmOps-Token: <api_key>:<api_secret>
    Content-Type: application/json

────────────────────────────────────────────────────────────────────────────
THE ONE PROMISE THIS FILE MAKES
────────────────────────────────────────────────────────────────────────────

**EVERY ANSWER IS `application/json`. THERE IS NO PATH OUT OF HERE THAT RENDERS
HTML AND NO PATH THAT REDIRECTS.** That is not a style preference, it is the
entire reason the service exists: v0.17.x's failure was a phone receiving HTTP
200 and the Desk's login page, and the app reporting it as a decoding error —
the least useful true thing it could have said. A 404, a 405, a 401, an
unhandled `KeyError` in a tool three layers down: all of them leave this module
as a JSON object with a status code, or the release has not fixed anything.

`_failure` is the only way a non-2xx leaves this file, and `dispatch` is wrapped
so that even a bug in this module's own error handling produces JSON.

THE ONE EXCEPTION IS `GET /mobile/login_qr_image`'s 200. It answers
`image/png` bytes, not JSON, because its entire reason to exist is that a
`png_base64` field decoded and re-saved by a human at a terminal was reliably
producing black or corrupt files — see `_login_qr_image`. Every refusal it can
give still leaves as `_failure`'s JSON, and it is the only route on this
surface that is GET, unauthenticated-by-header-only (no body to carry `_auth`
in), and restricted to an admin role rather than a scoped worker.

v0.167.0 ADDS A SECOND: `GET /tiles/slope_aspect/{z}/{x}/{y}.png`, the slope
aspect map tiles, which answer `image/png` because `MKTileOverlay` asks for an
image per tile and nothing else. Same rule: every refusal is still JSON.
v0.168.0 adds `GET /tiles/slope_grade/{z}/{x}/{y}.png[?asset=<docname>]` beside
it, through the same gates.

────────────────────────────────────────────────────────────────────────────
THE ERROR ENVELOPE IS FRAPPE'S, BECAUSE THE APP ALREADY READS FRAPPE'S
────────────────────────────────────────────────────────────────────────────

`FrappeClient.serverMessage` — the shipped iOS build, already in TestFlight —
looks for `_server_messages` first (a JSON string holding an array of JSON
strings, each with a `message` key), then `exception`, then `message`. So a
refusal from here emits `_server_messages` in exactly that shape, and the
sentence a wrapper was written to say reaches the phone as itself:

    "A reason is required to hand a task back. 'The ladder is broken and I
     could not reach the detector' is a fact somebody can act on…"

`error` is emitted alongside it because the v0.18.0 brief asks for `{"error": …}`
and because a `curl` is easier to read that way. Three keys, one message, no
client change.

────────────────────────────────────────────────────────────────────────────
THE STATUS CODES ARE A CONTRACT WITH THE APP, NOT DECORATION
────────────────────────────────────────────────────────────────────────────

`FarmOpsKit` treats **401 as "this credential is dead — sign out"**, which
discards the offline queue. Everything else it treats as "still signed in, try
later". So the mapping below is deliberate and narrow:

  * **401** — and only — for a credential that did not verify.
  * **403** for the role gate, the enrolment gate and entity scoping: refused,
    but the login is real and the queued day's work is not thrown away.
  * **404** for a docname that does not exist *or* is not this caller's, which
    `guard.require_scoped_doc` deliberately makes indistinguishable.
  * **429** rate limit, **503** kill switch — the two that must never be 401,
    or one flipped switch signs forty phones out and loses what is on them.
  * **400** for everything else a caller can correct, **500** for anything else,
    with the detail in the log and not in the answer.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
import traceback

import frappe
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.wrappers import Request, Response

from .. import audit, device_enrollment, security, security_alerts, slope_aspect, slope_grade, upload_links
from ..api import fallback_auth, guard
from ..errors import ToolError
from ..tools import employee as personnel
from ..tools import mobile as mobile_tools
from . import auth, session
from .routes import BY_PATH, PREFIX, bind

logger = logging.getLogger("farmops_api")

#: Where the service listens inside the container. Nothing new is published to
#: the LAN — see `RELEASES/v0.18.0.md` for how the funnel reaches it.
DEFAULT_PORT = 5250

#: Answered unauthenticated, and says nothing about the site. It exists so that
#: "the funnel path is wrong" and "the credential is wrong" are two different
#: answers at the point somebody is standing in a field trying to tell them
#: apart. It touches no database and opens no Frappe session.
HEALTH_PATH = f"{PREFIX}/health"

#: `GET /mobile/login_qr_image?user=<email>` — the one route on this surface
#: that answers a PNG. See `_login_qr_image` and the module docstring.
QR_IMAGE_PATH = f"{PREFIX}/mobile/login_qr_image"

#: `list_sidecar_routes` describes this app by reading `routes.ROUTES`, and
#: this path is not in that table — it is not a POST, does not go through
#: `routes.bind`, and its handler wears none of `guard.endpoint`'s attributes.
#: WITHOUT THIS, THE DIAGNOSTIC LIES BY OMISSION: an operator auditing "what
#: does the sidecar publish" would not see the one route that mints a login
#: credential. `tools/diagnostics.py` merges this in by hand.
DESCRIBED_ROUTE = {
	"path": QR_IMAGE_PATH,
	"method": "login_qr_image",
	"group": "mobile",
	"mutating": True,
	"arguments": ["user"],
}

#: The same sentence for every way authentication can fail — malformed header,
#: unknown key, wrong secret, disabled account. A caller learns whether it is
#: in, and nothing else; telling it which fact was wrong hands it a free oracle
#: for the one fact worth probing.
UNAUTHORIZED = (
	"This request carried no usable Farm Ops credential. Send "
	"`X-FarmOps-Token: <api_key>:<api_secret>` from the login QR. Nothing was read "
	"and nothing was changed."
)

#: `GET /tiles/slope_aspect/{z}/{x}/{y}.png` — see `_slope_aspect_tile`.
TILE_PREFIX = f"{PREFIX}/tiles/slope_aspect/"
_TILE_TAIL = re.compile(r"^(\d{1,2})/(\d{1,9})/(\d{1,9})\.png$")

#: Tiles a minute per caller. A map screen asks for twenty to sixty tiles at
#: once and as many again on every pan, so `guard.READ_LIMIT` (60) would blank
#: the layer on the second swipe. This still stops a scraper.
TILE_LIMIT = 1200

TILE_DESCRIBED_ROUTE = {
	"path": f"{TILE_PREFIX}{{z}}/{{x}}/{{y}}.png",
	"method": "slope_aspect_tile",
	"group": "tiles",
	"mutating": False,
	"arguments": ["z", "x", "y"],
}

#: `GET /tiles/slope_grade/{z}/{x}/{y}.png[?asset=]` — see `_slope_grade_tile`. v0.168.0.
GRADE_TILE_PREFIX = f"{PREFIX}/tiles/slope_grade/"

GRADE_TILE_DESCRIBED_ROUTE = {
	"path": f"{GRADE_TILE_PREFIX}{{z}}/{{x}}/{{y}}.png",
	"method": "slope_grade_tile",
	"group": "tiles",
	"mutating": False,
	"arguments": ["z", "x", "y", "asset"],
}

#: `POST /mobile/enroll_device` — the one route a phone calls BEFORE it holds a
#: credential. v0.175.0. See `_enroll_device` and `device_enrollment.exchange`.
ENROLL_PATH = device_enrollment.ENROLL_PATH

ENROLL_DESCRIBED_ROUTE = {
	"path": ENROLL_PATH,
	"method": "enroll_device",
	"group": "mobile",
	"mutating": True,
	"arguments": ["token", "device_name", "device_identifier"],
}

#: Refused exchanges a minute from one address before the route stops listening
#: to it. A token is 256 bits, so this is not what stops a guesser — it stops a
#: caller making the server hash and look up an unbounded stream of junk. Only
#: FAILURES count, for the reason `fallback_auth` meters only failures: forty
#: phones enrolling through one funnel address must never lock each other out.
ENROLL_FAILURE_LIMIT = 30

#: `GET /scan/<code>` — what a printed tag's QR opens in a browser. v0.216.0.
#: See `_scan_page`.
SCAN_PREFIX = f"{PREFIX}/scan/"

SCAN_DESCRIBED_ROUTE = {
	"path": f"{SCAN_PREFIX}{{code}}",
	"method": "scan_page",
	"group": "scan",
	"mutating": False,
	"arguments": ["code"],
}

#: `PUT|POST /upload/<token>` — one file onto one document, once. v0.244.0. See `_upload`
#: and `erpnext_mcp.upload_links`. THE ONLY ROUTE LET PAST `_MAX_BODY`, and only after
#: its token is proved.
UPLOAD_PREFIX = f"{PREFIX}/upload/"

#: Upload requests a minute from one address, good token or bad.
UPLOAD_LIMIT = 30

#: Unknown tokens an hour from one address that raise the alert (audited once).
UPLOAD_BAD_TOKEN_ALERT = 10

#: Room for the multipart envelope around a file at the link's own cap.
MULTIPART_SLACK = 64 * 1024

#: What a declared Content-Type may be for each extension. `application/octet-stream`
#: (or none) is always accepted: plenty of clients send nothing better, and the
#: first bytes are checked whatever is declared.
_DECLARED_TYPES = {
	"jpg": ("image/jpeg",), "jpeg": ("image/jpeg",), "png": ("image/png",),
	"heic": ("image/heic", "image/heif"), "heif": ("image/heif", "image/heic"),
	"pdf": ("application/pdf",),
}
_EXTENSION_FOR_TYPE = {
	"image/jpeg": "jpg", "image/png": "png", "image/heic": "heic", "image/heif": "heif",
	"application/pdf": "pdf",
}

#: `GET /brand/<company>` — the company's logo, for email. v0.216.1. See `_brand_image`.
BRAND_PREFIX = f"{PREFIX}/brand/"

BRAND_DESCRIBED_ROUTE = {
	"path": f"{BRAND_PREFIX}{{company}}",
	"method": "brand_image",
	"group": "brand",
	"mutating": False,
	"arguments": ["company"],
}

#: The magic numbers of the only things `_brand_image` will serve.
_IMAGE_SIGNATURES = (
	(b"\x89PNG\r\n\x1a\n", "image/png"),
	(b"\xff\xd8\xff", "image/jpeg"),
	(b"GIF87a", "image/gif"),
	(b"GIF89a", "image/gif"),
)

#: A logo larger than this is not served (a mail client would not want it either).
BRAND_MAX_BYTES = 2_000_000

#: v0.218.0 device keys — see `_device_key_route`.
PICKUP_PREFIX = f"{PREFIX}/enroll/"
CHALLENGE_PATH = f"{PREFIX}/auth/challenge"
TOKEN_PATH = f"{PREFIX}/auth/token"
#: v0.219.0. A new phone asks; the same phone collects (§6.2). OFF until phone_approval_enabled.
ACCESS_REQUEST_PATH = f"{PREFIX}/access/request"
ACCESS_STATUS_PATH = f"{PREFIX}/access/status"

_PICKUP_PAGE = (
	"Farm Ops sign-in code\n\n"
	"Open the Farm Ops app and scan this code from its sign-in screen.\n\n"
	"Abre la aplicación Farm Ops y escanea este código desde la pantalla de inicio de sesión.\n"
)

PICKUP_DESCRIBED_ROUTE = {
	"path": f"{PICKUP_PREFIX}{{nonce}}",
	"method": "enroll_pickup",
	"group": "auth",
	"mutating": True,
	"arguments": [
		"device_name",
		"platform",
		"os_version",
		"app_version",
		"unlock_public_key",
		"proof_public_key",
		"key_protection",
		"unlock_signature",
	],
}
CHALLENGE_DESCRIBED_ROUTE = {
	"path": CHALLENGE_PATH,
	"method": "auth_challenge",
	"group": "auth",
	"mutating": True,
	"arguments": ["device"],
}
TOKEN_DESCRIBED_ROUTE = {
	"path": TOKEN_PATH,
	"method": "auth_token",
	"group": "auth",
	"mutating": True,
	"arguments": ["device", "challenge", "signature"],
}
ACCESS_REQUEST_DESCRIBED_ROUTE = {
	"path": ACCESS_REQUEST_PATH,
	"method": "access_request",
	"group": "auth",
	"mutating": True,
	"arguments": [
		"device_name",
		"platform",
		"os_version",
		"app_version",
		"unlock_public_key",
		"proof_public_key",
		"key_protection",
		"unlock_signature",
	],
}
ACCESS_STATUS_DESCRIBED_ROUTE = {
	"path": ACCESS_STATUS_PATH,
	"method": "access_status",
	"group": "auth",
	"mutating": True,
	"arguments": ["request"],
}


#: Every route `routes.ROUTES` cannot describe, for `list_sidecar_routes`.
DESCRIBED_ROUTES = (
	DESCRIBED_ROUTE,
	TILE_DESCRIBED_ROUTE,
	GRADE_TILE_DESCRIBED_ROUTE,
	ENROLL_DESCRIBED_ROUTE,
	SCAN_DESCRIBED_ROUTE,
	BRAND_DESCRIBED_ROUTE,
	PICKUP_DESCRIBED_ROUTE,
	CHALLENGE_DESCRIBED_ROUTE,
	TOKEN_DESCRIBED_ROUTE,
	ACCESS_REQUEST_DESCRIBED_ROUTE,
	ACCESS_STATUS_DESCRIBED_ROUTE,
)

_MAX_BODY = auth.MAX_BODY_BYTES

#: The content types `_multipart` reads. `multipart/mixed` is not one of them:
#: only the form encoding names its parts, and a part with no name has no key to
#: land on.
_MULTIPART = frozenset({"multipart/form-data"})


#: v0.216.0 hardening (docs/design/farmops_only_funnel.md, addendum H1–H4).
TOO_LARGE = "That request is larger than this service accepts."
TOO_MANY = "Too many requests from this address. Wait a minute and try again."
NOT_FOUND = "Not found."

#: Failed authentications a minute from one address before further FAILED
#: attempts get 429. A valid credential is never refused on this count: forty
#: phones behind one carrier NAT must not be locked out by a stranger sharing it.
AUTH_FAILURE_LIMIT = 60

#: Failed authentications a minute from one address that raise the alert.
AUTH_FAILURE_ALERT = 10

#: The alert for one address is raised at most once in this many seconds.
AUTH_ALERT_INTERVAL = 3600

#: Requests a minute from one address to the routes that need no credential
#: (health, the tag page). Both are static; this only stops a flood.
OPEN_LIMIT = 120

#: The Frappe hook an operator's own app can register to hear about H4.
AUTH_ALERT_HOOK = "farmops_auth_alert"


def _peer(request: Request) -> str:
	"""The caller's address: rightmost `X-Forwarded-For` hop, else the socket peer.

	Rightmost for the reason `security.caller_ip` gives — it is the hop the
	nearest proxy appended, and the leftmost is whatever the client typed. Read
	off the request itself so it works before a Frappe session is open.
	"""
	forwarded = str(request.headers.get("X-Forwarded-For") or "")
	peer = str(request.remote_addr or "")
	# v0.217.0: the same rule as the MCP allowlist and the audit rows, trusted
	# proxy ranges included. Before a Frappe session is open the setting cannot
	# be read, and the old rule (rightmost hop) applies.
	try:
		return security.client_ip(forwarded, peer)
	except Exception:
		return security.client_ip(forwarded, peer, trusted=[])


def _open_route_limited(request: Request) -> bool:
	"""True when this address has used up `OPEN_LIMIT` on the credential-less routes."""
	return guard._count(f"open:{_peer(request) or 'unknown'}", 60) > OPEN_LIMIT


def _unauthenticated(request: Request, path: str, body=None) -> Response:
	"""THE ONE ANSWER TO A CALLER NOBODY VOUCHES FOR. H1, H3, H4.

	Called with a session open and before the path or the method has been
	looked at, so a route that exists, one that does not, a GET on a POST route,
	a tile and the login-QR image are indistinguishable from outside: the same
	401, with the same body.

	Counts the failure against the address, logs it, raises the alert the tenth
	time in a minute, and answers 429 once the address is past the limit. Never
	raises — a failure to count is not a reason to answer differently.
	"""
	ip = _peer(request) or "unknown"
	failures = 0
	try:
		api_key, _secret, _source = auth.presented(request.headers, body or {})
		failures = guard._count(f"authfail:{ip}", 60)
		logger.warning(
			"farmops-api auth-failure ip=%s path=%s key=%s n=%s",
			ip,
			path[:120],
			fallback_auth._fingerprint(api_key) if api_key else "-",
			failures,
		)
		if failures == AUTH_FAILURE_ALERT and _first_alert(ip):
			_raise_auth_alert(ip, failures, path)
	except Exception:  # pragma: no cover - counting must never change the answer
		logger.error("farmops-api auth-failure accounting failed\n%s", traceback.format_exc())
	if failures > AUTH_FAILURE_LIMIT:
		return _failure(429, TOO_MANY)
	return _failure(401, UNAUTHORIZED)


def _alert_listeners() -> list:
	"""The callables registered under the `farmops_auth_alert` hook. Never raises."""
	try:
		return [frappe.get_attr(method) for method in frappe.get_hooks(AUTH_ALERT_HOOK) or []]
	except Exception:
		return []


#: Address → when this worker last raised its alert. Bounded; see `_first_alert`.
_ALERTED: dict = {}


def _first_alert(ip: str) -> bool:
	"""True once per address per `AUTH_ALERT_INTERVAL`.

	This worker's own memory first, then the shared cache where there is one —
	so a bench with redis raises one alert an hour per address, and one without
	raises at most one per worker.
	"""
	now = time.time()
	if now - _ALERTED.get(ip, 0) < AUTH_ALERT_INTERVAL:
		return False
	if len(_ALERTED) > 2000:
		_ALERTED.clear()
	_ALERTED[ip] = now
	client = fallback_auth._client()
	if client is None:
		return True
	try:
		slot = f"erpnext_mcp:farmops:authalert:{ip}:{int(now // AUTH_ALERT_INTERVAL)}"
		hits = int(client.incr(slot))
		if hits == 1:
			client.expire(slot, AUTH_ALERT_INTERVAL * 2)
		return hits == 1
	except Exception:  # pragma: no cover - no redis on this bench
		return True


def _raise_auth_alert(ip: str, failures: int, path: str) -> None:
	"""One audit row, the hook, and an email if an address is configured. H4.

	At most once an hour per address — the caller holds that — so an
	unauthenticated flood cannot grow MCP Action Log or a mailbox without bound,
	which is the argument that kept refusals out of the log until now.
	"""
	payload = {
		"ip": ip,
		"failures": failures,
		"window_seconds": 60,
		"path": path[:120],
		"at": str(frappe.utils.now()),
	}
	summary = (
		f"Blocked — {failures} failed Farm Ops sign-ins from {ip} in one minute "
		f"(last path {payload['path']}). Further failures from this address are counted, not logged here."
	)
	try:
		audit.record(
			"mobile:auth_failures", payload, audit.STATUS_BLOCKED, summary, caller_ip=ip, commit=True
		)
	except Exception:  # pragma: no cover
		logger.error("farmops-api auth alert: audit row failed\n%s", traceback.format_exc())
	for listener in _alert_listeners():
		try:
			listener(dict(payload))
		except Exception:
			logger.error("farmops-api auth alert hook %r failed\n%s", listener, traceback.format_exc())
	# v0.216.1: Security Alert Recipients (empty → the System Managers). Until
	# then this read the drift report's field, emailed nobody when it was empty
	# and sent a comma-separated list as one address.
	try:
		if security_alerts.send("Farm Ops: repeated failed sign-ins", summary):
			frappe.db.commit()
	except Exception:  # pragma: no cover - mail is best effort
		logger.error("farmops-api auth alert: mail failed\n%s", traceback.format_exc())


# ── the answer shapes ───────────────────────────────────────────────────────
def _json(payload: dict, status: int = 200) -> Response:
	"""One JSON response. The only kind this module knows how to build."""
	return Response(
		json.dumps(payload, default=str),
		status=status,
		content_type="application/json",
		# A JSON API has no business being sniffed as anything else, and this
		# service is reachable from the public internet through the funnel.
		headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
	)


def _success(data) -> Response:
	"""`{"message": …}` — the envelope `FrappeClient.unwrapMessage` unwraps.

	Identical to what the whitelisted path returns for the same call, because it
	IS the same call: `routes.py` delegates to the `@guard.endpoint` function and
	this wraps its return value the one way Frappe would have wrapped it.
	"""
	return _json({"message": data})


def _failure(status: int, message: str, exception: str = "", error_key: str = "") -> Response:
	"""A refusal the phone can read, in the three places it looks for one.

	v0.268.0: `error_key` too when the refusal carries a translation key — the phone switches on it (the company
	switcher's `error.mobile.company_not_member`)."""
	extra = {"error_key": error_key} if error_key else {}
	return _json(
		{
			**extra,
			"error": message,
			"exception": exception or message,
			# Frappe's own shape: a JSON *string* containing an array of JSON
			# *strings*, each an object with a `message`. Nested twice, which
			# looks like a mistake and is not — `serverMessage` decodes exactly
			# this, and a flat array would silently produce a phone that shows
			# "Something went wrong" instead of the sentence somebody wrote.
			"_server_messages": json.dumps([json.dumps({"message": message})]),
		},
		status=status,
	)


# ── mapping a refusal to a status ───────────────────────────────────────────
#: What the phone is told when something failed that nobody wrote a sentence for.
#: Deliberately says nothing specific — see `_message_for`.
INTERNAL = (
	"The farm server could not complete that. It has been logged. Nothing was "
	"changed — try again, and if it keeps happening tell the office."
)


def _status_for(exc: Exception) -> int:
	"""Which deliberate refusal this is, or **0 for one nobody anticipated**.

	THE ZERO IS THE POINT AND IT IS NOT A STATUS. "Which HTTP code" and "did
	somebody mean this" are two different questions, and answering the second
	with `>= 500` gets 503 wrong: the kill switch is the most deliberate refusal
	in the whole system and it carries a sentence an operator wrote for a worker
	to read — "the Farm Ops mobile API is switched off on this site" — which a
	500-shaped test would replace with "something went wrong". Asking the
	question separately is what keeps that sentence on the phone.

	ORDER MATTERS BELOW. `MobileDisabled` and `RateLimited` both subclass
	`frappe.ValidationError`, so they have to be asked about before it, or a
	tripped kill switch answers 400 and the app shows a broken request instead
	of "the office turned it off — keep working".
	"""
	if isinstance(exc, guard.MobileDisabled):
		return 503
	if isinstance(exc, guard.RateLimited):
		return 429
	if isinstance(exc, frappe.PermissionError):
		return 403
	if isinstance(exc, frappe.DoesNotExistError):
		return 404
	if isinstance(exc, frappe.ValidationError):
		return 400
	return 0


_TAG = re.compile(r"<[^>]+>")
_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " "}


def _plain(text: str) -> str:
	"""A message as text: tags dropped, the common entities decoded, whitespace collapsed.

	v0.200.0, AFB-2026-00025. ERPNext writes its refusals for the Desk, in HTML —
	"Please enter <b>Difference Account</b> … <strong>Orchard Meadow, LLC</strong>"
	— and a phone draws the tags as characters. Stripped HERE, once, so every
	route's refusal reads as a sentence; the phone strips too, for older servers.
	"""
	plain = _TAG.sub("", str(text or ""))
	for entity, char in _ENTITIES.items():
		plain = plain.replace(entity, char)
	return " ".join(plain.split())


def _message_for(exc: Exception, anticipated: int) -> str:
	"""What to say. An UNANTICIPATED failure says nothing specific, on purpose.

	A refusal somebody wrote carries its own sentence, because those sentences
	were written to be read in a field — they name the argument, the record and
	the fix. An exception nobody anticipated carries none of it: the traceback
	goes to the log where an operator can read it, and the phone gets a fixed
	line, because an internal error message is where table names, file paths and
	query fragments leak out to whoever is holding the phone.
	"""
	if not anticipated:
		return INTERNAL
	text = _plain(str(exc))
	if text:
		return text
	# v0.191.1. Frappe's own permission check (`Document.raise_no_permission_to`)
	# raises a BARE PermissionError and leaves its sentence in
	# `frappe.flags.error_message`. Without this a refusal reached the phone as
	# "That request could not be completed." and nobody could tell it was a
	# permission, let alone which one.
	flagged = _plain(str(getattr(frappe.flags, "error_message", "") or ""))
	if flagged:
		return f"{flagged}. Nothing was changed."
	if anticipated == 403:
		return "This account isn't allowed to do that on this farm's server. Nothing was changed."
	return "That request could not be completed."


# ── the request ─────────────────────────────────────────────────────────────
def _multipart(request: Request) -> dict:
	"""A `multipart/form-data` body, flattened to the same dict a JSON one makes.

	WHY THIS EXISTS AND WHY IT IS A TRANSLATION RATHER THAN A SECOND PATH. Every
	method behind this service takes scalars and base64 strings, because that is
	what a JSON body carries and this transport has been JSON-only since v0.18.0.
	A client that would rather post a file part than base64 it — a curl, a web
	form, anything that is not the iOS build — should not need a second version
	of the method it is calling, and the methods must not each grow a branch on
	how the bytes arrived. So a file part is base64'd HERE and lands on exactly
	the key its own part is named, which is the shape every handler already
	takes: a part named `screenshot` becomes `screenshot`, alongside
	`screenshot_filename` and `screenshot_content_type` from the part's own
	headers.

	NOTHING ABOUT THE SURFACE WIDENS. `routes.bind` still reduces whatever comes
	out of here to the keys the method declares, so a form part naming an
	argument no signature has is dropped exactly as a JSON key would be.

	The same promise as `_body`: never raises, and `{}` for anything unreadable.
	"""
	fields = {}
	try:
		for key in request.form:
			values = request.form.getlist(key)
			# A repeated part is a list, the way a JSON array would be — `roles`
			# arrives that way from a form and as an array from JSON.
			fields[key] = values[0] if len(values) == 1 else values
		for key in request.files:
			part = request.files[key]
			content = part.read()
			if not content:
				continue
			fields[key] = base64.b64encode(content).decode("ascii")
			if part.filename:
				# The BASENAME only. A part naming itself `../../etc/passwd` is
				# `api/files.py`'s fourth refusal, and it is cheaper to make the
				# name harmless here than to trust every handler to.
				fields[f"{key}_filename"] = os.path.basename(str(part.filename))
			if part.mimetype:
				fields[f"{key}_content_type"] = str(part.mimetype)
	except RequestEntityTooLarge:
		raise
	except Exception:  # pragma: no cover - a truncated or hostile multipart body
		return {}
	return fields


def _body(request: Request) -> dict:
	"""The request body as a dict, or `{}`. NEVER raises, never reads a huge one.

	A body that is not JSON is `{}` rather than a 400: the eleven methods all
	have defaults for everything except what their own validation requires, and
	an unauthenticated caller sending nonsense should meet the 401 rather than a
	parser error that confirms the path exists.

	`multipart/form-data` IS THE ONE OTHER SHAPE READ, and it is read into the
	same dict rather than handed to anybody differently — see `_multipart`. The
	promise at the top of this file is about what leaves here, and both doors
	still leave as JSON.
	"""
	try:
		if (request.mimetype or "") in _MULTIPART:
			return _multipart(request)
		# v0.218.0: cached, because a key-bound request's DPoP proof signs the
		# body's hash and `auth.resolve` reads the same bytes again.
		raw = request.get_data(cache=True, as_text=True) or ""
	except RequestEntityTooLarge:
		# v0.216.0 (H2): `dispatch` set the ceiling and answers this with 413.
		raise
	except Exception:  # pragma: no cover - a truncated or hostile body
		return {}
	if not raw.strip().startswith("{"):
		return {}
	try:
		parsed = json.loads(raw)
	except Exception:
		return {}
	return parsed if isinstance(parsed, dict) else {}


def _login_qr_image(request: Request) -> Response:
	"""`GET /mobile/login_qr_image?user=<email>` — the enrolment PNG, as bytes.

	Every other route on this surface answers JSON and scopes its action to the
	CALLER's own account. This one does neither: it mints a fresh login
	credential for the account NAMED in `user`, and answers with the raw PNG
	`tools.mobile.generate_mobile_login_qr` draws — no `png_base64` field for a
	client to decode and re-save, which is the exact round-trip that was
	reliably producing black or corrupt files. Point a browser or `curl` at it
	with the same `X-FarmOps-Token` any other call here takes, and the bytes
	that come back are a working QR every time.

	GATED ON A ROLE, NOT ON A MOBILE GRANT. `guard.endpoint` requires the
	CALLER to hold an Active Mobile Access Grant, which is the right rule for a
	picker working their own scoped tasks and the wrong one here: the person
	asking for this is an office account minting somebody ELSE's credential,
	and may have no grant of their own. `personnel.require_hr_role()` is the
	gate instead — the same one `api/mobile.py` already runs before a personnel
	write reaches the register, checked against the CALLER's own roles: this
	transport never switches to the MCP System User, so `frappe.session.user`
	is the caller for the whole of this call, exactly as it is for every other
	`/mobile/*` route.
	"""
	with session.request_session(request=request, body={}):
		if not guard.mobile_enabled():
			return _failure(503, "The Farm Ops mobile API is switched off on this site.")

		# v0.216.0 (H1): WHO, before WHAT. The method and the argument are only
		# discussed with somebody holding a credential.
		caller, _source = auth.resolve(request.headers, {}, request.method, request.path, b"")
		if not caller:
			return _unauthenticated(request, QR_IMAGE_PATH)

		if request.method != "GET":
			return _failure(405, f"{QR_IMAGE_PATH} is GET only.")

		target = str(request.args.get("user") or "").strip()
		if not target:
			return _failure(400, "login_qr_image needs `?user=<email>`. Nothing was read.")

		ip = security.caller_ip()
		try:
			guard.throttle(caller, "login_qr_image", guard.WRITE_LIMIT)
		except guard.RateLimited as exc:
			return _failure(429, str(exc))

		session.become(caller)
		try:
			personnel.require_hr_role()
		except ToolError as exc:
			audit.record(
				"mobile:login_qr_image",
				{"user": target},
				audit.STATUS_ERROR,
				f"Error — {caller}: {exc}",
				caller_ip=ip,
				commit=True,
			)
			return _failure(400, str(exc))

		try:
			result = mobile_tools.generate_mobile_login_qr({"user": target})
		except ToolError as exc:
			session.rollback()
			audit.record(
				"mobile:login_qr_image",
				{"user": target},
				audit.STATUS_ERROR,
				f"Error — {caller}: {exc}",
				caller_ip=ip,
				commit=True,
			)
			return _failure(400, str(exc))
		except Exception as exc:
			session.rollback()
			anticipated = _status_for(exc)
			audit.record(
				"mobile:login_qr_image",
				{"user": target},
				audit.STATUS_ERROR,
				f"Error — {caller}: {type(exc).__name__}: {exc}",
				caller_ip=ip,
				commit=True,
			)
			if anticipated:
				return _failure(anticipated, _message_for(exc, anticipated), type(exc).__name__)
			logger.error("farmops-api 500 %s caller=%s\n%s", QR_IMAGE_PATH, caller, traceback.format_exc())
			return _failure(500, INTERNAL)

		session.commit()
		png = base64.b64decode(result.data["png_base64"])
		audit.record(
			"mobile:login_qr_image",
			{"user": target},
			audit.STATUS_SUCCESS,
			f"Success — {caller} minted a login QR for {target}",
			caller_ip=ip,
			commit=True,
		)
		logger.info("farmops-api 200 %s caller=%s user=%s", QR_IMAGE_PATH, caller, target)
		return Response(
			png,
			status=200,
			content_type="image/png",
			headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
		)


def _enroll_slot(ip: str) -> str:
	window = int(time.time() // 60)
	fingerprint = hashlib.sha256(str(ip or "").encode()).hexdigest()[:16]
	return f"erpnext_mcp:farmops:enroll_fail:{fingerprint}:{window}"


def _enroll_device(request: Request) -> Response:
	"""`POST /mobile/enroll_device` — spend a one-time QR token, receive a credential.

	THE ONLY UNAUTHENTICATED WRITE ON THIS SURFACE, and it is unauthenticated
	because it has to be: the phone calling it holds nothing yet. What stands in
	for a credential is the token itself — 256 bits, single use, inside a window
	the office opened — and everything the token does not prove is checked
	against the grant it names. See `device_enrollment.exchange`.

	The answer carries the device's `api_key` and `api_secret`. It is the ONLY
	time the secret exists in plaintext; the row keeps it encrypted from here on.
	A refusal is never 401: the phone has no session to lose, and 401 means
	"sign out" to it.

	The token is never logged and never audited. The audit row records the
	device name, the address and the outcome.
	"""
	if request.method != "POST":
		return _failure(405, f"{ENROLL_PATH} is POST only.")
	body = _body(request)

	with session.request_session(request=request, body=body):
		if not guard.mobile_enabled():
			return _failure(503, "The Farm Ops mobile API is switched off on this site.")

		ip = security.caller_ip()
		slot = _enroll_slot(ip)
		if fallback_auth._failures(slot) >= ENROLL_FAILURE_LIMIT:
			return _failure(
				429, "Too many enrolment attempts from this address. Wait a minute and scan again."
			)

		device_name = str(body.get("device_name") or "")[:120]
		try:
			issued = device_enrollment.exchange(
				body.get("token"),
				device_name=device_name,
				device_identifier=str(body.get("device_identifier") or ""),
			)
		except device_enrollment.EnrollmentRefused as exc:
			session.rollback()
			fallback_auth._note_failure(slot)
			audit.record(
				"mobile:enroll_device",
				{"device_name": device_name},
				audit.STATUS_ERROR,
				f"Error — enrolment refused: {exc}",
				caller_ip=ip,
				commit=True,
			)
			logger.info("farmops-api %s %s from %s", exc.status, ENROLL_PATH, request.remote_addr)
			return _failure(exc.status if exc.status != 401 else 403, str(exc))
		except Exception:
			session.rollback()
			logger.error("farmops-api 500 %s\n%s", ENROLL_PATH, traceback.format_exc())
			return _failure(500, INTERNAL)

		session.commit()
		audit.record(
			"mobile:enroll_device",
			{"device_name": issued.get("device_name"), "device": issued.get("device")},
			audit.STATUS_SUCCESS,
			f"Success — {issued['user']} enrolled device {issued.get('device')} ({issued.get('device_name')})",
			caller_ip=ip,
			commit=True,
		)
		logger.info("farmops-api 200 %s user=%s device=%s", ENROLL_PATH, issued["user"], issued.get("device"))
		base = mobile_tools._mobile_base_url({})
		return _success(
			{
				"type": "farm_ops_login",
				"v": 1,
				"url": base,
				"api_base": mobile_tools.API_BASE,
				"user": issued["user"],
				"device": issued["device"],
				"device_name": issued.get("device_name"),
				"api_key": issued["api_key"],
				"api_secret": issued["api_secret"],
				"token": issued["token"],
				"note": (
					"Store api_key and api_secret in the Keychain now: this is the only time the "
					"secret is sent. Send them on every call as X-FarmOps-Token: <api_key>:<api_secret>."
				),
			}
		)


def _png(data: bytes, cache: str) -> Response:
	return Response(
		data,
		status=200,
		content_type="image/png",
		headers={"X-Content-Type-Options": "nosniff", "Cache-Control": cache},
	)


def _slope_aspect_tile(request: Request, path: str) -> Response:
	"""`GET /tiles/slope_aspect/{z}/{x}/{y}.png` — one slope-aspect map tile.

	THE GATES ARE `guard.endpoint`'s, RUN BY HAND, AND ONE IS LEFT OUT ON
	PURPOSE. Kill switch (503), credential (401), rate limit (429), a Farm Ops
	role and an Active Mobile Access Grant (403) — exactly what every enrolled
	read on this surface requires. What is NOT done is the audit row: the
	decorator writes one per call, and a phone panning a map asks for dozens of
	tiles a second. MCP Action Log would become a tile log, and the rows that
	matter in it would be the ones nobody could find. Terrain from a public
	survey is not a record anybody needs an access trail for; the gunicorn log
	line below is kept.

	A tile outside the layer or the zoom range answers the transparent PNG with
	200, so `MKTileOverlay` draws nothing rather than logging a failure per tile.
	A site that has never built the layer answers 404 JSON, which says so.
	"""
	return _terrain_tile(
		request,
		path,
		TILE_PREFIX,
		"slope aspect",
		"slope_aspect_tile",
		lambda caller, z, x, y: slope_aspect.tile_png(z, x, y),
	)


def _slope_grade_tile(request: Request, path: str) -> Response:
	"""`GET /tiles/slope_grade/{z}/{x}/{y}.png[?asset=<docname>]` — one grade tile. v0.168.0.

	The aspect tile's gates, exactly (see `_slope_aspect_tile`). `asset` shifts
	the colours to that machine's rollover limit. IT IS SCOPED to the caller's
	entities — another company's docname is 404, worded as an absent one is —
	and an asset with no limit and no type figure is 400 by name rather than a
	tile of standard colours a driver would read as their machine's.
	"""

	def render(caller, z, x, y):
		scheme = None
		asset = str(request.args.get("asset") or "").strip()
		if asset:
			allowed = guard.require_scope(caller)
			name = guard.require_scoped_doc(slope_grade.ASSET_REGISTER, asset, "Asset", allowed)
			rating = slope_grade.asset_rating(name)
			scheme = slope_grade.equipment_scheme(rating["max_safe_slope_degrees"])
		return slope_grade.tile_png(z, x, y, scheme)

	return _terrain_tile(request, path, GRADE_TILE_PREFIX, "slope grade", "slope_grade_tile", render)


def _terrain_tile(
	request: Request, path: str, prefix: str, label: str, throttle_key: str, render
) -> Response:
	"""The gates and the answer shared by both terrain tile routes.

	`render(caller, z, x, y)` returns PNG bytes, or None for "never built". It
	may refuse: DoesNotExistError is 404, `slope_grade.AssetNotRated` 400, any
	other ToolError 503 (numpy missing), anything else 500.
	"""
	with session.request_session(request=request, body={}):
		if not guard.mobile_enabled():
			return _failure(503, "The Farm Ops mobile API is switched off on this site.")

		# v0.216.0 (H1): the credential first; the pattern and the method after.
		caller, _source = auth.resolve(request.headers, {}, request.method, request.path, b"")
		if not caller:
			return _unauthenticated(request, path)

		if request.method not in ("GET", "HEAD"):
			return _failure(405, f"{prefix}{{z}}/{{x}}/{{y}}.png is GET only.")
		match = _TILE_TAIL.match(path[len(prefix) :])
		if not match or not slope_aspect.valid_tile(*match.groups()):
			return _failure(404, f"{path} is not a {label} tile. The pattern is {{z}}/{{x}}/{{y}}.png.")
		z, x, y = (int(part) for part in match.groups())

		try:
			guard.throttle(caller, throttle_key, TILE_LIMIT)
		except guard.RateLimited as exc:
			return _failure(429, str(exc))

		session.become(caller)
		try:
			guard._require_farm_ops_role()
			guard._require_mobile_grant(caller)
		except frappe.PermissionError as exc:
			session.rollback()
			return _failure(403, str(exc), type(exc).__name__)

		try:
			png = render(caller, z, x, y)
		except frappe.PermissionError as exc:
			session.rollback()
			return _failure(403, str(exc), type(exc).__name__)
		except frappe.DoesNotExistError as exc:
			session.rollback()
			return _failure(404, str(exc), type(exc).__name__)
		except slope_grade.AssetNotRated as exc:
			session.rollback()
			return _failure(400, str(exc))
		except frappe.ValidationError as exc:
			session.rollback()
			return _failure(400, str(exc), type(exc).__name__)
		except ToolError as exc:
			session.rollback()
			return _failure(503, str(exc))
		except Exception:
			session.rollback()
			logger.error("farmops-api 500 %s caller=%s\n%s", path, caller, traceback.format_exc())
			return _failure(500, INTERNAL)

		# The grant gate stamps `last_seen_on` at most once a day.
		session.commit()
		if png is None:
			return _failure(
				404,
				f"The {label} layer has not been built on this site. An operator runs "
				"build_slope_aspect_layer once to fetch the elevation and cache the tiles.",
			)
		return _png(png, "private, max-age=86400")


_SCAN_PAGE = (
	"Farm Ops tag: {code}\n\n"
	"Open the Farm Ops app and scan this tag with the app's scanner.\n\n"
	"Abre la aplicación Farm Ops y escanea esta etiqueta con el escáner de la aplicación.\n"
)


def _brand_image(request: Request, path: str) -> Response:
	"""`GET /brand/<company>` — that company's logo, for the email footer. v0.216.1.

	PUBLIC ON PURPOSE AND NARROW ON PURPOSE. A logo in an email is fetched by the
	recipient's mail client (or Gmail's image proxy) from the open internet, and
	the site's own address is a tailnet address nobody outside can reach — the
	broken-image icon Tim saw. So the one public path serves this one thing: the
	Company's `badge_logo`, else its `company_logo` — the mark already printed on
	every card and letterhead — and only when it is a PNG, JPEG or GIF by its
	own bytes (never SVG, which can carry script). It is not a file server: the
	path names a Company, not a file, and nothing else on the site is reachable
	through it. Metered per address like the other open routes. Every miss — no
	such company, no logo, not an image — is the same plain 404.
	"""
	from urllib.parse import unquote

	if _open_route_limited(request):
		return _failure(429, TOO_MANY)
	company = unquote(path[len(BRAND_PREFIX) :]).strip()[:140]
	if not company or "/" in company:
		return _failure(404, NOT_FOUND)
	with session.request_session(request=request, body={}):
		try:
			from .. import card_print

			if not frappe.db.exists("Company", company):
				return _failure(404, NOT_FOUND)
			data = card_print.company_logo(company) or b""
		except Exception:  # pragma: no cover - a site mid-migrate
			data = b""
	mime = next((kind for magic, kind in _IMAGE_SIGNATURES if data.startswith(magic)), "")
	if not mime or len(data) > BRAND_MAX_BYTES:
		return _failure(404, NOT_FOUND)
	return Response(
		data,
		status=200,
		content_type=mime,
		headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"},
	)


# ── v0.218.0 device keys: pickup, challenge, token ─────────────────────────
#: docs/design/device_client_enrollment.md §3–§4. OFF until `device_keys_enabled`:
#: then these paths fall through to the uniform answer of a path that does not exist.
def _revoked_device() -> Response:
	"""The uniform 401, plus the one fact only the real device can earn (§4.3)."""
	response = _failure(401, UNAUTHORIZED)
	payload = json.loads(response.get_data(as_text=True))
	payload["device_revoked"] = True
	return _json(payload, 401)


def _device_key_refused(request: Request, path: str, status: int) -> Response:
	"""Counted like every failed sign-in; uniform; never says why."""
	refused = _unauthenticated(request, path)
	if refused.status_code == 429 or status == 401:
		return refused
	return _failure(404, NOT_FOUND)


def _device_key_route(request: Request, path: str):
	"""The three key routes, or None when device keys are off (the uniform path answers)."""
	from .. import device_keys

	raw = request.get_data(cache=True)
	body = _body(request)
	with session.request_session(request=request, body=body):
		if not device_keys.enabled():
			return None
		if path in (ACCESS_REQUEST_PATH, ACCESS_STATUS_PATH) and not device_keys.approval_enabled():
			return None
		if path.startswith(PICKUP_PREFIX) and request.method == "GET":
			if _open_route_limited(request):
				return _failure(429, TOO_MANY)
			return Response(
				_PICKUP_PAGE,
				status=200,
				content_type="text/plain; charset=utf-8",
				headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
			)
		if request.method != "POST":
			return None
		status = (
			404
			if path.startswith(PICKUP_PREFIX) or path in (ACCESS_REQUEST_PATH, ACCESS_STATUS_PATH)
			else 401
		)
		try:
			proof = device_keys.verify_dpop(str(request.headers.get("DPoP") or ""), "POST", request.path, raw)
			if path.startswith(PICKUP_PREFIX):
				answer = device_keys.redeem(path[len(PICKUP_PREFIX) :], body, proof)
				audit.record(
					"mobile:enroll_pickup",
					{"device": answer["device"]},
					audit.STATUS_SUCCESS,
					f"Success — {answer['user']} picked up device {answer['device']} with a pickup link",
					caller_ip=_peer(request),
					commit=False,
				)
			elif path == ACCESS_REQUEST_PATH:
				answer = device_keys.request_access(body, proof, _peer(request))
			elif path == ACCESS_STATUS_PATH:
				answer = device_keys.access_status(str(body.get("request") or ""), proof)
			elif path == CHALLENGE_PATH:
				answer = device_keys.challenge(str(body.get("device") or ""), proof)
			else:
				answer = device_keys.mint(
					str(body.get("device") or ""),
					str(body.get("challenge") or ""),
					str(body.get("signature") or ""),
					proof,
				)
		except device_keys.Refused as exc:
			session.rollback()
			logger.info("farmops-api device-key refusal %s: %s", path[:60], exc)
			if exc.revoked_device:
				return _revoked_device()
			return _device_key_refused(request, path, status)
		except Exception:
			session.rollback()
			logger.error("farmops-api device-key error %s\n%s", path[:60], traceback.format_exc())
			return _device_key_refused(request, path, status)
		session.commit()
		return _success(answer)


def _scan_page(path: str) -> Response:
	"""What a tag's QR shows a phone camera that is not the app. v0.216.0.

	PUBLIC, STATIC, PLAIN TEXT, AND IT LOOKS NOTHING UP. The code in the URL is
	echoed back and that is the whole of what an anonymous caller learns:
	whether the tag is real, what it is on and where are answered only by
	`universal_scan`, to an enrolled caller. No session is opened and nothing is
	read, so this cannot be used to enumerate the register.

	`text/plain` because nothing under /farmops answers HTML (addendum H7): a
	plain body cannot carry markup whatever the code in the URL says, and
	`nosniff` keeps a browser from deciding otherwise.

	It exists so a tag does not need `/erpnext/scan/...` on the public Funnel —
	a path that never had a page behind it and answered Frappe's 404.
	"""
	from urllib.parse import unquote

	code = " ".join(unquote(path[len(SCAN_PREFIX) :]).split())[:140]
	response = Response(
		_SCAN_PAGE.format(code=code or "—"), status=200, content_type="text/plain; charset=utf-8"
	)
	response.headers["Cache-Control"] = "public, max-age=3600"
	response.headers["X-Content-Type-Options"] = "nosniff"
	return response


def _upload_filename(request: Request) -> str:
	"""The file name a PUT carries: `X-File-Name`, else Content-Disposition, else one
	made from the Content-Type. Never a path — `upload_links.safe_filename` keeps the
	last component and the stored name is prefixed with the upload id."""
	from urllib.parse import unquote

	name = unquote(str(request.headers.get("X-File-Name") or ""))
	if not name:
		disposition = str(request.headers.get("Content-Disposition") or "")
		match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.IGNORECASE)
		if match:
			name = unquote(match.group(1))
	if not name:
		kind = str(request.mimetype or "").lower()
		name = f"upload.{_EXTENSION_FOR_TYPE.get(kind, 'bin')}"
	return name


def _declared_type_agrees(declared: str, filename: str) -> bool:
	declared = str(declared or "").split(";")[0].strip().lower()
	if declared in ("", "application/octet-stream", "binary/octet-stream"):
		return True
	return declared in _DECLARED_TYPES.get(upload_links.extension(upload_links.safe_filename(filename)), ())


def _upload(request: Request, path: str) -> Response:
	"""`PUT|POST /upload/<token>` — one file onto one document, once. v0.244.0.

	THE TOKEN IS PROVED BEFORE A BYTE OF BODY IS READ, and every token that will
	not work — unknown, expired, used, revoked, or the feature switched off — gets
	the same 404. Then the link is claimed (row-locked; a second request is 409),
	and the body streams to disk in 1 MB pieces under the LINK's cap rather than
	`_MAX_BODY`. Never 401: there is no session here to lose.
	"""
	token = path[len(UPLOAD_PREFIX) :]
	ip = _peer(request)
	# Metered before any session opens, as the other open routes are: a flood costs
	# a counter, not a database connection.
	if guard._count(f"upload:{ip or 'unknown'}", 60) > UPLOAD_LIMIT:
		return _failure(429, TOO_MANY)
	with session.request_session(request=request, body={}):
		if not upload_links.enabled():
			return _failure(404, NOT_FOUND)
		if request.method not in ("PUT", "POST"):
			# No GET, no HEAD, no listing: a link is not a page.
			return _failure(404, NOT_FOUND)
		link = upload_links.find(token)
		if link is None:
			session.commit()  # `find` may have marked a link Expired
			if guard._count(f"upload-bad:{ip or 'unknown'}", 3600) == UPLOAD_BAD_TOKEN_ALERT:
				audit.record(
					"farmops:upload",
					{"ip": ip},
					audit.STATUS_ERROR,
					f"Error — {UPLOAD_BAD_TOKEN_ALERT} unknown upload tokens from {ip} within an hour",
					caller_ip=ip,
					commit=True,
				)
			return _failure(404, NOT_FOUND)

		try:
			upload_links.claim(link["name"])
		except upload_links.UploadRefused as exc:
			session.rollback()
			return _failure(exc.status, str(exc))
		upload_links.note_attempt(link["name"], ip, str(request.headers.get("User-Agent") or ""))
		session.commit()

		cap = int(link.get("max_bytes") or 0)
		request.max_content_length = cap + MULTIPART_SLACK
		try:
			if request.method == "PUT":
				filename = _upload_filename(request)
				if not _declared_type_agrees(request.mimetype, filename):
					raise upload_links.UploadRefused(415, "The declared Content-Type does not match the file name.")
				result = upload_links.receive(
					link, request.stream, declared_length=request.content_length, filename=filename
				)
			else:
				parts = [part for part in request.files.values() if part and part.filename]
				if len(parts) != 1:
					raise upload_links.UploadRefused(400, "Send exactly one file part.")
				part = parts[0]
				if not _declared_type_agrees(part.mimetype, part.filename):
					raise upload_links.UploadRefused(415, "The declared Content-Type does not match the file name.")
				result = upload_links.receive(link, part.stream, filename=part.filename)
		except upload_links.UploadRefused as exc:
			session.rollback()
			upload_links.fail(link["name"], str(exc))
			audit.record(
				"farmops:upload",
				{"upload_id": link["upload_id"]},
				audit.STATUS_ERROR,
				f"Error — upload {link['upload_id']} refused ({exc.status}): {exc}",
				caller_ip=ip,
				commit=True,
			)
			logger.info("farmops-api %s upload %s from %s: %s", exc.status, link["upload_id"], ip, exc)
			return _failure(exc.status, str(exc))
		except RequestEntityTooLarge:
			session.rollback()
			upload_links.fail(link["name"], "Larger than the link allows.")
			return _failure(413, f"This file is larger than the link allows ({cap} bytes).")
		except Exception:
			session.rollback()
			upload_links.fail(link["name"], "The server could not store the file.")
			logger.error("farmops-api 500 upload %s\n%s", link["upload_id"], traceback.format_exc())
			return _failure(500, INTERNAL)

		session.commit()
		audit.record(
			"farmops:upload",
			{"upload_id": link["upload_id"], "file": result["file"]},
			audit.STATUS_SUCCESS,
			f"Success — upload {link['upload_id']}: {result['file_name']} ({result['file_size']} bytes, "
			f"sha256 {result['sha256'][:12]}) onto {link['target_doctype']} {link['target_name']}",
			caller_ip=ip,
			commit=True,
		)
		logger.info("farmops-api 200 upload %s %s bytes", link["upload_id"], result["file_size"])
		return _success(result)


def dispatch(request: Request) -> Response:
	"""One request, start to finish. Returns a JSON response for every outcome.

	v0.216.0 (addendum H1–H3). The order is the design:

	1. SIZE. Over the ceiling is 413 before a byte is parsed, with a
	   Content-Length or without one.
	2. THE THREE OPEN ROUTES — health, the tag page, enrolment.
	3. WHO. Everything else authenticates before the path or the method is
	   looked at, and nobody gets the one 401 whatever they asked for.
	4. WHAT. 404 and 405 are answered only to a caller holding a credential.
	"""
	path = (request.path or "").rstrip("/") or "/"

	# v0.244.0: the one route with its own ceiling, set by its link once the token is
	# proved. Everything else meets `_MAX_BODY` below, unchanged.
	if path.startswith(UPLOAD_PREFIX):
		try:
			return _upload(request, path)
		except RequestEntityTooLarge:
			return _failure(413, TOO_LARGE)

	if request.content_length and int(request.content_length) > _MAX_BODY:
		return _failure(413, TOO_LARGE)
	# The same ceiling for a body that did not announce its length: Werkzeug
	# stops reading at it and raises, which `_dispatch` lets through to here.
	request.max_content_length = _MAX_BODY
	try:
		return _dispatch(request, path)
	except RequestEntityTooLarge:
		return _failure(413, TOO_LARGE)


def _dispatch(request: Request, path: str) -> Response:
	if path == HEALTH_PATH:
		# GET or POST, unauthenticated, and deliberately incurious: it proves
		# this process is answering on this path and says nothing else — not
		# even which release it is, since v0.216.0.
		if _open_route_limited(request):
			return _failure(429, TOO_MANY)
		return _json({"ok": True, "service": "farmops-api"})

	if path.startswith(BRAND_PREFIX) and request.method in ("GET", "HEAD"):
		return _brand_image(request, path)

	if path.startswith(SCAN_PREFIX) and request.method == "GET":
		if _open_route_limited(request):
			return _failure(429, TOO_MANY)
		return _scan_page(path)

	if path.startswith(PICKUP_PREFIX) or path in (
		CHALLENGE_PATH,
		TOKEN_PATH,
		ACCESS_REQUEST_PATH,
		ACCESS_STATUS_PATH,
	):
		answer = _device_key_route(request, path)
		if answer is not None:
			return answer

	if path == ENROLL_PATH:
		return _enroll_device(request)

	if not path.startswith(f"{PREFIX}/"):
		# Not reachable through the Funnel mount at all. Nothing is echoed.
		return _failure(404, NOT_FOUND)

	if path == QR_IMAGE_PATH:
		return _login_qr_image(request)

	if path.startswith(TILE_PREFIX):
		return _slope_aspect_tile(request, path)

	if path.startswith(GRADE_TILE_PREFIX):
		return _slope_grade_tile(request, path)

	route = BY_PATH.get(path[len(PREFIX) :])
	body = _body(request)

	with session.request_session(request=request, body=body):
		user, source = auth.resolve(
			request.headers, body, request.method, request.path, request.get_data(cache=True)
		)
		if source == "revoked_device":
			return _revoked_device()
		if not user:
			# Nothing was opened as anybody, so there is nothing to roll back.
			# One refusal is not audited — `guard` audits per method on an
			# authenticated caller, and an unauthenticated flood must not be
			# able to grow MCP Action Log. `_unauthenticated` counts it, and
			# writes ONE row per address per hour when it becomes a pattern.
			return _unauthenticated(request, path, body)

		if route is None:
			# Said only to somebody holding a credential.
			return _failure(404, f"{path} is not a Farm Ops API method.")

		if request.method != "POST":
			# Every method is POST, including the reads. The whitelisted path
			# accepted GET on the reads and this one does not: a GET carries its
			# arguments in a URL, and a URL is the one part of a request that gets
			# written to an access log by every proxy between here and a phone.
			return _failure(405, f"{path} is POST only.")

		session.become(user)
		try:
			result = route.handler(**bind(route, body))
		except Exception as exc:
			anticipated = _status_for(exc)
			# `guard` already rolled back and committed its own audit row on
			# every refusal it raised. Rolling back again is harmless and covers
			# the exceptions it did not raise — a tool that threw halfway
			# through a write must not leave the half behind.
			session.rollback()
			if anticipated:
				logger.info("farmops-api %s %s user=%s: %s", anticipated, path, user, exc)
			else:
				logger.error("farmops-api 500 %s user=%s\n%s", path, user, traceback.format_exc())
			return _failure(
				session.status_hint(anticipated or 500),
				_message_for(exc, anticipated),
				type(exc).__name__,
				str(getattr(exc, "translation_key", "") or "") if anticipated else "",
			)

		# See `session.py`: reads commit too, because `_stamp_last_seen` writes
		# from inside one and the idle-grant sweep is what that stamp is for.
		session.commit()
		logger.info("farmops-api 200 %s user=%s door=%s", path, user, source or "header")
		return _success(result)


# ── the WSGI callable ───────────────────────────────────────────────────────
def create_app():
	"""The WSGI application. Nothing is configured at construction time.

	No state is captured in the closure on purpose: every request opens and
	closes its own Frappe session, so two gunicorn workers, two threads and two
	successive calls on one thread all behave identically and none of them can
	read another's identity.
	"""

	@Request.application
	def app(request: Request) -> Response:
		try:
			return dispatch(request)
		except Exception:  # pragma: no cover - the promise at the top of the file
			# A bug in THIS module still answers JSON. The alternative is
			# Werkzeug's own HTML 500 page, which is the exact failure — an
			# HTML body where the app expected JSON — that this release exists
			# to end. It would be perverse to reintroduce it in the handler.
			logger.error("farmops-api unhandled\n%s", traceback.format_exc())
			return _failure(500, INTERNAL)

	return app


#: What gunicorn is pointed at. See `supervisord.conf` in the image build.
application = create_app()
