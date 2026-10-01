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
	// v0.210.0: cards are printed by hand. A waiting row opens its PDF; a Failed one retries.
	button: {
		show(doc) {
			return ["Queued", "Downloaded", "Failed"].includes(doc.status);
		},
		get_label(doc) {
			return doc.status === "Failed" ? __("Retry") : __("Print");
		},
		get_description(doc) {
			return doc.status === "Failed"
				? __("Send {0} back to the queue", [doc.reference_title || doc.name])
				: __("Open {0} to print it and mark it Printed", [doc.reference_title || doc.name]);
		},
		action(doc) {
			if (doc.status !== "Failed") {
				frappe.set_route("Form", "Card Print Job", doc.name);
				return;
			}
			frappe
				.call({ method: "erpnext_mcp.api.card_print.retry_card_print_job", args: { name: doc.name } })
				.then(() => cur_list && cur_list.refresh());
		},
	},
};
