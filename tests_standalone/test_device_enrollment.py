# SPDX-License-Identifier: MIT
"""v0.175.0 — one phone, one credential, one row, one lookup path.

What these tests hold, in the order a phone meets it:

  * the office opens a window, and the QR carries a one-time token and NO
    credential (`TheWindow`, `TheQRIsSound`);
  * the phone spends the token at `/farmops/api/mobile/enroll_device` and gets
    its own pair exactly once (`TheExchange`);
  * that pair works, and only through the one verifier (`TheOneLookupPath`);
  * one device is revoked without touching the others, by hand or by the idle
    sweep (`OneDeviceAtATime`);
  * phones enrolled before this release keep working (`TheMigration`);
  * the Employee form's Onboard Worker button, and the three methods behind it
    (`TheEmployeeButton`).
"""

from __future__ import annotations

import base64
import json
import struct
import zlib

import frappe

from erpnext_mcp import audit, device_enrollment, onboard_worker_action
from erpnext_mcp.api import fallback_auth
from erpnext_mcp.farmops_api import PREFIX
from erpnext_mcp.farmops_api import app as farmops_app
from erpnext_mcp.patches import move_mobile_credentials_to_devices
from erpnext_mcp.render import qr
from erpnext_mcp.tools import mobile as mobile_tools

from .harness import STORE
from .test_api_mobile import WORKER, WORKER_EMPLOYEE
from .test_farmops_api import CONTEXT, FarmOpsAPITestCase
from .test_mobile import decode_png, shrink

ENROLL = f"{PREFIX}/mobile/enroll_device"


class DeviceTestCase(FarmOpsAPITestCase):
	"""The base class enrols WORKER through create_mobile_user — one direct-issue device."""

	def setUp(self):
		super().setUp()
		farmops_app.fallback_auth._FAILURES.clear()

	def open_window(self, user=WORKER, **kwargs):
		"""Open a window and COMMIT it, as the Desk request that opens one does.

		A refused exchange rolls its request back, and on a bench that cannot
		reach a window another request already committed. Without the commit
		here the double's rollback would take the window with it.
		"""
		frappe.local.session.user = "Administrator"
		opened = device_enrollment.open_enrollment(user, issued_by="Administrator", **kwargs)
		frappe.db.commit()
		return opened

	def exchange(self, token, **body):
		response = self.post(ENROLL, {"token": token, **body}, credential=False)
		return response.status_code, json.loads(response.get_data(as_text=True))

	def grant_json(self, user=WORKER) -> str:
		return json.dumps(STORE.get_raw("Mobile Access Grant", user), default=str)

	def row(self, device, user=WORKER) -> dict:
		for row in STORE.get_raw("Mobile Access Grant", user).get("devices") or []:
			if row.get("name") == device:
				return row
		raise AssertionError(f"no device row {device}")


# ── the window ──────────────────────────────────────────────────────────────
class TheWindow(DeviceTestCase):
	def test_a_window_is_a_pending_row_holding_the_tokens_hash_not_the_token(self):
		opened = self.open_window(device_name="Ana's iPhone")
		row = self.row(opened["device"])
		self.assertEqual(row["enrollment_status"], "Pending")
		self.assertEqual(row["enrollment_token"], device_enrollment.hash_token(opened["token"]))
		self.assertNotIn(opened["token"], self.grant_json())
		self.assertFalse(row.get("api_key"))

	def test_the_default_window_is_twenty_four_hours(self):
		opened = self.open_window()
		self.assertEqual(opened["hours"], 24)
		seconds = frappe.utils.time_diff_in_seconds(opened["expires_at"], frappe.utils.now())
		self.assertAlmostEqual(seconds, 24 * 3600, delta=120)

	def test_a_window_longer_than_a_week_is_refused(self):
		with self.assertRaises(device_enrollment.EnrollmentRefused):
			self.open_window(hours=169)
		with self.assertRaises(device_enrollment.EnrollmentRefused):
			self.open_window(hours=0)

	def test_a_new_window_closes_the_older_unscanned_one(self):
		first = self.open_window()
		second = self.open_window()
		self.assertEqual(second["superseded"], 1)
		self.assertEqual(self.row(first["device"])["enrollment_status"], "Revoked")
		status, _ = self.exchange(first["token"])
		self.assertEqual(status, 404)
		self.assertEqual(self.exchange(second["token"])[0], 200)

	def test_no_window_on_an_account_that_is_not_active(self):
		frappe.db.set_value("Mobile Access Grant", WORKER, "state", "Expired")
		with self.assertRaises(device_enrollment.EnrollmentRefused) as caught:
			self.open_window()
		self.assertIn("Expired", str(caught.exception))

	def test_the_payload_carries_the_url_and_the_token_and_nothing_else(self):
		opened = self.open_window()
		payload = device_enrollment.enroll_payload("https://farm.example.ts.net/", opened["token"])
		self.assertEqual(set(payload), {"type", "v", "url", "api_base", "token"})
		self.assertEqual(payload["type"], "farm_ops_enroll")
		self.assertEqual(payload["url"], "https://farm.example.ts.net")
		for credential_key in ("api_key", "api_secret", "user"):
			self.assertNotIn(credential_key, payload)


# ── the QR ──────────────────────────────────────────────────────────────────
class TheQRIsSound(DeviceTestCase):
	"""PHASE 1 OF THE BRIEF, ANSWERED WITH EVIDENCE RATHER THAN A SWAP.

	The report was "segno produces corrupt PNGs — valid headers, truncated zlib".
	segno never writes a PNG here: it supplies the module matrix and
	`render/qr.png_bytes` writes the file. These tests hold the file itself to
	the three properties that report says fail — every chunk's CRC, an IDAT that
	inflates to exactly height × (width + 1) bytes, and pixels that decode back
	to the payload — for whichever encoder this environment has.
	"""

	def _chunks(self, png: bytes):
		self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
		pos, out = 8, []
		while pos < len(png):
			(length,) = struct.unpack(">I", png[pos : pos + 4])
			kind = png[pos + 4 : pos + 8]
			data = png[pos + 8 : pos + 8 + length]
			(crc,) = struct.unpack(">I", png[pos + 8 + length : pos + 12 + length])
			self.assertEqual(zlib.crc32(kind + data) & 0xFFFFFFFF, crc, f"bad CRC on {kind!r}")
			out.append((kind, data))
			pos += 12 + length
		self.assertEqual(pos, len(png), "trailing bytes after IEND")
		self.assertEqual(out[-1][0], b"IEND")
		return out

	def test_every_chunk_checks_and_the_pixel_data_is_whole(self):
		if not qr.available():
			self.skipTest("no QR encoder")
		opened = self.open_window()
		for text in (
			json.dumps(device_enrollment.enroll_payload("https://farm.example.ts.net", opened["token"])),
			"x" * 900,
		):
			drawn = qr.render(text)
			chunks = self._chunks(drawn["png"])
			idat = b"".join(data for kind, data in chunks if kind == b"IDAT")
			width, height = struct.unpack(">II", dict(chunks)[b"IHDR"][:8])
			self.assertEqual(len(zlib.decompress(idat)), height * (width + 1))

	def test_the_pixels_are_the_enrolment_payload(self):
		if not qr.available():
			self.skipTest("no QR encoder")
		opened = self.open_window()
		text = json.dumps(
			device_enrollment.enroll_payload("https://farm.example.ts.net", opened["token"]),
			separators=(",", ":"),
			sort_keys=True,
		)
		drawn = qr.render(text)
		pixels = shrink(decode_png(drawn["png"]), drawn["scale"], drawn["border"])
		self.assertEqual(pixels, qr.qr_matrix(text))

	def test_pillow_reads_it_where_pillow_is_installed(self):
		try:
			import io

			from PIL import Image
		except ImportError:
			self.skipTest("Pillow not installed")
		if not qr.available():
			self.skipTest("no QR encoder")
		image = Image.open(io.BytesIO(qr.render("https://farm.example.ts.net")["png"]))
		image.load()
		self.assertEqual(image.mode, "L")


# ── the exchange ────────────────────────────────────────────────────────────
class TheExchange(DeviceTestCase):
	def test_a_token_buys_one_credential_that_works(self):
		opened = self.open_window()
		status, body = self.exchange(opened["token"], device_name="Ana's iPhone", device_identifier="ABC-123")
		self.assertEqual(status, 200, body)
		issued = body["message"]
		self.assertEqual(issued["user"], WORKER)
		self.assertEqual(issued["type"], "farm_ops_login", "the answer decodes as a login card")
		self.assertEqual(issued["token"], f"{issued['api_key']}:{issued['api_secret']}")
		self.assertEqual(self.message(CONTEXT, credential=issued)["user"], WORKER)
		row = self.row(opened["device"])
		self.assertEqual(row["enrollment_status"], "Enrolled")
		self.assertEqual(row["device_name"], "Ana's iPhone")
		self.assertEqual(row["device_identifier"], "ABC-123")
		self.assertTrue(row["enrolled_at"])

	def test_the_secret_is_readable_exactly_once(self):
		opened = self.open_window()
		issued = self.exchange(opened["token"])[1]["message"]
		row = self.row(opened["device"])
		self.assertTrue(set(str(row["api_secret"])) <= {"*"}, "the row holds Frappe's mask, not the secret")
		self.assertNotIn(issued["api_secret"], self.grant_json())
		self.assertNotIn(issued["api_secret"], json.dumps(device_enrollment.devices_of(WORKER), default=str))
		self.assertEqual(
			STORE.passwords[("Mobile Device Enrollment", opened["device"], "api_secret")],
			issued["api_secret"],
		)

	def test_the_token_is_spent_by_the_first_scan(self):
		opened = self.open_window()
		self.assertEqual(self.exchange(opened["token"])[0], 200)
		status, body = self.exchange(opened["token"])
		self.assertEqual(status, 404)
		self.assertIn("already been used", body["error"])
		self.assertFalse(self.row(opened["device"])["enrollment_token"])

	def test_an_expired_window_is_refused_and_is_never_a_401(self):
		opened = self.open_window()
		frappe.db.set_value(
			"Mobile Device Enrollment", opened["device"], "enrollment_expires_at", "2020-01-01 00:00:00"
		)
		frappe.db.commit()
		status, body = self.exchange(opened["token"])
		self.assertEqual(status, 410)
		self.assertIn("expired", body["error"])
		self.assertEqual(self.row(opened["device"])["enrollment_status"], "Pending")

	def test_an_unknown_token_names_nothing(self):
		status, _body = self.exchange("not-a-real-token")
		self.assertEqual(status, 404)
		self.assertEqual(self.exchange("")[0], 400)
		self.assertEqual(self.exchange("x" * 500)[0], 400)

	def test_a_grant_ended_after_the_window_opened_refuses_the_exchange(self):
		opened = self.open_window()
		frappe.db.set_value("Mobile Access Grant", WORKER, "state", "Expired")
		self.assertEqual(self.exchange(opened["token"])[0], 403)

	def test_a_disabled_login_refuses_the_exchange(self):
		opened = self.open_window()
		frappe.db.set_value("User", WORKER, "enabled", 0)
		self.assertEqual(self.exchange(opened["token"])[0], 403)

	def test_it_is_post_only_and_answers_json(self):
		response = self.post(ENROLL, credential=False, method="GET")
		self.assertEqual(response.status_code, 405)
		self.assertEqual(response.headers["Content-Type"], "application/json")

	def test_the_audit_row_never_holds_the_token(self):
		opened = self.open_window()
		self.exchange(opened["token"], device_name="Ana's iPhone")
		self.exchange("wrong-token")
		rows = [
			row for row in STORE.rows(audit.LOG_DOCTYPE) if "enroll_device" in str(row.get("tool_name") or "")
		]
		self.assertEqual(len(rows), 2)
		self.assertNotIn(opened["token"], json.dumps(rows, default=str))

	def test_a_stream_of_bad_tokens_is_cut_off_but_only_bad_ones_count(self):
		for _ in range(farmops_app.ENROLL_FAILURE_LIMIT):
			self.exchange("junk")
		self.assertEqual(self.exchange("junk")[0], 429)

	def test_the_request_form_dict_answers_attribute_access(self):
		"""The bench bug v0.175.0 found: Frappe reads `form_dict.cmd` by attribute
		in `get_user_permissions`, which a save reaches when it fills a child
		row's defaults. A plain dict raised there and the exchange answered 500."""
		from erpnext_mcp.farmops_api import session as farmops_session

		with farmops_session.request_session(request=None, body={"token": "x"}):
			self.assertEqual(frappe.local.form_dict.token, "x")
			self.assertIsNone(frappe.local.form_dict.cmd)

	def test_the_route_is_described_for_list_sidecar_routes(self):
		self.assertIn(farmops_app.ENROLL_DESCRIBED_ROUTE, farmops_app.DESCRIBED_ROUTES)
		self.assertEqual(farmops_app.ENROLL_DESCRIBED_ROUTE["path"], ENROLL)


# ── the one lookup path ─────────────────────────────────────────────────────
class TheOneLookupPath(DeviceTestCase):
	def test_the_worker_was_enrolled_on_a_device_row_not_the_user(self):
		self.assertFalse(STORE.get_raw("User", WORKER).get("api_key"))
		(device,) = device_enrollment.live_devices(WORKER)
		self.assertEqual(device["api_key"], self.credential["api_key"])

	def test_a_user_key_is_never_consulted(self):
		mobile_tools._issue_user_token(WORKER)
		user_key = STORE.get_raw("User", WORKER)["api_key"]
		secret = mobile_tools.read_user_api_secret(WORKER)
		self.assertEqual(fallback_auth.verify_credential(user_key, secret), "")
		self.assertEqual(
			self.refusal(CONTEXT, credential={"api_key": user_key, "api_secret": secret})[0], 401
		)

	def test_a_pending_row_is_not_a_credential(self):
		opened = self.open_window()
		self.assertEqual(device_enrollment.verify("", opened["token"]), "")
		self.assertEqual(
			device_enrollment.verify(device_enrollment.hash_token(opened["token"]), opened["token"]), ""
		)

	def test_the_status_alone_ends_a_credential(self):
		"""A row marked Revoked is refused even with its secret still stored.

		`_retire` clears the secret too, so every other revocation test would
		still pass with the status check deleted — this one isolates it.
		"""
		(device,) = device_enrollment.live_devices(WORKER)
		frappe.db.set_value("Mobile Device Enrollment", device["device"], "enrollment_status", "Revoked")
		self.assertTrue(device_enrollment.read_secret(device["device"]), "the secret is still there")
		self.assertEqual(
			fallback_auth.verify_credential(self.credential["api_key"], self.credential["api_secret"]), ""
		)

	def test_a_successful_call_stamps_the_devices_last_seen_once_an_hour(self):
		(device,) = device_enrollment.live_devices(WORKER)
		frappe.db.set_value("Mobile Device Enrollment", device["device"], "last_seen_on", None)
		self.message(CONTEXT)
		first = self.row(device["device"])["last_seen_on"]
		self.assertTrue(first)
		self.message(CONTEXT)
		self.assertEqual(self.row(device["device"])["last_seen_on"], first)


# ── one device at a time ────────────────────────────────────────────────────
class OneDeviceAtATime(DeviceTestCase):
	def second_phone(self):
		opened = self.open_window(device_name="Old phone")
		issued = self.exchange(opened["token"])[1]["message"]
		return opened["device"], issued

	def test_revoking_one_phone_leaves_the_other_working(self):
		device, old = self.second_phone()
		self.assertEqual(self.message(CONTEXT, credential=old)["user"], WORKER)
		frappe.local.session.user = "Administrator"
		result = device_enrollment.revoke(WORKER, device, "phone lost in the orchard")
		self.assertTrue(result["revoked"])
		self.assertEqual(self.refusal(CONTEXT, credential=old)[0], 401)
		self.assertEqual(self.message(CONTEXT)["user"], WORKER, "the other phone still works")
		row = self.row(device)
		self.assertEqual(row["revocation_reason"], "phone lost in the orchard")
		self.assertEqual(row["revoked_by"], "Administrator")
		self.assertNotIn(("Mobile Device Enrollment", device, "api_secret"), STORE.passwords)

	def test_a_revocation_needs_a_reason(self):
		device, _ = self.second_phone()
		with self.assertRaises(device_enrollment.EnrollmentRefused):
			device_enrollment.revoke(WORKER, device, "")

	def test_the_idle_sweep_takes_the_lost_phone_and_not_the_new_one(self):
		device, _old = self.second_phone()
		frappe.db.set_value("Mobile Device Enrollment", device, "last_seen_on", "2020-01-01 00:00:00")
		self.message(CONTEXT)  # the worker's current phone, in daily use
		self.configure(mobile_grant_idle_days=30)
		self.assertEqual(mobile_tools.sweep_idle_grants(), 1)
		self.assertEqual(self.row(device)["enrollment_status"], "Revoked")
		self.assertIn("idle sweep", self.row(device)["revocation_reason"])
		self.assertEqual(frappe.db.get_value("Mobile Access Grant", WORKER, "state"), "Active")
		self.assertEqual(self.message(CONTEXT)["user"], WORKER)

	def test_revoking_the_account_revokes_every_device(self):
		_device, old = self.second_phone()
		frappe.local.session.user = "Administrator"
		self.tool_data("revoke_mobile_user", {"email": WORKER, "reason": "left after harvest"})
		self.assertEqual(device_enrollment.live_devices(WORKER), [])
		self.assertEqual(self.refusal(CONTEXT, credential=old)[0], 401)
		self.assertEqual(self.refusal(CONTEXT)[0], 401)

	def test_the_mcp_tools(self):
		device, _ = self.second_phone()
		frappe.local.session.user = "Administrator"
		self.configure(
			allow_list_mobile_devices=1, allow_revoke_mobile_device=1, allow_open_device_enrollment=1
		)
		listed = self.tool_data("list_mobile_devices", {"user": WORKER})
		self.assertEqual(listed["enrolled"], 2)
		self.assertNotIn('api_secret": "', json.dumps(listed, default=str).replace('"api_secret": null', ""))
		revoked = self.tool_data(
			"revoke_mobile_device", {"user": WORKER, "device": device, "reason": "replaced"}
		)
		self.assertTrue(revoked["revoked"])
		self.assertEqual(revoked["still_live"], 1)
		opened = self.tool_data("open_device_enrollment", {"user": WORKER})
		self.assertEqual(opened["payload"]["type"], "farm_ops_enroll")
		self.assertNotIn("api_secret", opened["payload"])
		self.assertTrue(base64.b64decode(opened["png_base64"]).startswith(b"\x89PNG"))

	def test_the_writes_ship_off(self):
		frappe.local.session.user = "Administrator"
		self.configure(allow_revoke_mobile_device=0, allow_open_device_enrollment=0)
		for tool, arguments in (
			("open_device_enrollment", {"user": WORKER}),
			("revoke_mobile_device", {"user": WORKER, "device": "x", "reason": "because"}),
		):
			with self.subTest(tool=tool):
				self.assertIn(f"allow_{tool}", self.tool_error(tool, arguments))


# ── phones enrolled before v0.175.0 ─────────────────────────────────────────
class TheMigration(DeviceTestCase):
	def legacy(self, user=WORKER):
		"""What a site looked like before the upgrade: the pair on the User, no rows."""
		grant = STORE.get_raw("Mobile Access Grant", user)
		grant["devices"] = []
		for key in list(STORE.passwords):
			if key[0] == "Mobile Device Enrollment":
				STORE.passwords.pop(key)
		frappe.db.set_value("User", user, "api_key", "legacykey01")
		STORE.passwords[("User", user, "api_secret")] = "legacysecret01"
		return {"api_key": "legacykey01", "api_secret": "legacysecret01"}

	def test_a_phone_enrolled_before_the_upgrade_keeps_working(self):
		old = self.legacy()
		self.assertEqual(self.refusal(CONTEXT, credential=old)[0], 401, "the negative control")
		move_mobile_credentials_to_devices.execute()
		self.assertEqual(self.message(CONTEXT, credential=old)["user"], WORKER)
		(device,) = device_enrollment.live_devices(WORKER)
		self.assertEqual(device["device_name"], device_enrollment.MIGRATED_DEVICE_NAME)

	def test_it_is_idempotent(self):
		self.legacy()
		move_mobile_credentials_to_devices.execute()
		move_mobile_credentials_to_devices.execute()
		self.assertEqual(len(device_enrollment.devices_of(WORKER)), 1)

	def test_the_user_pair_is_left_and_revoking_the_migrated_device_ends_it(self):
		self.legacy()
		move_mobile_credentials_to_devices.execute()
		self.assertEqual(STORE.get_raw("User", WORKER)["api_key"], "legacykey01")
		(device,) = device_enrollment.live_devices(WORKER)
		frappe.local.session.user = "Administrator"
		device_enrollment.revoke(WORKER, device["device"], "phone replaced")
		self.assertFalse(STORE.get_raw("User", WORKER).get("api_key"))
		self.assertEqual(mobile_tools.read_user_api_secret(WORKER), "")

	def test_a_revoked_grant_is_not_brought_forward(self):
		self.legacy()
		frappe.db.set_value("Mobile Access Grant", WORKER, {"state": "Revoked", "revocation_reason": "left"})
		move_mobile_credentials_to_devices.execute()
		self.assertEqual(device_enrollment.devices_of(WORKER), [])


# ── the Employee form ───────────────────────────────────────────────────────
class TheEmployeeButton(DeviceTestCase):
	def setUp(self):
		super().setUp()
		frappe.local.session.user = "Administrator"

	def test_the_script_is_seeded_once_and_names_its_three_methods(self):
		first = onboard_worker_action.seed_onboard_worker_action()
		self.assertTrue(first["created"], first)
		self.assertEqual(onboard_worker_action.seed_onboard_worker_action()["reason"], "already present")
		source = onboard_worker_action.SCRIPT_SOURCE
		for method in (
			onboard_worker_action.CONTEXT_METHOD,
			onboard_worker_action.START_METHOD,
			onboard_worker_action.STATUS_METHOD,
		):
			self.assertIn(method, source)
			module, _, name = method.rpartition(".")
			self.assertEqual(module, "erpnext_mcp.onboard_worker_action")
			self.assertTrue(callable(getattr(onboard_worker_action, name)))
		self.assertIn('__("Actions")', source)
		self.assertIn(onboard_worker_action.SCRIPT_STAMP, source)

	def test_the_script_never_saves_the_qr(self):
		"""DOM-only: no upload, no attachment call, and the image is emptied on close."""
		source = onboard_worker_action.SCRIPT_SOURCE
		for forbidden in ("upload_file", "attach", "localStorage", "sessionStorage", "download"):
			self.assertNotIn(forbidden, source)
		self.assertIn("dialog.onhide", source)
		self.assertIn("body.empty()", source)

	def test_the_script_is_removed_on_uninstall(self):
		onboard_worker_action.seed_onboard_worker_action()
		self.assertTrue(onboard_worker_action.remove_onboard_worker_action()["removed"])
		self.assertEqual(onboard_worker_action._existing(), "")

	def test_an_enrolled_worker_gets_a_qr_and_no_file(self):
		files_before = len(STORE.rows("File"))
		context = onboard_worker_action.employee_enrollment_context(employee=WORKER_EMPLOYEE)
		self.assertTrue(context["has_account"])
		self.assertTrue(context["can_enrol"], context)
		answer = onboard_worker_action.start_employee_enrollment(
			employee=WORKER_EMPLOYEE, device_name="Ana's iPhone"
		)
		self.assertTrue(base64.b64decode(answer["png_base64"]).startswith(b"\x89PNG"))
		self.assertGreater(answer["seconds_remaining"], 23 * 3600)
		self.assertNotIn("token", answer)
		self.assertNotIn("api_secret", answer)
		self.assertEqual(len(STORE.rows("File")), files_before, "the QR must never become a file")
		status = onboard_worker_action.employee_enrollment_status(
			employee=WORKER_EMPLOYEE, device=answer["device"]
		)
		self.assertEqual(status["status"], "Pending")

	def test_the_poll_sees_the_phone_arrive(self):
		answer = onboard_worker_action.start_employee_enrollment(employee=WORKER_EMPLOYEE)
		pixels = decode_png(base64.b64decode(answer["png_base64"]))
		self.assertTrue(pixels)
		# The token is only in the image; read it back off the row's hash by
		# opening a fresh window the same way and exchanging THAT, which is what
		# a phone scanning the dialog does.
		opened = self.open_window(device_name="Ana's iPhone")
		self.exchange(opened["token"])
		status = onboard_worker_action.employee_enrollment_status(
			employee=WORKER_EMPLOYEE, device=opened["device"]
		)
		self.assertEqual(status["status"], "Enrolled")
		self.assertEqual(status["device_name"], "Ana's iPhone")

	def test_an_employee_with_no_account_gets_one_and_no_credential(self):
		STORE.seed(
			"Employee",
			[
				{
					"name": "HR-EMP-NEW",
					"employee_name": "Rosa Diaz",
					"company": "Main Farm LLC",
					"status": "Active",
				}
			],
		)
		company = STORE.get_raw("Employee", WORKER_EMPLOYEE)["company"]
		frappe.db.set_value("Employee", "HR-EMP-NEW", "company", company)
		context = onboard_worker_action.employee_enrollment_context(employee="HR-EMP-NEW")
		self.assertFalse(context["has_account"])
		answer = onboard_worker_action.start_employee_enrollment(
			employee="HR-EMP-NEW", email="Rosa@Example.test", role="Field Worker", company=company
		)
		self.assertEqual(answer["user"], "rosa@example.test")
		self.assertEqual(frappe.db.get_value("Employee", "HR-EMP-NEW", "user_id"), "rosa@example.test")
		self.assertEqual(
			device_enrollment.live_devices("rosa@example.test"), [], "no credential until a phone scans"
		)
		(pending,) = device_enrollment.devices_of("rosa@example.test")
		self.assertEqual(pending["status"], "Pending")
		self.assertFalse(STORE.get_raw("User", "rosa@example.test").get("api_key"))
