# SPDX-License-Identifier: MIT
"""The operational map, as tools: what is true of every block, and the soil book.

v0.116.0. Cycle 5, Precision Ag Map Phase 3. `erpnext_mcp/overlays.py` is the
engine and argues every decision in it; this file is the five doors onto it.
NAMED `map_overlays` AND NOT `overlays` so the two are never confused in an
import list — the engine is package-level because the Desk page and the mobile
route reach it too, and a tools module of the same name would have had to be
aliased at every call site.

  `get_map_overlays`               the whole map, role-filtered. READ.
  `list_soil_compaction_profiles`  the hour figures behind the compaction
                                   colours, and which blocks each one covers.
                                   READ.
  `create_soil_compaction_profile` a soil this farm has that the shipped book
                                   does not. MUTATING, off.
  `update_soil_compaction_profile` a shipped figure replaced with this farm's
                                   own. MUTATING, off.
  `assign_soil_profile`            point one block at one profile. MUTATING, off.

WHY THE LAST THREE EXIST AT ALL, given the four records are editable in the Desk.
"Rules as data, engine as code" is only true if the data can be authored, and a
threshold that needs a Desk login to change is one a foreman on a phone quietly
stops believing. The same argument `update_compliance_rule` and
`set_pest_action_threshold` already make about their own numbers.

`assign_soil_profile` IS ITS OWN TOOL AND NOT AN ARGUMENT ON `update_field`.
`link_field_to_cost_center` set that precedent for the identical shape — one
Link column on a Field, with a real consequence behind setting it wrong — and
the alternative would have changed the signature of a tool other clients already
call.

NOTHING HERE COMPUTES AN OVERLAY. Every derivation is in `overlays.py`, so the
Desk page, the mobile route and these tools cannot disagree about what colour a
block is.
"""

from __future__ import annotations

import frappe

from .. import compat, overlays
from ..args import as_bool, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import farm as farm_tools

SOIL_PROFILE = overlays.SOIL_PROFILE
FIELD = overlays.FIELD

#: Most profiles one listing returns. A farm with more distinct soils than this
#: has imported a survey rather than described its ground.
PROFILE_CAP = 200

#: Most blocks one listing will name under a profile. The count is always exact;
#: this caps only the list of names printed beside it.
COVERAGE_CAP = 50


def _require_profiles() -> None:
	compat.require_doctype(
		SOIL_PROFILE,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _hours(args: dict, key: str, current=None):
	"""One hour figure off the arguments, or `current` where it was not passed.

	NOT PASSED AND PASSED AS ZERO ARE DIFFERENT, and the difference matters on
	exactly this argument: an update that omits `red_hours` means "leave it", and
	one that sends 0 means "this soil is never too wet", which the controller
	refuses. Reading `float(value or 0)` would silently turn the first into the
	second and take the profile down with it.
	"""
	if key not in args or args.get(key) in (None, ""):
		return current
	try:
		return round(float(args[key]), 1)
	except (TypeError, ValueError):
		raise ToolError(f"{key} must be a number of hours, got {args[key]!r}. Nothing was changed.") from None


def _describe_profile(row: dict, coverage: dict) -> dict:
	name = str(row.get("name") or "")
	blocks = coverage.get(name) or []
	return {
		"name": name,
		"soil_type": row.get("soil_type") or name,
		"drainage_class": row.get("drainage_class") or None,
		"red_hours": round(float(row.get("red_hours") or 0), 1),
		"yellow_hours": round(float(row.get("yellow_hours") or 0), 1),
		"enabled": compat.checked(row.get("enabled")),
		"source": row.get("source") or None,
		"notes": row.get("notes") or None,
		# WHICH GROUND THIS NUMBER ACTUALLY COLOURS. A profile nothing points at
		# is a row somebody wrote and never wired up, and it is indistinguishable
		# from a working one on the form — which is how a farm ends up with four
		# beautifully maintained soil profiles and every block on the default.
		"blocks": len(blocks),
		"block_names": blocks[:COVERAGE_CAP],
	}


def _coverage(names: list) -> dict:
	"""`{profile: [Field docname]}` for these profiles. One query for the set."""
	wanted = sorted({str(name) for name in names or []} - {""})
	if not wanted or not compat.has_field(FIELD, "soil_profile"):
		return {}
	try:
		rows = (
			frappe.db.get_all(
				FIELD,
				filters={"soil_profile": ("in", wanted)},
				fields=["name", "soil_profile"],
				order_by="name asc",
				limit=farm_tools.REGISTER_CAP,
			)
			or []
		)
	except Exception:  # pragma: no cover - a site shaping the column differently
		return {}
	out: dict = {}
	for raw in rows:
		row = dict(raw)
		out.setdefault(str(row.get("soil_profile") or ""), []).append(str(row.get("name") or ""))
	return out


# ── the reads ───────────────────────────────────────────────────────────────
def get_map_overlays(args: dict) -> ToolResult:
	"""Every operational layer this login may see, over one entity. Read-only."""
	company = resolve_company(as_str(args, "company")) or ""
	allowed = overlays.layers_for(frappe.session.user)
	wanted, refused_layers = overlays.requested_layers(args.get("layers"), allowed["visible"])

	blocks = args.get("blocks") or args.get("block") or None
	if isinstance(blocks, str):
		blocks = [part.strip() for part in blocks.split(",") if part.strip()]

	answer = overlays.build(
		company=company,
		visible=wanted,
		blocks=blocks,
		limit=as_limit(args) if args.get("limit") not in (None, "") else overlays.SUBJECT_CAP,
	)
	answer["role"] = allowed
	answer["withheld"] = allowed["withheld"]
	answer["refused_layers"] = refused_layers

	counts = answer["counts"]
	parts = [f"{counts['blocks']} block(s)"]
	for key, label in (
		("restricted", "restricted"),
		("pre_harvest", "in pre-harvest"),
		("ready_to_pick", "ready to pick"),
		("too_wet", "zone(s) too wet"),
		("access_blocked", "closed to equipment"),
	):
		if counts.get(key):
			parts.append(f"{counts[key]} {label}")
	return ToolResult(
		data=answer,
		summary=f"Map overlays for {company or 'this site'}: {', '.join(parts)}",
	)


def list_soil_compaction_profiles(args: dict) -> ToolResult:
	"""The compaction hour figures, and how much ground each one colours. Read-only."""
	_require_profiles()
	limit = min(as_limit(args), PROFILE_CAP)
	filters: dict = {}
	if as_bool(args, "enabled_only", False):
		filters["enabled"] = 1
	rows = [
		dict(row)
		for row in frappe.db.get_all(
			SOIL_PROFILE,
			filters=filters,
			fields=compat.existing_fields(
				SOIL_PROFILE,
				(
					"name",
					"soil_type",
					"drainage_class",
					"red_hours",
					"yellow_hours",
					"enabled",
					"source",
					"notes",
				),
			),
			order_by="red_hours asc, name asc",
			limit=limit,
		)
		or []
	]
	coverage = _coverage([row.get("name") for row in rows])
	described = [_describe_profile(row, coverage) for row in rows]

	unassigned = 0
	if compat.has_field(FIELD, "soil_profile"):
		try:
			unassigned = frappe.db.count(FIELD, {"soil_profile": ("in", ("", None))})
		except Exception:  # pragma: no cover - a site with no Field register
			unassigned = 0

	return ToolResult(
		data={
			"profiles": described,
			"count": len(described),
			# The two figures a block with no profile is coloured by, reported
			# beside the register rather than left to be looked up in the source.
			"default_red_hours": overlays.DEFAULT_RED_HOURS,
			"default_yellow_hours": overlays.DEFAULT_YELLOW_HOURS,
			# THE NUMBER THAT SAYS WHETHER ANY OF THIS IS WIRED UP. Blocks with no
			# profile are coloured by the shipped default, which is a loam's — and
			# a farm on sand reading a loam's hours is being told to keep off dry
			# ground for a day and a half.
			"blocks_without_profile": unassigned,
		},
		summary=f"{len(described)} soil compaction profile(s); {unassigned} block(s) on the default",
	)


# ── the writes ──────────────────────────────────────────────────────────────
def create_soil_compaction_profile(args: dict) -> ToolResult:
	"""Add a soil this farm has that the shipped book does not."""
	_require_profiles()
	soil_type = as_str(args, "soil_type", required=True)
	if frappe.db.exists(SOIL_PROFILE, soil_type):
		raise ToolError(
			f"A Soil Compaction Profile called {soil_type!r} already exists. "
			"update_soil_compaction_profile changes its hours; nothing was created."
		)
	red = _hours(args, "red_hours")
	yellow = _hours(args, "yellow_hours")
	if red is None or yellow is None:
		raise ToolError(
			"red_hours and yellow_hours are both required. A profile without them colours "
			"nothing, and a blank Float arrives as zero — which claims this soil is never "
			"too wet to drive on. Nothing was created."
		)

	doc = frappe.new_doc(SOIL_PROFILE)
	doc.soil_type = soil_type
	doc.red_hours = red
	doc.yellow_hours = yellow
	doc.drainage_class = as_str(args, "drainage_class")
	doc.source = as_str(args, "source")
	doc.notes = as_str(args, "notes")
	doc.enabled = 1 if as_bool(args, "enabled", True) else 0
	doc.insert()

	return ToolResult(
		data=_describe_profile(doc.as_dict(), {}),
		summary=f"Created Soil Compaction Profile {doc.name} — red under {red}h, yellow under {yellow}h",
		docstatus_delta="none → 0 (saved)",
	)


def update_soil_compaction_profile(args: dict) -> ToolResult:
	"""Replace a shipped figure with this farm's own, or retire a profile."""
	_require_profiles()
	name = as_str(args, "soil_type", required=True)
	if not frappe.db.exists(SOIL_PROFILE, name):
		raise ToolError(
			f"no Soil Compaction Profile called {name!r}. list_soil_compaction_profiles has "
			"the register; create_soil_compaction_profile adds one. Nothing was changed."
		)
	doc = frappe.get_doc(SOIL_PROFILE, name)
	before = {"red_hours": doc.red_hours, "yellow_hours": doc.yellow_hours, "enabled": doc.enabled}

	doc.red_hours = _hours(args, "red_hours", doc.red_hours)
	doc.yellow_hours = _hours(args, "yellow_hours", doc.yellow_hours)
	for key in ("drainage_class", "source", "notes"):
		if key in args and args.get(key) is not None:
			doc.set(key, as_str(args, key))
	enabled = as_bool(args, "enabled", None)
	if enabled is not None:
		doc.enabled = 1 if enabled else 0
	doc.save()

	covered = _coverage([name]).get(name) or []
	changed = [key for key, value in before.items() if str(doc.get(key)) != str(value)]
	return ToolResult(
		data={
			**_describe_profile(doc.as_dict(), {name: covered}),
			"changed": changed,
			# What a colour change actually costs, said at the moment it is made.
			# Editing a profile recolours every block pointing at it on the next
			# map read, and "this affects 14 blocks" is the sentence that stops a
			# typo from being discovered by a tractor.
			"blocks_recoloured": len(covered),
		},
		summary=(
			f"Updated Soil Compaction Profile {name} ({', '.join(changed) or 'no threshold change'}) "
			f"— {len(covered)} block(s) recoloured"
		),
		docstatus_delta="0 → 0 (saved)",
	)


def assign_soil_profile(args: dict) -> ToolResult:
	"""Point one block at the soil profile its ground follows."""
	farm_tools._require(FIELD)
	_require_profiles()
	if not compat.has_field(FIELD, "soil_profile"):
		raise ToolError(
			"This site's Field register has no soil_profile column — run "
			"`bench --site <site> migrate` after upgrading the app. Nothing was changed."
		)
	company = resolve_company(as_str(args, "company")) or ""
	row = farm_tools.field_row(as_str(args, "field", required=True), "", company)

	wanted = as_str(args, "soil_profile")
	clear = as_bool(args, "clear", False)
	if clear:
		profile = ""
	elif wanted:
		if not frappe.db.exists(SOIL_PROFILE, wanted):
			raise ToolError(
				f"no Soil Compaction Profile called {wanted!r}. "
				"list_soil_compaction_profiles has the register. Nothing was changed."
			)
		profile = wanted
	else:
		raise ToolError(
			"soil_profile is required, or pass clear=true to put this block back on the "
			"shipped default. Nothing was changed."
		)

	before = str(row.get("soil_profile") or "")
	if profile and not compat.checked(frappe.db.get_value(SOIL_PROFILE, profile, "enabled")):
		# A refusal rather than a warning. Pointing a block at a retired profile
		# leaves it on the default while the form says otherwise, which is the
		# worst of both: the colour is the fallback's and the record claims a
		# measurement.
		raise ToolError(
			f"Soil Compaction Profile {profile!r} is disabled, so a block pointed at it would "
			"be coloured by the shipped default while its own form claimed otherwise. "
			"Re-enable it with update_soil_compaction_profile, or pick another. "
			"Nothing was changed."
		)

	if as_bool(args, "dry_run", False):
		return ToolResult(
			data={
				"field": row["name"],
				"soil_profile": profile or None,
				"previous_soil_profile": before or None,
				"dry_run": True,
			},
			summary=f"Dry run — {row['name']} would move from {before or 'the default'} to {profile or 'the default'}",
		)

	frappe.db.set_value(FIELD, row["name"], "soil_profile", profile)
	return ToolResult(
		data={
			"field": row["name"],
			"field_name": row.get("field_name") or None,
			"soil_profile": profile or None,
			"previous_soil_profile": before or None,
			"thresholds_source": "profile" if profile else "default",
		},
		summary=(
			f"{row['name']} now follows "
			+ (f"Soil Compaction Profile {profile}" if profile else "the shipped default hours")
		),
		docstatus_delta="0 → 0 (saved)",
	)
