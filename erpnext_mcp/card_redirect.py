# SPDX-License-Identifier: MIT
"""Every ID and asset print draws the CR80 card. v0.213.0.

Before v0.209.0 this app had four older ways to print an identifier, each with
its own layout: "Print Badge Sheet" on the Employee list (eight to a sheet of
Letter), "ID Card" on the Employee form, "Generate QR Sheet" / "QR Tag" on the
Asset Register, and the "Employee Badge Card" print format on a badge row. The
approved card is `card_art`'s — the one `request_card_print` records and the
"Employee ID Card (CR80)" / "Asset Tag Card (CR80)" print formats draw.

THE REDIRECT IS ON THE SERVER, NOT IN THE BUTTONS. Each of those buttons is a
Client Script row an operator may have edited, and each one only displays the
`html` its method answers. So the methods now answer the CR80 card, one side
per 85.6 × 54 mm page, and every existing button, edited or not, prints the new
card. Issuing a badge, the role gates and the per-record permission checks are
the old methods' own and are unchanged.

THE WAY BACK is the Farm Feature Flag `legacy_badge_layouts` (default off): set
it and the four methods draw what they drew before. The layouts are not deleted.
"""

from __future__ import annotations

import html as html_lib

import frappe

from . import card_art, card_print, card_print_format

FLAG = "legacy_badge_layouts"
EMPLOYEE = "Employee"
ASSET = "Asset Register"
FORMATS = {doctype: name for doctype, name, _job in card_print_format.FORMATS}

STYLE = """
  @page { size: 85.6mm 54mm; margin: 0; }
  html, body { margin: 0; padding: 0; background: #ffffff; }
  .cr80 { width: 85.6mm; height: 54mm; overflow: hidden; line-height: 0;
          page-break-after: always; break-after: page; }
  .cr80:last-child { page-break-after: auto; break-after: auto; }
  .cr80-note { font: 9pt Helvetica, Arial, sans-serif; line-height: 1.3; padding: 6mm; color: #b3261e; }
  @media screen { .cr80 { margin: 4mm auto; box-shadow: 0 0 0 0.2mm #cccccc; } }
"""


def legacy() -> bool:
	"""Whether this site asked for the old layouts back. Never raises."""
	try:
		from . import flags

		return bool(flags.enabled(FLAG, default=False))
	except Exception:
		return False


def _note(text: str) -> str:
	return f'<div class="cr80"><div class="cr80-note">{html_lib.escape(text)}</div></div>'


def card_divs(doctype: str, name: str) -> str:
	"""Front then back of one record's card, or a card-sized note saying why not."""
	card = card_print_format.erpnext_mcp_card_svg(doctype, name)
	if card["error"]:
		return _note(f"{name}: {card['error']}")
	return f'<div class="cr80">{card["front"]}</div><div class="cr80">{card["back"]}</div>'


def document(doctype: str, names, errors=None, title: str = "") -> str:
	"""A printable document: one card-sized page per side, for each record named.

	Whoever was skipped is printed on a last card-sized page, the promise the old
	sheets made — the person holding the paper finds out without the screen.
	"""
	body = [card_divs(doctype, str(name)) for name in names or [] if name]
	skipped = []
	for entry in errors or []:
		who = entry.get("employee") or entry.get("asset_name") or entry.get("index") or "?"
		skipped.append(f"{who}: {entry.get('error') or 'not printed'}")
	if skipped:
		body.append(_note("No card for — " + "; ".join(skipped)))
	if not body:
		body.append(_note("No cards to print."))
	return (
		'<!doctype html><html><head><meta charset="utf-8">'
		f"<title>{html_lib.escape(title or FORMATS.get(doctype, 'Cards'))}</title>"
		f"<style>{STYLE}</style></head><body>" + "".join(body) + "</body></html>"
	)


def pdf(doctype: str, name: str) -> tuple:
	"""(bytes | None, note) — the card as the PDF a print job carries. Never raises.

	`card_art` draws it in the standard library, so unlike the old badge PDF this
	needs no wkhtmltopdf on the bench.
	"""
	job_type = next((job for dt, _n, job in card_print_format.FORMATS if dt == doctype), "")
	try:
		_name, _title, company, row = (card_print._employee if doctype == EMPLOYEE else card_print._asset)(
			name
		)
		sides, _warnings = card_print.card_sides(job_type, row, company, allow_issue=False)
		try:
			rotations = card_print._rotations(card_print.station_for(job_type, company))
		except Exception:
			rotations = card_print._rotations({})
		return card_art.to_pdf(sides, rotations, title=f"{job_type} {name}"), ""
	except Exception as exc:
		return None, f"the card PDF could not be drawn ({exc}); the card's HTML prints the same card."


def badge_format_template() -> str:
	"""The "Employee Badge Card" print format (on a badge row), drawing the CR80 card."""
	return (
		"\n<style>"
		+ STYLE
		+ "\n  .print-format { margin: 0 !important; padding: 0 !important; width: 85.6mm !important;"
		" min-height: 0 !important; box-shadow: none !important; border: 0 !important; }"
		"\n  .print-toolbar, .navbar, .no-print, .letter-head, .print-heading, .footer, .page-footer"
		" { display: none !important; }\n</style>\n"
		'{%- set card = erpnext_mcp_card_svg("Employee", doc.employee) -%}\n'
		"{%- if card.error %}\n"
		'<div class="cr80"><div class="cr80-note">{{ card.error }}</div></div>\n'
		"{%- else %}\n"
		'<div class="cr80">{{ card.front | safe }}</div>\n'
		'<div class="cr80">{{ card.back | safe }}</div>\n'
		"{%- endif %}\n"
	)


def _same(a: str, b: str) -> bool:
	return "".join(str(a or "").split()) == "".join(str(b or "").split())


def repoint_badge_print_format() -> dict:
	"""Make an UNEDITED "Employee Badge Card" format draw the CR80 card. Never raises.

	An operator's own edit is left exactly as it is and reported; so is a format
	that already draws the new card.
	"""
	from . import badge_print_format as legacy_format

	report = {"format": legacy_format.FORMAT_NAME, "changed": False, "reason": ""}
	try:
		if not frappe.db.exists("Print Format", legacy_format.FORMAT_NAME):
			report["reason"] = "not on this site"
			return report
		current = str(frappe.db.get_value("Print Format", legacy_format.FORMAT_NAME, "html") or "")
		if _same(current, badge_format_template()):
			report["reason"] = "already draws the CR80 card"
		elif _same(current, legacy_format.LEGACY_TEMPLATE):
			frappe.db.set_value("Print Format", legacy_format.FORMAT_NAME, "html", badge_format_template())
			report["changed"] = True
		else:
			report["reason"] = (
				"it has been edited on this site, so it was left alone — delete the row and "
				"migrate to get the CR80 version, or keep yours"
			)
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		report["reason"] = f"{type(exc).__name__}: {exc}"
	return report
