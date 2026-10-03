# SPDX-License-Identifier: MIT
"""test_pdf_rendering — draw a PDF the way the Desk and Email Queue do, and say how it went. v0.216.1.

Read-only: nothing is saved, attached or emailed. It renders a one-page test
print through `frappe.utils.pdf.get_pdf` — the same function behind Print →
PDF, `download_multi_pdf` and `frappe.attach_print` — with a stylesheet linked
by a relative URL, which is exactly what failed with HostNotFoundError. With
`doctype` and `name` it also renders that document's real print.
"""

from __future__ import annotations

import time

import frappe

from .. import pdf_base
from ..args import as_str
from ..errors import ToolError
from ..result import ToolResult

ARGUMENTS = ("doctype", "name", "print_format")

#: wkhtmltopdf's own words, and what each means here.
_DIAGNOSES = (
	(
		"HostNotFoundError",
		"a stylesheet or image URL in the print names a host this container cannot resolve. "
		"pdf_base_url (ERPNext MCP Settings) should be an address the container itself serves, "
		"e.g. http://127.0.0.1:8080.",
	),
	(
		"ConnectionRefusedError",
		"a print asset URL points at a port nothing in this container listens on. Check pdf_base_url.",
	),
	(
		"No writable cache",
		"fontconfig has no writable cache directory: /var/cache/fontconfig must be writable.",
	),
	("No such file or directory: 'wkhtmltopdf'", "wkhtmltopdf is not installed in this image."),
	("TimeoutError", "an external image or stylesheet did not answer; wkhtmltopdf waited for it."),
)


def _diagnose(message: str) -> str:
	for marker, meaning in _DIAGNOSES:
		if marker in message:
			return meaning
	return ""


def _test_html() -> str:
	try:
		from frappe.utils.jinja_globals import bundled_asset

		css = bundled_asset("print.bundle.css")
	except Exception:
		css = "/assets/frappe/css/print.css"
	return (
		"<!DOCTYPE html><html><head><meta charset='utf-8'>"
		f"<link rel='stylesheet' href='{css}'>"
		"</head><body><div class='print-format'>"
		"<h2>ERPNext MCP PDF self-test</h2>"
		f"<p>Rendered {frappe.utils.now()} by test_pdf_rendering.</p>"
		"</div></body></html>"
	)


def _pages(data: bytes):
	try:
		import io

		from pypdf import PdfReader

		return len(PdfReader(io.BytesIO(data)).pages)
	except Exception:
		return None


def _render(label: str, draw) -> dict:
	started = time.monotonic()
	row = {"render": label}
	try:
		data = draw()
	except Exception as exc:
		message = f"{type(exc).__name__}: {exc}"
		row.update({"ok": False, "error": message[:600], "diagnosis": _diagnose(message) or None})
	else:
		ok = isinstance(data, (bytes, bytearray)) and bytes(data[:5]) == b"%PDF-"
		row.update({"ok": ok, "bytes": len(data or b""), "pages": _pages(bytes(data)) if ok else None})
		if not ok:
			row["error"] = "the renderer returned something that is not a PDF"
	row["ms"] = round((time.monotonic() - started) * 1000)
	return row


def _assets_reachable(base: str) -> dict:
	if not base:
		return {"checked": False}
	import urllib.request

	try:
		with urllib.request.urlopen(f"{base}/assets/assets.json", timeout=3) as response:
			return {"checked": True, "url": f"{base}/assets/assets.json", "status": response.status}
	except Exception as exc:
		return {"checked": True, "url": f"{base}/assets/assets.json", "error": f"{type(exc).__name__}: {exc}"}


def test_pdf_rendering(args: dict) -> ToolResult:
	"""Read-only. Render a test PDF (and optionally one document's print); report the outcome."""
	doctype = as_str(args, "doctype").strip()
	name = as_str(args, "name").strip()
	print_format = as_str(args, "print_format").strip() or None
	if bool(doctype) != bool(name):
		raise ToolError("pass both `doctype` and `name` to render a document's print, or neither.")
	if doctype and not frappe.has_permission(doctype, "print", doc=name):
		raise ToolError(f"you may not print {doctype} {name}. Nothing was rendered.")

	pdf_base.install()
	data = {
		"pdf_base": pdf_base.status(),
		"origins_rewritten": pdf_base.self_origins(),
	}
	data["pdf_base"]["assets"] = _assets_reachable(data["pdf_base"].get("base_url") or "")

	try:
		from frappe.utils.pdf import get_pdf, get_wkhtmltopdf_version
	except Exception as exc:
		data.update({"ok": False, "renders": [], "error": f"frappe.utils.pdf is unavailable: {exc}"})
		return ToolResult(data=data, summary="PDF rendering is unavailable on this bench")

	try:
		data["wkhtmltopdf_version"] = get_wkhtmltopdf_version()
	except Exception:  # pragma: no cover
		data["wkhtmltopdf_version"] = None

	renders = [_render("test page", lambda: get_pdf(_test_html()))]
	if doctype:
		renders.append(
			_render(
				f"{doctype} {name}",
				lambda: frappe.get_print(doctype, name, print_format, as_pdf=True),
			)
		)
	data["renders"] = renders
	data["ok"] = all(row["ok"] for row in renders)
	failed = [row for row in renders if not row["ok"]]
	summary = (
		"PDF rendering works: "
		+ ", ".join(f"{row['render']} ({row.get('pages')} page(s), {row['ms']} ms)" for row in renders)
		if not failed
		else f"PDF rendering FAILED for {failed[0]['render']}: {failed[0].get('diagnosis') or failed[0].get('error')}"
	)
	return ToolResult(data=data, summary=summary)
