# SPDX-License-Identifier: MIT
"""Tell the Farm triage. v0.206.0.

docs/design/config_flags_triage.md §3. A note from the feedback bubble is
classified the moment it lands, with the records it names linked as evidence.
A person (or Claude, over MCP) then attaches either a PROPOSAL — a list of MCP
tool calls that would fix a data or config problem — or a TICKET for a code
change. NOTHING IS EVER APPLIED WITHOUT A PERSON: `approve` runs the proposal's
calls through the normal dispatcher, so every tool's own switch, role gate and
audit row still apply, and only then replies to the worker and resolves the note.

THE CLASSIFIER IS DETERMINISTIC AND NEVER PROPOSES. It reads words, not
meaning; its class is a first sort for the queue, and the proposal step is
where judgement comes in.
"""

from __future__ import annotations

import json
import re

import frappe

from . import compat

DOCTYPE = "App Feedback"
CLASSES = ("Data or config", "Code bug", "Feature request", "Question", "Duplicate")
DATA, BUG, FEATURE, QUESTION, DUPLICATE = CLASSES
STATES = ("Auto-classified", "Proposed", "Approved", "Applied", "Rejected", "Ticketed")
AUTO, PROPOSED, APPROVED, APPLIED, REJECTED, TICKETED = STATES
APPROVER_ROLES = ("System Manager", "Farm Manager")
#: App Feedback statuses that mean the farm has answered (`tools/app_feedback.ANSWERED`).
ANSWERED = ("Resolved", "Won't Fix")
AUTHORS = ("AI-proposed", "Operator")
SEVERITIES = ("low", "medium", "high", "critical")
MAX_CALLS = 10
#: Tools a proposal may not contain: the triage tools themselves.
NOT_PROPOSABLE = ("propose_triage_fix", "approve_triage_proposal", "reject_triage_proposal")
TICKET_KEYS = (
	"title",
	"repro_steps",
	"expected",
	"actual",
	"suspected_area",
	"affected_records",
	"app_version",
	"severity",
)

# ── classifying ─────────────────────────────────────────────────────────────

_BUG = re.compile(
	r"\b(crash\w*|freez\w*|froze|hangs?|stuck|won'?t (load|open|save|sync)|doesn'?t (load|open|save|sync|work)|"
	r"not (loading|working|saving|syncing)|broken|bug|error|spinner|blank screen|se cierra|no funciona|"
	r"no carga|se traba|error)\b",
	re.IGNORECASE,
)
_FEATURE = re.compile(
	r"(would be (nice|great|good|helpful)|please add|can (we|you) (add|have|get)|could (we|you) (add|have)|"
	r"\bwish\b|\bfeature\b|should be able|it would help|add an? option|sería (bueno|útil)|\bagregar\b|"
	r"\bañadir\b|\bpodrían\b)",
	re.IGNORECASE,
)
_DATA = re.compile(
	r"\b(wrong|incorrect|missing|misspell\w*|typo|should (be|say|read)|is listed|isn'?t listed|not listed|"
	r"out of date|outdated|rate|label|unit|uom|price|name|spelling|duplicate entry|incorrecto|falta|"
	r"equivocad[oa]|mal escrito)\b",
	re.IGNORECASE,
)
_QUESTION_START = re.compile(
	r"^\s*(¿|how|what|why|where|when|who|can i|is there|does|do i|should i|cómo|qué|dónde|cuándo|por qué|puedo)\b",
	re.IGNORECASE,
)

#: Docname prefixes the classifier links, with the doctype each names.
PREFIXES = {
	"FT": "Farm Task",
	"DVAL": "Document Validation",
	"AFB": "App Feedback",
	"CRULE": "Compliance Rule",
}
_NAMED = re.compile(r"\b(" + "|".join(PREFIXES) + r")-\d{4}(?:-\d{2})?-\d{3,6}\b")
_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]{1,}(?:[-_][A-Z0-9]+)+\b")


def _exists(doctype: str, name: str) -> bool:
	try:
		return compat.doctype_exists(doctype) and bool(frappe.db.exists(doctype, name))
	except Exception:
		return False


def linked_records(text: str, reference_doctype: str = "", reference_name: str = "", own: str = "") -> list:
	"""Every record the note names that exists here: docnames by prefix, item
	codes and asset tags by token, and the note's own reference."""
	out: list = []

	def add(doctype, name):
		entry = {"doctype": doctype, "name": name}
		if name and name != own and entry not in out and _exists(doctype, name):
			out.append(entry)

	if reference_doctype and reference_name:
		add(reference_doctype, reference_name)
	for match in _NAMED.finditer(text or ""):
		add(PREFIXES[match.group(1)], match.group(0))
	for token in dict.fromkeys(_TOKEN.findall(text or "")):
		if _NAMED.fullmatch(token):
			continue
		for doctype in ("Item", "Asset Register"):
			add(doctype, token)
	return out


def _duplicate_of(doc) -> str:
	"""An open note from the same screen saying the same thing, or ""."""
	wanted = " ".join(str(doc.get("feedback_text") or "").lower().split())
	if not wanted:
		return ""
	for row in frappe.db.get_all(
		DOCTYPE,
		filters={
			"status": ("not in", ANSWERED),
			"screen_name": doc.get("screen_name") or "",
			"name": ("!=", doc.name),
		},
		fields=["name", "feedback_text"],
		order_by="creation asc",
		limit=200,
	):
		if " ".join(str(row.get("feedback_text") or "").lower().split()) == wanted:
			return row["name"]
	return ""


def classify(doc) -> dict:
	"""{triage_class, triage_summary, evidence} for a note. Reads only."""
	text = str(doc.get("feedback_text") or "")
	evidence = {
		"screen": doc.get("screen_name") or "",
		"screen_label": doc.get("screen_label") or "",
		"reference": (
			{"doctype": doc.get("reference_doctype"), "name": doc.get("reference_name")}
			if doc.get("reference_doctype") and doc.get("reference_name")
			else None
		),
		"screenshot": doc.get("screenshot") or None,
		"linked_records": linked_records(
			text, doc.get("reference_doctype") or "", doc.get("reference_name") or "", doc.name
		),
		"app_version": doc.get("app_version") or "",
	}
	duplicate = _duplicate_of(doc)
	if duplicate:
		evidence["duplicate_of"] = duplicate
		evidence["linked_records"].insert(0, {"doctype": DOCTYPE, "name": duplicate})
		triage_class = DUPLICATE
	elif _BUG.search(text):
		triage_class = BUG
	elif _FEATURE.search(text):
		triage_class = FEATURE
	elif _DATA.search(text) or (evidence["linked_records"] and not text.rstrip().endswith("?")):
		triage_class = DATA
	elif text.rstrip().endswith("?") or _QUESTION_START.search(text):
		triage_class = QUESTION
	else:
		triage_class = DATA if evidence["linked_records"] else QUESTION
	gist = " ".join(text.split())
	gist = gist if len(gist) <= 140 else gist[:139].rstrip() + "…"
	where = evidence["screen_label"] or evidence["screen"] or "an unnamed screen"
	summary = f"{triage_class} on {where}: {gist}"
	if duplicate:
		summary = f"Duplicate of {duplicate} on {where}: {gist}"
	return {"triage_class": triage_class, "triage_summary": summary, "evidence": evidence}


def ready() -> bool:
	return compat.has_field(DOCTYPE, "triage_state")


def auto_classify(name: str, overwrite: bool = False) -> dict | None:
	"""Classify one note and store it. Only fills a blank triage unless `overwrite`."""
	if not ready():
		return None
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.get("triage_state") and not overwrite:
		return None
	found = classify(doc)
	frappe.db.set_value(
		DOCTYPE,
		name,
		{
			"triage_class": found["triage_class"],
			"triage_state": AUTO,
			"triage_summary": found["triage_summary"],
			"evidence_json": json.dumps(found["evidence"], default=str),
		},
		update_modified=False,
	)
	return found


# ── proposals ───────────────────────────────────────────────────────────────

_JSON_TYPES = {
	"string": (str,),
	"integer": (int,),
	"number": (int, float),
	"boolean": (bool,),
	"array": (list,),
	"object": (dict,),
	"null": (type(None),),
}


def _type_ok(value, wanted) -> bool:
	wanted = wanted if isinstance(wanted, list) else [wanted]
	for name in wanted:
		kinds = _JSON_TYPES.get(name)
		if kinds is None:
			return True
		if isinstance(value, bool) and name in ("integer", "number"):
			continue
		if isinstance(value, kinds):
			return True
	return False


def argument_problems(tool: str, arguments) -> list:
	"""Why `arguments` does not fit `tool`'s input schema, or []."""
	from . import registry

	spec = registry.TOOLS.get(tool)
	if spec is None:
		return [f"{tool!r} is not a tool on this server"]
	if not isinstance(arguments, dict):
		return [f"{tool}: arguments must be an object"]
	schema = spec["inputSchema"]
	properties = schema.get("properties") or {}
	out = [f"{tool}: {key!r} is required" for key in schema.get("required") or () if key not in arguments]
	for key, value in arguments.items():
		if key not in properties:
			if schema.get("additionalProperties") is False:
				out.append(f"{tool}: {key!r} is not one of its arguments ({', '.join(sorted(properties))})")
			continue
		wanted = properties[key].get("type")
		if wanted and not _type_ok(value, wanted):
			out.append(f"{tool}: {key!r} should be {wanted}, got {type(value).__name__}")
		allowed = properties[key].get("enum")
		if allowed and value not in allowed:
			out.append(f"{tool}: {key!r} must be one of {', '.join(map(str, allowed))}")
	return out


def call_problems(calls) -> list:
	from . import registry

	if not isinstance(calls, list) or not calls:
		return ["calls must be a non-empty list of {tool, arguments, why}"]
	if len(calls) > MAX_CALLS:
		return [f"a proposal holds at most {MAX_CALLS} calls; split the fix"]
	out = []
	for index, call in enumerate(calls):
		if not isinstance(call, dict) or not str(call.get("tool") or "").strip():
			out.append(f"calls[{index}] needs a tool")
			continue
		tool = call["tool"]
		spec = registry.TOOLS.get(tool)
		if spec is None:
			out.append(f"calls[{index}]: {tool!r} is not a tool on this server")
			continue
		if tool in NOT_PROPOSABLE:
			out.append(f"calls[{index}]: {tool} cannot be part of a proposal")
			continue
		if not spec["mutating"]:
			out.append(
				f"calls[{index}]: {tool} is a read tool — there is nothing to approve. Run it now "
				"and put what it found in the summary."
			)
			continue
		if not str(call.get("why") or "").strip():
			out.append(f"calls[{index}]: say why ({tool})")
		out.extend(f"calls[{index}]: {p}" for p in argument_problems(tool, call.get("arguments") or {}))
	return out


def ticket_problems(ticket) -> list:
	if not isinstance(ticket, dict):
		return ["ticket must be an object"]
	out = []
	unknown = sorted(set(ticket) - set(TICKET_KEYS))
	if unknown:
		out.append(f"ticket keys not in the contract: {', '.join(unknown)}")
	for key in ("title", "expected", "actual"):
		if not str(ticket.get(key) or "").strip():
			out.append(f"ticket.{key} is required")
	if ticket.get("severity") and ticket["severity"] not in SEVERITIES:
		out.append(f"ticket.severity must be one of {', '.join(SEVERITIES)}")
	return out


def load(doc, fieldname: str):
	try:
		value = json.loads(doc.get(fieldname) or "null")
	except ValueError:
		return None
	return value


def describe(doc) -> dict:
	return {
		"name": doc.name,
		"status": doc.get("status"),
		"screen": doc.get("screen_label") or doc.get("screen_name"),
		"feedback_text": doc.get("feedback_text"),
		"company": doc.get("company"),
		"app_version": doc.get("app_version"),
		"timestamp": str(doc.get("timestamp") or "") or None,
		"triage_class": doc.get("triage_class") or None,
		"triage_state": doc.get("triage_state") or None,
		"triage_summary": doc.get("triage_summary") or None,
		"evidence": load(doc, "evidence_json"),
		"proposal": load(doc, "proposal_json"),
		"ticket": load(doc, "ticket_json"),
		"applied": load(doc, "applied_json"),
	}


def require_manager(action: str) -> str:
	"""The caller, once it holds System Manager or Farm Manager."""
	from . import roles, security
	from .errors import ToolError

	actor = security.caller_identity() or str(getattr(frappe.session, "user", "") or "")
	if not actor or actor == "Guest":
		raise ToolError(f"this call has no identity to {action} as. Nothing was changed.")
	held = set(frappe.get_roles(actor) or []) or set(roles.all_roles_of(actor) or [])
	if not held & set(APPROVER_ROLES):
		raise ToolError(
			f"{actor} may not {action}: it holds neither {' nor '.join(APPROVER_ROLES)}. Configuration "
			"the phones read and fixes applied to live records are the farm manager's call. Grant one of "
			"those roles in the Desk to the account this app acts as (`mcp_system_user` on ERPNext MCP "
			"Settings). Nothing was changed."
		)
	return actor
