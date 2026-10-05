# SPDX-License-Identifier: MIT
"""The Reference Library: the MCP half. v0.238.0. See `erpnext_mcp.reference_library`."""

from __future__ import annotations

import frappe

from .. import reference_library as library
from ..args import as_int, as_str
from ..errors import ToolError
from ..result import ToolResult
from . import files


def _user() -> str:
	return str(getattr(frappe.session, "user", "") or "")


def search_references(args: dict) -> ToolResult:
	hits = library.search(
		as_str(args, "query", required=True),
		{k: args.get(k) for k in ("ref_type", "crop", "topic", "include_superseded") if args.get(k)},
		as_int(args, "limit", 20),
	)
	return ToolResult(data={"hits": hits, "count": len(hits)}, summary=f"{len(hits)} hit(s)")


def get_reference(args: dict) -> ToolResult:
	name = as_str(args, "reference", required=True)
	if not frappe.db.exists(library.DOCTYPE, name):
		raise ToolError(f"no Reference Document {name!r}.")
	pages = args.get("pages")
	wanted = [int(p) for p in (pages if isinstance(pages, list) else str(pages or "").replace(",", " ").split()) if str(p).isdigit()]
	data = library.describe(name, wanted or None)
	return ToolResult(data=data, summary=f"{data['title']} ({data['text_status']})")


def list_references(args: dict) -> ToolResult:
	filters = {}
	for field in ("ref_type", "publisher", "text_status"):
		if as_str(args, field):
			filters[field] = as_str(args, field)
	rows = frappe.db.get_all(
		library.DOCTYPE, filters=filters,
		fields=["name", "title", "ref_type", "publisher", "year", "text_status", "page_count", "superseded_by"],
		order_by="title asc", limit=max(1, min(as_int(args, "limit", 100), 500)))
	return ToolResult(data={"references": rows, "count": len(rows)}, summary=f"{len(rows)} reference(s)")


def add_reference(args: dict) -> ToolResult:
	file_name = as_str(args, "file_name") or "reference.pdf"
	if as_str(args, "file"):
		data = files.read_file_bytes(as_str(args, "file"))
	elif as_str(args, "file_content"):
		data = files.decode_base64_content(as_str(args, "file_content"), tail="Nothing was written.")
	else:
		raise ToolError("pass file (a File docname already on the site — attached, staged or uploaded) or file_content (base64, up to 8 MB).")
	data = library.add(
		data=data, file_name=file_name, title=as_str(args, "title", required=True),
		ref_type=as_str(args, "ref_type", required=True), user=_user(),
		publisher=as_str(args, "publisher"), authors=as_str(args, "authors"), year=args.get("year"),
		publication_number=as_str(args, "publication_number"), source_url=as_str(args, "source_url"),
		tags={k: args.get(k) for k in ("crops", "varieties", "topics", "regions")},
		summary=as_str(args, "summary"), key_takeaways=as_str(args, "key_takeaways"),
	)
	return ToolResult(data=data, summary=f"added {data['name']}: {data['title']} ({data['text_status']}, {data['page_count']} page(s))",
	                  docstatus_delta="none → 0 (created)")


def update_reference(args: dict) -> ToolResult:
	name = as_str(args, "reference", required=True)
	if not frappe.db.exists(library.DOCTYPE, name):
		raise ToolError(f"no Reference Document {name!r}. Nothing was changed.")
	doc = frappe.get_doc(library.DOCTYPE, name)
	for field in ("title", "publisher", "authors", "publication_number", "source_url", "summary", "key_takeaways"):
		if field in args:
			doc.set(field, as_str(args, field))
	if "year" in args:
		doc.year = as_int(args, "year") or None
	tags = {k: args[k] for k in ("crops", "varieties", "topics", "regions") if k in args}
	if tags:
		doc.update(library.check_tags({**{k: doc.get(k) for k in ("crops", "varieties", "topics", "regions")}, **tags}))
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	if isinstance(args.get("page_texts"), dict):
		data = library.set_page_text(name, args["page_texts"], as_str(args, "text_source") or "OCR")
	else:
		data = library.describe(name)
	return ToolResult(data=data, summary=f"updated {name} ({data['text_status']})", docstatus_delta="0 → 0 (updated)")


def supersede_reference(args: dict) -> ToolResult:
	old = as_str(args, "reference", required=True)
	new = as_str(args, "superseded_by", required=True)
	for name in (old, new):
		if not frappe.db.exists(library.DOCTYPE, name):
			raise ToolError(f"no Reference Document {name!r}. Nothing was changed.")
	if old == new:
		raise ToolError("a document cannot supersede itself. Nothing was changed.")
	frappe.db.set_value(library.DOCTYPE, old, "superseded_by", new)
	return ToolResult(data=library.describe(old), summary=f"{old} superseded by {new}", docstatus_delta="0 → 0 (updated)")
