# SPDX-License-Identifier: MIT
"""OAuth 2.1 for MCP clients. v0.220.0 — device_client_enrollment.md §6.3–§6.4."""

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlsplit

import frappe

from erpnext_mcp import device_keys, oauth

from .harness import STORE, set_roles
from .test_phone_approval import BOSS, ApprovalCase
from .test_security_217 import Defaults

REDIRECT = "http://127.0.0.1:33418/callback"
ISSUER = "https://farm-office.tail1234.ts.net:8443"
ROOT = "root@example.test"


def pkce():
	verifier = secrets.token_urlsafe(48)
	challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
	return verifier, challenge


class OAuthCase(ApprovalCase):
	def setUp(self):
		super().setUp()
		frappe.conf["encryption_key"] = "k" * 44
		self.defaults = Defaults()
		original = getattr(frappe, "defaults", None)
		frappe.defaults = self.defaults
		self.addCleanup(
			lambda: setattr(frappe, "defaults", original) if original else delattr(frappe, "defaults")
		)
		self.oauth_on()
		STORE.seed("User", [{"name": ROOT, "enabled": 1}])
		set_roles(ROOT, ["System Manager"])

	def oauth_on(self, **extra):
		self.configure_keys(
			1, phone_approval_enabled=1, mcp_oauth_enabled=1, mcp_oauth_issuer=ISSUER, **extra
		)

	# ── the client's side ──
	def register(self, uris=(REDIRECT,)):
		return oauth.register_client({"client_name": "Claude Code", "redirect_uris": list(uris)})

	def open(self, client_id, scope="mcp:read", **extra):
		verifier, challenge = pkce()
		args = {
			"response_type": "code",
			"client_id": client_id,
			"redirect_uri": REDIRECT,
			"code_challenge": challenge,
			"code_challenge_method": "S256",
			"state": "st-123",
			"scope": scope,
			"resource": ISSUER + oauth.MCP_PATH,
			**extra,
		}
		opened = oauth.open_request(args, oauth.parse_client(client_id), REDIRECT, "100.64.0.9")
		return opened, verifier

	def desk(self, code, decision="approve", **kwargs):
		return device_keys.decide(ROOT, code, "", decision, "desk", **kwargs)

	def signed_in(self, scope="mcp:read", profile="read", **decide):
		client = self.register()
		opened, verifier = self.open(client["client_id"], scope)
		self.desk(opened["code"], profile=profile, **decide)
		answer = oauth.collect(opened["request"], opened["poll"])
		code = parse_qs(urlsplit(answer["redirect"]).query)["code"][0]
		tokens = oauth.exchange(
			{
				"grant_type": "authorization_code",
				"code": code,
				"client_id": client["client_id"],
				"redirect_uri": REDIRECT,
				"code_verifier": verifier,
			}
		)
		STORE.commit()
		return client, opened, tokens

	def mcp(self, method, params=None, bearer=None, token=False, path=None):
		"""One MCP call the way a site runs it: auth hook, then the endpoint."""
		from erpnext_mcp import mcp

		message = {"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params else {})}
		headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
		self.request(message, token=token, headers=headers, path=path)
		frappe.local.session.user = "Guest"
		oauth.authenticate()
		response = mcp.handle()
		STORE.commit()
		body = response.get_data(as_text=True)
		return (json.loads(body) if body.strip() else None), response


class OffByDefault(OAuthCase):
	def test_nothing_answers_and_the_static_token_is_untouched(self):
		self.configure_keys(1, phone_approval_enabled=1, mcp_oauth_issuer=ISSUER)
		self.assertFalse(oauth.enabled())
		self.assertFalse(oauth.WellKnownPage(".well-known/oauth-authorization-server").can_render())
		for endpoint in (oauth.register, oauth.authorize, oauth.token, oauth.revoke, oauth.authorize_status):
			self.request({}, token=False)
			self.assertEqual(endpoint().status_code, 404)
		_body, response = self.mcp("tools/list", token=None)
		self.assertEqual(response.status_code, 200)
		self.assertNotIn("WWW-Authenticate", response.headers)

	def test_shipped_defaults(self):
		from .harness import META

		fields = {f["fieldname"]: f for f in META["ERPNext MCP Settings"].fields}
		self.assertEqual(fields["mcp_oauth_enabled"]["default"], "0")
		self.assertEqual(fields["legacy_static_mcp_token"]["default"], "1")
		self.assertEqual(fields["allow_revoke_mcp_client"]["default"], "0")
		self.assertFalse([name for name in fields if name.startswith("allow_") and "approve_access" in name])


class Discovery(OAuthCase):
	def test_both_documents_with_and_without_a_path_suffix(self):
		prm = oauth.WellKnownPage(".well-known/oauth-protected-resource/api/method/erpnext_mcp.mcp.handle")
		self.assertTrue(prm.can_render())
		data = json.loads(prm.render().get_data(as_text=True))
		self.assertEqual(data["resource"], ISSUER + oauth.MCP_PATH)
		self.assertEqual(data["authorization_servers"], [ISSUER])
		asm = json.loads(
			oauth.WellKnownPage("/.well-known/oauth-authorization-server").render().get_data(as_text=True)
		)
		self.assertEqual(asm["issuer"], ISSUER)
		self.assertEqual(asm["code_challenge_methods_supported"], ["S256"])
		self.assertEqual(asm["token_endpoint_auth_methods_supported"], ["none"])
		self.assertTrue(asm["registration_endpoint"].endswith(oauth.REGISTER_PATH))

	def test_no_other_path_is_claimed(self):
		for path in (
			".well-known/openid-configuration",
			"app",
			"me",
			".well-known/oauth-protected-resourcex",
		):
			self.assertFalse(oauth.WellKnownPage(path).can_render(), path)

	def test_a_401_sends_the_client_to_discovery(self):
		_body, response = self.mcp("tools/list")
		self.assertEqual(response.status_code, 401)
		self.assertIn(
			f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource{oauth.MCP_PATH}"',
			response.headers["WWW-Authenticate"],
		)

	def test_with_oauth_on_and_no_static_token_it_is_401_not_404(self):
		self.set_token("")
		_body, response = self.mcp("tools/list")
		self.assertEqual(response.status_code, 401)


class Registration(OAuthCase):
	def test_registration_writes_nothing_and_the_id_is_signed(self):
		before = len(STORE.rows("Farm Access Request"))
		client = self.register()
		self.assertEqual(len(STORE.rows("Farm Access Request")), before)
		self.assertEqual(oauth.parse_client(client["client_id"])["redirect_uris"], [REDIRECT])
		forged = client["client_id"][:-3] + ("AAA" if not client["client_id"].endswith("AAA") else "BBB")
		self.assertIsNone(oauth.parse_client(forged))

	def test_redirect_uris_are_https_or_loopback(self):
		for bad in ("http://evil.example/cb", "https://x.example/cb#frag", "javascript:alert(1)"):
			with self.assertRaises(oauth.OAuthError):
				self.register([bad])
		self.register(["https://claude.ai/api/mcp/auth_callback"])

	def test_no_site_key_no_registration(self):
		frappe.conf.pop("encryption_key", None)
		with self.assertRaises(oauth.OAuthError):
			self.register()

	def test_a_loopback_redirect_may_change_port_only(self):
		self.assertTrue(oauth.redirect_matches([REDIRECT], "http://127.0.0.1:50001/callback"))
		self.assertFalse(oauth.redirect_matches([REDIRECT], "http://127.0.0.1:50001/other"))
		self.assertFalse(oauth.redirect_matches(["https://a.example/cb"], "https://a.example:444/cb"))


class TheAuthorizationRequest(OAuthCase):
	def test_pkce_s256_is_required_and_the_resource_must_be_this_endpoint(self):
		client = self.register()
		with self.assertRaises(oauth.OAuthError):
			self.open(client["client_id"], code_challenge_method="plain")
		with self.assertRaises(oauth.OAuthError) as raised:
			self.open(client["client_id"], resource="https://elsewhere.example/mcp")
		self.assertEqual(raised.exception.error, "invalid_target")
		with self.assertRaises(oauth.OAuthError):
			self.open(client["client_id"], scope="mcp:everything")

	def test_the_code_is_hashed_and_the_page_cannot_be_framed(self):
		client = self.register()
		opened, _ = self.open(client["client_id"])
		self.assertNotIn(
			opened["code"].replace("-", ""), json.dumps(STORE.rows("Farm Access Request"), default=str)
		)
		page = oauth._consent_page("<script>x</script>", opened)
		text = page.get_data(as_text=True)
		self.assertIn("&lt;script&gt;x&lt;/script&gt;", text)
		self.assertEqual(page.headers["X-Frame-Options"], "DENY")
		self.assertIn("frame-ancestors 'none'", page.headers["Content-Security-Policy"])

	def test_an_unregistered_client_is_never_redirected(self):
		self.request({}, token=False)
		frappe.local.form_dict = {"client_id": "fo1.bad.bad", "redirect_uri": "https://evil.example/cb"}
		response = oauth.authorize()
		self.assertEqual(response.status_code, 400)
		self.assertNotIn("Location", response.headers)


class TheWholeFlow(OAuthCase):
	def test_register_authorize_approve_exchange_call(self):
		_client, _opened, tokens = self.signed_in()
		self.assertEqual(tokens["token_type"], "Bearer")
		self.assertEqual(tokens["expires_in"], 3600)
		self.assertEqual(tokens["scope"], "mcp:read")
		stored = json.dumps(STORE.rows("Farm Access Token"), default=str)
		self.assertNotIn(tokens["access_token"], stored)
		self.assertNotIn(tokens["refresh_token"], stored)
		body, response = self.mcp("tools/list", bearer=tokens["access_token"])
		self.assertEqual(response.status_code, 200, body)
		names = {tool["name"] for tool in body["result"]["tools"]}
		self.assertIn("get_server_status", names)
		self.assertNotIn("create_crop", names)

	def test_the_page_collects_once_and_carries_state_and_issuer(self):
		client = self.register()
		opened, _ = self.open(client["client_id"])
		self.assertEqual(oauth.collect(opened["request"], opened["poll"])["status"], "pending")
		self.assertEqual(oauth.collect(opened["request"], "wrong")["status"], "unknown")
		self.desk(opened["code"], profile="read")
		answer = oauth.collect(opened["request"], opened["poll"])
		query = parse_qs(urlsplit(answer["redirect"]).query)
		self.assertEqual((query["state"], query["iss"]), (["st-123"], [ISSUER]))
		self.assertEqual(oauth.collect(opened["request"], opened["poll"])["status"], "unknown")

	def test_a_denial_redirects_with_access_denied(self):
		client = self.register()
		opened, _ = self.open(client["client_id"])
		self.desk(opened["code"], "deny")
		answer = oauth.collect(opened["request"], opened["poll"])
		self.assertIn("error=access_denied", answer["redirect"])

	def test_wrong_verifier_wrong_client_wrong_redirect(self):
		client = self.register()
		opened, verifier = self.open(client["client_id"])
		self.desk(opened["code"], profile="read")
		code = parse_qs(urlsplit(oauth.collect(opened["request"], opened["poll"])["redirect"]).query)["code"][
			0
		]
		base = {
			"grant_type": "authorization_code",
			"code": code,
			"client_id": client["client_id"],
			"redirect_uri": REDIRECT,
			"code_verifier": verifier,
		}
		for override in (
			{"code_verifier": "x" * 50},
			{"client_id": self.register()["client_id"]},
			{"redirect_uri": "http://127.0.0.1:1/callback"},
		):
			with self.assertRaises(oauth.OAuthError):
				oauth.exchange({**base, **override})
		self.assertTrue(oauth.exchange(base)["access_token"])

	def test_a_code_used_twice_ends_the_sign_in_and_alerts(self):
		client = self.register()
		opened, verifier = self.open(client["client_id"])
		self.desk(opened["code"], profile="read")
		code = parse_qs(urlsplit(oauth.collect(opened["request"], opened["poll"])["redirect"]).query)["code"][
			0
		]
		args = {
			"grant_type": "authorization_code",
			"code": code,
			"client_id": client["client_id"],
			"redirect_uri": REDIRECT,
			"code_verifier": verifier,
		}
		tokens = oauth.exchange(args)
		STORE.commit()
		STORE.emails.clear()
		with self.assertRaises(oauth.OAuthError):
			oauth.exchange(args)
		self.assertIsNone(oauth.bearer_grant(tokens["access_token"]))
		self.assertTrue(any("replayed" in mail["subject"] for mail in STORE.emails))


class Refresh(OAuthCase):
	def test_rotation_and_reuse_kills_the_family(self):
		client, _opened, first = self.signed_in()
		second = oauth.exchange(
			{
				"grant_type": "refresh_token",
				"refresh_token": first["refresh_token"],
				"client_id": client["client_id"],
			}
		)
		STORE.commit()
		self.assertNotEqual(second["refresh_token"], first["refresh_token"])
		self.assertIsNotNone(oauth.bearer_grant(second["access_token"]))
		STORE.emails.clear()
		with self.assertRaises(oauth.OAuthError):
			oauth.exchange(
				{
					"grant_type": "refresh_token",
					"refresh_token": first["refresh_token"],
					"client_id": client["client_id"],
				}
			)
		STORE.commit()
		self.assertIsNone(oauth.bearer_grant(second["access_token"]))
		with self.assertRaises(oauth.OAuthError):
			oauth.exchange(
				{
					"grant_type": "refresh_token",
					"refresh_token": second["refresh_token"],
					"client_id": client["client_id"],
				}
			)
		self.assertTrue(STORE.emails)

	def test_the_revoke_endpoint_ends_the_family(self):
		_client, _opened, tokens = self.signed_in()
		self.assertGreaterEqual(oauth.revoke_presented(tokens["refresh_token"]), 2)
		STORE.commit()
		self.assertIsNone(oauth.bearer_grant(tokens["access_token"]))


class Scopes(OAuthCase):
	def test_never_more_than_was_asked(self):
		self.assertEqual(
			oauth.granted_scopes("mcp:read", scopes="mcp:read mcp:write:accounting"), ["mcp:read"]
		)
		self.assertEqual(oauth.granted_scopes("mcp:read mcp:write:farm", profile="read"), ["mcp:read"])
		self.assertEqual(
			oauth.granted_scopes("mcp:read mcp:write:farm", profile="read_farm"),
			["mcp:read", "mcp:write:farm"],
		)

	def test_a_farm_manager_grants_read_only(self):
		set_roles("fm@example.test", ["Farm Manager"])
		self.assertTrue(oauth.may_approve("fm@example.test", ["mcp:read"]))
		self.assertFalse(oauth.may_approve("fm@example.test", ["mcp:read", "mcp:write:farm"]))
		self.assertFalse(oauth.may_approve(BOSS + ".nobody", ["mcp:read"]))

	def test_a_switched_on_write_tool_outside_the_grant_is_refused(self):
		self.oauth_on(allow_create_crop=1)
		_client, _opened, tokens = self.signed_in()
		body, _ = self.mcp(
			"tools/call", {"name": "create_crop", "arguments": {}}, bearer=tokens["access_token"]
		)
		self.assertTrue(body["result"]["isError"])
		self.assertIn("outside the scopes", body["result"]["content"][0]["text"])

	def test_scopes_never_widen_past_the_switches(self):
		_client, _opened, tokens = self.signed_in(scope="mcp:read mcp:write:farm", profile="read_farm")
		body, _ = self.mcp("tools/list", bearer=tokens["access_token"])
		names = {tool["name"] for tool in body["result"]["tools"]}
		self.assertNotIn("create_crop", names)  # its switch is off
		self.oauth_on(allow_create_crop=1)
		frappe.local.erpnext_mcp_oauth = {"scopes": "mcp:read mcp:write:farm", "client": "x", "sub": ROOT}
		self.assertEqual(
			oauth.permits("create_crop"),
			__import__("erpnext_mcp.tool_groups").tool_groups.domain_of("create_crop") == "farm",
		)
		frappe.local.erpnext_mcp_oauth = None

	def test_the_audit_row_names_the_client_and_the_approver(self):
		_client, opened, tokens = self.signed_in()
		self.mcp("tools/call", {"name": "get_server_status", "arguments": {}}, bearer=tokens["access_token"])
		rows = [row for row in STORE.rows("MCP Action Log") if row.get("tool_name") == "get_server_status"]
		self.assertIn(opened["request"], rows[-1].get("agent_session") or "")
		self.assertIn(ROOT, rows[-1].get("agent_session") or "")


class TheAuthHook(OAuthCase):
	def test_the_token_opens_the_mcp_endpoint_and_nothing_else(self):
		_client, _opened, tokens = self.signed_in()
		self.request(
			{},
			token=False,
			headers={"Authorization": f"Bearer {tokens['access_token']}"},
			path="/api/method/frappe.client.get_list",
		)
		frappe.local.session.user = "Guest"
		oauth.authenticate()
		self.assertEqual(frappe.local.session.user, "Guest")
		self.assertIsNone(oauth.current())

	def test_an_unknown_token_is_left_for_frappe_to_refuse(self):
		_body, response = self.mcp("tools/list", bearer="not-a-token")
		self.assertEqual(response.status_code, 401)

	def test_it_runs_as_the_oauth_user_and_is_nobodys_phone(self):
		self.oauth_on(mcp_oauth_user=ROOT)
		_client, _opened, tokens = self.signed_in()
		self.mcp("tools/list", bearer=tokens["access_token"])
		self.assertEqual(frappe.local.session.user, ROOT)
		self.assertEqual(getattr(frappe.local, "erpnext_mcp_calling_user", None), "")


class TheStaticToken(OAuthCase):
	def test_kept_by_default_refused_when_unticked(self):
		_body, response = self.mcp("tools/list", token=None)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(self.defaults.values.get(oauth.STATIC_LAST_USED_KEY), str(frappe.utils.now())[:10])
		self.oauth_on(legacy_static_mcp_token=0)
		_body, response = self.mcp("tools/list", token=None)
		self.assertEqual(response.status_code, 401)

	def test_ready_to_disable_needs_a_client_and_fourteen_quiet_days(self):
		status = oauth.static_token_status()
		self.assertFalse(status["ready_to_disable_static_mcp_token"])
		self.signed_in()
		self.defaults.values[oauth.STATIC_TRACKED_SINCE_KEY] = "2020-01-01"
		self.defaults.values[oauth.STATIC_LAST_USED_KEY] = "2020-01-02"
		self.assertTrue(oauth.static_token_status()["ready_to_disable_static_mcp_token"])
		self.assertIn("mcp_auth", self.tool_data("get_server_status"))


class ApproveOnAPhone(OAuthCase):
	def setUp(self):
		super().setUp()
		set_roles(BOSS, ["Farm Manager", "HR Manager", "System Manager"])

	def test_peek_then_approve_with_face_id_over_the_scopes(self):
		client = self.register()
		opened, _ = self.open(client["client_id"], scope="mcp:read mcp:write:farm")
		seen = self.payload(
			self.signed(
				self.boss,
				"/farmops/api/mobile/peek_access_request",
				{"code": opened["code"]},
				token=self.boss.token,
			)
		)["message"]
		self.assertEqual((seen["kind"], seen["client_name"]), ("mcp_client", "Claude Code"))
		self.assertEqual(seen["requested_scopes"], ["mcp:read", "mcp:write:farm"])
		body = {
			"code": opened["code"],
			"profile": "read",
			"signature": self.boss.unlock.sign(
				f"farmops-approve|{opened['request']}|mcp:read|approve".encode()
			),
		}
		answer = self.signed(
			self.boss, "/farmops/api/mobile/approve_access_request", body, token=self.boss.token
		)
		self.assertEqual(answer.status_code, 200, answer.get_data(as_text=True))
		self.assertEqual(oauth.collect(opened["request"], opened["poll"])["status"], "redirect")

	def test_a_signature_over_other_scopes_is_refused(self):
		client = self.register()
		opened, _ = self.open(client["client_id"], scope="mcp:read mcp:write:farm")
		body = {
			"code": opened["code"],
			"profile": "read_farm",
			"signature": self.boss.unlock.sign(
				f"farmops-approve|{opened['request']}|mcp:read|approve".encode()
			),
		}
		answer = self.signed(
			self.boss, "/farmops/api/mobile/approve_access_request", body, token=self.boss.token
		)
		self.assertNotEqual(answer.status_code, 200)
		self.assertEqual(oauth.collect(opened["request"], opened["poll"])["status"], "pending")


class InventoryAndRevoke(OAuthCase):
	def test_listed_then_revoked(self):
		self.oauth_on(allow_revoke_mcp_client=1)
		_client, opened, tokens = self.signed_in()
		self.be("Administrator")
		listed = self.tool_data("list_access_inventory")["clients"]
		self.assertEqual([row["name"] for row in listed], [opened["request"]])
		self.assertEqual(listed[0]["approved_by"], ROOT)
		answer = self.tool_data("revoke_mcp_client", {"client": opened["request"], "reason": "test"})
		self.assertGreaterEqual(answer["tokens_revoked"], 2)
		self.assertIsNone(oauth.bearer_grant(tokens["access_token"]))
		self.assertEqual(self.tool_data("list_access_inventory")["clients"], [])

	def test_no_mcp_tool_approves_a_client(self):
		from erpnext_mcp import registry

		self.assertFalse(
			[name for name in registry.TOOLS if "approve" in name and ("client" in name or "access" in name)]
		)
