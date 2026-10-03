# SPDX-License-Identifier: MIT
"""Device keys, pickup links, signed requests. v0.218.0 — docs/design/device_client_enrollment.md."""

import base64
import hashlib
import json
import time
import uuid

import frappe
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from erpnext_mcp import device_keys

from .harness import STORE
from .test_api_mobile import WORKER
from .test_farmops_api import FarmOpsAPITestCase

CONTEXT = "/farmops/api/mobile/get_current_user_context"


def b64u(data: bytes) -> str:
	return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Key:
	"""A P-256 key as the Secure Enclave would hold it: sign, and show the public JWK."""

	def __init__(self):
		self.private = ec.generate_private_key(ec.SECP256R1())

	@property
	def jwk(self) -> dict:
		numbers = self.private.public_key().public_numbers()
		return {
			"kty": "EC",
			"crv": "P-256",
			"x": b64u(numbers.x.to_bytes(32, "big")),
			"y": b64u(numbers.y.to_bytes(32, "big")),
		}

	def sign(self, message: bytes) -> str:
		r, s = decode_dss_signature(self.private.sign(message, ec.ECDSA(hashes.SHA256())))
		return b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


class Phone:
	def __init__(self):
		self.unlock, self.proof = Key(), Key()
		self.token = ""
		self.device = ""

	@property
	def jkt(self):
		return device_keys.thumbprint(self.proof.jwk)

	def dpop(self, method, path, body: bytes, token="", **override) -> str:
		header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self.proof.jwk}
		claims = {
			"htm": method,
			"htu": f"https://farm.tail1234.ts.net{path}",
			"iat": int(time.time()),
			"jti": uuid.uuid4().hex,
			"bh": b64u(hashlib.sha256(body).digest()),
		}
		if token:
			claims["ath"] = b64u(hashlib.sha256(token.encode()).digest())
		claims.update(override)
		signing = f"{b64u(json.dumps(header).encode())}.{b64u(json.dumps(claims).encode())}"
		return f"{signing}.{self.proof.sign(signing.encode())}"

	def keys_body(self, **extra) -> dict:
		return {
			"unlock_public_key": self.unlock.jwk,
			"proof_public_key": self.proof.jwk,
			"key_protection": "secure_enclave",
			"platform": "ios",
			"os_version": "26.4",
			"app_version": "0.28.0 (11)",
			**extra,
		}


class DeviceKeyCase(FarmOpsAPITestCase):
	def setUp(self):
		super().setUp()
		device_keys._JTI_SEEN.clear()
		self.configure_keys(1)

	def configure_keys(self, on, **extra):
		from .test_api_mobile import ON as BASE

		self.configure(
			enabled=1,
			public_url="https://umbrel.tail4a2b.ts.net",
			farmops_public_url="https://farm.tail1234.ts.net",
			device_keys_enabled=on,
			allow_issue_enrollment_link=1,
			allow_report_lost_device=1,
			security_alert_email="sec@example.test",
			**{**BASE, **extra},
		)

	def signed(self, phone, path, body=None, token=None, method="POST", dpop=None):
		raw = json.dumps(body or {}).encode()
		headers = {"DPoP": dpop if dpop is not None else phone.dpop(method, path, raw, token or "")}
		if token is not None:
			headers["Authorization"] = f"FarmOps {token}"
		return self.post(path, body, credential=False, headers=headers, method=method)

	def link(self, user=WORKER):
		self.be("Administrator")
		data = self.tool_data("issue_enrollment_link", {"user": user, "device_name": "New iPhone"})
		STORE.commit()
		return data["link"].rsplit("/", 1)[1], data

	def pickup(self, phone, nonce, **override):
		path = f"/farmops/api/enroll/{nonce}"
		body = phone.keys_body(
			unlock_signature=phone.unlock.sign(f"farmops-enroll|{nonce}|{phone.jkt}".encode())
		)
		body.update(override)
		return self.signed(phone, path, body)

	def enrolled_phone(self):
		phone = Phone()
		nonce, _data = self.link()
		answer = self.payload(self.pickup(phone, nonce))["message"]
		phone.token, phone.device = answer["access_token"], answer["device"]
		return phone


class OffByDefault(DeviceKeyCase):
	def test_with_it_off_the_paths_do_not_exist_and_no_link_is_issued(self):
		self.configure_keys(0)
		self.assertIn("device keys are off", self.tool_error("issue_enrollment_link", {"user": WORKER}))
		self.assertEqual(self.post("/farmops/api/enroll/abc", credential=False).status_code, 401)
		self.assertEqual(self.post("/farmops/api/auth/challenge", credential=False).status_code, 401)
		phone = Phone()
		self.assertEqual(self.signed(phone, CONTEXT, token="x").status_code, 401)

	def test_the_old_credential_still_works(self):
		self.configure_keys(0)
		self.assertEqual(self.post(CONTEXT).status_code, 200)


class Pickup(DeviceKeyCase):
	def test_the_link_is_on_the_farmops_address_and_carries_no_credential(self):
		nonce, data = self.link()
		self.assertTrue(data["link"].startswith("https://farm.tail1234.ts.net/farmops/api/enroll/"))
		self.assertGreaterEqual(len(nonce), 40)
		self.assertNotIn(nonce, json.dumps(STORE.rows("Mobile Access Grant"), default=str))

	def test_a_get_never_spends_it(self):
		nonce, _ = self.link()
		page = self.post(f"/farmops/api/enroll/{nonce}", credential=False, method="GET")
		self.assertEqual(page.status_code, 200)
		self.assertTrue(page.headers["Content-Type"].startswith("text/plain"))
		self.assertEqual(
			self.post("/farmops/api/enroll/NOT-A-NONCE", credential=False, method="GET").get_data(),
			page.get_data(),
		)
		self.assertEqual(self.pickup(Phone(), nonce).status_code, 200)

	def test_pickup_binds_both_keys_and_answers_a_token_not_a_secret(self):
		phone = Phone()
		nonce, _ = self.link()
		answer = self.payload(self.pickup(phone, nonce))["message"]
		self.assertNotIn("api_secret", answer)
		self.assertTrue(answer["access_token"])
		row = frappe.db.get_value(
			"Mobile Device Enrollment",
			answer["device"],
			["enrollment_status", "key_thumbprint", "approval_method", "api_key"],
			as_dict=True,
		)
		self.assertEqual(row["enrollment_status"], "Enrolled")
		self.assertEqual(row["key_thumbprint"], phone.jkt)
		self.assertEqual(row["approval_method"], "pickup_link")
		self.assertFalse(row.get("api_key"))

	def test_twice_expired_or_unsigned_is_the_same_404(self):
		phone = Phone()
		nonce, _ = self.link()
		self.assertEqual(self.pickup(phone, nonce).status_code, 200)
		again = self.pickup(Phone(), nonce)
		unknown = self.pickup(Phone(), "x" * 43)
		self.assertEqual((again.status_code, unknown.status_code), (404, 404))
		self.assertEqual(again.get_data(), unknown.get_data())
		other, _ = self.link()
		self.assertEqual(self.pickup(Phone(), other, unlock_signature="A" * 86).status_code, 404)

	def test_an_expired_link_is_refused(self):
		nonce, data = self.link()
		frappe.db.set_value(
			"Mobile Device Enrollment", data["device"], "enrollment_expires_at", "2000-01-01 00:00:00"
		)
		STORE.commit()
		self.assertEqual(self.pickup(Phone(), nonce).status_code, 404)


class SignedRequests(DeviceKeyCase):
	def test_a_signed_request_with_its_token_works(self):
		phone = self.enrolled_phone()
		response = self.signed(phone, CONTEXT, token=phone.token)
		self.assertEqual(response.status_code, 200)
		self.assertTrue(self.payload(response)["message"]["device_keys"]["enabled"])

	def test_every_way_of_getting_it_wrong_is_the_uniform_401(self):
		phone = self.enrolled_phone()
		thief = Phone()
		raw = b"{}"
		cases = {
			"no proof": self.post(
				CONTEXT, credential=False, headers={"Authorization": f"FarmOps {phone.token}"}
			),
			"another key": self.signed(thief, CONTEXT, token=phone.token),
			"wrong path": self.signed(
				phone,
				CONTEXT,
				token=phone.token,
				dpop=phone.dpop("POST", "/farmops/api/mobile/other", raw, phone.token),
			),
			"old proof": self.signed(
				phone,
				CONTEXT,
				token=phone.token,
				dpop=phone.dpop("POST", CONTEXT, raw, phone.token, iat=int(time.time()) - 600),
			),
			"body changed": self.signed(
				phone,
				CONTEXT,
				body={"company": "x"},
				token=phone.token,
				dpop=phone.dpop("POST", CONTEXT, raw, phone.token),
			),
			"no ath": self.signed(
				phone, CONTEXT, token=phone.token, dpop=phone.dpop("POST", CONTEXT, raw, "")
			),
		}
		for label, response in cases.items():
			with self.subTest(label):
				self.assertEqual(response.status_code, 401)

	def test_a_replayed_proof_is_refused(self):
		phone = self.enrolled_phone()
		proof = phone.dpop("POST", CONTEXT, b"{}", phone.token)
		self.assertEqual(self.signed(phone, CONTEXT, token=phone.token, dpop=proof).status_code, 200)
		self.assertEqual(self.signed(phone, CONTEXT, token=phone.token, dpop=proof).status_code, 401)

	def test_tokens_are_stored_as_hashes(self):
		phone = self.enrolled_phone()
		self.assertNotIn(phone.token, json.dumps(STORE.rows("Farm Access Token")))


class ChallengeAndToken(DeviceKeyCase):
	def mint(self, phone, signer=None):
		answer = self.payload(self.signed(phone, "/farmops/api/auth/challenge", {"device": phone.device}))[
			"message"
		]
		challenge = answer["challenge"]
		message = f"farmops-token|{challenge}|{phone.device}|{phone.jkt}".encode()
		return challenge, self.signed(
			phone,
			"/farmops/api/auth/token",
			{
				"device": phone.device,
				"challenge": challenge,
				"signature": (signer or phone.unlock).sign(message),
			},
		)

	def test_face_id_signs_a_challenge_for_a_fresh_token(self):
		phone = self.enrolled_phone()
		_challenge, response = self.mint(phone)
		self.assertEqual(response.status_code, 200)
		fresh = self.payload(response)["message"]["access_token"]
		self.assertEqual(self.signed(phone, CONTEXT, token=fresh).status_code, 200)

	def test_the_wrong_unlock_key_and_a_used_challenge_are_refused(self):
		phone = self.enrolled_phone()
		_c, response = self.mint(phone, signer=Key())
		self.assertEqual(response.status_code, 401)
		challenge, ok = self.mint(phone)
		self.assertEqual(ok.status_code, 200)
		message = f"farmops-token|{challenge}|{phone.device}|{phone.jkt}".encode()
		again = self.signed(
			phone,
			"/farmops/api/auth/token",
			{"device": phone.device, "challenge": challenge, "signature": phone.unlock.sign(message)},
		)
		self.assertEqual(again.status_code, 401)


class Revocation(DeviceKeyCase):
	def test_a_revoked_phone_is_told_and_only_it(self):
		phone = self.enrolled_phone()
		self.be("Administrator")
		self.tool_data("report_lost_device", {"user": WORKER, "device": phone.device})
		STORE.commit()
		mine = self.signed(phone, "/farmops/api/auth/challenge", {"device": phone.device})
		self.assertEqual(mine.status_code, 401)
		self.assertTrue(self.payload(mine).get("device_revoked"))
		theirs = self.signed(Phone(), "/farmops/api/auth/challenge", {"device": phone.device})
		self.assertEqual(theirs.status_code, 401)
		self.assertNotIn("device_revoked", self.payload(theirs))
		self.assertEqual(self.signed(phone, CONTEXT, token=phone.token).status_code, 401)

	def test_lost_ends_every_token(self):
		phone = self.enrolled_phone()
		self.be("Administrator")
		answer = self.tool_data(
			"report_lost_device", {"user": WORKER, "device": phone.device, "note": "left in a bin"}
		)
		self.assertGreaterEqual(answer["tokens_revoked"], 1)
		self.assertTrue(
			any(
				"reported" in (row.get("subject") or "") or "lost" in (row.get("message") or "")
				for row in STORE.emails
			)
		)


class TheSilentUpgrade(DeviceKeyCase):
	def test_an_old_phone_moves_to_keys_and_its_secret_dies(self):
		phone = Phone()
		message = f"farmops-upgrade|{self.credential['api_key']}|{phone.jkt}".encode()
		response = self.post(
			"/farmops/api/mobile/upgrade_device_key",
			phone.keys_body(
				unlock_signature=phone.unlock.sign(message), proof_signature=phone.proof.sign(message)
			),
		)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		answer = self.payload(response)["message"]
		self.assertTrue(answer["upgraded"])
		self.assertNotIn("access_token", answer)
		self.assertEqual(self.post(CONTEXT).status_code, 401, "the old secret is gone")
		phone.device = answer["device"]
		_challenge, minted = ChallengeAndToken.mint(self, phone)
		token = self.payload(minted)["message"]["access_token"]
		self.assertEqual(self.signed(phone, CONTEXT, token=token).status_code, 200)

	def test_off_refuses_the_upgrade(self):
		self.configure_keys(0)
		phone = Phone()
		response = self.post("/farmops/api/mobile/upgrade_device_key", phone.keys_body())
		self.assertNotEqual(response.status_code, 200)
		self.assertEqual(self.post(CONTEXT).status_code, 200)


class LegacySwitch(DeviceKeyCase):
	def test_turning_legacy_off_ends_the_old_pairs(self):
		self.configure_keys(1, legacy_device_secrets=0)
		self.assertEqual(self.post(CONTEXT).status_code, 401)

	def test_inventory_says_when_legacy_can_go(self):
		self.be("Administrator")
		data = self.tool_data("list_access_inventory", {})
		self.assertGreaterEqual(data["legacy_secret"], 1)
		self.assertFalse(data["ready_to_disable_legacy_secrets"])
