# SPDX-License-Identifier: MIT
"""Partial progress on a Farm Task, saved a piece at a time. v0.326.0.

WHY. AFB-2026-00035, the insurance photo field test on 2026-10-13: five tasks done offline, every photo uploaded,
and nothing on the server — the completions were refused ("FTA-… is Paused", "4 required checklist items not
marked done") and, because the completion was the ONLY call that carried the evidence, a refused completion meant
the server held none of it. Crew phones are offline for whole shifts (the Starlink is on Tim's vehicle), so a
completion that has to arrive whole and right first time is the wrong shape.

WHAT IT IS. Each piece the worker captures is saved on its own, onto the live assignment, as soon as the phone has
a connection — never checked against the evidence contract, which is the Complete step's job:

- `photo` (any evidence file): a Farm Task Evidence row on the assignment, tagged with its `checklist_item` when the
  worker took it on that step (which ticks the item), and filed on the task's asset as well;
- `tick`: one checklist item done (or explicitly not), with a note;
- `gps`: where the work is being done;
- `findings`: the findings draft (latest wins, by the time it was written on the phone);
- `answers`: draft form answers, merged key by key.

EVERY ITEM CARRIES THE PHONE'S `client_item_id`, so a retry is recognised and answered "duplicate" rather than filed
twice. Each item succeeds or fails on its own — one bad item never refuses the batch.

THE COMPLETION FOLDS IT IN (`dispatch.complete_farm_task`): saved photos count toward the contract, the draft
findings and answers stand in for ones the Complete call did not send, and a refused Complete keeps what it carried
(`keep_from_refused_completion`) so nothing captured is ever lost to a validation error.
"""

from __future__ import annotations

import json
import time

import frappe

from . import compat

ASSIGNMENT = "Farm Task Assignment"
TASK = "Farm Task"
EVIDENCE = "Farm Task Evidence"
#: Where progress may be saved. Held is included: a task put on hold mid-job keeps what was done before the hold.
OPEN_STATES = ("Claimed", "In-Progress", "Paused", "Held")
KINDS = ("photo", "evidence", "tick", "gps", "findings", "answers")
MAX_ITEMS = 60
#: How many received item ids an assignment remembers (enough for any one job's retries).
SEEN_CAP = 600


class ProgressError(ValueError):
	pass


def ready() -> bool:
	return compat.has_field(ASSIGNMENT, "progress_state") and compat.has_field(EVIDENCE, "client_item_id")


def state_of(raw) -> dict:
	"""The assignment's progress_state, tolerant of a value nobody can parse."""
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
	except Exception:
		return {}
	return value if isinstance(value, dict) else {}


def _later(stamp: str, than: str) -> bool:
	"""True when `stamp` is not older than `than` (both site-local strings); missing stamps never block."""
	return not stamp or not than or str(stamp) >= str(than)


def _tick(task_name: str, entries: list) -> list:
	"""Mark checklist items on the task's snapshot (cumulative). Returns the items' new states."""
	from .tools import dispatch

	task = dispatch.task_row(task_name)
	marked = dispatch._marked_checklist(task, {"checklist": entries})
	frappe.db.set_value(TASK, task_name, "checklist_status", json.dumps(marked), update_modified=False)
	wanted = {str(e.get("item_name") if isinstance(e, dict) else e).casefold() for e in entries}
	return [i for i in marked["items"] if str(i.get("item_name") or "").casefold() in wanted]


def file_on_asset(file_name: str, asset: str) -> bool:
	"""Make a photo of an asset readable from the asset too: a second File row with the same URL. Never raises."""
	if not file_name or not asset or not compat.doctype_exists("Asset Register"):
		return False
	try:
		url = frappe.db.get_value("File", file_name, "file_url")
		if not url or frappe.db.exists("File", {"file_url": url, "attached_to_doctype": "Asset Register",
		                                         "attached_to_name": asset}):
			return False
		src = frappe.get_doc("File", file_name)
		copy = frappe.get_doc({"doctype": "File", "file_url": url, "file_name": src.file_name,
		                       "is_private": src.is_private, "attached_to_doctype": "Asset Register",
		                       "attached_to_name": asset})
		copy.flags.ignore_permissions = True
		copy.insert(ignore_permissions=True)
		return True
	except Exception:  # noqa: BLE001 — the asset's copy is a courtesy; the assignment's row is the record
		return False


def attach_to_assignment(file_name: str, assignment: str) -> None:
	"""An unattached upload now belongs to the assignment, so anyone who can read the task can open it."""
	try:
		row = frappe.db.get_value("File", file_name, ["attached_to_doctype", "attached_to_name"], as_dict=True)
		if row and not row.get("attached_to_name"):
			frappe.db.set_value("File", file_name, {"attached_to_doctype": ASSIGNMENT,
			                                       "attached_to_name": assignment}, update_modified=False)
	except Exception:  # noqa: BLE001
		pass


def add_evidence(doc, task: dict, entries: list, checklist_items=None) -> list:
	"""Append evidence rows to an assignment doc, skipping any already on it. Returns the rows added.

	A row is the same row when it has the same `client_item_id` or the same File — so a completion that re-sends
	photos already saved as progress files each one once. `checklist_items` maps an entry index to its item.
	"""
	from .tools import inspections

	existing = list(doc.get("evidence_files") or [])
	ids = {str(r.get("client_item_id") or "") for r in existing if r.get("client_item_id")}
	files = {str(r.get("file") or r.get("file_url") or "") for r in existing}
	added = []
	for index, entry in enumerate(entries):
		row = inspections.normalise_evidence([entry], f"evidence[{index}]", enrich=True)[0]
		cid = str((entry or {}).get("client_item_id") or "").strip() if isinstance(entry, dict) else ""
		key = str(row.get("file") or row.get("file_url") or "")
		if (cid and cid in ids) or (key and key in files):
			continue
		item = (checklist_items or {}).get(index) or (
			str(entry.get("checklist_item") or "").strip() if isinstance(entry, dict) else "")
		if cid and compat.has_field(EVIDENCE, "client_item_id"):
			row["client_item_id"] = cid[:140]
		if item and compat.has_field(EVIDENCE, "checklist_item"):
			row["checklist_item"] = item[:140]
		doc.append("evidence_files", row)
		ids.add(cid)
		files.add(key)
		added.append(row)
		if row.get("file"):
			attach_to_assignment(row["file"], doc.name)
			file_on_asset(row["file"], str(task.get("asset") or ""))
	return added


def save(task_name: str, assignment_name: str, items, *, worker: str = "") -> dict:
	"""Save each progress item on its own. Returns per-item results; never refuses the batch for one item."""
	from .tools import dispatch

	if not ready():
		raise ProgressError("this site has not migrated to v0.326.0 yet (no progress fields).")
	if isinstance(items, dict):
		items = [items]
	if not isinstance(items, list) or not items:
		raise ProgressError("items must be a non-empty list of progress items.")
	if len(items) > MAX_ITEMS:
		raise ProgressError(f"at most {MAX_ITEMS} progress items per call; send the rest in the next one.")
	doc = frappe.get_doc(ASSIGNMENT, assignment_name)
	if doc.task != task_name:
		raise ProgressError(f"{assignment_name} is not an assignment of {task_name}.")
	if worker and doc.get("assigned_to") != worker:
		raise ProgressError(f"{assignment_name} is not yours, so its progress is not yours to save.")
	if doc.state not in OPEN_STATES and doc.state != "Completed":
		raise ProgressError(f"{assignment_name} is {doc.state}; there is no open job to save progress on.")
	task = dispatch.task_row(task_name)
	state = state_of(doc.get("progress_state"))
	seen = list(state.get("seen") or [])
	seen_set = set(seen)
	results = []
	changed = False
	for item in items:
		item = item if isinstance(item, dict) else {}
		cid = str(item.get("client_item_id") or "").strip()[:140]
		kind = str(item.get("kind") or "").strip().lower()
		out = {"client_item_id": cid or None, "kind": kind or None}
		if not cid:
			results.append({**out, "status": "refused", "reason": "client_item_id is required."})
			continue
		if cid in seen_set:
			results.append({**out, "status": "duplicate"})
			continue
		if kind not in KINDS:
			results.append({**out, "status": "refused", "reason": f"kind is one of: {', '.join(KINDS)}."})
			continue
		if doc.state == "Completed" and kind not in ("photo", "evidence"):
			results.append({**out, "status": "refused", "reason": f"{doc.name} is already completed."})
			continue
		at = str(item.get("at") or item.get("captured_at") or "")
		try:
			if kind in ("photo", "evidence"):
				entry = {k: v for k, v in item.items() if k not in ("kind", "at")}
				added = add_evidence(doc, task, [entry])
				out["status"] = "saved" if added else "duplicate"
				checklist_item = str(item.get("checklist_item") or "").strip()
				if added and checklist_item and doc.state != "Completed":
					# The photo is kept whatever the tick does: a step name the checklist no longer has is a note on
					# this item, never a reason to lose the picture.
					try:
						out["ticked"] = _tick(task_name, [{"item_name": checklist_item, "done": True, "note": "photo"}])
					except Exception as exc:  # noqa: BLE001
						out["tick_refused"] = str(exc)[:300]
			elif kind == "tick":
				name = str(item.get("item_name") or "").strip()
				if not name:
					raise ProgressError("a tick names its item_name.")
				entry = {"item_name": name, "done": bool(item.get("done", True))}
				if str(item.get("note") or "").strip():
					entry["note"] = str(item["note"]).strip()
				out["ticked"] = _tick(task_name, [entry])
				out["status"] = "saved"
			elif kind == "gps":
				lat, lon = item.get("latitude"), item.get("longitude")
				if lat in (None, "") or lon in (None, ""):
					raise ProgressError("a gps item carries latitude and longitude.")
				if _later(at, state.get("gps_at", "")):
					doc.farm_location_gps = f"{round(float(lat), 6)},{round(float(lon), 6)}"
					state["gps_at"] = at
				out["status"] = "saved"
			elif kind == "findings":
				if _later(at, state.get("findings_at", "")):
					doc.findings_text = str(item.get("text") or "")
					state["findings_at"] = at
					state["findings_draft"] = True
				out["status"] = "saved"
			elif kind == "answers":
				answers = item.get("answers")
				if isinstance(answers, str):
					answers = json.loads(answers)
				if not isinstance(answers, dict):
					raise ProgressError("an answers item carries an `answers` object.")
				draft = dict(state.get("answers") or {})
				draft.update(answers)
				state["answers"] = draft
				out["status"] = "saved"
			changed = True
			seen.append(cid)
			seen_set.add(cid)
		except Exception as exc:  # noqa: BLE001 — one item's failure is that item's, never the batch's
			# Every write above happens after its last check (a tick's set_value follows `_marked_checklist`; a photo
			# row is appended after `normalise_evidence`), so a refused item has written nothing.
			reason = str(exc) or type(exc).__name__
			results.append({**out, "status": "refused", "reason": reason[:500]})
			continue
		results.append(out)
	if changed:
		state["seen"] = seen[-SEEN_CAP:]
		doc.progress_state = json.dumps(state)
		doc.progress_saved_at = frappe.utils.now()
		doc.save(ignore_permissions=True)
	return {"task": task_name, "assignment": doc.name, "state": doc.state, "results": results,
	        "saved": sum(1 for r in results if r.get("status") == "saved"),
	        "progress": describe(doc.name)}


def keep_from_refused_completion(assignment_name: str, task: dict, args: dict) -> dict:
	"""A Complete that failed validation still leaves what it carried on the assignment. Never raises.

	Committed on its own: the refusal that follows rolls the request back, and the evidence must survive it.
	"""
	kept = {"evidence": 0, "findings": False, "gps": False, "answers": 0}
	if not ready():
		return kept
	try:
		doc = frappe.get_doc(ASSIGNMENT, assignment_name)
		if doc.state not in OPEN_STATES:
			return kept
		raw = args.get("evidence_files")
		entries = raw if isinstance(raw, list) else ([raw] if raw else [])
		kept["evidence"] = len(add_evidence(doc, task, entries))
		state = state_of(doc.get("progress_state"))
		if "findings_text" in args and str(args.get("findings_text") or "").strip():
			doc.findings_text = str(args.get("findings_text"))
			state["findings_draft"] = True
			kept["findings"] = True
		if args.get("farm_location_gps"):
			doc.farm_location_gps = str(args.get("farm_location_gps"))
			kept["gps"] = True
		answers = args.get("form_answers")
		if isinstance(answers, dict) and answers:
			state["answers"] = {**(state.get("answers") or {}), **answers}
			kept["answers"] = len(answers)
		doc.progress_state = json.dumps(state)
		doc.progress_saved_at = frappe.utils.now()
		doc.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception:  # noqa: BLE001 — keeping is best effort; the refusal itself must still reach the phone
		frappe.db.rollback()
	return kept


def draft_for_completion(assignment: dict) -> dict:
	"""What saved progress contributes to a Complete: the findings draft and the draft answers."""
	state = state_of(assignment.get("progress_state"))
	return {"answers": dict(state.get("answers") or {}),
	        "findings": str(assignment.get("findings_text") or "") if state.get("findings_draft") else None}


def describe(assignment_name: str) -> dict:
	"""The partial progress on one assignment — what get_farm_task and the phone show."""
	from .erpnext_mcp.doctype.farm_task.farm_task import checklist_items

	cols = compat.existing_fields(ASSIGNMENT, ("name", "task", "state", "findings_text", "farm_location_gps",
	                                           "progress_state", "progress_saved_at"))
	row = frappe.db.get_value(ASSIGNMENT, assignment_name, cols, as_dict=True) or {}
	if not row:
		return {}
	state = state_of(row.get("progress_state"))
	items = checklist_items(frappe.db.get_value(TASK, row.get("task"), "checklist_status"))
	evidence = frappe.db.get_all(EVIDENCE, filters={"parent": assignment_name, "parenttype": ASSIGNMENT},
	                             fields=compat.existing_fields(EVIDENCE, ("file", "file_url", "evidence_type",
	                                                                      "phase", "checklist_item",
	                                                                      "client_item_id", "captured_on")),
	                             order_by="idx asc") or []
	return {
		"saved_at": str(row.get("progress_saved_at") or "") or None,
		"evidence_count": len(evidence),
		"evidence": [dict(e) for e in evidence],
		"checklist": [{"item_name": i.get("item_name"), "done": bool(i.get("done")),
		               "required": bool(i.get("required", True)), **({"note": i["note"]} if i.get("note") else {})}
		              for i in items],
		"checklist_done": sum(1 for i in items if i.get("done")),
		"checklist_total": len(items),
		"findings_draft": row.get("findings_text") if state.get("findings_draft") else None,
		"gps": row.get("farm_location_gps") or None,
		"draft_answers": state.get("answers") or {},
	}


def keep_stale_evidence(task: dict, assignment: dict, worker: str, args: dict) -> dict:
	"""Late evidence for an assignment that ended without a completion: kept, never refused. v0.326.0.

	A phone's queued Complete can arrive after the job was rejected, reassigned or held. The photos go onto the
	worker's own most recent assignment of the task (rows added directly, so a closed assignment's controller is not
	re-run), every File is attached there and filed on the asset, and the mismatch is a comment on the task and an
	Error Log row an operator can find through MCP. Idempotent: a photo already on that assignment is not added
	twice. The answer lets the phone drop the item from its queue.
	"""
	from .tools import dispatch

	own = frappe.db.get_value(ASSIGNMENT, {"task": task["name"], "assigned_to": worker}, "name",
	                          order_by="creation desc") if worker else None
	target = str(own or "")
	mismatch = (f"{assignment.get('name')} is {assignment.get('state')}"
	            + (f" and held by {assignment.get('assigned_to')}" if assignment.get("assigned_to") != worker else ""))
	raw = args.get("evidence_files")
	entries = raw if isinstance(raw, list) else ([raw] if raw else [])
	added = 0
	if target:
		doc = frappe.get_doc(ASSIGNMENT, target)
		existing = {str(r.get("file") or r.get("file_url") or "") for r in doc.get("evidence_files") or []}
		from .tools import inspections

		for index, entry in enumerate(entries):
			try:
				row = inspections.normalise_evidence([entry], f"evidence[{index}]", enrich=True)[0]
			except Exception:  # noqa: BLE001 — one unreadable entry does not lose the others
				continue
			key = str(row.get("file") or row.get("file_url") or "")
			if key in existing:
				continue
			if isinstance(entry, dict):
				for tag in ("client_item_id", "checklist_item"):
					if entry.get(tag) and compat.has_field(EVIDENCE, tag):
						row[tag] = str(entry[tag])[:140]
			doc.append("evidence_files", row)
			existing.add(key)
			added += 1
			if row.get("file"):
				attach_to_assignment(row["file"], target)
		if added:
			# A closed assignment's rules are about its state, which this does not touch: it only gains photos.
			doc.flags.ignore_validate = True
			doc.save(ignore_permissions=True)
	for entry in entries:
		token = str((entry.get("file") or entry.get("file_token")) if isinstance(entry, dict) else entry or "")
		if token and frappe.db.exists("File", token):
			file_on_asset(token, str(task.get("asset") or ""))
	note = (f"Late evidence from the phone kept: {added} photo(s) onto {target or 'no assignment of theirs'}; "
	        f"the job was not completed because {mismatch}.")
	try:
		frappe.get_doc(TASK, task["name"]).add_comment("Info", note)
	except Exception:  # noqa: BLE001
		pass
	log_sync_refusal("complete_task_via_mobile", str(args.get("client_request_id") or ""), note, kept=True)
	return {"evidence_kept": added, "kept_on": target or None, "mismatch": mismatch}


_RECENT_REFUSALS: dict = {}


def log_sync_refusal(method: str, entry: str, reason: str, *, kept: bool = False) -> None:
	"""One Error Log row per refused (or only partly accepted) phone sync item, at most once an hour per item.

	v0.326.0. AFB-2026-00035's five completions were refused 30 times and Error Log stayed empty — the refusals were
	MCP Action Log rows, which nobody looking for errors opens. This puts each one where `list_error_logs` finds it,
	titled "FarmOps sync refused", with the phone's entry id. Never raises.
	"""
	try:
		key = f"farmops-sync-refusal:{method}:{entry or reason[:80]}"
		now = time.time()
		try:
			cache = frappe.cache()
			if cache.get_value(key):
				return
			cache.set_value(key, 1, expires_in_sec=3600)
		except Exception:  # noqa: BLE001 — no cache (a bare worker): this process remembers instead
			if now - _RECENT_REFUSALS.get(key, 0) < 3600:
				return
			_RECENT_REFUSALS[key] = now
		frappe.log_error(
			title=("FarmOps sync: late evidence kept" if kept else "FarmOps sync refused") + f" — {method}",
			message=f"entry: {entry or '(none)'}\nuser: {frappe.session.user}\n\n{reason}",
		)
	except Exception:  # noqa: BLE001
		pass
