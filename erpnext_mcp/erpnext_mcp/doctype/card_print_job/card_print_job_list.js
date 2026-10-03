// SPDX-License-Identifier: MIT
// Card Print Job list — status colours and Retry on Failed rows. v0.208.0.
frappe.listview_settings["Card Print Job"] = {
	add_fields: ["status", "error", "reference_title", "tag_format", "location_label"],
	// v0.226.0. The tag queue grouped by location, and Print all for one location.
	onload(listview) {
		listview.page.add_inner_button(__("Print tags for a location"), () => printTagsForLocation());
	},
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

function printTagsForLocation() {
	frappe.call({ method: "erpnext_mcp.api.card_print.list_tag_print_queue" }).then((r) => {
		const groups = (r.message && r.message.locations) || [];
		if (!groups.length) {
			frappe.msgprint(__("No tags are waiting to print."));
			return;
		}
		const options = groups.map((g) => ({
			label: `${g.location || __("(no location)")} — ${g.count} ${__("tag(s)")}`,
			value: g.location || "",
		}));
		const dialog = new frappe.ui.Dialog({
			title: __("Print tags for a location"),
			fields: [{ fieldname: "location", fieldtype: "Select", label: __("Location"), options, reqd: 1 }],
			primary_action_label: __("Print all"),
			primary_action(values) {
				frappe
					.call({
						method: "erpnext_mcp.api.card_print.print_tags_for_location",
						args: { location: values.location },
					})
					.then((res) => {
						dialog.hide();
						const out = res.message || {};
						if (out.sheet_html) {
							const w = window.open("", "_blank");
							if (w) {
								w.document.open();
								w.document.write(out.sheet_html);
								w.document.close();
							}
						}
						frappe.msgprint(out.note || __("Done."));
						cur_list && cur_list.refresh();
					});
			},
		});
		dialog.show();
	});
}
