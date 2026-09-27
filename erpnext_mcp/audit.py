# SPDX-License-Identifier: MIT
"""Every call gets a row in MCP Action Log. No exceptions, including the
rejected ones.

WHY READS ARE LOGGED TOO. A read tool cannot corrupt the ledger, but the
interesting question after the fact is rarely "what did it change" — it is
"what did it see". A log that only records mutations cannot answer whether an
AI client enumerated every account before it was switched off, so this one
records the lot.

SURVIVING A ROLLBACK. A Frappe request is one transaction. If a mutating tool
half-wrote a Journal Entry and then raised, the caller must roll that back — and
a naive audit row inserted before the rollback would go with it, losing exactly
the record you most want. So the contract is: `mcp.py` rolls back FIRST, then
calls `record(..., commit=True)`, so the failure row is inserted into a clean
transaction and committed on its own. This module never rolls back on its own
behalf; doing so would discard a caller's still-valid work.

BEST EFFORT, NEVER FATAL. An audit write that fails must not turn a working
tool call into a 500, and must not swallow the real error either. Failures here
go to the site's Error Log and are otherwise ignored.

REDACTION. Arguments are logged verbatim except for keys that name a secret —
no current tool takes one, but a future tool might, and a log that is read-only
forever is the wrong place to discover that.

ELISION IS NOT TRUNCATION. A single huge value is elided down to a note of its
length *before* the whole payload is serialised, because truncating afterwards
throws away whatever sorts after it. `attach_file_to_document` is where that
stopped being theoretical: its `file_content` is megabytes of base64 and sorts
ahead of `file_name`, `is_private` and `name`, so a whole-payload truncation
would leave a row recording that a file was attached and nothing about which
file or to what. The elided note keeps the length, which is the only part of
eight megabytes of base64 anybody reads a log for; the sha256 that identifies
the bytes is in `result_summary`.
"""

import json

import frappe

from .compat import traceback_text

LOG_DOCTYPE = "MCP Action Log"

STATUS_SUCCESS = "Success"
STATUS_ERROR = "Error"
STATUS_BLOCKED = "Blocked"
STATUS_UNAUTHORIZED = "Unauthorized"

#: Column widths in the doctype. Truncating here rather than letting MariaDB
#: do it means the log says it truncated.
_MAX_ARGS = 8000
_MAX_SUMMARY = 2000

#: Longest single argument value kept verbatim. Comfortably past any narrative,
#: remark or account name a tool takes, and far short of a base64 payload.
_MAX_VALUE = 512

_SECRET_HINTS = ("token", "password", "secret", "api_key", "apikey", "credential")

#: v0.196.0. WHICH MODEL MADE THE CALL. Every call on both transports already
#: writes one row here, so attribution is two columns on that row rather than a
#: second log: `X-Agent-Model` names the model ("claude-opus-4-6",
#: "llama-3.1-8b-q4"), `X-Agent-Session` groups one conversation's calls. Both
#: optional — a call without them is logged exactly as before. They are the
#: CLIENT'S CLAIM, like `role` on App Feedback: attribution, never authority.
#: Nothing gates on them, and the authenticated user is still the identity.
AGENT_MODEL_HEADER = "X-Agent-Model"
AGENT_SESSION_HEADER = "X-Agent-Session"
_MAX_AGENT = 140


def record(
	tool_name: str,
	arguments: dict | None = None,
	status: str = STATUS_SUCCESS,
	summary: str = "",
	docstatus_delta: str = "",
	caller_ip: str = "",
	commit: bool = False,
) -> str | None:
	"""Insert one audit row. Returns its name, or None if the write failed.

	`commit=True` is for rows that must outlive a rolled-back transaction; see
	the module docstring.
	"""
	try:
		doc = frappe.get_doc(
			{
				"doctype": LOG_DOCTYPE,
				"timestamp": frappe.utils.now(),
				"tool_name": (tool_name or "<none>")[:140],
				"caller_ip": (caller_ip or "")[:140],
				"arguments_json": _arguments_json(arguments),
				"result_status": status,
				"result_summary": _truncate(summary or "", _MAX_SUMMARY),
				"docstatus_delta": (docstatus_delta or "")[:140],
				**_agent_columns(),
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		if commit:
			frappe.db.commit()
		return doc.name
	except Exception:
		# Deliberately swallowed: see module docstring. frappe.log_error opens
		# its own transaction, so this survives even a poisoned one.
		try:
			frappe.log_error(
				title="erpnext_mcp: audit log write failed",
				message=traceback_text(),
			)
		except Exception:
			pass
		return None


def agent_identity() -> tuple[str, str]:
	"""(model, session) from this request's `X-Agent-*` headers, or ("", ""). NEVER RAISES.

	Its own try, separate from `record`'s: a header that will not read — no
	request at all in a scheduled job, an odd proxy — must cost the attribution
	and never the audit row.
	"""
	try:
		model = frappe.get_request_header(AGENT_MODEL_HEADER) or ""
		session = frappe.get_request_header(AGENT_SESSION_HEADER) or ""
	except Exception:
		return "", ""
	return _clean_agent(model), _clean_agent(session)


def _clean_agent(value) -> str:
	"""Printable, trimmed, capped — a header is free text from the client."""
	text = "".join(ch for ch in str(value or "") if ch.isprintable()).strip()
	return text[:_MAX_AGENT]


def _agent_columns() -> dict:
	"""The two columns, only when a header said something."""
	model, session = agent_identity()
	out = {}
	if model:
		out["agent_model"] = model
	if session:
		out["agent_session"] = session
	return out


def _arguments_json(arguments: dict | None) -> str:
	"""Arguments as JSON, secrets masked, oversized values elided, then truncated."""
	safe = {}
	for key, value in (arguments or {}).items():
		lowered = str(key).lower()
		if any(hint in lowered for hint in _SECRET_HINTS):
			safe[key] = "***redacted***"
		elif isinstance(value, str) and len(value) > _MAX_VALUE:
			safe[key] = f"<{len(value)} characters elided>"
		else:
			safe[key] = value
	try:
		text = json.dumps(safe, default=str, sort_keys=True)
	except Exception:
		text = repr(safe)
	return _truncate(text, _MAX_ARGS)


def _truncate(text: str, limit: int) -> str:
	if len(text) <= limit:
		return text
	return text[: limit - 15] + "…[truncated]"
