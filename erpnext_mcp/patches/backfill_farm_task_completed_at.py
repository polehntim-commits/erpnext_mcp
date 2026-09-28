# SPDX-License-Identifier: MIT
"""Give every Completed Farm Task a `completed_at`.

v0.203.0. The field is new: the rodent bait rules (and any rule that times work
from its completion) read it rather than `modified`, which every later edit
moved. A task completed before this release takes its latest Farm Task
Assignment's `completed_at` — when the worker actually finished — or, where no
assignment recorded one, its own `modified`, the best time on record.
Idempotent: a task that already has one is left alone.
"""

from __future__ import annotations

import frappe

from erpnext_mcp import compat

TASK = "Farm Task"
ASSIGNMENT = "Farm Task Assignment"


def execute() -> None:
	report = backfill_completed_at()
	print(
		f"erpnext_mcp: completed_at filled on {report['from_assignment']} task(s) from their "
		f"assignment and {report['from_modified']} from their last change"
	)


def backfill_completed_at() -> dict:
	report = {"from_assignment": 0, "from_modified": 0}
	if not compat.doctype_exists(TASK) or not compat.has_field(TASK, "completed_at"):
		return report
	rows = frappe.db.get_all(
		TASK, filters={"state": "Completed"}, fields=["name", "completed_at", "modified"], limit=100000
	)
	for row in rows or []:
		if row.get("completed_at"):
			continue
		when = ""
		if compat.doctype_exists(ASSIGNMENT):
			done = frappe.db.get_all(
				ASSIGNMENT,
				filters={"task": row["name"]},
				fields=["completed_at"],
				order_by="completed_at desc",
				limit=20,
			)
			stamps = sorted(
				str(entry.get("completed_at") or "") for entry in done or [] if entry.get("completed_at")
			)
			when = stamps[-1] if stamps else ""
		if when:
			report["from_assignment"] += 1
		else:
			when = str(row.get("modified") or "")
			report["from_modified"] += 1
		if when:
			frappe.db.set_value(TASK, row["name"], "completed_at", when, update_modified=False)
	return report
