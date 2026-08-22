# SPDX-License-Identifier: MIT
"""Change one place, and retire one place, in whichever of the four registers it is in.

The polymorphic pair that finishes the set `api/mobile.create_farm_location`
opened. A `Field`, an `Irrigation Zone`, a `Parcel` and a `Housing Unit` are four
genuinely different records with four different tools, and this module MERGES
NONE OF THEM: `update_farm_location` resolves the register and then calls that
register's own `update_` tool, with every refusal it has always made. What is new
here is the door, not the write.

`delete_farm_location` IS THE ONE THING THAT DID NOT EXIST ANYWHERE. Nothing in
this app has ever removed a row from any of the four registers — `update_field`
refuses to re-key one, `convey_parcel` moves a parcel and keeps it, and there was
no answer at all for the ordinary case that produced this: a block typed twice at
six in the morning, one of the two with nothing on it, sitting in every picker on
the farm forever.

────────────────────────────────────────────────────────────────────────────
WHY DELETE IS THE HARD HALF, AND WHAT "SAFE" MEANS HERE
────────────────────────────────────────────────────────────────────────────

A location docname is the join key for most of this app. A spray record names the
block it was applied to, a scale ticket names the block it came out of, a lot
code traces back through it, a cost entry allocates onto it, an Attendance day is
reconstructed from a shift that names it. DELETING A LOCATION IS THEREFORE NOT
DELETING A ROW — it is cutting every one of those edges at once, and the records
on the other side do not say so afterwards. A spray record pointing at a Field
that no longer exists still prints a name; what it no longer has is anything to
resolve it to, which is a Worker Protection Standard answer that has quietly
stopped being an answer.

So the same shape `delete_account` established: FOUR CHECKS, ALL REFUSALS RATHER
THAN WARNINGS, ALL RUN BEFORE ANYTHING IS DELETED so one call reports every
reason at once rather than one reason four times.

  children      the registers that hang off this one. A Parcel holds blocks,
                zones and cabins; a Field holds zones. Deleting the parent
                orphans them, and a Field whose parcel is gone is a block
                nobody can say the location of.
  references    everything else on this site that names it with a plain Link —
                the harvest, the costing, the biology, the water tests.
  activity      everything that names it through a DYNAMIC link, which is where
                the farm's actual work is: Farm Task, Spray Application Block,
                Spray REI, Crop Observation, Pest Pressure, IPM Recommendation,
                Inspection Session, Accident Report.
  attachments   the Files filed against it. A File points at its parent BY
                DOCNAME, which is a string and not a link — Frappe's own
                link-integrity check cannot see it, so nothing but this would
                refuse, and the photographs would be left attached to a name
                nothing resolves. `convey_parcel` migrates these for exactly
                the same reason.

THE ACTIVITY CHECK IS THE ONE THAT EARNS THIS TOOL'S CAUTION. Every other
category is a Link that Frappe would have refused the delete over anyway; a
Dynamic Link is a pair of plain columns as far as the database is concerned, and
whether the framework refuses over one depends on a link map this app does not
control. So the check is made here, by name, and the tables below are asserted
against the shipped DocType JSON by `test_locations` — the same guard
`test_realestate` puts on `realestate.PARCEL_REFERRERS`, for the same reason: a
register added in a later release must not be able to arrive unnoticed and turn
a refusal into an orphan.

────────────────────────────────────────────────────────────────────────────
WHAT `update_farm_location` DOES NOT ACCEPT
────────────────────────────────────────────────────────────────────────────

The union of four registers' arguments is fifty-odd columns and would be a tool
nobody could read the description of. This takes the THIRTEEN A PERSON HAS AN
OPINION ABOUT AT A TAILGATE — what it is called out there, how big it is, what is
planted on it, what waters it, how many it sleeps, what condition it is in — and
an argument the chosen register has no column for is REFUSED BY NAME, naming the
registers that do take it and the per-register tool that takes the rest. Silently
dropping it is how a caller comes to believe they recorded something they did
not.

NEITHER TOOL RENAMES ANYTHING, and that is not an omission. All four registers
build their docname from the name column and all four `update_` tools refuse to
re-key for the same stated reason — every zone, assignment, task and spray record
holds the docname. A rename here would be a fifth implementation of the thing
those four say no to.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe

from .. import compat, locations
from ..args import as_bool, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import farm as farm_tools
from . import housing as housing_tools
from . import realestate as realestate_tools

FIELD = "Field"
IRRIGATION_ZONE = "Irrigation Zone"
PARCEL = "Parcel"
HOUSING_UNIT = "Housing Unit"
FILE = "File"

#: Square feet in an acre. The exact definition rather than a rounding, and the
#: same constant `api/mobile.SQ_FT_PER_ACRE` converts a created zone's acres
#: with — an Irrigation Zone's `area_acres` is COMPUTED from `area_sq_ft` by its
#: own controller, so acres are converted on the way in rather than set beside a
#: figure that would disagree with them.
SQ_FT_PER_ACRE = 43560

#: Most example docnames any refusal lists before it says "…". A refusal naming
#: forty spray records is one nobody reads to the end of; the COUNT is the fact
#: and the names are so somebody can go and look at one.
EXAMPLES = 8

#: Which registers hang off which, and the column they hang by.
#:
#: THE TREE IS THE ONE THING FRAPPE WOULD HAVE CAUGHT ANYWAY and it is checked
#: first regardless, because the sentence matters: "it has 4 irrigation zones on
#: it" is a thing a foreman can act on, and `LinkExistsError` is not.
#:
#: An `Irrigation Zone` and a `Housing Unit` are LEAVES — nothing in any register
#: hangs off either — which is why the two of them are the ones a duplicate can
#: usually be removed from without touching anything else.
CHILD_REGISTERS = {
	FIELD: (("Irrigation Zone", "field"),),
	IRRIGATION_ZONE: (),
	PARCEL: (("Field", "parcel"), ("Irrigation Zone", "parcel"), ("Housing Unit", "parcel")),
	HOUSING_UNIT: (),
}

#: Everything else this app ships that names one of the four with a PLAIN Link,
#: and the column it names it with. Asserted against the shipped DocType JSON by
#: `test_locations`, so a doctype added later cannot be forgotten quietly.
#:
#: `Parcel`'s row is deliberately the same set `realestate.PARCEL_REFERRERS`
#: carries MINUS the three registers, which are in `CHILD_REGISTERS` above — one
#: fact, split by what a person can do about it rather than copied.
STATIC_REFERRERS = {
	FIELD: (
		("Activity Cost Pool", "cost_object"),
		("Bin Seal", "field"),
		("Biological Asset", "field"),
		("Block Cost Entry", "field"),
		("Block Revenue Entry", "field"),
		# v0.118.0. The device sits in the block; the reading carries a COPY of
		# where its device sat when it was taken, which is why both are here and
		# neither is redundant — deleting a block has to reach the historical
		# readings as well as the hardware currently standing in it.
		("IoT Device", "field"),
		("IoT Reading", "field"),
		# v0.122.0. A monitoring measurement names the block it was taken in. An
		# FSMA record outlives the block it describes, so a block deleted without
		# reaching here leaves a food safety log pointing at ground that is gone —
		# and that log is the evidence an audit asks for.
		("Monitoring Record", "block"),
		("Planting Season", "field"),
		# v0.121.0. The series a satellite pull leaves behind and the cursor that
		# stops it being paid for twice. Both are keyed on the block, and a block
		# deleted without reaching them leaves a cursor claiming a backfill was
		# done for ground nobody farms.
		("Satellite Backfill Cursor", "field"),
		("Satellite Metric", "field"),
		("Scale Ticket", "field"),
		("Traceability Lot Code", "field"),
		("Water Test", "block"),
	),
	IRRIGATION_ZONE: (
		("Asset Register", "irrigation_zone"),
		("Water Test", "source"),
	),
	PARCEL: (
		("Biological Asset", "parcel"),
		("Housing Assignment", "parcel"),
		("Lease", "parcel"),
	),
	HOUSING_UNIT: (
		("Detector Test", "unit"),
		("Housing Assignment", "unit"),
		("Housing Inspection", "unit"),
	),
}

#: Every DYNAMIC link on this app's own doctypes: the docname column, and the
#: column beside it holding the doctype that docname is in. THE SAME TUPLE FOR
#: ALL FOUR REGISTERS, because that is what a dynamic link is — a pair of plain
#: columns that can name anything, checked by asking rather than by assuming.
#:
#: `Farm Task.parent_task` is in here and will never hold a location. It is here
#: because this table is DERIVED rather than curated: the test builds the same
#: set off the shipped JSON and compares, so an entry that cannot match costs one
#: query and an entry left out costs an orphaned record.
DYNAMIC_REFERRERS = (
	("Accident Report", "location", "location_doctype"),
	("Block Cost Entry", "reference_name", "reference_doctype"),
	("Block Revenue Entry", "reference_name", "reference_doctype"),
	("Change Management Log", "reference_name", "reference_doctype"),
	("Compliance Alert", "source_docname", "source_doctype"),
	("Crop Observation", "block", "block_doctype"),
	("Farm Task", "location", "location_doctype"),
	("Farm Task", "parent_task", "parent_doctype"),
	("Farm Task", "subject_docname", "subject_doctype"),
	("Inspection Session", "location", "location_doctype"),
	("IPM Recommendation", "block", "block_doctype"),
	("Pest Pressure", "block", "block_doctype"),
	("Signing Evidence", "document_name", "document_type"),
	("Spray Application Block", "block", "block_doctype"),
	("Spray REI", "block", "block_doctype"),
	("Trade Document", "source_name", "source_doctype"),
)

#: The one referring table that is a CHILD TABLE rather than a document. Its own
#: docname is a hash nobody can open, so an example from it names the Spray
#: Application it is a row of — which is the record somebody would actually go
#: and look at.
CHILD_TABLE_PARENT = {"Spray Application Block": "Spray Application"}


@dataclass(frozen=True)
class Register:
	"""One of the four: how to find a row in it, and how to change one."""

	doctype: str
	#: The argument this register's own tools take the record under.
	argument: str
	#: The Data column the docname is built from.
	name_field: str
	#: `farm.field_row` and the three like it — docname or bare name, both.
	row: object
	#: This register's own `update_` tool. The thing that actually writes.
	update: object
	#: The read that lists it, named for the refusals rather than called.
	list_tool: str
	#: This register's own update tool, by name, for the sentence that sends a
	#: caller to it when they wanted a column this door does not set.
	tool_name: str
	#: The common argument names `update_farm_location` accepts for this
	#: register, mapped to the argument its own tool calls them.
	editable: dict


#: `acres` IS NOT IN THE ZONE'S MAP UNDER ITS OWN NAME and that is the whole
#: reason this is a map rather than a pass-through. `update_irrigation_zone`
#: refuses `area_acres` by name — the controller computes it from `area_sq_ft` —
#: so acres are converted below and delivered as square feet.
REGISTERS = {
	FIELD: Register(
		doctype=FIELD,
		argument="field",
		name_field="field_name",
		row=farm_tools.field_row,
		update=farm_tools.update_field,
		list_tool="list_fields",
		tool_name="update_field",
		editable={
			"acres": "acreage",
			"crop": "crop",
			"variety": "variety",
			"block_number": "block_number",
			"condition": "condition",
			"notes": "notes",
		},
	),
	IRRIGATION_ZONE: Register(
		doctype=IRRIGATION_ZONE,
		argument="zone",
		name_field="zone_name",
		row=farm_tools.zone_row,
		update=farm_tools.update_irrigation_zone,
		list_tool="list_irrigation_zones",
		tool_name="update_irrigation_zone",
		editable={
			"acres": "area_sq_ft",
			"water_source": "water_source",
			"flow_rate_gpm": "flow_rate_gpm",
			"notes": "notes",
		},
	),
	PARCEL: Register(
		doctype=PARCEL,
		argument="parcel",
		name_field="parcel_name",
		row=realestate_tools.parcel_row,
		update=realestate_tools.update_parcel,
		list_tool="list_parcels",
		tool_name="update_parcel",
		editable={
			"acres": "acreage",
			"county": "county",
			"state": "state",
			"address": "address",
			"notes": "notes",
		},
	),
	HOUSING_UNIT: Register(
		doctype=HOUSING_UNIT,
		argument="unit",
		name_field="unit_name",
		row=housing_tools.unit_row,
		update=housing_tools.update_housing_unit,
		list_tool="list_housing_units",
		tool_name="update_housing_unit",
		editable={
			"unit_type": "unit_type",
			"capacity": "capacity",
			"condition": "condition",
			"notes": "notes",
		},
	),
}

#: Every common argument the pair accepts, and which registers take each. Built
#: from the four maps above rather than restated, so the refusal a caller gets
#: for a wrong one cannot come to name a register that stopped accepting it.
EDITABLE_BY_ARGUMENT = {
	argument: tuple(name for name in locations.REGISTERS if argument in REGISTERS[name].editable)
	for argument in sorted({key for register in REGISTERS.values() for key in register.editable})
}

#: The four `delete_farm_location` runs, in the order it reports them. Each has a
#: `force_check_<name>` argument that turns it off — see the module docstring for
#: what turning one off does and does not achieve.
DELETE_CHECKS = ("children", "references", "activity", "attachments")


# ── shared helpers ──────────────────────────────────────────────────────────
def _register(args: dict, verb: str) -> Register:
	"""One of the four, from `doctype` or `register`, or a refusal naming all four.

	CASE-INSENSITIVE ON THE WAY IN AND EXACT ON THE WAY OUT, the same call
	`api/mobile._location_register` makes for the create door and for the same
	reason: `field` and `Field` both arrive from real callers, and what this app
	then uses has to be the doctype's own spelling.
	"""
	given = as_str(args, "doctype") or as_str(args, "register")
	if not given:
		raise ToolError(
			"doctype is required — a place without its register is not a place, and the four "
			"registers hold genuinely different records. Pass one of: "
			+ ", ".join(locations.REGISTERS)
			+ f". list_farm_locations reports it on every row. Nothing was {verb}."
		)
	for name in locations.REGISTERS:
		if name.lower() == given.strip().lower():
			compat.require_doctype(name, "It ships with erpnext_mcp — run `bench migrate`.")
			return REGISTERS[name]
	raise ToolError(
		f"{given!r} is not one of the four registers a location lives in. They are: "
		+ ", ".join(locations.REGISTERS)
		+ f". Nothing was {verb}."
	)


def _row(register: Register, args: dict, verb: str) -> dict:
	"""The record itself, by docname or by the name somebody typed.

	`owning_entity` and its alias `company` narrow a bare name to one entity, the
	same pair every tool in the four registers takes. The resolver raises its own
	sentence when the name matches two rows, which is the answer a caller can act
	on — this only adds which tool was refused.
	"""
	name = as_str(args, "name") or as_str(args, register.argument) or as_str(args, "location")
	if not name:
		raise ToolError(
			f"name is required — the {register.doctype} to change. Pass the docname, or the name "
			f"somebody typed; both resolve. {register.list_tool} and list_farm_locations both "
			f"have the register. Nothing was {verb}."
		)
	company = resolve_company(as_str(args, "owning_entity") or as_str(args, "company")) or ""
	return dict(register.row(name, company=company) if company else register.row(name))


def _option(register: Register, row: dict) -> dict:
	"""The picker row for a record, so an answer names what the phone will send back."""
	return locations.option(register.doctype, dict(row))


def _examples(doctype: str, filters: dict) -> tuple:
	"""`(count, [docnames])` for one referring register. Never raises.

	A doctype this site does not have contributes nothing rather than failing the
	call — the same rule `api/mobile._location_rows` applies to a register that
	was never installed, because a farm without a Biological Asset register
	should get a delete that works, not an error about an app it does not run.
	"""
	if not compat.doctype_exists(doctype):
		return 0, []
	column = "parent" if doctype in CHILD_TABLE_PARENT else "name"
	try:
		total = int(frappe.db.count(doctype, filters) or 0)
		if not total:
			return 0, []
		names = frappe.db.get_all(doctype, filters=filters, pluck=column, limit=EXAMPLES) or []
	except Exception:  # pragma: no cover - a site shaping one of these differently
		return 0, []
	return total, [str(name) for name in names if name]


def _listing(found: list) -> str:
	"""`"Spray Application X, Farm Task Y, …"` — what a refusal shows of a check."""
	shown = []
	for entry in found:
		label = CHILD_TABLE_PARENT.get(entry["doctype"], entry["doctype"])
		for name in entry["examples"]:
			shown.append(f"{label} {name}")
	listing = ", ".join(shown[:EXAMPLES])
	return listing + (", …" if sum(entry["count"] for entry in found) > len(shown[:EXAMPLES]) else "")


def _scan(pairs, docname: str) -> list:
	"""Static-link referrers holding this docname, as `{doctype, field, count, examples}`."""
	found = []
	for doctype, fieldname in pairs:
		if not compat.doctype_exists(doctype) or not compat.has_field(doctype, fieldname):
			continue
		count, examples = _examples(doctype, {fieldname: docname})
		if count:
			found.append({"doctype": doctype, "field": fieldname, "count": count, "examples": examples})
	return found


def _scan_dynamic(register: str, docname: str) -> list:
	"""Dynamic-link referrers holding this docname AND naming this register.

	BOTH COLUMNS, ALWAYS. Filtering on the docname alone would count a Farm Task
	whose location is a Housing Unit that happens to share a name with a Field,
	and refusing a delete over somebody else's record is as wrong as allowing one.
	"""
	found = []
	for doctype, name_field, doctype_field in DYNAMIC_REFERRERS:
		if not compat.doctype_exists(doctype):
			continue
		if not compat.has_field(doctype, name_field) or not compat.has_field(doctype, doctype_field):
			continue
		count, examples = _examples(doctype, {name_field: docname, doctype_field: register})
		if count:
			found.append(
				{
					"doctype": doctype,
					"field": name_field,
					"doctype_field": doctype_field,
					"count": count,
					"examples": examples,
				}
			)
	return found


def _scan_attachments(register: str, docname: str) -> list:
	"""Files filed against this record. A string pair, which no link check sees."""
	count, examples = _examples(FILE, {"attached_to_doctype": register, "attached_to_name": docname})
	if not count:
		return []
	return [{"doctype": FILE, "field": "attached_to_name", "count": count, "examples": examples}]


def _total(found: list) -> int:
	return sum(int(entry["count"]) for entry in found)


# ── update_farm_location ────────────────────────────────────────────────────
def update_farm_location(args: dict) -> ToolResult:
	"""Change one place in whichever register it is in. One door, four writes.

	THE REGISTER'S OWN TOOL IS WHAT RUNS. `update_field`, `update_irrigation_zone`,
	`update_parcel` and `update_housing_unit` keep every refusal they have: the
	parcel acreage rule, the zone-number clash, the block's zones summing past its
	acreage, the derived `organic_certified`, the GPS pair that moves together.
	Nothing here relaxes any of it, and nothing here duplicates any of it either —
	this resolves the register, maps thirteen argument names onto four vocabularies
	and delegates.

	AN ARGUMENT THE CHOSEN REGISTER HAS NO COLUMN FOR IS REFUSED BY NAME rather
	than dropped, and the refusal says which registers do take it. `capacity` on a
	block and `crop` on a cabin are both somebody working from the wrong screen,
	and a silent drop is how they come to believe they recorded it.
	"""
	register = _register(args, "changed")
	row = _row(register, args, "changed")
	docname = str(row.get("name") or "")

	inner: dict = {register.argument: docname}
	written = []
	for argument, value in sorted(args.items()):
		if argument in ("doctype", "register", "name", "location", "owning_entity", "company"):
			continue
		if argument == register.argument:
			continue
		if argument not in EDITABLE_BY_ARGUMENT:
			raise ToolError(
				f"{argument!r} is not something this door sets. It takes the ones a person has an "
				"opinion about standing in a block: "
				+ ", ".join(EDITABLE_BY_ARGUMENT)
				+ f". Everything else a {register.doctype} carries is on {register.tool_name}, "
				"which is where the columns that reach a financial statement or a compliance "
				"answer live. Nothing was changed."
			)
		if argument not in register.editable:
			takers = EDITABLE_BY_ARGUMENT[argument]
			raise ToolError(
				f"a {register.doctype} has no {argument!r}. That argument belongs to "
				+ (", ".join(takers) if takers else "no register on this site")
				+ ". Check the register before the column — this is usually the right value on "
				"the wrong kind of place. Nothing was changed."
			)
		if value in (None,):
			continue
		inner[register.editable[argument]] = _convert(register, argument, value)
		written.append(argument)

	if not written:
		raise ToolError(
			f"update_farm_location was given nothing to change about {docname}. Pass at least one "
			"of: "
			+ ", ".join(sorted(register.editable))
			+ f" — those are the ones a {register.doctype} carries. Nothing was changed."
		)

	result = register.update(inner)
	data = dict(result.data)
	return ToolResult(
		data={
			**data,
			"doctype": register.doctype,
			"location_type": register.doctype,
			"location": docname,
			# READ BACK RATHER THAN MERGED. The register's own tool may derive
			# columns on save — a zone's acres from its square feet, a unit's
			# lawful occupancy from its floor area — and a picker row assembled
			# from what was SENT would show the figure the caller typed instead
			# of the one that is now stored.
			"option": _option(register, register.row(docname)),
			"arguments_mapped": {argument: register.editable[argument] for argument in written},
			"note": (
				f"{register.tool_name} did the write, with every check it has always made. "
				"This door resolved the register and mapped the argument names; it relaxed "
				"nothing and it cannot rename anything — all four registers build their docname "
				"from the name column, and every zone, assignment and filed record holds it."
			),
		},
		summary=result.summary or f"updated {register.doctype} {docname}",
		docstatus_delta=result.docstatus_delta or "0 → 0 (amended)",
	)


def _convert(register: Register, argument: str, value):
	"""One common argument in the units the register's own tool wants.

	The only conversion there is, and it is the zone's. See `SQ_FT_PER_ACRE`: an
	`Irrigation Zone` computes `area_acres` from `area_sq_ft` and refuses the
	former by name, so acres given here become square feet rather than a second
	independently settable figure that would disagree with the first.
	"""
	if register.doctype != IRRIGATION_ZONE or argument != "acres":
		return value
	if value in (None, ""):
		return value
	try:
		measured = float(value)
	except (TypeError, ValueError):
		raise ToolError(f"acres must be a number, got {value!r}. Nothing was changed.") from None
	if measured < 0:
		raise ToolError(f"acres must be zero or more, got {measured}. Nothing was changed.")
	return round(measured * SQ_FT_PER_ACRE, 4)


# ── delete_farm_location ────────────────────────────────────────────────────
def delete_farm_location(args: dict) -> ToolResult:
	"""Remove one place from one register, once nothing at all depends on it.

	THE ONLY IRREVERSIBLE THING THIS APP DOES TO A REGISTER, and the only reason
	it exists is the duplicate: a block typed twice, one of the two never used,
	sitting in every picker on the farm because nothing could take it out. A row
	with any history is the other case and it is refused — there is no `disabled`
	column to hide behind on any of the four, which is exactly why the checks are
	strict rather than advisory.

	FOUR CHECKS, EVERY ONE A REFUSAL, ALL RUN BEFORE ANYTHING IS DELETED so a
	caller gets every reason in one answer. See the module docstring for what each
	covers and for why the `activity` check is the one that earns the caution.

	`dry_run` RUNS ALL FOUR AND DELETES NOTHING, which is the call to make first:
	it names the count and eight examples per referring register, so "why can I
	not remove this" is answered without a failed write.
	"""
	register = _register(args, "deleted")
	row = _row(register, args, "deleted")
	docname = str(row.get("name") or "")
	before = _option(register, row)
	dry_run = bool(as_bool(args, "dry_run", False))

	wanted = {check: bool(as_bool(args, f"force_check_{check}", True)) for check in DELETE_CHECKS}
	skipped = sorted(check for check, on in wanted.items() if not on)

	passed: dict = {}
	reports: dict = {}
	blockers: list = []

	if wanted["children"]:
		found = _scan(CHILD_REGISTERS[register.doctype], docname)
		reports["children"] = found
		if found:
			blockers.append(
				f"{_total(found)} register row(s) hang off it — {_listing(found)}. Deleting it "
				"would orphan them, and a place whose parent is gone is one nobody can say the "
				"location of. Remove or re-register the children first."
			)
		elif CHILD_REGISTERS[register.doctype]:
			passed["children"] = "nothing in any register hangs off it"
		else:
			passed["children"] = f"a {register.doctype} is a leaf — no register hangs off one"

	if wanted["references"]:
		found = _scan(STATIC_REFERRERS[register.doctype], docname)
		reports["references"] = found
		if found:
			blockers.append(
				f"{_total(found)} record(s) name it — {_listing(found)}. Every one of those is a "
				"row whose ground would stop resolving to anything. Repoint them first, or keep "
				"the register entry: a place with history is what the register is FOR."
			)
		else:
			passed["references"] = "no record on this site links to it"

	if wanted["activity"]:
		found = _scan_dynamic(register.doctype, docname)
		reports["activity"] = found
		if found:
			blockers.append(
				f"{_total(found)} record(s) of WORK name it — {_listing(found)}. A task, a spray "
				"record, an observation or an inspection filed against this place is the evidence "
				"an auditor reads; deleting the place leaves each of them printing a name that "
				"resolves to nothing. This is the check Frappe's own link integrity does not "
				"make, because a dynamic link is two plain columns to a database."
			)
		else:
			passed["activity"] = "no task, spray record, observation or inspection names it"

	if wanted["attachments"]:
		found = _scan_attachments(register.doctype, docname)
		reports["attachments"] = found
		if found:
			blockers.append(
				f"{_total(found)} file(s) are attached to it — {_listing(found)}. A File names its "
				"parent by DOCNAME rather than by link, so nothing else would have refused and the "
				"photographs would simply stop resolving. Move or remove them first."
			)
		else:
			passed["attachments"] = "nothing is attached to it"

	data = {
		"deleted": None,
		"doctype": register.doctype,
		"location_type": register.doctype,
		"location": docname,
		"location_row": before,
		"checks_passed": passed,
		"checks_skipped": skipped,
		"found": {key: value for key, value in reports.items() if value},
		"dry_run": dry_run,
		"blockers": blockers,
	}

	if blockers:
		if dry_run:
			data["note"] = (
				f"{docname} cannot be deleted yet: "
				+ "; ".join(f"({index}) {reason}" for index, reason in enumerate(blockers, start=1))
				+ " Nothing was deleted — this was a dry run, and it would have refused."
			)
			return ToolResult(
				data,
				f"dry run: {register.doctype} {docname} is NOT deletable ({len(blockers)} blocker(s))",
			)
		raise ToolError(
			f"{docname} cannot be deleted: "
			+ "; ".join(f"({index}) {reason}" for index, reason in enumerate(blockers, start=1))
			+ " Nothing was deleted."
		)

	if not dry_run:
		frappe.delete_doc(register.doctype, docname)
		data["deleted"] = docname

	data["note"] = (
		f"Gone. There is no undo, no draft and no cancelled state — the {register.doctype} "
		"register has no disabled column to hide a row behind, which is why this ran "
		f"{len(passed)} check(s) first. The name {docname!r} is free for another record."
		if not dry_run
		else f"Nothing was deleted. All {len(passed)} check(s) passed, so the same call "
		"without dry_run would remove it."
	) + (
		f" force_check_{', force_check_'.join(skipped)} was turned off, so this app did not "
		"run that check. Frappe's own link-integrity check still runs on the delete and will "
		"refuse a plain Link it can see — the flag changes which error you get, not the "
		"outcome. It sees NO dynamic link, so turning off force_check_activity is the one "
		"that genuinely removes a protection."
		if skipped
		else ""
	)
	return ToolResult(
		data,
		(
			f"dry run: {register.doctype} {docname} is deletable ({len(passed)} check(s) passed)"
			if dry_run
			else f"deleted {register.doctype} {docname} ({len(passed)} safety check(s) passed"
			+ (f", {len(skipped)} skipped" if skipped else "")
			+ ")"
		),
		docstatus_delta="" if dry_run else "existed → deleted",
	)
