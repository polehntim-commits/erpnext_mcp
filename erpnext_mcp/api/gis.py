# SPDX-License-Identifier: MIT
"""The county's copy of the shape, and the one path a drawn boundary takes to disk.

v0.33.0. THE THIRD TRANSPORT, and the smallest. `mcp.handle` serves the AI,
`api/mobile.py` serves forty phones, and these two methods serve exactly one
thing: the Leaflet map on a Desk form, driven by a person who is already signed
in to that Desk and already looking at the record.

────────────────────────────────────────────────────────────────────────────
WHY A BOUNDARY MAY NOW BE DRAWN, AFTER v0.32.0 SAID IT MAY NOT
────────────────────────────────────────────────────────────────────────────

v0.32.0's widget was read-only and said so at length: "a map that could nudge a
vertex would be a way to change all of that by accident, with no validation and
no audit row." Every word of that is still true, and it is the reason this file
exists rather than the reason it should not.

The thing v0.32.0 was refusing was a map that WROTE TO THE FIELD. What
`save_boundary` does instead is call `set_parcel_boundary`, `set_field_boundary`
or `set_zone_boundary` — the same three tools the AI calls, unchanged, with every
check they have always made: the polygon is parsed, self-intersection is refused,
the enclosed area is compared against the recorded acreage and a disagreement
past a quarter is REFUSED outright, containment against the shape above is
reported, and every derived field (centroid, bounding box, H3 coverage, computed
acres) is recomputed from the polygon rather than typed. A vertex nudged by
accident does not get saved quietly; it gets an area disagreement and a refusal.

So the map is not a way round the boundary tools. It is a second caller of them,
and the one whose caller can see what they are about to change.

AND THE FIELD WAS ALWAYS WRITABLE ANYWAY. `boundary_geojson` is an ordinary Long
Text on Parcel, Field and Irrigation Zone with no `read_only` flag: anybody with
write permission could already paste a polygon straight into the form and press
save — which stores it with NOTHING derived, no validation, no area check, and a
centroid left over from the previous shape. This endpoint is strictly the safer
of the two doors, and it is the one with a map next to it.

────────────────────────────────────────────────────────────────────────────
THE GATE IS FRAPPE'S OWN PERMISSION ON THE DOCUMENT, AND NOTHING ELSE
────────────────────────────────────────────────────────────────────────────

`api/guard.py` is not used here and must not be. Its seven checks are built for
an endpoint on the open internet reached by a phone with an API key, and its
fourth check — an Active Mobile Access Grant — would mean the operator could not
draw a boundary on their own Desk until they had enrolled themselves as a field
device. That is the wrong gate on the wrong door.

The right gate is the one the Desk already applies to the form the button is on:

  1. a named user (Guest is refused before anything is read or fetched),
  2. `frappe.has_permission(doctype, "write", doc=name, throw=True)` on the
     SPECIFIC document — not the doctype in general, so a User Permission that
     scopes somebody to one company scopes this too,
  3. a closed list of three doctypes. There is no dispatcher and no
     method-name argument: `SAVEABLE` below is the whole reachable surface and
     `create_journal_entry` is not in it.

Check 2 matters more than it looks. The boundary tools end in
`doc.save(ignore_permissions=True)` — correct for them, because the MCP transport
did its own authorisation three layers earlier — so a wrapper that forgot to ask
would have handed every signed-in user on the site a write to every parcel.

THE `allow_<tool>` SWITCHES ARE DELIBERATELY NOT CONSULTED, the same call
`api/__init__.py` made for the phone and for the same reason: those switches are
the AI's leash. `allow_set_parcel_boundary` off means "the model may not redraw
the farm", and reading it here would mean an operator who distrusts the model
also loses the ability to trace a parcel by hand — which is not what they asked
for and not what the switch says.

────────────────────────────────────────────────────────────────────────────
THE COUNTY LOOKUP, AND WHY IT IS PROXIED RATHER THAN FETCHED BY THE BROWSER
────────────────────────────────────────────────────────────────────────────

Wasco County publishes its tax lots as an ArcGIS FeatureServer, free, with no
key, in WGS84 on request. That is the same polygon the assessor, the deed and
the tax bill are describing, which makes it a far better starting point than a
person tracing an outline off a satellite image by eye — and tracing it by eye
is what everybody does when importing is hard.

The browser could call it directly. It should not, for three reasons:

  * CORS is not ours to promise. The endpoint may or may not send
    `Access-Control-Allow-Origin` today and may stop tomorrow, and the failure
    is a console error on somebody's form with no server-side trace of why.
  * One URL, one place. A county's endpoint that moves is one constant here
    rather than a string in a JavaScript file that a browser has cached.
  * The `where` clause is a query language, and the browser is the wrong place
    to be careful about it. See `_tax_lot_clause`.

`requests` IS IMPORTED DEFENSIVELY, like shapely and h3 and segno before it. It
is a Frappe dependency and every real bench has it; a bench that somehow does not
loses the county lookup BY NAME, with the reason, rather than failing to import
this module and taking `save_boundary` down with it. Drawing a boundary by hand
needs no network at all and must keep working.

NOTHING HERE IS A GENERAL HTTP PROXY. The host and path come from `COUNTIES`
below, which is a literal in this file; the caller chooses a key in that dict and
supplies a tax lot, an account number or a coordinate pair, all validated before
they are formatted. There is no argument from which a URL could be built.

────────────────────────────────────────────────────────────────────────────
v0.126.0: THE TAX LOT SEARCH HAD NEVER MATCHED A PARCEL
────────────────────────────────────────────────────────────────────────────

Read against the live layer rather than against the docstring, the search built
in v0.33.0 could not succeed. `MapTaxlot` on Wasco's server is stored SPACE-
DELIMITED and UNPADDED — `2N 11E 1 CC 4039` — and this file sent
`MapTaxlot='2N11E35BA-01600'`, the compact ORMAP spelling off a deed. Those are
never equal, and an ArcGIS query that matches nothing answers HTTP 200 with an
empty feature list. So the form reported "Wasco County, Oregon has no parcel
matching that tax lot number" for every tax lot in the county, which read as a
county problem and was ours.

The old allowlist closed the other direction at the same time: it refused any
value containing a space, so an operator who typed the county's OWN spelling was
told it was "not shaped like a tax lot number". Both halves of the round trip
were shut, and neither said so.

`_tax_lot_parts` now translates. It reads the grammar the layer actually holds —
verified across all 15,516 rows — and accepts the deed's compact spelling, the
county's spaced one, and the hyphenated middle ground, emitting the one the
server will match. The allowlist is TIGHTER than before, not looser: every part
is matched against digits and a known letter, so the clause cannot contain a
character no branch allowed.

AND AN ACCOUNT NUMBER IS NOW A SEARCH, which is the one an operator can read off
a tax statement without transcribing five fields in the right order. `AccountNum`
is an integer column, so `_account_clause` formats from `int()` and the clause
carries no quotes at all.
"""

from __future__ import annotations

import json
import re

import frappe

from .. import audit, fsa, geo, security
from ..errors import ToolError
from ..tools import farm as farm_tools
from ..tools import fsa as fsa_tools
from ..tools import realestate as realestate_tools

try:  # pragma: no cover - a bench without Frappe's own HTTP client
	import requests

	HAVE_REQUESTS = True
except Exception:  # pragma: no cover
	requests = None
	HAVE_REQUESTS = False


#: Every county this app knows how to ask, and how to ask it.
#:
#: A LITERAL, AND THE ONLY SOURCE OF A HOSTNAME IN THIS FILE. The caller picks a
#: key; it cannot supply a URL, a host, a path or a format. That is what keeps a
#: whitelisted method that makes an outbound request from being a way to make the
#: server fetch anything at all on somebody's behalf.
#:
#: WASCO IS THE ONLY ONE, and the dict shape is not speculation about a second:
#: it is what makes "which county is this parcel in" a question the code can
#: answer badly (with a named refusal listing what it does know) rather than one
#: it cannot ask. A farm two miles east is in Sherman County, whose server is a
#: different vendor entirely.
#:
#: `spatial_reference` IS RECORDED AND NOT SENT. The layer stores Oregon
#: Stateplane North in feet (WKID 2913); `outSR=4326` asks the server to project
#: to WGS84 degrees on the way out, which is the only projection this app can
#: read. It is here so that the number in the release notes and the number in the
#: request agree, and so a future county with a different native grid is a data
#: change rather than a puzzle.
COUNTIES = {
	"wasco": {
		"label": "Wasco County, Oregon",
		"url": "https://public.co.wasco.or.us/gisserver/rest/services/Taxlots/FeatureServer/0/query",
		"tax_lot_field": "MapTaxlot",
		#: The assessor's account number, and an INTEGER column rather than a
		#: string one — so its clause carries no quotes at all. See
		#: `_account_clause`.
		"account_field": "AccountNum",
		"spatial_reference": 2913,
		#: Where each thing this app wants lives in the county's own schema, in
		#: the order to try. Read case-insensitively — an ArcGIS layer's field
		#: names are whatever the person who published it typed.
		"properties": {
			"tax_lot": ("MapTaxlot", "MAPTAXLOT", "Taxlot"),
			"taxpayer": ("Taxpayer", "TAXPAYER", "OwnerName"),
			"acres": ("CalculatedAcres", "CALCULATEDACRES", "Acres", "GISAcres"),
			"account": ("AccountNum", "ACCOUNTNUM", "Account"),
		},
	}
}

DEFAULT_COUNTY = "wasco"

#: The one doctype the FSA import writes to. Named rather than passed in: a CLU
#: is a FIELD boundary and nothing else, which is the whole reason the county
#: import is on Parcel and this one is not.
FIELD_DOCTYPE = "Field"

#: The doctypes a boundary can be saved to, the argument each tool wants it
#: under, and the tool itself. THIS IS THE WHOLE REACHABLE SURFACE of
#: `save_boundary` — there is no lookup by name, no registry consulted and no
#: fourth entry that arrives without somebody editing this line.
SAVEABLE = {
	"Parcel": ("parcel", realestate_tools.set_parcel_boundary),
	"Field": ("field", farm_tools.set_field_boundary),
	"Irrigation Zone": ("zone", farm_tools.set_zone_boundary),
}

#: THE COUNTY DOES NOT STORE THE SPELLING ANYBODY TYPES, and that one fact is
#: why v0.33.0's tax lot search never returned a parcel. Wasco's layer holds
#: `MapTaxlot` SPACE-DELIMITED and UNPADDED — `2N 11E 1 CC 4039`, `1N 13E 7 200`
#: — while every deed, every tax bill and this app's own form description use
#: the compact ORMAP spelling `2N11E35BA-01600`. `MapTaxlot='2N11E35BA-01600'`
#: matches nothing, ever, and an ArcGIS query that matches nothing is an HTTP 200
#: with an empty feature list: the form said "the county has no parcel matching
#: that tax lot number" and every word of that was wrong.
#:
#: Worse, the OLD allowlist below refused the county's own spelling on the way
#: in, because it had no space in it. So both halves of the round trip were shut.
#:
#: The grammar here was read off all 15,516 rows of the live layer rather than
#: guessed: township is one digit and N or S, range is two digits and E or W,
#: section is one or two digits (0 for a lot that is not in a section), the
#: quarter is one or two letters and often absent, and the lot is three to five
#: digits. Nothing is zero-padded and nothing is hyphenated.
_TOWNSHIP = re.compile(r"^\d{1,2}[NS]$")
_RANGE = re.compile(r"^\d{1,2}[EW]$")
_SECTION = re.compile(r"^\d{1,2}$")
_QUARTER = re.compile(r"^[A-Z]{1,3}$")
_LOT = re.compile(r"^\d{1,5}$")

#: Anything a person might put between the parts: a space, a hyphen, a dot, a
#: slash, an underscore. Split on, never sent.
_SEPARATORS = re.compile(r"[\s.\-_/]+")

#: The compact ORMAP spelling, which is the one printed on a deed and the one
#: already sitting in `parcel_id` on parcels imported before this release.
#:
#: IT IS ONLY PARSEABLE BECAUSE IT IS PADDED. `2N11E35BA01600` splits because the
#: township and range end in their own letter, the section is exactly two digits
#: and the lot exactly five — so the quarter is whatever letters are left in the
#: middle. The unpadded run-together `2N11E7200` is genuinely ambiguous (section
#: 7 lot 200, or section 72 lot 00?) and is refused by name rather than guessed.
_COMPACT = re.compile(r"^(\d{1,2}[NS])(\d{1,2}[EW])(\d{2})([A-Z]{0,3})(\d{5})$")

#: An assessor's account number: `7503`. Digits, and an INTEGER column at the far
#: end, so the clause it builds has no quotes in it to escape.
_ACCOUNT = re.compile(r"^\d{1,9}$")

#: How long to wait for a county server that a farm's bench reaches over the
#: same connection everything else uses. Fifteen seconds is past a slow answer
#: and short enough that a form does not look hung.
_TIMEOUT_SECONDS = 15

#: How much of a response to read. A tax lot polygon is a few kilobytes; a
#: spatial query that somehow matched the whole county would not be. Capped
#: because an unbounded read of a remote body is a way to fill a worker's memory
#: from outside.
_MAX_BYTES = 4 * 1024 * 1024

#: The most features to hand back to a form. A point lands in one tax lot;
#: overlapping lots and a point on a shared line make two or three. Twenty is far
#: past any honest answer and stops a mistake becoming a payload.
_MAX_FEATURES = 20


def requests_sentence() -> str:
	return (
		"the county GIS lookup needs the `requests` package, which this bench does not have. "
		"Install it with `bench pip install requests` (it is normally already there as a Frappe "
		"dependency), or draw the boundary by hand — that path needs no network at all."
	)


# ── the gates ───────────────────────────────────────────────────────────────
def speaks_frappe(implementation, *args, **kwargs):
	"""Run one of the two implementations below, turning `ToolError` into a modal.

	A `ToolError` means "you asked for something I can't do, and that is not a
	bug" — an unknown tax lot, a polygon that crosses itself, an acreage that
	disagrees with the shape by half. On the MCP transport it becomes a tool
	error with the message intact. Raised out of a `@frappe.whitelist()` method
	it would become an HTTP 500 and a traceback in the browser console, and the
	sentence the tool wrote — the one that says which two acreages disagreed and
	what to do about it — would never reach the person who needs it.

	`frappe.throw` is the Desk's own channel for exactly this: a modal with the
	message in it. Anything that is NOT a ToolError is left alone and still
	reaches the Error Log with its traceback, because that is a bug and hiding it
	would be the wrong favour.

	A FUNCTION AND NOT A DECORATOR, which looks like the clumsier of the two and
	is the correct one here. `frappe.call` reads the whitelisted callable's
	argument names with `inspect.getfullargspec`, which does NOT follow
	`functools.wraps` — so a decorated method presents as `(*args, **kwargs)`,
	and Frappe answers a `(*args, **kwargs)` signature by forwarding the ENTIRE
	form dict, `cmd` and `csrf_token` included. The wrapped function then raises
	TypeError on an argument the browser never sent on purpose. Keeping the real
	signature on the whitelisted function is what stops that.
	"""
	try:
		return implementation(*args, **kwargs)
	except ToolError as error:
		frappe.throw(str(error), title="Boundary")


def _named_user() -> str:
	"""The signed-in user, or a refusal. Guest never gets past this line."""
	user = str(getattr(frappe.session, "user", "") or "")
	if not user or user == "Guest":
		frappe.throw(
			"You must be signed in to use the map tools.",
			frappe.PermissionError,
		)
	return user


def _may_write(doctype: str, name: str = "") -> None:
	"""Frappe's own answer to "may this person change this record".

	`doc=name` rather than the bare doctype ON PURPOSE. A User Permission that
	scopes somebody to one company is enforced per document, and a check written
	against the doctype alone would pass for a parcel they cannot open.
	"""
	frappe.has_permission(doctype, "write", doc=name or None, throw=True)


# ── the county lookup ───────────────────────────────────────────────────────
def _county(name: str = "") -> tuple:
	"""`(key, config)` for a county this app knows, or a refusal that lists them."""
	key = str(name or DEFAULT_COUNTY).strip().lower().replace(" county", "").replace(" ", "_")
	config = COUNTIES.get(key)
	if not config:
		raise ToolError(
			f"no county GIS service is configured for {name!r}. "
			f"Known: {', '.join(sorted(COUNTIES)) or '<none>'}. A county's parcel layer is a "
			"different server and a different schema for every county, so one is added by "
			"naming it rather than by guessing at a URL."
		)
	return key, config


#: The sentence every tax lot refusal ends with. One place, because a person who
#: has just been told "no" needs to be told what yes looks like, and there are
#: five ways to get here.
_TAX_LOT_HELP = (
	"Wasco County stores tax lots as township, range, section, quarter, lot — "
	"'2N 11E 1 CC 4039', or '1N 13E 7 200' where there is no quarter. The compact "
	"spelling off a deed, '2N11E35BA-01600', is accepted and translated. If you have "
	"the assessor's account number instead, search by that."
)


def _tax_lot_parts(tax_lot) -> tuple:
	"""`(township, range, section, quarter, lot)` in the county's own spelling.

	THE TRANSLATION IS THE WHOLE FIX. Whatever the operator typed — the deed's
	`2N11E35BA-01600`, the county's `2N 11E 35 BA 1600`, the half-way house
	`2N-11E-35-BA-1600` — comes out as the five parts the layer actually holds,
	with the section and the lot stripped back to the unpadded numbers it stores.

	STILL AN ALLOWLIST AND NOT AN ESCAPE, and a tighter one than v0.33.0's. Every
	part is matched against a regex of digits and a known letter before it is
	formatted, so there is no quote to smuggle and no expression to build — the
	clause this ends in cannot contain a character no branch here allowed.
	"""
	text = str(tax_lot if tax_lot is not None else "").strip().upper()
	if not text:
		raise ToolError(f"which tax lot? {_TAX_LOT_HELP}")

	# SEPARATED FIRST, COMPACT SECOND, and the deed's own `2N11E35BA-01600` needs
	# both: it has a hyphen, so it splits — into two pieces rather than five. So
	# the split is TRIED and the compact parse is what catches everything the
	# split did not resolve into the four or five parts a tax lot has.
	tokens = [token for token in _SEPARATORS.split(text) if token]
	if len(tokens) not in (4, 5):
		compact = _COMPACT.match("".join(tokens))
		if not compact:
			raise ToolError(
				f"{tax_lot!r} is not shaped like a tax lot. Written with no separators, the "
				"section has to be two digits and the lot five — '2N11E35BA01600' — because "
				"'2N11E7200' could be section 7 lot 200 or section 72 lot 00, and guessing would "
				f"import the wrong piece of ground. {_TAX_LOT_HELP}"
			)
		tokens = [token for token in compact.groups() if token]

	if len(tokens) == 4:
		township, range_, section, lot = tokens
		quarter = ""
	elif len(tokens) == 5:
		township, range_, section, quarter, lot = tokens
	else:  # pragma: no cover - the compact fallback above already refused these
		raise ToolError(
			f"{tax_lot!r} has {len(tokens)} part(s) and a tax lot has four or five. {_TAX_LOT_HELP}"
		)

	checks = (
		(_TOWNSHIP, township, "township", "1N or 2S"),
		(_RANGE, range_, "range", "11E or 13E"),
		(_SECTION, section, "section", "7, 35, or 0 where there is none"),
		(_LOT, lot, "lot", "200 or 4039"),
	)
	for pattern, value, label, example in checks:
		if not pattern.match(value):
			raise ToolError(f"{value!r} is not a {label} — Wasco's look like {example}. {_TAX_LOT_HELP}")
	if quarter and not _QUARTER.match(quarter):
		raise ToolError(
			f"{quarter!r} is not a quarter — Wasco's look like CC or A, and many lots have none. "
			f"{_TAX_LOT_HELP}"
		)

	# The layer pads NOTHING: section 0, section 7, lot 200, lot 4039. A deed's
	# `01600` and a form's `1600` are the same lot and only one of them matches.
	return township, range_, str(int(section)), quarter, str(int(lot))


def canonical_tax_lot(tax_lot) -> str:
	"""`'2N 11E 35 BA 1600'` — the county's own spelling, from any of ours."""
	township, range_, section, quarter, lot = _tax_lot_parts(tax_lot)
	return " ".join(part for part in (township, range_, section, quarter, lot) if part)


def _tax_lot_clause(config: dict, tax_lot: str) -> str:
	"""`MapTaxlot='2N 11E 35 BA 1600'`, from a value proven harmless part by part.

	The quoting here is trivial precisely because the validation above is not:
	every part has already been matched against digits and a known letter, so
	there is no quote to escape and no expression to smuggle.
	"""
	return f"{config['tax_lot_field']}='{canonical_tax_lot(tax_lot)}'"


def _account_clause(config: dict, account) -> str:
	"""`AccountNum=7503`. AN INTEGER COLUMN, so the clause carries no quotes.

	`AccountNum` is `esriFieldTypeInteger` on the live layer, and quoting an
	integer is the kind of thing an ArcGIS backend answers with a type error
	rather than a match. The value is parsed to an `int` and formatted from the
	int — which is a stronger guarantee than any allowlist, because there is no
	path from a string the caller supplied to the string that is sent.
	"""
	text = str(account if account is not None else "").strip()
	if not text:
		raise ToolError("which account? An assessor's account number looks like 7503.")
	if not _ACCOUNT.match(text):
		raise ToolError(
			f"{account!r} is not an account number. Wasco's are plain digits — 7503 — with no "
			"letters, spaces or punctuation. A number with a hyphen in it is a tax lot; search "
			"by that instead."
		)
	number = int(text)
	if number <= 0:
		raise ToolError(f"{account!r} is not an account number this county issues; they start at 1.")
	return f"{config['account_field']}={number}"


def _degrees(value, label: str, limit: float) -> float:
	number = str(value if value is not None else "").strip()
	try:
		degrees = float(number)
	except (TypeError, ValueError):
		raise ToolError(f"{label} must be a number in degrees, not {value!r}.") from None
	if degrees != degrees or degrees in (float("inf"), float("-inf")):  # NaN or infinity
		raise ToolError(f"{label} must be a real number in degrees, not {value!r}.")
	if abs(degrees) > limit:
		raise ToolError(f"{label} must be between -{limit} and {limit} degrees, not {degrees}.")
	return degrees


def _fetch(url: str, params: dict) -> dict:
	"""The one outbound request this app makes. Returns parsed JSON.

	SEPARATED OUT SO IT CAN BE REPLACED, which is what the tests do — every other
	function here is pure and testable without a network, and this one is the
	only thing between them and a county server that is nobody's dependency.
	"""
	if not HAVE_REQUESTS:
		raise ToolError(requests_sentence())
	try:
		response = requests.get(url, params=params, timeout=_TIMEOUT_SECONDS, stream=True)
	except Exception as error:  # pragma: no cover - exercised by a bench with no route out
		raise ToolError(
			f"the county GIS server could not be reached ({type(error).__name__}: {error}). "
			"Nothing was changed. A boundary can still be drawn by hand on the map, which "
			"needs no network."
		) from None
	try:
		if response.status_code != 200:
			raise ToolError(
				f"the county GIS server answered HTTP {response.status_code}. Nothing was "
				"changed. That is the county's server rather than this site — try again, or "
				"draw the boundary by hand."
			)
		body = b""
		for chunk in response.iter_content(chunk_size=65536):
			body += chunk or b""
			if len(body) > _MAX_BYTES:
				raise ToolError(
					f"the county GIS server sent more than {_MAX_BYTES // (1024 * 1024)} MB for "
					"one query. That is not one parcel — narrow the search to a tax lot number."
				)
	finally:
		try:
			response.close()
		except Exception:  # pragma: no cover
			pass

	try:
		return json.loads(body.decode("utf-8", "replace") or "{}")
	except json.JSONDecodeError as error:
		raise ToolError(
			f"the county GIS server did not answer with JSON ({error}). Nothing was changed."
		) from None


def _property(properties: dict, candidates: tuple):
	"""One value from a county's own schema, whatever case it published it in."""
	if not isinstance(properties, dict):
		return None
	lowered = {str(key).lower(): value for key, value in properties.items()}
	for candidate in candidates:
		if candidate in properties:
			return properties[candidate]
		if str(candidate).lower() in lowered:
			return lowered[str(candidate).lower()]
	return None


def _acres(value):
	try:
		acres = round(float(value), 2)
	except (TypeError, ValueError):
		return None
	return acres if acres > 0 else None


def parse_features(payload: dict, config: dict) -> tuple:
	"""`(features, warnings)` — the county's answer in this app's own vocabulary.

	PURE, AND SEPARATE FROM THE FETCH, so the shape of a real ArcGIS response is
	something the test suite pins down without a network.

	AN ArcGIS ERROR IS A 200. The service answers `{"error": {"code": 400,
	"message": "..."}}` with an HTTP 200 and a JSON content type, so a caller that
	checks only the status code reads a failure as an empty result — which on this
	path would mean "the county has never heard of your parcel" when what happened
	was a malformed query. It is checked first, by name.

	A POINT OR A LINE IS DROPPED RATHER THAN RETURNED. Some parcel layers carry
	annotation geometry alongside the lots. A boundary is an area, the three
	boundary tools refuse anything that is not, and handing the form a shape it
	will only be refused for later is worse than saying so here.
	"""
	warnings = []
	if not isinstance(payload, dict):
		raise ToolError("the county GIS server sent something that is not a GeoJSON object.")

	error = payload.get("error")
	if isinstance(error, dict):
		message = str(error.get("message") or "no message").strip()
		details = "; ".join(str(line) for line in (error.get("details") or []) if line)
		raise ToolError(
			f"the county GIS service refused the query: {message}"
			f"{' — ' + details if details else ''}. Nothing was changed."
		)

	raw = payload.get("features")
	if not isinstance(raw, list):
		raise ToolError(
			"the county GIS server did not send a GeoJSON FeatureCollection. Nothing was changed."
		)

	names = config.get("properties") or {}
	out = []
	dropped = 0
	for entry in raw:
		if not isinstance(entry, dict):
			continue
		geometry = entry.get("geometry")
		if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
			dropped += 1
			continue
		properties = entry.get("properties") or entry.get("attributes") or {}
		feature = {
			"tax_lot": _text(_property(properties, names.get("tax_lot", ()))),
			"taxpayer": _text(_property(properties, names.get("taxpayer", ()))),
			"county_acres": _acres(_property(properties, names.get("acres", ()))),
			"account": _text(_property(properties, names.get("account", ()))),
			"geometry": geometry,
			"area_computed_acres": _computed_acres(geometry),
		}
		out.append(feature)
		if len(out) >= _MAX_FEATURES:
			break

	if dropped:
		warnings.append(
			f"{dropped} shape(s) in the county's answer were not areas and were left out. A "
			"boundary has to be a Polygon or a MultiPolygon."
		)
	if len(raw) > _MAX_FEATURES:
		warnings.append(
			f"The county matched {len(raw)} shapes and the first {_MAX_FEATURES} are shown. "
			"Search by tax lot number to get one."
		)
	return out, warnings


def _text(value):
	text = str(value if value is not None else "").strip()
	return text or None


def _computed_acres(geometry: dict):
	"""What this app makes of the county's polygon, on a bench that can measure.

	Reported ALONGSIDE the county's own `CalculatedAcres` and never instead of
	it. They are two measurements — the county's on its own projected grid, this
	one spherical — and where they disagree by more than a rounding, that is
	information rather than an error to hide.
	"""
	if not geo.available():
		return None
	try:
		return geo.area_acres(geo.parse(geometry, "county boundary"))
	except Exception:
		return None


# ── the whitelisted surface: two methods ────────────────────────────────────
@frappe.whitelist()
def query_county_parcels(county=None, tax_lot=None, account=None, lat=None, lon=None):
	"""Ask a county's parcel layer for a shape, by tax lot, by account, or by a point."""
	return speaks_frappe(
		_query_county_parcels, county=county, tax_lot=tax_lot, account=account, lat=lat, lon=lon
	)


@frappe.whitelist()
def save_boundary(doctype=None, name=None, geojson=None, dry_run=0):
	"""Save a drawn or imported boundary, through the tool that validates it."""
	return speaks_frappe(_save_boundary, doctype=doctype, name=name, geojson=geojson, dry_run=dry_run)


@frappe.whitelist()
def read_fsa_clu(content=None, filename=None):
	"""Parse an uploaded FSA Common Land Unit file. Reads nothing on the site."""
	return speaks_frappe(_read_fsa_clu, content=content, filename=filename)


@frappe.whitelist()
def import_fsa_clu(content=None, filename=None, parcel=None, create_missing=0, apply=0):
	"""Match a whole CLU file against this site's blocks, and optionally write."""
	return speaks_frappe(
		_import_fsa_clu,
		content=content,
		filename=filename,
		parcel=parcel,
		create_missing=create_missing,
		apply=apply,
	)


def _read_fsa_clu(content=None, filename=None):
	"""Turn the bytes a browser just read off a memory stick into CLUs on a map.

	NO FILE RECORD IS CREATED. The upload goes into this request, is parsed, and
	the shapes come back — nothing is stored, so a grower who picked the wrong
	export off the stick has not left a copy of it on the site. The whole point of
	the panel this feeds is looking BEFORE anything is written.

	WRITE PERMISSION ON Field IS THE GATE, and it is deliberately stricter than
	the parse this performs. The only thing a CLU polygon is for here is setting a
	block's boundary; gating on `read` would leave the site parsing arbitrary
	uploaded archives for any signed-in account, which is a larger surface than
	the feature needs.
	"""
	_named_user()
	_may_write(FIELD_DOCTYPE)

	payload = fsa.read(fsa.decode_upload(content, "the uploaded file"), str(filename or ""))
	clus = []
	for index, feature in enumerate(payload.get("features") or [], start=1):
		attributes = fsa.canonical_attributes(feature.get("properties") or {})
		entry = {
			"index": index,
			"clu": fsa.clu_key(attributes),
			"suggested_field_name": fsa.suggested_field_name(attributes),
			"farm_number": attributes.get("farm_number"),
			"tract_number": attributes.get("tract_number"),
			"clu_number": attributes.get("clu_number"),
			"clu_identifier": attributes.get("clu_identifier"),
			"calc_acres": attributes.get("calc_acres"),
			"hel_type": attributes.get("hel_type"),
			"geometry": None,
			"computed_acres": None,
			"error": "",
		}
		try:
			geometry = geo.parse(feature.get("geometry"), f"CLU {entry['clu']}")
			entry["geometry"] = geometry
			entry["computed_acres"] = geo.area_acres(geometry)
		except ToolError as error:
			entry["error"] = str(error)
		clus.append(entry)

	return {
		"format": payload.get("format"),
		"crs": payload.get("crs"),
		"source_files": payload.get("source_files") or [],
		"warnings": list(payload.get("warnings") or []),
		"clu_count": len(clus),
		"clus": clus,
	}


def _import_fsa_clu(content=None, filename=None, parcel=None, create_missing=0, apply=0):
	"""The whole file at once, through the same tool the AI calls.

	THE GATE IS COARSER THAN `save_boundary`'S AND SAYS SO. That method knows
	which record it is about and asks Frappe whether this person may write to
	THAT document; a bulk import does not know which blocks it will touch until
	after it has matched them, so the check here is write permission on Field as
	a doctype. A site that scopes somebody to one company with a User Permission
	should not put this button in front of them — the per-CLU report names every
	block it changed, which is the compensating control and not a substitute for
	the one above.
	"""
	user = _named_user()
	_may_write(FIELD_DOCTYPE)

	truthy = ("1", "true", "True", "yes")
	arguments = {
		"file_base64": content,
		"filename": str(filename or ""),
		"create_missing": str(create_missing) in truthy,
		"apply": str(apply) in truthy,
	}
	if parcel:
		arguments["parcel"] = str(parcel)
		owner = frappe.db.get_value("Parcel", str(parcel), "owning_entity")
		if owner:
			arguments["owning_entity"] = owner

	result = fsa_tools.import_fsa_clu_boundaries(arguments)
	data = dict(result.data or {})

	audit.record(
		tool_name="desk:import_fsa_clu_boundaries",
		arguments={
			"filename": arguments["filename"],
			"parcel": arguments.get("parcel"),
			"create_missing": arguments["create_missing"],
			"apply": arguments["apply"],
		},
		summary=result.summary,
		docstatus_delta=result.docstatus_delta,
		caller_ip=_caller_ip(),
	)

	return {"user": user, "summary": result.summary, **data}


def _query_county_parcels(county=None, tax_lot=None, account=None, lat=None, lon=None):
	"""Ask a county's parcel layer for a shape, by tax lot, by account, or by a point.

	THREE WAYS TO ASK, AND EXACTLY ONE PER CALL. The account number is the one an
	operator can actually read off a tax statement without transcribing five
	fields, and it is the layer's own integer key — so it is the search that
	cannot be spelled wrong. See `_account_clause` and `_tax_lot_parts`.

	WRITE PERMISSION ON Parcel IS THE GATE, and it is deliberately stricter than
	the read this method performs. The only thing an imported polygon is for is
	setting a parcel's boundary; gating on `read` would leave the site hosting an
	outbound HTTP fetch that any signed-in account — a Family Member, an Advisor
	— could drive. That is a small thing to hand out and there is no reason to.
	"""
	_named_user()
	_may_write("Parcel")

	key, config = _county(county)
	tax_lot = str(tax_lot if tax_lot is not None else "").strip()
	account = str(account if account is not None else "").strip()
	has_point = lat not in (None, "") and lon not in (None, "")

	asked_for = [
		label
		for label, given in (
			("tax lot number", bool(tax_lot)),
			("account number", bool(account)),
			("point", has_point),
		)
		if given
	]
	if len(asked_for) > 1:
		raise ToolError(
			f"pass one of these, not {len(asked_for)}: {', '.join(asked_for)}. They are separate "
			"questions and answering them together would hide which one matched."
		)

	params = {
		"outFields": "*",
		"outSR": 4326,
		"returnGeometry": "true",
		"f": "geojson",
	}
	if tax_lot:
		asked = {"tax_lot": canonical_tax_lot(tax_lot)}
		params["where"] = _tax_lot_clause(config, tax_lot)
	elif account:
		params["where"] = _account_clause(config, account)
		# The clause is `AccountNum=7503` and the number in it has already been
		# through `int()`, so reading it back off the clause records exactly what
		# the county was asked rather than what the box was typed into.
		asked = {"account": int(params["where"].split("=", 1)[1])}
	elif has_point:
		latitude = _degrees(lat, "lat", 90.0)
		longitude = _degrees(lon, "lon", 180.0)
		asked = {"lat": latitude, "lon": longitude}
		# `x` is longitude and `y` is latitude — the opposite order from every
		# other pair in this app, and the mistake this comment exists to stop.
		# Getting it round the wrong way returns the tax lot at 45.6°E,
		# -121.18°N, which is in the Southern Ocean and comes back empty rather
		# than wrong, so nothing would ever say what happened.
		params["geometry"] = json.dumps({"x": longitude, "y": latitude})
		params["geometryType"] = "esriGeometryPoint"
		params["inSR"] = 4326
		params["spatialRel"] = "esriSpatialRelIntersects"
	else:
		raise ToolError(
			"pass a tax lot number, an assessor's account number, or a lat and lon to look under "
			"a point on the map."
		)

	payload = _fetch(config["url"], params)
	features, warnings = parse_features(payload, config)

	if not features:
		matched_on = "tax lot number" if tax_lot else ("account number" if account else "point")
		warnings.append(f"{config['label']} has no parcel matching that {matched_on}. Nothing was changed.")

	audit.record(
		tool_name="desk:query_county_parcels",
		arguments={"county": key, **asked},
		summary=f"{config['label']}: {len(features)} parcel(s) matched",
		caller_ip=_caller_ip(),
	)

	return {
		"county": key,
		"label": config["label"],
		"query": asked,
		"count": len(features),
		"features": features,
		"warnings": warnings,
	}


def _save_boundary(doctype=None, name=None, geojson=None, dry_run=0):
	"""Save a drawn or imported boundary, through the tool that validates it.

	THIS FUNCTION DOES NOT WRITE A FIELD. It checks who is asking, checks they
	may write to this exact document, and then calls the boundary tool — which
	parses the polygon, refuses a self-intersection, compares the enclosed area
	against the recorded acreage, reports what now falls outside the shape and
	recomputes every derived field. Everything that makes a boundary trustworthy
	lives there and none of it is reimplemented here.

	`dry_run` GOES STRAIGHT THROUGH, because the tools already have it and the
	map has an obvious use for it: "what would this shape do" before "do it".
	"""
	user = _named_user()
	doctype = str(doctype or "").strip()
	name = str(name or "").strip()

	if doctype not in SAVEABLE:
		raise ToolError(
			f"{doctype or '<none>'} does not carry a boundary this way. Known: {', '.join(sorted(SAVEABLE))}."
		)
	if not name:
		raise ToolError(f"which {doctype}? A record name is required.")
	if not frappe.db.exists(doctype, name):
		raise ToolError(f"there is no {doctype} named {name!r} on this site. Nothing was changed.")

	_may_write(doctype, name)

	argument, tool = SAVEABLE[doctype]
	arguments = {
		argument: name,
		"boundary_geojson": geojson,
		"dry_run": 1 if str(dry_run) in ("1", "true", "True") else 0,
	}

	# The tools resolve a company when they are not given one, and on a
	# multi-company site that is a refusal rather than a guess. The record in
	# front of the user already knows which company it belongs to, so read it
	# from there — a person who has the form open should never be asked which of
	# their companies the parcel they are looking at is on.
	owner = frappe.db.get_value(doctype, name, "owning_entity")
	if owner:
		arguments["owning_entity"] = owner

	result = tool(arguments)
	data = dict(result.data or {})

	audit.record(
		tool_name=f"desk:{tool.__name__}",
		arguments={"doctype": doctype, "name": name, "dry_run": arguments["dry_run"]},
		summary=result.summary,
		docstatus_delta=result.docstatus_delta,
		caller_ip=_caller_ip(),
	)

	return {
		"doctype": doctype,
		"name": name,
		"user": user,
		"changed": bool(data.get("changed")),
		"dry_run": bool(data.get("dry_run")),
		"summary": result.summary,
		"warnings": list(data.get("warnings") or []),
		"area_computed_acres": data.get("area_computed_acres"),
		"boundary_centroid": data.get("boundary_centroid"),
		"data": data,
	}


def _caller_ip() -> str:
	try:
		return security.caller_ip()
	except Exception:  # pragma: no cover - a call with no request behind it
		return ""
