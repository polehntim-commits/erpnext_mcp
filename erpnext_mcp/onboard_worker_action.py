# SPDX-License-Identifier: MIT
""" "Onboard Worker" on the Employee form: a phone enrolled from where the person is. v0.175.0.

The office already had two places to enrol a phone — `/app/mobile-onboarding`
and an MCP tool — and neither was where somebody is standing on a first
morning, which is on that worker's Employee record. This puts the enrolment on
that form, under Actions, as one button and one dialog.

────────────────────────────────────────────────────────────────────────────
WHAT THE DIALOG SHOWS, AND WHAT IT NEVER KEEPS
────────────────────────────────────────────────────────────────────────────

A QR carrying the server URL and a ONE-TIME ENROLMENT TOKEN — no credential —
with a countdown to the end of the window (24 hours by default). The phone
spends the token at `/farmops/api/mobile/enroll_device` and receives its own
device credential; see `device_enrollment`. The dialog polls, and the moment the
phone has enrolled the QR is taken off the screen and replaced with the
device's name.

THE QR IS DOM-ONLY. It arrives as a `data:` URI, lives in one `<img>` inside the
dialog, and is removed when the dialog closes. No File row, no attachment, no
Governance Document — there is nothing to find in the sidebar afterwards, which
is the point: a code that enrols a phone should not outlive the conversation it
was shown in. (The older login card could be archived because it WAS the
credential; this one is worth one scan and is dead after it.)

────────────────────────────────────────────────────────────────────────────
AN EMPLOYEE WITH NO ACCOUNT YET
────────────────────────────────────────────────────────────────────────────

The dialog asks for the three facts an account needs — the email, the mobile
role, and the entity (defaulting to the Employee's own company) — and makes the
account with `create_mobile_user(generate_token=false)`, then links it to the
Employee. It mints NO credential there: the only credential this dialog ever
causes to exist is the one the phone receives in the exchange.

────────────────────────────────────────────────────────────────────────────
PERMISSIONS
────────────────────────────────────────────────────────────────────────────

The same rule `/app/mobile-onboarding` keeps, from Frappe's own permission
tables: opening a window needs WRITE on Mobile Access Grant; making the account
first needs CREATE on Mobile Access Grant AND on User, so this can never produce
an account the caller could not have made on the User form by hand.

A CLIENT SCRIPT, FOR THE REASON `badge_form_action.py` GIVES: Employee is
ERPNext's doctype, so an operator can see this row, switch it off or delete it,
and this app will not put it back.
"""

from __future__ import annotations

import base64
import hashlib
import json

import frappe

from . import device_enrollment, mobile_onboarding, roles
from .errors import ToolError
from .render import qr

CLIENT_SCRIPT = "Client Script"
EMPLOYEE = "Employee"
GRANT = "Mobile Access Grant"
USER = "User"
FORM_VIEW = "Form"

SCRIPT_NAME = "Employee — Onboard Worker"
SCRIPT_MARKER = "erpnext_mcp:onboard-worker-button"
SCRIPT_REVISION = "r1"
SCRIPT_STAMP = f"{SCRIPT_MARKER}@{SCRIPT_REVISION}"

#: Earlier texts this app shipped, by fingerprint. None yet.
PRIOR_REVISIONS: dict = {}

CONTEXT_METHOD = "erpnext_mcp.onboard_worker_action.employee_enrollment_context"
START_METHOD = "erpnext_mcp.onboard_worker_action.start_employee_enrollment"
STATUS_METHOD = "erpnext_mcp.onboard_worker_action.employee_enrollment_status"

#: How often the dialog asks whether the phone has scanned yet, in milliseconds.
POLL_MS = 4000

TITLE = "Onboard Worker"

SCRIPT_SOURCE = """// %(stamp)s
// Added by erpnext_mcp (v0.175.0). Untick `enabled` above, or delete this row,
// to remove the button — the app will not put it back.
//
// Actions > Onboard Worker shows a ONE-TIME enrolment QR for this worker's phone.
// The QR carries the server address and a single-use token, never a credential,
// and it lives only in this dialog: nothing is saved as a file.

(function () {
	function esc(value) {
		return frappe.utils.escape_html(value == null ? "" : String(value));
	}

	function clock(seconds) {
		seconds = Math.max(0, Math.floor(seconds));
		var h = Math.floor(seconds / 3600);
		var m = Math.floor((seconds %% 3600) / 60);
		var s = seconds %% 60;
		var pad = function (n) { return (n < 10 ? "0" : "") + n; };
		return (h ? h + ":" + pad(m) : m) + ":" + pad(s);
	}

	function show_qr(frm, dialog, answer) {
		var timers = [];
		var stop = function () {
			timers.forEach(function (t) { clearInterval(t); });
			timers = [];
		};
		var body = dialog.fields_dict.qr.$wrapper;

		// THE QR EXISTS IN THIS ONE ELEMENT AND NOWHERE ELSE. Emptied on close,
		// on expiry and on enrolment — never written to a file or the sidebar.
		body.html(
			'<div style="text-align:center">' +
				'<div class="text-muted small">' + esc(answer.full_name || answer.user) + " &middot; " +
				esc(answer.user) + "</div>" +
				'<img class="ow-qr" alt="" style="width:260px;height:260px;margin:12px auto;display:block;image-rendering:pixelated" src="data:image/png;base64,' +
				esc(answer.png_base64) + '">' +
				'<div style="font-weight:600">' + __("Open Farm Ops on the phone and scan this.") + "</div>" +
				'<div class="ow-left" style="font-size:1.4em;margin-top:6px"></div>' +
				'<div class="text-muted small" style="margin-top:8px">' +
				__("Works once, for one phone. It carries no password — the phone receives its own credential when it scans.") +
				"</div></div>"
		);
		answer.png_base64 = null;

		var left = Number(answer.seconds_remaining || 0);
		var tick = function () {
			var $left = body.find(".ow-left");
			if (left <= 0) {
				stop();
				body.find(".ow-qr").remove();
				$left.html('<span class="text-danger">' + __("This code has expired. Close and open Onboard Worker again.") + "</span>");
				return;
			}
			$left.text(__("Valid for {0}", [clock(left)]));
			left -= 1;
		};
		tick();
		timers.push(setInterval(tick, 1000));

		timers.push(setInterval(function () {
			frappe.call({
				method: "%(status)s",
				args: { employee: frm.doc.name, device: answer.device },
				callback: function (r) {
					var state = (r && r.message) || {};
					if (state.status === "Enrolled") {
						stop();
						body.html(
							'<div style="text-align:center;padding:24px 0">' +
								'<div style="font-size:2.2em">&#10003;</div>' +
								'<div style="font-weight:600">' + __("Enrolled") + ": " + esc(state.device_name) + "</div>" +
								'<div class="text-muted small">' + esc(state.enrolled_at) + "</div></div>"
						);
						frm.reload_doc();
					} else if (state.status === "Revoked") {
						stop();
						body.html('<div class="text-danger" style="text-align:center;padding:24px 0">' +
							__("This code was closed — a newer one was opened, or it was revoked.") + "</div>");
					}
				},
			});
		}, %(poll)s));

		dialog.onhide = function () {
			stop();
			body.empty();
		};
		dialog.set_primary_action(__("Done"), function () { dialog.hide(); });
	}

	function open_dialog(frm, context) {
		var fields = [];
		if (context.blockers && context.blockers.length) {
			fields.push({
				fieldtype: "HTML",
				fieldname: "blockers",
				options: '<div class="alert alert-warning">' +
					context.blockers.map(function (b) { return esc(b.message) + (b.fix ? "<br><small>" + esc(b.fix) + "</small>" : ""); }).join("<hr>") +
					"</div>",
			});
		}
		if (!context.has_account) {
			fields.push(
				{ fieldtype: "HTML", fieldname: "why", options: '<p class="text-muted">' +
					__("{0} has no Farm Ops account yet. These three facts make one; no password is created.", [esc(context.employee_name)]) + "</p>" },
				{ fieldtype: "Data", fieldname: "email", label: __("Email (the login)"), options: "Email", reqd: 1, default: context.suggested_email },
				{ fieldtype: "Select", fieldname: "role", label: __("Mobile role"), reqd: 1,
					options: (context.roles || []).join("\\n"), default: context.default_role },
				{ fieldtype: "Link", fieldname: "company", label: __("Entity"), options: "Company", reqd: 1, default: context.company }
			);
		} else {
			fields.push({ fieldtype: "HTML", fieldname: "who", options: '<p class="text-muted">' +
				__("Account {0} ({1}).", [esc(context.user), esc(context.role || "")]) +
				(context.live_devices ? " " + __("{0} phone(s) already enrolled; this adds another.", [context.live_devices]) : "") + "</p>" });
		}
		fields.push(
			{ fieldtype: "Data", fieldname: "device_name", label: __("Phone name (optional)"), description: __("e.g. Ana's iPhone") },
			{ fieldtype: "Int", fieldname: "expiry_hours", label: __("Valid for (hours)"), default: context.default_hours },
			{ fieldtype: "HTML", fieldname: "qr" }
		);

		var dialog = new frappe.ui.Dialog({
			title: __("Onboard Worker"),
			fields: fields,
			primary_action_label: __("Show enrolment QR"),
			primary_action: function (values) {
				frappe.call({
					method: "%(start)s",
					args: {
						employee: frm.doc.name,
						email: values.email,
						role: values.role,
						company: values.company,
						device_name: values.device_name,
						expiry_hours: values.expiry_hours,
					},
					freeze: true,
					freeze_message: __("Opening the enrolment window..."),
					callback: function (r) {
						if (!r || !r.message) { return; }
						["email", "role", "company", "device_name", "expiry_hours"].forEach(function (f) {
							if (dialog.fields_dict[f]) { dialog.set_df_property(f, "hidden", 1); }
						});
						["why", "who", "blockers"].forEach(function (f) {
							if (dialog.fields_dict[f]) { dialog.fields_dict[f].$wrapper.empty(); }
						});
						show_qr(frm, dialog, r.message);
					},
				});
			},
		});
		if (!context.can_enrol) {
			dialog.get_primary_btn().prop("disabled", true);
		}
		dialog.show();
	}

	frappe.ui.form.on("%(doctype)s", {
		refresh: function (frm) {
			frm.add_custom_button(
				__("Onboard Worker"),
				function () {
					if (frm.is_new()) {
						frappe.msgprint(__("Save this employee before enrolling their phone."));
						return;
					}
					frappe.call({
						method: "%(context)s",
						args: { employee: frm.doc.name },
						callback: function (r) {
							if (r && r.message) { open_dialog(frm, r.message); }
						},
					});
				},
				__("Actions")
			);
		},
	});
})();
""" % {
	"stamp": SCRIPT_STAMP,
	"doctype": EMPLOYEE,
	"context": CONTEXT_METHOD,
	"start": START_METHOD,
	"status": STATUS_METHOD,
	"poll": POLL_MS,
}


# ── the whitelisted methods ─────────────────────────────────────────────────
def _throw(message: str) -> None:
	frappe.throw(message, title=frappe._(TITLE))


def _employee(employee: str) -> dict:
	if not employee or not frappe.db.exists(EMPLOYEE, employee):
		_throw(frappe._("Employee {0} was not found.").format(employee))
	if not mobile_onboarding._may(EMPLOYEE, "read"):
		_throw(frappe._("You cannot read Employee records."))
	row = frappe.db.get_value(
		EMPLOYEE,
		employee,
		["name", "employee_name", "company", "user_id", "company_email", "personal_email", "prefered_email"],
		as_dict=True,
	)
	return dict(row or {})


def _grant_state(user: str) -> str:
	if not user or not frappe.db.exists(GRANT, user):
		return ""
	return str(frappe.db.get_value(GRANT, user, "state") or "")


def _blockers() -> list:
	"""What would stop the phone reaching this site at all. Checked before, not after."""
	from .tools import mobile

	return mobile_onboarding.enrolment_blockers(mobile._endpoint_url({}), qr.available())


@frappe.whitelist()
def employee_enrollment_context(employee=None) -> dict:
	"""What the dialog needs to draw itself. Reads only."""
	emp = _employee(employee)
	user = str(emp.get("user_id") or "").strip()
	state = _grant_state(user)
	has_account = bool(user and state == "Active")
	may_write = mobile_onboarding._may(GRANT, "write")
	may_create = mobile_onboarding._may(GRANT, "create") and mobile_onboarding._may(USER, "create")
	blockers = _blockers()
	suggested = user or str(
		emp.get("prefered_email") or emp.get("company_email") or emp.get("personal_email") or ""
	)
	return {
		"employee": emp.get("name"),
		"employee_name": emp.get("employee_name"),
		"company": emp.get("company"),
		"user": user or None,
		"grant_state": state or None,
		"role": frappe.db.get_value(GRANT, user, "mobile_role") if state else None,
		"has_account": has_account,
		"suggested_email": suggested,
		"roles": list(roles.ROLE_NAMES),
		"default_role": "Field Worker" if "Field Worker" in roles.ROLE_NAMES else roles.ROLE_NAMES[0],
		"live_devices": len(device_enrollment.live_devices(user)) if has_account else 0,
		"default_hours": device_enrollment.DEFAULT_ENROLLMENT_HOURS,
		"max_hours": device_enrollment.MAX_ENROLLMENT_HOURS,
		"blockers": blockers,
		"can_enrol": (not blockers) and (may_write if has_account else may_create),
	}


def _ensure_account(emp: dict, email, role, company) -> str:
	"""The Employee's mobile account, made if it does not exist. Mints NO credential."""
	from .tools import mobile

	user = str(emp.get("user_id") or "").strip()
	if user and _grant_state(user) == "Active":
		if not mobile_onboarding._may(GRANT, "write"):
			_throw(frappe._("Enrolling a phone needs write permission on Mobile Access Grant."))
		return user

	if not (mobile_onboarding._may(GRANT, "create") and mobile_onboarding._may(USER, "create")):
		_throw(
			frappe._(
				"Making this worker's account needs create permission on Mobile Access Grant and on User."
			)
		)
	email = str(email or user or "").strip().lower()
	if not email:
		_throw(frappe._("Give the email this worker will sign in as."))
	if user and email != user.lower():
		_throw(
			frappe._(
				"This Employee is already linked to the login {0}. Enrol that account, or change the "
				"Employee's User ID first."
			).format(user)
		)
	try:
		mobile.create_mobile_user(
			{
				"email": email,
				"full_name": emp.get("employee_name"),
				"role": role or "Field Worker",
				"entity_access": [company or emp.get("company")],
				"generate_token": False,
				"update_existing": bool(frappe.db.exists(USER, email)),
			}
		)
	except ToolError as exc:
		_throw(str(exc))
	if not emp.get("user_id"):
		frappe.db.set_value(EMPLOYEE, emp["name"], "user_id", email)
	return email


@frappe.whitelist(methods=["POST"])
def start_employee_enrollment(
	employee=None, email=None, role=None, company=None, device_name=None, expiry_hours=None
) -> dict:
	"""Open a one-time enrolment window and return its QR for the dialog. DOM-only."""
	from .tools import mobile

	emp = _employee(employee)
	blockers = _blockers()
	if blockers:
		_throw(" ".join(b["message"] for b in blockers) + " " + frappe._("No account was created."))

	user = _ensure_account(emp, email, role, company)
	hours = mobile_onboarding._hours(expiry_hours, device_enrollment.DEFAULT_ENROLLMENT_HOURS)
	try:
		opened = device_enrollment.open_enrollment(
			user,
			device_name=device_name or "",
			hours=hours,
			issued_by=str(frappe.session.user or ""),
		)
	except device_enrollment.EnrollmentRefused as exc:
		_throw(str(exc))

	payload = device_enrollment.enroll_payload(mobile._endpoint_url({}), opened["token"])
	drawn = qr.render(json.dumps(payload, separators=(",", ":"), sort_keys=True), error="M")
	seconds = frappe.utils.time_diff_in_seconds(opened["expires_at"], frappe.utils.now())
	return {
		"user": user,
		"full_name": emp.get("employee_name"),
		"device": opened["device"],
		"expires_at": opened["expires_at"],
		"seconds_remaining": max(0, int(seconds)),
		"superseded": opened["superseded"],
		# The token is inside this image and nowhere else in the response.
		"png_base64": base64.b64encode(drawn["png"]).decode("ascii"),
	}


@frappe.whitelist()
def employee_enrollment_status(employee=None, device=None) -> dict:
	"""Has the phone scanned yet? One device's status, for the dialog's poll."""
	emp = _employee(employee)
	if not mobile_onboarding._may(GRANT, "read"):
		_throw(frappe._("You cannot read Mobile Access Grants."))
	entry = device_enrollment.status_of(str(emp.get("user_id") or ""), str(device or ""))
	return {
		"status": entry.get("status"),
		"device_name": entry.get("device_name"),
		"enrolled_at": entry.get("enrolled_at"),
		"window_expired": entry.get("window_expired"),
	}


# ── the Client Script row ───────────────────────────────────────────────────
def _fingerprint(text: str) -> str:
	"""`badge_form_action._fingerprint`, spelled out here for the reason it gives."""
	body = str(text or "").replace("\r\n", "\n").strip()
	lines = [line.rstrip() for line in body.split("\n")]
	return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _existing() -> str:
	rows = frappe.db.get_all(
		CLIENT_SCRIPT,
		filters={"dt": EMPLOYEE, "view": FORM_VIEW},
		fields=["name", "script"],
		limit=0,
	)
	for row in rows:
		if SCRIPT_MARKER in str(row.get("script") or ""):
			return str(row.get("name"))
	return ""


def seed_onboard_worker_action() -> dict:
	"""Create the Employee form button, or bring this app's own copy up to date.

	The three states `badge_form_action.seed_badge_form_action` keeps: absent is
	written, this app's unedited copy is updated, an operator's edit is left
	alone and reported. Never raises.
	"""
	report = {
		"created": False,
		"updated": False,
		"name": SCRIPT_NAME,
		"revision": SCRIPT_REVISION,
		"reason": "",
	}
	try:
		if not frappe.db.exists("DocType", CLIENT_SCRIPT):  # pragma: no cover - not a real Frappe
			report["reason"] = "this site has no Client Script doctype"
			return report
		if not frappe.db.exists("DocType", EMPLOYEE):
			report["reason"] = "this site has no Employee doctype — HR is not installed"
			return report

		found = _existing()
		if found:
			report["name"] = found
			stored = str(frappe.db.get_value(CLIENT_SCRIPT, found, "script") or "")
			if SCRIPT_STAMP in stored:
				report["reason"] = "already present"
				return report
			shipped = PRIOR_REVISIONS.get(_fingerprint(stored))
			if not shipped:
				report["reason"] = (
					"left alone — this site's copy has been edited, so it is not this app's "
					f"to rewrite. It is missing revision {SCRIPT_REVISION}"
				)
				return report
			doc = frappe.get_doc(CLIENT_SCRIPT, found)
			doc.script = SCRIPT_SOURCE
			doc.flags.ignore_permissions = True
			doc.save(ignore_permissions=True)
			report["updated"] = True
			report["reason"] = f"updated from {shipped.split(' —')[0]} to {SCRIPT_REVISION}"
			return report

		doc = frappe.get_doc(
			{
				"doctype": CLIENT_SCRIPT,
				"name": SCRIPT_NAME,
				"dt": EMPLOYEE,
				"view": FORM_VIEW,
				"enabled": 1,
				"script": SCRIPT_SOURCE,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		report["created"] = True
		report["name"] = doc.name
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		report["reason"] = f"{type(exc).__name__}: {exc}"
	return report


def remove_onboard_worker_action() -> dict:
	"""Take this app's button off the Employee form before the app goes. Never raises."""
	report = {"removed": False, "name": SCRIPT_NAME, "reason": ""}
	try:
		if not frappe.db.exists("DocType", CLIENT_SCRIPT):  # pragma: no cover - not a real Frappe
			report["reason"] = "this site has no Client Script doctype"
			return report
		found = _existing()
		if not found:
			report["reason"] = "not present"
			return report
		report["name"] = found
		frappe.delete_doc(CLIENT_SCRIPT, found, ignore_permissions=True, force=True)
		report["removed"] = True
	except Exception as exc:  # pragma: no cover - a site mid-uninstall
		report["reason"] = f"{type(exc).__name__}: {exc}"
	return report


__all__ = (
	"CONTEXT_METHOD",
	"SCRIPT_MARKER",
	"SCRIPT_NAME",
	"SCRIPT_REVISION",
	"SCRIPT_SOURCE",
	"SCRIPT_STAMP",
	"START_METHOD",
	"STATUS_METHOD",
	"employee_enrollment_context",
	"employee_enrollment_status",
	"remove_onboard_worker_action",
	"seed_onboard_worker_action",
	"start_employee_enrollment",
)
