// SPDX-License-Identifier: MIT
/**
 * Where the work is: every block (or tag, or parcel) the task covers, and a pin you can drag onto the work.
 *
 * v0.32.0 drew the one place `location_doctype` named. v0.323.0: a task covers one or more places
 * (`task_locations`), so the map draws the union of their outlines over the aerial, with the pin at their combined
 * centre — or at the task's Work Spot when somebody has set one. Dragging the pin sets the Work Spot right away
 * (it is a Field Pin, not a field on this form), records who moved it and when, and it then overrides the centre;
 * "Clear work spot" puts the pin back on the blocks. Foreman, Farm Manager and System Manager may move it.
 *
 * A TASK WITH NO LOCATION GETS NO MAP AND THAT IS CORRECT. A certificate renewal happens at a desk.
 */

frappe.ui.form.on("Farm Task", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		frappe
			.call({ method: "erpnext_mcp.pin_moves.task_map", args: { task: frm.doc.name }, type: "GET" })
			.then((response) => {
				const data = (response && response.message) || {};
				const point = data.map_point && data.map_point.latitude != null
					? [data.map_point.latitude, data.map_point.longitude]
					: null;
				if (!point && !data.outline_geojson) {
					return;
				}
				const names = (data.locations || []).map((row) => row.label || row.location).join(" + ");
				let note = names ? __("Covers {0}.", [names]) : "";
				if (data.work_spot && data.work_spot_moved && data.work_spot_moved.by) {
					note += " " + __("Work spot set by {0}{1}.", [data.work_spot_moved.by,
						data.work_spot_moved.at ? " " + frappe.datetime.str_to_user(data.work_spot_moved.at) : ""]);
				} else if (point) {
					note += " " + __("Pinned at the blocks' centre; drag to set the exact spot.");
				}
				erpnext_mcp.geo_map.pin_editor(frm, {
					title: __("Where the Work Is"),
					point: point,
					geometries: data.outline_geojson
						? [{ geometry: data.outline_geojson, label: names, colour: "#1f6feb", fill_opacity: 0.12 }]
						: [],
					may_move: !!data.may_move,
					note: note,
					on_move(lat, lon) {
						frappe
							.call({
								method: "erpnext_mcp.pin_moves.set_task_work_spot",
								args: { task: frm.doc.name, latitude: lat, longitude: lon },
							})
							.then(() => {
								frappe.show_alert({ message: __("Work spot set."), indicator: "green" });
								frm.reload_doc();
							});
					},
				});
				if (data.work_spot && data.may_move) {
					frm.add_custom_button(__("Clear work spot"), () => {
						frappe
							.call({
								method: "erpnext_mcp.pin_moves.set_task_work_spot",
								args: { task: frm.doc.name, clear: 1 },
							})
							.then(() => frm.reload_doc());
					}, __("Map"));
				}
			})
			.catch(() => {});
	},
});
