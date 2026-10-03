# SPDX-License-Identifier: MIT
"""Payroll and bookkeeping windows: a group of tool switches on for a limited time. v0.224.0.

docs/design/payroll_windows.md §1. Built on the v0.217.0 switch timer: every
switch a window opens is recorded in `switch_expiry` and the 5-minute job turns
it off when time is up. A switch already on by hand is permanent and left alone.
A person's act in the Desk (System Manager) — no MCP tool opens a window.

MONEY AND FILING STAY HUMAN. The groups hold the tools that PREPARE (payroll
runs, GL posting, pay stubs, NACHA/prenote files, tax-form PDFs); nothing here
sends money or submits a return, and `mark_tax_form_filed` is in no group.
"""

from __future__ import annotations

import json

import frappe

from . import audit, security_alerts, settings, switch_catalog, switch_timer
from .errors import ToolError

DEFAULT_MINUTES = 120

GROUPS = {
	"payroll": (
		"calculate_payroll",
		"run_payroll_for_period",
		"submit_payroll",
		"post_payroll_to_gl",
		"configure_payroll_accounts",
		"calculate_payroll_taxes",
		"render_pay_stub",
		"generate_nacha_file",
		"generate_prenote_file",
		"generate_tax_form",
		"regenerate_tax_form",
		"render_tax_form_pdf",
		"bulk_render_tax_form_pdfs",
		"generate_1099_prefill",
	),
	"bookkeeping": (
		"create_journal_entry",
		"submit_journal_entry",
		"bulk_submit_journal_entries",
		"create_purchase_invoice",
		"submit_purchase_invoice",
		"create_sales_invoice",
		"submit_sales_invoice",
		"create_payment_entry",
		"submit_payment_entry",
		"receive_payment",
	),
}
#: Never opened by a window, whatever a setting says: filing is a person's record.
NEVER = frozenset({"mark_tax_form_filed"})


def groups() -> dict:
	"""The built-in groups, with the `switch_windows` setting's additions and replacements."""
	out = {key: list(value) for key, value in GROUPS.items()}
	raw = settings._value("switch_windows")
	try:
		configured = json.loads(raw) if raw else {}
	except (TypeError, ValueError):
		configured = {}
	if isinstance(configured, dict):
		for key, value in configured.items():
			if isinstance(value, list):
				out[str(key).strip().lower()] = [str(v) for v in value]
	return out


def open_window(group: str, minutes, by: str) -> dict:
	"""Turn on every switch of `group` that is off, until now + minutes. Raises ToolError."""
	from . import registry

	group = str(group or "").strip().lower()
	members = groups().get(group)
	if not members:
		raise ToolError(
			f"{group or 'that'} is not a window. Windows: {', '.join(sorted(groups()))}. Nothing was changed."
		)
	try:
		minutes = int(minutes if minutes not in (None, "") else DEFAULT_MINUTES)
	except (TypeError, ValueError):
		raise ToolError("minutes must be a whole number.") from None
	if not switch_timer.MIN_MINUTES <= minutes <= switch_timer.MAX_MINUTES:
		raise ToolError(
			f"minutes must be {switch_timer.MIN_MINUTES}–{switch_timer.MAX_MINUTES}. Nothing was changed."
		)
	doc = frappe.get_single(settings.SETTINGS_DOCTYPE)
	timed = switch_timer._load(doc)
	until = switch_timer._shift(minutes=minutes)
	opened, already, missing, refused = [], [], [], []
	for tool in members:
		spec = registry.TOOLS.get(tool)
		if spec is None:
			missing.append(tool)
			continue
		if (
			tool in NEVER
			or not spec.get("mutating")
			or switch_catalog.tier(tool, spec) == switch_catalog.CREDENTIAL
		):
			refused.append(tool)
			continue
		if settings.as_bool(doc.get(f"allow_{tool}")) and tool not in timed:
			already.append(tool)
			continue
		timed[tool] = until
		doc.set(f"allow_{tool}", 1)
		opened.append(tool)
	if not opened:
		raise ToolError(
			f"the {group} window has nothing to open: {len(already)} already on by hand, "
			f"{len(missing)} not on this site. Nothing was changed."
		)
	doc.switch_expiry = json.dumps(timed, sort_keys=True)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	text = f"{by} opened the {group} window for {minutes} minute(s), until {until}: {', '.join(opened)}."
	audit.record(
		"switch:open_window",
		{"group": group, "minutes": minutes, "tools": opened},
		audit.STATUS_SUCCESS,
		text[:500],
		commit=False,
	)
	security_alerts.send(f"ERPNext MCP: the {group} window is open until {until}", text)
	return {
		"group": group,
		"expires_at": until,
		"opened": opened,
		"already_on": already,
		"not_on_this_site": missing,
		"never_in_a_window": refused,
	}
