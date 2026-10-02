# SPDX-License-Identifier: MIT
"""Mark the asset types that do not move. v0.214.0.

docs/design/badge_photo_and_fixed_assets.md, Part B. `Farm Asset Type` rows are
seeded once and never overwritten, so the new `fixed_location` column arrives
unticked on every type a site already has. This ticks it on the shipped fixed
types — wind machines, wells, valves, tanks, blocks, buildings — where the row
exists. Runs once: a farm that unticks one afterwards keeps its choice.
"""

from __future__ import annotations

import frappe

from erpnext_mcp import asset_moves


def execute() -> None:
	flagged = run()
	if flagged:
		print(
			f"erpnext_mcp: {len(flagged)} asset type(s) now have a fixed location — "
			f"{', '.join(flagged)}. A scan no longer moves them; move_asset does, with a reason."
		)


def run() -> list:
	if not asset_moves.ready():
		return []
	flagged = []
	for name in asset_moves.FIXED_TYPES:
		try:
			if not frappe.db.exists(asset_moves.TYPE, name):
				continue
			if not int(frappe.db.get_value(asset_moves.TYPE, name, "fixed_location") or 0):
				frappe.db.set_value(asset_moves.TYPE, name, "fixed_location", 1)
				flagged.append(name)
		except Exception:  # pragma: no cover - a site mid-migrate
			continue
	return flagged
