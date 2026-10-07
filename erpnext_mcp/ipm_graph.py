# SPDX-License-Identifier: MIT
"""The IPM relationship graph ("mind map"). v0.262.0 (Tim, 2026-10-06). Contract:
`docs/contracts/ipm_graph_v0_262.yaml`.

NODES ARE IPM ORGANISMS, EDGES ARE IPM RELATIONSHIPS, AND BOTH ARE THE FARM'S DATA. The literature
in `ipm_reference` (pests, beneficials, damage windows, efficacy, toxicity) is the seed: `seed()` turns
it into rows with provenance Literature, keyed by `seed_key`. A re-seed refreshes only rows that are
still Literature; a row the farm entered, observed, imported or had an AI propose is never touched.
Nothing here caps growth: every read is paginated.

WHAT THE GRAPH IS FOR is one question on the phone: for this crop at this stage (or this block), which
pests are active, which beneficials are working now, and which products would harm them — so the
lowest-impact option comes first (`threshold_status`). MBTA-protected species lead with exclusion,
netting, scare devices and habitat, and lethal options are not offered. A rodenticide is tagged
`ccf_gate: rodent_bait`: it goes through the rodent-bait task rules, never around them. The label is
the law; this is decision support (`ipm_reference.LABEL_CAVEAT`).
"""

from __future__ import annotations

import hashlib
import json

import frappe

from . import compat, ipm_reference, ipm_seed_data
from .erpnext_mcp.doctype.ipm_organism.ipm_organism import slug

ORGANISM = "IPM Organism"
RELATIONSHIP = "IPM Relationship"
THRESHOLD = "Pest Action Threshold"
FIELD = "Field"

LITERATURE, FARM, USER, AI, IMPORTED = "Literature", "Farm Observed", "User Entered", "AI Proposed", "Imported"
PROPOSED, ACTIVE, REJECTED = "Proposed", "Active", "Rejected"
RELATIONS = ("attacks", "preys_on", "parasitizes", "controls", "harmed_by", "hosts", "competes_with", "pollinates")
BENEFICIAL_KINDS = ("Beneficial Insect", "Beneficial Mite", "Beneficial Microbe", "Beneficial Vertebrate", "Pollinator")
PEST_KINDS = ("Insect Pest", "Mite Pest", "Disease", "Weed", "Vertebrate Pest")
MBTA = ("MBTA", "MBTA Depredation Order")
DEFAULT_LIMIT, MAX_LIMIT = 500, 2000
LABEL_CAVEAT = getattr(ipm_reference, "LABEL_CAVEAT", "The label is the law.")

#: Products whose active ingredient makes them a rodenticide: options using them carry the gate.
RODENTICIDES = ("zinc phosphide", "chlorophacinone", "diphacinone", "bromadiolone", "brodifacoum", "difethialone",
                "strychnine", "cholecalciferol", "bromethalin")

_PEST_KIND = {"Insect": "Insect Pest", "Mite": "Mite Pest", "Rodent": "Vertebrate Pest", "Bird": "Vertebrate Pest",
              "Mammal": "Vertebrate Pest", "Fungus": "Disease", "Bacteria": "Disease", "Virus": "Disease",
              "Weed": "Weed"}
_BENEFICIAL_KIND = {"Insect": "Beneficial Insect", "Mite": "Beneficial Mite", "Bacteria": "Beneficial Microbe",
                    "Fungus": "Beneficial Microbe", "Virus": "Beneficial Microbe", "Mammal": "Beneficial Vertebrate",
                    "Bird": "Beneficial Vertebrate"}
#: The crop names the reference tables use, as graph nodes. "Cherries" is the reference's spelling.
CROP_NAMES = {"Cherries": "Sweet Cherry"}


def installed() -> bool:
	return compat.doctype_exists(ORGANISM) and compat.doctype_exists(RELATIONSHIP)


def node_id(name: str) -> str:
	return slug(CROP_NAMES.get(name, name))


# ── the seed ────────────────────────────────────────────────────────────────
def _literature_nodes() -> list[dict]:
	out: dict[str, dict] = {}

	def add(row: dict) -> None:
		row.setdefault("provenance", LITERATURE)
		row.setdefault("status", ACTIVE)
		row.setdefault("enabled", 1)
		key = node_id(row["organism_name"])
		row["seed_key"] = f"node:{key}"
		out.setdefault(key, row)

	crops = {"Cherries"} | {r["crop"] for r in ipm_reference.PEST_DAMAGE} | {r["crop"] for r in ipm_reference.BENEFICIAL_ACTIVITY}
	for crop in sorted(crops):
		add({"organism_name": CROP_NAMES.get(crop, crop), "kind": "Crop", "aliases": crop})
	for pest in ipm_reference.PEST_MODELS:
		logic = pest.get("emergence_logic") or {}
		row = {"organism_name": pest["name"], "kind": _PEST_KIND.get(pest.get("kingdom"), "Insect Pest"),
		       "scientific_name": pest.get("scientific_name"), "description": pest.get("description"),
		       "source": "; ".join(ipm_reference.SOURCES[:2])}
		# NO DEGREE-DAY NUMBERS ARE COPIED. The reference states its bases in °C and its DD totals in what
		# match the published °F models, so any one conversion would be wrong for some model. The pest
		# DD models (v0.264.0) carry their own parameter table with explicit units and citations.
		del logic
		add(row)
	for ben in ipm_reference.BENEFICIALS:
		kind = "Pollinator" if ben.get("beneficial_type") == "pollinator" else _BENEFICIAL_KIND.get(ben.get("kingdom"), "Beneficial Insect")
		add({"organism_name": ben["name"], "kind": kind, "scientific_name": ben.get("scientific_name"),
		     "description": ben.get("description"), "source": ipm_reference.SOURCES[4]})
	for vert in ipm_seed_data.VERTEBRATES:
		add({**vert, "organism_name": vert["name"], "source": "USFWS MBTA list; WDFW/ODFW; PNW handbooks"})
	for name, (status, note) in ipm_seed_data.PROTECTION.items():
		key = node_id(name)
		if key in out:
			out[key].update({"protected_status": status, "protected_note": note})
	for damage in ipm_reference.PEST_DAMAGE:
		key = node_id(damage["pest"])
		if key in out and damage.get("crop") == "Cherries":
			out[key].setdefault("bbch_from", damage.get("vulnerable_bbch_start"))
			out[key].setdefault("bbch_to", damage.get("vulnerable_bbch_end"))
	for act in ipm_reference.BENEFICIAL_ACTIVITY:
		key = node_id(act["beneficial"])
		if key in out and act.get("crop") == "Cherries":
			out[key].setdefault("bbch_from", act.get("active_bbch_start"))
			out[key].setdefault("bbch_to", act.get("active_bbch_end"))
	products = {p["product"]: p for p in ipm_reference.PESTICIDE_PRODUCTS}
	moa = {e["product"]: e.get("moa_group") for e in ipm_reference.PESTICIDE_EFFICACY}
	named = set(products) | {e["product"] for e in ipm_reference.PESTICIDE_EFFICACY} | {
		t["product"] for t in ipm_reference.BENEFICIAL_TOXICITY}
	for name in sorted(named):
		info = products.get(name, {})
		ai = str(info.get("active_ingredient") or "").lower()
		add({"organism_name": name, "kind": "Product", "epa_reg_number": info.get("epa_reg_number"),
		     "moa_group": moa.get(name), "description": info.get("notes"),
		     "ccf_gate": ipm_seed_data.RODENTICIDE_GATE if any(r in ai for r in RODENTICIDES) else None,
		     "source": ipm_reference.SOURCES[-1]})
	return list(out.values())


def _literature_edges() -> list[dict]:
	cherry = node_id("Cherries")
	out = []

	def add(subject, relation, obj, weight=None, **extra):
		s, o = node_id(subject), node_id(obj)
		row = {"subject": s, "relation": relation, "object": o, "weight": weight, "provenance": LITERATURE,
		       "status": ACTIVE, "enabled": 1, "confidence": extra.pop("confidence", 0.7),
		       "seed_key": f"edge:{s}:{relation}:{o}:{extra.get('crop') or ''}", **extra}
		out.append(row)

	for d in ipm_reference.PEST_DAMAGE:
		add(d["pest"], "attacks", d["crop"], d.get("damage_severity"), bbch_from=d.get("vulnerable_bbch_start"),
		    bbch_to=d.get("vulnerable_bbch_end"), notes=d.get("notes"), source=ipm_reference.SOURCES[1])
	for e in ipm_reference.PESTICIDE_EFFICACY:
		add(e["product"], "controls", e["pest"], e.get("efficacy"), notes=e.get("notes"),
		    details=json.dumps({k: e.get(k) for k in ("moa_group", "resistance_risk", "target_life_stage",
		                                              "rainfastness_hours", "residual_days") if e.get(k) is not None}),
		    source=ipm_reference.SOURCES[0])
	for t in ipm_reference.BENEFICIAL_TOXICITY:
		add(t["beneficial"], "harmed_by", t["product"], t.get("toxicity_score"), notes=t.get("sublethal_effects"),
		    details=json.dumps({k: t.get(k) for k in ("toxicity_category", "exposure_route", "residual_toxicity_days",
		                                              "field_safe_days") if t.get(k) is not None}),
		    source=t.get("data_source") or ipm_reference.SOURCES[4], confidence=0.8)
	for subject, relation, obj, weight in ipm_seed_data.PREDATION:
		add(subject, relation, obj, weight, source="PNW Pest Management Handbooks; UC IPM", confidence=0.6)
	for pest, weight, low, high in ipm_seed_data.VERTEBRATE_DAMAGE:
		add(pest, "attacks", "Cherries", weight, bbch_from=low, bbch_to=high, source="PNW handbooks; WSU Tree Fruit",
		    confidence=0.6)
	del cherry
	return out


def _upsert(doctype: str, row: dict, report: dict, label: str) -> str:
	"""Create, or refresh a row that is still Literature. Never touches the farm's rows."""
	existing = frappe.db.get_value(doctype, {"seed_key": row["seed_key"]}, ["name", "provenance"], as_dict=True)
	if existing:
		if existing.get("provenance") != LITERATURE:
			report["kept"] += 1
			return existing["name"]
		doc = frappe.get_doc(doctype, existing["name"])
		changed = False
		for key, value in row.items():
			if key in ("status", "enabled") or value is None:
				continue  # a person who disabled or rejected a literature row keeps that decision
			if doc.get(key) != value:
				doc.set(key, value)
				changed = True
		if changed:
			doc.save(ignore_permissions=True)
			report["refreshed"] += 1
		return doc.name
	if doctype == ORGANISM and frappe.db.exists(ORGANISM, node_id(row["organism_name"])):
		report["kept"] += 1  # the farm made this node first: theirs stands
		return node_id(row["organism_name"])
	doc = frappe.get_doc({"doctype": doctype, **{k: v for k, v in row.items() if v is not None}})
	doc.insert(ignore_permissions=True)
	report["created"] += 1
	report.setdefault(label, 0)
	report[label] += 1
	return doc.name


def seed() -> dict:
	"""Create or refresh the literature graph and the starter thresholds. Never raises; returns a report."""
	report = {"created": 0, "refreshed": 0, "kept": 0, "errors": []}
	if not installed():
		return report
	for row in _literature_nodes():
		try:
			_upsert(ORGANISM, row, report, "nodes")
		except Exception as exc:  # pragma: no cover - one bad row never stops the seed
			report["errors"].append(f"{row.get('organism_name')}: {exc}")
	for row in _literature_edges():
		if not (frappe.db.exists(ORGANISM, row["subject"]) and frappe.db.exists(ORGANISM, row["object"])):
			continue
		try:
			_upsert(RELATIONSHIP, row, report, "edges")
		except Exception as exc:  # pragma: no cover
			report["errors"].append(f"{row['seed_key']}: {exc}")
	report["thresholds"] = seed_thresholds()
	return report


def seed_thresholds() -> int:
	"""Mid-Columbia sweet cherry starter thresholds: disabled, Proposed, with methods. Create-only."""
	if not compat.has_field(THRESHOLD, "status"):
		return 0
	made = 0
	for spec in ipm_seed_data.THRESHOLDS:
		key = f"threshold:cherries:{slug(spec['threat'])}"
		if frappe.db.exists(THRESHOLD, {"seed_key": key}):
			continue
		if frappe.db.exists(THRESHOLD, {"crop": "Cherries", "threat": spec["threat"]}):
			continue  # the farm already has a number for this: no proposal on top of it
		organism = node_id(spec["threat"])
		doc = frappe.get_doc({
			"doctype": THRESHOLD, "crop": "Cherries", "disabled": 1, "status": PROPOSED, "provenance": LITERATURE,
			"seed_key": key, "source": ipm_seed_data.STARTER_SOURCE[:140],
			"notes": ipm_seed_data.STARTER_SOURCE,
			"organism": organism if frappe.db.exists(ORGANISM, organism) else None,
			**{k: v for k, v in spec.items()},
		})
		doc.insert(ignore_permissions=True)
		made += 1
	return made


# ── reading ─────────────────────────────────────────────────────────────────
_NODE_FIELDS = ("name", "organism_name", "kind", "scientific_name", "parent_organism", "protected_status",
                "protected_note", "bbch_from", "bbch_to", "dd_from", "dd_to", "item", "epa_reg_number", "ccf_gate",
                "provenance", "enabled", "status", "company", "moa_group", "aliases", "description", "source",
                "confidence", "modified")
_EDGE_FIELDS = ("name", "subject", "relation", "object", "weight", "confidence", "provenance", "source", "notes",
                "crop", "bbch_from", "bbch_to", "dd_from", "dd_to", "status", "enabled", "company", "details",
                "modified")


def _in_window(low, high, stage) -> bool | None:
	if stage is None:
		return None
	if low in (None, "") and high in (None, ""):
		return True
	return (low in (None, "") or int(low) <= stage) and (high in (None, "") or stage <= int(high))


def describe_node(row: dict, stage: int | None = None) -> dict:
	return {
		"id": row["name"], "name": row.get("organism_name"), "kind": row.get("kind"),
		"scientific_name": row.get("scientific_name") or None, "parent": row.get("parent_organism") or None,
		"protected_status": row.get("protected_status") or "", "protected_note": row.get("protected_note") or None,
		"bbch_from": _int(row.get("bbch_from")), "bbch_to": _int(row.get("bbch_to")),
		"dd_from": _float(row.get("dd_from")), "dd_to": _float(row.get("dd_to")),
		"active_now": _in_window(row.get("bbch_from"), row.get("bbch_to"), stage),
		"item": row.get("item") or None, "epa_reg_number": row.get("epa_reg_number") or None,
		"ccf_gate": row.get("ccf_gate") or None, "provenance": row.get("provenance") or USER,
		"enabled": bool(compat.checked(row.get("enabled"))),
	}


def describe_edge(row: dict, stage: int | None = None) -> dict:
	return {
		"id": row["name"], "subject": row.get("subject"), "relation": row.get("relation"), "object": row.get("object"),
		"weight": _float(row.get("weight")), "confidence": _float(row.get("confidence")) or 0.0,
		"provenance": row.get("provenance") or USER, "source": row.get("source") or None,
		"notes": row.get("notes") or None, "crop": row.get("crop") or None,
		"bbch_from": _int(row.get("bbch_from")), "bbch_to": _int(row.get("bbch_to")),
		"active_now": _in_window(row.get("bbch_from"), row.get("bbch_to"), stage),
		"status": row.get("status") or ACTIVE, "enabled": bool(compat.checked(row.get("enabled"))),
	}


def _int(value):
	return None if value in (None, "") else int(value)


def _float(value):
	return None if value in (None, "") else round(float(value), 3)


def _rows(doctype: str, fields: tuple, filters: dict | None = None, order_by: str = "name asc") -> list[dict]:
	"""Every row matching, paged through internally — no cap on how big the graph may grow."""
	out, start, page = [], 0, 1000
	wanted = compat.existing_fields(doctype, fields)
	while True:
		chunk = frappe.db.get_all(doctype, filters=filters or {}, fields=wanted, order_by=order_by,
		                          limit_start=start, limit_page_length=page) or []
		out.extend(dict(r) for r in chunk)
		if len(chunk) < page:
			return out
		start += page


def _company_ok(row: dict, company: str) -> bool:
	return not row.get("company") or not company or row.get("company") == company


def resolve_crop(crop: str) -> str:
	"""A node id from a node id, a crop name or the reference's spelling ("Cherries")."""
	crop = str(crop or "").strip()
	if not crop:
		return ""
	for candidate in (crop, node_id(crop), slug(crop)):
		if frappe.db.exists(ORGANISM, candidate):
			return candidate
	found = frappe.db.get_value(ORGANISM, {"organism_name": crop}, "name")
	return str(found or "")


def block_context(block: str) -> dict:
	"""The block's crop and its stage: the latest observed BBCH first, then the degree-day estimate."""
	from . import bbch, degree_days, growth_stage

	crop = degree_days._crop_of(block)
	stage, source = None, None
	try:
		row = growth_stage.latest(block)
		if row:
			stage, source = bbch.parse(row.get("growth_stage_code")), "observed"
	except Exception:  # pragma: no cover - a site without the stage register
		pass
	if stage is None:
		try:
			estimate = degree_days.for_block(block, crop)
			if estimate.get("available") and estimate.get("estimated_bbch") is not None:
				stage, source = int(estimate["estimated_bbch"]), "estimated"
		except Exception:  # pragma: no cover - no network, no boundary: no estimate
			pass
	return {"crop": crop, "stage": stage, "source": source}


def version() -> str:
	"""Changes whenever a node or an edge does: the phone's cache key."""
	parts = []
	for doctype in (ORGANISM, RELATIONSHIP):
		newest = frappe.db.get_all(doctype, fields=["name", "modified"], order_by="modified desc", limit=1) or [{}]
		parts.append(f"{newest[0].get('modified')}|{newest[0].get('name')}|{frappe.db.count(doctype)}")
	return hashlib.sha1("/".join(parts).encode()).hexdigest()[:16]


def graph(crop: str = "", stage=None, block: str = "", depth: int = 2, kinds=None, start: int = 0,
          limit: int = DEFAULT_LIMIT, company: str = "", include_disabled: bool = False) -> dict:
	"""The neighbourhood of one crop, `depth` hops out, edges paginated. See the contract."""
	stage_source = None
	if block:
		ctx = block_context(block)
		crop = crop or ctx["crop"]
		if stage in (None, ""):
			stage, stage_source = ctx["stage"], ctx["source"]
	if stage not in (None, ""):
		stage = int(stage)
		stage_source = stage_source or "given"
	else:
		stage = None
	focus = resolve_crop(crop)
	if not focus:
		raise ValueError(f"no crop {crop!r} in the IPM graph" + (f" (block {block})" if block else "") +
		                 ". Name a crop the graph has (list_ipm_organisms kind Crop), or add it.")
	depth = max(1, min(int(depth or 2), 3))
	limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
	start = max(0, int(start or 0))
	kinds = {k.strip() for k in (kinds.split(",") if isinstance(kinds, str) else (kinds or [])) if k and k.strip()}

	node_filters = {} if include_disabled else {"enabled": 1, "status": ("!=", REJECTED)}
	nodes = {r["name"]: r for r in _rows(ORGANISM, _NODE_FIELDS, node_filters) if _company_ok(r, company)}
	edge_filters = {} if include_disabled else {"enabled": 1, "status": ACTIVE}
	parent = nodes.get(focus, {}).get("parent_organism")
	scope = {focus, parent} - {None, ""}
	edges = [r for r in _rows(RELATIONSHIP, _EDGE_FIELDS, edge_filters)
	         if _company_ok(r, company) and (not r.get("crop") or r.get("crop") in scope)
	         and r.get("subject") in nodes and r.get("object") in nodes]

	# The farm's copy of a literature edge stands in for it (`save_relationship` writes beside, never over).
	farm = {(e["subject"], e["relation"], e["object"], e.get("crop") or "") for e in edges if e.get("provenance") != LITERATURE}
	edges = [e for e in edges if e.get("provenance") != LITERATURE
	         or (e["subject"], e["relation"], e["object"], e.get("crop") or "") not in farm]

	by_node: dict[str, list] = {}
	for edge in edges:
		by_node.setdefault(edge["subject"], []).append(edge)
		by_node.setdefault(edge["object"], []).append(edge)
	reached, frontier, chosen = {focus} | scope, {focus} | scope, {}
	for _hop in range(depth):
		nxt = set()
		for node in frontier:
			for edge in by_node.get(node, []):
				other = edge["object"] if edge["subject"] == node else edge["subject"]
				if kinds and nodes[other]["kind"] not in kinds and other not in scope:
					continue
				chosen[edge["name"]] = edge
				if other not in reached:
					reached.add(other)
					nxt.add(other)
		frontier = nxt
	order = {r: i for i, r in enumerate(RELATIONS)}
	ranked = sorted(chosen.values(), key=lambda e: (order.get(e["relation"], 99), e["subject"], e["object"], e["name"]))
	page = ranked[start:start + limit]
	shown = {focus} | {e["subject"] for e in page} | {e["object"] for e in page}
	return {
		"graph_version": version(),
		"crop": describe_node(nodes[focus], stage),
		"block": block or None,
		"stage": {"bbch": stage, "source": stage_source, "label": f"BBCH {stage}" if stage is not None else None},
		"nodes": [describe_node(nodes[n], stage) for n in sorted(shown)],
		"edges": [describe_edge(e, stage) for e in page],
		"total_edges": len(ranked),
		"next_start": start + limit if start + limit < len(ranked) else None,
		"label_caveat": LABEL_CAVEAT,
	}


def organism(name: str, company: str = "") -> dict:
	row = frappe.db.get_value(ORGANISM, name, list(compat.existing_fields(ORGANISM, _NODE_FIELDS)), as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(f"no IPM Organism {name!r}.")
	row = dict(row)
	edges = [describe_edge(r) for side in ("subject", "object")
	         for r in _rows(RELATIONSHIP, _EDGE_FIELDS, {side: name, "enabled": 1, "status": ACTIVE})
	         if _company_ok(r, company)]
	return {
		"node": describe_node(row),
		"description": row.get("description") or None,
		"aliases": [a.strip() for a in str(row.get("aliases") or "").splitlines() if a.strip()],
		"edges": sorted(edges, key=lambda e: (e["relation"], e["subject"], e["object"])),
		"thresholds": thresholds_for(name, company),
	}


_THRESHOLD_FIELDS = ("name", "crop", "crop_stage", "sample_unit", "comparison", "action_threshold",
                     "warning_threshold", "recommended_methods", "status", "disabled", "threat", "organism", "company")


def describe_threshold(row: dict) -> dict:
	return {
		"name": row["name"], "crop": row.get("crop") or None, "crop_stage": row.get("crop_stage") or None,
		"sample_unit": row.get("sample_unit") or None, "comparison": row.get("comparison") or "Greater Than",
		"action_threshold": _float(row.get("action_threshold")),
		"warning_threshold": _float(row.get("warning_threshold")),
		"recommended_methods": row.get("recommended_methods") or None,
		"status": row.get("status") or "Approved", "enabled": not compat.checked(row.get("disabled")),
	}


def thresholds_for(name: str, company: str = "") -> list[dict]:
	if not compat.doctype_exists(THRESHOLD):
		return []
	node_name = frappe.db.get_value(ORGANISM, name, "organism_name") or name
	rows = [r for r in _rows(THRESHOLD, _THRESHOLD_FIELDS, {}) if _company_ok(r, company)
	        and (r.get("organism") == name or str(r.get("threat") or "").lower() == str(node_name).lower())]
	return [describe_threshold(r) for r in rows]


# ── threshold status: lowest impact first ───────────────────────────────────
THREAT_CATEGORY = {"Insect Pest": "Insect", "Mite Pest": "Insect", "Disease": "Disease", "Weed": "Weed",
                   "Vertebrate Pest": "Vertebrate"}
CONTROLLING = ("preys_on", "parasitizes", "controls")


def _node_row(name: str) -> dict:
	row = frappe.db.get_value(ORGANISM, name, list(compat.existing_fields(ORGANISM, _NODE_FIELDS)), as_dict=True)
	if not row:
		raise frappe.DoesNotExistError(f"no IPM Organism {name!r} in the IPM graph.")
	return dict(row)


def threshold_crop_names(crop_node: str, raw: str = "") -> list[str]:
	"""The spellings a Pest Action Threshold's free-text `crop` may use for this crop."""
	names = [raw] if raw else []
	if crop_node:
		row = frappe.db.get_value(ORGANISM, crop_node, ["organism_name", "aliases"], as_dict=True) or {}
		names += [a.strip() for a in str(row.get("aliases") or "").splitlines() if a.strip()]
		names.append(str(row.get("organism_name") or ""))
	seen, out = set(), []
	for n in names:
		if n and n.lower() not in seen:
			seen.add(n.lower())
			out.append(n)
	return out


def _find_threshold(company: str, crops: list[str], threat: str, stage_text: str, enabled: bool) -> dict | None:
	rows = _rows(THRESHOLD, _THRESHOLD_FIELDS, {"disabled": 0 if enabled else 1})
	for crop in crops:
		for row in rows:
			if (str(row.get("crop") or "").lower() == crop.lower() and str(row.get("threat") or "").lower() == threat.lower()
			        and _company_ok(row, company)):
				return row
	return None


def options_for(pest: str, crop_node: str, stage: int | None, company: str = "") -> tuple[list, str | None]:
	"""What can be done about `pest`, lowest impact on what is working now first. And the MBTA note."""
	row = _node_row(pest)
	nodes = {r["name"]: r for r in _rows(ORGANISM, _NODE_FIELDS, {"enabled": 1}) if _company_ok(r, company)}
	edges = [r for r in _rows(RELATIONSHIP, _EDGE_FIELDS, {"enabled": 1, "status": ACTIVE})
	         if _company_ok(r, company) and (not r.get("crop") or r.get("crop") == crop_node)]
	protected = row.get("protected_status") in MBTA
	note = None
	options: list[dict] = []

	group = ipm_seed_data.VERTEBRATE_GROUP.get(row.get("organism_name"))
	if row.get("kind") == "Vertebrate Pest" and group:
		for kind, title in ipm_seed_data.NON_LETHAL.get(group, []):
			options.append({"kind": kind, "title": title, "product": None, "harms_active": [], "impact": 0.0,
			                "efficacy": None, "ccf_gate": None, "note": None})
	if protected:
		note = (f"{row.get('organism_name')} is protected ({row.get('protected_status')}). "
		        f"{row.get('protected_note') or ''} Non-lethal options only.").strip()

	for edge in edges:
		if edge["object"] != pest or edge["relation"] not in CONTROLLING:
			continue
		agent = nodes.get(edge["subject"])
		if not agent:
			continue
		if agent["kind"] == "Product":
			continue
		active = _in_window(agent.get("bbch_from"), agent.get("bbch_to"), stage)
		options.append({"kind": "biological", "title": f"{agent['organism_name']} ({edge['relation'].replace('_', ' ')})"
		                + ("" if active is not False else " — not active at this stage"),
		                "product": agent["name"], "harms_active": [], "impact": 0.0,
		                "efficacy": _float(edge.get("weight")), "ccf_gate": None,
		                "note": "Conserve it: the products below that harm it are ranked lower."})

	if not protected:
		working = {n for n, r in nodes.items() if r["kind"] in BENEFICIAL_KINDS
		           and _in_window(r.get("bbch_from"), r.get("bbch_to"), stage) is not False}
		harms: dict[str, dict] = {}
		for edge in edges:
			if edge["relation"] == "harmed_by" and edge["subject"] in working:
				harms.setdefault(edge["object"], {})[edge["subject"]] = float(edge.get("weight") or 0)
		products = []
		for edge in edges:
			if edge["object"] != pest or edge["relation"] != "controls":
				continue
			product = nodes.get(edge["subject"])
			if not product or product["kind"] != "Product":
				continue
			hit = harms.get(product["name"], {})
			products.append({
				"kind": "product", "title": product["organism_name"], "product": product["name"],
				"harms_active": sorted(b for b, w in hit.items() if w >= 0.3),
				"impact": round(max(hit.values(), default=0.0), 3), "efficacy": _float(edge.get("weight")),
				"ccf_gate": product.get("ccf_gate") or None,
				"note": ("Goes through the rodent-bait task rules (applicator, notice, label)."
				         if product.get("ccf_gate") == ipm_seed_data.RODENTICIDE_GATE else None),
			})
		products.sort(key=lambda o: (o["impact"], -(o["efficacy"] or 0), o["title"]))
		options.extend(products)
	order = {"exclusion": 0, "cultural": 1, "biological": 2, "product": 3}
	options.sort(key=lambda o: (o["impact"], order.get(o["kind"], 9)))
	return options, note


def threshold_status(organism_name: str, block: str = "", crop: str = "", count=None, sample_unit: str = "",
                     sample_size=None, beneficials=None, company: str = "") -> dict:
	"""Where this pest stands against its threshold, and what to do — lowest impact first."""
	from .tools import cropprotect

	row = _node_row(organism_name)
	stage = None
	if block:
		ctx = block_context(block)
		crop = crop or ctx["crop"]
		stage = ctx["stage"]
	crop_node = resolve_crop(crop)
	crops = threshold_crop_names(crop_node, crop)
	threat = str(row.get("organism_name"))
	applied = _find_threshold(company, crops, threat, "", enabled=True)
	status = "no_threshold"
	evaluation = None
	if applied:
		if count in (None, ""):
			status = "unmeasured"
		else:
			evaluation = cropprotect._evaluate(applied, float(count), int(sample_size or 0),
			                                   None if beneficials in (None, "") else float(beneficials), sample_unit)
			status = "action" if evaluation["action_exceeded"] else "warning" if evaluation["warning_exceeded"] else "below"
	else:
		proposed = _find_threshold(company, crops, threat, "", enabled=False)
		if proposed:
			applied, status = proposed, "not_approved"
	options, note = options_for(row["name"], crop_node, stage, company)
	name = row.get("organism_name")
	message = {
		"action": f"{name} is over the action threshold. Act — the lowest-impact option is first.",
		"warning": f"{name} is over the warning level. Watch it; re-scout soon.",
		"below": f"{name} is below the threshold. No action needed now.",
		"unmeasured": f"{name} has a threshold; log a count to compare.",
		"not_approved": f"{name} has a proposed threshold that nobody has approved yet, so nothing was compared.",
		"no_threshold": f"No action threshold is on file for {name} on this crop.",
	}[status]
	if evaluation and evaluation.get("beneficials_holding"):
		message += " Beneficials look to be holding it — hold and re-scout."
	return {
		"organism": describe_node(row, stage),
		"status": status,
		"threshold": describe_threshold(applied) if applied else None,
		"observed": {"count": None if count in (None, "") else float(count), "sample_unit": sample_unit or None},
		"message": message,
		"options": options,
		"protected_species_note": note,
		"label_caveat": LABEL_CAVEAT,
	}


# ── writing ─────────────────────────────────────────────────────────────────
EDGE_KEYS = ("subject", "relation", "object", "crop")
EDGE_EDITABLE = ("weight", "confidence", "notes", "source", "crop", "bbch_from", "bbch_to", "dd_from", "dd_to",
                 "enabled", "details", "company")
NODE_EDITABLE = ("organism_name", "kind", "scientific_name", "parent_organism", "aliases", "description",
                 "protected_status", "protected_note", "ccf_gate", "bbch_from", "bbch_to", "dd_from", "dd_to",
                 "dd_base_f", "item", "epa_reg_number", "moa_group", "source", "confidence", "notes", "enabled",
                 "company")


def _node_ref(value) -> str:
	"""A node id from an id or a name; raises when it is neither."""
	value = str(value or "").strip()
	for candidate in (value, slug(value)):
		if candidate and frappe.db.exists(ORGANISM, candidate):
			return candidate
	found = frappe.db.get_value(ORGANISM, {"organism_name": value}, "name") if value else None
	if not found:
		raise frappe.ValidationError(f"no IPM Organism {value!r} — create it first (create_ipm_organism).")
	return str(found)


def _clean(values: dict, allowed: tuple) -> dict:
	out = {}
	for key in allowed:
		if key in values and values[key] is not None:
			value = values[key]
			if key == "enabled":
				value = 1 if compat.checked(value) or value is True else 0
			if key == "details" and isinstance(value, dict):
				value = json.dumps(value)
			out[key] = value
	return out


def save_relationship(values: dict, user: str, provenance: str = USER, status: str = ACTIVE,
                      relationship: str = "") -> tuple[dict, bool]:
	"""Create or edit one edge. A Literature edge is never edited: the farm's copy is written beside it."""
	now = frappe.utils.now()
	if relationship:
		current = frappe.db.get_value(RELATIONSHIP, relationship, ["name", "provenance", "subject", "relation",
		                                                           "object", "crop"], as_dict=True)
		if not current:
			raise frappe.DoesNotExistError(f"no IPM Relationship {relationship!r}.")
		if current.get("provenance") != LITERATURE:
			doc = frappe.get_doc(RELATIONSHIP, relationship)
			for key, value in _clean(values, EDGE_EDITABLE).items():
				doc.set(key, value)
			doc.save(ignore_permissions=True)
			return describe_edge(dict(doc.as_dict())), False
		values = {**{k: current.get(k) for k in EDGE_KEYS}, **values}
	key = {k: (_node_ref(values.get(k)) if k in ("subject", "object") else values.get(k)) for k in EDGE_KEYS}
	key["crop"] = _node_ref(key["crop"]) if key.get("crop") else None
	if key["relation"] not in RELATIONS:
		raise frappe.ValidationError(f"relation must be one of {', '.join(RELATIONS)}.")
	match = frappe.db.get_value(RELATIONSHIP, {
		"subject": key["subject"], "relation": key["relation"], "object": key["object"],
		"crop": key["crop"] or ("is", "not set"), "provenance": ("!=", LITERATURE)}, "name")
	if match:
		doc = frappe.get_doc(RELATIONSHIP, match)
		for k, v in _clean(values, EDGE_EDITABLE).items():
			doc.set(k, v)
		doc.save(ignore_permissions=True)
		return describe_edge(dict(doc.as_dict())), False
	doc = frappe.get_doc({
		"doctype": RELATIONSHIP, **{k: v for k, v in key.items() if v}, **_clean(values, EDGE_EDITABLE),
		"provenance": provenance, "status": status, "proposed_by": user,
		**({"approved_by": user, "approved_on": now} if status == ACTIVE else {}),
	})
	if "enabled" not in values:
		doc.enabled = 1 if status == ACTIVE else 0
	if "confidence" not in values or values.get("confidence") is None:
		doc.confidence = 0.7
	doc.insert(ignore_permissions=True)
	return describe_edge(dict(doc.as_dict())), True


def save_organism(values: dict, user: str, provenance: str = USER, status: str = ACTIVE, name: str = "") -> tuple[dict, bool]:
	if name:
		doc = frappe.get_doc(ORGANISM, _node_ref(name))
		if doc.provenance == LITERATURE:
			doc.provenance = USER  # once the farm edits a seeded node, it is the farm's and re-seed leaves it alone
		for key, value in _clean(values, NODE_EDITABLE).items():
			if key == "parent_organism" and value:
				value = _node_ref(value)
			doc.set(key, value)
		doc.save(ignore_permissions=True)
		return describe_node(dict(doc.as_dict())), False
	clean = _clean(values, NODE_EDITABLE)
	if not clean.get("organism_name") or not clean.get("kind"):
		raise frappe.ValidationError("organism_name and kind are required.")
	if clean.get("parent_organism"):
		clean["parent_organism"] = _node_ref(clean["parent_organism"])
	doc = frappe.get_doc({"doctype": ORGANISM, **clean, "provenance": provenance, "status": status,
	                      "proposed_by": user, "enabled": clean.get("enabled", 1 if status == ACTIVE else 0)})
	doc.insert(ignore_permissions=True)
	return describe_node(dict(doc.as_dict())), True


# ── import and export ───────────────────────────────────────────────────────
def _parse(data, fmt: str) -> dict:
	"""{"nodes": [...], "edges": [...]} from JSON (object or string) or one CSV with a `record` column."""
	if fmt == "csv":
		import csv
		import io

		nodes, edges = [], []
		for row in csv.DictReader(io.StringIO(str(data or ""))):
			row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
			record = (row.pop("record", "") or "").lower()
			row = {k: v for k, v in row.items() if v not in ("", None)}
			(nodes if record == "node" else edges if record == "edge" else []).append(row)
		return {"nodes": nodes, "edges": edges}
	parsed = json.loads(data) if isinstance(data, str) else (data or {})
	return {"nodes": list(parsed.get("nodes") or []), "edges": list(parsed.get("edges") or [])}


def import_graph(data, fmt: str = "json", dry_run: bool = True, status: str = PROPOSED, user: str = "") -> dict:
	"""Add or change nodes and edges in bulk. Dry run (default) writes nothing and returns the diff.

	Literature rows are never changed: an imported edge matching one is added as the farm's own row.
	`status` Proposed (default) lands rows disabled for approve_ipm_proposal; Active lands them live.
	"""
	parsed = _parse(data, fmt)
	diff = {"add": [], "change": [], "skip": [], "error": []}
	pending_nodes: dict[str, dict] = {}
	for raw in parsed["nodes"]:
		name = str(raw.get("organism_name") or raw.get("name") or "").strip()
		if not name or not raw.get("kind"):
			diff["error"].append({"record": "node", "row": raw, "why": "organism_name and kind are required"})
			continue
		key = slug(name)
		values = {**raw, "organism_name": name}
		values.pop("name", None)
		existing = frappe.db.get_value(ORGANISM, key, ["name", "provenance"], as_dict=True)
		if existing and existing.get("provenance") == LITERATURE:
			diff["skip"].append({"record": "node", "id": key, "why": "a literature node; edit it with update_ipm_organism"})
			continue
		if existing:
			current = frappe.get_doc(ORGANISM, key)
			changes = {k: v for k, v in _clean(values, NODE_EDITABLE).items() if str(current.get(k) or "") != str(v)}
			(diff["change"] if changes else diff["skip"]).append(
				{"record": "node", "id": key, **({"fields": changes} if changes else {"why": "unchanged"})})
			if changes and not dry_run:
				save_organism(changes, user, name=key)
		else:
			diff["add"].append({"record": "node", "id": key, "name": name, "kind": raw.get("kind")})
			pending_nodes[key] = values
			if not dry_run:
				save_organism(values, user, provenance=IMPORTED, status=status)

	def known(ref) -> bool:
		ref = str(ref or "")
		return bool(ref) and (slug(ref) in pending_nodes or frappe.db.exists(ORGANISM, ref) or frappe.db.exists(ORGANISM, slug(ref))
		                      or frappe.db.exists(ORGANISM, {"organism_name": ref}))

	for raw in parsed["edges"]:
		missing = [k for k in ("subject", "relation", "object") if not raw.get(k)]
		if missing or raw.get("relation") not in RELATIONS:
			diff["error"].append({"record": "edge", "row": raw, "why": f"needs subject, a known relation and object"})
			continue
		unknown = [k for k in ("subject", "object") if not known(raw[k])] + (["crop"] if raw.get("crop") and not known(raw["crop"]) else [])
		if unknown:
			diff["error"].append({"record": "edge", "row": raw, "why": f"unknown organism in {', '.join(unknown)}"})
			continue
		label = f"{slug(raw['subject'])} {raw['relation']} {slug(raw['object'])}"
		if dry_run:
			try:
				subject, obj = _node_ref(raw["subject"]), _node_ref(raw["object"])
				crop = _node_ref(raw["crop"]) if raw.get("crop") else None
				match = frappe.db.get_value(RELATIONSHIP, {"subject": subject, "relation": raw["relation"], "object": obj,
				                                           "crop": crop or ("is", "not set"), "provenance": ("!=", LITERATURE)}, "name")
			except frappe.ValidationError:
				match = None  # a node this same import adds
			(diff["change"] if match else diff["add"]).append({"record": "edge", "edge": label, **({"id": match} if match else {})})
			continue
		try:
			edge, created = save_relationship(raw, user, provenance=IMPORTED, status=status)
			(diff["add"] if created else diff["change"]).append({"record": "edge", "edge": label, "id": edge["id"]})
		except Exception as exc:
			diff["error"].append({"record": "edge", "row": raw, "why": str(exc)})
	counts = {k: len(v) for k, v in diff.items()}
	return {"dry_run": dry_run, "status": status, "counts": counts, "diff": diff}


def export_graph(fmt: str = "json", include_literature: bool = True, start: int = 0, limit: int = DEFAULT_LIMIT) -> dict:
	"""Every node and edge (paged on edges), in the shape `import_graph` reads back."""
	limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
	filters = {} if include_literature else {"provenance": ("!=", LITERATURE)}
	nodes = _rows(ORGANISM, _NODE_FIELDS, filters)
	edges = _rows(RELATIONSHIP, _EDGE_FIELDS, filters)
	page = edges[start:start + limit]
	node_out = [{k: n.get(k) for k in ("organism_name", "kind", "scientific_name", "parent_organism", "protected_status",
	                                   "bbch_from", "bbch_to", "epa_reg_number", "ccf_gate", "provenance", "status", "enabled")
	             if n.get(k) not in (None, "")} for n in nodes] if start == 0 else []
	edge_out = [{k: e.get(k) for k in ("subject", "relation", "object", "weight", "confidence", "crop", "bbch_from",
	                                   "bbch_to", "provenance", "status", "enabled", "source", "notes")
	             if e.get(k) not in (None, "")} for e in page]
	out = {"nodes": node_out, "edges": edge_out, "total_nodes": len(nodes), "total_edges": len(edges),
	       "next_start": start + limit if start + limit < len(edges) else None}
	if fmt == "csv":
		import csv
		import io

		buffer = io.StringIO()
		columns = ["record", "organism_name", "kind", "scientific_name", "parent_organism", "protected_status", "subject",
		           "relation", "object", "weight", "confidence", "crop", "bbch_from", "bbch_to", "provenance", "status",
		           "enabled", "source", "notes", "epa_reg_number", "ccf_gate"]
		writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
		writer.writeheader()
		for n in node_out:
			writer.writerow({"record": "node", **n})
		for e in edge_out:
			writer.writerow({"record": "edge", **e})
		out["csv"] = buffer.getvalue()
	return out


# ── AI proposals and approval ───────────────────────────────────────────────
def propose(items: dict, citation: str, authored_by: str, user: str) -> dict:
	"""An agent's reading of a handbook page or regulation, landed Proposed and disabled for a person to approve."""
	made = {"nodes": [], "edges": [], "thresholds": [], "errors": []}
	note = f"Proposed from: {citation}"
	for raw in items.get("nodes") or []:
		try:
			node, _ = save_organism({**raw, "source": citation, "notes": note}, authored_by or user, provenance=AI, status=PROPOSED)
			made["nodes"].append(node["id"])
		except Exception as exc:
			made["errors"].append({"record": "node", "row": raw, "why": str(exc)})
	for raw in items.get("edges") or []:
		try:
			edge, _ = save_relationship({**raw, "source": citation, "notes": raw.get("notes") or note},
			                            authored_by or user, provenance=AI, status=PROPOSED)
			made["edges"].append(edge["id"])
		except Exception as exc:
			made["errors"].append({"record": "edge", "row": raw, "why": str(exc)})
	for raw in items.get("thresholds") or []:
		try:
			made["thresholds"].append(_propose_threshold(raw, citation, authored_by or user))
		except Exception as exc:
			made["errors"].append({"record": "threshold", "row": raw, "why": str(exc)})
	return made


def _propose_threshold(raw: dict, citation: str, author: str) -> str:
	fields = ("crop", "threat", "threat_category", "crop_stage", "sample_unit", "comparison", "action_threshold",
	          "warning_threshold", "beneficial_ratio_min", "min_sample_size", "recommended_methods", "company")
	values = {k: raw.get(k) for k in fields if raw.get(k) not in (None, "")}
	if not values.get("crop") or not values.get("threat"):
		raise frappe.ValidationError("a threshold needs crop and threat")
	if not values.get("recommended_methods"):
		raise frappe.ValidationError("a proposed threshold needs recommended_methods — a number with no action is half a threshold")
	organism = raw.get("organism") or values["threat"]
	try:
		values["organism"] = _node_ref(organism)
		if not values.get("threat_category"):
			values["threat_category"] = THREAT_CATEGORY.get(frappe.db.get_value(ORGANISM, values["organism"], "kind"))
	except frappe.ValidationError:
		pass
	doc = frappe.get_doc({"doctype": THRESHOLD, **values, "disabled": 1, "status": PROPOSED, "provenance": AI,
	                      "source": citation[:140], "notes": f"Proposed by {author} from: {citation}"})
	doc.insert(ignore_permissions=True)
	return doc.name


def decide(names: list, decision: str, user: str, note: str = "") -> dict:
	"""A person approves or rejects proposals: nodes, edges and thresholds, by docname."""
	if decision not in ("approve", "reject"):
		raise frappe.ValidationError("decision is approve or reject.")
	now = frappe.utils.now()
	done, missing = [], []
	for name in names or []:
		for doctype in (RELATIONSHIP, ORGANISM, THRESHOLD):
			if frappe.db.exists(doctype, name):
				doc = frappe.get_doc(doctype, name)
				if doctype == THRESHOLD:
					doc.status = "Approved" if decision == "approve" else REJECTED
					doc.disabled = 0 if decision == "approve" else 1
				else:
					doc.status = ACTIVE if decision == "approve" else REJECTED
					doc.enabled = 1 if decision == "approve" else 0
				doc.approved_by, doc.approved_on = user, now
				if note:
					doc.notes = f"{doc.get('notes') or ''}\n{decision.title()}d by {user}: {note}".strip()
				doc.save(ignore_permissions=True)
				done.append({"name": name, "doctype": doctype, "decision": decision})
				break
		else:
			missing.append(name)
	return {"done": done, "missing": missing}


def thresholds_without_methods(crop: str = "") -> list[str]:
	"""The quality check Tim asked for: thresholds that name no recommended method."""
	filters = {"crop": crop} if crop else {}
	return [r["name"] for r in _rows(THRESHOLD, _THRESHOLD_FIELDS, filters) if not str(r.get("recommended_methods") or "").strip()]
