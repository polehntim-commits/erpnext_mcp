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
	keys = set(SEED)
	try:
		from . import phone_config

		if phone_config.ready():
			keys |= {r["config_key"] for r in frappe.db.get_all(phone_config.DOCTYPE, filters={"config_kind": KIND},
			                                                     fields=["config_key"]) or []}
	except Exception:
		pass
	return sorted(keys)


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


def normalise_size(raw, cfg: dict) -> tuple[str | None, int | None, bool]:
	"""(label, rank, known). Cherry row sizes and melon counts alike, from the config's vocabulary."""
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
	return str(raw).strip(), None, False


def normalise_pack(raw, cfg: dict) -> tuple[str | None, float | None, bool]:
	text = str(raw or "").strip().lower()
	if not text:
		return None, None, True
	for pack in sorted(cfg.get("packs") or [], key=lambda p: -max(len(m) for m in p.get("match") or [""])):
		if any(m.lower() in text for m in pack.get("match") or []):
			return pack["label"], pack.get("net_lb"), True
	return str(raw).strip(), None, False


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


def http_get(slug: str, start: datetime.date, end: datetime.date, all_sections: bool = False):
	"""One MARS request. Returns (rows, error). The key comes from ERPNext MCP Settings; never logged."""
	import requests

	from .services import usda_prices

	key = usda_prices.api_key()
	if not key:
		return None, "no USDA MARS API key in ERPNext MCP Settings (usda_mars_api_key)"
	query = f"report_begin_date={start:%m/%d/%Y}:{end:%m/%d/%Y}"
	base = usda_prices.base_url().rstrip("/")
	url = f"{base}/reports/{slug}?q={query}&allSections=true" if all_sections else f"{base}/reports/{slug}/{DETAILS_PATH}?q={query}"
	try:
		response = requests.get(url, auth=(key, ""), timeout=usda_prices.TIMEOUT_SECONDS)
	except Exception as exc:
		return None, f"{type(exc).__name__} reaching MARS"
	if response.status_code != 200:
		return None, f"HTTP {response.status_code} from MARS for report {slug}"
	try:
		payload = response.json()
	except ValueError:
		return None, f"report {slug} answered with something that is not JSON"
	return extract_rows(payload), None


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
	"""Fetch every configured report for a commodity over a date range, window by window, and store it.
	Nothing is dropped silently: unknown sizes / packs, unparseable rows, HTTP errors, field-set changes and
	in-season days with no shipping-point report all become Market Data Issues."""
	cfg = config(key)
	first, last = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
	report = {"commodity": key, "from": start, "to": end, "stored": 0, "updated": 0, "unchanged": 0,
	          "not_quoted": 0, "issues": 0, "rows_seen": 0, "reports": []}
	shipping_days: set = set()
	for rep in cfg.get("reports") or []:
		role = rep.get("role")
		if roles and role not in roles:
			continue
		per = {"slug": rep["slug"], "role": role, "rows": 0, "kept": 0}
		signature_seen = None
		for a, b in _windows(first, last):
			rows, error = fetch(str(rep["slug"]), a, b)
			if rows is None and error and "HTTP" in error:
				rows, error2 = fetch(str(rep["slug"]), a, b, all_sections=True)
				error = error2 if rows is None else None
			if rows is None:
				flag("http_error", str(rep["slug"]), key, a.isoformat(), error or "no answer")
				report["issues"] += 1
				continue
			per["rows"] += len(rows)
			report["rows_seen"] += len(rows)
			if rows:
				signature = sorted({str(k).lower() for r in rows for k in r})
				if signature_seen is None:
					signature_seen = signature
					_check_signature(str(rep["slug"]), key, signature, report)
			for raw in rows:
				try:
					point = parse_row(raw, role, rep, cfg)
				except Exception as exc:
					flag("parse_error", str(rep["slug"]), key, None, f"{type(exc).__name__}: {exc}", raw)
					report["issues"] += 1
					continue
				if point is None:
					continue
				if not point["report_date"]:
					flag("parse_error", str(rep["slug"]), key, None, "row has no readable report date", raw)
					report["issues"] += 1
					continue
				if not point["_size_known"] and point["market_type"] != MOVEMENT:
					flag("unknown_size", str(rep["slug"]), key, point["report_date"], f"size {point['size']!r} is not in the vocabulary", raw)
					report["issues"] += 1
				if not point["_pack_known"]:
					flag("unknown_pack", str(rep["slug"]), key, point["report_date"], f"pack {point['package']!r} is not in packs", raw)
					report["issues"] += 1
				try:
					outcome, _name = store(point, raw, key)
				except Exception as exc:
					flag("parse_error", str(rep["slug"]), key, point["report_date"], f"not stored: {exc}", raw)
					report["issues"] += 1
					continue
				report[outcome] += 1
				per["kept"] += 1
				if point["quote_status"] == NOT_QUOTED:
					report["not_quoted"] += 1
				if point["market_type"] == SHIPPING:
					shipping_days.add(point["report_date"])
		report["reports"].append(per)
	if not roles or "shipping_point" in roles:
		for day in in_season_weekdays(cfg, first, last):
			if day.isoformat() not in shipping_days and not _has_shipping(key, day.isoformat()):
				flag("report_missing", ",".join(r["slug"] for r in cfg["reports"] if r["role"] == "shipping_point"), key,
				     day.isoformat(), "no shipping-point quote for an in-season weekday")
				report["issues"] += 1
	return report


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
	sizes = sorted({(r.get("size_rank") or 999, r.get("size") or "") for r in rows})
	series = []
	for _rank, label in sizes:
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
	for label in sorted({r.get("size") for r in rows if r.get("size")}, key=lambda s: next((r.get("size_rank") or 999 for r in rows if r.get("size") == s), 999)):
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
		for key in commodities():
			cfg = config(key)
			if not in_season(cfg, today):
				continue
			try:
				result = ingest(key, (today - datetime.timedelta(days=7)).isoformat(), today.isoformat())
				print(f"erpnext_mcp: market prices {key}: {result['stored']} stored, {result['updated']} updated, "
				      f"{result['not_quoted']} not quoted, {result['issues']} issue(s).")
			except Exception:
				frappe.log_error(title=f"market prices daily pull {key}", message=frappe.get_traceback())
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
