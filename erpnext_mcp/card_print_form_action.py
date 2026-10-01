# SPDX-License-Identifier: MIT
""" "Print ID Card" on the Employee form and "Print Asset Tag" on Asset Register. v0.208.0.

docs/design/card_print_queue.md §4.4. Two Client Script RECORDS, seeded at
migrate, for the reason `badge_form_action` gives: the Employee form is
ERPNext's, and a customisation an operator cannot see or switch off is not one
this app installs. The same three states are kept — absent is written; this
app's own unedited copy is brought up to date; a copy somebody edited is left
alone and reported. Untick `enabled` or delete the row and it is not put back
unless it is deleted (a deleted row is recreated at the next migrate only if no
row carries the marker — so untick, don't delete, to decline it).

The buttons only ASK: `erpnext_mcp.api.card_print.request_card_print` checks the
role, renders the artwork and queues the job. A refusal that needs a reprint
reason opens a small dialog and asks again with the same request id.
"""

from __future__ import annotations

import hashlib

import frappe

CLIENT_SCRIPT = "Client Script"
FORM_VIEW = "Form"
SCRIPT_REVISION = "r1"
REQUEST_METHOD = "erpnext_mcp.api.card_print.request_card_print"
LIST_METHOD = "erpnext_mcp.api.card_print.list_card_print_jobs"

#: (doctype, script name, marker, job type, button label, button group)
TARGETS = (
	(
		"Employee",
		"Employee — Print ID Card",
		"erpnext_mcp:card-print-employee",
		"Employee ID",
		"Print ID Card",
		"Card",
	),
	(
		"Asset Register",
		"Asset Register — Print Asset Tag",
		"erpnext_mcp:card-print-asset",
		"Asset Tag",
		"Print Asset Tag",
		"Tags",
	),
)

#: Earlier texts this app shipped, by fingerprint. Empty at r1.
PRIOR_REVISIONS: dict = {}

SCRIPT_TEMPLATE = """// %(stamp)s
// Added by erpnext_mcp (v0.208.0). Untick `enabled` above to remove the buttons.
//
// "%(label)s" sends this record to the card print queue: the server renders the
// card and the print station's Mac prints it. Nothing is laid out or printed here.

(function () {
	function send(frm, reason) {
		frappe.call({
			method: "%(method)s",
			args: {
				job_type: "%(job_type)s",
				reference_name: frm.doc.name,
				client_request_id: frappe.utils.get_random(12) + "-" + Date.now(),
				reprint_reason: reason || "",
			},
			freeze: true,
			freeze_message: __("Sending to the print queue…"),
		}).then(function (r) {
			const answer = r && r.message;
			if (!answer || !answer.job) {
				return;
			}
			const station = answer.station || {};
			let message = answer.already_queued
				? __("Already in the print queue as {0}.", [answer.job.name])
				: __("Sent to the print queue as {0}.", [answer.job.name]);
			if (station.state && station.state !== "Ready") {
				message += " " + __("The printer is {0} — it will print when it is back.", [station.state]);
			}
			frappe.show_alert({ message: message, indicator: "green" }, 7);
		});
	}

	// A card already printed needs a reason. Asked BEFORE the request, off the
	// queue's own history, so the server's refusal is the backstop and not the UI.
	function request(frm) {
		frappe.call({
			method: "%(list_method)s",
			args: { reference_name: frm.doc.name, status: "Printed", mine_only: 0, limit: 5 },
		}).then(function (r) {
			const jobs = ((r && r.message && r.message.jobs) || []).filter(function (job) {
				return job.job_type === "%(job_type)s";
			});
			if (!jobs.length) {
				send(frm, "");
				return;
			}
			frappe.prompt(
				[
					{
						fieldname: "reason",
						fieldtype: "Select",
						label: __("Why is it being reprinted?"),
						options: ["Lost", "Damaged", "Details changed", "Other"],
						reqd: 1,
					},
					{ fieldname: "detail", fieldtype: "Data", label: __("Detail (for Other)") },
				],
				function (values) {
					send(frm, values.reason === "Other" ? "Other: " + (values.detail || "") : values.reason);
				},
				__("This card was already printed on {0}", [String(jobs[0].printed_at || "").slice(0, 10)]),
				__("Reprint")
			);
		});
	}

	frappe.ui.form.on("%(doctype)s", {
		refresh(frm) {
			if (frm.is_new()) {
				return;
			}
			if (!frappe.user.has_role("Card Print Requester") && !frappe.user.has_role("System Manager")) {
				return;
			}
			frm.add_custom_button(__("%(label)s"), function () { request(frm); }, __("%(group)s"));
			frm.add_custom_button(
				__("Print history"),
				function () {
					frappe.set_route("List", "Card Print Job", {
						job_type: "%(job_type)s",
						reference_name: frm.doc.name,
					});
				},
				__("%(group)s")
			);
		},
	});
})();
"""


def source(doctype: str, marker: str, job_type: str, label: str, group: str) -> str:
	return SCRIPT_TEMPLATE % {
		"stamp": f"{marker}@{SCRIPT_REVISION}",
		"doctype": doctype,
		"method": REQUEST_METHOD,
		"list_method": LIST_METHOD,
		"job_type": job_type,
		"label": label,
		"group": group,
	}


def _fingerprint(text: str) -> str:
	body = str(text or "").replace("\r\n", "\n").strip()
	return hashlib.sha256("\n".join(line.rstrip() for line in body.split("\n")).encode()).hexdigest()


def _existing(doctype: str, marker: str) -> str:
	rows = frappe.db.get_all(
		CLIENT_SCRIPT, filters={"dt": doctype, "view": FORM_VIEW}, fields=["name", "script"], limit=0
	)
	for row in rows:
		if marker in str(row.get("script") or ""):
			return str(row.get("name"))
	return ""


def seed_card_print_form_actions() -> list:
	"""Create or update both buttons. Never raises. One report per target."""
	reports = []
	for doctype, name, marker, job_type, label, group in TARGETS:
		report = {"created": False, "updated": False, "name": name, "doctype": doctype, "reason": ""}
		reports.append(report)
		try:
			if not frappe.db.exists("DocType", CLIENT_SCRIPT) or not frappe.db.exists("DocType", doctype):
				report["reason"] = f"this site has no {doctype} form to put it on"
				continue
			text = source(doctype, marker, job_type, label, group)
			found = _existing(doctype, marker)
			if found:
				report["name"] = found
				stored = str(frappe.db.get_value(CLIENT_SCRIPT, found, "script") or "")
				if f"{marker}@{SCRIPT_REVISION}" in stored:
					report["reason"] = "already present"
					continue
				if _fingerprint(stored) not in PRIOR_REVISIONS:
					report["reason"] = "left alone — this site's copy has been edited"
					continue
				doc = frappe.get_doc(CLIENT_SCRIPT, found)
				doc.script = text
				doc.flags.ignore_permissions = True
				doc.save(ignore_permissions=True)
				report["updated"] = True
				continue
			doc = frappe.get_doc(
				{
					"doctype": CLIENT_SCRIPT,
					"name": name,
					"dt": doctype,
					"view": FORM_VIEW,
					"enabled": 1,
					"script": text,
				}
			)
			doc.flags.ignore_permissions = True
			doc.insert(ignore_permissions=True)
			report["created"] = True
			report["name"] = doc.name
		except Exception as exc:  # pragma: no cover - a site mid-migrate
			report["reason"] = f"{type(exc).__name__}: {exc}"
	return reports


def remove_card_print_form_actions() -> list:
	"""Before uninstall: take away the rows still carrying this app's marker."""
	removed = []
	for doctype, _name, marker, _job, _label, _group in TARGETS:
		try:
			found = _existing(doctype, marker)
			if found:
				frappe.delete_doc(CLIENT_SCRIPT, found, ignore_permissions=True, force=True)
				removed.append(found)
		except Exception:  # pragma: no cover
			continue
	return removed
