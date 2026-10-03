# SPDX-License-Identifier: MIT
"""The HTTP transport. One endpoint:

    POST /api/method/erpnext_mcp.mcp.handle

That is a plain Frappe whitelisted method, so it inherits the site's existing
reverse proxy, TLS, nginx rate limits and access logs rather than opening a
second listener that an operator would have to secure separately. It is also
why there is no bench command to "start the MCP server": it is up whenever the
site is.

WHY A werkzeug Response INSTEAD OF A RETURN VALUE. A whitelisted method's return
value becomes `{"message": ...}` with status 200, and this endpoint has to answer
401, 403, 404 and 202 with a bare JSON-RPC body and no `message` wrapper — an MCP
client parses the body as JSON-RPC and a wrapper breaks it. Frappe's
`handler.handle()` passes a returned `Response` straight through, which is the
supported way to control status and content type from a whitelisted method.

WHY POST-ONLY. This server never initiates a message, so there is nothing for a
server-sent-events stream to carry. Streamable HTTP allows exactly this: reply to
each POST directly. Clients that ask for `text/event-stream` get their response
as a single SSE frame, which they accept; a GET (the old HTTP+SSE transport's
stream-open) gets a 405 that says so, rather than an idle connection that looks
like it is working.

WHO THE CALL RUNS AS. Guest until the bearer token checks out, then
`frappe.set_user()` to the configured MCP System User (or Administrator), so
every document a mutating tool creates carries that `owner` and every permission
check a `.insert()`/`.submit()` runs is that user's. Read tools use
`frappe.db.get_all`, which does not consult role permissions — the bearer token
is the read authorization, per docs/security.md.
"""

import json

import frappe
from werkzeug.wrappers import Response

from . import audit, oauth, protocol, security, settings
from .errors import AuthError

_JSON = "application/json"
_SSE = "text/event-stream"


@frappe.whitelist(allow_guest=True, methods=["POST", "GET"])
def handle():
	"""The MCP endpoint. Returns a werkzeug Response in every case."""
	request = getattr(frappe.local, "request", None)
	method = getattr(request, "method", "POST")

	if method == "GET":
		# Answered without authorizing: a 405 leaks nothing an OPTIONS probe
		# would not, and an operator who has pointed a legacy HTTP+SSE client at
		# this URL needs to read this message rather than a 401.
		return _respond(
			protocol.rpc_error(
				None,
				protocol.INVALID_REQUEST,
				"this endpoint is POST-only (MCP Streamable HTTP). It sends no "
				"server-initiated messages, so there is no SSE stream to open.",
			),
			405,
		)

	try:
		caller_ip = security.authorize()
	except AuthError as exc:
		_audit_rejection(exc)
		headers = {}
		if exc.http_status == 401 and oauth.enabled():
			# v0.220.0: what sends an MCP client to OAuth discovery (RFC 9728 §5.1).
			headers["WWW-Authenticate"] = oauth.www_authenticate()
		return _respond(
			protocol.rpc_error(None, protocol.INVALID_REQUEST, str(exc)), exc.http_status, headers
		)

	# ORDER MATTERS, AND IT IS THE ONLY PLACE IT DOES. Frappe has already
	# authenticated whatever `Authorization: token <key>:<secret>` the caller
	# sent, so `frappe.session.user` is the mobile worker RIGHT NOW and is the
	# MCP System User one line later. v0.17.0's per-user scoping reads what this
	# saves. See `security.capture_calling_user`.
	#
	# v0.220.0: an OAuth client is nobody's phone. Its session user is the MCP
	# OAuth user the auth hook set, never a person to scope by.
	if oauth.current() is not None:
		security.forget_calling_user()
		frappe.set_user(oauth.agent_user())
	else:
		security.capture_calling_user()
		frappe.set_user(settings.effective_user())

	try:
		payload = json.loads(frappe.request.get_data(as_text=True) or "")
	except (ValueError, TypeError):
		return _respond(
			protocol.rpc_error(None, protocol.PARSE_ERROR, "request body is not valid JSON"),
			400,
		)

	body, status = protocol.handle_payload(payload, caller_ip)
	if body is None:
		# Notification(s) only. 202 with no content is what the spec asks for.
		return Response(status=202, content_type=_JSON)
	return _respond(body, status)


@frappe.whitelist()
def selftest():
	"""Configuration report for the settings form's Test Connection button.

	System-Manager-only (a plain `@frappe.whitelist()` requires a session, and
	the doctype's own permissions gate the form). It never returns or echoes the
	token — only whether one exists — because the point of the Password field is
	that nothing reads it back out, including diagnostics.
	"""
	frappe.only_for("System Manager")
	from . import registry

	enabled_tools = [
		name for name in registry.TOOLS if settings.tool_enabled(name) and registry.is_available(name)
	]
	mutating_on = [name for name in mutating_tool_names() if settings.tool_enabled(name)]
	unavailable = {
		name: registry.TOOLS[name]["requires"] for name in registry.TOOLS if not registry.is_available(name)
	}
	return {
		"enabled": settings.is_enabled(),
		"token_configured": bool(settings.auth_token()),
		"allowed_cidrs": settings.allowed_cidrs(),
		"require_user_context": settings.require_user_context(),
		"effective_user": settings.effective_user(),
		"endpoint": "/api/method/erpnext_mcp.mcp.handle",
		"protocol_versions": list(protocol.SUPPORTED_PROTOCOL_VERSIONS),
		"tools_total": len(registry.TOOLS),
		"tools_enabled": enabled_tools,
		"mutating_tools_enabled": mutating_on,
		"tools_unavailable": unavailable,
		"ready": bool(settings.is_enabled() and settings.auth_token() and enabled_tools),
	}


@frappe.whitelist()
def mutating_tool_names() -> list:
	"""Every tool that can change data, straight from the registry.

	Whitelisted because the settings form needs it: hardcoding the list in
	JavaScript is how a form ends up quietly telling an operator "read-only" while
	a write tool is live. The form asks the server, and the server derives the
	answer from the same structure the dispatcher gates on.
	"""
	from . import registry as tool_registry

	return list(tool_registry.MUTATING_TOOLS)


def _audit_rejection(exc: AuthError) -> None:
	"""Log a rejected call — but only when the endpoint is actually live.

	When the master switch is off or no token exists there is nothing to abuse
	yet, and logging those would let anyone who can reach the URL grow the table
	at will. Once the endpoint is live, a rejected call is a real event and worth
	a row.
	"""
	if not settings.is_enabled() or not (settings.auth_token() or oauth.enabled()):
		return
	audit.record(
		"<transport>",
		{"method": getattr(getattr(frappe.local, "request", None), "method", "")},
		audit.STATUS_UNAUTHORIZED,
		exc.log_reason,
		caller_ip=security.caller_ip(),
		commit=True,
	)


def _respond(body, status: int, headers: dict | None = None) -> Response:
	"""Serialise a JSON-RPC body, as SSE when the client asked for it."""
	text = json.dumps(body, default=str)
	headers = headers or {}
	if _SSE in (frappe.get_request_header("Accept") or ""):
		# One `message` event carrying the whole response. Streamable HTTP
		# permits a single-frame stream, and clients that set this Accept header
		# handle it; the blank line terminates the event.
		return Response(
			f"event: message\ndata: {text}\n\n",
			status=status,
			content_type=f"{_SSE}; charset=utf-8",
			headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **headers},
		)
	return Response(text, status=status, content_type=f"{_JSON}; charset=utf-8", headers=headers)
