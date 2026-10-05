// v0.245.0. SOP review and approval (docs/design/sop_review_and_approval.md). Approving is a
// person's act: these buttons, or the phone — there is no MCP approve.
frappe.ui.form.on("Compliance Policy", {
	refresh(frm) {
		if (frm.is_new()) return;
		const call = (method, args) =>
			frappe.call({ method, args: { name: frm.doc.name, ...(args || {}) }, freeze: true, callback: () => frm.reload_doc() });
		if (frm.doc.status === "Draft") {
			frm.add_custom_button(__("Submit for Review"), () => call("erpnext_mcp.sop.desk_submit"));
		}
		if (frm.doc.status === "In Review") {
			const me = (frm.doc.approvers || []).find((r) => r.approver === frappe.session.user && !r.approved_at);
			if (me) {
				frm.add_custom_button(__("Approve"), () =>
					frappe.confirm(__("Approve this SOP version? Your approval is recorded as a signature."), () =>
						call("erpnext_mcp.sop.desk_approve")
					)
				);
				frm.add_custom_button(__("Request Changes"), () =>
					frappe.prompt({ fieldname: "note", fieldtype: "Small Text", label: __("What should change?"), reqd: 1 },
						(values) => call("erpnext_mcp.sop.desk_request_changes", { note: values.note }),
						__("Request Changes"))
				);
			}
		}
	},
});
