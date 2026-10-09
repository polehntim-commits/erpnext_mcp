# SPDX-License-Identifier: MIT
"""One tool for every link handed to someone outside. v0.282.0 (consolidation phase 4;
docs/design/plan_patterns_consolidation_2026_10.md).

`share_link(action, kind, …)` — create, extend, revoke, get, list, log, and `setup` where a kind has a share to set
up first — for the job links a contractor, supplier delivery / pickup or beekeeper opens (v0.271 / v0.275), the
packer portal's credentials (v0.276), one-time upload links (v0.244) and a new phone's enrolment link (v0.218).

NOTHING NEW UNDERNEATH: each action runs that kind's existing tool under its own switch, so every kind keeps its
token rules (shown once, only the hash kept), its expiry and its view log. A write needs `allow_share_link` AND that
tool's switch. The per-kind tools all remain. Merging the engines into one Share Link record is the later P2 step;
this tool's calls stay the same when it lands.
"""

from __future__ import annotations

from ..args import as_str
from ..errors import ToolError
from ..result import ToolResult
from .template_tool import call

ACTIONS = ("setup", "create", "extend", "revoke", "get", "list", "log")
#: kind → action → (tool, the argument `name` fills). Job links serve the contractor, delivery, pickup and
#: pollination jobs alike — the job's own template decides what its page shows.
KINDS = {
	"job": {
		"label": "Job link (contractor, supplier delivery / pickup, beekeeper)",
		"name_is": "create: the job; extend / revoke: the link id",
		"calls": {"create": ("create_job_link", "job"), "extend": ("extend_job_link", "link"),
		          "revoke": ("revoke_job_link", "link"), "list": ("list_job_links", "job"),
		          "log": ("list_job_links", "job")},
	},
	"packer": {
		"label": "Packer portal (a share, then one credential per contact)",
		"name_is": "the Packer Share (PKS-…); setup takes the share's fields in body",
		"calls": {"setup": ("create_packer_share", None), "create": ("issue_packer_credential", "share"),
		          "revoke": ("revoke_packer_access", "share"), "get": ("get_packer_share", "share"),
		          "list": ("list_packer_access_log", "share"), "log": ("list_packer_access_log", "share")},
	},
	"upload": {
		"label": "One-time upload link (a file too big to attach)",
		"name_is": "create: the document the file goes onto (with body.doctype); get / revoke: the UPL-… id",
		"calls": {"create": ("request_upload_url", "name"), "get": ("get_upload_status", "upload_id"),
		          "revoke": ("revoke_upload_link", "upload_id"), "list": ("list_upload_links", None),
		          "log": ("list_upload_links", None)},
	},
	"enrollment": {
		"label": "New phone pickup link (single use, no credential in it)",
		"name_is": "the account (login email) the phone is for",
		"calls": {"create": ("issue_enrollment_link", "user")},
	},
}
ALIASES = {"contractor": "job", "delivery": "job", "pickup": "job", "pollination": "job", "beekeeper": "job",
           "supplier": "job", "packer_portal": "packer", "upload_link": "upload", "enrolment": "enrollment",
           "phone": "enrollment"}
#: Top-level arguments passed through to the kind's tool when it takes them.
PASSED = ("days", "until", "reason", "contact_name", "contact_email", "company", "limit", "minutes", "device_name",
          "season", "status")


def _schema(tool: str) -> dict:
	from .. import registry

	return (registry.TOOLS.get(tool) or {}).get("inputSchema") or {}


def _kind(args: dict) -> tuple:
	raw = as_str(args, "kind", required=True).strip().lower().replace(" ", "_")
	kind = ALIASES.get(raw, raw)
	if kind not in KINDS:
		raise ToolError(f"kind is one of: {', '.join(KINDS)} (or contractor, delivery, pickup, pollination).")
	return kind, KINDS[kind]


def share_link(args: dict) -> ToolResult:
	action = as_str(args, "action", required=True).strip().lower()
	if action == "kinds":
		rows = [{"kind": k, "label": v["label"], "actions": sorted(v["calls"]), "name_is": v["name_is"]}
		        for k, v in KINDS.items()]
		return ToolResult(data={"kinds": rows, "count": len(rows), "aliases": ALIASES}, summary=f"{len(rows)} link kinds")
	kind, spec = _kind(args)
	if action == "describe":
		calls = {a: t for a, (t, _) in spec["calls"].items()}
		return ToolResult(data={"kind": kind, **spec, "calls": calls,
		                        "arguments": {a: sorted((_schema(t).get("properties") or {}).keys()) for a, t in calls.items()}},
		                  summary=f"{kind}: {', '.join(sorted(calls))}")
	if action not in ACTIONS:
		raise ToolError(f"action is one of: kinds, describe, {', '.join(ACTIONS)}.")
	if action not in spec["calls"]:
		raise ToolError(f"{kind} links have no {action} (they have {', '.join(sorted(spec['calls']))}).")
	tool, name_arg = spec["calls"][action]
	properties = _schema(tool).get("properties") or {}
	inner = dict(args.get("body") or {})
	for key in PASSED:
		if key in args and key in properties:
			inner.setdefault(key, args[key])
	name = as_str(args, "name")
	if name and name_arg:
		inner.setdefault(name_arg, name)
	if action == "log" and "include_views" in properties:
		inner.setdefault("include_views", True)
	if action == "list" and "include_views" in properties:
		inner.setdefault("include_views", False)
	return call(tool, inner)
