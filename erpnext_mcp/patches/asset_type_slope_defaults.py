# SPDX-License-Identifier: MIT
"""Move the slope-rated asset types from code into data. v0.230.5.

`slope_grade.TYPE_DEFAULTS` named the four types that carry a safe-slope limit
(Tractor 15°, Vehicle 20°, Sprayer 12°, Implement 15°). Those are now
`has_slope_limit` and `default_max_safe_slope_degrees` on Farm Asset Type, so a
new kind of machine — a Mini Excavator — is a tick rather than a release. This
writes today's answer onto the four rows where they exist and have nothing set,
and fills the Asset Register's fetched `type_has_slope_limit` flag so the Desk
form keeps showing the field. Runs once; an operator's later choice stands.
"""

from __future__ import annotations

import frappe

from erpnext_mcp import compat, slope_grade

TYPE = slope_grade.TYPE_DOCTYPE


def execute() -> None:
	report = run()
	if report["types"]:
		print(
			f"erpnext_mcp: slope limits are now data on Farm Asset Type — "
			f"{', '.join(report['types'])} carry has_slope_limit and their cautious default. "
			"Tick has_slope_limit on any other machine type with update_asset_type."
		)


def run() -> dict:
	report = {"types": [], "assets": 0}
	if not compat.doctype_exists(TYPE) or not compat.has_field(TYPE, "has_slope_limit"):
		return report
	for name, degrees in slope_grade.TYPE_DEFAULTS.items():
		try:
			if not frappe.db.exists(TYPE, name):
				continue
			row = frappe.db.get_value(TYPE, name, ["has_slope_limit", "default_max_safe_slope_degrees"], as_dict=True)
			if row and not compat.checked(row.get("has_slope_limit")) and not row.get("default_max_safe_slope_degrees"):
				frappe.db.set_value(TYPE, name, {"has_slope_limit": 1, "default_max_safe_slope_degrees": degrees})
				report["types"].append(name)
		except Exception:  # pragma: no cover - a site mid-migrate
			continue
	if compat.has_field("Asset Register", "type_has_slope_limit"):
		rated = frappe.db.get_all(TYPE, filters={"has_slope_limit": 1}, pluck="name") or []
		for name in frappe.db.get_all("Asset Register", filters={"asset_type": ("in", rated or ["-"])}, pluck="name") or []:
			frappe.db.set_value("Asset Register", name, "type_has_slope_limit", 1, update_modified=False)
			report["assets"] += 1
	return report
