# SPDX-License-Identifier: MIT
"""ML Model Registry — v0.43.0. The pure engine, tested as a pure engine.

`model_registry.py` imports nothing from `frappe` and reads no database, the
same contract `budget_engine.py` is tested under in `test_budget.py`: every
function here takes a plain dict in and returns a plain dict out, so a
`unittest.TestCase` with no setup is the honest test for that. The impure
half — reading and writing an `ML Model` document, finding the Active sibling
a candidate would conflict with — lives in `tools/ml_model.py` and is exercised
through the tool layer elsewhere; this file is about the three claims the pure
engine makes.

THREE CLAIMS, ONE CLASS EACH, PLUS THE REGISTRATION ITSELF.

1. `ValidatingRegistration` — `validate_model_registration` finds exactly the
   reasons a candidate record cannot be registered (or re-saved), and reports
   none when there are none.
2. `BuildingTheManifest` — `build_model_manifest` reshapes an ERPNext record
   into Volume Vision's own `to_dict()` shape, tolerates a JSON field arriving
   as either a string or an already-parsed value, and falls back to the
   docname for `uuid` when there is no `source_uuid`.
3. `CheckingConflicts` — `check_model_conflicts` finds exactly the collision
   activating a candidate would cause against whatever the caller already
   found to be Active — no existing Active model, an unrelated one, and the
   candidate's own record are all "not a conflict"; the true collision names
   what it would supersede.
4. `ReadingABundle` — v0.59.0. `looks_like_bundle` decides on the bytes and not
   the file name, `read_bundle` opens a real zip built in the test and reports
   what is in it, `validate_bundle_manifest` finds exactly the reasons a
   manifest cannot be the source of `class_names`, and
   `reconcile_bundle_manifest` lets the bundle win over the record's own labels
   while refusing to settle the three things that are the record's identity.
5. `TheManifestSchema` — v0.68.0. `normalize_manifest` restates a manifest of
   any vintage in the current schema without losing a key,
   `validate_manifest_schema` finds exactly the reasons a stored manifest is
   not in it, `is_bundle_payload` stops confusing "has a manifest" with "the
   file is a zip", and `manifest_migration_report` separates what migration
   fixes from what it refuses to guess.
6. `ToolRegistration` — the thirteen tools exist in `registry.TOOLS`, split six
   read / seven write the way the releases describe, and the registry's total
   counts reflect them.
"""

import io
import json
import unittest
import zipfile

from erpnext_mcp import model_registry as engine


def model(**overrides):
	base = {
		"model_name": "Cherry Fill Detection",
		"version": "3.2",
		"company": "Highland Orchards",
		"piecework_activity": "bucket_fill_detection",
	}
	base.update(overrides)
	return base


class ValidatingRegistration(unittest.TestCase):
	def test_a_complete_candidate_is_valid(self):
		self.assertEqual(engine.validate_model_registration(model()), [])

	def test_model_name_is_required(self):
		errors = engine.validate_model_registration(model(model_name=""))
		self.assertTrue(any("model_name" in e for e in errors))

	def test_version_is_required(self):
		errors = engine.validate_model_registration(model(version=""))
		self.assertTrue(any("version is required" in e for e in errors))

	def test_version_must_look_like_a_version(self):
		errors = engine.validate_model_registration(model(version="v3.2-beta"))
		self.assertTrue(any("version" in e and "not a recognized format" in e for e in errors))

	def test_version_accepts_a_bare_integer(self):
		self.assertEqual(engine.validate_model_registration(model(version="3")), [])

	def test_version_accepts_several_dotted_segments(self):
		self.assertEqual(engine.validate_model_registration(model(version="3.2.1")), [])

	def test_company_is_required(self):
		errors = engine.validate_model_registration(model(company=""))
		self.assertTrue(any("company is required" in e for e in errors))

	def test_piecework_activity_is_required(self):
		errors = engine.validate_model_registration(model(piecework_activity=""))
		self.assertTrue(any("piecework_activity is required" in e for e in errors))

	def test_a_well_formed_source_uuid_is_valid(self):
		errors = engine.validate_model_registration(model(source_uuid="4b6f6e1a-2c3d-4e5f-8a9b-0c1d2e3f4a5b"))
		self.assertEqual(errors, [])

	def test_a_malformed_source_uuid_is_refused(self):
		errors = engine.validate_model_registration(model(source_uuid="not-a-uuid"))
		self.assertTrue(any("source_uuid" in e for e in errors))

	def test_an_empty_source_uuid_is_not_refused(self):
		"""Optional field — absent is fine, only a present-but-malformed value errors."""
		self.assertEqual(engine.validate_model_registration(model(source_uuid="")), [])

	def test_model_kind_must_be_a_known_value(self):
		errors = engine.validate_model_registration(model(model_kind="Regression"))
		self.assertTrue(any("model_kind" in e for e in errors))

	def test_model_kind_accepts_a_known_value(self):
		self.assertEqual(engine.validate_model_registration(model(model_kind="Detection")), [])

	def test_model_format_must_be_a_known_value(self):
		errors = engine.validate_model_registration(model(model_format="PyTorch"))
		self.assertTrue(any("model_format" in e for e in errors))

	def test_status_must_be_a_known_value(self):
		errors = engine.validate_model_registration(model(status="Retired"))
		self.assertTrue(any("status" in e for e in errors))

	def test_class_names_as_a_native_list_is_valid(self):
		errors = engine.validate_model_registration(model(class_names=["empty", "partial", "full"]))
		self.assertEqual(errors, [])

	def test_class_names_as_a_json_string_is_valid(self):
		errors = engine.validate_model_registration(model(class_names='["empty", "full"]'))
		self.assertEqual(errors, [])

	def test_class_names_that_is_not_valid_json_is_refused(self):
		errors = engine.validate_model_registration(model(class_names="{not json"))
		self.assertTrue(any("class_names" in e for e in errors))

	def test_class_names_that_is_a_json_object_rather_than_an_array_is_refused(self):
		errors = engine.validate_model_registration(model(class_names='{"a": 1}'))
		self.assertTrue(any("class_names" in e for e in errors))

	def test_metrics_as_a_native_dict_is_valid(self):
		self.assertEqual(engine.validate_model_registration(model(metrics={"accuracy": 0.94})), [])

	def test_metrics_that_is_a_json_array_rather_than_an_object_is_refused(self):
		errors = engine.validate_model_registration(model(metrics="[1, 2, 3]"))
		self.assertTrue(any("metrics" in e for e in errors))

	def test_every_missing_required_field_is_reported_at_once(self):
		errors = engine.validate_model_registration({})
		for expected in ("model_name", "version", "company", "piecework_activity"):
			with self.subTest(field=expected):
				self.assertTrue(any(expected in e for e in errors))

	def test_an_empty_dict_does_not_raise(self):
		self.assertIsInstance(engine.validate_model_registration({}), list)

	def test_none_does_not_raise(self):
		self.assertIsInstance(engine.validate_model_registration(None), list)


class BuildingTheManifest(unittest.TestCase):
	def test_uuid_prefers_source_uuid_over_the_docname(self):
		manifest = engine.build_model_manifest(
			model(name="MLM-2026-0001", source_uuid="4b6f6e1a-2c3d-4e5f-8a9b-0c1d2e3f4a5b")
		)
		self.assertEqual(manifest["uuid"], "4b6f6e1a-2c3d-4e5f-8a9b-0c1d2e3f4a5b")

	def test_uuid_falls_back_to_the_docname_when_there_is_no_source_uuid(self):
		manifest = engine.build_model_manifest(model(name="MLM-2026-0001"))
		self.assertEqual(manifest["uuid"], "MLM-2026-0001")

	def test_name_and_class_names_are_carried_through(self):
		manifest = engine.build_model_manifest(model(class_names=["empty", "partial", "full"]))
		self.assertEqual(manifest["name"], "Cherry Fill Detection")
		self.assertEqual(manifest["class_names"], ["empty", "partial", "full"])

	def test_class_names_absent_becomes_an_empty_list_not_none(self):
		manifest = engine.build_model_manifest(model())
		self.assertEqual(manifest["class_names"], [])

	def test_class_names_as_a_json_string_is_parsed(self):
		manifest = engine.build_model_manifest(model(class_names='["a", "b"]'))
		self.assertEqual(manifest["class_names"], ["a", "b"])

	def test_metadata_carries_version_kind_format_and_activity(self):
		manifest = engine.build_model_manifest(model(model_kind="Detection", model_format="CoreML"))
		metadata = manifest["metadata"]
		self.assertEqual(metadata["version"], "3.2")
		self.assertEqual(metadata["kind"], "Detection")
		self.assertEqual(metadata["format"], "CoreML")
		self.assertEqual(metadata["piecework_activity"], "bucket_fill_detection")

	def test_model_format_defaults_when_absent(self):
		manifest = engine.build_model_manifest(model())
		self.assertEqual(manifest["metadata"]["format"], engine.DEFAULT_MODEL_FORMAT)

	def test_metrics_as_a_json_string_is_parsed_into_the_metadata(self):
		manifest = engine.build_model_manifest(model(metrics='{"accuracy": 0.94}'))
		self.assertEqual(manifest["metadata"]["metrics"], {"accuracy": 0.94})

	def test_metrics_absent_becomes_an_empty_dict_not_none(self):
		manifest = engine.build_model_manifest(model())
		self.assertEqual(manifest["metadata"]["metrics"], {})

	def test_the_shape_matches_volume_visions_own_to_dict(self):
		"""uuid / name / class_names / metadata — no other top-level keys, so an
		iOS client's existing Volume Vision manifest parser reads this too."""
		manifest = engine.build_model_manifest(model())
		self.assertEqual(set(manifest.keys()), {"uuid", "name", "class_names", "metadata"})


class CheckingConflicts(unittest.TestCase):
	def test_no_existing_active_model_is_not_a_conflict(self):
		result = engine.check_model_conflicts(model(name="MLM-2026-0002"), None)
		self.assertEqual(result, {"conflict": False, "supersedes": None})

	def test_a_different_company_is_not_a_conflict(self):
		existing = model(name="MLM-2026-0001", company="Constancy Farms")
		candidate = model(name="MLM-2026-0002", company="Highland Orchards")
		result = engine.check_model_conflicts(candidate, existing)
		self.assertFalse(result["conflict"])

	def test_a_different_piecework_activity_is_not_a_conflict(self):
		existing = model(name="MLM-2026-0001", piecework_activity="harvest_quality")
		candidate = model(name="MLM-2026-0002", piecework_activity="bucket_fill_detection")
		result = engine.check_model_conflicts(candidate, existing)
		self.assertFalse(result["conflict"])

	def test_the_same_company_and_activity_is_a_conflict(self):
		existing = model(name="MLM-2026-0001", model_name="Cherry Fill Detection v3")
		candidate = model(name="MLM-2026-0002", model_name="Cherry Fill Detection v4")
		result = engine.check_model_conflicts(candidate, existing)
		self.assertTrue(result["conflict"])
		self.assertEqual(result["supersedes"], "Cherry Fill Detection v3")

	def test_supersedes_falls_back_to_the_docname_when_there_is_no_model_name(self):
		existing = {
			"name": "MLM-2026-0001",
			"company": "Highland Orchards",
			"piecework_activity": "bucket_fill_detection",
		}
		candidate = model(name="MLM-2026-0002")
		result = engine.check_model_conflicts(candidate, existing)
		self.assertEqual(result["supersedes"], "MLM-2026-0001")

	def test_reactivating_the_same_record_is_not_a_conflict(self):
		"""A candidate that IS the existing Active record — same `name` — is
		activate_model refreshing deployed_at, not a supersession."""
		existing = model(name="MLM-2026-0001")
		candidate = model(name="MLM-2026-0001")
		result = engine.check_model_conflicts(candidate, existing)
		self.assertEqual(result, {"conflict": False, "supersedes": None})

	def test_a_candidate_with_no_name_never_matches_by_accident(self):
		"""Two candidates that both happen to have no `name` must not be read as
		'the same record' — an unsaved candidate is never its own sibling."""
		existing = {"name": "", "company": "Highland Orchards", "piecework_activity": "bucket_fill_detection"}
		candidate = model(name="")
		result = engine.check_model_conflicts(candidate, existing)
		self.assertTrue(result["conflict"])


def bundle_bytes(manifest=None, entries=None, manifest_name="manifest.json", model_name="model.mlmodel"):
	"""A real zip, built here, in the shape Volume Vision's exporter produces.

	Built rather than fixtured because the thing under test is the reading of a
	zip: a fixture file would prove the parser can read one particular archive
	somebody committed, and this proves it can read what the format says.
	"""
	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, "w") as archive:
		if manifest is not None:
			archive.writestr(manifest_name, manifest if isinstance(manifest, str) else json.dumps(manifest))
		if model_name:
			archive.writestr(model_name, b"\x00\x01coreml-weights\x02\x03")
		for name, payload in (entries or {}).items():
			archive.writestr(name, payload)
	return buffer.getvalue()


MANIFEST = {
	"uuid": "4b6f6e1a-2c3d-4e5f-8a9b-0c1d2e3f4a5b",
	"name": "Cherry Fill Detection",
	"version": "3.2",
	"class_names": ["background", "cherry", "bucket", "lip"],
	"class_roles": {"cherry": "fill", "bucket": "container"},
	"piecework_activity": "bucket_fill_detection",
	"model_kind": "Segmentation",
	"metrics": {"mAP50": 0.91, "precision": 0.88},
	"preprocessing": {"input_size": [640, 640], "normalization": "0-1", "color_space": "RGB"},
	"training_completed_at": "2026-08-01 14:22:00",
}


class ReadingABundle(unittest.TestCase):
	"""v0.59.0 — the zip, its manifest, and who wins when they disagree."""

	def test_the_magic_number_decides_and_not_the_file_name(self):
		self.assertTrue(engine.looks_like_bundle(bundle_bytes(MANIFEST)))
		self.assertFalse(engine.looks_like_bundle(b"\x00\x01coreml-weights\x02\x03"))
		self.assertFalse(engine.looks_like_bundle(b""))
		self.assertFalse(engine.looks_like_bundle(None))

	def test_a_bundle_reports_its_manifest_its_model_and_its_entries(self):
		read = engine.read_bundle(bundle_bytes(MANIFEST))
		self.assertTrue(read["is_bundle"])
		self.assertEqual(read["errors"], [])
		self.assertEqual(read["manifest"]["class_names"], MANIFEST["class_names"])
		self.assertEqual(read["manifest_entry"], "manifest.json")
		self.assertEqual(read["model_entry"], "model.mlmodel")
		self.assertEqual(read["model_format"], engine.MODEL_FORMAT_COREML)
		self.assertIn("model.mlmodel", read["entries"])

	def test_a_bundle_zipped_as_a_folder_is_still_read(self):
		"""`zip -r bundle.zip cherry_fill_v1/` is what a hand-built bundle looks like."""
		read = engine.read_bundle(
			bundle_bytes(
				MANIFEST, manifest_name="cherry_v1/manifest.json", model_name="cherry_v1/model.mlmodel"
			)
		)
		self.assertEqual(read["errors"], [])
		self.assertEqual(read["manifest_entry"], "cherry_v1/manifest.json")
		self.assertEqual(read["model_entry"], "cherry_v1/model.mlmodel")

	def test_a_raw_model_is_not_a_bundle_and_is_not_an_error(self):
		read = engine.read_bundle(b"\x00\x01coreml-weights\x02\x03")
		self.assertFalse(read["is_bundle"])
		self.assertEqual(read["errors"], [])
		self.assertEqual(read["manifest"], {})

	def test_a_truncated_zip_is_an_error_rather_than_an_empty_manifest(self):
		read = engine.read_bundle(bundle_bytes(MANIFEST)[:40])
		self.assertTrue(read["is_bundle"])
		self.assertTrue(any("truncated" in e for e in read["errors"]))

	def test_a_zip_with_no_manifest_is_refused(self):
		read = engine.read_bundle(bundle_bytes(manifest=None))
		self.assertTrue(any("manifest.json" in e for e in read["errors"]))

	def test_a_manifest_that_is_not_json_is_an_error(self):
		read = engine.read_bundle(bundle_bytes(manifest="{not json"))
		self.assertTrue(any("not valid UTF-8 JSON" in e for e in read["errors"]))

	def test_class_names_is_the_one_required_key(self):
		self.assertEqual(engine.validate_bundle_manifest(MANIFEST), [])
		self.assertTrue(any("no class_names" in e for e in engine.validate_bundle_manifest({"uuid": "x"})))
		self.assertTrue(
			any("not an ordered array" in e for e in engine.validate_bundle_manifest({"class_names": {}}))
		)
		self.assertTrue(any("is empty" in e for e in engine.validate_bundle_manifest({"class_names": []})))
		self.assertTrue(
			any(
				"not label strings" in e
				for e in engine.validate_bundle_manifest({"class_names": ["cherry", 7, ""]})
			)
		)

	def test_metrics_that_are_not_an_object_are_reported(self):
		errors = engine.validate_bundle_manifest({"class_names": ["a"], "metrics": [1, 2]})
		self.assertTrue(any("metrics" in e for e in errors))

	def test_the_bundle_wins_over_the_records_own_class_names_and_says_so(self):
		record = model(class_names=json.dumps(["cherry", "bucket"]), source_uuid=MANIFEST["uuid"])
		result = engine.reconcile_bundle_manifest(record, MANIFEST, "cherry.bundle.zip")
		self.assertEqual(result["updates"]["class_names"], MANIFEST["class_names"])
		self.assertEqual(result["conflicts"], [])
		self.assertTrue(any("class_names on this record were" in w for w in result["warnings"]))

	def test_a_record_with_no_labels_gets_them_without_a_warning(self):
		result = engine.reconcile_bundle_manifest(model(), MANIFEST, "cherry.bundle.zip")
		self.assertEqual(result["updates"]["class_names"], MANIFEST["class_names"])
		self.assertEqual(result["warnings"], [])

	def test_the_manifest_is_stored_whole_and_the_provenance_sentence_names_the_uuid(self):
		"""v0.68.0 normalizes on the way in, so "whole" now means EVERY KEY THE
		EXPORTER WROTE SURVIVES UNCHANGED — not that the stored dict is byte-
		identical to the one that arrived. What is added is derived (the schema
		version, the origin, the userDefined mirror); nothing that came out of
		training is dropped or rewritten. See `normalize_manifest`."""
		result = engine.reconcile_bundle_manifest(model(), MANIFEST, "cherry.bundle.zip")
		stored = result["updates"]["bundle_manifest"]
		for key, value in MANIFEST.items():
			with self.subTest(key=key):
				self.assertEqual(stored[key], value)
		self.assertEqual(stored["schema_version"], engine.MANIFEST_SCHEMA_VERSION)
		self.assertEqual(stored["manifest_origin"], engine.MANIFEST_ORIGIN_BUNDLE)
		self.assertEqual(engine.validate_manifest_schema(stored), [])
		note = result["updates"]["manifest_source"]
		self.assertTrue(note.startswith(engine.MANIFEST_SOURCE_BUNDLE))
		self.assertIn(MANIFEST["uuid"], note)
		self.assertIn("cherry.bundle.zip", note)

	def test_a_bundle_for_a_different_trained_model_is_a_conflict_not_a_warning(self):
		record = model(source_uuid="11111111-2222-3333-4444-555555555555")
		result = engine.reconcile_bundle_manifest(record, MANIFEST)
		self.assertTrue(result["conflicts"])
		self.assertIn(MANIFEST["uuid"], result["conflicts"][0])
		self.assertNotIn("source_uuid", result["updates"])

	def test_a_record_with_no_uuid_adopts_the_manifests(self):
		result = engine.reconcile_bundle_manifest(model(), MANIFEST)
		self.assertEqual(result["updates"]["source_uuid"], MANIFEST["uuid"])

	def test_a_malformed_manifest_uuid_is_not_adopted(self):
		result = engine.reconcile_bundle_manifest(model(), dict(MANIFEST, uuid="not-a-uuid"))
		self.assertNotIn("source_uuid", result["updates"])
		self.assertTrue(any("well-formed UUID" in w for w in result["warnings"]))

	def test_version_and_activity_are_warned_about_and_never_rewritten(self):
		record = model(version="1", piecework_activity="harvest_quality")
		result = engine.reconcile_bundle_manifest(record, MANIFEST)
		self.assertNotIn("version", result["updates"])
		self.assertNotIn("piecework_activity", result["updates"])
		self.assertTrue(any("training version" in w for w in result["warnings"]))
		self.assertTrue(any("piecework_activity is left alone" in w for w in result["warnings"]))

	def test_a_training_date_already_on_the_record_is_left_alone(self):
		record = model(training_completed_at="2026-07-30 08:00:00")
		result = engine.reconcile_bundle_manifest(record, MANIFEST)
		self.assertNotIn("training_completed_at", result["updates"])
		self.assertEqual(
			engine.reconcile_bundle_manifest(model(), MANIFEST)["updates"]["training_completed_at"],
			MANIFEST["training_completed_at"],
		)

	def test_an_iso_training_date_is_converted_before_it_reaches_a_datetime_column(self):
		"""The bug: MariaDB answers `2026-07-08T02:38:43Z` with
		`OperationalError (1292, "Incorrect datetime value")`, which failed the
		whole pull after the model had already come down the wire."""
		result = engine.reconcile_bundle_manifest(
			model(), dict(MANIFEST, training_completed_at="2026-07-08T02:38:43Z")
		)
		self.assertEqual(result["updates"]["training_completed_at"], "2026-07-08 02:38:43")
		self.assertEqual(result["warnings"], [])

	def test_an_unreadable_training_date_warns_instead_of_failing_the_attach(self):
		result = engine.reconcile_bundle_manifest(
			model(), dict(MANIFEST, training_completed_at="last Tuesday")
		)
		self.assertNotIn("training_completed_at", result["updates"])
		self.assertTrue(any("not a timestamp" in w for w in result["warnings"]))
		# Everything else still applied — one unreadable field is not a refusal.
		self.assertEqual(result["updates"]["class_names"], MANIFEST["class_names"])


class ConvertingATimestamp(unittest.TestCase):
	"""v0.59.0 — `as_mariadb_datetime`, the ISO 8601 a Datetime column refuses."""

	def test_the_shape_volume_vision_writes(self):
		self.assertEqual(engine.as_mariadb_datetime("2026-07-08T02:38:43Z"), "2026-07-08 02:38:43")

	def test_an_offset_is_applied_and_not_discarded(self):
		self.assertEqual(engine.as_mariadb_datetime("2026-07-08T04:38:43+02:00"), "2026-07-08 02:38:43")
		self.assertEqual(engine.as_mariadb_datetime("2026-07-07T19:38:43-07:00"), "2026-07-08 02:38:43")
		self.assertEqual(engine.as_mariadb_datetime("2026-07-08T04:38:43+0200"), "2026-07-08 02:38:43")

	def test_an_offset_that_crosses_a_day_boundary_moves_the_date(self):
		self.assertEqual(engine.as_mariadb_datetime("2026-07-08T00:30:00+02:00"), "2026-07-07 22:30:00")

	def test_fractional_seconds_are_dropped_rather_than_refused(self):
		self.assertEqual(engine.as_mariadb_datetime("2026-07-08T02:38:43.512394Z"), "2026-07-08 02:38:43")
		self.assertEqual(engine.as_mariadb_datetime("2026-07-08T02:38:43.5"), "2026-07-08 02:38:43")

	def test_a_value_already_in_the_column_s_own_format_is_unchanged(self):
		self.assertEqual(engine.as_mariadb_datetime("2026-07-08 02:38:43"), "2026-07-08 02:38:43")

	def test_a_missing_time_or_seconds_is_filled_with_zeroes(self):
		self.assertEqual(engine.as_mariadb_datetime("2026-07-08"), "2026-07-08 00:00:00")
		self.assertEqual(engine.as_mariadb_datetime("2026-07-08T02:38"), "2026-07-08 02:38:00")

	def test_a_datetime_object_survives_the_round_trip(self):
		import datetime as dt

		self.assertEqual(
			engine.as_mariadb_datetime(dt.datetime(2026, 7, 8, 2, 38, 43, 9999)), "2026-07-08 02:38:43"
		)
		self.assertEqual(
			engine.as_mariadb_datetime(
				dt.datetime(2026, 7, 8, 4, 38, 43, tzinfo=dt.timezone(dt.timedelta(hours=2)))
			),
			"2026-07-08 02:38:43",
		)
		self.assertEqual(engine.as_mariadb_datetime(dt.date(2026, 7, 8)), "2026-07-08 00:00:00")

	def test_anything_unreadable_is_an_empty_string_and_never_a_raise(self):
		for value in ("", None, "last Tuesday", "2026-13-01", "2026-02-30", "08/07/2026", 7, {}):
			with self.subTest(value=value):
				self.assertEqual(engine.as_mariadb_datetime(value), "")

	def test_an_unrecognised_model_kind_is_reported_rather_than_applied(self):
		result = engine.reconcile_bundle_manifest(model(), dict(MANIFEST, model_kind="Sorcery"))
		self.assertNotIn("model_kind", result["updates"])
		self.assertTrue(any("Sorcery" in w for w in result["warnings"]))

	def test_the_manifest_reaches_an_ios_client_without_unpacking_the_zip(self):
		record = model(bundle_manifest=json.dumps(MANIFEST), manifest_source=engine.MANIFEST_SOURCE_BUNDLE)
		manifest = engine.build_model_manifest(record)["metadata"]["bundle"]
		self.assertTrue(manifest["is_bundle"])
		self.assertEqual(manifest["preprocessing"], MANIFEST["preprocessing"])
		self.assertEqual(manifest["class_roles"], MANIFEST["class_roles"])

	def test_a_legacy_record_says_where_its_labels_came_from_too(self):
		manifest = engine.build_model_manifest(model())["metadata"]["bundle"]
		self.assertFalse(manifest["is_bundle"])
		self.assertEqual(manifest["manifest_source"], engine.MANIFEST_SOURCE_RECORD)


class TheManifestSchema(unittest.TestCase):
	"""v0.68.0. What the current manifest shape is, and what is not in it yet.

	`MANIFEST` above is a v0.59.0 bundle manifest exactly as Volume Vision's
	exporter writes one, which is what makes it the right fixture here: it is
	the shape that WAS current, and everything below is about the distance
	between it and the shape that is.
	"""

	def test_a_v059_manifest_is_not_in_the_current_schema(self):
		errors = engine.validate_manifest_schema(MANIFEST)
		self.assertTrue(any("schema_version" in e for e in errors))
		self.assertTrue(any("manifest_origin" in e for e in errors))
		self.assertTrue(any(engine.MANIFEST_USER_DEFINED_KEY in e for e in errors))

	def test_normalizing_it_makes_it_current_and_keeps_every_key_it_had(self):
		normalized = engine.normalize_manifest(MANIFEST, model(), origin=engine.MANIFEST_ORIGIN_BUNDLE)
		self.assertEqual(engine.validate_manifest_schema(normalized), [])
		for key, value in MANIFEST.items():
			with self.subTest(key=key):
				self.assertEqual(normalized[key], value)

	def test_normalizing_is_idempotent(self):
		once = engine.normalize_manifest(MANIFEST, model(), origin=engine.MANIFEST_ORIGIN_BUNDLE)
		twice = engine.normalize_manifest(once, model(), origin=engine.MANIFEST_ORIGIN_BUNDLE)
		self.assertEqual(once, twice)

	def test_the_user_defined_mirror_carries_the_labels_in_coremls_own_spelling(self):
		normalized = engine.normalize_manifest(MANIFEST, model())
		user_defined = normalized[engine.MANIFEST_USER_DEFINED_KEY]
		self.assertEqual(user_defined["class_names"], "background,cherry,bucket,lip")
		self.assertTrue(all(isinstance(value, str) for value in user_defined.values()))
		self.assertEqual(
			engine.decode_user_defined_class_names(user_defined["class_names"]),
			MANIFEST["class_names"],
		)

	def test_a_label_containing_a_comma_is_written_as_json_rather_than_split(self):
		"""SPLITTING IT WOULD RENUMBER EVERY OUTPUT INDEX AFTER IT, which is the
		silent wrong answer the whole format exists to stop."""
		labels = ["background", "bucket, full", "lip"]
		encoded = engine.encode_user_defined_class_names(labels)
		self.assertTrue(encoded.startswith("["))
		self.assertEqual(engine.decode_user_defined_class_names(encoded), labels)

	def test_a_manifest_whose_mirror_disagrees_with_its_array_is_refused(self):
		normalized = engine.normalize_manifest(MANIFEST, model())
		normalized[engine.MANIFEST_USER_DEFINED_KEY]["class_names"] = "background,cherry"
		errors = engine.validate_manifest_schema(normalized)
		self.assertTrue(any("cannot both be" in e for e in errors))

	def test_a_manifest_built_from_a_record_alone_carries_the_records_labels(self):
		record = model(
			class_names=json.dumps(["a", "b"]), source_uuid=MANIFEST["uuid"], model_kind="Segmentation"
		)
		normalized = engine.normalize_manifest({}, record, origin=engine.MANIFEST_ORIGIN_RECORD)
		self.assertEqual(engine.validate_manifest_schema(normalized), [])
		self.assertEqual(normalized["class_names"], ["a", "b"])
		self.assertEqual(normalized["manifest_origin"], engine.MANIFEST_ORIGIN_RECORD)
		self.assertEqual(normalized["uuid"], MANIFEST["uuid"])
		self.assertEqual(normalized["name"], "Cherry Fill Detection")
		self.assertEqual(normalized["model_format"], engine.DEFAULT_MODEL_FORMAT)

	def test_a_bundle_origin_is_never_downgraded_by_a_caller_that_guessed(self):
		"""`bundle` is a claim about the STORED BYTES. A caller passing `record`
		over a manifest that already says `bundle` would be telling a phone to
		compile a zip."""
		normalized = engine.normalize_manifest(MANIFEST, model(), origin=engine.MANIFEST_ORIGIN_BUNDLE)
		again = engine.normalize_manifest(normalized, model(), origin=engine.MANIFEST_ORIGIN_RECORD)
		self.assertEqual(again["manifest_origin"], engine.MANIFEST_ORIGIN_BUNDLE)

	def test_an_unrecognised_model_format_is_preserved_and_reported_not_defaulted(self):
		normalized = engine.normalize_manifest(dict(MANIFEST, model_format="PyTorch"), model())
		self.assertEqual(normalized["model_format"], "PyTorch")
		self.assertTrue(any("PyTorch" in e for e in engine.validate_manifest_schema(normalized)))

	def test_is_bundle_payload_reads_the_origin_and_not_the_mere_presence_of_a_manifest(self):
		from_bundle = engine.normalize_manifest(MANIFEST, model(), origin=engine.MANIFEST_ORIGIN_BUNDLE)
		from_record = engine.normalize_manifest(
			{}, model(class_names='["a"]'), origin=engine.MANIFEST_ORIGIN_RECORD
		)
		self.assertTrue(engine.is_bundle_payload(model(bundle_manifest=json.dumps(from_bundle))))
		self.assertFalse(engine.is_bundle_payload(model(bundle_manifest=json.dumps(from_record))))
		self.assertFalse(engine.is_bundle_payload(model()))
		# A manifest predating the field could only ever have come from a zip.
		self.assertTrue(engine.is_bundle_payload(model(bundle_manifest=json.dumps(MANIFEST))))

	def test_a_migrated_record_does_not_tell_an_ios_client_to_unpack_a_raw_model(self):
		from_record = engine.normalize_manifest(
			{}, model(class_names='["a"]'), origin=engine.MANIFEST_ORIGIN_RECORD
		)
		bundle_block = engine.build_model_manifest(
			model(bundle_manifest=json.dumps(from_record), manifest_source=engine.MANIFEST_SOURCE_MIGRATED)
		)["metadata"]["bundle"]
		self.assertFalse(bundle_block["is_bundle"])
		self.assertEqual(bundle_block["manifest_origin"], engine.MANIFEST_ORIGIN_RECORD)
		self.assertEqual(bundle_block["schema_version"], engine.MANIFEST_SCHEMA_VERSION)
		self.assertEqual(bundle_block["user_defined"]["class_names"], "a")

	def test_a_pre_v059_record_needs_migration_and_can_be_migrated(self):
		report = engine.manifest_migration_report(model(class_names=json.dumps(["a", "b"])))
		self.assertTrue(report["needs_migration"])
		self.assertTrue(report["can_migrate"])
		self.assertFalse(report["has_manifest"])
		self.assertEqual(report["manifest_origin"], engine.MANIFEST_ORIGIN_RECORD)
		self.assertTrue(any("no bundle_manifest at all" in r for r in report["reasons"]))

	def test_a_normalized_record_needs_nothing(self):
		normalized = engine.normalize_manifest(MANIFEST, model(), origin=engine.MANIFEST_ORIGIN_BUNDLE)
		record = model(
			class_names=json.dumps(MANIFEST["class_names"]), bundle_manifest=json.dumps(normalized)
		)
		report = engine.manifest_migration_report(record)
		self.assertFalse(report["needs_migration"])
		self.assertEqual(report["reasons"], [])
		self.assertEqual(report["manifest_origin"], engine.MANIFEST_ORIGIN_BUNDLE)

	def test_a_record_with_no_labels_anywhere_is_blocked_rather_than_migrated(self):
		report = engine.manifest_migration_report(model())
		self.assertTrue(report["needs_migration"])
		self.assertFalse(report["can_migrate"])
		self.assertTrue(any("no class_names anywhere" in b for b in report["blockers"]))

	def test_an_unrecognised_model_format_on_the_record_blocks_migration(self):
		report = engine.manifest_migration_report(
			model(class_names=json.dumps(["a"]), model_format="PyTorch")
		)
		self.assertFalse(report["can_migrate"])
		self.assertTrue(any("update_model" in b for b in report["blockers"]))

	def test_two_label_lists_that_disagree_are_a_reason_to_migrate(self):
		normalized = engine.normalize_manifest(MANIFEST, model(), origin=engine.MANIFEST_ORIGIN_BUNDLE)
		record = model(class_names=json.dumps(["something", "else"]), bundle_manifest=json.dumps(normalized))
		report = engine.manifest_migration_report(record)
		self.assertTrue(report["needs_migration"])
		self.assertTrue(report["can_migrate"])
		self.assertTrue(any("Only one of them" in r or "the other is not" in r for r in report["reasons"]))

	def test_an_unreadable_bundle_manifest_is_a_reason_and_not_a_raise(self):
		report = engine.manifest_migration_report(
			model(class_names=json.dumps(["a"]), bundle_manifest="{not json")
		)
		self.assertTrue(report["needs_migration"])
		self.assertTrue(report["can_migrate"])
		self.assertTrue(any("not a manifest anything can read" in r for r in report["reasons"]))


class ToolRegistration(unittest.TestCase):
	"""Thirteen tools, six reads and seven writes, wired into the catalogue."""

	READ_TOOLS = (
		"get_model",
		"list_models",
		"get_active_model",
		"get_model_file_chunk",
		"list_models_needing_migration",
		"validate_model_bundle",
	)
	MUTATING_TOOLS = (
		"register_model",
		"update_model",
		"activate_model",
		"deprecate_model",
		"attach_model_file",
		"pull_model_from_vv",
		"migrate_model_format",
	)

	def setUp(self):
		from erpnext_mcp import registry

		self.registry = registry

	def test_every_model_tool_is_registered(self):
		for name in self.READ_TOOLS + self.MUTATING_TOOLS:
			with self.subTest(tool=name):
				self.assertIn(name, self.registry.TOOLS)

	def test_the_read_tools_are_read_only(self):
		for name in self.READ_TOOLS:
			with self.subTest(tool=name):
				self.assertFalse(self.registry.TOOLS[name]["mutating"])
				self.assertIn(name, self.registry.READ_TOOLS)

	def test_the_mutating_tools_are_marked_mutating(self):
		for name in self.MUTATING_TOOLS:
			with self.subTest(tool=name):
				self.assertTrue(self.registry.TOOLS[name]["mutating"])
				self.assertIn(name, self.registry.MUTATING_TOOLS)
				self.assertIn("MUTATING", self.registry.TOOLS[name]["description"])

	def test_none_of_the_five_are_on_by_default(self):
		"""Mutating tools default off is the whole point of the switch — none of
		these five belong in registry.DEFAULT_ON_MUTATING_TOOLS."""
		for name in self.MUTATING_TOOLS:
			with self.subTest(tool=name):
				self.assertNotIn(name, self.registry.DEFAULT_ON_MUTATING_TOOLS)

	def test_the_registry_totals_include_the_ten(self):
		# 389/178/211 as of v0.51.1, plus v0.52.0's `attach_model_file` (write)
		# and `get_model_file_chunk` (read) — the file-serving pair that lets
		# ERPNext hand an iOS app the model binary itself instead of the model
		# only ever pointing at where Volume Vision keeps it. v0.53.0 adds one
		# more write, `generate_employee_badge_pass`, and v0.55.0 a second,
		# `collect_form_signature` — the call that files the capture a
		# missing-signature alert raised a task to go and collect. v0.56.0 adds
		# `generate_employee_id_card`, which puts the badge in the Attachments
		# sidebar of the Employee form somebody already has open. v0.57.0 adds
		# one more write, `dismiss_compliance_alert` — the same dismissal
		# `dismiss_alert` makes, gated on the alert's own say-so, for the callers
		# who are not sitting in front of the record. v0.59.0 adds
		# `pull_model_from_vv` — the one call that replaces the curl-and-bench-
		# console procedure for getting a trained model out of Volume Vision,
		# and the only tool in this app that fetches a file from another server.
		# v0.60.0 adds two READS and no write: `list_signing_evidence` and
		# `get_signing_evidence`, over the register of who signed what and how
		# anybody knows it was them. Nothing writes that register but the
		# signature path itself. v0.61.0 adds eight — four reads and four writes
		# — over the two company-wide wage tables: what the OPERATION pays for a
		# bucket and for an hour of a job title, as opposed to what one named
		# person earns. `Piecework Rate.activity` is the same vocabulary this
		# register keys a model by, which is what lets one rate table and one
		# deployed model talk about the same piece of work. v0.63.0 adds two over
		# the document either side of a signature: `get_document_preview` (read)
		# is step 1 of the evidence chain — the page the signer is shown, as bytes,
		# because the handset cannot follow a private file_url — and
		# `seal_signed_document` (write) is step 5, the verification page and the
		# hash of the finished file. v0.65.0 adds one write, `universal_scan` —
		# one scanned string resolved against the badge, asset, housing and block
		# registers in that order — and it counts as a write for one branch's
		# sake: the asset one is `scan_asset`, which stamps `last_scan_at`.
		# v0.66.0 adds nineteen master-data tools — ten reads, nine writes — over
		# Item, Item Group, Supplier, Customer, Warehouse and Item Price.
		# v0.67.0 adds nine receipt-capture tools — five reads, four writes — over
		# the two new registers, Scale Ticket and Settlement Statement, plus
		# `classify_receipt`, which is the only read here that touches no doctype
		# at all: it decides which of the four registers a photograph belongs in.
		# v0.67.1 adds ONE write, `patch_i9_section_1`, and it is the first tool
		# in the I-9 module that moves a form sideways rather than forward: every
		# other one advances a status, so a Section 1 filed with a blank date of
		# birth had no route to one on any status.
		# v0.68.0 adds six over the Container-Agnostic Fill Pipeline — four
		# reads (`get_fill_determination`, `get_fill_thresholds`,
		# `list_fill_threshold_changes`, `list_pending_threshold_acknowledgments`)
		# and two writes (`update_fill_threshold`, `acknowledge_threshold_update`)
		# — connecting a segmentation model's mask/container pixel areas to a
		# foreman-controlled band per container type, with a change log a checker
		# acknowledges.
		# v0.68.0 also adds seven over expense-receipt capture — four reads
		# (`get_expense_summary`, `get_expense_report`, `normalize_merchant`,
		# `list_merchant_aliases`) and three writes (`create_owner_draw`,
		# `update_expense_receipt`, `create_purchase_invoice_from_receipt`) —
		# the last of which builds a Purchase Invoice by calling
		# `purchasing.create_purchase_invoice` rather than writing one itself.
		# v0.68.0 also adds sixteen over the rest of the purchasing pipeline —
		# eight reads and eight writes — Sprint 3 of the Gap Closure Plan:
		# Purchase Order create/get/submit, Purchase Receipt
		# create/get/list/submit, Purchase Invoice create/get/list/submit,
		# Payment Entry create/get/list/submit, and `get_ap_aging`, which reads
		# GL Entry for a supplier's true balance and Purchase Invoice's own
		# outstanding_amount for the per-invoice ageing buckets.
		# v0.68.0 also adds THREE HERE — two reads
		# (`list_models_needing_migration`, `validate_model_bundle`) and one
		# write (`migrate_model_format`) — for the records v0.43.0, v0.52.0 and
		# v0.59.0 each left in a different shape. The write moves metadata only:
		# it never uploads, downloads or re-attaches a file.
		# v0.69.0 adds nine over stock and inventory — six reads and three
		# writes — Sprint 4 of the Gap Closure Plan: Stock Entry
		# create/get/list/submit, `get_stock_balance` and `get_warehouse_summary`
		# over Bin, `get_stock_ledger` over Stock Ledger Entry, and the reorder
		# pair `set_reorder_level` / `list_reorder_alerts`. Same draft/submit
		# split as purchasing: `create_stock_entry` moves nothing and
		# `submit_stock_entry` is the separately-switched tool that does.
		# v0.69.0 (Sprint 4) adds FIVE elsewhere — three reads and two writes
		# over Document Validation, which is what the deterministic half of
		# document intelligence writes its answer into. Nothing about the ML
		# model registry changed; the totals moved because the catalogue did.
		# v0.70.0 adds TWELVE over sales and settlements — six reads and six
		# writes — Sprint 5 of the Gap Closure Plan, and the other end of the
		# pipeline Sprints 2 and 3 opened: `create_sales_invoice` /
		# `create_sales_invoice_from_settlement` / `get_sales_invoice` /
		# `list_sales_invoices` / `submit_sales_invoice` turn a packer
		# settlement into revenue, `receive_payment` collects it as a
		# Receive/Customer Payment Entry, `post_settlement_to_gl` is the
		# journal-entry alternative to the invoice, and
		# `reconcile_settlement_to_tickets` attaches a ticket that arrived after
		# the settlement was filed. `get_settlement_shrink`,
		# `get_packout_summary`, `get_ar_aging` and `get_season_summary` are the
		# reads that make a season legible. There is deliberately no Delivery
		# Note tool: the packer owns the scale, so the Scale Ticket is the
		# delivery evidence.
		# v0.71.0 adds TEN over CFL Banking — six reads and four writes —
		# Sprint 6 of the Gap Closure Plan and its capstone: the bridge from the
		# receipt/expense/invoice pipeline to the bank's own record of the same
		# money. `match_receipt_to_bank_transaction` and `auto_match_receipts`
		# pair a slip with the withdrawal it is the paper for — the batch half
		# is a READ tool that proposes and never commits, because a wrong link
		# between a slip and a withdrawal is invisible afterwards.
		# `create_bank_categorization_rule`, `list_bank_categorization_rules`,
		# `apply_categorization_rules` and `seed_farm_categorization_rules` make
		# the dictionary a farm reads its own statement with a RECORD rather
		# than code. `get_bank_reconciliation_status` answers the ledger, the
		# evidence and the categorisation questions separately and never sums
		# them; `list_unmatched_receipts` and
		# `list_unmatched_bank_transactions` are the two worklists; and
		# `get_cash_flow_summary` reports the cash apart from the documents and
		# deduplicates a receipt against the withdrawal it is matched to.
		# Nothing in Sprint 6 posts to the ledger.
		# v0.73.0 adds FOURTEEN over the Bank Bridge consolidation — eight
		# reads and six writes — moving the statement anchor chain, the account
		# pairings and the advisory agreements out of a sidecar Flask app and
		# into the system that already holds the transactions, the ledger and
		# the company. `get_statement_anchor_chain`,
		# `list_unreconciled_anchors`, `get_anchor_variance_breakdown`,
		# `list_unmatched_statement_lines` and `get_statement_recon_report` are
		# the reads that answer whether a year of bank data is COMPLETE, which
		# no transaction list can answer on its own.
		# `set_anchor_variance_reason` and `rebuild_anchor_chain` are the two
		# writes on the chain, and neither touches the three numbers that came
		# off a bank statement. `get_account_pairing` and `pair_bank_accounts`
		# store a brokerage-to-cash-services relationship on both sides. The
		# four advisory tools make a fee that arrives already deducted
		# checkable against its own terms, amended by versioning rather than by
		# editing. `create_bank_categorization_rules` vets a whole book of
		# rules as a set.
		# Nothing in the consolidation posts to the ledger.
		# v0.78.0 adds ten more — six reads and four writes — turning the asset
		# register's accumulated state log into answers: a whole status report
		# per machine, engine hours, a service schedule, a water usage roll-up,
		# and the four restricted-entry tools. See `test_protocol.Catalogue`,
		# which carries the long version of the same three numbers.
		# v0.84.0 adds ten more — six reads and four writes — for
		# activity-based costing: the activity register, the pools, the
		# allocation engine and the reads over a stored run. See `test_abc.py`
		# for what the engine refuses to estimate.
		# v0.68.1 adds fifteen — five reads and ten writes — over the five
		# organisational masters an Employee links to: Designation, Department,
		# Branch, Employment Type and Employee Grade. Every one of them was a
		# register `create_employee` refused a value against and nothing could
		# add to, so the refusal named an answer the caller could not act on.
		# v0.93.0 adds FOUR reads and no write. `get_policy_coverage` is the only
		# read over the SOP library that is about the policies which do NOT
		# exist. `trace_backward` and `trace_forward` are the mock recall in both
		# directions — the CTE columns on Bucket Log Entry have threaded since
		# v0.44.0 and nothing walked them. `get_owner_dashboard` composes seven
		# existing reads into the one screen an owner opens. Nothing about the ML
		# model registry changed; the totals moved because the catalogue did.
		# v0.93.0 adds ONE write as well: `recover_mobile_access`, the lost-phone
		# path — revoke, re-mint, verify by badge, and never touch the Employee.
		# v0.101.0 adds two writes: `create_break_policy` and
		# `update_break_policy` — the break schedule for a state is now writeable
		# through the MCP surface.
		# v0.101.0 also adds garnishment compliance: two reads
		# (`list_garnishments`, `get_garnishment`) and three writes
		# (`create_garnishment`, `update_garnishment`,
		# `render_garnishment_response`) over the new Farm Garnishment doctype —
		# the court order the payroll deduction exists under.
		# v0.114.0 adds ONE read: `get_variety_care_recipe`, which resolves a
		# variety's water schedule against its crop's PER FIELD and returns its
		# cultural practice protocol grouped by practice. The two new child
		# tables it reads carry no tools of their own — they are edited on the
		# Crop form and through `create_crop`/`update_crop`, whose signatures
		# did not change.
		# v0.116.0 adds five, the operational map overlays: two reads
		# (`get_map_overlays`, `list_soil_compaction_profiles`) and three writes
		# over the new Soil Compaction Profile register
		# (`create_soil_compaction_profile`, `update_soil_compaction_profile`,
		# `assign_soil_profile`). `Field.soil_profile`, `Crop.target_brix` and
		# `Crop Variety.target_brix` are new columns on existing registers and
		# carry no tools of their own — no existing signature changed.
		# v0.122.0, Farm App Retirement Cycle 3, adds thirty-two: thirty-one for the
		# HACCP/FSMA food safety plan and the eight registers under it, and one
		# read, `list_tasks_by_location`, a third reader of
		# `list_available_for_me` and `list_my_tasks`'s own calls that groups the
		# pool and the caller's held work by place. Eighteen reads and fourteen
		# writes; no existing signature changed.
		# v0.128.0 adds two reads over App Feedback, the register the in-app
		# feedback bubble has been writing into since v0.105.0:
		# `list_app_feedback` (the feed, newest first on when Send was pressed)
		# and `get_app_feedback` (one note in full). The write half stays off the
		# catalogue and reachable only from the mobile surface — a phone files
		# its own note under its own login. No existing signature changed.
		# v0.129.0 adds four reads about the deployment rather than the farm:
		# get_server_status, list_error_logs, list_sidecar_routes and
		# query_doctype. No existing signature changed.
		# v0.139.0 adds two over the FSA county office's own Common Land Unit
		# file: one read, `read_fsa_clu_file`, which says what is in an export
		# and touches nothing on the site, and one write,
		# `import_fsa_clu_boundaries`, which matches those CLUs against the
		# blocks already registered and sets their boundaries. The seven `fsa_`
		# columns on Field are new columns on an existing register and carry no
		# tools of their own — no existing signature changed.
		self.assertEqual(len(self.registry.TOOLS), 853)
		self.assertEqual(len(self.registry.READ_TOOLS), 432)
		self.assertEqual(len(self.registry.MUTATING_TOOLS), 421)


if __name__ == "__main__":
	unittest.main()
