# SPDX-License-Identifier: MIT
"""The IPM relationship graph: the MCP tools. v0.262.0. See `erpnext_mcp/ipm_graph.py` and
`docs/contracts/ipm_graph_v0_262.yaml`.

Reads are on; every write is off until an operator switches it on. Every list pages with start / limit
and nothing caps how big the graph may grow. Nothing is deleted: a relationship is disabled or
Rejected, and kept.
"""

from __future__ import annotations

import frappe

from .. import ipm_graph, proposals
from ..args import as_bool, as_int, as_str
from ..errors import ToolError
from ..result import ToolResult


def _installed() -> None:
	if not ipm_graph.installed():
		raise ToolError("this site has not migrated to v0.262.0 (no IPM Organism / IPM Relationship).")


def _page(args: dict) -> tuple[int, int]:
	start = max(0, as_int(args, "start") or 0)
	limit = max(1, min(as_int(args, "limit") or ipm_graph.DEFAULT_LIMIT, ipm_graph.MAX_LIMIT))
	return start, limit


def _wrap(fn):
	try:
		return fn()
	except (ValueError, frappe.ValidationError, frappe.DoesNotExistError) as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None


# ── reads ───────────────────────────────────────────────────────────────────
def list_ipm_organisms(args: dict) -> ToolResult:
	_installed()
	start, limit = _page(args)
	filters = {}
	for key in ("kind", "provenance", "status", "protected_status"):
		if as_str(args, key):
			filters[key] = as_str(args, key)
	if not as_bool(args, "include_disabled", False):
		filters["enabled"] = 1
	rows = ipm_graph._rows(ipm_graph.ORGANISM, ipm_graph._NODE_FIELDS, filters)
	search = as_str(args, "search").lower()
	if search:
		rows = [r for r in rows if search in " ".join(str(r.get(k) or "") for k in ("organism_name", "scientific_name", "aliases")).lower()]
	page = rows[start:start + limit]
	return ToolResult(
		data={"organisms": [{**ipm_graph.describe_node(r), "status": r.get("status")} for r in page],
		      "total": len(rows), "next_start": start + limit if start + limit < len(rows) else None},
		summary=f"{len(page)} of {len(rows)} IPM organism(s)",
	)


def get_ipm_organism(args: dict) -> ToolResult:
	_installed()
	name = _wrap(lambda: ipm_graph._node_ref(as_str(args, "organism", required=True)))
	data = ipm_graph.organism(name, as_str(args, "company"))
	return ToolResult(data=data, summary=f"{data['node']['name']}: {len(data['edges'])} relationship(s)")


def list_ipm_relationships(args: dict) -> ToolResult:
	_installed()
	start, limit = _page(args)
	filters = {}
	for key in ("relation", "provenance", "status"):
		if as_str(args, key):
			filters[key] = as_str(args, key)
	for key in ("subject", "object", "crop"):
		if as_str(args, key):
			filters[key] = _wrap(lambda key=key: ipm_graph._node_ref(as_str(args, key)))
	if as_str(args, "organism"):
		node = _wrap(lambda: ipm_graph._node_ref(as_str(args, "organism")))
		rows = [r for side in ("subject", "object") for r in ipm_graph._rows(ipm_graph.RELATIONSHIP, ipm_graph._EDGE_FIELDS, {**filters, side: node})]
	else:
		rows = ipm_graph._rows(ipm_graph.RELATIONSHIP, ipm_graph._EDGE_FIELDS, filters)
	if not as_bool(args, "include_disabled", False):
		rows = [r for r in rows if r.get("enabled")]
	page = rows[start:start + limit]
	return ToolResult(
		data={"relationships": [ipm_graph.describe_edge(r) for r in page], "total": len(rows),
		      "next_start": start + limit if start + limit < len(rows) else None},
		summary=f"{len(page)} of {len(rows)} IPM relationship(s)",
	)


def get_ipm_graph(args: dict) -> ToolResult:
	_installed()
	start, limit = _page(args)
	data = _wrap(lambda: ipm_graph.graph(
		crop=as_str(args, "crop"), stage=args.get("stage"), block=as_str(args, "block"),
		depth=as_int(args, "depth") or 2, kinds=args.get("kinds"), start=start, limit=limit,
		company=as_str(args, "company"), include_disabled=as_bool(args, "include_disabled", False)))
	return ToolResult(
		data=data,
		summary=f"{data['crop']['name']}: {len(data['nodes'])} node(s), {len(data['edges'])} of {data['total_edges']} edge(s)"
		        + (f" at BBCH {data['stage']['bbch']}" if data["stage"]["bbch"] is not None else ""),
	)


def export_ipm_graph(args: dict) -> ToolResult:
	_installed()
	start, limit = _page(args)
	fmt = (as_str(args, "format") or "json").lower()
	if fmt not in ("json", "csv"):
		raise ToolError("format is json or csv.")
	data = ipm_graph.export_graph(fmt, as_bool(args, "include_literature", True), start, limit)
	return ToolResult(data=data, summary=f"{len(data['nodes'])} node(s), {len(data['edges'])} of {data['total_edges']} edge(s)")


# ── writes (each default OFF) ───────────────────────────────────────────────
def create_ipm_organism(args: dict) -> ToolResult:
	_installed()
	node, _ = _wrap(lambda: ipm_graph.save_organism(dict(args), frappe.session.user))
	return ToolResult(data=node, summary=f"created {node['name']} ({node['kind']})", docstatus_delta="none → 0 (created)")


def update_ipm_organism(args: dict) -> ToolResult:
	_installed()
	values = {k: v for k, v in args.items() if k != "organism"}
	node, _ = _wrap(lambda: ipm_graph.save_organism(values, frappe.session.user, name=as_str(args, "organism", required=True)))
	return ToolResult(data=node, summary=f"updated {node['name']}", docstatus_delta="0 → 0 (updated)")


def create_ipm_relationship(args: dict) -> ToolResult:
	_installed()
	for key in ("subject", "relation", "object"):
		as_str(args, key, required=True)
	edge, created = _wrap(lambda: ipm_graph.save_relationship(dict(args), frappe.session.user))
	return ToolResult(data={**edge, "created": created},
	                  summary=f"{'created' if created else 'updated'} {edge['subject']} {edge['relation']} {edge['object']}",
	                  docstatus_delta="none → 0 (created)" if created else "0 → 0 (updated)")


def update_ipm_relationship(args: dict) -> ToolResult:
	"""A Literature edge is never edited: the farm's own copy is written beside it, and stands in for it."""
	_installed()
	name = as_str(args, "relationship", required=True)
	values = {k: v for k, v in args.items() if k != "relationship"}
	edge, created = _wrap(lambda: ipm_graph.save_relationship(values, frappe.session.user, relationship=name))
	return ToolResult(data={**edge, "created": created, "copied_from_literature": created and edge["id"] != name},
	                  summary=f"{edge['subject']} {edge['relation']} {edge['object']}"
	                          + (" (the farm's copy of a literature edge)" if created else " updated"),
	                  docstatus_delta="0 → 0 (updated)")


def import_ipm_graph(args: dict) -> ToolResult:
	_installed()
	fmt = (as_str(args, "format") or "json").lower()
	if fmt not in ("json", "csv"):
		raise ToolError("format is json or csv.")
	data = args.get("data")
	if data in (None, ""):
		raise ToolError("data is required: JSON {nodes, edges}, or CSV with a record column (node / edge).")
	status = as_str(args, "status") or ipm_graph.PROPOSED
	if status not in (ipm_graph.PROPOSED, ipm_graph.ACTIVE):
		raise ToolError("status is Proposed (default: lands off for approve_ipm_proposal) or Active.")
	dry_run = as_bool(args, "dry_run", True)
	out = _wrap(lambda: ipm_graph.import_graph(data, fmt, dry_run=dry_run, status=status, user=frappe.session.user))
	c = out["counts"]
	return ToolResult(data=out, summary=("DRY RUN — nothing written: " if dry_run else "")
	                  + f"{c['add']} to add, {c['change']} to change, {c['skip']} skipped, {c['error']} error(s)",
	                  docstatus_delta="none" if dry_run else "none → 0 (created)")


def propose_ipm_relationships(args: dict) -> ToolResult:
	"""An agent's reading of a handbook page or regulation, landed Proposed and OFF. Same rails as
	propose_compliance_rule: it must cite what it read, it cannot fill in an approval, and the provenance
	(AI Proposed) is stamped here, not chosen by the proposer."""
	_installed()
	offered = proposals.offered_approval_fields(args) + [k for k in ("approved_by", "approved_on") if args.get(k)]
	if offered:
		raise ToolError(f"a proposal cannot fill in {', '.join(offered)} — approve_ipm_proposal records whoever "
		                "is authenticated on that call. Nothing was written.")
	try:
		cite = proposals.citation(as_str(args, "source_url"), as_str(args, "source_section"),
		                          as_str(args, "source_citation"), as_str(args, "source_text"), as_str(args, "read_on"))
	except ValueError as exc:
		raise ToolError(f"{exc}. Nothing was written.") from None
	items = {k: list(args.get(k) or []) for k in ("nodes", "edges", "thresholds")}
	if not any(items.values()):
		raise ToolError("nothing to propose: pass nodes, edges and/or thresholds. Nothing was written.")
	made = ipm_graph.propose(items, cite, as_str(args, "authored_by") or "AI-proposed", frappe.session.user)
	total = sum(len(made[k]) for k in ("nodes", "edges", "thresholds"))
	return ToolResult(
		data={**made, "citation": cite,
		      "next": "A person reviews each with get_ipm_organism / list_ipm_relationships status Proposed, then "
		              "approve_ipm_proposal (approve or reject)."},
		summary=f"{total} proposal(s) landed OFF for review" + (f"; {len(made['errors'])} refused" if made["errors"] else ""),
		docstatus_delta="none → 0 (created, disabled)",
	)


def approve_ipm_proposal(args: dict) -> ToolResult:
	_installed()
	names = list(args.get("names") or [])
	if not names:
		raise ToolError("names is required: the proposals (organisms, relationships, thresholds) to decide.")
	decision = as_str(args, "decision") or "approve"
	out = _wrap(lambda: ipm_graph.decide(names, decision, frappe.session.user, as_str(args, "note")))
	return ToolResult(data=out, summary=f"{decision}: {len(out['done'])} done, {len(out['missing'])} not found",
	                  docstatus_delta="0 → 0 (updated)")


def approve_pest_action_threshold(args: dict) -> ToolResult:
	"""A person approves (enables) or rejects a Proposed threshold — the seeded cherry starters included."""
	_installed()
	names = list(args.get("names") or ([] if not as_str(args, "threshold") else [as_str(args, "threshold")]))
	if not names:
		raise ToolError("threshold (or names) is required.")
	missing = [n for n in names if not frappe.db.exists(ipm_graph.THRESHOLD, n)]
	if missing:
		raise ToolError(f"no Pest Action Threshold {', '.join(missing)}. Nothing was changed.")
	decision = as_str(args, "decision") or "approve"
	out = _wrap(lambda: ipm_graph.decide(names, decision, frappe.session.user, as_str(args, "note")))
	return ToolResult(data=out, summary=f"{decision}: {len(out['done'])} threshold(s)", docstatus_delta="0 → 0 (updated)")
