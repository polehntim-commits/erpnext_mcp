# SPDX-License-Identifier: MIT
"""Employee badges: minting one, printing a sheet of them, reading one back. v0.50.0.

WHAT WAS MISSING. `link_badge_to_employee` has RECORDED a badge since v0.44.0 —
an operator typed or scanned a string and this app remembered whose it was — and
nothing anywhere ISSUED one. The badges actually in workers' pockets were
printed by `farm_app` (Flask): a uuid4 `QRToken` that ERPNext has never heard
of, whose revocation lifecycle lives in a different application, and which
nobody can read off a scuffed card or say aloud over a radio at 6am when the
scanner will not focus. Of the two QR generators this app already had, one
issues an operator login credential and the other tags a valve.

So there was no answer to "print a badge for this hire", and — because a badge
ID could be any string at all — no answer to "who holds CF-0042?" either. These
three tools are those two answers.

────────────────────────────────────────────────────────────────────────────
THE IDENTIFIER IS MINTED HERE, AND THE OLD ONES KEEP WORKING
────────────────────────────────────────────────────────────────────────────

A minted badge is `<company abbreviation>-<sequence>` — `CF-0001` — and the
reason to prefer that over adopting `farm_app`'s uuid is that piece-rate
attribution is a payroll record. The identifier that decides who gets paid for a
bucket should live in the system that pays, with a retirement flag an HR manager
can flip, and should be legible to a human when the technology fails.

The cutover is incremental rather than a reprint day, and that is a genuine
property of the existing design rather than a workaround:
`bucket_bridge.resolve_badge_to_employee` matches a badge ID as an EXACT STRING
with no format assumption, so an old `farm_app` uuid mapped with
`link_badge_to_employee` and a new `CF-0042` minted here both resolve to the
same person for as long as the transition takes.

────────────────────────────────────────────────────────────────────────────
ONE ACTIVE BADGE PER PERSON, AND REISSUING RETIRES THE OLD ONE
────────────────────────────────────────────────────────────────────────────

`Bucket Log Badge Map.active` was always the retirement flag; nothing enforced
that a person had only one ticked. Two active cards for one picker is not a
harmless duplicate — it is a lost card that still resolves, which is precisely
the situation the flag exists for. So minting a replacement (`regenerate`)
unticks every other active row for that employee in the same call, and the
result names what it retired.

`generate_employee_badge_qr` is IDEMPOTENT WITHOUT `regenerate`: a second call
for somebody who already holds a live badge re-renders THAT badge's QR rather
than issuing a second one. Reprinting a card that went through a wash cycle is
the common request and it must not consume an identifier.
"""

from __future__ import annotations

import base64

import frappe

from .. import bucket_bridge, compat
from ..args import as_bool, as_str, resolve_company
from ..errors import ToolError
from ..render import qr
from ..result import ToolResult
from . import artifacts, bucket_log
from . import employee as employee_tool

BADGE_DOCTYPE = bucket_log.BADGE_DOCTYPE
EMPLOYEE = "Employee"
FARM_SHIFT = "Farm Shift"

#: The camp registers a card reads. Both are this app's own doctypes and both
#: are optional to a card: a site that has not migrated them, or a worker who
#: lives off the farm, prints a badge with no cabin line on it.
HOUSING_ASSIGNMENT = "Housing Assignment"
HOUSING_UNIT = "Housing Unit"

#: Cards one sheet call produces. The same hundred `generate_asset_qr_sheet`
#: takes, for the same reason: past that the answer is a multi-megabyte JSON
#: payload of base64 PNGs, and a crew of a hundred is already two Avery sheets.
SHEET_CAP = 100

#: Attempts at finding a free sequence number before giving up. Bounded because
#: the loop exists for a race — two foremen issuing a badge in the same second —
#: and a race that has not settled in fifty tries is a bug, not contention.
MINT_ATTEMPTS = 50

#: Error correction for an EMPLOYEE BADGE, and it is not the app's default.
#:
#: `qr.render` defaults to M — 15% of the symbol recoverable — which is right
#: for a login QR held up to a screen for ten seconds and wrong for a card that
#: lives in a picker's back pocket through a cherry harvest. H recovers 30%,
#: which is the difference between a scuffed, muddy, creased card that still
#: resolves at a bin trailer and one a foreman has to read aloud over a radio.
#:
#: THE COST IS A DENSER SYMBOL, NOT A BIGGER CARD. `CF-0001` is seven
#: alphanumeric characters and fits a version-1 symbol at H with room to spare,
#: so the module count does not change for any badge this app mints. It is
#: overridable per call because a site printing onto something smaller than a
#: badge may need the density back.
BADGE_ERROR_CORRECTION = "H"

#: How large a badge QR must be PRINTED, in inches. Below about an inch a
#: phone camera at arm's length in bright orchard sun hunts instead of locking,
#: and the failure presents to a picker as "the scanner is broken".
BADGE_PRINT_INCHES = 1.5

#: The resolution that print size is sized for. A QR scaled UP by a printer
#: from too few pixels gets soft module edges, which costs more scans than the
#: dirt does — so the PNG carries enough pixels to be printed at 1.5" without
#: interpolation.
BADGE_PRINT_DPI = 300

#: The Company field a card's logo comes from. NOT A COMPLIANCE FIELD, which is
#: why it is installed from here rather than declared in `compliance_fields.py`:
#: that table requires a regulator and a citation for every column it adds, and
#: a farm's logo on a badge has neither. It is a print detail, and it lives with
#: the printer.
#:
#: An Attach Image rather than a Data URL because an operator uploads a PNG from
#: a laptop and Frappe's own field type is what gives them the picker, the
#: preview and the File row; a URL field would give them a box to paste a path
#: into and no way to tell whether it resolved.
BADGE_LOGO_FIELD = "badge_logo"

#: What a badge card carries, and the label printed above the QR. Kept here
#: rather than in the caller so the sheet and the single card agree.
#:
#: v0.104.0: the Employee form's own **Company Details** section, whole. Tim
#: named the section rather than the columns, which is the right way round — it
#: is the block an HR manager fills in at hire and the block somebody looks at
#: when they ask where a person sits. `_company_details` turns it into card
#: fields; `grade` and `employment_type` come back in the data and stay off the
#: card, because Tim's own layout does not ask for them and a CR-80 has no room
#: for a fact nobody requested.
#:
#: Every one is passed through `compat.existing_fields`, so a site whose HR app
#: spells its Employee differently reads a row without them rather than failing
#: on a column that is not there.
_EMPLOYEE_FIELDS = (
	"name",
	"employee_name",
	"employee_number",
	"designation",
	"status",
	"company",
	"image",
	"reports_to",
	"department",
	"branch",
	"grade",
	"employment_type",
)


def _require() -> None:
	compat.require_doctype(
		BADGE_DOCTYPE,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _employee_row(employee: str) -> dict:
	"""One Employee's card fields, by whichever spelling of their name arrived."""
	name = employee_tool.resolve_employee(employee)
	wanted = compat.existing_fields(EMPLOYEE, list(_EMPLOYEE_FIELDS))
	row = frappe.db.get_value(EMPLOYEE, name, wanted, as_dict=True) or {}
	row = dict(row)
	row["name"] = name
	return row


def _initials(employee_name: str) -> str:
	"""The two letters that go in the photo box when there is no photograph.

	A badge with an empty rectangle where a face should be is a badge somebody
	has to turn over to identify. Initials are not identification and are not
	pretending to be — they are what a foreman glances at across a bin trailer.
	"""
	words = [word for word in str(employee_name or "").split() if word]
	if not words:
		return "?"
	if len(words) == 1:
		return words[0][:2].upper()
	return (words[0][:1] + words[-1][:1]).upper()


# ── where somebody sits, and who they answer to ──────────────────────────
#
# v0.103.0 put a job title, a crew and a cabin on the card. v0.104.0 is Tim
# again, with the rest of the picture, and it CORRECTS the middle of those three
# rather than adding to it.
#
# WHAT v0.103.0 GOT WRONG. It read `reports_to`, `department` and `branch` as a
# FALLBACK CHAIN — the first one set won, printed on one line as "the crew". That
# was a reasonable way to fill a single line and it is the wrong model of the
# farm. Those are three DIFFERENT facts about one person and Tim wants all of
# them legible: the department is which part of the operation they belong to, the
# branch is which place they work out of, and `reports_to` is the chain of
# command. Collapsing them meant a picker in Harvest at Mill Creek who reports to
# Ramirez printed one of those three and hid the other two.
#
# So `_company_details` reads the Employee form's own **Company Details** section
# whole — the section an HR manager is already looking at — and every field on the
# card comes back separately.
#
# THE CREW IS A DIFFERENT QUESTION AND KEEPS A DIFFERENT ANSWER. Tim: workers
# "connect with foremen for enforcement (a foreman may not be their direct
# report-to but aids in oversight)". That is exactly the open Farm Shift, and it
# is NOT the reporting line — so `_crew` below is now only ever the shift's
# foreman, it no longer stands in for `reports_to`, and it stays OFF the printed
# card because a roster changes daily and a laminated card does not.
#
# EVERY LOOKUP IS BEST-EFFORT AND NONE MAY LOSE A BADGE. `_company_logo` above
# sets the posture and these keep it: a site that has not migrated the camp
# register, a worker who has never been rostered, an HR app that spells its
# Employee without `reports_to` — each is a card printed WITHOUT that line rather
# than a badge nobody can issue. What goes wrong at a shed door on a Monday is a
# picker with no card, and no cabin number is worth risking one for.


def _label(doctype: str, name: str, field: str) -> str:
	"""A linked record's printable label, falling back to its docname. Never raises.

	ERPNext suffixes a Department's docname with the company abbreviation —
	`Harvest - ETC` — which is correct in a Link field and is not what anybody
	wants printed on a card. The doctype's own title column is asked for first
	and the docname is what arrives when there is no such column, no such row, or
	no such doctype.
	"""
	name = str(name or "").strip()
	if not name:
		return ""
	try:
		if compat.has_field(doctype, field):
			return str(frappe.db.get_value(doctype, name, field) or name)
	except Exception:
		pass
	return name


def _company_details(row: dict) -> dict:
	"""The Employee form's **Company Details** section, as a card prints it.

	FIVE FIELDS AN HR MANAGER ALREADY MAINTAINS, and that is the argument for
	reading them rather than inventing anything: `designation`, `department`,
	`branch`, `reports_to`, `grade` and `employment_type` are on the Employee
	form under one heading, somebody fills them in at hire, and until v0.104.0 a
	badge printed exactly one of them.

	EACH COMES BACK SEPARATELY, which is the correction this release is. See the
	block comment above: three of these were read as a fallback chain and the
	first one set won, so a picker in Harvest at Mill Creek who reports to Ramirez
	printed one fact and hid two.

	DOCNAMES ARE NOT LABELS. ERPNext suffixes a Department with the company
	abbreviation and `_label` unpicks it; `reports_to` is an Employee DOCNAME, and
	a card that printed `HR-EMP-00007` where a supervisor's name belongs would be
	a chain of command nobody can read. `reports_to_designation` comes back too,
	because "Rosa Ramirez, Foreman" answers "who is that" in a way a bare name
	does not.

	THE CHAIN ABOVE THEM IS `shadow_log.raci_chain`, NOT A SECOND WALK. That
	function already walks `reports_to` upward, already caps the walk, already
	survives a cycle by reporting it, and already names the levels — direct
	supervisor, manager, owner, which is the ladder Tim describes. A second walker
	here would be a second opinion about the same chain, and the two would
	disagree the first time somebody fixed one of them. It is returned but NOT
	printed: a CR-80 card has room for the person somebody escalates to, and the
	rest of the ladder is a thing a handset shows.
	"""
	details = {
		"department": None,
		"branch": None,
		"reports_to": None,
		"reports_to_name": None,
		"reports_to_designation": None,
		"reports_to_chain": [],
		"grade": None,
		"employment_type": None,
	}

	department = str(row.get("department") or "").strip()
	if department:
		details["department"] = _label("Department", department, "department_name") or department

	# A Branch's docname IS its branch name — ERPNext gives it no separate title
	# column and no abbreviation suffix — so there is nothing to unpick.
	details["branch"] = str(row.get("branch") or "").strip() or None
	details["grade"] = str(row.get("grade") or "").strip() or None
	details["employment_type"] = str(row.get("employment_type") or "").strip() or None

	supervisor = str(row.get("reports_to") or "").strip()
	if not supervisor:
		return details
	details["reports_to"] = supervisor
	try:
		found = (
			frappe.db.get_value(
				EMPLOYEE,
				supervisor,
				compat.existing_fields(EMPLOYEE, ("employee_name", "designation")),
				as_dict=True,
			)
			or {}
		)
	except Exception:
		found = {}
	details["reports_to_name"] = str(found.get("employee_name") or "").strip() or supervisor
	details["reports_to_designation"] = str(found.get("designation") or "").strip() or None

	try:
		from .shadow_log import raci_chain

		details["reports_to_chain"] = list(raci_chain(str(row.get("name") or "")).get("chain") or [])
	except Exception:
		details["reports_to_chain"] = []
	return details


def _crew(row: dict) -> dict:
	"""The crew they are standing on right now — the OVERSIGHT link, not the formal one.

	v0.104.0 NARROWED THIS, and the narrowing is the point. It used to fall back
	through `reports_to`, `department` and `branch`, which conflated two different
	relationships: Tim's, on the farm's own model — a worker "connects with
	foremen for enforcement (a foreman may not be their direct report-to but aids
	in oversight)". The formal line is `reports_to` and it is on the card. THIS is
	the operational one, and it is a Farm Shift.

	IT IS NOT PRINTED, deliberately. A shift is one morning's roster, and a card
	that laminated `SHIFT-2026-0114`'s foreman would be wrong by Wednesday with no
	way to correct it short of a reprint. It comes back in the data because the
	handset that asks "who is overseeing this badge right now" is asking a real
	question with a today-shaped answer.

	AMBIGUITY ANSWERS NOTHING. Two open shifts naming one person is a roster
	somebody has to fix, and `dispatch._open_shift_for` already refuses to guess
	between them.
	"""
	crew = {"crew": None, "crew_source": None, "shift": None, "shift_type": None, "foreman": None}
	employee = str(row.get("name") or "").strip()
	if not employee:
		return crew

	# Imported here rather than at the top of the file because it is the only
	# reader of the dispatch module in this one, and because a card must not stop
	# being printable if that chain fails to import.
	try:
		from .dispatch import _open_shift_for

		shift = _open_shift_for(employee, str(row.get("company") or ""))
	except Exception:
		shift = ""
	if not shift:
		return crew
	try:
		found = (
			frappe.db.get_value(
				FARM_SHIFT,
				shift,
				compat.existing_fields(FARM_SHIFT, ("foreman", "foreman_name", "shift_type")),
				as_dict=True,
			)
			or {}
		)
	except Exception:
		found = {}
	label = str(found.get("foreman_name") or found.get("foreman") or "").strip()
	crew["crew"] = label or shift
	crew["crew_source"] = "open shift"
	crew["shift"] = shift
	crew["shift_type"] = str(found.get("shift_type") or "") or None
	crew["foreman"] = str(found.get("foreman") or "") or None
	return crew


def _housing(row: dict) -> dict:
	"""Where this person sleeps: the camp, the cabin, and the assignment behind them.

	CURRENT MEANS NO `end_date`, which is `housing._current_assignments`' own
	definition and not a second one invented here. `status` is a Select an
	operator maintains by hand and `end_date` is what `end_housing_assignment`
	actually writes, so a card that trusted the Select would print a cabin
	somebody moved out of in June.

	THE CAMP IS THE PARCEL. A labor camp is a place — `housing.create_housing_unit`
	numbers a unit within a parcel and refuses a second unit of the same name on
	it — so `<parcel> · <unit>` is the pair that identifies a bed to somebody
	walking the camp at six in the morning, and either half alone is ambiguous
	across a farm with two camps that both have a Cabin 3.

	THE MOST RECENT ONE WINS where a worker holds two open assignments. That is
	a data problem `get_housing_capacity` already surfaces, and unlike the crew
	line there is no honest way to print nothing: somebody sleeps somewhere, and
	the newest assignment is the best guess at where.
	"""
	housing = {"housing": None, "camp": None, "cabin": None, "housing_assignment": None, "unit": None}
	employee = str(row.get("name") or "").strip()
	if not employee or not compat.doctype_exists(HOUSING_ASSIGNMENT):
		return housing

	try:
		rows = (
			frappe.db.get_all(
				HOUSING_ASSIGNMENT,
				filters={"employee": employee, "end_date": ("is", "not set")},
				fields=compat.existing_fields(
					HOUSING_ASSIGNMENT, ("name", "unit", "parcel", "assigned_date", "status")
				),
				order_by="assigned_date desc",
				limit=5,
			)
			or []
		)
	except Exception:
		return housing

	live = [entry for entry in rows if str(entry.get("status") or "Current") != "Ended"]
	if not live:
		return housing
	found = dict(live[0])

	unit = str(found.get("unit") or "").strip()
	cabin = _label(HOUSING_UNIT, unit, "unit_name") if unit else ""
	camp = str(found.get("parcel") or "").strip()
	if not camp and unit:
		# The assignment carries its own `parcel` and may not have been given one.
		# The unit always has: `create_housing_unit` requires it.
		try:
			camp = str(frappe.db.get_value(HOUSING_UNIT, unit, "parcel") or "")
		except Exception:
			camp = ""

	housing["housing_assignment"] = str(found.get("name") or "") or None
	housing["unit"] = unit or None
	housing["cabin"] = cabin or None
	housing["camp"] = camp or None
	if cabin and camp:
		housing["housing"] = f"{camp} · {cabin}"
	else:
		housing["housing"] = cabin or camp or None
	return housing


def _company_prefix(company: str) -> str:
	"""The badge prefix for a company: its abbreviation, or its initials.

	The abbreviation is what an operator already chose and what every docname on
	the site is suffixed with, so a badge that carries it needs no second
	convention learned. A Company with no `abbr` — possible on an install where
	it was cleared — falls back to the initials of its name, and a name with no
	letters at all is a refusal rather than a guess: a badge with no prefix is
	`-0001`, which is not an identifier anybody can read aloud.
	"""
	abbr = ""
	if compat.has_field("Company", "abbr"):
		abbr = str(frappe.db.get_value("Company", company, "abbr") or "")
	prefix = bucket_bridge.normalise_badge_prefix(abbr)
	if not prefix:
		from .. import abbr as abbr_module

		prefix = bucket_bridge.normalise_badge_prefix(abbr_module.derive_abbr(company))
	if not prefix:
		raise ToolError(
			f"company {company!r} has no abbreviation and no letters in its name, so a readable "
			"badge prefix cannot be derived from it. Set the Company's abbreviation, or pass "
			"badge_id explicitly. Nothing was changed."
		)
	return prefix


def _company_logo(company: str) -> str:
	"""The company's badge logo URL, or "" — never a refusal.

	A SITE WITHOUT THE FIELD STILL PRINTS BADGES. The field arrives on the next
	`bench migrate` and a card is perfectly legible without a logo, so every
	failure here — no field, no value, a Company row that has gone — is the
	empty string rather than an exception. A badge nobody can print because a
	logo is missing is a worse outcome than a badge with no logo on it.
	"""
	if not company or not compat.has_field("Company", BADGE_LOGO_FIELD):
		return ""
	return str(frappe.db.get_value("Company", company, BADGE_LOGO_FIELD) or "")


def install_badge_logo_field() -> dict:
	"""Add `Company.badge_logo` if this site has not got it. Idempotent.

	Called from `install.after_migrate`, so an operator upgrading gets the field
	without a bespoke patch, and re-running finds it present and does nothing.
	"""
	report = {"created": False, "existed": False, "skipped": ""}
	if not compat.doctype_exists("Company"):
		report["skipped"] = "no Company doctype on this site"
		return report
	if compat.has_field("Company", BADGE_LOGO_FIELD):
		report["existed"] = True
		return report

	doc = frappe.get_doc(
		{
			"doctype": "Custom Field",
			"dt": "Company",
			"fieldname": BADGE_LOGO_FIELD,
			"label": "Badge Logo",
			"fieldtype": "Attach Image",
			"insert_after": "company_logo" if compat.has_field("Company", "company_logo") else "abbr",
			"description": (
				"The mark printed on this entity's employee badge cards. Separate from the "
				"letterhead logo on purpose: a badge is a 2x3 card read across a bin trailer, "
				"and the wordmark that works on an invoice is usually unreadable at that size."
			),
			"module": "ERPNext MCP",
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	report["created"] = True
	return report


def _issued_badge_ids() -> list:
	"""Every badge ID on the site, active or not.

	SITE-WIDE RATHER THAN PER-COMPANY, because `badge_id` is the DOCNAME and
	docnames are unique across the site. Reading only one company's rows would
	mint a number that another entity's register already holds, and the insert
	would fail on a uniqueness error the operator did not cause.

	RETIRED ROWS COUNT. A lost card's number is never handed to the next picker
	— see `bucket_bridge.next_badge_sequence`.
	"""
	return frappe.db.get_all(BADGE_DOCTYPE, filters={}, pluck="badge_id", limit_page_length=0) or []


def _active_badges(employee: str) -> list:
	"""Every live badge row for one person, newest first."""
	return (
		frappe.db.get_all(
			BADGE_DOCTYPE,
			filters={"employee": employee, "active": 1},
			fields=["name", "badge_id", "company"],
			order_by="modified desc",
			limit_page_length=0,
		)
		or []
	)


def _mint(company: str, prefix: str) -> str:
	"""A badge ID nothing on this site holds yet.

	THE LOOP IS THE CONCURRENCY STORY. Two foremen issuing a badge in the same
	second read the same register and compute the same next number; the second
	insert would collide on the unique `badge_id`. Re-reading `frappe.db.exists`
	between the computation and the write closes most of that window, and the
	insert itself is the backstop — `badge_id` is unique in the doctype, so the
	worst case is a refusal rather than two pickers sharing an identifier.
	"""
	issued = list(_issued_badge_ids())
	for _ in range(MINT_ATTEMPTS):
		candidate = bucket_bridge.next_badge_id(issued, prefix)
		if not frappe.db.exists(BADGE_DOCTYPE, candidate):
			return candidate
		issued.append(candidate)
	raise ToolError(
		f"could not find a free badge ID under the prefix {prefix!r} after {MINT_ATTEMPTS} "
		"attempts. Something else is issuing badges at the same time, or the register is "
		"inconsistent. Nothing was changed."
	)


def _badge_scale(modules: int) -> int:
	"""Pixels per module, so the PNG prints at `BADGE_PRINT_INCHES` unscaled.

	Computed from the symbol rather than hardcoded, because a longer company
	prefix pushes the badge into a version-2 symbol and a fixed scale would then
	print the same 1.5" card at two-thirds the resolution without anybody
	noticing. Never below `qr.SCALE` — a smaller image than the app's own
	default would be a regression on the screen as well as on paper.
	"""
	side = max(1, int(modules)) + 2 * qr.BORDER
	needed = BADGE_PRINT_INCHES * BADGE_PRINT_DPI
	return max(qr.SCALE, -(-int(needed) // side))  # ceil


def _print_spec(rendered: dict, badge_id: str) -> dict:
	"""What a card layout must honour, as numbers rather than as a paragraph.

	THIS APP DOES NOT LAY THE CARD OUT. `generate_employee_badge_sheet` returns
	card DATA and a template name — an Avery sheet, a label printer, the Desk's
	print format, the handset's preview — so "print it at least an inch and a
	half with a white box behind it" cannot be enforced by this module. What it
	can do is state the requirement in the payload, in units a renderer can act
	on, instead of leaving each one to guess and get a different answer.

	`caption` is the human-readable fallback and belongs BELOW the symbol. Every
	scanner eventually fails on a card that went through a wash cycle, and a
	badge nobody can read out over a radio is a picker whose buckets go
	unattributed for the rest of the morning. It is the whole reason this app
	mints `CF-0001` rather than adopting `farm_app`'s uuid.
	"""
	return {
		"min_width_inches": BADGE_PRINT_INCHES,
		"min_width_points": round(BADGE_PRINT_INCHES * 72.0, 1),
		"dpi_at_min_width": round(rendered["pixels"] / BADGE_PRINT_INCHES, 1),
		# Four modules on every side, the specification's own minimum, already
		# baked into `png_bytes`. Restated so a renderer that crops or insets
		# the image knows what it would be destroying.
		"quiet_zone_modules": rendered["border"],
		# A colored badge card is fine; a colored QR is not. The symbol needs a
		# white box under it whatever the card behind it looks like.
		"foreground": "#000000",
		"background": "#FFFFFF",
		"caption": badge_id,
		"caption_position": "below",
	}


def _render(badge_id: str, error: str = BADGE_ERROR_CORRECTION) -> dict:
	"""The QR for one badge, with a missing encoder reported as a tool failure.

	`registry.py` gates all three of these tools on `_qr_available`, so a bench
	with neither `segno` nor `qrcode` loses them by name with the pip command to
	fix it. This is the direct-call path — a test, another tool — and turning the
	renderer's `RuntimeError` into a `ToolError` keeps that failure a sentence
	rather than a traceback.

	TWO PASSES OVER THE ENCODER, ON PURPOSE. The scale that makes a 1.5" print
	come out at 300 dpi depends on how many modules the symbol has, and the
	module count depends on the payload and the error-correction level. So the
	matrix is built first, sized, and then rendered at the scale that sizing
	produced. Encoding `CF-0001` twice costs microseconds and is the difference
	between a card that prints crisply and one whose scale was guessed.
	"""
	try:
		modules = len(qr.qr_matrix(badge_id, error=error))
		return qr.render(badge_id, error=error, scale=_badge_scale(modules))
	except RuntimeError as exc:
		raise ToolError(str(exc)) from None


def _retire_others(employee: str, keep: str, reason: str) -> list:
	"""Untick every other live badge for this person. Returns what it retired.

	A REPLACEMENT CARD MUST KILL THE ONE IT REPLACES. Otherwise the lost badge
	somebody found in an orchard still resolves to its holder, and every bucket
	scanned with it is paid to them.
	"""
	retired = []
	for row in _active_badges(employee):
		if row["badge_id"] == keep:
			continue
		doc = frappe.get_doc(BADGE_DOCTYPE, row["name"])
		doc.active = 0
		doc.notes = ((doc.get("notes") or "") + f"\n{reason}").strip()
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		retired.append(row["badge_id"])
	return retired


def _choose_badge_id(row: dict, company: str, args: dict, explicit_badge: str = "") -> tuple:
	"""This employee's badge ID and whether it is a new one. READS ONLY.

	Split from `_record_badge` so the QR can be drawn BEFORE anything is
	written: a bench with no encoder must refuse before it has consumed an
	identifier and printed nothing, which is the failure order that leaves a
	register with a badge nobody holds.
	"""
	employee = row["name"]
	regenerate = as_bool(args, "regenerate", False)

	if explicit_badge:
		errors = bucket_bridge.validate_badge_id(explicit_badge)
		if errors:
			# Not quoted beyond `validate_badge_id`'s own bound — see
			# `bucket_bridge._display`.
			raise ToolError(f"that badge_id is not a usable badge: {'; '.join(errors)}. Nothing was changed.")
		holder = frappe.db.get_value(BADGE_DOCTYPE, explicit_badge, ["employee", "active"], as_dict=True)
		if holder and str(holder.get("employee")) != employee and holder.get("active"):
			raise ToolError(
				f"badge {explicit_badge!r} is already live for {holder['employee']}. Two people "
				"holding one badge ID is one person's piecework paid to the other. Retire it "
				"first with link_badge_to_employee(active=false), or let this tool mint a new "
				"ID. Nothing was changed."
			)
		return explicit_badge, not holder

	live = _active_badges(employee)
	if live and not regenerate:
		# The reprint case, and the reason this tool is idempotent: a card that
		# went through a wash cycle is reprinted, not reissued.
		return live[0]["badge_id"], False
	return _mint(company, _company_prefix(company)), True


def _record_badge(row: dict, company: str, badge_id: str, args: dict) -> dict:
	"""Make the register say this badge is this person's, and retire the rest.

	The upsert is `link_badge_to_employee`'s, not a second copy of it: that tool
	owns the reissue-to-somebody-else case AND the backfill of `employee` onto
	captures already synced against the badge, and a badge minted here would
	otherwise not claim what was picked before it was printed.
	"""
	employee = row["name"]
	notes = as_str(args, "notes")
	result = bucket_log.link_badge_to_employee(
		{
			"badge_id": badge_id,
			"employee": employee,
			"company": company,
			"active": True,
			**({"notes": notes} if notes else {}),
		}
	)
	retired = _retire_others(
		employee,
		badge_id,
		f"Retired {frappe.utils.today()}: replaced by badge {badge_id}.",
	)
	return {
		"retired": retired,
		"previous_employee": (result.data or {}).get("previous_employee"),
		"backfilled_entries": (result.data or {}).get("backfilled_entries") or [],
	}


def _card(row: dict, badge_id: str, company: str, rendered: dict) -> dict:
	"""One badge card's printable facts, in the order they go on the card."""
	return {
		"employee": row["name"],
		"employee_name": row.get("employee_name") or row["name"],
		"employee_number": row.get("employee_number") or None,
		"designation": row.get("designation") or None,
		"company": company,
		"badge_id": badge_id,
		# v0.104.0. Where this person sits and who they answer to, each fact its
		# own key. `department`, `branch`, `reports_to_name` and `housing` are
		# what a layout prints; `reports_to`, `reports_to_chain`, `camp`,
		# `cabin`, `unit` and the docnames are what a caller checks — a handset
		# linking to the Housing Assignment should not have to parse
		# `"Mill Creek · Cabin 3"` back apart to find it.
		#
		# `crew` is the shift foreman and is NOT printed; see `_crew`.
		**_company_details(row),
		**_crew(row),
		**_housing(row),
		# The photograph if the Employee record has one, and the initials that
		# go in its place when it does not. Both, always — a print template
		# should not have to ask this app a second question to lay out a card.
		"photo_url": row.get("image") or None,
		"photo_placeholder": _initials(row.get("employee_name") or row["name"]),
		# The entity's mark, for the same reason the photo and the initials are
		# both here: a print template should not have to ask this app a second
		# question to lay out a card. None where the site has no logo set.
		"company_logo_url": _company_logo(company) or None,
		"png_base64": base64.b64encode(rendered["png"]).decode("ascii"),
		"png_bytes": len(rendered["png"]),
		"modules": rendered["modules"],
		"pixels": rendered["pixels"],
		"error_correction": rendered["error_correction"],
		# What a layout has to honour. See `_print_spec` — this app returns card
		# data and a template NAME, so the requirement travels with the data.
		"print": _print_spec(rendered, badge_id),
	}


# ── the Employee record's own copy ───────────────────────────────────────
#
# v0.56.0, AND IT IS A BUG FIX WEARING A FEATURE'S CLOTHES. Every call below
# returned the card as base64 in a JSON payload and wrote NOTHING to the
# Employee record. That is exactly right for the handset — it draws the card on
# a screen — and it meant that on the Desk, where an HR manager opens somebody's
# Employee form to find their badge, there was nothing there. The badge existed,
# the register knew it, and the only ways to see it were an MCP call and a
# Bucket Log Badge Map docname nobody memorises.
#
# So the QR is also written where somebody would look for it: the Attachments
# sidebar of the Employee record it belongs to.
#
# PRIVATE, LIKE EVERY OTHER FILE THIS APP WRITES. A badge QR encodes the badge
# ID and nothing else — no name, no date of birth — so it is the least sensitive
# file in the app; it is private anyway, because "which of our files are safe to
# serve to anybody who guesses the URL" is not a question worth having two
# answers to.
#
# ONE FILE PER BADGE, REPLACED IN PLACE. The filename carries the badge ID, so
# reprinting the same badge overwrites its own attachment rather than leaving a
# sidebar with four identical QRs in it, and reissuing after a lost card leaves
# the old badge's file alone — it is the evidence of what that card was.

#: What the QR attachment is called on the Employee record. The badge ID is in
#: the name so a sidebar with two of them says which is which, and so the
#: replace-in-place check below can find its own previous copy without a field
#: on Employee to remember it by.
QR_FILE_TEMPLATE = "badge-{badge_id}-qr.png"

#: And the card. Same reasoning, and the suffix is what tells them apart at a
#: glance in a list that sorts alphabetically.
CARD_FILE_TEMPLATE = "badge-{badge_id}-card.pdf"


def _attached_file(employee: str, file_name: str) -> str:
	"""The docname of a File with this name already on this Employee, or "".

	MATCHED ON THE NAME, which is what makes reprinting idempotent without a
	column on Employee to remember the last one by. Frappe uniquifies a
	filename it has seen before rather than replacing it, so without this a
	foreman reprinting a washed card four times leaves four identical PNGs in
	the sidebar and the newest one is not obviously the newest.
	"""
	if not compat.doctype_exists("File"):  # pragma: no cover - not a real Frappe
		return ""
	try:
		rows = frappe.db.get_all(
			"File",
			filters={
				"attached_to_doctype": EMPLOYEE,
				"attached_to_name": employee,
				"file_name": file_name,
			},
			fields=["name"],
			limit=1,
		)
	except Exception:  # pragma: no cover - a site mid-migrate
		return ""
	return str(rows[0]["name"]) if rows else ""


def _attach_to_employee(employee: str, file_name: str, content: bytes) -> dict:
	"""Put one file in the Employee's Attachments sidebar. NEVER RAISES.

	A BADGE THAT PRINTED AND DID NOT ATTACH IS STILL A BADGE. This runs after
	the register has been written and the QR drawn, and the thing the caller
	asked for — issue a badge, give me the symbol — has already succeeded by the
	time it is reached. Failing the whole call because the File doctype refused
	an attachment would take a working badge away from somebody because a
	sidebar entry did not appear, so the outcome is REPORTED instead: `attached`
	is False and `note` says why.
	"""
	report = {"attached": False, "file_url": None, "file_docname": None, "replaced": None, "note": ""}
	try:
		existing = _attached_file(employee, file_name)
		if existing:
			# Deleted rather than left beside its replacement. This is the SAME
			# badge's symbol regenerated, not a second badge — see the section
			# comment. A reissue mints a new badge ID and therefore a new
			# filename, and that one is left alone.
			frappe.delete_doc("File", existing, ignore_permissions=True, force=True)
			report["replaced"] = existing
		attachment = artifacts.attach_bytes(EMPLOYEE, employee, file_name, content)
		report["attached"] = True
		report["file_url"] = attachment.get("file_url")
		report["file_docname"] = attachment.get("name")
	except Exception as exc:
		report["note"] = (
			f"the badge was issued and the file was not attached to {employee} "
			f"({type(exc).__name__}: {exc}). The QR is in this result either way, and calling "
			f"again will retry the attachment."
		)
	return report


# ── 1. generate_employee_badge_qr ────────────────────────────────────────


def generate_employee_badge_qr(args: dict) -> ToolResult:
	"""MUTATING (default OFF). Issue (or reprint) one employee's scanning badge:
	mint a readable badge ID if they have none, record it in the Bucket Log
	Badge Map register, and hand back the QR code that goes on the card.

	IDEMPOTENT WITHOUT `regenerate` — somebody who already holds a live badge
	gets THAT badge's QR back rather than a second identifier. `regenerate=true`
	is the lost-card path: it mints a new ID and RETIRES the old one in the same
	call, because a replacement that leaves its predecessor resolving is how a
	found badge keeps earning.

	v0.104.0: the card carries the Employee form's **Company Details** — job
	title, department, branch and who they report to — alongside the camp and
	cabin. See `_company_details` and `_housing`; every lookup is best-effort, and
	a site that records none of them prints exactly the card it printed before.

	`crew` is still returned and is no longer the same question: it is the foreman
	of the shift they are standing on RIGHT NOW, which is the oversight link
	rather than the reporting line, and it is not printed on a card that will
	outlive the roster.

	v0.94.0: `require_hiring_role`, NOT `require_hr_role`. A badge is an
	IDENTIFIER, not PII — it carries a name and a designation, both of which are
	already on the front of the worker's shirt — and issuing one is step 9 of a
	hire, between the bunk and the crew. The person who has just filled in the
	I-9 is the person who should hand over the card, and a gate that sent them
	looking for an HR account meant a worker who was hired, housed and rostered
	could not scan into the day's work. `link_badge_to_employee`, `resolve_badge`
	and `get_employee_badge_pass` were already scope-only and are unchanged."""
	_require()
	actor = employee_tool.require_hiring_role()

	row = _employee_row(as_str(args, "employee", required=True))
	company = resolve_company(as_str(args, "company") or str(row.get("company") or ""), required=True)
	employee_tool.require_company_scope(actor, company)

	if row.get("company") and str(row["company"]) != company:
		raise ToolError(
			f"{row['name']} is employed by {row['company']} and this call names {company}. A badge "
			"belongs to the entity that issued it and resolves only against that entity's "
			"captures. Nothing was changed."
		)

	status = str(row.get("status") or "Active")
	if status != "Active":
		raise ToolError(
			f"{row['name']} ({row.get('employee_name')}) has employment status {status}, not "
			"Active. A badge is what attributes piece work to somebody on the payroll — "
			"reactivate_employee first. Nothing was changed."
		)

	fmt = as_str(args, "format") or "png"
	if fmt not in ("png", "matrix"):
		raise ToolError("format must be 'png' or 'matrix'.")

	# The ID is settled and the QR drawn BEFORE the register is written, so a
	# bench that cannot draw one refuses before it has issued a badge nobody can
	# print.
	badge_id, created = _choose_badge_id(row, company, args, as_str(args, "badge_id"))
	rendered = _render(badge_id, as_str(args, "error_correction") or BADGE_ERROR_CORRECTION)
	recorded = _record_badge(row, company, badge_id, args)

	# v0.56.0. AFTER the register is written, and deliberately last: the badge is
	# issued by this point and the attachment is a convenience on top of it. See
	# `_attach_to_employee` — it never raises.
	attach = as_bool(args, "attach", True)
	attachment = (
		_attach_to_employee(row["name"], QR_FILE_TEMPLATE.format(badge_id=badge_id), rendered["png"])
		if attach
		else {"attached": False, "note": "attach=false — nothing was written to the Employee record."}
	)

	data = {
		"actor": actor,
		**_card(row, badge_id, company, rendered),
		"created": created,
		"reused": not created,
		"attachment": attachment,
		"retired_badges": recorded["retired"],
		"previous_employee": recorded["previous_employee"],
		"backfilled_entries": recorded["backfilled_entries"],
		"scale": rendered["scale"],
		"border": rendered["border"],
		"error_correction": rendered["error_correction"],
		"encoder": rendered["encoder"],
	}
	if fmt == "matrix":
		data.pop("png_base64", None)
		data.pop("png_bytes", None)
		data["matrix"] = rendered["matrix"]

	verb = "issued" if created else "reprinted"
	summary = f"badge {badge_id} {verb} for {row.get('employee_name') or row['name']}"
	if recorded["retired"]:
		summary += f", retired {', '.join(recorded['retired'])}"
	if attachment.get("attached"):
		summary += " and attached to their Employee record"
	return ToolResult(
		data=data,
		summary=summary,
		docstatus_delta=("none → 0 (badge issued)" if created else "badge reprinted"),
	)


# ── 2. generate_employee_badge_sheet ─────────────────────────────────────


def generate_employee_badge_sheet(args: dict) -> ToolResult:
	"""MUTATING (default OFF). A printable sheet of badge cards — name, photo (or
	initials), designation, department, branch, who they report to, camp and
	cabin, badge ID and QR — for a crew at once. Issues a badge to anybody who has
	none and reuses the live one where there is one.

	ONE EMPLOYEE'S FAILURE DOES NOT LOSE THE SHEET. A name that resolves to
	nobody, somebody who has left, an entity this actor cannot reach — each is
	reported by name in `errors` and the other twenty-nine cards are still
	printed, the same posture `sync_bucket_entries` takes for a batch. A hiring
	day with one bad row in the list should not be a hiring day with no badges.

	v0.94.0: `require_hiring_role`, for the reason above and one more that is
	specific to the sheet — twelve badges printed at a shed door IS the foreman's
	morning, and it is the case this tool was written for."""
	_require()
	actor = employee_tool.require_hiring_role()

	names = args.get("employees") or args.get("employee_names")
	if isinstance(names, str):
		names = [part.strip() for part in names.split(",") if part.strip()]
	if not names or not isinstance(names, list):
		raise ToolError(
			"employees is required and must be a list of Employee docnames (or names, numbers "
			"or logins — each is resolved the way link_badge_to_employee resolves one). "
			"Nothing was changed."
		)
	if len(names) > SHEET_CAP:
		raise ToolError(
			f"{len(names)} employees is more than one sheet call produces ({SHEET_CAP}). Split "
			"the crew. Nothing was changed."
		)

	wanted_company = resolve_company(as_str(args, "company"), required=False)
	template = as_str(args, "template") or "badge_card_2x3"

	cards = []
	errors = []
	seen: set = set()
	for index, raw in enumerate(names):
		reference = str(raw or "").strip()
		if not reference:
			errors.append({"index": index, "employee": None, "error": "empty entry"})
			continue
		try:
			row = _employee_row(reference)
			if row["name"] in seen:
				errors.append(
					{
						"index": index,
						"employee": row["name"],
						"error": "named twice in this call — one card was printed",
					}
				)
				continue
			company = resolve_company(wanted_company or str(row.get("company") or ""), required=True)
			employee_tool.require_company_scope(actor, company)
			if row.get("company") and str(row["company"]) != company:
				raise ToolError(f"employed by {row['company']}, and this sheet is for {company}")
			status = str(row.get("status") or "Active")
			if status != "Active":
				raise ToolError(f"employment status is {status}, not Active")

			badge_id, created = _choose_badge_id(row, company, args)
			rendered = _render(badge_id)
			recorded = _record_badge(row, company, badge_id, args)
			seen.add(row["name"])
			cards.append(
				{
					**_card(row, badge_id, company, rendered),
					"created": created,
					"reused": not created,
					"retired_badges": recorded["retired"],
				}
			)
		except ToolError as exc:
			errors.append({"index": index, "employee": reference, "error": str(exc)})

	minted = sum(1 for card in cards if card["created"])
	summary = f"{len(cards)} badge card(s) for {template}, {minted} newly issued"
	if errors:
		summary += f", {len(errors)} skipped"
	return ToolResult(
		data={
			"actor": actor,
			"template": template,
			"card_count": len(cards),
			"issued_count": minted,
			"error_count": len(errors),
			"cards": cards,
			"errors": errors,
			"encoder": qr.encoder_name(),
		},
		summary=summary,
		docstatus_delta=(f"{minted} Bucket Log Badge Map record(s) created" if minted else ""),
	)


# ── 3. generate_employee_id_card ─────────────────────────────────────────


def generate_employee_id_card(args: dict) -> ToolResult:
	"""MUTATING (default OFF). One employee's ID card, as a PDF on their own record.

	TIM'S ACTUAL PROBLEM, AND IT IS NOT THAT BADGES WERE HARD TO MAKE. Badges
	were being made and then being unfindable: `generate_employee_badge_qr`
	answered with base64 in a JSON payload, which is exactly what a handset wants
	and is nothing at all to somebody who has opened an Employee form in the Desk
	looking for a card to print. This attaches the card where they are already
	looking.

	IT ISSUES A BADGE IF THERE IS NONE, and reuses the live one where there is —
	the same contract `generate_employee_badge_qr` keeps, because it delegates to
	it rather than reimplementing the mint. Printing a card must not be a second
	way to consume an identifier.

	THE LAYOUT IS NOT THIS APP'S SECOND OPINION ABOUT A CARD. `badge_sheet` draws
	it, in the same millimetres and the same classes as the Print Format the Desk
	Print button uses, so the card that comes off this call and the card that
	comes off that button are the same card. A layout written here would be a
	third one, and three layouts is two too many for a thing that has to line up
	with a pre-printed lanyard slot.

	────────────────────────────────────────────────────────────────────────
	THE PDF IS BEST-EFFORT, AND THE REASON IS A POLICY THIS APP ALREADY HAS
	────────────────────────────────────────────────────────────────────────

	`render/__init__.py` states it: this app has no runtime dependency beyond
	Frappe itself, and `frappe.utils.pdf.get_pdf` shells out to a **wkhtmltopdf
	binary that is present in some images and absent in others**. Every other
	document this app writes is drawn by `render/pdf.py` in the standard library
	for exactly that reason — but `render/pdf.py` has no images and no colour,
	and a badge with no photograph and no QR is not a badge.

	So this is the one document that asks for the binary, and it asks POLITELY:
	where wkhtmltopdf is present the PDF is rendered and attached, and where it
	is not, the call still issues the badge, still attaches the QR, still returns
	the card's HTML, and says in one sentence that the PDF needs a binary this
	bench has not got. A bench without it keeps the Desk's own Print button,
	which renders the same layout through the same missing binary — so nothing
	this call could do would make a PDF appear there either.
	"""
	_require()
	inner = dict(args)
	# `attach` governs the CARD on this call; the QR underneath it is attached
	# either way, because the QR is what a scanner needs and it costs nothing.
	inner.pop("attach", None)
	badge = generate_employee_badge_qr(inner)
	card = dict(badge.data)
	employee = str(card.get("employee") or "")
	badge_id = str(card.get("badge_id") or "")

	from .. import card_redirect
	from ..badge_sheet import sheet_html

	# v0.213.0. THE CARD IS THE CR80 CARD — the one `request_card_print` records
	# and the "Employee ID Card (CR80)" print format draws — and its PDF is drawn
	# by `card_art`, so it no longer needs wkhtmltopdf. `legacy_badge_layouts`
	# (a Farm Feature Flag) brings the old layout back.
	attach = as_bool(args, "attach", True)
	if card_redirect.legacy():
		html = sheet_html([card], title=f"ID Card — {card.get('employee_name') or employee}")
		rendered = _card_pdf(html)
		print_format = "Employee Badge Card"
	else:
		html = card_redirect.document(
			"Employee", [employee], title=f"ID Card — {card.get('employee_name') or employee}"
		)
		pdf, note = card_redirect.pdf("Employee", employee)
		rendered = {"pdf": pdf, "note": note}
		print_format = card_redirect.FORMATS["Employee"]
	attachment = {"attached": False, "note": rendered["note"]}
	if attach and rendered["pdf"]:
		attachment = _attach_to_employee(
			employee, CARD_FILE_TEMPLATE.format(badge_id=badge_id), rendered["pdf"]
		)
	elif not attach:
		attachment = {"attached": False, "note": "attach=false — nothing was written to the record."}

	data = {
		"employee": employee,
		"employee_name": card.get("employee_name"),
		"badge_id": badge_id,
		"created": card.get("created"),
		"reused": card.get("reused"),
		# BOTH ATTACHMENTS REPORTED SEPARATELY. They fail independently and for
		# different reasons — the QR needs an encoder, the card needs a PDF
		# binary — and "the badge did not attach" would be an unanswerable
		# sentence if it covered two files.
		"qr_attachment": card.get("attachment"),
		"card_attachment": attachment,
		"card_html": html,
		"pdf_bytes": len(rendered["pdf"]) if rendered["pdf"] else 0,
		"print_format": print_format,
	}
	summary = f"ID card for {card.get('employee_name') or employee} ({badge_id})"
	summary += (
		" rendered and attached to their Employee record"
		if attachment.get("attached")
		else " — the card was built and not attached as a PDF"
	)
	return ToolResult(data=data, summary=summary)


def _card_pdf(html: str) -> dict:
	"""`{"pdf": bytes|None, "note": str}` — the card as a PDF where this bench can.

	NEVER RAISES AND NEVER IMPORTS AT MODULE LEVEL. `frappe.utils.pdf` pulls in
	pdfkit, which pulls in a subprocess call to a binary; on a bench without it
	the import itself is the failure, and an ImportError at the top of this file
	would take every badge tool down with it rather than one PDF.
	"""
	try:
		from frappe.utils.pdf import get_pdf
	except Exception as exc:  # pragma: no cover - depends on the bench image
		return {
			"pdf": None,
			"note": (
				f"this bench has no PDF renderer ({type(exc).__name__}), so the card was built "
				f"as HTML and not attached. `card_html` in this result is the same layout the "
				f"Desk's 'Employee Badge Card' print format uses; printing it from the browser "
				f"produces the same card. Installing wkhtmltopdf is what makes this attach."
			),
		}
	try:
		pdf = get_pdf(html)
	except Exception as exc:
		return {
			"pdf": None,
			"note": (
				f"the PDF renderer failed ({type(exc).__name__}: {exc}), so the card was built "
				f"as HTML and not attached. The badge itself was issued and its QR is attached."
			),
		}
	return {"pdf": pdf, "note": ""} if pdf else {"pdf": None, "note": "the PDF renderer produced nothing."}


# ── 4. resolve_badge ─────────────────────────────────────────────────────


def resolve_badge(args: dict) -> ToolResult:
	"""Read-only. Who holds this badge — the call between a scan and a name.

	THE READ THAT DID NOT EXIST. `get_employee` could answer "does this person
	have a badge?"; nothing could answer "whose badge is this?", which is the
	question a crew clock asks on every scan (`add_worker_to_shift` takes an
	Employee docname and a scanner produces a badge string) and the question a
	capture loop needs to put a picker's NAME on screen instead of a code.

	IT REFUSES RATHER THAN ANSWERING EMPTY. An unknown badge, a retired one and
	one belonging to somebody who has left each get their own sentence, because
	from the foreman's side those are three different situations with three
	different fixes and "not found" would collapse them into one.

	`shift` IS OPTIONAL AND ANSWERS THE SECOND HALF OF THE QUESTION. Given one,
	the result carries `on_shift` and `joined_at` — whether this person is
	actually clocked in right now — which is what turns a badge scan at a bin
	trailer from an identification into an admission."""
	_require()
	badge_id = as_str(args, "badge_id") or as_str(args, "badge", required=True)
	wanted_company = resolve_company(as_str(args, "company"), required=False)

	shape = bucket_bridge.validate_badge_id(badge_id)
	if shape:
		# The scan is NOT quoted here beyond what `validate_badge_id` already
		# allows itself: this is the branch a `generate_mobile_login_qr` payload
		# takes, and a refusal that echoed it would put an api_secret in a
		# ValidationError, in the audit row's arguments and on a screen.
		raise ToolError(
			f"that scan is not a badge ID: {'; '.join(shape)}. Nothing that was scanned here "
			"identifies a worker."
		)

	row = frappe.db.get_value(
		BADGE_DOCTYPE, badge_id, ["name", "badge_id", "company", "employee", "active", "notes"], as_dict=True
	)
	# A badge belonging to an entity the caller named a different one for is
	# reported as unknown rather than as "wrong company": confirming that a
	# badge exists somewhere else on the site is a fact this read has no reason
	# to hand to somebody holding a card.
	if not row or (wanted_company and str(row.get("company")) != wanted_company):
		raise ToolError(
			f"no badge {badge_id!r} in this company's register. Nothing was issued with that ID "
			"— it may be another site's card, or not a badge at all."
		)
	if not row.get("active"):
		raise ToolError(
			f"badge {badge_id!r} was retired and no longer resolves to anybody. It was last held "
			f"by {row.get('employee')}; a reissued card has its own ID."
		)

	person = str(row.get("employee") or "")
	wanted = compat.existing_fields(EMPLOYEE, list(_EMPLOYEE_FIELDS))
	employee = (frappe.db.get_value(EMPLOYEE, person, wanted, as_dict=True) if person else None) or {}
	if not employee:
		raise ToolError(
			f"badge {badge_id!r} maps to Employee {person!r}, which is not on this site. The "
			"register is inconsistent — remap it with link_badge_to_employee."
		)
	status = str(employee.get("status") or "Active")
	if status != "Active":
		raise ToolError(
			f"badge {badge_id!r} belongs to {person} ({employee.get('employee_name')}), whose "
			f"employment status is {status}, not Active. Retire the badge, or reactivate them."
		)

	data = {
		"badge_id": row["badge_id"],
		"company": row.get("company"),
		"active": True,
		"employee": person,
		"employee_name": employee.get("employee_name") or person,
		"employee_number": employee.get("employee_number") or None,
		"designation": employee.get("designation") or None,
		"status": status,
		"photo_url": employee.get("image") or None,
		"photo_placeholder": _initials(employee.get("employee_name") or person),
		"notes": row.get("notes") or None,
	}

	shift = as_str(args, "shift")
	if shift:
		data.update(_shift_membership(shift, person))

	summary = f"badge {row['badge_id']} → {person} ({data['employee_name']})"
	if shift:
		summary += ", on shift" if data.get("on_shift") else f", NOT on shift {shift}"
	return ToolResult(data=data, summary=summary)


def _shift_membership(shift: str, employee: str) -> dict:
	"""Whether this person is currently on that shift's crew.

	`on_shift` is False rather than an error for a shift that does not exist or
	has closed, and `shift_state` says which — a crew clock asking "may this
	badge log a bucket against the shift I have open" wants an answer it can put
	on a screen, and a phone holding a stale shift name is an ordinary thing.
	"""
	if not compat.doctype_exists(FARM_SHIFT):
		return {"shift": shift, "on_shift": False, "shift_state": "no Farm Shift doctype on this site"}
	if not frappe.db.exists(FARM_SHIFT, shift):
		return {"shift": shift, "on_shift": False, "shift_state": "no such shift"}

	doc = frappe.get_doc(FARM_SHIFT, shift)
	if doc.get("end_datetime"):
		state = "closed"
	else:
		state = "open"
	for entry in doc.get("crew") or []:
		if str(entry.get("employee")) == employee:
			return {
				"shift": shift,
				"on_shift": state == "open" and not entry.get("left_at"),
				"shift_state": state,
				"joined_at": str(entry.get("joined_at") or "") or None,
				"left_at": str(entry.get("left_at") or "") or None,
			}
	return {"shift": shift, "on_shift": False, "shift_state": state, "joined_at": None, "left_at": None}


# ── request_badge_photo / set_employee_photo ── v0.214.0 ────────────────────
#
# docs/design/badge_photo_and_fixed_assets.md, Part A. The model, the picture
# and the completion handler are `erpnext_mcp/badge_photo.py`.
def _photo_actor() -> str:
	from .. import security

	return security.caller_identity() or str(getattr(frappe.session, "user", "") or "")


def request_badge_photo(args: dict) -> ToolResult:
	"""Raise the "Badge photo" task for one person, or answer the one already open."""
	from .. import badge_photo

	actor = _photo_actor()
	employee = as_str(args, "employee") or badge_photo.employee_of(actor)
	if not employee:
		raise ToolError(
			"employee is required — whose badge photo. (A phone may leave it out: it is then the "
			"caller's own.) Nothing was changed."
		)
	row = _employee_row(employee)
	badge_photo.require_may(actor, row["name"], "ask for a badge photo")
	assign_to = as_str(args, "assign_to")
	if assign_to:
		assign_to = _employee_row(assign_to)["name"]
	answer = badge_photo.request(row["name"], assign_to)
	answer.pop("_task", None)
	return ToolResult(
		data={
			**answer,
			"employee_name": row.get("employee_name") or row["name"],
			"has_photo": bool(row.get("image")),
		},
		summary=(
			f"{answer['task']} is already open for {row.get('employee_name') or row['name']}"
			if answer["already"]
			else f"raised {answer['task']}: badge photo for {row.get('employee_name') or row['name']}"
		),
	)


def set_employee_photo(args: dict) -> ToolResult:
	"""Make a file already on the site the Employee's badge photo: cropped, resized, stripped."""
	from .. import badge_photo

	actor = _photo_actor()
	row = _employee_row(as_str(args, "employee", required=True))
	badge_photo.require_may(actor, row["name"], "set a badge photo")
	reference = as_str(args, "file_url") or as_str(args, "file")
	if not reference:
		raise ToolError("file_url (or file, a File docname) is required. Nothing was changed.")
	answer = badge_photo.set_photo(row["name"], reference, actor)
	return ToolResult(
		data={
			**answer,
			"note": (
				"Cropped to 4:5 about its centre, resized to 600 x 750 and saved with no camera "
				"metadata, as a private file on the Employee. The previous photo's file is kept."
			),
		},
		summary=f"badge photo set for {answer['employee_name']} ({answer['width']} x {answer['height']})",
		docstatus_delta="0 → 0 (updated)",
	)
