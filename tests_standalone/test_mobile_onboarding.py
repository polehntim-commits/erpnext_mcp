# SPDX-License-Identifier: MIT
"""The enrolment page — item 20. `/app/mobile-onboarding` and what stands behind it.

The tools have made mobile accounts since v0.17.0 and there has never been a
place for a person to make one from. This is that place, and the whole risk in
adding it is that a Desk surface quietly becomes a SECOND IMPLEMENTATION with a
weaker set of gates than the tools it wraps. Every claim below is about that.

SEVEN CLAIMS.

1. `ThePreFlight` — `enrolment_blockers` is a pure function over two facts, and
   the create path asks it BEFORE it writes anything. A site with no HTTPS
   endpoint gets a refusal and NO half-made account: the failure mode this
   guards is a worker whose login exists and who has nothing to scan.
2. `ThePermissionGate` — the gate is Frappe's own permission tables and not a
   role name in the source. Read is `Mobile Access Grant` read; enrolling is
   create on the grant AND create on `User`, so the page can never make an
   account the caller could not have made on the User form.
3. `TheCard` — the printable card is pure, escapes what somebody typed, carries
   the name AND the email, prints the expiry and the credential warning, and
   still prints when the symbol is missing.
4. `TheWrapper` — the two tools do the work, a `ToolError` reaches the operator
   as a sentence rather than an HTTP 500, and the api_secret never appears in
   the JSON the browser gets.
5. `OneRotationAndNotTwo` — a new account's card prints the credential the
   account was created with. Minting a second one a millisecond later would
   hand over a secret that was already dead on paper.
6. `TheHalfSuccess` — a card that fails to draw AFTER the account exists comes
   back as a made account plus a reason, not as an error. Reporting it as a
   failure invites a second press and "this user already exists".
7. `ThePageOnDisk` — the Page record, the script and the template agree with
   each other and with the module, and every method the script calls exists and
   is whitelisted.
"""

import json
import pathlib
import unittest

import frappe

from erpnext_mcp import mobile_onboarding, roles
from erpnext_mcp.errors import ToolError
from erpnext_mcp.render import qr
from erpnext_mcp.tools import mobile

from .fixtures import MAIN, OTHER, SeededTestCase
from .harness import STORE

FUNNEL = "https://umbrel.tail4a2b.ts.net"
WORKER = "ana@example.test"

PAGE_DIR = (
	pathlib.Path(__file__).resolve().parent.parent
	/ "erpnext_mcp"
	/ "erpnext_mcp"
	/ "page"
	/ "mobile_onboarding"
)

GRANT = "Mobile Access Grant"


def _card(**overrides) -> dict:
	base = {
		"user": WORKER,
		"full_name": "Ana Ramos",
		"role": "Field Worker",
		"entity_access": [MAIN],
		"preferred_company": MAIN,
		"employee": None,
		"endpoint": f"{FUNNEL}/farmops/api/mobile/get_current_user_context",
		"expires_at": "2026-08-20 09:14:00",
		"png_base64": "aGVsbG8=",
		"qr_error": None,
	}
	base.update(overrides)
	return base


class OnboardingTestCase(SeededTestCase):
	def setUp(self):
		super().setUp()
		# The page calls the tool FUNCTIONS directly, the way `badge_sheet` and
		# `asset_tag_sheet` do, so no `allow_<tool>` switch is in play here — the
		# session gate is the only gate this surface adds. `public_url` is what
		# the pre-flight reads.
		self.configure(enabled=1, public_url=FUNNEL)
		roles.install_roles()

	def enrol(self, **overrides):
		payload = {
			"full_name": "Ana Ramos",
			"email": WORKER,
			"role": "Field Worker",
			"companies": [MAIN],
		}
		payload.update(overrides)
		return mobile_onboarding.create_and_enrol(**payload)

	def deny(self, doctype, ptype):
		STORE.denied_permissions.add((doctype, ptype))
		self.addCleanup(STORE.denied_permissions.discard, (doctype, ptype))


# ── 1 ─────────────────────────────────────────────────────────────────────────
class ThePreFlight(unittest.TestCase):
	"""The pure half. No site, no session — which is the point of it being pure:
	the same list drives the banner at page load and the refusal at submit."""

	def test_a_ready_site_has_no_blockers(self):
		self.assertEqual(mobile_onboarding.enrolment_blockers(FUNNEL, True), [])

	def test_no_encoder_is_named_with_the_pip_command(self):
		found = mobile_onboarding.enrolment_blockers(FUNNEL, False)
		self.assertEqual([entry["code"] for entry in found], ["NO_QR_ENCODER"])
		self.assertEqual(found[0]["fix"], qr.REQUIRES)

	def test_an_empty_public_url_is_its_own_code(self):
		""" "Nothing configured" and "configured wrong" are different sentences
		and different things to go and do, so they are different codes."""
		found = mobile_onboarding.enrolment_blockers("", True)
		self.assertEqual([entry["code"] for entry in found], ["NO_PUBLIC_URL"])
		self.assertIn("ERPNext MCP Settings", found[0]["fix"])

	def test_a_plaintext_endpoint_is_refused_and_quoted_back(self):
		found = mobile_onboarding.enrolment_blockers("http://192.168.1.9", True)
		self.assertEqual([entry["code"] for entry in found], ["ENDPOINT_NOT_HTTPS"])
		self.assertIn("http://192.168.1.9", found[0]["message"])

	def test_both_are_reported_at_once(self):
		"""A pre-flight that stopped at the first problem would make somebody fix
		the site twice."""
		found = mobile_onboarding.enrolment_blockers("", False)
		self.assertEqual({entry["code"] for entry in found}, {"NO_QR_ENCODER", "NO_PUBLIC_URL"})


class ThePreFlightRunsFirst(OnboardingTestCase):
	def test_a_plaintext_site_creates_nothing_at_all(self):
		"""THE FAILURE THIS EXISTS TO PREVENT. Run in tool order the account would
		be made and the card would then fail, leaving a worker at the desk with a
		login and nothing to scan."""
		self.configure(enabled=1, public_url="http://192.168.1.9")
		with self.assertRaises(frappe.ValidationError) as caught:
			self.enrol()
		self.assertIn("No account was created", str(caught.exception))
		self.assertFalse(frappe.db.exists("User", WORKER))
		self.assertFalse(frappe.db.exists(GRANT, WORKER))

	def test_the_context_reports_the_same_blockers_the_submit_would(self):
		"""One pure function drives both, so the banner cannot promise a form
		that the submit then refuses."""
		self.configure(enabled=1, public_url="http://192.168.1.9")
		context = mobile_onboarding.onboarding_context()
		self.assertEqual([entry["code"] for entry in context["blockers"]], ["ENDPOINT_NOT_HTTPS"])
		self.assertFalse(context["may_enrol"] and not context["blockers"])
		with self.assertRaises(frappe.ValidationError) as caught:
			self.enrol()
		self.assertIn(context["blockers"][0]["message"], str(caught.exception))

	def test_an_empty_public_url_falls_back_to_what_the_server_knows(self):
		"""`_endpoint_url` answers with `frappe.utils.get_url()` when the settings
		field is empty, so "nothing configured" is only a blocker on a site that
		does not know its own address either — which is why the pre-flight reads
		the RESOLVED url rather than the field."""
		self.configure(enabled=1, public_url="")
		self.assertEqual(mobile_onboarding.onboarding_context()["blockers"], [])

	def test_the_context_carries_the_roles_and_the_entities(self):
		context = mobile_onboarding.onboarding_context()
		self.assertEqual([entry["role"] for entry in context["roles"]], list(roles.ROLE_NAMES))
		self.assertIn(MAIN, context["companies"])
		self.assertIn(OTHER, context["companies"])
		self.assertEqual(context["default_expiry_hours"], mobile.DEFAULT_QR_HOURS)

	def test_the_form_ceiling_is_the_one_the_tool_refuses_at(self):
		"""The number box stops where the tool would have said no. A mirror that
		disagreed would offer somebody a value and then reject it."""
		self.assertEqual(mobile_onboarding.MAX_EXPIRY_HOURS, 168)
		with self.assertRaises(ToolError):
			mobile.generate_mobile_login_qr({"user": WORKER, "expiry_hours": 169})


# ── 2 ─────────────────────────────────────────────────────────────────────────
class ThePermissionGate(OnboardingTestCase):
	def test_a_full_session_may_do_both(self):
		state = mobile_onboarding.permission_state()
		self.assertTrue(state["may_read"])
		self.assertTrue(state["may_enrol"])
		self.assertEqual(state["missing"], [])

	def test_enrolment_needs_create_on_this_apps_own_register(self):
		self.deny(GRANT, "create")
		state = mobile_onboarding.permission_state()
		self.assertFalse(state["may_enrol"])
		self.assertIn(GRANT, state["note"])
		with self.assertRaises(frappe.ValidationError):
			self.enrol()

	def test_enrolment_also_needs_create_on_frappes_user(self):
		"""THE HALF THAT MAKES WIDENING THE OTHER ONE SAFE. However far an
		operator opens the grant's permission table, this page can still only
		make an account the caller could have made on the User form."""
		self.deny("User", "create")
		self.assertFalse(mobile_onboarding.permission_state()["may_enrol"])
		with self.assertRaises(frappe.ValidationError):
			self.enrol()
		self.assertFalse(frappe.db.exists("User", WORKER))

	def test_the_roster_is_gated_on_read_and_not_on_create(self):
		"""Somebody who may look at who is enrolled is not thereby somebody who
		may enrol, and the page renders both answers rather than one."""
		self.deny(GRANT, "create")
		self.assertEqual(mobile_onboarding.mobile_users()["count"], 0)
		self.deny(GRANT, "read")
		with self.assertRaises(frappe.ValidationError):
			mobile_onboarding.mobile_users()

	def test_the_context_answers_rather_than_throwing(self):
		"""The page has to render the sentence. A context that threw would leave
		a blank panel and a console nobody opens."""
		self.deny(GRANT, "create")
		context = mobile_onboarding.onboarding_context()
		self.assertFalse(context["may_enrol"])
		self.assertIn("permission", context["permission_note"])

	def test_the_page_record_names_no_role(self):
		"""A standard Page is rewritten from this app's JSON at every migrate, so
		a role list stored there is a decision an operator makes and loses."""
		record = json.loads((PAGE_DIR / "mobile_onboarding.json").read_text(encoding="utf-8"))
		self.assertEqual(record["roles"], [])


# ── 3 ─────────────────────────────────────────────────────────────────────────
class TheCard(unittest.TestCase):
	def test_the_name_and_the_email_are_both_on_it(self):
		"""Six of these on a desk on a Monday morning, two of them called Maria.
		The email is what tells them apart, and scanning the wrong one scopes
		somebody to the wrong entity."""
		markup = mobile_onboarding.card_html(_card())
		self.assertIn("Ana Ramos", markup)
		self.assertIn(WORKER, markup)

	def test_it_says_what_it_is_and_when_it_stops_working(self):
		markup = mobile_onboarding.card_html(_card())
		self.assertIn("2026-08-20 09:14:00", markup)
		self.assertIn("live credential", markup)

	def test_it_carries_the_symbol_as_a_data_uri(self):
		self.assertIn("data:image/png;base64,aGVsbG8=", mobile_onboarding.card_html(_card()))

	def test_a_card_with_no_symbol_still_prints_and_says_why(self):
		"""The failure that matters is the one where somebody collects the paper
		and does not notice what is missing."""
		markup = mobile_onboarding.card_html(_card(png_base64="", qr_error="no encoder here"))
		self.assertIn("No QR code was drawn", markup)
		self.assertIn("no encoder here", markup)
		self.assertNotIn("data:image/png", markup)

	def test_everything_a_person_typed_is_escaped(self):
		markup = mobile_onboarding.card_html(_card(full_name='<script>alert("x")</script>'))
		self.assertNotIn("<script>", markup)
		self.assertIn("&lt;script&gt;", markup)

	def test_the_document_is_whole_and_prints_itself(self):
		document = mobile_onboarding.card_document(_card())
		self.assertTrue(document.startswith("<!doctype html>"))
		self.assertIn("window.print()", document)
		self.assertIn("mo-card", document)


# ── 4 ─────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(qr.available(), "needs a QR encoder (segno or qrcode)")
class TheWrapper(OnboardingTestCase):
	def test_it_makes_the_account_the_scoping_and_the_card(self):
		answer = self.enrol()
		self.assertTrue(answer["created"])
		self.assertEqual(answer["role"], "Field Worker")
		self.assertEqual(answer["entity_access"], [MAIN])
		self.assertEqual(roles.companies_for(WORKER), [MAIN])
		self.assertTrue(answer["qr"]["png_base64"])
		self.assertIsNone(answer["qr_error"])
		self.assertIn("mo-card", answer["card_html"])

	def test_the_secret_is_not_in_what_the_browser_gets(self):
		"""The PNG already carries it. Putting it in the JSON as well would put a
		live credential in the network log and in devtools to no end — nobody
		types this one in."""
		blob = json.dumps(self.enrol())
		secret = mobile.read_api_secret(WORKER)
		self.assertTrue(secret, "the account should have been given a credential")
		self.assertNotIn(secret, blob)
		self.assertNotIn("api_secret", blob)

	def test_the_companies_survive_the_json_round_trip_frappe_call_makes(self):
		"""`frappe.call` posts a JS array as a JSON string, so the front door has
		to read one."""
		answer = self.enrol(companies=json.dumps([MAIN, OTHER]))
		self.assertEqual(answer["entity_access"], [MAIN, OTHER])

	def test_a_tool_refusal_reaches_the_operator_as_a_sentence(self):
		"""Raised out of a whitelisted method a ToolError is an HTTP 500 and the
		sentence naming what to fix never reaches anybody."""
		with self.assertRaises(frappe.ValidationError) as caught:
			self.enrol(companies=["Nowhere Farms"])
		self.assertIn("Nowhere Farms", str(caught.exception))
		self.assertFalse(frappe.db.exists("User", WORKER))

	def test_enrolling_the_same_person_twice_is_refused_on_purpose(self):
		self.enrol()
		with self.assertRaises(frappe.ValidationError) as caught:
			self.enrol()
		self.assertIn("update_existing", str(caught.exception))

	def test_regenerate_issues_a_second_card_and_rotates(self):
		self.enrol()
		answer = mobile_onboarding.regenerate_qr(user=WORKER)
		self.assertTrue(answer["qr"]["png_base64"])
		self.assertTrue(answer["qr"]["token_rotated"])
		self.assertIn("Ana Ramos", answer["card_html"])

	def test_regenerate_refuses_an_account_that_is_not_there(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_onboarding.regenerate_qr(user="nobody@example.test")
		self.assertIn("nobody@example.test", str(caught.exception))


# ── 5 ─────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(qr.available(), "needs a QR encoder (segno or qrcode)")
class OneRotationAndNotTwo(OnboardingTestCase):
	def test_a_new_account_prints_the_credential_it_was_created_with(self):
		"""`create_mobile_user` mints one for a new account. Rotating again a
		millisecond later would issue two secrets to hand over one, and the first
		would be dead before it reached paper."""
		answer = self.enrol()
		self.assertFalse(answer["qr"]["token_rotated"])
		self.assertEqual(int(STORE.get_raw(GRANT, WORKER)["token_issue_count"]), 1)

	def test_a_re_scoped_account_gets_a_fresh_one(self):
		"""An account that already existed kept its credential by design — the
		phone in somebody's pocket must not go offline because they were moved to
		another block. So the card it prints needs a new one."""
		self.enrol()
		answer = self.enrol(companies=[MAIN, OTHER], update_existing=1)
		self.assertTrue(answer["updated"])
		self.assertTrue(answer["qr"]["token_rotated"])
		self.assertEqual(int(STORE.get_raw(GRANT, WORKER)["token_issue_count"]), 2)


# ── 6 ─────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(qr.available(), "needs a QR encoder (segno or qrcode)")
class TheHalfSuccess(OnboardingTestCase):
	def test_a_card_that_fails_after_the_account_exists_is_not_an_error(self):
		"""Reporting this as a failure invites a second press of the button and
		"this user already exists" — which is a worse place to be than the one
		the operator is actually in."""
		original = mobile.generate_mobile_login_qr

		def refuse(_args):
			raise ToolError("the encoder went away between the check and the draw")

		mobile.generate_mobile_login_qr = refuse
		self.addCleanup(setattr, mobile, "generate_mobile_login_qr", original)

		answer = self.enrol()
		self.assertTrue(answer["created"])
		self.assertTrue(frappe.db.exists("User", WORKER))
		self.assertIsNone(answer["qr"])
		self.assertIn("went away", answer["qr_error"])

	def test_the_printable_card_says_so_where_the_symbol_would_have_been(self):
		original = mobile.generate_mobile_login_qr

		def refuse(_args):
			raise ToolError("no encoder on this bench")

		mobile.generate_mobile_login_qr = refuse
		self.addCleanup(setattr, mobile, "generate_mobile_login_qr", original)

		answer = self.enrol()
		self.assertIn("No QR code was drawn", answer["card_html"])
		self.assertIn("no encoder on this bench", answer["card_html"])


# ── 7 ─────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(qr.available(), "needs a QR encoder (segno or qrcode)")
class TheRoster(OnboardingTestCase):
	def test_it_reports_status_last_activity_and_the_entities(self):
		self.enrol()
		STORE.get_raw(GRANT, WORKER)["last_seen_on"] = "2026-08-18 06:40:00"
		row = mobile_onboarding.mobile_users()["users"][0]
		self.assertEqual(row["user"], WORKER)
		self.assertEqual(row["state"], "Active")
		self.assertEqual(row["entity_access"], [MAIN])
		self.assertEqual(row["last_seen_on"], "2026-08-18 06:40:00")
		self.assertTrue(row["has_live_token"])

	def test_last_seen_on_comes_through_the_tool_and_not_around_it(self):
		"""It is the column `sweep_idle_grants` acts on, and until this release
		`list_mobile_users` was the one place that did not report it — so "why
		did that phone stop working" had no answer on the roster."""
		self.enrol()
		STORE.get_raw(GRANT, WORKER)["last_seen_on"] = "2026-08-18 06:40:00"
		data = mobile.list_mobile_users({}).data
		self.assertEqual(data["users"][0]["last_seen_on"], "2026-08-18 06:40:00")

	def test_the_drift_the_tool_finds_reaches_the_page(self):
		"""The rows worth a manager's attention are the ones with a concern on
		them, and a wrapper that dropped them would make the page look calmer
		than the site is."""
		self.enrol()
		for row in list(STORE.rows("User Permission")):
			if row.get("user") == WORKER:
				STORE.tables["User Permission"].pop(row["name"], None)
		row = mobile_onboarding.mobile_users()["users"][0]
		self.assertTrue(row["concerns"])
		self.assertTrue(any("EVERY entity" in line for line in row["concerns"]))
		self.assertEqual(mobile_onboarding.mobile_users()["needing_attention"], 1)

	def test_the_company_filter_is_passed_through(self):
		self.enrol()
		self.assertEqual(mobile_onboarding.mobile_users(company=MAIN)["count"], 1)
		self.assertEqual(mobile_onboarding.mobile_users(company=OTHER)["count"], 0)


# ── 8 ─────────────────────────────────────────────────────────────────────────
class ThePageOnDisk(unittest.TestCase):
	"""A Page is three files that have to agree, and nothing at runtime notices
	when they stop agreeing — Frappe renders an empty panel and moves on."""

	METHODS = ("onboarding_context", "create_and_enrol", "regenerate_qr", "mobile_users")

	def record(self) -> dict:
		return json.loads((PAGE_DIR / "mobile_onboarding.json").read_text(encoding="utf-8"))

	def script(self) -> str:
		return (PAGE_DIR / "mobile_onboarding.js").read_text(encoding="utf-8")

	def template(self) -> str:
		return (PAGE_DIR / "mobile_onboarding.html").read_text(encoding="utf-8")

	def test_the_record_is_a_standard_page_in_this_apps_module(self):
		record = self.record()
		self.assertEqual(record["doctype"], "Page")
		self.assertEqual(record["standard"], "Yes")
		self.assertEqual(record["module"], "ERPNext MCP")

	def test_the_route_is_the_one_the_module_and_the_script_both_name(self):
		"""Frappe keys `frappe.pages` on the Page's own name. A script that keyed
		on anything else would define a handler nothing ever calls."""
		self.assertEqual(self.record()["name"], mobile_onboarding.PAGE_ROUTE)
		self.assertEqual(self.record()["page_name"], mobile_onboarding.PAGE_ROUTE)
		self.assertIn(f'frappe.pages["{mobile_onboarding.PAGE_ROUTE}"]', self.script())

	def test_the_folder_is_the_scrubbed_route(self):
		"""`Page.load_assets` looks for `<module path>/page/<scrub(name)>/`, and a
		folder named anything else means the script and the template are simply
		never loaded."""
		self.assertEqual(PAGE_DIR.name, mobile_onboarding.PAGE_ROUTE.replace("-", "_"))
		for suffix in (".js", ".html", ".json"):
			with self.subTest(suffix=suffix):
				self.assertTrue((PAGE_DIR / f"{PAGE_DIR.name}{suffix}").is_file())

	def test_the_script_carries_the_licence_header_on_its_first_line(self):
		"""CI walks `git ls-files '*.js'` and reads the first three lines. One
		missing header fails the whole standalone job on both Python versions."""
		self.assertEqual(self.script().split("\n", 1)[0], "// SPDX-License-Identifier: MIT")

	def test_every_method_the_script_calls_exists_and_is_whitelisted(self):
		"""A button pointed at a method that is not whitelisted answers 403 and
		nothing on the page says so."""
		script = self.script()
		for name in self.METHODS:
			with self.subTest(method=name):
				self.assertIn(f'METHOD + "{name}"', script)
				function = getattr(mobile_onboarding, name)
				self.assertTrue(getattr(function, "__wrapped_whitelisted__", False))

	def test_the_script_names_the_module_it_calls_into_once(self):
		self.assertIn('const METHOD = "erpnext_mcp.mobile_onboarding.";', self.script())

	def test_the_template_opens_on_its_own_root_element(self):
		"""`$(frappe.render_template(...))` turns a leading comment into a node of
		its own, so a template that opened with one would hand back a collection
		and every `.find()` in the script would miss."""
		first = next(line for line in self.template().splitlines() if line.strip())
		self.assertTrue(first.startswith('<div class="mo-wrap"'), first)

	def test_the_template_holds_no_straight_apostrophe(self):
		"""Frappe compiles a page template into
		`frappe.templates["..."] = '...';` — a SINGLE-quoted JS string — and the
		only thing between a straight apostrophe and a syntax error that takes the
		whole page script down is one `str.replace` in `scrub_html_template`. The
		entity costs nothing and does not depend on which Frappe is installed."""
		self.assertNotIn("'", self.template())

	def test_the_template_carries_every_hook_the_script_reaches_for(self):
		"""The two files are wired by class name and nothing checks it at
		runtime: a renamed class is a button that silently does nothing."""
		template = self.template()
		for hook in (
			"mo-blockers",
			"mo-role",
			"mo-role-note",
			"mo-entities",
			"mo-form",
			"mo-submit",
			"mo-card-panel",
			"mo-card-result",
			"mo-card-preview",
			"mo-card-img",
			"mo-card-expiry",
			"mo-print",
			"mo-clear",
			"mo-roster-body",
			"mo-roster-summary",
			"mo-filter-company",
			"mo-filter-revoked",
			"mo-refresh",
		):
			with self.subTest(hook=hook):
				self.assertIn(hook, template)
				self.assertIn(hook, self.script())

	def test_the_template_names_the_id_the_script_reads(self):
		template = self.template()
		script = self.script()
		for field in ("mo-full-name", "mo-email", "mo-hours", "mo-notes", "mo-update-existing"):
			with self.subTest(field=field):
				self.assertIn(f'id="{field}"', template)
				self.assertIn(f"#{field}", script)
