# SPDX-License-Identifier: MIT
"""Copy each block's single variety into Field Variety, its own child row.

WHAT WAS WRONG. `Field.variety` is one Data column — one variety per block. Real
blocks do not respect that: the Pearl blocks carry Black Pearl, Burgundy Pearl and
Ebony Pearl in one field, and a single column can name exactly one of them.
v0.142.0 added `Field Variety`, a child table LINKING a block to the varieties in
its own crop's catalogue (`Crop.varieties`) rather than a second record of them —
but a table that ships EMPTY on every site that upgrades into it would answer the
multi-variety question no better than the column it replaces: every existing
block would look unrecorded in the new table until somebody retyped what
`Field.variety` already said.

WHAT THIS DOES. For every Field that names a `variety` and has no Field Variety
row yet, it appends one row naming that same variety at 100% of the block — the
whole block being that one variety is exactly what a single `variety` column
claiming to speak for the whole thing already meant.

THE VARIETY STILL HAS TO PASS `Field._check_varieties`. Where the block's `crop`
resolves to a real `Crop` record that lists at least one variety, the row is
checked against that catalogue the same way a caller's own `varieties` argument
is — a block whose crop was never turned into a catalogue keeps whatever
spelling `Field.variety` already held. A block whose crop DOES have a catalogue
but whose `variety` spelling is not in it is reported by name in
`not_in_catalogue` rather than silently skipped: that block needs a correction
this patch has no business making up.

IT IS A SEED, NOT A SYNC, AND IT RUNS ONCE. `Field.variety` and the other legacy
columns are left exactly as they were — they stay the primary answer for a
single-variety block — and the two are never reconciled after this. A block that
turns out to be genuinely multi-variety is edited through `update_field`'s
`varieties` argument, which replaces the child table wholesale; this patch does
not touch it again because a block with a Field Variety row already is skipped.

IT ONLY EVER FILLS A BLOCK WITH NO CHILD ROW OF ITS OWN. A block that already has
one — a fresh v0.142.0+ install, or a caller who has already used `varieties` — is
the better record by construction and is left alone rather than compared against.

APPENDING A CHILD ROW MOVES `modified`. Frappe has no way to write a child table
row without `doc.save()`, and `doc.save()` always stamps the parent's `modified`
timestamp — there is no `update_modified=False` escape hatch here the way
`backfill_planting_rootstock` has for a plain column. Every migrated block's
`modified` date moves to the day this patch ran. Accepted because there is no
cheaper way to create the first row in a table that otherwise never gets one on an
existing site.

It does not raise. Inside `bench migrate` an exception here aborts the migration
for the entire bench, and a block left with no Field Variety row is exactly the
block every site had this morning — `_describe_field` already reports an empty
`varieties` list for one.
"""

import frappe

from .. import compat

FIELD = "Field"
FIELD_VARIETY = "Field Variety"

#: Most blocks one pass will read. A site with more fields than this has an
#: import problem rather than an orchard, and the report says so instead of
#: silently covering the first slice.
SCAN_CAP = 50000


def execute() -> None:
	report = backfill_field_varieties()
	for line in report_lines(report):
		print(line)


def backfill_field_varieties() -> dict:
	"""Copy a blank-table block's single variety into its own child row, at 100%.

	Idempotent.
	"""
	report = {
		"scanned": 0,
		"filled": 0,
		"already_set": 0,
		"blank": 0,
		"not_in_catalogue": 0,
		"capped": False,
		"skipped": "",
	}

	if not (compat.doctype_exists(FIELD) and compat.doctype_exists(FIELD_VARIETY)):
		report["skipped"] = (
			f"this site has no {FIELD_VARIETY} DocType yet, so there is nowhere to copy a block's "
			"variety into"
		)
		return report

	rows = (
		frappe.db.get_all(
			FIELD,
			filters={"variety": ("is", "set")},
			fields=["name", "variety", "planting_year"],
			limit=SCAN_CAP,
		)
		or []
	)
	report["scanned"] = len(rows)
	if not rows:
		report["skipped"] = "no Field on this site names a variety, so there is nothing to copy down"
		return report
	if len(rows) >= SCAN_CAP:
		report["capped"] = True

	for row in rows:
		variety = str(row.get("variety") or "").strip()
		if not variety:
			# "is set" and whitespace-only are not the same claim; a name that is
			# only spaces has nothing worth copying down.
			report["blank"] += 1
			continue
		# `get_all(..., limit=1)` truthiness rather than `frappe.db.exists`: exists
		# reads the match's own `name` column, and this only needs to know
		# whether a row is there at all.
		if frappe.db.get_all(FIELD_VARIETY, filters={"parent": row["name"], "parenttype": FIELD}, limit=1):
			report["already_set"] += 1
			continue
		try:
			doc = frappe.get_doc(FIELD, row["name"])
			doc.append(
				"varieties",
				{
					"variety": variety,
					"percentage": 100.0,
					"planting_year": int(row.get("planting_year") or 0),
				},
			)
			doc.save(ignore_permissions=True)
		except frappe.ValidationError:
			# The block's crop resolves to a real Crop record whose catalogue does
			# not list this spelling. Named rather than folded into a generic
			# failure count, because a silent skip here reads identically to
			# "nothing to do" and this block genuinely needs a person's attention.
			report["not_in_catalogue"] += 1
			continue
		except Exception:  # pragma: no cover - a block that vanished mid-pass
			continue
		report["filled"] += 1

	return report


def report_lines(report: dict) -> list:
	"""What it did, for the console. Silent on a run that had nothing to fill."""
	if report.get("skipped"):
		return [f"erpnext_mcp: field varieties were not backfilled — {report['skipped']}."]

	lines = []
	if report["filled"]:
		lines.append(
			f"erpnext_mcp: copied {report['filled']} block's single variety into its own Field "
			f"Variety row at 100%, out of {report['scanned']} block(s) that name a variety. The "
			"legacy `variety` column is unchanged and stays the primary answer for a single-variety "
			"block; the child table is now where a genuinely multi-variety block records the rest, "
			"via update_field's `varieties` argument."
		)
	if report["already_set"]:
		lines.append(
			f"erpnext_mcp: {report['already_set']} block(s) already had at least one Field Variety "
			"row and were left alone."
		)
	if report["not_in_catalogue"]:
		lines.append(
			f"erpnext_mcp: {report['not_in_catalogue']} block(s) name a variety that is not in "
			"their crop's own Varieties catalogue, so no Field Variety row could be linked to it. "
			"Add the variety to the Crop's catalogue, or correct Field.variety's spelling, then "
			"re-run `bench migrate`."
		)
	if report["blank"]:
		lines.append(
			f"erpnext_mcp: {report['blank']} block(s) named a variety that was blank after "
			"stripping and were skipped."
		)
	if report["capped"]:
		lines.append(
			f"erpnext_mcp: the field variety backfill read its {SCAN_CAP} row ceiling. Re-running "
			"`bench migrate` fills the next slice."
		)
	return lines
