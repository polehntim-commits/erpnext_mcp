# SPDX-License-Identifier: MIT
"""Land and the agreements over it: the Parcel register and the Lease register.

WHY THE PARCEL IS THE UNIT. A family's land is described four different ways by
four different documents — an appraisal talks about "Red Camp", a tax statement
about parcel 1N-13E-8-1200, a deed about a metes-and-bounds description, and the
balance sheet about a Fixed Asset with a purchase price. Only one of those is a
unit everyone agrees on, and it is the parcel as the county assessor knows it. So
the register is keyed on the parcel, carries the assessor's number as the
identifier a third party will recognise, and links out to the Asset rather than
trying to be one.

WHY APPRAISED VALUE LIVES HERE AND NOT ON THE ASSET. They are different numbers
and they are meant to be. `gross_purchase_amount` on an Asset is what was paid,
which is what the balance sheet must carry; `appraised_value` on a Parcel is what
it is worth, which is what an estate plan turns on. A single field would force
one of those two questions to be answered wrongly, and `link_parcel_to_asset`
reports the gap between them rather than resolving it.

WHY DIRECTION IS A FIELD. A lease register that only holds leases in one
direction is a register that will be wrong the first time the family both lets
ground out and takes ground in — which for an operation of this shape is
immediately. `direction` says which side we are on, the caller states it rather
than the code guessing it from a name string, and `create_lease` reports whether
the claim looks consistent with the parties named instead of refusing on a
string comparison it cannot win. A legal name is "Highland Ltd Liability Co." and
a Company docname is "Highland LLC"; no amount of matching makes that reliable,
and a refusal built on it would be a refusal nobody could get past.

WHY A CONVEYANCE IS ITS OWN TOOL AND NOT AN EDIT. `update_parcel` refuses to move
a parcel between entities, and that refusal is right: ground changing hands is a
legal event with a date, an instrument behind it and consequences for two sets of
books, and a tool that let it happen by changing a field would record none of
those. `convey_parcel` is the door that refusal points at. It destroys the record
on one entity's books and builds it on the other's, carries everything that hung
off it across, and writes the event onto the survivor — because after a
conveyance there is exactly one document left to carry the history.

WHAT NOTHING HERE DOES. It does not post. A lease with rent on it produces no
Journal Entry, no receivable and no schedule — recording an agreement and booking
its consequences are separate acts, and the tool that records the agreement is not
the tool that can move money. Neither does a conveyance: basis transfer and
gain-or-loss recognition are entries somebody writes on purpose, with a narrative
of their own, and `convey_parcel` says which ones are owed rather than guessing
at them. It does not expire a lease because a date has passed either: see the
note in `list_leases` on why a calendar is not a decision.
"""

import json

import frappe

from .. import compat, geo
from ..args import (
	as_bool,
	as_choice,
	as_date,
	as_float,
	as_int,
	as_limit,
	as_str,
	resolve_company,
)
from ..errors import ToolError
from ..result import ToolResult
from . import files

PARCEL = "Parcel"
LEASE = "Lease"
RELATED_PARTY = "Related Party"
GOVERNANCE_DOCUMENT = "Governance Document"
CONVEYANCE_EVENT = "Parcel Conveyance Event"

#: Every doctype this app ships that points at a Parcel, and the field it points
#: with. A conveyance repoints all of them, and a register missing from this
#: tuple is a register left pointing at a document that no longer exists — which
#: Frappe would refuse to let the delete happen over, so the failure would be a
#: refusal mid-conveyance rather than a silent orphan. That is the safe direction,
#: and `test_realestate` checks the tuple against the shipped DocType JSON anyway,
#: so a doctype added in a later release cannot be forgotten quietly.
#:
#: DOCNAMES DO NOT FOLLOW, AND DO NOT NEED TO. A Field, an Irrigation Zone and a
#: Housing Unit are all named `"<their name> - <PARCEL abbr>"` — the parcel's key,
#: not the company's — and the conveyance preserves that key. So every one of
#: those docnames is still correct on the other side; only the link moves.
PARCEL_REFERRERS = (
	(LEASE, "parcel"),
	("Field", "parcel"),
	("Irrigation Zone", "parcel"),
	("Housing Unit", "parcel"),
	("Housing Assignment", "parcel"),
	# v0.80.0. The crop growing on the ground follows the ground, and this entry
	# is here because `test_realestate` refused to let it be forgotten — which is
	# the check working exactly as its own comment says it should.
	#
	# ITS `company` IS DELIBERATELY NOT IN `REFERRER_COMPANY_FIELDS` BELOW, and
	# that is the interesting half. A Field's `owning_entity` is a statement about
	# who owns the ground, so it must move with the deed. A Biological Asset's
	# `company` is a statement about WHO CARRIES THE CROP ON ITS BALANCE SHEET,
	# which a conveyance does not settle: selling the land under a standing
	# orchard and selling the orchard are two transactions, and which of them
	# happened is a question about the deed that this app cannot read. Moving the
	# carrying entity automatically would silently transfer an asset — and its
	# whole valuation history — between two sets of books on the strength of a
	# land transfer nobody said included it. So the link follows and the
	# accounting does not, and whoever conveyed the parcel is left to decide.
	("Biological Asset", "parcel"),
	# v0.169.0. The Parcels a recorded lot line adjustment wrote. They follow the
	# ground for the reason a Field does: the adjustment is the record of how the
	# parcel got its present line, and a conveyance does not unmake that history.
	("Lot Line Adjustment", "parcel_1"),
	("Lot Line Adjustment", "parcel_2"),
)

#: Referring registers that also carry their own `owning_entity`, which has to
#: move with the parcel or the register will disagree with the ground it
#: describes. Housing Assignment has none — it hangs off a Housing Unit, which
#: does — and Lease's is the entity that is party to the agreement, which a
#: conveyance does NOT change: a lease signed by the old owner is still that
#: entity's contract until somebody novates it, which is its own document.
REFERRER_COMPANY_FIELDS = {
	"Field": "owning_entity",
	"Irrigation Zone": "owning_entity",
	"Housing Unit": "owning_entity",
}

#: The parcel fields a conveyance copies across unchanged. Identity, geography,
#: size and valuation are facts about the ground; they do not change because the
#: deed did. `owning_entity`, `title_holder`, `appraisal_document` and
#: `related_asset` are each decided separately below, for four different reasons.
CONVEYED_FIELDS = (
	"parcel_name",
	"abbr",
	"parcel_id",
	"use_type",
	# v0.54.0. Conveyed for the reason the rest of this tuple is: the camp this
	# ground is part of does not change because the deed did. A parcel that moves
	# from the trust to the LLC is the same forty acres with the same cabins on
	# it, and dropping the branch would empty the hiring wizard's housing list
	# for that camp on the day of a paper transaction nobody in the orchard saw.
	"branch",
	"county",
	"state",
	"acreage",
	"address",
	"appraised_value",
	"appraised_as_of",
	"appraiser",
	"notes",
)

#: The shortest `reason` a conveyance is allowed to carry. A conveyance with no
#: narrative is a parcel that moved and nobody can say why, which is the one
#: question an estate attorney will certainly ask.
MIN_REASON = 12

_PARCEL_FIELDS = (
	"name",
	"parcel_name",
	# The parcel's own short key. Every Field, Irrigation Zone and Housing Unit
	# docname is suffixed with it, which is why `convey_parcel` has to be able to
	# read it — a conveyance that let the key be re-derived on the other side
	# would file the parcel's future blocks under a different suffix from its
	# existing ones.
	"abbr",
	"owning_entity",
	"parcel_id",
	"use_type",
	# v0.54.0. The operating unit this ground belongs to, and the ONLY join
	# between a person and a camp: an Employee carries a Branch, a Housing Unit
	# stands on a Parcel, and nothing else connects the two.
	# `list_available_housing` resolves a branch to its parcels through this
	# column. Read through `compat.existing_fields`, so a site that has not
	# migrated yet reads a parcel without it rather than failing on a column that
	# is not there.
	"branch",
	"county",
	"state",
	"acreage",
	"address",
	"title_holder",
	"appraised_value",
	"appraised_as_of",
	"appraiser",
	"appraisal_document",
	"related_asset",
	# v0.32.0. The shape on the ground and everything derived from it. Selected
	# through `compat.existing_fields`, so a site that has not migrated yet reads
	# a parcel without them rather than failing on a column that is not there.
	"boundary_geojson",
	"boundary_centroid_lat",
	"boundary_centroid_lon",
	"boundary_bbox_geojson",
	"area_computed_acres",
	"notes",
	"creation",
	"owner",
)

_LEASE_FIELDS = (
	"name",
	"lease_name",
	"owning_entity",
	"parcel",
	"direction",
	"status",
	"lessor",
	"lessee",
	"counterparty",
	"effective_date",
	"expiration_date",
	"termination_date",
	"termination_reason",
	"rent_amount",
	"rent_frequency",
	"rent_terms",
	"lease_document",
	"governance_document",
	"notes",
	"creation",
	"owner",
)

#: How many rent periods a year each frequency has. `None` means "not
#: annualisable" — a crop share has no fixed number and a one-time payment is not
#: a rate. Reporting those as 0 would understate a portfolio's rent roll by
#: pretending an unknown is a zero, so they are excluded from the total and
#: counted separately instead.
PERIODS_PER_YEAR = {
	"Monthly": 12,
	"Quarterly": 4,
	"Semi-Annual": 2,
	"Annual": 1,
	"One-Time": None,
	"Crop Share": None,
	"Other": None,
}

#: Default window for "what is about to run out" in `list_leases`. A quarter is
#: the shortest notice period worth having and long enough to do something about.
DEFAULT_EXPIRING_DAYS = 90

#: Rows returned by the list tools before the cap bites. Land holdings are
#: measured in dozens, not thousands; a register that needs paging is one where
#: something else has gone wrong.
REGISTER_CAP = 500


# ── shared ──────────────────────────────────────────────────────────────────
def _require(doctype: str) -> None:
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _entity(args: dict, required: bool = True) -> str:
	"""The company these records hang off, under either name a caller will use.

	`owning_entity` is what the field is called; `company` is what every other
	tool in this app calls the same argument. Accepting both costs one line and
	saves a round trip every time a model reaches for the name it already knows.
	"""
	requested = as_str(args, "owning_entity") or as_str(args, "company")
	return resolve_company(requested, required=required)


def _money(value) -> float:
	return round(float(value or 0), 2)


def _date_str(value) -> str | None:
	return str(value or "") or None


def _describe_parcel(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"parcel_name": row.get("parcel_name"),
		"abbr": row.get("abbr") or None,
		"owning_entity": row.get("owning_entity"),
		"parcel_id": row.get("parcel_id") or None,
		"use_type": row.get("use_type") or None,
		"branch": row.get("branch") or None,
		"county": row.get("county") or None,
		"state": row.get("state") or None,
		"acreage": round(float(row.get("acreage") or 0), 2),
		"address": row.get("address") or None,
		"title_holder": row.get("title_holder") or None,
		"appraised_value": _money(row.get("appraised_value")),
		"appraised_as_of": _date_str(row.get("appraised_as_of")),
		"appraiser": row.get("appraiser") or None,
		"appraisal_document": row.get("appraisal_document") or None,
		"related_asset": row.get("related_asset") or None,
		# v0.32.0. THE CENTROID IS REPORTED AND THE POLYGON IS NOT. A boundary is
		# a few kilobytes of coordinates and every list of parcels would carry one
		# per row; `mapped` says whether there is one and `get_parcel` hands back
		# the shape itself for the one parcel somebody asked about.
		"mapped": bool(str(row.get("boundary_geojson") or "").strip()),
		"boundary_centroid": _centroid(row),
		"area_computed_acres": round(float(row.get("area_computed_acres") or 0), 4) or None,
		"notes": row.get("notes") or None,
	}


def _centroid(row: dict) -> dict | None:
	"""`{lat, lon}` for a mapped parcel, or None. Never `{lat: 0, lon: 0}`.

	Null island is a real place and an unset coordinate looks exactly like it, so
	a parcel with no boundary reports nothing rather than a point in the Gulf of
	Guinea that a map would happily fly to.
	"""
	latitude = row.get("boundary_centroid_lat")
	longitude = row.get("boundary_centroid_lon")
	if latitude in (None, "") or longitude in (None, ""):
		return None
	return {"lat": round(float(latitude), 7), "lon": round(float(longitude), 7)}


def _describe_lease(row: dict, today: str = "") -> dict:
	frequency = row.get("rent_frequency") or ""
	periods = PERIODS_PER_YEAR.get(frequency)
	amount = _money(row.get("rent_amount"))
	expiration = _date_str(row.get("expiration_date"))
	status = row.get("status") or ""
	return {
		"name": row.get("name"),
		"lease_name": row.get("lease_name"),
		"owning_entity": row.get("owning_entity"),
		"parcel": row.get("parcel") or None,
		"direction": row.get("direction") or None,
		"status": status or None,
		"lessor": row.get("lessor") or None,
		"lessee": row.get("lessee") or None,
		"counterparty": row.get("counterparty") or None,
		"effective_date": _date_str(row.get("effective_date")),
		"expiration_date": expiration,
		"termination_date": _date_str(row.get("termination_date")),
		"termination_reason": row.get("termination_reason") or None,
		"rent_amount": amount,
		"rent_frequency": frequency or None,
		"annualised_rent": round(amount * periods, 2) if (periods and amount) else None,
		"rent_terms": row.get("rent_terms") or None,
		"lease_document": row.get("lease_document") or None,
		"governance_document": row.get("governance_document") or None,
		"notes": row.get("notes") or None,
		"past_expiration": bool(today and expiration and expiration < today and status == "Active"),
	}


def parcel_row(parcel: str, company: str = "") -> dict:
	"""One Parcel as a dict, from its docname or its parcel name.

	Two ways because a caller who created it knows it as `"Red Camp"` while
	everything that links to it holds `"Red Camp - HLD"`. Resolving both is what
	stops a model having to remember which of the two a given tool wanted.
	"""
	parcel = (parcel or "").strip()
	if not parcel:
		raise ToolError("parcel is required (a Parcel docname, or the parcel name such as 'Red Camp')")
	fields = compat.existing_fields(PARCEL, _PARCEL_FIELDS)

	if frappe.db.exists(PARCEL, parcel):
		row = frappe.db.get_value(PARCEL, parcel, fields, as_dict=True)
		if company and row and row.get("owning_entity") != company:
			raise ToolError(f"Parcel {parcel!r} belongs to {row.get('owning_entity')!r}, not {company!r}")
		return dict(row)

	filters = {"parcel_name": parcel}
	if company:
		filters["owning_entity"] = company
	matches = frappe.db.get_all(PARCEL, filters=filters, fields=fields, limit=25)
	if len(matches) == 1:
		return dict(matches[0])
	if len(matches) > 1:
		names = ", ".join(sorted(str(match.get("name")) for match in matches))
		raise ToolError(
			f"{parcel!r} matches {len(matches)} parcels: {names}. Pass the docname, or set "
			"owning_entity to narrow it."
		)
	scope = f" for {company}" if company else ""
	raise ToolError(f"no Parcel called {parcel!r}{scope}. list_parcels has the register.")


def lease_row(lease: str, company: str = "") -> dict:
	"""One Lease as a dict, from its docname or its lease name."""
	lease = (lease or "").strip()
	if not lease:
		raise ToolError("lease is required (a Lease docname, or the lease name)")
	fields = compat.existing_fields(LEASE, _LEASE_FIELDS)

	if frappe.db.exists(LEASE, lease):
		row = frappe.db.get_value(LEASE, lease, fields, as_dict=True)
		if company and row and row.get("owning_entity") != company:
			raise ToolError(f"Lease {lease!r} belongs to {row.get('owning_entity')!r}, not {company!r}")
		return dict(row)

	filters = {"lease_name": lease}
	if company:
		filters["owning_entity"] = company
	matches = frappe.db.get_all(LEASE, filters=filters, fields=fields, limit=25)
	if len(matches) == 1:
		return dict(matches[0])
	if len(matches) > 1:
		names = ", ".join(sorted(str(match.get("name")) for match in matches))
		raise ToolError(
			f"{lease!r} matches {len(matches)} leases: {names}. Pass the docname, or set "
			"owning_entity to narrow it."
		)
	scope = f" for {company}" if company else ""
	raise ToolError(f"no Lease called {lease!r}{scope}. list_leases has the register.")


def _check_link(doctype: str, value: str, company: str, company_field: str, label: str) -> str:
	"""A link argument that must exist and belong to the same company."""
	if not frappe.db.exists(doctype, value):
		raise ToolError(f"no {doctype} named {value!r}. Nothing was created.")
	owner = frappe.db.get_value(doctype, value, company_field)
	if owner and company and owner != company:
		raise ToolError(f"{label} {value!r} belongs to {owner!r}, not {company!r}. Nothing was created.")
	return value


def _asset_for_parcel(asset: str, company: str, exclude_parcel: str = "") -> dict:
	"""Validate an Asset as a parcel's balance-sheet counterpart, and describe it.

	The same checks whether the link is made at creation or afterwards with
	`link_parcel_to_asset`, because a rule that only one of two doors enforces is
	a rule the other door can be used to get around.
	"""
	if not frappe.db.exists("Asset", asset):
		raise ToolError(
			f"no Asset named {asset!r} on this site. Assets are created with create_asset, or "
			"in the Desk. Nothing was changed."
		)
	fields = compat.existing_fields(
		"Asset",
		("name", "asset_name", "company", "gross_purchase_amount", "asset_category", "status", "docstatus"),
	)
	row = dict(frappe.db.get_value("Asset", asset, fields, as_dict=True) or {})
	if company and row.get("company") and row["company"] != company:
		raise ToolError(
			f"Asset {asset!r} belongs to company {row['company']!r}, but this parcel belongs to "
			f"{company!r}. A parcel and the asset carrying it have to be on the same books. "
			"Nothing was changed."
		)
	filters = {"related_asset": asset}
	claimed = [
		name
		for name in (frappe.db.get_all(PARCEL, filters=filters, pluck="name", limit=25) or [])
		if name != exclude_parcel
	]
	if claimed:
		raise ToolError(
			f"Asset {asset!r} is already linked to parcel {claimed[0]!r}. One asset carries one "
			"parcel: if the asset really covers both, the parcels should share an asset only "
			"after somebody has decided how to split the cost. Nothing was changed."
		)
	return row


def _attachment_argument(args: dict, url_key: str, what: str) -> tuple[str, bytes, str]:
	"""Read the `file_url` / `file_content` pair the archive tools also take.

	Returns `(file_url, content, file_name)`. Exactly one of the first two is
	ever non-empty. The size rule lives in `files.decode_base64_content` so this
	module cannot drift from `attach_file_to_document`'s ceiling.
	"""
	file_url = as_str(args, url_key)
	file_content = as_str(args, "file_content")
	file_name = as_str(args, "file_name")
	if file_content and file_url:
		raise ToolError(
			f"pass file_content ({what}, base64) or {url_key} (where it already lives), not "
			"both. Nothing was created."
		)
	if file_content and not file_name:
		raise ToolError(
			"file_name is required with file_content — an attachment nobody can identify "
			"later is not evidence of anything. Nothing was created."
		)
	content = files.decode_base64_content(file_content) if file_content else b""
	return file_url, content, file_name


def _attach(doctype: str, name: str, field: str, file_name: str, content: bytes):
	attachment = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"is_private": 1,
			"content": content,
			"attached_to_doctype": doctype,
			"attached_to_name": name,
			"attached_to_field": field,
		}
	).insert()
	frappe.db.set_value(doctype, name, field, attachment.get("file_url"))
	return attachment


# ── 78. create_parcel ───────────────────────────────────────────────────────
def create_parcel(args: dict) -> ToolResult:
	"""Register one parcel: where it is, how big, who holds title, what it is worth."""
	_require(PARCEL)
	company = _entity(args)
	parcel_name = as_str(args, "parcel_name", required=True)
	parcel_id = as_str(args, "parcel_id")

	existing = frappe.db.get_value(PARCEL, {"parcel_name": parcel_name, "owning_entity": company}, "name")
	if existing:
		raise ToolError(
			f"{company} already has a parcel called {parcel_name!r} ({existing}). One parcel per "
			"name per entity — change it with update_parcel, or name this one so the two can be "
			"told apart. Nothing was created."
		)
	if parcel_id:
		collision = frappe.db.get_value(PARCEL, {"parcel_id": parcel_id, "owning_entity": company}, "name")
		if collision:
			raise ToolError(
				f"parcel id {parcel_id!r} is already on {collision!r} for {company}. That number "
				"is the county's key, so two parcels sharing it means one of them is a typo. "
				"Nothing was created."
			)

	acreage = as_float(args.get("acreage"), "acreage")
	appraised_value = as_float(args.get("appraised_value"), "appraised_value")
	if acreage < 0:
		raise ToolError(f"acreage must be zero or more, got {acreage}. Nothing was created.")
	if appraised_value < 0:
		raise ToolError(f"appraised_value must be zero or more, got {appraised_value}. Nothing was created.")

	use_type = as_str(args, "use_type")
	if use_type:
		use_type = as_choice(PARCEL, "use_type", use_type, "use_type")

	title_holder = as_str(args, "title_holder")
	if title_holder:
		_require(RELATED_PARTY)
		title_holder = _check_link(RELATED_PARTY, title_holder, company, "company", "title_holder")

	appraisal_document = as_str(args, "appraisal_document")
	if appraisal_document:
		appraisal_document = _check_link(
			GOVERNANCE_DOCUMENT, appraisal_document, company, "company", "appraisal_document"
		)

	related_asset = as_str(args, "related_asset")
	asset = _asset_for_parcel(related_asset, company) if related_asset else None

	appraised_as_of = as_date(args, "appraised_as_of")

	doc = frappe.new_doc(PARCEL)
	doc.parcel_name = parcel_name
	doc.owning_entity = company
	for field, value in (
		("parcel_id", parcel_id),
		("use_type", use_type),
		# v0.54.0. Checked against the Branch table by the controller, where the
		# check can also be skipped on a site that has no Branch doctype — see
		# `Parcel._check_branch`. Passing it through here rather than validating
		# again keeps one rule in one place.
		("branch", as_str(args, "branch")),
		("county", as_str(args, "county")),
		("state", as_str(args, "state")),
		("address", as_str(args, "address")),
		("appraiser", as_str(args, "appraiser")),
		("notes", as_str(args, "notes")),
		("title_holder", title_holder),
		("appraisal_document", appraisal_document),
		("related_asset", related_asset),
		("appraised_as_of", appraised_as_of),
	):
		if value:
			doc.set(field, value)
	doc.acreage = acreage
	doc.appraised_value = appraised_value
	doc.insert()

	row = frappe.db.get_value(PARCEL, doc.name, compat.existing_fields(PARCEL, _PARCEL_FIELDS), as_dict=True)
	data = _describe_parcel(dict(row))
	data["asset"] = _asset_summary(asset, appraised_value) if asset else None
	data["note"] = (
		"Appraised value is what it is worth; the Fixed Asset, if there is one, carries what "
		"was paid. They are different numbers on purpose and nothing here reconciles them."
	)
	data["next_step"] = "Record leases over this ground with create_lease. " + (
		"Link the Fixed Asset that carries it with link_parcel_to_asset."
		if not related_asset
		else "File the appraisal in the archive with attach_governance_document and point "
		"appraisal_document at it."
		if not appraisal_document
		else "The register entry is complete."
	)
	warnings = []
	if appraised_value and not appraised_as_of:
		warnings.append(
			"An appraised value with no as-of date is a number without a date, which is not a "
			"valuation. Set appraised_as_of with update_parcel."
		)
	if appraised_value and not appraisal_document:
		warnings.append(
			"No appraisal document is linked. A value with no report behind it is a figure "
			"somebody remembered — file the report with attach_governance_document."
		)
	if warnings:
		data["warnings"] = warnings

	return ToolResult(
		data,
		f"registered parcel {parcel_name} ({round(acreage, 2)} ac) for {company} as {doc.name}"
		+ (f", appraised {appraised_value:,.2f}" if appraised_value else ""),
		docstatus_delta="none → 0 (created)",
	)


def _asset_summary(asset: dict, appraised_value: float) -> dict:
	"""What the Fixed Asset says, beside what the appraisal says."""
	book = _money(asset.get("gross_purchase_amount"))
	return {
		"name": asset.get("name"),
		"asset_name": asset.get("asset_name"),
		"asset_category": asset.get("asset_category"),
		"status": asset.get("status"),
		"gross_purchase_amount": book,
		"appraised_value": _money(appraised_value),
		"appraisal_over_book": round(_money(appraised_value) - book, 2) if (book or appraised_value) else 0.0,
		"note": (
			"gross_purchase_amount is cost and belongs on the balance sheet; appraised_value is "
			"market and belongs in the estate file. The difference is unrealised appreciation, "
			"not an error, and nothing here posts it."
		),
	}


# ── 79. update_parcel ───────────────────────────────────────────────────────
def update_parcel(args: dict) -> ToolResult:
	"""Change a registered parcel. Cannot re-key it and cannot move it between entities."""
	_require(PARCEL)
	requested = as_str(args, "parcel", required=True)
	requested_entity = _entity(args, required=False) or ""
	# A docname is unambiguous, so an entity argument beside one is a GUARD rather
	# than a filter — and the guard's refusal should say what moving a parcel
	# between entities actually is. A bare name still gets narrowed.
	entry = parcel_row(requested, "" if frappe.db.exists(PARCEL, requested) else requested_entity)
	company = entry["owning_entity"]

	if "parcel_name" in args or "new_parcel_name" in args:
		raise ToolError(
			"the parcel_name cannot be changed. The docname is built from it, and every lease, "
			"asset link and appraisal that points at this parcel points at that docname. "
			"Nothing was changed."
		)
	if requested_entity and requested_entity != company:
		raise ToolError(
			f"parcel {entry['name']} belongs to {company!r} and update_parcel cannot move it to "
			f"{requested_entity!r}. A parcel changing hands is a conveyance: record the new "
			"entity's parcel with create_parcel and note the transfer on both. Nothing was "
			"changed."
		)

	changes: dict = {}
	updates: dict = {}

	# v0.54.0 puts `branch` in this loop rather than beside `use_type` below,
	# because it is free text on this doctype and not a Select — the Branch check
	# belongs to the controller, which is also the only place that can skip it on
	# a site with no Branch doctype. Passing "" clears it, which is how a camp
	# that was consolidated into another one is unassigned.
	for field in ("parcel_id", "branch", "county", "state", "address", "appraiser", "notes"):
		if field in args:
			value = as_str(args, field)
			if value != str(entry.get(field) or ""):
				updates[field] = value or None
				changes[field] = [entry.get(field) or "", value]

	if updates.get("parcel_id"):
		collision = frappe.db.get_value(
			PARCEL,
			{"parcel_id": updates["parcel_id"], "owning_entity": company, "name": ("!=", entry["name"])},
			"name",
		)
		if collision:
			raise ToolError(
				f"parcel id {updates['parcel_id']!r} is already on {collision!r} for {company}. "
				"Nothing was changed."
			)

	if "use_type" in args:
		requested = as_str(args, "use_type")
		use_type = as_choice(PARCEL, "use_type", requested, "use_type") if requested else ""
		if use_type != str(entry.get("use_type") or ""):
			updates["use_type"] = use_type or None
			changes["use_type"] = [entry.get("use_type") or "", use_type]

	for field, number_label in (("acreage", "acreage"), ("appraised_value", "appraised_value")):
		if args.get(field) not in (None, ""):
			value = as_float(args.get(field), number_label)
			if value < 0:
				raise ToolError(f"{number_label} must be zero or more, got {value}. Nothing was changed.")
			if abs(value - float(entry.get(field) or 0)) > 1e-9:
				updates[field] = value
				changes[field] = [round(float(entry.get(field) or 0), 2), round(value, 2)]

	appraised_as_of = as_date(args, "appraised_as_of")
	if appraised_as_of and appraised_as_of != str(entry.get("appraised_as_of") or ""):
		updates["appraised_as_of"] = appraised_as_of
		changes["appraised_as_of"] = [str(entry.get("appraised_as_of") or ""), appraised_as_of]

	if "title_holder" in args:
		requested = as_str(args, "title_holder")
		if requested:
			_require(RELATED_PARTY)
			requested = _check_link(RELATED_PARTY, requested, company, "company", "title_holder")
		if requested != str(entry.get("title_holder") or ""):
			updates["title_holder"] = requested or None
			changes["title_holder"] = [entry.get("title_holder") or "", requested]

	if "appraisal_document" in args:
		requested = as_str(args, "appraisal_document")
		if requested:
			requested = _check_link(GOVERNANCE_DOCUMENT, requested, company, "company", "appraisal_document")
		if requested != str(entry.get("appraisal_document") or ""):
			updates["appraisal_document"] = requested or None
			changes["appraisal_document"] = [entry.get("appraisal_document") or "", requested]

	if "related_asset" in args:
		raise ToolError(
			"update_parcel does not set related_asset. Use link_parcel_to_asset: it checks the "
			"asset is on the same company's books and that no other parcel already claims it, "
			"and it reports the gap between cost and appraised value. Nothing was changed."
		)

	if not changes:
		raise ToolError(
			f"nothing to change on {entry['name']}. Pass at least one of parcel_id, county, "
			"state, address, acreage, use_type, appraised_value, appraised_as_of, appraiser, "
			"title_holder, appraisal_document or notes, with a value that differs from the "
			"current one."
		)

	doc = frappe.get_doc(PARCEL, entry["name"])
	for field, value in updates.items():
		doc.set(field, value)
	doc.save()

	after = frappe.db.get_value(
		PARCEL, entry["name"], compat.existing_fields(PARCEL, _PARCEL_FIELDS), as_dict=True
	)
	data = _describe_parcel(dict(after))
	data["changes"] = changes
	data["note"] = (
		"Leases over this parcel, and the Fixed Asset carrying it, are untouched: they point at "
		"the docname, which this tool cannot change."
	)
	if "appraised_value" in changes and "appraised_as_of" not in changes:
		data["warning"] = (
			"The appraised value changed but appraised_as_of did not. A new value carrying an "
			"old date is worse than no date at all."
		)
	summary = ", ".join(f"{field} {before!r} → {value!r}" for field, (before, value) in changes.items())
	return ToolResult(data, f"updated Parcel {entry['name']}: {summary}", docstatus_delta="")


# ── 80. list_parcels ────────────────────────────────────────────────────────
def list_parcels(args: dict) -> ToolResult:
	"""One entity's land register, with acreage and appraised value totalled."""
	_require(PARCEL)
	company = _entity(args)
	limit = min(as_limit(args), REGISTER_CAP)

	filters = {"owning_entity": company}
	county = as_str(args, "county")
	if county:
		filters["county"] = county
	use_type = as_str(args, "use_type")
	if use_type:
		filters["use_type"] = as_choice(PARCEL, "use_type", use_type, "use_type")
	title_holder = as_str(args, "title_holder")
	if title_holder:
		filters["title_holder"] = title_holder
	# v0.54.0. Which ground belongs to one camp — the read `list_available_housing`
	# does to turn a branch into a set of parcels, and the one an operator does to
	# find out why a camp shows no cabins.
	branch = as_str(args, "branch")
	if branch and compat.has_field(PARCEL, "branch"):
		filters["branch"] = branch

	fields = compat.existing_fields(PARCEL, _PARCEL_FIELDS)
	rows = frappe.db.get_all(PARCEL, filters=filters, fields=fields, order_by="parcel_name asc", limit=limit)
	parcels = [_describe_parcel(dict(row)) for row in rows]

	linked = as_bool(args, "linked_to_asset")
	if linked is not None:
		parcels = [parcel for parcel in parcels if bool(parcel["related_asset"]) is linked]

	total_acreage = round(sum(parcel["acreage"] for parcel in parcels), 2)
	total_value = round(sum(parcel["appraised_value"] for parcel in parcels), 2)
	by_use: dict = {}
	for parcel in parcels:
		bucket = by_use.setdefault(
			parcel["use_type"] or "unspecified", {"count": 0, "acreage": 0.0, "appraised_value": 0.0}
		)
		bucket["count"] += 1
		bucket["acreage"] = round(bucket["acreage"] + parcel["acreage"], 2)
		bucket["appraised_value"] = round(bucket["appraised_value"] + parcel["appraised_value"], 2)

	dated = [parcel["appraised_as_of"] for parcel in parcels if parcel["appraised_as_of"]]
	unvalued = [parcel["name"] for parcel in parcels if not parcel["appraised_value"]]
	unsupported = [
		parcel["name"] for parcel in parcels if parcel["appraised_value"] and not parcel["appraisal_document"]
	]

	total_count = frappe.db.count(PARCEL, {"owning_entity": company})
	data = {
		"owning_entity": company,
		"parcels": parcels,
		"count": len(parcels),
		"total_in_register": total_count,
		"total_acreage": total_acreage,
		"total_appraised_value": total_value,
		"average_per_acre": round(total_value / total_acreage, 2) if total_acreage else None,
		"by_use_type": by_use,
		"oldest_appraisal": min(dated) if dated else None,
		"newest_appraisal": max(dated) if dated else None,
		"parcels_without_value": unvalued,
		"limit": limit,
		"note": (
			"Totals cover the rows returned, not the whole register, whenever `count` is below "
			"`total_in_register`. Appraised value is market value and is not what the balance "
			"sheet carries."
		),
	}
	if len(parcels) < total_count and not (county or use_type or title_holder or linked is not None):
		data["warning"] = (
			f"{total_count} parcels are registered for {company} but the limit returned "
			f"{len(parcels)}. Raise `limit` before relying on the totals."
		)
	elif unsupported:
		data["warning"] = (
			f"{len(unsupported)} parcel(s) carry an appraised value with no appraisal document "
			f"linked: {', '.join(unsupported[:5])}. A value with no report behind it will not "
			"survive a question."
		)
	return ToolResult(
		data,
		f"{len(parcels)} parcel(s) for {company}: {total_acreage} acres, {total_value:,.2f} appraised",
	)


# ── 81. get_parcel ──────────────────────────────────────────────────────────
def get_parcel(args: dict) -> ToolResult:
	"""One parcel in full, with the leases over it and the asset carrying it."""
	_require(PARCEL)
	company = _entity(args, required=False) or ""
	entry = parcel_row(as_str(args, "parcel", required=True), company)
	company = entry["owning_entity"]
	data = _describe_parcel(entry)

	if entry.get("related_asset") and frappe.db.exists("Asset", entry["related_asset"]):
		asset_fields = compat.existing_fields(
			"Asset",
			("name", "asset_name", "company", "gross_purchase_amount", "asset_category", "status"),
		)
		asset = dict(frappe.db.get_value("Asset", entry["related_asset"], asset_fields, as_dict=True) or {})
		data["asset"] = _asset_summary(asset, data["appraised_value"])
	else:
		data["asset"] = None

	leases = []
	if compat.doctype_exists(LEASE):
		today = frappe.utils.today()
		lease_fields = compat.existing_fields(LEASE, _LEASE_FIELDS)
		leases = [
			_describe_lease(dict(row), today)
			for row in frappe.db.get_all(
				LEASE,
				filters={"parcel": entry["name"]},
				fields=lease_fields,
				order_by="effective_date desc",
				limit=REGISTER_CAP,
			)
		]
	data["leases"] = leases
	data["active_leases"] = len([lease for lease in leases if lease["status"] == "Active"])

	# THE ONE READ THAT HANDS BACK THE WHOLE POLYGON. `list_parcels` reports
	# `mapped` and a centroid; this is the tool a map calls for one parcel, and
	# giving it the shape saves a second round trip for the only thing it wants.
	data["boundary_geojson"] = entry.get("boundary_geojson") or None
	data["boundary_bbox_geojson"] = entry.get("boundary_bbox_geojson") or None

	data["attachments"] = frappe.db.count(
		"File", {"attached_to_doctype": PARCEL, "attached_to_name": entry["name"]}
	)
	data["note"] = (
		"`leases` covers every lease recorded against this parcel in either direction — read "
		"`direction` per row before assuming which way the rent flows."
	)
	return ToolResult(
		data,
		f"parcel {entry['name']}: {data['acreage']} acres in {data['county'] or 'an unrecorded county'}, "
		f"{len(leases)} lease(s) on record",
	)


# ── 82. create_lease ────────────────────────────────────────────────────────
def create_lease(args: dict) -> ToolResult:
	"""Record one lease, in whichever direction it runs. Books nothing."""
	_require(LEASE)
	company = _entity(args)
	lease_name = as_str(args, "lease_name", required=True)
	direction = as_choice(LEASE, "direction", as_str(args, "direction", required=True), "direction")
	lessor = as_str(args, "lessor", required=True)
	lessee = as_str(args, "lessee", required=True)
	effective_date = as_date(args, "effective_date", required=True)
	expiration_date = as_date(args, "expiration_date")
	status = as_choice(LEASE, "status", as_str(args, "status") or "Active", "status")

	existing = frappe.db.get_value(LEASE, {"lease_name": lease_name, "owning_entity": company}, "name")
	if existing:
		raise ToolError(
			f"{company} already has a lease called {lease_name!r} ({existing}). Name a renewal "
			"for its term — 'Mill Creek Ground Lease 2026' — so the two can be told apart. "
			"Nothing was created."
		)
	if lessor.strip().lower() == lessee.strip().lower():
		raise ToolError(
			f"lessor and lessee are both {lessor!r}. A party cannot lease from itself. Nothing was created."
		)
	if expiration_date and expiration_date < effective_date:
		raise ToolError(
			f"expiration_date {expiration_date} is before effective_date {effective_date}. "
			"Nothing was created."
		)

	termination_date = as_date(args, "termination_date")
	termination_reason = as_str(args, "termination_reason")
	if status == "Terminated" and not termination_date:
		raise ToolError(
			"a Terminated lease needs a termination_date. 'We ended it' without 'when' is not a "
			"record anybody can rely on later. Nothing was created."
		)
	if termination_date and termination_date < effective_date:
		raise ToolError(
			f"termination_date {termination_date} is before effective_date {effective_date}. "
			"Nothing was created."
		)

	parcel = as_str(args, "parcel")
	parcel_name = ""
	if parcel:
		_require(PARCEL)
		entry = parcel_row(parcel, company)
		parcel = entry["name"]
		parcel_name = entry.get("parcel_name") or ""

	counterparty = as_str(args, "counterparty")
	if counterparty:
		_require(RELATED_PARTY)
		counterparty = _check_link(RELATED_PARTY, counterparty, company, "company", "counterparty")

	governance_document = as_str(args, "governance_document")
	if governance_document:
		governance_document = _check_link(
			GOVERNANCE_DOCUMENT, governance_document, company, "company", "governance_document"
		)

	rent_amount = as_float(args.get("rent_amount"), "rent_amount")
	if rent_amount < 0:
		raise ToolError(
			f"rent_amount must be zero or more, got {rent_amount}. Rent flowing the other way is "
			"a lease in the other direction, not a negative one. Nothing was created."
		)
	rent_frequency = as_str(args, "rent_frequency") or "Annual"
	rent_frequency = as_choice(LEASE, "rent_frequency", rent_frequency, "rent_frequency")

	file_url, content, file_name = _attachment_argument(args, "lease_document_url", "the executed lease")

	doc = frappe.new_doc(LEASE)
	doc.lease_name = lease_name
	doc.owning_entity = company
	doc.direction = direction
	doc.status = status
	doc.lessor = lessor
	doc.lessee = lessee
	doc.effective_date = effective_date
	doc.rent_amount = rent_amount
	doc.rent_frequency = rent_frequency
	for field, value in (
		("parcel", parcel),
		("counterparty", counterparty),
		("governance_document", governance_document),
		("expiration_date", expiration_date),
		("termination_date", termination_date),
		("termination_reason", termination_reason),
		("rent_terms", as_str(args, "rent_terms")),
		("notes", as_str(args, "notes")),
		("lease_document", file_url),
	):
		if value:
			doc.set(field, value)
	doc.insert()

	attachment = _attach(LEASE, doc.name, "lease_document", file_name, content) if content else None

	row = frappe.db.get_value(LEASE, doc.name, compat.existing_fields(LEASE, _LEASE_FIELDS), as_dict=True)
	data = _describe_lease(dict(row), frappe.utils.today())
	data["parcel_name"] = parcel_name or None
	data["direction_check"] = _direction_check(company, direction, lessor, lessee)
	data["attachment"] = (
		{
			"name": attachment.name,
			"file_name": attachment.get("file_name"),
			"file_size": len(content),
			"is_private": True,
		}
		if attachment
		else None
	)
	data["note"] = (
		"Nothing was posted. This records the agreement; the rent it describes reaches the "
		"ledger through whatever books it — a Journal Entry, an invoice, a bank transaction — "
		"and this tool has no part in that."
	)
	data["next_step"] = (
		"File the executed lease in the archive with attach_governance_document and point "
		"governance_document at it."
		if not governance_document
		else "Record the parcel this covers with create_parcel and set `parcel`."
		if not parcel
		else "The register entry is complete."
	)
	if data["direction_check"]["verdict"] == "inconsistent":
		data["warning"] = data["direction_check"]["explanation"]

	rent_note = f" at {rent_amount:,.2f} {rent_frequency.lower()}" if rent_amount else ""
	return ToolResult(
		data,
		f"recorded {direction.lower()} lease {lease_name} for {company} as {doc.name}"
		f" ({lessor} → {lessee}){rent_note}",
		docstatus_delta="none → 0 (created)",
	)


def _direction_check(company: str, direction: str, lessor: str, lessee: str) -> dict:
	"""Does the stated direction agree with the party names, as far as we can tell?

	Reported, never enforced. The owning entity's Company docname is "Highland
	LLC" and the lease says "Highland Ltd Liability Co."; there is no comparison
	that gets that right reliably, so a mismatch is an observation for a human
	and a match is a small reassurance. Refusing on it would be refusing on a
	string.
	"""
	ours = lessor if direction == "Outbound" else lessee
	theirs = lessee if direction == "Outbound" else lessor
	abbr = str(frappe.db.get_value("Company", company, "abbr") or "")
	needles = {company.lower(), abbr.lower()} - {""}
	haystack = ours.lower()
	if any(needle and needle in haystack for needle in needles):
		return {
			"verdict": "consistent",
			"our_side": ours,
			"their_side": theirs,
			"explanation": f"{direction} names {ours!r} on our side, which matches {company}.",
		}
	if any(needle and needle in theirs.lower() for needle in needles):
		return {
			"verdict": "inconsistent",
			"our_side": ours,
			"their_side": theirs,
			"explanation": (
				f"direction is {direction}, which puts {ours!r} on our side — but it is "
				f"{theirs!r} that resembles {company}. Check whether this lease runs the other "
				"way. Nothing was refused: a legal name and a Company docname often differ."
			),
		}
	return {
		"verdict": "unverified",
		"our_side": ours,
		"their_side": theirs,
		"explanation": (
			f"neither party name resembles {company}, so the stated direction could not be "
			"checked against them. That is normal when the legal name differs from the Company "
			"record's name."
		),
	}


# ── 83. update_lease ────────────────────────────────────────────────────────
def update_lease(args: dict) -> ToolResult:
	"""Change a recorded lease: status, term, rent, counterparty, notes."""
	_require(LEASE)
	requested = as_str(args, "lease", required=True)
	requested_entity = _entity(args, required=False) or ""
	entry = lease_row(requested, "" if frappe.db.exists(LEASE, requested) else requested_entity)
	company = entry["owning_entity"]

	if "lease_name" in args or "new_lease_name" in args:
		raise ToolError(
			"the lease_name cannot be changed. The docname is built from it. A lease that has "
			"been renewed is a new lease with its own term — create it and mark this one "
			"Expired. Nothing was changed."
		)
	if requested_entity and requested_entity != company:
		raise ToolError(
			f"lease {entry['name']} belongs to {company!r} and update_lease cannot move it to "
			f"{requested_entity!r}. Nothing was changed."
		)

	changes: dict = {}
	updates: dict = {}

	status = as_str(args, "status")
	if status:
		status = as_choice(LEASE, "status", status, "status")
		if status != entry.get("status"):
			updates["status"] = status
			changes["status"] = [entry.get("status"), status]

	termination_date = as_date(args, "termination_date")
	if termination_date and termination_date != str(entry.get("termination_date") or ""):
		updates["termination_date"] = termination_date
		changes["termination_date"] = [str(entry.get("termination_date") or ""), termination_date]

	effective = str(entry.get("effective_date") or "")
	final_status = updates.get("status", entry.get("status"))
	final_termination = updates.get("termination_date", str(entry.get("termination_date") or ""))
	if final_status == "Terminated" and not final_termination:
		raise ToolError(
			"marking a lease Terminated needs a termination_date in the same call. 'We ended it' "
			"without 'when' is not a record anybody can rely on later. Nothing was changed."
		)
	if final_termination and effective and final_termination < effective:
		raise ToolError(
			f"termination_date {final_termination} is before this lease's effective date "
			f"{effective}. Nothing was changed."
		)

	expiration_date = as_date(args, "expiration_date")
	if expiration_date and expiration_date != str(entry.get("expiration_date") or ""):
		if effective and expiration_date < effective:
			raise ToolError(
				f"expiration_date {expiration_date} is before this lease's effective date "
				f"{effective}. Nothing was changed."
			)
		updates["expiration_date"] = expiration_date
		changes["expiration_date"] = [str(entry.get("expiration_date") or ""), expiration_date]

	if args.get("rent_amount") not in (None, ""):
		rent_amount = as_float(args.get("rent_amount"), "rent_amount")
		if rent_amount < 0:
			raise ToolError(f"rent_amount must be zero or more, got {rent_amount}. Nothing was changed.")
		if abs(rent_amount - float(entry.get("rent_amount") or 0)) > 1e-9:
			updates["rent_amount"] = rent_amount
			changes["rent_amount"] = [_money(entry.get("rent_amount")), _money(rent_amount)]

	if "rent_frequency" in args:
		requested = as_str(args, "rent_frequency")
		frequency = as_choice(LEASE, "rent_frequency", requested, "rent_frequency") if requested else ""
		if frequency and frequency != entry.get("rent_frequency"):
			updates["rent_frequency"] = frequency
			changes["rent_frequency"] = [entry.get("rent_frequency"), frequency]

	for field in ("lessor", "lessee", "rent_terms", "termination_reason", "notes"):
		if field in args:
			value = as_str(args, field)
			if value != str(entry.get(field) or ""):
				updates[field] = value or None
				changes[field] = [entry.get(field) or "", value]

	final_lessor = str(updates.get("lessor", entry.get("lessor")) or "")
	final_lessee = str(updates.get("lessee", entry.get("lessee")) or "")
	if final_lessor and final_lessor.strip().lower() == final_lessee.strip().lower():
		raise ToolError(
			f"that would make lessor and lessee both {final_lessor!r}. A party cannot lease from "
			"itself. Nothing was changed."
		)

	for field, doctype in (("counterparty", RELATED_PARTY), ("governance_document", GOVERNANCE_DOCUMENT)):
		if field in args:
			requested = as_str(args, field)
			if requested:
				_require(doctype)
				requested = _check_link(doctype, requested, company, "company", field)
			if requested != str(entry.get(field) or ""):
				updates[field] = requested or None
				changes[field] = [entry.get(field) or "", requested]

	if "parcel" in args:
		requested = as_str(args, "parcel")
		if requested:
			_require(PARCEL)
			requested = parcel_row(requested, company)["name"]
		if requested != str(entry.get("parcel") or ""):
			updates["parcel"] = requested or None
			changes["parcel"] = [entry.get("parcel") or "", requested]

	if not changes:
		raise ToolError(
			f"nothing to change on {entry['name']}. Pass at least one of status, "
			"expiration_date, termination_date, termination_reason, rent_amount, "
			"rent_frequency, rent_terms, lessor, lessee, parcel, counterparty, "
			"governance_document or notes, with a value that differs from the current one."
		)

	doc = frappe.get_doc(LEASE, entry["name"])
	for field, value in updates.items():
		doc.set(field, value)
	doc.save()

	after = frappe.db.get_value(
		LEASE, entry["name"], compat.existing_fields(LEASE, _LEASE_FIELDS), as_dict=True
	)
	data = _describe_lease(dict(after), frappe.utils.today())
	data["changes"] = changes
	data["note"] = (
		"Nothing was posted. A rent change here does not restate anything already booked; the "
		"ledger records what was actually paid, and this records what was agreed."
	)
	summary = ", ".join(f"{field} {before!r} → {value!r}" for field, (before, value) in changes.items())
	return ToolResult(data, f"updated Lease {entry['name']}: {summary}", docstatus_delta="")


# ── 84. list_leases ─────────────────────────────────────────────────────────
def list_leases(args: dict) -> ToolResult:
	"""One entity's leases, both directions, with the rent roll and what is running out."""
	_require(LEASE)
	company = _entity(args)
	limit = min(as_limit(args), REGISTER_CAP)
	today = frappe.utils.today()

	filters = {"owning_entity": company}
	status = as_str(args, "status")
	if status:
		filters["status"] = as_choice(LEASE, "status", status, "status")
	direction = as_str(args, "direction")
	if direction:
		filters["direction"] = as_choice(LEASE, "direction", direction, "direction")
	parcel = as_str(args, "parcel")
	if parcel:
		_require(PARCEL)
		filters["parcel"] = parcel_row(parcel, company)["name"]
	counterparty = as_str(args, "counterparty")
	if counterparty:
		filters["counterparty"] = counterparty

	fields = compat.existing_fields(LEASE, _LEASE_FIELDS)
	rows = frappe.db.get_all(
		LEASE, filters=filters, fields=fields, order_by="effective_date desc", limit=limit
	)
	leases = [_describe_lease(dict(row), today) for row in rows]

	active_on = as_date(args, "active_on")
	if active_on:
		leases = [lease for lease in leases if _covers(lease, active_on)]

	window = as_int(args, "expiring_within_days", DEFAULT_EXPIRING_DAYS)
	if window is None or window < 0:
		raise ToolError(
			f"expiring_within_days must be zero or more days, got {args.get('expiring_within_days')!r}"
		)
	horizon = frappe.utils.add_days(frappe.utils.getdate(today), window).isoformat()

	rent_roll = {"Outbound": 0.0, "Inbound": 0.0}
	unannualised = []
	for lease in leases:
		if lease["status"] != "Active":
			continue
		if lease["annualised_rent"] is None:
			if lease["rent_amount"] or lease["rent_frequency"] in ("Crop Share", "One-Time"):
				unannualised.append({"lease": lease["name"], "rent_frequency": lease["rent_frequency"]})
			continue
		rent_roll[lease["direction"]] = round(
			rent_roll.get(lease["direction"], 0.0) + lease["annualised_rent"], 2
		)

	expiring = [
		{
			"name": lease["name"],
			"expiration_date": lease["expiration_date"],
			"direction": lease["direction"],
			"counterparty_name": lease["lessee"] if lease["direction"] == "Outbound" else lease["lessor"],
		}
		for lease in leases
		if lease["status"] == "Active"
		and lease["expiration_date"]
		and today <= lease["expiration_date"] <= horizon
	]
	past_expiration = [lease["name"] for lease in leases if lease["past_expiration"]]

	total_count = frappe.db.count(LEASE, {"owning_entity": company})
	data = {
		"owning_entity": company,
		"leases": leases,
		"count": len(leases),
		"total_in_register": total_count,
		"as_of": today,
		"active_count": len([lease for lease in leases if lease["status"] == "Active"]),
		"annual_rent_receivable": rent_roll["Outbound"],
		"annual_rent_payable": rent_roll["Inbound"],
		"net_annual_rent": round(rent_roll["Outbound"] - rent_roll["Inbound"], 2),
		"rent_not_annualisable": unannualised,
		"expiring_within_days": window,
		"expiring_soon": expiring,
		"active_past_expiration": past_expiration,
		"note": (
			"Rent is annualised from rent_amount and rent_frequency for ACTIVE leases only. A "
			"crop share and a one-time payment have no annual rate and are listed under "
			"rent_not_annualisable rather than counted as zero — a rent roll that quietly "
			"treated an unknown as nothing would understate the whole portfolio."
		),
	}
	if past_expiration:
		data["warning"] = (
			f"{len(past_expiration)} lease(s) are marked Active with an expiration date already "
			f"past: {', '.join(past_expiration[:5])}. That is legitimate — farm ground routinely "
			"runs on month to month past its stated term — and NOTHING HERE CHANGED IT. A status "
			"that flipped itself on a date would erase the difference between 'still running' "
			"and 'nobody has looked at this in years'. Settle it with update_lease."
		)
	elif len(leases) < total_count and not (status or direction or parcel or counterparty or active_on):
		data["warning"] = (
			f"{total_count} leases are recorded for {company} but the limit returned "
			f"{len(leases)}. Raise `limit` before relying on the rent roll."
		)
	return ToolResult(
		data,
		f"{len(leases)} lease(s) for {company}: {data['annual_rent_receivable']:,.2f} receivable "
		f"and {data['annual_rent_payable']:,.2f} payable a year, {len(expiring)} expiring within "
		f"{window} days",
	)


def _covers(lease: dict, when: str) -> bool:
	"""Was this lease in force on `when`, by the dates on the record?"""
	if lease["effective_date"] and when < lease["effective_date"]:
		return False
	end = lease["termination_date"] or lease["expiration_date"]
	if end and when > end:
		return False
	return True


# ── 85. get_lease ───────────────────────────────────────────────────────────
def get_lease(args: dict) -> ToolResult:
	"""One lease in full, with the parcel it covers and its attachments."""
	_require(LEASE)
	company = _entity(args, required=False) or ""
	entry = lease_row(as_str(args, "lease", required=True), company)
	today = frappe.utils.today()
	data = _describe_lease(entry, today)

	data["parcel_detail"] = None
	if entry.get("parcel") and compat.doctype_exists(PARCEL) and frappe.db.exists(PARCEL, entry["parcel"]):
		row = frappe.db.get_value(
			PARCEL, entry["parcel"], compat.existing_fields(PARCEL, _PARCEL_FIELDS), as_dict=True
		)
		data["parcel_detail"] = _describe_parcel(dict(row))

	attachments = frappe.db.get_all(
		"File",
		filters={"attached_to_doctype": LEASE, "attached_to_name": entry["name"]},
		fields=compat.existing_fields("File", ("name", "file_name", "file_url", "file_size", "is_private")),
		order_by="creation asc",
		limit=50,
	)
	data["attachments"] = [dict(attachment) for attachment in attachments]
	data["as_of"] = today
	data["in_force_today"] = data["status"] == "Active" and _covers(data, today)
	data["note"] = (
		"Read `direction` before reading `rent_amount`: Outbound means the owning entity "
		"collects it, Inbound means the owning entity pays it. Attachment bytes come back "
		"through get_attachment_content, which checks read permission on this lease first."
	)
	return ToolResult(
		data,
		f"lease {entry['name']}: {data['direction']} {data['status']}, "
		f"{data['lessor']} → {data['lessee']}"
		+ (f", expires {data['expiration_date']}" if data["expiration_date"] else ""),
	)


# ── 86. link_parcel_to_asset ────────────────────────────────────────────────
def link_parcel_to_asset(args: dict) -> ToolResult:
	"""Tie a parcel to the Fixed Asset that carries it, and report the gap between them.

	The gap is the point. A parcel appraised at 3,100,000 sitting on the books at
	a 1998 cost of 240,000 is not a discrepancy to be fixed — it is unrealised
	appreciation, it is the single most important number in an estate
	conversation, and neither record alone shows it. Linking them is what makes it
	visible; nothing here posts it, because unrealised appreciation is not a
	journal entry.
	"""
	_require(PARCEL)
	company = _entity(args, required=False) or ""
	entry = parcel_row(as_str(args, "parcel", required=True), company)
	company = entry["owning_entity"]
	asset_name = as_str(args, "asset", required=True)
	replace = as_bool(args, "replace", False)

	current = str(entry.get("related_asset") or "")
	if current and current == asset_name:
		raise ToolError(f"{entry['name']} is already linked to Asset {current}. Nothing was changed.")
	if current and not replace:
		raise ToolError(
			f"{entry['name']} is already linked to Asset {current}. Pass replace=true to point "
			"it at a different asset instead — which is right after a re-capitalisation and "
			"wrong if the two assets are really two parts of the parcel. Nothing was changed."
		)

	asset = _asset_for_parcel(asset_name, company, exclude_parcel=entry["name"])

	if as_bool(args, "dry_run", False):
		return ToolResult(
			{
				"dry_run": True,
				"parcel": entry["name"],
				"asset": asset_name,
				"currently_linked_to": current or None,
				"asset_summary": _asset_summary(asset, _money(entry.get("appraised_value"))),
				"note": "Nothing was written. Call again without dry_run to make the link.",
			},
			f"dry run: would link parcel {entry['name']} to Asset {asset_name}",
		)

	frappe.db.set_value(PARCEL, entry["name"], "related_asset", asset_name)

	summary = _asset_summary(asset, _money(entry.get("appraised_value")))
	data = {
		**_describe_parcel(
			dict(
				frappe.db.get_value(
					PARCEL, entry["name"], compat.existing_fields(PARCEL, _PARCEL_FIELDS), as_dict=True
				)
			)
		),
		"asset": summary,
		"replaced": current or None,
		"note": (
			"The link is a pointer, not a posting. No balance moved, no depreciation schedule "
			"changed, and the asset's cost is exactly what it was."
		),
		"next_step": (
			"Depreciation on any improvements sitting on this ground runs through the asset, not "
			"the parcel — land itself is not depreciated. run_depreciation_cycle is where that "
			"happens."
		),
	}
	if summary["appraisal_over_book"] and summary["gross_purchase_amount"]:
		data["unrealised_appreciation"] = summary["appraisal_over_book"]
	if not _money(entry.get("appraised_value")):
		data["warning"] = (
			"This parcel has no appraised value, so the gap between cost and market could not be "
			"reported. Set appraised_value and appraised_as_of with update_parcel."
		)
	return ToolResult(
		data,
		f"linked parcel {entry['name']} to Asset {asset_name} "
		f"(cost {summary['gross_purchase_amount']:,.2f}, appraised {summary['appraised_value']:,.2f})",
		docstatus_delta="",
	)


# ── 319. set_parcel_boundary ────────────────────────────────────────────────
def set_parcel_boundary(args: dict) -> ToolResult:
	"""Give a parcel its shape, and say which of the things on it now fall outside it.

	v0.32.0, and it closes a gap `set_field_boundary` has been apologising for
	since v0.12.0: that tool warned, on every call, that a parcel had no boundary
	so nothing had checked the block sat inside its parcel. It does now.

	CONTAINMENT IS REPORTED AND NEVER ENFORCED, which is the same posture
	`set_zone_boundary` takes towards its field and is more obviously right here.
	A parcel traced off an assessor's map and a block traced off a drone flight
	are two independent measurements of adjacent ground; where they disagree by a
	metre along a shared line, the answer is that somebody should look, not that
	the parcel cannot be recorded. And a block genuinely does straddle two parcels
	on plenty of farms — the deed line runs down the middle of a planting because
	the planting predates the split.
	"""
	_require(PARCEL)
	geo.require()
	company = _entity(args, required=False) or ""
	entry = parcel_row(as_str(args, "parcel", required=True), company)

	geometry = geo.parse(args.get("boundary_geojson"))
	derived = geo.derive(geometry)
	shape = derived.pop("shape")

	ratio, verdict = geo.area_disagreement(entry.get("acreage"), derived["area_computed_acres"])
	entered = round(float(entry.get("acreage") or 0), 2)
	if verdict == "refuse":
		raise ToolError(
			f"the boundary encloses {derived['area_computed_acres']} acres and {entry['name']} is "
			f"recorded as {entered} — a difference of {round(ratio * 100, 1)}%. That is not a survey "
			"disagreement, it is one of the two figures being about a different piece of ground. A "
			"parcel is the unit the assessor, the deed and the tax bill all agree on, so the acreage "
			"here is usually the one worth trusting — check the polygon, or fix the acreage with "
			"update_parcel. Nothing was changed."
		)

	warnings = list(geo.check_coordinates_look_like_degrees(geometry, "boundary_geojson"))
	if verdict == "warn":
		warnings.append(
			f"The boundary encloses {derived['area_computed_acres']} acres against a recorded "
			f"{entered} — {round(ratio * 100, 1)}%. A deed, an assessor's map and a GIS trace "
			"routinely disagree by a few percent; both figures are kept and neither is overwritten."
		)
	if not entered:
		warnings.append(
			f"No acreage was recorded on this parcel, so nothing was compared. The polygon says "
			f"{derived['area_computed_acres']} acres — set it with update_parcel if that is right."
		)

	outside = _things_outside(entry["name"], shape)
	for doctype, names in sorted(outside.items()):
		warnings.append(
			f"{len(names)} {doctype}(s) on this parcel fall outside its boundary: "
			f"{', '.join(names)}. That is allowed — a block really can straddle a deed line, and a "
			"cabin on the far side of a road easement is a real cabin — but if it was not "
			"deliberate, one of the two shapes is wrong."
		)

	dry_run = as_bool(args, "dry_run", False)
	data = {
		"parcel": entry["name"],
		"owning_entity": entry.get("owning_entity"),
		"acreage_recorded": entered or None,
		"area_computed_acres": derived["area_computed_acres"],
		"area_disagreement_ratio": ratio or None,
		"boundary_centroid": {
			"lat": derived["boundary_centroid_lat"],
			"lon": derived["boundary_centroid_lon"],
		},
		"boundary_bbox_geojson": derived["boundary_bbox_geojson"],
		"h3_cell_counts": {
			resolution: len(cells) for resolution, cells in sorted(json.loads(derived["h3_cells"]).items())
		},
		"h3_resolutions": list(geo.H3_RESOLUTIONS),
		"outside_boundary": {doctype: names for doctype, names in sorted(outside.items())},
		"warnings": warnings,
		"dry_run": bool(dry_run),
		"changed": False,
	}
	if dry_run:
		return ToolResult(
			data,
			f"dry run: would set a {derived['area_computed_acres']}-acre boundary on {entry['name']}",
		)

	doc = frappe.get_doc(PARCEL, entry["name"])
	for fieldname, value in derived.items():
		doc.set(fieldname, value)
	doc.save(ignore_permissions=True)

	data["changed"] = True
	return ToolResult(
		data,
		f"{entry['name']}: boundary set, {derived['area_computed_acres']} acres, "
		f"centroid {derived['boundary_centroid_lat']},{derived['boundary_centroid_lon']}",
		docstatus_delta="0 → 0 (updated)",
	)


#: What sits on a parcel and can be checked against its outline, and how each one
#: says where it is. A polygon is compared with `covers`; a point with
#: `covers_point`. Housing Unit is in the point list from v0.32.0, which is the
#: release that gave it coordinates at all.
_CONTAINED_POLYGONS = (("Field", "parcel"), ("Irrigation Zone", "parcel"))
_CONTAINED_POINTS = (("Housing Unit", "parcel", "gps_latitude", "gps_longitude"),)


def _things_outside(parcel: str, shape) -> dict:
	"""Everything registered on this parcel whose own position is not inside it.

	Only records that HAVE a position are tested. A block with no boundary and a
	cabin with no coordinates are not outside the parcel — they are unmapped, and
	reporting them as violations would fill the result with fifty names that mean
	nothing and bury the two that do.
	"""
	out: dict = {}
	for doctype, link_field in _CONTAINED_POLYGONS:
		if not compat.doctype_exists(doctype) or not compat.has_field(doctype, "boundary_geojson"):
			continue
		names = []
		for row in (
			frappe.db.get_all(
				doctype,
				filters={link_field: parcel, "boundary_geojson": ("is", "set")},
				fields=["name", "boundary_geojson"],
				limit=REGISTER_CAP,
			)
			or []
		):
			other = geo.stored_shape(row.get("boundary_geojson"))
			if other is not None and not geo.covers_shape(shape, other):
				names.append(str(row["name"]))
		if names:
			out[doctype] = sorted(names)

	for doctype, link_field, lat_field, lon_field in _CONTAINED_POINTS:
		if not compat.doctype_exists(doctype) or not compat.has_field(doctype, lat_field):
			continue
		names = []
		for row in (
			frappe.db.get_all(
				doctype,
				filters={link_field: parcel},
				fields=["name", lat_field, lon_field],
				limit=REGISTER_CAP,
			)
			or []
		):
			latitude, longitude = row.get(lat_field), row.get(lon_field)
			if not latitude or not longitude:
				continue
			if not geo.covers_point(shape, latitude, longitude):
				names.append(str(row["name"]))
		if names:
			out[doctype] = sorted(names)
	return out


# ── 127. convey_parcel ──────────────────────────────────────────────────────
def convey_parcel(args: dict) -> ToolResult:
	"""Move one parcel onto another entity's books, with everything that hangs off it.

	THE SITUATION THIS IS FOR. Eight parcels get registered under the only company
	that existed when somebody started typing. Later the other four entities are
	set up, and the ground belongs on its actual title holder's books. That is not
	an edit — `update_parcel` refuses it, correctly — it is a conveyance, and a
	conveyance has a date, an instrument behind it, and a trail.

	IT DELETES AND RECREATES, WHICH IS THE HONEST SHAPE. A Parcel's docname
	encodes its entity: `"Mill Creek - OML"` on one set of books and
	`"Mill Creek - HLD"` on the other, the same way every Account docname carries
	a company abbreviation. There is no field to change that makes that true — the
	record on the old books has to stop existing and the record on the new ones has
	to start. So: create the new one, repoint everything at it, move the
	attachments, delete the old one, write the event.

	THE PARCEL'S OWN KEY IS PRESERVED, AND THAT IS WHY THE FARM REGISTERS SURVIVE.
	Every Field, Irrigation Zone and Housing Unit is named
	`"<its name> - <parcel abbr>"` — the PARCEL's short key, not the company's. The
	conveyance carries that key across unchanged, so all 29 of Mill Creek's cabins
	keep the docnames they have always had and only their `parcel` link moves. A
	target entity that already uses the same key is refused rather than
	disambiguated: a silently changed key would file this parcel's future blocks
	under a different suffix from its existing ones.

	IT WRITES NO JOURNAL ENTRY, DELIBERATELY. Recording that ground changed hands
	and booking what that costs are separate acts. Basis transfer between two sets
	of books, and any gain or loss the transfer recognises, are entries with real
	tax consequences that somebody should write on purpose with a narrative of
	their own — not produce as a side effect of filing a deed. The result names
	the entries that are still owed.

	WHAT IT REFUSES, AND WHY EACH ONE IS A DIFFERENT DOCUMENT'S JOB:

	  * **An active lease over the ground.** Conveying out from under a live lease
	    is a legal event of its own: the lease is either novated to the new owner
	    or terminated, and either way there is a document. Refused with the
	    offending leases named. A lease with no expiration date counts as running.
	  * **A linked Fixed Asset.** The asset is the balance-sheet side, it belongs
	    to the old entity's books, and moving it is a posting rather than a
	    filing. Unlink it or move it first.
	  * **A half-built target company** — no chart of accounts, or no cost centers.
	    A parcel filed against an entity that cannot carry a cost is a parcel that
	    will be found again in six months.
	  * **A name, an assessor id or an abbreviation the target already uses.**
	  * **More referring records than one call should move silently.** No silent
	    caps: if the register is bigger than the ceiling, it says so and moves
	    nothing.

	EVERY REFUSAL IS REPORTED AT ONCE. A conveyance that failed on the lease, was
	fixed, and then failed on the asset is two round trips to learn two facts that
	were both true from the start. `dry_run=true` returns the whole plan and the
	whole list without touching anything.

	ATOMIC BY CONSTRUCTION. `registry.dispatch` rolls the transaction back before
	it logs, so a conveyance that dies half way — on a link nobody expected, on a
	permission — leaves neither parcel changed rather than leaving two.
	"""
	_require(PARCEL)
	requested = as_str(args, "parcel", required=True)
	scope = _entity(args, required=False) or ""
	source = parcel_row(requested, "" if frappe.db.exists(PARCEL, requested) else scope)
	from_company = source["owning_entity"]
	source_name = source["name"]

	target_company = resolve_company(as_str(args, "target_company", required=True), required=True)
	effective_date = as_date(args, "effective_date", required=True)
	reason = as_str(args, "reason", required=True)
	dry_run = bool(as_bool(args, "dry_run", False))

	if len(reason) < MIN_REASON:
		raise ToolError(
			"reason must be a real narrative of the conveyance — why the ground moved and what "
			"authorises it: the deed, the assignment, the trust amendment. It is the part of this "
			"record nobody can reconstruct later, and it is the first thing an estate attorney "
			"asks. Nothing was changed."
		)
	if target_company == from_company:
		raise ToolError(
			f"parcel {source_name} already belongs to {target_company!r}. A conveyance to the "
			"entity that already holds it is not a conveyance. Nothing was changed."
		)

	new_title_holder = as_str(args, "new_title_holder")
	if new_title_holder:
		_require(RELATED_PARTY)
		new_title_holder = _check_link(
			RELATED_PARTY, new_title_holder, target_company, "company", "new_title_holder"
		)

	target_abbr = str(frappe.db.get_value("Company", target_company, "abbr") or "")
	new_name = f"{source['parcel_name']} - {target_abbr}" if target_abbr else source["parcel_name"]

	title = _conveyed_title_holder(source, target_company, new_title_holder)
	appraisal = _conveyed_appraisal_document(source, target_company)
	referrers = _referring_records(source_name)
	attachments = _parcel_attachments(source_name)
	refusals = _conveyance_refusals(source, target_company, effective_date, referrers)

	plan = {
		"conveyed": False,
		"from": source_name,
		"to": new_name,
		"from_entity": from_company,
		"to_entity": target_company,
		"effective_date": effective_date,
		"reason": reason,
		"parcel_abbr": source.get("abbr") or None,
		"migrated_attachments": len(attachments),
		"migrated_leases": len(referrers.get(LEASE, [])),
		"migrated_housing_units": len(referrers.get("Housing Unit", [])),
		"migrated_fields": len(referrers.get("Field", [])),
		"migrated_irrigation_zones": len(referrers.get("Irrigation Zone", [])),
		"migrated_housing_assignments": len(referrers.get("Housing Assignment", [])),
		"relinked_records": sum(len(names) for names in referrers.values()),
		"relink_detail": _relink_detail(referrers),
		"title_holder": title["value"],
		"title_holder_status": title["status"],
		"appraisal_document": appraisal["value"],
		"appraisal_document_status": appraisal["status"],
		"refusals": refusals,
		"warnings": [entry for entry in (title["warning"], appraisal["warning"]) if entry],
	}

	if refusals:
		if dry_run:
			return ToolResult(
				{
					**plan,
					"dry_run": True,
					"note": (
						"Nothing was written, and nothing would be: the refusals above have to be "
						"settled first. Each one is a different document's job — see the tool "
						"description for which."
					),
				},
				f"dry run: parcel {source_name} CANNOT be conveyed to {target_company} — "
				f"{len(refusals)} refusal(s)",
			)
		raise ToolError(
			f"parcel {source_name} cannot be conveyed to {target_company}:\n  - "
			+ "\n  - ".join(refusals)
			+ "\nNothing was changed. Every refusal is listed rather than only the first, so "
			"they can be settled in one pass."
		)

	if dry_run:
		return ToolResult(
			{
				**plan,
				"dry_run": True,
				"note": (
					f"Nothing was written. Call again without dry_run to convey. It would delete "
					f"{source_name}, create {new_name}, repoint "
					f"{plan['relinked_records']} record(s) and {len(attachments)} attachment(s), "
					"and write NO journal entry."
				),
			},
			f"dry run: would convey parcel {source_name} to {target_company} as {new_name}, "
			f"moving {plan['relinked_records']} linked record(s) and {len(attachments)} attachment(s)",
		)

	# ── the conveyance itself ───────────────────────────────────────────────
	# Order matters and is not arbitrary. The new record has to exist before
	# anything can be repointed at it, and everything has to be repointed before
	# the old one can be deleted — Frappe refuses to delete a document another
	# document links to, which means a register missing from PARCEL_REFERRERS
	# fails the whole call rather than leaving a silent orphan.
	new_doc = frappe.new_doc(PARCEL)
	for field in CONVEYED_FIELDS:
		if compat.has_field(PARCEL, field) and source.get(field) not in (None, ""):
			new_doc.set(field, source.get(field))
	new_doc.owning_entity = target_company
	if title["value"]:
		new_doc.title_holder = title["value"]
	if appraisal["value"]:
		new_doc.appraisal_document = appraisal["value"]
	new_doc.insert()

	relinked = _relink_referrers(referrers, new_doc.name, target_company)
	for attachment in attachments:
		frappe.db.set_value("File", attachment, "attached_to_name", new_doc.name)

	frappe.delete_doc(PARCEL, source_name, ignore_permissions=False)

	# Guarded on both halves: a request landing mid-`bench migrate` may have the
	# Parcel field synced and the child doctype not, and a conveyance that lost
	# its own history would be the one record nobody could reconstruct.
	if compat.doctype_exists(CONVEYANCE_EVENT) and compat.has_field(PARCEL, "conveyance_events"):
		new_doc.append(
			"conveyance_events",
			{
				"event_type": "Conveyed In",
				"effective_date": effective_date,
				"from_entity": from_company,
				"from_docname": source_name,
				"to_entity": target_company,
				"reason": reason,
				"migrated_attachments": len(attachments),
				"relinked_records": relinked,
				"relink_detail": plan["relink_detail"],
				"appraisal_document_status": appraisal["status"],
				"recorded_on": frappe.utils.now(),
			},
		)
		new_doc.save()

	after = dict(
		frappe.db.get_value(
			PARCEL, new_doc.name, compat.existing_fields(PARCEL, _PARCEL_FIELDS), as_dict=True
		)
		or {}
	)
	data = {
		**plan,
		**_describe_parcel(after),
		"conveyed": True,
		"from": source_name,
		"to": new_doc.name,
		"relinked_records": relinked,
		"refusals": [],
		"journal_entry": None,
		"note": (
			"NO journal entry was written. A conveyance is a change to the land register; the "
			"basis sitting on "
			f"{from_company}'s books has not moved, and any gain or loss the transfer recognises "
			"has not been booked. Both are entries somebody should write on purpose — "
			"create_journal_entry drafts them — because debt-free transfers between related "
			"entities are exactly where an unconsidered posting becomes a tax problem."
		),
		"next_step": (
			"The appraisal report is filed under the previous entity and did NOT come across. "
			"Re-file it with attach_governance_document against " + target_company + ", then "
			"point appraisal_document at it with update_parcel."
			if appraisal["status"] == "unlinked_needs_reattach"
			else "Record the basis transfer with create_journal_entry when your accountant has "
			"decided what it is. get_parcel shows the new record with its conveyance history."
		),
	}
	if not data["warnings"]:
		data.pop("warnings")

	return ToolResult(
		data,
		f"conveyed parcel {source_name} ({from_company}) to {target_company} as {new_doc.name}, "
		f"effective {effective_date}: {relinked} linked record(s) and {len(attachments)} "
		f"attachment(s) moved, appraisal document {appraisal['status']} — {reason}",
		docstatus_delta="none → 0 (recreated on the new entity)",
	)


def _conveyance_refusals(source: dict, target_company: str, effective_date: str, referrers: dict) -> list:
	"""Every reason this conveyance cannot happen, collected rather than raised.

	Collected because a caller who fixes the lease and then learns about the asset
	has spent two round trips finding out two things that were both true from the
	start — and because `dry_run` has to be able to report them without raising at
	all.
	"""
	out = []
	source_name = source["name"]

	if source.get("related_asset"):
		out.append(
			f"{source_name} is linked to Fixed Asset {source['related_asset']}, which sits on "
			f"{source['owning_entity']}'s balance sheet. Moving an asset between entities is a "
			"posting, not a filing, and it is not this tool's business. Unlink it "
			"(link_parcel_to_asset with the asset it should point at, or clear it in the Desk) "
			"or move the asset first."
		)

	for lease in _blocking_leases(source_name, effective_date):
		out.append(
			f"Lease {lease['name']} is Active over this parcel"
			+ (f" until {lease['expiration_date']}" if lease["expiration_date"] else " with no end date")
			+ f", and the conveyance date {effective_date} falls inside its term. Conveying out "
			"from under a live lease is a legal event of its own: the lease is either novated to "
			"the new owner or terminated, and either way there is a document. Record it with "
			"update_lease, then convey."
		)

	if not compat.doctype_exists("Cost Center") or not frappe.db.count(
		"Cost Center", {"company": target_company}
	):
		out.append(
			f"{target_company} has no cost centers. A parcel filed against an entity that cannot "
			"carry a cost is one somebody finds again in six months — set the company up with "
			"create_cost_center first."
		)
	if not frappe.db.count("Account", {"company": target_company}):
		out.append(
			f"{target_company} has no chart of accounts. Import one with import_chart_of_accounts "
			"before moving ground onto its books."
		)

	name_clash = frappe.db.get_value(
		PARCEL, {"parcel_name": source["parcel_name"], "owning_entity": target_company}, "name"
	)
	if name_clash:
		out.append(
			f"{target_company} already has a parcel called {source['parcel_name']!r} "
			f"({name_clash}). One parcel per name per entity, and the two would be impossible to "
			"tell apart."
		)
	if source.get("parcel_id"):
		id_clash = frappe.db.get_value(
			PARCEL, {"parcel_id": source["parcel_id"], "owning_entity": target_company}, "name"
		)
		if id_clash:
			out.append(
				f"assessor parcel id {source['parcel_id']!r} is already on {id_clash} for "
				f"{target_company}. That number is the county's key: two parcels sharing one "
				"means a typo in one of them."
			)
	if source.get("abbr") and compat.has_field(PARCEL, "abbr"):
		abbr_clash = frappe.db.get_value(
			PARCEL, {"abbr": source["abbr"], "owning_entity": target_company}, "name"
		)
		if abbr_clash:
			out.append(
				f"abbreviation {source['abbr']!r} is already used by {abbr_clash} for "
				f"{target_company}. Every field, irrigation zone and housing unit on this parcel "
				"is filed under that key, and the conveyance keeps it rather than inventing a new "
				"one — so two parcels sharing it would file their blocks together. Change one "
				"parcel's abbreviation in the Desk first."
			)

	for doctype, names in referrers.items():
		if len(names) >= REGISTER_CAP:
			out.append(
				f"{len(names)} {doctype} record(s) point at {source_name}, which is at or above "
				f"this tool's ceiling of {REGISTER_CAP} per register. Nothing was moved rather "
				"than some of it — a half-conveyed parcel is worse than an unconveyed one."
			)
	return out


def _blocking_leases(parcel_name: str, effective_date: str) -> list:
	"""Active, unterminated leases whose term covers the conveyance date.

	`termination_date` is what says a lease actually ended, and a status of Active
	with no termination is a lease still running whatever its expiration says —
	`list_leases` explains at length why nothing here expires a lease on a
	calendar. So a lease with no expiration date BLOCKS: open-ended means still
	running, and reading a missing end date as "already over" is the one wrong
	answer that fails silently.
	"""
	if not compat.doctype_exists(LEASE):
		return []
	rows = frappe.db.get_all(
		LEASE,
		filters={"parcel": parcel_name, "status": "Active"},
		fields=compat.existing_fields(LEASE, ("name", "expiration_date", "termination_date", "lease_name")),
		order_by="name asc",
		limit=REGISTER_CAP,
	)
	out = []
	for row in rows or []:
		if str(row.get("termination_date") or ""):
			continue
		expiration = str(row.get("expiration_date") or "")
		if expiration and effective_date >= expiration:
			continue
		out.append({"name": row.get("name"), "expiration_date": expiration or None})
	return out


def _conveyed_title_holder(source: dict, target_company: str, new_title_holder: str) -> dict:
	"""Who holds title on the other side, and whether that had to be decided here.

	An explicit `new_title_holder` wins — the caller has already been made to pass
	one that belongs to the target entity. Otherwise the existing one is kept only
	if it is a related party of the target (or of nobody in particular); a title
	holder registered against the entity the ground just left is a pointer that
	would read as current and is not, so it is dropped and said so.
	"""
	if new_title_holder:
		return {
			"value": new_title_holder,
			"status": "set_from_argument",
			"warning": None,
		}
	current = str(source.get("title_holder") or "")
	if not current:
		return {"value": None, "status": "none_on_source", "warning": None}
	if not compat.doctype_exists(RELATED_PARTY) or not frappe.db.exists(RELATED_PARTY, current):
		return {
			"value": None,
			"status": "dropped_missing",
			"warning": (
				f"title_holder {current!r} is not in the related-party register any more, so it "
				"was not carried across. Set one with update_parcel."
			),
		}
	owner = str(frappe.db.get_value(RELATED_PARTY, current, "company") or "")
	if owner and owner != target_company:
		return {
			"value": None,
			"status": "dropped_wrong_company",
			"warning": (
				f"title_holder {current!r} is registered against {owner}, which is not the entity "
				f"receiving this parcel. It was NOT carried across — a title holder filed under "
				"the entity the ground just left would read as current and would be wrong. "
				"Register the holder against " + target_company + " with create_related_party "
				"and set it with update_parcel, or pass new_title_holder next time."
			),
		}
	return {"value": current, "status": "preserved", "warning": None}


def _conveyed_appraisal_document(source: dict, target_company: str) -> dict:
	"""Whether the appraisal report can follow the parcel, and what to say if not.

	A Governance Document belongs to a company, and the archive is scoped that way
	on purpose. So a report filed under the entity the ground came from cannot
	simply be pointed at from the other entity's parcel — the link would cross a
	boundary the archive exists to keep. It is dropped, flagged
	`unlinked_needs_reattach`, and named in the result and in the conveyance event,
	because "the appraisal needs re-filing" is a real piece of work and a silent
	null is how it gets forgotten.
	"""
	current = str(source.get("appraisal_document") or "")
	if not current:
		return {"value": None, "status": "n/a", "warning": None}
	if not compat.doctype_exists(GOVERNANCE_DOCUMENT) or not frappe.db.exists(GOVERNANCE_DOCUMENT, current):
		return {
			"value": None,
			"status": "unlinked_needs_reattach",
			"warning": (
				f"appraisal_document {current!r} is no longer in the archive, so nothing was carried across."
			),
		}
	owner = str(frappe.db.get_value(GOVERNANCE_DOCUMENT, current, "company") or "")
	if owner and owner != target_company:
		return {
			"value": None,
			"status": "unlinked_needs_reattach",
			"warning": (
				f"appraisal report {current!r} is filed in {owner}'s archive and did NOT come "
				f"across — a governance document belongs to a company, and pointing at it from "
				f"{target_company}'s parcel would cross the boundary the archive exists to keep. "
				"The appraised value and date DID come across; the report behind them has to be "
				"re-filed with attach_governance_document and relinked with update_parcel."
			),
		}
	return {"value": current, "status": "linked", "warning": None}


def _referring_records(parcel_name: str) -> dict:
	"""Every record in every register that points at this parcel, by doctype.

	Read once, before anything is written, so the same list drives the plan, the
	dry run, the ceiling refusal and the relink. Reading it twice would let a dry
	run promise a number the real call did not move.
	"""
	out: dict = {}
	for doctype, fieldname in PARCEL_REFERRERS:
		if not compat.doctype_exists(doctype) or not compat.has_field(doctype, fieldname):
			continue
		names = (
			frappe.db.get_all(
				doctype,
				filters={fieldname: parcel_name},
				pluck="name",
				order_by="name asc",
				limit=REGISTER_CAP,
			)
			or []
		)
		if names:
			out[doctype] = list(names)
	return out


def _relink_referrers(referrers: dict, new_name: str, target_company: str) -> int:
	"""Repoint every referring record at the new parcel, and move its entity with it.

	`frappe.db.set_value` rather than `doc.save()` on purpose. Nothing here is a
	change to what those records MEAN — a cabin is the same cabin, on the same
	ground, with the same docname — so re-running each register's own validation
	mid-conveyance would be asking a Housing Unit to re-check a parcel link while
	the old parcel still exists and the new one is not yet everything it will be.
	The field write is the whole of the change, and it is the whole of what is
	done.

	`owning_entity` moves too, but only on the registers that describe the GROUND.
	A Lease's owning entity is the party to the agreement, and a conveyance does
	not change who signed a contract — that is a novation, and it is its own
	document.
	"""
	moved = 0
	for doctype, fieldname in PARCEL_REFERRERS:
		names = referrers.get(doctype) or []
		if not names:
			continue
		company_field = REFERRER_COMPANY_FIELDS.get(doctype)
		updates = {fieldname: new_name}
		if company_field and compat.has_field(doctype, company_field):
			updates[company_field] = target_company
		for name in names:
			frappe.db.set_value(doctype, name, dict(updates))
			moved += 1
	return moved


def _parcel_attachments(parcel_name: str) -> list:
	"""File rows attached to this parcel, which follow it to the new docname.

	A File points at its parent by `attached_to_name`, which is a docname and not
	a link — so a conveyance that did not rewrite these would leave the tax
	statements and the survey attached to a string nothing resolves, with no
	error anywhere to say so. That is the failure mode this exists to prevent.
	"""
	return list(
		frappe.db.get_all(
			"File",
			filters={"attached_to_doctype": PARCEL, "attached_to_name": parcel_name},
			pluck="name",
			order_by="creation asc",
			limit=REGISTER_CAP,
		)
		or []
	)


def _relink_detail(referrers: dict) -> str:
	"""The relink counts as one readable line: "Lease: 1, Housing Unit: 29"."""
	parts = [f"{doctype}: {len(names)}" for doctype, names in sorted(referrers.items()) if names]
	return ", ".join(parts) or "nothing pointed at it"
