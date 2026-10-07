# SPDX-License-Identifier: MIT
"""The Data Access policy, read. v0.267.0 (docs/contracts/data_access_v0_267.yaml).

Read-only. Drafting is a Farm Config Version Draft in the Desk; publishing is a System Manager's, in the Desk.
"""

from __future__ import annotations

from .. import data_access
from ..args import as_str
from ..errors import ToolError
from ..result import ToolResult


def get_data_access_policy(args: dict) -> ToolResult:
	"""The policy in force (published, else the seed), or what one tier sees on one route."""
	tier = as_str(args, "tier")
	route = as_str(args, "route")
	if tier and tier not in data_access.TIERS:
		raise ToolError(f"tier is one of {', '.join(data_access.TIERS)}.")
	data = data_access.describe(tier, route)
	if route:
		summary = (f"{route}: not listed" if not data["listed"] else
		           f"{route}: gates {', '.join(data['gates']) or 'enrolled'}; {len(data['resources'])} resource(s)")
	else:
		summary = f"{data['routes']} route(s), {len(data['resources'] or {})} resource(s)"
	return ToolResult(data=data, summary=summary)
