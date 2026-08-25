# SPDX-License-Identifier: MIT
"""Tailscale Funnel: is the endpoint actually public, and what is it serving?

v0.17.0. The mobile app talks to this site over public HTTPS, and the transport
Tim chose is Tailscale Funnel — a tailnet node publishing one port to the whole
internet under `https://<host>.<tailnet>.ts.net`, with TLS terminated by
Tailscale's own certificate.

TWO READ TOOLS AND NO WRITE TOOL, AND THAT IS THE DESIGN.

`validate_public_endpoint` asks the question from OUTSIDE: it opens a TLS
connection to the public name, reads the certificate, POSTs to the MCP path and
reports what came back. `get_tailscale_funnel_config` asks it from INSIDE: what
does this machine think it is serving, on which ports, at which URLs.

**Nothing here can turn Funnel on or off, and nothing here ever will.** That is
not an omission to be filled in later:

  1. Flipping Funnel changes what is reachable from the entire internet. It is
     an operator decision, made deliberately, by somebody who can see the
     consequences — not something an AI does because a tool existed and the
     conversation drifted that way.
  2. It is not even this process's decision to make. `tailscale funnel` talks to
     `tailscaled` over a local socket that a containerised Frappe worker does
     not have, and running it needs privileges the bench user does not hold. A
     tool that shipped would fail on the target deployment while looking like it
     might work somewhere.

So this module reports, and an operator acts. `README.md` has the four commands.

────────────────────────────────────────────────────────────────────────────
THE SECURITY POSITION, STATED PLAINLY BECAUSE IT CHANGES WITH THIS RELEASE
────────────────────────────────────────────────────────────────────────────

Before v0.17.0 the honest description of this endpoint was "reachable from the
LAN, gated by a bearer token". After Funnel it is:

  **EVERYTHING THE API EXPOSES IS PUBLIC.** The hostname is in Tailscale's
  public DNS and appears in certificate transparency logs, so it is
  DISCOVERABLE — not secret, not obscure, findable by anybody who looks. The
  auth token is the only thing between the internet and this site's ledger.

Which makes three settings load-bearing that were previously belt-and-braces:

  * `allowed_cidrs` — Funnel arrives at nginx from Tailscale's own forwarder, so
    the caller IP the app sees is NOT the phone's. Getting this wrong either
    locks out the mobile app or opens the CIDR gate to everything that reaches
    the box. README says exactly what to put there and why.
  * `auth_token` — now the whole boundary. It should be long, it should be
    rotated when a phone is lost, and it should never be the same string as
    anything else on the site.
  * `enabled` — the one control that takes the endpoint off the internet in one
    tick, with no restart and no deploy.

`validate_public_endpoint` deliberately probes UNAUTHENTICATED by default. A 401
from outside is the best possible result: it proves the path is reachable, the
certificate is valid, and the token gate is doing its job. Sending the real
token out and back proves more, so `authenticate=true` exists — and it refuses
any URL that is not the operator's own configured `public_url`, because a tool
that will POST your bearer token to a hostname in its arguments is a tool that
exfiltrates it.

THE URL ALLOWLIST IS THE OTHER HALF OF THAT. This tool makes an outbound request
from inside the site's network, which is the shape of every server-side request
forgery there has ever been. So the only reachable targets are the configured
`public_url` and hosts under `.ts.net`, over HTTPS, on the default port. Not a
redirect-follow, not an IP literal, not a `file://`. There is a test for each.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import frappe

from .. import settings
from ..args import as_bool, as_int, as_str
from ..errors import ToolError
from ..result import ToolResult

#: The MCP path, which is the only path this tool ever asks for.
MCP_PATH = "/api/method/erpnext_mcp.mcp.handle"

#: Hosts an outbound probe may reach, beyond the operator's own configured
#: public_url. `.ts.net` is Tailscale's own zone and nothing else is on it.
ALLOWED_SUFFIXES = (".ts.net",)

#: How long a probe waits. Short: this answers "is it up", and a tool that hangs
#: for thirty seconds inside a request is one an operator kills rather than reads.
TIMEOUT_SECONDS = 8

#: Where `tailscale` usually is, when it is not on PATH. Checked in order.
_BINARY_CANDIDATES = (
	"/usr/bin/tailscale",
	"/usr/local/bin/tailscale",
	"/opt/homebrew/bin/tailscale",
	"/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)

#: The local socket `tailscale` talks to `tailscaled` through. Its presence is
#: what distinguishes "Tailscale is not installed" from "Tailscale is on the
#: host but this container cannot see it", which are different problems with
#: different fixes and are otherwise indistinguishable from in here.
_SOCKET_CANDIDATES = (
	"/var/run/tailscale/tailscaled.sock",
	"/run/tailscale/tailscaled.sock",
	"/Library/Tailscale/tailscaled.sock",
)


# ── 1. validate_public_endpoint ─────────────────────────────────────────────
def validate_public_endpoint(args: dict) -> ToolResult:
	"""Reach this site from outside over HTTPS and report what came back."""
	url = _target_url(args)
	parsed = urllib.parse.urlsplit(url)
	authenticate = as_bool(args, "authenticate", False)
	configured = (settings.public_url() or "").rstrip("/")

	if authenticate and url.rstrip("/") != configured:
		raise ToolError(
			"authenticate=true will only send this site's bearer token to the URL configured in "
			f"ERPNext MCP Settings ({configured or 'which is empty'}). It was asked to send it "
			f"to {url!r}. A tool that POSTs your token to a hostname in its arguments is a tool "
			"that exfiltrates it. Nothing was sent."
		)
	if authenticate and not settings.auth_token():
		raise ToolError(
			"authenticate=true, but this site has no auth_token configured — there is nothing to "
			"present. Generate one on ERPNext MCP Settings first."
		)

	timeout = min(30, max(1, as_int(args, "timeout_seconds", TIMEOUT_SECONDS)))
	data = {
		"url": url,
		"endpoint": f"{url}{MCP_PATH}",
		"host": parsed.hostname,
		"port": parsed.port or 443,
		"configured_public_url": configured or None,
		"is_configured_url": url.rstrip("/") == configured,
		"looks_like_tailscale_funnel": bool(parsed.hostname and parsed.hostname.endswith(".ts.net")),
		"authenticated_probe": authenticate,
		"timeout_seconds": timeout,
	}

	data["tls"] = _tls_report(parsed.hostname, parsed.port or 443, timeout)
	data["http"] = _http_report(data["endpoint"], authenticate, timeout)
	data.update(_verdict(data))

	if as_bool(args, "probe_routes", False):
		data["routes"] = _route_report(url, timeout)
		if data["routes"]["unpublished"]:
			data["summary"] = (
				f"{data['summary']} — and {len(data['routes']['unpublished'])} of "
				f"{data['routes']['checked']} phone routes are NOT published"
			)

	return ToolResult(data=data, summary=data["summary"])


def _target_url(args: dict) -> str:
	"""The URL to probe, allowlisted. See the module docstring on why this is strict."""
	explicit = as_str(args, "url").strip()
	url = (explicit or settings.public_url() or "").strip().rstrip("/")
	if not url:
		raise ToolError(
			"this site has no public_url configured and no `url` was passed, so there is nothing "
			"to probe. Fill in `public_url` on ERPNext MCP Settings with the Tailscale Funnel "
			"address — https://<host>.<tailnet>.ts.net — or pass `url`. "
			"get_tailscale_funnel_config reports what this machine is serving."
		)

	parsed = urllib.parse.urlsplit(url)
	if parsed.scheme != "https":
		raise ToolError(
			f"{url!r} is not an https:// URL. This tool exists to prove a PUBLIC endpoint is "
			"serving valid TLS; probing anything else would answer a different question and "
			"would put a bearer token on a plaintext wire if authenticate were ever set."
		)
	host = (parsed.hostname or "").lower()
	if not host:
		raise ToolError(f"{url!r} has no hostname.")
	if parsed.path.strip("/") or parsed.query or parsed.fragment:
		raise ToolError(
			f"{url!r} carries a path or query. Pass the BASE url — this tool appends "
			f"{MCP_PATH} itself, and a tool that will fetch an arbitrary path from inside the "
			"site's network is a request-forgery primitive."
		)

	configured = (settings.public_url() or "").strip().rstrip("/").lower()
	if url.lower() == configured:
		return url
	if any(host.endswith(suffix) for suffix in ALLOWED_SUFFIXES):
		return url
	raise ToolError(
		f"{url!r} is neither this site's configured public_url ({configured or 'unset'}) nor a "
		f"host under {', '.join(ALLOWED_SUFFIXES)}. This tool makes an outbound request from "
		"inside the site's network, so the set of things it may reach is a short allowlist and "
		"not an argument. Nothing was sent."
	)


# ── is every route a phone calls actually PUBLISHED? ────────────────────────
def _route_report(url: str, timeout: int) -> dict:
	"""Probe every `farmops_api` route from outside and say which ones answer.

	v0.57.1. THE CHECK THAT WAS MISSING, and the reason it is missing is that
	the two halves of "can a phone reach this" live in different places and only
	one of them is in this repository. `routes.py` is the route table and the
	test suite asserts it exhaustively; the funnel is a list of
	`tailscale funnel --set-path` mounts on the Umbrel, one line per method, and
	a route added to the table is NOT published by having been added.

	Three releases proved what that costs. v0.54.0, v0.55.0 and v0.57.0 each
	added routes, none was mounted, and six methods — the housing pair, the
	onboarding dropdowns, `collect_signature` and `submit_form_signature` —
	answered Tailscale's own plain-text `404 page not found` to every handset on
	the farm. NOTHING ON THE SERVER COULD SEE IT: the request stops at the
	proxy, so there is no access log line, no audit row and no traceback. The
	first report was a foreman with a signature on the glass being told the task
	had been taken by somebody else.

	SO IT IS ASKED FROM OUTSIDE, unauthenticated, exactly as the MCP probe above
	is. A 401 is the RIGHT answer and the one this looks for: it proves the path
	reached gunicorn and the credential gate refused an anonymous caller.
	Anything else — a plain-text 404 from the proxy, an HTML login page from
	nginx, a timeout — means the path is not carrying, and the method behind it
	is unreachable no matter how correct it is.

	NOTHING IS SENT BUT AN EMPTY BODY. Every route is POST-only and each one
	refuses an unauthenticated caller before reading an argument, so `{}` is
	enough to learn whether the path carries and cannot cause a write. The
	mutating routes are probed on the same footing for that reason.
	"""
	from ..farmops_api import PREFIX, ROUTES

	base = url.rstrip("/")
	published, unpublished = [], []
	for route in ROUTES:
		outcome = _probe_route(f"{base}{PREFIX}{route.path}", timeout)
		(published if outcome["published"] else unpublished).append(outcome)

	report = {
		"checked": len(ROUTES),
		"published": [row["path"] for row in published],
		"unpublished": unpublished,
	}
	if unpublished:
		report["diagnosis"] = (
			f"{len(unpublished)} of {len(ROUTES)} routes did not reach the service. A phone "
			f"calling one of them gets a 404 body the app cannot read, so it shows its generic "
			f"miss — 'That task no longer exists' — for a method that is working perfectly on "
			f"the other side of the proxy. Mount them: run scripts/mount_farmops_funnel.sh on "
			f"the Umbrel host, then run this again."
		)
	else:
		report["diagnosis"] = "every route a phone calls reaches the service."
	return report


def _probe_route(endpoint: str, timeout: int) -> dict:
	"""One route, from outside. `published` is true only for the service's own 401.

	THE SERVER HEADER IS THE TELL AND THE STATUS IS NOT. Tailscale answers an
	unmounted path with 404 and so does `farmops_api` for a path that is mounted
	and not in the route table — the same code, two completely different
	problems. gunicorn names itself and the proxy does not, and the body settles
	it either way: this service answers JSON on every single exit, including
	both of its 404s.
	"""
	row = {"path": endpoint.split("/farmops/api", 1)[-1]}
	request = urllib.request.Request(
		endpoint,
		data=b"{}",
		headers={"Content-Type": "application/json", "Accept": "application/json"},
		method="POST",
	)
	try:
		opener = urllib.request.build_opener(_NoRedirect)
		with opener.open(request, timeout=timeout) as response:
			status, headers, payload = response.status, response.headers, response.read(4096)
	except urllib.error.HTTPError as exc:
		status, headers = exc.code, exc.headers
		try:
			payload = exc.read(4096)
		except Exception:  # pragma: no cover
			payload = b""
	except Exception as exc:
		row.update({"published": False, "error": f"{type(exc).__name__}: {exc}"})
		return row

	content_type = (headers.get("Content-Type") if headers else "") or ""
	row.update(
		{
			"status": status,
			"content_type": content_type or None,
			"server": (headers.get("Server") if headers else None),
		}
	)
	# 401 AND JSON. A 200 here would mean the credential gate is not running,
	# which is a finding of its own and is not "published" in any sense worth
	# reporting as healthy.
	row["published"] = status == 401 and "json" in content_type.lower()
	if not row["published"]:
		row["body"] = payload[:200].decode("utf-8", "replace")
		row["note"] = (
			"not mounted on the funnel — this is the proxy answering, not the service"
			if "json" not in content_type.lower()
			else f"reached something that answered {status} rather than the service's 401"
		)
	return row


def _tls_report(host: str, port: int, timeout: int) -> dict:
	"""Open one TLS connection and read the certificate. Never raises."""
	report = {"reachable": False, "verified": False}
	started = time.monotonic()
	try:
		context = ssl.create_default_context()
		with socket.create_connection((host, port), timeout=timeout) as raw:
			with context.wrap_socket(raw, server_hostname=host) as tls:
				cert = tls.getpeercert() or {}
				report.update(
					{
						"reachable": True,
						"verified": True,
						"protocol": tls.version(),
						"cipher": (tls.cipher() or [None])[0],
						"subject": _name_from(cert.get("subject")),
						"issuer": _name_from(cert.get("issuer")),
						"not_before": cert.get("notBefore"),
						"not_after": cert.get("notAfter"),
						"subject_alt_names": [
							value for kind, value in (cert.get("subjectAltName") or ()) if kind == "DNS"
						],
					}
				)
				report["days_until_expiry"] = _days_until(cert.get("notAfter"))
	except ssl.SSLCertVerificationError as exc:
		report.update(
			{
				"reachable": True,
				"verified": False,
				"error": f"certificate verification failed: {exc}",
				"diagnosis": (
					"the host answered TLS but its certificate did not verify. A working Funnel "
					"serves a Let's Encrypt certificate Tailscale obtains itself, so this is "
					"usually a self-signed certificate from something else answering on 443 — "
					"not Funnel."
				),
			}
		)
	except TimeoutError:
		report["error"] = f"timed out after {timeout}s"
		report["diagnosis"] = (
			"nothing answered. Either Funnel is not enabled for this node, or this container "
			"has no outbound route to the internet — both look identical from here. "
			"get_tailscale_funnel_config says which."
		)
	except OSError as exc:
		report["error"] = f"{type(exc).__name__}: {exc}"
		report["diagnosis"] = (
			"the name did not resolve or the connection was refused. A Funnel hostname is in "
			"public DNS as soon as Funnel is on, so a name that does not resolve usually means "
			"it is not on."
		)
	except Exception as exc:  # pragma: no cover - anything else is worth reporting, not raising
		report["error"] = f"{type(exc).__name__}: {exc}"
	report["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
	return report


def _http_report(endpoint: str, authenticate: bool, timeout: int) -> dict:
	"""POST one MCP `tools/list` and report the status. Never raises.

	Deliberately a REAL MCP message rather than a HEAD or an empty body: a
	reverse proxy that answers 200 for anything would pass a shallower probe,
	and the question here is whether *this app* is on the other end.
	"""
	body = json.dumps({"jsonrpc": "2.0", "id": "probe", "method": "tools/list"}).encode("utf-8")
	headers = {"Content-Type": "application/json", "Accept": "application/json"}
	if authenticate:
		headers["X-MCP-Token"] = settings.auth_token()

	report = {"authenticated": authenticate}
	started = time.monotonic()
	request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
	try:
		# No redirect following: a 30x from a public endpoint is a finding, not a
		# step. urllib follows by default, so this uses an opener that does not.
		opener = urllib.request.build_opener(_NoRedirect)
		with opener.open(request, timeout=timeout) as response:
			payload = response.read(65536)
			report.update(
				{
					"status": response.status,
					"content_type": response.headers.get("Content-Type"),
					"body_bytes": len(payload),
					"server": response.headers.get("Server"),
				}
			)
			report.update(_read_rpc(payload))
	except urllib.error.HTTPError as exc:
		payload = b""
		try:
			payload = exc.read(65536)
		except Exception:  # pragma: no cover
			pass
		report.update(
			{
				"status": exc.code,
				"content_type": exc.headers.get("Content-Type") if exc.headers else None,
				"body_bytes": len(payload),
				"server": exc.headers.get("Server") if exc.headers else None,
			}
		)
		report.update(_read_rpc(payload))
	except TimeoutError:
		report["error"] = f"timed out after {timeout}s"
	except Exception as exc:
		report["error"] = f"{type(exc).__name__}: {exc}"
	report["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
	return report


class _NoRedirect(urllib.request.HTTPRedirectHandler):
	def redirect_request(self, req, fp, code, msg, headers, newurl):
		return None


def _read_rpc(payload: bytes) -> dict:
	"""Did a JSON-RPC body come back, and does it look like this app?"""
	try:
		parsed = json.loads(payload.decode("utf-8", "replace") or "null")
	except Exception:
		return {"json_rpc": False}
	if not isinstance(parsed, dict):
		return {"json_rpc": False}
	out = {"json_rpc": parsed.get("jsonrpc") == "2.0"}
	if isinstance(parsed.get("result"), dict) and isinstance(parsed["result"].get("tools"), list):
		out["tools_advertised"] = len(parsed["result"]["tools"])
	if isinstance(parsed.get("error"), dict):
		out["rpc_error"] = parsed["error"].get("message")
	return out


def _verdict(data: dict) -> dict:
	"""Turn the two reports into one sentence and one boolean."""
	tls = data.get("tls") or {}
	http = data.get("http") or {}
	status = http.get("status")

	if not tls.get("reachable"):
		return {
			"working": False,
			"summary": f"{data['host']} did not answer — {tls.get('error', 'unreachable')}",
			"next_step": (
				"Run get_tailscale_funnel_config to see whether this machine thinks it is "
				"serving anything, then `tailscale funnel status` on the host. README has the "
				"setup under 'Exposing ERPNext MCP publicly via Tailscale Funnel'."
			),
		}
	if not tls.get("verified"):
		return {
			"working": False,
			"summary": f"{data['host']} answered TLS but its certificate did not verify",
			"next_step": tls.get("diagnosis") or "Check what is actually listening on 443.",
		}
	if status is None:
		return {
			"working": False,
			"summary": f"TLS to {data['host']} is valid but the MCP path did not answer — {http.get('error')}",
			"next_step": (
				"The certificate is Tailscale's, so Funnel is up and something else is wrong "
				"between it and Frappe. Check that Funnel forwards to the port nginx serves the "
				"site on."
			),
		}
	if status in (401, 403) and not http.get("authenticated"):
		return {
			"working": True,
			"summary": (
				f"reachable: valid TLS, and {MCP_PATH} answered {status} to an unauthenticated "
				"probe — which is exactly right. The Funnel works and the token gate is holding."
			),
			"next_step": (
				"Nothing. If you want the whole round trip proved, re-run with "
				"authenticate=true and this site's own public_url."
			),
		}
	if status == 404:
		return {
			"working": False,
			"summary": f"{MCP_PATH} answered 404",
			"next_step": (
				"A 404 here means one of three things, and the app returns it for the first two "
				"deliberately: the master `enabled` switch is off, no auth_token is configured, "
				"or the app is not installed on the site this hostname reaches. Check ERPNext "
				"MCP Settings first."
			),
		}
	if status == 200 and http.get("json_rpc"):
		return {
			"working": True,
			"summary": (
				f"reachable and answering: valid TLS, {MCP_PATH} returned 200 with "
				f"{http.get('tools_advertised', 'some')} tool(s) advertised"
			),
			"next_step": (
				"THE ENDPOINT IS PUBLIC AND IT ANSWERED A REAL MCP CALL. Everything the enabled "
				"tools expose is now reachable from the internet by anybody holding the token. "
				"Check the enabled mutating tools are the ones you meant."
			),
		}
	return {
		"working": bool(status and status < 500),
		"summary": f"TLS is valid; {MCP_PATH} answered {status}",
		"next_step": "Read the http block — the status is not one this tool has a rule for.",
	}


def _name_from(pairs) -> str:
	"""Flatten a certificate's subject/issuer tuple-of-tuples into a string."""
	out = []
	for rdn in pairs or ():
		for key, value in rdn:
			out.append(f"{key}={value}")
	return ", ".join(out)


def _days_until(not_after) -> int | None:
	if not not_after:
		return None
	try:
		expiry = ssl.cert_time_to_seconds(not_after)
	except Exception:  # pragma: no cover
		return None
	return int((expiry - time.time()) // 86400)


# ── 2. get_tailscale_funnel_config ──────────────────────────────────────────
def get_tailscale_funnel_config(args: dict) -> ToolResult:
	"""What this machine is serving, from the inside: ports, URLs, and whether it can tell."""
	binary = _tailscale_binary()
	socket_path = _tailscale_socket()
	configured = (settings.public_url() or "").strip().rstrip("/")

	data = {
		"tailscale_binary": binary or None,
		"tailscaled_socket": socket_path or None,
		"configured_public_url": configured or None,
		"site_url": _site_url(),
		"can_read_config": bool(binary),
		"funnel_ports": [],
		"funnel_urls": [],
		"serve_ports": [],
	}

	if not binary:
		data["diagnosis"] = _no_binary_diagnosis(socket_path)
		data["next_step"] = (
			"Read the Funnel config on the HOST rather than from in here: `tailscale funnel "
			"status` and `tailscale serve status --json`. Then put the resulting "
			"https://<host>.<tailnet>.ts.net into `public_url` on ERPNext MCP Settings, and run "
			"validate_public_endpoint — which needs no local Tailscale at all, because it asks "
			"from outside."
		)
		return ToolResult(data=data, summary="no local tailscale CLI — reporting what can be seen without it")

	timeout = min(20, max(1, as_int(args, "timeout_seconds", TIMEOUT_SECONDS)))
	status = _run(binary, ["serve", "status", "--json"], timeout)
	data["serve_status"] = status

	parsed = None
	if status.get("ok") and status.get("stdout"):
		try:
			parsed = json.loads(status["stdout"])
		except Exception:
			data["parse_error"] = "tailscale serve status --json did not return JSON"

	if isinstance(parsed, dict):
		data.update(_read_serve_config(parsed))

	node = _run(binary, ["status", "--json"], timeout)
	if node.get("ok") and node.get("stdout"):
		try:
			payload = json.loads(node["stdout"])
			self_node = (payload.get("Self") or {}) if isinstance(payload, dict) else {}
			data["node"] = {
				"dns_name": str(self_node.get("DNSName") or "").rstrip("."),
				"hostname": self_node.get("HostName"),
				"online": self_node.get("Online"),
				"tailscale_ips": self_node.get("TailscaleIPs") or [],
				"backend_state": payload.get("BackendState"),
			}
			if data["node"]["dns_name"] and not data["funnel_urls"]:
				data["expected_funnel_url"] = f"https://{data['node']['dns_name']}"
		except Exception:  # pragma: no cover
			pass

	data["public_url_matches"] = bool(
		configured and (configured in data["funnel_urls"] or configured == data.get("expected_funnel_url"))
	)
	data["mutation_note"] = (
		"NOTHING IN THIS APP CAN TURN FUNNEL ON OR OFF, and nothing will. Changing what is "
		"reachable from the entire internet is an operator decision made deliberately, and "
		"`tailscale funnel` needs a local socket and privileges a containerised Frappe worker "
		"does not have — a tool that shipped would fail here while looking like it might work. "
		"The commands are in the README."
	)
	if data["funnel_ports"]:
		data["exposure_note"] = (
			f"This node publishes {', '.join(str(port) for port in data['funnel_ports'])} to the "
			"PUBLIC INTERNET. Everything served there is discoverable: the hostname is in "
			"Tailscale's public DNS and in certificate transparency logs. The MCP auth token is "
			"the whole boundary."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{len(data['funnel_ports'])} funnel port(s), {len(data['serve_ports'])} serve port(s)"
			+ (f"; node {data.get('node', {}).get('dns_name')}" if data.get("node") else "")
		),
	)


def _read_serve_config(parsed: dict) -> dict:
	"""Pull ports and URLs out of `tailscale serve status --json`.

	The shape has moved between Tailscale versions — `AllowFunnel` keyed by
	`host:port`, `TCP` keyed by port, `Web` keyed by host — so this reads
	defensively and reports what it found rather than asserting a schema. A
	config it cannot parse is reported as unparsed, not as empty: "no funnel
	ports" and "I could not tell" are different answers.
	"""
	out = {"funnel_ports": [], "funnel_urls": [], "serve_ports": [], "web_handlers": []}

	for key, enabled in (parsed.get("AllowFunnel") or {}).items():
		if not enabled:
			continue
		host, _, port = str(key).rpartition(":")
		if port.isdigit():
			out["funnel_ports"].append(int(port))
		if host:
			out["funnel_urls"].append(f"https://{host.strip('.')}" + ("" if port == "443" else f":{port}"))

	for port in parsed.get("TCP") or {}:
		if str(port).isdigit():
			out["serve_ports"].append(int(port))

	for host, handlers in (parsed.get("Web") or {}).items():
		for path, handler in (handlers.get("Handlers") or {}).items():
			out["web_handlers"].append(
				{
					"host": host,
					"path": path,
					"proxy": handler.get("Proxy"),
					"text": handler.get("Text"),
				}
			)
		host_port = str(host).rpartition(":")[2]
		if host_port.isdigit() and int(host_port) not in out["serve_ports"]:
			out["serve_ports"].append(int(host_port))

	out["funnel_ports"] = sorted(set(out["funnel_ports"]))
	out["serve_ports"] = sorted(set(out["serve_ports"]))
	out["funnel_urls"] = sorted(set(out["funnel_urls"]))
	return out


def _tailscale_binary() -> str:
	found = shutil.which("tailscale")
	if found:
		return found
	for candidate in _BINARY_CANDIDATES:
		if os.path.exists(candidate) and os.access(candidate, os.X_OK):
			return candidate
	return ""


def _tailscale_socket() -> str:
	for candidate in _SOCKET_CANDIDATES:
		if os.path.exists(candidate):
			return candidate
	return ""


def _no_binary_diagnosis(socket_path: str) -> str:
	if socket_path:
		return (
			f"there is a tailscaled socket at {socket_path} but no `tailscale` CLI on this "
			"machine's PATH. The daemon is reachable and the client is not installed."
		)
	return (
		"no `tailscale` CLI and no tailscaled socket are visible from this process. That is the "
		"EXPECTED state for the target deployment: Frappe runs in a container, Tailscale runs on "
		"the Umbrel host, and the container has neither the binary nor the host's socket. It is "
		"not a fault and nothing needs fixing in here — Funnel works by forwarding to the port "
		"nginx already serves, which needs no cooperation from this process at all."
	)


def _run(binary: str, argv: list, timeout: int) -> dict:
	"""Run one fixed tailscale subcommand. Never raises, never uses a shell.

	The argument vector is built from constants in this module and never from a
	tool argument, so there is nothing for a caller to inject into. `shell=False`
	is the default and is left the default deliberately.
	"""
	command = [binary, *argv]
	try:
		completed = subprocess.run(
			command,
			capture_output=True,
			text=True,
			timeout=timeout,
			check=False,
		)
	except subprocess.TimeoutExpired:
		return {"ok": False, "command": " ".join(argv), "error": f"timed out after {timeout}s"}
	except Exception as exc:
		return {"ok": False, "command": " ".join(argv), "error": f"{type(exc).__name__}: {exc}"}
	return {
		"ok": completed.returncode == 0,
		"command": " ".join(argv),
		"returncode": completed.returncode,
		"stdout": (completed.stdout or "").strip(),
		"stderr": (completed.stderr or "").strip()[:2000],
	}


def _site_url() -> str:
	try:
		return str(frappe.utils.get_url() or "")
	except Exception:  # pragma: no cover
		return ""
