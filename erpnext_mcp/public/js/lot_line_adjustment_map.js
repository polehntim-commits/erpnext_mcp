// SPDX-License-Identifier: MIT
/**
 * The button that takes a Lot Line Adjustment to the land map, and back again.
 *
 * v0.171.0. `/app/land-map` is where a proposed line is drawn; this adjustment is
 * where the drawing is kept. The two need one door between them, and this is it.
 *
 * IT CARRIES THE DOCNAME THROUGH `frappe.route_options`, which is Frappe's own
 * channel for handing a page a parameter, and the land map deletes the key as
 * soon as it has read it — so a later visit to the page from the sidebar opens
 * unattached rather than silently editing the adjustment somebody looked at an
 * hour ago.
 *
 * IT DRAWS NOTHING ITSELF. The seven map-carrying forms render a map inside the
 * form; this one only routes to the page that does. `geo_map_widget.js` is still
 * listed ahead of it in `doctype_js` — that file is this app's own asset and
 * fetches Leaflet only when something asks it to render, so listing it costs a
 * cached same-origin script and saves the land map a round trip.
 *
 * THE INDICATOR IS THE WHOLE OTHER HALF. An adjustment that already has a
 * proposal reads differently from one that does not, and the difference decides
 * whether somebody opens the map to draw or to check. It is read off the two
 * columns rather than from a flag that could disagree with them.
 */

frappe.ui.form.on("Lot Line Adjustment", {
	refresh(frm) {
		if (frm.is_new()) {
			// A docname is what the map saves onto. Save the draft first.
			return;
		}

		frm.add_custom_button(
			__("Land Map"),
			() => {
				frappe.route_options = { lot_line_adjustment: frm.doc.name };
				frappe.set_route("land-map");
			},
			__("Survey")
		);

		const has_geometry = !!String(frm.doc.proposed_geometry || "").trim();
		const has_description = !!String(frm.doc.generated_legal_description || "").trim();
		if (has_geometry || has_description) {
			frm.dashboard.add_comment(
				has_geometry && has_description
					? __(
							"A proposed boundary and a draft description are attached. Both were drawn on the land map and are a draft for a surveyor, not a survey."
						)
					: has_geometry
						? __("A proposed boundary is attached. Open the land map to generate its description.")
						: __("A draft description is attached with no boundary drawn."),
				"blue",
				true
			);
		}
	},
});
