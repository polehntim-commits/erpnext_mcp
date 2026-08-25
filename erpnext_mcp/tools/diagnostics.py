# SPDX-License-Identifier: MIT
"""What this deployment is, what it has been complaining about, and what it holds.

FOUR READS THAT ARE ABOUT THE INSTALL RATHER THAN ABOUT THE FARM. Every other
tool module here answers a question about an orchard — what was sprayed, who
worked, which bin went where. These four answer questions about the server the
orchard's records live on, which is a different kind of question and is why they
are in a module of their own rather than filed under whichever register they
happen to touch.

  * `get_server_status` — did the deploy land.
  * `list_error_logs` — what has been failing since it did.
  * `query_doctype` — read any register, including ones written after this file.
  * `list_sidecar_routes` — what the handset transport actually publishes.

────────────────────────────────────────────────────────────────────────────
`query_doctype` IS THE ONE THAT NEEDED THINKING ABOUT
────────────────────────────────────────────────────────────────────────────

Every other read in this app is a NAMED SURFACE: it reads one register, returns
the columns somebody chose, and an operator switches it on or off by name. That
is the whole shape of `settings.py` and of the tool catalogue. A tool that reads
any doctype by name is the opposite of that shape, and it is worth being honest
that it widens what one switch means rather than pretending the guards below make
it equivalent to the others.

It is here because the alternative is worse in a specific way. A farm that adds a
custom doctype next season either waits for a release that names it or goes round
this app entirely — and going round it means Frappe's own `/api/resource`, which
is exactly the door the farmops sidecar does not publish and which has no audit
row, no switch and no field filtering at all. One guarded generic reader is a
smaller hole than the pressure to open an unguarded specific one.

THREE GUARDS, AND EACH ONE CLOSES A DIFFERENT DOOR:

  1. **PERMISSIONS ARE ASKED FOR, WHICH MEANS `frappe.get_list`.** Every other
     read in this app calls `frappe.db.get_all`, which SKIPS the permission
     check — deliberately, because those tools decide for themselves what they
     return and the switch is the gate. This one cannot decide, so it must ask,
     and `get_list` is the call that asks.

     **BUT ASKING IS NOT THE SAME AS BEING BOUNDED, AND ON A DEFAULT INSTALL IT
     IS NOT BOUNDED AT ALL.** `mcp.handle` runs every call as
     `settings.effective_user()`, which is the operator's chosen
     `mcp_system_user` — a Link field that ships with NO DEFAULT — and falls
     back to `Administrator` until somebody picks one. Frappe's Administrator
     passes every permission check there is, so on a site where that field is
     empty this guard is present, correct, and restricting nothing.

     That is not a bug to fix by refusing: running as Administrator is the
     documented posture of this whole app until an operator configures
     otherwise, and every mutating tool here already writes as that account.
     What would be a bug is SAYING otherwise, so `query_doctype` reports the
     principal it actually ran as in `acting_user` and says in the answer
     itself, in as many words, when that principal bounds nothing. An operator
     who wants the guard live sets `mcp_system_user` to an account whose roles
     they control — that one setting is the whole difference between this tool
     being scoped and being a reader of the entire site.

  2. **A REGISTER OF DOCTYPES THAT ARE REFUSED WHATEVER THE PERMISSIONS SAY.**
     Defence in depth over guard 1, for the stores where even the columns that
     are not Password fields are credentials — `OAuth Bearer Token.access_token`
     is a Data field on some versions, and a permission check that passes is
     then the only thing between a token and a caller. See `REFUSED_DOCTYPES`.

  3. **PASSWORD FIELDS ARE NEVER RETURNED, ON ANY DOCTYPE.** Asking for one by
     name is refused rather than silently dropped, because a caller who thinks
     they asked for a column and got nothing back will conclude the column is
     empty. This is the general guard; the register above is the specific one.

`order_by` GOES INTO SQL AND IS VALIDATED AS SUCH. Frappe interpolates it, so it
is not an argument like the others — it is a fragment of the query. It is matched
against one column name and an optional direction, and anything else is refused
by shape rather than escaped, because escaping is a thing to get subtly wrong and
a whitelist is not.

────────────────────────────────────────────────────────────────────────────
TWO THINGS THESE TOOLS DELIBERATELY DO NOT CLAIM
────────────────────────────────────────────────────────────────────────────

**`list_sidecar_routes` IS NOT AN ACCESS MAP.** It reports every path the sidecar
publishes and whether each writes — which is a real question, and until now it
had no answer that did not involve reading `routes.py`. What it CANNOT report is
who may call each one, because that gate is one line inside each wrapper's own
body (`require_dispatch_role`, `personnel.require_hr_role()`, or nothing at all)
and is not an attribute of anything this can read. A reader who took a route's
absence from a gate column as "open" would be wrong about the most important
column on the table, so there is no gate column and the answer says why.

**`get_server_status` REPORTS ONE WORKER, NOT THE BENCH.** A Frappe bench runs
several worker processes and this call is answered by whichever one took it, so
`worker_uptime_seconds` is that process's age and two consecutive calls can
disagree. It is still the answer to "did the deploy land": a worker older than
the deploy has not been restarted, and the version beside it is what that
particular process actually imported.
"""

import platform
import re
import sys

import frappe

from .. import __version__, compat
from ..args import as_int, as_limit, as_str
from ..errors import ToolError
from ..farmops_api import routes as sidecar_routes
from ..result import ToolResult

ERROR_LOG = "Error Log"
PATCH_LOG = "Patch Log"

#: The most rows any read here returns. Same ceiling the other registers use.
REGISTER_CAP = 500

#: `platform.python_version()` is the three-part number; `sys.version` carries the
#: build and the compiler, which is what tells two otherwise identical benches
#: apart when only one of them is behaving.
PYTHON_BUILD = sys.version.replace("\n", " ")

#: When this worker process imported the module. See the module docstring for
#: why this is a property of ONE PROCESS and not of the bench.
#:
#: Taken off `frappe.utils.now()` rather than `datetime.now()` so it is the same
#: clock every other stamp in this app is written from — a site whose server
#: timezone differs from its `time_zone` setting would otherwise report an uptime
#: computed across two of them. The fallback exists because this line runs at
#: IMPORT, which on some paths is before a site is bound.
try:  # pragma: no cover - the except branch needs an unbound site
	_STARTED_AT = frappe.utils.now()
except Exception:  # pragma: no cover
	_STARTED_AT = ""


# ════════════════════════════════════════════════════════════════════════════
# 1. get_server_status
# ════════════════════════════════════════════════════════════════════════════
def _app_versions() -> dict:
	"""Every installed app and the version it reports, best effort.

	BEST EFFORT ON PURPOSE. This is the half of the answer that says whether a
	deploy landed, and a status tool that raised because one app on somebody's
	bench does not expose `__version__` would be useless in exactly the moment it
	is reached for — which is when something about the install is already wrong.
	"""
	versions = {"erpnext_mcp": __version__}
	try:
		installed = frappe.get_installed_apps() or []
	except Exception:
		installed = []
	for app in installed:
		if app in versions:
			continue
		try:
			versions[app] = str(frappe.get_attr(f"{app}.__version__") or "") or None
		except Exception:
			versions[app] = None
	return versions


def _last_patch() -> dict | None:
	"""The newest Patch Log row: the last migrate that had something to apply.

	NOT "THE LAST TIME ANYBODY RAN `bench migrate`", and the difference matters
	enough to be reported in the answer rather than left for a reader to assume.
	A migrate with no new patches writes no row, so this stamp can be weeks older
	than the last deploy on a bench that has been redeployed without schema
	changes — which is the ordinary case for a release that only adds tools.
	"""
	if not compat.doctype_exists(PATCH_LOG):
		return None
	rows = frappe.db.get_all(
		PATCH_LOG,
		fields=compat.existing_fields(PATCH_LOG, ("name", "patch", "creation")),
		order_by="creation desc",
		limit=1,
	)
	if not rows:
		return None
	row = dict(rows[0])
	return {
		"patch": row.get("patch") or row.get("name"),
		"applied_at": str(row.get("creation") or "") or None,
	}


def get_server_status(args: dict) -> ToolResult:
	"""Read-only. What this deployment is running, and since when."""
	uptime = None
	if _STARTED_AT:
		try:
			uptime = round(frappe.utils.time_diff_in_seconds(frappe.utils.now(), _STARTED_AT), 1)
		except Exception:  # pragma: no cover - an unparseable clock
			uptime = None

	apps = _app_versions()
	patch = _last_patch()
	data = {
		"erpnext_mcp_version": __version__,
		"python_version": platform.python_version(),
		"python_implementation": platform.python_implementation(),
		"python_build": PYTHON_BUILD,
		"frappe_version": str(getattr(frappe, "__version__", "") or "") or None,
		"installed_apps": apps,
		"site": str(getattr(frappe.local, "site", "") or "") or None,
		"worker_started_at": _STARTED_AT or None,
		"worker_uptime_seconds": uptime,
		"last_patch_applied": patch,
		"notes": [
			"worker_uptime_seconds IS THIS PROCESS, NOT THE BENCH. A Frappe bench runs "
			"several workers and this call was answered by one of them, so two consecutive "
			"calls can disagree. A worker older than a deploy has not been restarted — "
			"which is usually the thing somebody is actually asking.",
			"last_patch_applied IS THE LAST MIGRATE THAT HAD A PATCH TO APPLY, not the last "
			"migrate. A release that adds only tools writes no patch, so this stamp being old "
			"is not evidence that a deploy did not land. The version above is that evidence.",
		],
	}
	if not patch:
		data["notes"].append(
			f"No {PATCH_LOG} rows on this site, so nothing here can say when it was last "
			"migrated. On a site that has been migrated at least once this is unexpected."
		)
	return ToolResult(
		data=data,
		summary=(
			f"erpnext_mcp {__version__} on python {platform.python_version()}"
			+ (f", frappe {data['frappe_version']}" if data["frappe_version"] else "")
			+ (f", worker up {uptime}s" if uptime is not None else "")
		),
	)


# ════════════════════════════════════════════════════════════════════════════
# 2. list_error_logs
# ════════════════════════════════════════════════════════════════════════════
#: How much of a traceback comes back per row. A traceback is a page and a feed
#: of forty of them is not readable — `get_document_preview` on the docname has
#: the whole thing when one of them turns out to matter.
ERROR_PREVIEW = 1200

#: Credential-shaped assignments inside a traceback, replaced on the way out.
#: A traceback is the repr of whatever was in scope when something threw, and
#: what is in scope on a failed request routinely includes the request body —
#: which on this app's own transports carries a token. `strip_secrets` removes
#: credential-shaped KEYS from a structure and cannot see inside a string, so
#: this is the same judgement applied to the one field that is a string.
_SECRET_IN_TRACEBACK = re.compile(
	r"""((?:api[_-]?secret|api[_-]?key|password|secret|token|authorization)["']?\s*[:=]\s*["']?)([^\s,'")}]{4,})""",
	re.IGNORECASE,
)


def _redact(text: str) -> tuple[str, int]:
	"""(text with credential-shaped values replaced, how many were replaced)."""
	replaced = 0

	def swap(match):
		nonlocal replaced
		replaced += 1
		return f"{match.group(1)}<redacted>"

	return _SECRET_IN_TRACEBACK.sub(swap, text or ""), replaced


def _exception_line(error: str) -> str | None:
	"""The last non-empty line of a traceback, which is the exception itself.

	THE HEAD OF A TRACEBACK IS THE LEAST USEFUL PART OF IT. Python prints the
	call stack first and the exception LAST, so a preview that takes the first N
	characters — which is what a truncated feed necessarily does — is a preview
	with the answer cut off. Both are returned: the head for context and this for
	what actually went wrong.
	"""
	lines = [line.strip() for line in str(error or "").splitlines() if line.strip()]
	return lines[-1] if lines else None


def _since(args: dict) -> str | None:
	"""The floor of the window, from `minutes`, `hours` or an explicit `since`."""
	explicit = as_str(args, "since")
	if explicit:
		return explicit
	minutes = as_int(args, "minutes")
	hours = as_int(args, "hours")
	if minutes is None and hours is None:
		return None
	total = (minutes or 0) + (hours or 0) * 60
	if total <= 0:
		raise ToolError(
			f"a window of {total} minute(s) selects nothing. `minutes` and `hours` count "
			"BACKWARDS from now and must be positive; omit both for the whole register."
		)
	# `as_string=True, as_datetime=True` IS FRAPPE'S OWN DATETIME_FORMAT, and the
	# pair is not decoration: `add_to_date` returns a `datetime` OBJECT without
	# it, and a datetime handed to a filter against a `creation` column compares
	# as whatever the driver makes of it rather than as the string the column
	# holds. The same boundary `datetimes.py` exists for, approached from the
	# other side.
	return frappe.utils.add_to_date(frappe.utils.now(), minutes=-total, as_string=True, as_datetime=True)


def list_error_logs(args: dict) -> ToolResult:
	"""Read-only. Recent Error Log rows, newest first, with the exception named."""
	compat.require_doctype(
		ERROR_LOG,
		"which is a core Frappe doctype — a site without it is not a working Frappe site",
	)

	filters: dict = {}
	since = _since(args)
	if since:
		filters["creation"] = (">=", since)
	method = as_str(args, "method")
	if method:
		# SUBSTRING, NOT EXACT. A caller who has a method name has it from a
		# traceback or a route table and rarely has the dotted path Frappe stored
		# it under, and an exact match that answered empty would read as "nothing
		# failed" rather than as "not spelled the way I guessed".
		filters["method"] = ("like", f"%{method}%")
	contains = as_str(args, "contains")
	if contains:
		filters["error"] = ("like", f"%{contains}%")
	seen = args.get("seen")
	if seen is not None and seen != "":
		filters["seen"] = 1 if compat.checked(seen) else 0

	limit = min(as_limit(args), REGISTER_CAP)
	rows = (
		frappe.db.get_all(
			ERROR_LOG,
			filters=filters,
			fields=compat.existing_fields(
				ERROR_LOG,
				("name", "method", "error", "creation", "reference_doctype", "reference_name", "seen"),
			),
			order_by="creation desc",
			limit=limit + 1,
		)
		or []
	)
	truncated = len(rows) > limit
	rows = rows[:limit]

	redactions = 0
	logs = []
	by_method: dict = {}
	for raw in rows:
		row = dict(raw)
		full = str(row.get("error") or "")
		safe, hits = _redact(full)
		redactions += hits
		method_name = row.get("method") or "(no method recorded)"
		by_method[method_name] = by_method.get(method_name, 0) + 1
		logs.append(
			{
				"name": row.get("name"),
				"method": row.get("method") or None,
				"creation": str(row.get("creation") or "") or None,
				"exception": _exception_line(safe),
				"error": safe[:ERROR_PREVIEW],
				"error_truncated": len(safe) > ERROR_PREVIEW,
				"error_length": len(full),
				"reference_doctype": row.get("reference_doctype") or None,
				"reference_name": row.get("reference_name") or None,
				"seen": compat.checked(row.get("seen")),
			}
		)

	data = {
		"count": len(logs),
		"limit": limit,
		"truncated": truncated,
		"since": since,
		"filters": {
			"method": method or None,
			"contains": contains or None,
			"minutes": as_int(args, "minutes"),
			"hours": as_int(args, "hours"),
		},
		"by_method": dict(sorted(by_method.items(), key=lambda pair: (-pair[1], pair[0]))),
		"error_logs": logs,
		"notes": [
			"`exception` IS THE LAST LINE OF THE TRACEBACK, which is where Python puts what "
			"actually went wrong. `error` is the HEAD of the same traceback and is truncated "
			f"at {ERROR_PREVIEW} characters — the two together are the row; neither alone is.",
			"get_document_preview on the docname returns a whole traceback when one of these "
			"turns out to matter.",
		],
	}
	if redactions:
		data["redacted_values"] = redactions
		data["notes"].append(
			f"{redactions} credential-shaped value(s) were replaced with <redacted> before "
			"this answer left the server. A traceback is the repr of whatever was in scope "
			"when something threw, which on a failed request includes the request body."
		)
	if not logs:
		data["empty_note"] = (
			"No Error Log rows match. AN EMPTY REGISTER IS A GOOD ANSWER and is worth "
			"distinguishing from a narrow one: call this with no filters to see whether the "
			"site is logging anything at all before concluding that nothing has failed."
		)
	if truncated:
		data["truncated_note"] = (
			f"More than {limit} rows matched. Narrow with `minutes`, `method` or `contains` — "
			"a site that has been failing for a week will fill any limit you set."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{len(logs)} error log(s)"
			+ (f" since {since}" if since else "")
			+ (f", commonest: {next(iter(data['by_method']))}" if by_method else "")
		),
	)


# ════════════════════════════════════════════════════════════════════════════
# 3. query_doctype
# ════════════════════════════════════════════════════════════════════════════
#: Refused whatever this site's DocPerms say. See guard 2 in the module
#: docstring: these hold credentials in columns that are not Password fields, so
#: the permission check passing is then the only thing between a live token and a
#: caller. The named alternative for each is a tool that returns the part of it
#: somebody legitimately wants without the part they do not.
REFUSED_DOCTYPES = {
	"ERPNext MCP Settings": "it holds this endpoint's own auth token. `erpnext_mcp.mcp.report` says whether one is configured without returning it.",
	"User": "it holds api_key/api_secret pairs and password reset keys. `list_mobile_users` returns who exists and what they may do.",
	"OAuth Bearer Token": "the access token is a plain Data column on some Frappe versions.",
	"OAuth Client": "it holds the client secret, which is the credential every token issued against it derives from.",
	"OAuth Authorization Code": "an authorization code is a credential in flight.",
	"Token Cache": "it caches third-party access and refresh tokens.",
	"Social Login Key": "it holds the provider's client secret.",
	"Connected App": "it holds the client secret and the redirect credentials.",
	"Webhook": "request headers on a webhook routinely carry a bearer token.",
	"Webhook Request Log": "it stores the headers a webhook was sent with.",
	"Integration Request": "it stores whole third-party request and response bodies.",
	"Email Account": "it holds the mailbox password, and on most benches that mailbox is how a password reset is received.",
	"LDAP Settings": "it holds the bind password, which is a credential for the directory this site authenticates against.",
}

#: One column name, optionally with a direction. `order_by` is interpolated into
#: SQL by Frappe rather than parameterised, so it is a query fragment and not an
#: argument — matched against this by shape and refused otherwise. A whitelist is
#: used rather than escaping because escaping is a thing to get subtly wrong.
_ORDER_BY = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)(?:\s+(asc|desc))?$", re.IGNORECASE)

#: Fieldtypes never returned, on any doctype, whatever was asked for. `Password`
#: is encrypted at rest and excluded from `as_dict()` for a reason this tool does
#: not get to overrule.
_NEVER_RETURNED = ("Password",)


def _requested_doctype(args: dict) -> str:
	doctype = as_str(args, "doctype", required=True).strip()
	why = REFUSED_DOCTYPES.get(doctype)
	if why:
		raise ToolError(
			f"{doctype} is not readable through query_doctype: {why} This refusal does not "
			"depend on permissions and cannot be switched on — it is a property of what the "
			"doctype holds. Nothing was read."
		)
	if not compat.doctype_exists(doctype):
		raise ToolError(
			f"no DocType called {doctype!r} on this site. The name is the doctype's LABEL as "
			"the Desk shows it — 'Farm Task', not 'farm_task' — and it is case-sensitive."
		)
	return doctype


def _requested_fields(doctype: str, args: dict) -> list:
	"""The columns to select, validated against the doctype and against secrecy."""
	raw = args.get("fields")
	if isinstance(raw, str):
		raw = [part.strip() for part in raw.split(",") if part.strip()]
	if not raw:
		return ["name"]
	if not isinstance(raw, (list, tuple)):
		raise ToolError("fields must be a list of fieldnames, or a comma-separated string.")

	wanted = [str(field).strip() for field in raw if str(field).strip()]
	if "*" in wanted:
		raise ToolError(
			"fields does not take '*'. Name the columns you want — a select-everything on a "
			"doctype whose shape you have not read is how a Password field or a 40 KB text "
			"column ends up in an answer. `get_wizard_definition`-style metadata is what "
			"list_custom_fields and the Desk's own form are for."
		)

	secret = [field for field in wanted if _is_secret_field(doctype, field)]
	if secret:
		raise ToolError(
			f"{', '.join(sorted(secret))} on {doctype} "
			f"{'is a Password field' if len(secret) == 1 else 'are Password fields'} and is "
			"never returned by anything in this app. Refused rather than dropped, because a "
			"caller who asked for a column and got nothing back would read it as empty."
		)
	unknown = [field for field in wanted if not compat.has_field(doctype, field)]
	if unknown:
		raise ToolError(
			f"{doctype} has no field(s) called {', '.join(sorted(unknown))}. Selecting a "
			"column that does not exist is a hard SQL error rather than an empty column, "
			"so it is refused here. list_custom_fields shows what a site has added."
		)
	return wanted


def _is_secret_field(doctype: str, fieldname: str) -> bool:
	meta = compat.field_meta(doctype, fieldname)
	return bool(meta is not None and str(getattr(meta, "fieldtype", "")) in _NEVER_RETURNED)


def _visible_fields(doctype: str, fields: list) -> list:
	"""`fields`, minus anything this site declares Password. The general guard.

	`_requested_fields` refuses a Password field a caller NAMED. This runs over
	the list that is actually about to be selected, which on a site that added a
	Password field with a Custom Field is not the same set — and here the right
	answer is to drop rather than refuse, because the caller did not ask for it.
	"""
	return [field for field in fields if not _is_secret_field(doctype, field)]


def _requested_filters(args: dict) -> dict:
	raw = args.get("filters") or {}
	if isinstance(raw, str):
		raise ToolError(
			"filters must be an object mapping fieldname to a value — "
			'{"status": "Active"} — or to an [operator, value] pair — '
			'{"creation": [">", "2026-08-01"]}. A string is not one.'
		)
	if not isinstance(raw, dict):
		raise ToolError(
			"filters must be an object mapping fieldname to a value or an [operator, value] pair."
		)
	return {str(key): value for key, value in raw.items()}


def _requested_order(doctype: str, args: dict) -> str:
	given = as_str(args, "order_by").strip()
	if not given:
		return "modified desc" if compat.has_field(doctype, "modified") else "name asc"
	match = _ORDER_BY.match(given)
	if not match:
		raise ToolError(
			f"order_by {given!r} is not one column and an optional direction. It is "
			"interpolated into SQL rather than passed as an argument, so it is matched by "
			"shape and refused otherwise — 'creation desc' and 'name' are the forms it takes. "
			"Sorting on two columns is not available here."
		)
	column = match.group(1)
	if not compat.has_field(doctype, column):
		raise ToolError(f"{doctype} has no field called {column!r} to order by.")
	return f"{column} {(match.group(2) or 'asc').lower()}"


#: Principals that pass every permission check Frappe has, so a `get_list` made
#: as one of them is unrestricted however carefully it was written. Administrator
#: is the one this app reaches by default: `settings.effective_user()` falls back
#: to it, and `mcp_system_user` is a Link field with NO shipped default.
UNBOUNDED_PRINCIPALS = frozenset({"Administrator"})


def _acting_principal() -> tuple[str, bool]:
	"""(the user `get_list` will check, whether that user restricts anything).

	    READ OFF THE SESSION RATHER THAN OFF THE SETTINGS, because the session is
	    what the framework will actually consult. `mcp.handle` sets it to
	    `settings.effective_user()` before any tool runs, so the two normally agree —
	    but reading the session is right even when they do not, and "whose
	permissions applied" must never be answered from a different source than the
	    one that applied them.
	"""
	principal = str(getattr(frappe.session, "user", "") or "")
	return principal, bool(principal) and principal not in UNBOUNDED_PRINCIPALS


def query_doctype(args: dict) -> ToolResult:
	"""Read-only. Any register on this site, under this account's own permissions."""
	doctype = _requested_doctype(args)
	fields = _requested_fields(doctype, args)
	filters = _requested_filters(args)
	order_by = _requested_order(doctype, args)
	limit = min(as_limit(args), REGISTER_CAP)

	principal, bounding = _acting_principal()
	selected = _visible_fields(doctype, fields)
	dropped = [field for field in fields if field not in selected]
	if not selected:  # pragma: no cover - every doctype has `name`
		raise ToolError(f"nothing readable was left of the requested fields on {doctype}.")

	# `get_list` RATHER THAN `db.get_all`, WHICH IS THE WHOLE DIFFERENCE. See
	# guard 1 in the module docstring: every other read here decides for itself
	# what it returns and is gated by its switch; this one cannot decide, so it
	# asks the framework, and `get_list` is the call that asks.
	try:
		rows = (
			frappe.get_list(
				doctype,
				filters=filters,
				fields=selected,
				order_by=order_by,
				limit=limit + 1,
			)
			or []
		)
	except frappe.PermissionError as exc:
		raise ToolError(
			f"this account may not read {doctype}: {exc}. THE ACCOUNT IS THE ONE THE OPERATOR "
			"CONFIGURED as `mcp_system_user` on ERPNext MCP Settings, and its roles are what "
			"this tool can reach — a switch cannot widen it and nothing here overrides a "
			"DocPerm. Nothing was read."
		) from exc

	truncated = len(rows) > limit
	records = [dict(row) for row in rows[:limit]]

	data = {
		"doctype": doctype,
		"count": len(records),
		"limit": limit,
		"truncated": truncated,
		"fields": selected,
		"filters": filters,
		"order_by": order_by,
		"records": records,
		"acting_user": principal or None,
		"permissions_bounding": bounding,
		"note": (
			"READ THROUGH frappe.get_list, so what came back is what the account this app "
			"acts as may read, and not what the doctype holds. An empty answer from a "
			"register you know has rows is that account's permissions, not a missing filter."
		),
	}
	if not bounding:
		named = principal or "an unidentified principal"
		data["permissions_note"] = (
			f"THIS ANSWER WAS NOT BOUNDED BY PERMISSIONS. The call ran as {named}, which "
			"passes every permission check on this site, so `frappe.get_list` restricted "
			"nothing and this tool read the register in full. "
			"That is the default posture until an operator sets `mcp_system_user` on ERPNext "
			"MCP Settings to an account whose roles they control — one field, and it is the "
			"whole difference between this tool being scoped and being a reader of the entire "
			"site. The refused-doctype register and the Password-field rule applied as normal; "
			"those do not depend on permissions."
		)
	if dropped:
		data["fields_dropped"] = dropped
		data["fields_dropped_note"] = (
			f"{', '.join(sorted(dropped))} {'is a' if len(dropped) == 1 else 'are'} Password "
			f"field(s) on this site and {'was' if len(dropped) == 1 else 'were'} removed from "
			"the selection. Nothing in this app returns one."
		)
	if truncated:
		data["truncated_note"] = (
			f"More than {limit} rows matched. Narrow with `filters` rather than raising the "
			"limit — this is a query tool and not an export."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{len(records)} {doctype} record(s), ordered by {order_by}, read as {principal}"
			+ ("" if bounding else " — which bounds nothing")
		),
	)


# ════════════════════════════════════════════════════════════════════════════
# 4. list_sidecar_routes
# ════════════════════════════════════════════════════════════════════════════
def list_sidecar_routes(args: dict) -> ToolResult:
	"""Read-only. Every path the farmops sidecar publishes, and what each takes."""
	contains = as_str(args, "contains").strip().lower()
	group = as_str(args, "group").strip().lower()
	only_mutating = args.get("mutating")

	described = []
	by_group: dict = {}
	for route in sidecar_routes.ROUTES:
		path = route.path
		# `/mobile/get_task` → `mobile`. The prefix is how the surface divides:
		# the handset's own methods and the two file paths are different services
		# sharing a transport.
		section = path.strip("/").split("/", 1)[0]
		by_group[section] = by_group.get(section, 0) + 1
		if group and section != group:
			continue
		if contains and contains not in path.lower():
			continue
		mutating = bool(route.mutating)
		if only_mutating is not None and only_mutating != "" and compat.checked(only_mutating) != mutating:
			continue
		described.append(
			{
				"path": f"{sidecar_routes.PREFIX}{path}",
				"method": getattr(route.handler, "farm_ops_method", "") or None,
				"group": section,
				"mutating": mutating,
				"arguments": sorted(sidecar_routes.accepted_arguments(route.handler)),
			}
		)

	described.sort(key=lambda entry: entry["path"])
	writes = sum(1 for entry in described if entry["mutating"])
	data = {
		"prefix": sidecar_routes.PREFIX,
		"count": len(described),
		"total_routes": len(sidecar_routes.ROUTES),
		"mutating_count": writes,
		"read_count": len(described) - writes,
		"by_group": dict(sorted(by_group.items())),
		"routes": described,
		"notes": [
			"THIS IS NOT AN ACCESS MAP. It says which paths exist and which of them write. "
			"WHO may call each one is a line inside that route's own wrapper body — "
			"require_dispatch_role, require_hr_role, or nothing at all — and is not an "
			"attribute of anything readable from here. There is no gate column rather than "
			"an incomplete one, because a route missing from a gate column reads as open.",
			"`arguments` is read off each wrapper's signature, which IS the filter: the "
			"transport drops every body key that is not in this list, so a key absent here "
			"is unreachable rather than merely undocumented. `user` is never in it — the "
			"guard injects the authenticated caller and drops any body copy.",
			"A tool being in the MCP catalogue does not put it on this table and vice versa. "
			"The two surfaces are separate on purpose: create_journal_entry and convey_parcel "
			"are tools here and are reachable from no handset at any path.",
		],
	}
	if not described:
		data["empty_note"] = (
			f"No route matches. The sidecar publishes {len(sidecar_routes.ROUTES)} paths in "
			f"total across {', '.join(sorted(by_group))} — call this with no filters to see "
			"them."
		)
	return ToolResult(
		data=data,
		summary=(f"{len(described)} sidecar route(s) of {len(sidecar_routes.ROUTES)}, {writes} mutating"),
	)


#: Every argument each tool reads, so a test can hold the registry to it without
#: a hand-copied list going stale beside this one. `additionalProperties` is
#: advertised on every schema in this app and enforced by nothing, so an argument
#: the schema omits is not refused — it is ignored, and no caller learns it
#: exists. Mirrors `app_feedback.LIST_ARGUMENTS`.
STATUS_ARGUMENTS: tuple = ()
ERROR_LOG_ARGUMENTS = ("minutes", "hours", "since", "method", "contains", "seen", "limit")
QUERY_ARGUMENTS = ("doctype", "filters", "fields", "order_by", "limit")
ROUTE_ARGUMENTS = ("contains", "group", "mutating")
