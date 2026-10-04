# SPDX-License-Identifier: MIT
"""Same request, same result — for phone routes that had no idempotency key. v0.229.0.

docs/design/offline_mill_creek.md §5–6. A queued write is sent again whenever its
answer is lost: a timeout after the server committed, an app killed between the
reply and the delete. Ending a shift, signing a class sheet, completing a class and
reporting a problem each refused or doubled on a second send. With `client_request_id`
the second send gets the first answer back (`replayed: true`) and nothing is written.

THE RECEIPT IS AN AUDIT ROW, NOT A NEW TABLE — the same store `wizard_config` uses
for `client_reference`. It is written in the request's own transaction, so a write
that rolled back leaves no receipt behind and a retry runs for real.
"""

from __future__ import annotations

import functools
import json

import frappe

from . import audit


def _key(method: str, user: str, request_id: str) -> str:
	return f"once:{method}:{user}:{request_id}"[:140]


def earlier(method: str, user: str, request_id: str):
	if not request_id:
		return None
	rows = frappe.db.get_all(
		audit.LOG_DOCTYPE,
		filters={"tool_name": _key(method, user, request_id), "result_status": audit.STATUS_SUCCESS},
		fields=["result_summary", "timestamp"],
		limit=1,
	)
	if not rows:
		return None
	try:
		first = json.loads(rows[0]["result_summary"] or "{}")
	except ValueError:
		first = {}
	return {**(first if isinstance(first, dict) else {}), "first_sent_at": str(rows[0]["timestamp"])}


def _compact(answer: dict) -> dict:
	"""The answer's top-level scalars — what a resend needs to know it landed."""
	out = {}
	for key, value in (answer or {}).items():
		if isinstance(value, (str, int, float, bool)) or value is None:
			out[key] = value
	text = json.dumps(out, default=str)
	while len(text) > 1900 and out:
		out.pop(max(out, key=lambda k: len(str(out[k]))))
		text = json.dumps(out, default=str)
	return out


def remember(method: str, user: str, request_id: str, answer: dict) -> None:
	audit.record(
		_key(method, user, request_id),
		{"client_request_id": request_id},
		audit.STATUS_SUCCESS,
		json.dumps(_compact(answer), default=str),
		commit=False,
	)


def idempotent(method: str):
	"""Decorate a phone route that declares `client_request_id`. Goes under `guard.endpoint`."""

	def wrap(function):
		@functools.wraps(function)
		def inner(*args, **kwargs):
			key = str(kwargs.get("client_request_id") or "").strip()[:100]
			user = str(kwargs.get("user") or (args[0] if args else "") or frappe.session.user)
			if key:
				done = earlier(method, user, key)
				if done is not None:
					return {**done, "replayed": True}
			answer = function(*args, **kwargs)
			if key and isinstance(answer, dict):
				remember(method, user, key, answer)
			return answer

		return inner

	return wrap
