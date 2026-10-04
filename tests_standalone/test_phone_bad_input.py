# SPDX-License-Identifier: MIT
"""Phone routes answer bad input with a 400 that names the argument, never a 500. v0.230.3.

The server audit of 2026-10-03 found eight inputs that reached a phone as a 500:
`int("abc")` on a limit, `json.loads` on a plain string, `.items()` on a list,
iterating a number, and a filter on a column the doctype does not have.
"""

import frappe

from erpnext_mcp.api import mobile as mobile_api

from .test_api_mobile import MobileAPITestCase
from .test_app_feedback import FEEDBACK, AppFeedbackTestCase, a_note


class TheShapesAreChecked(MobileAPITestCase):
	def refused(self, call, *args, **kwargs) -> str:
		with self.assertRaises(frappe.ValidationError) as caught:
			call(*args, **kwargs)
		return str(caught.exception)

	def test_the_three_helpers(self):
		self.assertEqual(mobile_api._limit_argument(None, 20, 50), 20)
		self.assertEqual(mobile_api._limit_argument("500", 20, 50), 50)
		self.assertIn("limit must be a whole number", self.refused(mobile_api._limit_argument, "abc", 20, 50))
		self.assertEqual(mobile_api._list_argument('["a"]', "x"), ["a"])
		self.assertIn("employees must be a list", self.refused(mobile_api._list_argument, 5, "employees"))
		self.assertIn("badge_ids must be a list", self.refused(mobile_api._list_argument, True, "badge_ids"))
		self.assertEqual(mobile_api._object_argument("", "context"), {})
		self.assertIn("context must be an object", self.refused(mobile_api._object_argument, "[1]", "context"))
		self.assertIn("context must be an object", self.refused(mobile_api._object_argument, [1], "context"))

	def test_search_link_limit_and_unknown_filter(self):
		self.be()
		self.assertIn("limit must be a whole number", self.refused(mobile_api.search_link, doctype="Item", limit="abc"))
		message = self.refused(mobile_api.search_link, doctype="Item", filters={"no_such_column": "x"})
		self.assertIn("no field no_such_column", message)

	def test_list_my_inspections_limit(self):
		self.be()
		self.assertIn("limit must be a whole number", self.refused(mobile_api.list_my_inspections, limit="abc"))

	def test_device_capabilities_field_kinds_must_be_a_list(self):
		self.be()
		self.assertIn(
			"field_kinds must be a list",
			self.refused(mobile_api.report_device_capabilities, device_identifier="X", field_kinds=5),
		)


class APlainRoleIsARole(AppFeedbackTestCase):
	def test_roles_as_a_plain_string_files_the_note(self):
		body = a_note(roles="Field Worker")
		body.pop("role")
		self.message(FEEDBACK, body)
		self.assertEqual(self.only()["role"], "Field Worker")

	def test_roles_as_a_number_is_ignored_not_a_500(self):
		body = a_note(roles=5)
		body.pop("role")
		self.message(FEEDBACK, body)
		self.assertFalse(self.only().get("role"))


class APartlyMeasuredTankSaysSo(MobileAPITestCase):
	def test_the_unmeasured_blocks_are_named(self):
		rows = [{"block": "Home-7", "acres": 10.0}, {"block": "TAG-8"}]
		lines = mobile_api._unmeasured_blocks_warning(rows, 10.0)
		self.assertIn("TAG-8 has no acreage", lines[0])
		self.assertEqual(mobile_api._unmeasured_blocks_warning([{"block": "Home-7", "acres": 10.0}], 10.0), [])
		self.assertEqual(mobile_api._unmeasured_blocks_warning([{"block": "TAG-8"}], 0), [])


class ASprayResendIsTheSameSpray(MobileAPITestCase):
	"""v0.230.6. A phone spray resent after a lost reply filed a second application,
	task, REI set and stock drawdown. It now answers with the first."""

	def test_a_known_request_id_answers_from_the_receipt(self):
		from unittest import mock

		from erpnext_mcp import request_receipts

		self.be()
		with mock.patch.object(request_receipts, "earlier", return_value={"application": "SPRAY-1"}) as seen:
			answer = mobile_api.record_spray_application(blocks=["X"], client_request_id="spray-req-1")
		self.assertEqual(answer, {"application": "SPRAY-1", "replayed": True})
		self.assertEqual(seen.call_args.args[0], "record_spray_application")


class OfflineResendsAreTheSameWrite(MobileAPITestCase):
	"""v0.231.1. An asset report or a shift start resent after a lost reply filed a
	second task / opened a second shift. Both now answer with the first."""

	def _replays(self, route, keyed_as=None, **kwargs):
		from unittest import mock

		from erpnext_mcp import request_receipts

		self.be()
		with mock.patch.object(request_receipts, "earlier", return_value={"name": "FIRST"}) as seen:
			answer = getattr(mobile_api, route)(client_request_id=f"{route}-1", **kwargs)
		self.assertEqual(answer, {"name": "FIRST", "replayed": True})
		self.assertEqual(seen.call_args.args[0], keyed_as or route)

	def test_an_asset_report_resend_is_the_first_report(self):
		# Shares report_field_task's receipts: the phone's queued retry of a live
		# asset report goes there.
		self._replays("report_asset_issue", keyed_as="report_field_task",
		              asset_name="TC-TRAKHOE-1", description="hydraulic leak")

	def test_a_shift_start_resend_is_the_first_shift(self):
		self._replays("start_shift", location="Home-7", start_datetime="2026-10-04 06:01:12")


class AShiftStartsWhenThePhoneOpenedIt(MobileAPITestCase):
	"""v0.231.1. The phone's start stands inside the offline window; outside it, or
	ahead of the server, the server's time is used and the answer says so."""

	def _start(self, start_datetime):
		from unittest import mock

		from erpnext_mcp.tools import shifts

		self.be()
		seen = {}

		class Result:
			data = {"name": "SHIFT-1"}

		def fake(inner):
			seen.update(inner)
			return Result()

		with mock.patch.object(shifts, "start_shift", side_effect=fake), \
			mock.patch("frappe.utils.now", return_value="2026-10-04 14:40:00"):
			answer = mobile_api.start_shift(location="Home-7", start_datetime=start_datetime)
		return seen, answer

	def test_a_start_inside_the_window_is_the_phones(self):
		seen, answer = self._start("2026-10-04 06:02:00")
		self.assertEqual(seen["start_datetime"], "2026-10-04 06:02:00")
		self.assertNotIn("start_time_note", answer)

	def test_a_start_older_than_the_window_uses_the_servers_time(self):
		seen, answer = self._start("2026-10-01 06:02:00")
		self.assertNotIn("start_datetime", seen)
		self.assertIn("offline window", answer["start_time_note"])

	def test_a_phone_clock_ahead_uses_the_servers_time(self):
		seen, answer = self._start("2026-10-04 18:00:00")
		self.assertNotIn("start_datetime", seen)
		self.assertIn("phone clock may be wrong", answer["start_time_note"])


class TaskClocksQueuedOfflineCountFromTheTap(MobileAPITestCase):
	"""v0.231.2. Start, pause and resume queued with no signal carry the tap; inside the
	offline window it is the time recorded, outside it the server's time is, with a note."""

	NOW = "2026-10-04 14:40:00"

	def _call(self, route, tool_path, time_key, tapped_at):
		from unittest import mock

		from erpnext_mcp import request_receipts
		from erpnext_mcp.api import guard

		self.be()
		seen = {}

		class Result:
			data = {"task": {"name": "FT-1"}, "assignment": {}, "state": "ok"}

		def fake(inner):
			seen.update(inner)
			return Result()

		module, attr = tool_path
		with mock.patch.object(module, attr, side_effect=fake), \
			mock.patch.object(guard, "require_scoped_doc", side_effect=lambda dt, v, *a: v), \
			mock.patch.object(mobile_api, "_employee", return_value="HR-EMP-1"), \
			mock.patch.object(request_receipts, "earlier", return_value=None), \
			mock.patch.object(request_receipts, "remember"), \
			mock.patch("frappe.utils.now", return_value=self.NOW):
			answer = getattr(mobile_api, route)(task="FT-1", tapped_at=tapped_at, client_request_id=f"{route}-1")
		return seen.get(time_key), answer

	def _each(self):
		from erpnext_mcp.tools import dispatch, fieldwork

		return (
			("start_task", (fieldwork, "start_task_via_mobile"), "started_at"),
			("pause_task_via_mobile", (dispatch, "pause_farm_task"), "paused_at"),
			("resume_task_via_mobile", (dispatch, "resume_farm_task"), "resumed_at"),
		)

	def test_a_tap_inside_the_window_is_the_time_recorded(self):
		for route, tool, key in self._each():
			with self.subTest(route):
				when, answer = self._call(route, tool, key, "2026-10-04 09:15:00")
				self.assertEqual(when, "2026-10-04 09:15:00")
				self.assertNotIn("time_note", answer)

	def test_a_stale_or_future_tap_uses_the_servers_time_and_says_so(self):
		for route, tool, key in self._each():
			for tapped in ("2026-10-01 09:15:00", "2026-10-04 18:00:00"):
				with self.subTest(route=route, tapped=tapped):
					when, answer = self._call(route, tool, key, tapped)
					self.assertIsNone(when)
					self.assertIn("server's time was used", answer["time_note"])

	def test_no_tap_is_the_old_behaviour(self):
		for route, tool, key in self._each():
			with self.subTest(route):
				when, answer = self._call(route, tool, key, None)
				self.assertIsNone(when)
				self.assertNotIn("time_note", answer)
