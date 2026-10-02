# SPDX-License-Identifier: MIT
"""Which `require_*` checks each sidecar route runs. v0.216.0.

Security review 2026-10-02, L4: "expose require_* per route so the mutating
routes can be audited, and fail a test if one has no gate". WHO may call a
route was a line inside its wrapper and nowhere else, which is the right place
for it to be enforced and the wrong place for it to be the only record.

READ OFF THE SOURCE, not declared beside it. A declared column is a second copy
that drifts; this one cannot say a route is gated when the call is not there.
It follows one level of indirection — a module-level `_helper(...)` the wrapper
calls — because that is where half of them keep the check.

WHAT IT IS NOT: proof that the check is the right one. It answers "does a role
or scope check run", which is the question a route shipped with none fails.
"""

from __future__ import annotations

import inspect
import re
import sys

from . import routes

_GATE = re.compile(r"\b(require_[a-z_]+)\(")
_HELPER = re.compile(r"(?<![.\w])(_[a-z][a-z0-9_]*)\(")

#: Mutating routes with no role or scope check ON PURPOSE, and why. Each acts
#: only on something the authenticated caller owns. A route may be here or have
#: a gate, never both — `tests_standalone/test_route_gates.py` holds it to that.
CALLER_SCOPED = {
	"/files/stage_file_chunk": (
		"enrolled caller only: writes one chunk into the caller's own upload session (the staging "
		"layer refuses a session somebody else opened) and attaches nothing"
	),
	"/files/finalize_staged_file": (
		"enrolled caller only: assembles the caller's own upload session into a private, unattached "
		"File; the staging layer refuses a session somebody else opened"
	),
}

_CACHE: dict = {}


def _source(function) -> str:
	try:
		return inspect.getsource(inspect.unwrap(function))
	except (OSError, TypeError):  # pragma: no cover - a build without sources
		return ""


def gates_of(route) -> list:
	"""The `require_*` names a route's handler runs, sorted. Cached per path."""
	if route.path in _CACHE:
		return _CACHE[route.path]
	source = _source(route.handler)
	found = set(_GATE.findall(source))
	module = sys.modules.get(getattr(inspect.unwrap(route.handler), "__module__", ""))
	for name in set(_HELPER.findall(source)):
		helper = getattr(module, name, None)
		if callable(helper):
			found |= set(_GATE.findall(_source(helper)))
	_CACHE[route.path] = sorted(found)
	return _CACHE[route.path]


def describe(route) -> dict:
	"""The `gate` / `gate_note` columns of `list_sidecar_routes` for one route."""
	out = {"gate": gates_of(route)}
	note = CALLER_SCOPED.get(route.path)
	if note:
		out["gate_note"] = note
	return out


def ungated_mutating() -> list:
	"""Paths that write, run no `require_*`, and are not explained. Should be []."""
	return sorted(
		route.path
		for route in routes.ROUTES
		if route.mutating and not gates_of(route) and route.path not in CALLER_SCOPED
	)
