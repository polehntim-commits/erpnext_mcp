// SPDX-License-Identifier: MIT
/**
 * A draggable map pin for irrigation valves.
 *
 * WHY THIS IS NOT `attach_point`. Asset Register's own map script draws a
 * read-only marker for every asset type, and that is the right answer for a
 * pump, a bin trailer or a generator — a thing somebody scanned, at the
 * coordinates the scan recorded. A valve is different: the coordinate that
 * matters is where the valve IS, not where somebody happened to be standing
 * when they scanned it, and a valve installed at the wrong end of a row
 * stays wrong until a person corrects it.
 *
 * SO THIS MAP IS DRAGGABLE. The pin starts where the record says the valve
 * is. Dragging it updates `gps_latitude` and `gps_longitude` on the form in
 * real-time — the dirty flag is set and the fields are visible, and nothing
 * reaches the database until the form is saved. Editing the fields manually
 * moves the pin the other way.
 *
 * THE ZONE'S BOUNDARY IS DRAWN UNDERNEATH when the valve has one. A valve
 * whose pin sits outside its zone is not a subtle error once it is on a map,
 * and that is the same argument `housing_unit_map.js` makes about a cabin
 * outside its parcel.
 *
 * ONLY IRRIGATION VALVES GET THIS MAP. `asset_register_map.js` skips records
 * whose `asset_type` is "Irrigation Valve" so the two scripts do not fight
 * over the same dashboard section.
 *
 * DEFAULT VIEW: if no GPS is recorded, the map opens over the Yakima Valley
 * (roughly 46.2, -119.2) at a zoom that shows the orchard belt, with a note
 * saying no position has been recorded. The pin is not placed until somebody
 * clicks or drags on the map.
 */

(function () {

	//: Where the map opens when the valve has no recorded position. The farm
	//: is in the Columbia Gorge / Yakima Valley corridor; a reader starting
	//: here can pan to any block in a second.
	const DEFAULT_CENTRE = [46.2, -119.2];
	const DEFAULT_ZOOM = 13;

	//: How far a map with a pin zooms in. Seventeen shows about a hundred
	//: metres of ground — enough to see which row the valve sits on without
	//: losing the block outline.
	const PIN_ZOOM = 17;

	const MAP_HEIGHT = "400px";

	frappe.ui.form.on("Asset Register", {
		refresh: function (frm) {
			if (frm.doc.asset_type !== "Irrigation Valve") {
				return;
			}
			draw_valve_map(frm);
		},
		gps_latitude: function (frm) {
			if (frm.doc.asset_type !== "Irrigation Valve") {
				return;
			}
			move_pin_to_fields(frm);
		},
		gps_longitude: function (frm) {
			if (frm.doc.asset_type !== "Irrigation Valve") {
				return;
			}
			move_pin_to_fields(frm);
		},
	});

	/** Redraw the pin from the current field values, if the map is live. */
	function move_pin_to_fields(frm) {
		const state = frm.__erpnext_mcp_valve_map;
		if (!state || !state.map || !state.L) {
			return;
		}
		const point = erpnext_mcp.geo_map.point_of(
			frm.doc.gps_latitude,
			frm.doc.gps_longitude
		);
		if (!point) {
			// Fields were cleared or set to something invalid. Remove the
			// marker rather than leaving it at the old position — a pin that
			// disagrees with the fields it claims to show is worse than no pin.
			if (state.marker) {
				state.map.removeLayer(state.marker);
				state.marker = null;
			}
			return;
		}
		if (state.marker) {
			state.marker.setLatLng(point);
		} else {
			state.marker = state.L.marker(point, { draggable: true }).addTo(state.map);
			wire_drag(frm, state);
		}
		state.map.panTo(point);
	}

	/** Wire drag-end on a marker so it writes back to the form's fields. */
	function wire_drag(frm, state) {
		state.marker.on("dragend", function () {
			var latlng = state.marker.getLatLng();
			// Seven decimal places matches the field's `precision: "7"` on the
			// doctype, and is about a centimetre — well past what a GPS or a
			// finger on a map can place a valve to.
			frm.set_value("gps_latitude", parseFloat(latlng.lat.toFixed(7)));
			frm.set_value("gps_longitude", parseFloat(latlng.lng.toFixed(7)));
		});
	}

	function draw_valve_map(frm) {
		var point = erpnext_mcp.geo_map.point_of(
			frm.doc.gps_latitude,
			frm.doc.gps_longitude
		);

		// Fetch the zone boundary for context, then build the map. The zone
		// is context and losing it must not lose the map.
		var zone_name = frm.doc.irrigation_zone || "";
		var zone_promise = zone_name
			? erpnext_mcp.geo_map.fetch_boundary(
					"Irrigation Zone",
					zone_name,
					zone_name,
					"#1f6feb"
				)
			: Promise.resolve(null);

		zone_promise.then(function (zone) {
			render_map(frm, point, zone);
		});
	}

	function render_map(frm, point, zone) {
		// Use the same section_for mechanism the widget uses, via render.
		// But since render does not support draggable markers, we build the
		// map ourselves using the shared Leaflet loader and base layers.
		var key = "__erpnext_mcp_map";
		var body = get_or_create_section(frm, key);
		body.innerHTML = "";

		var canvas = document.createElement("div");
		canvas.style.height = MAP_HEIGHT;
		canvas.style.width = "100%";
		canvas.style.borderRadius = "6px";
		body.appendChild(canvas);

		// A note under the map when there is no position yet.
		var note = document.createElement("div");
		note.className = "text-muted";
		note.style.padding = "6px 0";
		body.appendChild(note);

		erpnext_mcp.geo_map.load_leaflet()
			.then(function (L) {
				var map = L.map(canvas, { scrollWheelZoom: false });
				erpnext_mcp.geo_map.add_base_layers(L, map);

				// Draw the zone boundary as faint context underneath.
				if (zone && zone.geometry) {
					L.geoJSON(zone.geometry, {
						style: {
							color: zone.colour || "#1f6feb",
							weight: 2,
							fillOpacity: 0.08,
						},
					})
						.addTo(map)
						.bindPopup(
							frappe.utils.escape_html(zone.label || "")
						);
				}

				var marker = null;
				var state = { map: map, L: L, marker: null };
				frm.__erpnext_mcp_valve_map = state;

				if (point) {
					marker = L.marker(point, { draggable: true }).addTo(map);
					marker.bindPopup(
						frappe.utils.escape_html(
							frm.doc.name + " — " + point[0] + ", " + point[1]
						)
					);
					state.marker = marker;
					wire_drag(frm, state);

					// Fit to the zone if it exists, otherwise zoom to the point.
					if (zone && zone.geometry) {
						try {
							var zone_layer = L.geoJSON(zone.geometry);
							var bounds = zone_layer.getBounds();
							bounds.extend(point);
							map.fitBounds(bounds.pad(0.08), {
								maxZoom: erpnext_mcp.geo_map.MAX_FIT_ZOOM || 18,
							});
						} catch (e) {
							map.setView(point, PIN_ZOOM);
						}
					} else {
						map.setView(point, PIN_ZOOM);
					}
					note.textContent = __(
						"Drag the pin to correct the valve's position. The latitude and longitude fields update automatically."
					);
				} else {
					// No position recorded. Show a default view and let the
					// operator click to place the pin.
					if (zone && zone.geometry) {
						try {
							var zl = L.geoJSON(zone.geometry);
							map.fitBounds(zl.getBounds().pad(0.15), {
								maxZoom: erpnext_mcp.geo_map.MAX_FIT_ZOOM || 18,
							});
						} catch (e) {
							map.setView(DEFAULT_CENTRE, DEFAULT_ZOOM);
						}
					} else {
						map.setView(DEFAULT_CENTRE, DEFAULT_ZOOM);
					}
					note.innerHTML =
						'<span class="indicator orange">' +
						frappe.utils.escape_html(
							__(
								"No GPS position recorded. Click the map to place the valve, or enter coordinates in the fields above."
							)
						) +
						"</span>";

					// One click places the pin and disarms the listener.
					map.once("click", function (e) {
						var latlng = e.latlng;
						marker = L.marker([latlng.lat, latlng.lng], {
							draggable: true,
						}).addTo(map);
						state.marker = marker;
						wire_drag(frm, state);
						frm.set_value(
							"gps_latitude",
							parseFloat(latlng.lat.toFixed(7))
						);
						frm.set_value(
							"gps_longitude",
							parseFloat(latlng.lng.toFixed(7))
						);
						note.textContent = __(
							"Drag the pin to correct the valve's position. The latitude and longitude fields update automatically."
						);
					});
				}

				// The layout timing fix every Leaflet embed needs — see
				// geo_map_widget.js for the explanation.
				setTimeout(function () {
					map.invalidateSize();
				}, 120);
			})
			.catch(function (error) {
				// No Leaflet. Print the coordinates so the record is not lost.
				var lines = [];
				if (point) {
					lines.push(point[0] + ", " + point[1]);
				}
				body.innerHTML =
					'<div class="text-muted" style="padding:8px 0">' +
					"<div>" +
					frappe.utils.escape_html(
						__(
							"The map library could not be loaded ({0}), so the position is printed instead.",
							[(error && error.message) || "unavailable"]
						)
					) +
					"</div>" +
					(lines.length
						? '<div style="margin-top:6px;font-family:monospace">' +
							lines
								.map(function (l) {
									return frappe.utils.escape_html(l);
								})
								.join("<br>") +
							"</div>"
						: "") +
					"</div>";
			});
	}

	/** The dashboard section, created once per form.
	 *
	 * This reuses the same `__erpnext_mcp_map` key as `geo_map_widget.js` so the
	 * two never coexist on the same form — which is the right behaviour, because
	 * `asset_register_map.js` skips valves and this script skips everything else.
	 */
	function get_or_create_section(frm, key) {
		if (frm[key] && frm[key].wrapper && document.body.contains(frm[key].wrapper)) {
			frm[key].body.innerHTML = "";
			show_dashboard(frm);
			return frm[key].body;
		}
		var wrapper = document.createElement("div");
		wrapper.className = "erpnext-mcp-map-section";
		var body = document.createElement("div");
		wrapper.appendChild(body);
		try {
			frm.dashboard.add_section($(wrapper), __("Valve Location"));
		} catch (e) {
			try {
				frm.dashboard.add_section($(wrapper));
			} catch (inner) {
				frm.$wrapper.find(".form-layout").first().prepend(wrapper);
			}
		}
		show_dashboard(frm);
		frm[key] = { wrapper: wrapper, body: body };
		return body;
	}

	function show_dashboard(frm) {
		try {
			if (frm.dashboard && typeof frm.dashboard.show === "function") {
				frm.dashboard.show();
			}
		} catch (e) {
			// A Frappe whose dashboard has no `show`.
		}
	}
})();
