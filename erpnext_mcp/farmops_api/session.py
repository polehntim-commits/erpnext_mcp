# SPDX-License-Identifier: MIT
"""A Frappe request lifecycle, by hand, because nothing else is doing it.

Frappe's own WSGI app opens `frappe.init` / `frappe.connect` per request, builds
`frappe.local`, authenticates, dispatches, then commits or rolls back and
destroys. This service replaces the dispatch step and therefore has to do the
other five itself. It is a short list and every item on it is load-bearing.

────────────────────────────────────────────────────────────────────────────
WHY init/connect PER REQUEST AND NOT ONCE PER WORKER
────────────────────────────────────────────────────────────────────────────

Once-per-worker is faster and is the wrong trade here. `frappe.local` is where
this app parks per-request scratch — the identity `security.capture_calling_user`
saved, v0.17.2's record of which door the caller came in through, `form_dict`,
the response's status code. Real Frappe rebuilds that namespace on every
request, and the standalone suite goes to some trouble to reproduce exactly
that, precisely because one caller's answer surviving into the next caller's
audit row is a bug that cannot happen on a site and would be invisible if it
did. A worker-lifetime `frappe.local` would make it possible here.

The cost is one MySQL connect per call against a database in the same container.
At sixty reads a minute per phone that is not a number worth trading a
cross-request identity leak for.

────────────────────────────────────────────────────────────────────────────
COMMIT AND ROLLBACK ARE THIS MODULE'S JOB NOW
────────────────────────────────────────────────────────────────────────────

`claim_task`, `start_task` and `complete_task_via_mobile` write. On the
whitelisted path Frappe commits at the end of a successful request and rolls
back on an exception, and every one of those wrappers was written expecting it —
`guard._record` even rolls back deliberately on a refusal and then commits the
audit row on its own transaction, which only works if somebody else owns the
outer one. **Without the commit below, a worker would claim a task, get a 200,
and find it unclaimed on the next refresh.** With a commit but no rollback, a
half-applied completion would survive an error.

READS COMMIT TOO, and that is not sloppiness. `guard._stamp_last_seen` writes
`last_seen_on` on the Mobile Access Grant from inside a read, and that stamp is
the only thing `sweep_idle_grants` has to go on when it decides whether a token
belongs to a phone somebody lost in an orchard a month ago. A read path that
never committed would silently disarm the idle sweep.

────────────────────────────────────────────────────────────────────────────
IT WORKS WITHOUT A REAL FRAPPE
────────────────────────────────────────────────────────────────────────────

`init`, `connect` and `destroy` are called through `getattr`, so the standalone
suite — whose `frappe` is a double with a fake database and no such functions —
drives this module's real code rather than a mock of it. That is the same choice
`fallback_auth` made about `resolve`, and for the same reason: a lifecycle that
is only exercised in production is a lifecycle nobody has tested.
"""

from __future__ import annotations

import contextlib
import os

import frappe

#: The site to open. `SITE_NAME` is what the Umbrel compose already sets on the
#: ERPNext container (`SITE_NAME: frontend`), so the sidecar and the site it
#: serves cannot drift apart by being configured in two places.
SITE_ENV = ("FARMOPS_API_SITE", "FRAPPE_SITE", "SITE_NAME")
DEFAULT_SITE = "frontend"

#: The bench's `sites/` directory. `frappe.init` defaults this to `"."` and
#: every `bench` command satisfies that by chdir-ing there first, which is what
#: `directory=` in the supervisord entry is doing. Passing it explicitly means a
#: supervisord that ever loses that line produces a working service rather than
#: a "site does not exist" on the first call from a phone.
SITES_PATH = "/home/frappe/frappe-bench/sites"

#: Per-request scratch that must not outlive the request. Real `frappe.init`
#: rebuilds `frappe.local` and makes this moot; it is popped by hand so the
#: guarantee holds under the test double too, which is where a leak would
#: otherwise go unnoticed.
_SCRATCH = (
	"erpnext_mcp_calling_user",
	"erpnext_mcp_fallback_auth_source",
	"form_dict",
)


def site_name() -> str:
	for variable in SITE_ENV:
		value = str(os.environ.get(variable) or "").strip()
		if value:
			return value
	return DEFAULT_SITE


def sites_path() -> str:
	"""Where the site lives. `FARMOPS_API_SITES_PATH` overrides for a dev bench."""
	value = str(os.environ.get("FARMOPS_API_SITES_PATH") or "").strip()
	if value:
		return value
	return SITES_PATH if os.path.isdir(SITES_PATH) else "."


@contextlib.contextmanager
def request_session(request=None, body=None):
	"""Open a Frappe session for one HTTP request, and close it whichever way it went.

	Yields nothing: the caller works through `frappe` exactly as a whitelisted
	method does, which is the point — the eleven wrappers must not be able to
	tell which transport called them.

	`frappe.local.request` is set to the incoming request because
	`security.caller_ip()` reads `X-Forwarded-For` off it and falls back to
	`remote_addr`, and the audit row this service writes has to record the same
	IP the whitelisted path would have recorded. Rightmost hop, not leftmost —
	see `security.py`; an audit log that records an attacker's chosen address is
	worse than one that records none.
	"""
	initialise = getattr(frappe, "init", None)
	connect = getattr(frappe, "connect", None)
	destroy = getattr(frappe, "destroy", None)

	if callable(initialise):
		# `force=True` because `frappe.init` RETURNS EARLY when `frappe.local`
		# is already initialised, and this is a long-lived worker process. The
		# `finally` below destroys on every path, but a `destroy` that itself
		# failed would otherwise leave the next request running on the previous
		# caller's `frappe.local` — which is the one failure mode in this file
		# that would be a security bug rather than an outage.
		initialise(site=site_name(), sites_path=sites_path(), force=True)
	if callable(connect):
		connect()

	for key in _SCRATCH:
		try:
			frappe.local.pop(key, None)
		except Exception:  # pragma: no cover - a local that is not a mapping
			pass
	frappe.local.request = request
	# `frappe._dict` and NOT a plain dict. v0.175.0 found it on a real bench:
	# Frappe reads `frappe.local.form_dict.cmd` BY ATTRIBUTE inside
	# `get_user_permissions`, which `Document.save` reaches whenever it has to
	# fill a child row's defaults while a request is present. A plain dict
	# raised AttributeError there and the enrolment exchange answered 500; the
	# double never calls that code, so only a bench could show it.
	make = getattr(frappe, "_dict", None) or dict
	frappe.local.form_dict = make(body or {})
	try:
		frappe.local.response = frappe._dict() if hasattr(frappe, "_dict") else {}
	except Exception:  # pragma: no cover
		pass

	try:
		yield
	finally:
		if callable(destroy):
			try:
				destroy()
			except Exception:  # pragma: no cover - a teardown must not mask the answer
				pass


def become(user: str) -> None:
	"""Run the rest of this request as the caller.

	`frappe.set_user` and not an assignment to `frappe.session.user`, for the
	reason `fallback_auth._establish` gives at length: becoming somebody resets
	the per-request role-permission and User Permission caches, and entity
	scoping — `roles.companies_for`, which is the whole of `guard.require_scope`
	— reads exactly those. A session carrying one identity's cached permissions
	under another's name is scoped to the wrong farm.
	"""
	frappe.set_user(user)


def commit() -> None:
	try:
		frappe.db.commit()
	except Exception:  # pragma: no cover - nothing open, or no db on this double
		pass


def rollback() -> None:
	try:
		frappe.db.rollback()
	except Exception:  # pragma: no cover - nothing open, or no db on this double
		pass


def status_hint(default: int) -> int:
	"""The status `guard` asked for on `frappe.local.response`, or `default`.

	`guard._set_status` writes 503 for the kill switch and 429 for the rate
	limit there, because on the whitelisted path that dict IS the response.
	Reading it back means those two answers reach the phone as themselves even
	if the exception mapping in `app.py` ever falls behind — and the app's
	behaviour turns on them: 401 means "sign out and lose the queue", 503 and
	429 mean "wait, you are still logged in".
	"""
	try:
		code = int((frappe.local.response or {}).get("http_status_code") or 0)
	except Exception:  # pragma: no cover - no response object
		return default
	return code if 100 <= code < 600 else default
