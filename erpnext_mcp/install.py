# SPDX-License-Identifier: MIT
"""Install / migrate / uninstall hooks.

Eleven jobs. The second arrived in v0.12.0, the third and fourth in v0.15.0,
the fifth — the Farm Task Dispatch Kanban board — in v0.16.0, the sixth —
the mobile roles — in v0.17.0, the seventh — the compliance vocabulary
and the training curricula — in v0.19.2, the eighth — the Weather Settings
defaults — in v0.19.4, and the ninth — the Sustainable CF/Acre dashboard charts
— in v0.19.5, which v0.19.6 turned into two charts rather than a tenth job. The
tenth — signing the completions filed before v0.20.1 — arrived in v0.20.1, and
the eleventh — the four seeded Inspection Templates — in v0.21.0.

The first is making the DocType JSON's declared defaults *true in the
database*. A Frappe Single stores a row per field that has been set, so straight
after `bench install-app` the settings document has no rows at all and every
field reads as None — which for the read-tool switches (default ON) would look
like "everything is disabled". `settings.seed_defaults()` writes them out.

It runs on install and after every migrate, so a version that adds a tool gets
its switch seeded without a bespoke patch. It only ever fills in fields with no
stored value, so it cannot undo an operator's decision — including a deliberate
"off".

The second is registering the two custom Party Types — `Family` and `Contact`.
Those are not settings; they are records a Journal Entry line links to, and a
site without them cannot book a payment to a family member at all. Seeding them
here rather than in a one-shot patch means a site upgrading from any earlier
version gets them on its next migrate, and re-running is a no-op because the
seeder checks before it inserts.

Registering a Party Type changes nothing that already exists. Rules and entries
using Shareholder, Employee or Supplier keep working exactly as they did; this
adds options, it does not reclassify anything.

The third is the v0.15.0 compliance fields, and it is the one that needs a
sentence of defence. It adds Custom Fields to Spray Log, to Employee, to the
BucketLog bridge and — from v0.19.3 — to Attendance: four doctypes this app did
not create, which is the exact thing `hooks.py` promises it does not do. `compliance_fields.py` argues the case
at length; the short version is that compliance woven into the operational record
is defensible under audit and a shadow log beside it is not, and you cannot weave
anything into a doctype you refuse to touch. It is the only such exception in the
app, it is behind a switch, and `before_uninstall` names the cost.

The fourth is the Compliance Command Center: a Dashboard, its Charts and its
Number Cards, built idempotently on every migrate, checking before it writes.
Deliberately NOT shipped as `fixtures`, which `test_hooks.py` forbids by name: a
fixture is imported by `bench migrate` with no ability to skip what a site
already has, and an operator who rearranged their dashboard would get it
silently rearranged back.

The fifth arrived in v0.16.0 and is the Farm Task Dispatch Kanban board, plus the
workspace that lands somebody on it. Built exactly like the fourth and for
exactly the same reasons — an existing board is left alone, including every
column somebody has since reordered or deleted.

The sixth arrived in v0.17.0: the mobile roles — Field Worker, Foreman,
Compliance Officer, Farm Manager, Family Member, Advisor, and Crew Leader since
v0.68.1 — and their Custom DocPerm rows. It is the second job here that touches something outside this app's
own records, and it is the one with the sharpest edge, which `roles.py` spends
forty lines on: **the moment any Custom DocPerm row exists for a doctype, Frappe
ignores every STANDARD permission on that doctype, for every role on the site.**
So the installer mirrors the standard perms into custom ones first, and refuses
outright to write a permission onto a doctype this app does not own. A role
nobody holds changes nothing; a Custom DocPerm on somebody else's doctype could
have taken HR Manager off Employee during a migration with nothing printed.

The seventh arrived in v0.19.2 and is two seeders and a migration: the ten
`Compliance Regime` rows, the ten common `Training Type` curricula, and the
one-time conversion of `Employee Training Record.training_type` from free text
into links to those curricula.

IT IS NOT A FRAPPE `fixtures` ENTRY, AND THAT IS DELIBERATE — `test_hooks.py`
forbids the word by name, for the reason the fourth job spells out: a fixture is
imported by `bench migrate` with no ability to skip what a site already has, so
an operator who corrected the regimes on a curriculum would get them silently
corrected back on the next upgrade. Both seeders check before they write and
leave an edited row exactly as it is, including one somebody deactivated. The
regime list is seeded FROM `training.REGIMES`, which stays the single definition
of what a regime is; the table exists so the Desk can offer a picker instead of a
text box, and a picker is what stops somebody typing `OSHA` where the vocabulary
says `OR-OSHA`.

The eighth arrived in v0.19.4 and is the first job's problem on a second Single.
`Weather Settings` ships an HTTP timeout, a cache lifetime and three compliance
thresholds as declared defaults, and until somebody saves the form none of them
has a row in `tabSingles` — so a timeout reads None, becomes zero, and fails every
connection immediately with nothing in a log to say why. It is the same function
as the first job with a doctype argument, so it inherits the same contract: it
fills blanks and never overwrites a choice, including a deliberate "off".

None of the eight raises. Every one of them runs inside `bench migrate`, where an
exception aborts the migration for the whole bench — so a failure here is
reported and the next job still runs. That is not defensive padding: v0.12.0
shipped an `after_migrate` that died on a link validation and left operators with
a traceback instead of an app.

What install does NOT do: generate a token, or set `enabled`. A freshly
installed app must be inert. Turning it on is a decision an operator makes on
the settings form, and there is no code path that makes it for them.
"""

import frappe

from . import (
	asset_tag_form_action,
	asset_tag_list_action,
	badge_form_action,
	badge_list_action,
	badge_print_format,
	compliance_fields,
	dashboard,
	i9_documents,
	i9_print_format,
	onboard_worker,
	roles,
	settings,
	state_withholding,
	training,
	withholding,
)
from .patches import backfill_completion_signatures, migrate_training_types
from .tools import badges, company


def after_install() -> None:
	settings.seed_defaults()
	_weather_settings()
	_i9_settings()
	_fica_settings()
	company.ensure_party_types()
	_compliance_fields()
	_command_center()
	_dispatch_board()
	_mobile_roles()
	_compliance_vocabulary()
	_kpi_charts()
	_completion_signatures()
	_inspection_templates()
	_compliance_rules()
	_i9_document_types()
	_i9_print_format()
	_federal_tax_table()
	_state_tax_table()
	_kpi_definitions()
	_farm_task_templates()
	_badge_logo_field()
	_badge_print_format()
	_badge_list_action()
	_badge_form_action()
	_asset_tag_list_action()
	_asset_tag_form_action()
	_onboard_worker()
	_settlement_invoice_link()
	_bank_categorization_fields()
	_bank_pairing_fields()
	_receipt_intelligence_fields()
	_translations()
	_breakeven_account_fields()
	_sales_channel_field()
	_pest_provider_field()
	_agricultural_masters()
	_soil_compaction_profiles()
	_employment_types()
	_farm_designations()
	frappe.db.commit()


def after_migrate() -> None:
	settings.seed_defaults()
	_weather_settings()
	_i9_settings()
	_fica_settings()
	company.ensure_party_types()
	_compliance_fields()
	_command_center()
	_dispatch_board()
	_mobile_roles()
	_compliance_vocabulary()
	_kpi_charts()
	_completion_signatures()
	_inspection_templates()
	_compliance_rules()
	_i9_document_types()
	_i9_print_format()
	_federal_tax_table()
	_state_tax_table()
	_kpi_definitions()
	_farm_task_templates()
	_badge_logo_field()
	_badge_print_format()
	_badge_list_action()
	_badge_form_action()
	_asset_tag_list_action()
	_asset_tag_form_action()
	_onboard_worker()
	_settlement_invoice_link()
	_bank_categorization_fields()
	_bank_pairing_fields()
	_receipt_intelligence_fields()
	_wizard_definitions()
	_trade_documents()
	_reporting_templates()
	_translations()
	_breakeven_account_fields()
	_sales_channel_field()
	_pest_provider_field()
	_agricultural_masters()
	_soil_compaction_profiles()
	_employment_types()
	_farm_designations()


def _soil_compaction_profiles() -> None:
	"""Seed the soil book behind the compaction overlay. v0.116.0.

	EIGHT ROWS AND NO WIRING. The profiles exist after this runs and NOTHING
	POINTS AT THEM — every block is still coloured by the shipped 24/48 default
	until somebody says which soil it is, with `assign_soil_profile` or on the
	Field form. That is deliberate and is the same call `_farm_task_templates`
	makes about its own seeds: guessing a farm's soil from its county would
	produce a map full of confident colours drawn off nobody's measurement, and
	the whole promise of this layer is that a colour can be traced to a record.

	`list_soil_compaction_profiles` reports `blocks_without_profile`, which is
	the number that says how much of that job is left.
	"""
	try:
		from . import agronomy_seed

		report = agronomy_seed.seed_soil_profiles()
	except Exception as exc:  # pragma: no cover - the seeder swallows its own
		print(f"erpnext_mcp: the soil compaction profiles were not seeded — {type(exc).__name__}: {exc}")
		return
	if report.get("created"):
		print(
			f"erpnext_mcp: seeded {len(report['created'])} soil compaction profile(s) — how long "
			"the ground stays too wet to drive on after a set, by USDA textural class, which is "
			"what the irrigation/compaction map layer colours a zone by. THE HOURS ARE "
			"DRAINAGE-CLASS SHAPES AND NOT MEASUREMENTS: what is worth keeping from them is "
			"that a sand is driveable in eight hours and a clay in sixty, not the particular "
			"figures. NOTHING IS WIRED UP — every block is still on the shipped 24/48 default "
			"until you point it at a soil with assign_soil_profile or on the Field form, and "
			"list_soil_compaction_profiles reports how many are still on it."
		)
	for skipped in report.get("skipped") or ():
		print(f"erpnext_mcp: skipped {skipped.get('name')} — {skipped.get('reason')}")
	for failure in report.get("failed") or ():
		print(f"erpnext_mcp: could not seed {failure.get('name')} — {failure.get('reason')}")


def _pest_provider_field() -> None:
	"""Give Company the pest management consultants table. v0.97.0.

	The ninth place this app installs a Custom Field at migrate time, and a child
	table rather than a Link on purpose: a farm running pome fruit and stone fruit
	commonly retains a different consultant for each, and one Link holds whichever
	was typed last while reading as the whole answer.

	`tools/company.ensure_pest_provider_field` creates the same column lazily on
	first use. Doing it here means it exists before anybody needs it, which is what
	makes it visible on the Company form.

	Never raises: it runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench.
	"""
	try:
		if not frappe.db.exists("DocType", "Company"):
			return
		if company.ensure_pest_provider_field():
			return
		print(
			"erpnext_mcp: Company did not take the pest_management_providers Custom Field. "
			"list_companies reports the table as absent rather than reporting every company as "
			"having no consultant, which is a different claim. Nothing else is affected."
		)
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		print(
			"erpnext_mcp: the Company pest management provider table was not installed — "
			f"{type(exc).__name__}: {exc}"
		)


def _sales_channel_field() -> None:
	"""Give Customer the column that says which channel a buyer is. v0.97.0.

	The eighth place this app installs a Custom Field at migrate time, and the
	narrowest argument of the eight: "what percentage of each commodity do you
	direct-market" is a question a stock ERPNext cannot answer at all, because
	nothing on the site says whether a sale was direct. One Select on the record
	that knows — the customer — turns it into a query over Sales Invoice.

	`tools/masters.ensure_sales_channel_field` creates the same column lazily on
	first use, so a bench that pulled the code without running the installer
	classifies a customer the first time somebody says so. Doing it here means it
	exists before anybody needs it, which is what makes it visible on the Customer
	form and filterable in the Desk.

	Never raises: it runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench.
	"""
	try:
		from .tools import masters

		if not frappe.db.exists("DocType", "Customer"):
			return
		if masters.ensure_sales_channel_field():
			return
		print(
			"erpnext_mcp: Customer did not take the sales_channel Custom Field. Every customer "
			"tool still works and reports the column as absent — which is a different claim from "
			"reporting every customer as unclassified. The direct-marketed share stays answerable "
			"only from the typed Crop.pct_direct_marketed fallback."
		)
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		print(
			f"erpnext_mcp: the Customer sales channel field was not installed — {type(exc).__name__}: {exc}"
		)


def _agricultural_masters() -> None:
	"""Seed the starting book of crops, markets, units and conversions. v0.82.0.

	THE JOB THAT MAKES THE OTHER FEATURES ANSWERABLE. Until this runs, a spray
	check asking what a crop's pre-harvest interval is, a settlement asking what
	a bin weighed, and a breakeven asking what a market's grades pay all have to
	take the answer from whoever called them. After it, each of those is a row an
	operator can read and correct.

	IT ONLY EVER CREATES WHAT IS NOT THERE, checked by docname across every row
	rather than only the live ones — the same contract as `_inspection_templates`
	and `_compliance_rules`, and the same reason `test_hooks.py` forbids the word
	`fixtures` by name. An operator who replaced the nominal cherry bin weight
	with their own weighed figure keeps it. One who deleted a market they do not
	sell into does not get it back on the next migrate.

	IT CANNOT TAKE AN INSTALL DOWN. Every record is attempted alone; a failure is
	printed and stepped over. On a Frappe bench with no ERPNext there is no UOM
	master, so the units and the conversions that link to them are skipped BY
	NAME — while the crops and markets, which link to neither, are seeded anyway.
	Losing the unit register on such a bench is correct: there is nothing on it
	that could store a quantity in those units.
	"""
	try:
		from . import agronomy_seed

		report = agronomy_seed.seed_agricultural_masters()
	except Exception as exc:  # pragma: no cover - the seeder swallows its own
		print(f"erpnext_mcp: the agricultural master data was not seeded — {type(exc).__name__}: {exc}")
		return
	if report.get("created"):
		print(
			f"erpnext_mcp: seeded {len(report['created'])} agricultural master record(s) — three "
			"crops with their varieties and water demand by growth stage, three markets with "
			"their grade ladders, four unit contexts and the conversions between them. THE "
			"NUMBERS ARE A STARTING BOOK, NOT YOUR FARM'S: every conversion but the three "
			"definitions is Nominal, the yields are expectations and the grade premiums are "
			"illustrative shapes rather than this season's prices. list_crops and list_markets "
			"report what is still missing; replace a nominal factor with your own weighed figure "
			"using an Operation Average basis and it wins every lookup."
		)
	for skipped in report.get("skipped") or ():
		print(f"erpnext_mcp: skipped {skipped.get('name')} — {skipped.get('reason')}")
	for failure in report.get("failed") or ():
		print(f"erpnext_mcp: could not seed {failure.get('name')} — {failure.get('reason')}")


#: The employment types a tree-fruit operation hires under that a stock Frappe HR
#: does not ship. Its own list is Full-time, Part-time, Probation, Contract,
#: Commission, Piecework, Intern and Apprentice — an office's categories, and the
#: two a farm needs most are not on it. Deliberately SHORT: `Employment Type` is a
#: master an operator can add to in the Desk in ten seconds, and a seeder that
#: guessed at a dozen would leave a picker full of categories nobody hires under.
FARM_EMPLOYMENT_TYPES = (
	# The one the bug was about, and the majority of a farm's payroll. Stock HR
	# has no Hourly because ERPNext treats hourly as a SALARY MODE, and an
	# Employee's `employment_type` is the only column a roster, a wage statement
	# or an ACA hours count can read to say how somebody is engaged.
	"Hourly",
	# The fact an H-2A roster, an ACA hours count and a piece-rate wage statement
	# all turn on, which is what `create_employee`'s own schema note says about
	# this field. Frappe HR has no word for it.
	"Seasonal Worker",
)


def _employment_types() -> None:
	"""Seed the employment types a farm hires under, so `Employee.employment_type` can name one.

	THE FIELD IS A LINK, AND A LINK IS ONLY AS GOOD AS ITS MASTER. `create_employee`
	— and `onboard_employee` through it — checks `employment_type` against this
	site's own `Employment Type` records and refuses a value that names none,
	listing what there is. That refusal is right and stays; what was wrong is that
	this app required a master it never seeded, so `employment_type: "Hourly"` was
	refused on a stock Frappe HR and the majority of a farm's workforce could not
	be onboarded through the tool at all.

	IT ONLY EVER CREATES WHAT IS NOT THERE, by docname — the same contract as
	`_i9_document_types` and `_agricultural_masters`, and the reason `test_hooks.py`
	forbids the word `fixtures` by name. An operator who renamed Hourly, or deleted
	a category they do not hire under, keeps their decision through every later
	migrate. It adds options and reclassifies nothing: every Employee already
	pointing at Full-time still points at Full-time.

	SKIPPED WHOLE ON A BENCH WITH NO FRAPPE HR, where the doctype is absent — the
	same site on which `create_employee` does not check this Link at all, because
	there is no schema to check it against.
	"""
	try:
		if not frappe.db.exists("DocType", "Employment Type"):
			return
		created = []
		for name in FARM_EMPLOYMENT_TYPES:
			if frappe.db.exists("Employment Type", name):
				continue
			doc = frappe.get_doc({"doctype": "Employment Type", "employee_type_name": name})
			doc.flags.ignore_permissions = True
			doc.insert()
			created.append(name)
		if created:
			print(
				f"erpnext_mcp: seeded {len(created)} Employment Type record(s) — "
				f"{', '.join(created)}. They are OPTIONS on the Employee form, not a "
				"reclassification: nothing already hired changed category."
			)
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		print(f"erpnext_mcp: the employment types were not seeded — {type(exc).__name__}: {exc}")


#: v0.68.1. The farm job titles this app READS and never created.
#:
#: `Employee.designation` is the column `list_pending_threshold_acknowledgments`
#: filters on to find every checker on the site, and the column a Position Wage
#: Default keys a rate on. A stock Frappe HR ships an office's titles — Analyst,
#: Consultant, Engineer — and none of these, so the app filtered on a master
#: nothing ever seeded and correctly found nobody.
#:
#: DELIBERATELY THE FIVE `roles.JOB_TITLES` NAMES AND NOT ONE MORE. This tuple is
#: read FROM that table rather than restated beside it, because a title seeded
#: here with no row there is a title the mapping cannot explain, and a row there
#: with nothing seeded here is a mapping that names a designation the site does
#: not have. One list, two uses.
FARM_DESIGNATIONS = tuple(entry["designation"] for entry in roles.JOB_TITLES)


def _farm_designations() -> None:
	"""Seed the job titles this app reads, so `Employee.designation` can name one.

	SAME CONTRACT AS `_employment_types` ABOVE, for the same reason and with the
	same one-sentence promise: it only ever creates what is not there, by
	docname. An operator who renamed Checker, or deleted a title they do not
	hire, keeps that decision through every later migrate. It adds options and
	reclassifies nobody — every Employee already pointing at a title still points
	at it.

	IT IS NOT WHAT DECIDES WHAT SOMEBODY MAY DO. A designation is a job title and
	a role is a permission set; `roles.JOB_TITLES` is the mapping between them and
	`create_mobile_user` writes the role. Seeding a title grants nothing.

	SKIPPED WHOLE ON A BENCH WITH NO FRAPPE HR, where the doctype is absent.
	"""
	try:
		if not frappe.db.exists("DocType", "Designation"):
			return
		created = []
		for name in FARM_DESIGNATIONS:
			if frappe.db.exists("Designation", name):
				continue
			doc = frappe.get_doc({"doctype": "Designation", "designation_name": name})
			doc.flags.ignore_permissions = True
			doc.insert()
			created.append(name)
		if created:
			print(
				f"erpnext_mcp: seeded {len(created)} Designation record(s) — "
				f"{', '.join(created)}. They are OPTIONS on the Employee form and grant "
				"nothing: what somebody may do is their mobile ROLE, and list_mobile_users "
				"returns the mapping between the two."
			)
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		print(f"erpnext_mcp: the farm designations were not seeded — {type(exc).__name__}: {exc}")


def _translations() -> None:
	"""Seed the shipped English and Spanish strings. v0.85.0.

	THE NON-OVERWRITE RULE HAS TWO HALVES AND BOTH MATTER, which is why this
	seeder is not shaped like `_wizard_definitions` above. A wizard is written
	only where none exists, full stop; a translation is written where none
	exists AND refreshed where one exists that nobody has edited. The difference
	is that a shipped MISTRANSLATION is a defect this app has to be able to fix
	on every site — and a seeder that never touched an existing row would make a
	bad Spanish string permanent everywhere it had ever landed.

	What protects an operator's own wording is `operator_edited`, set by
	`update_translation` and never cleared. A row carrying it is left alone
	forever. A farm whose crew kept misreading a shipped phrase reworded it, and
	putting the shipped wording back every upgrade would make the whole register
	decorative.

	Runs on install AND after every migrate, so a key ADDED in a later release
	reaches sites that already have the rest of the catalogue.

	Never raises: it runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench.
	"""
	try:
		from .tools import translations

		report = translations.install_translations()
	except Exception as exc:  # pragma: no cover - the seeder swallows its own
		print(f"erpnext_mcp: the translations were not seeded — {type(exc).__name__}: {exc}")
		return
	if report.get("created"):
		print(
			f"erpnext_mcp: seeded {len(report['created'])} translation(s) — task types, wizard "
			"labels, compliance form labels, shift status messages and the mobile error catalogue "
			"now exist in English and Spanish as ROWS. list_translations(language='es', "
			"missing_only=true) is what shows the gaps; update_translation fills one in with no "
			"code release. A MISSING TRANSLATION SERVES ENGLISH AND SAYS SO — never a blank, "
			"never a refusal."
		)
	if report.get("updated"):
		print(
			f"erpnext_mcp: refreshed {len(report['updated'])} shipped translation(s) to this "
			"release's wording. Rows an operator had edited were not touched."
		)
	if report.get("left_alone"):
		print(
			f"erpnext_mcp: left {len(report['left_alone'])} operator-edited translation(s) alone. "
			"That is the point of the operator_edited flag — your wording survives an upgrade."
		)
	for failure in report.get("failed") or ():
		print(f"erpnext_mcp: could not seed translation {failure.get('key')} — {failure.get('reason')}")


def _reporting_templates() -> None:
	"""Seed the three shipped report shapes, per company. v0.81.0.

	A 10-K's sections, a 10-Q's, and an MD&A on its own — each section already
	naming the tool on this site that fills it, which is what lets
	`generate_quarterly_report_skeleton` hand somebody a section that knows where
	its numbers come from.

	NEVER OVERWRITES ONE AN OPERATOR HAS EDITED, and that is the point of the
	doctype rather than a nicety. The whole reason a report's shape is data is so
	a farm can add the section its lender started asking for without waiting for a
	release; a migration that reset those edits every upgrade would make "config
	not code" a slogan instead of a property.

	Runs on install AND after every migrate, so a site upgrading from any earlier
	version gets the three on its next migrate rather than needing a patch.

	Never raises: it runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench.
	"""
	try:
		from .tools import disclosure

		report = disclosure.install_reporting_templates()
	except Exception as exc:  # pragma: no cover - the seeder swallows its own
		print(f"erpnext_mcp: the reporting templates were not seeded — {type(exc).__name__}: {exc}")
		return
	if report.get("created"):
		print(
			f"erpnext_mcp: seeded {len(report['created'])} reporting template(s) — the shape of a "
			"periodic report is now a record, so its sections, their order, their Spanish and the "
			"tool each is filled from are editable with update_reporting_template and no code "
			"release. list_reporting_templates has the register. AN EDITED TEMPLATE IS NEVER "
			"OVERWRITTEN by a later migrate."
		)
	if report.get("failed"):
		print(
			f"erpnext_mcp: {len(report['failed'])} reporting template(s) could not be seeded: {report['failed']}"
		)


def _trade_documents() -> None:
	"""Seed the trade document templates and the three tiers' default requirements.

	Same non-overwrite contract as the wizards, and one step more cautious for
	the requirements: a rule is written only where none exists for that
	destination and template AT ALL, enabled or disabled. An operator who turned
	a document off did so on purpose, and a seeder that put it back would be
	overruling them on every upgrade.

	Runs on install and after every migrate, so a site upgrading from any earlier
	version picks the templates up without a bespoke patch.

	Never raises: it runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench.
	"""
	try:
		from .tools import shipments

		report = shipments.install_trade_documents()
	except Exception as exc:  # pragma: no cover - the seeder swallows its own
		print(f"erpnext_mcp: the trade documents were not seeded — {type(exc).__name__}: {exc}")
		return
	if report.get("templates_created"):
		print(
			f"erpnext_mcp: seeded {len(report['templates_created'])} trade document template(s) "
			f"and {len(report['requirements_created'])} destination requirement(s) — shipping "
			"paperwork for local, domestic and international loads is now one register. "
			"list_trade_document_templates has the kinds; get_destination_requirements has what "
			"each destination asks for. A NEW EXPORT MARKET IS ROWS, NOT A RELEASE: nothing in "
			"this app's code names a country. THE GATE SHIPS OFF — an incomplete checklist is "
			"reported and does not hold a shipment until `trade_document_enforcement` is ticked."
		)
	elif report.get("requirements_created"):
		print(
			f"erpnext_mcp: seeded {len(report['requirements_created'])} destination "
			"requirement(s); the templates were already present and were left alone."
		)
	for failure in report.get("failed") or ():
		target = failure.get("template") or failure.get("requirement")
		print(f"erpnext_mcp: could not seed trade document {target} — {failure.get('reason')}")


def _wizard_definitions() -> None:
	"""Seed the five shipped wizards. Never overwrites one an operator has edited.

	THE NON-OVERWRITE IS THE POINT OF THE WHOLE DOCTYPE. A wizard is data so that
	a farm can reword a question, add the step their state started requiring in
	July, or translate a field their crew kept misreading — without waiting for
	an App Store review. A migration that reset those edits every upgrade would
	make "config not code" a slogan rather than a property, so the seeder writes
	a definition only where none exists.

	Runs on install AND after every migrate, so a site upgrading from any earlier
	version gets the five on its next migrate rather than needing a bespoke patch.

	Never raises: it runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench.
	"""
	try:
		from .tools import wizards

		report = wizards.install_wizard_definitions()
	except Exception as exc:  # pragma: no cover - the seeder swallows its own
		print(f"erpnext_mcp: the wizard definitions were not seeded — {type(exc).__name__}: {exc}")
		return
	if report.get("created"):
		print(
			f"erpnext_mcp: seeded {len(report['created'])} wizard definition(s) — a multi-step "
			"flow is now a record, so its steps, its fields, its validation and its Spanish are "
			"editable with no code release and no App Store review. list_wizard_definitions has "
			"the register. AN EDITED WIZARD IS NEVER OVERWRITTEN by a later migrate."
		)
	for failure in report.get("failed") or ():
		print(f"erpnext_mcp: could not seed wizard {failure.get('wizard')} — {failure.get('reason')}")


def _settlement_invoice_link() -> None:
	"""Give Sales Invoice the column that points back at a Settlement Statement.

	v0.70.0, and the second place in this app that adds a Custom Field to a
	doctype it does not own — `compliance_fields.py` is the first and argues the
	general case. The argument here is narrower and easier: the settlement→invoice
	link has to be readable from BOTH ends or neither end can be trusted. An
	invoice with no pointer back is an invoice nobody can trace to the statement
	it billed, and "which settlement is this revenue" is the question an audit
	asks first.

	The field is created lazily by `tools/sales.py` on first use as well, so a
	site that never runs this still gets a working link. Doing it here means the
	column exists before anybody needs it, which is what makes it filterable in
	the Desk and in `list_sales_invoices`.

	Never raises: it runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench.
	"""
	try:
		from .tools import sales

		if not frappe.db.exists("DocType", "Sales Invoice"):
			return
		if sales.ensure_settlement_link_field():
			return
		print(
			"erpnext_mcp: Sales Invoice did not take the settlement_statement Custom Field. "
			"Invoices created from a settlement will still be created; they will not carry a "
			"link back to it."
		)
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		print(
			f"erpnext_mcp: the Sales Invoice settlement link was not installed — {type(exc).__name__}: {exc}"
		)


def _bank_categorization_fields() -> None:
	"""Give Bank Transaction the three columns a categorisation writes into.

	v0.71.0, and the third place this app adds a Custom Field to somebody else's
	doctype — `compliance_fields.py` argues the general case and
	`_settlement_invoice_link` is the narrow precedent. The argument here is that
	the alternative is a parallel record with one row per bank transaction, and a
	shadow record of a thing that already exists drifts from it.

	`tools/banking_bridge.py` creates the same fields lazily on first use, so a
	bench that upgraded without running the installer still works. Doing it here
	means the columns exist before anybody needs them, which is what makes them
	filterable in the Desk.

	Never raises: it runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench.
	"""
	try:
		from .tools import banking_bridge

		if not frappe.db.exists("DocType", "Bank Transaction"):
			return
		if banking_bridge.ensure_categorization_fields():
			return
		print(
			"erpnext_mcp: Bank Transaction did not take the categorisation Custom Fields. "
			"apply_categorization_rules will refuse to write until they exist; every read tool "
			"still works and reports every transaction as uncategorised."
		)
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		print(
			f"erpnext_mcp: the Bank Transaction categorisation fields were not installed — "
			f"{type(exc).__name__}: {exc}"
		)


def _bank_pairing_fields() -> None:
	"""Give Bank Account the seven columns a pairing and a Plaid identity need.

	v0.73.0, and the fourth place this app adds Custom Fields to somebody else's
	doctype. The argument is the one `tools/anchors.py` makes at length: a pairing
	is a PROPERTY of an account — a brokerage and the cash-services account its
	trades settle through — and the alternative is an "Account Pairing" doctype
	with one row per pair, which is a shadow of a relationship a Link already
	expresses.

	`tools/anchors.py` creates the same fields lazily on first use, so a bench
	that pulled the code without running the installer works the first time
	somebody pairs two accounts. Doing it here means the columns exist before
	anybody needs them, which is what makes them filterable in the Desk.

	Never raises: it runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench.
	"""
	try:
		from .tools import anchors

		if not frappe.db.exists("DocType", "Bank Account"):
			return
		if anchors.ensure_pairing_fields():
			return
		print(
			"erpnext_mcp: Bank Account did not take the pairing Custom Fields. get_account_pairing "
			"will report every pairing as absent and pair_bank_accounts will refuse to write until "
			"they exist; every other tool is unaffected."
		)
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		print(
			f"erpnext_mcp: the Bank Account pairing fields were not installed — {type(exc).__name__}: {exc}"
		)


def _receipt_intelligence_fields() -> None:
	"""Give Expense Receipt the seven columns multi-vector resolution needs.

	v0.75.0, and the fifth place this app installs Custom Fields at migrate time.
	The four before it extend somebody else's doctype; this one extends our own,
	and `receipts.ensure_receipt_intelligence_fields` argues why — a register a
	site has been capturing into since v0.31.0 should not gain seven mandatory
	columns for a feature that site may never enable, and a Custom Field is
	something an operator can remove while a JSON field is something only a
	release can.

	`tools/receipts.py` creates the same columns lazily on first use, so a bench
	that pulled the code without running the installer resolves a merchant the
	first time somebody captures a receipt. Doing it here means they exist before
	anybody needs them, which is what makes them filterable in the Desk.

	Never raises: it runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench.
	"""
	try:
		from .tools import receipts

		if not frappe.db.exists("DocType", "Expense Receipt"):
			return
		if receipts.ensure_receipt_intelligence_fields():
			return
		print(
			"erpnext_mcp: Expense Receipt did not take the receipt-intelligence Custom Fields. "
			"Merchant resolution still RUNS and is still reported by submit_expense_receipt and "
			"normalize_merchant; only the storing of its answer on the record is skipped, and "
			"card-fingerprint bank matching is unavailable. Capture is otherwise unaffected."
		)
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		print(
			f"erpnext_mcp: the Expense Receipt intelligence fields were not installed — "
			f"{type(exc).__name__}: {exc}"
		)


def _i9_settings() -> None:
	"""Make I-9 Settings' declared defaults true in the database.

	v0.27.0, and the same pattern as _weather_settings — a Frappe Single whose
	defaults need seeding. Skipped silently on a site whose doctype has not
	migrated yet.
	"""
	try:
		if not frappe.db.exists("DocType", "I-9 Settings"):
			return
		settings.seed_defaults("I-9 Settings")
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		print(f"erpnext_mcp: I-9 Settings defaults were not seeded — {type(exc).__name__}: {exc}")


def _i9_document_types() -> None:
	"""Seed the USCIS-accepted documents for I-9 verification.

	v0.27.0. IT ONLY EVER CREATES WHAT IS NOT THERE, checked by doc_title.
	An operator who edited or deactivated a document keeps their change.
	"""
	try:
		report = i9_documents.seed_i9_document_types()
	except Exception as exc:  # pragma: no cover - the seeder swallows its own
		print(f"erpnext_mcp: the I-9 document types were not seeded — {type(exc).__name__}: {exc}")
		return
	if report.get("created"):
		print(f"erpnext_mcp: seeded {len(report['created'])} I-9 document type(s)")
	for failure in report.get("failed") or ():
		print(f"erpnext_mcp: could not seed I-9 document {failure.get('name')} — {failure.get('reason')}")


def _i9_print_format() -> None:
	"""Give the Desk's Print button an I-9 that looks like an I-9.

	v0.47.1. Without it the button renders Frappe's standard format — every one of
	the doctype's eighty-four fields, two to a row, `naming_series` and `pdf_col`
	included. IT ONLY EVER CREATES WHAT IS NOT THERE, by name, so an operator who
	edited the layout for their own printer keeps it through every future migrate.
	"""
	report = i9_print_format.seed_i9_print_format()
	if report.get("created"):
		print(
			f"erpnext_mcp: seeded the {report['name']!r} Print Format for I-9 Form — the Desk's "
			f"Print button now lays the record out as the form's own sections. It is a CUSTOM "
			f"format, so anything you change about it survives the next migrate."
		)
	elif report.get("reason") not in ("already present", ""):
		print(f"erpnext_mcp: the I-9 Print Format was not seeded — {report['reason']}")


def _fica_settings() -> None:
	"""Make FICA Configuration's declared defaults true in the database.

	v0.28.0, and the same pattern as _i9_settings — a Frappe Single whose
	defaults need seeding. Skipped silently on a site whose doctype has not
	migrated yet.
	"""
	try:
		if not frappe.db.exists("DocType", "FICA Configuration"):
			return
		settings.seed_defaults("FICA Configuration")
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		print(f"erpnext_mcp: FICA Configuration defaults were not seeded — {type(exc).__name__}: {exc}")


def _federal_tax_table() -> None:
	"""Seed 2025 federal tax brackets if the table is empty.

	v0.28.0. IT ONLY SEEDS WHEN THE TABLE HAS NO ROWS AT ALL for the seed
	year, so an operator who imported their own brackets or edited the seeded
	ones keeps their data.
	"""
	try:
		if not frappe.db.exists("DocType", "Federal Tax Table"):
			return
		existing = frappe.db.count("Federal Tax Table", {"tax_year": withholding.SEED_TAX_YEAR})
		if existing:
			return
		brackets = withholding.seed_brackets()
		created = 0
		for bracket in brackets:
			doc = frappe.get_doc(
				{
					"doctype": "Federal Tax Table",
					**bracket,
				}
			)
			doc.flags.ignore_permissions = True
			doc.insert()
			created += 1
		if created:
			print(
				f"erpnext_mcp: seeded {created} federal tax bracket(s) for tax year {withholding.SEED_TAX_YEAR}"
			)
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		print(f"erpnext_mcp: the federal tax table was not seeded — {type(exc).__name__}: {exc}")


def _state_tax_table() -> None:
	"""Seed 2025 Oregon income tax brackets if the table is empty.

	v0.91.0, AND UNTIL NOW NOTHING CALLED THE SEEDER AT ALL. `seed_or_brackets`
	has existed since v0.29.0 and no install path ran it, so State Tax Table was
	empty on every site — and an empty table is not an error anybody sees.
	`calculate_oregon_withholding` takes `if income_enabled and state_tax_table`
	and falls to a branch that records "disabled or no brackets", so Oregon income
	tax came out as a clean $0.00 on payroll that owed it. The other four Oregon
	amounts computed correctly the whole time, which is what kept a zero in the
	one column from looking like a broken install.

	It follows `_federal_tax_table` exactly, including the part that matters most:
	IT ONLY SEEDS WHEN THE TABLE HAS NO ROWS AT ALL for the seed year, so an
	operator who imported their own brackets — or corrected the seeded ones after
	Oregon revised them — keeps their data across every later migrate.

	Washington gets nothing here and needs nothing: it has no income tax, its
	programs are all flat rates on State Tax Configuration, and `_load_state_table`
	is only consulted for OR.
	"""
	try:
		if not frappe.db.exists("DocType", "State Tax Table"):
			return
		year = state_withholding.OR_SEED_TAX_YEAR
		existing = frappe.db.count("State Tax Table", {"state": "OR", "tax_year": year})
		if existing:
			return
		created = 0
		for bracket in state_withholding.seed_or_brackets(year):
			doc = frappe.get_doc(
				{
					"doctype": "State Tax Table",
					**bracket,
				}
			)
			doc.flags.ignore_permissions = True
			doc.insert()
			created += 1
		if created:
			print(f"erpnext_mcp: seeded {created} Oregon tax bracket(s) for tax year {year}")
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		print(f"erpnext_mcp: the state tax table was not seeded — {type(exc).__name__}: {exc}")


def _weather_settings() -> None:
	"""Make Weather Settings' declared defaults true in the database.

	v0.19.4, and the eighth job. Exactly the first job's problem on a second
	Single: a Frappe Single stores one row per field somebody has saved, so
	straight after `bench install-app` `http_timeout_seconds` has no row and reads
	None — which becomes a timeout of ZERO one `int()` later, and a timeout of zero
	is not "no timeout" to `requests`, it is a connection that fails immediately,
	every time, with nothing in a log to say why. `services/weather._setting` falls
	back to the meta default and then to its own constants for exactly this
	reason, but a belt that never gets a brace is a belt somebody eventually
	removes.

	IDEMPOTENT AND IT NEVER OVERWRITES A CHOICE, including a deliberate "off": it
	only fills fields with no stored value, which is the same contract as the
	first job because it is the same function.

	NOT A FRAPPE `fixtures` ENTRY — `test_hooks.py` forbids the word by name. A
	fixture is imported by `bench migrate` with no ability to skip what a site
	already has, so an operator who lowered their heat threshold to 75 °F would
	get it silently raised back to 80 on the next upgrade, and the first anybody
	would know is a hot afternoon that logged nothing.

	Skipped silently on a site whose doctype has not migrated yet — the same
	`bench migrate` that runs this creates it, so the next run finds it.
	"""
	try:
		from .services import weather

		if not frappe.db.exists("DocType", weather.SETTINGS_DOCTYPE):
			return
		settings.seed_defaults(weather.SETTINGS_DOCTYPE)
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		print(f"erpnext_mcp: Weather Settings defaults were not seeded — {type(exc).__name__}: {exc}")


def _compliance_fields() -> None:
	"""Add the v0.15.0 compliance fields, reporting anything that could not be done.

	`install_compliance_fields` already never raises; this wraps it anyway,
	because the thing that would take a migration down is not the installer
	failing, it is the installer failing in a way the installer did not anticipate.
	"""
	try:
		report = compliance_fields.install_compliance_fields()
	except Exception as exc:  # pragma: no cover - the installer already swallows its own
		print(f"erpnext_mcp: compliance fields were not installed — {type(exc).__name__}: {exc}")
		return
	for failure in report.get("failed") or ():
		print(f"erpnext_mcp: could not add {failure['doctype']}.{failure['fieldname']} — {failure['reason']}")


def _badge_logo_field() -> None:
	"""Add `Company.badge_logo`, the mark a printed badge card carries.

	Not a compliance field and deliberately not in that table — see
	`badges.BADGE_LOGO_FIELD`. Its own job here for the same reason the I-9
	print format has one: it belongs to a feature, the feature owns the spec,
	and a site upgrading gets it on the next migrate rather than on a patch
	somebody has to remember.

	A FAILURE HERE IS PRINTED, NOT RAISED. Badges print without a logo, so
	taking a migration down over one would be the wrong trade by a wide margin.
	"""
	try:
		badges.install_badge_logo_field()
	except Exception as exc:  # pragma: no cover - reported rather than fatal
		print(f"erpnext_mcp: Company.badge_logo was not added — {type(exc).__name__}: {exc}")


def _badge_print_format() -> None:
	"""Give the Desk's Print button a badge that looks like a badge.

	v0.56.0. Without it the button renders Frappe's standard format — Badge ID,
	Company, Employee, Active, Notes on a sheet of Letter, which is every fact on
	the record and nothing anybody can clip to a lanyard. IT ONLY EVER CREATES
	WHAT IS NOT THERE, by name, so an operator who nudged the layout for their own
	card printer keeps it through every future migrate. That matters more here
	than anywhere else this app seeds a format: laying out a CR-80 card is a
	fractional-millimetre argument with one specific piece of hardware.
	"""
	report = badge_print_format.seed_badge_print_format()
	if report.get("created"):
		print(
			f"erpnext_mcp: seeded the {report['name']!r} Print Format for Bucket Log Badge Map — "
			f"the Desk's Print button now lays a badge out at CR-80 (85.6 x 54mm), front and "
			f"back, for a card printer. It is a CUSTOM format, so anything you change about it "
			f"survives the next migrate."
		)
	elif report.get("reason") not in ("already present", ""):
		print(f"erpnext_mcp: the badge Print Format was not seeded — {report['reason']}")


def _badge_form_action() -> None:
	"""Put an "ID Card" button on the Employee form itself.

	v0.56.0, and it is the half of this feature the complaint was about. The list
	action prints a sheet for a ticked crew; this is for the far commoner moment
	— one Employee form open, and the question "where is this person's badge".
	Before it, the answer was an MCP call or a Bucket Log Badge Map docname
	nobody memorises.

	A RECORD RATHER THAN A HOOK, for the same reason the list action is one: the
	Employee form belongs to ERPNext, `test_hooks.TheFormScripts` holds every
	`doctype_js` entry to a doctype this app created, and a customisation an
	operator cannot see or switch off is not one this app installs.
	`badge_form_action.py` argues it.

	FROM v0.56.1 IT CAN ALSO BE UPDATED, and the three sentences below are the
	three states: written, brought up to date, or left alone because somebody has
	edited it. That last one PRINTS — an operator whose edit is silently kept and
	silently stale has been told nothing, and the fix they are missing here is a
	card that comes out of Save-as-PDF blank.
	"""
	report = badge_form_action.seed_badge_form_action()
	if report.get("created"):
		print(
			f"erpnext_mcp: seeded the {report['name']!r} Client Script — an Employee form now has "
			f"an ID Card button that issues or reprints their badge, shows the card, and leaves "
			f"the QR and the card PDF in the record's own Attachments. It is a row in the Desk: "
			f"untick `enabled` or delete it and this app will not put it back."
		)
	elif report.get("updated"):
		print(
			f"erpnext_mcp: {report['reason']} — the {report['name']!r} Client Script was this "
			f"app's own unedited copy, so it has been brought up to date. The card now reaches "
			f"the print tab as a blob URL instead of being written into it, which is what makes "
			f"Save-as-PDF produce a card rather than a blank page."
		)
	elif report.get("reason", "").startswith("left alone"):
		print(
			f"erpnext_mcp: the {report['name']!r} Client Script has been edited on this site, so "
			f"it was left exactly as it is — {report['reason']}. Delete the row and run "
			f"`bench migrate` to take this app's current copy, or paste the fix in by hand."
		)
	elif report.get("reason") not in ("already present", ""):
		print(f"erpnext_mcp: the Employee ID Card button was not seeded — {report['reason']}")


def _badge_list_action() -> None:
	"""Put "Print Badge Sheet" in the Employee list's Actions menu.

	v0.56.0, AND IT IS A RECORD RATHER THAN A HOOK. The Employee list belongs to
	ERPNext, and `hooks.py` promises this app does not change how a form the
	operator already had behaves. A Client Script row is the same route
	`compliance_fields` and `Company.badge_logo` take onto other people's
	doctypes: visible in the Desk, switchable off, deleted by
	`before_uninstall`, and never re-created once declined.
	`badge_list_action.py` argues the whole thing — including why, from v0.56.1,
	this app's OWN unedited copy of the script is updated in place while one an
	operator has edited is left alone and said out loud.
	"""
	report = badge_list_action.seed_badge_list_action()
	if report.get("created"):
		print(
			f"erpnext_mcp: seeded the {report['name']!r} Client Script — the Employee list's "
			f"Actions menu can now print a sheet of badge cards for the selected crew. It is a "
			f"row in the Desk: untick `enabled` or delete it and this app will not put it back."
		)
	elif report.get("updated"):
		print(
			f"erpnext_mcp: {report['reason']} — the {report['name']!r} Client Script was this "
			f"app's own unedited copy, so it has been brought up to date. The sheet now reaches "
			f"the print tab as a blob URL instead of being written into it, which is what makes "
			f"Save-as-PDF produce the cards rather than a blank page."
		)
	elif report.get("reason", "").startswith("left alone"):
		print(
			f"erpnext_mcp: the {report['name']!r} Client Script has been edited on this site, so "
			f"it was left exactly as it is — {report['reason']}. Delete the row and run "
			f"`bench migrate` to take this app's current copy, or paste the fix in by hand."
		)
	elif report.get("reason") not in ("already present", ""):
		print(f"erpnext_mcp: the Employee badge-sheet button was not seeded — {report['reason']}")


def _asset_tag_list_action() -> None:
	"""Put "Generate QR Sheet" in the Asset Register list's Actions menu. v0.83.0.

	A RECORD RATHER THAN A HOOK EVEN THOUGH THE HOOK WAS AVAILABLE. Asset Register
	is this app's own doctype, so `doctype_list_js` would not have broken any
	promise `hooks.py` makes — `asset_tag_form_action.py` gives the three reasons
	the pair went to Client Scripts anyway, and the operator-visible, switchable-off
	property is the one that decided it.
	"""
	report = asset_tag_list_action.seed_asset_tag_list_action()
	if report.get("created"):
		print(
			f"erpnext_mcp: seeded the {report['name']!r} Client Script — the Asset Register "
			f"list's Actions menu can now print a sheet of QR tags for the selected assets. It "
			f"is a row in the Desk: untick `enabled` or delete it and this app will not put it back."
		)
	elif report.get("updated"):
		print(
			f"erpnext_mcp: {report['reason']} — the {report['name']!r} Client Script was this "
			f"app's own unedited copy, so it has been brought up to date."
		)
	elif report.get("reason", "").startswith("left alone"):
		print(
			f"erpnext_mcp: the {report['name']!r} Client Script has been edited on this site, so "
			f"it was left exactly as it is — {report['reason']}. Delete the row and run "
			f"`bench migrate` to take this app's current copy, or paste the change in by hand."
		)
	elif report.get("reason") not in ("already present", ""):
		print(f"erpnext_mcp: the asset QR-sheet action was not seeded — {report['reason']}")


def _asset_tag_form_action() -> None:
	"""Put "QR Tag" on the Asset Register form. v0.83.0.

	The other half of the same feature. `generate_asset_qr` has drawn the symbol
	since v0.17.0 and there has never been a way to get it onto paper from the
	Desk; this is the button, and `api/asset_tags.py` is what it calls.
	"""
	report = asset_tag_form_action.seed_asset_tag_form_action()
	if report.get("created"):
		print(
			f"erpnext_mcp: seeded the {report['name']!r} Client Script — an Asset Register form "
			f"now has a Tags › QR Tag button that shows the tag and prints it. It is a row in the "
			f"Desk: untick `enabled` or delete it and this app will not put it back."
		)
	elif report.get("updated"):
		print(
			f"erpnext_mcp: {report['reason']} — the {report['name']!r} Client Script was this "
			f"app's own unedited copy, so it has been brought up to date."
		)
	elif report.get("reason", "").startswith("left alone"):
		print(
			f"erpnext_mcp: the {report['name']!r} Client Script has been edited on this site, so "
			f"it was left exactly as it is — {report['reason']}. Delete the row and run "
			f"`bench migrate` to take this app's current copy, or paste the change in by hand."
		)
	elif report.get("reason") not in ("already present", ""):
		print(f"erpnext_mcp: the asset QR-tag button was not seeded — {report['reason']}")


def _onboard_worker() -> None:
	"""Build or repair the Onboard Worker workspace. v0.83.0.

	`_report_failures` is not used here because this builder has three outcomes
	worth different sentences and one of them is "somebody has arranged this page,
	so nothing was done" — which is a success, not a failure, and would print as
	silence through the generic reporter.
	"""
	report = onboard_worker.install_onboard_worker()
	if report.get("created"):
		print(
			f"erpnext_mcp: built the {onboard_worker.WORKSPACE_NAME!r} workspace — "
			f"{report['shortcuts']} shortcut(s) in the order onboarding actually goes: hire, "
			f"badge, enrol the phone. It is at /app/onboard-worker."
		)
	elif report.get("filled"):
		print(
			f"erpnext_mcp: filled in the {onboard_worker.WORKSPACE_NAME!r} workspace, which was "
			f"on this site with nothing on it."
		)
	elif report.get("existed"):
		print(
			f"erpnext_mcp: the {onboard_worker.WORKSPACE_NAME!r} workspace has been arranged on "
			f"this site, so it was left exactly as it is."
		)
	elif report.get("note"):
		print(f"erpnext_mcp: the Onboard Worker workspace was not built — {report['note']}")
	for failure in report.get("failed") or []:
		print(f"erpnext_mcp: could not build {failure['name']} — {failure['reason']}")


def _command_center() -> None:
	"""Build or repair the Compliance Command Center dashboard."""
	_report_failures("the Compliance Command Center", dashboard.install_command_center)


def _dispatch_board() -> None:
	"""Build or repair the Farm Task Dispatch Kanban board and its workspace."""
	_report_failures("the Farm Task Dispatch board", dashboard.install_dispatch_board)


def _kpi_charts() -> None:
	"""Build the Sustainable CF/Acre charts. v0.19.5, and the ninth job.

	Reported through the same printer as the two dashboard builders, and it has
	one failure mode worth printing: each chart's source is a standard Script
	Report created by the SAME `bench migrate` that runs this, so a first pass may
	find a Report row not yet written. That lands in `failed` with the sentence
	saying the next migrate builds it, which is a far better outcome than a chart
	pointing at a report that does not exist — a missing chart renders nothing, and
	a broken one renders an error.

	v0.19.6 MADE IT TWO CHARTS AND EACH IS CHECKED SEPARATELY. The rolling
	twelve-month view is the new default and the discrete quarterly one stays
	beside it; a site part-way through a migrate may have one Report row and not
	the other, and building the chart that CAN be built is strictly better than
	refusing both. The quarterly chart is NOT renamed — a Dashboard Chart's
	docname is what a dashboard points at, and demoting it by renaming the record
	would silently empty the dashboards of every site that installed v0.19.5.
	"""
	_report_failures("the Sustainable CF/Acre charts", dashboard.install_kpi_charts)


def _mobile_roles() -> None:
	"""Create the v0.17.0 mobile roles and their permissions.

	Reported through the same printer as the two dashboard builders, and for the
	same v0.16.1 reason: a builder that cannot raise and is never read cannot
	report anything at all. A refused permission — one aimed at a doctype
	belonging to another app — lands in `failed` and gets printed here, which is
	the only way anybody would ever find out it did not happen.
	"""
	_report_failures("the mobile roles", roles.install_roles)


def _compliance_vocabulary() -> None:
	"""Seed the regimes and the curricula, then migrate free-text training types.

	ORDER IS LOAD-BEARING, all three steps. A `Training Type` carries regimes as
	links, so the regimes have to exist before any curriculum is written; and every
	curriculum has to exist before a training record is re-linked to one, because a
	link written to a master that is not there yet is a validation error inside a
	migration.

	The migration ALSO runs as a listed patch. Both, on purpose and for the same
	reason `register_custom_party_types` is both: the patch entry records in the
	Patch Log when the conversion first happened, and this hook catches a site that
	upgraded straight across v0.19.2. It is idempotent, so the second run is a
	no-op and prints nothing.
	"""
	for what, seeder in (
		("the compliance regimes", training.seed_regimes),
		("the common training types", training.seed_training_types),
	):
		try:
			report = seeder()
		except Exception as exc:  # pragma: no cover - the seeders swallow their own
			print(f"erpnext_mcp: {what} were not seeded — {type(exc).__name__}: {exc}")
			continue
		for failure in report.get("failed") or ():
			print(f"erpnext_mcp: could not seed {failure.get('name')} — {failure.get('reason')}")

	try:
		report = migrate_training_types.migrate_training_types()
	except Exception as exc:  # pragma: no cover - the migration swallows its own
		print(f"erpnext_mcp: training types were not migrated — {type(exc).__name__}: {exc}")
		return
	for line in migrate_training_types.report_lines(report):
		print(line)


def _completion_signatures() -> None:
	"""Sign the completions filed before v0.20.1, so a re-sent one is recognised.

	v0.20.1, and the tenth job. Runs as a listed patch AS WELL, for the same
	reason `_compliance_vocabulary` runs its migration twice: the patch entry
	records in the Patch Log when the backfill first happened, and this hook
	catches a site that upgraded straight across the version. It is idempotent —
	it only writes rows whose signature is empty — so the second run is a no-op
	and prints nothing.

	ON `after_install` IT WILL FIND NOTHING, which is correct and not worth
	branching on: a site installing this app for the first time has no completed
	assignments to sign, and a job that is only wired into one of the two hooks
	is a job somebody has to remember about later.
	"""
	try:
		report = backfill_completion_signatures.backfill_completion_signatures()
	except Exception as exc:  # pragma: no cover - the backfill swallows its own
		print(f"erpnext_mcp: completion signatures were not backfilled — {type(exc).__name__}: {exc}")
		return
	for line in backfill_completion_signatures.report_lines(report):
		print(line)


def _inspection_templates() -> None:
	"""Seed the four shapes of visit. v0.21.0, and the eleventh job.

	IT ONLY EVER CREATES WHAT IS NOT THERE, checked by template name across every
	row rather than only the live ones — which is the whole difference between
	this and a Frappe `fixtures` entry, and `test_hooks.py` forbids that word by
	name for exactly this reason. An operator who added a section to their
	close-down keeps it. One who deactivated a template the operation does not run
	keeps it deactivated. One who superseded a seeded template with their own
	version 2 does not get version 1 seeded back beside it every migrate — which
	would put two live templates with one name on the site and make the sweep's
	choice between them arbitrary.

	Runs on install AND after every migrate, so a site upgrading from any earlier
	version gets the four on its next migrate rather than needing a bespoke patch.
	"""
	try:
		from . import sessions

		report = sessions.seed_inspection_templates()
	except Exception as exc:  # pragma: no cover - the seeder swallows its own
		print(f"erpnext_mcp: the inspection templates were not seeded — {type(exc).__name__}: {exc}")
		return
	for failure in report.get("failed") or ():
		print(f"erpnext_mcp: could not seed template {failure.get('name')} — {failure.get('reason')}")


def _compliance_rules() -> None:
	"""Migrate the thirteen shipped rules into Compliance Rule records. v0.22.0.

	THE TWELFTH JOB, AND THE ONE WITH THE MOST TO GET WRONG. Until this release
	the compliance rules were Python functions and a threshold was a code change;
	after it they are records, and this is what puts them there on the migrate
	that installs the DocType.

	IT ONLY EVER CREATES WHAT IS NOT THERE, checked by `rule_id` across every row
	rather than only the live ones — the same contract as `_inspection_templates`
	above and the same reason `test_hooks.py` forbids the word `fixtures` by name.
	An operator who moved the annual housing walk from 365 days to 300 keeps 300.
	One who switched off a rule their operation does not run keeps it switched
	off. One who superseded a seeded rule with their own version 2 does not get
	version 1 seeded back beside it every migrate — which would put two live
	definitions of one rule_id on the site and make the sweep's choice between
	them arbitrary.

	THE RULES ARRIVE ENABLED, and that is deliberate against this app's usual
	instinct. Everything mutating here ships off; a migrated rule does not,
	because it was ALREADY RUNNING — as Python — the night before, and seeding it
	disabled would silently switch the whole compliance calendar off during an
	upgrade. Of the two ways to be wrong on a migrate, a calendar that keeps
	saying what it said yesterday is much the better one.

	Runs on install AND after every migrate, so a site upgrading from any earlier
	version gets the thirteen on its next migrate rather than needing a bespoke
	patch. Until it does, the sweep runs the shipped definitions and says so in
	its report — the calendar never goes blank.

	v0.80.0 SEEDS THE IPO-READINESS GATES THROUGH THIS SAME PATH, and there is
	nothing to do here for them: `seed_compliance_rules` now returns the swept
	rules plus one rule per control point in `enforcement.CONTROL_POINTS`. They
	arrive ENABLED AND ADVISORY, which is a different pairing from anything above
	and worth reading twice. Enabled, so the control runs from the first migrate
	and an operation starts accumulating the findings that will inform its own
	enforcement decision without configuring anything. Advisory, so it refuses
	nothing while it does. The usual "everything mutating ships off" instinct is
	not violated by that: an advisory control writes a calendar entry, which is
	the same thing the thirteen have been doing since v0.22.0.
	"""
	try:
		from . import compliance_rules

		report = compliance_rules.seed_compliance_rules()
	except Exception as exc:  # pragma: no cover - the seeder swallows its own
		print(f"erpnext_mcp: the compliance rules were not seeded — {type(exc).__name__}: {exc}")
		return
	if report.get("created"):
		print(
			f"erpnext_mcp: seeded {len(report['created'])} compliance rule(s) as records — "
			"thresholds, citations, scope and message are now editable with "
			"update_compliance_rule, with no code release. list_compliance_rules has the register."
		)
	for failure in report.get("failed") or ():
		print(f"erpnext_mcp: could not seed rule {failure.get('name')} — {failure.get('reason')}")
	_declarative_rules()


def _declarative_rules() -> None:
	"""Move the five rules v0.22.1 took declarative onto their new definitions.

	SEPARATE FROM THE SEEDER ABOVE, because the seeder's whole contract is that it
	leaves alone anything already on the site — which is exactly why it can never
	perform this migration. A v0.22.0 site already has a `certification_expiring`
	row naming a built-in scanner, and the seeder will never look at it again.

	Listed in `patches.txt` AND called from here, the same belt-and-braces
	`migrate_training_types` gets and for the same reason: the patch entry records
	in the Patch Log when this first ran, and the hook catches a site that upgraded
	across the version. It is therefore run at least twice on any real bench, and
	is a no-op the second time — the check is "does this row still name a
	scanner", which is false the moment the first run succeeded.
	"""
	try:
		from .patches import migrate_declarative_rules

		report = migrate_declarative_rules.migrate_declarative_rules()
	except Exception as exc:  # pragma: no cover - the patch swallows its own
		print(f"erpnext_mcp: the declarative rule migration did not run — {type(exc).__name__}: {exc}")
		return
	for line in migrate_declarative_rules.report_lines(report):
		print(f"erpnext_mcp: {line}")


def _report_failures(what: str, builder) -> None:
	"""Run a dashboard builder and PRINT WHATEVER IT COULD NOT BUILD.

	THIS FUNCTION IS THE v0.16.1 HOTFIX, and it matters more than either of the
	two bugs it was written for.

	Both builders catch their own exceptions into `report["failed"]` and return a
	report — which is right, because an exception here aborts `bench migrate` for
	the whole bench. But v0.16.0 called them and threw the report away. So when
	the Kanban Board insert failed on a real site the migration printed nothing,
	exited zero, and the board did not exist; the first anybody knew was an
	operator opening the documented route a week later and being offered a "New
	Kanban Board" dialog.

	A builder that cannot raise AND is never read cannot report anything at all.
	Not raising was the correct half; this is the half that was missing.
	"""
	try:
		report = builder()
	except Exception as exc:  # pragma: no cover - the builder already swallows its own
		print(f"erpnext_mcp: {what} was not built — {type(exc).__name__}: {exc}")
		return
	for failure in (report or {}).get("failed") or ():
		print(f"erpnext_mcp: could not build {failure.get('name')} — {failure.get('reason')}")
	# v0.18.5. A repair is the one thing this installer does to a document somebody
	# else may have edited, so it says so by name. Reading "repaired the filters on
	# Tasks in the Pool" in a migrate log is how an operator who had customised that
	# card finds out, rather than wondering later why it counts what it counts.
	repaired = (report or {}).get("repaired_filters") or ()
	if repaired:
		print(
			f"erpnext_mcp: rewrote dict-shaped filters into list form on {len(repaired)} card(s)/"
			f"chart(s) of {what}, which could not be counted otherwise: {', '.join(repaired)}"
		)


#: Doctypes whose contents are records an operator would want back, and what
#: each one is, in the words somebody reading an uninstall prompt needs.
#:
#: The governance three are here for a reason the audit log is not: they are the
#: only copy. An MCP Action Log row records something that also happened
#: somewhere else, but a Cap Table Entry is the *only* place a member id is
#: mapped to a legal name, and a Governance Document may hold the only digital
#: copy of a trust instrument. Dropping those silently would be unforgivable.
_PRECIOUS_DOCTYPES = (
	("MCP Action Log", "the audit trail of every MCP call"),
	("Cap Table Entry", "the member register — the only mapping from member id to legal name"),
	("Member Event", "the equity trail: contributions, distributions, transfers and their narratives"),
	("Governance Document", "the governance archive, including any attached agreements"),
	("Asset Cost Profile", "asset cost splits, note links and depreciation history"),
	(
		"Note Payable",
		"the notes and loans register — terms, provenance and payment history for "
		"debts whose only other record is a balance on a liability account",
	),
	(
		"Parcel",
		"the land register — assessor parcel ids, acreage, appraised values, the "
		"dates they were appraised as of, and the conveyance history of any ground "
		"that has changed entities, none of which is anywhere else on the site",
	),
	(
		"Lease",
		"the lease register, in both directions, including rent terms that exist in no other digital form",
	),
	(
		"Related Party",
		"the related-party register — who is related to the company, in what "
		"capacity, from when, and which document says so. The source for a "
		"related-party disclosure on a return",
	),
	(
		"Family",
		"the family register — the people a Family-party posting points at. Deleting "
		"it orphans every journal entry that named them, and those postings are the "
		"record of money that moved",
	),
	(
		"Field",
		"the block register — acreage, variety, rootstock, planting year and the "
		"food-safety facts about each piece of planted ground, including the last "
		"spray date a Worker Protection Standard report is built from",
	),
	(
		"Irrigation Zone",
		"the irrigation register — water sources, Oregon water right numbers, flow "
		"rates and the agricultural water test dates FSMA Subpart E turns on",
	),
	(
		"Housing Unit",
		"the labor camp register — every cabin and building with its capacity, "
		"condition, habitability inspection and detector test dates",
	),
	(
		"Housing Assignment",
		"who slept where and when. The audit trail defending an IRS Section 119 "
		"exclusion, the answer to an ORS 653 wage-deduction claim, and the camp "
		"roster a food safety investigation asks for. It exists nowhere else",
	),
	(
		"Compliance Policy",
		"the SOP library — harvest hygiene procedures, spray SOPs, worker training "
		"documents, with their versions, their effective dates and the PDFs "
		"attached. An audit asks which procedure was in force on a date, and this "
		"is the only record that answers",
	),
	(
		"Certification",
		"the certificate and licence register — GAP, GlobalGAP, PrimusGFS, organic, "
		"applicator and farm labor contractor licences, with issue and expiration "
		"dates and the certificates themselves. Operating without a current one is "
		"a violation, and this is what says whether it is current",
	),
	(
		"Regulatory Filing",
		"what was filed, to whom, on what date, under what docket number, and what "
		"they said back. A filing nobody can prove was made is a filing that was "
		"not made",
	),
	(
		"Audit Event",
		"every third-party audit and agency inspection, its findings, and whether "
		"each corrective action was ever closed. The single most damaging record to "
		"lose: an open corrective action nobody can produce a closure for is how a "
		"finding becomes a penalty",
	),
	(
		"Farm Task",
		"the dispatch register — what work was raised, from which compliance alert, "
		"what evidence closing it required and what it produced. The record that an "
		"alert was not merely read but answered",
	),
	(
		"Farm Task Assignment",
		"the chain of custody: who took each job, when they claimed it, when they "
		"started, when they finished, what they found, what proves it — and, for "
		"every job somebody could NOT do, the reason they gave. That last one exists "
		"nowhere else and is the answer to 'why was this never done'",
	),
	(
		"Housing Inspection",
		"every habitability walk of every cabin, with its findings and its "
		"photographs. The evidence behind OAR 437-004-1120 and 29 CFR 1910.142, and "
		"the only record that says a building somebody slept in was fit to",
	),
	(
		"Detector Test",
		"every smoke and CO detector test in the camp. A propane heater in a cabin "
		"with an untested CO detector is how somebody dies in their sleep, and this "
		"is the only record that says anybody checked",
	),
	(
		"Water Test",
		"every agricultural water sample, what the laboratory said, and the report "
		"itself. FSMA Subpart E asks whether the water that touched a harvested crop "
		"was tested, and nothing else on the site can answer",
	),
	(
		"Farm Shift",
		"the shift register — who was on which crew, at which block, from when to "
		"when, what the foreman did about the conditions hour by hour, WHAT THOSE "
		"CONDITIONS ACTUALLY WERE every fifteen minutes, and whose "
		"signature closed it. It is the exposure period every OAR 437-004-1131 "
		"question is asked against, and the per-worker joined/left spans inside it "
		"are what a wage claim turns on. The Attendance rows a close wrote survive; "
		"everything that explains them does not",
	),
	(
		"Heat Exposure Event",
		"the heat records — for each hot shift, whether water was provided at the "
		"required rate, whether shade was in reach, whether the rest cycle was "
		"taken, whether anybody showed signs and what was done about it, and the "
		"supervisor's signature under all of it. The record a serious-injury "
		"investigation is read from, and it exists nowhere else",
	),
	(
		"Normalization Adjustment",
		"the normalization register — every add-back and subtraction on operating "
		"cash flow with the sentence saying why it will not recur and the signature "
		"of whoever accepted that sentence. Losing it does not lose a number, it "
		"loses the DEFENCE of every Sustainable CF/Acre figure ever quoted from this "
		"site, and an adjusted figure nobody can inspect is indistinguishable from "
		"an arranged one",
	),
	(
		"Training Type",
		"the curriculum register — what each course is, which audits a session of "
		"it answers, and how long a record of it has to be kept. The ten this app "
		"seeds come back on a reinstall; a curriculum somebody added, and every "
		"regime somebody corrected on one, does not",
	),
	(
		"Inspection Session",
		"every templated visit: who went to which cabin on which afternoon, which "
		"version of which template they worked from, what they ticked in each "
		"section, and which Housing Inspection and Detector Test came out of it. "
		"The compliance records themselves are warned about separately and go "
		"either way; what dies with this is the CHAIN OF CUSTODY between them — "
		"that those three records are one walk with one signature rather than "
		"three claims filed a minute apart",
	),
	(
		"Inspection Template",
		"the visit register — what a Cabin Opening consists of, what evidence each "
		"section demands, which regulation each answers, and every superseded "
		"version of each. The four this app seeds come back on a reinstall; a "
		"template somebody wrote, and every edit anybody made to a seeded one, "
		"does not — and without the version a session was worked from, that "
		"session can no longer be read against what the worker was actually shown",
	),
	(
		"Mobile Access Grant",
		"who was given a phone, what for, which entities they could see, when their "
		"credential was issued — and, for every account that ended, WHO ended it and "
		"WHY. Frappe keeps the access; it keeps none of the story. 'Left at the end "
		"of harvest' and 'dismissed for cause' are different answers to the same "
		"question and this is the only place either survives",
	),
	(
		"I-9 Form",
		"every structured Form I-9 on the site — Section 1, Section 2, the Section 3 "
		"reverification history carried on the form itself, retention dates, and the status "
		"workflow from Draft through Complete to Destroyed. The only record of employment "
		"eligibility verification for every employee who has one, and for a seasonal worker "
		"on a renewing authorization the only record of every season they were reverified",
	),
	(
		"I-9 Audit Log",
		"the immutable trail of every I-9 action — who created, signed, viewed, printed, "
		"or destroyed each form, from which IP, at which moment. The record an audit asks "
		"for and the thing that says nobody tampered with the form between signing and filing",
	),
	(
		"W-4 Form",
		"every Form W-4 on the site — filing status, dependents credits, extra withholding, "
		"and the supersession chain showing which certificate replaced which. The basis for "
		"every federal withholding calculation and the record an IRS inquiry asks for",
	),
	(
		"Farm Salary Structure",
		"the salary register — which employee earns what rate, by what method (piece, "
		"hourly, salary), from which date. The basis for every payroll calculation and "
		"the record a wage claim or audit asks for",
	),
	(
		"Farm Payroll Entry",
		"the payroll register — every pay period's gross, deductions and net for every "
		"employee, with the per-slip breakdown of federal and state withholding, FICA, "
		"and the minimum wage check. The record that proves everybody was paid correctly",
	),
	(
		"Farm Payroll Deduction",
		"the standing instructions to withhold what is not a tax — court-ordered "
		"garnishments, child support, tax levies and student loans, alongside the "
		"worker's own elections for retirement, health cover and union dues. A court "
		"serves an order on the EMPLOYER, and an employer that cannot produce the order "
		"it withheld under, the date it started and the amount it took answers for the "
		"money itself. It exists nowhere else",
	),
	(
		"Expense Receipt",
		"the operational expense register — every receipt a foreman photographed in the "
		"field, with the image, the raw OCR text it was read out of, and who approved or "
		"refused it. The photograph is the substantiation a deduction rests on, and it "
		"exists nowhere else once the paper slip is in a truck door pocket",
	),
	(
		"Scale Ticket",
		"the delivery register — every load of fruit weighed onto somebody else's scale, "
		"with the photograph of the thermal slip it was read off. THE GROWER'S ONLY COPY: "
		"the packer keeps the original, the paper fades in a truck door pocket inside a "
		"season, and without these the packer's settlement statement is unauditable — "
		"there is nothing left to check it against",
	),
	(
		"Settlement Statement",
		"the packout register — what each packer said arrived, what packed out, what was "
		"culled, what it sold for and what was deducted before the cheque. The priced "
		"lines and the deduction rows exist nowhere else once the statement is filed, and "
		"they are what answers 'what did cold storage cost me last season' and 'which "
		"packer actually returns more per bin'",
	),
	(
		"Bin Seal",
		"the bin register — every bin closed in the field, with the tag that travels with it, "
		"the block and the shift it was filled on, and the names of the workers whose buckets "
		"are in it. IT CANNOT BE RECONSTRUCTED FROM ANYTHING: once the trailer has gone the "
		"buckets are tipped and mixed, the badge scans are on a handset, and the tag points at "
		"nothing. It is what answers a residue detection, a piece-rate dispute and a "
		"food-safety hold alike",
	),
	(
		"Shift Location Log",
		"the crew tracks — every GPS fix the phones posted during a shift, with the time "
		"each one was taken. It is the only record of where anybody actually went, which "
		"is what answers a re-entry-interval question and a disputed timesheet alike, and "
		"a measurement nobody kept cannot be taken again",
	),
	(
		"Regulation Feed",
		"the regulation register — every source this operation watches for change, and the "
		"append-only change log saying when each one moved. The log is the only record of "
		"WHEN a regulation shifted under a rule that was written from it, which is what an "
		"auditor asks about a citation that has been renumbered twice, and nothing outside "
		"this site holds it",
	),
	(
		"Tax Form",
		"the filing register — every W-2, 1099-NEC, 941, OR-WR, OQ and WA-ESD that was "
		"computed, holding the box and line values exactly as they stood when it was "
		"generated rather than as today's payroll would recompute them, when it was "
		"filed, and what the agency gave back. The record of what an employer told the "
		"IRS and two states, on a date",
	),
)

#: Doctypes that go with the app and are NOT worth warning about, with why. The
#: list exists so a reader can tell "deliberately omitted" from "forgotten".
#:
#: Compliance Alert is regenerated from operational state by the nightly job, so
#: losing it loses nothing that cannot be rebuilt in one scheduler tick. The only
#: irreplaceable thing on one is a human's dismissal reason, and a dismissal on
#: an alert whose source condition still holds comes back anyway.
_REGENERATED_DOCTYPES = (
	("Compliance Alert", "regenerated from operational state by the nightly sweep"),
	("Staged File Upload Session", "half-finished uploads"),
	("Staged File Chunk", "half-finished uploads"),
	# Seeded from `training.REGIMES` on every migrate, so a reinstall restores the
	# ten. A row an operator ADDED for a scheme this app does not model would not
	# come back — but it carries nothing except its own name, and the records that
	# referenced it are gone with their own doctypes anyway.
	("Compliance Regime", "seeded from erpnext_mcp/training.py on every migrate"),
	("Compliance Regime Link", "the regime tags on alerts and training types, rewritten by the sweep"),
	# v0.19.4. A Single whose every field is reseeded from its own declared
	# defaults on the next migrate, so a reinstall restores a working
	# configuration rather than an empty one. The per-company threshold override
	# rows are the exception and are deliberately not warned about: they are a
	# handful of numbers an operator typed and can retype, and the readings they
	# governed go with `Farm Shift` — which IS on the precious list, and whose
	# entry is where the loss of a weather timeline is actually spelled out.
	(
		"I-9 Document Type",
		"pre-seeded USCIS-accepted document list, rebuilt from i9_documents.py on every migrate",
	),
	("I-9 Settings", "reseeded from its own declared defaults on every migrate"),
	("FICA Configuration", "reseeded from its own declared defaults on every migrate"),
	("Federal Tax Table", "pre-seeded IRS Pub 15-T brackets, rebuilt from withholding.py on every migrate"),
	("Weather Settings", "reseeded from its own declared defaults on every migrate"),
	(
		"Weather Company Override",
		"per-company threshold rows on Weather Settings — a few numbers, retypable",
	),
	# v0.19.6. THE ONLY DOCTYPE THIS APP SHIPS THAT IS A CACHE, and the reason it
	# is on this list rather than the precious one is worth stating rather than
	# leaving to be inferred from its absence: every row in it is DERIVABLE. Each
	# is what `services/windowed_reports.py` would compute again from GL Entry,
	# the Asset register and the Field register — all of which survive, or are
	# themselves warned about above. Losing the cache loses no fact and no
	# judgement; it loses the speed of the first report somebody opens
	# afterwards, and the overnight sweep has it back by morning.
	(
		"Financial KPI History",
		"precomputed windowed KPI values — recomputed from the ledger by the overnight sweep",
	),
)


def before_uninstall() -> None:
	"""Warn about every record that goes with the app, while there is time to export.

	Frappe drops an app's doctypes and their tables on uninstall. An operator
	uninstalling for compliance reasons is exactly the person who wanted to keep
	this, so it is spelled out rather than discovered afterwards.
	"""
	_remove_badge_list_action()
	_remove_badge_form_action()
	_remove_asset_tag_list_action()
	_remove_asset_tag_form_action()
	_remove_onboard_worker()

	losses = []
	for doctype, what in _PRECIOUS_DOCTYPES:
		try:
			count = frappe.db.count(doctype)
		except Exception:
			continue
		if count:
			losses.append((doctype, count, what))

	grafted = _compliance_field_losses()
	_report_surviving_roles()
	if not losses and not grafted:
		return

	if losses:
		lines = "\n".join(f"  {count:>6}  {doctype} — {what}" for doctype, count, what in losses)
		exports = "\n".join(
			f"  bench --site <site> backup --only-doctype '{doctype}'" for doctype, _count, _what in losses
		)
		print(
			"\nerpnext_mcp: uninstalling will drop these records permanently:\n"
			f"{lines}\n\n"
			"Attachments on a Governance Document are Files and survive the uninstall, "
			"but nothing will say which document they belonged to.\n"
			"To keep any of it, export first — in the Desk via Report View > Menu > "
			"Export, or:\n"
			f"{exports}\n"
		)

	if grafted:
		columns = "\n".join(f"  {doctype}.{fieldname}" for doctype, fieldname in grafted)
		print(
			"\nerpnext_mcp: it will ALSO drop these columns from doctypes belonging to "
			"OTHER apps, and everything anybody has typed into them:\n"
			f"{columns}\n\n"
			"These are the v0.15.0 compliance fields. The records they sit on — spray "
			"logs, employees, bucket log entries — survive the uninstall; the applicator "
			"names, EPA registration numbers, REIs, PHIs, I-9 statuses and traceability "
			"links do not. Export the affected doctypes BEFORE uninstalling, not after:\n"
			f"{chr(10).join(sorted({f'  bench --site <site> backup --only-doctype {doctype!r}' for doctype, _f in grafted}))}\n"
		)


def _remove_badge_list_action() -> None:
	"""Take this app's button off the Employee list before the app goes.

	v0.56.0. THIS IS THE ONE PIECE OF CLEANUP `before_uninstall` DOES rather than
	warns about, and the asymmetry is the point. Everything else in this function
	reports what an uninstall will destroy, because those are the operator's
	records and destroying them is their decision. A Client Script this app wrote
	onto ERPNext's Employee list is not the operator's record and not a decision:
	left behind, it is a button calling a method that no longer exists — a form
	that behaves differently with no way to find out why, which is the exact
	outcome `hooks.py` promises against.

	It only removes a row still carrying this app's marker, so a script somebody
	has adopted and rewritten stays. Never raises: an uninstall that died here
	would leave the app half-removed.
	"""
	report = badge_list_action.remove_badge_list_action()
	if report.get("removed"):
		print(f"erpnext_mcp: removed the {report['name']!r} Client Script from the Employee list.")
	elif report.get("reason") not in ("not present", ""):
		print(
			"\nerpnext_mcp: could not remove this app's Client Script from the Employee list — "
			f"{report['reason']}.\nDelete it by hand in the Desk under Client Script, or its "
			'"Print Badge Sheet" button will stay on the list calling a method that has gone.\n'
		)


def _remove_badge_form_action() -> None:
	"""Take this app's ID Card button off the Employee form before the app goes.

	The same cleanup `_remove_badge_list_action` does and for the same reason: a
	button left behind calls a method that no longer exists, which is a form
	behaving differently with no way to find out why.
	"""
	report = badge_form_action.remove_badge_form_action()
	if report.get("removed"):
		print(f"erpnext_mcp: removed the {report['name']!r} Client Script from the Employee form.")
	elif report.get("reason") not in ("not present", ""):
		print(
			"\nerpnext_mcp: could not remove this app's Client Script from the Employee form — "
			f"{report['reason']}.\nDelete it by hand in the Desk under Client Script, or its "
			'"ID Card" button will stay on the form calling a method that has gone.\n'
		)


def _remove_asset_tag_list_action() -> None:
	"""Take this app's "Generate QR Sheet" action off the Asset Register list. v0.83.0.

	Asset Register goes with the app, so unlike the two badge rows this one is not
	left pointing at a form the operator keeps. The row is removed anyway: a Client
	Script naming a doctype that no longer exists is a row somebody has to work out
	the provenance of later, and leaving litter is not cheaper than sweeping it.
	"""
	report = asset_tag_list_action.remove_asset_tag_list_action()
	if report.get("removed"):
		print(f"erpnext_mcp: removed the {report['name']!r} Client Script from the Asset Register list.")
	elif report.get("reason") not in ("not present", ""):
		print(
			"\nerpnext_mcp: could not remove this app's Client Script from the Asset Register list "
			f"— {report['reason']}.\nDelete it by hand in the Desk under Client Script.\n"
		)


def _remove_asset_tag_form_action() -> None:
	"""Take this app's QR Tag button off the Asset Register form. v0.83.0."""
	report = asset_tag_form_action.remove_asset_tag_form_action()
	if report.get("removed"):
		print(f"erpnext_mcp: removed the {report['name']!r} Client Script from the Asset Register form.")
	elif report.get("reason") not in ("not present", ""):
		print(
			"\nerpnext_mcp: could not remove this app's Client Script from the Asset Register form "
			f"— {report['reason']}.\nDelete it by hand in the Desk under Client Script.\n"
		)


def _remove_onboard_worker() -> None:
	"""Take the Onboard Worker landing page off before the app goes. v0.83.0.

	The page links to Employee, which SURVIVES the uninstall — so this is closer to
	the badge buttons than to the Client Script above: left behind, it is a
	workspace in the operator's Desk, in a module that has gone, pointing at a mix
	of doctypes that still exist and doctypes that do not.

	A page somebody has moved to another module is theirs and stays — see
	`onboard_worker.remove_onboard_worker`.
	"""
	report = onboard_worker.remove_onboard_worker()
	if report.get("removed"):
		print(f"erpnext_mcp: removed the {report['name']!r} workspace.")
	elif report.get("reason", "").startswith("left alone"):
		print(f"erpnext_mcp: the {report['name']!r} workspace was {report['reason']}.")
	elif report.get("reason") not in ("not present", ""):
		print(
			f"\nerpnext_mcp: could not remove the {report['name']!r} workspace — "
			f"{report['reason']}.\nDelete it by hand in the Desk under Workspace.\n"
		)


def _report_surviving_roles() -> None:
	"""Say what UNINSTALLING DOES NOT REMOVE, which is the other half of honesty.

	The six v0.17.0 roles are rows in Frappe's own `Role` table and the User
	Permissions are rows in `User Permission`. Neither belongs to this app, so
	neither is dropped — and an operator uninstalling to revoke a fleet of phones
	would otherwise believe they had. They have not: the accounts still exist,
	still hold the roles, and still have live API credentials. What they lose is
	the ability to reach the MCP endpoint, which is a different thing.

	`_PRECIOUS_DOCTYPES` warns about what goes. This warns about what stays,
	because a surviving credential nobody knows about is worse than a lost
	record somebody was told about.
	"""
	try:
		held = [
			name
			for name in roles.ROLE_NAMES
			if frappe.db.exists("Role", name)
			and frappe.db.count("Has Role", {"role": name, "parenttype": "User"})
		]
	except Exception:
		return
	if not held:
		return
	print(
		"\nerpnext_mcp: uninstalling does NOT remove these, and they are not this app's to "
		"remove:\n"
		+ "\n".join(f"  the {name} role, and every user holding it" for name in held)
		+ "\n  every Company User Permission this app wrote\n"
		+ "  every API key and secret on those users\n\n"
		"Those accounts will lose the MCP endpoint and keep everything else — including "
		"live credentials for Frappe's own REST API. To actually end mobile access, run "
		"revoke_mobile_user for each account BEFORE uninstalling, or disable the users "
		"afterwards by hand.\n"
	)


def _compliance_field_losses() -> list:
	"""The v0.15.0 Custom Fields that are on this site, as (doctype, fieldname).

	Only the ones this app grafted onto somebody ELSE'S doctype — the `verify`
	targets are declared fields of this app's own doctypes and go with those, and
	are already covered by `_PRECIOUS_DOCTYPES`.
	"""
	out = []
	for target in compliance_fields.TARGETS:
		if target.mode != "extend":
			continue
		for spec in target.fields:
			try:
				if frappe.db.exists(
					compliance_fields.CUSTOM_FIELD,
					{"dt": target.doctype, "fieldname": spec.fieldname},
				):
					out.append((target.doctype, spec.fieldname))
			except Exception:
				continue
	return out


def _kpi_definitions() -> None:
	"""Seed Sustainable CF/Acre as the first Financial KPI Definition. v0.39.0.

	THE FOURTEENTH JOB, AND IT SEEDS EXACTLY ONE RECORD. That is the argument
	rather than a starting point somebody has not got round to finishing.

	A seeded KPI is a claim that this app knows what an operation should watch,
	and it can only honestly make that claim about a metric it also ships the
	computer for. Sustainable CF/Acre qualifies: the computer is tested, the
	reasoning is four pages in `docs/kpi_sustainable_cf_per_acre.md`, and the KPI
	is the reason v0.19.5 exists. A current ratio does not — not because it is a
	bad metric, but because the accounts that make it up are named differently on
	every chart, and a seeded definition pointing at the wrong ones would put a
	confident, precise, WRONG number on somebody's dashboard from the day they
	installed the app. `create_financial_kpi_definition` is how the rest arrive,
	authored by the operation whose accounts they are about.

	IT SEEDS NO THRESHOLDS EITHER, for the same reason. A defensible floor under
	cash flow per acre is a number about one operation's own cost structure and
	debt service, and a seeded one would be a line somebody had not drawn being
	enforced on a compliance calendar.

	THE `kpi_id` IS THE ONE THE CACHE ALREADY USES. Every Financial KPI History
	row written since v0.19.6 carries `kpi_key = "sustainable_cf_per_acre"`, so
	the seeded definition adopts that series rather than starting a second one
	beside it — which would be the unmarked join the whole framework is written
	against.

	Checked by `kpi_id` and idempotent, like every seeder here: an operator who
	disabled it stays disabled, one who set thresholds keeps them, one who moved
	it down the dashboard does not find it back at the top after every migrate.
	"""
	try:
		from .services import kpi_engine

		report = kpi_engine.seed_kpi_definitions()
	except Exception as exc:  # pragma: no cover - the seeder swallows its own
		print(f"erpnext_mcp: the KPI definitions were not seeded — {type(exc).__name__}: {exc}")
		return
	if report.get("created"):
		print(
			f"erpnext_mcp: seeded {len(report['created'])} financial KPI definition(s) — a KPI is "
			"now a record, so its window, thresholds and dashboard position are editable with "
			"update_financial_kpi_definition and new ones need no code release. "
			"list_financial_kpi_definitions has the register."
		)
	for failure in report.get("failed") or ():
		print(f"erpnext_mcp: could not seed KPI {failure.get('name')} — {failure.get('reason')}")


def _farm_task_templates() -> None:
	"""Seed the five shapes of single task. v0.41.0.

	THE FIVE ARE THE SHIPPED COMPLIANCE RULES WHOSE WORK REPEATS: a cabin
	habitability inspection, a smoke detector test, a water quality test, a
	certification renewal and a training record. Every type, skill, duration,
	dispatch mode and evidence contract on them matches `ALERT_TASK_MAP` in
	`tools/dispatch.py` to the letter, which is the backward-compatibility
	guarantee: a site that points its rules at these templates raises exactly the
	tasks it raised in v0.16.0, plus a checklist.

	NOTHING IS WIRED BY THIS JOB. Seeding a template does not point any rule at
	one — `Compliance Rule.producer_task_template` is left exactly as it was, so
	an upgrade changes no task any sweep produces. Pointing a rule at a template
	is a deliberate act, through `update_compliance_rule`, by somebody who has
	read what the template asks for. An upgrade that silently changed the shape of
	the work a calendar dispatches is the one thing this app will not do.

	IT ONLY EVER CREATES WHAT IS NOT THERE, checked by template name — the same
	contract `_inspection_templates` and `_compliance_rules` keep, and the same
	reason `test_hooks.py` forbids the word `fixtures` by name. An operator who
	added an item to the detector checklist keeps it. One who disabled a template
	their operation does not run keeps it disabled.

	Runs on install AND after every migrate, so a site upgrading from any earlier
	version gets the five on its next migrate rather than needing a bespoke patch.
	"""
	try:
		from . import task_templates

		report = task_templates.seed_farm_task_templates()
	except Exception as exc:  # pragma: no cover - the seeder swallows its own
		print(f"erpnext_mcp: the farm task templates were not seeded — {type(exc).__name__}: {exc}")
		return
	if report.get("created"):
		print(
			f"erpnext_mcp: seeded {len(report['created'])} farm task template(s) — the shape of a "
			"recurring job is now a record, so its skill, duration, evidence contract and "
			"checklist are editable with update_farm_task_template and new ones need no code "
			"release. list_farm_task_templates has the register. NOTHING WAS WIRED: point a "
			"Compliance Rule at one with update_compliance_rule(producer_task_template=...) when "
			"you have read what it asks for."
		)
	for failure in report.get("failed") or ():
		print(
			f"erpnext_mcp: could not seed farm task template {failure.get('name')} — {failure.get('reason')}"
		)


def _breakeven_account_fields() -> None:
	"""Give Account the three columns a cost classification lives in. v0.87.0.

	The seventh place this app installs Custom Fields at migrate time, and it
	extends somebody else's doctype for the reason the earlier ones do: an Account
	is ERPNext's, a farm may never run a breakeven, and three shipped fields on a
	core doctype for a feature that site will not use is schema nobody asked for. A
	Custom Field is something the operator who decided against it can remove.

	`tools/breakeven.ensure_account_behavior_fields` creates the same columns
	lazily on first use, so a bench that pulled the code without running the
	installer classifies an account the first time somebody says so. Doing it here
	means they exist before anybody needs them, which is what makes them visible on
	the Account form and filterable in the Desk.

	Never raises: it runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench.
	"""
	try:
		from .tools import breakeven

		if not frappe.db.exists("DocType", "Account"):
			return
		if breakeven.ensure_account_behavior_fields():
			return
		print(
			"erpnext_mcp: Account did not take the three breakeven classification Custom Fields. "
			"Every breakeven still COMPUTES and the heuristic still runs; the only loss is that a "
			"classification cannot be made to stick between analyses, so each one guesses afresh "
			"unless it is passed cost_overrides. Every result says so rather than pretending "
			"otherwise."
		)
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		print(f"erpnext_mcp: the Account breakeven fields were not installed — {type(exc).__name__}: {exc}")
