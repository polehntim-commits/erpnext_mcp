# SPDX-License-Identifier: MIT
"""Card Print Stations with no agent are printed by hand.

docs/design/card_print_queue.md, Amendment 4 §D1. The print agent was dropped:
a person downloads the card PDF, prints it and marks the job. A station whose
agent has never checked in becomes Manual (whole-card PDFs, no Offline state).
A station an agent HAS used is left as it is. Runs once.
"""

from __future__ import annotations

import frappe

from erpnext_mcp import card_print, compat


def execute() -> None:
	moved = run()
	if moved:
		print(f"erpnext_mcp: {moved} card print station(s) are now printed by hand (Manual)")


def run() -> int:
	if not card_print.ready() or not compat.has_field(card_print.STATION, "duplex"):
		return 0
	moved = 0
	for row in frappe.db.get_all(card_print.STATION, fields=["name", "duplex", "last_seen_at"], limit=0):
		if row.get("last_seen_at") or str(row.get("duplex") or "") == card_print.MANUAL:
			continue
		frappe.db.set_value(card_print.STATION, row["name"], "duplex", card_print.MANUAL)
		moved += 1
	return moved
