// SPDX-License-Identifier: MIT
//
// v0.222.0. Review an office@ reply and send it — only ever on a person's click.
// docs/design/office_reply_drafts.md §3.

frappe.ui.form.on("Office Mail", {
	refresh(frm) {
		const flagged = frm.doc.suspicious || frm.doc.state === "Needs person";
		if (flagged) {
			frm.dashboard.set_headline_alert(
				__("Flagged — no reply is drafted. If it asks to change bank or payment details, verify by phone on the number already on file, never one in the email."),
				"red"
			);
		}
		if (!flagged && ["Drafted", "Edited"].includes(frm.doc.state)) {
			frm.add_custom_button(__("Approve and send"), () => approve(frm)).addClass("btn-primary");
		}
		if (!["Sent", "Approved", "Discarded"].includes(frm.doc.state)) {
			frm.add_custom_button(__("Discard"), () =>
				frappe.prompt({ fieldname: "reason", fieldtype: "Data", label: __("Why") }, (v) =>
					frappe
						.call({ method: "erpnext_mcp.api.office_mail.discard", args: { name: frm.doc.name, reason: v.reason } })
						.then(() => frm.reload_doc())
				)
			);
		}
	},
});

function approve(frm) {
	frappe
		.call({ method: "erpnext_mcp.api.office_mail.available_attachments", args: { name: frm.doc.name } })
		.then((r) => {
			const files = (r && r.message) || [];
			const fields = [
				{ fieldname: "text", fieldtype: "Long Text", label: __("Reply"), default: frm.doc.draft_text, reqd: 1 },
			];
			if (files.length) {
				fields.push({
					fieldname: "attachments",
					fieldtype: "MultiCheck",
					label: __("Attach (each must be ticked)"),
					options: files.map((f) => ({ label: f.file_name, value: f.name })),
				});
			}
			fields.push({
				fieldname: "confirm_financial_details",
				fieldtype: "Check",
				label: __("I have checked every amount, account and bank detail in this reply"),
				depends_on: frm.doc.contains_financial_details ? "" : "eval:0",
			});
			const dialog = new frappe.ui.Dialog({
				title: __("Send this reply to {0}?", [frm.doc.sender]),
				fields,
				primary_action_label: __("Send"),
				primary_action(values) {
					dialog.hide();
					frappe
						.call({
							method: "erpnext_mcp.api.office_mail.approve",
							args: {
								name: frm.doc.name,
								text: values.text,
								attachments: JSON.stringify(values.attachments || []),
								confirm_financial_details: values.confirm_financial_details ? 1 : 0,
							},
							freeze: true,
						})
						.then(() => frm.reload_doc());
				},
			});
			dialog.show();
		});
}
