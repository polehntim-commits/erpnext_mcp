# SPDX-License-Identifier: MIT
"""apply_email_branding — a company footer and no unsubscribe link on one Email Account. v0.216.1.

MUTATING, default OFF. See `erpnext_mcp/email_branding.py` for what each of the
three Frappe settings does. Writes only the Email Account it names:
`footer` (its own marked block; anything else in the field is kept),
`brand_logo` (only when empty or already ours), and
`send_unsubscribe_message` = 0. `dry_run` (default true) shows the footer and
changes nothing. System Settings → Disable Standard Email Footer is left to the
operator. Like every mutating tool here it is gated by its own switch on ERPNext
MCP Settings (default off); it is not exposed to phones.
"""

from __future__ import annotations

import frappe

from .. import compat, email_branding
from ..args import as_bool, as_str
from ..errors import ToolError
from ..result import ToolResult

ARGUMENTS = ("email_account", "company", "phone", "dry_run")


def _account(name: str) -> dict:
	if not compat.doctype_exists(email_branding.ACCOUNT):
		raise ToolError("this site has no Email Account doctype. Nothing was changed.")
	fields = compat.existing_fields(
		email_branding.ACCOUNT,
		(
			"name",
			"email_id",
			"enable_outgoing",
			"default_outgoing",
			"footer",
			"brand_logo",
			"send_unsubscribe_message",
		),
	)
	if name:
		row = frappe.db.get_value(email_branding.ACCOUNT, name, fields, as_dict=True)
		if not row:
			raise ToolError(f"Email Account {name!r} does not exist. Nothing was changed.")
		return dict(row)
	rows = frappe.db.get_all(
		email_branding.ACCOUNT, filters={"enable_outgoing": 1, "default_outgoing": 1}, fields=fields, limit=2
	)
	if len(rows or []) != 1:
		raise ToolError(
			"name the `email_account`: this site has no single Default Outgoing account. Nothing was changed."
		)
	return dict(rows[0])


def apply_email_branding(args: dict) -> ToolResult:
	"""Footer + logo + unsubscribe off on one Email Account. Dry run by default."""
	account = _account(as_str(args, "email_account").strip())
	company = as_str(args, "company").strip() or email_branding.default_company()
	if not company or not frappe.db.exists("Company", company):
		raise ToolError(
			f"name the `company` the footer is for (got {company or 'none'!r}). Nothing was changed."
		)
	dry_run = as_bool(args, "dry_run", True)

	ours = email_branding.footer_html(
		company, str(account.get("email_id") or ""), as_str(args, "phone").strip()
	)
	footer, how = email_branding.merge_footer(account.get("footer"), ours)
	logo = email_branding.brand_url(company)
	brand_before = str(account.get("brand_logo") or "")
	set_brand = bool(logo) and (not brand_before or email_branding.BRAND_PATH in brand_before)

	changes = {"footer": how, "send_unsubscribe_message": 0}
	if set_brand:
		changes["brand_logo"] = logo
	data = {
		"email_account": account["name"],
		"company": company,
		"dry_run": dry_run,
		"changes": changes,
		"footer_html": ours,
		"images": email_branding.image_audit(ours),
		"logo_url": logo or None,
		"standard_footer_disabled": email_branding.standard_footer_disabled(),
	}
	notes = []
	if not logo:
		notes.append(
			"No logo: Farm Ops Public URL is empty, and an image on the site's own address is a broken "
			"icon for every recipient outside the tailnet. Set it (the cutover's C3) and run this again."
		)
	if not data["standard_footer_disabled"]:
		notes.append(
			"The standard 'Sent via ERPNext' line still follows this footer. To remove it: System "
			"Settings → Disable Standard Email Footer (tick, save)."
		)
	if how == "appended":
		notes.append(
			"The account already had footer content of its own; it is kept above the company footer."
		)
	data["notes"] = notes

	if not dry_run:
		frappe.db.set_value(email_branding.ACCOUNT, account["name"], "footer", footer, update_modified=False)
		if compat.has_field(email_branding.ACCOUNT, "send_unsubscribe_message"):
			frappe.db.set_value(
				email_branding.ACCOUNT, account["name"], "send_unsubscribe_message", 0, update_modified=False
			)
		if set_brand:
			frappe.db.set_value(
				email_branding.ACCOUNT, account["name"], "brand_logo", logo, update_modified=False
			)
		try:
			frappe.get_doc(email_branding.ACCOUNT, account["name"]).add_comment(
				"Info",
				f"Company footer ({company}) {how}, unsubscribe link off"
				+ (", brand logo set to the public logo URL" if set_brand else "")
				+ f" by {frappe.session.user} via apply_email_branding.",
			)
		except Exception:  # pragma: no cover - a comment is a courtesy
			pass

	verb = "Would apply" if dry_run else "Applied"
	return ToolResult(
		data=data,
		summary=f"{verb} the {company} footer to {account['name']} (footer {how}; unsubscribe link off"
		+ ("; logo" if logo else "; no logo — Farm Ops Public URL is empty")
		+ ")",
	)
