# SPDX-License-Identifier: MIT
"""The spray program: what is in the tank, what went on the ground, in what wind.

WHAT WAS ALREADY HERE AND WHY IT WAS NOT ENOUGH. `tools/spray_rei.py` (v0.78.0)
answers one question extremely well — is this block closed to entry right now —
and `record_spray_application` exists to open that window. It was never a record
of the SPRAY. It takes a flat materials list with quantities, it has nowhere to
put a rate per acre, nowhere to put the wind, nowhere to put a licence number,
and it cannot describe a machine running two sets of tips. A farm asked for its
spray records by a state inspector cannot produce them from it.

So this module is the record, and Spray REI stays the answer. The relationship
is one direction only: an application OPENS restricted-entry windows, and every
question about restricted entry is still asked of `get_active_rei`, which is one
indexed query on a block. Nothing here re-answers it.

────────────────────────────────────────────────────────────────────────────
THE ONE DELIBERATE DIFFERENCE FROM record_spray_application
────────────────────────────────────────────────────────────────────────────

`record_spray_application` REFUSES to write anything when no restricted-entry
interval can be computed. That is right for that tool: its entire purpose is the
window, and a window of zero hours reads on every screen as "this block is
clear".

`create_spray_application` DOES NOT REFUSE. A tank of foliar nitrogen has no
label interval, restricts nobody, and is still a spray application that a farm
has to be able to record — the same pass, the same machine, the same wind, the
same acres. It records the application, creates zero Spray REI records, and says
so in `rei_records_created` and in the summary.

That difference is the single most important line in this module to get right in
both directions. Refusing here would push a real pass onto a clipboard; opening
a zero-hour window there would put somebody in a treated row.

────────────────────────────────────────────────────────────────────────────
MULTI-PRODUCT TANKS, AND THE DUAL FLIP
────────────────────────────────────────────────────────────────────────────

A tank mix is several products each at its OWN rate per acre. Not one rate for
the tank: a cover spray is two or three answers to two or three different
problems, and only a per-product rate can be checked against a label.

A dual flip machine runs one tank through two sets of tips that get flipped
during the pass — fungicide through the upper tower and insecticide through the
lower, or a band herbicide flipped in at the headland. Which product is on which
set is part of the RECIPE (it lives on the tank mix product line); which set was
actually running on a given block is part of the EVENT (it lives on the
application's block row). Those are different facts and a farm that recorded only
the first would have no way to say which block got which product when the flip
happened at a block edge, which is where it usually happens.

────────────────────────────────────────────────────────────────────────────
WEATHER IS RECORDED AND NOT ENFORCED
────────────────────────────────────────────────────────────────────────────

See `SprayApplication._weather_advisories`. Wind outside the label window earns
an advisory on the record, not a refusal. The tank went out three hours ago; a
refusal does not prevent the spray, only the record of it.
"""

from __future__ import annotations

import json

import frappe

from .. import compat, stock_bridge, timezones
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
from . import spray_rei

NOZZLE_CONFIG = "Spray Nozzle Config"
TANK_MIX = "Spray Tank Mix"
APPLICATION = "Spray Application"
SPRAY_REI = spray_rei.SPRAY_REI
ITEM = "Item"
FARM_TASK = "Farm Task"

APPLIED = "Applied"
PLANNED = "Planned"
CANCELLED = "Cancelled"

#: `Farm Task.task_type` for a spray. The same literal `dispatch.py` keeps under
#: its own name — read as a constant here rather than spelled inline, because the
#: pre-harvest read below filters on it and a typo would return an empty answer
#: that looks exactly like "this block is clear".
SPRAY_TASK_TYPE = "Spray"

#: Block resolution is `spray_rei`'s, not a second copy. A block named on an
#: application and the same block named on a restriction have to resolve to the
#: same docname in the same register, or the application would open a window on a
#: block nobody can then ask about. Reusing the private helper is deliberate: a
#: parallel implementation here is exactly the drift this avoids.
_resolve_block = spray_rei._resolve_block

#: Most product lines one tank carries. A "mix" longer than this is a data entry
#: problem, and the cap is the same order as `stock_bridge.MATERIALS_CAP`.
PRODUCT_CAP = 25

#: Most blocks one application covers, matching `spray_rei.BLOCK_CAP` so an
#: application cannot describe more blocks than it can restrict.
BLOCK_CAP = spray_rei.BLOCK_CAP

#: Most rows any list here returns.
LIST_CAP = 200

_APPLICATION_FIELDS = (
	"name",
	"status",
	"company",
	"tank_mix",
	"applicator",
	"applicator_license",
	"sprayer",
	"source_task",
	"started_at",
	"completed_at",
	"ground_speed_mph",
	"tanks_used",
	"total_acres",
	"products_applied",
	"dual_nozzle",
	"nozzle_set_a",
	"nozzle_set_b",
	"set_a_purpose",
	"set_b_purpose",
	"flip_performed",
	"flip_at",
	"gallons_per_acre",
	"wind_speed_mph",
	"wind_direction",
	"temperature_f",
	"humidity_pct",
	"sky_conditions",
	"weather_source",
	"weather_recorded_at",
	"weather_advisories",
	"rei_hours",
	"rei_source_item",
	"rei_expires_at",
	"phi_days",
	"phi_source_item",
	"phi_clears_on",
	"rei_records_created",
	"cancelled_on",
	"cancellation_reason",
	"notes",
)

_NOZZLE_FIELDS = (
	"name",
	"nozzle_name",
	"company",
	"nozzle_type",
	"manufacturer",
	"orifice_color",
	"disabled",
	"flow_rate_gpm",
	"rated_pressure_psi",
	"droplet_class",
	"pattern",
	"spacing_inches",
	"nozzles_active",
	"boom_width_ft",
	"notes",
)


# ── shared ──────────────────────────────────────────────────────────────────
def _require(doctype: str) -> None:
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _now() -> str:
	return str(frappe.utils.now())


def _number(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def _checked(value) -> bool:
	return compat.checked(value)


# ── the pre-harvest interval, read back off the block ───────────────────────
#
# THE OTHER HALF OF THE INTERVAL PAIR, AND IT LIVES HERE RATHER THAN IN
# `spray_rei.py` FOR ONE REASON: an REI has a REGISTER — one indexed Spray REI
# row per block, opened by the application and closed by a sweep — and a PHI has
# none. The pre-harvest interval is a DATE STAMPED ON WHATEVER RECORDED THE
# SPRAY, and this app records a spray in two places on purpose: a Spray
# Application (this module, with the wind and the licence on it) and a completed
# Farm Task of type Spray (`stock_bridge.spray_windows` stamps the window when
# the tank mix is drawn down). Both are real, both are current, and a reader that
# consulted one of them would clear a block that the other says is closed.
#
# SO BOTH ARE READ, AND THE LONGEST WINS. Two products applied a week apart leave
# two dates; the block opens on the later one. Nothing here merges the records —
# each window keeps the register it came from, because "which spray was this" is
# the first question asked when somebody disputes a date.
#
# THE BOUNDARY IS THE SAME ONE THE COMPLIANCE RULE USES, and it is the reason
# `phi_harvest_window` and this function agree to the day: the interval is live
# while `phi_clears_on >= today` and the block opens the day AFTER that date. A
# day of over-caution against a residue violation on a shipped load is not a
# close call, and a guard that cleared a block a day before the alert about it
# went out would be worse than no guard.

#: The two registers a spray leaves a pre-harvest date on. Named rather than
#: derived so the two branches below read as one claim about where the fact is.
PHI_SOURCES = (APPLICATION, FARM_TASK)

#: `Farm Task.state` values whose stamped window counts. A DRAFT spray task has
#: a `phi_clears_on` of nothing and an in-progress one has not finished spraying
#: — the window is anchored on the completion, so only a task that reached one
#: has a date worth reading. The same two states the `phi_harvest_window`
#: compliance rule scopes on, and for the same reason.
PHI_TASK_STATES = ("Awaiting-Review", "Completed")

#: Most windows one call reports. A block with more than this many live
#: pre-harvest intervals on it has a data problem, not a spray program.
PHI_CAP = 100


def _kept_for_company(row: dict, company: str) -> bool:
	"""Whether a pre-harvest window belongs in an answer scoped to `company`.

	v0.230.1. A WINDOW WITH NO COMPANY IS KEPT — the rule `spray_rei._for_company`
	keeps for REI, for the same reason. `company` is optional on a Spray Application
	and on a Farm Task; an equality filter dropped a spray filed without one, and on
	the harvest guard dropping a window means telling somebody a block can be picked.
	"""
	return not company or not row.get("company") or row.get("company") == company


def _phi_from_applications(names: list, company: str, today: str) -> list[dict]:
	"""Live pre-harvest windows off Spray Application, one query plus one.

	The block lives on a CHILD TABLE, so this is the same two-step
	`list_spray_applications` uses and for the same reason: a join would need raw
	SQL, which this app does not write, and a per-row query would cost a round
	trip per application on a handset at the end of a row.
	"""
	if not compat.doctype_exists(APPLICATION):
		return []
	# v0.230.1. Company is NOT a query filter — see `_kept_for_company`.
	filters: dict = {"status": APPLIED, "phi_clears_on": (">=", today)}
	try:
		rows = frappe.db.get_all(
			APPLICATION,
			filters=filters,
			fields=compat.existing_fields(
				APPLICATION,
				["name", "company", "phi_clears_on", "phi_days", "phi_source_item", "completed_at"],
			),
			order_by="phi_clears_on desc",
			limit=PHI_CAP,
		)
	except Exception:  # pragma: no cover - a site shaping these columns differently
		return []
	rows = [dict(row) for row in rows or [] if _kept_for_company(dict(row), company)]
	if not rows:
		return []

	blocks = _blocks_by_application([row["name"] for row in rows])
	out = []
	for row in rows:
		for entry in blocks.get(row["name"], []):
			block = str(entry.get("block") or "")
			if block not in names:
				continue
			out.append(
				{
					"block": block,
					"block_doctype": str(entry.get("block_doctype") or "") or None,
					"source_doctype": APPLICATION,
					"source": row["name"],
					"company": row.get("company") or None,
					"phi_clears_on": str(row.get("phi_clears_on") or ""),
					"phi_days": _number(row.get("phi_days")) or None,
					"phi_source_item": row.get("phi_source_item") or None,
					"sprayed_at": str(row.get("completed_at") or "") or None,
				}
			)
	return out


def _phi_from_tasks(names: list, company: str, today: str) -> list[dict]:
	"""Live pre-harvest windows off a completed Spray Farm Task.

	`stock_bridge.spray_windows` stamps `phi_clears_on` on the task when its tank
	mix is drawn down, which is the path a spray dispatched from the board takes.
	A site that files sprays that way and never writes a Spray Application would
	get an empty answer from the branch above and a correct one from this.
	"""
	if not compat.doctype_exists(FARM_TASK) or not compat.has_field(FARM_TASK, "phi_clears_on"):
		return []
	filters: dict = {
		"task_type": SPRAY_TASK_TYPE,
		"state": ("in", list(PHI_TASK_STATES)),
		"phi_clears_on": (">=", today),
		"location": ("in", names),
	}
	try:
		rows = frappe.db.get_all(
			FARM_TASK,
			filters=filters,
			fields=compat.existing_fields(
				FARM_TASK,
				[
					"name",
					"company",
					"location",
					"location_doctype",
					"phi_clears_on",
					"phi_source_item",
					"spray_completed_at",
				],
			),
			order_by="phi_clears_on desc",
			limit=PHI_CAP,
		)
		# v0.195.0. A TASK A SPRAY APPLICATION CITES IS THAT APPLICATION'S SPRAY,
		# and the branch above already answered for it — counting the task again
		# listed one spray as two windows. The same rule `spray_rei.task_windows`
		# keeps for REI. A cancelled application no longer speaks for the task.
		cited = set()
		names_found = [str(dict(row).get("name") or "") for row in rows or []]
		if names_found and compat.doctype_exists(APPLICATION):
			cited = set(
				frappe.db.get_all(
					APPLICATION,
					filters={"source_task": ("in", names_found), "status": ("!=", "Cancelled")},
					pluck="source_task",
				)
				or []
			)
		rows = [
			row
			for row in rows or []
			if str(dict(row).get("name") or "") not in cited and _kept_for_company(dict(row), company)
		]
	except Exception:  # pragma: no cover
		return []
	return [
		{
			"block": str(dict(row).get("location") or ""),
			"block_doctype": str(dict(row).get("location_doctype") or "") or None,
			"source_doctype": FARM_TASK,
			"source": str(dict(row).get("name") or ""),
			"company": dict(row).get("company") or None,
			"phi_clears_on": str(dict(row).get("phi_clears_on") or ""),
			"phi_days": None,
			"phi_source_item": dict(row).get("phi_source_item") or None,
			"sprayed_at": str(dict(row).get("spray_completed_at") or "") or None,
		}
		for row in rows or []
	]


def phi_windows_for_blocks(blocks, company: str = "") -> list[dict]:
	"""Every live pre-harvest interval on any of these blocks. NEVER RAISES.

	The one read every surface that asks "may this be picked" shares — the same
	shape and the same contract as `spray_rei.active_for_blocks`, which answers
	the entry question. Returns `[]` where nothing is restricted, where the
	doctypes have not migrated, or where a column this site shapes differently
	will not answer: a harvest guard that failed CLOSED on an unreadable register
	would stop a farm picking because of a schema question, and one that failed
	open would be silent about the only thing it exists to say. It fails open and
	`clears_on` is stamped on the records either way, which is what the scheduled
	compliance sweep reads.

	Sorted latest-clearing first, so the caller's first row is the date the block
	actually opens.
	"""
	names = sorted({str(name).strip() for name in blocks or []} - {""})
	if not names:
		return []
	today = frappe.utils.today()
	found = []
	try:
		found = _phi_from_applications(names, company, today) + _phi_from_tasks(names, company, today)
	except Exception:  # pragma: no cover - a warning, never a refusal
		return []
	for window in found:
		window["days_remaining"] = _days_until(window["phi_clears_on"], today)
		window["warning"] = (
			f"PHI active on {window['block']} — no harvest until "
			f"{_day_after(window['phi_clears_on'])}"
			+ (f" ({window['phi_source_item']}" if window["phi_source_item"] else " (")
			+ (f", sprayed {window['sprayed_at'].split(' ')[0]}" if window["sprayed_at"] else "")
			+ f", {window['source']}). A pick inside the interval is a residue violation on a "
			"shipped load."
		)
	return sorted(found, key=lambda window: window["phi_clears_on"], reverse=True)


def _days_until(clears_on: str, today: str) -> int | None:
	"""Whole days from today to the day the block opens. None where unreadable."""
	try:
		return int(frappe.utils.date_diff(_day_after(clears_on), today))
	except Exception:  # pragma: no cover - a hand-edited date column
		return None


def _day_after(clears_on: str) -> str:
	"""The first date the block may be picked.

	`phi_clears_on` is the last date of the interval, not the first clear one —
	the `phi_harvest_window` rule raises while it is `>= today` and silences the
	day after. Reporting the raw column as "no harvest until X" would be off by
	one in the dangerous direction, so it is added here, once, where every
	message reads it.
	"""
	try:
		return str(frappe.utils.add_days(str(clears_on).split(" ")[0], 1))
	except Exception:  # pragma: no cover
		return str(clears_on)


# ── create_spray_nozzle_config ──────────────────────────────────────────────
def create_spray_nozzle_config(args: dict) -> ToolResult:
	"""Register one nozzle set as it is plumbed on a machine."""
	_require(NOZZLE_CONFIG)
	company = resolve_company(as_str(args, "company"))
	nozzle_name = as_str(args, "nozzle_name", required=True)

	if frappe.db.exists(NOZZLE_CONFIG, nozzle_name):
		raise ToolError(
			f"a nozzle configuration called {nozzle_name!r} already exists. The name is the "
			"docname, and every application that points at it means this one. Name the new set "
			"for what distinguishes it — the tip size, the tower position. Nothing was created."
		)

	flow = as_float(args.get("flow_rate_gpm"), "flow_rate_gpm")
	if flow <= 0:
		raise ToolError(
			"flow_rate_gpm must be greater than zero, and it is gallons per minute PER NOZZLE off "
			"the manufacturer's chart — not the boom total. A set that flows nothing makes every "
			"rate computed through it a division by zero. Nothing was created."
		)

	doc = frappe.new_doc(NOZZLE_CONFIG)
	doc.nozzle_name = nozzle_name
	doc.company = company or None
	doc.flow_rate_gpm = flow
	doc.manufacturer = as_str(args, "manufacturer") or None
	doc.orifice_color = as_str(args, "orifice_color") or None
	doc.rated_pressure_psi = as_float(args.get("rated_pressure_psi"), "rated_pressure_psi")
	doc.spacing_inches = as_float(args.get("spacing_inches"), "spacing_inches")
	doc.boom_width_ft = as_float(args.get("boom_width_ft"), "boom_width_ft")
	doc.nozzles_active = as_int(args, "nozzles_active") or 0
	doc.notes = as_str(args, "notes") or None
	for key in ("nozzle_type", "pattern", "droplet_class"):
		value = as_str(args, key)
		if value:
			doc.set(key, as_choice(NOZZLE_CONFIG, key, value, key))
	if as_bool(args, "disabled"):
		doc.disabled = 1
	doc.insert(ignore_permissions=True)

	warnings = []
	if not doc.spacing_inches:
		warnings.append(
			"No nozzle spacing recorded, so gallons per acre cannot be computed for applications "
			"through this set. An air-blast tower legitimately has none; a boom does not."
		)
	if not doc.droplet_class:
		warnings.append(
			"No droplet class recorded. It is the drift control on most labels — a label reading "
			"'apply as Coarse or coarser' is an instruction about this field."
		)
	if not doc.rated_pressure_psi:
		warnings.append(
			"No rated pressure recorded. A flow rate without the pressure it was read at cannot "
			"be checked against the manufacturer's chart."
		)

	return ToolResult(
		data={**_describe_nozzle(dict(doc.as_dict())), "warnings": warnings},
		summary=f"registered nozzle set {doc.name} at {flow:g} GPM/nozzle",
		docstatus_delta="none → 0 (created)",
	)


def _describe_nozzle(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"nozzle_name": row.get("nozzle_name"),
		"company": row.get("company") or None,
		"nozzle_type": row.get("nozzle_type") or None,
		"manufacturer": row.get("manufacturer") or None,
		"orifice_color": row.get("orifice_color") or None,
		"disabled": _checked(row.get("disabled")),
		"flow_rate_gpm": round(_number(row.get("flow_rate_gpm")), 3),
		"rated_pressure_psi": round(_number(row.get("rated_pressure_psi")), 1) or None,
		"droplet_class": row.get("droplet_class") or None,
		"pattern": row.get("pattern") or None,
		"spacing_inches": round(_number(row.get("spacing_inches")), 2) or None,
		"nozzles_active": int(row.get("nozzles_active") or 0) or None,
		"boom_width_ft": round(_number(row.get("boom_width_ft")), 2) or None,
		"notes": row.get("notes") or None,
	}


# ── list_spray_nozzle_configs ───────────────────────────────────────────────
def list_spray_nozzle_configs(args: dict) -> ToolResult:
	"""Every nozzle set on file."""
	_require(NOZZLE_CONFIG)
	company = resolve_company(as_str(args, "company"))

	filters: dict = {}
	if company:
		filters["company"] = company
	for key in ("nozzle_type", "pattern", "droplet_class"):
		value = as_str(args, key)
		if value:
			filters[key] = value
	if not as_bool(args, "include_disabled", False):
		filters["disabled"] = 0

	rows = frappe.db.get_all(
		NOZZLE_CONFIG,
		filters=filters,
		fields=compat.existing_fields(NOZZLE_CONFIG, _NOZZLE_FIELDS),
		order_by="nozzle_name asc",
		limit=min(as_limit(args), LIST_CAP),
	)
	configs = [_describe_nozzle(dict(row)) for row in rows or []]
	return ToolResult(
		data={"count": len(configs), "nozzle_configs": configs},
		summary=f"{len(configs)} nozzle configuration(s)",
	)


# ── create_spray_tank_mix ───────────────────────────────────────────────────
def _mix_products(args: dict, key: str = "products") -> list[dict]:
	"""The product lines off the argument, with each Item's label copied in.

	THE LABEL NUMBERS ARE READ HERE AND STORED, not fetched at read time. A mix
	written in April and read in a hearing in November has to say what the label
	said in April; a live join says what it says now, which is a different claim.
	"""
	raw = args.get(key)
	if raw in (None, ""):
		raise ToolError(
			f"{key} is required — a tank mix is what is in the tank. Send a list like "
			'[{"item_code": "SULFUR-90", "rate_per_acre": 5, "rate_uom": "Lb", '
			'"nozzle_set": "A"}]. Nothing was created.'
		)
	if isinstance(raw, dict):
		raw = [raw]
	if not isinstance(raw, list):
		raise ToolError(f"{key} must be a list of objects, got {type(raw).__name__}. Nothing was created.")
	if len(raw) > PRODUCT_CAP:
		raise ToolError(
			f"{key} has {len(raw)} lines, more than the {PRODUCT_CAP} one tank carries. Nothing was created."
		)

	out = []
	seen = set()
	for index, entry in enumerate(raw):
		if not isinstance(entry, dict):
			raise ToolError(f"{key}[{index}] must be an object. Nothing was created.")
		item_code = str(entry.get("item_code") or entry.get("item") or "").strip()
		if not item_code:
			raise ToolError(f"{key}[{index}] names no item_code. Nothing was created.")
		if not frappe.db.exists(ITEM, item_code):
			raise ToolError(f"no Item called {item_code!r} on this site. Nothing was created.")
		if item_code in seen:
			raise ToolError(
				f"{item_code!r} is listed twice. One product gets one rate in one tank — two lines "
				"is two rates, and nothing reading the mix could say which applied. Nothing was "
				"created."
			)
		seen.add(item_code)

		try:
			rate = float(entry.get("rate_per_acre"))
		except (TypeError, ValueError):
			raise ToolError(
				f"{key}[{index}].rate_per_acre must be a number, got "
				f"{entry.get('rate_per_acre')!r}. Nothing was created."
			) from None
		if rate <= 0:
			raise ToolError(
				f"{key}[{index}].rate_per_acre must be greater than zero. A product in the tank at "
				"no rate is either not in the tank or is a rate nobody entered, and the two need "
				"different corrections. Nothing was created."
			)

		nozzle_set = str(entry.get("nozzle_set") or "Both").strip().title()
		if nozzle_set in ("A", "B"):
			pass
		elif nozzle_set.lower() == "both":
			nozzle_set = "Both"
		else:
			raise ToolError(
				f"{key}[{index}].nozzle_set must be 'A', 'B' or 'Both', got "
				f"{entry.get('nozzle_set')!r}. Nothing was created."
			)

		rei_hours, phi_days = stock_bridge.item_intervals(item_code)
		item_name = epa = ""
		try:
			item_name = str(frappe.db.get_value(ITEM, item_code, "item_name") or "")
		except Exception:  # pragma: no cover - an Item register shaped differently
			item_name = ""
		if compat.has_field(ITEM, "epa_registration_number"):
			try:
				epa = str(frappe.db.get_value(ITEM, item_code, "epa_registration_number") or "")
			except Exception:  # pragma: no cover
				epa = ""

		out.append(
			{
				"item": item_code,
				"item_name": item_name or item_code,
				"rate_per_acre": rate,
				"rate_uom": str(entry.get("rate_uom") or "").strip() or None,
				"nozzle_set": nozzle_set,
				"rei_hours": rei_hours,
				"phi_days": phi_days,
				"epa_reg_number": str(entry.get("epa_reg_number") or epa or "").strip() or None,
				"target": str(entry.get("target") or "").strip() or None,
				# v0.272.0. The lot the jug came from, when the applicator records it — `trace_input_lot`
				# then follows that lot exactly instead of by the dates it was in the shed.
				**({"lot_no": str(entry.get("lot_no")).strip()[:80]} if str(entry.get("lot_no") or "").strip() else {}),
			}
		)
	return out


def create_spray_tank_mix(args: dict) -> ToolResult:
	"""Write a tank mix: several products, each at its own rate per acre."""
	_require(TANK_MIX)
	company = resolve_company(as_str(args, "company"))
	mix_name = as_str(args, "mix_name", required=True)

	if frappe.db.exists(TANK_MIX, mix_name):
		raise ToolError(
			f"a tank mix called {mix_name!r} already exists. The name is the docname and every "
			"application filed against it means this one. Nothing was created."
		)

	products = _mix_products(args)
	dual = bool(as_bool(args, "dual_nozzle", False))
	set_a = as_str(args, "nozzle_set_a")
	set_b = as_str(args, "nozzle_set_b")

	for label, value in (("nozzle_set_a", set_a), ("nozzle_set_b", set_b)):
		if value and not frappe.db.exists(NOZZLE_CONFIG, value):
			raise ToolError(
				f"no Spray Nozzle Config called {value!r} on this site — {label} has to name one. "
				"list_spray_nozzle_configs has the register. Nothing was created."
			)

	# The dual-set rules are the controller's and are checked there too. They are
	# ALSO checked here so the refusal is a sentence naming the argument to fix
	# rather than a framework validation error surfacing from doc.insert().
	if dual:
		sets = {line["nozzle_set"] for line in products}
		if "A" not in sets or "B" not in sets:
			missing = "B" if "B" not in sets else "A"
			raise ToolError(
				f"dual_nozzle is set but no product is assigned to set {missing}. A dual mix is "
				"products split across two sets that get flipped mid-pass; if everything runs "
				"through whichever set is on, leave dual_nozzle off — that is a single-set mix. "
				"Nothing was created."
			)
		if not set_a or not set_b:
			raise ToolError(
				"a dual mix names both nozzle sets — nozzle_set_a and nozzle_set_b. A flip to an "
				"unnamed set is not a record of anything. Nothing was created."
			)
		if set_a == set_b:
			raise ToolError(
				f"nozzle_set_a and nozzle_set_b are both {set_a!r}, which is one set rather than "
				"two. Nothing gets flipped. Nothing was created."
			)

	doc = frappe.new_doc(TANK_MIX)
	doc.mix_name = mix_name
	doc.company = company or None
	doc.crop = as_str(args, "crop") or None
	doc.target_pest = as_str(args, "target_pest") or None
	doc.season_year = as_int(args, "season_year") or 0
	doc.tank_size_gal = as_float(args.get("tank_size_gal"), "tank_size_gal")
	doc.carrier_gpa = as_float(args.get("carrier_gpa"), "carrier_gpa")
	doc.dual_nozzle = 1 if dual else 0
	doc.nozzle_set_a = set_a or None
	doc.nozzle_set_b = set_b or None
	doc.set_a_purpose = as_str(args, "set_a_purpose") or None
	doc.set_b_purpose = as_str(args, "set_b_purpose") or None
	doc.notes = as_str(args, "notes") or None
	status = as_str(args, "status")
	doc.status = as_choice(TANK_MIX, "status", status, "status") if status else "Draft"
	for line in products:
		doc.append("products", line)
	doc.insert(ignore_permissions=True)

	described = _describe_mix(doc)
	warnings = []
	if not described["rei_hours"]:
		warnings.append(
			"Nothing in this mix has an REI on its Item, so an application of it opens no "
			"restricted-entry window. For a foliar nutrient that is correct. For a pesticide it "
			"means the label interval has not been entered — set rei_hours on the product's own "
			"Item record, or run install_compliance_fields if this site has no such column."
		)
	if not described["acres_per_tank"]:
		warnings.append(
			"Tank size or carrier rate is missing, so acres per tank cannot be computed — that is "
			"the number the person filling the tank actually works with."
		)
	return ToolResult(
		data={**described, "warnings": warnings},
		summary=(
			f"tank mix {doc.name}: {len(products)} product(s), "
			f"REI {described['rei_hours']:g}h" + (", dual set" if dual else "")
		),
		docstatus_delta="none → 0 (created)",
	)


def _describe_mix(doc) -> dict:
	row = dict(doc.as_dict()) if hasattr(doc, "as_dict") else dict(doc)
	products = []
	for line in row.get("products") or []:
		line = dict(line)
		products.append(
			{
				"item": line.get("item"),
				"item_name": line.get("item_name") or line.get("item"),
				"rate_per_acre": round(_number(line.get("rate_per_acre")), 4),
				"rate_uom": line.get("rate_uom") or None,
				"nozzle_set": line.get("nozzle_set") or "Both",
				"rei_hours": round(_number(line.get("rei_hours")), 2),
				"phi_days": round(_number(line.get("phi_days")), 2),
				"epa_reg_number": line.get("epa_reg_number") or None,
				"target": line.get("target") or None,
			}
		)
	return {
		"name": row.get("name"),
		"mix_name": row.get("mix_name"),
		"status": row.get("status"),
		"company": row.get("company") or None,
		"crop": row.get("crop") or None,
		"target_pest": row.get("target_pest") or None,
		"season_year": int(row.get("season_year") or 0) or None,
		"tank_size_gal": round(_number(row.get("tank_size_gal")), 2) or None,
		"carrier_gpa": round(_number(row.get("carrier_gpa")), 2) or None,
		"acres_per_tank": round(_number(row.get("acres_per_tank")), 3),
		"dual_nozzle": _checked(row.get("dual_nozzle")),
		"nozzle_set_a": row.get("nozzle_set_a") or None,
		"nozzle_set_b": row.get("nozzle_set_b") or None,
		"set_a_purpose": row.get("set_a_purpose") or None,
		"set_b_purpose": row.get("set_b_purpose") or None,
		"rei_hours": round(_number(row.get("rei_hours")), 2),
		"rei_source_item": row.get("rei_source_item") or None,
		"phi_days": round(_number(row.get("phi_days")), 2),
		"phi_source_item": row.get("phi_source_item") or None,
		"product_count": len(products),
		"products": products,
		"notes": row.get("notes") or None,
	}


# ── create_spray_application ────────────────────────────────────────────────
def _application_blocks(args: dict) -> list[dict]:
	"""The block rows, resolved against the same registers Spray REI uses.

	Accepts three shapes because three callers are plausible and all three mean
	the same thing: a bare list of names off a handset, a list of objects with
	acres from a desk, and a single name from somebody spraying one block.
	"""
	raw = args.get("blocks")
	if raw in (None, ""):
		raw = [args.get("block")] if args.get("block") else []
	if isinstance(raw, (str, dict)):
		raw = [raw]
	if not isinstance(raw, list):
		raise ToolError(
			'blocks must be a list — either of names ["Home-7", "Home-8"] or of objects '
			'[{"block": "Home-7", "acres": 4.2, "nozzle_set_used": "A then B"}]. '
			"Nothing was recorded."
		)
	entries = [entry for entry in raw if entry not in (None, "")]
	if not entries:
		raise ToolError(
			"blocks is required. A pass recorded against nowhere restricts nobody and costs "
			"nothing to any block. Nothing was recorded."
		)
	if len(entries) > BLOCK_CAP:
		raise ToolError(
			f"blocks has {len(entries)} entries, more than the {BLOCK_CAP} one application covers. "
			"Nothing was recorded."
		)

	default_doctype = as_str(args, "block_doctype")
	out = []
	seen = set()
	for entry in entries:
		if isinstance(entry, dict):
			name = str(entry.get("block") or entry.get("name") or "").strip()
			doctype_hint = str(entry.get("block_doctype") or default_doctype or "").strip()
			acres = entry.get("acres")
			nozzle_used = str(entry.get("nozzle_set_used") or "Both").strip()
			started = str(entry.get("started_at") or "").strip()
			completed = str(entry.get("completed_at") or "").strip()
			tanks = entry.get("tanks_used")
			notes = str(entry.get("notes") or "").strip()
		else:
			name, doctype_hint = str(entry).strip(), default_doctype
			acres = None
			nozzle_used = "Both"
			started = completed = notes = ""
			tanks = None

		docname, doctype = _resolve_block(name, doctype_hint, "recorded")
		if (doctype, docname) in seen:
			continue
		seen.add((doctype, docname))

		if nozzle_used not in ("Both", "A", "B", "A then B", "B then A"):
			raise ToolError(
				f"nozzle_set_used on block {docname!r} must be one of: Both, A, B, 'A then B', "
				f"'B then A'. Got {nozzle_used!r}. Nothing was recorded."
			)
		acres_value = _number(acres)
		if acres_value < 0:
			raise ToolError(f"acres on block {docname!r} cannot be negative. Nothing was recorded.")
		if acres in (None, "") and doctype == "Field" and compat.has_field("Field", "acreage"):
			# v0.230.3. A bare name used to mean 0 acres, so the use report put
			# rate × 0 = 0 on a pass that covered the whole block. The phone route
			# already reads the Field's acreage; the tool now does too.
			acres_value = _number(frappe.db.get_value("Field", docname, "acreage"))

		out.append(
			{
				"block_doctype": doctype,
				"block": docname,
				"acres": acres_value,
				"nozzle_set_used": nozzle_used,
				"started_at": started or None,
				"completed_at": completed or None,
				"tanks_used": _number(tanks) or 0,
				"notes": notes or None,
			}
		)
	return out


def _products_for_application(args: dict, mix: dict | None) -> list[dict]:
	"""The tank as applied — the mix's own lines, or an explicit override.

	An explicit `products` list WINS OVER THE MIX and does not have to match it.
	That is not laxness: a crew that halved a rate because the block is young, or
	dropped a product they ran out of, has applied something different from the
	recipe, and the record has to be of what went out. The difference is reported
	rather than refused.
	"""
	if args.get("products") not in (None, ""):
		return _mix_products(args)
	if mix:
		return [
			{
				"item": line.get("item"),
				"item_name": line.get("item_name") or line.get("item"),
				"rate_per_acre": _number(line.get("rate_per_acre")),
				"rate_uom": line.get("rate_uom") or None,
				"nozzle_set": line.get("nozzle_set") or "Both",
				"rei_hours": _number(line.get("rei_hours")),
				"phi_days": _number(line.get("phi_days")),
				"epa_reg_number": line.get("epa_reg_number") or None,
				"target": line.get("target") or None,
			}
			for line in mix.get("products") or []
		]
	return []


def create_spray_application(args: dict) -> ToolResult:
	"""File a spray: what went out, where, when, in what weather — and open its REIs."""
	_require(APPLICATION)
	company = resolve_company(as_str(args, "company"))

	status = as_str(args, "status") or APPLIED
	status = as_choice(APPLICATION, "status", status, "status")

	mix_name = as_str(args, "tank_mix")
	mix = None
	if mix_name:
		if not frappe.db.exists(TANK_MIX, mix_name):
			raise ToolError(f"no Spray Tank Mix called {mix_name!r} on this site. Nothing was recorded.")
		mix = _describe_mix(frappe.get_doc(TANK_MIX, mix_name))
		if not company and mix.get("company"):
			company = mix["company"]

	blocks = _application_blocks(args)
	products = _products_for_application(args, mix)
	if not products:
		raise ToolError(
			"nothing is in the tank. Pass tank_mix to apply a recipe on file, or products for a "
			"tank mixed at the machine. Nothing was recorded."
		)

	completed_at = spray_rei._moment(args, "completed_at") or _now()
	started_at = spray_rei._moment(args, "started_at") or completed_at
	if str(completed_at) < str(started_at):
		raise ToolError(
			f"completed_at ({completed_at}) is before started_at ({started_at}). Every "
			"restricted-entry window runs from the completion, so this would open a window that "
			"closed before the spray began. Nothing was recorded."
		)

	sprayer = as_str(args, "sprayer")
	if sprayer and not frappe.db.exists(spray_rei.ASSET_REGISTER, sprayer):
		raise ToolError(f"no Asset Register record called {sprayer!r}. Nothing was recorded.")
	source_task = as_str(args, "source_task")
	if source_task and (not compat.doctype_exists(FARM_TASK) or not frappe.db.exists(FARM_TASK, source_task)):
		raise ToolError(f"no Farm Task called {source_task!r} on this site. Nothing was recorded.")

	set_a = as_str(args, "nozzle_set_a") or (mix or {}).get("nozzle_set_a") or ""
	set_b = as_str(args, "nozzle_set_b") or (mix or {}).get("nozzle_set_b") or ""
	for label, value in (("nozzle_set_a", set_a), ("nozzle_set_b", set_b)):
		if value and not frappe.db.exists(NOZZLE_CONFIG, value):
			raise ToolError(f"no Spray Nozzle Config called {value!r} — {label}. Nothing was recorded.")
	dual = as_bool(args, "dual_nozzle")
	if dual is None:
		dual = bool((mix or {}).get("dual_nozzle"))

	# ── the intervals ───────────────────────────────────────────────────────
	# `spray_windows` is called rather than reimplemented: the longest-in-the-tank
	# rule has one home, and a second copy here would be a second answer to "how
	# long is this block shut".
	materials = [{"item_code": line["item"], "qty": 1} for line in products]
	windows = stock_bridge.spray_windows(materials, completed_at)
	phi_days = _number(windows.get("phi_days"))
	phi_source = str(windows.get("phi_source_item") or "")
	phi_clears_on = windows.get("phi_clears_on") or None

	stated = args.get("rei_hours")
	if stated is not None:
		# A STATED INTERVAL OVERRIDES EVERY LABEL IN THE TANK. Two cases the
		# register cannot cover: a product this site has not entered yet, and a
		# state or certifier interval longer than the federal label. It does NOT
		# override the PHI, which is a separate label fact with its own argument.
		try:
			rei_hours = float(stated)
		except (TypeError, ValueError):
			# v0.230.3: "abc" became 0 and was answered as "must be greater than zero".
			raise ToolError(f"rei_hours must be a number of hours, got {stated!r}. Nothing was recorded.") from None
		if rei_hours <= 0:
			raise ToolError(
				"rei_hours must be greater than zero when stated. To record a spray that restricts "
				"nobody — a foliar nutrient — omit it entirely and no window is opened. Nothing "
				"was recorded."
			)
		rei_source = as_str(args, "rei_source_item") or products[0]["item"]
	else:
		rei_hours = _number(windows.get("rei_hours"))
		rei_source = str(windows.get("rei_source_item") or "")

	doc = frappe.new_doc(APPLICATION)
	doc.status = status
	doc.company = company or None
	doc.tank_mix = mix_name or None
	doc.applicator = (
		as_str(args, "applicator") or (frappe.session.user if hasattr(frappe, "session") else "") or None
	)
	doc.applicator_license = as_str(args, "applicator_license") or None
	doc.sprayer = sprayer or None
	doc.source_task = source_task or None
	doc.started_at = started_at
	doc.completed_at = completed_at
	doc.ground_speed_mph = as_float(args.get("ground_speed_mph"), "ground_speed_mph")
	doc.tanks_used = as_float(args.get("tanks_used"), "tanks_used")
	doc.dual_nozzle = 1 if dual else 0
	doc.nozzle_set_a = set_a or None
	doc.nozzle_set_b = set_b or None
	doc.set_a_purpose = as_str(args, "set_a_purpose") or (mix or {}).get("set_a_purpose") or None
	doc.set_b_purpose = as_str(args, "set_b_purpose") or (mix or {}).get("set_b_purpose") or None
	doc.flip_performed = 1 if as_bool(args, "flip_performed", False) else 0
	doc.flip_at = as_str(args, "flip_at") or None
	doc.wind_speed_mph = _reading(args, "wind_speed_mph", 0, 200)
	doc.wind_direction = as_str(args, "wind_direction") or None
	doc.temperature_f = _reading(args, "temperature_f", -60, 140)
	doc.humidity_pct = _reading(args, "humidity_pct", 0, 100)
	doc.sky_conditions = as_str(args, "sky_conditions") or None
	weather_source = as_str(args, "weather_source")
	if weather_source:
		doc.weather_source = as_choice(APPLICATION, "weather_source", weather_source, "weather_source")
	doc.weather_recorded_at = as_str(args, "weather_recorded_at") or None
	doc.rei_hours = rei_hours
	doc.rei_source_item = rei_source or None
	doc.phi_days = phi_days
	doc.phi_source_item = phi_source or None
	doc.phi_clears_on = phi_clears_on
	doc.notes = as_str(args, "notes") or None

	total_acres = round(sum(line["acres"] for line in blocks), 3)
	for line in blocks:
		doc.append(
			"blocks",
			{
				"block_doctype": line["block_doctype"],
				"block": line["block"],
				"acres": line["acres"],
				"nozzle_set_used": line["nozzle_set_used"],
				"started_at": line["started_at"],
				"completed_at": line["completed_at"],
				"tanks_used": line["tanks_used"],
				"notes": line["notes"],
			},
		)
	doc.products_applied = json.dumps(_applied_snapshot(products, total_acres))
	doc.insert(ignore_permissions=True)

	# ── the restricted-entry windows ────────────────────────────────────────
	reis, rei_errors, latest_expiry = _open_reis(doc, products, rei_hours, rei_source, company)
	if reis or rei_errors:
		doc.rei_records_created = len(reis)
		doc.rei_expires_at = latest_expiry or None
		for row in doc.blocks:
			for created in reis:
				if created["block"] == row.block and created["block_doctype"] == row.block_doctype:
					row.rei_record = created["name"]
		doc.save(ignore_permissions=True)

	described = _describe_application(dict(doc.as_dict()), include_blocks=True)
	notes = _application_notes(doc, mix, products, rei_hours, status, rei_errors)

	clock = timezones.Renderer(args)
	clock.add(described, "started_at", "completed_at", "flip_at", "rei_expires_at", "weather_recorded_at")

	return ToolResult(
		data={**described, "notes_for_caller": notes, "timezone": clock.block()},
		summary=(
			f"spray {doc.name}: {len(products)} product(s) over {len(blocks)} block(s), "
			f"{total_acres:g} ac, {len(reis)} REI window(s)"
		),
		docstatus_delta="none → 0 (created)",
	)


def _applied_snapshot(products: list[dict], total_acres: float) -> list[dict]:
	"""The mix as applied, with the total of each product this pass put out."""
	snapshot = []
	for line in products:
		entry = dict(line)
		entry["total_applied"] = (
			round(_number(line.get("rate_per_acre")) * total_acres, 4) if total_acres else None
		)
		snapshot.append(entry)
	return snapshot


def _open_reis(doc, products: list[dict], rei_hours: float, rei_source: str, company: str):
	"""One Spray REI per block, or none at all — and never a zero-hour window.

	NOTHING IS CREATED WHEN THE TANK RESTRICTS NOBODY. See the module docstring:
	a foliar nutrient has no label interval and closes no block, and an
	application of it is still a real record. A window of zero hours would read
	on every screen as "this block is clear", which is the one wrong answer.

	A PLANNED APPLICATION OPENS NOTHING either. Nothing has been put on the
	ground, and a restriction on a block nobody has sprayed keeps a crew out of
	somewhere they may work.

	FAILURES ARE ITEMISED, NEVER RAISED. The application is already on the
	record; one block whose window will not save must not lose the record of the
	whole pass — but it must also not be silent, because that block is then
	unrestricted and somebody has to know.
	"""
	created: list[dict] = []
	errors: list[dict] = []
	if str(doc.status) != APPLIED or rei_hours <= 0:
		return created, errors, ""
	if not compat.doctype_exists(SPRAY_REI):
		errors.append(
			{
				"block": "*",
				"reason": "the Spray REI DocType is not installed on this site, so no "
				"restricted-entry window could be opened. Run `bench migrate`.",
			}
		)
		return created, errors, ""

	product_name = ""
	for line in products:
		if line["item"] == rei_source:
			product_name = line.get("item_name") or ""
			break

	latest = ""
	for row in doc.blocks:
		# The block's OWN completion where the pass was long enough for the blocks
		# to differ. A block sprayed at eight in the morning does not stay shut
		# because the last block of the pass finished at two.
		anchor = str(row.completed_at or doc.completed_at)
		expires = str(frappe.utils.add_to_date(anchor, hours=rei_hours))
		try:
			rei = frappe.new_doc(SPRAY_REI)
			rei.status = spray_rei.ACTIVE
			rei.block_doctype = row.block_doctype
			rei.block = row.block
			rei.company = company or None
			rei.sprayer = doc.sprayer or None
			rei.applicator = doc.applicator or None
			rei.source_task = doc.source_task or None
			rei.product = rei_source or None
			rei.product_name = product_name or rei_source or None
			rei.rei_hours = rei_hours
			rei.all_products = json.dumps(_rei_products(products, _number(row.acres)))
			rei.started_at = anchor
			rei.expires_at = expires
			rei.notes = f"Opened by Spray Application {doc.name}."
			rei.insert(ignore_permissions=True)
		except Exception as exc:  # pragma: no cover - reported, never raised
			errors.append({"block": row.block, "reason": f"{type(exc).__name__}: {exc}"})
			continue
		created.append(
			{
				"name": rei.name,
				"block": row.block,
				"block_doctype": row.block_doctype,
				"expires_at": expires,
			}
		)
		latest = max(latest, expires)
	return created, errors, latest


def _rei_products(products: list[dict], acres: float) -> list[dict]:
	"""The tank in `spray_rei`'s own shape, so `get_active_rei` renders it.

	`qty` is the rate times THIS BLOCK's acres, which is a more honest number
	than the tank total: the question asked after somebody feels ill is what
	went onto the ground they were standing on.
	"""
	out = []
	for line in products:
		entry = {
			"item_code": line["item"],
			"item_name": line.get("item_name"),
			"qty": round(_number(line.get("rate_per_acre")) * acres, 4) if acres else None,
			"rei_hours": _number(line.get("rei_hours")),
			"phi_days": _number(line.get("phi_days")),
		}
		if line.get("rate_uom"):
			entry["uom"] = line["rate_uom"]
		out.append(entry)
	return out


def _application_notes(doc, mix, products, rei_hours, status, rei_errors) -> list[str]:
	"""What the caller should know, in the order it matters."""
	notes = []
	for failure in rei_errors:
		notes.append(
			f"NO RESTRICTED-ENTRY WINDOW WAS OPENED ON {failure['block']}: {failure['reason']} "
			"That block is not showing as restricted and a crew could be sent into it."
		)
	if status == PLANNED:
		notes.append(
			"Status is Planned, so no restricted-entry window was opened — nothing has been put "
			"on the ground yet. File it again as Applied, or update it, once the pass is made."
		)
	elif rei_hours <= 0:
		notes.append(
			"No product in this tank has an REI on its Item, so this application opened no "
			"restricted-entry window and no block is closed by it. For a foliar nutrient that is "
			"correct. For a pesticide it means a label interval has not been entered — set "
			"rei_hours on the product's Item, or pass rei_hours here for a state interval."
		)
	if status != PLANNED and not doc.get("phi_clears_on"):
		notes.append(
			"No product in this tank has a PHI on its Item, so no pre-harvest interval was recorded "
			"and the harvest guard treats these blocks as pickable. For a foliar nutrient that is "
			"correct. For a pesticide it means the label's PHI has not been entered — set phi_days "
			"on the product's Item."
		)
	unlabelled = _registered_without_intervals(products)
	if status != PLANNED and unlabelled:
		notes.append(
			"CHECK THE LABEL: "
			+ "; ".join(f"{item} has an EPA registration number but no {', '.join(gaps)}" for item, gaps in unlabelled)
			+ ". A registered pesticide almost always carries both intervals; until they are on the Item, "
			"this pass restricts less than the label does."
		)
	if doc.weather_advisories:
		notes.extend(str(doc.weather_advisories).split("\n"))
	if mix and _mix_differs(mix.get("products") or [], products):
		notes.append(
			f"What was applied differs from tank mix {mix['mix_name']!r} as it is written. The "
			"record is of what went out, which is correct — but if the recipe has changed for "
			"good, update the mix so the next pass starts from the right numbers."
		)
	if doc.dual_nozzle and not doc.flip_performed:
		notes.append(
			"This is a dual-set application and flip_performed is not set. If the flip did not "
			"happen, only one set's products reached the ground and the record above overstates "
			"what was applied."
		)
	if not doc.applicator_license:
		notes.append(
			"No applicator licence recorded. It is one of the first things asked for in a state "
			"pesticide record inspection."
		)
	return notes


def _reading(args: dict, key: str, low: float, high: float):
	"""One weather number off the arguments: None when absent, refused when it is not one.

	v0.230.1. Taken raw, "12 mph" was stored as 0.0 — dead calm, on the line of a
	spray record an inspector reads first — and raised a false inversion advisory.
	"""
	raw = args.get(key)
	if raw in (None, ""):
		return None
	try:
		value = float(raw)
	except (TypeError, ValueError):
		raise ToolError(f"{key} must be a number, got {raw!r}. Nothing was recorded.") from None
	if not low <= value <= high:
		raise ToolError(f"{key} must be between {low:g} and {high:g}, got {value:g}. Nothing was recorded.")
	return value


def _registered_without_intervals(products: list[dict]) -> list[tuple[str, list[str]]]:
	"""`[(item, ["REI", "PHI"])]` for products with an EPA registration number and a blank interval.

	v0.230.1. A blank `rei_hours` / `phi_days` reads as "nothing to restrict", which is
	right for a foliar nutrient and wrong for a registered pesticide. Never raises.
	"""
	wanted = [f for f in ("epa_registration_number", "rei_hours", "phi_days") if compat.has_field("Item", f)]
	if "epa_registration_number" not in wanted:
		return []
	items = sorted({str(line.get("item") or "") for line in products or []} - {""})
	if not items:
		return []
	try:
		rows = frappe.db.get_all("Item", filters={"name": ("in", items)}, fields=["name", *wanted]) or []
	except Exception:  # pragma: no cover
		return []
	out = []
	for row in rows:
		row = dict(row)
		if not str(row.get("epa_registration_number") or "").strip():
			continue
		gaps = [label for label, field in (("REI", "rei_hours"), ("PHI", "phi_days")) if field in wanted and _number(row.get(field)) <= 0]
		if gaps:
			out.append((str(row["name"]), gaps))
	return sorted(out)


def _mix_differs(mix_products: list[dict], applied: list[dict]) -> bool:
	"""Whether the tank as applied departs from the recipe, on item or on rate."""
	recipe = {line.get("item"): round(_number(line.get("rate_per_acre")), 4) for line in mix_products}
	actual = {line.get("item"): round(_number(line.get("rate_per_acre")), 4) for line in applied}
	return recipe != actual


def _describe_application(row: dict, include_blocks: bool = False) -> dict:
	out = {
		"name": row.get("name"),
		"status": row.get("status"),
		"company": row.get("company") or None,
		"tank_mix": row.get("tank_mix") or None,
		"applicator": row.get("applicator") or None,
		"applicator_license": row.get("applicator_license") or None,
		"sprayer": row.get("sprayer") or None,
		"source_task": row.get("source_task") or None,
		"started_at": str(row.get("started_at") or "") or None,
		"completed_at": str(row.get("completed_at") or "") or None,
		"ground_speed_mph": round(_number(row.get("ground_speed_mph")), 2) or None,
		"tanks_used": round(_number(row.get("tanks_used")), 2) or None,
		"total_acres": round(_number(row.get("total_acres")), 3),
		"gallons_per_acre": round(_number(row.get("gallons_per_acre")), 3) or None,
		"dual_nozzle": _checked(row.get("dual_nozzle")),
		"nozzle_set_a": row.get("nozzle_set_a") or None,
		"nozzle_set_b": row.get("nozzle_set_b") or None,
		"set_a_purpose": row.get("set_a_purpose") or None,
		"set_b_purpose": row.get("set_b_purpose") or None,
		"flip_performed": _checked(row.get("flip_performed")),
		"flip_at": str(row.get("flip_at") or "") or None,
		"weather": {
			"wind_speed_mph": _optional(row.get("wind_speed_mph")),
			"wind_direction": row.get("wind_direction") or None,
			"temperature_f": _optional(row.get("temperature_f")),
			"humidity_pct": _optional(row.get("humidity_pct")),
			"sky_conditions": row.get("sky_conditions") or None,
			"source": row.get("weather_source") or None,
			"recorded_at": str(row.get("weather_recorded_at") or "") or None,
		},
		"weather_advisories": [
			line for line in str(row.get("weather_advisories") or "").split("\n") if line.strip()
		],
		"rei_hours": round(_number(row.get("rei_hours")), 2),
		"rei_source_item": row.get("rei_source_item") or None,
		"rei_expires_at": str(row.get("rei_expires_at") or "") or None,
		"rei_records_created": int(row.get("rei_records_created") or 0),
		"phi_days": round(_number(row.get("phi_days")), 2),
		"phi_source_item": row.get("phi_source_item") or None,
		"phi_clears_on": str(row.get("phi_clears_on") or "") or None,
		"cancelled_on": str(row.get("cancelled_on") or "") or None,
		"cancellation_reason": row.get("cancellation_reason") or None,
		"notes": row.get("notes") or None,
	}
	products = row.get("products_applied")
	if products:
		try:
			parsed = json.loads(products) if isinstance(products, str) else products
			out["products_applied"] = parsed if isinstance(parsed, list) else []
		except (json.JSONDecodeError, ValueError, TypeError):
			out["products_applied"] = []
	else:
		out["products_applied"] = []
	if include_blocks:
		out["blocks"] = [
			{
				"block": dict(line).get("block"),
				"block_doctype": dict(line).get("block_doctype"),
				"acres": round(_number(dict(line).get("acres")), 3),
				"nozzle_set_used": dict(line).get("nozzle_set_used") or "Both",
				"started_at": str(dict(line).get("started_at") or "") or None,
				"completed_at": str(dict(line).get("completed_at") or "") or None,
				"tanks_used": round(_number(dict(line).get("tanks_used")), 2) or None,
				"rei_record": dict(line).get("rei_record") or None,
				"notes": dict(line).get("notes") or None,
			}
			for line in row.get("blocks") or []
		]
	return out


def _optional(value):
	"""A number, or None where the field was genuinely never filled in.

	Weather readings need this and totals do not: a wind speed of 0.0 is a real
	and important observation (dead calm is an inversion), while an unrecorded
	wind speed is a gap in a compliance record. Collapsing both to 0 would make
	the two indistinguishable on exactly the field where the difference matters
	most.
	"""
	if value in (None, ""):
		return None
	return round(_number(value), 2)


# ── list_spray_applications ─────────────────────────────────────────────────
def list_spray_applications(args: dict) -> ToolResult:
	"""Spray applications over a window, newest first."""
	_require(APPLICATION)
	company = resolve_company(as_str(args, "company"))

	filters: dict = {}
	if company:
		filters["company"] = company
	for key in ("tank_mix", "sprayer", "applicator", "source_task"):
		value = as_str(args, key)
		if value:
			filters[key] = value
	status = as_str(args, "status")
	if status:
		filters["status"] = as_choice(APPLICATION, "status", status, "status")

	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters["completed_at"] = ("between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"])
	elif from_date:
		filters["completed_at"] = (">=", f"{from_date} 00:00:00")
	elif to_date:
		filters["completed_at"] = ("<=", f"{to_date} 23:59:59")

	rows = frappe.db.get_all(
		APPLICATION,
		filters=filters,
		fields=compat.existing_fields(APPLICATION, _APPLICATION_FIELDS),
		order_by="completed_at desc",
		limit=min(as_limit(args), LIST_CAP),
	)
	applications = [_describe_application(dict(row)) for row in rows or []]

	# Filtering by block is done AFTER the query rather than in it, because the
	# block lives on a child table and a join would either need raw SQL — which
	# this app does not write — or a second query per row. The cap keeps the
	# post-filter honest, and `truncated` says when it bit.
	block = as_str(args, "block")
	if block:
		names = [app["name"] for app in applications]
		matching = set()
		if names:
			rows = frappe.db.get_all(
				"Spray Application Block",
				filters={"parent": ("in", names), "block": block},
				fields=["parent"],
				limit=LIST_CAP * 5,
			)
			matching = {str(dict(row).get("parent")) for row in rows or []}
		applications = [app for app in applications if app["name"] in matching]

	clock = timezones.Renderer(args)
	for app in applications:
		clock.add(app, "started_at", "completed_at", "rei_expires_at")

	total_acres = round(sum(app["total_acres"] for app in applications), 3)
	missing_weather = [app["name"] for app in applications if app["weather"]["wind_speed_mph"] is None]
	return ToolResult(
		data={
			"count": len(applications),
			"total_acres": total_acres,
			"applications": applications,
			"applications_without_wind_recorded": missing_weather,
			"truncated": len(applications) >= min(as_limit(args), LIST_CAP),
			"timezone": clock.block(),
		},
		summary=f"{len(applications)} spray application(s) over {total_acres:g} ac",
	)


# ── get_spray_application ───────────────────────────────────────────────────
def get_spray_application(args: dict) -> ToolResult:
	"""One application in full, with the restrictions it is still holding open."""
	_require(APPLICATION)
	name = as_str(args, "application", required=True)
	if not frappe.db.exists(APPLICATION, name):
		raise ToolError(
			f"no Spray Application called {name!r} on this site. list_spray_applications has the register."
		)
	doc = frappe.get_doc(APPLICATION, name)
	described = _describe_application(dict(doc.as_dict()), include_blocks=True)

	# THE LIVE RESTRICTIONS ARE READ THROUGH `spray_rei`, not recomputed. Whether
	# a block is closed right now is that module's question and it sweeps expired
	# windows before answering, so this stays correct on a bench whose scheduler
	# has stopped.
	blocks = [row["block"] for row in described.get("blocks") or []]
	live = spray_rei.active_for_blocks(blocks, described.get("company") or "")
	by_block: dict = {}
	for window in live:
		by_block.setdefault(str(window.get("block")), []).append(window)
	for row in described.get("blocks") or []:
		windows = by_block.get(str(row["block"]), [])
		row["restricted_now"] = bool(windows)
		row["active_restrictions"] = windows

	mix = None
	if described.get("tank_mix") and frappe.db.exists(TANK_MIX, described["tank_mix"]):
		mix = _describe_mix(frappe.get_doc(TANK_MIX, described["tank_mix"]))

	clock = timezones.Renderer(args)
	clock.add(described, "started_at", "completed_at", "flip_at", "rei_expires_at", "weather_recorded_at")

	restricted = [row["block"] for row in described.get("blocks") or [] if row.get("restricted_now")]
	return ToolResult(
		data={
			**described,
			"tank_mix_detail": mix,
			"blocks_restricted_now": restricted,
			"timezone": clock.block(),
		},
		summary=(
			f"{name}: {len(described.get('blocks') or [])} block(s), "
			f"{described['total_acres']:g} ac, {len(restricted)} still restricted"
		),
	)


def describe_applications(names: list, args: dict | None = None) -> dict:
	"""`get_spray_application`'s answer for many applications at once. v0.231.3.

	THE PHONE'S SPRAY HISTORY ASKED FOR EACH ROW SEPARATELY — the application and
	its blocks, the REI sweep and query, the tank mix and its products — about eleven
	queries a row, a thousand for a full page on a Pi. Here: the applications in one
	read, their blocks in a second, the live restrictions once per company (the sweep
	once with them), and each distinct tank mix once. The answer for each name is
	the single read's, field for field — `test_polish_v0_231_3` holds them equal.
	"""
	args = args or {}
	names = [str(name) for name in names or [] if name]
	if not names:
		return {}
	_require(APPLICATION)
	rows = frappe.db.get_all(APPLICATION, filters={"name": ("in", names)}, fields=["*"], limit=len(names))
	lines: dict = {}
	if compat.doctype_exists("Spray Application Block"):
		for line in (
			frappe.db.get_all(
				"Spray Application Block",
				filters={"parent": ("in", names), "parenttype": APPLICATION},
				fields=["*"],
				order_by="idx asc",
				limit=len(names) * BLOCK_CAP,
			)
			or []
		):
			entry = dict(line)
			lines.setdefault(str(entry.get("parent") or ""), []).append(entry)
	described: dict = {}
	for row in rows or []:
		row = dict(row)
		row["blocks"] = lines.get(str(row["name"]), [])
		described[str(row["name"])] = _describe_application(row, include_blocks=True)

	blocks_by_company: dict = {}
	for app in described.values():
		blocks_by_company.setdefault(app.get("company") or "", set()).update(
			b["block"] for b in app.get("blocks") or []
		)
	live = {
		company: spray_rei.active_for_blocks(sorted(blocks), company)
		for company, blocks in blocks_by_company.items()
	}

	mixes: dict = {}
	out: dict = {}
	for name in names:
		app = described.get(name)
		if app is None:
			continue
		mine = {b["block"] for b in app.get("blocks") or []}
		by_block: dict = {}
		for window in live.get(app.get("company") or "", []):
			if str(window.get("block")) in mine:
				by_block.setdefault(str(window.get("block")), []).append(window)
		for row in app.get("blocks") or []:
			windows = by_block.get(str(row["block"]), [])
			row["restricted_now"] = bool(windows)
			row["active_restrictions"] = windows
		mix_name = app.get("tank_mix")
		if mix_name and mix_name not in mixes:
			mixes[mix_name] = (
				_describe_mix(frappe.get_doc(TANK_MIX, mix_name)) if frappe.db.exists(TANK_MIX, mix_name) else None
			)
		clock = timezones.Renderer(args)
		clock.add(app, "started_at", "completed_at", "flip_at", "rei_expires_at", "weather_recorded_at")
		restricted = [row["block"] for row in app.get("blocks") or [] if row.get("restricted_now")]
		out[name] = {
			**app,
			"tank_mix_detail": mixes.get(mix_name) if mix_name else None,
			"blocks_restricted_now": restricted,
			"timezone": clock.block(),
		}
	return out


# ── get_spray_application_report ────────────────────────────────────────────
#
# WHAT WENT OUT, PER PRODUCT, PER BLOCK, OVER A SEASON. Every state that
# regulates pesticide use asks a version of the same question — Oregon's PARC
# reporting, California's monthly PUR, Washington's WPS records under WAC
# 16-233 — and all of them want it summed by product and located on the ground.
# Until this existed the data was here one application at a time and the sum was
# somebody's spreadsheet, which is the shape of a number nobody can reproduce
# twelve months later in front of the person asking.
#
# THE PER-BLOCK QUANTITY IS RATE × THAT BLOCK'S ACRES, never the tank total
# spread evenly. `_rei_products` settled this doctrine for restrictions and it
# is the same doctrine here: what a regulator asks about a block is what went
# onto THAT ground, and an even split across blocks of unequal size is a number
# that was never true of any of them.
#
# QUANTITIES ARE SUMMED PER UNIT AND NEVER ACROSS UNITS. One product recorded at
# lb/acre on one pass and qt/acre on another has two totals here, not one, and
# the response says the units are mixed. A single figure would require a density
# this app does not have, and a wrong total on a pesticide use report is the
# kind of wrong that gets discovered by an inspector rather than by an auditor.

#: Most applications one report reads. A season on a large operation is
#: hundreds; this is generous and the report says when it bit.
REPORT_CAP = 1000

#: Most block rows the report joins in one go, sized off the application cap.
_REPORT_BLOCK_CAP = REPORT_CAP * BLOCK_CAP


def _report_window(args: dict) -> tuple[str, str, bool]:
	"""The reporting period. Defaults to the calendar year in progress.

	A DEFAULT RATHER THAN A REQUIREMENT, because the question is almost always
	"this season", and a report that refused to run without dates would be run
	with whatever dates got typed first. `defaulted` comes back so the response
	can say the window was chosen rather than given.
	"""
	from_date = as_date(args, "date_from") or as_date(args, "from_date")
	to_date = as_date(args, "date_to") or as_date(args, "to_date")
	defaulted = not (from_date or to_date)
	today = str(frappe.utils.today())
	if not from_date:
		from_date = f"{today[:4]}-01-01"
	if not to_date:
		to_date = today
	if str(to_date) < str(from_date):
		raise ToolError(f"date_to ({to_date}) is before date_from ({from_date}). Nothing was reported.")
	return str(from_date), str(to_date), defaulted


def _blocks_by_application(names: list) -> dict:
	"""`{application: [block rows]}` for a batch, in one query."""
	if not names or not compat.doctype_exists("Spray Application Block"):
		return {}
	found: dict = {}
	for row in (
		frappe.db.get_all(
			"Spray Application Block",
			filters={"parent": ("in", names)},
			fields=["parent", "block", "block_doctype", "acres", "started_at", "completed_at"],
			limit=_REPORT_BLOCK_CAP,
		)
		or []
	):
		entry = dict(row)
		found.setdefault(str(entry.get("parent") or ""), []).append(entry)
	return found


def _product_lines(row: dict) -> list[dict]:
	"""The products on one application, off the stored JSON snapshot.

	READ OFF THE APPLICATION, NOT OFF THE TANK MIX AND NOT OFF THE ITEM. The mix
	says what was planned and the Item says what the label says today; the
	snapshot says what went out, at the rate it went out at, with the label
	numbers as they read on the day. A use report assembled from a live join
	would answer a question about now, and the question is about April.
	"""
	raw = row.get("products_applied")
	if not raw:
		return []
	try:
		parsed = json.loads(raw) if isinstance(raw, str) else raw
	except (json.JSONDecodeError, ValueError, TypeError):
		return []
	return [dict(line) for line in parsed if isinstance(line, dict)] if isinstance(parsed, list) else []


def _blank_product(line: dict) -> dict:
	return {
		"product": line.get("item"),
		"product_name": line.get("item_name") or line.get("item"),
		"epa_reg_number": line.get("epa_reg_number") or None,
		"rei_hours": _number(line.get("rei_hours")) or None,
		"phi_days": _number(line.get("phi_days")) or None,
		"targets": [],
		"total_by_uom": {},
		"total_acres_treated": 0.0,
		"application_count": 0,
		"blocks": {},
		"applications": [],
		"applicators": [],
		"first_application": None,
		"last_application": None,
	}


def get_spray_application_report(args: dict) -> ToolResult:
	"""Chemical usage over a period, grouped by product and then by block.

	THE REPORT A REGULATOR ASKS FOR. `list_spray_applications` answers "what did
	we spray on the 14th"; this answers "how much captan went onto Block 7 this
	season, on what dates, by whom, and what did it close" — which is the
	question every pesticide use reporting scheme is a form of, and which no
	amount of reading the register one row at a time answers reliably.

	APPLIED ONLY. A planned application is a plan and a cancelled one did not
	happen; neither belongs in a total of what went onto the ground. Both are
	counted in `excluded` so the report cannot be mistaken for the whole
	register.

	REI IS THE LABEL FIGURE OFF THE APPLICATION, not a live window. Whether a
	block is closed right now is `get_active_rei`'s question and this is a
	historical report — the hours here say what the restriction WAS, which is
	what a records inspection asks about.
	"""
	_require(APPLICATION)
	company = resolve_company(as_str(args, "company", required=True), required=True)
	from_date, to_date, defaulted = _report_window(args)

	block_filter = as_str(args, "block") or as_str(args, "field")
	product_filter = as_str(args, "product") or as_str(args, "item_code")

	rows = (
		frappe.db.get_all(
			APPLICATION,
			filters={
				"company": company,
				"completed_at": ("between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"]),
			},
			fields=compat.existing_fields(APPLICATION, _APPLICATION_FIELDS),
			order_by="completed_at asc",
			limit=REPORT_CAP + 1,
		)
		or []
	)
	truncated = len(rows) > REPORT_CAP
	rows = [dict(row) for row in rows[:REPORT_CAP]]

	applied = [row for row in rows if str(row.get("status") or "") == APPLIED]
	excluded = {
		"planned": len([row for row in rows if str(row.get("status") or "") == PLANNED]),
		"cancelled": len([row for row in rows if str(row.get("status") or "") == CANCELLED]),
	}

	blocks_by_app = _blocks_by_application([str(row.get("name")) for row in applied])

	by_product: dict = {}
	by_block: dict = {}
	mixed_units: list = []
	no_uom: list = []
	unblocked: list = []
	counted_applications: set = set()

	for row in applied:
		name = str(row.get("name"))
		lines = _product_lines(row)
		if product_filter:
			lines = [line for line in lines if str(line.get("item") or "") == product_filter]
		if not lines:
			continue

		block_rows = blocks_by_app.get(name) or []
		if block_filter:
			block_rows = [entry for entry in block_rows if str(entry.get("block") or "") == block_filter]
			if not block_rows:
				continue

		# An application with no block rows still put product on the ground —
		# `total_acres` is what it says. It is reported against the sentinel
		# rather than dropped, because a use report that silently omitted it
		# would understate the total, and understating is the direction that
		# gets an operation in trouble.
		if not block_rows:
			block_rows = [{"block": None, "block_doctype": None, "acres": _number(row.get("total_acres"))}]
			unblocked.append(name)

		applicator = row.get("applicator") or None
		completed = str(row.get("completed_at") or "")
		date_only = completed[:10] or None

		for line in lines:
			product = str(line.get("item") or "")
			if not product:
				continue
			entry = by_product.setdefault(product, _blank_product(line))
			uom = str(line.get("rate_uom") or "").strip() or "(no unit recorded)"
			if uom == "(no unit recorded)" and name not in no_uom:
				no_uom.append(name)
			rate = _number(line.get("rate_per_acre"))
			if line.get("target") and line["target"] not in entry["targets"]:
				entry["targets"].append(line["target"])

			for block_row in block_rows:
				block = block_row.get("block")
				acres = _number(block_row.get("acres"))
				quantity = round(rate * acres, 4)

				entry["total_by_uom"][uom] = round(entry["total_by_uom"].get(uom, 0.0) + quantity, 4)
				entry["total_acres_treated"] = round(entry["total_acres_treated"] + acres, 3)

				key = str(block) if block else "(no block recorded)"
				cell = entry["blocks"].setdefault(
					key,
					{
						"block": block,
						"block_doctype": block_row.get("block_doctype"),
						"acres_treated": 0.0,
						"quantity_by_uom": {},
						"application_count": 0,
						"dates": [],
						"applicators": [],
					},
				)
				cell["acres_treated"] = round(cell["acres_treated"] + acres, 3)
				cell["quantity_by_uom"][uom] = round(cell["quantity_by_uom"].get(uom, 0.0) + quantity, 4)
				cell["application_count"] += 1
				if date_only and date_only not in cell["dates"]:
					cell["dates"].append(date_only)
				if applicator and applicator not in cell["applicators"]:
					cell["applicators"].append(applicator)

				block_entry = by_block.setdefault(
					key,
					{
						"block": block,
						"block_doctype": block_row.get("block_doctype"),
						"products": [],
						"acres_treated": 0.0,
						"application_count": 0,
					},
				)
				if product not in block_entry["products"]:
					block_entry["products"].append(product)
				block_entry["acres_treated"] = round(block_entry["acres_treated"] + acres, 3)

			entry["application_count"] += 1
			entry["applications"].append(
				{
					"application": name,
					"completed_at": completed or None,
					"date": date_only,
					"applicator": applicator,
					"applicator_license": row.get("applicator_license") or None,
					"rate_per_acre": rate,
					"rate_uom": line.get("rate_uom") or None,
					"blocks": [block_row.get("block") for block_row in block_rows],
					"acres": round(sum(_number(b.get("acres")) for b in block_rows), 3),
					"rei_hours": _number(line.get("rei_hours")) or None,
				}
			)
			if applicator and applicator not in entry["applicators"]:
				entry["applicators"].append(applicator)
			if date_only:
				if entry["first_application"] is None or date_only < entry["first_application"]:
					entry["first_application"] = date_only
				if entry["last_application"] is None or date_only > entry["last_application"]:
					entry["last_application"] = date_only
			counted_applications.add(name)

		for key in {str(b.get("block")) if b.get("block") else "(no block recorded)" for b in block_rows}:
			if key in by_block:
				by_block[key]["application_count"] += 1

	for product, entry in by_product.items():
		entry["blocks"] = dict(sorted(entry["blocks"].items()))
		entry["units"] = sorted(entry["total_by_uom"])
		if len(entry["units"]) > 1:
			mixed_units.append(product)

	products = [by_product[key] for key in sorted(by_product)]
	blocks = {key: by_block[key] for key in sorted(by_block)}

	notes = []
	if defaulted:
		notes.append(
			f"No dates were given, so the window is the calendar year to date — {from_date} to "
			f"{to_date}. A state use report is usually filed on the calendar year; a season that "
			"crosses new year needs the dates stated."
		)
	if excluded["planned"] or excluded["cancelled"]:
		notes.append(
			f"{excluded['planned']} planned and {excluded['cancelled']} cancelled application(s) "
			"in this window are NOT in these totals. A plan is not an application and a cancelled "
			"one did not happen."
		)
	if mixed_units:
		notes.append(
			f"{len(mixed_units)} product(s) were recorded in more than one unit and their totals "
			"are reported per unit rather than summed: "
			f"{', '.join(sorted(mixed_units))}. Adding pounds to quarts needs a density this app "
			"does not have, and a wrong total on a use report is found by an inspector."
		)
	if no_uom:
		notes.append(
			f"{len(no_uom)} application(s) carry a rate with no unit, so their quantity is "
			"totalled under '(no unit recorded)'. A regulator's form has a units column and this "
			"is the one number nobody can supply afterwards from memory."
		)
	if unblocked:
		notes.append(
			f"{len(unblocked)} application(s) name no block and are reported against '(no block "
			"recorded)' using their own total acres. The product total is right; the map is not, "
			"and a use report that cannot locate an application is one that gets sent back."
		)
	if truncated:
		notes.append(
			f"The read stopped at {REPORT_CAP} applications in this window. Narrow the dates — "
			"the totals above are of that subset only."
		)
	if not products:
		notes.append(
			"No applied spray applications matched. Check the window and the filters before "
			"reading this as a season with no spraying in it."
		)

	# The REI table is per product rather than per application, because the
	# question a records inspection asks — "what interval did this product
	# carry" — is a fact about the label and repeating it per row would invite
	# the reader to average it.
	rei_by_product = {
		entry["product"]: {"rei_hours": entry["rei_hours"], "phi_days": entry["phi_days"]}
		for entry in products
	}

	return ToolResult(
		data={
			"company": company,
			"date_from": from_date,
			"date_to": to_date,
			"window_defaulted": defaulted,
			"block": block_filter or None,
			"product": product_filter or None,
			"products": products,
			"product_count": len(products),
			"by_block": blocks,
			"block_count": len(blocks),
			"rei_by_product": rei_by_product,
			"application_count": len(counted_applications),
			"applications_in_window": len(rows),
			"excluded": excluded,
			"truncated": truncated,
			"mixed_unit_products": sorted(mixed_units),
			"applications_without_blocks": unblocked,
			"applications_without_units": no_uom,
			"notes": notes,
		},
		summary=(
			f"{len(products)} product(s) over {len(counted_applications)} applied application(s) "
			f"on {len(blocks)} block(s), {from_date} to {to_date}"
		),
	)
