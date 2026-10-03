// SPDX-License-Identifier: MIT
//
// v0.225.0. A worker's self-service direct-deposit change: approve, reject, or
// record that the bank returned the prenote. docs/design/direct_deposit_setup.md.

frappe.ui.form.on("Employee Bank Account", {
	refresh(frm) {
		if (frm.doc.status !== "Pending") return;
		if (!frm.doc.approved_by && ["Verified", "Needs review"].includes(frm.doc.verification_state)) {
			frm.add_custom_button(__("Approve"), () =>
				frappe.confirm(
					frm.doc.verification_state === "Needs review"
						? __("Compare the bank form attached here with the last 4 digits shown before approving. Approve?")
						: __("Approve this direct-deposit change? It is paid only after the prenote and the hold."),
					() => call(frm, "approve", {})
				)
			).addClass("btn-primary");
		}
		frm.add_custom_button(__("Reject"), () =>
			frappe.prompt({ fieldname: "reason", fieldtype: "Data", label: __("Why"), reqd: 1 }, (v) =>
				call(frm, "reject", { reason: v.reason })
			)
		);
		if (frm.doc.prenote_sent) {
			frm.add_custom_button(__("Prenote returned"), () => call(frm, "prenote_returned", {}));
		}
	},
});

function call(frm, method, args) {
	frappe
		.call({ method: `erpnext_mcp.api.direct_deposit.${method}`, args: { account: frm.doc.name, ...args }, freeze: true })
		.then(() => frm.reload_doc());
}
