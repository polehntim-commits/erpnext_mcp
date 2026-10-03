# SPDX-License-Identifier: MIT
"""OAuth 2.1 for MCP clients, approved on a manager's phone. v0.220.0.

docs/design/device_client_enrollment.md §6.3–§6.4. OFF until `mcp_oauth_enabled`
is ticked on ERPNext MCP Settings; until then every path here answers as a path
that does not exist, and the static X-MCP-Token is the only way in, exactly as
before. With it on, the static token keeps working while
`legacy_static_mcp_token` is ticked (the default).

THE SHAPE, END TO END.

1. The client finds us: a 401 from the MCP endpoint carries
   `WWW-Authenticate: Bearer resource_metadata=…`, and both discovery documents
   are served under `/.well-known/` by `WellKnownPage` (a `page_renderer`).
2. It registers (RFC 7591). Registration WRITES NOTHING: the client_id is the
   client's name and redirect URIs, signed with this site's key. An open
   endpoint that inserted a row per call would be a table anybody could grow.
3. It sends the person's browser to `authorize`. That page writes one Pending
   `Farm Access Request` (kind `mcp_client`) and shows its code and QR — no
   password is ever typed into it.
4. A System Manager (or a Farm Manager, for read-only scopes) approves the code
   on their phone with Face ID, or in the Desk, choosing the scopes. A client
   never receives a scope it did not ask for. NO MCP TOOL CAN APPROVE.
5. The page, polling, gets a one-time code and redirects; the client trades it
   (PKCE S256, exact redirect URI) for a 60-minute access token and a refresh
   token that rotates on every use. A refresh token presented twice ends the
   whole family and raises a security alert — that is the theft signal.
6. On each MCP call the `auth_hooks` entry `authenticate` turns a live Bearer
   token into the MCP OAuth user, for THE MCP ENDPOINT ONLY. The tools the
   client sees are its scopes ∩ the site's `allow_<tool>` switches.

Every token is random, single-purpose and stored only as its SHA-256 (`Farm
Access Token`, shared with device keys). The approved request row IS the client:
revoking it ends every token it holds on the next call.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import secrets
import time
from urllib.parse import urlencode, urlsplit

import frappe

from . import compat, settings

MCP_PATH = "/api/method/erpnext_mcp.mcp.handle"
REGISTER_PATH = "/api/method/erpnext_mcp.oauth.register"
AUTHORIZE_PATH = "/api/method/erpnext_mcp.oauth.authorize"
STATUS_PATH = "/api/method/erpnext_mcp.oauth.authorize_status"
TOKEN_PATH = "/api/method/erpnext_mcp.oauth.token"
REVOKE_PATH = "/api/method/erpnext_mcp.oauth.revoke"
PRM_PATH = "/.well-known/oauth-protected-resource"
ASM_PATH = "/.well-known/oauth-authorization-server"

KIND = "mcp_client"
SCOPE_READ = "mcp:read"
WRITE_PREFIX = "mcp:write:"
TOOL_PREFIX = "mcp:tool:"
#: The two profiles an approver picks from; "custom" is any subset of the ask.
PROFILES = {"read": (SCOPE_READ,), "read_farm": (SCOPE_READ, "mcp:write:farm")}

ACCESS_MIN, ACCESS_MAX, ACCESS_DEFAULT = 5, 1440, 60
REFRESH_MIN, REFRESH_MAX, REFRESH_DEFAULT = 1, 90, 30
#: However often it is used, a refresh family dies this long after approval.
ABSOLUTE_DAYS = 90
CODE_SECONDS = 60
REQUEST_MINUTES = 10
AUTHORIZE_PER_HOUR_PER_ADDRESS = 10
REGISTER_PER_HOUR_PER_ADDRESS = 20
TOKEN_FAILURES_PER_MINUTE = 10
MAX_REDIRECT_URIS = 5
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")
CLIENT_ID_PREFIX = "fo1"
STATIC_LAST_USED_KEY = "erpnext_mcp_static_mcp_token_last_used"
STATIC_TRACKED_SINCE_KEY = "erpnext_mcp_static_mcp_token_tracked_since"
STATIC_QUIET_DAYS = 14

#: Where `authenticate` parks the grant for this request (per-request namespace).
_LOCAL_KEY = "erpnext_mcp_oauth"


class OAuthError(Exception):
	"""An RFC 6749 error: `error` is the code a client acts on."""

	def __init__(self, error: str, description: str = "", status: int = 400):
		super().__init__(description or error)
		self.error = error
		self.description = description
		self.status = status


# ── settings ────────────────────────────────────────────────────────────────
def _setting(name, default):
	value = settings._value(name)
	return default if value in (None, "") else value


def _bounded(name, default, low, high) -> int:
	try:
		value = int(_setting(name, default))
	except (TypeError, ValueError):
		value = default
	return max(low, min(high, value))


def enabled() -> bool:
	return settings.as_bool(_setting("mcp_oauth_enabled", 0))


def legacy_static_allowed() -> bool:
	"""The static X-MCP-Token works: always with OAuth off, else while the setting is ticked."""
	return not enabled() or settings.as_bool(_setting("legacy_static_mcp_token", 1))


def access_minutes() -> int:
	return _bounded("mcp_access_token_minutes", ACCESS_DEFAULT, ACCESS_MIN, ACCESS_MAX)


def refresh_days() -> int:
	return _bounded("mcp_refresh_days", REFRESH_DEFAULT, REFRESH_MIN, REFRESH_MAX)


def agent_user() -> str:
	"""Who an OAuth client runs as: `mcp_oauth_user`, else the MCP System User."""
	user = str(_setting("mcp_oauth_user", "") or "").strip()
	if user and frappe.db.exists("User", user):
		return user
	return settings.effective_user()


def issuer() -> str:
	"""This authorization server's origin, as the client must see it.

	`mcp_oauth_issuer` when set (the tailnet https origin — set it: proxies on
	the way rewrite scheme and host), else the Public URL's origin, else what
	this request says it was sent to.
	"""
	for candidate in (_setting("mcp_oauth_issuer", ""), settings.public_url()):
		origin = _origin(candidate)
		if origin:
			return origin
	request = getattr(frappe.local, "request", None)
	scheme = (
		frappe.get_request_header("X-Forwarded-Proto") or getattr(request, "scheme", "") or "https"
	).split(",")[0]
	host = (frappe.get_request_header("X-Forwarded-Host") or getattr(request, "host", "") or "").split(",")[0]
	return f"{scheme.strip()}://{host.strip()}" if host else ""


def _origin(url) -> str:
	parts = urlsplit(str(url or "").strip())
	if parts.scheme not in ("http", "https") or not parts.netloc:
		return ""
	return f"{parts.scheme}://{parts.netloc}"


def resource_url() -> str:
	return issuer() + MCP_PATH


# ── scopes ──────────────────────────────────────────────────────────────────
def parse_scopes(text) -> list:
	"""Scope strings, validated and de-duplicated in order. Raises OAuthError(invalid_scope)."""
	from . import registry, tool_groups

	out = []
	for scope in str(text or "").replace(",", " ").split():
		if scope == SCOPE_READ:
			pass
		elif scope.startswith(WRITE_PREFIX) and scope[len(WRITE_PREFIX) :] in tool_groups.DOMAIN_KEYS:
			pass
		elif scope.startswith(TOOL_PREFIX) and scope[len(TOOL_PREFIX) :] in registry.TOOLS:
			pass
		else:
			raise OAuthError("invalid_scope", f"unknown scope {scope[:80]!r}")
		if scope not in out:
			out.append(scope)
	return out


def scopes_supported() -> list:
	from . import tool_groups

	return [SCOPE_READ, *(WRITE_PREFIX + key for key in tool_groups.DOMAIN_KEYS)]


def granted_scopes(requested, profile: str = "", scopes="") -> list:
	"""What an approver's choice grants: always a subset of what was asked for."""
	asked = parse_scopes(" ".join(requested) if isinstance(requested, (list, tuple)) else requested)
	if profile:
		if profile not in PROFILES:
			raise OAuthError(
				"invalid_scope", f"profile must be one of {', '.join(PROFILES)} or custom scopes"
			)
		chosen = set(PROFILES[profile])
	else:
		chosen = set(parse_scopes(scopes))
	return [scope for scope in asked if scope in chosen]


def scope_text(scopes) -> str:
	"""The canonical form a phone signs: sorted, space-separated."""
	return " ".join(sorted(set(scopes)))


def may_approve(approver: str, scopes) -> bool:
	roles = set(frappe.get_roles(approver) or [])
	if "System Manager" in roles:
		return True
	return "Farm Manager" in roles and set(scopes) <= {SCOPE_READ}


def current() -> dict | None:
	"""This request's OAuth grant, or None (static token, or no OAuth)."""
	grant = getattr(frappe.local, _LOCAL_KEY, None)
	return grant if isinstance(grant, dict) else None


def permits(tool_name: str) -> bool:
	"""Whether this request's scopes cover the tool. True when there is no OAuth grant."""
	grant = current()
	if grant is None:
		return True
	from . import registry, tool_groups

	scopes = set(str(grant.get("scopes") or "").split())
	if TOOL_PREFIX + tool_name in scopes:
		return True
	spec = registry.TOOLS.get(tool_name)
	if spec is None:
		return False
	if spec["mutating"]:
		return WRITE_PREFIX + tool_groups.domain_of(tool_name) in scopes
	return SCOPE_READ in scopes


# ── client ids: signed, stored nowhere ──────────────────────────────────────
def _b64u(data: bytes) -> str:
	from .device_keys import b64u

	return b64u(data)


def _b64u_decode(text: str) -> bytes:
	from .device_keys import b64u_decode

	return b64u_decode(text)


def _client_key() -> bytes:
	"""A key only this site knows. Empty → registration is refused (fail closed)."""
	secret = ""
	try:
		from frappe.utils.password import get_encryption_key

		secret = str(get_encryption_key() or "")
	except Exception:
		secret = str((getattr(frappe, "conf", None) or {}).get("encryption_key") or "")
	if not secret:
		return b""
	return hashlib.sha256(f"erpnext_mcp.oauth.client|{secret}".encode()).digest()


def make_client_id(name: str, redirect_uris: list) -> str:
	key = _client_key()
	if not key:
		raise OAuthError(
			"temporarily_unavailable", "this site has no encryption key to sign a client id", 503
		)
	payload = _b64u(
		json.dumps(
			{"n": name, "r": redirect_uris, "t": int(time.time()), "j": secrets.token_hex(6)},
			separators=(",", ":"),
		).encode()
	)
	mac = _b64u(hmac.new(key, payload.encode(), hashlib.sha256).digest()[:18])
	return f"{CLIENT_ID_PREFIX}.{payload}.{mac}"


def parse_client(client_id) -> dict | None:
	"""`{name, redirect_uris}` for a client id this site signed, else None."""
	try:
		prefix, payload, mac = str(client_id or "").split(".")
	except ValueError:
		return None
	key = _client_key()
	if prefix != CLIENT_ID_PREFIX or not key:
		return None
	expected = _b64u(hmac.new(key, payload.encode(), hashlib.sha256).digest()[:18])
	if not hmac.compare_digest(expected, mac):
		return None
	try:
		data = json.loads(_b64u_decode(payload))
	except ValueError:
		return None
	return {"name": str(data.get("n") or "MCP client"), "redirect_uris": list(data.get("r") or [])}


def _valid_redirect(uri: str) -> bool:
	parts = urlsplit(str(uri or ""))
	if parts.fragment or not parts.netloc or len(uri) > 300:
		return False
	if parts.scheme == "https":
		return True
	return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


def redirect_matches(registered: list, presented: str) -> bool:
	"""Exact match; a loopback URI may differ only in its port (RFC 8252 §7.3)."""
	if presented in registered:
		return True
	asked = urlsplit(str(presented or ""))
	if asked.scheme != "http" or (asked.hostname or "") not in LOOPBACK_HOSTS:
		return False
	for uri in registered:
		known = urlsplit(uri)
		if (known.scheme, known.hostname, known.path, known.query) == (
			asked.scheme,
			asked.hostname,
			asked.path,
			asked.query,
		):
			return True
	return False


# ── discovery ───────────────────────────────────────────────────────────────
def protected_resource_metadata() -> dict:
	return {
		"resource": resource_url(),
		"authorization_servers": [issuer()],
		"scopes_supported": scopes_supported(),
		"bearer_methods_supported": ["header"],
		"resource_name": "ERPNext MCP",
	}


def authorization_server_metadata() -> dict:
	base = issuer()
	return {
		"issuer": base,
		"authorization_endpoint": base + AUTHORIZE_PATH,
		"token_endpoint": base + TOKEN_PATH,
		"registration_endpoint": base + REGISTER_PATH,
		"revocation_endpoint": base + REVOKE_PATH,
		"response_types_supported": ["code"],
		"grant_types_supported": ["authorization_code", "refresh_token"],
		"code_challenge_methods_supported": ["S256"],
		"token_endpoint_auth_methods_supported": ["none"],
		"revocation_endpoint_auth_methods_supported": ["none"],
		"scopes_supported": scopes_supported(),
		"client_id_metadata_document_supported": False,
		"authorization_response_iss_parameter_supported": True,
	}


def www_authenticate() -> str:
	"""The 401 header that sends an MCP client to discovery (RFC 9728 §5.1)."""
	return f'Bearer resource_metadata="{issuer()}{PRM_PATH}{MCP_PATH}", scope="{SCOPE_READ}"'


class WellKnownPage:
	"""`page_renderer` entry: the two discovery documents. Nothing else, never when off."""

	def __init__(self, path, http_status_code=None):
		self.path = "/" + str(path or "").strip("/ ")
		self.http_status_code = http_status_code or 200

	def _document(self):
		for prefix, build in (
			(PRM_PATH, protected_resource_metadata),
			(ASM_PATH, authorization_server_metadata),
		):
			if self.path == prefix or self.path.startswith(prefix + "/"):
				return build
		return None

	def can_render(self) -> bool:
		try:
			return self._document() is not None and enabled()
		except Exception:
			return False

	def render(self):
		return _json(self._document()(), 200, cors=True)


# ── responses ───────────────────────────────────────────────────────────────
def _response(body, status: int, content_type: str, headers: dict | None = None):
	from werkzeug.wrappers import Response

	return Response(body, status=status, content_type=content_type, headers=headers or {})


def _json(data, status: int = 200, cors: bool = False, headers: dict | None = None):
	out = {"Cache-Control": "no-store", "Pragma": "no-cache", **(headers or {})}
	if cors:
		out["Access-Control-Allow-Origin"] = "*"
	return _response(json.dumps(data, default=str), status, "application/json; charset=utf-8", out)


def _oauth_error(exc: OAuthError):
	body = {"error": exc.error}
	if exc.description:
		body["error_description"] = exc.description
	return _json(body, exc.status, cors=True)


def _not_found():
	return _response("Not Found", 404, "text/plain; charset=utf-8")


def _args() -> dict:
	"""Form fields, query string or a JSON body — whichever the client sent."""
	out = {}
	try:
		out.update({k: v for k, v in dict(frappe.local.form_dict or {}).items() if k != "cmd"})
	except Exception:
		pass
	request = getattr(frappe.local, "request", None)
	try:
		raw = request.get_data(as_text=True) if request is not None else ""
		if raw and raw.lstrip().startswith("{"):
			body = json.loads(raw)
			if isinstance(body, dict):
				out.update(body)
	except (ValueError, TypeError, AttributeError):
		pass
	return {str(k): v for k, v in out.items()}


def _ip() -> str:
	from . import security

	return security.caller_ip()


def _count(key: str, seconds: int) -> int:
	from .api import guard

	return guard._count(key, seconds)


def _stamp(seconds: int = 0) -> str:
	return str(frappe.utils.add_to_date(frappe.utils.now(), seconds=seconds))[:19]


# ── registration (RFC 7591) ─────────────────────────────────────────────────
@frappe.whitelist(allow_guest=True, methods=["POST"])
def register():
	if not enabled():
		return _not_found()
	try:
		if _count(f"oauth_register:{_ip() or 'unknown'}", 3600) > REGISTER_PER_HOUR_PER_ADDRESS:
			raise OAuthError("temporarily_unavailable", "too many registrations from this address", 429)
		answer = register_client(_args())
	except OAuthError as exc:
		return _oauth_error(exc)
	return _json(answer, 201, cors=True)


def register_client(body: dict) -> dict:
	uris = body.get("redirect_uris")
	if not isinstance(uris, list) or not uris or len(uris) > MAX_REDIRECT_URIS:
		raise OAuthError("invalid_redirect_uri", f"send 1–{MAX_REDIRECT_URIS} redirect_uris")
	uris = [str(uri) for uri in uris]
	bad = [uri for uri in uris if not _valid_redirect(uri)]
	if bad:
		raise OAuthError(
			"invalid_redirect_uri",
			"redirect URIs must be https, or http on localhost/127.0.0.1, with no fragment",
		)
	method = str(body.get("token_endpoint_auth_method") or "none")
	if method != "none":
		raise OAuthError(
			"invalid_client_metadata", "only public clients (token_endpoint_auth_method none, PKCE)"
		)
	for grant in body.get("grant_types") or ["authorization_code"]:
		if grant not in ("authorization_code", "refresh_token"):
			raise OAuthError("invalid_client_metadata", f"grant type {str(grant)[:40]!r} is not supported")
	name = " ".join(str(body.get("client_name") or "MCP client").split())[:80] or "MCP client"
	client_id = make_client_id(name, uris)
	return {
		"client_id": client_id,
		"client_id_issued_at": int(time.time()),
		"client_name": name,
		"redirect_uris": uris,
		"grant_types": ["authorization_code", "refresh_token"],
		"response_types": ["code"],
		"token_endpoint_auth_method": "none",
	}


# ── authorization: the consent page ─────────────────────────────────────────
@frappe.whitelist(allow_guest=True, methods=["GET"])
def authorize():
	if not enabled():
		return _not_found()
	args = _args()
	client = parse_client(args.get("client_id"))
	redirect_uri = str(args.get("redirect_uri") or "")
	if not client or not redirect_matches(client["redirect_uris"], redirect_uri):
		# Never redirect to a URI we cannot vouch for (RFC 6749 §4.1.2.1).
		return _page(
			"This sign-in link is not valid",
			"<p>The application that sent you here is not registered with this farm, or it asked to "
			"return somewhere it did not register. Start again from the application.</p>",
			400,
		)
	state = str(args.get("state") or "")
	try:
		opened = open_request(args, client, redirect_uri, _ip())
	except OAuthError as exc:
		if exc.status == 429:
			return _page(
				"Too many requests", "<p>Too many sign-in attempts from here. Wait an hour.</p>", 429
			)
		return _redirect(
			_with_query(
				redirect_uri, {"error": exc.error, "error_description": exc.description, "state": state}
			)
		)
	return _consent_page(client["name"], opened)


def open_request(args: dict, client: dict, redirect_uri: str, ip: str) -> dict:
	"""Write the Pending request a manager will approve. Raises OAuthError."""
	from .device_keys import MAX_OPEN_REQUESTS, REQUEST_DOCTYPE, _code_hash, _new_code

	if str(args.get("response_type") or "") != "code":
		raise OAuthError("unsupported_response_type", "response_type must be code")
	challenge = str(args.get("code_challenge") or "")
	if str(args.get("code_challenge_method") or "") != "S256" or not 43 <= len(challenge) <= 128:
		raise OAuthError("invalid_request", "PKCE with code_challenge_method=S256 is required")
	resource = str(args.get("resource") or "")
	if resource and urlsplit(resource).path.rstrip("/") != MCP_PATH:
		raise OAuthError("invalid_target", "resource must be this site's MCP endpoint")
	scopes = parse_scopes(args.get("scope") or SCOPE_READ)
	if not scopes:
		raise OAuthError("invalid_scope", "no scope requested")
	if _count(f"oauth_authorize:{ip or 'unknown'}", 3600) > AUTHORIZE_PER_HOUR_PER_ADDRESS:
		raise OAuthError("temporarily_unavailable", "too many requests", 429)
	open_count = len(
		frappe.db.get_all(
			REQUEST_DOCTYPE,
			filters={"status": "Pending", "expires_at": [">", _stamp()]},
			pluck="name",
			limit=MAX_OPEN_REQUESTS + 1,
		)
	)
	if open_count >= MAX_OPEN_REQUESTS:
		raise OAuthError("temporarily_unavailable", "too many open requests", 429)
	code = _new_code()
	poll = secrets.token_urlsafe(24)
	client_id = str(args.get("client_id") or "")
	doc = frappe.get_doc(
		{
			"doctype": REQUEST_DOCTYPE,
			"kind": KIND,
			"status": "Pending",
			"display_name": client["name"][:120],
			"code_hash": _code_hash(code),
			"expires_at": _stamp(REQUEST_MINUTES * 60),
			"ip": str(ip or "")[:60],
			"client_id": hashlib.sha256(client_id.encode()).hexdigest(),
			"redirect_uris": redirect_uri,
			"requested_scopes": " ".join(scopes),
			"client_metadata": json.dumps(
				{
					"client_name": client["name"],
					"client_id": client_id,
					"redirect_uri": redirect_uri,
					"code_challenge": challenge,
					"state": str(args.get("state") or "")[:500],
					"resource": resource,
					"poll_hash": hashlib.sha256(poll.encode()).hexdigest(),
				}
			),
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return {"request": doc.name, "code": code, "poll": poll, "scopes": scopes, "expires_at": doc.expires_at}


def _metadata(row) -> dict:
	try:
		data = json.loads(row.get("client_metadata") or "{}")
	except (TypeError, ValueError):
		data = {}
	return data if isinstance(data, dict) else {}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def authorize_status():
	"""The consent page polls; only the browser holding `poll` may collect."""
	if not enabled():
		return _not_found()
	args = _args()
	return _json(collect(str(args.get("request") or ""), str(args.get("poll") or "")))


def collect(request: str, poll: str) -> dict:
	"""`pending`, `expired`, or `redirect` with the code (once) or the denial."""
	from . import device_keys

	row = frappe.db.get_value(
		device_keys.REQUEST_DOCTYPE,
		{"name": request, "kind": KIND},
		["name", "status", "expires_at", "client_metadata", "granted_scopes", "approved_by"],
		as_dict=True,
		for_update=True,
	)
	meta = _metadata(row or {})
	if (
		not row
		or not poll
		or not meta.get("poll_hash")
		or not hmac.compare_digest(meta["poll_hash"], hashlib.sha256(poll.encode()).hexdigest())
	):
		return {"status": "unknown"}
	back = {"state": meta.get("state") or "", "iss": issuer()}
	if row["status"] == "Pending":
		if str(row.get("expires_at") or "") <= _stamp():
			frappe.db.set_value(
				device_keys.REQUEST_DOCTYPE, row["name"], "status", "Expired", update_modified=False
			)
			return {"status": "expired"}
		return {"status": "pending"}
	if row["status"] == "Denied":
		meta.pop("poll_hash", None)
		_save_metadata(row["name"], meta)
		return {
			"status": "redirect",
			"redirect": _with_query(meta["redirect_uri"], {"error": "access_denied", **back}),
		}
	if row["status"] != "Approved":
		return {"status": "expired"}
	meta.pop("poll_hash", None)
	family = secrets.token_hex(16)
	meta["family"] = family
	_save_metadata(row["name"], meta)
	code = device_keys.issue_token(
		"auth_code",
		str(row.get("approved_by") or ""),
		seconds=CODE_SECONDS,
		client=row["name"],
		scopes=str(row.get("granted_scopes") or ""),
		family=family,
	)
	frappe.db.commit()
	return {"status": "redirect", "redirect": _with_query(meta["redirect_uri"], {"code": code, **back})}


def _save_metadata(name: str, meta: dict) -> None:
	from .device_keys import REQUEST_DOCTYPE

	frappe.db.set_value(REQUEST_DOCTYPE, name, "client_metadata", json.dumps(meta), update_modified=False)


def _with_query(uri: str, params: dict) -> str:
	query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
	return uri + ("&" if "?" in uri else "?") + query


def _redirect(location: str):
	return _response("", 302, "text/plain", {"Location": location, "Cache-Control": "no-store"})


# ── approval (phone or Desk; never MCP) ─────────────────────────────────────
def decide_client(
	approver: str,
	row: dict,
	decision: str,
	via: str,
	scopes="",
	profile: str = "",
	signature: str = "",
	approver_device: str = "",
) -> dict:
	"""Approve (with scopes) or deny a client's request. Raises EnrollmentRefused."""
	from . import device_enrollment, device_keys, security_alerts

	refuse = device_enrollment.EnrollmentRefused
	try:
		granted = (
			granted_scopes(str(row.get("requested_scopes") or ""), profile=profile, scopes=scopes)
			if decision == "approve"
			else []
		)
	except OAuthError as exc:
		raise refuse(f"{exc.description}. Nothing was changed.") from None
	if decision == "approve" and not granted:
		raise refuse("choose at least one of the scopes the client asked for. Nothing was changed.")
	if decision == "approve" and not may_approve(approver, granted):
		raise refuse(
			"letting an AI client in takes a System Manager (a Farm Manager may grant read-only). "
			"Nothing was changed."
		)
	if via == "phone":
		device_keys._verify_approver(
			approver,
			approver_device,
			f"farmops-approve|{row['name']}|{scope_text(granted)}|{decision}",
			signature,
		)
	now = _stamp()
	values = {"approved_by": approver, "approved_at": now, "approved_via": via}
	if decision == "deny":
		frappe.db.set_value(
			device_keys.REQUEST_DOCTYPE, row["name"], {"status": "Denied", **values}, update_modified=False
		)
		return {"request": row["name"], "decision": "deny", "kind": KIND}
	frappe.db.set_value(
		device_keys.REQUEST_DOCTYPE,
		row["name"],
		{"status": "Approved", "granted_scopes": scope_text(granted), **values},
		update_modified=False,
	)
	security_alerts.send(
		"ERPNext MCP: an AI client was let in",
		f"{approver} approved the MCP client {row.get('display_name')!r} on {via} with {scope_text(granted)}. "
		f"It runs as {agent_user()}.",
	)
	return {"request": row["name"], "decision": "approve", "kind": KIND, "scopes": granted}


def describe(row: dict) -> dict:
	"""What an approver sees for a client request before deciding."""
	meta = _metadata(row)
	return {
		"kind": KIND,
		"client_name": row.get("display_name"),
		"returns_to": urlsplit(str(meta.get("redirect_uri") or "")).netloc,
		"requested_scopes": str(row.get("requested_scopes") or "").split(),
		"profiles": {key: list(value) for key, value in PROFILES.items()},
		"runs_as": agent_user(),
	}


# ── tokens ──────────────────────────────────────────────────────────────────
@frappe.whitelist(allow_guest=True, methods=["POST"])
def token():
	if not enabled():
		return _not_found()
	ip = _ip() or "unknown"
	try:
		if _count(f"oauth_token_fail:{ip}", 60) > TOKEN_FAILURES_PER_MINUTE:
			raise OAuthError("temporarily_unavailable", "too many failures; wait a minute", 429)
		answer = exchange(_args())
	except OAuthError as exc:
		if exc.status != 429:
			_count(f"oauth_token_fail:{ip}", 60)
		frappe.db.commit()
		return _oauth_error(exc)
	frappe.db.commit()
	return _json(answer)


def _token_row(value: str, kind: str):
	"""The row for a token of this kind INCLUDING used/revoked/expired ones, or None."""
	from .device_keys import TOKEN_DOCTYPE, hash_token

	if not value or not compat.doctype_exists(TOKEN_DOCTYPE):
		return None
	return frappe.db.get_value(
		TOKEN_DOCTYPE,
		{"token_hash": hash_token(value), "kind": kind},
		["name", "user", "client", "scopes", "family", "expires_at", "used_at", "revoked_at"],
		as_dict=True,
		for_update=True,
	)


def _client_row(name: str):
	from .device_keys import REQUEST_DOCTYPE

	return frappe.db.get_value(
		REQUEST_DOCTYPE,
		{"name": str(name or ""), "kind": KIND},
		["name", "status", "approved_at", "approved_by", "granted_scopes", "client_metadata", "display_name"],
		as_dict=True,
	)


def exchange(args: dict) -> dict:
	grant_type = str(args.get("grant_type") or "")
	if grant_type == "authorization_code":
		return _from_code(args)
	if grant_type == "refresh_token":
		return _from_refresh(args)
	raise OAuthError("unsupported_grant_type", "grant_type must be authorization_code or refresh_token")


def _from_code(args: dict) -> dict:
	from . import device_keys

	row = _token_row(str(args.get("code") or ""), "auth_code")
	if not row:
		raise OAuthError("invalid_grant", "unknown code")
	if row.get("used_at") or row.get("revoked_at"):
		# A code used twice: whoever holds the first set of tokens may not be the client.
		ended = device_keys.revoke_tokens(
			family=str(row.get("family") or ""), reason="authorization code reused"
		)
		_alert_reuse(row, ended, "authorization code")
		raise OAuthError("invalid_grant", "code already used")
	if str(row.get("expires_at") or "") <= _stamp():
		raise OAuthError("invalid_grant", "code expired")
	client = _client_row(row.get("client"))
	meta = _metadata(client or {})
	if not client or client["status"] != "Approved":
		raise OAuthError("invalid_grant", "this client is no longer approved")
	if str(args.get("client_id") or "") != meta.get("client_id"):
		raise OAuthError("invalid_grant", "code was issued to another client")
	if str(args.get("redirect_uri") or "") != meta.get("redirect_uri"):
		raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
	verifier = str(args.get("code_verifier") or "")
	if not 43 <= len(verifier) <= 128 or not hmac.compare_digest(
		device_keys.sha256_b64u(verifier.encode()), str(meta.get("code_challenge") or "")
	):
		raise OAuthError("invalid_grant", "PKCE verification failed")
	device_keys.spend(row)
	return _issue_pair(client, str(row.get("family") or ""))


def _from_refresh(args: dict) -> dict:
	from . import device_keys

	row = _token_row(str(args.get("refresh_token") or ""), "refresh")
	if not row:
		raise OAuthError("invalid_grant", "unknown refresh token")
	family = str(row.get("family") or "")
	if row.get("used_at"):
		ended = device_keys.revoke_tokens(family=family, reason="refresh token reused")
		_alert_reuse(row, ended, "refresh token")
		raise OAuthError("invalid_grant", "refresh token already used; this client must sign in again")
	if row.get("revoked_at") or str(row.get("expires_at") or "") <= _stamp():
		raise OAuthError("invalid_grant", "refresh token expired or revoked")
	client = _client_row(row.get("client"))
	meta = _metadata(client or {})
	if not client or client["status"] != "Approved":
		raise OAuthError("invalid_grant", "this client is no longer approved")
	if str(args.get("client_id") or "") != meta.get("client_id"):
		raise OAuthError("invalid_grant", "refresh token was issued to another client")
	if str(client.get("approved_at") or "") <= _stamp(-ABSOLUTE_DAYS * 86400):
		raise OAuthError(
			"invalid_grant", f"approval is older than {ABSOLUTE_DAYS} days; approve the client again"
		)
	device_keys.spend(row)
	return _issue_pair(client, family)


def _issue_pair(client: dict, family: str) -> dict:
	from . import device_keys

	scopes = str(client.get("granted_scopes") or "")
	sub = str(client.get("approved_by") or "")
	seconds = access_minutes() * 60
	access = device_keys.issue_token(
		"access", sub, seconds=seconds, client=client["name"], scopes=scopes, family=family
	)
	refresh_seconds = refresh_days() * 86400
	try:
		left = frappe.utils.time_diff_in_seconds(
			frappe.utils.add_to_date(client.get("approved_at"), days=ABSOLUTE_DAYS), frappe.utils.now()
		)
		refresh_seconds = max(60, min(refresh_seconds, int(left)))
	except Exception:  # pragma: no cover - an unparseable stamp keeps the idle limit
		pass
	refresh = device_keys.issue_token(
		"refresh", sub, seconds=refresh_seconds, client=client["name"], scopes=scopes, family=family
	)
	return {
		"access_token": access,
		"token_type": "Bearer",
		"expires_in": seconds,
		"refresh_token": refresh,
		"scope": scopes,
	}


def _alert_reuse(row: dict, ended: int, what: str) -> None:
	from . import audit, security_alerts

	text = (
		f"The {what} of MCP client {row.get('client')} was presented a second time. Every token of that "
		f"sign-in was ended ({ended}). If the client did not just crash and retry, treat its tokens as stolen."
	)
	try:
		audit.record(
			"oauth:token_reuse",
			{"client": row.get("client")},
			audit.STATUS_UNAUTHORIZED,
			text,
			caller_ip=_ip(),
		)
	except Exception:  # pragma: no cover
		pass
	security_alerts.send("ERPNext MCP security: a token was replayed", text)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def revoke():
	"""RFC 7009: always 200; a token we know ends with its whole sign-in."""
	if not enabled():
		return _not_found()
	revoke_presented(str(_args().get("token") or ""))
	frappe.db.commit()
	return _json({})


def revoke_presented(value: str) -> int:
	from . import device_keys

	for kind in ("access", "refresh"):
		row = _token_row(value, kind)
		if row and row.get("family"):
			return device_keys.revoke_tokens(family=str(row["family"]), reason="revoked by client")
	return 0


# ── the MCP call ────────────────────────────────────────────────────────────
def bearer_grant(value: str) -> dict | None:
	"""The grant a live access token carries, or None."""
	from . import device_keys

	row = device_keys.find_token(value, "access")
	if not row or not row.get("client"):
		return None
	client = _client_row(row["client"])
	if not client or client["status"] != "Approved":
		return None
	return {
		"client": client["name"],
		"client_name": client.get("display_name") or "",
		"scopes": str(row.get("scopes") or ""),
		"sub": str(row.get("user") or ""),
	}


def authenticate() -> None:
	"""`auth_hooks` entry. A live Bearer token on THE MCP ENDPOINT → the MCP OAuth user.

	Touches nothing else: any other path, any other scheme, OAuth off — it reads
	and returns. It never raises (it runs on every request to the site), never
	overrides an identity Frappe already set, and a token it does not know is
	left for Frappe to refuse.
	"""
	try:
		request = getattr(frappe.local, "request", None)
		if request is None or str(getattr(request, "path", "") or "").rstrip("/") != MCP_PATH:
			return
		if not enabled():
			return
		header = str(frappe.get_request_header("Authorization") or "")
		if header[:7].lower() != "bearer ":
			return
		user = getattr(getattr(frappe.local, "session", None), "user", "") or "Guest"
		if user not in ("Guest", ""):
			return
		grant = bearer_grant(header[7:].strip())
		if not grant:
			return
		form_dict = getattr(frappe.local, "form_dict", None)
		frappe.set_user(agent_user())
		if form_dict is not None:
			frappe.local.form_dict = form_dict
		setattr(frappe.local, _LOCAL_KEY, grant)
	except Exception:  # pragma: no cover - an auth hook must not take the site down
		return


def audit_label() -> str:
	"""`oauth <client> (<approver>)` for the audit row, or ""."""
	grant = current()
	if not grant:
		return ""
	return (
		f"oauth {grant['client']} {grant.get('client_name') or ''} (approved by {grant.get('sub') or '?'})"[
			:140
		]
	)


# ── the static token's retirement ───────────────────────────────────────────
def _default(key: str):
	try:
		return frappe.defaults.get_global_default(key)
	except Exception:
		return None


def _set_default(key: str, value: str) -> None:
	try:
		frappe.defaults.set_global_default(key, value)
	except Exception:
		pass


def note_static_use() -> None:
	"""One write a day at most: the date the static token was last used."""
	today = str(frappe.utils.now())[:10]
	if str(_default(STATIC_LAST_USED_KEY) or "") != today:
		_set_default(STATIC_LAST_USED_KEY, today)


def static_token_status() -> dict:
	"""`ready_to_disable_static_mcp_token`: OAuth on, a client approved, 14 quiet days on record."""
	from .device_keys import REQUEST_DOCTYPE

	tracked = str(_default(STATIC_TRACKED_SINCE_KEY) or "")
	if not tracked:
		tracked = str(frappe.utils.now())[:10]
		_set_default(STATIC_TRACKED_SINCE_KEY, tracked)
	last = str(_default(STATIC_LAST_USED_KEY) or "")
	cutoff = _stamp(-STATIC_QUIET_DAYS * 86400)[:10]
	clients = (
		len(
			frappe.db.get_all(
				REQUEST_DOCTYPE, filters={"kind": KIND, "status": "Approved"}, pluck="name", limit=50
			)
		)
		if compat.doctype_exists(REQUEST_DOCTYPE)
		else 0
	)
	reasons = []
	if not enabled():
		reasons.append("mcp_oauth_enabled is off")
	if not clients:
		reasons.append("no MCP client has been approved through OAuth yet")
	if tracked > cutoff:
		reasons.append(
			f"static-token use has been tracked only since {tracked}; {STATIC_QUIET_DAYS} days are needed"
		)
	if last and last > cutoff:
		reasons.append(f"the static X-MCP-Token was used on {last}")
	return {
		"mcp_oauth_enabled": enabled(),
		"legacy_static_mcp_token": legacy_static_allowed(),
		"approved_clients": clients,
		"static_token_last_used": last or None,
		"tracked_since": tracked,
		"ready_to_disable_static_mcp_token": not reasons,
		"reasons": reasons,
	}


# ── inventory and revoke ────────────────────────────────────────────────────
def clients(include_revoked: bool = False) -> list:
	from .device_keys import REQUEST_DOCTYPE, TOKEN_DOCTYPE

	if not compat.doctype_exists(REQUEST_DOCTYPE):
		return []
	statuses = ["Approved", "Revoked"] if include_revoked else ["Approved"]
	rows = frappe.db.get_all(
		REQUEST_DOCTYPE,
		filters={"kind": KIND, "status": ["in", statuses]},
		fields=[
			"name",
			"display_name",
			"status",
			"approved_by",
			"approved_at",
			"approved_via",
			"granted_scopes",
			"ip",
			"decided_reason",
		],
		limit=500,
	)
	out = []
	for row in rows:
		issued = frappe.db.get_all(
			TOKEN_DOCTYPE,
			filters={"client": row["name"], "kind": "access"},
			fields=["issued_at"],
			order_by="issued_at desc",
			limit=1,
		)
		out.append(
			{
				"kind": KIND,
				"name": row["name"],
				"client_name": row.get("display_name"),
				"status": row.get("status"),
				"scopes": str(row.get("granted_scopes") or "").split(),
				"approved_by": row.get("approved_by"),
				"approved_at": row.get("approved_at"),
				"approved_via": row.get("approved_via"),
				"requested_from": row.get("ip"),
				"last_token_at": issued[0]["issued_at"] if issued else None,
				"runs_as": agent_user(),
				"revoked_reason": row.get("decided_reason") if row.get("status") == "Revoked" else None,
			}
		)
	out.sort(key=lambda item: str(item.get("approved_at") or ""), reverse=True)
	return out


def revoke_client(name: str, by: str, reason: str = "") -> dict:
	"""End an approved client: status Revoked, every token gone, alert."""
	from . import device_enrollment, device_keys, security_alerts

	row = _client_row(name)
	if not row or row["status"] != "Approved":
		raise device_enrollment.EnrollmentRefused(
			f"{name or 'that'} is not an approved MCP client. Nothing was changed."
		)
	note = (reason or "revoked")[:140]
	frappe.db.set_value(
		device_keys.REQUEST_DOCTYPE,
		row["name"],
		{"status": "Revoked", "decided_reason": note},
		update_modified=False,
	)
	ended = device_keys.revoke_tokens(client=row["name"], reason=note)
	security_alerts.send(
		"ERPNext MCP: an AI client was revoked",
		f"{by or 'Somebody'} revoked the MCP client {row.get('display_name')!r} ({row['name']}); {ended} token(s) ended.",
	)
	return {"client": row["name"], "client_name": row.get("display_name"), "tokens_revoked": ended}


# ── the consent page ────────────────────────────────────────────────────────
_SCOPE_WORDS = {
	SCOPE_READ: "read every report and record the farm has switched on for AI",
	TOOL_PREFIX: "use the tool",
	WRITE_PREFIX: "make changes in",
}


def _scope_line(scope: str) -> str:
	from . import tool_groups

	if scope == SCOPE_READ:
		return _SCOPE_WORDS[SCOPE_READ]
	if scope.startswith(WRITE_PREFIX):
		domain = tool_groups.DOMAIN_BY_KEY.get(scope[len(WRITE_PREFIX) :])
		return f"make changes in {domain.label if domain else scope} (only tools the farm has switched on)"
	return f"use the tool {scope[len(TOOL_PREFIX) :]}"


def _consent_page(client_name: str, opened: dict):
	from .render import qr

	esc = html.escape
	code = opened["code"]
	image = ""
	if qr.available():
		import base64

		png = qr.render(f"farmops-approve:{code}", error="M")["png"]
		image = f'<img alt="QR code for {esc(code)}" src="data:image/png;base64,{base64.b64encode(png).decode()}">'
	nonce = secrets.token_urlsafe(16)
	scopes = "".join(f"<li>{esc(_scope_line(scope))}</li>" for scope in opened["scopes"])
	body = f"""
<p><b>{esc(client_name)}</b> is asking to work with this farm's records. It may:</p>
<ul>{scopes}</ul>
<p>A manager approves it. On a phone signed in to Farm Ops: <b>Phones → Approve</b>, then scan this
or type the code. Or a System Manager in the Desk: <b>ERPNext MCP Settings → Approve an access request</b>.</p>
<div class="code">{esc(code)}</div>
<div class="qr">{image}</div>
<p id="state">Waiting for approval. This page moves on by itself; the code is good for {REQUEST_MINUTES} minutes.</p>
<script nonce="{nonce}">
(function () {{
  var req = {json.dumps(opened["request"])}, poll = {json.dumps(opened["poll"])};
  var state = document.getElementById("state");
  function tick() {{
    fetch({json.dumps(STATUS_PATH)}, {{method: "POST", headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{request: req, poll: poll}})}})
      .then(function (r) {{ return r.json(); }})
      .then(function (a) {{
        if (a.status === "redirect") {{ state.textContent = "Approved. Returning you to the application…"; window.location.replace(a.redirect); return; }}
        if (a.status === "pending") {{ setTimeout(tick, 2000); return; }}
        state.textContent = "This request has ended. Start again from the application.";
      }})
      .catch(function () {{ setTimeout(tick, 4000); }});
  }}
  setTimeout(tick, 2000);
}})();
</script>"""
	return _page(f"Approve {client_name}", body, 200, nonce=nonce)


def _page(title: str, body: str, status: int, nonce: str = ""):
	esc = html.escape
	script = f" 'nonce-{nonce}'" if nonce else ""
	headers = {
		"Cache-Control": "no-store",
		"X-Frame-Options": "DENY",
		"Referrer-Policy": "no-referrer",
		"Content-Security-Policy": (
			f"default-src 'none'; script-src{script or ' ' + chr(39) + 'none' + chr(39)}; style-src 'unsafe-inline'; "
			"img-src data:; connect-src 'self'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'"
		),
	}
	page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)}</title>
<style>
body{{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:34rem;margin:2rem auto;padding:0 16px;color:#1d2b1f;background:#fbfaf6}}
h1{{font-size:1.4rem}}.code{{font:700 2.4rem ui-monospace,monospace;letter-spacing:.12em;text-align:center;margin:1.2rem 0}}
.qr{{text-align:center}}.qr img{{width:220px;height:220px;image-rendering:pixelated}}
@media (prefers-color-scheme: dark){{body{{background:#141a15;color:#e7efe8}}}}
</style></head><body><h1>{esc(title)}</h1>{body}</body></html>"""
	return _response(page, status, "text/html; charset=utf-8", headers)
