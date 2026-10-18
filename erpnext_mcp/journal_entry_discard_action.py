# SPDX-License-Identifier: MIT
"""Discard / Restore / Purge on the Journal Entry list and form, as records and not as hooks. v0.334.0.

Journal Entry is ERPNext's, so — exactly as `badge_list_action.py` argues for the
Employee list — the Desk side of discarding is two Client Script rows, not a
`doctype_js` / `doctype_list_js` hook and not a `doc_events` hook: visible in the
Desk, switchable off (untick `enabled`), never re-created once deleted, updated in
place only while still this app's own unedited text, and removed by
`before_uninstall`.

THE LIST: discarded drafts hidden by default (a `discarded = 0` filter, which the
"Show / hide discarded" button toggles), a grey "Discarded" indicator, "Discard"
in the Actions menu for ticked rows, and — for System Manager / Accounts Manager —
"Purge discarded" (count and total, then a confirm) and a per-row "Purge".

THE FORM: "Discard" on a draft; on a discarded one, the reason as a headline,
"Restore", "Purge", and a refusal on Submit. That refusal is client-side, because
this app installs no document hooks; the MCP submit tools refuse server-side.

ERPNext's own `frappe.listview_settings["Journal Entry"]` (its indicators) is kept
and chained, never replaced.
"""

from __future__ import annotations

import hashlib

import frappe

CLIENT_SCRIPT = "Client Script"
DOCTYPE = "Journal Entry"
METHOD = "erpnext_mcp.api.discard"

LIST_MARKER = "erpnext_mcp:je-discard-list"
FORM_MARKER = "erpnext_mcp:je-discard-form"
REVISION = "r1"

#: Earlier shipped texts by fingerprint, per marker — none yet.
PRIOR_REVISIONS: dict = {LIST_MARKER: {}, FORM_MARKER: {}}

_HELPERS = """
	const DT = "Journal Entry";
	const METHOD = "%(method)s";
	const can_purge = () =>
		frappe.user.has_role("System Manager") || frappe.user.has_role("Accounts Manager");

	function report(message) {
		const r = message || {};
		const refused = (r.results || []).filter((row) => !row.ok);
		let text = __("{0} discarded.", [r.discarded || 0]);
		if (refused.length) {
			text += "<br><br>" + __("Not discarded:") + "<ul>" +
				refused.map((row) => "<li><b>" + frappe.utils.escape_html(row.name) + "</b>: " +
					frappe.utils.escape_html(row.error || "") + "</li>").join("") + "</ul>";
		}
		frappe.msgprint({ title: __("Discard"), message: text, indicator: refused.length ? "orange" : "green" });
	}

	function discard_dialog(names, done) {
		if (!names || !names.length) {
			frappe.msgprint(__("Tick the drafts to discard."));
			return;
		}
		frappe.prompt(
			[
				{ fieldname: "reason", fieldtype: "Small Text", label: __("Why"), reqd: 1 },
				{ fieldname: "duplicate_of", fieldtype: "Link", options: DT, label: __("Duplicate of (optional)") },
			],
			(values) =>
				frappe
					.call({ method: METHOD + ".discard", args: { doctype: DT, names: names, reason: values.reason,
						duplicate_of: values.duplicate_of || "" }, freeze: true })
					.then((r) => { report(r.message); done && done(); }),
			__("Discard {0} draft(s)", [names.length]),
			__("Discard")
		);
	}

	function purge_dialog(names, done) {
		const go = (count, total) =>
			frappe.confirm(
				__("Delete {0} discarded draft(s), {1} in total, for good? Each is written to the Discarded Draft Log first. This cannot be undone.",
					[count, format_currency(total)]),
				() =>
					frappe
						.call({ method: METHOD + ".purge", args: { doctype: DT, names: names || null }, freeze: true })
						.then((r) => {
							const m = r.message || {};
							frappe.msgprint(__("Purged {0} ({1}); {2} could not be.", [m.purged || 0,
								format_currency(m.total || 0), m.failed || 0]));
							done && done();
						})
			);
		if (names && names.length) {
			go(names.length, 0);
			return;
		}
		frappe.call({ method: METHOD + ".summary", args: { doctype: DT } }).then((r) => {
			const m = r.message || {};
			if (!m.count) {
				frappe.msgprint(__("There are no discarded drafts."));
				return;
			}
			go(m.count, m.total);
		});
	}
"""

LIST_SOURCE = (
	"""// %(stamp)s
// Added by erpnext_mcp (v0.334.0). Untick `enabled` above, or delete this row, to remove Discard /
// Purge from the Journal Entry list — the app will not put it back. ERPNext's own list settings
// (its indicators) are kept and chained, not replaced.

(function () {"""
	+ _HELPERS
	+ """
	const settings = frappe.listview_settings[DT] || {};
	const previous_onload = settings.onload;
	const previous_indicator = settings.get_indicator;
	const HIDE = [DT, "discarded", "=", 0];

	settings.add_fields = (settings.add_fields || []).concat(["discarded", "discard_reason", "total_debit"]);

	settings.get_indicator = function (doc) {
		if (doc.docstatus === 0 && cint(doc.discarded)) {
			return [__("Discarded"), "gray", "discarded,=,1"];
		}
		return previous_indicator ? previous_indicator(doc) : undefined;
	};

	settings.onload = function (listview) {
		if (typeof previous_onload === "function") {
			previous_onload(listview);
		}
		// Hidden by default: a discarded draft is set aside, not waiting to be posted.
		if (!listview.filter_area.exists(HIDE)) {
			listview.filter_area.add([HIDE]);
		}
		listview.page.add_inner_button(__("Show / hide discarded"), function () {
			if (listview.filter_area.exists(HIDE)) {
				listview.filter_area.remove("discarded");
			} else {
				listview.filter_area.add([HIDE]);
			}
		});
		listview.page.add_actions_menu_item(
			__("Discard"),
			() => discard_dialog(listview.get_checked_items(true), () => listview.refresh()),
			false
		);
		if (can_purge()) {
			listview.page.add_inner_button(__("Purge discarded"), () =>
				purge_dialog(null, () => listview.refresh())
			);
		}
	};

	if (!settings.button) {
		settings.button = {
			show: (doc) => doc.docstatus === 0 && cint(doc.discarded) && can_purge(),
			get_label: () => __("Purge"),
			get_description: (doc) => __("Delete discarded draft {0} for good", [doc.name]),
			action: (doc) => purge_dialog([doc.name], () => cur_list && cur_list.refresh()),
		};
	}

	frappe.listview_settings[DT] = settings;
})();
"""
) % {"stamp": f"{LIST_MARKER}@{REVISION}", "method": METHOD}

FORM_SOURCE = (
	"""// %(stamp)s
// Added by erpnext_mcp (v0.334.0). Untick `enabled` above, or delete this row, to remove Discard /
// Restore / Purge from the Journal Entry form — the app will not put it back.

(function () {"""
	+ _HELPERS
	+ """
	frappe.ui.form.on(DT, {
		refresh(frm) {
			if (frm.is_new() || frm.doc.docstatus !== 0) {
				return;
			}
			if (cint(frm.doc.discarded)) {
				const dup = frm.doc.duplicate_of ? " " + __("Duplicate of {0}.", [frm.doc.duplicate_of]) : "";
				frm.dashboard.set_headline_alert(
					__("Discarded by {0} on {1}: {2}.", [frm.doc.discarded_by || "?",
						frappe.datetime.str_to_user(frm.doc.discarded_at || ""),
						frappe.utils.escape_html(frm.doc.discard_reason || "")]) + dup +
						" " + __("It will not be posted or counted. Restore it to use it again."),
					"orange"
				);
				frm.add_custom_button(__("Restore"), () =>
					frappe
						.call({ method: METHOD + ".restore", args: { doctype: DT, name: frm.doc.name }, freeze: true })
						.then(() => frm.reload_doc())
				);
				if (can_purge()) {
					frm.add_custom_button(__("Purge"), () =>
						purge_dialog([frm.doc.name], () => frappe.set_route("List", DT))
					);
				}
			} else {
				frm.add_custom_button(__("Discard"), () => discard_dialog([frm.doc.name], () => frm.reload_doc()));
			}
		},
		before_submit(frm) {
			if (cint(frm.doc.discarded)) {
				frappe.throw(__("This draft is discarded. Restore it before submitting."));
			}
		},
	});
})();
"""
) % {"stamp": f"{FORM_MARKER}@{REVISION}", "method": METHOD}

SCRIPTS = (
	{"marker": LIST_MARKER, "view": "List", "name": "Journal Entry — Discard (list)", "source": LIST_SOURCE},
	{"marker": FORM_MARKER, "view": "Form", "name": "Journal Entry — Discard (form)", "source": FORM_SOURCE},
)


def _fingerprint(text: str) -> str:
	body = str(text or "").replace("\r\n", "\n").strip()
	return hashlib.sha256("\n".join(line.rstrip() for line in body.split("\n")).encode("utf-8")).hexdigest()


def _existing(marker: str, view: str) -> str:
	for row in frappe.db.get_all(
		CLIENT_SCRIPT, filters={"dt": DOCTYPE, "view": view}, fields=["name", "script"], limit_page_length=0
	):
		if marker in str(row.get("script") or ""):
			return str(row.get("name"))
	return ""


def seed() -> list:
	"""Create both scripts, or update this app's own unedited copies. Never raises."""
	reports = []
	for spec in SCRIPTS:
		report = {"name": spec["name"], "created": False, "updated": False, "reason": ""}
		try:
			if not frappe.db.exists("DocType", CLIENT_SCRIPT) or not frappe.db.exists("DocType", DOCTYPE):
				report["reason"] = "no Client Script or Journal Entry doctype"
				reports.append(report)
				continue
			stamp = f"{spec['marker']}@{REVISION}"
			found = _existing(spec["marker"], spec["view"])
			if found:
				report["name"] = found
				stored = str(frappe.db.get_value(CLIENT_SCRIPT, found, "script") or "")
				if stamp in stored:
					report["reason"] = "already present"
				elif _fingerprint(stored) in PRIOR_REVISIONS[spec["marker"]]:
					doc = frappe.get_doc(CLIENT_SCRIPT, found)
					doc.script = spec["source"]
					doc.save(ignore_permissions=True)
					report.update(updated=True, reason=f"updated to {REVISION}")
				else:
					report["reason"] = f"left alone — edited on this site; missing {REVISION}"
				reports.append(report)
				continue
			doc = frappe.get_doc(
				{
					"doctype": CLIENT_SCRIPT,
					"name": spec["name"],
					"dt": DOCTYPE,
					"view": spec["view"],
					"enabled": 1,
					"script": spec["source"],
				}
			)
			doc.insert(ignore_permissions=True)
			report.update(created=True, name=doc.name)
		except Exception as exc:  # pragma: no cover - a site mid-migrate
			report["reason"] = f"{type(exc).__name__}: {exc}"
		reports.append(report)
	return reports


def remove() -> list:
	"""Delete both scripts (only rows still carrying the marker). Called from `before_uninstall`."""
	out = []
	for spec in SCRIPTS:
		try:
			found = _existing(spec["marker"], spec["view"])
			if found:
				frappe.delete_doc(CLIENT_SCRIPT, found, ignore_permissions=True, force=True)
				out.append(found)
		except Exception:  # pragma: no cover
			pass
	return out
