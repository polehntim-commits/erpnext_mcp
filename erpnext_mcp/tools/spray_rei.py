# SPDX-License-Identifier: MIT
"""Restricted entry: which blocks a worker may not walk into, and until when.

40 CFR §170.407 is the rule this file is about. After a pesticide application
the treated area is closed to anyone without the label's PPE for the product's
restricted-entry interval, and the employer must keep workers out of it. The
sentence a farm owes a worker at six in the morning is one line long — *not that
block, not for another three hours, not without a respirator* — and until now
this app could not say it.

WHAT ALREADY EXISTED, AND WHY IT WAS NOT ENOUGH. `stock_bridge.spray_windows`
has computed the window since v0.69.0 and stamps `rei_expires_at` on the Farm
Task a spray closed. That is the right arithmetic in the wrong shape for the
question anybody actually asks:

  * IT IS KEYED ON THE TASK, NOT ON THE BLOCK. "Is orchard block 7 clear" is
    answered by finding every task that ever touched block 7 and reading the
    latest one's stamp, which is a scan of the task register in the hands of a
    worker holding a phone at a gate.
  * A TASK NAMES ONE LOCATION. One tank goes out over four blocks in a morning,
    and four blocks are four restrictions with four different start times.
  * A SPRAY DOES NOT HAVE TO COME OFF A TASK AT ALL. Somebody mixes a tank and
    drives, and the record of that is a state change on the sprayer.
  * NOTHING CLOSED. `rei_expires_at` is a timestamp in the past for ever after,
    so "what is restricted right now" cannot be asked of it without reading
    every task ever completed.

So a restriction is its own record, one per block per application, with a
`status` column and an `expires_at` column. `get_active_rei` is then one indexed
query, and `list_active_reis` is the board a foreman reads before assigning
anybody anywhere.

THE LONGEST INTERVAL IN THE TANK WINS, and that decision is `spray_windows`'s
and is read from there rather than repeated here — a mix of a 4-hour and a
24-hour product restricts the block for 24 hours, and the block does not become
half-enterable at hour twelve. Every product in the tank is stored beside the
one that set the window, because the question asked after somebody feels ill is
about the mix and not about the strictest line in it.

CLOSING IS AN ACT, NOT A COMPARISON. `status` could have been derived — active
is `expires_at > now` — and deriving it would make "every restriction on this
farm right now" a query no database can answer, because a filter on a computed
value is not a filter. So the column is real, `close_expired_reis` is the sweep
that maintains it, and EVERY READ IN THIS FILE RUNS THAT SWEEP FIRST. That
belt-and-braces is deliberate for exactly this record: an operator whose
scheduler is wedged gets a correct answer at a gate anyway, and the cost is one
filtered query over rows whose window has passed.

THE REFUSALS ARE THE FEATURE. A spray recorded with a product this site has no
`rei_hours` for creates NO restriction and says so loudly, rather than writing a
zero-hour window that reads as "cleared". A window nobody can compute must never
look like a window that has passed.
"""

from __future__ import annotations

import json

import frappe

from .. import compat, stock_bridge, timezones
from ..args import as_bool, as_int, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import asset_tags

SPRAY_REI = "Spray REI"
ITEM = "Item"
FARM_TASK = "Farm Task"
FIELD = "Field"
ASSET_REGISTER = asset_tags.ASSET_REGISTER

ACTIVE = "Active"
EXPIRED = "Expired"
CANCELLED = "Cancelled"

#: Where a block may live, in the order a bare docname is resolved against them.
#: `Field` first because a planted block is the thing a spray actually goes onto
#: and the register a farm keeps its rows in; an `Asset Register` row of type
#: Block is the tag on the gate, and both are legitimate answers.
BLOCK_DOCTYPES = (FIELD, ASSET_REGISTER)

#: Most blocks one application may restrict. A tank that reached more than this
#: many blocks in one pass is a data entry problem — or somebody looping a whole
#: register into one call — and either way a tailgate is not where it should be
#: discovered.
BLOCK_CAP = 50

#: Most rows `list_active_reis` hands back. The board is a screen somebody reads
#: before sending a crew out; a farm with more live restrictions than this has
#: something to answer for that a list is not going to settle.
REGISTER_CAP = 200

#: Most rows one sweep will close in a single pass. A backlog larger than this
#: is closed over consecutive runs rather than in one transaction, and the count
#: is reported so nobody reads a capped sweep as a finished one.
SWEEP_CAP = 500

_REI_FIELDS = (
	"name",
	"status",
	"block_doctype",
	"block",
	"company",
	"sprayer",
	"applicator",
	"source_task",
	"product",
	"product_name",
	"rei_hours",
	"all_products",
	"started_at",
	"expires_at",
	"closed_at",
	"notes",
)


def _require() -> None:
	compat.require_doctype(
		SPRAY_REI,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _now() -> str:
	return str(frappe.utils.now())


def _hours_between(later: str, earlier: str) -> float:
	try:
		return round(float(frappe.utils.time_diff_in_seconds(later, earlier)) / 3600.0, 2)
	except Exception:  # pragma: no cover - an unparseable stored timestamp
		return 0.0


# ── closing ─────────────────────────────────────────────────────────────────
def close_expired_reis(as_of: str = "") -> dict:
	"""Move every Active window whose moment has passed to Expired.

	THE SCHEDULER ENTRY POINT AND ALSO THE FIRST THING EVERY READ HERE DOES. It
	is idempotent, it is one filtered query on an indexed column, and running it
	twice closes nothing the second time — so calling it on the read path costs
	a query and buys an answer that does not depend on a bench's scheduler being
	healthy. On this particular record that trade is not close: a stale `Active`
	row keeps a crew out of a block that cleared hours ago, and a scheduler
	nobody noticed had stopped is exactly how that happens.

	NEVER RAISES, the same guarantee every scheduled job in this app makes. A
	single row that will not save must not leave the rest of the farm restricted.
	"""
	report = {"closed": [], "closed_count": 0, "failed": [], "truncated": False}
	if not compat.doctype_exists(SPRAY_REI):
		return report

	moment = str(as_of or _now())
	try:
		due = (
			frappe.db.get_all(
				SPRAY_REI,
				filters={"status": ACTIVE, "expires_at": ("<=", moment)},
				fields=["name", "block", "expires_at"],
				order_by="expires_at asc",
				limit=SWEEP_CAP + 1,
			)
			or []
		)
	except Exception:  # pragma: no cover - a site shaping these columns differently
		return report

	report["truncated"] = len(due) > SWEEP_CAP
	for row in due[:SWEEP_CAP]:
		name = str(dict(row).get("name"))
		try:
			doc = frappe.get_doc(SPRAY_REI, name)
			doc.status = EXPIRED
			doc.closed_at = moment
			doc.closed_by_job = 1
			doc.save(ignore_permissions=True)
			report["closed"].append(name)
		except Exception as exc:  # pragma: no cover - reported, never raised
			report["failed"].append({"name": name, "reason": f"{type(exc).__name__}: {exc}"})
	report["closed_count"] = len(report["closed"])
	return report


# ── describing ──────────────────────────────────────────────────────────────
def _products(raw) -> list:
	"""The stored tank mix, or an empty list. Never raises on a bad blob.

	A restriction whose product list will not parse is still a restriction, and
	refusing to answer "may I enter this block" over a malformed JSON column
	would be the worst possible place to be strict.
	"""
	if not raw:
		return []
	try:
		parsed = json.loads(raw) if isinstance(raw, str) else raw
	except (json.JSONDecodeError, ValueError, TypeError):
		return []
	return parsed if isinstance(parsed, list) else []


def _describe(row: dict, now: str) -> dict:
	"""One restriction, with the number a worker at a gate actually needs.

	`hours_remaining` is clamped at zero rather than going negative. A window
	that passed forty minutes ago is not "-0.7 hours remaining"; it is over, and
	`active` is the flag that says so.
	"""
	expires = str(row.get("expires_at") or "")
	status = str(row.get("status") or "")
	remaining = _hours_between(expires, now) if expires else 0.0
	active = status == ACTIVE and remaining > 0
	return {
		"name": row.get("name"),
		"status": status,
		"active": active,
		"block": row.get("block"),
		"block_doctype": row.get("block_doctype") or None,
		"company": row.get("company") or None,
		"sprayer": row.get("sprayer") or None,
		"applicator": row.get("applicator") or None,
		"source_task": row.get("source_task") or None,
		"product": row.get("product") or None,
		"product_name": row.get("product_name") or row.get("product") or None,
		"rei_hours": round(float(row.get("rei_hours") or 0), 2),
		"products": _products(row.get("all_products")),
		"started_at": str(row.get("started_at") or "") or None,
		"expires_at": expires or None,
		"closed_at": str(row.get("closed_at") or "") or None,
		"hours_remaining": max(0.0, remaining),
		"minutes_remaining": max(0, round(max(0.0, remaining) * 60)),
		"notes": row.get("notes") or None,
		"warning": warning_line(row, now) if active else None,
	}


def warning_line(row: dict, now: str = "") -> str:
	"""The one sentence a screen puts in front of somebody. Imported, not copied.

	Every surface that shows a restriction — an asset scan, a task assignment,
	the board — says the same words in the same order, because a worker who reads
	one wording at a gate and a different one on a work order has been given two
	rules. The order is what a person needs first: the product, then how long,
	then what it takes to go in anyway.
	"""
	now = now or _now()
	remaining = max(0.0, _hours_between(str(row.get("expires_at") or ""), now))
	product = row.get("product_name") or row.get("product") or "an unnamed product"
	if remaining >= 1:
		left = f"{remaining:.1f} hours remaining"
	else:
		left = f"{round(remaining * 60)} minutes remaining"
	return (
		f"REI active — {product} — {left} — do not enter without PPE. "
		f"Block {row.get('block')} was sprayed at {row.get('started_at')}; entry is permitted "
		f"from {row.get('expires_at')}."
	)


# ── the reads ───────────────────────────────────────────────────────────────
def active_rows(
	block: str = "", block_doctype: str = "", company: str = "", sprayer: str = "", limit: int = REGISTER_CAP
) -> list[dict]:
	"""Live restrictions, as raw rows. The one read every other surface shares.

	Sweeps first — see `close_expired_reis` for why that is on the read path —
	then filters on `status` AND on `expires_at`, which is belt and braces on
	purpose: the sweep may have been capped, and a row it did not reach must not
	come back as a live restriction just because nothing has closed it yet.

	Returns `[]` rather than raising where the doctype has not migrated. A site
	without the register has no restrictions on it, and a scan is not the place
	to discover a pending `bench migrate`.
	"""
	if not compat.doctype_exists(SPRAY_REI):
		return []
	close_expired_reis()
	now = _now()
	filters: dict = {"status": ACTIVE, "expires_at": (">", now)}
	if block:
		filters["block"] = block
	if block_doctype:
		filters["block_doctype"] = block_doctype
	if company:
		filters["company"] = company
	if sprayer:
		filters["sprayer"] = sprayer
	try:
		rows = frappe.db.get_all(
			SPRAY_REI,
			filters=filters,
			fields=compat.existing_fields(SPRAY_REI, _REI_FIELDS),
			order_by="expires_at asc",
			limit=min(limit, REGISTER_CAP),
		)
	except Exception:  # pragma: no cover - a site shaping these columns differently
		return []
	return [dict(row) for row in rows or []]


def active_for_blocks(blocks: list, company: str = "") -> list[dict]:
	"""Live restrictions on any of these blocks, described. Never raises.

	Used by the surfaces that know a LOCATION rather than a restriction — a scan
	of a machine parked in a block, a task about to be assigned to one. One query
	for the whole list rather than one per block: a task naming three blocks must
	not cost three round trips on a handset at the end of a row.
	"""
	names = sorted({str(name).strip() for name in blocks or []} - {""})
	if not names or not compat.doctype_exists(SPRAY_REI):
		return []
	close_expired_reis()
	now = _now()
	filters: dict = {"status": ACTIVE, "expires_at": (">", now), "block": ("in", names)}
	if company:
		filters["company"] = company
	try:
		rows = frappe.db.get_all(
			SPRAY_REI,
			filters=filters,
			fields=compat.existing_fields(SPRAY_REI, _REI_FIELDS),
			order_by="expires_at asc",
			limit=REGISTER_CAP,
		)
	except Exception:  # pragma: no cover
		return []
	return [_describe(dict(row), now) for row in rows or []]


def _resolve_block(name: str, block_doctype: str, verb: str) -> tuple[str, str]:
	"""`(docname, doctype)` for one block, or a refusal naming what was searched.

	A BARE NAME IS RESOLVED RATHER THAN REFUSED, against Field and then Asset
	Register. Both registers hold blocks on a real site and the caller is a
	handset that scanned a gate tag, not somebody who knows which table this app
	keeps rows in. Where the name is in BOTH, the caller is asked to say which:
	picking one silently would restrict a block and leave its twin open.
	"""
	name = str(name or "").strip()
	if not name:
		raise ToolError(f"a block name is required. Nothing was {verb}.")

	if block_doctype:
		if block_doctype not in BLOCK_DOCTYPES:
			raise ToolError(
				f"block_doctype must be one of {', '.join(BLOCK_DOCTYPES)}, not {block_doctype!r}. "
				f"Nothing was {verb}."
			)
		if not compat.doctype_exists(block_doctype):
			raise ToolError(f"this site has no {block_doctype!r} DocType. Nothing was {verb}.")
		if not frappe.db.exists(block_doctype, name):
			raise ToolError(f"no {block_doctype} called {name!r} on this site. Nothing was {verb}.")
		return name, block_doctype

	found = [
		doctype
		for doctype in BLOCK_DOCTYPES
		if compat.doctype_exists(doctype) and frappe.db.exists(doctype, name)
	]
	if len(found) == 1:
		return name, found[0]
	if len(found) > 1:
		raise ToolError(
			f"{name!r} is a record in {' and in '.join(found)}, and a restriction has to name one "
			f"block rather than two. Pass block_doctype. Nothing was {verb}."
		)
	searched = [d for d in BLOCK_DOCTYPES if compat.doctype_exists(d)]
	raise ToolError(
		f"no block called {name!r} — searched {', '.join(searched) or 'no register at all'}. "
		f"list_fields and list_assets have the two registers. Nothing was {verb}."
	)


# ── get_active_rei ──────────────────────────────────────────────────────────
def get_active_rei(args: dict) -> ToolResult:
	"""Is this block restricted right now, and until when."""
	_require()
	company = resolve_company(as_str(args, "company"))
	block, block_doctype = _resolve_block(
		as_str(args, "block", required=True), as_str(args, "block_doctype"), "read"
	)

	now = _now()
	rows = active_rows(block=block, block_doctype=block_doctype, company=company or "")
	windows = [_describe(row, now) for row in rows]

	clock = timezones.Renderer(args)
	for window in windows:
		clock.add(window, "started_at", "expires_at", "closed_at")

	# THE LONGEST LIVE WINDOW IS THE ANSWER, not the first or the latest spray.
	# Two applications on one block a day apart leave two rows, and the block
	# clears when the LAST of them does — a worker told "two hours" because that
	# was the nearer window would walk in under a live restriction.
	longest = max(windows, key=lambda w: w["hours_remaining"], default=None)

	data = {
		"block": block,
		"block_doctype": block_doctype,
		"company": company,
		"checked_at": now,
		"checked_at_local": clock(now),
		"restricted": bool(windows),
		"active_rei_count": len(windows),
		"active_reis": windows,
		"clears_at": longest["expires_at"] if longest else None,
		"hours_remaining": longest["hours_remaining"] if longest else 0.0,
		"warning": longest["warning"] if longest else None,
		**clock.block(),
	}
	if longest:
		clock.add(data, "clears_at")

	return ToolResult(
		data=data,
		summary=(
			f"{block}: RESTRICTED, {data['hours_remaining']} h remaining ({len(windows)} window(s))"
			if windows
			else f"{block}: clear, no active restricted-entry interval"
		),
	)


# ── list_active_reis ────────────────────────────────────────────────────────
def list_active_reis(args: dict) -> ToolResult:
	"""Every block on the farm that is closed to entry right now."""
	_require()
	company = resolve_company(as_str(args, "company"))
	sprayer = as_str(args, "sprayer")
	include_expired = bool(as_bool(args, "include_expired", False))
	limit = min(as_limit(args), REGISTER_CAP)
	now = _now()

	close_expired_reis()

	filters: dict = {}
	if include_expired:
		hours_back = as_int(args, "expired_within_hours", 24)
		since = str(frappe.utils.add_to_date(now, hours=-abs(hours_back)))
		filters["expires_at"] = (">", since)
	else:
		filters["status"] = ACTIVE
		filters["expires_at"] = (">", now)
	if company:
		filters["company"] = company
	if sprayer:
		filters["sprayer"] = sprayer
	product = as_str(args, "product")
	if product:
		filters["product"] = product

	rows = (
		frappe.db.get_all(
			SPRAY_REI,
			filters=filters,
			fields=compat.existing_fields(SPRAY_REI, _REI_FIELDS),
			order_by="expires_at asc",
			limit=limit,
		)
		or []
	)
	windows = [_describe(dict(row), now) for row in rows]

	clock = timezones.Renderer(args)
	for window in windows:
		clock.add(window, "started_at", "expires_at", "closed_at")

	active = [window for window in windows if window["active"]]
	blocks = sorted({str(window["block"]) for window in active})

	return ToolResult(
		data={
			"company": company,
			"checked_at": now,
			"checked_at_local": clock(now),
			"active_count": len(active),
			"restricted_blocks": blocks,
			"restricted_block_count": len(blocks),
			# `reis` carries the expired rows too when they were asked for, and
			# every row says whether it is live — so a client rendering a
			# "cleared in the last day" view reads one list rather than two.
			"reis": windows,
			"rei_count": len(windows),
			"included_expired": include_expired,
			**clock.block(),
		},
		summary=(
			f"{len(active)} active restricted-entry interval(s) over {len(blocks)} block(s)"
			+ (f" for {company}" if company else "")
		),
	)


# ── record_spray_application ────────────────────────────────────────────────
def _tank(args: dict) -> tuple[list, list]:
	"""`(materials, per-product REI rows)` off the argument, validated.

	The materials list is `stock_bridge`'s shape — the same one a Farm Task
	stores and `complete_farm_task` draws stock down from — so a spray recorded
	here and a spray closed through a task are describing the tank in one
	vocabulary rather than two.
	"""
	raw = args.get("materials_used")
	if raw in (None, ""):
		raw = args.get("products")
	try:
		materials = stock_bridge.parse_materials(raw, "materials_used")
	except stock_bridge.MaterialsError as exc:
		raise ToolError(f"{exc} Nothing was recorded.") from None

	detailed = []
	for line in materials:
		item_code = str(line.get("item_code"))
		hours, days = stock_bridge.item_intervals(item_code)
		entry = {
			"item_code": item_code,
			"qty": line.get("qty"),
			"rei_hours": hours,
			"phi_days": days,
		}
		if line.get("uom"):
			entry["uom"] = line["uom"]
		try:
			entry["item_name"] = str(frappe.db.get_value(ITEM, item_code, "item_name") or "") or None
		except Exception:  # pragma: no cover - an Item register shaped differently
			entry["item_name"] = None
		detailed.append(entry)
	return materials, detailed


def _sprayer(args: dict, company: str) -> dict:
	"""The machine, checked to be one. Optional — a backpack sprayer has no tag.

	REFUSED WHERE IT IS NOT A SPRAYER rather than accepted quietly, because the
	whole reason the machine is on this record is that a scan of it should tell
	the next person what it left behind, and a restriction filed against a
	tractor is one nobody will ever see.
	"""
	name = as_str(args, "sprayer") or as_str(args, "asset_name")
	if not name:
		return {}
	row = asset_tags.asset_row(name, company or "")
	asset_type = str(row.get("asset_type") or "")
	if asset_type != "Sprayer":
		raise ToolError(
			f"{row['name']} is a {asset_type or 'untyped asset'}, not a Sprayer. A restricted-entry "
			"window filed against the wrong machine is one nobody scanning the sprayer will see. "
			"Nothing was recorded."
		)
	return row


def record_spray_application(args: dict) -> ToolResult:
	"""File a completed spray and open the restricted-entry window it creates.

	ONE CALL, N RECORDS. A tank that went out over four blocks writes four
	restrictions, all with the same product, the same start and the same expiry,
	because the four blocks clear together and each is asked about separately.

	THE WINDOW IS COMPUTED FROM THE PRODUCTS AND CAN BE STATED INSTEAD. The
	ordinary path reads `rei_hours` off each Item's own label column and takes
	the longest — `stock_bridge.spray_windows`'s rule, called rather than
	reimplemented. `rei_hours` as an argument overrides that, for the two cases
	the register cannot cover: a product this site has not entered yet, and a
	state or certifier interval that is longer than the federal label.

	A SPRAY WITH NO COMPUTABLE INTERVAL CREATES NOTHING, AND SAYS SO. Where the
	Item register carries no `rei_hours` column, or where nothing in the tank has
	one, this refuses rather than writing a zero-hour window: a restriction of no
	hours reads as "this block is clear", which is the one wrong answer that puts
	somebody in a treated row. The refusal names `install_compliance_fields`,
	which is the fix.

	THE STATE CHANGE IS ATTEMPTED AND IS NOT ALLOWED TO FAIL THE RECORD. Where
	the sprayer is `in_use`, `end_spray` is logged, because that is what finishing
	a spray means to the machine. Where it is not — somebody filed the paperwork
	an hour later, or never started the timer — the restriction is written anyway
	and the state machine's refusal comes back as a warning. The compliance record
	must not depend on a worker having pressed the right button first.
	"""
	_require()
	company = resolve_company(as_str(args, "company"))
	sprayer = _sprayer(args, company or "")
	if not company and sprayer.get("company"):
		company = str(sprayer["company"])

	raw_blocks = args.get("blocks")
	if raw_blocks in (None, ""):
		raw_blocks = [args.get("block")] if args.get("block") else []
	if isinstance(raw_blocks, str):
		raw_blocks = [raw_blocks]
	if not isinstance(raw_blocks, list):
		raise ToolError(
			'blocks must be a list of block names, e.g. ["Home-7", "Home-8"]. Nothing was recorded.'
		)
	names = [str(name).strip() for name in raw_blocks if str(name or "").strip()]
	if not names:
		raise ToolError(
			"blocks is required — a restricted-entry window is a fact about a place, and a spray "
			"recorded against nowhere restricts nobody. Nothing was recorded."
		)
	if len(names) > BLOCK_CAP:
		raise ToolError(
			f"blocks has {len(names)} entries, more than the {BLOCK_CAP} one application covers. "
			"Nothing was recorded."
		)

	block_doctype = as_str(args, "block_doctype")
	resolved = []
	seen = set()
	for name in names:
		docname, doctype = _resolve_block(name, block_doctype, "recorded")
		if (doctype, docname) in seen:
			continue
		seen.add((doctype, docname))
		resolved.append((docname, doctype))

	materials, detailed = _tank(args)
	completed_at = as_str(args, "completed_at") or _now()

	stated = args.get("rei_hours")
	if stated is not None:
		try:
			rei_hours = float(stated)
		except (TypeError, ValueError):
			raise ToolError(f"rei_hours must be a number, got {stated!r}. Nothing was recorded.") from None
		if rei_hours <= 0:
			raise ToolError(
				"rei_hours must be greater than zero. A window of no hours is not a restriction, "
				"and a record saying a block is restricted for nothing reads as 'cleared'. "
				"Nothing was recorded."
			)
		source_item = as_str(args, "rei_source_item") or (detailed[0]["item_code"] if detailed else "")
		rei_source = "the rei_hours argument"
	else:
		windows = stock_bridge.spray_windows(materials, completed_at)
		rei_hours = float(windows.get("rei_hours") or 0)
		source_item = str(windows.get("rei_source_item") or "")
		rei_source = f"Item {source_item!r} (rei_hours)" if source_item else ""
		if not rei_hours:
			# WHICH refusal, because the two have different fixes. A site with no
			# `rei_hours` column at all has never run `install_compliance_fields`;
			# a site with the column and nothing in it has a product whose label
			# nobody entered. Naming the wrong one sends somebody to the wrong
			# screen at the moment they are trying to file a spray.
			cause = (
				"this site's Item register has no rei_hours column, so no label interval can be "
				"read at all — run install_compliance_fields, then record this spray again."
				if windows.get("note")
				else "nothing in the mix has rei_hours set on its Item. Set it on the product's "
				"own record, or pass rei_hours here for a state or certifier interval that is "
				"longer than the label."
			)
			raise ToolError(
				f"no restricted-entry interval could be computed for this tank: {cause} "
				"NOTHING WAS RECORDED, deliberately: a window of zero hours reads as 'this block "
				"is clear', which is the one wrong answer that puts somebody in a treated row."
			)

	expires_at = str(frappe.utils.add_to_date(completed_at, hours=rei_hours))
	source_task = as_str(args, "source_task")
	if source_task and (not compat.doctype_exists(FARM_TASK) or not frappe.db.exists(FARM_TASK, source_task)):
		raise ToolError(f"no Farm Task called {source_task!r} on this site. Nothing was recorded.")

	product_name = ""
	if source_item:
		for line in detailed:
			if line["item_code"] == source_item:
				product_name = line.get("item_name") or ""
				break
		if not product_name:
			try:
				product_name = str(frappe.db.get_value(ITEM, source_item, "item_name") or "")
			except Exception:  # pragma: no cover
				product_name = ""

	applicator = as_str(args, "applicator") or (frappe.session.user if hasattr(frappe, "session") else "")
	notes = as_str(args, "notes")

	created = []
	for docname, doctype in resolved:
		doc = frappe.new_doc(SPRAY_REI)
		doc.status = ACTIVE
		doc.block_doctype = doctype
		doc.block = docname
		doc.company = company or None
		doc.sprayer = sprayer.get("name") or None
		doc.applicator = applicator or None
		doc.source_task = source_task or None
		doc.product = source_item or None
		doc.product_name = product_name or source_item or None
		doc.rei_hours = rei_hours
		doc.all_products = json.dumps(detailed)
		doc.started_at = completed_at
		doc.expires_at = expires_at
		doc.notes = notes or None
		doc.insert(ignore_permissions=True)
		created.append(
			{
				"name": doc.name,
				"block": docname,
				"block_doctype": doctype,
				"expires_at": expires_at,
			}
		)

	# THE BLOCK'S OWN SPRAY DATE, where the register carries one. `Field.last_spray_date`
	# is what the compliance rules read for "this block has not been sprayed since",
	# and a spray recorded here that did not touch it would leave two accounts of
	# one morning. Failures are reported, never raised: the restriction is the
	# compliance record and a stamp that would not save must not lose it.
	stamped, stamp_errors = [], []
	spray_date = str(completed_at).split(" ")[0]
	if compat.doctype_exists(FIELD) and compat.has_field(FIELD, "last_spray_date"):
		for docname, doctype in resolved:
			if doctype != FIELD:
				continue
			try:
				frappe.db.set_value(FIELD, docname, "last_spray_date", spray_date)
				stamped.append(docname)
			except Exception as exc:  # pragma: no cover - reported, never raised
				stamp_errors.append({"block": docname, "reason": f"{type(exc).__name__}: {exc}"})

	warnings = []
	state_change = None
	if sprayer and as_bool(args, "end_spray", True):
		try:
			state_change = asset_tags.log_asset_state_change(
				{
					"asset_name": sprayer["name"],
					"action": "end_spray",
					"performed_by": applicator,
					"notes": (
						f"Spray recorded over {len(resolved)} block(s); {rei_hours:g} h REI to {expires_at}."
					),
				}
			).data
		except ToolError as exc:
			warnings.append(
				f"The sprayer's state was left alone: {exc} The restricted-entry window(s) below "
				"were still recorded — a compliance record does not depend on the machine having "
				"been in the state somebody expected."
			)
	for entry in stamp_errors:
		warnings.append(
			f"{FIELD} {entry['block']}'s last_spray_date was not updated ({entry['reason']}). "
			"The restriction was recorded."
		)
	unrestricted = [line["item_code"] for line in detailed if not line["rei_hours"]]
	if unrestricted and stated is None:
		warnings.append(
			f"{', '.join(unrestricted)} carries no rei_hours on its Item record and did not "
			f"contribute to the window; {source_item or 'another product'} set it. A missing "
			"interval is not the same as a zero one — check the label if any of these is a "
			"restricted-use product."
		)

	clock = timezones.Renderer(args)
	data = {
		"sprayer": sprayer.get("name") or None,
		"company": company,
		"applicator": applicator or None,
		"source_task": source_task or None,
		"completed_at": completed_at,
		"rei_hours": rei_hours,
		"rei_source": rei_source,
		"product": source_item or None,
		"product_name": product_name or source_item or None,
		"expires_at": expires_at,
		"blocks": [entry["block"] for entry in created],
		"block_count": len(created),
		"reis": created,
		"products": detailed,
		"last_spray_date_stamped": stamped,
		"state_change": state_change,
		"warning": (
			f"REI active — {product_name or source_item or 'the applied product'} — "
			f"{rei_hours:g} hours — do not enter without PPE. "
			f"{', '.join(entry['block'] for entry in created)} restricted until {expires_at}."
		),
		**clock.block(),
	}
	clock.add(data, "completed_at", "expires_at")
	for entry in data["reis"]:
		clock.add(entry, "expires_at")
	if warnings:
		data["warnings"] = warnings

	return ToolResult(
		data=data,
		summary=(
			f"spray recorded on {len(created)} block(s), {rei_hours:g} h REI "
			f"({product_name or source_item or 'stated interval'}) to {expires_at}"
		),
		docstatus_delta="none → 0 (created)",
	)


# ── cancel_spray_rei ────────────────────────────────────────────────────────
def cancel_spray_rei(args: dict) -> ToolResult:
	"""Withdraw a restriction that was recorded against the wrong block.

	CANCELLED RATHER THAN DELETED. A restriction that was published to a crew and
	then lifted is itself a thing an inspector may ask about — "why did this
	block show as closed on Tuesday morning" has an answer, and a deleted row has
	none. The reason is required for the same reason.
	"""
	_require()
	name = as_str(args, "rei", required=True)
	reason = as_str(args, "reason", required=True)
	if not frappe.db.exists(SPRAY_REI, name):
		raise ToolError(f"no {SPRAY_REI} called {name!r} on this site. Nothing was changed.")

	doc = frappe.get_doc(SPRAY_REI, name)
	if doc.status == CANCELLED:
		raise ToolError(f"{name} was already cancelled. Nothing was changed.")

	before = doc.status
	doc.status = CANCELLED
	doc.closed_at = _now()
	doc.closed_by_job = 0
	doc.notes = f"{doc.notes}\n\nCancelled: {reason}".strip() if doc.notes else f"Cancelled: {reason}"
	doc.save(ignore_permissions=True)

	return ToolResult(
		data={
			"name": doc.name,
			"block": doc.block,
			"block_doctype": doc.block_doctype,
			"from_status": before,
			"status": CANCELLED,
			"reason": reason,
			"expires_at": str(doc.expires_at or ""),
		},
		summary=f"cancelled restriction {doc.name} on {doc.block}: {reason}",
		docstatus_delta="0 → 0 (updated)",
	)
