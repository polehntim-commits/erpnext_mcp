// SPDX-License-Identifier: MIT
/**
 * The Lot Line Adjustment's own map: the two lots, the corridors, and the well.
 *
 * v0.174.0. Until now this form drew nothing and carried a button to
 * `/app/land-map`. The button is still here and still does the tracing — but
 * somebody opening an easement agreement wants to SEE where access runs before
 * deciding whether to go and draw, and a form that answers that question only
 * by sending them to another page answers it badly.
 *
 * ────────────────────────────────────────────────────────────────────────────
 * IT DRAWS. IT DOES NOT TRACE. THAT SPLIT IS DELIBERATE.
 * ────────────────────────────────────────────────────────────────────────────
 *
 * `/app/land-map` owns drawing, and everything that makes a drawing worth
 * anything is there with it: the bearings and distances, the closure error, the
 * draft metes-and-bounds description, the check against every corridor already
 * on file, and the KML/GeoJSON/PDF a surveyor is handed. A second drawing
 * surface on this form would be a second implementation of all of it, and the
 * two would disagree the first time one was changed.
 *
 * So this map is READ-ONLY on purpose, and "Draw an access corridor" is a
 * button that opens the page already pointed at this record. What the map shows
 * is exactly what the record holds — open it, look at it, then go and change it
 * in the one place that knows how to measure.
 *
 * ────────────────────────────────────────────────────────────────────────────
 * THE CORRIDOR IS THE POINT, AND IT IS NOT THE PROPOSED LINE
 * ────────────────────────────────────────────────────────────────────────────
 *
 * `proposed_geometry` is a lot line: it drives the legal description, the
 * before-and-after acreage and the survey packet. `easement_geometry` is a
 * strip of ground somebody may cross. An access corridor written into the first
 * column would make the app compute "acres if giving / acres if receiving" for
 * a driveway and print it into a description as a new boundary. They are drawn
 * differently here for the same reason they are stored differently: red dashed
 * for a proposed line, a wide blue band for a corridor.
 *
 * ────────────────────────────────────────────────────────────────────────────
 * THE MARKER COMES OUT OF PROSE, AND THE PARSER IS DELIBERATELY FUSSY
 * ────────────────────────────────────────────────────────────────────────────
 *
 * A well's position is typed into an easement note as a sentence, so the server
 * reads it out of one. It insists on a hemisphere letter AND a decimal fraction
 * before it will call something a coordinate, because `T1N R13E` is how every
 * lot on this site is described and reading that as 1°N 13°E puts the marker in
 * the Gulf of Guinea. See `_gps_points` in `api/land_map.py` — the refusals are
 * the interesting half.
 */

frappe.ui.form.on("Lot Line Adjustment", {
	refresh(frm) {
		if (frm.is_new()) {
			// A docname is what the map reads by, and what the land map saves
			// onto. Save the draft first.
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

		frm.add_custom_button(
			__("Draw an access corridor"),
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

		render_map(frm);
	},
});

/** The colours, kept together so the legend and the shapes cannot drift apart. */
const COLOURS = {
	lot_1: "#b8860b",
	lot_2: "#2490ef",
	corridor: "#7575ff",
	proposed: "#e24c4c",
};

/**
 * Ask the server for this adjustment's shapes and hand them to the widget.
 *
 * ONE CALL, AND A FAILURE THAT SAYS SO. `adjustment_map` reads the two tax lots
 * by their own links rather than loading the farm, so this costs a row each
 * instead of the whole county. A refusal — no read permission on County Tax
 * Lot, a record that went away — leaves the form alone rather than rendering an
 * empty map that looks like a farm with no ground.
 */
function render_map(frm) {
	if (!window.erpnext_mcp || !erpnext_mcp.geo_map || !erpnext_mcp.geo_map.render) {
		return;
	}
	frappe
		.call({ method: "erpnext_mcp.api.land_map.adjustment_map", args: { name: frm.doc.name } })
		.then((response) => {
			const answer = (response && response.message) || null;
			if (!answer) {
				return;
			}
			const geometries = [];

			(answer.lots || []).forEach((lot) => {
				if (!lot.geometry) {
					return;
				}
				const acres = lot.acres ? __("{0} ac GIS", [String(lot.acres)]) : "";
				geometries.push({
					geometry: lot.geometry,
					colour: lot.side === "Lot 2" ? COLOURS.lot_2 : COLOURS.lot_1,
					fill_opacity: 0.08,
					label: [lot.side, lot.label, lot.owner, acres].filter(Boolean).join(" · "),
				});
			});

			(answer.easements || []).forEach((corridor) => {
				// A corridor is drawn heavier than the ground it crosses. It is
				// the smallest shape on the map and the reason the map is open.
				geometries.push({
					geometry: corridor.geometry,
					colour: COLOURS.corridor,
					fill_opacity: 0.35,
					label: [
						corridor.label,
						corridor.easement_type,
						corridor.burdened ? __("burdens {0}", [corridor.burdened]) : "",
						corridor.benefited ? __("benefits {0}", [corridor.benefited]) : "",
					]
						.filter(Boolean)
						.join(" · "),
				});
			});

			if (answer.proposed_geometry) {
				geometries.push({
					geometry: answer.proposed_geometry,
					colour: COLOURS.proposed,
					fill_opacity: 0.05,
					label: __("Proposed lot line — a draft for a surveyor, not a survey."),
				});
			}

			erpnext_mcp.geo_map.render(frm, {
				title: __("Access and Boundaries"),
				geometries: geometries,
				points: answer.points || [],
			});

			describe(frm, answer);
		})
		.catch((error) => {
			// Never silently. A map that did not load looks exactly like a
			// record with nothing on it, and those need different answers.
			if (window.console && console.error) {
				console.error("lot_line_adjustment_map:", error);
			}
		});
}

/**
 * Say in words what the map shows, including the two things it cannot show.
 *
 * A LOT WITH NO CACHED GEOMETRY IS THE COMMON FAILURE and it is invisible on a
 * map — the other lot draws, the view fits to it, and the result looks like a
 * complete picture of one side. It gets said out loud, with the fix.
 */
function describe(frm, answer) {
	const drawn = (answer.lots || []).filter((lot) => lot.geometry);
	const missing = (answer.lots || []).filter((lot) => !lot.geometry);
	const corridors = (answer.easements || []).length;
	const fixes = (answer.points || []).length;

	const parts = [];
	if (corridors) {
		parts.push(__("{0} access corridor(s) mapped.", [String(corridors)]));
	} else {
		parts.push(
			__("No access corridor is mapped yet. Use Draw an access corridor to trace one.")
		);
	}
	if (fixes) {
		parts.push(__("{0} GPS fix(es) read from the notes.", [String(fixes)]));
	}
	if (drawn.length) {
		parts.push(__("{0} of 2 lots drawn from county GIS.", [String(drawn.length)]));
	}
	frm.dashboard.add_comment(parts.join(" "), corridors ? "blue" : "orange", true);

	missing.forEach((lot) => {
		frm.dashboard.add_comment(
			__("{0} ({1}) has no cached boundary, so it is not on the map. Run a county tax lot lookup to fetch it.", [
				lot.label,
				lot.side,
			]),
			"red",
			true
		);
	});
}
