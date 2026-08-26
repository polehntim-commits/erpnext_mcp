# SPDX-License-Identifier: MIT
"""The Desk's Print button, pointed at something that looks like Form I-9.

v0.47.1. `render_i9_pdf` fills the government's own fillable PDF and is the
right answer whenever pypdf and the shipped template are both present. THIS IS
THE OTHER PATH, and it is not a lesser version of that one — it is the path a
Desk user takes. Frappe's Print button renders a Print Format through Jinja and
then wkhtmltopdf, and with no format defined it renders the standard one: every
field on the doctype, in the order the JSON declares them, two to a row, section
break headings and all. On an I-9 that is eighty-four fields including
`naming_series`, `pdf_col` and `signed_pdf_on` — a field dump that happens to
contain an I-9 rather than a form.

WHAT THIS PRODUCES is the record laid out the way USCIS lays the form out:
Section 1 with the citizenship attestation as ticked boxes, Section 2's three
document lists side by side in the columns they belong in, Supplement B as a
table of every reverification rather than the first three, and the retention
dates. It is a WORKING COPY and says so at the top of the page, because the
thing it most resembles is a federal form and the one thing it is not is one.

────────────────────────────────────────────────────────────────────────────
IT DOES NOT REFERENCE A SINGLE EXTERNAL RESOURCE, AND THAT IS THE POINT
────────────────────────────────────────────────────────────────────────────

wkhtmltopdf in a container fails in two ways that both look like a broken Print
button and have nothing to do with the format:

  * **fontconfig.** It wants a writable `/var/cache/fontconfig` and gets a
    read-only layer, so it dies before rendering anything. That is fixed in the
    image — `fafo-erpnext/build/Dockerfile` creates the directory and opens its
    permissions — and it is fixed there because no template can work around it.
  * **A broken `<img>`.** wkhtmltopdf fetches every external URL synchronously,
    with no timeout worth the name. One image that 404s — a letterhead, a signature
    capture whose private File URL the renderer cannot authenticate to — hangs
    the render or blanks the page.

So: no `<img>`, no `<link>`, no webfont, no logo. Every rule and every tick box
below is a border or a text glyph, the CSS is inline in one `<style>`, and the
whole thing renders identically with the network unplugged. THE SIGNATURE
CAPTURES ARE DELIBERATELY NOT EMBEDDED — they are private Files, the renderer
would have to authenticate to fetch them, and the page says whether one is on
file instead, which is the fact rather than the picture.

────────────────────────────────────────────────────────────────────────────
SEEDED, NOT FIXTURED, AND CUSTOM RATHER THAN STANDARD
────────────────────────────────────────────────────────────────────────────

`test_hooks.py` forbids the word `fixtures` by name and `i9_documents.py` states
the reason: a fixture is rewritten from the app's files on every `bench migrate`,
so an operator who adjusted a margin for their printer would lose it at the next
upgrade without a word. `seed_i9_print_format` CREATES WHAT IS NOT THERE and
touches nothing that is. An operator who wants this app's layout back after
editing it deletes their copy and migrates again.

`standard = "No"` for the same reason, which is the same judgement
`tools/printing.py` makes about the check format and states at length.
"""

from __future__ import annotations

import frappe

PRINT_FORMAT = "Print Format"
I9_FORM = "I-9 Form"

#: What the format is called. Named for what it prints rather than for this app,
#: because it appears in a dropdown beside `Standard` and an operator picking one
#: is choosing a layout, not a vendor.
FORMAT_NAME = "I-9 Form — Working Copy"

#: US Letter. Form I-9 is a US federal form and is filed on Letter; an A4 option
#: would be an option to print something that does not match the form it copies.
PAGE_SIZE = "Letter"

#: The line at the head of every page, and the shortest true statement about what
#: the page is. It is NOT the same claim `form_pdf_renderer` makes about a W-2 —
#: an I-9 is never filed with anybody, so there is no filing channel to name —
#: but it is the same discipline: say what this is where somebody will read it.
HEADER_NOTE = (
	"WORKING COPY — not the federal form. This page lays out an I-9 Form record held in "
	"this system. Form I-9 itself is the USCIS document (OMB No. 1615-0047); render it with "
	"render_i9_pdf, print it, and have it signed. This page is for review and for the file."
)

#: The Jinja template. One string beside the code that writes it, for the reason
#: `tools/printing.CHECK_TEMPLATE` gives: it is version-controlled with the module
#: and a site that edited its own copy keeps the edit, because this only ever
#: creates a format that is not there.
#:
#: `doc` is the I-9 Form. Every value goes through `or ""` rather than through a
#: default filter, because a Frappe Date that was never set is `None` and a
#: template that printed `None` in a date box would be worse than one that
#: printed nothing.
#:
#: THE CHILD ROWS ARE READ WITH `.get()` AND THE PARENT WITH ATTRIBUTES, and the
#: asymmetry is deliberate. `doc` is loaded through the doctype's meta, so every
#: declared column is on it whether or not anybody filled it in. A row in
#: `reverifications` is whatever was appended — Frappe fills a child Document
#: out too, but a row that reached the table by any other route need not be —
#: and one missing key would take the whole page down under somebody's finger
#: rather than leave one cell blank. `.get()` works on a Frappe Document and on
#: a plain dict alike.
I9_TEMPLATE = """
<style>
  .i9 { font-family: Arial, Helvetica, sans-serif; color: #000; font-size: 9pt; }
  .i9 h1 { font-size: 15pt; margin: 0 0 2pt 0; }
  .i9 .sub { font-size: 8.5pt; margin: 0 0 8pt 0; }
  .i9 .note { border: 1.5pt solid #000; padding: 5pt 7pt; font-size: 8pt; margin-bottom: 10pt; }
  .i9 h2 { font-size: 10pt; background: #e8e8e8; border: 0.75pt solid #000;
           margin: 12pt 0 0 0; padding: 3pt 5pt; }
  .i9 table { width: 100%; border-collapse: collapse; table-layout: fixed; }
  .i9 td, .i9 th { border: 0.5pt solid #000; padding: 3pt 5pt; vertical-align: top;
                   word-wrap: break-word; }
  .i9 th { background: #f4f4f4; font-size: 8pt; text-align: left; }
  .i9 .lbl { display: block; font-size: 6.5pt; text-transform: uppercase;
             letter-spacing: .04em; color: #333; }
  .i9 .val { font-size: 10pt; min-height: 12pt; }
  .i9 .tick { font-family: "DejaVu Sans", Arial, sans-serif; font-size: 10pt; }
  .i9 .flag { border: 1pt solid #000; padding: 4pt 6pt; margin-top: 8pt; font-size: 8.5pt; }
  .i9 .foot { margin-top: 12pt; font-size: 7.5pt; color: #333;
              border-top: 0.75pt solid #000; padding-top: 4pt; }
  .i9 .empty { color: #777; font-style: italic; font-size: 8pt; }
</style>

<div class="i9">
  <h1>Employment Eligibility Verification — Form I-9</h1>
  <p class="sub">
    Department of Homeland Security &middot; U.S. Citizenship and Immigration Services
    &middot; OMB No. 1615-0047
  </p>

  <div class="note">{{ header_note }}</div>

  <table>
    <tr>
      <td style="width:34%"><span class="lbl">Record</span>
        <span class="val">{{ doc.name }}</span></td>
      <td style="width:33%"><span class="lbl">Employee</span>
        <span class="val">{{ doc.employee_name or doc.employee or "" }}</span></td>
      <td style="width:33%"><span class="lbl">Status</span>
        <span class="val">{{ doc.status or "" }}</span></td>
    </tr>
    <tr>
      <td><span class="lbl">Hiring entity</span>
        <span class="val">{{ doc.company or "" }}</span></td>
      <td><span class="lbl">First day of employment</span>
        <span class="val">{{ doc.hire_date or "" }}</span></td>
      <td><span class="lbl">Employee record</span>
        <span class="val">{{ doc.employee or "" }}</span></td>
    </tr>
  </table>

  <h2>Section 1. Employee Information and Attestation</h2>
  <table>
    <tr>
      <td style="width:25%"><span class="lbl">Last name (family name)</span>
        <span class="val">{{ doc.legal_last_name or "" }}</span></td>
      <td style="width:25%"><span class="lbl">First name (given name)</span>
        <span class="val">{{ doc.legal_first_name or "" }}</span></td>
      <td style="width:22%"><span class="lbl">Middle name</span>
        <span class="val">{{ doc.legal_middle_name or "" }}</span></td>
      <td style="width:28%"><span class="lbl">Other last names used</span>
        <span class="val">{{ doc.other_last_names or "" }}</span></td>
    </tr>
    <tr>
      <td colspan="2"><span class="lbl">Address (street number and name)</span>
        <span class="val">{{ doc.address_street or "" }}</span></td>
      <td><span class="lbl">City or town</span>
        <span class="val">{{ doc.address_city or "" }}</span></td>
      <td><span class="lbl">State / ZIP</span>
        <span class="val">{{ doc.address_state or "" }} {{ doc.address_zip or "" }}</span></td>
    </tr>
    <tr>
      <td><span class="lbl">Date of birth</span>
        <span class="val">{{ doc.date_of_birth or "" }}</span></td>
      <td><span class="lbl">U.S. Social Security number</span>
        <span class="val">{% if doc.ssn_last_four %}XXX-XX-{{ doc.ssn_last_four }}{% endif %}</span></td>
      <td><span class="lbl">Email address</span>
        <span class="val">{{ doc.email or "" }}</span></td>
      <td><span class="lbl">Telephone number</span>
        <span class="val">{{ doc.phone or "" }}</span></td>
    </tr>
  </table>

  <table style="margin-top:6pt">
    <tr>
      <th colspan="2">Citizenship or immigration status attested to</th>
    </tr>
    <tr>
      <td style="width:50%">
        <div class="tick">
          [{% if doc.citizenship_status == "US Citizen" %}X{% else %}&nbsp;{% endif %}]
          1. A citizen of the United States
        </div>
        <div class="tick">
          [{% if doc.citizenship_status == "Noncitizen National" %}X{% else %}&nbsp;{% endif %}]
          2. A noncitizen national of the United States
        </div>
        <div class="tick">
          [{% if doc.citizenship_status == "Lawful Permanent Resident" %}X{% else %}&nbsp;{% endif %}]
          3. A lawful permanent resident
        </div>
        <div class="tick">
          [{% if doc.citizenship_status == "Alien Authorized to Work" %}X{% else %}&nbsp;{% endif %}]
          4. An alien authorized to work
          {% if doc.alien_work_authorization_expiry %}
            until {{ doc.alien_work_authorization_expiry }}
          {% endif %}
        </div>
      </td>
      <td style="width:50%">
        <span class="lbl">USCIS / A-Number</span>
        <span class="val">{{ doc.alien_registration_number or "" }}</span>
        <span class="lbl" style="margin-top:4pt">Form I-94 admission number</span>
        <span class="val">{{ doc.i94_admission_number or "" }}</span>
        <span class="lbl" style="margin-top:4pt">Foreign passport number / country of issuance</span>
        <span class="val">
          {{ doc.foreign_passport_number or "" }}
          {%- if doc.foreign_passport_country %} / {{ doc.foreign_passport_country }}{% endif %}
        </span>
      </td>
    </tr>
  </table>

  <table style="margin-top:6pt">
    <tr>
      <td style="width:50%"><span class="lbl">Section 1 attested</span>
        <span class="val">
          {%- if doc.section_1_signed_at %}{{ doc.section_1_signed_at }}
            {%- if doc.section_1_signed_ip %} from {{ doc.section_1_signed_ip }}{% endif %}
            {%- if doc.section_1_signed_gps %} at {{ doc.section_1_signed_gps }}{% endif %}
          {%- else %}<span class="empty">not yet attested</span>{% endif %}
        </span></td>
      <td style="width:50%"><span class="lbl">Signature capture on file</span>
        <span class="val">{% if doc.section_1_signature %}yes{% else %}no{% endif %}</span></td>
    </tr>
    {% if doc.preparer_used %}
    <tr>
      <td><span class="lbl">Preparer / translator</span>
        <span class="val">{{ doc.preparer_name or "" }}</span></td>
      <td><span class="lbl">Preparer address</span>
        <span class="val">{{ doc.preparer_address or "" }}</span></td>
    </tr>
    {% endif %}
  </table>

  <h2>Section 2. Employer Review and Verification</h2>
  <table>
    <tr>
      <th style="width:34%">List A — identity and work authorization</th>
      <th style="width:33%">List B — identity</th>
      <th style="width:33%">List C — work authorization</th>
    </tr>
    <tr>
      <td>
        <span class="lbl">Document title</span>
        <span class="val">{{ doc.list_a_doc_title or "" }}</span>
        <span class="lbl" style="margin-top:4pt">Issuing authority</span>
        <span class="val">{{ doc.list_a_doc_authority or "" }}</span>
        <span class="lbl" style="margin-top:4pt">Document number</span>
        <span class="val">{{ doc.list_a_doc_number or "" }}</span>
        <span class="lbl" style="margin-top:4pt">Expiration date</span>
        <span class="val">{{ doc.list_a_doc_expiry or "" }}</span>
        {% if doc.list_a_is_receipt %}<div class="tick">[X] receipt, not the document</div>{% endif %}
      </td>
      <td>
        <span class="lbl">Document title</span>
        <span class="val">{{ doc.list_b_doc_title or "" }}</span>
        <span class="lbl" style="margin-top:4pt">Issuing authority</span>
        <span class="val">{{ doc.list_b_doc_authority or "" }}</span>
        <span class="lbl" style="margin-top:4pt">Document number</span>
        <span class="val">{{ doc.list_b_doc_number or "" }}</span>
        <span class="lbl" style="margin-top:4pt">Expiration date</span>
        <span class="val">{{ doc.list_b_doc_expiry or "" }}</span>
        {% if doc.list_b_is_receipt %}<div class="tick">[X] receipt, not the document</div>{% endif %}
      </td>
      <td>
        <span class="lbl">Document title</span>
        <span class="val">{{ doc.list_c_doc_title or "" }}</span>
        <span class="lbl" style="margin-top:4pt">Issuing authority</span>
        <span class="val">{{ doc.list_c_doc_authority or "" }}</span>
        <span class="lbl" style="margin-top:4pt">Document number</span>
        <span class="val">{{ doc.list_c_doc_number or "" }}</span>
        <span class="lbl" style="margin-top:4pt">Expiration date</span>
        <span class="val">{{ doc.list_c_doc_expiry or "" }}</span>
        {% if doc.list_c_is_receipt %}<div class="tick">[X] receipt, not the document</div>{% endif %}
      </td>
    </tr>
  </table>

  {% if doc.receipt_pending %}
  <div class="flag">
    <strong>RECEIPT PENDING</strong> under 8 CFR 274a.2(b)(1)(vi). The employee presented a
    receipt for a replacement document and may work while it is outstanding.
    {% if doc.receipt_expires_on %}
      The document itself is due by <strong>{{ doc.receipt_expires_on }}</strong>.
    {% endif %}
    Record it with reverify_i9 when it arrives.
  </div>
  {% endif %}

  <table style="margin-top:6pt">
    <tr>
      <td style="width:25%"><span class="lbl">Documents examined by</span>
        <span class="val">{{ doc.verifier_name or "" }}</span></td>
      <td style="width:25%"><span class="lbl">Title</span>
        <span class="val">{{ doc.verifier_title or "" }}</span></td>
      <td style="width:25%"><span class="lbl">Verification date</span>
        <span class="val">{{ doc.verification_date or "" }}</span></td>
      <td style="width:25%"><span class="lbl">Section 2 attested</span>
        <span class="val">
          {%- if doc.section_2_signed_at %}{{ doc.section_2_signed_at }}
            {%- if doc.section_2_signed_ip %} from {{ doc.section_2_signed_ip }}{% endif %}
            {%- if doc.section_2_signed_gps %} at {{ doc.section_2_signed_gps }}{% endif %}
          {%- else %}<span class="empty">not yet attested</span>{% endif %}
        </span></td>
    </tr>
    <tr>
      <td><span class="lbl">Document path</span>
        <span class="val">{{ doc.document_path or "" }}</span></td>
      <td><span class="lbl">Copies of documents stored</span>
        <span class="val">
          {%- set copies = [] %}
          {%- if doc.list_a_doc_copy %}{% set _ = copies.append("List A") %}{% endif %}
          {%- if doc.list_b_doc_copy %}{% set _ = copies.append("List B") %}{% endif %}
          {%- if doc.list_c_doc_copy %}{% set _ = copies.append("List C") %}{% endif %}
          {%- if copies %}{{ copies | join(", ") }} on file
          {%- elif doc.document_copies_stored %}yes
          {%- else %}no{% endif %}
        </span></td>
      <td><span class="lbl">Signature capture on file</span>
        <span class="val">{% if doc.section_2_signature %}yes{% else %}no{% endif %}</span></td>
      <td><span class="lbl">Signed federal copy filed</span>
        <span class="val">
          {%- if doc.signed_pdf %}{{ doc.signed_pdf_on or "yes" }}
          {%- else %}<span class="empty">not filed</span>{% endif %}
        </span></td>
    </tr>
  </table>

  <h2>Supplement B. Reverification and Rehire</h2>
  {% if doc.reverifications %}
  <table>
    <tr>
      <th style="width:12%">Date</th>
      <th style="width:18%">Reason</th>
      <th style="width:11%">Rehire date</th>
      <th style="width:23%">Document title</th>
      <th style="width:14%">Number</th>
      <th style="width:11%">Expires</th>
      <th style="width:11%">Verifier</th>
    </tr>
    {% for row in doc.reverifications %}
    <tr>
      <td>{{ row.get("reverification_date") or "" }}</td>
      <td>{{ row.get("reason") or "" }}</td>
      <td>{{ row.get("rehire_date") or "" }}</td>
      <td>{{ row.get("document_title") or "" }}</td>
      <td>{{ row.get("document_number") or "" }}</td>
      <td>{{ row.get("document_expiry") or "" }}</td>
      <td>{{ row.get("verifier_name") or "" }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="empty">No reverification or rehire has been recorded against this form.</p>
  {% endif %}

  <h2>Retention</h2>
  <table>
    <tr>
      <td style="width:25%"><span class="lbl">Retain until</span>
        <span class="val">{{ doc.retention_until or "" }}</span></td>
      <td style="width:25%"><span class="lbl">Eligible for destruction</span>
        <span class="val">{{ doc.destruction_eligible_date or "" }}</span></td>
      <td style="width:25%"><span class="lbl">Destroyed</span>
        <span class="val">
          {%- if doc.destroyed_at %}{{ doc.destroyed_at }}
          {%- else %}<span class="empty">not destroyed</span>{% endif %}
        </span></td>
      <td style="width:25%"><span class="lbl">Federal PDF rendered</span>
        <span class="val">
          {%- if doc.generated_pdf %}{{ doc.generated_pdf_on or "yes" }}
          {%- else %}<span class="empty">not rendered</span>{% endif %}
        </span></td>
    </tr>
  </table>

  <div class="foot">
    Retention rule: the later of three years from the first day of employment and one year
    from the last. Every read and every write against this record is on the I-9 Audit Log —
    get_i9_audit_log has the trail. This page carries no signature: the signatures are on the
    printed federal form, which render_i9_pdf produces.
  </div>
</div>
"""


def print_format_fields() -> dict:
	"""Everything this app sets on the I-9 Print Format.

	`standard = "No"` and `custom_format = 1` together are what make an
	operator's edits survive `bench migrate` — see the module docstring. Margins
	are ERPNext's own defaults rather than zero: unlike a check, this page is
	printed on plain paper and wants a margin to punch holes in.
	"""
	return {
		"doctype": PRINT_FORMAT,
		"name": FORMAT_NAME,
		"doc_type": I9_FORM,
		"module": "ERPNext MCP",
		"standard": "No",
		"custom_format": 1,
		"print_format_type": "Jinja",
		"print_format_builder": 0,
		"disabled": 0,
		"page_size": PAGE_SIZE,
		"html": I9_TEMPLATE.replace("{{ header_note }}", HEADER_NOTE),
	}


def seed_i9_print_format() -> dict:
	"""Create the I-9 Print Format if this site has not got one. Never raises.

	IT ONLY EVER CREATES WHAT IS NOT THERE, checked by name, exactly like
	`seed_i9_document_types`. A site whose operator edited the layout keeps the
	edit through every future migrate; a site that deleted it gets it back.

	Returns `{"created": bool, "name": str, "reason": str}` so `install.py` can
	print one line about what happened rather than being silent either way.
	"""
	report = {"created": False, "name": FORMAT_NAME, "reason": ""}
	try:
		if not frappe.db.exists("DocType", I9_FORM):
			report["reason"] = "the I-9 Form doctype has not migrated yet"
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


__all__ = ("FORMAT_NAME", "HEADER_NOTE", "I9_TEMPLATE", "print_format_fields", "seed_i9_print_format")
