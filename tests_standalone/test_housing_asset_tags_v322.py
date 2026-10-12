# SPDX-License-Identifier: MIT
"""v0.322.0 — every Housing Unit has its tag on the Asset Register.

Tim's two Mill Creek cabins, registered from the phone, were missing from the phone's map: the map and asset lists
read the Asset Register and `create_housing_unit` never wrote one. Now the Housing Unit controller pairs each unit
with one tag — on any path — and a patch backfills the units already there.
"""

import frappe

from erpnext_mcp import housing_assets
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.patches import housing_unit_asset_tags

from . import test_housing as th
from .fixtures import MAIN
from .harness import STORE

GPS = {"gps_latitude": 45.5852, "gps_longitude": -121.2310}


class EveryDoorMakesTheTag(th.HousingTestCase):
	def test_create_housing_unit_makes_a_mapped_tag(self):
		self.a_parcel()
		unit = self.a_unit(unit_name="MC-CABIN-1&2", **GPS)
		asset = frappe.db.get_value("Housing Unit", unit["name"], "asset_tag")
		self.assertEqual(asset, unit["name"], "the unit's docname is the printable tag ID")
		row = STORE.get_raw("Asset Register", asset)
		self.assertEqual(row["asset_type"], "Housing Unit")
		self.assertEqual(row["company"], MAIN)
		self.assertAlmostEqual(row["gps_latitude"], 45.5852)
		self.assertIn("Mill Creek", row["description"])
		self.assertTrue(row.get("qr_url"), "the tag comes with its QR")
		self.assertEqual(self.tool_data("get_housing_unit", {"unit": unit["name"]})["asset_tag"], asset)

	def test_it_is_on_the_phone_map(self):
		self.a_parcel()
		unit = self.a_unit(unit_name="MC-CABIN-3&4", **GPS)
		rows, _ = mobile_api._asset_map_rows(MAIN, [MAIN])
		self.assertIn(unit["name"], [row["name"] for row in rows])

	def test_a_desk_insert_makes_it_too(self):
		parcel = self.a_parcel()["name"]
		doc = frappe.get_doc({"doctype": "Housing Unit", "unit_name": "Bunkhouse", "parcel": parcel,
		                      "unit_type": "Cabin", "capacity": 8})
		doc.insert(ignore_permissions=True)
		self.assertTrue(frappe.db.get_value("Housing Unit", doc.name, "asset_tag"))

	def test_saving_again_never_makes_a_second_tag_and_keeps_gps_in_step(self):
		self.a_parcel()
		unit = self.a_unit(unit_name="MC-CABIN-1&2")
		before = len(STORE.rows("Asset Register"))
		self.tool_data("update_housing_unit", {"unit": unit["name"], **GPS})
		self.assertEqual(len(STORE.rows("Asset Register")), before)
		self.assertAlmostEqual(STORE.get_raw("Asset Register", unit["name"])["gps_latitude"], 45.5852)


class AnExistingTagIsAdoptedNotDuplicated(th.HousingTestCase):
	def test_a_building_tag_with_the_units_name_is_adopted(self):
		self.a_parcel()
		STORE.seed("Asset Register", [{"name": "MC-Cabin-01 - MC", "asset_type": "Cabin", "company": MAIN,
		                               "description": "tagged by hand last spring"}])
		STORE.commit()
		unit = self.a_unit()
		self.assertEqual(frappe.db.get_value("Housing Unit", unit["name"], "asset_tag"), "MC-Cabin-01 - MC")
		self.assertEqual(STORE.get_raw("Asset Register", "MC-Cabin-01 - MC")["description"],
		                 "tagged by hand last spring", "somebody's own words stand")

	def test_a_non_building_with_the_name_is_left_alone(self):
		self.a_parcel()
		STORE.seed("Asset Register", [{"name": "MC-Cabin-01 - MC", "asset_type": "Valve", "company": MAIN}])
		STORE.commit()
		unit = self.a_unit()
		tag = frappe.db.get_value("Housing Unit", unit["name"], "asset_tag")
		self.assertEqual(tag, "MC-Cabin-01 - MC (housing)")
		self.assertEqual(STORE.get_raw("Asset Register", "MC-Cabin-01 - MC")["asset_type"], "Valve")
		# Scanning the cabin's own tag answers with the cabin, not a bare register row.
		self.configure(enabled=1, allow_universal_scan=1, **th.ALL_ON)
		scanned = self.tool_data("universal_scan", {"content": tag})
		self.assertEqual((scanned["entity_type"], scanned["entity_name"]), ("housing_unit", unit["name"]))


class TheBackfill(th.HousingTestCase):
	def test_units_from_before_get_one_tag_each_and_a_rerun_changes_nothing(self):
		self.a_parcel()
		names = [self.a_unit(unit_name=n, **GPS)["name"] for n in ("MC-CABIN-1&2", "MC-CABIN-3&4")]
		# What OML holds today: units with no tag at all.
		for name in names:
			frappe.db.set_value("Housing Unit", name, "asset_tag", None)
			frappe.delete_doc("Asset Register", name, force=True, ignore_permissions=True)
		housing_unit_asset_tags.execute()
		for name in names:
			self.assertEqual(frappe.db.get_value("Housing Unit", name, "asset_tag"), name)
		count = len(STORE.rows("Asset Register"))
		housing_unit_asset_tags.execute()
		report = housing_assets.backfill()
		self.assertEqual(len(STORE.rows("Asset Register")), count)
		self.assertEqual(report["created"] + report["adopted"], [])


class DeletingAUnitRetiresItsTag(th.HousingTestCase):
	def test_the_tag_is_kept_and_retired(self):
		self.a_parcel()
		unit = self.a_unit(unit_name="Old shed", unit_type="Shop", capacity=0)
		frappe.delete_doc("Housing Unit", unit["name"], ignore_permissions=True)
		self.assertTrue(STORE.get_raw("Asset Register", unit["name"])["retired_at"])


class TheDeskOverviewDrawsACabinOnce(th.HousingTestCase):
	def test_a_units_tag_is_not_a_second_pin(self):
		from erpnext_mcp import farm_overview

		self.a_parcel()
		unit = self.a_unit(unit_name="MC-CABIN-1&2", **GPS)
		data = farm_overview.farm_overview(company=MAIN)
		asset_names = [m.get("name") for m in data.get("assets") or []]
		self.assertNotIn(unit["name"], asset_names, "drawn as a housing unit, not again as an asset")
