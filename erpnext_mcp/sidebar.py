# SPDX-License-Identifier: MIT
"""Who sees which workspace in the Desk sidebar, and which default pages are put away.

v0.172.0. A fresh ERPNext shows every module it ships: Manufacturing, Quality,
Projects, Support, Website, CRM, and the rest. On a farm most of that is noise,
and the nine workspaces v0.170.0 added sit underneath it. This module is the
answer to both halves — what to hide, and who should see what is left.

────────────────────────────────────────────────────────────────────────────
THE MECHANISM IS FRAPPE'S OWN, AND IT WAS READ OUT OF v15 BEFORE THIS WAS BUILT
────────────────────────────────────────────────────────────────────────────

`frappe/desk/desktop.py::get_workspace_sidebar_items` decides the sidebar, and
three things there matter:

  * `Workspace.roles` — a `Has Role` child table. `Workspace.is_permitted()`
    returns True when the table is EMPTY, and otherwise only for a user holding
    one of the roles named in it. That is the whole visibility mechanism, and it
    is what this module writes.
  * `is_hidden` — a public workspace with it set is left out of the sidebar for
    everybody except a Workspace Manager. That is how a default page is put away
    without deleting anything.
  * `"Workspace Manager" in frappe.get_roles()` — a Workspace Manager SEES
    EVERYTHING. The filters are skipped entirely for them, hidden pages
    included. So nothing here can lock an administrator out of their own Desk,
    which is the fifth thing the brief asked for and is a property of Frappe
    rather than a promise this module makes.

`restrict_to_domain` is NOT used. It filters against the site's active Domains
(Manufacturing, Retail, Services…), which is a statement about what kind of
business the site is, not about who is looking at it. Putting a role-shaped
decision in it would be a lie that happened to work.

────────────────────────────────────────────────────────────────────────────
PROFILES ARE BUNDLES OF ROLES THIS APP ALREADY SHIPS. NO NEW ROLE IS ADDED
────────────────────────────────────────────────────────────────────────────

`PROFILES` maps the five names an operator thinks in — Owner, Manager,
Bookkeeper, Field Supervisor, Land Owner — onto roles that already exist, and
`Role Profile` (a Frappe doctype, with `User.role_profile_name` to apply it) is
where they are written. A sixth role invented for navigation would be a sixth
thing to keep in step with `roles.py`, which already argues every role it ships.

`WORKSPACE_ROLES` IS DERIVED FROM `PROFILES` AND `PROFILE_WORKSPACES`, not typed
out a third time, and the derivation is an INTERSECTION rather than a union.

A role may see a page only when EVERY profile holding that role should see it.
The obvious rule — "a page's roles are every role held by a profile that should
see it" — is wrong, and wrong in the direction that leaks: `Family Member` is the
Land Owner's whole bundle and is ALSO in the Owner's, so a union would have put
Family Member on all nine pages and handed the Land Owner the task board. That
was caught by `test_sidebar.TheProfilesSeeWhatTheyShould`, which states the
intended sidebars independently and walks Frappe's own mechanism to them, rather
than by reading this paragraph back.

What each profile then SEES is still a union — Frappe checks each page against
all the caller's roles — and the two compose correctly: the Owner holds Farm
Manager, Accounts Manager and Family Member, so the Owner's sidebar is the
Manager's pages plus the Bookkeeper's plus the Land Owner's, which is all nine.

────────────────────────────────────────────────────────────────────────────
WHAT IS HIDDEN, AND WHAT IS DELIBERATELY LEFT ALONE
────────────────────────────────────────────────────────────────────────────

`HIDDEN_DEFAULTS` is six pages a tree-fruit operation does not use. Accounting
and HR STAY: they are where an operator who has used ERPNext before goes
looking, and Financial and Crew & Labor sit beside them rather than replacing
them.

`Agriculture` is NOT hidden by default, and that is a decision rather than an
omission. It belongs to another app on this bench, a farm may well be using it,
and putting away somebody else's module on a guess is worse than leaving one
extra line in the sidebar. `hide_default_workspace("Agriculture")` is one call
for an operator who wants it gone.

HIDING WRITES `is_hidden` ON A RECORD THIS APP DID NOT CREATE, which is the one
place this module touches another app's data. It is reversible in one call, it
deletes nothing, and `before_uninstall` puts every one of them back. It is also
RE-APPLIED ON EVERY MIGRATE, because a Workspace is force-synced from its app's
JSON: when ERPNext ships a new version of that file the flag goes back to 0, and
an operator who hid Manufacturing once should not find it back after an upgrade.
IT RE-APPLIES THE OPERATOR'S LIST AND NOT THE SHIPPED CONSTANT. `hidden_list`
reads `ERPNext MCP Settings.hidden_default_workspaces`, which
`hide_default_workspace` writes — so a page somebody deliberately put BACK is off
that list and the next migrate leaves it alone. Only a site where nobody has ever
touched the setting gets `HIDDEN_DEFAULTS`.
"""

from __future__ import annotations

import json

import frappe

from . import compat
from .dashboard import MODULE, WORKSPACE
from .errors import ToolError

SETTINGS = "ERPNext MCP Settings"

#: Where the operator's own answer to "which defaults are put away" is kept.
#: EMPTY STRING MEANS NOBODY HAS DECIDED YET and the shipped list applies; an
#: empty JSON LIST means somebody decided none, which is a different fact and is
#: why the two are not collapsed into one falsy value.
HIDDEN_SETTING = "hidden_default_workspaces"

MODULE_PROFILE = "Module Profile"
MODULE_DEF = "Module Def"

HAS_ROLE = "Has Role"
ROLE = "Role"
ROLE_PROFILE = "Role Profile"
USER = "User"

#: Every role Frappe hands an administrator to see past all of this. Read off
#: `get_workspace_sidebar_items`, where `has_access` skips every filter.
WORKSPACE_MANAGER = "Workspace Manager"

#: The role every page here also names, so an administrator who has NOT been
#: given Workspace Manager still sees this app's own pages.
ADMIN_ROLE = "System Manager"

#: The five profiles, and the roles each one bundles. Every role already exists:
#: five come from `roles.py` and two from ERPNext's own accounting set.
PROFILES = {
	"Owner": ("Farm Manager", "Accounts Manager", "Family Member"),
	"Manager": ("Farm Manager",),
	"Bookkeeper": ("Accounts Manager", "Accounts User"),
	"Field Supervisor": ("Foreman",),
	"Land Owner": ("Family Member",),
}

#: What each profile should find in its sidebar. The Owner sees all nine; the
#: rest are the sets the operation asked for.
PROFILE_WORKSPACES = {
	"Owner": (
		"Farm Operations",
		"Crew & Labor",
		"Compliance",
		"Crop Protection",
		"Assets & Equipment",
		"Land & Parcels",
		"Financial",
		"Market & Sales",
		"Map & Terrain",
	),
	"Manager": (
		"Farm Operations",
		"Crew & Labor",
		"Compliance",
		"Crop Protection",
		"Assets & Equipment",
		"Land & Parcels",
		"Map & Terrain",
	),
	"Bookkeeper": ("Financial", "Compliance", "Market & Sales", "Assets & Equipment"),
	"Field Supervisor": ("Farm Operations", "Crew & Labor", "Crop Protection", "Assets & Equipment"),
	"Land Owner": ("Land & Parcels", "Financial", "Assets & Equipment"),
}

#: THIS APP'S OTHER THREE PAGES, which predate the nine and carry no roles — so
#: Frappe was showing them to everyone, a Land Owner included. They are this
#: app's own, so gating them is the same decision as gating the nine rather than
#: a new one, and it is what takes a real Land Owner sidebar from ten entries to
#: seven. A page is named here only if this app built it.
OUR_OTHER_WORKSPACES = {
	"Farm Task Dispatch": ("Farm Manager", "Foreman"),
	"Irrigation": ("Farm Manager", "Foreman"),
	"Onboard Worker": ("Farm Manager",),
}

#: Roles that see a page although no profile bundles them. `Compliance Officer`
#: is the whole point of the Compliance page and is a role somebody holds
#: without being a manager; the admin role is on every page so that an
#: administrator without Workspace Manager still has a Desk.
EXTRA_WORKSPACE_ROLES = {
	"Compliance": ("Compliance Officer",),
}

#: Which modules each profile KEEPS. Everything else on the site is blocked for
#: that person through a `Module Profile`, which is how Frappe trims one user's
#: Desk — `User.get_blocked_modules` is read by the sidebar builder itself.
#:
#: ROLE VISIBILITY ALONE DOES NOT MAKE A SHORT SIDEBAR, and a real bench is what
#: proved it: with the nine pages gated and six defaults hidden, a Land Owner
#: still saw twenty-seven entries, because every remaining ERPNext and HRMS page
#: carries an EMPTY roles table and Frappe shows those to everyone. Hiding them
#: globally would take them from the bookkeeper too. Blocking per person is the
#: mechanism that matches the intent: this is the owner's view of HER Desk.
#:
#: THE OWNER IS `None` ON PURPOSE — no modules blocked. Somebody who runs the
#: whole operation gets everything, and a short sidebar they did not ask for
#: would be this app deciding what its owner may look at.
PROFILE_KEEPS_MODULES = {
	"Owner": None,
	"Manager": ("ERPNext MCP", "HR"),
	"Bookkeeper": ("ERPNext MCP", "Accounts", "Payroll"),
	"Field Supervisor": ("ERPNext MCP", "HR"),
	"Land Owner": ("ERPNext MCP", "Accounts"),
}

#: The default pages put away on a tree-fruit site. See the module docstring for
#: why Accounting, HR and Agriculture are not here.
HIDDEN_DEFAULTS = (
	"Manufacturing",
	"Quality",
	"Projects",
	"Support",
	"Website",
	"CRM",
)


def role_workspaces() -> dict:
	"""`{role: {workspace, …}}` — what one role may see.

	THE INTERSECTION, and the module docstring says why: a role shared by two
	profiles may only see what BOTH of them should. A union here hands the
	narrowest profile the widest one's pages.
	"""
	out: dict = {}
	for profile, roles in PROFILES.items():
		allowed = set(PROFILE_WORKSPACES.get(profile, ()))
		for role in roles:
			out[role] = out[role] & allowed if role in out else set(allowed)
	for workspace, roles in EXTRA_WORKSPACE_ROLES.items():
		for role in roles:
			out.setdefault(role, set()).add(workspace)
	return out


def workspace_roles() -> dict:
	"""`{workspace: [role, …]}`, derived from `role_workspaces` and nothing else.

	Every page also names the admin role, so an administrator who has not been
	given Workspace Manager still has a Desk. Deriving rather than typing is the
	point: the four-copies failure this app has hit before starts with a second
	list that looks authoritative.
	"""
	out: dict = {}
	for workspace_set in (set(names) for names in PROFILE_WORKSPACES.values()):
		for workspace in workspace_set:
			out.setdefault(workspace, set())
	for role, workspaces in role_workspaces().items():
		for workspace in workspaces:
			out.setdefault(workspace, set()).add(role)
	for workspace, roles in OUR_OTHER_WORKSPACES.items():
		out.setdefault(workspace, set()).update(roles)
	for workspace in out:
		out[workspace].add(ADMIN_ROLE)
	return {workspace: sorted(roles) for workspace, roles in out.items()}


def profile_sidebar(profile: str) -> list:
	"""Which of this app's workspaces a profile's roles actually reach.

	Computed the way Frappe computes it — union over the roles — rather than by
	reading `PROFILE_WORKSPACES` back. That is what makes the test that compares
	the two a real check instead of a tautology.
	"""
	roles = set(PROFILES.get(profile, ()))
	return sorted(workspace for workspace, allowed in workspace_roles().items() if roles & set(allowed))


def _roles_held(doc) -> list:
	"""The roles a User document carries, read off the document rather than the
	session cache — it is what the write actually changed."""
	return sorted({str(row.get("role") or "") for row in (doc.get("roles") or []) if row.get("role")})


def _roles_on(name: str) -> list:
	try:
		doc = frappe.get_doc(WORKSPACE, name)
	except Exception:
		return []
	return [str(row.get("role") or "") for row in (doc.get("roles") or [])]


def _role_exists(role: str) -> bool:
	try:
		return bool(frappe.db.exists(ROLE, role))
	except Exception:
		return False


def set_workspace_roles(name: str, roles) -> dict:
	"""Write one workspace's `roles` table. Returns what changed.

	AN EMPTY LIST MEANS EVERYBODY, which is Frappe's own rule
	(`is_permitted` returns True on an empty table) and is therefore how this
	tool spells "show it to everyone" rather than a way of saying nothing.

	A role this site does not have is DROPPED AND NAMED rather than created: a
	Role record conjured to satisfy a visibility rule is a role with no
	permissions behind it, and the operator would have to discover that for
	themselves.
	"""
	if not compat.doctype_exists(WORKSPACE):
		raise ToolError("this site has no Workspace doctype, so there is no sidebar to configure.")
	if not frappe.db.exists(WORKSPACE, name):
		raise ToolError(f"no Workspace called {name!r}. list_workspace_visibility has them.")

	wanted, missing = [], []
	for role in roles or []:
		role = str(role or "").strip()
		if not role:
			continue
		if _role_exists(role):
			if role not in wanted:
				wanted.append(role)
		else:
			missing.append(role)

	before = _roles_on(name)
	doc = frappe.get_doc(WORKSPACE, name)
	doc.set("roles", [])
	for role in wanted:
		doc.append("roles", {"role": role})
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return {
		"workspace": name,
		"roles_before": before,
		"roles_after": sorted(wanted),
		"visible_to_everyone": not wanted,
		"roles_not_on_this_site": sorted(missing),
	}


def apply_visibility() -> dict:
	"""Write the derived role lists onto this app's nine workspaces. Never raises.

	A WORKSPACE THIS APP DID NOT BUILD IS LEFT ALONE. The check is the module the
	page carries — the same ownership test `farm_workspaces.remove_farm_workspaces`
	uses — so a page an operator has moved out of this app's module keeps whatever
	visibility they gave it.
	"""
	report = {"configured": [], "skipped": [], "failed": [], "note": ""}
	if not compat.doctype_exists(WORKSPACE):
		report["note"] = "this site has no Workspace doctype, so there is no sidebar to configure"
		return report

	for name, roles in sorted(workspace_roles().items()):
		try:
			if not frappe.db.exists(WORKSPACE, name):
				report["skipped"].append({"workspace": name, "reason": "not on this site yet"})
				continue
			module = (
				frappe.db.get_value(WORKSPACE, name, "module")
				if compat.has_field(WORKSPACE, "module")
				else None
			)
			if module and str(module) != MODULE:
				report["skipped"].append(
					{
						"workspace": name,
						"reason": f"moved to the {module!r} module, so its visibility is not this app's to set",
					}
				)
				continue
			if sorted(_roles_on(name)) == sorted(roles):
				report["skipped"].append({"workspace": name, "reason": "already set"})
				continue
			report["configured"].append(set_workspace_roles(name, roles))
		except Exception as error:
			report["failed"].append({"name": name, "reason": f"{type(error).__name__}: {error}"})
	return report


# ── the default pages ───────────────────────────────────────────────────────
def hide_workspace(name: str, hidden: bool = True) -> dict:
	"""Set or clear `is_hidden` on one workspace. The only writer of that flag here."""
	if not compat.doctype_exists(WORKSPACE):
		raise ToolError("this site has no Workspace doctype.")
	if not frappe.db.exists(WORKSPACE, name):
		raise ToolError(
			f"no Workspace called {name!r} on this site. list_workspace_visibility has every one "
			"this site carries, with the name to pass here."
		)
	if not compat.has_field(WORKSPACE, "is_hidden"):
		raise ToolError(
			"this Frappe version's Workspace has no is_hidden column, so a page cannot be put "
			"away without deleting it — which this app will not do to a page it did not create."
		)
	before = bool(frappe.db.get_value(WORKSPACE, name, "is_hidden"))
	frappe.db.set_value(WORKSPACE, name, "is_hidden", 1 if hidden else 0)
	return {
		"workspace": name,
		"was_hidden": before,
		"is_hidden": bool(hidden),
		"changed": before != bool(hidden),
		"note": (
			"A Workspace Manager still sees it — Frappe skips every sidebar filter for that "
			"role. Nothing was deleted."
		),
	}


def hidden_list() -> list:
	"""The workspaces this site keeps out of the sidebar: the operator's list, or the shipped one.

	THE STORED VALUE IS THE OPERATOR'S INTENT AND IT WINS. `hide_default_workspace`
	writes it, so somebody who puts Manufacturing back gets a list without
	Manufacturing in it and the next migrate leaves it alone. Only a site where
	nobody has ever touched the setting falls back to `HIDDEN_DEFAULTS`.
	"""
	try:
		raw = frappe.db.get_single_value(SETTINGS, HIDDEN_SETTING)
	except Exception:
		return list(HIDDEN_DEFAULTS)
	if raw is None or str(raw).strip() == "":
		return list(HIDDEN_DEFAULTS)
	try:
		stored = json.loads(raw)
	except ValueError:
		return list(HIDDEN_DEFAULTS)
	return (
		[str(name) for name in stored if str(name or "").strip()]
		if isinstance(stored, list)
		else list(HIDDEN_DEFAULTS)
	)


def set_hidden_list(names) -> list:
	"""Store the operator's list. Returns what was written."""
	cleaned = []
	for name in names or []:
		name = str(name or "").strip()
		if name and name not in cleaned:
			cleaned.append(name)
	# THROUGH THE DOCUMENT, NOT `db.set_single_value`. `settings.seed_defaults`
	# writes this Single the same way, and it is the spelling that works on every
	# Frappe version this app supports.
	doc = frappe.get_doc(SETTINGS)
	doc.set(HIDDEN_SETTING, json.dumps(cleaned))
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return cleaned


def hide_defaults() -> dict:
	"""Put away the default pages this site keeps out of the sidebar. Idempotent, never raises.

	RE-APPLIED ON EVERY MIGRATE ON PURPOSE. A Workspace is force-synced from its
	app's own JSON, so an upgrade of ERPNext can set `is_hidden` back to 0 and an
	operator who put Manufacturing away would find it back. Re-running is the fix,
	and it is why this is in `after_migrate` rather than only in `after_install`.

	IT RE-APPLIES `hidden_list()`, NOT THE SHIPPED CONSTANT, so this cannot fight
	an operator: a page they put back is off the list and stays back.
	"""
	report = {"hidden": [], "already": [], "absent": [], "failed": []}
	if not compat.doctype_exists(WORKSPACE):
		report["note"] = "this site has no Workspace doctype"
		return report
	for name in hidden_list():
		try:
			if not frappe.db.exists(WORKSPACE, name):
				report["absent"].append(name)
				continue
			answer = hide_workspace(name, True)
			(report["hidden"] if answer["changed"] else report["already"]).append(name)
		except Exception as error:
			report["failed"].append({"name": name, "reason": f"{type(error).__name__}: {error}"})
	return report


def restore_defaults() -> dict:
	"""Put every hidden default back. What `before_uninstall` calls.

	A page this app hid belongs to another app, and leaving it hidden after this
	one is removed would be a change to somebody's Desk with nothing left on the
	bench to explain it.
	"""
	report = {"restored": [], "failed": []}
	if not compat.doctype_exists(WORKSPACE):
		return report
	for name in hidden_list():
		try:
			if frappe.db.exists(WORKSPACE, name) and frappe.db.get_value(WORKSPACE, name, "is_hidden"):
				hide_workspace(name, False)
				report["restored"].append(name)
		except Exception as error:  # pragma: no cover - a site mid-uninstall
			report["failed"].append({"name": name, "reason": f"{type(error).__name__}: {error}"})
	return report


# ── reading it back ─────────────────────────────────────────────────────────
def visibility() -> list:
	"""Every workspace on this site, with who can see it and why. Read-only."""
	if not compat.doctype_exists(WORKSPACE):
		raise ToolError("this site has no Workspace doctype.")
	rows = (
		frappe.db.get_all(
			WORKSPACE,
			fields=["name", "title", "module", "public", "is_hidden", "for_user", "sequence_id"],
			order_by="sequence_id asc",
			limit=500,
		)
		or []
	)
	out = []
	for row in rows:
		name = str(row.get("name") or "")
		roles = sorted(_roles_on(name))
		out.append(
			{
				"workspace": name,
				"title": row.get("title") or name,
				"module": row.get("module") or None,
				"ours": str(row.get("module") or "") == MODULE,
				"public": bool(row.get("public")),
				"hidden": bool(row.get("is_hidden")),
				"for_user": row.get("for_user") or None,
				"roles": roles,
				"visible_to": "everyone with Desk access" if not roles else ", ".join(roles),
				"sequence_id": row.get("sequence_id"),
			}
		)
	return out


def user_sidebar(user: str) -> dict:
	"""What one person's sidebar holds, computed the way Frappe computes it.

	Answers with the reason for every page LEFT OUT as well as every page shown,
	because "why can my mother not see the parcels" is the question this is
	actually asked, and a list of what she can see does not answer it.
	"""
	login = str(user or "").strip()
	if not login:
		raise ToolError("user (the login email) is required.")
	if not frappe.db.exists(USER, login):
		raise ToolError(f"no User called {login!r} on this site.")

	roles = set(frappe.get_roles(login) or [])
	manager = WORKSPACE_MANAGER in roles
	blocked = set()
	try:
		blocked = set(frappe.get_doc(USER, login).get_blocked_modules() or [])
	except Exception:
		blocked = set()

	shown, hidden_rows = [], []
	for row in visibility():
		if row["for_user"] and row["for_user"] != login:
			hidden_rows.append({**row, "reason": f"a private page belonging to {row['for_user']}"})
			continue
		if not manager and row["module"] and row["module"] in blocked:
			hidden_rows.append({**row, "reason": f"the {row['module']} module is blocked on this user"})
			continue
		if not manager and row["hidden"]:
			hidden_rows.append({**row, "reason": "hidden on this site"})
			continue
		if not manager and row["roles"] and not (roles & set(row["roles"])):
			hidden_rows.append({**row, "reason": "needs one of: " + ", ".join(row["roles"])})
			continue
		shown.append(row)

	return {
		"user": login,
		"roles": sorted(roles),
		"is_workspace_manager": manager,
		"profile": profile_of(login),
		"sees": [row["workspace"] for row in shown],
		"sees_detail": shown,
		"does_not_see": hidden_rows,
		"note": (
			"This user holds Workspace Manager, so Frappe shows them every workspace including hidden ones."
			if manager
			else ""
		),
	}


def profile_of(user: str) -> str | None:
	"""The Role Profile assigned to a user, where the field exists."""
	try:
		if not compat.has_field(USER, "role_profile_name"):
			return None
		return frappe.db.get_value(USER, user, "role_profile_name") or None
	except Exception:
		return None


# ── the profiles ────────────────────────────────────────────────────────────
def ensure_profile(profile: str) -> dict:
	"""Create or repair one Role Profile from `PROFILES`. Returns what it holds.

	A role missing from this site is dropped and named, for `set_workspace_roles`'
	reason. A profile whose roles are ALL missing is refused rather than created
	empty — an empty Role Profile assigned to somebody grants nothing and looks
	like it worked.
	"""
	if profile not in PROFILES:
		raise ToolError(
			f"{profile!r} is not a profile this app defines. Known: {', '.join(sorted(PROFILES))}."
		)
	if not compat.doctype_exists(ROLE_PROFILE):
		raise ToolError("this Frappe version has no Role Profile doctype.")

	wanted = [role for role in PROFILES[profile] if _role_exists(role)]
	missing = [role for role in PROFILES[profile] if not _role_exists(role)]
	if not wanted:
		raise ToolError(
			f"none of the roles {profile!r} bundles are on this site ({', '.join(missing)}). "
			"They arrive with `bench migrate` for this app's own roles, and with ERPNext for "
			"the accounting ones."
		)

	name = f"Farm {profile}"
	existing = frappe.db.exists(ROLE_PROFILE, name)
	answer = {
		"profile": profile,
		"role_profile": name,
		"created": False,
		"changed": False,
		"roles": wanted,
		"roles_not_on_this_site": missing,
		"workspaces": profile_sidebar(profile),
	}

	if existing:
		doc = frappe.get_doc(ROLE_PROFILE, name)
		if _roles_held(doc) == sorted(wanted):
			# NOTHING TO DO, AND SAVING ANYWAY IS NOT FREE. Frappe's Role Profile
			# controller enqueues `update_all_users` through `queue_action`, which
			# LOCKS the document until that job runs — so a no-op save on every
			# migrate both churns every user holding the profile and leaves a lock
			# that makes the next call throw DocumentLockedError. Proven on a real
			# bench, where the second `ensure_profile` in one session did exactly
			# that.
			return answer
	else:
		doc = frappe.new_doc(ROLE_PROFILE)
		doc.role_profile = name
		doc.name = name
		doc.flags.name_set = True

	doc.set("roles", [])
	for role in wanted:
		doc.append("roles", {"role": role})
	doc.flags.ignore_permissions = True
	try:
		doc.save(ignore_permissions=True) if existing else doc.insert(ignore_permissions=True)
	except Exception as error:
		if type(error).__name__ == "DocumentLockedError":
			raise ToolError(
				f"the {name!r} role profile is locked while Frappe applies an earlier change to "
				"everyone holding it. Try again in a moment."
			) from None
		raise
	answer["created"] = not existing
	answer["changed"] = True
	return answer


def ensure_profiles() -> dict:
	"""Build all five Role Profiles. Never raises; used by the installer."""
	report = {"profiles": [], "failed": []}
	for profile in PROFILES:
		try:
			built = ensure_profile(profile)
			built["modules"] = ensure_module_profile(profile)
			report["profiles"].append(built)
		except ToolError as error:
			report["failed"].append({"name": profile, "reason": str(error)})
		except Exception as error:
			report["failed"].append({"name": profile, "reason": f"{type(error).__name__}: {error}"})
	return report


def site_modules() -> list:
	"""Every module on this site, from Frappe's own register."""
	try:
		return sorted(
			str(row.get("name")) for row in frappe.db.get_all(MODULE_DEF, fields=["name"], limit=500) or []
		)
	except Exception:
		return []


def ensure_module_profile(profile: str) -> dict:
	"""The blocked-module bundle for one profile, or nothing for a profile that keeps all.

	Same no-op guard as `ensure_profile`, and for the same reason: a save that
	changes nothing is still a save.
	"""
	keeps = PROFILE_KEEPS_MODULES.get(profile)
	if keeps is None:
		return {"profile": profile, "module_profile": None, "blocked": [], "note": "keeps every module"}
	if not compat.doctype_exists(MODULE_PROFILE):
		return {
			"profile": profile,
			"module_profile": None,
			"blocked": [],
			"note": "this Frappe version has no Module Profile doctype, so no modules were blocked",
		}

	blocked = [module for module in site_modules() if module not in set(keeps)]
	name = f"Farm {profile}"
	existing = frappe.db.exists(MODULE_PROFILE, name)
	if existing:
		doc = frappe.get_doc(MODULE_PROFILE, name)
		current = sorted({str(row.get("module") or "") for row in (doc.get("block_modules") or [])})
		if current == sorted(blocked):
			return {"profile": profile, "module_profile": name, "blocked": blocked, "changed": False}
	else:
		doc = frappe.new_doc(MODULE_PROFILE)
		doc.module_profile_name = name
		doc.name = name
		doc.flags.name_set = True

	doc.set("block_modules", [])
	for module in blocked:
		doc.append("block_modules", {"module": module})
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True) if existing else doc.insert(ignore_permissions=True)
	return {"profile": profile, "module_profile": name, "blocked": blocked, "changed": True}


def assign_profile(user: str, profile: str, trim_modules: bool = True) -> dict:
	"""Give one user a profile's roles, through Frappe's own Role Profile field.

	`User.role_profile_name` is what Frappe reads in `populate_role_profile_roles`
	on save, so setting it ADDS the profile's roles. It does not remove roles the
	person already holds, and this reports both facts rather than implying a
	swap: taking a role away is a decision with consequences for records they
	created, and it belongs to whoever is watching, not to a convenience call.
	"""
	login = str(user or "").strip()
	if not login:
		raise ToolError("user (the login email) is required.")
	if not frappe.db.exists(USER, login):
		raise ToolError(f"no User called {login!r} on this site.")
	built = ensure_profile(profile)

	doc = frappe.get_doc(USER, login)
	before = _roles_held(doc)
	if not compat.has_field(USER, "role_profile_name"):
		raise ToolError("this Frappe version's User has no role_profile_name field.")
	doc.role_profile_name = built["role_profile"]

	# THE ROLES ARE APPENDED HERE AS WELL AS NAMED. Frappe's own User controller
	# expands `role_profile_name` on save, and appending the same roles first is
	# idempotent there — `append_roles` skips what the user already holds. Relying
	# on the controller alone would make this call depend on a version's internals
	# for its entire effect, which is the sort of thing that fails quietly.
	for role in built["roles"]:
		if role not in before:
			doc.append("roles", {"role": role})
	# THE MODULE BUNDLE, AND NEVER FOR AN ADMINISTRATOR. Blocking modules for
	# somebody who administers the site would take the Desk they work in away
	# from them, so a System Manager or Workspace Manager keeps every module and
	# the answer says so.
	held_roles = set(frappe.get_roles(login) or []) | set(before)
	administers = bool(held_roles & {ADMIN_ROLE, WORKSPACE_MANAGER})
	modules = {"module_profile": None, "blocked": [], "note": ""}
	if not trim_modules:
		modules["note"] = "asked not to trim the sidebar, so no module was blocked"
	elif administers:
		modules["note"] = (
			f"{login} administers this site ({', '.join(sorted(held_roles & {ADMIN_ROLE, WORKSPACE_MANAGER}))}), "
			"so no module was blocked — an administrator keeps the whole Desk"
		)
	else:
		modules = ensure_module_profile(profile)
		if modules.get("module_profile") and compat.has_field(USER, "module_profile"):
			doc.module_profile = modules["module_profile"]
			doc.set("block_modules", [])
			for module in modules["blocked"]:
				doc.append("block_modules", {"module": module})

	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	try:
		frappe.clear_cache(user=login)
	except Exception:  # pragma: no cover - a double with no cache
		pass
	after = _roles_held(frappe.get_doc(USER, login))
	return {
		"user": login,
		"profile": profile,
		"role_profile": built["role_profile"],
		"roles_added": sorted(set(after) - set(before)),
		"roles_before": before,
		"roles_now": after,
		"workspaces": profile_sidebar(profile),
		"module_profile": modules.get("module_profile"),
		"modules_blocked": modules.get("blocked") or [],
		"modules_note": modules.get("note") or "",
		"note": (
			"Roles this person already held were kept — a profile adds, it does not take away. "
			"Remove a role on the User form if that is what you want, and clear Module Profile "
			"there to give the whole Desk back."
		),
	}
