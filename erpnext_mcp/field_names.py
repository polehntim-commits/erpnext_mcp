# SPDX-License-Identifier: MIT
"""Find a block by the name people actually use. v0.269.0 (docs/contracts/field_self_service_v0_269.yaml).

One rule, everywhere — the phone, the Desk, MCP: case-, space- and punctuation-insensitive, over the docname, the
field name, the buyer ticker and the block's aliases. An alias several blocks share is a NAMED GROUP ("Bing Block" →
Wind Machine and Center Piece). Anything else ambiguous comes back as candidates and is NEVER silently picked.
"""

from __future__ import annotations

import re

import frappe

from . import compat

FIELD = "Field"
_DROP = re.compile(r"[\s\-_.'’`]+")


def normalise(text) -> str:
	"""'Center Piece' == 'centerpiece' == 'CENTER-PIECE'; '&' reads as 'and'."""
	return _DROP.sub("", str(text or "").replace("&", "and").casefold())


def aliases_of(row: dict) -> list:
	return [line.strip() for line in str(row.get("aliases") or "").splitlines() if line.strip()]


def _rows(companies=None) -> list:
	if not compat.doctype_exists(FIELD):
		return []
	fields = compat.existing_fields(
		FIELD, ("name", "field_name", "parcel", "owning_entity", "acreage", "block_ticker", "aliases",
		        "acreage_source", "area_computed_acres", "crop", "variety")
	)
	filters = {"owning_entity": ("in", list(companies))} if companies is not None else {}
	return [dict(r) for r in frappe.db.get_all(FIELD, filters=filters, fields=fields, limit=5000) or []]


def _candidate(row: dict, matched_on: str, text: str) -> dict:
	return {
		"field": row["name"],
		"field_name": row.get("field_name"),
		"parcel": row.get("parcel"),
		"owning_entity": row.get("owning_entity"),
		"acreage": row.get("acreage"),
		"matched_on": matched_on,
		"matched_text": text,
	}


def find(query, companies=None) -> dict:
	"""{query, resolved, candidates, group}. `companies=None` is the operator's MCP view (not scoped).

	A named group is one alias shared by several blocks OF ONE COMPANY; the same alias in two companies is two
	different places and comes back as candidates."""
	want = normalise(query)
	out = {"query": str(query or ""), "resolved": [], "candidates": [], "group": None}
	if not want:
		return out
	by_name: dict = {}   # field docname → candidate (own name / docname / ticker)
	by_alias: dict = {}  # alias text → [field docnames]
	rows = _rows(companies)
	for row in rows:
		for label, value in (("docname", row["name"]), ("field_name", row.get("field_name")),
		                     ("block_ticker", row.get("block_ticker"))):
			if value and normalise(value) == want:
				by_name.setdefault(row["name"], _candidate(row, label, value))
		for alias in aliases_of(row):
			if normalise(alias) == want:
				by_alias.setdefault(alias, []).append(row)
	alias_rows = {row["name"]: (alias, row) for alias, group in by_alias.items() for row in group}
	candidates = list(by_name.values()) + [
		_candidate(row, "alias", alias) for name, (alias, row) in alias_rows.items() if name not in by_name
	]
	out["candidates"] = sorted(candidates, key=lambda c: (c["field_name"] or "", c["field"]))
	names = {c["field"] for c in candidates}
	if len(names) == 1:
		out["resolved"] = sorted(names)
	elif (not by_name and len(by_alias) == 1 and len(names) > 1
	      and len({row.get("owning_entity") for _alias, row in alias_rows.values()}) == 1):
		# One alias shared by several blocks and nothing else answering to the name: a named group.
		out["resolved"] = sorted(names)
		out["group"] = next(iter(by_alias))
	return out


def resolve_one(query, companies=None) -> str:
	"""Exactly one block, or ValueError naming the candidates (or that nothing matched)."""
	found = find(query, companies)
	if len(found["resolved"]) == 1:
		return found["resolved"][0]
	if found["candidates"]:
		listed = "; ".join(f"{c['field']} ({c['matched_on']} '{c['matched_text']}')" for c in found["candidates"])
		raise ValueError(f"'{query}' is more than one block — {listed}. Name one of them.")
	raise ValueError(f"no block answers to '{query}'. find_fields lists them; set_field_aliases adds a name.")


def resolve_many(query, companies=None) -> list:
	"""A block or a named group; ValueError with the candidates when it is ambiguous otherwise."""
	found = find(query, companies)
	if found["resolved"]:
		return found["resolved"]
	if found["candidates"]:
		listed = "; ".join(f"{c['field']} ({c['matched_on']} '{c['matched_text']}')" for c in found["candidates"])
		raise ValueError(f"'{query}' is ambiguous — {listed}. Name one of them.")
	raise ValueError(f"no block answers to '{query}'.")
