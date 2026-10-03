# SPDX-License-Identifier: MIT
"""Where wkhtmltopdf fetches a print's stylesheets and images from. v0.216.1.

────────────────────────────────────────────────────────────────────────────
THE BUG
────────────────────────────────────────────────────────────────────────────

Email Queue rows on OML failed with

    wkhtmltopdf exited with non-zero code 1. error:
    Exit with code 1 due to network error: HostNotFoundError

whenever a print was attached. `frappe.utils.pdf.get_pdf` (v15) starts with
`scrub_urls(html)`, which turns every relative `src`/`href`/`url()` in the
print — `/assets/.../print.bundle.css`, `/files/logo.png`, the letterhead — into
an absolute URL on `frappe.utils.get_url()`: the site config's `host_name` when
it is set, else the request's own host. That is the address PEOPLE use
(`http://100.69.162.122`, a Tailscale name, the Funnel address with its
`/erpnext` path). wkhtmltopdf runs INSIDE the ERPNext container and fetches
those URLs itself, from there — where that name does not resolve, that port is
not this nginx, or that path prefix does not exist. A failed stylesheet or a
failed host lookup is not one of the four errors `get_pdf` tolerates
(`PDF_CONTENT_ERRORS`), so the whole PDF fails: an Email Queue error, a blank
or failed Print → PDF in the Desk, a failed `download_multi_pdf`.

────────────────────────────────────────────────────────────────────────────
THE FIX: A PDF-ONLY BASE URL
────────────────────────────────────────────────────────────────────────────

Changing `host_name` would fix the PDF and break every link the site emails —
password reset, two-factor, "view online". So the address is changed only where
wkhtmltopdf reads it. `install()` replaces two names inside `frappe.utils.pdf`
and nowhere else:

  * `scrub_urls` — runs Frappe's own `scrub_urls` with `host_name` pointed at
    the INTERNAL base for the length of the call, then (a) moves any absolute URL
    on one of this site's own public addresses onto the internal base, for
    resources only (img/script/link/source src|href, CSS `url()`), and (b) puts
    the PUBLIC address back on `<a href>` links, so a link printed in the PDF
    still opens for the person reading it;
  * `get_cookie_options` — Frappe's own, with the same `host_name`, so the
    session cookie wkhtmltopdf carries is scoped to the internal host it now
    fetches from. It also adds `load-media-error-handling: ignore`: an image on
    a host this container cannot reach is left out instead of failing the PDF.

Email bodies, `get_url()` everywhere else, and every user-facing link are
untouched: `frappe.utils.scrub_urls` itself is not replaced, only the name
`frappe.utils.pdf` imported.

THE INTERNAL BASE. `pdf_base_url` on ERPNext MCP Settings when it is filled in
(`off` turns all of this off). Otherwise the first of `AUTO_CANDIDATES` that
accepts a TCP connection from this process — in the fafo-erpnext image that is
the container's own nginx on 127.0.0.1:8080, which serves `/assets` and
`/files` for the one site whatever Host header arrives
(`FRAPPE_SITE_NAME_HEADER=frontend`). A bench where nothing answers there gets
Frappe's behaviour unchanged.

WHEN IT IS INSTALLED. `before_request` and `before_job` (hooks.py), so the web
workers that serve Desk prints and the queue workers that send Email Queue
both have it before any PDF is drawn; and by this app's own PDF callers. It is
idempotent and costs one boolean check per request after the first.
"""

from __future__ import annotations

import re
import socket
import time
from urllib.parse import urlsplit

import frappe

#: Tried in order when `pdf_base_url` is empty. The fafo-erpnext image's own
#: nginx (Dockerfile: "frontend — nginx on 0.0.0.0:8080").
AUTO_CANDIDATES = ("http://127.0.0.1:8080",)

#: A cached answer to "what is the base" is good for this long per process.
CACHE_SECONDS = 300

#: How long the auto-detect waits for a connection.
PROBE_TIMEOUT = 0.3

#: Marker on the replacements, so `install` can tell they are ours.
_MARK = "_erpnext_mcp_pdf_base"

_CACHE: dict = {}
_INSTALLED = {"done": False}

_TAG = re.compile(r"<(?P<name>[a-zA-Z][a-zA-Z0-9]*)\b[^>]*>", re.S)
_ATTR = re.compile(
	r"(?P<lead>\b(?:src|href|srcset|xlink:href)\s*=\s*)(?P<q>[\"'])(?P<url>.*?)(?P=q)", re.S | re.I
)
_CSS_URL = re.compile(r"(?P<lead>url\(\s*)(?P<q>[\"']?)(?P<url>[^\"')]*)(?P=q)(?P<tail>\s*\))", re.I)


# ── which base ──────────────────────────────────────────────────────────────
def internal_base() -> str:
	"""The base wkhtmltopdf should fetch from, or "" for Frappe's own behaviour."""
	try:
		from . import settings

		explicit = settings.pdf_base_url()
	except Exception:  # pragma: no cover - settings unreadable
		explicit = ""
	if explicit.lower() in ("off", "none", "disabled"):
		return ""
	if explicit:
		return explicit.rstrip("/")
	cached = _CACHE.get("auto")
	if cached and time.time() - cached[1] < CACHE_SECONDS:
		return cached[0]
	found = ""
	for candidate in AUTO_CANDIDATES:
		if _answers(candidate):
			found = candidate
			break
	_CACHE["auto"] = (found, time.time())
	return found


def source() -> str:
	"""Where `internal_base` came from: "setting", "auto", "off" or "none"."""
	try:
		from . import settings

		explicit = settings.pdf_base_url()
	except Exception:  # pragma: no cover
		explicit = ""
	if explicit.lower() in ("off", "none", "disabled"):
		return "off"
	if explicit:
		return "setting"
	return "auto" if internal_base() else "none"


def _answers(url: str) -> bool:
	parts = urlsplit(url)
	host = parts.hostname or ""
	port = parts.port or (443 if parts.scheme == "https" else 80)
	try:
		with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
			return True
	except OSError:
		return False


def self_origins() -> list:
	"""Every public address this site is known by, longest first. Never raises."""
	found = []
	try:
		found.append(str(frappe.utils.get_url() or ""))
	except Exception:  # pragma: no cover
		pass
	try:
		from . import settings

		found += [settings.public_url(), settings.farmops_public_url()]
	except Exception:  # pragma: no cover
		pass
	conf = getattr(frappe.local, "conf", None) or {}
	for key in ("host_name", "hostname"):
		if conf.get(key):
			found.append(str(conf.get(key)))
	site = str(getattr(frappe.local, "site", "") or "")
	if site:
		found += [f"http://{site}", f"https://{site}"]
	out = []
	for origin in found:
		origin = str(origin or "").strip().rstrip("/")
		if origin.startswith(("http://", "https://")) and origin not in out:
			out.append(origin)
	return sorted(out, key=len, reverse=True)


# ── the rewrite ─────────────────────────────────────────────────────────────
class _HostName:
	"""`frappe.local.conf.host_name` = `base` for the length of a `with`."""

	_MISSING = object()

	def __init__(self, base: str):
		self.base = base

	def __enter__(self):
		self.conf = getattr(frappe.local, "conf", None)
		if self.conf is None:
			return self
		self.old = self.conf.get("host_name", self._MISSING)
		self.conf["host_name"] = self.base
		return self

	def __exit__(self, *exc):
		if self.conf is None:
			return False
		if self.old is self._MISSING:
			self.conf.pop("host_name", None)
		else:
			self.conf["host_name"] = self.old
		return False


def retarget(html: str, base: str, origins: list, public: str) -> str:
	"""Resources onto `base`; `<a href>` links back onto `public`. Pure."""

	def to_base(url: str) -> str:
		for origin in origins:
			if url == origin or url.startswith(origin + "/"):
				rest = url[len(origin) :]
				return base + (rest if rest.startswith("/") else "/" + rest)
		return url

	def to_public(url: str) -> str:
		if public and (url == base or url.startswith(base + "/")):
			return public + url[len(base) :]
		return url

	def tag(match):
		text = match.group(0)
		name = match.group("name").lower()
		fix = to_public if name == "a" else to_base

		def attr(found):
			return f"{found.group('lead')}{found.group('q')}{fix(found.group('url'))}{found.group('q')}"

		text = _ATTR.sub(attr, text)
		if name != "a":
			text = _CSS_URL.sub(lambda found: _css(found, to_base), text)
		return text

	html = _TAG.sub(tag, html or "")
	# `url()` inside <style> blocks, outside any tag.
	return _CSS_URL.sub(lambda found: _css(found, to_base), html)


def _css(found, fix) -> str:
	return f"{found.group('lead')}{found.group('q')}{fix(found.group('url'))}{found.group('q')}{found.group('tail')}"


def pdf_scrub_urls(html: str, original) -> str:
	"""Frappe's `scrub_urls`, aimed at the internal base. See the module docstring."""
	base = internal_base()
	if not base:
		return original(html)
	origins = self_origins()
	public = origins_public()
	with _HostName(base):
		out = original(html)
	return retarget(out, base, [origin for origin in origins if origin != base], public)


def origins_public() -> str:
	"""The address a reader of the PDF would open a link on."""
	try:
		return str(frappe.utils.get_url() or "").rstrip("/")
	except Exception:  # pragma: no cover
		return ""


def pdf_cookie_options(original) -> dict:
	"""Frappe's cookie jar, scoped to the internal host; missing images tolerated."""
	base = internal_base()
	if not base:
		return original()
	with _HostName(base):
		options = dict(original() or {})
	options.setdefault("load-media-error-handling", "ignore")
	return options


# ── installing it ───────────────────────────────────────────────────────────
def install(*_args, **_kwargs) -> bool:
	"""Point `frappe.utils.pdf` at the PDF-only base. Idempotent; never raises.

	Takes and ignores any arguments: `before_request` and `before_job` call it
	with whatever they pass.
	"""
	if _INSTALLED["done"]:
		return True
	try:
		from frappe.utils import pdf as frappe_pdf
	except Exception:
		return False
	return patch(frappe_pdf)


def patch(module) -> bool:
	"""Replace `scrub_urls` and `get_cookie_options` on `module`. Separate for tests."""
	try:
		original_scrub = module.scrub_urls
		original_cookie = module.get_cookie_options
	except AttributeError:
		return False
	if getattr(original_scrub, _MARK, False):
		_INSTALLED["done"] = True
		return True

	def scrub_urls(html):
		return pdf_scrub_urls(html, original_scrub)

	def get_cookie_options():
		return pdf_cookie_options(original_cookie)

	setattr(scrub_urls, _MARK, True)
	setattr(get_cookie_options, _MARK, True)
	module.scrub_urls = scrub_urls
	module.get_cookie_options = get_cookie_options
	_INSTALLED["done"] = True
	return True


def installed() -> bool:
	try:
		from frappe.utils import pdf as frappe_pdf
	except Exception:
		return False
	return bool(getattr(getattr(frappe_pdf, "scrub_urls", None), _MARK, False))


def status() -> dict:
	"""The `pdf` block of `get_server_status`. No rendering; never raises."""
	try:
		base = internal_base()
		return {"base_url": base or None, "source": source(), "installed": installed()}
	except Exception as exc:  # pragma: no cover
		return {"error": str(exc)}
