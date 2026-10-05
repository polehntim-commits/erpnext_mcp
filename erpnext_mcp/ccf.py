# SPDX-License-Identifier: MIT
"""The CCF generic evaluator. v0.233.0. docs/design/ccf_core_work_timing.md §2.

THE BOUNDARY (Tim, 2026-10-04): rules, thresholds, windows and messages are DATA;
code is written only to add a context provider, an action, an evaluation moment or
an operator. This module is that one-time code.

A rule carrying `condition_tree_json` runs here instead of the primitive scan; the
14 existing rules carry none and run exactly as before (decision 2).

THE TREE
    node   := {"all": [node, …]} | {"any": [node, …]} | {"not": node} | leaf
    leaf   := {"path": "<provider>.<dotted path>", "op": <op>, "value" | "value_source": …, "agg"?: …}
    every node may carry: id, reason {en, es}, basis (published | local_judgment), source

`path` names a value a PROVIDER publishes (`PROVIDERS`); nothing else is reachable —
no eval, no attribute walk. A path over a series takes an index window,
`weather.forecast.daily[0..6].precip_in`, and then needs an aggregate
(max min sum count_where any all). Missing data FAILS the leaf and says so: a
check that cannot be made is not a check that passed.

The result explains itself: every failing node with an `id` is reported with the
actual values its leaves saw, and the Hold sentence is built from their reasons.
"""

from __future__ import annotations

import datetime
import json
import re

import frappe

from . import compat

# ── limits ──────────────────────────────────────────────────────────────────
MAX_DEPTH = 8
MAX_NODES = 120

OPS = ("gte", "gt", "lte", "lt", "eq", "ne", "between", "in", "nin", "isnull", "isnotnull", "istrue", "isfalse")
NULLARY = ("isnull", "isnotnull", "istrue", "isfalse")
ORDERED = ("gte", "gt", "lte", "lt", "between")
AGGS = ("max", "min", "sum", "count_where", "any", "all")
BASES = ("published", "local_judgment")

#: When a rule is evaluated (§2.3). `sweep` is the existing nightly/hourly sweep;
#: the others are wired as the features that need them land.
MOMENTS = ("sweep", "day_start", "task_start", "evening_cutoff", "forecast_refresh")

#: The action vocabulary (§2.4). An alert is always raised; the rest are validated
#: and stored now and run as each lands — `describe` says which are live.
ACTIONS = (
	"alert",
	"create_farm_task",
	"add_to_audit_packet",
	"set_go_hold",
	"block_start",
	"require_override_reason",
	"require_checklist_item",
	"notify_workers",
	"notify_supervisor",
	"shift_dates",
)
LIVE_ACTIONS = ("alert", "set_go_hold", "block_start")

_SEGMENT = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\[(?P<a>\d+)(?:\.\.(?P<b>\d+))?\])?$")


class TreeError(ValueError):
	"""A tree that cannot be evaluated. Raised at create/propose/update, never in the sweep."""


# ── providers ───────────────────────────────────────────────────────────────
class Provider:
	"""A source of values. `paths` maps a normalised path (series marked `[]`) to
	`{type, unit, description, example}`; `"*"` means any field of the subject row."""

	def __init__(self, name: str, paths: dict, resolve, past: bool = False, description: str = ""):
		self.name = name
		self.paths = paths
		self.resolve = resolve
		self.past = past
		self.description = description


def _p(type_: str, description: str, unit: str = "", example=None) -> dict:
	return {"type": type_, "unit": unit, "description": description, "example": example}


def _calendar(subject: dict, ctx: dict) -> dict:
	day = ctx["as_of_date"]
	return {
		"date": day.isoformat(),
		"mmdd": day.strftime("%m-%d"),
		"day_of_year": day.timetuple().tm_yday,
		"weekday": day.strftime("%a"),
		"month": day.month,
	}


def _record(subject: dict, ctx: dict) -> dict:
	return dict(subject or {})


def _first(row: dict, *fields):
	for field in fields:
		if row.get(field) not in (None, ""):
			return row.get(field)
	return None


def _task(subject: dict, ctx: dict) -> dict:
	row = subject if ctx.get("doctype") == "Farm Task" else None
	if row is None:
		name = _first(subject, "source_task", "farm_task", "task")
		if name and compat.doctype_exists("Farm Task") and frappe.db.exists("Farm Task", name):
			row = frappe.db.get_value(
				"Farm Task",
				name,
				compat.existing_fields(
					"Farm Task",
					("name", "state", "template", "location", "location_doctype", "assigned_to", "urgency",
					 "skill_required", "task_type", "start_date", "due_date"),
				),
				as_dict=True,
			)
	row = dict(row or {})
	return {
		"state": row.get("state"),
		"template": row.get("template"),
		"location": row.get("location"),
		"location_doctype": row.get("location_doctype"),
		"assignee": row.get("assigned_to"),
		"urgency": row.get("urgency"),
		"skill_required": row.get("skill_required"),
		"task_type": row.get("task_type"),
		"start_date": str(row.get("start_date") or "") or None,
		"due_date": str(row.get("due_date") or "") or None,
	}


def _asset(subject: dict, ctx: dict) -> dict:
	name = subject.get("name") if ctx.get("doctype") == "Asset Register" else _first(subject, "asset", "sprayer")
	if not name or not compat.doctype_exists("Asset Register") or not frappe.db.exists("Asset Register", name):
		return {"type": None, "engine_hours": None, "status": None, "last_service": None}
	row = frappe.db.get_value(
		"Asset Register",
		name,
		compat.existing_fields(
			"Asset Register", ("asset_type", "engine_hours", "status", "last_service_date", "last_service_hours")
		),
		as_dict=True,
	) or {}
	return {
		"type": row.get("asset_type"),
		"engine_hours": _number_or_none(row.get("engine_hours")),
		"status": row.get("status"),
		"last_service": str(row.get("last_service_date") or "") or None,
	}


def _settings(subject: dict, ctx: dict) -> dict:
	from . import compliance_rules

	company = ctx.get("company") or ""
	weather = {}
	for key in compliance_rules.THRESHOLD_SOURCES:
		namespace, _, field = key.partition(".")
		if namespace == "weather":
			weather[field] = compliance_rules.threshold_from_source(key, company)
	return {"weather": weather}


PROVIDERS: dict = {}


def register(provider: Provider) -> Provider:
	PROVIDERS[provider.name] = provider
	return provider


register(
	Provider(
		"calendar",
		{
			"date": _p("date", "The day being judged (today, or tomorrow for an evening run).", example="2026-01-15"),
			"mmdd": _p("mmdd", "Month and day, for seasonal windows.", example="01-15"),
			"day_of_year": _p("number", "1–366.", example=15),
			"weekday": _p("string", "Mon … Sun.", example="Thu"),
			"month": _p("number", "1–12.", example=1),
		},
		_calendar,
		past=True,
		description="The clock, in the site's time zone.",
	)
)
register(
	Provider(
		"record",
		{"*": _p("any", "Any field of the record the rule walks (its target_doctype).", example="Open")},
		_record,
		past=False,
		description="The row itself.",
	)
)
register(
	Provider(
		"task",
		{
			"state": _p("string", "Farm Task state.", example="Available"),
			"template": _p("string", "Farm Task Template.", example="Dormant pruning"),
			"location": _p("string", "Where the task is.", example="Block 7"),
			"location_doctype": _p("string", "Field, Housing Unit, …", example="Field"),
			"assignee": _p("string", "Employee holding it.", example="HR-EMP-00012"),
			"urgency": _p("string", "Low / Normal / High / Critical.", example="Normal"),
			"skill_required": _p("string", "Skill pool.", example="Pruning"),
			"task_type": _p("string", "Farm Task type.", example="Pruning"),
			"start_date": _p("date", "Planned start.", example="2026-01-12"),
			"due_date": _p("date", "Due.", example="2026-01-30"),
		},
		_task,
		description="The Farm Task — the row itself when the rule walks Farm Task, else its source_task.",
	)
)
register(
	Provider(
		"asset",
		{
			"type": _p("string", "Asset type.", example="Tractor"),
			"engine_hours": _p("number", "Engine hours.", unit="h", example=1240.5),
			"status": _p("string", "Asset status.", example="In Use"),
			"last_service": _p("date", "Last service date.", example="2026-08-01"),
		},
		_asset,
		description="The Asset Register row — the row itself, or its asset / sprayer field.",
	)
)
register(
	Provider(
		"settings",
		{
			"weather.heat_threshold_temp_f": _p("number", "Weather Settings heat threshold, per company.", "°F", 80),
			"weather.heat_threshold_heat_index_f": _p("number", "Heat-index threshold, per company.", "°F", 80),
			"weather.wind_threshold_mph_spray_block": _p("number", "Spray-block wind limit, per company.", "mph", 10),
		},
		_settings,
		past=True,
		description="Farm-wide limits from Weather Settings (Desk only), per company.",
	)
)


# ── paths ───────────────────────────────────────────────────────────────────
def split_path(path: str) -> tuple:
	"""(provider, [(name, a, b)], normalised) — `b` is None for a single index, `a` None for none."""
	text = str(path or "").strip()
	if "." not in text:
		raise TreeError(f"path {text!r} must be <provider>.<field>, e.g. calendar.mmdd.")
	provider, _, rest = text.partition(".")
	segments, normal = [], []
	# Split on dots OUTSIDE brackets: `daily[0..6].precip_in` is two segments.
	for raw in re.findall(r"[^.\[\]]+(?:\[[^\]]*\])?", rest):
		match = _SEGMENT.match(raw)
		if not match:
			raise TreeError(f"path {text!r}: {raw!r} is not a field name or field[a..b].")
		a = match.group("a")
		b = match.group("b")
		segments.append((match.group("name"), int(a) if a is not None else None, int(b) if b is not None else None))
		normal.append(match.group("name") + ("[]" if a is not None else ""))
	return provider, segments, ".".join(normal)


def describe_path(path: str) -> dict:
	provider_name, segments, normal = split_path(path)
	provider = PROVIDERS.get(provider_name)
	if provider is None:
		raise TreeError(f"path {path!r}: no provider {provider_name!r}. Known: {', '.join(sorted(PROVIDERS))}.")
	spec = provider.paths.get(normal) or (provider.paths.get("*") if len(segments) == 1 else None)
	if spec is None:
		raise TreeError(
			f"path {path!r}: {provider_name} does not publish {normal!r}. get_compliance_field_map lists every "
			"path under `context`."
		)
	series = any(a is not None and b is not None for _, a, b in segments)
	return {"provider": provider_name, "spec": spec, "series": series}


def _walk(value, segments):
	"""Resolve segments over nested dicts/lists. A range yields a list (mapped onward)."""
	current = value
	mapped = False
	for name, a, b in segments:
		if mapped:
			current = [item.get(name) if isinstance(item, dict) else None for item in current or []]
		else:
			current = current.get(name) if isinstance(current, dict) else None
		if a is not None:
			if not isinstance(current, list):
				return None
			if b is None:
				current = current[a] if a < len(current) else None
			else:
				current = current[a : b + 1]
				mapped = True
	return current


# ── parsing / validation ────────────────────────────────────────────────────
def parse_tree(raw, label: str = "condition_tree") -> dict:
	"""The tree as a dict, validated. `{}` for none. Raises ValueError."""
	if raw in (None, "", {}, "{}"):
		return {}
	tree = raw
	if isinstance(raw, str):
		try:
			tree = json.loads(raw)
		except ValueError as exc:
			raise TreeError(f"{label} is not JSON: {exc}.") from None
	if not isinstance(tree, dict):
		raise TreeError(f"{label} must be an object (a node).")
	count = [0]
	_validate(tree, label, 1, count, set())
	return tree


def _validate(node, where: str, depth: int, count: list, ids: set) -> None:
	count[0] += 1
	if count[0] > MAX_NODES:
		raise TreeError(f"the tree has more than {MAX_NODES} nodes.")
	if depth > MAX_DEPTH:
		raise TreeError(f"{where}: the tree is deeper than {MAX_DEPTH}.")
	if not isinstance(node, dict):
		raise TreeError(f"{where}: every node is an object.")
	node_id = node.get("id")
	if node_id is not None:
		if not isinstance(node_id, str) or not node_id.strip():
			raise TreeError(f"{where}: id must be a non-empty string.")
		if node_id in ids:
			raise TreeError(f"{where}: id {node_id!r} is used twice.")
		ids.add(node_id)
	reason = node.get("reason")
	if reason is not None and not (isinstance(reason, str) or (isinstance(reason, dict) and set(reason) <= {"en", "es"})):
		raise TreeError(f"{where}: reason is a string or {{en, es}}.")
	if node.get("basis") is not None and node.get("basis") not in BASES:
		raise TreeError(f"{where}: basis is {' or '.join(BASES)}.")
	groups = [key for key in ("all", "any", "not") if key in node]
	if len(groups) > 1:
		raise TreeError(f"{where}: a node is one of all / any / not / a leaf.")
	if groups:
		key = groups[0]
		if "path" in node:
			raise TreeError(f"{where}: a group node has no path.")
		if key == "not":
			_validate(node["not"], f"{where}.not", depth + 1, count, ids)
			return
		children = node[key]
		if not isinstance(children, list) or not children:
			raise TreeError(f"{where}.{key} must be a non-empty list.")
		for index, child in enumerate(children):
			_validate(child, f"{where}.{key}[{index}]", depth + 1, count, ids)
		return
	_validate_leaf(node, where)


def _validate_leaf(leaf: dict, where: str) -> None:
	if "path" not in leaf:
		raise TreeError(f"{where}: a leaf needs a path (or the node needs all / any / not).")
	op = leaf.get("op")
	if op not in OPS:
		raise TreeError(f"{where}: op {op!r} is not one of {', '.join(OPS)}.")
	described = describe_path(leaf["path"])
	type_ = described["spec"]["type"]
	agg = leaf.get("agg")
	if described["series"]:
		if agg not in AGGS:
			raise TreeError(f"{where}: {leaf['path']} is a series and needs agg ({', '.join(AGGS)}).")
	elif agg is not None:
		raise TreeError(f"{where}: agg applies only to a path with an index window [a..b].")
	if op in NULLARY:
		if "value" in leaf or "value_source" in leaf:
			raise TreeError(f"{where}: {op} takes no value.")
		return
	if ("value" in leaf) == ("value_source" in leaf):
		raise TreeError(f"{where}: give exactly one of value or value_source.")
	if "value_source" in leaf:
		source = describe_path(leaf["value_source"])
		if source["series"]:
			raise TreeError(f"{where}: value_source must be a single value.")
		return
	value = leaf["value"]
	if op == "between":
		if not (isinstance(value, list) and len(value) == 2):
			raise TreeError(f"{where}: between takes [low, high].")
	elif op in ("in", "nin"):
		if not isinstance(value, list):
			raise TreeError(f"{where}: {op} takes a list.")
	if op in ORDERED and type_ in ("string", "bool") and agg not in ("count_where",):
		raise TreeError(f"{where}: {op} needs an ordered value; {leaf['path']} is {type_}.")
	if type_ == "number" and op in ORDERED and agg != "count_where":
		for item in value if op == "between" else [value]:
			if not isinstance(item, (int, float)) or isinstance(item, bool):
				raise TreeError(f"{where}: {leaf['path']} is a number; the value must be one.")
	if type_ == "mmdd" and op in ORDERED:
		for item in value if op == "between" else [value]:
			if not (isinstance(item, str) and re.fullmatch(r"\d{2}-\d{2}", item)):
				raise TreeError(f"{where}: {leaf['path']} compares as MM-DD, e.g. \"03-15\".")


def parse_evaluation(raw, label: str = "evaluation") -> dict:
	if raw in (None, "", {}, "{}"):
		return {}
	value = json.loads(raw) if isinstance(raw, str) else raw
	if not isinstance(value, dict):
		raise TreeError(f"{label} must be an object.")
	unknown = set(value) - {"raise_when", "when", "as_of", "for_each", "target"}
	if unknown:
		raise TreeError(f"{label}: unknown key(s) {', '.join(sorted(unknown))}.")
	if value.get("raise_when", "fail") not in ("fail", "pass"):
		raise TreeError(f"{label}.raise_when is fail (default — a Hold) or pass.")
	if value.get("as_of", "today") not in ("today", "tomorrow"):
		raise TreeError(f"{label}.as_of is today or tomorrow.")
	moments = value.get("when") or []
	if not isinstance(moments, list) or any(m not in MOMENTS for m in moments):
		raise TreeError(f"{label}.when is a list from: {', '.join(MOMENTS)}.")
	return value


def parse_actions(raw, label: str = "actions") -> list:
	if raw in (None, "", [], "[]"):
		return []
	value = json.loads(raw) if isinstance(raw, str) else raw
	if not isinstance(value, list):
		raise TreeError(f"{label} must be a list of {{type, …}}.")
	for index, action in enumerate(value):
		if not isinstance(action, dict) or action.get("type") not in ACTIONS:
			raise TreeError(f"{label}[{index}].type is one of: {', '.join(ACTIONS)}.")
	return value


# ── evaluation ──────────────────────────────────────────────────────────────
def providers_in(tree: dict) -> set:
	found: set = set()

	def walk(node):
		if not isinstance(node, dict):
			return
		for key in ("all", "any"):
			for child in node.get(key) or []:
				walk(child)
		if "not" in node:
			walk(node["not"])
		for key in ("path", "value_source"):
			if node.get(key):
				found.add(str(node[key]).split(".", 1)[0])

	walk(tree)
	return found


def build_context(subject: dict, *, doctype: str = "", company: str = "", as_of: str = "", providers=None) -> dict:
	"""The values a tree reads, for one subject. Only the providers named are resolved."""
	day = _as_date(as_of) or _as_date(frappe.utils.today())
	ctx = {"doctype": doctype, "company": company, "as_of_date": day}
	values = {}
	for name in providers or PROVIDERS:
		provider = PROVIDERS.get(name)
		if provider is None:
			continue
		try:
			values[name] = provider.resolve(subject or {}, ctx)
		except Exception:
			values[name] = {}
	return values


def resolve(values: dict, path: str):
	provider, segments, _ = split_path(path)
	return _walk(values.get(provider) or {}, segments)


def evaluate(tree: dict, values: dict, language: str = "en") -> dict:
	"""`{passed, failures: [...], hold: "…"}` — failures are the outermost failing nodes with an id."""
	if not tree:
		return {"passed": True, "failures": [], "hold": ""}
	passed, failures = _eval(tree, values, language)
	return {"passed": passed, "failures": failures, "hold": _hold_text(failures, language)}


def _eval(node: dict, values: dict, language: str) -> tuple:
	if "all" in node or "any" in node:
		key = "all" if "all" in node else "any"
		results = [_eval(child, values, language) for child in node[key]]
		passed = all(r[0] for r in results) if key == "all" else any(r[0] for r in results)
		inner = [f for ok, fs in results if not ok for f in fs]
		return passed, ([] if passed else _wrap(node, inner, language))
	if "not" in node:
		ok, _inner = _eval(node["not"], values, language)
		passed = not ok
		seen = _leaves_seen(node["not"], values)
		return passed, ([] if passed else _wrap(node, seen, language))
	return _leaf(node, values, language)


def _wrap(node: dict, inner: list, language: str) -> list:
	if node.get("id"):
		return [
			{
				"id": node["id"],
				"reason": _reason(node, language),
				"basis": node.get("basis"),
				"source": node.get("source"),
				"leaves": inner,
				"missing": any(f.get("missing") for f in inner),
			}
		]
	return inner


def _leaves_seen(node: dict, values: dict) -> list:
	out = []
	for key in ("all", "any"):
		for child in node.get(key) or []:
			out.extend(_leaves_seen(child, values))
	if "not" in node:
		out.extend(_leaves_seen(node["not"], values))
	if "path" in node:
		out.append({"path": node["path"], "op": node.get("op"), "value": node.get("value"),
		            "actual": _actual(node, values)})
	return out


def _actual(leaf: dict, values: dict):
	raw = resolve(values, leaf["path"])
	if leaf.get("agg") and isinstance(raw, list):
		return _aggregate(raw, leaf["agg"], leaf)
	return raw


def _leaf(leaf: dict, values: dict, language: str) -> tuple:
	op = leaf["op"]
	raw = resolve(values, leaf["path"])
	threshold = leaf.get("value")
	if "value_source" in leaf:
		threshold = resolve(values, leaf["value_source"])
	agg = leaf.get("agg")
	if agg:
		series = raw if isinstance(raw, list) else []
		present = [v for v in series if v is not None]
		if not present:
			return False, [_failure(leaf, None, threshold, language, missing=True)]
		if agg == "count_where":
			actual = sum(1 for v in present if _compare(v, leaf.get("where_op", "eq"), leaf.get("where", True)))
			ok = _compare(actual, op, threshold)
		elif agg in ("any", "all"):
			hits = [_compare(v, op, threshold) for v in present]
			ok = any(hits) if agg == "any" else all(hits)
			actual = present
		else:
			actual = _aggregate(present, agg, leaf)
			ok = _compare(actual, op, threshold)
		return ok, ([] if ok else [_failure(leaf, actual, threshold, language)])
	if op in ("isnull", "isnotnull"):
		ok = (raw in (None, "")) == (op == "isnull")
		return ok, ([] if ok else [_failure(leaf, raw, None, language)])
	if raw in (None, "") or (threshold is None and op not in NULLARY):
		return False, [_failure(leaf, raw, threshold, language, missing=True)]
	ok = _compare(raw, op, threshold)
	return ok, ([] if ok else [_failure(leaf, raw, threshold, language)])


def _aggregate(values: list, agg: str, leaf: dict):
	numbers = [_number_or_none(v) for v in values if v is not None]
	numbers = [n for n in numbers if n is not None]
	if not numbers:
		return None
	if agg == "max":
		return max(numbers)
	if agg == "min":
		return min(numbers)
	if agg == "sum":
		return round(sum(numbers), 4)
	return None


def _compare(actual, op: str, wanted) -> bool:
	if op == "istrue":
		return compat.checked(actual)
	if op == "isfalse":
		return not compat.checked(actual)
	if op in ("in", "nin"):
		hit = actual in (wanted or []) or str(actual) in [str(w) for w in wanted or []]
		return hit if op == "in" else not hit
	left, right = _comparable(actual, wanted[0] if op == "between" else wanted)
	if op == "between":
		_, high = _comparable(actual, wanted[1])
		return left is not None and right is not None and high is not None and right <= left <= high
	if left is None or right is None:
		return False
	if op == "eq":
		return left == right
	if op == "ne":
		return left != right
	if op == "gte":
		return left >= right
	if op == "gt":
		return left > right
	if op == "lte":
		return left <= right
	if op == "lt":
		return left < right
	return False


def _comparable(left, right):
	"""Both as numbers when both read as numbers, else both as strings (dates, MM-DD, codes)."""
	if isinstance(left, bool) or isinstance(right, bool):
		return bool(compat.checked(left)), bool(compat.checked(right))
	a, b = _number_or_none(left), _number_or_none(right)
	if a is not None and b is not None:
		return a, b
	if left is None or right is None:
		return None, None
	return str(left), str(right)


def _number_or_none(value):
	if value in (None, "") or isinstance(value, bool):
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def _failure(leaf: dict, actual, threshold, language: str, missing: bool = False) -> dict:
	return {
		"id": leaf.get("id"),
		"path": leaf["path"],
		"op": leaf["op"],
		"agg": leaf.get("agg"),
		"value": threshold,
		"actual": actual,
		"missing": missing,
		"reason": _reason(leaf, language) or _default_reason(leaf, actual, threshold, missing),
		"basis": leaf.get("basis"),
		"source": leaf.get("source"),
	}


def _reason(node: dict, language: str) -> str:
	reason = node.get("reason")
	if isinstance(reason, dict):
		return str(reason.get(language) or reason.get("en") or "")
	return str(reason or "")


def _default_reason(leaf: dict, actual, threshold, missing: bool) -> str:
	if missing:
		return f"no data for {leaf['path']}"
	what = f"{leaf.get('agg')} of {leaf['path']}" if leaf.get("agg") else leaf["path"]
	return f"{what} is {actual!r}; needs {leaf['op']} {threshold!r}"


def _hold_text(failures: list, language: str) -> str:
	if not failures:
		return ""
	parts = []
	for failure in failures:
		label = failure.get("id") or failure.get("path")
		parts.append(f"{label} — {failure.get('reason')}" if failure.get("reason") else str(label))
	prefix = "Espera" if language == "es" else "Hold"
	return f"{prefix}: " + "; ".join(parts)


def _as_date(value) -> datetime.date | None:
	try:
		return datetime.date.fromisoformat(str(value)[:10])
	except (TypeError, ValueError):
		return None


# ── the field map's context section ─────────────────────────────────────────
def context_map() -> list:
	out = []
	for name in sorted(PROVIDERS):
		provider = PROVIDERS[name]
		for path, spec in sorted(provider.paths.items()):
			out.append(
				{
					"path": f"{name}.{path}",
					"type": spec["type"],
					"unit": spec["unit"] or None,
					"description": spec["description"],
					"example": spec["example"],
					"past_days": provider.past,
					"provider": name,
				}
			)
	return out


# ── the scan ────────────────────────────────────────────────────────────────
def scan_for(row: dict):
	"""A `Rule.scan` closure for a rule with a condition tree. Same contract as the others."""
	from . import compliance_rules
	from .alerts import base as alerts_base
	from .alerts import engine

	tree = parse_tree(row.get("condition_tree_json"))
	evaluation = parse_evaluation(row.get("evaluation_json"))
	raise_on_fail = evaluation.get("raise_when", "fail") == "fail"
	needed = providers_in(tree)

	def scan(context: dict) -> list:
		today = context.get("today") or frappe.utils.today()
		as_of = today
		if evaluation.get("as_of") == "tomorrow":
			as_of = (_as_date(today) + datetime.timedelta(days=1)).isoformat()
		company = context.get("company") or ""
		doctype = str(row.get("target_doctype") or "").strip()
		if not doctype or not compat.doctype_exists(doctype):
			return []
		warnings: list = []
		try:
			filters = compliance_rules.parse_filters(row.get("scope_filters_json"))
		except ValueError:
			filters = []
		company_field = compat.first_field(doctype, *engine._COMPANY_FIELDS)
		selected = engine._selectable_fields(doctype)
		db_filters = {company_field: company} if (company and company_field) else {}
		rows = frappe.db.get_all(doctype, filters=db_filters, fields=selected, limit=engine.SCAN_CAP)
		out = []
		for candidate in rows or []:
			candidate = dict(candidate)
			matched, _ = compliance_rules.row_matches(candidate, filters, set(selected))
			if not matched:
				continue
			row_company = str(candidate.get(company_field) or "") if company_field else company
			values = build_context(candidate, doctype=doctype, company=row_company, as_of=as_of, providers=needed)
			result = evaluate(tree, values)
			if result["passed"] == raise_on_fail:
				continue
			message = engine.render_message(
				str(row.get("message_template") or ""),
				candidate,
				{"hold": result["hold"], "failures": result["failures"], "as_of": as_of, "today": today},
				warnings,
			) if row.get("message_template") else (result["hold"] or f"{row.get('title')}: {candidate.get('name')}")
			out.append(
				alerts_base.Observation(
					source_doctype=doctype,
					source_docname=str(candidate.get("name")),
					message=message,
					severity=str(row.get("default_severity") or compliance_rules.SEVERITY_DEFAULT),
					due_date="",
					company=row_company,
					category=str(row.get("category") or "Records"),
					regimes=None,
					computation_warnings=list(warnings) or None,
				)
			)
		return out

	return scan


# v0.239.0. Weather and phenology, registered here so every caller of `ccf` sees them.
import sys as _sys  # noqa: E402

from . import ccf_providers as _providers  # noqa: E402

_providers.register(_sys.modules[__name__])

# v0.245.0. Whether the SOPs covering a task's type are approved.
from . import sop as _sop  # noqa: E402

_sop.register(_sys.modules[__name__])

# v0.249.0. Growing degree days per block, and the stage they suggest (never a passing stage).
from . import degree_days as _degree_days  # noqa: E402

_degree_days.register(_sys.modules[__name__])
