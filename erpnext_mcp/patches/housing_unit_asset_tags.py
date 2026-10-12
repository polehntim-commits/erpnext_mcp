# SPDX-License-Identifier: MIT
"""Give every Housing Unit its Asset Register tag. v0.322.0.

Units registered before v0.322.0 (on OML: MC-CABIN-1&2 and MC-CABIN-3&4, made from the phone on 2026-10-11) have no
tag, so they are missing from the phone's map and asset lists. This pairs each one through `housing_assets.ensure`,
the same code a save runs: it adopts an Asset Register row already carrying the unit's name and a building type,
creates one otherwise, and never makes a second. Idempotent — a second run changes nothing.
"""

from __future__ import annotations

from erpnext_mcp import housing_assets


def execute() -> None:
	report = housing_assets.backfill()
	made = report["created"] + report["adopted"]
	if made:
		print(
			f"erpnext_mcp: {len(made)} housing unit(s) now carry an Asset Register tag and show on the phone map: "
			+ ", ".join(f"{row['unit']} → {row['asset']}" for row in made)
		)
	if report["skipped"]:
		print(f"erpnext_mcp: housing unit tags skipped (retried on the unit's next save): {report['skipped']}")
