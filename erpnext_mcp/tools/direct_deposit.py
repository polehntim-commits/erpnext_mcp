# SPDX-License-Identifier: MIT
"""list_direct_deposit_changes. v0.225.0 — docs/design/direct_deposit_setup.md."""

from __future__ import annotations

from .. import direct_deposit
from ..args import as_limit
from ..result import ToolResult
from .employee import require_hr_role


def list_direct_deposit_changes(args: dict) -> ToolResult:
	"""Read-only. Changes in progress or turned down, masked to the last 4."""
	require_hr_role()
	rows = direct_deposit.changes(as_limit(args))
	waiting = [r for r in rows if r["status"] == "Pending" and not r["approved"]]
	return ToolResult(
		data={
			"count": len(rows),
			"changes": rows,
			"note": "Approve or reject in the Desk (Employee Bank Account). No tool approves a bank change.",
		},
		summary=f"{len(waiting)} direct-deposit change(s) waiting for approval",
	)
