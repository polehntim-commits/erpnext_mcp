# SPDX-License-Identifier: MIT
"""Argument coercion and name resolution shared by every tool.

An MCP client is a language model. It will send `"2026-1-5"` for a date, the
string `"50"` for a limit, an account's *label* where its docname belongs, and
sometimes nothing at all where the site has only one sensible answer. Rejecting
those with a schema error wastes a round trip; each one has an unambiguous
reading, so this module does the reading and raises `ToolError` with a message
that says what to send instead only when it genuinely cannot tell.

The resolvers are the important part. `resolve_company` fills in the company
when the site has exactly one, which is the common case and saves the model
guessing. `resolve_account` accepts a docname, an account number, or an account
name, because those are three things a model will call "the account" and only
one of them is the primary key.
"""

import re

import frappe

from . import compat
from .errors import ToolError

#: Hard ceiling on any `limit`, whatever the caller asks for. A model that says
#: `limit=100000` wants "all of them"; answering with 500 rows and saying so is
#: more useful than timing out the request or blowing the context window.
MAX_LIMIT = 500
DEFAULT_LIMIT = 100


def as_str(args: dict, key: str, required: bool = False, default: str = "") -> str:
	value = args.get(key)
	if value is None or value == "":
		if required:
			raise ToolError(f"{key} is required")
		return default
	return str(value).strip()


#: The shape of a `visit_id`, confirmed against the iOS app: a UUID, 36
#: characters, 8-4-4-4-12. The handset mints them uppercase; this matches either
#: case, because the casing is not the part that has to be right.
VISIT_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def as_visit_id(args: dict, key: str = "visit_id") -> str:
	"""A handset's trip identifier, or `""` when the caller minted none.

	WHY THIS IS STRICT WHEN v0.20.1 WAS DELIBERATELY NOT. The old reading was
	that a server with opinions about somebody else's identifier format would
	refuse a completion over its least important field, so anything that arrived
	was written through and anything falsy was dropped. That trade was right only
	while the format was unknown. Now that it is confirmed, the lenient path has
	no upside left and one live cost: `list_visits` groups on this column by
	exact value, so a `visit_id` the handset garbled is not a cosmetic blemish on
	one row — it silently splits one trip into two, or drops a completion out of
	the rollup entirely, and the resulting report is WRONG RATHER THAN OBVIOUSLY
	MISSING. Nobody reading "four cabins" learns that a fifth was closed on that
	walk.

	Absent, `None` or empty is still no visit, and still not an error: a client
	that does not mint one is a supported client, and `list_visits` counts those
	completions separately and says so. Anything else present and unparseable is
	refused HERE, at the door, where the message can name the value and the shape
	— rather than accepted into a column somebody reports off a season later.
	"""
	raw = args.get(key)
	if raw is None:
		return ""
	visit = str(raw).strip()
	if not visit:
		return ""
	if not VISIT_ID_RE.match(visit):
		raise ToolError(
			f"{key} must be a UUID as 8-4-4-4-12 hex characters, e.g. "
			f"'5C1F0A64-2B3D-4E5F-8A9B-0C1D2E3F4A5B'. Got {str(raw)!r}. Nothing was written — "
			f"send the identifier the handset minted, or omit it entirely to file this outside "
			f"any visit."
		)
	return visit


def as_filter(args: dict, key: str, default: str = "") -> str:
	"""A filter value where an explicit empty string means "no filter".

	`as_str` cannot express this. It treats `""` the same as missing, so a tool
	whose default is `"Active"` would silently override a caller who deliberately
	passed an empty status meaning "every status" — and the tool's own
	description promises that works. Absent → the default; present but empty →
	no filter.
	"""
	if key not in args or args[key] is None:
		return default
	return str(args[key]).strip()


def as_date(args: dict, key: str, required: bool = False) -> str | None:
	"""An ISO `YYYY-MM-DD` date string, or None when absent and not required.

	Accepts anything `frappe.utils.getdate` accepts (which includes datetimes
	and single-digit months) and normalises it, so downstream comparisons are
	always string-comparable ISO dates.
	"""
	raw = args.get(key)
	if raw is None or raw == "":
		if required:
			raise ToolError(f"{key} is required (YYYY-MM-DD)")
		return None
	try:
		return frappe.utils.getdate(raw).isoformat()
	except Exception:
		raise ToolError(f"{key} must be a date as YYYY-MM-DD, got {raw!r}") from None


def as_int(args: dict, key: str, default: int | None = None) -> int | None:
	raw = args.get(key)
	if raw is None or raw == "":
		return default
	try:
		return int(raw)
	except (TypeError, ValueError):
		raise ToolError(f"{key} must be a whole number, got {raw!r}") from None


def as_float(value, key: str) -> float:
	if value is None or value == "":
		return 0.0
	try:
		return float(value)
	except (TypeError, ValueError):
		raise ToolError(f"{key} must be a number, got {value!r}") from None


def as_datetime_claim(value, key: str, now: str) -> str:
	"""A moment a CLIENT states, checked only for the thing that cannot be true.

	v0.137.0. Every other timestamp in this app is the server's own clock, on the
	argument that a handset which could set one could backdate it. That argument
	holds wherever the server is present at the event — and fails where it is
	not: an I-9 built and signed on a phone in an orchard with no bars and
	uploaded at the shed an hour later has an attestation moment the server never
	saw, and stamping `now()` would record the upload and call it the signature.

	SO THE CLAIM IS TAKEN AND ONLY THE IMPOSSIBLE IS REFUSED. A timestamp in the
	FUTURE cannot describe something that has happened, and it is what a
	clock-skewed or tampered client produces; everything else is unverifiable in
	both directions and is recorded as the claim it is. Callers keep the server's
	own arrival time separately, so the two can be compared later.
	"""
	if value in (None, ""):
		raise ToolError(f"{key} must be a date and time, got {value!r}")
	try:
		moment = frappe.utils.get_datetime(value)
	except (TypeError, ValueError):
		# A REFUSAL RATHER THAN A TRACEBACK. This value comes off a handset, so a
		# malformed one is an ordinary bad request and the caller has to be told
		# which argument to fix — an unhandled ValueError halfway through filing a
		# federal record says only that something went wrong.
		raise ToolError(f"{key} must be a date and time like '2026-08-25 16:32:20', got {value!r}.") from None
	if moment is None:  # pragma: no cover - a Frappe that answers None rather than raising
		raise ToolError(f"{key} must be a date and time, got {value!r}")
	if frappe.utils.get_datetime(moment) > frappe.utils.get_datetime(now):
		raise ToolError(
			f"{key} is {value!r}, which is in the future. A signature cannot have been made "
			f"later than now, so this is a clock that is wrong rather than a moment that "
			f"happened. Nothing was changed."
		)
	return str(moment)


#: The spellings a client may use for ONE unqualified fix, in precedence order.
#: THE ORDER MATCHES `signatures._context` AND HAS TO: that function reads the
#: same argument dict to build the Signing Evidence row while `as_gps` builds the
#: column on the form, and both describe one signature. A caller sending
#: `gps_latitude` and `gps_lat` with different values would otherwise put one
#: location in the register and a different one on the record — two answers to
#: "where was this signed" with nothing to say which is right.
GPS_KEYS = (("gps_latitude", "gps_longitude"), ("gps_lat", "gps_lon"), ("latitude", "longitude"))


def as_gps(args: dict, *pairs: tuple) -> str:
	"""One fix as `"45.523100,-122.676500"`, or "". v0.136.0.

	ONE `Data` COLUMN RATHER THAN TWO `Float`s, and the reason is the bug this
	was written for. A Frappe `Float` is `NOT NULL DEFAULT 0` in MariaDB, so a
	signature that reported no location comes back out of the database as
	`0.0, 0.0` — which is not "unknown", it is a point in the Gulf of Guinea
	about 300 miles off Ghana, and `pdf_seal` duly printed it onto every sealed
	I-9 this app has produced. A string column has an empty value that means
	empty, so "no fix" and "a fix at the origin" stop being the same row.

	ALL OR NOTHING. A latitude with no longitude is a point on a line rather
	than a place, which is the rule `signatures._context` already applies to the
	Signing Evidence pair; half a fix recorded as though it were a whole one is
	worse than none.

	THE (0, 0) PAIR IS REFUSED, for the reason above — a handset whose location
	services returned nothing before the fix landed sends two zeroes, and no farm
	this app serves is in the ocean. A zero on ONE axis is kept: the equator and
	the prime meridian are real lines and a coordinate on one of them is a real
	place.

	`as_float` IS DELIBERATELY NOT USED FOR THE ABSENCE TEST. It answers 0.0 for
	absent, for "" and for an explicit 0 alike, so branching on its result would
	be the zero-drop `tests_standalone/test_zero_drop.py` exists to catch. The
	raw values decide whether there is a fix at all; `as_float` only parses one
	that is already known to be there.

	NAMED PAIRS READ ONLY WHAT THEY NAME, and that is why they are a parameter
	rather than another entry in `GPS_KEYS`. v0.137.0 files a whole signed I-9 in
	one call and carries a fix for EACH section — `section_1_gps_lat` and
	`section_2_gps_lat` — so a caller reporting only Section 1's must not have it
	silently copied onto Section 2 by an alias chain falling through to a bare
	`gps_lat`. Passing pairs replaces the default chain outright; passing none
	keeps it, which is what every single-fix caller wants.
	"""
	for latitude_key, longitude_key in pairs or GPS_KEYS:
		latitude = args.get(latitude_key)
		longitude = args.get(longitude_key)
		if latitude is None or latitude == "" or longitude is None or longitude == "":
			continue
		fix = (as_float(latitude, latitude_key), as_float(longitude, longitude_key))
		if fix == (0.0, 0.0):
			return ""
		return f"{fix[0]:.6f},{fix[1]:.6f}"
	return ""


#: What a model actually sends when it means yes or no. JSON booleans are the
#: common case; the strings turn up whenever a client stringifies its arguments.
_TRUE_WORDS = ("1", "true", "yes", "y", "on")
_FALSE_WORDS = ("0", "false", "no", "n", "off")


def as_bool(args: dict, key: str, default=None):
	"""A boolean argument, with anything unrecognisable refused rather than false.

	Two failure modes this exists to avoid, both of which have shipped in real
	MCP servers:

	  * `bool("false")` is True, and `bool("0")` is True. Any coercion that goes
	    through Python's truthiness gets both backwards.
	  * A tolerant reading that maps everything it does not recognise to False
	    turns `dry_run="please"` into a live run. When the default is True
	    *because the operation is dangerous*, silently falling to False is the
	    one outcome the default was chosen to prevent — so an unparseable value
	    is an error, not a vote.

	Absent or empty gives `default`, which is how a caller distinguishes "not
	specified" from "specified as false".
	"""
	raw = args.get(key)
	if raw is None or raw == "":
		return default
	if isinstance(raw, bool):
		return raw
	if isinstance(raw, (int, float)):
		return bool(raw)
	text = str(raw).strip().lower()
	if text in _TRUE_WORDS:
		return True
	if text in _FALSE_WORDS:
		return False
	raise ToolError(f"{key} must be true or false, got {raw!r}")


def as_limit(args: dict, key: str = "limit") -> int:
	"""A row limit, clamped to [1, MAX_LIMIT].

	An explicit 0 clamps to 1 rather than falling back to the default: `x or
	DEFAULT` reads nicely and quietly discards a caller's real answer, which is
	the wrong trade when the caller is a model that may have meant it.
	"""
	value = as_int(args, key, DEFAULT_LIMIT)
	if value is None:
		value = DEFAULT_LIMIT
	return max(1, min(MAX_LIMIT, value))


def as_docstatus(args: dict, key: str = "docstatus") -> int | None:
	"""0 (draft), 1 (submitted) or 2 (cancelled) — or None for "any".

	Also accepts the words, since a model is at least as likely to say
	`"submitted"` as `1`.
	"""
	raw = args.get(key)
	if raw is None or raw == "":
		return None
	words = {"draft": 0, "submitted": 1, "cancelled": 2, "canceled": 2}
	if isinstance(raw, str) and raw.strip().lower() in words:
		return words[raw.strip().lower()]
	value = as_int(args, key)
	if value not in (0, 1, 2):
		raise ToolError(f"{key} must be 0 (draft), 1 (submitted) or 2 (cancelled), got {raw!r}")
	return value


def select_options(doctype: str, fieldname: str) -> list[str]:
	"""A Select field's options, read off this site's meta rather than hardcoded.

	A tool carrying its own copy of the list would accept a value the doctype
	rejects the moment somebody customises it, and the failure would arrive from
	`doc.insert()` as a framework error instead of as a sentence naming the
	choices. Blank options — the leading empty line a Select uses to mean "not
	set" — are dropped, because a caller who wants no value omits the argument.
	"""
	field = compat.field_meta(doctype, fieldname)
	raw = str((field or {}).get("options") or "")
	return [line.strip() for line in raw.split("\n") if line.strip()]


def as_choice(doctype: str, fieldname: str, value: str, label: str) -> str:
	"""Match `value` against a Select's options case-insensitively, or refuse.

	Returns the option in the doctype's own casing, so what is stored matches
	what a filter on the list view will look for. A site whose meta offers no
	options at all gets the value through unchanged — that is a customised or
	half-migrated field, and refusing everything would be worse than trusting the
	caller.
	"""
	options = select_options(doctype, fieldname)
	if not options:  # pragma: no cover - a site whose meta has no options at all
		return value
	for option in options:
		if option.lower() == value.lower():
			return option
	raise ToolError(f"{label} must be one of: {', '.join(options)}. Got {value!r}. Nothing was created.")


def resolve_company(company: str = "", required: bool = False) -> str | None:
	"""A Company docname, inferred when the site leaves no ambiguity.

	Empty input on a single-company site returns that company. On a
	multi-company site it raises with the list of names, which is a far more
	useful reply than a wrong default silently applied to a Journal Entry.
	"""
	company = (company or "").strip()
	if company:
		if not frappe.db.exists("Company", company):
			match = frappe.db.get_value("Company", {"abbr": company}, "name")
			if match:
				return match
			raise ToolError(
				f"no Company named {company!r} on this site. "
				f"Known companies: {', '.join(_company_names()) or '<none>'}"
			)
		return company
	names = _company_names()
	if len(names) == 1:
		return names[0]
	if required:
		raise ToolError(
			f"company is required on a multi-company site. Known companies: {', '.join(names) or '<none>'}"
		)
	return None


def resolve_account(account: str, company: str = "") -> str:
	"""An Account docname from a docname, an account number, or an account name.

	ERPNext's Account primary key is `"<name> - <company abbr>"`, which a model
	rarely reproduces exactly. Resolution order is most-specific first:

	  1. exact docname
	  2. exact `account_number` (unique per company, when the site numbers them)
	  3. exact `account_name`
	  4. case-insensitive `account_name`

	Anything ambiguous raises with the candidates listed, so the model can pick
	rather than this module guessing which "Cash" was meant.
	"""
	account = (account or "").strip()
	if not account:
		raise ToolError("account is required")
	base = {"company": company} if company else {}

	if frappe.db.exists("Account", account):
		found = frappe.db.get_value("Account", account, ["name", "company"], as_dict=True)
		if company and found and found["company"] != company:
			raise ToolError(f"account {account!r} belongs to company {found['company']!r}, not {company!r}")
		return account

	for filters in (
		{**base, "account_number": account},
		{**base, "account_name": account},
		{**base, "account_name": ("like", account)},
	):
		matches = frappe.db.get_all("Account", filters=filters, pluck="name", limit=25)
		if len(matches) == 1:
			return matches[0]
		if len(matches) > 1:
			raise ToolError(
				f"{account!r} matches {len(matches)} accounts: "
				f"{', '.join(sorted(matches)[:10])}. "
				"Pass the full account name, or set company to narrow it."
			)

	scope = f" in company {company!r}" if company else ""
	raise ToolError(f"no Account matching {account!r}{scope}. Try search_accounts to find the right name.")


def resolve_cost_center(cost_center: str, company: str = "") -> str:
	"""A Cost Center docname from a docname, a cost center number, or a name.

	The same three-ways-to-say-it problem `resolve_account` solves, for the other
	tree ERPNext files a posting under. Resolution order is most-specific first:

	  1. exact docname
	  2. exact `cost_center_number` (unique per company, when the site numbers them)
	  3. exact `cost_center_name`
	  4. case-insensitive `cost_center_name`

	Unlike `resolve_account` this checks that `cost_center_number` exists on the
	site before filtering on it. Account numbers predate every ERPNext this app
	supports; cost center numbers do not, and selecting a column a site does not
	have is a SQL error rather than an empty result.
	"""
	cost_center = (cost_center or "").strip()
	if not cost_center:
		raise ToolError("cost_center is required")
	base = {"company": company} if company else {}

	if frappe.db.exists("Cost Center", cost_center):
		found = frappe.db.get_value("Cost Center", cost_center, ["name", "company"], as_dict=True)
		if company and found and found["company"] != company:
			raise ToolError(
				f"cost center {cost_center!r} belongs to company {found['company']!r}, not {company!r}"
			)
		return cost_center

	attempts = [
		{**base, "cost_center_name": cost_center},
		{**base, "cost_center_name": ("like", cost_center)},
	]
	if compat.has_field("Cost Center", "cost_center_number"):
		attempts.insert(0, {**base, "cost_center_number": cost_center})

	for filters in attempts:
		matches = frappe.db.get_all("Cost Center", filters=filters, pluck="name", limit=25)
		if len(matches) == 1:
			return matches[0]
		if len(matches) > 1:
			raise ToolError(
				f"{cost_center!r} matches {len(matches)} cost centers: "
				f"{', '.join(sorted(matches)[:10])}. "
				"Pass the full docname, or set company to narrow it."
			)

	scope = f" in company {company!r}" if company else ""
	raise ToolError(
		f"no Cost Center matching {cost_center!r}{scope}. "
		"Try list_cost_centers to see the tree this company actually has."
	)


def _company_names() -> list[str]:
	return sorted(frappe.db.get_all("Company", pluck="name") or [])
