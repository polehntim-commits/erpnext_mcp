# SPDX-License-Identifier: MIT
"""Say, once, that every observation written before v0.115.0 was a pest scout.

WHAT CHANGED. `Crop Observation` gained `observation_type` in v0.115.0, and with
it the rule that `threat`, `threat_category` and `count_observed` are mandatory
on a Pest Scout AND ONLY THERE — so that a Harvest Readiness round, which is a
Brix and a growth stage with no organism in it, has somewhere to go that is not a
pest count of zero.

WHY THE BLANK IS NOT GOOD ENOUGH. Frappe adds the column with the DocType's
default, and on most benches every existing row does come out reading
`Pest Scout`. On some it comes out NULL, and the difference is invisible until
somebody filters. Both are wrong to rely on, because neither is a statement: a
row that reads Pest Scout because a DDL default fell that way looks exactly like
one somebody classified, and a NULL reads as "unclassified" for records that
were never anything else. Every observation on every site before v0.115.0 named
a threat and carried a count, because the DocType refused one that did not. They
are pest scouts, and this patch says so on purpose.

IT ONLY EVER FILLS A BLANK — the same rule `backfill_planting_rootstock` keeps
and for the same reason. A row that already names a type was classified by
somebody or written after the upgrade, and this is inferring from the fact that
nothing else used to be possible. An existing value is never rewritten and never
compared.

Written with `update_modified=False`. `modified` on an observation is how
somebody tells a record that was revisited from one nobody has touched since it
was filed, and a migration that stamped every row would erase that across the
whole register to record something nobody did.

It does not raise. Inside `bench migrate` an exception aborts the migration for
the entire bench, and an observation with a blank type is exactly the observation
every site had this morning.
"""

import frappe

from .. import compat
from ..erpnext_mcp.doctype.crop_observation.crop_observation import PEST_SCOUT

OBSERVATION = "Crop Observation"

#: Most rows one pass will read. A site with more observations than this has
#: several seasons of scouting, which is a good problem — the report says the
#: ceiling was reached rather than silently covering the first slice, and a
#: second `bench migrate` fills the next one.
SCAN_CAP = 50000


def execute() -> None:
	report = backfill_observation_type()
	for line in report_lines(report):
		print(line)


def backfill_observation_type() -> dict:
	"""Stamp `Pest Scout` on every observation that names no type. Idempotent."""
	report = {"scanned": 0, "filled": 0, "already_set": 0, "capped": False, "skipped": ""}

	if not compat.doctype_exists(OBSERVATION):
		report["skipped"] = f"this site has no {OBSERVATION} DocType, so there is nothing to classify"
		return report
	if not compat.has_field(OBSERVATION, "observation_type"):
		report["skipped"] = (
			f"{OBSERVATION} has no `observation_type` column on this site yet. That means the "
			"DocType has not been migrated — run `bench migrate` again and this patch will do "
			"its work on the next pass"
		)
		return report

	rows = frappe.db.get_all(OBSERVATION, fields=["name", "observation_type"], limit=SCAN_CAP) or []
	report["scanned"] = len(rows)
	report["capped"] = len(rows) >= SCAN_CAP

	for row in rows:
		if str(row.get("observation_type") or "").strip():
			report["already_set"] += 1
			continue
		try:
			frappe.db.set_value(
				OBSERVATION, row["name"], "observation_type", PEST_SCOUT, update_modified=False
			)
		except Exception:  # pragma: no cover - a row that vanished mid-pass
			continue
		report["filled"] += 1

	return report


def report_lines(report: dict) -> list:
	"""What it did, for the console. Silent on a run that had nothing to fill."""
	if report.get("skipped"):
		return [f"erpnext_mcp: observation types were not backfilled — {report['skipped']}."]

	lines = []
	if report["filled"]:
		lines.append(
			f"erpnext_mcp: classified {report['filled']} crop observation(s) as '{PEST_SCOUT}'. "
			"Every one of them named a threat and carried a count, because until v0.115.0 the "
			"DocType refused one that did not — so this records what they always were rather "
			"than guessing. A Harvest Readiness or Growth Stage round now has somewhere to go "
			"that is not a pest count of zero, and index_scouting_observations files one from "
			"every completed scouting task."
		)
	if report["capped"]:
		lines.append(
			f"erpnext_mcp: the observation-type backfill read its {SCAN_CAP} row ceiling, so "
			"older observations may still be unclassified. Re-running `bench migrate` fills the "
			"next slice."
		)
	return lines
