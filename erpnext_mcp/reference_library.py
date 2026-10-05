# SPDX-License-Identifier: MIT
"""The Reference Library. v0.238.0. docs/design/reference_library.md (approved; queue item 4).

A searchable library of PDFs — OSU/WSU extension guides, research, labels, manuals, regulations,
internal documents — to look up and cite, to the page, when SOPs and rules are drafted.

* ONE DOCTYPE, TWO CHILD TABLES: Reference Document (metadata, tags, the private PDF, summary,
  takeaways, SHA-256 so nothing loads twice, text status) with its Reference Pages; and Reference
  Citation, carried by Compliance Policy (SOPs), Farm Task Template, Training Type, Compliance Rule
  and Inspection Template. "Cited by" is a query over the citations.
* TEXT: on add, each page's text layer is read (pypdf). A page with none — a scan — leaves the
  document "OCR pending"; Tim's Mac OCRs it with Apple Vision and posts the text back through
  `update_reference` (decision 41: Mac-side OCR). Without pypdf on a box, every page is pending.
* TAGS are checked against vocabularies held in ERPNext MCP Settings, editable there (decision).
* COPYRIGHT: PDFs are private, never on a public link; the source URL is kept; answers quote short
  snippets with a citation. Nothing is re-published.
"""

from __future__ import annotations

import hashlib
import io
import re

import frappe

from . import compat
from .errors import ToolError

DOCTYPE = "Reference Document"
PAGE = "Reference Page"
CITATION = "Reference Citation"
TYPES = ("Extension Guide", "Research Paper", "Manual", "Label", "Regulation", "Internal")
TEXT_LAYER, OCR_PENDING, OCRD = "Text layer", "OCR pending", "OCR'd"
TAG_FIELDS = {"crops": "reference_crops", "topics": "reference_topics", "regions": "reference_regions"}
CITING = ("Compliance Policy", "Farm Task Template", "Training Type", "Compliance Rule", "Inspection Template")

SNIPPET = 160
#: A page with fewer characters than this read from its text layer is treated as a scan.
MIN_TEXT = 20


def _lines(value) -> list:
	if isinstance(value, (list, tuple)):
		items = value
	else:
		items = str(value or "").replace(",", "\n").splitlines()
	return [str(item).strip() for item in items if str(item).strip()]


def vocabulary(field: str) -> list:
	from . import settings

	return _lines(settings.get_settings().get(TAG_FIELDS[field]) or "")


def check_tags(tags: dict) -> dict:
	"""Tags as newline lists; each must be in its vocabulary when the vocabulary is set."""
	out = {}
	for field in ("crops", "varieties", "topics", "regions"):
		values = _lines(tags.get(field))
		if field in TAG_FIELDS:
			allowed = vocabulary(field)
			unknown = [v for v in values if allowed and v not in allowed]
			if unknown:
				raise ToolError(
					f"{field} {', '.join(unknown)} not in the vocabulary (ERPNext MCP Settings → "
					f"{TAG_FIELDS[field]}: {', '.join(allowed)}). Add it there first. Nothing was written."
				)
		out[field] = "\n".join(values)
	return out


def extract_pages(data: bytes) -> list[str] | None:
	"""Each page's text-layer text, or None where pypdf is not installed."""
	try:
		from pypdf import PdfReader
	except ImportError:
		return None
	try:
		reader = PdfReader(io.BytesIO(data))
		return [(page.extract_text() or "").strip() for page in reader.pages]
	except Exception:
		return []


def add(
	*,
	data: bytes,
	file_name: str,
	title: str,
	ref_type: str,
	user: str,
	publisher: str = "",
	authors: str = "",
	year=None,
	publication_number: str = "",
	source_url: str = "",
	tags: dict | None = None,
	summary: str = "",
	key_takeaways: str = "",
) -> dict:
	"""File one PDF in the library. Refuses a duplicate (same bytes) and an untyped or unsourced one."""
	if not data[:5] == b"%PDF-":
		raise ToolError("the library holds PDFs; this file is not one. Nothing was written.")
	if ref_type not in TYPES:
		raise ToolError(f"ref_type is one of: {', '.join(TYPES)}. Nothing was written.")
	if ref_type != "Internal" and not str(source_url or "").strip():
		raise ToolError(
			"source_url is required (where the document came from) for everything except Internal documents. "
			"Nothing was written."
		)
	digest = hashlib.sha256(data).hexdigest()
	already = frappe.db.get_value(DOCTYPE, {"sha256": digest}, "name")
	if already:
		raise ToolError(f"this exact PDF is already in the library as {already}. Nothing was written.")
	tagged = check_tags(tags or {})
	pages = extract_pages(data)
	doc = frappe.new_doc(DOCTYPE)
	doc.update(
		{
			"title": str(title).strip()[:140],
			"ref_type": ref_type,
			"publisher": publisher,
			"authors": authors,
			"year": int(year) if str(year or "").strip().isdigit() else None,
			"publication_number": publication_number,
			"source_url": source_url,
			"summary": summary,
			"key_takeaways": key_takeaways,
			"sha256": digest,
			"added_by": user,
			**tagged,
		}
	)
	pending = pages is None or any(len(text) < MIN_TEXT for text in pages) or not pages
	for index, text in enumerate(pages or []):
		doc.append("pages", {"page_number": index + 1, "text": text if len(text) >= MIN_TEXT else "",
		                     "source": TEXT_LAYER})
	doc.page_count = len(pages or [])
	doc.text_status = OCR_PENDING if pending else TEXT_LAYER
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	attached = frappe.get_doc(
		{"doctype": "File", "file_name": file_name, "is_private": 1, "content": data,
		 "attached_to_doctype": DOCTYPE, "attached_to_name": doc.name}
	).insert(ignore_permissions=True)
	frappe.db.set_value(DOCTYPE, doc.name, "file", attached.get("file_url"))
	return describe(doc.name)


def set_page_text(name: str, page_texts: dict, source: str = "OCR") -> dict:
	"""Post OCR text for pages (`{page_number: text}`); the status follows what is still missing."""
	doc = frappe.get_doc(DOCTYPE, name)
	by_number = {int(row.get("page_number")): row for row in doc.get("pages") or []}
	for raw_number, text in (page_texts or {}).items():
		number = int(raw_number)
		row = by_number.get(number)
		if row is None:
			row = doc.append("pages", {"page_number": number})
			by_number[number] = row
		_set(row, "text", str(text or "").strip())
		_set(row, "source", source)
	doc.page_count = max([doc.page_count or 0, *by_number.keys()]) if by_number else (doc.page_count or 0)
	missing = [n for n in range(1, (doc.page_count or 0) + 1) if len(str((by_number.get(n) or {}).get("text") or "")) < MIN_TEXT]
	doc.text_status = OCR_PENDING if missing else (OCRD if source == "OCR" else TEXT_LAYER)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return describe(name)


def _set(row, field, value) -> None:
	if hasattr(row, "set") and not isinstance(row, dict):
		row.set(field, value)
	else:
		row[field] = value


def describe(name: str, with_pages=None) -> dict:
	doc = frappe.get_doc(DOCTYPE, name)
	row = doc.as_dict()
	out = {
		key: row.get(key)
		for key in ("name", "title", "ref_type", "publisher", "authors", "year", "publication_number", "source_url",
		            "text_status", "page_count", "summary", "key_takeaways", "superseded_by", "added_by", "sha256")
	}
	for field in ("crops", "varieties", "topics", "regions"):
		out[field] = _lines(row.get(field))
	out["copy_note"] = "Internal reference copy — see the source." if row.get("source_url") else "Internal document."
	pending = [int(p.get("page_number")) for p in doc.get("pages") or [] if len(str(p.get("text") or "")) < MIN_TEXT]
	out["pages_pending_ocr"] = pending
	if with_pages:
		wanted = set(with_pages)
		out["pages"] = [{"page": int(p.get("page_number")), "text": p.get("text")} for p in doc.get("pages") or []
		                if int(p.get("page_number")) in wanted]
	out["cited_by"] = cited_by(name)
	return out


def cited_by(name: str) -> list:
	rows = frappe.db.get_all(CITATION, filters={"reference": name}, fields=["parenttype", "parent", "pages"], limit=200)
	return [{"doctype": r.get("parenttype"), "name": r.get("parent"), "pages": r.get("pages")} for r in rows or []]


def _terms(query: str) -> list:
	return [t for t in re.findall(r"[\w'-]+", str(query or "").lower()) if len(t) > 1]


def search(query: str, filters: dict | None = None, limit: int = 20) -> list:
	"""Ranked hits — document, page, snippet. Every term must appear on the page (or in the title/summary)."""
	terms = _terms(query)
	if not terms:
		raise ToolError("query needs at least one word.")
	filters = filters or {}
	doc_filters = {"superseded_by": ("in", ["", None])} if not filters.get("include_superseded") else {}
	if filters.get("ref_type"):
		doc_filters["ref_type"] = filters["ref_type"]
	docs = {r["name"]: r for r in frappe.db.get_all(
		DOCTYPE, filters=doc_filters,
		fields=["name", "title", "publisher", "year", "summary", "key_takeaways", "crops", "topics", "ref_type"], limit=2000)}
	for field in ("crop", "topic"):
		wanted = str(filters.get(field) or "").strip().lower()
		if wanted:
			docs = {k: v for k, v in docs.items() if wanted in [x.lower() for x in _lines(v.get(field + "s"))]}
	if not docs:
		return []
	pages = frappe.db.get_all(PAGE, filters={"parent": ("in", list(docs))}, fields=["parent", "page_number", "text"],
	                          limit=200000)
	hits = []
	for page in pages or []:
		meta = docs.get(page["parent"]) or {}
		text = str(page.get("text") or "")
		haystack = (text + " " + str(meta.get("title") or "")).lower()
		if not all(term in haystack for term in terms):
			continue
		score = sum(text.lower().count(term) for term in terms) + 5 * sum(term in str(meta.get("title") or "").lower() for term in terms)
		hits.append({"reference": page["parent"], "title": meta.get("title"), "publisher": meta.get("publisher"),
		             "year": meta.get("year"), "page": int(page["page_number"]), "score": score,
		             "snippet": _snippet(text, terms)})
	# Title / summary hits for documents whose pages are still awaiting OCR.
	matched = {h["reference"] for h in hits}
	for name, meta in docs.items():
		if name in matched:
			continue
		blob = " ".join(str(meta.get(k) or "") for k in ("title", "summary", "key_takeaways")).lower()
		if all(term in blob for term in terms):
			hits.append({"reference": name, "title": meta.get("title"), "publisher": meta.get("publisher"),
			             "year": meta.get("year"), "page": None, "score": 1, "snippet": str(meta.get("summary") or "")[:SNIPPET]})
	hits.sort(key=lambda h: (-h["score"], str(h["title"] or ""), h["page"] or 0))
	return hits[: max(1, min(int(limit or 20), 100))]


def _snippet(text: str, terms: list) -> str:
	lower = text.lower()
	at = min((lower.find(t) for t in terms if lower.find(t) >= 0), default=0)
	start = max(0, at - SNIPPET // 3)
	piece = " ".join(text[start : start + SNIPPET].split())
	return ("…" if start else "") + piece + ("…" if start + SNIPPET < len(text) else "")
