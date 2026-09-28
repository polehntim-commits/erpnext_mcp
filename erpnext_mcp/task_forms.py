# SPDX-License-Identifier: MIT
"""A Farm Task's form: what the phone renders, what a completion must answer. v0.204.0.

docs/design/form_schema_and_labels.md §3. The vocabulary is `form_schema`; this
is where a TASK meets it — the snapshot (or the legacy checklist, for a task
raised before forms existed), the task facts conditions read, the approval
steps, the products the task handles and whether their labels were on hand.
"""

from __future__ import annotations

import json

import frappe

from . import compat, form_schema
from .errors import ToolError

FARM_TASK = "Farm Task"
PEST_CONTROL_GROUP = "Pest Control Products"


def _json(raw, fallback):
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
	except ValueError:
		return fallback
	return value if isinstance(value, type(fallback)) else fallback


def fields_of(task: dict) -> tuple:
	"""`(fields, legacy)` — the snapshot, or the checklist rendered as fields."""
	from .erpnext_mcp.doctype.farm_task.farm_task import checklist_items

	snapshot = _json(task.get("form_schema"), [])
	if snapshot:
		return snapshot, False
	return form_schema.legacy_fields(checklist_items(task.get("checklist_status"))), True


def context_of(task: dict, language: str = "en") -> dict:
	asset_type = ""
	if task.get("location_doctype") == "Asset Register" and task.get("location"):
		asset_type = str(frappe.db.get_value("Asset Register", task["location"], "asset_type") or "")
	elif task.get("asset"):
		asset_type = str(frappe.db.get_value("Asset Register", task["asset"], "asset_type") or "")
	return {
		"occupancy_at_creation": task.get("occupancy_at_creation") or "",
		"bait_placement": task.get("bait_placement") or "",
		"location_doctype": task.get("location_doctype") or "",
		"asset_type": asset_type,
		"task_type": task.get("task_type") or "",
		"template": task.get("template") or "",
		"language": language,
	}


def approvals_of(task: dict) -> dict:
	return _json(task.get("approvals"), {})


def approval_fields(fields: list) -> list:
	return [field for field in fields if field.get("type") == "approval"]


def pending_approvals(task: dict, *, before_start_only: bool = False) -> list:
	fields, _legacy = fields_of(task)
	done = approvals_of(task)
	answers = _json(task.get("form_answers"), {})
	context = context_of(task)
	out = []
	for field in approval_fields(fields):
		if before_start_only and field.get("before_start") is False:
			continue
		if field.get("show_if") and not form_schema.holds(field["show_if"], answers, context):
			continue
		if not done.get(field["key"]):
			out.append(field)
	return out


def stamp_label(task: dict) -> None:
	"""§4: whether every product's label is on file, stamped when the work starts. Never raises."""
	try:
		available, snapshot = label_state(task)
		if snapshot and compat.has_field(FARM_TASK, "label_available"):
			frappe.db.set_value(
				FARM_TASK,
				task["name"],
				{"label_available": 1 if available else 0, "label_snapshot": json.dumps(snapshot)},
				update_modified=False,
			)
	except Exception:  # pragma: no cover
		pass


def refuse_start_while_unapproved(task: dict) -> None:
	pending = pending_approvals(task, before_start_only=True)
	if pending:
		names = ", ".join(
			f"{form_schema.text_of(field.get('label'))} ({field.get('role')})" for field in pending
		)
		raise ToolError(
			f"{task.get('name')} cannot start before it is approved: {names}. The approver signs it "
			"on their phone (approve_task_step). Nothing was started."
		)


# ── completion ──────────────────────────────────────────────────────────────
def check_completion(task: dict, args: dict) -> dict | None:
	"""Validate `form_answers`; fold the answers into the legacy `checklist` argument.

	Returns the answers to store, or None when the task has no form and none
	was sent. Raises `ToolError` naming every problem, BEFORE anything is written.
	"""
	from .erpnext_mcp.doctype.farm_task.farm_task import checklist_items

	fields, _legacy = fields_of(task)
	sent = args.get("form_answers")
	pending = pending_approvals(task)
	if pending:
		raise ToolError(
			f"{task.get('name')} is waiting for approval: "
			+ ", ".join(f"{form_schema.text_of(f.get('label'))} ({f.get('role')})" for f in pending)
			+ ". Nothing was changed."
		)
	if sent in (None, "", {}):
		return None
	language = str(args.get("language") or "en")
	report = form_schema.check_answers(
		fields, sent, context_of(task, language), language=language, approvals=approvals_of(task)
	)
	if report["problems"]:
		raise ToolError(
			f"{task.get('name')} cannot be completed — the form is not finished:\n"
			+ "\n".join(f"  - {problem}" for problem in report["problems"])
			+ "\nNothing was changed."
		)
	answers = report["answers"]
	ticks = form_schema.ticks_from_answers(fields, answers, checklist_items(task.get("checklist_status")))
	if ticks:
		existing = args.get("checklist")
		existing = existing if isinstance(existing, list) else []
		args["checklist"] = [*existing, *ticks]
	return answers


def task_fields_after(task: dict, answers: dict | None) -> dict:
	"""What a completion writes onto the task from its answers and its products."""
	out: dict = {}
	fields, _legacy = fields_of(task)
	if answers is not None:
		out["form_answers"] = json.dumps(answers)
		items = form_schema.link_values(fields, answers, "Item")
		pest = [item for item in items if _group_of(item) == PEST_CONTROL_GROUP]
		if pest and not task.get("bait_product") and compat.has_field(FARM_TASK, "bait_product"):
			out["bait_product"] = pest[0]
		found = answers.get("rodent_activity_found")
		if found not in (None, "") and compat.has_field(FARM_TASK, "bait_activity"):
			yes = str(found).strip().lower() in ("yes", "true", "1", "activity")
			out["bait_activity"] = "Activity" if yes else "No activity"
	merged = {**task, **out}
	available, snapshot = label_state(merged)
	if snapshot:
		out["label_available"] = 1 if available else 0
		out["label_snapshot"] = json.dumps(snapshot)
	return out


def _group_of(item: str) -> str:
	return str(frappe.db.get_value("Item", item, "item_group") or "") if item else ""


# ── products and labels ─────────────────────────────────────────────────────
def products_of(task: dict) -> list:
	"""Every Item the task handles: its bait product, its Item answers, its materials."""
	from . import rodent_bait

	fields, _legacy = fields_of(task)
	answers = _json(task.get("form_answers"), {})
	seen: list = []
	for item in [
		task.get("bait_product"),
		*form_schema.link_values(fields, answers, "Item"),
		*rodent_bait.materials_items(task.get("materials_used")),
	]:
		item = str(item or "").strip()
		if item and item not in seen and frappe.db.exists("Item", item):
			seen.append(item)
	return seen


def label_state(task: dict) -> tuple:
	"""`(all_available, snapshot)` for the task's products. Snapshot empty when it handles none."""
	from . import product_labels

	snapshot = []
	for item in products_of(task):
		files = product_labels.label_files(item)
		pdf = next((row for row in files if row["kind"] == "epa_label_pdf"), None)
		snapshot.append(
			{
				"item_code": item,
				"epa_registration_number": str(
					frappe.db.get_value("Item", item, "epa_registration_number") or ""
				)
				if compat.has_field("Item", "epa_registration_number")
				else "",
				"label_pdf": pdf["file_name"] if pdf else None,
			}
		)
	return all(row["label_pdf"] for row in snapshot) if snapshot else False, snapshot


# ── what the phone receives ─────────────────────────────────────────────────
def phone_block(task: dict) -> dict:
	"""§3.1: `form`, `form_answers`, `approvals`, `products`, `sop` for `get_task`."""
	from . import product_labels, task_templates

	fields, legacy = fields_of(task)
	answers = _json(task.get("form_answers"), {})
	done = approvals_of(task)
	approvals = [
		{
			"key": field["key"],
			"label": field.get("label"),
			"role": field.get("role"),
			"before_start": field.get("before_start") is not False,
			"status": "approved" if done.get(field["key"]) else "pending",
			"approved_by": (done.get(field["key"]) or {}).get("approved_by"),
			"approved_at": (done.get(field["key"]) or {}).get("approved_at"),
		}
		for field in approval_fields(fields)
	]
	products = []
	for item in products_of(task):
		products.append(
			{
				"item_code": item,
				"item_name": frappe.db.get_value("Item", item, "item_name") or item,
				"label_available": product_labels.has_label_pdf(item),
			}
		)
	sop = {"en": None, "es": None}
	if task.get("template"):
		try:
			sop = task_templates.sop_documents(task["template"])
		except Exception:
			pass
	return {
		"form": form_schema.for_phone(fields, answers),
		"form_is_legacy_checklist": legacy,
		"form_context": context_of(task),
		"form_answers": answers,
		"approvals": approvals,
		"products": products,
		"sop": sop,
	}


# ── approvals ───────────────────────────────────────────────────────────────
def approve_step(task_name: str, key: str, user: str, signature: str = "") -> dict:
	"""Record one approval step by a user holding its role. §3.3."""
	if not frappe.db.exists(FARM_TASK, task_name):
		raise ToolError(f"no Farm Task called {task_name!r}. Nothing was approved.")
	from .tools.dispatch import task_row

	task = task_row(task_name)
	fields, _legacy = fields_of(task)
	field = next((f for f in approval_fields(fields) if f.get("key") == key), None)
	if not field:
		keys = ", ".join(f["key"] for f in approval_fields(fields)) or "none"
		raise ToolError(
			f"{task_name} has no approval step {key!r} (its steps: {keys}). Nothing was approved."
		)
	role = str(field.get("role") or "")
	from . import roles as role_lib

	held = set(frappe.get_roles(user) or []) | set(role_lib.all_roles_of(user) or [])
	if role not in held:
		raise ToolError(
			f"{user} does not hold {role}, which this step needs. Ask somebody who does. Nothing was approved."
		)
	done = approvals_of(task)
	if done.get(key):
		return {"task": task_name, "key": key, "already": True, **done[key]}
	done[key] = {
		"approved_by": user,
		"approved_at": frappe.utils.now(),
		"signature": signature or None,
		"role": role,
	}
	frappe.db.set_value(FARM_TASK, task_name, "approvals", json.dumps(done), update_modified=False)
	return {"task": task_name, "key": key, "already": False, **done[key]}


def record_label_viewed(task_name: str, item_code: str, user: str) -> dict:
	"""Append `{item_code, user, viewed_at}` to the task's `label_views`, once per user/item/day."""
	if not frappe.db.exists(FARM_TASK, task_name):
		raise ToolError(f"no Farm Task called {task_name!r}.")
	views = _json(frappe.db.get_value(FARM_TASK, task_name, "label_views"), [])
	today = str(frappe.utils.today())
	for row in views:
		if (
			row.get("item_code") == item_code
			and row.get("user") == user
			and str(row.get("viewed_at") or "")[:10] == today
		):
			return {"task": task_name, "item_code": item_code, "recorded": False, "views": len(views)}
	views.append({"item_code": item_code, "user": user, "viewed_at": frappe.utils.now()})
	frappe.db.set_value(FARM_TASK, task_name, "label_views", json.dumps(views), update_modified=False)
	return {"task": task_name, "item_code": item_code, "recorded": True, "views": len(views)}
