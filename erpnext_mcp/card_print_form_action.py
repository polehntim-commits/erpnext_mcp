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
SCRIPT_REVISION = "r2"
REQUEST_METHOD = "erpnext_mcp.api.card_print.request_card_print"
PREVIEW_METHOD = "erpnext_mcp.api.card_print.preview_card"
DOWNLOAD_METHOD = "erpnext_mcp.api.card_print.download_card_pdf"

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

#: Earlier texts this app shipped, by fingerprint — how the seeder tells its own
#: unedited copy (updated) from one an operator changed (left alone).
PRIOR_REVISIONS: dict = {
	"47fd72ca4d605dfde779294ff63d064f67bfbdb14494f7e90ca9936bfeec60df": "r1 — v0.208.0, Employee: queued directly, no preview or download",
	"644854e4961095a08d9ce99e5404085ccc6371f73075aa520082e6fbfd991129": "r1 — v0.208.0, Asset Register: queued directly, no preview or download",
}

SCRIPT_TEMPLATE = """// %(stamp)s
// Added by erpnext_mcp (v0.209.0). Untick `enabled` above to remove the buttons.
//
// "%(label)s" opens one dialog: a preview of the card (drawn by the server), then
// either SEND TO PRINTER (the print queue; the station's Mac prints it) or
// DOWNLOAD CARD PDF (one card-sized PDF, front and back, to print from Preview
// with Paper Size CR80, 100%%, no fit). Nothing here uses Frappe's print dialog.

(function () {
	function request_id() {
		return frappe.utils.get_random(12) + "-" + Date.now();
	}

	function reason_then(preview, run) {
		if (!preview.already_printed_on) {
			run("");
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
				run(values.reason === "Other" ? "Other: " + (values.detail || "") : values.reason);
			},
			__("This card was already printed on {0}", [preview.already_printed_on]),
			__("Continue")
		);
	}

	function send(frm, preview, dialog) {
		reason_then(preview, function (reason) {
			frappe.call({
				method: "%(method)s",
				args: {
					job_type: "%(job_type)s",
					reference_name: frm.doc.name,
					client_request_id: request_id(),
					reprint_reason: reason,
				},
				freeze: true,
				freeze_message: __("Sending to the print queue…"),
			}).then(function (r) {
				const answer = r && r.message;
				if (!answer || !answer.job) {
					return;
				}
				dialog.hide();
				const station = answer.station || {};
				let message = answer.already_queued
					? __("Already in the print queue as {0}.", [answer.job.name])
					: __("Sent to the print queue as {0}.", [answer.job.name]);
				if (station.state && station.state !== "Ready") {
					message += " " + __("The printer is {0} — it will print when it is back.", [station.state]);
				}
				if (answer.job.pages === "Front" && answer.job.sides === "Dual") {
					message += " " + __("This printer is single-sided: use Print back on the job after the front prints.");
				}
				frappe.show_alert({ message: message, indicator: "green" }, 9);
			});
		});
	}

	function download(frm, preview, dialog) {
		reason_then(preview, function (reason) {
			frappe.call({
				method: "%(download_method)s",
				args: { job_type: "%(job_type)s", reference_name: frm.doc.name, reprint_reason: reason },
				freeze: true,
				freeze_message: __("Drawing the card…"),
			}).then(function (r) {
				const answer = r && r.message;
				if (!answer || !answer.file_url) {
					return;
				}
				dialog.hide();
				window.open(answer.file_url, "_blank");
				frappe.msgprint({
					title: __("Card PDF ready ({0})", [answer.job.name]),
					indicator: "green",
					message: __(
						"Print it from Preview: Paper Size <b>CR80 / ISO 7810</b>, Scale <b>100%%</b>, no Scale to Fit, Auto Rotate <b>off</b>. Page 1 is the front and page 2 the back."
					),
				});
			});
		});
	}

	function open_dialog(frm) {
		frappe.call({
			method: "%(preview_method)s",
			args: { job_type: "%(job_type)s", reference_name: frm.doc.name },
			freeze: true,
		}).then(function (r) {
			const preview = r && r.message;
			if (!preview) {
				return;
			}
			const station = preview.station;
			const online = !!preview.agent_online;
			let line = __("No print station is checking in — download the PDF and print it by hand.");
			if (station && online) {
				line = __("Print station {0}: {1}.", [station.station, station.state]) +
					(station.message ? " " + frappe.utils.escape_html(station.message) : "");
			} else if (station) {
				line = __("Print station {0} is offline — download the PDF, or queue it to print when the station is back.", [station.station]);
			}
			const warnings = (preview.warnings || [])
				.map(function (w) { return "<li>" + frappe.utils.escape_html(w) + "</li>"; })
				.join("");
			const frame = "border:1px solid #d1d8dd;border-radius:6px;background:#fff;line-height:0;box-shadow:0 1px 3px rgba(0,0,0,.08)";
			const dialog = new frappe.ui.Dialog({
				title: __("%(label)s — {0}", [preview.title]),
				size: "large",
				fields: [
					{
						fieldtype: "HTML",
						fieldname: "card",
						options:
							'<div style="display:flex;gap:16px;justify-content:center;align-items:flex-start;flex-wrap:wrap;padding:8px 0">' +
							'<div><div class="text-muted small">' + __("Front") + '</div><div style="' + frame + '">' + preview.front_svg + "</div></div>" +
							'<div><div class="text-muted small">' + __("Back") + '</div><div style="' + frame + '">' + preview.back_svg + "</div></div>" +
							"</div>" +
							'<p class="small" style="text-align:center">' + line + "</p>" +
							(warnings ? '<ul class="small text-warning">' + warnings + "</ul>" : ""),
					},
				],
				primary_action_label: online ? __("Send to printer") : __("Download card PDF"),
				primary_action: function () {
					(online ? send : download)(frm, preview, dialog);
				},
				secondary_action_label: online ? __("Download card PDF") : __("Queue for the printer"),
				secondary_action: function () {
					(online ? download : send)(frm, preview, dialog);
				},
			});
			dialog.show();
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
			frm.add_custom_button(__("%(label)s"), function () { open_dialog(frm); }, __("%(group)s"));
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
		"preview_method": PREVIEW_METHOD,
		"download_method": DOWNLOAD_METHOD,
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
