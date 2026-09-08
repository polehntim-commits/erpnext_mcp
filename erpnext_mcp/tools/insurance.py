# SPDX-License-Identifier: MIT
"""The equipment schedule an insurer asks for, off the register that already has it.

Once a year somebody rebuilds this in a spreadsheet: every tractor, every
implement, every truck, with its serial number, what it would cost to replace,
and a photograph. The register has been collecting the same machines all along —
each one tagged, scanned and photographed by whoever was standing at it — so the
schedule is a read, and the spreadsheet was a transcription of it.

WHAT THIS ANSWERS THAT `list_assets` DOES NOT. The register is a working list: a
valve, a cabin and a block are all assets and none of them goes on an equipment
schedule. This selects the capital types, joins each one to its photograph, and —
the part that makes it worth a tool rather than a filter — REPORTS WHAT IS
MISSING. A schedule of forty machines where nine have no serial number and four
have no value is not a schedule; it is a list plus an afternoon's work, and the
afternoon is invisible until an adjuster asks. `gaps` is that afternoon, itemised
and countable, so it can be worked through before renewal rather than during a
claim.

REPLACEMENT VALUE FALLS BACK TO PURCHASE VALUE, AND SAYS WHEN IT DID. A tractor
bought in 2011 for $28,000 does not cost $28,000 to replace, and a schedule that
quietly presented the older number as cover would be understating the loss by a
decade of inflation on exactly the machines most likely to be old. So the
fallback happens — a stated number beats a blank line — and every row that used
it carries `value_basis: "purchase"`, with the count in `gaps`.

VALUES ARE NOT CONVERTED AND NOT SUMMED ACROSS COMPANIES. Each row carries its
own company; the total is refused unless the schedule is scoped to one, because
a farm holding equipment in two entities insures them on two policies and one
number spanning both is the wrong number for either.

PHOTOGRAPHS ARE FILE ATTACHMENTS AND NOT A COLUMN. `register_asset` takes a
`photo_file_token` and `attach_file_to_document` does the same job afterwards,
both landing a File row against the asset — which is also where a photograph
taken during an inspection ends up. A dedicated `photo` column would have been a
fifth place a picture of a tractor can live. Every attachment is reported, newest
first, because the useful one is usually the most recent and the older ones are
the damage history.

PRIVATE FILE URLS ARE REPORTED AS THEY ARE STORED. `/private/files/...` requires
an authenticated session to fetch, which is correct for a photograph of a farm's
equipment, and rewriting it to something public here would be this tool quietly
deciding to publish them. A caller sending the schedule to a broker fetches the
bytes with the credential it already has.
"""

from __future__ import annotations

import frappe

from .. import asset_types, compat, timezones
from ..args import as_bool, as_date, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import asset_tags

ASSET_REGISTER = asset_tags.ASSET_REGISTER
FILE = "File"

#: What goes on an equipment schedule by default: the things that are driven,
#: towed or pulled, and are worth insuring individually. A Block is land, a valve
#: is a fitting, a cabin is covered by a structures policy written against the
#: parcel — none of them belongs on this list, and a default that included them
#: would make the first thing anybody did with this tool be filtering it.
#: Widened per call with `asset_types`, because a farm insuring its cold storage
#: on the same schedule is not wrong, it is just not the default.
CAPITAL_TYPES = ("Tractor", "Vehicle", "Implement", "Sprayer")

#: How deep a parent chain is walked to describe where a machine lives. A
#: register nested deeper than this is a data problem, and an insurance export is
#: not where somebody should meet it.
PATH_DEPTH = 8

_SCHEDULE_FIELDS = (
	"name",
	"asset_type",
	"company",
	"location",
	"description",
	"serial_number",
	"model",
	"acquired_on",
	"purchase_value",
	"replacement_value",
	"gps_latitude",
	"gps_longitude",
	"retired_at",
	"last_scan_at",
	"creation",
)


def _require() -> None:
	compat.require_doctype(
		ASSET_REGISTER,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)
	for column in ("serial_number", "replacement_value"):
		if not compat.has_field(ASSET_REGISTER, column):
			raise ToolError(
				f"this site's Asset Register has no {column!r} column, so an insurance schedule "
				"would be a list of names. The column ships with erpnext_mcp v0.77.0 — run "
				"`bench --site <site> migrate`. Nothing was exported."
			)


def _types(args: dict) -> list[str]:
	"""Which asset types to schedule, defaulting to the capital ones."""
	raw = args.get("asset_types")
	if raw in (None, "", []):
		return list(CAPITAL_TYPES)
	wanted = [str(item).strip() for item in (raw if isinstance(raw, (list, tuple)) else str(raw).split(","))]
	wanted = [item for item in wanted if item]
	# v0.162.0. THE SITE'S OWN REGISTER, NOT THE SHIPPED TUPLE. A farm that adds
	# a `Generator` type and insures three of them must be able to schedule them;
	# checking against what this app shipped would have refused the type the
	# operator had just created in the Desk. Retired types are accepted — an
	# insurance schedule is a historical document and a sold sprayer was on last
	# year's.
	known = asset_types.names(enabled_only=False)
	unknown = [item for item in wanted if item not in known]
	if unknown:
		raise ToolError(
			f"asset_types names {', '.join(repr(item) for item in unknown)}, which "
			f"{'are' if len(unknown) > 1 else 'is'} not in this site's Farm Asset Type "
			f"register: {', '.join(known)}. Nothing was exported."
		)
	return wanted


def _path(name: str, parents: dict) -> list[str]:
	"""Where this machine sits, as the chain of assets above it.

	The register's `location` is a parent ASSET, so "where is it" is a path
	rather than a value — a sprayer under a shed under a ranch. Reported as the
	chain because the useful answer depends on who is asking: a broker wants the
	site, a foreman wants the shed, and either can take the end they need.
	Cycle-guarded for the reason `asset_tags._descendants` is: nothing refuses
	A → B → A.
	"""
	chain, seen, cursor = [], {name}, parents.get(name) or ""
	while cursor and cursor not in seen and len(chain) < PATH_DEPTH:
		chain.append(cursor)
		seen.add(cursor)
		cursor = parents.get(cursor) or ""
	chain.reverse()
	return chain


def _photos(names: list[str]) -> dict:
	"""Every file attached to each of these assets, newest first.

	ONE QUERY FOR THE WHOLE SCHEDULE. A schedule is forty machines and a per-row
	lookup would be forty round trips to answer one question — the same reason
	`universal_scan._alert_due_dates` batches.
	"""
	if not names or not compat.doctype_exists(FILE):
		return {}
	fields = compat.existing_fields(
		FILE, ("name", "file_name", "file_url", "is_private", "file_size", "creation")
	)
	if "file_url" not in fields:
		return {}
	try:
		rows = frappe.db.get_all(
			FILE,
			filters={"attached_to_doctype": ASSET_REGISTER, "attached_to_name": ("in", names)},
			fields=[*fields, "attached_to_name"],
			order_by="creation desc",
			limit=len(names) * 20,
		)
	except Exception:  # pragma: no cover - a site shaping File differently
		return {}

	found: dict = {}
	for row in rows or []:
		row = dict(row)
		found.setdefault(str(row.get("attached_to_name")), []).append(
			{
				"file": row.get("name"),
				"file_name": row.get("file_name") or None,
				"file_url": row.get("file_url") or None,
				"is_private": bool(frappe.utils.cint(row.get("is_private"))),
				"file_size": int(row.get("file_size") or 0) or None,
				"uploaded_at": str(row.get("creation") or "") or None,
			}
		)
	return found


def _row(asset: dict, photos: list, parents: dict, clock) -> dict:
	"""One line of the schedule."""
	replacement = asset_tags._money(asset.get("replacement_value"))
	purchase = asset_tags._money(asset.get("purchase_value"))

	if replacement is not None:
		insured, basis = replacement, "replacement"
	elif purchase is not None:
		insured, basis = purchase, "purchase"
	else:
		insured, basis = None, "none"

	path = _path(str(asset.get("name")), parents)
	entry = {
		"asset": asset.get("name"),
		"asset_type": asset.get("asset_type") or None,
		"company": asset.get("company") or None,
		"description": asset.get("description") or None,
		"serial_number": asset.get("serial_number") or None,
		"model": asset.get("model") or None,
		"acquired_on": str(asset.get("acquired_on") or "") or None,
		"purchase_value": purchase,
		"replacement_value": replacement,
		# The one number a schedule is read for, and where it came from.
		"insured_value": insured,
		"value_basis": basis,
		"parent_asset": asset.get("location") or None,
		"location_path": path,
		"location": " › ".join(path) if path else None,
		"gps_latitude": round(float(asset.get("gps_latitude") or 0), 7) or None,
		"gps_longitude": round(float(asset.get("gps_longitude") or 0), 7) or None,
		"photos": photos,
		"photo_count": len(photos),
		# The most recent attachment, lifted out because a schedule prints one
		# picture per machine and every client would otherwise write this line.
		"photo_url": photos[0]["file_url"] if photos else None,
		"retired": bool(asset.get("retired_at")),
		"retired_at": str(asset.get("retired_at") or "") or None,
		"last_scan_at": str(asset.get("last_scan_at") or "") or None,
		"registered_at": str(asset.get("creation") or "") or None,
	}
	# `acquired_on` is a DATE and gets no local twin: there is no instant in it
	# and midnight would be this app inventing one. See `timezones._naive`.
	clock.add(entry, "last_scan_at", "registered_at")
	return entry


def _gaps(rows: list) -> dict:
	"""What an adjuster would ask for that this schedule cannot answer."""
	missing_serial = [row["asset"] for row in rows if not row["serial_number"]]
	missing_value = [row["asset"] for row in rows if row["insured_value"] is None]
	missing_photo = [row["asset"] for row in rows if not row["photo_count"]]
	missing_date = [row["asset"] for row in rows if not row["acquired_on"]]
	purchase_basis = [row["asset"] for row in rows if row["value_basis"] == "purchase"]
	return {
		"missing_serial_number": missing_serial,
		"missing_serial_number_count": len(missing_serial),
		"missing_value": missing_value,
		"missing_value_count": len(missing_value),
		"missing_photo": missing_photo,
		"missing_photo_count": len(missing_photo),
		"missing_acquired_on": missing_date,
		"missing_acquired_on_count": len(missing_date),
		"valued_at_purchase_price": purchase_basis,
		"valued_at_purchase_price_count": len(purchase_basis),
		"complete": not (missing_serial or missing_value or missing_photo),
	}


# ── export_insurance_schedule ───────────────────────────────────────────────
def export_insurance_schedule(args: dict) -> ToolResult:
	"""Every capital asset as an insurance schedule line, with what is missing."""
	_require()
	company = resolve_company(as_str(args, "company"))
	types = _types(args)
	include_retired = bool(as_bool(args, "include_retired", False))
	clock = timezones.Renderer(args)

	filters: dict = {"asset_type": ("in", types)}
	if company:
		filters["company"] = company
	if not include_retired:
		filters["retired_at"] = ("is", "not set")
	acquired_before = as_date(args, "acquired_before")
	acquired_after = as_date(args, "acquired_after")
	if acquired_after and acquired_before and acquired_after > acquired_before:
		raise ToolError(
			f"acquired_after {acquired_after!r} is later than acquired_before "
			f"{acquired_before!r}. Nothing was exported."
		)
	if acquired_after and acquired_before:
		filters["acquired_on"] = ("between", [acquired_after, acquired_before])
	elif acquired_after:
		filters["acquired_on"] = (">=", acquired_after)
	elif acquired_before:
		filters["acquired_on"] = ("<=", acquired_before)

	limit = as_limit(args)
	found = (
		frappe.db.get_all(
			ASSET_REGISTER,
			filters=filters,
			fields=compat.existing_fields(ASSET_REGISTER, _SCHEDULE_FIELDS),
			order_by="asset_type asc, name asc",
			limit=limit + 1,
		)
		or []
	)
	truncated = len(found) > limit
	found = [dict(row) for row in found[:limit]]

	names = [str(row.get("name")) for row in found]
	photos = _photos(names)

	# The parent of every asset on the schedule AND of every asset above them,
	# read in one pass so `_path` is dictionary lookups rather than queries.
	parents = {str(row.get("name")): str(row.get("location") or "") for row in found}
	frontier = {value for value in parents.values() if value and value not in parents}
	for _ in range(PATH_DEPTH):
		if not frontier:
			break
		rows = (
			frappe.db.get_all(
				ASSET_REGISTER,
				filters={"name": ("in", sorted(frontier))},
				fields=compat.existing_fields(ASSET_REGISTER, ("name", "location")),
				limit=len(frontier),
			)
			or []
		)
		frontier = set()
		for row in rows:
			row = dict(row)
			key = str(row.get("name"))
			if key in parents:
				continue
			parents[key] = str(row.get("location") or "")
			if parents[key] and parents[key] not in parents:
				frontier.add(parents[key])

	schedule = [_row(row, photos.get(str(row.get("name")), []), parents, clock) for row in found]
	gaps = _gaps(schedule)

	valued = [row["insured_value"] for row in schedule if row["insured_value"] is not None]
	by_type: dict = {}
	for row in schedule:
		bucket = by_type.setdefault(row["asset_type"] or "Untyped", {"count": 0, "insured_value": 0.0})
		bucket["count"] += 1
		bucket["insured_value"] = round(bucket["insured_value"] + (row["insured_value"] or 0), 2)

	data = {
		"company": company or None,
		"asset_types": types,
		"include_retired": include_retired,
		"generated_at": str(frappe.utils.now()),
		"asset_count": len(schedule),
		"schedule": schedule,
		"by_asset_type": by_type,
		"gaps": gaps,
		"limit": limit,
		"truncated": truncated,
		**clock.block(),
	}
	data["generated_at_local"] = clock(data["generated_at"])

	# ONE COMPANY OR NO TOTAL. Two entities' equipment is two policies, and a
	# single figure spanning them is the wrong number for both of them.
	if company:
		data["total_insured_value"] = round(sum(valued), 2)
		data["valued_asset_count"] = len(valued)
	else:
		companies = sorted({row["company"] for row in schedule if row["company"]})
		data["total_insured_value"] = None
		data["valued_asset_count"] = len(valued)
		data["total_withheld_because"] = (
			"this schedule spans "
			+ (f"{len(companies)} companies ({', '.join(companies)})" if companies else "no company")
			+ ". Equipment held in two entities is insured on two policies, and one total across "
			"them is the wrong number for either — pass `company` for a total."
		)

	headline = f"{len(schedule)} asset(s)"
	if data.get("total_insured_value") is not None:
		headline += f", {data['total_insured_value']:,.2f} insured value"
	if not gaps["complete"]:
		headline += (
			f"; {gaps['missing_serial_number_count']} without a serial, "
			f"{gaps['missing_value_count']} unvalued, {gaps['missing_photo_count']} without a photo"
		)
	return ToolResult(data=data, summary=headline)
