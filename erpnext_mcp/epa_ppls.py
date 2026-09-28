# SPDX-License-Identifier: MIT
"""EPA's Pesticide Product Label System: the registration, and the label PDF on file.

v0.201.0. Tim, on PROWLER™: "the PDF is not getting attached to the item." The
label a product is used under is the one EPA accepted, and EPA publishes it:
`ordspub.epa.gov/ords/pesticides/cswu/ppls/{reg}` answers the registration —
product name, registrant, signal word, active ingredients with CAS numbers, and
every label PDF it has accepted — and each PDF is at
`www3.epa.gov/pesticides/chem_search/ppls/{pdffile}`.

A DISTRIBUTOR NUMBER HAS THREE PARTS, AND EPA IS ASKED FOR TWO. PROWLER is
12455-97-3240: registration 12455-97 (Bell Laboratories' F-TRAC PLACE PACS)
sold by distributor 3240 (Motomco) under its own name. EPA files the label under
the registration, so 12455-97-3240 answers `{"items": []}` and 12455-97 answers
the product. That is also why the name on the tub is not the name EPA holds, and
why nothing here compares the two.

EVERY FETCH GOES THROUGH `url_fetch`: the public-address check, the pinned
connection, the size cap. EPA's hosts are ordinary public hosts; nothing here
trusts them more than any other link.
"""

from __future__ import annotations

import re

import frappe

from . import url_fetch
from .errors import ToolError

PPLS_API = "https://ordspub.epa.gov/ords/pesticides/cswu/ppls/{registration}"
PPLS_PDF = "https://www3.epa.gov/pesticides/chem_search/ppls/{pdffile}"

_REGISTRATION = re.compile(r"^\s*(\d{1,7})\s*-\s*(\d{1,6})(?:\s*-\s*\d{1,6})?\s*$")
_PDF_DATE = re.compile(r"-(\d{4})(\d{2})(\d{2})\.pdf$", re.IGNORECASE)


def base_registration(number) -> str:
	"""`12455-97` from `12455-97-3240`, `12455-97` or `012455-00097`; "" when it is not one."""
	match = _REGISTRATION.match(str(number or ""))
	if not match:
		return ""
	return f"{int(match.group(1))}-{int(match.group(2))}"


def lookup(number) -> dict:
	"""EPA's record for the registration, summarised; `{}` when EPA has none.

	Raises `ToolError` when EPA could not be asked — a network failure is not the
	same answer as "no such registration", and the caller reports them apart.
	"""
	registration = base_registration(number)
	if not registration:
		return {}
	answer = url_fetch.fetch_json(PPLS_API.format(registration=registration))
	items = answer.get("items") if isinstance(answer, dict) else None
	if not items:
		return {}
	return summarise(items[0], registration)


def summarise(item: dict, registration: str = "") -> dict:
	"""The parts of an EPA product record this app uses, newest label PDF first."""
	company = (item.get("companyinfo") or [{}])[0] or {}
	pdfs = []
	for entry in item.get("pdffiles") or []:
		name = str(entry.get("pdffile") or "").strip()
		if not name:
			continue
		match = _PDF_DATE.search(name)
		pdfs.append(
			{
				"pdffile": name,
				"date": f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else "",
				"accepted": entry.get("pdffile_accepted_date") or None,
			}
		)
	pdfs.sort(key=lambda row: row["date"], reverse=True)
	return {
		"registration": str(item.get("eparegno") or registration),
		"product_name": item.get("productname") or None,
		"registrant": company.get("name") or None,
		"status": item.get("product_status") or None,
		"restricted_use": str(item.get("rup_yn") or "").lower() == "yes",
		"signal_word": _signal_word(item.get("signal_word")),
		"active_ingredients": [
			{
				"name": row.get("active_ing"),
				"cas": row.get("cas_number") or None,
				"percent": row.get("active_ing_percent"),
			}
			for row in item.get("active_ingredients") or []
			if row.get("active_ing")
		],
		"pdf_files": pdfs,
	}


def _signal_word(raw) -> str | None:
	word = str(raw or "").strip().title()
	return word if word in ("Danger", "Warning", "Caution") else None


def attach_label_pdf(item_code: str, record: dict) -> dict:
	"""Download EPA's newest accepted label and file it on the Item. Returns the File card.

	A PDF of the same name already on this Item is the answer, not a second copy —
	registering the same product twice must not stack two identical labels.
	"""
	from .tools import files as file_tools

	pdfs = record.get("pdf_files") or []
	if not pdfs:
		raise ToolError(f"EPA holds no label PDF for {record.get('registration')}.")
	newest = pdfs[0]
	file_name = (
		f"EPA label {record.get('registration')}"
		+ (f" ({newest['date']})" if newest["date"] else "")
		+ ".pdf"
	)
	existing = frappe.db.get_value(
		"File",
		{"attached_to_doctype": "Item", "attached_to_name": item_code, "file_name": file_name},
		["name", "file_name", "file_url"],
		as_dict=True,
	)
	if existing:
		return {**dict(existing), "already_attached": True}
	fetched = url_fetch.fetch(PPLS_PDF.format(pdffile=newest["pdffile"]))
	if fetched.extension != "pdf":
		raise ToolError(f"EPA answered {newest['pdffile']} with a {fetched.extension}, not a PDF.")
	handle = file_tools.insert_attachment(
		file_name, fetched.content, is_private=True, doctype="Item", name=item_code
	)
	return {
		"name": handle.name,
		"file_name": handle.get("file_name") or file_name,
		"file_url": handle.get("file_url"),
		"already_attached": False,
	}


def label_for_item(item_code: str, number, *, attach: bool = True) -> dict:
	"""The `epa_label` block of `register_product_label`'s answer. Never raises."""
	registration = base_registration(number)
	answer = {
		"status": "skipped",
		"registration": registration or None,
		"product_name": None,
		"registrant": None,
		"signal_word": None,
		"active_ingredients": [],
		"pdf": None,
		"reason": None,
		"record": None,
	}
	if not registration:
		answer["reason"] = "No EPA registration number was read or recorded, so EPA was not asked."
		return answer
	try:
		record = lookup(registration)
	except ToolError as exc:
		answer.update(status="failed", reason=f"EPA could not be reached: {exc}")
		return answer
	if not record:
		answer.update(status="not_found", reason=f"EPA has no product registered as {registration}.")
		return answer
	answer.update(
		product_name=record["product_name"],
		registrant=record["registrant"],
		signal_word=record["signal_word"],
		active_ingredients=record["active_ingredients"],
		record=record,
		status="found",
	)
	if not attach:
		return answer
	try:
		card = attach_label_pdf(item_code, record)
	except ToolError as exc:
		answer.update(status="failed", reason=f"The label PDF could not be attached: {exc}")
		return answer
	answer.update(
		status="attached",
		pdf={"file": card["name"], "file_name": card["file_name"], "file_url": card.get("file_url")},
	)
	return answer
