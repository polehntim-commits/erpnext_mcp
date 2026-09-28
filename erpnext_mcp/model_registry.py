# SPDX-License-Identifier: MIT
"""Which ML model is deployed for which piecework activity. PURE FUNCTIONS.

Same contract `budget_engine.py` and `payroll_gl.py` keep: no database reads, no
side effects, everything arrives as a plain dict and everything returned is
derivable again from the same dict. `tools/ml_model.py` is the only place that
reads or writes an `ML Model` document.

WHAT AN `ML Model` RECORD IS. Volume Vision (the training side) already knows a
`TrainedModel` by its own `uuid`, `name`, `class_names_json` and
`metrics_json`. This app does not train anything and does not hold the weights
— it holds ONE FACT Volume Vision has no reason to know: which of its trained
models is the one an iOS app (BucketLog, Farm Ops) should be pulling right now
for a given company and a given piecework activity. `source_uuid` is the join
key back to Volume Vision's `TrainedModel.uuid`, which is also the key the iOS
offline cache is keyed by — so `get_active_model`'s manifest and a locally
cached model are talking about the same UUID without either side maintaining a
translation table.

`build_model_manifest` MATCHES VOLUME VISION'S OWN `to_dict()` SHAPE
(uuid/name/class_names/metadata) rather than inventing a second one, because an
iOS client that already parses one manifest shape from Volume Vision's sync
endpoint should not need a second parser for ERPNext's.

────────────────────────────────────────────────────────────────────────────
v0.59.0: THE BUNDLE, AND WHY `class_names` HAS EXACTLY ONE SOURCE
────────────────────────────────────────────────────────────────────────────

Three lists could disagree about what output index 2 of a segmentation model
means: the labels typed into this record at registration, the labels Volume
Vision holds on its `TrainedModel`, and the labels an iOS app cached beside a
compiled model months ago. Nothing forced them to agree, and when they did not
the failure was silent — inference kept returning confident numbers against the
wrong names.

A **model bundle** is the fix: one zip carrying `model.mlmodel` and a
`manifest.json` written by Volume Vision at export time, in model-output-index
order, from the same training config the weights were produced under. The
functions below are the reading of it — `looks_like_bundle` (the PK magic in the
first four bytes, which is what tells a bundle from a raw `.mlmodel` without
trusting a file extension anybody can rename), `read_bundle`,
`validate_bundle_manifest` and `reconcile_bundle_manifest`.

**The bundle wins.** `reconcile_bundle_manifest` reports what it is about to
overwrite rather than merging: a manifest that came out of training is a
statement about the weights in the same zip, and a list typed into this record
is a statement about somebody's memory of them. What it does NOT overwrite is
the three fields that are this record's own identity and not training's to
assign — `version`, `piecework_activity`, and a `source_uuid` that is already
set and disagrees. A disagreeing `source_uuid` means the wrong bundle is being
attached to this record, which is a mistake to refuse rather than a value to
reconcile; `tools/ml_model.py` turns that into the refusal.

Nothing here reads a file or a database. `read_bundle` takes the bytes and
returns a dict, so the same function serves an operator's upload and a pull
straight off Volume Vision without either path getting its own parser.

────────────────────────────────────────────────────────────────────────────
v0.68.0: THE SCHEMA THE MANIFEST IS HELD TO, AND MIGRATING TO IT
────────────────────────────────────────────────────────────────────────────

v0.59.0 defined what a bundle must carry (`validate_bundle_manifest`:
`class_names`, and nothing else, because that is all a training exporter can
fairly be asked for). It did not define what THIS APP STORES, and the answer
turned out to be three different things depending on which release registered
the record — a v0.43.0 record with labels typed onto it and no manifest at all,
a v0.52.0 record with a raw file attached, and a v0.59.0 record with whatever
JSON the exporter happened to write. `get_active_model` served all three and a
client could not tell them apart.

`MANIFEST_SCHEMA_VERSION` is the answer, and `validate_manifest_schema` is the
check. On top of the bundle contract it requires two things this app can always
supply itself: `schema_version`, so a cached manifest says what shape it is, and
`userDefined` — CoreML's own string-to-string metadata dictionary, carrying the
label list in the spelling an iOS client reads off the compiled model. That
mirror is the point: labels that agree with the weights' own embedded metadata
have been corroborated by something other than whoever typed them.

`manifest_origin` is the third addition and the one that changed an existing
answer. Until now "this record has a `bundle_manifest`" and "the attached file
is a zip" were the same fact, and `is_bundle` was computed from the first. A
migrated record breaks that: it has a manifest built from its own fields and a
raw `.mlmodel` beside it, and a client trusting the old proxy would try to
unzip the weights. `is_bundle_payload` reads the origin instead, and treats a
manifest that predates the field as the bundle it could only have been.

`normalize_manifest` is additive and never lossy — every key the exporter wrote
survives, and what is added is derived. `reconcile_bundle_manifest` runs it on
the way in, so a bundle pulled today is already current; `manifest_migration_
report` is what `tools/ml_model.py` reads to tell an operator which older
records are not.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import zipfile

# `as_mariadb_datetime` LIVES IN `datetimes.py` NOW AND IS RE-EXPORTED HERE.
# v0.59.1 wrote it in this module because the failing case was Volume Vision's
# `training_completed_at`; v0.59.2 found the same rule wanted at a second
# boundary — an iPhone's `ISO8601DateFormatter` stamping a bucket capture — and
# `api/mobile.py` importing the ML model registry to convert a timestamp would
# have claimed a dependency that is not one. The name stays reachable here
# because `reconcile_bundle_manifest` below and `tools/ml_model.py` already
# call it, and because `MARIADB_DATETIME_FORMAT` is this module's own answer to
# "what shape does a Datetime column want".
from .datetimes import MARIADB_DATETIME_FORMAT, as_mariadb_datetime, as_site_datetime  # noqa: F401

STATUS_DRAFT = "Draft"
STATUS_ACTIVE = "Active"
STATUS_DEPRECATED = "Deprecated"
STATUS_ARCHIVED = "Archived"
STATUSES = (STATUS_DRAFT, STATUS_ACTIVE, STATUS_DEPRECATED, STATUS_ARCHIVED)

MODEL_KIND_CLASSIFICATION = "Classification"
MODEL_KIND_SEGMENTATION = "Segmentation"
MODEL_KIND_DETECTION = "Detection"
MODEL_KIND_OTHER = "Other"
MODEL_KINDS = (MODEL_KIND_CLASSIFICATION, MODEL_KIND_SEGMENTATION, MODEL_KIND_DETECTION, MODEL_KIND_OTHER)

MODEL_FORMAT_COREML = "CoreML"
MODEL_FORMAT_ONNX = "ONNX"
MODEL_FORMAT_TENSORFLOW = "TensorFlow"
MODEL_FORMAT_OTHER = "Other"
MODEL_FORMATS = (MODEL_FORMAT_COREML, MODEL_FORMAT_ONNX, MODEL_FORMAT_TENSORFLOW, MODEL_FORMAT_OTHER)
DEFAULT_MODEL_FORMAT = MODEL_FORMAT_COREML

#: The file inside a bundle that carries the class names. Volume Vision writes
#: it at export time; every other name in the zip is payload.
BUNDLE_MANIFEST_NAME = "manifest.json"

#: The first four bytes of a zip. `PK\x03\x04` is a local file header (every
#: bundle with anything in it), `PK\x05\x06` an empty archive, `PK\x07\x08` a
#: spanned one. Checked instead of the file extension because the extension is
#: whatever the operator's browser called the download, and a `.mlmodel` that is
#: really a zip fails on the phone, hours later, as a CoreML compile error.
BUNDLE_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

#: Extensions a bundle's model payload may have, most specific first. Used only
#: to name which entry is the model and to infer `model_format` when the
#: manifest does not state one.
_MODEL_SUFFIXES = (
	(".mlpackage", MODEL_FORMAT_COREML),
	(".mlmodelc", MODEL_FORMAT_COREML),
	(".mlmodel", MODEL_FORMAT_COREML),
	(".onnx", MODEL_FORMAT_ONNX),
	(".tflite", MODEL_FORMAT_TENSORFLOW),
	(".pb", MODEL_FORMAT_TENSORFLOW),
)

#: What `manifest_source` says when the labels came out of a bundle, and when
#: they did not. The record itself carries the sentence, so somebody reading the
#: ML Model form in a year can see where its `class_names` came from without
#: reconstructing which release attached the file.
MANIFEST_SOURCE_BUNDLE = "class_names source: bundle manifest from VV training"
MANIFEST_SOURCE_RECORD = "class_names source: entered on this site — no bundle manifest"

#: v0.68.0. The third answer, and the reason `migrate_model_format` needed one:
#: a record whose manifest was BUILT OUT OF ITS OWN FIELDS has a manifest and
#: has never had a bundle, and saying either of the two sentences above about it
#: would be false. This says what actually happened.
MANIFEST_SOURCE_MIGRATED = (
	"class_names source: this record's own fields, restructured into a manifest by "
	"migrate_model_format — no bundle manifest, so the labels remain unverified against the weights"
)

#: The version `normalize_manifest` writes and `validate_manifest_schema` holds
#: a stored manifest to. It rides ON THE MANIFEST rather than on the record
#: because the manifest is the thing that travels: a phone that cached one
#: months ago can tell from the JSON alone whether it is reading the shape it
#: was written against, without asking this site what release it is running.
MANIFEST_SCHEMA_VERSION = "1.0"

#: Where a stored `bundle_manifest` came from — and, because it is the same
#: question, WHAT THE ATTACHED BYTES ARE. `bundle` means the manifest was read
#: out of a zip, so the payload is that zip and a client must unpack it before
#: compiling anything. `record` means the manifest was assembled from this
#: record's fields by `migrate_model_format`, so the payload (if there is one at
#: all) is a raw model to compile directly.
#:
#: THE DISTINCTION EXISTS BECAUSE MIGRATION BROKE THE OLD PROXY. Through
#: v0.59.x, "this record has a bundle_manifest" and "the attached file is a zip"
#: were the same fact, and `is_bundle` was computed from the first to answer the
#: second. A migrated record has a manifest and a raw file, so a client reading
#: the old proxy would try to unzip a `.mlmodel` — the exact silent-wrong-answer
#: class of bug the bundle format was introduced to close. See
#: `is_bundle_payload`: a manifest carrying no `manifest_origin` at all predates
#: this release and can only have come from a bundle, so it still reads `bundle`.
MANIFEST_ORIGIN_BUNDLE = "bundle"
MANIFEST_ORIGIN_RECORD = "record"
MANIFEST_ORIGINS = (MANIFEST_ORIGIN_BUNDLE, MANIFEST_ORIGIN_RECORD)

#: CoreML's own metadata dictionary — what an iOS client reads off the COMPILED
#: MODEL as `modelDescription.metadata[.creatorDefinedKey]`, without this app,
#: this manifest, or a network. Carrying it in the manifest too is what makes
#: the two checkable against each other: a manifest whose labels match the
#: weights' own embedded metadata is a manifest that has been corroborated by
#: something other than whoever typed it.
#:
#: IT IS STRING → STRING, which is CoreML's constraint and not a choice made
#: here, so the label array is ENCODED into one value rather than nested — see
#: `encode_user_defined_class_names`.
MANIFEST_USER_DEFINED_KEY = "userDefined"
USER_DEFINED_CLASS_NAMES_KEY = "class_names"
USER_DEFINED_SEPARATOR = ","

#: `version` is compared and sorted as a string everywhere in this app (the
#: same way ERPNext compares one), so the only thing worth policing is that it
#: looks like a version at all — digits and dots, one to four segments: "3",
#: "3.2", "3.2.1". Not full semver (no pre-release/build suffix) because
#: Volume Vision's own `TrainedModel.version` is typically an integer or a
#: short dotted counter, not a package version.
_VERSION_PATTERN = re.compile(r"^\d+(\.\d+){0,3}$")
_UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _clean(value) -> str:
	return str(value or "").strip()


def _parse_json_value(raw, expected_type: type):
	"""`(parsed, error)` — `raw` may already be `expected_type`, a JSON string
	of it, or empty. `error` is `None` on success; `parsed` is `None` on
	failure or when `raw` is empty."""
	if raw is None or raw == "":
		return None, None
	if isinstance(raw, expected_type):
		return raw, None
	if isinstance(raw, str):
		try:
			parsed = json.loads(raw)
		except (TypeError, ValueError):
			return None, "must be valid JSON"
		if not isinstance(parsed, expected_type):
			return None, f"must be a JSON {'array' if expected_type is list else 'object'}"
		return parsed, None
	return (
		None,
		f"must be a JSON {'array' if expected_type is list else 'object'} or a {expected_type.__name__}",
	)


def class_names_of(model_doc: dict) -> list:
	"""`class_names` read as a list, or `[]` if absent or unparseable.

	Lenient by design — this is a read path (`build_model_manifest`), not a
	validation path. `validate_model_registration` is where an unparseable
	value becomes a reported error instead of a silently empty list.
	"""
	parsed, _error = _parse_json_value((model_doc or {}).get("class_names"), list)
	return parsed if parsed is not None else []


def metrics_of(model_doc: dict) -> dict:
	"""`metrics` read as a dict, or `{}` if absent or unparseable."""
	parsed, _error = _parse_json_value((model_doc or {}).get("metrics"), dict)
	return parsed if parsed is not None else {}


def bundle_manifest_of(model_doc: dict) -> dict:
	"""The stored `bundle_manifest` read as a dict, or `{}`.

	Lenient like `class_names_of` and for the same reason: this is a read path.
	A record whose `bundle_manifest` is empty is a record whose binary is a raw
	model rather than a bundle — not an error, just the older shape.
	"""
	parsed, _error = _parse_json_value((model_doc or {}).get("bundle_manifest"), dict)
	return parsed if parsed is not None else {}


def is_bundle_payload(model_doc: dict) -> bool:
	"""Whether the bytes attached to this record are a BUNDLE ZIP.

	Not "does this record have a manifest" — see `MANIFEST_ORIGIN_BUNDLE` on why
	those stopped being the same question in v0.68.0. A manifest with no
	`manifest_origin` predates this release, and before it a stored manifest
	could only have been read out of a zip, so it answers `True`.

	Read from the record rather than from the bytes because the callers are
	`_describe` and `build_model_manifest`, which are answering "what will a
	client find when it pulls" without opening a file to do it.
	`get_model_file_chunk` sniffs the actual bytes, which is the check that
	settles a disagreement between the two.
	"""
	manifest = bundle_manifest_of(model_doc)
	if not manifest:
		return False
	origin = _clean(manifest.get("manifest_origin"))
	if not origin:
		return True
	return origin == MANIFEST_ORIGIN_BUNDLE


# ── reading a model bundle ───────────────────────────────────────────────


def looks_like_bundle(content: bytes) -> bool:
	"""Whether `content` starts with a zip's magic number.

	Four bytes, no extension, no MIME type. See `BUNDLE_MAGICS`.
	"""
	if not isinstance(content, (bytes, bytearray, memoryview)):
		return False
	head = bytes(content[:4])
	return any(head.startswith(magic) for magic in BUNDLE_MAGICS)


def _manifest_entry(names: list) -> str:
	"""The name of the manifest inside the zip, allowing for one wrapping folder.

	A zip built by `zip -r bundle.zip cherry_fill_v1/` puts everything under a
	directory, which is what `zip` does by default and what an operator who
	zipped a folder by hand will produce. Refusing that would be refusing the
	likeliest hand-built bundle over a detail Volume Vision's own exporter
	happens not to have.
	"""
	for name in names:
		if posixpath.basename(name) == BUNDLE_MANIFEST_NAME and name.count("/") <= 1:
			return name
	return ""


def _model_entry(names: list) -> tuple:
	"""`(entry_name, model_format)` for the model payload in the zip, or `("", "")`."""
	for suffix, model_format in _MODEL_SUFFIXES:
		for name in names:
			lowered = name.lower().rstrip("/")
			if lowered.endswith(suffix):
				return name, model_format
	return "", ""


def read_bundle(content: bytes) -> dict:
	"""Open `content` as a model bundle and report what is in it.

	Returns `is_bundle` (the magic number said zip), `entries` (every name in
	the archive), `manifest_entry`/`manifest` (the parsed `manifest.json`),
	`model_entry`/`model_format` (the payload the phone will compile), and
	`errors` — every reason this is not a usable bundle, as sentences.

	A zip with no `manifest.json` is an ERROR rather than a bundle with an empty
	manifest: the whole point of the format is that the labels travel with the
	weights, and a zip that carries only weights is a raw model in a wrapper
	that would be stored as if its provenance had been checked.
	"""
	result = {
		"is_bundle": looks_like_bundle(content),
		"entries": [],
		"manifest_entry": "",
		"manifest": {},
		"model_entry": "",
		"model_format": "",
		"errors": [],
	}
	if not result["is_bundle"]:
		return result

	try:
		archive = zipfile.ZipFile(io.BytesIO(bytes(content)))
	except (zipfile.BadZipFile, OSError, ValueError) as exc:
		result["errors"].append(
			f"the file begins like a zip but does not open as one ({type(exc).__name__}: {exc}) — "
			"it is likely a truncated download"
		)
		return result

	with archive:
		result["entries"] = list(archive.namelist())
		manifest_entry = _manifest_entry(result["entries"])
		if not manifest_entry:
			result["errors"].append(
				f"the zip has no {BUNDLE_MANIFEST_NAME} — a bundle carries the class names beside "
				"the weights, and a zip without one has no provenance to read"
			)
			return result
		result["manifest_entry"] = manifest_entry
		try:
			raw = archive.read(manifest_entry)
		except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
			result["errors"].append(f"{manifest_entry} could not be read ({type(exc).__name__}: {exc})")
			return result

	try:
		manifest = json.loads(raw.decode("utf-8"))
	except (UnicodeDecodeError, ValueError) as exc:
		result["errors"].append(f"{manifest_entry} is not valid UTF-8 JSON ({exc})")
		return result
	if not isinstance(manifest, dict):
		result["errors"].append(f"{manifest_entry} is a JSON {type(manifest).__name__}, not an object")
		return result

	result["manifest"] = manifest
	result["model_entry"], result["model_format"] = _model_entry(result["entries"])
	result["errors"].extend(validate_bundle_manifest(manifest))
	return result


def validate_bundle_manifest(manifest: dict) -> list:
	"""Every reason this manifest cannot be trusted as the source of `class_names`.

	Empty list means usable. Only `class_names` is REQUIRED — the rest of the
	manifest is metadata this app is glad to have and can do without, but a
	bundle whose labels are missing, empty, or not an ordered list of strings
	cannot answer the one question the format exists to answer.
	"""
	manifest = manifest or {}
	errors = []

	if "class_names" not in manifest:
		errors.append(
			f"{BUNDLE_MANIFEST_NAME} has no class_names — that list, in model-output-index order, "
			"is the reason the bundle format exists"
		)
		return errors

	class_names = manifest.get("class_names")
	if not isinstance(class_names, list):
		errors.append(
			f"{BUNDLE_MANIFEST_NAME}'s class_names is a {type(class_names).__name__}, not an ordered array"
		)
		return errors
	if not class_names:
		errors.append(f"{BUNDLE_MANIFEST_NAME}'s class_names is empty")
		return errors

	bad = [repr(value) for value in class_names if not isinstance(value, str) or not value.strip()]
	if bad:
		errors.append(
			f"{BUNDLE_MANIFEST_NAME}'s class_names has {len(bad)} entry/entries that are not label "
			f"strings: {', '.join(bad[:5])}"
		)

	metrics = manifest.get("metrics")
	if metrics is not None and not isinstance(metrics, dict):
		errors.append(f"{BUNDLE_MANIFEST_NAME}'s metrics is a {type(metrics).__name__}, not an object")

	return errors


def manifest_source_note(manifest: dict, file_name: str = "") -> str:
	"""The sentence `manifest_source` carries when a bundle supplied the labels."""
	manifest = manifest or {}
	detail = []
	uuid = _clean(manifest.get("uuid"))
	if uuid:
		detail.append(f"uuid {uuid}")
	version = _clean(manifest.get("version"))
	if version:
		detail.append(f"training version {version}")
	completed = _clean(manifest.get("training_completed_at"))
	if completed:
		detail.append(f"trained {completed}")
	if file_name:
		detail.append(f"{BUNDLE_MANIFEST_NAME} in {file_name}")
	return MANIFEST_SOURCE_BUNDLE + (f" ({', '.join(detail)})" if detail else "")


def reconcile_bundle_manifest(
	model_doc: dict, manifest: dict, file_name: str = "", *, tz_name: str = "UTC"
) -> dict:
	"""What attaching this bundle changes on this record, and what it refuses to.

	Returns `updates` (field → new value, ready to set), `warnings` (every place
	the bundle disagreed with what was already there and won), and `conflicts`
	(the disagreements that are NOT the bundle's to settle — see the module
	docstring). A caller with a non-empty `conflicts` has the wrong bundle for
	this record and should stop; `warnings` are for the audit trail and are not
	a reason to refuse anything.

	`file_size_bytes` is deliberately not set here: this function sees a
	manifest, not the zip, and the size that matters is the stored file's.

	`tz_name` is the site's zone, passed in rather than read so this module
	stays pure. v0.199.0: `training_completed_at` lands in it, beside a
	`deployed_at` that `activate_model` writes with `frappe.utils.now()` —
	site-local. The default, UTC, is the old behaviour exactly.
	"""
	model_doc = model_doc or {}
	manifest = manifest or {}
	updates: dict = {}
	warnings: list = []
	conflicts: list = []

	class_names = manifest.get("class_names")
	if isinstance(class_names, list) and class_names:
		existing = class_names_of(model_doc)
		updates["class_names"] = list(class_names)
		if existing and existing != list(class_names):
			warnings.append(
				f"class_names on this record were {json.dumps(existing)} and the bundle's manifest "
				f"says {json.dumps(list(class_names))}. The bundle wins — it was written at export "
				"time from the config the weights were trained under, and the record's list was "
				"typed by hand. The old list is in this call's `previous` block."
			)

	metrics = manifest.get("metrics")
	if isinstance(metrics, dict) and metrics:
		existing_metrics = metrics_of(model_doc)
		updates["metrics"] = dict(metrics)
		if existing_metrics and existing_metrics != dict(metrics):
			warnings.append("metrics on this record differed from the bundle's; the bundle's replaced them.")

	model_kind = _clean(manifest.get("model_kind"))
	if model_kind:
		if model_kind not in MODEL_KINDS:
			warnings.append(
				f"the manifest's model_kind {model_kind!r} is not one of {', '.join(MODEL_KINDS)} "
				"and was not applied."
			)
		else:
			existing_kind = _clean(model_doc.get("model_kind"))
			if existing_kind != model_kind:
				updates["model_kind"] = model_kind
				if existing_kind:
					warnings.append(
						f"model_kind was {existing_kind!r} and the bundle says {model_kind!r}; the "
						"bundle wins."
					)

	model_format = _clean(manifest.get("model_format")) or _clean(manifest.get("format"))
	if model_format and model_format in MODEL_FORMATS:
		existing_format = _clean(model_doc.get("model_format"))
		if existing_format != model_format:
			updates["model_format"] = model_format
			if existing_format:
				warnings.append(
					f"model_format was {existing_format!r} and the bundle says {model_format!r}; the "
					"bundle wins."
				)

	for field, key in (("taxonomy_schema", "taxonomy_schema"), ("taxonomy_version", "taxonomy_version")):
		value = _clean(manifest.get(key))
		if value and not _clean(model_doc.get(field)):
			updates[field] = value

	# FILLED WHEN EMPTY, NEVER OVERWRITTEN. A datetime already on the record was
	# written by somebody who registered this model; the manifest's is the same
	# fact in whatever format the exporter used, and rewriting one well-formed
	# timestamp with another spelling of itself churns the record for nothing.
	#
	# AND CONVERTED, because "whatever format the exporter used" is ISO 8601
	# with a `T` and a `Z` in it, and a Frappe Datetime column is a MariaDB
	# DATETIME that refuses one — see `as_mariadb_datetime`. Storing the raw
	# string failed the whole pull at the save, after the model had already come
	# down the wire.
	completed = _clean(manifest.get("training_completed_at"))
	if completed and not _clean(model_doc.get("training_completed_at")):
		converted = as_site_datetime(completed, tz_name)
		if converted:
			updates["training_completed_at"] = converted
		else:
			warnings.append(
				f"the manifest's training_completed_at ({completed!r}) is not a timestamp this app "
				"can read, so it was left unset rather than written — a Datetime column refuses it "
				"and the refusal would fail the whole attach. Everything else in the manifest was "
				"applied."
			)

	manifest_uuid = _clean(manifest.get("uuid"))
	record_uuid = _clean(model_doc.get("source_uuid"))
	if manifest_uuid and not record_uuid:
		if _UUID_PATTERN.match(manifest_uuid):
			updates["source_uuid"] = manifest_uuid
		else:
			warnings.append(
				f"the manifest's uuid {manifest_uuid!r} is not a well-formed UUID and was not "
				"adopted as source_uuid."
			)
	elif manifest_uuid and record_uuid and manifest_uuid != record_uuid:
		conflicts.append(
			f"this record's source_uuid is {record_uuid} and the bundle's manifest says "
			f"{manifest_uuid}. That is a different trained model, not a newer file for this one — "
			"attaching it would make every downstream cache keyed on the uuid wrong."
		)

	manifest_version = _clean(manifest.get("version"))
	record_version = _clean(model_doc.get("version"))
	if manifest_version and record_version and manifest_version != record_version:
		warnings.append(
			f"the bundle's training version is {manifest_version!r} and this record is version "
			f"{record_version!r}. version is left alone — it is half of the (company, model_name, "
			"version) key this record is unique on, and update_model is where it changes."
		)

	manifest_activity = _clean(manifest.get("piecework_activity"))
	record_activity = _clean(model_doc.get("piecework_activity"))
	if manifest_activity and record_activity and manifest_activity != record_activity:
		warnings.append(
			f"the bundle says it was trained for {manifest_activity!r} and this record is deployed "
			f"for {record_activity!r}. piecework_activity is left alone — it is the identity "
			"get_active_model is queried by, not a value training assigns."
		)

	# NORMALIZED ON THE WAY IN, v0.68.0. Storing the manifest exactly as Volume
	# Vision wrote it meant every freshly pulled record was immediately outdated
	# by `list_models_needing_migration`'s reckoning — a nonsense state, and one
	# an operator would reasonably read as a bug in the migration tools rather
	# than as the truth about the format. Normalizing here is ADDITIVE: every key
	# the exporter wrote survives untouched, and what is added is derived from
	# the manifest and this record. See `normalize_manifest`.
	updates["bundle_manifest"] = normalize_manifest(manifest, model_doc, origin=MANIFEST_ORIGIN_BUNDLE)
	updates["manifest_source"] = manifest_source_note(manifest, file_name)

	return {"updates": updates, "warnings": warnings, "conflicts": conflicts}


# ── the current manifest schema, and migrating a record to it ────────────


def encode_user_defined_class_names(class_names: list) -> str:
	"""`class_names` as the one string CoreML's `userDefined` can hold.

	Comma-joined, which is the convention every CoreML exporter uses and what an
	iOS client splitting the compiled model's own metadata already expects —
	EXCEPT when a label would not survive the round trip, where the whole array
	is written as JSON instead. A label containing a comma or padded with spaces
	is unusual and probably a mistake, but silently splitting "bucket, full" into
	two labels would renumber every output index after it, which is precisely the
	class of silent wrong answer the bundle format exists to prevent.
	`decode_user_defined_class_names` reads either spelling.
	"""
	labels = [str(value) for value in (class_names or [])]
	if any(USER_DEFINED_SEPARATOR in label or label != label.strip() for label in labels):
		return json.dumps(labels)
	return USER_DEFINED_SEPARATOR.join(labels)


def decode_user_defined_class_names(value) -> list:
	"""The label list back out of a `userDefined` value, in either spelling.

	Lenient: an unreadable value returns `[]` rather than raising, and
	`validate_manifest_schema` is where "the mirror does not match the array"
	becomes a reported error.
	"""
	if isinstance(value, list):
		return [str(item) for item in value]
	text = str(value if value is not None else "").strip()
	if not text:
		return []
	if text.startswith("["):
		try:
			parsed = json.loads(text)
		except (TypeError, ValueError):
			return []
		return [str(item) for item in parsed] if isinstance(parsed, list) else []
	return [part.strip() for part in text.split(USER_DEFINED_SEPARATOR)]


def manifest_origin_for(model_doc: dict) -> str:
	"""Which origin a migration of this record would write.

	A manifest that says which it is, is believed. A manifest that says nothing
	predates v0.68.0, and before v0.68.0 the only thing that ever wrote a
	`bundle_manifest` was an attached bundle — so it is one. A record with no
	manifest at all has nothing to migrate FROM except its own fields, which is
	`record`.
	"""
	manifest = bundle_manifest_of(model_doc)
	origin = _clean(manifest.get("manifest_origin"))
	if origin in MANIFEST_ORIGINS:
		return origin
	return MANIFEST_ORIGIN_BUNDLE if manifest else MANIFEST_ORIGIN_RECORD


def normalize_manifest(
	manifest: dict, model_doc: dict | None = None, origin: str = MANIFEST_ORIGIN_RECORD
) -> dict:
	"""`manifest` restated in the current schema, filled out from `model_doc`.

	ADDITIVE, NEVER LOSSY. Every key the caller passed in survives with the value
	it had; what this adds is `schema_version`, `manifest_origin`, the
	`userDefined` mirror, and — only where the manifest is SILENT — the facts the
	record already holds. A manifest that disagrees with the record is left
	disagreeing: `reconcile_bundle_manifest` is where that argument is settled,
	and settling it twice in two places is how the two answers drift apart.

	`origin` NEVER DOWNGRADES. A manifest that already claims `bundle` keeps it
	whatever the caller passed, because that claim is about the stored bytes and
	a caller that guessed `record` would be telling a phone to compile a zip.
	The reverse is not symmetric — a caller that knows it read this out of a zip
	is entitled to say so about a manifest that never said either way.

	An unrecognised `model_format` is PRESERVED rather than replaced with the
	default. Rewriting "PyTorch" to "CoreML" would make the manifest valid by
	making it false; `validate_manifest_schema` reports it and
	`manifest_migration_report` turns it into a blocker an operator settles with
	`update_model`.
	"""
	manifest = dict(manifest or {})
	model_doc = model_doc or {}

	origin = _clean(origin)
	if origin not in MANIFEST_ORIGINS:
		origin = MANIFEST_ORIGIN_RECORD
	if _clean(manifest.get("manifest_origin")) == MANIFEST_ORIGIN_BUNDLE:
		origin = MANIFEST_ORIGIN_BUNDLE
	manifest["manifest_origin"] = origin
	manifest["schema_version"] = MANIFEST_SCHEMA_VERSION

	class_names = manifest.get("class_names")
	if not (isinstance(class_names, list) and class_names):
		class_names = class_names_of(model_doc)
	class_names = [str(label) for label in class_names]
	if class_names:
		manifest["class_names"] = class_names

	for key, field in (
		("uuid", "source_uuid"),
		("name", "model_name"),
		("version", "version"),
		("model_kind", "model_kind"),
		("piecework_activity", "piecework_activity"),
		("taxonomy_schema", "taxonomy_schema"),
		("taxonomy_version", "taxonomy_version"),
		("training_completed_at", "training_completed_at"),
	):
		if _clean(manifest.get(key)):
			continue
		value = _clean(model_doc.get(field))
		if value:
			manifest[key] = value

	model_format = _clean(manifest.get("model_format")) or _clean(manifest.get("format"))
	if not model_format:
		model_format = _clean(model_doc.get("model_format")) or DEFAULT_MODEL_FORMAT
	manifest["model_format"] = model_format

	metrics = manifest.get("metrics")
	if metrics is None or (isinstance(metrics, dict) and not metrics):
		record_metrics = metrics_of(model_doc)
		if record_metrics:
			manifest["metrics"] = record_metrics

	user_defined = manifest.get(MANIFEST_USER_DEFINED_KEY)
	user_defined = (
		{str(key): str(value) for key, value in user_defined.items()}
		if isinstance(user_defined, dict)
		else {}
	)
	# THE MIRROR IS REWRITTEN, NOT MERGED. Two spellings of the label list that
	# disagree is worse than one, and the array is what every other function in
	# this module treats as canonical.
	user_defined[USER_DEFINED_CLASS_NAMES_KEY] = encode_user_defined_class_names(class_names)
	for key in (
		"uuid",
		"name",
		"version",
		"model_kind",
		"model_format",
		"piecework_activity",
		"taxonomy_schema",
		"taxonomy_version",
	):
		if _clean(user_defined.get(key)):
			continue
		value = _clean(manifest.get(key))
		if value:
			user_defined[key] = value
	manifest[MANIFEST_USER_DEFINED_KEY] = user_defined

	return manifest


def validate_manifest_schema(manifest: dict) -> list:
	"""Every reason this manifest is not in the CURRENT schema. Empty means it is.

	A SUPERSET OF `validate_bundle_manifest`, and deliberately a separate
	function rather than a stricter version of it. `validate_bundle_manifest` is
	the contract a bundle arriving from Volume Vision is held to — `class_names`
	and nothing else, because that is all an exporter can be asked for and
	tightening it would start refusing bundles that are fine. This is the
	contract on what THIS APP STORES, after normalization, and it is stricter
	because everything it adds is something this app is able to add itself.
	"""
	manifest = manifest or {}
	errors = list(validate_bundle_manifest(manifest))

	schema_version = _clean(manifest.get("schema_version"))
	if not schema_version:
		errors.append(
			"the manifest has no schema_version — it predates v0.68.0's manifest schema, and the "
			f"current one is {MANIFEST_SCHEMA_VERSION}"
		)
	elif schema_version != MANIFEST_SCHEMA_VERSION:
		errors.append(
			f"the manifest's schema_version is {schema_version!r} and this app writes and reads "
			f"{MANIFEST_SCHEMA_VERSION!r}"
		)

	origin = _clean(manifest.get("manifest_origin"))
	if not origin:
		errors.append(
			"the manifest has no manifest_origin, so nothing in it says whether the attached bytes "
			f"are a bundle zip to unpack or a raw model to compile — it must be one of "
			f"{', '.join(MANIFEST_ORIGINS)}"
		)
	elif origin not in MANIFEST_ORIGINS:
		errors.append(
			f"the manifest's manifest_origin is {origin!r}, not one of {', '.join(MANIFEST_ORIGINS)}"
		)

	declared = manifest.get("class_names")
	declared = [str(label) for label in declared] if isinstance(declared, list) else []

	user_defined = manifest.get(MANIFEST_USER_DEFINED_KEY)
	if user_defined is None:
		errors.append(
			f"the manifest has no {MANIFEST_USER_DEFINED_KEY} block — that is CoreML's own metadata "
			"dictionary, the one an iOS client reads off the compiled model itself, and a manifest "
			"without it cannot be corroborated against the weights"
		)
	elif not isinstance(user_defined, dict):
		errors.append(
			f"the manifest's {MANIFEST_USER_DEFINED_KEY} is a {type(user_defined).__name__}, not an object"
		)
	else:
		not_strings = sorted(key for key, value in user_defined.items() if not isinstance(value, str))
		if not_strings:
			errors.append(
				f"{MANIFEST_USER_DEFINED_KEY} is CoreML's string-to-string dictionary and "
				f"{len(not_strings)} of its values are not strings: {', '.join(not_strings[:5])}"
			)
		if USER_DEFINED_CLASS_NAMES_KEY not in user_defined:
			errors.append(
				f"{MANIFEST_USER_DEFINED_KEY} has no {USER_DEFINED_CLASS_NAMES_KEY} — that mirror is "
				"the whole reason the block is carried, and without it there is nothing to check the "
				"label array against"
			)
		else:
			mirrored = decode_user_defined_class_names(user_defined.get(USER_DEFINED_CLASS_NAMES_KEY))
			if mirrored != declared:
				errors.append(
					f"{MANIFEST_USER_DEFINED_KEY}.{USER_DEFINED_CLASS_NAMES_KEY} says "
					f"{json.dumps(mirrored)} and the manifest's class_names array says "
					f"{json.dumps(declared)}. Two spellings of one list that disagree cannot both be "
					"the output-index order"
				)

	model_format = _clean(manifest.get("model_format"))
	if model_format and model_format not in MODEL_FORMATS:
		errors.append(
			f"the manifest's model_format {model_format!r} is not one of {', '.join(MODEL_FORMATS)}"
		)

	model_kind = _clean(manifest.get("model_kind"))
	if model_kind and model_kind not in MODEL_KINDS:
		errors.append(f"the manifest's model_kind {model_kind!r} is not one of {', '.join(MODEL_KINDS)}")

	return errors


def manifest_migration_report(model_doc: dict) -> dict:
	"""Whether this record's manifest is in the current schema, and what stops it.

	`reasons` is why it is outdated — every one of them something
	`migrate_model_format` fixes on its own. `blockers` is the other kind: a
	disagreement or an absence this function cannot invent a value for, where
	migrating would mean guessing. `can_migrate` is `not blockers`, and a record
	with blockers is reported rather than half-migrated.

	Takes a plain dict, so `list_models_needing_migration` can run it over a
	`get_all` result without loading a document per row.
	"""
	model_doc = model_doc or {}
	raw_manifest = model_doc.get("bundle_manifest")
	parsed, parse_error = _parse_json_value(raw_manifest, dict)
	manifest = parsed or {}

	reasons: list = []
	blockers: list = []

	if parse_error:
		reasons.append(
			f"bundle_manifest on this record {parse_error} — whatever is in that field is not a "
			"manifest anything can read, and migrating replaces it"
		)
	elif not manifest:
		reasons.append(
			"this record has no bundle_manifest at all: it is the pre-v0.59.0 shape, where the "
			"labels lived only in class_names and nothing recorded where they came from"
		)
	else:
		reasons.extend(validate_manifest_schema(manifest))

	record_class_names, class_names_error = _parse_json_value(model_doc.get("class_names"), list)
	record_class_names = [str(label) for label in record_class_names] if record_class_names else []
	manifest_class_names = manifest.get("class_names")
	manifest_class_names = (
		[str(label) for label in manifest_class_names] if isinstance(manifest_class_names, list) else []
	)

	if class_names_error:
		reasons.append(f"class_names on this record {class_names_error}")

	if record_class_names and manifest_class_names and record_class_names != manifest_class_names:
		reasons.append(
			f"class_names on this record say {json.dumps(record_class_names)} and its stored manifest "
			f"says {json.dumps(manifest_class_names)}. One of those is what an output index means and "
			"the other is not"
		)

	if not record_class_names and not manifest_class_names:
		blockers.append(
			"there are no class_names anywhere on this record — not on the record, not in a manifest — "
			"so there is nothing to build a manifest out of. pull_model_from_vv gets them from the "
			"training run, or update_model(class_names=[...]) sets them by hand"
		)

	record_format = _clean(model_doc.get("model_format"))
	if record_format and record_format not in MODEL_FORMATS:
		blockers.append(
			f"model_format on this record is {record_format!r}, not one of {', '.join(MODEL_FORMATS)}. "
			"update_model(model_format=...) is where that is settled — migration will not guess it"
		)
	manifest_format = _clean(manifest.get("model_format")) or _clean(manifest.get("format"))
	if manifest_format and manifest_format not in MODEL_FORMATS:
		blockers.append(
			f"the stored manifest's model_format is {manifest_format!r}, not one of "
			f"{', '.join(MODEL_FORMATS)}. Migration preserves it rather than overwriting it with a "
			"default, so it has to be corrected first"
		)

	origin = manifest_origin_for(model_doc)
	return {
		"name": model_doc.get("name"),
		"model_name": model_doc.get("model_name"),
		"version": model_doc.get("version"),
		"status": model_doc.get("status"),
		"company": model_doc.get("company"),
		"piecework_activity": model_doc.get("piecework_activity"),
		"has_manifest": bool(manifest),
		"schema_version": _clean(manifest.get("schema_version")) or None,
		"manifest_origin": origin,
		"manifest_source": _clean(model_doc.get("manifest_source")) or None,
		"class_names_count": len(manifest_class_names or record_class_names),
		"needs_migration": bool(reasons),
		"reasons": reasons,
		"can_migrate": not blockers,
		"blockers": blockers,
	}


# ── validating a registration ────────────────────────────────────────────


def validate_model_registration(model_doc: dict) -> list:
	"""Every reason `model_doc` cannot become (or remain) an ML Model record.

	Empty list means valid. Checks only what this module can check without a
	database: required fields, recognisable shapes for `version` and
	`source_uuid`, and that `class_names`/`metrics` are the JSON shape they
	claim to be. Uniqueness (one Active model per company + activity) needs a
	database read and lives in `check_model_conflicts` instead.
	"""
	model_doc = model_doc or {}
	errors = []

	if not _clean(model_doc.get("model_name")):
		errors.append("model_name is required")

	version = _clean(model_doc.get("version"))
	if not version:
		errors.append("version is required")
	elif not _VERSION_PATTERN.match(version):
		errors.append(
			f"version {version!r} is not a recognized format — digits and dots only, e.g. '3.2' or '1'"
		)

	if not _clean(model_doc.get("company")):
		errors.append("company is required")

	if not _clean(model_doc.get("piecework_activity")):
		errors.append(
			"piecework_activity is required — it is the other half of the (company, "
			"piecework_activity) pair that determines which model is Active"
		)

	source_uuid = _clean(model_doc.get("source_uuid"))
	if source_uuid and not _UUID_PATTERN.match(source_uuid):
		errors.append(f"source_uuid {source_uuid!r} is not a well-formed UUID")

	model_kind = model_doc.get("model_kind")
	if model_kind and model_kind not in MODEL_KINDS:
		errors.append(f"model_kind must be one of {', '.join(MODEL_KINDS)}; got {model_kind!r}")

	model_format = model_doc.get("model_format")
	if model_format and model_format not in MODEL_FORMATS:
		errors.append(f"model_format must be one of {', '.join(MODEL_FORMATS)}; got {model_format!r}")

	status = model_doc.get("status")
	if status and status not in STATUSES:
		errors.append(f"status must be one of {', '.join(STATUSES)}; got {status!r}")

	_class_names, class_names_error = _parse_json_value(model_doc.get("class_names"), list)
	if class_names_error:
		errors.append(f"class_names {class_names_error}")

	_metrics, metrics_error = _parse_json_value(model_doc.get("metrics"), dict)
	if metrics_error:
		errors.append(f"metrics {metrics_error}")

	return errors


# ── building the manifest an iOS app pulls ───────────────────────────────


def build_model_manifest(model_doc: dict) -> dict:
	"""The ERPNext record, reshaped to what Volume Vision's `to_dict()` returns.

	`uuid` is `source_uuid` when this model came from Volume Vision, falling
	back to the ERPNext docname for a model registered without one — an iOS
	cache keyed on `uuid` still gets a stable key either way, it is just not
	one Volume Vision itself would recognise.

	v0.59.0 adds `metadata.bundle`. An iOS app that has read this manifest knows
	BEFORE it starts pulling chunks whether the bytes are a zip it must unpack or
	a raw model it can compile directly, and it gets the manifest's own
	`preprocessing` and `class_roles` blocks — the two things that have no field
	of their own on this record — without unpacking anything. `is_bundle` false
	is the legacy shape and is not an error.
	"""
	model_doc = model_doc or {}
	bundle = bundle_manifest_of(model_doc)
	return {
		"uuid": _clean(model_doc.get("source_uuid")) or _clean(model_doc.get("name")),
		"name": model_doc.get("model_name"),
		"class_names": class_names_of(model_doc),
		"metadata": {
			"version": model_doc.get("version"),
			"kind": model_doc.get("model_kind"),
			"format": model_doc.get("model_format") or DEFAULT_MODEL_FORMAT,
			"piecework_activity": model_doc.get("piecework_activity"),
			"taxonomy_schema": model_doc.get("taxonomy_schema"),
			"taxonomy_version": model_doc.get("taxonomy_version"),
			"metrics": metrics_of(model_doc),
			"file_size_bytes": model_doc.get("file_size_bytes"),
			"source_server": model_doc.get("source_server"),
			# v0.52.0. Whether get_model_file_chunk has something to serve. An
			# iOS app checks this before ever asking for a chunk rather than
			# discovering "not attached yet" on the first read call — and,
			# deliberately, before it would ever fall back to source_server.
			"downloadable": bool(_clean(model_doc.get("model_file"))),
			"bundle": {
				# v0.68.0: NOT `bool(bundle)` any more. A migrated record has a
				# manifest and a raw model file, and a client that unzipped on
				# the old proxy would fail on the handset. See
				# `is_bundle_payload` and `MANIFEST_ORIGIN_BUNDLE`.
				"is_bundle": is_bundle_payload(model_doc),
				"manifest_source": _clean(model_doc.get("manifest_source"))
				or (MANIFEST_SOURCE_BUNDLE if bundle else MANIFEST_SOURCE_RECORD),
				"schema_version": _clean(bundle.get("schema_version")) or None,
				"manifest_origin": _clean(bundle.get("manifest_origin")) or None,
				# CoreML's own metadata dictionary, so a client can compare the
				# labels this app hands it against the ones embedded in the
				# weights it just compiled — without unpacking anything.
				"user_defined": bundle.get(MANIFEST_USER_DEFINED_KEY) or None,
				"class_roles": bundle.get("class_roles"),
				"preprocessing": bundle.get("preprocessing"),
				"training_version": bundle.get("version"),
			},
			"deployment_targets": model_doc.get("deployment_targets"),
			"status": model_doc.get("status"),
			"deployed_at": model_doc.get("deployed_at"),
			"training_completed_at": model_doc.get("training_completed_at"),
		},
	}


# ── checking for a conflicting Active model ──────────────────────────────


def check_model_conflicts(candidate: dict, existing_active: dict | None) -> dict:
	"""Would activating `candidate` collide with `existing_active`?

	`existing_active` is whatever the caller already found — the Active model,
	if any, for `candidate`'s (company, piecework_activity) pair — or `None`.
	This function makes no database read of its own; the read is
	`tools/ml_model.py`'s job precisely because it needs a database to do it.

	A candidate that reactivates ITSELF (same `name` as `existing_active`) is
	not a conflict: `activate_model` on an already-Active model is a no-op
	that should not report deprecating anything.
	"""
	candidate = candidate or {}
	if not existing_active:
		return {"conflict": False, "supersedes": None}

	same_record = bool(candidate.get("name")) and candidate.get("name") == existing_active.get("name")
	if same_record:
		return {"conflict": False, "supersedes": None}

	same_company = _clean(candidate.get("company")) == _clean(existing_active.get("company"))
	same_activity = _clean(candidate.get("piecework_activity")) == _clean(
		existing_active.get("piecework_activity")
	)
	if same_company and same_activity:
		return {
			"conflict": True,
			"supersedes": existing_active.get("model_name") or existing_active.get("name"),
		}
	return {"conflict": False, "supersedes": None}
