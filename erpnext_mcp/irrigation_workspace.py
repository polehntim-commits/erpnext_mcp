# SPDX-License-Identifier: MIT
"""The "Irrigation" landing page: the valves, the zones, the log, the water.

WHAT THIS IS NOT. It is not a register. Every valve on this farm has been a
Frappe document since v0.25.0 — an `Asset Register` row whose `asset_type` is
"Irrigation Valve", whose docname is the string printed on the zip-tied tag, and
which has had a draggable GPS pin on its own form since v0.145.0. Thirty-three of
them are on the Orchard Meadow site right now, every one carrying a coordinate.
`tools/valves.py` opens with the argument for why a second `Irrigation Valve`
doctype would be a second account of the same pipe, and nothing here disturbs it.

WHAT WAS ACTUALLY MISSING WAS THE DOOR. This app ships no workspace of its own
for any of that. Reaching the valves meant knowing to search "Asset Register" in
the awesomebar and then knowing to set the Asset Type filter by hand — and a
register you have to already know about is one an operator reports as absent.
`asset_type` and `valve_type` have both been `in_standard_filter` all along; what
did not exist was anything that said so.

SO THE VALVE SHORTCUT CARRIES ITS FILTER. `Workspace Shortcut.stats_filter` is
read twice by Frappe's own widget — once for the count badge on the card, and
once at `shortcut_widget.js`'s click handler, which sets `frappe.route_options`
from it before routing. One field, and the card both COUNTS the valves and lands
on a list already narrowed to them. That is the whole difference between a
shortcut to Asset Register and a shortcut to the valves.

    let filters = frappe.utils.get_filter_from_json(this.stats_filter);
    if (this.type == "DocType" && filters) {
        frappe.route_options = filters;
    }

IT IS BUILT IN CODE RATHER THAN SHIPPED AS A workspace/*.json, and that is the
one decision here worth defending. A Workspace JSON in the module folder is in
Frappe's `IMPORTABLE_DOCTYPES` and is FORCE-SYNCED by every `bench migrate` —
which would silently overwrite an operator's rearrangement of their own landing
page on every single upgrade. `dashboard._build_dispatch_workspace` argued this
first and `onboard_worker` follows it: build it once, and never touch a page
somebody has arranged. `_workspace_is_empty` is that guard and it is the reason
this file exists instead of a JSON.

Best-effort throughout, for `onboard_worker`'s reason: the Workspace doctype has
been rewritten twice across the Frappe versions this app supports. Every field
and every child table is written only where the site has it, every shortcut is
dropped if its doctype is absent, and a failure is REPORTED rather than raised.
"""

from __future__ import annotations

import json

import frappe

from . import compat
from .dashboard import MODULE, WORKSPACE, _select_value, _slug, _workspace_is_empty

#: The docname and the title. `/app/irrigation` is the route Frappe derives.
WORKSPACE_NAME = "Irrigation"

ASSET_REGISTER = "Asset Register"
IRRIGATION_ZONE = "Irrigation Zone"
ASSET_STATE_LOG = "Asset State Log"
WATER_TEST = "Water Test"

#: The `asset_type` that makes a register row a valve — `tools.valves.VALVE`,
#: spelled again here for the reason the patch spells it: this module is read at
#: install time on sites whose app tree may be mid-upgrade, and the string is the
#: doctype's own Select option either way.
VALVE = "Irrigation Valve"

#: The row of shortcuts across the top.
#:
#: THE FIRST ONE IS THE WHOLE RELEASE. `stats_filter` narrows Asset Register to
#: the valves, which makes the card read "33 tagged" and makes the click land on
#: a filtered list. Without it this would be a shortcut to the asset register —
#: which is the thing an operator already could not find the valves in.
#:
#: THERE IS DELIBERATELY NO "OPEN VALVES" CARD. `current_state` is a JSON column
#: and a valve that has never been toggled is CLOSED by the state machine's
#: default with the column still EMPTY — see `tools.valves._state_of` — so a
#: `stats_filter` of `{"current_state": "open"}` would both miscount and mislead.
#: The state belongs on a screen that resolves it, which is what
#: `list_irrigation_valves` and the valve form already do.
IRRIGATION_SHORTCUTS = (
	{
		"label": "Irrigation Valves",
		"link_to": ASSET_REGISTER,
		"type": "DocType",
		"doc_view": "List",
		"color": "Blue",
		"format": "{} tagged",
		"stats_filter": {"asset_type": VALVE},
		"why": (
			"Every valve on the farm, with its rank, its parent, its zone and its pin. "
			"The tag on the gate is the docname."
		),
	},
	{
		"label": "Irrigation Zones",
		"link_to": IRRIGATION_ZONE,
		"type": "DocType",
		"doc_view": "List",
		"why": (
			"The zone a valve draws through, and the only place this app holds a flow "
			"rate — which is what turns a valve's minutes into gallons."
		),
	},
	{
		"label": "Valve State Log",
		"link_to": ASSET_STATE_LOG,
		"type": "DocType",
		"doc_view": "List",
		"why": (
			"Every open and every close anybody logged. Runtime is summed from these "
			"rows and from nothing else."
		),
	},
	{
		"label": "Water Tests",
		"link_to": WATER_TEST,
		"type": "DocType",
		"doc_view": "List",
		"why": "The sampling record the produce-safety rule asks for.",
	},
)

#: The link cards under the shortcuts. Kept short on purpose: this is an
#: irrigation page, not an index of the app, and every register named here is one
#: somebody opens while thinking about water.
IRRIGATION_LINK_CARDS = (
	("Irrigation", (ASSET_REGISTER, IRRIGATION_ZONE, ASSET_STATE_LOG)),
	("Water", (WATER_TEST,)),
)


def available() -> bool:
	"""Whether this site can have the page at all.

	`Workspace` AND `Asset Register`: the valves ARE the asset register on this
	app, so a site without it has nothing for this page to point at.
	"""
	return compat.doctype_exists(WORKSPACE) and compat.doctype_exists(ASSET_REGISTER)


def install_irrigation_workspace() -> dict:
	"""Build or repair the Irrigation workspace.

	Never raises. Returns a report `install.py` can print one true sentence from:
	`{"created": bool, "filled": bool, "existed": bool, "blocks": int,
	"shortcuts": int, "note": str, "failed": list}`.
	"""
	report = {
		"created": False,
		"filled": False,
		"existed": False,
		"blocks": 0,
		"shortcuts": 0,
		"note": "",
		"failed": [],
	}
	if not compat.doctype_exists(WORKSPACE):
		report["note"] = "this site has no Workspace doctype, so there is no landing page to build"
		return report
	if not compat.doctype_exists(ASSET_REGISTER):
		report["note"] = (
			f"this site has no {ASSET_REGISTER} doctype — the valves live in it, so an "
			"irrigation page would be a page of dead links"
		)
		return report

	try:
		existing = frappe.db.exists(WORKSPACE, WORKSPACE_NAME)
		if existing and not _workspace_is_empty(WORKSPACE_NAME):
			# Somebody arranged this page. Leave it exactly as they left it.
			report["existed"] = True
			return report

		doc = frappe.get_doc(WORKSPACE, WORKSPACE_NAME) if existing else frappe.new_doc(WORKSPACE)
		if existing:
			# Repairing a page this app shipped blank. Clear the child tables first
			# so a partial set cannot be doubled.
			for fieldname in ("shortcuts", "links", "number_cards", "charts"):
				if compat.has_field(WORKSPACE, fieldname):
					doc.set(fieldname, [])
		else:
			doc.name = WORKSPACE_NAME
			doc.flags.name_set = True

		for fieldname, value in (
			("title", WORKSPACE_NAME),
			("label", WORKSPACE_NAME),
			("module", MODULE),
			("icon", "agriculture"),
			("public", 1),
			("is_hidden", 0),
			# After Onboard Worker (21.0). Irrigation is a page somebody opens in
			# season, several times a week, and neither of the two before it is.
			("sequence_id", 22.0),
		):
			if compat.has_field(WORKSPACE, fieldname):
				doc.set(fieldname, value)

		content = _build_content(doc, report)
		if compat.has_field(WORKSPACE, "content"):
			doc.content = json.dumps(content)

		doc.save(ignore_permissions=True) if existing else doc.insert(ignore_permissions=True)
		report["filled" if existing else "created"] = True
		report["blocks"] = len(content)
	except Exception as exc:
		report["failed"].append(
			{"name": f"{WORKSPACE_NAME} workspace", "reason": f"{type(exc).__name__}: {exc}"}
		)
	return report


def _build_content(doc, report: dict) -> list:
	"""Build the page, appending each child row as the block that renders it.

	THE TWO HAVE TO BE WRITTEN TOGETHER — `dashboard._workspace_content`'s rule
	and the bug v0.16.0 shipped: a modern Frappe renders ONLY what `content`
	names. A shortcut row with no block is invisible; a block naming a row that
	does not exist is a rendering error. One pass, and nothing can drift.
	"""
	content = []

	def block(kind: str, key: str, value: str, col: int) -> None:
		content.append({"id": _slug(f"{kind}-{value}")[:32], "type": kind, "data": {key: value, "col": col}})

	def header(text: str) -> None:
		content.append(
			{
				"id": _slug(f"header-{text}")[:32],
				"type": "header",
				"data": {"text": f'<span class="h4"><b>{text}</b></span>', "col": 12},
			}
		)

	def paragraph(text: str) -> None:
		content.append(
			{"id": _slug(f"para-{text}")[:32], "type": "paragraph", "data": {"text": text, "col": 12}}
		)

	if compat.has_field(WORKSPACE, "shortcuts"):
		header("The water")
		# SAYS WHERE THE VALVES ACTUALLY LIVE, because the first thing somebody
		# does on finding this page is wonder why there is no Irrigation Valve
		# list. There is not one, there has never been one, and the sentence that
		# explains it belongs on the page rather than in a release note.
		paragraph(
			"A valve is an Asset Register record — the tag on the gate is its name. "
			"The first card opens the register already filtered to them."
		)
		for spec in IRRIGATION_SHORTCUTS:
			if not compat.doctype_exists(spec["link_to"]):
				continue
			row = {"label": spec["label"], "link_to": spec["link_to"]}
			kind = _select_value("Workspace Shortcut", "type", spec.get("type") or "DocType")
			if kind:
				row["type"] = kind
			view = _select_value("Workspace Shortcut", "doc_view", spec.get("doc_view") or "")
			if view and compat.has_field("Workspace Shortcut", "doc_view"):
				row["doc_view"] = view
			# The filter, the badge and its colour. All three are optional on the
			# older Workspace Shortcut shapes, and a card without them is still a
			# working card — just one that counts the whole register.
			if spec.get("stats_filter") and compat.has_field("Workspace Shortcut", "stats_filter"):
				row["stats_filter"] = json.dumps(spec["stats_filter"], indent=1)
			for fieldname in ("color", "format"):
				if spec.get(fieldname) and compat.has_field("Workspace Shortcut", fieldname):
					row[fieldname] = spec[fieldname]
			doc.append("shortcuts", row)
			block("shortcut", "shortcut_name", spec["label"], 3)
			report["shortcuts"] += 1

	if compat.has_field(WORKSPACE, "links"):
		header("The registers underneath")
		for card_name, links in IRRIGATION_LINK_CARDS:
			present = [link for link in links if compat.doctype_exists(link)]
			if not present:
				continue
			break_row = {"label": card_name, "link_count": len(present)}
			kind = _select_value("Workspace Link", "type", "Card Break")
			if kind:
				break_row["type"] = kind
			doc.append("links", break_row)
			for link in present:
				link_row = {"label": link, "link_to": link}
				kind = _select_value("Workspace Link", "type", "Link")
				if kind:
					link_row["type"] = kind
				link_type = _select_value("Workspace Link", "link_type", "DocType")
				if link_type:
					link_row["link_type"] = link_type
				doc.append("links", link_row)
			block("card", "card_name", card_name, 4)

	return content


def remove_irrigation_workspace() -> dict:
	"""Take the page off before the app goes.

	`onboard_worker.remove_onboard_worker`'s argument, unchanged: a Workspace this
	app built is not the operator's record, and left behind it is a page of links
	into a module that has gone. A page somebody has MOVED to another module is
	theirs and stays — that is the one thing that cannot be faked. Never raises.
	"""
	report = {"removed": False, "name": WORKSPACE_NAME, "reason": ""}
	try:
		if not compat.doctype_exists(WORKSPACE):  # pragma: no cover - not a real Frappe
			report["reason"] = "this site has no Workspace doctype"
			return report
		if not frappe.db.exists(WORKSPACE, WORKSPACE_NAME):
			report["reason"] = "not present"
			return report
		if compat.has_field(WORKSPACE, "module"):
			module = frappe.db.get_value(WORKSPACE, WORKSPACE_NAME, "module")
			if module and str(module) != MODULE:
				report["reason"] = (
					f"left alone — this page has been moved to the {module!r} module, so it is "
					"not this app's to delete"
				)
				return report
		frappe.delete_doc(WORKSPACE, WORKSPACE_NAME, ignore_permissions=True, force=True)
		report["removed"] = True
	except Exception as exc:  # pragma: no cover - a site mid-uninstall
		report["reason"] = f"{type(exc).__name__}: {exc}"
	return report
