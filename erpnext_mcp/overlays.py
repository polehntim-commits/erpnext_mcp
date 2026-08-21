# SPDX-License-Identifier: MIT
"""What is TRUE OF A BLOCK RIGHT NOW, in the four colours a map can carry.

v0.116.0. Cycle 5, Precision Ag Map Phase 3. `farm_overview.py` put every
boundary on one page in v0.110.0 and it draws SHAPE — where the ground is and
whose it is. Shape does not change between one morning and the next, and every
question a farm actually asks a map at six in the morning does:

    *Which blocks may a tractor go on today? Which are closed to entry, and for
    how long? Which are ready to pick? Where did the water run last night?*

Four registers already hold all four answers and not one of them was reachable
from the map. This module is the join, and it is a READ — it writes nothing,
computes nothing that is not derived from a stored record, and hands every
answer back with the record it came from.

────────────────────────────────────────────────────────────────────────────
EVERY LAYER IS A JOIN OVER A REGISTER THIS APP ALREADY OWNS
────────────────────────────────────────────────────────────────────────────

  irrigation   Asset State Log — the valve opens and closes `log_asset_state_change`
               has been writing since v0.25.0, reached through
               `Asset Register.irrigation_zone`. Not a new measurement: the same
               events `get_irrigation_runtime` sums into hours, read for the ONE
               fact a map needs — when did the water last come off.
  spray_rei    `spray_rei.active_for_blocks`. Not re-queried here; the restricted
               entry register has ONE reader and this is a caller of it.
  spray_phi    `spray.phi_windows_for_blocks`, which already reads BOTH places a
               pre-harvest date is stamped and takes the later. Re-deriving it
               here would have produced a map that cleared a block the compliance
               rule says is closed.
  harvest      The latest `Crop Observation` per block — the register v0.115.0's
               scouting sweep exists to fill, read for its BBCH code and its
               Brix.

NOTHING BELOW REIMPLEMENTS ANY OF THE FOUR. That is the whole discipline of this
file: an overlay that computed its own REI window would be a second opinion on a
federal restriction, and the two would drift the first time either was corrected.

────────────────────────────────────────────────────────────────────────────
COMPACTION IS NOT THE SAME QUESTION AS RESTRICTED ENTRY, AND THE COLOURS MUST
NOT BE READ AS IF IT WERE
────────────────────────────────────────────────────────────────────────────

The irrigation layer answers *may a MACHINE go on this ground* — wet soil under
a loaded sprayer takes wheel ruts and a compacted pan that outlasts the planting.
The REI layer answers *may a PERSON walk in* — 40 CFR §170.407, PPE, and an
employer's duty to keep workers out. They are different rules with different
subjects and different consequences, so they are two layers with two vocabularies
and this module never merges them into one traffic light. `equipment_access` is
the ONE place they are read together, it says which input drove its verdict, and
a live REI beats every soil consideration on it: nobody is driving into a treated
block to avoid a rut.

────────────────────────────────────────────────────────────────────────────
UNKNOWN IS A COLOUR AND IT IS NEVER GREEN
────────────────────────────────────────────────────────────────────────────

A zone whose valves have never been logged has not been proven dry. A block with
no observation on it this season is not "not ready". A crop with no Brix target
recorded does not make every block ripe. Each of those comes back as `unknown`
with the reason stated, because the failure this whole surface has to avoid is
the comfortable one: a map that draws an unmeasured block in the colour of a
measured, safe one is worse than a map with a hole in it — the hole is a job
somebody can go and do.

────────────────────────────────────────────────────────────────────────────
WHO SEES WHICH LAYER IS A TABLE, AND IT IS A DISPLAY FILTER RATHER THAN A LOCK
────────────────────────────────────────────────────────────────────────────

`ROLE_LAYERS` below. A field worker gets restricted entry and nothing else — not
because the rest is secret but because a picking crew's phone showing five
overlapping colour schemes is a phone nobody reads the one that matters off.
The restricted-entry layer is the one every role gets, always, and that is the
single hardest rule in this file: it is the layer that exists to keep somebody
out of a treated block.

AN ACCOUNT HOLDING NONE OF THE SEVEN IS NOT FILTERED AT ALL, and that is not a
hole. A picker HAS the Field Worker role — `create_mobile_user` grants one as
part of enrolling them — so a login with no farm role is the MCP system user, an
accountant or an operator's own Desk session, none of which is the phone this
table exists to keep readable. `layers_for` reports `unfiltered` so a legend can
tell "all five, filtered to your role" from "no filter applied".

AND IT IS NOT THE GATE. `frappe.has_permission` on each underlying register is
what actually decides whether a row can be read, exactly as it does on
`farm_overview`, and `guard.scoped` runs on the mobile surface as it does on
every other route. This table decides what a screen is CLUTTERED with, and a
client that trusted it in place of the permission check would be a sign on a
door with no lock — the same sentence `roles.role_indicator` carries about the
badge, for the same reason.
"""

from __future__ import annotations

import frappe

from . import compat, roles
from .erpnext_mcp.doctype.soil_compaction_profile.soil_compaction_profile import (
	GREEN,
	IRRIGATING,
	RED,
	UNKNOWN,
	YELLOW,
	band,
)
from .tools import asset_tags
from .tools import farm as farm_tools
from .tools import spray as spray_tools
from .tools import spray_rei as rei_tools

FIELD = "Field"
IRRIGATION_ZONE = "Irrigation Zone"
ASSET_REGISTER = "Asset Register"
ASSET_STATE_LOG = "Asset State Log"
CROP_OBSERVATION = "Crop Observation"
CROP = "Crop"
CROP_VARIETY = "Crop Variety"
FARM_TASK = "Farm Task"
FARM_TASK_ASSIGNMENT = "Farm Task Assignment"
SOIL_PROFILE = "Soil Compaction Profile"

# ── the layers ──────────────────────────────────────────────────────────────

LAYER_IRRIGATION = "irrigation"
LAYER_REI = "spray_rei"
LAYER_PHI = "spray_phi"
LAYER_HARVEST = "harvest"
LAYER_ACCESS = "equipment_access"

#: Every layer, with the register it is drawn over and the sentence a legend
#: prints. `subject` is `zone` or `block` and is what decides which shape on the
#: map gets the colour — an irrigation set is a ZONE fact and a restricted entry
#: is a BLOCK fact, and drawing either on the other's polygon would put a
#: restriction on ground it does not cover.
LAYERS = (
	{
		"key": LAYER_IRRIGATION,
		"label": "Irrigation & compaction",
		"subject": "zone",
		"detail": (
			"How long since the water came off, against this soil's own hours. Red is "
			"wet ground a machine should stay off; it says nothing about whether a "
			"person may walk in."
		),
	},
	{
		"key": LAYER_REI,
		"label": "Restricted entry (REI)",
		"subject": "block",
		"detail": (
			"Blocks closed to entry after a pesticide application, with the hours remaining. 40 CFR §170.407."
		),
	},
	{
		"key": LAYER_PHI,
		"label": "Pre-harvest interval (PHI)",
		"subject": "block",
		"detail": "Blocks that may not be PICKED yet, with the date each one opens.",
	},
	{
		"key": LAYER_HARVEST,
		"label": "Harvest readiness",
		"subject": "block",
		"detail": (
			"The latest scouting round's growth stage and Brix, against the variety's "
			"pick target. Both numbers, because either alone calls a pick wrong."
		),
	},
	{
		"key": LAYER_ACCESS,
		"label": "Equipment access",
		"subject": "block",
		"detail": (
			"Whether a tractor or sprayer may go on this block: live restrictions "
			"first, then the wettest zone on it."
		),
	},
)

LAYER_KEYS = tuple(spec["key"] for spec in LAYERS)
LAYER_BY_KEY = {spec["key"]: spec for spec in LAYERS}

#: The layer nobody is ever without. Stated as its own constant rather than
#: repeated into seven rows of the table below, because it is the one entry that
#: is a SAFETY rule and not a screen-clutter judgement — a role that could be
#: configured without it would be a role that can be sent into a treated block.
ALWAYS = frozenset({LAYER_REI})

#: Which of this app's roles sees which layers, and the whole of the reasoning.
#:
#: THE PLAN THIS IMPLEMENTS NAMES THREE TIERS — worker, foreman, manager — and
#: this app has seven roles, so the mapping is written out rather than inferred
#: from `roles.ROLE_SPECS`, whose order is when each role was WRITTEN and not
#: what each role does (see `ROLE_INDICATORS`, which learned that the hard way).
#:
#:   Field Worker      restricted entry only. Everything else on this map is a
#:                     decision somebody else makes, and a picker's phone showing
#:                     five colour schemes is a phone the REI is missed on.
#:   Crew Leader       and harvest readiness, because the crew lead standing in
#:                     the row is who is asked "is this block ready" — but not
#:                     the machinery layers, which they do not dispatch.
#:   Foreman           all five. They send the tractor, they call the pick, and
#:                     the pre-harvest interval is the one that stops a load
#:                     being rejected at the packhouse.
#:   Farm Manager      all five.
#:   Compliance Officer the two REGULATED windows and nothing operational. REI
#:                     and PHI are their register; which block is wet is not.
#:   Family Member     none. The holding-company view is the land, not the day.
#:   Advisor           none, which is the narrowest role in the app being narrow.
#:
#: A role not named here — including one an operator invented — gets `ALWAYS`,
#: which is restricted entry. Failing OPEN on the safety layer and CLOSED on
#: everything else is the only direction that is safe in both.
ROLE_LAYERS = {
	"Field Worker": ALWAYS,
	"Crew Leader": ALWAYS | {LAYER_HARVEST},
	"Foreman": frozenset(LAYER_KEYS),
	"Farm Manager": frozenset(LAYER_KEYS),
	"Compliance Officer": ALWAYS | {LAYER_PHI},
	"Family Member": ALWAYS,
	"Advisor": ALWAYS,
}

#: What an account holding System Manager sees. The operator's own login, and a
#: map that hid a layer from the person who administers the site would send them
#: to support over a display filter.
ADMIN_LAYERS = frozenset(LAYER_KEYS)

# ── caps ────────────────────────────────────────────────────────────────────

#: Blocks and zones one answer covers. `farm_tools.REGISTER_CAP` is the app's
#: standard ceiling and the one `farm_overview` draws to, so the overlay and the
#: shapes under it stop at the same row.
SUBJECT_CAP = farm_tools.REGISTER_CAP

#: Valves one answer will read state for. A farm past this has an automation
#: writing rows rather than people opening gates, and the cap is REPORTED rather
#: than quietly shortening a map.
VALVE_CAP = 1000

#: State-log rows read in one pass. The walk below wants the LATEST event per
#: valve, so it reads newest-first and stops caring once every valve has been
#: seen — this is the ceiling on how far back it will look for the stragglers.
LOG_CAP = 4000

#: Crop Observations read in one pass, newest first, for the same reason.
OBSERVATION_CAP = 2000

#: Completed assignments read for the "recently worked" note on equipment access.
OPERATION_CAP = 1000

# ── defaults ────────────────────────────────────────────────────────────────

#: The hours a block with no Soil Compaction Profile falls back to. The figures
#: the build plan names, and a loam's — which is the middle of the seeded book
#: rather than a number invented here. AN ANSWER USING THEM SAYS SO in
#: `thresholds_source`, so a colour drawn off the fallback is never mistaken for
#: one drawn off this farm's own measurement.
DEFAULT_RED_HOURS = 24.0
DEFAULT_YELLOW_HOURS = 48.0

#: How old a scouting round may be before the harvest layer calls it stale. A
#: week, because Brix moves several degrees in a hot one and a fortnight-old
#: reading quoted as today's is exactly the number that ends up in a buyer's
#: specification. A stale round is still REPORTED with its figures — it is the
#: best anybody has — and flagged, rather than dropped for a block that would
#: then look unscouted.
OBSERVATION_STALE_DAYS = 7

#: How far below the target Brix still counts as "nearly there". Two degrees is
#: about a week of accumulation in a good spell, which makes it the band worth
#: drawing: it is the set of blocks to walk again on Friday.
BRIX_NEAR_MARGIN = 2.0

#: How recently a completed job on a block counts as "just worked" for the
#: equipment-access note. One shift. It INFORMS and never decides — see
#: `_access_verdict` for why a pass over dry ground is not a reason to keep the
#: next machine off it.
RECENT_OPERATION_HOURS = 12.0

# ── harvest readiness vocabulary ────────────────────────────────────────────

STAGE_VEGETATIVE = "vegetative"
STAGE_SIZING = "sizing"
STAGE_COLOURING = "colouring"
STAGE_NEAR = "near_ready"
STAGE_READY = "ready"
STAGE_POST_HARVEST = "post_harvest"

READY_NOW = "pick_now"

#: The BBCH principal growth stages, banded for a fruit crop. The scale is a
#: PUBLISHED STANDARD and not this farm's configuration, which is why it is code
#: here and a record nowhere: 87 means "fruit ripe for picking" on every crop
#: that uses the scale, in every county, and a site that could edit it would be a
#: site whose observations no longer mean what an agronomist reads them as.
#:
#: Banded on the whole two-digit code rather than only the principal digit,
#: because the whole argument of the layer lives inside stage 8: 81 is the very
#: beginning of colouring and 89 is fully ripe, and a map that drew those the
#: same colour would have a fortnight of the season in one band.
BBCH_BANDS = (
	# (inclusive lower, inclusive upper, band, the words a popup prints)
	(0, 69, STAGE_VEGETATIVE, "Vegetative to bloom"),
	(70, 80, STAGE_SIZING, "Fruit development"),
	(81, 84, STAGE_COLOURING, "Beginning of ripening"),
	(85, 86, STAGE_NEAR, "Advanced ripening"),
	(87, 89, STAGE_READY, "Ripe for picking"),
	(90, 99, STAGE_POST_HARVEST, "Senescence / post-harvest"),
)

#: The two colour words this app needs that a soil profile's three do not cover.
#: Named rather than typed as literals in four places — every client keys its
#: styling off these exact strings.
ORANGE = "orange"
GREY = "grey"

#: Which harvest bands colour as which traffic light. Kept beside the bands
#: rather than in the page script, because the mobile client and the Desk page
#: both draw this and two copies would diverge on the release one was edited.
HARVEST_COLOUR = {
	STAGE_VEGETATIVE: GREEN,
	STAGE_SIZING: GREEN,
	STAGE_COLOURING: YELLOW,
	STAGE_NEAR: ORANGE,
	STAGE_READY: RED,
	STAGE_POST_HARVEST: GREY,
	UNKNOWN: UNKNOWN,
}

# ── equipment access vocabulary ─────────────────────────────────────────────

ACCESS_BLOCKED = "blocked"
ACCESS_CAUTION = "caution"
ACCESS_OPEN = "open"

# ── the palette ─────────────────────────────────────────────────────────────
#
# EVERY LAYER'S DICT CARRIES A `colour` AND NO CLIENT EVER MAPS A STATUS TO ONE.
# That is the whole reason this table is on the server. The Desk page, the iOS
# map and anything else drawing this answer would otherwise each hold a copy of
# "irrigating is blue, blocked is red" — and the copies would not diverge
# loudly, they would diverge on ONE status on ONE client, which reads as a block
# that is simply a different colour on the phone than on the office screen.
# Nobody files that as a bug; they stop trusting the map.
#
# `irrigating` IS BLUE AND NOT A DARKER RED, deliberately. Water on the ground
# right now is not "very restricted"; it is a state with an action attached — go
# and shut the valve — and giving it its own hue is what stops it being read as
# the top of a severity ramp.
#
# `unknown` IS GREY AND NEVER GREEN, which is the module docstring's rule in
# hex. A hatched or hollow rendering would be better still and is left to the
# client; the one thing that must not happen is an unmeasured shape wearing the
# colour of a measured safe one.
PALETTE = {
	RED: "#cf222e",
	YELLOW: "#d4a72c",
	GREEN: "#1a7f37",
	ORANGE: "#bc4c00",
	GREY: "#8c959f",
	UNKNOWN: "#8c959f",
	IRRIGATING: "#0969da",
	ACCESS_BLOCKED: "#cf222e",
	ACCESS_CAUTION: "#d4a72c",
	ACCESS_OPEN: "#1a7f37",
	READY_NOW: "#cf222e",
	STAGE_VEGETATIVE: "#1a7f37",
	STAGE_SIZING: "#1a7f37",
	STAGE_COLOURING: "#d4a72c",
	STAGE_NEAR: "#bc4c00",
	STAGE_READY: "#cf222e",
	STAGE_POST_HARVEST: "#8c959f",
}

#: What a status this table has never heard of is drawn as. A colour rather than
#: a crash, and the SAME grey as `unknown` — a client meeting a status from a
#: newer server should draw the shape as unmeasured, which is the honest reading
#: of "this build does not know what that means".
UNKNOWN_COLOUR = "#8c959f"


def colour_of(status) -> str:
	"""The hex for one status. Never raises, never returns green by accident."""
	return PALETTE.get(str(status or ""), UNKNOWN_COLOUR)


# ── role filtering ──────────────────────────────────────────────────────────
def layers_for(user: str) -> dict:
	"""Which layers this login is shown, which are held back, and why.

	NEVER RAISES, for the reason `roles.role_indicator` does not: this is folded
	into an answer a handset draws its whole map from, and an exception here
	would read on the phone as "your session is dead" rather than "you are a
	picker".

	`unfiltered` SAYS WHICH BRANCH RAN, because the two produce the same five
	layers for opposite reasons — an administrator sees everything because they
	are the operator, and an account with no farm role sees everything because
	the table has nothing to say about it. A client drawing a legend needs to be
	able to tell "all five, filtered to your role" from "no filter applied".
	"""
	login = str(user or "").strip()
	held: list = []
	is_admin = False
	if login:
		try:
			held = roles.roles_of(login)
		except Exception:  # pragma: no cover - no Has Role table
			held = []
		try:
			is_admin = "System Manager" in set(roles.all_roles_of(login))
		except Exception:  # pragma: no cover
			is_admin = False

	known = [role for role in held if role in ROLE_LAYERS]
	if is_admin or not known:
		# HOLDING NONE OF THE SEVEN IS NOT THE SAME AS BEING A PICKER, and this
		# is the branch that says so. A picker HAS the Field Worker role —
		# `create_mobile_user` is what enrols them and it grants one — so an
		# account with none of these roles is not a phone in an orchard: it is
		# the MCP system user, an accountant, or somebody's Desk login. Narrowing
		# THAT to one layer would hide four of them from the operator's own
		# console over a table that was never about them.
		#
		# It is a fail-open and it is worth naming as one. What makes it safe is
		# that this table has never been the gate: `frappe.has_permission` on
		# each register decides what can actually be read, and the one role that
		# genuinely must be narrowed — the field worker on a handset — is the one
		# case that always arrives here carrying a role.
		visible = set(ADMIN_LAYERS)
	else:
		visible = set(ALWAYS)
		for role in known:
			visible |= set(ROLE_LAYERS[role])

	shown = [key for key in LAYER_KEYS if key in visible]
	withheld = [
		{
			"key": key,
			"label": LAYER_BY_KEY[key]["label"],
			"reason": frappe._(
				"Not shown for the roles this login holds. It is a display filter, "
				"not a permission — see erpnext_mcp/overlays.py."
			),
		}
		for key in LAYER_KEYS
		if key not in visible
	]
	return {
		"visible": shown,
		"withheld": withheld,
		"roles_held": list(held),
		"farm_roles_held": known,
		"is_administrator": is_admin,
		"unfiltered": bool(is_admin or not known),
	}


def requested_layers(wanted, allowed: list) -> tuple[list, list]:
	"""`(layers to compute, names refused)` for an explicit `layers` argument.

	A caller narrowing the answer to one layer is the handset's toggle and the
	Desk page's picker; a caller naming a layer their roles do not show gets it
	REFUSED BY NAME rather than silently dropped, because a client that asked for
	harvest readiness and got a map with none would draw an empty legend and
	conclude the farm has no observations.
	"""
	if not wanted:
		return list(allowed), []
	if isinstance(wanted, str):
		names = [part.strip() for part in wanted.replace(",", " ").split() if part.strip()]
	else:
		names = [str(part).strip() for part in wanted if str(part).strip()]
	keep, refused = [], []
	for name in names:
		key = name.lower()
		if key not in LAYER_BY_KEY:
			refused.append({"key": name, "reason": f"not a layer. The five are: {', '.join(LAYER_KEYS)}."})
		elif key not in allowed:
			refused.append({"key": key, "reason": "not shown for the roles this login holds."})
		elif key not in keep:
			keep.append(key)
	return keep, refused


# ── time ────────────────────────────────────────────────────────────────────
def _now() -> str:
	return str(frappe.utils.now())


def _hours_between(later: str, earlier: str):
	"""Whole hours, to one place, or None where either stamp is unreadable."""
	if not later or not earlier:
		return None
	try:
		return round(float(frappe.utils.time_diff_in_seconds(later, earlier)) / 3600.0, 1)
	except Exception:  # pragma: no cover - a hand-edited Datetime column
		return None


def _stamp(row: dict) -> str:
	"""When an asset event happened. The same fallback `tools/irrigation.py`
	reports on: `performed_at` where there is one, else the row's own creation."""
	return str(row.get("performed_at") or row.get("creation") or "")


# ── the irrigation / compaction layer ───────────────────────────────────────
def _soil_profiles(names) -> dict:
	"""`{docname: {red_hours, yellow_hours, enabled, drainage_class}}`.

	One query for every profile the blocks in this answer name, rather than one
	per block. A farm has a handful of soils and hundreds of blocks.
	"""
	wanted = sorted({str(name).strip() for name in names or []} - {""})
	if not wanted or not compat.doctype_exists(SOIL_PROFILE):
		return {}
	fields = compat.existing_fields(
		SOIL_PROFILE, ("name", "red_hours", "yellow_hours", "enabled", "drainage_class", "source")
	)
	try:
		rows = (
			frappe.db.get_all(
				SOIL_PROFILE, filters={"name": ("in", wanted)}, fields=fields, limit=len(wanted)
			)
			or []
		)
	except Exception:  # pragma: no cover - a site shaping these columns differently
		return {}
	return {str(dict(row)["name"]): dict(row) for row in rows}


def resolve_thresholds(profile_name: str, profiles: dict) -> dict:
	"""The two hour figures to colour one block by, and where they came from.

	  FOUR OUTCOMES AND EACH IS NAMED, because "24 and 48" arriving with no
	  provenance is indistinguishable between a farm that measured its loam and a
	  farm that has never opened the soil register:

	`profile`   the block names a profile and it is enabled. Its own hours.
	`default`   the block names none. The shipped fallback.
	`missing`   the block names a profile this site no longer has. The
	            fallback, and the name of what could not be found — a Link
	            whose target was deleted is a data fix, not a colour choice.
	`disabled`  the block names a profile somebody retired. The fallback, and
	            the name — so a colour that changed across the farm on the day
	            a row was unticked can be traced to that row.
	"""
	name = str(profile_name or "").strip()
	if not name:
		return {
			"red_hours": DEFAULT_RED_HOURS,
			"yellow_hours": DEFAULT_YELLOW_HOURS,
			"soil_profile": None,
			"drainage_class": None,
			"thresholds_source": "default",
		}
	row = profiles.get(name)
	if not row:
		return {
			"red_hours": DEFAULT_RED_HOURS,
			"yellow_hours": DEFAULT_YELLOW_HOURS,
			"soil_profile": name,
			"drainage_class": None,
			"thresholds_source": "missing",
		}
	if not compat.checked(row.get("enabled")):
		return {
			"red_hours": DEFAULT_RED_HOURS,
			"yellow_hours": DEFAULT_YELLOW_HOURS,
			"soil_profile": name,
			"drainage_class": row.get("drainage_class") or None,
			"thresholds_source": "disabled",
		}
	return {
		"red_hours": round(float(row.get("red_hours") or DEFAULT_RED_HOURS), 1),
		"yellow_hours": round(float(row.get("yellow_hours") or DEFAULT_YELLOW_HOURS), 1),
		"soil_profile": name,
		"drainage_class": row.get("drainage_class") or None,
		"thresholds_source": "profile",
	}


def _valves_for_zones(zone_names: list, company: str) -> tuple[dict, bool]:
	"""`({zone: [valve docname]}, hit the cap)` off `Asset Register.irrigation_zone`.

	THE LINK IS THE ONE v0.78.0 ADDED FOR EXACTLY THIS and it is never inferred:
	a valve whose `irrigation_zone` is blank belongs to no zone here, and the
	zone it physically waters is not guessed at by name. `get_water_usage_report`
	made the same call about pricing a valve's minutes, and for the same reason —
	a match by name is wrong silently and only on the farm that renamed a zone.
	"""
	if not compat.doctype_exists(ASSET_REGISTER) or not compat.has_field(ASSET_REGISTER, "irrigation_zone"):
		return {}, False
	names = sorted({str(name).strip() for name in zone_names or []} - {""})
	if not names:
		return {}, False
	filters: dict = {
		"irrigation_zone": ("in", names),
		"asset_type": ("in", list(_valve_types())),
		"retired_at": ("is", "not set"),
	}
	if company:
		filters["company"] = company
	try:
		rows = (
			frappe.db.get_all(
				ASSET_REGISTER,
				filters=filters,
				fields=["name", "irrigation_zone"],
				order_by="name asc",
				limit=VALVE_CAP + 1,
			)
			or []
		)
	except Exception:  # pragma: no cover
		return {}, False
	capped = len(rows) > VALVE_CAP
	by_zone: dict = {}
	for row in rows[:VALVE_CAP]:
		by_zone.setdefault(str(dict(row)["irrigation_zone"]), []).append(str(dict(row)["name"]))
	return by_zone, capped


def _valve_types() -> tuple:
	"""The asset types with a valve's state machine.

	Read off `asset_tags._STATE_DEFINITIONS` by the ACTIONS they define, which is
	the same derivation `tools/irrigation.py` makes and copied here as a call
	rather than a list: three shipped types reach a state called `open` and only
	a valve defines `open_valve`/`close_valve`, so a rule written on the state
	would count a packing shed's season as an irrigation set.
	"""
	wanted = ("open_valve", "close_valve")
	return tuple(
		asset_type
		for asset_type, defn in asset_tags._STATE_DEFINITIONS.items()
		if all(action in defn["actions"] for action in wanted)
	)


def _latest_valve_events(valve_names: list) -> tuple[dict, bool]:
	"""`({valve: latest state row}, hit the cap)`.

	NEWEST FIRST AND THE FIRST ROW PER VALVE WINS. Ordered on `creation` in the
	database — the column that is always present and indexed — and re-sorted in
	Python on `performed_at` where the row carries one, which is the same
	two-step `tools/irrigation._events` takes and for the same reason: a valve
	logged this morning for a set that ran last night has a `performed_at` behind
	its `creation`, and reading only one of the two gets the order wrong on
	exactly the rows a phone wrote from a pocket with no signal.
	"""
	names = sorted({str(name) for name in valve_names or []} - {""})
	if not names or not compat.doctype_exists(ASSET_STATE_LOG):
		return {}, False
	fields = compat.existing_fields(
		ASSET_STATE_LOG,
		(
			"name",
			"asset_name",
			"action",
			"from_state",
			"to_state",
			"performed_by",
			"performed_at",
			"creation",
		),
	)
	try:
		rows = (
			frappe.db.get_all(
				ASSET_STATE_LOG,
				filters={"asset_name": ("in", names), "action": ("in", list(("open_valve", "close_valve")))},
				fields=fields,
				order_by="creation desc",
				limit=LOG_CAP + 1,
			)
			or []
		)
	except Exception:  # pragma: no cover
		return {}, False
	capped = len(rows) > LOG_CAP
	ordered = sorted((dict(row) for row in rows[:LOG_CAP]), key=_stamp, reverse=True)
	latest: dict = {}
	for row in ordered:
		key = str(row.get("asset_name") or "")
		if key and key not in latest:
			latest[key] = row
	return latest, capped


def zone_water(zone_rows: list, blocks_by_name: dict, profiles: dict, company: str, now: str) -> dict:
	"""`{zone docname: the irrigation/compaction overlay for it}`.

	THE ZONE IS THE SUBJECT AND THE BLOCK SUPPLIES THE SOIL. A zone resolves its
	thresholds through `Irrigation Zone.field` — see `Field.soil_profile` for why
	the column is on the block — and a zone naming no block falls back to the
	shipped default and says so, exactly as a block naming no profile does.

	`irrigating` IS ITS OWN STATE AND IS NOT ROLLED INTO `red`. Water on the
	ground right now is a different fact from water that came off an hour ago,
	and it is the one a foreman can act on immediately: go and shut the valve, or
	do not plan the pass at all today.
	"""
	names = [str(row.get("name") or "") for row in zone_rows]
	valves_by_zone, valves_capped = _valves_for_zones(names, company)
	every_valve = [name for valves in valves_by_zone.values() for name in valves]
	latest, log_capped = _latest_valve_events(every_valve)

	out = {}
	for row in zone_rows:
		zone = str(row.get("name") or "")
		block = str(row.get("field") or "")
		thresholds = resolve_thresholds((blocks_by_name.get(block) or {}).get("soil_profile") or "", profiles)
		valves = valves_by_zone.get(zone) or []
		events = [latest[name] for name in valves if name in latest]

		open_now = [event for event in events if str(event.get("action")) == "open_valve"]
		closes = [event for event in events if str(event.get("action")) == "close_valve"]

		if open_now:
			# The most recently OPENED valve, so the popup names one somebody can
			# go and shut rather than reporting "some valve on this zone".
			opened = max(open_now, key=_stamp)
			state = dict(
				status=IRRIGATING,
				hours_since_water_off=0.0,
				last_event_at=_stamp(opened) or None,
				last_event="open_valve",
				open_valves=[str(event.get("asset_name")) for event in open_now],
				last_valve=str(opened.get("asset_name") or "") or None,
			)
		elif closes:
			latest_close = max(closes, key=_stamp)
			elapsed = _hours_between(now, _stamp(latest_close))
			state = dict(
				status=band(elapsed, thresholds["red_hours"], thresholds["yellow_hours"]),
				hours_since_water_off=elapsed,
				last_event_at=_stamp(latest_close) or None,
				last_event="close_valve",
				open_valves=[],
				last_valve=str(latest_close.get("asset_name") or "") or None,
			)
		else:
			state = dict(
				status=UNKNOWN,
				hours_since_water_off=None,
				last_event_at=None,
				last_event=None,
				open_valves=[],
				last_valve=None,
			)

		out[zone] = {
			"zone": zone,
			"colour": colour_of(state["status"]),
			"label": str(row.get("zone_name") or "") or zone,
			"field": block or None,
			"valves": len(valves),
			"valves_with_history": len(events),
			# The sentence a legend prints for an uncoloured zone, and it is
			# different for the two ways a zone can have no answer. "No valve is
			# tagged to this zone" is a job in the asset register; "the valves are
			# tagged and none has ever been logged" is a job in the orchard.
			"reason": _water_reason(state["status"], valves, events),
			**thresholds,
			**state,
		}

	return {
		"zones": out,
		"valves_truncated": valves_capped,
		"events_truncated": log_capped,
	}


def _water_reason(status: str, valves: list, events: list):
	if status != UNKNOWN:
		return None
	if not valves:
		return frappe._(
			"No valve in the Asset Register names this zone. Set `irrigation_zone` on the "
			"zone's valves and every set they log colours this shape."
		)
	if not events:
		return frappe._(
			"{0} valve(s) are tagged to this zone and none has ever been opened or closed "
			"through a scan. Nothing has been measured, which is not the same as dry."
		).format(len(valves))
	return None  # pragma: no cover - unreachable: events present means a status


def roll_up_zones(block: str, zone_states: dict) -> dict:
	"""The wettest zone on one block, as the block's own water status.

	THE WORST WINS AND THE BLOCK NAMES WHICH ZONE SET IT. A block watered by four
	zones where one ran an hour ago is not three-quarters driveable; the machine
	has to cross the wet quarter. Averaging the hours — the other obvious
	choice — produces a green block with a bog in the middle of it.

	A block with no zones at all comes back `unknown`, not green. Ground this app
	has no irrigation record for has not been proven dry.
	"""
	mine = [state for state in zone_states.values() if str(state.get("field") or "") == block]
	if not mine:
		return {
			"status": UNKNOWN,
			"colour": colour_of(UNKNOWN),
			"driving_zone": None,
			"hours_since_water_off": None,
			"zones": 0,
			"reason": frappe._("No irrigation zone on this site names this block."),
		}
	order = {IRRIGATING: 0, RED: 1, YELLOW: 2, UNKNOWN: 3, GREEN: 4}
	worst = min(mine, key=lambda state: (order.get(str(state.get("status")), 3), state.get("zone") or ""))
	return {
		"status": worst.get("status"),
		"colour": colour_of(worst.get("status")),
		"driving_zone": worst.get("zone"),
		"hours_since_water_off": worst.get("hours_since_water_off"),
		"zones": len(mine),
		"reason": worst.get("reason"),
	}


# ── the spray layers ────────────────────────────────────────────────────────
def block_restrictions(block_names: list, company: str) -> dict:
	"""`{block: {rei: [...], phi: [...]}}`, both read through their own owners.

	NEITHER IS RE-QUERIED HERE. `spray_rei.active_for_blocks` runs the expiry
	sweep before it answers and `spray.phi_windows_for_blocks` reads BOTH
	registers a pre-harvest date is stamped on and takes the later of them. A map
	that reimplemented either would be a second opinion on a federal restriction,
	and the first anybody would know of the drift is a worker in a treated block
	or a load rejected at the packhouse.
	"""
	names = sorted({str(name).strip() for name in block_names or []} - {""})
	out = {name: {"rei": [], "phi": []} for name in names}
	if not names:
		return out
	for window in rei_tools.active_for_blocks(names, company) or []:
		key = str(window.get("block") or "")
		if key in out:
			out[key]["rei"].append(window)
	for window in spray_tools.phi_windows_for_blocks(names, company) or []:
		key = str(window.get("block") or "")
		if key in out:
			out[key]["phi"].append(window)
	return out


def rei_overlay(windows: list) -> dict:
	"""One block's restricted-entry state. THE STRICTEST WINDOW IS THE ANSWER.

	A block with two live restrictions on it opens when the LAST of them does,
	and the countdown a screen prints is that one. Reporting the first to expire
	would clear a block hours early with a number that looked precise.
	"""
	live = [window for window in windows or [] if window.get("active")]
	if not live:
		return {"status": GREEN, "colour": colour_of(GREEN), "restricted": False, "windows": 0}
	longest = max(live, key=lambda window: float(window.get("hours_remaining") or 0))
	return {
		"status": RED,
		"colour": colour_of(RED),
		"restricted": True,
		"windows": len(live),
		"hours_remaining": longest.get("hours_remaining"),
		"minutes_remaining": longest.get("minutes_remaining"),
		"expires_at": longest.get("expires_at"),
		"product": longest.get("product_name") or longest.get("product"),
		"products": longest.get("products") or [],
		"source": longest.get("name"),
		# Imported and never rewritten. Every surface that shows a restriction
		# says the same words in the same order — `spray_rei.warning_line` is
		# emphatic about why, and a map is one more screen a worker reads it off.
		"warning": longest.get("warning"),
	}


def phi_overlay(windows: list) -> dict:
	"""One block's pre-harvest state, latest-clearing window first.

	`spray.phi_windows_for_blocks` already sorts that way and already reads both
	registers, so the only decision here is which of them to print: the LAST date
	to clear, because a block is pickable when every interval on it has run.
	"""
	live = list(windows or [])
	if not live:
		return {"status": GREEN, "colour": colour_of(GREEN), "restricted": False, "windows": 0}
	longest = live[0]
	days = longest.get("days_remaining")
	return {
		"status": ORANGE,
		"colour": colour_of(ORANGE),
		"restricted": True,
		"windows": len(live),
		"days_remaining": days,
		"clears_on": longest.get("phi_clears_on"),
		"product": longest.get("phi_source_item"),
		"source": longest.get("source"),
		"source_doctype": longest.get("source_doctype"),
		"warning": longest.get("warning"),
	}


# ── the harvest readiness layer ─────────────────────────────────────────────
def bbch_band(code) -> tuple:
	"""`(band, the words for it)` for a BBCH code, or `(UNKNOWN, None)`.

	`Crop Observation.growth_stage_code` IS A `Data` COLUMN, deliberately — the
	farm types what the farm keeps — so what arrives here is "87", " 87 ", "BBCH
	87" or "petal fall". Anything that does not reduce to a two-digit number in
	range is UNKNOWN rather than an error: an unparseable stage is a round that
	still recorded a Brix, and refusing the whole observation over the stage
	column would throw away the half that parsed.
	"""
	text = str(code or "").strip()
	if not text:
		return UNKNOWN, None
	digits = "".join(char for char in text if char.isdigit())
	if not digits or len(digits) > 2:
		return UNKNOWN, None
	number = int(digits)
	for low, high, key, label in BBCH_BANDS:
		if low <= number <= high:
			return key, label
	return UNKNOWN, None  # pragma: no cover - the bands cover 0-99


def _crop_targets(crops: list) -> dict:
	"""`{crop name (casefolded): {"target": float|None, "varieties": {...}}}`.

	One query for the crops the blocks in this answer name. `Crop.target_brix` is
	the crop's own pick figure and the `varieties` child table carries the sparse
	per-variety overlay — the same shape v0.114.0 gave the water schedule, and
	resolved the same way: a blank cell is a variety with no opinion, and a ZERO
	is a variety claiming zero.
	"""
	wanted = sorted({str(name).strip().casefold() for name in crops or []} - {""})
	if not wanted or not compat.doctype_exists(CROP) or not compat.has_field(CROP, "target_brix"):
		return {}
	try:
		rows = (
			frappe.db.get_all(
				CROP,
				fields=compat.existing_fields(CROP, ("name", "crop_name", "target_brix")),
				limit=SUBJECT_CAP,
			)
			or []
		)
	except Exception:  # pragma: no cover
		return {}
	out = {}
	for raw in rows:
		row = dict(raw)
		key = str(row.get("crop_name") or row.get("name") or "").strip().casefold()
		if key not in wanted:
			continue
		out[key] = {
			"crop": str(row.get("name") or ""),
			"target": _number_or_none(row.get("target_brix")),
			"varieties": _variety_targets(str(row.get("name") or "")),
		}
	return out


def _variety_targets(crop: str) -> dict:
	"""`{variety (casefolded): target}` off the crop's own variety table."""
	if (
		not crop
		or not compat.doctype_exists(CROP_VARIETY)
		or not compat.has_field(CROP_VARIETY, "target_brix")
	):
		return {}
	try:
		rows = (
			frappe.db.get_all(
				CROP_VARIETY,
				filters={"parent": crop, "parenttype": CROP, "parentfield": "varieties"},
				fields=["variety_name", "target_brix"],
				limit=SUBJECT_CAP,
			)
			or []
		)
	except Exception:  # pragma: no cover
		return {}
	out = {}
	for raw in rows:
		row = dict(raw)
		name = str(row.get("variety_name") or "").strip().casefold()
		if name:
			out[name] = _number_or_none(row.get("target_brix"))
	return out


def _number_or_none(value):
	"""A stored float, or None where the cell is empty.

	`float(value or 0)` is wrong on a Brix target and wrong QUIETLY: it turns a
	variety nobody has set a target for into a variety picked at zero Brix, which
	makes every block on the farm ready on the day it is planted. The same
	distinction `tools/agronomy._number_or_none` draws, for the same reason.
	"""
	if value in (None, ""):
		return None
	try:
		return round(float(value), 2)
	except (TypeError, ValueError):  # pragma: no cover
		return None


def brix_target(crop: str, variety: str, targets: dict) -> dict:
	"""`{"target": float|None, "target_source": ...}` for one block's planting.

	THE ANSWER TRAVELS WITH WHERE IT CAME FROM, the rule the variety water
	overlay already follows: a caller handed 19.0 cannot tell this variety's
	considered figure from its crop's default, and those are different facts
	about the block in front of them.
	"""
	entry = targets.get(str(crop or "").strip().casefold())
	if not entry:
		return {"target": None, "target_source": "none", "target_crop": None}
	own = entry["varieties"].get(str(variety or "").strip().casefold())
	if own is not None:
		return {"target": own, "target_source": "variety", "target_crop": entry["crop"]}
	if entry["target"] is not None:
		return {"target": entry["target"], "target_source": "crop", "target_crop": entry["crop"]}
	return {"target": None, "target_source": "none", "target_crop": entry["crop"]}


def _latest_observations(block_names: list, company: str) -> tuple[dict, bool]:
	"""`({block: latest Crop Observation}, hit the cap)`, newest round wins.

	Ordered on `observed_on` — the day it was SEEN, which the column's own
	description insists is not the day it was typed — and then on `observed_at`
	and `creation` to settle a block walked twice on one day. Two rounds on one
	morning is a real thing (a Brix at seven and a second at eleven), and the
	later one is the one worth colouring.
	"""
	names = sorted({str(name).strip() for name in block_names or []} - {""})
	if not names or not compat.doctype_exists(CROP_OBSERVATION):
		return {}, False
	filters: dict = {"block": ("in", names)}
	if company:
		filters["company"] = company
	fields = compat.existing_fields(
		CROP_OBSERVATION,
		(
			"name",
			"block",
			"block_doctype",
			"company",
			"observation_type",
			"observed_on",
			"observed_at",
			"observer",
			"crop",
			"crop_stage",
			"growth_stage_code",
			"brix_reading",
			"brix_method",
			"source_task",
			"creation",
		),
	)
	try:
		rows = (
			frappe.db.get_all(
				CROP_OBSERVATION,
				filters=filters,
				fields=fields,
				order_by="observed_on desc, creation desc",
				limit=OBSERVATION_CAP + 1,
			)
			or []
		)
	except Exception:  # pragma: no cover
		return {}, False
	capped = len(rows) > OBSERVATION_CAP
	latest: dict = {}
	for raw in rows[:OBSERVATION_CAP]:
		row = dict(raw)
		key = str(row.get("block") or "")
		if not key:
			continue
		if key not in latest or _observation_key(row) > _observation_key(latest[key]):
			latest[key] = row
	return latest, capped


def _observation_key(row: dict) -> tuple:
	return (
		str(row.get("observed_on") or ""),
		str(row.get("observed_at") or ""),
		str(row.get("creation") or ""),
	)


def harvest_overlay(observation: dict, target: dict, today: str) -> dict:
	"""One block's readiness, from a growth stage and a Brix and NEITHER ALONE.

	`Crop Observation.brix_reading`'s own description states the rule this
	implements: "Brix rises while the stage stands still in a hot week and the
	stage advances while Brix stalls in a wet one; either number alone will call
	a pick wrong." So:

	  * BOTH SAY READY → `pick_now`. THE ONLY COMBINATION THAT DOES.
	  * ONE SAYS READY → `near_ready`, and `short_of` NAMES what is missing.
	    Four values, because they are four different jobs:
	      `brix`          the reading is in and it is under the target. Walk it
	                      again on Friday.
	      `brix_reading`  the stage says ripe and NOBODY HAS TAKEN A BRIX. Go and
	                      take one — this is not a promotion to `pick_now`,
	                      because the figure a buyer's specification quotes is
	                      exactly the one nobody can tell apart afterwards.
	      `brix_target`   the reading is in and this crop has no pick figure
	                      recorded to judge it against. That is a row to fill in
	                      on the Crop, and inventing a target instead would turn
	                      every block on the farm the same colour on one day —
	                      the failure mode that looks most like working.
	      `stage`         the sugar is there and the fruit is not. A block whose
	                      colour has not arrived yet.

	A ROUND OLDER THAN A WEEK IS STILL REPORTED, flagged `stale`. It is the best
	anybody has, and dropping it would draw an unscouted block over a block that
	was scouted a fortnight ago — opposite problems with the same picture.
	"""
	if not observation:
		return {
			"status": UNKNOWN,
			"colour": colour_of(UNKNOWN),
			"ready": False,
			"observed_on": None,
			"reason": frappe._(
				"No Crop Observation on this block. A completed scouting round becomes one "
				"through index_scouting_observations."
			),
		}

	stage, stage_label = bbch_band(observation.get("growth_stage_code"))
	brix = _number_or_none(observation.get("brix_reading"))
	goal = target.get("target")

	have_brix = brix is not None
	have_target = goal is not None
	stage_ready = stage == STAGE_READY
	brix_ready = have_brix and have_target and brix >= goal

	short_of = None
	if stage_ready and brix_ready:
		status, colour = READY_NOW, RED
	elif stage_ready:
		status, colour = STAGE_NEAR, ORANGE
		short_of = (
			"brix" if (have_brix and have_target) else ("brix_reading" if not have_brix else "brix_target")
		)
	elif brix_ready and stage in (STAGE_NEAR, STAGE_COLOURING):
		status, colour, short_of = STAGE_NEAR, ORANGE, "stage"
	else:
		status = stage
		colour = HARVEST_COLOUR.get(stage, UNKNOWN)

	observed_on = str(observation.get("observed_on") or "")[:10] or None
	age = _days_since(observed_on, today)
	return {
		"status": status,
		# The BAND is what the logic above decided; the hex is looked up here so
		# that no client ever maps one to the other. See `PALETTE`.
		"colour": colour_of(colour),
		"band": colour,
		"ready": status == READY_NOW,
		"stage": stage,
		"stage_label": stage_label,
		"growth_stage_code": str(observation.get("growth_stage_code") or "") or None,
		"crop_stage": observation.get("crop_stage") or None,
		"brix": brix,
		"brix_method": observation.get("brix_method") or None,
		"brix_missing": brix is None,
		"short_of": short_of,
		"observation": observation.get("name"),
		"observation_type": observation.get("observation_type") or None,
		"observed_on": observed_on,
		"observed_days_ago": age,
		"observer": observation.get("observer") or None,
		"stale": bool(age is not None and age > OBSERVATION_STALE_DAYS),
		"reason": None,
		**target,
	}


def _days_since(date: str, today: str):
	if not date:
		return None
	try:
		return int(frappe.utils.date_diff(today, date))
	except Exception:  # pragma: no cover - a hand-edited Date column
		return None


# ── the equipment access composite ──────────────────────────────────────────
def _recent_operations(block_names: list, company: str) -> dict:
	"""`{block: {task, task_type, completed_at}}` for the last job closed on each.

	TWO STEPS AND NOT A JOIN, the same shape `spray._phi_from_applications` takes
	through its child table: a Farm Task carries the LOCATION and a Farm Task
	Assignment carries the COMPLETION, and this app does not write raw SQL. The
	task set is bounded by the blocks already in the answer, so the second query
	is over a list rather than a register.
	"""
	names = sorted({str(name).strip() for name in block_names or []} - {""})
	if not names or not compat.doctype_exists(FARM_TASK) or not compat.doctype_exists(FARM_TASK_ASSIGNMENT):
		return {}
	filters: dict = {"location": ("in", names), "state": ("in", ("Awaiting-Review", "Completed"))}
	if company:
		filters["company"] = company
	try:
		tasks = (
			frappe.db.get_all(
				FARM_TASK,
				filters=filters,
				fields=["name", "location", "task_type", "task_name"],
				order_by="modified desc",
				limit=OPERATION_CAP,
			)
			or []
		)
	except Exception:  # pragma: no cover
		return {}
	tasks = [dict(row) for row in tasks]
	if not tasks:
		return {}
	by_task = {str(row["name"]): row for row in tasks}
	try:
		closed = (
			frappe.db.get_all(
				FARM_TASK_ASSIGNMENT,
				filters={"task": ("in", list(by_task)), "completed_at": ("is", "set")},
				fields=["task", "completed_at", "assigned_to_name"],
				order_by="completed_at desc",
				limit=OPERATION_CAP,
			)
			or []
		)
	except Exception:  # pragma: no cover
		return {}
	out: dict = {}
	for raw in closed:
		row = dict(raw)
		task = by_task.get(str(row.get("task") or ""))
		if not task:
			continue
		block = str(task.get("location") or "")
		if block in out:
			continue  # ordered newest first, so the first one seen is the latest
		out[block] = {
			"task": task["name"],
			"task_name": task.get("task_name") or None,
			"task_type": task.get("task_type") or None,
			"completed_at": str(row.get("completed_at") or "") or None,
			"by": row.get("assigned_to_name") or None,
		}
	return out


def access_overlay(water: dict, rei: dict, operation: dict, now: str) -> dict:
	"""May a tractor or a sprayer go on this block, and what decided it.

	THE ORDER IS THE POINT AND IT IS NOT A WEIGHTED SCORE. Three inputs, read
	strictly in this order, with the first that fires deciding:

	  1. A LIVE RESTRICTED-ENTRY WINDOW BLOCKS IT, full stop. Driving a sprayer
	     into a treated block is an entry, the operator is a person, and no soil
	     consideration on earth outranks 40 CFR §170.407. This is why the
	     composite is not an average: an average lets a very dry block outvote a
	     federal restriction.
	  2. WET GROUND IS CAUTION, NOT A REFUSAL. Whether a pass is worth a rut is a
	     judgement about the machine, the load and how badly the job is needed —
	     which is the foreman's and not this app's. `driving_zone` names the zone
	     that set it so the judgement can be made about the right piece of ground.
	  3. AN UNMEASURED BLOCK IS CAUTION, NOT OPEN. Nothing about a zone with no
	     valve history says it is dry.

	SOIL MOISTURE IS NOT AN INPUT AND THE ANSWER SAYS SO. This app has no soil
	moisture register yet — it arrives with the satellite work — and weighting a
	missing reading as zero would produce a verdict that looked like it had
	consulted a probe. `inputs_missing` names it, so a green block here is
	honestly "nothing we can measure is against it" rather than "we checked
	everything".

	THE RECENT OPERATION INFORMS AND NEVER DECIDES. A pass over dry ground is not
	a reason to keep the next machine off it, and treating it as one would close
	a block to the second half of a job the first half started. It is reported
	because repeat passes on WET ground are the actual compaction mechanism, and
	that is a sentence a foreman reads and acts on.
	"""
	worked_hours = _hours_between(now, str((operation or {}).get("completed_at") or ""))
	notes = []
	if rei.get("restricted"):
		verdict, driver = ACCESS_BLOCKED, LAYER_REI
		notes.append(rei.get("warning"))
	elif water.get("status") in (IRRIGATING, RED):
		verdict, driver = ACCESS_CAUTION, LAYER_IRRIGATION
	elif water.get("status") == UNKNOWN:
		verdict, driver = ACCESS_CAUTION, LAYER_IRRIGATION
		notes.append(water.get("reason"))
	elif water.get("status") == YELLOW:
		verdict, driver = ACCESS_CAUTION, LAYER_IRRIGATION
	else:
		verdict, driver = ACCESS_OPEN, None

	if (
		worked_hours is not None
		and worked_hours <= RECENT_OPERATION_HOURS
		and water.get("status")
		in (
			IRRIGATING,
			RED,
			YELLOW,
		)
	):
		notes.append(
			frappe._(
				"A {0} job closed here {1} hours ago and the ground is still {2}. Repeat "
				"passes on wet soil are what compacts it."
			).format(
				(operation or {}).get("task_type") or "field",
				worked_hours,
				water.get("status"),
			)
		)

	return {
		"status": verdict,
		"colour": colour_of(verdict),
		"decided_by": driver,
		"water_status": water.get("status"),
		"driving_zone": water.get("driving_zone"),
		"hours_since_water_off": water.get("hours_since_water_off"),
		"restricted": bool(rei.get("restricted")),
		"hours_remaining": rei.get("hours_remaining"),
		"last_operation": operation or None,
		"last_operation_hours_ago": worked_hours,
		"notes": [note for note in notes if note],
		# Named rather than silently weighted at zero. See the docstring.
		"inputs_missing": ["soil_moisture"],
	}


# ── the whole answer ────────────────────────────────────────────────────────
def may_read(doctype: str) -> bool:
	"""`frappe.has_permission`, never raising.

	THE ONE COPY. `farm_overview._may_read` delegates here rather than keeping a
	second, because both surfaces make the identical promise — a register this
	login may not read contributes nothing AND IS NAMED, instead of taking the
	whole page down over one layer — and two implementations of "never raise" is
	two chances for one of them to start raising.
	"""
	try:
		return bool(frappe.has_permission(doctype, "read"))
	except Exception:  # pragma: no cover - a site mid-migrate with no meta
		return False


def _register_rows(tool: str, key: str, company: str, limit: int) -> list:
	"""One register through its own tool, or an empty list.

	The same call `farm_overview._rows` and `api/mobile._location_rows` both
	make, and for the third time the same reason: each tool is the ONE place that
	says what its register reports, and a register this site has not installed —
	or whose tool refuses for want of a company — contributes nothing rather than
	failing the answer. A farm with no irrigation zones should get an overlay
	with four layers on it, not an error.
	"""
	arguments = {"limit": limit}
	if company:
		arguments["company"] = company
	try:
		return list(getattr(farm_tools, tool)(arguments).data.get(key) or [])
	except Exception:  # pragma: no cover - ToolError on a site with no company
		return []


def build(company: str = "", visible=None, blocks=None, limit: int = SUBJECT_CAP) -> dict:
	"""Every overlay this caller may see, over one entity, in ONE answer.

	ONE CALL AND NOT FIVE, the same argument `farm_overview.farm_overview` makes:
	the layers share their subjects — equipment access reads the water status the
	irrigation layer computed and the restriction the REI layer computed — and
	five responses the browser had to reconcile would be five chances to draw a
	block green on one layer and blocked on another from two different moments.

	NOTHING IS CACHED. A valve somebody shut two minutes ago is the whole point,
	and a map that showed them the previous state would be worse than no map.

	A LAYER NOT ASKED FOR IS NOT COMPUTED. `visible` is what the caller's roles
	allow, already narrowed by any explicit request, and the reads behind an
	unasked layer do not run — a picker's phone asking for restricted entry pays
	for one query and not for the observation register.
	"""
	visible = list(visible if visible is not None else LAYER_KEYS)
	now = _now()
	today = str(frappe.utils.today())
	refused = []
	warnings = []

	wants_water = bool({LAYER_IRRIGATION, LAYER_ACCESS} & set(visible))
	wants_rei = bool({LAYER_REI, LAYER_ACCESS} & set(visible))
	wants_phi = LAYER_PHI in visible
	wants_harvest = LAYER_HARVEST in visible

	cap = max(1, min(int(limit or SUBJECT_CAP), SUBJECT_CAP))

	if may_read(FIELD):
		field_rows = _register_rows("list_fields", "fields", company, cap)
	else:
		field_rows, refused = [], [FIELD]
	wanted = {str(name).strip() for name in (blocks or []) if str(name).strip()}
	if wanted:
		field_rows = [row for row in field_rows if str(row.get("name") or "") in wanted]
		missing = sorted(wanted - {str(row.get("name") or "") for row in field_rows})
		for name in missing:
			warnings.append(
				frappe._("{0} is not a Field this login can read on {1}, so it carries no overlay.").format(
					name, company or frappe._("this site")
				)
			)

	if wants_water and may_read(IRRIGATION_ZONE):
		zone_rows = _register_rows("list_irrigation_zones", "zones", company, cap)
	else:
		zone_rows = []
		if wants_water and IRRIGATION_ZONE not in refused and not may_read(IRRIGATION_ZONE):
			refused.append(IRRIGATION_ZONE)
	if wanted and zone_rows:
		zone_rows = [row for row in zone_rows if str(row.get("field") or "") in wanted]

	blocks_by_name = {str(row.get("name") or ""): dict(row) for row in field_rows}
	block_names = list(blocks_by_name)

	profiles = _soil_profiles([row.get("soil_profile") for row in field_rows if row.get("soil_profile")])
	water = (
		zone_water(zone_rows, blocks_by_name, profiles, company, now)
		if wants_water
		else {"zones": {}, "valves_truncated": False, "events_truncated": False}
	)
	restrictions = block_restrictions(block_names, company) if (wants_rei or wants_phi) else {}
	observations, observations_capped = (
		_latest_observations(block_names, company) if wants_harvest else ({}, False)
	)
	targets = _crop_targets([row.get("crop") for row in field_rows]) if wants_harvest else {}
	operations = _recent_operations(block_names, company) if LAYER_ACCESS in visible else {}

	block_out = []
	for name, row in blocks_by_name.items():
		entry = {
			"doctype": FIELD,
			"name": name,
			"label": str(row.get("field_name") or "") or name,
			"company": row.get("owning_entity") or None,
			"crop": row.get("crop") or None,
			"variety": row.get("variety") or None,
			"acres": row.get("acreage") or None,
		}
		windows = restrictions.get(name) or {"rei": [], "phi": []}
		rolled = roll_up_zones(name, water["zones"]) if wants_water else {"status": UNKNOWN}
		if LAYER_IRRIGATION in visible:
			entry[LAYER_IRRIGATION] = rolled
		if LAYER_REI in visible:
			entry[LAYER_REI] = rei_overlay(windows["rei"])
		if wants_phi:
			entry[LAYER_PHI] = phi_overlay(windows["phi"])
		if wants_harvest:
			entry[LAYER_HARVEST] = harvest_overlay(
				observations.get(name) or {},
				brix_target(row.get("crop") or "", row.get("variety") or "", targets),
				today,
			)
		if LAYER_ACCESS in visible:
			entry[LAYER_ACCESS] = access_overlay(
				rolled, rei_overlay(windows["rei"]), operations.get(name) or {}, now
			)
		block_out.append(entry)
	block_out.sort(key=lambda entry: entry["label"].lower())

	zone_out = []
	if LAYER_IRRIGATION in visible:
		zone_out = sorted(water["zones"].values(), key=lambda entry: str(entry["label"]).lower())

	if water["valves_truncated"]:
		warnings.append(
			frappe._(
				"More than {0} valves are tagged to these zones, so some zones may be "
				"coloured off an incomplete set of them."
			).format(VALVE_CAP)
		)
	if water["events_truncated"]:
		warnings.append(
			frappe._(
				"The valve log reached its {0}-row ceiling in this pass. A zone whose last "
				"event is older than that reads as unmeasured rather than dry."
			).format(LOG_CAP)
		)
	if observations_capped:
		warnings.append(
			frappe._(
				"The scouting register reached its {0}-row ceiling, so a block whose latest "
				"round is older than that may read as unscouted."
			).format(OBSERVATION_CAP)
		)

	return {
		"company": company or None,
		"as_of": now,
		"layers": [{**LAYER_BY_KEY[key], "visible": True} for key in LAYER_KEYS if key in visible],
		"blocks": block_out,
		"zones": zone_out,
		"counts": _counts(block_out, zone_out, visible),
		"refused": refused,
		"warnings": warnings,
		"cap": cap,
		"capped": len(block_out) >= cap,
		"defaults": {
			"red_hours": DEFAULT_RED_HOURS,
			"yellow_hours": DEFAULT_YELLOW_HOURS,
			"brix_near_margin": BRIX_NEAR_MARGIN,
			"observation_stale_days": OBSERVATION_STALE_DAYS,
		},
	}


def _counts(block_out: list, zone_out: list, visible: list) -> dict:
	"""The four numbers a morning is planned off, not a tally of rows.

	"Forty blocks" is a map. "Three restricted, eleven wet, two ready to pick" is
	a day's work, and each of the three is a different person's day.
	"""
	counts = {"blocks": len(block_out), "zones": len(zone_out)}
	if LAYER_REI in visible:
		counts["restricted"] = sum(1 for entry in block_out if entry[LAYER_REI]["restricted"])
	if LAYER_PHI in visible:
		counts["pre_harvest"] = sum(1 for entry in block_out if entry[LAYER_PHI]["restricted"])
	if LAYER_HARVEST in visible:
		counts["ready_to_pick"] = sum(1 for entry in block_out if entry[LAYER_HARVEST]["ready"])
		counts["unscouted"] = sum(1 for entry in block_out if entry[LAYER_HARVEST]["status"] == UNKNOWN)
	if LAYER_IRRIGATION in visible:
		counts["irrigating"] = sum(1 for entry in zone_out if entry["status"] == IRRIGATING)
		counts["too_wet"] = sum(1 for entry in zone_out if entry["status"] == RED)
	if LAYER_ACCESS in visible:
		counts["access_blocked"] = sum(
			1 for entry in block_out if entry[LAYER_ACCESS]["status"] == ACCESS_BLOCKED
		)
		counts["access_open"] = sum(1 for entry in block_out if entry[LAYER_ACCESS]["status"] == ACCESS_OPEN)
	return counts


__all__ = [
	"ACCESS_BLOCKED",
	"ACCESS_CAUTION",
	"ACCESS_OPEN",
	"ALWAYS",
	"BBCH_BANDS",
	"BRIX_NEAR_MARGIN",
	"DEFAULT_RED_HOURS",
	"DEFAULT_YELLOW_HOURS",
	"LAYERS",
	"LAYER_ACCESS",
	"LAYER_HARVEST",
	"LAYER_IRRIGATION",
	"LAYER_KEYS",
	"LAYER_PHI",
	"LAYER_REI",
	"OBSERVATION_STALE_DAYS",
	"PALETTE",
	"READY_NOW",
	"ROLE_LAYERS",
	"access_overlay",
	"bbch_band",
	"block_restrictions",
	"brix_target",
	"build",
	"colour_of",
	"harvest_overlay",
	"layers_for",
	"may_read",
	"phi_overlay",
	"rei_overlay",
	"requested_layers",
	"resolve_thresholds",
	"roll_up_zones",
	"zone_water",
]
