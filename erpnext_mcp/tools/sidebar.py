# SPDX-License-Identifier: MIT
"""The Desk sidebar, as tools. v0.172.0.

`erpnext_mcp/sidebar.py` is the engine and argues the design — including which
Frappe mechanism does the work and why `restrict_to_domain` is not it. This file
is the five doors onto it.

  list_workspace_visibility       every workspace on the site: who sees it, what is hidden. READ.
  get_user_sidebar                one person's sidebar, with the reason for every page left out. READ.
  configure_workspace_visibility  set which roles may see one workspace. MUTATING.
  hide_default_workspace          put a default page away, or bring it back. MUTATING.
  set_user_role_profile           give somebody one of the five farm profiles. MUTATING.

THE THREE MUTATING TOOLS ARE System Manager ONLY, and that is narrower than the
rest of this app on purpose: changing who can see what is an administrative act,
and a Farm Manager who could quietly take the Financial page off somebody's
sidebar is a different kind of power from one who can raise a task.
"""

from __future__ import annotations

import frappe

from .. import compat, sidebar
from ..args import as_str
from ..errors import ToolError
from ..result import ToolResult

#: Who may change the sidebar. See the module docstring.
ADMIN_ROLES = ("System Manager",)


def _admin(action: str, tail: str) -> str:
	"""Refuse anybody without the role, naming what they would have changed."""
	held = set(frappe.get_roles(frappe.session.user) or [])
	if held & set(ADMIN_ROLES):
		return str(frappe.session.user)
	raise ToolError(
		f"{frappe.session.user} may not {action}: it holds none of {', '.join(ADMIN_ROLES)}. "
		f"Who can see which workspace is an administrative decision. {tail}"
	)


def _require_workspace() -> None:
	compat.require_doctype(
		sidebar.WORKSPACE,
		"It is Frappe's own doctype and every supported version has it; a site without one has "
		"no Desk sidebar to configure.",
	)


# ── reading ─────────────────────────────────────────────────────────────────
def list_workspace_visibility(args: dict) -> ToolResult:
	_require_workspace()
	rows = sidebar.visibility()
	ours = [row for row in rows if row["ours"]]
	hidden = [row["workspace"] for row in rows if row["hidden"]]
	return ToolResult(
		data={
			"workspaces": rows,
			"count": len(rows),
			"ours": [row["workspace"] for row in ours],
			"hidden": hidden,
			"restricted": [row["workspace"] for row in rows if row["roles"]],
			"profiles": {profile: sidebar.profile_sidebar(profile) for profile in sorted(sidebar.PROFILES)},
			"note": (
				"A workspace with no roles is visible to everyone with Desk access — that is "
				"Frappe's own rule. Anybody holding Workspace Manager sees every page including "
				"the hidden ones."
			),
		},
		summary=(
			f"{len(rows)} workspace(s): {len(ours)} from this app, {len(hidden)} hidden, "
			f"{len([row for row in rows if row['roles']])} restricted by role."
		),
	)


def get_user_sidebar(args: dict) -> ToolResult:
	user = as_str(args, "user")
	if not user:
		raise ToolError("user (the login email, e.g. mary@example.com) is required.")
	_require_workspace()
	answer = sidebar.user_sidebar(user)
	return ToolResult(
		data=answer,
		summary=(
			f"{answer['user']} sees {len(answer['sees'])} workspace(s): "
			f"{', '.join(answer['sees']) or 'none'}."
		),
	)


# ── writing ─────────────────────────────────────────────────────────────────
def configure_workspace_visibility(args: dict) -> ToolResult:
	tail = "Nothing was changed."
	workspace = as_str(args, "workspace")
	if not workspace:
		raise ToolError(f"workspace (the docname, e.g. 'Financial') is required. {tail}")
	if "roles" not in args:
		raise ToolError(
			"roles is required: a list of role names, or an empty list to show the workspace to "
			f"everyone with Desk access. {tail}"
		)
	roles = args.get("roles")
	if isinstance(roles, str):
		roles = [part.strip() for part in roles.split(",") if part.strip()]
	if not isinstance(roles, list):
		raise ToolError(f"roles must be a list of role names. {tail}")

	_require_workspace()
	_admin("change who can see a workspace", tail)
	answer = sidebar.set_workspace_roles(workspace, roles)
	missing = answer["roles_not_on_this_site"]
	summary = f"{workspace}: visible to {', '.join(answer['roles_after']) or 'everyone with Desk access'}"
	if missing:
		summary += f" (dropped, not on this site: {', '.join(missing)})"
	return ToolResult(data=answer, summary=summary, docstatus_delta="")


def hide_default_workspace(args: dict) -> ToolResult:
	tail = "Nothing was changed."
	workspace = as_str(args, "workspace")
	if not workspace:
		raise ToolError(f"workspace (the docname, e.g. 'Manufacturing') is required. {tail}")
	hidden = args.get("hidden", True)
	if isinstance(hidden, str):
		hidden = hidden.strip().lower() not in ("0", "false", "no", "")
	hidden = bool(hidden)

	_require_workspace()
	_admin("hide or restore a workspace", tail)
	answer = sidebar.hide_workspace(workspace, hidden)

	# The operator's intent, so `after_migrate` re-applies exactly this and a page
	# somebody put back does not come back hidden after the next upgrade.
	keep = [name for name in sidebar.hidden_list() if name != workspace]
	if hidden:
		keep.append(workspace)
	answer["hidden_default_workspaces"] = sidebar.set_hidden_list(keep)
	return ToolResult(
		data=answer,
		summary=(
			f"{workspace} is {'hidden from' if hidden else 'back in'} the sidebar. "
			f"Nothing was deleted; a Workspace Manager still sees it."
		),
		docstatus_delta="",
	)


def set_user_role_profile(args: dict) -> ToolResult:
	tail = "Nothing was changed."
	user = as_str(args, "user")
	profile = as_str(args, "profile")
	if not user:
		raise ToolError(f"user (the login email) is required. {tail}")
	if not profile:
		raise ToolError(f"profile is required. One of: {', '.join(sorted(sidebar.PROFILES))}. {tail}")
	if profile not in sidebar.PROFILES:
		raise ToolError(
			f"{profile!r} is not a profile this app defines. One of: "
			f"{', '.join(sorted(sidebar.PROFILES))}. {tail}"
		)
	trim = args.get("trim_sidebar", True)
	if isinstance(trim, str):
		trim = trim.strip().lower() not in ("0", "false", "no", "")
	_admin("assign a role profile", tail)
	answer = sidebar.assign_profile(user, profile, trim_modules=bool(trim))
	return ToolResult(
		data=answer,
		summary=(
			f"{answer['user']} now holds the {profile} profile "
			f"({', '.join(answer['roles_added']) or 'no new roles — they held them already'}) "
			f"and sees {len(answer['workspaces'])} workspace(s)"
			+ (
				f", with {len(answer['modules_blocked'])} module(s) taken off their sidebar."
				if answer["modules_blocked"]
				else f". {answer['modules_note'] or ''}".rstrip()
			)
		),
		docstatus_delta="",
	)
