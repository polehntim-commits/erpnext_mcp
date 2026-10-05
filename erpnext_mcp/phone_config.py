# SPDX-License-Identifier: MIT
"""Phone configuration as versioned, immutable data. v0.207.0.

docs/design/phone_config_and_compliance_loop.md §1. One table — `Farm Config
Version` — holds every version of every Wizard, Tile and Label Profile. A row is
one immutable version of one key:

    Draft → (Staged →) Published → Superseded
                     any → Retired;  Superseded/Retired → Published (rollback)

ONLY A DRAFT'S BODY CAN CHANGE. From Staged on, the controller refuses a body
whose hash differs from the one written at insert, so what a phone was served
under `wizard:near_miss@3` is what `wizard:near_miss@3` still says.

SERVING. A user gets the Staged version when its rollout flag resolves true for
them (Tim first), else the Published one, else nothing. `publish` makes a
version everyone's and switches the rollout flag off; `rollback` is one step.

VALIDATION is per kind (`wizard_config`, `tiles`, `label_compliance`) plus the
checks every kind shares: size, schema version, and — at publish — that the
audience resolves to real people and the English has its Spanish.
"""

from __future__ import annotations

import hashlib
import json
import re

import frappe

from . import compat, flags

DOCTYPE = "Farm Config Version"
#: v0.235.0: "Payroll Setting" — payroll settings as versioned data (`payroll_settings`), never served
#: to a phone and published only by a person.
#: v0.247.0: "Quiz" — a course's knowledge check (`training_quiz`), served with the course, not as a phone config.
KINDS = {"Wizard": "wizard", "Tile": "tile", "Label Profile": "label_profile", "Payroll Setting": "payroll_setting",
         "Quiz": "quiz"}
SLUG_KINDS = {slug: kind for kind, slug in KINDS.items()}
DRAFT, STAGED, PUBLISHED, SUPERSEDED, RETIRED = "Draft", "Staged", "Published", "Superseded", "Retired"
STATUSES = (DRAFT, STAGED, PUBLISHED, SUPERSEDED, RETIRED)
SCHEMA_VERSION = 1
MAX_BODY_BYTES = 64 * 1024
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,59}$")
ROW_FIELDS = [
	"name",
	"config_kind",
	"config_key",
	"version",
	"status",
	"title",
	"body_hash",
	"rollout_flag",
	"notes",
	"authored_by",
	"change_note",
	"staged_by",
	"staged_on",
	"published_by",
	"published_on",
	"retired_by",
	"retired_on",
	"modified",
]


class ConfigError(ValueError):
	"""A refusal a tool turns into a ToolError sentence."""


# ── names, hashes ───────────────────────────────────────────────────────────
def kind_of(raw) -> str:
	text = str(raw or "").strip()
	for kind, slug in KINDS.items():
		if text.lower() in (kind.lower(), slug):
			return kind
	raise ConfigError(f"kind is one of {', '.join(KINDS)}; got {raw!r}.")


def name_of(kind: str, key: str, version) -> str:
	return f"{KINDS[kind]}:{key}@{int(version)}"


def canonical(body) -> str:
	return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def body_hash(body) -> str:
	if isinstance(body, str):
		try:
			body = json.loads(body)
		except ValueError:
			return hashlib.sha256(body.encode()).hexdigest()
	return hashlib.sha256(canonical(body).encode()).hexdigest()


def rollout_flag_key(kind: str, key: str) -> str:
	return f"rollout_{KINDS[kind]}_{key}"[:80]


def parse_body(raw) -> dict:
	if isinstance(raw, str):
		if len(raw.encode()) > MAX_BODY_BYTES:
			raise ConfigError(f"the body is over {MAX_BODY_BYTES // 1024} KB.")
		try:
			raw = json.loads(raw)
		except ValueError as exc:
			raise ConfigError(f"the body is not JSON: {exc}") from exc
	if not isinstance(raw, dict):
		raise ConfigError("the body must be a JSON object.")
	if len(canonical(raw).encode()) > MAX_BODY_BYTES:
		raise ConfigError(f"the body is over {MAX_BODY_BYTES // 1024} KB.")
	return raw


# ── records ─────────────────────────────────────────────────────────────────
def ready() -> bool:
	return compat.doctype_exists(DOCTYPE)


def rows(kind: str = "", key: str = "", status=None) -> list:
	if not ready():
		return []
	filters: dict = {}
	if kind:
		filters["config_kind"] = kind
	if key:
		filters["config_key"] = key
	if status:
		filters["status"] = ("in", list(status)) if isinstance(status, (list, tuple)) else status
	out = frappe.db.get_all(DOCTYPE, filters=filters, fields=ROW_FIELDS, limit=100000)
	return sorted(out, key=lambda r: (r["config_kind"], r["config_key"], -int(r.get("version") or 0)))


def doc_of(kind: str, key: str, version=None, status=None):
	"""One version: `version`, else the newest with `status`. None when absent."""
	if version not in (None, ""):
		name = name_of(kind, key, version)
		return frappe.get_doc(DOCTYPE, name) if frappe.db.exists(DOCTYPE, name) else None
	for row in rows(kind, key, status):
		return frappe.get_doc(DOCTYPE, row["name"])
	return None


def body_of(doc) -> dict:
	try:
		return json.loads(doc.body_json or "{}")
	except ValueError:
		return {}


def version_string(doc) -> str:
	return name_of(doc.config_kind, doc.config_key, doc.version)


def next_version(kind: str, key: str) -> int:
	return max([int(r.get("version") or 0) for r in rows(kind, key)] or [0]) + 1


# ── validation ──────────────────────────────────────────────────────────────
def _validator(kind: str):
	if kind == "Wizard":
		from . import wizard_config

		return wizard_config.validate
	if kind == "Tile":
		from . import tiles

		return tiles.validate
	if kind == "Payroll Setting":
		from . import payroll_settings

		return payroll_settings.validate
	if kind == "Quiz":
		from . import training_quiz

		return training_quiz.validate
	from . import label_compliance

	return label_compliance.validate


def validate(kind: str, key: str, body: dict, *, for_publish: bool = False) -> dict:
	"""`{errors, warnings}` for a body. Never raises."""
	errors: list = []
	warnings: list = []
	if not KEY_PATTERN.match(str(key or "")):
		errors.append(f"key {key!r} must be lower_snake_case, 2–60 characters")
	if body.get("schema_version") != SCHEMA_VERSION:
		errors.append(f"schema_version must be {SCHEMA_VERSION}")
	if body.get("key") not in (None, key):
		errors.append(f"the body's key {body.get('key')!r} is not {key!r}")
	try:
		report = _validator(kind)(body, key=key, for_publish=for_publish)
	except Exception as exc:  # a validator bug must not pass a body
		report = {"errors": [f"the {kind} validator failed: {type(exc).__name__}: {exc}"], "warnings": []}
	errors += report.get("errors") or []
	warnings += report.get("warnings") or []
	return {"errors": errors, "warnings": warnings, "checked_at": frappe.utils.now()}


def spanish_gaps(value, path: str = "") -> list:
	"""Every `{en: …}` in a body with no Spanish beside it."""
	out = []
	if isinstance(value, dict):
		if "en" in value and set(value) <= {"en", "es"}:
			if str(value.get("en") or "").strip() and not str(value.get("es") or "").strip():
				out.append(path or "(root)")
			return out
		for child, item in value.items():
			if child in ("formula", "show_if", "required_if", "if", "when", "link", "scan"):
				continue
			out += spanish_gaps(item, f"{path}.{child}" if path else child)
	elif isinstance(value, list):
		for index, item in enumerate(value):
			out += spanish_gaps(item, f"{path}[{index}]")
	return out


def language_findings(body: dict, for_publish: bool) -> tuple:
	"""(errors, warnings) for missing Spanish. An error at publish unless allow_english_only."""
	gaps = spanish_gaps(body)
	if not gaps:
		return [], []
	sentence = f"no Spanish for {len(gaps)} string(s): {', '.join(gaps[:8])}" + ("…" if len(gaps) > 8 else "")
	if for_publish and not body.get("allow_english_only"):
		return [sentence + " — add `es`, or set allow_english_only: true"], []
	return [], [sentence]


# ── audience: who a body reaches ────────────────────────────────────────────
def _person(user: str) -> dict:
	"""{user, employee, companies, roles, skills} for one user with a live grant."""
	from .api import guard

	try:
		companies = guard.accessible_companies(user)
	except Exception:
		companies = []
	employee = ""
	skills: list = []
	if compat.doctype_exists("Employee"):
		employee = frappe.db.get_value("Employee", {"user_id": user}, "name") or ""
		if employee:
			from .tools import fieldwork

			field = compat.first_field("Employee", *fieldwork._SKILL_FIELDS)
			if field:
				listed = str(frappe.db.get_value("Employee", employee, field) or "")
				skills = [s.strip() for s in re.split(r"[,\n;]", listed) if s.strip()]
	try:
		roles = list(frappe.get_roles(user) or [])
	except Exception:
		roles = []
	return {"user": user, "employee": employee, "companies": companies, "roles": roles, "skills": skills}


def mobile_people(company: str = "") -> list:
	"""Every active user with a live Mobile Access Grant: {user, employee, companies, roles, skills}."""
	out = []
	if not compat.doctype_exists("Mobile Access Grant"):
		return out
	for grant in frappe.db.get_all(
		"Mobile Access Grant", filters={"state": "Active"}, fields=["user"], limit=5000
	):
		user = grant.get("user")
		if not user:
			continue
		person = _person(user)
		if company and company not in person["companies"]:
			continue
		out.append(person)
	return out


def person_of(user: str) -> dict:
	"""One user, as `mobile_people` would describe them.

	v0.230.3. IT ASKS FOR THE ONE GRANT. It used to build `mobile_people()` — 3–4
	queries for EVERY phone user on the farm — and then pick one out; `get_tiles`
	calls it per badge tile, two or three times per app launch, on a Pi.
	"""
	if user and compat.doctype_exists("Mobile Access Grant") and frappe.db.exists(
		"Mobile Access Grant", {"user": user, "state": "Active"}
	):
		return _person(user)
	try:
		roles = list(frappe.get_roles(user) or [])
	except Exception:
		roles = []
	employee = (
		frappe.db.get_value("Employee", {"user_id": user}, "name")
		if compat.doctype_exists("Employee")
		else ""
	)
	return {"user": user, "employee": employee or "", "companies": [], "roles": roles, "skills": []}


def matches(person: dict, audience: dict | None) -> bool:
	"""Every non-empty axis must match (AND); within an axis any value (OR)."""
	audience = audience or {}
	checks = (
		("users", lambda wanted: person["user"] in wanted),
		("roles", lambda wanted: bool(set(wanted) & set(person.get("roles") or []))),
		("companies", lambda wanted: bool(set(wanted) & set(person.get("companies") or []))),
		(
			"skills",
			lambda wanted: bool(
				{s.casefold() for s in wanted} & {s.casefold() for s in person.get("skills") or []}
			),
		),
		("certifications", lambda wanted: _certified(person, wanted)),
	)
	for axis, test in checks:
		wanted = [str(v).strip() for v in audience.get(axis) or [] if str(v).strip()]
		if wanted and not test(wanted):
			return False
	return True


def _certified(person: dict, wanted: list) -> bool:
	from . import qualifications

	return bool(person.get("employee")) and any(
		qualifications.qualification(person["employee"], cert) for cert in wanted
	)


def audience_people(audience: dict | None, company: str = "") -> list:
	return [person for person in mobile_people(company) if matches(person, audience)]


def audience_problems(audience) -> list:
	if audience is None:
		return []
	if not isinstance(audience, dict):
		return ["audience must be an object of roles, companies, skills, certifications, users"]
	out = []
	for axis, values in audience.items():
		if axis not in ("roles", "companies", "skills", "certifications", "users"):
			out.append(f"audience.{axis} is not an audience axis")
		elif not isinstance(values, list):
			out.append(f"audience.{axis} must be a list")
	if not out and not audience_people(audience):
		out.append("the audience resolves to nobody: no active mobile user matches every axis")
	return out


# ── lifecycle ───────────────────────────────────────────────────────────────
def _stamp(doc, what: str, actor: str, change_note: str) -> None:
	doc.set(f"{what}_by", actor)
	doc.set(f"{what}_on", frappe.utils.now())
	doc.change_note = change_note


def save_draft(kind: str, key: str, body, notes: str, authored_by: str = "Operator", *, create_only=False):
	"""Create a Draft (version 1 or max+1) or replace the open Draft's body. Refuses on errors."""
	body = parse_body(body)
	body.setdefault("key", key)
	body.setdefault("schema_version", SCHEMA_VERSION)
	report = validate(kind, key, body)
	if report["errors"]:
		raise ConfigError("the body was not saved:\n- " + "\n- ".join(report["errors"]))
	existing = rows(kind, key)
	if create_only and existing:
		raise ConfigError(
			f"{KINDS[kind]} {key!r} already exists; use update_{KINDS[kind]} for a new version."
		)
	draft = next((r for r in existing if r["status"] == DRAFT), None)
	if draft:
		doc = frappe.get_doc(DOCTYPE, draft["name"])
	else:
		doc = frappe.new_doc(DOCTYPE)
		doc.config_kind = kind
		doc.config_key = key
		doc.version = next_version(kind, key)
		doc.status = DRAFT
	doc.schema_version = SCHEMA_VERSION
	doc.body_json = json.dumps(body, indent=1, ensure_ascii=False, sort_keys=True)
	doc.body_hash = body_hash(body)
	doc.title = _title(body) or key
	doc.validation_json = json.dumps(report)
	doc.notes = notes
	doc.authored_by = authored_by
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True) if draft else doc.insert(ignore_permissions=True)
	return doc, report


def _title(body: dict) -> str:
	title = body.get("title")
	return str(title.get("en") if isinstance(title, dict) else title or "")[:140]


def _publishable(doc) -> dict:
	report = validate(doc.config_kind, doc.config_key, body_of(doc), for_publish=True)
	doc.validation_json = json.dumps(report)
	if report["errors"]:
		raise ConfigError(f"{version_string(doc)} cannot go live:\n- " + "\n- ".join(report["errors"]))
	return report


def stage(kind, key, version, change_note, actor, users=(), roles=(), companies=()):
	doc = _require(kind, key, version)
	if doc.status != DRAFT:
		raise ConfigError(f"{version_string(doc)} is {doc.status}; only a Draft is staged.")
	if not (users or roles or companies):
		raise ConfigError("name who sees it first: users, roles or companies (e.g. users=[tim@…]).")
	_publishable(doc)
	for other in rows(kind, key, STAGED):
		_move(frappe.get_doc(DOCTYPE, other["name"]), SUPERSEDED, actor, change_note, None)
	flag_key = rollout_flag_key(kind, key)
	for company in companies or [""]:
		flags.upsert(
			flag_key,
			"Flag",
			True,
			company=company,
			roles=roles,
			users=users,
			description=f"Staged rollout of {version_string(doc)}.",
			owner_area="phone_config",
			active=True,
		)
	doc.status = STAGED
	doc.rollout_flag = flag_key
	_stamp(doc, "staged", actor, change_note)
	_save(doc)
	return doc


def publish(kind, key, version, change_note, actor):
	doc = _require(kind, key, version)
	if doc.status == PUBLISHED:
		return doc, "", True
	_publishable(doc)
	previous = ""
	for row in rows(kind, key, (PUBLISHED, STAGED)):
		if row["name"] == doc.name:
			continue
		other = frappe.get_doc(DOCTYPE, row["name"])
		if other.status == PUBLISHED:
			previous = other.name
		_move(other, SUPERSEDED, actor, change_note, None)
	_switch_off_rollout(kind, key)
	doc.status = PUBLISHED
	_stamp(doc, "published", actor, change_note)
	_save(doc)
	return doc, previous, False


def rollback(kind, key, change_note, actor):
	current = doc_of(kind, key, status=PUBLISHED)
	if current is None:
		raise ConfigError(f"{KINDS[kind]} {key!r} has no Published version to roll back.")
	earlier = [r for r in rows(kind, key, SUPERSEDED) if int(r["version"]) < int(current.version)]
	if not earlier:
		raise ConfigError(f"{version_string(current)} has no earlier version to go back to.")
	target = frappe.get_doc(DOCTYPE, earlier[0]["name"])
	_move(current, SUPERSEDED, actor, change_note, None)
	for row in rows(kind, key, STAGED):
		_move(frappe.get_doc(DOCTYPE, row["name"]), SUPERSEDED, actor, change_note, None)
	_switch_off_rollout(kind, key)
	target.status = PUBLISHED
	_stamp(target, "published", actor, change_note)
	_save(target)
	return target, current.name


def retire(kind, key, change_note, actor):
	moved = []
	for row in rows(kind, key, (DRAFT, STAGED, PUBLISHED)):
		doc = frappe.get_doc(DOCTYPE, row["name"])
		_move(doc, RETIRED, actor, change_note, "retired")
		moved.append(doc.name)
	_switch_off_rollout(kind, key)
	if not moved and not rows(kind, key):
		raise ConfigError(f"no {KINDS[kind]} called {key!r}.")
	return moved


def _require(kind, key, version):
	doc = doc_of(kind, key, version)
	if doc is None:
		raise ConfigError(f"no {name_of(kind, key, version or 0)}; list_phone_configs has the versions.")
	return doc


def _move(doc, status, actor, change_note, stamp) -> None:
	doc.status = status
	if stamp:
		_stamp(doc, stamp, actor, change_note)
	else:
		doc.change_note = change_note
	_save(doc)


def _save(doc) -> None:
	doc.flags.ignore_permissions = True
	doc.flags.lifecycle = True
	doc.save(ignore_permissions=True)


def _switch_off_rollout(kind, key) -> None:
	for row in flags.rows(key=rollout_flag_key(kind, key)):
		if int(row.get("active") or 0):
			frappe.db.set_value(flags.DOCTYPE, row["name"], "active", 0)


# ── serving ─────────────────────────────────────────────────────────────────
def in_force(kind: str, key: str, user: str = "", company: str = "", roles=(), app_version=None):
	"""(doc, body) a user is served for one key: Staged for the rollout, else Published."""
	if not ready():
		return None, None
	staged = doc_of(kind, key, status=STAGED)
	if staged is not None and flags.value(
		rollout_flag_key(kind, key), company, roles, app_version, default=False, user=user
	):
		return staged, body_of(staged)
	published = doc_of(kind, key, status=PUBLISHED)
	return (published, body_of(published)) if published is not None else (None, None)


def served(kind: str, user: str = "", company: str = "", roles=(), app_version=None) -> list:
	"""[(doc, body)] for every live key of a kind, as this user is served them."""
	keys = sorted({r["config_key"] for r in rows(kind, status=(STAGED, PUBLISHED))})
	out = []
	for key in keys:
		doc, body = in_force(kind, key, user, company, roles, app_version)
		if doc is not None:
			out.append((doc, body))
	return out


def describe(doc, include_body: bool = False) -> dict:
	row = {
		"name": doc.name,
		"kind": doc.config_kind,
		"key": doc.config_key,
		"version": doc.version,
		"config_version": version_string(doc),
		"status": doc.status,
		"title": doc.get("title"),
		"rollout_flag": doc.get("rollout_flag") or None,
		"notes": doc.get("notes"),
		"authored_by": doc.get("authored_by"),
		"change_note": doc.get("change_note") or None,
		"staged_by": doc.get("staged_by") or None,
		"published_by": doc.get("published_by") or None,
		"published_on": str(doc.get("published_on") or "") or None,
		"retired_by": doc.get("retired_by") or None,
		"body_hash": doc.get("body_hash"),
	}
	try:
		row["validation"] = json.loads(doc.get("validation_json") or "null")
	except ValueError:
		row["validation"] = None
	if include_body:
		row["body"] = body_of(doc)
	return row


def seed(kind: str, key: str, body: dict, notes: str) -> str:
	"""Create-only: version 1 Published where the key has no row. Skips validation of the
	audience (a fresh site has no phones yet) but not of the body's shape."""
	if not ready() or rows(kind, key):
		return ""
	body = dict(body)
	body.setdefault("key", key)
	body.setdefault("schema_version", SCHEMA_VERSION)
	doc = frappe.new_doc(DOCTYPE)
	doc.config_kind = kind
	doc.config_key = key
	doc.version = 1
	doc.status = PUBLISHED
	doc.schema_version = SCHEMA_VERSION
	doc.body_json = json.dumps(body, indent=1, ensure_ascii=False, sort_keys=True)
	doc.body_hash = body_hash(body)
	doc.title = _title(body) or key
	doc.notes = notes
	doc.authored_by = "System"
	doc.published_by = "Administrator"
	doc.published_on = frappe.utils.now()
	doc.change_note = "Seeded at install."
	doc.validation_json = json.dumps(
		{"errors": [], "warnings": [], "checked_at": frappe.utils.now(), "seeded": True}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc.name
