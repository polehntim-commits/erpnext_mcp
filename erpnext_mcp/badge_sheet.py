# SPDX-License-Identifier: MIT
"""A crew's badges on one sheet of Letter, laid out at CR-80. v0.56.0.

`generate_employee_badge_sheet` has issued a crew's badges since v0.50.0 and
returns CARD DATA — a name, the Employee form's Company Details (designation,
department, branch, who they report to), a camp and cabin, a photo URL, a base64
QR and a `print` block of numbers a layout must honour. `_print_spec` says in its own
docstring that this app does not lay the card out, and that was the right call
for a tool whose answer is consumed by an Avery template, a label printer, a
handset preview and an MCP client in turn.

The Desk is the fifth consumer and the one with nowhere to send the JSON. An HR
manager who ticks thirty pickers on the Employee list wants thirty cards, and
"here is a JSON array containing thirty base64 PNGs" is not that.

────────────────────────────────────────────────────────────────────────────
THE LAYOUT IS PYTHON AND NOT JAVASCRIPT, WHICH IS THE WHOLE DESIGN
────────────────────────────────────────────────────────────────────────────

The obvious build puts the grid in the button's client script: it already has
the card data in hand. It was not done that way because the layout is the part
worth testing and a Client Script is the part this repo's standalone suite
cannot reach. Here, `sheet_html` is a pure function from card dicts to a string
— `test_badge_sheet.py` asserts the pagination, the escaping and the
millimetres — and the two buttons that call it are four lines of `frappe.call`
apiece.

It also means the card geometry has ONE definition. `CARD_CSS` comes from
`badge_print_format.py`, so the badge on this sheet and the badge from the Print
button are the same card measured the same way. Two copies of that stylesheet
would have drifted the first time somebody nudged a photo frame.

────────────────────────────────────────────────────────────────────────────
EIGHT TO A PAGE, AND WHAT THE SHEET IS ACTUALLY FOR
────────────────────────────────────────────────────────────────────────────

Letter is 215.9 x 279.4mm. At a 9mm margin that leaves 197.9 x 261.4, which
takes two 85.6mm cards across and four 54mm cards down with room for a gutter:
EIGHT to a page, and the rest spill onto the next one.

A DTC4250e TAKES ONE BLANK AT A TIME, so this sheet is not fed to it. It is the
other half of the job: a proofing sheet to check thirty cards before committing
thirty blanks, and a cutting sheet for the sites that print onto adhesive card
stock instead. Cards are laid down at exactly CR-80 with a dashed cut guide, so
what comes off the guillotine drops into a badge holder. The single-card Print
Format is what feeds the printer.

IMAGES HERE ARE URLs AND NOT `data:` URIs, which is the opposite of what
`render/badge_card.py` does and is not an inconsistency. That module inlines
because wkhtmltopdf fetches external URLs synchronously and hangs on one that
404s. THIS page is opened in the operator's own signed-in browser, which fetches
`/files/…` and `/private/files/…` with the session cookie it already holds — so a
URL works, a private photo resolves, and thirty inlined photographs do not become
a forty-megabyte document the browser renders as a spinner. The QR is base64
regardless, because the tool hands it over that way and it was never a URL.
"""

from __future__ import annotations

import html
import json

import frappe

from .badge_print_format import CARD_CSS, CARD_HEIGHT_MM, CARD_WIDTH_MM
from .errors import ToolError

#: Letter, and the margin the grid is measured inside. 9mm rather than a round
#: 10 because two cards plus a 4mm gutter is 175.2mm and the extra millimetre of
#: margin is what keeps a card off the edge of a printer's unprintable region.
PAGE = {"width_mm": 215.9, "height_mm": 279.4, "margin_mm": 9.0}

#: What fits, derived rather than asserted — `test_badge_sheet.py` checks the
#: arithmetic against `PAGE` so a change to the margin cannot leave these stale.
CARDS_ACROSS = 2
CARDS_DOWN = 4
CARDS_PER_PAGE = CARDS_ACROSS * CARDS_DOWN

#: The gutter between cards, which is also the cut allowance.
GUTTER_MM = 4.0

#: Everything that is not the card: the page, the grid, and the on-screen banner
#: that does not print. `CARD_CSS` is prepended by `sheet_html`.
SHEET_CSS = """
  @page { size: Letter; margin: %(margin)smm; }
  html, body { margin: 0; padding: 0; background: #ffffff; color: #111111;
               font-family: Helvetica, Arial, sans-serif; }
  .sheet-bar { padding: 10px 14px; border-bottom: 1px solid #d5dce3; background: #f4f6f8;
               font-size: 13px; color: #33404d; }
  .sheet-bar button { font: inherit; padding: 4px 12px; margin-right: 10px; cursor: pointer; }
  .sheet-bar .warn { color: #b3261e; }
  .sheet-page { padding: 0; }
  .sheet-page + .sheet-page { page-break-before: always; break-before: page; }
  .sheet-cell { display: inline-block; vertical-align: top;
                margin: 0 %(gutter)smm %(gutter)smm 0;
                outline: 0.2mm dashed #b9c4ce; outline-offset: 0; }
  .sheet-note { font-size: 11px; color: #5a6672; padding: 8px 14px; }
  @media print {
    .no-print { display: none !important; }
    html, body { background: #ffffff !important; }
    .sheet-cell { outline: 0.2mm dashed #cfd8e0; }
  }
""" % {"margin": PAGE["margin_mm"], "gutter": GUTTER_MM}


def _esc(value) -> str:
	"""Every card value goes through this. A card carries an employee's own name.

	`html.escape` rather than Frappe's `escape_html` so `sheet_html` stays a pure
	function the standalone suite can call without a site — which is the reason
	the layout lives in Python at all.
	"""
	return html.escape(str(value if value is not None else ""), quote=True)


def _qr_src(card: dict) -> str:
	"""The card's QR as something an `<img>` can take, or "".

	`png_base64` is what `generate_employee_badge_sheet` returns. A card rendered
	with `format="matrix"` has none, and a bench with no encoder has none either;
	both print a card with the badge ID and no symbol rather than a broken image.
	"""
	blob = str(card.get("png_base64") or "")
	return f"data:image/png;base64,{blob}" if blob else ""


def card_html(card: dict) -> str:
	"""One badge front, in the same classes and the same millimetres as the Print Format.

	FRONT ONLY, and deliberately. The back of the card is the 38.1mm symbol that
	`badge_print_format` puts on page two, and a sheet of backs is not something
	anybody cuts out — the front carries its own 23mm code and scans.
	"""
	logo = str(card.get("company_logo_url") or "")
	photo = str(card.get("photo_url") or "")
	qr = _qr_src(card)
	shifted = " bc-org-logo" if logo else ""

	parts = ['<div class="badge-card">', '<div class="bc-abs bc-band"></div>']
	if logo:
		parts.append(f'<img class="bc-abs bc-logo" src="{_esc(logo)}" alt="">')
	parts.append(
		f'<div class="bc-abs bc-org{shifted}">{_esc(card.get("company"))}</div>'
		f'<div class="bc-abs bc-kind{" bc-kind-logo" if logo else ""}">Employee Badge</div>'
	)
	if photo:
		parts.append(f'<img class="bc-abs bc-photo" src="{_esc(photo)}" alt="">')
	else:
		parts.append(f'<div class="bc-abs bc-initials">{_esc(card.get("photo_placeholder") or "?")}</div>')
	parts.append(f'<div class="bc-abs bc-name">{_esc(card.get("employee_name"))}</div>')
	# Under the name: the job title and the department. See `CARD_CSS` in
	# `badge_print_format.py` for why these are fixed slots that do not close up,
	# and note that every label here is the same word the Jinja format prints. A
	# card off this sheet and a card off the Print button have to be the same card.
	if card.get("designation"):
		parts.append(f'<div class="bc-abs bc-line bc-role">{_esc(card.get("designation"))}</div>')
	if card.get("department"):
		parts.append(f'<div class="bc-abs bc-line bc-dept">{_esc(card.get("department"))}</div>')
	parts.append(
		'<div class="bc-abs bc-idlabel">Badge</div>'
		f'<div class="bc-abs bc-id">{_esc(card.get("badge_id"))}</div>'
	)
	if qr:
		parts.append(
			f'<img class="bc-abs bc-qr" src="{qr}" alt="">'
			f'<div class="bc-abs bc-qrcap">{_esc(card.get("badge_id"))}</div>'
		)
	# The detail band, in the full-width dead space below the photograph and the
	# QR caption: the reporting line, the branch, and the cabin.
	if card.get("reports_to_name"):
		parts.append(
			f'<div class="bc-abs bc-line bc-reports">Reports to: {_esc(card.get("reports_to_name"))}</div>'
		)
	if card.get("branch"):
		parts.append(f'<div class="bc-abs bc-line bc-branch">{_esc(card.get("branch"))}</div>')
	if card.get("housing"):
		parts.append(f'<div class="bc-abs bc-line bc-house">Camp: {_esc(card.get("housing"))}</div>')

	parts.append('<div class="bc-abs bc-foot"></div>')
	number = card.get("employee_number")
	parts.append(
		f'<div class="bc-abs bc-foot-l">{("No. " + _esc(number)) if number else ""}</div>'
		f'<div class="bc-abs bc-foot-r">{_esc(card.get("employee"))}</div>'
	)
	parts.append("</div>")
	return "".join(parts)


def paginate(cards: list) -> list:
	"""Cards grouped into pages of `CARDS_PER_PAGE`. Empty in, empty out."""
	rows = list(cards or [])
	return [rows[start : start + CARDS_PER_PAGE] for start in range(0, len(rows), CARDS_PER_PAGE)]


def sheet_html(cards: list, errors: list | None = None, title: str = "Badge Sheet") -> str:
	"""The whole printable document, as a string. A PURE FUNCTION — no site needed.

	THE ERRORS ARE ON THE PAGE AND NOT ONLY IN THE CONSOLE. The sheet tool's own
	contract is that one bad row does not lose the sheet: a name that resolves to
	nobody, somebody who has left, an entity the actor cannot reach — each is
	reported and the other twenty-nine cards still print. That promise is only
	kept if whoever collects the paper can see WHO IS MISSING, so the skipped
	names are printed in a block above the cards. A crew of thirty that quietly
	came out as twenty-eight is two pickers with no badge on Monday.
	"""
	pages = paginate(cards)
	skipped = list(errors or [])

	body = [
		'<div class="sheet-bar no-print">',
		'<button type="button" onclick="window.print()">Print</button>',
		f"<span>{len(cards or [])} card(s) on {max(1, len(pages))} sheet(s) of Letter, "
		f"laid out at {CARD_WIDTH_MM} &times; {CARD_HEIGHT_MM}mm (CR-80).</span>",
	]
	if skipped:
		body.append(f'<span class="warn"> &nbsp;{len(skipped)} skipped &mdash; see below.</span>')
	body.append("</div>")

	if skipped:
		body.append('<div class="sheet-note"><b>No card was printed for:</b><ul>')
		for entry in skipped:
			who = _esc(entry.get("employee") or f"entry {entry.get('index')}")
			body.append(f"<li>{who} &mdash; {_esc(entry.get('error'))}</li>")
		body.append("</ul></div>")

	if not pages:
		body.append('<div class="sheet-note">No badge cards were produced.</div>')
	for page in pages:
		body.append('<div class="sheet-page">')
		body.extend(f'<div class="sheet-cell">{card_html(card)}</div>' for card in page)
		body.append("</div>")

	return (
		"<!doctype html>\n"
		'<html><head><meta charset="utf-8">'
		f"<title>{_esc(title)}</title>"
		f"<style>{CARD_CSS}{SHEET_CSS}</style>"
		"</head><body>" + "".join(body) + "</body></html>"
	)


@frappe.whitelist(methods=["POST"])
def render_badge_sheet(employees=None, company=None):
	"""Issue (or reuse) a badge for each employee and hand back a printable sheet.

	A THIN WRAPPER OVER THE TOOL AND NOT A SECOND IMPLEMENTATION. Every gate that
	matters is `generate_employee_badge_sheet`'s own — the HR role, the company
	scope, the employment status, the one-active-badge rule, the hundred-card cap
	— and this method adds nothing to them and takes nothing away. It turns a
	form post into that tool's argument dict and its answer into HTML.

	`ToolError` becomes `frappe.throw` for the reason `api/gis.speaks_frappe`
	gives at length: raised out of a whitelisted method it would be an HTTP 500
	and a traceback in the console, and the sentence the tool wrote — the one
	naming which employee has left and what to do about it — would never reach
	the person who needs it. Anything that is NOT a ToolError is left alone and
	still reaches the Error Log, because that is a bug.
	"""
	from .tools import badges

	names = employees
	if isinstance(names, str):
		# `frappe.call` posts a JS array as a JSON string. A bare comma-separated
		# string is left for the tool, which already accepts one.
		text = names.strip()
		if text.startswith("["):
			try:
				names = json.loads(text)
			except ValueError:
				frappe.throw(
					frappe._("The list of employees could not be read."), title=frappe._("Badge Sheet")
				)

	try:
		result = badges.generate_employee_badge_sheet({"employees": names, "company": company})
	except ToolError as error:
		frappe.throw(str(error), title=frappe._("Badge Sheet"))
		return None  # pragma: no cover - frappe.throw does not return

	data = result.data or {}
	# v0.213.0. THE SHEET IS NOW THE CR80 CARD, one side per card-sized page — see
	# `card_redirect`. The badges were issued by the tool above exactly as before.
	from . import card_redirect

	if card_redirect.legacy():
		html = sheet_html(data.get("cards") or [], data.get("errors") or [])
	else:
		html = card_redirect.document(
			"Employee",
			[card.get("employee") for card in data.get("cards") or []],
			data.get("errors") or [],
			"Employee ID Cards",
		)
	return {
		"html": html,
		"card_count": data.get("card_count") or 0,
		"issued_count": data.get("issued_count") or 0,
		"errors": data.get("errors") or [],
		"summary": result.summary,
	}


__all__ = (
	"CARDS_ACROSS",
	"CARDS_DOWN",
	"CARDS_PER_PAGE",
	"PAGE",
	"card_html",
	"paginate",
	"render_badge_sheet",
	"sheet_html",
)
