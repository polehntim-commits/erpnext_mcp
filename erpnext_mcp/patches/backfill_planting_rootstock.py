# SPDX-License-Identifier: MIT
"""Push the catalogue's rootstock down onto the plantings that record none.

WHAT WAS WRONG. `Crop Variety.rootstock` is one column on a CATALOGUE — one row
per variety per crop. So it holds exactly one rootstock for 'Bing', while a real
farm has Bing on Mazzard in the old block and Bing on Gisela 6 in the 2019
planting. The rootstock is half the tree: it decides vigour, final size, density,
how soon the block bears and how it takes wet ground, so those two blocks are
different trees with different yields, and a per-acre figure quoted against the
wrong one is not comparable to anything.

The block-level answer already existed — `Planting Season.rootstock` and
`Field.rootstock` have both been there since v0.88.0 — and on most sites it is
blank, because the catalogue column was the one anybody filled in. This carries
the value down to the grain that can hold more than one of it.

IT ONLY EVER FILLS A BLANK. A planting that already names a rootstock is the
better record by construction: somebody typed it against that block, whereas
this patch is inferring from a species-level default. So an existing value is
never rewritten and never compared — no "which is right" question is raised,
because the answer is always the one on the planting.

IT IS A SEED, NOT A SYNC, AND IT RUNS ONCE. After this the two columns are free
to disagree, and they SHOULD: that is the whole point of moving the fact down.
Nothing re-runs this on save, and re-running the patch on a site that has since
corrected a block does not undo the correction, because a filled value is a
value it will not touch.

WHY IT MATCHES ON CROP AS WELL AS VARIETY. 'Gala' is an apple and 'Bing' is a
cherry, but variety names are free text on both sides and a site that types
'Jonagold' under two different crops would otherwise get one crop's rootstock on
the other's block. Both halves are casefolded and stripped for the same reason
every other lookup in this app is: 'bing' and 'Bing ' are one tree.

Written with `update_modified=False`. `modified` on a planting is how somebody
tells a record that was reviewed this season from one nobody has touched since
2019, and a migration that stamped every row would erase that distinction across
the whole register to record something nobody did.

It does not raise. Inside `bench migrate` an exception aborts the migration for
the entire bench, and a planting with no rootstock is exactly the planting every
site had this morning — every reader of it already treats a blank as unknown.
"""

import frappe

from .. import compat

CROP_VARIETY = "Crop Variety"

#: The two registers that record a planting, in the order a reader should trust
#: them. Both carry `crop`, `variety` and `rootstock` as free text, and both are
#: filled by the same rule.
TARGETS = (
	("Planting Season", "a block-year"),
	("Field", "a block"),
)

#: Most rows one pass will read per register. A site with more plantings than
#: this has an import problem rather than an orchard, and the report says so
#: instead of silently covering the first slice.
SCAN_CAP = 50000


def execute() -> None:
	report = backfill_planting_rootstock()
	for line in report_lines(report):
		print(line)


def backfill_planting_rootstock() -> dict:
	"""Fill a blank `rootstock` from the crop catalogue. Idempotent."""
	report = {
		"catalogue": 0,
		"filled": {},
		"scanned": {},
		"already_set": {},
		"unmatched": {},
		"capped": [],
		"skipped": "",
	}

	catalogue = _catalogue()
	if catalogue is None:
		report["skipped"] = (
			f"this site has no {CROP_VARIETY} DocType, so there is no catalogue to read a "
			"default rootstock from"
		)
		return report
	report["catalogue"] = len(catalogue)
	if not catalogue:
		report["skipped"] = (
			"no variety in this site's crop register names a rootstock, so there is nothing "
			"to carry down. Plantings keep whatever they already record"
		)
		return report

	for doctype, _grain in TARGETS:
		if not (compat.doctype_exists(doctype) and compat.has_field(doctype, "rootstock")):
			continue
		_fill_register(doctype, catalogue, report)

	return report


def _catalogue() -> dict | None:
	"""(crop, variety) → rootstock, for every catalogue row that names one.

	Returns None where the DocType is absent, which is a different answer from
	an empty catalogue and is reported differently.

	A DUPLICATE KEY IS LEFT TO THE FIRST ROW AND NOT RESOLVED HERE. `crop.py`
	refuses two variety rows with one name inside a crop, so a collision means
	two CROPS spell a variety the same way with different rootstocks — which the
	crop half of the key already separates. What is left after that is a site
	whose data this patch should not be arbitrating.
	"""
	if not compat.doctype_exists(CROP_VARIETY):
		return None
	rows = (
		frappe.db.get_all(
			CROP_VARIETY,
			filters={"parenttype": "Crop"},
			fields=["parent", "variety_name", "rootstock"],
			limit=SCAN_CAP,
		)
		or []
	)
	catalogue = {}
	for row in rows:
		rootstock = str(row.get("rootstock") or "").strip()
		if not rootstock:
			continue
		key = _key(row.get("parent"), row.get("variety_name"))
		if key is None or key in catalogue:
			continue
		catalogue[key] = rootstock
	return catalogue


def _key(crop, variety):
	"""The lookup key, or None where either half is missing."""
	crop = str(crop or "").strip().casefold()
	variety = str(variety or "").strip().casefold()
	if not crop or not variety:
		return None
	return (crop, variety)


def _fill_register(doctype: str, catalogue: dict, report: dict) -> None:
	rows = (
		frappe.db.get_all(
			doctype,
			fields=["name", "crop", "variety", "rootstock"],
			limit=SCAN_CAP,
		)
		or []
	)
	report["scanned"][doctype] = len(rows)
	report["filled"][doctype] = 0
	report["already_set"][doctype] = 0
	report["unmatched"][doctype] = 0
	if len(rows) >= SCAN_CAP:
		report["capped"].append(doctype)

	for row in rows:
		if str(row.get("rootstock") or "").strip():
			# The better record by construction — typed against this block.
			report["already_set"][doctype] += 1
			continue
		key = _key(row.get("crop"), row.get("variety"))
		rootstock = catalogue.get(key) if key else None
		if not rootstock:
			# A planting whose crop or variety is blank, or names a variety the
			# catalogue has never heard of. Ordinary on a site that records
			# blocks before it fills in its crop register, and not a gap this
			# patch should invent an answer for.
			report["unmatched"][doctype] += 1
			continue
		try:
			frappe.db.set_value(doctype, row["name"], "rootstock", rootstock, update_modified=False)
		except Exception:  # pragma: no cover - a row that vanished mid-pass
			continue
		report["filled"][doctype] += 1


def report_lines(report: dict) -> list:
	"""What it did, for the console. Silent on a run that had nothing to fill."""
	if report.get("skipped"):
		return [f"erpnext_mcp: planting rootstocks were not backfilled — {report['skipped']}."]

	lines = []
	total = sum(report.get("filled", {}).values())
	if total:
		per = ", ".join(f"{count} {doctype}" for doctype, count in sorted(report["filled"].items()) if count)
		lines.append(
			f"erpnext_mcp: carried a rootstock down onto {total} planting record(s) ({per}) from "
			f"the {report['catalogue']} variety(ies) whose catalogue row names one. THE PLANTING "
			"IS NOW THE BINDING ANSWER: the same variety may sit on a different rootstock in "
			"every block, which is what the catalogue column could never say. Correct any block "
			"whose rootstock this got wrong — nothing re-derives it, and a value on a planting "
			"is never overwritten again."
		)
	unmatched = sum(report.get("unmatched", {}).values())
	if unmatched:
		lines.append(
			f"erpnext_mcp: {unmatched} planting record(s) kept a blank rootstock — their crop or "
			"variety is empty, or names a variety no Crop record lists. That is the ordinary "
			"state of a site that registered its blocks before its crop register, and every "
			"reader already treats a blank as unknown."
		)
	for doctype in report.get("capped", []):
		lines.append(
			f"erpnext_mcp: the rootstock backfill read its {SCAN_CAP} row ceiling on {doctype}, so "
			"older plantings may still be blank. Re-running `bench migrate` fills the next slice."
		)
	return lines
