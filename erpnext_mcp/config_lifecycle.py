# SPDX-License-Identifier: MIT
"""The shared config lifecycle's publish policy. v0.234.1.

docs/design/config_lifecycle_and_tool_consolidation.md §6, and Tim's decision 5
(2026-10-04): **a version an AI proposed is published only in the Desk or on the
phone, for every kind.** Over MCP it may be drafted, previewed and staged; it may not
go live. A version an operator wrote in the Desk may still be published over MCP
where that kind allowed it before — wherever publishing was human-only, it stays so.

`refuse_ai_publish` is called by every MCP publish path (the generic `publish_config`
and the specific tools it routes to). The Desk buttons call the same logic inside
`desk_action()`, which is how a person's click is told apart from a model's call.
"""

from __future__ import annotations

import contextlib
import contextvars

import frappe

from .errors import ToolError

AI = "AI-proposed"
#: Who may publish or approve in the Desk.
DESK_PUBLISHERS = ("System Manager", "Farm Manager")

_DESK: contextvars.ContextVar = contextvars.ContextVar("erpnext_mcp_desk_action", default=False)


@contextlib.contextmanager
def desk_action():
	token = _DESK.set(True)
	try:
		yield
	finally:
		_DESK.reset(token)


def in_desk() -> bool:
	return bool(_DESK.get())


def refuse_ai_publish(authored_by, what: str) -> None:
	"""Raise unless this is a person in the Desk, or the version is not AI-proposed."""
	if in_desk() or str(authored_by or "").strip() != AI:
		return
	raise ToolError(
		f"{what} is AI-proposed, and an AI-proposed version is published only in the Desk or on the phone "
		"(Tim's decision 5, 2026-10-04). It has been drafted and can be previewed; open it in the Desk and "
		"press Publish (or Approve) there. Nothing was published."
	)


def refuse_payroll_publish(what: str) -> None:
	"""Payroll settings are published by a person in the Desk, whoever drafted them (§4)."""
	if in_desk():
		return
	raise ToolError(
		f"{what} is published only in the Desk by a person (HR Manager or System Manager) — payroll kinds are "
		"never published over MCP, whoever drafted them. preview_config shows the pay difference first. "
		"Nothing was published."
	)


def _require_publisher() -> str:
	user = frappe.session.user
	if not set(frappe.get_roles(user)).intersection(DESK_PUBLISHERS):
		frappe.throw(f"Publishing is for {' or '.join(DESK_PUBLISHERS)}.", frappe.PermissionError)
	return user


@frappe.whitelist(methods=["POST"])
def publish_config_version(name: str, change_note: str = "") -> dict:
	"""Desk button on Farm Config Version: publish this version as the person clicking."""
	from . import phone_config
	from .tools import phone_configs

	_require_publisher()
	doc = frappe.get_doc(phone_config.DOCTYPE, name)
	note = str(change_note or "").strip() or f"Published in the Desk by {frappe.session.user}."
	with desk_action():
		result = phone_configs.publish_phone_config(
			{
				"kind": phone_config.KINDS[doc.config_kind],
				"key": doc.config_key,
				"version": doc.version,
				"change_note": note,
			}
		)
	return result.data


@frappe.whitelist(methods=["POST"])
def approve_rule(name: str, accept_ai_authored_code: int = 0, accept_loop_gap: int = 0) -> dict:
	"""Desk button on Compliance Rule: approve (turn on) this rule as the person clicking."""
	from .tools import rules

	_require_publisher()
	with desk_action():
		result = rules.approve_compliance_rule(
			{
				"name": name,
				"accept_ai_authored_code": bool(int(accept_ai_authored_code or 0)),
				"accept_loop_gap": bool(int(accept_loop_gap or 0)),
			}
		)
	return result.data
