// SPDX-License-Identifier: MIT
/**
 * A draggable map pin for every asset on the register.
 *
 * v0.32.0 drew a read-only marker here. v0.145.0 added a draggable one in
 * `irrigation_valve_map.js` for the one asset type that clearly needed it, and
 * each script returned early on the other's records. v0.154.0 folds the two
 * together: the pin is draggable on EVERY Asset Register record and this is the
 * only map script the doctype has.
 *
 * WHY THE VALVE ARGUMENT WAS NEVER ACTUALLY ABOUT VALVES. The case made in
 * v0.145.0 was that the coordinate which matters is where the valve IS, not
 * where somebody happened to be standing when they scanned it — a valve
 * recorded from the far side of a block sits on the wrong lateral, and stays
 * wrong until a person corrects it. Every word of that is true of a pump, a
 * wind machine, a bin trailer, a generator and a cold store. A scan records the
 * scanner's position, not the machine's, and "the shop yard" is four acres.
 * Dragging the pin is the check that catches what a GPS fix cannot.
 *
 * NOTHING REACHES THE DATABASE UNTIL THE FORM IS SAVED. A drag calls
 * `frm.set_value`, which sets the visible fields and the dirty flag; the
 * operator sees the numbers change and saves, or navigates away and does not.
 * Editing the fields by hand moves the pin the other way.
 *
 * IT DOES NOT DEFEND AGAINST THE NEXT SCAN. A corrected position is a value in
 * two Float columns like any other, so a later mobile write that carries GPS
 * overwrites it. That was equally true of the valve map and is a property of
 * the columns rather than of this script; the place to fix it, if it ever needs
 * fixing, is a "position confirmed by hand" flag on the doctype that the scan
 * path honours.
 *
 * THE ZONE'S BOUNDARY IS DRAWN UNDERNEATH when the record names one. That is
 * usually a valve, because `irrigation_zone` is usually blank on everything
 * else — but it is a field on Asset Register rather than on a valve, so this
 * asks the record instead of asking the asset type. A pin sitting outside the
 * zone it claims to belong to is not a subtle error once it is on a map, which
 * is the argument `housing_unit_map.js` makes about a cabin outside its parcel.
 *
 * DEFAULT VIEW: with no GPS recorded the map opens over the Yakima Valley at a
 * zoom that shows the orchard belt and says no position has been recorded. The
 * pin is not placed until somebody clicks.
 */

(function () {

	//: Where the map opens when the record has no position. The farm is in the
	//: Columbia Gorge / Yakima Valley corridor; a reader starting here can pan
	//: to any block in a second.
	const DEFAULT_CENTRE = [46.2, -119.2];
	const DEFAULT_ZOOM = 13;

	//: How far a map with a pin zooms in. Seventeen shows about a hundred
	//: metres of ground — enough to see which row a valve sits on, or which
	//: corner of the yard a trailer is in, without losing the block outline.
	const PIN_ZOOM = 17;

	const MAP_HEIGHT = "400px";

	//: The asset type whose form calls the section by its own name. Everything
	//: else is "Asset Location", which is what the section was called before
	//: the two scripts merged.
	const VALVE = "Irrigation Valve";

	frappe.ui.form.on("Asset Register", {
		refresh: function (frm) {
			draw_asset_map(frm);
		},
		gps_latitude: function (frm) {
			move_pin_to_fields(frm);
		},
		gps_longitude: function (frm) {
			move_pin_to_fields(frm);
		},
	});

	/** What this record's map section is called. */
	function section_title(frm) {
		return frm.doc.asset_type === VALVE ? __("Valve Location") : __("Asset Location");
	}

	/** The sentence under a map that has a pin on it. */
	function drag_hint() {
		return __(
			"Drag the pin to correct the position. The latitude and longitude fields update automatically."
		);
	}

	/** The sentence under a map that has none. */
	function place_hint() {
		return __(
			"No GPS position recorded. Click the map to place this asset, or enter coordinates in the fields above."
		);
	}

	/** Redraw the pin from the current field values, if the map is live. */
	function move_pin_to_fields(frm) {
		const state = frm.__erpnext_mcp_asset_map;
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
			// finger on a map can place anything to.
			frm.set_value("gps_latitude", parseFloat(latlng.lat.toFixed(7)));
			frm.set_value("gps_longitude", parseFloat(latlng.lng.toFixed(7)));
		});
	}

	function draw_asset_map(frm) {
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
		// The same section key `geo_map_widget.js` uses, so this and the shared
		// widget can never coexist on one form. `render` has no draggable
		// marker, so the map is built here on the widget's Leaflet loader and
		// base layers rather than through it.
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
				frm.__erpnext_mcp_asset_map = state;

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
					note.textContent = drag_hint();
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
						frappe.utils.escape_html(place_hint()) +
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
						note.textContent = drag_hint();
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

	/** The dashboard section, created once per form. */
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
			frm.dashboard.add_section($(wrapper), section_title(frm));
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
