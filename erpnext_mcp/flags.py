# SPDX-License-Identifier: MIT
"""Feature flags and thresholds as data. v0.206.0.

docs/design/config_flags_triage.md §2. One table, `Farm Feature Flag`, holds
flags (bool), thresholds (number) and text values, company-scoped with optional
role and app-version targeting. Server code and the phone read through here.

RESOLUTION. The most specific active row that applies wins:
company-and-role, then company, then global-and-role, then global. A row whose
app-version range excludes the caller is skipped. NO ROW MEANS THE CALLER'S
DEFAULT — every new behaviour ships dark, and nothing is seeded.

RECORDING. What a flag decided is written on the record it affected
(`stamp`), so a result can always be explained by the flags in force when it
was made.
"""

from __future__ import annotations

import json
import re

import frappe

from . import compat

DOCTYPE = "Farm Feature Flag"
KINDS = ("Flag", "Threshold", "Text")
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
ROW_FIELDS = [
	"name",
	"flag_key",
	"kind",
	"enabled",
	"number_value",
	"text_value",
	"company",
	"roles",
	"min_app_version",
	"max_app_version",
	"description",
	"owner_area",
	"active",
	"modified",
]


def parse_version(raw):
	"""(major, minor, patch) from "1.42" / "1.42.3" / "v1.42 (118)", or None."""
	match = re.match(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(raw or ""))
	if not match:
		return None
	return tuple(int(part or 0) for part in match.groups())


def role_list(raw) -> list:
	if isinstance(raw, (list, tuple)):
		items = raw
	else:
		items = str(raw or "").replace(",", "\n").splitlines()
	return [item.strip() for item in items if str(item).strip()]


def _ready() -> bool:
	try:
		return compat.doctype_exists(DOCTYPE)
	except Exception:
		return False


def rows(key: str = "", company: str = "", include_inactive: bool = True) -> list:
	if not _ready():
		return []
	filters: dict = {}
	if key:
		filters["flag_key"] = key
	if company:
		filters["company"] = company
	if not include_inactive:
		filters["active"] = 1
	return frappe.db.get_all(
		DOCTYPE, filters=filters, fields=ROW_FIELDS, order_by="flag_key asc", limit=100000
	)


def value_of(row: dict):
	kind = row.get("kind") or "Flag"
	if kind == "Threshold":
		number = row.get("number_value")
		return float(number) if number is not None else None
	if kind == "Text":
		return str(row.get("text_value") or "")
	return bool(int(row.get("enabled") or 0))


def _in_range(row: dict, app_version) -> bool:
	lower = parse_version(row.get("min_app_version")) if row.get("min_app_version") else None
	upper = parse_version(row.get("max_app_version")) if row.get("max_app_version") else None
	if not (lower or upper):
		return True
	mine = parse_version(app_version)
	if mine is None:
		# A caller that does not say its version gets only unbounded rows:
		# a targeted row is for phones that can prove they are in range.
		return False
	return not ((lower and mine < lower) or (upper and mine > upper))


def _rank(row: dict, company: str, roles: set):
	"""Specificity, or None when the row does not apply."""
	row_company = str(row.get("company") or "")
	row_roles = set(role_list(row.get("roles")))
	if row_company and row_company != company:
		return None
	if row_roles and not (row_roles & roles):
		return None
	return (2 if row_company else 0) + (1 if row_roles else 0)


def resolve(company: str = "", roles=(), app_version=None, key: str = "") -> dict:
	"""{flag_key: {value, kind, row}} for every key with an applicable row."""
	roles = set(role_list(roles))
	best: dict = {}
	for row in rows(key=key, include_inactive=False):
		if not int(row.get("active") or 0) or not _in_range(row, app_version):
			continue
		rank = _rank(row, company or "", roles)
		if rank is None:
			continue
		current = best.get(row["flag_key"])
		if (
			current is None
			or rank > current[0]
			or (rank == current[0] and str(row["modified"]) > str(current[1]["modified"]))
		):
			best[row["flag_key"]] = (rank, row)
	return {
		k: {"value": value_of(r), "kind": r.get("kind"), "row": r["name"]} for k, (_rank_, r) in best.items()
	}


def value(key: str, company: str = "", roles=(), app_version=None, default=None):
	"""The resolved value of one key, or `default` when no row applies."""
	found = resolve(company, roles, app_version, key=key).get(key)
	return default if found is None else found["value"]


def enabled(key: str, company: str = "", roles=(), app_version=None, default: bool = False) -> bool:
	return bool(value(key, company, roles, app_version, default))


def for_user(user: str = "", company: str = "", app_version=None) -> dict:
	"""{key: value} for a user's roles — what `get_feature_flags` answers."""
	user = user or getattr(frappe.session, "user", "") or ""
	try:
		roles = frappe.get_roles(user) if user else []
	except Exception:
		roles = []
	return {k: v["value"] for k, v in resolve(company, roles, app_version).items()}


def clean(raw) -> dict:
	"""A client's `feature_flags` argument as a small {key: scalar} dict."""
	if isinstance(raw, str):
		try:
			raw = json.loads(raw) if raw.strip() else {}
		except ValueError:
			return {}
	if not isinstance(raw, dict):
		return {}
	out = {}
	for key, item in list(raw.items())[:64]:
		key = str(key).strip()
		if KEY_PATTERN.match(key) and (item is None or isinstance(item, (bool, int, float, str))):
			out[key] = item if not isinstance(item, str) else item[:200]
	return out


def stamp(doc, key: str, item, fieldname: str = "feature_flags") -> None:
	"""Record that `key` was `item` when `doc` was made. Doesn't save."""
	meta = getattr(doc, "meta", None)
	if meta is not None and not meta.has_field(fieldname):
		return
	try:
		current = json.loads(doc.get(fieldname) or "{}")
	except ValueError:
		current = {}
	if not isinstance(current, dict):
		current = {}
	current[str(key)] = item
	doc.set(fieldname, json.dumps(current, sort_keys=True))


def stamp_all(doc, values: dict, fieldname: str = "feature_flags") -> None:
	for key, item in (values or {}).items():
		stamp(doc, key, item, fieldname)


def upsert(
	flag_key: str,
	kind: str,
	item,
	company: str = "",
	roles=(),
	min_app_version: str = "",
	max_app_version: str = "",
	description=None,
	owner_area=None,
	active=None,
):
	"""Create or update the row for (key, company, roles, versions). Returns (doc, created)."""
	roles_text = "\n".join(role_list(roles))
	name = None
	for row in rows(key=flag_key):
		if (
			str(row.get("company") or "") == (company or "")
			and "\n".join(role_list(row.get("roles"))) == roles_text
			and str(row.get("min_app_version") or "") == (min_app_version or "")
			and str(row.get("max_app_version") or "") == (max_app_version or "")
		):
			name = row["name"]
			break
	doc = frappe.get_doc(DOCTYPE, name) if name else frappe.new_doc(DOCTYPE)
	doc.flag_key = flag_key
	doc.kind = kind
	doc.company = company or None
	doc.roles = roles_text
	doc.min_app_version = min_app_version or ""
	doc.max_app_version = max_app_version or ""
	if kind == "Threshold":
		doc.number_value = float(item)
	elif kind == "Text":
		doc.text_value = str(item if item is not None else "")
	else:
		doc.enabled = 1 if _truthy(item) else 0
	if description is not None:
		doc.description = description
	if owner_area is not None:
		doc.owner_area = owner_area
	if active is not None:
		doc.active = 1 if _truthy(active) else 0
	elif not name:
		doc.active = 1
	if name:
		doc.save(ignore_permissions=True)
	else:
		doc.insert(ignore_permissions=True)
	return doc, not name


def _truthy(item) -> bool:
	if isinstance(item, str):
		return item.strip().lower() in ("1", "true", "yes", "on", "enabled")
	return bool(item)
