// SPDX-License-Identifier: MIT
// v0.263.0. "View in IPM Map" on the IPM Organism and IPM Relationship forms: opens /app/ipm-map on
// the organism (or the relationship's subject), with its side panel open.
(function () {
	function open_map(organism, crop) {
		frappe.route_options = { organism, crop: crop || undefined };
		frappe.set_route("ipm-map");
	}
	frappe.ui.form.on("IPM Organism", {
		refresh(frm) {
			if (frm.is_new()) return;
			const crop = ["Crop", "Variety"].includes(frm.doc.kind) ? frm.doc.name : undefined;
			frm.add_custom_button(__("View in IPM Map"), () => open_map(frm.doc.name, crop));
		},
	});
	frappe.ui.form.on("IPM Relationship", {
		refresh(frm) {
			if (frm.is_new()) return;
			frm.add_custom_button(__("View in IPM Map"), () => open_map(frm.doc.subject, frm.doc.crop));
		},
	});
})();
