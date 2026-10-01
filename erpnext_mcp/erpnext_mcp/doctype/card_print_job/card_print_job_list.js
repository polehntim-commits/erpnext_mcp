// SPDX-License-Identifier: MIT
// Card Print Job list — status colours and Retry on Failed rows. v0.208.0.
frappe.listview_settings["Card Print Job"] = {
	add_fields: ["status", "error", "reference_title"],
	get_indicator(doc) {
		const colours = {
			Queued: "orange",
			Printing: "blue",
			Printed: "green",
			Failed: "red",
			Cancelled: "gray",
			Downloaded: "purple",
		};
		return [__(doc.status), colours[doc.status] || "gray", "status,=," + doc.status];
	},
	button: {
		show(doc) {
			return doc.status === "Failed";
		},
		get_label() {
			return __("Retry");
		},
		get_description(doc) {
			return __("Send {0} back to the queue", [doc.reference_title || doc.name]);
		},
		action(doc) {
			frappe
				.call({ method: "erpnext_mcp.api.card_print.retry_card_print_job", args: { name: doc.name } })
				.then(() => cur_list && cur_list.refresh());
		},
	},
};
