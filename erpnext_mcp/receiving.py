# SPDX-License-Identifier: MIT
"""Chemical receiving — a supplier's delivery, from the document to a DRAFT Purchase Receipt. v0.272.0
(docs/contracts/chemical_receiving_v0_272.yaml).

The driver does almost nothing: taps Delivered on the job link and photographs the ticket. The LINES come from
documents, in this order of preference, because no major ag-chemical supplier offers a grower an invoice API
(researched 2026-10: My Wilbur-Ellis, Nutrien Digital Hub, CHS Connect and Helena Agri Hub are portals; Agvance's
REST API is the retailer's back office; AgGateway EDI / agXML carry Invoice and Ship Notice between businesses):

  1. the ticket photo (read on the phone — Apple Vision + Foundation Models — and posted as lines);
  2. the emailed invoice / packing slip PDF arriving at office@ (text out of the PDF, lines by the connector's pattern);
  3. the supplier portal's CSV export (columns named in the connector);
  4. an API — `rest_api`, `edi`, `agxml` connectors are accepted as configuration and answer "not available yet".

A connector is CONFIGURATION ("Supplier Connector", one per supplier and channel): who it is, how to recognise its
mail, which CSV column is which, the line pattern for its PDFs, and its item numbers → our Items. The adapter
interface is `fetch(since)` and `to_lines(payload)`; adding a channel is one class, adding a supplier is a config.

Then the same steps whatever the source: match each line to an Item and a PO line, reconcile (ok / short / over /
substitution / not ordered / unmatched / unit to check), a person resolves what is not ok, and a DRAFT Purchase
Receipt goes into the company's chemical storage warehouse with one Batch per lot. Nobody's stock moves until a
person submits it. A check-in task (count, lots, labels, SDS, storage, RUP) is raised for the crew; restricted-use
products on it reuse the Applicator License gate, so only a licensed person can claim it.

OFF per company until `chemical_receiving_enabled` is on — Constancy Farms stays inactive until its go-live.
"""

from __future__ import annotations

import csv
import io
import json
import re

import frappe

from . import compat

INTAKE = "Supplier Delivery Intake"
LINE = "Supplier Delivery Intake Line"
KIND = "Supplier Connector"
FLAG = "chemical_receiving_enabled"
WAREHOUSE_FLAG = "chemical_storage_warehouse"
TOLERANCE_FLAG = "receiving_qty_tolerance_pct"
PO, PO_ITEM, PR, ITEM, BATCH = "Purchase Order", "Purchase Order Item", "Purchase Receipt", "Item", "Batch"
CHECK_IN = "Chemical delivery check-in"
CHANNELS = ("email_pdf", "portal_csv", "rest_api", "edi", "agxml")
SOURCES = ("ticket_photo", "email_pdf", "portal_csv", "rest_api", "edi", "agxml", "phone_extraction", "manual")
OK, SHORT, OVER, SUB, NOT_ORDERED, UNMATCHED, UOM_CHECK = (
	"ok", "short", "over", "substitution", "not_ordered", "unmatched", "uom_check")
NEEDS_A_PERSON = (SHORT, OVER, SUB, NOT_ORDERED, UNMATCHED, UOM_CHECK)
CSV_FIELDS = ("item", "sku", "qty", "uom", "lot", "expiry", "epa", "document_no", "po_number", "date")
MAX_LINES = 500
MAX_CSV_BYTES = 2 * 1024 * 1024
EPA = re.compile(r"^\d{1,7}-\d{1,6}(-\d{1,7})?$")
SDS = re.compile(r"(^|[^a-z])sds([^a-z]|$)|safety[ _-]?data[ _-]?sheet", re.I)
#: Label storage wording that means "keep it from freezing" — read off the Item's `storage_disposal`.
FREEZE = re.compile(r"do not (allow (it |product )?to )?freeze|protect(ed)? from freez|avoid freez|keep from freez|"
                    r"store (above|at or above) \d{2}\s*°?\s*f|freezing (will|may) (damage|ruin)", re.I)
#: The same unit, spelled the ways tickets spell it.
UNITS = {"gal": ("gal", "gals", "gallon", "gallons", "ga"), "lb": ("lb", "lbs", "pound", "pounds", "#"),
         "qt": ("qt", "qts", "quart", "quarts"), "pt": ("pt", "pint", "pints"), "oz": ("oz", "fl oz", "floz", "ounce"),
         "case": ("cs", "case", "cases", "cse"), "each": ("ea", "each", "unit", "units", "nos", "pc", "pcs"),
         "jug": ("jug", "jugs"), "bag": ("bag", "bags", "bg"), "tote": ("tote", "totes"), "drum": ("drum", "drums"),
         "kg": ("kg", "kgs"), "l": ("l", "lt", "liter", "litre", "liters")}
_UNIT = {alias: unit for unit, names in UNITS.items() for alias in names}


class ReceivingError(Exception):
	pass


def _put(row, key: str, value) -> None:
	"""Set a child-row value whether the row is a Document or (after a reload) a plain dict."""
	if isinstance(row, dict):
		row[key] = value
	else:
		setattr(row, key, value)


# ── switches and settings (per company, as flags) ────────────────────────────
def enabled(company: str) -> bool:
	from . import flags

	return bool(company) and bool(flags.value(FLAG, company=company, default=False))


def storage_warehouse(company: str) -> str:
	from . import flags

	return str(flags.value(WAREHOUSE_FLAG, company=company, default="") or "").strip()


def tolerance_pct(company: str) -> float:
	from . import flags

	try:
		return max(0.0, float(flags.value(TOLERANCE_FLAG, company=company, default=0) or 0))
	except (TypeError, ValueError):
		return 0.0


def _require_on(company: str) -> None:
	if not enabled(company):
		raise ReceivingError(f"chemical receiving is off for {company} — turn on `{FLAG}` for it (Farm Flag) when "
		                     "it goes live.")


# ── connectors (configuration) ───────────────────────────────────────────────
def validate(body: dict, *, key: str = "", for_publish: bool = False) -> dict:
	errors, warnings = [], []
	channel = body.get("channel")
	if channel not in CHANNELS:
		errors.append(f"channel is one of {', '.join(CHANNELS)}")
	if not str(body.get("supplier") or "").strip():
		errors.append("supplier: the Supplier this connector reads for")
	elif for_publish and compat.doctype_exists("Supplier") and not frappe.db.exists("Supplier", body["supplier"]):
		errors.append(f"supplier: no Supplier {body['supplier']!r}")
	if channel == "portal_csv":
		columns = (body.get("csv") or {}).get("columns") or {}
		unknown = [k for k in columns if k not in CSV_FIELDS]
		if unknown:
			errors.append(f"csv.columns: unknown {', '.join(unknown)} (use {', '.join(CSV_FIELDS)})")
		if not columns.get("qty") or not (columns.get("item") or columns.get("sku")):
			errors.append("csv.columns needs qty and item or sku")
	if channel == "email_pdf":
		pdf = body.get("pdf") or {}
		for name in ("line_pattern", "document_no_pattern", "po_pattern", "date_pattern"):
			if pdf.get(name):
				try:
					compiled = re.compile(pdf[name], re.I | re.M)
				except re.error as exc:
					errors.append(f"pdf.{name}: {exc}")
					continue
				groups = set(compiled.groupindex)
				if name == "line_pattern" and ("qty" not in groups or not {"item", "sku"} & groups):
					errors.append("pdf.line_pattern needs named groups qty and item or sku")
		if not pdf.get("line_pattern"):
			errors.append("pdf.line_pattern: how one line reads on this supplier's invoice")
		if not ((body.get("match") or {}).get("senders")):
			warnings.append("match.senders is empty — email from this supplier will not be recognised")
	if channel in ("rest_api", "edi", "agxml"):
		warnings.append(f"{channel} is accepted as configuration but nothing fetches it yet")
	item_map = body.get("item_map") or {}
	if not isinstance(item_map, dict):
		errors.append("item_map: {supplier item number: our Item}")
	elif for_publish and compat.doctype_exists(ITEM):
		missing = [code for code in item_map.values() if not frappe.db.exists(ITEM, code)]
		if missing:
			errors.append(f"item_map: no Item {', '.join(missing[:5])}")
	return {"errors": errors, "warnings": warnings}


#: Drafts only — a person checks the columns against a real export / invoice and publishes.
SEED_CONNECTORS = {
	"wilbur_ellis_portal_csv": {
		"supplier": "Wilbur-Ellis", "channel": "portal_csv",
		"note": "My Wilbur-Ellis → Invoices / Shipments → Export. Check the column names against a real export.",
		"csv": {"columns": {"item": "Product Description", "sku": "Product #", "qty": "Quantity", "uom": "UOM",
		                    "lot": "Lot Number", "epa": "EPA Reg #", "document_no": "Invoice #", "po_number": "PO #",
		                    "date": "Ship Date"}},
		"item_map": {},
	},
	"nutrien_portal_csv": {
		"supplier": "Nutrien Ag Solutions", "channel": "portal_csv",
		"note": "Nutrien Ag Solutions Digital Hub → Invoices → Download CSV. Check the column names against a real export.",
		"csv": {"columns": {"item": "Product", "sku": "Product Code", "qty": "Quantity", "uom": "Unit",
		                    "lot": "Lot", "document_no": "Invoice Number", "po_number": "Customer PO", "date": "Invoice Date"}},
		"item_map": {},
	},
	"wilbur_ellis_email_pdf": {
		"supplier": "Wilbur-Ellis", "channel": "email_pdf",
		"note": "Invoices / delivery tickets emailed to office@. Set the sender and check the line pattern on a real PDF.",
		"match": {"senders": ["wilburellis.com"]},
		"pdf": {"line_pattern": r"^(?P<sku>\d{4,10})\s+(?P<item>.+?)\s+(?P<qty>\d+(?:\.\d+)?)\s+(?P<uom>[A-Za-z]{1,6})"
		                        r"(?:\s+LOT[:# ]*(?P<lot>[A-Z0-9-]+))?(?:\s+EPA[:# ]*(?P<epa>\d+-\d+(?:-\d+)?))?\s*$",
		        "document_no_pattern": r"(?:Invoice|Ticket)\s*(?:No\.?|#|Number)?[: ]*([A-Z0-9-]{4,})",
		        "po_pattern": r"(?:PO|P\.O\.|Purchase Order)\s*(?:No\.?|#|Number)?[: ]*([A-Z0-9-]{3,})",
		        "date_pattern": r"(?:Date|Ship Date)[: ]*(\d{1,2}/\d{1,2}/\d{2,4})"},
		"item_map": {},
	},
	"agvance_rest_api": {
		"supplier": "Wilbur-Ellis", "channel": "rest_api",
		"note": "Agvance's API belongs to the retailer's back office: only usable if the retailer grants access.",
		"item_map": {},
	},
}


def seed() -> dict:
	from . import phone_config

	made = []
	for key, body in SEED_CONNECTORS.items():
		if phone_config.rows(KIND, key):
			continue
		phone_config.save_draft(KIND, key, body, "Seeded draft — check it against a real export / invoice, set "
		                        "the Supplier name to yours, then publish.", "System")
		made.append(key)
	return {"drafts": made}


def connectors(supplier: str = "", channel: str = "", published_only: bool = True) -> list:
	from . import phone_config

	out = []
	for row in phone_config.rows(KIND, status=phone_config.PUBLISHED if published_only else None):
		doc = frappe.get_doc(phone_config.DOCTYPE, row["name"])
		body = phone_config.body_of(doc)
		if (supplier and body.get("supplier") != supplier) or (channel and body.get("channel") != channel):
			continue
		if any(c["key"] == row["config_key"] for c in out):
			continue
		out.append({"key": row["config_key"], "status": row.get("status"), "version": row.get("version"), **body})
	return out


def connector(key: str) -> dict:
	from . import phone_config

	doc = phone_config.doc_of(KIND, key, status=phone_config.PUBLISHED)
	if doc is None:
		raise ReceivingError(f"no published Supplier Connector {key!r} (seeded ones are drafts — publish in the Desk).")
	return {"key": key, **phone_config.body_of(doc)}


class Adapter:
	"""One channel. `fetch(since)` finds new documents; `to_lines(payload)` turns one into a header and lines."""

	channel = ""

	def __init__(self, body: dict):
		self.body = body

	def fetch(self, since: str = "") -> list:
		raise ReceivingError(f"{self.channel} connectors are configuration only for now — nothing fetches them yet. "
		                     "Upload the portal's CSV or let the emailed invoice arrive at office@.")

	def to_lines(self, payload) -> dict:
		raise ReceivingError(f"{self.channel} documents are not read yet.")


class PortalCSV(Adapter):
	"""The portal's export, uploaded by a person (portals have no grower API to fetch from)."""

	channel = "portal_csv"

	def to_lines(self, payload) -> list:
		text = payload.decode("utf-8-sig", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload or "")
		if len(text.encode()) > MAX_CSV_BYTES:
			raise ReceivingError("the CSV is over 2 MB.")
		spec = self.body.get("csv") or {}
		columns = spec.get("columns") or {}
		reader = csv.DictReader(io.StringIO(text), delimiter=spec.get("delimiter") or ",")
		headers = {str(h or "").strip().casefold(): h for h in reader.fieldnames or []}
		missing = [columns[k] for k in ("qty",) if columns.get(k) and columns[k].strip().casefold() not in headers]
		if missing:
			raise ReceivingError(f"the CSV has no column {missing[0]!r} — check the connector's csv.columns.")
		documents: dict = {}
		for number, row in enumerate(reader, start=2):
			got = {k: str(row.get(headers.get(str(v).strip().casefold(), ""), "") or "").strip()
			       for k, v in columns.items()}
			if not any(got.get(k) for k in ("item", "sku", "qty")):
				continue
			key = got.get("document_no") or "(no number)"
			doc = documents.setdefault(key, {"document_no": got.get("document_no") or "",
			                                 "po_number": got.get("po_number") or "", "date": got.get("date") or "",
			                                 "lines": []})
			doc["lines"].append({"line_text": f"row {number}: " + ", ".join(f"{k}={v}" for k, v in got.items() if v),
			                     "item_text": got.get("item"), "sku": got.get("sku"), "qty": got.get("qty"),
			                     "uom": got.get("uom"), "lot_no": got.get("lot"), "expiry_date": got.get("expiry"),
			                     "epa_reg_number": got.get("epa")})
		return list(documents.values())


class EmailPDF(Adapter):
	"""An invoice / delivery ticket emailed to office@: the PDF's text, read line by line with the connector's pattern."""

	channel = "email_pdf"

	def senders(self) -> list:
		return [str(s).strip().casefold() for s in (self.body.get("match") or {}).get("senders") or [] if str(s).strip()]

	def recognises(self, sender: str) -> bool:
		sender = str(sender or "").casefold()
		return any(sender == s or sender.endswith("@" + s) or sender.endswith("." + s) for s in self.senders())

	def fetch(self, since: str = "") -> list:
		"""Received email from this supplier since `since` with a PDF attached: [{communication, file}]."""
		if not compat.doctype_exists("Communication"):
			return []
		filters = {"sent_or_received": "Received", "communication_type": "Communication"}
		if since:
			filters["communication_date"] = (">=", since)
		rows = frappe.db.get_all("Communication", filters=filters, fields=["name", "sender"], limit=500)
		out = []
		for row in rows or []:
			if not self.recognises(_address(row.get("sender"))):
				continue
			for f in frappe.db.get_all("File", filters={"attached_to_doctype": "Communication",
			                                            "attached_to_name": row["name"]},
			                           fields=["name", "file_name"], limit=20) or []:
				if str(f.get("file_name") or "").lower().endswith(".pdf"):
					out.append({"communication": row["name"], "file": f["name"]})
		return out

	def to_lines(self, payload) -> list:
		text = pdf_text(payload) if isinstance(payload, (bytes, bytearray)) else str(payload or "")
		spec = self.body.get("pdf") or {}
		pattern = re.compile(spec.get("line_pattern") or r"$^", re.I | re.M)

		def first(name):
			if not spec.get(name):
				return ""
			hit = re.search(spec[name], text, re.I | re.M)
			return (hit.group(1) if hit and hit.groups() else "") or ""

		lines = []
		for hit in pattern.finditer(text):
			got = {k: (v or "").strip() for k, v in hit.groupdict().items()}
			lines.append({"line_text": hit.group(0).strip()[:300], "item_text": got.get("item"), "sku": got.get("sku"),
			              "qty": got.get("qty"), "uom": got.get("uom"), "lot_no": got.get("lot"),
			              "expiry_date": got.get("expiry"), "epa_reg_number": got.get("epa")})
		return [{"document_no": first("document_no_pattern"), "po_number": first("po_pattern"),
		         "date": first("date_pattern"), "lines": lines, "text_chars": len(text)}]


ADAPTERS = {"portal_csv": PortalCSV, "email_pdf": EmailPDF, "rest_api": Adapter, "edi": Adapter, "agxml": Adapter}


def adapter(body: dict) -> Adapter:
	cls = ADAPTERS.get(body.get("channel"), Adapter)
	made = cls(body)
	made.channel = made.channel or str(body.get("channel") or "")
	return made


def pdf_text(content: bytes) -> str:
	try:
		from pypdf import PdfReader
	except ImportError:
		raise ReceivingError("this server cannot read PDF text (pypdf is not installed) — upload the portal CSV, or "
		                     "type the lines.") from None
	try:
		reader = PdfReader(io.BytesIO(bytes(content)))
		return "\n".join((page.extract_text() or "") for page in reader.pages[:20])
	except Exception as exc:  # a damaged PDF is a document problem, not a crash
		raise ReceivingError(f"the PDF could not be read ({type(exc).__name__}).") from None


def _address(sender: str) -> str:
	hit = re.search(r"<([^>]+)>", str(sender or ""))
	return (hit.group(1) if hit else str(sender or "")).strip().casefold()


# ── the intake ───────────────────────────────────────────────────────────────
def open_intake(company: str, *, supplier: str = "", purchase_order: str = "", job: str = "", source: str = "manual",
                connector_key: str = "", direction: str = "Delivery", return_against: str = "", source_file: str = "",
                communication: str = "", notes: str = "") -> str:
	_require_on(company)
	if source not in SOURCES:
		raise ReceivingError(f"source is one of {', '.join(SOURCES)}")
	if direction not in ("Delivery", "Return"):
		raise ReceivingError("direction is Delivery or Return")
	if purchase_order:
		row = frappe.db.get_value(PO, purchase_order, ["company", "supplier", "docstatus"], as_dict=True)
		if not row:
			raise ReceivingError(f"no Purchase Order {purchase_order!r}.")
		if row.get("company") and row["company"] != company:
			raise ReceivingError(f"{purchase_order} is {row['company']}'s order, not {company}'s.")
		supplier = supplier or row.get("supplier") or ""
	if direction == "Return":
		if not return_against or not frappe.db.exists(PR, return_against):
			raise ReceivingError("a return names the Purchase Receipt it goes back against (return_against).")
		supplier = supplier or frappe.db.get_value(PR, return_against, "supplier") or ""
	doc = frappe.new_doc(INTAKE)
	doc.company = company
	doc.supplier = supplier or None
	doc.purchase_order = purchase_order or None
	doc.job = job or None
	doc.direction = direction
	doc.return_against = return_against or None
	doc.source = source
	doc.connector = connector_key or None
	doc.status = "Awaiting Lines"
	doc.source_file = source_file or None
	doc.communication = communication or None
	doc.notes = notes or None
	doc.insert(ignore_permissions=True)
	return doc.name


def _number(raw, label: str) -> float:
	text = str(raw if raw is not None else "").replace(",", "").strip()
	try:
		return float(text)
	except ValueError:
		raise ReceivingError(f"{label}: {raw!r} is not a number") from None


def _date(raw) -> str | None:
	text = str(raw or "").strip()
	if not text:
		return None
	for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y"):
		try:
			import datetime

			return datetime.datetime.strptime(text, fmt).date().isoformat()
		except ValueError:
			continue
	return None


def unit(raw) -> str:
	text = str(raw or "").strip().casefold().rstrip(".")
	return _UNIT.get(text, text)


def check_lines(lines: list) -> dict:
	"""What a document's lines have to survive before anybody matches them. Problems, never a crash."""
	problems, warnings = [], []
	for index, line in enumerate(lines, start=1):
		try:
			qty = _number(line.get("qty"), f"line {index} qty")
			if qty <= 0:
				problems.append(f"line {index}: quantity {qty:g} — must be more than 0")
		except ReceivingError as exc:
			problems.append(str(exc))
		epa = str(line.get("epa_reg_number") or "").strip()
		if epa and not EPA.match(epa):
			problems.append(f"line {index}: EPA number {epa!r} is not the shape of one (e.g. 524-537)")
		if not (line.get("item_text") or line.get("sku") or line.get("item_code")):
			problems.append(f"line {index}: no product")
		if line.get("expiry_date") and not _date(line.get("expiry_date")):
			warnings.append(f"line {index}: expiry {line.get('expiry_date')!r} is not a date")
	return {"problems": problems, "warnings": warnings}


def set_lines(intake: str, lines: list, *, document_no: str = "", document_date: str = "", po_number: str = "",
              source: str = "", actor: str = "") -> dict:
	"""Replace an intake's lines (from a document, the phone or a person), then match and reconcile."""
	doc = frappe.get_doc(INTAKE, intake)
	if doc.status in ("Receipt Drafted", "Return Drafted", "Rejected"):
		raise ReceivingError(f"{intake} is {doc.status} — its lines are settled.")
	if not isinstance(lines, list) or not lines:
		raise ReceivingError("lines: at least one line")
	if len(lines) > MAX_LINES:
		raise ReceivingError(f"lines: at most {MAX_LINES}")
	checked = check_lines(lines)
	if checked["problems"]:
		raise ReceivingError("; ".join(checked["problems"][:8]))
	doc.set("lines", [])
	for line in lines:
		doc.append("lines", {
			"line_text": str(line.get("line_text") or line.get("item_text") or line.get("sku") or "")[:500],
			"sku": str(line.get("sku") or "").strip()[:80] or None,
			"item_code": str(line.get("item_code") or "").strip() or None,
			"qty": _number(line.get("qty"), "qty"),
			"uom": str(line.get("uom") or "").strip()[:20] or None,
			"lot_no": str(line.get("lot_no") or line.get("lot") or "").strip()[:80] or None,
			"expiry_date": _date(line.get("expiry_date")),
			"epa_reg_number": str(line.get("epa_reg_number") or "").strip()[:30] or None,
		})
	if document_no:
		doc.document_no = str(document_no)[:80]
	if document_date:
		doc.document_date = _date(document_date)
	if po_number:
		doc.po_number_read = str(po_number)[:80]
	if source in SOURCES:
		doc.source = source
	_match(doc, item_text={i + 1: str(line.get("item_text") or "") for i, line in enumerate(lines)})
	doc.validation = json.dumps({"warnings": checked["warnings"], "by": actor or None})
	doc.save(ignore_permissions=True)
	return summary(doc)


# ── matching and reconciling ─────────────────────────────────────────────────
def _norm(text) -> str:
	return re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()


def _po_rows(purchase_order: str) -> list:
	if not purchase_order or not frappe.db.exists(PO, purchase_order):
		return []
	rows = []
	for index, item in enumerate(frappe.get_doc(PO, purchase_order).get("items") or [], start=1):
		row = {k: item.get(k) for k in ("name", "item_code", "item_name", "qty", "received_qty", "uom", "rate",
		                                "warehouse")}
		row["name"] = row["name"] or f"{purchase_order}-{index}"
		row["open"] = max(0.0, float(row.get("qty") or 0) - float(row.get("received_qty") or 0))
		rows.append(row)
	return rows


def _item_facts(item_code: str) -> dict:
	if not item_code:
		return {}
	fields = compat.existing_fields(ITEM, ("name", "item_name", "stock_uom", "has_batch_no", "restricted_use",
	                                       "epa_registration_number", "storage_disposal"))
	return dict(frappe.db.get_value(ITEM, item_code, fields, as_dict=True) or {})


def _by_epa(epa: str) -> list:
	if not epa or not compat.has_field(ITEM, "epa_registration_number"):
		return []
	return [r["name"] for r in frappe.db.get_all(ITEM, filters={"epa_registration_number": epa}, fields=["name"],
	                                             limit=20) or []]


def _resolve(line, text: str, item_map: dict, po_rows: list) -> tuple:
	"""(item_code, how) — connector map, exact Item, EPA number (an Item on the PO first), then PO line name."""
	if line.get("item_code") and frappe.db.exists(ITEM, line.get("item_code")):
		return line.get("item_code"), "given"
	sku = str(line.get("sku") or "").strip()
	if sku and item_map.get(sku):
		return item_map[sku], "connector item_map"
	for candidate in (sku, text):
		if candidate and frappe.db.exists(ITEM, candidate):
			return candidate, "item code"
	epa = str(line.get("epa_reg_number") or "").strip()
	hits = _by_epa(epa)
	on_po = [h for h in hits if any(r.get("item_code") == h for r in po_rows)]
	if on_po or hits:
		return (on_po or hits)[0], "EPA number"
	words = _norm(text)
	if words:
		for row in po_rows:
			name = _norm(row.get("item_name") or row.get("item_code"))
			if name and (name in words or words in name):
				return row.get("item_code"), "name on the PO"
	return None, ""


def _match(doc, item_text: dict | None = None) -> None:
	"""Match every line, allocate PO quantities in order, flag what a person must look at."""
	item_map = {}
	if doc.connector:
		try:
			item_map = connector(doc.connector).get("item_map") or {}
		except ReceivingError:
			item_map = {}
	po_rows = _po_rows(doc.purchase_order)
	left = {r["name"]: r["open"] for r in po_rows}
	tolerance = tolerance_pct(doc.company) / 100.0
	hit_rows = set()
	for index, line in enumerate(doc.get("lines") or [], start=1):
		text = (item_text or {}).get(index) or line.get("line_text") or ""
		item, how = _resolve(line, text, item_map, po_rows)
		facts = _item_facts(item)
		_put(line, "item_code", item or None)
		_put(line, "match_by", how or None)
		_put(line, "restricted_use", 1 if compat.checked(facts.get("restricted_use")) else 0)
		_put(line, "freeze_sensitive", 1 if FREEZE.search(str(facts.get("storage_disposal") or "")) else 0)
		_put(line, "sds_on_file", 1 if item and has_sds(item) else 0)
		for key, value in (("po_item", None), ("po_qty_open", 0), ("delta", 0)):
			_put(line, key, value)
		if not item:
			_put(line, "match", UNMATCHED)
			continue
		if doc.direction == "Return" or not doc.purchase_order:
			_put(line, "match", OK)
			continue
		row = next((r for r in po_rows if r.get("item_code") == item and left[r["name"]] > 0), None) or next(
			(r for r in po_rows if r.get("item_code") == item), None)
		if row is None:
			epa = str(line.get("epa_reg_number") or facts.get("epa_registration_number") or "")
			twin = next((r for r in po_rows if epa and epa == str(_item_facts(r.get("item_code")).get(
				"epa_registration_number") or "")), None)
			if twin is not None:
				_put(line, "po_item", twin["name"])
				_put(line, "po_qty_open", left[twin["name"]])
				_put(line, "match", SUB)
				hit_rows.add(twin["name"])
				left[twin["name"]] = max(0.0, left[twin["name"]] - float(line.get("qty") or 0))
			else:
				_put(line, "match", NOT_ORDERED)
			continue
		hit_rows.add(row["name"])
		open_qty = left[row["name"]]
		_put(line, "po_item", row["name"])
		_put(line, "po_qty_open", open_qty)
		qty = float(line.get("qty") or 0)
		if line.get("uom") and unit(line.get("uom")) != unit(row.get("uom") or facts.get("stock_uom")):
			_put(line, "match", UOM_CHECK)
			left[row["name"]] = max(0.0, open_qty - qty)
			continue
		delta = round(qty - open_qty, 6)
		_put(line, "delta", delta)
		slack = open_qty * tolerance
		_put(line, "match", OK if abs(delta) <= slack + 1e-9 else (SHORT if delta < 0 else OVER))
		left[row["name"]] = max(0.0, open_qty - qty)
	doc.missing_lines = json.dumps([
		{"po_item": r["name"], "item_code": r.get("item_code"), "item_name": r.get("item_name"), "open": r["open"]}
		for r in po_rows if r["open"] > 0 and r["name"] not in hit_rows])
	_status(doc)


def _pending(line) -> bool:
	if line.get("resolution") == "Reject":
		return False
	if line.get("match") in (UNMATCHED,) or not line.get("item_code"):
		return True
	return line.get("match") in NEEDS_A_PERSON and line.get("resolution") != "Accept"


def _status(doc) -> None:
	if doc.status in ("Receipt Drafted", "Return Drafted", "Rejected"):
		return
	lines = doc.get("lines") or []
	if not lines:
		doc.status = "Awaiting Lines"
	elif any(_pending(line) for line in lines):
		doc.status = "Needs Review"
	else:
		doc.status = "Matched"


def resolve_line(intake: str, idx: int, *, resolution: str = "", item_code: str = "", uom: str = "", qty=None,
                 note: str = "", actor: str = "") -> dict:
	"""A person settles one line: the right Item / unit / qty, then Accept (receive as delivered) or Reject."""
	doc = frappe.get_doc(INTAKE, intake)
	if doc.status in ("Receipt Drafted", "Return Drafted", "Rejected"):
		raise ReceivingError(f"{intake} is {doc.status}.")
	lines = doc.get("lines") or []
	if not 1 <= int(idx) <= len(lines):
		raise ReceivingError(f"line {idx}: {intake} has {len(lines)} line(s).")
	line = lines[int(idx) - 1]
	changed = False
	if item_code:
		if not frappe.db.exists(ITEM, item_code):
			raise ReceivingError(f"no Item {item_code!r}.")
		_put(line, "item_code", item_code)
		changed = True
	if uom:
		_put(line, "uom", uom)
		changed = True
	if qty not in (None, ""):
		value = _number(qty, "qty")
		if value <= 0:
			raise ReceivingError("qty must be more than 0")
		_put(line, "qty", value)
		changed = True
	if changed:
		_put(line, "resolution", None)
		_match(doc)
		line = (doc.get("lines") or [])[int(idx) - 1]
	if resolution:
		if resolution not in ("Accept", "Reject"):
			raise ReceivingError("resolution is Accept or Reject")
		if resolution == "Accept" and not line.get("item_code"):
			raise ReceivingError(f"line {idx} has no Item yet — name the Item, then accept it.")
		_put(line, "resolution", resolution)
		_put(line, "resolution_note", (str(note or "") + (f" — {actor}" if actor else ""))[:140] or None)
	_status(doc)
	doc.save(ignore_permissions=True)
	return summary(doc)


# ── SDS ──────────────────────────────────────────────────────────────────────
def has_sds(item_code: str) -> bool:
	"""A Safety Data Sheet on file: a file on the Item named SDS / Safety Data Sheet."""
	if not compat.doctype_exists("File"):
		return False
	rows = frappe.db.get_all("File", filters={"attached_to_doctype": ITEM, "attached_to_name": item_code},
	                         fields=["file_name"], limit=200) or []
	return any(SDS.search(str(r.get("file_name") or "")) for r in rows)


# ── the draft receipt ────────────────────────────────────────────────────────
def _batch(item_code: str, lot: str, *, expiry: str = "", supplier: str = "", intake: str = "") -> tuple:
	"""(batch name, note). One Batch per Item + lot; a lot number another Item already uses gets the Item prefixed."""
	if not compat.doctype_exists(BATCH):
		return None, "this site has no Batch register — the lot is kept on the intake"
	if not compat.checked(frappe.db.get_value(ITEM, item_code, "has_batch_no")):
		return None, f"{item_code} does not track batches (tick Has Batch No to keep lots in stock) — lot kept on the intake"
	for candidate in (lot, f"{item_code}-{lot}"):
		existing = frappe.db.get_value(BATCH, candidate, "item")
		if existing == item_code:
			return candidate, ""
		if existing:
			continue
		doc = frappe.new_doc(BATCH)
		doc.batch_id = candidate
		doc.name = candidate
		doc.item = item_code
		if compat.has_field(BATCH, "supplier"):
			doc.supplier = supplier or None
		if expiry and compat.has_field(BATCH, "expiry_date"):
			doc.expiry_date = expiry
		if compat.has_field(BATCH, "reference_doctype"):
			doc.reference_doctype, doc.reference_name = INTAKE, intake
		doc.insert(ignore_permissions=True)
		return doc.name, ""
	return None, f"lot {lot} could not be given a Batch"


def draft_receipt(intake: str, actor: str = "") -> dict:
	"""The DRAFT Purchase Receipt (or return) from a settled intake, then the check-in task. Never submits."""
	doc = frappe.get_doc(INTAKE, intake)
	_require_on(doc.company)
	if doc.purchase_receipt:
		return {"intake": intake, "purchase_receipt": doc.purchase_receipt, "already": True,
		        "check_in_task": doc.check_in_task}
	if doc.status != "Matched":
		pending = [str(i) for i, line in enumerate(doc.get("lines") or [], start=1) if _pending(line)]
		raise ReceivingError(f"{intake} is {doc.status}" + (f" — settle line(s) {', '.join(pending)} first."
		                                                    if pending else "."))
	if not doc.supplier:
		raise ReceivingError(f"{intake} has no supplier.")
	warehouse = storage_warehouse(doc.company)
	if not warehouse:
		raise ReceivingError(f"no chemical storage warehouse for {doc.company} — set `{WAREHOUSE_FLAG}` (Farm Flag, "
		                     "text) to the warehouse the shed is.")
	if not frappe.db.exists("Warehouse", warehouse):
		raise ReceivingError(f"no Warehouse {warehouse!r}.")
	owner = frappe.db.get_value("Warehouse", warehouse, "company")
	if owner and owner != doc.company:
		raise ReceivingError(f"{warehouse} is {owner}'s warehouse, not {doc.company}'s.")
	if doc.purchase_order and int(frappe.db.get_value(PO, doc.purchase_order, "docstatus") or 0) != 1:
		raise ReceivingError(f"{doc.purchase_order} is not submitted — a receipt goes against a submitted order.")
	rows = {r["name"]: r for r in _po_rows(doc.purchase_order)}
	returning = doc.direction == "Return"
	pr = frappe.new_doc(PR)
	pr.company = doc.company
	pr.supplier = doc.supplier
	pr.posting_date = frappe.utils.today()
	if doc.purchase_order:
		pr.set("purchase_order", doc.purchase_order)
	if returning:
		pr.set("is_return", 1)
		pr.set("return_against", doc.return_against)
	notes = []
	taken = []
	for line in doc.get("lines") or []:
		if line.get("resolution") == "Reject" or not line.get("item_code"):
			continue
		code = line.get("item_code")
		facts = _item_facts(code)
		row = rows.get(line.get("po_item")) or {}
		same_item = row.get("item_code") == code
		qty = float(line.get("qty") or 0)
		entry = {"item_code": code, "qty": -qty if returning else qty,
		         "received_qty": -qty if returning else qty, "rate": float(row.get("rate") or 0),
		         "amount": round((-qty if returning else qty) * float(row.get("rate") or 0), 2), "warehouse": warehouse,
		         "uom": (row.get("uom") if same_item else None) or facts.get("stock_uom") or None}
		if doc.purchase_order and same_item:
			entry["purchase_order"] = doc.purchase_order
			entry["purchase_order_item"] = row["name"]
		elif line.get("match") == SUB:
			notes.append(f"{code} stands in for PO line {row.get('item_code')} — received unlinked to it")
		if line.get("lot_no") and not returning:
			batch, why = _batch(code, line.get("lot_no"), expiry=str(line.get("expiry_date") or ""),
			                    supplier=doc.supplier, intake=doc.name)
			if batch:
				entry["batch_no"] = batch
				_put(line, "batch_no", batch)
			if why:
				notes.append(why)
		elif line.get("batch_no") and returning:
			entry["batch_no"] = line.get("batch_no")
		pr.append("items", entry)
		taken.append(line)
	if not taken:
		raise ReceivingError(f"{intake} has nothing to receive (every line rejected).")
	pr.flags.ignore_permissions = True
	pr.insert()
	doc.purchase_receipt = pr.name
	doc.status = "Return Drafted" if returning else "Receipt Drafted"
	doc.drafted_by = actor or None
	task = None if returning else _raise_check_in(doc, taken)
	doc.check_in_task = task or None
	doc.save(ignore_permissions=True)
	return {"intake": intake, "purchase_receipt": pr.name, "docstatus": 0, "warehouse": warehouse,
	        "lines": len(taken), "notes": notes, "check_in_task": task, "already": False,
	        "next_step": "A person checks the draft against the ticket and submits it in ERPNext."}


def _raise_check_in(doc, lines: list) -> str | None:
	if not compat.doctype_exists("Farm Task Template") or not frappe.db.get_value(
		"Farm Task Template", {"template_name": CHECK_IN}, "enabled"):
		return None
	from .tools import tasktemplates

	parts, materials = [], []
	for line in lines:
		flags = [label for label, on in (("RESTRICTED USE — licensed person", line.get("restricted_use")),
		                                 ("freeze-sensitive: store heated", line.get("freeze_sensitive")),
		                                 ("NO SDS ON FILE", not line.get("sds_on_file"))) if on]
		code, qty, lot = line.get("item_code"), float(line.get("qty") or 0), line.get("lot_no")
		parts.append(f"• {code} × {qty:g} {line.get('uom') or ''}".rstrip()
		             + (f", lot {lot}" if lot else "") + (f" — {'; '.join(flags)}" if flags else ""))
		materials.append({"item_code": code, "qty": qty})
	result = tasktemplates.create_task_from_template(
		{"template": CHECK_IN, "company": doc.company,
		 "task_name": f"{CHECK_IN} — {doc.supplier} {doc.document_no or doc.name}"[:140],
		 "notes": f"Delivery {doc.name} ({doc.purchase_receipt}, draft):\n" + "\n".join(parts)},
		origin="compliance_rule",
		fields={"source_workorder": f"receiving:{doc.name}"[:140], "materials_used": json.dumps(materials)})
	return (result.data or {}).get("name")


CHECK_IN_TEMPLATE = {
	"template_name": CHECK_IN,
	"title_es": "Revisión de entrega de químicos",
	"task_type": "Inspection",
	"description": "Check a chemical delivery into the shed: count it, match the lots, check labels and SDS, store "
	               "each product the way its label says.",
	"instructions": "Count every container against the list on this task. Read the lot on each and match it. Look for "
	                "leaks or damaged labels. Check the SDS binder has a sheet for each product (the task says which "
	                "have none on file). Store by the label: freeze-sensitive products in the heated room, "
	                "restricted-use products locked. Photograph the stacked delivery.",
	"instructions_es": "Cuenta cada envase contra la lista de esta tarea. Lee el lote de cada uno y compáralo. Busca "
	                   "fugas o etiquetas dañadas. Revisa que la carpeta de hojas de seguridad (SDS) tenga una hoja de "
	                   "cada producto (la tarea dice cuáles no la tienen). Guarda según la etiqueta: los que no deben "
	                   "congelarse en el cuarto con calefacción, los de uso restringido bajo llave. Toma una foto de la "
	                   "entrega acomodada.",
	"estimated_duration_minutes": 30,
	"skill_required": "",
	"dispatch_mode": "Either",
	"evidence_required": {"photos": True},
	"checklist": [{"item_name": "Containers counted — match the list", "required": True},
	              {"item_name": "Lot on each container matches", "required": True},
	              {"item_name": "No leaks; labels intact and readable", "required": True},
	              {"item_name": "SDS on file for every product", "required": True},
	              {"item_name": "Stored per label (heated / locked where it says)", "required": True}],
	"enabled": 1,
}


# ── from the job link and from email ─────────────────────────────────────────
def from_job(job: str, *, file: str = "") -> str | None:
	"""A supplier delivery job's ticket photo or Delivered tap opens (once) its intake. Quiet when receiving is off."""
	row = frappe.db.get_value("Contractor Job", job, ["company", "kind", "supplier", "purchase_order"], as_dict=True)
	if not row or row.get("kind") not in ("Supplier Delivery", "Supplier Pickup") or not enabled(row.get("company")):
		return None
	existing = frappe.db.get_value(INTAKE, {"job": job}, "name")
	if existing:
		if file and not frappe.db.get_value(INTAKE, existing, "source_file"):
			frappe.db.set_value(INTAKE, existing, "source_file", file)
		return existing
	if row.get("kind") == "Supplier Pickup":
		return None  # a return needs the receipt it goes back against — a person opens it
	return open_intake(row["company"], supplier=row.get("supplier") or "", purchase_order=row.get("purchase_order") or "",
	                   job=job, source="ticket_photo", source_file=file,
	                   notes="Opened from the delivery job — the lines come from the ticket photo (phone) or the "
	                         "supplier's invoice.")


def ingest_csv(company: str, connector_key: str, text, *, purchase_order: str = "", actor: str = "") -> list:
	"""A portal export: one intake per document number in it."""
	_require_on(company)
	body = connector(connector_key)
	if body.get("channel") != "portal_csv":
		raise ReceivingError(f"{connector_key} is a {body.get('channel')} connector, not portal_csv.")
	made = []
	for document in adapter(body).to_lines(text):
		if not document["lines"]:
			continue
		duplicate = document["document_no"] and frappe.db.get_value(
			INTAKE, {"company": company, "document_no": document["document_no"], "supplier": body["supplier"]}, "name")
		if duplicate:
			made.append({"intake": duplicate, "already": True, "document_no": document["document_no"]})
			continue
		po = purchase_order or _po_from_number(document["po_number"], company)
		name = open_intake(company, supplier=body["supplier"], purchase_order=po, source="portal_csv",
		                   connector_key=connector_key)
		made.append(set_lines(name, document["lines"], document_no=document["document_no"],
		                      document_date=document["date"], po_number=document["po_number"], actor=actor))
	return made


def ingest_communication(communication: str, *, company: str = "", connector_key: str = "", actor: str = "") -> list:
	"""An email's PDF(s) through the matching email_pdf connector: one intake per PDF."""
	sender = _address(frappe.db.get_value("Communication", communication, "sender"))
	if connector_key:
		body = connector(connector_key)
	else:
		found = [c for c in connectors(channel="email_pdf") if adapter(c).recognises(sender)]
		if not found:
			raise ReceivingError(f"no published email_pdf connector recognises {sender or 'that sender'}.")
		body = found[0]
	company = company or (body.get("companies") or [None])[0] or ""
	if not company:
		raise ReceivingError("which company is this delivery for? (company, or the connector's companies)")
	_require_on(company)
	from .tools import files

	made = []
	for f in frappe.db.get_all("File", filters={"attached_to_doctype": "Communication",
	                                            "attached_to_name": communication},
	                           fields=["name", "file_name"], limit=20) or []:
		if not str(f.get("file_name") or "").lower().endswith(".pdf"):
			continue
		if frappe.db.get_value(INTAKE, {"source_file": f["name"]}, "name"):
			continue
		documents = adapter(body).to_lines(files.read_file_bytes(f["name"]))
		document = documents[0] if documents else {"lines": []}
		po = _po_from_number(document.get("po_number"), company)
		name = open_intake(company, supplier=body["supplier"], purchase_order=po, source="email_pdf",
		                   connector_key=body["key"], source_file=f["name"], communication=communication)
		if document["lines"]:
			made.append(set_lines(name, document["lines"], document_no=document.get("document_no") or "",
			                      document_date=document.get("date") or "", po_number=document.get("po_number") or "",
			                      actor=actor))
		else:
			made.append(summary(frappe.get_doc(INTAKE, name)) | {
				"note": "no lines matched the connector's line pattern — type them or fix the pattern"})
	return made


def _po_from_number(number: str, company: str) -> str:
	number = str(number or "").strip()
	if not number:
		return ""
	if frappe.db.exists(PO, number):
		return number
	hits = [r["name"] for r in frappe.db.get_all(PO, filters={"company": company, "docstatus": 1}, fields=["name"],
	                                             limit=5000) or [] if str(r["name"]).endswith(number)]
	return hits[0] if len(hits) == 1 else ""


# ── reading ──────────────────────────────────────────────────────────────────
def summary(doc) -> dict:
	if isinstance(doc, str):
		doc = frappe.get_doc(INTAKE, doc)
	lines = []
	for i, line in enumerate(doc.get("lines") or [], start=1):
		lines.append({"idx": i, "as_read": line.get("line_text"), "item_code": line.get("item_code"),
		              "qty": float(line.get("qty") or 0), "uom": line.get("uom"), "lot_no": line.get("lot_no"),
		              "expiry_date": str(line.get("expiry_date") or "") or None,
		              "epa_reg_number": line.get("epa_reg_number"), "match": line.get("match"),
		              "matched_by": line.get("match_by"), "po_item": line.get("po_item"),
		              "open_on_po": float(line.get("po_qty_open") or 0), "delta": float(line.get("delta") or 0),
		              "resolution": line.get("resolution") or None, "batch_no": line.get("batch_no"),
		              "restricted_use": bool(compat.checked(line.get("restricted_use"))),
		              "freeze_sensitive": bool(compat.checked(line.get("freeze_sensitive"))),
		              "sds_on_file": bool(compat.checked(line.get("sds_on_file"))),
		              "needs_a_person": _pending(line)})
	raw = doc.get("missing_lines")
	missing = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
	return {"intake": doc.name, "status": doc.status, "company": doc.company, "supplier": doc.supplier,
	        "purchase_order": doc.purchase_order, "job": doc.job, "direction": doc.direction, "source": doc.source,
	        "document_no": doc.document_no, "lines": lines, "ordered_not_delivered": missing,
	        "purchase_receipt": doc.purchase_receipt, "check_in_task": doc.check_in_task}


def list_intakes(company: str = "", status: str = "", supplier: str = "", limit: int = 50) -> list:
	filters = {}
	if company:
		filters["company"] = company
	if status:
		filters["status"] = status
	if supplier:
		filters["supplier"] = supplier
	return frappe.db.get_all(INTAKE, filters=filters, fields=["name", "company", "supplier", "status", "source",
	                                                          "document_no", "purchase_order", "purchase_receipt",
	                                                          "creation"],
	                         order_by="creation desc", limit=max(1, min(int(limit or 50), 500)))


def trace_lot(lot: str, item_code: str = "") -> dict:
	"""A delivered lot: where it came in, and the spray applications that used that product while it was in the shed."""
	filters = {"lot_no": lot}
	if item_code:
		filters["item_code"] = item_code
	lines = frappe.db.get_all(LINE, filters=filters, fields=["parent", "item_code", "qty", "uom", "batch_no"],
	                          limit=200) or []
	arrivals = []
	for line in lines:
		head = frappe.db.get_value(INTAKE, line["parent"], ["company", "supplier", "document_no", "purchase_receipt",
		                                                    "creation"], as_dict=True) or {}
		arrivals.append({"intake": line["parent"], "item_code": line["item_code"], "qty": line["qty"],
		                 "uom": line.get("uom"), "batch_no": line.get("batch_no"), **{k: (str(v) if v else v)
		                                                                              for k, v in head.items()}})
	sprays = []
	if arrivals and compat.doctype_exists("Spray Application"):
		codes = {a["item_code"] for a in arrivals}
		lots = {lot, *(a.get("batch_no") for a in arrivals if a.get("batch_no"))}
		since = min(str(a.get("creation") or "")[:10] for a in arrivals)
		fields = compat.existing_fields("Spray Application", ("name", "started_at", "completed_at", "products_applied",
		                                                     "company", "status"))
		for row in frappe.db.get_all("Spray Application", fields=fields, limit=5000) or []:
			day = str(row.get("completed_at") or row.get("started_at") or "")[:10]
			if since and day and day < since:
				continue
			try:
				applied = json.loads(row.get("products_applied") or "[]")
			except ValueError:
				applied = []
			used = [p for p in applied if isinstance(p, dict) and p.get("item") in codes]
			if not used:
				continue
			named = [p for p in used if p.get("lot_no") or p.get("batch_no")]
			exact = any(str(p.get("lot_no") or p.get("batch_no")) in lots for p in named)
			if named and not exact:
				continue
			blocks = [b.get("block") for b in frappe.get_doc("Spray Application", row["name"]).get("blocks") or []]
			sprays.append({"spray_application": row["name"], "date": day or None, "blocks": blocks,
			               "lot_recorded": exact, "items": sorted({p.get("item") for p in used})})
	return {"lot": lot, "arrived": arrivals, "sprays": sprays,
	        "note": "Sprays that recorded the lot are exact; the rest used the product while this lot was in stock."}


def audit_packet(intake: str) -> dict:
	"""Everything about one delivery in one answer: documents, lines, reconciliation, receipt, check-in, licences."""
	out = summary(intake)
	doc = frappe.get_doc(INTAKE, intake)
	out["source_file"] = doc.source_file
	out["communication"] = doc.communication
	out["connector"] = doc.connector
	raw = doc.get("validation")
	out["checks"] = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
	out["receipt_docstatus"] = (int(frappe.db.get_value(PR, doc.purchase_receipt, "docstatus") or 0)
	                            if doc.purchase_receipt else None)
	if doc.check_in_task:
		task = frappe.db.get_value("Farm Task", doc.check_in_task, ["state", "assigned_to"], as_dict=True) or {}
		out["check_in"] = {"task": doc.check_in_task, "state": task.get("state"), "assigned_to": task.get("assigned_to")}
	rup = sorted({line["item_code"] for line in out["lines"] if line["restricted_use"]})
	out["restricted_use_items"] = rup
	out["no_sds"] = sorted({line["item_code"] for line in out["lines"] if line["item_code"] and not line["sds_on_file"]})
	return out
