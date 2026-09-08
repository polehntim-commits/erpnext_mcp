# SPDX-License-Identifier: MIT
"""Turn `Asset Register.asset_type` from a Select into a register. v0.162.0.

THE COLUMN DID NOT CHANGE SHAPE, WHICH IS WHY THIS IS SAFE. `asset_type` was a
Select holding 'Irrigation Valve'; it is now a Link holding 'Irrigation Valve',
because `Farm Asset Type` names itself from `type_name` and the docname IS the
string every asset already stores. So this patch rewrites nothing on any asset.
It creates the masters those values were already pointing at in spirit.

That is the whole reason the master is `field:type_name` rather than a series.
A `FAT-00007` autoname would have meant rewriting `asset_type` on every asset on
the site inside a migration — and a migration that rewrites a register whose
docnames are printed on zip-tied tags in an orchard is one somebody has to be
able to prove was correct afterwards.

────────────────────────────────────────────────────────────────────────────
IT SEEDS WHAT SHIPS **AND** WHAT THE REGISTER ACTUALLY HOLDS
────────────────────────────────────────────────────────────────────────────

`asset_types.SEEDED` is fifteen: the thirteen that were on the Select, plus Fuel
Tank and Gas Tank. That covers every site running a build of this app.

It is not enough on its own. A `reqd` Link whose target does not exist is, in the
Desk, an asset that cannot be opened or saved — so a value this app does not
ship, however it got there (an operator widened the Select by hand; a site is
upgrading across several releases whose option lists differed), would take those
assets out of service at the exact moment nobody is looking. So the patch reads
the DISTINCT values off the column and seeds those too, under their own name,
with their own first letter as an icon. A type this app cannot describe is still
better than a link that resolves to nothing.

────────────────────────────────────────────────────────────────────────────
IDEMPOTENT, AND ORDERED
────────────────────────────────────────────────────────────────────────────

Listed in `patches.txt` AND called from `after_migrate`, the same as
`migrate_training_types` and `register_custom_party_types` — the patch entry
records in the Patch Log when this first ran, and the hook catches a site that
upgraded across the version. So it runs at least twice on any real bench, and
`asset_types.seed` only ever creates what is absent by docname. An operator who
retired a type, renamed one, reordered the picker or rewrote a description keeps
every one of those decisions through every later migrate.

IT DOES NOT RAISE. It runs inside `bench migrate`, where an exception aborts the
migration for the whole bench. A type it could not create is named on the
console, and the assets carrying it are readable and reportable — they simply
will not resolve in the Desk until somebody makes the record, which is a much
smaller problem than a bench that will not migrate.

IT DOES NOT REASSIGN ANYTHING. `MC-FUEL-TANK` on the site this was written
against is registered as `Storage`, and Fuel Tank now exists — but retyping it is
a judgement about a physical object and belongs to whoever walks past it, not to
a migration reading its docname.
"""

import frappe

from erpnext_mcp import asset_types, compat


def execute() -> None:
	report = migrate_asset_types()
	for line in report_lines(report):
		print(line)


def migrate_asset_types() -> dict:
	"""Create a `Farm Asset Type` for everything shipped and everything in use."""
	report = {
		"scanned": 0,
		"in_register": [],
		"inherited": [],
		"created": [],
		"present": [],
		"failed": [],
		"skipped": "",
	}
	if not asset_types.available():
		report["skipped"] = (
			f"this site has no {asset_types.DOCTYPE} DocType yet — it ships with erpnext_mcp "
			"v0.162.0, so run `bench --site <site> migrate` again"
		)
		return report

	if compat.doctype_exists(asset_types.ASSET_REGISTER):
		report["scanned"] = frappe.db.count(asset_types.ASSET_REGISTER)
		report["in_register"] = asset_types.distinct_in_register()
	report["inherited"] = [name for name in report["in_register"] if name not in asset_types.SEEDED_NAMES]

	outcome = asset_types.seed(extra=report["in_register"])
	report["created"] = outcome["created"]
	report["present"] = outcome["present"]
	report["failed"] = outcome["failed"]
	return report


def report_lines(report: dict) -> list:
	"""What the migration did, for the console. Quiet when there was nothing to do."""
	lines = []
	if report.get("skipped"):
		return [f"erpnext_mcp: the asset types were not migrated — {report['skipped']}."]

	created = report.get("created") or []
	if created:
		lines.append(
			f"erpnext_mcp: created {len(created)} Farm Asset Type record(s) — "
			f"{', '.join(created)}. `Asset Register.asset_type` is a Link to that register from "
			f"v0.162.0 instead of a hard-coded Select, so a new kind of asset is now a record "
			f"somebody creates in the Desk. NOTHING WAS RETYPED: all "
			f"{report.get('scanned', 0)} asset(s) still carry exactly the type they carried, "
			"because each type's docname is the string they already stored."
		)
	inherited = report.get("inherited") or []
	if inherited:
		lines.append(
			f"erpnext_mcp: {len(inherited)} asset type(s) already in this register are not ones "
			f"this app ships — {', '.join(inherited)}. A record was created for each so the "
			"assets carrying them still resolve; give them an icon and a description when you "
			"get a moment, or untick Enabled to keep them off new pickers."
		)
	for failure in report.get("failed") or ():
		lines.append(
			f"erpnext_mcp: could not create the {failure['type_name']!r} asset type "
			f"— {failure['reason']}. Assets carrying it will not resolve in the Desk until "
			"somebody creates it by hand."
		)
	return lines
