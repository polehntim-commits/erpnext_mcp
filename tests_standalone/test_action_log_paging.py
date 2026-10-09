# SPDX-License-Identifier: MIT
"""Reading the whole MCP Action Log, and the 30-day evidence. v0.284.0 (consolidation phase 6).

`query_doctype` pages past its 500-row cap with `start` / `next_start`, so every row comes back once and in order.
`get_superseded_usage` counts old-name calls per tool from the Superseded By column, lists the covered names nobody
called, and says when the log is younger than the window.
"""

import datetime

from erpnext_mcp import superseded

from .fixtures import MASTER_SUPPLIER, PurchasingTestCase
from .harness import STORE, frappe

LOG = "MCP Action Log"


def seed_log(count, start="2026-10-01 00:00:00", tool="get_company_topology", **extra):
	base = datetime.datetime.fromisoformat(start)
	STORE.seed(LOG, [{"name": f"LOG-{tool}-{i:05d}", "timestamp": str(base + datetime.timedelta(minutes=i)),
	                  "tool_name": tool, "result_status": "Success", **extra} for i in range(count)])
	STORE.commit()


class Paging(PurchasingTestCase):
	def test_every_row_past_500_comes_back_once_in_order(self):
		seed_log(1234)
		seen, start = [], 0
		while start is not None:
			page = self.tool_data("query_doctype", {"doctype": LOG, "fields": ["name", "timestamp"],
			                                        "filters": {"tool_name": "get_company_topology"},
			                                        "order_by": "timestamp asc", "limit": 500, "start": start})
			seen += [r["name"] for r in page["records"]]
			self.assertEqual(page["start"], start)
			start = page["next_start"]
		self.assertEqual(len(seen), 1234)
		self.assertEqual(len(set(seen)), 1234)
		self.assertEqual(seen, sorted(seen))

	def test_a_short_answer_has_no_next_page(self):
		seed_log(3)
		page = self.tool_data("query_doctype", {"doctype": LOG, "filters": {"tool_name": "get_company_topology"}})
		self.assertIsNone(page["next_start"])
		self.assertEqual(page["start"], 0)


class Usage(PurchasingTestCase):
	def test_old_name_calls_are_counted_and_the_uncalled_listed(self):
		for _ in range(3):
			self.tool_data("get_supplier", {"name": MASTER_SUPPLIER}, headers={"User-Agent": "brief-3am/1.0"})
		got = self.tool_data("get_superseded_usage", {"days": 30})
		row = next(r for r in got["used"] if r["tool"] == "get_supplier")
		self.assertEqual(row["calls"], 3)
		self.assertEqual(row["covered_by"], "record")
		self.assertIn("brief-3am/1.0", row["clients"])
		self.assertNotIn("get_supplier", got["unused"]["record"])
		self.assertIn("create_supplier", got["unused"]["record"])
		self.assertEqual(got["unused_count"] + len(got["used"]), len(superseded.covering()))
		self.assertFalse(got["hide_superseded_tools"])

	def test_a_log_younger_than_the_window_says_so(self):
		self.tool_data("get_supplier", {"name": MASTER_SUPPLIER})
		got = self.tool_data("get_superseded_usage", {})
		self.assertFalse(got["window_covered"])
		self.assertIn("not yet 30 days", got["note"])
		old = str(frappe.utils.add_days(frappe.utils.now(), -40))
		seed_log(1, start=old[:19])
		self.assertTrue(self.tool_data("get_superseded_usage", {})["window_covered"])

	def test_calls_outside_the_window_do_not_count(self):
		old = str(frappe.utils.add_days(frappe.utils.now(), -60))[:19]
		seed_log(5, start=old, tool="get_supplier", superseded_by="record")
		got = self.tool_data("get_superseded_usage", {"days": 30})
		self.assertNotIn("get_supplier", [r["tool"] for r in got["used"]])
