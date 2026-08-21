# SPDX-License-Identifier: MIT
"""FSMA 204: the lot code, the critical tracking events, and the recall drill.

────────────────────────────────────────────────────────────────────────────
WHAT WAS ALREADY HERE, AND WHY IT WAS NOT ENOUGH
────────────────────────────────────────────────────────────────────────────

`traceability.py` and `tools/trace.py` already walk this farm's registers and
already answer a mock recall — from a bin, a shipment or a block, through the
bucket captures, out to the customers. They are not replaced by anything here and
nothing here duplicates them. `trace_forward` and `trace_backward` keep their
arguments and their answers exactly.

What they walk is a chain of FREE-TEXT IDS: `block_id`, `bin_id`, `shipment_id`,
written on a bucket capture by whoever was holding the phone. That chain is real
and it is the honest record of what this site stored — and it has three properties
the Food Traceability Rule will not accept:

  * IT IS NOT AN IDENTIFIER. Two bins called "17" in two seasons are two bins,
    and `traceability.py` says so at length. A regulator asking "produce the
    records for lot X" is asking for something that names ONE lot.
  * IT DOES NOT SURVIVE A HAND-OFF. The buyer's portal, the packing house's
    system and the carrier's manifest do not have this site's bucket captures.
  * IT DOES NOT SURVIVE A TRANSFORMATION. Four field lots combined into one
    pallet destroy the join, and nothing in the free-text chain records which
    four.

So this module adds the thing the rule actually requires — a **traceability lot
code**, assigned once, unique on the site, printed on the fruit — and a
**Critical Tracking Event** register that INDEXES the records already here under
it. Nothing is copied. A CTE says "Spray Application SP-0041 is a Growing event in
lot YC3-BING-20260821-01"; the spray's own record still holds the products, the
rate and the weather, and remains the only place those live.

────────────────────────────────────────────────────────────────────────────
THE TWO TRACES, AND WHY THEY ARE NAMED SEPARATELY
────────────────────────────────────────────────────────────────────────────

`trace_lot_forward` and `trace_lot_backward` are NOT renamed or widened versions
of `trace_forward` and `trace_backward`. They take a lot code and walk the
transformation graph; the older pair take a block, a bin or a shipment and walk
the free-text chain. Both are correct, they answer different questions from
different evidence, and giving the new pair the old names — or bolting a
`lot_code` argument onto the old pair — would have made every existing caller's
tool description a lie about what it now does.

    trace_lot_backward   lot → its source lots → their source lots → the blocks
                         → the Growing events on those blocks, and then the
                         SPRAY REGISTER, bounded at each root lot's own harvest
                         date. Asked when the product is suspect.

    trace_lot_forward    lot → the lots it was made into → their Shipping events
                         → the destinations, the receivers and the carriers.
                         Asked when the SOURCE is suspect, and it is the half a
                         recall is actually run from.

`recall_drill` is `trace_lot_forward` written as the document somebody reads at
eleven at night: everywhere the lot went, who has it, when it got there, and —
stated first rather than omitted — the fruit that cannot be placed at all.

────────────────────────────────────────────────────────────────────────────
EVERY BREAK IS NAMED. THIS IS THE POINT OF THE READ, NOT A FAILURE OF IT
────────────────────────────────────────────────────────────────────────────

The idiom `trace_contract_to_cash` established and `traceability.py` argues for
holds harder here, because the FSMA answer is the one shown to a regulator. A
lot with no Shipping event is reported as "this lot has not left, or its
departure was never recorded, and those are different" rather than as an empty
destination list. A lot with no `field` and no `source_lots` is reported as fruit
whose origin was never written down. A `reference_doctype`/`reference_name` that
resolves to nothing is reported as an unresolved reference rather than dropped.

A confident empty answer is the failure mode this whole file exists to avoid.

────────────────────────────────────────────────────────────────────────────
WHY THE AUTO-INDEXING IS A TOOL AND NOT A `doc_events` HOOK
────────────────────────────────────────────────────────────────────────────

`hooks.py` promises this app installs no `doc_events`, and
`tests_standalone/test_hooks.py` fails the build over one. `tools/itgc.py` had
exactly this problem and settled it the same way: the indexing runs from this
app's own tool layer, where an operator can see it, switch it off and re-run it.

`index_lot_events` is that tool. It sweeps a window of Bin Seals, Scale Tickets
and Spray Applications and writes the lots and CTEs they imply. It is IDEMPOTENT
on `(lot_code, event_type, reference_doctype, reference_name)`, so re-running it
over a week already indexed writes nothing — which is what makes it safe to
schedule, safe to run twice by accident, and safe to run over a whole season the
first time somebody turns it on.
"""

from __future__ import annotations

import re

import frappe

from .. import compat, traceability
from ..args import as_date, as_float, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import employee as employee_tool

DOCTYPE = "Traceability Lot Code"
SOURCE_DOCTYPE = "Traceability Lot Source"
CTE_DOCTYPE = "Critical Tracking Event"

BIN_SEAL = "Bin Seal"
SCALE_TICKET = "Scale Ticket"
SPRAY = "Spray Application"
SHIPMENT = "Trade Shipment"
FIELD = "Field"

#: The rule's five events, in the order a lot passes through them. Kept here as
#: well as on the DocType because an argument is validated before a document
#: exists, and a caller who sends "Harvest" deserves the list rather than a
#: Frappe link-validation error thrown three layers down.
EVENT_TYPES = ("Growing", "Receiving", "Transforming", "Creating", "Shipping")

#: Most lots one traversal will walk. A transformation graph is a few hops wide
#: in any real packing operation; five hundred means either a season-long chain
#: or a data fault, and both are better reported than silently truncated.
LOT_CAP = 500

#: Most events one lot's timeline returns. A lot with more than this has been
#: indexed twice or is a year-long aggregate, and either way the answer is meant
#: to be READ — the same argument `traceability.HOP_CAP` makes.
EVENT_CAP = 1000

#: Most lots one list read returns.
RECORD_CAP = 500

#: Most source records one `index_lot_events` sweep will touch, per register.
#: A sweep is run over a week or a season and is meant to finish inside one
#: request; a cap that bites is stated in the answer, never silent.
SWEEP_CAP = 1000

#: What a read returns off a lot. `name` is the lot code — see the DocType.
LOT_FIELDS = (
	"name",
	"lot_code",
	"status",
	"variety",
	"company",
	"field",
	"harvest_date",
	"harvest_shift",
	"planting_season",
	"quantity",
	"quantity_uom",
	"notes",
)

#: What a read returns off an event. Every Key Data Element the rule names is
#: here, which is the point: an answer that omitted one would be an answer a
#: regulator sends back.
CTE_FIELDS = (
	"name",
	"event_type",
	"lot_code",
	"event_datetime",
	"location",
	"company",
	"actor",
	"actor_name",
	"reference_doctype",
	"reference_name",
	"description",
	"quantity",
	"quantity_uom",
	"source_location",
	"destination_location",
	"carrier",
	"receiver",
)

#: The key details `get_lot_timeline` pulls back off each referenced record, per
#: register. A CTE is a POINTER (see the DocType) and the detail lives in the
#: register it points at; this is how the timeline shows it without the caller
#: making one read per event.
#:
#: A doctype that is not in this map still resolves — see `_reference_detail` —
#: it just comes back with its docname and nothing else, which is the honest
#: answer for a register this module was not written against.
REFERENCE_FIELDS = {
	SPRAY: ("name", "status", "completed_at", "applicator", "tank_mix", "total_acres"),
	BIN_SEAL: ("name", "bin_tag", "bucket_count", "sealed_at", "field", "shift", "crop"),
	SCALE_TICKET: (
		"name",
		"ticket_number",
		"date",
		"customer",
		"variety",
		"net_weight",
		"weight_uom",
		"destination",
		"field",
		"status",
	),
	SHIPMENT: (
		"name",
		"status",
		"customer",
		"ship_date",
		"carrier",
		"destination_name",
		"destination_city",
		"destination_state",
		"total_packages",
		"net_weight",
		"weight_uom",
	),
	"Settlement Statement": ("name", "customer", "statement_date", "status"),
	"Farm Shift": ("name", "shift_type", "start_datetime", "foreman_name", "status"),
	"Water Test": ("name", "sample_date", "result", "irrigation_zone"),
}


def _require() -> None:
	compat.require_doctype(
		DOCTYPE,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app. "
		"It is the FSMA 204 lot code every Critical Tracking Event is filed under.",
	)


def _require_events() -> None:
	compat.require_doctype(
		CTE_DOCTYPE,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _break(after: str, missing: str, note: str) -> dict:
	"""One named gap in a chain, in the shape `tools/trace.py` already uses."""
	return {"after": after, "missing": missing, "note": note}


# ── the lot code itself ─────────────────────────────────────────────────────


def _code_part(value: str, fallback: str, length: int = 10) -> str:
	"""One segment of a lot code: upper-case, alphanumeric, bounded.

	A lot code is READ OFF A BIN AND TYPED INTO A BUYER'S PORTAL. Spaces, commas
	and slashes in a docname are a URL-escaping problem for somebody three
	systems away, so 'Yellow Camp Block 3 - MC' becomes 'YELLOWCAMP' and the
	Field link on the record carries the full name for anybody who needs it.
	"""
	cleaned = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
	return (cleaned or fallback)[:length]


def _initials(value: str) -> str:
	"""'Yellow Camp Block 3' → 'YCB3'. The way a crew already says a block.

	TRUNCATING THE NAME DOES NOT WORK, and the failure is silent. 'Yellow Camp
	Block 3' and 'Yellow Camp Block 4' both truncate to 'YELLOWCAMP', so two
	sibling blocks harvested on one day produce codes distinguishable only by
	their sequence number — which is unique, and useless: nobody reading
	'YELLOWCAMP-BING-20260821-02' off a bin can tell which block it was.

	Initials plus the digits keep exactly the part people say out loud. The
	company abbreviation Frappe appends to a Field docname is dropped first: it
	is the same on every block on the site and carries no information here.
	"""
	head = str(value or "").split(" - ")[0]
	words = re.findall(r"[A-Za-z]+|[0-9]+", head)
	letters = "".join(word[0] for word in words if word[:1].isalpha())
	digits = "".join(word for word in words if word[:1].isdigit())
	return f"{letters}{digits}".upper()


def _field_code(field: str) -> str:
	"""The block segment: its `block_number` where the register has one.

	`block_number` FIRST because it is what the crew and the packing house both
	say — 'YC3'. Where the register has none, the Field's own name is reduced to
	its initials and digits by `_initials`, which is the same thing by another
	route. 'LOT' is the fallback for a lot with no block at all, which a
	transformation lot legitimately has.
	"""
	if not field:
		return "LOT"
	row = {}
	if compat.doctype_exists(FIELD):
		row = frappe.db.get_value(FIELD, field, ["block_number", "field_name"], as_dict=True) or {}
	stated = str(row.get("block_number") or "").strip()
	if stated:
		return _code_part(stated, "BLOCK")
	derived = _initials(row.get("field_name") or field)
	return _code_part(derived, "BLOCK") if len(derived) >= 2 else _code_part(field, "BLOCK")


def _next_sequence(prefix: str) -> str:
	"""The next free two-digit sequence under one prefix.

	COUNTED OFF THE EXISTING CODES rather than off a Frappe naming series,
	because the sequence is per block, per variety, PER DAY — 'the third Bing lot
	off Yellow Camp 3 today' — and a series is per doctype. A series would give
	'-00417', which is a number about the site rather than about the lot.

	The scan is bounded to one prefix, which on any real day is single digits.
	Past 99 it keeps counting and the code simply grows a digit: a hundred lots
	off one block in one day is unusual, not wrong, and refusing the hundredth
	would refuse a real lot.
	"""
	existing = (
		frappe.db.get_all(
			DOCTYPE,
			filters={"lot_code": ("like", f"{prefix}-%")},
			fields=["lot_code"],
			limit=LOT_CAP,
		)
		or []
	)
	highest = 0
	for row in existing:
		tail = str(row.get("lot_code") or "").rsplit("-", 1)[-1]
		if tail.isdigit():
			highest = max(highest, int(tail))
	return f"{highest + 1:02d}"


def generate_lot_code(field: str, variety: str, harvest_date: str) -> str:
	"""`{block}-{variety}-{YYYYMMDD}-{sequence}`, unique on this site.

	The uniqueness is the DocType's, not this function's: `lot_code` carries a
	unique index and is the docname. This computes the next FREE code; if two
	requests race for it, the second one's insert fails on the index rather than
	quietly creating a duplicate, which is the correct outcome and the reason the
	index is there.
	"""
	day = str(harvest_date or frappe.utils.today())[:10].replace("-", "")
	prefix = f"{_field_code(field)}-{_code_part(variety, 'LOT', 8)}-{day}"
	return f"{prefix}-{_next_sequence(prefix)}"


def create_traceability_lot(args: dict) -> ToolResult:
	"""Assign one lot code to one day's fruit off one block.

	THE MOMENT THE JOIN BECOMES POSSIBLE. Before this call the afternoon's fruit
	is a set of bins, a shift and a block name; afterwards it is a lot, and every
	record touching it can be filed under one identifier that survives leaving
	the farm.

	IDEMPOTENT ON `(field, variety, harvest_date, company)` unless the caller
	asks otherwise. A phone that created a lot and did not hear the answer sends
	the same call again, and a second lot code for one afternoon's fruit is worse
	than a failed call: it splits the recall in half, and neither half names the
	other. Pass `allow_duplicate` where the operation genuinely picked the same
	block twice in one day and wants two codes.

	A LOT WITH NO BLOCK IS ACCEPTED where `source_lots` is given, and only there.
	That is a transformation lot — a pallet made of four field lots — whose block
	is whatever its sources name, and asserting one of them on the pallet would
	be a guess that a backward trace would then report as fact.
	"""
	_require()
	actor = employee_tool.require_shift_role()

	field = as_str(args, "field")
	variety = as_str(args, "variety")
	harvest_date = as_date(args, "harvest_date") or frappe.utils.today()
	sources = _source_rows(args.get("source_lots"))

	if field and compat.doctype_exists(FIELD) and not frappe.db.exists(FIELD, field):
		raise ToolError(
			f"no Field called {field!r} on this site. `field` is a Link because a residue "
			"question runs from a lot to a block to a spray register, and a typed block name "
			"breaks that chain at its weakest point. list_fields has the register. Nothing was "
			"created."
		)
	if not field and not sources:
		raise ToolError(
			"a lot needs either a `field` — the block the fruit came off — or `source_lots`, the "
			"lots it was made from. A lot with neither has an origin nobody wrote down, and a "
			"backward trace from it stops at the lot code itself. Nothing was created."
		)

	company = resolve_company(as_str(args, "company"), required=False) or _company_of_field(field)
	if company:
		employee_tool.require_company_scope(actor, company)

	given_code = as_str(args, "lot_code").strip().upper()
	if given_code and frappe.db.exists(DOCTYPE, given_code):
		raise ToolError(
			f"{given_code} is already a lot on this site. A lot code names ONE lot — a second "
			"record under one code is the join breaking, which is exactly what the unique index "
			"on it exists to refuse. get_traceability_lot has the existing one. Nothing was "
			"created."
		)

	if not given_code and not compat.checked(args.get("allow_duplicate")):
		existing = _existing_lot(field, variety, harvest_date, company)
		if existing:
			return ToolResult(
				data={
					**_describe_lot(existing),
					"actor": actor,
					"already_existed": True,
					"note": (
						f"{existing} already covers {variety or 'this fruit'} off "
						f"{field or 'this block'} on {harvest_date}, so this call was a retry and "
						"nothing was written a second time. Two lot codes for one afternoon's "
						"fruit split a recall in half and neither half names the other. Pass "
						"allow_duplicate where the block genuinely produced two lots that day."
					),
				},
				summary=f"{existing} already covered {field or 'that lot'} on {harvest_date}",
			)

	lot_code = given_code or generate_lot_code(field, variety, harvest_date)

	doc = frappe.new_doc(DOCTYPE)
	doc.lot_code = lot_code
	doc.field = field or None
	doc.variety = variety or None
	doc.harvest_date = harvest_date
	doc.company = company or None
	doc.status = as_str(args, "status") or "Active"
	doc.harvest_shift = as_str(args, "harvest_shift") or as_str(args, "shift") or None
	doc.planting_season = as_str(args, "planting_season") or None
	doc.quantity = as_float(args.get("quantity"), "quantity") if args.get("quantity") is not None else None
	doc.quantity_uom = as_str(args, "quantity_uom") or None
	doc.notes = as_str(args, "notes") or None
	for row in sources:
		doc.append("source_lots", row)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	created_event = _write_cte(
		lot_code=doc.name,
		event_type="Transforming" if sources else "Creating",
		event_datetime=f"{harvest_date} 00:00:00",
		company=company,
		location=as_str(args, "location") or field or "",
		description=(
			f"Lot {doc.name} created from {len(sources)} source lot(s)."
			if sources
			else f"Lot {doc.name} created for {variety or 'fruit'} off {field or 'no block'} "
			f"on {harvest_date}."
		),
		quantity=doc.quantity,
		quantity_uom=doc.quantity_uom or "",
		actor=as_str(args, "actor"),
	)

	data = {
		**_describe_lot(doc.name, row=dict(doc.as_dict())),
		"actor": actor,
		"already_existed": False,
		"opening_event": created_event,
		"note": (
			f"{doc.name} is the identifier every record touching this fruit can now be filed "
			"under. index_lot_events will attach the sprays, the bin seals and the scale tickets "
			"this site already holds; record_cte files anything it does not."
		),
	}
	if not company:
		data["no_company_note"] = (
			"This lot names no company. Frappe scopes this register by the Company link, so a lot "
			"without one is visible to every principal on the site regardless of their entity "
			"access. Pass `company`, or set `owning_entity` on the Field."
		)
	return ToolResult(
		data=data,
		summary=f"created lot {doc.name} for {variety or 'fruit'} off {field or 'no block'}",
		docstatus_delta="none → 0 (created)",
	)


def _company_of_field(field: str) -> str:
	"""The entity a block belongs to, read off `owning_entity`.

	`owning_entity` AND NOT `company`. Field, Parcel, Irrigation Zone and Housing
	Unit spell it that way; a lookup for `company` returns nothing on every site
	and the lot arrives unscoped with no error anywhere.
	"""
	if not field or not compat.doctype_exists(FIELD):
		return ""
	return str(frappe.db.get_value(FIELD, field, "owning_entity") or "")


def _existing_lot(field: str, variety: str, harvest_date: str, company: str) -> str:
	"""The lot already covering this block, variety and day, if there is one."""
	filters = {"harvest_date": harvest_date}
	if field:
		filters["field"] = field
	if variety:
		filters["variety"] = variety
	if company:
		filters["company"] = company
	rows = frappe.db.get_all(DOCTYPE, filters=filters, fields=["name"], limit=1) or []
	return str(rows[0]["name"]) if rows else ""


def _source_rows(raw) -> list:
	"""`source_lots` from a list of codes or a list of dicts.

	TWO SHAPES BECAUSE TWO CALLERS SEND TWO. A list of lot codes is what a model
	writes when it knows the inputs and nothing else; a list of dicts is what a
	pack line sends when it has weighed each contribution, and it is the only
	shape that can carry one.
	"""
	entries = raw if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
	out = []
	for entry in entries:
		if isinstance(entry, dict):
			code = str(entry.get("source_lot") or entry.get("lot_code") or "").strip().upper()
			row = {"source_lot": code}
			if entry.get("quantity_contributed") is not None:
				row["quantity_contributed"] = as_float(
					entry.get("quantity_contributed"), "quantity_contributed"
				)
			if entry.get("quantity_uom"):
				row["quantity_uom"] = str(entry["quantity_uom"])
			if entry.get("note"):
				row["note"] = str(entry["note"])
		else:
			code = str(entry or "").strip().upper()
			row = {"source_lot": code}
		if not code:
			continue
		if not frappe.db.exists(DOCTYPE, code):
			raise ToolError(
				f"no lot called {code!r} on this site. `source_lots` is the transformation edge "
				"and the whole graph is built on it — a typo here is a pallet whose blocks cannot "
				"be found in either direction. list_traceability_lots has the register. Nothing "
				"was created."
			)
		out.append(row)
	return out


# ── the events ──────────────────────────────────────────────────────────────


def _write_cte(
	lot_code: str,
	event_type: str,
	event_datetime: str,
	company: str = "",
	location: str = "",
	description: str = "",
	quantity=None,
	quantity_uom: str = "",
	actor: str = "",
	reference_doctype: str = "",
	reference_name: str = "",
	source_location: str = "",
	destination_location: str = "",
	carrier: str = "",
	receiver: str = "",
) -> dict:
	"""Write one event, or hand back the one already there.

	IDEMPOTENT ON `(lot_code, event_type, reference_doctype, reference_name)`, and
	that tuple is what makes `index_lot_events` safe to re-run. It deliberately
	does NOT include the timestamp: an indexer re-reading a Spray Application
	whose `completed_at` was corrected must not write a second Growing event for
	the same pass — one pass is one event, and the corrected time belongs on the
	spray.

	An event with NO reference is never deduplicated, because there is nothing to
	deduplicate on: two hand-entered Shipping events on one lot are two loads
	that left, and collapsing them would delete one.
	"""
	if not compat.doctype_exists(CTE_DOCTYPE):
		return {}
	if reference_doctype and reference_name:
		existing = frappe.db.get_all(
			CTE_DOCTYPE,
			filters={
				"lot_code": lot_code,
				"event_type": event_type,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
			},
			fields=["name"],
			limit=1,
		)
		if existing:
			return {"name": str(existing[0]["name"]), "created": False, "event_type": event_type}

	doc = frappe.new_doc(CTE_DOCTYPE)
	doc.lot_code = lot_code
	doc.event_type = event_type
	doc.event_datetime = event_datetime or frappe.utils.now()
	doc.company = company or None
	doc.location = location or None
	doc.actor = actor or None
	doc.reference_doctype = reference_doctype or None
	doc.reference_name = reference_name or None
	doc.description = description or None
	doc.quantity = quantity
	doc.quantity_uom = quantity_uom or None
	doc.source_location = source_location or None
	doc.destination_location = destination_location or None
	doc.carrier = carrier or None
	doc.receiver = receiver or None
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return {"name": doc.name, "created": True, "event_type": event_type}


def record_cte(args: dict) -> ToolResult:
	"""File one Critical Tracking Event against one lot.

	THE FIVE EVENT TYPES ARE THE RULE, not this app's taxonomy — see the DocType.
	The Key Data Elements are arguments here for the same reason they are columns
	there: a regulator's request names them, and a record that carries the event
	without them is a record that has to be answered a second time.

	IT NEVER CHANGES THE LOT. A Shipping event does not decrement the lot's
	quantity and does not set its status to Shipped. Those are two different
	measurements taken by two different people, and an event that silently
	rewrote the lot would make the lot's own record disagree with the sum of its
	history — with no way to tell afterwards which one somebody meant. Set the
	status deliberately if the operation wants it set.
	"""
	_require()
	_require_events()
	actor_principal = employee_tool.require_shift_role()

	lot_code = as_str(args, "lot_code", required=True).strip().upper()
	lot = _lot_row(lot_code)

	event_type = as_str(args, "event_type", required=True).strip().title()
	if event_type not in EVENT_TYPES:
		raise ToolError(
			f"{event_type!r} is not one of the rule's five critical tracking events: "
			f"{', '.join(EVENT_TYPES)}. Nothing was created."
		)

	company = resolve_company(as_str(args, "company"), required=False) or str(lot.get("company") or "")
	if company:
		employee_tool.require_company_scope(actor_principal, company)

	actor = as_str(args, "actor")
	if actor:
		actor = employee_tool.resolve_employee(actor)

	reference_doctype = as_str(args, "reference_doctype")
	reference_name = as_str(args, "reference_name")
	unresolved = ""
	if reference_doctype and reference_name and compat.doctype_exists(reference_doctype):
		if not frappe.db.exists(reference_doctype, reference_name):
			unresolved = (
				f"{reference_doctype} {reference_name!r} is not a record on this site. The event "
				"WAS filed — an event refused over a pointer is an event nobody records, and the "
				"reads report an unresolved reference as the data fault it is rather than "
				"dropping it silently."
			)

	quantity = as_float(args.get("quantity"), "quantity") if args.get("quantity") is not None else None
	written = _write_cte(
		lot_code=lot_code,
		event_type=event_type,
		event_datetime=as_str(args, "event_datetime") or as_str(args, "when") or frappe.utils.now(),
		company=company,
		location=as_str(args, "location"),
		description=as_str(args, "description"),
		quantity=quantity,
		quantity_uom=as_str(args, "quantity_uom"),
		actor=actor,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		source_location=as_str(args, "source_location"),
		destination_location=as_str(args, "destination_location"),
		carrier=as_str(args, "carrier"),
		receiver=as_str(args, "receiver"),
	)

	row = dict(frappe.db.get_value(CTE_DOCTYPE, written["name"], CTE_FIELDS, as_dict=True) or {})
	data = {
		"event": row,
		"lot": _describe_lot(lot_code, row=lot, with_events=False),
		"actor": actor_principal,
		"already_recorded": not written.get("created", True),
		"note": (
			f"{written['name']} is a {event_type} event on {lot_code}."
			if written.get("created", True)
			else (
				f"{reference_doctype} {reference_name} was already filed as a {event_type} event "
				f"on {lot_code}, so nothing was written a second time. One source record is one "
				"event; a corrected timestamp belongs on the source record, not on a second CTE."
			)
		),
	}
	if unresolved:
		data["unresolved_reference_note"] = unresolved
	if event_type == "Shipping" and not (data["event"].get("destination_location") or row.get("receiver")):
		data["no_destination_note"] = (
			"This Shipping event names no destination and no receiver. That is product which left "
			"and cannot be traced to anybody — recall_drill reports it as a break rather than as "
			"an empty list, because the honest scope of a recall is wider than the customers it "
			"can name."
		)
	return ToolResult(
		data=data,
		summary=f"{event_type} event on {lot_code}"
		+ (f" ({reference_doctype} {reference_name})" if reference_name else ""),
		docstatus_delta="none → 0 (created)" if written.get("created", True) else "",
	)


# ── reads ───────────────────────────────────────────────────────────────────


def _lot_row(lot_code: str) -> dict:
	"""One lot, or the refusal that says where the register is."""
	row = frappe.db.get_value(DOCTYPE, lot_code, LOT_FIELDS, as_dict=True)
	if not row:
		raise ToolError(
			f"no lot called {lot_code!r} on this site. A lot code is assigned by "
			"create_traceability_lot or by index_lot_events; list_traceability_lots has the "
			"register. Note that a lot code is stored upper-case."
		)
	return dict(row)


def _source_lots_of(lot_code: str) -> list:
	"""This lot's own `source_lots`, read THROUGH THE PARENT.

	Frappe will happily filter a child doctype by `parent` on a bench, and the
	standalone double will as happily return nothing — see the note in
	`traceability.sprays_on` about the same trap from the other side. Loading the
	parent document is the shape that is correct in both.
	"""
	if not compat.doctype_exists(DOCTYPE):
		return []
	try:
		doc = frappe.get_doc(DOCTYPE, lot_code)
	except Exception:
		return []
	out = []
	for row in doc.get("source_lots") or []:
		code = str(getattr(row, "source_lot", "") or (row.get("source_lot") if hasattr(row, "get") else ""))
		if not code:
			continue
		out.append(
			{
				"source_lot": code,
				"source_field": getattr(row, "source_field", None),
				"quantity_contributed": getattr(row, "quantity_contributed", None),
				"quantity_uom": getattr(row, "quantity_uom", None),
				"note": getattr(row, "note", None),
			}
		)
	return out


def _child_lots_of(lot_codes: list) -> dict:
	"""`{source lot: [lots it went into]}`, read off the child table by `source_lot`.

	The OTHER direction, and it cannot be read through a parent because the
	parents are what is being looked for. `source_lot` is a real column so the
	filter is real; `parent` is asked for BY NAME because `compat.existing_fields`
	drops framework columns and a batched child read that loses it files every
	row under one empty key.
	"""
	wanted = [code for code in traceability.distinct(lot_codes) if code]
	if not wanted or not compat.doctype_exists(SOURCE_DOCTYPE):
		return {}
	out: dict = {}
	for row in (
		frappe.db.get_all(
			SOURCE_DOCTYPE,
			filters={"source_lot": ("in", wanted)},
			fields=["parent", "source_lot", "quantity_contributed", "quantity_uom"],
			limit=LOT_CAP,
		)
		or []
	):
		parent = str(row.get("parent") or "")
		if not parent:
			continue
		out.setdefault(str(row.get("source_lot") or ""), []).append(
			{
				"lot_code": parent,
				"quantity_contributed": row.get("quantity_contributed"),
				"quantity_uom": row.get("quantity_uom"),
			}
		)
	return out


def _events_for(lot_codes, event_types=(), limit: int = EVENT_CAP) -> list:
	"""Events for one lot or many, oldest first."""
	wanted = traceability.distinct(lot_codes if isinstance(lot_codes, (list, tuple)) else [lot_codes])
	if not wanted or not compat.doctype_exists(CTE_DOCTYPE):
		return []
	filters: dict = {"lot_code": ("in", wanted)}
	if event_types:
		filters["event_type"] = ("in", list(event_types))
	return [
		dict(row)
		for row in frappe.db.get_all(
			CTE_DOCTYPE,
			filters=filters,
			fields=compat.existing_fields(CTE_DOCTYPE, CTE_FIELDS),
			order_by="event_datetime asc",
			limit=limit,
		)
		or []
	]


def _describe_lot(lot_code: str, row: dict | None = None, with_events: bool = True) -> dict:
	"""One lot as every read here returns it."""
	base = dict(row or _lot_row(lot_code))
	base.setdefault("lot_code", lot_code)
	out = {key: base.get(key) for key in LOT_FIELDS}
	out["lot_code"] = base.get("lot_code") or base.get("name") or lot_code
	out["source_lots"] = _source_lots_of(out["lot_code"])
	if with_events:
		events = _events_for(out["lot_code"])
		out["events"] = events
		out["event_count"] = len(events)
		out["event_types"] = traceability.distinct(entry.get("event_type") for entry in events)
	return out


def get_traceability_lot(args: dict) -> ToolResult:
	"""One lot with every Critical Tracking Event filed against it. Read-only."""
	_require()
	lot_code = as_str(args, "lot_code", required=True).strip().upper()
	described = _describe_lot(lot_code)

	breaks = []
	if not described.get("events"):
		breaks.append(
			_break(
				"the lot",
				"critical tracking events",
				"Nothing has been filed against this lot. The records may still exist — sprays, "
				"bin seals and scale tickets are kept in their own registers and are not events "
				"until something indexes them. index_lot_events attaches what this site already "
				"holds.",
			)
		)
	if not described.get("field") and not described.get("source_lots"):
		breaks.append(
			_break(
				"the lot",
				"an origin",
				"This lot names no block and no source lots, so a backward trace stops at the lot "
				"code itself. It is fruit whose origin was never written down.",
			)
		)
	data = {
		"lot": described,
		"breaks": breaks,
		"note": (
			"An event is a POINTER at the register that holds the detail — get_lot_timeline "
			"resolves those pointers and returns the referenced records' key fields alongside."
		),
	}
	return ToolResult(
		data=data,
		summary=f"{lot_code}: {described.get('event_count', 0)} event(s), status "
		f"{described.get('status') or 'unset'}",
	)


def list_traceability_lots(args: dict) -> ToolResult:
	"""The lot register — by block, variety, day or status. Newest first. Read-only."""
	_require()
	limit = min(as_limit(args), RECORD_CAP)

	filters: dict = {}
	company = resolve_company(as_str(args, "company"), required=False)
	if company:
		filters["company"] = company
	for key, column in (("field", "field"), ("variety", "variety"), ("status", "status")):
		value = as_str(args, key)
		if value:
			filters[column] = value
	for key, column in (
		("harvest_shift", "harvest_shift"),
		("planting_season", "planting_season"),
	):
		value = as_str(args, key)
		if value:
			filters[column] = value
	date_from = as_date(args, "date_from")
	date_to = as_date(args, "date_to")
	if date_from and date_to:
		filters["harvest_date"] = ("between", [date_from, date_to])
	elif date_from:
		filters["harvest_date"] = (">=", date_from)
	elif date_to:
		filters["harvest_date"] = ("<=", date_to)

	rows = (
		frappe.db.get_all(
			DOCTYPE,
			filters=filters,
			fields=compat.existing_fields(DOCTYPE, LOT_FIELDS),
			order_by="harvest_date desc",
			limit=limit + 1,
		)
		or []
	)
	truncated = len(rows) > limit
	rows = rows[:limit]
	lots = [{key: row.get(key) for key in LOT_FIELDS} for row in rows]
	for entry in lots:
		entry["lot_code"] = entry.get("lot_code") or entry.get("name")

	data = {
		"lots": lots,
		"count": len(lots),
		"limit": limit,
		"truncated": truncated,
		"filters": {key: value for key, value in filters.items() if not isinstance(value, tuple)},
		"note": (
			"Events and source lots are NOT on this register — both are per-lot reads and forty "
			"lots would be eighty of them. get_traceability_lot has one lot in full."
		),
	}
	if truncated:
		data["truncated_note"] = (
			f"More than {limit} lots matched. Narrow by block, variety or date — a lot register "
			"is read to answer a question about a block or a week, not to be exported."
		)
	return ToolResult(data=data, summary=f"{len(lots)} traceability lot(s)")


# ── the two traces ──────────────────────────────────────────────────────────


def trace_lot_backward(args: dict) -> ToolResult:
	"""Everything upstream of one lot: its sources, their blocks, and the sprays.

	THE RESIDUE QUESTION, ANSWERED IN ONE CALL. It walks `source_lots` back to the
	lots that came off blocks — the ROOTS of the transformation graph — and then
	asks the spray register what those blocks had been given, bounded at each
	root's own harvest date. A pass made after the fruit came off did not reach
	it, and naming it sends somebody to investigate a tank that was never on that
	crop.

	NOT THE SAME TOOL AS `trace_backward`, which takes a shipment, a bin or a
	scale ticket and walks the free-text bucket chain. That one is untouched and
	still the right call when all anybody has is a bin id.
	"""
	_require()
	lot_code = as_str(args, "lot_code", required=True).strip().upper()
	root = _lot_row(lot_code)

	visited: dict = {lot_code: dict(root)}
	edges: list = []
	frontier = [lot_code]
	depth = 0
	capped = False
	while frontier and depth < LOT_CAP:
		depth += 1
		nxt = []
		for code in frontier:
			for edge in _source_lots_of(code):
				edges.append({"lot_code": code, **edge, "depth": depth})
				source = edge["source_lot"]
				if source in visited:
					continue
				if len(visited) >= LOT_CAP:
					capped = True
					continue
				row = frappe.db.get_value(DOCTYPE, source, LOT_FIELDS, as_dict=True)
				visited[source] = dict(row or {"name": source, "lot_code": source})
				nxt.append(source)
		frontier = nxt

	roots = [code for code, row in visited.items() if not _source_lots_of(code) and row.get("field")]
	blocks = traceability.distinct(row.get("field") for row in visited.values())
	growing = _events_for(list(visited), event_types=("Growing",))

	sprays = []
	for code in visited:
		row = visited[code]
		block = row.get("field")
		if not block:
			continue
		for spray in traceability.sprays_on([block], before=traceability._day(row.get("harvest_date")) or ""):
			sprays.append({**spray, "reached_lot": code, "block": block})

	breaks = []
	if not edges:
		breaks.append(
			_break(
				"the lot",
				"source lots",
				"This lot was not made from other lots, so the trace is one hop: it came off a "
				"block directly. That is the ordinary case for a field lot and is not a fault.",
			)
		)
	if not blocks:
		breaks.append(
			_break(
				"the lot graph",
				"a block",
				"No lot in this chain names a Field. The chain ends at a lot code and cannot "
				"reach the spray register at all — which is the one hop a residue question is "
				"asked through.",
			)
		)
	if blocks and not sprays:
		breaks.append(
			_break(
				"the blocks",
				"spray applications",
				f"No Applied spray reached {', '.join(blocks)} on or before the harvest dates in "
				"this chain. Either nothing was applied, or the passes were filed against block "
				"ids the Spray Application Block table spells differently from the Field docnames "
				"these lots carry.",
			)
		)
	if capped:
		breaks.append(
			_break(
				"the lot graph",
				"the rest of the chain",
				f"The traversal stopped at {LOT_CAP} lots. A transformation graph this deep is "
				"either a season-long chain or a data fault, and the answer is truncated rather "
				"than wrong.",
			)
		)

	data = {
		"lot": _describe_lot(lot_code, row=root, with_events=False),
		"upstream_lots": [
			{key: row.get(key) for key in LOT_FIELDS} for code, row in visited.items() if code != lot_code
		],
		"transformation_edges": edges,
		"root_lots": roots,
		"blocks": blocks,
		"growing_events": growing,
		"spray_applications": sprays,
		"counts": {
			"upstream_lots": max(len(visited) - 1, 0),
			"blocks": len(blocks),
			"growing_events": len(growing),
			"spray_applications": len(sprays),
		},
		"breaks": breaks,
		"note": (
			"Applications are bounded at each root lot's own harvest date — a pass made after the "
			"fruit came off did not reach it. Planned and Cancelled passes are excluded: neither "
			"put anything on the ground."
		),
	}
	return ToolResult(
		data=data,
		summary=(
			f"{lot_code} traces back to {len(blocks)} block(s), {len(sprays)} spray "
			f"application(s), through {max(len(visited) - 1, 0)} upstream lot(s)"
		),
	)


def _forward_closure(lot_code: str) -> tuple:
	"""Every lot downstream of this one, and the edges that got there."""
	visited: dict = {lot_code: 0}
	edges: list = []
	frontier = [lot_code]
	depth = 0
	capped = False
	while frontier and depth < LOT_CAP:
		depth += 1
		children = _child_lots_of(frontier)
		nxt = []
		for parent_code, rows in children.items():
			for entry in rows:
				edges.append({"source_lot": parent_code, "depth": depth, **entry})
				child = entry["lot_code"]
				if child in visited:
					continue
				if len(visited) >= LOT_CAP:
					capped = True
					continue
				visited[child] = depth
				nxt.append(child)
		frontier = nxt
	return visited, edges, capped


def trace_lot_forward(args: dict) -> ToolResult:
	"""Which lots carry this lot's fruit, and WHO HAS THEM. Read-only.

	It walks the transformation graph downwards — this lot, the lots it was made
	into, the lots THOSE were made into — and then reads every Shipping event in
	that closure. The answer is the destinations, the receivers and the carriers:
	the people who have to be telephoned.

	NOT THE SAME TOOL AS `trace_forward`, which takes a block, a spray or a water
	test and walks the free-text bucket chain to the settlements and the invoices.
	That one is untouched and remains the right call when the SOURCE is what is
	suspect and no lot code was ever assigned.

	`recall_drill` is this read written as a document somebody acts on.
	"""
	_require()
	lot_code = as_str(args, "lot_code", required=True).strip().upper()
	root = _lot_row(lot_code)
	visited, edges, capped = _forward_closure(lot_code)

	lot_rows = []
	for code in visited:
		row = frappe.db.get_value(DOCTYPE, code, LOT_FIELDS, as_dict=True)
		lot_rows.append(dict(row or {"name": code, "lot_code": code}))

	shipping = _events_for(list(visited), event_types=("Shipping",))
	destinations = []
	unplaced = 0
	for event in shipping:
		where = event.get("destination_location") or event.get("receiver") or ""
		if not where:
			unplaced += 1
			continue
		destinations.append(
			{
				"destination": event.get("destination_location"),
				"receiver": event.get("receiver"),
				"carrier": event.get("carrier"),
				"shipped_at": event.get("event_datetime"),
				"lot_code": event.get("lot_code"),
				"quantity": event.get("quantity"),
				"quantity_uom": event.get("quantity_uom"),
				"reference": _reference_label(event),
			}
		)

	breaks = []
	if not shipping:
		breaks.append(
			_break(
				"the lot graph",
				"shipping events",
				"No lot in this chain has a Shipping event. Either the fruit has not left, or it "
				"left and nobody recorded where it went — and those are DIFFERENT ANSWERS. An "
				"empty destination list is not a clean bill; check the scale tickets and trade "
				"shipments for this period and run index_lot_events over them.",
			)
		)
	if unplaced:
		breaks.append(
			_break(
				"the shipping events",
				"a destination",
				f"{unplaced} Shipping event(s) name neither a destination nor a receiver. That is "
				"product which left and cannot be traced to anybody — the honest scope of this "
				"recall is wider than the list above.",
			)
		)
	if not edges:
		breaks.append(
			_break(
				"the lot",
				"downstream lots",
				"This lot was not combined into any other lot. That is the ordinary case for a "
				"field lot that shipped as itself, and is not a fault.",
			)
		)
	if capped:
		breaks.append(
			_break(
				"the lot graph",
				"the rest of the chain",
				f"The traversal stopped at {LOT_CAP} lots and the answer is truncated rather than wrong.",
			)
		)

	data = {
		"lot": _describe_lot(lot_code, row=root, with_events=False),
		"downstream_lots": [row for row in lot_rows if (row.get("name") or row.get("lot_code")) != lot_code],
		"transformation_edges": edges,
		"shipping_events": shipping,
		"destinations": destinations,
		"counts": {
			"downstream_lots": max(len(visited) - 1, 0),
			"shipping_events": len(shipping),
			"destinations": len(destinations),
			"unplaced_shipments": unplaced,
		},
		"breaks": breaks,
	}
	return ToolResult(
		data=data,
		summary=(
			f"{lot_code} reaches {len(destinations)} destination(s) through "
			f"{max(len(visited) - 1, 0)} downstream lot(s)"
		),
	)


def _reference_label(event: dict) -> str:
	doctype = str(event.get("reference_doctype") or "")
	name = str(event.get("reference_name") or "")
	return f"{doctype} {name}".strip()


def recall_drill(args: dict) -> ToolResult:
	"""The twenty-four-hour answer: everywhere this lot went, and who has it.

	THE TOOL THE WHOLE FEATURE IS FOR. A buyer telephones, or a residue result
	comes back, and the operation has one day to say where the fruit is. This runs
	the forward trace and writes it as the thing somebody acts on: a list of
	parties to contact, each with what they received, when, and under which lot
	code — plus, stated FIRST rather than omitted, the fruit that cannot be
	placed.

	IT WRITES NOTHING AND SETS NOTHING TO RECALLED. A drill is run on fruit
	nobody is worried about — that is what makes it a drill — and a read that
	changed a status would make the rehearsal indistinguishable from the event.
	Setting the lot's status is a deliberate, separate act.

	READINESS IS REPORTED AS A COUNT, NOT A VERDICT. This app computes no opinion
	about whether an operation is compliant; it says how many lots were reached,
	how many parties can be named, and how many shipments name nobody. An
	operation with unplaced shipments has a real gap and now knows the size of it.
	"""
	forward = trace_lot_forward(args)
	payload = forward.data
	lot_code = payload["lot"]["lot_code"]

	parties: dict = {}
	for entry in payload["destinations"]:
		key = str(entry.get("receiver") or entry.get("destination") or "")
		bucket = parties.setdefault(
			key,
			{
				"party": key,
				"destination": entry.get("destination"),
				"receiver": entry.get("receiver"),
				"carriers": [],
				"lot_codes": [],
				"shipments": [],
				"first_shipped_at": entry.get("shipped_at"),
				"last_shipped_at": entry.get("shipped_at"),
			},
		)
		if entry.get("carrier") and entry["carrier"] not in bucket["carriers"]:
			bucket["carriers"].append(entry["carrier"])
		if entry.get("lot_code") and entry["lot_code"] not in bucket["lot_codes"]:
			bucket["lot_codes"].append(entry["lot_code"])
		if entry.get("reference"):
			bucket["shipments"].append(entry["reference"])
		when = str(entry.get("shipped_at") or "")
		if when:
			bucket["first_shipped_at"] = min(str(bucket["first_shipped_at"] or when), when)
			bucket["last_shipped_at"] = max(str(bucket["last_shipped_at"] or when), when)

	lots_affected = [lot_code] + [
		str(row.get("name") or row.get("lot_code")) for row in payload["downstream_lots"]
	]
	unplaced = payload["counts"]["unplaced_shipments"]

	data = {
		"lot": payload["lot"],
		"parties_to_notify": list(parties.values()),
		"lots_affected": lots_affected,
		"shipping_events": payload["shipping_events"],
		"counts": {
			"lots_affected": len(lots_affected),
			"parties_to_notify": len(parties),
			"shipping_events": payload["counts"]["shipping_events"],
			"unplaced_shipments": unplaced,
		},
		"breaks": payload["breaks"],
		"note": (
			"This is a READ. Nothing was recalled, no status changed and no message was sent — a "
			"drill is run on fruit nobody is worried about, and a read that changed a status "
			"would make the rehearsal indistinguishable from the event."
		),
	}
	if unplaced:
		data["scope_warning"] = (
			f"{unplaced} shipment(s) of this lot name nobody. The parties listed here are the ones "
			"that CAN be named, and the true scope of the recall is wider. Fixing this is a "
			"records problem, not a query problem: a Shipping event needs a destination or a "
			"receiver at the moment the load leaves."
		)
	if not parties:
		data["scope_warning"] = (
			"No party can be named for this lot. Either the fruit has not left, or every "
			"departure was recorded without a destination — and those are different answers. "
			"Do not read this as a clean bill."
		)
	return ToolResult(
		data=data,
		summary=(
			f"recall drill {lot_code}: {len(parties)} party(ies) to notify across "
			f"{len(lots_affected)} lot(s)" + (f", {unplaced} shipment(s) name nobody" if unplaced else "")
		),
	)


def _reference_detail(doctype: str, name: str) -> dict:
	"""The referenced record's key fields, or why it could not be read.

	THREE OUTCOMES AND THEY ARE DIFFERENT. A doctype this site does not have; a
	docname that does not exist in it; and a record that is there. The first two
	are reported as what they are rather than collapsed into an empty dict — a
	pointer at a register that was uninstalled is a different problem from a
	pointer at a record that was deleted, and only one of them means somebody
	deleted evidence.
	"""
	if not doctype or not name:
		return {}
	if not compat.doctype_exists(doctype):
		return {
			"resolved": False,
			"reason": f"{doctype} is not a DocType on this site — the register may not be installed.",
		}
	fields = REFERENCE_FIELDS.get(doctype) or ("name",)
	row = frappe.db.get_value(doctype, name, compat.existing_fields(doctype, fields), as_dict=True)
	if not row:
		return {
			"resolved": False,
			"reason": f"{doctype} {name!r} is not a record on this site — it was deleted, renamed "
			"or never existed. The event is kept and the fault is reported rather than dropped.",
		}
	return {"resolved": True, **dict(row)}


def get_lot_timeline(args: dict) -> ToolResult:
	"""One lot's events in order, with the referenced records' details resolved.

	THE DOCUMENT AN AUDIT ASKS FOR. `get_traceability_lot` returns the events;
	this returns them chronologically WITH the register rows they point at, so the
	answer to "produce the records for lot X" is one call rather than one call
	plus one read per event.

	The chronology is `event_datetime` — when the event HAPPENED — and not the
	creation order. A phone that posts an afternoon of events when the signal
	comes back would otherwise produce a timeline that reads in the order the
	bars returned.
	"""
	_require()
	lot_code = as_str(args, "lot_code", required=True).strip().upper()
	lot = _lot_row(lot_code)
	limit = min(as_limit(args), EVENT_CAP)
	events = _events_for(lot_code, limit=limit + 1)
	truncated = len(events) > limit
	events = events[:limit]

	unresolved = 0
	timeline = []
	for event in events:
		detail = _reference_detail(
			str(event.get("reference_doctype") or ""), str(event.get("reference_name") or "")
		)
		if detail and not detail.get("resolved"):
			unresolved += 1
		timeline.append({**event, "reference_detail": detail})

	breaks = []
	if not timeline:
		breaks.append(
			_break(
				"the lot",
				"critical tracking events",
				"Nothing has been filed against this lot, so there is no timeline to produce. "
				"index_lot_events attaches the sprays, bin seals and scale tickets this site "
				"already holds.",
			)
		)
	if unresolved:
		breaks.append(
			_break(
				"the events",
				"their referenced records",
				f"{unresolved} event(s) point at a record this site cannot read. Each one says "
				"why in `reference_detail.reason` — an uninstalled register and a deleted record "
				"are different problems, and only one of them means evidence went missing.",
			)
		)
	types = traceability.distinct(entry.get("event_type") for entry in timeline)
	for wanted in ("Growing", "Shipping"):
		if timeline and wanted not in types:
			breaks.append(
				_break(
					"the timeline",
					f"a {wanted} event",
					f"This lot has no {wanted} event. "
					+ (
						"Nothing links it to what was applied to the block it came off, which is "
						"the hop a residue question is asked through."
						if wanted == "Growing"
						else "Either it has not left, or its departure was never recorded."
					),
				)
			)

	data = {
		"lot": _describe_lot(lot_code, row=lot, with_events=False),
		"timeline": timeline,
		"count": len(timeline),
		"event_types": types,
		"truncated": truncated,
		"breaks": breaks,
		"note": (
			"Ordered by when each event HAPPENED, not by when it was written. An event is a "
			"pointer; `reference_detail` is the register row it points at, read live."
		),
	}
	return ToolResult(
		data=data, summary=f"{lot_code}: {len(timeline)} event(s) from {', '.join(types) or 'nothing'}"
	)


# ── indexing what this site already holds ───────────────────────────────────


def index_lot_events(args: dict) -> ToolResult:
	"""Attach the sprays, bin seals and scale tickets already here to lot codes.

	  WHAT THIS REPLACES, AND WHY IT IS NOT A `doc_events` HOOK. `hooks.py` promises
	  this app installs none and `tests_standalone/test_hooks.py` fails the build
	  over one; `tools/itgc.py` settled the identical question the identical way.
	  Running the indexing from the tool layer means an operator can see it, switch
	  it off, run it over last season, and run it twice without consequence.

	  WHAT IT DOES, IN ORDER, OVER ONE WINDOW:

	1. BIN SEALS become lots. A seal names a block, a crop and a day, which is
	   exactly a lot; where no lot covers that combination one is CREATED, and
	   either way the seal is filed as a RECEIVING event. This is the step that
	   gets an operation from nothing to a lot register in one call.
	2. SCALE TICKETS become SHIPPING events. A ticket is a load weighed onto
	   somebody else's scale — the grower's fruit arriving at the packer — so
	   its `customer` is the receiver and its `destination` is where the fruit
	   went. That is a departure from this operation, and it is what makes
	   `recall_drill` able to name anybody at all.
	3. SPRAY APPLICATIONS become GROWING events, on every lot whose block the
	   pass reached and whose harvest date is on or after it. A pass applied
	   after the fruit came off did not reach it.

	  WHAT IT DOES NOT DO. It does not index Trade Shipments. A Trade Shipment
	  carries no lot column and this release does not add one to a doctype it would
	  then have to keep — see the module docstring on staying additive. File those
	  with `record_cte`, which is one call per shipment and names the lots
	  deliberately rather than guessing them off a date.

	  IDEMPOTENT THROUGHOUT. Lots are matched on `(field, variety, harvest_date,
	  company)` and events on `(lot, event_type, reference_doctype,
	  reference_name)`, so a second sweep over the same window writes nothing and
	  says so.
	"""
	_require()
	_require_events()
	actor = employee_tool.require_shift_role()

	company = resolve_company(as_str(args, "company"), required=False)
	if company:
		employee_tool.require_company_scope(actor, company)
	date_from = as_date(args, "date_from")
	date_to = as_date(args, "date_to")
	if not date_from or not date_to:
		raise ToolError(
			"date_from and date_to are both required. A sweep with no window is a sweep over "
			"every record on the site, which on a bench with three seasons of history is a "
			"request that does not finish — and an operation that wants three seasons indexed "
			"wants it done a week at a time so it can read what happened. Nothing was written."
		)
	if date_from > date_to:
		raise ToolError(f"date_from {date_from} is after date_to {date_to}. Nothing was written.")

	created_lots: list = []
	events: list = []
	skipped: dict = {"bin_seals_without_a_block": 0, "scale_tickets_without_a_lot": 0}
	notes: list = []

	# ── 1. bin seals → lots + Receiving ─────────────────────────────────────
	seals = _sweep_rows(
		BIN_SEAL,
		{"sealed_at": ("between", [f"{date_from} 00:00:00", f"{date_to} 23:59:59"])},
		("name", "bin_tag", "bucket_count", "sealed_at", "field", "crop", "company", "sealed_by", "shift"),
		company,
	)
	for seal in seals:
		block = str(seal.get("field") or "")
		if not block:
			skipped["bin_seals_without_a_block"] += 1
			continue
		day = traceability._day(seal.get("sealed_at")) or date_from
		variety = str(seal.get("crop") or "")
		scope = str(seal.get("company") or company or "") or _company_of_field(block)
		lot = _ensure_lot(block, variety, day, scope, created_lots)
		events.append(
			_write_cte(
				lot_code=lot,
				event_type="Receiving",
				event_datetime=str(seal.get("sealed_at") or f"{day} 00:00:00"),
				company=scope,
				location=block,
				description=f"Bin {seal.get('bin_tag')} sealed with {seal.get('bucket_count')} bucket(s).",
				quantity=seal.get("bucket_count"),
				quantity_uom="bucket",
				actor=str(seal.get("sealed_by") or ""),
				reference_doctype=BIN_SEAL,
				reference_name=str(seal["name"]),
				source_location=block,
			)
		)

	# ── 2. scale tickets → Shipping ─────────────────────────────────────────
	tickets = _sweep_rows(
		SCALE_TICKET,
		{"date": ("between", [date_from, date_to])},
		(
			"name",
			"ticket_number",
			"date",
			"customer",
			"variety",
			"net_weight",
			"weight_uom",
			"destination",
			"field",
			"block",
			"company",
			"status",
		),
		company,
	)
	for ticket in tickets:
		block = str(ticket.get("field") or "")
		day = traceability._day(ticket.get("date")) or date_from
		scope = str(ticket.get("company") or company or "")
		lot = _match_lot(block, str(ticket.get("variety") or ""), day, scope)
		if not lot:
			skipped["scale_tickets_without_a_lot"] += 1
			continue
		events.append(
			_write_cte(
				lot_code=lot,
				event_type="Shipping",
				event_datetime=f"{day} 00:00:00",
				company=scope,
				location=block or str(ticket.get("block") or ""),
				description=(
					f"Scale ticket {ticket.get('ticket_number') or ticket['name']} — "
					f"{ticket.get('net_weight')} {ticket.get('weight_uom') or ''} delivered."
				),
				quantity=ticket.get("net_weight"),
				quantity_uom=str(ticket.get("weight_uom") or ""),
				reference_doctype=SCALE_TICKET,
				reference_name=str(ticket["name"]),
				source_location=block or str(ticket.get("block") or ""),
				destination_location=str(ticket.get("destination") or ""),
				receiver=str(ticket.get("customer") or ""),
			)
		)

	# ── 3. spray applications → Growing ─────────────────────────────────────
	lot_rows = _sweep_rows(
		DOCTYPE,
		{"harvest_date": ("between", [date_from, date_to])},
		("name", "lot_code", "field", "harvest_date", "company"),
		company,
	)
	for row in lot_rows:
		block = str(row.get("field") or "")
		if not block:
			continue
		harvest = traceability._day(row.get("harvest_date")) or date_to
		for spray in traceability.sprays_on([block], before=harvest):
			events.append(
				_write_cte(
					lot_code=str(row.get("lot_code") or row["name"]),
					event_type="Growing",
					event_datetime=str(spray.get("completed_at") or f"{harvest} 00:00:00"),
					company=str(row.get("company") or company or ""),
					location=block,
					description=(
						f"Spray application {spray['name']} reached this block on or before "
						f"harvest. Products and rates are on the application itself."
					),
					reference_doctype=SPRAY,
					reference_name=str(spray["name"]),
					source_location=block,
				)
			)

	written = [entry for entry in events if entry and entry.get("created")]
	already = [entry for entry in events if entry and not entry.get("created")]

	if skipped["bin_seals_without_a_block"]:
		notes.append(
			f"{skipped['bin_seals_without_a_block']} bin seal(s) in this window name no Field. A "
			"seal without a block cannot be filed under a lot, because the lot code IS a block "
			"and a day — those bins are traceable to a shift and no further."
		)
	if skipped["scale_tickets_without_a_lot"]:
		notes.append(
			f"{skipped['scale_tickets_without_a_lot']} scale ticket(s) matched no lot. A ticket is "
			"filed as a Shipping event against the lot covering its block, variety and day; where "
			"no such lot exists, the load left and nothing here can say which fruit it was. "
			"Creating the lots first — or widening the window to include the bin seals from those "
			"days — closes this."
		)
	notes.append(
		"Trade Shipments are NOT indexed by this sweep: a shipment carries no lot column and "
		"guessing its lots off a date would put fruit on a truck it was never on. Use record_cte."
	)

	data = {
		"window": {"date_from": date_from, "date_to": date_to},
		"company": company,
		"lots_created": created_lots,
		"events_written": written,
		"events_already_present": [entry["name"] for entry in already],
		"counts": {
			"bin_seals_read": len(seals),
			"scale_tickets_read": len(tickets),
			"lots_in_window": len(lot_rows),
			"lots_created": len(created_lots),
			"events_written": len(written),
			"events_already_present": len(already),
		},
		"skipped": skipped,
		"actor": actor,
		"notes": notes,
	}
	return ToolResult(
		data=data,
		summary=(
			f"indexed {date_from}..{date_to}: {len(created_lots)} lot(s) created, "
			f"{len(written)} event(s) written, {len(already)} already present"
		),
		docstatus_delta="none → 0 (created)" if (created_lots or written) else "",
	)


def _sweep_rows(doctype: str, filters: dict, fields: tuple, company: str) -> list:
	"""One register's rows inside the sweep window, company-scoped where it can be."""
	if not compat.doctype_exists(doctype):
		return []
	scoped = dict(filters)
	if company and compat.has_field(doctype, "company"):
		scoped["company"] = company
	return [
		dict(row)
		for row in frappe.db.get_all(
			doctype,
			filters=scoped,
			fields=compat.existing_fields(doctype, fields),
			limit=SWEEP_CAP,
		)
		or []
	]


def _match_lot(field: str, variety: str, day: str, company: str) -> str:
	"""The lot covering this block, variety and day — variety first, then without.

	THE FALLBACK IS THE POINT. A bin seal writes `crop` ('Cherry') and a scale
	ticket writes `variety` ('Bing'), and the two are genuinely different words
	for overlapping things because a packer writes what a packer writes. Matching
	on block and day alone after the exact match fails is what stops a whole day's
	tickets falling on the floor over a vocabulary mismatch nobody agreed.
	"""
	if not field:
		return ""
	exact = _existing_lot(field, variety, day, company)
	if exact:
		return exact
	return _existing_lot(field, "", day, company)


def _ensure_lot(field: str, variety: str, day: str, company: str, created: list) -> str:
	"""The lot for this block, variety and day — creating it where there is none."""
	found = _match_lot(field, variety, day, company)
	if found:
		return found
	doc = frappe.new_doc(DOCTYPE)
	doc.lot_code = generate_lot_code(field, variety, day)
	doc.field = field
	doc.variety = variety or None
	doc.harvest_date = day
	doc.company = company or None
	doc.status = "Active"
	doc.notes = "Created by index_lot_events from the bin seals for this block and day."
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	created.append(doc.name)
	_write_cte(
		lot_code=doc.name,
		event_type="Creating",
		event_datetime=f"{day} 00:00:00",
		company=company,
		location=field,
		description=f"Lot {doc.name} created by index_lot_events from the bin seals for {field}.",
	)
	return doc.name
