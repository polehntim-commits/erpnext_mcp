# SPDX-License-Identifier: MIT
"""Mobile accounts, their entity scoping, and the credential a phone carries.

v0.17.0. Sprint 8 built the dispatch board. This is what makes it safe to point
forty phones at from outside the LAN, and it is three things:

  1. **An account with a role and an entity list.** `create_mobile_user` does in
     one call what four Desk forms do in ten minutes: the User, the role, a User
     Permission per Company, the grant record, and the API credential.
  2. **A credential the phone keeps.** Frappe's own API key/secret pair, over
     HTTPS, in the Keychain. Not a session cookie — a worker in an orchard with
     one bar of signal cannot re-authenticate, and a token that survives a lost
     connection is the difference between an app and a demonstration.
  3. **A way to hand it over that a person can actually do.** A QR code. Typing
     a 15-character secret into a phone keyboard while standing in a farm office
     is how the secret gets written on a whiteboard instead.

────────────────────────────────────────────────────────────────────────────
THE ENTITY SCOPING, WHICH IS THE POINT OF THE WHOLE RELEASE
────────────────────────────────────────────────────────────────────────────

Several entities are live on one site: an operating company, a land-holding
company, a family office, trusts. A field worker at the operator must not see
the holding company's parcels; an advisor to the family office must not see the
operator's task board.

Frappe answers this with **User Permission**, and the answer is better than
anything this app could have built: one row saying `allow=Company,
for_value=<name>, apply_to_all_doctypes=1` restricts EVERY document that links
to a Company, across every doctype, for that user, in every list, report, API
call and Desk view — including doctypes this app has not written yet.

SO THE ROLE SAYS WHAT KIND OF WORK, AND THE USER PERMISSION SAYS WHOSE. See
`roles.py`, which argues that split at length and is why no company name appears
in any role definition.

**AN EMPTY User Permission LIST MEANS EVERY COMPANY.** That is Frappe's rule,
not this app's choice, and it is the one place where the safe-looking option is
the dangerous one: a mobile account created without entity_access would see the
entire site. `create_mobile_user` therefore REFUSES to make an account with no
entities, in a release whose entire purpose is scoping. There is no flag to
override it.

────────────────────────────────────────────────────────────────────────────
WHAT THIS MODULE DOES NOT PROMISE
────────────────────────────────────────────────────────────────────────────

**API secrets still do not expire, and v0.17.1 added a sweep anyway.** Frappe has
no expiry on an API secret, so `token_expires_on` remains a REVIEW DATE and
calling it an expiry would be a false assurance about a credential, which is
worse than none.

WHAT CHANGED, AND WHY THE OLD ARGUMENT LOST. v0.17.0 said here that this app
would install no scheduled job to revoke one, "because such a job would rewrite
another app's User records on a timer with nobody watching". Every clause of that
turned out to be answerable, and v0.17.1 answers them: `sweep_idle_grants`
rewrites only the two credential fields, only on accounts this app created and
minted a key for, only where a Mobile Access Grant says so — and somebody IS
watching, because each revocation writes an MCP Action Log row and the run emails
a summary.

What actually changed is the threat. A key that only ever travelled to a laptop
on the LAN could be left to a human to review. v0.17.1 put forty of them in forty
pockets on the open internet, and a phone left on a truck seat and not mentioned
is the ordinary case rather than the exotic one. A credential that stops working
by itself is the only control that does not depend on somebody admitting to it.

The sweep works on IDLENESS (`last_seen_on`, stamped by `api/guard` at most once
a day) rather than on the review date: a credential in daily use is not stale
because a date passed, and one nobody has touched for a month is stale whatever
the date says. It revokes the TOKEN and never the account — the worker keeps
their roles and entity access and needs one new QR. `revoke_mobile_user` is the
other thing, and no timer ever reaches it.

**The QR carries a live credential.** Anybody who photographs it over somebody's
shoulder has the account. That is inherent to enrolment-by-QR and the mitigation
is time: the payload carries `expires_at`, minted for the length of an
onboarding conversation rather than the length of a season, and re-minting
rotates the secret by default so the photograph stops working. The tool is
kill-switched off, its result says all of this in `security_note`, and the
archived copy is a PRIVATE file.

**Nothing here weakens the transport gates.** A mobile request still presents
the shared `X-MCP-Token` and still comes from an allowed CIDR. The per-user
credential is carried ALONGSIDE it in `Authorization: token <key>:<secret>`, and
what it buys is identity, not entry. `security.capture_calling_user` is where
that identity is caught, in the one-line window before this app assumes the MCP
System User.
"""

from __future__ import annotations

import base64
import json

import frappe

from .. import audit, compat, roles, security, settings
from ..args import as_bool, as_int, as_str, resolve_company
from ..compat import doctype_exists
from ..errors import ToolError
from ..render import qr
from ..result import ToolResult
from . import artifacts

GRANT = "Mobile Access Grant"
USER = "User"
USER_PERMISSION = "User Permission"
EMPLOYEE = "Employee"

#: How long an API token goes before somebody should look at it again. A season
#: — long enough that re-issuing is not a weekly chore, short enough that a
#: worker who left in July is not still holding a live credential at Christmas.
DEFAULT_TOKEN_REVIEW_DAYS = 120

#: How long a login QR stays valid to enrol with. The spec asked for 24 hours
#: and 24 hours is right: it is the length of an onboarding conversation plus a
#: night, and a QR that is valid for a week is a QR sitting on a desk for a week.
DEFAULT_QR_HOURS = 24

#: Frappe's own API secret length. Matching it rather than inventing a longer
#: one, because the value has to survive Frappe's own validation and storage.
SECRET_LENGTH = 15

#: The most accounts one `list_mobile_users` call reports. A number nobody's
#: crew reaches, which is the point — a cap that truncates is a cap that lies.
LIST_CAP = 500


# ── shared helpers ──────────────────────────────────────────────────────────
def _require_grant() -> None:
	if not doctype_exists(GRANT):
		raise ToolError(
			f"this site has no {GRANT} doctype. It ships with erpnext_mcp — run "
			"`bench --site <site> migrate` after upgrading the app."
		)


def _user_row(user: str) -> dict:
	user = (user or "").strip()
	if not user:
		raise ToolError("user is required (the email address the account signs in with).")
	if not frappe.db.exists(USER, user):
		raise ToolError(
			f"no User {user!r} on this site. list_mobile_users has every account this app made. "
			"Nothing was changed."
		)
	fields = ["name", "enabled", "full_name", "user_type", "api_key"]
	return dict(frappe.db.get_value(USER, user, fields, as_dict=True) or {})


def _grant_row(user: str) -> dict:
	if not doctype_exists(GRANT) or not frappe.db.exists(GRANT, user):
		return {}
	return dict(frappe.get_doc(GRANT, user).as_dict())


def _is_a_company(name: str) -> bool:
	"""Whether `name` resolves to a Company here — docname or abbreviation.

	`resolve_company` RAISES on an unknown non-empty name; `required=False` only
	governs what it does with an EMPTY one, which is a distinction worth writing
	down because `_resolve_entities` below was written as though the falsy return
	were the unknown case and has a branch that has therefore never run. Used as
	the split predicate, a raise would abort the parse on the first candidate
	that is not a company — and the whole method is to TRY candidates — so it is
	caught here and answered as the no it means.
	"""
	try:
		return bool(resolve_company(name, required=False))
	except ToolError:
		return False


def _resolve_entities(args: dict, key: str = "entity_access") -> list:
	"""The Companies an account may see, resolved from names or abbreviations.

	Refuses an empty list. See the module docstring: an account with no User
	Permission on Company sees EVERY company, which is the exact opposite of
	what somebody asking for entity scoping wants, and there is no flag here to
	produce one.
	"""
	raw = args.get(key)
	if raw is None:
		wanted = []
	elif isinstance(raw, (str, list, tuple)):
		# A COMMA IS PART OF "ORCHARD MEADOW, LLC" UNTIL THE REGISTER SAYS
		# OTHERWISE. This used to be `raw.replace("\n", ",").split(",")`, which
		# split every LLC on the site into a name and a suffix — neither of which
		# resolves, so the refusal below named a company the caller had spelled
		# correctly. `split_entity_names` tries the comma against
		# `resolve_company` (abbreviations and all) and undoes the split where
		# the pieces are not entities. See `roles.py`.
		wanted = roles.split_entity_names(raw, _is_a_company)
	else:
		raise ToolError(f"{key} must be a list of Company names (or a comma-separated string).")

	if not wanted:
		raise ToolError(
			f"{key} is required and must name at least one Company. In Frappe a user with NO "
			"User Permission on Company sees EVERY company on the site — so an account created "
			"without entities would be the least scoped account here, not the most. "
			"get_company_topology lists what this site has."
		)

	resolved = []
	for entry in wanted:
		name = resolve_company(entry, required=False)
		if not name:
			raise ToolError(
				f"{entry!r} is not a Company on this site (nor an abbreviation of one). "
				"get_company_topology lists them. Nothing was changed."
			)
		if name not in resolved:
			resolved.append(name)
	return resolved


def _preferred(args: dict, entities: list) -> str:
	"""Which entity the app opens on. Defaults to the first one named."""
	wanted = as_str(args, "preferred_company")
	if not wanted:
		return entities[0]
	name = resolve_company(wanted, required=False)
	if not name:
		raise ToolError(f"preferred_company {wanted!r} is not a Company on this site.")
	if name not in entities:
		raise ToolError(
			f"preferred_company {name!r} is not in entity_access ({', '.join(entities)}). The "
			"entity the app opens on has to be one the account may actually see."
		)
	return name


def _role_spec(args: dict) -> roles.RoleSpec:
	role = as_str(args, "role", required=True)
	spec = roles.spec_for(role)
	if spec is None:
		raise ToolError(
			f"{role!r} is not one of this app's mobile roles. The {len(roles.ROLE_NAMES)} are: "
			f"{', '.join(roles.ROLE_NAMES)}. list_mobile_users returns what each one is for, "
			"and the job-title mapping — which mobile role a Checker, a Tractor Driver or a "
			"Crew Leader gets — beside it. A job title is a Designation on the Employee, not "
			"a role."
		)
	return spec


def _employee_for(user: str) -> str:
	"""The Employee record linked to a login, where the site has one."""
	if not doctype_exists(EMPLOYEE):
		return ""
	try:
		return str(frappe.db.get_value(EMPLOYEE, {"user_id": user}, "name") or "")
	except Exception:  # pragma: no cover - an Employee table without user_id
		return ""


def _assign_role(user: str, role: str) -> bool:
	"""Add one role to a user. True if it was added, False if already held."""
	doc = frappe.get_doc(USER, user)
	if any(str(row.get("role")) == role for row in (doc.get("roles") or [])):
		return False
	doc.append("roles", {"role": role})
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return True


def _sync_user_permissions(user: str, entities: list, preferred: str) -> dict:
	"""Make the user's Company User Permissions match `entities`. Additive-first.

	ADDS what is missing and REMOVES what is no longer granted, because a stale
	permission is the failure this release exists to prevent: an account moved
	from the operator to the holding company that still carries the operator's
	entity can still read the operator's task board, and nothing anywhere would
	say so.

	It touches ONLY `allow=Company` rows. A User Permission somebody added on
	Cost Center, Warehouse or Project is a decision this app did not make and
	does not get to undo.
	"""
	report = {"added": [], "removed": [], "kept": [], "default": preferred}
	existing = (
		frappe.db.get_all(
			USER_PERMISSION,
			filters={"user": user, "allow": "Company"},
			fields=["name", "for_value", "is_default"],
			limit=LIST_CAP,
		)
		or []
	)
	by_value = {str(row["for_value"]): row for row in existing}

	for company in entities:
		row = by_value.get(company)
		if row is None:
			doc = frappe.get_doc(
				{
					"doctype": USER_PERMISSION,
					"user": user,
					"allow": "Company",
					"for_value": company,
					"apply_to_all_doctypes": 1,
					"is_default": 1 if company == preferred else 0,
				}
			)
			doc.flags.ignore_permissions = True
			doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
			report["added"].append(company)
		else:
			report["kept"].append(company)
			wanted_default = 1 if company == preferred else 0
			if int(row.get("is_default") or 0) != wanted_default:
				frappe.db.set_value(USER_PERMISSION, row["name"], "is_default", wanted_default)

	for value, row in by_value.items():
		if value not in entities:
			frappe.delete_doc(USER_PERMISSION, row["name"], force=True, ignore_permissions=True)
			report["removed"].append(value)
	return report


def _write_grant(user: str, values: dict) -> str:
	"""Create or update the one Mobile Access Grant for a user."""
	_require_grant()
	if frappe.db.exists(GRANT, user):
		doc = frappe.get_doc(GRANT, user)
	else:
		doc = frappe.new_doc(GRANT)
		doc.user = user
	for key, value in values.items():
		doc.set(key, value)
	doc.flags.ignore_permissions = True
	if doc.get("name") and frappe.db.exists(GRANT, user):
		doc.save(ignore_permissions=True)
	else:
		doc.insert(ignore_permissions=True)
	return doc.name


def _issue_token(user: str) -> dict:
	"""Mint a fresh API key/secret pair on a User. Returns the plaintext ONCE.

	`api_key` is reused where one exists — it is the public half, it appears in
	access logs, and rotating it would orphan every log line that named it. The
	SECRET is always new, because the whole point of issuing a token is that the
	previous one stops working.
	"""
	doc = frappe.get_doc(USER, user)
	api_key = str(doc.get("api_key") or "").strip() or frappe.generate_hash(length=SECRET_LENGTH)
	secret = frappe.generate_hash(length=SECRET_LENGTH)
	doc.api_key = api_key
	doc.api_secret = secret
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return {"api_key": api_key, "api_secret": secret}


def read_api_secret(user: str) -> str:
	"""The stored API secret in plaintext, or "".

	Used in exactly one place — `generate_mobile_login_qr` with
	`rotate_token=false`, for re-printing a card without breaking the phone that
	already has it — and by the tests that prove a revocation actually revoked.
	Frappe stores this encrypted at rest; this is the framework's own accessor
	and not a way round it.
	"""
	try:
		return str(frappe.get_doc(USER, user).get_password("api_secret", raise_exception=False) or "")
	except Exception:
		return ""


def _clear_token(user: str) -> bool:
	"""Invalidate the credential. True if there was one to invalidate.

	BOTH HALVES GO. Clearing only the secret leaves an api_key on the row that
	reads like a live credential to anybody scanning the User list, and the
	whole value of a revocation record is that somebody can tell at a glance
	that it happened.
	"""
	had = bool(read_api_secret(user)) or bool(frappe.db.get_value(USER, user, "api_key"))
	doc = frappe.get_doc(USER, user)
	doc.api_secret = ""
	doc.api_key = ""
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return had


def _endpoint_url(args: dict | None = None) -> str:
	"""The base URL a phone should call. The operator's public_url wins.

	`frappe.utils.get_url()` is correct for the server and useless to a phone
	outside the LAN: a site behind a Tailscale Funnel has no way of knowing its
	own public name from inside a request. So the settings field is the answer
	whenever it is filled in, and `get_url()` is the fallback that at least
	works on the LAN.
	"""
	explicit = as_str(args or {}, "url")
	if explicit:
		return explicit.rstrip("/")
	configured = settings.public_url()
	if configured:
		return configured.rstrip("/")
	try:
		return str(frappe.utils.get_url() or "").rstrip("/")
	except Exception:  # pragma: no cover
		return ""


def _now() -> str:
	return str(frappe.utils.now())


# ── 1. create_mobile_user ───────────────────────────────────────────────────
def create_mobile_user(args: dict) -> ToolResult:
	"""One call: the User, the role, the entity scoping, the grant, the token."""
	_require_grant()
	email = as_str(args, "email", required=True).strip().lower()
	if "@" not in email:
		raise ToolError(f"email {email!r} is not an email address, and a Frappe User is named by one.")

	spec = _role_spec(args)
	entities = _resolve_entities(args)
	preferred = _preferred(args, entities)
	full_name = as_str(args, "full_name")
	review_days = as_int(args, "token_expiry_days", DEFAULT_TOKEN_REVIEW_DAYS) or DEFAULT_TOKEN_REVIEW_DAYS

	existed = bool(frappe.db.exists(USER, email))
	# THE DEFAULT DEPENDS ON WHETHER THE ACCOUNT IS NEW, AND THAT IS DELIBERATE.
	# A new account with no credential cannot sign in, so issuing one is the only
	# useful default. An EXISTING account already has a phone in somebody's
	# pocket, and minting a fresh secret would silently knock it offline — so
	# changing somebody's entity access defaults to leaving their credential
	# alone. Either default can be overridden by passing `generate_token`
	# explicitly, which is the point of having the argument at all.
	with_token = as_bool(args, "generate_token", not existed)
	if existed and not as_bool(args, "update_existing", False):
		raise ToolError(
			f"User {email!r} already exists on this site. Re-running this on a live account "
			"would rewrite its roles and its entity scoping, which is a decision rather than a "
			"retry — pass update_existing=true to say so. To only re-issue a credential, use "
			"generate_api_token. Nothing was changed."
		)

	if not existed:
		if not full_name:
			raise ToolError(
				"full_name is required for a new account. A dispatch board and an evidence "
				"record both name the person; an account called 'jose@' names nobody."
			)
		first, _, last = full_name.partition(" ")
		doc = frappe.get_doc(
			{
				"doctype": USER,
				"email": email,
				"first_name": first or full_name,
				"last_name": last,
				"full_name": full_name,
				"enabled": 1,
				# A System User, because API access and role permissions are what
				# this account is for. Desk access is controlled on the ROLE — the
				# two phone-only roles ship with desk_access off, so a Field Worker
				# holds a System User account that cannot open /app.
				"user_type": "System User",
				# NOT sent. A welcome email to a crew address that does not exist
				# is a bounce, and enrolment here is the QR, not a password reset.
				"send_welcome_email": 0,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
	elif full_name:
		frappe.db.set_value(USER, email, "full_name", full_name)

	assigned = []
	if _assign_role(email, spec.name):
		assigned.append(spec.name)
	companions_missing = []
	for companion in spec.companion_roles:
		if frappe.db.exists("Role", companion):
			if _assign_role(email, companion):
				assigned.append(companion)
		else:
			companions_missing.append(companion)

	permissions = _sync_user_permissions(email, entities, preferred)

	token = {}
	if with_token:
		token = _issue_token(email)

	grant_values = {
		"full_name": full_name or str(frappe.db.get_value(USER, email, "full_name") or ""),
		"mobile_role": spec.name,
		"state": "Active",
		"preferred_company": preferred,
		"entity_access": "\n".join(entities),
		"notes": as_str(args, "notes"),
		"endpoint_url": _endpoint_url(args),
		"revocation_reason": "",
		"revoked_on": None,
		"revoked_by": None,
	}
	if token:
		grant_values.update(
			{
				"api_key": token["api_key"],
				"token_issued_on": _now(),
				"token_expires_on": frappe.utils.add_days(frappe.utils.today(), review_days),
				"token_revoked_on": None,
				"token_issue_count": int(_grant_row(email).get("token_issue_count") or 0) + 1,
			}
		)
	grant = _write_grant(email, grant_values)

	data = {
		"user": email,
		"created": not existed,
		"updated": existed,
		"full_name": grant_values["full_name"],
		"role": spec.name,
		"role_summary": spec.summary,
		"role_cannot": list(spec.cannot),
		"roles_assigned": assigned,
		"desk_access": bool(spec.desk_access),
		"entity_access": entities,
		"preferred_company": preferred,
		"user_permissions": permissions,
		"grant": grant,
		"employee": _employee_for(email) or None,
		"endpoint_url": grant_values["endpoint_url"] or None,
		"token_review_due": grant_values.get("token_expires_on"),
	}
	if token:
		data["api_key"] = token["api_key"]
		data["api_secret"] = token["api_secret"]
		data["auth_header"] = f"Authorization: token {token['api_key']}:{token['api_secret']}"
		# v0.17.2. THE SAME PAIR, UNDER A NAME NO PROXY HAS AN OPINION ABOUT. The
		# Tailscale serve/funnel step removes `Authorization`, so a phone sending
		# only the line above arrives as Guest and gets the Desk's `/me` page.
		# The app sends BOTH on every call; see `api/fallback_auth.py`.
		data["farmops_auth_header"] = f"X-FarmOps-Token: {token['api_key']}:{token['api_secret']}"
		data["secret_note"] = (
			"THIS IS THE ONLY TIME THE SECRET IS READABLE IN A RESULT. Frappe stores it "
			"encrypted; nothing reads it back except generate_mobile_login_qr with "
			"rotate_token=false. Hand it over now or re-issue with generate_api_token."
		)
	elif existed:
		data["secret_note"] = (
			"NO CREDENTIAL WAS TOUCHED, which is the default when updating an account that "
			"already exists: this person has a phone in their pocket and re-scoping them should "
			"not knock it offline. Their existing token still works and now carries the new "
			"entity access. Pass generate_token=true to mint a fresh one — which invalidates "
			"the phone."
		)
	else:
		data["secret_note"] = (
			"No credential was issued (generate_token=false). The account exists and is scoped "
			"but cannot sign in until generate_api_token is run for it."
		)

	if companions_missing:
		data["companion_roles_missing"] = companions_missing
		data["companion_note"] = (
			f"This site has no {', '.join(companions_missing)} role, so it was not assigned. "
			"That role belongs to another app (Frappe HR ships Employee), and this app will not "
			"write permissions onto a doctype it does not own — doing so would make Frappe "
			"ignore every standard permission on it, for every role. Install the owning app, or "
			"accept that this account cannot read its own Employee record."
		)

	data["entity_note"] = roles.entity_access_note(entities)
	return ToolResult(
		data=data,
		summary=(
			f"{'created' if not existed else 'updated'} {email} as {spec.name} "
			f"scoped to {len(entities)} entity/entities" + ("; token issued" if token else "; no token")
		),
	)


# ── 2. list_mobile_users ────────────────────────────────────────────────────
def list_mobile_users(args: dict) -> ToolResult:
	"""Every mobile account: role, entity access, credential age, and any drift."""
	_require_grant()
	filters = {}
	role = as_str(args, "role")
	if role:
		spec = roles.spec_for(role)
		if spec is None:
			raise ToolError(
				f"{role!r} is not one of this app's mobile roles. The {len(roles.ROLE_NAMES)} are: "
				f"{', '.join(roles.ROLE_NAMES)}."
			)
		filters["mobile_role"] = spec.name
	state = as_str(args, "state")
	if state:
		filters["state"] = state
	elif not as_bool(args, "include_revoked", False):
		filters["state"] = ("!=", "Revoked")

	company = resolve_company(as_str(args, "company"), required=False)
	today = str(frappe.utils.today())

	rows = (
		frappe.db.get_all(
			GRANT,
			filters=filters or None,
			fields=[
				"name",
				"user",
				"full_name",
				"mobile_role",
				"state",
				"preferred_company",
				"entity_access",
				"api_key",
				"token_issued_on",
				"token_expires_on",
				"token_revoked_on",
				"token_issue_count",
				"last_qr_issued_on",
				"last_seen_on",
				"revoked_on",
				"revoked_by",
				"revocation_reason",
				"endpoint_url",
				"notes",
			],
			order_by="modified desc",
			limit=LIST_CAP,
		)
		or []
	)

	users = []
	for row in rows:
		row = dict(row)
		user = str(row.get("user") or "")
		live_entities = roles.companies_for(user)
		# THE SAME PARSER THE COLUMN IS WRITTEN WITH, so a grant recorded before
		# the comma fix — one line reading "Orchard Meadow, LLC" — reads back as
		# the one entity it is rather than as two the roster would show as
		# granted. See `roles.split_entity_names`.
		recorded = roles.parse_entity_access(row.get("entity_access"))
		if company and company not in live_entities:
			continue
		enabled = bool(frappe.db.get_value(USER, user, "enabled")) if frappe.db.exists(USER, user) else False
		has_secret = bool(read_api_secret(user))
		review_due = str(row.get("token_expires_on") or "")
		overdue = bool(review_due and review_due < today and row.get("state") == "Active" and has_secret)

		entry = {
			"user": user,
			"full_name": row.get("full_name") or None,
			"role": row.get("mobile_role"),
			"state": row.get("state"),
			"user_enabled": enabled,
			"has_live_token": has_secret,
			"api_key": row.get("api_key") or None,
			"entity_access": live_entities,
			"entity_access_recorded": recorded,
			"preferred_company": row.get("preferred_company") or None,
			"token_issued_on": str(row.get("token_issued_on") or "") or None,
			"token_review_due": review_due or None,
			"token_review_overdue": overdue,
			"token_revoked_on": str(row.get("token_revoked_on") or "") or None,
			"tokens_issued": int(row.get("token_issue_count") or 0),
			"last_qr_issued_on": str(row.get("last_qr_issued_on") or "") or None,
			# THE COLUMN THE IDLE SWEEP ACTS ON, and until now the one column this
			# list did not report. `api/guard` stamps it at most once a day and
			# `sweep_idle_grants` revokes a token nobody has used for
			# `idle_token_days`; a roster that showed every other date but not this
			# one could not answer "why did that phone stop working", which is the
			# only question anybody asks after a sweep.
			"last_seen_on": str(row.get("last_seen_on") or "") or None,
			"endpoint_url": row.get("endpoint_url") or None,
			"revoked_on": str(row.get("revoked_on") or "") or None,
			"revoked_by": row.get("revoked_by") or None,
			"revocation_reason": row.get("revocation_reason") or None,
			"roles_held": roles.roles_of(user),
			"all_roles": roles.all_roles_of(user),
			"employee": _employee_for(user) or None,
			"notes": row.get("notes") or None,
		}

		# THE DRIFT CHECKS. Each one is a state that looks fine on a list and is
		# not, and each one has bitten somebody on some system somewhere.
		concerns = []
		if not live_entities:
			concerns.append(
				"NO User Permission on Company — in Frappe that means this account sees EVERY "
				"entity on the site. Re-run create_mobile_user with update_existing=true."
			)
		if sorted(live_entities) != sorted(recorded) and recorded:
			concerns.append(
				f"the grant records {', '.join(recorded) or 'nothing'} but the live User "
				f"Permissions allow {', '.join(live_entities) or 'nothing'}. Somebody changed "
				"one without the other."
			)
		if row.get("state") == "Revoked" and has_secret:
			concerns.append("REVOKED BUT THE TOKEN STILL WORKS. Run revoke_api_token.")
		if row.get("state") == "Revoked" and enabled:
			concerns.append("REVOKED BUT THE LOGIN IS STILL ENABLED.")
		if overdue:
			concerns.append(
				f"the token's review date ({review_due}) has passed and it is still live. "
				"Frappe API secrets do not expire on their own — this is a reminder, not an "
				"enforcement. Re-issue with generate_api_token or end it with revoke_api_token."
			)
		if row.get("mobile_role") and row["mobile_role"] not in entry["roles_held"]:
			concerns.append(f"the grant says {row['mobile_role']} but the account does not hold that role.")
		entry["concerns"] = concerns
		users.append(entry)

	catalogue = [roles.describe_role(spec, include_permissions=False) for spec in roles.ROLE_SPECS]
	# v0.68.1. THE ANSWER TO "WE HIRED A CHECKER, WHAT DO WE GIVE THEM", returned
	# beside the roles rather than left in a release note. A job title is a
	# Designation on the Employee and a role is what `create_mobile_user` takes;
	# they are two fields set by two tools, and the pair is the configuration.
	job_titles = roles.job_titles()
	flagged = [entry for entry in users if entry["concerns"]]
	return ToolResult(
		data={
			"count": len(users),
			"users": users,
			"needing_attention": len(flagged),
			"company_filter": company,
			"roles": catalogue,
			"job_titles": job_titles,
			"note": (
				"entity_access is read from the LIVE User Permission rows, not from the grant — "
				"so a scoping somebody changed in the Desk shows here as drift rather than "
				"agreeing with a record that is out of date."
			),
			"job_title_note": (
				"`job_titles` maps the farm job titles onto the mobile roles that carry them. A "
				"Checker and a Tractor Driver are DESIGNATIONS on the Employee, not roles — they "
				"touch the same records any Field Worker does, so they hold that role and are "
				"told apart by their designation, which is how "
				"list_pending_threshold_acknowledgments finds every checker on the site. A Crew "
				"Leader IS a role, because forming and closing a shift writes a register no "
				"Field Worker may. Set the designation with update_employee and the role with "
				"create_mobile_user."
			),
		},
		summary=f"{len(users)} mobile account(s), {len(flagged)} needing attention",
	)


# ── 3. revoke_mobile_user ───────────────────────────────────────────────────
def revoke_mobile_user(args: dict) -> ToolResult:
	"""End one account: disable the login, kill the credential, record the reason."""
	_require_grant()
	email = as_str(args, "email") or as_str(args, "user")
	if not email:
		raise ToolError("email is required (the account to revoke).")
	email = email.strip().lower()
	row = _user_row(email)

	reason = as_str(args, "reason").strip()
	if len(reason) < 8:
		raise ToolError(
			"reason is required and has to be a real one. 'Left at the end of harvest', 'phone "
			"lost in the orchard' and 'dismissed for cause' are three different answers to the "
			"question an auditor asks about why somebody's access ended, and this record is the "
			"only place any of them survives. Nothing was changed."
		)

	keep_permissions = as_bool(args, "keep_user_permissions", True)
	had_token = _clear_token(email)
	was_enabled = bool(row.get("enabled"))
	if was_enabled:
		frappe.db.set_value(USER, email, "enabled", 0)

	removed = []
	if not keep_permissions:
		for permission in (
			frappe.db.get_all(
				USER_PERMISSION,
				filters={"user": email, "allow": "Company"},
				fields=["name", "for_value"],
				limit=LIST_CAP,
			)
			or []
		):
			frappe.delete_doc(USER_PERMISSION, permission["name"], force=True, ignore_permissions=True)
			removed.append(permission["for_value"])

	now = _now()
	grant = _write_grant(
		email,
		{
			"state": "Revoked",
			"revocation_reason": reason,
			"revoked_on": now,
			"revoked_by": frappe.session.user,
			"token_revoked_on": now if had_token else None,
			"api_key": "",
			# An account revoked before it ever had a grant still needs one, and
			# guessing "Field Worker" would put a fact on the record that nobody
			# stated. The roles the account actually holds are the honest answer.
			"mobile_role": _grant_row(email).get("mobile_role") or _role_from_held(email),
		},
	)

	return ToolResult(
		data={
			"user": email,
			"grant": grant,
			"login_disabled": True,
			"was_enabled": was_enabled,
			"token_revoked": had_token,
			"user_permissions_kept": keep_permissions,
			"user_permissions_removed": removed,
			"reason": reason,
			"revoked_by": frappe.session.user,
			"revoked_on": now,
			"roles_kept": roles.roles_of(email),
			"note": (
				"The ROLES are left on the account, deliberately. A disabled user with no live "
				"token cannot sign in, and keeping the roles means a re-hire is one "
				"create_mobile_user away and — more importantly — that the record still says "
				"what this person was. An account stripped of its roles is an account nobody "
				"can later answer 'what could they see' about."
				+ (
					""
					if keep_permissions
					else " The Company User Permissions were removed as asked, which loses that "
					"same evidence for the entity scoping. The grant still records it."
				)
			),
		},
		summary=f"revoked {email}: login disabled, {'token killed' if had_token else 'no token to kill'}",
	)


# ── 4. generate_api_token ───────────────────────────────────────────────────
def generate_api_token(args: dict) -> ToolResult:
	"""Mint a fresh API credential for one user. The secret is readable ONCE."""
	email = (as_str(args, "user", required=True) or "").strip().lower()
	row = _user_row(email)
	if not row.get("enabled"):
		raise ToolError(
			f"User {email!r} is disabled, so a credential for it would not work. Enable the "
			"account first — a token minted for a disabled login is a token somebody will spend "
			"an afternoon debugging. Nothing was changed."
		)

	review_days = as_int(args, "expiry_days", DEFAULT_TOKEN_REVIEW_DAYS) or DEFAULT_TOKEN_REVIEW_DAYS
	if review_days <= 0:
		raise ToolError("expiry_days must be a positive number of days.")

	replaced = bool(read_api_secret(email))
	token = _issue_token(email)
	review_due = frappe.utils.add_days(frappe.utils.today(), review_days)
	existing = _grant_row(email)
	grant = None
	if doctype_exists(GRANT):
		grant = _write_grant(
			email,
			{
				"mobile_role": existing.get("mobile_role") or _role_from_held(email),
				"full_name": existing.get("full_name") or str(row.get("full_name") or ""),
				"state": "Active",
				"api_key": token["api_key"],
				"token_issued_on": _now(),
				"token_expires_on": review_due,
				"token_revoked_on": None,
				"token_issue_count": int(existing.get("token_issue_count") or 0) + 1,
				"revocation_reason": "",
				"revoked_on": None,
				"revoked_by": None,
				"entity_access": existing.get("entity_access") or "\n".join(roles.companies_for(email)),
				"preferred_company": existing.get("preferred_company") or roles.default_company_for(email),
				"endpoint_url": existing.get("endpoint_url") or _endpoint_url(args),
			},
		)

	return ToolResult(
		data={
			"user": email,
			"api_key": token["api_key"],
			"api_secret": token["api_secret"],
			"auth_header": f"Authorization: token {token['api_key']}:{token['api_secret']}",
			#: v0.17.2 — the same pair for the mobile transport, which reaches
			#: Frappe through a proxy that eats `Authorization`.
			"farmops_auth_header": f"X-FarmOps-Token: {token['api_key']}:{token['api_secret']}",
			"endpoint": f"{_endpoint_url(args)}/api/method/erpnext_mcp.mcp.handle",
			#: v0.18.0 — where a PHONE goes, which is somewhere else entirely.
			#: `endpoint` above is the MCP endpoint an AI client calls and means
			#: the same thing in all three tools that emit it; this is the first
			#: URL the Farm Ops app hits with this credential, and the one an
			#: operator should curl before handing a phone to somebody.
			"mobile_endpoint": f"{_endpoint_url(args)}{LOGIN_PROBE_PATH}",
			"replaced_previous_token": replaced,
			"token_review_due": review_due,
			"review_days": review_days,
			"grant": grant,
			"roles_held": roles.roles_of(email),
			"entity_access": roles.companies_for(email),
			"secret_note": (
				"THIS IS THE ONLY TIME THE SECRET APPEARS IN A RESULT. Frappe stores it "
				"encrypted from here on. Hand it over now, or re-run this — which mints a new "
				"one and stops the old one working."
			),
			"expiry_note": (
				f"token_review_due is {review_due}, and it is a REVIEW DATE, NOT AN EXPIRY. "
				"Frappe API secrets do not expire on their own, and this app installs no "
				"scheduled job that revokes one — a job rewriting another app's User records at "
				"three in the morning is not a thing this app does. list_mobile_users flags an "
				"overdue grant; revoke_api_token is what actually ends it."
			),
			"transport_note": (
				"This credential buys IDENTITY, not entry. Against the MCP endpoint it is the "
				"second header: that request still presents the shared X-MCP-Token and still "
				"has to come from an allowed CIDR. Against the mobile endpoint (mobile_endpoint "
				"above, v0.18.0) it is the ONLY credential — there is no shared token and no "
				"CIDR gate on that path, and what stands in their place is the role gate, the "
				"Mobile Access Grant, entity scoping and the rate limit, on every call."
			),
		},
		summary=f"issued an API token for {email}"
		+ (" (replacing the previous one)" if replaced else "")
		+ f", review due {review_due}",
	)


def _role_from_held(user: str) -> str:
	held = roles.roles_of(user)
	return held[0] if held else "Field Worker"


# ── 5. revoke_api_token ─────────────────────────────────────────────────────
def revoke_api_token(args: dict) -> ToolResult:
	"""Invalidate one user's API credential. The account itself stays enabled."""
	email = (as_str(args, "user", required=True) or "").strip().lower()
	_user_row(email)
	had = _clear_token(email)
	reason = as_str(args, "reason")

	grant = None
	if doctype_exists(GRANT) and frappe.db.exists(GRANT, email):
		existing = _grant_row(email)
		grant = _write_grant(
			email,
			{
				"mobile_role": existing.get("mobile_role") or _role_from_held(email),
				"api_key": "",
				"token_revoked_on": _now(),
				"notes": _append_note(existing.get("notes"), f"token revoked: {reason}" if reason else ""),
			},
		)

	return ToolResult(
		data={
			"user": email,
			"token_revoked": had,
			"grant": grant,
			"reason": reason or None,
			"login_still_enabled": bool(frappe.db.get_value(USER, email, "enabled")),
			"note": (
				"The credential is gone; the ACCOUNT is untouched and still enabled. That is the "
				"difference between this and revoke_mobile_user: this is 'they lost their "
				"phone', that is 'they no longer work here'."
				if had
				else "There was no live credential on this account, so nothing was revoked. The "
				"account is unchanged."
			),
		},
		summary=f"{'revoked' if had else 'found no'} API token for {email}",
	)


def _append_note(existing, addition: str) -> str:
	existing = str(existing or "").strip()
	addition = str(addition or "").strip()
	if not addition:
		return existing
	return f"{existing}\n{addition}".strip()


# ── 6. get_current_user_context ─────────────────────────────────────────────
def get_current_user_context(args: dict) -> ToolResult:
	"""Who is calling, what they may do, and which entity the app should open on."""
	user, source = resolve_context_user(args)

	if not user or user == "Guest":
		return ToolResult(
			data={
				"user": None,
				"identified": False,
				"identity_source": source,
				"note": (
					"No per-user identity on this request. The MCP bearer token authorises the "
					"CALL; it does not name a person. A mobile client identifies itself by "
					"sending `Authorization: token <api_key>:<api_secret>` ALONGSIDE the "
					"X-MCP-Token header — generate_api_token produces the pair and prints the "
					"header. An operator driving this from a desktop client can pass `user` "
					"explicitly instead."
				),
			},
			summary="no per-user identity on this request",
		)

	row = dict(frappe.db.get_value(USER, user, ["name", "enabled", "full_name"], as_dict=True) or {})
	grant = _grant_row(user)
	held = roles.roles_of(user)
	entities = roles.companies_for(user)
	preferred = grant.get("preferred_company") or roles.default_company_for(user)
	specs = [roles.describe_role(roles.BY_NAME[name]) for name in held]
	today = str(frappe.utils.today())
	review_due = str(grant.get("token_expires_on") or "")

	return ToolResult(
		data={
			"user": user,
			"identified": True,
			"identity_source": source,
			"full_name": row.get("full_name") or None,
			"enabled": bool(row.get("enabled")),
			"employee": _employee_for(user) or None,
			"mobile_roles": held,
			"all_roles": roles.all_roles_of(user),
			"role_detail": specs,
			"primary_role": held[0] if held else None,
			"entity_access": entities,
			"preferred_company": preferred or None,
			"entity_note": roles.entity_access_note(entities),
			"grant_state": grant.get("state") or None,
			"token_review_due": review_due or None,
			"token_review_overdue": bool(review_due and review_due < today),
			"endpoint": f"{_endpoint_url(args)}/api/method/erpnext_mcp.mcp.handle",
			"can": sorted({item for spec in specs for item in _can_lines(spec)}),
			"cannot": sorted({line for spec in specs for line in spec.get("cannot", [])}),
			"note": (
				"entity_access is what this user's Company User Permissions allow. Every list "
				"this app returns to them is already filtered by it at the framework level — the "
				"app does not have to filter again and must not pretend it did."
			),
		},
		summary=(
			f"{user}: {', '.join(held) or 'no mobile role'}; "
			f"{len(entities)} entity/entities; opens on {preferred or 'nothing'}"
		),
	)


def _can_lines(spec: dict) -> list:
	out = []
	for perm in spec.get("permissions") or []:
		verbs = [name for name in ("create", "write", "read") if perm.get(name)]
		if verbs:
			out.append(f"{'/'.join(verbs)} {perm['doctype']}")
	return out


def resolve_context_user(args: dict) -> tuple:
	"""Which user a mobile-ergonomic tool is acting for, and how that was decided.

	THE SESSION DECIDES; AN EXPLICIT ARGUMENT IS A FALLBACK, NOT AN OVERRIDE.
	`security.caller_identity()` is the Frappe user who authenticated THIS
	request — a real credential Frappe validated, caught in the one-line window
	before this app assumes the MCP System User. When there is one, it wins, and
	a `user` argument naming somebody else is REFUSED rather than quietly
	honoured: a worker whose phone can act as another worker by adding a field
	to a JSON body is not scoping, it is decoration.

	When there is no per-user identity — an operator's desktop MCP client
	presenting only the shared bearer token — an explicit `user` argument is
	accepted, because that caller has already proved they hold the operator's
	token and could read the same records through any read tool anyway.
	"""
	authenticated = security.caller_identity()
	explicit = (as_str(args or {}, "user") or as_str(args or {}, "worker_user")).strip().lower()

	if authenticated and authenticated not in ("Guest", settings.FALLBACK_USER):
		if explicit and explicit != str(authenticated).lower():
			raise ToolError(
				f"this request is authenticated as {authenticated} and asked to act as "
				f"{explicit!r}. It will not: an account that can name somebody else in a "
				"request body is not scoped to anything. Drop the `user` argument, or call "
				"with that account's own credential."
			)
		return str(authenticated), "authenticated request (Authorization: token …)"

	if explicit:
		if not frappe.db.exists(USER, explicit):
			raise ToolError(f"no User {explicit!r} on this site.")
		return explicit, "the `user` argument (this request carries no per-user credential)"

	return "", "none"


# ── 7. generate_mobile_login_qr ─────────────────────────────────────────────
def generate_mobile_login_qr(args: dict) -> ToolResult:
	"""The enrolment card: url + user + token, as a scannable PNG.

	The mobile app scans it, stores the credential in the Keychain, and every
	call after that carries it as a header. That is the whole flow, and the
	alternative is somebody typing a 15-character secret into a phone keyboard
	in a farm office, which is how the secret ends up on a whiteboard.
	"""
	if not qr.available():
		raise ToolError(
			"this site has no QR encoder, so the login card cannot be drawn. It needs " + qr.REQUIRES
		)

	email = (as_str(args, "user", required=True) or "").strip().lower()
	row = _user_row(email)
	if not row.get("enabled"):
		raise ToolError(
			f"User {email!r} is disabled. A login card for an account that cannot sign in is a "
			"card somebody will scan in a field and blame the app for. Nothing was changed."
		)

	url = _endpoint_url(args)
	if not url:
		raise ToolError(
			"this site does not know its own public URL, so the QR would point a phone at "
			"nothing. Fill in `public_url` on ERPNext MCP Settings with the Tailscale Funnel "
			"address — https://<host>.<tailnet>.ts.net — or pass `url`. get_tailscale_funnel_config "
			"reports what this machine is actually serving."
		)
	if not str(url).lower().startswith("https://"):
		raise ToolError(
			f"the endpoint URL is {url!r}, which is not HTTPS. This QR carries a live credential; "
			"encoding one for a plaintext endpoint would put it on the wire in the clear at every "
			"call, forever. Fix public_url, or pass an https:// url. Nothing was written."
		)

	# NOT `... or DEFAULT_QR_HOURS`. `as_int` already answers the default for a
	# missing or empty value, so the trailing `or` could only fire on an explicit
	# 0 — and 0 is the exact value THE NEXT LINE IS WRITTEN TO REFUSE. With it
	# there the refusal was unreachable for the case it names first, and a caller
	# asking for a zero-hour credential was handed a live working login QR on the
	# default window instead of being told no.
	hours = as_int(args, "expiry_hours", DEFAULT_QR_HOURS)
	if hours <= 0 or hours > 168:
		raise ToolError(
			"expiry_hours must be between 1 and 168 (a week). A login QR is valid for the "
			"length of an onboarding conversation; one valid for a season is a live credential "
			"sitting in somebody's photo roll."
		)

	rotate = as_bool(args, "rotate_token", True)
	if rotate:
		token = _issue_token(email)
		secret = token["api_secret"]
		api_key = token["api_key"]
	else:
		secret = read_api_secret(email)
		api_key = str(row.get("api_key") or "")
		if not secret or not api_key:
			raise ToolError(
				f"{email} has no live API credential to put on a card, and rotate_token=false "
				"says not to mint one. Run generate_api_token, or call this with "
				"rotate_token=true (the default)."
			)

	expires_at = frappe.utils.add_to_date(_now(), hours=hours)
	payload = mobile_login_payload(
		url=url,
		user=email,
		api_key=api_key,
		api_secret=secret,
		expires_at=str(expires_at),
	)
	text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
	drawn = qr.render(text, error=as_str(args, "error_correction") or "M")
	png = drawn["png"]

	archived = None
	if as_bool(args, "archive", False):
		archived = _archive_card(email, png, url, expires_at, args)

	grant = None
	if doctype_exists(GRANT):
		existing = _grant_row(email)
		values = {
			"mobile_role": existing.get("mobile_role") or _role_from_held(email),
			"last_qr_issued_on": _now(),
			"qr_expires_at": str(expires_at),
			"endpoint_url": url,
		}
		if archived:
			values["qr_document"] = archived["governance_document"]
		if rotate:
			values.update(
				{
					"api_key": api_key,
					"token_issued_on": _now(),
					"token_issue_count": int(existing.get("token_issue_count") or 0) + 1,
					"token_revoked_on": None,
					"state": "Active",
				}
			)
		grant = _write_grant(email, values)

	return ToolResult(
		data={
			"user": email,
			"png_base64": base64.b64encode(png).decode("ascii"),
			"mime_type": "image/png",
			"bytes": len(png),
			"pixels": drawn["pixels"],
			"modules": drawn["modules"],
			"encoder": drawn["encoder"],
			"error_correction": drawn["error_correction"],
			"payload": payload,
			"payload_bytes": len(text),
			"expires_at": str(expires_at),
			"expiry_hours": hours,
			"token_rotated": rotate,
			"endpoint": payload["endpoint"],
			"grant": grant,
			"archive": archived,
			"roles_held": roles.roles_of(email),
			"entity_access": roles.companies_for(email),
			"security_note": (
				"THIS IMAGE IS A LIVE CREDENTIAL. Anybody who photographs it over somebody's "
				"shoulder has this account until the token is revoked. That is inherent to "
				f"enrolment by QR, and the mitigation is time: it stops being valid to enrol "
				f"with at {expires_at} ({hours}h), and re-minting rotates the secret so an old "
				"photograph stops working. Show it, let it be scanned, and do not put it in a "
				"group chat."
				+ (
					" rotate_token=true means the previous credential has ALREADY stopped "
					"working — any phone already enrolled on this account must re-scan."
					if rotate
					else " rotate_token=false: the existing credential was re-printed, so "
					"phones already enrolled keep working — and so does any earlier copy of "
					"this card."
				)
			),
			"app_note": (
				"The app stores `api_key` and `api_secret` in the Keychain and sends the pair "
				"TWICE on every call: `Authorization: token <api_key>:<api_secret>` and "
				"`X-FarmOps-Token: <api_key>:<api_secret>`. The second is not decoration — "
				"v0.17.2: the Tailscale serve/funnel proxy removes `Authorization`, so a phone "
				"sending only the first arrives as Guest and gets the Desk's /me page instead "
				"of JSON. A client that cannot set custom headers may send the same pair as "
				'`{"_auth": {"api_key": …, "api_secret": …}}` in the POST body. `expires_at` is '
				"the deadline for ENROLLING, not for the credential — once stored, the token "
				"works until it is revoked."
			),
			"endpoint_note": (
				f"v0.18.0 MOVED WHERE THE PHONE CALLS: `{API_BASE}/mobile/<method>` and "
				f"`{API_BASE}/files/<method>`, served by farmops-api — a separate process that "
				"does not go through Frappe's /api/method handler, because through the Tailscale "
				"funnel that handler answered every one of v0.17.2's five credential carriers "
				"with the Desk's HTML login page. `api_base` in the payload carries the prefix, "
				"so a site that ever moves it moves it in one place and the cards follow. The "
				"old path stays live for the LAN. `endpoint` above is the FIRST URL the phone "
				"will call — curl it with the X-FarmOps-Token header before handing the phone to "
				"anybody, and a JSON user context back means enrolment will work."
			),
		},
		summary=(
			f"login QR for {email} ({drawn['pixels']}px, {len(png)} bytes), valid to enrol "
			f"until {expires_at}" + ("; token rotated" if rotate else "")
		),
	)


#: What the payload says it IS. v0.17.1.
#:
#: `FarmOpsKit`'s `LoginQRParser` refuses any payload whose `type` is not exactly
#: this, BY NAME, and v0.17.0 shipped without the key — so every scan failed
#: with "That's a different kind of QR code, not a Farm Ops login" and enrolment
#: was impossible. The check is not pedantry on the app's side: FarmCore and
#: BucketLog issue their own onboarding codes (`farm_app_nostr_link`) on the same
#: phones, and a scanner that accepted any well-formed JSON would let two apps
#: cross-sign each other's credentials.
LOGIN_QR_TYPE = "farm_ops_login"

#: Where the phone's eleven methods answer from v0.18.0 on. The app builds each
#: URL as `<url><API_BASE>/mobile/<method>` — see `farmops_api/routes.py`, which
#: owns the same constant and is what actually serves them.
#:
#: `/api/method/erpnext_mcp.api.mobile.<method>` still works and is still tested;
#: it is the LAN and in-container path. What it does not do is survive the
#: Tailscale proxy, which is the whole of why this constant exists.
API_BASE = "/farmops/api"

#: What the card says to curl. The FIRST call any phone makes, so an operator who
#: pastes it either gets a JSON user context — in which case enrolment will work
#: — or learns which of the funnel, the service and the credential is wrong,
#: before handing the phone to somebody standing in an orchard.
LOGIN_PROBE_PATH = f"{API_BASE}/mobile/get_current_user_context"


def mobile_login_payload(url: str, user: str, api_key: str, api_secret: str, expires_at: str) -> dict:
	"""What goes IN the QR. One function, so the tests read the same shape the app does.

	`type` comes first and is the whole reason v0.17.1 exists — see
	`LOGIN_QR_TYPE`. It is a constant rather than an argument: a payload whose
	type a caller could choose would be a payload that could claim to be another
	app's.

	`token` is the whole `key:secret` pair in the form the header wants, because
	the app's job at enrolment is to store a string and put it after the word
	`token` — splitting it into two fields invites a client to reassemble it in
	the wrong order, and the failure looks like an authentication bug rather than
	a parsing one. `api_key` and `api_secret` are ALSO present, separately, and
	they are what `LoginQRParser` actually reads.

	Keys are short and stable. A QR's module count grows with its payload, and a
	payload with roomy key names is a physically larger square somebody has to
	hold a phone further back from.

	────────────────────────────────────────────────────────────────────────
	v0.18.0: `api_base` IS NEW AND `v` DELIBERATELY DID NOT MOVE
	────────────────────────────────────────────────────────────────────────

	`LoginQRParser` refuses any payload whose `v` is greater than the build's own
	`supportedVersion`, which is 1 — so bumping it here would make **every card
	unscannable by every phone already in the field**, including the phones this
	release exists to fix. The transport moved; the enrolment format did not.

	`api_base` and the repointed `endpoint` are therefore ADDITIVE and
	INFORMATIONAL. `LoginQRPayload` decodes six keys and ignores the rest, so a
	shipped build reads this card exactly as it read the last one. What the two
	keys are actually for is the human in the loop: an operator with a card and
	a terminal can now `curl` the exact URL the phone is about to call, which is
	how "the funnel is wrong" stops being indistinguishable from "the credential
	is wrong" at the point somebody is standing in an orchard.
	"""
	base = str(url).rstrip("/")
	return {
		"type": LOGIN_QR_TYPE,
		"v": 1,
		"url": base,
		# The method-path prefix a v0.18.0 client joins to `url`. The app builds
		# `<url><api_base>/mobile/<method>`; it is emitted rather than hardcoded
		# in the client so a site that ever moves the prefix moves it in one
		# place and the cards follow.
		"api_base": API_BASE,
		"endpoint": f"{base}{LOGIN_PROBE_PATH}",
		"user": user,
		"token": f"{api_key}:{api_secret}",
		"api_key": api_key,
		"api_secret": api_secret,
		"expires_at": str(expires_at),
	}


def _archive_card(user: str, png: bytes, url: str, expires_at, args: dict) -> dict:
	"""File the card in the governance archive, as a PRIVATE attachment.

	THE OFFLINE DISTRIBUTION PATH. A camp office at the end of a gravel road
	prints the archived copy rather than asking somebody to hold a laptop up.
	Private is not an argument — see `artifacts.attach_bytes`; this one carries a
	credential, so it is the last file on the site that should be at a public URL.
	"""
	if not doctype_exists("Governance Document"):
		return {
			"archived": False,
			"reason": "this site has no Governance Document doctype, so there is nowhere to file it.",
		}
	company = resolve_company(as_str(args, "company"), required=False) or roles.default_company_for(user)
	if not company:
		return {
			"archived": False,
			"reason": (
				"a Governance Document needs a Company and this account has no entity access to "
				"take one from. Pass `company`."
			),
		}
	doc = frappe.get_doc(
		{
			"doctype": "Governance Document",
			"title": f"Farm Ops mobile enrolment card — {user}",
			"category": "Other",
			"company": company,
			"effective_date": frappe.utils.today(),
			"notes": (
				f"<p>Enrolment QR for <b>{frappe.utils.escape_html(user)}</b> against "
				f"{frappe.utils.escape_html(url)}. Valid to enrol until {expires_at}.</p>"
				"<p><b>This attachment is a live credential.</b> It is private; keep it that "
				"way. Once the account is enrolled, or once the deadline passes, this document "
				"should be deleted rather than kept as a record — the record worth keeping is "
				"the Mobile Access Grant, which holds no secret.</p>"
			),
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	attachment = artifacts.attach_bytes(
		"Governance Document",
		doc.name,
		f"mobile-enrolment-{user.replace('@', '-at-')}.png",
		png,
		field="attached_file",
	)
	return {
		"archived": True,
		"governance_document": doc.name,
		"company": company,
		"attachment": artifacts.describe_attachment(attachment, png),
		"note": (
			"Filed PRIVATE. Delete this document once the phone is enrolled — the durable record "
			"is the Mobile Access Grant, and it holds no secret."
		),
	}


# ── the nightly idle sweep ──────────────────────────────────────────────────
#: What the sweep writes into a swept grant's revocation reason.
IDLE_REASON = "token revoked by the idle sweep: no call from this credential in {days} day(s)"


def sweep_idle_grants() -> int:
	"""Revoke the token on every credential nobody has used for the idle window.

	v0.17.1. THIS IS THE JOB THE v0.17.0 DOCSTRING SAID THIS APP WOULD NOT
	INSTALL, and the reversal is deliberate rather than an oversight — see the
	module docstring, which now records both the old argument and why it lost.

	The short version: the old objection was that such a job "would rewrite
	another app's User records on a timer with nobody watching", and every clause
	of that was answerable. It rewrites only the two credential fields, only on
	accounts this app itself created and minted a key for, only where a Mobile
	Access Grant says so — and somebody IS watching, because every revocation
	writes an MCP Action Log row and the run emails a summary.

	What actually changed is that v0.17.1 put those credentials on the open
	internet. A key that only ever travelled to a laptop on the LAN could be left
	to a human to review; forty of them in forty pockets, any one of which can be
	left on a truck seat and not mentioned, cannot. An unreported lost phone is
	the threat, and a credential that stops working by itself is the only control
	that does not depend on somebody admitting to it.

	FOUR THINGS IT DOES NOT DO, each of them a way this could have gone wrong:

	  * It does NOT disable the account, remove roles, or touch entity access.
	    The worker still exists and still has the same access the day they scan a
	    new QR. This is "your phone went quiet", not "you no longer work here" —
	    `revoke_mobile_user` is the other one and a timer must never reach it.
	  * It does NOT touch a grant that is not Active, so a revoked grant is not
	    revoked twice and an Expired one is left for a human.
	  * It does NOT touch a grant marked `persistent`. A winter caretaker's phone
	    is legitimately quiet for months.
	  * It does NOT age a grant it has no clock for. Where `last_seen_on` is
	    empty the token's ISSUE date is used, and where there is neither, the
	    grant is left alone and reported — guessing an age for a credential and
	    then acting on the guess is the shape of mistake that ends with somebody
	    locked out mid-harvest.

	Never raises, like every scheduled job here. Returns how many it revoked.
	"""
	try:
		days = settings.mobile_grant_idle_days()
		if days <= 0 or not doctype_exists(GRANT):
			return 0
		swept, skipped = _sweep_idle(days)
		if swept:
			_report_sweep(swept, skipped, days)
		return len(swept)
	except Exception:
		try:
			frappe.log_error(
				title="erpnext_mcp: the idle mobile credential sweep failed",
				message=frappe.get_traceback(),
			)
		except Exception:
			pass
		return 0


def _sweep_idle(days: int) -> tuple:
	"""Revoke the idle ones; return (what was revoked, what could not be judged)."""
	cutoff = str(frappe.utils.add_days(frappe.utils.now(), -days))
	rows = (
		frappe.db.get_all(
			GRANT,
			filters={"state": "Active"},
			fields=["name", "user", "full_name", "last_seen_on", "token_issued_on", "persistent"],
			limit=LIST_CAP,
		)
		or []
	)

	swept, skipped = [], []
	for row in rows:
		row = dict(row)
		if compat.checked(row.get("persistent")):
			continue
		# A grant carrying no credential has nothing to revoke — a worker whose
		# token was already taken away is not swept again.
		if not read_api_secret(row["user"]):
			continue
		clock = str(row.get("last_seen_on") or "") or str(row.get("token_issued_on") or "")
		if not clock:
			skipped.append({"user": row["user"], "reason": "no last_seen_on and no token_issued_on"})
			continue
		if clock >= cutoff:
			continue
		try:
			_clear_token(row["user"])
			_write_grant(
				row["user"],
				{
					"mobile_role": _grant_row(row["user"]).get("mobile_role") or _role_from_held(row["user"]),
					"state": "Revoked",
					"api_key": "",
					"token_revoked_on": _now(),
					"revocation_reason": IDLE_REASON.format(days=days),
					"revoked_on": _now(),
					"revoked_by": "Administrator",
				},
			)
		except Exception as exc:
			# One grant that will not revoke must not stop the other thirty-nine.
			skipped.append({"user": row["user"], "reason": f"{type(exc).__name__}: {exc}"})
			continue
		swept.append({"user": row["user"], "full_name": row.get("full_name"), "last_seen_on": clock})
		audit.record(
			"sweep_idle_grants",
			{"user": row["user"], "idle_since": clock, "idle_days": days},
			audit.STATUS_SUCCESS,
			f"revoked the idle credential for {row['user']} (last seen {clock})",
			commit=False,
		)
	return swept, skipped


def _report_sweep(swept: list, skipped: list, days: int) -> None:
	"""Tell somebody. A credential that stopped working silently is a support call.

	Goes to the same recipients as the drift watch, and falls back to the Error
	Log for the same reason: a site with no outgoing mail account is ordinary, and
	the message that explains why forty phones asked to be re-enrolled is exactly
	the one nobody should have to reconstruct.
	"""
	from .. import drift

	lines = [
		f"<p><b>{len(swept)} Farm Ops credential(s)</b> were revoked after {days} day(s) with no "
		"call. The accounts are untouched — same roles, same entity access — and each worker "
		"needs a fresh login QR (<code>generate_mobile_login_qr</code>) to get back on.</p>",
		"<ul>"
		+ "".join(
			f"<li>{frappe.utils.escape_html(str(row.get('full_name') or row['user']))} "
			f"({frappe.utils.escape_html(row['user'])}) — last seen {row['last_seen_on']}</li>"
			for row in swept
		)
		+ "</ul>",
		"<p>Tick <b>Exempt From Idle Sweep</b> on a Mobile Access Grant that is legitimately quiet "
		"for months, or set the window to 0 on ERPNext MCP Settings to switch the sweep off.</p>",
	]
	if skipped:
		lines.append(
			"<p>Left alone because their age could not be judged: "
			+ ", ".join(frappe.utils.escape_html(f"{row['user']} ({row['reason']})") for row in skipped)
			+ "</p>"
		)
	body = "".join(lines)
	try:
		to = drift.recipients()
		if to:
			frappe.sendmail(
				recipients=to,
				subject=f"ERPNext MCP: {len(swept)} idle Farm Ops credential(s) revoked",
				message=body,
				now=False,
			)
			return
	except Exception:
		pass
	try:
		frappe.log_error(title="erpnext_mcp: idle Farm Ops credentials revoked", message=body)
	except Exception:  # pragma: no cover
		pass


# ── the role catalogue, for a client that wants it without a user ───────────
def list_mobile_roles() -> list:
	"""Every role this app defines, in full. Used by list_mobile_users."""
	return [roles.describe_role(spec) for spec in roles.ROLE_SPECS]


# ── 12. recover_mobile_access ───────────────────────────────────────────────
#: Shortest a recovery `reason` may be and still be an explanation. Matches the
#: floor `evidence.py` puts on a journal-entry reason, for the same judgement: a
#: mandatory field somebody types "lost" into has been satisfied without being
#: answered, and this one is the audit trail for a credential reset.
MIN_RECOVERY_REASON = 8


def recover_mobile_access(args: dict) -> ToolResult:
	"""A worker lost their phone: kill the old credential, mint a new one, keep the person.

	  WHAT EXISTED AND WHY IT WAS NOT A RECOVERY PATH. Every mechanical piece has
	  been here since v0.17.0 — `revoke_api_token` even says in its own result that
	  it is "the 'they lost their phone' one" — and a manager holding a lost-phone
	  report still had to do three things in the right order, keyed on a value they
	  usually do not have.

	1. THEY DO NOT KNOW THE LOGIN. A foreman knows a face and a badge. Every
	   tool in this module takes `user`, which is an email address on a system
	   the worker has never signed into from a keyboard.

	2. THE ORDER MATTERS AND NOTHING ENFORCED IT. The phone is in somebody
	   else's pocket right now. Minting the replacement first and revoking
	   afterwards leaves the old credential live for as long as the second call
	   takes — and if the second call never happens, forever. This revokes
	   FIRST, always, and reports it as a separate outcome.

	3. NOTHING ASKED WHO IT WAS FOR. `generate_api_token` mints a credential
	   for whatever login it is given. That is right for a tool an
	   administrator drives, and wrong as the whole of an account-recovery
	   path, because the request arrives as somebody at a farm office saying
	   they are somebody.

	  ────────────────────────────────────────────────────────────────────────
	  THE BADGE IS THE IDENTITY PROOF, AND ITS ABSENCE IS RECORDED RATHER THAN
	  REFUSED
	  ────────────────────────────────────────────────────────────────────────

	  A badge is a physical card the worker still has when the phone is gone, and
	  scanning it proves possession of something this site issued to one person.
	  When `badge` is given it resolves through the same register a crew clock
	  uses — so a retired card, an unknown card and a card belonging to somebody
	  who has left are three different refusals rather than one — and when the
	  caller ALSO names an employee or a login, the two must agree. A badge that
	  resolves to somebody else stops the reset, because that is either the wrong
	  card or the wrong person and neither should end in a working credential.

	  THE NO-BADGE PATH IS NOT REFUSED. A worker who lost the phone AND the card is
	  an ordinary Tuesday, and a recovery tool that could not serve it is a recovery
	  tool a farm routes around. What it does instead is SAY SO:
	  `identity_verified_by` is `"badge"` or `"manager assertion"`, and the second
	  is a fact about how much this reset is worth, recorded on the grant and in
	  the audit row rather than left to be inferred from an absent argument.

	  ────────────────────────────────────────────────────────────────────────
	  THE EMPLOYEE RECORD IS NEVER TOUCHED
	  ────────────────────────────────────────────────────────────────────────

	  Not re-created, not duplicated, not re-onboarded. Their badge, their shifts,
	  their buckets, their housing, their I-9 and their W-4 all hang off an Employee
	  docname that does not change here — which is the whole difference between
	  recovering an account and hiring somebody twice. A person with no login at all
	  is REFUSED and pointed at `onboard_employee(employee=...)`, which reuses the
	  same record for exactly this reason.
	"""
	reason = as_str(args, "reason", required=True).strip()
	if len(reason) < MIN_RECOVERY_REASON:
		raise ToolError(
			f"reason is {reason!r}. This is the audit trail for killing somebody's credential and "
			"minting another — say what happened and where, so the row means something in "
			"November. Nothing was changed."
		)

	person, email, verification = _recovery_subject(args)

	if not email:
		raise ToolError(
			f"{person or 'that person'} has no login on this site, so there is no credential to "
			"recover. onboard_employee(employee=...) gives them one and REUSES this Employee "
			"record rather than creating a second — their badge, shifts, buckets and paperwork "
			"all hang off it. Nothing was changed."
		)

	row = _user_row(email)
	if not row.get("enabled"):
		raise ToolError(
			f"User {email!r} is disabled. A credential minted for a disabled login does not work, "
			"and re-enabling somebody is a different decision from replacing their phone — if "
			"they still work here, enable the account first. Nothing was changed."
		)

	# ARGUMENTS ARE CHECKED BEFORE ANYTHING IS DESTROYED. `expiry_days` is
	# validated inside `generate_api_token`, which runs after the revocation —
	# so a typo in it would have killed a working credential and then refused to
	# issue the replacement. Nothing about a bad argument requires the revocation
	# to have happened, and the "safe side" argument below is about failures
	# that only surface once the mint is genuinely under way.
	review_days = as_int(args, "expiry_days", DEFAULT_TOKEN_REVIEW_DAYS) or DEFAULT_TOKEN_REVIEW_DAYS
	if review_days <= 0:
		raise ToolError(
			"expiry_days must be a positive number of days. Nothing was changed — the phone that "
			"was lost still works, so fix the argument and call again."
		)

	# REVOKED FIRST, ALWAYS. The old phone is in somebody else's pocket while
	# this call runs; a failure after this point leaves the account with NO
	# credential, which is the safe side of that trade.
	had_token = _clear_token(email)

	issued = generate_api_token(
		{
			"user": email,
			"expiry_days": review_days,
			"url": args.get("url"),
		}
	).data

	if doctype_exists(GRANT) and frappe.db.exists(GRANT, email):
		existing = _grant_row(email)
		_write_grant(
			email,
			{
				"mobile_role": existing.get("mobile_role") or _role_from_held(email),
				"notes": _append_note(
					existing.get("notes"),
					f"credential recovered ({verification['method']}): {reason}",
				),
			},
		)

	card = None
	if as_bool(args, "issue_qr", False):
		card = generate_mobile_login_qr({**args, "user": email}).data

	return ToolResult(
		data={
			"user": email,
			"employee": person or None,
			"employee_name": verification.get("employee_name"),
			"identity_verified_by": verification["method"],
			"badge": verification.get("badge"),
			"reason": reason,
			"previous_credential_revoked": had_token,
			"api_key": issued["api_key"],
			"api_secret": issued["api_secret"],
			"auth_header": issued["auth_header"],
			"farmops_auth_header": issued["farmops_auth_header"],
			"mobile_endpoint": issued["mobile_endpoint"],
			"token_review_due": issued["token_review_due"],
			"entity_access": issued["entity_access"],
			"roles_held": issued["roles_held"],
			"qr": card,
			"employee_record_note": (
				"THE EMPLOYEE RECORD WAS NOT TOUCHED. Their badge, shifts, buckets, housing and "
				"paperwork all still hang off the same docname — recovering an account and "
				"hiring somebody twice are different acts, and only one of them leaves a person "
				"on the dispatch board twice and in the payroll register once."
			),
			"secret_note": issued["secret_note"],
			"old_device_note": (
				"The previous credential was revoked BEFORE the new one was minted, so the lost "
				"handset stopped working at that moment rather than when somebody got round to "
				"the second call."
				if had_token
				else "This account held no live credential, so nothing was revoked — the phone "
				"that was lost was already logged out, or was never enrolled."
			),
			"verification_note": verification["note"],
		},
		summary=(
			f"recovered mobile access for {verification.get('employee_name') or email} "
			f"({verification['method']})"
			+ ("; previous credential revoked" if had_token else "; there was no live credential")
			+ ("; QR issued" if card else "")
		),
	)


def _recovery_subject(args: dict) -> tuple:
	"""`(employee, login, verification)` from a badge, an Employee or a login.

	THE BADGE WINS AND THE OTHERS ARE CHECKED AGAINST IT. When a caller scans a
	card and also names who they think it is, agreement is the verification —
	and disagreement is the one outcome that must not end in a working
	credential, because it is either the wrong card or the wrong person.
	"""
	from . import badges

	badge_id = as_str(args, "badge") or as_str(args, "badge_id")
	claimed_employee = as_str(args, "employee")
	claimed_user = (as_str(args, "user") or "").strip().lower()

	if not (badge_id or claimed_employee or claimed_user):
		raise ToolError(
			"name who lost the phone: `badge` (scanned from the card they still have, which is "
			"the only argument here that PROVES anything), `employee`, or `user`. Nothing was "
			"changed."
		)

	if badge_id:
		held = badges.resolve_badge({"badge": badge_id, "company": args.get("company")}).data
		person = str(held["employee"])
		if claimed_employee and claimed_employee != person:
			raise ToolError(
				f"badge {badge_id!r} belongs to {person} ({held['employee_name']}), and the call "
				f"names {claimed_employee!r}. That is either the wrong card or the wrong person, "
				"and neither ends in a working credential. Nothing was changed."
			)
		email = str(frappe.db.get_value(EMPLOYEE, person, "user_id") or "").strip().lower()
		if claimed_user and email and claimed_user != email:
			raise ToolError(
				f"badge {badge_id!r} belongs to {person} ({held['employee_name']}), whose login "
				f"is not {claimed_user!r}. Nothing was changed."
			)
		return (
			person,
			email or claimed_user,
			{
				"method": "badge",
				"badge": badge_id,
				"employee_name": held.get("employee_name"),
				"note": (
					f"The card issued to {held.get('employee_name')} was presented and resolved "
					"through the same register a crew clock reads, so this reset is tied to "
					"possession of something this site issued to one person."
				),
			},
		)

	person = claimed_employee
	if not person and claimed_user:
		person = _employee_for(claimed_user)
	email = claimed_user
	if not email and person:
		if not frappe.db.exists(EMPLOYEE, person):
			raise ToolError(f"no Employee called {person!r} on this site. Nothing was changed.")
		email = str(frappe.db.get_value(EMPLOYEE, person, "user_id") or "").strip().lower()

	name = str(frappe.db.get_value(EMPLOYEE, person, "employee_name") or "") if person else ""
	return (
		person,
		email,
		{
			"method": "manager assertion",
			"badge": None,
			"employee_name": name or None,
			"note": (
				"NO BADGE WAS PRESENTED. This reset rests on the calling manager saying who the "
				"person is, which is a weaker claim than a scanned card and is recorded as such "
				"— on the grant, in this result and in the audit row. Somebody who lost the "
				"phone AND the card is an ordinary Tuesday; pass `badge` when they still have it."
			),
		},
	)
