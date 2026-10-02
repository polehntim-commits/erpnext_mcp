# SPDX-License-Identifier: MIT
"""Farm Ops over /farmops/api only. v0.216.0 — docs/design/farmops_only_funnel.md.

The app never called ERPNext directly; what tied it to the public /erpnext
mount was the base URL on its login card. These hold the server's half of
moving that base: which address a card carries, who has moved, what the probe
says, that every mutating route runs a gate, and the tag page.
"""

import frappe

from erpnext_mcp import funnel_readiness, settings
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.erpnext_mcp.doctype.asset_register import asset_register
from erpnext_mcp.farmops_api import app as sidecar_app
from erpnext_mcp.farmops_api import gates, routes
from erpnext_mcp.tools import funnel, universal_scan

from .harness import STORE
from .test_api_mobile import WORKER, MobileAPITestCase
from .test_farmops_api import FarmOpsAPITestCase
from .test_itgc import MAIN, ITGCTestCase

HOST = "https://umbrel.tail4a2b.ts.net"
LEGACY = f"{HOST}/erpnext"


class TheCardCarriesThePhonesAddress(MobileAPITestCase):
	def configure(self, **values):
		super().configure(**{"enabled": 1, "allow_generate_mobile_login_qr": 1, **values})

	def card_url(self, **arguments):
		return self.tool_data("generate_mobile_login_qr", {"user": WORKER, **arguments})["payload"]["url"]

	def test_with_nothing_new_configured_the_card_is_what_it_always_was(self):
		self.configure(public_url=LEGACY)
		self.assertEqual(self.card_url(), LEGACY)

	def test_the_farmops_url_wins_once_it_is_set(self):
		self.configure(public_url=LEGACY, farmops_public_url=HOST)
		data = self.tool_data("generate_mobile_login_qr", {"user": WORKER})
		self.assertEqual(data["payload"]["url"], HOST)
		self.assertEqual(data["payload"]["api_base"], "/farmops/api")
		# The shape an app built before this release reads is untouched.
		self.assertEqual(data["payload"]["v"], 1)

	def test_an_explicit_url_still_wins(self):
		self.configure(public_url=LEGACY, farmops_public_url=HOST)
		self.assertEqual(self.card_url(url="https://other.tail4a2b.ts.net"), "https://other.tail4a2b.ts.net")

	def test_legacy_off_refuses_a_base_with_a_path_and_writes_nothing(self):
		self.configure(public_url=LEGACY, legacy_erpnext_paths=0)
		STORE.commit()
		message = self.tool_error("generate_mobile_login_qr", {"user": WORKER})
		self.assertIn("Farm Ops Public URL", message)
		self.assertIn("Nothing was written", message)

	def test_legacy_off_is_fine_once_the_farmops_url_is_set(self):
		self.configure(public_url=LEGACY, farmops_public_url=HOST, legacy_erpnext_paths=0)
		self.assertEqual(self.card_url(), HOST)

	def test_a_site_that_never_saved_the_switch_reads_it_as_on(self):
		self.assertTrue(settings.allow_legacy_erpnext_paths())

	def test_the_mcp_endpoint_stays_on_public_url(self):
		from erpnext_mcp.tools import mobile as mobile_tools

		self.configure(public_url=LEGACY, farmops_public_url=HOST)
		self.assertEqual(mobile_tools._endpoint_url({}), LEGACY)
		self.assertEqual(mobile_tools._mobile_base_url({}), HOST)


class WhoHasMoved(MobileAPITestCase):
	def report(self, mode=None, version="0.27.0"):
		self.be()
		arguments = {"app_version": version, "schema_version": 2, "field_kinds": []}
		if mode is not None:
			arguments["api_base_mode"] = mode
		stored = mobile_api.report_device_capabilities(**arguments)
		self.be("Administrator")
		return stored

	def test_no_farmops_url_is_a_reason(self):
		status = funnel_readiness.status()
		self.assertFalse(status["ready_to_close_erpnext_funnel"])
		self.assertIn("Farm Ops Public URL is empty", status["reasons"][0])

	def test_a_phone_that_never_reported_blocks(self):
		self.configure(farmops_public_url=HOST)
		status = funnel_readiness.status()
		self.assertFalse(status["ready_to_close_erpnext_funnel"])
		self.assertEqual([row["ready"] for row in status["devices"]], [False])
		self.assertIn("has not reported using /farmops/api", status["reasons"][0])

	def test_an_old_app_reporting_nothing_does_not_erase_or_pass(self):
		self.configure(farmops_public_url=HOST)
		self.report(mode=None, version="0.26.0")
		self.assertFalse(funnel_readiness.status()["ready_to_close_erpnext_funnel"])

	def test_a_phone_on_the_legacy_base_blocks_and_says_why(self):
		self.configure(farmops_public_url=HOST)
		self.assertEqual(self.report("legacy")["api_base_mode"], "legacy")
		status = funnel_readiness.status()
		self.assertFalse(status["ready_to_close_erpnext_funnel"])
		self.assertIn("still calling through /erpnext", status["reasons"][0])

	def test_every_phone_on_farmops_is_ready(self):
		self.configure(farmops_public_url=HOST)
		self.report("farmops")
		status = funnel_readiness.status()
		self.assertTrue(status["ready_to_close_erpnext_funnel"], status)
		self.assertEqual(status["reasons"], [])
		self.assertEqual(status["devices"][0]["app_version"], "0.27.0")

	def test_a_mode_nobody_defined_is_not_stored(self):
		self.assertNotIn("api_base_mode", self.report("sideways"))

	def test_an_idle_phone_does_not_hold_it_up(self):
		self.configure(farmops_public_url=HOST)
		for row in frappe.db.get_all("Mobile Device Enrollment", fields=["name"]):
			frappe.db.set_value(
				"Mobile Device Enrollment", row["name"], "last_seen_on", "2020-01-01 00:00:00"
			)
			frappe.db.set_value("Mobile Device Enrollment", row["name"], "enrolled_at", "2020-01-01 00:00:00")
		status = funnel_readiness.status()
		self.assertTrue(status["ready_to_close_erpnext_funnel"])
		self.assertEqual(status["ignored_idle_devices"], 1)

	def test_get_server_status_carries_it(self):
		self.configure(enabled=1, farmops_public_url=HOST)
		block = self.tool_data("get_server_status", {})["erpnext_funnel"]
		self.assertEqual(block["farmops_public_url"], HOST)
		self.assertIn("ready_to_close_erpnext_funnel", block)


PROXY_404 = {"status": 404, "content_type": "text/plain; charset=utf-8", "body": "404 page not found"}
FRAPPE_404 = {"status": 404, "content_type": "text/html; charset=utf-8", "body": "<!DOCTYPE html>"}
HEALTH = {"status": 200, "content_type": "application/json", "body": '{"ok": true, "service": "farmops-api"}'}
PAGE = {"status": 200, "content_type": "text/html; charset=utf-8", "body": "<!doctype html>"}


class TheProbeSaysWhetherErpnextCanGo(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		self.configure(
			enabled=1, allow_validate_public_endpoint=1, public_url=LEGACY, farmops_public_url=HOST
		)
		self.asked = []
		self.erpnext = dict(FRAPPE_404)
		self.unpublished = set()
		originals = {
			name: getattr(funnel, name) for name in ("_tls_report", "_http_report", "_probe_route", "_get")
		}
		self.addCleanup(lambda: [setattr(funnel, name, value) for name, value in originals.items()])
		funnel._tls_report = lambda *a, **k: {"reachable": True, "verified": True}
		funnel._http_report = lambda *a, **k: {"status": 401, "authenticated": False}
		funnel._probe_route = self.fake_route
		funnel._get = self.fake_get

	def fake_route(self, endpoint, timeout):
		self.asked.append(endpoint)
		path = endpoint.split("/farmops/api", 1)[-1]
		return {"path": path, "published": path not in self.unpublished}

	def fake_get(self, endpoint, timeout):
		self.asked.append(endpoint)
		if "/erpnext/" in endpoint:
			return dict(self.erpnext)
		return dict(HEALTH if endpoint.endswith("/health") else PAGE)

	def probe(self, **arguments):
		return self.tool_data("validate_public_endpoint", {"probe_routes": True, **arguments})

	def test_a_public_url_with_a_path_is_the_operators_to_set(self):
		self.assertEqual(funnel._target_url({}), LEGACY)

	def test_a_path_in_an_argument_is_still_refused(self):
		STORE.commit()
		self.assertIn("carries a path", self.tool_error("validate_public_endpoint", {"url": LEGACY}))

	def test_routes_are_asked_for_where_a_phone_asks(self):
		data = self.probe()
		self.assertEqual(data["routes"]["base"], HOST)
		self.assertIn(f"{HOST}/farmops/api/mobile/scan_asset", self.asked)
		self.assertIn(f"{HOST}/farmops/api/scan/probe", self.asked)
		self.assertEqual(data["routes"]["checked"], len(routes.ROUTES))

	def test_only_fixed_erpnext_paths_are_fetched(self):
		self.probe()
		fetched = sorted(url for url in self.asked if "/erpnext/" in url)
		self.assertEqual(fetched, sorted(f"{HOST}{path}" for path in funnel.ERPNEXT_PROBES))

	def test_frappes_own_404_means_erpnext_is_still_public(self):
		data = self.probe()
		self.assertTrue(data["erpnext"]["erpnext_public"])
		self.assertFalse(data["farmops_only"]["erpnext_closed"])
		self.assertTrue(data["farmops_only"]["routes_ok"])
		self.assertIn("must stay until the phones move", data["farmops_only"]["verdict"])

	def test_the_proxys_404_means_it_is_closed(self):
		self.erpnext = dict(PROXY_404)
		data = self.probe()
		self.assertFalse(data["erpnext"]["erpnext_public"])
		self.assertTrue(data["farmops_only"]["erpnext_closed"])
		self.assertIn("cutover complete", data["summary"])

	def test_an_unanswered_probe_is_not_called_closed(self):
		self.erpnext = {"status": None, "error": "timed out"}
		data = self.probe()
		self.assertFalse(data["farmops_only"]["erpnext_closed"])
		self.assertEqual(len(data["erpnext"]["undetermined"]), 3)

	def test_an_unmounted_route_says_mount_first(self):
		self.unpublished = {"/mobile/scan_asset"}
		data = self.probe()
		self.assertFalse(data["farmops_only"]["routes_ok"])
		self.assertIn("before touching /erpnext", data["farmops_only"]["verdict"])

	def test_when_every_phone_has_moved_it_says_so(self):
		self.be()
		mobile_api.report_device_capabilities(app_version="0.27.0", schema_version=2, api_base_mode="farmops")
		self.be("Administrator")
		data = self.probe()
		self.assertTrue(data["farmops_only"]["ready_to_close_erpnext_funnel"])
		self.assertIn("/erpnext can be removed", data["farmops_only"]["verdict"])

	def test_without_a_farmops_url_the_legacy_base_is_named_as_the_problem(self):
		self.configure(enabled=1, allow_validate_public_endpoint=1, public_url=LEGACY, farmops_public_url="")
		data = self.probe()
		self.assertEqual(data["routes"]["base"], LEGACY)
		self.assertIn("itself under a path", data["farmops_only"]["verdict"])


class EveryMutatingRouteRunsAGate(MobileAPITestCase):
	"""Security review 2026-10-02, L4."""

	def test_no_mutating_route_is_ungated(self):
		self.assertEqual(
			gates.ungated_mutating(),
			[],
			"a mutating sidecar route runs no require_* check. Add the check to its wrapper, or — if it "
			"acts only on what the caller owns — add it to gates.CALLER_SCOPED with the reason.",
		)

	def test_the_exceptions_are_real_mutating_routes_that_need_to_be_there(self):
		by_path = {route.path: route for route in routes.ROUTES}
		for path, reason in gates.CALLER_SCOPED.items():
			self.assertIn(path, by_path, f"{path} is not a route any more")
			self.assertTrue(by_path[path].mutating, f"{path} does not write; it needs no exception")
			self.assertEqual(gates.gates_of(by_path[path]), [], f"{path} now runs a gate; drop the exception")
			self.assertGreater(len(reason), 40)

	def test_the_exceptions_are_exactly_the_two_upload_routes(self):
		self.assertEqual(
			sorted(gates.CALLER_SCOPED), ["/files/finalize_staged_file", "/files/stage_file_chunk"]
		)

	def test_a_gate_one_helper_down_is_found(self):
		by_path = {route.path: route for route in routes.ROUTES}
		self.assertIn("require_hr_role", gates.gates_of(by_path["/mobile/create_designation"]))
		self.assertIn("require_dispatch_role", gates.gates_of(by_path["/mobile/assign_farm_task"]))

	def test_list_sidecar_routes_shows_the_column(self):
		self.configure(enabled=1, allow_list_sidecar_routes=1)
		data = self.tool_data("list_sidecar_routes", {"contains": "stage_file_chunk"})
		row = data["routes"][0]
		self.assertEqual(row["gate"], [])
		self.assertIn("enrolled caller only", row["gate_note"])
		self.assertEqual(data["mutating_ungated"], [])
		moved = self.tool_data("list_sidecar_routes", {"contains": "/mobile/move_asset"})["routes"][0]
		self.assertTrue(moved["gate"])
		self.assertNotIn("gate_note", moved)


class TheTagPage(FarmOpsAPITestCase):
	def get(self, path):
		return self.post(path, credential=False, method="GET")

	def test_it_answers_anybody_and_shows_only_the_code(self):
		response = self.get("/farmops/api/scan/40-WM-SE")
		self.assertEqual(response.status_code, 200)
		self.assertIn("text/html", response.headers["Content-Type"])
		body = response.get_data(as_text=True)
		self.assertIn("40-WM-SE", body)
		self.assertIn("Farm Ops", body)
		self.assertNotIn("latitude", body.lower())

	def test_the_code_is_escaped(self):
		body = self.get("/farmops/api/scan/%3Cscript%3Ealert(1)%3C%2Fscript%3E").get_data(as_text=True)
		self.assertNotIn("<script>", body)
		self.assertIn("&lt;script&gt;", body)

	def test_it_is_the_same_page_for_a_tag_that_does_not_exist(self):
		"""No lookup: the page cannot be used to learn which assets are real."""
		self.assertEqual(self.get("/farmops/api/scan/NOPE-1").status_code, 200)

	def test_a_post_there_is_not_a_method(self):
		self.assertEqual(self.post("/farmops/api/scan/40-WM-SE", credential=False).status_code, 404)

	def test_it_is_described(self):
		self.assertIn(sidecar_app.SCAN_DESCRIBED_ROUTE, sidecar_app.DESCRIBED_ROUTES)


class TagsPointAtThePage(MobileAPITestCase):
	def test_the_old_shape_without_the_setting(self):
		self.configure(public_url=LEGACY)
		self.assertEqual(asset_register._build_qr_url("40-WM-SE"), f"{LEGACY}/scan/40-WM-SE")

	def test_the_new_shape_with_it(self):
		self.configure(public_url=LEGACY, farmops_public_url=HOST)
		self.assertEqual(
			asset_register._build_qr_url("MC Valve 05"), f"{HOST}/farmops/api/scan/MC%20Valve%2005"
		)

	def test_both_shapes_unwrap_to_the_same_docname(self):
		for url in (f"{LEGACY}/scan/MC%20Valve%2005", f"{HOST}/farmops/api/scan/MC%20Valve%2005"):
			self.assertEqual(universal_scan.scan_target(url), "MC Valve 05")


class TheReviewFixes(ITGCTestCase):
	def test_the_access_report_survives_an_orphaned_permission_row(self):
		STORE.seed("DocPerm", [{"name": "orphan-perm", "parent": None, "role": "System Manager", "read": 1}])
		data = self.tool_data("generate_access_control_report", {"company": MAIN})
		self.assertTrue(data["users"])

	def test_a_full_name_resolves_to_the_user(self):
		backup = self.a_backup()
		data = self.tool_data(
			"record_backup_test",
			{"backup_record": backup["name"], "test_restore_result": "Pass", "test_restore_by": "Mo Manager"},
		)
		self.assertEqual(data["test_restore_by"], "manager@example.test")

	def test_a_name_that_is_nobody_is_kept_in_the_notes(self):
		backup = self.a_backup()
		data = self.tool_data(
			"record_backup_test",
			{
				"backup_record": backup["name"],
				"test_restore_result": "Pass",
				"test_restore_by": "erp-backup@umbrellocal",
				"test_restore_notes": "partial-restore",
			},
		)
		self.assertEqual(data["test_restore_by"], "Administrator")
		notes = frappe.db.get_value("Backup Record", backup["name"], "test_restore_notes")
		self.assertIn("partial-restore", notes)
		self.assertIn("Tested by: erp-backup@umbrellocal", notes)

	def test_a_long_location_is_shortened_and_kept_whole_in_the_notes(self):
		long = "orchardmeadow-umbrel:/home/umbrel/umbrel/erp-backup-from-umbrellocal/daily/" + "x" * 120
		backup = self.a_backup(location=long, notes="set 2026-10-02_0230")
		self.assertLessEqual(len(backup["location"]), 140)
		notes = frappe.db.get_value("Backup Record", backup["name"], "notes")
		self.assertIn("set 2026-10-02_0230", notes)
		self.assertIn(f"Full location: {long}", notes)

	def test_a_short_location_is_untouched(self):
		backup = self.a_backup()
		self.assertEqual(backup["location"], "s3://orchard-backups/nightly")
		self.assertFalse(frappe.db.get_value("Backup Record", backup["name"], "notes"))
