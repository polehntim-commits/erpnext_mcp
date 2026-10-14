# SPDX-License-Identifier: MIT
"""Task photos, the assets they are of, and the metadata that makes them evidence. v0.328.0.

WHY (Tim, the 2026-10-13 camp-buildings insurance report):
- MC-SHOWER-TOILET-1 showed no tasks although FT-2026-10-00013 photographed it: the asset screens read `task.asset`
  and live tasks only, and the insurance tasks name their building as the task's LOCATION.
- The side photos stayed on the task assignments; a Cowork task attached 20 of them to the assets by hand, and the
  server re-encoded every copy.
- None of the 144 photos carries GPS or a capture time — Frappe's System Setting "Strip EXIF tags from uploaded
  images" (default on) re-encodes every JPEG saved through `File.save_file`, after the phone hashed it.

WHAT THIS MODULE DOES
1. `tasks_for_asset` — every task about an asset, open or done: `asset`, the task's location, a multi-place task's
   rows, a cabin's Housing Unit; each with its latest assignment and a photo strip.
2. `gallery` — a task's evidence, every assignment, for the Desk form and the phone.
3. `keeps_exif` / `exif_kept` — task and asset evidence is stored as uploaded (GPS, time, lens intact); people's
   records (employee, I-9, W-4, injury, badge) are still stripped.
4. `link_row` — a checklist side photo (or one marked "for the asset") is LINKED to the asset it shows: a second
   File row with the same file_url and content_hash — one set of bytes, shown on both, never a copy, and deleting
   either row leaves the bytes for the other. Never signatures, before photos or untagged extras; only for the
   task types in `asset_photo_task_types`; per photo, the asset it was tagged with on a multi-place task.
5. `backfill` — the same for completed tasks, dry run first, every link logged; the hand-attached re-encoded copies
   are reported, and replaced only when asked.

The task's evidence rows and files are the record of truth: nothing here moves, renames or deletes them.
"""

from __future__ import annotations

import contextlib
import re

import frappe

from . import compat

TASK = "Farm Task"
ASSIGNMENT = "Farm Task Assignment"
EVIDENCE = "Farm Task Evidence"
ASSET = "Asset Register"
HOUSING = "Housing Unit"
SETTINGS = "ERPNext MCP Settings"
#: Where a photo keeps what the camera wrote: the task and asset registers and their inspections.
KEEP_EXIF_DOCTYPES = {TASK, ASSIGNMENT, ASSET, HOUSING, "Field", "Field Pin", "Housing Inspection", "Detector Test",
                      "Water Test", "Crop Observation", "Spray Application", "Scale Ticket", "Delivery Intake",
                      "Inspection Session", "Contractor Job", "Irrigation Zone", "Parcel"}
#: Never linked to an asset, whatever the task type: people.
PEOPLE_TEMPLATES = re.compile(r"badge|i-?9|w-?4|injur|accident|first aid|training|hiring|onboard", re.I)
DEFAULT_TASK_TYPES = ("Inspection", "Maintenance", "Repair")
PHOTO_STRIP = 6
TASK_LIMIT = 50


# ── 3. EXIF ────────────────────────────────────────────────────────────────
def keeps_exif(doctype: str, file_name: str = "") -> bool:
	"""Whether an image saved for this record keeps its EXIF. Unattached phone task photos (`FT-…`) do."""
	if doctype:
		return doctype in KEEP_EXIF_DOCTYPES
	return str(file_name or "").upper().startswith("FT-")


@contextlib.contextmanager
def exif_kept(keep: bool):
	"""For this save only, do not let Frappe strip EXIF (the site setting itself is untouched)."""
	if not keep:
		yield
		return
	settings = None
	previous = None
	try:
		frappe.get_system_settings("strip_exif_metadata_from_uploaded_images")
		settings = getattr(frappe.local, "system_settings", None)
		if settings is not None:
			previous = settings.get("strip_exif_metadata_from_uploaded_images")
			settings.strip_exif_metadata_from_uploaded_images = 0
	except Exception:  # noqa: BLE001 — a bare worker with no System Settings strips nothing anyway
		settings = None
	try:
		yield
	finally:
		if settings is not None:
			settings.strip_exif_metadata_from_uploaded_images = previous


# ── 1. tasks for an asset ──────────────────────────────────────────────────
def _housing_of(asset: str) -> str:
	if compat.doctype_exists(HOUSING) and compat.has_field(HOUSING, "asset_tag"):
		return str(frappe.db.get_value(HOUSING, {"asset_tag": asset}, "name") or "")
	return ""


def task_names_for_asset(asset: str) -> list:
	"""Every Farm Task about this asset, by any route; newest first."""
	if not compat.doctype_exists(TASK) or not asset:
		return []
	names: set = set()
	filter_sets = [{"asset": asset}, {"location_doctype": ASSET, "location": asset}]
	unit = _housing_of(asset)
	if unit:
		filter_sets.append({"location_doctype": HOUSING, "location": unit})
	for filters in filter_sets:
		if any(not compat.has_field(TASK, f) for f in filters):
			continue
		names.update(str(n) for n in frappe.db.get_all(TASK, filters=filters, pluck="name", limit=500) or [])
	try:
		from . import task_locations

		names.update(task_locations.tasks_at(ASSET, asset) or [])
		if unit:
			names.update(task_locations.tasks_at(HOUSING, unit) or [])
	except Exception:  # noqa: BLE001 — a site without task places has the routes above
		pass
	return sorted(names, reverse=True)


def _evidence(assignments: list, limit: int | None = None) -> list:
	if not assignments or not compat.doctype_exists(EVIDENCE):
		return []
	fields = ["parent", *compat.existing_fields(EVIDENCE, ("file", "file_url", "evidence_type", "phase", "caption",
	                                                      "captured_on", "checklist_item", "gps_latitude",
	                                                      "gps_longitude", "idx"))]
	rows = frappe.db.get_all(EVIDENCE, filters={"parent": ("in", list(assignments)), "parenttype": ASSIGNMENT},
	                         fields=fields, order_by="idx asc", limit=limit or 2000) or []
	return [{"assignment": r.get("parent"), "file": r.get("file"), "url": r.get("file_url"),
	         "type": r.get("evidence_type") or "Photo", "phase": r.get("phase") or None,
	         "step": r.get("checklist_item") or None, "captured_on": str(r.get("captured_on") or "") or None,
	         "latitude": r.get("gps_latitude"), "longitude": r.get("gps_longitude")} for r in rows]


def tasks_for_asset(asset: str, *, limit: int = TASK_LIMIT, photos: int = PHOTO_STRIP) -> list:
	"""[{task, task_name, task_type, state, completed_at, assignment, assignment_state, worker, photos[]}]."""
	names = task_names_for_asset(asset)[: max(1, int(limit or TASK_LIMIT))]
	if not names:
		return []
	cols = compat.existing_fields(TASK, ("name", "task_name", "task_type", "state", "completed_at", "creation",
	                                     "assigned_to_name"))
	tasks = frappe.db.get_all(TASK, filters={"name": ("in", names)}, fields=cols, order_by="creation desc") or []
	assignments = frappe.db.get_all(ASSIGNMENT, filters={"task": ("in", names)},
	                                fields=["name", "task", "state", "assigned_to_name", "completed_at", "creation"],
	                                order_by="creation desc", limit=2000) or []
	latest: dict = {}
	by_task: dict = {}
	for a in assignments:
		latest.setdefault(a["task"], a)
		by_task.setdefault(a["task"], []).append(a["name"])
	strip: dict = {}
	for row in _evidence([a["name"] for a in assignments]):
		if row["type"] != "Photo":
			continue
		task = next((t for t, names_ in by_task.items() if row["assignment"] in names_), None)
		if task and len(strip.setdefault(task, [])) < photos:
			strip[task].append(row)
	out = []
	for t in tasks:
		a = latest.get(t["name"]) or {}
		out.append({"task": t["name"], "task_name": t.get("task_name"), "task_type": t.get("task_type"),
		            "state": t.get("state"), "completed_at": str(t.get("completed_at") or a.get("completed_at") or "")
		            or None, "assignment": a.get("name"), "assignment_state": a.get("state"),
		            "worker": a.get("assigned_to_name") or t.get("assigned_to_name"),
		            "photos": strip.get(t["name"], []), "photo_count": len(strip.get(t["name"], []))})
	return out


# ── 2. a task's gallery ────────────────────────────────────────────────────
def gallery(task: str) -> dict:
	"""Every assignment's evidence for one task, grouped, newest assignment first."""
	assignments = frappe.db.get_all(ASSIGNMENT, filters={"task": task},
	                                fields=["name", "state", "assigned_to_name", "completed_at", "creation"],
	                                order_by="creation desc", limit=100) or []
	rows = _evidence([a["name"] for a in assignments])
	groups = []
	for a in assignments:
		items = [r for r in rows if r["assignment"] == a["name"]]
		groups.append({"assignment": a["name"], "state": a.get("state"), "worker": a.get("assigned_to_name"),
		               "completed_at": str(a.get("completed_at") or "") or None, "evidence": items})
	return {"task": task, "assignments": groups, "count": len(rows)}


# ── 4. linking a side photo to its asset ───────────────────────────────────
def task_types() -> set:
	raw = ""
	try:
		if compat.has_field(SETTINGS, "asset_photo_task_types"):
			raw = str(frappe.db.get_single_value(SETTINGS, "asset_photo_task_types") or "")
	except Exception:  # noqa: BLE001
		raw = ""
	found = {t.strip() for t in re.split(r"[\n,]", raw) if t.strip()}
	return found or set(DEFAULT_TASK_TYPES)


def task_assets(task: dict) -> list:
	"""The Asset Register records a task is about: `asset`, its location (a cabin through its tag), every place."""
	if not task or not compat.doctype_exists(ASSET):
		return []
	places = [{"location_type": task.get("location_doctype"), "location": task.get("location")}]
	try:
		from . import task_locations

		places += task_locations.rows(str(task.get("name") or ""))
	except Exception:  # noqa: BLE001
		pass
	found = [str(task.get("asset") or "")]
	for place in places:
		kind, name = str(place.get("location_type") or ""), str(place.get("location") or "")
		if kind == ASSET:
			found.append(name)
		elif kind == HOUSING and compat.has_field(HOUSING, "asset_tag"):
			found.append(str(frappe.db.get_value(HOUSING, name, "asset_tag") or ""))
	out = []
	for name in found:
		if name and name not in out and frappe.db.exists(ASSET, name):
			out.append(name)
	return out


def _task(task) -> dict:
	if isinstance(task, dict) and "task_type" in task:
		return task
	name = task.get("name") if isinstance(task, dict) else task
	cols = compat.existing_fields(TASK, ("name", "task_type", "template", "task_name", "asset", "location_doctype",
	                                     "location"))
	return dict(frappe.db.get_value(TASK, name, cols, as_dict=True) or {})


def why_not(row: dict, task: dict) -> str:
	"""Why this evidence row is not linked to an asset, or "" when it is linked."""
	if str(row.get("evidence_type") or "Photo") != "Photo":
		return "not a photo (signatures, audio and documents stay on the task)"
	if str(row.get("phase") or "") == "before":
		return "a before photo"
	if not (row.get("checklist_item") or compat.checked(row.get("for_asset"))):
		return "not tagged to a checklist side or marked for the asset"
	if str(task.get("task_type") or "") not in task_types():
		return f"task type {task.get('task_type') or '(none)'} is not one whose photos go on the asset"
	if PEOPLE_TEMPLATES.search(" ".join(str(task.get(k) or "") for k in ("template", "task_name"))):
		return "a task about people"
	return ""


def targets(row: dict, task: dict) -> tuple:
	"""(asset names, reason when none). A multi-place task needs the photo's own asset tag."""
	tagged = str(row.get("asset") or "")
	if tagged:
		return ([tagged], "") if frappe.db.exists(ASSET, tagged) else ([], f"tagged asset {tagged} does not exist")
	assets = task_assets(task)
	if not assets:
		return [], "the task names no asset"
	if len(assets) > 1:
		return [], f"the task covers {len(assets)} assets and the photo names none of them"
	return assets, ""


def _slug(text: str) -> str:
	words = re.sub(r"[^A-Za-z0-9]+", " ", str(text or "")).split()
	keep = [w for w in words if w.lower() not in ("photo", "photos", "taken", "side", "of", "the")]
	return "-".join(keep or words or ["PHOTO"]).upper()[:40]


def label(asset: str, row: dict, when: str = "") -> str:
	"""`<ASSET>_<SIDE>_<date>` — the name the hand-attached 10/13 copies used, so links and copies read alike."""
	date = str(row.get("captured_on") or when or frappe.utils.today())[:10]
	side = _slug(row.get("checklist_item") or "ASSET")
	return f"{asset}_{side}_{date}"


def _existing_link(asset: str, url: str):
	return frappe.db.get_value("File", {"file_url": url, "attached_to_doctype": ASSET, "attached_to_name": asset},
	                           "name")


def link_file(file_name: str, asset: str, name_as: str, *, dry_run: bool = False) -> dict:
	"""One more File row for the same bytes, attached to the asset. Idempotent by (asset, file_url)."""
	src = frappe.db.get_value("File", file_name, ["name", "file_url", "content_hash", "is_private", "file_size"],
	                          as_dict=True) if file_name else None
	if not src or not src.get("file_url"):
		return {"asset": asset, "file": file_name, "outcome": "skipped", "reason": "no such file"}
	existing = _existing_link(asset, src["file_url"])
	if existing:
		return {"asset": asset, "file": file_name, "outcome": "already", "link": existing}
	if dry_run:
		return {"asset": asset, "file": file_name, "outcome": "would_link", "name": name_as}
	ext = "." + src["file_url"].rsplit(".", 1)[-1] if "." in src["file_url"] else ""
	doc = frappe.get_doc({"doctype": "File", "file_url": src["file_url"], "file_name": name_as + ext,
	                      "is_private": src.get("is_private") or 0, "content_hash": src.get("content_hash"),
	                      "file_size": src.get("file_size"), "attached_to_doctype": ASSET, "attached_to_name": asset})
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	if src.get("content_hash") and not frappe.db.get_value("File", doc.name, "content_hash"):
		frappe.db.set_value("File", doc.name, "content_hash", src["content_hash"], update_modified=False)
	return {"asset": asset, "file": file_name, "outcome": "linked", "link": doc.name}


def link_row(row: dict, task, *, dry_run: bool = False) -> list:
	"""Link one evidence row's photo to its asset(s), if it qualifies. Never raises; answers per asset."""
	try:
		task = _task(task)
		reason = why_not(row, task)
		if reason:
			return [{"file": row.get("file"), "outcome": "skipped", "reason": reason}]
		assets, reason = targets(row, task)
		if not assets:
			return [{"file": row.get("file"), "outcome": "skipped", "reason": reason}]
		return [link_file(str(row.get("file") or ""), asset, label(asset, row), dry_run=dry_run) for asset in assets]
	except Exception as exc:  # noqa: BLE001 — a link is a view; the task's evidence is the record
		return [{"file": row.get("file"), "outcome": "skipped", "reason": str(exc)[:200]}]


def link_assignment(assignment: str, *, dry_run: bool = False) -> list:
	"""Every evidence row of one assignment, through `link_row`."""
	task_name = frappe.db.get_value(ASSIGNMENT, assignment, "task")
	if not task_name:
		return []
	task = _task(task_name)
	fields = compat.existing_fields(EVIDENCE, ("file", "file_url", "evidence_type", "phase", "checklist_item",
	                                          "for_asset", "asset", "captured_on"))
	out = []
	for row in frappe.db.get_all(EVIDENCE, filters={"parent": assignment, "parenttype": ASSIGNMENT},
	                             fields=fields, order_by="idx asc") or []:
		if row.get("file"):
			for result in link_row(dict(row), task, dry_run=dry_run):
				out.append({"task": task_name, "assignment": assignment, "step": row.get("checklist_item"), **result})
	return out


# ── 5. backfill ────────────────────────────────────────────────────────────
MANUAL_NAME = re.compile(r"^(?P<asset>.+)_(?P<side>[A-Z][A-Z0-9-]*)_(?P<date>\d{4}-\d{2}-\d{2})\.(jpe?g|png|heic)$", re.I)


def manual_copies(assets: list) -> list:
	"""Files attached to these assets by hand under `<ASSET>_<SIDE>_<date>` whose bytes are NOT a task's file —
	the re-encoded copies. Reported always; replaced only by `backfill(replace_manual=True)`."""
	out = []
	for asset in assets:
		for f in frappe.db.get_all("File", filters={"attached_to_doctype": ASSET, "attached_to_name": asset},
		                           fields=["name", "file_name", "file_url", "content_hash", "file_size"]) or []:
			m = MANUAL_NAME.match(str(f.get("file_name") or ""))
			if not m:
				continue
			shared = frappe.db.count("File", {"file_url": f["file_url"], "name": ("!=", f["name"])})
			if shared:
				continue  # a link, not a copy
			out.append({"asset": asset, "file": f["name"], "file_name": f["file_name"], "side": m.group("side"),
			            "date": m.group("date"), "size": f.get("file_size")})
	return out


def backfill(tasks: list | None = None, *, since: str = "", dry_run: bool = True, replace_manual: bool = False,
             note: str = "") -> dict:
	"""Link completed tasks' side photos to their assets. Dry run by default; each applied link is logged."""
	filters: dict = {"state": "Completed"}
	if since:
		filters["completed_at"] = (">=", since)
	names = [str(t) for t in (tasks or [])] or frappe.db.get_all(ASSIGNMENT, filters=filters, pluck="task",
	                                                                limit=5000) or []
	results, assets_touched = [], set()
	for task_name in dict.fromkeys(names):
		for assignment in frappe.db.get_all(ASSIGNMENT, filters={"task": task_name, "state": "Completed"},
		                                    pluck="name") or []:
			for r in link_assignment(assignment, dry_run=dry_run):
				results.append(r)
				if r.get("asset"):
					assets_touched.add(r["asset"])
	for t in names:
		assets_touched.update(task_assets(_task(t)))
	manual = manual_copies(sorted(assets_touched))
	if not dry_run:
		_note_runs(results, note)
	replaced = []
	if replace_manual and not dry_run:
		replaced = _replace_manual(manual, results)
	linked = [r for r in results if r.get("outcome") in ("linked", "would_link")]
	return {"dry_run": dry_run, "tasks": len(dict.fromkeys(names)), "links": linked,
	        "already": sum(1 for r in results if r.get("outcome") == "already"),
	        "skipped": [r for r in results if r.get("outcome") == "skipped"],
	        "manual_copies": manual, "manual_replaced": replaced, "note": note or None}


def _note_runs(results: list, note: str) -> None:
	"""One timeline line per asset that gained links: which photos, from which tasks — findable for cleanup."""
	by_asset: dict = {}
	for r in results:
		if r.get("outcome") == "linked":
			by_asset.setdefault(r["asset"], []).append(f"{r.get('task')} {r.get('step') or 'photo'} ({r.get('link')})")
	for asset, lines in by_asset.items():
		try:
			frappe.get_doc(ASSET, asset).add_comment(
				"Info", f"Task photos linked (asset_photos backfill{': ' + note if note else ''}): " + "; ".join(lines))
		except Exception:  # noqa: BLE001
			pass


def _replace_manual(manual: list, results: list) -> list:
	"""Delete a hand-attached re-encoded copy only where the asset now has a link for the same side and date."""
	done = []
	for copy in manual:
		side, date = copy["side"].upper(), copy["date"]
		has_link = any(r.get("asset") == copy["asset"] and r.get("outcome") in ("linked", "already")
		               and str(r.get("step") and _slug(r["step"])) == side for r in results)
		if not has_link:
			done.append({**copy, "outcome": "kept", "reason": "no linked task photo for this side"})
			continue
		frappe.delete_doc("File", copy["file"], ignore_permissions=True, force=True)
		done.append({**copy, "outcome": "removed", "date": date})
	return done


# ── Desk ───────────────────────────────────────────────────────────────────
@frappe.whitelist(methods=["GET"])
def desk_gallery(task: str) -> dict:
	"""The Farm Task form's photo gallery (display only)."""
	if not frappe.has_permission(TASK, "read", task):
		frappe.throw("Not permitted", frappe.PermissionError)
	return gallery(task)


@frappe.whitelist(methods=["GET"])
def desk_asset_tasks(asset: str) -> dict:
	"""The Asset Register form's "Tasks & photos" section (display only)."""
	if not frappe.has_permission(ASSET, "read", asset):
		frappe.throw("Not permitted", frappe.PermissionError)
	return {"asset": asset, "tasks": tasks_for_asset(asset)}
