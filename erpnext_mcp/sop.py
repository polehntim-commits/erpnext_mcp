# SPDX-License-Identifier: MIT
"""SOP review and approval. v0.245.0. docs/design/sop_review_and_approval.md (approved; queue item 6).

AN SOP IS A COMPLIANCE POLICY — no new doctype. A version moves Draft → In Review → Approved → Superseded
(Retired kept). "Active", from before, reads as Approved.

* WHO APPROVES (decisions 38, 40): by the work or the position the SOP covers. The SOP lists the task types
  (`covers_task_types`) and positions (`covers_positions`) it governs; ERPNext MCP Settings → SOP Approver
  Rules maps each to approvers, one rule per line:

      task_type: Spray = tim@example.com
      position: Tractor Operator = tim@example.com, manager@example.com
      * = tim@example.com

  At submit the matching approvers are written onto the SOP (its Approvers table, editable in the Desk until
  someone approves). Every required approver must approve; with none marked required, any one is enough.
* APPROVAL IS HUMAN-ONLY: the Desk button or the phone, never MCP. Each approval is a Signing Evidence row
  (role Approver) on the SOP. "Request changes" sends it back to Draft with the note.
* A NEW VERSION supersedes the one it names in `supersedes` when it is approved.
* GATING is a CCF rule, not code here: the `sop` provider says how many SOPs covering a task's type are not
  approved; the preset Work Timing rule (seeded OFF, Advisory) holds such work until they are.
"""

from __future__ import annotations

import frappe

from . import compat, settings

DOCTYPE = "Compliance Policy"
DRAFT, IN_REVIEW, APPROVED, ACTIVE, SUPERSEDED, RETIRED = (
	"Draft", "In Review", "Approved", "Active", "Superseded", "Retired",
)
LIVE = (APPROVED, ACTIVE)
SIGN_ROLE = "Approver"


def installed() -> bool:
	return compat.has_field(DOCTYPE, "covers_task_types")


def _lines(value) -> list:
	if isinstance(value, (list, tuple)):
		items = value
	else:
		items = str(value or "").replace(",", "\n").splitlines()
	return [str(i).strip() for i in items if str(i).strip()]


# ── approver rules ──────────────────────────────────────────────────────────
def parse_rules(raw: str) -> tuple[list, list]:
	"""(rules, problems). A rule: {scope: task_type|position|*, value, approvers: [user]}."""
	rules, problems = [], []
	for number, line in enumerate(str(raw or "").splitlines(), 1):
		line = line.strip()
		if not line or line.startswith("#"):
			continue
		if "=" not in line:
			problems.append(f"line {number}: no '=' — write 'task_type: Spray = user@example.com'.")
			continue
		left, right = (part.strip() for part in line.split("=", 1))
		approvers = [u.strip() for u in right.split(",") if u.strip()]
		if not approvers:
			problems.append(f"line {number}: no approver after '='.")
			continue
		if left == "*":
			rules.append({"scope": "*", "value": "", "approvers": approvers})
			continue
		scope, _, value = (part.strip() for part in left.partition(":"))
		scope = scope.lower().replace(" ", "_")
		if scope not in ("task_type", "position") or not value:
			problems.append(f"line {number}: start with 'task_type: …', 'position: …' or '*'.")
			continue
		rules.append({"scope": scope, "value": value, "approvers": approvers})
	return rules, problems


def approvers_for(policy: dict) -> list:
	"""The users who approve this SOP, from what it covers (decision 40). Falls back to '*'."""
	rules, _ = parse_rules(settings.get_settings().get("sop_approver_rules") or "")
	task_types = {t.lower() for t in _lines(policy.get("covers_task_types"))}
	positions = {p.lower() for p in _lines(policy.get("covers_positions"))}
	found = []
	for rule in rules:
		if (rule["scope"] == "task_type" and rule["value"].lower() in task_types) or (
			rule["scope"] == "position" and rule["value"].lower() in positions
		):
			found += rule["approvers"]
	if not found:
		for rule in rules:
			if rule["scope"] == "*":
				found += rule["approvers"]
	out = []
	for user in found:
		if user not in out:
			out.append(user)
	return out


# ── the lifecycle ───────────────────────────────────────────────────────────
def _row_get(row, field):
	return row.get(field) if hasattr(row, "get") else None


def _row_set(row, field, value):
	if hasattr(row, "set") and not isinstance(row, dict):
		row.set(field, value)
	else:
		row[field] = value


def submit(name: str, user: str) -> dict:
	"""Draft → In Review. Fills the Approvers table from the rules when it is empty."""
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.status not in (DRAFT,):
		raise ValueError(f"{name} is {doc.status}; only a Draft goes to review.")
	if not doc.get("approvers"):
		users = approvers_for(dict(doc.as_dict()))
		if not users:
			raise ValueError(
				"no approver for what this SOP covers. Set covers_task_types / covers_positions, and an approver "
				"rule in ERPNext MCP Settings → SOP Approver Rules (e.g. '* = tim@…')."
			)
		for user_id in users:
			doc.append("approvers", {"approver": user_id, "required": 1})
	doc.status = IN_REVIEW
	doc.review_note = None
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	_notify([_row_get(r, "approver") for r in doc.get("approvers") or []],
	        f"SOP to review: {doc.policy_name} v{doc.version or ''}", name)
	return describe(name)


def _approver_row(doc, user: str):
	for row in doc.get("approvers") or []:
		if _row_get(row, "approver") == user:
			return row
	return None


def approve(name: str, user: str, method: str = "App sign-in") -> dict:
	"""One approver's approval — a person in the Desk or on the phone. Approved when all required have."""
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.status != IN_REVIEW:
		raise ValueError(f"{name} is {doc.status}; only an SOP In Review can be approved.")
	row = _approver_row(doc, user)
	if row is None:
		raise PermissionError(f"{user} is not an approver of {name}.")
	if _row_get(row, "approved_at"):
		raise ValueError(f"{user} already approved {name}.")
	now = frappe.utils.now()
	evidence = _sign(doc, user, method, now)
	_row_set(row, "approved_at", now)
	_row_set(row, "signing_evidence", evidence)
	rows = doc.get("approvers") or []
	required = [r for r in rows if int(_row_get(r, "required") or 0)]
	done = all(_row_get(r, "approved_at") for r in required) if required else True
	superseded = None
	if done:
		doc.status = APPROVED
		doc.effective_date = doc.effective_date or str(frappe.utils.today())[:10]
		if doc.supersedes and frappe.db.exists(DOCTYPE, doc.supersedes):
			frappe.db.set_value(DOCTYPE, doc.supersedes, {"status": SUPERSEDED, "superseded_by": doc.name})
			superseded = doc.supersedes
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	out = describe(name)
	out["approved_now"] = done
	out["superseded"] = superseded
	return out


def request_changes(name: str, user: str, note: str) -> dict:
	note = str(note or "").strip()
	if not note:
		raise ValueError("say what to change.")
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.status != IN_REVIEW:
		raise ValueError(f"{name} is {doc.status}; changes are requested on an SOP In Review.")
	if _approver_row(doc, user) is None:
		raise PermissionError(f"{user} is not an approver of {name}.")
	doc.status = DRAFT
	doc.review_note = f"{user}: {note}"
	for row in doc.get("approvers") or []:
		_row_set(row, "approved_at", None)
		_row_set(row, "signing_evidence", None)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	if doc.policy_owner:
		_notify_users([doc.policy_owner], f"Changes requested: {doc.policy_name}", note)
	return describe(name)


def _sign(doc, user: str, method: str, now: str) -> str:
	try:
		employee = frappe.db.get_value("Employee", {"user_id": user}, ["name", "employee_name"], as_dict=True) or {}
		ev = frappe.get_doc(
			{
				"doctype": "Signing Evidence",
				"signer": employee.get("name") or None,
				"signer_name": employee.get("employee_name") or user,
				"signer_user": user,
				"verification_method": method if method in ("Face ID (device key)", "App sign-in") else "App sign-in",
				"signed_at": now,
				"status": "Recorded",
				"company": doc.company or None,
				"document_type": DOCTYPE,
				"document_name": doc.name,
				"signature_role": SIGN_ROLE,
				"signature_field": f"approval of version {doc.version or ''}".strip(),
			}
		).insert(ignore_permissions=True)
		return ev.name
	except Exception:
		frappe.log_error(title="SOP approval: signing evidence not written", message=frappe.get_traceback())
		return ""


def _notify(users, title: str, name: str) -> None:
	_notify_users(users, title, f"Open {name} to approve or request changes.")


def _notify_users(users, title: str, body: str) -> None:
	try:
		from .services import push

		employees = [frappe.db.get_value("Employee", {"user_id": u}, "name") for u in users if u]
		push.send_push_to_employees([e for e in employees if e], {"aps": {"alert": {"title": title, "body": body}},
		                                                          "kind": "sop_review"})
	except Exception:
		frappe.log_error(title="SOP notice not pushed", message=frappe.get_traceback())


# ── reads ───────────────────────────────────────────────────────────────────
def describe(name: str) -> dict:
	doc = frappe.get_doc(DOCTYPE, name)
	row = doc.as_dict()
	approvers = [
		{"approver": _row_get(r, "approver"), "required": bool(int(_row_get(r, "required") or 0)),
		 "approved_at": _row_get(r, "approved_at"), "signing_evidence": _row_get(r, "signing_evidence")}
		for r in doc.get("approvers") or []
	]
	return {
		"name": name,
		"policy_name": row.get("policy_name"),
		"version": row.get("version"),
		"status": row.get("status"),
		"approved": row.get("status") in LIVE,
		"covers_task_types": _lines(row.get("covers_task_types")),
		"covers_positions": _lines(row.get("covers_positions")),
		"approvers": approvers,
		"waiting_on": [a["approver"] for a in approvers if a["required"] and not a["approved_at"]],
		"review_note": row.get("review_note"),
		"supersedes": row.get("supersedes"),
		"superseded_by": row.get("superseded_by"),
	}


def covering(task_type: str) -> list:
	"""Current SOPs (not superseded or retired) that cover a task type."""
	if not installed() or not task_type:
		return []
	rows = frappe.db.get_all(
		DOCTYPE,
		filters={"status": ("in", [DRAFT, IN_REVIEW, APPROVED, ACTIVE])},
		fields=["name", "policy_name", "version", "status", "covers_task_types", "supersedes", "creation"],
		limit=500,
	)
	wanted = task_type.lower()
	found = [dict(r) for r in rows or [] if wanted in {t.lower() for t in _lines(r.get("covers_task_types"))}]
	# A draft of a new version does not un-approve the version in force. Versions are separate records
	# chained by `supersedes`; per chain, the newest live version answers, and with none live, the newest.
	parent = {str(r["name"]): str(r.get("supersedes") or "") for r in rows or []}

	def root(name: str) -> str:
		seen = set()
		while parent.get(name) and name not in seen:
			seen.add(name)
			name = parent[name]
		return name

	by_name: dict = {}
	for row in found:
		by_name.setdefault(root(str(row["name"])), []).append(row)
	out = []
	for versions in by_name.values():
		live = [v for v in versions if v.get("status") in LIVE]
		out.append(live[-1] if live else versions[-1])
	return out


def provider_values(subject: dict, ctx: dict) -> dict:
	sops = covering(str(subject.get("task_type") or ""))
	unapproved = [s for s in sops if s.get("status") not in LIVE]
	return {
		"covering_count": len(sops),
		"unapproved_count": len(unapproved),
		"unapproved": ", ".join(f"{s.get('policy_name')} ({s.get('status')})" for s in unapproved),
	}


def register(ccf) -> None:
	p = ccf._p
	ccf.register(
		ccf.Provider(
			"sop",
			{
				"covering_count": p("number", "SOPs covering the task's type.", example=1),
				"unapproved_count": p("number", "Of those, how many are not approved.", example=0),
				"unapproved": p("string", "Which, by name and status.", example="Tree removal (In Review)"),
			},
			lambda subject, ctx: provider_values(subject, ctx),
			past=False,
			description="The SOPs (Compliance Policies) covering a Farm Task's type, and whether they are approved.",
		)
	)


@frappe.whitelist(methods=["POST"])
def desk_submit(name: str) -> dict:
	try:
		return submit(name, frappe.session.user)
	except (ValueError, PermissionError) as exc:
		frappe.throw(str(exc))


@frappe.whitelist(methods=["POST"])
def desk_approve(name: str) -> dict:
	"""The Desk button. Approval is a person's act; there is no MCP approve."""
	try:
		return approve(name, frappe.session.user, "App sign-in")
	except (ValueError, PermissionError) as exc:
		frappe.throw(str(exc))


@frappe.whitelist(methods=["POST"])
def desk_request_changes(name: str, note: str = "") -> dict:
	try:
		return request_changes(name, frappe.session.user, note)
	except (ValueError, PermissionError) as exc:
		frappe.throw(str(exc))
