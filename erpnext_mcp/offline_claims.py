# SPDX-License-Identifier: MIT
"""A claim made with no signal. v0.261.0 (Tim, 2026-10-06).

THE RULE: THE FIRST CLAIM TO REACH THE SERVER WINS, AND THE SECOND WORKER'S TIME
IS NEVER DISCARDED. Two phones offline in the same block can both take the same
task from their cached pool. When the second claim syncs, the task already has a
holder; refusing it — as an online claim is refused — would throw away the
minutes and the photographs of somebody who did the work.

So the late claim becomes a SECOND CLAIM: its own Farm Task Assignment on the
same task, `second_claim` set, `claim_conflict` saying in plain words what
happened. It starts, pauses, resumes and completes like any assignment and its
minutes count wherever an assignment's minutes count (`payroll_gl` excludes no
state). What it never does is move the TASK: the task's state, holder, stock
drawdown, spray windows and compliance record stay the winner's. A supervisor
sees "claimed offline by two people" on both assignments and decides.

The same door takes an offline claim the server would otherwise refuse for a
rule (a hold, a qualification, a draft, a crew task): the work happened, so it
is recorded as a second claim with the reason, and the task is left as it was.
"""

from __future__ import annotations

import frappe

from . import compat

FARM_TASK = "Farm Task"
FARM_TASK_ASSIGNMENT = "Farm Task Assignment"

#: What the flag says, for the board and the review. The words Tim asked for.
CONFLICT_HEADLINE = "Claimed offline by two people"


def is_second(row) -> bool:
	"""True for an assignment recorded as a second claim."""
	if not row:
		return False
	value = row.get("second_claim") if isinstance(row, dict) else getattr(row, "second_claim", 0)
	return compat.checked(value)


def ready() -> bool:
	"""The columns exist (a site mid-migrate has neither)."""
	return compat.has_field(FARM_TASK_ASSIGNMENT, "second_claim")


def conflict_text(holder_name: str, reason: str = "") -> str:
	"""The sentence on the assignment, and what the phone shows."""
	if reason:
		return f"Claimed offline, but this claim could not hold the task: {reason} Your time and evidence are kept."
	return (
		f"{CONFLICT_HEADLINE}: {holder_name or 'someone else'}'s claim reached the server first. "
		"Your time and evidence are kept on this task as a second worker."
	)


def record_second_claim(task_doc, worker: str, worker_name: str, claimed_at: str, holder_name: str, reason: str = "") -> dict:
	"""The late claim's own assignment. Never touches the task.

	Idempotent per (task, worker): a worker who already has a live or finished
	second claim on this task gets that one back rather than a duplicate.
	"""
	existing = frappe.db.get_value(
		FARM_TASK_ASSIGNMENT,
		{"task": task_doc.name, "assigned_to": worker, "second_claim": 1},
		"name",
	)
	if existing:
		return dict(frappe.get_doc(FARM_TASK_ASSIGNMENT, existing).as_dict())
	doc = frappe.get_doc(
		{
			"doctype": FARM_TASK_ASSIGNMENT,
			"task": task_doc.name,
			"task_name": task_doc.get("task_name"),
			"company": task_doc.get("company"),
			"assigned_to": worker,
			"assigned_to_name": worker_name,
			"state": "Claimed",
			"claimed_at": claimed_at or frappe.utils.now(),
			"dispatched_by_foreman": 0,
			"second_claim": 1,
			"claimed_offline": 1,
			"device_claimed_at": claimed_at or None,
			"claim_conflict": conflict_text(holder_name, reason),
		}
	)
	doc.insert(ignore_permissions=True)
	_flag_the_winner(task_doc.name, worker_name)
	return dict(doc.as_dict())


def _flag_the_winner(task: str, second_name: str) -> None:
	"""The holder's assignment says it too, so the review sees the pair."""
	holder = frappe.db.get_value(
		FARM_TASK_ASSIGNMENT,
		{"task": task, "second_claim": 0, "state": ("not in", ["Rejected", "Merged"])},
		"name",
		order_by="creation desc",
	)
	if not holder:
		return
	text = f"{CONFLICT_HEADLINE}: {second_name} also claimed this task offline and is recorded as a second worker."
	current = str(frappe.db.get_value(FARM_TASK_ASSIGNMENT, holder, "claim_conflict") or "")
	if second_name not in current:
		frappe.db.set_value(FARM_TASK_ASSIGNMENT, holder, "claim_conflict", f"{current}\n{text}".strip())


def complete_second(assignment: dict, args: dict) -> dict:
	"""Finish a second claim: the minutes, the words and the files, on its own row.

	No evidence contract, no compliance record, no stock, no spray window — those
	belong to the task, and the task is the winner's. A resend of a completion
	already recorded returns it unchanged.
	"""
	from .args import as_int, as_str
	from .tools import dispatch, inspections

	doc = frappe.get_doc(FARM_TASK_ASSIGNMENT, assignment["name"])
	if doc.state == "Completed":
		return dict(doc.as_dict())
	completed_at = as_str(args, "completed_at") or frappe.utils.now()
	if doc.state in ("In-Progress",):
		dispatch._close_segment(doc, completed_at, dispatch.SEGMENT_COMPLETION)
	doc.state = "Completed"
	doc.completed_at = completed_at
	doc.paused_at = None
	doc.actual_duration_minutes = as_int(args, "actual_duration_minutes") or dispatch.active_minutes(doc)
	for key in ("completion_narrative", "findings_text", "witness", "farm_location_gps"):
		if args.get(key) is not None:
			doc.set(key, str(args.get(key)))
	for row in inspections.normalise_evidence(args.get("evidence_files"), "evidence_files", enrich=True) or []:
		doc.append("evidence_files", dict(row))
	doc.save(ignore_permissions=True)
	return dict(doc.as_dict())


def conflicts_on(task: str) -> list:
	"""Every flagged assignment on a task, for the task detail and the review."""
	if not ready():
		return []
	rows = frappe.db.get_all(
		FARM_TASK_ASSIGNMENT,
		filters={"task": task, "claim_conflict": ("is", "set")},
		fields=["name", "assigned_to", "assigned_to_name", "state", "second_claim", "device_claimed_at",
		        "claim_conflict", "actual_duration_minutes"],
		order_by="creation asc",
		limit=50,
	) or []
	return [dict(row) for row in rows]


# ── review: a CCF compliance item, like punch review (v0.251.0) ─────────────
REVIEW_ROLES = ("Foreman", "Farm Manager", "HR Manager", "HR User", "System Manager")
RULE_ID = "task_claimed_offline_twice"


def unreviewed(task: str) -> list:
	"""Flagged assignments on a task nobody has reviewed yet."""
	return [row for row in conflicts_on(task) if not compat.checked(frappe.db.get_value(FARM_TASK_ASSIGNMENT, row["name"], "claim_reviewed"))]


def claim_values(subject: dict, ctx: dict) -> dict:
	"""The `claim.*` facts on a Farm Task."""
	if not ready() or not subject.get("name"):
		return {"offline_conflicts": 0}
	return {"offline_conflicts": len(unreviewed(subject["name"]))}


def register(ccf) -> None:
	p = ccf._p
	ccf.register(
		ccf.Provider(
			"claim",
			{"offline_conflicts": p("number", "Assignments flagged 'claimed offline by two people' that nobody has reviewed.", example=0)},
			lambda subject, ctx: claim_values(subject, ctx),
			past=False,
			description="Offline claims on a Farm Task (v0.261.0): two people claimed it with no signal.",
		)
	)


def rule_spec() -> dict:
	reason = {"en": "Claimed offline by two people", "es": "Dos personas la tomaron sin conexión"}
	return {
		"rule_id": RULE_ID,
		"title": "Task claimed offline by two people",
		"category": "Workforce",
		"target_doctype": FARM_TASK,
		"condition_tree": {"id": RULE_ID, "path": "claim.offline_conflicts", "op": "eq", "value": 0, "reason": reason},
		"evaluation": {"when": ["sweep"]},
		"actions": [{"type": "alert"}],
		"default_severity": "Warning",
		"due_date_mode": "Today",
		"message_template": "{{ name }} ({{ task_name }}): {{ hold }}",
		"kairotic_gate_description": "Raised while a task has an unreviewed second claim; clears when a supervisor reviews it.",
		"purpose": "Both workers' time is kept; a supervisor decides whose evidence stands and settles the pay split.",
		"regimes": ["Internal"],
		"extra_parameters": {"notify_roles": list(REVIEW_ROLES)},
		"scope_filters": [],
		"enabled": 1,
		"authored_by": "System",
	}


def seed() -> list:
	"""Create-only; never raises."""
	from . import compliance_rules

	if not compat.doctype_exists(compliance_rules.DOCTYPE):
		return []
	try:
		if frappe.db.exists(compliance_rules.DOCTYPE, {"rule_id": RULE_ID}):
			return []
		now = frappe.utils.now()
		compliance_rules.build_rule({**rule_spec(), "human_approved_by": "Administrator", "human_approved_on": now}).insert(
			ignore_permissions=True
		)
		return [RULE_ID]
	except Exception:
		frappe.log_error(title="offline claim rule not seeded", message=frappe.get_traceback())
		return []


def review(user: str, task: str, note: str = "") -> dict:
	"""A supervisor has looked: every flagged assignment on the task is marked reviewed."""
	if not set(frappe.get_roles(user)) & set(REVIEW_ROLES):
		raise frappe.PermissionError(f"Reviewing a claim conflict is for {', '.join(REVIEW_ROLES)}.")
	rows = unreviewed(task)
	for row in rows:
		values = {"claim_reviewed": 1, "claim_reviewed_by": user}
		if note:
			values["claim_conflict"] = f"{row.get('claim_conflict') or ''}\nReviewed by {user}: {note}".strip()
		frappe.db.set_value(FARM_TASK_ASSIGNMENT, row["name"], values)
	return {"task": task, "reviewed": [row["name"] for row in rows]}
