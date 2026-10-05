# SPDX-License-Identifier: MIT
"""Knowledge checks (quizzes) and trainer sign-off. v0.247.0. docs/design/training_quiz_and_video.md §2
(approved; queue item 7; decisions 42–45).

* A QUIZ IS A CONFIG KIND ("Quiz" in Farm Config Version), keyed per course, versioned, drafted and
  previewed over MCP, published by a person (decision 5). Its body:

      {schema_version: 1, key, training_type, title: {en, es}, pass_pct: 80, shuffle: true, retakes: true,
       topics_covered: [...], questions: [{id, type: choice | true_false | short, prompt: {en, es},
       choices: [{id, text: {en, es}}], answer, explanation: {en, es}}]}

  Shuffle is on by default (decision 43; the phone never shuffles true/false). Videos before the quiz are
  not required unless the course sets a minimum % (decision 44, v0.246.0).
* AN ATTEMPT is Training Evidence (decision 42): the version it was taken on, the answers, the server's
  grade (the phone's own is only a preview), short answers awaiting a trainer, and the video-minimum result.
* PASSING IS NOT COMPLETING. A pass with every short answer marked is "Ready for sign-off"; a person signs it
  off in the Desk or on the phone (decision 45) — a Signing Evidence row (role Trainer) — and only then is the
  Employee Training Record filed. An attempt below the video minimum can be signed off only with a reason.
"""

from __future__ import annotations

import json

import frappe

from . import compat, settings, training_videos

KIND = "Quiz"
EVIDENCE = training_videos.EVIDENCE
ATTEMPT = training_videos.ATTEMPT
TYPES = ("choice", "true_false", "short")
PASSED, FAILED, AWAITING = "Passed", "Failed", "Awaiting marking"
READY, SIGNED = "Ready for sign-off", "Signed off"
SIGN_ROLE = "Trainer"
SIGNOFF_ROLES = ("System Manager", "HR Manager", "Farm Manager")


def enabled() -> bool:
	return bool(int(settings.get_settings().get("knowledge_checks_enabled") or 0))


def _text(value, path, errors, warnings, for_publish):
	if not isinstance(value, dict) or not str(value.get("en") or "").strip():
		errors.append(f"{path} needs English text ({{\"en\": …, \"es\": …}}).")
	elif not str(value.get("es") or "").strip():
		(errors if for_publish else warnings).append(f"{path} has no Spanish.")


def validate(body: dict, key: str = "", for_publish: bool = False) -> dict:
	errors: list = []
	warnings: list = []
	training_type = str(body.get("training_type") or "")
	if not training_type:
		errors.append("training_type (the course) is required.")
	elif compat.doctype_exists("Training Type") and not frappe.db.exists("Training Type", training_type):
		errors.append(f"no Training Type {training_type!r}.")
	_text(body.get("title"), "title", errors, warnings, for_publish)
	pass_pct = body.get("pass_pct", 80)
	if not isinstance(pass_pct, (int, float)) or not 1 <= pass_pct <= 100:
		errors.append("pass_pct is 1 to 100.")
	if not [t for t in body.get("topics_covered") or [] if str(t).strip()]:
		(errors if for_publish else warnings).append("topics_covered is needed to file the training record.")
	questions = body.get("questions") or []
	if not questions:
		errors.append("questions: at least one.")
	seen = set()
	for index, q in enumerate(questions):
		where = f"questions[{index}]"
		qid = str(q.get("id") or "")
		if not qid or qid in seen:
			errors.append(f"{where}.id is required and unique.")
		seen.add(qid)
		kind = q.get("type")
		if kind not in TYPES:
			errors.append(f"{where}.type is one of {', '.join(TYPES)}.")
			continue
		_text(q.get("prompt"), f"{where}.prompt", errors, warnings, for_publish)
		if q.get("explanation") is not None:
			_text(q.get("explanation"), f"{where}.explanation", errors, warnings, for_publish)
		if kind == "choice":
			choices = q.get("choices") or []
			ids = [str(c.get("id") or "") for c in choices]
			if len(choices) < 2 or len(set(ids)) != len(ids) or "" in ids:
				errors.append(f"{where}.choices: two or more, each with a unique id.")
			for c_index, choice in enumerate(choices):
				_text(choice.get("text"), f"{where}.choices[{c_index}].text", errors, warnings, for_publish)
			if str(q.get("answer") or "") not in ids:
				errors.append(f"{where}.answer must be one of its choice ids.")
		elif kind == "true_false" and not isinstance(q.get("answer"), bool):
			errors.append(f"{where}.answer is true or false.")
		elif kind == "short" and q.get("answer") not in (None, ""):
			warnings.append(f"{where}: a short answer is marked by a trainer; its 'answer' is shown to them as a guide.")
	return {"errors": errors, "warnings": warnings}


def key_for(training_type: str) -> str:
	import re

	slug = re.sub(r"[^a-z0-9]+", "_", str(training_type).lower()).strip("_")
	return (slug or "course")[:60]


def body_for(training_type: str, version=None) -> tuple:
	"""(doc, body) of the published quiz for a course, or a given version."""
	from . import phone_config

	key = key_for(training_type)
	if version not in (None, ""):
		doc = phone_config.doc_of(KIND, key, version)
	else:
		doc = phone_config.doc_of(KIND, key, status=phone_config.PUBLISHED)
	return (doc, phone_config.body_of(doc)) if doc is not None else (None, None)


def grade(body: dict, answers: dict, marks: dict | None = None) -> dict:
	marks = marks or {}
	per, right, total, pending = [], 0, 0, 0
	for q in body.get("questions") or []:
		qid = str(q["id"])
		given = (answers or {}).get(qid)
		total += 1
		if q["type"] == "short":
			mark = marks.get(qid)
			if mark is None:
				pending += 1
				per.append({"id": qid, "given": given, "result": "pending"})
				continue
			ok = bool(mark)
		elif q["type"] == "true_false":
			ok = isinstance(given, bool) and given == q["answer"] or str(given).lower() == str(q["answer"]).lower()
		else:
			ok = str(given or "") == str(q["answer"])
		right += 1 if ok else 0
		per.append({"id": qid, "given": given, "result": "right" if ok else "wrong",
		            **({} if ok else {"explanation": q.get("explanation")})})
	pass_pct = float(body.get("pass_pct", 80))
	if pending:
		# What is known so far; the result waits for the trainer.
		score = round(right / total * 100, 1) if total else 0.0
		result = AWAITING
	else:
		score = round(right / total * 100, 1) if total else 0.0
		result = PASSED if score >= pass_pct else FAILED
	return {"questions": per, "right": right, "total": total, "pending": pending, "score_pct": score,
	        "pass_pct": pass_pct, "result": result}


def _signoff_status(result: str, minimum_met: bool) -> str:
	return READY if result == PASSED else ""


def submit(*, employee: str, training_type: str, answers: dict, version=None, started_at: str = "",
           finished_at: str = "", device: str = "", client_request_id: str = "") -> dict:
	if not enabled():
		raise ValueError("knowledge checks are switched off (ERPNext MCP Settings → Knowledge Checks).")
	if not frappe.db.exists("Employee", employee):
		raise ValueError(f"no Employee {employee!r}.")
	if client_request_id:
		again = frappe.db.get_value(EVIDENCE, {"client_request_id": client_request_id}, "name")
		if again:
			return {**describe(again), "duplicate": True}
	doc, body = body_for(training_type, version)
	if body is None:
		raise ValueError(f"no published quiz for {training_type}" + (f" version {version}" if version else "") + ".")
	if not body.get("retakes", True):
		passed = frappe.db.exists(EVIDENCE, {"evidence_kind": ATTEMPT, "employee": employee,
		                                     "training_type": training_type, "result": PASSED})
		if passed:
			raise ValueError("this course allows one pass; it has been passed.")
	graded = grade(body, answers or {})
	videos = training_videos.progress(employee, training_type) if training_videos.installed() else {"minimum_met": True}
	minimum = "Off" if not videos.get("course_minimum_pct") and all(not v.get("minimum_pct") for v in videos.get("videos", [])) \
		else ("Met" if videos.get("minimum_met") else "Not met")
	person = frappe.db.get_value("Employee", employee, ["employee_name", "company"], as_dict=True) or {}
	ev = frappe.get_doc(
		{
			"doctype": EVIDENCE,
			"evidence_kind": ATTEMPT,
			"employee": employee,
			"employee_name": person.get("employee_name") or employee,
			"company": person.get("company") or None,
			"training_type": training_type,
			"quiz_version": int(doc.version),
			"answers": json.dumps(answers or {}),
			"grading": json.dumps(graded["questions"]),
			"score_pct": graded["score_pct"],
			"short_pending": graded["pending"],
			"result": graded["result"],
			"video_minimum": minimum,
			"signoff_status": _signoff_status(graded["result"], minimum != "Not met"),
			"started_at": started_at or None,
			"finished_at": finished_at or None,
			"received_at": frappe.utils.now(),
			"device": device or None,
			"client_request_id": client_request_id or None,
		}
	).insert(ignore_permissions=True)
	return describe(ev.name)


def mark(name: str, marks: dict, user: str) -> dict:
	ev = frappe.get_doc(EVIDENCE, name)
	if ev.evidence_kind != ATTEMPT:
		raise ValueError(f"{name} is not a quiz attempt.")
	if ev.signoff_status == SIGNED:
		raise ValueError(f"{name} is signed off; it cannot be re-marked.")
	_doc, body = body_for(ev.training_type, ev.quiz_version)
	shorts = {str(q["id"]) for q in body.get("questions") or [] if q["type"] == "short"}
	unknown = [k for k in (marks or {}) if k not in shorts]
	if unknown:
		raise ValueError(f"not short-answer questions: {', '.join(unknown)}.")
	merged = {**json.loads(ev.get("marks") or "{}"), **{k: bool(v) for k, v in (marks or {}).items()}}
	graded = grade(body, json.loads(ev.answers or "{}"), merged)
	ev.marks = json.dumps(merged)
	ev.marked_by = user
	ev.grading = json.dumps(graded["questions"])
	ev.score_pct = graded["score_pct"]
	ev.short_pending = graded["pending"]
	ev.result = graded["result"]
	ev.signoff_status = _signoff_status(graded["result"], ev.video_minimum != "Not met")
	ev.flags.ignore_permissions = True
	ev.save(ignore_permissions=True)
	return describe(name)


def can_sign_off(user: str) -> bool:
	return user == "Administrator" or bool(set(frappe.get_roles(user) or []) & set(SIGNOFF_ROLES))


def sign_off(name: str, user: str, reason: str = "", method: str = "App sign-in") -> dict:
	"""A person's act (Desk or phone). Files the Employee Training Record."""
	ev = frappe.get_doc(EVIDENCE, name)
	if not can_sign_off(user):
		raise PermissionError(f"{user} cannot sign off training — HR Manager, Farm Manager or System Manager can.")
	if ev.signoff_status != READY:
		raise ValueError(f"{name} is not ready for sign-off ({ev.result}).")
	if ev.video_minimum == "Not met" and not str(reason or "").strip():
		raise ValueError("the course's video minimum was not met; give a reason to sign off anyway.")
	_doc, body = body_for(ev.training_type, ev.quiz_version)
	now = frappe.utils.now()
	from .tools import training as training_tool

	course = frappe.get_doc("Training Type", ev.training_type)
	regimes = [r.get("regime") for r in course.get("regimes") or [] if r.get("regime")] or ["Internal"]
	record = training_tool.record_training(
		{
			"employee": ev.employee,
			"training_type": ev.training_type,
			"company": ev.company,
			"completed_date": str(now)[:10],
			"regimes": regimes,
			"content_topics_covered": body.get("topics_covered") or [ev.training_type],
			"training_source": "Internal",
			"notes": f"Knowledge check v{ev.quiz_version}: {ev.score_pct:g}% ({ev.result}). Signed off by {user}."
			         + (f" Video minimum not met — {reason}." if ev.video_minimum == "Not met" else ""),
		},
		authorized_actor=user,
	).data
	record_name = record.get("name") or record.get("record")
	if record_name and compat.has_field("Employee Training Record", "supervisor_reviewed_by"):
		frappe.db.set_value("Employee Training Record", record_name,
		                    {"supervisor_reviewed_by": user, "supervisor_reviewed_on": now})
	evidence = _sign(ev, user, method, now)
	ev.signoff_status = SIGNED
	ev.signed_off_by = user
	ev.signed_off_at = now
	ev.signing_evidence = evidence or None
	ev.override_reason = str(reason or "").strip() or None
	ev.training_record = record_name or None
	ev.flags.ignore_permissions = True
	ev.save(ignore_permissions=True)
	return describe(name)


def _sign(ev, user: str, method: str, now: str) -> str:
	try:
		employee = frappe.db.get_value("Employee", {"user_id": user}, ["name", "employee_name"], as_dict=True) or {}
		return frappe.get_doc(
			{
				"doctype": "Signing Evidence",
				"signer": employee.get("name") or None,
				"signer_name": employee.get("employee_name") or user,
				"signer_user": user,
				"verification_method": method if method in ("Face ID (device key)", "App sign-in") else "App sign-in",
				"signed_at": now,
				"status": "Recorded",
				"company": ev.company or None,
				"document_type": EVIDENCE,
				"document_name": ev.name,
				"signature_role": SIGN_ROLE,
				"signature_field": "training sign-off",
			}
		).insert(ignore_permissions=True).name
	except Exception:
		frappe.log_error(title="training sign-off: signing evidence not written", message=frappe.get_traceback())
		return ""


def describe(name: str) -> dict:
	row = frappe.get_doc(EVIDENCE, name).as_dict()
	return {
		"evidence": name,
		"kind": row.get("evidence_kind"),
		"employee": row.get("employee"),
		"employee_name": row.get("employee_name"),
		"training_type": row.get("training_type"),
		"quiz_version": row.get("quiz_version"),
		"score_pct": row.get("score_pct"),
		"result": row.get("result"),
		"short_pending": int(row.get("short_pending") or 0),
		"video_minimum": row.get("video_minimum"),
		"signoff_status": row.get("signoff_status") or None,
		"signed_off_by": row.get("signed_off_by"),
		"training_record": row.get("training_record"),
		"questions": json.loads(row.get("grading") or "[]"),
	}


def results(employee: str = "", training_type: str = "", status: str = "", limit: int = 100) -> list:
	filters = {"evidence_kind": ATTEMPT}
	if employee:
		filters["employee"] = employee
	if training_type:
		filters["training_type"] = training_type
	if status == "ready":
		filters["signoff_status"] = READY
	elif status == "marking":
		filters["result"] = AWAITING
	elif status:
		filters["result"] = status
	rows = frappe.db.get_all(EVIDENCE, filters=filters, fields=["name"], order_by="creation desc", limit=limit) or []
	return [describe(r["name"]) for r in rows]


@frappe.whitelist(methods=["POST"])
def desk_sign_off(name: str, reason: str = "") -> dict:
	try:
		return sign_off(name, frappe.session.user, reason)
	except (ValueError, PermissionError) as exc:
		frappe.throw(str(exc))
