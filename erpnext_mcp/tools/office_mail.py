# SPDX-License-Identifier: MIT
"""office@ — incoming status, the sync fix, triage, the review queue. v0.221.0.

v0.222.0: drafts, review and send — only ever on a named person's approval.

docs/design/office_reply_drafts.md. The writes ship OFF like every mutating
tool. Inbound mail is untrusted: a message's text comes back marked as such, and
nothing here acts on what it says.
"""

from __future__ import annotations

from .. import mail_drafts, office_mail
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


# ── v0.222.0: drafts, review and send (docs/design/office_reply_drafts.md §2–§5) ──
def _refused(exc) -> ToolError:
	return ToolError(str(exc))


def list_mail_needing_drafts(args: dict) -> ToolResult:
	"""Read-only. Triaged office mail that may be drafted (never flagged mail)."""
	rows = mail_drafts.needing_drafts(as_limit(args))
	return ToolResult(data={"count": len(rows), "rows": rows}, summary=f"{len(rows)} email(s) to draft")


def get_mail_context(args: dict) -> ToolResult:
	"""Read-only. What a drafter may use: the rules, the message (untrusted), the facts, the style."""
	try:
		data = mail_drafts.context(as_str(args, "name", required=True))
	except mail_drafts.Refused as exc:
		raise _refused(exc) from None
	return ToolResult(data=data, summary=f"context for {data['name']}")


def save_mail_draft(args: dict) -> ToolResult:
	"""Store a reply draft. It is never sent from here: a person approves it, or nothing happens."""
	try:
		data = mail_drafts.save_draft(
			as_str(args, "name", required=True),
			as_str(args, "text", required=True),
			model=as_str(args, "model"),
			proposed_attachments=args.get("proposed_attachments") or [],
		)
	except mail_drafts.Refused as exc:
		raise _refused(exc) from None
	return ToolResult(data=data, summary=f"draft saved on {data['name']}; waiting for a person")


def _person() -> str:
	from .. import security

	return security.caller_identity()


def update_mail_draft(args: dict) -> ToolResult:
	"""A person edits the draft, the class or the link."""
	try:
		data = mail_drafts.update_draft(
			as_str(args, "name", required=True),
			_person(),
			text=args.get("text"),
			mail_class=as_str(args, "mail_class") or None,
			linked_doctype=args.get("linked_doctype"),
			linked_name=args.get("linked_name"),
		)
	except mail_drafts.Refused as exc:
		raise _refused(exc) from None
	return ToolResult(data=data, summary=f"{data['name']} updated")


def approve_mail_draft(args: dict) -> ToolResult:
	"""THE send: a named person approves; every check of mail_drafts.approve applies."""
	try:
		data = mail_drafts.approve(
			as_str(args, "name", required=True),
			_person(),
			"mcp",
			text=args.get("text"),
			attachments=args.get("attachments") or [],
			confirm_financial_details=as_bool(args, "confirm_financial_details", False),
		)
	except mail_drafts.Refused as exc:
		raise _refused(exc) from None
	return ToolResult(data=data, summary=f"{data['name']} approved by {data['approved_by']} and sent")


def discard_mail_draft(args: dict) -> ToolResult:
	try:
		data = mail_drafts.discard(as_str(args, "name", required=True), _person(), as_str(args, "reason"))
	except mail_drafts.Refused as exc:
		raise _refused(exc) from None
	return ToolResult(data=data, summary=f"{data['name']} discarded")


def redraft_mail(args: dict) -> ToolResult:
	try:
		data = mail_drafts.redraft(as_str(args, "name", required=True))
	except mail_drafts.Refused as exc:
		raise _refused(exc) from None
	return ToolResult(data=data, summary=f"{data['name']} back to Triaged for a new draft")
