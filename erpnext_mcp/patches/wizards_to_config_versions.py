# SPDX-License-Identifier: MIT
"""Every Wizard Definition becomes version 1 Published of its key. v0.207.0.

docs/design/phone_config_and_compliance_loop.md §8. Each legacy wizard is
converted to form_schema v2 steps (`form_schema.from_wizard_fields`) and seeded
create-only as a Farm Config Version, so from now on it is versioned, previewed
and rolled back like any other. The legacy row stays as the fallback. A wizard
that was disabled arrives Retired. Idempotent.
"""

from __future__ import annotations

from erpnext_mcp import wizard_config


def execute() -> None:
	made = wizard_config.seed_from_legacy()
	if made:
		print(f"erpnext_mcp: converted wizard(s) {', '.join(made)} to Farm Config Versions")
