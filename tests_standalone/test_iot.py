# SPDX-License-Identifier: MIT
"""The device network: what a sensor said, and whether to believe it.

1. **`last_seen` MEANS THE DEVICE SPOKE, AND ONLY THE READING PATH WRITES IT.**
   `TheLastSeenColumnMeansOneThing`. `update_iot_device` refuses the argument.
   The negative control is here too: recording a reading DOES move it, so a
   regression that stops writing it fails rather than passing on the refusal
   alone.

2. **ONLINE IS COMPUTED, NEVER STORED.** `TheDeviceHealthIsComputed`. A stored
   flag is wrong from the moment a device goes quiet, which is the only moment it
   matters. A silent probe is not reading zero.

3. **A READING'S BLOCK IS A COPY, NOT A LINK.** `TheReadingKeepsItsOwnBlock`.
   Moving a probe in July must not relocate June's readings, because the June
   irrigation decisions were justified from them.

4. **DUPLICATES ARE REFUSED.** `TheReplayIsRefused`. Devices retry and gateways
   replay, and a batch stored twice doubles every average computed off it.

5. **AGGREGATES NEVER CROSS A UNIT BOUNDARY.** `TheSummaryDoesNotMixUnits`. A
   device reconfigured mid-season reports one type in two units, and the summary
   says so rather than averaging through it.
"""

from .fixtures import MAIN, V12TestCase, seed_masters
from .harness import frappe

BLOCK = "Yellow Camp Block 3 - MC"
BLOCK_TWO = "Yellow Camp Block 4 - MC"

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_parcel",
		"create_field",
		"create_iot_device",
		"get_iot_device",
		"list_iot_devices",
		"update_iot_device",
		"create_iot_reading",
		"list_iot_readings",
		"get_device_readings",
	)
}


class IoTTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		seed_masters()
		self.configure(enabled=1, **ALL_ON)
		self.tool_data(
			"create_parcel",
			{
				"owning_entity": MAIN,
				"parcel_name": "Mill Creek",
				"acreage": 131.43,
				"county": "Wasco",
				"state": "OR",
				"use_type": "Orchard",
			},
		)
		for name in ("Yellow Camp Block 3", "Yellow Camp Block 4"):
			self.tool_data(
				"create_field",
				{"parcel": "Mill Creek", "field_name": name, "acreage": 12.5, "crop": "Cherry"},
			)

	def a_device(self, **kw):
		payload = {
			"company": MAIN,
			"device_name": "Yellow Camp Probe 1",
			"hardware_id": "AA:BB:CC:00:11:22",
			"device_type": "Soil Moisture",
			"field": BLOCK,
		}
		payload.update(kw)
		return self.tool_data("create_iot_device", payload)

	def a_reading(self, device, **kw):
		payload = {
			"device": device,
			"timestamp": "2026-06-01 06:00:00",
			"reading_type": "soil_moisture_vwc",
			"value": 0.24,
			"unit": "%",
		}
		payload.update(kw)
		return self.tool_data("create_iot_reading", payload)


class TheDeviceRegister(IoTTestCase):
	def test_a_device_is_registered_with_a_token_shown_once(self):
		created = self.a_device()
		self.assertEqual(created["device_type"], "Soil Moisture")
		self.assertEqual(created["device_class"], "Sensor")
		self.assertTrue(created["auth_token"])
		self.assertIn("only time", created["next_step"])

	def test_the_token_is_not_returned_by_a_later_read(self):
		"""Nothing reads it back. A device that loses it is re-registered."""
		created = self.a_device()
		read = self.tool_data("get_iot_device", {"device": created["name"]})
		self.assertNotIn("auth_token", read)

	def test_the_caller_cannot_choose_the_token(self):
		"""A token somebody chose is a token somebody can guess."""
		one = self.a_device()
		two = self.a_device(device_name="Probe 2", hardware_id="AA:BB:CC:00:11:33", auth_token="hunter2")
		self.assertNotEqual(two["auth_token"], "hunter2")
		self.assertNotEqual(one["auth_token"], two["auth_token"])

	def test_a_duplicate_hardware_id_is_refused(self):
		self.a_device()
		error = self.tool_error(
			"create_iot_device",
			{
				"company": MAIN,
				"device_name": "Different Name",
				"hardware_id": "AA:BB:CC:00:11:22",
				"device_type": "Temperature",
			},
		)
		self.assertIn("AA:BB:CC:00:11:22", error)

	def test_a_device_can_be_read_by_hardware_id(self):
		created = self.a_device()
		read = self.tool_data("get_iot_device", {"device": "AA:BB:CC:00:11:22"})
		self.assertEqual(read["name"], created["name"])

	def test_a_device_on_a_block_that_does_not_exist_is_refused(self):
		error = self.tool_error(
			"create_iot_device",
			{
				"company": MAIN,
				"device_name": "Ghost",
				"hardware_id": "DE:AD:BE:EF:00:00",
				"device_type": "Generic",
				"field": "Nowhere Block - XX",
			},
		)
		self.assertIn("Nowhere Block", error)

	def test_a_config_that_is_not_json_is_refused(self):
		error = self.tool_error(
			"create_iot_device",
			{
				"company": MAIN,
				"device_name": "Bad Config",
				"hardware_id": "DE:AD:BE:EF:00:01",
				"device_type": "Generic",
				"device_config": "{not json",
			},
		)
		self.assertIn("JSON", error)

	def test_a_battery_reading_of_exactly_zero_is_stored(self):
		"""A flat battery is a fact, and it is the one worth recording.

		Regression test for a staging helper that collapsed `0` into blank — the
		update reported nothing changed and the device kept reading as healthy.
		"""
		device = self.a_device()
		changed = self.tool_data("update_iot_device", {"device": device["name"], "battery_level": 0})
		self.assertEqual(changed["battery_level"], 0.0)
		self.assertTrue(changed["battery_low"])
		self.assertIn("battery_level", changed["changed"])

	def test_a_battery_over_one_hundred_percent_is_refused(self):
		device = self.a_device()
		error = self.tool_error("update_iot_device", {"device": device["name"], "battery_level": 140})
		self.assertIn("0 to 100", error)


class TheLastSeenColumnMeansOneThing(IoTTestCase):
	def test_update_refuses_to_set_last_seen(self):
		device = self.a_device()
		error = self.tool_error(
			"update_iot_device", {"device": device["name"], "last_seen": "2026-06-01 06:00:00"}
		)
		self.assertIn("last_seen", error)
		self.assertIn("Nothing was changed", error)

	def test_update_refuses_to_rotate_the_token(self):
		device = self.a_device()
		error = self.tool_error("update_iot_device", {"device": device["name"], "auth_token": "new-token"})
		self.assertIn("auth_token", error)

	def test_the_negative_control_a_reading_does_move_last_seen(self):
		"""The refusal above is only worth having if the reading path still writes it."""
		device = self.a_device()
		self.assertIsNone(self.tool_data("get_iot_device", {"device": device["name"]})["last_seen"])

		self.a_reading(device["name"], timestamp="2026-06-01 06:00:00")

		read = self.tool_data("get_iot_device", {"device": device["name"]})
		self.assertEqual(read["last_seen"], "2026-06-01 06:00:00")

	def test_a_reading_posted_by_hardware_id_also_moves_it(self):
		device = self.a_device()
		self.a_reading("AA:BB:CC:00:11:22", timestamp="2026-06-02 06:00:00")
		read = self.tool_data("get_iot_device", {"device": device["name"]})
		self.assertEqual(read["last_seen"], "2026-06-02 06:00:00")


class TheDeviceHealthIsComputed(IoTTestCase):
	def test_a_device_that_never_reported_says_so(self):
		device = self.a_device()
		read = self.tool_data("get_iot_device", {"device": device["name"]})
		self.assertFalse(read["online"])
		self.assertIsNone(read["seconds_since_seen"])
		self.assertTrue(any("never reported" in w for w in read["health_warnings"]))

	def test_a_stale_device_is_not_online_and_the_warning_says_how_long(self):
		device = self.a_device()
		self.a_reading(device["name"], timestamp="2026-06-01 06:00:00")
		read = self.tool_data("get_iot_device", {"device": device["name"]})
		self.assertFalse(read["online"])
		self.assertGreater(read["seconds_since_seen"], 600)
		self.assertTrue(any("Silent for" in w for w in read["health_warnings"]))

	def test_a_device_that_just_reported_is_online(self):
		device = self.a_device()
		self.a_reading(device["name"], timestamp=frappe.utils.now())
		read = self.tool_data("get_iot_device", {"device": device["name"]})
		self.assertTrue(read["online"])

	def test_a_low_battery_is_called_out_as_the_readings_to_distrust(self):
		device = self.a_device()
		self.tool_data("update_iot_device", {"device": device["name"], "battery_level": 8})
		read = self.tool_data("get_iot_device", {"device": device["name"]})
		self.assertTrue(read["battery_low"])
		self.assertTrue(any("drift" in w for w in read["health_warnings"]))

	def test_the_register_names_the_offline_devices_rather_than_counting_them(self):
		one = self.a_device()
		self.a_device(device_name="Probe 2", hardware_id="AA:BB:CC:00:11:33", field=BLOCK_TWO)
		self.a_reading(one["name"], timestamp=frappe.utils.now())

		listed = self.tool_data("list_iot_devices", {"company": MAIN})
		self.assertEqual(listed["device_count"], 2)
		self.assertEqual(listed["online_count"], 1)
		# NAMED, not counted. The docname is asserted rather than a position in
		# the list, which is ordered by device_name and would silently pass on
		# whichever row happened to sort second.
		silent = next(row for row in listed["devices"] if row["device_name"] == "Probe 2")
		self.assertEqual(listed["never_reported"], [silent["name"]])
		self.assertEqual(listed["offline"], [silent["name"]])

	def test_a_disabled_device_is_reported_as_historical_not_as_offline(self):
		device = self.a_device()
		self.tool_data("update_iot_device", {"device": device["name"], "enabled": False})
		read = self.tool_data("get_iot_device", {"device": device["name"]})
		self.assertTrue(any("historical" in w for w in read["health_warnings"]))
		self.assertFalse(any("Silent for" in w for w in read["health_warnings"]))

	def test_the_online_filter_selects_both_ways(self):
		one = self.a_device()
		self.a_device(device_name="Probe 2", hardware_id="AA:BB:CC:00:11:33")
		self.a_reading(one["name"], timestamp=frappe.utils.now())

		online = self.tool_data("list_iot_devices", {"company": MAIN, "online": True})
		self.assertEqual(online["device_count"], 1)
		offline = self.tool_data("list_iot_devices", {"company": MAIN, "online": False})
		self.assertEqual(offline["device_count"], 1)


class TheReadingKeepsItsOwnBlock(IoTTestCase):
	def test_a_reading_takes_the_block_off_the_device(self):
		device = self.a_device()
		reading = self.a_reading(device["name"])
		self.assertEqual(reading["field"], BLOCK)
		self.assertEqual(reading["company"], MAIN)

	def test_moving_the_device_does_not_move_an_earlier_reading(self):
		"""The claim this doctype's denormalisation exists for."""
		device = self.a_device()
		june = self.a_reading(device["name"], timestamp="2026-06-01 06:00:00")

		self.tool_data("update_iot_device", {"device": device["name"], "field": BLOCK_TWO})
		july = self.a_reading(device["name"], timestamp="2026-07-01 06:00:00")

		self.assertEqual(frappe.db.get_value("IoT Reading", june["name"], "field"), BLOCK)
		self.assertEqual(july["field"], BLOCK_TWO)

	def test_readings_can_be_listed_by_block(self):
		device = self.a_device()
		self.a_reading(device["name"], timestamp="2026-06-01 06:00:00")
		self.tool_data("update_iot_device", {"device": device["name"], "field": BLOCK_TWO})
		self.a_reading(device["name"], timestamp="2026-07-01 06:00:00")

		self.assertEqual(self.tool_data("list_iot_readings", {"field": BLOCK})["reading_count"], 1)
		self.assertEqual(self.tool_data("list_iot_readings", {"field": BLOCK_TWO})["reading_count"], 1)


class TheReplayIsRefused(IoTTestCase):
	def test_the_same_reading_twice_is_refused(self):
		device = self.a_device()
		self.a_reading(device["name"], timestamp="2026-06-01 06:00:00")
		error = self.tool_error(
			"create_iot_reading",
			{
				"device": device["name"],
				"timestamp": "2026-06-01 06:00:00",
				"reading_type": "soil_moisture_vwc",
				"value": 0.24,
				"unit": "%",
			},
		)
		self.assertIn("2026-06-01 06:00:00", error)

	def test_a_different_reading_type_at_the_same_moment_is_fine(self):
		"""One device reporting moisture and temperature at 06:00 is ordinary."""
		device = self.a_device()
		self.a_reading(device["name"], timestamp="2026-06-01 06:00:00")
		second = self.a_reading(
			device["name"],
			timestamp="2026-06-01 06:00:00",
			reading_type="soil_temp_c",
			value=14.2,
			unit="C",
		)
		self.assertEqual(second["reading_type"], "soil_temp_c")

	def test_a_reading_from_the_future_is_refused(self):
		device = self.a_device()
		error = self.tool_error(
			"create_iot_reading",
			{
				"device": device["name"],
				"timestamp": str(frappe.utils.add_to_date(frappe.utils.now(), days=3)),
				"reading_type": "air_temp_c",
				"value": 20,
				"unit": "C",
			},
		)
		self.assertIn("ahead of the server", error)
		self.assertIn("clock", error)

	def test_a_reading_from_a_disabled_device_is_refused(self):
		device = self.a_device()
		self.tool_data("update_iot_device", {"device": device["name"], "enabled": False})
		error = self.tool_error(
			"create_iot_reading",
			{
				"device": device["name"],
				"timestamp": "2026-06-01 06:00:00",
				"reading_type": "soil_moisture_vwc",
				"value": 0.24,
				"unit": "%",
			},
		)
		self.assertIn("disabled", error)
		self.assertIn("Nothing was recorded", error)

	def test_a_reading_with_no_unit_is_refused(self):
		device = self.a_device()
		error = self.tool_error(
			"create_iot_reading",
			{
				"device": device["name"],
				"timestamp": "2026-06-01 06:00:00",
				"reading_type": "air_temp_c",
				"value": 20,
			},
		)
		self.assertIn("unit", error.lower())


class TheSummaryDoesNotMixUnits(IoTTestCase):
	def a_series(self, device):
		for hour, value in (("06", 0.24), ("07", 0.22), ("08", 0.19)):
			self.a_reading(device, timestamp=f"2026-06-01 {hour}:00:00", value=value)

	def test_the_summary_is_per_reading_type(self):
		device = self.a_device()
		self.a_series(device["name"])
		self.a_reading(
			device["name"],
			timestamp="2026-06-01 06:00:00",
			reading_type="soil_temp_c",
			value=14.0,
			unit="C",
		)

		summary = self.tool_data("get_device_readings", {"device": device["name"]})
		self.assertEqual(set(summary["by_reading_type"]), {"soil_moisture_vwc", "soil_temp_c"})
		moisture = summary["by_reading_type"]["soil_moisture_vwc"]
		self.assertEqual(moisture["count"], 3)
		self.assertEqual(moisture["min"], 0.19)
		self.assertEqual(moisture["max"], 0.24)
		self.assertAlmostEqual(moisture["mean"], 0.2167, places=3)
		self.assertFalse(moisture["mixed_units"])

	def test_two_units_in_one_series_are_reported_not_averaged_through(self):
		device = self.a_device()
		self.a_reading(
			device["name"],
			timestamp="2026-06-01 06:00:00",
			reading_type="air_temp_c",
			value=14.0,
			unit="C",
		)
		self.a_reading(
			device["name"],
			timestamp="2026-06-01 07:00:00",
			reading_type="air_temp_c",
			value=57.2,
			unit="F",
		)
		summary = self.tool_data("get_device_readings", {"device": device["name"]})
		series = summary["by_reading_type"]["air_temp_c"]
		self.assertTrue(series["mixed_units"])
		self.assertEqual(series["units"], ["C", "F"])
		self.assertIn("reconfigured", series["note"])

	def test_the_date_range_narrows_the_series(self):
		device = self.a_device()
		self.a_series(device["name"])
		self.a_reading(device["name"], timestamp="2026-07-01 06:00:00", value=0.31)

		june = self.tool_data(
			"get_device_readings",
			{
				"device": device["name"],
				"from_timestamp": "2026-06-01 00:00:00",
				"to_timestamp": "2026-06-30 23:59:59",
			},
		)
		self.assertEqual(june["reading_count"], 3)
		self.assertEqual(june["by_reading_type"]["soil_moisture_vwc"]["count"], 3)

	def test_the_device_health_comes_back_with_the_numbers(self):
		"""A flatline is a dry block or a dead battery, and those are opposite calls."""
		device = self.a_device()
		self.a_series(device["name"])
		summary = self.tool_data("get_device_readings", {"device": device["name"]})
		self.assertEqual(summary["device"]["name"], device["name"])
		self.assertIn("health_warnings", summary["device"])

	def test_suspect_readings_are_included_unless_excluded_deliberately(self):
		device = self.a_device()
		self.a_reading(device["name"], timestamp="2026-06-01 06:00:00")
		self.a_reading(device["name"], timestamp="2026-06-01 07:00:00", value=9.9, quality="Suspect")

		listed = self.tool_data("list_iot_readings", {"device": device["name"]})
		self.assertEqual(listed["reading_count"], 2)
		self.assertEqual(listed["by_quality"], {"Good": 1, "Suspect": 1})

		good = self.tool_data("list_iot_readings", {"device": device["name"], "quality": "Good"})
		self.assertEqual(good["reading_count"], 1)
