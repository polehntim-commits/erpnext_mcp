// SPDX-License-Identifier: MIT
/**
 * A Field Pin (entrance, delivery spot, loading area, hive drop, a task's work spot) dragged to the right spot.
 * v0.323.0. The block it belongs to is drawn underneath over the aerial; dragging writes latitude / longitude into
 * the form, and saving stamps who moved it and when.
 */

frappe.ui.form.on("Field Pin", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		const point = erpnext_mcp.geo_map.point_of(frm.doc.latitude, frm.doc.longitude);
		erpnext_mcp.geo_map.fetch_boundary("Field", frm.doc.field, frm.doc.field, "#1f6feb").then((field) => {
			erpnext_mcp.geo_map.pin_editor(frm, {
				title: __("Pin"),
				point: point,
				geometries: field ? [Object.assign(field, { fill_opacity: 0.1 })] : [],
				may_move: !!frm.perm && !!frm.perm[0] && !!frm.perm[0].write,
				note: frm.doc.moved_at
					? __("Last moved by {0} on {1}.", [frm.doc.moved_by || "?", frappe.datetime.str_to_user(frm.doc.moved_at)])
					: "",
				on_move(lat, lon) {
					frm.set_value("latitude", lat);
					frm.set_value("longitude", lon);
				},
			});
		});
	},
});
