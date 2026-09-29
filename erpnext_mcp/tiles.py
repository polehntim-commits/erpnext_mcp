# SPDX-License-Identifier: MIT
"""Server-driven tiles on Today, Work and the asset-scan screen. v0.207.0.

docs/design/phone_config_and_compliance_loop.md §3. A tile is a Farm Config
Version of kind Tile: a title, an icon from a fixed list, a target from a fixed
list of kinds, an audience, an order, an optional badge from an allowlisted
query, show-if conditions and a minimum app version. Nothing in a tile is code.
"""

from __future__ import annotations

import frappe

from . import compat, flags, phone_config

SURFACES = ("today", "work", "asset_scan")
MAX_PER_SURFACE = 24
REPORTS = (
	"compliance_inbox",
	"my_tasks",
	"available_tasks",
	"my_inspections",
	"compliance",
	"farm_dashboard",
	"my_feedback",
)
DOCUMENT_DOCTYPES = ("Farm Task", "Inspection Session", "Asset Register", "Item", "App Feedback")
TARGET_KINDS = ("wizard", "task_template", "inspection_template", "report", "list_query", "document")
FALLBACK_ICON = "square.grid.2x2"

#: The SF Symbols a tile may name. The phone compiles the same list and falls
#: back to FALLBACK_ICON for anything else (§3.4).
ICONS = (
	"square.grid.2x2",
	"tray.full",
	"tray",
	"exclamationmark.shield",
	"exclamationmark.triangle",
	"checklist",
	"checklist.checked",
	"list.bullet.clipboard",
	"doc.text",
	"doc.text.magnifyingglass",
	"calendar",
	"calendar.badge.exclamationmark",
	"hammer",
	"wrench.and.screwdriver",
	"drop",
	"drop.fill",
	"leaf",
	"ant",
	"hare",
	"testtube.2",
	"aqi.medium",
	"person.badge.shield.checkmark",
	"person.badge.clock",
	"person.crop.circle.badge.plus",
	"graduationcap",
	"graduationcap.fill",
	"bubble.left.and.text.bubble.right",
	"shippingbox",
	"basket",
	"basket.fill",
	"tractor.fill",
	"map",
	"mappin.and.ellipse",
	"qrcode.viewfinder",
	"binoculars",
	"bubbles.and.sparkles",
	"signature",
	"sun.max.fill",
	"sun.horizon",
	"bell",
	"bell.badge.fill",
	"building.2",
	"house",
	"flame",
	"bolt",
	"thermometer.sun",
	"cross.case",
	"plus.circle",
)
SEASONS = ("in", "off")
OCCUPANCY = ("Occupied", "Unoccupied")


# ── validation ──────────────────────────────────────────────────────────────
def validate(body: dict, *, key: str = "", for_publish: bool = False) -> dict:
	errors: list = []
	warnings: list = []
	surface = body.get("surface")
	if surface not in SURFACES:
		errors.append(f"surface must be one of {', '.join(SURFACES)}")
	if not str((body.get("title") or {}).get("en") or "").strip():
		errors.append("title.en is required")
	if body.get("icon") not in ICONS:
		errors.append(f"icon {body.get('icon')!r} is not on the allowlist (tiles.ICONS)")
	if not isinstance(body.get("order", 0), int):
		errors.append("order must be an integer")
	errors += target_problems(body.get("target"), body.get("audience"))
	errors += phone_config.audience_problems(body.get("audience") or {})
	badge = body.get("badge")
	if badge is not None:
		from . import tile_queries

		errors += tile_queries.problems(badge, body.get("audience") or {})
	errors += _show_if_problems(body.get("show_if") or {}, surface)
	if body.get("min_app_version") and flags.parse_version(body["min_app_version"]) is None:
		errors.append(f"min_app_version {body['min_app_version']!r} is not a version like 0.21.0")
	lang_errors, lang_warnings = phone_config.language_findings(body, for_publish)
	return {"errors": errors + lang_errors, "warnings": warnings + lang_warnings}


def target_problems(target, audience) -> list:
	if not isinstance(target, dict) or target.get("kind") not in TARGET_KINDS:
		return [f"target.kind must be one of {', '.join(TARGET_KINDS)}"]
	kind = target["kind"]
	if kind == "wizard":
		return [] if _wizard_live(target.get("wizard")) else [f"no live wizard {target.get('wizard')!r}"]
	if kind == "task_template":
		return _template_problems(str(target.get("template") or ""), audience)
	if kind == "inspection_template":
		name = inspection_template(target.get("template"))
		return [] if name else [f"no active Inspection Template {target.get('template')!r}"]
	if kind == "report":
		return [] if target.get("report") in REPORTS else [f"report must be one of {', '.join(REPORTS)}"]
	if kind == "list_query":
		from . import tile_queries

		return tile_queries.problems(
			{"query": target.get("query"), "params": target.get("params") or {}}, audience or {}
		)
	doctype = target.get("doctype")
	if doctype not in DOCUMENT_DOCTYPES:
		return [f"document doctype must be one of {', '.join(DOCUMENT_DOCTYPES)}"]
	if not frappe.db.exists(doctype, str(target.get("name") or "")):
		return [f"no {doctype} {target.get('name')!r}"]
	return []


def _wizard_live(key) -> bool:
	key = str(key or "")
	if not key:
		return False
	if phone_config.rows("Wizard", key, (phone_config.STAGED, phone_config.PUBLISHED)):
		return True
	if compat.doctype_exists("Wizard Definition"):
		return bool(frappe.db.get_value("Wizard Definition", key, "enabled"))
	return False


def _template_problems(name: str, audience) -> list:
	if not name or not frappe.db.exists("Farm Task Template", name):
		return [f"no Farm Task Template {name!r}"]
	row = (
		frappe.db.get_value(
			"Farm Task Template", name, ["enabled", "form_schema", "required_certification"], as_dict=True
		)
		or {}
	)
	out = []
	if not compat.checked(row.get("enabled")):
		out.append(f"Farm Task Template {name!r} is disabled")
	from . import form_schema

	report = form_schema.validate(row.get("form_schema") or [])
	out += [f"{name}: {f['path']}: {f['message']}" for f in report["errors"]]
	cert = str(row.get("required_certification") or "").strip()
	if cert:
		holders = phone_config.audience_people({**(audience or {}), "certifications": [cert]})
		if not holders:
			out.append(f"{name} requires {cert!r} and nobody in the tile's audience holds it")
	return out


def inspection_template(value) -> str:
	"""The live Inspection Template docname for a docname or template name, or ''."""
	value = str(value or "").strip()
	if not value or not compat.doctype_exists("Inspection Template"):
		return ""
	from . import sessions

	name = sessions.resolve_template(value) or ""
	if name and compat.checked(frappe.db.get_value("Inspection Template", name, "active")):
		return name
	return ""


def _show_if_problems(show_if, surface) -> list:
	if not isinstance(show_if, dict):
		return ["show_if must be an object"]
	out = []
	unknown = set(show_if) - {"flag", "season", "occupancy", "asset_types"}
	if unknown:
		out.append(f"show_if has unknown keys: {', '.join(sorted(unknown))}")
	if show_if.get("flag") and not flags.KEY_PATTERN.match(str(show_if["flag"])):
		out.append("show_if.flag must be a flag key")
	if show_if.get("season") and show_if["season"] not in SEASONS:
		out.append("show_if.season is 'in' or 'off'")
	if show_if.get("occupancy") and show_if["occupancy"] not in OCCUPANCY:
		out.append("show_if.occupancy is Occupied or Unoccupied")
	if (show_if.get("occupancy") or show_if.get("asset_types")) and surface != "asset_scan":
		out.append("show_if.occupancy and asset_types apply to the asset_scan surface only")
	return out


# ── serving ─────────────────────────────────────────────────────────────────
def _asset_context(asset) -> dict:
	if not asset:
		return {}
	name = str(asset)
	for doctype in ("Asset Register", "Housing Unit"):
		if compat.doctype_exists(doctype) and frappe.db.exists(doctype, name):
			from . import occupancy

			kind = frappe.db.get_value(
				doctype, name, "asset_type" if doctype == "Asset Register" else "unit_type"
			)
			occupied = occupancy.occupancy(doctype, name).get("occupied")
			return {"doctype": doctype, "name": name, "asset_type": kind or "", "occupied": bool(occupied)}
	return {"doctype": "", "name": name, "asset_type": "", "occupied": False}


def hidden_reason(body: dict, person: dict, company: str, app_version, asset: dict) -> str:
	"""Why a tile is not shown to this person, or ''."""
	if not phone_config.matches(person, body.get("audience")):
		return "audience"
	minimum = body.get("min_app_version")
	if minimum:
		mine = flags.parse_version(app_version)
		if mine is None or mine < flags.parse_version(minimum):
			return f"needs app {minimum}"
	show_if = body.get("show_if") or {}
	if show_if.get("flag") and not flags.enabled(
		show_if["flag"], company, person.get("roles") or (), app_version, user=person["user"]
	):
		return f"flag {show_if['flag']} is off"
	if show_if.get("season"):
		from . import occupancy

		inside = occupancy.in_season(company)
		if (show_if["season"] == "in") != inside:
			return f"season is {'in' if inside else 'off'}"
	if show_if.get("occupancy"):
		occupied = bool(asset.get("occupied"))
		if (show_if["occupancy"] == "Occupied") != occupied:
			return "occupancy"
	if show_if.get("asset_types") and asset.get("asset_type") not in show_if["asset_types"]:
		return "asset type"
	return ""


def for_user(user: str, surface: str, app_version=None, asset=None, company: str = "", explain=False) -> list:
	"""The tiles `get_tiles` answers, in order, with badges. `explain` adds hidden ones."""
	from . import tile_queries

	person = phone_config.person_of(user)
	company = company or (person.get("companies") or [""])[0]
	context = _asset_context(asset) if surface == "asset_scan" else {}
	shown, hidden = [], []
	for doc, body in phone_config.served("Tile", user, company, person.get("roles") or (), app_version):
		if body.get("surface") != surface:
			continue
		reason = hidden_reason(body, person, company, app_version, context)
		if reason:
			hidden.append(
				{"key": doc.config_key, "config_version": phone_config.version_string(doc), "hidden": reason}
			)
			continue
		shown.append(
			{
				"key": doc.config_key,
				"config_version": phone_config.version_string(doc),
				"title": body.get("title"),
				"subtitle": body.get("subtitle"),
				"icon": body.get("icon") if body.get("icon") in ICONS else FALLBACK_ICON,
				"order": int(body.get("order") or 0),
				"target": body.get("target"),
				"badge": tile_queries.badge(body.get("badge"), user, company),
				"stale": False,
			}
		)
	shown.sort(key=lambda row: (row["order"], row["key"]))
	if len(shown) > MAX_PER_SURFACE:
		hidden += [
			{"key": row["key"], "hidden": "over the per-surface cap"} for row in shown[MAX_PER_SURFACE:]
		]
		shown = shown[:MAX_PER_SURFACE]
	return (shown, hidden) if explain else shown


#: What install seeds (create-only, version 1 Published).
SEEDS = {
	"compliance_inbox": {
		"surface": "today",
		"title": {"en": "Compliance inbox", "es": "Pendientes de cumplimiento"},
		"subtitle": {"en": "Due, overdue, blocked", "es": "Por vencer, vencidos, bloqueados"},
		"icon": "tray.full",
		"order": 10,
		"target": {"kind": "report", "report": "compliance_inbox"},
		"audience": {},
		"badge": {"query": "compliance_inbox", "params": {}},
		"show_if": {},
		"min_app_version": "0.21.0",
	},
	"my_feedback": {
		"surface": "today",
		"title": {"en": "My notes and replies", "es": "Mis notas y respuestas"},
		"subtitle": {"en": "What the farm answered", "es": "Lo que respondió la granja"},
		"icon": "bubble.left.and.text.bubble.right",
		"order": 90,
		"target": {"kind": "report", "report": "my_feedback"},
		"audience": {},
		"badge": {"query": "my_feedback_answered", "params": {}},
		"show_if": {},
		"min_app_version": "0.21.0",
	},
}


def seed() -> list:
	made = []
	for key, body in SEEDS.items():
		name = phone_config.seed("Tile", key, body, "Built-in tile, seeded at install (v0.207.0).")
		if name:
			made.append(name)
	return made
