# SPDX-License-Identifier: MIT
"""The Desk's Print button, pointed at a badge, producing a card. v0.56.0.

WHAT WAS THERE BEFORE. `Bucket Log Badge Map` had no Print Format, so Frappe
rendered the standard one: Badge ID, Company, Employee, Active, Notes — five
labelled rows on a sheet of Letter. Every fact on the record and nothing anybody
can clip to a lanyard. `generate_employee_badge_qr` has issued the identifier and
drawn the symbol since v0.50.0 and `_print_spec` states, in numbers, what a
layout has to honour, and then said in its own docstring that THIS APP DOES NOT
LAY THE CARD OUT — the sheet returns card DATA and a template name and leaves the
arrangement to whatever prints it.

This is that arrangement, for one card, on the Desk. It is the first thing in
this app that honours `_print_spec` rather than restating it, and the numbers
below are that spec turned into millimetres.

────────────────────────────────────────────────────────────────────────────
CR-80, AND WHY THE SIZE IS SET IN THREE PLACES
────────────────────────────────────────────────────────────────────────────

A CR-80 card is 85.6mm x 54mm — the ISO/IEC 7810 ID-1 size, the same blank a
DTC4250e takes out of its hopper. Printing 1:1 on a card printer means the page
the driver receives IS the card, so the size has to survive three different
renderers that do not read the same instruction:

  * `@page { size: 85.6mm 54mm; margin: 0 }` is what a BROWSER honours, and the
    browser is the path that matters here. A card printer is driven from the
    print dialogue, not from a downloaded PDF.
  * `page_size` / `page_width` / `page_height` on the Print Format record are
    what WKHTMLTOPDF honours, for the operator who takes the PDF button instead.
    Set only when this site's Print Format actually offers a Custom page size —
    see `_page_size_fields`, and note that the CSS above still governs the print
    dialogue either way.
  * The `.badge-card` box itself is given the same millimetres, because a page
    that is the right size with content laid out for a Letter sheet is a card
    with the top left corner of a form on it.

MARGINS ARE ZERO ON PURPOSE, which is the opposite of what `i9_print_format.py`
chose and for the opposite reason: an I-9 is printed on plain paper and wants a
margin to punch holes in, and a card has no margin because the card IS the page.
The quiet space around the artwork is inside `.badge-card`.

────────────────────────────────────────────────────────────────────────────
TWO PAGES, BECAUSE THE PRINTER HAS TWO SIDES AND THE QR NEEDS THE ROOM
────────────────────────────────────────────────────────────────────────────

`tools/badges.BADGE_PRINT_INCHES` is 1.5" — 38.1mm — and states why: below about
an inch a phone camera at arm's length in bright orchard sun hunts instead of
locking, and that failure presents to a picker as "the scanner is broken". A
38.1mm square will not sit on the FRONT of an 85.6 x 54 card beside a photograph,
a name and a mark. Something had to give, and the honest options were a smaller
symbol or a second side.

THE DTC4250e IS A DUAL-SIDE PRINTER, so it is a second side. The front carries
the photograph, the name, the designation, the mark and a 23mm QR — enough for a
foreman standing over somebody at a bin trailer. The back carries the symbol at
the full 38.1mm the spec asks for, which is the one a phone reads across a row.
Both encode the same bare badge ID, so either side scans to the same person.

A SINGLE-SIDE PRINTER LOSES NOTHING IT NEEDED: page one is a complete badge on
its own. Printing only the front is a supported outcome rather than a broken one.

────────────────────────────────────────────────────────────────────────────
SEEDED, NOT FIXTURED — AND THIS IS THE REQUEST THIS MODULE DID NOT GRANT
────────────────────────────────────────────────────────────────────────────

The obvious way to ship a Print Format with an app is the `fixtures` hook, and
this app cannot use it: `test_hooks.FORBIDDEN_HOOKS` names `fixtures` with the
reason, and `i9_documents.py` argues it. A fixture is REWRITTEN FROM THE APP'S
FILES ON EVERY `bench migrate`, so an operator who nudged a margin because their
own DTC4250e feeds 0.4mm high would lose the correction at the next upgrade,
silently, and would have no way to find out where it went.

`seed_badge_print_format` CREATES WHAT IS NOT THERE and touches nothing that is —
the same contract as `seed_i9_print_format`, and `standard = "No"` with
`custom_format = 1` is what makes an edit survive. An operator who wants this
app's layout back deletes their copy and migrates again. On a card printer, where
the whole job is a fractional-millimetre argument with one specific piece of
hardware, that difference is the entire point.

EVERY IMAGE ON THE CARD IS A `data:` URI. `render/badge_card.py` argues that at
length and it is the reason a card may carry a photograph at all where the I-9
format refuses one: the hazard wkhtmltopdf presents is the FETCH, not the tag.
"""

from __future__ import annotations

import frappe

from . import compat

PRINT_FORMAT = "Print Format"
BADGE_DOCTYPE = "Bucket Log Badge Map"

#: What the format is called. Named for what it prints rather than for this app,
#: for the reason `i9_print_format.FORMAT_NAME` gives — it appears in a dropdown
#: beside `Standard` and an operator picking one is choosing a layout.
FORMAT_NAME = "Employee Badge Card"

#: ISO/IEC 7810 ID-1, in millimetres. The blank a DTC4250e takes.
CARD_WIDTH_MM = 85.6
CARD_HEIGHT_MM = 54.0

#: The back-of-card symbol, in millimetres: `tools/badges.BADGE_PRINT_INCHES`
#: (1.5") at 25.4mm to the inch. Asserted against that constant in the tests, so
#: a change to the scanning floor cannot leave this behind.
QR_BACK_MM = 38.1

#: The front-of-card symbol. Smaller than the floor above and deliberately so —
#: see the module docstring. It is the glance-distance code; the back is the one
#: sized for a phone.
QR_FRONT_MM = 23.0

#: The Jinja global `render/badge_card.py` registers, spelled here so the
#: template and the hook can be held to the same name by a test. `render/checks.py`
#: explains what a disagreement between those two halves cost the last time.
JINJA_GLOBAL = "erpnext_mcp_badge_card"

#: THE GEOMETRY OF ONE CARD, and the reason it is a constant of its own: two
#: different pages draw this card. This Print Format puts one on a CR-80 page for
#: the printer's hopper, and `badge_sheet.py` puts eight on a sheet of Letter for
#: a batch. A card that measured differently on the two would be a badge whose
#: photograph moved 2mm depending on which button somebody pressed, and nothing
#: would have caught it.
#:
#: Absolute positioning in millimetres throughout, and NOT flexbox: wkhtmltopdf is
#: an old WebKit and lays flex containers out in ways that are fine on screen and a
#: quarter-millimetre out on a card, which is the difference between a photograph
#: inside its frame and one clipped by it. Absolute mm is dull and it measures the
#: same in every renderer this page will meet.
CARD_CSS = """
  .badge-card {
    width: 85.6mm; height: 54mm; box-sizing: border-box; position: relative;
    overflow: hidden; background: #ffffff; color: #111111;
    font-family: Helvetica, Arial, sans-serif; -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }
  .badge-card * { box-sizing: border-box; }
  .bc-abs { position: absolute; }

  /* The branding band. A white card with a ruled band rather than a block of
     colour: an ink-heavy edge is the first thing to band or streak on a
     retransfer card, and it is the part nobody can retouch. */
  .bc-band { left: 0; top: 0; width: 85.6mm; height: 11mm; background: #f4f6f8;
             border-bottom: 0.5mm solid #1f4e79; }
  .bc-logo { left: 3mm; top: 1.6mm; height: 7.8mm; max-width: 17mm; object-fit: contain; }
  .bc-org  { left: 3mm; top: 2.2mm; width: 79mm; font-size: 8.5pt; font-weight: bold;
             letter-spacing: .01em; color: #1f4e79; line-height: 1.15; }
  .bc-org-logo { left: 22mm; width: 60mm; }
  .bc-kind { left: 3mm; top: 7.4mm; font-size: 5.2pt; letter-spacing: .16em;
             text-transform: uppercase; color: #5a6672; }
  .bc-kind-logo { left: 22mm; }

  /* The face. 4:5 portrait, which is the aspect an Employee photograph is
     usually cropped to, so the frame does not decide who gets their chin cut. */
  .bc-photo { left: 3.5mm; top: 13.5mm; width: 20mm; height: 25mm;
              border: 0.3mm solid #c3ccd5; object-fit: cover; background: #ffffff; }
  .bc-initials { left: 3.5mm; top: 13.5mm; width: 20mm; height: 25mm;
                 border: 0.3mm solid #c3ccd5; background: #eef2f6; color: #1f4e79;
                 font-size: 20pt; font-weight: bold; text-align: center;
                 line-height: 25mm; }

  .bc-name { left: 26mm; top: 13.2mm; width: 32mm; font-size: 10pt; font-weight: bold;
             line-height: 1.1; color: #111111; }

  /* WHO THIS PERSON IS, IN FIXED SLOTS RATHER THAN A STACK THAT CLOSES UP. The
     tempting build gives each line the next free millimetre, so a worker with no
     cabin gets a tighter card. It was not built that way: the positions are
     absolute millimetres for the reason the block comment above gives, a
     conditional stack would need the same arithmetic in Python for the sheet and
     in Jinja for this format, and those two would drift. A blank slot is
     whitespace on a card. Two layouts that disagree by 3mm is a badge whose
     photograph moves depending on which button somebody pressed.

     EACH CLIPS RATHER THAN WRAPS. A designation like "Equipment Operator (Class
     II)" or a camp with a long parcel name would otherwise take a second line
     and push into the badge ID, which is the one thing on the front that has to
     stay readable. Clipped is legible; overlapped is not. */
  .bc-line { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bc-role { left: 26mm; top: 23.2mm; width: 32mm; font-size: 6.8pt; color: #5a6672;
             line-height: 1.15; }
  .bc-dept { left: 26mm; top: 26.2mm; width: 32mm; font-size: 5.8pt; color: #5a6672;
             line-height: 1.15; }

  .bc-idlabel { left: 26mm; top: 32.4mm; font-size: 5pt; letter-spacing: .14em;
                text-transform: uppercase; color: #5a6672; }
  .bc-id { left: 26mm; top: 34.6mm; width: 32mm; font-size: 10.5pt; font-weight: bold;
           font-family: "DejaVu Sans Mono", "Courier New", monospace; color: #111111; }

  /* THE DETAIL BAND, AND IT IS THE DEAD SPACE THAT PAID FOR v0.104.0. The
     photograph ends at 38.5mm, the QR caption at 38.5mm and the badge ID at
     39mm; the footer rule is at 47.4mm. That leaves EIGHT MILLIMETRES of full
     card width that had nothing on it, which is where the reporting line and the
     cabin go.

     Full width rather than the 32mm column between the photo and the QR, and
     that is the whole reason these two lines are legible: "Reports to: Rosa
     Ramirez" is twenty-four characters and does not fit a 32mm column at a size
     anybody reads across a bin trailer. Two of them do not fit one line either,
     which is why the branch takes the right half of the first row and the cabin
     takes the whole of the second — a camp and a cabin number is the longest
     string on the card. */
  .bc-reports { left: 3mm; top: 40mm; width: 40mm; font-size: 5.6pt; color: #5a6672;
                line-height: 1.15; }
  .bc-branch { left: 44mm; top: 40mm; width: 38.6mm; text-align: right; font-size: 5.6pt;
               color: #5a6672; line-height: 1.15; }
  .bc-house { left: 3mm; top: 43.2mm; width: 79.6mm; font-size: 5.6pt; color: #5a6672;
              line-height: 1.15; }

  /* The symbol always sits on white, whatever the card behind it looks like —
     `_print_spec` states that as a requirement and this is it honoured. */
  .bc-qr { left: 59.6mm; top: 13.2mm; width: 23mm; height: 23mm; background: #ffffff; }
  .bc-qrcap { left: 59.6mm; top: 36.6mm; width: 23mm; text-align: center; font-size: 5.6pt;
              font-family: "DejaVu Sans Mono", "Courier New", monospace; color: #5a6672; }

  .bc-foot { left: 0; top: 47.4mm; width: 85.6mm; height: 6.6mm;
             border-top: 0.25mm solid #d5dce3; }
  .bc-foot-l { left: 3mm; top: 49.2mm; width: 55mm; font-size: 5.6pt; color: #5a6672; }
  .bc-foot-r { left: 58mm; top: 49.2mm; width: 24.6mm; text-align: right;
               font-size: 5.6pt; color: #5a6672; }

  /* A retired badge that prints as a live one is the exact situation `active`
     exists to prevent, so it is said on the card and not only in the register. */
  .bc-void { left: 0; top: 20mm; width: 85.6mm; text-align: center; font-size: 15pt;
             font-weight: bold; letter-spacing: .3em; color: #b3261e;
             border-top: 0.6mm solid #b3261e; border-bottom: 0.6mm solid #b3261e;
             padding: 1mm 0; background: #ffffff; }

  /* Back of card. */
  .bc-back-qr { left: 23.75mm; top: 3.2mm; width: 38.1mm; height: 38.1mm; background: #ffffff; }
  .bc-back-id { left: 0; top: 41.8mm; width: 85.6mm; text-align: center; font-size: 12pt;
                font-weight: bold; letter-spacing: .06em;
                font-family: "DejaVu Sans Mono", "Courier New", monospace; color: #111111; }
  .bc-back-note { left: 6mm; top: 47.6mm; width: 73.6mm; text-align: center;
                  font-size: 5.4pt; color: #5a6672; line-height: 1.25; }
  .bc-back-name { left: 0; top: 3.2mm; width: 85.6mm; text-align: center; font-size: 7pt;
                  color: #5a6672; }

  .bc-missing { left: 6mm; top: 20mm; width: 73.6mm; text-align: center; font-size: 8pt;
                color: #b3261e; line-height: 1.3; }
"""

#: What turns the card into a PAGE, and it is only this format's business —
#: `badge_sheet.py` sets its own `@page` to Letter and must not inherit this one.
CARD_PAGE_CSS = """
  @page { size: 85.6mm 54mm; margin: 0; }

  @media print {
    /* Frappe's print view wraps the format in preview chrome — a grey gutter, a
       drop shadow, a letterhead slot, a toolbar. On a sheet of Letter that is
       invisible padding; on a card it is the reason the artwork comes out 4mm
       down and to the right of where it was drawn. */
    html, body { width: 85.6mm; margin: 0 !important; padding: 0 !important;
                 background: #ffffff !important; }
    .print-preview, .print-format-gutter, .page-break, #page-break {
      margin: 0 !important; padding: 0 !important; box-shadow: none !important;
      background: #ffffff !important; border: 0 !important;
    }
    .print-format {
      margin: 0 !important; padding: 0 !important; width: 85.6mm !important;
      min-height: 0 !important; box-shadow: none !important; border: 0 !important;
    }
    .print-toolbar, .navbar, .no-print, .letter-head, .print-heading,
    .footer, .page-footer { display: none !important; }
    /* One card to a page, and the LAST one does not force a blank sheet after it. */
    .badge-card { page-break-after: always; break-after: page; }
    .badge-card:last-child { page-break-after: auto; break-after: auto; }
  }
"""

#: The template Frappe renders. THE JINJA GLOBAL IS GUARDED WITH `is defined`, the
#: same belt-and-brace `printing.CHECK_TEMPLATE` wears: a site whose hook has not
#: loaded prints a card with the badge ID and the register's own fields on it
#: instead of a traceback where the Print button was.
#: `CARD_PAGE_CSS` comes AFTER `CARD_CSS` because the `@media print` block in it
#: overrides the base rules, and a stylesheet that stated the override first would
#: lose the argument at the moment it mattered.
CARD_MARKUP = """
{%- if erpnext_mcp_badge_card is defined -%}
  {%- set card = erpnext_mcp_badge_card(doc.badge_id) -%}
{%- else -%}
  {%- set card = {"ok": False, "badge_id": doc.badge_id, "active": doc.active,
                  "employee": doc.employee, "employee_name": doc.employee,
                  "company_name": doc.company, "designation": "", "employee_number": "",
                  "department": "", "branch": "", "reports_to_name": "", "housing": "",
                  "photo": "", "logo": "", "initials": "?", "qr": ""} -%}
{%- endif -%}

<!-- FRONT -->
<div class="badge-card">
  <div class="bc-abs bc-band"></div>
  {%- if card.logo %}
  <img class="bc-abs bc-logo" src="{{ card.logo }}" alt="">
  {%- endif %}
  <div class="bc-abs bc-org {% if card.logo %}bc-org-logo{% endif %}">
    {{ card.company_name or doc.company or "" }}
  </div>
  <div class="bc-abs bc-kind {% if card.logo %}bc-kind-logo{% endif %}">Employee Badge</div>

  {%- if card.photo %}
  <img class="bc-abs bc-photo" src="{{ card.photo }}" alt="">
  {%- else %}
  <div class="bc-abs bc-initials">{{ card.initials or "?" }}</div>
  {%- endif %}

  <div class="bc-abs bc-name">{{ card.employee_name or doc.employee or "" }}</div>
  {%- if card.designation %}
  <div class="bc-abs bc-line bc-role">{{ card.designation }}</div>
  {%- endif %}
  {%- if card.department %}
  <div class="bc-abs bc-line bc-dept">{{ card.department }}</div>
  {%- endif %}
  <div class="bc-abs bc-idlabel">Badge</div>
  <div class="bc-abs bc-id">{{ card.badge_id or doc.badge_id or "" }}</div>

  {%- if card.qr %}
  <img class="bc-abs bc-qr" src="{{ card.qr }}" alt="">
  <div class="bc-abs bc-qrcap">{{ card.badge_id or doc.badge_id or "" }}</div>
  {%- endif %}

  {%- if card.reports_to_name %}
  <div class="bc-abs bc-line bc-reports">Reports to: {{ card.reports_to_name }}</div>
  {%- endif %}
  {%- if card.branch %}
  <div class="bc-abs bc-line bc-branch">{{ card.branch }}</div>
  {%- endif %}
  {%- if card.housing %}
  <div class="bc-abs bc-line bc-house">Camp: {{ card.housing }}</div>
  {%- endif %}

  <div class="bc-abs bc-foot"></div>
  <div class="bc-abs bc-foot-l">
    {%- if card.employee_number %}No. {{ card.employee_number }}{% endif -%}
  </div>
  <div class="bc-abs bc-foot-r">{{ card.employee or doc.employee or "" }}</div>

  {%- if not card.active %}
  <div class="bc-abs bc-void">RETIRED</div>
  {%- endif %}
</div>

<!-- BACK. The 1.5" symbol tools/badges asks for, which the front cannot seat. -->
<div class="badge-card">
  <div class="bc-abs bc-back-name">{{ card.company_name or doc.company or "" }}</div>
  {%- if card.qr %}
  <img class="bc-abs bc-back-qr" src="{{ card.qr }}" alt="">
  {%- else %}
  <div class="bc-abs bc-missing">
    No QR could be drawn for this badge. Install <b>segno</b> on the bench and print again —
    the badge ID below is what a scanner resolves either way.
  </div>
  {%- endif %}
  <div class="bc-abs bc-back-id">{{ card.badge_id or doc.badge_id or "" }}</div>
  <div class="bc-abs bc-back-note">
    Property of {{ card.company_name or doc.company or "this employer" }}. If found, please
    return it. This card records piece work and is not proof of identity.
  </div>
</div>
"""

#: What the Print Format record actually holds.
#: The layout this format shipped with until v0.213.0. Kept so the redirect
#: patch can tell an untouched format from one an operator edited, and because
#: `badge_sheet` still draws it for a site that sets `legacy_badge_layouts`.
LEGACY_TEMPLATE = "\n<style>" + CARD_CSS + CARD_PAGE_CSS + "</style>\n" + CARD_MARKUP
BADGE_TEMPLATE = LEGACY_TEMPLATE


def _page_size_fields() -> dict:
	"""`page_size` (and the millimetres) this site's Print Format will accept.

	A Print Format's `page_size` is a Select, and Frappe validates a Select
	against its options on insert — so writing "Custom" to a site whose field
	does not offer it does not produce a slightly wrong page, it produces NO
	FORMAT AT ALL. The field's own options are asked instead of assumed.

	THE FALLBACK COSTS THE PDF BUTTON AND NOT THE PRINTER. `@page` in the
	template governs the browser's print dialogue, which is how a card printer is
	actually driven; `page_size` governs wkhtmltopdf. A site that lands on Letter
	here still prints a correct card and gets a card-sized image on a Letter PDF.
	"""
	meta = compat.field_meta(PRINT_FORMAT, "page_size")
	options = [line.strip() for line in str(getattr(meta, "options", "") or "").splitlines()]
	if "Custom" in options:
		return {"page_size": "Custom", "page_width": CARD_WIDTH_MM, "page_height": CARD_HEIGHT_MM}
	return {"page_size": "Letter"}


def print_format_fields() -> dict:
	"""Everything this app sets on the badge Print Format.

	`standard = "No"` with `custom_format = 1` is what makes an operator's edits
	survive `bench migrate` — see the module docstring, and `i9_print_format`,
	which makes the same choice for the same reason.

	Margins are ZERO, which is the one place this differs from every other format
	this app ships: the card is the page.
	"""
	fields = {
		"doctype": PRINT_FORMAT,
		"name": FORMAT_NAME,
		"doc_type": BADGE_DOCTYPE,
		"module": "ERPNext MCP",
		"standard": "No",
		"custom_format": 1,
		"print_format_type": "Jinja",
		"print_format_builder": 0,
		"disabled": 0,
		# A letterhead is a band across the top of a sheet of Letter. On a card it
		# is the top third of the badge.
		"default_print_language": None,
		"margin_top": 0,
		"margin_bottom": 0,
		"margin_left": 0,
		"margin_right": 0,
		# v0.213.0. A new site gets the CR80 card on this format too; see
		# `card_redirect`. `BADGE_TEMPLATE` is the layout it replaced.
		"html": _cr80_template(),
	}
	fields.update(_page_size_fields())
	return {key: value for key, value in fields.items() if value is not None}


def _cr80_template() -> str:
	from . import card_redirect

	return card_redirect.badge_format_template()


def seed_badge_print_format() -> dict:
	"""Create the badge Print Format if this site has not got one. Never raises.

	IT ONLY EVER CREATES WHAT IS NOT THERE, checked by name, exactly like
	`seed_i9_print_format`. A site whose operator nudged the layout for their own
	printer keeps the nudge through every future migrate; a site that deleted it
	gets it back.

	Returns `{"created": bool, "name": str, "reason": str}` so `install.py` can
	print one line about what happened rather than being silent either way.
	"""
	report = {"created": False, "name": FORMAT_NAME, "reason": ""}
	try:
		if not frappe.db.exists("DocType", BADGE_DOCTYPE):
			report["reason"] = "the Bucket Log Badge Map doctype has not migrated yet"
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
		report["name"] = doc.name
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		report["reason"] = f"{type(exc).__name__}: {exc}"
	return report


__all__ = (
	"BADGE_TEMPLATE",
	"CARD_HEIGHT_MM",
	"CARD_WIDTH_MM",
	"FORMAT_NAME",
	"JINJA_GLOBAL",
	"QR_BACK_MM",
	"QR_FRONT_MM",
	"print_format_fields",
	"seed_badge_print_format",
)
