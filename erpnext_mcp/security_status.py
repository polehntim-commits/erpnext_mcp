# SPDX-License-Identifier: MIT
"""One scored checklist of this site's security posture. v0.217.0.

docs/design/security_status_and_alerts.md §2. Read-only, and NOTHING SECRET
LEAVES: users with an API key are named, the key is not; the MCP token is
reported as configured or not.
"""

from __future__ import annotations

import frappe

from . import compat, security, security_alerts, settings, switch_catalog

PASS, WARN, FAIL, INFO = "pass", "warn", "fail", "info"

#: Doctypes whose MCP-updatable fields are worth naming: people and money.
SENSITIVE_DOCTYPES = frozenset(
	{
		"Employee",
		"Employee Bank Account",
		"Bank Account",
		"User",
		"Salary Structure",
		"Salary Structure Assignment",
		"Payment Entry",
		"Journal Entry",
		"Purchase Invoice",
		"Sales Invoice",
		"Supplier",
		"Customer",
		"Garnishment",
		"I-9 Form",
		"W-4 Form",
	}
)

#: frappe/frappe#42852: the Administrator 2FA lockout regression. Versions from
#: here up are warned about while 2FA is on, until a fixed release is known.
REGRESSION_FROM = (15, 118, 0)

FAILED_LOGIN_LIMIT = 5


def _shift(**delta) -> str:
	"""Now, moved by `delta` (days/hours/minutes), as 'YYYY-MM-DD HH:MM:SS'."""
	return str(frappe.utils.add_to_date(frappe.utils.now(), **delta))[:19]


def _check(key, status, finding, fix="", weight=1) -> dict:
	return {"key": key, "status": status, "finding": finding, "fix": fix or None, "weight": weight}


def _single(doctype: str, field: str):
	try:
		if not compat.has_field(doctype, field):
			return None
		return frappe.db.get_single_value(doctype, field)
	except Exception:
		return None


def _int(value, default=0) -> int:
	try:
		return int(value)
	except (TypeError, ValueError):
		return default


def _version(text: str) -> tuple:
	parts = []
	for piece in str(text or "").split(".")[:3]:
		digits = "".join(ch for ch in piece if ch.isdigit())
		parts.append(int(digits) if digits else 0)
	while len(parts) < 3:
		parts.append(0)
	return tuple(parts)


def checks(probe_public: bool = False) -> list:
	out = []
	sys = "System Settings"

	two_factor = _single(sys, "enable_two_factor_auth")
	if two_factor is None:
		out.append(_check("two_factor", INFO, "This site's System Settings has no two-factor field."))
	elif _int(two_factor):
		out.append(
			_check(
				"two_factor",
				PASS,
				f"Two-factor auth is on ({_single(sys, 'two_factor_method') or 'OTP App'}).",
				weight=3,
			)
		)
	else:
		out.append(
			_check(
				"two_factor",
				FAIL,
				"Two-factor auth is off for Desk logins.",
				"System Settings → Enable Two Factor Auth (OTP App). Mind frappe_version below first.",
				weight=3,
			)
		)

	policy = _single(sys, "enable_password_policy")
	score = _int(_single(sys, "minimum_password_score"))
	if policy is not None:
		ok = _int(policy) and score >= 3
		out.append(
			_check(
				"password_policy",
				PASS if ok else FAIL,
				f"Password policy {'on' if _int(policy) else 'off'}, minimum score {score}.",
				"" if ok else "System Settings → Enable Password Policy, Minimum Password Score 3 or 4.",
				weight=2,
			)
		)

	attempts = _single(sys, "allow_consecutive_login_attempts")
	if attempts is not None:
		ok = 0 < _int(attempts) <= 10
		out.append(
			_check(
				"login_attempts",
				PASS if ok else WARN,
				f"Consecutive failed logins allowed before a lockout: {_int(attempts) or 'unlimited'}.",
				"" if ok else "System Settings → Allow Consecutive Login Attempts: 5.",
			)
		)

	link_login = _single(sys, "login_with_email_link")
	if link_login is not None:
		out.append(
			_check(
				"email_link_login",
				WARN if _int(link_login) else PASS,
				f"Login with email link is {'on' if _int(link_login) else 'off'}.",
				"System Settings → untick Login with email link (it bypasses 2FA)."
				if _int(link_login)
				else "",
			)
		)

	expiry = str(_single(sys, "session_expiry") or "")
	if expiry:
		hours = _int(expiry.split(":", 1)[0])
		out.append(
			_check(
				"session_expiry",
				PASS if 0 < hours <= 24 else WARN,
				f"Desk sessions expire after {expiry}.",
				"" if 0 < hours <= 24 else "System Settings → Session Expiry: 12:00 or less.",
			)
		)

	user = str(settings._value("mcp_system_user") or "")
	if not user or user == "Administrator":
		out.append(
			_check(
				"mcp_system_user",
				FAIL,
				f"MCP tools run as {user or 'Administrator (no MCP System User set)'}, which passes every permission check.",
				"ERPNext MCP Settings → MCP System User: a dedicated user with only the roles the AI needs.",
				weight=3,
			)
		)
	else:
		roles = sorted(frappe.get_roles(user) or [])
		out.append(
			_check(
				"mcp_system_user",
				WARN if "System Manager" in roles else PASS,
				f"MCP tools run as {user} ({', '.join(roles) or 'no roles'}).",
				"Take System Manager off the MCP System User." if "System Manager" in roles else "",
				weight=3,
			)
		)

	rows = switch_catalog.catalogue()
	dangerous = switch_catalog.summary(rows)["dangerous_enabled"]
	out.append(
		_check(
			"mcp_switches",
			WARN if dangerous else PASS,
			f"{len(dangerous)} dangerous tool switch(es) on"
			+ (f": {', '.join(dangerous)}" if dangerous else "."),
			"Turn them off when not in use, or use 'Enable a tool for N minutes' on ERPNext MCP Settings."
			if dangerous
			else "",
			weight=2,
		)
	)

	out.append(_static_token_check())
	out.append(_oauth_user_check())

	try:
		holders = frappe.db.get_all(
			"User", filters={"api_key": ["is", "set"], "enabled": 1}, pluck="name", limit=200
		)
	except Exception:
		holders = []
	out.append(
		_check(
			"api_keys",
			WARN if len(holders) > 3 else INFO,
			f"{len(holders)} enabled user(s) hold a Frappe API key"
			+ (f": {', '.join(sorted(holders))}" if holders else "."),
			"Each Frappe API key opens /api/resource as that user; revoke the ones nothing uses."
			if holders
			else "",
		)
	)

	since = str(_shift(days=-30))[:19]
	admin = _logins({"user": "Administrator", "status": "Success", "creation": [">=", since]})
	out.append(
		_check(
			"administrator_logins",
			WARN if admin else PASS,
			f"Administrator logged in {admin} time(s) in the last 30 days.",
			"Use named System Manager accounts; keep Administrator for recovery." if admin else "",
		)
	)

	week = str(_shift(days=-7))[:19]
	failed = _failed_bursts(week)
	out.append(
		_check(
			"failed_logins",
			WARN if failed else PASS,
			f"{len(failed)} user(s) had more than {FAILED_LOGIN_LIMIT} failed logins in one hour this week"
			+ (f": {', '.join(failed)}" if failed else "."),
		)
	)

	sensitive = []
	try:
		for row in settings.get_settings().get("update_document_fields") or []:
			doctype = str(row.get("doctype_name") or "")
			if settings.as_bool(row.get("enabled")) and doctype in SENSITIVE_DOCTYPES:
				sensitive.append(f"{doctype}.{row.get('field_name')}")
	except Exception:
		pass
	out.append(
		_check(
			"updatable_fields",
			WARN if sensitive else PASS,
			f"{len(sensitive)} MCP-updatable field(s) on people or money"
			+ (f": {', '.join(sorted(sensitive))}" if sensitive else "."),
		)
	)

	recipients = settings.security_alert_email()
	out.append(
		_check(
			"security_alert_recipients",
			PASS if recipients else INFO,
			f"Security alerts go to {recipients}."
			if recipients
			else "Security alerts go to the System Managers (no recipients set).",
			""
			if recipients
			else "ERPNext MCP Settings → Security Alert Recipients, if that is the wrong list.",
		)
	)

	out.append(_client_ip_check())
	out.append(_log_retention())
	out.append(_emailed_links())
	out.append(_frappe_version(two_factor))

	if probe_public:
		out.append(_public_surface())
	return out


def _logins(filters: dict) -> int:
	if not compat.doctype_exists("Activity Log"):
		return 0
	try:
		return len(
			frappe.db.get_all(
				"Activity Log", filters={"operation": "Login", **filters}, pluck="name", limit=1000
			)
		)
	except Exception:
		return 0


def _failed_bursts(since: str) -> list:
	if not compat.doctype_exists("Activity Log"):
		return []
	try:
		rows = frappe.db.get_all(
			"Activity Log",
			filters={"operation": "Login", "status": "Failed", "creation": [">=", since]},
			fields=["user", "creation"],
			limit=5000,
		)
	except Exception:
		return []
	buckets: dict = {}
	for row in rows:
		key = (str(row.get("user") or ""), str(row.get("creation") or "")[:13])
		buckets[key] = buckets.get(key, 0) + 1
	return sorted({user for (user, _hour), count in buckets.items() if count > FAILED_LOGIN_LIMIT and user})


def _client_ip_check() -> dict:
	trusted = settings.trusted_proxy_cidrs()
	try:
		request = getattr(frappe.local, "request", None)
		chain = str(frappe.get_request_header("X-Forwarded-For") or "") if request else ""
		peer = getattr(request, "remote_addr", "") if request else ""
	except Exception:
		chain, peer = "", ""
	now = security.caller_ip()
	return _check(
		"client_ip",
		PASS if trusted else INFO,
		(
			f"Trusted proxy ranges: {', '.join(trusted)}. "
			if trusted
			else "No trusted proxy range set: the rightmost forwarded hop is the client. "
		)
		+ f"This request resolved to {now or 'nothing'} (X-Forwarded-For {chain or 'none'}, peer {peer or 'none'}).",
		""
		if trusted
		else "See docs/design/security_status_and_alerts.md §4 before setting Trusted Proxy Ranges.",
	)


#: v0.223.1. The two logs an investigation reads, and the least they should keep.
#: Error Log is reported but not judged: 30 days of tracebacks is a normal choice.
RETAINED_LOGS = ("Activity Log", "Access Log")
RETENTION_MIN_DAYS = 90
LOG_SETTINGS_FIX = (
	"Desk → Log Settings (/app/log-settings) → the Logs to Clear table. Set 'Clear Logs After (days)' to at "
	"least 90 on the {names} row. Access Log has NO row by default, which means it is never cleared; it "
	"only needs a row (Add Row → Log DocType = Access Log) if you want a limit, and then at least 90."
)


def _log_retention() -> dict:
	"""Activity and Access Log kept at least 90 days. FOREVER PASSES. v0.223.1.

	In Log Settings' Logs to Clear table, a doctype with no row is never
	cleared, and a row with 0 days is not a limit either — both are "forever",
	which satisfies "at least 90". Only a positive number under 90 on one of
	RETAINED_LOGS warns. Error Log is shown, never judged (v0.217.0 judged it,
	so OML's 30-day Error Log warned while the fix text named the other two).
	"""
	if not compat.doctype_exists("Log Settings"):
		return _check("log_retention", INFO, "This site has no Log Settings.")
	try:
		doc = frappe.get_single("Log Settings")
		rows = {
			str(row.get("ref_doctype")): _int(row.get("days")) for row in (doc.get("logs_to_clear") or [])
		}
	except Exception:
		rows = {}

	def kept(name: str) -> str:
		days = rows.get(name)
		if not days:
			return f"{name} forever" + ("" if name in rows else " (no row)")
		return f"{name} {days} days"

	short = [name for name in RETAINED_LOGS if 0 < (rows.get(name) or 0) < RETENTION_MIN_DAYS]
	return _check(
		"log_retention",
		WARN if short else PASS,
		"Kept: " + ", ".join(kept(name) for name in ("Error Log", *RETAINED_LOGS)) + ".",
		LOG_SETTINGS_FIX.format(names=" and ".join(short)) if short else "",
	)


def _emailed_links() -> dict:
	"""v0.223.3. A password-reset link nobody can open is a lockout. See `site_url`."""
	from . import site_url

	found = site_url.status()
	if "error" in found:
		return _check("emailed_links", INFO, f"Could not read the site's link address: {found['error']}.")
	if found["problems"]:
		return _check(
			"emailed_links",
			FAIL,
			"Emailed links (password reset, 2FA QR, notifications) cannot be opened: "
			+ "; ".join(found["problems"])
			+ ".",
			f"Set the site's external Desk URL once: {found['fix']} (docs/deploy/v0.223.3_emailed_links.md).",
			weight=2,
		)
	if found["warnings"]:
		return _check(
			"emailed_links",
			WARN,
			f"Emailed links use {found['host_name']}: " + "; ".join(found["warnings"]) + ".",
			f"If the Desk is elsewhere: {found['fix']}",
		)
	return _check("emailed_links", PASS, f"Emailed links use {found['host_name']}.")


def _frappe_version(two_factor) -> dict:
	version = str(getattr(frappe, "__version__", "") or "")
	risky = _version(version) >= REGRESSION_FROM and _int(two_factor)
	return _check(
		"frappe_version",
		WARN if risky else INFO,
		f"Frappe {version or 'unknown'}"
		+ (
			" — in the range of the Administrator 2FA lockout regression (frappe/frappe#42852)."
			if risky
			else "."
		),
		"Do not enable 2FA for Administrator on this version; keep a second System Manager." if risky else "",
	)


def _public_surface() -> dict:
	from .tools import funnel

	base = settings.farmops_public_url()
	if not base:
		return _check("public_surface", INFO, "No Farm Ops Public URL; nothing to probe.")
	bad = []
	for path in ("/erpnext/login", "/erpnext/api/method/ping", "/", "/bankbridge"):
		answer = funnel._get(f"{base}{path}", 8)
		kind = str(answer.get("content_type") or "").lower()
		if answer.get("status") is not None and not (answer.get("status") == 404 and "html" not in kind):
			bad.append(f"{path} answered {answer.get('status')}")
	probe = funnel._probe_route(f"{base}/farmops/api/mobile/scan_asset", 8)
	if not probe.get("published"):
		bad.append("/farmops/api/mobile/scan_asset did not answer 401 JSON")
	return _check(
		"public_surface",
		FAIL if bad else PASS,
		"Public surface: " + ("; ".join(bad) if bad else "only /farmops answers, and it wants a credential."),
		"See the cutover runbook (docs/deploy/v0.216.0_cutover_paste_ready.md)." if bad else "",
		weight=3,
	)


def report(probe_public: bool = False) -> dict:
	rows = checks(probe_public)
	scored = [row for row in rows if row["status"] in (PASS, WARN, FAIL)]
	total = sum(row["weight"] for row in scored) or 1
	earned = sum(
		row["weight"] * (1 if row["status"] == PASS else 0.5 if row["status"] == WARN else 0)
		for row in scored
	)
	score = round(100 * earned / total)
	fails = [row["key"] for row in rows if row["status"] == FAIL]
	warns = [row["key"] for row in rows if row["status"] == WARN]
	return {
		"score": score,
		"checks": rows,
		"fails": fails,
		"warnings": warns,
		"alert_recipients": security_alerts.recipients(),
		"summary": f"Security score {score}/100"
		+ (f"; failing: {', '.join(fails)}" if fails else "")
		+ (f"; warnings: {', '.join(warns)}" if warns else ""),
	}


def _static_token_check() -> dict:
	"""v0.220.0: informational until OAuth is on; then a warning while both doors are open."""
	from . import oauth

	configured = bool(settings.auth_token())
	if not oauth.enabled():
		return _check(
			"static_mcp_token",
			INFO,
			"A static MCP token is configured (OAuth for MCP clients is off)."
			if configured
			else "No MCP token is configured.",
		)
	if not configured or not oauth.legacy_static_allowed():
		return _check("static_mcp_token", PASS, "OAuth only: the static MCP token is not accepted.")
	ready = oauth.static_token_status()
	return _check(
		"static_mcp_token",
		WARN if ready["ready_to_disable_static_mcp_token"] else INFO,
		"OAuth is on and the static MCP token still works"
		+ (" — and nothing has used it in 14 days." if ready["ready_to_disable_static_mcp_token"] else "."),
		"Untick 'Keep the static MCP token working' on ERPNext MCP Settings."
		if ready["ready_to_disable_static_mcp_token"]
		else "",
	)


def _oauth_user_check() -> dict:
	"""v0.220.0: who an OAuth client runs as, and whether that user is too strong."""
	from . import oauth

	if not oauth.enabled():
		return _check("mcp_oauth_user", INFO, "OAuth for MCP clients is off.")
	user = oauth.agent_user()
	roles = set(frappe.get_roles(user) or [])
	strong = user == "Administrator" or "System Manager" in roles
	return _check(
		"mcp_oauth_user",
		FAIL if strong else PASS,
		f"OAuth clients run as {user}" + (" — Administrator or a System Manager." if strong else "."),
		"Create a dedicated user (e.g. mcp-agent) with only the roles the tools need and choose it in "
		"'OAuth clients run as' on ERPNext MCP Settings."
		if strong
		else "",
		weight=2,
	)
