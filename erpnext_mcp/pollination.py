# SPDX-License-Identifier: MIT
"""Pollination — where the hives go, how the crew moves them, and that every hive is counted back. v0.275.0
(docs/contracts/pollination_v0_275.yaml).

THE PLAN IS CONFIGURATION ("Pollination Plan", one per company and season): the blocks, hives per acre, hives per
pallet, pallets per drop, the loading area the beekeeper sets the pallets down at, the machine (pallets per trip,
speed, handling), clearance from hazard pins, the bloom-to-petal-fall stage, and the rental rate. `plan_hive_placement`
works out the drop points — an even grid inside each block's outline, clear of valves and hazard pins, as many drops
as the block's hives need — and saves the plan as a DRAFT a person publishes.

THE SEASON IS A CONTRACTOR JOB of kind Pollination (v0.271.0's job links): the beekeeper gets a link with the loading
area and taps Delivered (hives, pallets, frame strength) and later Picked up. The job keeps the season's working copy
of the drops (people move them by drag), the crew trips and the counts:
  1. expected   — the plan's hives;
  2. delivered  — what the beekeeper says came off the truck;
  3. placed     — the hives on the distribution trips the crew completed;
  4. gathered   — the hives on the gather-up trips the crew completed;
  5. picked up  — what the beekeeper says went back on the truck.
Each step that does not match the one before raises a flag to the managers until somebody resolves it with a reason.

THE CREW: "Distribute hives — trip n" tasks are raised with the job and released when the beekeeper taps Delivered;
"Gather hives — trip n" (the reverse order) are released at petal fall (the block's recorded stage) or by a person.
The beekeeper cannot tap Picked up until every gather-up trip is done.

BEES AND SPRAYS: the rule engine gets `pollination.hives_out` (hives are on this block) and `pollination.bee_toxic`
(the task's products harm a pollinator in the IPM graph — harmed_by edges, the farm's data). A Go / Hold preset holds
such a spray while hives are out; it is seeded OFF and Advisory.

RENTAL: `link_pollination_invoice` reads the beekeeper's Purchase Invoice and compares it to the hives delivered at
the plan's rate. It never changes the invoice.
"""

from __future__ import annotations

import itertools
import json
import math
import re

import frappe

from . import compat

KIND = "Pollination Plan"
JOB = "Contractor Job"
TASK = "Farm Task"
DISTRIBUTE, GATHER = "Distribute hives", "Gather hives"
DONE_STATES = ("Completed",)
FLAG = "pollination_enabled"
COUNTS = ("expected", "delivered", "placed", "gathered", "picked_up")
FT_PER_DEG_LAT = 364_000.0
DEFAULTS = {"hives_per_acre": 1.0, "hives_per_pallet": 4, "pallets_per_drop": 1, "clearance_ft": 30,
            "machine": {"pallets_per_trip": 6, "speed_mph": 4, "handling_min_per_drop": 4, "load_min_per_trip": 10},
            "petal_fall_bbch": 69, "rental": {"rate_per_hive": None}, "bee_harm_min_weight": 0.3}


class PollinationError(Exception):
	pass


def plan_key(company: str, season) -> str:
	return f"{re.sub(r'[^a-z0-9]+', '_', str(company).casefold()).strip('_')}_{season}"


def _settings(body: dict) -> dict:
	out = {**DEFAULTS, **{k: v for k, v in body.items() if v is not None}}
	out["machine"] = {**DEFAULTS["machine"], **(body.get("machine") or {})}
	out["rental"] = {**DEFAULTS["rental"], **(body.get("rental") or {})}
	return out


# ── geometry (pure Python: both test environments, no GIS library) ───────────
def _xy(lat, lon, lat0, lon0) -> tuple:
	return ((lon - lon0) * FT_PER_DEG_LAT * math.cos(math.radians(lat0)), (lat - lat0) * FT_PER_DEG_LAT)


def _latlon(x, y, lat0, lon0) -> tuple:
	return (lat0 + y / FT_PER_DEG_LAT, lon0 + x / (FT_PER_DEG_LAT * math.cos(math.radians(lat0))))


def distance_ft(a: tuple, b: tuple) -> float:
	x, y = _xy(b[0], b[1], a[0], a[1])
	return math.hypot(x, y)


def _bbox(geojson) -> tuple:
	from .field_card import _rings

	points = [pt for polygon in _rings(geojson) for ring in polygon for pt in ring]
	if not points:
		return None
	lons, lats = [p[0] for p in points], [p[1] for p in points]
	return min(lats), min(lons), max(lats), max(lons)


def drop_points(geojson, count: int, hazards=(), clearance_ft: float = 30.0) -> list:
	"""`count` points spread evenly inside the outline and `clearance_ft` from every hazard: [(lat, lon)]."""
	from .field_card import contains

	box = _bbox(geojson)
	if not box or count <= 0:
		return []
	lat0, lon0 = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
	x_min, y_min = _xy(box[0], box[1], lat0, lon0)
	x_max, y_max = _xy(box[2], box[3], lat0, lon0)
	area = abs((x_max - x_min) * (y_max - y_min)) or 1.0
	spacing = math.sqrt(area / count)
	best = []
	for _attempt in range(14):
		found = []
		y = y_min + spacing / 2
		while y < y_max:
			x = x_min + spacing / 2
			while x < x_max:
				lat, lon = _latlon(x, y, lat0, lon0)
				if contains(geojson, lat, lon) and all(distance_ft((lat, lon), (h[0], h[1])) >= clearance_ft
				                                       for h in hazards):
					found.append((round(lat, 6), round(lon, 6)))
				x += spacing
			y += spacing
		if len(found) >= count:
			step = len(found) / count
			return [found[int(i * step)] for i in range(count)]
		best = found if len(found) > len(best) else best
		spacing *= 0.85
	return best


# ── the plan ────────────────────────────────────────────────────────────────
def validate(body: dict, *, key: str = "", for_publish: bool = False) -> dict:
	errors, warnings = [], []
	if not body.get("company"):
		errors.append("company")
	if not re.fullmatch(r"\d{4}", str(body.get("season") or "")):
		errors.append("season: YYYY")
	settings = _settings(body)
	for name in ("hives_per_acre", "hives_per_pallet", "pallets_per_drop"):
		try:
			if float(settings[name]) <= 0:
				errors.append(f"{name}: more than 0")
		except (TypeError, ValueError):
			errors.append(f"{name}: a number")
	if int(_num(settings["machine"].get("pallets_per_trip"))) < 1:
		errors.append("machine.pallets_per_trip: at least 1")
	area = body.get("loading_area")
	if not (isinstance(area, (list, tuple)) and len(area) == 2):
		errors.append("loading_area: [lat, lon] where the beekeeper sets the pallets down")
	drops = body.get("drops") or []
	if not drops:
		errors.append("drops: none — run plan_hive_placement")
	for drop in drops:
		if not drop.get("block") or drop.get("lat") is None or drop.get("lon") is None:
			errors.append(f"drop {drop.get('id')}: block, lat and lon")
	return {"errors": errors, "warnings": warnings}


def _num(value) -> float:
	try:
		return float(value)
	except (TypeError, ValueError):
		return 0.0


def _blocks(company: str, names) -> list:
	from . import field_names

	out = []
	for name in names or []:
		try:
			for field in field_names.resolve_many(name, [company]):
				if field not in out:
					out.append(field)
		except ValueError as exc:
			raise PollinationError(str(exc)) from None
	return out


def place(company: str, season, fields, *, loading_area=None, actor: str = "", save: bool = True, **settings) -> dict:
	"""Drop points for the blocks; with `save`, a DRAFT Pollination Plan holding them."""
	from . import field_card, phone_config

	body = _settings({"company": company, "season": str(season), **settings})
	blocks = _blocks(company, fields)
	if not blocks:
		raise PollinationError("fields: at least one block (by name or alias).")
	drops, notes = [], []
	per_drop = int(body["hives_per_pallet"]) * int(body["pallets_per_drop"])
	for block in blocks:
		row = frappe.db.get_value("Field", block, ["field_name", "acreage", "area_computed_acres", "boundary_geojson"],
		                          as_dict=True) or {}
		if not row.get("boundary_geojson"):
			notes.append(f"{block}: no outline — draw it on the phone first")
			continue
		acres = _num(row.get("area_computed_acres")) or _num(row.get("acreage"))
		hives = max(1, round(acres * float(body["hives_per_acre"])))
		count = max(1, math.ceil(hives / per_drop))
		hazards = [(h["latitude"], h["longitude"]) for h in field_card.hazards(block, row["boundary_geojson"])]
		points = drop_points(row["boundary_geojson"], count, hazards, float(body["clearance_ft"]))
		if len(points) < count:
			notes.append(f"{block}: room for {len(points)} of {count} drops clear of hazards — some drops carry more")
		left = hives
		for index, (lat, lon) in enumerate(points or [], start=1):
			share = min(per_drop, left) if index < len(points) else left
			left -= share
			drops.append({"id": f"{_short(block)}-{index}", "block": block, "block_name": row.get("field_name") or block,
			              "lat": lat, "lon": lon, "hives": share, "pallets": math.ceil(share / int(body["hives_per_pallet"]))})
	if loading_area is None:
		loading_area = (drops[0]["lat"], drops[0]["lon"]) if drops else None
		if drops:
			notes.append("loading_area defaulted to the first drop — set the real spot")
	body.update({"fields": blocks, "drops": drops, "loading_area": list(loading_area) if loading_area else None,
	             "planned_by": actor or None})
	out = {"plan": plan_key(company, season), "drops": drops, "hives": sum(d["hives"] for d in drops),
	       "pallets": sum(d["pallets"] for d in drops), "trips": trips(body), "notes": notes}
	if save:
		report = validate(body)
		if report["errors"]:
			raise PollinationError("; ".join(report["errors"][:6]))
		doc, _ = phone_config.save_draft(KIND, plan_key(company, season), body,
		                                 "Drops placed by plan_hive_placement — check, move any, then publish.", "Operator")
		out.update({"version": doc.version, "status": "Draft"})
	return out


def _short(block: str) -> str:
	return re.sub(r"[^A-Za-z0-9]+", "", block)[:10] or "B"


def plan(company: str, season) -> dict:
	from . import phone_config

	doc = phone_config.doc_of(KIND, plan_key(company, season), status=phone_config.PUBLISHED)
	if doc is None:
		raise PollinationError(f"no published Pollination Plan for {company} {season} — place the hives, check, publish.")
	return {**phone_config.body_of(doc), "_version": doc.version}


def trips(body: dict, reverse: bool = False) -> list:
	"""Drops batched into machine loads from the loading area, nearest first; times in minutes."""
	settings = _settings(body)
	area = tuple(body.get("loading_area") or ())
	left = [d for d in body.get("drops") or []]
	if not left or len(area) != 2:
		return []
	machine = settings["machine"]
	per_trip = max(1, int(_num(machine["pallets_per_trip"])))
	feet_per_min = max(0.1, _num(machine["speed_mph"])) * 5280 / 60
	out = []
	while left:
		load, here, pallets = [], area, 0
		while left:
			nearest = min(left, key=lambda d: distance_ft(here, (d["lat"], d["lon"])))
			if pallets and pallets + nearest["pallets"] > per_trip:
				break
			load.append(nearest)
			pallets += nearest["pallets"]
			here = (nearest["lat"], nearest["lon"])
			left.remove(nearest)
		path = [area, *[(d["lat"], d["lon"]) for d in load], area]
		feet = sum(distance_ft(path[i], path[i + 1]) for i in range(len(path) - 1))
		minutes = _num(machine["load_min_per_trip"]) + feet / feet_per_min + len(load) * _num(machine["handling_min_per_drop"])
		out.append({"trip": len(out) + 1, "drops": [d["id"] for d in load], "hives": sum(d["hives"] for d in load),
		            "pallets": pallets, "feet": round(feet), "minutes": round(minutes)})
	if reverse:
		out = list(reversed(out))
		for index, trip in enumerate(out, start=1):
			trip["trip"] = index
	return out


# ── the season's job ─────────────────────────────────────────────────────────
def _op(doc) -> dict:
	raw = doc.get("operation")
	return json.loads(raw) if isinstance(raw, str) and raw else dict(raw or {})


def _save_op(doc, op: dict) -> None:
	doc.operation = json.dumps(op)
	doc.save(ignore_permissions=True)


def create_job(company: str, season, *, supplier: str = "", start_date: str = "", end_date: str = "",
               contact_name: str = "", contact_phone: str = "", actor: str = "") -> dict:
	"""The season's Pollination job from the published plan, with the crew's trips raised (held until delivery)."""
	from . import flags, job_links

	if not flags.value(FLAG, company=company, default=False):
		raise PollinationError(f"pollination jobs are off for {company} — turn on `{FLAG}` for it when it goes live.")
	body = plan(company, season)
	existing = [r["name"] for r in frappe.db.get_all(JOB, filters={"company": company, "kind": "Pollination",
	                                                                "status": ("not in", list(job_links.CLOSED))},
	                                                  fields=["name", "operation"], limit=50) or []
	            if json.loads(r.get("operation") or "{}").get("plan") == plan_key(company, season)]
	if existing:
		raise PollinationError(f"{existing[0]} is already this season's pollination job.")
	area = body.get("loading_area")
	name = job_links.create_job("pollination", company, fields=body.get("fields") or [], supplier=supplier,
	                            title=f"Pollination {season}", start_date=start_date, end_date=end_date,
	                            contact_name=contact_name, contact_phone=contact_phone,
	                            delivery_spot=tuple(area) if area else None, delivery_spot_label="Loading area — set the pallets here",
	                            actor=actor)
	doc = frappe.get_doc(JOB, name)
	op = {"plan": plan_key(company, season), "plan_version": body.get("_version"), "season": str(season),
	      "drops": body.get("drops") or [], "settings": {k: body.get(k) for k in DEFAULTS if k in body},
	      "loading_area": area, "expected": sum(int(d.get("hives") or 0) for d in body.get("drops") or []),
	      "distribute": [], "gather": [], "resolutions": {}, "invoice": None}
	op["distribute"] = _raise_trips(doc, op, DISTRIBUTE, trips({**body, "drops": op["drops"]}), start_date)
	op["gather"] = _raise_trips(doc, op, GATHER, trips({**body, "drops": op["drops"]}, reverse=True), end_date)
	_save_op(doc, op)
	return {"job": name, "expected": op["expected"], "distribute_tasks": [t["task"] for t in op["distribute"]],
	        "gather_tasks": [t["task"] for t in op["gather"]]}


def _raise_trips(doc, op: dict, template: str, plan_trips: list, due: str) -> list:
	from .tools import tasktemplates

	if not frappe.db.get_value("Farm Task Template", {"template_name": template}, "enabled"):
		return []
	drops = {d["id"]: d for d in op["drops"]}
	made = []
	for trip in plan_trips:
		lines = [f"• {drops[i]['block_name']} {i}: {drops[i]['hives']} hives ({drops[i]['pallets']} pallet) at "
		         f"{drops[i]['lat']:.6f}, {drops[i]['lon']:.6f}" for i in trip["drops"]]
		result = tasktemplates.create_task_from_template(
			{"template": template, "company": doc.company, "draft": True,
			 "task_name": f"{template} — trip {trip['trip']} of {len(plan_trips)} ({trip['hives']} hives)",
			 "notes": f"{doc.name}. From the loading area, about {trip['minutes']} min:\n" + "\n".join(lines),
			 **({"due_date": str(due)} if due else {})},
			origin="compliance_rule", fields={"source_workorder": f"pollination:{doc.name}:{template}:{trip['trip']}"[:140]})
		made.append({"task": (result.data or {}).get("name"), **trip})
	return made


def _job(job: str):
	doc = frappe.get_doc(JOB, job)
	if doc.kind != "Pollination":
		raise PollinationError(f"{job} is not a pollination job.")
	return doc


def update_drops(job: str, moves: list, actor: str = "") -> dict:
	"""Move drops (the drag on the map). Inside one of the job's blocks; a drop near a hazard is warned, not refused."""
	from . import field_card

	doc = _job(job)
	op = _op(doc)
	started = [t for t in op["distribute"] if frappe.db.get_value(TASK, t["task"], "state") not in ("Draft", "Available")]
	if started:
		raise PollinationError("the crew has started distributing — drops cannot move now.")
	by_id = {d["id"]: d for d in op["drops"]}
	warnings = []
	clearance = float(_settings(op.get("settings") or {})["clearance_ft"])
	for move in moves or []:
		drop = by_id.get(move.get("id"))
		if drop is None:
			raise PollinationError(f"no drop {move.get('id')!r}.")
		lat, lon = float(move["lat"]), float(move["lon"])
		block = next((b for b in [r.get("field") for r in doc.get("fields") or []]
		              if field_card.contains(frappe.db.get_value("Field", b, "boundary_geojson"), lat, lon)), None)
		if block is None:
			raise PollinationError(f"{drop['id']}: that point is outside the job's blocks.")
		near = [h for h in field_card.hazards(block) if distance_ft((lat, lon), (h["latitude"], h["longitude"])) < clearance]
		if near:
			warnings.append(f"{drop['id']} is within {clearance:g} ft of {near[0]['name']}")
		drop.update({"lat": round(lat, 6), "lon": round(lon, 6), "block": block, "moved_by": actor or None})
		if move.get("hives") is not None:
			drop["hives"] = int(move["hives"])
	op["expected"] = sum(int(d.get("hives") or 0) for d in op["drops"])
	body = {"drops": op["drops"], "loading_area": op.get("loading_area"), **(op.get("settings") or {})}
	for name, template, reverse in (("distribute", DISTRIBUTE, False), ("gather", GATHER, True)):
		for old in op[name]:
			if old.get("task") and frappe.db.exists(TASK, old["task"]):
				frappe.db.set_value(TASK, old["task"], "state", "Cancelled")
		op[name] = _raise_trips(doc, op, template, trips(body, reverse=reverse), "")
	_save_op(doc, op)
	return {"job": job, "drops": op["drops"], "expected": op["expected"], "warnings": warnings,
	        "trips": [{k: t[k] for k in ("trip", "hives", "minutes", "task")} for t in op["distribute"]]}


def _release(tasks: list) -> list:
	freed = []
	for trip in tasks:
		if trip.get("task") and frappe.db.get_value(TASK, trip["task"], "state") == "Draft":
			frappe.db.set_value(TASK, trip["task"], "state", "Available")
			freed.append(trip["task"])
	return freed


def on_event(job: str, event: str) -> None:
	"""The beekeeper's Delivered releases the distribution trips; the counts are re-checked."""
	doc = _job(job)
	if event == "Delivered":
		_release(_op(doc)["distribute"])
	reconcile(job)


def release_gather(job: str, actor: str = "") -> dict:
	doc = _job(job)
	return {"job": job, "released": _release(_op(doc)["gather"]), "by": actor or None}


def can_pick_up(job: str) -> tuple:
	op = _op(_job(job))
	open_trips = [t["task"] for t in op["gather"] if frappe.db.get_value(TASK, t["task"], "state") not in DONE_STATES]
	return (not open_trips, open_trips)


# ── counts ───────────────────────────────────────────────────────────────────
def _event_count(doc, event: str):
	for row in reversed(doc.get("events") or []):
		if row.get("event") == event:
			raw = row.get("counts")
			counts = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
			return int(_num(counts.get("hives"))) if counts.get("hives") not in (None, "") else None
	return None


def counts(job: str) -> dict:
	doc = _job(job)
	op = _op(doc)

	def done(trips_):
		return sum(int(t.get("hives") or 0) for t in trips_ if frappe.db.get_value(TASK, t["task"], "state") in DONE_STATES)

	return {"expected": int(op.get("expected") or 0), "delivered": _event_count(doc, "Delivered"),
	        "placed": done(op["distribute"]) if op["distribute"] else None,
	        "gathered": done(op["gather"]) if op["gather"] else None, "picked_up": _event_count(doc, "Picked Up")}


def reconcile(job: str) -> dict:
	"""Each step against the one before; a mismatch raises a flag until somebody resolves it."""
	doc = _job(job)
	op = _op(doc)
	got = counts(job)
	flags = []
	for earlier, later in itertools.pairwise(COUNTS):
		a, b = got[earlier], got[later]
		if a is None or b is None or a == b:
			continue
		if later in ("placed", "gathered") and b < a and not _all_done(op, later):
			continue  # the crew is still going
		pair = f"{earlier}_vs_{later}"
		resolved = (op.get("resolutions") or {}).get(pair)
		flags.append({"flag": pair, "earlier": earlier, "later": later, earlier: a, later: b, "difference": b - a,
		              "resolved": resolved or None, "alert": None if resolved else _flag_alert(doc, pair, earlier, a, later, b)})
	return {"job": job, "counts": got, "flags": flags}


def _all_done(op: dict, step: str) -> bool:
	trips_ = op["distribute"] if step == "placed" else op["gather"]
	return all(frappe.db.get_value(TASK, t["task"], "state") in DONE_STATES for t in trips_)


def _flag_alert(doc, pair: str, earlier: str, a: int, later: str, b: int) -> str | None:
	if not compat.doctype_exists("Compliance Alert"):
		return None
	from .alerts import base as alerts

	key = alerts.alert_key(f"pollination_{pair}", JOB, doc.name)
	if frappe.db.exists("Compliance Alert", key):
		return key
	alert = frappe.new_doc("Compliance Alert")
	alert.alert_key = key
	alert.alert_type = f"pollination_{pair}"
	alert.severity = "Warning"
	alert.category = "Other"
	alert.company = doc.company
	alert.source_doctype = JOB
	alert.source_docname = doc.name
	alert.alert_message = (f"{doc.job_title}: {later.replace('_', ' ')} {b} hives against {earlier.replace('_', ' ')} "
	                       f"{a} ({b - a:+d}). Count again, or resolve it with the reason.")
	alert.first_seen = frappe.utils.today()
	alert.last_refreshed = frappe.utils.now()
	alert.insert(ignore_permissions=True)
	return alert.name


def resolve_flag(job: str, flag: str, note: str, actor: str = "") -> dict:
	if not str(note or "").strip():
		raise PollinationError("say why — e.g. 'two dead-outs left with the beekeeper'.")
	doc = _job(job)
	op = _op(doc)
	op.setdefault("resolutions", {})[flag] = {"note": str(note)[:300], "by": actor or None, "on": frappe.utils.today()}
	_save_op(doc, op)
	from .alerts import base as alerts

	key = alerts.alert_key(f"pollination_{flag}", JOB, job)
	if compat.doctype_exists("Compliance Alert") and frappe.db.exists("Compliance Alert", key):
		for field, value in (("dismissed", 1), ("dismissed_by", actor or None), ("dismissed_on", frappe.utils.now()),
		                     ("dismissed_reason", str(note)[:140])):
			frappe.db.set_value("Compliance Alert", key, field, value)
	return reconcile(job)


# ── rental ─────────────────────────────────────────────────────────────────────
def link_invoice(job: str, invoice: str, actor: str = "") -> dict:
	"""The beekeeper's Purchase Invoice against the hives delivered at the plan's rate — read, never changed."""
	doc = _job(job)
	if not frappe.db.exists("Purchase Invoice", invoice):
		raise PollinationError(f"no Purchase Invoice {invoice!r}.")
	inv = frappe.get_doc("Purchase Invoice", invoice)
	op = _op(doc)
	rate = _num((_settings(op.get("settings") or {})["rental"] or {}).get("rate_per_hive"))
	delivered = counts(job)["delivered"]
	billed_hives = sum(_num(r.get("qty")) for r in inv.get("items") or [])
	billed = _num(inv.get("grand_total") or inv.get("total"))
	expected_amount = round(delivered * rate, 2) if delivered is not None and rate else None
	issues = []
	if inv.get("supplier") and doc.supplier and inv.get("supplier") != doc.supplier:
		issues.append(f"the invoice is from {inv.get('supplier')}, the job's beekeeper is {doc.supplier}")
	if delivered is not None and billed_hives and int(billed_hives) != delivered:
		issues.append(f"billed for {billed_hives:g} hives, {delivered} delivered")
	if expected_amount is not None and abs(billed - expected_amount) > 0.01:
		issues.append(f"billed {billed:,.2f}, {delivered} hives at {rate:,.2f} is {expected_amount:,.2f}")
	op["invoice"] = {"invoice": invoice, "billed": billed, "billed_hives": billed_hives,
	                 "expected_amount": expected_amount, "issues": issues, "by": actor or None}
	_save_op(doc, op)
	return {"job": job, **op["invoice"], "matches": not issues}


# ── the map and the status ───────────────────────────────────────────────────
def hive_map(job: str) -> dict:
	from . import field_card

	doc = _job(job)
	op = _op(doc)
	blocks = []
	for row in doc.get("fields") or []:
		raw = frappe.db.get_value("Field", row.get("field"), "boundary_geojson")
		blocks.append({"block": row.get("field"), "label": row.get("field_name") or row.get("field"),
		               "geojson": json.loads(raw) if isinstance(raw, str) and raw else raw,
		               "hazards": [{"name": h["name"], "lat": h["latitude"], "lon": h["longitude"],
		                            "kind": "valve" if h.get("asset_type") == "Irrigation Valve" else "hazard"}
		                           for h in field_card.hazards(row.get("field"))]})
	return {"job": job, "loading_area": op.get("loading_area"), "drops": op["drops"], "blocks": blocks,
	        "distribute": op["distribute"], "gather": op["gather"]}


def status(job: str = "", field: str = "", company: str = "") -> dict:
	if job:
		doc = _job(job)
		out = reconcile(job)
		ok, open_trips = can_pick_up(job)
		return {**out, "status": doc.status, "pickup_allowed": ok, "gather_open": open_trips,
		        "invoice": _op(doc).get("invoice")}
	filters = {"kind": "Pollination"}
	if company:
		filters["company"] = company
	history = []
	for row in frappe.db.get_all(JOB, filters=filters, fields=["name", "status", "operation"], limit=500) or []:
		doc = frappe.get_doc(JOB, row["name"])
		if field and field not in [r.get("field") for r in doc.get("fields") or []]:
			continue
		op = _op(doc)
		history.append({"job": row["name"], "season": op.get("season"), "status": row.get("status"),
		                "counts": counts(row["name"]),
		                "drops_here": [d for d in op.get("drops") or [] if not field or d.get("block") == field]})
	return {"field": field or None, "seasons": sorted(history, key=lambda h: h["season"] or "", reverse=True)}


# ── petal fall (daily) ───────────────────────────────────────────────────────
def daily() -> dict:
	"""Hives out and a block at petal fall: release the gather-up and tell the managers, once."""
	from . import bbch, growth_stage

	out = {}
	for row in frappe.db.get_all(JOB, filters={"kind": "Pollination", "status": ("in", ["In Progress", "Shared",
	                                                                                      "Ready"])},
	                             fields=["name"], limit=200) or []:
		doc = frappe.get_doc(JOB, row["name"])
		if _event_count(doc, "Delivered") is None and not any(e.get("event") == "Delivered" for e in doc.get("events") or []):
			continue
		op = _op(doc)
		threshold = int(_num(_settings(op.get("settings") or {})["petal_fall_bbch"]))
		at = []
		for f in doc.get("fields") or []:
			last = growth_stage.latest(f.get("field"))
			code = bbch.parse((last or {}).get("growth_stage_code"))
			if code is not None and code >= threshold:
				at.append(f.get("field_name") or f.get("field"))
		if at:
			out[doc.name] = {"released": _release(op["gather"]), "alert": _petal_alert(doc, at, threshold)}
	return out


def _petal_alert(doc, blocks: list, threshold: int) -> str | None:
	if not compat.doctype_exists("Compliance Alert"):
		return None
	from .alerts import base as alerts

	key = alerts.alert_key("pollination_petal_fall", JOB, doc.name)
	if frappe.db.exists("Compliance Alert", key):
		return key
	alert = frappe.new_doc("Compliance Alert")
	alert.alert_key = key
	alert.alert_type = "pollination_petal_fall"
	alert.severity = "Info"
	alert.category = "Other"
	alert.company = doc.company
	alert.source_doctype = JOB
	alert.source_docname = doc.name
	alert.alert_message = (f"Petal fall (BBCH {threshold}+) on {', '.join(blocks)}: gather the hives — the gather-up "
	                       "trips are on the board; the beekeeper's pickup opens when they are done.")
	alert.first_seen = frappe.utils.today()
	alert.last_refreshed = frappe.utils.now()
	alert.insert(ignore_permissions=True)
	return alert.name


# ── bees and sprays (a value provider for the rule engine) ─────────────────────
def hives_out_on(block: str) -> bool:
	if not block:
		return False
	for row in frappe.db.get_all(JOB, filters={"kind": "Pollination", "status": ("not in", ["Closed", "Cancelled"])},
	                             fields=["name"], limit=200) or []:
		doc = frappe.get_doc(JOB, row["name"])
		events = [e.get("event") for e in doc.get("events") or []]
		if "Delivered" not in events or "Picked Up" in events:
			continue
		if block in [f.get("field") for f in doc.get("fields") or []]:
			return True
	return False


def bee_harm(items: list, min_weight: float = DEFAULTS["bee_harm_min_weight"]) -> list:
	"""The task's products a pollinator in the IPM graph is harmed_by (by Item or EPA number): [product names]."""
	if not items or not compat.doctype_exists("IPM Organism") or not compat.doctype_exists("IPM Relationship"):
		return []
	pollinators = {r["name"] for r in frappe.db.get_all("IPM Organism", filters={"kind": "Pollinator"}, fields=["name"],
	                                                    limit=500) or []}
	if not pollinators:
		return []
	epas = {str(frappe.db.get_value("Item", i, "epa_registration_number") or "") for i in items
	        if compat.has_field("Item", "epa_registration_number")} - {""}
	products = {r["name"]: r for r in frappe.db.get_all("IPM Organism", filters={"kind": "Product"},
	                                                     fields=["name", "organism_name", "item", "epa_reg_number"],
	                                                     limit=5000) or []
	            if r.get("item") in items or (r.get("epa_reg_number") and r.get("epa_reg_number") in epas)}
	if not products:
		return []
	hit = []
	for edge in frappe.db.get_all("IPM Relationship", filters={"relation": "harmed_by"},
	                              fields=["subject", "object", "weight", "enabled"], limit=20000) or []:
		if edge.get("subject") in pollinators and edge.get("object") in products and int(edge.get("enabled") or 1) \
				and _num(edge.get("weight")) >= min_weight:
			hit.append(products[edge["object"]].get("organism_name") or edge["object"])
	return sorted(set(hit))


def provider_values(subject: dict, ctx: dict) -> dict:
	from . import label_compliance

	block = subject.get("location") if subject.get("location_doctype") in ("Field", None, "") else ""
	items = label_compliance.products_of(subject)
	if subject.get("tank_mix") and compat.doctype_exists("Spray Tank Mix"):
		mix = frappe.get_doc("Spray Tank Mix", subject["tank_mix"]) if frappe.db.exists("Spray Tank Mix", subject["tank_mix"]) else None
		items += [p.get("item") for p in (mix.get("products") or [])] if mix else []
	harm = bee_harm([i for i in items if i])
	return {"hives_out": hives_out_on(block), "bee_toxic": bool(harm), "bee_toxic_products": ", ".join(harm)}


def register(ccf) -> None:
	p = ccf._p
	ccf.register(ccf.Provider(
		"pollination",
		{"hives_out": p("bool", "Rented hives are out on this block (delivered, not yet picked up).", example=True),
		 "bee_toxic": p("bool", "A product on this task harms a pollinator in the IPM graph (harmed_by).", example=False),
		 "bee_toxic_products": p("string", "Those products, named.", example="Warrior II")},
		lambda subject, ctx: provider_values(subject, ctx), past=False,
		description="Pollination (v0.275.0): hives on the block and bee-toxic products on the task.",
	))


# ── seeds ────────────────────────────────────────────────────────────────────
TASK_TEMPLATES = (
	{"template_name": DISTRIBUTE, "title_es": "Distribuir colmenas", "task_type": "Other",
	 "description": "Take the pallets from the loading area to the drop points on this trip and set them down.",
	 "instructions": "Load the pallets on this trip. Drive the route in order; set each pallet at its drop point, "
	                 "entrance facing the morning sun, level and off the drive row. Count the hives you set down.",
	 "instructions_es": "Carga las tarimas de este viaje. Sigue la ruta en orden; pon cada tarima en su punto, la entrada "
	                    "hacia el sol de la mañana, nivelada y fuera del paso. Cuenta las colmenas que dejas.",
	 "estimated_duration_minutes": 45, "dispatch_mode": "Either", "evidence_required": {"photos": True},
	 "checklist": [{"item_name": "Every pallet on this trip set at its drop", "required": True},
	               {"item_name": "Hives counted", "required": True}], "enabled": 1},
	{"template_name": GATHER, "title_es": "Recoger colmenas", "task_type": "Other",
	 "description": "Bring this trip's pallets back to the loading area for the beekeeper.",
	 "instructions": "After petal fall. Pick up the pallets on this trip, close entrances if the beekeeper asks, and "
	                 "set them at the loading area. Count the hives you bring back.",
	 "instructions_es": "Después de la caída de pétalos. Recoge las tarimas de este viaje, cierra las entradas si el "
	                    "apicultor lo pide y déjalas en el área de carga. Cuenta las colmenas que traes.",
	 "estimated_duration_minutes": 45, "dispatch_mode": "Either", "evidence_required": {"photos": True},
	 "checklist": [{"item_name": "Every pallet on this trip back at the loading area", "required": True},
	               {"item_name": "Hives counted", "required": True}], "enabled": 1},
)

