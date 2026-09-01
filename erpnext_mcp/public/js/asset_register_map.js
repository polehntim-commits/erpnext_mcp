// SPDX-License-Identifier: MIT
/**
 * Where the tagged asset was last recorded as standing.
 *
 * v0.32.0. Asset Register has carried gps_latitude and gps_longitude since
 * v0.17.0 and nothing has ever drawn them. A pump, a bin trailer or a generator
 * is findable by coordinate and by nothing else — "the shop yard" is four acres.
 *
 * IRRIGATION VALVES ARE SKIPPED HERE. `irrigation_valve_map.js` draws a
 * draggable pin for valves, so the operator can correct a position by dragging
 * rather than typing seven decimal places. The two scripts share the same
 * dashboard section key and must not both try to render — the one that applies
 * draws, and the other returns early.
 */

(function () {
	frappe.ui.form.on("Asset Register", {
		refresh: function (frm) {
			if (frm.doc.asset_type === "Irrigation Valve") {
				return;
			}
			var point = erpnext_mcp.geo_map.point_of(
				frm.doc.gps_latitude,
				frm.doc.gps_longitude
			);
			if (!point) {
				return;
			}
			erpnext_mcp.geo_map.render(frm, {
				title: __("Asset Location"),
				point: point,
				point_label: frm.doc.name,
			});
		},
	});
})();
