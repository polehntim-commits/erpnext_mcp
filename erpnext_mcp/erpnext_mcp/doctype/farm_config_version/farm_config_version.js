// v0.234.1. Publishing is a person's act (Tim's decision 5): an AI-proposed version is
// published here, in the Desk, never over MCP. See erpnext_mcp/config_lifecycle.py.
frappe.ui.form.on("Farm Config Version", {
	refresh(frm) {
		if (frm.is_new() || !["Draft", "Staged"].includes(frm.doc.status)) return;
		frm.add_custom_button(__("Publish"), () => {
			frappe.prompt(
				[{ fieldname: "change_note", fieldtype: "Small Text", label: __("Why (change note)"), reqd: 1 }],
				(values) => {
					frappe.call({
						method: "erpnext_mcp.config_lifecycle.publish_config_version",
						args: { name: frm.doc.name, change_note: values.change_note },
						freeze: true,
						callback: () => frm.reload_doc(),
					});
				},
				__("Publish {0}", [frm.doc.name]),
				__("Publish")
			);
		});
	},
});
