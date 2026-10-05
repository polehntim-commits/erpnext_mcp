# SPDX-License-Identifier: MIT
"""Course videos and how much of them was watched. v0.246.0. docs/design/training_quiz_and_video.md §3–4
(approved; queue item 7; decisions 42, 44, 46).

* COURSE VIDEOS are a table on the Training Type (Course Video): title EN/ES, URL, optional Spanish URL
  (decision 46), YouTube or an uploaded file, required or optional, section ("Core", "Vintage films"),
  order, length. The old single `video_url` is moved in as a required Core video at migrate (create-only),
  and `get_training_curriculum` keeps returning it for older phones.
* WATCHED AMOUNT is counted by the phone's player while it plays on screen and sent as STRETCHES of the
  video ([start, end] seconds). Here they are merged across views and phones: % watched = unique seconds
  covered ÷ length. It is APPROXIMATE — the player cannot tell whether anyone was looking — and every
  answer says so. A video opened outside Farm Ops is recorded as "not measured".
* ONE TRAINING EVIDENCE DOCTYPE (decision 42) holds video views now and quiz attempts next release.
* THE MINIMUM IS OFF BY DEFAULT (decision 44): a course may set a minimum % for its required videos (a
  video may override it). Nothing here refuses anything; the quiz release reads `minimum_met`.
"""

from __future__ import annotations

import json
import re

import frappe

from . import compat

TYPE = "Training Type"
VIDEO = "Course Video"
EVIDENCE = "Training Evidence"
VIEW = "Video view"
ATTEMPT = "Quiz attempt"
APPROXIMATE, NOT_MEASURED = "Approximate", "Not measured"
FOOTER = {
	"en": "Approximate: counted by the app's player while the video was playing on screen. It can't tell whether "
	      "anyone was looking.",
	"es": "Aproximado: lo cuenta el reproductor de la app mientras el video se reproduce en pantalla. No puede saber "
	      "si alguien lo estaba viendo.",
}
_YOUTUBE = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def installed() -> bool:
	return compat.doctype_exists(EVIDENCE) and compat.has_field(TYPE, "videos")


def youtube_id(url: str) -> str:
	match = _YOUTUBE.search(str(url or ""))
	return match.group(1) if match else ""


def kind_of(url: str) -> str:
	if youtube_id(url):
		return "YouTube"
	if re.search(r"\.(mp4|m4v|mov)(\?|$)", str(url or ""), re.I) or str(url or "").startswith("/files/"):
		return "File"
	raise ValueError("url is a YouTube link or an uploaded video file (/files/… .mp4 / .mov).")


def _key(row) -> str:
	return str((row.get("name") if hasattr(row, "get") else None) or "")


FIELDS = ("title", "title_es", "url", "url_es", "required", "section", "sort_order", "length_seconds", "min_pct")


def _clean(values: dict) -> dict:
	out = {k: values[k] for k in FIELDS if k in values and values[k] is not None}
	if "url" in out:
		out["video_kind"] = kind_of(out["url"])
	if out.get("url_es"):
		kind_of(out["url_es"])
	for key in ("min_pct",):
		if key in out and out[key] not in ("", None) and not (0 <= int(out[key]) <= 100):
			raise ValueError("min_pct is 0 to 100.")
	if "required" in out:
		out["required"] = 1 if out["required"] in (1, True, "1", "true", "yes") else 0
	return out


def add(training_type: str, values: dict) -> dict:
	values = _clean(values)
	if not values.get("title") or not values.get("url"):
		raise ValueError("title and url are required.")
	doc = frappe.get_doc(TYPE, training_type)
	values.setdefault("section", "Core")
	values.setdefault("required", 1)
	values.setdefault("sort_order", len(doc.get("videos") or []) + 1)
	row = doc.append("videos", values)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	key = _key(row) or _key((doc.get("videos") or [])[-1])
	return next((v for v in videos(training_type) if v["video"] == key), videos(training_type)[-1])


def _row(doc, video: str):
	for row in doc.get("videos") or []:
		if _key(row) == video:
			return row
	raise ValueError(f"{doc.name} has no video {video!r} (see get_training_curriculum).")


def update(training_type: str, video: str, values: dict) -> dict:
	values = _clean(values)
	if not values:
		raise ValueError("nothing to change.")
	doc = frappe.get_doc(TYPE, training_type)
	row = _row(doc, video)
	for key, value in values.items():
		if hasattr(row, "set") and not isinstance(row, dict):
			row.set(key, value)
		else:
			row[key] = value
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return next(v for v in videos(training_type) if v["video"] == video)


def remove(training_type: str, video: str) -> dict:
	doc = frappe.get_doc(TYPE, training_type)
	row = _row(doc, video)
	kept = [r for r in doc.get("videos") or [] if _key(r) != video]
	doc.set("videos", kept)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return {"removed": video, "title": row.get("title"), "views_kept": frappe.db.count(EVIDENCE, {"video": video})}


def videos(training_type: str) -> list:
	doc = frappe.get_doc(TYPE, training_type)
	out = []
	for row in sorted(doc.get("videos") or [], key=lambda r: (int(r.get("sort_order") or 0), str(r.get("title") or ""))):
		out.append(
			{
				"video": _key(row),
				"title": row.get("title"),
				"title_es": row.get("title_es") or None,
				"url": row.get("url"),
				"url_es": row.get("url_es") or None,
				"kind": row.get("video_kind") or None,
				"youtube_id": youtube_id(row.get("url")) or None,
				"required": bool(int(row.get("required") or 0)),
				"section": row.get("section") or "Core",
				"sort_order": int(row.get("sort_order") or 0),
				"length_seconds": int(row.get("length_seconds") or 0) or None,
				"min_pct": int(row.get("min_pct") or 0) or None,
			}
		)
	return out


def by_section(rows: list) -> list:
	sections: dict = {}
	for row in rows:
		sections.setdefault(row["section"], []).append(row)
	return [{"section": name, "videos": items} for name, items in sections.items()]


def migrate_video_urls() -> list:
	"""Each Training Type's `video_url` → one required Core video, where it has none yet. Create-only."""
	moved = []
	if not compat.has_field(TYPE, "videos"):
		return moved
	for row in frappe.db.get_all(TYPE, filters={"video_url": ("not in", ["", None])}, fields=["name", "video_url"]) or []:
		doc = frappe.get_doc(TYPE, row["name"])
		if doc.get("videos"):
			continue
		try:
			kind = kind_of(row["video_url"])
		except ValueError:
			kind = "File"
		doc.append("videos", {"title": doc.get("training_type_name") or row["name"], "url": row["video_url"],
		                      "video_kind": kind, "required": 1, "section": "Core", "sort_order": 1})
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		moved.append(row["name"])
	return moved


# ── watched amount ──────────────────────────────────────────────────────────
def merge(stretches) -> list:
	"""[[a, b], …] → sorted, overlapping/touching stretches joined. Bad pairs dropped."""
	clean = []
	for pair in stretches or []:
		try:
			a, b = float(pair[0]), float(pair[1])
		except (TypeError, ValueError, IndexError):
			continue
		if b > a >= 0:
			clean.append([a, b])
	clean.sort()
	out: list = []
	for a, b in clean:
		if out and a <= out[-1][1] + 0.5:
			out[-1][1] = max(out[-1][1], b)
		else:
			out.append([a, b])
	return out


def covered(stretches) -> float:
	return sum(b - a for a, b in merge(stretches))


def record_view(*, employee: str, training_type: str, video: str, stretches, length_seconds=None, play_seconds=None,
                seeks=None, furthest=None, started_at: str = "", ended_at: str = "", device: str = "",
                measured: bool = True, client_request_id: str = "") -> dict:
	if not frappe.db.exists("Employee", employee):
		raise ValueError(f"no Employee {employee!r}.")
	known = {v["video"]: v for v in videos(training_type)}
	if video not in known:
		raise ValueError(f"{training_type} has no video {video!r} (see get_training_curriculum).")
	if client_request_id:
		again = frappe.db.get_value(EVIDENCE, {"client_request_id": client_request_id}, "name")
		if again:
			return {**describe(again), "duplicate": True}
	person = frappe.db.get_value("Employee", employee, ["employee_name", "company"], as_dict=True) or {}
	merged = merge(stretches) if measured else []
	length = float(length_seconds or known[video]["length_seconds"] or 0)
	pct = round(min(100.0, covered(merged) / length * 100), 1) if (measured and length > 0) else None
	doc = frappe.get_doc(
		{
			"doctype": EVIDENCE,
			"evidence_kind": VIEW,
			"employee": employee,
			"employee_name": person.get("employee_name") or employee,
			"company": person.get("company") or None,
			"training_type": training_type,
			"video": video,
			"video_title": known[video]["title"],
			"stretches": json.dumps(merged),
			"watched_pct": pct,
			"length_seconds": int(length) or None,
			"play_seconds": int(float(play_seconds or 0)) or None,
			"seeks": int(seeks or 0),
			"furthest_seconds": int(float(furthest or 0)) or None,
			"started_at": started_at or None,
			"finished_at": ended_at or None,
			"received_at": frappe.utils.now(),
			"device": device or None,
			"measured": APPROXIMATE if measured else NOT_MEASURED,
			"client_request_id": client_request_id or None,
		}
	).insert(ignore_permissions=True)
	return describe(doc.name)


def progress(employee: str, training_type: str) -> dict:
	"""Per video: merged across this person's views and phones; and whether the course minimum is met."""
	course = frappe.get_doc(TYPE, training_type)
	course_min = int(course.get("video_min_pct") or 0)
	views = frappe.db.get_all(
		EVIDENCE,
		filters={"evidence_kind": VIEW, "employee": employee, "training_type": training_type},
		fields=["video", "stretches", "seeks", "furthest_seconds", "measured", "length_seconds"],
		limit=2000,
	) or []
	out, met = [], True
	for video in videos(training_type):
		mine = [v for v in views if v.get("video") == video["video"]]
		all_stretches = [s for v in mine for s in json.loads(v.get("stretches") or "[]")]
		length = video["length_seconds"] or max([int(v.get("length_seconds") or 0) for v in mine] or [0])
		seconds = covered(all_stretches)
		pct = round(min(100.0, seconds / length * 100), 1) if length else None
		minimum = (video["min_pct"] or course_min) if video["required"] else 0
		ok = (not minimum) or (pct is not None and pct >= minimum)
		met = met and ok
		out.append(
			{
				**video,
				"views": len(mine),
				"watched_pct": pct,
				"watched_seconds": int(seconds),
				"seeks": sum(int(v.get("seeks") or 0) for v in mine),
				"furthest_seconds": max([int(v.get("furthest_seconds") or 0) for v in mine] or [0]) or None,
				"opened_outside": sum(1 for v in mine if v.get("measured") == NOT_MEASURED),
				"minimum_pct": minimum or None,
				"minimum_met": ok,
				"line": _line(pct, seconds, length, len(mine), mine),
			}
		)
	return {"employee": employee, "training_type": training_type, "videos": out,
	        "course_minimum_pct": course_min or None, "minimum_met": met, "footer": FOOTER}


def _clock(seconds) -> str:
	seconds = int(seconds or 0)
	return f"{seconds // 60}:{seconds % 60:02d}"


def _line(pct, seconds, length, views, rows) -> str:
	if not rows:
		return "Not watched"
	if pct is None:
		return f"{views} view(s), not measured"
	seeks = sum(int(v.get("seeks") or 0) for v in rows)
	furthest = max([int(v.get("furthest_seconds") or 0) for v in rows] or [0])
	return (f"Watched {pct:g}% ({_clock(seconds)} of {_clock(length)}) · {views} view(s) · {seeks} skip(s)"
	        + (f" · reached {_clock(furthest)}" if furthest else ""))


def describe(name: str) -> dict:
	row = frappe.get_doc(EVIDENCE, name).as_dict()
	return {
		"evidence": name,
		"kind": row.get("evidence_kind"),
		"employee": row.get("employee"),
		"training_type": row.get("training_type"),
		"video": row.get("video") or None,
		"watched_pct": row.get("watched_pct"),
		"stretches": json.loads(row.get("stretches") or "[]"),
		"measured": row.get("measured"),
		"received_at": str(row.get("received_at") or "") or None,
	}
