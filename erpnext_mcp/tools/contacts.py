# SPDX-License-Identifier: MIT
"""Contacts from business cards: the MCP half. v0.231.0. See `business_cards`."""

from __future__ import annotations

import frappe

from .. import business_cards
from ..args import as_bool, as_int, as_str
from ..errors import ToolError
from ..result import ToolResult


def _caller() -> str:
	user = str(getattr(frappe.session, "user", "") or "")
	business_cards.require_role(user)
	return user


def search_contacts(args: dict) -> ToolResult:
	"""Contacts by name, company, email, phone or where met. Read-only."""
	_caller()
	rows = business_cards.search(
		as_str(args, "query"), as_str(args, "company"), as_str(args, "met_at"), as_int(args, "limit", 50)
	)
	return ToolResult(data={"contacts": rows, "count": len(rows)}, summary=f"{len(rows)} contact(s)")


def save_contact(args: dict) -> ToolResult:
	"""Create or update a Contact from a confirmed card. `dry_run` previews only."""
	user = _caller()
	card = args.get("card")
	if not isinstance(card, dict):
		raise ToolError("card is required — the confirmed fields (first_name, company, emails, phones …).")
	if as_bool(args, "dry_run", False):
		data = business_cards.preview(card)
		return ToolResult(
			data=data,
			summary=f"preview: {len(data['duplicates'])} possible duplicate(s); "
			f"match {data['match'].get('supplier') or data['match'].get('customer') or 'none'}",
		)
	decision = {
		key: args.get(key)
		for key in ("merge_into", "save_as_new", "link_to", "create_party")
		if args.get(key) not in (None, "")
	}
	data = business_cards.save(user, card, decision, as_str(args, "client_request_id"), args.get("photos") or [])
	return ToolResult(
		data=data,
		summary=f"{'merged into' if data.get('merged') else 'saved'} contact {data['name']}",
		docstatus_delta="none → 0 (created)" if data.get("created") else "0 → 0 (updated)",
	)
