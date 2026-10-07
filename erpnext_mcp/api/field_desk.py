# SPDX-License-Identifier: MIT
"""The Field form's History and card in the Desk. v0.269.0.

The same `field_card` / `history` the phone and MCP use. Needs read permission on the Field; costs only for the
roles that read them.
"""

from __future__ import annotations

import frappe

from .. import field_card, history
from ..errors import ToolError
from .gis import speaks_frappe


def _require(field: str) -> None:
	if not frappe.has_permission("Field", "read", doc=field, user=frappe.session.user):
		raise ToolError("Reading a block's history needs read access to Field.")


def _history(field=None, types=None, season=None, from_date=None, to_date=None, before=None, limit=None) -> dict:
	_require(str(field or ""))
	return history.field_history(
		str(field), types=[t for t in str(types or "").split(",") if t.strip()],
		season=int(season) if str(season or "").isdigit() else None, from_date=str(from_date or ""),
		to_date=str(to_date or ""), before=str(before or ""),
		limit=int(limit) if str(limit or "").isdigit() else 50,
		include_sensitive=history.may_see_sensitive(frappe.get_roles(frappe.session.user)),
	)


def _card(field=None) -> dict:
	_require(str(field or ""))
	return field_card.card(str(field), include_sensitive=history.may_see_sensitive(frappe.get_roles(frappe.session.user)))


@frappe.whitelist()
def field_history(field=None, types=None, season=None, from_date=None, to_date=None, before=None, limit=None):
	return speaks_frappe(_history, field=field, types=types, season=season, from_date=from_date, to_date=to_date,
	                     before=before, limit=limit)


@frappe.whitelist()
def card(field=None):
	return speaks_frappe(_card, field=field)
