# SPDX-License-Identifier: MIT
"""The phone's company switcher. v0.268.0 (docs/contracts/company_switcher_v0_268.yaml; Tim, 2026-10-07).

Through the real /farmops transport, two companies on one site: the `X-FarmOps-Company` header selects the company for
every route that takes one; a company named in the body wins; a company the caller is not in — another farm's, or one
that does not exist — gets the same 403; routes that take no company ignore the header. Fixtures are regenerated
deliberately with FARM_CONTRACT_REGEN=1, then copied to the app.
"""

import json
import os
import pathlib

from erpnext_mcp import data_access_scan
from erpnext_mcp.api import guard

from .harness import STORE, set_roles
from .test_api_mobile import MAIN, OTHER, OUTSIDER_EMPLOYEE, WORKER, WORKER_EMPLOYEE
from .test_farmops_api import PREFIX, FarmOpsAPITestCase

HERE = pathlib.Path(__file__).parent / "contract" / "v0_268_0"
H = guard.COMPANY_HEADER
BOSS = "boss@example.test"


def fixture(test, name, value):
	path = HERE / name
	text = json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
	if os.environ.get("FARM_CONTRACT_REGEN"):
		HERE.mkdir(parents=True, exist_ok=True)
		path.write_text(text)
	test.assertEqual(path.read_text(), text, f"{name} drifted — FARM_CONTRACT_REGEN=1 rewrites it; then copy to the app")


class Switcher(FarmOpsAPITestCase):
	def setUp(self):
		super().setUp()
		self.boss = self.enrol(email=BOSS, name="Bo Ss", role="Foreman", entities=[MAIN, OTHER])
		set_roles(BOSS, ["Foreman"])
		set_roles(WORKER, ["Field Worker", "Foreman"])
		STORE.seed("Accident Report", [
			{"name": "ACC-MAIN-1", "company": MAIN, "status": "Open", "osha_recordable": "Yes",
			 "occurred_at": "2026-09-01 08:00:00", "days_away_from_work": 2},
			{"name": "ACC-OTHER-1", "company": OTHER, "status": "Open", "osha_recordable": "Yes",
			 "occurred_at": "2026-09-02 08:00:00", "days_away_from_work": 9},
		])
		STORE.seed("Leave Application", [
			{"name": "LV-MAIN-1", "employee": WORKER_EMPLOYEE, "company": MAIN, "status": "Open",
			 "from_date": "2026-09-01", "to_date": "2026-09-01", "total_leave_days": 1},
			{"name": "LV-OTHER-1", "employee": OUTSIDER_EMPLOYEE, "company": OTHER, "status": "Open",
			 "from_date": "2026-09-01", "to_date": "2026-09-05", "total_leave_days": 5},
		])
		STORE.commit()

	def test_the_header_selects_the_company_for_accidents_and_leave(self):
		for company, accident, leave, days in ((MAIN, "ACC-MAIN-1", "LV-MAIN-1", 1), (OTHER, "ACC-OTHER-1", "LV-OTHER-1", 5)):
			acc = self.message(f"{PREFIX}/mobile/list_accident_reports", {}, headers={H: company}, credential=self.boss)
			self.assertEqual((acc["company"], [r["name"] for r in acc["reports"]]), (company, [accident]))
			lv = self.message(f"{PREFIX}/mobile/list_leave_requests", {}, headers={H: company}, credential=self.boss)
			self.assertEqual(([r["name"] for r in lv["requests"]], lv["total_days"]), ([leave], days))

	def test_no_header_is_the_first_company_and_the_body_wins_over_the_header(self):
		self.assertEqual(self.message(f"{PREFIX}/mobile/list_accident_reports", {}, credential=self.boss)["company"], MAIN)
		body_wins = self.message(f"{PREFIX}/mobile/list_accident_reports", {"company": MAIN}, headers={H: OTHER},
		                         credential=self.boss)
		self.assertEqual(body_wins["company"], MAIN)

	def test_a_company_that_is_not_yours_is_refused_alike_and_routes_without_company_ignore_it(self):
		bodies = []
		for name in (OTHER, "No Such Farm LLC"):
			status, body = self.refusal(f"{PREFIX}/mobile/list_accident_reports", {}, headers={H: name})
			self.assertEqual(status, 403)
			self.assertEqual(body["exception"], "CompanyNotMember")
			self.assertEqual(body["error_key"], "error.mobile.company_not_member")
			bodies.append(json.loads(json.dumps(body).replace(name, "<company>")))
		self.assertEqual(bodies[0], bodies[1])
		fixture(self, "refusal_company_not_member.json", bodies[0])
		context = self.message(f"{PREFIX}/mobile/get_current_user_context", {}, headers={H: OTHER})
		self.assertEqual([c["name"] for c in context["companies"]], [MAIN])

	def test_a_single_company_user_needs_no_header_and_the_context_says_so(self):
		context = self.message(f"{PREFIX}/mobile/get_current_user_context", {})
		self.assertEqual(len(context["companies"]), 1)
		two = self.message(f"{PREFIX}/mobile/get_current_user_context", {}, credential=self.boss)
		self.assertEqual(sorted(c["name"] for c in two["companies"]), sorted([MAIN, OTHER]))
		self.assertEqual(self.message(f"{PREFIX}/mobile/list_accident_reports", {})["company"], MAIN)

	def test_role_gating_is_unchanged(self):
		set_roles(BOSS, ["Field Worker"])
		status, _body = self.refusal(f"{PREFIX}/mobile/list_accident_reports", {}, headers={H: MAIN}, credential=self.boss)
		self.assertEqual(status, 403)

	def test_the_company_scoped_routes_fixture(self):
		routes = sorted(m for m, fn in data_access_scan.handlers().items() if getattr(fn, "farm_ops_takes_company", False))
		self.assertIn("list_accident_reports", routes)
		self.assertIn("list_leave_requests", routes)
		self.assertNotIn("get_current_user_context", routes)
		fixture(self, "company_scoped_routes.json", routes)
