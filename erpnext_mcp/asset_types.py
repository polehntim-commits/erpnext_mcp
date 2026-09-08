# SPDX-License-Identifier: MIT
"""The asset-type register: what it seeds, and how everything else reads it.

v0.162.0. `Farm Asset Type` is the doctype; this is the one module that knows
what ships in it and the one place the rest of the app asks "is this a type, and
what does it look like".

WHY A MODULE AND NOT JUST THE DOCTYPE. Four callers need the answer in three
different shapes — a validator wants a set, a picker wants ordered rows, the map
wants an icon and a colour — and each of them reaching for `frappe.db.get_all`
on its own is how the four lists that existed before this release drifted apart
in the first place.

────────────────────────────────────────────────────────────────────────────
IT DEGRADES TO THE SHIPPED SET, AND THAT IS NOT A CONVENIENCE
────────────────────────────────────────────────────────────────────────────

Every read here answers from `SEEDED` when the doctype is absent. A bench that
has pulled this release and not yet run `bench migrate` has no register, and
`register_asset` on such a bench must keep taking the same thirteen types it took
yesterday rather than refusing every asset on the farm with "no such type". The
same posture `compat` takes everywhere else in this app: a missing migration is
reported where somebody can act on it, never enforced as a refusal on a worker.

────────────────────────────────────────────────────────────────────────────
THE SEED IS THE UNION OF WHAT ALREADY EXISTED, PLUS THE TWO THAT WERE ASKED FOR
────────────────────────────────────────────────────────────────────────────

Thirteen came off `Asset Register.asset_type`'s Select options, which was the
widest of the four lists and therefore the only one that could not strand an
asset already on a site. `Fuel Tank` and `Gas Tank` are new and were asked for.

THE BRIEF FOR THIS RELEASE NAMED SEVEN TYPES TO SEED and eight of the thirteen
were not among them. Seeding only the seven would have left every asset of a
missing type — Sprayer, Implement, Vehicle, Block, Water Source, Cold Storage,
Housing Unit, Irrigation Zone — pointing at a Link target that does not exist,
which in the Desk is an asset that cannot be opened or saved. The register is
authoritative about what a site HAS; the brief was describing what a farm USES.

`migrate_asset_types` seeds this list AND every distinct value it finds in the
register, which is the half that makes the migration safe on a site this app has
never seen.
"""

from __future__ import annotations

import frappe

from . import compat

DOCTYPE = "Farm Asset Type"
ASSET_REGISTER = "Asset Register"

#: What ships. `(type_name, icon, display_order, description)`.
#:
#: THE ICON IS ONE LETTER, and the map takes its first character. Distinct
#: within the set where it can be — a Tractor and a Storage shed share no letter
#: — and where two types start alike the second takes a letter from further in
#: the word, because two identical badges on one map is worse than an
#: unmemorable one.
#:
#: THE ORDER IS WHAT A FARM REACHES FOR, not alphabetical. Irrigation valves are
#: the overwhelming majority of a tagged orchard's register — 33 of the 41 rows
#: on the site this was written against — so they are first, and General, which
#: is what somebody picks when none of the others fit, is last.
SEEDED: tuple[tuple[str, str, int, str], ...] = (
	("Irrigation Valve", "V", 10, "A valve on a line. Opens, closes and winterizes."),
	("Irrigation Zone", "Z", 20, "A block of ground under one set of valves."),
	("Water Source", "W", 30, "A well, a pond or a district turnout."),
	("Tractor", "T", 40, "A tractor. Carries hours and a service schedule."),
	("Implement", "I", 50, "Something a tractor pulls."),
	("Sprayer", "P", 60, "A sprayer. Full or empty, and rinsed."),
	("Vehicle", "U", 70, "A truck, a quad or a mule."),
	("Wind Machine", "M", 80, "Frost protection. Runs on cold nights."),
	("Fuel Tank", "F", 90, "Bulk fuel. Diesel or gasoline in a yard tank."),
	("Gas Tank", "G", 100, "Bottled or bulk gas — propane, LPG, welding stock."),
	("Storage", "S", 110, "A barn, a shop or a shed."),
	("Cold Storage", "C", 120, "Refrigerated storage. Carries a temperature record."),
	("Block", "B", 130, "A farmed block, tagged so a scan reaches it."),
	("Housing Unit", "H", 140, "A cabin or a bunkhouse, tagged in the field."),
	("General", "A", 200, "Anything the other types do not describe."),
)

#: The type names alone, in seed order.
SEEDED_NAMES: tuple[str, ...] = tuple(row[0] for row in SEEDED)

#: What a type with no icon of its own is drawn as. NOT a blank: a pin with no
#: glyph is a pin somebody cannot tell from any other pin, and a type an operator
#: invented is exactly the one they are looking for on the map.
DEFAULT_ICON = "A"


def available() -> bool:
	"""Whether this site has the register yet. False on a bench mid-migrate."""
	return compat.doctype_exists(DOCTYPE)


def rows(*, enabled_only: bool = True) -> list[dict]:
	"""Every asset type, in picker order, as plain dicts.

	ORDERED BY `display_order` THEN `type_name`, and the tie-break is the half
	that matters. A Frappe Int column is `NOT NULL DEFAULT 0`, so a register
	whose operator has never touched `display_order` has fifteen rows all
	claiming to be first — without the second key they would come back in
	whatever order the table happened to hold them, which is creation order and
	looks like a bug.
	"""
	if not available():
		return [
			{
				"name": name,
				"type_name": name,
				"icon": icon,
				"display_order": order,
				"description": detail,
				"enabled": True,
			}
			for name, icon, order, detail in SEEDED
		]
	filters = {"enabled": 1} if enabled_only else {}
	found = (
		frappe.db.get_all(
			DOCTYPE,
			filters=filters,
			fields=compat.existing_fields(
				DOCTYPE, ("name", "type_name", "icon", "display_order", "description", "enabled")
			),
			order_by="display_order asc, type_name asc",
			limit=1000,
		)
		or []
	)
	out = []
	for row in found:
		row = dict(row)
		row["enabled"] = bool(row.get("enabled"))
		row["icon"] = str(row.get("icon") or "").strip()
		row["display_order"] = int(row.get("display_order") or 0)
		out.append(row)
	return out


def names(*, enabled_only: bool = True) -> list[str]:
	"""Every asset type's docname, in picker order."""
	return [str(row["name"]) for row in rows(enabled_only=enabled_only)]


def exists(asset_type) -> bool:
	"""Whether this exact string is a type on this site. Retired types count.

	ENABLED IS NOT CHECKED HERE, DELIBERATELY. `enabled` governs what a PICKER
	offers; it must not govern whether an asset that already carries a retired
	type can be read, saved or re-scanned. A farm that retires 'Sprayer' after
	selling the sprayers still has last season's spray records pointing at one.
	`register_asset` checks enablement separately, because CREATING a new asset
	of a retired type is the case the flag is actually about.
	"""
	wanted = str(asset_type or "").strip()
	if not wanted:
		return False
	if not available():
		return wanted in SEEDED_NAMES
	return bool(frappe.db.exists(DOCTYPE, wanted))


def is_enabled(asset_type) -> bool:
	"""Whether this type is one a picker should still offer."""
	wanted = str(asset_type or "").strip()
	if not wanted:
		return False
	if not available():
		return wanted in SEEDED_NAMES
	value = frappe.db.get_value(DOCTYPE, wanted, "enabled")
	return bool(value)


def icon_for(asset_type) -> str:
	"""The glyph a map badge carries for this type, or the fallback.

	FIRST CHARACTER, because the badge is a circle 22 pixels across. The column
	holds whatever an operator or a handset finds useful — a letter, an SF Symbol
	name — and each client takes what it can draw.
	"""
	wanted = str(asset_type or "").strip()
	if not wanted:
		return DEFAULT_ICON
	if available():
		stored = str(frappe.db.get_value(DOCTYPE, wanted, "icon") or "").strip()
		if stored:
			return stored[:1].upper()
	for name, icon, _order, _detail in SEEDED:
		if name == wanted:
			return icon
	# A type an operator invented and gave no icon. Its own first letter beats a
	# generic badge: 'Cider Press' reads as C, which is at least the right thing.
	return (wanted[:1] or DEFAULT_ICON).upper()


def seed(*, extra: list | None = None) -> dict:
	"""Create every shipped type that is not there, plus anything `extra` names.

	IT ONLY EVER CREATES WHAT IS ABSENT, by docname — the same contract
	`_employment_types` and `_i9_document_types` keep, and the reason
	`test_hooks.py` forbids the word `fixtures`. An operator who renamed a type,
	retired one, reordered the picker or rewrote a description keeps every one of
	those decisions through every later migrate. Nothing here updates a row that
	exists.

	`extra` IS WHAT MAKES THE MIGRATION SAFE ON A SITE THIS APP HAS NEVER SEEN.
	`migrate_asset_types` passes every distinct `asset_type` already in the
	register, so a farm that added a Select option by hand — or one running a
	build whose options list has since changed — gets a master for the value its
	assets actually carry, rather than a Link pointing at nothing.

	Never raises: it runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench.
	"""
	report = {"created": [], "present": [], "failed": [], "skipped": ""}
	if not available():
		report["skipped"] = (
			f"this site has no {DOCTYPE} DocType yet — it ships with erpnext_mcp v0.162.0, "
			"so run `bench --site <site> migrate` again"
		)
		return report

	wanted = list(SEEDED)
	known = {row[0] for row in SEEDED}
	for name in extra or ():
		text = " ".join(str(name or "").split()).strip()
		if not text or text in known:
			continue
		known.add(text)
		# AN INHERITED TYPE GETS NO DESCRIPTION AND NO ORDER, because this app
		# does not know what it means. It gets its own first letter as an icon
		# and sorts after everything shipped, which is where an operator will
		# find it to fill the rest in.
		wanted.append((text, text[:1].upper(), 500, ""))

	for name, icon, order, detail in wanted:
		try:
			if frappe.db.exists(DOCTYPE, name):
				report["present"].append(name)
				continue
			doc = frappe.new_doc(DOCTYPE)
			doc.type_name = name
			doc.icon = icon
			doc.display_order = order
			doc.description = detail
			doc.enabled = 1
			doc.flags.ignore_permissions = True
			doc.insert(ignore_permissions=True)
			report["created"].append(name)
		except Exception as exc:  # pragma: no cover - a site mid-migrate
			report["failed"].append({"type_name": name, "reason": f"{type(exc).__name__}: {exc}"})
	return report


def distinct_in_register() -> list[str]:
	"""Every asset_type value the register actually holds, however odd.

	Read straight off the column rather than through `list_assets`, because the
	tool filters retired assets out by default and a retired asset's type still
	has to have a master or the row cannot be opened.
	"""
	if not compat.doctype_exists(ASSET_REGISTER):
		return []
	found = set()
	for row in frappe.db.get_all(ASSET_REGISTER, fields=["asset_type"], limit=100000) or []:
		text = " ".join(str(row.get("asset_type") or "").split()).strip()
		if text:
			found.add(text)
	return sorted(found)


def require(asset_type, label: str = "asset_type", *, creating: bool = False) -> str:
	"""One asset type, proved to be on this site's register, or a refusal naming them.

	THE REFUSAL LISTS WHAT THERE IS, which is the whole ergonomic difference
	between a Link and a Select. A Select refused with "must be one of: ..." and
	the list was the truth; a bare Frappe link error says "Could not find Farm
	Asset Type: Tracter" and leaves the caller guessing whether they typed it
	wrong or the site has not got it.

	IT IS NOT `ensure_type`. `training.ensure_type` creates a curriculum from free
	text on purpose — curricula are an open vocabulary. Asset types are CLOSED
	here, because `_STATE_DEFINITIONS`, `ASSET_TYPE_SKILL_MAP` and the action menu
	are keyed on them: a typo that silently created 'Tracter' would give that
	asset no state machine, no skill and no actions, and nothing anywhere would
	say so. Adding a type is a deliberate act in the Desk, and the refusal below
	says so.

	`creating` IS THE ONLY PLACE `enabled` BITES. A retired type must still be
	readable, savable and re-scannable on the assets that already carry it — a
	farm that sold its sprayers still has last season's spray records — so this
	only refuses a retired type when a NEW asset is being registered as one.
	"""
	wanted = " ".join(str(asset_type or "").split()).strip()
	if not wanted:
		raise ValueError(f"{label} is required.")
	if exists(wanted):
		if creating and not is_enabled(wanted):
			raise ValueError(
				f"{wanted!r} is a retired asset type on this site, so no new asset is "
				f"registered as one. Tick Enabled on the {DOCTYPE} record if it is back in "
				f"use, or pick another type: {', '.join(names()) or 'none are enabled'}. "
				"Nothing was created."
			)
		return wanted

	# CASE-INSENSITIVE SECOND LOOK, and only to make the refusal better. It does
	# NOT accept the value: 'tractor' resolving silently to 'Tractor' would be
	# this app deciding what an operator meant, and the docname is what every
	# asset stores. Naming the near-miss is the useful half.
	folded = wanted.casefold()
	near = [name for name in names(enabled_only=False) if name.casefold() == folded]
	suggestion = f" Did you mean {near[0]!r}?" if near else ""
	raise ValueError(
		f"{wanted!r} is not an asset type on this site.{suggestion} The register has: "
		f"{', '.join(names()) or 'nothing yet — run bench migrate'}. A new kind of asset is a "
		f"{DOCTYPE} record somebody creates in the Desk, not a typo this accepts."
	)
