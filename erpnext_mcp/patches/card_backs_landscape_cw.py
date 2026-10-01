# SPDX-License-Identifier: MIT
"""Card Print Stations send the back as a landscape page, turned clockwise.

docs/design/card_print_queue.md, Amendment 3 §C1. v0.209.0 added
`back_orientation` with the default Portrait, before a card had been printed
with it. Tim's bench tests chose the landscape page with the back rotated CW,
so a station still on Portrait (or blank) is moved once. Runs once; a station
set back to Portrait afterwards stays there.
"""

from __future__ import annotations

import frappe

from erpnext_mcp import card_art, card_print, compat


def execute() -> None:
	moved = run()
	if moved:
		print(f"erpnext_mcp: {moved} card print station(s) now send the back landscape, rotated CW")


def run() -> int:
	if not card_print.ready() or not compat.has_field(card_print.STATION, "back_orientation"):
		return 0
	moved = 0
	for row in frappe.db.get_all(card_print.STATION, fields=["name", "back_orientation"], limit=0):
		if str(row.get("back_orientation") or "Portrait") == "Portrait":
			frappe.db.set_value(
				card_print.STATION, row["name"], "back_orientation", card_art.DEFAULT_BACK_ORIENTATION
			)
			moved += 1
	return moved
