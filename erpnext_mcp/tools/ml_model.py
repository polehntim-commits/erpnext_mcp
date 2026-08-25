# SPDX-License-Identifier: MIT
"""ML Model Registry — v0.43.0, plus v0.52.0's file serving, v0.59.0's bundles
and v0.68.0's migration. Thirteen tools, and only seven of them write.

WHAT THIS APP TRACKS, AND WHAT IT DOES NOT. Volume Vision (Flask/SQLAlchemy)
trains models and holds the weights; this app never sees a weight and never
computes a metric. What ERPNext adds is the one fact Volume Vision has no
reason to know: which trained model is DEPLOYED for which company and which
piecework activity — the record `get_active_model` is queried against by an
iOS app (BucketLog, Farm Ops) deciding what to pull. `source_uuid` is the join
key back to Volume Vision's `TrainedModel.uuid`, which is also the key an iOS
offline cache is keyed by, so a manifest this app hands back and a model
already cached on a phone are provably talking about the same trained model.

THE ARITHMETIC IS NOT HERE. Field validation, the manifest shape, and whether
activating one model conflicts with another live in
`erpnext_mcp/model_registry.py`, which is pure and reads no database — the
same split `budget_engine.py` and `payroll_gl.py` keep. This module is the only
place that reads or writes an `ML Model` document.

THE UNIQUENESS INVARIANT IS ENFORCED TWICE, DELIBERATELY. `activate_model`
here computes and REPORTS what it is about to supersede before saving, using
`model_registry.check_model_conflicts` against whatever this module found by
querying the database — that read is this module's job precisely because
`check_model_conflicts` cannot do it itself. The DocType controller
(`erpnext_mcp/erpnext_mcp/doctype/ml_model/ml_model.py`) separately guarantees
the invariant holds in the database regardless of which door a save came
through, the same way `Budget`'s controller refuses a duplicate account
independent of which tool built the document. Neither makes the other
redundant: this module's job is to tell a caller what changed, the
controller's job is to make sure the database cannot disagree.

WHO MAY WRITE. The same three roles the KPI and Budget frameworks use — System
Manager, Accounts Manager, Farm Manager — via `kpi_tools.require_kpi_role`.
Which model is live for a piecework activity determines what a scanning app
counts as a filled bucket, which is the same class of judgement that ends up
on a payroll run as a Budget's variance or a KPI's threshold does.

────────────────────────────────────────────────────────────────────────────
v0.52.0: ERPNEXT SERVES THE BINARY. IT DOES NOT ONLY POINT AT WHO HAS IT
────────────────────────────────────────────────────────────────────────────

Through v0.51.1 this module answered "which model" and nothing about "from
where" — an iOS app read `source_server` off the manifest and pulled the
`.mlmodelc` from Volume Vision directly. That made Volume Vision a second
thing a phone in an orchard has to reach, on a network path this app's own
farmops-api sidecar exists specifically to be the one door through (see
`farmops_api/wsgi.py`).

`attach_model_file` is the fix: an operator uploads the binary ONCE — straight
through this tool for something small, or through `tools/uploads.py`'s staged
chunks (`stage_file_chunk`/`commit_staged_file`, `attach_to_doctype="ML
Model"`) for something that is not — and the ML Model record owns it from then
on, as `model_file`, a plain Frappe Attach field. `get_model_file_chunk` is
the read half: an iOS app that already speaks farmops-api's `X-FarmOps-Token`
door reads the binary back through IT, in the same base64-chunked JSON shape
`stage_file_chunk` takes, rather than opening a second connection to a second
service with a second credential.

WHAT THIS DOES NOT DO. There is no on-demand proxy back to `source_server` when
a model has no `model_file` yet — `get_model_file_chunk` refuses by name and
says which tool fixes it. A pull-through cache that fetched from Volume Vision
the first time anybody asked would still make a phone's first request depend on
Volume Vision being reachable, which is the exact dependency this release
removes; an operator running `attach_model_file` once, deliberately, after a
training run is the one dependency it replaces it with.

────────────────────────────────────────────────────────────────────────────
v0.59.0: THE BUNDLE, AND THE PULL THAT FETCHES ONE
────────────────────────────────────────────────────────────────────────────

v0.52.0 got the binary out of Volume Vision's hands and into ERPNext's, and left
one thing behind: the LABELS. `class_names` was typed onto the record at
registration, the weights were exported separately, and nothing checked that
output index 2 of the model and entry 2 of the list were the same thing. When
they were not, nothing failed — inference went on returning confident numbers
against the wrong names, which is worse than an error and much harder to notice.

A **model bundle** is one zip carrying `model.mlmodel` and a `manifest.json`
Volume Vision writes at export time, in model-output-index order, from the same
training config the weights came out of. Two tools change for it:

  * `attach_model_file` SNIFFS THE BYTES. Four bytes of PK magic — not the file
    extension, which is whatever a browser called the download — decide whether
    what arrived is a bundle or a raw model. A bundle's manifest is read, its
    `class_names` become the record's, and `manifest_source` says on the record
    itself where they came from. A raw file behaves exactly as it did in
    v0.52.0, and says so.

  * `pull_model_from_vv` IS THE WHOLE MANUAL PROCEDURE AS ONE CALL. It fetches
    the bundle from Volume Vision's new `/training/models/<uuid>/bundle`, falls
    back to the original `/download` when that server has not been upgraded yet
    (reporting the fallback rather than hiding it), attaches what it got, and
    reconciles the manifest exactly as an operator's own upload would be. The
    HTTP half — and the reasoning about making an outbound request from inside
    the site's network — lives in `services/volume_vision.py`.

THE BUNDLE WINS AND SAYS SO. When a bundle's `class_names` disagree with the
record's, the record's are replaced and the previous list is returned in the
result's `previous` block with a warning naming both. The one disagreement that
is NOT reconciled is `source_uuid`: a bundle for a different trained model is the
wrong file for this record, and attaching it would make every iOS cache keyed on
that uuid wrong. That is refused by name, with `force=true` for the operator who
means it. See `model_registry.reconcile_bundle_manifest`.

────────────────────────────────────────────────────────────────────────────
v0.68.0: THE RECORDS THAT PREDATE ALL OF THAT
────────────────────────────────────────────────────────────────────────────

Three releases have now each defined what an ML Model record carries, and a
site that has been running since v0.43.0 holds all three shapes at once: a
record with labels typed onto it and no file, a record with a raw `.mlmodel`
attached, and a record with a bundle manifest stored exactly as Volume Vision's
exporter wrote it. `get_active_model` serves all three identically and a client
cannot tell them apart — which is the same "nothing fails, the answer is just
wrong" shape the bundle format was introduced to close, one level up.

`model_registry.MANIFEST_SCHEMA_VERSION` is the shape they are all brought to,
and the three tools here are the operator's route to it:

  * `list_models_needing_migration` — the register. Every record not in the
    current schema, with the reasons per record, and separately the ones that
    CANNOT be migrated as they stand because there are no labels anywhere to
    build a manifest out of. Metadata only, no file reads.

  * `validate_model_bundle` — one record, in depth. The manifest against the
    schema, the record against its own manifest, and — this is the half a pure
    function cannot do — the FILE against both: does `model_file` resolve, are
    the bytes the shape the manifest claims, and does a zip still contain what
    it says it does.

  * `migrate_model_format` — the write, and the only one of the three. It moves
    METADATA ONLY: no upload, no download, no re-attach, and it never reads the
    binary. A record that already had a bundle keeps that provenance and gains
    the schema fields; a record that never had one gets a manifest assembled
    from its own fields and `manifest_source` SAYING SO, because claiming the
    labels came out of a training run when somebody typed them is exactly the
    lie the `manifest_source` field was added to prevent.

WHAT MIGRATION DELIBERATELY DOES NOT FIX. A record with no `class_names` at all,
and a `model_format` this app does not recognise. Both are refused by name with
the tool that settles them — inventing a label list is the one thing worse than
not having one.
"""

from __future__ import annotations

import base64
import json
import os

import frappe

from .. import compat, model_registry
from ..args import as_bool, as_int, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from ..services import volume_vision
from . import files as files_tools
from . import kpi as kpi_tools

DOCTYPE = "ML Model"

#: Most models one list call returns. See budget.py's RECORD_CAP for the same
#: reasoning: a cap that is reported beats a register that silently stops
#: somewhere nobody chose.
RECORD_CAP = 200

#: Raw bytes per piece `get_model_file_chunk` returns, before base64. 512 KB
#: matches FarmOpsKit's own upload slice (`FarmOpsConfig.uploadChunkBytes`,
#: see `tools/uploads.py`) so the read direction costs the client no new
#: chunking logic — it already drives one chunked transfer.
MODEL_CHUNK_BYTES = 512 * 1024

#: Fields `update_model` may edit. `status`, `company` and `deployed_at` are
#: deliberately absent — status moves only through activate_model/
#: deprecate_model, which is where the supersession bookkeeping lives, and
#: `company`/`piecework_activity` are the identity a caller is disambiguating
#: BY, not a field being renamed out from under an in-flight lookup.
_EDITABLE_FIELDS = (
	"model_name",
	"version",
	"source_uuid",
	"source_server",
	"model_kind",
	"model_format",
	"taxonomy_schema",
	"taxonomy_version",
	"file_size_bytes",
	"deployment_targets",
	"training_completed_at",
	"notes",
)


def _require() -> None:
	compat.require_doctype(
		DOCTYPE,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


# ── resolving ─────────────────────────────────────────────────────────────


def _resolve(reference: str, company: str = "", version: str = ""):
	reference = str(reference or "").strip()
	if not reference:
		raise ToolError(
			"model is required — an ML Model docname (e.g. MLM-2026-0001), or a model_name to "
			"look up by. list_models has the register. Nothing was changed."
		)
	if frappe.db.exists(DOCTYPE, reference):
		return frappe.get_doc(DOCTYPE, reference)

	filters: dict = {"model_name": reference}
	if company:
		filters["company"] = resolve_company(company, required=False) or company
	if version:
		filters["version"] = str(version).strip()
	matches = frappe.db.get_all(DOCTYPE, filters=filters, pluck="name", limit=25)
	if len(matches) == 1:
		return frappe.get_doc(DOCTYPE, matches[0])
	if len(matches) > 1:
		raise ToolError(
			f"{reference!r} matches {len(matches)} ML Model records: "
			f"{', '.join(sorted(matches)[:10])}. Pass the docname, or narrow with version "
			"and/or company. Nothing was changed."
		)

	# A caller holding a manifest (get_active_model, or an iOS app's own cache)
	# has `source_uuid` under the name `uuid` and nothing else — see
	# `model_registry.build_model_manifest`. Tried last, after the docname and
	# model_name lookups, because a source_uuid is Volume Vision's identifier
	# and this app's own docname and model_name are what most callers pass.
	by_uuid = frappe.db.get_value(DOCTYPE, {"source_uuid": reference}, "name")
	if by_uuid:
		return frappe.get_doc(DOCTYPE, by_uuid)

	raise ToolError(
		f"no ML Model called {reference!r} on this site. list_models has the register. Nothing was changed."
	)


def _reference(args: dict) -> str:
	return as_str(args, "model") or as_str(args, "name") or as_str(args, "model_name", required=True)


# ── reading JSON arguments ───────────────────────────────────────────────


def _as_json_arg(args: dict, key: str, expected_type: type):
	"""`args[key]` as `expected_type` (list or dict) — accepts a native JSON
	value or a JSON string, since a model context sends both. `None` when the
	key is absent, so a caller can tell "not sent" from "sent empty"."""
	if key not in args or args[key] is None:
		return None
	raw = args[key]
	if isinstance(raw, expected_type):
		return raw
	if isinstance(raw, str):
		if raw.strip() == "":
			return expected_type()
		try:
			parsed = json.loads(raw)
		except (TypeError, ValueError):
			raise ToolError(f"{key} must be valid JSON. Nothing was changed.") from None
		if not isinstance(parsed, expected_type):
			raise ToolError(
				f"{key} must be a JSON {'array' if expected_type is list else 'object'}. Nothing was changed."
			)
		return parsed
	raise ToolError(
		f"{key} must be a JSON {'array' if expected_type is list else 'object'}. Nothing was changed."
	)


# ── reading a document ───────────────────────────────────────────────────


def _describe(doc) -> dict:
	return {
		"name": doc.name,
		"model_name": doc.model_name,
		"version": doc.version,
		"status": doc.status,
		"company": doc.company,
		"piecework_activity": doc.piecework_activity,
		"deployment_targets": doc.get("deployment_targets") or None,
		"source_uuid": doc.get("source_uuid") or None,
		"source_server": doc.get("source_server") or None,
		"model_kind": doc.get("model_kind") or None,
		"model_format": doc.get("model_format") or model_registry.DEFAULT_MODEL_FORMAT,
		"taxonomy_schema": doc.get("taxonomy_schema") or None,
		"taxonomy_version": doc.get("taxonomy_version") or None,
		"model_file": doc.get("model_file") or None,
		"manifest_source": doc.get("manifest_source") or None,
		"bundle_manifest": model_registry.bundle_manifest_of(doc.as_dict()) or None,
		# v0.68.0: read from the manifest's `manifest_origin`, not from whether
		# there IS a manifest — a migrated record has one and a raw file. See
		# `model_registry.is_bundle_payload`.
		"is_bundle": model_registry.is_bundle_payload(doc.as_dict()),
		"class_names": model_registry.class_names_of(doc.as_dict()),
		"metrics": model_registry.metrics_of(doc.as_dict()),
		"file_size_bytes": doc.get("file_size_bytes") or None,
		"training_completed_at": str(doc.get("training_completed_at") or "") or None,
		"deployed_at": str(doc.get("deployed_at") or "") or None,
		"notes": doc.get("notes") or None,
	}


def _active_sibling(company: str, piecework_activity: str, exclude_name: str = ""):
	"""The Active ML Model for (company, piecework_activity), or `None`.

	The database read `model_registry.check_model_conflicts` cannot do itself —
	see the module docstring.
	"""
	filters = {
		"company": company,
		"piecework_activity": piecework_activity,
		"status": model_registry.STATUS_ACTIVE,
	}
	if exclude_name:
		filters["name"] = ("!=", exclude_name)
	name = frappe.db.get_value(DOCTYPE, filters, "name")
	if not name:
		return None
	return _describe(frappe.get_doc(DOCTYPE, name))


# ── 1. register_model ─────────────────────────────────────────────────────


def register_model(args: dict) -> ToolResult:
	"""MUTATING (default OFF). Create an ML Model record from Volume Vision
	metadata: which trained model this is, what it predicts, and what it is
	used for. Starts life as Draft — activate_model is what makes it the model
	an iOS app pulls."""
	_require()
	actor = kpi_tools.require_kpi_role()

	company = resolve_company(as_str(args, "company"), required=True)
	model_name = as_str(args, "model_name", required=True)
	version = as_str(args, "version", required=True)
	piecework_activity = as_str(args, "piecework_activity", required=True)

	if frappe.db.exists(DOCTYPE, {"company": company, "model_name": model_name, "version": version}):
		raise ToolError(
			f"{model_name!r} version {version!r} is already registered for {company}. "
			"update_model edits the existing record; a genuinely different model wants a "
			"different name or version. Nothing was changed."
		)

	class_names = _as_json_arg(args, "class_names", list)
	metrics = _as_json_arg(args, "metrics", dict)
	status = as_str(args, "status") or model_registry.STATUS_DRAFT

	candidate = {
		"model_name": model_name,
		"version": version,
		"company": company,
		"piecework_activity": piecework_activity,
		"source_uuid": as_str(args, "source_uuid"),
		"model_kind": as_str(args, "model_kind"),
		"model_format": as_str(args, "model_format"),
		"status": status,
		"class_names": class_names,
		"metrics": metrics,
	}
	errors = model_registry.validate_model_registration(candidate)
	if errors:
		raise ToolError("; ".join(errors) + ". Nothing was changed.")

	doc = frappe.new_doc(DOCTYPE)
	doc.company = company
	doc.model_name = model_name
	doc.version = version
	doc.piecework_activity = piecework_activity
	doc.status = status
	doc.source_uuid = as_str(args, "source_uuid") or None
	doc.source_server = as_str(args, "source_server") or None
	doc.model_kind = as_str(args, "model_kind") or None
	doc.model_format = as_str(args, "model_format") or model_registry.DEFAULT_MODEL_FORMAT
	doc.taxonomy_schema = as_str(args, "taxonomy_schema") or None
	doc.taxonomy_version = as_str(args, "taxonomy_version") or None
	doc.deployment_targets = as_str(args, "deployment_targets") or None
	notes = as_str(args, "notes")
	if notes:
		doc.notes = notes
	file_size_bytes = as_int(args, "file_size_bytes")
	if file_size_bytes is not None:
		doc.file_size_bytes = file_size_bytes
	training_completed_at = as_str(args, "training_completed_at")
	if training_completed_at:
		doc.training_completed_at = training_completed_at
	if class_names is not None:
		doc.class_names = json.dumps(class_names, default=str)
	if metrics is not None:
		doc.metrics = json.dumps(metrics, default=str)

	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	described = _describe(doc)
	return ToolResult(
		data={"actor": actor, "model": described},
		summary=f"registered {doc.model_name} v{doc.version} ({doc.name}) for {company}, status {status}",
		docstatus_delta="none → 0 (model registered)",
	)


# ── 2. update_model ────────────────────────────────────────────────────────


def update_model(args: dict) -> ToolResult:
	"""MUTATING (default OFF). Edit metadata fields on an existing model.
	`status`, `company` and `piecework_activity` cannot be changed here —
	activate_model/deprecate_model own status, and a model's company and
	activity are its identity rather than an editable field."""
	_require()
	actor = kpi_tools.require_kpi_role()
	doc = _resolve(_reference(args), company=as_str(args, "company"), version=as_str(args, "version_hint"))
	before = _describe(doc)
	changed = []

	for field in _EDITABLE_FIELDS:
		if field not in args:
			continue
		if field == "file_size_bytes":
			value = as_int(args, field)
		else:
			value = as_str(args, field) or None
		if getattr(doc, field) != value:
			setattr(doc, field, value)
			changed.append(field)

	if "class_names" in args:
		class_names = _as_json_arg(args, "class_names", list)
		doc.class_names = json.dumps(class_names, default=str) if class_names is not None else None
		changed.append("class_names")

	if "metrics" in args:
		metrics = _as_json_arg(args, "metrics", dict)
		doc.metrics = json.dumps(metrics, default=str) if metrics is not None else None
		changed.append("metrics")

	if not changed:
		raise ToolError(
			f"nothing to update. Pass at least one of: {', '.join(_EDITABLE_FIELDS)}, class_names, "
			"metrics. Nothing was changed."
		)

	if "model_name" in changed or "version" in changed:
		clash = frappe.db.exists(
			DOCTYPE,
			{
				"company": doc.company,
				"model_name": doc.model_name,
				"version": doc.version,
				"name": ("!=", doc.name),
			},
		)
		if clash:
			raise ToolError(
				f"{doc.model_name!r} version {doc.version!r} is already registered for {doc.company} "
				f"as {clash}. Nothing was changed."
			)

	errors = model_registry.validate_model_registration(doc.as_dict())
	if errors:
		raise ToolError("; ".join(errors) + ". Nothing was changed.")

	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	described = _describe(doc)
	return ToolResult(
		data={"actor": actor, "model": described, "changed_fields": changed, "previous": before},
		summary=f"updated {doc.name}: {', '.join(changed)}",
		docstatus_delta=f"{len(changed)} field(s) changed on {doc.name}",
	)


# ── 3. get_model ──────────────────────────────────────────────────────────


def get_model(args: dict) -> ToolResult:
	"""Read-only. One ML Model record in full."""
	_require()
	doc = _resolve(_reference(args), company=as_str(args, "company"), version=as_str(args, "version"))
	described = _describe(doc)
	return ToolResult(
		data=described,
		summary=f"{doc.model_name} v{doc.version} ({doc.name}): {doc.status} for {doc.company}",
	)


# ── 4. list_models ────────────────────────────────────────────────────────


def list_models(args: dict) -> ToolResult:
	"""Read-only. The model register: every ML Model matching the filters,
	newest first."""
	_require()
	limit = min(as_limit(args), RECORD_CAP)

	filters: dict = {}
	company = resolve_company(as_str(args, "company"), required=False)
	if company:
		filters["company"] = company
	status = as_str(args, "status")
	if status:
		if status not in model_registry.STATUSES:
			raise ToolError(f"status must be one of {', '.join(model_registry.STATUSES)}; got {status!r}.")
		filters["status"] = status
	piecework_activity = as_str(args, "piecework_activity")
	if piecework_activity:
		filters["piecework_activity"] = piecework_activity

	found = frappe.db.get_all(
		DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"model_name",
			"version",
			"status",
			"company",
			"piecework_activity",
			"source_uuid",
			"model_kind",
			"model_format",
			"deployed_at",
			"modified",
		],
		order_by="modified desc",
		limit=limit + 1,
	)
	truncated = len(found) > limit
	found = found[:limit]

	data = {"models": found, "count": len(found), "truncated": truncated, "limit": limit}
	summary = f"{len(found)} model(s)" + (f", truncated at {limit}" if truncated else "")
	return ToolResult(data=data, summary=summary)


# ── 5. activate_model ────────────────────────────────────────────────────


def activate_model(args: dict) -> ToolResult:
	"""MUTATING (default OFF). Set status=Active and deployed_at=now. Whichever
	OTHER model was Active for the same (company, piecework_activity) auto-
	transitions to Deprecated — never more than one model is Active for one
	activity at one company. Activating an already-Active model is a no-op
	that still refreshes deployed_at."""
	_require()
	actor = kpi_tools.require_kpi_role()
	doc = _resolve(_reference(args), company=as_str(args, "company"), version=as_str(args, "version"))

	sibling = _active_sibling(doc.company, doc.piecework_activity, exclude_name=doc.name)
	conflict = model_registry.check_model_conflicts(
		{"name": doc.name, "company": doc.company, "piecework_activity": doc.piecework_activity},
		sibling,
	)

	was_active = doc.status == model_registry.STATUS_ACTIVE
	doc.status = model_registry.STATUS_ACTIVE
	doc.deployed_at = frappe.utils.now()
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	described = _describe(doc)
	data = {"actor": actor, "model": described, "superseded": conflict["supersedes"]}
	if conflict["supersedes"]:
		data["note"] = (
			f"{conflict['supersedes']} was Active for this (company, piecework_activity) pair and "
			"has been auto-deprecated. get_active_model for this pair now returns this record."
		)
	summary = (
		f"activated {doc.model_name} v{doc.version} ({doc.name}) for {doc.company}/{doc.piecework_activity}"
	)
	if was_active:
		summary += " (was already Active — deployed_at refreshed)"
	elif conflict["supersedes"]:
		summary += f", superseding {conflict['supersedes']}"
	return ToolResult(
		data=data,
		summary=summary,
		docstatus_delta=f"status → Active on {doc.name}"
		+ (f"; {conflict['supersedes']} → Deprecated" if conflict["supersedes"] else ""),
	)


# ── 6. deprecate_model ────────────────────────────────────────────────────


def deprecate_model(args: dict) -> ToolResult:
	"""MUTATING (default OFF). Set status=Deprecated. Nothing is deleted — a
	deprecated model keeps every field it had and simply stops being returned
	by get_active_model."""
	_require()
	actor = kpi_tools.require_kpi_role()
	doc = _resolve(_reference(args), company=as_str(args, "company"), version=as_str(args, "version"))
	if doc.status == model_registry.STATUS_DEPRECATED:
		raise ToolError(f"{doc.name} is already Deprecated. Nothing was changed.")

	previous_status = doc.status
	doc.status = model_registry.STATUS_DEPRECATED
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	return ToolResult(
		data={"actor": actor, "name": doc.name, "status": doc.status, "previous_status": previous_status},
		summary=f"deprecated {doc.name} (was {previous_status})",
		docstatus_delta=f"status: {previous_status} → Deprecated on {doc.name}",
	)


# ── 7. get_active_model ──────────────────────────────────────────────────


def get_active_model(args: dict) -> ToolResult:
	"""Read-only. THE TOOL AN iOS APP QUERIES to find out which model to pull
	for one company and one piecework activity. Returns the full record and its
	manifest (uuid/name/class_names/metadata, matching Volume Vision's own
	`to_dict()` shape) when a model is Active; a clear "nothing deployed yet"
	result, not an error, when none is."""
	_require()
	company = resolve_company(as_str(args, "company"), required=True)
	piecework_activity = as_str(args, "piecework_activity", required=True)

	name = frappe.db.get_value(
		DOCTYPE,
		{
			"company": company,
			"piecework_activity": piecework_activity,
			"status": model_registry.STATUS_ACTIVE,
		},
		"name",
	)
	if not name:
		return ToolResult(
			data={
				"company": company,
				"piecework_activity": piecework_activity,
				"active": False,
				"model": None,
				"manifest": None,
			},
			summary=f"no Active model for {company}/{piecework_activity}",
		)

	doc = frappe.get_doc(DOCTYPE, name)
	described = _describe(doc)
	manifest = model_registry.build_model_manifest(doc.as_dict())
	return ToolResult(
		data={
			"company": company,
			"piecework_activity": piecework_activity,
			"active": True,
			"model": described,
			"manifest": manifest,
		},
		summary=f"{doc.model_name} v{doc.version} ({doc.name}) is Active for {company}/{piecework_activity}",
	)


# ── 8. attach_model_file ─────────────────────────────────────────────────


def _staged_file(file_token: str):
	"""The File `file_token` names, read but NOT yet re-parented.

	Separated from the attach so the bytes can be inspected — is this a bundle,
	and does its manifest belong to this record? — before anything is written.
	A refusal that has already moved the File would be a refusal that says
	"nothing was changed" and is lying.
	"""
	if not frappe.db.exists("File", file_token):
		raise ToolError(f"no File named {file_token!r}. Nothing was changed.")
	file_doc = frappe.get_doc("File", file_token)
	if file_doc.get("is_folder"):
		raise ToolError(f"File {file_token!r} is a folder, not a model binary. Nothing was changed.")
	return file_doc


def _attach_file_token(doc, file_doc):
	file_doc.attached_to_doctype = DOCTYPE
	file_doc.attached_to_name = doc.name
	file_doc.attached_to_field = "model_file"
	file_doc.flags.ignore_permissions = True
	file_doc.save(ignore_permissions=True)
	return file_doc


def _attach_bytes(doc, content: bytes, file_name: str):
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": os.path.basename(str(file_name).replace("\\", "/")).strip() or "model",
			"attached_to_doctype": DOCTYPE,
			"attached_to_name": doc.name,
			"attached_to_field": "model_file",
			"is_private": 1,
			"content": content,
		}
	)
	file_doc.flags.ignore_permissions = True
	file_doc.insert(ignore_permissions=True)
	return file_doc


# ── the bundle half, shared by attach_model_file and pull_model_from_vv ───


def _read_bundle(content: bytes, file_name: str) -> dict:
	"""`model_registry.read_bundle`, with a zip that will not open turned into a refusal.

	A file whose first four bytes are PK and whose contents are not a readable
	bundle is a truncated or corrupt download, and storing it would mean an iOS
	app discovering that hours later on a handset in an orchard. The raw path is
	untouched: bytes that are not a zip are not a bundle and never come here.
	"""
	bundle = model_registry.read_bundle(content)
	if not bundle["is_bundle"]:
		return bundle
	if bundle["errors"]:
		raise ToolError(
			f"{file_name} is a zip, so it was read as a model bundle, and it is not a usable one: "
			+ "; ".join(bundle["errors"])
			+ ". A raw .mlmodel attaches unchanged; it is only a zip that is held to the bundle "
			"contract. Nothing was changed."
		)
	return bundle


def _apply_bundle(doc, bundle: dict, file_name: str, force: bool) -> dict:
	"""Reconcile a bundle's manifest onto `doc` in memory. Returns the audit block.

	Nothing is saved here — the caller saves once, after the File is attached,
	so a conflict refuses before either the record or the attachment moves.
	"""
	reconciled = model_registry.reconcile_bundle_manifest(doc.as_dict(), bundle["manifest"], file_name)
	if reconciled["conflicts"] and not force:
		raise ToolError(
			"; ".join(reconciled["conflicts"])
			+ ". Attach this bundle to the record that actually names that model, or pass force=true "
			"if this record is being repointed deliberately. Nothing was changed."
		)
	if reconciled["conflicts"]:
		reconciled["warnings"].append(
			"force=true: " + "; ".join(reconciled["conflicts"]) + " — applied anyway, and source_uuid "
			"was left as it was. Any iOS cache keyed on the old uuid is now stale."
		)

	for field, value in reconciled["updates"].items():
		if field in ("class_names", "metrics", "bundle_manifest"):
			setattr(doc, field, json.dumps(value, default=str))
		else:
			setattr(doc, field, value)
	return reconciled


def _apply_raw(doc, file_name: str) -> list:
	"""What a NON-bundle attachment records about its own provenance.

	The record keeps whatever `class_names` it already had — there is nothing in
	a raw `.mlmodel` to check them against — and `manifest_source` says so, so
	the difference between "these labels came out of training" and "somebody
	typed these" is visible on the form rather than inferred from which release
	the file was attached in.
	"""
	doc.manifest_source = model_registry.MANIFEST_SOURCE_RECORD
	doc.bundle_manifest = None
	if not model_registry.class_names_of(doc.as_dict()):
		return [
			f"{file_name} is a raw model file, not a bundle, and this record has no class_names — "
			"so nothing on this site knows what the model's output indices mean. Pull a bundle with "
			"pull_model_from_vv, or set them with update_model(class_names=[...])."
		]
	return [
		f"{file_name} is a raw model file, not a bundle. class_names on this record are unchanged "
		"and unverified against the weights — nothing in a raw .mlmodel says what its output "
		"indices mean. pull_model_from_vv gets the bundle when Volume Vision serves one."
	]


def attach_model_file(args: dict) -> ToolResult:
	"""MUTATING (default OFF). Give an ML Model record the binary an iOS app
	pulls. This is the upload-once step v0.52.0's module docstring describes —
	after this call `get_model_file_chunk` can serve the model, and an iOS app
	never has to reach Volume Vision for it.

	TWO WAYS IN, EXACTLY ONE REQUIRED. `file_token` is a File docname already on
	this site — the shape `commit_staged_file` (`tools/uploads.py`) hands back
	after a large binary has gone up in pieces, which is the path any real
	`.mlmodelc` should take. `file_content` is base64 in the call itself, for
	something small enough to fit one argument; `decode_base64_content` refuses
	anything over this app's per-call ceiling rather than accepting a truncated
	model silently.

	RE-ATTACHING REPLACES `model_file` AND LEAVES THE OLD FILE ON THE SITE. The
	previous binary is not deleted — the same posture `generate_employee_badge_qr`
	takes with a badge `regenerate` supersedes — so a rollback to a previous
	version is `activate_model` on an older record plus a fresh attach, not a
	recovery from a bin.

	v0.59.0: A ZIP IS READ AS A BUNDLE, ANYTHING ELSE IS THE OLD BEHAVIOUR. The
	first four bytes decide, not the file name. A bundle's `manifest.json`
	supplies `class_names`, `metrics` and the rest, and the record says so in
	`manifest_source`; a raw model attaches exactly as it did before, and says
	that its labels are unverified. See the module docstring.
	"""
	_require()
	actor = kpi_tools.require_kpi_role()
	doc = _resolve(_reference(args), company=as_str(args, "company"), version=as_str(args, "version"))
	before = _describe(doc)

	file_token = as_str(args, "file_token")
	file_content = as_str(args, "file_content")

	if file_token and file_content:
		raise ToolError(
			"pass file_token (a File already on this site) or file_content (the bytes, base64), "
			"not both. Nothing was changed."
		)
	if not file_token and not file_content:
		raise ToolError(
			"one of file_token or file_content is required — there is no model binary to attach "
			"otherwise. Nothing was changed."
		)

	# READ AND JUDGE BEFORE WRITING ANYTHING. Both paths produce the bytes first
	# so a bundle that belongs to another trained model is refused with the File
	# still where it was and the record untouched.
	if file_token:
		staged = _staged_file(file_token)
		content = staged.get_content()
		file_name = staged.get("file_name") or file_token
	else:
		staged = None
		file_name = as_str(args, "file_name")
		if not file_name:
			raise ToolError("file_name is required alongside file_content. Nothing was changed.")
		content = files_tools.decode_base64_content(file_content, tail="Nothing was changed.")

	if isinstance(content, str):  # a File stored as text still has to be sniffed as bytes
		content = content.encode("utf-8", "surrogateescape")

	bundle = _read_bundle(content, file_name)
	force = as_bool(args, "force", False)
	if bundle["is_bundle"]:
		reconciled = _apply_bundle(doc, bundle, file_name, force)
	else:
		reconciled = {"updates": {}, "warnings": _apply_raw(doc, file_name), "conflicts": []}

	file_doc = _attach_file_token(doc, staged) if staged else _attach_bytes(doc, content, file_name)

	doc.model_file = file_doc.file_url
	doc.file_size_bytes = int(file_doc.get("file_size") or 0) or len(content)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	described = _describe(doc)
	data = {
		"actor": actor,
		"model": described,
		"file": file_doc.name,
		"file_size_bytes": doc.file_size_bytes,
		"is_bundle": bundle["is_bundle"],
		"bundle": {
			"entries": bundle["entries"],
			"manifest_entry": bundle["manifest_entry"],
			"model_entry": bundle["model_entry"],
			"manifest": bundle["manifest"],
		}
		if bundle["is_bundle"]
		else None,
		"manifest_source": described["manifest_source"],
		"warnings": reconciled["warnings"],
		"previous": {"class_names": before["class_names"], "metrics": before["metrics"]},
	}
	shape = "bundle" if bundle["is_bundle"] else "raw model file"
	summary = f"attached {file_doc.get('file_name')} ({doc.file_size_bytes} bytes, {shape}) to {doc.name}"
	if bundle["is_bundle"]:
		summary += f"; class_names from its manifest ({len(described['class_names'])} label(s))"
	if reconciled["warnings"]:
		summary += f"; {len(reconciled['warnings'])} warning(s)"
	return ToolResult(
		data=data,
		summary=summary,
		docstatus_delta=f"model_file set on {doc.name}"
		+ ("; class_names/metrics taken from the bundle manifest" if bundle["is_bundle"] else ""),
	)


# ── 9. get_model_file_chunk ──────────────────────────────────────────────


def get_model_file_chunk(args: dict) -> ToolResult:
	"""Read-only. One base64 slice of an ML Model's attached binary.

	THE SAME SHAPE `stage_file_chunk` TAKES, READ BACKWARDS. A compiled model
	can run to tens of megabytes; asking a caller — this app's own farmops-api
	sidecar included, see `farmops_api/wsgi.py`'s JSON-only promise — to hold
	the whole thing in one response is the same memory spike Sprint 6 built
	chunked staging to avoid on the way up. `chunk_index` counts from 0; a
	caller that does not know `total_chunks` yet asks for index 0 and reads it
	off the answer, the same contract `stage_file_chunk` documents for pieces
	going the other way.

	REFUSES BY NAME WHEN NOTHING IS ATTACHED YET, rather than reaching for
	`source_server` on the caller's behalf — see the module docstring on why
	there is no proxy here. `attach_model_file` is the fix, and the refusal
	says so.

	v0.59.0: SERVES WHATEVER IS ATTACHED, AND SAYS WHICH IT IS. A bundle is
	larger than the raw model it contains, so `total_chunks` is larger too — it
	is computed from the stored bytes on every call and is right for either
	shape. `is_bundle` is read from the bytes themselves rather than from the
	record, so a client can branch on unzip-vs-compile from the first chunk it
	receives even if it never read the manifest.
	"""
	_require()
	doc = _resolve(_reference(args), company=as_str(args, "company"), version=as_str(args, "version"))

	model_file = doc.get("model_file")
	if not model_file:
		raise ToolError(
			f"{doc.name} has no model file attached yet. attach_model_file is what gives it one; "
			"until then this record is metadata only. Nothing was returned."
		)

	chunk_index = as_int(args, "chunk_index")
	if chunk_index is None or chunk_index < 0:
		raise ToolError("chunk_index is required and counts from 0. Nothing was returned.")
	requested = as_int(args, "chunk_bytes")
	chunk_bytes = MODEL_CHUNK_BYTES if requested is None else max(1, min(requested, MODEL_CHUNK_BYTES))

	file_name = frappe.db.get_value(
		"File",
		{"attached_to_doctype": DOCTYPE, "attached_to_name": doc.name, "file_url": model_file},
		"name",
	)
	if not file_name:
		raise ToolError(
			f"{doc.name}'s model_file does not resolve to a File this site can read. "
			"attach_model_file again to fix it. Nothing was returned."
		)
	file_doc = frappe.get_doc("File", file_name)
	content = file_doc.get_content()
	if isinstance(content, str):
		content = content.encode("utf-8", "surrogateescape")
	total_bytes = len(content)
	total_chunks = max(1, -(-total_bytes // chunk_bytes))
	if chunk_index >= total_chunks:
		raise ToolError(
			f"chunk_index {chunk_index} is outside the {total_chunks} piece(s) this file has. "
			"Nothing was returned."
		)

	start = chunk_index * chunk_bytes
	piece = content[start : start + chunk_bytes]
	is_bundle = model_registry.looks_like_bundle(content)
	return ToolResult(
		data={
			"model": doc.name,
			"file_name": file_doc.get("file_name"),
			"chunk_index": chunk_index,
			"total_chunks": total_chunks,
			"chunk_bytes": len(piece),
			"total_bytes": total_bytes,
			"chunk_base64": base64.b64encode(piece).decode("ascii"),
			"is_last": chunk_index == total_chunks - 1,
			"is_bundle": is_bundle,
			"manifest_source": doc.get("manifest_source")
			or (
				model_registry.MANIFEST_SOURCE_BUNDLE if is_bundle else model_registry.MANIFEST_SOURCE_RECORD
			),
		},
		summary=f"{doc.name} chunk {chunk_index + 1}/{total_chunks} ({len(piece)} bytes"
		+ (", bundle" if is_bundle else ", raw model")
		+ ")",
	)


# ── 10. pull_model_from_vv ───────────────────────────────────────────────


def _timeout(args: dict) -> int:
	"""`timeout_seconds`, defaulted when absent and REFUSED when zero.

	Not `as_int(...) or DEFAULT`: that reinstated the default for an explicit 0, so a
	caller asking for a zero-second fetch got a full-length one and a success. Zero is
	not a shorter timeout, it is a fetch that cannot succeed, and it is worth saying so.
	"""
	seconds = as_int(args, "timeout_seconds")
	if seconds is None:
		return volume_vision.DEFAULT_TIMEOUT_SECONDS
	if seconds <= 0:
		raise ToolError(
			f"timeout_seconds must be positive, got {seconds}. Omit it for the default of "
			f"{volume_vision.DEFAULT_TIMEOUT_SECONDS}s. Nothing was fetched."
		)
	return seconds


def pull_model_from_vv(args: dict) -> ToolResult:
	"""MUTATING (default OFF). Fetch a trained model straight from Volume Vision
	and attach it to this ML Model record — the whole manual procedure (curl on a
	laptop, base64, `bench console`) as one call, with the provenance check nobody
	was doing by hand.

	WHAT IT ASKS FOR, IN ORDER. `/training/models/<uuid>/bundle` first — the zip
	carrying `model.mlmodel` beside a `manifest.json` written at export time —
	and `/training/models/<uuid>/download`, the endpoint every existing consumer
	has always used, only as a fallback when the first answers 404/405/501. A
	Volume Vision that has not had the bundle export deployed yet answers exactly
	that, so the fallback is the "Phase 1 is not live on the training box"
	path. IT IS REPORTED, NOT HIDDEN: a raw file has no manifest, so the
	record's `class_names` remain whatever somebody typed, `manifest_source` says
	so, and the warning is in the result and the summary.

	WHERE IT FETCHES FROM. `source_server` on the record, unless `source_server`
	is passed here — the port is read, never assumed, because a Volume Vision on
	an operator's Umbrel is on whatever port they put it on. `source_uuid`
	likewise defaults to the record's. A record can also be resolved BY its
	source_uuid alone, which is how a caller holding only Volume Vision's
	identifier finds the ERPNext record for it.

	IT REFUSES A BUNDLE THAT NAMES A DIFFERENT TRAINED MODEL. See
	`_apply_bundle`. Everything else the manifest disagrees with, the manifest
	wins and the warning says what was replaced.
	"""
	_require()
	actor = kpi_tools.require_kpi_role()

	reference = as_str(args, "model") or as_str(args, "name") or as_str(args, "model_name")
	source_uuid = as_str(args, "source_uuid")
	if not reference and not source_uuid:
		raise ToolError(
			"one of model (a docname or model_name) or source_uuid is required — there is nothing "
			"to pull into otherwise. register_model is what creates the record first. Nothing was "
			"changed."
		)
	doc = _resolve(reference or source_uuid, company=as_str(args, "company"), version=as_str(args, "version"))
	before = _describe(doc)

	uuid = source_uuid or str(doc.get("source_uuid") or "").strip()
	if not uuid:
		raise ToolError(
			f"{doc.name} has no source_uuid and none was passed. That is Volume Vision's own "
			"TrainedModel.uuid and the only thing that names a model there — pass source_uuid, or "
			"set it with update_model. Nothing was changed."
		)
	base_url = as_str(args, "source_server") or as_str(args, "vv_url") or (doc.get("source_server") or "")

	fetched = volume_vision.fetch_model(
		base_url,
		uuid,
		prefer_bundle=as_bool(args, "prefer_bundle", True),
		allow_raw_fallback=as_bool(args, "allow_raw_fallback", True),
		timeout=_timeout(args),
	)
	content = fetched["content"]
	file_name = fetched["file_name"]

	bundle = _read_bundle(content, file_name)
	force = as_bool(args, "force", False)
	warnings = list(fetched["warnings"])
	if bundle["is_bundle"]:
		reconciled = _apply_bundle(doc, bundle, file_name, force)
		warnings.extend(reconciled["warnings"])
	else:
		warnings.extend(_apply_raw(doc, file_name))
		if fetched["endpoint"] == "bundle":
			warnings.append(
				f"{fetched['url']} answered 200 with something that is not a zip. The endpoint "
				"exists but did not serve a bundle — it was stored as a raw model."
			)

	# RECORDED BEFORE THE SAVE, NOT AFTER. The two arguments that steered this
	# fetch are the two facts a later reader needs to repeat it, and a pull that
	# worked from an argument the record does not hold is a pull nobody can
	# reproduce from the record.
	if source_uuid and not (doc.get("source_uuid") or ""):
		doc.source_uuid = source_uuid
	if base_url and not (doc.get("source_server") or ""):
		doc.source_server = base_url

	file_doc = _attach_bytes(doc, content, file_name)
	doc.model_file = file_doc.file_url
	doc.file_size_bytes = int(file_doc.get("file_size") or 0) or len(content)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	described = _describe(doc)
	data = {
		"actor": actor,
		"model": described,
		"file": file_doc.name,
		"file_size_bytes": doc.file_size_bytes,
		"source": {
			"url": fetched["url"],
			"endpoint": fetched["endpoint"],
			"fell_back": fetched["fell_back"],
			"source_uuid": uuid,
			"server_version": fetched.get("server_version"),
			"server_class_names": fetched.get("server_class_names"),
		},
		"is_bundle": bundle["is_bundle"],
		"bundle": {
			"entries": bundle["entries"],
			"manifest_entry": bundle["manifest_entry"],
			"model_entry": bundle["model_entry"],
			"manifest": bundle["manifest"],
		}
		if bundle["is_bundle"]
		else None,
		"manifest_source": described["manifest_source"],
		"warnings": warnings,
		"previous": {"class_names": before["class_names"], "metrics": before["metrics"]},
	}
	shape = "bundle" if bundle["is_bundle"] else "raw model file"
	summary = (
		f"pulled {file_name} ({doc.file_size_bytes} bytes, {shape}) from {fetched['url']} onto {doc.name}"
	)
	if bundle["is_bundle"]:
		summary += f"; class_names from its manifest ({len(described['class_names'])} label(s))"
	if fetched["fell_back"]:
		summary += "; FELL BACK to the raw /download endpoint — no manifest"
	if warnings:
		summary += f"; {len(warnings)} warning(s)"
	return ToolResult(
		data=data,
		summary=summary,
		docstatus_delta=f"model_file set on {doc.name} from {fetched['url']}"
		+ ("; class_names/metrics taken from the bundle manifest" if bundle["is_bundle"] else ""),
	)


# ── 11-13. v0.68.0: the records that predate the format ──────────────────


#: What `manifest_migration_report` reads off a record. Listed rather than
#: fetching whole documents because `list_models_needing_migration` runs the
#: report over every model on the site, and loading 200 documents to look at ten
#: fields on each is 200 round trips for a question one query answers.
_MIGRATION_FIELDS = (
	"name",
	"model_name",
	"version",
	"status",
	"company",
	"piecework_activity",
	"class_names",
	"bundle_manifest",
	"manifest_source",
	"model_format",
	"model_file",
	"modified",
)


def _attached_file(doc):
	"""The File holding this record's `model_file`, or `None`.

	The same lookup `get_model_file_chunk` makes, without its refusal: this is a
	VALIDATION path, where "the record points at a File that is not there" is a
	finding to report rather than an error to raise. A caller asking what is
	wrong with a record should get the whole list, not the first item on it.
	"""
	model_file = doc.get("model_file")
	if not model_file:
		return None
	file_name = frappe.db.get_value(
		"File",
		{"attached_to_doctype": DOCTYPE, "attached_to_name": doc.name, "file_url": model_file},
		"name",
	)
	if not file_name:
		return None
	return frappe.get_doc("File", file_name)


def _payload_bytes(file_doc) -> bytes:
	content = file_doc.get_content()
	if isinstance(content, str):
		return content.encode("utf-8", "surrogateescape")
	return bytes(content or b"")


def _issue(severity: str, code: str, message: str) -> dict:
	return {"severity": severity, "code": code, "message": message}


def migrate_model_format(args: dict) -> ToolResult:
	"""MUTATING (default OFF). Restate one ML Model record's manifest in the
	current schema. METADATA ONLY — nothing is uploaded, downloaded or
	re-attached, and the bytes on the record are not read or moved.

	WHAT "LEGACY" MEANS HERE, AND WHAT EACH SHAPE BECOMES.

	  * A v0.43.0/v0.52.0 record has NO manifest — labels typed onto it at
	    registration and, if anything is attached at all, a raw `.mlmodel`
	    beside them. There is no bundle to read, so the manifest is BUILT FROM
	    THE RECORD'S OWN FIELDS and says so: `manifest_origin` is `record` and
	    `manifest_source` becomes the sentence that names this tool. It does not
	    claim the labels came out of training, because they did not, and
	    `is_bundle` stays false so no client tries to unzip the weights.

	  * A v0.59.x record HAS a manifest, stored exactly as Volume Vision's
	    exporter wrote it. Its origin is `bundle` — that fact is real and is
	    preserved, along with every key the exporter put in it. What migration
	    adds is `schema_version` and the `userDefined` mirror.

	NOTHING IS INVENTED. A record with no `class_names` anywhere has nothing to
	build a manifest out of and is REFUSED by name, pointing at
	`pull_model_from_vv` (which gets them from the training run) or
	`update_model` (which sets them by hand). A `model_format` this app does not
	recognise is refused the same way rather than being quietly replaced with the
	default — see `model_registry.manifest_migration_report`'s blockers.

	ALREADY CURRENT IS NOT AN ERROR. A record needing nothing returns a result
	saying so, which is what makes this safe to run across a register from
	`list_models_needing_migration` without filtering first. `force=true`
	rewrites one anyway.

	`dry_run=true` computes the whole thing and saves nothing, returning the
	manifest it would have written.
	"""
	_require()
	actor = kpi_tools.require_kpi_role()
	doc = _resolve(_reference(args), company=as_str(args, "company"), version=as_str(args, "version"))
	before = _describe(doc)

	dry_run = as_bool(args, "dry_run", False)
	force = as_bool(args, "force", False)
	report = model_registry.manifest_migration_report(doc.as_dict())

	if report["blockers"]:
		raise ToolError("; ".join(report["blockers"]) + f". {doc.name} was left exactly as it was.")

	if not report["needs_migration"] and not force:
		return ToolResult(
			data={
				"actor": actor,
				"model": before,
				"migrated": False,
				"already_current": True,
				"report": report,
				"changed_fields": [],
				"warnings": [],
			},
			summary=(
				f"{doc.name} is already in manifest schema {model_registry.MANIFEST_SCHEMA_VERSION} "
				"— nothing to migrate"
			),
		)

	origin = report["manifest_origin"]
	existing = model_registry.bundle_manifest_of(doc.as_dict())
	normalized = model_registry.normalize_manifest(existing, doc.as_dict(), origin=origin)

	# THE OUTPUT IS CHECKED BEFORE IT IS WRITTEN. `manifest_migration_report`
	# clears a record for migration; this asserts that migrating it actually
	# produced the schema, so a future change to `normalize_manifest` that stops
	# satisfying `validate_manifest_schema` fails here rather than leaving a
	# register full of records that claim to be current and are not.
	errors = model_registry.validate_manifest_schema(normalized)
	if errors:
		raise ToolError(
			"migrating "
			+ doc.name
			+ " would produce a manifest that is still not in the current schema: "
			+ "; ".join(errors)
			+ ". That is a bug in this app rather than in the record. Nothing was changed."
		)

	warnings: list = []
	changed: list = []

	manifest_class_names = list(normalized.get("class_names") or [])
	if manifest_class_names != before["class_names"]:
		if origin == model_registry.MANIFEST_ORIGIN_BUNDLE and before["class_names"]:
			warnings.append(
				f"class_names on this record were {json.dumps(before['class_names'])} and its stored "
				f"bundle manifest says {json.dumps(manifest_class_names)}. The manifest wins, the same "
				"way it does on an attach — it was written at export time from the config the weights "
				"came out of. The old list is in this call's `previous` block."
			)
		doc.class_names = json.dumps(manifest_class_names, default=str)
		changed.append("class_names")

	if not (doc.get("model_format") or "").strip():
		doc.model_format = normalized["model_format"]
		changed.append("model_format")

	doc.bundle_manifest = json.dumps(normalized, default=str)
	changed.append("bundle_manifest")

	if origin == model_registry.MANIFEST_ORIGIN_RECORD:
		doc.manifest_source = model_registry.MANIFEST_SOURCE_MIGRATED
		changed.append("manifest_source")
		warnings.append(
			f"{doc.name} had no bundle, so its manifest was assembled from the record's own fields. "
			"The labels are still unverified against the weights — manifest_origin says 'record' and "
			"manifest_source names this tool. pull_model_from_vv is what replaces them with a "
			"manifest written at export time."
		)
	elif not str(doc.get("manifest_source") or "").startswith(model_registry.MANIFEST_SOURCE_BUNDLE):
		doc.manifest_source = model_registry.manifest_source_note(normalized)
		changed.append("manifest_source")

	if dry_run:
		return ToolResult(
			data={
				"actor": actor,
				"model": before,
				"migrated": False,
				"dry_run": True,
				"already_current": False,
				"report": report,
				"manifest": normalized,
				"changed_fields": changed,
				"warnings": warnings,
				"previous": {"class_names": before["class_names"], "bundle_manifest": existing or None},
			},
			summary=(
				f"dry run: {doc.name} would be migrated to manifest schema "
				f"{model_registry.MANIFEST_SCHEMA_VERSION} ({origin} origin), changing "
				f"{', '.join(changed)}. Nothing was changed."
			),
		)

	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	described = _describe(doc)
	after = model_registry.manifest_migration_report(doc.as_dict())
	return ToolResult(
		data={
			"actor": actor,
			"model": described,
			"migrated": True,
			"dry_run": False,
			"already_current": False,
			"manifest_origin": origin,
			"schema_version": model_registry.MANIFEST_SCHEMA_VERSION,
			"report": report,
			"report_after": after,
			"manifest": normalized,
			"changed_fields": changed,
			"warnings": warnings,
			"previous": {"class_names": before["class_names"], "bundle_manifest": existing or None},
		},
		summary=(
			f"migrated {doc.name} to manifest schema {model_registry.MANIFEST_SCHEMA_VERSION} "
			f"({origin} origin): {', '.join(changed)}"
		)
		+ (f"; {len(warnings)} warning(s)" if warnings else ""),
		docstatus_delta=f"bundle_manifest restated on {doc.name}; no file was uploaded or replaced",
	)


def validate_model_bundle(args: dict) -> ToolResult:
	"""Read-only. Hold ONE ML Model record's stored manifest to the current
	schema and report every way it falls short — pass/fail plus the specific
	issues, rather than the first thing that went wrong.

	THREE LAYERS, AND THEY FAIL DIFFERENTLY.

	  1. THE MANIFEST ITSELF, against `model_registry.validate_manifest_schema`:
	     required fields present, `class_names` an ordered array of labels,
	     `model_kind`/`model_format` recognised, and the `userDefined` mirror
	     agreeing with the label array it mirrors.

	  2. THE RECORD AGAINST ITS MANIFEST. Two lists that disagree about what
	     output index 2 means is the failure the bundle format exists to prevent,
	     and it is an ERROR here rather than a note.

	  3. THE FILE REFERENCES. `model_file` set but resolving to no File on this
	     site is an error — `get_model_file_chunk` would refuse and an iOS app
	     would find out on a handset. A manifest whose `manifest_origin` says
	     `bundle` over bytes that are not a zip is the same class of error read
	     from the other side, and a bundle whose zip has lost its `manifest.json`
	     or its model payload is reported entry by entry.

	`check_payload=false` skips layer 3's byte reads. Frappe reads a File whole,
	so validating a register of compiled models pulls every one of them into
	memory; the metadata checks alone are the cheap pass.

	NOTHING IS CHANGED, INCLUDING NOTHING THAT IS OBVIOUSLY WRONG.
	`migrate_model_format` is the tool that writes, and the split is deliberate:
	this one is safe to run over a whole register.
	"""
	_require()
	doc = _resolve(_reference(args), company=as_str(args, "company"), version=as_str(args, "version"))
	check_payload = as_bool(args, "check_payload", True)

	record = doc.as_dict()
	report = model_registry.manifest_migration_report(record)
	manifest = model_registry.bundle_manifest_of(record)
	issues: list = []

	if not manifest:
		issues.append(
			_issue(
				"error",
				"no_manifest",
				"this record has no bundle_manifest — nothing on this site records what its model's "
				"output indices mean or where that claim came from. migrate_model_format builds one "
				"from the record's own fields; pull_model_from_vv gets the real one from training.",
			)
		)
	else:
		for error in model_registry.validate_manifest_schema(manifest):
			issues.append(_issue("error", "schema", error))

	record_class_names = model_registry.class_names_of(record)
	manifest_class_names = manifest.get("class_names") if isinstance(manifest, dict) else None
	manifest_class_names = (
		[str(label) for label in manifest_class_names] if isinstance(manifest_class_names, list) else []
	)
	if not record_class_names:
		issues.append(
			_issue(
				"error",
				"no_class_names",
				"class_names is empty on this record, so an inference result's output index cannot be "
				"mapped back to a label at all.",
			)
		)
	elif manifest_class_names and manifest_class_names != record_class_names:
		issues.append(
			_issue(
				"error",
				"class_names_disagree",
				f"class_names on the record are {json.dumps(record_class_names)} and its manifest says "
				f"{json.dumps(manifest_class_names)}. Only one of them is the output-index order, and "
				"nothing here can say which.",
			)
		)

	model_format = str(doc.get("model_format") or "").strip()
	if model_format and model_format not in model_registry.MODEL_FORMATS:
		issues.append(
			_issue(
				"error",
				"model_format",
				f"model_format on the record is {model_format!r}, not one of "
				f"{', '.join(model_registry.MODEL_FORMATS)}.",
			)
		)

	if report["manifest_origin"] == model_registry.MANIFEST_ORIGIN_RECORD:
		issues.append(
			_issue(
				"warning",
				"labels_unverified",
				"this manifest was assembled from the record's own fields, not read out of a bundle, "
				"so its labels have never been checked against the weights. pull_model_from_vv is what "
				"replaces them with a manifest written at export time.",
			)
		)

	checks = {
		"has_manifest": bool(manifest),
		"schema_version": report["schema_version"],
		"expected_schema_version": model_registry.MANIFEST_SCHEMA_VERSION,
		"manifest_origin": report["manifest_origin"],
		"class_names_count": len(record_class_names),
		"needs_migration": report["needs_migration"],
		"file_attached": bool(doc.get("model_file")),
		"file_resolves": None,
		"file_size_bytes_matches": None,
		"payload_is_bundle": None,
		"payload_matches_manifest_origin": None,
	}

	if not doc.get("model_file"):
		checks["file_resolves"] = False
		issues.append(
			_issue(
				"warning",
				"no_model_file",
				"nothing is attached to this record yet, so it is metadata only — get_model_file_chunk "
				"has nothing to serve. attach_model_file or pull_model_from_vv gives it a binary.",
			)
		)
	else:
		file_doc = _attached_file(doc)
		checks["file_resolves"] = bool(file_doc)
		if not file_doc:
			issues.append(
				_issue(
					"error",
					"file_missing",
					f"model_file points at {doc.get('model_file')!r} and no File attached to this "
					"record has that URL. get_model_file_chunk would refuse; attach_model_file again "
					"is the fix.",
				)
			)
		elif check_payload:
			content = _payload_bytes(file_doc)
			is_bundle = model_registry.looks_like_bundle(content)
			checks["payload_is_bundle"] = is_bundle
			declared_bundle = report["manifest_origin"] == model_registry.MANIFEST_ORIGIN_BUNDLE
			checks["payload_matches_manifest_origin"] = is_bundle == declared_bundle
			recorded_size = int(doc.get("file_size_bytes") or 0)
			checks["file_size_bytes_matches"] = recorded_size == len(content)
			if recorded_size and recorded_size != len(content):
				issues.append(
					_issue(
						"warning",
						"file_size_drift",
						f"file_size_bytes on the record says {recorded_size} and the stored file is "
						f"{len(content)} bytes. attach_model_file rewrites both together, so a "
						"disagreement means the File was replaced underneath the record.",
					)
				)
			if declared_bundle and not is_bundle:
				issues.append(
					_issue(
						"error",
						"payload_not_a_bundle",
						"the manifest's manifest_origin says 'bundle' and the attached bytes are not a "
						"zip. A client reading this record would try to unpack a raw model and fail on "
						"the handset.",
					)
				)
			elif is_bundle and not declared_bundle:
				issues.append(
					_issue(
						"error",
						"payload_is_an_unread_bundle",
						"the attached bytes ARE a zip and this record's manifest does not come from "
						"one — so a bundle was stored without its manifest ever being read. "
						"attach_model_file on the same file reads it.",
					)
				)
			if is_bundle:
				read = model_registry.read_bundle(content)
				for error in read["errors"]:
					issues.append(_issue("error", "bundle_contents", error))
				checks["bundle_entries"] = read["entries"]
				checks["bundle_model_entry"] = read["model_entry"] or None
				checks["bundle_manifest_entry"] = read["manifest_entry"] or None
				if not read["errors"] and not read["model_entry"]:
					issues.append(
						_issue(
							"warning",
							"bundle_model_entry_missing",
							"the bundle's manifest.json is readable but no entry in the zip looks like a "
							"model payload (.mlpackage/.mlmodelc/.mlmodel/.onnx/.tflite/.pb). A client "
							"unpacking it would have labels and nothing to apply them to.",
						)
					)

	errors = [issue for issue in issues if issue["severity"] == "error"]
	warnings = [issue for issue in issues if issue["severity"] == "warning"]
	valid = not errors
	summary = (
		f"{doc.name} ({doc.model_name} v{doc.version}): "
		+ ("PASS" if valid else f"FAIL — {len(errors)} error(s)")
		+ (f", {len(warnings)} warning(s)" if warnings else "")
	)
	return ToolResult(
		data={
			"model": doc.name,
			"model_name": doc.model_name,
			"version": doc.version,
			"status": doc.status,
			"company": doc.company,
			"valid": valid,
			"error_count": len(errors),
			"warning_count": len(warnings),
			"issues": issues,
			"checks": checks,
			"needs_migration": report["needs_migration"],
			"can_migrate": report["can_migrate"],
			"blockers": report["blockers"],
			"payload_checked": check_payload,
		},
		summary=summary,
	)


def list_models_needing_migration(args: dict) -> ToolResult:
	"""Read-only. Which ML Model records are NOT in the current manifest schema,
	and what is outdated about each — the register `migrate_model_format` works
	through.

	WHY A RECORD APPEARS HERE. No `bundle_manifest` at all (the pre-v0.59.0
	shape, where labels lived only in `class_names`), a manifest with no
	`schema_version` or an older one, a missing `userDefined` mirror or one that
	disagrees with the label array, an unrecognised `model_kind`/`model_format`,
	or a record whose own `class_names` disagree with its manifest's. Each row
	carries its own `reasons` rather than a code, because the fix differs.

	`blockers` IS THE COLUMN TO READ FIRST. A record with one cannot be migrated
	by `migrate_model_format` at all — there are no labels anywhere to build a
	manifest out of, or a `model_format` nothing recognises — and needs
	`update_model` or `pull_model_from_vv` before migration will touch it.
	`ready_to_migrate` counts the rest.

	A MODEL PULLED TODAY WILL NOT BE HERE. `attach_model_file` and
	`pull_model_from_vv` normalize on the way in as of v0.68.0, so this is a
	register of records that predate the format, not a queue that refills.

	Reads no files: this is the cheap metadata pass over the whole register.
	`validate_model_bundle` is the deep single-record check that opens the
	attached bytes.
	"""
	_require()
	limit = min(as_limit(args), RECORD_CAP)
	include_current = as_bool(args, "include_current", False)

	filters: dict = {}
	company = resolve_company(as_str(args, "company"), required=False)
	if company:
		filters["company"] = company
	status = as_str(args, "status")
	if status:
		if status not in model_registry.STATUSES:
			raise ToolError(f"status must be one of {', '.join(model_registry.STATUSES)}; got {status!r}.")
		filters["status"] = status
	piecework_activity = as_str(args, "piecework_activity")
	if piecework_activity:
		filters["piecework_activity"] = piecework_activity

	found = frappe.db.get_all(
		DOCTYPE,
		filters=filters,
		fields=list(_MIGRATION_FIELDS),
		order_by="modified desc",
		limit=limit + 1,
	)
	truncated = len(found) > limit
	found = found[:limit]

	rows = []
	current = 0
	blocked = 0
	for record in found:
		report = model_registry.manifest_migration_report(record)
		report["has_model_file"] = bool(record.get("model_file"))
		if not report["needs_migration"]:
			current += 1
			if not include_current:
				continue
		elif not report["can_migrate"]:
			blocked += 1
		rows.append(report)

	needing = sum(1 for row in rows if row["needs_migration"])
	summary = (
		f"{needing} of {len(found)} model(s) need migration to manifest schema "
		f"{model_registry.MANIFEST_SCHEMA_VERSION}"
	)
	if blocked:
		summary += f"; {blocked} of them blocked and cannot be migrated as they stand"
	if truncated:
		summary += f"; scan truncated at {limit}"

	return ToolResult(
		data={
			"models": rows,
			"count": needing,
			"scanned": len(found),
			"already_current": current,
			"blocked": blocked,
			"ready_to_migrate": needing - blocked,
			"schema_version": model_registry.MANIFEST_SCHEMA_VERSION,
			"truncated": truncated,
			"limit": limit,
			"include_current": include_current,
		},
		summary=summary,
	)
