# SPDX-License-Identifier: MIT
"""v0.129.0 — the four reads about the deployment, and what each refuses.

These tools answer questions about the SERVER rather than about the orchard, and
three of the four are unremarkable. The fourth is not, and most of this file is
about it.

1. **THE STATUS IS ONE WORKER'S AND SAYS SO.** `TheStatusIsOneWorkers`. A bench
   runs several worker processes and this call is answered by one of them, so an
   uptime is that process's age. The tool reports the version it actually
   imported and the frappe version it actually read, rather than either being a
   string somebody typed.

2. **A TRACEBACK'S ANSWER IS ITS LAST LINE.** `TheExceptionIsNamed`. Python
   prints the stack first and the exception last, so the truncated head a feed
   necessarily shows is the half without the answer in it. Both are returned.

3. **A TRACEBACK IS THE REPR OF WHATEVER WAS IN SCOPE.** `TheTracebackIsRedacted`.
   On a failed request that includes the request body, which on this app's own
   transports carries a token. The negative control is in the test: the
   unredacted field really does contain the secret the answer does not.

4. **`query_doctype` ASKS THE FRAMEWORK.** `TheGenericReadAsksPermission`. Every
   other read here calls `frappe.db.get_all`, which skips the permission check.
   This one calls `frappe.get_list`, which does not — and the test proves it by
   denying the permission and requiring the refusal, which is the only way to
   tell the two calls apart from outside.

5. **A CREDENTIAL IS NOT READABLE THROUGH IT.** `TheCredentialStoresAreRefused`.
   Password fields on any doctype, and a short register of doctypes refused
   outright because their tokens are plain Data columns. `order_by` is matched by
   shape because Frappe interpolates it into SQL.

6. **THE ROUTE TABLE IS NOT AN ACCESS MAP.** `TheRouteTableIsHonest`. It reports
   what exists and what writes. Who may call each route is a line in that
   route's own wrapper body, and the answer says so rather than shipping a gate
   column that would read as "open" wherever it was silent.
"""

import unittest
from typing import ClassVar

import frappe

from erpnext_mcp import __version__, registry
from erpnext_mcp.errors import ToolError
from erpnext_mcp.farmops_api import routes as sidecar_routes
from erpnext_mcp.tools import diagnostics

from .fixtures import MAIN, V12TestCase
from .harness import STORE, add_field

ERROR_LOG = "Error Log"
PATCH_LOG = "Patch Log"

#: A traceback shaped like the ones Frappe files, with a credential in the frame
#: repr — which is what a failed request on this app's own transports leaves in
#: scope. THE SECRET IS THE POINT OF THE FIXTURE: `TheTracebackIsRedacted`
#: asserts both that the answer does not carry it and that this string does.
SECRET = "s3cr3t-tok3n-abcdef0123456789"
TRACEBACK = (
	"Traceback (most recent call last):\n"
	'  File "/home/frappe/apps/erpnext_mcp/erpnext_mcp/api/mobile.py", line 41, in wrapper\n'
	f"    result = function(user, **{{'auth_token': '{SECRET}', 'task': 'TASK-0001'}})\n"
	'  File "/home/frappe/apps/erpnext_mcp/erpnext_mcp/tools/fieldwork.py", line 88, in get_task\n'
	"    return ToolResult(data=row)\n"
	"frappe.exceptions.ValidationError: no Farm Task called 'TASK-0001'"
)

#: Three rows and how old each one is, in minutes. STAMPED RELATIVE TO THE
#: HARNESS CLOCK AT `setUp` RATHER THAN HARDCODED, and that is not tidiness: the
#: double's `now` is a base instant plus one second per call, so it has already
#: advanced by however many rows the fixture site seeded before a test runs.
#: Hardcoded stamps put the rows minutes or hours from a "now" nobody can
#: predict — the first version of this file dated them AFTER it, where every
#: backwards window matched all three and the window test passed proving nothing.
LOGS = (
	{
		"name": "ERR-0001",
		"age_minutes": 30,
		"method": "erpnext_mcp.api.mobile.get_task",
		"error": TRACEBACK,
		"reference_doctype": "Farm Task",
		"reference_name": "TASK-0001",
		"seen": 0,
	},
	{
		"name": "ERR-0002",
		"age_minutes": 60 * 24 * 4,
		"method": "erpnext_mcp.api.mobile.sync_bucket_entries",
		"error": "Traceback (most recent call last):\n  ...\nKeyError: 'bucket_count'",
		"seen": 1,
	},
	{
		"name": "ERR-0003",
		"age_minutes": 60 * 24 * 60,
		"method": "erpnext_mcp.api.mobile.get_task",
		"error": "Traceback (most recent call last):\n  ...\nTimeoutError: weather provider",
		"seen": 0,
	},
)


class DiagnosticsTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(
			enabled=1,
			allow_get_server_status=1,
			allow_list_error_logs=1,
			allow_query_doctype=1,
			allow_list_sidecar_routes=1,
		)
		self.clock = frappe.utils.now()
		STORE.seed(
			ERROR_LOG,
			[
				{
					**{k: v for k, v in row.items() if k != "age_minutes"},
					"creation": self.ago(row["age_minutes"]),
				}
				for row in LOGS
			],
		)
		STORE.seed(
			PATCH_LOG,
			[
				{
					"name": "PL-0001",
					"patch": "erpnext_mcp.patches.v0_105_0",
					"creation": self.ago(60 * 24 * 30),
				},
				{"name": "PL-0002", "patch": "erpnext_mcp.patches.v0_128_0", "creation": self.ago(60 * 7)},
			],
		)

	def ago(self, minutes: int) -> str:
		"""A stamp that many minutes before the clock this test started on."""
		return frappe.utils.add_to_date(self.clock, minutes=-minutes, as_string=True, as_datetime=True)

	def status(self, **args) -> dict:
		return diagnostics.get_server_status(args).data

	def errors(self, **args) -> dict:
		return diagnostics.list_error_logs(args).data

	def query(self, **args) -> dict:
		return diagnostics.query_doctype(args).data

	def routes(self, **args) -> dict:
		return diagnostics.list_sidecar_routes(args).data


# ── 1. the status is one worker's ───────────────────────────────────────────
class TheStatusIsOneWorkers(DiagnosticsTestCase):
	def test_the_version_is_the_one_this_process_imported(self):
		"""NOT A STRING IN THE ANSWER. The whole use of this tool is telling a
		deployed worker from the one it replaced, so the number has to come off
		the module rather than out of a literal beside it."""
		self.assertEqual(self.status()["erpnext_mcp_version"], __version__)

	def test_the_frappe_version_is_read_off_the_module(self):
		self.assertEqual(self.status()["frappe_version"], frappe.__version__)

	def test_the_python_version_is_this_interpreter(self):
		import platform

		self.assertEqual(self.status()["python_version"], platform.python_version())
		self.assertIn(platform.python_version(), self.status()["python_build"])

	def test_the_uptime_is_a_number_and_the_answer_says_whose(self):
		status = self.status()
		self.assertIsInstance(status["worker_uptime_seconds"], float)
		self.assertGreaterEqual(status["worker_uptime_seconds"], 0)
		self.assertIn("THIS PROCESS, NOT THE BENCH", " ".join(status["notes"]))

	def test_the_last_patch_is_the_newest_one_and_not_the_first(self):
		self.assertEqual(self.status()["last_patch_applied"]["patch"], "erpnext_mcp.patches.v0_128_0")
		self.assertEqual(self.status()["last_patch_applied"]["applied_at"], self.ago(60 * 7))

	def test_the_answer_says_a_patch_stamp_is_not_a_migrate_stamp(self):
		"""A release that adds only tools writes no patch, so an old stamp here
		is not evidence a deploy failed — and a reader who assumed otherwise
		would go looking for a problem that is not there."""
		self.assertIn("NOT THE LAST MIGRATE", " ".join(self.status()["notes"]).upper())

	def test_a_site_with_no_patch_log_rows_says_so_rather_than_answering_null(self):
		STORE.tables[PATCH_LOG] = {}
		status = self.status()
		self.assertIsNone(status["last_patch_applied"])
		self.assertIn("last migrated", " ".join(status["notes"]))

	def test_the_installed_apps_carry_this_apps_own_version(self):
		self.assertEqual(self.status()["installed_apps"]["erpnext_mcp"], __version__)

	def test_it_takes_no_arguments(self):
		self.assertEqual(registry.TOOLS["get_server_status"]["inputSchema"]["properties"], {})
		self.assertEqual(diagnostics.STATUS_ARGUMENTS, ())


# ── 2. a traceback's answer is its last line ────────────────────────────────
class TheExceptionIsNamed(DiagnosticsTestCase):
	def test_the_exception_line_is_the_end_of_the_traceback(self):
		row = self.errors(limit=1)["error_logs"][0]
		self.assertEqual(
			row["exception"], "frappe.exceptions.ValidationError: no Farm Task called 'TASK-0001'"
		)

	def test_the_head_alone_would_not_have_carried_it(self):
		"""THE NEGATIVE CONTROL for the claim above. Python prints the stack
		first, so a preview that takes the first N characters — which is what a
		truncated feed necessarily is — cuts the answer off. Proved by shortening
		the preview to a length the exception cannot be inside."""
		row = self.errors(limit=1)["error_logs"][0]
		head = row["error"][:80]
		self.assertNotIn("ValidationError", head)
		self.assertIn("ValidationError", row["exception"])

	def test_a_long_traceback_is_marked_as_truncated_with_its_real_length(self):
		STORE.tables[ERROR_LOG]["ERR-0001"]["error"] = "x" * 5000
		row = self.errors(limit=1)["error_logs"][0]
		self.assertTrue(row["error_truncated"])
		self.assertEqual(row["error_length"], 5000)
		self.assertEqual(len(row["error"]), diagnostics.ERROR_PREVIEW)

	def test_the_newest_row_is_first(self):
		self.assertEqual(
			[row["name"] for row in self.errors()["error_logs"]],
			["ERR-0001", "ERR-0002", "ERR-0003"],
		)

	def test_the_fixture_is_behind_the_clock_the_windows_count_back_from(self):
		"""THE GUARD THAT KEEPS THE THREE TESTS BELOW FROM GOING VACUOUS. A row
		stamped after `now` is matched by every backwards window there is, so the
		window tests would pass while proving nothing about the sign — which is
		exactly what they did on this file's first run, against hardcoded stamps
		a month ahead of the double's clock."""
		stored = {row["name"]: row["creation"] for row in STORE.rows(ERROR_LOG)}
		for row in LOGS:
			with self.subTest(row=row["name"]):
				self.assertLess(stored[row["name"]], frappe.utils.now())

	def test_the_window_counts_backwards_from_now(self):
		"""`minutes` and `hours` add. One row is half an hour old, one is four
		days old and one is nearly two months old, so the three windows below
		select one, two and three of them — which is what proves the sign is
		right rather than merely that a filter was applied."""
		self.assertEqual(self.errors(hours=1)["count"], 1)
		self.assertEqual(self.errors(hours=24 * 7)["count"], 2)
		self.assertEqual(self.errors(hours=24 * 365)["count"], 3)

	def test_minutes_and_hours_add_rather_than_one_winning(self):
		"""31 minutes reaches the half-hour-old row; 30 does not; and an hour
		expressed as `minutes=30, hours=1` reaches further than either alone."""
		self.assertEqual(self.errors(minutes=31)["count"], 1)
		self.assertEqual(self.errors(minutes=29)["count"], 0)
		self.assertEqual(self.errors(minutes=30, hours=1)["count"], 1)

	def test_an_explicit_since_wins_over_the_relative_window(self):
		"""`hours=1` alone reaches one row. The floor below reaches two, and the
		answer reports which of the two the tool actually used."""
		floor = self.ago(60 * 24 * 5)
		result = self.errors(since=floor, hours=1)
		self.assertEqual(result["since"], floor)
		self.assertEqual(result["count"], 2)

	def test_a_zero_or_negative_window_is_refused_rather_than_ignored(self):
		with self.assertRaises(ToolError) as caught:
			self.errors(minutes=0)
		self.assertIn("selects nothing", str(caught.exception))

	def test_method_matches_as_a_substring(self):
		"""A caller has a method name off a traceback, not off Frappe's own
		dotted path. An exact match answering empty reads as 'nothing failed'."""
		self.assertEqual(self.errors(method="get_task")["count"], 2)
		self.assertEqual(self.errors(method="erpnext_mcp.api.mobile.get_task")["count"], 2)

	def test_contains_searches_the_traceback_itself(self):
		self.assertEqual(self.errors(contains="TimeoutError")["count"], 1)
		self.assertEqual(self.errors(contains="KeyError")["count"], 1)

	def test_seen_narrows_to_what_nobody_has_opened(self):
		self.assertEqual(self.errors(seen=False)["count"], 2)
		self.assertEqual(self.errors(seen=True)["count"], 1)

	def test_the_commonest_method_is_counted(self):
		self.assertEqual(self.errors()["by_method"]["erpnext_mcp.api.mobile.get_task"], 2)

	def test_an_empty_register_is_distinguished_from_a_narrow_filter(self):
		result = self.errors(contains="nothing matches this")
		self.assertEqual(result["count"], 0)
		self.assertIn("GOOD ANSWER", result["empty_note"])


# ── 3. a traceback is the repr of whatever was in scope ─────────────────────
class TheTracebackIsRedacted(DiagnosticsTestCase):
	def test_the_token_in_the_frame_repr_does_not_reach_the_caller(self):
		row = self.errors(limit=1)["error_logs"][0]
		self.assertNotIn(SECRET, row["error"])
		self.assertIn("<redacted>", row["error"])

	def test_the_stored_row_really_did_contain_it(self):
		"""THE NEGATIVE CONTROL. Without this the test above passes on a fixture
		that never had a secret in it, which is the failure mode a redaction test
		is most likely to have."""
		self.assertIn(SECRET, STORE.tables[ERROR_LOG]["ERR-0001"]["error"])
		self.assertIn(SECRET, TRACEBACK)

	def test_the_count_of_what_was_replaced_is_reported(self):
		result = self.errors(limit=1)
		self.assertGreaterEqual(result["redacted_values"], 1)
		self.assertIn("credential-shaped", " ".join(result["notes"]))

	def test_a_clean_traceback_reports_no_redactions_at_all(self):
		"""A counter that always fired would tell a reader nothing."""
		result = self.errors(method="sync_bucket_entries")
		self.assertNotIn("redacted_values", result)

	def test_the_search_still_matches_against_the_unredacted_text(self):
		"""Redaction happens on the way OUT. Filtering on the stored column is
		what makes `contains` able to find an error by what it actually said."""
		self.assertEqual(self.errors(contains="ValidationError")["count"], 1)


# ── 4. the generic read asks the framework ──────────────────────────────────
class TheGenericReadAsksPermission(DiagnosticsTestCase):
	def test_it_reads_an_ordinary_register(self):
		result = self.query(doctype="Company", fields=["name"], order_by="name asc")
		self.assertIn(MAIN, [row["name"] for row in result["records"]])
		self.assertEqual(result["order_by"], "name asc")

	def test_a_denied_permission_refuses_the_read(self):
		"""THE CLAIM THAT SEPARATES THIS TOOL FROM EVERY OTHER READ HERE.
		`frappe.db.get_all` skips the permission check and `frappe.get_list`
		applies it, and from outside the only way to tell which one a tool called
		is to deny the permission and see whether it notices."""
		STORE.denied_permissions.add(("Company", "read"))
		with self.assertRaises(ToolError) as caught:
			self.query(doctype="Company", fields=["name"])
		message = str(caught.exception)
		self.assertIn("may not read Company", message)
		self.assertIn("mcp_system_user", message)

	def test_the_negative_control_a_db_get_all_would_have_answered_anyway(self):
		"""The test above is only worth making if the call every other tool uses
		really does ignore the denial — otherwise it would pass against either
		implementation and prove nothing about which one is in the source."""
		STORE.denied_permissions.add(("Company", "read"))
		self.assertTrue(frappe.db.get_all("Company", fields=["name"], limit=1))

	def test_filters_narrow_the_records(self):
		self.assertEqual(self.query(doctype="Company", filters={"name": MAIN}, fields=["name"])["count"], 1)
		self.assertEqual(
			self.query(doctype="Company", filters={"name": "No Such Co"}, fields=["name"])["count"], 0
		)

	def test_an_operator_pair_is_accepted_as_a_filter_value(self):
		result = self.query(
			doctype=ERROR_LOG,
			filters={"creation": [">", self.ago(60 * 24 * 10)]},
			fields=["name"],
			order_by="name asc",
		)
		self.assertEqual([row["name"] for row in result["records"]], ["ERR-0001", "ERR-0002"])

	def test_a_string_filter_is_refused_with_the_shape_it_wanted(self):
		with self.assertRaises(ToolError) as caught:
			self.query(doctype="Company", filters="name=Example")
		self.assertIn('{"status": "Active"}', str(caught.exception))

	def test_an_unknown_doctype_is_refused_with_how_to_spell_one(self):
		with self.assertRaises(ToolError) as caught:
			self.query(doctype="farm_task")
		self.assertIn("case-sensitive", str(caught.exception))

	def test_an_unknown_field_is_refused_rather_than_becoming_a_sql_error(self):
		with self.assertRaises(ToolError) as caught:
			self.query(doctype="Company", fields=["name", "no_such_column"])
		self.assertIn("no_such_column", str(caught.exception))

	def test_the_default_selection_is_just_the_docname(self):
		self.assertEqual(self.query(doctype="Company")["fields"], ["name"])

	def test_a_comma_separated_string_is_accepted_as_a_field_list(self):
		self.assertEqual(self.query(doctype="Company", fields="name,abbr")["fields"], ["name", "abbr"])

	def test_select_everything_is_refused(self):
		with self.assertRaises(ToolError) as caught:
			self.query(doctype="Company", fields=["*"])
		self.assertIn("Name the columns", str(caught.exception))

	def test_the_limit_is_capped_and_truncation_is_reported(self):
		result = self.query(doctype=ERROR_LOG, fields=["name"], limit=1)
		self.assertEqual(result["count"], 1)
		self.assertTrue(result["truncated"])
		self.assertIn("Narrow with `filters`", result["truncated_note"])


# ── 5. a credential is not readable through it ──────────────────────────────
class TheCredentialStoresAreRefused(DiagnosticsTestCase):
	def test_every_named_store_is_refused_whatever_the_permissions_say(self):
		for doctype in diagnostics.REFUSED_DOCTYPES:
			with self.subTest(doctype=doctype):
				with self.assertRaises(ToolError) as caught:
					self.query(doctype=doctype, fields=["name"])
				message = str(caught.exception)
				self.assertIn("not readable through query_doctype", message)
				self.assertIn("cannot be switched on", message)

	def test_this_endpoints_own_token_store_is_one_of_them(self):
		"""The settings Single holds `auth_token`. A generic reader that could
		reach it would be handing out the credential that gates the reader."""
		self.assertIn("ERPNext MCP Settings", diagnostics.REFUSED_DOCTYPES)
		self.assertIn("User", diagnostics.REFUSED_DOCTYPES)

	def test_every_refusal_names_a_reason_rather_than_just_refusing(self):
		for doctype, why in diagnostics.REFUSED_DOCTYPES.items():
			with self.subTest(doctype=doctype):
				self.assertGreater(len(why), 30, f"{doctype} is refused without an argument")

	def test_a_password_field_named_explicitly_is_refused_not_dropped(self):
		"""Refused, because a caller who asked for a column and got nothing back
		reads the column as empty — which about a password is the wrong thing to
		come away believing."""
		add_field("Company", "portal_password", "Password", label="Portal Password")
		with self.assertRaises(ToolError) as caught:
			self.query(doctype="Company", fields=["name", "portal_password"])
		self.assertIn("Password field", str(caught.exception))

	def test_the_negative_control_the_same_field_as_data_is_returned(self):
		"""The refusal above must be about the FIELDTYPE and not about the name.
		A tool that matched on 'password' in the fieldname would pass the test
		above while missing a Password field called `secret_answer`."""
		add_field("Company", "portal_password", "Data", label="Portal Password")
		result = self.query(doctype="Company", fields=["name", "portal_password"])
		self.assertIn("portal_password", result["fields"])

	def test_an_order_by_that_is_not_one_column_is_refused_by_shape(self):
		"""Frappe interpolates order_by into SQL rather than parameterising it."""
		for attempt in (
			"name; DROP TABLE tabCompany",
			"name desc, creation desc",
			"(select 1)",
			"name --",
		):
			with self.subTest(attempt=attempt), self.assertRaises(ToolError) as caught:
				self.query(doctype="Company", order_by=attempt)
			self.assertIn("one column and an optional direction", str(caught.exception))

	def test_an_order_by_naming_a_column_that_does_not_exist_is_refused(self):
		with self.assertRaises(ToolError) as caught:
			self.query(doctype="Company", order_by="no_such_column desc")
		self.assertIn("no field called", str(caught.exception))

	def test_the_shapes_order_by_does_take(self):
		self.assertEqual(self.query(doctype="Company", order_by="name")["order_by"], "name asc")
		self.assertEqual(self.query(doctype="Company", order_by="name DESC")["order_by"], "name desc")


# ── 6. the route table is not an access map ─────────────────────────────────
class TheRouteTableIsHonest(DiagnosticsTestCase):
	def test_every_mounted_route_is_reported(self):
		result = self.routes()
		self.assertEqual(result["count"], len(sidecar_routes.ROUTES))
		self.assertEqual(result["total_routes"], len(sidecar_routes.ROUTES))

	def test_a_path_carries_the_prefix_a_caller_would_post_to(self):
		paths = {entry["path"] for entry in self.routes()["routes"]}
		self.assertIn(f"{sidecar_routes.PREFIX}/mobile/get_task", paths)

	def test_the_answer_refuses_to_imply_it_knows_who_may_call_what(self):
		"""THE MISSING COLUMN IS THE IMPORTANT ONE. A route absent from a gate
		column would read as open, and the gate is a line in each wrapper's own
		body that nothing here can see."""
		result = self.routes()
		self.assertIn("NOT AN ACCESS MAP", " ".join(result["notes"]))
		for entry in result["routes"]:
			self.assertNotIn("gate", entry)
			self.assertNotIn("roles", entry)

	def test_mutating_is_read_off_the_wrapper_rather_than_restated(self):
		by_path = {entry["path"]: entry for entry in self.routes()["routes"]}
		for route in sidecar_routes.ROUTES:
			with self.subTest(path=route.path):
				self.assertEqual(
					by_path[f"{sidecar_routes.PREFIX}{route.path}"]["mutating"], bool(route.mutating)
				)

	def test_arguments_are_the_transports_own_filter(self):
		"""Not documentation of the filter — the same call the transport makes,
		so a key absent here is unreachable rather than merely unlisted."""
		entry = next(e for e in self.routes()["routes"] if e["path"].endswith("/mobile/get_task"))
		self.assertEqual(
			set(entry["arguments"]),
			sidecar_routes.accepted_arguments(
				next(r.handler for r in sidecar_routes.ROUTES if r.path == "/mobile/get_task")
			),
		)

	def test_user_is_never_an_argument_on_any_route(self):
		"""The guard injects the authenticated caller and drops any body copy."""
		for entry in self.routes()["routes"]:
			with self.subTest(path=entry["path"]):
				self.assertNotIn("user", entry["arguments"])

	def test_the_filters_narrow_the_table(self):
		self.assertTrue(self.routes(contains="i9")["count"] >= 1)
		for entry in self.routes(contains="i9")["routes"]:
			self.assertIn("i9", entry["path"].lower())

		writes = self.routes(mutating=True)
		self.assertTrue(all(entry["mutating"] for entry in writes["routes"]))
		self.assertEqual(writes["count"], writes["mutating_count"])

		reads = self.routes(mutating=False)
		self.assertTrue(not any(entry["mutating"] for entry in reads["routes"]))
		self.assertEqual(reads["count"] + writes["count"], len(sidecar_routes.ROUTES))

	def test_the_groups_are_counted_whether_or_not_they_are_filtered_to(self):
		"""`by_group` is the whole surface even under a filter, so a caller who
		narrowed to nothing can see what there was to narrow to."""
		result = self.routes(group="files")
		self.assertTrue(all(entry["group"] == "files" for entry in result["routes"]))
		self.assertIn("mobile", result["by_group"])
		self.assertEqual(sum(result["by_group"].values()), len(sidecar_routes.ROUTES))

	def test_a_filter_matching_nothing_says_what_there_was(self):
		result = self.routes(contains="no-such-route")
		self.assertEqual(result["count"], 0)
		self.assertIn("in total", result["empty_note"])


# ── the registry, and what each tool advertises ─────────────────────────────
class TheToolsAreRegistered(unittest.TestCase):
	NAMES: ClassVar[tuple] = (
		"get_server_status",
		"list_error_logs",
		"query_doctype",
		"list_sidecar_routes",
	)

	ARGUMENT_TABLES: ClassVar[dict] = {
		"get_server_status": diagnostics.STATUS_ARGUMENTS,
		"list_error_logs": diagnostics.ERROR_LOG_ARGUMENTS,
		"query_doctype": diagnostics.QUERY_ARGUMENTS,
		"list_sidecar_routes": diagnostics.ROUTE_ARGUMENTS,
	}

	def test_all_four_are_read_only(self):
		for name in self.NAMES:
			with self.subTest(name=name):
				self.assertIn(name, registry.TOOLS)
				self.assertFalse(registry.TOOLS[name]["mutating"])
				self.assertTrue(registry.TOOLS[name]["annotations"]["readOnlyHint"])

	def test_the_schema_declares_exactly_what_each_handler_reads(self):
		"""`additionalProperties` is advertised on every schema in this app and
		enforced by nothing, so an argument the schema omits is not refused — it
		is ignored, and no caller ever learns it exists. Both directions."""
		for name, table in self.ARGUMENT_TABLES.items():
			with self.subTest(name=name):
				self.assertEqual(set(registry.TOOLS[name]["inputSchema"]["properties"]), set(table))

	def test_query_doctype_says_in_its_own_description_what_it_can_reach(self):
		"""An operator reading the catalogue must not have to infer that this one
		tool is bounded by DocPerms rather than by its switch."""
		description = registry.TOOLS["query_doctype"]["description"]
		self.assertIn("frappe.get_list", description)
		self.assertIn("mcp_system_user", description)
		self.assertIn("password", description.lower())

	def test_list_sidecar_routes_says_it_is_not_an_access_map(self):
		self.assertIn("NOT AN ACCESS MAP", registry.TOOLS["list_sidecar_routes"]["description"])

	def test_list_error_logs_declares_the_doctype_it_needs(self):
		self.assertEqual(registry.TOOLS["list_error_logs"]["requires"][:7], "Frappe'")
		self.assertFalse(registry.TOOLS["get_server_status"]["requires"])


if __name__ == "__main__":
	unittest.main()
