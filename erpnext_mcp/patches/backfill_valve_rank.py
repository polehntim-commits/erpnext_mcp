# SPDX-License-Identifier: MIT
"""Give every valve the rank its own line already states.

WHAT IS BROKEN WITHOUT THIS, AND IT IS NOT COSMETIC. `create_irrigation_valve`
refuses a child whose parent outranks it:

    parent_rank = _rank(str(parent_row.get("valve_type") or ""))
    if parent_rank > _rank(valve_type):

`_rank` answers `len(VALVE_TYPES)` — 3, one past Lateral — for a type it does not
recognise, and an EMPTY `valve_type` is a type it does not recognise. So a parent
with a blank rank scores 3, every real rank scores 0, 1 or 2, and the comparison
is true for all of them: on a register where nobody filled the column, that guard
refuses every valve anybody tries to hang off an existing one, with a message
naming the parent's rank as `''`. `_rank`'s own docstring says unranked "blocks
nothing", and that is true of a blank CHILD and false of a blank PARENT — the
asymmetry this patch removes the data half of. `update_irrigation_valve` already
guards its two comparisons with `if parent_type and` / `if child_type and`;
`create_irrigation_valve` does not, and correcting the register is the fix that
does not change a shipped refusal's meaning.

WHERE THE RANK COMES FROM, AND WHY IT IS NOT A GUESS ABOUT PLUMBING. This patch
reads no names and infers nothing from them. `Asset Register.location` IS the
hierarchy — that is the column the closing cascade walks and the one `_ancestry`
climbs — and `valve_type` is the rank ON that hierarchy. So the tree already
states the answer and this only writes it down:

  * A valve with no valve above it is at the head of its line. That is a MAIN.
  * A valve with a valve above it AND valves below it is in the middle. That is
    a SUB-MAIN.
  * A valve with a valve above it and nothing below it is at the row. That is a
    LATERAL.

"No valve above it" is `_ancestry`'s rule and not merely an empty `location`: a
line usually hangs off the block or zone asset it waters, and `_ancestry` STOPS
AT THE FIRST THING THAT IS NOT A VALVE for exactly that reason. A valve parented
to a Block is the head of its line, and gets Main.

THE THREE RANKS ARE A FLOOR, NOT A DEPTH. `VALVE_TYPES` has three entries and a
line can be nested deeper than three. Anything below the second level is a
Lateral rather than a fourth name this app does not have — which keeps the guard
monotonic down every chain (Main → Sub-Main → Lateral → Lateral …), so no valve
this patch writes can refuse a child that the register itself permits.

IT ONLY EVER FILLS A BLANK, the rule `backfill_observation_type` and
`backfill_planting_rootstock` both keep. A valve that already names a rank was
ranked by somebody who has walked the line, and this has read a tree. Where the
two disagree the person is right, and an existing value is never rewritten and
never compared — a valve deliberately filed as a Sub-Main with nothing under it
yet is a line somebody is part-way through building, not an error to correct.

RETIRED VALVES ARE RANKED TOO. A retired valve is still somebody's parent in the
register until its children are repointed, and leaving its rank blank would leave
`create_irrigation_valve` refusing underneath it — which is the whole failure
this patch exists to end.

Written with `update_modified=False`. `modified` on a valve is how somebody tells
a record that was revisited from one nobody has touched since it was tagged, and
a migration that stamped all thirty-three would erase that to record something
nobody did.

It does not raise. Inside `bench migrate` an exception aborts the migration for
every app on the bench, and a valve with a blank rank is what every site had this
morning.
"""

import frappe

from .. import compat

ASSET_REGISTER = "Asset Register"

#: The `asset_type` that makes a register row a valve. Spelled here rather than
#: imported from `tools.valves`, because a patch must keep running after the
#: module it was written beside is refactored — and this string is the doctype's
#: own Select option, which a rename would have to migrate anyway.
VALVE = "Irrigation Valve"

#: The ranks, coarsest first, in `tools.valves.VALVE_TYPES` order. The order is
#: the rank: `_rank` is `VALVE_TYPES.index`, so a name moved here changes which
#: parents `create_irrigation_valve` accepts.
MAIN, SUB_MAIN, LATERAL = "Main", "Sub-Main", "Lateral"

#: Most valves one pass will read. A register past this is a farm with more
#: gates than this app has ever been run against; the report says the ceiling
#: was reached rather than silently ranking a prefix, and a second `bench
#: migrate` fills the next slice.
SCAN_CAP = 20000

#: How far up a chain the walk climbs before calling it a miskeyed register.
#: `tools.valves.ANCESTRY_DEPTH` is the same bound for the same reason, and a
#: cycle — which nothing in the register refuses — is caught by `seen` rather
#: than by this.
ANCESTRY_DEPTH = 12


def execute() -> None:
	report = backfill_valve_rank()
	for line in report_lines(report):
		print(line)


def backfill_valve_rank() -> dict:
	"""Rank every valve that names no rank, from the tree. Idempotent."""
	report = {
		"scanned": 0,
		"filled": 0,
		"already_set": 0,
		"main": 0,
		"sub_main": 0,
		"lateral": 0,
		"capped": False,
		"skipped": "",
	}

	if not compat.doctype_exists(ASSET_REGISTER):
		report["skipped"] = f"this site has no {ASSET_REGISTER} DocType, so there are no valves to rank"
		return report
	if not compat.has_field(ASSET_REGISTER, "valve_type"):
		report["skipped"] = (
			f"{ASSET_REGISTER} has no `valve_type` column on this site yet. That means the "
			"DocType has not been migrated — run `bench migrate` again and this patch will do "
			"its work on the next pass"
		)
		return report

	rows = (
		frappe.db.get_all(
			ASSET_REGISTER,
			filters={"asset_type": VALVE},
			fields=["name", "location", "valve_type"],
			limit=SCAN_CAP,
		)
		or []
	)
	report["scanned"] = len(rows)
	report["capped"] = len(rows) >= SCAN_CAP

	# The whole register in memory, keyed by docname. Every question below —
	# "is this valve's parent a valve", "does anything hang off it" — is asked
	# once per valve, and asking the database each time would be a query per
	# edge on a tree that fits in a dictionary.
	valves = {str(row["name"]): dict(row) for row in rows}
	has_children = {
		parent for parent in (str(row.get("location") or "") for row in valves.values()) if parent in valves
	}

	for name, row in valves.items():
		if str(row.get("valve_type") or "").strip():
			report["already_set"] += 1
			continue

		rank = _rank_from_tree(name, valves, has_children)
		try:
			frappe.db.set_value(ASSET_REGISTER, name, "valve_type", rank, update_modified=False)
		except Exception:  # pragma: no cover - a row that vanished mid-pass
			continue
		report["filled"] += 1
		report[{MAIN: "main", SUB_MAIN: "sub_main", LATERAL: "lateral"}[rank]] += 1

	return report


def _rank_from_tree(name: str, valves: dict, has_children: set) -> str:
	"""Main at the head of a line, Sub-Main with valves under it, Lateral at the row.

	`valves` is every valve on the site by docname, so "my parent is a valve" is
	a membership test rather than a read — and a `location` pointing at a Block,
	a zone or a row that no longer exists all answer the same way, which is
	`_ancestry`'s rule: the first thing that is not a valve ends the line.
	"""
	parent = str(valves.get(name, {}).get("location") or "")
	if parent not in valves:
		return MAIN

	# One level is enough to tell a Sub-Main from a Lateral, but not to tell a
	# Sub-Main from a Main: a valve two levels down with children under it is
	# below a head that is itself below a head. Everything past the second level
	# is a Lateral, because there is no fourth name — see the module docstring.
	depth = _depth(name, valves)
	if depth >= 2:
		return LATERAL
	return SUB_MAIN if name in has_children else LATERAL


def _depth(name: str, valves: dict) -> int:
	"""How many valves stand above this one, stopping at the first non-valve.

	Cycle-safe: nothing in the register refuses A → B → A, and a walk that
	trusted the tree would spin. `ANCESTRY_DEPTH` bounds a chain that is long
	rather than circular.
	"""
	seen = {name}
	current = str(valves.get(name, {}).get("location") or "")
	depth = 0
	while current in valves and current not in seen and depth < ANCESTRY_DEPTH:
		seen.add(current)
		depth += 1
		current = str(valves[current].get("location") or "")
	return depth


def report_lines(report: dict) -> list:
	"""What it did, for the console. Silent on a run that had nothing to rank."""
	if report.get("skipped"):
		return [f"erpnext_mcp: valve ranks were not backfilled — {report['skipped']}."]

	lines = []
	if report["filled"]:
		lines.append(
			f"erpnext_mcp: ranked {report['filled']} irrigation valve(s) from the register's own "
			f"tree — {report['main']} Main, {report['sub_main']} Sub-Main, {report['lateral']} "
			f"Lateral. The rank was read from `location`, which is the hierarchy the closing "
			f"cascade already walks, and no name was interpreted. Until now a blank rank on a "
			f"PARENT scored one past Lateral in create_irrigation_valve's guard, which refused "
			f"every valve anybody tried to hang underneath it; that refusal is gone. A rank that "
			f"is wrong about the line is corrected with update_irrigation_valve."
		)
	if report["capped"]:
		lines.append(
			f"erpnext_mcp: the valve-rank backfill read its {SCAN_CAP} row ceiling, so some "
			"valves may still be unranked. Re-running `bench migrate` ranks the next slice."
		)
	return lines
