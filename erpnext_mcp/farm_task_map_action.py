# SPDX-License-Identifier: MIT
""" "Map View" on the Farm Task list and its dispatch Kanban, as a record. v0.161.0.

`/app/farm-overview` has drawn the farm's boundaries since v0.110.0 and, from
this release, the open jobs on them. The place somebody looks at open jobs is the
Farm Task Dispatch Kanban, and this is the one line of JavaScript that connects
the two.

WHY THE BOARD NEEDS A DOOR TO THE MAP AT ALL. A Kanban answers "what is
outstanding and who has it"; it cannot answer "where". A foreman reading six
Critical cards has no way to tell from that screen whether they are six jobs in
one block — an afternoon — or six jobs across four parcels, which is a day and a
truck. The map answers that in one glance and there was no way to reach it from
the board.

────────────────────────────────────────────────────────────────────────────
WHY A CLIENT SCRIPT RATHER THAN `doctype_list_js`
────────────────────────────────────────────────────────────────────────────

Farm Task is OURS, so the argument `badge_list_action.py` makes — that a
`doctype_list_js` entry for an ERPNext doctype breaks the promise `hooks.py`
opens with — does not apply and the hook was open. It goes to a Client Script
anyway, for the reason `asset_tag_form_action.py` states: an operator can SEE
this row in the Desk, can untick `enabled` or delete it, and this module will not
put it back. A hook file gets neither of those.

It goes when the app goes, because `before_uninstall` names it.

────────────────────────────────────────────────────────────────────────────
`onload` AND NOT `add_actions_menu_item`, AND THAT IS THE KANBAN'S DOING
────────────────────────────────────────────────────────────────────────────

The two list actions this app already ships hang off `add_actions_menu_item`,
which puts an entry in the menu that appears once rows are TICKED. That is right
for them: rendering a sheet of tags is a thing you do TO a selection.

A Kanban has no checkboxes. `frappe.views.KanbanView` extends `ListView` and
therefore still runs `frappe.listview_settings[doctype].onload`, but nothing on
that screen can be selected — so an Actions entry would be a door that never
opens on the one view this exists for. `page.add_menu_item` puts it in the ⋯ menu
instead, which is present on every list view type this doctype has: List, Report
and Kanban.

THE BUTTON CARRIES NO FILTER, and that is deliberate rather than unfinished. A
Kanban filtered to one crew does not mean the map should hide the other crew's
work — the question the map is being opened to answer is "where is all of this",
and a map that silently inherited a board filter would answer a narrower question
while looking like it answered the whole one. `/app/farm-overview` has its own
entity picker and its own layer toggles.

────────────────────────────────────────────────────────────────────────────
THE THREE STATES
────────────────────────────────────────────────────────────────────────────

Inherited whole from `badge_list_action`, via `asset_tag_list_action`:

  * NOT THERE — write it. An operator who deleted it is not in this state,
    because they get to keep declining it; a site that has never had it is.
  * THERE AND UNTOUCHED — the stored text fingerprints as one this app shipped
    (`PRIOR_REVISIONS`). Update it, because leaving it would leave a known bug on
    the site and it is this app's own text.
  * THERE AND EDITED — the fingerprint matches nothing this app ever shipped, so
    a person has been in it. LEFT EXACTLY AS IT IS, and `install.py` prints the
    revision they are on and the one they are missing.

AND IT DOES NOT OVERWRITE `frappe.listview_settings["Farm Task"]`. Nothing sets
one today, which makes this the easy case and exactly the case where an
assignment gets written and survives review — until something else starts setting
indicators for overdue tasks and silently loses them. The script reads what is
there, chains any existing `onload`, and adds one entry.
"""

from __future__ import annotations

import hashlib

import frappe

CLIENT_SCRIPT = "Client Script"
FARM_TASK = "Farm Task"
LIST_VIEW = "List"

#: The name this app gives the row. Frappe autonames Client Script, so this is a
#: request rather than a guarantee — which is why `_existing` looks for the marker.
SCRIPT_NAME = "Farm Task — Map View"

#: HOW THIS APP RECOGNISES ITS OWN ROW. Identity, and it never changes.
SCRIPT_MARKER = "erpnext_mcp:farm-task-map-action"

#: WHICH REVISION OF THE TEXT THIS APP SHIPS. Changes every time the script does.
SCRIPT_REVISION = "r1"

SCRIPT_STAMP = f"{SCRIPT_MARKER}@{SCRIPT_REVISION}"

#: Every earlier text this app shipped, fingerprinted by `_fingerprint`. Empty at
#: r1: there has never been an earlier revision, so any row that is not the
#: current text is one somebody has edited.
PRIOR_REVISIONS: dict[str, str] = {}

#: The route the button opens, spelled once so a test can hold the script and
#: `farm_overview.PAGE_ROUTE` to the same string. A button pointing at a page
#: that does not exist is a 404 an operator reads as a broken app.
MAP_ROUTE = "farm-overview"

SCRIPT_SOURCE = """// %(stamp)s
// Added by erpnext_mcp (v0.161.0). Untick `enabled` above, or delete this row,
// to remove the button — the app will not put it back.
//
// It adds ONE entry to the Farm Task list's ... menu, which is present on the
// List, Report and Kanban views alike. It does not replace
// frappe.listview_settings["Farm Task"]: nothing sets one today, and a script
// that assigned a fresh object over the top would silently take away whatever
// does later.

(function () {
	const settings = frappe.listview_settings["%(doctype)s"] || {};
	const previous_onload = settings.onload;

	settings.onload = function (listview) {
		if (typeof previous_onload === "function") {
			previous_onload(listview);
		}

		// GUARDED, because `onload` runs again every time somebody switches
		// between List, Report and Kanban on this doctype, and Frappe's menu
		// does not de-duplicate — without this the ... menu grows a new "Map
		// View" on every switch.
		if (listview.__erpnext_mcp_map_view) {
			return;
		}
		listview.__erpnext_mcp_map_view = true;

		listview.page.add_menu_item(__("Map View"), function () {
			frappe.set_route("%(route)s");
		});
	};

	frappe.listview_settings["%(doctype)s"] = settings;
})();
""" % {"stamp": SCRIPT_STAMP, "doctype": FARM_TASK, "route": MAP_ROUTE}


def _fingerprint(text: str) -> str:
	"""A hash of a script with line endings and trailing whitespace normalised.

	Spelled out here rather than imported for the reason these modules are
	separate at all: they are independent rows, and neither should stop working
	because the other was refactored.
	"""
	body = str(text or "").replace("\r\n", "\n").strip()
	lines = [line.rstrip() for line in body.split("\n")]
	return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _existing() -> str:
	"""The docname of this app's Client Script on the Farm Task list, or "".

	Matched on the marker in the source rather than on the docname, because
	Frappe autonames the doctype and an operator may rename the row.
	"""
	rows = frappe.db.get_all(
		CLIENT_SCRIPT,
		filters={"dt": FARM_TASK, "view": LIST_VIEW},
		fields=["name", "script"],
		limit=0,
	)
	for row in rows:
		if SCRIPT_MARKER in str(row.get("script") or ""):
			return str(row.get("name"))
	return ""


def seed_farm_task_map_action() -> dict:
	"""Create the Farm Task map button, or bring this app's own copy up to date.

	The three states are the module docstring's. Never raises. Returns
	`{"created": bool, "updated": bool, "name": str, "revision": str,
	"reason": str}` so `install.py` can say one true sentence whichever of the
	four happened.
	"""
	report = {
		"created": False,
		"updated": False,
		"name": SCRIPT_NAME,
		"revision": SCRIPT_REVISION,
		"reason": "",
	}
	try:
		if not frappe.db.exists("DocType", CLIENT_SCRIPT):  # pragma: no cover - not a real Frappe
			report["reason"] = "this site has no Client Script doctype"
			return report
		if not frappe.db.exists("DocType", FARM_TASK):
			report["reason"] = "this site has no Farm Task doctype — run `bench migrate` for this app first"
			return report

		found = _existing()
		if found:
			report["name"] = found
			stored = str(frappe.db.get_value(CLIENT_SCRIPT, found, "script") or "")
			if SCRIPT_STAMP in stored:
				report["reason"] = "already present"
				return report

			shipped = PRIOR_REVISIONS.get(_fingerprint(stored))
			if not shipped:
				report["reason"] = (
					"left alone — this site's copy has been edited, so it is not this app's "
					f"to rewrite. It is missing revision {SCRIPT_REVISION}"
				)
				return report

			doc = frappe.get_doc(CLIENT_SCRIPT, found)
			doc.script = SCRIPT_SOURCE
			doc.flags.ignore_permissions = True
			doc.save(ignore_permissions=True)
			report["updated"] = True
			report["reason"] = f"updated from {shipped.split(' —')[0]} to {SCRIPT_REVISION}"
			return report

		doc = frappe.get_doc(
			{
				"doctype": CLIENT_SCRIPT,
				"name": SCRIPT_NAME,
				"dt": FARM_TASK,
				"view": LIST_VIEW,
				"enabled": 1,
				"script": SCRIPT_SOURCE,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		report["created"] = True
		report["name"] = doc.name
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		report["reason"] = f"{type(exc).__name__}: {exc}"
	return report


def remove_farm_task_map_action() -> dict:
	"""Take this app's button off the Farm Task list before the app goes.

	CLEANUP RATHER THAN A WARNING — the same asymmetry `asset_tag_list_action`
	states. Only a row still carrying this app's marker is removed, so a script
	somebody has adopted and rewritten stays theirs. Never raises: an uninstall
	that died here would leave the app half-removed.
	"""
	report = {"removed": False, "name": SCRIPT_NAME, "reason": ""}
	try:
		if not frappe.db.exists("DocType", CLIENT_SCRIPT):  # pragma: no cover - not a real Frappe
			report["reason"] = "this site has no Client Script doctype"
			return report
		found = _existing()
		if not found:
			report["reason"] = "not present"
			return report
		report["name"] = found
		frappe.delete_doc(CLIENT_SCRIPT, found, ignore_permissions=True, force=True)
		report["removed"] = True
	except Exception as exc:  # pragma: no cover - a site mid-uninstall
		report["reason"] = f"{type(exc).__name__}: {exc}"
	return report


__all__ = (
	"MAP_ROUTE",
	"PRIOR_REVISIONS",
	"SCRIPT_MARKER",
	"SCRIPT_NAME",
	"SCRIPT_REVISION",
	"SCRIPT_SOURCE",
	"SCRIPT_STAMP",
	"remove_farm_task_map_action",
	"seed_farm_task_map_action",
)
