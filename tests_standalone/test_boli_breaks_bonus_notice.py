# SPDX-License-Identifier: MIT
"""Rest and meal periods, and harvest-bonus notices, as BOLI states them. v0.302.0 (Tim, 2026-10-10).

BOLI's chart (OAR 839-020-0050): a paid 10-minute rest per four-hour segment or major part; a 30-minute meal from six
hours, a second at 14, a third at 22. Minors (OAR 839-021-0072): 15-minute rests at the same frequency. Harvest bonus
terms must be posted (OAR 839-020-0013).
"""

from erpnext_mcp import breaks, install, minors, pay_items

from .fixtures import V12TestCase
from .harness import STORE, frappe


def owed(hours, rows):
	band = breaks._best_band(hours, [dict(r) for r in rows])
	return int(band["periods_owed"]) if band else 0


class TheChart(V12TestCase):
	def test_rests(self):
		cases = {2.0: 0, 2.1: 1, 5.9: 1, 6.0: 1, 6.5: 2, 10.0: 2, 10.5: 3, 14.0: 3, 14.5: 4, 18.5: 5, 22.0: 5, 23.0: 6}
		self.assertEqual({h: owed(h, minors.OR_REST_SCHEDULE) for h in cases}, cases)

	def test_meals(self):
		cases = {5.9: 0, 6.0: 1, 13.9: 1, 14.0: 2, 22.0: 3}
		self.assertEqual({h: owed(h, minors.OR_MEAL_SCHEDULE) for h in cases}, cases)

	def test_minors_rest_fifteen_minutes_at_the_adult_frequency(self):
		self.assertEqual([r["minutes_each"] for r in minors.MINOR_REST_SCHEDULE], [15] * 6)
		self.assertEqual(owed(8.0, minors.MINOR_REST_SCHEDULE), 2, "not four — the adult frequency")
		self.assertEqual(owed(5.0, minors.MINOR_MEAL_SCHEDULE), 0)


class TheDraftPolicy(V12TestCase):
	def test_the_migrate_leaves_a_disabled_unapproved_oregon_policy(self):
		install._oregon_break_policy_draft()
		STORE.commit()
		name = frappe.db.get_value("Labor Break Policy", {"work_state": "OR"}, "name")
		doc = frappe.get_doc("Labor Break Policy", name)
		self.assertEqual(int(doc.enabled or 0), 0)
		self.assertFalse(doc.get("human_approved_by"))
		self.assertEqual(len(doc.get("rest_schedule")), 6)
		install._oregon_break_policy_draft()
		self.assertEqual(len(frappe.db.get_all("Labor Break Policy", filters={"work_state": "OR"})), 1)


class TheBonusNotice(V12TestCase):
	def test_unposted_terms_are_flagged(self):
		got = pay_items.unposted_bonus_terms({"E1": [{"name": "P1", "kind": "Production Bonus"},
		                                             {"name": "P2", "kind": "Bonus", "terms_posted_on": "2025-05-01"},
		                                             {"name": "P3", "kind": "Profit Share"}]})
		self.assertEqual([g["item"] for g in got], ["P1"])
		self.assertIn("839-020-0013", got[0]["reason"])
