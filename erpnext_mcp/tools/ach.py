# SPDX-License-Identifier: MIT
"""Direct deposit: where an employee's wages go, and the file that sends them.

Two things live here. The register of employee bank accounts — CRUD, and the
allocation arithmetic that turns one net pay into one or several deposits — and
the generators that turn a completed payroll run into a NACHA file for the
company's bank. The file FORMAT is `..nacha`, which is pure and knows nothing
about this site; everything site-shaped is here.

────────────────────────────────────────────────────────────────────────────
THE ACCOUNT NUMBER LEAVES THIS SITE IN EXACTLY ONE DIRECTION
────────────────────────────────────────────────────────────────────────────

It is stored in a Password field, which Frappe keeps in its encrypted `__Auth`
table rather than in a column. No read tool here returns it, `get_employee_bank_account`
returns the last four, and the mobile route returns the last four. The one place
the full number is materialised is inside a generated ACH file, and that file is
written as a PRIVATE attachment — the same rule `artifacts.attach_bytes`
enforces for a 1099.

This is why there is no `list` field selection that could accidentally include
it: the field list is a constant, and the constant does not contain it.

────────────────────────────────────────────────────────────────────────────
WHY AN UNRESOLVABLE ALLOCATION REFUSES THE WHOLE FILE
────────────────────────────────────────────────────────────────────────────

An employee with NO active bank account is skipped and reported: they are paid
by cheque, which is an ordinary thing on a farm, and a payroll file that refused
to exist because one picker has no bank account would be useless.

An employee WITH accounts whose allocations do not add up is different, and it
refuses the entire file rather than paying them what could be resolved. A
half-allocated cheque is not a smaller payment — it is a wrong one, and it is
wrong silently, on a file that balances internally because the batch total is
computed from the entries actually written. There is no partially-correct
payroll file; there is a correct one and a support call.

────────────────────────────────────────────────────────────────────────────
PRENOTES
────────────────────────────────────────────────────────────────────────────

A prenote is a zero-dollar entry with its own transaction code that asks the
receiving bank to confirm an account exists. NACHA gives the RDFI three banking
days to return it, and the convention is to wait that out before sending real
money. `generate_prenote_file` marks the accounts it wrote, and
`generate_nacha_file` WARNS about accounts that were never prenoted or are still
inside the window — it warns rather than refuses, because the waiting period is
a convention between a company and its bank and not something this app should
enforce against a payroll that has to go out.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, getdate, nowdate
from frappe.utils import now as nowdate_time

from .. import nacha
from ..args import as_bool, as_int, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import artifacts

EMPLOYEE_BANK_ACCOUNT = "Employee Bank Account"
ACH_ORIGINATOR = "ACH Originator Configuration"
PAYROLL_ENTRY = "Farm Payroll Entry"
EMPLOYEE = "Employee"

#: The fields a read tool may return. `account_number` IS NOT IN THIS LIST and
#: must not be added to it — see the module docstring. The last four are a
#: separate column precisely so that display never needs the real number.
SAFE_FIELDS = (
	"name",
	"employee",
	"employee_name",
	"company",
	"bank_name",
	"routing_number",
	"account_number_last_four",
	"account_type",
	"allocation_type",
	"allocation_amount",
	"priority",
	"status",
	"prenote_sent",
	"prenote_date",
	"last_deposit_date",
)

#: NACHA gives the receiving bank three banking days to return a prenote.
PRENOTE_WAIT_DAYS = 3

ACCOUNT_TYPES = ("Checking", "Savings")
ALLOCATION_TYPES = ("Full", "Fixed Amount", "Percentage")
STATUSES = ("Active", "Inactive")


# ── read tools ─────────────────────────────────────────────────────────────


def get_employee_bank_account(args: dict) -> ToolResult:
	"""One bank account by name, or all of one employee's. Never the full number."""
	name = as_str(args, "name")
	if name:
		row = _safe_row(name)
		return ToolResult(
			data=row,
			summary=f"{row['bank_name']} {row['account_type'].lower()} ****"
			f"{row['account_number_last_four']} for {row['employee_name'] or row['employee']}",
		)

	employee = _resolve_employee(args)
	rows = _employee_accounts(employee, active_only=False)
	return ToolResult(
		data={"employee": employee, "accounts": rows, "count": len(rows)},
		summary=f"{len(rows)} bank account(s) for {employee}",
	)


def list_employee_bank_accounts(args: dict) -> ToolResult:
	"""Bank accounts across employees, filtered. Never the full number."""
	filters = {}
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)
	employee = as_str(args, "employee")
	if employee:
		filters["employee"] = _resolve_employee(args)
	status = as_str(args, "status")
	if status:
		filters["status"] = _choice(status, STATUSES, "status")
	if as_bool(args, "prenote_pending") is True:
		filters["prenote_sent"] = 0

	rows = frappe.db.get_all(
		EMPLOYEE_BANK_ACCOUNT,
		filters=filters,
		fields=list(SAFE_FIELDS),
		limit_page_length=as_limit(args),
		order_by="employee asc, priority asc, creation asc",
	)
	rows = [_mask(dict(r)) for r in rows]
	return ToolResult(
		data={"accounts": rows, "count": len(rows)},
		summary=f"{len(rows)} employee bank account(s)",
	)


# ── mutating tools ─────────────────────────────────────────────────────────


def create_employee_bank_account(args: dict) -> ToolResult:
	"""Register one deposit destination for one employee."""
	employee = _resolve_employee(args)
	company = resolve_company(as_str(args, "company")) or frappe.db.get_value(
		EMPLOYEE,
		employee,
		"company",
	)
	if not company:
		raise ToolError(f"employee {employee!r} has no company, and none was given.")

	routing = _routing(as_str(args, "routing_number", required=True))
	account_number = _account_number(as_str(args, "account_number", required=True))
	account_type = _choice(as_str(args, "account_type") or "Checking", ACCOUNT_TYPES, "account_type")
	allocation_type = _choice(
		as_str(args, "allocation_type") or "Full",
		ALLOCATION_TYPES,
		"allocation_type",
	)
	allocation_amount = _allocation_amount(args, allocation_type)
	bank_name = as_str(args, "bank_name", required=True)
	status = _choice(as_str(args, "status") or "Active", STATUSES, "status")

	if status == "Active":
		_check_allocation_set(employee, allocation_type, allocation_amount, exclude="")

	doc = frappe.get_doc(
		{
			"doctype": EMPLOYEE_BANK_ACCOUNT,
			"employee": employee,
			"employee_name": frappe.db.get_value(EMPLOYEE, employee, "employee_name") or "",
			"company": company,
			"bank_name": bank_name,
			"routing_number": routing,
			"account_number": account_number,
			"account_number_last_four": account_number[-4:],
			"account_type": account_type,
			"allocation_type": allocation_type,
			"allocation_amount": allocation_amount,
			"priority": as_int(args, "priority", 0) or 0,
			"status": status,
			"prenote_sent": 0,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()

	return ToolResult(
		data=_safe_row(doc.name),
		summary=f"Bank account registered for {employee}: {bank_name} "
		f"{account_type.lower()} ****{account_number[-4:]} ({allocation_type})",
	)


def update_employee_bank_account(args: dict) -> ToolResult:
	"""Change one bank account. Re-validates the employee's whole allocation set."""
	name = as_str(args, "name", required=True)
	if not frappe.db.exists(EMPLOYEE_BANK_ACCOUNT, name):
		raise ToolError(f"no Employee Bank Account called {name!r} on this site.")

	doc = frappe.get_doc(EMPLOYEE_BANK_ACCOUNT, name)
	updated = []

	if args.get("bank_name") is not None:
		doc.bank_name = as_str(args, "bank_name", required=True)
		updated.append("bank_name")
	if args.get("routing_number") is not None:
		doc.routing_number = _routing(as_str(args, "routing_number", required=True))
		updated.append("routing_number")
	if args.get("account_number") is not None:
		number = _account_number(as_str(args, "account_number", required=True))
		doc.account_number = number
		doc.account_number_last_four = number[-4:]
		updated.append("account_number")
	if args.get("account_type") is not None:
		doc.account_type = _choice(as_str(args, "account_type"), ACCOUNT_TYPES, "account_type")
		updated.append("account_type")
	if args.get("priority") is not None:
		doc.priority = as_int(args, "priority", 0) or 0
		updated.append("priority")
	if args.get("status") is not None:
		doc.status = _choice(as_str(args, "status"), STATUSES, "status")
		updated.append("status")

	if args.get("allocation_type") is not None:
		doc.allocation_type = _choice(
			as_str(args, "allocation_type"),
			ALLOCATION_TYPES,
			"allocation_type",
		)
		updated.append("allocation_type")
	if args.get("allocation_amount") is not None or "allocation_type" in updated:
		doc.allocation_amount = _allocation_amount(
			args,
			str(doc.allocation_type),
			current=float(doc.allocation_amount or 0),
		)
		if "allocation_amount" not in updated and args.get("allocation_amount") is not None:
			updated.append("allocation_amount")

	if not updated:
		raise ToolError("no fields to update. Pass at least one changeable field.")

	# CHANGING THE ACCOUNT INVALIDATES ITS PRENOTE. The bank confirmed the old
	# number, not this one, so a stale tick would let a never-verified account
	# through the warning in `generate_nacha_file` on the strength of a test that
	# was run against different digits.
	if "routing_number" in updated or "account_number" in updated:
		doc.prenote_sent = 0
		doc.prenote_date = None

	if str(doc.status) == "Active":
		_check_allocation_set(
			str(doc.employee),
			str(doc.allocation_type),
			float(doc.allocation_amount or 0),
			exclude=name,
		)

	doc.flags.ignore_permissions = True
	doc.save()

	return ToolResult(
		data={**_safe_row(name), "updated_fields": updated},
		summary=f"Bank account {name} updated: {', '.join(updated)}",
	)


def generate_nacha_file(args: dict) -> ToolResult:
	"""Turn a completed Farm Payroll Entry into a NACHA file for the company's bank."""
	entry = _load_payroll_entry(as_str(args, "payroll_entry", required=True))
	company = str(entry.get("company") or "")
	originator = _load_originator(company)

	effective = _effective_date(args, originator)
	slips = entry.get("slips") or []
	if not slips:
		raise ToolError(f"payroll entry {entry['name']} has no slips, so there is nothing to deposit.")

	entries: list[dict] = []
	skipped: list[dict] = []
	problems: list[str] = []
	warnings: list[str] = []
	paid: list[dict] = []
	touched: list[str] = []

	for slip in slips:
		employee = str(slip.get("employee") or "")
		name = str(slip.get("employee_name") or employee)
		net = round(float(slip.get("net_pay") or 0), 2)

		accounts = _employee_accounts(employee, active_only=True, with_secret=True)
		if not accounts:
			skipped.append(
				{
					"employee": employee,
					"employee_name": name,
					"net_pay": net,
					"reason": "no active bank account — pay by cheque",
				}
			)
			continue
		if net <= 0:
			skipped.append(
				{
					"employee": employee,
					"employee_name": name,
					"net_pay": net,
					"reason": "net pay is not positive",
				}
			)
			continue

		try:
			splits = allocate(net, accounts)
		except ToolError as exc:
			problems.append(f"{name} ({employee}): {exc}")
			continue

		for account, amount in splits:
			warnings.extend(_prenote_warnings(account, name))
			entries.append(
				{
					"routing_number": account["routing_number"],
					"account_number": account["account_number"],
					"account_type": account["account_type"],
					"amount": amount,
					"individual_id": employee,
					"individual_name": name,
				}
			)
			touched.append(account["name"])
		paid.append(
			{
				"employee": employee,
				"employee_name": name,
				"net_pay": net,
				"entries": len(splits),
			}
		)

	if problems:
		raise ToolError(
			"the deposit allocations do not resolve for "
			f"{len(problems)} employee(s), so no file was written — a payroll file that paid "
			"everybody else and shorted them would balance internally and still be wrong. "
			+ " | ".join(problems)
		)
	if not entries:
		raise ToolError(
			f"none of the {len(slips)} slip(s) on {entry['name']} has an active bank account with "
			"positive net pay, so there is nothing to send. Everybody on this run is paid by "
			"cheque."
		)

	built = nacha.build_file(
		originator=originator,
		entries=entries,
		file_creation_date=_yymmdd(nowdate()),
		file_creation_time=_hhmm(),
		effective_entry_date=_yymmdd(effective),
		entry_description=as_str(args, "entry_description") or originator["entry_description"],
		file_id_modifier=(as_str(args, "file_id_modifier") or "A")[:1].upper(),
		descriptive_date=_yymmdd(entry.get("pay_period_end") or effective),
		prenote=False,
	)

	total = round(sum(e["amount"] for e in entries), 2)
	if abs(total - built["total_credit"]) > 0.005:  # pragma: no cover - arithmetic guard
		raise ToolError(
			f"the file's credit total ({built['total_credit']}) does not match the deposits "
			f"computed from the slips ({total}). Nothing was written."
		)

	file_name = f"ach-{entry['name']}-{_yymmdd(effective)}.ach"
	delivery = _deliver(args, PAYROLL_ENTRY, entry["name"], file_name, built["content"])

	if as_bool(args, "mark_deposited") is True:
		today = nowdate()
		for account_name in sorted(set(touched)):
			frappe.db.set_value(
				EMPLOYEE_BANK_ACCOUNT,
				account_name,
				"last_deposit_date",
				today,
			)

	data = {
		"payroll_entry": entry["name"],
		"company": company,
		"effective_entry_date": str(effective),
		"entry_count": built["entry_count"],
		"record_count": built["record_count"],
		"block_count": built["block_count"],
		"batch_count": built["batch_count"],
		"entry_hash": built["entry_hash"],
		"total_credit": built["total_credit"],
		"employees_paid": paid,
		"employees_skipped": skipped,
		"warnings": warnings,
		**delivery,
	}
	summary = (
		f"NACHA file for {entry['name']}: {built['entry_count']} entr(ies), "
		f"${built['total_credit']:,.2f} effective {effective}"
	)
	if skipped:
		summary += f", {len(skipped)} employee(s) skipped"
	if warnings:
		summary += f", {len(warnings)} prenote warning(s)"
	return ToolResult(data=data, summary=summary)


def generate_prenote_file(args: dict) -> ToolResult:
	"""Zero-dollar test entries that ask the banks to confirm the accounts exist."""
	company = resolve_company(as_str(args, "company"), required=True)
	originator = _load_originator(company)
	effective = _effective_date(args, originator)

	# v0.225.0: an approved self-service account (status Pending) is prenoted too —
	# it is never PAID until its hold passes (direct_deposit.activate_due).
	filters = {"company": company, "status": ("in", ["Active", "Pending"])}
	employee = as_str(args, "employee")
	if employee:
		filters["employee"] = _resolve_employee(args)
	names = as_str(args, "accounts")
	if as_bool(args, "resend") is not True:
		filters["prenote_sent"] = 0

	rows = frappe.db.get_all(
		EMPLOYEE_BANK_ACCOUNT,
		filters=filters,
		fields=list(SAFE_FIELDS),
		order_by="employee asc, priority asc, creation asc",
	)
	if names:
		wanted = {n.strip() for n in str(names).split(",") if n.strip()}
		rows = [r for r in rows if r["name"] in wanted]
	rows = [
		r
		for r in rows
		if r.get("status") == "Active" or frappe.db.get_value(EMPLOYEE_BANK_ACCOUNT, r["name"], "approved_by")
	]
	if not rows:
		raise ToolError(
			f"no active bank accounts for {company} are awaiting a prenote. Pass resend=true to "
			"send one for accounts already prenoted, or register an account first."
		)

	entries = []
	for row in rows:
		secret = _full_account_number(row["name"])
		entries.append(
			{
				"routing_number": row["routing_number"],
				"account_number": secret,
				"account_type": row["account_type"],
				"amount": 0,
				"individual_id": row["employee"],
				"individual_name": row["employee_name"] or row["employee"],
			}
		)

	built = nacha.build_file(
		originator=originator,
		entries=entries,
		file_creation_date=_yymmdd(nowdate()),
		file_creation_time=_hhmm(),
		effective_entry_date=_yymmdd(effective),
		entry_description=as_str(args, "entry_description") or "PRENOTE",
		file_id_modifier=(as_str(args, "file_id_modifier") or "A")[:1].upper(),
		prenote=True,
	)

	file_name = f"prenote-{_slug(company)}-{_yymmdd(effective)}.ach"
	target = _prenote_attachment_target(company)
	delivery = _deliver(args, target[0], target[1], file_name, built["content"])

	today = nowdate()
	marked = []
	if as_bool(args, "mark_sent") is not False:
		for row in rows:
			frappe.db.set_value(
				EMPLOYEE_BANK_ACCOUNT,
				row["name"],
				{"prenote_sent": 1, "prenote_date": today},
			)
			marked.append(row["name"])
		from .. import direct_deposit

		direct_deposit.on_prenote(marked, str(today))

	data = {
		"company": company,
		"effective_entry_date": str(effective),
		"entry_count": built["entry_count"],
		"record_count": built["record_count"],
		"block_count": built["block_count"],
		"entry_hash": built["entry_hash"],
		"total_credit": 0.0,
		"accounts": [
			{
				"name": r["name"],
				"employee": r["employee"],
				"account_number_last_four": r["account_number_last_four"],
			}
			for r in rows
		],
		"marked_sent": marked,
		"clears_on": str(add_days(today, PRENOTE_WAIT_DAYS)),
		**delivery,
	}
	return ToolResult(
		data=data,
		summary=f"Prenote file for {company}: {built['entry_count']} zero-dollar entr(ies), "
		f"effective {effective}",
	)


# ── allocation ─────────────────────────────────────────────────────────────


def allocate(net_pay: float, accounts: list[dict]) -> list[tuple[dict, float]]:
	"""Split one net pay across one employee's accounts. PURE — no database.

	THE ORDER IS FIXED AND IT MATTERS. Fixed amounts are satisfied first, then
	percentages, then whatever is left goes to the Full account. A percentage is
	taken of the ORIGINAL net pay rather than of the running remainder, because
	"20% to savings" means a fifth of the cheque and not a fifth of what happens
	to survive the fixed transfers — the second reading makes the amount depend on
	the order two sibling rows were created in.

	Cents are reconciled onto the Full account, which is the only row whose amount
	is defined as a remainder and therefore the only one that can absorb a
	rounding difference without contradicting what the employee asked for.

	Returns pairs of (account, amount), dropping any that round to zero — a
	zero-dollar entry in a live payroll batch is not a payment and some RDFIs
	treat it as a malformed prenote.
	"""
	net_pay = nacha.round_money(net_pay)
	ordered = sorted(accounts, key=lambda a: (int(a.get("priority") or 0), str(a.get("name") or "")))

	fixed = [a for a in ordered if str(a.get("allocation_type")) == "Fixed Amount"]
	percentage = [a for a in ordered if str(a.get("allocation_type")) == "Percentage"]
	full = [a for a in ordered if str(a.get("allocation_type")) == "Full"]

	if len(full) > 1:
		raise ToolError(
			f"{len(full)} accounts are marked Full and only one can take the remainder of a cheque."
		)

	splits: list[tuple[dict, float]] = []
	remaining = net_pay

	for account in fixed:
		amount = nacha.round_money(min(float(account.get("allocation_amount") or 0), remaining))
		if amount > 0:
			splits.append((account, amount))
			remaining = nacha.round_money(remaining - amount)

	for account in percentage:
		share = nacha.round_money(net_pay * float(account.get("allocation_amount") or 0) / 100.0)
		amount = nacha.round_money(min(share, remaining))
		if amount > 0:
			splits.append((account, amount))
			remaining = nacha.round_money(remaining - amount)

	if full:
		if remaining > 0:
			splits.append((full[0], nacha.round_money(remaining)))
			remaining = 0.0
	elif remaining > 0.005:
		raise ToolError(
			f"${remaining:,.2f} of ${net_pay:,.2f} net pay is not allocated to any account, and "
			"none is marked Full to take the remainder. Mark one account Full, or raise the fixed "
			"and percentage allocations to cover the whole cheque."
		)

	if not splits:
		raise ToolError(f"the allocations produce no deposit at all out of ${net_pay:,.2f} net pay.")

	total = nacha.round_money(sum(a for _, a in splits))
	if abs(total - net_pay) > 0.005:
		raise ToolError(f"the deposits total ${total:,.2f} but net pay is ${net_pay:,.2f}.")
	return splits


# ── internal helpers ───────────────────────────────────────────────────────


def _resolve_employee(args: dict) -> str:
	emp = as_str(args, "employee") or as_str(args, "employee_name")
	if not emp:
		raise ToolError("employee is required.")
	if frappe.db.exists(EMPLOYEE, emp):
		return emp
	found = frappe.db.get_value(EMPLOYEE, {"employee_name": emp}, "name")
	if found:
		return str(found)
	raise ToolError(f"no Employee called {emp!r} on this site.")


def _choice(value: str, allowed: tuple, label: str) -> str:
	text = str(value or "").strip()
	for option in allowed:
		if text.lower() == option.lower():
			return option
	raise ToolError(f"{label} must be one of: {', '.join(allowed)}. Got {value!r}.")


def _routing(value: str) -> str:
	try:
		return nacha.normalize_routing(value)
	except nacha.NachaError as exc:
		raise ToolError(str(exc)) from None


def _account_number(value: str) -> str:
	cleaned = "".join(c for c in str(value or "") if c.isalnum())
	if not cleaned:
		raise ToolError("account_number has no letters or digits in it.")
	if len(cleaned) > 17:
		raise ToolError(
			f"account_number is {len(cleaned)} characters. An ACH entry carries at most 17, so a "
			"longer one cannot be sent without being truncated into a different account."
		)
	return cleaned


def _allocation_amount(args: dict, allocation_type: str, current: float = 0.0) -> float:
	if allocation_type == "Full":
		return 0.0
	raw = args.get("allocation_amount")
	amount = float(raw) if raw not in (None, "") else float(current)
	if amount <= 0:
		raise ToolError(
			f"an allocation type of {allocation_type} needs an allocation_amount above zero. Use "
			"Full for the account that takes whatever is left."
		)
	if allocation_type == "Percentage" and amount > 100:
		raise ToolError(f"a percentage allocation of {amount:g} is more than the whole cheque.")
	return amount


def _check_allocation_set(employee: str, allocation_type: str, amount: float, exclude: str) -> None:
	"""One Full account per employee; percentages totalling 100 or less.

	Mirrors `EmployeeBankAccount._validate_sibling_allocations`. Both exist
	because the controller catches the Desk and an import while this catches the
	tool — and the standalone suite exercises the tool.
	"""
	siblings = [
		row
		for row in frappe.db.get_all(
			EMPLOYEE_BANK_ACCOUNT,
			filters={"employee": employee, "status": "Active"},
			fields=["name", "allocation_type", "allocation_amount"],
		)
		if row["name"] != exclude
	]

	if allocation_type == "Full":
		existing = [s for s in siblings if str(s["allocation_type"]) == "Full"]
		if existing:
			raise ToolError(
				f"{employee} already has a Full deposit account ({existing[0]['name']}). Only one "
				"account can take the remainder of a cheque — make this one a Fixed Amount or a "
				"Percentage, or deactivate the other."
			)

	if allocation_type == "Percentage":
		total = amount + sum(
			float(s["allocation_amount"] or 0) for s in siblings if str(s["allocation_type"]) == "Percentage"
		)
		if total > 100:
			raise ToolError(
				f"the percentage allocations for {employee} would total {total:g}%, which is more "
				"than the cheque. Reduce one of them."
			)


def _employee_accounts(employee: str, active_only: bool, with_secret: bool = False) -> list[dict]:
	filters = {"employee": employee}
	if active_only:
		filters["status"] = "Active"
	rows = frappe.db.get_all(
		EMPLOYEE_BANK_ACCOUNT,
		filters=filters,
		fields=list(SAFE_FIELDS),
		order_by="priority asc, creation asc",
	)
	out = []
	for row in rows:
		row = dict(row)
		if with_secret:
			row["account_number"] = _full_account_number(row["name"])
		else:
			row = _mask(row)
		out.append(row)
	return out


def _full_account_number(name: str) -> str:
	"""The one read path that materialises the secret. Only generators call it."""
	doc = frappe.get_doc(EMPLOYEE_BANK_ACCOUNT, name)
	number = (
		doc.get_password("account_number", raise_exception=False)
		if hasattr(
			doc,
			"get_password",
		)
		else None
	)
	number = str(number or doc.get("account_number") or "").strip()
	if not number or set(number) == {"*"}:
		raise ToolError(
			f"the account number for {name} could not be read back. It is stored encrypted and "
			"may have been cleared — re-enter it with update_employee_bank_account."
		)
	return number


def _safe_row(name: str) -> dict:
	if not frappe.db.exists(EMPLOYEE_BANK_ACCOUNT, name):
		raise ToolError(f"no Employee Bank Account called {name!r} on this site.")
	row = frappe.db.get_value(EMPLOYEE_BANK_ACCOUNT, name, list(SAFE_FIELDS), as_dict=True)
	return _mask(dict(row))


def _mask(row: dict) -> dict:
	"""Belt to the field-list brace: strip a secret even if one arrives.

	v0.267.1 (Tim, access audit #1): the ROUTING number is masked the same way as the account number — last four —
	for everyone but the payroll / HR principals (the private-HR gate: HR Manager, HR User, System Manager). The
	NACHA and prenote generators never come through here; they read the stored values directly.
	"""
	row.pop("account_number", None)
	last_four = str(row.get("account_number_last_four") or "")
	row["account_number_masked"] = f"****{last_four}" if last_four else ""
	routing = "".join(ch for ch in str(row.get("routing_number") or "") if ch.isdigit())
	row["routing_number_last_four"] = routing[-4:]
	if routing and not sees_full_routing():
		row["routing_number"] = f"*****{routing[-4:]}"
	return row


def sees_full_routing(actor: str = "") -> bool:
	"""v0.267.1. Payroll / HR (the private-HR gate) read the full routing number; everyone else its last four."""
	from .. import security
	from ..api import guard

	actor = actor or security.caller_identity() or str(getattr(frappe.session, "user", "") or "")
	if not actor or actor == "Guest":
		return False
	return bool(guard.roles_held(actor) & guard._gate("private_hr", guard.PRIVATE_HR_ROLES))


def _prenote_warnings(account: dict, employee_name: str) -> list[str]:
	if not int(account.get("prenote_sent") or 0):
		return [
			f"{employee_name}: account ****{account.get('account_number_last_four')} has never "
			"been prenoted, so the bank has not confirmed it exists."
		]
	sent = account.get("prenote_date")
	if sent and getdate(sent) > getdate(add_days(nowdate(), -PRENOTE_WAIT_DAYS)):
		return [
			f"{employee_name}: account ****{account.get('account_number_last_four')} was prenoted "
			f"on {sent}, inside the {PRENOTE_WAIT_DAYS}-banking-day return window."
		]
	return []


def _load_payroll_entry(name: str) -> dict:
	if not frappe.db.exists(PAYROLL_ENTRY, name):
		raise ToolError(f"no Farm Payroll Entry called {name!r} on this site.")
	doc = frappe.get_doc(PAYROLL_ENTRY, name)
	status = str(doc.get("status") or "")
	if status not in ("Calculated", "Submitted"):
		raise ToolError(
			f"payroll entry {name} is {status or 'unset'}. An ACH file may only be generated from "
			"a run that has been calculated, because the net pay it would send is not final until "
			"then. Calculate the run first."
		)
	return {
		"name": doc.name,
		"company": doc.get("company"),
		"status": status,
		"pay_period_end": doc.get("pay_period_end"),
		"slips": [
			{
				"employee": row.get("employee"),
				"employee_name": row.get("employee_name"),
				"net_pay": row.get("net_pay"),
			}
			for row in (doc.get("slips") or [])
		],
	}


def _load_originator(company: str) -> dict:
	name = frappe.db.get_value(ACH_ORIGINATOR, {"company": company, "status": "Active"}, "name")
	if not name:
		raise ToolError(
			f"no active ACH Originator Configuration for {company}. An ACH file carries the "
			"company's own bank identifiers — the originating routing number and the company "
			"identification its bank issued — and none of them can be inferred from anything else "
			"on this site. Create one first."
		)
	doc = frappe.get_doc(ACH_ORIGINATOR, name)
	originating = str(doc.get("originating_dfi") or "")
	return {
		"originating_dfi": originating,
		"immediate_destination": str(doc.get("immediate_destination") or "") or originating,
		"immediate_destination_name": str(doc.get("immediate_destination_name") or ""),
		"immediate_origin": str(doc.get("immediate_origin") or ""),
		"immediate_origin_name": str(doc.get("immediate_origin_name") or "") or str(company),
		"company_name": str(doc.get("company_name") or "") or str(company),
		"company_identification": str(doc.get("company_identification") or ""),
		"entry_description": str(doc.get("entry_description") or "") or "PAYROLL",
		"discretionary_data": str(doc.get("discretionary_data") or ""),
		"settlement_days": int(doc.get("settlement_days") or 0),
	}


def _effective_date(args: dict, originator: dict):
	given = as_str(args, "effective_entry_date")
	if given:
		return getdate(given)
	return getdate(add_days(nowdate(), originator.get("settlement_days") or 0))


def _prenote_attachment_target(company: str) -> tuple[str, str]:
	"""Where a prenote file is attached — the company, which always exists."""
	return ("Company", company)


def _deliver(args: dict, doctype: str, name: str, file_name: str, content: str) -> dict:
	"""Attach the file, and optionally also write it to the site's file storage."""
	payload = content.encode("ascii", errors="replace")
	attachment = artifacts.attach_bytes(doctype, name, file_name, payload)
	out = {
		"file_name": file_name,
		"attachment": artifacts.describe_attachment(attachment, payload),
		"attached_to": {"doctype": doctype, "name": name},
	}
	path = artifacts.resolve_output_path(as_str(args, "output_path"), file_name)
	if path:
		out["written"] = artifacts.write_output(
			path,
			payload,
			as_bool(args, "overwrite") is True,
		)
	return out


def _yymmdd(value) -> str:
	return getdate(value).strftime("%y%m%d")


def _hhmm() -> str:
	"""The file creation time, HHMM, off `frappe.utils.now`.

	Parsed out of the timestamp string rather than taken from a datetime object
	because `now` is what both the framework and the test double agree on —
	`now_datetime` exists on a bench and not in the double, and a helper that
	only works on one of them is a helper whose behaviour is untested.

	The field is informational: the ACH network settles on the effective entry
	date, not on this. It exists so two files sent the same day can be told
	apart, which the file ID modifier also does.
	"""
	stamp = str(nowdate_time() or "")
	# "2026-08-17 09:30:00.000000" — the clock is characters 11-16.
	digits = "".join(c for c in stamp[11:16] if c.isdigit())
	return digits.rjust(4, "0")[:4] if digits else "0000"


def _slug(value: str) -> str:
	return "".join(c if c.isalnum() else "-" for c in str(value)).strip("-").lower()[:40]
