# SPDX-License-Identifier: MIT
"""v0.328.0 — task photos where people look: on the asset (linked, not copied), in the task, with their EXIF.

Tim's camp-buildings insurance report (2026-10-13): MC-SHOWER-TOILET-1 showed no tasks though FT-2026-10-00013
photographed it; side photos never reached the assets; and none of 144 photos kept GPS or a capture time.
"""

import frappe

from erpnext_mcp import asset_photos, housing_assets
from erpnext_mcp.farmops_api import routes

from . import test_housing as th
from .harness import STORE
from .test_task_progress_v326 import FOUR, ProgressCase


def photo_files(prefix, n, *, hash_prefix="h"):
	return [{"name": f"{prefix}{i}", "file_name": f"{prefix}_{i}.jpg", "file_url": f"/private/files/{prefix}_{i}.jpg",
	         "is_private": 1, "content_hash": f"{hash_prefix}{prefix}{i}", "file_size": 1000} for i in range(n)]


class PhotoCase(ProgressCase):
	def barn_task(self, task_type="Inspection"):
		STORE.seed("Asset Register", [{"name": "MC-BARN", "asset_type": "Barn"}])
		STORE.seed("File", photo_files("B", 4) + [
			{"name": "SIG", "file_name": "sig.png", "file_url": "/private/files/sig.png", "content_hash": "hs"},
			{"name": "BEF", "file_name": "before.jpg", "file_url": "/private/files/before.jpg", "content_hash": "hb"}])
		task = self.insurance_task()
		frappe.db.set_value("Farm Task", task, {"location_doctype": "Asset Register", "location": "MC-BARN",
		                                        "task_type": task_type})
		STORE.commit()
		return task

	def complete_with_sides(self, task):
		files = [{"file": f"B{i}", "checklist_item": item} for i, item in enumerate(FOUR)]
		files += [{"file": "SIG", "kind": "signature"}, {"file": "BEF", "phase": "before"}]
		return self.tool_data("complete_farm_task", {"task": task, "worker_id": "EMP-001", "evidence_files": files})

	def links_on(self, asset):
		return [f for f in STORE.rows("File") if f.get("attached_to_doctype") == "Asset Register"
		        and f.get("attached_to_name") == asset]


class SidePhotosAreLinkedToTheAsset(PhotoCase):
	def test_the_four_sides_are_linked_and_nothing_else(self):
		task = self.barn_task()
		data = self.complete_with_sides(task)
		links = self.links_on("MC-BARN")
		self.assertEqual(len(links), 4)
		self.assertEqual(len(data["asset_photo_links"]), 4)
		self.assertTrue(all(link["file_name"].startswith("MC-BARN_") for link in links))
		self.assertIn("MC-BARN_FRONT_", " ".join(link["file_name"] for link in links))
		urls = {link["file_url"] for link in links}
		self.assertNotIn("/private/files/sig.png", urls, "never a signature")
		self.assertNotIn("/private/files/before.jpg", urls, "never a before photo")

	def test_a_link_is_the_same_bytes_not_a_copy(self):
		task = self.barn_task()
		self.complete_with_sides(task)
		link = next(f for f in self.links_on("MC-BARN") if f["file_url"].endswith("B_0.jpg"))
		self.assertEqual(link["content_hash"], "hB0", "deleting either row leaves the bytes for the other")

	def test_idempotent(self):
		task = self.barn_task()
		self.complete_with_sides(task)
		assignment = self.assignment(task)
		again = asset_photos.link_assignment(assignment)
		self.assertTrue(all(r["outcome"] in ("already", "skipped") for r in again))
		self.assertEqual(len(self.links_on("MC-BARN")), 4)

	def test_only_building_and_equipment_task_types(self):
		task = self.barn_task(task_type="Training")
		self.complete_with_sides(task)
		self.assertEqual(self.links_on("MC-BARN"), [])

	def test_a_multi_asset_task_needs_the_photos_own_asset(self):
		task = self.barn_task()
		STORE.seed("Asset Register", [{"name": "MC-SHOP", "asset_type": "Shop"}])
		STORE.commit()
		row = {"file": "B0", "checklist_item": "FRONT photo taken", "evidence_type": "Photo"}
		info = {"name": task, "task_type": "Inspection", "asset": "MC-SHOP", "location_doctype": "Asset Register",
		        "location": "MC-BARN"}
		self.assertIn("names none of them", asset_photos.link_row(row, info)[0]["reason"])
		tagged = asset_photos.link_row({**row, "asset": "MC-SHOP"}, info)
		self.assertEqual(tagged[0]["asset"], "MC-SHOP")

	def test_marked_for_the_asset_without_a_side(self):
		task = self.barn_task()
		self.tool_data("complete_farm_task", {"task": task, "worker_id": "EMP-001", "checklist": FOUR,
		                                      "evidence_files": [{"file": "B0", "for_asset": True}, {"file": "B1"}]})
		self.assertEqual([f["file_url"] for f in self.links_on("MC-BARN")], ["/private/files/B_0.jpg"])


class TheAssetShowsItsTasks(PhotoCase):
	def test_a_completed_task_found_through_its_location_with_photos(self):
		task = self.barn_task()
		self.complete_with_sides(task)
		history = asset_photos.tasks_for_asset("MC-BARN")
		self.assertEqual(history[0]["task"], task)
		self.assertEqual(history[0]["state"], "Completed")
		self.assertEqual(history[0]["assignment"], self.assignment(task))
		self.assertGreaterEqual(history[0]["photo_count"], 4)

	def test_get_farm_task_carries_the_gallery(self):
		task = self.barn_task()
		self.complete_with_sides(task)
		gallery = self.tool_data("get_farm_task", {"task": task})["gallery"]
		self.assertEqual(gallery["count"], 6)
		steps = {e["step"] for e in gallery["assignments"][0]["evidence"] if e["step"]}
		self.assertEqual(steps, set(FOUR))


class TheBackfillIsReviewedFirst(PhotoCase):
	def test_dry_run_lists_links_and_the_hand_attached_copies(self):
		task = self.barn_task()
		self.tool_data("complete_farm_task", {"task": task, "worker_id": "EMP-001", "checklist": FOUR,
		                                      "evidence_files": [{"file": "B0", "checklist_item": FOUR[0]}]})
		for f in self.links_on("MC-BARN"):  # as if completed before v0.328.0: no links yet
			STORE.tables["File"].pop(f["name"])
		STORE.seed("File", [{"name": "MAN", "file_name": "MC-BARN_FRONT_2026-10-13.jpg", "attached_to_doctype":
		                     "Asset Register", "attached_to_name": "MC-BARN", "file_url": "/private/files/man.jpg",
		                     "content_hash": "re-encoded"}])
		STORE.commit()
		dry = asset_photos.backfill([task])
		self.assertTrue(dry["dry_run"])
		self.assertEqual([r["outcome"] for r in dry["links"]], ["would_link"])
		self.assertEqual([m["file"] for m in dry["manual_copies"]], ["MAN"])
		self.assertEqual(self.links_on("MC-BARN")[0]["name"], "MAN", "a dry run writes nothing")
		done = asset_photos.backfill([task], dry_run=False, replace_manual=True)
		self.assertEqual(done["manual_replaced"][0]["outcome"], "removed")
		self.assertEqual([f["file_url"] for f in self.links_on("MC-BARN")], ["/private/files/B_0.jpg"])


class ExifIsKeptForWorkNotPeople(PhotoCase):
	def test_which_records_keep_it(self):
		self.assertTrue(asset_photos.keeps_exif("Farm Task Assignment"))
		self.assertTrue(asset_photos.keeps_exif("Asset Register"))
		self.assertTrue(asset_photos.keeps_exif("", "FT-2026-10-00015_photo_after_X.jpg"))
		self.assertFalse(asset_photos.keeps_exif("Employee"))
		self.assertFalse(asset_photos.keeps_exif("I-9 Form"))
		self.assertFalse(asset_photos.keeps_exif("", "badge.jpg"))

	def test_the_setting_is_off_only_inside_the_save(self):
		class Settings(dict):
			__getattr__ = dict.get

			def __setattr__(self, key, value):
				self[key] = value

		original = getattr(frappe.local, "system_settings", None)
		frappe.local.system_settings = Settings(strip_exif_metadata_from_uploaded_images=1)
		real = getattr(frappe, "get_system_settings", None)
		frappe.get_system_settings = lambda key: frappe.local.system_settings.get(key)
		try:
			with asset_photos.exif_kept(True):
				self.assertEqual(frappe.local.system_settings["strip_exif_metadata_from_uploaded_images"], 0)
			self.assertEqual(frappe.local.system_settings["strip_exif_metadata_from_uploaded_images"], 1)
		finally:
			frappe.local.system_settings = original
			if real is None:
				del frappe.get_system_settings
			else:
				frappe.get_system_settings = real


class ACabinsValueLandsOnItsTag(th.HousingTestCase):
	def test_create_update_and_read(self):
		self.configure(enabled=1, **th.ALL_ON)
		self.a_parcel()
		unit = self.a_unit(unit_name="MC-CABIN-5&6", replacement_value=45000, purchase_value=30000,
		                   acquired_on="2019-05-01")["name"]
		tag = STORE.get_raw("Asset Register", unit)
		self.assertEqual(tag["replacement_value"], 45000)
		self.assertEqual(str(tag["acquired_on"]), "2019-05-01")
		self.tool_data("update_housing_unit", {"unit": unit, "replacement_value": 52000})
		self.assertEqual(self.tool_data("get_housing_unit", {"unit": unit})["value"]["replacement_value"], 52000)

	def test_a_bad_value_writes_nothing(self):
		self.configure(enabled=1, **th.ALL_ON)
		self.a_parcel()
		before = len(STORE.rows("Housing Unit"))
		self.assertIn("must be a number", self.tool_error("create_housing_unit", {
			"parcel": "Mill Creek", "unit_name": "X", "unit_type": "Cabin", "replacement_value": "lots"}))
		self.assertEqual(len(STORE.rows("Housing Unit")), before)

	def test_housing_assets_set_values_refuses_negative(self):
		self.configure(enabled=1, **th.ALL_ON)
		self.a_parcel()
		unit = self.a_unit(unit_name="MC-CABIN-1&2")["name"]
		with self.assertRaises(ValueError):
			housing_assets.set_values(unit, {"replacement_value": -5})


class ADroppedPhoneFieldIsSaid(PhotoCase):
	def test_bind_logs_what_it_drops_once(self):
		routes._WARNED.clear()
		route = next(r for r in routes.ROUTES if r.path == "/mobile/create_housing_unit")
		before = len(STORE.errors)
		kept = routes.bind(route, {"unit_name": "X", "serial_number": "123", "_auth": "secret"})
		routes.bind(route, {"unit_name": "Y", "serial_number": "456"})
		self.assertNotIn("serial_number", kept)
		rows = STORE.errors[before:]
		self.assertEqual(len(rows), 1)
		self.assertIn("serial_number", rows[0]["message"])
		self.assertNotIn("_auth", rows[0]["message"])
