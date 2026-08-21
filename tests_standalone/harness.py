# SPDX-License-Identifier: MIT
"""A Frappe stand-in, so this app's logic can be tested without a bench.

WHY THIS EXISTS. The tests that matter most here are about *refusal*: does a
disabled tool stay invisible, does a bad token get an opaque 401, does an
unbalanced Journal Entry get rejected before anything is written, does a failed
mutation still leave an audit row. Every one of those is answerable from the
app's own logic, and none of them needs MariaDB, redis or a site. Requiring a
full bench to run them means they get run rarely, which for refusal tests is the
same as not having them.

So this module installs an in-memory `frappe` into `sys.modules` before the app
is imported: a small document store with real filter semantics, a real doctype
meta (loaded from the app's own shipped DocType JSON, so the tests assert against
the defaults that actually ship), and the handful of framework functions the app
touches. It is a test double, not an emulator — it implements what this app uses
and nothing else. Reaching for a framework function it does not have raises an
AttributeError naming the missing function (see the module `__getattr__` in
`_build_frappe`), so the double can never quietly return None for something the
real framework would have answered.

WHAT IT DELIBERATELY DOES NOT PROVE. Whether ERPNext's Journal Entry validation
accepts a given posting date. Whether `add_payment_entries` exists on this
version's Bank Transaction. Whether the DocType JSON migrates. Those are
integration facts about a real site, and they belong to the FrappeTestCase suite
in `erpnext_mcp/tests/`, which runs inside a bench. The two suites are not
alternatives — this one is fast and covers logic, that one is slow and covers the
framework contract.

Import this module before importing anything from `erpnext_mcp`.
"""

from __future__ import annotations

import copy
import datetime
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import traceback
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCTYPE_DIR = os.path.join(REPO_ROOT, "erpnext_mcp", "erpnext_mcp", "doctype")
REPORT_DIR = os.path.join(REPO_ROOT, "erpnext_mcp", "erpnext_mcp", "report")

#: A real directory standing in for the site folder, so `frappe.get_site_path`
#: answers with somewhere a tool can genuinely write. The report generators take
#: an `output_path` and confine it to the site's own files directories; a double
#: that returned a fictional path would let the confinement logic be tested and
#: the writing not be, which is the half that corrupts something.
SITE_ROOT = tempfile.mkdtemp(prefix="erpnext-mcp-site-")

#: Subdirectories of the site folder the app is allowed to write into.
SITE_FILE_DIRS = (("private", "files"), ("public", "files"))

#: What a MariaDB DATETIME or DATE column accepts as a string. The `T`
#: separator is tolerated because the server tolerates it; a trailing `Z` or a
#: `+02:00` offset is not, because the column has nowhere to put a zone — see
#: `Document._validate_datetimes`.
_MARIADB_DATETIME = re.compile(
	r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
	r"(?:[ T](?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?(?:\.\d+)?)?$"
)


def get_site_path(*parts) -> str:
	return os.path.join(SITE_ROOT, *[str(part) for part in parts])


def reset_site_files() -> None:
	"""Empty the fake site's file directories between tests, and recreate them."""
	for parts in SITE_FILE_DIRS:
		path = get_site_path(*parts)
		shutil.rmtree(path, ignore_errors=True)
		os.makedirs(path, exist_ok=True)


if REPO_ROOT not in sys.path:
	sys.path.insert(0, REPO_ROOT)


# ── the dict-with-attributes Frappe passes everywhere ───────────────────────
class FrappeDict(dict):
	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError:
			return None

	def __setattr__(self, key, value):
		self[key] = value

	def __delattr__(self, key):
		self.pop(key, None)


# ── exceptions ──────────────────────────────────────────────────────────────
class ValidationError(Exception):
	pass


class DoesNotExistError(ValidationError):
	pass


class PermissionError_(ValidationError):
	pass


class LinkValidationError(ValidationError):
	"""What Frappe raises for a Link pointing at something that is not there.

	Modelled because v0.12.0 shipped a `bench migrate` that died on it and this
	suite passed the whole way. `Party Type` names itself after its `party_type`
	field, and that field is a **Link to DocType** — so registering a party type
	called "Family" requires a DocType called "Family" to exist. There was none,
	`_validate_links()` refused the insert, and the patch took the migration down
	with it.

	The double had no link validation at all, so it inserted the row happily.
	That is the same shape of failure as the Account `MandatoryError` below: a
	test double that answers a question the real framework refuses is a double
	that certifies code which cannot run. See `Document._validate_links`.
	"""


class LinkExistsError(ValidationError):
	"""What Frappe raises when a delete is refused because something links to it.

	MODELLED IN v0.83.0, AND THE GAP IT CLOSES IS THE SAME SHAPE AS
	`LinkValidationError`'s. Real `frappe.model.delete_doc` runs
	`check_if_doc_is_linked` and refuses to delete any document a Link field
	points at; this double used to pop the row out of the table and say nothing.
	So the suite could certify "the archived card was deleted" against a
	framework that would have refused, and — worse for the release this arrived
	in — the FIX for that refusal was untestable, because there was no refusal to
	fix.

	`force=True` skips the check, as it does in Frappe, and `on_trash` runs first,
	as it does in Frappe. Both matter: the app's own uninstall paths pass
	`force=True` deliberately, and `Governance Document.on_trash` releases its
	archive links in the window between the two.

	THIS IS A FIDELITY INCREASE AND IT CAN ONLY MAKE TESTS STRICTER. A test that
	starts failing here was passing on a delete a real bench would have refused.
	"""


class MandatoryError(ValidationError):
	"""What Frappe raises for an empty `reqd` field, and what the app has to dodge.

	Modelled because it is the exact failure a live site hit: ERPNext's Account
	marks `parent_account` required, so creating a new *root* account — which by
	definition has no parent — dies with
	`MandatoryError: [Account, 1000 - Assets - ABC]: parent_account` before any of
	this app's own logic runs. The double used to insert roots quite happily,
	which is precisely why the standalone suite passed against code that could not
	create one. See `AccountDocument.validate`.
	"""


# ── schema ──────────────────────────────────────────────────────────────────
#: Field lists for the ERPNext doctypes this app reads. Only the fields the app
#: actually selects need to be here; `compat.existing_fields` filters against
#: this, which is the same thing it does against a real site's meta.
ERPNEXT_SCHEMA = {
	"Company": [
		"name",
		"abbr",
		"default_currency",
		"country",
		"chart_of_accounts",
		"parent_company",
		"is_group",
		"tax_id",
		"cost_center",
		"company_logo",
		# The default-account fields `set_company_defaults` writes. Listed here
		# rather than invented per test, because the tool asks the site whether
		# each one exists and a fixture that answered "yes" to everything would
		# make the version-tolerance path untestable.
		"default_receivable_account",
		"default_payable_account",
		"default_cash_account",
		"default_bank_account",
		"default_income_account",
		"default_expense_account",
		"cost_of_goods_sold_account",
		"round_off_account",
		"round_off_cost_center",
		"exchange_gain_loss_account",
		"write_off_account",
		"default_deferred_revenue_account",
		# v0.8.0 added thirteen more supported defaults. Six are here — enough to
		# exercise every new shape of rule: a P&L account with no type constraint
		# (disposal_account), an Asset with a required type
		# (capital_work_in_progress_account), an Expense, two Liabilities of which
		# one is the counter-intuitive Receivable-typed advance account, and a cost
		# center.
		"disposal_account",
		"capital_work_in_progress_account",
		"stock_adjustment_account",
		"stock_received_but_not_billed",
		"default_advance_received_account",
		"default_selling_cost_center",
		# No `default_deferred_expense_account`, and none of the other seven
		# v0.8.0 keys: supported defaults this fixture's ERPNext does not have, so
		# the "your version has no such field" refusal is exercised by a real
		# absence rather than a mock.
	],
	"Account": [
		"name",
		"account_name",
		"account_number",
		"parent_account",
		"is_group",
		"root_type",
		"report_type",
		"account_type",
		"account_currency",
		"tax_rate",
		"disabled",
		"freeze_account",
		"lft",
		"rgt",
		"company",
	],
	# No `description`: stock ERPNext's Account has none, which is why
	# `tools.accounts` falls back to a comment. A site that added the custom
	# field is a separate case, and its test adds the field deliberately.
	"Currency": ["name", "enabled"],
	# `Party Type` is what a GL Entry's `party_type` points at, and v0.12.0 adds
	# two of its own to it. `Country` is here so `create_company`'s ISO check has
	# something real to refuse against rather than a mock that always agrees.
	"Party Type": ["name", "party_type", "account_type"],
	"Country": ["name", "code"],
	# `Contact` is CORE FRAPPE, and it is here because v0.12.1 needs the fixture
	# to know that. A Party Type's name has to be a DocType — so `Contact`
	# registers on a real site precisely because Frappe ships this, and `Family`
	# did not until this app started shipping one. A fixture that omitted Contact
	# would make the two look alike and hide the whole distinction.
	"Contact": ["name", "first_name", "last_name", "email_id", "company_name"],
	"GL Entry": [
		"name",
		"account",
		"posting_date",
		"debit",
		"credit",
		"company",
		"is_cancelled",
		"voucher_type",
		"voucher_no",
		# The Journal Entry Account row this GL row came from. It is what makes
		# "update the party on line 2" exact rather than approximate: an entry
		# with two lines to the same account for the same amount produces two GL
		# rows that differ in nothing else, and a fixture without this column
		# would let `update_journal_entry_party` match both and look correct.
		"voucher_detail_no",
		"party",
		"party_type",
		"cost_center",
		# `is_opening` is "Yes"/"No" on a GL Entry, not a Check. The 1099
		# pre-fill excludes opening entries from payments, and a fixture without
		# the column would make that exclusion untestable.
		"is_opening",
	],
	"Cost Center": [
		"name",
		"cost_center_name",
		"cost_center_number",
		"parent_cost_center",
		"is_group",
		"disabled",
		"company",
		"lft",
		"rgt",
	],
	"Accounting Dimension": ["name", "label", "fieldname", "document_type", "disabled"],
	"Module Def": ["name", "app_name"],
	"Journal Entry": [
		"name",
		"posting_date",
		"company",
		"voucher_type",
		"naming_series",
		"total_debit",
		"total_credit",
		"difference",
		"user_remark",
		"remark",
		"cheque_no",
		"cheque_date",
		"bill_no",
		"bill_date",
		"finance_book",
		"docstatus",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"is_opening",
		"clearance_date",
		"mode_of_payment",
		"multi_currency",
		"accounts",
	],
	"Journal Entry Account": [
		# A child row has a docname of its own, and it is not decoration: a GL
		# Entry points back at it through `voucher_detail_no`, and that pointer is
		# the only thing distinguishing two identical lines of one voucher.
		"name",
		"idx",
		"account",
		"account_type",
		"party_type",
		"party",
		"debit",
		"credit",
		"debit_in_account_currency",
		"credit_in_account_currency",
		"account_currency",
		"exchange_rate",
		"against_account",
		"cost_center",
		"project",
		"reference_type",
		"reference_name",
		"reference_due_date",
		# THE FIELD THAT CAUSED v0.14.0's FEATURE E. ERPNext's
		# `JournalEntry.get_gl_entries` fills `GL Entry.voucher_detail_no` from
		# THIS field, not from the line's docname — it names a payment schedule
		# row on an invoice being settled and is empty on an ordinary line. The
		# fixture had no such column, so nothing here could express the truth
		# that a Journal Entry's GL rows carry no line docname at all.
		"reference_detail_no",
		"user_remark",
		"is_advance",
		"parent",
		"parenttype",
		"bank_account",
	],
	# `year` is the field a Fiscal Year names itself from, so it is the one
	# `create_fiscal_year` sets — a double without it would let the tool name a
	# year by writing `name` directly, which is not what a real insert does.
	"Fiscal Year": [
		"name",
		"year",
		"year_start_date",
		"year_end_date",
		"disabled",
		"is_short_year",
		"auto_created",
		"companies",
	],
	"Fiscal Year Company": ["parent", "parenttype", "company"],
	# ERPNext splits the institution from the account at it. Both are here because
	# `create_bank_account` writes both, and a double with only the second would
	# let the tool "succeed" while the Bank it claims to have created went nowhere.
	"Bank": ["name", "bank_name", "swift_number", "website"],
	"Bank Account": [
		"name",
		"account_name",
		"bank",
		"company",
		"account",
		"iban",
		"bank_account_no",
		"branch_code",
		"is_company_account",
		"is_default",
		"party_type",
		"party",
		"disabled",
	],
	"Bank Transaction": [
		"name",
		"date",
		"bank_account",
		"company",
		"description",
		"status",
		"reference_number",
		"currency",
		"party_type",
		"party",
		"bank_party_name",
		"docstatus",
		"deposit",
		"withdrawal",
		"allocated_amount",
		"unallocated_amount",
		"payment_entries",
	],
	"Bank Transaction Payments": [
		"payment_document",
		"payment_entry",
		"allocated_amount",
		"parent",
		"parenttype",
	],
	"Bank Statement": [
		"name",
		"bank_account",
		"from_date",
		"to_date",
		"opening_balance",
		"closing_balance",
		"company",
	],
	# ERPNext's check-cutting document. The v0.14.0 check Print Format renders
	# against it, so the fields it prints have to be here — a fixture that stopped
	# at the amount would let a template reference a column nobody has.
	"Payment Entry": [
		"name",
		"posting_date",
		"paid_amount",
		"received_amount",
		"total_allocated_amount",
		"unallocated_amount",
		"docstatus",
		"company",
		"payment_type",
		"party_type",
		"party",
		"party_name",
		"paid_from",
		"paid_from_account_currency",
		"paid_to",
		"paid_to_account_currency",
		"reference_no",
		"reference_date",
		"remarks",
		"mode_of_payment",
		"bank_account",
		"references",
	],
	"Payment Entry Reference": [
		"reference_doctype",
		"reference_name",
		"due_date",
		"total_amount",
		"outstanding_amount",
		"allocated_amount",
		"parent",
		"parenttype",
	],
	# Core Frappe. Only the columns `create_check_print_format` writes or reads —
	# `standard` above all, because a STANDARD format is one an app rewrites on
	# every migrate and the refusal that protects it is the point.
	"Print Format": [
		"name",
		"print_format_name",
		"doc_type",
		"module",
		"standard",
		"custom_format",
		"print_format_type",
		"print_format_builder",
		"disabled",
		"page_size",
		"margin_top",
		"margin_bottom",
		"margin_left",
		"margin_right",
		"align_labels_right",
		"line_breaks",
		"show_section_headings",
		"html",
	],
	# v0.17.0. The mobile account tools write every one of these, and `api_secret`
	# is a Password field on the real doctype — which matters, because the whole
	# "generate → works → revoke → fails" round trip is a question about whether
	# the value survives a save and stops existing after a revocation.
	"User": [
		"name",
		"email",
		"enabled",
		"full_name",
		"first_name",
		"last_name",
		"user_type",
		"api_key",
		"api_secret",
		"send_welcome_email",
		"roles",
		# v0.81.0. Both are standard Frappe columns and both are read by
		# `itgc.generate_access_control_report` through `compat.existing_fields`.
		# They are here because the double lacking them made the report answer
		# "this site does not record last login" for a field every real site has
		# — which is the double being wrong rather than the app, and it would
		# have made the dormant-login flags untestable.
		"last_login",
		"last_active",
	],
	# ── v0.17.0: the permission tables the six mobile roles are written into ──
	#
	# Modelled rather than stubbed because the ONE thing that could go wrong here
	# is a framework rule, not an app rule: Frappe discards every standard DocPerm
	# on a doctype the moment a single Custom DocPerm exists for it. `DocPerm` is
	# therefore a real child table of DocType, seeded from each doctype's own
	# shipped permissions, so `roles._mirror_standard_perms` has something real to
	# copy and a test can assert it copied.
	"Role": ["name", "role_name", "desk_access", "disabled", "is_custom"],
	"Has Role": ["name", "role", "parent", "parenttype", "parentfield", "idx"],
	"User Permission": [
		"name",
		"user",
		"allow",
		"for_value",
		"apply_to_all_doctypes",
		"applicable_for",
		"is_default",
		"hide_descendants",
	],
	"DocPerm": [
		"name",
		"parent",
		"parenttype",
		"parentfield",
		"idx",
		"role",
		"permlevel",
		"read",
		"write",
		"create",
		"delete",
		"submit",
		"cancel",
		"amend",
		"report",
		"export",
		"print",
		"email",
		"share",
		"if_owner",
	],
	"Custom DocPerm": [
		"name",
		"parent",
		"role",
		"permlevel",
		"read",
		"write",
		"create",
		"delete",
		"submit",
		"cancel",
		"amend",
		"report",
		"export",
		"print",
		"email",
		"share",
		"if_owner",
	],
	"DocType": [
		"name",
		"module",
		"issingle",
		"istable",
		"custom",
		"autoname",
		"naming_rule",
		"track_changes",
		"fields",
		"permissions",
	],
	"Singles": ["doctype", "field", "value"],
	# ── v0.2.0 ──────────────────────────────────────────────────────────────
	"Workflow": [
		"name",
		"workflow_name",
		"document_type",
		"is_active",
		"workflow_state_field",
		"send_email_alert",
		"override_status",
		"states",
		"transitions",
	],
	"Workflow Document State": [
		"state",
		"doc_status",
		"allow_edit",
		"update_field",
		"update_value",
		"is_optional_state",
		"message",
		"parent",
		"parenttype",
	],
	"Workflow Transition": [
		"state",
		"action",
		"next_state",
		"allowed",
		"allow_self_approval",
		"condition",
		"parent",
		"parenttype",
	],
	"Report": [
		"name",
		"report_name",
		"ref_doctype",
		"report_type",
		"module",
		"is_standard",
		"disabled",
		"prepared_report",
		"add_total_row",
		"json",
		"query",
	],
	"File": [
		"name",
		"file_name",
		"file_url",
		"file_size",
		"is_private",
		"is_folder",
		"attached_to_doctype",
		"attached_to_name",
		"attached_to_field",
		"content_hash",
		"folder",
		"owner",
		"creation",
	],
	"Comment": [
		"name",
		"comment_type",
		"content",
		"comment_by",
		"comment_email",
		"reference_doctype",
		"reference_name",
		"owner",
		"creation",
		"modified",
	],
	"ToDo": [
		"name",
		"status",
		"priority",
		"date",
		"description",
		"reference_type",
		"reference_name",
		"assigned_by",
		"allocated_to",
		"owner",
		"creation",
		"modified",
	],
	# v0.18.1 added the seven personal fields `create_employee` writes. They are
	# on the stock Frappe HR Employee and were missing here, so a tool that wrote
	# `gender` or `cell_number` would have had the value silently dropped by
	# `compat.has_field` and the suite would have called that a pass.
	"Employee": [
		"name",
		"employee_name",
		"employee_number",
		"first_name",
		# Standard on ERPNext's Employee. Absent from this double until v0.51.0,
		# which is part of why nobody noticed the middle name read off every
		# licence barcode had nowhere to land.
		"middle_name",
		"last_name",
		"gender",
		"date_of_birth",
		# Standard on ERPNext's Employee and absent from this double until
		# v0.51.0, which made the badge card's photograph untestable: `_card`
		# reads it through `existing_fields`, so the double answered "no such
		# column" and every test saw a card falling back to initials.
		"image",
		"personal_email",
		"cell_number",
		# v0.62.0 joined these three to `employee.WRITABLE` so the mobile
		# surface's `set_employee_contact_fields` had somewhere to put the two
		# halves of an emergency contact. All three are standard on ERPNext's
		# Employee; absent from this double they would be dropped silently by
		# `compat.has_field` and the suite would call that a pass — which is the
		# same trap `middle_name` and `image` are commented for above.
		"current_address",
		"person_to_be_contacted",
		"emergency_phone_number",
		"department",
		"designation",
		"status",
		"date_of_joining",
		"relieving_date",
		"company",
		"user_id",
		"reports_to",
		"branch",
		"employment_type",
		# v0.68.1. `Employee.grade` is Frappe HR's own pay-band Link, and it is
		# here for one reason: `tools/org.list_employee_grades` counts through it.
		# A column the fixture did not carry would have made every grade report a
		# headcount of zero, which reads as "nobody is on this band" rather than
		# as "this app asked a question the schema could not answer".
		"grade",
	],
	# The HR masters the Employee's Links point at. Present so the link
	# validation below is a real check rather than a skipped one — see
	# `_validate_links_on`, which does not validate a target doctype the fixture
	# has never heard of.
	# v0.68.1 adds `parent_department` and `disabled` — the two columns
	# `update_department` writes. Frappe HR's Department is a NestedSet and the
	# parent link is the tree; a fixture without it would have let the tool set a
	# field `compat.has_field` says is absent, silently, and the suite would have
	# called a no-op a pass.
	"Department": ["name", "department_name", "company", "is_group", "parent_department", "disabled"],
	"Designation": ["name", "designation_name", "description"],
	"Employment Type": ["name", "employee_type_name"],
	# v0.68.1. Frappe HR's pay band. PROMPT-NAMED on a stock site — there is no
	# column behind the docname, which is why `tools/org._name_column` asks this
	# site's meta before it believes the spec and why the fixture gives it none.
	# `default_base_pay` and `default_salary_structure` are deliberately ABSENT:
	# this app refuses to write them by name, and a fixture that carried them
	# would let a test assert a write that the tool must never make.
	"Employee Grade": ["name"],
	# v0.54.0. `Employee.branch` has been a column in this double since before
	# anything wrote it, modelled as Data with no master behind it — so the link
	# check below was skipped and `create_employee` would have accepted a branch
	# naming nothing. Frappe HR's Branch carries one column beside the docname.
	"Branch": ["name", "branch"],
	"Gender": ["name", "gender"],
	# v0.19.3 added the three span columns Frappe HR's own Attendance carries. The
	# shift-close bridge writes them — a worker who joined an hour late and left
	# two hours early gets an Attendance row for THEIR span, not the shift's — and
	# a fixture without the columns would have let `compat.has_field` drop the
	# values silently and the suite would have called that a pass.
	"Attendance": [
		"name",
		"employee",
		"employee_name",
		"attendance_date",
		"status",
		"department",
		"company",
		"in_time",
		"out_time",
		"working_hours",
		"docstatus",
	],
	"Leave Allocation": [
		"name",
		"employee",
		"leave_type",
		"from_date",
		"to_date",
		"new_leaves_allocated",
		"total_leaves_allocated",
		"docstatus",
	],
	"Leave Type": ["name", "max_leaves_allowed", "is_lwp"],
	"Sales Order": [
		"name",
		"customer",
		"customer_name",
		"transaction_date",
		"delivery_date",
		"grand_total",
		"rounded_total",
		"currency",
		"status",
		"per_delivered",
		"per_billed",
		"docstatus",
		"company",
		"owner",
	],
	# `supplier_type` is ERPNext's Company/Individual Select, and it is the field
	# the 1099 pre-fill classifies on. `tax_withholding_category` and `tax_id`
	# are here because the tool reads them off the site when they exist and says
	# so when they do not — a fixture that always had them would leave the
	# degraded path untested.
	"Supplier": [
		"name",
		"supplier_name",
		"supplier_type",
		"supplier_group",
		"tax_id",
		"tax_category",
		"tax_withholding_category",
		"country",
		"disabled",
		"is_transporter",
	],
	"Purchase Order": [
		"name",
		"supplier",
		"supplier_name",
		"transaction_date",
		"schedule_date",
		"grand_total",
		"rounded_total",
		"currency",
		"status",
		"per_received",
		"per_billed",
		"docstatus",
		"company",
		"owner",
		"workflow_state",
		"items",
	],
	# v0.68.0. Purchase Order's one child table. Registered here rather than left
	# to a test's own `register_doctype`, unlike Purchase Invoice below: nothing
	# in this suite depends on Purchase Order Item being ABSENT, so there is no
	# reason to make every purchasing test re-declare it.
	"Purchase Order Item": [
		"name",
		"idx",
		"item_code",
		"item_name",
		"description",
		"qty",
		"rate",
		"amount",
		"uom",
		"warehouse",
		"schedule_date",
		"received_qty",
		"billed_amt",
		"cost_center",
		"parent",
		"parenttype",
		"parentfield",
	],
	# v0.68.0. Goods received against a Supplier. Registered permanently — same
	# reasoning as Purchase Order Item above.
	"Purchase Receipt": [
		"name",
		"posting_date",
		"supplier",
		"supplier_name",
		"company",
		"currency",
		"status",
		"docstatus",
		"grand_total",
		"per_billed",
		"purchase_order",
		"items",
		"owner",
	],
	"Purchase Receipt Item": [
		"name",
		"idx",
		"item_code",
		"item_name",
		"qty",
		"received_qty",
		"rate",
		"amount",
		"warehouse",
		"purchase_order",
		"purchase_order_item",
		"cost_center",
		"parent",
		"parenttype",
		"parentfield",
	],
	# v0.70.0 widened this from the eleven columns `get_outstanding_invoices`
	# reads to what a Sales Invoice this app WRITES actually carries. The
	# `settlement_statement` link is deliberately NOT here: it is a Custom Field
	# `tools/sales.py` installs on first use, and a fixture that shipped it
	# would make the "this site has not got the field yet" degradation — which
	# every link in that module reports rather than assumes — unreachable.
	"Sales Invoice": [
		"name",
		"customer",
		"customer_name",
		"posting_date",
		"due_date",
		"debit_to",
		"net_total",
		"total_taxes_and_charges",
		"grand_total",
		"rounded_total",
		"outstanding_amount",
		"currency",
		"status",
		"company",
		"is_return",
		"remarks",
		"docstatus",
		"owner",
		"items",
		"taxes",
	],
	"Sales Invoice Item": [
		"name",
		"idx",
		"item_code",
		"item_name",
		"description",
		"qty",
		"uom",
		"rate",
		"amount",
		"income_account",
		"cost_center",
		"parent",
		"parenttype",
		"parentfield",
	],
	# ERPNext's charge table, and the shape a grower settlement's deductions
	# land in: `charge_type: "Actual"` with a NEGATIVE `tax_amount`.
	"Sales Taxes and Charges": [
		"name",
		"idx",
		"charge_type",
		"account_head",
		"description",
		"rate",
		"tax_amount",
		"parent",
		"parenttype",
		"parentfield",
	],
	# No "Purchase Invoice", deliberately: this fixture is a site that does not
	# have one, which is what makes the "that DocType is not installed"
	# degradation in the fiscal-year packet and in create_accounting_dimension
	# testable against a real absence. A test that needs it registers it.
	"Custom Field": [
		"name",
		"dt",
		"fieldname",
		"label",
		"fieldtype",
		"options",
		"insert_after",
		"idx",
		"reqd",
		"hidden",
		"read_only",
		"in_list_view",
		"in_standard_filter",
		"depends_on",
		"default",
		"description",
		"module",
		"owner",
		"modified",
	],
	# ── v0.15.0: what the Compliance Command Center is built out of ─────────
	#
	# Frappe's own dashboard doctypes. Modelled because `dashboard.py` builds
	# them on every migrate and the property that has to be true — that a second
	# migrate changes nothing — is only testable against something that records
	# what the first one wrote.
	"Dashboard": ["name", "dashboard_name", "is_default", "is_standard", "module", "charts", "cards"],
	"Dashboard Chart": [
		"name",
		"chart_name",
		"chart_type",
		"document_type",
		"based_on",
		"group_by_type",
		"group_by_based_on",
		"time_interval",
		"timespan",
		"timeseries",
		"type",
		"filters_json",
		"number_of_groups",
		"is_public",
		"module",
		# v0.19.5. A chart whose source is a REPORT rather than a doctype needs
		# both of these, and the double did not have them because nothing before
		# the KPI chart drew anything it could not get by counting rows.
		"report_name",
		"use_report_chart",
	],
	"Dashboard Chart Link": ["name", "chart"],
	"Number Card": [
		"name",
		"label",
		"document_type",
		"function",
		"aggregate_function_based_on",
		"filters_json",
		"is_public",
		"color",
		"type",
		"module",
	],
	"Number Card Link": ["name", "card"],
	# ── v0.16.0: the Farm Task Dispatch Kanban board and its landing page ────
	"Kanban Board": [
		"name",
		"kanban_board_name",
		"reference_doctype",
		"field_name",
		"private",
		"show_labels",
		"columns",
	],
	"Kanban Board Column": ["name", "column_name", "indicator", "status", "order"],
	"Workspace": [
		"name",
		"title",
		"label",
		"module",
		"icon",
		"public",
		"is_hidden",
		"content",
		"sequence_id",
		"shortcuts",
		"links",
		"number_cards",
		"charts",
	],
	"Workspace Shortcut": ["name", "label", "type", "link_to", "doc_view", "kanban_board", "color"],
	"Workspace Link": [
		"name",
		"type",
		"label",
		"link_type",
		"link_to",
		"link_count",
		"onboard",
		"hidden",
	],
	"Workspace Number Card": ["name", "number_card_name", "label"],
	"Workspace Chart": ["name", "chart_name", "label"],
	"Client Script": [
		"name",
		"dt",
		"view",
		"enabled",
		"script",
		"script_type",
		"module",
		"owner",
		"modified",
	],
	# ── v0.7.0: assets ──────────────────────────────────────────────────────
	"Asset": [
		"name",
		"asset_name",
		"item_code",
		"asset_category",
		"company",
		"purchase_date",
		"available_for_use_date",
		"gross_purchase_amount",
		"asset_quantity",
		"is_existing_asset",
		"calculate_depreciation",
		"cost_center",
		"location",
		"status",
		"docstatus",
	],
	"Asset Category": ["name", "asset_category_name", "accounts"],
	"Asset Category Account": [
		"company",
		"fixed_asset_account",
		"accumulated_depreciation_account",
		"depreciation_expense_account",
		"parent",
		"parenttype",
	],
	"Item": [
		"name",
		"item_code",
		"item_name",
		"item_group",
		"stock_uom",
		"is_fixed_asset",
		"is_stock_item",
		"asset_category",
		"disabled",
		# v0.66.0. `tools/masters.py` reads and writes these. NOTE what is NOT
		# here: a flat `default_warehouse` and flat `re_order_level`/`re_order_qty`.
		# ERPNext moved both into child tables in v12, and this fixture is a site
		# that made that move — which is what makes the child-table branch of
		# `_set_default_warehouse` and `_set_reorder` the one the suite exercises,
		# and the flat fallback a branch a v11 site would take instead.
		"description",
		"brand",
		"is_purchase_item",
		"is_sales_item",
		"has_batch_no",
		"has_serial_no",
		"valuation_rate",
		"standard_rate",
		"item_defaults",
		"reorder_levels",
		# v0.69.0. The per-item UOM conversion table. Present because
		# `stock_inventory._conversion` REFUSES a UOM it cannot convert, and a
		# fixture with no table at all could only ever exercise the refusal —
		# the branch that actually converts (3 Case → 36 Lb) needs a site that
		# has the row, and the one that refuses needs an item that has not.
		"uoms",
	],
	"Item Default": [
		"name",
		"parent",
		"parenttype",
		"parentfield",
		"idx",
		"company",
		"default_warehouse",
		"default_price_list",
		"buying_cost_center",
		"selling_cost_center",
		"expense_account",
		"income_account",
	],
	"Item Reorder": [
		"name",
		"parent",
		"parenttype",
		"parentfield",
		"idx",
		"warehouse",
		"warehouse_group",
		"warehouse_reorder_level",
		"warehouse_reorder_qty",
		"material_request_type",
	],
	"Item Group": ["name", "item_group_name", "parent_item_group", "is_group"],
	"UOM": ["name", "enabled"],
	"UOM Conversion Detail": [
		"name",
		"parent",
		"parenttype",
		"parentfield",
		"idx",
		"uom",
		"conversion_factor",
	],
	# ── v0.69.0: stock ──────────────────────────────────────────────────────
	#
	# The three doctypes that answer three different questions, and are easy to
	# confuse — see `tools/stock_inventory.py`'s module docstring. Stock Entry is
	# the instruction, Stock Ledger Entry is the history, Bin is the balance.
	# All three are registered permanently: nothing in this suite depends on any
	# of them being ABSENT, and the "that DocType is not installed" degradation
	# already has Purchase Invoice to prove it against.
	"Stock Entry": [
		"name",
		"company",
		"posting_date",
		"posting_time",
		"stock_entry_type",
		"purpose",
		"docstatus",
		"remarks",
		"total_amount",
		"total_outgoing_value",
		"total_incoming_value",
		"work_order",
		"purchase_order",
		"purchase_receipt_no",
		"delivery_note_no",
		"sales_invoice_no",
		"outgoing_stock_entry",
		"items",
		"owner",
	],
	"Stock Entry Detail": [
		"name",
		"idx",
		"item_code",
		"item_name",
		"qty",
		"uom",
		"stock_uom",
		"conversion_factor",
		"transfer_qty",
		"s_warehouse",
		"t_warehouse",
		"batch_no",
		"basic_rate",
		"basic_amount",
		"parent",
		"parenttype",
		"parentfield",
	],
	"Stock Entry Type": ["name", "purpose"],
	"Stock Ledger Entry": [
		"name",
		"item_code",
		"warehouse",
		"posting_date",
		"posting_time",
		"actual_qty",
		"qty_after_transaction",
		"valuation_rate",
		"stock_value",
		"stock_value_difference",
		"voucher_type",
		"voucher_no",
		"company",
		"is_cancelled",
	],
	# NOTE what Bin does NOT have: a `company` column. It is scoped only through
	# its warehouse, which is why every company-filtered balance read in
	# `stock_inventory.py` resolves the company's warehouses first — a filter
	# this fixture would silently accept if it invented the column.
	"Bin": [
		"name",
		"item_code",
		"warehouse",
		"actual_qty",
		"valuation_rate",
		"stock_value",
		"reserved_qty",
		"ordered_qty",
		"projected_qty",
		"stock_uom",
	],
	# v0.66.0. The masters `tools/masters.py` creates and reads. `Customer` and
	# `Supplier` carry no `company` column here, deliberately and faithfully:
	# stock ERPNext puts none on either, and the tools' promise is that a company
	# argument on a site-wide doctype is REPORTED as not applied rather than
	# quietly dropped. A fixture that invented the column would make that
	# reporting untestable.
	"Customer": [
		"name",
		"customer_name",
		"customer_group",
		"customer_type",
		"territory",
		"disabled",
		"tax_id",
		"tax_category",
		"default_currency",
		"default_price_list",
		"payment_terms",
		"credit_limit",
		"accounts",
	],
	"Supplier Group": ["name", "parent_supplier_group", "is_group"],
	"Customer Group": ["name", "parent_customer_group", "is_group"],
	"Territory": ["name", "parent_territory", "is_group"],
	"Warehouse": [
		"name",
		"warehouse_name",
		"company",
		"parent_warehouse",
		"is_group",
		"disabled",
		"warehouse_type",
		"account",
		"city",
		"phone_no",
		"address_line_1",
	],
	"Warehouse Type": ["name"],
	"Price List": [
		"name",
		"price_list_name",
		"currency",
		"enabled",
		"buying",
		"selling",
		"price_not_uom_dependent",
	],
	"Item Price": [
		"name",
		"item_code",
		"item_name",
		"price_list",
		"price_list_rate",
		"currency",
		"uom",
		"valid_from",
		"valid_upto",
		"customer",
		"supplier",
		"batch_no",
		"buying",
		"selling",
	],
	# The per-company control-account override both party doctypes carry.
	#
	# NO "Address" AND NO "Dynamic Link", deliberately. This double is a site
	# that never installed ERPNext's address module — a real configuration, and
	# the one `newhire`'s jurisdiction fallback exists for — so both stay absent
	# and the tests that need them register them per test, as test_employee does.
	# Putting them here would also have quietly changed how `Dynamic Link` is
	# queried, since it is a flat table that tests seed directly.
	"Party Account": ["name", "parent", "parenttype", "parentfield", "idx", "company", "account"],
	# No "Location", deliberately: ERPNext's Asset requires one on some versions
	# and not others, and this fixture is a site without it — which is what makes
	# `create_asset`'s "set it only where the field exists" branch a real case.
}

#: Doctypes whose docname is built from one of their own fields, as ERPNext
#: names them. Only the ones this app's behaviour depends on: `create_asset`
#: creates an Item and then links the Asset to whatever the Item ended up
#: called, and a double that named it `I-00001` would make the link untestable.
ERPNEXT_AUTONAME = {
	"Item": "field:item_code",
	"Bank": "field:bank_name",
	"Fiscal Year": "field:year",
	"Supplier": "field:supplier_name",
	# v0.66.0. Each of these IS its own name on a stock install, and every one of
	# them is a docname a caller stores: `create_item_group` refuses a name that
	# is taken, and it can only do that if the name is the key here too.
	"Item Group": "field:item_group_name",
	"Customer": "field:customer_name",
	"Price List": "field:price_list_name",
	# A Party Type IS its name, which is what makes `frappe.db.exists("Party
	# Type", "Family")` the check every caller writes.
	"Party Type": "field:party_type",
	# Frappe HR names an Employment Type after `employee_type_name`, and the same
	# sentence applies: the type IS its name, which is what makes
	# `frappe.db.exists("Employment Type", "Hourly")` both the idempotence check
	# `install._employment_types` writes and the Link check `create_employee`
	# refuses on. A double that serial-named it would have let the seeder insert
	# "Hourly" and then refuse an Employee who named it.
	"Employment Type": "field:employee_type_name",
	# v0.68.1. THE OTHER THREE ORG MASTERS, and each is the same sentence as
	# Employment Type above: the record IS its name, which is what makes
	# `frappe.db.exists("Designation", "Picker")` both the idempotence check
	# `create_designation` writes and the Link check `create_employee` refuses on.
	# A double that serial-named them would have let a seeder insert "Picker" and
	# then refuse an Employee who named it.
	"Designation": "field:designation_name",
	"Branch": "field:branch",
	# Frappe HR's pay band is PROMPT-named: the docname is whatever the caller
	# supplied and there is no column behind it. Modelled rather than left blank
	# so `create_employee_grade` exercises the same path here as on a bench.
	"Employee Grade": "prompt",
	# `Department` IS DELIBERATELY ABSENT, and that is the faithful choice rather
	# than the lazy one. Frappe HR names a Department through a controller that
	# appends the company abbreviation — "Harvest" at Orchard Meadow becomes
	# "Harvest - OML" — and neither `field:department_name` nor a serial name is
	# that. The serial name this fixture falls through to has the ONE property
	# that matters for the tools: the docname is not the string somebody typed,
	# so `tools/org._resolve` is forced to go through the name column here
	# exactly as it must on a bench. A `field:` entry would have hidden that.
	# ERPNext's Company is `field:company_name`, and `create_company` depends on
	# it: a Company that came back named "C-00001" would make every account
	# docname built from its abbreviation point at a company nobody can find.
	"Company": "field:company_name",
	# A Dashboard, a Chart and a Card are each named by their label, which is what
	# makes `frappe.db.exists("Number Card", "Critical Compliance Alerts")` the
	# idempotence check `dashboard.install_command_center` writes.
	"Dashboard": "field:dashboard_name",
	"Dashboard Chart": "field:chart_name",
	"Number Card": "field:label",
	# Same for a Kanban Board, which is what makes `install_dispatch_board`
	# idempotent: the second migrate finds "Farm Task Dispatch" and leaves it
	# exactly as somebody has since arranged it.
	"Kanban Board": "field:kanban_board_name",
	# v0.17.0. A Frappe User IS its email address and a Role IS its name — both
	# are what `frappe.db.exists("User", "worker@example.com")` and
	# `frappe.db.exists("Role", "Field Worker")` depend on, and both are the check
	# every idempotent installer in this app writes.
	"User": "field:email",
	"Role": "field:role_name",
}

#: Doctypes this app owns. Their meta is loaded from the shipped JSON so tests
#: assert against the real defaults rather than a copy that can drift.
APP_DOCTYPES = {
	"ERPNext MCP Settings": "erpnext_mcp_settings",
	"MCP Action Log": "mcp_action_log",
	"Cap Table Entry": "cap_table_entry",
	"Member Event": "member_event",
	"Governance Document": "governance_document",
	"Asset Cost Profile": "asset_cost_profile",
	"Asset Cost Center Allocation": "asset_cost_center_allocation",
	"Asset Depreciation Posting": "asset_depreciation_posting",
	"Note Payable": "note_payable",
	"Note Payable Event": "note_payable_event",
	"Parcel": "parcel",
	"Parcel Conveyance Event": "parcel_conveyance_event",
	"Lease": "lease",
	"Related Party": "related_party",
	"Field": "field",
	"Irrigation Zone": "irrigation_zone",
	"Housing Unit": "housing_unit",
	"Housing Assignment": "housing_assignment",
	"Family": "family",
	"Staged File Upload Session": "staged_file_upload_session",
	"Staged File Chunk": "staged_file_chunk",
	# ── v0.15.0: the compliance framework ───────────────────────────────────
	"Compliance Policy": "compliance_policy",
	"Certification": "certification",
	"Certification Renewal": "certification_renewal",
	"Regulatory Filing": "regulatory_filing",
	"Audit Event": "audit_event",
	"Audit Corrective Action": "audit_corrective_action",
	"Compliance Alert": "compliance_alert",
	# ── v0.16.0: Farm Task Dispatch and the records a completion produces ────
	"Farm Task": "farm_task",
	"Farm Task Assignment": "farm_task_assignment",
	"Farm Task Evidence": "farm_task_evidence",
	"Housing Inspection": "housing_inspection",
	"Detector Test": "detector_test",
	"Water Test": "water_test",
	# ── v0.17.0: mobile access ──────────────────────────────────────────────
	"Mobile Access Grant": "mobile_access_grant",
	# ── v0.19.0: the training register ──────────────────────────────────────
	"Employee Training Record": "employee_training_record",
	# ── v0.19.2: the regime vocabulary and the curriculum master ─────────────
	"Compliance Regime": "compliance_regime",
	"Compliance Regime Link": "compliance_regime_link",
	"Training Type": "training_type",
	# The group training event and its sign-in sheet. The session is the ACT and
	# the Employee Training Records it produces are the EVIDENCE — see
	# `erpnext_mcp/training_sessions.py` — so both are here and the register above
	# is unchanged.
	"Training Session": "training_session",
	"Training Session Attendee": "training_session_attendee",
	# ── v0.19.3: the shift, and the heat record anchored to it ──────────────
	"Farm Shift": "farm_shift",
	"Farm Shift Crew Member": "farm_shift_crew_member",
	"Farm Shift Compliance Event": "farm_shift_compliance_event",
	"Farm Shift Weather Reading": "farm_shift_weather_reading",
	"Heat Exposure Event": "heat_exposure_event",
	"Heat Acclimatization Worker": "heat_acclimatization_worker",
	# ── v0.19.4: what the conditions were, and the numbers they are read against ─
	"Weather Settings": "weather_settings",
	"Weather Company Override": "weather_company_override",
	# ── v0.19.5: the judgement behind a normalized cash flow figure ──────────
	"Normalization Adjustment": "normalization_adjustment",
	# ── v0.19.6: the precomputed history every windowed report reads from ────
	"Financial KPI History": "financial_kpi_history",
	# v0.39.0. The KPIs themselves, as records — what is computed, over what
	# window, and what values are worth an alert.
	"Financial KPI Definition": "financial_kpi_definition",
	# ── v0.21.0: the visit as a shape of data, and one worker's execution of it ─
	"Inspection Template": "inspection_template",
	"Inspection Template Section": "inspection_template_section",
	"Inspection Session": "inspection_session",
	"Inspection Session Evidence": "inspection_session_evidence",
	"Inspection Session Section Submission": "inspection_session_section_submission",
	# v0.22.0. The rule DEFINITIONS, which the sweep now reads instead of the
	# dict `alerts/rules.py` used to populate at import time.
	"Compliance Rule": "compliance_rule",
	# v0.24.0. Universal Asset Tags — one durable ID per reportable asset.
	"Asset Register": "asset_register",
	# v0.25.0. State-change events on assets — who did what, when, where.
	"Asset State Log": "asset_state_log",
	# v0.78.0. One block, restricted from entry until one moment, because of one
	# product. Registered here rather than seeded per-test because the REI reads
	# are on the SCAN path: `asset_status` asks for live restrictions on every
	# asset scan, and a double without the register would make every one of
	# those a silently-empty section rather than a real query.
	"Spray REI": "spray_rei",
	# v0.79.0. The narrative table and the two tables that make a Farm Task
	# something other than a thing one person does in one visit: the time it was
	# actually worked, and what it is related to.
	"Task Note": "task_note",
	"Task Time Segment": "task_time_segment",
	"Farm Task Link": "farm_task_link",
	# v0.79.0. The two registers this release exists for. Both are on the SCAN
	# and TASK paths — a discipline chain is read from a mobile wrapper and an
	# accident report spawns sub-tasks — so they are registered here rather than
	# seeded per-test, for the reason Spray REI is.
	"Farm Incident Record": "farm_incident_record",
	"Accident Report": "accident_report",
	"Accident Witness": "accident_witness",
	# v0.79.0. The wizard registry. Definitions are DATA, so the double has to
	# hold them the same way a bench does — a test that hard-coded a wizard would
	# be testing a shape this app does not use.
	"Wizard Definition": "wizard_definition",
	"Wizard Step": "wizard_step",
	"Wizard Field": "wizard_field",
	# v0.27.0. The structured I-9 workflow.
	"I-9 Form": "i_9_form",
	"I-9 Audit Log": "i_9_audit_log",
	"I-9 Settings": "i_9_settings",
	"I-9 Document Type": "i_9_document_type",
	# v0.55.0. The Supplement B child table. Registered because
	# `i9_supplement_b_unsigned` reads it as a doctype in its own right —
	# `frappe.db.get_all("I-9 Reverification", ...)` rather than through its
	# parent — which is what lets one query answer "which forms have an unsigned
	# reverification" for a whole register.
	"I-9 Reverification": "i_9_reverification",
	# v0.28.0. W-4 / Federal Withholding Engine.
	"W-4 Form": "w_4_form",
	"Federal Tax Table": "federal_tax_table",
	"FICA Configuration": "fica_configuration",
	# v0.29.0. State Tax Engines (Oregon + Washington).
	"State Tax Configuration": "state_tax_configuration",
	"State Tax Table": "state_tax_table",
	# v0.30.0. Salary Structures + Payroll.
	# v0.99.0. Where a break horn is delivered. Registered here rather than
	# seeded per-test because it is on the BREAK path: `log_shift_break` looks
	# up the crew's handsets on every crew break, and a double without the
	# register would make every one of those a silently-empty lookup rather
	# than a real query.
	"Mobile Push Token": "mobile_push_token",
	"Farm Salary Structure": "farm_salary_structure",
	"Farm Payroll Deduction": "farm_payroll_deduction",
	# The court order the deduction above exists under. Separate because a
	# payroll run should not have to parse a court order, and because a case
	# number, a balance and a date of service are what answers a court — none of
	# which belongs on a row read forty times a year by an arithmetic engine.
	"Farm Garnishment": "farm_garnishment",
	"Farm Payroll Entry": "farm_payroll_entry",
	"Farm Payroll Slip": "farm_payroll_slip",
	# v0.40.0. Payroll to the general ledger: which accounts a company's payroll
	# posts to, and the draft Journal Entries a run produced.
	"Farm Payroll Account Mapping": "farm_payroll_account_mapping",
	"Farm Payroll Account Map Row": "farm_payroll_account_map_row",
	"Farm Payroll GL Posting": "farm_payroll_gl_posting",
	# v0.31.0. Expense Receipt Capture.
	"Expense Receipt": "expense_receipt",
	"Expense Receipt Item": "expense_receipt_item",
	# v0.32.0. The crew's track. Standalone rather than a child table of the
	# shift, and `shift_location_log.py` argues why.
	"Shift Location Log": "shift_location_log",
	# v0.34.0. The filing register.
	"Tax Form": "tax_form",
	# v0.38.0. Where the regulations live, and what they said last time.
	"Regulation Feed": "regulation_feed",
	"Regulation Feed Rule Link": "regulation_feed_rule_link",
	# v0.41.0. The shape of ONE recurring job, as data — and the items a worker
	# ticks off inside it.
	"Farm Task Template": "farm_task_template",
	"Farm Task Template Checklist Item": "farm_task_template_checklist_item",
	# v0.42.0. Budget + Variance Alerts: what was planned per account and per
	# KPI, and what refresh_budget found when it checked.
	"Budget": "budget",
	"Budget Line Item": "budget_line_item",
	"Budget KPI Target": "budget_kpi_target",
	# v0.43.0. Which trained model is deployed for which company and which
	# piecework activity.
	"ML Model": "ml_model",
	# v0.44.0. BucketLog -> ERPNext piecework bridge: synced captures, the
	# sessions that group them, and the badge -> Employee register.
	"Bucket Log Entry": "bucket_log_entry",
	"Bucket Log Session": "bucket_log_session",
	"Bucket Log Badge Map": "bucket_log_badge_map",
	# v0.58.0. The break policy and its two child schedules.
	"Labor Break Policy": "labor_break_policy",
	"Labor Break Schedule Row": "labor_break_schedule_row",
	"Labor Heat Break Row": "labor_heat_break_row",
	# v0.60.0. One row per signature event: who was proved to be at the pad, on
	# what device, where, and against what hash of the record they were shown.
	"Signing Evidence": "signing_evidence",
	# v0.61.0. What the OPERATION pays, as opposed to what one person earns:
	# a rate per unit per (company, activity), read on every payroll run, and a
	# default hourly rate per (company, designation), read once at hire.
	"Piecework Rate": "piecework_rate",
	"Position Wage Default": "position_wage_default",
	# v0.67.0. The two receipt registers and the settlement's two child tables:
	# what a packer weighed in, and what the packer later said it was worth.
	"Scale Ticket": "scale_ticket",
	"Settlement Statement": "settlement_statement",
	"Settlement Line Item": "settlement_line_item",
	"Settlement Deduction": "settlement_deduction",
	# v0.68.0. Container-Agnostic Fill Pipeline: the current threshold per
	# (company, container_type), the append-only log of changes to it, and the
	# per-checker acknowledgment child table on each change.
	"Container Fill Threshold": "container_fill_threshold",
	"Fill Threshold Change Log": "fill_threshold_change_log",
	"Fill Threshold Acknowledgment": "fill_threshold_acknowledgment",
	# v0.69.0. Document Intelligence: one scanned document, everything that was
	# read off it, and what checking it produced — the OCR text, the on-device
	# extraction and the judgement stage as three columns rather than three
	# records, so a value that turns out to be wrong can be traced to the stage
	# that produced it.
	"Document Validation": "document_validation",
	# v0.71.0. What a line on a bank statement is, as a record rather than as
	# code — see `tools/banking_bridge.py` for why the dictionary a farm uses to
	# read its own statement cannot live in a release.
	"Bank Categorization Rule": "bank_categorization_rule",
	# v0.73.0. The Bank Bridge consolidation: one statement period and whether
	# the books tie out against it, the statement's own lines beside it, and the
	# terms an investment account is managed under. See `tools/anchors.py` for
	# why the reconciliation truth cannot live in the pipe that parses the PDFs.
	"Statement Anchor": "statement_anchor",
	"Statement Anchor Line": "statement_anchor_line",
	"Advisory Agreement": "advisory_agreement",
	# v0.75.0. The one spelling a till prints for one vendor, taught the moment a
	# bookkeeper links a receipt to a Supplier by hand — see `tools/receipts.py`
	# for why "SIATAPING" is a mapping no string algorithm can ever compute.
	"Merchant Alias": "merchant_alias",
	# v0.80.0. Trade documentation across three tiers: the shape of a kind of
	# paper, the rule saying which destination asks for it, one shipment, its
	# checklist, and the documents themselves. See `tools/shipments.py` for why
	# thirteen document types are one polymorphic doctype rather than thirteen.
	"Trade Document Template": "trade_document_template",
	"Destination Document Requirement": "destination_document_requirement",
	"Trade Shipment": "trade_shipment",
	"Trade Shipment Document": "trade_shipment_document",
	"Trade Document": "trade_document",
	# v0.80.0, IPO readiness Phase 1 — the financial controls. Spending authority
	# as a table with a chain on it, and one accounting period carrying both the
	# steps that have to be finished before it closes AND whether it is now locked
	# against posting. See `tools/controls.py` for why the checklist and the lock
	# are one row rather than two doctypes.
	"Approval Threshold": "approval_threshold",
	"Approval Threshold Level": "approval_threshold_level",
	"Closing Checklist": "closing_checklist",
	"Closing Checklist Item": "closing_checklist_item",
	# v0.80.0, IPO readiness Phase 2 — revenue. The ASC 606 unit of account: what
	# was promised, how the transaction price is allocated between the promises,
	# and when each is earned. See `tools/revenue.py` for why a Sales Order cannot
	# hold any of the four questions ASC 606 asks.
	"Revenue Contract": "revenue_contract",
	"Revenue Performance Obligation": "revenue_performance_obligation",
	"Revenue Recognition Schedule": "revenue_recognition_schedule",
	# v0.80.0, IPO readiness Phase 3 — cost. A growing crop carried at a value
	# with the remeasurements behind it, and what a thing is supposed to cost for
	# a date range. See `tools/costing.py` on why a consumable biological asset
	# cannot be an ERPNext Asset.
	"Biological Asset": "biological_asset",
	"Biological Asset Valuation": "biological_asset_valuation",
	"Standard Cost": "standard_cost",
	# v0.81.0, IPO readiness Phases 4 to 6 — the governance domain. The
	# arm's-length case for a related-party dealing; the three ITGC records
	# (who changed what and who approved it, and whether a backup has ever
	# been restored from); and the two reporting shapes — which SECTIONS a
	# report has, and which DISCLOSURES a filing must make, which are
	# deliberately different objects. See `tools/disclosure.py` for why.
	"Transfer Pricing Documentation": "transfer_pricing_documentation",
	"Change Management Log": "change_management_log",
	"Backup Record": "backup_record",
	"Reporting Template": "reporting_template",
	"Reporting Template Section": "reporting_template_section",
	"Disclosure Checklist": "disclosure_checklist",
	"Disclosure Checklist Item": "disclosure_checklist_item",
	# v0.84.0, the activity-based costing engine. An activity is the thing that
	# costs money, a pool is its money for one year, and an assignment is one
	# run of pushing the pools out to the blocks that consumed them — stored
	# whole, intermediates and all, because a per-acre cost is a quotient of two
	# numbers that both moved during the year. See `tools/abc.py`.
	"Cost Activity": "cost_activity",
	"Cost Activity Account": "cost_activity_account",
	"Activity Cost Pool": "activity_cost_pool",
	"Activity Cost Pool Source": "activity_cost_pool_source",
	"ABC Cost Assignment": "abc_cost_assignment",
	"ABC Cost Assignment Line": "abc_cost_assignment_line",
	# v0.85.0. `Farm Translation` is one string in one language, keyed by a
	# STABLE KEY rather than by the English — which is what separates it from
	# Frappe's own Translation doctype and why both exist on a site. `Shadow Log
	# Entry` is a frozen copy of an event addressed up the chain of command; its
	# `source_doctype` / `source_name` are Data and not Links ON PURPOSE, so a
	# deleted source cannot cascade into the backup of it. See
	# `tools/translations.py` and `tools/shadow_log.py`.
	"Farm Translation": "farm_translation",
	"Shadow Log Entry": "shadow_log_entry",
	# v0.87.0, the breakeven calculator. The analysis is a PERSPECTIVE on the
	# chart of accounts — nothing it holds is posted — and the two child tables
	# are what make it comparable across seasons: one line per account with WHO
	# classified it, and the stored what-if band. `USDA Price Quote` is the
	# market register the overlay reads; it links to no Company because a
	# shipping point price belongs to no operation.
	"Breakeven Analysis": "breakeven_analysis",
	"Breakeven Cost Line": "breakeven_cost_line",
	"Breakeven Scenario": "breakeven_scenario",
	"USDA Price Quote": "usda_price_quote",
	# v0.82.0, agricultural master data. The three registers every other feature
	# had been taking on trust from whoever was calling it: what is grown, where
	# it is sold, and in what units. `Crop` and `Market` are deliberately NOT
	# company-scoped — a species is a species and a market is a place in the
	# world — which is why neither carries the `company` column almost
	# everything else in this map does. See `tools/agronomy.py`.
	"Crop": "crop",
	"Crop Variety": "crop_variety",
	# v0.97.0. The consultants child table, which hangs off ERPNext's Company
	# through a Custom Field rather than off a doctype of ours — so it is
	# registered here for its meta, and read through `frappe.get_doc("Company")`
	# rather than by filtering the child, which is the shape that works on a
	# bench. It needs no CHILD_TABLE_SOURCES entry for that reason.
	"Pest Management Provider": "pest_management_provider",
	"Crop Water Requirement": "crop_water_requirement",
	# v0.114.0. The two per-variety overlays. Both hang off CROP rather than off
	# Crop Variety, because Frappe has no nested child tables and Crop Variety is
	# itself a child — so each names its variety as a text column, and `crop.py`
	# checks that name against the parent's catalogue on save. See
	# `tools/agronomy.resolve_variety_water` for the fallback the water one feeds.
	"Crop Variety Water Requirement": "crop_variety_water_requirement",
	"Crop Variety Protocol": "crop_variety_protocol",
	"Market": "market",
	"Market Grade Standard": "market_grade_standard",
	"Agricultural UOM Context": "agricultural_uom_context",
	"Agricultural UOM Context Entry": "agricultural_uom_context_entry",
	"Agricultural UOM Conversion": "agricultural_uom_conversion",
	# v0.88.0, Wave 3 — the spray program. The recipe (a tank mix is several
	# products each at its OWN rate per acre, optionally split across two nozzle
	# sets flipped mid-pass), the nozzle master those sets point at, and the
	# EVENT — what went out, where, when, in what wind. Registered here rather
	# than seeded per-test because an application OPENS Spray REI records, and
	# Spray REI is already on the scan path.
	"Spray Nozzle Config": "spray_nozzle_config",
	"Spray Tank Mix": "spray_tank_mix",
	"Spray Tank Mix Product": "spray_tank_mix_product",
	"Spray Application": "spray_application",
	"Spray Application Block": "spray_application_block",
	# v0.88.0, Wave 3 — crop protection. An observation raises a block's pressure
	# for the season and, where it crosses that pest's action threshold,
	# generates a recommendation whose options are ordered least-chemical-first.
	# See `tools/cropprotect.py` for the resolution order and for the beneficial
	# override, which is the part that makes it integrated pest management rather
	# than pest counting.
	"Crop Observation": "crop_observation",
	"Pest Action Threshold": "pest_action_threshold",
	"Pest Pressure": "pest_pressure",
	"IPM Recommendation": "ipm_recommendation",
	"IPM Recommendation Action": "ipm_recommendation_action",
	# v0.88.0, Wave 3 — the value block lifecycle. A junction between a block and
	# what grows on it for one year, so a perennial that costs money for four
	# years before it returns any can be read over its LIFE rather than through a
	# fiscal year. See `tools/blocklifecycle.py` on why no general ledger can
	# answer that question and why an establishing block's negative margin is
	# reported as investment rather than as a loss.
	"Planting Season": "planting_season",
	"Block Cost Entry": "block_cost_entry",
	"Block Revenue Entry": "block_revenue_entry",
	# v0.91.0 — direct deposit. Employee Bank Account is the reason
	# `account_number` had to be a real Password field in this double rather
	# than a Data one: the tools' whole promise is that the number goes in and
	# never comes back out, and a double that kept it as an ordinary column
	# would let a read tool return it and the test still pass.
	"Employee Bank Account": "employee_bank_account",
	"ACH Originator Configuration": "ach_originator_configuration",
	# v0.98.0 — bin sealing. One closed bin and the crew whose buckets are in it.
	# STANDALONE rather than a child table of Bucket Log Session, for the reason
	# `bin_seal.py` gives: a bin is sealed by a CHECKER whose phone is not editing
	# anybody's session, several sessions tip into one bin, and one session fills
	# many bins over a day. The contributors table needs no CHILD_TABLE_SOURCES
	# entry — `get_bin_seal` and `trace_bin` read it through the parent document,
	# which is the shape that works on a bench.
	"Bin Seal": "bin_seal",
	"Bin Seal Contributor": "bin_seal_contributor",
	# v0.105.0 — the in-app feedback bubble's register. `entry_uuid` is unique
	# because the handset drains a backlog in one pass and drains it again from
	# the start if that pass is interrupted; see `tools/app_feedback.py`.
	"App Feedback": "app_feedback",
	# v0.111.0 — FSMA 204. The traceability lot code is the one identifier the Food
	# Traceability Rule requires, and it is the DOCNAME (`autoname: field:lot_code`)
	# because it is read off a bin and typed into a buyer's portal by somebody who
	# has never seen this site. `Traceability Lot Source` DOES need a
	# CHILD_TABLE_SOURCES entry, unlike Bin Seal Contributor: `trace_lot_forward`
	# has to find the PARENTS of a source row, which is the one direction that
	# cannot be read through a parent document.
	"Traceability Lot Code": "traceability_lot_code",
	"Traceability Lot Source": "traceability_lot_source",
	"Critical Tracking Event": "critical_tracking_event",
}

#: The standard reports this app ships, by folder name under `REPORT_DIR`. Rows
#: are built from each one's own JSON at `Store.reset` — see `_seed_app_reports`
#: for why a migrated site already has them and the double had to catch up.
APP_REPORTS = ("sustainable_cf_per_acre_by_quarter", "sustainable_cf_per_acre_ttm_monthly")


def _load_app_doctype(folder: str) -> dict:
	payload = _APP_DOCTYPE_CACHE.get(folder)
	if payload is None:
		with open(os.path.join(DOCTYPE_DIR, folder, f"{folder}.json")) as handle:
			payload = json.load(handle)
		_APP_DOCTYPE_CACHE[folder] = payload
	return payload


#: The shipped DocType JSON, read once. `reset_meta` rebuilds the meta objects
#: between tests — a test that adds a Custom Field or a whole DocType must not
#: leak it into the next one — and rereading two files per test would be a file
#: system call in every setUp for no benefit.
_APP_DOCTYPE_CACHE: dict = {}


class Field(FrappeDict):
	pass


class Meta:
	"""Just enough of frappe.model.meta.Meta for `compat` to interrogate.

	`autoname` is here because the app reads it: `dimensions._naming_field` asks a
	master DocType how it names itself before deciding whether to set a field or
	pass a name. A double that always answered "" would make every dimension value
	take the fallback path, which is the one that does *not* produce the readable
	docname the tool exists to produce.

	`max_attachments` is a class attribute rather than a constructor argument
	because Frappe's own default is 0-meaning-unlimited on every DocType, and
	`attach_file_to_document` reads it on every call. A test that wants the limit
	to bite sets it on the one meta it cares about; `reset_meta` rebuilds the
	objects between tests, so it cannot leak.
	"""

	#: 0 is Frappe's "no limit", and is what every DocType has unless somebody set one.
	max_attachments = 0

	def __init__(self, doctype: str, fields: list[Field], issingle: bool = False, autoname: str = ""):
		self.doctype = doctype
		self.fields = fields
		self.issingle = issingle
		self.autoname = autoname
		self._by_name = {f.fieldname: f for f in fields}

	def has_field(self, fieldname: str) -> bool:
		return fieldname in self._by_name

	def get_field(self, fieldname: str):
		return self._by_name.get(fieldname)

	def add(self, field: Field) -> None:
		"""Register a field added at runtime, as inserting a Custom Field does."""
		if field.fieldname in self._by_name:
			return
		self.fields.append(field)
		self._by_name[field.fieldname] = field


#: Select options this double reproduces verbatim, because the app reads them
#: off `frappe.get_meta` and branches on what it finds. `Account.account_type`
#: is the whole reason: `charts.site_account_types()` asks the site which types
#: it supports and substitutes a fallback for one it does not, and a double that
#: answered "no options at all" would make that path — the one that decides what
#: a hundred-account import writes — silently untested.
#:
#: This is ERPNext v15's list. Note what is NOT in it: "Credit Card", which the
#: shipped `us_llc_farm` template asks for on 2160.
#: ERPNext fields that are Links or Dynamic Links rather than plain Data, as
#: `(doctype, fieldname) -> (fieldtype, options)`.
#:
#: THIS TABLE IS WHY v0.12.1 EXISTS. Everything in `ERPNEXT_SCHEMA` used to be
#: modelled as Data, so `Party Type.party_type` — which is really a **Link to
#: DocType** — accepted any string the app handed it. v0.12.0 registered a party
#: type called "Family" against a site with no Family DocType, this suite said
#: fine, and `bench migrate` on a real bench raised `LinkValidationError` and
#: aborted.
#:
#: The party trio is modelled in full because it is one mechanism: `party_type`
#: names a DocType, and `party` is a **Dynamic Link** resolved through it. That
#: is what makes a Party Type's name load-bearing rather than a label, and it is
#: the fact the release was missing.
ERPNEXT_FIELD_LINKS = {
	# ── v0.18.1 ─────────────────────────────────────────────────────────────
	# THE EMPLOYEE'S SIX LINKS, AND `user_id` IS THE ONE THAT MATTERS. Modelled
	# as Data here until v0.18.1, which meant `onboard_employee` could insert an
	# Employee whose `user_id` named a User THAT DID NOT EXIST YET — it created
	# the record before it created the login — and this suite called it a pass.
	# On a real bench Frappe validates that link and the insert raises. Modelling
	# it faithfully is what turned that into a failing test, and the fix is the
	# ordering change in `newhire.py`: employee, then login, then link.
	("Employee", "company"): ("Link", "Company"),
	("Employee", "branch"): ("Link", "Branch"),
	("Employee", "department"): ("Link", "Department"),
	("Employee", "designation"): ("Link", "Designation"),
	("Employee", "employment_type"): ("Link", "Employment Type"),
	("Employee", "grade"): ("Link", "Employee Grade"),
	("Employee", "gender"): ("Link", "Gender"),
	("Department", "parent_department"): ("Link", "Department"),
	("Employee", "user_id"): ("Link", "User"),
	# ── v0.66.0: the master-data links and the two party Selects ────────────
	# Every one of these is a link `tools/masters.py` writes. Modelling them is
	# what makes its refusals real rather than decorative: a tool that checks a
	# warehouse exists before appending an Item Default row is indistinguishable
	# from one that does not, on a double where the bad row inserts anyway.
	("Item", "item_group"): ("Link", "Item Group"),
	("Item", "stock_uom"): ("Link", "UOM"),
	("Item Group", "parent_item_group"): ("Link", "Item Group"),
	("Item Default", "company"): ("Link", "Company"),
	("Item Default", "default_warehouse"): ("Link", "Warehouse"),
	("Item Reorder", "warehouse"): ("Link", "Warehouse"),
	("Warehouse", "company"): ("Link", "Company"),
	("Warehouse", "parent_warehouse"): ("Link", "Warehouse"),
	("Warehouse", "warehouse_type"): ("Link", "Warehouse Type"),
	("Supplier", "supplier_group"): ("Link", "Supplier Group"),
	("Customer", "customer_group"): ("Link", "Customer Group"),
	("Customer", "territory"): ("Link", "Territory"),
	("Item Price", "item_code"): ("Link", "Item"),
	("Item Price", "price_list"): ("Link", "Price List"),
	("Item Price", "customer"): ("Link", "Customer"),
	("Item Price", "supplier"): ("Link", "Supplier"),
	# `masters._party_type` reads these options off the site's own meta rather
	# than comparing against a hardcoded pair, so a fixture with no options at
	# all would let any string through and prove nothing.
	("Supplier", "supplier_type"): ("Select", "\nCompany\nIndividual"),
	("Customer", "customer_type"): ("Select", "\nCompany\nIndividual"),
	("Party Type", "party_type"): ("Link", "DocType"),
	("GL Entry", "party_type"): ("Link", "DocType"),
	("GL Entry", "party"): ("Dynamic Link", "party_type"),
	("Journal Entry Account", "party_type"): ("Link", "DocType"),
	("Journal Entry Account", "party"): ("Dynamic Link", "party_type"),
	# ── v0.68.0: the purchasing pipeline's Links ─────────────────────────────
	# Modelled for the same reason the master-data links above are: a tool that
	# checks a supplier or an item exists before appending a line is
	# indistinguishable from one that does not, on a double where the bad row
	# inserts anyway. `STORE.seed()` bypasses this (it writes rows directly,
	# never through `insert()`), so the existing `_trade()` fixture's Purchase
	# Order rows are unaffected by adding it here.
	("Purchase Order", "supplier"): ("Link", "Supplier"),
	("Purchase Order", "company"): ("Link", "Company"),
	("Purchase Order Item", "item_code"): ("Link", "Item"),
	("Purchase Order Item", "warehouse"): ("Link", "Warehouse"),
	("Purchase Order Item", "cost_center"): ("Link", "Cost Center"),
	("Purchase Receipt", "supplier"): ("Link", "Supplier"),
	("Purchase Receipt", "company"): ("Link", "Company"),
	("Purchase Receipt", "purchase_order"): ("Link", "Purchase Order"),
	("Purchase Receipt Item", "item_code"): ("Link", "Item"),
	("Purchase Receipt Item", "warehouse"): ("Link", "Warehouse"),
	("Purchase Receipt Item", "purchase_order"): ("Link", "Purchase Order"),
	("Purchase Receipt Item", "cost_center"): ("Link", "Cost Center"),
	# Purchase Invoice's own Links are declared here even though the doctype
	# stays absent by default — a test that registers it with
	# `harness.purchase_invoice_fields()` gets these for free rather than
	# re-declaring fieldtypes a fixture already knows.
	("Purchase Invoice", "supplier"): ("Link", "Supplier"),
	("Purchase Invoice", "company"): ("Link", "Company"),
	("Purchase Invoice", "credit_to"): ("Link", "Account"),
	("Purchase Invoice", "purchase_order"): ("Link", "Purchase Order"),
	("Purchase Invoice", "purchase_receipt"): ("Link", "Purchase Receipt"),
	("Purchase Invoice Item", "item_code"): ("Link", "Item"),
	("Purchase Invoice Item", "expense_account"): ("Link", "Account"),
	("Purchase Invoice Item", "warehouse"): ("Link", "Warehouse"),
	("Purchase Invoice Item", "cost_center"): ("Link", "Cost Center"),
	# ── v0.69.0: stock ───────────────────────────────────────────────────────
	# Same reasoning again, and one link that earns its place twice over:
	# `Stock Entry Detail.s_warehouse` and `.t_warehouse` are what make "the
	# tool put the warehouse in the wrong column" a failure the double can see.
	# v0.70.0. The sales side. `debit_to` is the one that earns its place: a
	# tool that put a payable there instead of a receivable would be caught by
	# nothing else in this fixture.
	("Sales Invoice", "customer"): ("Link", "Customer"),
	("Sales Invoice", "company"): ("Link", "Company"),
	("Sales Invoice", "debit_to"): ("Link", "Account"),
	("Sales Invoice Item", "item_code"): ("Link", "Item"),
	("Sales Invoice Item", "income_account"): ("Link", "Account"),
	("Sales Invoice Item", "cost_center"): ("Link", "Cost Center"),
	("Sales Taxes and Charges", "account_head"): ("Link", "Account"),
	("Stock Entry", "company"): ("Link", "Company"),
	("Stock Entry", "stock_entry_type"): ("Link", "Stock Entry Type"),
	("Stock Entry", "purchase_order"): ("Link", "Purchase Order"),
	("Stock Entry", "purchase_receipt_no"): ("Link", "Purchase Receipt"),
	("Stock Entry", "outgoing_stock_entry"): ("Link", "Stock Entry"),
	("Stock Entry Detail", "item_code"): ("Link", "Item"),
	("Stock Entry Detail", "s_warehouse"): ("Link", "Warehouse"),
	("Stock Entry Detail", "t_warehouse"): ("Link", "Warehouse"),
	("Stock Entry Detail", "uom"): ("Link", "UOM"),
	("Stock Entry Detail", "stock_uom"): ("Link", "UOM"),
	("UOM Conversion Detail", "uom"): ("Link", "UOM"),
	("Bin", "item_code"): ("Link", "Item"),
	("Bin", "warehouse"): ("Link", "Warehouse"),
	("Stock Ledger Entry", "item_code"): ("Link", "Item"),
	("Stock Ledger Entry", "warehouse"): ("Link", "Warehouse"),
}


def purchase_invoice_fields() -> list:
	"""Field objects for a test that needs Purchase Invoice to exist.

	The fixture keeps the doctype out of `ERPNEXT_SCHEMA` on purpose — see the
	"No Purchase Invoice" note there — so a test that needs it (this app's own
	purchasing tools, and `create_accounting_dimension`'s default-document-types
	path) calls `register_doctype("Purchase Invoice", purchase_invoice_fields())`
	rather than re-typing the field list, which is how the two callers drifted
	before this existed: `test_dimensions.py` registered one Data field, enough
	for its own purposes and not enough for a Purchase Invoice `insert()` to
	validate against.
	"""
	names = [
		"name",
		"supplier",
		"supplier_name",
		"company",
		"posting_date",
		"due_date",
		"bill_no",
		"bill_date",
		"credit_to",
		"currency",
		"status",
		"docstatus",
		"grand_total",
		"rounded_total",
		"outstanding_amount",
		"purchase_order",
		"purchase_receipt",
		"items",
		"owner",
	]
	return [_erpnext_field("Purchase Invoice", name) for name in names]


def purchase_invoice_item_fields() -> list:
	names = [
		"name",
		"idx",
		"item_code",
		"item_name",
		"qty",
		"rate",
		"amount",
		"expense_account",
		"cost_center",
		"warehouse",
		"purchase_order",
		"purchase_receipt",
		"parent",
		"parenttype",
		"parentfield",
	]
	return [_erpnext_field("Purchase Invoice Item", name) for name in names]


#: Fieldtypes that are neither Link, Dynamic Link, Select nor plain Data.
#: v0.17.0 needed this because a Password field is not cosmetic in the double —
#: `Store._extract_passwords` keys off the FIELDTYPE, so a `User.api_secret`
#: modelled as Data would be readable straight off the row and the revocation
#: test would prove nothing.
ERPNEXT_FIELD_TYPES = {
	("User", "api_secret"): ("Password", None),
	("User", "roles"): ("Table", "Has Role"),
	("DocType", "permissions"): ("Table", "DocPerm"),
	("User Permission", "allow"): ("Link", "DocType"),
	("User Permission", "for_value"): ("Dynamic Link", "allow"),
	("Has Role", "role"): ("Link", "Role"),
	("Custom DocPerm", "role"): ("Link", "Role"),
	("DocPerm", "role"): ("Link", "Role"),
	("User Permission", "user"): ("Link", "User"),
}

ERPNEXT_FIELD_OPTIONS = {
	("Account", "account_type"): "\n".join(
		[
			"",
			"Accumulated Depreciation",
			"Asset Received But Not Billed",
			"Bank",
			"Cash",
			"Chargeable",
			"Capital Work in Progress",
			"Cost of Goods Sold",
			"Current Asset",
			"Current Liability",
			"Depreciation",
			"Direct Expense",
			"Direct Income",
			"Equity",
			"Expense Account",
			"Expenses Included In Asset Valuation",
			"Expenses Included In Valuation",
			"Fixed Asset",
			"Income Account",
			"Indirect Expense",
			"Indirect Income",
			"Liability",
			"Payable",
			"Payment",
			"Payroll Payable",
			"Provision",
			"Receivable",
			"Round Off",
			"Round Off for Opening",
			"Service Received But Not Billed",
			"Stock",
			"Stock Adjustment",
			"Stock Received But Not Billed",
			"Tax",
			"Temporary",
		]
	),
	("Account", "root_type"): "Asset\nLiability\nIncome\nExpense\nEquity",
	# Frappe HR's four employee statuses. `create_employee` and `update_employee`
	# match against the site's own options case-insensitively, so a double with no
	# options would leave that path — and the refusal that lists the choices —
	# untested.
	("Employee", "status"): "Active\nInactive\nSuspended\nLeft",
	# ── v0.16.1 ─────────────────────────────────────────────────────────────
	# THE OPTIONS THAT COST A RELEASE. v0.16.0 wrote `indicator="gray"` at this
	# field and the Kanban Board insert threw on a site where the options are
	# capitalised — silently, because `install.py` discarded the report. The
	# double could not catch it, because it did not police Select options at all.
	#
	# The casing here is DELIBERATELY NOT the casing v0.16.0 assumed, and this
	# fixture does not claim to know what any given Frappe version ships. That is
	# the whole point: `dashboard._select_value` now reads the options off the
	# site and matches case-insensitively, and `TheIndicatorPaletteIsNotAssumed`
	# re-declares this field three different ways to prove the board still
	# installs against all of them.
	("Kanban Board Column", "indicator"): "Blue\nOrange\nRed\nGreen\nGray\nPurple\nYellow\nPink",
	("Kanban Board Column", "status"): "Active\nArchived",
	("Workspace Shortcut", "type"): "DocType\nReport\nPage\nDashboard\nURL",
	("Workspace Shortcut", "doc_view"): (
		"\nList\nReport Builder\nDashboard\nTree\nNew\nCalendar\nKanban\nImage\nInbox\nGantt"
	),
	("Workspace Link", "type"): "Card Break\nLink",
	("Workspace Link", "link_type"): "DocType\nPage\nReport\nDashboard",
	# v15's Journal Entry voucher types. `set_opening_balance` sets "Opening
	# Entry" only when the site's own meta offers it, so a double with no options
	# would leave that branch — the one that keeps opening balances out of the
	# period's activity in every report that separates them — untested.
	("Journal Entry", "voucher_type"): "\n".join(
		[
			"Journal Entry",
			"Inter Company Journal Entry",
			"Bank Entry",
			"Cash Entry",
			"Credit Card Entry",
			"Debit Note",
			"Credit Note",
			"Contra Entry",
			"Excise Entry",
			"Write Off Entry",
			"Opening Entry",
			"Depreciation Entry",
			"Exchange Rate Revaluation",
			"Exchange Gain Or Loss",
		]
	),
}


def _erpnext_field(doctype: str, name: str):
	"""One ERPNext field, as a Link, a Dynamic Link, a Table, a Password, a Select or Data."""
	link = ERPNEXT_FIELD_LINKS.get((doctype, name)) or ERPNEXT_FIELD_TYPES.get((doctype, name))
	if link:
		return Field(fieldname=name, fieldtype=link[0], options=link[1], label=name)
	return Field(
		fieldname=name,
		fieldtype="Select" if (doctype, name) in ERPNEXT_FIELD_OPTIONS else "Data",
		options=ERPNEXT_FIELD_OPTIONS.get((doctype, name)),
		label=name,
	)


def _build_meta() -> dict:
	metas = {}
	for doctype, fields in ERPNEXT_SCHEMA.items():
		metas[doctype] = Meta(
			doctype,
			[_erpnext_field(doctype, name) for name in fields],
			autoname=ERPNEXT_AUTONAME.get(doctype, ""),
		)
	for doctype, folder in APP_DOCTYPES.items():
		payload = _load_app_doctype(folder)
		metas[doctype] = Meta(
			doctype,
			[Field(**field) for field in payload["fields"]],
			issingle=bool(payload.get("issingle")),
			autoname=str(payload.get("autoname") or ""),
		)
	return metas


META = _build_meta()


def reset_meta() -> None:
	"""Put the schema back to what this fixture ships, discarding runtime additions.

	The same object is kept rather than rebound, because tests import `META` by
	name. Called from `MCPTestCase.setUp`: a test that creates an accounting
	dimension really does add a DocType and a Custom Field to the site, and the
	next test has to start from a site where neither exists.
	"""
	META.clear()
	META.update(_build_meta())


def register_doctype(doctype: str, fields, issingle: bool = False, autoname: str = "") -> None:
	"""Make a DocType exist on the fake site, as inserting a DocType does."""
	META[doctype] = Meta(
		doctype,
		[Field(**field) if isinstance(field, dict) else field for field in fields or ()],
		issingle=issingle,
		autoname=autoname,
	)
	INSTALLED_DOCTYPES.add(doctype)


def add_field(
	doctype: str, fieldname: str, fieldtype: str = "Data", options=None, label=None, reqd=0
) -> None:
	"""Make a field exist on a DocType, as inserting a Custom Field does.

	`reqd` is carried because `compliance_fields.py` installs three of its Employee
	columns MANDATORY, and a double that dropped the flag made
	`employee._mandatory_gaps` unreachable for exactly the fields this app is
	itself the reason for — the wall the iOS wizard hit in v0.46.0 and the suite
	could not have seen.
	"""
	meta = META.get(doctype)
	if meta is None:
		raise ValidationError(f"stub has no meta for {doctype!r}, so it cannot take a custom field")
	meta.add(
		Field(fieldname=fieldname, fieldtype=fieldtype, options=options, label=label, reqd=int(reqd or 0))
	)


# ── filters ─────────────────────────────────────────────────────────────────
def _match(row: dict, filters) -> bool:
	if not filters:
		return True
	if isinstance(filters, str):
		return row.get("name") == filters
	if isinstance(filters, list):
		# [[fieldname, operator, value], ...]
		return all(_match_one(row, f[0], (f[1], f[2])) for f in filters)
	return all(_match_one(row, field, value) for field, value in filters.items())


def _match_one(row: dict, field: str, condition) -> bool:
	actual = row.get(field)
	if not isinstance(condition, (tuple, list)):
		return _eq(actual, condition)
	operator, expected = condition[0], condition[1]
	operator = str(operator).lower()
	if operator == "=":
		return _eq(actual, expected)
	if operator in ("!=", "not="):
		return not _eq(actual, expected)
	if operator == "in":
		return any(_eq(actual, item) for item in expected)
	if operator == "not in":
		return not any(_eq(actual, item) for item in expected)
	if operator == "like":
		return _like(actual, expected)
	if operator == "not like":
		return not _like(actual, expected)
	if operator == "between":
		low, high = expected
		return actual is not None and _key(low) <= _key(actual) <= _key(high)
	if operator == "is":
		if expected == "set":
			return actual not in (None, "")
		return actual in (None, "")
	if operator in ("<", "<=", ">", ">="):
		if actual is None:
			return False
		left, right = _key(actual), _key(expected)
		return {
			"<": left < right,
			"<=": left <= right,
			">": left > right,
			">=": left >= right,
		}[operator]
	raise NotImplementedError(f"stub filter operator {operator!r}")


def _eq(actual, expected) -> bool:
	if isinstance(expected, (int, float)) and not isinstance(expected, bool):
		return float(actual or 0) == float(expected)
	return str(actual if actual is not None else "") == str(expected if expected is not None else "")


def _like(actual, pattern: str) -> bool:
	# MariaDB LIKE with the site's default collation: case-insensitive.
	text = str(actual or "").lower()
	needle = str(pattern or "").lower()
	if needle.startswith("%") and needle.endswith("%"):
		return needle.strip("%") in text
	if needle.startswith("%"):
		return text.endswith(needle.lstrip("%"))
	if needle.endswith("%"):
		return text.startswith(needle.rstrip("%"))
	return text == needle


def _key(value):
	"""Comparison key that keeps numbers numeric and everything else a string."""
	if isinstance(value, (int, float)) and not isinstance(value, bool):
		return value
	if isinstance(value, (datetime.date, datetime.datetime)):
		return value.isoformat()
	try:
		return float(value)
	except (TypeError, ValueError):
		return str(value)


# ── documents ───────────────────────────────────────────────────────────────
CHILD_TABLES = {
	# v0.79.0. The narrative table has THREE PARENTS and that is the point of it:
	# appending an account of what happened is one act, and three near-identical
	# tables would drift the first time one of them grew a column.
	("Farm Task", "task_notes"): "Task Note",
	("Accident Report", "investigation_notes"): "Task Note",
	("Farm Incident Record", "discipline_notes"): "Task Note",
	("Farm Task Assignment", "time_segments"): "Task Time Segment",
	("Farm Task", "linked_tasks"): "Farm Task Link",
	("Accident Report", "witnesses"): "Accident Witness",
	("Wizard Definition", "steps"): "Wizard Step",
	("Wizard Step", "fields"): "Wizard Field",
	("Journal Entry", "accounts"): "Journal Entry Account",
	("Bank Transaction", "payment_entries"): "Bank Transaction Payments",
	("Fiscal Year", "companies"): "Fiscal Year Company",
	("Workflow", "states"): "Workflow Document State",
	("Workflow", "transitions"): "Workflow Transition",
	("Asset Category", "accounts"): "Asset Category Account",
	("Item", "item_defaults"): "Item Default",
	("Item", "reorder_levels"): "Item Reorder",
	("Supplier", "accounts"): "Party Account",
	("Customer", "accounts"): "Party Account",
	("Asset Cost Profile", "cost_center_allocation"): "Asset Cost Center Allocation",
	("Asset Cost Profile", "depreciation_postings"): "Asset Depreciation Posting",
	("Statement Anchor", "statement_lines"): "Statement Anchor Line",
	("Note Payable", "payment_events"): "Note Payable Event",
	("Parcel", "conveyance_events"): "Parcel Conveyance Event",
	("Payment Entry", "references"): "Payment Entry Reference",
	# v0.68.0. The purchasing pipeline's three item tables. Purchase Invoice
	# Item is mapped here even though the Purchase Invoice DOCTYPE stays absent
	# by default (see the "No Purchase Invoice" note in ERPNEXT_SCHEMA) — this
	# mapping only matters once a test has registered the parent, and having it
	# ready is what lets `create_purchase_invoice`'s `doc.append("items", …)`
	# behave the same whether or not that test bothered to add the child too.
	("Purchase Order", "items"): "Purchase Order Item",
	("Purchase Receipt", "items"): "Purchase Receipt Item",
	("Purchase Invoice", "items"): "Purchase Invoice Item",
	# v0.69.0. Stock Entry's one line table, and the Item's UOM conversions —
	# the table `stock_inventory._conversion` reads before it will accept a qty
	# in anything other than the item's own stock UOM.
	# v0.70.0. A Sales Invoice's lines and its charge rows.
	("Sales Invoice", "items"): "Sales Invoice Item",
	("Sales Invoice", "taxes"): "Sales Taxes and Charges",
	("Stock Entry", "items"): "Stock Entry Detail",
	("Item", "uoms"): "UOM Conversion Detail",
	("Certification", "renewals"): "Certification Renewal",
	("Audit Event", "corrective_actions_required"): "Audit Corrective Action",
	("Dashboard", "charts"): "Dashboard Chart Link",
	("Dashboard", "cards"): "Number Card Link",
	("Kanban Board", "columns"): "Kanban Board Column",
	("Workspace", "shortcuts"): "Workspace Shortcut",
	("User", "roles"): "Has Role",
	("DocType", "permissions"): "DocPerm",
	("Workspace", "links"): "Workspace Link",
	("Workspace", "number_cards"): "Workspace Number Card",
	("Workspace", "charts"): "Workspace Chart",
	("Fill Threshold Change Log", "acknowledgments"): "Fill Threshold Acknowledgment",
	("Farm Task Assignment", "evidence_files"): "Farm Task Evidence",
	("Housing Inspection", "photos"): "Farm Task Evidence",
	("Detector Test", "photos"): "Farm Task Evidence",
	("Water Test", "sample_photos"): "Farm Task Evidence",
	# v0.19.2. Two Table MultiSelects over one child doctype, which is the same
	# one-shape-many-parents case as Farm Task Evidence above. Modelling both is
	# what makes `training.rows_for_parents` — which filters on `parenttype` —
	# actually testable: a double that knew only the alert side would report every
	# Training Type as untagged and call the packet filter a pass.
	("Compliance Alert", "regime"): "Compliance Regime Link",
	("Training Type", "regimes"): "Compliance Regime Link",
	("Training Session", "regimes"): "Compliance Regime Link",
	("Training Session", "attendees"): "Training Session Attendee",
	# v0.19.3. The crew envelope and the two timelines hanging off a shift, plus
	# the acclimatization plan on a heat record. Modelling all four is what makes
	# `shifts.crew_of` and the Attendance bridge testable at all: both read the
	# child rows back through `frappe.db.get_all` with a `parent` filter rather
	# than off the document in hand, because the bridge runs from a docname.
	("Farm Shift", "crew"): "Farm Shift Crew Member",
	("Farm Shift", "compliance_events"): "Farm Shift Compliance Event",
	("Farm Shift", "weather_timeline"): "Farm Shift Weather Reading",
	("Heat Exposure Event", "acclimatization_plan"): "Heat Acclimatization Worker",
	# v0.19.4. `thresholds_for` reads the override rows back through
	# `frappe.db.get_all` with a `parent` filter rather than off the Single in
	# hand, because it is called from the sweep with only a company name — so the
	# double has to store them as child rows or the per-company threshold is
	# untestable and would look like it worked.
	("Weather Settings", "per_company_overrides"): "Weather Company Override",
	# v0.31.0. The line items OCR read off a receipt. Modelled so an appended row
	# carries its own doctype the way Frappe gives it one — which is what makes
	# `get_expense_receipt` returning the same four keys on a freshly built
	# document and on one re-read from the store a test rather than a hope.
	("Expense Receipt", "items"): "Expense Receipt Item",
	# v0.67.0. Both are read back off the document in hand rather than through a
	# `parent` filter, but they are modelled here anyway: `_name_children` is what
	# gives a child row an `idx`, and a settlement whose lines had none would sum
	# correctly and print in the wrong order.
	("Settlement Statement", "line_items"): "Settlement Line Item",
	("Settlement Statement", "deductions"): "Settlement Deduction",
	# v0.34.0. The tax form generators read a pay period's slips back off the
	# entry with `frappe.get_doc`, so the double has to store them as child rows
	# of the doctype they belong to or a W-2 built from a seeded payroll entry
	# would be testing a list of dicts the framework would never have produced.
	("Farm Payroll Entry", "slips"): "Farm Payroll Slip",
	# v0.40.0. `post_payroll_to_gl` appends one row per draft Journal Entry it
	# created and `_live_postings` reads them back to decide whether this run has
	# already been posted — which is the idempotency check, so the rows have to
	# survive a re-read as the rows they were.
	("Farm Payroll Entry", "gl_postings"): "Farm Payroll GL Posting",
	("Farm Payroll Account Mapping", "components"): "Farm Payroll Account Map Row",
	# v0.41.0. `task_templates.checklist_of` and `regimes_of` read both child
	# doctypes directly with a `parent` filter, because a template is read from a
	# docname rather than from a document somebody already loaded — the snapshot
	# `create_task_from_template` copies onto a task goes through it, and so does
	# the recipe the compliance sweep builds. Without these, every template would
	# look checklist-less and the enforcement would silently never fire.
	("Farm Task Template", "checklist"): "Farm Task Template Checklist Item",
	("Farm Task Template", "compliance_regimes"): "Compliance Regime Link",
	# v0.48.0. The first child table on a SINGLE that this double has had to
	# model. `signers._rows` reads the roster back off `frappe.get_doc` on every
	# Section 2 and `add_authorized_signer` appends to it — so a double that left
	# the rows as bare dicts would let `update_authorized_signer` appear to work
	# against a row that never made it into the store.
	("I-9 Settings", "authorized_signers"): "Authorized Signer",
	# v0.55.0. Supplement B, read as a doctype in its own right by the
	# `i9_supplement_b_unsigned` scanner — one query with a `parent` filter
	# answers "which forms have an unsigned reverification" for a whole
	# register, rather than loading every I-9 to find the handful with a
	# Section 3 at all. Without this the scanner sees an empty table and the
	# rule goes quiet, which is the failure that looks like compliance.
	("I-9 Form", "reverifications"): "I-9 Reverification",
}

#: Child tables `frappe.get_doc` rehydrates into Documents rather than leaving as
#: plain dicts. A row this app appends to and re-reads has to behave the same on
#: the second read as on the first.
REHYDRATED_CHILD_FIELDS = (
	"accounts",
	"payment_entries",
	"companies",
	"cost_center_allocation",
	"depreciation_postings",
	"payment_events",
	"references",
	"renewals",
	"corrective_actions_required",
	"columns",
	"shortcuts",
	"links",
	"number_cards",
	"charts",
	"evidence_files",
	"photos",
	"sample_photos",
	# v0.19.3. `add_worker_to_shift` and `remove_worker_from_shift` re-read a
	# shift, walk its crew rows and mutate one — which needs the rows to behave
	# the same on the second read as on the first.
	"crew",
	"compliance_events",
	"weather_timeline",
	"acclimatization_plan",
	# v0.19.4. The Weather Settings form re-reads its own override rows and the
	# controller walks them looking for two rows naming one company.
	"per_company_overrides",
	# v0.37.0. `approve_inspection_template` re-reads a template it did not write
	# and saves it, so the Inspection Template controller walks the section rows
	# tidying names and order indexes — which needs them to be documents on the
	# second read as well as on the first.
	"sections",
	# v0.40.0. `configure_payroll_accounts` re-reads a mapping it did not write
	# and merges rows into it, and `post_payroll_to_gl` re-reads its own GL
	# postings to decide whether the run is already in the ledger.
	"components",
	"gl_postings",
	# v0.41.0. `update_farm_task_template` re-reads a template it did not write
	# and replaces its checklist whole, so the Farm Task Template controller walks
	# the rows checking names and filling in sort orders — which needs them to be
	# documents on the second read as well as on the first.
	"checklist",
	"compliance_regimes",
	# `sign_session_attendance` and `complete_training_session` both re-read a
	# session they did not write, find one attendee row and set a field on it —
	# which is `.set()` and attribute assignment, neither of which a bare dict has.
	"attendees",
	# v0.48.0. The authorized signer roster, on a Single. `update_authorized_signer`
	# and `remove_authorized_signer` re-read I-9 Settings, find one row and set a
	# field on it — which is `.set()`, which a bare dict does not have.
	"authorized_signers",
)


class Document(FrappeDict):
	"""A stand-in for frappe.model.document.Document.

	Runs the controller hooks this app relies on (`validate`, `before_save`,
	`on_update`) in Frappe's order, because two of this app's guarantees — the
	settings form refusing a bad CIDR, the audit log refusing an update — live in
	exactly those hooks.
	"""

	def __init__(self, data=None):
		super().__init__(data or {})
		self.flags = FrappeDict()
		self._doc_before_save = None

	# -- lifecycle ------------------------------------------------------------
	def insert(self, ignore_permissions=False, ignore_if_duplicate=False):
		self.flags.in_insert = True
		# Frappe runs `autoname` before validation, and a doctype whose docname
		# is built from its own fields (Account is the one this app writes)
		# depends on that order. Falling straight through to a serial name would
		# make every docname in a chart-of-accounts import a fiction.
		self._run("autoname")
		if not self.get("name"):
			self.name = _autoname_from_meta(self) or STORE.next_name(self.doctype)
		self.creation = _now()
		self.modified = self.creation
		self.owner = self.get("owner") or frappe.session.user
		self.docstatus = int(self.get("docstatus") or 0)
		self._run("before_validate")
		self._run("validate")
		self._run("before_save")
		self._validate_links()
		self._validate_selects()
		self._validate_datetimes()
		self._name_children()
		STORE.put(self)
		self._run("after_insert")
		self._run("on_update")
		self.flags.in_insert = False
		return self

	def save(self, ignore_permissions=False):
		if not self.get("name"):
			return self.insert(ignore_permissions=ignore_permissions)
		self._doc_before_save = STORE.get_raw(self.doctype, self.name)
		self.modified = _now()
		self._run("before_validate")
		self._run("validate")
		self._run("before_save")
		self._validate_links()
		self._validate_selects()
		self._validate_datetimes()
		self._name_children()
		STORE.put(self)
		self._run("on_update")
		return self

	def _name_children(self):
		"""Give every child row a docname, as Frappe does on save.

		Not cosmetic. Frappe names child rows with a hash and other tables point
		at them: a GL Entry's `voucher_detail_no` IS the Journal Entry Account
		row's name, and it is the only thing that tells two identical lines of one
		voucher apart. A double that left children unnamed would let a tool that
		matched on it appear to work while matching nothing, or — worse — let one
		that matched on account and amount instead look correct here and update
		the wrong row on a real site.

		Names are assigned once and never reassigned, because a row that changed
		its name on every save would orphan whatever already points at it.
		"""
		for (parent, fieldname), child_doctype in CHILD_TABLES.items():
			if parent != self.doctype:
				continue
			for index, row in enumerate(self.get(fieldname) or [], start=1):
				if not isinstance(row, dict):  # pragma: no cover - rows are always dicts
					continue
				row.setdefault("idx", index)
				if not row.get("name"):
					row["name"] = STORE.next_child_name(child_doctype)
				row.setdefault("parent", self.get("name"))
				row.setdefault("parenttype", self.doctype)
				row.setdefault("parentfield", fieldname)

	def submit(self):
		self.docstatus = 1
		self._run("before_submit")
		STORE.put(self)
		self._run("on_submit")
		return self

	def cancel(self):
		self.docstatus = 2
		self._run("before_cancel")
		STORE.put(self)
		self._run("on_cancel")
		return self

	def reload(self):
		fresh = STORE.get_raw(self.doctype, self.name)
		if fresh:
			self.update(copy.deepcopy(fresh))
		return self

	def _validate_links(self):
		"""Refuse a Link or Dynamic Link that points at nothing, as Frappe does.

		WHY THIS IS WORTH THE FIDELITY. Frappe runs this on every insert and save,
		and it is the check that stopped v0.12.0 migrating: a Party Type whose
		`party_type` field is a Link to DocType cannot name a DocType that does
		not exist. Without this here, the suite certified a patch that took a real
		bench down.

		Three cases, and the third is the one that matters:

		  * a Link whose `options` is an ordinary doctype — the value has to be a
		    row in it.
		  * a **Dynamic Link** — the target doctype comes from the field named in
		    `options`, which is how a Journal Entry line's `party` is resolved
		    through its `party_type`.
		  * a Link whose `options` is the literal `"DocType"` — the value has to
		    be a doctype this site HAS. That is the Party Type case.

		Scoped to doctypes whose meta the fixture actually knows, and skipped
		entirely when `flags.ignore_links` is set, both of which Frappe also does.
		A link to a doctype the fixture has never heard of is not validated, since
		the double cannot tell "absent record" from "absent schema" and guessing
		wrong would refuse perfectly good fixtures.
		"""
		if self.flags.get("ignore_links"):
			return
		self._validate_links_on(self.doctype, self)
		# Child rows carry links of their own, and on a Journal Entry they carry
		# the ones that matter: `party_type` and `party` are on the LINE, not on
		# the header. Validating only the parent would have left the whole party
		# mechanism unchecked, which is the gap this release closed.
		for (parent, fieldname), child_doctype in CHILD_TABLES.items():
			if parent != self.doctype:
				continue
			for row in self.get(fieldname) or []:
				self._validate_links_on(child_doctype, row)

	def _validate_selects(self):
		"""Refuse a Select value the field does not offer, as Frappe does.

		THIS IS THE v0.16.1 GAP, AND IT COST A RELEASE. The double validated Links
		faithfully and did not look at Selects at all, so v0.16.0 could write
		`indicator="gray"` into a `Kanban Board Column` whose real options are
		capitalised, pass 2864 tests, and then throw on `doc.insert()` during
		`bench migrate` on Tim's site. The exception was swallowed by an installer
		that discarded its own report, so the migration reported success and the
		board did not exist.

		Both halves of that failure are now closed — the installer prints what it
		could not build, and this refuses the value that broke it — but this is the
		half that makes the *class* of bug catchable rather than the instance.

		Faithful to Frappe in the three ways that matter: an empty value is always
		allowed (it means "not set"), a field with no options at all is not
		policed (that is a customised or dynamically-populated Select, and Frappe
		does not police those either), and child rows are checked as well as the
		parent — which is where `indicator` actually lives.
		"""
		if self.flags.get("ignore_validate"):
			return
		self._validate_selects_on(self.doctype, self)
		for (parent, fieldname), child_doctype in CHILD_TABLES.items():
			if parent != self.doctype:
				continue
			for row in self.get(fieldname) or []:
				self._validate_selects_on(child_doctype, row)

	def _validate_datetimes(self):
		"""Refuse a Datetime or Date value MariaDB would refuse, as the column does.

		THIS IS THE v0.59.0 GAP, AND IT IS THE SAME SHAPE AS v0.16.1's. The double
		stored whatever string it was handed into a `Datetime` field, so
		`pull_model_from_vv` could write Volume Vision's own
		`2026-07-08T02:38:43Z` into `ML Model.training_completed_at`, pass the
		whole standalone suite, and then fail on Tim's site with
		`OperationalError (1292, "Incorrect datetime value")` — after the model
		had already come down the wire. Every JSON producer on earth writes ISO
		8601 and no MariaDB DATETIME accepts one, so this is a boundary the app
		crosses often and could not previously test.

		THE RULE IS MariaDB'S, NOT `datetime.fromisoformat`'s. A `T` between the
		date and the time is tolerated (the server takes it); a zone designator —
		a trailing `Z`, or a `+02:00` — is NOT, because a DATETIME column has
		nowhere to put one, and that refusal is the entire bug. A Date column
		accepts a datetime string, which the server truncates.

		Reimplemented here rather than imported from `model_registry.as_mariadb_datetime`,
		for the reason `account_autoname` is reimplemented above: two independent
		copies of a rule that must match the server is how a test notices one of
		them drifting. A double that called the app's own converter would agree
		with the app and prove nothing.
		"""
		if self.flags.get("ignore_validate"):
			return
		self._validate_datetimes_on(self.doctype, self)
		for (parent, fieldname), child_doctype in CHILD_TABLES.items():
			if parent != self.doctype:
				continue
			for row in self.get(fieldname) or []:
				self._validate_datetimes_on(child_doctype, row)

	def _validate_datetimes_on(self, doctype: str, doc):
		meta = META.get(doctype)
		if meta is None:
			return
		for field in meta.fields:
			fieldtype = field.get("fieldtype")
			if fieldtype not in ("Datetime", "Date"):
				continue
			value = doc.get(field["fieldname"])
			if value in (None, "") or isinstance(value, (datetime.datetime, datetime.date)):
				continue
			match = _MARIADB_DATETIME.match(str(value).strip())
			calendar_error = None
			if match:
				try:
					datetime.datetime(
						int(match["year"]),
						int(match["month"]),
						int(match["day"]),
						int(match["hour"] or 0),
						int(match["minute"] or 0),
						int(match["second"] or 0),
					)
				except ValueError as exc:
					calendar_error = str(exc)
			if not match or calendar_error:
				raise ValidationError(
					f"Incorrect datetime value: {str(value)!r} for column "
					f"{doctype}.{field['fieldname']} — MariaDB takes 'YYYY-MM-DD HH:MM:SS' and "
					f"nothing else. An ISO 8601 string with a trailing 'Z' or a '+02:00' offset is "
					f"the usual source of this; convert it before the save."
				)

	def _validate_selects_on(self, doctype: str, doc):
		meta = META.get(doctype)
		if meta is None:
			return
		for field in meta.fields:
			if field.get("fieldtype") != "Select":
				continue
			options = str(field.get("options") or "").split("\n")
			if not [line for line in options if line.strip()]:
				# No options: a Select whose choices the site fills in at runtime.
				continue
			value = doc.get(field["fieldname"])
			if value in (None, ""):
				continue
			if str(value) not in options:
				raise ValidationError(
					f"{value!r} is not a valid value for {doctype}.{field['fieldname']}. "
					f"Options are: {', '.join(line for line in options if line.strip())}"
				)

	def _validate_links_on(self, doctype: str, doc):
		meta = META.get(doctype)
		if meta is None:
			return
		for field in meta.fields:
			fieldtype = field.get("fieldtype")
			if fieldtype not in ("Link", "Dynamic Link"):
				continue
			value = doc.get(field.get("fieldname"))
			if value in (None, "", 0):
				continue

			if fieldtype == "Dynamic Link":
				target = doc.get(str(field.get("options") or ""))
				if not target:
					continue
			else:
				target = str(field.get("options") or "")
			if not target:
				continue

			if target == "DocType":
				if str(value) not in INSTALLED_DOCTYPES:
					raise LinkValidationError(
						f"Could not find {field.get('label') or field.get('fieldname')}: {value}"
					)
				continue
			if target not in INSTALLED_DOCTYPES:
				continue
			if not STORE.get_raw(target, str(value)):
				raise LinkValidationError(
					f"Could not find {field.get('label') or field.get('fieldname')}: {value}"
				)

	def _run(self, hook: str):
		method = getattr(self, hook, None)
		if callable(method):
			method()

	# -- data -----------------------------------------------------------------
	def append(self, fieldname, value=None):
		rows = self.get(fieldname)
		if not isinstance(rows, list):
			rows = []
			self[fieldname] = rows
		child = Document(dict(value or {}))
		child.doctype = CHILD_TABLES.get((self.doctype, fieldname), "")
		child.parenttype = self.doctype
		child.parentfield = fieldname
		child.idx = len(rows) + 1
		rows.append(child)
		return child

	def set(self, fieldname, value):
		self[fieldname] = value

	def get(self, key, default=None):
		return dict.get(self, key, default)

	def as_dict(self, no_nulls=False, convert_dates_to_str=False):
		out = {}
		for key, value in self.items():
			if key in ("flags", "_doc_before_save"):
				continue
			if no_nulls and value is None:
				continue
			out[key] = value
		return FrappeDict(out)

	def get_doc_before_save(self):
		return self._doc_before_save

	def is_new(self) -> bool:
		return not self.get("creation")

	def db_set(self, fieldname, value, **kwargs):
		self[fieldname] = value
		STORE.put(self)

	def add_comment(self, comment_type, text):
		"""Frappe inserts a Comment row and RETURNS IT. So does this.

		The return value is not incidental: a tool that reports the docname of the
		note it left needs one, and a double returning None would let the tool ship
		reporting None forever. `STORE.comments` stays as it is because a dozen
		existing tests read it, and the Comment row is what a tool asserting on the
		timeline actually queries.
		"""
		STORE.comments.append(
			{"doctype": self.doctype, "name": self.name, "type": comment_type, "text": text}
		)
		return frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": comment_type,
				"content": text,
				"reference_doctype": self.doctype,
				"reference_name": self.name,
			}
		).insert()

	def get_password(self, fieldname, raise_exception=True):
		value = STORE.passwords.get((self.doctype, self.get("name"), fieldname))
		if value is None and raise_exception:
			raise ValidationError(f"no password stored for {fieldname}")
		return value


class FileDocument(Document):
	"""Only File has `get_content`, so only File gets it here.

	Putting it on the base Document would make every doctype quack like a file
	and hide a real bug where the app reads content off the wrong thing.

	`content` is faithful to Frappe in the part that matters: a File inserted with
	content has its bytes written to storage and its `file_url`, `file_name` and
	`file_size` filled in from them, and the content itself is NOT a column on the
	row afterwards. An app that read the field back would be reading something a
	real site does not keep.
	"""

	def get_content(self):
		if self.name not in STORE.file_contents:
			raise OSError(f"no stored content for File {self.name}")
		return STORE.file_contents[self.name]

	def validate(self):
		if self.get("content") is None:
			return
		data = _as_bytes(self["content"])
		self["file_name"] = self.get("file_name") or "attachment"
		self["file_size"] = len(data)
		folder = "/private/files/" if int(self.get("is_private") or 0) else "/files/"
		self["file_url"] = folder + self["file_name"]

	def on_update(self):
		if self.get("content") is None:
			return
		STORE.file_contents[self.name] = _as_bytes(self.pop("content"))
		STORE.put(self)


def _as_bytes(value) -> bytes:
	return value.encode("utf-8") if isinstance(value, str) else bytes(value or b"")


def account_autoname(account_number, account_name, abbr: str) -> str:
	"""ERPNext's `get_account_autoname`, reproduced.

	Deliberately a second implementation rather than an import of
	`erpnext_mcp.charts.account_docname`. The app duplicates this rule too (it
	has to, to predict a docname during a dry run), and two independent copies of
	a rule that must match ERPNext is how a test notices one of them drifting. A
	shared helper would agree with itself and prove nothing.
	"""
	parts = [str(account_name or "").strip()]
	if str(abbr or "").strip():
		parts.append(str(abbr).strip())
	number = str(account_number or "").strip()
	if number:
		parts.insert(0, number)
	return " - ".join(part for part in parts if part)


class AccountDocument(Document):
	"""Account, with the parts of ERPNext's controller this app leans on.

	Being faithful here is not decoration. Three of this project's shipped bugs
	came from the double being more permissive than the framework, and Account is
	where that would bite hardest: its docname *encodes* two of its own fields,
	and ERPNext refuses to save a root account at all. A double that named
	accounts `A-00001` and happily saved roots would make every rename, every
	root refusal and every parent link in `tools/accounts.py` untestable — and
	those are the whole of what the module does.

	Reproduced from `Account.autoname`, `validate_parent`, `validate_root_details`
	and `set_root_and_report_type`. Everything else ERPNext's controller does
	(nested-set maintenance, child-company sync, currency checks) is left out;
	the app does not depend on it.
	"""

	def autoname(self):
		abbr = frappe.db.get_value("Company", self.get("company"), "abbr") or ""
		self.name = account_autoname(self.get("account_number"), self.get("account_name"), abbr)

	def validate(self):
		parent = str(self.get("parent_account") or "").strip()
		if not parent:
			# validate_root_details: an account with no parent is a root, and a
			# root that already exists cannot be saved at all.
			if not self.flags.in_insert:
				raise ValidationError("Root cannot be edited.")
			if not int(self.get("is_group") or 0):
				raise ValidationError(f"The root account {self.get('account_name')} must be a group")
			# ...and then Frappe's own mandatory pass, which runs after the
			# controller hook and refuses the insert because ERPNext's Account
			# marks parent_account `reqd`. ERPNext's chart importer gets past it
			# with `flags.ignore_mandatory` for root nodes only, and so does this
			# app. Without this branch the double would create roots the framework
			# refuses, which is how the bug shipped.
			if not self.flags.ignore_mandatory:
				raise MandatoryError(f"[Account, {self.get('name')}]: parent_account")
		else:
			row = STORE.get_raw("Account", parent)
			if row is None:
				raise DoesNotExistError(f"Could not find Parent Account: {parent}")
			if not int(row.get("is_group") or 0):
				raise ValidationError(f"Account {parent} cannot be a parent account: it is a ledger")
			if row.get("company") != self.get("company"):
				raise ValidationError("Account and parent account must belong to the same company")
			if not self.get("root_type"):
				self.root_type = row.get("root_type")
		# set_root_and_report_type
		self.report_type = (
			"Balance Sheet"
			if self.get("root_type") in ("Asset", "Liability", "Equity")
			else "Profit and Loss"
		)


class CostCenterDocument(Document):
	"""Cost Center, with the parts of ERPNext's controller this app leans on.

	Reproduced from `CostCenter.autoname`, `validate_mandatory` and
	`validate_parent_cost_center`. The two rules that matter to the app are the
	docname — `"<number> - <name> - <abbr>"`, the same shape Account uses and the
	one `dimensions.cost_center_docname` predicts — and the root rule, which is
	that a cost center with no parent must be named exactly after its company.
	The app cites that rule in two refusals, so a double that let anything be a
	root would make both of them look like this app being obstructive.
	"""

	def autoname(self):
		abbr = frappe.db.get_value("Company", self.get("company"), "abbr") or ""
		self.name = account_autoname(self.get("cost_center_number"), self.get("cost_center_name"), abbr)

	def validate(self):
		parent = str(self.get("parent_cost_center") or "").strip()
		if not parent:
			if self.get("cost_center_name") != self.get("company"):
				raise ValidationError("Please enter parent cost center")
			return
		if self.get("cost_center_name") == self.get("company"):
			raise ValidationError("Root cannot have a parent cost center")
		row = STORE.get_raw("Cost Center", parent)
		if row is None:
			raise DoesNotExistError(f"Could not find Parent Cost Center: {parent}")
		if not int(row.get("is_group") or 0):
			raise ValidationError(
				f"{parent} is not a group node. Please select a group node as parent cost center"
			)
		if row.get("company") != self.get("company"):
			raise ValidationError("Cost Center and parent cost center must belong to the same company")


class BankAccountDocument(Document):
	"""Bank Account, which names itself after the account and the institution.

	ERPNext's `BankAccount.autoname` is `" - ".join(filter(None, [account_name,
	bank]))`, which is why the fixture's one account is called
	`Operating - Example Bank`. Reproduced because `create_bank_account` reports
	the docname it produced and a caller wires a bank feed to that string; a double
	that named it `BA-00001` would make the one field anybody copies out of the
	response a fiction.
	"""

	def autoname(self):
		parts = [str(self.get("account_name") or "").strip(), str(self.get("bank") or "").strip()]
		self.name = " - ".join(part for part in parts if part)


class WarehouseDocument(Document):
	"""Warehouse, which names itself `"<warehouse_name> - <company abbr>"`.

	Reproduced from ERPNext's `Warehouse.autoname` for the reason
	`AccountDocument` and `BankAccountDocument` are reproduced: `create_warehouse`
	PREDICTS that docname before it writes anything, so it can refuse a collision
	with a sentence instead of a framework error — and a double that named
	warehouses `WH-00001` would make the prediction, the collision check and the
	docname the response hands back all fictions at once.
	"""

	def autoname(self):
		abbr = frappe.db.get_value("Company", self.get("company"), "abbr") or ""
		name = str(self.get("warehouse_name") or "").strip()
		self.name = f"{name} - {abbr}" if abbr else name


class DocTypeDocument(Document):
	"""Inserting a DocType makes it exist, which is the whole point of the test.

	`create_accounting_dimension` can generate the master DocType a dimension's
	values live in. A double where that insert wrote a row nobody could then
	create records in would let the tool "succeed" and prove nothing.
	"""

	def on_update(self):
		register_doctype(
			self.name,
			self.get("fields") or [],
			issingle=bool(int(self.get("issingle") or 0)),
			autoname=str(self.get("autoname") or ""),
		)


class CustomFieldDocument(Document):
	"""Inserting a Custom Field makes `frappe.get_meta` report the field.

	Faithful, and load-bearing twice over: it is what lets a test create a
	dimension and then use it on a journal entry line, and it is what makes
	`create_accounting_dimension`'s idempotency — "this doctype already has the
	field, skip it" — reachable at all.
	"""

	def on_update(self):
		add_field(
			self.get("dt"),
			self.get("fieldname"),
			fieldtype=str(self.get("fieldtype") or "Data"),
			options=self.get("options"),
			label=self.get("label"),
			reqd=self.get("reqd") or 0,
		)


def _autoname_from_meta(doc) -> str:
	"""Frappe's `field:<fieldname>` and `prompt` naming rules."""
	meta = META.get(doc.doctype)
	autoname = str(getattr(meta, "autoname", "") or "") if meta else ""
	if autoname == "prompt":
		return str(doc.get("__newname") or "").strip()
	if not autoname.startswith("field:"):
		return ""
	return str(doc.get(autoname.split(":", 1)[1]) or "").strip()


#: Link fields `rename_doc` repoints, per renamed doctype. See `rename_doc`.
RENAME_LINK_FIELDS = {
	"Account": (
		("Account", "parent_account"),
		("GL Entry", "account"),
		("Bank Account", "account"),
	),
	"Cost Center": (
		("Cost Center", "parent_cost_center"),
		("GL Entry", "cost_center"),
		("Company", "cost_center"),
		("Company", "round_off_cost_center"),
	),
	# v0.68.1. The five org masters `tools/org.py` renames. Every entry here is a
	# Link a real bench would repoint, and the Employee columns are the point:
	# correcting "Mill Creak" to "Mill Creek" has to carry the forty people
	# already posted to it, or the rename is a way to orphan a crew.
	"Designation": (
		("Employee", "designation"),
		("Position Wage Default", "designation"),
	),
	"Department": (
		("Employee", "department"),
		("Department", "parent_department"),
		("Attendance", "department"),
	),
	"Branch": (("Employee", "branch"),),
	"Employment Type": (("Employee", "employment_type"),),
	"Employee Grade": (("Employee", "grade"),),
}


class JournalEntryDocument(Document):
	"""ERPNext's Journal Entry, in the one respect that broke a real ledger.

	`JournalEntry.set_amounts_in_account_currency` does not fill the
	`*_in_account_currency` columns in from `debit`/`credit`. It runs the other
	way: `debit = debit_in_account_currency * exchange_rate`, on every validate.
	So a line built with `debit` alone inserts looking correct — the zero check
	has already run against the values as given — and is written to the database
	with its debit zeroed. The draft then exists, reads as 0.00, and is refused
	the moment anything validates it again:

	    Row 1: Both Debit and Credit values cannot be zero

	which is what four auto-generated opening-balance entries did on a live site
	under v0.8.0, and why `tools/mutate.py` now fills both columns for every line.

	Modelling it here in that order — check, then derive — is the point. A double
	that derived first would fail the *insert*, which is not what the site did,
	and a double that filled the columns in from `debit` (the intuitive
	direction) would make the broken code pass. This is the fourth time in this
	project's history that a permissive double let a real site break; see the
	0.8.0 changelog on `AccountDocument` and mandatory fields.

	`before_submit` re-runs it because real Frappe's `submit()` goes through
	`save()`, and validating only on insert would let a draft full of zeros post.
	"""

	def validate(self):
		total_debit = 0.0
		total_credit = 0.0
		for row in self.get("accounts") or []:
			debit = float(row.get("debit") or 0)
			credit = float(row.get("credit") or 0)
			if not debit and not credit:
				raise ValidationError(f"Row {row.get('idx')}: Both Debit and Credit values cannot be zero")
			rate = float(row.get("exchange_rate") or 0) or 1.0
			row["debit_in_account_currency"] = round(float(row.get("debit_in_account_currency") or 0), 2)
			row["credit_in_account_currency"] = round(float(row.get("credit_in_account_currency") or 0), 2)
			row["exchange_rate"] = rate
			row["debit"] = round(row["debit_in_account_currency"] * rate, 2)
			row["credit"] = round(row["credit_in_account_currency"] * rate, 2)
			total_debit += row["debit"]
			total_credit += row["credit"]
		self.total_debit = round(total_debit, 2)
		self.total_credit = round(total_credit, 2)
		self.difference = round(total_debit - total_credit, 2)
		if abs(self.difference) > 0.005:
			raise ValidationError(
				f"Total Debit must be equal to Total Credit. The difference is {self.difference}"
			)

	def before_submit(self):
		self.validate()


def post_journal_entry_gl(name: str) -> list[dict]:
	"""Write the GL Entry rows a real ERPNext submit would write for one entry.

	THE FIFTH TIME A PERMISSIVE DOUBLE CERTIFIED CODE THAT COULD NOT WORK. Until
	v0.14.0 the tests seeded GL rows by hand with
	`voucher_detail_no = <the account line's docname>`, because that is the
	obvious thing to write and because it is true of Sales Invoice Item. It is
	not true of Journal Entry. ERPNext's `JournalEntry.get_gl_entries` fills that
	column from the line's **`reference_detail_no`** — a pointer at a payment
	schedule row on an invoice being settled, empty on every ordinary line — so a
	real Journal Entry's GL rows carry NO line docname whatsoever.

	v0.13.0's `update_journal_entry_party` looked its GL rows up by that column,
	the fixture agreed, every test passed, and on Tim's site the tool matched zero
	rows on every submitted entry: it updated the voucher, left the general ledger
	saying the old party, and blamed the site in a warning. That is precisely the
	failure the module docstring at the top of this file promises this double
	exists to prevent, and it happened anyway because the double was written from
	the same wrong belief as the code.

	TWO THINGS ARE MODELLED, AND THE SECOND MATTERS AS MUCH AS THE FIRST.

	  * `voucher_detail_no` comes from `reference_detail_no`, so it is empty
	    unless a test deliberately sets one.
	  * GL entries are MERGED. `make_gl_entries` runs `merge_similar_entries` by
	    default, which collapses rows sharing an account, cost center, party and
	    against-voucher into ONE row with the amounts summed. So a two-line entry
	    posting twice to the same account produces one GL row, not two — and a
	    tool that writes a party onto it would attribute both lines to one person.
	    A double that emitted one row per line would have made that unreachable.

	Cancelled and draft entries post nothing, as they do on a real site. Returns
	the rows it wrote.
	"""
	entry = STORE.get_raw("Journal Entry", name)
	if entry is None:
		raise DoesNotExistError(f"Journal Entry {name} not found")
	if int(entry.get("docstatus") or 0) != 1:
		return []

	merged: dict = {}
	order: list = []
	for row in entry.get("accounts") or []:
		detail_no = str(row.get("reference_detail_no") or "")
		key = (
			str(row.get("account") or ""),
			str(row.get("cost_center") or ""),
			str(row.get("party_type") or ""),
			str(row.get("party") or ""),
			detail_no,
			str(row.get("reference_type") or ""),
			str(row.get("reference_name") or ""),
		)
		if key not in merged:
			merged[key] = {
				"name": f"GL-{name}-{len(order) + 1}",
				"account": row.get("account"),
				"posting_date": entry.get("posting_date"),
				"debit": 0.0,
				"credit": 0.0,
				"company": entry.get("company"),
				"is_cancelled": 0,
				"voucher_type": "Journal Entry",
				"voucher_no": name,
				"voucher_detail_no": detail_no,
				"party_type": row.get("party_type"),
				"party": row.get("party"),
				"cost_center": row.get("cost_center"),
				"against_voucher_type": row.get("reference_type"),
				"against_voucher": row.get("reference_name"),
				"is_opening": "Yes" if entry.get("is_opening") == "Yes" else "No",
			}
			order.append(key)
		merged[key]["debit"] = round(merged[key]["debit"] + float(row.get("debit") or 0), 2)
		merged[key]["credit"] = round(merged[key]["credit"] + float(row.get("credit") or 0), 2)

	rows = [merged[key] for key in order]
	table = STORE.tables.setdefault("GL Entry", {})
	for row in rows:
		row.setdefault("docstatus", 1)
		row.setdefault("creation", _now())
		table[row["name"]] = row
	return rows


class PurchaseOrderDocument(Document):
	"""Purchase Order, in the two respects `tools/purchasing.py` reads back.

	Real ERPNext computes `grand_total` from `items` and derives `status` from
	`docstatus`, `per_received` and `per_billed` in `set_status()`, run on every
	validate and every submit. Modelled here for the same reason `AccountDocument`
	reproduces `validate_root_details`: `create_purchase_order` reports
	`grand_total` straight off the document `insert()` returned, and a double
	that left it at whatever the caller happened to pass would make that number
	a fiction the tool never actually computed.
	"""

	def validate(self):
		total = 0.0
		for row in self.get("items") or []:
			qty = float(row.get("qty") or 0)
			rate = float(row.get("rate") or 0)
			row["amount"] = round(qty * rate, 2)
			total += row["amount"]
		self.grand_total = round(total, 2)
		self.rounded_total = self.grand_total
		self.per_received = float(self.get("per_received") or 0)
		self.per_billed = float(self.get("per_billed") or 0)
		self.status = self._computed_status()

	def before_submit(self):
		self.validate()

	def _computed_status(self) -> str:
		docstatus = int(self.get("docstatus") or 0)
		if docstatus == 2:
			return "Cancelled"
		if docstatus == 0:
			return "Draft"
		received = self.per_received >= 100
		billed = self.per_billed >= 100
		if received and billed:
			return "Completed"
		if received:
			return "To Bill"
		if billed:
			return "To Receive"
		return "To Receive and Bill"


class PurchaseReceiptDocument(Document):
	"""Purchase Receipt: `grand_total` from items, `status` from `per_billed`.

	Same reasoning as `PurchaseOrderDocument` — `create_purchase_receipt` and
	`submit_purchase_receipt` report fields ERPNext's own controller computes,
	not fields this app writes.
	"""

	def validate(self):
		total = 0.0
		for row in self.get("items") or []:
			qty = float(row.get("qty") or 0)
			rate = float(row.get("rate") or 0)
			row["amount"] = round(qty * rate, 2)
			total += row["amount"]
		self.grand_total = round(total, 2)
		self.per_billed = float(self.get("per_billed") or 0)
		self.status = self._computed_status()

	def before_submit(self):
		self.validate()

	def _computed_status(self) -> str:
		docstatus = int(self.get("docstatus") or 0)
		if docstatus == 2:
			return "Cancelled"
		if docstatus == 0:
			return "Draft"
		return "Completed" if self.per_billed >= 100 else "To Bill"


class PurchaseInvoiceDocument(Document):
	"""Purchase Invoice: `grand_total` and `outstanding_amount` from items.

	`outstanding_amount` is set equal to `grand_total` the moment the invoice is
	submitted, and left alone before that — real ERPNext's payment ledger starts
	an invoice fully outstanding at submit, and it is `PaymentEntryDocument.on_submit`
	that moves it from there, exactly as ERPNext's own controller does when a
	real Payment Entry against the invoice is submitted. This class does not
	touch it a second time.
	"""

	def validate(self):
		total = 0.0
		for row in self.get("items") or []:
			qty = float(row.get("qty") or 0)
			rate = float(row.get("rate") or 0)
			row["amount"] = round(qty * rate, 2)
			total += row["amount"]
		self.grand_total = round(total, 2)
		self.rounded_total = self.grand_total

	def before_submit(self):
		self.validate()

	def on_submit(self):
		self.outstanding_amount = self.grand_total
		self.status = "Unpaid" if self.grand_total > 0.005 else "Paid"


class PaymentEntryDocument(Document):
	"""Payment Entry: allocation totals on validate, invoice balances on submit.

	`total_allocated_amount` and `unallocated_amount` are what a caller reading
	`get_payment_entry` back expects to see computed, not typed. `on_submit` is
	this double's model of ERPNext's own `PaymentEntry.on_submit ->
	update_outstanding_amt`, which is what actually reduces a Purchase Invoice's
	`outstanding_amount` on a real site — `submit_payment_entry` triggers it and
	reports the result; it does not compute the reduction itself, and neither
	does anything in this file outside this one method.
	"""

	def validate(self):
		total_allocated = 0.0
		for row in self.get("references") or []:
			total_allocated += float(row.get("allocated_amount") or 0)
		self.total_allocated_amount = round(total_allocated, 2)
		self.unallocated_amount = round(float(self.get("paid_amount") or 0) - total_allocated, 2)

	def before_submit(self):
		self.validate()

	def on_submit(self):
		for row in self.get("references") or []:
			doctype = row.get("reference_doctype")
			name = row.get("reference_name")
			allocated = float(row.get("allocated_amount") or 0)
			if not doctype or not name or allocated <= 0:
				continue
			raw = STORE.get_raw(doctype, name)
			if raw is None:
				continue
			outstanding = float(raw.get("outstanding_amount") or 0)
			new_outstanding = round(max(0.0, outstanding - allocated), 2)
			raw["outstanding_amount"] = new_outstanding
			if new_outstanding <= 0.005:
				raw["status"] = "Paid"


def post_purchase_invoice_gl(name: str) -> list[dict]:
	"""Write the GL Entry rows a real ERPNext submit would write for one Purchase Invoice.

	One row per item line debiting its `expense_account`, and one row crediting
	`credit_to` for the total — merged by account and party the way
	`post_journal_entry_gl` merges Journal Entry lines, because two items
	expensed to the same account are one ledger movement, not two.

	NOT wired into `PurchaseInvoiceDocument.on_submit`, for the reason
	`post_journal_entry_gl` is not wired into `JournalEntryDocument.submit`: a
	test that needs GL rows to exist (an AP ageing report reading GL Entry
	against a Payable account) calls this explicitly, so the tests that only
	care about the invoice's own fields are not paying for postings nobody
	asked for.
	"""
	invoice = STORE.get_raw("Purchase Invoice", name)
	if invoice is None:
		raise DoesNotExistError(f"Purchase Invoice {name} not found")
	if int(invoice.get("docstatus") or 0) != 1:
		return []

	merged: dict = {}
	order: list = []

	def _line(account, debit, credit, party_type=None, party=None):
		key = (account, party_type or "", party or "")
		if key not in merged:
			merged[key] = {
				"name": f"GL-{name}-{len(order) + 1}",
				"account": account,
				"posting_date": invoice.get("posting_date"),
				"debit": 0.0,
				"credit": 0.0,
				"company": invoice.get("company"),
				"is_cancelled": 0,
				"voucher_type": "Purchase Invoice",
				"voucher_no": name,
				"voucher_detail_no": "",
				"party_type": party_type,
				"party": party,
				"cost_center": None,
				"is_opening": "No",
			}
			order.append(key)
		merged[key]["debit"] = round(merged[key]["debit"] + debit, 2)
		merged[key]["credit"] = round(merged[key]["credit"] + credit, 2)

	for row in invoice.get("items") or []:
		_line(row.get("expense_account"), float(row.get("amount") or 0), 0.0)
	_line(
		invoice.get("credit_to"),
		0.0,
		float(invoice.get("grand_total") or 0),
		"Supplier",
		invoice.get("supplier"),
	)

	rows = [merged[key] for key in order]
	table = STORE.tables.setdefault("GL Entry", {})
	for row in rows:
		row.setdefault("docstatus", 1)
		row.setdefault("creation", _now())
		table[row["name"]] = row
	return rows


def post_payment_entry_gl(name: str) -> list[dict]:
	"""Write the GL Entry rows a real ERPNext submit would write for one Payment Entry.

	Debits `paid_to` (extinguishing the payable) and credits `paid_from`
	(reducing the bank/cash balance) for `paid_amount`. Explicit, not automatic,
	for the same reason `post_purchase_invoice_gl` is.
	"""
	payment = STORE.get_raw("Payment Entry", name)
	if payment is None:
		raise DoesNotExistError(f"Payment Entry {name} not found")
	if int(payment.get("docstatus") or 0) != 1:
		return []

	amount = round(float(payment.get("paid_amount") or 0), 2)
	rows = [
		{
			"name": f"GL-{name}-1",
			"account": payment.get("paid_to"),
			"posting_date": payment.get("posting_date"),
			"debit": amount,
			"credit": 0.0,
			"company": payment.get("company"),
			"is_cancelled": 0,
			"voucher_type": "Payment Entry",
			"voucher_no": name,
			"voucher_detail_no": "",
			"party_type": payment.get("party_type"),
			"party": payment.get("party"),
			"cost_center": None,
			"is_opening": "No",
		},
		{
			"name": f"GL-{name}-2",
			"account": payment.get("paid_from"),
			"posting_date": payment.get("posting_date"),
			"debit": 0.0,
			"credit": amount,
			"company": payment.get("company"),
			"is_cancelled": 0,
			"voucher_type": "Payment Entry",
			"voucher_no": name,
			"voucher_detail_no": "",
			"party_type": None,
			"party": None,
			"cost_center": None,
			"is_opening": "No",
		},
	]
	table = STORE.tables.setdefault("GL Entry", {})
	for row in rows:
		row.setdefault("docstatus", 1)
		row.setdefault("creation", _now())
		table[row["name"]] = row
	return rows


class SalesInvoiceDocument(Document):
	"""Sales Invoice: net total from items, grand total after the charge rows.

	`amount` IS RECOMPUTED FROM `qty × rate` ON EVERY VALIDATE, and that is the
	whole reason this class exists rather than a plain Document. ERPNext really
	does this, and `tools/sales.py` is built around it: a settlement line whose
	stated gross amount does not equal weight × price has its RATE adjusted so
	the product comes out right, because the amount will not survive. A double
	that kept whatever `amount` the caller appended would let that adjustment be
	deleted and every test of it still pass.

	`outstanding_amount` is set equal to `grand_total` at submit and left alone
	before it — same contract as `PurchaseInvoiceDocument`, and it is
	`PaymentEntryDocument.on_submit` that moves it from there.
	"""

	def validate(self):
		net = 0.0
		for row in self.get("items") or []:
			qty = float(row.get("qty") or 0)
			rate = float(row.get("rate") or 0)
			row["amount"] = round(qty * rate, 2)
			net += row["amount"]
		charges = 0.0
		for row in self.get("taxes") or []:
			if str(row.get("charge_type") or "") == "On Net Total":
				row["tax_amount"] = round(net * float(row.get("rate") or 0) / 100.0, 2)
			charges += float(row.get("tax_amount") or 0)
		self.net_total = round(net, 2)
		self.total_taxes_and_charges = round(charges, 2)
		self.grand_total = round(net + charges, 2)
		self.rounded_total = self.grand_total

	def before_submit(self):
		self.validate()

	def on_submit(self):
		# `db_set`, NOT a plain assignment. `Document.submit` writes the row to the
		# store BEFORE it runs `on_submit`, exactly as Frappe writes to the
		# database before its own submit hooks — so a controller that changes a
		# field here has to write it through, which is what ERPNext's own
		# controllers do and why `db_set` exists at all. A plain assignment would
		# leave the value in memory only: the document in hand would look right and
		# `frappe.db.get_value(..., "outstanding_amount")` would answer 0, which is
		# what `receive_payment`'s allocation actually reads.
		self.db_set("outstanding_amount", self.grand_total)
		self.db_set("status", "Unpaid" if self.grand_total > 0.005 else "Paid")


def post_sales_invoice_gl(name: str) -> list[dict]:
	"""Write the GL Entry rows a real ERPNext submit would for one Sales Invoice.

	One row per item line CREDITING its `income_account`, one per charge row
	debiting its `account_head` (a negative charge is a debit — that is what a
	withheld packing deduction is), and one DEBITING `debit_to` for the grand
	total against the customer. Merged by account and party the way
	`post_purchase_invoice_gl` merges, because two lines to one income account
	are one ledger movement.

	NOT wired into `SalesInvoiceDocument.on_submit`, for the reason none of the
	others are: a test that only cares whether the docstatus moved should not
	pay for postings nobody asked for. A test about AR ageing calls this.
	"""
	invoice = STORE.get_raw("Sales Invoice", name)
	if invoice is None:
		raise DoesNotExistError(f"Sales Invoice {name} not found")
	if int(invoice.get("docstatus") or 0) != 1:
		return []

	merged: dict = {}
	order: list = []

	def _line(account, debit, credit, party_type=None, party=None):
		key = (account, party_type or "", party or "")
		if key not in merged:
			merged[key] = {
				"name": f"GL-{name}-{len(order) + 1}",
				"account": account,
				"posting_date": invoice.get("posting_date"),
				"debit": 0.0,
				"credit": 0.0,
				"company": invoice.get("company"),
				"is_cancelled": 0,
				"voucher_type": "Sales Invoice",
				"voucher_no": name,
				"voucher_detail_no": "",
				"party_type": party_type,
				"party": party,
				"cost_center": None,
				"is_opening": "No",
			}
			order.append(key)
		merged[key]["debit"] = round(merged[key]["debit"] + debit, 2)
		merged[key]["credit"] = round(merged[key]["credit"] + credit, 2)

	for row in invoice.get("items") or []:
		_line(row.get("income_account"), 0.0, float(row.get("amount") or 0))
	for row in invoice.get("taxes") or []:
		_line(row.get("account_head"), -float(row.get("tax_amount") or 0), 0.0)
	_line(
		invoice.get("debit_to"),
		float(invoice.get("grand_total") or 0),
		0.0,
		"Customer",
		invoice.get("customer"),
	)

	rows = [merged[key] for key in order]
	table = STORE.tables.setdefault("GL Entry", {})
	for row in rows:
		row.setdefault("docstatus", 1)
		row.setdefault("creation", _now())
		table[row["name"]] = row
	return rows


class StockEntryDocument(Document):
	"""Stock Entry, in the respects `tools/stock_inventory.py` reads back.

	Real ERPNext's `StockEntry.validate` fills `transfer_qty` from
	`qty * conversion_factor`, `basic_amount` from `transfer_qty * basic_rate`,
	and the three totals from the lines. Modelled here for the reason
	`PurchaseOrderDocument` models `grand_total`: `create_stock_entry` reports
	`total_qty` and `total_value` straight off the document `insert()` returned,
	and a double that left them at whatever the caller happened to pass would
	make both numbers a fiction the tool never computed.

	`total_incoming_value` and `total_outgoing_value` are split by which
	warehouse column a line carries, which is the whole point of the split: a
	Material Transfer is both, and nets to zero.
	"""

	def validate(self):
		incoming = 0.0
		outgoing = 0.0
		for row in self.get("items") or []:
			qty = float(row.get("qty") or 0)
			factor = float(row.get("conversion_factor") or 1) or 1.0
			row["transfer_qty"] = round(qty * factor, 6)
			rate = float(row.get("basic_rate") or 0)
			row["basic_amount"] = round(row["transfer_qty"] * rate, 2)
			if row.get("t_warehouse"):
				incoming += row["basic_amount"]
			if row.get("s_warehouse"):
				outgoing += row["basic_amount"]
		self.total_incoming_value = round(incoming, 2)
		self.total_outgoing_value = round(outgoing, 2)
		# ERPNext's `total_amount` is the value of the movement, which for a
		# transfer is one side of it rather than both added together.
		self.total_amount = round(max(incoming, outgoing), 2)

	def before_submit(self):
		self.validate()


def post_stock_entry_ledger(name: str) -> list[dict]:
	"""Write the Stock Ledger Entry rows and Bin updates a real submit would.

	One row per line per warehouse column — negative out of `s_warehouse`,
	positive into `t_warehouse`, so a Material Transfer produces two — with the
	Bin's `actual_qty` moved by the same amount and `qty_after_transaction`
	recorded as the balance that resulted. An incoming line carrying a
	`basic_rate` sets the Bin's valuation rate; this double does NOT model
	ERPNext's moving-average revaluation, because nothing in this app computes a
	valuation and a fake average would only invite a test to assert one.

	NOT wired into `StockEntryDocument.on_submit`, for the reason
	`post_purchase_invoice_gl` is not wired into its own document's: a test that
	only cares whether `submit_stock_entry` moved the docstatus should not pay
	for ledger rows nobody asked for. A test about balances calls this.

	Creating the Bin on first touch is deliberate and load-bearing: ERPNext does
	exactly that, and "no Bin row" versus "Bin row saying 0" is a distinction
	`get_stock_balance` and `list_reorder_alerts` treat differently on purpose.
	"""
	entry = STORE.get_raw("Stock Entry", name)
	if entry is None:
		raise DoesNotExistError(f"Stock Entry {name} not found")
	if int(entry.get("docstatus") or 0) != 1:
		return []

	bins = STORE.tables.setdefault("Bin", {})
	ledger = STORE.tables.setdefault("Stock Ledger Entry", {})
	rows = []

	def _move(item_code, warehouse, qty_change, rate):
		key = f"{item_code}-{warehouse}"
		row = bins.get(key)
		if row is None:
			row = {
				"name": key,
				"item_code": item_code,
				"warehouse": warehouse,
				"actual_qty": 0.0,
				"valuation_rate": 0.0,
				"stock_value": 0.0,
				"docstatus": 0,
				"creation": _now(),
			}
			bins[key] = row
		row["actual_qty"] = round(float(row.get("actual_qty") or 0) + qty_change, 6)
		if qty_change > 0 and rate:
			row["valuation_rate"] = rate
		row["stock_value"] = round(row["actual_qty"] * float(row.get("valuation_rate") or 0), 2)

		sle = {
			"name": f"SLE-{name}-{len(rows) + 1}",
			"item_code": item_code,
			"warehouse": warehouse,
			"posting_date": entry.get("posting_date"),
			"posting_time": entry.get("posting_time") or "00:00:00",
			"actual_qty": qty_change,
			"qty_after_transaction": row["actual_qty"],
			"valuation_rate": float(row.get("valuation_rate") or 0),
			"stock_value": row["stock_value"],
			"stock_value_difference": round(qty_change * float(row.get("valuation_rate") or 0), 2),
			"voucher_type": "Stock Entry",
			"voucher_no": name,
			"company": entry.get("company"),
			"is_cancelled": 0,
			"docstatus": 1,
			"creation": _now(),
		}
		ledger[sle["name"]] = sle
		rows.append(sle)

	for line in entry.get("items") or []:
		qty = float(line.get("transfer_qty") or line.get("qty") or 0)
		rate = float(line.get("basic_rate") or 0)
		if line.get("s_warehouse"):
			_move(line.get("item_code"), line.get("s_warehouse"), -qty, rate)
		if line.get("t_warehouse"):
			_move(line.get("item_code"), line.get("t_warehouse"), qty, rate)
	return rows


#: Doctypes whose stub behaviour differs from a plain Document.
STUB_CONTROLLERS = {
	"File": FileDocument,
	"Account": AccountDocument,
	"Bank Account": BankAccountDocument,
	"Cost Center": CostCenterDocument,
	"Warehouse": WarehouseDocument,
	"DocType": DocTypeDocument,
	"Custom Field": CustomFieldDocument,
	"Purchase Order": PurchaseOrderDocument,
	"Purchase Receipt": PurchaseReceiptDocument,
	"Purchase Invoice": PurchaseInvoiceDocument,
	"Sales Invoice": SalesInvoiceDocument,
	"Payment Entry": PaymentEntryDocument,
	"Journal Entry": JournalEntryDocument,
	"Stock Entry": StockEntryDocument,
}


#: The one permission a stock Frappe doctype ships with. Not a full palette:
#: what the v0.17.0 tests need is that SOMETHING standard exists to be discarded,
#: because "Frappe ignores every standard DocPerm once one Custom DocPerm exists"
#: is a rule you can only demonstrate against a standard DocPerm.
_STOCK_DOCPERM = {
	"role": "System Manager",
	"permlevel": 0,
	"read": 1,
	"write": 1,
	"create": 1,
	"delete": 1,
	"report": 1,
	"export": 1,
	"print": 1,
	"email": 1,
	"share": 1,
}


class Store:
	"""The in-memory database."""

	def __init__(self):
		self.reset()

	def reset(self):
		global _now_counter
		_now_counter = 0
		self.file_contents: dict[str, bytes] = {}
		self.denied_permissions: set = set()
		self.installed_apps: list[str] = ["frappe", "erpnext"]
		self.report_runners: dict = {}
		self.tables: dict[str, dict[str, dict]] = {}
		self.singles: dict[str, dict] = {}
		self.passwords: dict[tuple, str] = {}
		self.comments: list[dict] = []
		self.errors: list[dict] = []
		#: What `frappe.sendmail` was asked to send. v0.17.1 — the drift watch is
		#: the first thing in this app that emails anybody, and "did it send, and
		#: to whom" is the only interesting question about a watchdog.
		self.emails: list[dict] = []
		#: Set by a test to make `sendmail` raise, standing in for a site with no
		#: outgoing email account. Reset here so it cannot leak into the next test.
		self.mail_fails = False
		self.counters: dict[str, int] = {}
		self.committed = 0
		self.rolled_back = 0
		self._seed_doctypes()
		self._seed_app_reports()
		self._seed_roles()
		# Rows written since the last commit, so a rollback can discard exactly
		# those — which is what the audit-survives-rollback tests need to see.
		self.pending: list[tuple[str, str]] = []
		# Before-images of rows CHANGED or DELETED since the last commit, so a
		# rollback puts them back. Discarding new rows was never the whole of what
		# a rollback does, and modelling only that half made a multi-step tool look
		# atomic when it was not: `convey_parcel` repoints a dozen leases and
		# housing units before it deletes anything, and a double that kept those
		# updates through a rollback would certify a half-conveyed parcel as
		# impossible while a real MariaDB transaction was the only thing making it
		# so. One entry per (doctype, name), taken the FIRST time it is touched —
		# the state to restore is the one the transaction opened with, not the one
		# the second write found.
		self.before_images: dict[tuple[str, str], dict | None] = {}

	def _seed_app_reports(self):
		"""A Report row per standard report this app ships. v0.19.5.

		A REAL BENCH ALREADY HAS THESE by the time `after_migrate` runs: Frappe
		imports an app's `<module>/report/<name>/<name>.json` during the sync phase
		of `bench migrate`, which is before any `after_migrate` hook fires. The
		double did not, and the first thing that noticed was the KPI dashboard chart
		— whose source is a Report — printing "could not build" on every clean
		migrate in the suite. That was the fixture being wrong about a migrated
		site, not the installer being noisy, and the two tests asserting a clean
		migrate says nothing at all were right to fail.

		Read off the shipped JSON for the same reason `_seed_doctypes` is: a
		hardcoded row here would be a second declaration of the report, and the two
		would disagree the first time somebody renamed one.
		"""
		rows = {}
		for folder in APP_REPORTS:
			path = os.path.join(REPORT_DIR, folder, f"{folder}.json")
			try:
				with open(path) as handle:
					payload = json.load(handle)
			except OSError:  # pragma: no cover - a checkout missing the file
				continue
			rows[payload["name"]] = {
				"name": payload["name"],
				"report_name": payload.get("report_name") or payload["name"],
				"ref_doctype": payload.get("ref_doctype") or "",
				"report_type": payload.get("report_type") or "Script Report",
				"module": payload.get("module") or "ERPNext MCP",
				"is_standard": payload.get("is_standard") or "Yes",
				"disabled": int(payload.get("disabled") or 0),
				"prepared_report": int(payload.get("prepared_report") or 0),
				"add_total_row": int(payload.get("add_total_row") or 0),
				"json": "",
				"query": "",
			}
		self.tables.setdefault("Report", {}).update(rows)

	def _seed_roles(self):
		"""The stock roles a Frappe site ships with. v0.17.0.

		Needed because every DocType row now carries its own DocPerm rows, and a
		DocPerm's `role` is a Link — so a site with no Role table cannot hold a
		permission at all. Seeding the ones the double already pretends users hold
		(`ROLES`) plus `Employee`, which is the companion role the mobile installer
		looks for and deliberately does not create.
		"""
		self.tables["Role"] = {
			name: {
				"name": name,
				"role_name": name,
				"desk_access": 1,
				"disabled": 0,
				"is_custom": 0,
			}
			for name in (
				"System Manager",
				"Accounts Manager",
				"Accounts User",
				"Purchase Manager",
				"Purchase User",
				"Employee",
				"All",
			)
		}

	def _seed_doctypes(self):
		"""A row per DocType, because `tabDocType` really is a table.

		`frappe.db.exists("DocType", …)` is answered from `INSTALLED_DOCTYPES`
		(tests flip entries there to simulate a site missing an optional doctype),
		but `issingle` and `istable` are read as ordinary columns — which is how
		`create_accounting_dimension` refuses a Single or a child table as a
		dimension master. Those refusals need real rows to read.
		"""
		rows = {}
		child_tables = set(CHILD_TABLES.values())
		for doctype in ERPNEXT_SCHEMA:
			rows[doctype] = {
				"name": doctype,
				"module": "Core",
				"issingle": 0,
				"istable": 1 if doctype in child_tables else 0,
				# v0.17.0. Every core doctype gets the one standard permission a
				# stock Frappe install has, so `roles._mirror_standard_perms` has
				# a real row to copy — and so the test that it copies BEFORE
				# writing the first custom row has something to lose.
				"permissions": [dict(_STOCK_DOCPERM)],
			}
		for doctype, folder in APP_DOCTYPES.items():
			payload = _load_app_doctype(folder)
			rows[doctype] = {
				"name": doctype,
				"module": payload.get("module") or "ERPNext MCP",
				"issingle": int(payload.get("issingle") or 0),
				"istable": int(payload.get("istable") or 0),
				# The doctype's OWN shipped permissions, from its JSON. These are
				# what an operator loses if a Custom DocPerm lands without a
				# mirror first, so the fixture has to carry the real ones.
				"permissions": [dict(row) for row in (payload.get("permissions") or [])],
			}
		self.tables["DocType"] = rows

	def next_name(self, doctype: str) -> str:
		self.counters[doctype] = self.counters.get(doctype, 0) + 1
		prefix = "".join(word[0] for word in doctype.split() if word).upper() or "DOC"
		return f"{prefix}-{self.counters[doctype]:05d}"

	def next_child_name(self, doctype: str) -> str:
		"""A child row's docname. Frappe uses a hash; the shape does not matter.

		What matters is that it is opaque and stable: a test that could predict it
		from the row's contents would let a tool "find" a row by reconstructing the
		name rather than by following the pointer, which is not what a real site
		allows.
		"""
		key = f"child:{doctype}"
		self.counters[key] = self.counters.get(key, 0) + 1
		return f"{secrets.token_hex(5)}{self.counters[key]:03d}"

	def put(self, doc: Document):
		self._extract_passwords(doc)
		if META.get(doc.doctype) and META[doc.doctype].issingle:
			self.singles[doc.doctype] = _drop_grandchildren(_plain(doc), doc.doctype)
			return
		table = self.tables.setdefault(doc.doctype, {})
		is_new = doc.name not in table
		self.snapshot(doc.doctype, doc.name)
		table[doc.name] = _drop_grandchildren(_plain(doc), doc.doctype)
		if is_new:
			self.pending.append((doc.doctype, doc.name))

	def snapshot(self, doctype: str, name: str) -> None:
		"""Remember what a row looked like before this transaction touched it.

		Only the first touch is recorded. A row updated three times and then
		rolled back has to come back as it was before the first of them, and
		keeping the latest before-image would restore it to the state left by the
		second write — which is not a state the database was ever in.
		"""
		key = (doctype, name)
		if key in self.before_images:
			return
		row = self.tables.get(doctype, {}).get(name)
		self.before_images[key] = copy.deepcopy(row) if row is not None else None

	def _extract_passwords(self, doc: Document):
		"""Move Password field values out of the row, as Frappe does on save.

		Frappe writes a Password field to the encrypted `__Auth` table and leaves
		a row of asterisks in the document, which is why `get_password()` exists
		and why reading the field directly gives you nothing useful. Reproducing
		that here is what makes "the token is never returned to a caller"
		something the tests can actually check rather than take on trust.
		"""
		meta = META.get(doc.doctype)
		if not meta:
			return
		for field in meta.fields:
			if field.get("fieldtype") != "Password":
				continue
			value = doc.get(field["fieldname"])
			if value == "":
				# EXPLICITLY EMPTIED, WHICH FRAPPE TREATS AS A DELETION.
				# `Document.save_passwords` calls `remove_encrypted_password` for a
				# falsy value, and v0.17.0's revocation depends on it: `_clear_token`
				# revokes by setting `api_secret = ""`, and a double that kept the
				# old secret would let "revoke, then the credential stops working"
				# pass while the credential still worked.
				#
				# `""` and not `not value`. A document loaded from the store carries
				# None for a Password field nobody set, and treating THAT as a
				# deletion would wipe the settings token on every `seed_defaults`
				# save.
				self.passwords.pop((doc.doctype, doc.get("name"), field["fieldname"]), None)
				continue
			if value and not set(str(value)) <= {"*"}:
				self.passwords[(doc.doctype, doc.get("name"), field["fieldname"])] = value
				doc[field["fieldname"]] = "*" * len(str(value))

	def get_raw(self, doctype: str, name: str):
		if META.get(doctype) and META[doctype].issingle:
			return self.singles.get(doctype)
		return self.tables.get(doctype, {}).get(name)

	def rows(self, doctype: str) -> list[dict]:
		if META.get(doctype) and META[doctype].issingle:
			single = self.singles.get(doctype)
			return [single] if single else []
		return list(self.tables.get(doctype, {}).values())

	def seed(self, doctype: str, rows: list[dict]):
		table = self.tables.setdefault(doctype, {})
		for index, row in enumerate(rows, start=1):
			row = dict(row)
			row.setdefault("name", f"{doctype}-{index}")
			row.setdefault("docstatus", 0)
			row.setdefault("creation", _now())
			table[row["name"]] = row
		# Seeded fixtures are "already committed" state.
		self.pending.clear()
		self.before_images.clear()

	def commit(self):
		self.committed += 1
		self.pending.clear()
		self.before_images.clear()

	def rollback(self):
		self.rolled_back += 1
		for doctype, name in self.pending:
			self.tables.get(doctype, {}).pop(name, None)
		for (doctype, name), row in self.before_images.items():
			table = self.tables.setdefault(doctype, {})
			if row is None:
				table.pop(name, None)
			else:
				table[name] = copy.deepcopy(row)
		self.pending.clear()
		self.before_images.clear()


def _plain(doc) -> dict:
	out = {}
	for key, value in doc.items():
		if key in ("flags", "_doc_before_save"):
			continue
		if isinstance(value, list):
			out[key] = [_plain(item) if isinstance(item, dict) else item for item in value]
		else:
			out[key] = value
	return out


def _nested_table_fields(child_doctype: str) -> tuple:
	"""The Table fieldnames declared ON a child doctype — a GRANDCHILD table.

	Only one doctype in this app has any: `Wizard Step.fields` → `Wizard Field`.
	Read off the meta rather than listed, so a second one added later is covered
	by the rule below without anybody remembering this function exists.
	"""
	meta = META.get(child_doctype)
	if meta is None:
		return ()
	return tuple(
		str(field.get("fieldname"))
		for field in meta.fields
		if field.get("fieldtype") == "Table" and field.get("fieldname")
	)


def _drop_grandchildren(row: dict, doctype: str) -> dict:
	"""A child row is written as its own columns and NOTHING ELSE, as Frappe does.

	THIS IS THE v0.91.0 GAP AND IT COST TWO RELEASES. `Document.insert()` writes
	the parent, then walks `get_all_children()` — which reads `meta.get_table_fields()`
	on the PARENT and so goes exactly one level down — and `db_insert`s each row
	from `get_valid_dict()`, which has no place for a Table field. A grandchild
	appended onto a child row is therefore never written, and it is never read
	back either: `load_from_db` fills the parent's tables and stops.

	The double stored documents whole, so `wizard_key → steps → fields` survived
	a save here and came back nested on the next read. `install_wizard_definitions`
	appended fields onto step rows, `describe()` read them off the step it was
	handed, 9,859 tests agreed, and on Tim's site every one of the five shipped
	wizards answered `fields: []` — a form the handset correctly refuses to draw
	as "nothing to fill". A test asserting the seeder's own nesting could not
	have caught it, because the nesting was real in the double and fiction in
	MariaDB.

	So the double drops them, which makes the grandchild pattern fail here the
	way it fails on a bench: a wizard field only exists if something wrote it as
	a `Wizard Field` document of its own, with `parent` pointing at the step row.
	The in-memory document keeps its nested rows — that is faithful too, and it
	is what `WizardDefinition.validate` walks — so only the STORED copy is
	stripped.
	"""
	for (parent, fieldname), child_doctype in CHILD_TABLES.items():
		if parent != doctype:
			continue
		nested = _nested_table_fields(child_doctype)
		if not nested:
			continue
		for child in row.get(fieldname) or []:
			if not isinstance(child, dict):  # pragma: no cover - rows are always dicts
				continue
			for column in nested:
				child.pop(column, None)
	return row


STORE = Store()


# ── the fake frappe.db ──────────────────────────────────────────────────────
#: Sentinel for "the caller did not pass order_by", so the double can tell that
#: apart from an explicit None. Real Frappe uses the same trick, which is what
#: makes the default invisible at the call site and therefore easy to get wrong.
DEFAULT_ORDER_BY = object()

#: Tables that are NOT DocType tables and so have none of the framework columns.
#: `tabSingles` is three columns: doctype, field, value. Ordering by `modified`
#: — which `frappe.db.get_value` and `get_values` do unless told otherwise —
#: is `Unknown column 'modified' in 'ORDER BY'`.
#:
#: v0.2.0 shipped an `after_migrate` hook that did exactly that and broke
#: `bench migrate` on a live site. The double answered the query happily, which
#: is why the standalone suite did not catch it. It refuses now.
FRAMEWORKLESS_TABLES = {"Singles", "__Auth", "__global_search", "tabSeries"}

#: Columns every real DocType table has, whether or not this fixture's schema
#: bothers to list them.
FRAMEWORK_COLUMNS = frozenset({"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"})


class OperationalError(Exception):
	"""What pymysql raises for a bad column. Mirrored so tests can assert on it."""


def _reject_default_ordering(doctype: str, order_by) -> None:
	"""Fail the way MariaDB does when a query would ORDER BY a missing column.

	Only the default is rejected. A caller that passed `order_by=None`, or named
	a column the table has, knows what it is doing — the bug this reproduces is
	specifically the one you cannot see at the call site.
	"""
	if order_by is not DEFAULT_ORDER_BY:
		return
	if doctype not in FRAMEWORKLESS_TABLES:
		return
	raise OperationalError(
		f"(1054, \"Unknown column 'modified' in 'ORDER BY'\") — `tab{doctype}` is "
		"not a DocType table and has no framework columns. Pass order_by=None, or "
		"use the framework's own accessor (frappe.db.get_singles_dict for "
		"tabSingles)."
	)


class FakeDB:
	def escape(self, value, percent=True):
		"""`frappe.db.escape`, reproduced. v0.17.1.

		Returns a QUOTED, escaped SQL string literal — quoted is the part that
		catches people out, because a double that returned the bare escaped text
		would let `permissions.py` build a WHERE clause that looks right in a test
		and is a syntax error on MariaDB.

		Frappe delegates to pymysql's `escape_string` and wraps the result in
		single quotes; this does the same two substitutions in the same order. The
		order matters: escaping the quote first and the backslash second would
		re-escape the backslash this function just inserted.
		"""
		text = "" if value is None else str(value)
		text = text.replace("\\", "\\\\").replace("'", "\\'")
		if percent:
			text = text.replace("%", "%%")
		return f"'{text}'"

	def get_all(
		self,
		doctype,
		filters=None,
		fields=None,
		order_by=None,
		limit=None,
		limit_page_length=None,
		pluck=None,
		as_dict=True,
		distinct=False,
		group_by=None,
		**kwargs,
	):
		rows = [row for row in STORE.rows(doctype) if _match(row, filters)]
		if doctype in CHILD_TABLE_SOURCES:
			rows = [row for row in _child_rows(doctype) if _match(row, filters)]

		aggregates = [f for f in (fields or []) if "(" in str(f)]
		if aggregates:
			if group_by:
				return _grouped_aggregate(rows, fields, aggregates, group_by)
			return [_aggregate(rows, aggregates)]

		if order_by:
			rows = _sorted(rows, order_by)
		cap = limit or limit_page_length
		if cap:
			rows = rows[: int(cap)]
		if pluck:
			return [row.get(pluck) for row in rows]
		# `fields="*"` is Frappe's own idiom for "every column", and it is what
		# `frappe.core.page.permission_manager.copy_perms` passes when it mirrors
		# DocPerm into Custom DocPerm. A double that answered it with a single key
		# literally called "*" would make that mirror copy nothing while looking
		# like it worked. v0.17.0.
		if fields in ("*", ["*"], ("*",)):
			return [FrappeDict(copy.deepcopy(row)) for row in rows]
		if fields:
			return [FrappeDict({f: row.get(f) for f in fields}) for row in rows]
		return [FrappeDict(copy.deepcopy(row)) for row in rows]

	def get_value(
		self,
		doctype,
		filters=None,
		fieldname="name",
		as_dict=False,
		order_by=DEFAULT_ORDER_BY,
		**kwargs,
	):
		# Real `get_value` is `get_values(..., limit=1)`, so it inherits the same
		# default ordering and the same failure on a frameworkless table.
		_reject_default_ordering(doctype, order_by)
		rows = self.get_all(doctype, filters=filters)
		if not rows:
			return None
		row = rows[0]
		if isinstance(fieldname, (list, tuple)):
			if as_dict:
				return FrappeDict({f: row.get(f) for f in fieldname})
			return [row.get(f) for f in fieldname]
		if as_dict:
			return FrappeDict({fieldname: row.get(fieldname)})
		return row.get(fieldname)

	def get_values(
		self,
		doctype,
		filters=None,
		fieldname="name",
		as_dict=False,
		order_by=DEFAULT_ORDER_BY,
		**kwargs,
	):
		_reject_default_ordering(doctype, order_by)
		if doctype == "Singles":
			target = (filters or {}).get("doctype")
			return [
				FrappeDict({"field": key, "value": value})
				for key, value in (STORE.singles.get(target) or {}).items()
			]
		rows = self.get_all(doctype, filters=filters)
		fields = fieldname if isinstance(fieldname, (list, tuple)) else [fieldname]
		return [FrappeDict({f: row.get(f) for f in fields}) for row in rows]

	def get_singles_dict(self, doctype, debug=False, *, for_update=False, cast=False):
		"""The framework's own reader for `tabSingles` — no ORDER BY to get wrong.

		Returns `{fieldname: value}` for the fields that have a stored row, which
		is exactly the "which fields are already set" question `seed_defaults`
		asks. `doctype` and `name` are excluded because tabSingles rows are
		(doctype, field, value) — the doctype is the filter, not a field.
		"""
		stored = STORE.singles.get(doctype) or {}
		return FrappeDict({k: copy.deepcopy(v) for k, v in stored.items() if k not in ("doctype", "name")})

	def get_single_value(self, doctype, fieldname):
		return (STORE.singles.get(doctype) or {}).get(fieldname)

	def exists(self, doctype, filters=None):
		if doctype == "DocType":
			name = filters if isinstance(filters, str) else (filters or {}).get("name")
			return name if name in INSTALLED_DOCTYPES else None
		rows = self.get_all(doctype, filters=filters)
		return rows[0].get("name") if rows else None

	def count(self, doctype, filters=None):
		return len(self.get_all(doctype, filters=filters))

	def set_value(self, doctype, name, fieldname, value=None, **kwargs):
		if doctype in CHILD_TABLE_SOURCES:
			return self._set_child_value(doctype, name, fieldname, value)
		row = STORE.get_raw(doctype, name)
		if row is None:
			return
		STORE.snapshot(doctype, name)
		if isinstance(fieldname, dict):
			row.update(fieldname)
		else:
			row[fieldname] = value

	def _set_child_value(self, doctype, name, fieldname, value):
		"""`frappe.db.set_value` against a child row, found by its own docname.

		Real Frappe writes `tabJournal Entry Account` directly and this is how a
		submitted document's line gets a field changed at all — `party` is not
		allowed on submit, so `doc.save()` is not available. The double stores
		children inside their parents, so the row has to be located by walking
		them; the observable behaviour is the same, which is the point.

		Silently does nothing for a name that matches no row, exactly as
		`frappe.db.set_value` does for a missing document.
		"""
		for parent_doctype, fieldname_on_parent in CHILD_TABLE_SOURCES[doctype]:
			self._set_child_value_in(parent_doctype, fieldname_on_parent, doctype, name, fieldname, value)

	def _set_child_value_in(self, parent_doctype, fieldname_on_parent, doctype, name, fieldname, value):
		for parent in STORE.rows(parent_doctype):
			for row in parent.get(fieldname_on_parent) or []:
				if row.get("name") != name:
					continue
				# The child lives inside its parent's row, so the parent is what a
				# rollback has to restore.
				STORE.snapshot(parent_doctype, parent.get("name"))
				if isinstance(fieldname, dict):
					row.update(fieldname)
				else:
					row[fieldname] = value
				return

	def commit(self):
		STORE.commit()

	def rollback(self):
		STORE.rollback()

	def sql(self, query, values=None, as_dict=0, **kwargs):
		"""Deliberately extended for `tabSingles`, per this stub's own invitation.

		`migrate_incident_tool_switches` (v0.95.0) has to reach a `tabSingles`
		row whose field no longer exists in the DocType JSON — real Frappe's
		`get_single_value`/`set_value` validate a Single's fieldname against
		the current schema first, which is exactly what makes them unable to
		read or write an old, renamed-away fieldname. Raw SQL is the only path
		that still reaches that row, on the real bench and here. Every other
		shape of query still hits the `AssertionError` below.
		"""
		normalized = " ".join(str(query).split()).upper()
		if normalized.startswith("SELECT VALUE FROM `TABSINGLES` WHERE DOCTYPE=%S AND FIELD=%S"):
			doctype, field = values
			stored = STORE.singles.get(doctype) or {}
			if field not in stored:
				return []
			value = stored[field]
			return [{"value": value}] if as_dict else [(value,)]
		if normalized.startswith("INSERT INTO `TABSINGLES` (DOCTYPE, FIELD, VALUE) VALUES (%S, %S, %S)"):
			doctype, field, value = values
			STORE.singles.setdefault(doctype, {})[field] = value
			return []
		raise AssertionError(  # pragma: no cover - app must not use raw SQL
			"erpnext_mcp must not run raw SQL: every write goes through the ORM so "
			"doctype validation runs. If a read genuinely needs SQL, extend this "
			"stub deliberately."
		)


#: Child doctypes are stored inside their parents, so a query against one has to
#: flatten the parents first. The value is a TUPLE OF (parent, fieldname) PAIRS
#: rather than a single pair, because v0.16.0 ships a child table with four
#: parents: `Farm Task Evidence` is the photographs on a task completion, on a
#: housing inspection, on a detector test and on a water sample — one shape of
#: row, one place to change it, four documents that carry it. A double that
#: assumed one parent per child would have flattened only the first and reported
#: every other record's evidence as absent, which is the kind of empty result
#: that reads as "no photographs were filed".
CHILD_TABLE_SOURCES = {
	"Journal Entry Account": (("Journal Entry", "accounts"),),
	# v0.91.0. The wizard's steps, read by `parent` rather than off the
	# definition in hand — which is how `wizards._step_names` finds the rows a
	# `Wizard Field` points at, and how an overwrite finds the ones it has to
	# delete before the definition goes. `Wizard Field` is DELIBERATELY ABSENT
	# from this table: it is a grandchild, it is written as a document of its
	# own (see `_drop_grandchildren`), and it lives in `tabWizard Field` like
	# any other row rather than nested inside anything.
	"Wizard Step": (("Wizard Definition", "steps"),),
	"Parcel Conveyance Event": (("Parcel", "conveyance_events"),),
	"Bank Transaction Payments": (("Bank Transaction", "payment_entries"),),
	"Statement Anchor Line": (("Statement Anchor", "statement_lines"),),
	"Fiscal Year Company": (("Fiscal Year", "companies"),),
	"Workflow Document State": (("Workflow", "states"),),
	"Workflow Transition": (("Workflow", "transitions"),),
	"Farm Task Evidence": (
		("Farm Task Assignment", "evidence_files"),
		("Housing Inspection", "photos"),
		("Detector Test", "photos"),
		("Water Test", "sample_photos"),
	),
	# v0.17.0. Both are child tables of core doctypes, and both are queried
	# through `frappe.db.get_all` by `roles.py` exactly as they are on a site.
	"Has Role": (("User", "roles"),),
	"DocPerm": (("DocType", "permissions"),),
	# v0.19.2. `training.rows_for_parents` queries this child doctype directly,
	# filtering on `parenttype` — so both parents have to be flattened or the
	# filter would be matching against half the table and passing.
	"Compliance Regime Link": (
		("Compliance Alert", "regime"),
		("Training Type", "regimes"),
		# v0.21.0. A third parent, and the same reason: `sessions.regimes_of`
		# reads the child doctype directly with a `parenttype` filter, so a
		# template's regimes would come back empty and every regime-filtered
		# listing would silently answer "none".
		("Inspection Template", "regimes"),
		# v0.22.0. A fourth parent, and the same reason a fourth time:
		# `compliance_rules.regimes_of` reads the child doctype directly with a
		# `parenttype` filter, so a rule's regimes would come back empty — and a
		# rule with no regimes is invisible to `refresh_compliance_alerts(regime=…)`
		# and to every regime-filtered packet, which is the one failure a
		# compliance calendar must not have quietly.
		("Compliance Rule", "regimes"),
		# v0.41.0. A fifth parent. `task_templates.regimes_of` filters on
		# `parenttype` for the same reason every reader above does.
		("Farm Task Template", "compliance_regimes"),
		# A sixth parent. `training_sessions.describe` reads a session's regimes
		# through `rows_for_parents`, so without this every session would report
		# itself untagged — and `completion_blockers` would then refuse every
		# completion here while passing on a bench, which is the worst direction
		# for the double to be wrong in.
		("Training Session", "regimes"),
	),
	# The sign-in sheet, read by `parent` rather than off a document somebody
	# already loaded: `list_training_sessions` reports the attendance counts of
	# forty sessions at once, and forty parent loads to count signatures is forty
	# round trips to answer one join.
	"Training Session Attendee": (("Training Session", "attendees"),),
	# v0.19.3. `shifts.crew_of`, `events_of` and `weather_of` all query the child
	# doctype directly with a `parent` filter, because the Attendance bridge and
	# the read tools work from a docname rather than from a document somebody
	# already loaded. Without these the crew of every shift would come back empty
	# and the bridge would silently write nothing while reporting success.
	"Farm Shift Crew Member": (("Farm Shift", "crew"),),
	"Farm Shift Compliance Event": (("Farm Shift", "compliance_events"),),
	"Farm Shift Weather Reading": (("Farm Shift", "weather_timeline"),),
	"Heat Acclimatization Worker": (("Heat Exposure Event", "acclimatization_plan"),),
	# v0.21.0. All three are queried directly with a `parent` filter, because a
	# template is read from a docname rather than from a document somebody
	# already loaded — `sessions.sections_of` is called by the rule engine's
	# matcher, by the audit packet and by every read tool. Without these, every
	# template would look sectionless and the matcher would silently never match.
	"Inspection Template Section": (("Inspection Template", "sections"),),
	"Inspection Session Evidence": (("Inspection Session", "evidence_files"),),
	"Inspection Session Section Submission": (("Inspection Session", "section_submissions"),),
	# v0.41.0. Read with a `parent` filter by `task_templates.checklist_of`,
	# which the snapshot and the compliance recipe both go through.
	"Farm Task Template Checklist Item": (("Farm Task Template", "checklist"),),
	# v0.47.0. `i9._reverification_history` reads this child doctype directly
	# with a `parent` filter rather than loading the parent, and deliberately:
	# loading an I-9 Form to reach its Section 3 rows would pull the Section 1
	# columns — the encrypted SSN among them — into memory for a caller who asked
	# for a reverification history. Without this entry every I-9 would report
	# itself as never reverified, which is exactly the answer that gets a lawfully
	# reverified worker walked through a second I-9.
	"I-9 Reverification": (("I-9 Form", "reverifications"),),
	# v0.66.0. `masters._items_with_defaults_for` queries this child doctype
	# directly with a `company` filter, because the question — "which items has
	# this company set a default for" — is asked before any Item is loaded.
	# Without this entry the company filter on `list_items` would match nothing
	# and the tool would report an empty catalogue as an answer.
	"Item Default": (("Item", "item_defaults"),),
	# v0.68.0. `tools/fill_pipeline.py` reads this child doctype directly with a
	# `parenttype`/`parent` filter — once to count acknowledgments per change for
	# list_fill_threshold_changes, once to know who has already acknowledged the
	# CURRENT version for list_pending_threshold_acknowledgments. Without this
	# entry every change would report zero acknowledgments and every checker
	# would look permanently pending, however many times they acknowledged.
	"Fill Threshold Acknowledgment": (("Fill Threshold Change Log", "acknowledgments"),),
	# v0.69.0. All three are read directly, parent unloaded, by
	# `tools/stock_inventory.py`:
	#
	#   * `Item Reorder` — `_reorder_rules` asks "every rule on this site" before
	#     it knows which items have one, which is the whole shape of a reorder
	#     report. Without this entry `list_reorder_alerts` would find no rules
	#     and answer "nothing to buy" on a site with a shed full of them.
	#   * `UOM Conversion Detail` — `_conversion` looks up one item's factor to
	#     decide whether to accept a qty or refuse it, and a lookup that always
	#     came back empty would make the refusal unconditional.
	#   * `Stock Entry Detail` — `list_stock_entries` filters on warehouse and
	#     item by finding the LINES that match and then their parents, because
	#     neither is a column on the Stock Entry header.
	"Item Reorder": (("Item", "reorder_levels"),),
	"UOM Conversion Detail": (("Item", "uoms"),),
	"Stock Entry Detail": (("Stock Entry", "items"),),
	# v0.80.0. `tools/controls.approval_findings` reads the rungs of every
	# enabled threshold in one query rather than loading each parent document,
	# because it runs on the write path of every journal entry and a document
	# load per threshold is a cost the ledger should not pay. Without this entry
	# every rung would come back empty, `required_authority` would see a chain
	# with no ceilings, and the control would report EVERY transaction as being
	# above an authority table that in fact covered it — a false refusal under
	# enforcement, which is the worst way for this control to be wrong.
	"Approval Threshold Level": (("Approval Threshold", "levels"),),
	"Closing Checklist Item": (("Closing Checklist", "items"),),
	# v0.80.0 Phases 2 and 3. `Payment Entry Reference` is ERPNext's own and is
	# flattened because `revenue.trace_contract_to_cash` walks invoice → payment
	# through it — without this the trace would report "no payment references any
	# of these invoices", which is the one break in that chain a reader is most
	# likely to act on, and it would be wrong.
	"Payment Entry Reference": (("Payment Entry", "references"),),
	"Revenue Performance Obligation": (("Revenue Contract", "obligations"),),
	"Revenue Recognition Schedule": (("Revenue Contract", "schedule"),),
	"Biological Asset Valuation": (("Biological Asset", "valuations"),),
	# v0.81.0. `disclosure.list_reporting_templates` counts a template's sections
	# with `frappe.db.count` rather than loading each document, because the list
	# is a register view and a document load per template is a cost paid for one
	# integer. Without this entry every count would come back zero and a list of
	# real templates would read as a list of empty ones.
	"Reporting Template Section": (("Reporting Template", "sections"),),
	"Disclosure Checklist Item": (("Disclosure Checklist", "items"),),
	# v0.82.0. All four are read directly with a `parent`/`parenttype` filter by
	# `tools/agronomy.py`, parent unloaded, and for one reason in each case:
	#
	#   * `Crop Variety` and `Market Grade Standard` — `list_crops` and
	#     `list_markets` count and name the child rows of EVERY row in the
	#     register in one query. Loading each parent to count its children is a
	#     document load per master paid for one integer, on a read whose whole
	#     job is to be the cheap overview.
	#   * `Crop Water Requirement` — `get_crop` reads the stages without the
	#     parent for symmetry with the varieties beside it.
	#   * `Agricultural UOM Context Entry` — `list_ag_uom_contexts` gathers the
	#     units of every context at once, for the same reason.
	#
	# Without these entries every count would come back zero and a register full
	# of varieties and grades would read as a register of empty masters — which
	# is exactly the "active market with no grade standards" gap `list_markets`
	# reports, so the double would have manufactured the finding it exists to
	# surface.
	"Crop Variety": (("Crop", "varieties"),),
	"Crop Water Requirement": (("Crop", "water_requirements"),),
	# v0.114.0. Read the same way and for the same reason as the two above:
	# `get_crop` and `get_variety_care_recipe` pull the overlay rows with a
	# `parent`/`parentfield` filter rather than loading the Crop. Without these
	# entries every override would come back empty, and — this is the direction
	# that matters — a variety with a real Kc override would silently resolve to
	# the crop default while reporting itself as overridden.
	"Crop Variety Water Requirement": (("Crop", "variety_water_requirements"),),
	"Crop Variety Protocol": (("Crop", "variety_protocols"),),
	"Market Grade Standard": (("Market", "grade_standards"),),
	"Agricultural UOM Context Entry": (("Agricultural UOM Context", "uoms"),),
	# v0.88.0. `spray.list_spray_applications` filters by BLOCK, and the block
	# lives on the child table rather than on the header — one pass covers
	# several blocks and there is no single block column to filter on. So it
	# finds the matching child rows and keeps their parents, which is the same
	# shape `list_stock_entries` uses for warehouse and item and for the same
	# reason. Without this entry the block filter would match nothing and a
	# register full of sprays on that block would answer "none", which is the
	# worst possible direction for a pesticide record to be wrong in.
	"Spray Application Block": (("Spray Application", "blocks"),),
	# v0.111.0. `lots._child_lots_of` asks the OTHER direction of the
	# transformation graph: given a lot, which lots name it as a source. The
	# parents are what is being looked for, so this is the one edge that cannot be
	# read through a parent document — without this entry `trace_lot_forward` would
	# answer "this lot went nowhere" for a lot that went into a pallet, which is
	# the worst possible direction for a recall answer to be wrong in.
	"Traceability Lot Source": (("Traceability Lot Code", "source_lots"),),
}


def _child_rows(child_doctype: str) -> list[dict]:
	out = []
	for parent_doctype, fieldname in CHILD_TABLE_SOURCES[child_doctype]:
		for parent in STORE.rows(parent_doctype):
			for row in parent.get(fieldname) or []:
				merged = dict(row)
				merged.setdefault("parent", parent.get("name"))
				merged.setdefault("parenttype", parent_doctype)
				merged.setdefault("parentfield", fieldname)
				out.append(merged)
	return out


def _aggregate(rows: list[dict], expressions: list[str]) -> FrappeDict:
	out = FrappeDict()
	for expression in expressions:
		function, _, rest = expression.partition("(")
		column = rest.split(")")[0].strip()
		alias = expression.split(" as ")[-1].strip() if " as " in expression else expression
		function = function.strip().lower()
		if function == "sum":
			out[alias] = sum(float(row.get(column) or 0) for row in rows) or 0
		elif function == "count":
			out[alias] = len(rows)
		else:  # pragma: no cover
			raise NotImplementedError(f"stub aggregate {function!r}")
	return out


def _grouped_aggregate(rows, fields, aggregates, group_by):
	"""`SELECT <key>, sum(...) ... GROUP BY <key>`, as the packets use it."""
	key = str(group_by).split(",")[0].strip().strip("`").split(".")[-1]
	plain = [f for f in (fields or []) if "(" not in str(f)]
	buckets: dict = {}
	for row in rows:
		buckets.setdefault(row.get(key), []).append(row)
	out = []
	for value, group in buckets.items():
		entry = _aggregate(group, aggregates)
		for column in plain:
			entry[column] = group[0].get(column)
		entry[key] = value
		out.append(entry)
	return out


def _sort_key(value):
	"""A total ordering key, so a column of mixed types cannot raise.

	`0 or ""` was the original spelling and it has a hole in it that only shows on
	a column that is legitimately zero: `chunk_index` counts from 0, so ordering
	staged upload pieces turned index 0 into the empty string and then compared a
	string against the integers beside it — `TypeError: '<' not supported between
	instances of 'int' and 'str'`. MariaDB has no such problem, so this was the
	double refusing a query a real site answers, which is the mirror image of the
	usual failure and just as capable of blocking working code.

	Empty and NULL sort first, as MariaDB puts NULLs first ascending; then
	numbers; then text. The three-part tuple is what makes the comparison total
	whatever the column holds.
	"""
	if value is None or value == "":
		return (0, 0.0, "")
	key = _key(value)
	if isinstance(key, (int, float)) and not isinstance(key, bool):
		return (1, float(key), "")
	return (2, 0.0, str(key))


def _sorted(rows: list[dict], order_by: str) -> list[dict]:
	out = list(rows)
	# Apply each clause in reverse so the leftmost wins, as SQL does.
	for clause in reversed([c.strip() for c in order_by.split(",") if c.strip()]):
		parts = clause.split()
		column = parts[0].split(".")[-1].strip("`")
		reverse = len(parts) > 1 and parts[1].lower() == "desc"
		out.sort(key=lambda row: _sort_key(row.get(column)), reverse=reverse)
	return out


#: Which doctypes this fake site "has installed". Tests flip entries to exercise
#: the graceful-degrade paths (a site without Bank Statement, say).
INSTALLED_DOCTYPES = set(ERPNEXT_SCHEMA) | set(APP_DOCTYPES)


# ── utils ───────────────────────────────────────────────────────────────────
_now_counter = 0


def _now() -> str:
	global _now_counter
	_now_counter += 1
	base = datetime.datetime(2026, 7, 24, 9, 0, 0)
	return (base + datetime.timedelta(seconds=_now_counter)).isoformat(sep=" ")


def _getdate(value=None):
	if value is None:
		return datetime.date(2026, 7, 24)
	if isinstance(value, datetime.datetime):
		return value.date()
	if isinstance(value, datetime.date):
		return value
	text = str(value).strip().split(" ")[0].split("T")[0]
	parts = text.split("-")
	if len(parts) != 3:
		raise ValueError(f"cannot parse date {value!r}")
	return datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))


def _build_utils() -> types.ModuleType:
	module = types.ModuleType("frappe.utils")
	module.now = _now
	module.nowdate = lambda: _getdate().isoformat()
	module.today = lambda: _getdate().isoformat()
	module.getdate = _getdate
	module.flt = lambda value, precision=None: round(float(value or 0), precision or 2)
	module.cint = lambda value: int(float(value or 0))
	module.cstr = lambda value: "" if value is None else str(value)

	def escape_html(text):
		"""Frappe's own HTML escaper. Real, because a governance document's notes
		field is a Text Editor and v0.17.0 writes a user's email address into it."""
		if text is None:
			return ""
		return (
			str(text)
			.replace("&", "&amp;")
			.replace("<", "&lt;")
			.replace(">", "&gt;")
			.replace('"', "&quot;")
			.replace("'", "&#39;")
		)

	module.escape_html = escape_html

	def get_url(uri=None, full_address=False):
		"""frappe.utils.get_url — the site's own address, as the server sees it."""
		base = "https://test.localhost"
		return f"{base}/{str(uri).lstrip('/')}" if uri else base

	module.get_url = get_url

	def add_days(date, days):
		return _getdate(date) + datetime.timedelta(days=days)

	def date_diff(later, earlier):
		return (_getdate(later) - _getdate(earlier)).days

	module.add_days = add_days
	module.date_diff = date_diff

	def time_diff_in_seconds(later, earlier):
		"""frappe.utils.time_diff_in_seconds — seconds between two datetimes.

		v0.16.0's dispatch tools use it to work out how long a task actually took
		from its clock-in and clock-out. Faithful in the one way that matters:
		it takes DATETIMES and returns a float, so a task that ran twenty-five
		minutes reports twenty-five and not zero, which is what a date-only
		double would have said.
		"""

		def parse(value):
			if isinstance(value, datetime.datetime):
				return value
			if isinstance(value, datetime.date):
				return datetime.datetime(value.year, value.month, value.day)
			return datetime.datetime.fromisoformat(str(value).strip().replace("T", " "))

		return (parse(later) - parse(earlier)).total_seconds()

	module.time_diff_in_seconds = time_diff_in_seconds

	def add_to_date(
		date=None,
		years=0,
		months=0,
		weeks=0,
		days=0,
		hours=0,
		minutes=0,
		seconds=0,
		as_string=False,
		as_datetime=False,
	):
		"""frappe.utils.add_to_date, in the shapes this app uses it.

		`as_string=True, as_datetime=True` returns Frappe's own DATETIME_FORMAT,
		which matters: the staged-upload sweeper compares the result against the
		`modified` COLUMN, and a value formatted any other way — an isoformat with
		a `T`, say — compares as a string and quietly matches nothing. Months and
		years are refused rather than approximated, because this double has no
		dateutil and a 30-day month would be a lie a test could come to rely on.
		"""
		if months or years:  # pragma: no cover - nothing in the app asks for these
			raise NotImplementedError("stub add_to_date does not do months or years")
		if isinstance(date, datetime.datetime):
			base = date
		elif isinstance(date, datetime.date):
			base = datetime.datetime(date.year, date.month, date.day)
		else:
			base = datetime.datetime.fromisoformat(str(date or _now()).strip().replace("T", " "))
		moved = base + datetime.timedelta(
			weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds
		)
		if as_string:
			return moved.strftime("%Y-%m-%d %H:%M:%S.%f" if as_datetime else "%Y-%m-%d")
		return moved

	module.add_to_date = add_to_date
	return module


# ── the module itself ───────────────────────────────────────────────────────
def _controller(doctype: str):
	"""Resolve a doctype to this app's controller class, as Frappe does.

	Including the child tables. Frappe imports `<folder>/<folder>.py` for every
	DocType it loads and does not make an exception for a table, which is what
	v0.7.0 learned the hard way — so neither does this. A folder with a JSON and
	no module raises ImportError here for the same reason `bench migrate` raises
	ModuleNotFoundError there.
	"""
	folder = APP_DOCTYPES.get(doctype)
	if not folder:
		return STUB_CONTROLLERS.get(doctype, Document)
	module = __import__(f"erpnext_mcp.erpnext_mcp.doctype.{folder}.{folder}", fromlist=["x"])
	class_name = doctype.replace(" ", "").replace("-", "")
	return getattr(module, class_name, Document)


#: Reverse link index: target doctype → tuple of `(linking doctype, fieldname)`.
#: Built once on first use because it is a scan of every field of every doctype in
#: `META` and nothing about it changes between tests.
_LINK_INDEX: dict[str, tuple] | None = None


def _link_index() -> dict:
	"""Every Link field on the site, indexed by what it points AT.

	`Document._validate_links` asks the forward question — "does the thing this
	field names exist" — and walks one document's own fields. This is the reverse,
	which is the question a delete asks, and it has to be answered across every
	doctype at once.

	FIELDS MARKED `ignore_links` ARE LEFT OUT, as Frappe leaves them out of
	`check_if_doc_is_linked`. That flag is precisely how a schema says "this link
	is a convenience and must not keep the target alive", and honouring it here is
	what keeps the double from being stricter than the framework.
	"""
	global _LINK_INDEX
	if _LINK_INDEX is not None:
		return _LINK_INDEX
	index: dict[str, list] = {}
	for doctype, meta in META.items():
		for field in meta.fields:
			if field.get("fieldtype") != "Link":
				continue
			if field.get("ignore_links"):
				continue
			target = str(field.get("options") or "")
			fieldname = str(field.get("fieldname") or "")
			if not target or not fieldname:
				continue
			index.setdefault(target, []).append((doctype, fieldname))
	_LINK_INDEX = {key: tuple(value) for key, value in index.items()}
	return _LINK_INDEX


def _refuse_if_linked(doctype: str, name: str) -> None:
	"""Raise `LinkExistsError` if anything still links to this document.

	`frappe.model.delete_doc.check_if_doc_is_linked`, to the fidelity this double
	can reach. The message is shaped like Frappe's — the linking doctype and the
	linking docname — because the app prints it and a test that asserts on it
	should be asserting on something recognisable.

	TWO DELIBERATE NARROWINGS, both stated rather than hidden:

	  * A DOCUMENT LINKING TO ITSELF DOES NOT BLOCK ITS OWN DELETE. Frappe does not
	    refuse that either, and `Governance Document.supersedes` on a chain being
	    torn down is the case that would otherwise deadlock.
	  * CHILD-TABLE LINKS ARE NOT WALKED. This double stores child rows inside
	    their parent rather than in tables of their own, so there is nothing to
	    scan; Frappe does check them. The gap is real and it makes this double
	    PERMISSIVE rather than strict — a delete refused on a real bench by a
	    child-row link would still pass here. Left rather than faked because a
	    half-built scan over `CHILD_TABLES` would look like coverage and not be it.
	"""
	target = str(name)
	for linking_doctype, fieldname in _link_index().get(doctype, ()):
		for row in STORE.rows(linking_doctype):
			if str(row.get(fieldname) or "") != target:
				continue
			if linking_doctype == doctype and str(row.get("name") or "") == target:
				continue
			raise LinkExistsError(
				f"Cannot delete or cancel because {doctype} {name} is linked with "
				f"{linking_doctype} {row.get('name')}"
			)


def _build_frappe() -> types.ModuleType:
	module = types.ModuleType("frappe")

	module._dict = FrappeDict
	module.ValidationError = ValidationError
	module.DoesNotExistError = DoesNotExistError
	module.PermissionError = PermissionError_
	module.MandatoryError = MandatoryError
	module.LinkValidationError = LinkValidationError
	module.LinkExistsError = LinkExistsError
	module.db = FakeDB()
	module.local = FrappeDict(
		site="test.localhost",
		request=None,
		session=FrappeDict(user="Guest", data=FrappeDict()),
	)
	module.flags = FrappeDict()
	# common_site_config.json merged with site_config.json. Real Frappe exposes
	# the same object as frappe.conf and frappe.local.conf.
	module.conf = FrappeDict()
	module.local.conf = module.conf
	# What a whitelisted method fills in to serve a file. Frappe's
	# `frappe.utils.response.as_binary` reads type/filename/filecontent off it.
	module.response = FrappeDict()

	def _translate(text, *args, **kwargs):
		return text

	module._ = _translate

	def get_meta(doctype):
		if doctype not in META:
			raise ValidationError(f"stub has no meta for {doctype!r}")
		return META[doctype]

	def get_doc(*args, **kwargs):
		if args and isinstance(args[0], dict):
			payload = dict(args[0])
			doctype = payload.pop("doctype")
			return _controller(doctype)({**payload, "doctype": doctype})
		doctype = args[0]
		if META.get(doctype) and META[doctype].issingle:
			data = copy.deepcopy(STORE.singles.get(doctype) or {})
			data["doctype"] = doctype
			data.setdefault("name", doctype)
			single = _controller(doctype)(data)
			# A Single's child tables are rehydrated the same way an ordinary
			# document's are. Frappe loads them in one loop for both cases —
			# `Document.load_from_db` runs `_get_table_fields` after the
			# single/not-single branch — and v0.48.0 put a roster on a Single
			# whose rows are read back and mutated in place.
			for fieldname in REHYDRATED_CHILD_FIELDS:
				if isinstance(single.get(fieldname), list):
					single[fieldname] = [Document(item) for item in single[fieldname]]
			return single
		name = args[1] if len(args) > 1 else kwargs.get("name")
		row = STORE.get_raw(doctype, name)
		if row is None:
			raise DoesNotExistError(f"{doctype} {name} not found")
		data = copy.deepcopy(row)
		data["doctype"] = doctype
		doc = _controller(doctype)(data)
		for fieldname in REHYDRATED_CHILD_FIELDS:
			if isinstance(doc.get(fieldname), list):
				doc[fieldname] = [Document(item) for item in doc[fieldname]]
		return doc

	def new_doc(doctype):
		doc = _controller(doctype)({"doctype": doctype, "docstatus": 0})
		for field in META[doctype].fields if doctype in META else []:
			if field.get("default") not in (None, ""):
				doc[field["fieldname"]] = field["default"]
		return doc

	def get_cached_doc(*args, **kwargs):
		return get_doc(*args, **kwargs)

	def get_single(doctype):
		return get_doc(doctype)

	def whitelist(*dargs, **dkwargs):
		def decorator(function):
			function.__wrapped_whitelisted__ = True
			return function

		if dargs and callable(dargs[0]):
			return decorator(dargs[0])
		return decorator

	def throw(message, exc=None, title=None):
		raise (exc or ValidationError)(str(message))

	def only_for(roles, message=False):
		roles = [roles] if isinstance(roles, str) else list(roles)
		if not set(roles) & set(get_roles(module.session.user)):
			raise PermissionError_(f"requires role: {', '.join(roles)}")

	def get_roles(user=None):
		return ROLES.get(user or module.session.user, [])

	def get_request_header(name, default=None):
		request = module.local.request
		if request is None:
			return default
		return request.headers.get(name, default)

	def set_user(user):
		module.local.session.user = user

	def log_error(title=None, message=None, reference_doctype=None, reference_name=None):
		STORE.errors.append({"title": title, "message": message})

	def sendmail(recipients=None, subject=None, message=None, **kwargs):
		"""Record the message rather than send it. v0.17.1.

		NOT a no-op, and not a raiser. A double that silently succeeded would let
		"the report went nowhere" pass; one that always raised would only ever
		exercise the Error Log fallback. Recording lets the tests assert both
		paths — `drift.notify` is required to fall back when mail is unavailable,
		because a site with no outgoing email account is an ordinary state and
		losing the one message that says the ledger disagrees with itself would
		reproduce the original bug's defining property exactly.
		"""
		if getattr(STORE, "mail_fails", False):
			raise ValidationError("no outgoing email account")
		STORE.emails.append({"recipients": list(recipients or []), "subject": subject, "message": message})

	def get_traceback(with_context=False):
		return traceback.format_exc()

	def generate_hash(txt=None, length=56):
		return secrets.token_hex(max(1, length // 2))[:length]

	def msgprint(message, title=None, indicator=None, **kwargs):
		STORE.comments.append({"type": "msgprint", "text": str(message)})

	def get_attr(path):
		parts = path.split(".")
		obj = __import__(".".join(parts[:-1]), fromlist=["x"])
		return getattr(obj, parts[-1])

	def get_installed_apps():
		return list(STORE.installed_apps)

	def has_permission(doctype=None, ptype="read", doc=None, user=None, throw=False, **kwargs):
		"""Allow by default; tests deny specific (doctype, ptype) or (doctype, name).

		Default-allow is the right polarity for a double: a test that forgets to
		grant something should not silently pass because everything was denied,
		and every permission test here is about a *refusal*, which it has to ask
		for explicitly.
		"""
		name = doc if isinstance(doc, str) else (doc.get("name") if doc else None)
		denied = (
			(doctype, ptype) in STORE.denied_permissions
			or (doctype, name) in STORE.denied_permissions
			or (doctype, name, ptype) in STORE.denied_permissions
		)
		if denied and throw:
			raise PermissionError_(f"no {ptype} permission for {doctype} {name}")
		return not denied

	def get_list(doctype, **kwargs):
		"""`get_all` with the permission check `get_all` skips."""
		if not has_permission(doctype, "read"):
			raise PermissionError_(f"no read permission for {doctype}")
		return module.db.get_all(doctype, **kwargs)

	def scrub(text):
		return str(text or "").replace(" ", "_").replace("-", "_").lower()

	def clear_cache(user=None, doctype=None, *args, **kwargs):
		"""A no-op: this double has no meta cache to invalidate.

		Present so the app can call it — adding a Custom Field without clearing
		the cache is a real bug on a real site, and a double that raised
		AttributeError here would push the app into not doing it.
		"""

	def delete_doc(doctype, name, force=False, ignore_permissions=False, ignore_on_trash=False, **kwargs):
		"""Delete a row, running `on_trash` and refusing a linked document as Frappe does.

		THE ORDER IS THE WHOLE POINT AND IT IS FRAPPE'S. `delete_doc` runs
		`on_trash` first and `check_if_doc_is_linked` second, which is what lets a
		controller release the links it is entitled to release and still be
		refused for the ones it is not. `Governance Document.on_trash` depends on
		exactly that window; a double that checked links first would refuse a
		delete the real framework allows, and one that never checked at all —
		which is what this was until v0.83.0 — allows every delete the real
		framework refuses.

		`force=True` skips the check, as in Frappe. The app's uninstall paths pass
		it deliberately.
		"""
		if STORE.get_raw(doctype, name) is not None and not ignore_on_trash:
			try:
				doc = get_doc(doctype, name)
			except DoesNotExistError:  # pragma: no cover - checked immediately above
				doc = None
			if doc is not None:
				doc._run("on_trash")
		if not force:
			_refuse_if_linked(doctype, name)
		STORE.snapshot(doctype, name)
		STORE.tables.get(doctype, {}).pop(name, None)

	def rename_doc(doctype, old, new, force=False, merge=False, **kwargs):
		"""Move a docname and repoint the links this app can observe.

		Real Frappe rewrites every Link field on the site that pointed at the old
		name. Reproducing that generically would mean a link graph this double
		does not have, so `RENAME_LINK_FIELDS` names the ones the account tools
		actually depend on — the child's `parent_account` above all, since an
		import that renamed a group and orphaned its children is exactly the
		failure a test here should catch.
		"""
		table = STORE.tables.setdefault(doctype, {})
		if old not in table:
			raise DoesNotExistError(f"{doctype} {old} not found")
		if new == old:
			return old
		if new in table:
			raise ValidationError(f"{doctype} {new} already exists")
		row = table.pop(old)
		row["name"] = new
		# v0.68.1. THE NAME COLUMN MOVES WITH THE DOCNAME, which is what real
		# Frappe's `rename_doc` does in `update_autoname_field` and what this
		# double did not. On a `field:`-named doctype the two are the same string
		# by construction, so a rename that moved only the key would leave
		# `Designation.designation_name` reading the OLD title — and every read
		# in `tools/org.py` goes through that column, so the suite would have
		# reported a rename that half happened as one that worked. Doctypes named
		# any other way have no such column and are untouched.
		meta = META.get(doctype)
		autoname = str(getattr(meta, "autoname", "") or "") if meta else ""
		if autoname.startswith("field:"):
			row[autoname.split(":", 1)[1]] = new
		table[new] = row
		STORE.pending = [(dt, new if (dt == doctype and dn == old) else dn) for dt, dn in STORE.pending]
		for link_doctype, fieldname in RENAME_LINK_FIELDS.get(doctype, ()):
			for other in STORE.rows(link_doctype):
				if other.get(fieldname) == old:
					other[fieldname] = new
		return new

	module.clear_cache = clear_cache
	module.get_installed_apps = get_installed_apps
	module.has_permission = has_permission
	module.get_list = get_list
	module.scrub = scrub
	module.delete_doc = delete_doc
	module.rename_doc = rename_doc
	module.get_meta = get_meta
	module.get_doc = get_doc
	module.new_doc = new_doc
	module.get_cached_doc = get_cached_doc
	module.get_single = get_single
	module.whitelist = whitelist
	module.throw = throw
	module.only_for = only_for
	module.get_roles = get_roles
	module.get_request_header = get_request_header
	module.set_user = set_user
	module.log_error = log_error
	module.sendmail = sendmail
	module.get_traceback = get_traceback
	module.generate_hash = generate_hash
	module.msgprint = msgprint
	module.get_attr = get_attr
	module.get_site_path = get_site_path
	module.utils = _build_utils()

	# `frappe.session` and `frappe.request` are properties on the real module;
	# a module-level __getattr__ is the closest a stub gets.
	def __getattr__(name):
		if name == "session":
			return module.local.session
		if name == "request":
			return module.local.request
		if name == "site":
			return module.local.site
		raise AttributeError(
			f"erpnext_mcp used frappe.{name}, which this test double does not "
			"implement. Add it to tests_standalone/harness.py deliberately, or "
			"reconsider whether the app should depend on it."
		)

	module.__getattr__ = __getattr__
	return module


ROLES = {
	"Administrator": [
		"System Manager",
		"Accounts Manager",
		"Accounts User",
		"Purchase Manager",
		"Purchase User",
	]
}


def set_roles(user: str, roles) -> None:
	"""Give a fake user a role set, for the permission and workflow tests."""
	ROLES[user] = list(roles)


# ── frappe.model.workflow ───────────────────────────────────────────────────
def _active_workflow_for(doc):
	name = frappe.db.get_value("Workflow", {"document_type": doc.doctype, "is_active": 1}, "name")
	return frappe.get_doc("Workflow", name) if name else None


def _stub_get_transitions(doc, workflow=None, raise_exception=False):
	"""Frappe's transition resolution, faithfully enough to test against.

	Role and condition only. Frappe's get_transitions does NOT check
	self-approval — that rule lives in apply_workflow and throws at execution
	time — and a double that filtered it here would hide the fact that
	`list_available_actions` has to apply the rule itself. Verified against a
	real Workflow in erpnext_mcp/tests/test_workflow_scenarios.py.
	"""
	workflow = workflow or _active_workflow_for(doc)
	if workflow is None:
		return []
	state_field = workflow.get("workflow_state_field") or "workflow_state"
	current = doc.get(state_field)
	roles = set(frappe.get_roles(frappe.session.user) or [])
	out = []
	for row in workflow.get("transitions") or []:
		if row.get("state") != current:
			continue
		if row.get("allowed") and row["allowed"] not in roles:
			continue
		condition = row.get("condition")
		# A real Frappe uses safe_eval here; the double uses eval because the
		# only conditions it ever sees are the ones a test wrote.
		if condition and not eval(condition, {"doc": doc, "frappe": frappe}):
			continue
		out.append(dict(row))
	return out


def _stub_apply_workflow(doc, action):
	"""As Frappe does it — including enforcing self-approval *here*, late."""
	workflow = _active_workflow_for(doc)
	if workflow is None:
		raise ValidationError(f"no active workflow for {doc.doctype}")
	state_field = workflow.get("workflow_state_field") or "workflow_state"
	for row in _stub_get_transitions(doc, workflow):
		if row.get("action") != action:
			continue
		if (
			frappe.session.user != "Administrator"
			and not row.get("allow_self_approval")
			and doc.get("owner") == frappe.session.user
		):
			raise ValidationError("Self approval is not allowed")
		next_state = row.get("next_state")
		doc.set(state_field, next_state)
		target = next((s for s in workflow.get("states") or [] if s.get("state") == next_state), {})
		doc.docstatus = int(target.get("doc_status") or 0)
		doc.save()
		return doc
	raise ValidationError(f"transition {action!r} is not allowed")


# ── frappe.desk.query_report / reportview ───────────────────────────────────
def _stub_query_report_run(report_name, filters=None, user=None, **kwargs):
	ref_doctype = frappe.db.get_value("Report", report_name, "ref_doctype")
	if not frappe.has_permission(ref_doctype, "report"):
		raise PermissionError_(f"no report permission for {ref_doctype}")
	runner = STORE.report_runners.get(report_name)
	if runner is None:
		raise ValidationError(f"stub has no runner registered for report {report_name!r}")
	return runner(filters or {}, user)


def _stub_reportview_get(doctype, *args, **kwargs):
	params = getattr(frappe.local, "form_dict", None) or {}
	if not frappe.has_permission(doctype, "read"):
		raise PermissionError_(f"no read permission for {doctype}")
	fields = json.loads(params.get("fields") or "[]")
	conditions = json.loads(params.get("filters") or "[]")
	rows = frappe.db.get_all(
		doctype,
		filters={condition[1]: (condition[2], condition[3]) for condition in conditions},
		fields=fields,
		limit=params.get("page_length"),
	)
	return {"keys": fields, "values": [[row.get(field) for field in fields] for row in rows]}


# ── erpnext.accounts.doctype.account.account ────────────────────────────────
def _stub_update_account_number(name, account_name, account_number=None, from_descendant=False):
	"""ERPNext's own rename helper, reproduced closely enough to test against.

	The shape that matters to the app is the awkward one: it writes the two
	*fields* with `db.set_value` and then, only if the resulting autoname differs,
	renames the *document*. Two writes, in that order — which is precisely why
	`tools.accounts` delegates here instead of calling `rename_doc` and leaving a
	document whose name and fields disagree forever.

	Also faithful in its return value: the new docname when the name moved, and
	`None` when it did not. A double that always returned a name would hide the
	`returned or name` the caller needs.
	"""
	company = frappe.db.get_value("Account", name, "company")
	if not company:
		return None
	frappe.db.set_value("Account", name, "account_name", str(account_name).strip())
	frappe.db.set_value("Account", name, "account_number", account_number)
	abbr = frappe.db.get_value("Company", company, "abbr") or ""
	new_name = account_autoname(account_number, account_name, abbr)
	if name != new_name:
		frappe.rename_doc("Account", name, new_name, force=1)
		return new_name
	return None


def _install_erpnext_account_api() -> None:
	path = "erpnext.accounts.doctype.account.account"
	leaf = types.ModuleType(path)
	leaf.update_account_number = _stub_update_account_number
	leaf.get_account_autoname = account_autoname
	parts = path.split(".")
	for index in range(1, len(parts)):
		branch = ".".join(parts[:index])
		sys.modules.setdefault(branch, types.ModuleType(branch))
	sys.modules[path] = leaf


def install() -> types.ModuleType:
	"""Put the stub into sys.modules. Idempotent."""
	if "frappe" in sys.modules and getattr(sys.modules["frappe"], "__is_stub__", False):
		return sys.modules["frappe"]
	module = _build_frappe()
	module.__is_stub__ = True
	sys.modules["frappe"] = module

	model = types.ModuleType("frappe.model")
	document = types.ModuleType("frappe.model.document")
	document.Document = Document
	workflow = types.ModuleType("frappe.model.workflow")
	workflow.get_transitions = _stub_get_transitions
	workflow.apply_workflow = _stub_apply_workflow
	model.document = document
	model.workflow = workflow
	sys.modules["frappe.model"] = model
	sys.modules["frappe.model.document"] = document
	sys.modules["frappe.model.workflow"] = workflow
	sys.modules["frappe.utils"] = module.utils
	module.model = model

	desk = types.ModuleType("frappe.desk")
	query_report = types.ModuleType("frappe.desk.query_report")
	query_report.run = _stub_query_report_run
	reportview = types.ModuleType("frappe.desk.reportview")
	reportview.get = _stub_reportview_get
	desk.query_report = query_report
	desk.reportview = reportview
	sys.modules["frappe.desk"] = desk
	sys.modules["frappe.desk.query_report"] = query_report
	sys.modules["frappe.desk.reportview"] = reportview
	module.desk = desk
	_install_erpnext_account_api()
	return module


frappe = install()


# ── request plumbing ────────────────────────────────────────────────────────
class FakeRequest:
	def __init__(
		self,
		body="",
		headers=None,
		remote_addr="127.0.0.1",
		method="POST",
		host="test.localhost",
		scheme="https",
		path="/api/method/erpnext_mcp.mcp.handle",
	):
		self.headers = {k: v for k, v in (headers or {}).items()}
		self._body = body if isinstance(body, str) else json.dumps(body)
		self.remote_addr = remote_addr
		self.method = method
		self.host = host
		self.scheme = scheme
		# v0.17.2. `api/fallback_auth.authenticate` is an `auth_hooks` entry, so
		# it runs on every request to the site and decides whether this one is
		# any of its business by reading the path. A double with no path would
		# make that check untestable — and the check is the only thing keeping
		# the hook off every Desk page of every other installed app.
		self.path = path

	def get_data(self, as_text=False):
		return self._body if as_text else self._body.encode()


# ── the base test case ──────────────────────────────────────────────────────
def authenticated_user(headers: dict) -> str:
	"""Frappe's own API-key auth, reproduced. v0.17.0.

	WHY THE DOUBLE HAS TO DO THIS AT ALL. Frappe validates
	`Authorization: token <api_key>:<api_secret>` in its request layer, BEFORE any
	whitelisted method runs and whether or not the method allows guests. That is
	the entire mechanism v0.17.0's per-user scoping rests on: the mobile worker is
	`frappe.session.user` for the one line between "Frappe finished
	authenticating" and `frappe.set_user(effective_user())`, and
	`security.capture_calling_user` saves it there.

	A double that left `session.user` as Administrator would make every scoping
	test pass for the wrong reason — the tools would fall through to their "no
	per-user identity" branch and nobody would notice the header was never read.

	Returns "" when NO api-key header was presented, which means "leave the
	session alone" — Frappe's validator does nothing in that case and the session
	comes from a cookie. Returns "Guest" for a malformed or WRONG credential,
	exactly as Frappe does; a wrong secret is Guest and not an error, and that is
	the check that makes "revoke, then the request stops being that person" a real
	assertion rather than a hopeful one.
	"""
	header = str(headers.get("Authorization") or "")
	if header[:6].lower() != "token ":
		return ""
	api_key, _, secret = header[6:].strip().partition(":")
	if not api_key or not secret:
		return "Guest"
	for row in STORE.rows("User"):
		if str(row.get("api_key") or "") != api_key:
			continue
		stored = STORE.passwords.get(("User", row["name"], "api_secret"))
		if stored and hmac.compare_digest(str(stored), secret) and int(row.get("enabled") or 0):
			return row["name"]
		return "Guest"
	return "Guest"


def seed_compliance_regimes() -> None:
	"""Put the ten `Compliance Regime` rows on the fake site, as a migrate does.

	v0.19.2. NOT A CONVENIENCE — IT IS WHAT A REAL SITE HAS. `install.after_migrate`
	seeds this table on every migrate, so a bench that can insert a Compliance
	Alert has already got it. Modelling that here is what stops the double from
	being EASIER than a site: `Compliance Alert.regime` and `Training Type.regimes`
	are Table MultiSelects over real Links, the double validates Links faithfully
	(see `_validate_links`), and a suite that skipped the seed would have every
	regime-tagged insert fail with `Could not find Regime` — a failure that says
	nothing about the code and everything about the fixture.

	The `Training Type` seeds are deliberately NOT here. Those are ten curricula an
	operation is offered, not something the app needs to function: `ensure_type`
	creates one from free text on demand, which is the path most tests exercise and
	the one worth having under test. `TheTrainingTypeSeeder` covers the seeder
	itself.
	"""
	from erpnext_mcp import training

	try:
		training.seed_regimes()
	except Exception:  # pragma: no cover - a test that deliberately dropped the DocType
		pass


class MCPTestCase(unittest.TestCase):
	"""Resets the fake site, and gives every test a configured-but-off server."""

	TOKEN = "t" * 48

	def setUp(self):
		# Meta first: a test that created a DocType or a Custom Field changed the
		# schema, and `STORE.reset` builds its tabDocType rows from it.
		reset_meta()
		STORE.reset()
		reset_site_files()
		frappe.conf.clear()
		INSTALLED_DOCTYPES.clear()
		INSTALLED_DOCTYPES.update(set(ERPNEXT_SCHEMA) | set(APP_DOCTYPES))
		frappe.local.request = None
		frappe.local.session = FrappeDict(user="Administrator", data=FrappeDict())
		seed_compliance_regimes()
		self.configure()

	def configure(self, **overrides):
		"""Write the settings single, defaults from the shipped JSON.

		Starts from what the app itself would seed on install, so a test that
		does not override anything is testing the real out-of-the-box posture.

		Note the values go in as the *strings* the DocType JSON declares, not as
		integers. That is faithful: `tabSingles.value` is a text column, so a
		Check field on a Single reads back as `"0"` — which is truthy in Python
		and is exactly how a switch-is-off bug gets shipped. `settings._as_bool`
		is what stops it, and this fixture is what would catch its removal.
		"""
		values = {}
		for field in META["ERPNext MCP Settings"].fields:
			if field.get("default") not in (None, ""):
				values[field["fieldname"]] = field["default"]
		values.update({"enabled": 1, "doctype": "ERPNext MCP Settings"})
		values.update(overrides)
		STORE.singles["ERPNext MCP Settings"] = values
		STORE.passwords[("ERPNext MCP Settings", "ERPNext MCP Settings", "auth_token")] = self.TOKEN
		return values

	def set_token(self, token):
		STORE.passwords[("ERPNext MCP Settings", "ERPNext MCP Settings", "auth_token")] = token

	def request(self, payload, token=None, headers=None, remote_addr="127.0.0.1", method="POST", path=None):
		"""Point frappe.local.request at a fake request and return it."""
		# A NEW REQUEST IS A NEW `frappe.local`. Real Frappe rebuilds that
		# namespace for every request, so the per-request scratch this app parks
		# there cannot survive into the next call — the identity
		# `security.capture_calling_user` saved, and v0.17.2's record of WHICH
		# DOOR the caller came in through. A double that carried them forward
		# would let one call's answer be read in the next call's audit row, which
		# is a bug that cannot happen on a site and would be invisible here.
		# `form_dict` goes with them. Real Frappe rebuilds it from THIS request's
		# body in `make_form_dict`, so one request's arguments cannot be read by
		# the next — and v0.17.2 reads `_auth` out of it.
		for scratch in ("erpnext_mcp_calling_user", "erpnext_mcp_fallback_auth_source", "form_dict"):
			frappe.local.pop(scratch, None)
		all_headers = {"Content-Type": "application/json"}
		if token is not False:
			# X-MCP-Token, not Authorization: Bearer — see security.presented_token
			# for why that is the documented header. The Bearer path has its own
			# test rather than being the default the whole suite exercises.
			all_headers["X-MCP-Token"] = token or self.TOKEN
		all_headers.update(headers or {})
		request = FakeRequest(
			body=payload if isinstance(payload, str) else json.dumps(payload),
			headers=all_headers,
			remote_addr=remote_addr,
			method=method,
			**({"path": path} if path else {}),
		)
		frappe.local.request = request
		# ONLY when a credential was actually presented. A request with no
		# `Authorization: token …` leaves the session exactly as the test set it,
		# which is faithful — Frappe's api-key validator does nothing when there is
		# no api-key header, and the session comes from the cookie instead.
		presented = authenticated_user(all_headers)
		if presented:
			frappe.local.session = FrappeDict(user=presented, data=FrappeDict())
		return request

	def call(self, method, params=None, request_id=1, **kwargs):
		"""POST one JSON-RPC message through the real endpoint. Returns (body, status)."""
		# Imported here rather than at module scope: the stub has to be in
		# sys.modules before anything from erpnext_mcp is loaded.
		from erpnext_mcp import mcp

		message = {"jsonrpc": "2.0", "id": request_id, "method": method}
		if params is not None:
			message["params"] = params
		self.request(message, **kwargs)
		response = mcp.handle()
		# Frappe commits at the end of a served request. Without this the double
		# accumulates uncommitted rows across calls, so a rollback inside call N
		# would discard everything calls 1..N-1 wrote — which is not a thing that
		# can happen on a real site, and made "re-running an import is safe" look
		# like a bug in the app rather than in the double.
		STORE.commit()
		body = response.get_data(as_text=True)
		parsed = json.loads(body) if body.strip() else None
		return parsed, response.status_code

	def tool(self, name, arguments=None, **kwargs):
		"""Call one tool and return its parsed result dict."""
		body, status = self.call("tools/call", {"name": name, "arguments": arguments or {}}, **kwargs)
		self.assertEqual(status, 200, body)
		return body["result"]

	def tool_data(self, name, arguments=None, **kwargs):
		"""Call one tool, assert it succeeded, and return its parsed payload."""
		result = self.tool(name, arguments, **kwargs)
		self.assertFalse(result.get("isError"), f"{name} failed: {result['content'][0]['text']}")
		return json.loads(result["content"][0]["text"])

	def tool_error(self, name, arguments=None, **kwargs):
		"""Call one tool, assert it failed, and return the error text."""
		result = self.tool(name, arguments, **kwargs)
		self.assertTrue(result.get("isError"), f"{name} unexpectedly succeeded: {result}")
		return result["content"][0]["text"]

	# -- convenience assertions ----------------------------------------------
	def audit_rows(self, **filters):
		rows = STORE.rows("MCP Action Log")
		return [row for row in rows if _match(row, filters or None)]

	def assertAudited(self, tool_name, status=None):
		rows = self.audit_rows(tool_name=tool_name)
		self.assertTrue(rows, f"no MCP Action Log row for {tool_name}")
		if status:
			self.assertEqual(rows[-1]["result_status"], status, rows[-1])
		return rows[-1]
