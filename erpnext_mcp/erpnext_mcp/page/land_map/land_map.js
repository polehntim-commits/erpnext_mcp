// SPDX-License-Identifier: MIT
//
// /app/land-map — draw a proposed lot line and get back what a surveyor needs.
// See erpnext_mcp/api/land_map.py and erpnext_mcp/surveying.py, which argue the
// whole thing. What this file is allowed to do:
//
//   * It CALLS three whitelisted methods and draws what comes back. It decides
//     nothing about who may read which register, which entity is in scope, or
//     what a drawn line measures — every number on this page is computed on the
//     server by `surveying.py`, which is the module the tests measure.
//
//   * IT COMPUTES NO GEOMETRY OF ITS OWN, and that is the rule
//     geo_map_widget.js states for the browser: "no area is computed in this
//     file… the day it disagreed with geo.area_acres by three percent nobody
//     would know which one was wrong." Leaflet.draw offers a live acreage
//     readout while you drag and this page does not take it. The bearings, the
//     distances, the acreage and the closure all come from one Python module
//     that the standalone suite checks against known values.
//
//   * IT WRITES THROUGH THE LOT LINE ADJUSTMENT TOOLS. "Save to adjustment"
//     posts to `api.land_map.save_proposal`, which calls `lla_update` or
//     `lla_create` — so the role gate, the status machine and the after-submit
//     field lock all apply exactly as they do from a console.
//
// THE LIBRARY AND THE TILES COME FROM `geo_map_widget.js` AND NOT FROM HERE, for
// the reason that file gives: one place for the CDN URL, the tile URLs and the
// attributions, which are a condition of use rather than a courtesy. v0.171.0
// exports `load_draw` there so this page uses the same plugin the editable forms
// already load, from the same URL.
//
// IT DEGRADES RATHER THAN FAILING. No Leaflet means no map, and the page then
// lists what it would have drawn, with links to the records. A drawing cannot be
// made without the library, and the page says that in a sentence rather than
// offering a button that does nothing.

frappe.pages["land-map"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Land Map"),
		single_column: true,
	});
	wrapper.land_map = erpnext_mcp_land_map(page);
};

frappe.pages["land-map"].on_page_show = function (wrapper) {
	// A boundary somebody just saved on a Parcel form, or an adjustment they
	// came here from, is the first thing they expect to see. Nothing is cached.
	if (wrapper.land_map) {
		wrapper.land_map.reload();
	}
};

function erpnext_mcp_land_map(page) {
	const METHOD = "erpnext_mcp.api.land_map.land_map";
	const PREVIEW_METHOD = "erpnext_mcp.api.land_map.survey_preview";
	const SAVE_METHOD = "erpnext_mcp.api.land_map.save_proposal";
	// v0.173.0. The three exports are DOWNLOADS rather than `frappe.call`s: the
	// server answers a file, so the browser is pointed at the method and the
	// bytes never pass through this script. Every one of them is built by
	// erpnext_mcp/land_export.py, which is also what the MCP export tools call —
	// so the KML a surveyor gets from this page and the one a model hands over
	// are the same file.
	const EXPORT_KML = "erpnext_mcp.api.land_map.export_kml";
	const EXPORT_GEOJSON = "erpnext_mcp.api.land_map.export_geojson";
	const EXPORT_PDF = "erpnext_mcp.api.land_map.export_pdf";
	const WIDGET = "/assets/erpnext_mcp/js/geo_map_widget.js";
	const LOAD_TIMEOUT_MS = 15000;

	const $body = $(frappe.render_template("land_map")).appendTo(page.main);
	const $status = $body.find(".lm-status");
	const $fallback = $body.find(".lm-fallback");
	const $company = $body.find(".lm-company");
	const $adjustment = $body.find(".lm-adjustment");
	const $legal = $body.find(".lm-legal");

	let answer = null;
	let preview = null;
	let company = null;
	let adjustment = frappe.route_options && frappe.route_options.lot_line_adjustment;
	let loading_widget = null;
	let map = null;
	let leaflet = null;
	let drawn_layer = null;
	let drawn_points = [];
	let drawing = null;
	//: Corridors traced on this page and not yet saved, as GeoJSON Features. A
	//: ditch somebody knows about is worth checking against before either it or
	//: the line is written down, so they are sent with every measure request.
	let easements = [];
	let easement_layer = null;
	let drawing_easement = false;
	let easement_label = "";

	if (frappe.route_options) {
		delete frappe.route_options.lot_line_adjustment;
	}

	/** The widget, loaded once. Resolves with `erpnext_mcp.geo_map`. */
	function load_widget() {
		if (window.erpnext_mcp && erpnext_mcp.geo_map && erpnext_mcp.geo_map.load_leaflet) {
			return Promise.resolve(erpnext_mcp.geo_map);
		}
		if (loading_widget) {
			return loading_widget;
		}
		loading_widget = new Promise((resolve, reject) => {
			const script = document.createElement("script");
			script.src = WIDGET;
			script.async = true;
			const timer = setTimeout(() => reject(new Error(__("timed out"))), LOAD_TIMEOUT_MS);
			script.onload = () => {
				clearTimeout(timer);
				if (window.erpnext_mcp && erpnext_mcp.geo_map && erpnext_mcp.geo_map.load_draw) {
					resolve(erpnext_mcp.geo_map);
				} else {
					// Served, and not the build this page reads. Looks identical
					// to no network and is a different problem: `bench build`.
					reject(new Error(__("the map widget loaded but is an older build")));
				}
			};
			script.onerror = () => {
				clearTimeout(timer);
				reject(new Error(__("could not be fetched")));
			};
			document.head.appendChild(script);
		});
		return loading_widget;
	}

	function say(text, colour) {
		$status.text(text || "");
		if (colour) {
			page.set_indicator(text, colour);
		}
	}

	function reload() {
		say(__("Loading"), "orange");
		return frappe
			.call({ method: METHOD, args: { company: company } })
			.then((response) => {
				answer = (response && response.message) || null;
				if (!answer) {
					say(__("No answer from the server"), "red");
					return;
				}
				company = answer.company || company;
				fill_company_picker();
				fill_adjustment_picker();
				$body.find(".lm-disclaimer").text(answer.disclaimer || "");
				return draw();
			})
			.catch((error) => {
				say(__("Could not load the map"), "red");
				$fallback.show().text(String((error && error.message) || error));
			});
	}

	function fill_company_picker() {
		const options = (answer.companies || []).map(
			(name) =>
				`<option value="${frappe.utils.escape_html(name)}"${
					name === answer.company ? " selected" : ""
				}>${frappe.utils.escape_html(name)}</option>`
		);
		$company.html(options.join("")).toggle(options.length > 1);
	}

	function fill_adjustment_picker() {
		const rows = answer.adjustments || [];
		const options = [`<option value="">${__("No adjustment chosen")}</option>`].concat(
			rows.map((row) => {
				const label = `${row.name} — ${row.title || ""} (${row.status || ""})`;
				return `<option value="${frappe.utils.escape_html(row.name)}"${
					row.name === adjustment ? " selected" : ""
				}>${frappe.utils.escape_html(label)}</option>`;
			})
		);
		$adjustment.html(options.join(""));
	}

	function chosen_adjustment() {
		return (answer.adjustments || []).find((row) => row.name === adjustment) || null;
	}

	/** Every polygon the answer carries, as `{label, colour, shapes}` groups. */
	function groups() {
		const out = [];
		(answer.layers || []).forEach((layer) => {
			if ((layer.shapes || []).length) {
				out.push({ label: layer.label, colour: layer.colour, shapes: layer.shapes });
			}
		});
		const lots = answer.tax_lots || {};
		if ((lots.shapes || []).length) {
			out.push({ label: lots.label, colour: lots.colour, shapes: lots.shapes });
		}
		return out;
	}

	function draw() {
		return load_widget()
			.then((widget) => widget.load_draw())
			.then((L) => {
				leaflet = L;
				$fallback.hide();
				build_map(L);
			})
			.catch((error) => {
				// No library, no map, and no pretending otherwise.
				const reason = String((error && error.message) || error);
				$body.find(".lm-map").hide();
				$fallback.show().html(fallback_html(reason));
				say(__("The map could not be drawn"), "red");
			});
	}

	function fallback_html(reason) {
		const rows = [];
		groups().forEach((group) => {
			(group.shapes || []).forEach((shape) => {
				const centre = shape.centre ? `${shape.centre[1]}, ${shape.centre[0]}` : __("no coordinates");
				rows.push(
					`<tr><td>${frappe.utils.escape_html(group.label)}</td>` +
						`<td><a href="${shape.route}">${frappe.utils.escape_html(shape.label)}</a></td>` +
						`<td>${frappe.utils.escape_html(centre)}</td></tr>`
				);
			});
		});
		return (
			`<p>${__("The map library {0}, so there is nothing to draw on.", [
				frappe.utils.escape_html(reason),
			])} ${__("Drawing a proposed line needs it. The records are below.")}</p>` +
			`<table class="lm-courses"><tbody>${rows.join("")}</tbody></table>`
		);
	}

	function build_map(L) {
		if (map) {
			map.remove();
			map = null;
		}
		map = L.map($body.find(".lm-map")[0], { scrollWheelZoom: true });
		const overlays = {};
		groups().forEach((group) => {
			const layer = L.featureGroup();
			(group.shapes || []).forEach((shape) => {
				if (!shape.geometry) {
					return;
				}
				L.geoJSON(shape.geometry, {
					style: { color: group.colour, weight: 2, fillOpacity: 0.08 },
				})
					.bindPopup(popup_html(shape))
					.addTo(layer);
			});
			layer.addTo(map);
			overlays[group.label] = layer;
		});

		drawn_layer = L.featureGroup().addTo(map);
		easement_layer = L.featureGroup().addTo(map);
		erpnext_mcp.geo_map.add_base_layers(L, map, overlays);

		const bounds = answer.bounds;
		if (bounds) {
			map.fitBounds(
				L.latLngBounds(L.latLng(bounds[0][0], bounds[0][1]), L.latLng(bounds[1][0], bounds[1][1])),
				{ maxZoom: erpnext_mcp.geo_map.MAX_FIT_ZOOM }
			);
		} else {
			map.setView(erpnext_mcp.geo_map.HOME_VIEW.centre, erpnext_mcp.geo_map.HOME_VIEW.zoom);
		}

		map.on(L.Draw.Event.CREATED, function (event) {
			if (drawing_easement) {
				// A corridor, not the proposal. It joins the list the measure
				// request is checked against and leaves the proposal alone.
				const feature = event.layer.toGeoJSON();
				const label = easement_label || __("Easement {0}", [String(easements.length + 1)]);
				feature.properties = Object.assign({}, feature.properties, { label: label });
				easements.push(feature);
				easement_layer.addLayer(event.layer);
				drawing_easement = false;
				drawing = null;
				say(__("{0} easement corridor(s) drawn.", [String(easements.length)]));
				return;
			}
			drawn_layer.clearLayers();
			drawn_layer.addLayer(event.layer);
			drawn_points = ring_of(event.layer);
			drawing = null;
			say(__("{0} points drawn. Press Compute.", [String(drawn_points.length)]));
		});

		// An adjustment already carrying a proposal opens with it on the map.
		const chosen = chosen_adjustment();
		if (chosen && chosen.proposed_geometry) {
			const layer = L.geoJSON(chosen.proposed_geometry, {
				style: { color: "#e24c4c", weight: 3, dashArray: "6 4", fillOpacity: 0.05 },
			});
			drawn_layer.addLayer(layer);
			drawn_points = points_of(chosen.proposed_geometry);
		}
		say(__("Ready"), "green");
	}

	function popup_html(shape) {
		const parts = [`<b>${frappe.utils.escape_html(shape.label || shape.name)}</b>`];
		if (shape.detail) {
			parts.push(frappe.utils.escape_html(shape.detail));
		}
		if (shape.acres) {
			parts.push(`${shape.acres} ${__("acres")}`);
		}
		parts.push(`<a href="${shape.route}">${__("Open the record")}</a>`);
		return parts.join("<br>");
	}

	/** A drawn Leaflet layer as `[lon, lat]` pairs — GeoJSON order, not Leaflet order. */
	function ring_of(layer) {
		const geojson = layer.toGeoJSON();
		return points_of(geojson.geometry || geojson);
	}

	function points_of(geometry) {
		const out = [];
		walk(geometry && geometry.coordinates, out);
		return out;
	}

	function walk(coordinates, out) {
		if (!Array.isArray(coordinates) || !coordinates.length) {
			return;
		}
		if (typeof coordinates[0] === "number") {
			out.push([coordinates[0], coordinates[1]]);
			return;
		}
		coordinates.forEach((item) => walk(item, out));
	}

	function collection() {
		return easements.length ? { type: "FeatureCollection", features: easements } : null;
	}

	function draw_easement() {
		frappe.prompt(
			[{ fieldname: "label", fieldtype: "Data", label: __("What is this easement?"), reqd: 1 }],
			(values) => {
				easement_label = values.label;
				drawing_easement = true;
				start_drawing("easement");
			},
			__("Draw an easement corridor"),
			__("Draw")
		);
	}

	function start_drawing(kind) {
		if (!leaflet || !map) {
			frappe.msgprint(__("The map library is not loaded, so there is nothing to draw on."));
			return;
		}
		if (drawing) {
			drawing.disable();
		}
		if (kind !== "easement") {
			drawn_layer.clearLayers();
			drawn_points = [];
		}
		const options = {
			shapeOptions:
				kind === "easement"
					? { color: "#7575ff", weight: 6, opacity: 0.5 }
					: { color: "#e24c4c", weight: 3 },
		};
		drawing =
			kind === "area"
				? new leaflet.Draw.Polygon(map, options)
				: new leaflet.Draw.Polyline(map, options);
		drawing.enable();
		say(__("Click each corner. Click the last point twice to finish."));
	}

	function compute() {
		if (drawn_points.length < 2) {
			frappe.msgprint(__("Draw a line or an area first."));
			return;
		}
		say(__("Measuring"), "orange");
		return frappe
			.call({
				method: PREVIEW_METHOD,
				args: {
					points: JSON.stringify(drawn_points),
					adjustment: adjustment || null,
					company: company || null,
					easements: collection() ? JSON.stringify(collection()) : null,
				},
			})
			.then((response) => {
				preview = (response && response.message) || null;
				render_preview();
				say(__("Measured"), "green");
			})
			.catch(() => say(__("Could not measure the drawing"), "red"));
	}

	function render_preview() {
		if (!preview) {
			return;
		}
		const closure = preview.closure || {};
		$body
			.find(".lm-summary")
			.text(
				__("{0} acres · {1} courses · closure {2} ft ({3})", [
					String(preview.acres),
					String((preview.courses || []).length),
					String(closure.error_ft),
					String(closure.precision_text || ""),
				])
			);

		const rows = (preview.courses || []).map(
			(course) =>
				`<tr><td>${course.index}</td><td>${frappe.utils.escape_html(course.bearing)}</td>` +
				`<td class="lm-num">${frappe.utils.escape_html(
					frappe.format(course.distance_ft, { fieldtype: "Float" })
				)}</td>` +
				`<td class="lm-muted">${course.closing ? __("to the point of beginning") : ""}</td></tr>`
		);
		$body.find(".lm-course-rows").html(rows.join(""));

		const flags = [];
		(preview.easement_crossings || []).forEach((crossing) => {
			flags.push(
				`<li class="lm-warn">${frappe.utils.escape_html(crossing.label)}: ${frappe.utils.escape_html(
					crossing.note
				)}</li>`
			);
		});
		if (!flags.length) {
			flags.push(
				`<li class="lm-muted">${__("No mapped easement is crossed. {0} corridor(s) checked.", [
					String(preview.easements_considered || 0),
				])}</li>`
			);
		}
		if (preview.tie_in && !preview.tie_in.found) {
			flags.push(`<li class="lm-warn">${frappe.utils.escape_html(preview.tie_in.note || "")}</li>`);
		}
		$body.find(".lm-flags").html(flags.join(""));

		const acreage = (preview.affected || []).map((row) => {
			const overlap = row.overlap_acres === null ? __("not computed") : `${row.overlap_acres} ac`;
			const giving = row.acres_if_giving === null ? "—" : `${row.acres_if_giving} ac`;
			const receiving = row.acres_if_receiving === null ? "—" : `${row.acres_if_receiving} ac`;
			return (
				`<tr><td>${frappe.utils.escape_html(row.label || "")}</td>` +
				`<td class="lm-num">${row.acres_before === null ? "—" : row.acres_before}</td>` +
				`<td class="lm-num">${frappe.utils.escape_html(overlap)}</td>` +
				`<td class="lm-num">${frappe.utils.escape_html(giving)}</td>` +
				`<td class="lm-num">${frappe.utils.escape_html(receiving)}</td></tr>`
			);
		});
		$body
			.find(".lm-acreage")
			.html(
				acreage.length
					? `<table class="lm-courses"><thead><tr><th>${__("Lot")}</th><th>${__("Now")}</th>` +
							`<th>${__("Overlap")}</th><th>${__("If giving")}</th><th>${__(
								"If receiving"
							)}</th></tr></thead><tbody>${acreage.join("")}</tbody></table>` +
							sides_html()
					: __("The drawing does not fall on any mapped lot.")
			);

		$legal.val(preview.legal_description || preview.note || "");
	}

	function sides_html() {
		const sides = preview.sides || [];
		if (!sides.length) {
			return "";
		}
		const rows = sides.map((side) => {
			const after = side.acres_after === undefined || side.acres_after === null ? "—" : side.acres_after;
			return (
				`<tr><td>${frappe.utils.escape_html(side.side || "")}</td>` +
				`<td>${frappe.utils.escape_html(String(side.lot || ""))}</td>` +
				`<td class="lm-num">${after}</td>` +
				`<td class="lm-muted">${frappe.utils.escape_html(side.acreage_basis || "")}</td></tr>`
			);
		});
		return (
			`<p class="lm-muted" style="margin-top:8px;">${__(
				"From the adjustment&#39;s own pieces and parties:"
			)}</p>` + `<table class="lm-courses"><tbody>${rows.join("")}</tbody></table>`
		);
	}

	function save() {
		if (!preview || !preview.geometry) {
			frappe.msgprint(__("Draw a boundary and press Compute before saving."));
			return;
		}
		if (!answer.may_write_adjustment) {
			frappe.msgprint(__("You may not edit a Lot Line Adjustment on this site."));
			return;
		}
		const existing = (answer.adjustments || []).filter((row) => row.docstatus < 2);
		const dialog = new frappe.ui.Dialog({
			title: __("Save to Lot Line Adjustment"),
			fields: [
				{
					fieldname: "adjustment",
					fieldtype: "Select",
					label: __("Existing adjustment"),
					options: [""].concat(existing.map((row) => row.name)).join("\n"),
					default: adjustment || "",
				},
				{
					fieldname: "title",
					fieldtype: "Data",
					label: __("Or create one with this title"),
					depends_on: "eval:!doc.adjustment",
				},
			],
			primary_action_label: __("Save"),
			primary_action(values) {
				dialog.hide();
				frappe
					.call({
						method: SAVE_METHOD,
						args: {
							adjustment: values.adjustment || null,
							title: values.title || null,
							proposed_geometry: JSON.stringify(preview.geometry),
							legal_description: $legal.val(),
							easement_geometry: collection() ? JSON.stringify(collection()) : null,
						},
					})
					.then((response) => {
						const saved = (response && response.message) || {};
						if (!saved.name) {
							return;
						}
						adjustment = saved.name;
						frappe.show_alert({ message: saved.summary || __("Saved"), indicator: "green" });
						reload();
					});
			},
		});
		dialog.show();
	}

	/** The package a surveyor is handed: the description, the courses, the flags. */
	function print_package() {
		if (!preview) {
			frappe.msgprint(__("Compute the drawing first."));
			return;
		}
		const chosen = chosen_adjustment();
		const courses = (preview.courses || [])
			.map(
				(course) =>
					`<tr><td>${course.index}</td><td>${frappe.utils.escape_html(course.bearing)}</td>` +
					`<td>${course.distance_ft} ft</td><td>${course.distance_m} m</td></tr>`
			)
			.join("");
		const crossings = (preview.easement_crossings || [])
			.map((crossing) => `<li>${frappe.utils.escape_html(crossing.label)} — ${crossing.note}</li>`)
			.join("");
		const closure = preview.closure || {};
		$body.find(".lm-print").html(
			`<h3>${__("Proposed lot line adjustment — draft for survey")}</h3>` +
				`<p>${frappe.utils.escape_html(
					chosen ? `${chosen.name} — ${chosen.title || ""}` : __("Not yet saved to an adjustment")
				)}<br>${frappe.utils.escape_html(frappe.datetime.now_datetime())}</p>` +
				`<h4>${__("Draft description")}</h4><pre>${frappe.utils.escape_html($legal.val())}</pre>` +
				`<h4>${__("Courses")}</h4><table><thead><tr><th>#</th><th>${__("Bearing")}</th>` +
				`<th>${__("Distance")}</th><th>${__("Metric")}</th></tr></thead><tbody>${courses}</tbody></table>` +
				`<p>${__("Area")}: ${preview.acres} ${__("acres")} · ${__("closure")}: ${
					closure.error_ft
				} ft (${closure.precision_text || ""}) · ${__("perimeter")}: ${closure.perimeter_ft} ft</p>` +
				(crossings ? `<h4>${__("Easements crossed")}</h4><ul>${crossings}</ul>` : "") +
				`<p><small>${frappe.utils.escape_html(preview.disclaimer || "")}</small></p>`
		);
		window.print();
	}

	// v0.173.0. An export names the adjustment it exports, so the buttons say so
	// rather than downloading an empty file: the shapes and the description live
	// ON the record, and a drawing that has not been saved yet is not on one.
	function download(method, extra) {
		if (!adjustment) {
			frappe.msgprint(
				__("Choose a lot line adjustment first, or save this drawing to one — an export reads the saved record.")
			);
			return;
		}
		const params = Object.assign({ name: adjustment }, extra || {});
		const query = Object.keys(params)
			.map((key) => encodeURIComponent(key) + "=" + encodeURIComponent(params[key]))
			.join("&");
		window.open("/api/method/" + method + "?" + query, "_blank");
	}

	$company.on("change", function () {
		company = $(this).val() || null;
		reload();
	});
	$adjustment.on("change", function () {
		adjustment = $(this).val() || null;
	});
	$body.find(".lm-draw-line").on("click", () => start_drawing("line"));
	$body.find(".lm-draw-area").on("click", () => start_drawing("area"));
	$body.find(".lm-draw-easement").on("click", draw_easement);
	$body.find(".lm-undo").on("click", () => {
		if (drawing) {
			drawing.disable();
			drawing = null;
		}
		if (drawn_layer) {
			drawn_layer.clearLayers();
		}
		if (easement_layer) {
			easement_layer.clearLayers();
		}
		easements = [];
		drawing_easement = false;
		drawn_points = [];
		preview = null;
		$body.find(".lm-course-rows").empty();
		$body.find(".lm-flags").empty();
		$legal.val("");
		say(__("Cleared"));
	});
	$body.find(".lm-compute").on("click", compute);
	$body.find(".lm-save").on("click", save);
	$body.find(".lm-print-button").on("click", print_package);
	$body.find(".lm-export-kml").on("click", () => download(EXPORT_KML, {}));
	$body.find(".lm-export-geojson").on("click", () =>
		download(EXPORT_GEOJSON, { doctype: "Lot Line Adjustment" })
	);
	$body.find(".lm-export-pdf").on("click", () => download(EXPORT_PDF, { kind: "packet" }));

	reload();
	return { reload: reload, compute: compute, points: () => drawn_points };
}
