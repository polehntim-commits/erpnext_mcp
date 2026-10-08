# SPDX-License-Identifier: MIT
"""The shop queue — work to do indoors when the weather says no. v0.273.0 (docs/contracts/shop_queue_v0_273.yaml).

THE BACKLOG IS DATA, NOT A NEW REGISTER. A shop item is an open Farm Task that is shop work:
  * raised from a template ticked Shop work (sharpen the pruning tools, rebuild sprinkler heads, bin repair …);
  * a repair or service on a machine (an asset issue someone reported, a service that came due);
  * a seasonal winterize / start-up task — those rank first: the freeze does not wait for a dry day;
  * training (a video and its knowledge check is shop-day work).
Things that are DUE but not yet a task are SUGGESTIONS beside it — a machine whose service is due, reorder alerts,
tags waiting to be printed — and one call (`add_shop_item`) turns a suggestion into a task.

A WEATHER DAY is read from today's forecast at the farm: rain likely (chance or amount), wind, or a day that stays
below freezing — each threshold a per-company flag. A person can also declare a shop day (smoke, a burn ban, a
breakdown) with the `shop_day_declared` flag set to the date. On a weather day the plan is the backlog in order
(winterize first, then urgency, due date, age) filled up to the hours there are (`shop_day_hours`, default 8 per
person on the list). Skilled jobs carry a "pair a learner with someone who knows it" note: a learner is whoever holds
the `shop_learner` skill.

The evening notice ("tomorrow looks like a shop day") is OFF until `shop_evening_notice` is on for a company; it is
an Info alert for the foreman, never a message sent to the crew.
"""

from __future__ import annotations

import datetime

import frappe

from . import compat

TASK = "Farm Task"
TEMPLATE = "Farm Task Template"
OPEN_STATES = ("Available", "Claimed", "In-Progress", "Paused")
SHOP_TASK_TYPES = ("Repair", "Maintenance", "Training")
SKILLS = ("Any", "Learner OK", "Skilled")
LEARNER_SKILL = "shop_learner"
URGENCY = {"Critical": 0, "Urgent": 0, "High": 1, "Normal": 2, "Low": 3}
#: The thresholds a company can change (Farm Feature Flags, Threshold), and their defaults.
THRESHOLDS = {"shop_day_rain_pct": 60.0, "shop_day_rain_in": 0.10, "shop_day_wind_mph": 20.0,
              "shop_day_gust_mph": 30.0, "shop_day_cold_f": 32.0, "shop_day_hours": 8.0, "shop_evening_hour": 17.0}
DECLARED = "shop_day_declared"
EVENING = "shop_evening_notice"


def _flag(key: str, company: str):
	from . import flags

	return flags.value(key, company=company, default=THRESHOLDS.get(key))


def threshold(key: str, company: str) -> float:
	try:
		return float(_flag(key, company))
	except (TypeError, ValueError):
		return float(THRESHOLDS[key])


# ── the backlog ──────────────────────────────────────────────────────────────
def shop_templates() -> dict:
	"""{template docname: {shop_skill, tools_parts, safety_note, template_name}} for templates ticked Shop work."""
	if not compat.has_field(TEMPLATE, "shop_work"):
		return {}
	fields = compat.existing_fields(TEMPLATE, ("name", "template_name", "shop_skill", "tools_parts", "safety_note",
	                                           "estimated_duration_minutes", "enabled"))
	return {r["name"]: dict(r) for r in frappe.db.get_all(TEMPLATE, filters={"shop_work": 1}, fields=fields,
	                                                      limit=2000) or []}


def _is_field(task: dict) -> bool:
	return str(task.get("location_doctype") or "") == "Field"


def _kind(task: dict, templates: dict) -> str | None:
	if str(task.get("source_workorder") or "").startswith("seasonal:"):
		return "seasonal"
	if task.get("template") in templates:
		return "shop"
	if task.get("task_type") in ("Repair", "Maintenance") and task.get("asset") and not _is_field(task):
		return "machine"
	if task.get("task_type") == "Training":
		return "training"
	return None


def backlog(company: str) -> list:
	"""Open shop work for one company, in the order it should be done."""
	templates = shop_templates()
	fields = compat.existing_fields(TASK, ("name", "task_name", "task_type", "template", "asset", "location",
	                                       "location_doctype", "state", "urgency", "due_date", "creation",
	                                       "estimated_duration_minutes", "assigned_to", "source_workorder",
	                                       "skill_required"))
	rows = frappe.db.get_all(TASK, filters={"company": company, "state": ("in", list(OPEN_STATES))}, fields=fields,
	                         limit=5000) or []
	out = []
	for raw in rows:
		task = dict(raw)
		kind = _kind(task, templates)
		if not kind:
			continue
		meta = templates.get(task.get("template")) or {}
		out.append({
			"task": task["name"], "title": task.get("task_name"), "kind": kind, "task_type": task.get("task_type"),
			"asset": task.get("asset"), "state": task.get("state"), "urgency": task.get("urgency") or "Normal",
			"due_date": str(task.get("due_date") or "") or None,
			"minutes": int(task.get("estimated_duration_minutes") or meta.get("estimated_duration_minutes") or 60),
			"skill": meta.get("shop_skill") or ("Skilled" if kind == "machine" else "Any"),
			"tools_parts": meta.get("tools_parts") or None, "safety_note": meta.get("safety_note") or None,
			"assigned_to": task.get("assigned_to"), "created": str(task.get("creation") or "")[:10] or None,
		})
	rank = {"seasonal": 0, "machine": 1, "shop": 2, "training": 3}
	out.sort(key=lambda r: (rank[r["kind"]], URGENCY.get(r["urgency"], 2), r["due_date"] or "9999-12-31",
	                        r["created"] or ""))
	return out


def suggestions(company: str) -> list:
	"""Due but not yet a task: machines whose service is due, reorder alerts, tags waiting to print."""
	out = []
	open_assets = {r.get("asset") for r in frappe.db.get_all(
		TASK, filters={"company": company, "state": ("in", list(OPEN_STATES)), "asset": ("is", "set")},
		fields=["asset"], limit=5000) or []}
	try:
		from .tools import maintenance

		due = maintenance.check_maintenance_due({"company": company}).data or {}
		for row in [*(due.get("due") or []), *(due.get("due_soon") or [])]:
			asset = row.get("asset_name")
			if asset and asset not in open_assets:
				out.append({"kind": "maintenance_due", "ref": asset, "title": f"Service — {asset}",
				            "why": row.get("message") or "service due", "template": SERVICE_TEMPLATE["template_name"]})
	except Exception:  # a register this site does not have is not a reason to lose the backlog
		pass
	try:
		from .tools import stock_inventory

		alerts = stock_inventory.list_reorder_alerts({"company": company}).data or {}
		short = [a for a in alerts.get("alerts") or alerts.get("rows") or [] if a.get("item_code")]
		if short:
			out.append({"kind": "reorder", "ref": ",".join(sorted({a["item_code"] for a in short}))[:140],
			            "title": f"Count and reorder — {len(short)} item(s) low", "why": "below the reorder level",
			            "template": COUNT_TEMPLATE["template_name"]})
	except Exception:
		pass
	try:
		from . import card_print

		queue = card_print.list_tag_queue("Administrator", [company]) or {}  # the system reads it; the list shows a count
		if int(queue.get("count") or 0):
			out.append({"kind": "tags", "ref": "tag queue", "title": f"Print and hang {queue['count']} tag(s)",
			            "why": "tags waiting to print", "template": TAGS_TEMPLATE["template_name"]})
	except Exception:
		pass
	return out


def add_item(company: str, template: str, *, title: str = "", asset: str = "", notes: str = "", actor: str = "") -> dict:
	"""One shop task from a template (a suggestion, or anything somebody thinks of)."""
	from .tools import tasktemplates

	row = frappe.db.get_value(TEMPLATE, {"template_name": template}, ["name", "enabled"], as_dict=True) or (
		frappe.db.get_value(TEMPLATE, template, ["name", "enabled"], as_dict=True) if frappe.db.exists(TEMPLATE, template)
		else None)
	if not row:
		raise ValueError(f"no Farm Task Template {template!r}.")
	if asset and not frappe.db.exists("Asset Register", asset):
		raise ValueError(f"no asset {asset!r}.")
	result = tasktemplates.create_task_from_template(
		{"template": row["name"], "company": company, **({"task_name": title} if title else {}),
		 **({"notes": notes} if notes else {})},
		origin="foreman_dispatch", fields={"asset": asset} if asset else {})
	return {"task": (result.data or {}).get("name"), "template": row["name"], "by": actor or None}


# ── the weather day ──────────────────────────────────────────────────────────
def _forecast(company: str):
	from . import ccf_providers, seasonal

	point = seasonal.company_point(company)
	if not point or not ccf_providers.forecast_enabled():
		return None
	return ccf_providers.fetch_forecast(*point) or {}


def weather_day(company: str, day: str = "", forecast=None) -> dict:
	"""{shop_day, reasons, forecast_day} for a date (default today). `forecast` for tests."""
	day = day or str(frappe.utils.today())
	declared = str(_flag(DECLARED, company) or "").strip()
	reasons = []
	if declared and declared.split()[0] == day:
		reasons.append(f"declared a shop day ({declared})")
	if forecast is None:
		forecast = _forecast(company)
	row = next((d for d in ((forecast or {}).get("forecast") or {}).get("daily") or [] if str(d.get("date")) == day),
	           None)
	if row:
		def over(key, limit):
			value = row.get(key)
			return value is not None and float(value) >= limit

		if over("precip_prob_pct", threshold("shop_day_rain_pct", company)):
			reasons.append(f"rain likely ({row['precip_prob_pct']:g}%)")
		elif over("precip_in", threshold("shop_day_rain_in", company)):
			reasons.append(f"rain {row['precip_in']:g} in")
		if over("wind_mph", threshold("shop_day_wind_mph", company)):
			reasons.append(f"wind {row['wind_mph']:g} mph")
		elif over("gust_mph", threshold("shop_day_gust_mph", company)):
			reasons.append(f"gusts {row['gust_mph']:g} mph")
		if row.get("tmax_f") is not None and float(row["tmax_f"]) <= threshold("shop_day_cold_f", company):
			reasons.append(f"high of {row['tmax_f']:g} °F")
	return {"date": day, "shop_day": bool(reasons), "reasons": reasons, "forecast_day": row,
	        "forecast": "none" if row is None else "used"}


def learners(company: str) -> list:
	"""Employees whose skill field lists `shop_learner` — the same field the qualification check reads."""
	if not compat.doctype_exists("Employee"):
		return []
	from .tools import fieldwork

	field = compat.first_field("Employee", *fieldwork._SKILL_FIELDS)
	if not field:
		return []
	rows = frappe.db.get_all("Employee", filters={"company": company, "status": "Active"},
	                         fields=["name", "employee_name", field], limit=2000) or []
	return sorted(str(r.get("employee_name") or r["name"]) for r in rows
	              if LEARNER_SKILL in {t.strip().casefold() for t in str(r.get(field) or "").replace(";", ",")
	                                   .replace("\n", ",").split(",")})


def day_plan(company: str, day: str = "", people: int = 0, forecast=None) -> dict:
	"""The shop list for a day: the backlog in order, filled to the hours there are."""
	weather = weather_day(company, day, forecast)
	hours = threshold("shop_day_hours", company) * max(1, int(people or 1))
	budget = int(hours * 60)
	picked, used = [], 0
	items = backlog(company)
	pairs = learners(company)
	for item in items:
		if used + item["minutes"] > budget and picked:
			continue
		used += item["minutes"]
		if item["skill"] == "Skilled":
			item["pairing"] = ("Pair a learner with someone who knows this job" +
			                   (f" ({', '.join(pairs[:3])})" if pairs else "") + ".")
		elif item["skill"] == "Learner OK":
			item["pairing"] = "A learner can take this one."
		picked.append(item)
	return {**weather, "company": company, "hours": hours, "minutes_planned": used, "items": picked if
	        weather["shop_day"] else [], "would_do": picked, "backlog_count": len(items),
	        "suggestions": suggestions(company)}


# ── the evening notice ───────────────────────────────────────────────────────
def hourly(now=None, forecast_for=None) -> dict:
	"""At `shop_evening_hour`, for each company with the notice on: tomorrow a shop day? One Info alert a day."""
	from . import flags

	now = now or frappe.utils.get_datetime(frappe.utils.now())
	made = {}
	for company in [r["name"] for r in frappe.db.get_all("Company", fields=["name"], limit=200) or []]:
		if not flags.value(EVENING, company=company, default=False):
			continue
		if int(now.hour) != int(threshold("shop_evening_hour", company)):
			continue
		tomorrow = (now.date() + datetime.timedelta(days=1)).isoformat()
		plan = day_plan(company, tomorrow, forecast=forecast_for(company) if forecast_for else None)
		if plan["shop_day"]:
			made[company] = _notice(company, tomorrow, plan)
	return made


def _notice(company: str, day: str, plan: dict) -> str | None:
	if not compat.doctype_exists("Compliance Alert"):
		return None
	from .alerts import base as alerts

	key = alerts.alert_key("shop_day_tomorrow", "Company", f"{company}:{day}")
	if frappe.db.exists("Compliance Alert", key):
		return key
	alert = frappe.new_doc("Compliance Alert")
	alert.alert_key = key
	alert.alert_type = "shop_day_tomorrow"
	alert.severity = "Info"
	alert.category = "Other"
	alert.company = company
	alert.source_doctype = "Company"
	alert.source_docname = company
	top = "; ".join(i["title"] or i["task"] for i in plan["would_do"][:5])
	alert.alert_message = (f"Tomorrow ({day}) looks like a shop day — {', '.join(plan['reasons'])}. "
	                       f"{len(plan['would_do'])} shop job(s) ready: {top}.")[:1000]
	alert.first_seen = frappe.utils.today()
	alert.last_refreshed = frappe.utils.now()
	alert.insert(ignore_permissions=True)
	return alert.name


# ── the starter shop templates (Mid-Columbia), DISABLED until somebody enables them ─────────────────────────
def _t(name, es, task_type, minutes, skill, tools, safety, description, checklist, enabled=0):
	return {"template_name": name, "title_es": es, "task_type": task_type, "description": description,
	        "instructions": description, "estimated_duration_minutes": minutes, "dispatch_mode": "Either",
	        "skill_required": "", "evidence_required": {"photos": True},
	        "checklist": [{"item_name": c, "required": True} for c in checklist], "enabled": enabled,
	        "shop_work": 1, "shop_skill": skill, "tools_parts": tools, "safety_note": safety}


SERVICE_TEMPLATE = _t("Service — machine due", "Servicio — máquina pendiente", "Maintenance", 120, "Skilled",
                      "Filters, oil, grease gun, the machine's service sheet",
                      "Machine off, key out, blocked; let it cool.",
                      "Do the service that came due on this machine and record the hour meter.",
                      ["Service done per the sheet", "Hour meter recorded"], enabled=1)
COUNT_TEMPLATE = _t("Count and reorder — shop and shed", "Contar y pedir — taller y bodega", "Other", 60, "Learner OK",
                    "The low-stock list, a clipboard or the phone", "",
                    "Count what is low and hand the list to whoever orders.", ["Low items counted", "List handed in"],
                    enabled=1)
TAGS_TEMPLATE = _t("Print and hang asset tags", "Imprimir y colgar etiquetas", "Other", 60, "Learner OK",
                   "Tag printer, zip ties", "", "Print the waiting tags and put each one on its machine or place.",
                   ["Tags printed", "Each tag hung and scanned"], enabled=1)
SEED_TEMPLATES = (
	SERVICE_TEMPLATE, COUNT_TEMPLATE, TAGS_TEMPLATE,
	_t("Sharpen and oil pruning tools", "Afilar y aceitar herramientas de poda", "Maintenance", 120, "Learner OK",
	   "Files, whetstone, oil, rags, spare springs and blades", "Cut-resistant gloves; clamp the tool.",
	   "Clean, sharpen, oil and adjust the hand pruners and loppers; replace worn springs and blades.",
	   ["Every pruner cleaned and sharpened", "Worn springs / blades replaced", "Counted back in"]),
	_t("Service chainsaws and pole saws", "Servicio de motosierras y podadoras de pértiga", "Maintenance", 120,
	   "Skilled", "Chain file and guide, bar oil, plugs, air filters", "Chaps and gloves; chain brake on.",
	   "Sharpen chains, clean filters, check plugs and bars on every saw.",
	   ["Chains sharpened", "Air filters cleaned", "Plugs and bars checked"]),
	_t("Rebuild sprinkler heads", "Reconstruir aspersores", "Irrigation", 180, "Learner OK",
	   "Rebuild kits, nozzles, washers, the bags from removal", "",
	   "Rebuild the sprinkler heads pulled from the blocks; bag them by block for the spring.",
	   ["Heads rebuilt or scrapped", "Bagged and labelled by block"]),
	_t("Repair picking bins", "Reparar bins de cosecha", "Repair", 180, "Learner OK",
	   "Boards, screws, impact driver, bin paint", "Eye protection; lift with two.",
	   "Fix broken boards and runners, then stack the bins for the season.",
	   ["Broken bins repaired or tagged out", "Stacked and counted"]),
	_t("Grease and inspect bin trailers and forks", "Engrasar e inspeccionar remolques y horquillas", "Maintenance",
	   90, "Learner OK", "Grease gun, tire gauge", "Blocked and on level ground.",
	   "Grease every fitting, check tires, lights and hitches on the bin trailers and forks.",
	   ["Every fitting greased", "Tires and hitches checked"]),
	_t("Inspect and repair orchard ladders", "Inspeccionar y reparar escaleras", "Repair", 120, "Learner OK",
	   "Rungs, bolts, the ladder log", "A ladder that fails inspection is tagged out, not used.",
	   "Inspect every ladder (rungs, rails, tripod leg, feet); repair or tag out.",
	   ["Every ladder inspected", "Failed ones tagged out", "Ladder log updated"]),
	_t("Sharpen mower blades and service the mower", "Afilar cuchillas y dar servicio a la podadora", "Maintenance",
	   120, "Skilled", "Blade sharpener, grease, belts", "Machine off, key out, blades blocked.",
	   "Pull and sharpen the flail / rotary blades, grease and check belts.",
	   ["Blades sharpened and balanced", "Greased; belts checked"]),
	_t("Clean and organise the shop", "Limpiar y ordenar el taller", "Other", 120, "Learner OK",
	   "Brooms, bins, labels", "", "Sweep, put tools back where they live, label what is not, take out the scrap.",
	   ["Swept", "Tools put away", "Scrap out"]),
	_t("Sprayer nozzle check before calibration", "Revisar boquillas antes de calibrar", "Maintenance", 90, "Skilled",
	   "Spare nozzles and screens, a measuring jug, the nozzle chart", "Clean water only; gloves.",
	   "Run the sprayer on clean water, measure each nozzle's output, replace worn tips and screens.",
	   ["Each nozzle measured", "Worn tips / screens replaced"]),
)
