# SPDX-License-Identifier: MIT
"""Trade documentation across three tiers, from one sales desk.

WHAT THIS IS FOR. Fruit leaves a farm three ways: on a truck to the packing
house down the road, on a truck across a state line, and in a reefer on a
vessel. The fruit is the same fruit. The paperwork is not — a local delivery
needs a scale ticket, a receipt, an invoice and a grade; an interstate load
picks up federal food-safety and cold-chain records; an export picks up
everything a border asks for, most of which is filed in somebody else's system
before the container is sealed.

THE MISTAKE THIS MODULE EXISTS TO PREVENT is running those three out of three
places. A desk that keeps local deliveries in a spreadsheet, interstate freight
in a folder and exports in a broker's portal is a desk where the export
paperwork is the only paperwork anybody checks — because it is the only one that
lives somewhere that looks like paperwork. Then a domestic load moves without a
cold-chain record and nobody notices until a buyer asks for one, by which time
the truck arrived three weeks ago and the record cannot be made honestly.

So the tier decides how much paper, not which system. One `Trade Shipment`, one
checklist built from the destination's own rules, one register of documents.

────────────────────────────────────────────────────────────────────────────
CONFIG, NOT CODE — WHICH IS THE ONLY WAY THIS SURVIVES CONTACT WITH TRADE
────────────────────────────────────────────────────────────────────────────

A farm that lands a buyer in a country it has never shipped to before needs
documents it has never issued before, and it needs them this week. If that is a
release, the release is the bottleneck and the desk goes back to the broker's
portal. So:

  * a KIND of document is a `Trade Document Template` — its schema, where it
    populates from, who signs it, whose system files it;
  * a DESTINATION'S DEMANDS are `Destination Document Requirement` rows —
    shipping here needs that;
  * a SHIPMENT'S CHECKLIST is built from those rules at creation.

Adding Vietnam is adding rows. Nothing in this file knows the name of a country.

────────────────────────────────────────────────────────────────────────────
ADVISORY BY DEFAULT, ENFORCED WHERE AN OPERATION ASKS FOR IT
────────────────────────────────────────────────────────────────────────────

`update_shipment_status` is the only gate here, and it guards one transition:
Ready to Ship. Everything else reports and refuses nothing.

AND IT SHIPS OFF. `trade_document_enforcement` on ERPNext MCP Settings defaults
to advisory, because a two-truck operation locked out of its own delivery by a
phytosanitary certificate it will never need would turn this module off within a
week — and an operation that has turned the module off gets no warnings either.
Advisory mode returns the identical readiness answer and lets the release
through with the gaps named. An operation big enough to want the gate turns it
on, per site or per shipment.

THE OVERRIDE IS RECORDED. Where enforcement is on, releasing anyway needs
`override_reason`, and it is written to the shipment. A bypass nobody recorded is
a bypass nobody can review, which is the difference between an advisory control
and no control at all.

────────────────────────────────────────────────────────────────────────────
WHAT THIS APP DOES NOT DO, STATED HERE SO NOBODY DISCOVERS IT AT A PORT
────────────────────────────────────────────────────────────────────────────

IT FILES NOTHING. An ePhyto is lodged in PCIT, an EEI in AES, an eBL on a DCSA
platform. This app records that somebody filed and what reference came back, and
a document whose template says it needs an external filing and which has no
reference is reported INCOMPLETE — never quietly counted as done. A module that
implied it had transmitted a certificate would be the most dangerous thing in
this repository.

The field names follow the standards so a broker's schema and this app's can be
reconciled by reading: IPPC/ISPM-12 for ePhyto, the DCSA data model for eBL,
15 CFR 30 EEI for AES, WCO origin criteria for a certificate of origin. They are
the standards' NAMES, not an implementation of the standards' transports.
"""

from __future__ import annotations

import hashlib
import json

import frappe

from .. import compat, security, settings
from ..args import as_bool, as_date, as_filter, as_int, as_limit, as_str, resolve_company
from ..erpnext_mcp.doctype.destination_document_requirement.destination_document_requirement import (
	destination_label,
	normalise_country,
)
from ..erpnext_mcp.doctype.trade_document.trade_document import (
	APPROVED,
	PENDING_REVIEW,
	SATISFYING,
	SEALED,
	SEALED_FIELDS,
	VOID,
	normalise_status,
	parse_data,
)
from ..erpnext_mcp.doctype.trade_document.trade_document import (
	DRAFT as DOC_DRAFT,
)
from ..erpnext_mcp.doctype.trade_document_template.trade_document_template import (
	INTERNATIONAL,
	TIERS,
	applies_to_tier,
	normalise_tier,
	parse_tiers,
)
from ..erpnext_mcp.doctype.trade_shipment.trade_shipment import (
	ADVISORY,
	CANCELLED,
	DELIVERED,
	ENFORCED,
	GATED_TRANSITION,
	IN_TRANSIT,
	READY_TO_SHIP,
	SITE_DEFAULT,
	normalise_shipment_status,
)
from ..erpnext_mcp.doctype.trade_shipment.trade_shipment import (
	TRANSITIONS as SHIPMENT_TRANSITIONS,
)
from ..errors import ToolError
from ..result import ToolResult
from . import signing_evidence, wizards
from .trade_seed import SHIPPED_REQUIREMENTS, SHIPPED_TEMPLATES

SHIPMENT = "Trade Shipment"
DOCUMENT = "Trade Document"
TEMPLATE = "Trade Document Template"
REQUIREMENT = "Destination Document Requirement"
GOVERNANCE_DOCUMENT = "Governance Document"

#: Who may approve or seal a trade document. A commercial invoice is a customs
#: declaration and a phytosanitary certificate is a claim about a pest; neither
#: is a picker's to sign off. `Sales Manager` and `Accounts Manager` are
#: ERPNext's own roles and are here because the sales desk this module is
#: operated from is usually one of them.
TRADE_ROLES = (
	"System Manager",
	"Farm Manager",
	"Compliance Officer",
	"Sales Manager",
	"Accounts Manager",
)

#: Hard cap on a packet's document list. A shipment with more documents than
#: this is a shipment somebody has been generating placeholders against in a
#: loop, and a bundle nobody can read is not evidence.
PACKET_CAP = 200

#: Bookkeeping columns that never belong in a seal. Distinct from
#: `SEALED_FIELDS` (an allow-list): this is the belt to that brace, applied when
#: a caller hands in their own field list.
_NEVER_SEALED = ("document_hash", "hashed_fields", "sealed_by", "sealed_at", "signing_evidence", "status")


def available() -> bool:
	try:
		return compat.doctype_exists(SHIPMENT) and compat.doctype_exists(DOCUMENT)
	except Exception:  # pragma: no cover
		return False


def _require() -> None:
	compat.require_doctype(
		SHIPMENT,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)
	compat.require_doctype(DOCUMENT, "Run `bench --site <site> migrate`.")


# ── the principal ───────────────────────────────────────────────────────────
def require_trade_role() -> str:
	"""The principal this call is attributed to, once it has proved it may certify.

	The same shape as `employee.require_hr_role`, and the same argument: on the
	MCP transport the principal is the MCP System User an operator configured and
	granted roles to in the Desk; on the mobile transport it is whoever Frappe
	authenticated. Gating on whichever is present is what makes the check mean
	something on both instead of nothing on one.
	"""
	actor = security.caller_identity() or str(getattr(frappe.session, "user", "") or "")
	if not actor or actor == "Guest":
		raise ToolError(
			"this call has no identity to attribute an approval to. Approving a trade "
			"document is an attestation — somebody is certifying something to a customer or "
			"a border — so it cannot be anonymous. Nothing was changed."
		)
	held = set(frappe.get_roles(actor) or [])
	if not held & set(TRADE_ROLES):
		raise ToolError(
			f"{actor} may not approve or seal trade documents: it holds none of "
			f"{', '.join(TRADE_ROLES)}. A commercial invoice is a customs declaration and a "
			f"phytosanitary certificate is a claim about a pest. This is the account this app "
			f"acts as — an operator sets it with `mcp_system_user` on ERPNext MCP Settings and "
			f"grants it a role in the Desk. Nothing was changed."
		)
	return actor


# ── enforcement ─────────────────────────────────────────────────────────────
def site_enforcement() -> bool:
	"""Whether this site holds shipments for their paperwork. Ships OFF."""
	try:
		return settings.trade_document_enforcement()
	except Exception:  # pragma: no cover - settings not migrated
		return False


def enforcement_for(shipment: dict) -> tuple:
	"""`(enforced: bool, source: str)` for one shipment.

	The source is returned because every answer that turns on this decision says
	where the decision came from. An operator looking at a shipment that let a
	gap through wants to know whether that was the site's setting or this
	shipment's own override, and computing it twice in two places is how those
	two answers come to differ.
	"""
	mode = str(shipment.get("enforcement") or SITE_DEFAULT)
	if mode == ENFORCED:
		return True, "this shipment's own Enforced setting"
	if mode == ADVISORY:
		return False, "this shipment's own Advisory setting"
	if site_enforcement():
		return True, "the site's trade_document_enforcement setting"
	return False, "the site's trade_document_enforcement setting, which is advisory"


# ── language ────────────────────────────────────────────────────────────────
def _resolve_label(row: dict, base: str, language: str, missing: list, where: str) -> str:
	"""One translatable string, falling back to English and saying so.

	The identical contract `get_wizard_definition` has, and deliberately not a
	second implementation of the idea: a missing Spanish label is REPORTED in
	`untranslated` rather than silently served in English, because a sales desk
	run in Spanish that quietly gets English strings is a desk where nobody finds
	out until somebody misreads a document type.
	"""
	wanted = row.get(f"{base}_{language}") if language else None
	if wanted:
		return str(wanted)
	english = row.get(f"{base}_en")
	if language and language != "en" and english:
		missing.append({"where": where, "key": base, "language": language})
	return str(english or "")


def _language(args: dict) -> str:
	explicit = as_str(args, "language")
	if explicit:
		return explicit.strip().lower()[:12]
	return (
		wizards.preferred_language(
			user=as_str(args, "user") or str(getattr(frappe.session, "user", "") or ""),
			employee=as_str(args, "employee"),
		)
		or "en"
	)


# ── small shared readers ────────────────────────────────────────────────────
def _load(doctype: str, name: str, missing: str) -> dict:
	"""One whole record as a plain dict, child rows included.

	`frappe.get_doc(...).as_dict()` rather than `db.get_value(..., "*")` because
	the checklist is a CHILD TABLE and a column read never returns one — the
	shipment's documents would come back absent rather than empty, which is the
	same shape as a shipment that needs nothing.
	"""
	if not name:
		raise ToolError(missing)
	if not frappe.db.exists(doctype, name):
		raise ToolError(f"{doctype} {name!r} does not exist.")
	return dict(frappe.get_doc(doctype, name).as_dict())


def _shipment_row(name: str) -> dict:
	if not name:
		raise ToolError("shipment is required")
	if not frappe.db.exists(SHIPMENT, name):
		raise ToolError(f"{SHIPMENT} {name!r} does not exist. list_shipments has the register.")
	return dict(frappe.get_doc(SHIPMENT, name).as_dict())


def _document_row(name: str) -> dict:
	if not name:
		raise ToolError("trade_document is required")
	if not frappe.db.exists(DOCUMENT, name):
		raise ToolError(f"{DOCUMENT} {name!r} does not exist. list_trade_documents has the register.")
	return dict(frappe.get_doc(DOCUMENT, name).as_dict())


def _template_row(name: str) -> dict:
	if not name or not frappe.db.exists(TEMPLATE, name):
		raise ToolError(
			f"{TEMPLATE} {name!r} does not exist. list_trade_document_templates has the "
			f"register, and create_trade_document_template adds one — a new kind of paper is "
			f"a row, not a release."
		)
	return dict(frappe.get_doc(TEMPLATE, name).as_dict())


def _checklist_rows(shipment) -> list:
	"""A shipment's checklist lines, in display order.

	Takes either a docname or an already-loaded row, because most callers have
	the shipment in hand and re-reading it per call would be three loads of the
	same record to answer one question about it.
	"""
	row = _shipment_row(shipment) if isinstance(shipment, str) else dict(shipment)
	lines = []
	for entry in row.get("documents") or []:
		entry = dict(entry)
		lines.append(
			{
				"name": entry.get("name"),
				"template": entry.get("template"),
				"document_type": entry.get("document_type"),
				"required": entry.get("required"),
				"trade_document": entry.get("trade_document"),
				"status": entry.get("status"),
				"sequence": entry.get("sequence"),
				"notes": entry.get("notes"),
				"idx": entry.get("idx") or 0,
			}
		)
	lines.sort(key=lambda line: (int(line.get("sequence") or 50), int(line.get("idx") or 0)))
	return lines


def _documents_of(shipment: str) -> list:
	rows = frappe.db.get_all(
		DOCUMENT,
		filters={"shipment": shipment},
		fields=[
			"name",
			"title",
			"status",
			"document_type",
			"template",
			"required",
			"expires_on",
			"issued_on",
			"requires_external_filing",
			"external_system",
			"external_reference",
			"document_hash",
			"sealed_at",
			"approved_by",
			"approved_on",
			"signing_evidence",
		],
		order_by="creation asc",
	)
	return [dict(row) for row in rows]


# ── the requirement lookup ──────────────────────────────────────────────────
def requirements_for(tier: str, country: str = "", company: str = "") -> list:
	"""Every rule that applies to a destination, tier rules and country rules both.

	THE COUNTRY'S RULES ADD TO THE TIER'S RATHER THAN REPLACING THEM. The paper a
	country asks for sits on top of the paper an export always needs; a lookup
	that returned only the country's rows would drop the AES declaration from
	every shipment to a country somebody had written a rule for, which is exactly
	the shipments most likely to need it.

	COMPANY-SCOPED RULES ADD TO UNSCOPED ONES for the same reason, and a rule for
	the same template at both scopes resolves to the company's — the more
	specific rule is the one somebody wrote on purpose.
	"""
	tier = normalise_tier(tier)
	country = normalise_country(country)
	wanted = [{"destination_tier": tier, "destination_country": ""}]
	if country and tier == INTERNATIONAL:
		wanted.append({"destination_tier": tier, "destination_country": country})

	seen = {}
	for filters in wanted:
		rows = frappe.db.get_all(
			REQUIREMENT,
			filters={**filters, "enabled": 1},
			fields=[
				"name",
				"destination_tier",
				"destination_country",
				"trade_document_template",
				"required",
				"company",
				"sequence",
				"notes",
			],
			order_by="sequence asc",
		)
		for row in rows:
			row = dict(row)
			scope = str(row.get("company") or "")
			if scope and company and scope != company:
				continue
			if scope and not company:
				# A company-scoped rule on a lookup that named no company is not
				# applied. Applying it would mean a shipment for one entity
				# picking up another entity's arrangements.
				continue
			key = row["trade_document_template"]
			previous = seen.get(key)
			if previous is None:
				seen[key] = row
				continue
			# More specific wins: a country rule over a tier rule, a company
			# rule over an unscoped one.
			previous_rank = (bool(previous.get("destination_country")), bool(previous.get("company")))
			this_rank = (bool(row.get("destination_country")), bool(row.get("company")))
			if this_rank > previous_rank:
				seen[key] = row
	out = list(seen.values())
	out.sort(key=lambda row: (int(row.get("sequence") or 50), str(row.get("trade_document_template") or "")))
	return out


def _templates_by_name(names) -> dict:
	if not names:
		return {}
	rows = frappe.db.get_all(
		TEMPLATE,
		filters={"name": ("in", list(names))},
		fields=[
			"name",
			"document_type",
			"label_en",
			"label_es",
			"description_en",
			"description_es",
			"enabled",
			"applicable_tiers",
			"requires_signature",
			"signature_role",
			"requires_external_filing",
			"external_system",
			"standard_reference",
			"auto_populate_from",
			"auto_populate_map",
			"required_fields",
			"sequence",
			"notes",
		],
	)
	return {row["name"]: dict(row) for row in rows}


# ── 1. create_shipment ──────────────────────────────────────────────────────
def create_shipment(args: dict) -> ToolResult:
	"""Open a shipment and build its document checklist from the destination.

	THE CHECKLIST IS BUILT ONCE, HERE, and is not silently rebuilt afterwards —
	see `get_shipment_readiness`, which reports drift instead. A destination's
	rules changing in March must not quietly add a requirement to a February
	shipment that has already sailed.
	"""
	_require()
	tier = normalise_tier(as_str(args, "destination_tier", required=True))
	country = normalise_country(as_str(args, "destination_country"))
	company = resolve_company(as_str(args, "company"), required=True)

	if tier == INTERNATIONAL and not country:
		raise ToolError(
			"destination_country is required for an International shipment. The checklist is "
			"looked up by country, so a shipment without one gets only the rules that apply to "
			"every export — a shorter checklist that looks exactly like a complete one. "
			"Nothing was created."
		)

	customer = as_str(args, "customer")
	if customer and not frappe.db.exists("Customer", customer):
		raise ToolError(f"Customer {customer!r} does not exist. Nothing was created.")
	for field, doctype in (("sales_order", "Sales Order"), ("sales_invoice", "Sales Invoice")):
		value = as_str(args, field)
		if value and not frappe.db.exists(doctype, value):
			raise ToolError(f"{doctype} {value!r} does not exist. Nothing was created.")

	doc = frappe.new_doc(SHIPMENT)
	doc.company = company
	doc.destination_tier = tier
	doc.destination_country = country
	doc.status = normalise_shipment_status(as_str(args, "status")) if as_str(args, "status") else "Draft"
	doc.customer = customer or None
	doc.sales_order = as_str(args, "sales_order") or None
	doc.sales_invoice = as_str(args, "sales_invoice") or None
	doc.ship_date = as_date(args, "ship_date")
	for field in (
		"commodity",
		"destination_name",
		"destination_address",
		"destination_state",
		"destination_city",
		"destination_postal_code",
		"carrier",
		"tracking_number",
		"container_number",
		"vessel",
		"voyage_number",
		"port_of_loading",
		"port_of_discharge",
		"weight_uom",
		"notes",
	):
		value = as_str(args, field)
		if value:
			doc.set(field, value)
	for field in ("total_packages",):
		value = as_int(args, field)
		if value is not None:
			doc.set(field, value)
	for field in ("gross_weight", "net_weight"):
		raw = args.get(field)
		if raw not in (None, ""):
			try:
				doc.set(field, float(raw))
			except (TypeError, ValueError):
				raise ToolError(f"{field} must be a number, got {raw!r}. Nothing was created.") from None

	enforcement = as_str(args, "enforcement")
	if enforcement:
		match = {mode.casefold(): mode for mode in (SITE_DEFAULT, ADVISORY, ENFORCED)}.get(
			enforcement.casefold()
		)
		if not match:
			raise ToolError(
				f"enforcement must be one of {SITE_DEFAULT}, {ADVISORY} or {ENFORCED}, got "
				f"{enforcement!r}. Nothing was created."
			)
		doc.enforcement = match

	rules = requirements_for(tier, country, company)
	templates = _templates_by_name([rule["trade_document_template"] for rule in rules])
	skipped = []
	for rule in rules:
		template = templates.get(rule["trade_document_template"])
		if not template:
			# A rule pointing at a template that has been deleted. Named rather
			# than dropped: a checklist quietly one line short is the failure
			# this whole module is built against.
			skipped.append(
				{
					"template": rule["trade_document_template"],
					"reason": "the template this rule requires no longer exists",
				}
			)
			continue
		if not compat.checked(template.get("enabled")):
			skipped.append({"template": template["name"], "reason": "the template is disabled"})
			continue
		if not applies_to_tier(template.get("applicable_tiers"), tier):
			skipped.append(
				{
					"template": template["name"],
					"reason": (
						f"the template applies to {template.get('applicable_tiers')} and this "
						f"shipment is {tier}"
					),
				}
			)
			continue
		doc.append(
			"documents",
			{
				"template": template["name"],
				"document_type": template.get("document_type"),
				"required": 1 if compat.checked(rule.get("required")) else 0,
				"sequence": int(rule.get("sequence") or template.get("sequence") or 50),
				"notes": rule.get("notes") or template.get("notes") or "",
				"status": "Not Started",
			},
		)

	doc.insert(ignore_permissions=True)

	checklist = _checklist_rows(dict(doc.as_dict()))
	required = [row for row in checklist if compat.checked(row.get("required"))]
	enforced, source = enforcement_for(dict(doc.as_dict()))
	data = {
		"shipment": doc.name,
		"status": doc.status,
		"company": company,
		"destination": destination_label(tier, country),
		"destination_tier": tier,
		"destination_country": country or None,
		"customer": doc.customer,
		"checklist": checklist,
		"required_count": len(required),
		"optional_count": len(checklist) - len(required),
		"rules_applied": [rule["name"] for rule in rules],
		"skipped": skipped,
		"enforcement": {
			"enforced": enforced,
			"source": source,
			"note": (
				"An incomplete checklist will BLOCK this shipment reaching Ready to Ship."
				if enforced
				else "An incomplete checklist will be reported and will NOT block this shipment. "
				"Turn `trade_document_enforcement` on in ERPNext MCP Settings, or set this "
				"shipment's own enforcement to Enforced, to hold it instead."
			),
		},
		"note": (
			"THE CHECKLIST IS A SNAPSHOT of what this destination asked for today. It is not "
			"rebuilt when the destination's rules change — get_shipment_readiness reports the "
			"drift instead, because a requirement added in March silently appearing on a "
			"February shipment that has already sailed would be worse than the drift."
		),
	}
	if not checklist:
		data["note"] = (
			f"NO DOCUMENTS ARE CONFIGURED FOR {destination_label(tier, country)}. This shipment "
			f"has an empty checklist, which means nothing is being tracked for it — not that "
			f"nothing is needed. set_destination_requirements is where that is fixed."
		)
	return ToolResult(
		data,
		f"created shipment {doc.name} to {destination_label(tier, country)} with "
		f"{len(checklist)} document(s) on its checklist ({len(required)} required)",
		"none → 0 (draft)",
	)


# ── 2. get_shipment ─────────────────────────────────────────────────────────
def get_shipment(args: dict) -> ToolResult:
	"""One shipment in full: the load, the route, and every document on it."""
	_require()
	name = as_str(args, "shipment", required=True)
	row = _shipment_row(name)
	language = _language(args)
	missing: list = []

	documents = _documents_of(name)
	by_template = {}
	for document in documents:
		by_template.setdefault(str(document.get("template") or ""), []).append(document)

	lines = _checklist_rows(row)
	templates = _templates_by_name([entry["template"] for entry in lines if entry.get("template")])
	checklist = []
	for entry in lines:
		template = templates.get(str(entry.get("template") or ""), {})
		matches = by_template.get(str(entry.get("template") or ""), [])
		live = [doc for doc in matches if str(doc.get("status")) != VOID]
		checklist.append(
			{
				"template": entry.get("template"),
				"label": _resolve_label(
					template, "label", language, missing, str(entry.get("template") or "")
				)
				or entry.get("template"),
				"document_type": entry.get("document_type"),
				"required": bool(compat.checked(entry.get("required"))),
				"sequence": entry.get("sequence"),
				"notes": entry.get("notes") or None,
				"documents": [
					{
						"trade_document": doc["name"],
						"title": doc.get("title"),
						"status": doc.get("status"),
						"expires_on": doc.get("expires_on"),
						"external_reference": doc.get("external_reference") or None,
						"sealed_at": doc.get("sealed_at"),
					}
					for doc in live
				],
				"started": bool(live),
			}
		)

	readiness = _readiness(row)
	# A shipment's own readiness is part of "the shipment in full": a caller
	# that had to make a second call to find out whether it can move would
	# mostly not make it.
	data = {
		"shipment": row["name"],
		"status": row.get("status"),
		"company": row.get("company"),
		"customer": row.get("customer"),
		"customer_name": row.get("customer_name"),
		"destination": destination_label(row.get("destination_tier"), row.get("destination_country")),
		"destination_tier": row.get("destination_tier"),
		"destination_country": row.get("destination_country") or None,
		"consignee": row.get("destination_name") or None,
		"destination_address": row.get("destination_address") or None,
		"destination_state": row.get("destination_state") or None,
		"destination_city": row.get("destination_city") or None,
		"sales_order": row.get("sales_order"),
		"sales_invoice": row.get("sales_invoice"),
		"commodity": row.get("commodity"),
		"total_packages": row.get("total_packages"),
		"gross_weight": row.get("gross_weight"),
		"net_weight": row.get("net_weight"),
		"weight_uom": row.get("weight_uom"),
		"transport": {
			"carrier": row.get("carrier"),
			"tracking_number": row.get("tracking_number"),
			"container_number": row.get("container_number"),
			"vessel": row.get("vessel"),
			"voyage_number": row.get("voyage_number"),
			"port_of_loading": row.get("port_of_loading"),
			"port_of_discharge": row.get("port_of_discharge"),
		},
		"timeline": {
			"ship_date": row.get("ship_date"),
			"released_on": row.get("released_on"),
			"departed_on": row.get("departed_on"),
			"delivered_on": row.get("delivered_on"),
			"cancelled_on": row.get("cancelled_on"),
		},
		"released_by": row.get("released_by"),
		"override_reason": row.get("override_reason") or None,
		"cancellation_reason": row.get("cancellation_reason") or None,
		"packet_document": row.get("packet_document"),
		"checklist": checklist,
		"documents": documents,
		"readiness": readiness,
		"next_statuses": list(SHIPMENT_TRANSITIONS.get(str(row.get("status") or ""), ())),
		"language": language,
		"untranslated": missing,
		"notes": row.get("notes"),
	}
	return ToolResult(
		data,
		f"shipment {row['name']} to {data['destination']}: {row.get('status')}, "
		f"{readiness['satisfied_count']}/{readiness['required_count']} required document(s) in order",
	)


# ── 3. list_shipments ───────────────────────────────────────────────────────
def list_shipments(args: dict) -> ToolResult:
	"""The shipping register, newest first."""
	_require()
	filters = {}
	status = as_filter(args, "status")
	if status:
		filters["status"] = normalise_shipment_status(status)
	customer = as_str(args, "customer")
	if customer:
		filters["customer"] = customer
	tier = as_str(args, "destination_tier")
	if tier:
		filters["destination_tier"] = normalise_tier(tier)
	country = as_str(args, "destination_country")
	if country:
		filters["destination_country"] = normalise_country(country)
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company, required=True)

	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters["ship_date"] = ("between", [from_date, to_date])
	elif from_date:
		filters["ship_date"] = (">=", from_date)
	elif to_date:
		filters["ship_date"] = ("<=", to_date)

	if as_bool(args, "open_only", False):
		filters["status"] = ("not in", [DELIVERED, CANCELLED])

	limit = as_limit(args)
	rows = frappe.db.get_all(
		SHIPMENT,
		filters=filters,
		fields=[
			"name",
			"status",
			"destination_tier",
			"destination_country",
			"customer",
			"customer_name",
			"company",
			"ship_date",
			"commodity",
			"container_number",
			"override_reason",
			"packet_document",
		],
		order_by="ship_date desc, modified desc",
		limit=limit,
	)
	shipments = []
	for row in rows:
		row = dict(row)
		row["destination"] = destination_label(row.get("destination_tier"), row.get("destination_country"))
		row["released_with_override"] = bool(row.get("override_reason"))
		shipments.append(row)

	by_status = {}
	by_tier = {}
	for row in shipments:
		by_status[row["status"]] = by_status.get(row["status"], 0) + 1
		by_tier[row["destination_tier"]] = by_tier.get(row["destination_tier"], 0) + 1

	data = {
		"shipments": shipments,
		"count": len(shipments),
		"by_status": by_status,
		"by_tier": by_tier,
		"limit": limit,
		"truncated": len(shipments) == limit,
	}
	return ToolResult(data, f"{len(shipments)} shipment(s)")


# ── readiness, shared ───────────────────────────────────────────────────────
def _document_is_satisfied(document: dict, today: str) -> tuple:
	"""`(satisfied: bool, reason: str)` for one document against a checklist line.

	FOUR WAYS A DOCUMENT THAT LOOKS DONE IS NOT, and every one of them has held a
	container somewhere:

	  * it is not approved yet;
	  * it is void — withdrawn, and a withdrawn certificate is not a certificate;
	  * it has EXPIRED. A phytosanitary certificate approved in June for a
	    shipment sailing in September is a document a border rejects, and a
	    checklist that counted it because its status column said Approved would
	    be reporting a shipment ready that is not;
	  * it needs an EXTERNAL FILING and carries no reference. This app does not
	    file anything, so an ePhyto nobody lodged in PCIT is a row in a database
	    and nothing else.
	"""
	status = str(document.get("status") or "")
	if status == VOID:
		return False, "it has been voided"
	if status not in SATISFYING:
		return False, f"it is {status or 'not started'}, not Approved"
	expires = document.get("expires_on")
	if expires and today and str(expires) < today:
		return False, f"it expired on {expires}"
	if (
		compat.checked(document.get("requires_external_filing"))
		and not str(document.get("external_reference") or "").strip()
	):
		system = str(document.get("external_system") or "the external system")
		return False, (
			f"it is approved but has no reference from {system}. This app does not file "
			f"anything — a document that requires an external filing and has no reference "
			f"back from it is not filed."
		)
	return True, ""


def _readiness(shipment: dict) -> dict:
	"""What this shipment still needs. The one computation the gate reads.

	Shared by `get_shipment_readiness`, `get_shipment` and `update_shipment_status`
	so a caller cannot be told it is ready by one and held by another.
	"""
	name = shipment["name"]
	today = frappe.utils.today()
	checklist = _checklist_rows(shipment)
	documents = _documents_of(name)
	by_template = {}
	for document in documents:
		by_template.setdefault(str(document.get("template") or ""), []).append(document)

	lines = []
	blocking = []
	satisfied_count = 0
	required_count = 0
	for entry in checklist:
		template = str(entry.get("template") or "")
		required = bool(compat.checked(entry.get("required")))
		if required:
			required_count += 1
		candidates = by_template.get(template, [])
		best = None
		reason = "nothing has been started for it"
		for document in candidates:
			ok, why = _document_is_satisfied(document, today)
			if ok:
				best = document
				reason = ""
				break
			if best is None:
				best = document
				reason = why
		satisfied = bool(best) and not reason
		if satisfied and required:
			satisfied_count += 1
		line = {
			"template": template,
			"document_type": entry.get("document_type"),
			"required": required,
			"satisfied": satisfied,
			"trade_document": best["name"] if best else None,
			"status": best.get("status") if best else None,
			"reason": reason or None,
			"notes": entry.get("notes") or None,
		}
		lines.append(line)
		if required and not satisfied:
			blocking.append(line)

	# WHAT THE DESTINATION ASKS FOR TODAY, against what this shipment's checklist
	# froze. Reported rather than applied — see `create_shipment` on why.
	drift = []
	try:
		current = requirements_for(
			shipment.get("destination_tier"), shipment.get("destination_country"), shipment.get("company")
		)
	except Exception:  # pragma: no cover - a tier that no longer parses
		current = []
	on_checklist = {str(entry.get("template") or "") for entry in checklist}
	for rule in current:
		if rule["trade_document_template"] not in on_checklist:
			drift.append(
				{
					"template": rule["trade_document_template"],
					"required": bool(compat.checked(rule.get("required"))),
					"note": (
						"this destination asks for it now and did not when the shipment was "
						"created. It is NOT on the checklist and is NOT blocking — add it with "
						"create_trade_document if this shipment needs it."
					),
				}
			)

	enforced, source = enforcement_for(shipment)
	return {
		"shipment": name,
		"shipment_status": shipment.get("status"),
		"destination": destination_label(
			shipment.get("destination_tier"), shipment.get("destination_country")
		),
		"lines": lines,
		"required_count": required_count,
		"satisfied_count": satisfied_count,
		"optional_count": len(lines) - required_count,
		"blocking": blocking,
		"ready": not blocking,
		"enforced": enforced,
		"enforcement_source": source,
		"requirement_drift": drift,
		"as_of": today,
	}


# ── 4. get_shipment_readiness ───────────────────────────────────────────────
def get_shipment_readiness(args: dict) -> ToolResult:
	"""Required versus done, and exactly what is holding this shipment."""
	_require()
	row = _shipment_row(as_str(args, "shipment", required=True))
	readiness = _readiness(row)
	readiness["note"] = (
		"READY means every REQUIRED document is Approved or Sealed, unexpired, and — where "
		"its template says somebody else's system files it — carries the reference that "
		"system issued. Optional documents are tracked and never block."
	)
	if not readiness["enforced"] and readiness["blocking"]:
		readiness["advisory_note"] = (
			"Enforcement is off, so this shipment CAN be moved to Ready to Ship with these "
			"gaps open. They are reported because an operation that has turned the gate off "
			"still wants to know what is missing."
		)
	summary = (
		f"shipment {row['name']}: {readiness['satisfied_count']}/{readiness['required_count']} "
		f"required document(s) in order"
	)
	if readiness["blocking"]:
		summary += f", {len(readiness['blocking'])} outstanding"
	return ToolResult(readiness, summary)


# ── 5. update_shipment_status ───────────────────────────────────────────────
def update_shipment_status(args: dict) -> ToolResult:
	"""Walk the shipment forward. The ONE gate in this module is on Ready to Ship.

	The walk itself is enforced by the doctype controller, because the order of
	real events is not a policy. What lives here is the DOCUMENT gate, which is,
	and which needs two things a `validate` cannot do: read the site's and the
	shipment's enforcement settings, and take a reason from the caller when
	somebody overrides it.
	"""
	_require()
	row = _shipment_row(as_str(args, "shipment", required=True))
	wanted = normalise_shipment_status(as_str(args, "status", required=True))
	was = str(row.get("status") or "")

	if wanted == was:
		return ToolResult(
			{"shipment": row["name"], "status": was, "changed": False},
			f"shipment {row['name']} is already {was}; nothing was changed",
		)

	allowed = SHIPMENT_TRANSITIONS.get(was, ())
	if wanted not in allowed:
		raise ToolError(
			f"{row['name']} cannot go from {was} to {wanted}. From {was} it can go to "
			f"{', '.join(allowed) if allowed else 'nowhere; it is a final state'}. Nothing was "
			f"changed."
		)

	readiness = _readiness(row)
	override_reason = as_str(args, "override_reason")
	overridden = False

	if wanted == GATED_TRANSITION and readiness["blocking"]:
		if readiness["enforced"]:
			if not override_reason:
				missing = "; ".join(
					f"{line['template']} ({line['reason']})" for line in readiness["blocking"]
				)
				raise ToolError(
					f"{row['name']} is not ready to ship: {len(readiness['blocking'])} required "
					f"document(s) are outstanding — {missing}. Enforcement is on via "
					f"{readiness['enforcement_source']}. Complete and approve them, or pass "
					f"`override_reason` naming why this shipment is going anyway — the reason is "
					f"written to the shipment, because a bypass nobody recorded is a bypass "
					f"nobody can review. Nothing was changed."
				)
			overridden = True
		elif override_reason:
			# Advisory mode with a reason given anyway: record it. Somebody
			# taking the trouble to say why has produced evidence worth keeping,
			# whether or not the gate demanded it.
			overridden = True

	doc = frappe.get_doc(SHIPMENT, row["name"])
	doc.status = wanted
	stamp = frappe.utils.now()
	actor = security.caller_identity() or str(getattr(frappe.session, "user", "") or "")
	if wanted == READY_TO_SHIP:
		doc.released_on = stamp
		if actor and actor != "Guest" and frappe.db.exists("User", actor):
			doc.released_by = actor
		if overridden:
			doc.override_reason = override_reason
	elif wanted == IN_TRANSIT:
		doc.departed_on = as_str(args, "departed_on") or stamp
	elif wanted == DELIVERED:
		doc.delivered_on = as_str(args, "delivered_on") or stamp
	elif wanted == CANCELLED:
		reason = as_str(args, "reason")
		if not reason:
			raise ToolError(
				"a cancellation needs a reason. A shipment that did not go is kept rather than "
				"deleted precisely so the file says why, and a blank one is a record that "
				"answers nothing. Nothing was changed."
			)
		doc.cancelled_on = stamp
		doc.cancellation_reason = reason
	notes = as_str(args, "notes")
	if notes:
		doc.notes = f"{doc.notes}\n{notes}" if doc.notes else notes
	doc.save(ignore_permissions=True)

	after = _readiness(dict(doc.as_dict()))
	data = {
		"shipment": doc.name,
		"status": doc.status,
		"previous_status": was,
		"changed": True,
		"released_by": doc.released_by,
		"released_on": doc.released_on,
		"overridden": overridden,
		"override_reason": doc.override_reason or None,
		"readiness": after,
		"next_statuses": list(SHIPMENT_TRANSITIONS.get(doc.status, ())),
	}
	if overridden:
		data["warning"] = (
			f"{row['name']} was released with {len(readiness['blocking'])} required document(s) "
			f"outstanding. The reason is on the shipment and this call is in the audit log."
		)
	elif wanted == GATED_TRANSITION and readiness["blocking"]:
		data["warning"] = (
			f"{row['name']} was released with {len(readiness['blocking'])} required document(s) "
			f"outstanding. Enforcement is advisory via {readiness['enforcement_source']}, so "
			f"this was allowed and reported rather than refused."
		)
	summary = f"shipment {doc.name}: {was} → {doc.status}"
	if overridden:
		summary += f" (OVERRIDDEN with {len(readiness['blocking'])} document(s) outstanding)"
	return ToolResult(data, summary, f"{was} → {doc.status}")


# ── auto-population ─────────────────────────────────────────────────────────
def _auto_populate(template: dict, shipment: dict, args: dict) -> tuple:
	"""Opening values for a new document, drawn from the record it is about.

	A STARTING POINT, NOT A BINDING. The values are copied once and the trade
	document owns them afterwards — a certificate that silently changed when
	somebody edited the invoice behind it is a certificate whose seal proves
	nothing about what was presented.

	NEVER RAISES OVER THE SOURCE. A template pointing at a doctype this site does
	not have, or a source record that has been deleted, produces an empty
	population and a note saying so — losing the document because its convenience
	feature could not run would be the wrong trade.
	"""
	source_doctype = str(template.get("auto_populate_from") or "").strip()
	source_name = as_str(args, "source_name")
	notes = []

	if not source_name and source_doctype:
		# The shipment usually knows: an invoice document populates from the
		# shipment's own invoice without anybody naming it twice.
		for field, doctype in (
			("sales_invoice", "Sales Invoice"),
			("sales_order", "Sales Order"),
		):
			if source_doctype == doctype and shipment.get(field):
				source_name = str(shipment[field])
				break

	if not source_doctype or not source_name:
		return {}, source_doctype, source_name, notes

	if not compat.doctype_exists(source_doctype):
		notes.append(
			f"{template['name']} populates from {source_doctype}, which this site does not "
			f"have. The document was created empty."
		)
		return {}, source_doctype, "", notes
	if not frappe.db.exists(source_doctype, source_name):
		notes.append(
			f"{source_doctype} {source_name!r} does not exist, so nothing was populated. The "
			f"document was created empty."
		)
		return {}, source_doctype, "", notes

	try:
		record = frappe.db.get_value(source_doctype, source_name, "*", as_dict=True) or {}
	except Exception:  # pragma: no cover
		notes.append(f"{source_doctype} {source_name!r} could not be read; the document was created empty.")
		return {}, source_doctype, source_name, notes

	mapping = {}
	raw_map = template.get("auto_populate_map")
	if raw_map:
		try:
			parsed = json.loads(raw_map) if isinstance(raw_map, str) else raw_map
			if isinstance(parsed, dict):
				mapping = parsed
		except (json.JSONDecodeError, ValueError, TypeError):
			notes.append(
				f"{template['name']} has an auto-populate map that will not parse; the fields "
				f"sharing a name were copied and nothing else."
			)

	populated = {}
	if mapping:
		for target, source_field in mapping.items():
			value = record.get(str(source_field))
			if value not in (None, ""):
				populated[str(target)] = value
	else:
		for field in _declared_fields(template):
			value = record.get(field)
			if value not in (None, ""):
				populated[field] = value
	if not populated:
		notes.append(
			f"nothing was populated from {source_doctype} {source_name}: none of the fields "
			f"this template declares matched a column on it. The reference is still recorded."
		)
	return populated, source_doctype, source_name, notes


def _declared_fields(template: dict) -> list:
	"""The field names a template's `required_fields` declares, in order."""
	raw = template.get("required_fields")
	if not raw:
		return []
	try:
		parsed = json.loads(raw) if isinstance(raw, str) else raw
	except (json.JSONDecodeError, ValueError, TypeError):
		return []
	if not isinstance(parsed, list):
		return []
	out = []
	for entry in parsed:
		if isinstance(entry, str) and entry.strip():
			out.append(entry.strip())
		elif isinstance(entry, dict) and str(entry.get("fieldname") or "").strip():
			out.append(str(entry["fieldname"]).strip())
	return out


def _schema(template: dict, language: str, missing: list) -> list:
	"""A template's declared fields, resolved into the caller's language."""
	raw = template.get("required_fields")
	if not raw:
		return []
	try:
		parsed = json.loads(raw) if isinstance(raw, str) else raw
	except (json.JSONDecodeError, ValueError, TypeError):
		return []
	if not isinstance(parsed, list):
		return []
	where = str(template.get("name") or "")
	out = []
	for entry in parsed:
		if isinstance(entry, str):
			out.append({"fieldname": entry, "label": entry, "type": "text", "required": False, "help": None})
			continue
		if not isinstance(entry, dict):
			continue
		fieldname = str(entry.get("fieldname") or "").strip()
		if not fieldname:
			continue
		out.append(
			{
				"fieldname": fieldname,
				"label": _resolve_label(entry, "label", language, missing, f"{where}.{fieldname}")
				or fieldname,
				"type": entry.get("type") or "text",
				"required": bool(entry.get("required")),
				"help": _resolve_label(entry, "help", language, missing, f"{where}.{fieldname}") or None,
			}
		)
	return out


# ── 6. create_trade_document ────────────────────────────────────────────────
def create_trade_document(args: dict) -> ToolResult:
	"""Start one document for a shipment, populated from its source where it can be."""
	_require()
	shipment_name = as_str(args, "shipment", required=True)
	shipment = _shipment_row(shipment_name)
	template_name = as_str(args, "trade_document_template", required=True)
	template = _template_row(template_name)

	if not compat.checked(template.get("enabled")):
		raise ToolError(
			f"{template_name} is disabled, so no new document of that type can be started. "
			f"Re-enable the template if this is still a document this operation issues. "
			f"Nothing was created."
		)

	tier = str(shipment.get("destination_tier") or "")
	if not applies_to_tier(template.get("applicable_tiers"), tier):
		if not as_bool(args, "allow_off_tier", False):
			raise ToolError(
				f"{template_name} applies to {template.get('applicable_tiers')} and "
				f"{shipment_name} is a {tier} shipment. That is usually a template picked by "
				f"mistake. Pass allow_off_tier=true if this shipment genuinely needs it — a "
				f"buyer really can ask for an export document on a domestic load. Nothing was "
				f"created."
			)

	language = _language(args)
	missing: list = []
	populated, source_doctype, source_name, notes = _auto_populate(template, shipment, args)

	given = args.get("document_data")
	if given not in (None, ""):
		if isinstance(given, str):
			try:
				given = json.loads(given)
			except (json.JSONDecodeError, ValueError, TypeError):
				raise ToolError(
					"document_data must be a JSON object of field → value. Nothing was created."
				) from None
		if not isinstance(given, dict):
			raise ToolError(
				f"document_data must be a JSON object of field → value, got "
				f"{type(given).__name__}. Nothing was created."
			)
		# THE CALLER'S VALUES WIN over the auto-population. Somebody who typed a
		# value meant it; a copy from an invoice is a convenience.
		populated.update(given)

	doc = frappe.new_doc(DOCUMENT)
	doc.shipment = shipment_name
	doc.template = template_name
	doc.document_type = template.get("document_type")
	doc.company = shipment.get("company")
	doc.title = as_str(args, "title") or (template.get("label_en") or template_name)
	doc.status = DOC_DRAFT
	doc.document_data = json.dumps(populated, sort_keys=True, default=str) if populated else ""
	doc.source_doctype = source_doctype or None
	doc.source_name = source_name or None
	doc.issued_on = as_date(args, "issued_on")
	doc.expires_on = as_date(args, "expires_on")
	doc.requires_external_filing = 1 if compat.checked(template.get("requires_external_filing")) else 0
	doc.external_system = template.get("external_system") or None
	doc.external_reference = as_str(args, "external_reference") or None
	doc.notes = as_str(args, "notes") or None

	checklist = _checklist_rows(shipment)
	line = next((entry for entry in checklist if str(entry.get("template")) == template_name), None)
	required = as_bool(args, "required", None)
	if required is None:
		required = bool(compat.checked(line.get("required"))) if line else False
	doc.required = 1 if required else 0
	doc.insert(ignore_permissions=True)

	if line:
		_mirror_status(shipment_name, template_name, doc.name, doc.status)
	else:
		notes.append(
			f"{template_name} was not on {shipment_name}'s checklist, so this document is "
			f"tracked but adds no checklist line. That is the right shape for paper a buyer "
			f"asked for that the destination's rules do not require; if the destination DOES "
			f"require it, the rule is missing — set_destination_requirements is where it goes."
		)

	schema = _schema(template, language, missing)
	unfilled = [
		field["fieldname"] for field in schema if field["required"] and field["fieldname"] not in populated
	]
	data = {
		"trade_document": doc.name,
		"shipment": shipment_name,
		"template": template_name,
		"document_type": doc.document_type,
		"status": doc.status,
		"title": doc.title,
		"required": bool(doc.required),
		"document_data": populated,
		"schema": schema,
		"unfilled_required_fields": unfilled,
		"source": {"doctype": source_doctype or None, "name": source_name or None},
		"requires_external_filing": bool(doc.requires_external_filing),
		"external_system": doc.external_system,
		"requires_signature": bool(compat.checked(template.get("requires_signature"))),
		"standard_reference": template.get("standard_reference"),
		"language": language,
		"untranslated": missing,
		"notes": notes,
	}
	if doc.requires_external_filing and not doc.external_reference:
		data["filing_note"] = (
			f"THIS DOCUMENT IS NOT FINISHED WHEN THIS APP SAYS IT IS. {doc.external_system} "
			f"files it, this app does not, and until update_trade_document records the "
			f"reference that system issues, this document will be reported as outstanding "
			f"however approved it looks."
		)
	return ToolResult(
		data,
		f"created {doc.document_type} {doc.name} for shipment {shipment_name}"
		+ (f", {len(unfilled)} required field(s) unfilled" if unfilled else ""),
		"none → 0 (draft)",
	)


def _mirror_status(shipment: str, template: str, document: str, status: str) -> None:
	"""Refresh the checklist line's display mirror. Never authoritative.

	Best-effort by construction: a mirror that failed to write is a grid that
	reads slightly stale, and nothing in this app decides anything from it —
	every readiness answer reads the Trade Document itself. Losing a document
	write because its cosmetic twin could not be updated would be the wrong
	trade.
	"""
	try:
		parent = frappe.get_doc(SHIPMENT, shipment)
		for row in parent.get("documents") or []:
			if str(row.get("template") or "") != template:
				continue
			frappe.db.set_value(
				"Trade Shipment Document",
				row.get("name"),
				{"trade_document": document, "status": status},
				update_modified=False,
			)
			break
	except Exception:  # pragma: no cover
		pass


# ── 7. get_trade_document ───────────────────────────────────────────────────
def get_trade_document(args: dict) -> ToolResult:
	"""One document in full, with its schema and what is still missing from it."""
	_require()
	row = _document_row(as_str(args, "trade_document", required=True))
	language = _language(args)
	missing: list = []
	template = _template_row(row["template"]) if row.get("template") else {}
	data_fields = parse_data(row.get("document_data"))
	schema = _schema(template, language, missing) if template else []
	unfilled = [
		field["fieldname"]
		for field in schema
		if field["required"] and not str(data_fields.get(field["fieldname"]) or "").strip()
	]

	today = frappe.utils.today()
	satisfied, reason = _document_is_satisfied(row, today)
	seal = _verify_seal(row)

	data = {
		"trade_document": row["name"],
		"title": row.get("title"),
		"status": row.get("status"),
		"document_type": row.get("document_type"),
		"shipment": row.get("shipment"),
		"template": row.get("template"),
		"company": row.get("company"),
		"required": bool(compat.checked(row.get("required"))),
		"document_data": data_fields,
		"schema": schema,
		"unfilled_required_fields": unfilled,
		"source": {"doctype": row.get("source_doctype"), "name": row.get("source_name")},
		"issued_on": row.get("issued_on"),
		"expires_on": row.get("expires_on"),
		"external": {
			"required": bool(compat.checked(row.get("requires_external_filing"))),
			"system": row.get("external_system"),
			"reference": row.get("external_reference"),
			"filed_on": row.get("external_filed_on"),
		},
		"review": {
			"reviewed_by": row.get("reviewed_by"),
			"reviewed_on": row.get("reviewed_on"),
			"approved_by": row.get("approved_by"),
			"approved_on": row.get("approved_on"),
			"approval_notes": row.get("approval_notes"),
		},
		"seal": seal,
		"signing_evidence": row.get("signing_evidence"),
		"satisfies_checklist": satisfied,
		"outstanding_because": reason or None,
		"standard_reference": template.get("standard_reference") if template else None,
		"requires_signature": bool(compat.checked(template.get("requires_signature"))) if template else False,
		"language": language,
		"untranslated": missing,
		"notes": row.get("notes"),
	}
	return ToolResult(data, f"{row.get('document_type')} {row['name']}: {row.get('status')}")


#: Everything `update_trade_document` can change other than the status. Named
#: once so the sealed-document guard and the "nothing to update" refusal cannot
#: come to disagree about what an edit is.
_EDITABLE = (
	"document_data",
	"title",
	"external_reference",
	"external_system",
	"external_filed_on",
	"issued_on",
	"expires_on",
	"required",
	"notes",
)


def _other_edits(args: dict) -> bool:
	"""Whether this call changes anything besides the status."""
	return any(args.get(field) not in (None, "") for field in _EDITABLE)


# ── 8. update_trade_document ────────────────────────────────────────────────
def update_trade_document(args: dict) -> ToolResult:
	"""Fill in a document's fields, its dates, and the reference its filing returned.

	MERGES BY DEFAULT rather than replacing. A sales desk completing a
	phytosanitary certificate over three calls, each carrying the fields it just
	learned, would find each call erasing the last under replace semantics — and
	would find it at the worst moment. `replace=true` is there for the caller who
	genuinely means to discard what is in the document.
	"""
	_require()
	row = _document_row(as_str(args, "trade_document", required=True))
	wanted_status = normalise_status(as_str(args, "status")) if as_str(args, "status") else ""

	# VOIDING A SEALED DOCUMENT IS THE ONE EDIT THAT GETS THROUGH, and it has to
	# be: the seal's own refusal message tells the caller to void and reissue, so
	# a tool layer that blocked voiding would name an escape hatch nothing could
	# reach. Everything else is refused — that is what sealing IS.
	if str(row.get("status")) == SEALED:
		if wanted_status != VOID or _other_edits(args):
			raise ToolError(
				f"{row['name']} is sealed. Its hash says 'this is what we presented', and a hash "
				f"over a row anybody can still edit says nothing — so a sealed document is "
				f"closed. Void it (status='Void', on its own) and issue a replacement, which is "
				f"what actually happens when a certificate is withdrawn and reissued. Nothing "
				f"was changed."
			)
	if str(row.get("status")) == VOID:
		raise ToolError(f"{row['name']} has been voided. Nothing was changed.")

	doc = frappe.get_doc(DOCUMENT, row["name"])
	changed = []

	given = args.get("document_data")
	if given not in (None, ""):
		if isinstance(given, str):
			try:
				given = json.loads(given)
			except (json.JSONDecodeError, ValueError, TypeError):
				raise ToolError(
					"document_data must be a JSON object of field → value. Nothing was changed."
				) from None
		if not isinstance(given, dict):
			raise ToolError(
				f"document_data must be a JSON object of field → value, got "
				f"{type(given).__name__}. Nothing was changed."
			)
		current = parse_data(doc.document_data)
		merged = dict(given) if as_bool(args, "replace", False) else {**current, **given}
		doc.document_data = json.dumps(merged, sort_keys=True, default=str)
		changed.append("document_data")

	for field in ("title", "external_reference", "external_system", "notes"):
		value = as_str(args, field)
		if value:
			doc.set(field, value)
			changed.append(field)
	for field in ("issued_on", "expires_on"):
		value = as_date(args, field)
		if value:
			doc.set(field, value)
			changed.append(field)
	filed_on = as_str(args, "external_filed_on")
	if filed_on:
		doc.external_filed_on = filed_on
		changed.append("external_filed_on")
	elif "external_reference" in changed and not doc.external_filed_on:
		# A reference arriving is a filing having happened; stamping it saves a
		# second call and is what the caller meant.
		doc.external_filed_on = frappe.utils.now()
		changed.append("external_filed_on")

	required = as_bool(args, "required", None)
	if required is not None:
		doc.required = 1 if required else 0
		changed.append("required")

	status = as_str(args, "status")
	if status:
		wanted = normalise_status(status)
		if wanted in (APPROVED, SEALED):
			raise ToolError(
				f"update_trade_document does not move a document to {wanted}. "
				f"{'approve_trade_document' if wanted == APPROVED else 'seal_trade_document'} "
				f"does, because it is an act with a principal behind it — it checks the role, "
				f"stamps who did it, and writes the attestation where the template asks for "
				f"one. Nothing was changed."
			)
		if wanted != doc.status:
			doc.status = wanted
			changed.append("status")

	if not changed:
		raise ToolError(
			"nothing to update. Pass document_data, title, issued_on, expires_on, "
			"external_reference, external_filed_on, required, notes or status."
		)

	doc.save(ignore_permissions=True)
	if doc.shipment and doc.template:
		_mirror_status(doc.shipment, doc.template, doc.name, doc.status)

	template = _template_row(doc.template) if doc.template else {}
	data_fields = parse_data(doc.document_data)
	schema = _schema(template, "en", []) if template else []
	unfilled = [
		field["fieldname"]
		for field in schema
		if field["required"] and not str(data_fields.get(field["fieldname"]) or "").strip()
	]
	data = {
		"trade_document": doc.name,
		"status": doc.status,
		"changed": changed,
		"document_data": data_fields,
		"unfilled_required_fields": unfilled,
		"ready_to_approve": not unfilled,
		"external": {
			"required": bool(compat.checked(doc.requires_external_filing)),
			"system": doc.external_system,
			"reference": doc.external_reference,
			"filed_on": doc.external_filed_on,
		},
	}
	if unfilled:
		data["note"] = (
			f"{len(unfilled)} field(s) this template declares required are still empty: "
			f"{', '.join(unfilled)}. approve_trade_document will say so — it does not refuse "
			f"over them, because a template's schema is this app's reading of the document and "
			f"the person holding the actual certificate is a better authority on it."
		)
	return ToolResult(data, f"updated {doc.name} ({', '.join(changed)})")


# ── 9. approve_trade_document ───────────────────────────────────────────────
def approve_trade_document(args: dict) -> ToolResult:
	"""Approve a document, with an attestation where its template asks for one."""
	_require()
	actor = require_trade_role()
	row = _document_row(as_str(args, "trade_document", required=True))
	status = str(row.get("status") or "")
	if status == APPROVED:
		return ToolResult(
			{"trade_document": row["name"], "status": APPROVED, "changed": False},
			f"{row['name']} is already approved; nothing was changed",
		)
	if status not in (DOC_DRAFT, PENDING_REVIEW):
		raise ToolError(f"{row['name']} is {status} and cannot be approved from there. Nothing was changed.")

	template = _template_row(row["template"]) if row.get("template") else {}
	data_fields = parse_data(row.get("document_data"))
	schema = _schema(template, "en", []) if template else []
	unfilled = [
		field["fieldname"]
		for field in schema
		if field["required"] and not str(data_fields.get(field["fieldname"]) or "").strip()
	]

	doc = frappe.get_doc(DOCUMENT, row["name"])
	doc.status = APPROVED
	doc.approved_on = frappe.utils.now()
	if frappe.db.exists("User", actor):
		doc.approved_by = actor
		if not doc.reviewed_by:
			doc.reviewed_by = actor
			doc.reviewed_on = doc.approved_on
	notes = as_str(args, "approval_notes")
	if notes:
		doc.approval_notes = notes

	evidence = None
	if compat.checked(template.get("requires_signature")):
		# THE FINGERPRINT IS TAKEN BEFORE THE APPROVAL COLUMNS ARE WRITTEN, which
		# is the only moment that answers the question it exists to answer: what
		# did this person see when they certified it. `signing_evidence.record`
		# never raises, so a site without the doctype loses the evidence row and
		# keeps the approval — and says which.
		fingerprint = signing_evidence.document_fingerprint(
			frappe.get_doc(DOCUMENT, row["name"]),
			exclude=(
				*_NEVER_SEALED,
				"approved_by",
				"approved_on",
				"approval_notes",
				"reviewed_by",
				"reviewed_on",
			),
		)
		evidence = signing_evidence.record(
			document_type=DOCUMENT,
			document_name=doc.name,
			signature_role=str(template.get("signature_role") or "Employer Representative"),
			signed_at=doc.approved_on,
			company=str(doc.company or ""),
			signer=as_str(args, "signer_employee"),
			signer_name=as_str(args, "signer_name") or actor,
			signer_user=actor,
			verification_method=as_str(args, "verification_method"),
			signature_field="approved_by",
			document_hash=fingerprint.get("hash", ""),
			hashed_fields=json.dumps(list(fingerprint.get("fields") or ())),
		)
		if evidence.get("evidence"):
			doc.signing_evidence = evidence["evidence"]

	doc.save(ignore_permissions=True)
	if doc.shipment and doc.template:
		_mirror_status(doc.shipment, doc.template, doc.name, doc.status)

	data = {
		"trade_document": doc.name,
		"status": doc.status,
		"changed": True,
		"approved_by": doc.approved_by or actor,
		"approved_on": doc.approved_on,
		"unfilled_required_fields": unfilled,
		"attestation": evidence,
		"signing_evidence": doc.signing_evidence,
	}
	if unfilled:
		data["warning"] = (
			f"approved with {len(unfilled)} field(s) this template declares required still "
			f"empty: {', '.join(unfilled)}. Reported rather than refused — a template's schema "
			f"is this app's reading of the document, and the person holding the actual "
			f"certificate is a better authority on what it needs than a JSON list is."
		)
	if compat.checked(doc.requires_external_filing) and not str(doc.external_reference or "").strip():
		data["filing_warning"] = (
			f"approved, and STILL OUTSTANDING for the shipment: {doc.external_system} has to "
			f"file this and no reference from it has been recorded. get_shipment_readiness "
			f"counts it as not satisfied until update_trade_document supplies one."
		)
	return ToolResult(data, f"approved {doc.document_type} {doc.name} as {actor}", f"{status} → {APPROVED}")


# ── the seal ────────────────────────────────────────────────────────────────
def _seal_payload(row: dict) -> tuple:
	"""`(hash, fields)` over a document's content. Deterministic by construction.

	AN ALLOW-LIST RATHER THAN "EVERYTHING EXCEPT". A column added in a later
	version would silently join an all-columns hash and make every document
	sealed before that version fail verification — which is an integrity check
	that fires on documents nobody touched, and one nobody reads. The list grows
	only when somebody means it to.

	Empty columns are dropped for the same reason `signing_evidence` drops them:
	the fields list is stored beside the hash so verification recomputes over
	exactly what was covered.
	"""
	payload = {}
	for field in SEALED_FIELDS:
		if field in _NEVER_SEALED:
			continue
		value = row.get(field)
		if value in (None, ""):
			continue
		payload[field] = str(value)
	blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True)
	digest = "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
	return digest, tuple(sorted(payload))


def _verify_seal(row: dict) -> dict:
	"""Whether a sealed document still hashes to what was sealed."""
	stored = str(row.get("document_hash") or "")
	if not stored:
		return {"sealed": False, "hash": None, "verified": None, "note": "this document is not sealed"}
	try:
		fields = json.loads(row.get("hashed_fields") or "[]")
	except (json.JSONDecodeError, ValueError, TypeError):
		fields = []
	payload = {}
	for field in fields:
		value = row.get(field)
		if value in (None, ""):
			continue
		payload[str(field)] = str(value)
	blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True)
	recomputed = "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
	matches = recomputed == stored
	return {
		"sealed": True,
		"hash": stored,
		"recomputed": recomputed,
		"verified": matches,
		"hashed_fields": list(fields),
		"sealed_by": row.get("sealed_by"),
		"sealed_at": row.get("sealed_at"),
		"note": (
			"the document hashes to what was sealed"
			if matches
			else "THE DOCUMENT DOES NOT HASH TO WHAT WAS SEALED. Its content has changed since "
			"it was sealed, or a column the seal covered was cleared. Treat the seal as broken."
		),
	}


# ── 10. seal_trade_document ─────────────────────────────────────────────────
def seal_trade_document(args: dict) -> ToolResult:
	"""Fingerprint an approved document and close it to editing.

	SEALING IS WHAT MAKES A ROW EVIDENCE. Until it is sealed, "this is the
	certificate we presented" is an assertion about a record anybody could have
	edited since. After it, the claim is checkable — and the document refuses
	content edits, because a seal over a row that can still change is a timestamp
	wearing a seal's clothes.
	"""
	_require()
	actor = require_trade_role()
	row = _document_row(as_str(args, "trade_document", required=True))
	status = str(row.get("status") or "")
	if status == SEALED:
		return ToolResult(
			{"trade_document": row["name"], "status": SEALED, "changed": False, "seal": _verify_seal(row)},
			f"{row['name']} is already sealed; nothing was changed",
		)
	if status != APPROVED:
		raise ToolError(
			f"{row['name']} is {status} and only an Approved document can be sealed. The seal "
			f"fingerprints content somebody approved; sealing an unapproved one would certify "
			f"that nobody had checked it. Nothing was changed."
		)

	digest, fields = _seal_payload(row)
	doc = frappe.get_doc(DOCUMENT, row["name"])
	doc.status = SEALED
	doc.document_hash = digest
	doc.hashed_fields = json.dumps(list(fields))
	doc.sealed_at = frappe.utils.now()
	if frappe.db.exists("User", actor):
		doc.sealed_by = actor
	doc.save(ignore_permissions=True)
	if doc.shipment and doc.template:
		_mirror_status(doc.shipment, doc.template, doc.name, doc.status)

	data = {
		"trade_document": doc.name,
		"status": doc.status,
		"changed": True,
		"document_hash": digest,
		"hashed_fields": list(fields),
		"sealed_by": doc.sealed_by or actor,
		"sealed_at": doc.sealed_at,
		"note": (
			"This document is now closed to content edits. The hash covers the fields listed, "
			"and get_trade_document recomputes it on every read — so a document whose content "
			"was changed underneath the seal reports the seal as broken rather than looking "
			"intact. Voiding it and issuing a replacement is the way to correct a sealed "
			"document, which is also what happens to a real certificate that is withdrawn."
		),
	}
	return ToolResult(
		data, f"sealed {doc.document_type} {doc.name} ({digest[:19]}…)", f"{APPROVED} → {SEALED}"
	)


# ── 11. list_trade_documents ────────────────────────────────────────────────
def list_trade_documents(args: dict) -> ToolResult:
	"""The document register, filterable by shipment, type and status."""
	_require()
	filters = {}
	shipment = as_str(args, "shipment")
	if shipment:
		filters["shipment"] = shipment
	document_type = as_str(args, "document_type")
	if document_type:
		filters["document_type"] = document_type
	status = as_filter(args, "status")
	if status:
		filters["status"] = normalise_status(status)
	template = as_str(args, "trade_document_template")
	if template:
		filters["template"] = template
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company, required=True)
	if as_bool(args, "outstanding_only", False):
		filters["status"] = ("not in", [*SATISFYING, VOID])

	limit = as_limit(args)
	rows = frappe.db.get_all(
		DOCUMENT,
		filters=filters,
		fields=[
			"name",
			"title",
			"status",
			"document_type",
			"shipment",
			"template",
			"company",
			"required",
			"issued_on",
			"expires_on",
			"requires_external_filing",
			"external_system",
			"external_reference",
			"sealed_at",
			"approved_by",
		],
		order_by="modified desc",
		limit=limit,
	)
	today = frappe.utils.today()
	documents = []
	by_status = {}
	unfiled = []
	expired = []
	for row in rows:
		row = dict(row)
		satisfied, reason = _document_is_satisfied(row, today)
		row["satisfies_checklist"] = satisfied
		row["outstanding_because"] = reason or None
		documents.append(row)
		by_status[row["status"]] = by_status.get(row["status"], 0) + 1
		if compat.checked(row.get("requires_external_filing")) and not row.get("external_reference"):
			unfiled.append(row["name"])
		if row.get("expires_on") and str(row["expires_on"]) < today and str(row["status"]) != VOID:
			expired.append(row["name"])

	data = {
		"documents": documents,
		"count": len(documents),
		"by_status": by_status,
		"awaiting_external_filing": unfiled,
		"expired": expired,
		"limit": limit,
		"truncated": len(documents) == limit,
	}
	summary = f"{len(documents)} trade document(s)"
	if unfiled:
		summary += f", {len(unfiled)} awaiting an external filing"
	if expired:
		summary += f", {len(expired)} expired"
	return ToolResult(data, summary)


# ── 12. generate_shipment_packet ────────────────────────────────────────────
def generate_shipment_packet(args: dict) -> ToolResult:
	"""Bundle a shipment's documents into one tamper-evident record.

	WHAT MAKES THIS A PACKET RATHER THAN A LIST. It carries each document's own
	seal, a hash over the whole bundle, and — where a document is NOT sealed —
	says so in the packet rather than leaving it out. A bundle that silently
	dropped the unsealed documents would read as a shipment with less paperwork
	than it has, which is the direction that gets a container held.

	IT REFUSES BY DEFAULT WHEN DOCUMENTS ARE UNSEALED, on the same argument
	`generate_audit_packet` refuses an open corrective action: a packet is
	something somebody carries into a room and defends, and one assembled from
	documents that can still be edited is a claim rather than evidence.
	`allow_unsealed=true` produces it anyway, with the unsealed ones listed at
	the front — an operation that genuinely has to hand something over
	mid-preparation is better served by disclosing that than by hiding it.
	"""
	_require()
	row = _shipment_row(as_str(args, "shipment", required=True))
	documents = _documents_of(row["name"])
	live = [document for document in documents if str(document.get("status")) != VOID]
	if not live:
		raise ToolError(
			f"{row['name']} has no documents to bundle. A packet of nothing is not evidence of "
			f"a compliant shipment, it is an empty folder. Nothing was created."
		)
	if len(live) > PACKET_CAP:
		raise ToolError(
			f"{row['name']} has {len(live)} documents, over the {PACKET_CAP} at which a bundle "
			f"stops being something anybody reads. Nothing was created."
		)

	unsealed = [document["name"] for document in live if str(document.get("status")) != SEALED]
	allow_unsealed = as_bool(args, "allow_unsealed", False)
	if unsealed and not allow_unsealed:
		raise ToolError(
			f"{len(unsealed)} of {row['name']}'s {len(live)} document(s) are not sealed: "
			f"{', '.join(unsealed[:8])}{'…' if len(unsealed) > 8 else ''}. A packet assembled "
			f"from documents that can still be edited is a claim, not evidence — seal them "
			f"with seal_trade_document, or pass allow_unsealed=true to produce it anyway with "
			f"the unsealed ones named at the front. Nothing was created."
		)

	readiness = _readiness(row)
	entries = []
	broken = []
	for document in live:
		full = _document_row(document["name"])
		seal = _verify_seal(full)
		if seal.get("sealed") and seal.get("verified") is False:
			broken.append(document["name"])
		entries.append(
			{
				"trade_document": document["name"],
				"title": document.get("title"),
				"document_type": document.get("document_type"),
				"status": document.get("status"),
				"required": bool(compat.checked(document.get("required"))),
				"issued_on": document.get("issued_on"),
				"expires_on": document.get("expires_on"),
				"external_system": document.get("external_system"),
				"external_reference": document.get("external_reference"),
				"document_hash": document.get("document_hash"),
				"sealed_at": document.get("sealed_at"),
				"seal_verified": seal.get("verified"),
				"signing_evidence": document.get("signing_evidence"),
			}
		)

	# The packet's own hash is over the DOCUMENT HASHES, not over the documents.
	# That is what makes it checkable without re-reading every row: a bundle
	# whose members are individually sealed needs only to prove which members it
	# claimed.
	spine = json.dumps(
		[
			{
				"document": entry["trade_document"],
				"type": entry["document_type"],
				"hash": entry["document_hash"] or "",
				"status": entry["status"],
			}
			for entry in sorted(entries, key=lambda entry: entry["trade_document"])
		],
		sort_keys=True,
		ensure_ascii=True,
	)
	packet_hash = "sha256:" + hashlib.sha256(spine.encode("utf-8")).hexdigest()

	filed = None
	note = ""
	if compat.doctype_exists(GOVERNANCE_DOCUMENT):
		try:
			doc = frappe.new_doc(GOVERNANCE_DOCUMENT)
			doc.title = f"Shipment document packet — {row['name']} — {readiness['destination']}"
			doc.company = row.get("company")
			doc.category = _governance_category()
			doc.effective_date = row.get("ship_date") or frappe.utils.today()
			doc.notes = (
				f"{len(entries)} document(s) for shipment {row['name']} to "
				f"{readiness['destination']}. Packet hash {packet_hash}. "
				f"{len(unsealed)} unsealed. "
				f"{readiness['satisfied_count']}/{readiness['required_count']} required "
				f"document(s) in order at assembly."
			)
			doc.insert(ignore_permissions=True)
			filed = doc.name
			frappe.db.set_value(SHIPMENT, row["name"], "packet_document", doc.name, update_modified=False)
		except Exception as exc:
			# Filing is the archive copy, not the packet. Losing the archive is
			# not a reason to lose the bundle the caller asked for.
			note = (
				f"the packet was assembled and could not be filed as a Governance Document "
				f"({type(exc).__name__}: {exc}). The bundle and its hash are in this result."
			)
	else:
		note = (
			"Governance Document is not on this site, so the packet was assembled and not "
			"archived. Run `bench --site <site> migrate`."
		)

	data = {
		"shipment": row["name"],
		"destination": readiness["destination"],
		"packet_hash": packet_hash,
		"documents": entries,
		"document_count": len(entries),
		"sealed_count": len(entries) - len(unsealed),
		"unsealed": unsealed,
		"broken_seals": broken,
		"readiness": readiness,
		"governance_document": filed,
		"generated_at": frappe.utils.now(),
		"note": note or None,
	}
	if unsealed:
		data["disclosure"] = (
			f"THIS PACKET CONTAINS {len(unsealed)} UNSEALED DOCUMENT(S), listed in `unsealed`. "
			f"They can still be edited, so this bundle does not fix their content. They are "
			f"named here rather than dropped, because a packet that quietly omitted them would "
			f"read as a shipment with less paperwork than it has."
		)
	if broken:
		data["warning"] = (
			f"{len(broken)} document(s) in this packet DO NOT HASH TO WHAT WAS SEALED: "
			f"{', '.join(broken)}. Their content changed after sealing. Treat those seals as "
			f"broken and establish what happened before this packet is relied on."
		)
	summary = (
		f"packet for shipment {row['name']}: {len(entries)} document(s), "
		f"{len(entries) - len(unsealed)} sealed, hash {packet_hash[:19]}…"
	)
	return ToolResult(data, summary, "none → 0 (draft)" if filed else "")


def _governance_category() -> str:
	"""'Audit Packet' where the site's Select offers it, 'Other' where it does not."""
	try:
		field = compat.field_meta(GOVERNANCE_DOCUMENT, "category")
		options = [line.strip() for line in str((field or {}).get("options") or "").split("\n")]
		return "Audit Packet" if "Audit Packet" in options else "Other"
	except Exception:  # pragma: no cover
		return "Other"


# ── 13. create_trade_document_template ──────────────────────────────────────
def create_trade_document_template(args: dict) -> ToolResult:
	"""Add or amend a kind of trade document. Config, not code."""
	compat.require_doctype(TEMPLATE, "Run `bench --site <site> migrate`.")
	name = as_str(args, "template_name", required=True)
	document_type = as_str(args, "document_type", required=True)

	existing = frappe.db.exists(TEMPLATE, name)
	if existing and not as_bool(args, "update_existing", False):
		raise ToolError(
			f"{TEMPLATE} {name!r} already exists. Pass update_existing=true to amend it — the "
			f"refusal is here because silently overwriting a template an operator tuned for "
			f"their own broker is how 'config not code' becomes a lie. Nothing was changed."
		)

	doc = frappe.get_doc(TEMPLATE, name) if existing else frappe.new_doc(TEMPLATE)
	if not existing:
		doc.template_name = name
	doc.document_type = document_type

	for field in (
		"label_en",
		"label_es",
		"description_en",
		"description_es",
		"external_system",
		"standard_reference",
		"signature_role",
		"notes",
	):
		value = as_str(args, field)
		if value:
			doc.set(field, value)

	tiers = args.get("applicable_tiers")
	if tiers not in (None, ""):
		parsed = parse_tiers(tiers)
		if not parsed:
			raise ToolError(
				f"applicable_tiers {tiers!r} names no tier. Use any of {', '.join(TIERS)}, "
				f"comma-separated, or omit it — which means every tier. Nothing was changed."
			)
		doc.applicable_tiers = ", ".join(parsed)

	for field in ("requires_signature", "requires_external_filing", "enabled"):
		value = as_bool(args, field, None)
		if value is not None:
			doc.set(field, 1 if value else 0)
	sequence = as_int(args, "sequence")
	if sequence is not None:
		doc.sequence = sequence

	for field in ("required_fields", "auto_populate_map"):
		value = args.get(field)
		if value in (None, ""):
			continue
		if isinstance(value, str):
			try:
				json.loads(value)
			except (json.JSONDecodeError, ValueError, TypeError):
				raise ToolError(f"{field} must be valid JSON. Nothing was changed.") from None
			doc.set(field, value)
		else:
			doc.set(field, json.dumps(value, sort_keys=True, default=str))

	source = as_str(args, "auto_populate_from")
	if source:
		if not compat.doctype_exists(source):
			raise ToolError(
				f"auto_populate_from names {source!r}, which is not a DocType on this site. "
				f"Nothing was changed."
			)
		doc.auto_populate_from = source

	if existing:
		doc.save(ignore_permissions=True)
	else:
		doc.insert(ignore_permissions=True)

	data = {
		"template": doc.name,
		"document_type": doc.document_type,
		"created": not existing,
		"updated": bool(existing),
		"applicable_tiers": list(parse_tiers(doc.applicable_tiers)) or list(TIERS),
		"requires_signature": bool(compat.checked(doc.requires_signature)),
		"requires_external_filing": bool(compat.checked(doc.requires_external_filing)),
		"external_system": doc.external_system,
		"enabled": bool(compat.checked(doc.enabled)),
		"declared_fields": _declared_fields(dict(doc.as_dict())),
		"note": (
			"A template is the SHAPE of a document. It does not require anything of any "
			"shipment on its own — set_destination_requirements is what makes a destination "
			"ask for it."
		),
	}
	return ToolResult(
		data,
		f"{'updated' if existing else 'created'} trade document template {doc.name} ({doc.document_type})",
		"none → 0 (draft)" if not existing else "",
	)


# ── 14. list_trade_document_templates ───────────────────────────────────────
def list_trade_document_templates(args: dict) -> ToolResult:
	"""What kinds of document this site can issue."""
	compat.require_doctype(TEMPLATE, "Run `bench --site <site> migrate`.")
	language = _language(args)
	missing: list = []
	filters = {}
	if not as_bool(args, "include_disabled", False):
		filters["enabled"] = 1
	document_type = as_str(args, "document_type")
	if document_type:
		filters["document_type"] = document_type

	rows = frappe.db.get_all(
		TEMPLATE,
		filters=filters,
		fields=[
			"name",
			"document_type",
			"label_en",
			"label_es",
			"description_en",
			"description_es",
			"applicable_tiers",
			"requires_signature",
			"signature_role",
			"requires_external_filing",
			"external_system",
			"standard_reference",
			"auto_populate_from",
			"required_fields",
			"sequence",
			"enabled",
			"shipped_default",
			"notes",
		],
		order_by="sequence asc, name asc",
		limit=as_limit(args),
	)
	tier = as_str(args, "destination_tier")
	wanted_tier = normalise_tier(tier) if tier else ""

	templates = []
	for row in rows:
		row = dict(row)
		if wanted_tier and not applies_to_tier(row.get("applicable_tiers"), wanted_tier):
			continue
		templates.append(
			{
				"template": row["name"],
				"document_type": row.get("document_type"),
				"label": _resolve_label(row, "label", language, missing, row["name"]) or row["name"],
				"description": _resolve_label(row, "description", language, missing, row["name"]) or None,
				"applicable_tiers": list(parse_tiers(row.get("applicable_tiers"))) or list(TIERS),
				"requires_signature": bool(compat.checked(row.get("requires_signature"))),
				"signature_role": row.get("signature_role") or None,
				"requires_external_filing": bool(compat.checked(row.get("requires_external_filing"))),
				"external_system": row.get("external_system") or None,
				"standard_reference": row.get("standard_reference") or None,
				"auto_populate_from": row.get("auto_populate_from") or None,
				"declared_fields": _declared_fields(row),
				"sequence": row.get("sequence"),
				"enabled": bool(compat.checked(row.get("enabled"))),
				"shipped_default": bool(compat.checked(row.get("shipped_default"))),
				"notes": row.get("notes") or None,
			}
		)

	data = {
		"templates": templates,
		"count": len(templates),
		"tiers": list(TIERS),
		"language": language,
		"untranslated": missing,
	}
	return ToolResult(data, f"{len(templates)} trade document template(s)")


# ── 15. get_destination_requirements ────────────────────────────────────────
def get_destination_requirements(args: dict) -> ToolResult:
	"""What paperwork a destination asks for, before a shipment exists."""
	compat.require_doctype(REQUIREMENT, "Run `bench --site <site> migrate`.")
	tier = normalise_tier(as_str(args, "destination_tier", required=True))
	country = normalise_country(as_str(args, "destination_country"))
	company = as_str(args, "company")
	if company:
		company = resolve_company(company, required=True)
	language = _language(args)
	missing: list = []

	if country and tier != INTERNATIONAL:
		raise ToolError(
			f"destination_country is only read on {INTERNATIONAL} lookups; this one is {tier}. "
			f"A {tier} rule applies to every {tier} shipment, so naming a country here would "
			f"return an answer that is not the one you asked for."
		)

	rules = requirements_for(tier, country, company)
	templates = _templates_by_name([rule["trade_document_template"] for rule in rules])
	requirements = []
	for rule in rules:
		template = templates.get(rule["trade_document_template"], {})
		requirements.append(
			{
				"requirement": rule["name"],
				"template": rule["trade_document_template"],
				"label": _resolve_label(template, "label", language, missing, rule["trade_document_template"])
				or rule["trade_document_template"],
				"document_type": template.get("document_type"),
				"required": bool(compat.checked(rule.get("required"))),
				"from": destination_label(rule.get("destination_tier"), rule.get("destination_country")),
				"company_scope": rule.get("company") or None,
				"requires_external_filing": bool(compat.checked(template.get("requires_external_filing"))),
				"external_system": template.get("external_system") or None,
				"requires_signature": bool(compat.checked(template.get("requires_signature"))),
				"standard_reference": template.get("standard_reference") or None,
				"sequence": rule.get("sequence"),
				"notes": rule.get("notes") or template.get("notes") or None,
				"template_exists": bool(template),
				"template_enabled": bool(compat.checked(template.get("enabled"))) if template else False,
			}
		)

	required = [entry for entry in requirements if entry["required"]]
	data = {
		"destination": destination_label(tier, country),
		"destination_tier": tier,
		"destination_country": country or None,
		"company": company or None,
		"requirements": requirements,
		"count": len(requirements),
		"required_count": len(required),
		"external_filings": sorted(
			{entry["external_system"] for entry in requirements if entry["external_system"]}
		),
		"language": language,
		"untranslated": missing,
		"note": (
			f"A country's rules ADD to the tier's rather than replacing them, so this answer "
			f"is every {tier} rule plus every rule written for "
			f"{country or 'the country, where one is named'}."
			if tier == INTERNATIONAL
			else f"Every rule written for {tier} shipments."
		),
	}
	if not requirements:
		data["note"] = (
			f"NOTHING IS CONFIGURED for {destination_label(tier, country)}. A shipment created "
			f"for it would get an empty checklist — which means nothing is being tracked, not "
			f"that nothing is needed. set_destination_requirements is where that is fixed."
		)
	return ToolResult(
		data,
		f"{len(requirements)} document(s) for {destination_label(tier, country)} ({len(required)} required)",
	)


# ── 16. set_destination_requirements ────────────────────────────────────────
def set_destination_requirements(args: dict) -> ToolResult:
	"""Configure what a destination asks for. A new export market is rows, not a release.

	TAKES A LIST AND IS ADDITIVE BY DEFAULT. A caller stating the four documents
	a country needs should not silently delete a fifth somebody added last week
	for a reason nobody wrote down. `replace=true` is there for the caller who
	means to restate the destination outright, and it says what it removed.
	"""
	compat.require_doctype(REQUIREMENT, "Run `bench --site <site> migrate`.")
	tier = normalise_tier(as_str(args, "destination_tier", required=True))
	country = normalise_country(as_str(args, "destination_country"))
	company = as_str(args, "company")
	if company:
		company = resolve_company(company, required=True)

	if country and tier != INTERNATIONAL:
		raise ToolError(
			f"only an {INTERNATIONAL} rule carries a country; this one is {tier}. A {tier} rule "
			f"naming a country would apply to every {tier} shipment, which is not what naming "
			f"one means. Nothing was changed."
		)
	if tier == INTERNATIONAL and not country and not as_bool(args, "all_countries", False):
		raise ToolError(
			f"an {INTERNATIONAL} rule with no country applies to EVERY export, which is right "
			f"for an AES declaration and wrong for an import permit. Pass destination_country, "
			f"or all_countries=true if you mean every export. Nothing was changed."
		)

	# `requirements` accepts three shapes because a model will send all three.
	raw = args.get("requirements")
	if raw in (None, ""):
		raise ToolError(
			"requirements is required: a list of template names, or of objects with "
			"`trade_document_template`, `required`, `sequence` and `notes`. Nothing was changed."
		)
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except (json.JSONDecodeError, ValueError, TypeError):
			raw = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
	if isinstance(raw, dict):
		raw = [raw]
	if not isinstance(raw, list) or not raw:
		raise ToolError(
			f"requirements must be a non-empty list, got {type(raw).__name__}. Nothing was changed."
		)

	wanted = []
	for entry in raw:
		if isinstance(entry, str):
			wanted.append({"trade_document_template": entry.strip(), "required": True})
			continue
		if not isinstance(entry, dict):
			raise ToolError(
				f"each requirement is a template name or an object, got {type(entry).__name__}. "
				f"Nothing was changed."
			)
		template = str(entry.get("trade_document_template") or entry.get("template") or "").strip()
		if not template:
			raise ToolError("each requirement has to name a trade_document_template. Nothing was changed.")
		wanted.append(
			{
				"trade_document_template": template,
				"required": entry.get("required", True),
				"sequence": entry.get("sequence"),
				"notes": entry.get("notes"),
			}
		)

	for entry in wanted:
		if not frappe.db.exists(TEMPLATE, entry["trade_document_template"]):
			raise ToolError(
				f"{TEMPLATE} {entry['trade_document_template']!r} does not exist. "
				f"create_trade_document_template adds one. NOTHING WAS CHANGED — the whole list "
				f"is checked before anything is written, so a typo in the fourth entry does not "
				f"leave three rules half-applied."
			)

	existing = {
		row["trade_document_template"]: dict(row)
		for row in frappe.db.get_all(
			REQUIREMENT,
			filters={
				"destination_tier": tier,
				"destination_country": country or "",
				"company": company or "",
			},
			fields=["name", "trade_document_template", "required", "sequence", "notes", "enabled"],
		)
	}

	created, updated, removed = [], [], []
	for entry in wanted:
		template = entry["trade_document_template"]
		required = 1 if entry.get("required", True) not in (False, 0, "0", "false", "no") else 0
		if template in existing:
			doc = frappe.get_doc(REQUIREMENT, existing[template]["name"])
			doc.required = required
			doc.enabled = 1
			if entry.get("sequence") is not None:
				doc.sequence = int(entry["sequence"])
			if entry.get("notes"):
				doc.notes = str(entry["notes"])
			doc.save(ignore_permissions=True)
			updated.append(doc.name)
			continue
		doc = frappe.new_doc(REQUIREMENT)
		doc.destination_tier = tier
		doc.destination_country = country
		doc.trade_document_template = template
		doc.required = required
		doc.enabled = 1
		doc.company = company or None
		if entry.get("sequence") is not None:
			doc.sequence = int(entry["sequence"])
		if entry.get("notes"):
			doc.notes = str(entry["notes"])
		doc.insert(ignore_permissions=True)
		created.append(doc.name)

	if as_bool(args, "replace", False):
		keep = {entry["trade_document_template"] for entry in wanted}
		for template, row in existing.items():
			if template in keep:
				continue
			# DISABLED RATHER THAN DELETED. A shipment made last season under
			# this rule is audited against what was asked for then, and a rule
			# that has been deleted cannot answer "why did we need that".
			frappe.db.set_value(REQUIREMENT, row["name"], "enabled", 0)
			removed.append({"requirement": row["name"], "template": template})

	data = {
		"destination": destination_label(tier, country),
		"destination_tier": tier,
		"destination_country": country or None,
		"company": company or None,
		"created": created,
		"updated": updated,
		"disabled": removed,
		"count": len(created) + len(updated),
		"note": (
			"Existing shipments are NOT re-checked against this change. Their checklists were "
			"a snapshot of what this destination asked for on the day they were created — "
			"get_shipment_readiness reports the drift under `requirement_drift`, because a "
			"requirement added today silently appearing on a shipment that has already sailed "
			"would be worse than the drift."
		),
	}
	if removed:
		data["removal_note"] = (
			f"{len(removed)} rule(s) were DISABLED rather than deleted. A shipment made under "
			f"one of them is audited against what was asked for then, and a deleted rule cannot "
			f"answer why it was needed."
		)
	summary = (
		f"{destination_label(tier, country)}: {len(created)} requirement(s) added, {len(updated)} updated"
	)
	if removed:
		summary += f", {len(removed)} disabled"
	return ToolResult(data, summary, "none → 0 (draft)" if created else "")


# ── seeding ─────────────────────────────────────────────────────────────────
def install_trade_documents(overwrite: bool = False) -> dict:
	"""Seed the shipped templates and destination rules. Idempotent, NEVER raises.

	NEVER OVERWRITES BY DEFAULT, and that is the whole promise of "config not
	code": a template an operator tuned for their own broker — changed a field,
	turned off a document their trade does not use — being reset by the next
	`bench migrate` is what would make this module a lie. `overwrite=True` exists
	for a deliberate reset and is not what migration calls.

	THE REQUIREMENTS ARE SEEDED MORE CAUTIOUSLY STILL. A rule is written only
	where NO rule for that destination and template exists at all, enabled or
	disabled — because an operator who turned a requirement off did so on
	purpose, and a seeder that put it back would be overruling them every time
	they upgraded.

	Never raises, for the reason `install_wizard_definitions` does not: this runs
	from `after_migrate`, and an exception there takes somebody's whole migration
	down.
	"""
	report = {
		"templates_created": [],
		"templates_existing": [],
		"requirements_created": [],
		"requirements_existing": [],
		"failed": [],
	}
	if not compat.doctype_exists(TEMPLATE):
		return report

	for spec in SHIPPED_TEMPLATES:
		name = spec["template_name"]
		try:
			exists = frappe.db.exists(TEMPLATE, name)
			if exists and not overwrite:
				report["templates_existing"].append(name)
				continue
			if exists:
				frappe.delete_doc(TEMPLATE, name, force=True, ignore_permissions=True)

			doc = frappe.new_doc(TEMPLATE)
			doc.template_name = name
			doc.__newname = name
			for field in (
				"document_type",
				"label_en",
				"label_es",
				"description_en",
				"description_es",
				"external_system",
				"standard_reference",
				"signature_role",
				"auto_populate_from",
				"notes",
			):
				if spec.get(field):
					doc.set(field, spec[field])
			doc.applicable_tiers = spec.get("tiers") or ""
			doc.sequence = int(spec.get("sequence") or 50)
			doc.requires_signature = 1 if spec.get("requires_signature") else 0
			doc.requires_external_filing = 1 if spec.get("requires_external_filing") else 0
			doc.enabled = 1
			doc.shipped_default = 1
			if spec.get("required_fields"):
				doc.required_fields = json.dumps(spec["required_fields"], indent=1)
			if spec.get("auto_populate_map"):
				doc.auto_populate_map = json.dumps(spec["auto_populate_map"], indent=1)
			# `auto_populate_from` is a Link to DocType, and a site without the
			# source doctype would fail the whole insert over a convenience
			# feature. Dropped rather than guessed at — the same trade
			# `signing_evidence.record` makes with its own links.
			if doc.auto_populate_from and not compat.doctype_exists(doc.auto_populate_from):
				doc.auto_populate_from = None
			doc.insert(ignore_permissions=True)
			report["templates_created"].append(name)
		except Exception as exc:  # pragma: no cover - a site shaping the doctype differently
			report["failed"].append({"template": name, "reason": f"{type(exc).__name__}: {exc}"})

	if not compat.doctype_exists(REQUIREMENT):
		return report

	for tier, country, entries in SHIPPED_REQUIREMENTS:
		for template, required, sequence, notes in entries:
			label = f"{tier}{' — ' + country if country else ''} / {template}"
			try:
				if not frappe.db.exists(TEMPLATE, template):
					report["failed"].append({"requirement": label, "reason": "its template was not seeded"})
					continue
				# ENABLED IS NOT IN THE FILTER. A rule an operator disabled is a
				# decision, and a seeder that re-created it would overrule them
				# on every upgrade.
				# v0.230.2. ANY COMPANY, AND A BLANK COUNTRY IS BLANK WHETHER IT IS
				# "" OR NULL. OML's rules were seeded with a company stamped on them
				# and NULL countries; a filter for company "" / country "" missed every
				# one, the insert below hit the controller's duplicate check, and a
				# migrate printed ~26 "could not seed" lines for rules that were there.
				clash = frappe.db.get_all(
					REQUIREMENT,
					filters={
						"destination_tier": tier,
						"destination_country": country if country else ("is", "not set"),
						"trade_document_template": template,
					},
					fields=["name"],
					limit=1,
				)
				if clash:
					report["requirements_existing"].append(label)
					continue
				doc = frappe.new_doc(REQUIREMENT)
				doc.destination_tier = tier
				doc.destination_country = country
				doc.trade_document_template = template
				doc.required = 1 if required else 0
				doc.enabled = 1
				doc.sequence = sequence
				doc.notes = notes
				doc.shipped_default = 1
				doc.insert(ignore_permissions=True)
				report["requirements_created"].append(label)
			except Exception as exc:  # pragma: no cover
				# The controller's own duplicate refusal means the rule is there in a
				# shape the lookup above did not match — present, not failed.
				if "already has a rule requiring" in str(exc):
					report["requirements_existing"].append(label)
					continue
				report["failed"].append({"requirement": label, "reason": f"{type(exc).__name__}: {exc}"})

	return report
