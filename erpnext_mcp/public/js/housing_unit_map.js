// SPDX-License-Identifier: MIT
/**
 * Where the cabin actually stands — and a pin you can drag there. v0.32.0; draggable from v0.323.0.
 *
 * A camp address is a driveway off a county road and a cabin number is paint on a door; neither puts an ambulance,
 * an inspector or a crew bus at the right building. The parcel and its blocks are drawn underneath over the aerial,
 * so a pin outside its own parcel is obvious, and dragging the pin writes the latitude and longitude into the form
 * (saved with the form; the cabin's Asset Register tag follows, and who moved it and when is stamped on save).
 */

frappe.ui.form.on("Housing Unit", {
	refresh(frm) {
		draw(frm);
	},
	gps_latitude(frm) {
		draw(frm);
	},
	gps_longitude(frm) {
		draw(frm);
	},
});

function draw(frm) {
	if (frm.is_new()) {
		return;
	}
	const point = erpnext_mcp.geo_map.point_of(frm.doc.gps_latitude, frm.doc.gps_longitude);
	const parcel = frm.doc.parcel;
	Promise.all([
		erpnext_mcp.geo_map.fetch_boundary("Parcel", parcel, parcel, "#9a6700"),
		parcel
			? frappe.db.get_list("Field", { filters: { parcel: parcel }, fields: ["name", "field_name", "boundary_geojson"], limit: 200 })
			: Promise.resolve([]),
	]).then(([parcel_entry, fields]) => {
		const geometries = parcel_entry ? [Object.assign(parcel_entry, { fill_opacity: 0.03 })] : [];
		(fields || []).forEach((row) => {
			const geometry = erpnext_mcp.geo_map.parse_geometry(row.boundary_geojson);
			if (geometry) {
				geometries.push({ geometry: geometry, label: row.field_name || row.name, colour: "#1f6feb", fill_opacity: 0.08 });
			}
		});
		erpnext_mcp.geo_map.pin_editor(frm, {
			title: __("Location"),
			point: point,
			geometries: geometries,
			may_move: !!frm.perm && !!frm.perm[0] && !!frm.perm[0].write,
			note: frm.doc.position_set_at
				? __("Pin last set by {0} on {1} ({2}).", [frm.doc.position_set_by || "?", frappe.datetime.str_to_user(frm.doc.position_set_at), frm.doc.position_source || "Desk"])
				: "",
			on_move(lat, lon) {
				frm.set_value("gps_latitude", lat);
				frm.set_value("gps_longitude", lon);
			},
		});
	});
}
