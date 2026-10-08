# SPDX-License-Identifier: MIT
"""Chemical receiving — the MCP side. v0.272.0 (docs/contracts/chemical_receiving_v0_272.yaml).

Reads are on; writes are OFF until switched on, and every write is refused for a company until
`chemical_receiving_enabled` is on for it. Nothing here submits a receipt or moves stock: the end of the road is a
DRAFT Purchase Receipt a person checks against the ticket and submits.
"""

from __future__ import annotations

import frappe

from .. import receiving
from ..args import as_bool, as_int, as_str
from ..errors import ToolError
from ..result import ToolResult


def _actor() -> str:
	from .. import security

	return security.caller_identity() or str(getattr(frappe.session, "user", "") or "")


def _wrap(fn):
	try:
		return fn()
	except receiving.ReceivingError as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None


def _lines(args: dict):
	raw = args.get("lines")
	if isinstance(raw, str):
		import json

		try:
			raw = json.loads(raw)
		except ValueError:
			raise ToolError("lines: a JSON list of {item_text or sku or item_code, qty, uom, lot_no, expiry_date, "
			                "epa_reg_number}.") from None
	return raw


def _intake(args: dict) -> str:
	name = as_str(args, "intake", required=True)
	if not frappe.db.exists(receiving.INTAKE, name):
		raise ToolError(f"no Supplier Delivery Intake {name!r}.")
	return name


def open_delivery_intake(args: dict) -> ToolResult:
	"""An intake for a delivery (or a return), optionally with its lines straight away."""
	def run():
		name = receiving.open_intake(
			as_str(args, "company", required=True), supplier=as_str(args, "supplier"),
			purchase_order=as_str(args, "purchase_order"), job=as_str(args, "job"),
			source=as_str(args, "source") or "manual", connector_key=as_str(args, "connector"),
			direction=as_str(args, "direction") or "Delivery", return_against=as_str(args, "return_against"),
			notes=as_str(args, "notes"))
		lines = _lines(args)
		if lines:
			return receiving.set_lines(name, lines, document_no=as_str(args, "document_no"),
			                           document_date=as_str(args, "document_date"), actor=_actor())
		return receiving.summary(name)
	data = _wrap(run)
	return ToolResult(data=data, summary=f"{data['intake']}: {data['status']}, {len(data['lines'])} line(s)",
	                  docstatus_delta="0 → 0 (created)")


def set_delivery_lines(args: dict) -> ToolResult:
	"""Replace an intake's lines (typed, or read off the ticket), then match and reconcile."""
	intake = _intake(args)
	data = _wrap(lambda: receiving.set_lines(intake, _lines(args), document_no=as_str(args, "document_no"),
	                                         document_date=as_str(args, "document_date"),
	                                         po_number=as_str(args, "po_number"), source=as_str(args, "source"),
	                                         actor=_actor()))
	return ToolResult(data=data, summary=f"{intake}: {data['status']}", docstatus_delta="0 → 0 (updated)")


def ingest_supplier_csv(args: dict) -> ToolResult:
	"""A supplier portal's CSV export through its connector: one intake per invoice / ticket number in it."""
	text = args.get("csv_text")
	file = as_str(args, "file")
	if not text and file:
		from . import files

		text = files.read_file_bytes(file)
	if not text:
		raise ToolError("csv_text (the export's contents) or file (an uploaded File) is required.")
	made = _wrap(lambda: receiving.ingest_csv(as_str(args, "company", required=True),
	                                          as_str(args, "connector", required=True), text,
	                                          purchase_order=as_str(args, "purchase_order"), actor=_actor()))
	return ToolResult(data={"intakes": made, "count": len(made)}, summary=f"{len(made)} intake(s) from the CSV",
	                  docstatus_delta="0 → 0 (created)")


def ingest_delivery_email(args: dict) -> ToolResult:
	"""An email's PDF invoice / ticket through the email_pdf connector that recognises its sender."""
	communication = as_str(args, "communication", required=True)
	if not frappe.db.exists("Communication", communication):
		raise ToolError(f"no Communication {communication!r}.")
	made = _wrap(lambda: receiving.ingest_communication(communication, company=as_str(args, "company"),
	                                                    connector_key=as_str(args, "connector"), actor=_actor()))
	return ToolResult(data={"intakes": made, "count": len(made)}, summary=f"{len(made)} intake(s) from the email",
	                  docstatus_delta="0 → 0 (created)")


def resolve_delivery_line(args: dict) -> ToolResult:
	"""Settle one line: the right Item / unit / qty, then Accept (receive as delivered) or Reject."""
	intake = _intake(args)
	data = _wrap(lambda: receiving.resolve_line(intake, as_int(args, "idx", 0), resolution=as_str(args, "resolution"),
	                                            item_code=as_str(args, "item_code"), uom=as_str(args, "uom"),
	                                            qty=args.get("qty"), note=as_str(args, "note"), actor=_actor()))
	return ToolResult(data=data, summary=f"{intake}: {data['status']}", docstatus_delta="0 → 0 (updated)")


def draft_delivery_receipt(args: dict) -> ToolResult:
	"""The DRAFT Purchase Receipt (batches per lot, chemical storage warehouse) and the check-in task."""
	intake = _intake(args)
	data = _wrap(lambda: receiving.draft_receipt(intake, _actor()))
	return ToolResult(data=data, summary=f"{intake}: draft {data['purchase_receipt']}"
	                  + (f", check-in {data['check_in_task']}" if data.get("check_in_task") else ""),
	                  docstatus_delta="none → 0 (draft)")


def get_delivery_intake(args: dict) -> ToolResult:
	"""One delivery: lines as read, match and reconciliation, what is ordered but not delivered, receipt, check-in."""
	intake = _intake(args)
	data = receiving.audit_packet(intake)
	return ToolResult(data=data, summary=f"{intake}: {data['status']}")


def list_delivery_intakes(args: dict) -> ToolResult:
	rows = receiving.list_intakes(as_str(args, "company"), as_str(args, "status"), as_str(args, "supplier"),
	                              as_int(args, "limit", 50))
	return ToolResult(data={"intakes": rows, "count": len(rows)}, summary=f"{len(rows)} intake(s)")


def list_supplier_connectors(args: dict) -> ToolResult:
	rows = receiving.connectors(as_str(args, "supplier"), as_str(args, "channel"),
	                            published_only=not as_bool(args, "include_drafts", False))
	return ToolResult(data={"connectors": rows, "count": len(rows), "channels": list(receiving.CHANNELS),
	                        "reading": ["email_pdf", "portal_csv"]}, summary=f"{len(rows)} connector(s)")


def trace_input_lot(args: dict) -> ToolResult:
	"""A delivered lot forward: where it came in, and the sprays that used it."""
	data = receiving.trace_lot(as_str(args, "lot", required=True), as_str(args, "item_code"))
	return ToolResult(data=data, summary=f"lot {data['lot']}: {len(data['arrived'])} arrival(s), "
	                  f"{len(data['sprays'])} spray(s)")
