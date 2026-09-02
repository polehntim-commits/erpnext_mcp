# SPDX-License-Identifier: MIT
"""Compliance metadata on the doctypes where the work actually happens.

THIS FILE IS THE ONE PLACE THIS APP BREAKS ITS OWN RULE, AND THE RULE IS WORTH
RESTATING BEFORE THE EXCEPTION. `hooks.py` promises that installing erpnext_mcp
adds no field to any doctype it did not create, so an operator who removes the
app gets their site back exactly as it was. v0.7.0's asset tooling keeps its cost
split in an `Asset Cost Profile` beside ERPNext's Asset for precisely that
reason: a doctype of ours goes with the app, a field on theirs does not.

Sprint 7 adds fields to Spray Log, to Employee and (through v0.43.0) to Bucket
Log Entry — doctypes belonging to other apps — and it does so **on purpose**,
because the alternative is worse in a way that matters more than the promise.
v0.19.3 adds a fourth target, `Attendance`, for the same reason and with the
same argument: the shift-close bridge writes payroll rows, and a row that
cannot say which shift it came from is a row nobody can reach the working
conditions through. v0.44.0 makes Bucket Log Entry erpnext_mcp's OWN doctype —
see its `Target` entry below, `mode="verify"` rather than `mode="extend"`, the
same treatment Housing Unit and Field already get.

WHY. Compliance is a lens on operational data, not a duplicate set of records.
Every spray IS an EPA and Worker Protection Standard record; every hire IS an
I-9 record; every bucket IS an FSMA traceability record. The bolt-on version of
this feature is a "Spray Compliance Log" doctype somebody fills in after doing
the spraying, and it fails the only test that matters:

    Does removing the feature break OPERATIONS, or only break COMPLIANCE
    REPORTING?
      breaks operations too  → compliance is woven in correctly
      only breaks reporting  → it is a shadow layer; refactor

A shadow log drifts from reality the first busy week of harvest, and an auditor
who finds two records of one spray that disagree has found something far worse
than a missing field. So the applicator's name, the EPA registration number, the
restricted-entry interval and the pre-harvest interval go **on the spray record**,
where the person doing the spraying already is, and where leaving them blank
stops the spray being recorded at all.

WHAT THAT COSTS, SAID PLAINLY. Uninstalling erpnext_mcp from a site where these
have been filled in drops the columns and the data in them. `before_uninstall`
says so by name. That is a real cost and it is the right trade: an app that
refuses to touch anybody else's doctype cannot make compliance fundamental to
operations, it can only make it adjacent to them.

CUSTOM FIELD, NOT A DOCTYPE EDIT. Every field here is a `Custom Field` row,
which is Frappe's supported way for one app to extend another's doctype. The
target app's repository is untouched, its own migrations keep working, and a
version of farm_precision_ag that later adds `epa_reg_number` itself finds this
one already there and is not confused by it — `_existing` matches on the field
being present at all, not on who put it there.

GRACEFUL DEGRADATION IS THE DEFAULT, NOT A FEATURE FLAG. A site without
farm_precision_ag has no Spray Log, and the installer skips it by name and says
why. A site with Frappe HR but not farm_hr still gets the Employee fields,
because the Employee register is the same register either way. Nothing here
fails because a target is absent; a target that is absent is reported.

IT RUNS ON EVERY MIGRATE AND IS A NO-OP ON THE SECOND. `_existing` asks this
site's own meta whether the field is there before writing anything, so `bench
migrate` three times running creates the fields once. That is asserted by a test.

THE REQUIRED FIELDS ARE REQUIRED, AND THAT HAS A CONSEQUENCE. Frappe enforces
`reqd` on save, not retroactively, so existing records keep their rows and keep
being readable. But re-saving one without filling the new field in is refused —
which is exactly the intended behaviour ("this spray record was never compliant
and now you cannot pretend otherwise") and is also a surprise if nobody said it
first. So the installer counts the existing rows that would fail and reports the
number per field. That count is the operation's compliance backlog, stated in
rows, and it is the most useful thing this installer produces on a site with
history.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe

from . import compat, settings

CUSTOM_FIELD = "Custom Field"

#: The settings switch. Unlike every other switch in this app it defaults ON,
#: because this is an installer that adds columns to a schema rather than a tool
#: that writes somebody's data — and a compliance field that arrives only when an
#: operator remembers to tick a box is a compliance field that is missing on the
#: sites that needed it most. An operator who does not want their Spray Log
#: touched turns it off and the fields are never added.
SWITCH = "install_compliance_fields"

#: How many existing rows to count when reporting the backlog for a newly
#: required field. Past this the answer is "lots" and counting further is a table
#: scan nobody reads.
BACKLOG_CAP = 10000


@dataclass(frozen=True)
class ComplianceField:
	"""One column, and the sentence explaining why a regulator wants it.

	`framework` and `why` are not documentation of the code — they are the
	content of `docs/compliance_fields.md`, generated from this table by
	`describe()`, so the doc and the schema cannot drift. A field nobody can say
	the framework for does not belong here.

	`operational` is the woven-in claim, per field: what BREAKS in the day-to-day
	work if this is missing, not what breaks in the report. A field whose
	`operational` reads "nothing" is a shadow field and should be somewhere else.
	"""

	fieldname: str
	label: str
	fieldtype: str
	framework: str
	why: str
	operational: str
	reqd: bool = False
	options: str = ""
	description: str = ""
	insert_after: str = ""
	#: A Frappe `depends_on` expression, so a field that is only meaningful for
	#: some rows is only SHOWN on those rows. v0.69.0's Item target is the first
	#: to need one: a restricted-entry interval belongs on a chemical and would
	#: be nine columns of noise on a bin, a picking bag and a length of irrigation
	#: pipe. It is a DISPLAY rule and nothing more — Frappe still stores the
	#: column on every row, and a `depends_on` that hid a REQUIRED field would
	#: make the record unsaveable in the Desk, which is why nothing here combines
	#: the two.
	depends_on: str = ""
	#: Whether the Desk lets anybody type into it. v0.148.0's first use is the
	#: only one, and it is what makes a DENORMALISED column honest: the three
	#: Asset columns below are written by `asset_mirror` and by nothing else, and
	#: a copy of a fact that a second person can edit is a copy that will one day
	#: disagree with the fact. Anything a human is supposed to answer must NOT be
	#: read-only — a compliance column nobody can fill in is worse than an absent
	#: one, because the form looks complete.
	read_only: bool = False
	#: What a NEW record starts with. v0.85.0's first use is the only one, and it
	#: is worth stating what a default on a compliance column does and does not
	#: mean: it fills the Desk form for a record nobody has typed yet, and it
	#: changes NOTHING about a row that already exists. A site upgrading keeps
	#: every blank it had — which matters here, because on this particular column
	#: a blank means "nobody asked" and that is a different fact from "they said
	#: English". Leave this empty for any column where the two would be confused.
	default: str = ""

	def as_custom_field(self, doctype: str) -> dict:
		"""The `Custom Field` row this becomes, minus the fields Frappe fills in."""
		row = {
			"dt": doctype,
			"fieldname": self.fieldname,
			"label": self.label,
			"fieldtype": self.fieldtype,
			"reqd": 1 if self.reqd else 0,
			"description": self.description or self.why,
			"module": "ERPNext MCP",
		}
		if self.read_only:
			row["read_only"] = 1
		if self.default:
			row["default"] = self.default
		if self.options:
			row["options"] = self.options
		if self.insert_after:
			row["insert_after"] = self.insert_after
		if self.depends_on:
			row["depends_on"] = self.depends_on
		return row

	def describe(self, doctype: str) -> dict:
		return {
			"doctype": doctype,
			"fieldname": self.fieldname,
			"label": self.label,
			"fieldtype": self.fieldtype,
			"required": bool(self.reqd),
			"read_only": bool(self.read_only),
			"options": [line for line in self.options.split("\n") if line] if self.options else [],
			# The rows this field is shown on, when it is not shown on all of
			# them. Reported rather than left in the code because "the column is
			# there and you cannot see it" is the one thing about a `depends_on`
			# an operator has to be told.
			"shown_when": self.depends_on,
			"framework": self.framework,
			"why": self.why,
			"breaks_operationally": self.operational,
		}


@dataclass(frozen=True)
class Target:
	"""One doctype this installer touches, or checks and leaves alone.

	`mode` is "extend" for a doctype belonging to another app, which gets Custom
	Fields; and "verify" for a doctype this app already ships, whose compliance
	columns are declared in its own JSON and should simply be *there*. The second
	kind writes nothing at all: a Housing Unit missing `fsma_worker_facility` is a
	migration that did not finish, and quietly adding a Custom Field over the top
	of it would hide a real problem behind a duplicate column.
	"""

	doctype: str
	owner_app: str
	purpose: str
	fields: tuple = ()
	mode: str = "extend"
	#: What the operator does about it when the doctype is not on this site.
	absent_note: str = ""


# ── Spray Log — farm_precision_ag ───────────────────────────────────────────
#
# The Worker Protection Standard (40 CFR 170) is the reason this doctype carries
# more required fields than any other. A spray record without a restricted-entry
# interval cannot answer the only question that matters the morning after — can
# the crew go in — and a crew sent into a block inside its REI is a human injury,
# not a paperwork finding.
_SPRAY_FIELDS = (
	ComplianceField(
		fieldname="applicator_name",
		label="Applicator",
		fieldtype="Data",
		reqd=True,
		framework="EPA WPS 40 CFR 170.309(f); ORS 634 / OAR 603-057",
		why=(
			"Federal and Oregon pesticide records must name the person who made the "
			"application. Oregon additionally ties the record to a licensed applicator."
		),
		operational=(
			"Nobody can be asked what the tank actually held, whether the nozzles were "
			"the high- or low-volume set, or why a block was skipped. The applicator is "
			"the only person who knows what happened in the field that day."
		),
	),
	ComplianceField(
		fieldname="epa_reg_number",
		label="EPA Registration Number",
		fieldtype="Data",
		reqd=True,
		framework="FIFRA; EPA WPS 40 CFR 170.309(f)(3)",
		why=(
			"The registration number identifies the product as registered for this crop "
			"and this use. It is the number a residue detection is traced back through."
		),
		operational=(
			"The label is the law: without the registration number nothing downstream "
			"can check the product against the crop, the rate or the buyer's maximum "
			"residue limit, so a load can be rejected at the packing house with no way "
			"to find out which block it came from."
		),
	),
	ComplianceField(
		fieldname="rei_hours",
		label="REI (hours)",
		fieldtype="Int",
		reqd=True,
		framework="EPA WPS 40 CFR 170.407 — restricted-entry interval",
		why=(
			"The interval during which workers may not enter the treated area without "
			"PPE. Posting and notification obligations run off it."
		),
		operational=(
			"THE crew-scheduling number. Without it nobody knows when the block can be "
			"picked, thinned or irrigated, and the crew boss guesses. This is the field "
			"that makes the compliance record and the work order the same record."
		),
	),
	ComplianceField(
		fieldname="phi_hours",
		label="PHI (hours)",
		fieldtype="Int",
		reqd=True,
		framework="FIFRA label; FDA tolerances 40 CFR 180",
		why=(
			"The pre-harvest interval: how long after application the fruit may not be "
			"picked. Violating it is a residue violation on a shipped load."
		),
		operational=(
			"Harvest scheduling. A block sprayed inside its PHI cannot be picked, and "
			"the pick date is planned off this number weeks in advance."
		),
	),
	ComplianceField(
		fieldname="weather_temp_f",
		label="Temperature (°F)",
		fieldtype="Float",
		framework="EPA WPS 40 CFR 170.309; label temperature restrictions",
		why=(
			"Many labels restrict application above a stated temperature, and an "
			"inversion is the usual cause of an off-target drift complaint."
		),
		operational=(
			"Efficacy. Half the products in a tank behave differently at 90°F, and the "
			"reason a spray did not work is read out of this column the following week."
		),
	),
	ComplianceField(
		fieldname="weather_wind_mph",
		label="Wind Speed (mph)",
		fieldtype="Float",
		framework="EPA label drift restrictions; ODA drift investigations",
		why=(
			"Nearly every label sets a maximum wind speed. It is the first thing an "
			"Oregon Department of Agriculture drift investigation asks for."
		),
		operational=(
			"Whether to spray at all that morning, and the defence when a neighbour "
			"complains. Without it a drift complaint is unanswerable."
		),
	),
	ComplianceField(
		fieldname="wind_direction",
		label="Wind Direction",
		fieldtype="Data",
		framework="EPA label drift restrictions; ODA drift investigations",
		why=(
			"Direction is what turns a wind speed into a statement about where the "
			"spray went, and about which neighbouring property was downwind."
		),
		operational=(
			"Which end of the block to start at, and which rows to leave for a calmer "
			"day. A drift complaint from upwind answers itself."
		),
	),
	ComplianceField(
		fieldname="target_pest",
		label="Target Pest",
		fieldtype="Data",
		framework="FIFRA label use; IPM records for GAP / GlobalGAP",
		why=(
			"A product applied for a pest not on its label is an off-label application. "
			"Food safety audits ask for the IPM justification for every application."
		),
		operational=(
			"The IPM loop. The threshold that triggered the spray and the assessment of "
			"whether it worked both key off the target pest; without it the next "
			"application is chosen blind."
		),
	),
)


# ── Employee — farm_hr, or Frappe HR ────────────────────────────────────────
#
# These five are the difference between a payroll register and an employment
# record that survives an I-9 audit. Three are required because a person whose
# work authorisation, tax withholding or governing jurisdiction is unknown cannot
# be lawfully paid, and payroll is an operation.
_EMPLOYEE_FIELDS = (
	ComplianceField(
		fieldname="preferred_language",
		label="Preferred Language",
		fieldtype="Select",
		options="\nen\nes",
		# v0.85.0. A DEFAULT ON THE FORM, NOT A BACKFILL. Every existing blank
		# stays blank, and the blank still means "nobody asked" — which the
		# resolver treats differently from a stated "en", because a person who
		# was never asked is a training record with a hole in it and a person who
		# said English is not. What this buys is the ordinary case: an operator
		# typing a new hire on an English-speaking crew should not have to
		# choose, and a column left empty by inattention is the failure mode this
		# whole field exists to prevent.
		default="en",
		framework=(
			"EEOC national-origin guidance (29 CFR 1606); OSHA 1910.1200(h) and the Worker "
			"Protection Standard 40 CFR 170.501, both of which require training and hazard "
			"communication 'in a manner the employee can understand'"
		),
		why=(
			"Hazard communication, pesticide safety training and heat-illness training are all "
			"required to be delivered in a language the worker understands. An employer who "
			"trained a Spanish-speaking crew in English has not trained them, and the citation "
			"reads the same as if the training had not happened. This column is what lets the "
			"app prove which language each person was served in."
		),
		operational=(
			"Which language every wizard, warning, task and REI notice this person sees comes "
			"back in. NEVER INFERRED FROM A DEVICE LOCALE: a phone set to English by whoever "
			"handed it over says nothing about who is holding it now, and getting this wrong "
			"silently is exactly the failure the column exists to prevent. Where it is empty the "
			"app serves English and says so rather than guessing."
		),
		description=(
			"ISO code — 'es' for Spanish, 'en' for English. Asked at hire. Defaults to 'en' on a "
			"NEW record and never backfills an existing blank, because an empty column means "
			"'nobody asked' and that is a different fact from somebody saying English. Extensible: "
			"any code is stored, and a string with no translation for it falls back to English and "
			"reports the gap rather than serving a half-translated screen."
		),
	),
	ComplianceField(
		fieldname="i9_status",
		label="I-9 Status",
		fieldtype="Select",
		reqd=True,
		options="\nVerified\nPending\nExpired\nN-A",
		framework="IRCA 8 USC 1324a; Form I-9",
		why=(
			"Employment eligibility must be verified within three business days of hire "
			"and re-verified when a document expires. ICE fines are per form."
		),
		operational=(
			"Whether this person may be put on a crew at all. Expired means they cannot "
			"lawfully work tomorrow, which is a scheduling fact before it is a filing "
			"fact — and it is what the Sprint 7 alert engine blocks employment on."
		),
	),
	ComplianceField(
		fieldname="w4_status",
		label="W-4 Status",
		fieldtype="Select",
		reqd=True,
		options="\nOn-File\nMissing\nRequires-Update",
		framework="IRC §3402; Form W-4",
		why=(
			"Withholding must follow a signed W-4. Missing means the employer withholds "
			"at the default single rate and owes an explanation if asked."
		),
		operational=(
			"Payroll cannot compute a net cheque without it. Missing is not a reporting "
			"gap, it is a cheque that comes out at the wrong number."
		),
	),
	ComplianceField(
		fieldname="jurisdiction",
		label="Wage-Law Jurisdiction",
		fieldtype="Data",
		reqd=True,
		framework="FLSA; ORS 653 (Oregon); RCW 49.46 (Washington)",
		why=(
			"Wage law follows the location where the work is performed, not where the "
			"employer sits. Oregon and Washington differ on overtime for agricultural "
			"labour, on rest breaks and on minimum wage regions."
		),
		operational=(
			"The minimum wage and the overtime rule used to compute this person's pay. "
			"A crew that crossed the river to a Washington block is paid under a "
			"different rule that day, and this is the field that says so."
		),
		description=(
			"OR, WA, CA or another two-letter state code. Wage law follows the work "
			"location — a crew tagged here as OR that spent the week on a Washington "
			"block is being paid under the wrong rule."
		),
	),
	ComplianceField(
		fieldname="flc_license_status",
		label="FLC License Status",
		fieldtype="Data",
		framework="MSPA 29 USC 1801; ORS 658.405 farm labor contractor licensing",
		why=(
			"Anyone recruiting, supervising or transporting agricultural workers for a "
			"fee needs a farm labor contractor licence, federally and in Oregon. Using "
			"an unlicensed contractor is the grower's violation as well as theirs."
		),
		operational=(
			"Whether this person may lawfully run a crew or drive the bus. An expired "
			"licence takes a crew boss off the schedule that morning."
		),
	),
	ComplianceField(
		fieldname="flc_license_expiration",
		label="FLC License Expiration",
		fieldtype="Date",
		framework="MSPA 29 USC 1801; ORS 658.405",
		why="A licence is only a defence while it is current. The expiration date is the fact.",
		operational=(
			"Feeds the renewal alert. A crew boss whose licence lapses mid-harvest is a "
			"crew with nobody who can lawfully supervise it."
		),
	),
)


# ── Bucket Log Entry — erpnext_mcp's own doctype as of v0.44.0 ──────────────
#
# Traceability is a chain, and a chain is only as good as its weakest link. The
# FSMA Food Traceability Rule (21 CFR 1 Subpart S) wants a Traceability Lot Code
# that survives from the field to the shipment; these four columns are the
# links from the bucket to the shipment. "Picker" is not among them — Bucket
# Log Entry's own `employee` (resolved from `worker_badge` by
# `link_badge_to_employee`) already carries that fact as a proper Employee
# Link, which a Data column of the same name would only duplicate.
#
# THIS TARGET IS `mode="verify"`, NOT `mode="extend"`, unlike every other
# entry in TARGETS. Through v0.43.0 this doctype belonged to a hypothetical
# external "BucketLog bridge" app this file grafted onto; v0.44.0 makes it
# erpnext_mcp's own — the sync endpoint, the badge register and the doctype
# all ship together — so these four columns are declared in its shipped JSON
# and verified here, the same as Housing Unit's and Field's below.
_BUCKET_FIELDS = (
	ComplianceField(
		fieldname="crew_id",
		label="Crew",
		fieldtype="Data",
		framework="FSMA Subpart S; MSPA crew records",
		why=(
			"The crew is the unit a hygiene training record, a field sanitation "
			"inspection and a wage-law jurisdiction all attach to."
		),
		operational=(
			"Who to pay, who to send where tomorrow, and which crew boss answers for "
			"the block. Harvest is organised by crew, not by picker."
		),
	),
	ComplianceField(
		fieldname="block_id",
		label="Block",
		fieldtype="Data",
		framework="FSMA Subpart S critical tracking event; spray REI/PHI linkage",
		why=(
			"The block is where the lot came from, and it is the join to the spray "
			"record — which is how a residue question becomes an answerable question."
		),
		operational=(
			"Yield by block, cost by block, and the REI check that says whether the "
			"block could lawfully be picked at all."
		),
	),
	ComplianceField(
		fieldname="bin_id",
		label="Bin",
		fieldtype="Data",
		framework="FSMA Subpart S — commingling / transformation event",
		why=(
			"A bin is where buckets from several pickers become one lot. It is the "
			"transformation event the rule asks to be recorded."
		),
		operational=(
			"What actually goes on the truck. The bin is the physical unit the packing "
			"house receives and pays against."
		),
	),
	ComplianceField(
		fieldname="shipment_id",
		label="Shipment",
		fieldtype="Data",
		framework="FSMA Subpart S — shipping event; buyer traceback exercises",
		why=(
			"The shipping event closes the chain. A buyer's mock recall is timed, and "
			"an operation that cannot answer in four hours fails the audit."
		),
		operational=(
			"Getting paid. The shipment is what the invoice is raised against, and an "
			"unlinked bin is fruit that left the farm with no receivable behind it."
		),
	),
)


# ── Attendance — hrms ───────────────────────────────────────────────────────
#
# ONE COLUMN, AND IT IS A BRIDGE RATHER THAN A COMPLIANCE FACT IN ITSELF. v0.19.3
# makes the Farm Shift the anchor for exposure-based compliance, and closing a
# shift writes one submitted Attendance per crew member for the span that person
# was actually present. Without a column pointing back at the shift those rows
# are indistinguishable from a hand-keyed day, with two consequences: nobody
# reading an attendance register can get from a day to the water breaks, the
# weather and the supervisor's signature that describe it; and the bridge cannot
# tell its own rows from somebody else's, so re-closing an amended shift would
# duplicate them.
#
# It sits here rather than in `shifts.py` because this file is where every column
# this app grafts onto another app's doctype is declared, and — the part that
# matters — where `before_uninstall` goes looking to warn that removing the app
# drops it.
_ATTENDANCE_FIELDS = (
	ComplianceField(
		fieldname="farm_shift",
		label="Farm Shift",
		fieldtype="Link",
		options="Farm Shift",
		framework="OAR 437-004-1131; FSMA 21 CFR 112.161(b); ORS 653 wage records",
		why=(
			"An attendance row says somebody was at work. The shift says what the "
			"conditions were, what breaks were called, who supervised and who signed. "
			"A heat-illness investigation and a wage claim both start from the day and "
			"need the second, and this link is the only way from one to the other."
		),
		operational=(
			"Payroll reconciliation. A shift-formed day and a hand-keyed day look "
			"identical without it, so nobody can tell which rows a re-closed shift "
			"already wrote — and the bridge, unable to tell either, would pay somebody "
			"twice for one afternoon."
		),
	),
)


# ── Asset — ERPNext ─────────────────────────────────────────────────────────
#
# v0.19.5, AND THE FIRST TARGET IN THIS FILE THAT IS NOT ABOUT A REGULATOR. The
# framework line on each field below says so plainly: this is managerial
# accounting, and it is here rather than in a doctype of ours for exactly the
# argument the module docstring makes about Spray Log.
#
# WHY NOT AN `Asset Capex Profile` BESIDE THE ASSET. v0.7.0 put the cost split in
# an `Asset Cost Profile` precisely so this app would touch nobody else's schema,
# and the obvious move would be to put `capex_type` there too. It fails the test
# this file is built on. The maintenance/growth call is made ONCE, by the person
# raising the purchase, at the moment they know why they are buying the thing —
# the old pump failed, or the new block needs a pump it never had. A profile row
# written afterwards by somebody reconciling the quarter is a person reconstructing
# an intention from an invoice, and they will get it wrong in the direction that
# makes the quarter look better. Six months later nobody alive can say which it
# was.
#
# And it breaks operations, not only reporting. `capex_type` is what a replacement
# budget is built from: an operation that cannot separate "what we spend to stay
# where we are" from "what we spend to get bigger" cannot plan either one, and the
# first thing that happens is that growth is funded out of the maintenance the
# orchard needed.
#
# `capex_type` IS NOT `reqd`, AND THAT IS DELIBERATE. Frappe enforces `reqd` on
# save rather than retroactively, so marking it required would leave every
# existing Asset readable and unsaveable — a farm with two hundred assets would
# find that editing a location on a tractor bought in 2019 now demands a capex
# classification nobody present can make. The gate is in `create_asset` instead,
# where the person raising the purchase is standing, and `backfill_asset_capex_type`
# is how the history gets classified in bulk.
_ASSET_FIELDS = (
	ComplianceField(
		fieldname="capex_type",
		label="Capex Type",
		fieldtype="Select",
		options="\nMaintenance\nGrowth\nMixed",
		framework="Managerial accounting — Sustainable CF/Acre (v0.19.5); lender maintenance-capex covenants",
		why=(
			"Maintenance capex replaces productive capacity that wore out; growth capex "
			"adds capacity that was never there. Sustainable cash flow is what is left "
			"after the first is funded, and an operation that cannot tell them apart "
			"reports growth spending as if it were keeping the orchard whole."
		),
		operational=(
			"The replacement budget. 'What we spend to stay where we are' and 'what we "
			"spend to get bigger' are two different plans, and an operation that cannot "
			"separate them funds the second out of the first — which is deferred "
			"maintenance with a better name."
		),
		description=(
			"Maintenance replaces existing productive capacity (a failed irrigation pump, "
			"a worn-out tractor, a replant in kind). Growth adds capacity that was not "
			"there (a new block, a new zone, a second sprayer). Mixed is split across the "
			"two portion fields, which must sum to the gross purchase amount."
		),
	),
	ComplianceField(
		fieldname="maintenance_portion",
		label="Maintenance Portion",
		fieldtype="Currency",
		framework="Managerial accounting — Sustainable CF/Acre (v0.19.5)",
		why=(
			"A single purchase is often both — a bigger tractor replacing a smaller one "
			"is the old machine's capacity as maintenance and the difference as growth. "
			"Recording only the total forces the whole amount into one bucket and the "
			"KPI reads whichever the person picked."
		),
		operational=(
			"What a replacement reserve is sized against. The maintenance half of a mixed "
			"purchase is the recurring number; the growth half happens once."
		),
	),
	ComplianceField(
		fieldname="growth_portion",
		label="Growth Portion",
		fieldtype="Currency",
		framework="Managerial accounting — Sustainable CF/Acre (v0.19.5)",
		why=(
			"The other half of the split, stored rather than derived. A portion computed "
			"as 'the total minus the other one' cannot disagree with the total, which "
			"sounds like a virtue and means a transposed figure is silently absorbed "
			"instead of refused."
		),
		operational=(
			"What the expansion actually cost, separable from what keeping the existing "
			"ground going cost. It is the number a return-on-new-planting calculation "
			"starts from."
		),
	),
	ComplianceField(
		fieldname="capex_justification",
		label="Capex Justification",
		fieldtype="Small Text",
		framework="Managerial accounting — Sustainable CF/Acre (v0.19.5)",
		why=(
			"Required for Growth and Mixed by `create_asset`: what capacity does this "
			"add? Classifying a purchase as growth takes it out of the maintenance "
			"figure, which raises sustainable cash flow — the one direction in which "
			"a misclassification flatters the operation, and therefore the one that "
			"needs a sentence behind it."
		),
		operational=(
			"The reason the purchase was made, in the words of whoever made it, on the "
			"record it was made against. It is what next year's planning reads to find "
			"out whether the new capacity did what it was bought to do."
		),
	),
	# ── v0.148.0: the tag and the asset, made the same machine ──────────────
	#
	# THREE COLUMNS, AND THE RESTRAINT IS THE DESIGN. `Asset Register` carries
	# twenty-odd operational columns — GPS, serial, model, the service schedule,
	# the hour meter, the scan stamps — and the obvious build of this feature
	# copies all of them onto Asset so the Desk shows everything in one place.
	# That is the shadow layer this file's own docstring argues against, aimed
	# the other way: two copies of a coordinate that a person can edit on either
	# record will disagree, and an insurance schedule reading one of them while a
	# dispatcher reads the other is worse than a single copy behind one click.
	#
	# So exactly one column here is not derivable from somewhere else — the Link
	# — and the other two exist to make the Link's own truthfulness visible: what
	# kind of thing it is, so an Asset LIST can be filtered without a join, and
	# when the mirror last agreed with the tag, so drift is a column rather than
	# a discovery. Everything else is one click away on the record that owns it.
	#
	# ALL THREE ARE READ-ONLY, for the reason the `read_only` flag's own comment
	# gives: they are written by `asset_mirror` and by nothing else. A denormalised
	# copy somebody can type over is a copy that lies.
	ComplianceField(
		fieldname="asset_register",
		label="Asset Register Tag",
		fieldtype="Link",
		options="Asset Register",
		read_only=True,
		framework="Fixed-asset register integrity — the unified asset register (v0.148.0)",
		why=(
			"Which printed tag this asset is. Without it the same machine exists twice on "
			"one site — once on the books and once on a sticker — and no query can tell "
			"that the tractor in the depreciation schedule and the tractor a worker "
			"scanned this morning are one tractor."
		),
		operational=(
			"An adjuster holding a serial number, or an accountant holding a depreciation "
			"line, can reach the scan history, the service record and the photograph "
			"without knowing this app exists. Without the link, each has half a machine."
		),
		description=(
			"The Asset Register record this Asset mirrors. Written by the mirror and by "
			"nothing else — the register is the operational record and this Asset is the "
			"same machine on the books."
		),
	),
	ComplianceField(
		fieldname="farm_asset_type",
		label="Farm Asset Type",
		fieldtype="Data",
		read_only=True,
		framework="Fixed-asset register integrity — the unified asset register (v0.148.0)",
		why=(
			"The farm's own vocabulary for what the thing is — valve, tractor, wind "
			"machine, cabin — which is finer than the Asset Category the accounts are "
			"kept by and is the word anybody on the ground would use to ask for it."
		),
		operational=(
			"Filtering the Asset list to every wind machine, or every valve, without "
			"opening a record. A category built for depreciation accounts puts four "
			"unlike machines in one bucket."
		),
	),
	ComplianceField(
		fieldname="asset_register_synced_at",
		label="Register Last Synced",
		fieldtype="Datetime",
		read_only=True,
		framework="Fixed-asset register integrity — the unified asset register (v0.148.0)",
		why=(
			"When the mirror last agreed with the tag. A denormalised copy with no "
			"as-of stamp cannot be audited: nobody can tell a column that is current "
			"from one this app stopped being able to write months ago."
		),
		operational=(
			"Whether the books are being kept up to date by the field at all. A stamp "
			"months behind the tag's own modified date is a sync that has been failing "
			"silently, and it is the only thing that would say so."
		),
	),
	# ── v0.149.0: where the machine is standing ─────────────────────────────
	#
	# THE ONE DELIBERATE SECOND COPY IN THIS TARGET, and the argument above about
	# shadow columns is the reason it needs its own. Everything else on the tag
	# stays on the tag and is reached through the Link. A coordinate does not,
	# because the unified map plots equipment out of the FIXED-ASSET register
	# alongside blocks, zones and valves — and a map that had to join through
	# `Asset Register` to find a tractor could not plot one somebody created in
	# the Desk, which is half the machines on a farm that also buys through
	# invoices.
	#
	# THE DRIFT THIS NORMALLY INVITES IS ANSWERED BY REFRESHING, NOT BY HOPING.
	# `asset_mirror._refresh` rewrites both columns on every sync and stamps
	# `asset_register_synced_at`, so a stale coordinate is visible as a stale
	# stamp rather than as a wrong pin. They are read-only for the same reason
	# the other three are.
	ComplianceField(
		fieldname="gps_latitude",
		label="GPS Latitude",
		fieldtype="Float",
		read_only=True,
		framework="Fixed-asset register integrity — the unified asset register (v0.149.0)",
		why=(
			"Where the asset physically is, on the record an insurer, an assessor and a "
			"lender read. A schedule that lists a wind machine and cannot say which "
			"corner of which orchard it stands in describes a machine nobody can find."
		),
		operational=(
			"Walking to it. 'The shop yard' is four acres and a pump, a bin trailer or a "
			"generator is findable by coordinate and by nothing else — and the dispatch "
			"map plots equipment from this column."
		),
	),
	ComplianceField(
		fieldname="gps_longitude",
		label="GPS Longitude",
		fieldtype="Float",
		read_only=True,
		framework="Fixed-asset register integrity — the unified asset register (v0.149.0)",
		why=(
			"The other half, and it is stored rather than derived for the reason every "
			"coordinate pair is: half a position is not a position. A record carrying one "
			"of the two is a point on the equator or the prime meridian."
		),
		operational=(
			"The same walk. The mirror writes both columns or neither, so a machine on "
			"the map is a machine somebody actually took a fix on."
		),
	),
)


# ── Item — erpnext ──────────────────────────────────────────────────────────
#
# v0.69.0, TWO SPRINT 4 HALVES ON ONE DOCTYPE, AND THE SAME ARGUMENT AS
# Spray Log WITH THE SUBJECT CHANGED. The REI and the PHI are on the Spray Log
# because that is where the person doing the spraying is. They are ALSO on the
# product, because that is where the label says them — and the label is the law.
# A site keeping them only on the spray record has to have somebody read a jug
# before every application and type the number in correctly, which is a
# data-entry step standing between a crew and a block they may not enter.
#
# WHAT THE FIRST TWO COLUMNS BUY, CONCRETELY. `complete_farm_task` reads them
# off the chemicals in the tank mix and stamps the WINDOW onto the task — an
# expiry to the hour and a harvest date — which is what `rei_active_block_entry`
# and `phi_harvest_window` raise from. Without them the app can record that a
# spray happened and cannot say when the block reopens, which is the one
# question the record exists to answer.
#
# WHAT THE OTHER SEVEN BUY. They are the rest of what a photographed label
# says, so a scanned pesticide label has somewhere to land and the numbers
# above have something to be checked AGAINST rather than being the only copy
# of what somebody typed. `label_scan_validation` links back to the
# `Document Validation` they were read off, which is where the photograph, the
# OCR text and every check run against it live.
#
# NONE IS REQUIRED, and that is deliberate in the way `Asset.capex_type` is:
# most items in an orchard's register are bins, twine and diesel, and a required
# REI would make every one of them unsaveable until somebody typed a zero into a
# column that does not apply to a pallet.

#: When the seven label-detail fields are SHOWN. Frappe stores the column on
#: every Item either way; this decides whether a person editing a picking bag
#: has to look at seven columns about pesticide labels.
#:
#: MATCHED ON THE GROUP'S NAME RATHER THAN ON A LIST OF GROUPS, because every
#: site names its item groups differently and a hard-coded list would hide the
#: fields on the first site whose group is called 'Crop Protection' instead of
#: 'Chemicals'.
#:
#: THE SECOND HALF OF THE EXPRESSION IS THE IMPORTANT HALF. An Item that already
#: carries an EPA registration number or a signal word shows the fields whatever
#: its group is called. A `depends_on` that hides data somebody has already
#: entered is not a display preference, it is a way to lose a record — and the
#: site where the group is named something this expression does not anticipate
#: is exactly the site where that would happen silently.
#:
#: `rei_hours` and `phi_days` do NOT carry it. See `_ITEM_FIELDS`.
CHEMICAL_ITEM_DEPENDS_ON = (
	"eval:(doc.item_group && "
	"/chemical|pesticide|spray|agrochem|crop protection|fungicide|herbicide|insecticide/i"
	".test(doc.item_group)) || doc.epa_registration_number || doc.signal_word"
)

_ITEM_FIELDS = (
	# The two v0.69.0 spray-window columns. NO `depends_on`, deliberately:
	# `complete_farm_task` reads them off every chemical in a tank mix, their own
	# release frames them as 'leave at zero for anything that is not a
	# restricted-entry product', and a display rule that hid them on a site whose
	# item group this file's regex does not anticipate would silently give that
	# feature a zero it could not distinguish from a real one.
	ComplianceField(
		fieldname="rei_hours",
		label="REI (hours)",
		fieldtype="Int",
		framework="EPA WPS 40 CFR 170.407 — restricted-entry interval; FIFRA label",
		why=(
			"The label's restricted-entry interval for this product, in hours. It is the "
			"number the re-entry prohibition after every application of it is computed "
			"from, and it belongs to the product rather than to any one spray."
		),
		operational=(
			"Crew scheduling, from the item register outwards. Recorded here, finishing a "
			"spray task states the hour the block reopens by itself; recorded nowhere, "
			"somebody reads a jug in the field and the crew boss guesses."
		),
		description=(
			"Hours workers may not enter a treated area without PPE, off this product's "
			"label. Leave at zero for anything that is not a restricted-entry product — a "
			"fertiliser, a foliar nutrient, bin liners. A tank mix takes the LONGEST REI "
			"of the products in it."
		),
	),
	ComplianceField(
		fieldname="phi_days",
		label="PHI (days)",
		fieldtype="Int",
		framework="FIFRA label; FDA tolerances 40 CFR 180",
		why=(
			"The label's pre-harvest interval for this product, in days. Picking inside it "
			"is a residue violation on a shipped load, and the interval is a property of "
			"the product the same way the REI is."
		),
		operational=(
			"Harvest scheduling weeks out. A block sprayed inside its PHI cannot be picked, "
			"and the pick date is planned against this number long before the sprayer is "
			"filled — so it has to be knowable from the product, not only from the last "
			"application record."
		),
		description=(
			"Days after application before the crop may be harvested, off this product's "
			"label. Leave at zero for anything with no pre-harvest restriction. A tank mix "
			"takes the LONGEST PHI of the products in it."
		),
	),
	# The seven label-detail columns, which ARE hidden on anything that is not a
	# chemical — nothing computes off them, so hiding one costs a person a
	# scroll rather than costing a crew a re-entry window.
	ComplianceField(
		fieldname="epa_registration_number",
		label="EPA Registration Number",
		fieldtype="Data",
		framework="FIFRA 7 USC 136; 40 CFR 152.132 registration numbering",
		why=(
			"The registration number identifies the product as registered for this crop and "
			"this use, and it is the number a residue detection is traced back through. On the "
			"Item rather than only on each Spray Log it is stated once, from the label, instead "
			"of typed from memory on every application."
		),
		operational=(
			"Whether the jug in the shed may be used on the block at all. Without it nobody can "
			"check the product against the crop, the rate or the buyer's maximum residue limit "
			"before the tank is filled — which is a decision made at the shed, not at a desk "
			"afterwards."
		),
		depends_on=CHEMICAL_ITEM_DEPENDS_ON,
		insert_after="item_group",
	),
	ComplianceField(
		fieldname="signal_word",
		label="Signal Word",
		fieldtype="Select",
		options="\nDanger\nWarning\nCaution\nNone",
		framework="FIFRA labeling — 40 CFR 156.64 signal words",
		why=(
			"The signal word is the label's own statement of acute toxicity, and it is what "
			"decides the personal protective equipment the applicator wears. 'None' is a real "
			"answer for a Category IV product and is in the list for that reason."
		),
		operational=(
			"What the person mixing puts on before they open the jug. A blank here and a 'None' "
			"here mean different things to that person, and only one of them is safe to act on."
		),
		depends_on=CHEMICAL_ITEM_DEPENDS_ON,
	),
	ComplianceField(
		fieldname="phi_crop",
		label="PHI Crop",
		fieldtype="Data",
		framework="FIFRA label; FDA tolerances 40 CFR 180",
		why=(
			"One label carries a different pre-harvest interval for cherries, apples and pears. "
			"An interval with no crop beside it cannot be applied to a block, so the crop is "
			"stored with the number rather than assumed from the operation."
		),
		operational=(
			"Which blocks the interval above actually governs. A grower running cherries and "
			"pears off one chemical shed has two answers for one jug, and a record holding only "
			"one of them is wrong half the time."
		),
		depends_on=CHEMICAL_ITEM_DEPENDS_ON,
	),
	ComplianceField(
		fieldname="active_ingredients",
		label="Active Ingredients",
		fieldtype="JSON",
		framework="FIFRA 40 CFR 156.10(g) ingredient statement; FRAC/IRAC resistance management",
		why=(
			"The ingredient statement is what ties a product to a resistance-management group, "
			"to the restricted-entry interval its class carries, and to every residue tolerance "
			"downstream. Stored as [{name, concentration, unit}] because a product is often "
			"several ingredients and the concentrations are what distinguish two formulations "
			"of the same active."
		),
		operational=(
			"Rotation. Two products with different trade names and the same active ingredient "
			"are one spray as far as resistance is concerned, and a shed that cannot see that "
			"builds resistance while believing it is rotating."
		),
		depends_on=CHEMICAL_ITEM_DEPENDS_ON,
	),
	ComplianceField(
		fieldname="application_rate",
		label="Application Rate",
		fieldtype="Data",
		framework="FIFRA label use directions — 40 CFR 156.10(i)",
		why=(
			"Applying above the labeled rate is an off-label application and a residue risk; "
			"applying below it is a failed spray. The rate is on the label and belongs on the "
			"product record beside the interval it goes with."
		),
		operational=(
			"What goes in the tank. The mix is calculated from this number and the acreage, at "
			"the shed, usually before anybody has opened a compliance record."
		),
		depends_on=CHEMICAL_ITEM_DEPENDS_ON,
	),
	ComplianceField(
		fieldname="ppe_requirements",
		label="PPE Requirements",
		fieldtype="Small Text",
		framework="EPA WPS 40 CFR 170.507 — handler PPE; label PPE statement",
		why=(
			"The label's PPE statement is what the handler and any early-entry worker must "
			"wear, and the Worker Protection Standard requires the employer to provide it. It "
			"is a property of the product, so it is recorded once per product."
		),
		operational=(
			"What has to be in the shed before the spray can happen. A respirator nobody stocked "
			"is a spray that does not go out, and this is the field that says so a week early "
			"instead of on the morning."
		),
		depends_on=CHEMICAL_ITEM_DEPENDS_ON,
	),
	ComplianceField(
		fieldname="label_scan_validation",
		label="Label Scan Validation",
		fieldtype="Link",
		options="Document Validation",
		framework="Internal provenance — v0.69.0 Document Intelligence",
		why=(
			"Where the eight fields above came from. A Document Validation holds the "
			"photograph, the OCR text, the extraction and every check run against it, so a "
			"number on this Item can be traced to the label it was read off rather than to "
			"whoever typed it. It also carries whether a person has confirmed the reading, "
			"which is the only thing on that record a machine did not produce."
		),
		operational=(
			"Whether the numbers above can be trusted at the shed. An unvalidated REI and one "
			"read off a photograph a supervisor confirmed are the same integer on the screen and "
			"two very different things to bet a crew's re-entry on."
		),
		depends_on=CHEMICAL_ITEM_DEPENDS_ON,
	),
)


#: Every doctype this installer knows about, in the order it reports them.
#:
#: The last two are `verify` targets: Housing Unit and Field are this app's own
#: doctypes and already carry their compliance columns in their shipped JSON.
#: They are listed so the report answers "is the whole compliance surface
#: present" rather than "did the three custom-field targets work", which is a
#: different and less useful question.
TARGETS = (
	Target(
		doctype="Spray Log",
		owner_app="farm_precision_ag",
		purpose=(
			"Pesticide application records under FIFRA, the EPA Worker Protection "
			"Standard and Oregon's ORS 634. Every spray is a compliance event; these "
			"are the columns that make it one."
		),
		fields=_SPRAY_FIELDS,
		absent_note=(
			"farm_precision_ag is not installed on this site, so there is no Spray Log "
			"to extend. Install it and re-run `bench migrate` — nothing else is needed."
		),
	),
	Target(
		doctype="Employee",
		owner_app="farm_hr / hrms",
		purpose=(
			"Employment eligibility, tax withholding, the wage law that governs this "
			"person's pay, farm labor contractor licensing, and the language this person "
			"is trained and warned in. Every hire is a compliance event."
		),
		fields=_EMPLOYEE_FIELDS,
		absent_note=(
			"No HR app on this site, so there is no Employee register to extend. "
			"Install farm_hr or Frappe HR and re-run `bench migrate`."
		),
	),
	Target(
		doctype="Bucket Log Entry",
		owner_app="erpnext_mcp",
		mode="verify",
		purpose=(
			"Harvest chain of custody: bucket → employee → crew → block → bin → shipment. "
			"The FSMA Food Traceability Rule's critical tracking events. `employee` is a "
			"declared field of the doctype itself (resolved from worker_badge by "
			"link_badge_to_employee); crew_id/block_id/bin_id/shipment_id, verified here, "
			"are the rest of the chain. Shipped as declared fields in v0.44.0."
		),
		fields=_BUCKET_FIELDS,
		absent_note=(
			"Bucket Log Entry is not on this site, which means the DocType did not "
			"migrate — it ships with erpnext_mcp. Run `bench --site <site> migrate`."
		),
	),
	Target(
		doctype="Attendance",
		owner_app="hrms",
		purpose=(
			"The one-way bridge from a closed Farm Shift to the payroll register. A shift "
			"close writes one submitted Attendance per crew member for that person's own "
			"span, and this column is what says which shift it came from — so farm_hr has "
			"one canonical answer to 'when was Ana at work' and an investigator reading "
			"that day can get to the conditions she worked in."
		),
		fields=_ATTENDANCE_FIELDS,
		absent_note=(
			"No HR app on this site, so there is no Attendance register to extend. The "
			"shift's own crew table still carries every joined_at and left_at — nothing "
			"about the compliance record depends on the bridge. Install Frappe HR and "
			"re-run `bench migrate`, and the next shift close writes the rows."
		),
	),
	Target(
		doctype="Asset",
		owner_app="erpnext",
		purpose=(
			"The maintenance-versus-growth split every sustainable cash flow figure is "
			"read through, and the link that makes an asset on the books and a tag in the "
			"field one machine. Maintenance capex replaces what wore out and growth capex "
			"buys capacity that was never there; an operation that cannot tell them apart "
			"cannot say whether a good year was earned or borrowed from the orchard. And "
			"an operation whose fixed-asset register and whose scanned tags are two "
			"unconnected lists cannot say how many machines it owns."
		),
		fields=_ASSET_FIELDS,
		absent_note=(
			"This site has no Asset doctype, which means ERPNext's asset module is not "
			"present. get_sustainable_cf_per_acre still computes — it reports a "
			"maintenance capex of zero and says why in its warnings, which is the honest "
			"answer for a site that records no fixed assets at all."
		),
	),
	Target(
		doctype="Item",
		owner_app="erpnext",
		purpose=(
			"The pesticide label, as columns on the product it belongs to, and the two intervals "
			"every application of a chemical inherits from it. A Spray Log records what one "
			"application used; these record what the LABEL says — the restricted-entry and "
			"pre-harvest intervals, the EPA registration number, the crop the PHI applies to, "
			"the ingredient statement, the rate and the PPE — once per product. That is what "
			"lets a finished spray task say when the block reopens and when it may be picked "
			"without anybody reading a jug in the field, and what gives a scanned label's "
			"figures something to be checked against."
		),
		fields=_ITEM_FIELDS,
		absent_note=(
			"This site has no Item DocType, which means ERPNext's stock module is not present. "
			"Spray tasks still complete and still record what was used; no REI or PHI window is "
			"computed, and the two interval rules raise nothing rather than raising wrongly. A "
			"Spray Log still carries its own EPA registration number and intervals, which is "
			"where they were before v0.69.0 and still legally sufficient."
		),
	),
	# ── Company — v0.94.0 ───────────────────────────────────────────────────
	#
	# ONE FIELD, AND IT EXISTS TO STOP A QUESTION BEING ASKED FORTY TIMES.
	# `housing_deduction_from_wages` is a per-assignment Select on Housing
	# Assignment and stays there — the Housing Unit doctype's own help text
	# defends the placement ("the question is asked of the arrangement rather
	# than of the building") and that reasoning is right for the rare farm that
	# does charge rent. What was wrong was WHO ANSWERS IT: a wage deduction is
	# not a foreman's call, and this farm does not charge rent for labor camps at
	# all, so the honest default is "No" set once per entity rather than a
	# three-way choice a supervisor guesses at on every bunk assignment.
	#
	# ON COMPANY RATHER THAN A SINGLE-DOCTYPE SETTING, DELIBERATELY. This app is
	# multi-company — the orchard plus the holding company — and a `"issingle": 1`
	# doctype like `i_9_settings` holds ONE ROW FOR THE WHOLE SITE, which would
	# need a per-company child table to be correct. A field on Company is
	# per-company by construction. It is also NOT `set_company_defaults`
	# (`tools/dimensions.py`), which is explicitly the accounting-defaults tool
	# keyed to `SUPPORTED_COMPANY_DEFAULTS`; a camp housing policy is not an
	# accounting dimension.
	Target(
		doctype="Company",
		owner_app="erpnext",
		purpose=(
			"Whether this entity charges its labor camp occupants rent, answered once for "
			"the entity instead of guessed at on every bunk assignment. ORS 653 and OAR "
			"839-015 require a housing deduction to be disclosed; the Housing Assignment "
			"row is that disclosure and still carries the answer. What this changes is who "
			"supplies it."
		),
		fields=(
			ComplianceField(
				fieldname="default_housing_deduction_from_wages",
				label="Default Housing Deduction From Wages",
				fieldtype="Select",
				options="\nYes\nNo\nUnknown",
				default="No",
				framework=(
					"ORS 653.035 and OAR 839-015-0100 (deductions from agricultural wages "
					"must be disclosed and authorised); 29 CFR 531 on lodging credited "
					"against the minimum wage"
				),
				why=(
					"A housing deduction is a wage deduction, and a record that says "
					"'Unknown' for every assignment is a disclosure nobody made. This is "
					"the entity's standing answer, so each Housing Assignment is written "
					"with a real one."
				),
				operational=(
					"The foreman assigning a bunk stops being asked a wage question. The "
					"value is WRITTEN ONTO each Housing Assignment at creation, not "
					"resolved when a report reads it — `audit_packets` and the camp "
					"register read the per-assignment column, and a lazily-resolved "
					"default would leave them reporting 'Unknown' for every row created "
					"after this shipped."
				),
				description=(
					"Defaults to No — most farms charge no rent for labor camp housing. An "
					"explicit housing_deduction_from_wages on a single assignment still "
					"wins, because one arrangement can differ from the entity's norm."
				),
			),
		),
		absent_note=(
			"This site has no Company doctype, which means ERPNext itself is not installed. "
			"Housing assignments still record their own deduction answer where one is sent."
		),
	),
	Target(
		doctype="Housing Unit",
		owner_app="erpnext_mcp",
		mode="verify",
		purpose=(
			"FSMA Produce Safety Rule Subpart L worker facilities, and the habitability "
			"and detector-test dates Oregon's agricultural labor housing rules turn on. "
			"Shipped as declared fields in v0.12.0, verified here."
		),
		fields=(
			ComplianceField(
				fieldname="fsma_worker_facility",
				label="FSMA Worker Facility",
				fieldtype="Check",
				framework="FSMA Produce Safety Rule 21 CFR 112 Subpart L",
				why=(
					"Which of fifty buildings are subject to the worker facility "
					"sanitation requirements. Without the flag every building is either "
					"in scope or none is."
				),
				operational=(
					"Which buildings get walked on the sanitation round, and which need "
					"supplies restocked before a crew arrives."
				),
			),
			ComplianceField(
				fieldname="last_habitability_inspection",
				label="Last Habitability Inspection",
				fieldtype="Date",
				framework="OAR 437-004-1120 agricultural labor housing; 29 CFR 1910.142",
				why="Annual habitability inspection is the cadence a camp is walked on.",
				operational=(
					"Whether a cabin can be assigned. An uninspected unit is one nobody "
					"has confirmed has running water this season."
				),
			),
			ComplianceField(
				fieldname="smoke_detector_last_test",
				label="Smoke Detector Last Test",
				fieldtype="Date",
				framework="OAR 437-004-1120; ORS 479 smoke alarm requirements",
				why="A detector nobody has tested is a detector nobody knows works.",
				operational="Somebody sleeps there tonight.",
			),
			ComplianceField(
				fieldname="co_detector_last_test",
				label="CO Detector Last Test",
				fieldtype="Date",
				framework="OAR 437-004-1120; ORS 690 carbon monoxide alarms",
				why=(
					"Required wherever there is a fuel-burning appliance, which on a camp "
					"cabin usually means a propane heater."
				),
				operational="Somebody sleeps there tonight.",
			),
		),
	),
	Target(
		doctype="Field",
		owner_app="erpnext_mcp",
		mode="verify",
		purpose=(
			"Food safety zoning, the agricultural water and spray dates the Produce "
			"Safety Rule turns on, the dates that say when this block was actually "
			"earning, and — from v0.97.0 — where the ground stands with the National "
			"Organic Program. Shipped as declared fields in v0.12.0, v0.19.5 and "
			"v0.97.0, verified here."
		),
		fields=(
			ComplianceField(
				fieldname="productive_from_date",
				label="Productive From",
				fieldtype="Date",
				framework="Managerial accounting — Sustainable CF/Acre (v0.19.5)",
				why=(
					"The denominator of every per-acre metric is what is PRODUCTIVE, not "
					"what is owned. Without this date a pre-yield block counts as earning "
					"ground and every per-acre figure is understated by however much of the "
					"farm is still coming into bearing."
				),
				operational=(
					"When a block starts being budgeted as a crop rather than as capital "
					"under construction. It is what a picking plan, a bin forecast and a "
					"crew estimate all key off."
				),
			),
			ComplianceField(
				fieldname="productive_through_date",
				label="Productive Through",
				fieldtype="Date",
				framework="Managerial accounting — Sustainable CF/Acre (v0.19.5)",
				why=(
					"A block pulled in July earned for half the year. Null means still "
					"productive, which is the ordinary case; a date means the acreage stops "
					"counting from it, pro-rated."
				),
				operational=(
					"Whether to send a crew there next season, and whether the water and "
					"spray programme still applies to it."
				),
			),
			ComplianceField(
				fieldname="pre_yield_end_date",
				label="Pre-Yield End",
				fieldtype="Date",
				framework="Managerial accounting — Sustainable CF/Acre (v0.19.5)",
				why=(
					"Perennials spend their first years as capital rather than as crop — "
					"cherry is commonly three or four. Recorded separately from "
					"`productive_from_date` so a block still in its pre-yield years is "
					"COUNTED and reported rather than merely absent: those acres are next "
					"year's denominator, and a reader who cannot see them coming cannot "
					"read the trend."
				),
				operational=(
					"When the block moves onto the picking plan, and when the establishment "
					"budget stops. Both are planned years ahead off this date."
				),
			),
			ComplianceField(
				fieldname="food_safety_zone",
				label="Food Safety Zone",
				fieldtype="Data",
				framework="FSMA Produce Safety Rule 21 CFR 112; GAP / GlobalGAP zoning",
				why=(
					"Zoning is how a hazard assessment is expressed on the ground — which "
					"ground is adjacent to a dairy, a road, a wildlife corridor."
				),
				operational=(
					"Which blocks get walked for animal intrusion before a pick, and which "
					"can be picked at all after a flood event."
				),
			),
			ComplianceField(
				fieldname="last_spray_date",
				label="Last Spray Date",
				fieldtype="Date",
				framework="EPA WPS 40 CFR 170.407 REI; FIFRA label PHI",
				why="The date the REI and PHI windows are counted from.",
				operational=(
					"Whether a crew can enter this block today. It is read before every "
					"pick and every thinning pass."
				),
			),
			ComplianceField(
				fieldname="organic_status",
				label="Organic Status",
				fieldtype="Select",
				options="\nConventional\nTransitional\nCertified Organic",
				framework="National Organic Program 7 CFR 205 — §205.202 land requirements, §205.400 certification",
				why=(
					"Certification attaches to GROUND. The thirty-six months since the "
					"last prohibited application is a per-block fact, and a crop-level "
					"flag can represent neither it nor a farm running certified and "
					"conventional blocks of one variety. Certified acres are summed from "
					"this column."
				),
				operational=(
					"Which materials may go on this block at all. A conventional product "
					"applied to a certified block does not produce a paperwork finding — "
					"it restarts the three-year clock on that ground, and the decision is "
					"made at the shed before the tank is filled."
				),
			),
		),
	),
)


def targets_by_doctype() -> dict:
	return {target.doctype: target for target in TARGETS}


# ── the installer ───────────────────────────────────────────────────────────
def install_compliance_fields(dry_run: bool = False, respect_switch: bool = True) -> dict:
	"""Add every missing compliance field. Idempotent, and NEVER raises.

	Never raises for the same reason `ensure_party_types` does not: this runs from
	`after_migrate`, and an exception there aborts `bench migrate` for the whole
	bench. A field that cannot be added is worth reporting; it is not worth taking
	somebody's migration down over, and least of all on a site where the failure is
	that another app's doctype is half-migrated at the moment we look at it.

	`respect_switch` is False only for the MCP tool, where the dispatcher has
	already checked the same switch and checking it twice would report "off" for a
	call that got through.

	Returns a report: per target, what was created, what was already there, what
	could not be done and why, and — the number worth reading — how many existing
	rows do not satisfy each newly required field.
	"""
	report = {
		"created": [],
		"existing": [],
		"skipped": [],
		"failed": [],
		"targets": [],
		"dry_run": bool(dry_run),
		"switch": f"allow_{SWITCH}",
		"enabled": True,
	}

	if respect_switch and not _switch_on():
		report["enabled"] = False
		report["skipped"] = [
			{
				"doctype": target.doctype,
				"reason": (
					f"allow_{SWITCH} is off, so this app adds no field to any doctype. "
					"Compliance fields are the one place erpnext_mcp extends another "
					"app's schema, and an operator who has turned that off means it."
				),
			}
			for target in TARGETS
		]
		return report

	for target in TARGETS:
		report["targets"].append(_apply(target, dry_run, report))
	return report


def _switch_on() -> bool:
	try:
		return settings.tool_enabled(SWITCH)
	except Exception:
		# Settings unreadable — a request landing mid-migrate, or the Single not
		# yet created on a first install. The switch defaults ON, and the whole
		# point of this installer is that it runs on a fresh site, so the safe
		# answer here is the declared default rather than "off".
		return True


def _apply(target: Target, dry_run: bool, report: dict) -> dict:
	out = {
		"doctype": target.doctype,
		"owner_app": target.owner_app,
		"mode": target.mode,
		"purpose": target.purpose,
		"installed": False,
		"created": [],
		"existing": [],
		"missing": [],
		"failed": [],
		"backlog": {},
	}

	if not doctype_present(target.doctype):
		out["note"] = target.absent_note or f"{target.doctype} is not installed on this site."
		report["skipped"].append({"doctype": target.doctype, "reason": out["note"]})
		return out
	out["installed"] = True

	for spec in target.fields:
		if _existing(target.doctype, spec.fieldname):
			out["existing"].append(spec.fieldname)
			report["existing"].append(f"{target.doctype}.{spec.fieldname}")
			if spec.reqd:
				out["backlog"][spec.fieldname] = _backlog(target.doctype, spec.fieldname)
			continue

		if target.mode == "verify":
			# See Target.mode. A declared field that is absent is an unfinished
			# migration, and papering over it with a Custom Field would leave the
			# site with two columns and no error.
			out["missing"].append(spec.fieldname)
			report["failed"].append(
				{
					"doctype": target.doctype,
					"fieldname": spec.fieldname,
					"reason": (
						f"{target.doctype}.{spec.fieldname} ships as a declared field of this "
						"app's own DocType and is not on this site — which means the DocType "
						"did not migrate, not that a Custom Field is missing. Run `bench "
						"--site <site> migrate`. Nothing was added: a Custom Field over the "
						"top of an unfinished migration would give this site two columns and "
						"no error."
					),
				}
			)
			continue

		if dry_run:
			out["created"].append(spec.fieldname)
			report["created"].append(f"{target.doctype}.{spec.fieldname}")
			if spec.reqd:
				out["backlog"][spec.fieldname] = _backlog(target.doctype, spec.fieldname)
			continue

		problem = _create(target.doctype, spec)
		if problem:
			out["failed"].append({"fieldname": spec.fieldname, "reason": problem})
			report["failed"].append(
				{"doctype": target.doctype, "fieldname": spec.fieldname, "reason": problem}
			)
			continue
		out["created"].append(spec.fieldname)
		report["created"].append(f"{target.doctype}.{spec.fieldname}")
		if spec.reqd:
			out["backlog"][spec.fieldname] = _backlog(target.doctype, spec.fieldname)

	return out


def doctype_present(doctype: str) -> bool:
	"""Is the target doctype on this site? Public: `get_compliance_field_map` asks."""
	try:
		return compat.doctype_exists(doctype)
	except Exception:
		return False


def field_present(doctype: str, fieldname: str) -> bool:
	"""Is one compliance field already here? Public for the same reason."""
	return _existing(doctype, fieldname)


def _existing(doctype: str, fieldname: str) -> bool:
	"""Is the field on this site AT ALL — declared, custom, or added by anybody?

	Deliberately not "is there a Custom Field row we wrote". A later version of
	farm_precision_ag that adds `epa_reg_number` itself must not end up with two
	columns, and an operator who added the field by hand in the Desk has already
	solved the problem this installer exists to solve.
	"""
	try:
		if compat.has_field(doctype, fieldname):
			return True
	except Exception:
		return False
	try:
		return bool(frappe.db.exists(CUSTOM_FIELD, {"dt": doctype, "fieldname": fieldname}))
	except Exception:
		return False


def _create(doctype: str, spec: ComplianceField) -> str:
	"""Insert one Custom Field. Returns "" on success, or why it could not."""
	try:
		doc = frappe.new_doc(CUSTOM_FIELD)
		for key, value in spec.as_custom_field(doctype).items():
			if key == "module" and not compat.has_field(CUSTOM_FIELD, "module"):
				continue
			if key == "insert_after" and not compat.has_field(doctype, value):
				# The anchor field is not on this site's version of the doctype.
				# Frappe puts the field at the end, which is cosmetic. Losing the
				# field over a layout preference would not be.
				continue
			doc.set(key, value)
		doc.insert(ignore_permissions=True)
		return ""
	except Exception as exc:
		return f"{type(exc).__name__}: {exc}"


def _backlog(doctype: str, fieldname: str) -> dict:
	"""How many existing rows do not satisfy a newly required field.

	This is the number worth reading in the whole report. `reqd` binds on save,
	not retroactively, so the history stays readable — but every one of these rows
	is a record that was never compliant, and re-saving one is now refused. The
	count is the operation's compliance backlog stated in rows.
	"""
	out = {"rows_missing_a_value": None, "total_rows": None, "note": ""}
	try:
		total = frappe.db.count(doctype)
	except Exception:
		out["note"] = "this site would not answer a row count for that doctype"
		return out
	out["total_rows"] = int(total or 0)
	if not total:
		out["note"] = "no existing rows, so nothing to backfill"
		return out
	if total > BACKLOG_CAP:
		out["note"] = (
			f"more than {BACKLOG_CAP} rows; not counted. Query the doctype directly if the "
			"exact backlog matters."
		)
		return out
	try:
		missing = frappe.db.count(doctype, {fieldname: ("in", (None, ""))})
	except Exception:
		# A column that exists in meta but not yet in the table — the Custom Field
		# was inserted in this same transaction and the ALTER has not landed. Every
		# existing row is missing a value by definition.
		out["rows_missing_a_value"] = int(total or 0)
		out["note"] = (
			"the column was created in this run, so every existing row is missing a value "
			"until somebody fills it in."
		)
		return out
	out["rows_missing_a_value"] = int(missing or 0)
	if missing:
		out["note"] = (
			f"{missing} of {total} existing {doctype} record(s) have no value for this now-required "
			"field. They remain readable — Frappe enforces `reqd` on save, not retroactively — but "
			"none of them can be re-saved until it is filled in, and none of them is evidence of a "
			"compliant operation."
		)
	return out


# ── documentation, generated from the table above ───────────────────────────
def describe() -> dict:
	"""The whole compliance surface as data, for a tool and for the docs.

	`docs/compliance_fields.md` is written from this, so a field added to the
	table above cannot ship undocumented — which is the failure mode a hand-kept
	table of the same information has every single time.
	"""
	return {
		"targets": [
			{
				"doctype": target.doctype,
				"owner_app": target.owner_app,
				"mode": target.mode,
				"purpose": target.purpose,
				"field_count": len(target.fields),
				"required_fields": [spec.fieldname for spec in target.fields if spec.reqd],
				"fields": [spec.describe(target.doctype) for spec in target.fields],
			}
			for target in TARGETS
		],
		"field_count": sum(len(target.fields) for target in TARGETS),
		"required_field_count": sum(1 for target in TARGETS for spec in target.fields if spec.reqd),
		"frameworks": sorted({spec.framework for target in TARGETS for spec in target.fields}),
	}
