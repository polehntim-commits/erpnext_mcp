# SPDX-License-Identifier: MIT
"""Secure direct-deposit setup: in the app only, verified, approved, held. v0.225.0.

docs/design/direct_deposit_setup.md. Off until `direct_deposit_self_service`.

A worker submits a new account from their own authenticated phone (Face ID when
device keys are on). It becomes an Employee Bank Account with status **Pending**
— which `generate_nacha_file` never pays — and the account it replaces keeps
paying. It is verified (Plaid Auth + Identity, or the bank's own direct-deposit
form), approved by a manager in the Desk, prenoted, and only made Active after
the hold. A notice goes to the worker's contact channel on submit and on
activation. Numbers are masked everywhere: last 4 only.
"""

from __future__ import annotations

import hashlib
import io
import re

import frappe

from . import audit, compat, settings

ACCOUNT = "Employee Bank Account"
EMPLOYEE = "Employee"
EVIDENCE = "Signing Evidence"
PENDING, ACTIVE, INACTIVE, REJECTED = "Pending", "Active", "Inactive", "Rejected"
SUBMITTED, VERIFIED, NEEDS_REVIEW, FAILED = "Submitted", "Verified", "Needs review", "Failed"
ACCOUNT_TYPES = ("Checking", "Savings")
APPROVER_ROLES = ("System Manager", "HR Manager")
HOLD_DEFAULT = 14
MAX_FORM_BYTES = 10 * 1024 * 1024
FORM_TYPES = (".pdf", ".jpg", ".jpeg", ".png", ".heic")
#: Sponsor banks behind the fintech accounts workers bring. A hint for the bank
#: name only; any routing number with a valid checksum is accepted.
KNOWN_ROUTING = {
	"041215663": "Sutton Bank (Cash App)",
	"073923033": "Lincoln Savings Bank (Cash App)",
	"031101279": "The Bancorp Bank (Chime)",
	"103100195": "Stride Bank (Chime)",
}


class Refused(Exception):
	"""A refusal the caller reads. Nothing was changed."""


# ── settings ────────────────────────────────────────────────────────────────
def enabled() -> bool:
	return settings.as_bool(settings._value("direct_deposit_self_service"))


def plaid_enabled() -> bool:
	return enabled() and settings.as_bool(settings._value("direct_deposit_plaid_enabled"))


def hold_days() -> int:
	try:
		return max(0, int(settings._value("direct_deposit_hold_days") or HOLD_DEFAULT))
	except (TypeError, ValueError):
		return HOLD_DEFAULT


# ── numbers ─────────────────────────────────────────────────────────────────
def digits(value) -> str:
	return re.sub(r"\D", "", str(value or ""))


def aba_valid(routing) -> bool:
	"""The ABA checksum: 3·(d1+d4+d7) + 7·(d2+d5+d8) + (d3+d6+d9) ≡ 0 (mod 10)."""
	r = digits(routing)
	if len(r) != 9:
		return False
	d = [int(c) for c in r]
	return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])) % 10 == 0


def mask(value) -> str | None:
	text = digits(value)
	return f"•••• {text[-4:]}" if text else None


def account_hash(account: str) -> str:
	return hashlib.sha256(digits(account).encode()).hexdigest()


def signed_message(employee: str, routing: str, account: str, account_type: str) -> str:
	return f"farmops-direct-deposit|{employee}|{digits(routing)}|{account_hash(account)}|{account_type}"


# ── whose ───────────────────────────────────────────────────────────────────
def employee_of(user: str) -> dict:
	if not user or not compat.doctype_exists(EMPLOYEE):
		return {}
	row = frappe.db.get_value(
		EMPLOYEE,
		{"user_id": user},
		compat.existing_fields(
			EMPLOYEE,
			(
				"name",
				"employee_name",
				"company",
				"personal_email",
				"company_email",
				"prefered_email",
				"user_id",
			),
		),
		as_dict=True,
	)
	return dict(row or {})


def _row(name: str) -> dict:
	if not name or not frappe.db.exists(ACCOUNT, name):
		raise Refused(f"no direct-deposit account {name!r}. Nothing was changed.")
	return dict(frappe.get_doc(ACCOUNT, name).as_dict())


def _full_number(name: str) -> str:
	from .tools import ach

	return ach._full_account_number(name)


def masked_row(row: dict) -> dict:
	return {
		"name": row.get("name"),
		"bank_name": row.get("bank_name"),
		"routing": mask(row.get("routing_number")),
		"account": f"•••• {row.get('account_number_last_four')}"
		if row.get("account_number_last_four")
		else None,
		"account_type": row.get("account_type"),
		"status": row.get("status"),
		"verification_state": row.get("verification_state"),
		"verification_method": row.get("verification_method"),
		"approved": bool(row.get("approved_by")),
		"prenote_sent": bool(row.get("prenote_sent")),
		"activates_on": str(row.get("activates_on") or "") or None,
		"allocation_type": row.get("allocation_type"),
	}


def mine(user: str) -> dict:
	emp = employee_of(user)
	if not emp:
		raise Refused("your account is not linked to an Employee, so there is no direct deposit to show.")
	rows = frappe.db.get_all(
		ACCOUNT,
		filters={"employee": emp["name"], "status": ["in", [ACTIVE, PENDING]]},
		fields=compat.existing_fields(
			ACCOUNT,
			(
				"name",
				"bank_name",
				"routing_number",
				"account_number_last_four",
				"account_type",
				"status",
				"verification_state",
				"verification_method",
				"approved_by",
				"prenote_sent",
				"activates_on",
				"allocation_type",
			),
		),
		order_by="creation desc",
		limit=10,
	)
	return {
		"employee": emp["name"],
		"self_service": enabled(),
		"plaid_available": plaid_enabled(),
		"device_keys": _device_keys_on(),
		"accounts": [masked_row(dict(r)) for r in rows],
		"note": "Direct-deposit changes are made only here, in the app — never by email.",
	}


def _device_keys_on() -> bool:
	from . import device_keys

	return device_keys.enabled()


# ── §1 submit ───────────────────────────────────────────────────────────────
def submit(user: str, body: dict, device: str = "", key_bound: bool = False, ip: str = "") -> dict:
	"""Create the Pending account, sign it, notice it. Raises Refused."""
	from . import device_keys

	if not enabled():
		raise Refused("direct-deposit changes from the app are switched off on this farm. Ask the office.")
	emp = employee_of(user)
	if not emp:
		raise Refused("your account is not linked to an Employee. Nothing was changed.")
	routing = digits(body.get("routing_number"))
	account = digits(body.get("account_number"))
	confirm = digits(body.get("account_number_confirm"))
	account_type = str(body.get("account_type") or "Checking").strip().title()
	if not aba_valid(routing):
		raise Refused("that routing number is not valid (9 digits with a checksum). Nothing was changed.")
	if not 4 <= len(account) <= 17:
		raise Refused("an account number is 4 to 17 digits. Nothing was changed.")
	if account != confirm:
		raise Refused("the two account numbers do not match. Nothing was changed.")
	if account_type not in ACCOUNT_TYPES:
		raise Refused("account type is Checking or Savings. Nothing was changed.")
	message = signed_message(emp["name"], routing, account, account_type)
	if device_keys.enabled():
		if not key_bound or not device:
			raise Refused(
				"changing direct deposit takes a phone signed in with Face ID. Nothing was changed."
			)
		try:
			device_keys._verify_approver(user, device, message, str(body.get("signature") or ""))
		except Exception as exc:
			raise Refused(f"{exc}") from None
		method = "Face ID (device key)"
	else:
		method = "App sign-in"
	bank_name = str(body.get("bank_name") or KNOWN_ROUTING.get(routing) or "").strip()[:140] or "Bank"

	for old in frappe.db.get_all(ACCOUNT, filters={"employee": emp["name"], "status": PENDING}, pluck="name"):
		frappe.db.set_value(
			ACCOUNT, old, {"status": REJECTED, "verification_detail": "superseded by a newer request"}
		)
	replaces = frappe.db.get_value(
		ACCOUNT, {"employee": emp["name"], "status": ACTIVE, "allocation_type": "Full"}, "name"
	)
	now = str(frappe.utils.now())[:19]
	doc = frappe.get_doc(
		{
			"doctype": ACCOUNT,
			"employee": emp["name"],
			"employee_name": emp.get("employee_name"),
			"company": emp.get("company"),
			"status": PENDING,
			"priority": 1,
			"bank_name": bank_name,
			"routing_number": routing,
			"account_type": account_type,
			"account_number": account,
			"account_number_last_four": account[-4:],
			"allocation_type": "Full",
			"allocation_amount": 0,
			"prenote_sent": 0,
			"verification_state": SUBMITTED,
			"submitted_via": "phone",
			"submitted_at": now,
			"replaces": replaces or None,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	evidence = _evidence(emp, user, doc.name, message, method, device, ip)
	sent_to = notice(emp, "requested", device)
	frappe.db.set_value(ACCOUNT, doc.name, {"signing_evidence": evidence, "notice_sent_to": sent_to})
	_audit(
		"direct_deposit:submitted",
		doc.name,
		f"{emp['name']} submitted {bank_name} {mask(account)} ({method})",
	)
	return {
		"account": doc.name,
		"bank_name": bank_name,
		"routing": mask(routing),
		"last4": account[-4:],
		"status": PENDING,
		"next": {"plaid": plaid_enabled(), "bank_form": True},
		"note": (
			"Your current account keeps being paid until this one is verified, approved and its hold has "
			f"passed (about {hold_days()} days after the bank confirms it). We sent a notice to your contact "
			"details on file."
		),
	}


def _evidence(
	emp: dict, user: str, account: str, message: str, method: str, device: str, ip: str
) -> str | None:
	if not compat.doctype_exists(EVIDENCE):
		return None
	doc = frappe.get_doc(
		{
			"doctype": EVIDENCE,
			"signer": emp["name"],
			"signer_name": emp.get("employee_name"),
			"signer_user": user,
			"verification_method": method,
			"signed_at": frappe.utils.now(),
			"status": "Recorded",
			"company": emp.get("company"),
			"document_type": ACCOUNT,
			"document_name": account,
			"signature_role": "Employee",
			"document_hash": hashlib.sha256(message.encode()).hexdigest(),
			"hashed_fields": "employee, routing_number, sha256(account_number), account_type",
			"device_id": str(device or "")[:140],
			"ip_address": str(ip or "")[:140],
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _audit(kind: str, account: str, text: str) -> None:
	audit.record(kind, {"account": account}, audit.STATUS_SUCCESS, text[:500], commit=False)


# ── §3 the notice ───────────────────────────────────────────────────────────
def _hide(email: str) -> str:
	local, _, host = str(email).partition("@")
	return f"{local[:1]}***@{host}" if host else "***"


def notice(emp: dict, what: str, device: str = "") -> str:
	"""Email the addresses on file and push the worker's phones. No numbers, no link."""
	addresses = []
	for field in ("personal_email", "company_email", "prefered_email", "user_id"):
		value = str(emp.get(field) or "").strip()
		if "@" in value and value not in addresses:
			addresses.append(value)
	when = str(frappe.utils.now())[:16]
	text = (
		f"A change to your direct deposit was {what} on {when}"
		+ (f" from the phone '{device}'" if device and what == "requested" else "")
		+ ". If this was not you, tell the office now."
	)
	if addresses:
		try:
			frappe.sendmail(
				recipients=addresses,
				subject="Farm Ops: your direct deposit",
				message=f"<p>{text}</p>",
				now=False,
			)
		except Exception:  # pragma: no cover - a notice never undoes the change
			pass
	try:
		from .services import push

		push.send_push_to_employees(
			[emp["name"]],
			{"aps": {"alert": {"title": "Direct deposit", "body": text[: push.MAX_BODY]}}},
			priority="10",
		)
	except Exception:  # pragma: no cover
		pass
	return ", ".join(_hide(a) for a in addresses)[:140] or "push only"


# ── §1.5 verification: Plaid ────────────────────────────────────────────────
def start_plaid(user: str, account: str) -> dict:
	from . import plaid_link

	row = _own_pending(user, account)
	if not plaid_enabled():
		raise Refused(
			"bank verification through Plaid is not switched on. Upload your bank's direct-deposit form instead."
		)
	answer = plaid_link.start(row["employee"])
	frappe.db.set_value(ACCOUNT, account, "plaid_request", answer["request_id"])
	return {
		"account": account,
		"url": answer["hosted_link_url"],
		"callback_scheme": plaid_link.CALLBACK_SCHEME,
	}


def finish_plaid(user: str, account: str) -> dict:
	from . import plaid_link

	row = _own_pending(user, account)
	result = plaid_link.finish(row["employee"])
	if result.get("state") == "pending":
		return {"account": account, "state": "pending"}
	entered_routing, entered_account = digits(row.get("routing_number")), digits(_full_number(account))
	numbers_match = any(
		digits(a.get("routing")) == entered_routing and digits(a.get("account")) == entered_account
		for a in result.get("ach") or []
	)
	name_ok = name_matches(row.get("employee_name") or "", result.get("owner_names") or [])
	state = VERIFIED if numbers_match and name_ok else FAILED
	detail = (
		"Plaid: routing and account match; owner name matches"
		if state == VERIFIED
		else "Plaid: "
		+ ", ".join(
			p
			for p, ok in (("numbers do not match", numbers_match), ("owner name does not match", name_ok))
			if not ok
		)
	)
	frappe.db.set_value(
		ACCOUNT,
		account,
		{
			"verification_state": state,
			"verification_method": "Plaid",
			"name_match": 1 if name_ok else 0,
			"verification_detail": detail,
		},
	)
	_audit(
		"direct_deposit:verified" if state == VERIFIED else "direct_deposit:verification_failed",
		account,
		detail,
	)
	return {"account": account, "state": state, "detail": detail}


def name_matches(employee_name: str, owner_names: list) -> bool:
	"""First and last name tokens of the Employee both appear in one Plaid owner name."""
	want = [t for t in re.findall(r"[a-z]+", employee_name.lower()) if len(t) > 1]
	if len(want) < 2:
		return False
	first, last = want[0], want[-1]
	for owner in owner_names:
		tokens = set(re.findall(r"[a-z]+", str(owner).lower()))
		if first in tokens and last in tokens:
			return True
	return False


# ── §1.5 verification: the bank's form ──────────────────────────────────────
def _numbers_in(text: str) -> set:
	return {m for m in re.findall(r"\d{4,17}", re.sub(r"(?<=\d)[ -](?=\d)", "", text or ""))}


def _pdf_text(content: bytes) -> str:
	try:
		from pypdf import PdfReader

		return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages[:5])
	except Exception:
		return ""


def upload_form(
	user: str, account: str, file_name: str, content: bytes, extracted_routing: str, extracted_account: str
) -> dict:
	from .tools import artifacts

	row = _own_pending(user, account)
	name = str(file_name or "").lower()
	if not name.endswith(FORM_TYPES):
		raise Refused("the form must be a PDF or a photo (JPEG, PNG, HEIC). Nothing was changed.")
	if not content or len(content) > MAX_FORM_BYTES:
		raise Refused("the file is empty or over 10 MB. Nothing was changed.")
	routing, full = digits(row.get("routing_number")), digits(_full_number(account))
	if digits(extracted_routing) != routing or digits(extracted_account) != full:
		raise Refused(
			"the numbers read from the form do not match what you entered. Check both, or enter them again. Nothing was changed."
		)
	attachment = artifacts.attach_bytes(
		ACCOUNT, account, f"direct-deposit-form-{account}{name[name.rfind('.') :]}", content
	)
	sha = hashlib.sha256(content).hexdigest()
	text = _pdf_text(content) if name.endswith(".pdf") else ""
	found = _numbers_in(text)
	if text and routing in found and full in found:
		state, detail = (
			VERIFIED,
			"Bank form: the server read the same routing and account numbers from the PDF",
		)
	elif text:
		state, detail = FAILED, "Bank form: the server could not find the entered numbers in the PDF's text"
	else:
		state, detail = (
			NEEDS_REVIEW,
			"Bank form (photo or scanned PDF): the phone read matching numbers; a manager must compare by eye",
		)
	frappe.db.set_value(
		ACCOUNT,
		account,
		{
			"verification_state": state,
			"verification_method": "Bank form",
			"verification_detail": detail,
			"evidence_file": getattr(attachment, "file_url", None),
			"evidence_sha256": sha,
		},
	)
	_audit("direct_deposit:form", account, detail)
	return {"account": account, "state": state, "detail": detail}


def _own_pending(user: str, account: str) -> dict:
	if not enabled():
		raise Refused("direct-deposit changes from the app are switched off on this farm.")
	emp = employee_of(user)
	row = _row(account)
	if not emp or row.get("employee") != emp.get("name"):
		raise Refused(f"no direct-deposit change {account!r} of yours. Nothing was changed.")
	if row.get("status") != PENDING:
		raise Refused(f"{account} is {row.get('status')}, not waiting for verification. Nothing was changed.")
	return row


# ── §1.6–1.8 approve, reject, prenote, activate ─────────────────────────────
def approve(account: str, approver: str) -> dict:
	row = _row(account)
	if row.get("status") != PENDING:
		raise Refused(f"{account} is {row.get('status')}. Nothing was changed.")
	if not set(frappe.get_roles(approver) or []) & set(APPROVER_ROLES):
		raise Refused(
			"approving a direct-deposit change takes HR Manager or System Manager. Nothing was changed."
		)
	if frappe.db.get_value(EMPLOYEE, row.get("employee"), "user_id") == approver:
		raise Refused("you cannot approve a change to your own direct deposit. Nothing was changed.")
	if row.get("verification_state") not in (VERIFIED, NEEDS_REVIEW):
		raise Refused(f"{account} is not verified ({row.get('verification_state')}). Nothing was changed.")
	frappe.db.set_value(
		ACCOUNT, account, {"approved_by": approver, "approved_at": str(frappe.utils.now())[:19]}
	)
	_audit(
		"direct_deposit:approved", account, f"{approver} approved {account} ({row.get('verification_state')})"
	)
	return {
		"account": account,
		"approved_by": approver,
		"next": "include it in the next generate_prenote_file",
	}


def reject(account: str, by: str, reason: str = "") -> dict:
	row = _row(account)
	if row.get("status") != PENDING:
		raise Refused(f"{account} is {row.get('status')}. Nothing was changed.")
	frappe.db.set_value(
		ACCOUNT, account, {"status": REJECTED, "verification_detail": (f"rejected by {by}: {reason}")[:140]}
	)
	emp = dict(
		frappe.db.get_value(
			EMPLOYEE,
			row.get("employee"),
			compat.existing_fields(
				EMPLOYEE, ("name", "personal_email", "company_email", "prefered_email", "user_id")
			),
			as_dict=True,
		)
		or {}
	)
	if emp:
		notice(emp, "turned down")
	_audit("direct_deposit:rejected", account, f"{by} rejected {account}: {reason}")
	return {"account": account, "status": REJECTED}


def prenote_returned(account: str, by: str) -> dict:
	row = _row(account)
	frappe.db.set_value(
		ACCOUNT,
		account,
		{"prenote_returned": 1, "status": REJECTED if row.get("status") == PENDING else row.get("status")},
	)
	_audit("direct_deposit:prenote_returned", account, f"{by} recorded a prenote return on {account}")
	return {"account": account, "status": REJECTED if row.get("status") == PENDING else row.get("status")}


def on_prenote(names: list, prenote_date: str) -> None:
	"""generate_prenote_file wrote these: an approved Pending account's hold starts now."""
	activates = str(frappe.utils.add_days(prenote_date, hold_days()))[:10]
	for name in names:
		if frappe.db.get_value(ACCOUNT, name, "status") == PENDING:
			frappe.db.set_value(ACCOUNT, name, "activates_on", activates)


def activate_due() -> list:
	"""Daily. Pending + approved + prenoted + hold passed → Active; the replaced one → Inactive."""
	if not compat.doctype_exists(ACCOUNT) or not compat.has_field(ACCOUNT, "activates_on"):
		return []
	try:
		today = str(frappe.utils.today())[:10]
		done = []
		for row in frappe.db.get_all(
			ACCOUNT,
			filters={"status": PENDING, "prenote_sent": 1, "activates_on": ["<=", today]},
			fields=["name", "employee", "replaces", "approved_by", "prenote_returned"],
			limit=500,
		):
			if not row.get("approved_by") or row.get("prenote_returned"):
				continue
			if row.get("replaces") and frappe.db.get_value(ACCOUNT, row["replaces"], "status") == ACTIVE:
				frappe.db.set_value(ACCOUNT, row["replaces"], "status", INACTIVE)
			frappe.db.set_value(ACCOUNT, row["name"], "status", ACTIVE)
			emp = dict(
				frappe.db.get_value(
					EMPLOYEE,
					row["employee"],
					compat.existing_fields(
						EMPLOYEE, ("name", "personal_email", "company_email", "prefered_email", "user_id")
					),
					as_dict=True,
				)
				or {}
			)
			if emp:
				notice(emp, "made active (your pay now goes to the new account)")
			_audit("direct_deposit:activated", row["name"], f"{row['name']} is now the account paid")
			done.append(row["name"])
		frappe.db.commit()
		return done
	except Exception:  # pragma: no cover - a scheduled job never raises
		try:
			frappe.log_error(title="erpnext_mcp: direct_deposit.activate_due failed")
		except Exception:
			pass
		return []


def changes(limit: int = 50) -> list:
	"""Every change in progress or recently turned down, masked. For the HR read tool."""
	rows = frappe.db.get_all(
		ACCOUNT,
		filters={"status": ["in", [PENDING, REJECTED]]},
		fields=compat.existing_fields(
			ACCOUNT,
			(
				"name",
				"employee",
				"employee_name",
				"bank_name",
				"routing_number",
				"account_number_last_four",
				"account_type",
				"status",
				"verification_state",
				"verification_method",
				"verification_detail",
				"approved_by",
				"prenote_sent",
				"activates_on",
				"submitted_at",
				"allocation_type",
			),
		),
		order_by="modified desc",
		limit=limit,
	)
	out = []
	for r in rows:
		item = masked_row(dict(r))
		item.update(
			{
				"employee": r.get("employee"),
				"employee_name": r.get("employee_name"),
				"verification_detail": r.get("verification_detail"),
				"submitted_at": str(r.get("submitted_at") or "")[:19] or None,
			}
		)
		out.append(item)
	return out
