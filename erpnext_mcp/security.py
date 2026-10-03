# SPDX-License-Identifier: MIT
"""The transport gates: master switch, auth token, network allowlist.

Three checks run before any tool is looked up, in this order, and all three
must pass:

  1. MASTER SWITCH. `enabled` off → HTTP 404. Not 403: a disabled endpoint
     should be indistinguishable from an app that was never installed, so a
     scanner learns nothing from probing the path.

  2. TOKEN. Presented as `X-MCP-Token`, compared with `hmac.compare_digest` so
     the comparison takes the same time whatever the guess. No token configured
     → 404 as well; there is no state in which this endpoint answers without a
     secret.

  3. NETWORK ALLOWLIST. The caller's IP must fall inside one of the CIDRs in
     settings, OR the request must come from a browser page on this very site
     carrying a valid System Manager session (that is the operator using the
     Test Connection button on the settings form, which is a same-origin
     fetch). An empty allowlist denies everything rather than allowing
     everything — a blank field is much more likely a mistake than an intent to
     publish an accounting API to the internet.

WHY THE CLIENT NEVER LEARNS WHICH GATE IT FAILED. Every rejection is the same
opaque body. The *reason* is written to the MCP Action Log, where the operator
can read it and the caller cannot. Telling an unauthenticated caller "your IP
is fine, your token is wrong" hands them a free oracle for exactly the two
facts worth probing for.

TRUSTING X-Forwarded-For. Behind a reverse proxy `remote_addr` is the proxy, so
the real caller is only in the `X-Forwarded-For` chain. That header is
client-supplied, and bench's stock nginx *appends* to it rather than replacing
it (`$proxy_add_x_forwarded_for`), so its leftmost entry is whatever the client
felt like claiming. This module therefore gates on the RIGHTMOST entry — the
one the nearest proxy appended itself, which a client cannot forge — and falls
back to `remote_addr` when the header is absent. That is correct for the
one-reverse-proxy topology every stock Frappe deployment has. Behind two or
more proxy layers the rightmost entry is an inner proxy rather than the client,
and the CIDR gate stops being meaningful: on that topology rely on the auth
token plus a firewall rule, per docs/security.md.
"""

import hmac
import ipaddress

import frappe

from . import settings
from .errors import AuthError

#: What every rejection says, whichever gate failed.
_OPAQUE = "unauthorized"

#: Where `capture_calling_user` parks the answer. On `frappe.local`, which is a
#: per-request namespace the framework rebuilds for every request — so a value
#: left here cannot leak from one caller's request into the next one's, which a
#: module global emphatically could.
_CALLING_USER_KEY = "erpnext_mcp_calling_user"


def authorize() -> str:
	"""Run all three gates. Returns the caller IP; raises AuthError otherwise.

	The returned IP is what the audit log records, and is the IP the allowlist
	was evaluated against, so a log row and a gate decision can never disagree.
	"""
	ip = caller_ip()

	if not settings.is_enabled():
		raise AuthError("not found", http_status=404, log_reason="master switch (enabled) is off")

	configured = settings.auth_token()
	if not configured:
		raise AuthError("not found", http_status=404, log_reason="no auth_token configured")

	presented = presented_token()
	if not presented or not hmac.compare_digest(presented, configured):
		raise AuthError(
			_OPAQUE,
			http_status=401,
			log_reason="auth token missing or incorrect",
		)

	if not _network_allowed(ip):
		raise AuthError(
			_OPAQUE,
			http_status=403,
			log_reason=f"caller ip {ip or '<unknown>'} is outside allowed_cidrs",
		)

	return ip


def capture_calling_user() -> str:
	"""Remember WHO Frappe authenticated, before this app becomes somebody else.

	v0.17.0. THIS IS THE WHOLE BASIS OF PER-USER SCOPING ON THE MOBILE APP, and
	it has to happen in exactly one place at exactly one moment.

	The transport authorizes with a shared bearer token and then calls
	`frappe.set_user(settings.effective_user())`, so from that line onwards every
	tool runs as the MCP System User. That is right — it is what makes an
	AI-authored document traceable to a principal that is not a human — and it
	also means `frappe.session.user` inside a tool tells you nothing about which
	of forty field workers is holding the phone.

	But Frappe has ALREADY authenticated the request by the time this app sees
	it. A mobile client that sends `Authorization: token <api_key>:<api_secret>`
	alongside `X-MCP-Token` arrives here with `frappe.session.user` set to that
	worker, because Frappe's auth layer runs on every request whether or not the
	whitelisted method allows guests. So the worker's identity is present for
	exactly the window between "Frappe finished authenticating" and "this app set
	the user" — and this function is what saves it before it is overwritten.

	Returns "" for Guest, which is the ordinary case: an operator's Claude
	Desktop presents the MCP token and nothing else, and there is no human
	identity to scope by. `caller_identity()` reads it back.
	"""
	user = getattr(getattr(frappe.local, "session", None), "user", "") or ""
	user = "" if user in ("Guest", None) else str(user)
	try:
		setattr(frappe.local, _CALLING_USER_KEY, user)
	except Exception:  # pragma: no cover - a local that refuses attributes
		return ""
	return user


def caller_identity() -> str:
	"""The Frappe user who authenticated THIS request, or "" if nobody did.

	NOT `frappe.session.user`, which is the MCP System User by the time any tool
	runs. See `capture_calling_user`. Never raises: an identity this cannot
	establish is an identity that is absent, and every caller of this treats
	absent as "fall back to what the operator passed explicitly".
	"""
	return str(getattr(frappe.local, _CALLING_USER_KEY, "") or "")


def presented_token() -> str:
	"""The token the caller presented. `X-MCP-Token` first, `Bearer` accepted.

	`Authorization: Bearer <token>` is the MCP norm, and it is NOT the primary
	header here, for a reason found the hard way on a live v15 site: Frappe's own
	auth layer inspects `Authorization` before any whitelisted method runs, and
	routes a `Bearer` value into its OAuth2 validator. A token that is not an
	OAuth Bearer Token does not survive that trip intact on every version, so a
	perfectly correct MCP client can arrive here with nothing to present.

	`X-MCP-Token` is a header Frappe has no opinion about, so it reaches this
	function exactly as the client sent it. It is what the README, the settings
	form and the Claude Desktop example all use.

	`Authorization: Bearer` is still accepted second, because it costs nothing
	and it is what a client will try by default. On a site where Frappe leaves it
	alone, it works. Where it does not, the failure is a 401 rather than
	something subtler — see docs/security.md.
	"""
	token = (frappe.get_request_header("X-MCP-Token") or "").strip()
	if token:
		return token
	header = frappe.get_request_header("Authorization") or ""
	if header[:7].lower() == "bearer ":
		return header[7:].strip()
	return ""


def caller_ip() -> str:
	"""The caller's IP as the allowlist and the audit log should see it.

	Rightmost `X-Forwarded-For` hop when proxied, else the socket peer. See the
	module docstring for why rightmost and not `frappe.local.request_ip` (which
	is the leftmost, i.e. spoofable, entry). v0.217.0: with Trusted Proxy Ranges
	set, hops inside those ranges are skipped — see `client_ip`.
	"""
	forwarded = frappe.get_request_header("X-Forwarded-For") or ""
	request = getattr(frappe.local, "request", None)
	return client_ip(forwarded, getattr(request, "remote_addr", "") or "")


def client_ip(forwarded: str, peer: str, trusted=None) -> str:
	"""The client address from an X-Forwarded-For chain and the socket peer. v0.217.0.

	`trusted` empty (the default setting): the rightmost hop, else the peer —
	exactly the rule before v0.217.0. Set: walk from the right — the peer first,
	then each hop — skipping addresses inside a trusted proxy range; the first
	one outside is the client. A hop that does not parse ends the walk (it is
	somebody's claim, not a proxy's), and it is returned so it is seen. If every
	address is a proxy, the leftmost is returned.
	"""
	hops = [hop.strip() for hop in str(forwarded or "").split(",") if hop.strip()]
	if trusted is None:
		trusted = settings.trusted_proxy_cidrs()
	if not trusted:
		return hops[-1] if hops else str(peer or "")
	networks = []
	for entry in trusted:
		try:
			networks.append(ipaddress.ip_network(entry, strict=False))
		except ValueError:
			continue
	chain = [*hops, str(peer or "")] if peer else hops
	for address in reversed(chain):
		try:
			parsed = ipaddress.ip_address(address)
		except ValueError:
			return address
		if not any(parsed.version == net.version and parsed in net for net in networks):
			return address
	return chain[0] if chain else ""


def _network_allowed(ip: str) -> bool:
	if _is_same_origin_operator():
		return True
	cidrs = settings.allowed_cidrs()
	if not cidrs:
		return False
	try:
		address = ipaddress.ip_address(ip)
	except ValueError:
		# No parseable caller IP (a unit-test WSGI environ, an odd proxy). The
		# allowlist cannot be evaluated, so it cannot be satisfied.
		return False
	for entry in cidrs:
		try:
			network = ipaddress.ip_network(entry, strict=False)
		except ValueError:
			# A typo in the operator's list must not take out the whole gate;
			# skip the bad entry and keep checking the good ones.
			continue
		if address.version == network.version and address in network:
			return True
	return False


def _is_same_origin_operator() -> bool:
	"""True when this is an operator's own browser calling from this site.

	Both halves are required, and each closes the other's hole: the `Origin`
	check alone is worthless (any non-browser client can send any Origin), and
	the session check alone would let a logged-in user's browser be driven from
	a third-party page. Together they describe exactly one situation — a page
	served by this site, fetched by a System Manager who is signed in — which is
	the Test Connection button on the settings form.
	"""
	origin = frappe.get_request_header("Origin") or ""
	if not origin:
		return False
	request = getattr(frappe.local, "request", None)
	host = (getattr(request, "host", "") or "").lower()
	if not host or origin.split("//")[-1].strip("/").lower() != host:
		return False
	user = getattr(getattr(frappe.local, "session", None), "user", None) or "Guest"
	if user in ("Guest", None, ""):
		return False
	return "System Manager" in (frappe.get_roles(user) or [])
