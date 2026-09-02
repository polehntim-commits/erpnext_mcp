# SPDX-License-Identifier: MIT
"""Every hook in `hooks.py` resolves — which v0.14.0 shipped without checking.

WHAT THIS EXISTS TO PREVENT, IN THE WORDS OF WHAT IT DID.

v0.14.0 declared a Jinja method as
`"erpnext_mcp_amount_in_words:erpnext_mcp.render.checks.amount_in_words"`. That
`"<name>:<path>"` form belongs to Frappe's OLDER `jenv` hook, whose reader splits
on the colon. The modern `jinja` hook hands each entry straight to
`frappe.get_attr` and takes the global's name from the callable's own
`__name__`. So `get_attr` received the whole string, split it on the first dot to
find an app name, and threw:

    AppNotInstalledError: App erpnext_mcp_amount_in_words:erpnext_mcp is not installed

**Frappe builds the Jinja environment to render the error page too.** The
exception was raised inside the handler for its own exception, so every page on
the site returned 500 — including the one that would have said why. A cosmetic
string on a print format took the whole UI down, and nothing in a 2400-test suite
noticed, because no test had ever read `hooks.py`.

THE FAILURE MODE IS THE POINT, NOT THE HOOK. A hook is a string the app never
executes itself: nothing imports it, nothing calls it, and every existing test
exercises the functions it names *directly*. So a hook can name a module that
does not exist, a function that was renamed, or — as here — a perfectly real
function in a syntax the reader does not speak, and the suite is green right up
until `bench migrate` on somebody's site. This module closes that by resolving
every path in the file the way FRAPPE resolves it, including its app-name rule,
which is the specific thing that threw.

IT REFUSES AN UNKNOWN HOOK KEY. `KNOWN_HOOKS` has to name every attribute in
`hooks.py`, so adding `doc_events`, `override_whitelisted_methods`,
`permission_query_conditions` or anything else fails here until somebody says
which shape it holds and therefore how it gets validated. That is deliberate
friction: this app's whole promise is that installing it cannot change how a
site behaves, and every one of those hooks is a way to break that promise
silently.
"""

import json
import pathlib
import types
import unittest

from erpnext_mcp import hooks

from .harness import frappe  # noqa: F401 - installs the frappe double before erpnext_mcp imports

#: Every attribute `hooks.py` declares, and what has to be true of it.
#:
#:   "metadata"   plain values Frappe reads as configuration. Nothing to resolve.
#:   "app_list"   a list of app names.
#:   "path"       ONE dotted path to a callable.
#:   "path_map"   a dict of lists of dotted paths — `scheduler_events`. FROM
#:                v0.19.4 one of its values is itself a dict, because Frappe's
#:                `cron` key is `{expression: [paths]}` rather than a bare list.
#:                The walker below descends into it; a walker that did not would
#:                iterate the cron EXPRESSION one character at a time and report
#:                every character as a path that fails to resolve, which is the
#:                same class of mistake as conflating `path_map` with `path_dict`
#:                and is the reason both are spelled out here.
#:   "path_dict"  a dict of ONE dotted path each, keyed by doctype — the two
#:                permission hooks. A DIFFERENT SHAPE from `path_map`, and
#:                conflating them is not cosmetic: the resolver iterates a
#:                path_map's values, so a plain string gets walked one CHARACTER
#:                at a time and every one of them "fails to resolve".
#:   "path_list"  a plain list of dotted paths — `auth_hooks`, which is the
#:                shape `validate_auth_via_hooks` iterates.
#:   "jinja"      a dict of lists of dotted paths that must carry NO colon.
#:   "asset_map"  a dict of doctype → list of FILE PATHS inside the app —
#:                `doctype_js`. NOT dotted paths, so the resolver must not walk
#:                it: `frappe.get_attr` on "public/js/field_map.js" would look
#:                for an app called "public/js/field_map" and throw the same
#:                AppNotInstalledError this module exists for. It gets its own
#:                shape and its own checks — the files exist, and every doctype
#:                named is one this app created.
#:
#: A key missing from here fails `test_every_hook_key_is_accounted_for`, which is
#: the whole point: a new hook is a new way to take a site down and it does not
#: get to arrive unexamined.
KNOWN_HOOKS = {
	"app_name": "metadata",
	"app_title": "metadata",
	"app_publisher": "metadata",
	"app_description": "metadata",
	"app_email": "metadata",
	"app_license": "metadata",
	"required_apps": "app_list",
	"after_install": "path",
	"after_migrate": "path",
	"before_uninstall": "path",
	"scheduler_events": "path_map",
	"jinja": "jinja",
	"permission_query_conditions": "path_dict",
	"has_permission": "path_dict",
	"auth_hooks": "path_list",
	"doctype_js": "asset_map",
}

#: Hook keys this app must NOT declare, and why each one would be a lie.
#:
#: `jenv` is here because v0.14.0 declared it *as an alias of* `jinja` — two hook
#: keys with two different syntaxes pointing at one dict. It is the deprecated
#: spelling, `jinja` has existed since v14, and v14 is this app's floor, so a
#: second declaration buys nothing and doubles the surface for exactly the format
#: mistake that caused the outage.
#: `doctype_js` WAS here until v0.32.0, spelled "this app adds no client script
#: to a doctype it does not own" — and the clause after the dash was always the
#: real rule. v0.32.0 attaches a map to seven doctypes, all seven of which this
#: app created, so `TheFormScripts` below asserts the rule the sentence stated
#: rather than the ban that stood in for it. Same move, and the same argument, as
#: the two permission hooks made in v0.17.1.
#: `permission_query_conditions` and `has_permission` WERE here until v0.17.1,
#: on the grounds that "this app changes nobody's visibility". That was a proxy
#: for the invariant actually worth keeping — do not touch OTHER PEOPLE'S
#: doctypes — and the proxy was wrong for two doctypes this app ships and never
#: scoped. `test_permissions.py` enforces the narrower, stronger rule in its
#: place. See `permissions.py`.
FORBIDDEN_HOOKS = {
	"jenv": "the deprecated spelling of `jinja`, with a DIFFERENT syntax",
	"doc_events": "this app installs no document hooks — see the hooks.py docstring",
	"override_doctype_class": "this app overrides no doctype",
	"override_whitelisted_methods": "this app overrides no framework method",
	"fixtures": "this app ships no fixtures",
}


def hook_attributes() -> dict:
	"""Everything `hooks.py` declares, excluding dunders and imported modules."""
	return {
		name: value
		for name, value in vars(hooks).items()
		if not name.startswith("_") and not isinstance(value, types.ModuleType)
	}


def dotted_paths() -> list:
	"""(hook key, path string) for every path in the file, whatever shape holds it."""
	out = []
	for name, shape in KNOWN_HOOKS.items():
		value = getattr(hooks, name, None)
		if shape == "path":
			out.append((name, value))
		elif shape == "path_list":
			for entry in value or []:
				out.append((name, entry))
		elif shape in ("path_map", "jinja"):
			for group, entries in (value or {}).items():
				if isinstance(entries, dict):
					# Frappe's `cron` key: {expression: [paths]}. One level deeper
					# than every other interval, and a walker that treated it like
					# a list would walk the expression string character by
					# character. v0.19.4.
					for expression, nested in entries.items():
						for entry in nested or []:
							out.append((f"{name}.{group}[{expression}]", entry))
					continue
				for entry in entries:
					out.append((f"{name}.{group}", entry))
		elif shape == "path_dict":
			for group, entry in (value or {}).items():
				out.append((f"{name}.{group}", entry))
	return out


def resolve(path: str):
	"""`frappe.get_attr`, reproduced — INCLUDING the app-name rule that threw.

	Deliberately a second implementation rather than a call into the double.
	`frappe.get_attr` is what runs on the site, and the half of it that matters
	here is the half nobody thinks about:

	    app_name = method_string.split(".", 1)[0]
	    if app_name not in get_installed_apps(): throw(AppNotInstalledError)

	A path carrying a `name:` prefix has an "app name" of
	`erpnext_mcp_amount_in_words:erpnext_mcp`, which is not installed, and that is
	the whole outage. A test that skipped straight to `importlib` would import the
	module fine and prove nothing.
	"""
	app_name = str(path).split(".", 1)[0]
	if app_name != "erpnext_mcp":
		raise AssertionError(
			f"{path!r} resolves to app {app_name!r}, which is not this app. Frappe reads the "
			"app name as everything before the first dot and refuses a path whose app is not "
			"installed — which is exactly how a `name:path` string becomes "
			"AppNotInstalledError."
		)
	module_path, _, attribute = str(path).rpartition(".")
	module = __import__(module_path, fromlist=["x"])
	return getattr(module, attribute)


class TheHookFile(unittest.TestCase):
	def test_every_hook_key_is_accounted_for(self):
		"""A new hook does not get to arrive unexamined. Add it to KNOWN_HOOKS with
		its shape — which is a decision about how it is validated — or do not add
		it at all."""
		declared = set(hook_attributes())
		self.assertEqual(
			declared,
			set(KNOWN_HOOKS),
			"hooks.py declares something KNOWN_HOOKS does not describe (or the reverse). "
			"Every hook is a way to change how a site behaves; say which shape it holds.",
		)

	def test_the_hooks_this_app_promises_not_to_install_are_absent(self):
		for name, why in sorted(FORBIDDEN_HOOKS.items()):
			with self.subTest(hook=name):
				self.assertFalse(
					hasattr(hooks, name),
					f"hooks.py declares {name!r} — {why}. The README and the module docstring "
					"both promise it does not.",
				)


class EveryPathResolves(unittest.TestCase):
	def test_there_are_paths_to_check(self):
		"""Guard against the walk silently finding nothing and passing."""
		self.assertGreaterEqual(len(dotted_paths()), 5)

	def test_every_hook_path_resolves_to_a_callable(self):
		"""The test v0.14.0 did not have. Every one of these is a string Frappe
		imports on install, on migrate, on a scheduler tick or on the first page
		render — and none of them is executed by any other test in this suite."""
		for hook, path in dotted_paths():
			with self.subTest(hook=hook, path=path):
				self.assertTrue(path, f"{hook} has an empty path")
				resolved = resolve(path)
				self.assertTrue(
					callable(resolved), f"{hook} → {path} resolved to {resolved!r}, not a callable"
				)

	def test_no_hook_path_carries_a_colon(self):
		"""THE v0.14.0 BUG, asserted directly.

		Not one of Frappe's modern hook readers splits on a colon. `jinja` hands
		the string to `frappe.get_attr` whole; `scheduler_events` does the same;
		`after_install` and its siblings do the same. The `"<name>:<path>"` form
		is `jenv`'s alone, and `jenv` is not declared here.
		"""
		for hook, path in dotted_paths():
			with self.subTest(hook=hook, path=path):
				self.assertNotIn(
					":",
					str(path),
					f"{hook} → {path!r} carries a colon. That is the OLD `jenv` syntax; "
					"Frappe's modern hooks take a bare dotted path and derive any name they "
					"need from the callable's __name__. A colon here is "
					"AppNotInstalledError on every page render.",
				)

	def test_a_colon_prefixed_path_would_be_caught(self):
		"""The guard has to actually fail on the string that shipped, or it is
		decoration."""
		shipped = "erpnext_mcp_amount_in_words:erpnext_mcp.render.checks.amount_in_words"
		with self.assertRaises(AssertionError) as caught:
			resolve(shipped)
		# The app name Frappe would have looked for, and did not find.
		self.assertIn("erpnext_mcp_amount_in_words:erpnext_mcp", str(caught.exception))
		self.assertIn(":", shipped)


class TheFormScripts(unittest.TestCase):
	"""`doctype_js`, and the rule the blanket ban on it was standing in for.

	v0.32.0 attaches a Leaflet map to the seven doctypes in this app that know
	where they are. Until then the key was FORBIDDEN outright, on the grounds that
	"this app adds no client script to a doctype it does not own" — and the clause
	after the comma was always the actual invariant. A script on a doctype this
	app created goes when the app goes; a script on somebody else's stays behind
	changing how their form behaves, which is the promise `hooks.py` makes.

	So the ban becomes three assertions: every doctype named is one this app
	shipped, every file named exists, and the shared widget is listed first in
	every entry — because Frappe concatenates them in order and the per-doctype
	file calls into the widget as it is evaluated.
	"""

	#: Where the app's own DocType JSON lives, so "did this app create it" is
	#: answered from what ships rather than from a list somebody maintains.
	DOCTYPE_DIR = pathlib.Path(__file__).resolve().parent.parent / "erpnext_mcp" / "erpnext_mcp" / "doctype"
	APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "erpnext_mcp"

	SHARED_WIDGET = "public/js/geo_map_widget.js"

	def app_doctypes(self) -> set:
		out = set()
		for path in self.DOCTYPE_DIR.glob("*/*.json"):
			if path.stem != path.parent.name:
				continue
			out.add(json.loads(path.read_text(encoding="utf-8"))["name"])
		return out

	def test_every_doctype_it_names_is_one_this_app_created(self):
		"""THE INVARIANT THE BAN WAS A PROXY FOR. A form script on a doctype this
		app created disappears with the app; one on somebody else's does not, and
		an operator who removes us would be left with a form that behaves
		differently and no way to find out why."""
		ours = self.app_doctypes()
		self.assertTrue(ours, "could not enumerate this app's doctypes")
		for doctype in hooks.doctype_js:
			with self.subTest(doctype=doctype):
				self.assertIn(
					doctype,
					ours,
					f"doctype_js attaches a script to {doctype!r}, which this app did not "
					"create. Installing this app would then change how a form the operator "
					"already had behaves — which hooks.py promises never happens.",
				)

	def test_every_file_it_names_exists(self):
		"""A `doctype_js` path Frappe cannot read is not an error anybody sees: the
		form simply renders without the script. This is the only thing that
		notices a renamed file."""
		for doctype, files in hooks.doctype_js.items():
			for relative in files:
				with self.subTest(doctype=doctype, file=relative):
					self.assertTrue(
						(self.APP_DIR / relative).is_file(),
						f"doctype_js[{doctype!r}] names {relative!r}, which is not a file in "
						"the app. Frappe skips a path it cannot read and the form loses its "
						"script silently.",
					)

	def test_the_shared_widget_is_listed_first_everywhere(self):
		"""ORDER IS LOAD ORDER. Frappe concatenates these into one script in the
		order given, and each per-doctype file calls into the widget as it is
		evaluated — so a widget listed second is a ReferenceError that takes the
		whole form script down.

		THE WIDGET IS LISTED ONCE, AND EVERY OTHER ENTRY IS A FORM SCRIPT. This
		read `len(files) == 2` until v0.148.0, which was the same rule written as
		a count while every doctype happened to have exactly one script. Asset
		Register had two from v0.145.0 to v0.153.0 — `asset_register_map.js` drew
		a read-only pin for a pump or a bin trailer and `irrigation_valve_map.js`
		a draggable one for a valve, each returning early on the other's records
		— and the count assertion had been failing on that legitimate pair since
		the day it landed. v0.154.0 merged the two, so every doctype is back to
		one script; the rule stays written as a rule, because the next pair will
		not announce itself either.

		Naming the widget twice is still a fault and is still caught: Frappe
		would evaluate it twice and the second evaluation would re-register every
		handler it installs.
		"""
		for doctype, files in hooks.doctype_js.items():
			with self.subTest(doctype=doctype):
				self.assertIsInstance(files, list)
				self.assertEqual(files[0], self.SHARED_WIDGET)
				self.assertEqual(files.count(self.SHARED_WIDGET), 1, "the shared widget is listed once")
				self.assertGreaterEqual(len(files), 2, "one widget plus at least one per-doctype script")

	def test_nothing_here_is_a_dotted_path(self):
		"""The resolver must never walk this hook. `frappe.get_attr` on
		"public/js/field_map.js" reads the app name as everything before the first
		dot — "public/js/field_map" — and throws the AppNotInstalledError this
		whole module exists because of."""
		for _, path in dotted_paths():
			self.assertNotIn("public/js", str(path))

	def test_every_map_script_carries_the_licence_header(self):
		for files in hooks.doctype_js.values():
			for relative in files:
				with self.subTest(file=relative):
					head = (self.APP_DIR / relative).read_text(encoding="utf-8").split("\n", 1)[0]
					self.assertEqual(head, "// SPDX-License-Identifier: MIT")


class TheJinjaMethod(unittest.TestCase):
	def test_it_resolves_and_writes_a_check_amount(self):
		"""Resolved through the hook string, not imported by name — so a hook that
		pointed at the wrong function would fail here even though the right
		function exists."""
		path = hooks.jinja["methods"][0]
		self.assertEqual(resolve(path)(1234.56), "One Thousand Two Hundred Thirty-Four and 56/100")

	def test_the_global_name_comes_from_the_function_and_is_namespaced(self):
		"""Frappe names the Jinja global after the callable's `__name__`, so the
		hook string no longer gets a say in it — and a Jinja global lands in a
		namespace shared with Frappe, ERPNext and every other installed app."""
		resolved = resolve(hooks.jinja["methods"][0])
		self.assertEqual(resolved.__name__, "erpnext_mcp_amount_in_words")
		self.assertTrue(resolved.__name__.startswith("erpnext_mcp_"))

	def test_the_check_template_calls_the_name_the_hook_actually_registers(self):
		"""The two halves have to agree, and nothing else makes them."""
		from erpnext_mcp.tools import printing

		registered = resolve(hooks.jinja["methods"][0]).__name__
		self.assertIn(registered, printing.CHECK_TEMPLATE)

	def test_the_template_still_prints_a_check_if_the_hook_is_absent(self):
		"""Belt to the brace. A check with no amount in words is not a check, so
		the template guards with `is defined` and falls back to Frappe's own."""
		from erpnext_mcp.tools import printing

		self.assertIn("erpnext_mcp_amount_in_words is defined", printing.CHECK_TEMPLATE)
		self.assertIn("frappe.utils.money_in_words", printing.CHECK_TEMPLATE)

	def test_the_badge_method_resolves_and_answers_with_a_dict(self):
		"""v0.56.0's second method, resolved through the hook string rather than
		imported by name — so a hook pointing at the wrong function fails here even
		though the right function exists.

		Called with a badge nothing knows about, because the contract that matters
		is that it NEVER RAISES: it is resolved while Frappe builds the Jinja
		environment, which it also does to render the error page."""
		card = resolve(hooks.jinja["methods"][1])("no-such-badge")
		self.assertIsInstance(card, dict)
		self.assertFalse(card["ok"])
		self.assertEqual(card["badge_id"], "no-such-badge")

	def test_the_badge_global_name_comes_from_the_function_and_is_namespaced(self):
		resolved = resolve(hooks.jinja["methods"][1])
		self.assertEqual(resolved.__name__, "erpnext_mcp_badge_card")
		self.assertTrue(resolved.__name__.startswith("erpnext_mcp_"))

	def test_the_badge_template_calls_the_name_the_hook_actually_registers(self):
		"""The two halves have to agree, and nothing else makes them."""
		from erpnext_mcp import badge_print_format

		registered = resolve(hooks.jinja["methods"][1]).__name__
		self.assertEqual(registered, badge_print_format.JINJA_GLOBAL)
		self.assertIn(registered, badge_print_format.BADGE_TEMPLATE)

	def test_the_badge_template_still_prints_a_card_if_the_hook_is_absent(self):
		"""Belt to the brace, the same one the check template wears. A Print button
		that renders a traceback is worse than one that renders a plainer card."""
		from erpnext_mcp import badge_print_format

		self.assertIn("erpnext_mcp_badge_card is defined", badge_print_format.BADGE_TEMPLATE)

	def test_these_two_jinja_methods_and_nothing_else(self):
		"""Every entry here is evaluated when Frappe BUILDS THE JINJA ENVIRONMENT,
		which is the most expensive place in `hooks.py` to be wrong — v0.14.0 took
		every page on the site down, error page included, from this one key. The
		count is asserted so a third arrives on purpose and with an argument.

		It was `assertEqual(..., 1)` until v0.56.0, when the badge card needed to
		resolve four records into one dict and the alternative was a Print Format
		making four framework calls in presentation code."""
		self.assertEqual(len(hooks.jinja["methods"]), 2)
		self.assertEqual(set(hooks.jinja), {"methods"})


class TheScheduledJobs(unittest.TestCase):
	def test_every_scheduled_job_resolves(self):
		self.assertEqual(sorted(hooks.scheduler_events), ["cron", "daily", "hourly", "weekly"])
		for hook, path in dotted_paths():
			if not hook.startswith("scheduler_events"):
				continue
			with self.subTest(hook=hook, path=path):
				self.assertTrue(callable(resolve(path)))

	def test_they_are_these_nine_and_nothing_else(self):
		"""Eleven jobs. Three write only this app's own doctypes or nothing at all;
		the fourth writes two credential fields on Frappe's User and had to argue
		for it — see hooks.py and tools/mobile.sweep_idle_grants — the fifth makes
		an outbound request to somebody else's server, which is a bar none of the
		others had to clear, and the sixth does arbitrary arithmetic over a
		site's whole ledger, which is a different bar again and is why it is the
		only one with a kill switch of its own.

		Asserted as an exact mapping rather than a membership check, so a twelfth
		job fails here and has to be argued for. (The method's name says nine and
		is left alone on purpose: renaming it every time the list grows would
		break the git history of the one test that guards this file, and the
		count that matters is the mapping below.) Every scheduled job is code that
		runs on somebody's site with nobody watching, which is the same reason
		`KNOWN_HOOKS` refuses an unexamined hook key.

		The alert sweep moved to HOURLY in v0.17.1 — see the hooks.py docstring.
		The short version: the sweep is what makes a completed task's alert go
		away, and nightly meant a worker saw the phone asking them to walk a cabin
		they had already walked, all day.

		The weather sweep arrived in v0.19.4 as the first `cron` entry, at fifteen
		minutes, because OAR 437-004-1131 asks what the conditions were across an
		exposure period: nine readings on a nine-hour shift is a sketch and
		thirty-six is a timeline.

		The KPI history sweep arrived in v0.19.6 as the second, at two in the
		morning. `daily` would be tidier and is wrong: Frappe's `daily` fires on
		the day's first scheduler tick, which on a farm bench is during the
		morning, and this is the one job here that can take minutes on a large
		ledger. IT IS ONE ENTRY THAT ITERATES over every registered report and
		every company — a scheduler with a cron per KPI is one nobody can read,
		and the Financial KPI Framework is going to add KPIs as data.

		The regulation feed sweep arrived in v0.38.0 as the third, at four in the
		morning, and it is the SECOND job here that talks to somebody else's
		server — a different server per feed. It DETECTS AND DOES NOT REMEDIATE:
		it writes a hash, a timestamp and a log line on this app's own Regulation
		Feed, and modifies no Compliance Rule at all. The alternative would be a
		farm's compliance calendar rewritten at four in the morning off a website
		redesign, which is why the assertion below is worth having: a future
		release wiring a remediation job in has to change this line to do it.

		The KPI cache refresh arrived in v0.39.0 as the eighth, at three in the
		morning — between the shipped-report sweep and the regulation feed. It is
		the two o'clock job's counterpart for KPIs that are RECORDS, and it exists
		separately because a shipped report has no definition to read its window
		type, window length and step from, so the older job assumes a monthly TTM.
		It is one entry that iterates, and here that is load-bearing rather than
		tidy: the whole point of v0.39.0 is that an operator adds a KPI without a
		code release, and a KPI needing its own scheduler entry would be one they
		could not add. It shares `enable_kpi_history_sweep` with the two o'clock
		job rather than getting a second checkbox — they cache the same doctype
		for the same reason, and a second setting called something almost
		identical is how a setting stops being read.

		The budget refresh arrived in v0.42.0 as the ninth, at 3:15 — fifteen
		minutes after the KPI cache job and DELIBERATELY AFTER IT, since a
		budget's KPI targets read `compute_kpi(..., use_cache=True)` and a
		budget refreshed before the night's KPI figures land would save
		yesterday's cached value under tonight's date. It is one entry that
		iterates over every ACTIVE Budget, writes only this app's own `Budget`,
		and never raises — the same three-part contract every job on this list
		keeps. It carries no kill switch of its own: unlike the KPI history job,
		its cost scales with the number of accounts and KPIs one budget names
		rather than with the size of the whole ledger.

		The restricted-entry sweep arrived in v0.78.0 as the tenth, on `hourly`,
		beside the alert sweep. AN REI IS MEASURED IN HOURS, so an hourly cadence
		is the coarsest one that does not keep a crew out of a block they may
		work — a four-hour window that cleared at 10:40 and still reads Active at
		11:00 is the same cost as the opposite error and happens far more often.
		IT IS BELT AND BRACES RATHER THAN THE MECHANISM: every read in
		`tools/spray_rei.py` runs the same sweep before it answers, so a bench
		whose scheduler is wedged still tells the truth at a gate. This entry
		keeps the register tidy for a Desk report; it is deliberately not the
		only thing standing between a worker and a stale restriction.

		The maintenance sweep arrived in v0.78.0 as the eleventh, at 4:30 — after
		the regulation feed and before anybody is at a tailgate. IT IS THE FIRST
		JOB ON THIS LIST THAT RAISES WORK FOR OTHER PEOPLE, which is what puts it
		on a named hour rather than on `hourly`: a service is a day's job, and a
		machine that came due at 09:12 does not need telling before tomorrow
		morning's board is read. It cannot raise a second task against an asset
		that already has one open, which is what makes a nightly job safe to
		leave running rather than a nightly source of duplicates. One entry that
		iterates every company, same as the four crons above it.

		The USDA price sweep arrived in v0.87.0 as the twelfth, at five — last of
		the night jobs, because it is the only one whose whole purpose is to have
		an answer waiting before anybody asks, and a grower reads a market at
		breakfast. IT IS THE THIRD JOB HERE THAT TALKS TO SOMEBODY ELSE'S SERVER
		AND THE ONLY ONE THAT SHIPS SWITCHED OFF: the weather and the regulation
		feeds work against keyless public sources and this needs an AMS API key,
		so an always-on entry would log an authentication failure every night on
		every site that never wanted a market overlay. One entry that iterates
		the report slugs an operator configured; with none configured it does
		nothing and says so rather than guessing at a report.
		"""
		self.assertEqual(
			hooks.scheduler_events,
			{
				"cron": {
					"*/15 * * * *": ["erpnext_mcp.services.weather.sweep_open_shifts"],
					"0 2 * * *": ["erpnext_mcp.services.windowed_reports.recompute_kpi_history_incremental"],
					"0 3 * * *": ["erpnext_mcp.services.kpi_engine.refresh_all_kpi_caches"],
					"15 3 * * *": ["erpnext_mcp.tools.budget.refresh_all_active_budgets"],
					"0 4 * * *": ["erpnext_mcp.services.regulation_feed.sweep_due_feeds"],
					"30 4 * * *": ["erpnext_mcp.tools.maintenance.sweep_due_maintenance"],
					"0 5 * * *": ["erpnext_mcp.services.usda_prices.sweep_configured_reports"],
				},
				"hourly": [
					"erpnext_mcp.alerts.sweep",
					"erpnext_mcp.tools.spray_rei.close_expired_reis",
				],
				"daily": [
					"erpnext_mcp.tools.uploads.collect_expired_sessions",
					"erpnext_mcp.tools.mobile.sweep_idle_grants",
				],
				"weekly": ["erpnext_mcp.drift.scan"],
			},
		)

	def test_the_kpi_history_sweep_never_raises_and_takes_no_arguments(self):
		"""Same contract as the other five, and it needs it for its own reason.

		It is the only scheduled job whose cost scales with the size of somebody's
		BOOKS rather than with the number of their cabins or their open shifts, so
		its failure modes include ones nobody can enumerate in advance: a company
		with a decade of ledger, a chart of accounts nobody typed correctly, a
		computer that raises on a period the developer never saw. None of those
		may take a scheduler tick down, and none of them is a reason the other
		five jobs in that tick do not run.
		"""
		import inspect

		from erpnext_mcp.services import windowed_reports

		from .harness import INSTALLED_DOCTYPES

		self.assertEqual(
			list(inspect.signature(windowed_reports.recompute_kpi_history_incremental).parameters), []
		)
		INSTALLED_DOCTYPES.discard("Financial KPI History")
		try:
			self.assertEqual(windowed_reports.recompute_kpi_history_incremental(), 0)
		finally:
			INSTALLED_DOCTYPES.add("Financial KPI History")

	def test_the_weather_sweep_never_raises_and_takes_no_arguments(self):
		"""Same contract as the other four, and it needs it more than any of them.

		It is the only job that talks to a third party, so its failure modes
		include ones this app cannot influence — a slow link out of a farm office,
		a rate limit, a captive portal answering 200 with a login page. None of
		those may take a scheduler tick down, and none of them is a reason the
		other jobs in that tick do not run.
		"""
		import inspect

		from erpnext_mcp.services import weather

		from .harness import INSTALLED_DOCTYPES

		self.assertEqual(list(inspect.signature(weather.sweep_open_shifts).parameters), [])
		INSTALLED_DOCTYPES.discard("Weather Settings")
		try:
			self.assertEqual(weather.sweep_open_shifts(), 0)
		finally:
			INSTALLED_DOCTYPES.add("Weather Settings")

	def test_the_sweep_is_a_full_reconciliation_so_the_cadence_is_only_tuning(self):
		"""Running it twice must produce what running it once does — that is what
		makes hourly a cost decision rather than a correctness one."""
		from erpnext_mcp import alerts

		first = alerts.sweep()
		second = alerts.sweep()
		self.assertEqual(first, 0)
		self.assertEqual(second, 0)

	def test_the_upload_sweeper_never_raises(self):
		"""It runs on the site's scheduler beside everybody else's jobs."""
		from erpnext_mcp.tools import uploads

		from .harness import INSTALLED_DOCTYPES

		INSTALLED_DOCTYPES.discard("Staged File Upload Session")
		try:
			self.assertEqual(uploads.collect_expired_sessions(), 0)
		finally:
			INSTALLED_DOCTYPES.add("Staged File Upload Session")

	def test_the_alert_sweep_never_raises(self):
		"""Same contract, same reason. A compliance calendar that took the site's
		scheduler down would be considerably worse than one that missed a night."""
		from erpnext_mcp import alerts

		from .harness import INSTALLED_DOCTYPES

		INSTALLED_DOCTYPES.discard("Compliance Alert")
		try:
			self.assertEqual(alerts.sweep(), 0)
		finally:
			INSTALLED_DOCTYPES.add("Compliance Alert")

	def test_the_alert_sweep_takes_no_arguments(self):
		"""`scheduler_events` calls it bare. A job whose signature needs arguments
		is a job that raises TypeError on the first tick, on somebody's site, at
		three in the morning."""
		import inspect

		from erpnext_mcp import alerts

		signature = inspect.signature(alerts.sweep)
		self.assertEqual(list(signature.parameters), [])


class TheInstallHooks(unittest.TestCase):
	def test_install_migrate_and_uninstall_all_resolve(self):
		for name in ("after_install", "after_migrate", "before_uninstall"):
			with self.subTest(hook=name):
				self.assertTrue(callable(resolve(getattr(hooks, name))))

	def test_required_apps_names_erpnext(self):
		"""`install-app` has to refuse on a Frappe-only site rather than fail at
		the first tool call."""
		self.assertEqual(hooks.required_apps, ["erpnext"])

	def test_the_app_name_matches_the_package(self):
		"""Every hook path's first segment is read as this app's name."""
		self.assertEqual(hooks.app_name, "erpnext_mcp")
		for _hook, path in dotted_paths():
			self.assertTrue(str(path).startswith(f"{hooks.app_name}."))
