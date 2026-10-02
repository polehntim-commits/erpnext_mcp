# SPDX-License-Identifier: MIT
"""Tests for v0.24.0 — Universal Asset Tags."""

from .fixtures import MAIN, V12TestCase

ALL_ON = {
	"allow_list_assets": 1,
	"allow_get_asset_detail": 1,
	"allow_get_asset_history": 1,
	"allow_scan_asset": 1,
	"allow_register_asset": 1,
	"allow_update_registered_asset": 1,
	"allow_retire_asset": 1,
	"allow_bulk_create_assets": 1,
	"allow_generate_asset_qr": 1,
	"allow_generate_asset_qr_sheet": 1,
}


class AssetTagTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def an_asset(self, name="MC-Valve-05", asset_type="Irrigation Valve", company=MAIN, **kw):
		payload = {"name": name, "asset_type": asset_type, "company": company, **kw}
		return self.tool_data("register_asset", payload)

	def a_block(self, name="MC-Block-A", company=MAIN):
		return self.an_asset(name=name, asset_type="Block", company=company)


class CreateAsset(AssetTagTestCase):
	def test_it_registers_an_asset(self):
		data = self.an_asset()
		self.assertEqual(data["name"], "MC-Valve-05")
		self.assertEqual(data["asset_type"], "Irrigation Valve")
		self.assertEqual(data["company"], MAIN)
		self.assertFalse(data["retired"])

	def test_the_qr_url_is_populated(self):
		data = self.an_asset()
		self.assertIn("/scan/MC-Valve-05", data["qr_url"])

	def test_a_duplicate_name_is_refused(self):
		self.an_asset()
		error = self.tool_error(
			"register_asset",
			{
				"name": "MC-Valve-05",
				"asset_type": "Irrigation Valve",
				"company": MAIN,
			},
		)
		self.assertIn("already has a record", error)

	def test_missing_name_is_refused(self):
		error = self.tool_error(
			"register_asset",
			{
				"asset_type": "Sprayer",
				"company": MAIN,
			},
		)
		self.assertIn("required", error.lower())

	def test_missing_asset_type_is_refused(self):
		error = self.tool_error(
			"register_asset",
			{
				"name": "MC-Test-01",
				"company": MAIN,
			},
		)
		self.assertIn("required", error.lower())

	def test_location_links_to_parent_asset(self):
		self.a_block()
		data = self.an_asset(name="MC-Valve-01", location="MC-Block-A")
		self.assertEqual(data["location"], "MC-Block-A")

	def test_nonexistent_location_is_refused(self):
		error = self.tool_error(
			"register_asset",
			{
				"name": "MC-Valve-99",
				"asset_type": "Irrigation Valve",
				"company": MAIN,
				"location": "NOWHERE",
			},
		)
		self.assertIn("does not exist", error)

	def test_gps_coordinates_are_stored(self):
		data = self.an_asset(gps_latitude=45.5152, gps_longitude=-122.6784)
		self.assertAlmostEqual(data["gps_latitude"], 45.5152, places=4)
		self.assertAlmostEqual(data["gps_longitude"], -122.6784, places=4)

	def test_nfc_uid_is_stored(self):
		data = self.an_asset(nfc_uid="04:A2:B3:C4:D5:E6:F7")
		self.assertEqual(data["nfc_uid"], "04:A2:B3:C4:D5:E6:F7")

	def test_description_is_stored(self):
		data = self.an_asset(description="Main water valve at block entrance")
		self.assertEqual(data["description"], "Main water valve at block entrance")


class ListAssets(AssetTagTestCase):
	def test_it_lists_all_assets(self):
		self.an_asset(name="MC-V-01", asset_type="Irrigation Valve")
		self.an_asset(name="MC-S-01", asset_type="Sprayer")
		data = self.tool_data("list_assets", {"company": MAIN})
		self.assertEqual(data["asset_count"], 2)
		names = [a["name"] for a in data["assets"]]
		self.assertIn("MC-V-01", names)
		self.assertIn("MC-S-01", names)

	def test_filter_by_asset_type(self):
		self.an_asset(name="MC-V-01", asset_type="Irrigation Valve")
		self.an_asset(name="MC-S-01", asset_type="Sprayer")
		data = self.tool_data("list_assets", {"company": MAIN, "asset_type": "Sprayer"})
		self.assertEqual(data["asset_count"], 1)
		self.assertEqual(data["assets"][0]["name"], "MC-S-01")

	def test_filter_by_location(self):
		self.a_block(name="MC-Block-A")
		self.an_asset(name="MC-V-01", location="MC-Block-A")
		self.an_asset(name="MC-V-02")
		data = self.tool_data("list_assets", {"company": MAIN, "location": "MC-Block-A"})
		self.assertEqual(data["asset_count"], 1)
		self.assertEqual(data["assets"][0]["name"], "MC-V-01")

	def test_retired_assets_excluded_by_default(self):
		self.an_asset(name="MC-V-01")
		self.an_asset(name="MC-V-02")
		self.tool_data("retire_asset", {"asset_name": "MC-V-02"})
		data = self.tool_data("list_assets", {"company": MAIN})
		self.assertEqual(data["asset_count"], 1)
		self.assertEqual(data["assets"][0]["name"], "MC-V-01")

	def test_retired_assets_included_when_asked(self):
		self.an_asset(name="MC-V-01")
		self.an_asset(name="MC-V-02")
		self.tool_data("retire_asset", {"asset_name": "MC-V-02"})
		data = self.tool_data("list_assets", {"company": MAIN, "retired": True})
		self.assertEqual(data["asset_count"], 1)
		self.assertEqual(data["assets"][0]["name"], "MC-V-02")

	def test_by_asset_type_counts(self):
		self.an_asset(name="MC-V-01", asset_type="Irrigation Valve")
		self.an_asset(name="MC-V-02", asset_type="Irrigation Valve")
		self.an_asset(name="MC-S-01", asset_type="Sprayer")
		data = self.tool_data("list_assets", {"company": MAIN})
		self.assertEqual(data["by_asset_type"]["Irrigation Valve"], 2)
		self.assertEqual(data["by_asset_type"]["Sprayer"], 1)

	def test_empty_register(self):
		data = self.tool_data("list_assets", {"company": MAIN})
		self.assertEqual(data["asset_count"], 0)
		self.assertEqual(data["assets"], [])


class GetAssetDetail(AssetTagTestCase):
	def test_it_returns_full_detail(self):
		self.an_asset()
		data = self.tool_data("get_asset_detail", {"asset_name": "MC-Valve-05"})
		self.assertEqual(data["name"], "MC-Valve-05")
		self.assertEqual(data["asset_type"], "Irrigation Valve")
		self.assertIn("history", data)
		self.assertIn("open_tasks", data)
		self.assertIn("children", data)

	def test_children_are_listed(self):
		self.a_block(name="MC-Block-A")
		self.an_asset(name="MC-V-01", location="MC-Block-A")
		self.an_asset(name="MC-V-02", location="MC-Block-A")
		data = self.tool_data("get_asset_detail", {"asset_name": "MC-Block-A"})
		self.assertEqual(data["child_count"], 2)
		child_names = [c["name"] for c in data["children"]]
		self.assertIn("MC-V-01", child_names)
		self.assertIn("MC-V-02", child_names)

	def test_nonexistent_asset_is_refused(self):
		error = self.tool_error("get_asset_detail", {"asset_name": "NOWHERE"})
		self.assertIn("no Asset Register", error)


class GetAssetHistory(AssetTagTestCase):
	def test_it_returns_history_for_existing_asset(self):
		self.an_asset()
		data = self.tool_data("get_asset_history", {"asset_name": "MC-Valve-05"})
		self.assertEqual(data["asset_name"], "MC-Valve-05")
		self.assertIn("events", data)
		self.assertIsInstance(data["events"], list)


class ScanAsset(AssetTagTestCase):
	def test_scan_updates_last_scan_at(self):
		self.an_asset()
		data = self.tool_data(
			"scan_asset",
			{
				"asset_name": "MC-Valve-05",
			},
		)
		self.assertTrue(data["scan_recorded"])
		self.assertIsNotNone(data["last_scan_at"])

	def test_scan_records_where_the_phone_was_and_does_not_move_a_valve(self):
		"""v0.214.0. A valve has a fixed location: the scan's fix is the PHONE's.
		`test_asset_moves` has the rest."""
		self.an_asset()
		data = self.tool_data(
			"scan_asset",
			{
				"asset_name": "MC-Valve-05",
				"gps_lat": 45.5152,
				"gps_lon": -122.6784,
			},
		)
		self.assertIsNone(data["gps_latitude"])
		self.assertFalse(data["position_updated"])
		self.assertAlmostEqual(data["scan_location"]["latitude"], 45.5152, places=4)

	def test_scan_nonexistent_is_refused(self):
		error = self.tool_error("scan_asset", {"asset_name": "NOWHERE"})
		self.assertIn("no Asset Register", error)

	def test_scan_is_audited(self):
		self.an_asset()
		self.tool_data("scan_asset", {"asset_name": "MC-Valve-05"})
		self.assertAudited("scan_asset", "Success")


class UpdateAsset(AssetTagTestCase):
	def test_it_changes_description(self):
		self.an_asset()
		data = self.tool_data(
			"update_registered_asset",
			{
				"asset_name": "MC-Valve-05",
				"description": "Updated valve description",
			},
		)
		self.assertEqual(data["description"], "Updated valve description")
		self.assertIn("description", data["changed"])

	def test_it_changes_location(self):
		self.a_block(name="MC-Block-A")
		self.an_asset()
		data = self.tool_data(
			"update_registered_asset",
			{
				"asset_name": "MC-Valve-05",
				"location": "MC-Block-A",
			},
		)
		self.assertEqual(data["location"], "MC-Block-A")

	def test_self_referential_location_is_refused(self):
		self.an_asset()
		error = self.tool_error(
			"update_registered_asset",
			{
				"asset_name": "MC-Valve-05",
				"location": "MC-Valve-05",
			},
		)
		self.assertIn("own parent", error)

	def test_no_changes_is_refused(self):
		self.an_asset()
		error = self.tool_error("update_registered_asset", {"asset_name": "MC-Valve-05"})
		self.assertIn("nothing to change", error)

	def test_it_changes_asset_type(self):
		self.an_asset()
		data = self.tool_data(
			"update_registered_asset",
			{
				"asset_name": "MC-Valve-05",
				"asset_type": "General",
			},
		)
		self.assertEqual(data["asset_type"], "General")

	def test_it_changes_nfc_uid(self):
		self.an_asset()
		data = self.tool_data(
			"update_registered_asset",
			{
				"asset_name": "MC-Valve-05",
				"nfc_uid": "AA:BB:CC:DD",
			},
		)
		self.assertEqual(data["nfc_uid"], "AA:BB:CC:DD")


class RetireAsset(AssetTagTestCase):
	def test_it_retires_an_asset(self):
		self.an_asset()
		data = self.tool_data(
			"retire_asset",
			{
				"asset_name": "MC-Valve-05",
				"reason": "Replaced by MC-Valve-06",
			},
		)
		self.assertTrue(data["retired"])
		self.assertIsNotNone(data["retired_at"])
		self.assertEqual(data["reason"], "Replaced by MC-Valve-06")

	def test_already_retired_is_refused(self):
		self.an_asset()
		self.tool_data("retire_asset", {"asset_name": "MC-Valve-05"})
		error = self.tool_error("retire_asset", {"asset_name": "MC-Valve-05"})
		self.assertIn("already retired", error)

	def test_reason_appended_to_description(self):
		self.an_asset(description="Main valve")
		data = self.tool_data(
			"retire_asset",
			{
				"asset_name": "MC-Valve-05",
				"reason": "Cracked housing",
			},
		)
		self.assertIn("Cracked housing", data["description"])
		self.assertIn("Main valve", data["description"])


class BulkCreateAssets(AssetTagTestCase):
	def test_it_creates_multiple_assets(self):
		data = self.tool_data(
			"bulk_create_assets",
			{
				"company": MAIN,
				"assets": [
					{"name": "MC-V-01", "asset_type": "Irrigation Valve"},
					{"name": "MC-V-02", "asset_type": "Irrigation Valve"},
					{"name": "MC-S-01", "asset_type": "Sprayer"},
				],
			},
		)
		self.assertEqual(data["created_count"], 3)
		self.assertEqual(data["error_count"], 0)

	def test_duplicates_are_skipped_not_fatal(self):
		self.an_asset(name="MC-V-01")
		data = self.tool_data(
			"bulk_create_assets",
			{
				"company": MAIN,
				"assets": [
					{"name": "MC-V-01", "asset_type": "Irrigation Valve"},
					{"name": "MC-V-02", "asset_type": "Irrigation Valve"},
				],
			},
		)
		self.assertEqual(data["created_count"], 1)
		self.assertEqual(data["error_count"], 1)
		self.assertIn("already exists", data["errors"][0]["error"])

	def test_missing_fields_are_errors(self):
		data = self.tool_data(
			"bulk_create_assets",
			{
				"company": MAIN,
				"assets": [
					{"name": "MC-V-01"},
					{"asset_type": "Sprayer"},
				],
			},
		)
		self.assertEqual(data["created_count"], 0)
		self.assertEqual(data["error_count"], 2)

	def test_empty_list_is_refused(self):
		error = self.tool_error(
			"bulk_create_assets",
			{
				"company": MAIN,
				"assets": [],
			},
		)
		self.assertIn("required", error.lower())

	def test_non_list_is_refused(self):
		error = self.tool_error(
			"bulk_create_assets",
			{
				"company": MAIN,
				"assets": "not a list",
			},
		)
		self.assertIn("list", error.lower())


class KillSwitches(AssetTagTestCase):
	def test_read_tools_on_by_default(self):
		self.configure(enabled=1)
		data = self.tool_data("list_assets", {"company": MAIN})
		self.assertEqual(data["asset_count"], 0)

	def test_read_tools_can_be_turned_off(self):
		self.configure(enabled=1, allow_list_assets=0)
		error = self.tool_error("list_assets", {"company": MAIN})
		self.assertIn("switched off", error.lower())

	def test_mutating_tools_off_by_default(self):
		self.configure(enabled=1)
		error = self.tool_error(
			"register_asset",
			{
				"name": "MC-V-01",
				"asset_type": "Sprayer",
				"company": MAIN,
			},
		)
		self.assertIn("switched off", error.lower())

	def test_mutating_tool_works_when_enabled(self):
		self.configure(enabled=1, allow_register_asset=1)
		data = self.tool_data(
			"register_asset",
			{
				"name": "MC-V-01",
				"asset_type": "Sprayer",
				"company": MAIN,
			},
		)
		self.assertEqual(data["name"], "MC-V-01")


class AssetTypesCoverage(AssetTagTestCase):
	def test_all_ten_types_accepted(self):
		types = [
			"Housing Unit",
			"Irrigation Zone",
			"Irrigation Valve",
			"Sprayer",
			"Tractor",
			"Block",
			"Water Source",
			"Storage",
			"Cold Storage",
			"General",
		]
		for i, asset_type in enumerate(types):
			data = self.an_asset(name=f"TEST-{i:02d}", asset_type=asset_type)
			self.assertEqual(data["asset_type"], asset_type)
