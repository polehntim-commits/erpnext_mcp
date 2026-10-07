# SPDX-License-Identifier: MIT
"""Who sees what on the phone, as data. v0.267.0 (docs/contracts/data_access_v0_267.yaml).

The seed is today's behaviour; the tests hold the policy to the code (every route listed, each route's gates the ones
its code calls), generate the leak matrix from the policy itself (every route × tier × restricted field), and pin the
two safety rules: a gate only narrows (never private HR back to Farm Manager), and only a System Manager in the Desk
publishes it.
"""

import json
import os

import frappe

from erpnext_mcp import config_lifecycle, data_access, data_access_scan, phone_config, tiles
from erpnext_mcp.api import guard

from .harness import ROLES, STORE
from .test_api_mobile import MAIN, WORKER, MobileAPITestCase

TIER_ROLE = {"worker": "Field Worker", "crew_lead": "Crew Leader", "foreman": "Foreman", "manager": "Farm Manager",
             "hr": "HR Manager", "accounts": "Accounts Manager"}


class AccessCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		data_access.forget()
		self.addCleanup(data_access.forget)

	def publish(self, body, user="Administrator", roles=("System Manager",)):
		saved = list(ROLES.get(user, []))
		self.addCleanup(lambda: ROLES.__setitem__(user, saved))
		ROLES[user] = list(roles)
		frappe.local.session.user = user
		doc, _report = phone_config.save_draft(data_access.KIND, data_access.KEY, body, "test", "Operator")
		with config_lifecycle.desk_action():
			phone_config.publish(data_access.KIND, data_access.KEY, doc.version, "test", user)
		STORE.commit()
		return doc


class TheSeedIsTheCode(AccessCase):
	def test_regenerate_or_compare(self):
		uses = {m: r["resources"] for m, r in data_access.seed()["routes"].items() if r.get("resources")}
		fresh = data_access_scan.build_seed(data_access.seed()["resources"], uses)
		if os.environ.get("FARM_DATA_ACCESS_REGEN"):
			data_access.SEED_FILE.write_text(json.dumps(fresh, indent=1, ensure_ascii=False) + "\n")
		self.assertEqual(data_access.seed(), fresh,
		                 "the seed policy drifted from the code — FARM_DATA_ACCESS_REGEN=1 rewrites it; review the diff")

	def test_every_phone_route_is_listed_with_the_gates_its_code_calls(self):
		listed = data_access.seed()["routes"]
		scanned = data_access_scan.routes()
		self.assertEqual(sorted(listed), sorted(scanned))
		for method, gates in scanned.items():
			self.assertEqual(listed[method].get("gates", []), gates, method)

	def test_the_seed_validates_and_is_the_policy_until_one_is_published(self):
		self.assertEqual(data_access.validate(data_access.seed(), key="farm")["errors"], [])
		self.assertEqual(data_access.policy(), data_access.seed())
		self.assertLess(len(json.dumps(data_access.seed()).encode()), phone_config.MAX_BODY_BYTES)

	def test_the_gates_hold_todays_roles(self):
		self.assertEqual(data_access.gate_roles("private_hr", frozenset()), guard.PRIVATE_HR_ROLES)
		self.assertEqual(data_access.gate_roles("compliance", frozenset()), guard.COMPLIANCE_ROLES)
		self.assertEqual(data_access.gate_roles("dispatch", frozenset()), frozenset(guard.DISPATCH_ROLES))


class GatesOnlyNarrow(AccessCase):
	def test_private_hr_cannot_be_widened_to_farm_manager(self):
		body = data_access.seed()
		body["gates"]["private_hr"]["roles"].append("Farm Manager")
		errors = data_access.validate(body, key="farm")["errors"]
		self.assertTrue(any("private_hr" in e and "Farm Manager" in e for e in errors), errors)

	def test_even_a_stored_wider_body_is_capped_at_the_ceiling(self):
		frappe.local.farm_data_access = {"gates": {"private_hr": {"roles": ["Farm Manager", "HR User"]}}}
		self.assertEqual(data_access.gate_roles("private_hr", guard.PRIVATE_HR_ROLES), frozenset({"HR User"}))

	def test_a_published_narrowing_closes_the_gate(self):
		body = data_access.seed()
		body["gates"]["compliance"]["roles"] = ["Compliance Officer", "Farm Manager", "System Manager"]
		self.publish(body)
		ROLES["foreman@example.test"] = ["Foreman"]
		with self.assertRaises(frappe.PermissionError) as caught:
			guard.require_compliance_role("foreman@example.test", "list_compliance_alerts")
		self.assertNotIn("Foreman", str(caught.exception))


class OnlyASystemManagerInTheDesk(AccessCase):
	def test_not_over_mcp(self):
		frappe.local.session.user = "Administrator"
		doc, _report = phone_config.save_draft(data_access.KIND, data_access.KEY, data_access.seed(), "x", "Operator")
		with self.assertRaises(phone_config.ConfigError):
			phone_config.publish(data_access.KIND, data_access.KEY, doc.version, "x", "Administrator")

	def test_not_a_farm_manager_in_the_desk(self):
		with self.assertRaises(phone_config.ConfigError):
			self.publish(data_access.seed(), roles=("Farm Manager",))

	def test_a_system_manager_in_the_desk(self):
		body = data_access.seed()
		body["gates"]["location"]["roles"] = ["Farm Manager", "Foreman"]
		self.publish(body)
		self.assertEqual(data_access.gate_roles("location", guard.LOCATION_ROLES), frozenset({"Farm Manager", "Foreman"}))


class TheLeakMatrix(AccessCase):
	"""Generated from the policy: for every route that carries a resource, every tier, every restricted field."""

	def _synthetic(self, parts, row):
		value = [dict(row), dict(row)]
		for part in reversed(parts):
			value = {part: value}
		return value if parts else dict(row)

	def test_every_route_tier_and_field(self):
		pol = data_access.seed()
		checked = 0
		for method, route in pol["routes"].items():
			for use in route.get("resources") or []:
				res = pol["resources"][use["resource"]]
				fields = res.get("fields") or {}
				if not fields:
					continue
				row = {f: f"secret-{f}" for f in fields}
				row["harmless"] = "ok"
				if res.get("owner_key"):
					row[res["owner_key"]] = "EMP-SOMEONE-ELSE"
				if res.get("department_key"):
					row[res["department_key"]] = "Somewhere Else"
				parts = [p for p in str(use.get("at") or "").split(".") if p]
				for tier, role in TIER_ROLE.items():
					scope = (res.get("scope") or {}).get(tier, "all")
					out = data_access.apply(method, "nobody@example.test", self._synthetic(parts, row), roles=[role])
					seen = out
					for part in parts:
						seen = (seen or {}).get(part) if isinstance(seen, dict) else seen
					rows = seen if isinstance(seen, list) else ([seen] if seen else [])
					if scope in ("own", "department", "none"):
						self.assertEqual(rows, [], f"{method} {tier}: a row outside {scope} reached the phone")
						continue
					for got in rows:
						self.assertEqual(got["harmless"], "ok")
						for field, allowed in fields.items():
							self.assertEqual(got.get(field) is not None, tier in allowed, f"{method} {tier} {field}")
					checked += 1
		self.assertGreater(checked, 0)

	def test_your_own_row_is_whole(self):
		pol = data_access.seed()
		for name, res in pol["resources"].items():
			if not res.get("owner_key") or not res.get("fields"):
				continue
			method = next((m for m, r in pol["routes"].items()
			               if any(u["resource"] == name for u in r.get("resources") or [])), None)
			if not method:
				continue
			use = next(u for u in pol["routes"][method]["resources"] if u["resource"] == name)
			parts = [p for p in str(use.get("at") or "").split(".") if p]
			row = {f: "x" for f in res["fields"]}
			row[res["owner_key"]] = "EMP-ANA"
			STORE.seed("Employee", [{"name": "EMP-ANA", "user_id": "ana@example.test", "company": MAIN, "status": "Active"}])
			out = data_access.apply(method, "ana@example.test", self._synthetic(parts, row), roles=["Field Worker"])
			for part in parts:
				out = out[part]
			for got in out if isinstance(out, list) else [out]:
				for field in res["fields"]:
					self.assertIn(field, got, f"{name}: own row lost {field}")

	def test_a_system_manager_is_never_trimmed_and_unlisted_routes_pass(self):
		answer = {"anything": 1}
		self.assertIs(data_access.apply("no_such_route", "x", answer, roles=["Field Worker"]), answer)


class TileAudienceByTier(AccessCase):
	def test_a_tier_audience_matches_through_the_roles(self):
		person = {"user": WORKER, "roles": ["Foreman"], "companies": [MAIN], "skills": []}
		self.assertTrue(phone_config.matches(person, {"tiers": ["foreman", "manager"]}))
		self.assertFalse(phone_config.matches(person, {"tiers": ["manager"]}))
		self.assertTrue(any("not one of" in e for e in phone_config.audience_problems({"tiers": ["boss"]})))
		self.assertIn("market_card", tiles.SEEDS)


class ThroughTheEndpoint(AccessCase):
	"""A published narrowing trims a real answer at guard.endpoint's exit — the route's code is unchanged."""

	def test_supplier_tax_id_narrowed_to_accounts(self):
		body = data_access.seed()
		body["resources"]["supplier_tax"]["fields"]["tax_id"] = ["accounts", "manager"]
		self.publish(body)

		@guard.endpoint("list_suppliers")
		def fake(user):
			return {"suppliers": [{"name": "Sheppard's", "tax_id": "12-3456789"}]}

		self.be(WORKER)
		answer = fake()
		self.assertEqual(answer["suppliers"], [{"name": "Sheppard's"}])
