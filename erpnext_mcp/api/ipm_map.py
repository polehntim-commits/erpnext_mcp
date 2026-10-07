# SPDX-License-Identifier: MIT
"""/app/ipm-map — the IPM relationship graph in the Desk. v0.263.0 (Tim, 2026-10-06).

ONE SOURCE OF TRUTH. Every answer here comes from `ipm_graph.graph()` / `ipm_graph.organism()` — the
functions the phone's `get_ipm_graph` and the MCP `get_ipm_graph` call — and every write goes through
`ipm_graph.save_relationship()` behind the same gate (`guard.may_edit_ipm`: Farm Manager, Compliance
Officer, System Manager). The Desk adds only what a bench needs on top: filters over the same answer,
recent observations in the side panel, and which products would harm a beneficial active now.

Reading needs read permission on IPM Organism (a Desk login with any farm role). Nothing here is a
second set of rules: a literature edge is never edited (the farm's copy is written beside it), and the
ends of a relation are checked by the doctype controller, exactly as from the phone.
"""

from __future__ import annotations

import datetime
import json

import frappe

from .. import compat, ipm_graph
from ..errors import ToolError
from . import guard
from .gis import speaks_frappe

KIND_GROUPS = {
	"pest": ("Insect Pest", "Mite Pest"),
	"disease": ("Disease",),
	"weed": ("Weed",),
	"vertebrate": ("Vertebrate Pest",),
	"beneficial": ("Beneficial Insect", "Beneficial Mite", "Beneficial Microbe", "Beneficial Vertebrate"),
	"pollinator": ("Pollinator",),
	"product": ("Product",),
	"crop": ("Crop", "Variety"),
}
HARM_FLOOR = 0.3


def _require_reader() -> str:
	user = frappe.session.user
	if not frappe.has_permission(ipm_graph.ORGANISM, "read", user=user):
		raise ToolError("The IPM Map needs read access to IPM Organism (any farm role in the Desk).")
	if not ipm_graph.installed():
		raise ToolError("The IPM graph is not on this site yet (needs v0.262.0's migrate).")
	return user


def _list(value) -> list[str]:
	if value in (None, ""):
		return []
	if isinstance(value, str):
		value = json.loads(value) if value.strip().startswith("[") else value.split(",")
	return [str(v).strip() for v in value if str(v).strip()]


def stage_on(block: str, on: str) -> int | None:
	"""The block's latest observed BBCH on or before a date — what "a date" means as a stage filter."""
	from .. import bbch, growth_stage

	day = datetime.date.fromisoformat(str(on)[:10])
	best = None
	for row in growth_stage.stages(block):
		seen = row.get("_day")
		code = bbch.parse(row.get("growth_stage_code"))
		if seen and code is not None and seen <= day:
			best = code
	return best


def _view(crop=None, block=None, stage=None, date=None, depth=None, kinds=None, relations=None, provenance=None,
          include_disabled=None) -> dict:
	user = _require_reader()
	if date and block and stage in (None, ""):
		stage = stage_on(block, date)
	pages, start = [], 0
	while start is not None:
		try:
			page = ipm_graph.graph(crop=crop or "", stage=stage, block=block or "", depth=int(depth or 2), start=start,
			                       limit=ipm_graph.MAX_LIMIT, include_disabled=bool(compat.checked(include_disabled)))
		except ValueError as exc:
			raise ToolError(str(exc)) from None
		pages.append(page)
		start = page["next_start"]
	answer = pages[0]
	nodes = {n["id"]: n for p in pages for n in p["nodes"]}
	edges = [e for p in pages for e in p["edges"]]

	groups = _list(kinds)
	wanted_kinds = {k for g in groups for k in KIND_GROUPS.get(g, (g,))}
	wanted_relations = set(_list(relations))
	wanted_provenance = set(_list(provenance))
	focus = answer["crop"]["id"]
	edges = [e for e in edges
	         if (not wanted_relations or e["relation"] in wanted_relations)
	         and (not wanted_provenance or e["provenance"] in wanted_provenance)
	         and (not wanted_kinds or all(nodes[x]["kind"] in wanted_kinds or x == focus for x in (e["subject"], e["object"])))]
	keep = {focus} | {e["subject"] for e in edges} | {e["object"] for e in edges}

	# Products that would harm a beneficial or pollinator active at this stage: flagged on the node.
	active = {n for n, row in nodes.items() if row["kind"] in ipm_graph.BENEFICIAL_KINDS and row["active_now"] is not False}
	harms: dict[str, list] = {}
	for edge in edges:
		if edge["relation"] == "harmed_by" and edge["subject"] in active and (edge["weight"] or 0) >= HARM_FLOOR:
			harms.setdefault(edge["object"], []).append(edge["subject"])
	out_nodes = []
	for node_id in sorted(keep):
		node = dict(nodes[node_id])
		node["harms_active"] = sorted(harms.get(node_id, []))
		node["protected"] = node.get("protected_status") in ipm_graph.MBTA
		out_nodes.append(node)
	return {
		**{k: answer[k] for k in ("graph_version", "crop", "block", "stage", "label_caveat")},
		"nodes": out_nodes,
		"edges": edges,
		"total_edges": len(edges),
		"can_edit": guard.may_edit_ipm(user),
	}


def _panel(organism=None, block=None) -> dict:
	_require_reader()
	if not organism or not frappe.db.exists(ipm_graph.ORGANISM, organism):
		raise ToolError(f"No IPM Organism {organism!r}.")
	data = ipm_graph.organism(organism)
	name = data["node"]["name"]
	filters = {"threat": name}
	if block:
		filters["block"] = block
	data["observations"] = [dict(r) for r in frappe.get_list(
		"Crop Observation", filters=filters,
		fields=compat.existing_fields("Crop Observation", ("name", "block", "observed_on", "count_observed",
		                                                   "sample_unit", "threshold_exceeded", "observer")),
		order_by="observed_on desc", limit_page_length=20)] if compat.doctype_exists("Crop Observation") else []
	data["can_edit"] = guard.may_edit_ipm(frappe.session.user)
	return data


def _save(relationship=None, subject=None, relation=None, object=None, weight=None, confidence=None, notes=None,
          crop=None, bbch_from=None, bbch_to=None, enabled=None) -> dict:
	user = _require_reader()
	if not guard.may_edit_ipm(user):
		raise ToolError(f"Editing the IPM graph is restricted to {', '.join(sorted(guard.IPM_EDIT_ROLES))}.")
	values = {k: v for k, v in {"subject": subject, "relation": relation, "object": object, "weight": weight,
	                            "confidence": confidence, "notes": notes, "crop": crop, "bbch_from": bbch_from,
	                            "bbch_to": bbch_to, "enabled": enabled}.items() if v not in (None, "")}
	if not relationship and not all(values.get(k) for k in ("subject", "relation", "object")):
		raise ToolError("Subject, relation and object are required to add a relationship.")
	try:
		edge, created = ipm_graph.save_relationship(values, user, relationship=str(relationship or ""))
	except (frappe.ValidationError, frappe.DoesNotExistError) as exc:
		raise ToolError(str(exc)) from None
	return {"edge": edge, "created": created}


def _choices() -> dict:
	_require_reader()
	crops = frappe.get_all(ipm_graph.ORGANISM, filters={"kind": ("in", ["Crop", "Variety"]), "enabled": 1},
	                       fields=["name", "organism_name", "kind"], order_by="organism_name asc")
	blocks = frappe.get_list("Field", fields=["name"], order_by="name asc", limit_page_length=0)
	return {
		"crops": [dict(r) for r in crops],
		"blocks": [r["name"] for r in blocks],
		"kinds": list(KIND_GROUPS),
		"relations": list(ipm_graph.RELATIONS),
		"provenance": [ipm_graph.LITERATURE, ipm_graph.FARM, ipm_graph.USER, ipm_graph.AI, ipm_graph.IMPORTED],
		"can_edit": guard.may_edit_ipm(frappe.session.user),
	}


@frappe.whitelist()
def view(crop=None, block=None, stage=None, date=None, depth=None, kinds=None, relations=None, provenance=None,
         include_disabled=None):
	"""The graph the page draws: ipm_graph.graph(), every page, filtered."""
	return speaks_frappe(_view, crop=crop, block=block, stage=stage, date=date, depth=depth, kinds=kinds,
	                     relations=relations, provenance=provenance, include_disabled=include_disabled)


@frappe.whitelist()
def panel(organism=None, block=None):
	"""The side panel: the node, its relationships, thresholds and recent observations."""
	return speaks_frappe(_panel, organism=organism, block=block)


@frappe.whitelist(methods=["POST"])
def save(relationship=None, subject=None, relation=None, object=None, weight=None, confidence=None, notes=None,
         crop=None, bbch_from=None, bbch_to=None, enabled=None):
	"""Add, edit or disable one relationship — the phone's save, behind the same gate."""
	return speaks_frappe(_save, relationship=relationship, subject=subject, relation=relation, object=object,
	                     weight=weight, confidence=confidence, notes=notes, crop=crop, bbch_from=bbch_from,
	                     bbch_to=bbch_to, enabled=enabled)


@frappe.whitelist()
def choices():
	"""What the filters offer."""
	return speaks_frappe(_choices)
