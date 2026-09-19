# SPDX-License-Identifier: MIT
"""The second door: identity for a request whose `Authorization` header was eaten.

v0.17.2. THIS FILE EXISTS BECAUSE A PROXY THIS APP DOES NOT CONTROL REMOVES THE
ONE HEADER THE WHOLE MOBILE SURFACE RESTS ON.

────────────────────────────────────────────────────────────────────────────
WHAT WAS PROVEN, AND HOW
────────────────────────────────────────────────────────────────────────────

A Farm Ops phone got HTTP 200 from every call and JSON from none of them. The
body was the Desk's `/me` page and the response set `sid=Guest`, which is what
Frappe does when a whitelisted method is reached by a caller it never
authenticated. Three probes, on 2026-08-01:

  1. `curl` to `http://localhost:5300/api/method/…get_current_user_context`
     from INSIDE the container, carrying `Authorization: token key:secret`
     → correct JSON.
  2. The same call through `https://<host>.ts.net/…` from the public funnel
     → HTML `/me`, `sid=Guest`.
  3. The same call through `https://<host>.ts.net/…` from a Mac ON the tailnet,
     which bypasses the funnel edge and hits the `tailscale serve` handler
     directly → ALSO HTML `/me`.

Probe 3 is the one that settles it. The request never leaves the tailnet and the
header still does not arrive, so this is the `serve`/`funnel` proxy step and not
the public edge. The credential was fine; it was never presented.

THE REQUIREMENT THAT RULES OUT THE OBVIOUS FIXES. Farm Ops has to work on
cellular, on the LAN and on the tailnet — the same binary, the same enrolment,
whichever way the phone happens to be reaching the site that morning. So
"connect over Tailscale instead" is not a fix, it is a different product. The fix
has to survive whatever the proxy does to headers, on every path, without the
app knowing which path it is on.

────────────────────────────────────────────────────────────────────────────
THE ANSWER: CARRY THE SAME CREDENTIAL IN TWO MORE PLACES
────────────────────────────────────────────────────────────────────────────

The app sends the identical `<api_key>:<api_secret>` pair three ways, and the
server takes the first one that arrives:

  (a) `Authorization: token <key>:<secret>` — Frappe's own, unchanged. When it
      survives, NOTHING in this file runs and the request is authenticated
      before this app is ever consulted.
  (b) `X-FarmOps-Token: <key>:<secret>` — a header name no proxy has an opinion
      about. `Authorization` is special to every intermediary on earth; this one
      is special to nobody.
  (c) `"_auth": {"api_key": …, "api_secret": …}` in the POST body — the belt to
      (b)'s braces. A layer that strips one custom header may strip another; a
      layer that rewrites the request body is not a proxy, it is an attack.

NOTHING HERE WIDENS WHAT A CREDENTIAL MAY DO. This module answers exactly one
question — *which Frappe user is this?* — by looking the `api_key` up, comparing
the stored `api_secret` with `hmac.compare_digest`, and refusing a disabled
account. SINCE v0.175.0 THE LOOKUP IS ON DEVICE ROWS, NOT ON USER: a phone's pair
lives on a `Mobile Device Enrollment` row (see `device_enrollment`), so door (a)
above no longer carries it — Frappe's own validator has never heard of a device
key and answers 401 on a bench before any hook runs. The phone has sent (b) on
every call since v0.17.2, and the /farmops transport reads both headers itself. Everything downstream is
unchanged, and `guard.endpoint` still runs all seven of its checks on the user
this establishes. A phone that got in through door (b) and a phone that got in
through door (a) are the same principal with the same role gate, the same
Mobile Access Grant requirement, the same entity scoping, the same rate limit
and the same audit row.

────────────────────────────────────────────────────────────────────────────
WHY THIS RUNS AS AN `auth_hooks` ENTRY AND NOT ONLY IN THE DECORATOR
────────────────────────────────────────────────────────────────────────────

THE DECORATOR ALONE WOULD NEVER HAVE RUN, and this is the trap in the obvious
implementation. Frappe's `is_whitelisted` refuses a Guest before the method is
dispatched — `@frappe.whitelist()` without `allow_guest=True` throws
`PermissionError` for Guest in the framework, not in the method — so a fallback
check written inside `guard.endpoint` would sit behind a door that never opens.
The `/me` page the phone was getting IS that refusal being rendered.

`auth_hooks` is Frappe's own extension point for exactly this: `validate_auth()`
runs it on every request, after the session is established and BEFORE the
handler decides whether the caller may reach the method. So identity is settled
in the same window Frappe settles it in, and by the time `is_whitelisted` looks,
there is a real user to look at.

`guard.endpoint` calls `resolve()` a second time anyway, and that is not
redundancy for its own sake:

  * the standalone suite calls the wrapped functions directly, with no request
    lifecycle and therefore no `auth_hooks` — the belt is what makes the tests
    exercise the real resolver instead of a mock,
  * a site whose `hooks.py` did not reload after a migrate still authenticates,
  * anything that calls these methods in-process gets the same treatment.

It is idempotent: `resolve()` returns immediately when the session is already
somebody, so the second call on a real request costs one string comparison.

────────────────────────────────────────────────────────────────────────────
THE THINGS THIS FILE IS CAREFUL ABOUT
────────────────────────────────────────────────────────────────────────────

**IT TOUCHES ONLY THIS APP'S OWN PATHS.** `authenticate` is called on every
request to the site, including every Desk page load of every other app. The
first thing it does is check the path prefix and return, and it never raises:
an auth hook that threw would take the whole site down, not one endpoint.

**IT RESTORES `form_dict` AFTER `set_user`.** `frappe.set_user` CLEARS
`frappe.local.form_dict`, and at the moment an auth hook runs, `form_dict` is
still the only copy of the request's arguments — the handler has not bound them
yet. Clearing it would deliver every mobile call with no arguments at all.
Frappe's own `validate_api_key_secret` saves and restores it around the same
call for the same reason; this does what Frappe does.

**A WRONG SECRET IS GUEST, NOT AN ERROR.** Same as Frappe. The caller learns
nothing about whether the key existed, and `guard` produces the one refusal it
produces for everything else.

**FAILED VERIFICATIONS ARE METERED, SUCCESSES ARE NOT.** The counter is keyed on
a fingerprint of the presented `api_key` and only a FAILURE touches it, so a
phone with a good credential can never be locked out by anybody else's traffic —
which is the failure mode that matters when forty phones arrive from one funnel
address and an IP-keyed limit would make them share a bucket. A guesser working
one key is stopped after ten wrong answers a minute; a guesser enumerating keys
is unmetered here and irrelevant, because each unknown key still needs one
correct guess of a 56-character secret.
"""

from __future__ import annotations

import hashlib
import json
import time

import frappe

#: The header the app sends alongside `Authorization`. Same value, same format.
HEADER = "X-FarmOps-Token"

#: The POST body key carrying the same pair. Leading underscore so it sorts and
#: reads as envelope rather than argument, and so it cannot collide with a
#: current or future argument name on any method under `erpnext_mcp.api.*`.
BODY_KEY = "_auth"

SOURCE_HEADER = "header"
SOURCE_BODY = "body"

#: Where the resolved source is parked for the audit row. On `frappe.local`,
#: which the framework rebuilds per request — see `security._CALLING_USER_KEY`
#: for the same reasoning at more length. A module global would leak one
#: caller's answer into the next caller's log line.
_SOURCE_KEY = "erpnext_mcp_fallback_auth_source"

#: The only paths this hook will act on. Everything else on the site — every
#: Desk page, every other app's endpoint, `mcp.handle` itself — returns at the
#: first `if` having read one attribute.
_PATH_PREFIX = "/api/method/erpnext_mcp.api."

#: Wrong answers per key per minute before the fallback path stops answering for
#: that key. Ten is far above anything a working phone does (a working phone
#: does zero) and far below anything useful to a guesser.
FAILURE_LIMIT = 10
_FAILURE_WINDOW = 60

#: Per-process failure buckets, used when this bench has no cache. Same
#: arrangement as `guard._BUCKETS`, and weaker for the same reason: per worker
#: rather than per site, which is a real limit rather than no limit.
_FAILURES: dict = {}

#: A request body larger than this is not read looking for `_auth`. On a real
#: site the body has already been parsed into `form_dict` by the time anything
#: here runs, so this ceiling only bounds the fallback parse — and the one
#: endpoint that carries megabytes, `stage_file_chunk`, sends 512 KB slices.
_MAX_BODY = 2_000_000


# ── the entry points ────────────────────────────────────────────────────────
def authenticate() -> str:
	"""`auth_hooks` entry point. Runs on EVERY request to this site.

	Cheap and silent for everything that is not one of this app's mobile
	paths, and incapable of raising: `validate_auth` calls this before the
	handler, so an exception here would be an exception on every request to the
	site rather than a failure of one endpoint.
	"""
	try:
		if not _is_mobile_path():
			return ""
		return resolve()
	except Exception:  # pragma: no cover - see the docstring; never fatal
		try:
			frappe.log_error(
				title="erpnext_mcp: fallback auth hook failed",
				message=frappe.get_traceback(),
			)
		except Exception:
			pass
		return ""


def resolve() -> str:
	"""Establish the session from a fallback credential. Returns the source, or "".

	The source is `"header"`, `"body"` or `""` — the last meaning "nothing was
	presented, or what was presented did not check out", which are deliberately
	the same answer to the caller.

	RETURNS IMMEDIATELY WHEN FRAPPE ALREADY AUTHENTICATED SOMEBODY. That is the
	priority order the whole design rests on: `Authorization: token` wins where
	it survives, and this never overwrites an identity the framework established.
	A request carrying both a good `Authorization` and a stale `X-FarmOps-Token`
	is the first one, every time.
	"""
	if _session_user() not in ("", "Guest"):
		return ""

	api_key, api_secret, source = _presented()
	if not (api_key and api_secret):
		return ""

	user = verify_credential(api_key, api_secret)
	if not user:
		return ""

	_establish(user)
	try:
		setattr(frappe.local, _SOURCE_KEY, source)
	except Exception:  # pragma: no cover - a local that refuses attributes
		pass
	return source


def verify_credential(api_key: str, api_secret: str) -> str:
	"""One `<api_key>:<api_secret>` pair, metered and verified. The User, or "".

	v0.18.0 SPLIT THIS OUT OF `resolve` SO THERE IS STILL EXACTLY ONE VERIFIER.
	`farmops_api` presents the same pair on the same terms, but it reads it off
	its own request object rather than out of `frappe.local`, so it needs the
	half of `resolve` that checks a credential without the half that finds one.

	The alternative was a second copy of Frappe's api-key scheme living in the
	sidecar, and the day the two disagreed the symptom would be an
	authentication bug on forty phones with two places to look for it. `resolve`
	now calls this, so both transports share one verifier AND one failure
	counter — a guesser working a key through the sidecar is metered by the same
	bucket as one working it through the whitelisted path, which is what makes
	the limit mean ten wrong answers a minute rather than ten per door.

	NOTHING HERE ESTABLISHES A SESSION. It answers "whose credential is this?"
	and stops; becoming that user is `_establish`'s job on one path and
	`farmops_api.session`'s on the other, and every gate in `guard.py` runs
	afterwards on either.
	"""
	api_key = str(api_key or "").strip()
	api_secret = str(api_secret or "").strip()
	if not (api_key and api_secret):
		return ""

	slot = _failure_slot(api_key)
	if _failures(slot) >= FAILURE_LIMIT:
		return ""

	user = _verified_user(api_key, api_secret)
	if not user:
		_note_failure(slot)
		return ""
	return user


def split_token(raw: str) -> tuple:
	"""`key:secret` → the two halves, tolerating a leading `token `.

	The public name for `_split`. `farmops_api.auth` reads the same
	`X-FarmOps-Token` header in the same format, and parsing it a second way is
	how a client that works against one door stops working against the other.
	"""
	return _split(raw)


def source() -> str:
	"""How this request got in: `"header"`, `"body"`, or "" for Frappe's own auth.

	`guard._record` reads this into the audit row. An empty answer is the normal
	path and says so by saying nothing — a log line with no fallback tag is a
	request whose `Authorization` header arrived intact.
	"""
	return str(getattr(frappe.local, _SOURCE_KEY, "") or "")


# ── what the caller presented ───────────────────────────────────────────────
def _presented() -> tuple:
	"""(api_key, api_secret, source). The header first, then the body.

	The header is preferred because it is the cheaper read and because a client
	that can get a custom header through has no reason to also be trusted about
	its body. Neither is trusted about anything except which pair to verify.
	"""
	raw = str(frappe.get_request_header(HEADER) or "").strip()
	if raw:
		api_key, api_secret = _split(raw)
		if api_key and api_secret:
			return api_key, api_secret, SOURCE_HEADER

	body = _body_auth()
	if body:
		api_key = str(body.get("api_key") or "").strip()
		api_secret = str(body.get("api_secret") or "").strip()
		if not (api_key and api_secret):
			api_key, api_secret = _split(str(body.get("token") or ""))
		if api_key and api_secret:
			return api_key, api_secret, SOURCE_BODY

	return "", "", ""


def _split(raw: str) -> tuple:
	"""`key:secret` → the two halves. A leading `token ` is tolerated.

	The app is copying the value it already built for `Authorization`, and the
	most likely mistake in a two-line client change is copying the word as well
	as the value. Accepting it costs one comparison and turns a silent 403 into
	a working request; refusing it would produce exactly the failure this
	release exists to end.
	"""
	value = str(raw or "").strip()
	if value[:6].lower() == "token ":
		value = value[6:].strip()
	api_key, separator, api_secret = value.partition(":")
	if not separator:
		return "", ""
	return api_key.strip(), api_secret.strip()


def _body_auth() -> dict:
	"""`_auth` out of the POST body, whatever shape it arrived in.

	`form_dict` first: Frappe has already parsed a JSON body into it by the time
	an auth hook runs, so on a real site this is a dict lookup. The raw parse
	below is for a caller reached in-process (the test suite) and for a body that
	arrived by a route that did not populate `form_dict`.

	A string value is decoded as JSON, because a form-encoded client sends a
	nested object as text and a `_auth` that cannot be read is a phone that
	cannot sign in.
	"""
	form = getattr(frappe.local, "form_dict", None)
	value = form.get(BODY_KEY) if isinstance(form, dict) else None
	if value in (None, ""):
		value = _json_body().get(BODY_KEY)
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except Exception:
			return {}
	return value if isinstance(value, dict) else {}


def _json_body() -> dict:
	"""The request body as a dict, or {}. Never raises, never reads a huge body."""
	request = getattr(frappe.local, "request", None)
	if request is None:
		return {}
	try:
		body = request.get_data(as_text=True) or ""
	except Exception:
		return {}
	if len(body) > _MAX_BODY or not body.strip().startswith("{"):
		return {}
	try:
		parsed = json.loads(body)
	except Exception:
		return {}
	return parsed if isinstance(parsed, dict) else {}


# ── verification ────────────────────────────────────────────────────────────
def _verified_user(api_key: str, api_secret: str) -> str:
	"""The User this DEVICE pair belongs to, or "". v0.175.0: devices, not Users.

	Until v0.175.0 this looked the key up on `User.api_key`. It now delegates to
	`device_enrollment.verify`, which reads the `Mobile Device Enrollment` rows on
	the worker's grant and nothing else — so a phone's credential is one Frappe's
	own REST auth has never heard of, and revoking one handset leaves the
	worker's other devices working. `device_enrollment`'s module docstring argues
	the move; this function is still the only verifier both transports call.

	The secret is read back through Frappe's own `get_decrypted_password` and
	compared with `hmac.compare_digest`, and a DISABLED ACCOUNT IS STILL REFUSED
	— an operator who unticks Enabled means it, and it is the fastest way to stop
	one person without touching anybody else's enrolment.

	The import is deferred because this module is loaded by an auth hook on every
	request to the site: a Desk page load should not pay for a module it will
	never call.
	"""
	from .. import device_enrollment

	return device_enrollment.verify(api_key, api_secret)


def _establish(user: str) -> None:
	"""Become this user for the rest of the request.

	`frappe.set_user` and not a hand-written assignment, because becoming
	somebody is more than a name: it resets the per-request role-permission cache
	and the User Permission cache, and entity scoping — `roles.companies_for` —
	reads exactly those. A session that carried Guest's cached permissions under
	a worker's name would scope the worker to whatever Guest could see.

	AND THEN IT PUTS `form_dict` BACK. `frappe.set_user` clears it, and when this
	runs from an auth hook `form_dict` is still the only copy of the request's
	arguments — the handler binds them afterwards. Losing it would deliver every
	mobile call with no task, no company and no evidence. This is not a
	workaround: Frappe's own `validate_api_key_secret` saves and restores
	`form_dict` around its own `set_user` call, on every version.
	"""
	form = getattr(frappe.local, "form_dict", None)
	frappe.set_user(user)
	if form is not None:
		try:
			frappe.local.form_dict = form
		except Exception:  # pragma: no cover - a local that refuses attributes
			pass


def _session_user() -> str:
	session = getattr(frappe.local, "session", None)
	return str(getattr(session, "user", "") or "")


def _is_mobile_path() -> bool:
	"""True only for `/api/method/erpnext_mcp.api.*`. The first check on every request.

	The `cmd` form is checked too because `/api/method/<dotted.path>` and a POST
	to `/` carrying `cmd=<dotted.path>` are the same call to Frappe, and a client
	library is free to send either.
	"""
	request = getattr(frappe.local, "request", None)
	if request is not None and str(getattr(request, "path", "") or "").startswith(_PATH_PREFIX):
		return True
	form = getattr(frappe.local, "form_dict", None)
	if isinstance(form, dict):
		return str(form.get("cmd") or "").startswith("erpnext_mcp.api.")
	return False


# ── metering the wrong answers ──────────────────────────────────────────────
def _fingerprint(api_key: str) -> str:
	"""A stable, non-reversible handle for a key, for use in a cache key.

	The `api_key` is the public half of the credential and appears in access
	logs, but a redis key is a place values get read by things that were not
	thinking about credentials, and a hash costs nothing.
	"""
	return hashlib.sha256(str(api_key).encode()).hexdigest()[:16]


def _failure_slot(api_key: str) -> str:
	window = int(time.time() // _FAILURE_WINDOW)
	return f"erpnext_mcp:farmops:fallback_fail:{_fingerprint(api_key)}:{window}"


def _client():
	cache = getattr(frappe, "cache", None)
	if not callable(cache):
		return None
	try:
		return cache()
	except Exception:  # pragma: no cover - no redis on this bench
		return None


def _failures(slot: str) -> int:
	"""Wrong answers already recorded in this window. READ ONLY — never increments.

	`get` and not Frappe's `get_value`: the counter is written with `incr`, which
	stores a raw integer, and `get_value` would try to unpickle it.
	"""
	client = _client()
	if client is not None:
		try:
			return int(client.get(slot) or 0)
		except Exception:  # pragma: no cover - no redis on this bench
			pass
	return int(_FAILURES.get(slot, 0))


def _note_failure(slot: str) -> None:
	client = _client()
	if client is not None:
		try:
			hits = int(client.incr(slot))
			if hits == 1:
				client.expire(slot, _FAILURE_WINDOW * 2)
			return
		except Exception:  # pragma: no cover - no redis on this bench
			pass
	suffix = slot.rsplit(":", 1)[-1]
	for stale in [entry for entry in _FAILURES if not entry.endswith(f":{suffix}")]:
		_FAILURES.pop(stale, None)
	_FAILURES[slot] = _FAILURES.get(slot, 0) + 1
