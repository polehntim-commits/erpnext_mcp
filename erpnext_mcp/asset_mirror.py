# SPDX-License-Identifier: MIT
"""The tag in the field and the asset on the books, made the same thing.

WHAT THIS SOLVES. A worker registers a tractor from the handset. It lands in
`Asset Register`, which is where the tag, the QR, the scan history, the service
schedule and eight other doctypes' link fields all point — and it does NOT land
in ERPNext's `Asset`, which is where the fixed-asset register, the insurance
schedule and the depreciation run all look. The machine exists twice on one site
and neither copy knows about the other, so an asset registered in the field is
invisible on the books and an asset bought through the books has no tag.

THIS IS A MIRROR AND NOT A MIGRATION, AND THE DIRECTION IS THE WHOLE DESIGN.
`Asset Register` stays the operational record: its docname IS the string printed
on the sticker, thirty-three of them are already zip-tied to valves, and Farm
Task, Compliance Alert, Accident Report, Crop Observation, Spray Application,
Spray REI and Asset State Log all carry a Link to it. Moving the store would
repoint every one of those and reprint every tag. So the register keeps the
ground truth and this module writes a SECOND record — an ordinary ERPNext Asset,
in draft, carrying a Link back to the tag — so that the same machine appears in
the place an accountant, an adjuster and a lender look for it.

WHY MOST FIELD REGISTRATIONS DO NOT MIRROR, AND WHY THAT IS CORRECT. ERPNext's
`Asset` is a submittable financial document. `Asset.validate_asset_values`
throws `MandatoryError("Gross Purchase Amount is mandatory")` on a zero, the
doctype marks `purchase_date`, `item_code` and `location` required, and
`validate_cost_center` throws unless the asset or the company names one. A valve
somebody tagged while walking a line has none of those: the thirty-three on this
site all read `purchase_value` 0.0 and `acquired_on` empty.

The tempting fix is to invent them — a dollar, or today's date, or ERPNext's
`is_composite_asset` flag, which skips the amount check because a capitalization
entry is supposed to supply the value later. Each one puts a number that nobody
measured into the fixed-asset register, and that register is what the
depreciation run, `export_insurance_schedule` and Sustainable CF/Acre are all
computed from. A fabricated cost basis does not stay in the row it was invented
in.

So the gate is: an Asset Register record mirrors when it carries a purchase
value and an acquisition date, and otherwise it does not, and the reason comes
back naming the two arguments that would fix it. A tractor registered from the
handset with its price appears on the books immediately, because the mobile
`register_asset` route has taken `purchase_value` and `acquired_on` since
v0.78.0. A valve tagged with neither stays where it is until somebody says what
it cost.

A FAILED MIRROR NEVER UNDOES A REGISTRATION. This is the same trade
`asset_tags._attach_photo` makes and for the same reason: the tag is the record,
the mirror is a second copy of it, and losing a tractor's registration because
its Asset could not be built would be the wrong way round. Every entry point
here returns a verdict — `mirrored`, `asset`, `reason` — and raises nothing at
the caller.

WHAT IS NEVER MIRRORED ONTO AN ASSET THAT ALREADY EXISTS. Money. Editing
`purchase_value` on a tag refreshes nothing on the books: `gross_purchase_amount`
is a figure the ledger has been reconciled against, and a field edit that
restated it would move a balance from a screen where nobody could see that is
what they were doing. Identity is refreshed (the name, the type, the photograph,
the stamp); value is written once, at creation, and changed in the Desk.

RETIRING A TAG DOES NOT DISPOSE OF THE ASSET, for the same reason. ERPNext
disposes of an asset through a scrap or sale journal that posts to the general
ledger. `retire_asset` writes a date on a register row. They are not the same
act and this module will not turn one into the other.

IT SHIPS OFF. `mirror_assets_to_erpnext` on ERPNext MCP Settings defaults to 0,
like every other mutating switch in this app. What it gates is this app writing
into a doctype ERPNext owns, on a trigger a field worker pulls — which is
precisely the kind of thing an operator should have to agree to by name.
"""

from __future__ import annotations

import frappe

from . import compat, settings

ASSET = "Asset"
ASSET_REGISTER = "Asset Register"

#: The Link back to the tag, and the column everything here is keyed on. Two
#: Assets carrying the same value would be two sets of books for one machine, so
#: the lookup below treats more than one match as a fault and refuses rather
#: than picking.
LINK_FIELD = "asset_register"

#: The farm's own vocabulary, denormalised onto the Asset so the Desk's Asset
#: list can be filtered by it without a join. Read-only: it is written here and
#: nowhere else, and a column somebody could type over would make "every valve"
#: mean whatever the last person entered.
TYPE_FIELD = "farm_asset_type"

#: When the mirror last ran. Its whole job is to make drift visible — an Asset
#: whose stamp is months behind its tag's `modified` is one this app stopped
#: being able to update, and without the column that is invisible.
SYNCED_FIELD = "asset_register_synced_at"

#: Where the machine stands, copied onto the Asset and KEPT CURRENT — this is
#: the one pair that is deliberately a second copy of a column the register
#: already has, and the reason is that the unified map plots equipment from the
#: fixed-asset register alongside blocks, zones and valves. A map that had to
#: join through `Asset Register` to find a tractor would be a map that cannot
#: plot an asset somebody created in the Desk.
#:
#: KEPT CURRENT IS THE WHOLE OF IT. A coordinate copied once and never refreshed
#: is worse than no coordinate: it sends somebody to where the tractor was in
#: March. `_refresh` rewrites both on every sync, and `asset_register_synced_at`
#: is what says when that last happened.
GPS_FIELDS = ("gps_latitude", "gps_longitude")

#: All of them, in the order the Desk shows them. `compliance_fields.py` is what
#: actually creates them and this is what it is checked against: a column added
#: to one and not the other is a mirror writing into nothing, or a Desk column
#: nobody fills in, and neither announces itself.
CUSTOM_FIELDS = (LINK_FIELD, TYPE_FIELD, SYNCED_FIELD, *GPS_FIELDS)

#: Which Asset Category an `Asset Register.asset_type` belongs in. These are the
#: six the unified asset register is built on; a site creates them with
#: `create_asset_category`, which is where the fixed-asset, accumulated-
#: depreciation and depreciation-expense accounts get named.
#:
#: `Block` IS DELIBERATELY ABSENT rather than mapped to something. A block is
#: planted ground: its establishment cost is capitalised against the planting,
#: not against a machinery category, and filing one under "Machinery &
#: Equipment" because the table needed an answer would put orchard ground into
#: an equipment depreciation schedule. An unmapped type mirrors with no
#: category, which ERPNext allows — `asset_category` is `reqd: 0` on the Asset
#: and is only consulted when `calculate_depreciation` is on, which here it
#: never is.
CATEGORY_BY_TYPE = {
	"Irrigation Valve": "Irrigation Valve",
	"Irrigation Zone": "Irrigation Valve",
	"Tractor": "Tractor",
	"Wind Machine": "Wind Machine",
	"Housing Unit": "Housing Unit",
	"Storage": "Structure",
	"Cold Storage": "Structure",
	"Water Source": "Structure",
	"Sprayer": "Machinery & Equipment",
	"Implement": "Machinery & Equipment",
	"Vehicle": "Machinery & Equipment",
	"General": "Machinery & Equipment",
}

#: Prefix for the fixed-asset Item every Asset has to hang off. One per asset
#: type rather than one per machine: ERPNext wants an Item to say what KIND of
#: thing this is, and forty valves are forty assets of one kind.
ITEM_PREFIX = "FARM-ASSET-"


def enabled() -> bool:
	"""Whether this app may write into ERPNext's Asset register at all."""
	return settings.asset_mirror_enabled()


def available() -> str:
	"""Empty when a mirror could be written, or the sentence saying why not.

	Checked in this order on purpose: an operator who has not turned the switch
	on should be told that and not told about a migration they do not need to
	run.
	"""
	if not enabled():
		return (
			"asset mirroring is off. Tick 'Mirror Field Assets into ERPNext Assets' on "
			"ERPNext MCP Settings to have a registration also create the ERPNext Asset that "
			"puts this machine on the books. The registration itself is unaffected either way."
		)
	if not compat.doctype_exists(ASSET):
		return (
			"this site has no Asset doctype, which means ERPNext's asset module is not "
			"installed. The tag register is complete; there is nowhere to mirror it to."
		)
	if not compat.has_field(ASSET, LINK_FIELD):
		return (
			f"ERPNext's Asset has no {LINK_FIELD!r} column on this site yet, and without it a "
			"mirrored Asset could not be matched back to its tag. Run `bench --site <site> "
			"migrate` — install_compliance_fields adds it — and register again."
		)
	return ""


def mirror_of(tag: str) -> str:
	"""The ERPNext Asset docname mirroring this tag, or "" when there is none.

	MORE THAN ONE IS A FAULT AND IS REPORTED AS ONE. Two Assets pointing at a
	single tag is two sets of books for one machine; returning the first would
	make every later sync update an arbitrary half of them and leave the other
	drifting silently.
	"""
	tag = (tag or "").strip()
	if not tag or not compat.doctype_exists(ASSET) or not compat.has_field(ASSET, LINK_FIELD):
		return ""
	matches = frappe.db.get_all(ASSET, filters={LINK_FIELD: tag}, pluck="name", limit=5)
	if len(matches) == 1:
		return str(matches[0])
	return ""


def mirrors_for(tags) -> dict:
	"""`{tag: asset_docname}` for a list of tags, in one query rather than N.

	`list_assets` returns up to five hundred rows and calling `mirror_of` per row
	would be five hundred round trips to answer a column.
	"""
	wanted = [str(tag).strip() for tag in (tags or []) if str(tag or "").strip()]
	if not wanted or not compat.doctype_exists(ASSET) or not compat.has_field(ASSET, LINK_FIELD):
		return {}
	rows = frappe.db.get_all(
		ASSET,
		filters={LINK_FIELD: ("in", wanted)},
		fields=["name", LINK_FIELD],
		limit=len(wanted) * 2 + 10,
	)
	found: dict = {}
	duplicated = set()
	for row in rows or []:
		tag = str(row.get(LINK_FIELD) or "")
		if tag in found:
			duplicated.add(tag)
			continue
		found[tag] = str(row.get("name"))
	# Same rule as `mirror_of`: a tag with two Assets has no single mirror, and
	# reporting one of them would hide the fault.
	for tag in duplicated:
		found.pop(tag, None)
	return found


def sync(row: dict, *, location: str = "", photo_file: str = "") -> dict:
	"""Create or refresh the ERPNext Asset mirroring one Asset Register record.

	`row` is an `Asset Register` row as `asset_tags.asset_row` returns it. The
	verdict comes back rather than being raised: see the module docstring on why
	a failed mirror must not undo a registration.
	"""
	verdict = {"mirrored": False, "asset": None, "created": False, "reason": ""}
	try:
		return _sync(row, location=location, photo_file=photo_file, verdict=verdict)
	except Exception:
		# The tag is written and committed by the time this runs. Whatever went
		# wrong here is reported and swallowed; the traceback goes to the error
		# log so it is diagnosable rather than merely survived.
		frappe.log_error(title="erpnext_mcp: the asset mirror failed", message=compat.traceback_text())
		verdict["reason"] = (
			"the tag was registered, and building the ERPNext Asset for it failed. The "
			"traceback is in the Error Log. Nothing about the registration was rolled back."
		)
		return verdict


def _sync(row: dict, *, location: str, photo_file: str, verdict: dict) -> dict:
	blocked = available()
	if blocked:
		verdict["reason"] = blocked
		return verdict

	tag = str(row.get("name") or "").strip()
	if not tag:
		verdict["reason"] = "the register row has no docname, so there is nothing to mirror."
		return verdict

	existing = mirror_of(tag)
	if existing:
		_refresh(existing, row, photo_file)
		verdict.update(mirrored=True, asset=existing, created=False)
		return verdict

	# A tag with two Assets already: say so rather than making a third.
	if compat.has_field(ASSET, LINK_FIELD):
		count = len(frappe.db.get_all(ASSET, filters={LINK_FIELD: tag}, pluck="name", limit=5))
		if count > 1:
			verdict["reason"] = (
				f"{count} ERPNext Assets already carry a link to {tag!r}, which is more sets of "
				"books than there are machines. Nothing was written — resolve the duplicates in "
				"the Desk first."
			)
			return verdict

	missing = _missing_facts(row)
	if missing:
		verdict["reason"] = missing
		return verdict

	place, place_reason = _location(row, location)
	if place_reason:
		verdict["reason"] = place_reason
		return verdict

	company = str(row.get("company") or "")
	cost_center = compat.company_default_cost_center(company)
	if not cost_center and not frappe.db.get_value("Company", company, "depreciation_cost_center"):
		verdict["reason"] = (
			f"{company} has neither a default cost center nor an Asset Depreciation Cost Center, "
			"and ERPNext refuses an Asset that has no cost center to fall back on. Set one on the "
			"Company. The tag itself was registered."
		)
		return verdict

	category = _category(row)
	item, item_reason = _item(row, category)
	if not item:
		verdict["reason"] = item_reason
		return verdict

	doc = frappe.new_doc(ASSET)
	doc.asset_name = _asset_name(row)
	doc.item_code = item
	doc.company = company
	if category:
		doc.asset_category = category
	if place and compat.has_field(ASSET, "location"):
		doc.location = place
	doc.purchase_date = row.get("acquired_on")
	doc.gross_purchase_amount = float(row.get("purchase_value") or 0)
	# ERPNext checks `gross_purchase_amount == purchase_amount` unless the asset
	# is an existing one, and a mirror is by definition a machine the farm
	# already owns rather than one being booked off a purchase invoice.
	if compat.has_field(ASSET, "is_existing_asset"):
		doc.is_existing_asset = 1
	if compat.has_field(ASSET, "asset_quantity"):
		doc.asset_quantity = 1
	# THE line, and it is the same one `tools/assets.create_asset` calls the most
	# important in that file: ERPNext's daily scheduler posts depreciation for
	# every asset with this set, and `run_depreciation_cycle` owns the schedule
	# for anything this app creates. Both posting means depreciating twice,
	# silently, monthly, in the background.
	if compat.has_field(ASSET, "calculate_depreciation"):
		doc.calculate_depreciation = 0
	if cost_center and compat.has_field(ASSET, "cost_center"):
		doc.cost_center = cost_center
	doc.set(LINK_FIELD, tag)
	if compat.has_field(ASSET, TYPE_FIELD):
		doc.set(TYPE_FIELD, row.get("asset_type") or "")
	if compat.has_field(ASSET, SYNCED_FIELD):
		doc.set(SYNCED_FIELD, frappe.utils.now())
	for field, value in _gps_values(row).items():
		if compat.has_field(ASSET, field):
			doc.set(field, value)
	image = _image_url(photo_file)
	if image and compat.has_field(ASSET, "image"):
		doc.image = image
	doc.insert(ignore_permissions=True)

	verdict.update(mirrored=True, asset=doc.name, created=True)
	verdict["photos"] = copy_photographs(doc.name, tag)
	return verdict


def _refresh(asset: str, row: dict, photo_file: str) -> None:
	"""Bring an existing mirror's IDENTITY columns back in line with the tag.

	`frappe.db.set_value` rather than a `save`, because an Asset that has been
	submitted refuses a save on fields ERPNext does not mark allow-on-submit —
	and a mirror that stopped updating the day somebody submitted the asset would
	be a drift nobody was told about.

	WHAT IS NOT HERE IS THE POINT. No `gross_purchase_amount`, no
	`purchase_date`, no `asset_category`, no `cost_center`: those are the figures
	the ledger has been reconciled against, and a tag edit is not the place they
	change. See the module docstring.
	"""
	values = {}
	name = _asset_name(row)
	if name:
		values["asset_name"] = name
	if compat.has_field(ASSET, TYPE_FIELD):
		values[TYPE_FIELD] = row.get("asset_type") or ""
	if compat.has_field(ASSET, SYNCED_FIELD):
		values[SYNCED_FIELD] = frappe.utils.now()
	# THE COORDINATE IS REFRESHED AND THE MONEY IS NOT, and the two live one
	# line apart on purpose. A price is a fact about a transaction that happened
	# once; a position is a fact about where the thing is standing now, and the
	# map is read by somebody trying to walk to it.
	for field, value in _gps_values(row).items():
		if compat.has_field(ASSET, field):
			values[field] = value
	image = _image_url(photo_file)
	if image and compat.has_field(ASSET, "image"):
		values["image"] = image
	if values:
		frappe.db.set_value(ASSET, asset, values, update_modified=False)
	copy_photographs(asset, str(row.get("name") or ""))


def _missing_facts(row: dict) -> str:
	"""The sentence naming what ERPNext needs and this tag has not got.

	BOTH ARE NAMED AT ONCE, not one per round trip. A caller told to supply a
	purchase value, who supplies it, and is then told about an acquisition date
	has been made to do the work twice for no reason.
	"""
	missing = []
	try:
		value = float(row.get("purchase_value") or 0)
	except (TypeError, ValueError):
		value = 0.0
	if value <= 0:
		missing.append("purchase_value")
	if not row.get("acquired_on"):
		missing.append("acquired_on")
	if not missing:
		return ""
	return (
		f"the tag was registered, and it is not on the books yet because "
		f"{' and '.join(missing)} {'are' if len(missing) > 1 else 'is'} not set. ERPNext's Asset "
		"is a financial document: it marks purchase_date required and throws "
		'"Gross Purchase Amount is mandatory" on a zero, so an asset mirrored without them '
		"would carry a cost basis nobody measured into the depreciation run, the insurance "
		"schedule and Sustainable CF/Acre. Supply them with update_registered_asset and the "
		"Asset is created then."
	)


def _location(row: dict, requested: str) -> tuple:
	"""ERPNext's `Location` for this asset, or the reason there is not one.

	`Asset.location` is `reqd` on ERPNext v15 and absent on older ones, so the
	column is checked before it is demanded. Where it exists there has to be a
	real `Location` to name: this resolves the one the caller asked for, else the
	site's only one, and otherwise says what the site has rather than guessing
	between several.
	"""
	# An ERPNext old enough not to have the column needs nothing filed anywhere:
	# no place, and no reason, which is what the caller reads as "carry on".
	if not compat.has_field(ASSET, "location"):
		return "", ""
	if not compat.doctype_exists("Location"):
		return "", (
			"the tag was registered, and ERPNext's Asset on this version requires a Location "
			"which this site has no Location doctype to supply. Nothing was mirrored."
		)
	wanted = (requested or "").strip()
	if wanted:
		if frappe.db.exists("Location", wanted):
			return wanted, ""
		return "", (
			f"the tag was registered, and no Location called {wanted!r} exists on this site, so "
			"the ERPNext Asset was not created. list the Locations in the Desk and pass one that "
			"is there."
		)
	known = frappe.db.get_all("Location", pluck="name", limit=25)
	if len(known) == 1:
		return str(known[0]), ""
	if not known:
		return "", (
			"the tag was registered, and this site has no ERPNext Location records — Asset marks "
			"one required. Create the farm's Location in the Desk and register again, or pass "
			"asset_location. Nothing was mirrored."
		)
	return "", (
		"the tag was registered, and this site has "
		f"{len(known)} Locations ({', '.join(sorted(str(n) for n in known))}), so there is no "
		"single one to file the ERPNext Asset under. Pass asset_location. Nothing was mirrored."
	)


def _category(row: dict) -> str:
	"""The Asset Category for this type, where the site actually has one.

	FALLS BACK TO NOTHING RATHER THAN TO SOMETHING. `asset_category` is optional
	on ERPNext's Asset and is only consulted when depreciation is calculated,
	which for a mirror it never is — so a site that has not created
	"Irrigation Valve" yet gets an Asset with no category, and gets it now,
	rather than a refusal about a master it can create whenever it likes. Filing
	a valve under whichever category happened to exist would be worse than
	filing it under none: a wrong category names the wrong depreciation account.
	"""
	wanted = CATEGORY_BY_TYPE.get(str(row.get("asset_type") or ""), "")
	if wanted and frappe.db.exists("Asset Category", wanted):
		return wanted
	return ""


def _item(row: dict, category: str) -> tuple:
	"""The fixed-asset Item this Asset hangs off, created once per asset type.

	ERPNext will not accept an Asset whose Item is missing, disabled, a stock
	item, or not flagged `is_fixed_asset`. An existing Item that fails those is
	REFUSED rather than edited, exactly as `tools/assets._resolve_item` refuses
	it: flipping `is_fixed_asset` on an item that may have stock movements is a
	change to the site's inventory made from a handset.
	"""
	asset_type = str(row.get("asset_type") or "General").strip() or "General"
	code = ITEM_PREFIX + asset_type.upper().replace(" ", "-")
	if not compat.doctype_exists("Item"):
		return "", (
			"the tag was registered, and this site has no Item doctype for the ERPNext Asset to "
			"hang off. Nothing was mirrored."
		)
	if frappe.db.exists("Item", code):
		fields = compat.existing_fields("Item", ("name", "is_fixed_asset", "is_stock_item", "disabled"))
		found = dict(frappe.db.get_value("Item", code, fields, as_dict=True) or {})
		if compat.has_field("Item", "is_fixed_asset") and not int(found.get("is_fixed_asset") or 0):
			return "", (
				f"the tag was registered, and Item {code!r} exists on this site but is not flagged "
				"as a fixed asset, which is the one thing ERPNext requires of an Asset's item. "
				"Setting that flag is an inventory decision and is not made from here. Nothing "
				"was mirrored."
			)
		if int(found.get("disabled") or 0):
			return "", (
				f"the tag was registered, and Item {code!r} is disabled, so no Asset can be "
				"created against it. Nothing was mirrored."
			)
		return code, ""

	payload = {
		"doctype": "Item",
		"item_code": code,
		"item_name": f"{asset_type} (farm asset)",
		"is_fixed_asset": 1,
		"is_stock_item": 0,
	}
	if category:
		payload["asset_category"] = category
	group = _first_existing("Item Group", ("All Item Groups", "Products", "Fixed Asset"))
	if group:
		payload["item_group"] = group
	uom = _first_existing("UOM", ("Nos", "Unit", "Each"))
	if uom:
		payload["stock_uom"] = uom
	item = frappe.get_doc(payload).insert(ignore_permissions=True)
	return item.name, ""


def _first_existing(doctype: str, candidates) -> str:
	if not compat.doctype_exists(doctype):
		return ""
	for candidate in candidates:
		if frappe.db.exists(doctype, candidate):
			return candidate
	rows = frappe.db.get_all(doctype, pluck="name", limit=1)
	return str(rows[0]) if rows else ""


def _asset_name(row: dict) -> str:
	"""What the Asset is called on the books: the tag, then what it is.

	The tag leads because it is the string on the sticker and the string somebody
	standing at the machine will search for. `Asset.asset_name` is a plain Data
	column and is not the docname, so this is a label rather than a key and may
	be improved later without breaking anything.
	"""
	tag = str(row.get("name") or "").strip()
	detail = str(row.get("description") or "").strip() or str(row.get("asset_type") or "").strip()
	if detail and detail != tag:
		return f"{tag} — {detail}"[:140]
	return tag[:140]


def _gps_values(row: dict) -> dict:
	"""`{gps_latitude, gps_longitude}` as floats, or `{}` when there is no fix.

	BOTH OR NEITHER. Half a coordinate is not a position — it is a point on the
	prime meridian or the equator, which the map would plot in the Gulf of
	Guinea — so a row carrying one and not the other writes nothing rather than
	writing a place nobody has ever been.

	ZERO IS TREATED AS ABSENT HERE, DELIBERATELY, and it is the one place in this
	app that is the right call. `Asset Register.gps_latitude` is a Frappe Float:
	NOT NULL DEFAULT 0, so every valve tagged before anybody switched GPS on
	reads exactly 0.0 and there is no null to tell it apart from a real reading.
	Null Island is 1,600 km off the coast of Ghana; there is no farm there, and
	plotting thirty-three valves into the Atlantic is a worse answer than
	plotting none. See `a-change-guard-that-drops-zero` — this is the exception
	that rule's own note calls out, because the alternative is a stored default
	masquerading as a measurement.
	"""
	try:
		latitude = float(row.get("gps_latitude") or 0)
		longitude = float(row.get("gps_longitude") or 0)
	except (TypeError, ValueError):
		return {}
	if not latitude or not longitude:
		return {}
	return {"gps_latitude": latitude, "gps_longitude": longitude}


def copy_photographs(asset: str, tag: str) -> list:
	"""Put every photograph filed against the tag onto the Asset as well.

	THIS IS THE ONE TIM ASKED FOR, and the reason it is a copy rather than a
	move is `export_insurance_schedule`, which reads the attachments on the
	Asset Register and would be emptied by taking them away. So the blob is
	stored once and TWO File rows point at it: one on the tag, one on the Asset,
	and the photograph appears in the Attachments sidebar of both.

	`File.create_attachment_copy` IS FRAPPE'S OWN CALL FOR THIS — "efficiently
	copy an attachment from one document to another by reusing `file_url`". It
	sets `flags.copy_from_existing_file`, which skips re-writing the bytes while
	keeping the rest of the insert lifecycle. Hand-rolling the insert would
	re-read and re-store a megabyte per photograph.

	DELETING EITHER ROW IS SAFE. Frappe's `_delete_file_on_disk` removes the
	blob only when no OTHER File row shares its `content_hash`, so removing the
	copy from the Asset leaves the tag's photograph on disk, and removing the
	tag's leaves the Asset's. That guarantee is why two rows is the right shape
	and a shared `file_url` with one row would not have been.

	IT RAISES NOTHING. A photograph that could not be copied is reported and the
	Asset stands — the same trade every other step of this module makes.
	"""
	copied: list = []
	if not asset or not tag or not compat.doctype_exists("File"):
		return copied
	existing = {
		str(row.get("file_url") or "")
		for row in frappe.db.get_all(
			"File",
			filters={"attached_to_doctype": ASSET, "attached_to_name": asset},
			fields=["file_url"],
			limit=200,
		)
		or []
	}
	sources = frappe.db.get_all(
		"File",
		filters={"attached_to_doctype": ASSET_REGISTER, "attached_to_name": tag},
		fields=["name", "file_url"],
		limit=100,
	)
	for row in sources or []:
		url = str(row.get("file_url") or "")
		# Already there. Re-copying on every sync would give an Asset a fresh
		# duplicate of every photograph each time somebody edited its tag.
		if url and url in existing:
			continue
		try:
			handle = frappe.get_doc("File", row.get("name"))
			# `callable(...)` AND NOT `hasattr(...)`. Frappe's own Document has no
			# `__getattr__`, so `hasattr` answers correctly on a bench — and the
			# standalone double's does, returning None for every unknown key, so
			# `hasattr` is True there for a method that does not exist. A guard
			# that is always True would have made the fallback below unreachable
			# in the only place it can be tested.
			copier = getattr(handle, "create_attachment_copy", None)
			if callable(copier):
				made = copier(ASSET, asset, ignore_permissions=True)
				copied.append(getattr(made, "name", None) or row.get("name"))
			else:
				# An older Frappe with no such helper. The insert re-reads the
				# blob and Frappe's own content-hash check hands back the same
				# `file_url`, so the site still stores one copy.
				made = frappe.get_doc(
					{
						"doctype": "File",
						"file_url": url,
						"file_name": handle.get("file_name"),
						"is_private": handle.get("is_private"),
						"attached_to_doctype": ASSET,
						"attached_to_name": asset,
					}
				).insert(ignore_permissions=True)
				copied.append(made.name)
			if url:
				existing.add(url)
		except Exception:
			frappe.log_error(
				title="erpnext_mcp: copying an asset photograph failed",
				message=compat.traceback_text(),
			)
	return copied


def _image_url(photo_file: str) -> str:
	"""The `file_url` of an already-attached File, for `Asset.image`.

	THE FILE IS NOT RE-POINTED. A Frappe File belongs to one document, and
	`asset_tags._attach_photo` gives it to the Asset Register — which is where
	`export_insurance_schedule` reads attachments from, so taking it away would
	empty an insurance schedule to fill a thumbnail. `Asset.image` is an
	Attach Image, which stores a URL and does not need to own the file, so the
	photograph shows on both records and belongs to one.
	"""
	token = (photo_file or "").strip()
	if not token or not frappe.db.exists("File", token):
		return ""
	return str(frappe.db.get_value("File", token, "file_url") or "")
