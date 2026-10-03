# SPDX-License-Identifier: MIT
"""Every MCP tool switch, its danger tier, and whether it is on. v0.217.0.

docs/design/security_status_and_alerts.md §1. The tier is DERIVED — from the
registry's own `mutating` and `destructive` hints and from the tool's name —
so a tool added next month is tiered without anybody remembering a list.
"""

from __future__ import annotations

import json

from . import settings

READ, WRITE, CREDENTIAL, FINANCIAL, DESTRUCTIVE = "read", "write", "credential", "financial", "destructive"
TIERS = (READ, WRITE, CREDENTIAL, FINANCIAL, DESTRUCTIVE)
DANGEROUS = (CREDENTIAL, FINANCIAL, DESTRUCTIVE)

CREDENTIAL_TOOLS = frozenset(
	{
		"generate_api_token",
		"generate_mobile_login_qr",
		"create_mobile_user",
		"recover_mobile_access",
		"open_device_enrollment",
	}
)
CREDENTIAL_PREFIXES = ("issue_",)
FINANCIAL_TOOLS = frozenset(
	{
		"submit_payroll",
		"generate_nacha_file",
		"generate_prenote_file",
		"receive_payment",
		"record_loan_payment",
		"create_payment_entry",
		"convey_parcel",
	}
)
FINANCIAL_PREFIXES = ("submit_", "post_", "bulk_submit_", "cancel_", "close_", "reopen_", "run_payroll")
DESTRUCTIVE_PREFIXES = ("delete_", "destroy_", "revoke_")


def tier(name: str, spec: dict) -> str:
	if not spec.get("mutating"):
		return READ
	if name in CREDENTIAL_TOOLS or name.startswith(CREDENTIAL_PREFIXES):
		return CREDENTIAL
	if name in FINANCIAL_TOOLS or name.startswith(FINANCIAL_PREFIXES):
		return FINANCIAL
	if (spec.get("annotations") or {}).get("destructiveHint") or name.startswith(DESTRUCTIVE_PREFIXES):
		return DESTRUCTIVE
	return WRITE


def expiries() -> dict:
	"""{tool: expires_at} for switches turned on for a limited time (§5)."""
	try:
		raw = settings._value("switch_expiry")
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
		return value if isinstance(value, dict) else {}
	except Exception:  # pragma: no cover
		return {}


def catalogue() -> list:
	"""One row per tool switch."""
	from . import registry

	timed = expiries()
	rows = []
	for name, spec in registry.TOOLS.items():
		rows.append(
			{
				"tool": name,
				"enabled": settings.tool_enabled(name),
				"default": bool(not spec.get("mutating") or name in registry.DEFAULT_ON_MUTATING_TOOLS),
				"tier": tier(name, spec),
				"available": registry.is_available(name),
				"expires_at": timed.get(name),
			}
		)
	return rows


def summary(rows: list) -> dict:
	counts = {key: {"total": 0, "enabled": 0} for key in TIERS}
	for row in rows:
		counts[row["tier"]]["total"] += 1
		counts[row["tier"]]["enabled"] += 1 if row["enabled"] else 0
	return {
		"by_tier": counts,
		"dangerous_enabled": sorted(
			row["tool"] for row in rows if row["enabled"] and row["tier"] in DANGEROUS
		),
		"timeboxed": sorted(row["tool"] for row in rows if row["expires_at"]),
	}
