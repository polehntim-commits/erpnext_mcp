# SPDX-License-Identifier: MIT
"""Every ID and asset print draws the CR80 card. v0.213.0.

`card_redirect` moved the four legacy print buttons to the approved card on the
server — "Print Badge Sheet" and "ID Card" on Employee, "Generate QR Sheet" and
"QR Tag" on the Asset Register — so they need nothing here: the Client Script
rows are untouched and print the new card from the first request after deploy.

What a deploy cannot reach is DATA: the "Employee Badge Card" print format on a
badge row is a record, seeded once and never overwritten. This patch repoints it
to the CR80 card where it is still exactly as shipped, leaves an edited one
alone and says which happened, and makes sure both CR80 formats exist and are
their doctypes' defaults where none is set. Runs once.
"""

from __future__ import annotations

from erpnext_mcp import card_print_format, card_redirect


def execute() -> None:
	report = run()
	legacy = report["badge_format"]
	if legacy["changed"]:
		print(f"erpnext_mcp: print format '{legacy['format']}' now draws the CR80 ID card")
	elif legacy["reason"]:
		print(f"erpnext_mcp: print format '{legacy['format']}' was not changed — {legacy['reason']}")
	print(
		"erpnext_mcp: Print Badge Sheet, ID Card, Generate QR Sheet and QR Tag now print the CR80 "
		"card. Set the Farm Feature Flag `legacy_badge_layouts` to get the old layouts back."
	)


def run() -> dict:
	return {
		"badge_format": card_redirect.repoint_badge_print_format(),
		"card_formats": card_print_format.seed_card_print_formats(),
	}
