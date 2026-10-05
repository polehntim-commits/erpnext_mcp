# SPDX-License-Identifier: MIT
"""The one tool that changes somebody else's schema, and the read that explains it.

`install_compliance_fields` runs the same installer `after_migrate` runs, on
demand, so an operator who has just installed farm_precision_ag does not have to
run a migration to get the spray columns — and so an AI operator can ask what
would happen before anything does. `compliance_fields.py` makes the argument for
why the fields belong on somebody else's doctype at all; this module is the door.

WHY IT IS A TOOL AT ALL, GIVEN THE HOOK ALREADY DOES IT. Two reasons, and the
second is the real one. Installing an app mid-season is common and running
`bench migrate` on a production site to pick up four columns is not something
anybody wants to do on a Tuesday in August. And `dry_run` answers a question the
hook cannot: *what is this going to do to my site, and how many of my existing
records will stop being saveable*. That second number — the backlog — is the most
useful output either path produces, and it needs a caller who can read it.

`get_compliance_field_map` is the read. It answers "what does this app think
compliance requires, and why" without touching anything, and it is generated from
the same table the installer walks — so the answer cannot drift from the
behaviour.
"""

from .. import compliance_fields
from ..args import as_bool
from ..result import ToolResult


def install_compliance_fields(args: dict) -> ToolResult:
	"""Add every missing compliance field to the doctypes that carry the work."""
	dry_run = as_bool(args, "dry_run", False)
	# `respect_switch=False`: the dispatcher has already checked
	# `allow_install_compliance_fields` and a second check here would report "off"
	# for a call that got through it.
	report = compliance_fields.install_compliance_fields(dry_run=dry_run, respect_switch=False)

	backlog = {}
	for target in report["targets"]:
		for fieldname, detail in (target.get("backlog") or {}).items():
			rows = detail.get("rows_missing_a_value")
			if rows:
				backlog[f"{target['doctype']}.{fieldname}"] = rows

	data = {
		**report,
		"created_count": len(report["created"]),
		"existing_count": len(report["existing"]),
		"backlog": backlog,
		"backlog_total": sum(backlog.values()),
		"note": (
			"THIS IS THE ONE PLACE erpnext_mcp EXTENDS A DOCTYPE IT DID NOT CREATE, and it is "
			"deliberate. Compliance woven into the operational record is defensible under audit; "
			"a shadow log filled in afterwards drifts from reality the first busy week of "
			"harvest. The cost is real and stated: uninstalling this app drops these columns and "
			"everything typed into them, which before_uninstall says by name. "
			"Idempotent — running it again creates nothing. "
			"Every field is a Custom Field, so the target app's own repository and migrations "
			"are untouched."
		),
	}
	if backlog:
		data["backlog_note"] = (
			f"{sum(backlog.values())} existing record(s) have no value for a field that is now "
			"REQUIRED. They stay readable — Frappe enforces `reqd` on save, not retroactively — "
			"but none of them can be re-saved until it is filled in, and none of them is "
			"evidence of a compliant operation. This number is the operation's compliance "
			"backlog stated in rows, and it is the most useful thing this call produces."
		)
	if report["skipped"]:
		data["skipped_note"] = (
			"A doctype that is not on this site is skipped by name rather than failing. Install "
			"the owning app and run this again — nothing else is needed, and nothing that was "
			"already added is disturbed."
		)

	return ToolResult(
		data=data,
		summary=(
			("dry run: would add " if dry_run else "added ")
			+ f"{len(report['created'])} compliance field(s); {len(report['existing'])} already "
			f"present, {len(report['skipped'])} doctype(s) not on this site"
			+ (f", {sum(backlog.values())} existing row(s) now non-compliant" if backlog else "")
		),
		docstatus_delta="" if dry_run or not report["created"] else "schema → schema (fields added)",
	)


def _ccf_context() -> list:
	from .. import ccf

	return ccf.context_map()


def get_compliance_field_map(args: dict) -> ToolResult:
	"""What this app believes compliance requires of an operational record, and why.

	Read-only, and generated from the same table the installer walks — so the
	answer here and the behaviour there cannot drift. Each field carries the
	framework that wants it, why that framework wants it, and — the part that
	matters for the woven-in claim — what breaks OPERATIONALLY if it is missing.
	A field whose operational answer is "nothing" would be a shadow field and
	should not be in the table.
	"""
	described = compliance_fields.describe()
	present, missing, absent = [], [], []
	for target in described["targets"]:
		installed = compliance_fields.doctype_present(target["doctype"])
		target["doctype_installed"] = installed
		if not installed:
			absent.append(target["doctype"])
			target["fields_present"] = []
			target["fields_missing"] = [field["fieldname"] for field in target["fields"]]
			continue
		here, gone = [], []
		for field in target["fields"]:
			if compliance_fields.field_present(target["doctype"], field["fieldname"]):
				here.append(field["fieldname"])
				present.append(f"{target['doctype']}.{field['fieldname']}")
			else:
				gone.append(field["fieldname"])
				missing.append(f"{target['doctype']}.{field['fieldname']}")
		target["fields_present"] = here
		target["fields_missing"] = gone

	return ToolResult(
		data={
			**described,
			"present_here": present,
			"missing_here": missing,
			# v0.233.0. Every value a CCF condition tree can read, by provider.
			"context": _ccf_context(),
			"doctypes_not_on_this_site": absent,
			"switch": f"allow_{compliance_fields.SWITCH}",
			"the_test": (
				"Does removing a field break OPERATIONS, or only break COMPLIANCE REPORTING? "
				"Breaks operations too → compliance is woven in correctly. Only breaks reporting "
				"→ it is a shadow layer and belongs somewhere else. Every field below carries "
				"its answer under `breaks_operationally`, and a field that could not answer "
				"would not be in this table."
			),
			"note": (
				f"{len(present)} of {described['field_count']} field(s) are on this site. "
				+ (f"{len(missing)} are missing — run install_compliance_fields. " if missing else "")
				+ (
					f"{len(absent)} target doctype(s) are not installed here at all, which is not "
					"a fault: a site without farm_precision_ag has no spray records to make "
					"compliant."
					if absent
					else ""
				)
			).strip(),
		},
		summary=(
			f"{described['field_count']} compliance field(s) across "
			f"{len(described['targets'])} doctype(s), {described['required_field_count']} required; "
			f"{len(present)} present here, {len(missing)} missing"
		),
	)
