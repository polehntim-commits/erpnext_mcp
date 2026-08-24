// SPDX-License-Identifier: MIT
/**
 * A Leaflet map on a Desk form, for every doctype in this app that knows where it is.
 *
 * v0.32.0. WHY A MAP IS NOT DECORATION HERE. This app has been storing polygons
 * since v0.12.0 and coordinates since v0.17.0, and until now the only way to see
 * one was to read a Long Text field full of numbers. A boundary nobody can look
 * at is a boundary nobody checks, and the failure that produces is specific: a
 * block traced with two vertices transposed passes every validation this app
 * makes — it is a valid polygon, it is on Earth, its area is plausible — and it
 * is obviously wrong to anybody who sees it drawn. The map is the check that
 * catches the mistakes arithmetic cannot.
 *
 * ONE WIDGET, SEVEN FORMS. Everything below is shared; each doctype's own script
 * says only where its geometry lives. That is deliberate: seven copies of a
 * Leaflet bootstrap is seven places for the CDN URL, the tile attribution and
 * the zoom defaults to drift apart, and the first symptom of the drift is one
 * form that mysteriously has no map.
 *
 * IT IS LOADED FROM A CDN AND IT DEGRADES. Leaflet is not vendored into this app
 * and the tiles are fetched from Esri and OpenStreetMap, so a bench with no
 * outbound internet — which is a perfectly reasonable way to run a farm's
 * server — gets no map. That case is HANDLED RATHER THAN LEFT TO FAIL: the
 * section renders a sentence saying the library could not be reached, and it
 * prints the coordinates in plain text underneath. The record is the coordinates;
 * the map is a reading of them, and losing the reading must not look like losing
 * the record.
 *
 * ──────────────────────────────────────────────────────────────────────────
 * v0.33.0: THE MAP CAN NOW BE DRAWN ON, AND v0.32.0'S REFUSAL STILL HOLDS
 * ──────────────────────────────────────────────────────────────────────────
 *
 * What this file said a release ago was "nothing here writes… a map that could
 * nudge a vertex would be a way to change all of that by accident, with no
 * validation and no audit row." Every word of that is still true, and it is
 * satisfied rather than abandoned.
 *
 * The thing being refused was a map that WROTE TO THE FIELD. Nothing here does.
 * The draw tools produce a polygon; the polygon is sent to
 * `erpnext_mcp.api.gis.save_boundary`; that calls `set_parcel_boundary`,
 * `set_field_boundary` or `set_zone_boundary` — the same three tools the AI
 * calls, unchanged. They parse the shape, refuse a self-intersection, compare
 * the enclosed area against the recorded acreage and REFUSE a disagreement past
 * a quarter, report what now falls outside, recompute every derived field, and
 * leave a row. A vertex nudged by accident does not get saved quietly; it gets
 * an area disagreement and a refusal, on screen, before anything changes.
 *
 * SO THE MAP IS NOT A WAY ROUND THE BOUNDARY TOOLS. It is a second caller of
 * them, and the only one whose caller can see the shape they are about to
 * commit. `api/gis.py` argues the whole thing, including why the permission
 * check had to be rebuilt there.
 *
 * ONLY THREE FORMS ARE EDITABLE — Parcel, Field, Irrigation Zone — because only
 * those three carry a polygon. Housing Unit, Asset Register, Farm Shift and Farm
 * Task use `attach_point` and stay exactly as read-only as they were.
 *
 * NO AREA IS COMPUTED IN THIS FILE, and that is deliberate rather than lazy.
 * Leaflet.draw offers a live acreage readout while you drag; taking it would put
 * a second area implementation in the app, in a different language, and the day
 * it disagreed with `geo.area_acres` by three percent nobody would know which
 * number the compliance record was built on. The server answers with the area it
 * actually stored, after the save, and that is the only figure shown.
 *
 * SATELLITE IS THE DEFAULT LAYER FROM v0.33.0, because a street map cannot be
 * traced against. An orchard block's corner is a change in canopy, a headland or
 * a road edge — all of them visible in imagery and none of them on a street map,
 * which for most of this county draws two roads and a lot of white. OSM is still
 * one click away in the layer control, and it is the better layer for finding a
 * cabin by its driveway.
 *
 * THE TILE ATTRIBUTIONS ARE THE CONDITION OF USE, NOT A COURTESY, for both
 * providers. Esri's World Imagery and OSM's tile server are free and ask to be
 * credited; the strings are in the layer options below and must stay there.
 *
 * ──────────────────────────────────────────────────────────────────────────
 * v0.34.0: THE MAP ARRIVES BEFORE THE RECORD DOES, AND FAILURES ARE AUDIBLE
 * ──────────────────────────────────────────────────────────────────────────
 *
 * TWO THINGS CHANGE, AND ONE OF THEM IS A REVERSAL.
 *
 * The first: a form that has never been saved now gets the map and the draw
 * tools, and Save Boundary on it creates the record and then sets the boundary,
 * in one press. v0.33.0 withheld the map from a new record because the server
 * needs a docname and the boundary tools need the acreage out of the database —
 * both still true, and both now handled in `save()` rather than in front of the
 * operator. The order of a parcel being created is: open the form, look at the
 * imagery, trace the block, fill in the fields, press once.
 *
 * The second: every `frappe.call` in this file now has a `.catch`. It did not,
 * and the shape of that bug was the worst available — a rejected promise nobody
 * listened to, so an expired session or a dropped connection lifted the freeze,
 * re-enabled the button, left the drawing exactly where it was and said nothing
 * whatsoever. A boundary that silently did not save looks identical to one that
 * did, and the operator finds out weeks later from a report. See
 * `explain_failure`, including why it stays quiet when the server has already
 * spoken for itself.
 */

frappe.provide("erpnext_mcp.geo_map");

(function () {
	if (erpnext_mcp.geo_map.render) {
		// Already installed by another doctype's form on this page. Every
		// `doctype_js` entry pulls this file in, so on a session that opens a
		// Field and then a Parcel it arrives twice.
		return;
	}

	const LEAFLET_VERSION = "1.9.4";
	const LEAFLET_JS = `https://cdnjs.cloudflare.com/ajax/libs/leaflet/${LEAFLET_VERSION}/leaflet.js`;
	const LEAFLET_CSS = `https://cdnjs.cloudflare.com/ajax/libs/leaflet/${LEAFLET_VERSION}/leaflet.css`;

	//: The draw plugin, loaded ONLY on the three forms that can be drawn on and
	//: only when one of them is opened. It is 130 KB of JavaScript and a marker
	//: sprite that a Housing Unit form has no use for.
	const DRAW_VERSION = "1.0.4";
	const DRAW_JS = `https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/${DRAW_VERSION}/leaflet.draw.js`;
	const DRAW_CSS = `https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/${DRAW_VERSION}/leaflet.draw.css`;

	//: Esri's World Imagery, which is free, needs no key, and is the layer a
	//: boundary is actually traced against. `{y}` BEFORE `{x}` — this is an
	//: ArcGIS tile path (`/tile/{z}/{row}/{col}`), not the XYZ order every other
	//: tile URL in the world uses, and getting it round the wrong way produces a
	//: map of somewhere else entirely rather than an error.
	const SATELLITE_URL =
		"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
	const SATELLITE_ATTRIBUTION =
		"Imagery &copy; Esri — Esri, Maxar, Earthstar Geographics, and the GIS User Community";

	const STREET_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
	const STREET_ATTRIBUTION =
		'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

	//: How far in a map opens on a single point. Seventeen is about a hundred
	//: metres across, which shows a cabin and the road to it — a point zoomed to
	//: nineteen shows a roof and no context, and a reader cannot tell one cabin
	//: from the next by its roof.
	const POINT_ZOOM = 17;

	//: The most a boundary is zoomed to when its shape is tiny. Without this a
	//: single-acre block fills the frame at street level and the reader cannot see
	//: which block it is next to.
	const MAX_FIT_ZOOM = 18;

	//: Where an editable map opens when the record has NO shape and nothing above
	//: it has one either — which is the ordinary state of a parcel somebody is
	//: about to trace for the first time.
	//:
	//: A HARDCODED PLACE, AND SAID SO. The honest alternatives are a world view at
	//: zoom 2, where the operator pans for a minute before they can start, or a
	//: geocode of the parcel's address, which is a call to somebody's paid service
	//: to guess at a farm road. This app was built for tree fruit in the Columbia
	//: Gorge and its only county GIS integration is Wasco County's; opening over
	//: The Dalles is a default that is right most of the time and costs one drag
	//: when it is wrong. `attach_editable_boundary` takes `home` to override it.
	const HOME_VIEW = { centre: [45.6, -121.18], zoom: 12 };

	const MAP_HEIGHT = "360px";
	const EDITABLE_MAP_HEIGHT = "480px";

	//: How long to wait for the CDN before giving up and printing the coordinates.
	//: Eight seconds is past any working connection and short enough that a bench
	//: with no internet does not look hung.
	const LOAD_TIMEOUT_MS = 8000;

	const SAVE_METHOD = "erpnext_mcp.api.gis.save_boundary";
	const COUNTY_METHOD = "erpnext_mcp.api.gis.query_county_parcels";

	//: THE COLOURS MEAN A STATE, NOT A DOCTYPE. v0.32.0 gave each form's own
	//: shape its own colour, which was fine when every shape was equally fixed.
	//: With three layers that can be on one map at once — the container above,
	//: the shape being edited, and a county polygon on approval — what a reader
	//: needs to tell apart is which one they are about to change. So: purple and
	//: faint is context and cannot be touched, red is the editable shape, and
	//: purple dashed is a proposal that has not been applied to anything yet.
	const DRAWN_STYLE = { color: "#cf222e", weight: 3, fillOpacity: 0.18 };
	const PREVIEW_STYLE = { color: "#8250df", weight: 3, dashArray: "6 4", fillOpacity: 0.12 };

	let loading = null;
	let loading_draw = null;

	/** One <script> tag, resolved when it runs. Rejects on error or timeout. */
	function load_script(source) {
		return new Promise((resolve, reject) => {
			const script = document.createElement("script");
			script.src = source;
			script.async = true;
			const timer = setTimeout(() => reject(new Error("timed out")), LOAD_TIMEOUT_MS);
			script.onload = () => {
				clearTimeout(timer);
				resolve();
			};
			script.onerror = () => {
				clearTimeout(timer);
				reject(new Error("could not be fetched"));
			};
			document.head.appendChild(script);
		});
	}

	function load_stylesheet(href) {
		if (document.querySelector(`link[href="${href}"]`)) {
			return;
		}
		const link = document.createElement("link");
		link.rel = "stylesheet";
		link.href = href;
		document.head.appendChild(link);
	}

	/** Leaflet, loaded once per page. Resolves with `L`, or rejects. */
	function load_leaflet() {
		if (window.L && window.L.map) {
			return Promise.resolve(window.L);
		}
		if (loading) {
			return loading;
		}
		loading = new Promise((resolve, reject) => {
			load_stylesheet(LEAFLET_CSS);
			load_script(LEAFLET_JS).then(
				() =>
					window.L && window.L.map
						? resolve(window.L)
						: reject(new Error("loaded but empty")),
				reject
			);
		});
		return loading;
	}

	/** Leaflet.draw on top of Leaflet, loaded once per page. Resolves with `L`.
	 *
	 * THE CSS IS LOADED FROM HERE AND NOT FROM `app_include_css`, which would put
	 * it on every page of the Desk for every user, including the several hundred
	 * who will never open a Parcel. `hooks.py` is a file this app keeps almost
	 * empty on purpose; a stylesheet loaded where it is needed costs one request
	 * on three forms instead of one on all of them.
	 */
	function load_draw() {
		return load_leaflet().then((L) => {
			if (L.Control && L.Control.Draw) {
				return L;
			}
			if (!loading_draw) {
				loading_draw = new Promise((resolve, reject) => {
					load_stylesheet(DRAW_CSS);
					load_script(DRAW_JS).then(
						() =>
							window.L && window.L.Control && window.L.Control.Draw
								? resolve(window.L)
								: reject(new Error("the draw plugin loaded but empty")),
						reject
					);
				});
			}
			return loading_draw;
		});
	}

	/** A GeoJSON geometry from a stored Long Text field, or null. */
	function parse_geometry(raw) {
		if (!raw || !String(raw).trim()) {
			return null;
		}
		try {
			const parsed = typeof raw === "object" ? raw : JSON.parse(raw);
			// Accept whatever the boundary tools accept, for the same reason they
			// do: a caller exporting from QGIS gets whichever of the three shapes
			// the export button produced.
			if (parsed && parsed.type === "FeatureCollection") {
				return (parsed.features || []).length === 1 ? parsed.features[0].geometry : parsed;
			}
			if (parsed && parsed.type === "Feature") {
				return parsed.geometry;
			}
			return parsed;
		} catch (error) {
			return null;
		}
	}

	/** `[lat, lon]` when both are real coordinates, else null.
	 *
	 * NULL ISLAND IS REFUSED. An unset Float pair reads as [0, 0], which is a
	 * real place in the Gulf of Guinea — and a map that flies there looks exactly
	 * like a map showing you where something is.
	 */
	function point_of(lat, lon) {
		const latitude = parseFloat(lat);
		const longitude = parseFloat(lon);
		if (!isFinite(latitude) || !isFinite(longitude)) {
			return null;
		}
		if (!latitude && !longitude) {
			return null;
		}
		if (latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) {
			return null;
		}
		return [latitude, longitude];
	}

	/** "45.52,-122.68" or "Latitude 45.52, longitude -122.68" → `[lat, lon]`.
	 *
	 * The two spellings Farm Shift and Farm Task Assignment both accept, since
	 * v0.19.1. A place name comes back null and the caller says so — resolving it
	 * would mean geocoding, which is a network call to somebody's paid service to
	 * guess at a field name a farm made up.
	 */
	function parse_gps_string(raw) {
		const text = String(raw || "").trim();
		if (!text) {
			return null;
		}
		const numbers = text.match(/-?\d+(\.\d+)?/g);
		if (!numbers || numbers.length < 2) {
			return null;
		}
		return point_of(numbers[0], numbers[1]);
	}

	/** The colour a breadcrumb is drawn in, from its place in the day.
	 *
	 * Blue at the start of the shift through to red at the end. A single colour
	 * would draw a track that says where the crew went and not which way round
	 * they went, and the direction is half of what a reader is asking.
	 */
	function track_colour(fraction) {
		const hue = 220 - 220 * Math.max(0, Math.min(1, fraction));
		return `hsl(${Math.round(hue)}, 75%, 45%)`;
	}

	function escape_html(value) {
		return frappe.utils && frappe.utils.escape_html
			? frappe.utils.escape_html(String(value == null ? "" : value))
			: String(value == null ? "" : value).replace(/[&<>"']/g, (character) => {
					return {
						"&": "&amp;",
						"<": "&lt;",
						">": "&gt;",
						'"': "&quot;",
						"'": "&#39;",
					}[character];
				});
	}

	/** The section this map lives in, created once per form.
	 *
	 * `frm.dashboard.add_section` rather than an HTML field on the doctype: a
	 * field would mean editing seven shipped DocType JSONs to hold something that
	 * is presentation and not data, and it would put an empty grey box on every
	 * form of every unmapped record. A section is added only when there is
	 * something to draw.
	 */
	function section_for(frm, title) {
		const key = "__erpnext_mcp_map";
		if (frm[key] && frm[key].wrapper && document.body.contains(frm[key].wrapper)) {
			frm[key].body.innerHTML = "";
			show_dashboard(frm);
			return frm[key].body;
		}
		const wrapper = document.createElement("div");
		wrapper.className = "erpnext-mcp-map-section";
		const body = document.createElement("div");
		wrapper.appendChild(body);
		try {
			frm.dashboard.add_section($(wrapper), __(title || "Map"));
		} catch (error) {
			// A Frappe version whose dashboard does not take a label, or does not
			// have add_section at all. The map is worth more than the heading.
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

	/** Make sure the dashboard the section was just added to is not hidden.
	 *
	 * `.empty-section` is `display: none !important` in the Desk's own stylesheet
	 * and Frappe puts it on the WHOLE dashboard container — not on one section —
	 * whenever a form's dashboard has nothing in it. `add_section` does not take
	 * it off again, and `dashboard.reset()` does not either, so a container hidden
	 * while some other record was on screen stays hidden with the map inside it.
	 *
	 * THAT IS NOT HYPOTHETICAL FROM v0.34.0. Frappe keeps ONE form object per
	 * doctype and reuses it for every record of that doctype, including the new
	 * one — so the state left behind by the record viewed a moment ago is the
	 * state a blank form starts in, and "the map does not appear when I press New,
	 * but only sometimes" is the shape that bug would take.
	 */
	function show_dashboard(frm) {
		try {
			if (frm.dashboard && typeof frm.dashboard.show === "function") {
				frm.dashboard.show();
			}
		} catch (error) {
			// A Frappe whose dashboard has no `show`. The section is already in the
			// page; this only decides whether the container admits to holding it.
		}
	}

	/** What to show when there is no Leaflet: the numbers, in plain text. */
	function render_fallback(body, reason, spec) {
		const lines = [];
		if (spec.point) {
			lines.push(`${spec.point[0]}, ${spec.point[1]}`);
		}
		(spec.geometries || []).forEach((entry) => {
			if (entry.centroid) {
				lines.push(`${entry.label || "centroid"}: ${entry.centroid[0]}, ${entry.centroid[1]}`);
			}
		});
		if (spec.editable && spec.editable.centroid) {
			lines.push(
				`${spec.editable.docname || "centroid"}: ${spec.editable.centroid[0]}, ${
					spec.editable.centroid[1]
				}`
			);
		}
		if (spec.track && spec.track.length) {
			lines.push(
				__("{0} location fix(es) recorded, from {1} to {2}", [
					spec.track.length,
					spec.track[0].timestamp || "?",
					spec.track[spec.track.length - 1].timestamp || "?",
				])
			);
		}
		const editing = spec.editable
			? `<div style="margin-top:6px">${escape_html(
					__(
						"Drawing a boundary needs the map, so it is unavailable here. The boundary tools still work from the MCP surface, and the field can be edited directly."
					)
				)}</div>`
			: "";
		body.innerHTML = `
			<div class="text-muted" style="padding:8px 0">
				<div>${escape_html(
					__("The map library could not be loaded ({0}), so the position is printed instead.", [
						reason,
					])
				)}</div>
				<div style="margin-top:6px;font-family:monospace">${lines
					.map((line) => escape_html(line))
					.join("<br>")}</div>
				${editing}
			</div>`;
	}

	/** The two base layers and the control that swaps them.
	 *
	 * Satellite is added to the map and is therefore the one that shows; both are
	 * named in the control. See the module docstring for why imagery and not
	 * streets is the default.
	 */
	function add_base_layers(L, map) {
		const satellite = L.tileLayer(SATELLITE_URL, {
			attribution: SATELLITE_ATTRIBUTION,
			maxZoom: 19,
		});
		const streets = L.tileLayer(STREET_URL, { attribution: STREET_ATTRIBUTION, maxZoom: 19 });
		satellite.addTo(map);
		L.control
			.layers({ [__("Satellite")]: satellite, [__("Streets")]: streets }, {}, { position: "topright" })
			.addTo(map);
		return { satellite: satellite, streets: streets };
	}

	/**
	 * Draw one map.
	 *
	 * `spec` carries whatever this record knows about where it is:
	 *   geometries: [{ geometry, centroid, label, colour }]  — polygons
	 *   point:      [lat, lon]                               — a single marker
	 *   point_label: string
	 *   track:      [{ lat, lon, timestamp, label }]         — a crew's breadcrumbs
	 *   editable:   { doctype, docname, geometry, centroid, county } — v0.33.0
	 *   home:       { centre: [lat, lon], zoom }             — where an empty map opens
	 *   title:      the section heading
	 * Any combination; a record with none of them and nothing editable gets no
	 * section at all.
	 */
	erpnext_mcp.geo_map.render = function (frm, spec) {
		spec = spec || {};
		const editable = spec.editable || null;
		const has_anything =
			(spec.geometries || []).length || spec.point || (spec.track || []).length;
		if (!has_anything && !editable) {
			return;
		}
		const body = section_for(frm, spec.title);
		const canvas = document.createElement("div");
		canvas.style.height = editable ? EDITABLE_MAP_HEIGHT : MAP_HEIGHT;
		canvas.style.width = "100%";
		canvas.style.borderRadius = "6px";
		body.appendChild(canvas);

		const controls = editable ? document.createElement("div") : null;
		if (controls) {
			controls.className = "erpnext-mcp-map-controls";
			controls.style.marginTop = "10px";
			body.appendChild(controls);
		}

		(editable ? load_draw() : load_leaflet())
			.then((L) => {
				const map = L.map(canvas, { scrollWheelZoom: false });
				add_base_layers(L, map);
				const bounds = [];

				(spec.geometries || []).forEach((entry) => {
					if (!entry.geometry) {
						return;
					}
					const layer = L.geoJSON(entry.geometry, {
						style: {
							color: entry.colour || "#1f6feb",
							weight: 2,
							fillOpacity: entry.fill_opacity == null ? 0.12 : entry.fill_opacity,
						},
					}).addTo(map);
					if (entry.label) {
						layer.bindPopup(escape_html(entry.label));
					}
					try {
						const box = layer.getBounds();
						if (box.isValid()) {
							bounds.push(box.getSouthWest(), box.getNorthEast());
						}
					} catch (error) {
						// A geometry Leaflet drew but cannot bound. Nothing to fit to;
						// the other layers still decide the view.
					}
					if (entry.centroid) {
						L.circleMarker(entry.centroid, {
							radius: 5,
							color: entry.colour || "#1f6feb",
							fillColor: "#ffffff",
							fillOpacity: 1,
							weight: 2,
						})
							.addTo(map)
							.bindPopup(
								escape_html(
									__("Centroid: {0}, {1}", [entry.centroid[0], entry.centroid[1]])
								)
							);
						bounds.push(entry.centroid);
					}
				});

				if (spec.point) {
					L.marker(spec.point)
						.addTo(map)
						.bindPopup(
							escape_html(
								spec.point_label || `${spec.point[0]}, ${spec.point[1]}`
							)
						);
					bounds.push(spec.point);
				}

				const track = spec.track || [];
				if (track.length) {
					const line = track.map((fix) => [fix.lat, fix.lon]);
					L.polyline(line, { color: "#6e7781", weight: 3, opacity: 0.7 }).addTo(map);
					track.forEach((fix, index) => {
						const fraction = track.length > 1 ? index / (track.length - 1) : 0;
						L.circleMarker([fix.lat, fix.lon], {
							radius: index === 0 || index === track.length - 1 ? 6 : 4,
							color: track_colour(fraction),
							fillColor: track_colour(fraction),
							fillOpacity: 0.9,
							weight: 1,
						})
							.addTo(map)
							.bindPopup(escape_html(fix.label || fix.timestamp || ""));
					});
					line.forEach((position) => bounds.push(position));
				}

				if (editable) {
					install_editing(L, map, frm, spec, controls, bounds);
				}

				if (bounds.length > 1) {
					map.fitBounds(L.latLngBounds(bounds).pad(0.08), { maxZoom: MAX_FIT_ZOOM });
				} else if (bounds.length === 1) {
					map.setView(bounds[0], POINT_ZOOM);
				} else {
					// Nothing at all to fit to, which only happens on an editable map
					// of a record that has never been traced. See HOME_VIEW.
					const home = spec.home || HOME_VIEW;
					map.setView(home.centre, home.zoom);
				}

				// Leaflet measures its container on creation, and the dashboard
				// section is still being laid out at that moment — without this the
				// map renders as a single tile in the top-left corner. It is the
				// oldest bug in embedding Leaflet and it looks like a broken map
				// rather than a layout problem, which is why it gets a comment.
				setTimeout(() => map.invalidateSize(), 120);
			})
			.catch((error) => {
				render_fallback(body, (error && error.message) || "unavailable", spec);
			});
	};

	// ── drawing, and the one call that saves what was drawn ──────────────────

	/** Every shape currently in the editable group, as one GeoJSON geometry.
	 *
	 * TWO SHAPES BECOME A MultiPolygon rather than a refusal. A parcel cut in half
	 * by a county road is two pieces of one parcel, `geo.parse` accepts a
	 * MultiPolygon, and telling somebody to draw it as one shape would mean
	 * drawing a boundary across a road that is not theirs.
	 */
	function current_geometry(group) {
		const collection = group.toGeoJSON();
		const areas = ((collection && collection.features) || [])
			.map((feature) => feature && feature.geometry)
			.filter((geometry) => geometry && (geometry.type === "Polygon" || geometry.type === "MultiPolygon"));
		if (!areas.length) {
			return null;
		}
		if (areas.length === 1) {
			return areas[0];
		}
		const coordinates = [];
		areas.forEach((geometry) => {
			if (geometry.type === "Polygon") {
				coordinates.push(geometry.coordinates);
			} else {
				(geometry.coordinates || []).forEach((polygon) => coordinates.push(polygon));
			}
		});
		return { type: "MultiPolygon", coordinates: coordinates };
	}

	/** A button, in the Desk's own classes so it looks like the rest of the form. */
	function button(label, variant) {
		const element = document.createElement("button");
		element.type = "button";
		element.className = `btn btn-${variant || "default"} btn-sm`;
		element.style.marginRight = "8px";
		element.textContent = label;
		return element;
	}

	/** True when Frappe has already put this failure on screen itself.
	 *
	 * A `ToolError` out of `api/gis.py` is turned into `frappe.throw`, which
	 * arrives here as an HTTP 417 carrying `_server_messages` — and Frappe's own
	 * request cleanup has already shown that sentence in a modal by the time the
	 * rejection reaches us. It is the sentence worth reading, too: it names which
	 * two acreages disagreed and by how much.
	 */
	function already_spoken(error) {
		// The field is read by name and never inferred from the body being JSON:
		// a 500 answers with a JSON body too, and taking that as "the server has
		// spoken" would suppress the message on the one failure that has none.
		const payload = (error && error.responseJSON) || error || {};
		const messages = payload._server_messages;
		return !!(messages && String(messages).replace(/[[\]"\s]/g, "").length);
	}

	/** Say that a call did not come back. Never say nothing.
	 *
	 * THIS IS THE HANDLER THAT WAS MISSING, and its absence had one symptom:
	 * a boundary that silently did not save looks exactly like one that did.
	 * `frappe.call` rejects its promise on any non-200 — an expired session, a
	 * dropped connection, a server exception with no message — and a `.then()`
	 * with no `.catch()` after it turns that into an unhandled rejection in a
	 * console nobody has open. The freeze lifts, the button re-enables, the shape
	 * stays on the map, and the record is unchanged.
	 *
	 * THE ONE THING IT WILL NOT DO IS SAY IT TWICE. Where the server refused on
	 * purpose, Frappe has already shown the refusal and it is a better sentence
	 * than anything this function knows how to write; a second modal on top of it
	 * would train people to dismiss both without reading either.
	 */
	function explain_failure(error, headline) {
		if (window.console && console.error) {
			console.error("erpnext_mcp.geo_map:", headline, error);
		}
		if (already_spoken(error)) {
			return null;
		}
		const detail =
			(error && (error.message || (error.responseJSON && error.responseJSON.exception))) || "";
		frappe.msgprint({
			title: __("Boundary Error"),
			indicator: "red",
			message: `<div>${escape_html(headline)}</div>${
				detail ? `<div style="margin-top:6px;font-family:monospace">${escape_html(detail)}</div>` : ""
			}`,
		});
		return null;
	}

	/** `frm.save()`, as a promise that actually settles when the save fails.
	 *
	 * THE FOURTH ARGUMENT IS THE WHOLE POINT. Frappe's `Form.save(action,
	 * callback, btn, on_error)` resolves its promise after a successful write,
	 * but on a failed one it rejects ONLY if an `on_error` was handed in — a
	 * plain `frm.save()` that trips a mandatory field shows its dialog and then
	 * leaves the promise pending forever. Anything chained behind it, including
	 * the `.catch` that is supposed to explain the failure, never runs at all.
	 *
	 * The no-op callback exists purely to switch that rejection on; what to say
	 * about it is the caller's business, in the rejection handler.
	 */
	function save_record(frm) {
		return frm.save("Save", null, null, () => {});
	}

	/** What the server said, in the channel that suits it.
	 *
	 * WARNINGS GET A MODAL AND NOT A TOAST. Every warning the boundary tools emit
	 * is a thing somebody has to decide about — a block that now hangs outside its
	 * parcel, an acreage that disagrees by eight percent, three zones left outside
	 * the shape. A toast that fades after four seconds is how those get missed.
	 */
	function report(result) {
		const warnings = (result && result.warnings) || [];
		if (warnings.length) {
			frappe.msgprint({
				title: __("Boundary saved, with {0} note(s)", [warnings.length]),
				indicator: "orange",
				message: `<div>${escape_html(result.summary || "")}</div><ul>${warnings
					.map((line) => `<li>${escape_html(line)}</li>`)
					.join("")}</ul>`,
			});
			return;
		}
		frappe.show_alert({ message: escape_html(result.summary || __("Boundary saved")), indicator: "green" });
	}

	/** The draw toolbar, the buttons under it, and the save path they lead to. */
	function install_editing(L, map, frm, spec, controls, bounds) {
		const conf = spec.editable;
		const group = new L.FeatureGroup();
		map.addLayer(group);

		if (conf.geometry) {
			L.geoJSON(conf.geometry, { style: DRAWN_STYLE }).eachLayer((layer) => group.addLayer(layer));
			try {
				const box = group.getBounds();
				if (box.isValid()) {
					bounds.push(box.getSouthWest(), box.getNorthEast());
				}
			} catch (error) {
				// Unboundable, same as the read-only path. The view falls back.
			}
		}

		map.addControl(
			new L.Control.Draw({
				position: "topleft",
				draw: {
					// `allowIntersection: false` is the client-side half of the
					// check `geo.validate_geometry` makes on the server: a
					// self-intersecting ring is refused there, and refusing it
					// while the mouse is still moving is a better place to say so.
					// The server check is not removed — it is what protects the
					// other two callers.
					polygon: {
						allowIntersection: false,
						showArea: false,
						shapeOptions: DRAWN_STYLE,
						drawError: {
							color: "#cf222e",
							message: __("A boundary cannot cross itself."),
						},
					},
					rectangle: { showArea: false, shapeOptions: DRAWN_STYLE },
					polyline: false,
					circle: false,
					marker: false,
					circlemarker: false,
				},
				edit: { featureGroup: group, remove: true },
			})
		);

		const original = conf.geometry ? JSON.stringify(conf.geometry) : "";
		let dirty = false;

		// Read by the container link's own handler, which rebuilds this whole
		// section and would throw an unsaved shape away with it. Set on the form
		// rather than kept here because the two live in different closures, and
		// reassigned on every rebuild so it always answers for the map on screen.
		frm.__erpnext_mcp_drawing = () => dirty;

		const save_button = button(__("Save Boundary"), "primary");
		const revert_button = button(__("Revert"), "default");
		const status = document.createElement("span");
		status.className = "text-muted";
		status.style.marginLeft = "4px";

		function refresh_buttons() {
			save_button.disabled = !dirty;
			revert_button.disabled = !dirty;
			// `frm.is_new()` is asked here rather than read off `conf` because a
			// form can stop being new while this toolbar is on screen: that is
			// exactly what pressing Save Boundary on an unsaved record does.
			if (frm.is_new()) {
				status.textContent = dirty
					? __(
							"Unsaved shape — Save Boundary will create this record first, then set the boundary on it."
						)
					: __("Draw a boundary, then press Save Boundary to create the record.");
				return;
			}
			status.textContent = dirty
				? __("Unsaved shape — nothing is written until you press Save Boundary.")
				: conf.geometry
					? __("Drawn from the saved boundary. Use the edit tool to move a vertex.")
					: __("No boundary recorded. Draw one with the polygon or rectangle tool.");
		}

		function mark_dirty() {
			dirty = true;
			refresh_buttons();
		}

		map.on(L.Draw.Event.CREATED, (event) => {
			// ONE RECORD, ONE BOUNDARY. Drawing a second shape replaces the first
			// rather than adding to it — a person who wants two pieces edits the
			// shape into a MultiPolygon deliberately, and the far commoner case is
			// somebody redrawing because the first attempt was wrong.
			group.clearLayers();
			group.addLayer(event.layer);
			mark_dirty();
		});
		map.on(L.Draw.Event.EDITED, mark_dirty);
		map.on(L.Draw.Event.DELETED, mark_dirty);

		/**
		 * Send what is on the map to the boundary tools, creating the record first
		 * if it does not exist yet.
		 *
		 * THE RECORD IS SAVED BEFORE THE SHAPE IS, AND IN THAT ORDER ONLY. There
		 * is no way to hand a polygon to `set_parcel_boundary` and have it invent
		 * the parcel: the tool reads the recorded acreage out of the database to
		 * compare the drawn area against, refuses a disagreement past a quarter,
		 * and writes its derived fields onto a row that has to be there. So an
		 * unsaved form does `frm.save()` first — which runs every mandatory-field
		 * and validation check the doctype has, in Frappe's own channel, with
		 * Frappe's own dialog — and only a record that actually landed goes on to
		 * get a boundary.
		 *
		 * THE GEOMETRY IS READ OFF THE MAP BEFORE ANY OF THAT. Saving a new record
		 * re-renders the form, which tears this map section down and builds a
		 * fresh one; `group` is then a layer group belonging to a map nobody can
		 * see. The shape has to be in hand before the save, not fetched after it.
		 *
		 * AND THE NAME IS READ AFTER. `conf.docname` is null on a form that was
		 * new when the map was drawn — `frm.doc.name` is the one that becomes the
		 * real name the moment the record exists.
		 */
		function save(extra) {
			const geometry = current_geometry(group);
			if (!geometry) {
				frappe.msgprint({
					title: __("Nothing to save"),
					indicator: "red",
					message: __(
						"There is no shape on the map. Draw one with the polygon tool, or import it from the county."
					),
				});
				return Promise.resolve(null);
			}

			function commit_boundary() {
				return frappe
					.call({
						method: SAVE_METHOD,
						args: {
							doctype: conf.doctype,
							name: frm.doc.name,
							geojson: JSON.stringify(geometry),
						},
						freeze: true,
						freeze_message: __("Saving the boundary…"),
					})
					.then((response) => {
						const result = response && response.message;
						if (!result) {
							return null;
						}
						dirty = false;
						report(Object.assign({}, result, extra || {}));
						// Reload rather than patch: the tools recompute the centroid,
						// the bounding box, the H3 coverage and the computed acreage,
						// and a form still showing the previous release's numbers next
						// to the new shape is exactly the disagreement this widget
						// exists to catch.
						frm.reload_doc();
						return result;
					})
					.catch((error) =>
						explain_failure(
							error,
							__("The boundary was not saved. The shape is still on the map — nothing on this record changed.")
						)
					);
			}

			if (frm.is_new()) {
				return save_record(frm).then(commit_boundary, (error) =>
					// A rejection here is nearly always a mandatory field the form
					// has already highlighted and named in its own dialog, so this
					// says what did NOT happen rather than repeating what did.
					explain_failure(
						error,
						__("The record was not created, so the boundary was not saved. Fill in the required fields and press Save Boundary again.")
					)
				);
			}
			return commit_boundary();
		}

		save_button.addEventListener("click", () => save());
		revert_button.addEventListener("click", () => {
			group.clearLayers();
			if (original) {
				L.geoJSON(JSON.parse(original), { style: DRAWN_STYLE }).eachLayer((layer) =>
					group.addLayer(layer)
				);
			}
			dirty = false;
			refresh_buttons();
		});

		controls.appendChild(save_button);
		controls.appendChild(revert_button);

		if (conf.county) {
			const import_button = button(__("Import from County GIS"), "default");
			import_button.addEventListener("click", () =>
				open_county_import(frm, {
					L: L,
					map: map,
					group: group,
					controls: controls,
					conf: conf,
					save: save,
					mark_dirty: mark_dirty,
					style: DRAWN_STYLE,
				})
			);
			controls.appendChild(import_button);
		}

		controls.appendChild(status);
		refresh_buttons();
	}

	// ── the county import ────────────────────────────────────────────────────

	/** Ask the server's proxy, and hand the answer to the preview.
	 *
	 * THE BROWSER NEVER TALKS TO THE COUNTY. `api/gis.py` holds the URL, validates
	 * the tax lot before it goes into a `where` clause, and answers with a shape
	 * in this app's own vocabulary. See that module for why the request is proxied
	 * rather than made from here.
	 */
	function query_county(args) {
		return frappe
			.call({
				method: COUNTY_METHOD,
				args: args,
				freeze: true,
				freeze_message: __("Asking the county…"),
			})
			.then((response) => (response && response.message) || null)
			.catch((error) =>
				explain_failure(
					error,
					__("The county could not be asked. Nothing was imported and nothing on this record changed.")
				)
			);
	}

	/** Two ways to name one parcel, and the form clears whichever you are not using.
	 *
	 * THE ACCOUNT NUMBER IS THE ONE A PERSON CAN ACTUALLY READ OFF A TAX BILL. A
	 * tax lot is five fields in a fixed order and a character wrong in any of them
	 * returns a real parcel somewhere else; the account number is four digits and
	 * the layer's own integer key. It is the search that cannot be mistyped into a
	 * different farm.
	 *
	 * THE FIELDS CLEAR EACH OTHER RATHER THAN REFUSING. `tax_lot` is pre-filled
	 * from this form's Assessor Parcel ID, which is the right default and would
	 * otherwise make "fill in one or the other" an error message on every single
	 * account-number search. Typing in one box empties the other, so the rule is
	 * visible as you type instead of arriving after you press Search. The refusal
	 * below stays as the guard for the case where something fills both anyway.
	 */
	function open_county_import(frm, ctx) {
		const only_one = (typed, cleared) => () => {
			if (String(dialog.get_value(typed) || "").trim()) {
				dialog.set_value(cleared, "");
			}
		};
		const dialog = new frappe.ui.Dialog({
			title: __("Import from County GIS"),
			fields: [
				{
					fieldtype: "Data",
					fieldname: "tax_lot",
					label: __("Tax Lot Number"),
					default: frm.doc.parcel_id || "",
					description: __(
						"Wasco County tax lots are township, range, section, quarter, lot — 2N 11E 1 CC 4039, or 1N 13E 7 200 where there is no quarter. The compact spelling off a deed, 2N11E35BA-01600, works too. It is the Assessor Parcel ID on this form."
					),
					onchange: only_one("tax_lot", "account"),
				},
				{
					fieldtype: "Data",
					fieldname: "account",
					label: __("Account Number"),
					description: __(
						"The assessor's account number off a tax statement — 7503. Plain digits. Fill in this or the tax lot number, not both; typing in one empties the other."
					),
					onchange: only_one("account", "tax_lot"),
				},
				{
					fieldtype: "HTML",
					fieldname: "note",
					options: `<div class="text-muted" style="margin-top:4px">${escape_html(
						__(
							"Or close this and press Find Under a Point, then click the parcel on the satellite map. Nothing is saved until you press Apply on the result."
						)
					)}</div>`,
				},
			],
			primary_action_label: __("Search"),
			primary_action(values) {
				const lot = String((values && values.tax_lot) || "").trim();
				const account = String((values && values.account) || "").trim();
				if (lot && account) {
					frappe.msgprint(
						__(
							"Search by tax lot number or by account number, not both. They are two questions and the answer would not say which one matched — clear one of them."
						)
					);
					return;
				}
				if (!lot && !account) {
					frappe.msgprint(
						__("Enter a tax lot number or an account number, or use Find Under a Point.")
					);
					return;
				}
				dialog.hide();
				query_county(lot ? { tax_lot: lot } : { account: account }).then((result) =>
					preview_county(frm, ctx, result)
				);
			},
			secondary_action_label: __("Find Under a Point"),
			secondary_action() {
				dialog.hide();
				arm_point_pick(frm, ctx);
			},
		});
		dialog.show();
	}

	/** One click on the map becomes one spatial query, and then disarms itself.
	 *
	 * ARMED RATHER THAN ALWAYS-ON. A map that queried the county on every click
	 * would fire while somebody was panning to look at a corner, and each firing
	 * is an outbound request from the farm's server.
	 */
	function arm_point_pick(frm, ctx) {
		const map = ctx.map;
		const banner = notice(
			ctx.controls,
			__("Click the parcel on the map. Press Escape to cancel."),
			"blue"
		);
		const previous_cursor = map.getContainer().style.cursor;
		map.getContainer().style.cursor = "crosshair";

		function disarm() {
			map.off("click", on_click);
			document.removeEventListener("keydown", on_key);
			map.getContainer().style.cursor = previous_cursor;
			banner.remove();
		}
		function on_click(event) {
			disarm();
			query_county({
				lat: event.latlng.lat.toFixed(6),
				lon: event.latlng.lng.toFixed(6),
			}).then((result) => preview_county(frm, ctx, result));
		}
		function on_key(event) {
			if (event.key === "Escape") {
				disarm();
			}
		}
		map.on("click", on_click);
		document.addEventListener("keydown", on_key);
	}

	/** A dismissable line under the map. Returns something with `.remove()`. */
	function notice(controls, text, indicator) {
		const element = document.createElement("div");
		element.className = "erpnext-mcp-map-notice";
		element.style.marginTop = "8px";
		element.innerHTML = `<span class="indicator ${indicator || "blue"}">${escape_html(text)}</span>`;
		controls.appendChild(element);
		return element;
	}

	/** Draw what the county sent, and let somebody look at it before applying it.
	 *
	 * THE PREVIEW IS THE POINT OF THE WHOLE FEATURE. A tax lot number typed with
	 * one character wrong returns a real parcel, somewhere else, and every number
	 * on it looks plausible. Drawn on the satellite image next to the block the
	 * operator knows, it is obviously the wrong piece of ground in under a second.
	 */
	function preview_county(frm, ctx, result) {
		if (!result) {
			return;
		}
		const features = result.features || [];
		(result.warnings || []).forEach((line) =>
			frappe.show_alert({ message: escape_html(line), indicator: "orange" }, 10)
		);
		if (!features.length) {
			return;
		}

		const layers = new ctx.L.FeatureGroup();
		ctx.map.addLayer(layers);
		features.forEach((feature) => {
			ctx.L.geoJSON(feature.geometry, { style: PREVIEW_STYLE }).eachLayer((layer) =>
				layers.addLayer(layer)
			);
		});
		try {
			ctx.map.fitBounds(layers.getBounds().pad(0.15), { maxZoom: MAX_FIT_ZOOM });
		} catch (error) {
			// Nothing to fit to. The shape is still on the map at whatever view.
		}

		const panel = document.createElement("div");
		panel.className = "erpnext-mcp-county-result";
		panel.style.marginTop = "10px";
		panel.style.padding = "8px 10px";
		panel.style.border = "1px solid var(--border-color, #d0d7de)";
		panel.style.borderRadius = "6px";

		const heading = document.createElement("div");
		heading.className = "text-muted";
		heading.style.marginBottom = "6px";
		heading.textContent = __("{0} — {1} match(es). Nothing is saved until you press Apply.", [
			result.label || result.county,
			features.length,
		]);
		panel.appendChild(heading);

		function close() {
			ctx.map.removeLayer(layers);
			panel.remove();
		}

		features.forEach((feature) => {
			const row = document.createElement("div");
			row.style.display = "flex";
			row.style.alignItems = "center";
			row.style.gap = "8px";
			row.style.padding = "4px 0";

			const apply = button(__("Apply"), "primary");
			apply.style.marginRight = "0";
			apply.addEventListener("click", () => {
				close();
				apply_county_feature(frm, ctx, feature, result);
			});
			row.appendChild(apply);

			const label = document.createElement("span");
			label.innerHTML = describe_feature(feature);
			row.appendChild(label);
			panel.appendChild(row);
		});

		const discard = button(__("Discard"), "default");
		discard.style.marginTop = "6px";
		discard.addEventListener("click", close);
		panel.appendChild(discard);

		ctx.controls.appendChild(panel);
	}

	/** One county parcel in a sentence, with both acreages when they exist.
	 *
	 * BOTH NUMBERS, NEVER ONE. `county_acres` is the assessor's own figure on its
	 * own projected grid; `area_computed_acres` is what `geo.area_acres` makes of
	 * the same polygon on a sphere. They agree to a fraction of a percent when the
	 * import is right, and a reader who can see both can tell a projection
	 * difference from the wrong parcel.
	 */
	function describe_feature(feature) {
		const parts = [];
		if (feature.tax_lot) {
			parts.push(`<strong>${escape_html(feature.tax_lot)}</strong>`);
		}
		// THE ACCOUNT NUMBER WAS ALWAYS EXTRACTED AND NEVER SHOWN. It is the
		// county's own key for this parcel and the one thing on this line a person
		// can check against a tax statement in a glance — and from v0.126.0 it is
		// also a way to search, so a reader who sees it here can come back to the
		// same parcel without retyping five fields in the right order.
		if (feature.account) {
			parts.push(escape_html(__("account {0}", [feature.account])));
		}
		if (feature.taxpayer) {
			parts.push(escape_html(feature.taxpayer));
		}
		if (feature.county_acres) {
			parts.push(escape_html(__("{0} acres (county)", [feature.county_acres])));
		}
		if (feature.area_computed_acres) {
			parts.push(escape_html(__("{0} acres (computed)", [feature.area_computed_acres])));
		}
		return parts.join(" &middot; ") || escape_html(__("an unnamed parcel"));
	}

	/**
	 * Put the county's shape on the form and save it, replacing what is there.
	 *
	 * THE COUNTY WINS, WHICH IS AN OPERATOR'S DECISION AND NOT A DEDUCTION — and
	 * v0.34.0 to v0.126.0 did the opposite, so it is worth saying which changed
	 * and why. Those releases treated a populated field as a second source to be
	 * protected: an acreage typed off a deed and a county GIS that disagreed were
	 * two claims, and the form reported the difference rather than picking. That
	 * is a defensible policy and it is not the one this farm wants. The assessor,
	 * the deed and the tax bill all describe the same lot the county's layer
	 * does; a figure typed into this form years ago is a transcription of that
	 * lot, and when the two disagree it is nearly always the transcription that
	 * is stale. So from v0.127.0 an import OVERWRITES, and does not ask.
	 *
	 * WHAT IS REPORTED IS WHAT WAS REPLACED, not a request to confirm it. The
	 * summary lists the old value beside the new one for every field that
	 * changed, which is what makes a wrong import recoverable by hand — but
	 * nothing is skipped and nothing is held back waiting for an answer.
	 *
	 * THIS TAKES THE ACREAGE GUARD OUT OF THE LOOP, and that is the one real cost.
	 * `set_parcel_boundary` REFUSES a polygon that disagrees with the recorded
	 * acreage by more than a quarter, and until now that refusal was the app's
	 * automatic answer to "the wrong tax lot was imported": a parcel recorded at
	 * 131 acres would not accept a 41-acre lot's shape. Overwriting the acreage
	 * with the county's own figure first means the polygon is then compared
	 * against a number that came off the same measurement, so it always agrees
	 * and the guard can no longer fire on an import.
	 *
	 * WHAT REPLACES IT IS THE PREVIEW, which was always the better check and is
	 * why it exists. The county's shape is drawn dashed on the satellite image
	 * next to the block the operator knows, and nothing is written until they
	 * press Apply on that row. A wrong lot is obvious there in under a second, in
	 * a way that an acreage percentage never was. The guard still protects every
	 * other caller — the AI, the phone, a hand-drawn polygon — because it lives
	 * in `set_parcel_boundary` and none of this touches it.
	 *
	 * `title_holder` IS STILL NEVER TOUCHED, and that is not an exception to the
	 * policy above but a different thing entirely. It is a Link to a Related Party
	 * on this site and the county's `Taxpayer` is free text off a tax roll;
	 * writing one into the other is not "use the county's value", it is inventing
	 * a link between two registers by string match, which is how a parcel ends up
	 * owned by the wrong entity in an accounting system. The name is reported so
	 * a person can set it themselves.
	 */
	function apply_county_feature(frm, ctx, feature, result) {
		ctx.group.clearLayers();
		ctx.L.geoJSON(feature.geometry, { style: ctx.style }).eachLayer((layer) =>
			ctx.group.addLayer(layer)
		);
		ctx.mark_dirty();

		const filled = [];
		const replaced = [];
		const kept = [];

		/** Whether there was anything there to replace, for the summary's wording.
		 *
		 * A FLOAT FIELD HAS NO EMPTY. `acreage` on a Parcel that nobody has typed
		 * one into is `0`, not `""` and not null — so a check that only knew about
		 * null and the empty string would tell somebody creating a parcel that
		 * "Acreage: 0 replaced with 131.43", which is both noise and a claim that
		 * a figure existed and was discarded. The zero test is deliberately
		 * restricted to values that are actually NUMBERS: a Data field holding the
		 * string "0" is a value somebody typed.
		 *
		 * This decides the WORDING ONLY. Both branches write; nothing here can
		 * skip a field.
		 */
		const is_blank = (value) =>
			value == null || value === "" || (typeof value === "number" && value === 0);

		/** Write the county's value over whatever is there, and say what moved.
		 *
		 * THE ONLY REASON TO RETURN EARLY IS THAT THE COUNTY SAID NOTHING. A field
		 * the layer did not answer for has no new value to write, and blanking a
		 * populated field because the county happens to be silent about it would
		 * be destroying data rather than importing any. A value that already
		 * matches is written past in silence — there is nothing to report.
		 */
		function consider(fieldname, label, value) {
			if (value == null || value === "") {
				return;
			}
			const current = frm.doc[fieldname];
			if (String(current == null ? "" : current) === String(value)) {
				return;
			}
			frm.set_value(fieldname, value);
			if (is_blank(current)) {
				filled.push(__("{0} set to {1}", [label, value]));
			} else {
				replaced.push(__("{0}: {1} replaced with {2}, from the county.", [label, current, value]));
			}
		}

		consider("parcel_id", __("Assessor Parcel ID"), feature.tax_lot);
		consider("acreage", __("Acreage"), feature.county_acres);
		consider("county", __("County"), county_name(result));

		if (feature.account) {
			// NOT `consider`, BECAUSE PARCEL HAS NOWHERE TO PUT IT. There is no
			// account-number field on the doctype, so the number is reported here
			// rather than dropped — it is what somebody types into Account Number
			// to pull this same parcel back next season.
			kept.push(
				__(
					"The county's account number for this parcel is {0}. Nothing on this form holds it — note it if you want to search by it later.",
					[feature.account]
				)
			);
		}

		if (feature.taxpayer) {
			kept.push(
				__(
					"The county names the taxpayer as {0}. Title Holder links to a Related Party on this site, so it is not set from a tax roll — set it by hand if it is right.",
					[feature.taxpayer]
				)
			);
		}

		const notes = replaced.concat(filled, kept);
		const finish = () =>
			ctx.save().then((result) => {
				if (result && notes.length) {
					frappe.msgprint({
						title: __("Imported from the county"),
						indicator: "blue",
						message: `<ul>${notes.map((line) => `<li>${escape_html(line)}</li>`).join("")}</ul>`,
					});
				}
			});

		// The form's own fields are saved FIRST, because the boundary tool reads
		// the acreage it is about to compare against out of the database. Saving
		// the shape against an acreage that is still only on the screen would
		// compare it with whatever was there before — WHICH FROM v0.127.0 IS THE
		// WHOLE POINT OF THE ORDER RATHER THAN A DETAIL OF IT. The acreage on
		// screen is now the county's, and the polygon is the county's; getting
		// them to disk in that order is what lets `set_parcel_boundary` compare
		// two halves of one measurement instead of the new shape against the old
		// record's number, which is the comparison that would refuse.
		//
		// A NEVER-SAVED PARCEL TAKES THE SAME PATH, which is why `is_new` is asked
		// alongside `is_dirty` rather than left to `save()` to notice. Importing a
		// tax lot onto a blank Parcel form is the ordinary way to create one from
		// v0.34.0: the county sets the parcel ID, the acreage and the county
		// name, and those three are precisely the values the boundary tool is
		// about to check the polygon against — so they have to be on disk before
		// the polygon is offered, on a new record exactly as on an old one.
		if (frm.is_dirty() || frm.is_new()) {
			save_record(frm).then(finish, (error) =>
				explain_failure(
					error,
					__("The record was not saved, so the county's boundary was not applied. Fill in the required fields and import again.")
				)
			);
			return;
		}
		finish();
	}

	/** "Wasco" from "Wasco County, Oregon" — the server's own label, trimmed.
	 *
	 * The Parcel form's `county` is a plain Data field that people type "Wasco"
	 * into. Writing the service's full label there would put a different spelling
	 * in every parcel imported after this release than in every one before it.
	 */
	function county_name(result) {
		const label = String((result && result.label) || "").trim();
		return label ? label.split(",")[0].replace(/\s+County$/i, "").trim() : "";
	}

	// ── one linked record's shape, for context underneath ────────────────────

	/** One linked record's boundary, or a null entry. Never rejects.
	 *
	 * A parent whose polygon cannot be read must not stop the child's own map
	 * being drawn — the containing shape is context, and context is the part it
	 * is safe to lose.
	 */
	function fetch_boundary(doctype, name, label, colour) {
		if (!doctype || !name) {
			return Promise.resolve(null);
		}
		return frappe.db
			.get_value(doctype, name, [
				"boundary_geojson",
				"boundary_centroid_lat",
				"boundary_centroid_lon",
			])
			.then((response) => {
				const row = (response && response.message) || {};
				const geometry = parse_geometry(row.boundary_geojson);
				if (!geometry) {
					return null;
				}
				return {
					geometry: geometry,
					centroid: null,
					label: label || name,
					colour: colour || "#8250df",
					fill_opacity: 0.04,
				};
			})
			.catch(() => null);
	}

	/**
	 * The whole map for a doctype that carries a boundary: Field, Irrigation Zone
	 * and Parcel are the same form as far as this is concerned.
	 *
	 * `options.container` names the link field holding the shape this one is
	 * expected to sit inside — its parcel, or the block a zone waters. Drawing it
	 * underneath in a lighter outline is the point of the whole widget: the
	 * boundary tools REPORT containment and never enforce it, and a disagreement
	 * reported in a warning string is a disagreement nobody pictures. Drawn, it
	 * takes a second to see whether the overhang is the shared water line
	 * somebody meant or two vertices in the wrong order.
	 *
	 * READ-ONLY, AND STILL USED. v0.33.0 gave the three boundary-carrying forms an
	 * editable map through `attach_editable_boundary`; this stays because it is
	 * the right shape for any future doctype that carries a polygon nobody should
	 * be redrawing from a form, and because the read path is what the editable one
	 * degrades to when the draw plugin cannot be fetched.
	 */
	erpnext_mcp.geo_map.attach_boundary = function (doctype, options) {
		options = options || {};
		frappe.ui.form.on(doctype, {
			refresh(frm) {
				const geometry = parse_geometry(frm.doc.boundary_geojson);
				const centroid = point_of(
					frm.doc.boundary_centroid_lat,
					frm.doc.boundary_centroid_lon
				);
				if (!geometry && !centroid) {
					return;
				}
				const own = {
					geometry: geometry,
					centroid: centroid,
					label: frm.doc.name,
					colour: options.colour || "#1f6feb",
				};
				const container = options.container ? frm.doc[options.container.field] : null;
				fetch_boundary(
					options.container && options.container.doctype,
					container,
					container,
					"#8250df"
				).then((outer) => {
					erpnext_mcp.geo_map.render(frm, {
						title: options.title || __("Boundary"),
						// The container FIRST, so the record's own shape is drawn on
						// top of it rather than underneath.
						geometries: outer ? [outer, own] : [own],
					});
				});
			},
		});
	};

	/**
	 * The same map, with a draw toolbar and a save button. v0.33.0.
	 *
	 * THE DIFFERENCE FROM `attach_boundary` IS NOT ONLY THE TOOLBAR: this one
	 * renders on a record that has NO boundary at all, which the read-only version
	 * deliberately does not. An empty map is nothing to look at and everything to
	 * draw on, and a parcel with no shape yet is exactly the record somebody opens
	 * meaning to trace it.
	 *
	 * IT IS ATTACHED TO A NEW, UNSAVED RECORD TOO, FROM v0.34.0 — which is a
	 * reversal, and the reason for the original refusal is worth restating because
	 * it was a real constraint and it has not gone away. `save_boundary` needs a
	 * docname to write to, and the boundary tools read the recorded acreage out of
	 * the DATABASE to compare the drawn area against; neither works against a form
	 * that exists only on screen.
	 *
	 * What was wrong was the conclusion drawn from it. Withholding the map until
	 * the record was saved put the constraint in front of the operator instead of
	 * behind them, and it did it at the worst possible moment: a parcel being
	 * created is precisely the record nobody has traced yet, and the answer they
	 * got was a form with no map and no explanation, followed by a save, followed
	 * by a map. The constraint is now honoured where it belongs — in `save()`,
	 * which creates the record first and sets the boundary second, in one press.
	 *
	 * SO THE ORDER IS ENFORCED AND NOT ASKED FOR. Nothing about a drawn shape
	 * skips validation: `frm.save()` runs every mandatory field and every doctype
	 * check before the polygon is offered to anything, and a record that will not
	 * save does not get a boundary.
	 *
	 * `options.county` turns on the county GIS import, and only Parcel sets it. A
	 * county publishes TAX LOTS: the unit the assessor, the deed and the tax bill
	 * agree on, which is this app's Parcel. It has never heard of an orchard block
	 * or an irrigation zone, and an import button on those forms would be an
	 * invitation to set a block's boundary to the whole tax lot it sits in.
	 */
	erpnext_mcp.geo_map.attach_editable_boundary = function (doctype, options) {
		options = options || {};

		const handlers = {
			refresh: draw,
		};

		// THE SHAPE ABOVE ARRIVES WHILE THE FORM IS BEING FILLED IN, which only
		// matters now that the map is on a new record. A block's parcel is chosen
		// on the form, and until it is chosen there is nothing to draw underneath
		// — so on a saved record the container was always there when the map was
		// built, and on a new one it is empty at exactly the moment somebody wants
		// to trace against it. The deed line a block should stop at is not visible
		// on satellite imagery; without this the first thing the map is for does
		// not work on the record that needs it most.
		//
		// IT WILL NOT REDRAW OVER WORK IN PROGRESS. Rebuilding the section throws
		// the map away and with it any shape not yet saved, so a form where
		// somebody has already drawn is left alone and gets the container on its
		// next refresh instead. Losing a traced boundary to a link field is a far
		// worse trade than a missing outline.
		if (options.container && options.container.field) {
			handlers[options.container.field] = function (frm) {
				if (frm.__erpnext_mcp_drawing && frm.__erpnext_mcp_drawing()) {
					return;
				}
				draw(frm);
			};
		}

		frappe.ui.form.on(doctype, handlers);

		function draw(frm) {
			const geometry = parse_geometry(frm.doc.boundary_geojson);
			const centroid = point_of(frm.doc.boundary_centroid_lat, frm.doc.boundary_centroid_lon);
			const container = options.container ? frm.doc[options.container.field] : null;
			fetch_boundary(
				options.container && options.container.doctype,
				container,
				container,
				"#8250df"
			).then((outer) => {
				erpnext_mcp.geo_map.render(frm, {
					title: options.title || __("Boundary"),
					geometries: outer ? [outer] : [],
					home: options.home,
					editable: {
						doctype: doctype,
						// Null on a form that has never been saved. Frappe's
						// placeholder name for one is a real-looking string —
						// "new-parcel-abc123" — and a placeholder that reaches the
						// server would be looked up, missed, and reported as a
						// parcel that does not exist. `save()` reads the name off
						// `frm.doc` at the moment it sends, by which point the
						// record is real.
						docname: frm.is_new() ? null : frm.doc.name,
						geometry: geometry,
						centroid: centroid,
						county: !!options.county,
					},
				});
			});
		}
	};

	/** The whole map for a doctype that carries a single lat/lon pair. */
	erpnext_mcp.geo_map.attach_point = function (doctype, options) {
		options = options || {};
		const lat_field = options.lat_field || "gps_latitude";
		const lon_field = options.lon_field || "gps_longitude";
		frappe.ui.form.on(doctype, {
			refresh(frm) {
				const point = point_of(frm.doc[lat_field], frm.doc[lon_field]);
				if (!point) {
					return;
				}
				erpnext_mcp.geo_map.render(frm, {
					title: options.title || __("Location"),
					point: point,
					point_label: frm.doc.name,
				});
			},
		});
	};

	erpnext_mcp.geo_map.parse_geometry = parse_geometry;
	erpnext_mcp.geo_map.point_of = point_of;
	erpnext_mcp.geo_map.parse_gps_string = parse_gps_string;
	erpnext_mcp.geo_map.fetch_boundary = fetch_boundary;
	erpnext_mcp.geo_map.current_geometry = current_geometry;

	//: v0.110.0. THE BOOTSTRAP AND THE BASE LAYERS, EXPORTED FOR THE ONE MAP IN
	//: THIS APP THAT IS NOT ON A FORM.
	//:
	//: `/app/farm-overview` draws every boundary the farm has on one map, and it
	//: is a Page rather than a doctype form — so `render` above, which starts by
	//: asking a `frm` for its dashboard section, is not the function it can call.
	//: What it needs is the two things this file exists to keep in ONE place: the
	//: CDN the library comes from, and the tile URLs, attributions and zoom
	//: defaults of the two base layers.
	//:
	//: EXPORTING THEM IS THE WHOLE POINT OF THE MODULE DOCSTRING'S FIRST CLAIM.
	//: "Seven copies of a Leaflet bootstrap is seven places for the CDN URL, the
	//: tile attribution and the zoom defaults to drift apart, and the first
	//: symptom of the drift is one form that mysteriously has no map." An eighth
	//: caller that pasted the constants would be that drift, and the tile
	//: attributions are a CONDITION OF USE for both providers rather than a
	//: courtesy — a second copy is a second place for one to be dropped.
	//:
	//: NOTHING WAS MOVED TO ADD THEM. `load_leaflet` and `add_base_layers` are
	//: the same two functions the seven forms have used since v0.32.0, named on
	//: the namespace as well as in the closure.
	erpnext_mcp.geo_map.load_leaflet = load_leaflet;
	erpnext_mcp.geo_map.add_base_layers = add_base_layers;
	erpnext_mcp.geo_map.MAX_FIT_ZOOM = MAX_FIT_ZOOM;
	erpnext_mcp.geo_map.POINT_ZOOM = POINT_ZOOM;
	erpnext_mcp.geo_map.HOME_VIEW = HOME_VIEW;
})();
