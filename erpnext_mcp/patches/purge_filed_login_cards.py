# SPDX-License-Identifier: MIT
"""Filed login cards are deleted; grants link their Employee. v0.223.0.

docs/design/employee_file.md §1. Every login card already filed on a Governance
Document that has expired, whose phone has signed in, or that a newer card
superseded more than a card's life ago is deleted (it is a credential image,
not a record). Every Mobile Access Grant gets its `employee` from
Employee.user_id. Runs once.
"""

from __future__ import annotations

from erpnext_mcp import login_cards


def execute() -> None:
	purged = login_cards.purge_due()
	linked = login_cards.backfill_grant_employees()
	if purged or linked:
		print(
			f"erpnext_mcp: deleted {len(purged)} filed login card(s); linked {linked} mobile grant(s) to "
			"their Employee."
		)
