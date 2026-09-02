# SPDX-License-Identifier: MIT
"""Read access to the "ERPNext MCP Settings" Single doctype.

Every gate in this app resolves through here, so there is exactly one place
that decides what "enabled" means. Three properties of this module matter:

MISSING IS NOT OFF. A Frappe Single stores one row per *set* field in
`tabSingles`; a field nobody has saved yet has no row at all, so a naive read
returns None. For the read-tool switches — which default ON — treating None as
"off" would silently disable the whole read surface on a fresh install. So
`tool_enabled()` falls back to the field's declared default from the DocType
meta whenever no value is stored. `seed_defaults()` (run on install and by a
patch after upgrades) writes the values out properly; the meta fallback is the
belt to that braces, and it is what makes adding a new tool in a later version
safe without a migration.

"0" IS NOT ON. `tabSingles.value` is a text column, so depending on the Frappe
version a Check field on a Single comes back as the *string* `"0"` rather than
the integer 0 — and `bool("0")` is True. Reading a switch with a bare `bool()`
would therefore report every disabled tool as enabled, including the master
switch, which is about the worst possible direction for that bug to fail in.
Every boolean here goes through `as_bool` — public, because the doctype
controller has to make the same judgement about the same values — which treats
"0", "", "false" and None as off. This is not defensive padding: it is the difference between a
fresh install being inert and a fresh install answering with write tools live.

THE TOKEN IS NEVER RETURNED TO A CALLER. `auth_token()` exists only for the
constant-time comparison in `security.py`. Nothing logs it, nothing renders it,
and the doctype stores it in a Password field so it is encrypted at rest and
excluded from `frappe.get_doc().as_dict()` output.

READS ARE CACHED. `frappe.get_cached_doc` on a Single is a redis hit, and the
framework invalidates it on save, so the per-tool switch check costs nothing
even though it runs on every single call.
"""

import frappe

SETTINGS_DOCTYPE = "ERPNext MCP Settings"

#: The Frappe user a mutation is attributed to when `require_user_context` is
#: off, or when it is on but no MCP System User has been chosen.
FALLBACK_USER = "Administrator"

#: Sensible LAN defaults: loopback (v4 and v6) plus the three RFC1918 blocks.
#: Deliberately NOT 0.0.0.0/0 — an operator who wants the endpoint reachable
#: from anywhere has to type that themselves.
#:
#: `::1/128` is in the list because it has to be: on a modern host `localhost`
#: resolves to IPv6 first, so a first-run `curl http://localhost/api/method/...`
#: arrives from `::1` and would otherwise be denied by a list that "obviously"
#: allows loopback. Sites that reach the endpoint over IPv6 from elsewhere on
#: the LAN need to add their own ULA prefix (typically `fd00::/8`).
DEFAULT_CIDRS = "127.0.0.1/32,::1/128,10.0.0.0/8,192.168.0.0/16,172.16.0.0/12"


def get_settings():
	"""The settings document. Cached; safe to call repeatedly per request."""
	return frappe.get_cached_doc(SETTINGS_DOCTYPE)


def is_enabled() -> bool:
	"""The master switch. Off → the endpoint behaves as if it does not exist.

	This is the "turn everything off right now" control: one checkbox, no
	restart, no token rotation, takes effect on the next request.
	"""
	return as_bool(_value("enabled"))


def auth_token() -> str:
	"""The configured bearer token, or "" when none has been generated.

	Never log, echo, or include this in a tool result.
	"""
	try:
		token = get_settings().get_password("auth_token", raise_exception=False)
	except Exception:
		# A Password field with no stored value, or an undecryptable one on a
		# site whose encryption key was rotated. Either way: no valid token,
		# which fails closed in security.py.
		return ""
	return (token or "").strip()


def allowed_cidrs() -> list[str]:
	"""The network allowlist as a list of CIDR strings, comments stripped.

	An empty list means "no allowlist configured", which `security.py` treats
	as deny-all rather than allow-all.
	"""
	raw = _value("allowed_cidrs")
	if raw is None:
		raw = DEFAULT_CIDRS
	out = []
	for chunk in str(raw).replace("\n", ",").split(","):
		entry = chunk.split("#", 1)[0].strip()
		if entry:
			out.append(entry)
	return out


def tool_enabled(tool_name: str) -> bool:
	"""Whether the `allow_<tool_name>` switch is on for this tool.

	Read tools ship with the switch on, mutating tools with it off; both are
	honoured identically here, so an operator can disable a read tool for the
	same reason they can enable a write one.
	"""
	return as_bool(_value(f"allow_{tool_name}"))


def public_url() -> str:
	"""An externally reachable base URL for this site, when one is configured.

	`frappe.utils.get_url()` is correct for the server and can be useless to a
	client: a site behind a Tailscale Funnel, a tunnel or a reverse proxy on
	another hostname has no way of knowing its public name from inside a request.
	So this is a field an operator fills in, and the connection panel prefers it.
	"""
	return str(_value("public_url") or "").strip()


def farm_ops_mobile_enabled() -> bool:
	"""The kill switch for the whole Farm Ops mobile HTTP surface.

	Separate from `enabled`, which is the MCP endpoint's master switch, because
	the two answer different questions and an operator needs to be able to answer
	them differently: "stop the AI" and "stop the phones" are not the same
	decision, and a release that made them the same control would guarantee that
	shutting one off took the other with it.

	Ships ON — v0.17.1 exists to make the phones work, and a switch that had to
	be found and flipped before anything worked would be a support call rather
	than a safeguard. `api/guard.mobile_enabled` reads this AND `site_config.json`
	and treats either one saying off as off.
	"""
	return as_bool(_value("farm_ops_mobile_enabled"))


def mobile_grant_idle_days() -> int:
	"""Days a mobile credential may go unused before the nightly sweep revokes it.

	0 switches the sweep off, and that is a real configuration rather than a
	broken one: a site whose crew works one month in twelve is better served by
	the operator revoking by hand than by a timer that fires every February.

	Ships at 30. `tools/mobile.sweep_idle_grants` reads it on every run rather
	than caching it, so shortening the window takes effect that night.
	"""
	try:
		return max(0, int(_value("mobile_grant_idle_days") or 0))
	except (TypeError, ValueError):
		# An unparseable value must not be read as "0 = off", which would
		# silently disable a security control. Fall back to the shipped default.
		return 30


def drift_report_email() -> str:
	"""Who hears when the weekly JE drift watch finds something. May be empty.

	Empty is a working configuration, not an unconfigured one: `drift.recipients`
	falls back to the site's enabled System Managers, who are the people able to
	act on a ledger finding. The field exists for the case where that is the wrong
	list — an accountant who has no login, or an operator who wants it in a
	ticket queue rather than an inbox.
	"""
	return str(_value("drift_report_email") or "").strip()


def kpi_history_sweep_enabled() -> bool:
	"""Whether the overnight Financial KPI History sweep runs at all. v0.19.6.

	Ships ON, because the sweep is what makes a windowed report answer in a
	second rather than a minute — a five-year Monthly history is sixty full
	computations over twelve months of GL each, and the whole point of computing
	them at two in the morning is that nobody is waiting.

	It is a switch rather than a fact because it is the only scheduled job in
	this app that does arbitrary amounts of arithmetic over a site's whole
	ledger. An operator with a very large book, a shared bench, or a night window
	they need for something else has a reason to turn it off — and the cost of
	doing so is bounded and visible: reports still answer, they answer from a
	cold cache, and they say in `computation_warnings` how much history they had
	to leave out.

	Turning it off does NOT stop `recompute_kpi_history`, which is somebody
	asking on purpose, in the moment, with the result in front of them.
	"""
	return as_bool(_value("enable_kpi_history_sweep"))


def require_user_context() -> bool:
	return as_bool(_value("require_user_context"))


def asset_mirror_enabled() -> bool:
	"""Whether registering a field asset ALSO writes an ERPNext Asset. Ships OFF.

	v0.148.0. What this gates is this app inserting into a doctype ERPNext owns,
	on a trigger a worker with a handset pulls — the fixed-asset register, which
	the depreciation run, `export_insurance_schedule` and Sustainable CF/Acre are
	all computed from. Every other mutating capability in this app ships off and
	this is a stronger case than most of them.

	Turning it off is not a loss of data. `Asset Register` is the operational
	record either way — the tag, the QR, the scan history and eight doctypes'
	link fields all point there — and the mirror is a second copy of the machine
	written where an accountant, an adjuster and a lender look for it. A site
	that leaves this off has a complete tag register and an untouched book.
	"""
	return as_bool(_value("mirror_assets_to_erpnext"))


def trade_document_enforcement() -> bool:
	"""Whether an incomplete document checklist HOLDS a shipment. Ships OFF.

	Advisory by default, and the default is the load-bearing part. A two-truck
	operation locked out of its own delivery by a phytosanitary certificate it
	will never need turns this module off within a week — and an operation that
	has turned the module off gets no warnings either, which is strictly worse
	than an advisory one that reports the gaps and lets the truck go. An
	operation big enough to want the gate turns it on here, or per shipment on
	the shipment's own `enforcement` field.
	"""
	return as_bool(_value("trade_document_enforcement"))


def effective_user() -> str:
	"""The Frappe user mutations run as, and are attributed to in `owner`.

	With `require_user_context` on (the default) this is the configured MCP
	System User, so every document the AI touches is traceable to a principal
	that is not a human and whose Role Profile the operator controls. With it
	off — or with no user chosen — it falls back to Administrator, which works
	but makes MCP-authored documents indistinguishable from an admin's own.
	See docs/security.md.
	"""
	if require_user_context():
		user = (_value("mcp_system_user") or "").strip()
		if user and frappe.db.exists("User", user):
			return user
	return FALLBACK_USER


def seed_defaults(doctype: str = SETTINGS_DOCTYPE) -> None:
	"""Write every unset field's declared default into `tabSingles`.

	Idempotent, and safe to run on every migrate: it only fills in fields that
	have no stored value, so an operator's choices are never overwritten. This
	is what makes "read tools default ON" true in the database rather than only
	in the DocType JSON.

	TAKES A DOCTYPE FROM v0.19.4, because there are now two Singles that need it.
	`Weather Settings` has the same problem for the same reason — a fresh install
	stores no rows, so `http_timeout_seconds` reads None and becomes a timeout of
	zero one `int()` later, which fails every connection immediately and silently.
	The default is a parameter rather than a second copy of this function, so the
	one place that knows how a Frappe Single hides its defaults stays one place.
	"""
	doc = frappe.get_doc(doctype)
	present = set(stored_fieldnames(doctype))
	changed = False
	for field in frappe.get_meta(doctype).fields:
		if field.fieldtype in ("Section Break", "Column Break", "HTML", "Tab Break", "Table"):
			continue
		if field.fieldname in present or field.default in (None, ""):
			continue
		doc.set(field.fieldname, field.default)
		changed = True
	if changed:
		doc.flags.ignore_permissions = True
		doc.save()


def stored_fieldnames(doctype: str = SETTINGS_DOCTYPE) -> set:
	"""Which of this Single's fields actually have a row in `tabSingles`.

	NEVER query `tabSingles` with `frappe.db.get_value` / `get_values` and no
	explicit `order_by`. Both default to ordering by `modified`, and `tabSingles`
	has three columns — `doctype`, `field`, `value` — so the query dies with
	`Unknown column 'modified' in 'ORDER BY'`. It is not a DocType table and does
	not get the framework columns. That is exactly how v0.2.0 shipped an
	`after_migrate` hook that crashed `bench migrate` on a real site: the default
	argument is invisible at the call site, and the standalone test double was
	happy to answer a query MariaDB refuses.

	`frappe.db.get_singles_dict` is the framework's own accessor for this table.
	It issues its own `SELECT field, value FROM tabSingles WHERE doctype = %s`
	with no ORDER BY at all, so there is no default to get wrong — which is the
	real reason to prefer it over passing `order_by=None` to the generic helper.
	"""
	return set(frappe.db.get_singles_dict(doctype) or {})


def as_bool(value) -> bool:
	"""Frappe-safe truthiness for a Check field. See the module docstring.

	Public so the settings controller can use it: a form that read `"0"` as True
	would refuse to save a perfectly valid disabled configuration.

	The cases that matter are the string forms: `"0"` must be False even though
	Python says a non-empty string is truthy.
	"""
	if value is None or isinstance(value, bool):
		return bool(value)
	if isinstance(value, (int, float)):
		return bool(value)
	return str(value).strip().lower() not in ("", "0", "false", "no", "none")


def _value(fieldname: str):
	"""One settings value, falling back to the DocType's declared default.

	Returns None only when the field neither has a stored value nor a default —
	i.e. genuinely unconfigured, such as `auth_token` before the operator has
	pressed Generate.
	"""
	try:
		doc = get_settings()
	except Exception:
		# The doctype isn't migrated yet (a request landing mid-`bench migrate`).
		# Fail closed: every gate reads falsy.
		return None
	if doc.get(fieldname) is not None:
		return doc.get(fieldname)
	return _meta_default(fieldname)


def _meta_default(fieldname: str):
	try:
		field = frappe.get_meta(SETTINGS_DOCTYPE).get_field(fieldname)
	except Exception:
		return None
	if field is None or field.default in (None, ""):
		return None
	if field.fieldtype == "Check":
		return int(field.default or 0)
	return field.default
