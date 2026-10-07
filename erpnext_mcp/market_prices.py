# SPDX-License-Identifier: MIT
"""USDA AMS market prices, done right. v0.265.0 (Tim, 2026-10-06). Contract: docs/contracts/market_prices_v0_265.yaml.

WHAT WENT WRONG BEFORE, AND THE RULE THAT REPLACES EACH ONE:

* Shipping-point rows "never came through" in the Farm App: its nightly job sent the API key still encrypted, a
  terminal-market package filter dropped every shipping-point row, and failures were logged at debug level.
  Here every report is fetched with its Report Details section, nothing is filtered by a package list meant
  for another report type, and anything unexpected becomes a Market Data Issue a person can see.
* The old app carried a size's last price forward on days with no quote, drawing flat lines that were never a
  market. HERE NO QUOTE IS NO DATA POINT: a day without a row is a gap, and a row the report LISTS without a
  price is stored as Not Quoted — a marked gap. Nothing anywhere fills a gap.
* erpnext_mcp's own v0.87.0 fetch had no size or district in a row's identity, so sizes overwrote each other.
  A market point's identity is commodity key, market type, district, variety, size, pack, report and date.

COMMODITY-AGNOSTIC: everything about a crop — its AMS names, reports, field names, size vocabulary (cherry row
sizes, melon counts), packs and their weights, grower deductions and breakeven — is a Market Commodity config
(Farm Config Version), published by a person. Adding a crop is configuration.

GROWER FRAMING (Tim's standing rule): shipping point is the primary line; terminal is context. The difference
between them is the COST OF MARKET ACCESS, never margin. Grower return is $/lb after the configured deductions,
read against breakeven $/lb from the farm's own costing.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import statistics

import frappe

from . import compat

QUOTE = "USDA Price Quote"
ISSUE = "Market Data Issue"
KIND = "Market Commodity"
SHIPPING, TERMINAL, MOVEMENT = "Shipping Point", "Terminal", "Movement"
ROLE_TYPE = {"shipping_point": SHIPPING, "terminal": TERMINAL, "movement": MOVEMENT, "movement_weekly": MOVEMENT}
PRICED, NOT_QUOTED = "Priced", "Not Quoted"
WINDOW_DAYS = 31
DETAILS_PATH = "Report%20Details"

_COMMON_FIELDS = {
	"commodity": ["commodity", "commodity_name"],
	"variety": ["var", "variety", "variety_name"],
	"size": ["item_size", "size", "grade_size"],
	"pack": ["pkg", "package", "package_desc"],
	"grade": ["grade", "quality"],
	"district": ["district", "origin", "origin_district", "shipping_point"],
	"origin": ["origin", "state", "origin_state"],
	"low": ["low_price", "price_low", "low"],
	"high": ["high_price", "price_high", "high"],
	"mostly_low": ["mostly_low_price", "mostly_low"],
	"mostly_high": ["mostly_high_price", "mostly_high"],
	"comment": ["rep_cmt", "comment", "commodity_comment", "price_comment"],
	"report_date": ["report_date", "report_begin_date", "published_date"],
	"volume": ["10000_lb_units", "units", "volume", "total_pounds", "package_count"],
	"volume_unit": ["unit", "volume_unit"],
	"market": ["market_location_name", "market", "city"],
}

#: Published at install; every other Market Commodity is seeded as a DRAFT for a person to review.
PUBLISHED_BY_DEFAULT = ("sweet_cherries", "cantaloupe")

CHERRY_SIZES = ["8 1/2 row", "9 row", "9 1/2 row", "10 row", "10 1/2 row", "11 row", "11 1/2 row", "12 row"]

SEED = {
	"sweet_cherries": {
		"title": "Sweet cherries",
		"match": {"commodity": ["CHERRIES", "CHERRIES, SWEET"]},
		"season": {"start_mmdd": "05-15", "end_mmdd": "09-15", "week_anchor": "first_quote"},
		"reports": [
			{"slug": "2412", "role": "shipping_point", "label": "Yakima Valley & Wenatchee shipping point (YA_FV110)", "districts": []},
			{"slug": "2290", "role": "terminal", "label": "Chicago"},
			{"slug": "2314", "role": "terminal", "label": "New York"},
			{"slug": "2306", "role": "terminal", "label": "Los Angeles"},
			{"slug": "2277", "role": "terminal", "label": "Atlanta"},
			{"slug": "2285", "role": "terminal", "label": "Boston"},
			{"slug": "2318", "role": "terminal", "label": "Philadelphia"},
			{"slug": "3284", "role": "movement", "label": "National daily movement (WA_FV175)", "origins": ["WA", "OR"]},
			{"slug": "3258", "role": "movement_weekly", "label": "Cherries weekly movement (WA_FV415)"},
		],
		"field_map": {"shipping_point": _COMMON_FIELDS, "terminal": _COMMON_FIELDS, "movement": _COMMON_FIELDS,
		              "movement_weekly": _COMMON_FIELDS},
		"size": {"kind": "row", "order": "ascending", "note": "a smaller row number is BIGGER fruit",
		         "vocabulary": [{"label": s, "aliases": [s + " size", s.replace(" 1/2", ".5")]} for s in CHERRY_SIZES]},
		"packs": [{"label": "18 lb cartons", "match": ["18 lb", "18-lb"], "net_lb": 18.0, "unit": "carton"},
		          {"label": "16 lb cartons", "match": ["16 lb"], "net_lb": 16.0, "unit": "carton"},
		          {"label": "20 lb cartons", "match": ["20 lb"], "net_lb": 20.0, "unit": "carton"},
		          {"label": "15 lb cartons", "match": ["15 lb"], "net_lb": 15.0, "unit": "carton"}],
		"grower_deductions_per_lb": [{"name": "packing, selling and harvest (Constancy pool deal)", "per_lb": 0.60}],
		"breakeven": {"analysis": None, "per_lb": 1.22, "label": "Constancy 2027 draft pro forma"},
		"headline_size": "10 row",
	},
	"cantaloupe": {
		"title": "Cantaloupe",
		"match": {"commodity": ["CANTALOUPS", "CANTALOUPES", "CANTALOUPE"]},
		"season": {"start_mmdd": "04-15", "end_mmdd": "11-15", "week_anchor": "first_quote"},
		"reports": [
			{"slug": "2402", "role": "shipping_point", "label": "Phoenix shipping point (IX_FV110): Imperial / AZ, San Joaquin Valley", "districts": []},
			{"slug": "2290", "role": "terminal", "label": "Chicago"},
			{"slug": "2314", "role": "terminal", "label": "New York"},
			{"slug": "2306", "role": "terminal", "label": "Los Angeles"},
			{"slug": "3284", "role": "movement", "label": "National daily movement (WA_FV175)", "origins": ["CA", "AZ"]},
			{"slug": "3254", "role": "movement_weekly", "label": "Cantaloupes weekly movement (WA_FV411)"},
		],
		"field_map": {"shipping_point": _COMMON_FIELDS, "terminal": _COMMON_FIELDS, "movement": _COMMON_FIELDS,
		              "movement_weekly": _COMMON_FIELDS},
		"size": {"kind": "count", "order": "ascending", "note": "fewer melons to the carton is BIGGER fruit",
		         "vocabulary": [{"label": "9s", "aliases": ["9s (6 size)", "9 count", "9's"]},
		                        {"label": "12s", "aliases": ["12 count", "12's"]},
		                        {"label": "15s", "aliases": ["15 count", "15's"]},
		                        {"label": "18s", "aliases": ["18 count", "18's"]},
		                        {"label": "23s", "aliases": ["23 count", "23's"]}]},
		"packs": [{"label": "1/2 cartons", "match": ["1/2 carton", "1/2 ctn", "half carton"], "net_lb": 40.0, "unit": "carton"},
		          {"label": "oversized 1/2 cartons", "match": ["oversized 1/2"], "net_lb": 45.0, "unit": "carton"}],
		"grower_deductions_per_lb": [{"name": "packing, selling and harvest (set from the deal)", "per_lb": 0.0}],
		"breakeven": {"analysis": None, "per_lb": None, "label": "no cantaloupe breakeven yet"},
		"headline_size": "12s",
	},
}


# ── draft seeds: the major tree fruit and melons the AMS shipping-point reports carry ──
#: Current (2026) shipping-point FRUIT reports, terminal-market FRUIT reports and the national daily movement,
#: from the public MARS index (listPublishedReports). A draft lists them all; the commodity filter inside each
#: row keeps only its own, and observing the reports (observe_market_reports) narrows them before publishing.
FRUIT_SHIPPING_POINT = ("2386", "2390", "2392", "2394", "2395", "2399", "2401", "2402", "2404", "2412")
FRUIT_TERMINALS = ("2277", "2281", "2285", "2290", "2294", "2302", "2306", "2310", "2314", "2318")
NATIONAL_MOVEMENT = "3284"

DRAFT_SEEDS = {
	"apples": ("Apples", ["APPLES"], "07-15", "06-30"),
	"pears": ("Pears", ["PEARS"], "07-15", "05-31"),
	"peaches": ("Peaches", ["PEACHES"], "05-01", "10-15"),
	"nectarines": ("Nectarines", ["NECTARINES"], "05-01", "10-15"),
	"apricots": ("Apricots", ["APRICOTS"], "05-01", "08-15"),
	"plums": ("Plums", ["PLUMS", "PLUMS & PLUOTS", "PLUOTS"], "05-15", "10-31"),
	"grapes": ("Grapes", ["GRAPES", "GRAPES, TABLE"], "05-15", "12-31"),
	"blueberries": ("Blueberries", ["BLUEBERRIES"], "04-01", "10-15"),
	"watermelon": ("Watermelon", ["WATERMELONS", "WATERMELON"], "04-01", "10-31"),
	"honeydew": ("Honeydew", ["HONEYDEWS", "HONEYDEW"], "04-15", "11-15"),
}


def draft_seed_body(key: str) -> dict:
	"""A DRAFT Market Commodity for one of DRAFT_SEEDS — generic: no sizes or packs assumed (the generic reading
	and observation fill them), no deductions or breakeven (a person sets those)."""
	title, names, start, end = DRAFT_SEEDS[key]
	if start > end:  # a season that runs over the new year (apples, pears)
		start, end = "01-01", "12-31"
	return {
		"schema_version": 1, "key": key, "title": title, "match": {"commodity": names},
		"season": {"start_mmdd": start, "end_mmdd": end, "week_anchor": "first_quote"},
		"reports": [{"slug": s, "role": "shipping_point", "label": f"shipping point {s}", "districts": []} for s in FRUIT_SHIPPING_POINT]
		+ [{"slug": s, "role": "terminal", "label": f"terminal {s}"} for s in FRUIT_TERMINALS]
		+ [{"slug": NATIONAL_MOVEMENT, "role": "movement", "label": "National daily movement (WA_FV175)"}],
		"field_map": {role: _COMMON_FIELDS for role in ("shipping_point", "terminal", "movement", "movement_weekly")},
		"size": {"kind": "auto", "order": "generic", "vocabulary": []},
		"packs": [], "grower_deductions_per_lb": [], "breakeven": {"analysis": None, "per_lb": None, "label": "not set"},
		"headline_size": None,
		"note": "Seeded DRAFT (v0.265.0). Observe the reports, then draft_market_commodity rebuilds it from what they carry; a person publishes.",
	}


# ── config ──────────────────────────────────────────────────────────────────
def seed_body(key: str) -> dict:
	return {"schema_version": 1, "key": key, **json.loads(json.dumps(SEED[key]))}


def config(key: str) -> dict:
	"""The published Market Commodity config, or the seed when none is published yet."""
	try:
		from . import phone_config

		if phone_config.ready():
			rows = phone_config.rows(KIND, key, (phone_config.PUBLISHED,))
			if rows:
				row = max(rows, key=lambda r: int(r.get("version") or 0))
				body = phone_config.body_of(frappe.get_doc(phone_config.DOCTYPE, row["name"]))
				return {**body, "_version": row["name"]}
	except Exception:
		pass
	if key in SEED:
		return {**seed_body(key), "_version": "built-in"}
	raise ValueError(f"no Market Commodity {key!r}. Seeded: {', '.join(SEED)}; add one with draft_config.")


def commodities() -> list[str]:
	"""The commodities the charts, card and pulls serve: the PUBLISHED Market Commodity configs."""
	return commodities_published()


def validate(body: dict, key: str = "", for_publish: bool = False) -> dict:
	errors, warnings = [], []
	if not (body.get("match") or {}).get("commodity"):
		errors.append("match.commodity: the AMS commodity names are required.")
	roles = {r.get("role") for r in body.get("reports") or []}
	if "shipping_point" not in roles:
		errors.append("reports: a shipping_point report is required — it is the primary line.")
	for r in body.get("reports") or []:
		if r.get("role") not in ROLE_TYPE:
			errors.append(f"report {r.get('slug')}: role is one of {', '.join(ROLE_TYPE)}.")
		if not str(r.get("slug") or "").strip():
			errors.append("a report has no slug.")
	for p in body.get("packs") or []:
		if not p.get("net_lb"):
			warnings.append(f"pack {p.get('label')}: no net_lb — grower return cannot be computed for it.")
	if not (body.get("size") or {}).get("vocabulary"):
		warnings.append("size.vocabulary is empty — every size will be flagged unknown.")
	be = body.get("breakeven") or {}
	if not be.get("analysis") and be.get("per_lb") in (None, ""):
		warnings.append("no breakeven — the overlay will be missing.")
	return {"errors": errors, "warnings": warnings}


# ── parsing ─────────────────────────────────────────────────────────────────
def _pick(row: dict, aliases: list):
	lower = {str(k).lower(): v for k, v in row.items()}
	for name in aliases:
		value = lower.get(str(name).lower())
		if value not in (None, ""):
			return value
	return None


def _num(value):
	if value in (None, ""):
		return None
	try:
		return round(float(str(value).replace("$", "").replace(",", "").strip()), 4)
	except (TypeError, ValueError):
		return None


def _date(value) -> str | None:
	text = str(value or "").strip()
	if not text:
		return None
	for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %H:%M:%S"):
		try:
			return datetime.datetime.strptime(text[:19] if "T" in text else text[:10], fmt).date().isoformat()
		except ValueError:
			continue
	return None


#: Word sizes, biggest first — the generic order when a commodity has no vocabulary for them.
WORD_SIZES = ("colossal", "super colossal", "jumbo", "extra large", "xl", "large", "medium large", "medium", "small", "extra small", "petite")

_ROW = re.compile(r"^(\d+)(?:\s+1/2|\.5|½)?\s*row\b")
_COUNT = re.compile(r"^(\d+)\s*(?:s\b|'s\b|count\b|ct\b|size\b)")
_INCH = re.compile(r"(\d+(?:[ -]\d/\d|\.\d+)?)\s*(?:inch|in\.?|\")(?:\s|$|\b)")


def generic_size(raw) -> dict:
	"""Any AMS size, with no commodity in mind: {label, kind, sort}. `sort` puts bigger fruit first within a
	kind (row 8½ before 11, 9s before 15s, 3 inch before 2½, jumbo before small); anything unrecognised keeps its
	own words and sorts last — shown as-is, never dropped."""
	text = re.sub(r"\s+", " ", str(raw or "").strip())
	low = text.lower()
	if not low:
		return {"label": None, "kind": None, "sort": (9, 0.0, "")}
	m = _ROW.match(low)
	if m:
		half = bool(re.search(r"1/2|\.5|½", low[: m.end()]))
		number = int(m.group(1)) + (0.5 if half else 0)
		return {"label": f"{m.group(1)}{' 1/2' if half else ''} row", "kind": "row", "sort": (0, number, "")}
	m = _COUNT.match(low)
	if m:
		return {"label": f"{m.group(1)}s", "kind": "count", "sort": (1, float(m.group(1)), "")}
	m = _INCH.search(low)
	if m:
		raw_n = m.group(1).replace("-", " ")
		parts = raw_n.split()
		number = float(parts[0]) + (eval_fraction(parts[1]) if len(parts) > 1 else 0) if "." not in raw_n else float(raw_n)
		return {"label": text, "kind": "inch", "sort": (2, -number, "")}
	for rank, word in enumerate(WORD_SIZES):
		if low == word or low.startswith(word + " "):
			return {"label": text, "kind": "word", "sort": (3, float(rank), "")}
	return {"label": text, "kind": "other", "sort": (8, 0.0, low)}


def eval_fraction(text: str) -> float:
	try:
		a, b = text.split("/")
		return float(a) / float(b)
	except (ValueError, ZeroDivisionError):
		return 0.0


def size_order(label, rank=None) -> tuple:
	"""One ordering for every commodity: the config's vocabulary rank first, then the generic order."""
	if rank not in (None, "", 0):
		return (0, float(rank), "")
	return (1,) + generic_size(label)["sort"]


def normalise_size(raw, cfg: dict) -> tuple[str | None, int | None, bool]:
	"""(label, rank, recognised). The commodity's vocabulary first (its aliases, its order); otherwise the generic
	reading — a row size, a count, an inch size, a word size — and otherwise the report's own words, as-is."""
	text = re.sub(r"\s+", " ", str(raw or "").strip().lower())
	if not text:
		return None, None, True
	vocab = (cfg.get("size") or {}).get("vocabulary") or []
	for rank, entry in enumerate(vocab, start=1):
		names = [entry["label"]] + list(entry.get("aliases") or [])
		if any(text == n.lower() for n in names):
			return entry["label"], rank, True
	for rank, entry in enumerate(vocab, start=1):  # "10 row size" and "9s (6 size)" style suffixes
		if text.startswith(entry["label"].lower() + " ") or text.startswith(entry["label"].lower() + "("):
			return entry["label"], rank, True
	g = generic_size(raw)
	if g["kind"] != "other":
		for rank, entry in enumerate(vocab, start=1):
			if entry["label"].lower() == str(g["label"]).lower():
				return entry["label"], rank, True
		return g["label"], None, True
	return g["label"], None, False


_LB = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|\s)?(?:lb|lbs|pound|pounds)\b")
_KG = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|\s)?(?:kg|kilo|kilogram)s?\b")


def generic_pack(raw) -> dict:
	"""Net pounds from the pack's own words where they say it: '18 lb cartons' → 18, '10 kg' → 22.05, 'per lb' /
	'per pound' → 1. Cartons and bins that do not say their weight give None — grower $/lb then needs the
	commodity config's pack table."""
	low = str(raw or "").lower()
	if re.search(r"\bper\s+(?:lb|pound)\b", low):
		return {"net_lb": 1.0, "unit": "lb"}
	m = _LB.search(low)
	if m:
		return {"net_lb": float(m.group(1)), "unit": "carton" if "carton" in low else "box" if "box" in low else "package"}
	m = _KG.search(low)
	if m:
		return {"net_lb": round(float(m.group(1)) * 2.20462, 2), "unit": "carton" if "carton" in low else "package"}
	unit = next((u for u in ("bin", "carton", "crate", "flat", "tray", "box", "bag", "sack", "lug") if u in low), "package")
	return {"net_lb": None, "unit": unit}


def normalise_pack(raw, cfg: dict) -> tuple[str | None, float | None, bool]:
	"""(label, net_lb, recognised). The commodity's pack table first; otherwise the report's words, with net lb
	read from them when they state it."""
	text = str(raw or "").strip().lower()
	if not text:
		return None, None, True
	for pack in sorted(cfg.get("packs") or [], key=lambda p: -max(len(m) for m in p.get("match") or [""])):
		if any(m.lower() in text for m in pack.get("match") or []):
			return pack["label"], pack.get("net_lb"), True
	g = generic_pack(raw)
	return str(raw).strip(), g["net_lb"], g["net_lb"] is not None


def parse_row(row: dict, role: str, report: dict, cfg: dict) -> dict | None:
	"""One Report Details row → a market point dict, or None when it is another commodity / district."""
	fields = {**_COMMON_FIELDS, **((cfg.get("field_map") or {}).get(role) or {})}
	commodity = str(_pick(row, fields["commodity"]) or "").strip().upper()
	if commodity not in {c.upper() for c in (cfg.get("match") or {}).get("commodity") or []}:
		return None
	district = str(_pick(row, fields["district"]) or _pick(row, fields["market"]) or "").strip()
	wanted = [d.upper() for d in report.get("districts") or []]
	if wanted and district.upper() not in wanted:
		return None
	if role in ("movement", "movement_weekly"):
		origin = str(_pick(row, fields["origin"]) or district).strip()
		origins = [o.upper() for o in report.get("origins") or []]
		if origins and not any(origin.upper().startswith(o) for o in origins):
			return None
		district = origin
	day = _date(_pick(row, fields["report_date"]))
	size_label, size_rank, size_known = normalise_size(_pick(row, fields["size"]), cfg)
	pack_label, net_lb, pack_known = normalise_pack(_pick(row, fields["pack"]), cfg)
	low, high = _num(_pick(row, fields["low"])), _num(_pick(row, fields["high"]))
	mostly_low, mostly_high = _num(_pick(row, fields["mostly_low"])), _num(_pick(row, fields["mostly_high"]))
	market_type = ROLE_TYPE[role]
	priced = any(v is not None for v in (low, high, mostly_low, mostly_high))
	out = {
		"commodity": commodity, "variety": str(_pick(row, fields["variety"]) or "").strip(),
		"grade": str(_pick(row, fields["grade"]) or "").strip(), "district": district,
		"market": report.get("label") if market_type == TERMINAL else district,
		"size": size_label, "size_rank": size_rank, "package": pack_label, "pack_net_lb": net_lb,
		"report_date": day, "market_type": market_type, "report_slug": str(report["slug"]),
		"comment": str(_pick(row, fields["comment"]) or "").strip() or None,
		"_size_known": size_known, "_pack_known": pack_known,
	}
	if market_type == MOVEMENT:
		out.update({"volume": _num(_pick(row, fields["volume"])), "volume_unit": str(_pick(row, fields["volume_unit"]) or "10,000 lb units"),
		            "quote_status": PRICED})
	else:
		out.update({"low_price": low, "high_price": high, "mostly_low": mostly_low, "mostly_high": mostly_high,
		            "quote_status": PRICED if priced else NOT_QUOTED})
	return out


def identity(point: dict, key: str) -> str:
	parts = [key, point["market_type"], point.get("district") or "", point.get("variety") or "", point.get("size") or "",
	         point.get("package") or "", point["report_slug"], point.get("report_date") or ""]
	return hashlib.sha1("|".join(p.upper() for p in parts).encode()).hexdigest()


# ── storing ─────────────────────────────────────────────────────────────────
def season_of(day: str) -> int:
	return int(day[:4])


def store(point: dict, raw: dict, key: str) -> tuple[str, str]:
	"""Idempotent upsert of one market point. Returns (outcome, docname)."""
	row_hash = identity(point, key)
	values = {k: v for k, v in point.items() if not k.startswith("_")}
	values.update({"commodity_key": key, "row_hash": row_hash, "season": season_of(point["report_date"]),
	               "source": "USDA AMS Market News", "fetched_on": frappe.utils.now(),
	               "fetched_by": "erpnext_mcp market_prices", "payload": json.dumps(raw, sort_keys=True, default=str)})
	existing = frappe.db.get_value(QUOTE, {"row_hash": row_hash}, "name")
	if existing:
		doc = frappe.get_doc(QUOTE, existing)
		changed = any(str(doc.get(k) or "") != str(v or "") for k, v in values.items()
		              if k not in ("fetched_on", "payload"))
		if not changed:
			return "unchanged", existing
		for k, v in values.items():
			doc.set(k, v)
		doc.save(ignore_permissions=True)
		return "updated", doc.name
	doc = frappe.get_doc({"doctype": QUOTE, **values})
	doc.insert(ignore_permissions=True)
	return "stored", doc.name


def flag(kind: str, slug: str, key: str, day: str | None, detail: str, raw=None, count: int = 1) -> str:
	"""One Market Data Issue per (report, date, kind, detail) — re-flagging the same thing updates it."""
	issue_key = hashlib.sha1("|".join((slug, key, day or "", kind, detail)).encode()).hexdigest()
	now = frappe.utils.now()
	existing = frappe.db.get_value(ISSUE, {"issue_key": issue_key}, "name") if compat.doctype_exists(ISSUE) else None
	if existing:
		frappe.db.set_value(ISSUE, existing, {"last_seen": now, "count": count})
		return existing
	if not compat.doctype_exists(ISSUE):
		return ""
	doc = frappe.get_doc({"doctype": ISSUE, "report_slug": slug, "commodity_key": key, "report_date": day, "kind": kind,
	                      "detail": detail[:500], "status": "Open", "issue_key": issue_key, "first_seen": now,
	                      "last_seen": now, "count": count,
	                      "raw": json.dumps(raw, sort_keys=True, default=str)[:60000] if raw is not None else None})
	doc.insert(ignore_permissions=True)
	return doc.name


# ── fetching ────────────────────────────────────────────────────────────────
def _windows(start: datetime.date, end: datetime.date):
	day = start
	while day <= end:
		last = min(end, day + datetime.timedelta(days=WINDOW_DAYS - 1))
		yield day, last
		day = last + datetime.timedelta(days=1)


#: The API's row ceiling for a registered key. A window that comes back AT it is split, never truncated.
ROW_LIMIT = 100_000
#: Seconds between live requests in a run, and the 429 back-off: polite to a free public API with no
#: published request limit (its errors page lists 429).
REQUEST_PAUSE = 0.5
RETRIES_429 = 3


def http_get(slug: str, start: datetime.date, end: datetime.date, all_sections: bool = False):
	"""One MARS request. Returns (rows, error). The key comes from ERPNext MCP Settings; never logged.
	A 429 waits (Retry-After, else 5 s, doubling) and retries."""
	import time

	import requests

	from .services import usda_prices

	key = usda_prices.api_key()
	if not key:
		return None, "no USDA MARS API key in ERPNext MCP Settings (usda_mars_api_key)"
	query = f"report_begin_date={start:%m/%d/%Y}:{end:%m/%d/%Y}"
	base = usda_prices.base_url().rstrip("/")
	url = f"{base}/reports/{slug}?q={query}&allSections=true" if all_sections else f"{base}/reports/{slug}/{DETAILS_PATH}?q={query}"
	wait = 5.0
	for attempt in range(RETRIES_429 + 1):
		try:
			response = requests.get(url, auth=(key, ""), timeout=usda_prices.TIMEOUT_SECONDS)
		except Exception as exc:
			return None, f"{type(exc).__name__} reaching MARS"
		if response.status_code == 429 and attempt < RETRIES_429:
			try:
				wait = float(response.headers.get("Retry-After") or wait)
			except ValueError:
				pass
			time.sleep(min(wait, 120))
			wait *= 2
			continue
		break
	if response.status_code != 200:
		return None, f"HTTP {response.status_code} from MARS for report {slug}"
	try:
		payload = response.json()
	except ValueError:
		return None, f"report {slug} answered with something that is not JSON"
	time.sleep(REQUEST_PAUSE)
	return extract_rows(payload), None


def fetch_window(slug: str, start: datetime.date, end: datetime.date, fetch=http_get, issues: list | None = None):
	"""All rows of one report over one window — split in halves while a request comes back at the row ceiling,
	so nothing is cut off. Falls back to allSections on an HTTP error. Returns (rows, error)."""
	rows, error = fetch(slug, start, end)
	if rows is None and error and "HTTP" in error:
		rows, error2 = fetch(slug, start, end, all_sections=True)
		error = error2 if rows is None else None
	if rows is None:
		return None, error
	if len(rows) >= ROW_LIMIT:
		if start >= end:
			if issues is not None:
				issues.append(("row_limit", start.isoformat(), f"one day of report {slug} reached {ROW_LIMIT} rows"))
			return rows, None
		mid_day = start + (end - start) // 2
		a, ea = fetch_window(slug, start, mid_day, fetch, issues)
		b, eb = fetch_window(slug, mid_day + datetime.timedelta(days=1), end, fetch, issues)
		if a is None or b is None:
			return None, ea or eb
		return a + b, None
	return rows, None


def extract_rows(payload) -> list[dict]:
	"""The detail rows from any of MARS's answer shapes: a list, {results: [...]}, or sections."""
	if isinstance(payload, list):
		if payload and isinstance(payload[0], dict) and "results" in payload[0]:
			out = []
			for section in payload:
				name = str(section.get("reportSection") or section.get("report_section") or "").lower()
				if "header" not in name:
					out.extend(section.get("results") or [])
			return out
		return [r for r in payload if isinstance(r, dict)]
	if isinstance(payload, dict):
		if isinstance(payload.get("results"), list):
			return payload["results"]
		for value in payload.values():
			if isinstance(value, list) and value and isinstance(value[0], dict):
				return value
	return []


def ingest(key: str, start: str, end: str, roles=None, fetch=http_get) -> dict:
	"""One commodity over a date range — `ingest_all` for that one key."""
	return ingest_all(start, end, keys=[key], roles=roles, fetch=fetch)["commodities"][key]


def ingest_all(start: str, end: str, keys=None, roles=None, fetch=http_get) -> dict:
	"""Every configured commodity over a date range, FETCHING EACH REPORT ONCE per window and fanning its rows
	out to every commodity that lists it — enabling more crops adds commodities to a report's fan-out, not
	requests. Nothing is dropped silently: unknown sizes / packs, unparseable rows, HTTP errors, field-set
	changes, row-ceiling splits and in-season days with no shipping-point report all become Market Data Issues."""
	keys = list(keys) if keys else commodities_published()
	cfgs = {key: config(key) for key in keys}
	first, last = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
	per = {key: {"commodity": key, "from": start, "to": end, "stored": 0, "updated": 0, "unchanged": 0, "not_quoted": 0,
	             "issues": 0, "rows_seen": 0, "reports": []} for key in keys}
	fanout: dict = {}
	for key, cfg in cfgs.items():
		for rep in cfg.get("reports") or []:
			if roles and rep.get("role") not in roles:
				continue
			fanout.setdefault(str(rep["slug"]), []).append((key, rep, cfg))
	requests_made = 0
	shipping_days: dict = {key: set() for key in keys}
	for slug, takers in sorted(fanout.items()):
		kept = {key: 0 for key, _r, _c in takers}
		seen_rows = 0
		checked = False
		for a, b in _windows(first, last):
			split_issues: list = []
			rows, error = fetch_window(slug, a, b, fetch, split_issues)
			requests_made += 1
			for kind, day, detail in split_issues:
				for key, _r, _c in takers:
					flag(kind, slug, key, day, detail)
					per[key]["issues"] += 1
			if rows is None:
				for key, _r, _c in takers:
					flag("http_error", slug, key, a.isoformat(), error or "no answer")
					per[key]["issues"] += 1
				continue
			seen_rows += len(rows)
			if rows and not checked:
				checked = True
				signature = sorted({str(k).lower() for r in rows for k in r})
				for key, _r, _c in takers:
					_check_signature(slug, key, signature, per[key])
			for raw in rows:
				for key, rep, cfg in takers:
					try:
						point = parse_row(raw, rep["role"], rep, cfg)
					except Exception as exc:
						flag("parse_error", slug, key, None, f"{type(exc).__name__}: {exc}", raw)
						per[key]["issues"] += 1
						continue
					if point is None:
						continue
					_keep(point, raw, key, slug, per[key])
					kept[key] += 1
					if point["market_type"] == SHIPPING and point.get("report_date"):
						shipping_days[key].add(point["report_date"])
		for key, rep, _c in takers:
			per[key]["rows_seen"] += seen_rows
			per[key]["reports"].append({"slug": slug, "role": rep["role"], "rows": seen_rows, "kept": kept[key]})
	for key, cfg in cfgs.items():
		if roles and "shipping_point" not in roles:
			continue
		slugs = ",".join(str(r["slug"]) for r in cfg.get("reports") or [] if r.get("role") == "shipping_point")
		for day in in_season_weekdays(cfg, first, last):
			if day.isoformat() not in shipping_days[key] and not _has_shipping(key, day.isoformat()):
				flag("report_missing", slugs, key, day.isoformat(), "no shipping-point quote for an in-season weekday")
				per[key]["issues"] += 1
	return {"from": start, "to": end, "requests": requests_made, "reports": len(fanout), "commodities": per}


def _keep(point: dict, raw: dict, key: str, slug: str, totals: dict) -> None:
	if not point["report_date"]:
		flag("parse_error", slug, key, None, "row has no readable report date", raw)
		totals["issues"] += 1
		return
	if not point["_size_known"] and point["market_type"] != MOVEMENT:
		flag("unknown_size", slug, key, point["report_date"], f"size {point['size']!r} is not recognised — kept as-is", raw)
		totals["issues"] += 1
	if not point["_pack_known"] and point["market_type"] != MOVEMENT:
		flag("unknown_pack", slug, key, point["report_date"], f"pack {point['package']!r}: no net weight known", raw)
		totals["issues"] += 1
	try:
		outcome, _name = store(point, raw, key)
	except Exception as exc:
		flag("parse_error", slug, key, point["report_date"], f"not stored: {exc}", raw)
		totals["issues"] += 1
		return
	totals[outcome] += 1
	if point["quote_status"] == NOT_QUOTED:
		totals["not_quoted"] += 1


def commodities_published() -> list[str]:
	"""Keys with a PUBLISHED Market Commodity — drafts are not pulled until a person publishes them. The two
	built-in seeds count as published when no Farm Config Version exists yet (a site before its migrate)."""
	try:
		from . import phone_config

		if phone_config.ready():
			rows = frappe.db.get_all(phone_config.DOCTYPE, filters={"config_kind": KIND, "status": phone_config.PUBLISHED},
			                         fields=["config_key"]) or []
			if rows or frappe.db.get_all(phone_config.DOCTYPE, filters={"config_kind": KIND}, limit=1):
				return sorted({r["config_key"] for r in rows})
	except Exception:
		pass
	return sorted(PUBLISHED_BY_DEFAULT)


def _has_shipping(key: str, day: str) -> bool:
	return bool(frappe.db.get_value(QUOTE, {"commodity_key": key, "market_type": SHIPPING, "report_date": day}, "name"))


def _check_signature(slug: str, key: str, signature: list, report: dict) -> None:
	"""A report whose set of field names changed since the last pull is flagged — the old app broke silently here."""
	last = frappe.db.get_all(QUOTE, filters={"commodity_key": key, "report_slug": slug}, fields=["payload"],
	                         order_by="report_date desc", limit=1)
	if not last:
		return
	try:
		before = sorted(str(k).lower() for k in json.loads(last[0]["payload"] or "{}"))
	except ValueError:
		return
	if before and set(before) != set(signature):
		gone, new = sorted(set(before) - set(signature)), sorted(set(signature) - set(before))
		flag("field_set_changed", slug, key, None, f"fields gone: {gone}; new: {new}", {"before": before, "now": signature})
		report["issues"] += 1


def in_season_weekdays(cfg: dict, first: datetime.date, last: datetime.date):
	season = cfg.get("season") or {}
	start_mmdd, end_mmdd = season.get("start_mmdd", "01-01"), season.get("end_mmdd", "12-31")
	today = datetime.date.fromisoformat(str(frappe.utils.today())[:10])
	day = first
	while day <= min(last, today):
		mmdd = day.strftime("%m-%d")
		if start_mmdd <= mmdd <= end_mmdd and day.weekday() < 5 and _season_started(cfg, day):
			yield day
		day += datetime.timedelta(days=1)


def _season_started(cfg: dict, day: datetime.date) -> bool:
	"""In-season means between the first and last shipping-point quote of that year (once any exist), so the
	shoulder weeks before the district starts quoting are not flagged as missing reports."""
	key = cfg["key"]
	rows = frappe.db.get_all(QUOTE, filters={"commodity_key": key, "market_type": SHIPPING, "season": day.year},
	                         fields=["report_date"], order_by="report_date asc") or []
	if not rows:
		return False
	return str(rows[0]["report_date"])[:10] <= day.isoformat() <= str(rows[-1]["report_date"])[:10]


# ── reading: points, candles, trend, grower return ──────────────────────────
_POINT_FIELDS = ("name", "commodity_key", "market_type", "district", "market", "variety", "size", "size_rank", "package",
                 "pack_net_lb", "report_date", "low_price", "high_price", "mostly_low", "mostly_high", "quote_status",
                 "volume", "volume_unit", "report_slug", "comment", "season")


def points(key: str, market_type: str = SHIPPING, variety: str = "", size: str = "", district: str = "",
           start: str = "", end: str = "", season: int | None = None, limit_start: int = 0, limit: int = 0) -> list[dict]:
	filters = {"commodity_key": key, "market_type": market_type}
	for field, value in (("variety", variety), ("size", size), ("district", district)):
		if value:
			filters[field] = value
	if season:
		filters["season"] = int(season)
	if start and end:
		filters["report_date"] = ("between", [start, end])
	elif start:
		filters["report_date"] = (">=", start)
	elif end:
		filters["report_date"] = ("<=", end)
	out, offset, page = [], int(limit_start or 0), 2000
	while True:
		chunk = frappe.db.get_all(QUOTE, filters=filters, fields=list(compat.existing_fields(QUOTE, _POINT_FIELDS)),
		                          order_by="report_date asc, size_rank asc", limit_start=offset, limit_page_length=page) or []
		out.extend(dict(r) for r in chunk)
		if limit and len(out) >= limit:
			return out[:limit]
		if len(chunk) < page:
			return out
		offset += page


def mid(row: dict) -> float | None:
	ml, mh = row.get("mostly_low"), row.get("mostly_high")
	if ml not in (None, "") and mh not in (None, ""):
		return round((float(ml) + float(mh)) / 2, 4)
	lo, hi = row.get("low_price"), row.get("high_price")
	if lo not in (None, "") and hi not in (None, ""):
		return round((float(lo) + float(hi)) / 2, 4)
	single = next((float(v) for v in (lo, hi, ml, mh) if v not in (None, "")), None)
	return single


def period_start(day: str, interval: str) -> str:
	d = datetime.date.fromisoformat(str(day)[:10])
	return (d - datetime.timedelta(days=d.weekday())).isoformat() if interval == "week" else d.isoformat()


def candles(rows: list[dict], interval: str = "day") -> tuple[list[dict], list[str]]:
	"""Candles from Priced rows only (see the contract's derivation). Returns (candles, not_quoted_dates).
	A period with no Priced row is NOT a candle — a gap. Nothing is carried forward."""
	buckets: dict = {}
	not_quoted = []
	for row in sorted(rows, key=lambda r: str(r["report_date"])):
		day = str(row["report_date"])[:10]
		key = period_start(day, interval)
		bucket = buckets.setdefault(key, {"priced": [], "not_quoted": 0})
		if row.get("quote_status") == NOT_QUOTED or mid(row) is None:
			bucket["not_quoted"] += 1
			not_quoted.append(day)
			continue
		bucket["priced"].append(row)
	out = []
	for key in sorted(buckets):
		priced = buckets[key]["priced"]
		if not priced:
			continue
		highs = [float(r["high_price"]) for r in priced if r.get("high_price") not in (None, "")] or [mid(r) for r in priced]
		lows = [float(r["low_price"]) for r in priced if r.get("low_price") not in (None, "")] or [mid(r) for r in priced]
		out.append({"time": key, "open": mid(priced[0]), "high": round(max(highs), 4), "low": round(min(lows), 4),
		            "close": mid(priced[-1]), "quotes": len(priced), "not_quoted": buckets[key]["not_quoted"]})
	return out, sorted(set(not_quoted))


def _deductions(cfg: dict) -> float:
	return round(sum(float(d.get("per_lb") or 0) for d in cfg.get("grower_deductions_per_lb") or []), 4)


def grower_return_per_lb(fob_per_pack, net_lb, cfg: dict) -> float | None:
	if fob_per_pack in (None, "") or not net_lb:
		return None
	return round(float(fob_per_pack) / float(net_lb) - _deductions(cfg), 4)


def breakeven(cfg: dict) -> dict | None:
	be = cfg.get("breakeven") or {}
	if be.get("analysis") and frappe.db.exists("Breakeven Analysis", be["analysis"]):
		row = frappe.db.get_value("Breakeven Analysis", be["analysis"], ["breakeven_price", "unit_label", "analysis_name"], as_dict=True)
		if row and row.get("breakeven_price") and str(row.get("unit_label") or "") == "Pound":
			return {"per_lb": round(float(row["breakeven_price"]), 4), "label": row.get("analysis_name") or be["analysis"],
			        "source": "analysis"}
	if be.get("per_lb") not in (None, ""):
		return {"per_lb": round(float(be["per_lb"]), 4), "label": be.get("label") or "configured", "source": "config"}
	return None


def signal(price_change_pct, volume_change_pct) -> dict:
	"""Rate of change, read as a SIGNAL (not advice): is demand building or softening?"""
	if price_change_pct is None:
		return {"label": "not enough data", "why": "fewer than two weeks of quotes for this size"}
	p, v = price_change_pct, volume_change_pct
	if p >= 3 and (v is None or v >= 0):
		return {"label": "demand building", "why": f"price {p:+.1f}% week on week" + (f" with volume {v:+.1f}%" if v is not None else "")}
	if p <= -3 and v is not None and v >= 10:
		return {"label": "supply pressure", "why": f"price {p:+.1f}% while volume {v:+.1f}% — more fruit moving"}
	if p <= -3:
		return {"label": "softening", "why": f"price {p:+.1f}% week on week" + (f", volume {v:+.1f}%" if v is not None else "")}
	return {"label": "steady", "why": f"price {p:+.1f}% week on week"}


def _pct(new, old):
	if new in (None, 0) or old in (None, 0):
		return None
	return round((float(new) - float(old)) / float(old) * 100, 1)


def week_change(rows: list[dict]) -> float | None:
	weekly, _ = candles(rows, "week")
	return _pct(weekly[-1]["close"], weekly[-2]["close"]) if len(weekly) >= 2 else None


def volume_series(key: str, start: str = "", end: str = "", interval: str = "day") -> list[dict]:
	rows = points(key, MOVEMENT, start=start, end=end)
	agg: dict = {}
	unit = None
	for r in rows:
		if r.get("volume") in (None, ""):
			continue
		t = period_start(str(r["report_date"]), interval)
		agg[t] = agg.get(t, 0.0) + float(r["volume"])
		unit = unit or r.get("volume_unit")
	return [{"time": t, "value": round(v, 2), "unit": unit or "10,000 lb units"} for t, v in sorted(agg.items())]


def week_of_season(rows: list[dict]) -> list[dict]:
	"""Season-over-season, aligned by week of season (week 1 = the week of the season's first quote)."""
	by_season: dict = {}
	for r in rows:
		by_season.setdefault(int(r.get("season") or str(r["report_date"])[:4]), []).append(r)
	out = []
	for season in sorted(by_season):
		weekly, _ = candles(by_season[season], "week")
		if not weekly:
			continue
		first = datetime.date.fromisoformat(weekly[0]["time"])
		out.append({"season": season, "week_of_season": [
			{"week": (datetime.date.fromisoformat(c["time"]) - first).days // 7 + 1, "close": c["close"]} for c in weekly]})
	return out


def chart(key: str, variety: str = "", size: str = "", district: str = "", interval: str = "day", season=None,
          start: str = "", end: str = "", overlays=()) -> dict:
	cfg = config(key)
	if interval not in ("day", "week"):
		raise ValueError("interval is day or week.")
	rows = points(key, SHIPPING, variety, size, district, start, end, season)
	ranks = {}
	for r in rows:
		ranks.setdefault(r.get("size") or "", r.get("size_rank"))
	series = []
	for label in sorted(ranks, key=lambda lab: size_order(lab, ranks[lab])):
		mine = [r for r in rows if (r.get("size") or "") == label]
		c, nq = candles(mine, interval)
		series.append({"size": label or None, "candles": c, "not_quoted_dates": nq})
	unit = _unit(cfg, rows)
	out = {"commodity": key, "interval": interval, "unit": unit, "series": series,
	       "volume": volume_series(key, start or (min((str(r["report_date"]) for r in rows), default="")),
	                               end or (max((str(r["report_date"]) for r in rows), default="")), interval),
	       "seasons": week_of_season(points(key, SHIPPING, variety, size, district)) if not season else []}
	overlays = set(overlays or [])
	if "terminal" in overlays:
		trows = points(key, TERMINAL, variety, size, "", start, end, season)
		tc, _ = candles(trows, interval)
		out["terminal"] = [{"time": c["time"], "value": c["close"]} for c in tc]
	if "grower_return" in overlays:
		head = series[0]["candles"] if series else []
		net = next((r.get("pack_net_lb") for r in rows if r.get("pack_net_lb")), None)
		out["grower_return"] = [{"time": c["time"], "value": grower_return_per_lb(c["close"], net, cfg)} for c in head]
	if "breakeven" in overlays:
		out["breakeven"] = breakeven(cfg)
	return out


def _unit(cfg: dict, rows: list[dict]) -> str:
	pack = next((r.get("package") for r in rows if r.get("package")), None) or ((cfg.get("packs") or [{}])[0].get("label"))
	return f"$ per {pack}" if pack else "$ per package"


def card(key: str = "sweet_cherries", variety: str = "", size: str = "") -> dict:
	""""Should I be picking today?": the latest shipping-point quotes by size, the week's change, the signal,
	grower return $/lb against breakeven, terminal as context, volume. Every number is a quoted one."""
	cfg = config(key)
	rows = points(key, SHIPPING, variety, size)
	latest_day = max((str(r["report_date"])[:10] for r in rows), default=None)
	sizes = []
	ranks = {}
	for r in rows:
		if r.get("size"):
			ranks.setdefault(r["size"], r.get("size_rank"))
	for label in sorted(ranks, key=lambda lab: size_order(lab, ranks[lab])):
		mine = [r for r in rows if r.get("size") == label]
		today = [r for r in mine if str(r["report_date"])[:10] == latest_day]
		row = today[-1] if today else None
		m = mid(row) if row and row.get("quote_status") == PRICED else None
		sizes.append({"size": label,
		              "low": row.get("low_price") if row else None, "high": row.get("high_price") if row else None,
		              "mostly_low": row.get("mostly_low") if row else None, "mostly_high": row.get("mostly_high") if row else None,
		              "mid": m, "report_date": latest_day if row else None,
		              "quote_status": (row.get("quote_status") if row else "No Row"),
		              "change_wow_pct": week_change(mine),
		              "grower_return_per_lb": grower_return_per_lb(m, (row or {}).get("pack_net_lb"), cfg) if m else None})
	headline = next((s for s in sizes if s["size"] == cfg.get("headline_size") and s["mid"] is not None), None) or \
	    next((s for s in sizes if s["mid"] is not None), None)
	vol = volume_series(key, interval="week")
	volume = None
	if vol:
		volume = {"value": vol[-1]["value"], "unit": vol[-1]["unit"], "report_date": vol[-1]["time"],
		          "change_wow_pct": _pct(vol[-1]["value"], vol[-2]["value"]) if len(vol) >= 2 else None}
	be = breakeven(cfg)
	gr = headline["grower_return_per_lb"] if headline else None
	terminal = None
	trows = [r for r in points(key, TERMINAL, variety, size) if str(r["report_date"])[:10] == latest_day and mid(r) is not None]
	if trows and headline and headline["mid"] is not None:
		tmid = round(statistics.median(mid(r) for r in trows), 4)
		terminal = {"mid": tmid, "market": ", ".join(sorted({r.get("market") or "" for r in trows})),
		            "cost_of_market_access": round(tmid - headline["mid"], 4)}
	issues = frappe.db.count(ISSUE, {"commodity_key": key, "status": "Open"}) if compat.doctype_exists(ISSUE) else 0
	return {
		"commodity": key, "title": cfg.get("title"), "as_of": latest_day, "unit": _unit(cfg, rows), "sizes": sizes,
		"headline_size": headline["size"] if headline else None,
		"signal": signal(headline["change_wow_pct"] if headline else None, volume["change_wow_pct"] if volume else None),
		"grower_return_per_lb": gr, "breakeven": be,
		"above_breakeven_per_lb": round(gr - be["per_lb"], 4) if gr is not None and be else None,
		"terminal": terminal, "volume": volume, "data_issues_open": issues,
		"caveat": ("Shipping-point FOB from USDA AMS Market News; grower return is an estimate after the configured "
		           "deductions. The terminal–shipping difference is the cost of market access, not margin. A size with "
		           "no quote today shows no price — nothing is carried forward."),
	}


def percentiles(key: str, size: str = "", seasons: int = 5) -> list[dict]:
	"""Historical shipping-point ranges for the pro forma's price sensitivity: p10 / p50 / p90 of the daily mid,
	per season (and size when given). Not-quoted days count for nothing."""
	rows = [r for r in points(key, SHIPPING, size=size) if r.get("quote_status") == PRICED and mid(r) is not None]
	by: dict = {}
	for r in rows:
		by.setdefault(int(r.get("season") or 0), []).append(mid(r))
	out = []
	for season in sorted(by)[-seasons:]:
		values = sorted(by[season])
		q = statistics.quantiles(values, n=10) if len(values) >= 10 else None
		out.append({"season": season, "quotes": len(values), "p10": round(q[0], 2) if q else None,
		            "p50": round(statistics.median(values), 2), "p90": round(q[-1], 2) if q else None})
	return out


# ── schedule, backfill, probe ───────────────────────────────────────────────
def enabled() -> bool:
	try:
		from .settings import get_settings

		return bool(compat.checked(get_settings().get("market_prices_enabled")))
	except Exception:
		return False


def in_season(cfg: dict, day: datetime.date) -> bool:
	season = cfg.get("season") or {}
	return season.get("start_mmdd", "01-01") <= day.strftime("%m-%d") <= season.get("end_mmdd", "12-31")


def daily_pull() -> None:
	"""05:30. Each commodity in season: the last 7 days (AMS publishes corrections), every report. Off until
	market_prices_enabled; never raises."""
	try:
		if not enabled():
			return
		today = datetime.date.fromisoformat(str(frappe.utils.today())[:10])
		keys = [k for k in commodities_published() if in_season(config(k), today)]
		if keys:
			result = ingest_all((today - datetime.timedelta(days=7)).isoformat(), today.isoformat(), keys=keys)
			for key, r in result["commodities"].items():
				print(f"erpnext_mcp: market prices {key}: {r['stored']} stored, {r['updated']} updated, "
				      f"{r['not_quoted']} not quoted, {r['issues']} issue(s).")
			print(f"erpnext_mcp: market prices — {result['requests']} request(s) for {result['reports']} report(s), "
			      f"{len(keys)} commodity / commodities.")
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="market prices daily pull", message=frappe.get_traceback())


def backfill(key: str, seasons: list[int], roles=None) -> dict:
	"""Whole past seasons, every report, window by window — no caps."""
	cfg = config(key)
	season = cfg.get("season") or {}
	totals = {"commodity": key, "seasons": [], "stored": 0, "updated": 0, "unchanged": 0, "not_quoted": 0, "issues": 0}
	for year in sorted(set(int(s) for s in seasons)):
		start = f"{year}-{season.get('start_mmdd', '01-01')}"
		end = min(f"{year}-{season.get('end_mmdd', '12-31')}", str(frappe.utils.today())[:10])
		if start > end:
			continue
		result = ingest(key, start, end, roles=roles)
		totals["seasons"].append({"season": year, **{k: result[k] for k in ("stored", "updated", "unchanged", "not_quoted", "issues", "rows_seen")}})
		for k in ("stored", "updated", "unchanged", "not_quoted", "issues"):
			totals[k] += result[k]
	return totals


def probe(slug: str, start: str, end: str, save: bool = True) -> dict:
	"""READ-ONLY against USDA: one report's raw Report Details answer (and the allSections answer), saved as a
	private File for the contract fixtures. Shows the field names and what a no-price row looks like."""
	first, last = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
	out = {"slug": slug, "from": start, "to": end, "answers": []}
	for all_sections in (False, True):
		rows, error = http_get(slug, first, last, all_sections=all_sections)
		entry = {"all_sections": all_sections, "error": error, "rows": len(rows or []),
		         "fields": sorted({str(k) for r in (rows or []) for k in r}),
		         "no_price_rows": [r for r in (rows or []) if not any(_num(r.get(k)) is not None for k in
		                           ("low_price", "high_price", "mostly_low_price", "mostly_high_price"))][:5],
		         "sample": (rows or [])[:5]}
		if save and rows:
			name = f"mars_probe_{slug}_{start}_{end}_{'all' if all_sections else 'details'}.json"
			doc = frappe.get_doc({"doctype": "File", "file_name": name, "is_private": 1,
			                      "content": json.dumps(rows, indent=1, sort_keys=True, default=str)})
			doc.insert(ignore_permissions=True)
			entry["saved"] = doc.file_url
		out["answers"].append(entry)
	return out


# ── discovery: the catalog, what each report carries, drafts from the data ───
REPORT = "Market Report"
PUBLIC_INDEX = "https://marsapi.ams.usda.gov/services/v3.1/public/listPublishedReports/all?format=json"
_CODE = re.compile(r"\(([A-Z]{2}_[A-Z]{2}\d{3})\)")


def classify(title: str, code: str = "") -> str:
	"""A report's role from its title / code: shipping point, terminal, movement, weekly movement, or other."""
	t = str(title or "").lower()
	c = str(code or "").upper()
	if "shipping point" in t and "trends" not in t and "recap" not in t:
		return "shipping_point"
	if "terminal market" in t:
		return "terminal"
	if ("movement" in t or "shipments" in t) and "grain" not in t:
		return "movement_weekly" if re.search(r"FV4\d\d$", c) else "movement"
	return "other"


def http_index():
	"""The public MARS report index — no key needed. Returns (reports, error)."""
	import requests

	try:
		response = requests.get(PUBLIC_INDEX, timeout=60)
	except Exception as exc:
		return None, f"{type(exc).__name__} reaching the MARS index"
	if response.status_code != 200:
		return None, f"HTTP {response.status_code} from the MARS index"
	try:
		payload = response.json()
	except ValueError:
		return None, "the MARS index answered with something that is not JSON"
	return (payload.get("reports") if isinstance(payload, dict) else payload) or [], None


def refresh_catalog(fetch_index=http_index) -> dict:
	"""Every report in the public index → a Market Report row (create or refresh). Fruit and vegetable roles are
	classified; anything else is kept as `other` so the index is complete."""
	reports, error = fetch_index()
	if reports is None:
		raise ValueError(error or "no answer from the MARS index")
	now = frappe.utils.now()
	cutoff = (datetime.date.fromisoformat(str(frappe.utils.today())[:10]) - datetime.timedelta(days=366)).isoformat()
	made = refreshed = 0
	for r in reports:
		slug = str(r.get("id") or r.get("slug_id") or "").strip()
		if not slug:
			continue
		title = str(r.get("reportTitle") or r.get("report_title") or "").strip()
		code = (_CODE.search(title).group(1) if _CODE.search(title) else str(r.get("slug_name") or ""))
		published = str(r.get("publishedDate") or r.get("published_date") or "")[:19] or None
		values = {"report_title": title[:140], "slug_name": code, "role": classify(title, code),
		          "office": re.split(r"\s+(?:shipping point|terminal market|fruit|vegetables|truck)", title, flags=re.I)[0][:140],
		          "last_published": published, "stale": 1 if published and published[:10] < cutoff else 0,
		          "catalog_refreshed": now}
		if frappe.db.exists(REPORT, slug):
			frappe.db.set_value(REPORT, slug, values)
			refreshed += 1
		else:
			frappe.get_doc({"doctype": REPORT, "slug_id": slug, **values}).insert(ignore_permissions=True)
			made += 1
	return {"reports": len(reports), "created": made, "refreshed": refreshed}


def observe(slugs: list, start: str, end: str, fetch=http_get) -> dict:
	"""Read reports (keyed) over a window and record what each carries, per commodity: rows, priced, not quoted,
	districts, varieties, sizes (generic labels), packs (with net lb where stated), first / last date. Stores
	nothing else; this is the data a draft is built from."""
	first, last = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
	out = []
	for slug in slugs:
		role = frappe.db.get_value(REPORT, slug, "role") if frappe.db.exists(REPORT, slug) else None
		stats: dict = {}
		error = None
		for a, b in _windows(first, last):
			rows, error = fetch_window(str(slug), a, b, fetch)
			if rows is None:
				break
			for raw in rows:
				name = str(_pick(raw, _COMMON_FIELDS["commodity"]) or "").strip().upper()
				if not name:
					continue
				st = stats.setdefault(name, {"rows": 0, "priced": 0, "not_quoted": 0, "districts": {}, "varieties": {},
				                             "sizes": {}, "packs": {}, "first": None, "last": None})
				st["rows"] += 1
				prices = [_num(_pick(raw, _COMMON_FIELDS[k])) for k in ("low", "high", "mostly_low", "mostly_high")]
				st["priced" if any(p is not None for p in prices) else "not_quoted"] += 1
				for field, bucket in (("district", "districts"), ("variety", "varieties")):
					v = str(_pick(raw, _COMMON_FIELDS[field]) or "").strip()
					if v:
						st[bucket][v] = st[bucket].get(v, 0) + 1
				size = generic_size(_pick(raw, _COMMON_FIELDS["size"]))["label"]
				if size:
					st["sizes"][size] = st["sizes"].get(size, 0) + 1
				pack = str(_pick(raw, _COMMON_FIELDS["pack"]) or "").strip()
				if pack:
					st["packs"][pack] = st["packs"].get(pack, 0) + 1
				day = _date(_pick(raw, _COMMON_FIELDS["report_date"]))
				if day:
					st["first"] = min(filter(None, (st["first"], day)))
					st["last"] = max(filter(None, (st["last"], day)))
		if frappe.db.exists(REPORT, slug) and error is None:
			frappe.db.set_value(REPORT, slug, {"observations": json.dumps(stats, sort_keys=True), "observed_from": start,
			                                   "observed_to": end, "commodities": ", ".join(sorted(stats))[:5000]})
		out.append({"slug": str(slug), "role": role, "error": error, "commodities": sorted(stats),
		            "rows": sum(s["rows"] for s in stats.values())})
	return {"from": start, "to": end, "reports": out}


def available(search: str = "", role: str = "", include_stale: bool = False) -> list[dict]:
	"""The browsable index: every commodity seen in any observed report, with the reports (and roles) that carry
	it, its districts and how much data — and whether a Market Commodity config already covers it."""
	filters = {} if include_stale else {"stale": 0}
	if role:
		filters["role"] = role
	rows = frappe.db.get_all(REPORT, filters=filters, fields=["slug_id", "report_title", "role", "observations", "last_published"],
	                         limit_page_length=0) or []
	index: dict = {}
	for r in rows:
		try:
			obs = json.loads(r.get("observations") or "{}")
		except ValueError:
			obs = {}
		for name, st in obs.items():
			if search and search.lower() not in name.lower():
				continue
			entry = index.setdefault(name, {"commodity": name, "reports": [], "districts": {}, "rows": 0, "last": None})
			entry["reports"].append({"slug": r["slug_id"], "title": r["report_title"], "role": r["role"]})
			entry["rows"] += st.get("rows", 0)
			for d, n in (st.get("districts") or {}).items():
				entry["districts"][d] = entry["districts"].get(d, 0) + n
			entry["last"] = max(filter(None, (entry["last"], st.get("last")))) if st.get("last") or entry["last"] else None
	configured = {}
	for key in set(commodities_published()) | set(DRAFT_SEEDS) | set(SEED):
		try:
			body = config(key) if key in commodities_published() or key in SEED else draft_seed_body(key)
		except ValueError:
			continue
		for n in (body.get("match") or {}).get("commodity") or []:
			configured[n.upper()] = key
	out = []
	for name, e in sorted(index.items()):
		e["districts"] = sorted(e["districts"], key=lambda d: -e["districts"][d])
		e["config"] = configured.get(name)
		e["published"] = configured.get(name) in commodities_published()
		out.append(e)
	return out


def draft_from_observations(commodity: str, key: str = "", author: str = "") -> dict:
	"""A Market Commodity DRAFT built from what the observed reports carry: its reports and roles, districts,
	the observed sizes in generic order (bigger fruit first; unrecognised sizes kept as-is, last), packs with
	net lb where the pack says it, a season from the observed dates, the most-quoted size as headline. A person
	adds deductions and breakeven, reviews and publishes."""
	from . import phone_config

	name = str(commodity or "").strip().upper()
	entry = next((e for e in available(name) if e["commodity"] == name), None)
	if not entry:
		raise ValueError(f"no observed report carries {name!r} — refresh_market_catalog, then observe_market_reports first.")
	sizes: dict = {}
	packs: dict = {}
	firsts, lasts = [], []
	reps = []
	for rep in entry["reports"]:
		obs = json.loads(frappe.db.get_value(REPORT, rep["slug"], "observations") or "{}").get(name) or {}
		for k, v in (obs.get("sizes") or {}).items():
			sizes[k] = sizes.get(k, 0) + v
		for k, v in (obs.get("packs") or {}).items():
			packs[k] = packs.get(k, 0) + v
		firsts += [obs["first"]] if obs.get("first") else []
		lasts += [obs["last"]] if obs.get("last") else []
		if rep["role"] in ROLE_TYPE:
			reps.append({"slug": rep["slug"], "role": rep["role"], "label": rep["title"], "districts": []})
	if not any(r["role"] == "shipping_point" for r in reps):
		raise ValueError(f"{name} was seen only outside shipping-point reports; shipping point is the primary line, so no draft.")
	key = key or re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
	ordered = sorted(sizes, key=size_order)
	pack_rows = [{"label": p, "match": [p.lower()], "net_lb": generic_pack(p)["net_lb"], "unit": generic_pack(p)["unit"]}
	             for p in sorted(packs, key=lambda p: -packs[p])]
	start = min(f[5:] for f in firsts) if firsts else "01-01"
	end = max(l[5:] for l in lasts) if lasts else "12-31"
	if start > end:
		start, end = "01-01", "12-31"
	body = {"schema_version": 1, "key": key, "title": name.title(), "match": {"commodity": [name]},
	        "season": {"start_mmdd": start, "end_mmdd": end, "week_anchor": "first_quote"}, "reports": reps,
	        "field_map": {role: _COMMON_FIELDS for role in ("shipping_point", "terminal", "movement", "movement_weekly")},
	        "size": {"kind": "auto", "order": "generic",
	                 "vocabulary": [{"label": s, "aliases": []} for s in ordered]},
	        "packs": pack_rows, "grower_deductions_per_lb": [],
	        "breakeven": {"analysis": None, "per_lb": None, "label": "not set"},
	        "headline_size": max(sizes, key=lambda s: sizes[s]) if sizes else None,
	        "note": f"Drafted from observed reports ({', '.join(r['slug'] for r in reps)}) by {author or 'draft_market_commodity'}."}
	doc, report = phone_config.save_draft(KIND, key, body, f"Drafted from observations of {name}.", "AI-proposed")
	return {"key": key, "draft": phone_config.describe(doc), "validation": report, "body": body,
	        "next": "Review sizes / packs, add grower deductions and breakeven, then a person publishes it (Desk)."}


def seed_drafts() -> list[str]:
	"""Install: a DRAFT for each of DRAFT_SEEDS that has no Market Commodity rows yet. Never published here."""
	from . import phone_config

	made = []
	if not phone_config.ready():
		return made
	for key in DRAFT_SEEDS:
		if phone_config.rows(KIND, key):
			continue
		try:
			phone_config.save_draft(KIND, key, draft_seed_body(key),
			                        "Seeded DRAFT at install (v0.265.0) — observe the reports, redraft from the data, then publish.",
			                        "System")
			made.append(key)
		except Exception:
			frappe.log_error(title=f"market commodity draft {key}", message=frappe.get_traceback())
	return made
