# SPDX-License-Identifier: MIT
"""Domains, preset profiles and counts for the tool-permission console.

WHY THIS MODULE EXISTS. "ERPNext MCP Settings" carries one Check field per tool,
and there are now seven hundred and fifty-seven of them. Every one is a real
control an operator has a real reason to want — that is the promise the app
makes about what an AI client can reach — but a form of seven hundred and fifty
seven checkboxes is a form nobody configures. They scroll to the section whose
name they recognise, tick two boxes and leave the other seven hundred at
whatever the release shipped, which means the switches stop being a decision and
become a default.

So this module gives that surface three things it did not have: a coarse
GROUPING an operator can think in, PRESET PROFILES that set a whole working
configuration in one action, and COUNTS so the page can say what is actually on
before anybody scrolls.

WHAT IS DERIVED AND WHAT IS DECLARED, because the difference is the whole
maintenance story:

  DERIVED   which section each tool's switch sits in. Read out of the shipped
            DocType JSON's `field_order`, so a tool added to a section is in
            that section here on the same commit, with nothing to update. There
            is no second copy of the catalogue in this file and there must never
            be one — `registry.py` and the settings JSON are the two registers
            that already have to agree, and a third would be a third thing to
            forget.

  DECLARED  which DOMAIN each section rolls up into. A hundred and five sections
            is barely better than seven hundred switches, and no rule reliably
            turns "Kairotic Compliance Calendar" into "Compliance" — so the
            rollup is a table, and `test_tool_groups` fails the build if a
            section holding a tool switch is missing from it. A new section
            cannot arrive without somebody deciding which domain it belongs to.

WHY THE JSON RATHER THAN `frappe.get_meta`. Real Frappe orders `meta.fields` by
`idx`, which follows `field_order`; the standalone test double orders them by
the JSON's own `fields` array, which is a DIFFERENT order. A "walk the fields
and remember the last Section Break" written against meta would therefore group
tools one way in the suite and another way on the bench, and every test would
pass. Reading `field_order` off disk is the same answer in both places.

WHAT A PROFILE DOES AND DOES NOT DO. Applying one writes EVERY tool switch —
tools in the profile's domains on, everything else off — because a preset that
only ever adds is not a configuration, it is a ratchet, and after three clicks
an operator has a superset of all three profiles and no idea what is live. What
it does not do is enable write tools by default: a profile names its write
domains separately and explicitly, and `apply_profile` reports every mutating
tool it turned on. See `Profile.writes`.

THE MASTER SWITCH, THE TOKEN, THE CIDRS AND THE PACKET TYPES ARE NEVER TOUCHED.
A profile is about which tools exist for a client, not about whether the
endpoint answers, who may reach it, or which compliance packets it may build.
Nothing here can turn the endpoint on.
"""

import json
import os

import frappe
from frappe import _

from . import registry, settings

#: The shipped DocType JSON. Read rather than `frappe.get_meta`-ed — see the
#: module docstring for why the difference is load-bearing.
SETTINGS_JSON_PATH = os.path.join(
	os.path.dirname(os.path.abspath(__file__)),
	"erpnext_mcp",
	"doctype",
	"erpnext_mcp_settings",
	"erpnext_mcp_settings.json",
)

#: The switch-name prefix. One place, because three functions here slice it off.
SWITCH_PREFIX = "allow_"


class Domain:
	"""One coarse grouping of tool switches, as an operator thinks about them."""

	__slots__ = ("description", "key", "label")

	def __init__(self, key: str, label: str, description: str):
		self.key = key
		self.label = label
		self.description = description

	def as_dict(self) -> dict:
		return {"key": self.key, "label": self.label, "description": self.description}


#: The domains, in the order the console draws them. Seven, deliberately: few
#: enough to read as a row of chips on one line, coarse enough that an operator
#: picking one is making a decision about their operation rather than about this
#: app's internal module boundaries.
DOMAINS = (
	Domain(
		"farm",
		"Farm Operations",
		"The work in the orchard: dispatch, spraying, irrigation, harvest capture, "
		"asset tags and the weather behind them.",
	),
	Domain(
		"workforce",
		"HR & Payroll",
		"People: hiring paperwork, training, shifts, wages, withholding and what the payroll run produces.",
	),
	Domain(
		"compliance",
		"Compliance & Safety",
		"The registers an inspection asks for: evidence, the alert calendar, the "
		"rules behind it, incidents and audit packets.",
	),
	Domain(
		"accounting",
		"Accounting & Finance",
		"The ledger and everything that reconciles to it: accounts, banking, "
		"budgets, costing, tax forms and the KPI framework.",
	),
	Domain(
		"commerce",
		"Buying, Selling & Inventory",
		"Purchase invoices, sales and settlements, stock movements and the trade "
		"documents a load travels with.",
	),
	Domain(
		"holding",
		"Assets, Property & Governance",
		"What the business owns and the paper that says so: the cap table, "
		"governance documents, leases, depreciation and disclosure.",
	),
	Domain(
		"platform",
		"Platform & Administration",
		"Running the system itself: attachments, uploads, master data, mobile "
		"accounts, translations and the model registry.",
	),
)

DOMAIN_KEYS = tuple(domain.key for domain in DOMAINS)
DOMAIN_BY_KEY = {domain.key: domain for domain in DOMAINS}

#: Section fieldname → domain key, for every section of the settings form that
#: holds at least one tool switch.
#:
#: TOTALITY IS ASSERTED, not hoped for: `test_tool_groups` walks the shipped JSON
#: and fails if a section holding a tool switch is absent here. That is the check
#: that makes a hand-written table safe — the alternative, a default bucket for
#: anything unrecognised, would quietly file next year's release under "other"
#: and nobody would ever look.
SECTION_DOMAIN = {
	# ── the orchard ──────────────────────────────────────────────────────
	"farm_tools_section": "farm",
	# v0.116.0. The operational map overlays and the soil book behind their
	# compaction colours file under `farm` beside the registers they read — the
	# blocks, the zones and the valves are all here, and an operator looking for
	# "why is this block red" should find the map switch in the same chip as the
	# register it is drawn over rather than under Compliance because two of the
	# five layers happen to be regulated windows.
	"map_overlay_section": "farm",
	"geo_tools_section": "farm",
	"irrigation_valve_section": "farm",
	"agronomy_section": "farm",
	"spray_program_section": "farm",
	"crop_protection_section": "farm",
	"block_lifecycle_section": "farm",
	"dispatch_read_section": "farm",
	"task_templates_section": "farm",
	"fieldwork_read_section": "farm",
	"fieldwork_write_section": "farm",
	"bucket_log_read_section": "farm",
	"bucket_log_write_section": "farm",
	"fill_pipeline_read_section": "farm",
	"fill_pipeline_write_section": "farm",
	"bin_seal_read_section": "farm",
	"bin_seal_write_section": "farm",
	# v0.111.0. The FSMA 204 lot register files under `farm` with every other
	# trace tool — trace_forward, trace_backward and trace_bin are all here, and
	# an operator hunting for a recall switch should find all five in one chip
	# rather than two under Farm and three under Compliance.
	"traceability_lot_read_section": "farm",
	"traceability_lot_write_section": "farm",
	"receipts_section": "farm",
	"weather_section": "farm",
	"asset_tag_section": "farm",
	"usda_market_section": "farm",
	# ── the crew ─────────────────────────────────────────────────────────
	"hr_tools_section": "workforce",
	"org_master_section": "workforce",
	"org_master_write_section": "workforce",
	"training_section": "workforce",
	"i9_section": "workforce",
	"signers_section": "workforce",
	"w4_section": "workforce",
	"state_tax_section": "workforce",
	"payroll_section": "workforce",
	"payroll_deduction_section": "workforce",
	"garnishment_section": "workforce",
	"wage_tables_section": "workforce",
	"direct_deposit_section": "workforce",
	"shift_section": "workforce",
	"housing_tools_section": "workforce",
	"family_tools_section": "workforce",
	# ── what an inspection asks for ──────────────────────────────────────
	"packet_tools_section": "compliance",
	"compliance_schema_section": "compliance",
	"evidence_read_section": "compliance",
	"evidence_write_section": "compliance",
	"calendar_section": "compliance",
	"evidence_section": "compliance",
	"signed_documents_section": "compliance",
	"records_read_section": "compliance",
	"records_write_section": "compliance",
	"inspection_session_section": "compliance",
	"compliance_rule_section": "compliance",
	"regulation_feed_section": "compliance",
	"visit_section": "compliance",
	"v79_section": "compliance",
	"audit_packet_section": "compliance",
	"document_intel_read_section": "compliance",
	"document_intel_write_section": "compliance",
	# ── the ledger ───────────────────────────────────────────────────────
	"read_tools_section": "accounting",
	"mutating_tools_section": "accounting",
	"chart_tools_section": "accounting",
	"fiscal_tools_section": "accounting",
	"dimension_tools_section": "accounting",
	"report_tools_section": "accounting",
	"document_tools_section": "accounting",
	"notes_tools_section": "accounting",
	"banking_bridge_read_section": "accounting",
	"banking_bridge_write_section": "accounting",
	"bank_consolidation_read_section": "accounting",
	"bank_consolidation_write_section": "accounting",
	"budget_read_section": "accounting",
	"budget_write_section": "accounting",
	"kpi_read_section": "accounting",
	"kpi_write_section": "accounting",
	"abc_section": "accounting",
	"breakeven_section": "accounting",
	"ipo_phase1_section": "accounting",
	"ipo_phase23_section": "accounting",
	"expense_receipt_section": "accounting",
	"payroll_gl_section": "accounting",
	"tax_form_section": "accounting",
	"tax_remittance_section": "accounting",
	"drift_section": "accounting",
	# ── in and out ───────────────────────────────────────────────────────
	"trade_tools_section": "commerce",
	"purchasing_tools_section": "commerce",
	"sales_tools_section": "commerce",
	"stock_tools_section": "commerce",
	"trade_section": "commerce",
	# ── what the family owns ─────────────────────────────────────────────
	"governance_tools_section": "holding",
	"archive_tools_section": "holding",
	"asset_tools_section": "holding",
	"realestate_tools_section": "holding",
	"party_tools_section": "holding",
	"company_tools_section": "holding",
	"ipo_phase456_section": "holding",
	# ── running the thing ────────────────────────────────────────────────
	# Frappe's own workflow engine, not a farm process — the tools read and
	# advance whatever workflow a site has attached to whatever doctype, so the
	# domain is "running the system" rather than any one register above.
	"workflow_tools_section": "platform",
	"attachment_tools_section": "platform",
	"printing_tools_section": "platform",
	"upload_tools_section": "platform",
	"collab_tools_section": "platform",
	"master_data_tools_section": "platform",
	"meta_tools_section": "platform",
	"ml_model_read_section": "platform",
	"ml_model_write_section": "platform",
	"translation_section": "platform",
	"shadow_log_section": "platform",
	"mobile_read_section": "platform",
	"mobile_write_section": "platform",
	"push_notification_section": "platform",
	# v0.118.0, Farm App Retirement Cycle 1. The IoT network files under `farm`
	# beside the blocks and zones its devices sit in — a soil probe is a fact
	# about a block, and an operator looking for "why is this block's moisture
	# stale" should find the device switch in the same chip as the block
	# register.
	"iot_read_section": "farm",
	"iot_write_section": "farm",
	# Residue limits file under `compliance` rather than `farm`, because the
	# question an MRL answers is not "what may I spray" — it is "may this load
	# cross this border", which is the same kind of question as a trade document
	# or an inspection record. `get_ipm_reference` sits in the read section with
	# them rather than in a section of its own: it is the book somebody consults
	# WHILE deciding whether a spray will clear a destination, and a switch on
	# its own chip would be found by nobody.
	"mrl_read_section": "compliance",
	"mrl_write_section": "compliance",
	# The competitive picture and the written strategy file under `holding` with
	# governance and the cap table: they are decisions the OWNERS make about what
	# the business is, not work anybody does in the orchard.
	"competitive_intel_read_section": "holding",
	"competitive_intel_write_section": "holding",
	"strategy_read_section": "holding",
	"strategy_write_section": "holding",
}


class Profile:
	"""A named working configuration: which domains a client may see, and write.

	`reads` and `writes` are both domain keys, and `writes` is a SUBSET of
	`reads` by construction — a profile that could write a register it cannot
	read would be a configuration nobody meant. `validate()` asserts it rather
	than fixing it up, because the fix-up would hide a typo in the table below.
	"""

	__slots__ = ("key", "label", "reads", "summary", "writes")

	def __init__(self, key: str, label: str, summary: str, reads: tuple, writes: tuple = ()):
		self.key = key
		self.label = label
		self.summary = summary
		self.reads = tuple(reads)
		self.writes = tuple(writes)

	def covers(self, domain: str, mutating: bool) -> bool:
		"""Whether a tool in `domain` is switched on by this profile."""
		if mutating:
			return domain in self.writes
		return domain in self.reads

	def as_dict(self) -> dict:
		return {
			"key": self.key,
			"label": self.label,
			"summary": self.summary,
			"reads": list(self.reads),
			"writes": list(self.writes),
			"read_domain_labels": [DOMAIN_BY_KEY[key].label for key in self.reads],
			"write_domain_labels": [DOMAIN_BY_KEY[key].label for key in self.writes],
		}


#: The shipped profiles.
#:
#: EVERY ONE OF THEM IS A STARTING POINT AND SAYS SO. A profile is a hundred
#: decisions taken at once, which is worth having precisely because the
#: alternative is a hundred decisions never taken — but an operator who applies
#: "Bookkeeper" and then ticks four more boxes has done the right thing, and
#: nothing here should make that feel like fighting the form.
#:
#: WRITE DOMAINS ARE SPARSE ON PURPOSE. "Mutating tools ship off" is one of this
#: app's load-bearing promises (see `registry.DEFAULT_ON_MUTATING_TOOLS`), and a
#: preset that quietly handed an AI client three hundred write tools would end
#: that promise while looking like a convenience. Only the two profiles whose
#: whole job is to record what happened in the orchard carry a write domain, and
#: `apply_profile` names every mutating tool it enabled in its answer.
PROFILES = (
	Profile(
		"farm_manager",
		"Farm Manager",
		"The whole operation: the orchard, the crew, the compliance registers behind "
		"both, and read access to the books. Writes the farm's own records.",
		reads=("farm", "workforce", "compliance", "commerce", "accounting", "platform"),
		writes=("farm",),
	),
	Profile(
		"foreman",
		"Foreman",
		"The board and the block. Dispatch, shifts, the compliance calendar that "
		"raises the work, and the records that close it.",
		reads=("farm", "compliance", "workforce"),
		writes=("farm",),
	),
	Profile(
		"field_worker",
		"Field Worker",
		"The narrowest useful configuration: the task pool, the job in hand and the "
		"evidence that closes it. Nothing about anybody's pay.",
		reads=("farm",),
		writes=(),
	),
	Profile(
		"bookkeeper",
		"Bookkeeper",
		"The books and what feeds them: the ledger, banking, purchasing and sales, "
		"payroll output and the tax forms. Read-only until you say otherwise.",
		reads=("accounting", "commerce", "workforce", "platform"),
		writes=(),
	),
	Profile(
		"compliance_officer",
		"Compliance Officer",
		"The registers an audit asks for, the calendar that schedules them, the rules "
		"that generate it, and the training and hiring paperwork behind the crew.",
		reads=("compliance", "workforce", "farm"),
		writes=(),
	),
	Profile(
		"owner",
		"Owner / Family",
		"What the family owns and how the operation is doing: the cap table, "
		"governance paper, leases, the ledger and the KPI framework.",
		reads=("holding", "accounting", "compliance", "commerce"),
		writes=(),
	),
	Profile(
		"read_only",
		"Read Only — Everything",
		"Every read tool in the app, every write tool off. The configuration to hand "
		"an assistant you want to be able to ask anything and change nothing.",
		reads=DOMAIN_KEYS,
		writes=(),
	),
	Profile(
		"minimal",
		"Nothing Enabled",
		"Every tool switch off. A starting point for building a configuration by "
		"hand, and the fastest way to take a client's whole surface away without "
		"touching the master switch.",
		reads=(),
		writes=(),
	),
)

PROFILE_BY_KEY = {profile.key: profile for profile in PROFILES}


def _load_field_order() -> list:
	with open(SETTINGS_JSON_PATH, encoding="utf-8") as handle:
		payload = json.load(handle)
	by_name = {field["fieldname"]: field for field in payload["fields"]}
	return [(name, by_name[name]) for name in payload["field_order"] if name in by_name]


_SECTION_OF_TOOL = None


def section_of_tool() -> dict:
	"""`{tool name: section fieldname}` for every tool that has a switch.

	Cached at first use. The file is shipped with the app and cannot change
	between requests on a running bench, so re-reading it per call would be a
	disk hit to learn the same thing.
	"""
	global _SECTION_OF_TOOL
	if _SECTION_OF_TOOL is None:
		out = {}
		section = ""
		for fieldname, field in _load_field_order():
			if field.get("fieldtype") == "Section Break":
				section = fieldname
			elif fieldname.startswith(SWITCH_PREFIX):
				out[fieldname[len(SWITCH_PREFIX) :]] = section
		_SECTION_OF_TOOL = out
	return _SECTION_OF_TOOL


def domain_of(switch_name: str) -> str:
	"""Which domain a switch belongs to, or "" for a name this form does not carry.

	IT ANSWERS FOR ANY `allow_` SWITCH, NOT ONLY FOR TOOLS. The two compliance
	packet types carry one each and are not tools — see
	`test_settings.test_every_switch_has_a_tool_or_a_packet_type` — and the
	console has to be able to file them under a domain like everything else, or
	they vanish from the form the moment somebody clicks a chip.

	"" rather than a fallback domain, and the caller decides what to do with it.
	A tool with no switch cannot exist (`test_every_tool_has_a_switch` fails the
	build over one), so "" means the argument names nothing on this form.
	"""
	return SECTION_DOMAIN.get(section_of_tool().get(switch_name, ""), "")


def tools_in_domain(domain_key: str) -> tuple:
	"""Every tool in one domain, in catalogue order."""
	return tuple(name for name in registry.TOOLS if domain_of(name) == domain_key)


def non_tool_switches() -> dict:
	"""The `allow_` switches that are not tools — today, the two packet types.

	The console needs them so its filter can file them under a domain instead of
	hiding them behind every chip, and the COUNTS need them excluded, because
	"412 of 757 tools" has to mean tools. Two maps rather than one union for
	exactly that reason.
	"""
	return {
		name: {"domain": SECTION_DOMAIN.get(section, ""), "mutating": False}
		for name, section in section_of_tool().items()
		if name not in registry.TOOLS
	}


def summary() -> dict:
	"""How many tools are on, in total and per domain, with the writes called out.

	THE WRITE COUNT IS SEPARATE EVERYWHERE IT APPEARS. "412 of 757 enabled" and
	"412 of 757 enabled, 3 of them write" are different sentences to put in front
	of an operator, and only the second one lets them stop reading.
	"""
	rows = []
	total = enabled = writes_total = writes_enabled = 0
	for domain in DOMAINS:
		names = tools_in_domain(domain.key)
		on = [name for name in names if settings.tool_enabled(name)]
		writing = [name for name in names if registry.TOOLS[name]["mutating"]]
		writing_on = [name for name in writing if settings.tool_enabled(name)]
		total += len(names)
		enabled += len(on)
		writes_total += len(writing)
		writes_enabled += len(writing_on)
		rows.append(
			{
				**domain.as_dict(),
				"total": len(names),
				"enabled": len(on),
				"writes_total": len(writing),
				"writes_enabled": len(writing_on),
			}
		)
	return {
		"total": total,
		"enabled": enabled,
		"writes_total": writes_total,
		"writes_enabled": writes_enabled,
		"domains": rows,
	}


def plan_profile(profile_key: str) -> dict:
	"""What applying a profile would do, without doing it.

	Returned by `apply_profile(dry_run=1)` and shown in the confirmation dialog,
	because "this will turn 300 things off" is the one fact an operator needs
	before pressing a button labelled with somebody's job title.
	"""
	profile = PROFILE_BY_KEY.get(profile_key)
	if profile is None:
		frappe.throw(
			_("{0} is not a profile. The {1} are: {2}.").format(
				profile_key, len(PROFILES), ", ".join(item.key for item in PROFILES)
			),
			title=_("Unknown Profile"),
		)

	turning_on, turning_off, unchanged, writes_on = [], [], [], []
	for name, spec in registry.TOOLS.items():
		wanted = profile.covers(domain_of(name), bool(spec["mutating"]))
		if wanted and spec["mutating"]:
			writes_on.append(name)
		if wanted == settings.tool_enabled(name):
			unchanged.append(name)
		elif wanted:
			turning_on.append(name)
		else:
			turning_off.append(name)
	return {
		"profile": profile.as_dict(),
		"enabling": turning_on,
		"disabling": turning_off,
		"unchanged": len(unchanged),
		"write_tools_enabled": writes_on,
		"total": len(registry.TOOLS),
		# What the form will read afterwards, stated here so the confirmation
		# dialog can say "412 of 757" before anything is written rather than
		# after. It is the profile's own arithmetic — every tool it covers —
		# and not a subtraction from the current state.
		"will_be_enabled": len(turning_on) + len([name for name in unchanged if settings.tool_enabled(name)]),
	}


@frappe.whitelist()
def console() -> dict:
	"""Everything the settings form's tool console needs, in one call.

	System-Manager-only, like every other helper this form calls: the doctype's
	own permissions already gate the page, and `frappe.only_for` makes the method
	safe on its own rather than safe because of where it is called from.

	IT SENDS THE TOOL→DOMAIN MAP RATHER THAN LETTING THE BROWSER DERIVE IT. The
	form could walk its own `meta.fields` and remember the last Section Break —
	and then the grouping would live in two places written in two languages, and
	`test_settings.SettingsFormJS` exists because that arrangement has already
	shipped a form that lied about which tools were write tools. Seven hundred
	short entries is around thirty kilobytes on a page an operator opens
	deliberately, once.
	"""
	frappe.only_for("System Manager")
	return {
		"domains": [domain.as_dict() for domain in DOMAINS],
		"profiles": [profile.as_dict() for profile in PROFILES],
		"tools": {
			name: {
				"domain": domain_of(name),
				"mutating": bool(spec["mutating"]),
				"available": registry.is_available(name),
			}
			for name, spec in registry.TOOLS.items()
		},
		"switches": non_tool_switches(),
		"summary": summary(),
	}


@frappe.whitelist()
def apply_profile(profile: str, dry_run=0) -> dict:
	"""Set every tool switch to what one profile says. System-Manager-only.

	IT WRITES THE WHOLE SURFACE, not just the additions — see the module
	docstring. The document is saved once, so `track_changes` records the change
	as a single Version row an operator can read afterwards and revert from,
	which is the audit trail this needed and the reason it is a `.save()` rather
	than seven hundred `db_set` calls.

	NOTHING BUT THE TOOL SWITCHES MOVES. The master switch, the token, the
	allowlist, the attribution user and the two packet types are all left exactly
	as they were: a profile answers "which tools", and every other field on this
	form answers a question a job title does not.
	"""
	frappe.only_for("System Manager")
	plan = plan_profile(profile)
	if settings.as_bool(dry_run):
		return plan

	doc = frappe.get_doc(settings.SETTINGS_DOCTYPE)
	for name in plan["enabling"]:
		doc.set(f"{SWITCH_PREFIX}{name}", 1)
	for name in plan["disabling"]:
		doc.set(f"{SWITCH_PREFIX}{name}", 0)
	doc.save()
	return {**plan, "applied": True, "summary": summary()}
