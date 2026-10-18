# SPDX-License-Identifier: MIT
"""`page_renderer` entry: the public website under its one prefix. v0.335.0.

Inert unless `public_site_enabled` is ticked AND a `site` config is published —
`can_render` answers False otherwise and Frappe falls through to its own 404, so
an install that never turns the site on serves exactly what it served before.
See `public_site.py`.
"""

import frappe

from . import public_site


class PublicSitePage:
	def __init__(self, path, http_status_code=None):
		self.path = "/" + str(path or "").strip("/ ")
		self.http_status_code = http_status_code or 200

	def _relative(self) -> str | None:
		try:
			prefix = public_site.live_prefix()
		except Exception:
			return None
		if not prefix:
			return None
		if self.path == f"/{prefix}":
			return ""
		if self.path.startswith(f"/{prefix}/"):
			return self.path[len(prefix) + 2 :]
		return None

	def can_render(self) -> bool:
		return self._relative() is not None

	def render(self):
		from werkzeug.wrappers import Response

		request = getattr(frappe, "request", None)
		method = (getattr(request, "method", "GET") or "GET").upper()
		files = dict(getattr(request, "files", {}) or {}) if method == "POST" else {}
		ip = str(getattr(frappe.local, "request_ip", "") or "")
		page = public_site.render(
			self._relative() or "", method=method, form=dict(frappe.form_dict or {}), files=files, ip=ip
		)
		headers = {
			"Cache-Control": "no-store",
			"X-Content-Type-Options": "nosniff",
			"Referrer-Policy": "same-origin",
			"X-Frame-Options": "SAMEORIGIN",
			"Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; "
			"form-action 'self'; frame-ancestors 'self'",
		}
		return Response(page.body, status=page.status, content_type=page.content_type, headers=headers)
