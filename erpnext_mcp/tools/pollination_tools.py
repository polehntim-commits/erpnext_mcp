# SPDX-License-Identifier: MIT
"""Pollination — the MCP side. v0.275.0 (docs/contracts/pollination_v0_275.yaml).

Two reads (the hive map; the counts and flags, or a block's history); six writes, OFF until switched on. Creating
the season's job is refused for a company until `pollination_enabled` is on for it; sharing the beekeeper's link
still needs `job_links_enabled`.
"""

from __future__ import annotations

import json

import frappe

from .. import pollination
from ..args import as_int, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult


def _actor() -> str:
	from .. import security

	return security.caller_identity() or str(getattr(frappe.session, "user", "") or "")


def _wrap(fn):
	try:
		return fn()
	except pollination.PollinationError as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None


def _point(raw):
	if not raw:
		return None
	if isinstance(raw, (list, tuple)) and len(raw) == 2:
		return float(raw[0]), float(raw[1])
	parts = [p.strip() for p in str(raw).split(",")]
	if len(parts) != 2:
		raise ToolError("a point is 'lat, lon'.")
	return float(parts[0]), float(parts[1])


def _list(raw, what: str):
	if isinstance(raw, str):
		try:
			return json.loads(raw)
		except ValueError:
			if what == "fields":
				return [f.strip() for f in raw.replace("+", ",").split(",") if f.strip()]
			raise ToolError(f"{what}: a JSON list.") from None
	return raw or []


def _season(args: dict) -> int:
	return as_int(args, "season", int(str(frappe.utils.today())[:4]))


def plan_hive_placement(args: dict) -> ToolResult:
	company = resolve_company(as_str(args, "company"), required=True)
	settings = {k: args[k] for k in ("hives_per_acre", "hives_per_pallet", "pallets_per_drop", "clearance_ft",
	                                 "petal_fall_bbch") if args.get(k) not in (None, "")}
	if isinstance(args.get("machine"), dict):
		settings["machine"] = args["machine"]
	if args.get("rate_per_hive") not in (None, ""):
		settings["rental"] = {"rate_per_hive": float(args["rate_per_hive"])}
	data = _wrap(lambda: pollination.place(company, _season(args), _list(args.get("fields"), "fields"),
	                                       loading_area=_point(args.get("loading_area")), actor=_actor(),
	                                       save=bool(args.get("save", True)), **settings))
	return ToolResult(data=data, summary=f"{len(data['drops'])} drop(s), {data['hives']} hives on {data['pallets']} "
	                                     f"pallet(s), {len(data['trips'])} trip(s)" + (f"; plan {data['plan']} drafted"
	                                                                                    if data.get("version") else ""),
	                  docstatus_delta="0 → 0 (draft)" if data.get("version") else "")


def update_hive_drops(args: dict) -> ToolResult:
	job = as_str(args, "job", required=True)
	data = _wrap(lambda: pollination.update_drops(job, _list(args.get("moves"), "moves"), _actor()))
	return ToolResult(data=data, summary=f"{job}: drops moved; {len(data['trips'])} trip(s); "
	                                     f"{len(data['warnings'])} warning(s)", docstatus_delta="0 → 0 (updated)")


def get_hive_map(args: dict) -> ToolResult:
	job = as_str(args, "job", required=True)
	data = _wrap(lambda: pollination.hive_map(job))
	return ToolResult(data=data, summary=f"{job}: {len(data['drops'])} drop(s) on {len(data['blocks'])} block(s)")


def create_pollination_job(args: dict) -> ToolResult:
	company = resolve_company(as_str(args, "company"), required=True)
	data = _wrap(lambda: pollination.create_job(company, _season(args), supplier=as_str(args, "supplier"),
	                                            start_date=as_str(args, "start_date"), end_date=as_str(args, "end_date"),
	                                            contact_name=as_str(args, "contact_name"),
	                                            contact_phone=as_str(args, "contact_phone"), actor=_actor()))
	return ToolResult(data=data, summary=f"{data['job']}: {data['expected']} hives, {len(data['distribute_tasks'])} "
	                                     "distribution trip(s) held until the beekeeper delivers",
	                  docstatus_delta="0 → 0 (created)")


def get_pollination_status(args: dict) -> ToolResult:
	data = _wrap(lambda: pollination.status(as_str(args, "job"), as_str(args, "field"), as_str(args, "company")))
	if data.get("job"):
		return ToolResult(data=data, summary=f"{data['job']}: {data['counts']}; {len(data['flags'])} flag(s)")
	return ToolResult(data=data, summary=f"{len(data['seasons'])} season(s)")


def resolve_pollination_flag(args: dict) -> ToolResult:
	job = as_str(args, "job", required=True)
	data = _wrap(lambda: pollination.resolve_flag(job, as_str(args, "flag", required=True), as_str(args, "note"),
	                                              _actor()))
	return ToolResult(data=data, summary=f"{job}: flag resolved", docstatus_delta="0 → 0 (updated)")


def release_hive_gather(args: dict) -> ToolResult:
	job = as_str(args, "job", required=True)
	data = _wrap(lambda: pollination.release_gather(job, _actor()))
	return ToolResult(data=data, summary=f"{job}: {len(data['released'])} gather-up trip(s) on the board",
	                  docstatus_delta="0 → 0 (updated)")


def link_pollination_invoice(args: dict) -> ToolResult:
	job = as_str(args, "job", required=True)
	data = _wrap(lambda: pollination.link_invoice(job, as_str(args, "invoice", required=True), _actor()))
	return ToolResult(data=data, summary=f"{job} ↔ {data['invoice']}: " + ("matches" if data["matches"]
	                                                                        else "; ".join(data["issues"])),
	                  docstatus_delta="0 → 0 (updated)")
