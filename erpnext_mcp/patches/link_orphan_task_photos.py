# SPDX-License-Identifier: MIT
"""Re-link task photos that uploaded but were never attached. v0.326.0 (AFB-2026-00035).

FarmOps uploads a photo first and names it in the completion afterwards. When the completion is refused, the File
stays on the site with no `attached_to` — readable only by whoever took it, and on no task. Tim's insurance photos
of 2026-10-13 (FT-2026-10-00013/14/15/16/21, before / after / signature, 09:17 and 15:17–15:19) are such Files.

Every unattached File whose name starts with a task's name (`FT-2026-10-00016_photo_…`, which is how the phone
names everything it takes for a task) is attached to that task's most recent assignment (v0.328.0: and no longer filed on the asset — the reviewed
`asset_photos.backfill` decides which photos go there). It is NOT made into evidence rows: the queued completion, when it lands, files the photos it names,
and doing it here too would count one photo twice. Idempotent (only unattached Files are touched); never raises.
"""

from __future__ import annotations

import re

import frappe

from erpnext_mcp import compat

PREFIX = re.compile(r"^(FT-\d{4}-\d{2}-\d{5})_")


def execute() -> dict:
	report = {"linked": 0, "tasks": set()}
	try:
		if not compat.doctype_exists("Farm Task Assignment"):
			return report
		from erpnext_mcp import task_progress

		rows = frappe.db.get_all("File", filters={"file_name": ("like", "FT-%"), "attached_to_name": ("in", ["", None])},
		                         fields=["name", "file_name"], limit=5000) or []
		rows += frappe.db.get_all("File", filters={"file_name": ("like", "FT-%"), "attached_to_name": ("is", "not set")},
		                          fields=["name", "file_name"], limit=5000) or []
		seen = set()
		for row in rows:
			if row["name"] in seen:
				continue
			seen.add(row["name"])
			match = PREFIX.match(str(row.get("file_name") or ""))
			if not match or not frappe.db.exists("Farm Task", match.group(1)):
				continue
			task = match.group(1)
			assignment = frappe.db.get_value("Farm Task Assignment", {"task": task}, "name", order_by="creation desc")
			if not assignment:
				continue
			task_progress.attach_to_assignment(row["name"], assignment)
			# v0.328.0: not onto the asset here — which photos go there is `asset_photos.backfill`'s reviewed call.
			report["linked"] += 1
			report["tasks"].add(task)
		if report["linked"]:
			print(f"link_orphan_task_photos: attached {report['linked']} photo(s) on {sorted(report['tasks'])}")
	except Exception:  # noqa: BLE001 — a repair that cannot run must not stop the migrate
		frappe.log_error(title="erpnext_mcp: link_orphan_task_photos", message=compat.traceback_text())
	return report
