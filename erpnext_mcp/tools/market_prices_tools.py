# SPDX-License-Identifier: MIT
"""USDA AMS market prices: the MCP tools. v0.265.0. See `erpnext_mcp/market_prices.py`.

Reads are on; fetching, backfilling and probing are off until an operator switches them on. The key is read from
ERPNext MCP Settings and never appears in an answer. Shipping point is the primary line; terminal is context, and
the difference between them is the cost of market access — never margin.
"""

from __future__ import annotations

import datetime

import frappe

from .. import market_prices as mp
from ..args import as_bool, as_int, as_str
from ..errors import ToolError
from ..result import ToolResult


def _key(args: dict) -> str:
	key = as_str(args, "commodity") or "sweet_cherries"
	try:
		mp.config(key)
	except ValueError as exc:
		raise ToolError(str(exc)) from None
	return key


def _wrap(fn):
	try:
		return fn()
	except ValueError as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None


def get_market_prices(args: dict) -> ToolResult:
	key = _key(args)
	market_type = as_str(args, "market_type") or mp.SHIPPING
	if market_type not in (mp.SHIPPING, mp.TERMINAL, mp.MOVEMENT):
		raise ToolError("market_type is Shipping Point (default), Terminal or Movement.")
	start = as_int(args, "start")
	start = 0 if start is None else max(0, start)
	limit = as_int(args, "limit")
	limit = 500 if limit is None else max(1, limit)
	rows = mp.points(key, market_type, as_str(args, "variety"), as_str(args, "size"), as_str(args, "district"),
	                 as_str(args, "from"), as_str(args, "to"), as_int(args, "season"), limit_start=start, limit=limit + 1)
	more = len(rows) > limit
	rows = rows[:limit]
	for r in rows:
		r["mid"] = mp.mid(r) if r.get("quote_status") == mp.PRICED else None
		r["report_date"] = str(r["report_date"])[:10]
	return ToolResult(
		data={"commodity": key, "market_type": market_type, "points": rows, "next_start": start + limit if more else None,
		      "note": "Not Quoted rows are items the report listed without a price — gaps, never filled."},
		summary=f"{len(rows)} {market_type} point(s) for {key}" + (" (more)" if more else ""),
	)


def get_price_trend(args: dict) -> ToolResult:
	key = _key(args)
	interval = as_str(args, "interval") or "day"
	overlays = args.get("overlays") or []
	if isinstance(overlays, str):
		overlays = [o.strip() for o in overlays.split(",") if o.strip()]
	data = _wrap(lambda: mp.chart(key, as_str(args, "variety"), as_str(args, "size"), as_str(args, "district"), interval,
	                              as_int(args, "season"), as_str(args, "from"), as_str(args, "to"), overlays))
	rows = mp.points(key, mp.SHIPPING, as_str(args, "variety"), as_str(args, "size"), as_str(args, "district"))
	vol = mp.volume_series(key, interval="week")
	data["signal"] = mp.signal(mp.week_change(rows), mp._pct(vol[-1]["value"], vol[-2]["value"]) if len(vol) >= 2 else None)
	data["percentiles"] = mp.percentiles(key, as_str(args, "size"))
	data["season_curve"] = mp.season_curve(key, as_str(args, "size"))
	return ToolResult(data=data, summary=f"{key}: {sum(len(s['candles']) for s in data['series'])} {interval} candle(s) across "
	                                     f"{len(data['series'])} size(s); signal {data['signal']['label']}")


def get_grower_return_vs_breakeven(args: dict) -> ToolResult:
	key = _key(args)
	data = mp.card(key, as_str(args, "variety"), as_str(args, "size"))
	be = data.get("breakeven")
	gr = data.get("grower_return_per_lb")
	summary = (f"{key}: grower return ≈ ${gr:.2f}/lb vs breakeven ${be['per_lb']:.2f}/lb ({be['label']})"
	           if gr is not None and be else f"{key}: not enough to compare (latest quote {data.get('as_of') or 'none'})")
	return ToolResult(data=data, summary=summary)


def list_market_data_issues(args: dict) -> ToolResult:
	filters = {"status": as_str(args, "status") or "Open"}
	for f in ("commodity_key", "kind", "report_slug"):
		if as_str(args, f):
			filters[f] = as_str(args, f)
	if as_str(args, "commodity"):
		filters["commodity_key"] = as_str(args, "commodity")
	start = as_int(args, "start")
	start = 0 if start is None else max(0, start)
	rows = frappe.db.get_all(mp.ISSUE, filters=filters, fields=["name", "report_slug", "commodity_key", "report_date", "kind",
	                                                             "status", "detail", "count", "first_seen", "last_seen"],
	                         order_by="last_seen desc", limit_start=start, limit_page_length=201) or []
	more = len(rows) > 200
	return ToolResult(data={"issues": [dict(r) for r in rows[:200]], "next_start": start + 200 if more else None},
	                  summary=f"{min(len(rows), 200)} market data issue(s)")


def fetch_market_reports(args: dict) -> ToolResult:
	key = _key(args)
	today = str(frappe.utils.today())[:10]
	start = as_str(args, "from") or (datetime.date.fromisoformat(today) - datetime.timedelta(days=7)).isoformat()
	end = as_str(args, "to") or today
	roles = args.get("roles") or None
	data = _wrap(lambda: mp.ingest(key, start, end, roles=roles))
	return ToolResult(data=data, summary=f"{key} {start}–{end}: {data['stored']} stored, {data['updated']} updated, "
	                                     f"{data['not_quoted']} not quoted, {data['issues']} issue(s)",
	                  docstatus_delta="none → 0 (created)")


def backfill_market_reports(args: dict) -> ToolResult:
	key = _key(args)
	seasons = [int(s) for s in (args.get("seasons") or [])]
	if not seasons:
		raise ToolError("seasons is required, e.g. [2023, 2024, 2025].")
	data = _wrap(lambda: mp.backfill(key, seasons, roles=args.get("roles") or None))
	return ToolResult(data=data, summary=f"{key} seasons {seasons}: {data['stored']} stored, {data['issues']} issue(s)",
	                  docstatus_delta="none → 0 (created)")


def probe_market_report(args: dict) -> ToolResult:
	slug = as_str(args, "report", required=True)
	start = as_str(args, "from", required=True)
	end = as_str(args, "to") or start
	data = _wrap(lambda: mp.probe(slug, start, end, save=as_bool(args, "save", True)))
	return ToolResult(data=data, summary=f"report {slug}: " + "; ".join(
		f"{'allSections' if a['all_sections'] else 'Report Details'} {a['rows']} row(s)" + (f" ({a['error']})" if a["error"] else "")
		for a in data["answers"]))


# ── discovery ───────────────────────────────────────────────────────────────
def list_market_catalog(args: dict) -> ToolResult:
	"""The browsable index: commodities seen in observed reports (default), or the report catalog itself."""
	view = as_str(args, "view") or "commodities"
	start = as_int(args, "start")
	start = 0 if start is None else max(0, start)
	limit = as_int(args, "limit")
	limit = 200 if limit is None else max(1, limit)
	if view == "reports":
		filters = {}
		if as_str(args, "role"):
			filters["role"] = as_str(args, "role")
		if not as_bool(args, "include_stale", False):
			filters["stale"] = 0
		if as_str(args, "search"):
			filters["report_title"] = ("like", f"%{as_str(args, 'search')}%")
		rows = frappe.db.get_all(mp.REPORT, filters=filters, fields=["slug_id", "report_title", "slug_name", "role", "office",
		                                                              "last_published", "stale", "commodities", "observed_to"],
		                         order_by="role asc, report_title asc", limit_start=start, limit_page_length=limit + 1) or []
		more = len(rows) > limit
		return ToolResult(data={"view": view, "reports": [dict(r) for r in rows[:limit]], "next_start": start + limit if more else None},
		                  summary=f"{min(len(rows), limit)} report(s)")
	entries = mp.available(as_str(args, "search"), as_str(args, "role"), as_bool(args, "include_stale", False))
	page = entries[start:start + limit]
	return ToolResult(data={"view": "commodities", "commodities": page, "total": len(entries),
	                        "next_start": start + limit if start + limit < len(entries) else None,
	                        "note": "Seen in observed reports only — observe_market_reports adds more. `config` names the "
	                                "Market Commodity that covers a commodity; `published` whether it is live."},
	                  summary=f"{len(page)} of {len(entries)} commodity / commodities")


def refresh_market_catalog(args: dict) -> ToolResult:
	data = _wrap(lambda: mp.refresh_catalog())
	return ToolResult(data=data, summary=f"{data['reports']} report(s) in the MARS index: {data['created']} new, {data['refreshed']} refreshed",
	                  docstatus_delta="none → 0 (created)")


def observe_market_reports(args: dict) -> ToolResult:
	slugs = [str(s) for s in (args.get("reports") or [])]
	if not slugs:
		role = as_str(args, "role") or "shipping_point"
		slugs = [r["slug_id"] for r in frappe.db.get_all(mp.REPORT, filters={"role": role, "stale": 0}, fields=["slug_id"]) or []]
	if not slugs:
		raise ToolError("no reports to observe — run refresh_market_catalog first, or name reports.")
	today = str(frappe.utils.today())[:10]
	start = as_str(args, "from") or (datetime.date.fromisoformat(today) - datetime.timedelta(days=14)).isoformat()
	end = as_str(args, "to") or today
	data = _wrap(lambda: mp.observe(slugs, start, end))
	seen = sorted({c for r in data["reports"] for c in r["commodities"]})
	return ToolResult(data={**data, "commodities_seen": seen},
	                  summary=f"{len(slugs)} report(s) observed {start}–{end}: {len(seen)} commodity / commodities",
	                  docstatus_delta="0 → 0 (updated)")


def draft_market_commodity(args: dict) -> ToolResult:
	commodity = as_str(args, "commodity", required=True)
	data = _wrap(lambda: mp.draft_from_observations(commodity, as_str(args, "key"), frappe.session.user))
	return ToolResult(data=data, summary=f"drafted Market Commodity {data['key']} — a person publishes it",
	                  docstatus_delta="none → 0 (draft)")
