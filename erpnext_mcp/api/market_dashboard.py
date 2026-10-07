# SPDX-License-Identifier: MIT
"""/app/market-prices — the USDA market dashboard in the Desk. v0.266.0 (Tim, 2026-10-06).

ONE SOURCE OF TRUTH: every number here is `market_prices.chart()` / `market_prices.card()` — what the phone's
get_market_chart / get_market_card and the MCP tools answer. The page adds the bench view: candles per size with
a volume pane, season-over-season by week of season, weekly bars, overlays, filters, and the open data issues.

Reading needs read permission on USDA Price Quote (Farm Manager, Foreman, Accounts, System Manager). Shipping
point is the primary line; terminal is context, and the difference is the cost of market access — never margin.
"""

from __future__ import annotations

import frappe

from .. import compat
from .. import market_prices as mp
from ..errors import ToolError
from .gis import speaks_frappe


def _require_reader() -> None:
	if not frappe.has_permission(mp.QUOTE, "read", user=frappe.session.user):
		raise ToolError("The market dashboard needs read access to USDA Price Quote (Farm Manager, Foreman, Accounts).")


def _choices(commodity=None) -> dict:
	_require_reader()
	key = commodity or "sweet_cherries"
	try:
		cfg = mp.config(key)
	except ValueError as exc:
		raise ToolError(str(exc)) from None
	rows = frappe.db.get_all(mp.QUOTE, filters={"commodity_key": key}, fields=["variety", "size", "size_rank", "district", "season"],
	                         limit_page_length=0) or []
	ranks = {r["size"]: r.get("size_rank") for r in rows if r.get("size")}
	sizes = sorted(ranks, key=lambda label: mp.size_order(label, ranks[label]))
	return {
		"commodities": [{"key": k, "title": mp.config(k).get("title") or k} for k in mp.commodities()],
		"commodity": key,
		"varieties": sorted({r["variety"] for r in rows if r.get("variety")}),
		"sizes": sizes or [v["label"] for v in (cfg.get("size") or {}).get("vocabulary") or []],
		"districts": sorted({r["district"] for r in rows if r.get("district")}),
		"seasons": sorted({int(r["season"]) for r in rows if r.get("season")}, reverse=True),
		"headline_size": cfg.get("headline_size"),
		"config_version": cfg.get("_version"),
	}


def _view(commodity=None, variety=None, size=None, district=None, interval=None, season=None, from_date=None,
          to_date=None, overlays=None) -> dict:
	_require_reader()
	key = commodity or "sweet_cherries"
	wanted = [o.strip() for o in str(overlays or "").split(",") if o.strip()]
	try:
		chart = mp.chart(key, variety or "", size or "", district or "", interval or "day",
		                 int(season) if str(season or "").isdigit() else None, from_date or "", to_date or "", wanted)
		card = mp.card(key, variety or "", size or "")
	except ValueError as exc:
		raise ToolError(str(exc)) from None
	issues = frappe.db.get_all(mp.ISSUE, filters={"commodity_key": key, "status": "Open"},
	                           fields=["name", "kind", "report_slug", "report_date", "detail", "count"],
	                           order_by="last_seen desc", limit_page_length=50) if compat.doctype_exists(mp.ISSUE) else []
	return {"chart": chart, "card": card, "issues": [dict(r) for r in issues or []],
	        "percentiles": mp.percentiles(key, size or card.get("headline_size") or ""),
	        "season_curve": mp.season_curve(key, size or "")}


@frappe.whitelist()
def choices(commodity=None):
	"""What the filters offer for one commodity."""
	return speaks_frappe(_choices, commodity=commodity)


@frappe.whitelist()
def view(commodity=None, variety=None, size=None, district=None, interval=None, season=None, from_date=None,
         to_date=None, overlays=None):
	"""Everything the dashboard draws: the chart, the card, open issues and the season percentiles."""
	return speaks_frappe(_view, commodity=commodity, variety=variety, size=size, district=district, interval=interval,
	                     season=season, from_date=from_date, to_date=to_date, overlays=overlays)
