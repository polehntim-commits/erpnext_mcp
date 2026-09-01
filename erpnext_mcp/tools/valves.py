# SPDX-License-Identifier: MIT
"""The valve a worker scans, and the one call that opens or shuts it.

WHY THERE IS NO `Irrigation Valve` DOCTYPE. A valve on this farm has been a
tagged asset since v0.25.0: it has a QR label whose payload is its docname, a
parent in the `location` tree, a state machine (`open`/`closed`/`winterized`), a
closing cascade down the line, and an Asset State Log that
`get_irrigation_runtime` has been summing into water minutes since v0.76.0. A
second table of valves would be a second account of the same pipe — two rows for
one gate, two states that disagree the first time somebody corrects one, and two
answers to "how long did zone 3 run" of which the wrong one is whichever a water
district happened to read. So this module adds no register. It adds the
vocabulary a valve screen needs on top of the register that is already there.

WHAT IS ACTUALLY NEW HERE, and it is the workflow rather than the model:

  * A TOGGLE. `log_asset_state_change` needs the caller to know the state before
    it can name the action — `open_valve` from `closed`, `close_valve` from
    `open` — and a worker holding a phone in front of a gate does not. So
    `toggle_irrigation_valve` reads the state and picks the action, which is one
    round trip instead of two and, more to the point, is the only way a scan can
    resolve to a button rather than to a menu.
  * THE VALVE'S OWN RANK. `Asset Register.valve_type` says Main, Sub-Main or
    Lateral. It is NOT the hierarchy — `location` is, and the cascade walks that
    — but it is what lets `create_irrigation_valve` refuse a Main filed
    underneath a Lateral, which is a data-entry error the cascade would
    otherwise honour by shutting the whole line from the wrong end.
  * A SCAN THAT ANSWERS THE VALVE QUESTION. `universal_scan` resolves any string
    against four registers and answers generically; `scan_valve_qr` resolves the
    same string, refuses anything that is not a valve BY NAMING WHAT IT IS, and
    answers with the three things a person standing at a gate asked: is it open,
    how long has it run today, and what does this button do.

CLOSING CASCADES AND OPENING DOES NOT, and this module does not change that.
Shutting a main stops the water below it for certain, so `close_valve` carries
downhill and every valve it reaches gets a real close with `cascaded_from` on it.
Opening a main only makes water AVAILABLE to what is below, each of which is open
or shut on its own account — so an opening cascade would mark every lateral on
the line as running and those events are the ones `get_water_usage_report` prices
into gallons. See `asset_tags._CASCADING_ACTIONS` for the argument in full. A
child closes on its own without touching its parent, because a lateral shut at
the row does not shut the turnout.

RUNTIME IS STILL THE LOG'S ANSWER AND NOT THIS MODULE'S. Every minute reported
here comes from `irrigation._runs_for`, the function that already handles the
four cases that make this harder than subtracting timestamps — a run that began
before the window, one that ended after it, one still open, and a close the
cascade wrote. `get_valve_runtime` is that measurement scoped to one valve's
subtree with its zone's total beside it; it is not a second sum.
"""

from __future__ import annotations

import frappe

from .. import compat, timezones
from ..args import as_bool, as_date, as_float, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import asset_tags, irrigation, universal_scan

ASSET_REGISTER = asset_tags.ASSET_REGISTER
ASSET_STATE_LOG = asset_tags.ASSET_STATE_LOG
IRRIGATION_ZONE = asset_tags.IRRIGATION_ZONE

#: The `asset_type` that makes a register row a valve. One string, named once,
#: because every filter and every refusal in this file turns on it.
VALVE = "Irrigation Valve"

#: What a valve is on its line, coarsest first. The order IS the rank — see
#: `_rank` — so a type inserted in the middle of this tuple changes what
#: `create_irrigation_valve` will accept as a parent without any other edit.
VALVE_TYPES = ("Main", "Sub-Main", "Lateral")

#: Spellings a caller may send for a valve type, resolved to the canonical one.
#: A handset sends what a person typed and an importer sends what a spreadsheet
#: had; refusing `submain` over a hyphen would be a refusal about punctuation.
_TYPE_ALIASES = {
	"main": "Main",
	"mainline": "Main",
	"sub-main": "Sub-Main",
	"sub main": "Sub-Main",
	"submain": "Sub-Main",
	"lateral": "Lateral",
	"row": "Lateral",
}

#: The two states a toggle moves between, and the action that makes each move.
#: `winterized` is deliberately not here: it is reached by `winterize` and left
#: by `reopen`, and a toggle that quietly un-winterized a line in February would
#: be a toggle nobody could trust in July.
_TOGGLE = {
	"closed": "open_valve",
	"open": "close_valve",
}

#: The state that means water is moving, borrowed from the module that does the
#: arithmetic rather than restated here — a valve is "running" in exactly the
#: sense `get_irrigation_runtime` counts.
RUNNING = irrigation.RUNNING

#: Most valves one list returns. Lower than the register's own 500 because this
#: list carries a state, a zone and a child count per row and is drawn on a
#: phone; a farm past this filters by zone, which is what the argument is for.
LIST_CAP = 200

#: How far up the line `_ancestry` walks before it stops. The same bound the
#: cascade uses walking down, for the same reason: a line nested deeper than
#: this is a miskeyed register rather than an orchard, and a worker at a gate is
#: not who should discover it.
ANCESTRY_DEPTH = asset_tags.CASCADE_DEPTH


def _require() -> None:
	asset_tags._require()
	compat.require_doctype(
		ASSET_STATE_LOG,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _rank(valve_type: str) -> int:
	"""How far down the line a rank sits. Unranked sorts last and blocks nothing."""
	try:
		return VALVE_TYPES.index(valve_type)
	except ValueError:
		return len(VALVE_TYPES)


def _as_valve_type(value: str, verb: str) -> str:
	"""One of Main, Sub-Main or Lateral, however the caller spelled it."""
	text = str(value or "").strip()
	if not text:
		return ""
	resolved = _TYPE_ALIASES.get(text.lower())
	if resolved:
		return resolved
	for known in VALVE_TYPES:
		if known.lower() == text.lower():
			return known
	raise ToolError(
		f"valve_type must be one of {', '.join(VALVE_TYPES)}, not {text!r}. It is the valve's "
		f"rank on the line — a Main at the turnout, a Sub-Main off it, a Lateral at the row. "
		f"Nothing was {verb}."
	)


def _state_of(row: dict) -> str:
	"""This valve's state, or the state machine's default where none is stored.

	A valve registered and never touched has an empty `current_state` column and
	is nonetheless CLOSED — that is what the machine's default means, and it is
	the same resolution `get_available_actions` and the cascade both make. A tool
	that reported it as unknown would put a third state on a screen that has two.
	"""
	stored = asset_tags._current_state_value(row.get("current_state"))
	return stored or asset_tags._STATE_DEFINITIONS[VALVE]["default"]


def _valve_row(name: str, company: str = "", verb: str = "read") -> dict:
	"""One Asset Register row, checked to be a valve.

	THE REFUSAL NAMES WHAT THE ASSET ACTUALLY IS. A tractor's tag scanned at a
	valve screen is an ordinary mistake in an orchard, and "not a valve" sends
	the worker back to a menu with nothing learned; "MC-Tractor-02 is a Tractor"
	tells them which screen they wanted.

	RESOLVED THROUGH `asset_row` AND THEN RE-READ. That function does the partial
	-match and the company check every asset tool gets, and it reads the
	register's own field list — which does not carry `valve_type` or
	`installed_date`, because those columns mean nothing on a tractor. So the row
	it returns is the one that proves the valve exists and belongs here, and this
	second read is the one the valve tools actually work from.
	"""
	row = asset_tags.asset_row(name, company or "")
	asset_type = str(row.get("asset_type") or "")
	if asset_type != VALVE:
		raise ToolError(
			f"{row['name']} is a {asset_type or 'untyped asset'}, not an {VALVE}. "
			f"list_irrigation_valves has the valves; get_asset_detail reads any asset. "
			f"Nothing was {verb}."
		)
	full = frappe.db.get_value(ASSET_REGISTER, row["name"], _fields(), as_dict=True)
	return {**row, **dict(full or {})}


def _zone_block(zone_name: str) -> dict | None:
	"""The Irrigation Zone record behind a valve, or None where none is linked.

	None rather than an empty shell, because "this valve draws through zone 4"
	and "nobody has mapped this valve" are different facts and a water report
	already distinguishes them (`unpriced_valves`). A screen that got zeros for
	the second would show a flow rate of nothing as though it were measured.
	"""
	zone_name = str(zone_name or "")
	if not zone_name or not compat.doctype_exists(IRRIGATION_ZONE):
		return None
	fields = compat.existing_fields(
		IRRIGATION_ZONE,
		("name", "zone_name", "flow_rate_gpm", "area_acres", "field", "water_right_id", "owning_entity"),
	)
	row = frappe.db.get_value(IRRIGATION_ZONE, zone_name, fields, as_dict=True)
	if not row:
		return None
	row = dict(row)
	return {
		"name": row.get("name"),
		"zone_name": row.get("zone_name") or row.get("name"),
		"flow_rate_gpm": round(float(row.get("flow_rate_gpm") or 0), 2) or None,
		"area_acres": round(float(row.get("area_acres") or 0), 3) or None,
		"block": row.get("field") or None,
		"water_right_id": row.get("water_right_id") or None,
		"owning_entity": row.get("owning_entity") or None,
	}


def _describe(row: dict) -> dict:
	"""One valve as a screen reads it.

	`valve_id` and `name` are the SAME STRING and both are returned. The docname
	is the printable tag ID — that is the whole design of the asset register —
	and a client that asked for a valve by `valve_id` should not have to learn
	that the answer calls it something else.
	"""
	state = _state_of(row)
	return {
		"name": row.get("name"),
		"valve_id": row.get("name"),
		"valve_type": row.get("valve_type") or None,
		"state": state,
		"is_open": state == RUNNING,
		"company": row.get("company") or None,
		"zone": row.get("irrigation_zone") or None,
		"parent_valve": row.get("location") or None,
		"qr_code": row.get("qr_url") or None,
		"nfc_uid": row.get("nfc_uid") or None,
		"installed_date": str(row.get("installed_date") or "") or None,
		"last_state_change": str(row.get("last_state_change") or "") or None,
		"last_scan_at": str(row.get("last_scan_at") or "") or None,
		"last_scan_by": row.get("last_scan_by") or None,
		"description": row.get("description") or None,
		"gps_latitude": round(float(row.get("gps_latitude") or 0), 7) or None,
		"gps_longitude": round(float(row.get("gps_longitude") or 0), 7) or None,
		"retired": bool(row.get("retired_at")),
		"retired_at": str(row.get("retired_at") or "") or None,
	}


_VALVE_FIELDS = (
	"name",
	"asset_type",
	"company",
	"location",
	"irrigation_zone",
	"valve_type",
	"installed_date",
	"current_state",
	"last_state_change",
	"qr_url",
	"nfc_uid",
	"last_scan_at",
	"last_scan_by",
	"description",
	"gps_latitude",
	"gps_longitude",
	"retired_at",
)


def _fields() -> list:
	return compat.existing_fields(ASSET_REGISTER, _VALVE_FIELDS)


def _today_window() -> tuple[str, str]:
	today = str(frappe.utils.today())
	return f"{today} 00:00:00", f"{today} 23:59:59"


def _runtime_today(row: dict) -> dict:
	"""Minutes on this valve and everything below it, since midnight.

	SUBTREE AND NOT JUST THE VALVE, because that is the number that matches what
	shutting this valve would stop. A main with four laterals running under it
	has not itself been open for four times as long — so both figures come back,
	`minutes` for the valve in front of the worker and `subtree_minutes` for the
	line it commands.

	THE TWO TOTALS STAY APART here as they do upstream: finished runs do not move
	between two identical calls and a run still going does. See
	`irrigation._runs_for`.
	"""
	opened, closed = _today_window()
	now = str(frappe.utils.now())

	own = irrigation._runs_for(row, opened, closed, now)
	subtree = [own]
	for valve in irrigation._valves(row):
		if str(valve.get("name")) != str(row.get("name")):
			subtree.append(irrigation._runs_for(valve, opened, closed, now))

	minutes = round(sum(entry["runtime_minutes"] for entry in subtree), 1)
	open_minutes = round(sum(entry["open_run_minutes"] for entry in subtree), 1)
	return {
		"date": opened[:10],
		"minutes": own["runtime_minutes"],
		"hours": round(own["runtime_minutes"] / 60.0, 2),
		"open_run_minutes": own["open_run_minutes"],
		"run_count": own["run_count"],
		"still_open": own["still_open"],
		"open_since": own["open_since"],
		"subtree_valve_count": len(subtree),
		"subtree_minutes": minutes,
		"subtree_hours": round(minutes / 60.0, 2),
		"subtree_open_run_minutes": open_minutes,
	}


def _ancestry(row: dict) -> list:
	"""The valves this one hangs off, nearest first, up to the first non-valve.

	STOPS AT THE FIRST THING THAT IS NOT A VALVE, which is usually the zone or
	block asset the line sits under. Walking past it would report a ranch as a
	valve's parent, and the question this answers — "what would somebody have to
	shut to dry this out" — has the same answer either way.

	Cycle-safe for `_descendants`' reason: nothing in the register refuses
	A → B → A, and a walk that trusted the tree would spin.
	"""
	chain: list = []
	seen = {str(row.get("name"))}
	current = str(row.get("location") or "")
	fields = _fields()

	for _ in range(ANCESTRY_DEPTH):
		if not current or current in seen:
			break
		seen.add(current)
		parent = frappe.db.get_value(ASSET_REGISTER, current, fields, as_dict=True)
		if not parent:
			break
		parent = dict(parent)
		if str(parent.get("asset_type") or "") != VALVE:
			break
		chain.append(
			{
				"name": parent.get("name"),
				"valve_type": parent.get("valve_type") or None,
				"state": _state_of(parent),
				"zone": parent.get("irrigation_zone") or None,
			}
		)
		current = str(parent.get("location") or "")
	return chain


def _children(name: str) -> list:
	"""The valves hanging directly off this one."""
	rows = (
		frappe.db.get_all(
			ASSET_REGISTER,
			filters={"location": name, "asset_type": VALVE},
			fields=_fields(),
			order_by="name asc",
			limit=LIST_CAP,
		)
		or []
	)
	return [
		{
			"name": child.get("name"),
			"valve_type": child.get("valve_type") or None,
			"state": _state_of(dict(child)),
			"zone": child.get("irrigation_zone") or None,
			"retired": bool(child.get("retired_at")),
		}
		for child in (dict(row) for row in rows)
	]


def _next_action(state: str) -> dict:
	"""What one press of the toggle would do, from here.

	A SHAPE THAT IS ALWAYS PRESENT, including where the answer is "nothing". A
	winterized valve has no toggle, and a client that had to test for a missing
	key would draw a button for it — so `action` comes back null with a sentence
	saying which call un-winterizes it.
	"""
	action = _TOGGLE.get(state)
	if not action:
		available = [entry["action"] for entry in asset_tags._actions_for(VALVE, state)]
		return {
			"action": None,
			"to_state": None,
			"cascades": False,
			"note": (
				f"A valve in state {state!r} does not toggle. "
				+ (
					f"Available actions: {', '.join(available)} — send one through log_asset_state_change."
					if available
					else "It has no available action at all, which means the register holds a "
					"state this valve's machine does not define."
				)
			),
		}
	cascades = action in asset_tags._CASCADING_ACTIONS.get(VALVE, ())
	return {
		"action": action,
		"to_state": asset_tags._STATE_DEFINITIONS[VALVE]["actions"][action]["to"],
		"cascades": cascades,
		"note": (
			"Closing this valve closes every valve below it on the line — no water can reach "
			"them once it is shut."
			if cascades
			else "Opening this valve opens only this valve. Water becomes available to what is "
			"below it; each of those is opened on its own account."
		),
	}


def _status(row: dict, args: dict) -> dict:
	"""Everything a valve screen draws, from one register row.

	Shared by `get_irrigation_valve`, `toggle_irrigation_valve` and
	`scan_valve_qr` so the three cannot drift into three shapes of the same
	answer — a handset that renders a scan renders a toggle's result.
	"""
	described = _describe(row)
	state = described["state"]
	clock = timezones.Renderer(args)

	data = {
		**described,
		"zone_detail": _zone_block(row.get("irrigation_zone")),
		"parent_chain": _ancestry(row),
		"children": _children(str(row["name"])),
		"runtime_today": _runtime_today(row),
		"available_actions": asset_tags._actions_for(VALVE, state),
		"next_action": _next_action(state),
		**clock.block(),
	}
	data["child_count"] = len(data["children"])
	data["children_open"] = [child["name"] for child in data["children"] if child["state"] == RUNNING]
	clock.add(data, "last_state_change", "last_scan_at")
	clock.add(data["runtime_today"], "open_since")
	return data


# ── create_irrigation_valve ─────────────────────────────────────────────────
def create_irrigation_valve(args: dict) -> ToolResult:
	"""Register one valve on a line, under its zone and its parent valve.

	`register_asset` DOES THE WRITING. This is that call with the valve's own
	three refusals in front of it — a rank this app does not have, a parent that
	is not a valve, and a parent further down the line than its child — and with
	`asset_type` fixed rather than asked for. Re-implementing the insert would be
	a second way to put a row in the register with one fewer check on it.

	THE PARENT IS THE HIERARCHY AND THE RANK IS NOT. `parent_valve` writes
	`location`, which is the column the closing cascade walks; `valve_type` is
	what a worker calls the thing. They are checked against each other exactly
	once, here, because that is the only moment somebody states both.
	"""
	_require()
	company = resolve_company(as_str(args, "company"), required=True)
	valve_id = as_str(args, "valve_id") or as_str(args, "name", required=True)
	valve_type = _as_valve_type(as_str(args, "valve_type", required=True), "created")

	parent = as_str(args, "parent_valve") or as_str(args, "parent_asset") or as_str(args, "location")
	parent_row: dict = {}
	if parent:
		parent_row = _valve_row(parent, company or "", verb="created")
		parent = str(parent_row["name"])
		parent_rank = _rank(str(parent_row.get("valve_type") or ""))
		if parent_rank > _rank(valve_type):
			raise ToolError(
				f"{parent} is a {parent_row.get('valve_type')} and cannot be the parent of a "
				f"{valve_type}: water runs Main → Sub-Main → Lateral, and the closing cascade "
				f"walks that direction. If the line really is plumbed this way, correct the "
				f"valve_type on {parent} first. Nothing was created."
			)

	# THE ZONE IS INHERITED ONLY FROM A PARENT THAT HAS ONE, AND THE ANSWER SAYS
	# SO. A lateral draws through whatever its sub-main draws through — that is
	# what being downstream means — so requiring it to be restated is data entry
	# that invites a typo. Inheriting from anywhere ELSE would be a guess, and
	# this column is what `get_water_usage_report` prices gallons with.
	zone = as_str(args, "zone") or as_str(args, "irrigation_zone")
	zone_source = "the zone argument"
	if not zone and parent_row.get("irrigation_zone"):
		zone = str(parent_row["irrigation_zone"])
		zone_source = f"inherited from parent valve {parent}"
	if not zone:
		raise ToolError(
			"zone is required: it is the Irrigation Zone this valve draws through, and it is "
			"the only link this app has between a valve and a flow rate — without it "
			"get_water_usage_report counts this valve's minutes and can price none of them. "
			"Pass zone, or give a parent_valve that already has one. "
			"list_irrigation_zones has the register. Nothing was created."
		)
	if not compat.doctype_exists(IRRIGATION_ZONE) or not frappe.db.exists(IRRIGATION_ZONE, zone):
		raise ToolError(
			f"no {IRRIGATION_ZONE} called {zone!r} on this site. list_irrigation_zones has the "
			"register. Nothing was created."
		)
	owner = frappe.db.get_value(IRRIGATION_ZONE, zone, "owning_entity")
	if owner and company and str(owner) != company:
		raise ToolError(
			f"Irrigation zone {zone!r} belongs to {owner!r}, not {company!r}. A valve and the "
			"zone it draws through are the same entity's water. Nothing was created."
		)

	installed = as_date(args, "installed_date")

	inner = {
		"name": valve_id,
		"asset_type": VALVE,
		"company": company,
		"irrigation_zone": zone,
		"description": as_str(args, "description"),
		"nfc_uid": as_str(args, "nfc_uid"),
	}
	if parent:
		inner["location"] = parent
	for key in ("gps_latitude", "gps_longitude", "serial_number", "model", "acquired_on"):
		if args.get(key) is not None:
			inner[key] = args[key]

	created = asset_tags.register_asset(inner).data

	# The two columns `register_asset` does not know about, written straight
	# rather than through a second `save`: neither is validated by the controller
	# and re-saving the document would fire `validate` twice on one insert.
	extra = {}
	if compat.has_field(ASSET_REGISTER, "valve_type"):
		extra["valve_type"] = valve_type
	if installed and compat.has_field(ASSET_REGISTER, "installed_date"):
		extra["installed_date"] = installed
	if extra:
		frappe.db.set_value(ASSET_REGISTER, created["name"], extra, update_modified=False)

	row = dict(frappe.db.get_value(ASSET_REGISTER, created["name"], _fields(), as_dict=True) or {})
	described = _describe(row)
	described["zone_source"] = zone_source
	described["zone_detail"] = _zone_block(zone)
	described["parent_chain"] = _ancestry(row)
	# STATE IS NOT WRITTEN AT REGISTRATION and the answer says which it is. A new
	# valve has an empty `current_state` and is closed, because that is the
	# machine's default; writing `{"state":"closed"}` here would put a state on
	# the register that no Asset State Log row accounts for, and runtime is
	# summed from those rows.
	described["state_source"] = (
		"the Irrigation Valve state machine's default — no state change has been logged on this valve yet"
	)
	described["next_action"] = _next_action(described["state"])

	return ToolResult(
		data=described,
		summary=(
			f"registered valve {row['name']} ({valve_type}) on zone {zone}"
			+ (f", under {parent}" if parent else ", at the head of its line")
		),
		docstatus_delta="none → 0 (created)",
	)


# ── update_irrigation_valve ─────────────────────────────────────────────────
def update_irrigation_valve(args: dict) -> ToolResult:
	"""Correct a valve's record in place: its rank, its parent, its zone, where it is.

	THE TAG IS THE ONE THING THAT DOES NOT MOVE. The docname IS the printable ID
	— it is on the label, it is the QR payload, and it is the string every Asset
	State Log row naming this valve's runtime carries — so a rename is refused
	rather than performed. Everything a person can get wrong ABOUT a valve is
	editable here; what the valve is CALLED is settled the moment the tag is
	printed. A valve that needs a different ID is a new tag and a retirement, not
	an edit.

	THE SAME THREE REFUSALS `create_irrigation_valve` MAKES, made again, because
	an edit can put the register into exactly the state the create refused to
	create: a Main filed underneath a Lateral, a parent that is not a valve, and
	a zone that does not exist or belongs to another entity. A check that only
	ran at insert would be a check anybody could get around by creating the valve
	correctly and then editing it.

	AND ONE REFUSAL CREATE CANNOT MAKE, because a new valve has nothing under it:
	the rank is checked DOWNWARDS TOO. Demoting a Main to a Lateral while a
	Sub-Main still hangs off it produces the same upside-down line the create
	refuses — from the other end — and the closing cascade would honour it. The
	children are only consulted when `valve_type` is what changed; moving a valve
	to a different parent does not alter what its own children are.

	A LOOP IS REFUSED AHEAD OF THE CASCADE THAT WOULD WALK IT. `location` is the
	tree `close_valve` descends, and nothing in the register refuses A → B → A —
	`_descendants` and `_ancestry` are both written to survive one because the
	data can contain one. Filing a valve underneath its own descendant is the
	edit that creates one, and it is the only place this tool can catch it.

	AN UNRANKED PARENT BLOCKS NOTHING HERE, which is a deliberate difference from
	`create_irrigation_valve`. A valve registered through `register_asset` has no
	`valve_type` at all, and `_rank` sorts it last — so the create's check refuses
	to file anything under it. This tool is how that valve gets its rank, and it
	must not refuse an unrelated edit (a GPS fix, a description) on the grounds
	of a rank nobody has stated yet. A parent that HAS a rank is checked exactly
	as the create checks it.

	A RETIRED VALVE IS STILL EDITABLE, unlike a toggled one. Retirement says the
	valve is not operated; it does not say the record about it was right. The
	`retired` flag comes back on the answer, so a client that wants to warn can.
	"""
	_require()
	company = resolve_company(as_str(args, "company"))
	name = as_str(args, "name") or as_str(args, "valve") or as_str(args, "valve_id", required=True)
	row = _valve_row(name, company or "", verb="changed")

	# THE RENAME IS CAUGHT BY NAME RATHER THAN IGNORED. A caller that sends
	# `new_valve_id` has stated an intention, and a tool that silently dropped
	# the key would report "3 field(s) changed" over an edit that did not do the
	# thing the caller asked for. `valve_id` disagreeing with `name` is the same
	# request spelled by a client that used one key to find the valve and the
	# other to rename it.
	renamed = ""
	for key in ("new_name", "new_valve_id", "rename_to"):
		renamed = as_str(args, key)
		if renamed:
			break
	if not renamed and as_str(args, "name") and as_str(args, "valve_id"):
		stated = as_str(args, "valve_id")
		if stated != str(row["name"]):
			renamed = stated
	if renamed:
		raise ToolError(
			f"a valve cannot be renamed from {row['name']!r} to {renamed!r}. The docname IS the "
			f"tag ID — it is printed on the label, it is what the QR encodes, and it is the "
			f"string every Asset State Log row carrying this valve's runtime names. Renaming the "
			f"record would leave the tag in the orchard pointing at a valve that is no longer "
			f"there. Print the new tag, register it with create_irrigation_valve, and retire this "
			f"one with retire_asset. Nothing was changed."
		)

	doc = frappe.get_doc(ASSET_REGISTER, row["name"])
	changes: dict = {}

	# ── the rank, and the two directions it is checked in ──────────────────
	retype = "valve_type" in args
	valve_type = str(row.get("valve_type") or "")
	if retype:
		stated = as_str(args, "valve_type")
		if not stated:
			raise ToolError(
				"valve_type cannot be cleared: it is the valve's rank on the line — Main at the "
				"turnout, Sub-Main off it, Lateral at the row — and an unranked valve is one "
				"create_irrigation_valve will not file anything underneath. Pass one of "
				f"{', '.join(VALVE_TYPES)}. Nothing was changed."
			)
		valve_type = _as_valve_type(stated, "changed")

	reparent = any(key in args for key in ("parent_valve", "parent_asset", "location"))
	parent_row: dict = {}
	if reparent:
		parent = as_str(args, "parent_valve") or as_str(args, "parent_asset") or as_str(args, "location")
		if parent:
			parent_row = _valve_row(parent, company or "", verb="changed")
			parent = str(parent_row["name"])
			if parent == str(row["name"]):
				raise ToolError(
					f"{row['name']} cannot be its own parent. A valve at the head of its line has "
					"no parent_valve at all — pass an empty one. Nothing was changed."
				)
			descendants, _truncated = asset_tags._descendants(str(row["name"]))
			below = {str(entry.get("name") or "") for entry in descendants}
			if parent in below:
				raise ToolError(
					f"{parent} already hangs below {row['name']} on this line, so filing "
					f"{row['name']} underneath it would close the tree into a loop — and "
					f"`location` is the tree a close_valve cascade walks downwards. Move {parent} "
					f"out from under {row['name']} first. Nothing was changed."
				)
	else:
		# The parent it already has, read only far enough to rank it. A valve at
		# the head of its line sits under the block or ranch asset rather than
		# under another valve, which `_ancestry` stops at for the same reason:
		# a ranch has no rank and ranks nothing.
		parent = str(row.get("location") or "")
		if parent:
			existing = dict(frappe.db.get_value(ASSET_REGISTER, parent, _fields(), as_dict=True) or {})
			if str(existing.get("asset_type") or "") == VALVE:
				parent_row = existing

	if retype or reparent:
		parent_type = str(parent_row.get("valve_type") or "")
		if parent_type and _rank(parent_type) > _rank(valve_type):
			raise ToolError(
				f"{parent_row['name']} is a {parent_type} and cannot be the parent of a "
				f"{valve_type}: water runs {' → '.join(VALVE_TYPES)}, and the closing cascade "
				f"walks that direction. If the line really is plumbed this way, correct the "
				f"valve_type on {parent_row['name']} first. Nothing was changed."
			)

	if retype:
		for child in _children(str(row["name"])):
			if child.get("retired"):
				continue
			child_type = str(child.get("valve_type") or "")
			if child_type and _rank(child_type) < _rank(valve_type):
				raise ToolError(
					f"{child['name']} is a {child_type} hanging off {row['name']}, which cannot "
					f"be a {valve_type}: that would put a {child_type} below a {valve_type} and "
					f"the closing cascade would shut the line from the wrong end. Re-rank "
					f"{child['name']}, or move it, before re-ranking {row['name']}. "
					f"Nothing was changed."
				)

	# ── the zone, which is the only link to a flow rate ────────────────────
	rezone = "zone" in args or "irrigation_zone" in args
	if rezone:
		zone = as_str(args, "zone") or as_str(args, "irrigation_zone")
		if not zone:
			raise ToolError(
				"zone cannot be cleared: it is the Irrigation Zone this valve draws through and "
				"the only link this app has between a valve and a flow rate — without it "
				"get_water_usage_report counts this valve's minutes and can price none of them, "
				"and names it in unpriced_valves. Pass the zone it draws through instead. "
				"Nothing was changed."
			)
		if not compat.doctype_exists(IRRIGATION_ZONE) or not frappe.db.exists(IRRIGATION_ZONE, zone):
			raise ToolError(
				f"no {IRRIGATION_ZONE} called {zone!r} on this site. list_irrigation_zones has the "
				"register. Nothing was changed."
			)
		# CHECKED AGAINST THE VALVE'S OWN COMPANY and not against the argument.
		# `company` is optional here — it narrows which valve the name resolves
		# to — so on a call that omitted it the argument is None and a check
		# against it would pass anything. The register row states the owner.
		owner = frappe.db.get_value(IRRIGATION_ZONE, zone, "owning_entity")
		holder = str(row.get("company") or "") or (company or "")
		if owner and holder and str(owner) != holder:
			raise ToolError(
				f"Irrigation zone {zone!r} belongs to {owner!r}, not {holder!r}. A valve and the "
				"zone it draws through are the same entity's water. Nothing was changed."
			)
		asset_tags._stage(changes, doc, "irrigation_zone", zone)

	# ── what is written ────────────────────────────────────────────────────
	if retype and compat.has_field(ASSET_REGISTER, "valve_type"):
		asset_tags._stage(changes, doc, "valve_type", valve_type)
	if reparent:
		asset_tags._stage(changes, doc, "location", parent or None)
	for key in ("description", "nfc_uid"):
		if key in args:
			asset_tags._stage(changes, doc, key, as_str(args, key))
	if "installed_date" in args and compat.has_field(ASSET_REGISTER, "installed_date"):
		asset_tags._stage(changes, doc, "installed_date", as_date(args, "installed_date"))
	# A NULL FIX CLEARS THE COLUMN RATHER THAN WRITING 0. `as_float(None)` is
	# 0.0, and 0.0/0.0 is a real coordinate in the Gulf of Guinea — a valve whose
	# position was cleared must not come back onto a map off the coast of Africa.
	for stored, alias in (("gps_latitude", "gps_lat"), ("gps_longitude", "gps_lon")):
		if stored not in args and alias not in args:
			continue
		value = args.get(stored) if stored in args else args.get(alias)
		if value is None or value == "":
			asset_tags._stage(changes, doc, stored, None)
		else:
			asset_tags._stage(changes, doc, stored, as_float(value, stored))

	if not changes:
		raise ToolError(
			f"nothing to change on {row['name']}. Pass at least one of: valve_type, parent_valve, "
			"zone, description, nfc_uid, installed_date, gps_latitude, gps_longitude. The tag ID "
			"is the docname and cannot be changed at all."
		)

	doc.save(ignore_permissions=True)

	after = dict(frappe.db.get_value(ASSET_REGISTER, doc.name, _fields(), as_dict=True) or {})
	data = _status(after, args)
	data["changed"] = {key: [before, value] for key, (before, value) in changes.items()}

	return ToolResult(
		data=data,
		summary=(f"{doc.name}: {len(changes)} field(s) changed — {', '.join(sorted(changes))}"),
		docstatus_delta="0 → 0 (updated)",
	)


# ── list_irrigation_valves ──────────────────────────────────────────────────
def list_irrigation_valves(args: dict) -> ToolResult:
	"""Every valve, or every valve on one zone, with what each is doing now.

	THE STATE FILTER IS APPLIED IN PYTHON AND THE ROW CAP IS NOT. `current_state`
	is a JSON column and a valve that has never been touched has nothing in it
	while still being closed — so a SQL filter would silently drop exactly the
	valves nobody has scanned yet, which on a new install is all of them. The
	rows are read under the register's filters and the state is resolved per row,
	the same resolution the cascade makes.
	"""
	_require()
	company = resolve_company(as_str(args, "company"))
	limit = min(as_limit(args), LIST_CAP)

	filters: dict = {"asset_type": VALVE}
	if company:
		filters["company"] = company
	zone = as_str(args, "zone") or as_str(args, "irrigation_zone")
	if zone:
		filters["irrigation_zone"] = zone
	parent = as_str(args, "parent_valve") or as_str(args, "location")
	if parent:
		filters["location"] = parent

	valve_type = as_str(args, "valve_type")
	if valve_type:
		valve_type = _as_valve_type(valve_type, "listed")
		if compat.has_field(ASSET_REGISTER, "valve_type"):
			filters["valve_type"] = valve_type

	retired = as_bool(args, "retired")
	if retired is True:
		filters["retired_at"] = ("is", "set")
	elif retired is not None or as_bool(args, "include_retired") is not True:
		filters["retired_at"] = ("is", "not set")

	rows = (
		frappe.db.get_all(
			ASSET_REGISTER,
			filters=filters,
			fields=_fields(),
			order_by="irrigation_zone asc, name asc",
			limit=LIST_CAP + 1,
		)
		or []
	)
	truncated = len(rows) > LIST_CAP
	valves = [_describe(dict(row)) for row in rows[:LIST_CAP]]

	wanted = as_str(args, "state")
	if wanted:
		state = wanted.strip().lower()
		known = sorted(
			{rule["to"] for rule in asset_tags._STATE_DEFINITIONS[VALVE]["actions"].values()}
			| {asset_tags._STATE_DEFINITIONS[VALVE]["default"]}
		)
		if state not in known:
			raise ToolError(f"state must be one of {', '.join(known)}, not {wanted!r}. Nothing was listed.")
		valves = [valve for valve in valves if valve["state"] == state]

	# One query for every child count rather than one per valve: a turnout with
	# forty laterals is ordinary and forty round trips behind a list screen is
	# not. Bounded by the same cap the list itself is.
	names = [valve["name"] for valve in valves]
	children: dict = {}
	if names:
		for child in (
			frappe.db.get_all(
				ASSET_REGISTER,
				filters={"location": ("in", names), "asset_type": VALVE, "retired_at": ("is", "not set")},
				fields=["name", "location"],
				limit=LIST_CAP * 4,
			)
			or []
		):
			key = str(dict(child).get("location") or "")
			children[key] = children.get(key, 0) + 1
	for valve in valves:
		valve["child_count"] = children.get(valve["name"], 0)

	clock = timezones.Renderer(args)
	for valve in valves:
		clock.add(valve, "last_state_change", "last_scan_at")

	by_state: dict = {}
	by_type: dict = {}
	for valve in valves:
		by_state[valve["state"]] = by_state.get(valve["state"], 0) + 1
		key = valve["valve_type"] or "(unranked)"
		by_type[key] = by_type.get(key, 0) + 1

	open_now = [valve["name"] for valve in valves if valve["is_open"]]
	data = {
		"company": company,
		"zone": zone or None,
		"parent_valve": parent or None,
		"valve_type": valve_type or None,
		"state": wanted or None,
		"valve_count": len(valves),
		"by_state": dict(sorted(by_state.items())),
		"by_valve_type": dict(sorted(by_type.items())),
		"open_now": open_now,
		"open_count": len(open_now),
		"valves": valves,
		"limit": limit,
		"truncated": truncated,
		**clock.block(),
	}
	if truncated:
		data["truncated_note"] = (
			f"more than {LIST_CAP} valves matched and the list stops there. Narrow it with "
			"zone, parent_valve or valve_type — a total drawn from a silently shortened list "
			"is the wrong number to act on."
		)

	return ToolResult(
		data=data,
		summary=(
			f"{len(valves)} valve(s)"
			+ (f" on zone {zone}" if zone else "")
			+ (f", {len(open_now)} open" if open_now else ", none open")
		),
	)


# ── get_irrigation_valve ────────────────────────────────────────────────────
def get_irrigation_valve(args: dict) -> ToolResult:
	"""One valve in full: state, zone, the line above it, what is below, today's runtime."""
	_require()
	company = resolve_company(as_str(args, "company"))
	name = as_str(args, "name") or as_str(args, "valve") or as_str(args, "valve_id", required=True)
	row = _valve_row(name, company or "")

	data = _status(row, args)
	runtime = data["runtime_today"]
	return ToolResult(
		data=data,
		summary=(
			f"{row['name']} ({data['valve_type'] or 'unranked'}): {data['state']}, "
			f"{runtime['hours']} h today"
			+ (
				f", {len(data['children_open'])} of {data['child_count']} children open"
				if data["child_count"]
				else ""
			)
		),
	)


# ── toggle_irrigation_valve ─────────────────────────────────────────────────
def toggle_irrigation_valve(args: dict) -> ToolResult:
	"""Open a closed valve, close an open one. Closing carries down the line.

	THE STATE IS READ HERE SO THE WORKER DOES NOT HAVE TO. `log_asset_state_change`
	takes an action name and validates it against the machine, which means a
	caller must already know whether the gate is open before it can say
	`close_valve`. A person standing in front of it can see that; a phone that
	has just read a QR cannot, and asking would be a second round trip in the
	moment somebody is holding a wrench.

	`log_asset_state_change` STILL DOES THE WRITING, cascade included. What this
	adds is the choice of action and the runtime beside the result — the two
	things a screen needs and neither of which is a second way to change a state.

	AN ALREADY-OPEN VALVE OPENED AGAIN IS NOT A NO-OP HERE, IT IS A CLOSE. That
	is what a toggle means, and it is why this tool exists rather than an
	`open_irrigation_valve` pair: a button whose label depends on state must
	resolve the state at the moment it is pressed, not at the moment it was
	drawn. `expect_state` is there for a caller that wants the other guarantee.
	"""
	_require()
	company = resolve_company(as_str(args, "company"))
	name = as_str(args, "name") or as_str(args, "valve") or as_str(args, "valve_id", required=True)
	row = _valve_row(name, company or "", verb="changed")

	if row.get("retired_at"):
		raise ToolError(
			f"{row['name']} was retired on {row['retired_at']}. A retired valve keeps its tag "
			"and its history and is not operated. Nothing was changed."
		)

	before = _state_of(row)
	action = _TOGGLE.get(before)
	if not action:
		available = [entry["action"] for entry in asset_tags._actions_for(VALVE, before)]
		raise ToolError(
			f"{row['name']} is {before!r} and a toggle moves a valve between open and closed "
			f"only. "
			+ (
				f"Send {', '.join(available)} through log_asset_state_change — un-winterizing a "
				"line in the middle of a season is a decision somebody makes deliberately, not "
				"one a toggle makes for them. "
				if available
				else ""
			)
			+ "Nothing was changed."
		)

	# A CALLER MAY STATE WHAT IT BELIEVED. A screen drawn a minute ago and pressed
	# now may be acting on a state somebody else has already changed — and on a
	# valve the wrong way round is the difference between watering a block and
	# drying it out. Optional, because the handset flow that scans and toggles in
	# one gesture has no stale reading to guard against.
	expected = as_str(args, "expect_state")
	if expected and expected.strip().lower() != before:
		raise ToolError(
			f"{row['name']} is {before!r}, not {expected.strip().lower()!r} — somebody has "
			f"changed it since this was read. A toggle from here would {_TOGGLE[before]}. "
			"Re-read it with get_irrigation_valve and press again if that is still what you "
			"want. Nothing was changed."
		)

	inner = {"asset_name": row["name"], "action": action}
	for key in ("performed_by", "notes", "photo_file_token", "gps_lat", "gps_lon", "timezone"):
		if args.get(key) is not None:
			inner[key] = args[key]
	changed = asset_tags.log_asset_state_change(inner).data

	after = dict(frappe.db.get_value(ASSET_REGISTER, row["name"], _fields(), as_dict=True) or {})
	data = {
		**_status(after, args),
		"action": action,
		"from_state": changed.get("from_state"),
		"to_state": changed.get("to_state"),
		"log_name": changed.get("log_name"),
		"performed_by": changed.get("performed_by"),
		"performed_at": changed.get("performed_at"),
		"performed_at_local": changed.get("performed_at_local"),
		"cascaded": changed.get("cascaded") or [],
		"cascaded_count": changed.get("cascaded_count") or 0,
		"cascade_skipped": changed.get("cascade_skipped") or [],
		"cascade_truncated": bool(changed.get("cascade_truncated")),
	}

	summary = f"{row['name']}: {before} → {data['to_state']}"
	if data["cascaded_count"]:
		summary += f", closed {data['cascaded_count']} valve(s) downstream"
	return ToolResult(data=data, summary=summary, docstatus_delta="0 → 0 (updated)")


# ── get_valve_runtime ───────────────────────────────────────────────────────
def get_valve_runtime(args: dict) -> ToolResult:
	"""Hours this valve ran over a window, and what its whole zone ran beside it.

	`get_irrigation_runtime`'S MEASUREMENT, NOT A SECOND ONE. The valve figure is
	that tool called on this valve, which walks its subtree and handles the four
	edge cases the module docstring in `irrigation.py` sets out. The zone figure
	is the same `_runs_for` over every valve the zone links, so the two can be
	read against each other — a lateral that ran three hours on a zone that ran
	forty is a sentence somebody can act on, and two separately-derived numbers
	would eventually disagree about it.

	`date_from`/`date_to` AND `from_date`/`to_date` BOTH WORK. The first pair is
	what this tool was asked for and the second is what every other dated tool in
	this app takes; refusing either spelling would be a refusal about a call
	nobody could have got right from the catalogue alone.
	"""
	_require()
	company = resolve_company(as_str(args, "company"))
	name = as_str(args, "name") or as_str(args, "valve") or as_str(args, "valve_id", required=True)
	row = _valve_row(name, company or "")

	inner = dict(args)
	inner["asset"] = row["name"]
	for stated, canonical in (("date_from", "from_date"), ("date_to", "to_date")):
		if args.get(stated) is not None and args.get(canonical) is None:
			inner[canonical] = args[stated]
	inner.pop("name", None)
	inner.pop("valve", None)
	inner.pop("valve_id", None)

	measured = irrigation.get_irrigation_runtime(inner).data

	data = {
		"valve": row["name"],
		"valve_type": row.get("valve_type") or None,
		"state": _state_of(row),
		"zone": row.get("irrigation_zone") or None,
		"zone_detail": _zone_block(row.get("irrigation_zone")),
		**{key: value for key, value in measured.items() if key != "asset"},
	}

	zone_name = str(row.get("irrigation_zone") or "")
	if zone_name and as_bool(args, "include_zone", True) is not False:
		data["zone_rollup"] = _zone_rollup(zone_name, company or "", measured["from"], measured["to"])
	else:
		data["zone_rollup"] = None

	rollup = data["zone_rollup"]
	share = ""
	if rollup and rollup["runtime_minutes"]:
		share = f" of the zone's {rollup['runtime_hours']} h"
	return ToolResult(
		data=data,
		summary=(
			f"{row['name']}: {data['runtime_hours']} h over {measured['from'][:10]}"
			f"–{measured['to'][:10]}{share}"
		),
	)


def _zone_rollup(zone: str, company: str, opened: str, closed: str) -> dict:
	"""Every valve on one zone, summed over the same window.

	Capped at `irrigation.VALVE_CAP` and the cap is REPORTED. A zone past five
	hundred valves is a report run per sub-line rather than one call, and a total
	that quietly stopped counting is the worst number to put next to a valve's
	own — it would make the valve look like a larger share of the zone than it is.
	"""
	filters: dict = {
		"asset_type": VALVE,
		"irrigation_zone": zone,
		"retired_at": ("is", "not set"),
	}
	if company:
		filters["company"] = company
	rows = (
		frappe.db.get_all(
			ASSET_REGISTER,
			filters=filters,
			fields=_fields(),
			order_by="name asc",
			limit=irrigation.VALVE_CAP + 1,
		)
		or []
	)
	truncated = len(rows) > irrigation.VALVE_CAP
	valves = [dict(row) for row in rows[: irrigation.VALVE_CAP]]

	now = str(frappe.utils.now())
	measured = [irrigation._runs_for(valve, opened, closed, now) for valve in valves]
	minutes = round(sum(entry["runtime_minutes"] for entry in measured), 1)
	open_minutes = round(sum(entry["open_run_minutes"] for entry in measured), 1)

	detail = _zone_block(zone) or {}
	rate = detail.get("flow_rate_gpm")
	gallons = round(minutes * rate, 1) if rate else None
	return {
		"zone": zone,
		"zone_name": detail.get("zone_name"),
		"valve_count": len(valves),
		"run_count": sum(entry["run_count"] for entry in measured),
		"runtime_minutes": minutes,
		"runtime_hours": round(minutes / 60.0, 2),
		"open_run_minutes": open_minutes,
		"valves_open_now": [entry["asset_name"] for entry in measured if entry["still_open"]],
		"flow_rate_gpm": rate,
		"gallons": gallons,
		"acre_inches": (round(gallons / irrigation.GALLONS_PER_ACRE_INCH, 3) if gallons else None),
		"valves_truncated": truncated,
	}


# ── scan_valve_qr ───────────────────────────────────────────────────────────
def scan_valve_qr(args: dict) -> ToolResult:
	"""Resolve a scanned QR to its valve, record the scan, answer with the whole screen.

	THE STRING A CAMERA PRODUCES IS A URL, NOT A DOCNAME. `Asset Register`
	derives `qr_url` as `<public url>/scan/<name>`, so the payload off a printed
	tag has to be unwound before it will match anything — `universal_scan.scan_target`
	is that unwinding and is reused rather than re-written, because two parsers
	for one tag format is one parser that eventually disagrees about a valve whose
	name has a slash in it. A bare docname typed into a manual-entry box passes
	through untouched, which is the same call.

	A CREDENTIAL QR IS REFUSED BEFORE ANY REGISTER IS READ, and is not quoted
	back. The nearest QR to a valve tag is sometimes the login one, and this
	answer is drawn on a screen and cached by a handset.

	WHAT IS WRITTEN: the scan stamp, and nothing else. `last_scan_at`,
	`last_scan_by` and — where the handset sent a fix — the valve's GPS position,
	which is `scan_asset`'s existing behaviour and the reason a valve knows when
	somebody was last standing at it. THE VALVE IS NOT TOGGLED HERE. Scanning a
	tag is looking at a thing; opening water onto a block is a decision, and one
	that cascades. `next_action` in the answer is the button; `toggle_irrigation_valve`
	is what the button posts to.
	"""
	_require()
	raw = ""
	for key in ("qr_data", "content", "scan", "raw", "code", "qr_code", "qr_url"):
		raw = as_str(args, key)
		if raw:
			break
	if not raw:
		raise ToolError(
			"qr_data is required — the string the scanner read. It may be the tag's full URL "
			"(…/scan/MC-Valve-05) or the bare valve ID typed into a manual-entry box; both "
			"resolve to the same valve."
		)

	universal_scan._refuse_credential_payload(raw.strip())
	candidate = universal_scan.scan_target(raw)
	company = resolve_company(as_str(args, "company"))

	if not frappe.db.exists(ASSET_REGISTER, candidate):
		raise ToolError(
			f"nothing in the Asset Register is called {candidate!r}. That scan is not a valve "
			"tag from this site — universal_scan matches a string against the badge, asset, "
			"housing and block registers at once and will say which, if any, it belongs to. "
			"Nothing was recorded."
		)
	row = _valve_row(candidate, company or "", verb="recorded")

	inner = {"asset_name": row["name"]}
	scanned_by = as_str(args, "scanned_by") or as_str(args, "performed_by")
	if scanned_by:
		inner["scanned_by"] = scanned_by
	for key in ("gps_lat", "gps_lon", "gps_latitude", "gps_longitude"):
		if args.get(key) is not None:
			inner[key] = args[key]
	asset_tags.scan_asset(inner)

	# READ AFTER THE STAMP, not before, so the screen shows the scan that just
	# happened rather than the one before it.
	row = dict(frappe.db.get_value(ASSET_REGISTER, row["name"], _fields(), as_dict=True) or {})
	data = {
		"scanned": raw,
		"resolved_from": candidate,
		"entity_type": "irrigation_valve",
		"scan_recorded": True,
		**_status(row, args),
	}

	runtime = data["runtime_today"]
	return ToolResult(
		data=data,
		summary=(
			f"scanned {row['name']} ({data['valve_type'] or 'unranked'}): {data['state']}, "
			f"{runtime['hours']} h today"
			+ (f", next action {data['next_action']['action']}" if data["next_action"]["action"] else "")
		),
		docstatus_delta="0 → 0 (updated)",
	)
