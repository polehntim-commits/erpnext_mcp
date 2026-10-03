# SPDX-License-Identifier: MIT
"""Which address do emailed links carry? v0.223.3.

OML, 2026-10-03: a password-reset email linked to `http://frontend/update-password?…`.
Frappe builds every emailed link — password reset, the two-factor QR email,
notification and document links — with `frappe.utils.get_url()`. With the site
config's `host_name` unset that is the REQUEST's host, and behind the fafo-erpnext
image's nginx the request host is the SITE NAME (`frontend`), which no browser can
open. After the /farmops-only cutover the Desk lives only at the tailnet address
(`https://<host>.ts.net:8443`), so `host_name` must say exactly that.

THE FIX IS FRAPPE'S OWN SETTING, NOT THIS APP'S. `bench --site <site> set-config
host_name https://<host>.ts.net:8443` (docs/deploy/v0.223.3_emailed_links.md). It
does not move anything this app owns: PDFs fetch their assets from `pdf_base`'s
internal base (127.0.0.1:8080, independent of host_name), phones use Farm Ops
Public URL, and the MCP connection panel uses Public URL.

`status()` reads only — never a secret — and is the `email_links` block of
`get_server_status` and the `emailed_links` check of `get_security_status`.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

import frappe

from . import settings

LOOPBACK = ("127.0.0.1", "localhost", "::1", "0.0.0.0")
#: Ports that, on an Umbrel, are the Umbrel dashboard and not this site.
UMBREL_DASHBOARD_PORTS = (80, None)


def _conf() -> dict:
	return getattr(frappe.local, "conf", None) or {}


def _origin(url) -> str:
	parts = urlsplit(str(url or "").strip())
	if parts.scheme not in ("http", "https") or not parts.netloc:
		return ""
	return f"{parts.scheme}://{parts.netloc}".lower()


def site_name() -> str:
	return str(getattr(frappe.local, "site", "") or "")


def fix_command(target: str = "https://<host>.ts.net:8443") -> str:
	site = site_name() or "<site>"
	return (
		f"sudo docker exec -u frappe -w /home/frappe/frappe-bench fafo-erpnext_server_1 "
		f"bench --site {site} set-config host_name {target}"
	)


def expected() -> str:
	"""The Desk address an operator has told this app: Public URL, if it is a plain origin."""
	public = settings.public_url()
	parts = urlsplit(public)
	return _origin(public) if public and not parts.path.strip("/") else ""


def status() -> dict:
	"""What emailed links say, and whether a person can open them. Never raises."""
	try:
		conf = _conf()
		host_name = str(conf.get("host_name") or conf.get("hostname") or "").strip()
		site = site_name()
		try:
			link = str(frappe.utils.get_url("/update-password?key=…") or "")
		except Exception:  # pragma: no cover
			link = ""
		want = expected()
		problems, warnings = [], []
		parts = urlsplit(host_name if "://" in host_name else f"http://{host_name}")
		host = (parts.hostname or "").lower()

		if not host_name:
			problems.append(
				"host_name is not set, so emailed links (password reset, 2FA QR, notifications) use the "
				f"request's host — behind this image's nginx that is the site name, e.g. http://{site or 'frontend'}/…"
			)
		elif host == site.lower() or _origin(host_name) in (
			f"http://{site}".lower(),
			f"https://{site}".lower(),
		):
			problems.append(f"host_name is the site name ({host_name}); no browser can open http://{site}/…")
		elif host in LOOPBACK:
			problems.append(
				f"host_name is a loopback address ({host_name}); only the server itself can open it"
			)
		else:
			if "://" not in host_name:
				warnings.append(f"host_name has no scheme ({host_name}); Frappe will assume http://")
			if parts.path.strip("/"):
				warnings.append(
					f"host_name carries a path ({parts.path}); Frappe joins links onto the origin"
				)
			try:
				is_ip = bool(ipaddress.ip_address(host))
			except ValueError:
				is_ip = False
			if is_ip and parts.port in UMBREL_DASHBOARD_PORTS:
				warnings.append(
					f"host_name is {host_name} with no port: on an Umbrel that is the Umbrel dashboard, not ERPNext"
				)
			farmops = _origin(settings.farmops_public_url())
			if farmops and _origin(host_name) == farmops:
				warnings.append(
					f"host_name is the Funnel address ({farmops}), which serves only /farmops after the cutover"
				)
			if want and _origin(host_name) != want:
				warnings.append(f"host_name ({_origin(host_name)}) is not Public URL ({want})")

		target = want or "https://<host>.ts.net:8443"
		ok = not problems and not warnings
		return {
			"host_name": host_name or None,
			"site": site or None,
			"reset_link_starts": link.split("/update-password", 1)[0] or None,
			"public_url": want or None,
			"ok": ok,
			"problems": problems,
			"warnings": warnings,
			"fix": None if ok else fix_command(target),
			"note": (
				"Frappe's own site setting. Changing it does not move PDFs (pdf_base's internal base), phones (Farm "
				"Ops Public URL) or the MCP (Public URL)."
			),
		}
	except Exception as exc:  # pragma: no cover - a status block never fails its call
		return {"error": f"{type(exc).__name__}: {exc}"}
