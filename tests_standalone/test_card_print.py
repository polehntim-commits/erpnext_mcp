# SPDX-License-Identifier: MIT
"""The card print queue (v0.208.0). docs/design/card_print_queue.md.

Records and methods, artwork at the card's size, routing as data, the stuck-job
sweep, the three doors, and the acceptance tests that do not need a printer.
"""

import base64
import sys
import types
import unittest

import frappe

from erpnext_mcp import card_art, card_print, card_print_form_action, tile_queries, tiles
from erpnext_mcp.api import card_print as desk_api
from erpnext_mcp.api import guard
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.render import qr

from . import harness
from .fixtures import MAIN, OTHER
from .harness import ROLES, STORE, set_roles
from .test_api_mobile import OUTSIDER_EMPLOYEE, WORKER, WORKER_EMPLOYEE, MobileAPITestCase

STATION_USER = "printer@farm.test"
FAKE_PDF = b"%PDF-1.4\n% a card\n%%EOF\n"
ON = {
	f"allow_{name}": 1
	for name in ("request_card_print", "cancel_card_print_job", "retry_card_print_job", "register_asset")
}
NEEDS_QR = unittest.skipUnless(qr.available(), "needs a QR encoder (segno)")


def uid(n: int) -> str:
	return f"00000000-0000-4000-8000-{n:012d}"


class CardPrintCase(MobileAPITestCase):
	"""A site with the queue installed, a requester (Ana), and a station user."""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		self.be("Administrator")
		card_print.seed()
		set_roles(WORKER, [*guard.roles_held(WORKER), card_print.REQUESTER_ROLE])
		set_roles(STATION_USER, [card_print.STATION_ROLE])
		self.rendered = []
		module = types.ModuleType("frappe.utils.pdf")

		def get_pdf(html, options=None):
			self.rendered.append((html, options))
			return FAKE_PDF

		module.get_pdf = get_pdf
		sys.modules["frappe.utils.pdf"] = module
		self.addCleanup(sys.modules.pop, "frappe.utils.pdf", None)
		STORE.commit()

	def ask(self, n=1, **extra):
		args = {
			"job_type": "Employee ID",
			"reference_name": WORKER_EMPLOYEE,
			"client_request_id": uid(n),
			**extra,
		}
		return card_print.request(WORKER, requested_from="iOS", **args)

	def ready(self):
		return card_print.claim(card_print.DEFAULT_STATION, STATION_USER, "Ready", "", "test")

	def printed(self, name):
		return card_print.complete(name, STATION_USER, True, cups_job="Primacy_2-1")


# ── requesting ──────────────────────────────────────────────────────────────
@NEEDS_QR
class Requesting(CardPrintCase):
	def test_a_request_is_queued_with_private_card_sized_artwork(self):
		answer = self.ask()
		job = answer["job"]
		self.assertTrue(answer["created"])
		self.assertEqual((job["status"], job["print_station"]), ("Queued", "primacy2-main"))
		self.assertEqual((job["requested_by"], job["requested_from"], job["company"]), (WORKER, "iOS", MAIN))
		self.assertNotIn("artwork", job)
		row = STORE.get_raw("Card Print Job", job["name"])
		self.assertTrue(job["name"].startswith("CPJ-"))
		self.assertTrue(row["artwork"].startswith("/private/files/"))
		html, options = self.rendered[-1]
		self.assertEqual((options["page-width"], options["page-height"]), ("85.6mm", "54.0mm"))
		self.assertIn("badge-card", html)
		self.assertEqual(html.count('<div class="badge-card">'), 1)

	def test_dual_adds_the_existing_back(self):
		self.ask(sides="Dual")
		html, _ = self.rendered[-1]
		self.assertEqual(html.count('<div class="badge-card">'), 2)
		self.assertIn("bc-back-qr", html)

	def test_the_same_request_id_twice_is_one_job(self):
		first = self.ask(7)
		again = self.ask(7)
		self.assertTrue(again["duplicate"])
		self.assertEqual(again["job"]["name"], first["job"]["name"])
		self.assertEqual(len(STORE.rows("Card Print Job")), 1)
		self.assertEqual(len(self.rendered), 1)

	def test_a_second_tap_does_not_queue_a_second_card(self):
		first = self.ask(1)
		second = self.ask(2)
		self.assertTrue(second["already_queued"])
		self.assertEqual(second["job"]["name"], first["job"]["name"])

	def test_a_reprint_needs_a_reason(self):
		job = self.ask(1)["job"]["name"]
		self.ready()
		self.printed(job)
		with self.assertRaises(card_print.CardPrintError) as caught:
			self.ask(2)
		self.assertIn(card_print.REPRINT_MARKER, str(caught.exception))
		again = self.ask(2, reprint_reason="Lost")
		self.assertEqual((again["job"]["is_reprint"], again["job"]["reprint_reason"]), (True, "Lost"))

	def test_without_the_role_the_request_is_refused(self):
		set_roles(WORKER, [r for r in frappe.get_roles(WORKER) if r != card_print.REQUESTER_ROLE])
		with self.assertRaises(card_print.CardPrintError) as caught:
			self.ask()
		self.assertEqual(caught.exception.kind, "forbidden")
		self.assertEqual(STORE.rows("Card Print Job"), [])

	def test_copies_sides_and_the_request_id_are_checked(self):
		for bad in ({"copies": 6}, {"copies": 0}, {"sides": "Triple"}, {"client_request_id": "x"}):
			with self.assertRaises(card_print.CardPrintError):
				self.ask(3, **bad)

	def test_no_renderer_means_no_job(self):
		sys.modules.pop("frappe.utils.pdf", None)
		with self.assertRaises(card_print.CardPrintError) as caught:
			self.ask()
		self.assertIn("Nothing was queued", str(caught.exception))
		self.assertEqual(STORE.rows("Card Print Job"), [])

	def test_the_daily_limit(self):
		STORE.seed(
			"Card Print Job",
			[
				{
					"name": f"CPJ-X-{i}",
					"requested_by": WORKER,
					"status": "Printed",
					"job_type": "Asset Tag",
					"reference_name": f"A{i}",
					"creation": frappe.utils.now(),
				}
				for i in range(card_print.PER_DAY)
			],
		)
		with self.assertRaises(card_print.CardPrintError) as caught:
			self.ask()
		self.assertIn("daily limit", str(caught.exception))


# ── the station ─────────────────────────────────────────────────────────────
@NEEDS_QR
class TheStation(CardPrintCase):
	def three(self):
		names = []
		for index, employee in enumerate((WORKER_EMPLOYEE, "EMP-B", "EMP-C")):
			if employee != WORKER_EMPLOYEE:
				STORE.seed(
					"Employee",
					[{"name": employee, "employee_name": employee, "company": MAIN, "status": "Active"}],
				)
			names.append(self.ask(10 + index, reference_name=employee)["job"]["name"])
		return names

	def test_jobs_are_claimed_oldest_first_one_at_a_time(self):
		names = self.three()
		first = self.ready()["job"]
		self.assertEqual(first["name"], names[0])
		self.assertEqual(base64.b64decode(first["artwork_base64"]), FAKE_PDF)
		self.assertEqual(
			self.ready()["job"]["name"], names[0], "an open job is handed back, not a second one"
		)
		self.printed(names[0])
		self.assertEqual(self.ready()["job"]["name"], names[1])
		row = STORE.get_raw("Card Print Job", names[1])
		self.assertEqual((row["status"], row["attempts"], row["claimed_by"]), ("Printing", 1, STATION_USER))

	def test_a_printer_that_is_not_ready_claims_nothing(self):
		name = self.ask()["job"]["name"]
		answer = card_print.claim(card_print.DEFAULT_STATION, STATION_USER, "Paused", "out of ribbon", "t")
		self.assertIsNone(answer["job"])
		self.assertEqual(STORE.get_raw("Card Print Job", name)["status"], "Queued")
		state = card_print.list_jobs(WORKER)["stations"][0]
		self.assertEqual((state["state"], state["message"]), ("Paused", "out of ribbon"))
		self.assertEqual(self.ready()["job"]["name"], name)

	def test_only_a_station_claims_or_completes(self):
		name = self.ask()["job"]["name"]
		with self.assertRaises(card_print.CardPrintError):
			card_print.claim(card_print.DEFAULT_STATION, WORKER, "Ready")
		self.ready()
		with self.assertRaises(card_print.CardPrintError):
			card_print.complete(name, WORKER, True)

	def test_a_retryable_failure_goes_back_to_the_queue_three_times(self):
		name = self.ask()["job"]["name"]
		for attempt in (1, 2):
			self.ready()
			answer = card_print.complete(name, STATION_USER, False, "ribbon ran out", retryable=True)
			self.assertEqual((answer["status"], answer["attempts"]), ("Queued", attempt))
		self.ready()
		answer = card_print.complete(name, STATION_USER, False, "ribbon ran out", retryable=True)
		self.assertEqual(answer["status"], "Failed")
		self.assertEqual(STORE.get_raw("Card Print Job", name)["error"], "ribbon ran out")

	def test_a_bad_file_fails_at_once_and_retry_requeues(self):
		name = self.ask()["job"]["name"]
		self.ready()
		card_print.complete(name, STATION_USER, False, "lp refused the file")
		self.assertEqual(STORE.get_raw("Card Print Job", name)["status"], "Failed")
		again = card_print.retry(name, WORKER)
		self.assertEqual((again["job"]["status"], again["job"]["attempts"]), ("Queued", 0))

	def test_the_sweep_returns_a_stuck_job(self):
		name = self.ask()["job"]["name"]
		self.ready()
		self.assertEqual(card_print.sweep_stuck(), 0)
		frappe.db.set_value("Card Print Job", name, "claimed_at", "2020-01-01 00:00:00")
		self.assertEqual(card_print.sweep_stuck(), 1)
		row = STORE.get_raw("Card Print Job", name)
		self.assertEqual((row["status"], row["error"]), ("Queued", "the print station did not report back"))
		frappe.db.set_value(
			"Card Print Job", name, {"status": "Printing", "attempts": 3, "claimed_at": "2020-01-01 00:00:00"}
		)
		card_print.sweep_stuck()
		self.assertEqual(STORE.get_raw("Card Print Job", name)["status"], "Failed")

	def test_complete_twice_is_a_success(self):
		name = self.ask()["job"]["name"]
		self.ready()
		self.printed(name)
		self.assertTrue(self.printed(name)["already"])

	def test_cancel_only_while_queued(self):
		name = self.ask()["job"]["name"]
		self.ready()
		with self.assertRaises(card_print.CardPrintError):
			card_print.cancel(name, WORKER)
		card_print.complete(name, STATION_USER, False, "x")
		card_print.retry(name, WORKER)
		self.assertEqual(card_print.cancel(name, WORKER)["job"]["status"], "Cancelled")
		self.assertIsNone(self.ready()["job"])

	def test_an_offline_station_is_reported(self):
		state = card_print.list_jobs(WORKER)["stations"][0]
		self.assertEqual(state["state"], "Offline")


# ── routing is data ─────────────────────────────────────────────────────────
class RoutingIsData(CardPrintCase):
	def test_a_second_station_takes_asset_tags_without_code(self):
		doc = frappe.new_doc("Card Print Station")
		doc.station_name = "labels-shop"
		doc.enabled = 1
		doc.priority = 1
		doc.job_types = "Asset Tag"
		doc.artwork_width_mm = 50
		doc.artwork_height_mm = 25
		doc.insert(ignore_permissions=True)
		self.assertEqual(card_print.station_for("Asset Tag", MAIN)["name"], "labels-shop")
		self.assertEqual(card_print.station_for("Employee ID", MAIN)["name"], "primacy2-main")
		frappe.db.set_value("Card Print Station", "primacy2-main", "enabled", 0)
		with self.assertRaises(card_print.CardPrintError) as caught:
			card_print.station_for("Employee ID", MAIN)
		self.assertIn("no print station", str(caught.exception))

	def test_a_station_can_be_limited_to_companies(self):
		frappe.db.set_value("Card Print Station", "primacy2-main", "companies", OTHER)
		with self.assertRaises(card_print.CardPrintError):
			card_print.station_for("Employee ID", MAIN)


# ── artwork ─────────────────────────────────────────────────────────────────
@unittest.skipUnless(card_art.reportlab_available() and qr.available(), "needs reportlab and segno")
class AssetTagArtwork(CardPrintCase):
	def setUp(self):
		super().setUp()
		self.tool_data(
			"register_asset", {"name": "MC-Valve-05", "asset_type": "Irrigation Valve", "company": MAIN}
		)
		STORE.commit()

	def test_one_card_sized_page_whose_qr_is_the_assets_own(self):
		answer = card_print.request(WORKER, "Asset Tag", "MC-Valve-05", uid(1))
		self.ready()
		payload = card_print._claim_payload(frappe.get_doc("Card Print Job", answer["job"]["name"]))
		pdf = base64.b64decode(payload["artwork_base64"])
		self.assertTrue(pdf.startswith(b"%PDF"))
		try:
			self.assertEqual(card_art.page_sizes_mm(pdf), [(85.6, 54.0)])
		except ImportError:
			pass
		row = card_print._asset("MC-Valve-05")[3]
		self.assertTrue(str(row.get("qr_url")).endswith("/scan/MC-Valve-05"))

	def test_an_asset_tag_is_one_side(self):
		with self.assertRaises(card_print.CardPrintError):
			card_print.request(WORKER, "Asset Tag", "MC-Valve-05", uid(2), sides="Dual")


# ── the three doors ─────────────────────────────────────────────────────────
@NEEDS_QR
class ThePhone(CardPrintCase):
	def a_badge(self):
		"""Issued by a manager first: a first badge is a hiring-role act."""
		row = card_print._employee(WORKER_EMPLOYEE)[3]
		card_print._employee_card(row, MAIN)
		STORE.commit()

	def test_a_first_badge_still_needs_a_hiring_role(self):
		self.be()
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.request_card_print(
				job_type="Employee ID", reference_name=WORKER_EMPLOYEE, client_request_id=uid(9)
			)
		self.assertIn("has no badge yet", str(caught.exception))
		self.assertEqual(STORE.rows("Card Print Job"), [])

	def test_request_list_cancel_retry(self):
		self.a_badge()
		self.be()
		answer = mobile_api.request_card_print(
			job_type="Employee ID", reference_name=WORKER_EMPLOYEE, client_request_id=uid(1)
		)
		name = answer["job"]["name"]
		listed = mobile_api.list_card_print_jobs()
		self.assertEqual([j["name"] for j in listed["jobs"]], [name])
		self.assertTrue(listed["can_request"])
		self.assertTrue(listed["jobs"][0]["can_cancel"])
		self.assertEqual(mobile_api.cancel_card_print_job(name=name)["job"]["status"], "Cancelled")
		with self.assertRaises(frappe.ValidationError):
			mobile_api.retry_card_print_job(name=name)

	def test_another_entitys_employee_is_not_found(self):
		self.be()
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.request_card_print(
				job_type="Employee ID", reference_name=OUTSIDER_EMPLOYEE, client_request_id=uid(2)
			)

	def test_the_reprint_refusal_carries_the_marker(self):
		job = self.ask(1)["job"]["name"]
		self.ready()
		self.printed(job)
		STORE.commit()
		self.be()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.request_card_print(
				job_type="Employee ID", reference_name=WORKER_EMPLOYEE, client_request_id=uid(3)
			)
		self.assertIn("reprint_reason_required", str(caught.exception))


@NEEDS_QR
class TheDeskAndTheAgent(CardPrintCase):
	def test_the_agent_claims_and_completes_through_the_whitelisted_methods(self):
		name = self.ask()["job"]["name"]
		STORE.commit()
		self.be(STATION_USER)
		claimed = desk_api.claim_next_card_print_job(print_station="primacy2-main", printer_state="Ready")
		self.assertEqual(claimed["job"]["name"], name)
		done = desk_api.complete_card_print_job(name=name, success=1, cups_job="Primacy_2-7")
		self.assertEqual(done["status"], "Printed")
		self.assertEqual(STORE.get_raw("Card Print Job", name)["cups_job"], "Primacy_2-7")

	def test_a_requester_cannot_claim(self):
		self.be()
		with self.assertRaises(frappe.PermissionError):
			desk_api.claim_next_card_print_job(print_station="primacy2-main", printer_state="Ready")

	def test_the_desk_cannot_edit_a_status(self):
		name = self.ask()["job"]["name"]
		doc = frappe.get_doc("Card Print Job", name)
		doc.status = "Printed"
		with self.assertRaises(Exception) as caught:
			doc.save(ignore_permissions=True)
		self.assertIn("through the queue", str(caught.exception))


@NEEDS_QR
class OverMCP(CardPrintCase):
	def test_request_list_and_stations(self):
		set_roles("Administrator", [*ROLES.get("Administrator", []), "System Manager"])
		data = self.tool_data(
			"request_card_print",
			{"job_type": "Employee ID", "reference_name": WORKER_EMPLOYEE, "client_request_id": uid(1)},
		)
		self.assertTrue(data["created"])
		self.assertEqual(self.tool_data("list_card_print_jobs", {})["count"], 1)
		stations = self.tool_data("list_card_print_stations", {})["stations"]
		self.assertEqual(stations[0]["job_types"], ["Employee ID", "Asset Tag"])
		self.tool_data("cancel_card_print_job", {"name": data["job"]["name"]})
		STORE.commit()
		self.assertIn(
			"only a Failed job", self.tool_error("retry_card_print_job", {"name": data["job"]["name"]})
		)


# ── tiles and Desk furniture ────────────────────────────────────────────────
class TheFurniture(CardPrintCase):
	def test_the_tile_allowlists(self):
		self.assertIn("print_queue", tiles.REPORTS)
		self.assertIn("printer", tiles.ICONS)
		self.assertEqual(len(tiles.ICONS), 49)
		self.assertIn("my_print_jobs", tile_queries.QUERIES)
		self.assertEqual(tiles.PRINT_QUEUE_TILE["audience"], {"roles": ["Card Print Requester"]})

	@NEEDS_QR
	def test_the_badge_counts_open_and_failed_jobs(self):
		name = self.ask()["job"]["name"]
		self.assertEqual(
			tile_queries.badge({"query": "my_print_jobs"}, WORKER), {"count": 1, "tone": "attention"}
		)
		self.ready()
		card_print.complete(name, STATION_USER, False, "x")
		self.assertEqual(tile_queries.badge({"query": "my_print_jobs"}, WORKER)["tone"], "critical")

	def test_the_buttons_are_seeded_once(self):
		harness.register_doctype(
			"Client Script", ["dt", "view", "enabled", "script"]
		) if not frappe.db.exists("DocType", "Client Script") else None
		first = card_print_form_action.seed_card_print_form_actions()
		created = [r for r in first if r["created"]]
		if created:
			second = card_print_form_action.seed_card_print_form_actions()
			self.assertEqual(
				[r["reason"] for r in second if r["name"] in [c["name"] for c in created]],
				["already present"] * len(created),
			)
		for doctype, _n, marker, job_type, label, group in card_print_form_action.TARGETS:
			text = card_print_form_action.source(doctype, marker, job_type, label, group)
			self.assertIn(card_print_form_action.REQUEST_METHOD, text)
			self.assertIn(f"{marker}@r1", text)
			self.assertNotIn("%(", text)
		self.assertTrue(callable(desk_api.request_card_print))
