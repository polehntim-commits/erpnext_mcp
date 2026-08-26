# SPDX-License-Identifier: MIT
"""v0.138.0. Stop the two I-9 signature rules firing on phone-built forms.

THE SEEDER CANNOT DO THIS, and it is the same reason `migrate_declarative_rules`
exists: `seed_compliance_rules` checks for the `rule_id` and leaves anything it
finds alone, which is the property that stops an operator's edit being corrected
back on every upgrade. Every site that installed before this release therefore
already has `i9_section_1_unsigned` and `i9_section_2_unsigned` rows carrying the
old scope, and editing `declarative_seed_specs` alone would change what a FRESH
install gets and nothing else.

WHAT IT CHANGES. One filter is appended to each rule: the `any` group
`compliance_rules.i9_attestation_group()` builds, which passes a row when at
least one of `signed_pdf`, `section_1_signed_at` and `section_2_signed_at` is
empty. A form carrying all three has been attested by the handset — the sealed
retained page is on the record and the employer's app has stated when each
section was signed — and `tools/i9.unsigned_boxes` reads the same three columns
to decide the same question. Before this the two disagreed, and the disagreement
was not academic: every I-9 the app has built since the signing moved onto the
phone was signed, sealed and retained, rested at `Awaiting Verification` for
ever, and carried two Criticals saying nobody had signed it.

IT APPENDS RATHER THAN REPLACES, for the reason `_merged_filters` gives in
`migrate_declarative_rules`: anything else in that column was put there by an
operator narrowing the rule for their own operation, and dropping it would widen
a rule somebody deliberately narrowed. A rule that already carries the group —
because the site installed at v0.138.0, or because this patch has run — is left
exactly as it is.

IT EDITS THE LIVE ROW RATHER THAN SUPERSEDING IT, which is the opposite of what
`update_compliance_rule` does, and the difference is worth stating. Superseding
exists so an alert raised last April can still be read against the definition
that raised it. The alerts this narrowing suppresses are alerts that were WRONG
when they were raised — they assert that a signed, sealed, retained federal form
is unsigned — and preserving a definition so those can be re-derived faithfully
preserves nothing anybody wants. Nothing else on the row moves: the severity,
the citation, the producer recipe, the approval and the switch are untouched, and
what the rule says about a genuinely unsigned form is unchanged.

NEVER RAISES. It runs inside `bench migrate`, where an exception aborts the
migration for the whole bench. A rule it could not widen is named on the console
and keeps the old scope — which raises too much rather than too little, and is
the direction a compliance rule should fail in.
"""

import json

import frappe

from .. import compat, compliance_rules

#: The two rules the phone attestation test spares, and nothing else. Named
#: explicitly rather than derived, for the reason `migrate_declarative_rules`
#: gives: a patch that silently widens its own scope on the next upgrade is a
#: patch nobody can review once. `i9_supplement_b_unsigned` is deliberately
#: absent — a Supplement B is signed in a child row that no sealed Section 1/2
#: page says anything about.
WIDENED = ("i9_section_1_unsigned", "i9_section_2_unsigned")


def execute() -> None:
	for line in report_lines(widen_i9_attestation_filters()):
		print(f"erpnext_mcp: {line}")


def widen_i9_attestation_filters() -> dict:
	"""Append the phone-attestation group to each rule that has not got it."""
	report = {"widened": [], "already": [], "absent": [], "failed": []}
	if not compat.doctype_exists(compliance_rules.DOCTYPE):
		return report

	try:
		group = compliance_rules.i9_attestation_group()
	except Exception as exc:  # pragma: no cover - a partial import during a migrate
		report["failed"].append({"name": "i9_attestation_group", "reason": f"{type(exc).__name__}: {exc}"})
		return report

	for rule_id in WIDENED:
		try:
			report[_widen_one(rule_id, group)].append(rule_id)
		except Exception as exc:  # pragma: no cover - reported, never raised
			report["failed"].append({"name": rule_id, "reason": f"{type(exc).__name__}: {exc}"})
	return report


def _widen_one(rule_id: str, group: dict) -> str:
	name = compliance_rules.resolve(rule_id)
	if not name:
		return "absent"
	row = compliance_rules.rule_row(name)
	try:
		filters = compliance_rules.parse_filters(row.get("scope_filters_json"))
	except ValueError:
		# A column somebody hand-edited into something the parser refuses. Adding
		# to it would need this patch to guess what they meant, and the rule is
		# already running unscoped — which is loud, not quiet. Left alone.
		return "absent"
	if _carries(filters, group):
		return "already"
	frappe.db.set_value(
		compliance_rules.DOCTYPE,
		name,
		"scope_filters_json",
		json.dumps([*filters, group]),
		update_modified=False,
	)
	return "widened"


def _carries(filters: list, group: dict) -> bool:
	"""Is the phone-attestation group already on this rule?

	COMPARED ON THE COLUMNS IT READS, not on the whole dict. An operator who
	reordered the group's members, or a future release that adds a fourth column
	to it, must not make this patch append a second, near-identical group — two
	ANDed groups asking overlapping questions is a scope nobody can read.
	"""
	wanted = {str(entry.get("field") or "") for entry in group.get("value") or []}
	for entry in filters:
		if str(entry.get("op") or "") != group.get("op"):
			continue
		if {str(member.get("field") or "") for member in entry.get("value") or []} & wanted:
			return True
	return False


def report_lines(report: dict) -> list:
	"""What the console says. One line per outcome that happened, and no line for
	the ones that did not — a migrate that prints reassuring nothings trains
	people to scroll past the line that mattered."""
	lines = []
	if report["widened"]:
		lines.append(
			f"narrowed {len(report['widened'])} I-9 signature rule(s) so they no longer fire on a "
			f"form the handset attested: {', '.join(sorted(report['widened']))}. A sealed signed "
			"copy on the record WITH both section_N_signed_at moments beside it now counts as the "
			"attestation, the same test tools/i9.unsigned_boxes uses to decide whether the form is "
			"Complete. A genuinely unsigned I-9 still raises exactly what it did. Existing alerts "
			"on attested forms are dismissed by the next sweep, which stops observing them."
		)
	for entry in report["failed"]:
		lines.append(
			f"could not widen compliance rule {entry['name']} — {entry['reason']}. It keeps the "
			"old scope, so it may raise a Critical on a phone-built I-9 that is signed, sealed "
			"and retained. update_compliance_rule can add the filter by hand."
		)
	return lines
