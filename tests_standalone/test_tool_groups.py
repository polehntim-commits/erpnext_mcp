# SPDX-License-Identifier: MIT
"""The tool-permission console: domains, presets, counts, and what they touch.

WHAT IS ACTUALLY AT RISK HERE, because it is not the arithmetic. Three things:

1. A SECTION THAT NOBODY FILED. `SECTION_DOMAIN` is the one hand-written table
   in `tool_groups.py`, and the failure mode of a hand-written table over a
   generated form is that next year's release adds a section, nobody adds a row
   here, and forty tools quietly stop appearing under any domain chip. They are
   still on the form and still switchable, so nothing looks broken — the console
   just silently under-reports. `EveryToolIsFiled` is the check that makes that
   impossible to ship.

2. A PRESET THAT TOUCHES SOMETHING IT SHOULD NOT. `apply_profile` writes seven
   hundred and fifty-seven fields on the one document that also holds the master
   switch, the bearer token, the network allowlist and the attribution user. A
   bug that cleared any of those would take a site's endpoint down, or — worse —
   turn it on. `APresetTouchesOnlyToolSwitches` asserts every one of them by
   name, before and after.

3. A BADGE THAT NAMES THE WRONG ROLE. Not in this file — see
   `test_role_indicator.py`.

THE NEGATIVE CONTROLS ARE THE POINT. `test_the_minimal_preset_really_turns_the
_read_tools_off` exists because "read tools default on" means a preset that did
nothing at all would still leave a read-only site looking exactly like a
correctly applied read-only preset. Green has to be evidence.
"""

import json

from erpnext_mcp import registry, settings, tool_groups

from .fixtures import SeededTestCase
from .harness import DOCTYPE_DIR, STORE, _load_app_doctype, frappe

#: The fields on this form that are NOT tool switches, and that a preset must
#: never write. Named individually rather than derived, because the whole value
#: of the assertion is that somebody has to look at this list and agree.
NON_TOOL_FIELDS = (
	"enabled",
	"auth_token",
	"allowed_cidrs",
	"public_url",
	"require_user_context",
	"mcp_system_user",
	"farm_ops_mobile_enabled",
	"mobile_grant_idle_days",
	"drift_report_email",
	"trade_document_enforcement",
	"enable_kpi_history_sweep",
	"allow_reconciliation_packet",
	"allow_fiscal_year_audit_packet",
)


def settings_json() -> dict:
	return _load_app_doctype("erpnext_mcp_settings")


def sections_holding_a_tool() -> set:
	"""Every Section Break of the settings form under which a tool switch sits."""
	payload = settings_json()
	by_name = {field["fieldname"]: field for field in payload["fields"]}
	found, section = set(), ""
	for fieldname in payload["field_order"]:
		field = by_name[fieldname]
		if field["fieldtype"] == "Section Break":
			section = fieldname
		elif fieldname.startswith("allow_") and fieldname[len("allow_") :] in registry.TOOLS:
			found.add(section)
	return found


class EveryToolIsFiled(SeededTestCase):
	"""The totality check. See the module docstring — this is the one that stops
	a later release adding a section and losing forty tools out of the console."""

	def test_every_section_holding_a_tool_has_a_domain(self):
		missing = sorted(sections_holding_a_tool() - set(tool_groups.SECTION_DOMAIN))
		self.assertEqual(
			missing,
			[],
			"these sections of the settings form hold tool switches and are not in "
			f"tool_groups.SECTION_DOMAIN, so their tools appear under no domain: {missing}",
		)

	def test_every_mapped_section_is_a_real_section(self):
		"""The other direction: a typo, or a section a later release deleted."""
		by_name = {field["fieldname"]: field for field in settings_json()["fields"]}
		for fieldname in tool_groups.SECTION_DOMAIN:
			with self.subTest(section=fieldname):
				self.assertEqual(
					(by_name.get(fieldname) or {}).get("fieldtype"),
					"Section Break",
					f"{fieldname} is mapped to a domain and is not a section on this form",
				)

	def test_every_mapped_domain_exists(self):
		for fieldname, key in tool_groups.SECTION_DOMAIN.items():
			with self.subTest(section=fieldname):
				self.assertIn(key, tool_groups.DOMAIN_BY_KEY)

	def test_every_tool_resolves_to_a_domain(self):
		homeless = sorted(name for name in registry.TOOLS if not tool_groups.domain_of(name))
		self.assertEqual(homeless, [], f"tools with no domain: {homeless}")

	def test_every_domain_holds_at_least_one_tool(self):
		"""An empty domain is a chip an operator clicks and gets nothing from."""
		for domain in tool_groups.DOMAINS:
			with self.subTest(domain=domain.key):
				self.assertTrue(tool_groups.tools_in_domain(domain.key))

	def test_the_domains_partition_the_catalogue(self):
		counted = sum(len(tool_groups.tools_in_domain(d.key)) for d in tool_groups.DOMAINS)
		self.assertEqual(counted, len(registry.TOOLS))

	def test_a_switch_that_is_not_a_tool_is_still_filed(self):
		"""The packet types carry an `allow_` switch and are not tools. They are
		on the form, so the console has to be able to file them — a domain chip
		that hid them would hide part of the form for no visible reason."""
		self.assertEqual(tool_groups.domain_of("reconciliation_packet"), "compliance")
		self.assertEqual(
			set(tool_groups.non_tool_switches()), {"reconciliation_packet", "fiscal_year_audit_packet"}
		)

	def test_a_name_this_form_does_not_carry_has_no_domain(self):
		self.assertEqual(tool_groups.domain_of("tool_from_a_future_version"), "")

	def test_the_packet_types_are_not_counted_as_tools(self):
		""" "412 of 757 tools" has to mean tools, which is why there are two maps
		rather than one union."""
		self.assertEqual(tool_groups.summary()["total"], len(registry.TOOLS))
		self.assertNotIn("reconciliation_packet", tool_groups.console()["tools"])


class TheDomainsAreDescribed(SeededTestCase):
	def test_the_keys_are_unique(self):
		keys = [domain.key for domain in tool_groups.DOMAINS]
		self.assertEqual(len(keys), len(set(keys)))

	def test_each_one_says_what_it_is(self):
		"""The description is the chip's tooltip, and it is the only explanation
		an operator gets before narrowing the form to it."""
		for domain in tool_groups.DOMAINS:
			with self.subTest(domain=domain.key):
				self.assertTrue(domain.label)
				self.assertGreater(len(domain.description), 40)


class TheProfilesAreCoherent(SeededTestCase):
	def test_the_keys_are_unique(self):
		keys = [profile.key for profile in tool_groups.PROFILES]
		self.assertEqual(len(keys), len(set(keys)))

	def test_every_named_domain_is_real(self):
		for profile in tool_groups.PROFILES:
			for key in profile.reads + profile.writes:
				with self.subTest(profile=profile.key, domain=key):
					self.assertIn(key, tool_groups.DOMAIN_BY_KEY)

	def test_a_profile_never_writes_what_it_cannot_read(self):
		"""A configuration nobody meant, and the shape a typo in the table takes."""
		for profile in tool_groups.PROFILES:
			with self.subTest(profile=profile.key):
				self.assertEqual(set(profile.writes) - set(profile.reads), set())

	def test_each_one_says_what_it_is_for(self):
		for profile in tool_groups.PROFILES:
			with self.subTest(profile=profile.key):
				self.assertTrue(profile.label)
				self.assertGreater(len(profile.summary), 60)

	def test_write_domains_stay_rare(self):
		""" "Mutating tools ship off" is a promise, and a preset is the easiest
		place to lose it by accident. Asserted as a bound rather than as an exact
		list so a new profile is free — but a release that gave four profiles
		write access has to come and change this number and say why."""
		with_writes = [profile.key for profile in tool_groups.PROFILES if profile.writes]
		self.assertLessEqual(len(with_writes), 2, f"profiles that enable write tools: {with_writes}")


class ThePlanIsHonest(SeededTestCase):
	def test_the_minimal_preset_disables_the_whole_catalogue(self):
		plan = tool_groups.plan_profile("minimal")
		self.assertEqual(plan["will_be_enabled"], 0)
		self.assertEqual(plan["write_tools_enabled"], [])

	def test_the_read_only_preset_enables_every_read_tool_and_no_write_tool(self):
		plan = tool_groups.plan_profile("read_only")
		self.assertEqual(plan["will_be_enabled"], len(registry.READ_TOOLS))
		self.assertEqual(plan["write_tools_enabled"], [])

	def test_a_narrow_preset_is_narrower_than_a_wide_one(self):
		field = tool_groups.plan_profile("field_worker")["will_be_enabled"]
		manager = tool_groups.plan_profile("farm_manager")["will_be_enabled"]
		self.assertLess(field, manager)

	def test_the_farm_manager_preset_enables_farm_write_tools(self):
		"""The one profile pair that carries a write domain, asserted in both
		directions: it turns farm writes on and leaves the ledger's alone."""
		plan = tool_groups.plan_profile("farm_manager")
		enabled_writes = set(plan["write_tools_enabled"])
		self.assertTrue(enabled_writes)
		for name in enabled_writes:
			with self.subTest(tool=name):
				self.assertEqual(tool_groups.domain_of(name), "farm")
				self.assertTrue(registry.TOOLS[name]["mutating"])

	def test_an_unknown_profile_is_refused_by_name(self):
		with self.assertRaises(Exception) as caught:
			tool_groups.plan_profile("chief_executive")
		self.assertIn("farm_manager", str(caught.exception))

	def test_planning_changes_nothing(self):
		before = dict(STORE.singles["ERPNext MCP Settings"])
		tool_groups.plan_profile("minimal")
		self.assertEqual(STORE.singles["ERPNext MCP Settings"], before)


class APresetTouchesOnlyToolSwitches(SeededTestCase):
	def apply(self, key: str) -> dict:
		return tool_groups.apply_profile(key)

	def test_the_minimal_preset_really_turns_the_read_tools_off(self):
		"""THE NEGATIVE CONTROL. Read tools ship ON, so a preset that did nothing
		at all would leave a site looking exactly like one where "Nothing
		Enabled" had been applied correctly. This is what tells the two apart."""
		self.assertTrue(settings.tool_enabled("get_company_topology"))
		self.apply("minimal")
		self.assertFalse(settings.tool_enabled("get_company_topology"))
		self.assertEqual(tool_groups.summary()["enabled"], 0)

	def test_a_preset_switches_its_own_domain_on_and_the_others_off(self):
		self.apply("field_worker")
		farm_read = next(
			name for name in tool_groups.tools_in_domain("farm") if not registry.TOOLS[name]["mutating"]
		)
		accounting_read = next(
			name for name in tool_groups.tools_in_domain("accounting") if not registry.TOOLS[name]["mutating"]
		)
		self.assertTrue(settings.tool_enabled(farm_read))
		self.assertFalse(settings.tool_enabled(accounting_read))

	def test_it_leaves_every_other_field_on_the_form_exactly_as_it_was(self):
		"""The master switch, the token, the allowlist, the attribution user and
		the two packet types. This document is the whole security surface of the
		endpoint and a preset answers one question about it."""
		before = {key: STORE.singles["ERPNext MCP Settings"].get(key) for key in NON_TOOL_FIELDS}
		self.apply("minimal")
		after = {key: STORE.singles["ERPNext MCP Settings"].get(key) for key in NON_TOOL_FIELDS}
		self.assertEqual(after, before)

	def test_a_preset_cannot_turn_the_endpoint_on(self):
		self.configure(enabled=0)
		self.apply("read_only")
		self.assertFalse(settings.is_enabled())

	def test_applying_one_preset_replaces_the_last_rather_than_adding_to_it(self):
		"""A preset that only ever added would be a ratchet: three clicks and an
		operator has the union of three profiles and no idea what is live."""
		self.apply("bookkeeper")
		self.apply("field_worker")
		accounting_read = next(
			name for name in tool_groups.tools_in_domain("accounting") if not registry.TOOLS[name]["mutating"]
		)
		self.assertFalse(settings.tool_enabled(accounting_read))

	def test_a_dry_run_reports_and_writes_nothing(self):
		before = dict(STORE.singles["ERPNext MCP Settings"])
		plan = tool_groups.apply_profile("minimal", dry_run=1)
		self.assertNotIn("applied", plan)
		self.assertEqual(STORE.singles["ERPNext MCP Settings"], before)

	def test_a_dry_run_flag_of_string_zero_is_not_a_dry_run(self):
		""" "0" arrives from the browser as a string, and Python calls it true.
		`settings.as_bool` is what stops a real apply being swallowed as a
		preview — the same bug the settings module exists to prevent."""
		result = tool_groups.apply_profile("minimal", dry_run="0")
		self.assertTrue(result.get("applied"))

	def test_only_a_system_manager_may_apply_one(self):
		frappe.local.session.user = "nobody@example.test"
		with self.assertRaises(Exception):
			tool_groups.apply_profile("minimal")

	def test_only_a_system_manager_may_read_the_console(self):
		frappe.local.session.user = "nobody@example.test"
		with self.assertRaises(Exception):
			tool_groups.console()


class TheSummaryCounts(SeededTestCase):
	def test_a_fresh_install_is_read_only_and_says_so(self):
		summary = tool_groups.summary()
		self.assertEqual(summary["total"], len(registry.TOOLS))
		self.assertEqual(
			summary["enabled"], len(registry.READ_TOOLS) + len(registry.DEFAULT_ON_MUTATING_TOOLS)
		)
		self.assertEqual(summary["writes_enabled"], len(registry.DEFAULT_ON_MUTATING_TOOLS))

	def test_the_domain_rows_add_up_to_the_totals(self):
		summary = tool_groups.summary()
		self.assertEqual(sum(row["total"] for row in summary["domains"]), summary["total"])
		self.assertEqual(sum(row["enabled"] for row in summary["domains"]), summary["enabled"])
		self.assertEqual(sum(row["writes_total"] for row in summary["domains"]), summary["writes_total"])

	def test_turning_one_switch_off_moves_one_count(self):
		before = tool_groups.summary()["enabled"]
		self.configure(allow_get_company_topology=0)
		self.assertEqual(tool_groups.summary()["enabled"], before - 1)


class TheConsolePayload(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.payload = tool_groups.console()

	def test_it_carries_every_tool(self):
		self.assertEqual(set(self.payload["tools"]), set(registry.TOOLS))

	def test_every_entry_says_its_domain_and_whether_it_writes(self):
		for name, entry in self.payload["tools"].items():
			with self.subTest(tool=name):
				self.assertIn(entry["domain"], tool_groups.DOMAIN_BY_KEY)
				self.assertEqual(entry["mutating"], bool(registry.TOOLS[name]["mutating"]))

	def test_it_carries_the_domains_and_the_profiles(self):
		self.assertEqual(
			[row["key"] for row in self.payload["domains"]],
			[domain.key for domain in tool_groups.DOMAINS],
		)
		self.assertEqual(
			[row["key"] for row in self.payload["profiles"]],
			[profile.key for profile in tool_groups.PROFILES],
		)

	def test_a_profile_reports_its_domains_in_words(self):
		"""The dialog says "Farm Operations", not "farm". Resolved on the server
		so the browser holds no copy of the domain table."""
		manager = next(row for row in self.payload["profiles"] if row["key"] == "farm_manager")
		self.assertIn("Farm Operations", manager["read_domain_labels"])
		self.assertIn("Farm Operations", manager["write_domain_labels"])

	def test_it_survives_a_json_round_trip(self):
		"""It goes to a browser. A value `frappe.as_json` cannot render is a
		console that renders nothing at all."""
		self.assertTrue(json.loads(json.dumps(self.payload, default=str)))


class TheFormHasSomewhereToDrawIt(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.payload = settings_json()
		self.order = self.payload["field_order"]

	def test_the_console_field_exists(self):
		by_name = {field["fieldname"]: field for field in self.payload["fields"]}
		self.assertEqual(by_name["tool_console_html"]["fieldtype"], "HTML")
		self.assertEqual(by_name["tool_console_section"]["fieldtype"], "Section Break")

	def test_it_sits_above_the_first_tool_section(self):
		"""A summary an operator has to scroll past seven hundred checkboxes to
		read is not a summary."""
		self.assertLess(self.order.index("tool_console_html"), self.order.index("read_tools_section"))

	def test_it_sits_below_the_connection_settings(self):
		"""The endpoint's own configuration comes first: whether the thing is on
		outranks which tools it offers."""
		self.assertGreater(self.order.index("tool_console_section"), self.order.index("attribution_section"))

	def test_the_console_field_is_not_a_switch(self):
		"""`allow_*` is load-bearing: `test_settings` fails the build over an
		`allow_` field with no tool behind it."""
		self.assertFalse("tool_console_html".startswith("allow_"))


class TheFormJSAsksTheServer(SeededTestCase):
	"""The same rule `SettingsFormJS` in `test_settings.py` applies to the
	write-tool banner, pointed at the console: a catalogue transcribed into
	JavaScript is a catalogue that goes stale, and this form's whole job is to
	tell the truth about what an AI client can reach."""

	def js(self) -> str:
		import os

		path = os.path.join(DOCTYPE_DIR, "erpnext_mcp_settings", "erpnext_mcp_settings.js")
		with open(path) as handle:
			return handle.read()

	def test_it_fetches_the_grouping_from_the_server(self):
		self.assertIn("erpnext_mcp.tool_groups.console", self.js())

	def test_it_applies_presets_on_the_server(self):
		self.assertIn("erpnext_mcp.tool_groups.apply_profile", self.js())

	def test_it_hardcodes_no_domain_or_profile(self):
		body = self.js()
		keys = [domain.key for domain in tool_groups.DOMAINS] + [p.key for p in tool_groups.PROFILES]
		hardcoded = [key for key in keys if f'"{key}"' in body or f"'{key}'" in body]
		self.assertEqual(hardcoded, [], f"domain/profile keys hardcoded in the JS: {hardcoded}")

	def test_it_hardcodes_no_tool_name(self):
		body = self.js()
		hardcoded = [name for name in registry.TOOLS if f'"allow_{name}"' in body]
		self.assertEqual(hardcoded, [], f"tool names hardcoded in the JS: {hardcoded}")

	def test_it_previews_before_it_writes(self):
		"""`dry_run` is what puts "this switches 412 things off" in front of
		somebody before the button that does it."""
		self.assertIn("dry_run", self.js())

	#: A `data-*` hook as it appears in MARKUP — deliberately not preceded by `[`,
	#: which is what distinguishes `data-console="shown"` inside an element from
	#: the `[data-console="shown"]` of a selector. Without that lookbehind every
	#: selector also counts as markup, the two sets are trivially equal, and the
	#: test passes on a file where the hook exists ONLY in the selector. It did,
	#: until the mutation below was actually run.
	DRAWN = r'(?<!\[)(data-(?:console|domain|bulk|profile|count-for)="[^"]*")'
	QUERIED = r'\.find\(\s*[\'"`](\[data-[a-z-]+="[^"]*"\])'

	def test_every_control_it_wires_up_is_a_control_it_drew(self):
		"""The console builds its markup in one function and binds handlers in
		another, keyed on `data-*` attributes. Nothing else checks that the two
		agree, and the failure is silent in the worst way: a renamed hook leaves a
		button that renders, looks enabled, and does nothing when clicked.

		STATIC HOOKS ONLY. Several are built by interpolation — the markup writes
		`data-count-for="${esc(domain.key)}"` and the selector reads
		`[data-count-for="${domain.key}"]` — and comparing those as strings
		compares two spellings of the same runtime value. They are excluded rather
		than normalised, because a normaliser clever enough to equate them would
		be clever enough to equate a genuine mismatch.
		"""
		import re

		body = self.js()
		static = lambda hooks: {hook for hook in hooks if "${" not in hook}  # noqa: E731
		drawn = static(re.findall(self.DRAWN, body))
		queried = static(re.findall(self.QUERIED, body))
		missing = sorted(hook for hook in queried if hook.strip("[]") not in drawn)
		self.assertEqual(
			missing,
			[],
			f"the JS binds handlers to controls its own markup never draws: {missing}",
		)
		self.assertGreaterEqual(
			len(queried),
			4,
			f"the hook regex has gone stale rather than green — it found {queried}",
		)

	#: An `<option value="x">` inside the filter `<select>`, and the branch that
	#: acts on one. The select is `data-console="only"` and its value lands in
	#: `VIEW.only`, so a branch is always spelled `VIEW.only === "x"`.
	OPTIONS = r'<option value="([a-z_]+)"'
	BRANCHES = r'VIEW\.only === "([a-z_]+)"'

	def test_every_filter_option_it_offers_is_one_it_acts_on(self):
		"""A dropdown entry with no branch renders, selects, and does nothing.

		Same silent-failure class as the hook test above and caught the same way:
		by comparing what the markup offers against what the code answers. `all`
		is excluded because it is the absence of a filter — it is the default
		`VIEW.only` and correctly has no branch of its own.

		WHAT THIS TEST CANNOT DO, said plainly because a test that only checks
		one direction should admit which. It catches an ORPHANED option — one
		offered and never acted on. It cannot catch a MISSING one, and a missing
		one is exactly what shipped in v0.108.0: four options, three branches,
		and no way to filter to read tools, which left "enable all of this
		domain's reads" unreachable until v0.114.0. Nothing string-level could
		have found that, because the gap was a control nobody had written.

		Nor does any of this prove the console renders. `self.js()` reads the file
		and every assertion in this class is a regex over source; the suite never
		executes `apply_tool_filter`.
		"""
		import re

		body = self.js()
		options = set(re.findall(self.OPTIONS, body)) - {"all"}
		branches = set(re.findall(self.BRANCHES, body))
		orphaned = sorted(options - branches)
		self.assertEqual(
			orphaned,
			[],
			f"the filter offers option(s) nothing acts on, so choosing one silently does nothing: {orphaned}",
		)
		self.assertGreaterEqual(
			len(options),
			4,
			f"the option regex has gone stale rather than green — it found {sorted(options)}",
		)

	def test_the_read_filter_excludes_the_packet_types(self):
		"""The two compliance packet types are switches, not tools.

		They carry an `allow_` switch and sit in `CONSOLE.switches`, and
		`mutating` is false on both — so a bare `!info.mutating` read filter would
		sweep them in and let "enable every read tool in this domain" tick a
		packet type. The blast radius is nil (they are read-only artefacts that
		default on) but the meaning is wrong, so the exclusion is explicit and
		this pins it.

		ASSERTS ON THE WHOLE READ BRANCH, NOT ON THE EXCLUSION ALONE. The obvious
		version of this test — `assertIn("CONSOLE.switches || {})[tool]", …)` —
		is green on a file with the exclusion deleted, because the line above it
		that resolves `info` contains that same substring already:

		    const info = (CONSOLE.tools || {})[tool] || (CONSOLE.switches || {})[tool];

		It was written that way, run against a deliberately broken file, and
		passed. Matching the branch as one expression is what actually pins it.
		"""
		import re

		body = self.js()
		branch = re.search(r'VIEW\.only === "read"\)\s*\n?\s*visible = ([^;]+);', body)
		self.assertIsNotNone(branch, "the read-filter branch is gone or has been rewritten")
		expression = " ".join(branch.group(1).split())
		self.assertEqual(
			expression,
			"!!info && !info.mutating && !(CONSOLE.switches || {})[tool]",
			f"the read filter no longer excludes the packet types: {expression!r}",
		)
