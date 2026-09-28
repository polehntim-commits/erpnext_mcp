// SPDX-License-Identifier: MIT
// App Feedback — Approve proposal / Reject proposal. v0.206.0.
//
// Shown only on a note with a proposal, to a System Manager or Farm Manager.
// The server checks the role, the switch and the triage state again
// (`tools/moments.approve` / `reject`); hiding the buttons is courtesy, not
// the control. Approving runs each proposed MCP call through the normal
// dispatcher, then replies to the worker and resolves the note.
frappe.ui.form.on("App Feedback", {
	refresh(frm) {
		const manager = frappe.user.has_role("System Manager") || frappe.user.has_role("Farm Manager");
		const state = frm.doc.triage_state;
		if (!manager || !["Proposed", "Approved", "Ticketed"].includes(state)) {
			return;
		}
		if (state !== "Ticketed") {
			frm.add_custom_button(__("Approve proposal"), () => {
				frappe.prompt(
					{ fieldname: "note", fieldtype: "Small Text", label: __("Note to the worker (optional)") },
					(values) => {
						frm.call("approve_triage_proposal", { note: values.note || "" }).then((r) => {
							frm.reload_doc();
							const result = r.message || {};
							frappe.show_alert({
								message: result.ok
									? __("Applied and resolved.")
									: __("Stopped: {0}", [result.message || ""]),
								indicator: result.ok ? "green" : "orange",
							});
						});
					},
					__("Apply this proposal?"),
					__("Approve and apply")
				);
			}, __("Triage"));
		}
		frm.add_custom_button(__("Reject proposal"), () => {
			frappe.prompt(
				{ fieldname: "reason", fieldtype: "Small Text", label: __("Why"), reqd: 1 },
				(values) => {
					frm.call("reject_triage_proposal", { reason: values.reason }).then(() => frm.reload_doc());
				},
				__("Reject this proposal?"),
				__("Reject")
			);
		}, __("Triage"));
	},
});
