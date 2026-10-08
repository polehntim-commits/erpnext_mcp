# SPDX-License-Identifier: MIT
"""Seasonal work lists — the MCP reads. v0.270.0 (docs/contracts/seasonal_v0_270.yaml).

Editing a program or its checklists is the generic config / task-template tools; publishing is a person's, in the
Desk. These two only read.
"""

from __future__ import annotations

from .. import phone_config, seasonal
from ..args import as_int, as_str
from ..result import ToolResult


def list_seasonal_work(args: dict) -> ToolResult:
	"""Every seasonal program: published or draft, its trigger state per company, its checklists (enabled or not),
	and how many tasks are done / open this season."""
	wanted = as_str(args, "company")
	programs = []
	keys = sorted(set(seasonal.SEED_PROGRAMS) | {r["config_key"] for r in phone_config.rows(seasonal.KIND)})
	for key in keys:
		doc = phone_config.doc_of(seasonal.KIND, key, status=phone_config.PUBLISHED)
		draft = None if doc else (phone_config.rows(seasonal.KIND, key) or [None])[0]
		body = phone_config.body_of(doc) if doc else seasonal.SEED_PROGRAMS.get(key, {})
		companies = [wanted] if wanted else seasonal._companies(body)
		programs.append({
			"program": key,
			"title": (body.get("title") or {}).get("en") or key,
			"published": bool(doc),
			"draft": (draft or {}).get("name") if draft else None,
			"trigger": body.get("trigger"),
			"checklists": [{"template": i["template"], "asset_types": i.get("asset_types") or [],
			                "scope": i.get("scope", "asset"), "enabled": seasonal._template_enabled(i["template"])}
			               for i in body.get("items") or []],
			"companies": [{"company": c, **seasonal.triggered(body, c), "targets": len(seasonal.targets(body, c)),
			               **{k: v for k, v in seasonal.status(key, company=c).items() if k in ("total", "done", "open")}}
			              for c in companies] if doc else [],
		})
	live = [p["program"] for p in programs if p["published"]]
	return ToolResult(data={"programs": programs},
	                  summary=f"{len(programs)} seasonal program(s); published: {', '.join(live) or 'none'}")


def get_winterize_status(args: dict) -> ToolResult:
	"""Fall winterize this season: done / open per asset, open ones soonest-due first."""
	data = seasonal.status(as_str(args, "program") or seasonal.FALL, as_int(args, "year"), as_str(args, "company"))
	return ToolResult(data=data, summary=f"{data['program']} {data['year']}: {data['done']} done, {data['open']} open")
