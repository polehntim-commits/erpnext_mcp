# SPDX-License-Identifier: MIT
"""IT general controls: who can do what, what changed, and whether the backups restore.

THE THREE ITGCs AN AUDITOR TESTS, and they are the same three at a four-person
farm as at a listed company — only the evidence is thinner:

    ACCESS      who holds which permissions, and when somebody last looked
    CHANGE      what changed, who approved it, how it would be undone
    OPERATIONS  whether a backup has ever actually been restored from

WHY THE ACCESS REPORT IS COMPUTED AND NEVER STORED. A permissions snapshot in a
table is wrong the moment somebody adds a role, and a stale snapshot is more
dangerous than none: it is the document an operator hands an auditor while
believing it. So `generate_access_control_report` reads User, Has Role and the
permission tables at the moment it is called, says when it read them, and stores
nothing. What IS stored is the fact that somebody reviewed it — a Change
Management Log row of type `Permission` — because the review is an event and the
access list is a state.

WHY THIS APP DOES NOT USE `doc_events` TO POPULATE THE CHANGE LOG, and it is the
one place a reader will expect it. `hooks.py` opens with a promise: this app
installs no `doc_events`, no overrides, and adds no field to a doctype it did not
create, so that removing it gives an operator their site back exactly as it was.
Hanging a hook on every Custom DocPerm and Role write would break that promise
for every site, including the ones that never look at this module.

What it does instead is narrower and, for the control, better: `record_tool_change`
is called from this app's OWN dispatcher path for the calls that change access,
schema or configuration. Those rows carry `source = MCP Tool` and a link to the
MCP Action Log row for the call, so they are the ones an auditor can see were not
curated after the fact. Changes made in the Desk or on the server are recorded by
hand with `source = Manual`, and `get_change_management_report` reports the split
rather than hiding it — because "how much of your change log is self-attested" is
the first question worth asking about a change log.

EVERY CONTROL HERE IS ADVISORY UNTIL SOMEBODY SAYS OTHERWISE. See
`erpnext_mcp/enforcement.py`: this module supplies findings and never decides
what happens to them.
"""

import frappe

from .. import compat, enforcement, roles
from ..args import as_bool, as_choice, as_date, as_float, as_int, as_limit, as_str, resolve_company
from ..erpnext_mcp.doctype.backup_record.backup_record import (
	FAIL,
	NOT_TESTED,
	PASS,
	VERIFYING_RESULTS,
)
from ..erpnext_mcp.doctype.change_management_log.change_management_log import (
	APPROVAL_EXPECTED,
	APPROVED,
	NOT_REQUIRED,
	PENDING,
)
from ..errors import ToolError
from ..result import ToolResult

CHANGE_LOG = "Change Management Log"
BACKUP = "Backup Record"
ACTION_LOG = "MCP Action Log"

ACCESS_REVIEW = "access_review"
CHANGE_APPROVAL = "change_approval"
BACKUP_VERIFICATION = "backup_verification"

#: How long an access review, a backup verification and a change stay fresh.
#: DEFAULTS, not law — every one is overridable per call, and the seeded
#: Compliance Rule is where an operation states its own. Quarterly access and
#: monthly restore-testing are the cadences the ITGC frameworks assume and the
#: ones a small operation can actually keep.
ACCESS_REVIEW_DAYS = 90
BACKUP_VERIFICATION_DAYS = 30

#: Roles that can change the books or who may change them. A user holding one of
#: these is `privileged` in the report — not a judgement about the person, a
#: statement about what the login can reach if it is taken.
PRIVILEGED_ROLES = (
	"System Manager",
	"Administrator",
	"Accounts Manager",
	"Accounts User",
	"Auditor",
	"HR Manager",
	"Payroll Manager",
)

_CHANGE_FIELDS = (
	"name",
	"company",
	"change_type",
	"title",
	"change_date",
	"changed_by",
	"risk_level",
	"source",
	"description",
	"reference_doctype",
	"reference_name",
	"mcp_action_log",
	"approval_status",
	"approved_by",
	"approved_on",
	"rollback_plan",
	"tested",
	"notes",
	"creation",
	"owner",
)

_BACKUP_FIELDS = (
	"name",
	"company",
	"backup_type",
	"status",
	"started_at",
	"completed_at",
	"location",
	"offsite",
	"size_mb",
	"retention_days",
	"rpo_hours",
	"rto_hours",
	"test_restore_result",
	"test_restore_on",
	"test_restore_by",
	"restore_duration_minutes",
	"test_restore_notes",
	"notes",
	"creation",
	"owner",
)


def available() -> bool:
	return compat.doctype_exists(CHANGE_LOG) and compat.doctype_exists(BACKUP)


def _require(doctype: str) -> None:
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


# ── access ──────────────────────────────────────────────────────────────────
def _users() -> list:
	"""Every enabled login on the site, with whatever the site records about it.

	`last_login` and `last_active` are read through `existing_fields` rather than
	named directly: they are standard Frappe columns but not universal across
	versions, and a report that raised on a site missing one would be a report
	nobody could run. What is missing is REPORTED as missing — never quietly
	rendered as "never logged in", which is the same sentence with the opposite
	meaning.
	"""
	fields = compat.existing_fields(
		"User",
		(
			"name",
			"email",
			"full_name",
			"enabled",
			"user_type",
			"last_login",
			"last_active",
			"creation",
		),
	)
	rows = frappe.db.get_all("User", fields=fields, order_by="name asc", limit=2000)
	return [dict(row) for row in rows]


def _permissions_by_role() -> dict:
	"""{role: [{doctype, read, write, create, delete, submit}]}, custom first.

	Custom DocPerm SHADOWS DocPerm for a doctype rather than adding to it, which
	is Frappe's own rule: the moment somebody edits permissions for a doctype in
	the Desk, the custom table becomes the whole truth for it. A report that
	merged the two would show permissions the site does not actually grant.
	"""
	out: dict = {}
	customised = set()
	for doctype in ("Custom DocPerm", "DocPerm"):
		if not compat.doctype_exists(doctype):
			continue
		fields = compat.existing_fields(
			doctype,
			(
				"parent",
				"role",
				"permlevel",
				"read",
				"write",
				"create",
				"delete",
				"submit",
				"cancel",
				"if_owner",
			),
		)
		rows = frappe.db.get_all(doctype, fields=fields, limit=20000)
		for row in rows:
			target = row.get("parent")
			if doctype == "Custom DocPerm":
				customised.add(target)
			elif target in customised:
				# Overridden in the Desk — the standard row is not in force.
				continue
			if frappe.utils.cint(row.get("permlevel")):
				continue
			entry = {
				"doctype": target,
				"read": bool(frappe.utils.cint(row.get("read"))),
				"write": bool(frappe.utils.cint(row.get("write"))),
				"create": bool(frappe.utils.cint(row.get("create"))),
				"delete": bool(frappe.utils.cint(row.get("delete"))),
				"submit": bool(frappe.utils.cint(row.get("submit"))),
				"if_owner": bool(frappe.utils.cint(row.get("if_owner"))),
				"source": doctype,
			}
			out.setdefault(str(row.get("role") or ""), []).append(entry)
	return out


def _last_access_review(company: str) -> dict:
	"""The most recent recorded access review, as a Change Management Log row."""
	if not compat.doctype_exists(CHANGE_LOG):
		return {}
	rows = frappe.db.get_all(
		CHANGE_LOG,
		filters={"company": company, "change_type": "Permission"},
		fields=compat.existing_fields(
			CHANGE_LOG, ("name", "title", "change_date", "changed_by", "approval_status")
		),
		order_by="change_date desc",
		limit=1,
	)
	return dict(rows[0]) if rows else {}


def generate_access_control_report(args: dict) -> ToolResult:
	"""Who has what, computed now: users, roles, the permissions behind them, last login."""
	company = resolve_company(as_str(args, "company"), required=False) or ""
	include_disabled = as_bool(args, "include_disabled", default=False)
	privileged_only = as_bool(args, "privileged_only", default=False)
	review_days = as_int(args, "review_period_days", default=ACCESS_REVIEW_DAYS)
	stale_login_days = as_int(args, "stale_login_days", default=90)
	limit = as_limit(args)

	today = frappe.utils.today()
	permissions = _permissions_by_role()
	users = _users()
	has_last_login = compat.has_field("User", "last_login")

	report = []
	for row in users:
		enabled = bool(frappe.utils.cint(row.get("enabled", 1)))
		if not enabled and not include_disabled:
			continue
		user = row.get("name")
		held = roles.all_roles_of(user)
		privileged = sorted(set(held) & set(PRIVILEGED_ROLES))
		if privileged_only and not privileged:
			continue

		last_login = str(row.get("last_login") or "") or None
		days_since_login = None
		if last_login:
			days_since_login = frappe.utils.date_diff(today, last_login[:10])

		reach = sorted({entry["doctype"] for role in held for entry in permissions.get(role, [])})
		can_write = sorted(
			{
				entry["doctype"]
				for role in held
				for entry in permissions.get(role, [])
				if entry["write"] or entry["create"] or entry["delete"]
			}
		)
		flags = []
		if privileged:
			flags.append("privileged")
		if not enabled:
			flags.append("disabled")
		if enabled and has_last_login and not last_login:
			flags.append("never logged in")
		if enabled and days_since_login is not None and days_since_login > stale_login_days:
			flags.append(f"no login in {days_since_login} days")
		if enabled and not held:
			flags.append("no roles")

		report.append(
			{
				"user": user,
				"full_name": row.get("full_name"),
				"enabled": enabled,
				"user_type": row.get("user_type"),
				"roles": sorted(held),
				"role_count": len(held),
				"privileged_roles": privileged,
				"privileged": bool(privileged),
				"last_login": last_login,
				"days_since_login": days_since_login,
				"doctypes_readable": len(reach),
				"doctypes_writable": can_write,
				"writable_count": len(can_write),
				"flags": flags,
			}
		)

	report.sort(key=lambda row: (not row["privileged"], row["user"]))
	review = _last_access_review(company) if company else {}
	last_review_on = str(review.get("change_date") or "")[:10] or None
	days_since_review = frappe.utils.date_diff(today, last_review_on) if last_review_on else None
	overdue = days_since_review is None or days_since_review > review_days

	findings = []
	if overdue and company:
		findings.append(
			enforcement.Finding(
				control_point=ACCESS_REVIEW,
				message=(
					f"Privileged access for {company} was last reviewed "
					+ (f"{days_since_review} days ago ({last_review_on})." if last_review_on else "never.")
					+ f" The review period is {review_days} days."
				),
				remedy=(
					"Read this report, then record the review with create_change_management_log "
					"(change_type='Permission', title='Quarterly access review') so the next run "
					"can see it was done."
				),
				company=company,
				detail={
					"last_review_on": last_review_on,
					"days_since_review": days_since_review,
					"review_period_days": review_days,
					"privileged_users": len([row for row in report if row["privileged"]]),
				},
			)
		)
	# raise_on_enforced=False: a READ tool never refuses. It shows what
	# enforcement would do, which is the point of running it before switching on.
	control = enforcement.evaluate(ACCESS_REVIEW, findings, company=company, raise_on_enforced=False)

	data = {
		"company": company or None,
		"generated_at": frappe.utils.now(),
		"user_count": len(report),
		"users": report[:limit],
		"truncated": len(report) > limit,
		"privileged_count": len([row for row in report if row["privileged"]]),
		"disabled_included": include_disabled,
		"flagged": [
			{"user": row["user"], "flags": row["flags"]}
			for row in report
			if row["flags"] and row["flags"] != ["privileged"]
		],
		"roles_in_use": sorted({role for row in report for role in row["roles"]}),
		"last_access_review": (
			{
				"change_management_log": review.get("name"),
				"on": last_review_on,
				"by": review.get("changed_by"),
				"days_ago": days_since_review,
			}
			if review
			else None
		),
		"review_period_days": review_days,
		"review_overdue": overdue,
		"control": control,
		"last_login_available": has_last_login,
		"nothing_was_stored": (
			"This report is computed at call time and stored nowhere. A permissions snapshot in "
			"a table is wrong the moment somebody adds a role, and a stale one is worse than "
			"none because it is the document somebody hands an auditor while believing it. What "
			"IS worth storing is that a review happened — record that with "
			"create_change_management_log."
		),
	}
	if not has_last_login:
		data["last_login_note"] = (
			"This site's User doctype has no `last_login` column, so dormant-login flags could "
			"not be computed. They are ABSENT rather than clear — an empty flag list here does "
			"not mean nobody is dormant."
		)
	return ToolResult(
		data=data,
		summary=(
			f"access control report: {len(report)} user(s), "
			f"{data['privileged_count']} privileged, review "
			+ (f"{days_since_review} days old" if days_since_review is not None else "never recorded")
		),
	)


# ── change management ───────────────────────────────────────────────────────
def _describe_change(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"company": row.get("company"),
		"change_type": row.get("change_type"),
		"title": row.get("title"),
		"change_date": str(row.get("change_date") or "") or None,
		"changed_by": row.get("changed_by"),
		"risk_level": row.get("risk_level"),
		"source": row.get("source"),
		"description": row.get("description"),
		"reference_doctype": row.get("reference_doctype") or None,
		"reference_name": row.get("reference_name") or None,
		"mcp_action_log": row.get("mcp_action_log") or None,
		"approval_status": row.get("approval_status"),
		"approved_by": row.get("approved_by") or None,
		"approved_on": str(row.get("approved_on") or "") or None,
		"rollback_plan": row.get("rollback_plan") or None,
		"tested": bool(frappe.utils.cint(row.get("tested"))),
		"notes": row.get("notes") or None,
		"self_approved": bool(row.get("approved_by") and row.get("approved_by") == row.get("changed_by")),
		"approval_expected": row.get("change_type") in APPROVAL_EXPECTED,
	}


def create_change_management_log(args: dict) -> ToolResult:
	"""Record one system change — what, who, approved by whom, and how to undo it."""
	_require(CHANGE_LOG)
	company = resolve_company(as_str(args, "company"), required=True)
	change_type = as_choice(
		CHANGE_LOG, "change_type", as_str(args, "change_type", required=True), "change_type"
	)
	title = as_str(args, "title", required=True)
	description = as_str(args, "description", required=True)
	changed_by = as_str(args, "changed_by") or frappe.session.user
	change_date = as_str(args, "change_date") or frappe.utils.now()
	approved_by = as_str(args, "approved_by")
	approval_status = as_choice(
		CHANGE_LOG,
		"approval_status",
		as_str(args, "approval_status") or (APPROVED if approved_by else PENDING),
		"approval_status",
	)

	if approved_by and approved_by == changed_by:
		raise ToolError(
			f"{changed_by} is named as both the person who made this change and the person who "
			"approved it, which is not an approval. If one person genuinely does both here, say "
			"so: approval_status='Not Required' with the compensating control in `notes`. A "
			"documented exception is defensible; a self-approval is a finding. Nothing was created."
		)

	# THE GATE IS CONSULTED BEFORE ANYTHING IS WRITTEN, which is what lets an
	# enforced refusal leave the site exactly as it was.
	findings = []
	if change_type in APPROVAL_EXPECTED and approval_status not in (APPROVED, NOT_REQUIRED):
		findings.append(
			enforcement.Finding(
				control_point=CHANGE_APPROVAL,
				message=(
					f"A {change_type} change ({title!r}) is being recorded with no approver — "
					f"approval status {approval_status}."
				),
				remedy=(
					"Name who approved it with approved_by (somebody other than "
					f"{changed_by}), or record approval_status='Not Required' with the reason "
					"in notes if this change genuinely needed nobody's sign-off."
				),
				source_doctype=CHANGE_LOG,
				source_docname="",
				company=company,
				detail={"change_type": change_type, "title": title, "changed_by": changed_by},
			)
		)
	control = enforcement.evaluate(CHANGE_APPROVAL, findings, company=company)

	doc = frappe.new_doc(CHANGE_LOG)
	doc.company = company
	doc.change_type = change_type
	doc.title = title
	doc.description = description
	doc.changed_by = changed_by
	doc.change_date = change_date
	doc.approval_status = approval_status
	for field, value in (
		("risk_level", as_str(args, "risk_level")),
		("source", as_str(args, "source") or "Manual"),
		("reference_doctype", as_str(args, "reference_doctype")),
		("reference_name", as_str(args, "reference_name")),
		("mcp_action_log", as_str(args, "mcp_action_log")),
		("approved_by", approved_by),
		("approved_on", as_str(args, "approved_on")),
		("rollback_plan", as_str(args, "rollback_plan")),
		("notes", as_str(args, "notes")),
	):
		if value:
			doc.set(field, value)
	if as_bool(args, "tested", default=False):
		doc.tested = 1
	doc.insert()

	row = frappe.db.get_value(
		CHANGE_LOG, doc.name, compat.existing_fields(CHANGE_LOG, _CHANGE_FIELDS), as_dict=True
	)
	data = _describe_change(dict(row))
	data["control"] = control
	if not doc.rollback_plan:
		data["next_step"] = (
			"No rollback plan was recorded. It is cheap to write now and impossible to think "
			"clearly about at the moment it is needed."
		)
	return ToolResult(
		data=data,
		summary=f"change log {doc.name}: {change_type} — {title} ({approval_status})",
		docstatus_delta="none → 0 (draft)",
	)


def get_change_management_log(args: dict) -> ToolResult:
	"""One recorded change in full."""
	_require(CHANGE_LOG)
	name = as_str(args, "change_management_log", required=True)
	row = frappe.db.get_value(
		CHANGE_LOG, name, compat.existing_fields(CHANGE_LOG, _CHANGE_FIELDS), as_dict=True
	)
	if not row:
		raise ToolError(f"Change Management Log {name!r} does not exist.")
	return ToolResult(data=_describe_change(dict(row)), summary=f"change log {name}")


def list_change_management_logs(args: dict) -> ToolResult:
	"""The change log, newest first, with the gaps an auditor samples for."""
	_require(CHANGE_LOG)
	company = resolve_company(as_str(args, "company"), required=True)
	filters = {"company": company}
	for key, field in (
		("change_type", "change_type"),
		("approval_status", "approval_status"),
		("risk_level", "risk_level"),
		("source", "source"),
		("changed_by", "changed_by"),
	):
		value = as_str(args, key)
		if value:
			filters[field] = value
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters["change_date"] = ("between", (from_date, f"{to_date} 23:59:59"))
	elif from_date:
		filters["change_date"] = (">=", from_date)
	elif to_date:
		filters["change_date"] = ("<=", f"{to_date} 23:59:59")
	limit = as_limit(args)

	rows = frappe.db.get_all(
		CHANGE_LOG,
		filters=filters,
		fields=compat.existing_fields(CHANGE_LOG, _CHANGE_FIELDS),
		order_by="change_date desc",
		limit=limit + 1,
	)
	changes = [_describe_change(dict(row)) for row in rows[:limit]]
	unapproved = [
		change["name"]
		for change in changes
		if change["approval_expected"] and change["approval_status"] not in (APPROVED, NOT_REQUIRED)
	]
	data = {
		"company": company,
		"count": len(changes),
		"truncated": len(rows) > limit,
		"changes": changes,
		"unapproved": unapproved,
		"self_approved": [change["name"] for change in changes if change["self_approved"]],
		"by_type": _count_by(changes, "change_type"),
		"by_source": _count_by(changes, "source"),
	}
	return ToolResult(data=data, summary=f"{len(changes)} change log row(s) for {company}")


def _count_by(rows: list, key: str) -> dict:
	out: dict = {}
	for row in rows:
		out[row.get(key) or "Unspecified"] = out.get(row.get(key) or "Unspecified", 0) + 1
	return dict(sorted(out.items(), key=lambda pair: -pair[1]))


def get_change_management_report(args: dict) -> ToolResult:
	"""The change control picture for a period: volume, approval, and what is self-attested."""
	_require(CHANGE_LOG)
	company = resolve_company(as_str(args, "company"), required=True)
	to_date = as_date(args, "to_date") or frappe.utils.today()
	from_date = as_date(args, "from_date") or str(frappe.utils.add_days(to_date, -90))
	if from_date > to_date:
		raise ToolError(f"from_date {from_date} is after to_date {to_date}.")

	rows = frappe.db.get_all(
		CHANGE_LOG,
		filters={"company": company, "change_date": ("between", (from_date, f"{to_date} 23:59:59"))},
		fields=compat.existing_fields(CHANGE_LOG, _CHANGE_FIELDS),
		order_by="change_date desc",
		limit=5000,
	)
	changes = [_describe_change(dict(row)) for row in rows]

	needing = [change for change in changes if change["approval_expected"]]
	approved = [change for change in needing if change["approval_status"] == APPROVED]
	waived = [change for change in needing if change["approval_status"] == NOT_REQUIRED]
	outstanding = [change for change in needing if change not in approved and change not in waived]
	self_approved = [change for change in changes if change["self_approved"]]
	tool_written = [change for change in changes if change["source"] == "MCP Tool"]

	findings = []
	for change in outstanding:
		findings.append(
			enforcement.Finding(
				control_point=CHANGE_APPROVAL,
				message=(
					f"{change['change_type']} change {change['name']} ({change['title']!r}, "
					f"{str(change['change_date'])[:10]}) has no approver — status "
					f"{change['approval_status']}."
				),
				remedy="Record who approved it, or mark it Not Required with the reason.",
				source_doctype=CHANGE_LOG,
				source_docname=change["name"],
				company=company,
			)
		)
	control = enforcement.evaluate(CHANGE_APPROVAL, findings, company=company, raise_on_enforced=False)

	high_risk_untested = [
		change["name"] for change in changes if change["risk_level"] == "High" and not change["tested"]
	]
	no_rollback = [change["name"] for change in changes if not change["rollback_plan"]]

	data = {
		"company": company,
		"from_date": from_date,
		"to_date": to_date,
		"total_changes": len(changes),
		"by_type": _count_by(changes, "change_type"),
		"by_risk": _count_by(changes, "risk_level"),
		"by_source": _count_by(changes, "source"),
		"approval": {
			"expected": len(needing),
			"approved": len(approved),
			"waived_not_required": len(waived),
			"outstanding": len(outstanding),
			"outstanding_rows": [change["name"] for change in outstanding],
			"self_approved": [change["name"] for change in self_approved],
			"rate": round(len(approved) / len(needing) * 100, 1) if needing else 100.0,
		},
		"self_attestation": {
			"written_by_this_app": len(tool_written),
			"written_by_hand": len(changes) - len(tool_written),
			"pct_written_by_this_app": round(len(tool_written) / len(changes) * 100, 1) if changes else 0.0,
			"why_this_matters": (
				"Rows this app wrote about its own privileged calls carry a link to the MCP "
				"Action Log for that call and could not have been curated after the fact. Rows "
				"typed by hand are somebody's account of a change made elsewhere — which is the "
				"honest thing to record, and the thing an auditor samples hardest. The split is "
				"reported rather than hidden because 'how much of your change log is "
				"self-attested' is the first question worth asking about a change log."
			),
		},
		"high_risk_untested": high_risk_untested,
		"no_rollback_plan": no_rollback,
		"control": control,
	}
	return ToolResult(
		data=data,
		summary=(
			f"change management report for {company}, {from_date} to {to_date}: {len(changes)} "
			f"change(s), {len(outstanding)} unapproved"
		),
	)


def record_tool_change(
	tool_name: str,
	company: str,
	change_type: str,
	title: str,
	description: str,
	*,
	reference_doctype: str = "",
	reference_name: str = "",
	action_log: str = "",
	risk_level: str = "Medium",
) -> str:
	"""Write a Change Management Log row about one of this app's own privileged calls.

	NEVER RAISES, and never fails the call it is recording. A change that
	happened and was not logged is a gap in the change log; a change that was
	REFUSED because the change log was unwritable is an outage caused by a
	control, which is how controls get removed. The gap is the better failure and
	it is visible — `get_change_management_report` reports what this app wrote
	against what it was asked to.

	Called from this app's own tool layer rather than from a `doc_events` hook,
	for the reason in the module docstring: `hooks.py` promises this app installs
	no document hooks on doctypes it did not create.
	"""
	if not compat.doctype_exists(CHANGE_LOG) or not company:
		return ""
	try:
		doc = frappe.new_doc(CHANGE_LOG)
		doc.company = company
		doc.change_type = change_type
		doc.title = title[:140]
		doc.description = description
		doc.changed_by = frappe.session.user
		doc.change_date = frappe.utils.now()
		doc.source = "MCP Tool"
		doc.risk_level = risk_level
		# PENDING and not Approved: this app cannot approve a change on anybody's
		# behalf, and a row that arrived pre-approved by the system that made the
		# change would be the exact fiction the control exists to prevent.
		doc.approval_status = PENDING
		if reference_doctype:
			doc.reference_doctype = reference_doctype
		if reference_name:
			doc.reference_name = reference_name
		if action_log:
			doc.mcp_action_log = action_log
		doc.notes = f"Recorded automatically by {tool_name}."
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:  # pragma: no cover - a gap is better than an outage
		return ""


# ── backup and recovery ─────────────────────────────────────────────────────
def _describe_backup(row: dict) -> dict:
	started = str(row.get("started_at") or "")
	completed = str(row.get("completed_at") or "")
	duration = None
	if started and completed:
		duration = round(frappe.utils.time_diff_in_seconds(completed, started) / 60.0, 1)
	return {
		"name": row.get("name"),
		"company": row.get("company"),
		"backup_type": row.get("backup_type"),
		"status": row.get("status"),
		"started_at": started or None,
		"completed_at": completed or None,
		"duration_minutes": duration,
		"location": row.get("location"),
		"offsite": bool(frappe.utils.cint(row.get("offsite"))),
		"size_mb": round(frappe.utils.flt(row.get("size_mb")), 2) or None,
		"retention_days": frappe.utils.cint(row.get("retention_days")) or None,
		"rpo_hours": frappe.utils.cint(row.get("rpo_hours")) or None,
		"rto_hours": frappe.utils.cint(row.get("rto_hours")) or None,
		"test_restore_result": row.get("test_restore_result") or NOT_TESTED,
		"test_restore_on": str(row.get("test_restore_on") or "") or None,
		"test_restore_by": row.get("test_restore_by") or None,
		"restore_duration_minutes": frappe.utils.cint(row.get("restore_duration_minutes")) or None,
		"test_restore_notes": row.get("test_restore_notes") or None,
		"verified": row.get("test_restore_result") in VERIFYING_RESULTS,
		"notes": row.get("notes") or None,
	}


def create_backup_record(args: dict) -> ToolResult:
	"""Record that a backup ran: what kind, when, where it went, and how it ended."""
	_require(BACKUP)
	company = resolve_company(as_str(args, "company"), required=True)
	backup_type = as_choice(BACKUP, "backup_type", as_str(args, "backup_type", required=True), "backup_type")
	status = as_choice(BACKUP, "status", as_str(args, "status") or "Success", "status")
	started_at = as_str(args, "started_at") or frappe.utils.now()
	location = as_str(args, "location", required=True)

	doc = frappe.new_doc(BACKUP)
	doc.company = company
	doc.backup_type = backup_type
	doc.status = status
	doc.started_at = started_at
	doc.location = location
	completed_at = as_str(args, "completed_at")
	if not completed_at and status == "Success":
		completed_at = frappe.utils.now()
	if completed_at:
		doc.completed_at = completed_at
	if as_bool(args, "offsite", default=False):
		doc.offsite = 1
	if args.get("size_mb") is not None:
		doc.size_mb = as_float(args.get("size_mb"), "size_mb")
	for field in ("retention_days", "rpo_hours", "rto_hours"):
		value = as_int(args, field, default=None)
		if value is not None:
			doc.set(field, value)
	notes = as_str(args, "notes")
	if notes:
		doc.notes = notes
	doc.insert()

	row = frappe.db.get_value(BACKUP, doc.name, compat.existing_fields(BACKUP, _BACKUP_FIELDS), as_dict=True)
	data = _describe_backup(dict(row))
	data["next_step"] = (
		"A backup nobody has restored from is a belief. Record a test restore against this row "
		"with record_backup_test when somebody actually pulls it back — that, and not the job "
		"status, is what the verification control counts."
	)
	return ToolResult(
		data=data,
		summary=f"backup {doc.name}: {backup_type} {status} at {location}",
		docstatus_delta="none → 0 (draft)",
	)


def get_backup_record(args: dict) -> ToolResult:
	"""One backup event in full."""
	_require(BACKUP)
	name = as_str(args, "backup_record", required=True)
	row = frappe.db.get_value(BACKUP, name, compat.existing_fields(BACKUP, _BACKUP_FIELDS), as_dict=True)
	if not row:
		raise ToolError(f"Backup Record {name!r} does not exist.")
	data = _describe_backup(dict(row))
	if data["rto_hours"] and data["restore_duration_minutes"]:
		objective = data["rto_hours"] * 60
		data["met_rto"] = data["restore_duration_minutes"] <= objective
		if not data["met_rto"]:
			data["rto_note"] = (
				f"The test restore took {data['restore_duration_minutes']} minutes against an "
				f"objective of {objective}. The objective is the number somebody promised; this "
				"is the number the system actually did."
			)
	return ToolResult(data=data, summary=f"backup record {name}")


def list_backup_records(args: dict) -> ToolResult:
	"""Backups on file, newest first, with the verification picture."""
	_require(BACKUP)
	company = resolve_company(as_str(args, "company"), required=True)
	filters = {"company": company}
	for key in ("backup_type", "status", "test_restore_result"):
		value = as_str(args, key)
		if value:
			filters[key] = value
	if as_bool(args, "offsite_only", default=False):
		filters["offsite"] = 1
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters["started_at"] = ("between", (from_date, f"{to_date} 23:59:59"))
	limit = as_limit(args)

	rows = frappe.db.get_all(
		BACKUP,
		filters=filters,
		fields=compat.existing_fields(BACKUP, _BACKUP_FIELDS),
		order_by="started_at desc",
		limit=limit + 1,
	)
	backups = [_describe_backup(dict(row)) for row in rows[:limit]]
	verified = [backup for backup in backups if backup["verified"]]
	window = as_int(args, "verification_window_days", default=BACKUP_VERIFICATION_DAYS)
	# THE WINDOW IS ASKED OF THE WHOLE COMPANY, not of the page returned. A
	# filtered list showing ten failed jobs must not report the verification
	# control as clear merely because the passing restore is on another page.
	control = enforcement.evaluate(
		BACKUP_VERIFICATION,
		backup_verification_findings(company, window),
		company=company,
		raise_on_enforced=False,
	)
	data = {
		"company": company,
		"count": len(backups),
		"truncated": len(rows) > limit,
		"backups": backups,
		"verified_count": len(verified),
		"failed_count": len([backup for backup in backups if backup["status"] == "Failed"]),
		"offsite_count": len([backup for backup in backups if backup["offsite"]]),
		"never_tested": [backup["name"] for backup in backups if backup["test_restore_result"] == NOT_TESTED],
		"last_verified_on": max(
			(backup["test_restore_on"] for backup in verified if backup["test_restore_on"]), default=None
		),
		"verification_window_days": window,
		"control": control,
	}
	return ToolResult(
		data=data, summary=f"{len(backups)} backup record(s) for {company}, {len(verified)} verified"
	)


def record_backup_test(args: dict) -> ToolResult:
	"""Record a test restore against a backup — the event that turns a job into a control."""
	_require(BACKUP)
	name = as_str(args, "backup_record", required=True)
	if not frappe.db.exists(BACKUP, name):
		raise ToolError(f"Backup Record {name!r} does not exist. Nothing was recorded.")
	result = as_choice(
		BACKUP,
		"test_restore_result",
		as_str(args, "test_restore_result", required=True),
		"test_restore_result",
	)
	if result == NOT_TESTED:
		raise ToolError(
			"test_restore_result of 'Not Tested' is not a test — it is the absence of one, and "
			"it is already the default. Record Pass, Partial or Fail. Nothing was recorded."
		)
	tested_on = as_date(args, "test_restore_on") or frappe.utils.today()
	if tested_on > frappe.utils.today():
		raise ToolError(f"test_restore_on {tested_on} is in the future. Nothing was recorded.")

	doc = frappe.get_doc(BACKUP, name)
	doc.test_restore_result = result
	doc.test_restore_on = tested_on
	doc.test_restore_by = as_str(args, "test_restore_by") or frappe.session.user
	duration = as_int(args, "restore_duration_minutes", default=None)
	if duration is not None:
		doc.restore_duration_minutes = duration
	notes = as_str(args, "test_restore_notes")
	if notes:
		doc.test_restore_notes = notes
	doc.save()

	row = frappe.db.get_value(BACKUP, name, compat.existing_fields(BACKUP, _BACKUP_FIELDS), as_dict=True)
	data = _describe_backup(dict(row))
	if result == FAIL:
		data["note"] = (
			"A FAILED test restore is the most valuable row in this table. It was found on a "
			"day chosen by somebody rather than by a disaster, and everything the operation "
			"believed about its recovery until this moment was wrong."
		)
	elif result == "Partial":
		data["note"] = (
			"A partial restore does not verify this copy — `verified` stays false and the "
			"verification window still counts this as untested. Recovering some of the data is a "
			"finding with a silver lining, not a verification."
		)
	if data["rto_hours"] and data["restore_duration_minutes"]:
		objective = data["rto_hours"] * 60
		data["met_rto"] = data["restore_duration_minutes"] <= objective
	return ToolResult(
		data=data,
		summary=f"test restore on {name}: {result} on {tested_on}",
	)


def _last_verified(company: str) -> dict:
	"""The most recent PASSING test restore for a company, or {}."""
	rows = frappe.db.get_all(
		BACKUP,
		filters={"company": company, "test_restore_result": PASS},
		fields=compat.existing_fields(BACKUP, ("name", "test_restore_on", "test_restore_by", "backup_type")),
		order_by="test_restore_on desc",
		limit=1,
	)
	return dict(rows[0]) if rows else {}


def backup_verification_findings(company: str, window_days: int = BACKUP_VERIFICATION_DAYS) -> list:
	"""Findings for the backup verification control. Shared by the report and the gate."""
	if not compat.doctype_exists(BACKUP):
		return []
	last = _last_verified(company)
	today = frappe.utils.today()
	verified_on = str(last.get("test_restore_on") or "")[:10]
	days = frappe.utils.date_diff(today, verified_on) if verified_on else None
	if days is not None and days <= window_days:
		return []
	return [
		enforcement.Finding(
			control_point=BACKUP_VERIFICATION,
			message=(
				f"No backup for {company} has been verified by a passing test restore "
				+ (f"since {verified_on} ({days} days ago)." if verified_on else "ever.")
				+ f" The verification window is {window_days} days."
			),
			remedy=(
				"Restore one backup somewhere safe, check the contents, and record it with "
				"record_backup_test(test_restore_result='Pass'). A green job log is not a "
				"verification."
			),
			company=company,
			detail={
				"last_verified_on": verified_on or None,
				"days_since_verification": days,
				"window_days": window_days,
				"backup_record": last.get("name"),
			},
		)
	]
