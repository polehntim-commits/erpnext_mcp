# SPDX-License-Identifier: MIT
"""Company email branding: footer, logo, no unsubscribe link on transactional mail. v0.216.1.

THREE SUPPORTED FRAPPE SETTINGS, NO CORE PATCH.

* **Unsubscribe.** "Leave this conversation" is added by
  `Communication.get_unsubscribe_message` only when the outgoing Email Account
  has **Send unsubscribe message in email** (`send_unsubscribe_message`, default
  ON). Turning it off removes it from Communication replies, notifications and
  document emails. A Newsletter (and so an Email Group mailing) keeps its link
  whatever the setting says — `QueueBuilder.should_include_unsubscribe_link`
  always includes it for `reference_doctype == "Newsletter"` — which is the
  CAN-SPAM line: unsubscribe on bulk, not on an invoice.
* **Footer.** `email_body.get_footer` renders the Email Account's own
  **Footer Content** (`footer`) on every email that account sends, then the
  standard "Sent via ERPNext" line unless System Settings → **Disable Standard
  Email Footer** is ticked. This writes a company footer into `footer`; the
  standard line is Tim's to switch off.
* **Logo.** An email is read outside the tailnet. A relative image URL is made
  absolute on the site's own address (`http://100.69.162.122/...`), which no
  recipient can reach — the broken-image icon. The footer's logo, and the
  account's **Brand Logo** when it is empty, point at
  `<farmops_public_url>/farmops/api/brand/<company>`: the one public path, an
  image and nothing else (`farmops_api.app._brand_image`). With no Farm Ops
  Public URL the footer goes out without a logo rather than with a broken one.
"""

from __future__ import annotations

import html
import re
from urllib.parse import quote

import frappe

from . import compat, settings

ACCOUNT = "Email Account"
MARK_START = "<!-- erpnext_mcp:company-footer -->"
MARK_END = "<!-- /erpnext_mcp:company-footer -->"
BRAND_PATH = "/farmops/api/brand/"

_IMG_SRC = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*([\"'])(.*?)\1", re.I | re.S)


def brand_url(company: str) -> str:
	"""The public URL of a company's logo, or "" when there is no public address."""
	base = settings.farmops_public_url()
	if not base or not company:
		return ""
	return f"{base}{BRAND_PATH}{quote(company, safe='')}"


def company_facts(company: str) -> dict:
	"""Name, address lines and phone for the footer. Never raises."""
	facts = {"name": company, "address": [], "phone": ""}
	try:
		row = frappe.db.get_value(
			"Company",
			company,
			compat.existing_fields("Company", ("company_name", "phone_no")),
			as_dict=True,
		)
		if row:
			facts["name"] = row.get("company_name") or company
			facts["phone"] = str(row.get("phone_no") or "")
	except Exception:  # pragma: no cover
		pass
	try:
		parents = frappe.db.get_all(
			"Dynamic Link",
			filters={"link_doctype": "Company", "link_name": company, "parenttype": "Address"},
			pluck="parent",
			limit=10,
		)
		for name in parents or []:
			address = frappe.db.get_value(
				"Address",
				name,
				[
					"address_line1",
					"address_line2",
					"city",
					"state",
					"pincode",
					"country",
					"is_your_company_address",
					"disabled",
				],
				as_dict=True,
			)
			if not address or int(address.get("disabled") or 0):
				continue
			line2 = ", ".join(
				part
				for part in (
					address.get("city"),
					" ".join(p for p in (address.get("state"), address.get("pincode")) if p),
				)
				if part
			)
			facts["address"] = [
				line for line in (address.get("address_line1"), address.get("address_line2"), line2) if line
			]
			if int(address.get("is_your_company_address") or 0):
				break
	except Exception:  # pragma: no cover - a site with no Address table
		pass
	return facts


def footer_html(company: str, email: str, phone: str = "") -> str:
	"""The footer, between markers so a re-apply replaces only its own part."""
	facts = company_facts(company)
	logo = brand_url(company)
	esc = html.escape
	phone = phone or facts["phone"]
	contact = (
		f'<a href="mailto:{esc(email)}" style="color:#1d5d38;text-decoration:none">{esc(email)}</a>'
		if email
		else ""
	)
	if phone:
		contact = f"{contact} &middot; {esc(phone)}" if contact else esc(phone)
	lines = [f'<strong style="color:#222">{esc(facts["name"])}</strong>']
	lines += [esc(line) for line in facts["address"]]
	if contact:
		lines.append(contact)
	logo_cell = (
		f'<td style="padding:0 14px 0 0;vertical-align:top">'
		f'<img src="{esc(logo)}" alt="{esc(facts["name"])}" width="64" '
		f'style="display:block;border:0;width:64px;max-width:64px;height:auto"></td>'
		if logo
		else ""
	)
	return (
		f"{MARK_START}"
		'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
		'style="margin-top:24px;border-top:1px solid #e3e3e3;padding-top:12px;'
		'font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.45;color:#555">'
		f'<tr>{logo_cell}<td style="vertical-align:top">{"<br>".join(lines)}</td></tr></table>'
		f"{MARK_END}"
	)


def merge_footer(existing: str, ours: str) -> tuple[str, str]:
	"""(new footer, what happened). Replaces our own block; keeps anything else."""
	existing = str(existing or "")
	if MARK_START in existing and MARK_END in existing:
		before, rest = existing.split(MARK_START, 1)
		_old, after = rest.split(MARK_END, 1)
		return before + ours + after, "replaced"
	if existing.strip():
		return existing.rstrip() + ours, "appended"
	return ours, "set"


def default_company() -> str:
	try:
		return str(frappe.defaults.get_global_default("company") or "")
	except Exception:  # pragma: no cover
		return ""


def image_audit(fragment: str) -> list:
	"""Every <img src> in an email fragment and whether a recipient can load it."""
	public = settings.farmops_public_url()
	out = []
	for _quote, src in _IMG_SRC.findall(fragment or ""):
		src = src.strip()
		if src.startswith("cid:"):
			verdict = "inline"
		elif public and src.startswith(public + "/"):
			verdict = "public"
		elif src.startswith("https://") and not _tailnet(src):
			verdict = "external"
		else:
			verdict = "unreachable"
		out.append({"src": src[:200], "verdict": verdict})
	return out


def _tailnet(url: str) -> bool:
	host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
	if host.startswith("100.") or host.endswith(".local") or host in ("localhost", "127.0.0.1"):
		return True
	return False


def unsubscribe_on(row: dict) -> bool:
	"""`send_unsubscribe_message` as Frappe reads it: absent means its default, ON."""
	value = row.get("send_unsubscribe_message")
	return True if value is None else bool(int(value or 0))


def account_status(row: dict) -> dict:
	"""Branding facts and warnings for one outgoing account (status block)."""
	footer = str(row.get("footer") or "")
	brand = str(row.get("brand_logo") or "")
	images = image_audit(footer) + (image_audit(f'<img src="{brand}">') if brand else [])
	unsubscribe = unsubscribe_on(row)
	warnings = []
	if unsubscribe:
		warnings.append(
			"'Send unsubscribe message in email' is ON: document and notification emails carry "
			"'Leave this conversation'. apply_email_branding turns it off (newsletters keep theirs)."
		)
	if MARK_START not in footer:
		warnings.append("No company footer on this account — apply_email_branding adds one.")
	for image in images:
		if image["verdict"] == "unreachable":
			warnings.append(f"An image a recipient cannot load: {image['src']}")
	return {
		"send_unsubscribe_message": unsubscribe,
		"company_footer": MARK_START in footer,
		"images": images,
		"warnings": warnings,
	}


def standard_footer_disabled() -> bool:
	try:
		return bool(int(frappe.db.get_single_value("System Settings", "disable_standard_email_footer") or 0))
	except Exception:  # pragma: no cover
		return False
