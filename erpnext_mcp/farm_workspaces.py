# SPDX-License-Identifier: MIT
"""The farm's own Desk sidebar: nine workspaces grouped the way a farm thinks.

Farm Operations, Crew & Labor, Compliance, Crop Protection, Assets & Equipment,
Land & Parcels, Financial, Market & Sales and Map & Terrain. Each is a landing
page of shortcut cards, number cards and grouped link cards over registers that
already exist. Without them, reaching a register means knowing its DocType name
and typing it into the awesomebar.

THE PAGES ARE DESCRIBED IN JSON AND BUILT IN CODE. The descriptions live in
`workspace_specs/*.json`, one file per page, so changing what a page shows is a
data edit. They are deliberately NOT Frappe fixtures and NOT `workspace/*.json`
files in the module folder. Both of those are imported by every `bench migrate`
and would overwrite an operator's own arrangement of the page on every upgrade.
`irrigation_workspace` and `dashboard._build_dispatch_workspace` make the same
choice for the same reason, and this module uses their guard,
`_workspace_is_empty`: a page with anything on it belongs to whoever arranged it
and is never touched.

A SPEC NAMES WHAT A FARM CALLS A THING, AND THE ROW NAMES THE DOCTYPE. Several
words in a farmer's vocabulary are not DocTypes here, and the specs say so
rather than inventing registers:

  * a Block and an Irrigation Valve are `Asset Register` rows, reached through a
    shortcut whose `stats_filter` narrows the register (see `irrigation_workspace`
    for why one field does both the badge and the click-through);
  * an Owner Draw is an `Expense Receipt` in the Owner Draw category;
  * a crew is the people on a `Farm Shift`, and there is no Crew record at all;
  * a Leave Request is HRMS's `Leave Application`.

EVERY LINK IS CHECKED AGAINST THE SITE. A DocType, Page or Dashboard that this
site does not have is dropped from the page and named in the report, so a site
without HRMS gets a Crew & Labor page with no leave card rather than a dead link.
Number cards are created through `dashboard._build`, which leaves an existing
card exactly as an operator left it. A spec can reuse a card defined elsewhere
(the Command Center's, the dispatch board's, or another spec's) by naming it
with `"shared": true`, so one count is never defined twice.

Never raises: this runs inside `bench migrate`.
"""

from __future__ import annotations

import json
import os

import frappe

from . import compat
from .dashboard import (
	CARD,
	CARDS,
	DASHBOARD,
	DISPATCH_NUMBER_CARDS,
	MODULE,
	WORKSPACE,
	_build,
	_select_value,
	_slug,
	_workspace_is_empty,
	card_filters,
)

SPEC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace_specs")

PAGE = "Page"


def load_specs() -> list:
	"""Every workspace spec, in sidebar order. A file that cannot be read is skipped.

	The caller learns about an unreadable file from `spec_errors`, which reads the
	same directory. Loading and reporting are separate so a test can hold the
	shipped specs to a stricter standard than a migrate does.
	"""
	specs = []
	for filename in sorted(os.listdir(SPEC_DIR)):
		if not filename.endswith(".json"):
			continue
		try:
			with open(os.path.join(SPEC_DIR, filename), encoding="utf-8") as handle:
				spec = json.load(handle)
		except Exception:
			continue
		if isinstance(spec, dict) and spec.get("name"):
			specs.append(spec)
	specs.sort(key=lambda spec: float(spec.get("sequence_id") or 0))
	return specs


def spec_errors() -> list:
	"""The spec files that could not be loaded, as `{"name", "reason"}` rows."""
	errors = []
	for filename in sorted(os.listdir(SPEC_DIR)):
		if not filename.endswith(".json"):
			continue
		try:
			with open(os.path.join(SPEC_DIR, filename), encoding="utf-8") as handle:
				spec = json.load(handle)
			if not isinstance(spec, dict) or not spec.get("name"):
				raise ValueError("the file is not an object with a name")
		except Exception as exc:
			errors.append({"name": f"workspace spec {filename}", "reason": f"{type(exc).__name__}: {exc}"})
	return errors


def workspace_names() -> list:
	return [spec["name"] for spec in load_specs()]


def _card_specs(specs: list) -> dict:
	"""Every number card a spec may name, by label.

	The Command Center's and the dispatch board's cards come first, so a spec
	that names one of them gets exactly that card rather than a second definition.
	"""
	known = {}
	for spec in (*CARDS, *DISPATCH_NUMBER_CARDS):
		known[spec["label"]] = spec
	for workspace in specs:
		for card in workspace.get("number_cards") or []:
			if card.get("shared") or card["label"] in known:
				continue
			known[card["label"]] = {
				"label": card["label"],
				"document_type": card["document_type"],
				"function": card.get("function") or "Count",
				"filters_json": card_filters(card["document_type"], card.get("filters") or {}),
				"color": card.get("color") or "#449cf0",
			}
	return known


def _target_exists(link_type: str, link_to: str) -> bool:
	if link_type == "DocType":
		return compat.doctype_exists(link_to)
	if link_type in (PAGE, DASHBOARD):
		try:
			return compat.doctype_exists(link_type) and bool(frappe.db.exists(link_type, link_to))
		except Exception:
			return False
	return False


def install_farm_workspaces() -> dict:
	"""Build or repair every workspace in `workspace_specs`.

	Never raises. Returns `{"workspaces": [...], "failed": [...], "note": str}`,
	where each workspace row is `{"name", "created", "filled", "existed",
	"shortcuts", "number_cards", "links", "dropped"}`.
	"""
	report = {"workspaces": [], "failed": spec_errors(), "note": ""}
	if not compat.doctype_exists(WORKSPACE):
		report["note"] = "this site has no Workspace doctype, so there are no landing pages to build"
		return report

	specs = load_specs()
	known_cards = _card_specs(specs)
	card_report = {"created_cards": [], "existing_cards": [], "failed": []}
	if compat.doctype_exists(CARD):
		wanted = {card["label"] for spec in specs for card in spec.get("number_cards") or []}
		for label in sorted(wanted):
			spec = known_cards.get(label)
			if spec is None:
				report["failed"].append(
					{"name": f"number card {label!r}", "reason": "no spec defines it, so it cannot be built"}
				)
				continue
			# A card over a doctype this site lacks (Leave Application without
			# HRMS) is not a failure. The page simply does not show it.
			if compat.doctype_exists(spec["document_type"]):
				_build(CARD, "label", spec, card_report, "cards")
		report["failed"].extend(card_report["failed"])

	for spec in specs:
		report["workspaces"].append(_install_one(spec, report))
	return report


def _install_one(spec: dict, report: dict) -> dict:
	name = spec["name"]
	row = {
		"name": name,
		"created": False,
		"filled": False,
		"existed": False,
		"shortcuts": 0,
		"number_cards": 0,
		"links": 0,
		"dropped": [],
	}
	try:
		existing = frappe.db.exists(WORKSPACE, name)
		if existing and not _workspace_is_empty(name):
			# Somebody arranged this page. Leave it exactly as they left it.
			row["existed"] = True
			return row

		doc = frappe.get_doc(WORKSPACE, name) if existing else frappe.new_doc(WORKSPACE)
		if existing:
			# Repairing a page left blank. Clear the child tables first so a
			# partial set cannot be doubled.
			for fieldname in ("shortcuts", "links", "number_cards", "charts"):
				if compat.has_field(WORKSPACE, fieldname):
					doc.set(fieldname, [])
		else:
			doc.name = name
			doc.flags.name_set = True

		for fieldname, value in (
			("title", name),
			("label", name),
			("module", MODULE),
			("icon", spec.get("icon") or ""),
			("public", 1),
			("is_hidden", 0),
			("sequence_id", float(spec.get("sequence_id") or 0)),
		):
			if compat.has_field(WORKSPACE, fieldname):
				doc.set(fieldname, value)

		content = _build_content(doc, spec, row)
		if compat.has_field(WORKSPACE, "content"):
			doc.content = json.dumps(content)

		doc.save(ignore_permissions=True) if existing else doc.insert(ignore_permissions=True)
		row["filled" if existing else "created"] = True
	except Exception as exc:
		report["failed"].append({"name": f"{name} workspace", "reason": f"{type(exc).__name__}: {exc}"})
	return row


def _build_content(doc, spec: dict, row: dict) -> list:
	"""Build the page, appending each child row with the block that renders it.

	Written in one pass for `dashboard._workspace_content`'s reason: a modern
	Frappe renders only what `content` names, so a row without a block is
	invisible and a block without a row is a rendering error.
	"""
	content = []
	used_ids = set()

	def add(kind: str, data: dict, key: str) -> None:
		base = _slug(f"{kind}-{key}")[:32]
		block_id, n = base, 1
		while block_id in used_ids:
			n += 1
			block_id = f"{base[:29]}-{n}"
		used_ids.add(block_id)
		content.append({"id": block_id, "type": kind, "data": data})

	def header(text: str) -> None:
		add("header", {"text": f'<span class="h4"><b>{text}</b></span>', "col": 12}, text)

	if spec.get("summary"):
		add("paragraph", {"text": spec["summary"], "col": 12}, "summary")

	if compat.has_field(WORKSPACE, "shortcuts"):
		shortcuts = []
		for item in spec.get("shortcuts") or []:
			kind = item.get("type") or "DocType"
			if not _target_exists(kind, item["link_to"]):
				row["dropped"].append(item["label"])
				continue
			shortcuts.append((item, kind))
		if shortcuts:
			header("Shortcuts")
		for item, kind in shortcuts:
			child = {"label": item["label"], "link_to": item["link_to"]}
			site_kind = _select_value("Workspace Shortcut", "type", kind)
			if site_kind:
				child["type"] = site_kind
			if kind == "DocType" and compat.has_field("Workspace Shortcut", "doc_view"):
				view = _select_value("Workspace Shortcut", "doc_view", item.get("doc_view") or "List")
				if view:
					child["doc_view"] = view
			if item.get("stats_filter") and compat.has_field("Workspace Shortcut", "stats_filter"):
				child["stats_filter"] = json.dumps(item["stats_filter"], indent=1)
			for fieldname in ("color", "format"):
				if item.get(fieldname) and compat.has_field("Workspace Shortcut", fieldname):
					child[fieldname] = item[fieldname]
			doc.append("shortcuts", child)
			add("shortcut", {"shortcut_name": item["label"], "col": 3}, item["label"])
			row["shortcuts"] += 1

	if compat.has_field(WORKSPACE, "number_cards") and compat.doctype_exists(CARD):
		present = []
		for card in spec.get("number_cards") or []:
			if frappe.db.exists(CARD, card["label"]):
				present.append(card["label"])
			else:
				row["dropped"].append(card["label"])
		if present:
			header("At a glance")
		for label in present:
			doc.append("number_cards", {"number_card_name": label, "label": label})
			add("number_card", {"number_card_name": label, "col": 4}, label)
			row["number_cards"] += 1

	if compat.has_field(WORKSPACE, "links"):
		cards = []
		for card in spec.get("link_cards") or []:
			links = []
			for link in card.get("links") or []:
				link_type = link.get("link_type") or "DocType"
				if _target_exists(link_type, link["link_to"]):
					links.append((link, link_type))
				else:
					row["dropped"].append(link["label"])
			if links:
				cards.append((card["label"], links))
		if cards:
			header("Registers")
		for card_label, links in cards:
			break_row = {"label": card_label, "link_count": len(links)}
			kind = _select_value("Workspace Link", "type", "Card Break")
			if kind:
				break_row["type"] = kind
			doc.append("links", break_row)
			for link, link_type in links:
				link_row = {"label": link["label"], "link_to": link["link_to"]}
				kind = _select_value("Workspace Link", "type", "Link")
				if kind:
					link_row["type"] = kind
				site_link_type = _select_value("Workspace Link", "link_type", link_type)
				if site_link_type:
					link_row["link_type"] = site_link_type
				doc.append("links", link_row)
				row["links"] += 1
			add("card", {"card_name": card_label, "col": 4}, card_label)

	return content


def remove_farm_workspaces() -> list:
	"""Take the pages off before the app goes. One report row per workspace.

	`irrigation_workspace.remove_irrigation_workspace`'s rule: a page this app
	built goes with it, and a page somebody has moved to another module is theirs
	and stays. The number cards are left, like the Command Center's: they are
	counts an operator may have put on their own pages. Never raises.
	"""
	out = []
	for name in workspace_names():
		row = {"removed": False, "name": name, "reason": ""}
		try:
			if not compat.doctype_exists(WORKSPACE):
				row["reason"] = "this site has no Workspace doctype"
			elif not frappe.db.exists(WORKSPACE, name):
				row["reason"] = "not present"
			else:
				module = (
					frappe.db.get_value(WORKSPACE, name, "module")
					if compat.has_field(WORKSPACE, "module")
					else None
				)
				if module and str(module) != MODULE:
					row["reason"] = (
						f"left alone. This page has been moved to the {module!r} module, so it is "
						"not this app's to delete"
					)
				else:
					frappe.delete_doc(WORKSPACE, name, ignore_permissions=True, force=True)
					row["removed"] = True
		except Exception as exc:  # pragma: no cover - a site mid-uninstall
			row["reason"] = f"{type(exc).__name__}: {exc}"
		out.append(row)
	return out
