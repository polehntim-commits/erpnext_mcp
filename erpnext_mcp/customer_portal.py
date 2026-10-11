# SPDX-License-Identifier: MIT
"""The customer / first-handler portal — a read-only share for a customer the fruit is committed or delivered to.
v0.313.0 (docs/design/customer_portal_plan.md); was the packer portal, v0.276.0 (docs/contracts/packer_portal_v0_276.yaml).

ANY ERPNext Customer that buys, packs or first-handles the fruit — Orchard View, Monson, PFI, a future buyer — can
have a portal (`Customer Portal`, one per customer and season); Orchard View is just the first configured instance.
The issuing company is the grower entity. For the blocks committed to that customer, by the block's ticker code, and
for those blocks only —
  * sprays: product, EPA number, rate, date, REI and PHI, and the MRL status for each market the portal lists;
  * IPM: observations and threshold events;
  * projections: tons by block and variety from past seasons' scale tickets and the acres, the harvest window from
    past seasons' first and last tickets, recent size / quality notes;
  * clear to harvest per block: PHI cleared, no open re-entry interval, no market without an MRL on file.
Sections are toggled per portal. Downloads: CSV, XLSX and a PDF pack per block or season (the packer spray record and
GlobalG.A.P. fields), and the same data as a JSON feed their systems pull with the credential as a Bearer token.

NEVER: costs, prices, labour, people, HR, other customers' blocks or any block not committed. The data passes through
one allow-list per section (`ALLOWED`) and the tests read the answer for what must not be there.

ACCESS: a named credential per contact — a Share Link of kind "Customer Portal" (links issued as "Packer" before
v0.313.0 still work) — 32 random bytes shown once, only the hash kept, live until revoked or its expiry (a season by
default); every view, download and feed pull logged with the contact, time and address; the public routes are rate
limited per address. OFF for a company until `customer_portal_enabled` (or the older `packer_portal_enabled`) is on.
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json

import frappe

from . import compat

SHARE = "Customer Portal"
FLAG = "customer_portal_enabled"
#: v0.313.0. The v0.276.0 switch still turns a company's portals on.
LEGACY_FLAG = "packer_portal_enabled"
KIND = "Customer Portal"
#: Share Link kinds read as this portal's credentials: the current one, and links issued before v0.313.0.
KINDS = (KIND, "Packer")
ROLES = ("Packer", "Buyer", "First Handler")
SECTIONS = ("sprays", "ipm", "projections", "clearance", "feed")
DEFAULT_MARKETS = ("US", "Canada")
CREDENTIAL_DAYS = 400
FORMATS = ("csv", "xlsx", "pdf", "json")
#: What each section may carry, and nothing else.
ALLOWED = {
	"sprays": ("block", "variety", "date", "product", "epa_reg_number", "rate", "rate_uom", "rei_hours", "phi_days",
	           "phi_clears_on", "target", "lot_no", "mrl"),
	"ipm": ("block", "date", "type", "threat", "count", "sample_size", "percent_affected", "threshold_exceeded",
	        "beneficials", "stage"),
	"projections": ("block", "variety", "acres", "tons_per_acre", "projected_tons", "seasons_used", "window_start",
	                "window_end", "notes"),
	"clearance": ("block", "variety", "status", "phi_clears_on", "open_rei_until", "mrl_gaps", "reasons"),
}


class PortalError(Exception):
	pass


def enabled(company: str) -> bool:
	from . import flags

	return bool(company) and any(bool(flags.value(f, company=company, default=False)) for f in (FLAG, LEGACY_FLAG))


def hash_token(token: str) -> str:
	return hashlib.sha256(str(token or "").encode()).hexdigest()


def _now() -> datetime.datetime:
	return frappe.utils.get_datetime(frappe.utils.now())


def _json(raw, default):
	if isinstance(raw, (dict, list)):
		return raw
	try:
		return json.loads(raw) if raw else default
	except ValueError:
		return default


# ── the share ────────────────────────────────────────────────────────────────
def create_share(company: str, customer: str, fields, *, season: str = "", markets=(), sections=None,
                 role: str = "Packer", display_name: str = "", title: str = "", actor: str = "") -> str:
	"""A portal for `customer` (an ERPNext Customer), issued by the grower `company`."""
	from . import field_names

	if not frappe.db.exists("Company", company):
		raise PortalError(f"no Company {company!r}.")
	if not customer or not frappe.db.exists("Customer", customer):
		raise PortalError(f"customer: an ERPNext Customer (the buyer, packer or first handler) — {customer!r} is not one.")
	if role not in ROLES:
		raise PortalError(f"role is one of {', '.join(ROLES)}.")
	name_shown = display_name or frappe.db.get_value("Customer", customer, "customer_name") or customer
	blocks = []
	for name in fields or []:
		try:
			for field in field_names.resolve_many(name, [company]):
				if field not in blocks:
					blocks.append(field)
		except ValueError as exc:
			raise PortalError(str(exc)) from None
	if not blocks:
		raise PortalError("fields: the blocks committed to this customer (by name, alias or ticker).")
	doc = frappe.new_doc(SHARE)
	doc.share_title = (title or f"{name_shown} — {company} {season}".strip())[:140]
	doc.company = company
	doc.customer_name = name_shown
	doc.customer = customer
	doc.customer_role = role
	doc.status = "Active"
	doc.season = str(season or "") or None
	doc.markets = "\n".join(markets or DEFAULT_MARKETS)
	doc.sections = json.dumps({s: 1 for s in SECTIONS} | dict(sections or {}))
	for field in blocks:
		row = frappe.db.get_value("Field", field, ["field_name", "block_ticker", "variety"], as_dict=True) or {}
		doc.append("blocks", {"field": field, "block_ticker": row.get("block_ticker") or None,
		                      "variety": row.get("variety") or None})
	doc.insert(ignore_permissions=True)
	return doc.name


def issue_credential(share: str, contact_name: str, contact_email: str = "", days: int = CREDENTIAL_DAYS,
                     actor: str = "") -> dict:
	"""A named credential — a Share Link of kind Customer Portal (v0.313.0; Packer before) — shown ONCE."""
	from . import share_links

	doc = frappe.get_doc(SHARE, share)
	if not enabled(doc.company):
		raise PortalError(f"the customer portal is off for {doc.company} — turn on `{FLAG}` for it when it goes live.")
	if doc.status != "Active":
		raise PortalError(f"{share} is {doc.status}.")
	if not str(contact_name or "").strip():
		raise PortalError("contact_name: the person this credential is for.")
	made = share_links.issue(KIND, share, company=doc.company, actor=actor, days=days or CREDENTIAL_DAYS,
	                         contact_name=contact_name, contact_email=contact_email)
	return {"share": share, "contact": contact_name.strip(), "token": made["token"], "url": made["url"],
	        "feed": feed_url(), "expires_at": made["expires_at"], "shown_once": True}


def link_url(token: str) -> str:
	from . import share_links

	return share_links.url(KIND, token)


def feed_url() -> str:
	from . import settings

	base = settings.farmops_public_url() or str(frappe.utils.get_url() or "").rstrip("/")
	return f"{base}/farmops/api/portal-feed"


def revoke(share: str, contact_name: str = "", reason: str = "", actor: str = "") -> dict:
	"""One contact's credential, or (no contact) the whole share."""
	from . import share_links

	doc = frappe.get_doc(SHARE, share)
	hit = sum(share_links.revoke_target(kind, share, actor, reason or "revoked", contact_name=contact_name)
	          for kind in KINDS)
	if not contact_name:
		doc.status = "Revoked"
		doc.save(ignore_permissions=True)
	return {"share": share, "revoked": hit, "status": doc.status}


def credentials(share: str) -> list:
	"""Every credential issued for a share — never the token."""
	from . import share_links

	return [{"link": r["name"], "contact": r.get("contact_name"), "email": r.get("contact_email"),
	         "issued_at": r.get("issued_at") or "", "expires_at": r.get("expires_at") or "",
	         "revoked": r.get("status") == "Revoked", "status": r.get("status"),
	         "last_used": r.get("last_used_at") or None, "uses": int(r.get("use_count") or 0)}
	        for kind in KINDS for r in share_links.links(kind, target=share)]


def find(token: str) -> dict | None:
	"""{name, share, company, contact} for a live credential, else None — the same None for every reason."""
	from . import share_links

	row = None
	for kind in KINDS:
		row = share_links.find(token, kind)
		if row is not None:
			break
	if row is None:
		return None
	share = frappe.db.get_value(SHARE, row["target"], ["status", "company"], as_dict=True) or {}
	if share.get("status") != "Active" or not enabled(share.get("company")):
		return None
	return {**row, "share": row["target"], "company": share.get("company"), "contact": row.get("contact_name")}


def log(found: dict, what: str, *, detail: str = "", ip: str = "", user_agent: str = "") -> None:
	from . import share_links

	share_links.record_use(found, what, detail=detail, ip=ip, user_agent=user_agent)


def access_log(share: str = "", company: str = "", limit: int = 200) -> list:
	from . import share_links

	names = {}
	out = []
	cap = max(1, min(int(limit or 200), 2000))
	rows = [row for kind in KINDS for row in share_links.views(kind, target=share, company=company, limit=cap)]
	rows.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
	for row in rows[:cap]:
		if row["target"] not in names:
			names[row["target"]] = frappe.db.get_value(SHARE, row["target"], "customer_name")
		out.append({"portal": row["target"], "customer": names[row["target"]], "at": row["at"],
		            "contact": row.get("contact_name"), "what": row.get("what"), "detail": row.get("detail"),
		            "ip": row.get("ip")})
	return out


# ── what the customer sees ─────────────────────────────────────────────────────
def _share(share: str) -> dict:
	doc = frappe.get_doc(SHARE, share)
	blocks = [{"field": b.get("field"), "ticker": b.get("block_ticker") or b.get("field"), "variety": b.get("variety")}
	          for b in doc.get("blocks") or []]
	return {"doc": doc, "blocks": blocks, "sections": {s: bool(int(_json(doc.sections, {}).get(s, 1) or 0))
	                                                   for s in SECTIONS},
	        "markets": [m.strip() for m in str(doc.markets or "").splitlines() if m.strip()] or list(DEFAULT_MARKETS)}


def _season(share_doc, season: str = "") -> str:
	return str(season or share_doc.season or str(frappe.utils.today())[:4])


def _pick(row: dict, section: str) -> dict:
	return {k: row.get(k) for k in ALLOWED[section] if k in row}


def _mrl(chemical: str, crop: str, markets: list, company: str) -> dict:
	if not compat.doctype_exists("MRL Record") or not chemical:
		return {m: "no record" for m in markets}
	out = {}
	for market in markets:
		rows = frappe.db.get_all("MRL Record", filters={"chemical": chemical, "market": market},
		                         fields=compat.existing_fields("MRL Record", ("crop", "mrl_ppm", "company",
		                                                                       "substance_status")),
		                         limit=50) or []
		rows = [r for r in rows if not crop or not r.get("crop") or str(r.get("crop")).casefold() in str(crop).casefold()
		        or str(crop).casefold() in str(r.get("crop")).casefold()]
		rows = [r for r in rows if not r.get("company") or r.get("company") == company] or []
		if not rows:
			out[market] = "no record"
		elif str(rows[0].get("substance_status") or "").casefold() in ("banned", "not approved", "prohibited"):
			out[market] = "not approved"
		else:
			out[market] = f"{rows[0].get('mrl_ppm')} ppm"
	return out


def sprays(share: str, season: str = "") -> list:
	s = _share(share)
	doc = s["doc"]
	season = _season(doc, season)
	by_field = {b["field"]: b for b in s["blocks"]}
	out = []
	if not compat.doctype_exists("Spray Application"):
		return out
	fields = compat.existing_fields("Spray Application", ("name", "company", "status", "completed_at", "started_at",
	                                                     "products_applied", "rei_hours", "phi_days", "phi_clears_on"))
	for row in frappe.db.get_all("Spray Application", filters={"company": doc.company}, fields=fields, limit=20000) or []:
		day = str(row.get("completed_at") or row.get("started_at") or "")[:10]
		if not day.startswith(season) or str(row.get("status") or "") == "Cancelled":
			continue
		blocks = [b.get("block") for b in frappe.get_doc("Spray Application", row["name"]).get("blocks") or []]
		for block in [b for b in blocks if b in by_field]:
			crop = frappe.db.get_value("Field", block, "crop") or ""
			for product in _json(row.get("products_applied"), []):
				if not isinstance(product, dict):
					continue
				chemical = product.get("item_name") or product.get("item") or ""
				out.append(_pick({"block": by_field[block]["ticker"], "variety": by_field[block]["variety"], "date": day,
				                  "product": chemical, "epa_reg_number": product.get("epa_reg_number"),
				                  "rate": product.get("rate_per_acre"), "rate_uom": product.get("rate_uom"),
				                  "rei_hours": product.get("rei_hours") or row.get("rei_hours"),
				                  "phi_days": product.get("phi_days") or row.get("phi_days"),
				                  "phi_clears_on": str(row.get("phi_clears_on") or "")[:10] or None,
				                  "target": product.get("target"), "lot_no": product.get("lot_no"),
				                  "mrl": _mrl(_active(product), crop, s["markets"], doc.company)}, "sprays"))
	out.sort(key=lambda r: (r["date"], r["block"] or ""))
	return out


def _active(product: dict) -> str:
	"""The chemical an MRL is written against: the Item's active ingredients when known, else the product name."""
	item = product.get("item")
	if item and compat.has_field("Item", "active_ingredients"):
		raw = frappe.db.get_value("Item", item, "active_ingredients")
		parsed = _json(raw, None)
		if isinstance(parsed, list) and parsed:
			first = parsed[0]
			return str(first.get("name") if isinstance(first, dict) else first)
		if isinstance(raw, str) and raw.strip() and not raw.strip().startswith("["):
			return raw.split(",")[0].strip()
	return product.get("item_name") or item or ""


def ipm(share: str, season: str = "") -> list:
	s = _share(share)
	season = _season(s["doc"], season)
	by_field = {b["field"]: b for b in s["blocks"]}
	if not compat.doctype_exists("Crop Observation"):
		return []
	fields = compat.existing_fields("Crop Observation", ("block", "observed_on", "observation_type", "threat",
	                                                    "count_observed", "sample_size", "percent_affected",
	                                                    "threshold_exceeded", "beneficial_name", "growth_stage_code"))
	out = []
	for row in frappe.db.get_all("Crop Observation", filters={"block": ("in", list(by_field))}, fields=fields,
	                             limit=20000) or []:
		day = str(row.get("observed_on") or "")[:10]
		if not day.startswith(season) or row.get("observation_type") not in ("Pest", "Disease", "Beneficial",
		                                                                      "Growth Stage", "Threshold"):
			continue
		out.append(_pick({"block": by_field[row["block"]]["ticker"], "date": day, "type": row.get("observation_type"),
		                  "threat": row.get("threat"), "count": row.get("count_observed"),
		                  "sample_size": row.get("sample_size"), "percent_affected": row.get("percent_affected"),
		                  "threshold_exceeded": bool(int(row.get("threshold_exceeded") or 0)),
		                  "beneficials": row.get("beneficial_name"), "stage": row.get("growth_stage_code")}, "ipm"))
	out.sort(key=lambda r: (r["date"], r["block"] or ""))
	return out


def _tons(row: dict) -> float:
	weight = float(row.get("net_weight") or 0)
	unit = str(row.get("weight_uom") or "lb").casefold()
	return weight / 2000 if unit in ("lb", "lbs", "pound", "pounds") else weight / 907.185 if unit in ("kg", "kgs") \
		else weight


def projections(share: str, season: str = "") -> list:
	"""Tons from past seasons' scale tickets per acre × acres; the window from past first / last tickets."""
	s = _share(share)
	season = int(_season(s["doc"], season))
	out = []
	for block in s["blocks"]:
		field = block["field"]
		acres = float(frappe.db.get_value("Field", field, "area_computed_acres") or
		              frappe.db.get_value("Field", field, "acreage") or 0)
		tickets = frappe.db.get_all("Scale Ticket", filters={"field": field}, fields=["date", "net_weight", "weight_uom"],
		                            limit=5000) if compat.doctype_exists("Scale Ticket") else []
		by_year: dict = {}
		for t in tickets or []:
			year = int(str(t.get("date") or "0000")[:4] or 0)
			if not year or year >= season:
				continue
			entry = by_year.setdefault(year, {"tons": 0.0, "days": []})
			entry["tons"] += _tons(t)
			day = datetime.date.fromisoformat(str(t["date"])[:10])
			entry["days"].append(day.timetuple().tm_yday)
		per_acre = [v["tons"] / acres for v in by_year.values() if acres]
		tpa = round(sum(per_acre) / len(per_acre), 2) if per_acre else None
		starts = sorted(min(v["days"]) for v in by_year.values() if v["days"])
		ends = sorted(max(v["days"]) for v in by_year.values() if v["days"])

		def as_date(days):
			if not days:
				return None
			median = days[len(days) // 2]
			return str(datetime.date(season, 1, 1) + datetime.timedelta(days=median - 1))

		notes = []
		if compat.doctype_exists("Crop Observation"):
			for obs in frappe.db.get_all("Crop Observation", filters={"block": field},
			                             fields=compat.existing_fields("Crop Observation", ("observed_on", "observation_type",
			                                                                               "brix_reading", "notes")),
			                             order_by="observed_on desc", limit=50) or []:
				if obs.get("brix_reading"):
					notes.append(f"{str(obs.get('observed_on'))[:10]}: Brix {obs['brix_reading']}")
		out.append(_pick({"block": block["ticker"], "variety": block["variety"], "acres": round(acres, 2),
		                  "tons_per_acre": tpa, "projected_tons": round(tpa * acres, 1) if tpa else None,
		                  "seasons_used": sorted(by_year), "window_start": as_date(starts), "window_end": as_date(ends),
		                  "notes": notes[:3]}, "projections"))
	return out


def clearance(share: str, as_of: str = "") -> list:
	"""Per block: Clear, or Not yet (PHI / an open REI), or Check (a market with no MRL on file)."""
	s = _share(share)
	today = as_of or str(frappe.utils.today())
	season = today[:4]
	recent = sprays(share, season)
	out = []
	for block in s["blocks"]:
		rows = [r for r in recent if r["block"] == block["ticker"]]
		phi = max((r["phi_clears_on"] for r in rows if r.get("phi_clears_on")), default=None)
		rei = None
		if compat.doctype_exists("Spray REI"):
			for r in frappe.db.get_all("Spray REI", filters={"block": block["field"], "status": "Active"},
			                           fields=["expires_at"], limit=50) or []:
				until = str(r.get("expires_at") or "")[:16]
				if until and until > str(_now())[:16]:
					rei = max(rei or "", until)
		gaps = sorted({f"{r['product']} → {m}" for r in rows for m, v in (r.get("mrl") or {}).items()
		               if v in ("no record", "not approved")})
		reasons = []
		if phi and phi > today:
			reasons.append(f"PHI until {phi}")
		if rei:
			reasons.append(f"re-entry closed until {rei}")
		if gaps:
			reasons.append(f"{len(gaps)} product / market without an MRL on file")
		status = "Not yet" if (phi and phi > today) or rei else ("Check" if gaps else "Clear")
		out.append(_pick({"block": block["ticker"], "variety": block["variety"], "status": status,
		                  "phi_clears_on": phi, "open_rei_until": rei, "mrl_gaps": gaps, "reasons": reasons},
		                 "clearance"))
	return out


def page(share: str, season: str = "") -> dict:
	s = _share(share)
	doc = s["doc"]
	data = {"customer": doc.customer_name, "role": doc.get("customer_role") or "Packer", "grower": doc.company, "season": _season(doc, season), "markets": s["markets"],
	        "blocks": [{"block": b["ticker"], "variety": b["variety"]} for b in s["blocks"]],
	        "sections": [k for k, on in s["sections"].items() if on and k != "feed"]}
	for name, fn in (("sprays", sprays), ("ipm", ipm), ("projections", projections)):
		if s["sections"][name]:
			data[name] = fn(share, season)
	if s["sections"]["clearance"]:
		data["clearance"] = clearance(share)
	return data


# ── downloads ────────────────────────────────────────────────────────────────
def pack(share: str, *, block: str = "", season: str = "", fmt: str = "csv") -> tuple:
	"""(bytes, content type, file name) — one block (by ticker) or every committed block, for a season."""
	if fmt not in FORMATS:
		raise PortalError(f"format is one of {', '.join(FORMATS)}")
	data = page(share, season)
	if block:
		if block not in [b["block"] for b in data["blocks"]]:
			raise PortalError(f"{block!r} is not one of this portal's blocks.")
		for name in ("sprays", "ipm", "projections", "clearance"):
			if name in data:
				data[name] = [r for r in data[name] if r.get("block") == block]
		data["blocks"] = [b for b in data["blocks"] if b["block"] == block]
	stem = f"{data['grower']}_{block or 'all-blocks'}_{data['season']}".replace(" ", "_").replace(",", "")
	if fmt == "json":
		return json.dumps(data, default=str, indent=1).encode(), "application/json", f"{stem}.json"
	if fmt == "csv":
		buffer = io.StringIO()
		writer = csv.writer(buffer)
		writer.writerow(["Spray record — " + data["grower"], f"{data['role']}: " + data["customer"], "Season " + data["season"]])
		writer.writerow(["Block", "Variety", "Date", "Product", "EPA Reg No", "Rate", "Unit", "REI h", "PHI d",
		                 "PHI clears", "Target", "Lot", *[f"MRL {m}" for m in data["markets"]]])
		for r in data.get("sprays") or []:
			writer.writerow([r.get("block"), r.get("variety"), r.get("date"), r.get("product"), r.get("epa_reg_number"),
			                 r.get("rate"), r.get("rate_uom"), r.get("rei_hours"), r.get("phi_days"),
			                 r.get("phi_clears_on"), r.get("target"), r.get("lot_no"),
			                 *[(r.get("mrl") or {}).get(m) for m in data["markets"]]])
		return buffer.getvalue().encode("utf-8"), "text/csv; charset=utf-8", f"{stem}.csv"
	if fmt == "xlsx":
		from .render.xlsx import Sheet, XlsxWorkbook

		book = XlsxWorkbook(title=f"Customer portal pack {stem}")
		book.add(Sheet("Sprays", ["Block", "Variety", "Date", "Product", "EPA Reg No", "Rate", "Unit", "REI h", "PHI d",
		                          "PHI clears", "Target", "Lot", *[f"MRL {m}" for m in data["markets"]]],
		               [[r.get("block"), r.get("variety"), r.get("date"), r.get("product"), r.get("epa_reg_number"),
		                 r.get("rate"), r.get("rate_uom"), r.get("rei_hours"), r.get("phi_days"), r.get("phi_clears_on"),
		                 r.get("target"), r.get("lot_no"), *[(r.get("mrl") or {}).get(m) for m in data["markets"]]]
		                for r in data.get("sprays") or []]))
		book.add(Sheet("IPM", ["Block", "Date", "Type", "Threat", "Count", "Sample", "% affected", "Over threshold",
		                       "Beneficials", "Stage"],
		               [[r.get(k) for k in ("block", "date", "type", "threat", "count", "sample_size", "percent_affected",
		                                    "threshold_exceeded", "beneficials", "stage")] for r in data.get("ipm") or []]))
		book.add(Sheet("Projections", ["Block", "Variety", "Acres", "t/ac", "Projected t", "Window start", "Window end"],
		               [[r.get(k) for k in ("block", "variety", "acres", "tons_per_acre", "projected_tons", "window_start",
		                                    "window_end")] for r in data.get("projections") or []]))
		book.add(Sheet("Clear to harvest", ["Block", "Variety", "Status", "PHI clears", "Re-entry until", "Why"],
		               [[r.get("block"), r.get("variety"), r.get("status"), r.get("phi_clears_on"), r.get("open_rei_until"),
		                 "; ".join(r.get("reasons") or [])] for r in data.get("clearance") or []]))
		return book.render(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"{stem}.xlsx"
	from .render.pdf import PdfDocument

	doc = PdfDocument(title=f"Spray record — {data['grower']}", author="erpnext_mcp", subject="Customer portal pack",
	                  footer=f"{data['grower']} for {data['customer']} — {data['season']}")
	doc.title_block("Grower spray record and pre-harvest status", f"{data['grower']} for {data['customer']}",
	                f"Season {data['season']} — " + (f"block {block}" if block else "all committed blocks"))
	doc.key_values([("Grower (GlobalG.A.P. producer)", data["grower"]), (data["role"], data["customer"]),
	                ("Blocks", ", ".join(f"{b['block']} ({b['variety'] or '—'})" for b in data["blocks"])),
	                ("Export markets", ", ".join(data["markets"]))])
	if data.get("clearance") is not None:
		doc.heading("Clear to harvest")
		doc.table(["Block", "Status", "PHI clears", "Why"], [[r.get("block"), r.get("status"), r.get("phi_clears_on") or "",
		                                                       "; ".join(r.get("reasons") or [])]
		                                                      for r in data["clearance"]])
	if data.get("sprays") is not None:
		doc.heading("Applications (crop protection record)")
		doc.table(["Date", "Block", "Product", "EPA Reg No", "Rate", "REI h", "PHI d", "Target"],
		          [[r.get("date"), r.get("block"), r.get("product"), r.get("epa_reg_number") or "",
		            f"{r.get('rate') or ''} {r.get('rate_uom') or ''}".strip(), r.get("rei_hours") or "",
		            r.get("phi_days") or "", r.get("target") or ""] for r in data["sprays"]] or [["—"] * 8])
		doc.heading("MRL status by market")
		doc.table(["Product", *data["markets"]],
		          [[p, *[next((r["mrl"].get(m) for r in data["sprays"] if r["product"] == p), "") for m in data["markets"]]]
		           for p in sorted({r["product"] for r in data["sprays"]})] or [["—"] * (1 + len(data["markets"]))])
	if data.get("ipm") is not None:
		doc.heading("IPM observations")
		doc.table(["Date", "Block", "Type", "Threat", "Over threshold"],
		          [[r.get("date"), r.get("block"), r.get("type"), r.get("threat") or "",
		            "yes" if r.get("threshold_exceeded") else ""] for r in data["ipm"]] or [["—"] * 5])
	return doc.render(), "application/pdf", f"{stem}.pdf"
