# SPDX-License-Identifier: MIT
"""Seed update_document's whitelist with a starter set. Bench console; idempotent.

WHAT IT DOES. Appends (DocType, field) rows to ERPNext MCP Settings → Updatable
Fields for the registers that have a `create_*` tool and no `update_*` tool of
their own — the ones `update_document` exists to cover. Each new row is ticked.

IDEMPOTENT. A pair already on the table is left exactly as it is — including a
row an operator has UNTICKED, because unticking is a decision and a seed script
is not the place to overrule it. Run it twice and the second run adds nothing.

EVERY PAIR IS CHECKED AGAINST THIS SITE FIRST, with the same check
`manage_updatable_fields` uses (`generic_update._whitelist_refusal`): a DocType
that is not installed, a field this site's version does not have, a system
column, a Password or Table field — each is skipped and printed with its reason,
never written. So the list below can name ERPNext fields that differ between
versions without the script failing on the site that lacks one.

RUNNING IT. On the bench, as the frappe user:

    bench --site <site> console
    >>> %run apps/erpnext_mcp/scripts/seed_updatable_fields.py

or, without IPython magics, `exec(open("apps/erpnext_mcp/scripts/seed_updatable_fields.py").read())`.
In the Umbrel container: `sudo docker exec -it -u frappe fafo-erpnext_server_1
bench --site frontend console`, then the same line. The script commits.

Adding rows does not switch anything on: update_document still needs
`allow_update_document` ticked, and it still refuses submitted and cancelled
documents whatever this table says.

WHAT IS IN THE LIST, AND WHAT IS NOT. Descriptive fields an operator would fix
after a typo — names, notes, dates, locations, simple statuses. Deliberately
left out:

  - fields a dedicated workflow tool owns (Farm Task `state`, Training Session
    `status`, Spray Tank Mix approval, Housing Assignment end — each has its own
    tool that does more than set a value);
  - amounts, weights, rates and anything that feeds a posting or a payroll
    figure (a draft invoice's lines, a Scale Ticket's weights, deposits);
  - regulatory evidence beyond its free-text notes (Spray Application, Heat
    Exposure Event, Monitoring Record), and approval fields anywhere;
  - whole registers: I-9, Mobile User, Bank Account, Bank Transaction, IoT
    Reading, Incident Record, Verification Record, Owner Draw, Normalization
    Adjustment, Traceability Lot, Asset, Check Print Format — legal records,
    credentials, sensor data, or approval-gated money.
"""

STARTER = {
	# -- the three the release was asked for -------------------------------
	"Training Session": (
		"session_date",
		"start_time",
		"end_time",
		"duration_minutes",
		"location",
		"instructor_name",
		"provider",
		"training_source",
		"delivery_method",
		"expires_date",
		"notes",
	),
	"Warehouse": (
		"warehouse_name",
		"warehouse_type",
		"disabled",
		"city",
		"address_line_1",
		"address_line_2",
		"state",
		"pin",
		"phone_no",
	),
	"ToDo": ("status", "description", "date", "priority"),
	# -- farm registers ----------------------------------------------------
	"Farm Task": ("task_name", "urgency", "notes", "estimated_duration_minutes", "skill_required"),
	"Planting Season": (
		"variety",
		"rootstock",
		"acres",
		"trees_planted",
		"spacing_in_row_ft",
		"spacing_between_rows_ft",
		"expected_yield_per_acre",
		"yield_uom",
		"productive_from",
		"productive_through",
		"notes",
	),
	"Crop Observation": ("crop_stage", "growth_stage_code", "notes"),
	"Scale Ticket": ("variety", "grade", "block", "truck_id", "driver", "destination", "notes"),
	"Spray Nozzle Config": (
		"nozzle_name",
		"manufacturer",
		"orifice_color",
		"disabled",
		"rated_pressure_psi",
		"spacing_inches",
		"nozzles_active",
		"boom_width_ft",
		"notes",
	),
	"Spray Tank Mix": ("mix_name", "crop", "target_pest", "season_year", "notes"),
	"Spray Application": ("notes",),
	"Housing Assignment": ("notes",),
	"Heat Exposure Event": ("notes",),
	"Monitoring Record": ("observation_notes", "notes"),
	"Settlement Statement": ("notes",),
	# -- platform and planning ---------------------------------------------
	"Backup Record": ("location", "offsite", "size_mb", "retention_days", "test_restore_notes", "notes"),
	"Change Management Log": ("title", "description", "risk_level", "rollback_plan", "tested", "notes"),
	"Breakeven Analysis": ("analysis_name", "usda_commodity", "usda_variety", "usda_market", "notes"),
	"Activity Cost Pool": ("notes",),
	"Bank Categorization Rule": ("rule_name", "enabled", "priority", "notes"),
	"Trade Document Template": (
		"label_en",
		"label_es",
		"description_en",
		"description_es",
		"standard_reference",
		"enabled",
		"sequence",
		"notes",
	),
	"Note Payable": ("document_reference", "notes"),
	# -- ERPNext drafts: references and remarks only, never amounts ---------
	"Journal Entry": ("user_remark", "cheque_no", "cheque_date", "bill_no", "bill_date"),
	"Purchase Invoice": ("bill_no", "bill_date", "due_date"),
	"Purchase Order": ("schedule_date", "order_confirmation_no", "order_confirmation_date"),
	"Purchase Receipt": ("supplier_delivery_note",),
	"Sales Invoice": ("po_no", "po_date", "due_date"),
	"Payment Entry": ("reference_no", "reference_date", "remarks"),
	"Stock Entry": ("remarks",),
}


def starter_pairs() -> list[tuple]:
	return [(doctype, field) for doctype, fields in STARTER.items() for field in fields]


def seed(pairs=None, commit: bool = True) -> dict:
	"""Append each pair not already on the table. Returns what happened, and prints it."""
	import frappe

	from erpnext_mcp import settings
	from erpnext_mcp.tools import generic_update

	if not frappe.db.exists("DocType", generic_update.WHITELIST_DOCTYPE):
		raise SystemExit(
			f"{generic_update.WHITELIST_DOCTYPE} is not installed on this site — run "
			"`bench --site <site> migrate` first."
		)

	doc = frappe.get_single(settings.SETTINGS_DOCTYPE)
	present = {generic_update._row_pair(row) for row in doc.get(generic_update.WHITELIST_FIELD) or []}

	added, existing, skipped = [], [], []
	for doctype, fieldname in pairs if pairs is not None else starter_pairs():
		if (doctype, fieldname) in present:
			existing.append((doctype, fieldname))
			continue
		reason, _info = generic_update._whitelist_refusal(doctype, fieldname)
		if reason:
			skipped.append((doctype, fieldname, reason))
			continue
		doc.append(
			generic_update.WHITELIST_FIELD,
			{"doctype_name": doctype, "field_name": fieldname, "enabled": 1},
		)
		present.add((doctype, fieldname))
		added.append((doctype, fieldname))

	if added:
		doc.save()
		if commit:
			frappe.db.commit()

	for doctype, fieldname in added:
		print(f"added    {doctype}.{fieldname}")
	for doctype, fieldname, reason in skipped:
		print(f"skipped  {doctype}.{fieldname} — {reason}")
	print(
		f"\n{len(added)} added, {len(existing)} already on the table (left as they were), "
		f"{len(skipped)} skipped as not writable on this site."
	)
	return {"added": added, "existing": existing, "skipped": skipped}


if __name__ == "__main__":  # pragma: no cover — runs inside `bench console`
	seed()
