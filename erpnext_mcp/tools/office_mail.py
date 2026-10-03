# SPDX-License-Identifier: MIT
"""office@ — incoming status, the sync fix, triage, the review queue. v0.221.0.

docs/design/office_reply_drafts.md. The writes ship OFF like every mutating
tool. Inbound mail is untrusted: a message's text comes back marked as such, and
nothing here acts on what it says.
"""

from __future__ import annotations

from .. import office_mail
from ..args import as_bool, as_limit, as_str
from ..errors import ToolError
from ..result import ToolResult


def get_mail_status(args: dict) -> ToolResult:
	"""Read-only. Is office@ mail arriving, and if not, why."""
	data = office_mail.incoming_status()
	return ToolResult(data=data, summary=data["summary"][:300])


def fix_incoming_mail_sync(args: dict) -> ToolResult:
	"""Sync rule ALL, incoming on, failure counters cleared. Dry run unless told otherwise."""
	account = as_str(args, "account", required=True)
	try:
		answer = office_mail.fix_sync(
			account,
			dry_run=as_bool(args, "dry_run", True),
			accept_initial_import=as_bool(args, "accept_initial_import", False),
		)
	except ValueError as exc:
		raise ToolError(f"{exc}. Nothing was changed.") from None
	if answer.get("refused"):
		raise ToolError(answer["refused"])
	verb = "would set" if answer["dry_run"] else "set"
	return ToolResult(data=answer, summary=f"{verb} {account} to sync ALL with incoming on")


def triage_mail(args: dict) -> ToolResult:
	"""Triage new office mail now (or re-triage one email). Writes only Office Mail rows."""
	communication = as_str(args, "communication")
	if communication:
		row = office_mail.triage_one(communication, overwrite=as_bool(args, "overwrite", False))
		if row is None:
			raise ToolError(
				f"{communication} was not triaged: it does not exist, it was triaged already (pass "
				"overwrite=true), or its reply was approved or sent. Nothing was changed."
			)
		return ToolResult(data=row, summary=f"{communication}: {row['mail_class']}, {row['state']}")
	rows = office_mail.pending_communications(as_limit(args))
	out = [office_mail.triage_one(r["name"]) for r in rows]
	out = [r for r in out if r]
	return ToolResult(data={"triaged": len(out), "rows": out}, summary=f"{len(out)} email(s) triaged")


def list_mail_drafts(args: dict) -> ToolResult:
	"""Read-only. The office mail queue by state and class."""
	rows = office_mail.rows(as_str(args, "state"), as_str(args, "mail_class"), as_limit(args))
	return ToolResult(data={"count": len(rows), "rows": rows}, summary=f"{len(rows)} office email(s)")


def get_mail_draft(args: dict) -> ToolResult:
	"""Read-only. One office email: message (untrusted), flags, link, draft, audit."""
	name = as_str(args, "name", required=True)
	row = office_mail.one(name)
	if row is None:
		raise ToolError(f"no Office Mail named {name!r}")
	return ToolResult(data=row, summary=f"{row.get('subject') or name}: {row.get('state')}")
