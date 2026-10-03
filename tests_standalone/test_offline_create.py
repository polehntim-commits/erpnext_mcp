# SPDX-License-Identifier: MIT
"""Offline Add Asset, server side. v0.226.0 — docs/design/offline_add_asset.md.

Made on a phone with no signal and sent later: the tag is the phone's UUID and
never changes, a resend is the same request, a likely duplicate goes to a
person, a cut-off photo resumes, and tags print by location only when asked.
"""

import base64
import hashlib
import json
import unittest
from pathlib import Path

import frappe

from erpnext_mcp import card_art, card_print, offline_create
from erpnext_mcp.api import files as files_api
from erpnext_mcp.api import guard
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.errors import ToolError
from erpnext_mcp.render import qr
from erpnext_mcp.tools import asset_tags, universal_scan

from .fixtures import MAIN
from .harness import STORE, set_roles
from .test_api_mobile import ON as MOBILE_ON
from .test_api_mobile import WORKER, MobileAPITestCase

DOCTYPES = Path(__file__).resolve().parents[1] / "erpnext_mcp" / "erpnext_mcp" / "doctype"
TAG = "6f1c2a9e-4b7d-4e21-9c3a-0d5e8f7a1b22"
TAG2 = "0a9b8c7d-6e5f-4a3b-8c2d-1e0f9a8b7c6d"
REQ = "req-0001-aaaa"
REQ2 = "req-0002-bbbb"
ON = {
	**MOBILE_ON,
	**{
		f"allow_{name}": 1
		for name in (
			"register_asset",
			"create_housing_unit",
			"create_parcel",
			"universal_scan",
			"request_card_print",
			"print_tags_for_location",
		)
	},
}
NEEDS_QR = unittest.skipUnless(qr.available(), "needs a QR encoder (segno)")


def offline(tag=TAG, request=REQ, **extra):
	return {
		"tag_uuid": tag,
		"request_id": request,
		"device_created_at": "2026-10-03T09:15:00-07:00",
		"offline": 1,
		"created_device": "Ana's iPhone",
		**extra,
	}


class OfflineCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)

	def register(self, name="MC-Pump-01", **extra):
		payload = {"name": name, "asset_type": "General", "company": MAIN, **extra}
		return asset_tags.register_asset(payload).data


class TheColumns(unittest.TestCase):
	def test_both_registers_carry_the_offline_columns_and_the_tag_is_unique(self):
		for folder in ("asset_register", "housing_unit"):
			data = json.loads((DOCTYPES / folder / f"{folder}.json").read_text())
			fields = {f["fieldname"]: f for f in data["fields"]}
			with self.subTest(folder=folder):
				for name in offline_create.FIELDS:
					self.assertIn(name, fields)
					self.assertIn(name, data["field_order"])
				self.assertEqual(fields["tag_uuid"].get("unique"), 1)

	def test_the_print_job_carries_format_and_location(self):
		data = json.loads((DOCTYPES / "card_print_job" / "card_print_job.json").read_text())
		fields = {f["fieldname"]: f for f in data["fields"]}
		self.assertEqual(fields["tag_format"]["options"].split("\n"), list(card_print.TAG_FORMATS))
		self.assertEqual(fields["tag_format"]["default"], "Card")
		self.assertIn("location_label", fields)


class SameRequestSameResult(OfflineCase):
	def test_created_logged_and_the_tag_is_kept(self):
		data = self.register(**offline())
		self.assertEqual((data["outcome"], data["replayed"], data["tag_uuid"]), ("created", False, TAG))
		row = frappe.get_doc("Asset Register", "MC-Pump-01").as_dict()
		self.assertEqual(row["tag_uuid"], TAG)
		self.assertEqual(row["request_id"], REQ)
		self.assertEqual(row["created_offline"], 1)
		self.assertEqual(row["created_device"], "Ana's iPhone")
		self.assertTrue(str(row["device_created_at"]).startswith("2026-10-03"))
		self.assertTrue(row["qr_url"].endswith(f"/scan/{TAG}"))
		comments = frappe.db.get_all("Comment", filters={"reference_name": "MC-Pump-01"}, fields=["content"])
		self.assertTrue(any("offline" in str(c.get("content")) for c in comments))

	def test_a_resend_is_answered_with_the_same_record(self):
		first = self.register(**offline())
		again = self.register(**offline())
		self.assertEqual(again["name"], first["name"])
		self.assertEqual((again["outcome"], again["replayed"]), ("replayed", True))
		self.assertEqual(len(frappe.db.get_all("Asset Register", filters={"tag_uuid": TAG})), 1)

	def test_a_resend_after_a_rename_still_replays(self):
		"""The phone renamed nothing; the first send already made it. Same request, same answer."""
		self.register(**offline())
		again = self.register(name="MC-Pump-01-renamed", **offline())
		self.assertEqual(again["name"], "MC-Pump-01")

	def test_no_offline_arguments_keeps_the_old_refusal(self):
		self.register()
		with self.assertRaises(ToolError) as caught:
			self.register()
		self.assertIn("already has a record", str(caught.exception))
		self.assertEqual(caught.exception.translation_key, "")

	def test_a_tag_on_another_record_is_refused(self):
		self.register(**offline())
		with self.assertRaises(ToolError) as caught:
			self.register(name="MC-Pump-02", **offline(request=REQ2))
		self.assertEqual(caught.exception.translation_key, "error.asset.tag_in_use")
		self.assertFalse(frappe.db.exists("Asset Register", "MC-Pump-02"))

	def test_a_bad_uuid_is_refused(self):
		with self.assertRaises(ToolError):
			self.register(**offline(tag="not-a-uuid"))


class NeedsAttention(OfflineCase):
	def test_a_name_taken_elsewhere_is_fixed_and_resent_with_the_same_tag(self):
		self.register(name="Cabin Pump", location="")
		self.register(name="Shop", asset_type="General")
		with self.assertRaises(ToolError) as caught:
			self.register(name="Cabin Pump", location="Shop", **offline(confirm_new=1))
		self.assertEqual(caught.exception.translation_key, "error.asset.name_taken")
		fixed = self.register(name="Cabin Pump 2", location="Shop", **offline(confirm_new=1))
		self.assertEqual((fixed["name"], fixed["tag_uuid"]), ("Cabin Pump 2", TAG))

	def test_an_unknown_type_says_so(self):
		with self.assertRaises(ToolError) as caught:
			self.register(asset_type="Flux Capacitor", **offline())
		self.assertEqual(caught.exception.translation_key, "error.asset.unknown_type")

	def test_a_parent_typed_offline_and_not_found_is_flagged_not_refused(self):
		data = self.register(location="North Shed", **offline(review_note="parent typed offline: North Shed"))
		row = frappe.get_doc("Asset Register", data["name"]).as_dict()
		self.assertFalse(row.get("location"))
		self.assertEqual(row["needs_review"], 1)
		self.assertIn("North Shed", row["review_note"])
		self.assertIn("North Shed", data["needs_review"])


class DuplicatesGoToAPerson(OfflineCase):
	def test_same_serial_creates_nothing_and_names_the_candidate(self):
		self.register(name="Old pump", serial_number="SN-77")
		data = self.register(name="New pump", serial_number="SN-77", **offline())
		self.assertEqual(data["outcome"], offline_create.POSSIBLE_DUPLICATE)
		self.assertEqual([c["name"] for c in data["candidates"]], ["Old pump"])
		self.assertFalse(frappe.db.exists("Asset Register", "New pump"))

	def test_keep_as_new(self):
		self.register(name="Old pump", serial_number="SN-77")
		data = self.register(name="New pump", serial_number="SN-77", **offline(confirm_new=1))
		self.assertEqual(data["outcome"], "created")

	def test_its_the_same_one_adds_the_tag_and_the_tag_scans_to_it(self):
		self.register(name="Old pump", serial_number="SN-77")
		data = self.register(name="New pump", serial_number="SN-77", **offline(link_to_existing="Old pump"))
		self.assertEqual((data["outcome"], data["name"]), ("linked", "Old pump"))
		self.assertIn(TAG, frappe.db.get_value("Asset Register", "Old pump", "tag_aliases"))
		again = self.register(name="New pump", serial_number="SN-77", **offline(link_to_existing="Old pump"))
		self.assertTrue(again["replayed"])
		self.assertEqual(offline_create.resolve(TAG), ("Asset Register", "Old pump"))
		self.assertEqual(
			universal_scan.scan_target(f"https://farm.example/farmops/api/scan/{TAG}"), "Old pump"
		)


class TheTagScans(OfflineCase):
	def test_scan_target_and_asset_lookup_resolve_the_uuid(self):
		self.register(**offline())
		self.assertEqual(universal_scan.scan_target(f"https://x.ts.net/farmops/api/scan/{TAG}"), "MC-Pump-01")
		self.assertEqual(universal_scan.scan_target(TAG.upper()), "MC-Pump-01")
		data = self.tool_data("universal_scan", {"content": f"https://x.ts.net/farmops/api/scan/{TAG}"})
		self.assertEqual(data["entity_name"], "MC-Pump-01")

	def test_an_unknown_uuid_passes_through(self):
		self.assertEqual(universal_scan.scan_target(TAG2), TAG2)


class FromThePhone(OfflineCase):
	def setUp(self):
		super().setUp()
		self.parcel = self.tool_data(
			"create_parcel",
			{
				"owning_entity": MAIN,
				"parcel_name": "Mill Creek",
				"acreage": 131.4,
				"county": "Wasco",
				"state": "OR",
			},
		)["name"]
		set_roles(WORKER, [*guard.roles_held(WORKER), "Farm Manager"])
		STORE.commit()
		self.be()

	def test_the_register_route_forwards_and_replays(self):
		first = mobile_api.register_asset(name="MC-Pump-07", asset_type="General", company=MAIN, **offline())
		again = mobile_api.register_asset(name="MC-Pump-07", asset_type="General", company=MAIN, **offline())
		self.assertEqual((first["outcome"], again["outcome"]), ("created", "replayed"))

	def test_a_refusal_comes_back_as_an_answer_then_is_fixed_and_resent(self):
		"""Needs attention on the phone: the code says what to fix; the same tag and request go again."""
		mobile_api.register_asset(name="Shop", asset_type="General", company=MAIN)
		taken = mobile_api.register_asset(
			name="Shop", asset_type="General", company=MAIN, location="", **offline(confirm_new=1)
		)
		self.assertEqual((taken["outcome"], taken["code"]), ("refused", "name_taken"))
		typed = mobile_api.register_asset(name="Shop 2", asset_type="Flux", company=MAIN, **offline())
		self.assertEqual(typed["code"], "unknown_type")
		fixed = mobile_api.register_asset(name="Shop 2", asset_type="General", company=MAIN, **offline())
		self.assertEqual((fixed["outcome"], fixed["tag_uuid"]), ("created", TAG))

	def test_without_offline_arguments_a_refusal_still_raises(self):
		mobile_api.register_asset(name="Shop", asset_type="General", company=MAIN)
		STORE.commit()
		with self.assertRaises(Exception):
			mobile_api.register_asset(name="Shop", asset_type="General", company=MAIN)

	def test_a_cabin_with_beds_offline_and_its_resend(self):
		first = mobile_api.create_housing_unit(
			unit_name="Cabin 1",
			parcel=self.parcel,
			unit_type="Cabin",
			capacity=4,
			gps_latitude=45.6,
			gps_longitude=-121.2,
			**offline(),
		)
		self.assertEqual((first["outcome"], first["capacity"]), ("created", 4))
		row = frappe.get_doc("Housing Unit", first["name"]).as_dict()
		self.assertEqual((row["tag_uuid"], row["created_offline"]), (TAG, 1))
		self.assertAlmostEqual(float(row["gps_latitude"]), 45.6)
		again = mobile_api.create_housing_unit(unit_name="Cabin 1", parcel=self.parcel, **offline())
		self.assertEqual((again["outcome"], again["name"]), ("replayed", first["name"]))
		self.assertEqual(universal_scan.scan_target(TAG), first["name"])

	def test_a_cabins_photo_attaches_from_the_phone(self):
		unit = mobile_api.create_housing_unit(
			unit_name="Cabin 3", parcel=self.parcel, capacity=2, **offline()
		)
		payload = b"cabin-photo"
		upload = f"{TAG}-photo"
		files_api.stage_file_chunk(
			upload_id=upload,
			file_name="cabin.jpg",
			chunk_index=0,
			chunk_count=1,
			total_bytes=len(payload),
			data=base64.b64encode(payload).decode(),
		)
		done = files_api.finalize_staged_file(
			upload_id=upload,
			file_name="cabin.jpg",
			sha256=hashlib.sha256(payload).hexdigest(),
			total_bytes=len(payload),
		)
		mobile_api.attach_file_to_document(
			doctype="Housing Unit", name=unit["name"], file_name="cabin.jpg", file_url=done["file_url"]
		)
		files = frappe.db.get_all(
			"File", filters={"attached_to_doctype": "Housing Unit", "attached_to_name": unit["name"]}
		)
		self.assertEqual(len(files), 1)

	def test_a_second_cabin_1_on_the_parcel_is_a_possible_duplicate(self):
		first = mobile_api.create_housing_unit(unit_name="Cabin 1", parcel=self.parcel, capacity=4)
		data = mobile_api.create_housing_unit(
			unit_name="Cabin 1", parcel=self.parcel, capacity=4, **offline()
		)
		self.assertEqual(data["outcome"], offline_create.POSSIBLE_DUPLICATE)
		self.assertFalse(data["can_keep_new"])
		linked = mobile_api.create_housing_unit(
			unit_name="Cabin 1", parcel=self.parcel, **offline(link_to_existing=first["name"])
		)
		self.assertEqual((linked["outcome"], linked["name"]), ("linked", first["name"]))


class AnUploadResumes(OfflineCase):
	def setUp(self):
		super().setUp()
		self.be()

	def stage(self, upload_id, index, pieces):
		files_api.stage_file_chunk(
			upload_id=upload_id,
			file_name="pump.jpg",
			chunk_index=index,
			chunk_count=len(pieces),
			total_bytes=sum(len(p) for p in pieces),
			data=base64.b64encode(pieces[index]).decode(),
		)

	def test_the_phone_asks_what_arrived_sends_the_rest_and_a_lost_finalize_replays(self):
		pieces = [b"first-piece", b"second-piece", b"third-piece"]
		upload = f"{TAG}-abc123"
		self.assertEqual(files_api.get_staged_upload(upload_id=upload)["state"], "none")
		self.stage(upload, 0, pieces)
		self.stage(upload, 2, pieces)
		status = files_api.get_staged_upload(upload_id=upload)
		self.assertEqual((status["state"], status["received"], status["missing"]), ("partial", [0, 2], [1]))
		self.stage(upload, 1, pieces)
		whole = b"".join(pieces)
		args = {"upload_id": upload, "file_name": "pump.jpg", "sha256": hashlib.sha256(whole).hexdigest()}
		done = files_api.finalize_staged_file(total_bytes=len(whole), **args)
		self.assertTrue(done["file_token"])
		again = files_api.finalize_staged_file(total_bytes=len(whole), **args)
		self.assertEqual(again["file_token"], done["file_token"])
		after = files_api.get_staged_upload(upload_id=upload)
		self.assertEqual((after["state"], after["file_token"]), ("committed", done["file_token"]))

	def test_another_users_upload_is_not_readable(self):
		upload = f"{TAG}-xyz"
		self.stage(upload, 0, [b"only"])
		STORE.commit()
		self.be("Administrator")
		with self.assertRaises(Exception):
			files_api.get_staged_upload(upload_id=upload)


class TagsPrintByLocation(OfflineCase):
	def setUp(self):
		super().setUp()
		self.be("Administrator")
		card_print.seed()
		set_roles(WORKER, [*guard.roles_held(WORKER), card_print.REQUESTER_ROLE])
		self.register(name="Pump House")
		for n, name in enumerate(("Pump A", "Pump B"), start=1):
			self.register(
				name=name,
				location="Pump House",
				**offline(tag=f"{n:08d}-0000-4000-8000-000000000000", request=f"req-{n:08d}"),
			)
		STORE.commit()

	def ask(self, reference, key, fmt="Sheet", **extra):
		return card_print.request(
			WORKER, "Asset Tag", reference, key, requested_from="iOS", tag_format=fmt, **extra
		)

	def test_a_sheet_job_has_no_station_and_the_queue_groups_by_location(self):
		job = self.ask("Pump A", "00000000-0000-4000-8000-000000000101")["job"]
		self.assertEqual(
			(job["tag_format"], job["location_label"], job["print_station"]), ("Sheet", "Pump House", None)
		)
		self.ask(
			"00000002-0000-4000-8000-000000000000",
			"00000000-0000-4000-8000-000000000102",
			fmt="Outdoor label",
		)
		queue = card_print.list_tag_queue(WORKER)
		group = next(g for g in queue["locations"] if g["location"] == "Pump House")
		self.assertEqual(group["count"], 2)
		self.assertEqual(group["formats"], {"Sheet": 1, "Outdoor label": 1})

	@NEEDS_QR
	def test_print_all_makes_one_sheet_marks_printed_and_a_second_copy_needs_a_reason(self):
		self.ask("Pump A", "00000000-0000-4000-8000-000000000201")
		self.ask("Pump B", "00000000-0000-4000-8000-000000000202")
		out = card_print.print_for_location(WORKER, None, "Pump House")
		self.assertEqual((out["sheet_labels"], out["cards_queued"]), (2, 0))
		self.assertIn("Pump A", out["sheet_html"])
		statuses = {r["status"] for r in frappe.db.get_all("Card Print Job", fields=["status"])}
		self.assertEqual(statuses, {"Printed"})
		self.assertEqual(card_print.list_tag_queue(WORKER)["count"], 0)
		with self.assertRaises(card_print.CardPrintError):
			self.ask("Pump A", "00000000-0000-4000-8000-000000000203")
		again = self.ask("Pump A", "00000000-0000-4000-8000-000000000204", reprint_reason="Damaged")
		self.assertTrue(again["job"]["is_reprint"])

	def test_a_bad_format_is_refused(self):
		with self.assertRaises(card_print.CardPrintError):
			self.ask("Pump A", "00000000-0000-4000-8000-000000000301", fmt="Tattoo")

	@unittest.skipUnless(qr.available() and card_art.reportlab_available(), "needs segno and reportlab")
	def test_a_card_tag_still_goes_to_the_station(self):
		job = self.ask("Pump A", "00000000-0000-4000-8000-000000000401", fmt="Card")["job"]
		self.assertEqual(job["print_station"], card_print.DEFAULT_STATION)
