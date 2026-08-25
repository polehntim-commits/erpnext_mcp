# SPDX-License-Identifier: MIT
"""Mobile accounts, entity scoping and the credential — v0.17.0 Features A and B.

FIVE CLAIMS.

1. **AN ACCOUNT CANNOT BE CREATED WITHOUT ENTITIES, AND THERE IS NO OVERRIDE.**
   `EntityScopingIsMandatory`. In Frappe a user with NO User Permission on
   Company sees EVERY company — so the account this release would have shipped
   by accident is the LEAST scoped one on the site, not the most. That is the
   one mistake a release about scoping must make impossible, and the test that a
   Field Worker at one company cannot reach the other's parcels is the whole
   point of the sprint.

2. **THE CREDENTIAL ROUND TRIP IS REAL.** `TheCredential`. Generate, make an
   authenticated request that the server identifies as that person, revoke, make
   the same request and watch it stop being them. The harness reproduces
   Frappe's own `Authorization: token <key>:<secret>` validation for this, so
   the identity under test is the one a phone would actually present — not a
   fixture that says who the caller is.

3. **THE QR IMAGE CARRIES THE PAYLOAD IT CLAIMS TO.** `TheLoginCard`. The PNG is
   decoded — really decoded, in thirty lines of `zlib` — back to a module
   matrix, and that matrix is compared with a fresh encoding of the expected
   JSON. See `decode_png` on why that is a decode and not a hopeful equality.

4. **REVOCATION KEEPS THE EVIDENCE.** `Revocation`. The reason is mandatory, the
   roles stay on the account, and `list_mobile_users` can still answer "what
   could this person see" afterwards. An account stripped bare is an account
   nobody can be asked about.

5. **THE ROSTER REPORTS ITS OWN DRIFT.** `TheRoster`. Every `concerns` entry is
   a state that looks fine on a list and is not.
"""

import json
import struct
import zlib

import frappe

from erpnext_mcp import roles, settings
from erpnext_mcp.render import qr
from erpnext_mcp.tools import mobile

from .fixtures import MAIN, OTHER, SeededTestCase
from .harness import STORE

FUNNEL = "https://umbrel.tail4a2b.ts.net"

READS_ON = {f"allow_{name}": 1 for name in ("list_mobile_users", "get_current_user_context")}
WRITES_ON = {
	f"allow_{name}": 1
	for name in (
		"create_mobile_user",
		"revoke_mobile_user",
		"generate_api_token",
		"revoke_api_token",
		"generate_mobile_login_qr",
		"recover_mobile_access",
		"link_badge_to_employee",
		"resolve_badge",
	)
}
ALL_ON = {**READS_ON, **WRITES_ON}

WORKER = "ana@example.test"


def decode_png(data: bytes) -> list:
	"""Read one of THIS APP'S PNGs back to a matrix of 0/1. v0.17.0.

	WHY THIS EXISTS RATHER THAN A THIRD-PARTY DECODER. The claim under test is
	"the image an operator hands to a worker encodes this exact JSON". Proving it
	needs two things: the bytes turned back into pixels, and the pixels compared
	with an independent encoding of the expected payload. This is the first half,
	and it is short only because `render/qr.py` writes the simplest PNG that is
	still a PNG — 8-bit greyscale, no palette, no interlacing — which was one of
	the reasons for writing the PNG here instead of taking one from a library.

	A full QR DECODER would be the wrong test. It would spend four hundred lines
	on Reed–Solomon and mask removal in order to check `segno`'s arithmetic, which
	is not this app's arithmetic. Re-encoding the expected payload and comparing
	module for module proves the same thing about the part this app is
	responsible for: that the payload which went in is the payload that came out.
	"""
	assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
	pos, width, height, raw = 8, 0, 0, b""
	while pos < len(data):
		(length,) = struct.unpack(">I", data[pos : pos + 4])
		kind = data[pos + 4 : pos + 8]
		payload = data[pos + 8 : pos + 8 + length]
		if kind == b"IHDR":
			width, height, depth, colour = struct.unpack(">IIBB", payload[:10])
			assert (depth, colour) == (8, 0), "expected 8-bit greyscale"
		elif kind == b"IDAT":
			raw += payload
		elif kind == b"IEND":
			break
		pos += 12 + length

	pixels = zlib.decompress(raw)
	rows, stride = [], width + 1
	for index in range(height):
		chunk = pixels[index * stride : (index + 1) * stride]
		filter_type, line = chunk[0], bytearray(chunk[1:])
		# `png_bytes` writes filter type 0 on every row, so there is no unfiltering
		# to do. Asserting it rather than implementing the other four keeps this
		# honest about what it can read.
		assert filter_type == 0, f"unexpected PNG filter {filter_type}"
		rows.append(line)
	return [[1 if value == 0 else 0 for value in row] for row in rows]


def shrink(pixels: list, scale: int, border: int) -> list:
	"""Undo `png_bytes`' scaling and quiet zone, back to one cell per module."""
	inner = pixels[border * scale : len(pixels) - border * scale]
	out = []
	for index in range(0, len(inner), scale):
		row = inner[index][border * scale : len(inner[index]) - border * scale]
		out.append([row[column] for column in range(0, len(row), scale)])
	return out


class MobileTestCase(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, public_url=FUNNEL, **ALL_ON)
		roles.install_roles()

	def make(self, **overrides):
		payload = {
			"email": WORKER,
			"full_name": "Ana Ramos",
			"role": "Field Worker",
			"entity_access": [MAIN],
		}
		payload.update(overrides)
		return self.tool_data("create_mobile_user", payload)

	def make_error(self, **overrides):
		payload = {
			"email": WORKER,
			"full_name": "Ana Ramos",
			"role": "Field Worker",
			"entity_access": [MAIN],
		}
		payload.update(overrides)
		return self.tool_error("create_mobile_user", payload)

	def company_permissions(self, user=WORKER) -> list:
		return [
			row
			for row in STORE.rows("User Permission")
			if row.get("user") == user and row.get("allow") == "Company"
		]

	def as_worker(self, tool, arguments=None, key=None, secret=None):
		"""Call a tool the way a phone does: MCP token AND the worker's own credential."""
		return self.tool(
			tool,
			arguments or {},
			headers={"Authorization": f"token {key}:{secret}"},
		)


class CreatingAnAccount(MobileTestCase):
	def test_it_creates_the_user_the_role_the_scoping_and_the_grant(self):
		data = self.make()
		self.assertTrue(data["created"])
		self.assertTrue(frappe.db.exists("User", WORKER))
		self.assertEqual(roles.roles_of(WORKER), ["Field Worker"])
		self.assertEqual(roles.companies_for(WORKER), [MAIN])
		self.assertTrue(frappe.db.exists("Mobile Access Grant", WORKER))

	def test_the_secret_comes_back_exactly_once_and_says_so(self):
		data = self.make()
		self.assertTrue(data["api_secret"])
		self.assertIn("ONLY TIME", data["secret_note"])
		self.assertEqual(data["auth_header"], f"Authorization: token {data['api_key']}:{data['api_secret']}")

	def test_the_grant_never_holds_the_secret(self):
		"""A credential in a readable column leaks with the first CSV export
		somebody takes of this list."""
		self.make()
		row = STORE.get_raw("Mobile Access Grant", WORKER)
		self.assertNotIn("api_secret", row)
		self.assertEqual(row["api_key"], STORE.get_raw("User", WORKER)["api_key"])

	def test_the_preferred_company_carries_is_default(self):
		self.make(entity_access=[MAIN, OTHER], preferred_company=OTHER)
		defaults = [row for row in self.company_permissions() if int(row.get("is_default") or 0)]
		self.assertEqual([row["for_value"] for row in defaults], [OTHER])
		self.assertEqual(roles.default_company_for(WORKER), OTHER)

	def test_every_company_permission_applies_to_all_doctypes(self):
		"""One row scoping every doctype at once is the whole reason this uses
		Frappe's mechanism rather than filtering in each tool."""
		self.make(entity_access=[MAIN, OTHER])
		for row in self.company_permissions():
			with self.subTest(company=row["for_value"]):
				self.assertEqual(int(row["apply_to_all_doctypes"]), 1)

	def test_the_phone_role_is_reported_as_having_no_desk(self):
		self.assertFalse(self.make()["desk_access"])
		self.assertTrue(
			self.make(email="fran@example.test", role="Foreman", update_existing=True)["desk_access"]
		)

	def test_it_assigns_the_owning_apps_role_where_the_site_has_one(self):
		"""A Field Worker needs to read their own Employee record, and Employee
		belongs to another app. Granting it here would take HR Manager off the
		Employee register — so the role is assigned instead."""
		data = self.make()
		self.assertIn("Employee", data["roles_assigned"])
		self.assertNotIn("companion_roles_missing", data)

	def test_a_missing_companion_role_is_named_with_the_reason(self):
		STORE.tables["Role"].pop("Employee", None)
		data = self.make()
		self.assertEqual(data["companion_roles_missing"], ["Employee"])
		self.assertIn("would make Frappe ignore every standard permission", data["companion_note"])

	def test_an_unknown_role_is_refused_with_the_six(self):
		message = self.make_error(role="Supervisor")
		self.assertIn("Supervisor", message)
		for name in roles.ROLE_NAMES:
			self.assertIn(name, message)

	def test_a_new_account_needs_a_name(self):
		message = self.make_error(full_name="")
		self.assertIn("full_name is required", message)
		self.assertIn("names nobody", message)

	def test_something_that_is_not_an_email_is_refused(self):
		self.assertIn("not an email address", self.make_error(email="ana"))

	def test_an_existing_user_is_refused_unless_the_caller_says_so(self):
		"""Re-running this on a live account rewrites its roles and its scoping,
		which is a decision rather than a retry."""
		self.make()
		message = self.make_error()
		self.assertIn("already exists", message)
		self.assertIn("update_existing=true", message)
		self.assertIn("generate_api_token", message)

	def test_update_existing_rewrites_the_scoping_and_removes_what_was_dropped(self):
		"""A stale permission is the failure this release exists to prevent: an
		account moved between entities that still carries the old one."""
		self.make(entity_access=[MAIN, OTHER])
		data = self.make(entity_access=[OTHER], update_existing=True)
		self.assertEqual(data["user_permissions"]["removed"], [MAIN])
		self.assertEqual(roles.companies_for(WORKER), [OTHER])

	def test_updating_an_account_does_not_knock_its_phone_offline(self):
		"""THE DEFAULT DEPENDS ON WHETHER THE ACCOUNT IS NEW, deliberately. A new
		account with no credential cannot sign in; an existing one has a phone in
		somebody's pocket, and re-scoping them should not silently invalidate it."""
		self.make(entity_access=[MAIN, OTHER])
		before = mobile.read_api_secret(WORKER)
		data = self.make(entity_access=[MAIN], update_existing=True)
		self.assertNotIn("api_secret", data)
		self.assertIn("NO CREDENTIAL WAS TOUCHED", data["secret_note"])
		self.assertEqual(mobile.read_api_secret(WORKER), before)
		self.assertEqual(roles.companies_for(WORKER), [MAIN])

	def test_an_update_can_still_be_asked_to_mint_a_fresh_one(self):
		self.make()
		before = mobile.read_api_secret(WORKER)
		data = self.make(update_existing=True, generate_token=True)
		self.assertNotEqual(data["api_secret"], before)

	def test_generate_token_false_makes_a_scoped_account_that_cannot_sign_in(self):
		data = self.make(generate_token=False)
		self.assertNotIn("api_secret", data)
		self.assertIn("cannot sign in", data["secret_note"])
		self.assertEqual(mobile.read_api_secret(WORKER), "")

	def test_it_is_off_out_of_the_box(self):
		self.configure(enabled=1, public_url=FUNNEL)
		self.assertIn(
			"allow_create_mobile_user",
			self.tool_error(
				"create_mobile_user",
				{"email": WORKER, "full_name": "Ana", "role": "Field Worker", "entity_access": [MAIN]},
			),
		)


class EntityScopingIsMandatory(MobileTestCase):
	"""THE POINT OF THE RELEASE, and the one place the safe-looking option is the
	dangerous one."""

	def test_an_account_with_no_entities_is_refused(self):
		message = self.make_error(entity_access=[])
		self.assertIn("sees EVERY company", message)
		self.assertIn("least scoped", message)

	def test_a_missing_entity_argument_is_refused_the_same_way(self):
		message = self.tool_error(
			"create_mobile_user", {"email": WORKER, "full_name": "Ana", "role": "Field Worker"}
		)
		self.assertIn("entity_access", message)

	def test_there_is_no_flag_that_produces_an_unscoped_account(self):
		"""Asserted against the schema, not against a behaviour: a future argument
		called `all_entities` would have to fail here first."""
		from erpnext_mcp import registry

		schema = registry.TOOLS["create_mobile_user"]["inputSchema"]
		self.assertIn("entity_access", schema["required"])
		self.assertFalse(schema["additionalProperties"])

	def test_a_company_that_does_not_exist_is_refused_by_name(self):
		message = self.make_error(entity_access=["Highland Holdings LLC"])
		self.assertIn("Highland Holdings LLC", message)
		self.assertIn(MAIN, message)
		self.assertIn(OTHER, message)

	def test_an_abbreviation_resolves_to_the_company(self):
		from .fixtures import MAIN_ABBR

		self.make(entity_access=[MAIN_ABBR])
		self.assertEqual(roles.companies_for(WORKER), [MAIN])

	def test_a_preferred_company_outside_the_grant_is_refused(self):
		message = self.make_error(entity_access=[MAIN], preferred_company=OTHER)
		self.assertIn("not in entity_access", message)

	def test_a_field_worker_at_one_company_cannot_reach_the_other(self):
		"""THE TEST THE SPRINT WAS FOR. The scoping is one User Permission row per
		entity with apply_to_all_doctypes — so it covers Parcel, and Field, and
		Housing Unit, and every doctype a later release adds, without any of them
		knowing about it."""
		self.make(entity_access=[MAIN])
		allowed = roles.companies_for(WORKER)
		self.assertEqual(allowed, [MAIN])
		self.assertNotIn(OTHER, allowed)
		self.assertEqual([row["for_value"] for row in self.company_permissions()], [MAIN])

	def test_the_result_spells_out_what_is_invisible(self):
		data = self.make(entity_access=[MAIN])
		self.assertIn(MAIN, data["entity_note"])
		self.assertIn("invisible", data["entity_note"])


class ACompanyNameWithACommaInIt(MobileTestCase):
	"""S6, through the tool. "Orchard Meadow, LLC" is one entity, not two.

	`_resolve_entities` used to do `raw.replace("\n", ",").split(",")` on a
	string body, which split every LLC on the site into a name and a suffix. The
	refusal that came back named "Orchard Meadow" — a company the caller had
	spelled correctly and in full — so the only way to create the account was to
	rename the business. The parser and its reasoning are in `roles.py`; these
	are the two paths a request body actually takes.
	"""

	COMMA_CO = "Orchard Meadow, LLC"

	def setUp(self):
		super().setUp()
		STORE.seed(
			"Company",
			[
				{
					"name": self.COMMA_CO,
					"abbr": "OML",
					"default_currency": "USD",
					"country": "United States",
					"is_group": 0,
				}
			],
		)

	def test_a_list_element_containing_a_comma_is_one_entity(self):
		self.make(entity_access=[self.COMMA_CO])
		self.assertEqual(roles.companies_for(WORKER), [self.COMMA_CO])

	def test_a_string_body_containing_one_such_name_is_one_entity(self):
		self.make(entity_access=self.COMMA_CO)
		self.assertEqual(roles.companies_for(WORKER), [self.COMMA_CO])

	def test_a_string_body_naming_two_companies_still_splits(self):
		self.make(entity_access=f"{self.COMMA_CO}, {MAIN}")
		self.assertEqual(sorted(roles.companies_for(WORKER)), sorted([self.COMMA_CO, MAIN]))

	def test_the_grant_column_records_one_line_per_entity(self):
		self.make(entity_access=[self.COMMA_CO, MAIN])
		stored = frappe.db.get_value("Mobile Access Grant", WORKER, "entity_access")
		self.assertEqual(str(stored).split("\n"), [self.COMMA_CO, MAIN])

	def test_the_roster_reads_the_comma_name_back_as_one(self):
		self.make(entity_access=[self.COMMA_CO])
		rows = self.tool_data("list_mobile_users", {})["users"]
		mine = next(row for row in rows if row["user"] == WORKER)
		self.assertEqual(mine["entity_access_recorded"], [self.COMMA_CO])
		self.assertEqual(mine["entity_access"], [self.COMMA_CO])

	def test_a_name_that_is_not_a_company_is_still_refused_whole(self):
		message = self.make_error(entity_access="Nowhere Farms, LLC")
		self.assertIn("Nowhere Farms, LLC", message)


class TheCredential(MobileTestCase):
	def token_for(self, user=WORKER):
		data = self.tool_data("generate_api_token", {"user": user})
		return data["api_key"], data["api_secret"]

	def test_generate_then_authenticate_then_revoke_then_fail(self):
		"""THE ROUND TRIP, end to end, through the real endpoint with the real
		header. Not a fixture asserting who the caller is — the harness reproduces
		Frappe's own api-key validation, so this is the credential a phone sends."""
		self.make(generate_token=False)
		key, secret = self.token_for()

		result = self.as_worker("get_current_user_context", key=key, secret=secret)
		identified = json.loads(result["content"][0]["text"])
		self.assertTrue(identified["identified"])
		self.assertEqual(identified["user"], WORKER)

		self.tool_data("revoke_api_token", {"user": WORKER, "reason": "phone lost in the orchard"})

		result = self.as_worker("get_current_user_context", key=key, secret=secret)
		after = json.loads(result["content"][0]["text"])
		self.assertFalse(after["identified"])
		self.assertIn("Authorization: token", after["note"])

	def test_a_wrong_secret_is_nobody(self):
		self.make()
		key = STORE.get_raw("User", WORKER)["api_key"]
		result = self.as_worker("get_current_user_context", key=key, secret="wrong")
		self.assertFalse(json.loads(result["content"][0]["text"])["identified"])

	def test_re_issuing_stops_the_previous_one_working(self):
		"""Which is what makes this the answer to a lost phone."""
		self.make()
		first = mobile.read_api_secret(WORKER)
		data = self.tool_data("generate_api_token", {"user": WORKER})
		self.assertTrue(data["replaced_previous_token"])
		self.assertNotEqual(data["api_secret"], first)
		self.assertEqual(mobile.read_api_secret(WORKER), data["api_secret"])

	def test_the_public_half_is_kept_so_an_access_log_still_names_somebody(self):
		self.make()
		key = STORE.get_raw("User", WORKER)["api_key"]
		self.assertEqual(self.tool_data("generate_api_token", {"user": WORKER})["api_key"], key)

	def test_the_review_date_is_called_a_review_date_and_not_an_expiry(self):
		"""Frappe API secrets do not expire and this app installs no job that
		revokes one. Calling a reminder an expiry is a false assurance about a
		credential, which is worse than none."""
		self.make()
		data = self.tool_data("generate_api_token", {"user": WORKER})
		self.assertIn("REVIEW DATE, NOT AN EXPIRY", data["expiry_note"])
		self.assertIn("no scheduled job", data["expiry_note"])
		self.assertIn("token_review_due", data)

	def test_it_says_the_credential_buys_identity_and_not_entry(self):
		self.make()
		note = self.tool_data("generate_api_token", {"user": WORKER})["transport_note"]
		self.assertIn("X-MCP-Token", note)
		self.assertIn("allowed CIDR", note)

	def test_a_disabled_account_is_refused(self):
		self.make()
		frappe.db.set_value("User", WORKER, "enabled", 0)
		message = self.tool_error("generate_api_token", {"user": WORKER})
		self.assertIn("disabled", message)
		self.assertIn("afternoon debugging", message)

	def test_revoking_takes_both_halves(self):
		"""An api_key left behind reads like a live credential to anybody scanning
		the User list."""
		self.make()
		self.tool_data("revoke_api_token", {"user": WORKER})
		row = STORE.get_raw("User", WORKER)
		self.assertFalse(row.get("api_key"))
		self.assertEqual(mobile.read_api_secret(WORKER), "")

	def test_revoking_a_credential_leaves_the_account_alone(self):
		"""'They lost their phone', not 'they no longer work here'."""
		self.make()
		data = self.tool_data("revoke_api_token", {"user": WORKER})
		self.assertTrue(data["login_still_enabled"])
		self.assertTrue(data["token_revoked"])
		self.assertEqual(roles.roles_of(WORKER), ["Field Worker"])

	def test_revoking_nothing_says_so_rather_than_pretending(self):
		self.make(generate_token=False)
		data = self.tool_data("revoke_api_token", {"user": WORKER})
		self.assertFalse(data["token_revoked"])
		self.assertIn("no live credential", data["note"])

	def test_both_are_off_out_of_the_box(self):
		self.configure(enabled=1, public_url=FUNNEL)
		for tool in ("generate_api_token", "revoke_api_token"):
			with self.subTest(tool=tool):
				self.assertIn(f"allow_{tool}", self.tool_error(tool, {"user": WORKER}))


class UserContext(MobileTestCase):
	def test_with_no_credential_it_says_the_call_names_nobody(self):
		data = self.tool_data("get_current_user_context")
		self.assertFalse(data["identified"])
		self.assertIn("authorises the CALL; it does not name a person", data["note"])

	def test_an_operator_may_pass_a_user_when_the_request_carries_no_credential(self):
		"""They already hold the operator's bearer token and could read the same
		records through any read tool anyway."""
		self.make()
		data = self.tool_data("get_current_user_context", {"user": WORKER})
		self.assertTrue(data["identified"])
		self.assertIn("`user` argument", data["identity_source"])

	def test_a_request_that_authenticated_cannot_act_as_somebody_else(self):
		"""An account that can name somebody else in a request body is not scoped
		to anything."""
		self.make()
		self.make(email="fran@example.test", full_name="Fran F", role="Foreman", entity_access=[MAIN])
		key = STORE.get_raw("User", WORKER)["api_key"]
		secret = mobile.read_api_secret(WORKER)
		result = self.as_worker(
			"get_current_user_context", {"user": "fran@example.test"}, key=key, secret=secret
		)
		self.assertTrue(result["isError"])
		message = result["content"][0]["text"]
		self.assertIn("will not", message)
		self.assertIn("not scoped to anything", message)

	def test_naming_yourself_is_allowed(self):
		self.make()
		key = STORE.get_raw("User", WORKER)["api_key"]
		secret = mobile.read_api_secret(WORKER)
		result = self.as_worker("get_current_user_context", {"user": WORKER}, key=key, secret=secret)
		self.assertFalse(result["isError"])

	def test_it_reports_the_roles_the_entities_and_what_the_role_cannot_do(self):
		self.make()
		data = self.tool_data("get_current_user_context", {"user": WORKER})
		self.assertEqual(data["mobile_roles"], ["Field Worker"])
		self.assertEqual(data["entity_access"], [MAIN])
		self.assertEqual(data["preferred_company"], MAIN)
		self.assertTrue(any("Compliance Policy" in line for line in data["cannot"]))
		self.assertTrue(any("Farm Task Assignment" in line for line in data["can"]))

	def test_an_unscoped_account_is_reported_as_unrestricted(self):
		frappe.get_doc(
			{"doctype": "User", "email": "loose@example.test", "first_name": "Lou", "enabled": 1}
		).insert()
		data = self.tool_data("get_current_user_context", {"user": "loose@example.test"})
		self.assertEqual(data["entity_access"], [])
		self.assertIn("UNRESTRICTED", data["entity_note"])

	def test_it_is_on_out_of_the_box(self):
		self.configure(enabled=1, public_url=FUNNEL)
		self.assertFalse(self.tool("get_current_user_context").get("isError"))


class TheLoginCard(MobileTestCase):
	def card(self, **overrides):
		payload = {"user": WORKER}
		payload.update(overrides)
		return self.tool_data("generate_mobile_login_qr", payload)

	def test_the_png_really_decodes_to_the_expected_json(self):
		"""THE CLAIM. The image is decoded back to a module matrix and compared
		with an independent encoding of the payload the tool says it wrote."""
		import base64

		self.make()
		data = self.card()
		png = base64.b64decode(data["png_base64"])

		pixels = decode_png(png)
		self.assertEqual(len(pixels), data["pixels"])

		modules = shrink(pixels, qr.SCALE, qr.BORDER)
		self.assertEqual(len(modules), data["modules"])

		expected = json.dumps(data["payload"], separators=(",", ":"), sort_keys=True)
		self.assertEqual(modules, qr.qr_matrix(expected))

	def test_the_payload_is_the_shape_the_app_reads(self):
		self.make()
		data = self.card()
		payload = data["payload"]
		self.assertEqual(payload["url"], FUNNEL)
		self.assertEqual(payload["user"], WORKER)
		self.assertEqual(payload["token"], f"{payload['api_key']}:{payload['api_secret']}")
		self.assertTrue(payload["expires_at"])
		self.assertEqual(payload["v"], 1)

	def test_the_card_points_at_where_v0180_actually_serves_the_phone(self):
		"""v0.18.0. `endpoint` was the MCP path, which no phone has ever called.
		It is now the FIRST URL the app hits — so an operator can curl the card
		before handing the phone to somebody standing in an orchard."""
		self.make()
		payload = self.card()["payload"]
		self.assertEqual(payload["api_base"], "/farmops/api")
		self.assertEqual(payload["endpoint"], f"{FUNNEL}/farmops/api/mobile/get_current_user_context")

	def test_the_payload_version_did_not_move_or_every_shipped_phone_stops_scanning(self):
		"""`LoginQRParser` refuses a payload whose `v` exceeds the build's
		`supportedVersion`, which is 1. The transport moved; the enrolment format
		did not, and bumping this would brick the phones this release is for."""
		self.make()
		self.assertEqual(self.card()["payload"]["v"], 1)

	def test_the_token_on_the_card_is_the_one_that_works(self):
		self.make()
		payload = self.card()["payload"]
		result = self.as_worker(
			"get_current_user_context", key=payload["api_key"], secret=payload["api_secret"]
		)
		self.assertEqual(json.loads(result["content"][0]["text"])["user"], WORKER)

	def test_rotating_by_default_kills_the_phone_that_was_already_enrolled(self):
		"""Which is what makes re-minting a card a real revocation of the old one."""
		self.make()
		before = mobile.read_api_secret(WORKER)
		data = self.card()
		self.assertTrue(data["token_rotated"])
		self.assertNotEqual(data["payload"]["api_secret"], before)
		self.assertIn("must re-scan", data["security_note"])

	def test_not_rotating_reprints_the_same_credential_and_says_the_cost(self):
		self.make()
		before = mobile.read_api_secret(WORKER)
		data = self.card(rotate_token=False)
		self.assertEqual(data["payload"]["api_secret"], before)
		self.assertIn("any earlier copy of this card", data["security_note"])

	def test_not_rotating_with_no_credential_to_reprint_is_refused(self):
		self.make(generate_token=False)
		self.assertIn(
			"no live API credential",
			self.tool_error("generate_mobile_login_qr", {"user": WORKER, "rotate_token": False}),
		)

	def test_a_plaintext_endpoint_is_refused(self):
		"""Encoding a live credential for an http:// URL puts it on the wire in the
		clear at every call, forever."""
		self.make()
		message = self.tool_error("generate_mobile_login_qr", {"user": WORKER, "url": "http://umbrel.local"})
		self.assertIn("not HTTPS", message)
		self.assertIn("Nothing was written", message)

	def test_a_site_that_does_not_know_its_own_public_url_is_refused(self):
		"""`frappe.utils.get_url()` answers for the server; behind a Funnel it can
		answer with nothing useful, and a QR pointing a phone at nothing is worse
		than no QR."""
		self.configure(enabled=1, public_url="", **ALL_ON)
		self.make()
		original = frappe.utils.get_url
		frappe.utils.get_url = lambda *args, **kwargs: ""
		try:
			message = self.tool_error("generate_mobile_login_qr", {"user": WORKER})
		finally:
			frappe.utils.get_url = original
		self.assertIn("public_url", message)
		self.assertIn("ts.net", message)
		self.assertIn("get_tailscale_funnel_config", message)

	def test_a_disabled_account_is_refused(self):
		self.make()
		frappe.db.set_value("User", WORKER, "enabled", 0)
		self.assertIn("disabled", self.tool_error("generate_mobile_login_qr", {"user": WORKER}))

	def test_an_expiry_beyond_a_week_is_refused(self):
		self.make()
		message = self.tool_error("generate_mobile_login_qr", {"user": WORKER, "expiry_hours": 200})
		self.assertIn("between 1 and 168", message)
		self.assertIn("photo roll", message)

	def test_an_expiry_of_zero_is_refused_by_the_guard_that_names_it(self):
		"""THE REFUSAL THAT COULD NOT SEE THE VALUE IT NAMED. The guard reads
		`if hours <= 0 or hours > 168`, and the line above it was
		`as_int(args, "expiry_hours", DEFAULT_QR_HOURS) or DEFAULT_QR_HOURS` — so
		an explicit 0 became the default before the check ever ran. The `<= 0`
		half was unreachable, and a caller asking for a zero-hour credential was
		handed a LIVE WORKING LOGIN QR on the default window instead of a no.

		That is the direction that matters: the mistake produced a usable
		credential and a success payload, not an error."""
		self.make()
		message = self.tool_error("generate_mobile_login_qr", {"user": WORKER, "expiry_hours": 0})
		self.assertIn("between 1 and 168", message)

	def test_a_negative_expiry_is_refused_too(self):
		"""Already reachable before the fix, and asserted here so the `<= 0` half
		of the guard is covered on both sides of zero rather than only below it."""
		self.make()
		self.assertIn(
			"between 1 and 168",
			self.tool_error("generate_mobile_login_qr", {"user": WORKER, "expiry_hours": -1}),
		)

	def test_an_absent_expiry_still_takes_the_default_window(self):
		"""The other half of the fix: removing the `or` must not have removed the
		default. A card minted with no expiry_hours is still valid for the
		shipped window."""
		self.make()
		self.assertEqual(self.card()["expiry_hours"], mobile.DEFAULT_QR_HOURS)

	def test_it_says_the_image_is_a_live_credential(self):
		self.make()
		note = self.card()["security_note"]
		self.assertIn("LIVE CREDENTIAL", note)
		self.assertIn("group chat", note)

	def test_the_grant_records_the_card_without_recording_the_secret(self):
		self.make()
		data = self.card()
		row = STORE.get_raw("Mobile Access Grant", WORKER)
		self.assertTrue(row["last_qr_issued_on"])
		self.assertEqual(str(row["qr_expires_at"]), data["expires_at"])
		self.assertEqual(row["endpoint_url"], FUNNEL)
		self.assertNotIn(data["payload"]["api_secret"], json.dumps(row, default=str))

	def test_archiving_files_it_private_on_a_governance_document(self):
		self.make()
		data = self.card(archive=True, company=MAIN)
		archive = data["archive"]
		self.assertTrue(archive["archived"])
		self.assertTrue(archive["attachment"]["is_private"])
		self.assertTrue(frappe.db.exists("Governance Document", archive["governance_document"]))
		self.assertIn("Delete this document", archive["note"])

	def test_it_is_off_out_of_the_box(self):
		self.configure(enabled=1, public_url=FUNNEL)
		self.assertIn(
			"allow_generate_mobile_login_qr",
			self.tool_error("generate_mobile_login_qr", {"user": WORKER}),
		)


class Revocation(MobileTestCase):
	def test_it_disables_the_login_and_destroys_the_credential(self):
		self.make()
		data = self.tool_data("revoke_mobile_user", {"email": WORKER, "reason": "left at the end of harvest"})
		self.assertTrue(data["login_disabled"])
		self.assertTrue(data["token_revoked"])
		self.assertEqual(int(STORE.get_raw("User", WORKER)["enabled"]), 0)
		self.assertEqual(mobile.read_api_secret(WORKER), "")

	def test_the_reason_is_mandatory_and_has_to_be_a_real_one(self):
		self.make()
		for reason in ("", "x", "gone"):
			with self.subTest(reason=reason):
				message = self.tool_error("revoke_mobile_user", {"email": WORKER, "reason": reason})
				self.assertIn("reason is required", message)
				self.assertIn("Nothing was changed", message)

	def test_the_reason_survives_on_the_grant(self):
		"""Frappe keeps the access and none of the story. Six months later this is
		the only thing on the row that cannot be reconstructed."""
		self.make()
		self.tool_data("revoke_mobile_user", {"email": WORKER, "reason": "phone lost in the orchard"})
		row = STORE.get_raw("Mobile Access Grant", WORKER)
		self.assertEqual(row["state"], "Revoked")
		self.assertEqual(row["revocation_reason"], "phone lost in the orchard")
		self.assertTrue(row["revoked_on"])
		self.assertTrue(row["revoked_by"])

	def test_the_roles_stay_so_somebody_can_still_be_asked_what_they_could_see(self):
		self.make()
		data = self.tool_data("revoke_mobile_user", {"email": WORKER, "reason": "end of season"})
		self.assertEqual(data["roles_kept"], ["Field Worker"])
		self.assertIn("what this person was", data["note"])

	def test_the_entity_permissions_stay_by_default_and_go_when_asked(self):
		self.make(entity_access=[MAIN, OTHER])
		data = self.tool_data("revoke_mobile_user", {"email": WORKER, "reason": "end of season"})
		self.assertTrue(data["user_permissions_kept"])
		self.assertEqual(len(self.company_permissions()), 2)

		self.make(entity_access=[MAIN], update_existing=True)
		data = self.tool_data(
			"revoke_mobile_user",
			{"email": WORKER, "reason": "end of season", "keep_user_permissions": False},
		)
		self.assertEqual(data["user_permissions_removed"], [MAIN])
		self.assertIn("loses that same evidence", data["note"])

	def test_an_unknown_account_is_refused(self):
		self.assertIn(
			"nobody@example.test",
			self.tool_error(
				"revoke_mobile_user", {"email": "nobody@example.test", "reason": "never existed"}
			),
		)

	def test_it_is_off_out_of_the_box(self):
		self.configure(enabled=1, public_url=FUNNEL)
		self.assertIn(
			"allow_revoke_mobile_user",
			self.tool_error("revoke_mobile_user", {"email": WORKER, "reason": "end of season"}),
		)


class LosingThePhone(MobileTestCase):
	"""v0.93.0: `recover_mobile_access`, and what it refuses.

	  EVERY MECHANICAL PIECE ALREADY EXISTED — `revoke_api_token` says in its own
	  result that it is "the 'they lost their phone' one" — and a manager holding a
	  lost-phone report still had to do three things in the right order, keyed on a
	  login they usually do not have. A foreman knows a face and a badge.

	  THE THREE CLAIMS:

	IT REVOKES BEFORE IT MINTS. The lost handset is in somebody else's pocket
	while this call runs, and a failure after the revocation leaves the account
	with no credential — which is the safe side of the trade. Minting first
	would leave the old credential live for as long as the second step took.

	THE BADGE IS THE IDENTITY PROOF, AND A MISMATCH STOPS EVERYTHING. A card
	that resolves to somebody else is either the wrong card or the wrong
	person, and neither ends in a working credential. The absence of a badge is
	RECORDED rather than refused — somebody who lost the phone and the card is
	an ordinary Tuesday.

	THE EMPLOYEE RECORD IS NEVER TOUCHED. Recovering an account and hiring
	somebody twice are different acts, and only one of them puts a person on
	the dispatch board twice and in the payroll register once.
	"""

	REASON = "phone lost at Yellow Camp on 2026-08-18"

	def an_employee(self, name="HR-EMP-ANA", user=WORKER, employee_name="Ana Ramos"):
		STORE.seed(
			"Employee",
			[
				{
					"name": name,
					"employee_name": employee_name,
					"company": MAIN,
					"status": "Active",
					"date_of_joining": "2026-05-01",
					"user_id": user,
				}
			],
		)
		return name

	def a_badge(self, badge="QR-0001", employee="HR-EMP-ANA"):
		STORE.seed(
			"Bucket Log Badge Map",
			[{"name": badge, "badge_id": badge, "company": MAIN, "employee": employee, "active": 1}],
		)
		return badge

	def recover(self, **overrides):
		payload = {"reason": self.REASON}
		payload.update(overrides)
		return self.tool_data("recover_mobile_access", payload)

	def secret_of(self, user=WORKER):
		return mobile.read_api_secret(user)

	# ── the credential ─────────────────────────────────────────────────────
	def test_the_old_credential_stops_working_and_a_new_one_is_issued(self):
		self.make()
		before = self.secret_of()
		self.an_employee()
		data = self.recover(user=WORKER)
		self.assertTrue(data["previous_credential_revoked"])
		self.assertTrue(data["api_secret"])
		self.assertNotEqual(data["api_secret"], before)
		self.assertEqual(self.secret_of(), data["api_secret"])

	def test_an_account_that_held_no_credential_says_so_rather_than_claiming_a_revocation(self):
		self.make()
		self.tool_data("revoke_api_token", {"user": WORKER, "reason": "test"})
		self.an_employee()
		data = self.recover(user=WORKER)
		self.assertFalse(data["previous_credential_revoked"])
		self.assertIn("already logged out", data["old_device_note"])

	def test_the_revocation_happens_before_the_mint(self):
		"""THE ORDERING GUARANTEE, and the only way to see it is to make the mint
		fail. If the replacement were minted first, a failure here would leave the
		lost handset working — which is the whole thing this order prevents."""
		self.make()
		self.an_employee()
		self.assertTrue(self.secret_of())

		def explode(_args):
			raise RuntimeError("the mint failed after the revocation")

		original = mobile.generate_api_token
		mobile.generate_api_token = explode
		self.addCleanup(setattr, mobile, "generate_api_token", original)

		self.tool("recover_mobile_access", {"user": WORKER, "reason": self.REASON})
		self.assertEqual(self.secret_of(), "", "the lost phone still has a working credential")

	def test_a_bad_argument_does_not_destroy_a_working_credential(self):
		"""The other side of it. Nothing about a typo requires the revocation to
		have happened, so the arguments are checked before anything is killed."""
		self.make()
		self.an_employee()
		before = self.secret_of()
		error = self.tool_error(
			"recover_mobile_access", {"user": WORKER, "reason": self.REASON, "expiry_days": -1}
		)
		self.assertIn("Nothing was changed", error)
		self.assertEqual(self.secret_of(), before)

	def test_the_qr_is_returned_only_when_asked_for(self):
		self.make()
		self.an_employee()
		self.assertIsNone(self.recover(user=WORKER)["qr"])
		self.assertTrue(self.recover(user=WORKER, issue_qr=True)["qr"])

	# ── the badge ──────────────────────────────────────────────────────────
	def test_a_scanned_badge_finds_the_login_without_anybody_knowing_it(self):
		"""The argument the tool exists for: a foreman knows a card, not an email."""
		self.make()
		self.an_employee()
		self.a_badge()
		data = self.recover(badge="QR-0001")
		self.assertEqual(data["user"], WORKER)
		self.assertEqual(data["employee"], "HR-EMP-ANA")
		self.assertEqual(data["identity_verified_by"], "badge")
		self.assertEqual(data["badge"], "QR-0001")

	def test_a_badge_naming_somebody_else_stops_the_reset(self):
		"""Either the wrong card or the wrong person, and neither ends in a
		working credential."""
		self.make()
		self.an_employee()
		self.a_badge()
		before = self.secret_of()
		error = self.tool_error(
			"recover_mobile_access",
			{"badge": "QR-0001", "employee": "HR-EMP-SOMEBODY-ELSE", "reason": self.REASON},
		)
		self.assertIn("wrong card or the wrong person", error)
		self.assertEqual(self.secret_of(), before)

	def test_a_badge_against_the_wrong_login_stops_the_reset(self):
		self.make()
		self.an_employee()
		self.a_badge()
		error = self.tool_error(
			"recover_mobile_access",
			{"badge": "QR-0001", "user": "someone.else@example.test", "reason": self.REASON},
		)
		self.assertIn("whose login is not", error)

	def test_a_retired_badge_is_refused_by_the_register_that_owns_it(self):
		"""Delegated to resolve_badge, so a retired card, an unknown card and one
		belonging to somebody who has left stay three different refusals."""
		self.make()
		self.an_employee()
		STORE.seed(
			"Bucket Log Badge Map",
			[
				{
					"name": "QR-OLD",
					"badge_id": "QR-OLD",
					"company": MAIN,
					"employee": "HR-EMP-ANA",
					"active": 0,
				}
			],
		)
		error = self.tool_error("recover_mobile_access", {"badge": "QR-OLD", "reason": self.REASON})
		self.assertIn("retired", error)

	def test_no_badge_is_allowed_and_recorded_as_the_weaker_claim(self):
		"""Somebody who lost the phone AND the card is an ordinary Tuesday, and a
		recovery tool that could not serve it is one a farm routes around."""
		self.make()
		self.an_employee()
		data = self.recover(employee="HR-EMP-ANA")
		self.assertEqual(data["identity_verified_by"], "manager assertion")
		self.assertIsNone(data["badge"])
		self.assertIn("NO BADGE WAS PRESENTED", data["verification_note"])

	def test_the_verification_method_is_written_onto_the_grant(self):
		"""Not left to be inferred from an absent argument."""
		self.make()
		self.an_employee()
		self.a_badge()
		self.recover(badge="QR-0001")
		notes = str(frappe.db.get_value("Mobile Access Grant", WORKER, "notes") or "")
		self.assertIn("credential recovered (badge)", notes)
		self.assertIn(self.REASON, notes)

	# ── the person ─────────────────────────────────────────────────────────
	def test_the_employee_record_is_not_touched(self):
		self.make()
		employee = self.an_employee()
		self.a_badge()
		before = dict(STORE.get_raw("Employee", employee))
		count = len(STORE.rows("Employee"))
		data = self.recover(badge="QR-0001")
		self.assertEqual(data["employee"], employee)
		self.assertEqual(len(STORE.rows("Employee")), count)
		self.assertEqual(dict(STORE.get_raw("Employee", employee)), before)

	def test_somebody_with_no_login_is_refused_and_pointed_at_onboarding(self):
		"""The re-onboarding path, and it REUSES the Employee — which is the whole
		reason it is named here rather than create_employee."""
		self.an_employee("HR-EMP-BETO", user="", employee_name="Beto Cruz")
		error = self.tool_error("recover_mobile_access", {"employee": "HR-EMP-BETO", "reason": self.REASON})
		self.assertIn("onboard_employee(employee=...)", error)
		self.assertIn("REUSES", error)

	def test_a_disabled_login_is_refused_rather_than_quietly_re_enabled(self):
		"""Re-enabling somebody is a different decision from replacing a phone."""
		self.make()
		self.an_employee()
		frappe.db.set_value("User", WORKER, "enabled", 0)
		error = self.tool_error("recover_mobile_access", {"user": WORKER, "reason": self.REASON})
		self.assertIn("disabled", error)

	# ── the audit trail ────────────────────────────────────────────────────
	def test_a_reason_is_required_and_a_word_is_not_one(self):
		self.make()
		self.an_employee()
		self.assertIn("reason", self.tool_error("recover_mobile_access", {"user": WORKER}))
		error = self.tool_error("recover_mobile_access", {"user": WORKER, "reason": "lost"})
		self.assertIn("say what happened", error)

	def test_naming_nobody_is_refused_with_the_three_ways_to_name_somebody(self):
		error = self.tool_error("recover_mobile_access", {"reason": self.REASON})
		for option in ("badge", "employee", "user"):
			self.assertIn(option, error)

	def test_it_is_off_by_default_like_every_other_mutating_tool(self):
		self.configure(enabled=1, **READS_ON)
		self.assertIn(
			"allow_recover_mobile_access",
			self.tool_error("recover_mobile_access", {"user": WORKER, "reason": self.REASON}),
		)


class TheRoster(MobileTestCase):
	def roster(self, **arguments):
		return self.tool_data("list_mobile_users", arguments)

	def test_it_lists_the_accounts_with_their_live_entity_access(self):
		self.make(entity_access=[MAIN, OTHER])
		data = self.roster()
		self.assertEqual(data["count"], 1)
		entry = data["users"][0]
		self.assertEqual(entry["user"], WORKER)
		self.assertEqual(entry["role"], "Field Worker")
		self.assertEqual(sorted(entry["entity_access"]), sorted([MAIN, OTHER]))
		self.assertTrue(entry["has_live_token"])

	def test_it_returns_the_role_catalogue_so_a_client_needs_no_second_call(self):
		data = self.roster()
		self.assertEqual([entry["role"] for entry in data["roles"]], list(roles.ROLE_NAMES))
		self.assertTrue(all(entry["cannot"] for entry in data["roles"]))

	def test_drift_between_the_grant_and_the_live_permissions_is_reported(self):
		"""Somebody changed one without the other, in the Desk. The list reads the
		LIVE rows, so this shows as drift rather than agreeing with a stale record."""
		self.make(entity_access=[MAIN])
		row = self.company_permissions()[0]
		frappe.delete_doc("User Permission", row["name"], force=True)
		concerns = self.roster()["users"][0]["concerns"]
		self.assertTrue(any("NO User Permission on Company" in line for line in concerns))

	def test_a_revoked_account_whose_token_still_works_is_flagged(self):
		self.make()
		frappe.db.set_value("Mobile Access Grant", WORKER, "state", "Revoked")
		concerns = self.roster(include_revoked=True)["users"][0]["concerns"]
		self.assertTrue(any("REVOKED BUT THE TOKEN STILL WORKS" in line for line in concerns))

	def test_an_overdue_review_date_is_flagged_as_a_reminder_and_not_an_expiry(self):
		self.make()
		frappe.db.set_value("Mobile Access Grant", WORKER, "token_expires_on", "2020-01-01")
		entry = self.roster()["users"][0]
		self.assertTrue(entry["token_review_overdue"])
		self.assertTrue(any("do not expire on their own" in line for line in entry["concerns"]))

	def test_a_grant_whose_role_is_not_on_the_account_is_flagged(self):
		self.make()
		user = frappe.get_doc("User", WORKER)
		user.roles = [row for row in user.roles if row.get("role") != "Field Worker"]
		user.save()
		concerns = self.roster()["users"][0]["concerns"]
		self.assertTrue(any("does not hold that role" in line for line in concerns))

	def test_revoked_accounts_are_history_and_not_roster(self):
		self.make()
		self.tool_data("revoke_mobile_user", {"email": WORKER, "reason": "end of season"})
		self.assertEqual(self.roster()["count"], 0)
		self.assertEqual(self.roster(include_revoked=True)["count"], 1)

	def test_it_can_be_narrowed_to_one_entity_and_one_role(self):
		self.make(entity_access=[MAIN])
		self.make(
			email="fran@example.test",
			full_name="Fran F",
			role="Foreman",
			entity_access=[OTHER],
		)
		self.assertEqual(self.roster(company=OTHER)["count"], 1)
		self.assertEqual(self.roster(role="Field Worker")["count"], 1)
		self.assertEqual(self.roster(role="Foreman")["users"][0]["user"], "fran@example.test")

	def test_an_unknown_role_filter_is_refused_with_the_six(self):
		self.assertIn("Supervisor", self.tool_error("list_mobile_users", {"role": "Supervisor"}))

	def test_it_is_on_out_of_the_box(self):
		self.configure(enabled=1, public_url=FUNNEL)
		self.assertFalse(self.tool("list_mobile_users").get("isError"))


class TheEndpointUrl(MobileTestCase):
	def test_the_operators_public_url_wins_over_the_sites_own(self):
		"""`frappe.utils.get_url()` is correct for the server and useless to a
		phone: a site behind a Funnel has no way of knowing its public name from
		inside a request."""
		self.assertEqual(mobile._endpoint_url({}), FUNNEL)

	def test_an_explicit_url_wins_over_both(self):
		self.assertEqual(mobile._endpoint_url({"url": "https://other.ts.net/"}), "https://other.ts.net")

	def test_with_nothing_configured_it_falls_back_to_the_site(self):
		self.configure(enabled=1, public_url="", **ALL_ON)
		self.assertEqual(mobile._endpoint_url({}), str(frappe.utils.get_url()).rstrip("/"))
		self.assertEqual(settings.public_url(), "")
