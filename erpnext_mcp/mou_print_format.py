# SPDX-License-Identifier: MIT
"""The Memorandum of Understanding for a Lot Line Adjustment: one template, two doors.

v0.169.0. The Desk's Print button on a Lot Line Adjustment and the MCP tool
`lla_render_mou` print the SAME Jinja template, `MOU_TEMPLATE`. The Print Format
record holds it for the button; the tool renders it straight from the record,
or through the site's Print Format where one exists, so an operator who edited
the layout gets their edit from both.

SEEDED, NOT FIXTURED — the contract `badge_print_format` and `i9_print_format`
already keep. `seed_mou_print_format` creates the format if it is missing and
touches nothing that is there; `standard = "No"` with `custom_format = 1` makes
an operator's edits survive `bench migrate`.

THE TEMPLATE READS THE RECORD AND NOTHING ELSE. No `<img>`, no stylesheet link,
no database lookup inside Jinja: wkhtmltopdf fetches every external URL and a
broken one hangs the render (`i9_print_format` states the failure). Every value
is read with `.get`, which a Frappe Document and a plain dict both answer, and
escaped with `|e`.

THE PDF IS WHERE THE BENCH CAN MAKE ONE. On a bench: Frappe's own print path
(the Print Format through wkhtmltopdf). Where that is missing, the same content
is set in `render/pdf.py`, the standard-library writer every other document in
this app uses, and `renderer` says which produced the file. The content of the
two is identical; only the typography differs.
"""

from __future__ import annotations

import base64

import frappe

from . import land_adjustment as land

PRINT_FORMAT = "Print Format"
FORMAT_NAME = "Memorandum of Understanding"

MOU_TEMPLATE = """\
{%- set p1 = doc.get("party_1") or "Party 1" -%}
{%- set p2 = doc.get("party_2") or "Party 2" -%}
{%- macro who(value) -%}{{ (p1 if value == "Party 1" else p2 if value == "Party 2" else value or "") | e }}{%- endmacro -%}
<style>
  .mou { font-family: Georgia, "Times New Roman", serif; font-size: 11pt; line-height: 1.4; color: #111; }
  .mou h1 { font-size: 16pt; text-align: center; margin: 0 0 2pt; }
  .mou .sub { text-align: center; margin: 0 0 12pt; }
  .mou h2 { font-size: 12pt; border-bottom: 1px solid #333; margin: 14pt 0 6pt; }
  .mou table { width: 100%; border-collapse: collapse; margin: 4pt 0; }
  .mou th, .mou td { border: 1px solid #888; padding: 3pt 5pt; vertical-align: top; text-align: left; }
  .mou td.num, .mou th.num { text-align: right; }
  .mou .sign td { border: none; padding-top: 28pt; width: 50%; }
  .mou .line { border-top: 1px solid #111; padding-top: 2pt; }
  .mou pre { font-size: 7.5pt; white-space: pre-wrap; word-break: break-all; border: 1px solid #bbb; padding: 4pt; }
  .mou .exhibit { page-break-before: always; }
</style>
<div class="mou">
  <h1>Memorandum of Understanding</h1>
  <p class="sub">Lot Line Adjustment &mdash; {{ doc.get("title") | e }}<br>
    {{ doc.get("county") | e }} County, {{ doc.get("state") | e }} &middot; {{ doc.get("name") | e }} &middot; Status: {{ doc.get("status") | e }}</p>

  <p>This memorandum records the understanding between <b>{{ p1 | e }}</b> and <b>{{ p2 | e }}</b>
  about an adjustment of the line between their tax lots. It sets out the terms the parties intend to carry
  into the county's lot line adjustment. It is not a deed and does not itself convey any interest in land;
  the adjustment takes effect only when the county approves it and the survey is recorded.</p>

  <h2>1. Parties and lots</h2>
  <table>
    <tr><th></th><th>Party</th><th>Tax lot before</th><th>Signer</th></tr>
    <tr><td>Party 1</td><td>{{ p1 | e }} ({{ doc.get("party_1_type") | e }})</td><td>{{ (doc.get("lot_1") or "to be confirmed") | e }}</td>
        <td>{{ doc.get("signer_1") | e }}{% if doc.get("signer_1_title") %}, {{ doc.get("signer_1_title") | e }}{% endif %}</td></tr>
    <tr><td>Party 2</td><td>{{ p2 | e }} ({{ doc.get("party_2_type") | e }})</td><td>{{ (doc.get("lot_2") or "to be confirmed") | e }}</td>
        <td>{{ doc.get("signer_2") | e }}{% if doc.get("signer_2_title") %}, {{ doc.get("signer_2_title") | e }}{% endif %}</td></tr>
  </table>

  <h2>2. Consideration</h2>
  {%- set consideration = doc.get("consideration") or "Even swap" %}
  {%- if consideration == "Even swap" %}
  <p><b>Even swap.</b> The pieces below are exchanged for one another. No cash changes hands.</p>
  {%- elif consideration == "Cash true-up" %}
  <p><b>Cash true-up.</b> {{ who(doc.get("true_up_payer")) }} pays {{ "%.2f"|format((doc.get("true_up_amount") or 0)|float) }} to the other party in addition to the exchange of pieces.</p>
  {%- else %}
  <p><b>Netted.</b> The value of the pieces is netted between the parties{% if doc.get("true_up_amount") %}, leaving {{ "%.2f"|format((doc.get("true_up_amount") or 0)|float) }} payable by {{ who(doc.get("true_up_payer")) }}{% endif %}.</p>
  {%- endif %}

  <h2>3. Pieces</h2>
  <table>
    <tr><th>Piece</th><th>From</th><th>To</th><th class="num">Acres (GIS)</th><th class="num">Acres (surveyed)</th><th>Improvements</th><th>Line</th></tr>
    {%- for row in doc.get("pieces") or [] %}
    <tr><td>{{ row.get("piece_name") | e }}{% if row.get("gis_sketch_ref") %}<br><small>{{ row.get("gis_sketch_ref") | e }}</small>{% endif %}</td>
        <td>{{ who(row.get("from_party")) }}</td><td>{{ who(row.get("to_party")) }}</td>
        <td class="num">{{ "%.2f"|format(row.get("acres_gis")|float) if row.get("acres_gis") else "" }}</td>
        <td class="num">{{ "%.3f"|format(row.get("acres_surveyed")|float) if row.get("acres_surveyed") else "pending survey" }}</td>
        <td>{{ (row.get("improvements") or "") | e }}</td><td>{{ (row.get("line_notes") or "") | e }}</td></tr>
    {%- else %}
    <tr><td colspan="7">No pieces recorded yet.</td></tr>
    {%- endfor %}
  </table>
  <p>Acreages marked GIS are sketch figures. The recorded survey controls.</p>

  <h2>4. Easements</h2>
  {%- if doc.get("easements") %}
  <table>
    <tr><th>Type</th><th>Burdened</th><th>Benefited</th><th>Notes</th></tr>
    {%- for row in doc.get("easements") %}
    <tr><td>{{ row.get("easement_type") | e }}</td><td>{{ who(row.get("burdened")) }}</td><td>{{ who(row.get("benefited")) }}</td><td>{{ (row.get("notes") or "") | e }}</td></tr>
    {%- endfor %}
  </table>
  {%- else %}
  <p>None recorded.</p>
  {%- endif %}

  <h2>5. Lender</h2>
  {%- if doc.get("lender") %}
  <p>{{ doc.get("lender") | e }}{% if doc.get("lender_conditions") %}: {{ doc.get("lender_conditions") | e }}{% endif %}</p>
  {%- else %}
  <p>No lender named.</p>
  {%- endif %}

  <h2>6. Closing</h2>
  <p>The parties intend to close {% if doc.get("target_close") %}on or before {{ doc.get("target_close") | e }}{% else %}on a date to be agreed{% endif %}. The open items in Exhibit C are to be resolved first.</p>

  <table class="sign">
    <tr><td><div class="line">{{ p1 | e }}<br>By: {{ doc.get("signer_1") | e }}{% if doc.get("signer_1_title") %}, {{ doc.get("signer_1_title") | e }}{% endif %}<br>Date:</div></td>
        <td><div class="line">{{ p2 | e }}<br>By: {{ doc.get("signer_2") | e }}{% if doc.get("signer_2_title") %}, {{ doc.get("signer_2_title") | e }}{% endif %}<br>Date:</div></td></tr>
  </table>

  <div class="exhibit">
    <h2>Exhibit A &mdash; Pieces by acreage</h2>
    <table>
      <tr><th>Piece</th><th>From &rarr; To</th><th class="num">Acres (GIS)</th><th class="num">Acres (surveyed)</th></tr>
      {%- for row in doc.get("pieces") or [] %}
      <tr><td>{{ row.get("piece_name") | e }}</td><td>{{ who(row.get("from_party")) }} &rarr; {{ who(row.get("to_party")) }}</td>
          <td class="num">{{ "%.2f"|format(row.get("acres_gis")|float) if row.get("acres_gis") else "" }}</td>
          <td class="num">{{ "%.3f"|format(row.get("acres_surveyed")|float) if row.get("acres_surveyed") else "" }}</td></tr>
      {%- endfor %}
    </table>

    <h2>Exhibit B &mdash; Geometry (GeoJSON, WGS84)</h2>
    {%- for row in doc.get("pieces") or [] %}
    <p><b>{{ row.get("piece_name") | e }}</b></p>
    <pre>{{ (row.get("geometry") or "No geometry recorded.") | e }}</pre>
    {%- endfor %}

    <h2>Exhibit C &mdash; Open items</h2>
    <table>
      <tr><th>Item</th><th>Question</th><th>Owner</th><th>Status</th><th>Due</th></tr>
      {%- for row in doc.get("open_items") or [] %}
      <tr><td>{{ row.get("item") | e }}</td><td>{{ (row.get("question") or "") | e }}</td><td>{{ (row.get("responsible") or "") | e }}</td>
          <td>{{ (row.get("status") or "Open") | e }}</td><td>{{ (row.get("due") or "") | e }}</td></tr>
      {%- else %}
      <tr><td colspan="5">None.</td></tr>
      {%- endfor %}
    </table>
  </div>
</div>
"""


def print_format_fields() -> dict:
	return {
		"doctype": PRINT_FORMAT,
		"name": FORMAT_NAME,
		"doc_type": land.LOT_LINE_ADJUSTMENT,
		"module": "ERPNext MCP",
		"standard": "No",
		"custom_format": 1,
		"print_format_type": "Jinja",
		"print_format_builder": 0,
		"disabled": 0,
		"html": MOU_TEMPLATE,
	}


def seed_mou_print_format() -> dict:
	"""Create the MOU Print Format if this site has not got one. Never raises."""
	report = {"created": False, "name": FORMAT_NAME, "reason": ""}
	try:
		if not frappe.db.exists("DocType", land.LOT_LINE_ADJUSTMENT):
			report["reason"] = "the Lot Line Adjustment doctype has not migrated yet"
			return report
		if not frappe.db.exists("DocType", PRINT_FORMAT):  # pragma: no cover - not a real Frappe
			report["reason"] = "this site has no Print Format doctype"
			return report
		if frappe.db.exists(PRINT_FORMAT, FORMAT_NAME):
			report["reason"] = "already present"
			return report
		doc = frappe.get_doc(print_format_fields())
		doc.flags.ignore_permissions = True
		doc.insert()
		report["created"] = True
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		report["reason"] = f"{type(exc).__name__}: {exc}"
	return report


# ── rendering ───────────────────────────────────────────────────────────────
def render_html(doc) -> str:
	"""The MOU as HTML, from the shipped template. Pure Jinja, no Frappe."""
	import jinja2

	environment = jinja2.Environment(autoescape=False, undefined=jinja2.ChainableUndefined)
	return environment.from_string(MOU_TEMPLATE).render(doc=doc)


def _site_print(name: str, as_pdf: bool):
	"""Frappe's own print path through the site's Print Format, or None where there is none."""
	get_print = getattr(frappe, "get_print", None)
	if not callable(get_print) or not frappe.db.exists(PRINT_FORMAT, FORMAT_NAME):
		return None
	try:
		return get_print(land.LOT_LINE_ADJUSTMENT, name, print_format=FORMAT_NAME, as_pdf=as_pdf)
	except Exception:  # pragma: no cover - wkhtmltopdf missing or failing on this bench
		return None


def render(doc) -> dict:
	"""`{"html", "pdf", "renderer", "note"}` for one adjustment."""
	html = _site_print(doc.name, as_pdf=False) or render_html(doc)
	pdf = _site_print(doc.name, as_pdf=True)
	if pdf:
		return {"html": html, "pdf": pdf, "renderer": "frappe print format (wkhtmltopdf)", "note": ""}
	return {
		"html": html,
		"pdf": builtin_pdf(doc),
		"renderer": "erpnext_mcp render/pdf.py",
		"note": (
			"This bench has no working Print Format PDF path (the Print Format record or wkhtmltopdf "
			"is missing), so the PDF was set by this app's own writer. The content is the same as the "
			"Print Format's; the typography is plainer."
		),
	}


def _acres(value, places: int) -> str:
	return f"{float(value):.{places}f}" if value not in (None, "") and float(value) else ""


def builtin_pdf(doc) -> bytes:
	"""The same memorandum, set in the standard-library PDF writer."""
	from .render.pdf import PdfDocument

	p1 = doc.get("party_1") or land.PARTY_1
	p2 = doc.get("party_2") or land.PARTY_2

	def who(value):
		return p1 if value == land.PARTY_1 else p2 if value == land.PARTY_2 else str(value or "")

	def signer(index):
		name = doc.get(f"signer_{index}") or ""
		title = doc.get(f"signer_{index}_title") or ""
		return f"{name}, {title}" if name and title else name

	pdf = PdfDocument(
		title=f"Memorandum of Understanding — {doc.get('title')}",
		author="erpnext_mcp",
		subject=f"Lot Line Adjustment {doc.name}",
		footer=f"{doc.name} — Memorandum of Understanding — not a deed",
	)
	pdf.title_block(
		"Memorandum of Understanding",
		f"Lot Line Adjustment — {doc.get('title')}",
		f"{doc.get('county')} County, {doc.get('state')} · {doc.name} · Status: {doc.get('status')}",
	)
	pdf.paragraph(
		f"This memorandum records the understanding between {p1} and {p2} about an adjustment of the "
		"line between their tax lots. It sets out the terms the parties intend to carry into the "
		"county's lot line adjustment. It is not a deed and does not itself convey any interest in "
		"land; the adjustment takes effect only when the county approves it and the survey is recorded."
	)
	pdf.heading("1. Parties and lots")
	pdf.table(
		["", "Party", "Tax lot before", "Signer"],
		[
			[
				"Party 1",
				f"{p1} ({doc.get('party_1_type')})",
				doc.get("lot_1") or "to be confirmed",
				signer(1),
			],
			[
				"Party 2",
				f"{p2} ({doc.get('party_2_type')})",
				doc.get("lot_2") or "to be confirmed",
				signer(2),
			],
		],
	)
	pdf.heading("2. Consideration")
	consideration = doc.get("consideration") or "Even swap"
	amount = float(doc.get("true_up_amount") or 0)
	if consideration == "Even swap":
		pdf.paragraph("Even swap. The pieces below are exchanged for one another. No cash changes hands.")
	elif consideration == "Cash true-up":
		pdf.paragraph(
			f"Cash true-up. {who(doc.get('true_up_payer'))} pays {amount:.2f} to the other party in "
			"addition to the exchange of pieces."
		)
	else:
		tail = f", leaving {amount:.2f} payable by {who(doc.get('true_up_payer'))}" if amount else ""
		pdf.paragraph(f"Netted. The value of the pieces is netted between the parties{tail}.")
	pdf.heading("3. Pieces")
	pieces = list(doc.get("pieces") or [])
	pdf.table(
		["Piece", "From", "To", "Acres (GIS)", "Acres (surveyed)", "Improvements", "Line"],
		[
			[
				row.get("piece_name"),
				who(row.get("from_party")),
				who(row.get("to_party")),
				_acres(row.get("acres_gis"), 2),
				_acres(row.get("acres_surveyed"), 3) or "pending survey",
				row.get("improvements") or "",
				row.get("line_notes") or "",
			]
			for row in pieces
		]
		or [["No pieces recorded yet.", "", "", "", "", "", ""]],
		align=["l", "l", "l", "r", "r", "l", "l"],
	)
	pdf.paragraph("Acreages marked GIS are sketch figures. The recorded survey controls.")
	pdf.heading("4. Easements")
	easements = list(doc.get("easements") or [])
	if easements:
		pdf.table(
			["Type", "Burdened", "Benefited", "Notes"],
			[
				[
					row.get("easement_type"),
					who(row.get("burdened")),
					who(row.get("benefited")),
					row.get("notes") or "",
				]
				for row in easements
			],
		)
	else:
		pdf.paragraph("None recorded.")
	pdf.heading("5. Lender")
	if doc.get("lender"):
		conditions = f": {doc.get('lender_conditions')}" if doc.get("lender_conditions") else ""
		pdf.paragraph(f"{doc.get('lender')}{conditions}")
	else:
		pdf.paragraph("No lender named.")
	pdf.heading("6. Closing")
	close = f"on or before {doc.get('target_close')}" if doc.get("target_close") else "on a date to be agreed"
	pdf.paragraph(
		f"The parties intend to close {close}. The open items in Exhibit C are to be resolved first."
	)
	pdf.spacer(24)
	pdf.table(
		[p1, p2],
		[
			[f"By: {signer(1)}", f"By: {signer(2)}"],
			["Signature: ____________________", "Signature: ____________________"],
			["Date: ________", "Date: ________"],
		],
	)
	pdf.page_break()
	pdf.heading("Exhibit A — Pieces by acreage")
	pdf.table(
		["Piece", "From -> To", "Acres (GIS)", "Acres (surveyed)"],
		[
			[
				row.get("piece_name"),
				f"{who(row.get('from_party'))} -> {who(row.get('to_party'))}",
				_acres(row.get("acres_gis"), 2),
				_acres(row.get("acres_surveyed"), 3),
			]
			for row in pieces
		],
		align=["l", "l", "r", "r"],
	)
	pdf.heading("Exhibit B — Geometry (GeoJSON, WGS84)")
	for row in pieces:
		pdf.subheading(str(row.get("piece_name") or ""))
		pdf.paragraph(str(row.get("geometry") or "No geometry recorded."), size=7.0)
	pdf.heading("Exhibit C — Open items")
	items = list(doc.get("open_items") or [])
	pdf.table(
		["Item", "Question", "Owner", "Status", "Due"],
		[
			[
				row.get("item"),
				row.get("question") or "",
				row.get("responsible") or "",
				row.get("status") or "Open",
				str(row.get("due") or ""),
			]
			for row in items
		]
		or [["None.", "", "", "", ""]],
	)
	return pdf.render()


def pdf_base64(pdf: bytes) -> str:
	return base64.b64encode(pdf).decode("ascii")
