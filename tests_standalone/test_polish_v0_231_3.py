"""v0.231.3 polish: per-company compliance inbox, and list reads without per-row queries."""

from unittest import mock

from erpnext_mcp import compliance_loop, phone_config

from .fixtures import MAIN, OTHER
from .harness import MCPTestCase, STORE


class TheInboxJudgesEachCompanyByItsOwnLoop(MCPTestCase):
	"""Two companies, one alert type: each company's alert is blocked (or not) by that
	company's loop — before, the first company's report was reused for both."""

	def test_each_company_gets_its_own_report(self):
		STORE.seed(
			"Compliance Alert",
			[
				{"name": "ALERT-A", "alert_type": "Detector Test Due", "company": MAIN, "dismissed": 0,
				 "subject_employee": "EMP-1", "due_date": "2026-10-05", "alert_message": "Main"},
				{"name": "ALERT-B", "alert_type": "Detector Test Due", "company": OTHER, "dismissed": 0,
				 "subject_employee": "EMP-1", "due_date": "2026-10-05", "alert_message": "Other"},
			],
		)
		person = {"companies": [MAIN, OTHER], "employee": "EMP-1", "roles": [], "skills": []}
		reports = {
			MAIN: {"gaps": [], "path": {"kind": "none", "ref": ""}},
			OTHER: {"gaps": ["nobody"], "path": {"kind": "none", "ref": ""}},
		}
		seen = []

		def fake_report(rule, company=""):
			seen.append(company)
			return reports[company]

		def fake_blocked(person, task, loop):
			return "no_one" if loop.get("gaps") else ""

		with mock.patch.object(phone_config, "person_of", return_value=person), \
			mock.patch.object(compliance_loop, "rule_of", return_value={"name": "R"}), \
			mock.patch.object(compliance_loop, "report", side_effect=fake_report), \
			mock.patch.object(compliance_loop, "_blocked", side_effect=fake_blocked), \
			mock.patch.object(compliance_loop, "_reason", return_value="Nobody here can do this."):
			out = compliance_loop.inbox("someone@farm.test")
		self.assertEqual(sorted(seen), sorted([MAIN, OTHER]), "one report per company")
		self.assertEqual([i["alert"] for i in out["blocked"]], ["ALERT-B"])
		self.assertNotIn("ALERT-A", [i["alert"] for i in out["blocked"]])


class TheAlertListIsReadInOneGo(MCPTestCase):
	"""v0.231.3. `shape.alerts` reads every linked task in one query and asks each alert
	type's recipe once; each row must equal the one-at-a-time `shape.alert`."""

	def test_the_list_equals_each_row_and_asks_each_type_once(self):
		from erpnext_mcp.api import rectify, shape
		from erpnext_mcp.tools import dispatch

		STORE.seed(
			"Farm Task",
			[
				{"name": "FT-OLD", "source_alert": "AL-1", "creation": "2026-10-01 08:00:00"},
				{"name": "FT-NEW", "source_alert": "AL-1", "creation": "2026-10-03 08:00:00"},
				{"name": "FT-2", "source_alert": "AL-2", "creation": "2026-10-02 08:00:00"},
			],
		)
		rows = [
			{"name": "AL-1", "alert_type": "Mystery Type", "company": MAIN, "severity": "Warning"},
			{"name": "AL-2", "alert_type": "Mystery Type", "company": MAIN, "severity": "Critical"},
			{"name": "AL-3", "alert_type": "Other Mystery", "company": MAIN},
		]
		one_by_one = [shape.alert(row) for row in rows]
		with mock.patch.object(dispatch, "has_task_recipe", wraps=dispatch.has_task_recipe) as recipe:
			batched = shape.alerts(rows)
		self.assertEqual(batched, one_by_one)
		self.assertEqual([a["linked_task"] for a in batched], ["FT-NEW", "FT-2", None])
		self.assertEqual(recipe.call_count, 2, "once per alert type, not once per alert")
		self.assertIsNone(rectify._RECIPES.get(), "the memo does not outlive the list")
