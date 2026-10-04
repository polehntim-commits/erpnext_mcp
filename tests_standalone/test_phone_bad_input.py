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
