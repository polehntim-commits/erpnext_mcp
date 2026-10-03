# SPDX-License-Identifier: MIT
"""Approve a new phone from a manager's phone. v0.219.0 — device_client_enrollment.md §6.2."""

import json

import frappe

from erpnext_mcp import device_keys

from .harness import STORE, set_roles
from .test_api_mobile import WORKER
from .test_device_keys import DeviceKeyCase, Phone

BOSS = "boss@example.test"


class ApprovalCase(DeviceKeyCase):
	def setUp(self):
		super().setUp()
		self.configure_keys(1, phone_approval_enabled=1)
		# The manager: an HR Manager whose own phone is key-bound.
		from .test_api_mobile import ON  # noqa: F401

		self.enrol(email=BOSS, name="Bo Boss", role="Farm Manager")
		set_roles(BOSS, ["Farm Manager", "HR Manager"])
		self.boss = self.enrolled_phone_for(BOSS)

	def enrolled_phone_for(self, user):
		phone = Phone()
		nonce, _ = self.link(user)
		answer = self.payload(self.pickup(phone, nonce))["message"]
		phone.token, phone.device = answer["access_token"], answer["device"]
		return phone

	def ask(self, phone, **override):
		body = phone.keys_body(device_name="Ana's new phone")
		body["unlock_signature"] = phone.unlock.sign(f"farmops-request|{phone.jkt}".encode())
		body.update(override)
		return self.signed(phone, "/farmops/api/access/request", body)

	def collect(self, phone, request):
		return self.signed(phone, "/farmops/api/access/status", {"request": request})

	def decide(self, approver_phone, code, for_user, decision="approve", sign=True, request=None):
		path = f"/farmops/api/mobile/{'approve' if decision == 'approve' else 'deny'}_access_request"
		body = {"code": code}
		if decision == "approve":
			body["for_user"] = for_user
		if sign:
			message = (
				f"farmops-approve|{request}|{for_user if decision == 'approve' else ''}|{decision}".encode()
			)
			body["signature"] = approver_phone.unlock.sign(message)
		return self.signed(approver_phone, path, body, token=approver_phone.token)


class TheWholeFlow(ApprovalCase):
	def test_ask_approve_collect_and_sign_in(self):
		new = Phone()
		asked = self.payload(self.ask(new))["message"]
		self.assertRegex(asked["code"], r"^[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}$")
		self.assertNotIn(
			asked["code"].replace("-", ""), json.dumps(STORE.rows("Farm Access Request"), default=str)
		)
		self.assertEqual(self.payload(self.collect(new, asked["request"]))["message"]["status"], "pending")
		decided = self.decide(self.boss, asked["code"], WORKER, request=asked["request"])
		self.assertEqual(decided.status_code, 200, decided.get_data(as_text=True))
		got = self.payload(self.collect(new, asked["request"]))["message"]
		self.assertEqual((got["status"], got["user"]), ("approved", WORKER))
		new.token = got["access_token"]
		self.assertEqual(
			self.signed(new, "/farmops/api/mobile/get_current_user_context", token=new.token).status_code, 200
		)
		row = frappe.db.get_value(
			"Mobile Device Enrollment", got["device"], ["approval_method", "approved_by"], as_dict=True
		)
		self.assertEqual((row["approval_method"], row["approved_by"]), ("phone_approval", BOSS))
		# Collected once.
		self.assertEqual(self.collect(new, asked["request"]).status_code, 404)

	def test_only_the_asking_phone_can_collect(self):
		new = Phone()
		asked = self.payload(self.ask(new))["message"]
		self.decide(self.boss, asked["code"], WORKER, request=asked["request"])
		self.assertEqual(self.collect(Phone(), asked["request"]).status_code, 404)

	def test_a_denied_request_is_a_404_to_the_phone(self):
		new = Phone()
		asked = self.payload(self.ask(new))["message"]
		self.assertEqual(
			self.decide(self.boss, asked["code"], "", "deny", request=asked["request"]).status_code, 200
		)
		self.assertEqual(self.collect(new, asked["request"]).status_code, 404)


class TheGates(ApprovalCase):
	def test_off_means_the_paths_do_not_exist(self):
		self.configure_keys(1, phone_approval_enabled=0)
		self.assertEqual(self.ask(Phone()).status_code, 401)

	def test_an_unsigned_or_wrongly_signed_approval_is_refused(self):
		asked = self.payload(self.ask(Phone()))["message"]
		self.assertNotEqual(
			self.decide(self.boss, asked["code"], WORKER, sign=False, request=asked["request"]).status_code,
			200,
		)
		self.assertNotEqual(
			self.decide(self.boss, asked["code"], WORKER, request="someone-else").status_code, 200
		)

	def test_a_legacy_secret_cannot_approve(self):
		asked = self.payload(self.ask(Phone()))["message"]
		credential = self.enrol(email="hr2@example.test", name="Hal", role="Farm Manager")
		set_roles("hr2@example.test", ["Farm Manager", "HR Manager"])
		response = self.post(
			"/farmops/api/mobile/approve_access_request",
			{"code": asked["code"], "for_user": WORKER},
			credential=credential,
		)
		self.assertNotEqual(response.status_code, 200)
		self.assertIn("Face ID", response.get_data(as_text=True))

	def test_nobody_approves_their_own_and_a_picker_cannot_approve(self):
		asked = self.payload(self.ask(Phone()))["message"]
		self.assertNotEqual(
			self.decide(self.boss, asked["code"], BOSS, request=asked["request"]).status_code, 200
		)
		self.assertFalse(device_keys.may_approve_for(WORKER, BOSS))

	def test_a_managers_phone_needs_a_system_manager(self):
		set_roles(WORKER, ["Field Worker"])
		set_roles("hr3@example.test", ["HR Manager"])
		set_roles("mgr@example.test", ["Farm Manager"])
		self.assertFalse(device_keys.may_approve_for("hr3@example.test", "mgr@example.test"))
		set_roles("root@example.test", ["System Manager"])
		self.assertTrue(device_keys.may_approve_for("root@example.test", "mgr@example.test"))

	def test_five_requests_an_hour_per_address(self):
		codes = [self.ask(Phone()).status_code for _ in range(device_keys.REQUESTS_PER_HOUR_PER_ADDRESS + 1)]
		self.assertEqual(codes[:5], [200] * 5)
		self.assertEqual(codes[-1], 404)

	def test_no_mcp_tool_approves_a_phone(self):
		from erpnext_mcp import registry

		self.assertFalse(
			[name for name in registry.TOOLS if "approve_access" in name or "deny_access" in name]
		)

	def test_the_people_list_never_includes_the_approver(self):
		answer = self.payload(
			self.signed(self.boss, "/farmops/api/mobile/list_approvable_people", token=self.boss.token)
		)["message"]
		users = [row["user"] for row in answer["people"]]
		self.assertIn(WORKER, users)
		self.assertNotIn(BOSS, users)
