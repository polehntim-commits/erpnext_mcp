# SPDX-License-Identifier: MIT
"""From hours to moments: the MCP half. v0.206.0.

docs/design/config_flags_triage.md. Eleven tools in three groups:

  extraction config  list / get / preview (read) · update / publish (write)
  feature flags      list (read) · set (write)
  triage             list_triage_queue (read) · propose / approve / reject (write)

Every write is off by default and needs System Manager or Farm Manager.
`update_extraction_config` never touches what the phones read — it writes a
Draft; `publish_extraction_config` is the separate step that does.
`approve_triage_proposal` runs each proposed call through `registry.dispatch`,
so the proposed tool's own switch, role gate and audit row all apply.
"""

from __future__ import annotations

import json

import frappe

from .. import compat, extraction_config, flags, security, triage
from ..args import as_bool, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import app_feedback


def _json_arg(args: dict, key: str, kind=dict, required: bool = False):
	value = args.get(key)
	if isinstance(value, str) and value.strip():
		try:
			value = json.loads(value)
		except ValueError as exc:
			raise ToolError(f"{key} is not valid JSON: {exc}. Nothing was changed.") from exc
	if value in (None, "") and not required:
		return None
	if not isinstance(value, kind):
		raise ToolError(f"{key} must be a JSON {'object' if kind is dict else 'list'}. Nothing was changed.")
	return value


# ── extraction config ───────────────────────────────────────────────────────


def _document_type(args: dict) -> str:
	from .. import document_intel

	raw = as_str(args, "document_type", required=True)
	resolved = document_intel.normalise_document_type(raw)
	if not resolved:
		raise ToolError(
			f"{raw!r} is not a document type. It is one of: {', '.join(document_intel.DOCUMENT_TYPES)}."
		)
	return resolved


def _require_doctype() -> None:
	if not compat.doctype_exists(extraction_config.DOCTYPE):
		raise ToolError(
			"this site has no Extraction Config doctype yet — run `bench --site <site> migrate` "
			"after upgrading the app."
		)


def _config_row(doc) -> dict:
	return {
		"name": doc.get("name"),
		"document_type": doc.get("document_type"),
		"version": doc.get("version"),
		"status": doc.get("status"),
		"config_version": doc.get("config_version"),
		"notes": doc.get("notes"),
		"staged_users": extraction_config.staged_users(doc) if doc.get("status") == "Staged" else [],
		"authored_by": doc.get("authored_by"),
		"published_by": doc.get("published_by") or None,
		"published_on": str(doc.get("published_on") or "") or None,
	}


def list_extraction_configs(args: dict) -> ToolResult:
	"""Every version of every type's config, newest first, with the one in use."""
	document_type = _document_type(args) if as_str(args, "document_type") else ""
	listed = [_config_row(row) for row in extraction_config.rows(document_type)]
	types = (
		[document_type]
		if document_type
		else sorted({r["document_type"] for r in listed} | set(extraction_config.BUILTINS))
	)
	in_use = {t: extraction_config.active(t)[1] or None for t in types}
	return ToolResult(
		data={"configs": listed, "in_use": in_use, "count": len(listed)},
		summary=f"{len(listed)} extraction config version(s)",
	)


def get_extraction_config(args: dict) -> ToolResult:
	"""One config body: `version`, else Published, else the built-in."""
	document_type = _document_type(args)
	version = as_str(args, "version")
	doc = extraction_config.row(document_type, version or None)
	if doc is not None:
		data = {**_config_row(doc), "config": extraction_config.body_of(doc), "source": "record"}
	elif version:
		raise ToolError(f"{document_type} has no version {version}. list_extraction_configs has them.")
	else:
		body, config_version = extraction_config.builtin(document_type)
		if body is None:
			raise ToolError(f"{document_type} has no published or built-in extraction config.")
		data = {
			"document_type": document_type,
			"status": "Built-in",
			"config_version": config_version,
			"config": body,
			"source": "builtin",
		}
	return ToolResult(data=data, summary=f"{data['config_version']} ({data['status']})")


def update_extraction_config(args: dict) -> ToolResult:
	"""Check a body and write it as a Draft at the next version. Never publishes."""
	_require_doctype()
	triage.require_manager("write an extraction config")
	document_type = _document_type(args)
	# v0.211.0. `from_builtin` drafts the body this app version ships for the
	# type — how a site that already has a Published row picks up a new built-in
	# (a migrate never overwrites one). It is still only a Draft.
	if as_bool(args, "from_builtin", False):
		if args.get("config") not in (None, "", {}):
			raise ToolError("pass config or from_builtin, not both. Nothing was written.")
		config, _version = extraction_config.builtin(document_type)
		if config is None:
			raise ToolError(f"{document_type} has no built-in extraction config. Nothing was written.")
	else:
		config = _json_arg(args, "config", dict, required=True)
	notes = as_str(args, "notes")
	if not notes:
		raise ToolError(
			"notes is required — say why this version exists (usually the App Feedback it answers)."
		)
	found = extraction_config.problems(config)
	if found:
		raise ToolError("the phone would refuse this config, so it was not written:\n- " + "\n- ".join(found))
	authored_by = as_str(args, "authored_by") or "AI-proposed"
	if authored_by not in ("Operator", "AI-proposed"):
		raise ToolError("authored_by is Operator or AI-proposed.")
	doc = extraction_config.create_draft(document_type, config, notes, authored_by)
	return ToolResult(
		data={
			**_config_row(doc),
			"next": (
				f"preview_extraction_config(document_type={document_type!r}, version={doc.version}, "
				f"validation=<a DVAL>) to see what it changes; stage_extraction_config(users=[…]) to "
				"try it on named accounts first; then publish_extraction_config to ship it."
			),
		},
		summary=f"drafted {doc.name}",
		docstatus_delta="none → 0 (draft)",
	)


def stage_extraction_config(args: dict) -> ToolResult:
	"""Draft → Staged for named accounts; everyone else keeps the Published version. v0.211.0."""
	_require_doctype()
	actor = triage.require_manager("stage an extraction config")
	document_type = _document_type(args)
	version = as_str(args, "version", required=True)
	users = args.get("users")
	if isinstance(users, str):
		users = [part for part in users.replace(",", "\n").splitlines()]
	try:
		doc, people = extraction_config.stage(document_type, version, users or [], actor)
	except (LookupError, ValueError) as exc:
		raise ToolError(f"{exc} Nothing was changed.") from exc
	return ToolResult(
		data={
			**_config_row(doc),
			"in_force_for_everyone_else": extraction_config.active(document_type)[1] or None,
			"next": (
				"Those accounts' phones fetch it at their next capture. When it reads right, "
				f"publish_extraction_config(document_type={document_type!r}, version={doc.version})."
			),
		},
		summary=f"staged {doc.name} to {', '.join(people)}",
		docstatus_delta="0 → 0 (updated)",
	)


def publish_extraction_config(args: dict) -> ToolResult:
	"""Draft → Published; the previous Published row → Superseded."""
	_require_doctype()
	actor = triage.require_manager("publish an extraction config")
	document_type = _document_type(args)
	version = as_str(args, "version", required=True)
	try:
		doc, previous = extraction_config.publish(document_type, version, actor)
	except (LookupError, ValueError) as exc:
		raise ToolError(f"{exc} Nothing was changed.") from exc
	return ToolResult(
		data={**_config_row(doc), "superseded": previous or None},
		summary=f"published {doc.name}" + (f", superseding {previous}" if previous else ""),
		docstatus_delta="0 → 0 (updated)",
	)


def preview_extraction_config(args: dict) -> ToolResult:
	"""Run a config against a stored Document Validation. Writes nothing."""
	validation = as_str(args, "validation", required=True)
	if not frappe.db.exists("Document Validation", validation):
		raise ToolError(f"no Document Validation called {validation!r}. list_document_validations has them.")
	source = frappe.get_doc("Document Validation", validation)
	document_type = _document_type(args) if as_str(args, "document_type") else source.document_type
	config = _json_arg(args, "config", dict)
	version = as_str(args, "version")
	if config is not None:
		found = extraction_config.problems(config)
		if found:
			raise ToolError("the phone would refuse this config:\n- " + "\n- ".join(found))
		config_version = "(unsaved)"
	elif version:
		doc = extraction_config.row(document_type, version)
		if doc is None:
			raise ToolError(f"{document_type} has no version {version}.")
		config, config_version = extraction_config.body_of(doc), doc.config_version
	else:
		config, config_version = extraction_config.active(document_type)
		if config is None:
			raise ToolError(f"{document_type} has no config to preview; pass config or version.")
	result = extraction_config.preview(config, source)
	result.update(
		{
			"validation": validation,
			"document_type": document_type,
			"config_version": config_version,
			"recorded_config_version": source.get("config_version") or None,
		}
	)
	return ToolResult(
		data=result,
		summary=(
			f"{config_version} on {validation}: {len(result['extracted'])} extracted, "
			f"{len(result['rule_failures'])} rule failure(s), {len(result['advisory_dropped'])} "
			f"advisory finding(s) dropped, {len(result['diff'])} difference(s)"
		),
	)


# ── feature flags ───────────────────────────────────────────────────────────


def list_feature_flags(args: dict) -> ToolResult:
	key = as_str(args, "key") or as_str(args, "flag_key")
	company = as_str(args, "company")
	if company:
		company = resolve_company(company) or company
	listed = [
		{**row, "value": flags.value_of(row), "roles": flags.role_list(row.get("roles"))}
		for row in flags.rows(key=key, company=company)
	]
	return ToolResult(
		data={
			"flags": listed,
			"count": len(listed),
			"note": "No row means the caller's default: new behaviour ships dark.",
		},
		summary=f"{len(listed)} feature flag row(s)",
	)


def set_feature_flag(args: dict) -> ToolResult:
	if not compat.doctype_exists(flags.DOCTYPE):
		raise ToolError("this site has no Farm Feature Flag doctype yet — run `bench --site <site> migrate`.")
	triage.require_manager("set a feature flag")
	flag_key = as_str(args, "flag_key", required=True)
	if not flags.KEY_PATTERN.match(flag_key):
		raise ToolError(f"{flag_key!r} is not a lower_snake_case flag key, e.g. label_capture_v2.")
	kind = as_str(args, "kind") or "Flag"
	if kind not in flags.KINDS:
		raise ToolError(f"kind is one of {', '.join(flags.KINDS)}.")
	if "value" not in args:
		raise ToolError("value is required: true/false for a Flag, a number for a Threshold, text for Text.")
	value = args.get("value")
	if kind == "Threshold":
		try:
			value = float(value)
		except (TypeError, ValueError) as exc:
			raise ToolError(f"a Threshold's value must be a number, got {value!r}.") from exc
	company = as_str(args, "company")
	if company:
		company = resolve_company(company) or company
	for key in ("min_app_version", "max_app_version"):
		if as_str(args, key) and flags.parse_version(as_str(args, key)) is None:
			raise ToolError(f"{key} {as_str(args, key)!r} is not a version like 1.42 or 1.42.3.")
	roles = args.get("roles") or ()
	doc, created = flags.upsert(
		flag_key,
		kind,
		value,
		company=company,
		roles=roles,
		min_app_version=as_str(args, "min_app_version"),
		max_app_version=as_str(args, "max_app_version"),
		description=as_str(args, "description") if "description" in args else None,
		owner_area=as_str(args, "owner_area") if "owner_area" in args else None,
		active=as_bool(args, "active") if "active" in args else None,
	)
	row = {f: doc.get(f) for f in flags.ROW_FIELDS if f != "modified"}
	return ToolResult(
		data={**row, "value": flags.value_of(row), "created": created},
		summary=f"{'created' if created else 'updated'} {flag_key} ({kind}) = {flags.value_of(row)!r}"
		+ (f" for {company}" if company else " globally"),
		docstatus_delta="none → 0 (draft)" if created else "0 → 0 (updated)",
	)


# ── triage ──────────────────────────────────────────────────────────────────


def _require_triage() -> None:
	if not triage.ready():
		raise ToolError(
			"this site's App Feedback has no triage fields yet — run `bench --site <site> migrate`."
		)


def _feedback(args: dict):
	name = app_feedback.find(as_str(args, "feedback", required=True))
	return frappe.get_doc(triage.DOCTYPE, name)


def list_triage_queue(args: dict) -> ToolResult:
	_require_triage()
	filters: dict = {}
	state = as_str(args, "state") or as_str(args, "triage_state")
	if state:
		if state not in triage.STATES:
			raise ToolError(f"state is one of {', '.join(triage.STATES)}.")
		filters["triage_state"] = state
	else:
		filters["triage_state"] = ("in", [triage.AUTO, triage.PROPOSED, triage.APPROVED, triage.TICKETED])
	triage_class = as_str(args, "triage_class")
	if triage_class:
		if triage_class not in triage.CLASSES:
			raise ToolError(f"triage_class is one of {', '.join(triage.CLASSES)}.")
		filters["triage_class"] = triage_class
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company) or company
	names = frappe.db.get_all(
		triage.DOCTYPE,
		filters=filters,
		pluck="name",
		order_by="creation desc",
		limit=as_limit(args),
	)
	rows = [triage.describe(frappe.get_doc(triage.DOCTYPE, name)) for name in names]
	counts: dict = {}
	for row in rows:
		counts[row["triage_state"] or "blank"] = counts.get(row["triage_state"] or "blank", 0) + 1
	return ToolResult(
		data={"queue": rows, "count": len(rows), "by_state": counts},
		summary=f"{len(rows)} note(s) in the triage queue",
	)


def propose_triage_fix(args: dict) -> ToolResult:
	"""Attach a fix (calls) or a ticket to a note. Never applies anything."""
	_require_triage()
	doc = _feedback(args)
	if doc.get("triage_state") == triage.APPLIED:
		raise ToolError(f"{doc.name} was already fixed ({doc.get('triage_summary')}). Nothing was changed.")
	triage_class = as_str(args, "triage_class", required=True)
	if triage_class not in triage.CLASSES:
		raise ToolError(f"triage_class is one of {', '.join(triage.CLASSES)}.")
	summary = as_str(args, "summary", required=True)
	calls = _json_arg(args, "calls", list)
	ticket = _json_arg(args, "ticket", dict)
	problems: list = []
	if triage_class == triage.DATA:
		if ticket:
			problems.append("a Data or config fix is calls, not a ticket")
		problems += triage.call_problems(calls)
	elif triage_class in (triage.BUG, triage.FEATURE):
		if calls:
			problems.append(f"a {triage_class} is a ticket, not calls — code is not fixed by data")
		problems += triage.ticket_problems(ticket)
	elif calls or ticket:
		problems.append(f"a {triage_class} carries neither calls nor a ticket: the summary is the answer")
	if problems:
		raise ToolError("the proposal was not attached:\n- " + "\n- ".join(problems))
	actor = security.caller_identity() or str(getattr(frappe.session, "user", "") or "")
	authored_by = as_str(args, "authored_by") or "AI-proposed"
	if authored_by not in triage.AUTHORS:
		raise ToolError(f"authored_by is one of {', '.join(triage.AUTHORS)}.")
	proposal = None
	if calls is not None or triage_class in (triage.QUESTION, triage.DUPLICATE):
		proposal = {
			"calls": [
				{"tool": c["tool"], "arguments": c.get("arguments") or {}, "why": c.get("why") or ""}
				for c in calls or []
			],
			"proposed_by": actor,
			"proposed_at": frappe.utils.now(),
			"authored_by": authored_by,
		}
	state = triage.TICKETED if ticket else triage.PROPOSED
	values = {
		"triage_class": triage_class,
		"triage_state": state,
		"triage_summary": summary,
		"proposal_json": json.dumps(proposal, default=str) if proposal else None,
		"ticket_json": json.dumps({k: ticket.get(k) for k in triage.TICKET_KEYS if k in ticket}, default=str)
		if ticket
		else None,
	}
	for key, value in values.items():
		doc.set(key, value)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return ToolResult(
		data={
			**triage.describe(doc),
			"next": "approve_triage_proposal (a manager) applies it; reject_triage_proposal declines it."
			if state == triage.PROPOSED
			else "The ticket is for a code change; resolve the note when the release ships.",
		},
		summary=f"{doc.name}: {state} ({triage_class})",
		docstatus_delta="0 → 0 (updated)",
	)


def _last_log(tool: str):
	rows = frappe.db.get_all(
		"MCP Action Log",
		filters={"tool_name": tool},
		pluck="name",
		order_by="creation desc",
		limit=1,
	)
	return rows[0] if rows else None


def _result_text(result: dict) -> str:
	return " ".join(part.get("text", "") for part in result.get("content") or () if isinstance(part, dict))


def approve(name: str, note: str = "", caller_ip: str = "") -> dict:
	"""Apply a Proposed note's calls, reply and resolve. Shared by the tool and Desk."""
	from .. import registry

	actor = triage.require_manager("approve a triage proposal")
	doc = frappe.get_doc(triage.DOCTYPE, name)
	state = doc.get("triage_state")
	if state == triage.TICKETED:
		raise ToolError(
			f"{name} is a ticket for a code change; there is nothing to apply. Resolve the note when the "
			"release ships."
		)
	if state not in (triage.PROPOSED, triage.APPROVED):
		raise ToolError(f"{name} is {state or 'not triaged'}; only a Proposed note can be approved.")
	proposal = triage.load(doc, "proposal_json") or {"calls": []}
	earlier = triage.load(doc, "applied_json") or {}
	done = {i for i, r in enumerate(earlier.get("results") or ()) if r.get("ok")}
	results = list(earlier.get("results") or [])
	failed = None
	for index, call in enumerate(proposal.get("calls") or ()):
		if index in done:
			continue
		outcome = registry.dispatch(call["tool"], call.get("arguments") or {}, caller_ip)
		ok = not outcome.get("isError")
		text = _result_text(outcome)
		entry = {
			"tool": call["tool"],
			"ok": ok,
			"summary": text[:400] if not ok else _ok_summary(text),
			"action_log": _last_log(call["tool"]),
		}
		if index < len(results):
			results[index] = entry
		else:
			results.append(entry)
		if not ok:
			failed = index
			break
		frappe.db.commit()
	applied = {
		"approved_by": actor,
		"approved_at": frappe.utils.now(),
		"results": results,
		"note": note,
	}
	doc = frappe.get_doc(triage.DOCTYPE, name)
	if failed is not None:
		applied["failed_at"] = failed
		doc.applied_json = json.dumps(applied, default=str)
		doc.triage_state = triage.APPROVED
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		return {
			**triage.describe(doc),
			"ok": False,
			"stopped_at": failed,
			"message": (
				f"call {failed} ({proposal['calls'][failed]['tool']}) failed: {results[failed]['summary']} "
				"The calls before it were applied and stay applied. Fix the cause (often a tool switched "
				"off) and approve again: calls already applied are not re-run. The note stays open."
			),
		}
	doc.applied_json = json.dumps(applied, default=str)
	doc.triage_state = triage.APPLIED
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	summary = doc.get("triage_summary") or "your note"
	reply = f"Fixed: {summary}" + (f"\n\n{note}" if note else "")
	if doc.get("triage_class") == triage.QUESTION:
		reply = summary + (f"\n\n{note}" if note else "")
	still_open = doc.get("status") not in app_feedback.ANSWERED
	if still_open and compat.has_field(triage.DOCTYPE, "replies"):
		app_feedback.reply_to_app_feedback(
			{
				"name": name,
				"reply": reply[: app_feedback.REPLY_MAX],
				"status": "Resolved",
				"resolution_note": (note or summary)[:1000],
				"author": actor,
				"author_name": actor,
			}
		)
	elif still_open:
		app_feedback.resolve_app_feedback(
			{"name": name, "status": "Resolved", "resolution_note": note or summary}
		)
	doc = frappe.get_doc(triage.DOCTYPE, name)
	return {**triage.describe(doc), "ok": True}


def _ok_summary(text: str) -> str:
	try:
		data = json.loads(text)
	except ValueError:
		return text[:200]
	if isinstance(data, dict):
		for key in ("name", "summary", "status"):
			if data.get(key):
				return f"{key}: {data[key]}"[:200]
	return "ok"


def approve_triage_proposal(args: dict) -> ToolResult:
	_require_triage()
	name = _feedback(args).name
	data = approve(name, as_str(args, "note"), security.caller_ip())
	return ToolResult(
		data=data,
		summary=f"{name}: "
		+ ("applied and resolved" if data.get("ok") else f"stopped at call {data['stopped_at']}"),
		docstatus_delta="0 → 0 (updated)",
	)


def reject(name: str, reason: str) -> dict:
	actor = triage.require_manager("reject a triage proposal")
	if not reason:
		raise ToolError("reason is required — the next proposal needs to know why this one was declined.")
	doc = frappe.get_doc(triage.DOCTYPE, name)
	if doc.get("triage_state") not in (triage.PROPOSED, triage.TICKETED, triage.APPROVED):
		raise ToolError(
			f"{name} is {doc.get('triage_state') or 'not triaged'}; there is no proposal to reject."
		)
	applied = triage.load(doc, "applied_json") or {}
	applied.update({"rejected_by": actor, "rejected_at": frappe.utils.now(), "reason": reason})
	doc.applied_json = json.dumps(applied, default=str)
	doc.triage_state = triage.REJECTED
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return triage.describe(doc)


def reject_triage_proposal(args: dict) -> ToolResult:
	_require_triage()
	name = _feedback(args).name
	data = reject(name, as_str(args, "reason"))
	return ToolResult(
		data=data,
		summary=f"{name}: proposal rejected; the note stays open",
		docstatus_delta="0 → 0 (updated)",
	)
