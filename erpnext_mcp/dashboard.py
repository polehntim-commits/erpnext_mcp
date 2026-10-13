# SPDX-License-Identifier: MIT
"""The Compliance Command Center: what is due, what is late, and how ready you are.

WHY IT IS A FRAPPE DASHBOARD AND NOT A PAGE THIS APP DRAWS. Everything on it is
a standard `Dashboard`, `Dashboard Chart` and `Number Card`, which means it
renders in the Desk with the site's own theme, respects the reading user's
permissions, drills through to the underlying list view on a click, and keeps
working when Frappe changes its front end. A bespoke page would look better for
one release and be a maintenance liability for every one after it. The landing
page is `/app/compliance-command-center`, which is Frappe's own route for the
dashboard named below and not a route this app registers.

WHY IT IS BUILT BY AN INSTALLER RATHER THAN SHIPPED AS `fixtures`. `test_hooks.py`
forbids the `fixtures` hook by name, and this is exactly why. A fixture is
imported by `bench migrate` with no ability to look at what is already there: an
operator who reordered their cards, changed a colour or deleted a chart they did
not want would get it silently put back on the next migrate, every time, forever.
So this checks before it writes — a card that exists is left alone, including all
the ways somebody has since edited it — and only the ones that are genuinely
missing are created. Re-running is a no-op, which is asserted by a test.

THE READINESS SCORE IS THE ONE NUMBER SOMEBODY ACTS ON. Everything else on the
dashboard is a count, and a count only means something to a person who already
knows what normal looks like. "Eleven open warnings" is not actionable on a
Tuesday in July. `audit_readiness_score` — resolved alerts over all alerts raised,
as a percentage — is comparable to yesterday's, which is the property that makes
a number worth putting on a wall. It is computed in `readiness()` rather than in a
chart, because it is a ratio of two collections and a Number Card can only count
one.

WHAT THE SCORE DELIBERATELY DOES NOT DO: reward dismissing things. A human
dismissal counts as resolved — because somebody looked and decided, and that IS
the work — but the reason is mandatory, and `dismissal_quality` reports how many
of the resolutions were auto-dismissals (the condition genuinely went away)
against human ones. An operation whose score is 95% entirely through dismissals
is a different operation from one whose score is 95% because the work got done,
and the dashboard says which.

NOTHING HERE RAISES — BUT IT REPORTS, AND v0.16.0 PROVED WHY THAT MATTERS. Every
builder here catches its own exceptions into `report["failed"]` and returns. In
v0.16.0 `install.py` then threw the report away, so when the Farm Task Dispatch
Kanban failed to insert on a real site the migration printed nothing, reported
success, and the board simply did not exist. Silence in an installer is not
safety; it is a bug that has been given somewhere to hide. `install.py` now prints
every entry in `failed`, and the two are only useful together.

NOTHING HERE ASSUMES ANOTHER APP'S SELECT OPTIONS, AND THAT IS THE OTHER HALF OF
THE SAME LESSON. `Kanban Board Column.indicator` is Frappe's field, not this
app's, and its option list has changed across the versions this app supports.
v0.16.0 hardcoded `"gray"`; on Tim's site the options were capitalised, the
insert threw, and the swallowed exception did the rest. `_select_value` now reads
the options off the site and matches case-insensitively, and drops the value
entirely when nothing matches — a column with no colour is cosmetic, a board that
does not exist is not. The rule generalises: this app validates its OWN Selects
against its own JSON, and asks the site about everybody else's.
"""

from __future__ import annotations

import json

import frappe

from . import compat, kpi
from .alerts import ALERT_DOCTYPE, SEVERITY_CRITICAL, SEVERITY_INFO, SEVERITY_WARNING

DASHBOARD = "Dashboard"
CHART = "Dashboard Chart"
CARD = "Number Card"


def _select_value(doctype: str, fieldname: str, wanted: str):
	"""One Select value, matched against what the SITE says the options are.

	Returns the option in the site's own casing, or None when this version offers
	no match — and None means "do not set the field", which is always safer than
	setting a value the doctype will refuse. A field with no options at all is a
	customised or runtime-populated Select and the value is passed through, which
	is what Frappe's own validation does.

	THE WHOLE POINT IS THAT THIS APP DOES NOT KNOW. `Kanban Board Column`
	belongs to Frappe and its colour palette has been lowercase, capitalised and
	a list of hex codes in different releases. Guessing produced v0.16.0's
	silently missing dispatch board.
	"""
	wanted = str(wanted or "").strip()
	if not wanted:
		return None
	try:
		field = compat.field_meta(doctype, fieldname)
	except Exception:
		return None
	if field is None:
		return None
	options = [line.strip() for line in str(field.get("options") or "").split("\n") if line.strip()]
	if not options:
		return wanted
	for option in options:
		if option.lower() == wanted.lower():
			return option
	return None


#: The dashboard's name, and therefore its route: Frappe slugifies this into
#: `/app/compliance-command-center`.
DASHBOARD_NAME = "Compliance Command Center"

MODULE = "ERPNext MCP"


def card_filters(doctype: str, clauses: dict) -> str:
	"""`filters_json` in the LIST format a Number Card can actually run.

	v0.18.5. Frappe accepts either shape at `frappe.db.get_list` — `{"state":
	"Available"}` and `[["Farm Task", "state", "=", "Available"]]` mean the same
	thing there — so every card on this dashboard was written in the dict shape,
	which is shorter and reads better, and every card RENDERED. What none of them
	survived was the comparison arrow: `number_card.get_result` computes the
	period-over-period difference by appending a date clause to the caller's own
	filters —

	    filters = frappe.parse_json(doc.filters_json)
	    filters.append([doc.document_type, "creation", "<", to_date])

	— and `dict` has no `.append`. Python resolves `{}.append` to nothing and
	Frappe's own `__getattr__` shim turns that into `'NoneType' object is not
	callable`, so the Farm Task Dispatch workspace answered Internal Server Error
	on load rather than naming the card that could not be counted. A dict is a
	valid filter to query WITH and an invalid filter to build ON, and only the
	list shape is both.

	Values follow Frappe's own dict convention: a bare value means `=`, and a
	two-element `[operator, operand]` carries its own operator through.
	"""
	out = []
	for field, value in clauses.items():
		if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], str):
			out.append([doctype, field, value[0], value[1]])
		else:
			out.append([doctype, field, "=", value])
	return json.dumps(out)


def _live_alert_filters(extra: dict | None = None) -> str:
	"""JSON filters selecting alerts that are actually on the calendar.

	Dismissed is off it. Snoozed is not filtered here and is deliberately left in:
	a Frappe filter cannot express "snoozed_until is in the past OR unset" in one
	clause, and a card that silently omitted snoozed items would let somebody snooze
	their way to a clean dashboard. `get_compliance_calendar` applies the snooze
	rule properly; the card counts what has been raised.
	"""
	filters = {"dismissed": 0}
	filters.update(extra or {})
	return card_filters(ALERT_DOCTYPE, filters)


#: The Number Cards, in the order they read across the top.
#:
#: Critical first because that is the order somebody works them in, and the whole
#: point of the severity split is that a list where everything is urgent is a list
#: nobody reads.
CARDS = (
	{
		"label": "Critical Compliance Alerts",
		"document_type": ALERT_DOCTYPE,
		"function": "Count",
		"filters_json": _live_alert_filters({"severity": SEVERITY_CRITICAL}),
		"color": "#e24c4c",
		"why": "Something has stopped being lawful. Somebody cannot work, or a block cannot be entered.",
	},
	{
		"label": "Warning Compliance Alerts",
		"document_type": ALERT_DOCTYPE,
		"function": "Count",
		"filters_json": _live_alert_filters({"severity": SEVERITY_WARNING}),
		"color": "#f5a623",
		"why": "Needs doing this month. Left alone, most of these become Critical on their own.",
	},
	{
		"label": "Info Compliance Alerts",
		"document_type": ALERT_DOCTYPE,
		"function": "Count",
		"filters_json": _live_alert_filters({"severity": SEVERITY_INFO}),
		"color": "#449cf0",
		"why": "Context. Worth knowing, not worth interrupting anybody for.",
	},
	{
		"label": "Overdue Corrective Actions",
		"document_type": ALERT_DOCTYPE,
		"function": "Count",
		"filters_json": _live_alert_filters({"alert_type": "audit_action_overdue"}),
		"color": "#e24c4c",
		"why": (
			"The one an auditor is certain to ask about on the next visit, and the gate "
			"generate_audit_packet refuses a period on."
		),
	},
	{
		"label": "Certificates Expiring",
		"document_type": ALERT_DOCTYPE,
		"function": "Count",
		"filters_json": _live_alert_filters({"category": "Certifications"}),
		"color": "#f5a623",
		"why": "Each one is a buyer requirement or a licence to operate that is running out.",
	},
	{
		"label": "Open Audit Events",
		"document_type": "Audit Event",
		"function": "Count",
		"filters_json": card_filters("Audit Event", {"corrective_actions_closed": ["is", "not set"]}),
		"color": "#7575ff",
		"why": "Audits whose findings have not all been closed. Not the same as audits that failed.",
	},
)


#: The charts. Two of the three are time series, because the question a compliance
#: dashboard actually answers is "are we getting better or worse", and a snapshot
#: cannot answer it.
CHARTS = (
	{
		"chart_name": "Compliance Alerts by Category",
		"chart_type": "Group By",
		"document_type": ALERT_DOCTYPE,
		"group_by_type": "Count",
		"group_by_based_on": "category",
		"type": "Donut",
		"filters_json": _live_alert_filters(),
		"number_of_groups": 9,
		"why": (
			"Where the work is. Categories are chosen so a whole slice can be cleared in one "
			"afternoon — every housing item is one walk round the camp."
		),
	},
	{
		"chart_name": "Compliance Alerts Raised Over Time",
		"chart_type": "Count",
		"document_type": ALERT_DOCTYPE,
		"based_on": "first_seen",
		"time_interval": "Monthly",
		"timespan": "Last Year",
		"timeseries": 1,
		"type": "Line",
		# v0.18.3: Dashboard Chart's `filters_json` is mandatory even for a chart with
		# no filters, and Frappe refuses to insert one where it is None. The three
		# charts below were the ones after_migrate warned about every deploy.
		#
		# v0.18.5 makes the empty value `"[]"` rather than v0.18.3's `"{}"`. Both
		# parse to something falsy and both render today, but a TIMESERIES chart —
		# which these two are — has its date range appended to the caller's own
		# filters, and only one of the two shapes can be appended to. See
		# `card_filters` for the Number Card version of the same crash, which is
		# not latent and took the dispatch workspace down.
		"filters_json": "[]",
		"why": (
			"Raised, not open — `first_seen` is never moved forward, so this is the shape of "
			"how often the operation drifts out of compliance rather than of how fast it "
			"catches up."
		),
	},
	{
		"chart_name": "Certificate Expirations Ahead",
		"chart_type": "Count",
		"document_type": "Certification",
		"based_on": "expiration_date",
		"time_interval": "Monthly",
		"timespan": "Last Year",
		"timeseries": 1,
		"type": "Bar",
		"filters_json": "[]",
		"why": (
			"The renewal calendar as a shape. Three certificates expiring in one month is a "
			"week of somebody's life, and it is visible a year out."
		),
	},
	{
		"chart_name": "Regulatory Filings by Agency",
		"chart_type": "Group By",
		"document_type": "Regulatory Filing",
		"group_by_type": "Count",
		"group_by_based_on": "agency",
		"type": "Bar",
		"number_of_groups": 11,
		"filters_json": "[]",
		"why": "Which agencies this operation actually deals with, which is rarely the list anybody expects.",
	},
)


#: v0.16.0. The dispatch board is a Frappe KANBAN BOARD and not a page this app
#: draws, for exactly the reasons the Command Center is a Dashboard: a foreman
#: drags a card between columns and Frappe writes the field, on desktop and on a
#: phone, with the site's own permissions and the site's own theme, and it keeps
#: working when Frappe changes its front end. There is no custom UI in this
#: release and there does not need to be.
#:
#: The route is Frappe's own: `/app/farm-task/view/kanban/Farm Task Dispatch`.
#: The Sprint 8 note asked for `/app/farm-task-dispatch`, which is a Workspace
#: route rather than a board route — a Workspace is a landing page of shortcuts,
#: and it is what `WORKSPACE_SHORTCUTS` below builds where the site has the
#: doctype for one. The board is the thing that works either way.
KANBAN = "Kanban Board"
KANBAN_COLUMN = "Kanban Board Column"
WORKSPACE = "Workspace"

DISPATCH_BOARD_NAME = "Farm Task Dispatch"
DISPATCH_DOCTYPE = "Farm Task"

#: The column each state gets, and its indicator colour. In board order, which is
#: left-to-right the order work moves: pool, claimed, being done, found something,
#: done. Rejected and Cancelled are columns too — a rejection is a first-class
#: state and a board that hid it would hide the one thing a foreman has to act on.
DISPATCH_COLUMNS = (
	("Draft", "gray"),
	("Available", "blue"),
	("Claimed", "purple"),
	("In-Progress", "orange"),
	# v0.79.0. Paused sits between In-Progress and Awaiting-Review, which is where
	# it sits in a day: the work is somebody's, it is not finished, and it is not
	# being done this minute. Yellow rather than orange so a board read across a
	# yard tells the two apart at a glance — "who is actually working" is the
	# question this column exists to keep answerable.
	("Paused", "yellow"),
	# v0.324.0. On Hold: nobody should pick it up yet (task_holds). Grey, after Paused — it is waiting on
	# something outside the crew, and the card says what.
	("On Hold", "gray"),
	("Awaiting-Review", "red"),
	("Completed", "green"),
	("Rejected", "red"),
	("Cancelled", "gray"),
	# Merged is last and grey because it is an archive column: nothing is worked
	# out of it and every card in it points at another card. It is on the board
	# rather than hidden so a foreman looking for a job somebody swears they
	# raised finds it, with the arrow to where it went.
	("Merged", "gray"),
)

#: The shortcut row across the top of the workspace, in the order somebody uses
#: them. "Raise a Task" is first and is a `doc_view: New` shortcut — the
#: quick-add button — because the commonest thing a foreman does on this page is
#: put work on the board, and making them open a list first to find New is the
#: kind of friction that ends with the work being shouted across a yard instead.
DISPATCH_SHORTCUTS = (
	{"label": "Raise a Task", "link_to": DISPATCH_DOCTYPE, "type": "DocType", "doc_view": "New"},
	{
		"label": "Dispatch Board",
		"link_to": DISPATCH_DOCTYPE,
		"type": "DocType",
		"doc_view": "Kanban",
		"kanban_board": DISPATCH_BOARD_NAME,
	},
	{"label": "All Tasks", "link_to": DISPATCH_DOCTYPE, "type": "DocType", "doc_view": "List"},
	{"label": "Assignments", "link_to": "Farm Task Assignment", "type": "DocType", "doc_view": "List"},
	{"label": "Compliance Calendar", "link_to": ALERT_DOCTYPE, "type": "DocType", "doc_view": "List"},
)

#: The Number Cards on the workspace.
#:
#: THE LAST TWO ARE A PAIR AND HAVE TO BE READ TOGETHER. A Number Card counts one
#: collection and cannot divide two, so "what fraction of the board came from the
#: compliance calendar" is shown as the two counts side by side rather than as a
#: percentage the card would have to invent. That fraction is the honest measure
#: of whether the calendar is driving work or being read and ignored, and
#: `list_dispatch_board` returns it as a single number for a caller that wants one.
DISPATCH_NUMBER_CARDS = (
	{
		"label": "Tasks in the Pool",
		"document_type": DISPATCH_DOCTYPE,
		"function": "Count",
		"filters_json": card_filters(DISPATCH_DOCTYPE, {"state": "Available"}),
		"color": "#449cf0",
		"why": "Work nobody has taken yet. The number a foreman is trying to get to zero.",
	},
	{
		"label": "Open Critical Tasks",
		"document_type": DISPATCH_DOCTYPE,
		"function": "Count",
		"filters_json": card_filters(
			DISPATCH_DOCTYPE,
			{"urgency": "Critical", "state": ["not in", ["Completed", "Rejected", "Cancelled"]]},
		),
		"color": "#e24c4c",
		"why": "Something has already stopped being lawful and nobody has finished dealing with it.",
	},
	{
		"label": "Tasks Awaiting Review",
		"document_type": DISPATCH_DOCTYPE,
		"function": "Count",
		"filters_json": card_filters(DISPATCH_DOCTYPE, {"state": "Awaiting-Review"}),
		"color": "#f5a623",
		"why": (
			"Work that was DONE and found something. The register moved; what needs a person is the finding."
		),
	},
	{
		"label": "Tasks Raised From Alerts",
		"document_type": DISPATCH_DOCTYPE,
		"function": "Count",
		"filters_json": card_filters(DISPATCH_DOCTYPE, {"source_alert": ["is", "set"]}),
		"color": "#7575ff",
		"why": "The compliance calendar turned into work somebody was actually sent to do.",
	},
	{
		"label": "Tasks Raised By Hand",
		"document_type": DISPATCH_DOCTYPE,
		"function": "Count",
		"filters_json": card_filters(DISPATCH_DOCTYPE, {"source_alert": ["is", "not set"]}),
		"color": "#98d85b",
		"why": "The operation's own work. Read against the card beside it, not on its own.",
	},
)

#: The charts on the workspace: what kind of work is outstanding, and how urgent.
DISPATCH_CHARTS = (
	{
		"chart_name": "Farm Tasks by Type",
		"chart_type": "Group By",
		"document_type": DISPATCH_DOCTYPE,
		"group_by_type": "Count",
		"group_by_based_on": "task_type",
		"type": "Donut",
		"filters_json": card_filters(
			DISPATCH_DOCTYPE, {"state": ["not in", ["Completed", "Rejected", "Cancelled"]]}
		),
		"number_of_groups": 10,
		"why": (
			"What kind of work is outstanding. A board that is nine-tenths inspections is a "
			"different afternoon from one that is nine-tenths repairs."
		),
	},
	{
		"chart_name": "Farm Tasks by Urgency",
		"chart_type": "Group By",
		"document_type": DISPATCH_DOCTYPE,
		"group_by_type": "Count",
		"group_by_based_on": "urgency",
		"type": "Bar",
		"filters_json": card_filters(
			DISPATCH_DOCTYPE, {"state": ["not in", ["Completed", "Rejected", "Cancelled"]]}
		),
		"number_of_groups": 4,
		"why": (
			"The shape of the queue. If this is flat at Critical, the urgency scale has stopped "
			"meaning anything and the board has stopped being read."
		),
	},
)

#: The link cards down the side: the registers a completion writes into.
DISPATCH_LINK_CARDS = (
	("Compliance Records", ("Housing Inspection", "Detector Test", "Water Test")),
	("Dispatch", (DISPATCH_DOCTYPE, "Farm Task Assignment")),
	("The Camp", ("Housing Unit", "Housing Assignment", "Irrigation Zone")),
)


def dispatch_board_available() -> bool:
	"""Whether this site can hold a Kanban Board for Farm Task."""
	try:
		return all(compat.doctype_exists(doctype) for doctype in (KANBAN, DISPATCH_DOCTYPE))
	except Exception:
		return False


def install_dispatch_board() -> dict:
	"""Build the Farm Task Dispatch Kanban and its landing page. Idempotent, never raises.

	Same contract as `install_command_center`: anything that already exists is
	left exactly as it is, including every column an operator has since
	reordered, renamed or deleted. Re-running is a no-op.

	WITH TWO REPAIR CLAUSES. The first, added in v0.16.1: a Workspace that exists
	but is EMPTY is filled in. v0.16.0 created the workspace with `content: "[]"`,
	which renders a page with nothing on it — and the plain existence check would
	have skipped that page forever on every site that took the bad release. An
	empty page is not an arrangement somebody chose; a page with anything on it
	is, and that one is still left alone.

	The second, added in v0.18.5: a card whose `filters_json` is DICT-SHAPED is
	rewritten into the list shape. See `_repair_filters`. It is the same argument
	as the first — every site that took v0.16.0 through v0.18.4 has five cards
	that cannot be counted, and leaving them alone out of politeness leaves the
	workspace answering Internal Server Error forever.
	"""
	report = {
		"board": DISPATCH_BOARD_NAME,
		"route": f"/app/{_slug(DISPATCH_DOCTYPE)}/view/kanban/{DISPATCH_BOARD_NAME}",
		"workspace_route": f"/app/{_slug(DISPATCH_BOARD_NAME)}",
		"columns": [state for state, _colour in DISPATCH_COLUMNS],
		"created": False,
		"existed": False,
		"workspace_created": False,
		"workspace_existed": False,
		"workspace_filled": False,
		"created_cards": [],
		"created_charts": [],
		"existing_cards": [],
		"existing_charts": [],
		"repaired_filters": [],
		"failed": [],
		"available": dispatch_board_available(),
	}
	if not report["available"]:
		report["note"] = (
			"this site does not have both the Kanban Board and Farm Task doctypes, so there is "
			"nothing to build the dispatch board out of. Every task on it is still readable "
			"through list_dispatch_board, which returns the same columns as JSON."
		)
		return report

	_build_kanban(report)
	for spec in DISPATCH_NUMBER_CARDS:
		_build(CARD, "label", spec, report, "cards")
	for spec in DISPATCH_CHARTS:
		_build(CHART, "chart_name", spec, report, "charts")
	_repair_filters(CARD, "label", DISPATCH_NUMBER_CARDS, report)
	_repair_filters(CHART, "chart_name", DISPATCH_CHARTS, report)
	_build_dispatch_workspace(report)
	return report


def _build_kanban(report: dict) -> None:
	"""Create the Kanban Board, twice if it has to.

	THE SECOND ATTEMPT DROPS THE COLUMNS, and that is the lesson of v0.16.0 turned
	into code. The columns are the only part of this document made of another
	app's Select values, so they are the only part that can be refused by a
	Frappe version this app did not anticipate. A board with no columns still
	works — Frappe builds them from the distinct values of the field on first view
	— and a board that does not exist does not. Degrade; do not vanish.

	The name is FORCED rather than left to Frappe's autoname, because
	`/app/farm-task/view/kanban/Farm Task Dispatch` is a route this app documents
	in three places and a board named anything else is a board nobody finds. If a
	version ignores the flag, the real name is reported rather than assumed.
	"""
	if frappe.db.exists(KANBAN, DISPATCH_BOARD_NAME):
		report["existed"] = True
		return

	attempts = []
	for with_columns in (True, False):
		savepoint = _savepoint()
		try:
			doc = _new_kanban(with_columns)
			doc.insert(ignore_permissions=True)
			report["created"] = True
			report["board_name"] = doc.name
			report["columns_written"] = len(doc.get("columns") or [])
			if not with_columns:
				report["note_columns"] = (
					"The board was created WITHOUT its columns: this Frappe version refused them, "
					"and a board with no columns still works — Frappe builds them from the distinct "
					f"values of `state` the first time somebody opens it. Reason: {attempts[0]}"
				)
			if doc.name != DISPATCH_BOARD_NAME:
				report["failed"].append(
					{
						"name": DISPATCH_BOARD_NAME,
						"reason": (
							f"this Frappe version named the board {doc.name!r} rather than "
							f"{DISPATCH_BOARD_NAME!r}, so the documented route "
							f"{report['route']} will not find it. Open it from the Farm Task list "
							"view instead."
						),
					}
				)
			return
		except Exception as exc:
			attempts.append(f"{type(exc).__name__}: {exc}")
			_rollback_to(savepoint)

	report["failed"].append(
		{
			"name": DISPATCH_BOARD_NAME,
			"reason": (
				"the Kanban Board could not be created, with or without its columns: "
				+ "; ".join(attempts)
				+ ". Every task is still readable through list_dispatch_board, which returns the "
				"same columns as JSON, and a board can be made by hand from the Farm Task list "
				"view."
			),
		}
	)


def _new_kanban(with_columns: bool):
	doc = frappe.new_doc(KANBAN)
	doc.kanban_board_name = DISPATCH_BOARD_NAME
	doc.reference_doctype = DISPATCH_DOCTYPE
	# The field the columns ARE. Getting this wrong produces a board that renders
	# and groups by nothing, which looks like a working board.
	doc.field_name = "state"
	# Force the docname: the route is documented and a board under any other name
	# is one nobody finds. Frappe honours `flags.name_set`; versions that do not
	# are caught by the check on the way out.
	doc.name = DISPATCH_BOARD_NAME
	doc.flags.name_set = True
	if compat.has_field(KANBAN, "private"):
		doc.private = 0
	if compat.has_field(KANBAN, "show_labels"):
		doc.show_labels = 1
	if with_columns and compat.has_field(KANBAN, "columns"):
		for order, (state, colour) in enumerate(DISPATCH_COLUMNS, start=1):
			row = {"column_name": state}
			indicator = _select_value(KANBAN_COLUMN, "indicator", colour)
			if indicator:
				row["indicator"] = indicator
			status = _select_value(KANBAN_COLUMN, "status", "Active")
			if status:
				row["status"] = status
			if compat.has_field(KANBAN_COLUMN, "order"):
				row["order"] = order
			doc.append("columns", row)
	return doc


def _savepoint():
	"""A named savepoint, where this Frappe has them. Returns None where it does not.

	A failed insert inside `after_migrate` must not be allowed to poison the
	migration's transaction, and must not roll back anything the migration
	already did. A savepoint is the only tool that does both.
	"""
	name = "erpnext_mcp_dispatch_board"
	try:
		frappe.db.savepoint(name)
		return name
	except Exception:
		return None


def _rollback_to(savepoint) -> None:
	if not savepoint:
		return
	try:
		frappe.db.rollback(save_point=savepoint)
	except Exception:
		pass


def _slug(name: str) -> str:
	return name.replace(" ", "-").lower()


def _build_dispatch_workspace(report: dict) -> None:
	"""The `/app/farm-task-dispatch` landing page, with something actually on it.

	v0.16.0 CREATED THIS PAGE EMPTY, and that was a second, independent bug from
	the same misunderstanding as the first. In a modern Frappe a Workspace renders
	ONLY what its `content` block list names. Rows in `shortcuts`, `links`,
	`number_cards` and `charts` supply the data; `content` decides what appears.
	v0.16.0 wrote the child rows and then set `content` to `[]`, which is a page
	with a title and nothing else — exactly what Tim saw.

	Best-effort throughout, because the Workspace doctype has been rewritten twice
	across the Frappe versions this app supports: every field and every child table
	is written only where the site has it, and a failure is reported rather than
	raised. The Kanban board works without any of this.
	"""
	if not compat.doctype_exists(WORKSPACE):
		report["workspace_note"] = "this site has no Workspace doctype, so there is no landing page to build"
		return
	try:
		existing = frappe.db.exists(WORKSPACE, DISPATCH_BOARD_NAME)
		if existing and not _workspace_is_empty(DISPATCH_BOARD_NAME):
			# Somebody arranged this page. Leave it exactly as they left it.
			report["workspace_existed"] = True
			return

		doc = frappe.get_doc(WORKSPACE, DISPATCH_BOARD_NAME) if existing else frappe.new_doc(WORKSPACE)
		if existing:
			# Repairing a page v0.16.0 shipped blank. Clear the child tables first
			# so a partial set from the bad release cannot be doubled.
			for fieldname in ("shortcuts", "links", "number_cards", "charts"):
				if compat.has_field(WORKSPACE, fieldname):
					doc.set(fieldname, [])
		else:
			doc.name = DISPATCH_BOARD_NAME
			doc.flags.name_set = True

		for fieldname, value in (
			("title", DISPATCH_BOARD_NAME),
			("label", DISPATCH_BOARD_NAME),
			("module", MODULE),
			("icon", "activity"),
			("public", 1),
			("is_hidden", 0),
			("sequence_id", 20.0),
		):
			if compat.has_field(WORKSPACE, fieldname):
				doc.set(fieldname, value)

		content = _workspace_content(doc)
		if compat.has_field(WORKSPACE, "content"):
			doc.content = json.dumps(content)

		doc.save(ignore_permissions=True) if existing else doc.insert(ignore_permissions=True)
		report["workspace_filled" if existing else "workspace_created"] = True
		report["workspace_blocks"] = len(content)
	except Exception as exc:
		report["failed"].append(
			{"name": f"{DISPATCH_BOARD_NAME} workspace", "reason": f"{type(exc).__name__}: {exc}"}
		)


def _workspace_is_empty(name: str) -> bool:
	"""Has anybody put anything on this page?

	An empty `content` is what v0.16.0 wrote and is the thing worth repairing. A
	page with blocks on it is somebody's arrangement and is never touched.
	"""
	try:
		raw = frappe.db.get_value(WORKSPACE, name, "content")
	except Exception:
		return False
	try:
		return not (json.loads(raw) if raw else [])
	except Exception:
		return False


def _workspace_content(doc) -> list:
	"""Build the page, appending each child row as the block that renders it.

	The two have to be written together — a shortcut row with no `shortcut` block
	is invisible, and a block naming a shortcut row that does not exist is a
	rendering error — so this does both in one pass and nothing can drift.
	"""
	content = []

	def block(kind: str, key: str, value: str, col: int) -> None:
		content.append({"id": _slug(f"{kind}-{value}")[:32], "type": kind, "data": {key: value, "col": col}})

	def header(text: str) -> None:
		content.append(
			{
				"id": _slug(f"header-{text}")[:32],
				"type": "header",
				"data": {"text": f'<span class="h4"><b>{text}</b></span>', "col": 12},
			}
		)

	has_shortcuts = compat.has_field(WORKSPACE, "shortcuts")
	if has_shortcuts:
		header("Dispatch")
		for spec in DISPATCH_SHORTCUTS:
			if not compat.doctype_exists(spec["link_to"]):
				continue
			row = {"label": spec["label"], "link_to": spec["link_to"]}
			kind = _select_value("Workspace Shortcut", "type", spec.get("type") or "DocType")
			if kind:
				row["type"] = kind
			view = _select_value("Workspace Shortcut", "doc_view", spec.get("doc_view") or "")
			if view and compat.has_field("Workspace Shortcut", "doc_view"):
				row["doc_view"] = view
			if spec.get("kanban_board") and compat.has_field("Workspace Shortcut", "kanban_board"):
				if frappe.db.exists(KANBAN, spec["kanban_board"]):
					row["kanban_board"] = spec["kanban_board"]
				elif view == "Kanban":
					# A Kanban shortcut with no board behind it is a dead link.
					continue
			doc.append("shortcuts", row)
			block("shortcut", "shortcut_name", spec["label"], 3)

	if compat.has_field(WORKSPACE, "number_cards") and compat.doctype_exists(CARD):
		header("How the board stands")
		for spec in DISPATCH_NUMBER_CARDS:
			if not frappe.db.exists(CARD, spec["label"]):
				continue
			doc.append("number_cards", {"number_card_name": spec["label"], "label": spec["label"]})
			block("number_card", "number_card_name", spec["label"], 4)

	if compat.has_field(WORKSPACE, "charts") and compat.doctype_exists(CHART):
		for spec in DISPATCH_CHARTS:
			if not frappe.db.exists(CHART, spec["chart_name"]):
				continue
			doc.append("charts", {"chart_name": spec["chart_name"], "label": spec["chart_name"]})
			block("chart", "chart_name", spec["chart_name"], 6)

	if compat.has_field(WORKSPACE, "links"):
		for card_name, links in DISPATCH_LINK_CARDS:
			present = [link for link in links if compat.doctype_exists(link)]
			if not present:
				continue
			break_row = {"label": card_name, "link_count": len(present)}
			kind = _select_value("Workspace Link", "type", "Card Break")
			if kind:
				break_row["type"] = kind
			doc.append("links", break_row)
			for link in present:
				link_row = {"label": link, "link_to": link}
				kind = _select_value("Workspace Link", "type", "Link")
				if kind:
					link_row["type"] = kind
				link_type = _select_value("Workspace Link", "link_type", "DocType")
				if link_type:
					link_row["link_type"] = link_type
				doc.append("links", link_row)
			block("card", "card_name", card_name, 4)

	return content


def available() -> bool:
	"""Whether this site has the dashboard doctypes at all.

	Frappe has shipped all three since v13, but a site can have them disabled or
	a stripped install can lack them, and a dashboard that cannot be built is not
	a reason for a migration to fail.
	"""
	try:
		return all(compat.doctype_exists(doctype) for doctype in (DASHBOARD, CHART, CARD))
	except Exception:
		return False


#: v0.19.5. The KPI chart, and the only chart in this file whose source is a
#: REPORT rather than a count over a doctype.
#:
#: IT HAS TO BE. Sustainable CF/Acre is not a number of rows and is not a sum of
#: a column: it is (normalized operating cash flow - maintenance capex) divided by
#: acres weighted by how many days of the quarter each block was productive. A
#: `Group By` or `Count` chart cannot express any of the three terms, let alone
#: the ratio. `report/sustainable_cf_per_acre_by_quarter` runs the same
#: `services.sustainable_cf_per_acre.compute` every tool and every packet runs,
#: which is what stops the chart and the tool ever disagreeing about the figure.
#:
#: PER COMPANY IS NOT A CHART PROPERTY HERE. The report takes `company` as a
#: filter and the chart carries none, so it opens on the site's company and an
#: operator picks the entity — which is the honest shape for a multi-entity
#: family office where OM Family Office, Highland LLC and Constancy Farms have
#: different lenders asking different questions. A chart per company, installed
#: at migrate time, would be a chart per company as the entities stood on the day
#: somebody upgraded.
KPI_REPORT_NAME = "Sustainable CF Per Acre by Quarter"
KPI_CHART_NAME = "Sustainable CF/Acre by Quarter"

#: v0.19.6. The rolling view, and the DEFAULT one from here on.
#:
#: Each point is a full twelve months rather than a discrete quarter, and that
#: is the whole retrofit. Four calendar quarters on a farm are not comparable
#: with each other — Q3 is harvest and Q1 is pruning, so the quarterly line falls
#: off a cliff every January and climbs back every September whether or not
#: anything happened. A reader who does not already know that reads a crisis.
#: Consecutive rolling points differ only by the month that entered and the month
#: that left, so the line moves when the business moves.
#:
#: `use_report_chart` IS 1 HERE AND 0 ON THE QUARTERLY ONE, which is the one
#: mechanical difference between them. The rolling chart needs a dashed reference
#: rule at the prior-window mean, and a `yMarker` is how frappe-charts draws a
#: reference level — natively dashed, labelled, and not invited to be compared by
#: shape the way a second line would be. A Dashboard Chart can only carry that if
#: the report supplies its own chart config, which a Script Report does by
#: returning a fourth value.
KPI_TTM_REPORT_NAME = "Sustainable CF Per Acre TTM Monthly"
KPI_TTM_CHART_NAME = "Sustainable CF/Acre TTM Monthly"

KPI_CHARTS = (
	{
		"chart_name": KPI_TTM_CHART_NAME,
		"chart_type": "Report",
		"report_name": KPI_TTM_REPORT_NAME,
		# See the note above: the report supplies the line AND the dashed mean.
		"use_report_chart": 1,
		"type": "Line",
		"is_public": 1,
		"filters_json": "{}",
		"why": (
			"The default view, and the one that can be read without knowing the season. Each "
			"point is a rolling twelve months, so pruning, thinning, harvest and the winter "
			"are inside every point exactly once and the line only moves when the business "
			"does. The dashed rule is what the operation's own rolling figure has averaged, "
			"which is the only thing that says whether today's number is good."
		),
	},
	{
		"chart_name": KPI_CHART_NAME,
		"chart_type": "Report",
		"report_name": KPI_REPORT_NAME,
		"use_report_chart": 0,
		"type": "Line",
		"is_public": 1,
		"filters_json": "{}",
		"why": (
			"The discrete quarterly view, kept as a secondary. Four calendar quarters are "
			"what a lender's own pack is laid out in, and lining up with theirs is worth "
			"having — as long as nobody reads the January cliff as an event. The rolling "
			"chart above is the one to read for a trend."
		),
	},
)

#: THE QUARTERLY CHART IS NOT RENAMED, and that is deliberate rather than an
#: omission. v0.19.6 makes the rolling view the default and demotes the discrete
#: quarterly one to a secondary — but a Dashboard Chart's DOCNAME is what a
#: Dashboard, a workspace and anybody's saved link point at, so renaming the
#: existing record would silently empty the dashboards of every site that
#: installed v0.19.5. The demotion is expressed in the `why` above and in the new
#: chart being the one the documentation opens with, which costs a reader one
#: sentence and costs nobody a working dashboard.


def kpi_chart_available() -> bool:
	"""Whether this site can hold the KPI charts: the chart doctype and the reports."""
	try:
		return bool(compat.doctype_exists(CHART) and compat.doctype_exists("Report"))
	except Exception:
		return False


def install_kpi_charts() -> dict:
	"""Install the Sustainable CF/Acre chart. Idempotent, and NEVER raises.

	Same contract as `install_command_center`: a chart that already exists is left
	exactly as somebody has since edited it, and only a genuinely missing one is
	created. See the module docstring on why these are installers rather than
	Frappe `fixtures` — a fixture is imported by `bench migrate` with no ability
	to skip what a site already has.

	THE REPORT IS CHECKED FOR BY NAME rather than assumed. It ships as a standard
	Script Report in this app's own module, so `bench migrate` creates it — but it
	is created by the SAME migrate that runs this, and on the first pass the row
	may not be there yet. A chart pointing at a report that does not exist renders
	an error where a missing chart renders nothing, so the missing case is
	reported and the next migrate builds it.
	"""
	report = {
		"chart": KPI_TTM_CHART_NAME,
		"charts": [spec["chart_name"] for spec in KPI_CHARTS],
		"report": KPI_TTM_REPORT_NAME,
		"created_charts": [],
		"existing_charts": [],
		"failed": [],
		"available": kpi_chart_available(),
	}
	if not report["available"]:
		report["note"] = (
			"this site does not have both the Dashboard Chart and Report doctypes, so there is "
			"nothing to build the KPI charts out of. Every figure behind them is still readable "
			"through get_windowed_report, which returns a great deal more than a chart draws."
		)
		return report
	if not compat.doctype_exists(kpi.DOCTYPE):
		report["available"] = False
		report["note"] = (
			f"this site has no {kpi.DOCTYPE} DocType yet — it ships with erpnext_mcp and arrives "
			"with `bench migrate`. The charts are built on the migrate that creates it."
		)
		return report

	# EACH CHART IS CHECKED AGAINST ITS OWN REPORT, and a missing one takes only
	# that chart down. v0.19.6 ships two, and a site part-way through a migrate
	# may have one Report row and not the other — building the chart that CAN be
	# built is strictly better than refusing both, and the one that could not is
	# named so the next migrate is visibly the fix.
	for spec in KPI_CHARTS:
		if not frappe.db.exists("Report", spec["report_name"]):
			report["failed"].append(
				{
					"name": spec["chart_name"],
					"reason": (
						f"the {spec['report_name']!r} Report is not on this site yet. It ships as "
						"a standard Script Report in this app's module and is created by the same "
						"`bench migrate` that runs this, so the next migrate builds the chart. "
						"Nothing was created: a chart pointing at a report that does not exist "
						"renders an error, where a missing chart renders nothing."
					),
				}
			)
			continue
		_build(CHART, "chart_name", spec, report, "charts")
	return report


def install_command_center() -> dict:
	"""Build or repair the dashboard. Idempotent, and NEVER raises.

	Anything that already exists is left exactly as it is, including every way an
	operator has since edited it — with the one exception `_repair_filters`
	documents, which is a card that cannot be counted at all. Only genuinely
	missing pieces are created. See the module docstring on why this is an
	installer rather than a fixture.
	"""
	report = {
		"dashboard": DASHBOARD_NAME,
		"route": "/app/compliance-command-center",
		"created_cards": [],
		"created_charts": [],
		"existing_cards": [],
		"existing_charts": [],
		"repaired_filters": [],
		"failed": [],
		"available": available(),
	}
	if not report["available"]:
		report["note"] = (
			"this site does not have the Dashboard, Dashboard Chart and Number Card doctypes, "
			"so there is nothing to build the command center out of. Every underlying number "
			"is still readable through get_compliance_calendar."
		)
		return report
	if not compat.doctype_exists(ALERT_DOCTYPE):
		report["available"] = False
		report["note"] = (
			"this site has no Compliance Alert DocType yet — it ships with erpnext_mcp and "
			"arrives with `bench migrate`. The dashboard is built on the migrate that creates it."
		)
		return report

	for spec in CARDS:
		_build(CARD, "label", spec, report, "cards")
	for spec in CHARTS:
		_build(CHART, "chart_name", spec, report, "charts")
	_repair_filters(CARD, "label", CARDS, report)
	_repair_filters(CHART, "chart_name", CHARTS, report)

	_build_dashboard(report)
	return report


def _build(doctype: str, key_field: str, spec: dict, report: dict, bucket: str) -> None:
	name = spec[key_field]
	kind = "cards" if bucket == "cards" else "charts"
	try:
		if frappe.db.exists(doctype, name):
			report[f"existing_{kind}"].append(name)
			return
		# v0.19.5: a chart whose source is a REPORT names no `document_type`, and
		# there is nothing to check it against. The report's own existence is
		# checked by `install_kpi_charts` before it gets here, because a missing
		# report and a missing doctype are different sentences.
		if spec.get("document_type") and not compat.doctype_exists(spec["document_type"]):
			report["failed"].append(
				{
					"name": name,
					"reason": (
						f"{spec.get('document_type')!r} is not on this site, so there is nothing "
						"for this to count. It is built on the migrate that installs it."
					),
				}
			)
			return
		doc = frappe.new_doc(doctype)
		doc.set(key_field, name)
		for field, value in spec.items():
			if field in (key_field, "why"):
				continue
			if not compat.has_field(doctype, field):
				# A Frappe version that spells this differently, or does not have it.
				# Losing a chart's colour is cosmetic; losing the chart is not.
				continue
			doc.set(field, value)
		if compat.has_field(doctype, "is_public"):
			doc.is_public = 1
		if compat.has_field(doctype, "module"):
			doc.module = MODULE
		if compat.has_field(doctype, "type") and not doc.get("type"):
			doc.type = "Document Type"
		doc.insert(ignore_permissions=True)
		report[f"created_{kind}"].append(name)
	except Exception as exc:
		report["failed"].append({"name": name, "reason": f"{type(exc).__name__}: {exc}"})


def _repair_filters(doctype: str, key_field: str, specs, report: dict) -> None:
	"""Rewrite dict-shaped `filters_json` on cards and charts that already exist.

	THE ONLY REASON THIS IS NOT A FIXTURE-SHAPED OVERWRITE. `_build` leaves an
	existing document completely alone, on the argument the module docstring
	makes: somebody who recoloured a card or changed what it counts did it on
	purpose. That argument holds for every field on these documents except this
	one, because a dict-shaped `filters_json` is not a preference — it is a card
	that raises `TypeError` the moment Frappe tries to draw its comparison arrow,
	and it takes the whole workspace's render down with it.

	So the repair is as narrow as it can be and still work:

	  * ONLY `filters_json`, and only when it parses to a `dict`. A list is either
	    this app's own current output or an operator's, and both are fine.
	  * THE OPERATOR'S OWN CLAUSES ARE CARRIED ACROSS, not replaced with the
	    spec's. Somebody who changed a card to count `Awaiting-Review` instead of
	    `Available` gets a working card counting `Awaiting-Review`. Replacing the
	    filters with this app's would be the fixture behaviour, and it would
	    silently undo their edit under cover of a bug fix.
	  * `document_type` is read off THE DOCUMENT, not the spec, for the same
	    reason — the list shape names the doctype in every clause, and the
	    document knows which doctype it actually counts.

	Never raises: a repair that cannot be made is reported and the migration
	carries on, exactly like every other builder here.
	"""
	for spec in specs:
		name = spec[key_field]
		try:
			if not frappe.db.exists(doctype, name):
				continue
			raw = frappe.db.get_value(doctype, name, "filters_json")
			if not isinstance(raw, str) or not raw.strip():
				continue
			existing = json.loads(raw)
			if not isinstance(existing, dict):
				continue
			document_type = frappe.db.get_value(doctype, name, "document_type") or spec.get("document_type")
			if not document_type:
				continue
			frappe.db.set_value(doctype, name, "filters_json", card_filters(document_type, existing))
			report["repaired_filters"].append(name)
		except Exception as exc:
			report["failed"].append(
				{
					"name": name,
					"reason": (
						f"its filters_json is dict-shaped and could not be rewritten into the list "
						f"shape a Number Card can count: {type(exc).__name__}: {exc}. Until it is, "
						"this card raises when Frappe draws its period-over-period arrow, and the "
						"page it sits on answers Internal Server Error."
					),
				}
			)


def _build_dashboard(report: dict) -> None:
	"""Create the dashboard and wire the cards and charts onto it.

	An EXISTING dashboard is not rebuilt and not appended to. Somebody who took a
	chart off it took it off on purpose, and a migrate that put it back would be
	the fixture behaviour this whole module exists to avoid.
	"""
	try:
		if frappe.db.exists(DASHBOARD, DASHBOARD_NAME):
			report["dashboard_existed"] = True
			return
		doc = frappe.new_doc(DASHBOARD)
		doc.dashboard_name = DASHBOARD_NAME
		if compat.has_field(DASHBOARD, "module"):
			doc.module = MODULE
		if compat.has_field(DASHBOARD, "is_standard"):
			doc.is_standard = 0
		for spec in CARDS:
			if frappe.db.exists(CARD, spec["label"]):
				doc.append("cards", {"card": spec["label"]})
		for spec in CHARTS:
			if frappe.db.exists(CHART, spec["chart_name"]):
				doc.append("charts", {"chart": spec["chart_name"]})
		doc.insert(ignore_permissions=True)
		report["dashboard_created"] = True
	except Exception as exc:
		report["failed"].append({"name": DASHBOARD_NAME, "reason": f"{type(exc).__name__}: {exc}"})


# ── the readiness score ─────────────────────────────────────────────────────
def readiness(company: str = "", today: str = "") -> dict:
	"""Audit readiness as one comparable number, plus what is behind it.

	resolved / raised, as a percentage, where resolved means dismissed by anybody
	— the sweep because the condition went away, or a person because they looked
	and decided. Both are the work being done; a dismissal that was not the work
	is caught by the mandatory reason on it.

	`dismissal_quality` is the honesty check on the headline. An operation at 95%
	entirely through human dismissals is a different operation from one at 95%
	because the conditions genuinely resolved, and a score that could not tell
	them apart would be a score worth gaming.

	A site with no alerts at all scores 100 and says so. That is not a lie: there
	is nothing outstanding. It is reported with `raised: 0` so nobody mistakes an
	empty site for a well-run one.
	"""
	today = today or frappe.utils.today()
	out = {
		"company": company or None,
		"as_of": today,
		"raised": 0,
		"resolved": 0,
		"open": 0,
		"snoozed": 0,
		"audit_readiness_score": 100.0,
		"by_severity": {SEVERITY_CRITICAL: 0, SEVERITY_WARNING: 0, SEVERITY_INFO: 0},
		"by_category": {},
		"dismissal_quality": {"auto_dismissed": 0, "dismissed_by_a_person": 0},
	}
	if not compat.doctype_exists(ALERT_DOCTYPE):
		out["note"] = "this site has no Compliance Alert DocType — run `bench migrate`."
		return out

	filters = {"company": company} if company else {}
	rows = frappe.db.get_all(
		ALERT_DOCTYPE,
		filters=filters,
		fields=["name", "severity", "category", "dismissed", "auto_dismissed", "snoozed_until"],
		limit=20000,
	)
	for row in rows or []:
		out["raised"] += 1
		if frappe.utils.cint(row.get("dismissed")):
			out["resolved"] += 1
			if frappe.utils.cint(row.get("auto_dismissed")):
				out["dismissal_quality"]["auto_dismissed"] += 1
			else:
				out["dismissal_quality"]["dismissed_by_a_person"] += 1
			continue
		out["open"] += 1
		severity = str(row.get("severity") or SEVERITY_WARNING)
		out["by_severity"][severity] = out["by_severity"].get(severity, 0) + 1
		category = str(row.get("category") or "Other")
		out["by_category"][category] = out["by_category"].get(category, 0) + 1
		snoozed = str(row.get("snoozed_until") or "")
		if snoozed and snoozed >= today:
			out["snoozed"] += 1

	if out["raised"]:
		out["audit_readiness_score"] = round(100.0 * out["resolved"] / out["raised"], 1)
	out["by_category"] = dict(sorted(out["by_category"].items()))
	out["note"] = (
		"No compliance alert has ever been raised on this site. The score is 100 because "
		"nothing is outstanding, which is not the same as being audit-ready — run "
		"refresh_compliance_alerts first."
		if not out["raised"]
		else (
			f"{out['resolved']} of {out['raised']} alert(s) resolved. "
			f"{out['dismissal_quality']['auto_dismissed']} resolved themselves when the "
			f"underlying condition cleared; "
			f"{out['dismissal_quality']['dismissed_by_a_person']} were dismissed by a person, "
			"each with a recorded reason."
		)
	)
	return out
