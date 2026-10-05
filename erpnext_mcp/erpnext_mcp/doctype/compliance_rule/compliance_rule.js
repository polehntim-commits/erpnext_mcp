// v0.234.1. Approving (turning on) a rule is a person's act (Tim's decision 5): an
// AI-proposed rule is approved here, in the Desk, never over MCP.
frappe.ui.form.on("Compliance Rule", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.enabled || frm.doc.superseded_by) return;
		frm.add_custom_button(__("Approve"), () => {
			const ai = frm.doc.authored_by === "AI-proposed";
			frappe.confirm(
				ai
					? __("This rule was proposed by AI. Approving it turns it on and records you as the approver. Continue?")
					: __("Approve and turn on this rule?"),
				() =>
					frappe.call({
						method: "erpnext_mcp.config_lifecycle.approve_rule",
						args: { name: frm.doc.name, accept_ai_authored_code: ai ? 1 : 0 },
						freeze: true,
						callback: () => frm.reload_doc(),
					})
			);
		});
	},
});
