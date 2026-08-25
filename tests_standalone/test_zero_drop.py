# SPDX-License-Identifier: MIT
"""The `coerce(...) or DEFAULT` idiom, found by shape instead of by memory.

────────────────────────────────────────────────────────────────────────────
WHAT THIS CATCHES
────────────────────────────────────────────────────────────────────────────

    value = as_int(args, "key", DEFAULT) or DEFAULT

`as_int` already answers `DEFAULT` for a missing or empty value, so the trailing
`or` cannot fire on `None` and cannot fire on `""`. The only thing it can ever
catch is an EXPLICIT ZERO — which it replaces with the default, silently, and
reports success for.

The direction is what makes it worth a test rather than a comment. It does not
produce an error; it produces a plausible answer to a question nobody asked. Two
that shipped:

  * `generate_mobile_login_qr` guarded `if hours <= 0` and the line above it
    turned an explicit 0 into the default first. A caller asking for a zero-hour
    credential was not refused — they were handed a live, working login QR.
  * `create_sales_invoice` recomputed a line the caller had explicitly comped:
    `qty: 1000, rate: 0.62, amount: 0` invoiced at 620.00. `test_sales`'s own
    module docstring names this exact harm and says the only evidence would be
    "a rounding-sized difference nobody looks at".

────────────────────────────────────────────────────────────────────────────
WHY A TEST AND NOT A COMMENT
────────────────────────────────────────────────────────────────────────────

`tools/files.py:_resolve_max_bytes` has got this right since v0.100 and SAYS SO
in a comment — "Not `as_int(...) or DEFAULT`: that idiom turns an explicit 0 back
into the default, so a caller asking for something impossible gets a silent
success instead of the refusal below". Roughly forty other sites never received
it. A comment in one file has demonstrably failed as the mechanism, which is why
this is a scan.

────────────────────────────────────────────────────────────────────────────
THERE ARE TWO FIX SHAPES, AND USING THE WRONG ONE IS A SILENT NO-OP
────────────────────────────────────────────────────────────────────────────

The helper's own signature decides which:

  A. THE HELPER CARRIES A DEFAULT — `as_int(args, key, default)` returns `default`
     for absent and `None` only when no default was given. Deleting the trailing
     `or` is the whole fix; an absent value still takes the default.

  B. THE HELPER CARRIES NO DEFAULT — `as_float(value, key)` answers `0.0` for
     absent, for `""` and for an explicit `0` ALIKE, and never returns `None`.
     After coercion those three are the same float, so deleting the `or` changes
     the ABSENT case too. These need a branch on the RAW value:

         raw = entry.get("amount")
         amount = as_float(raw, key) if raw not in (None, "") else round(qty * rate, 2)

Applying shape A to a shape-B site looks exactly like a fix and does nothing —
`as_float(args, key, None)` is not even a valid call. The scan reports which
shape each site is so the reader is not left to work it out from the helper.

────────────────────────────────────────────────────────────────────────────
WHAT THIS DELIBERATELY DOES NOT FLAG, AND THE RULE THAT DECIDES
────────────────────────────────────────────────────────────────────────────

`x or 0` is NOT this bug. It maps `None → 0` and `0 → 0` — the same answer
either way — and 78 of the 97 sites in this tree are that harmless identity, with
19 left flagged and every one of them allowlisted with a reason. Only a fallback
that differs from the value the coercion ALREADY answers for absent can corrupt
anything, so the scan ignores `or 0`, `or 0.0`, and — when the coercion answers
zero for absent — `or None`, `or ""` and `or False` too. That last clause is
per-call-site rather than per-helper; see `_carries_a_nonzero_default`.

The sharper rule, which cost two reverted fixes to find:

    AN EXPLICIT ZERO IS ONLY A DISTINCT VALUE FOR AN ARGUMENT.

A Frappe `Int`, `Float`, `Currency` or `Percent` column has no empty state — an
unset one reads back as `0` — so for a STORED field "explicitly zero" and "never
set" are the same bytes, and no amount of fixing the coercion can separate them.
Every entry in `ALLOWLIST` below is either that, or a case where the `or` turned
out to be load-bearing. Each carries its reason, because an allowlist without
reasons becomes a list of things nobody dares touch.

AND A REASON CAN BE WRONG WHILE ITS VERDICT IS RIGHT. The budget_engine entries
first shipped claiming that removing either `or` raised ZeroDivisionError. A peer
re-ran it: removing either ALONE is safe and only removing BOTH raises. The
verdict held, the mechanism did not. Worth knowing before citing any entry here
as settled — and worth knowing that no test in test_budget.py sets `threshold_pct`
to 0 at all, so a green suite could never have caught it either way.

────────────────────────────────────────────────────────────────────────────
THE CONTROLS
────────────────────────────────────────────────────────────────────────────

A scan that finds nothing is indistinguishable from a scan that looks for nothing,
so `TheScannerItself` proves the scanner FLAGS a deliberately broken snippet,
proves it ignores the identity case, and proves it tells the two fix shapes
apart. `TheAllowlist` proves every entry still matches a real site — an entry
whose code was fixed or deleted is stale and says so rather than quietly
excusing whatever moves into that name next.
"""

import ast
import pathlib
import unittest

#: Coercion helpers that take a `default` and answer it for an absent value.
#: For these the fix is to delete the trailing `or` — see shape A above.
COERCERS_WITH_DEFAULT = frozenset({"as_int", "_as_int", "_as_float", "_money"})

#: Coercion helpers with no default, which answer 0/0.0 for absent, for "" and
#: for an explicit 0 alike. For these the fix is a branch on the RAW value —
#: see shape B above. `int`/`float`/`cint`/`flt` are here because they are the
#: same hazard written out longhand.
COERCERS_WITHOUT_DEFAULT = frozenset({"as_float", "cint", "flt", "int", "float", "_number"})

COERCERS = COERCERS_WITH_DEFAULT | COERCERS_WITHOUT_DEFAULT

#: A fallback equal to the coerced zero cannot corrupt anything: `x or 0` answers
#: 0 whether x was absent or explicitly 0. Compared by type as well as value so
#: that `or False` is not confused with `or 0` — they are the same number to
#: Python and different intentions to a reader.
_IDENTITY_FALLBACKS = ((None, type(None)), (0, int), (0.0, float), ("", str), (False, bool))

_SOURCE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "erpnext_mcp"


def _callee(node) -> str:
	"""The bare name of whatever a Call node calls, or "" if it is not a Call.

	`frappe.utils.cint(x)` and a bare `cint(x)` are the same hazard, so the
	attribute chain is discarded and only the final name is kept.
	"""
	if not isinstance(node, ast.Call):
		return ""
	func = node.func
	if isinstance(func, ast.Name):
		return func.id
	if isinstance(func, ast.Attribute):
		return func.attr
	return ""


#: Where each default-carrying helper keeps its default, positionally. `as_int` is
#: `(args, key, default)` and `_as_float`/`_money` are `(value, default)`, so the
#: index differs and guessing it wrong reads a KEY as a default.
_DEFAULT_ARG_INDEX = {"as_int": 2, "_as_int": 2, "_as_float": 1, "_money": 1}


def _carries_a_nonzero_default(call) -> bool:
	"""True when this CALL SITE supplies a default that is not zero.

	`_money(item.get("price"))` takes its helper's own default of 0.0, so absent and
	explicit-zero still coincide and `or None` stays innocent. `as_int(args, "x", 5)`
	does not. The distinction is per call site, not per helper, which is why this
	reads the arguments rather than the name.

	An expression it cannot evaluate — a Name, an attribute, a call — is assumed
	NON-ZERO. That errs toward flagging, which is the safe direction for a guard:
	a false positive is one allowlist entry with a reason, a false negative is
	another decade of this bug.
	"""
	helper = _callee(call)
	index = _DEFAULT_ARG_INDEX.get(helper)
	candidates = [kw.value for kw in call.keywords if kw.arg == "default"]
	if index is not None and len(call.args) > index:
		candidates.append(call.args[index])
	for node in candidates:
		if isinstance(node, ast.Constant):
			if type(node.value) in (int, float) and node.value == 0:
				continue
		return True
	return False


def _is_identity_fallback(node, call) -> bool:
	"""True when the `or` branch answers what the coercion already answers for absent.

	WHICH CONSTANTS COUNT DEPENDS ON THE CALL, and getting this wrong in the
	generous direction is a false negative nobody would ever notice:

	  * WHEN THE COERCION ANSWERS ZERO FOR ABSENT — every NO-DEFAULT helper, and
	    every default-carrying one called without a default or with a zero one —
	    the information is already gone before the `or` runs. `or None`, `or ""`
	    and `or False` lose nothing further: absent and explicit-zero come out the
	    same either way, so the `or` is innocent. 51 sites tree-wide rest on this,
	    nearly all `int(row.get("x") or 0) or None` over a stored column.

	  * WHEN THE CALL SUPPLIES A NON-ZERO DEFAULT it is a different story.
	    `as_int(args, "x", 5) or None` answers 5 for absent and None for an
	    explicit 0 — two different values, and there the `or` IS the mechanism.
	    Only a numeric zero is a true identity for those.

	There is no site of the second kind at HEAD. This is hardening against the one
	that lands later, not a live finding.
	"""
	if not isinstance(node, ast.Constant):
		return False
	if _carries_a_nonzero_default(call):
		return type(node.value) in (int, float) and node.value == 0
	return any(type(node.value) is kind and node.value == value for value, kind in _IDENTITY_FALLBACKS)


def scan_source(source: str, where: str = "<memory>") -> list:
	"""Every `coerce(...) or <non-zero fallback>` in one module.

	Returns dicts with `where`, `line`, `helper`, `shape` and `expression`. The
	expression is normalised to one line so it can be used as a stable key: line
	numbers drift every time a peer edits the file above it, and an allowlist
	keyed on them rots within a day in this tree.
	"""
	found = []
	tree = ast.parse(source)
	for node in ast.walk(tree):
		if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
			continue
		helper = _callee(node.values[0])
		if helper not in COERCERS:
			continue
		# `a or b or c` parses as one BoolOp with three values. Any of the later
		# branches can swallow the zero, so the whole chain is one finding.
		if all(_is_identity_fallback(value, node.values[0]) for value in node.values[1:]):
			continue
		expression = " ".join((ast.get_source_segment(source, node) or "").split())
		found.append(
			{
				"where": where,
				"line": node.lineno,
				"helper": helper,
				"shape": ("with-default" if helper in COERCERS_WITH_DEFAULT else "NO-DEFAULT"),
				"expression": expression,
			}
		)
	return found


def _tracked_modules() -> set:
	"""The shipped modules, as git knows them — or `None` where git cannot answer.

	THE SCAN JUDGES WHAT SHIPS, NOT WHAT HAPPENS TO BE ON DISK. An untracked file
	is in no committed tree, so CI never sees it and it cannot be allowlisted
	without the entry being permanently stale — but it sits in the working tree
	and would fail this test for every person who has one. `erpnext_mcp/
	remittance.py` is exactly that: untracked for days, disclaimed by every session
	that has looked at it.

	Returning `None` when git is unavailable is deliberate rather than lazy. CI
	runs from a checkout where git answers; a `git archive` extraction has no
	`.git` at all, and there every file present IS a tracked file, so scanning
	everything is the same answer by a different route. The fallback cannot
	silently narrow coverage.
	"""
	import subprocess

	try:
		out = subprocess.run(
			["git", "ls-files", "-z", "--", str(_SOURCE_ROOT.name)],
			cwd=_SOURCE_ROOT.parent,
			capture_output=True,
			timeout=30,
			check=False,
		)
	except (OSError, subprocess.SubprocessError):
		return None
	if out.returncode != 0:
		return None
	names = {name for name in out.stdout.decode("utf-8", "replace").split("\0") if name}
	return names or None


def _scanned_paths() -> list:
	"""Exactly the modules `scan_tree` reads, so its coverage can be asserted."""
	tracked = _tracked_modules()
	paths = []
	for path in sorted(_SOURCE_ROOT.rglob("*.py")):
		if "__pycache__" in path.parts:
			continue
		relative = str(path.relative_to(_SOURCE_ROOT.parent))
		if tracked is not None and relative not in tracked:
			continue
		paths.append(relative)
	return paths


def scan_tree() -> list:
	"""The same scan over every shipped module. Test code is not scanned."""
	found = []
	for relative in _scanned_paths():
		source = (_SOURCE_ROOT.parent / relative).read_text(encoding="utf-8")
		found.extend(scan_source(source, relative))
	return found


def _key(finding) -> tuple:
	return (finding["where"], finding["expression"])


# ── the allowlist ───────────────────────────────────────────────────────────
#
# Keyed on (file, expression) rather than on a line number, so a peer editing the
# file above one of these does not invalidate the entry — but CHANGING the line
# itself does, which is the point. Every entry states why the zero is not
# recoverable there. "Stored column" is the common reason and it is a fact about
# the schema, not an opinion: a Frappe Int/Float/Percent has no empty state.

ALLOWLIST = {
	# ── stored columns: unset and explicitly-zero are the same bytes ──────────
	(
		"erpnext_mcp/alerts/engine.py",
		"int(candidate.get(window_field) or 0) or warning_days",
	): "Stored Int on the alert rule. An unset window reads back as 0, so the zero "
	"cannot be told from 'never configured' and warning_days is the only sane answer.",
	(
		"erpnext_mcp/alerts/rules.py",
		"int(row.get(window_field) or 0) or default_window",
	): "Stored Int on the rule row — same schema fact as alerts/engine.py above.",
	(
		"erpnext_mcp/erpnext_mcp/doctype/strategic_plan/strategic_plan.py",
		"int(self.version or 1) or 1",
	): "A document field on the doc itself. Version 0 is not a version this app ever "
	"writes; the first save is 1 and the `or` is what makes that true on an unset field.",
	(
		"erpnext_mcp/payroll_gl.py",
		'int(_as_float(row.get("task_count"), 1.0)) or 1',
	): "Stored Int on a payroll attribution row. A bucket with a zero task count would "
	"divide by zero downstream; the 1 is a floor, not a default.",
	(
		"erpnext_mcp/tools/evidence.py",
		'int(row.get("renewal_window_days") or 0) or 90',
	): "Stored Int on the certification row, with no doctype default — an unset window "
	"reads back as 0 and 90 days is the shipped policy for one that was never set.",
	(
		"erpnext_mcp/tools/garnishments.py",
		'float( row.get("max_disposable_earnings_percentage") or 0 ) or STATUTORY_CEILING.get(kind)',
	): "Stored Percent on the garnishment. A 0 here would withhold nothing and silently "
	"defeat a court order, so the statutory ceiling is the floor rather than the default.",
	(
		"erpnext_mcp/tools/ml_model.py",
		'int(file_doc.get("file_size") or 0) or len(content)',
	): "Frappe's own File.file_size, which is 0 for a file attached before the field was "
	"populated. len(content) is the measurement, not a default.",
	(
		"erpnext_mcp/tools/sales.py",
		'float(row.get("gross_amount") or 0) or round(qty * stated_rate, 2)',
	): "VERIFIED, not assumed: `gross_amount` is a Currency with doctype default '0' on the "
	"Settlement Line Item child table, and `rows` comes from receipts._lines_out(doc), which "
	"reads doc['line_items'] — a stored child table, not a caller's argument. An unset "
	"gross_amount and an explicit 0 are therefore the same bytes. This is the settlement "
	"twin of the create_sales_invoice reprice fixed in v0.134.0, but that one reads a "
	"caller's items[] dict and this one reads a column, which is the whole difference. The "
	"fallback also does real work here — a weighed-but-unpriced line gets qty * rate — and a "
	"line that really is worth nothing is refused by the `amount <= 0` guard just below.",
	(
		"erpnext_mcp/document_intel.py",
		'_as_float(entry.get("qty")) or _as_float(entry.get("quantity"))',
	): "Two spellings of one extracted field, not a value and its default: whichever the "
	"extractor populated is the answer, and neither is a caller's argument.",
	# ── the `or` turned out to be load-bearing ───────────────────────────────
	(
		"erpnext_mcp/budget_engine.py",
		'_as_float(row.get("threshold_pct"), DEFAULT_THRESHOLD_PCT) or DEFAULT_THRESHOLD_PCT',
	): "Column class, and the zero is not recoverable: threshold_pct is a Percent with a "
	"doctype default of '10' on BOTH budget_line_item.json and budget_kpi_target.json, so a "
	"stored 0 and a never-set field are the same bytes. Its description also defines "
	"behaviour only for positive values ('Warning at 1-2x this number') so what a stored 0 "
	"MEANS is undefined by the product — giving it one is a schema decision, not a patch. "
	"SEPARATELY: these two `or`s are TOGETHER the only divide-by-zero guard on "
	"`ratio = abs(pct) / threshold` at check_budget_variances. Measured, not assumed — "
	"removing EITHER alone is safe, removing BOTH raises ZeroDivisionError. Neither is "
	"individually load-bearing; the pair is. (An earlier version of this reason claimed "
	"this entry alone raised. It does not: this line is in compute_budget_actuals, which "
	"contains no division at all.) NOTE no test in test_budget.py sets threshold_pct to 0, "
	"so the suite cannot see any of this — a direct probe is the only thing that answers it.",
	(
		"erpnext_mcp/budget_engine.py",
		'_as_float(row.get("threshold_pct"), threshold_default) or threshold_default',
	): "Same Percent column, same undefined meaning for a stored 0 — see the entry above. "
	"This is the half that sits beside the division, but removing it alone is still safe, "
	"because the entry above has already replaced the stored 0 with the default upstream "
	"and this line never sees a zero on that path. The two only matter together.",
	(
		"erpnext_mcp/tools/dispatch.py",
		'as_int(args, "actual_duration_minutes") or active_minutes(doc)',
	): "An argument, but the schema cannot carry the fix. actual_duration_minutes is an "
	"Int with no default, and the READ path at dispatch.py deliberately maps 0 -> None to "
	"mean 'not recorded'. A stated zero and a never-set field are the same stored value, "
	"so the distinction this would preserve does not survive the round trip. Found and "
	"reverted in v0.132.0.",
	# ── owned by another session's in-flight work ────────────────────────────
	(
		"erpnext_mcp/tools/mobile.py",
		'as_int(args, "token_expiry_days", DEFAULT_TOKEN_REVIEW_DAYS) or DEFAULT_TOKEN_REVIEW_DAYS',
	): "Not triaged here: tools/mobile.py is another session's claimed file. Listed so the "
	"scan stays green for them rather than silently excused — retriage when that lands.",
	(
		"erpnext_mcp/tools/mobile.py",
		'as_int(args, "expiry_days", DEFAULT_TOKEN_REVIEW_DAYS) or DEFAULT_TOKEN_REVIEW_DAYS',
	): "Not triaged here: tools/mobile.py is another session's claimed file. Two sites "
	"share this expression. Retriage when that work lands.",
	#
	# NOT LISTED, DELIBERATELY: erpnext_mcp/remittance.py carries
	# `float(wage_base or 0) or FUTA_WAGE_BASE`, but that file is UNTRACKED and has been
	# disclaimed by every session that has looked at it. It is in no committed tree, so
	# the scan never sees it and an entry for it would be permanently stale. This was
	# caught by running the suite against the EXTRACTED commit tree rather than the
	# shared working tree — the only place the difference shows. If it is ever
	# committed the scan will flag it, and whoever lands it gets to triage it.
}


class TheScannerItself(unittest.TestCase):
	"""A scan that finds nothing must be shown to be capable of finding something."""

	def test_it_flags_a_default_carrying_coercion_that_drops_a_zero(self):
		found = scan_source('hours = as_int(args, "expiry_hours", DEFAULT) or DEFAULT\n')
		self.assertEqual(len(found), 1)
		self.assertEqual(found[0]["helper"], "as_int")
		self.assertEqual(found[0]["shape"], "with-default")

	def test_it_flags_a_no_default_coercion_and_says_which_shape(self):
		"""The distinction that decides the fix, and the one that no-ops if missed."""
		found = scan_source('amount = as_float(entry.get("amount"), key) or round(qty * rate, 2)\n')
		self.assertEqual(len(found), 1)
		self.assertEqual(found[0]["shape"], "NO-DEFAULT")

	def test_it_ignores_the_harmless_identity_case(self):
		"""`x or 0` answers 0 for absent AND for zero. 78 sites in this tree are this."""
		self.assertEqual(scan_source('n = as_int(args, "limit") or 0\n'), [])
		self.assertEqual(scan_source('n = as_float(v, "k") or 0.0\n'), [])
		self.assertEqual(scan_source("n = cint(v) or None\n"), [])

	def test_or_none_is_flagged_only_when_the_call_supplies_a_nonzero_default(self):
		"""The precision that separates a real hazard from 51 harmless sites.

		`as_int(args, "x", 5) or None` answers 5 for absent and None for an explicit
		0 — two different values, so the `or` IS the mechanism. `_money(x) or None`
		answers None either way, because the helper's own default is already zero.
		Reading the helper NAME cannot tell these apart; only the call site can.
		"""
		self.assertEqual(len(scan_source('n = as_int(args, "x", 5) or None\n')), 1)
		self.assertEqual(len(scan_source('n = as_int(args, "x", default=5) or None\n')), 1)
		self.assertEqual(scan_source('n = _money(item.get("price")) or None\n'), [])
		self.assertEqual(scan_source('n = as_int(args, "x", 0) or None\n'), [])
		self.assertEqual(scan_source('n = as_int(args, "x") or None\n'), [])

	def test_an_unreadable_default_is_assumed_nonzero(self):
		"""Erring toward flagging: one allowlist entry costs a sentence, a false
		negative costs another decade of this bug."""
		self.assertEqual(len(scan_source('n = as_int(args, "x", SOME_CONSTANT) or None\n')), 1)

	def test_the_key_argument_is_never_mistaken_for_the_default(self):
		"""`as_int` is (args, key, default) and `_as_float` is (value, default). Reading
		index 1 on an as_int call would see the KEY, a non-zero string, and flag the lot."""
		self.assertEqual(scan_source('n = as_int(args, "some_key_name") or None\n'), [])

	def test_it_ignores_an_or_that_is_not_over_a_coercion(self):
		self.assertEqual(scan_source('name = args.get("company") or DEFAULT_COMPANY\n'), [])

	def test_it_sees_through_an_attribute_chain(self):
		"""`frappe.utils.cint(x)` is the same hazard as a bare `cint(x)`."""
		found = scan_source("seq = frappe.utils.cint(entry.get('sequence')) or index * 10\n")
		self.assertEqual(len(found), 1)
		self.assertEqual(found[0]["helper"], "cint")

	def test_a_three_branch_chain_is_flagged_once_when_any_branch_can_swallow_the_zero(self):
		found = scan_source('y = as_int(args, "year") or row.get("year") or 2025\n')
		self.assertEqual(len(found), 1)

	def test_a_three_branch_chain_of_pure_identity_is_still_ignored(self):
		self.assertEqual(scan_source('y = as_int(args, "year") or 0 or 0\n'), [])

	def test_the_expression_key_is_stable_under_line_drift(self):
		"""The allowlist is keyed on this, so a peer's edit above a site must not move it."""
		one = scan_source('x = as_int(args, "k", D) or D\n')
		two = scan_source('\n\n# a comment somebody added\nx = as_int(args, "k", D) or D\n')
		self.assertNotEqual(one[0]["line"], two[0]["line"])
		self.assertEqual(one[0]["expression"], two[0]["expression"])

	def test_the_exemplar_in_files_py_is_not_flagged(self):
		"""`_resolve_max_bytes` is how this is supposed to look, and has been since v0.100."""
		flagged = [row for row in scan_tree() if row["where"] == "erpnext_mcp/tools/files.py"]
		self.assertEqual(flagged, [])

	def test_the_scan_actually_reaches_the_whole_package(self):
		"""MEASURE THE SCAN'S OWN COVERAGE, because every clean result above is
		indistinguishable from a scan that read nothing.

		`scan_tree` filters to git-tracked modules so an untracked file in somebody's
		working tree cannot fail the build. A filter is exactly the kind of thing that
		quietly narrows to nothing — a wrong path prefix, a git that answers oddly —
		and every 'not flagged' assertion would then pass for the wrong reason.
		"""
		scanned = {path for path in _scanned_paths()}
		self.assertGreater(len(scanned), 200, "the scan is reaching far too few modules")
		for expected in (
			"erpnext_mcp/tools/files.py",
			"erpnext_mcp/tools/sales.py",
			"erpnext_mcp/budget_engine.py",
			"erpnext_mcp/args.py",
		):
			self.assertIn(expected, scanned)

	def test_an_untracked_module_is_left_out_rather_than_failing_the_build(self):
		"""The reason the filter exists. Pinned so nobody removes it as dead weight."""
		tracked = _tracked_modules()
		if tracked is None:
			self.skipTest("no git here — the archive path scans everything, which is the same set")
		self.assertNotIn("erpnext_mcp/remittance.py", _scanned_paths())


class TheTreeIsClean(unittest.TestCase):
	def test_no_coercion_drops_an_explicit_zero_outside_the_allowlist(self):
		unexpected = [row for row in scan_tree() if _key(row) not in ALLOWLIST]
		if unexpected:
			lines = "\n".join(
				f"  {row['where']}:{row['line']}  [{row['shape']}]\n      {row['expression']}"
				for row in unexpected
			)
			self.fail(
				f"{len(unexpected)} coercion(s) drop an explicit zero:\n{lines}\n\n"
				"An explicit 0 is a distinct value for an ARGUMENT, and this idiom replaces "
				"it with the default silently and reports success.\n\n"
				"  with-default (as_int): delete the trailing `or` — the default parameter "
				"already covers absent.\n"
				"  NO-DEFAULT (as_float, cint, int): the helper answers 0 for absent AND for "
				"zero, so deleting the `or` changes the absent case too. Branch on the RAW "
				"value:\n"
				'      raw = entry.get("amount")\n'
				'      amount = as_float(raw, key) if raw not in (None, "") else <fallback>\n\n'
				"If the value is a STORED Frappe Int/Float/Percent rather than an argument, "
				"the zero is not recoverable — an unset column reads back as 0 — so add it to "
				"ALLOWLIST in this file WITH ITS REASON instead of patching it."
			)


class TheAllowlist(unittest.TestCase):
	"""An allowlist nobody re-checks becomes a list of things nobody dares touch."""

	def test_every_entry_still_matches_a_real_site(self):
		live = {_key(row) for row in scan_tree()}
		stale = sorted(key for key in ALLOWLIST if key not in live)
		if stale:
			lines = "\n".join(f"  {where}\n      {expression}" for where, expression in stale)
			self.fail(
				f"{len(stale)} ALLOWLIST entr(ies) no longer match any code:\n{lines}\n\n"
				"This is usually GOOD NEWS — somebody fixed the site or deleted it. Remove "
				"the entry from ALLOWLIST in this file. It is reported rather than ignored "
				"because a stale entry would go on excusing whatever expression moves into "
				"that name next."
			)

	def test_every_entry_states_a_reason(self):
		for key, reason in ALLOWLIST.items():
			with self.subTest(site=key[0]):
				self.assertGreater(len(reason.strip()), 40, f"{key[0]} is allowlisted without a real reason")

	def test_the_allowlist_suppresses_only_by_its_stated_key(self):
		"""A control on the CONTROL: prove an entry is matched exactly, not by filename.

		If the key were only the path, any new zero-dropping line in an allowlisted
		file would be excused for free — the failure mode an allowlist is most
		prone to, and one no green run would ever reveal.
		"""
		allowlisted_file = "erpnext_mcp/budget_engine.py"
		self.assertTrue(any(where == allowlisted_file for where, _ in ALLOWLIST))
		invented = scan_source('x = as_int(args, "brand_new_argument", D) or D\n', where=allowlisted_file)
		self.assertEqual(len(invented), 1)
		self.assertNotIn(_key(invented[0]), ALLOWLIST)


# ── the behaviour the scan is a proxy for ───────────────────────────────────
#
# The scan above is a shape check: it proves the idiom is gone, not that anything
# behaves better. These are the four sites where a dropped zero reached something
# a person would act on, tested through the tools rather than through the scanner.
#
# EACH ONE COMES IN A PAIR, deliberately. "The zero survives" alone is satisfiable
# by deleting the fallback along with the `or`, which would break every caller who
# omits the argument — a regression no assertion here would have caught. So every
# claim below also asserts that an ABSENT value still takes the default it always
# did. The pair is the test; either half alone is a trap.
#
# The base classes are referenced through their modules rather than imported by
# name, so unittest does not collect another module's cases a second time here.

from . import test_assets, test_expenses, test_mutate_tools, test_sales  # noqa: E402
from .fixtures import MAIN, MASTER_CUSTOMER, cash, sales  # noqa: E402


class TheInvoiceThatRepricedAComp(test_sales.SalesTestCase):
	"""`create_sales_invoice` — the headline site, and NOT for the reason it was reported.

	sales.py carried the flagged idiom, `as_float(entry.get("amount"), ...) or
	round(qty * rate, 2)`, and it really did invent a figure for a stated zero. But
	removing it did not stop the reprice, and the test that was supposed to prove
	the fix FAILED. `SalesInvoiceDocument.validate` recomputes `amount` from qty ×
	rate on every validate — real ERPNext does this, and the harness models it on
	purpose — so whatever `_manual_lines` writes is overwritten before anybody can
	read it.

	THE CONTROL THAT SETTLED IT is `test_a_stated_amount_is_honoured_even_when_it_is
	_not_the_multiplication`: a stated 615.50 was discarded exactly like a stated 0.
	A value that is not a zero cannot be a victim of a zero-dropping idiom, so the
	`or` was never the mechanism. Without that control this module would have
	shipped a green test over an unfixed bug.

	The real fix is the one `_settlement_lines_to_invoice_lines` has always used for
	a packer's stated gross: derive `rate = amount / qty` so the product survives
	validate. The manual path now does the same and REPORTS the adjustment.

	NOTE THE RATE IN THE FIXTURE. With `rate: 0` the old fallback computed
	`round(qty * 0, 2)` = 0, the same answer the fix gives, so a test written that
	way passes against unfixed code. It takes a non-zero rate for the two to differ.
	"""

	def setUp(self):
		super().setUp()
		self.line = {"item_code": "SURROUND-WP", "qty": 1000, "rate": 0.62}

	def invoice(self, **line):
		self.created = self.tool_data(
			"create_sales_invoice",
			{
				"customer": MASTER_CUSTOMER,
				"company": MAIN,
				"posting_date": "2026-10-01",
				"due_date": "2026-10-31",
				"items": [{**self.line, **line}],
			},
		)
		return self.tool_data("get_sales_invoice", {"sales_invoice": self.created["name"]})

	def test_a_line_explicitly_comped_to_zero_is_not_repriced(self):
		got = self.invoice(amount=0)
		self.assertEqual(got["items"][0]["amount"], 0.0)
		self.assertEqual(got["grand_total"], 0.0)

	def test_the_comp_is_carried_by_the_rate_because_the_amount_cannot_survive(self):
		"""The mechanism, asserted directly: qty is untouched and the RATE went to zero."""
		got = self.invoice(amount=0)
		self.assertEqual(got["items"][0]["qty"], 1000.0)
		self.assertEqual(got["items"][0]["rate"], 0.0)

	def test_a_line_with_no_amount_still_takes_qty_times_rate(self):
		"""The other half: the fix must not be satisfiable by dropping the fallback."""
		got = self.invoice()
		self.assertEqual(got["items"][0]["amount"], 620.0)
		self.assertEqual(got["items"][0]["rate"], 0.62)
		self.assertEqual(self.created["rate_adjustments"], [])

	def test_a_stated_amount_is_honoured_even_when_it_is_not_the_multiplication(self):
		"""THE CONTROL that caught the wrong diagnosis. 615.50 is not a zero, and it
		was being discarded just the same — which is what proved the `or` innocent."""
		got = self.invoice(amount=615.5)
		self.assertEqual(got["items"][0]["amount"], 615.5)

	def test_an_adjusted_rate_is_reported_rather_than_changed_quietly(self):
		self.invoice(amount=615.5)
		adjusted = self.created["rate_adjustments"]
		self.assertEqual(len(adjusted), 1)
		self.assertEqual(adjusted[0]["stated_rate"], 0.62)
		self.assertEqual(adjusted[0]["rate"], 0.6155)
		self.assertIn("RATE was adjusted", adjusted[0]["note"])

	def test_a_negative_amount_is_refused(self):
		"""Deriving the rate from the amount would otherwise smuggle in a negative rate,
		which the `rate < 0` refusal above catches only for a rate stated directly."""
		message = self.tool_error(
			"create_sales_invoice",
			{
				"customer": MASTER_CUSTOMER,
				"company": MAIN,
				"posting_date": "2026-10-01",
				"due_date": "2026-10-31",
				"items": [{**self.line, "amount": -5}],
			},
		)
		self.assertIn("amount cannot be negative", message)


class TheExchangeRateGuardThatCouldNotFire(test_mutate_tools.SeededTestCase):
	"""`create_journal_entry` — a refusal that named the one value it could not see."""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **test_mutate_tools.ALL_ON)

	def entry(self, **line):
		return {
			"company": MAIN,
			"posting_date": "2026-03-01",
			"user_remark": "Zero-drop",
			"accounts": [
				{"account": cash(), "debit": 10, **line},
				{"account": sales(), "credit": 10},
			],
		}

	def test_an_exchange_rate_of_zero_is_refused_by_the_guard_that_names_it(self):
		message = self.tool_error("create_journal_entry", self.entry(exchange_rate=0))
		self.assertIn("exchange_rate", message)
		self.assertIn("must be", message)

	def test_a_negative_exchange_rate_is_still_refused(self):
		"""This half always worked — asserted so a fix cannot trade one for the other."""
		message = self.tool_error("create_journal_entry", self.entry(exchange_rate=-1))
		self.assertIn("exchange_rate", message)

	def test_an_absent_exchange_rate_still_posts_at_par(self):
		data = self.tool_data("create_journal_entry", self.entry())
		self.assertTrue(data["name"])


class TheDepreciationFrequencyOfNoMonths(test_assets.AssetTestCase):
	"""`create_asset` — `if frequency <= 0` sat below a line that made 0 impossible."""

	def test_a_depreciation_frequency_of_zero_is_refused(self):
		message = self.tool_error(
			"create_asset", self.create(useful_life_months=12, depreciation_frequency_months=0)
		)
		self.assertIn("depreciation_frequency_months", message)

	def test_an_absent_frequency_still_takes_the_monthly_default(self):
		"""The other half: deleting the default along with the `or` would pass the
		refusal test above and break every caller who omits the argument."""
		data = self.an_asset(useful_life_months=12)
		self.assertEqual(data["period_count"], 12)


class TheLimitThatMeantEverything(test_expenses.ExpenseTestCase):
	"""`list_expense_receipts` — where a dropped zero was hiding a WORSE bug.

	`limit: 0` became 100, which looked harmless. But the value was on its way to
	Frappe's `limit_page_length`, where 0 does not mean "no rows" — it means NO
	LIMIT. So the obvious fix here, deleting the `or 100`, would have turned a
	caller's 0 into an unbounded table scan: strictly worse than the bug it
	replaced, and green on every existing test.

	Routing through `as_limit` is what makes it safe. Its docstring has said so
	since it was written: "An explicit 0 clamps to 1 rather than falling back to
	the default."
	"""

	def receipts(self, **args):
		for day in ("2026-06-14", "2026-06-15", "2026-06-16"):
			self.capture(receipt_date=day)
		return self.tool_data("list_expense_receipts", {"company": MAIN, **args})["receipts"]

	def test_a_limit_of_zero_returns_one_row_and_not_the_whole_table(self):
		self.assertEqual(len(self.receipts(limit=0)), 1)

	def test_an_absent_limit_still_returns_everything_under_the_default_page(self):
		self.assertEqual(len(self.receipts()), 3)

	def test_a_stated_limit_is_still_honoured(self):
		self.assertEqual(len(self.receipts(limit=2)), 2)


if __name__ == "__main__":
	unittest.main()
