# SPDX-License-Identifier: MIT
"""Re-sort Auto-classified "Question" notes with the v0.207.0 classifier.

Field feedback showed most feature requests were labelled Question ("can the
app show my hours?"). Only notes the classifier itself sorted (Auto-classified)
and that it called Question are re-read; anything a person or a proposal
touched is left alone. Idempotent.
"""

from __future__ import annotations

import frappe

from erpnext_mcp import triage


def execute() -> None:
	moved = run()
	if moved:
		print(f"erpnext_mcp: re-sorted {moved} Auto-classified Question note(s)")


def run() -> int:
	if not triage.ready():
		return 0
	moved = 0
	for name in frappe.db.get_all(
		triage.DOCTYPE,
		filters={"triage_state": triage.AUTO, "triage_class": triage.QUESTION},
		pluck="name",
		limit=100000,
	):
		found = triage.classify(frappe.get_doc(triage.DOCTYPE, name))
		if found["triage_class"] != triage.QUESTION:
			frappe.db.set_value(
				triage.DOCTYPE,
				name,
				{"triage_class": found["triage_class"], "triage_summary": found["triage_summary"]},
				update_modified=False,
			)
			moved += 1
	return moved
