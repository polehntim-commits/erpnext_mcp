# SPDX-License-Identifier: MIT
"""Classify the open App Feedback already on the site. v0.206.0.

docs/design/config_flags_triage.md §3. New notes are classified on arrival;
this gives the backlog the same first sort, so the triage queue starts with
everything still open rather than only what arrives after the upgrade. It only
fills a blank triage and never proposes a change. Idempotent.
"""

from __future__ import annotations

import frappe

from erpnext_mcp import triage


def execute() -> None:
	done = run()
	if done:
		print(f"erpnext_mcp: classified {done} open App Feedback note(s) for triage")


def run() -> int:
	if not triage.ready():
		return 0
	done = 0
	for name in frappe.db.get_all(
		triage.DOCTYPE, filters={"status": ("not in", triage.ANSWERED)}, pluck="name", order_by="creation asc", limit=100000
	):
		try:
			if triage.auto_classify(name):
				done += 1
		except Exception:
			frappe.log_error(title="triage_existing_feedback", message=frappe.get_traceback())
	return done
