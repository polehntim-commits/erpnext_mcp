// SPDX-License-Identifier: MIT
/**
 * One layers button for every Leaflet map this app draws. v0.330.0.
 *
 * Tim: "we may need to have a way of changing what layer we are looking at: satellite, aspect, street, slope". The
 * phone and the Desk now share one model, the one Apple and Google Maps use: a single Layers button that opens a
 * panel with
 *
 *   Base map  — Satellite / Street / Hybrid (whatever the caller offers)
 *   Overlay   — None / Slope / Aspect, with room for more (irrigation zones, spray REI, tags) as entries
 *   Opacity   — how strongly the overlay is laid over the base, 20–100 %
 *   Legend    — on or off
 *
 * THE SLOPE COLOURS ARE PAINTED ONLY INSIDE THE RECORD'S OUTLINE when the caller hands one (a Farm Task's blocks, a
 * Field, a Parcel). The raster is built once for the whole site; the clip is the page's, done on a canvas tile at
 * draw time. The farm map hands no outline and gets everything.
 *
 * THE SAME LAYER DATA AS THE PHONE. The descriptors are `slope_aspect.describe` / `slope_grade.describe` with the
 * tile URL swapped for whichever door this page has: the Desk's session (`farm_overview.terrain_tile`) or a share
 * link's token (`/farmops/api/job/<token>/tiles/...`).
 *
 * FRAMEWORK-FREE ON PURPOSE. The share-link page has no Frappe, a strict CSP (`style-src 'self'`, `img-src` without
 * `blob:`), and sets text with `textContent` only — so this file never assigns markup, never sets a `style`
 * attribute (CSSOM properties only, which the CSP allows), and decodes tiles with `createImageBitmap` rather than an
 * object URL.
 *
 * REMEMBERED PER MAP CONTEXT in `localStorage`: the base, opacity and legend per kind of map (`Farm Task`,
 * `farm_overview`, …) and the overlay per record when the caller names one (`Farm Task:FT-2026-10-00009`), so a
 * template's default overlay still applies to a task nobody has touched.
 */
(function (root) {
	"use strict";
	if (root.FarmMapLayers) {
		return;
	}

	var STORE_PREFIX = "farmops.map_layers.";
	var MIN_OPACITY = 0.2;

	//: The overlay entries in panel order. `slope_grade` is "Slope" on screen, the word a crew uses.
	var OVERLAYS = [
		{ key: "none", label: "None" },
		{ key: "slope_grade", label: "Slope" },
		{ key: "slope_aspect", label: "Aspect" },
	];

	//: The bands a dozer needs signing off on — steep and very steep. Same rule as the phone's
	//: `SlopeGradeAPI.needsDozerSignOff`.
	var DOZER_SIGN_OFF_LEVEL = 2;

	// ── pure helpers (exported for the tests) ─────────────────────────────────

	/** "Slope", "slope_grade", "grade" → "slope_grade"; "Aspect" → "slope_aspect"; anything else → "none". */
	function normalise_overlay(value) {
		var v = String(value || "").trim().toLowerCase();
		if (v === "slope" || v === "slope_grade" || v === "grade" || v === "steepness") {
			return "slope_grade";
		}
		if (v === "aspect" || v === "slope_aspect") {
			return "slope_aspect";
		}
		return "none";
	}

	function clamp_opacity(value) {
		var n = Number(value);
		if (!isFinite(n)) {
			return 1;
		}
		return Math.max(MIN_OPACITY, Math.min(1, n));
	}

	/**
	 * GeoJSON (a geometry, a Feature, a FeatureCollection or a list of any of those) → rings of [lat, lng].
	 * Only outer rings: the clip unions them, and a hole in a block is not ground anybody plans a burn around.
	 */
	function rings_of(input) {
		var out = [];
		function polygon(coords) {
			if (coords && coords[0] && coords[0].length >= 3) {
				out.push(
					coords[0].map(function (p) {
						return [Number(p[1]), Number(p[0])];
					})
				);
			}
		}
		function walk(g) {
			if (!g) {
				return;
			}
			if (Array.isArray(g)) {
				g.forEach(walk);
				return;
			}
			if (typeof g === "string") {
				try {
					walk(JSON.parse(g));
				} catch (e) {
					/* not GeoJSON — nothing to clip to */
				}
				return;
			}
			if (g.geometry !== undefined && g.type === "Feature") {
				walk(g.geometry);
			} else if (g.type === "FeatureCollection") {
				walk(g.features || []);
			} else if (g.type === "GeometryCollection") {
				walk(g.geometries || []);
			} else if (g.type === "Polygon") {
				polygon(g.coordinates);
			} else if (g.type === "MultiPolygon") {
				(g.coordinates || []).forEach(polygon);
			} else if (g.geometry) {
				walk(g.geometry); // `{geometry, label, colour}` entries as geo_map_widget's specs carry them
			}
		}
		walk(input);
		return out;
	}

	/** Where a layer's coverage sits relative to the outline: "full", "partial", "none" or "unknown". */
	function coverage(rings, bounds) {
		var points = [];
		(rings || []).forEach(function (r) {
			points = points.concat(r);
		});
		if (!bounds || !points.length) {
			return "unknown";
		}
		var inside = points.filter(function (p) {
			return p[0] >= bounds.south && p[0] <= bounds.north && p[1] >= bounds.west && p[1] <= bounds.east;
		}).length;
		if (inside === points.length) {
			return "full";
		}
		return inside === 0 ? "none" : "partial";
	}

	/**
	 * The state a map opens in: what this context remembered, then the record's default overlay, then plain
	 * satellite with nothing over it. A remembered overlay the farm no longer serves falls back to None.
	 */
	function initial_state(stored_view, stored_overlay, options, base_keys, available_overlays) {
		var view = stored_view || {};
		var base = base_keys.indexOf(view.base) >= 0 ? view.base : base_keys[0];
		var overlay =
			stored_overlay != null ? normalise_overlay(stored_overlay) : normalise_overlay(options.default_overlay);
		if (overlay !== "none" && available_overlays && available_overlays.indexOf(overlay) < 0) {
			overlay = "none";
		}
		return {
			base: base,
			overlay: overlay,
			opacity: view.opacity == null ? 1 : clamp_opacity(view.opacity),
			legend: view.legend == null ? true : !!view.legend,
		};
	}

	/** The tile URL for one address. */
	function tile_url(template, z, x, y) {
		return String(template).replace("{z}", z).replace("{x}", x).replace("{y}", y);
	}

	/** Legend rows, as data: the panel and the tests read the same thing. */
	function legend_rows(spec) {
		if (!spec) {
			return [];
		}
		if (spec.key === "slope_grade") {
			return (spec.legend || []).map(function (band) {
				return {
					colour: band.color,
					label: band.label || band.band || "",
					range: band.max_degrees == null ? band.min_degrees + "°+" : band.min_degrees + "–" + band.max_degrees + "°",
					dozer: Number(band.level) >= DOZER_SIGN_OFF_LEVEL,
				};
			});
		}
		var rows = (spec.legend || [])
			.slice()
			.sort(function (a, b) {
				return Number(a.bearing) - Number(b.bearing);
			})
			.map(function (entry) {
				return { colour: entry.color, label: entry.aspect + "-facing", range: "", dozer: false };
			});
		if (spec.flat_color) {
			rows.push({ colour: spec.flat_color, label: "Flat", range: "", dozer: false });
		}
		return rows;
	}

	/** The aspect wheel: a conic gradient in bearing order, closed back onto north. */
	function wheel_gradient(spec) {
		var sorted = (spec.legend || []).slice().sort(function (a, b) {
			return Number(a.bearing) - Number(b.bearing);
		});
		if (!sorted.length) {
			return "";
		}
		var stops = sorted.map(function (e) {
			return e.color + " " + Number(e.bearing) + "deg";
		});
		stops.push(sorted[0].color + " 360deg");
		return "conic-gradient(" + stops.join(", ") + ")";
	}

	function storage() {
		try {
			return root.localStorage || null;
		} catch (e) {
			return null;
		}
	}

	function read_json(key) {
		var s = storage();
		if (!s) {
			return null;
		}
		try {
			var raw = s.getItem(STORE_PREFIX + key);
			return raw == null ? null : JSON.parse(raw);
		} catch (e) {
			return null;
		}
	}

	function write_json(key, value) {
		var s = storage();
		if (!s) {
			return;
		}
		try {
			s.setItem(STORE_PREFIX + key, JSON.stringify(value));
		} catch (e) {
			/* private window, or full — the choice just isn't remembered */
		}
	}

	// ── DOM helpers: textContent and CSSOM only ───────────────────────────────

	function el(tag, text, style) {
		var node = root.document.createElement(tag);
		if (text != null) {
			node.textContent = text;
		}
		if (style) {
			Object.keys(style).forEach(function (k) {
				node.style[k] = style[k];
			});
		}
		return node;
	}

	// ── the clipped terrain layer ─────────────────────────────────────────────

	function terrain_layer_class(L) {
		if (terrain_layer_class.cached) {
			return terrain_layer_class.cached;
		}
		terrain_layer_class.cached = L.GridLayer.extend({
			initialize: function (spec, options) {
				this._spec = spec;
				this._rings = options.rings || [];
				this._credentials = options.credentials || "same-origin";
				this._onProblem = options.onProblem || function () {};
				var b = spec.bounds;
				L.GridLayer.prototype.initialize.call(this, {
					tileSize: spec.tile_size || 256,
					minZoom: 0,
					minNativeZoom: spec.min_zoom,
					maxNativeZoom: spec.max_zoom,
					maxZoom: 22,
					opacity: options.opacity == null ? 1 : options.opacity,
					zIndex: 5,
					bounds: b ? L.latLngBounds([b.south, b.west], [b.north, b.east]) : undefined,
					attribution: spec.source || "",
				});
			},
			createTile: function (coords, done) {
				var size = this.getTileSize();
				var canvas = root.document.createElement("canvas");
				canvas.width = size.x;
				canvas.height = size.y;
				var self = this;
				var map = this._map;
				var url = tile_url(this._spec.tile_url_template, coords.z, coords.x, coords.y);
				root
					.fetch(url, { credentials: this._credentials })
					.then(function (response) {
						if (!response.ok) {
							throw new Error(String(response.status));
						}
						return response.blob();
					})
					.then(function (blob) {
						return root.createImageBitmap(blob);
					})
					.then(function (bitmap) {
						var ctx = canvas.getContext("2d");
						if (self._rings.length && map) {
							ctx.beginPath();
							self._rings.forEach(function (ring) {
								ring.forEach(function (p, i) {
									var px = map.project(L.latLng(p[0], p[1]), coords.z);
									var x = px.x - coords.x * size.x;
									var y = px.y - coords.y * size.y;
									if (i === 0) {
										ctx.moveTo(x, y);
									} else {
										ctx.lineTo(x, y);
									}
								});
								ctx.closePath();
							});
							ctx.clip();
						}
						ctx.drawImage(bitmap, 0, 0, size.x, size.y);
						done(null, canvas);
					})
					.catch(function (error) {
						self._onProblem(error);
						done(null, canvas); // a blank tile, not a broken-image icon; the panel says why once
					});
				return canvas;
			},
		});
		return terrain_layer_class.cached;
	}

	// ── install ───────────────────────────────────────────────────────────────

	/**
	 * Put the Layers button on `map`.
	 *
	 * options:
	 *   bases:           [{key, label, layer}] — first is the default
	 *   terrain:         [descriptor] or a function returning a Promise of them (read only when an overlay is picked
	 *                    or one is remembered)
	 *   clip:            GeoJSON (or geo_map_widget geometry entries) — the outline the colours are painted inside
	 *   context:         storage key for base / opacity / legend ("Farm Task", "farm_overview", …)
	 *   overlay_context: storage key for the overlay (defaults to `context`)
	 *   default_overlay: "Slope" | "Aspect" | "None" — the record's default when nothing is remembered
	 *   credentials:     fetch credentials for tiles ("same-origin" on the Desk, "omit" on a share link)
	 *   t:               translation function (Frappe's `__`), identity by default
	 *   position:        Leaflet corner, "topright"
	 */
	function install(L, map, options) {
		options = options || {};
		var t = options.t || function (s) {
			return s;
		};
		var bases = options.bases || [];
		var base_keys = bases.map(function (b) {
			return b.key;
		});
		var context = options.context || "map";
		var overlay_context = options.overlay_context || context;
		var rings = rings_of(options.clip);
		var specs = null; // descriptors once read
		var specs_promise = null;
		var problem = null;
		var terrain_layer = null;

		var state = initial_state(
			read_json("view:" + context),
			(read_json("overlay:" + overlay_context) || {}).overlay,
			options,
			base_keys,
			null
		);

		function remember() {
			write_json("view:" + context, { base: state.base, opacity: state.opacity, legend: state.legend });
			write_json("overlay:" + overlay_context, { overlay: state.overlay });
		}

		function load_specs() {
			if (specs_promise) {
				return specs_promise;
			}
			var source = options.terrain;
			var p = typeof source === "function" ? source() : root.Promise.resolve(source || []);
			specs_promise = root.Promise.resolve(p)
				.then(function (list) {
					specs = Array.isArray(list) ? list : [];
					return specs;
				})
				.catch(function () {
					specs = [];
					return specs;
				});
			return specs_promise;
		}

		function spec_for(key) {
			return (specs || []).filter(function (s) {
				return s.key === key;
			})[0];
		}

		// The base map.
		var current_base = null;
		function apply_base() {
			var wanted = bases.filter(function (b) {
				return b.key === state.base;
			})[0] || bases[0];
			if (!wanted) {
				return;
			}
			if (current_base && current_base !== wanted.layer) {
				map.removeLayer(current_base);
			}
			if (!map.hasLayer(wanted.layer)) {
				wanted.layer.addTo(map);
			}
			if (wanted.layer.bringToBack) {
				wanted.layer.bringToBack();
			}
			current_base = wanted.layer;
		}

		// The overlay.
		function apply_overlay() {
			if (terrain_layer) {
				map.removeLayer(terrain_layer);
				terrain_layer = null;
			}
			problem = null;
			if (state.overlay === "none") {
				render();
				return;
			}
			load_specs().then(function () {
				var spec = spec_for(state.overlay);
				if (spec && spec.available && state.overlay !== "none" && !terrain_layer) {
					var Layer = terrain_layer_class(L);
					terrain_layer = new Layer(spec, {
						rings: rings,
						opacity: state.opacity,
						credentials: options.credentials,
						onProblem: function (error) {
							if (!problem) {
								problem =
									String(error && error.message) === "404"
										? t("The slope map hasn't been built on this site yet.")
										: t("Some of the slope map couldn't be drawn.");
								render();
							}
						},
					});
					terrain_layer.addTo(map);
				}
				render();
			});
		}

		// ── the button and the panel ──
		var Control = L.Control.extend({
			options: { position: options.position || "topright" },
			onAdd: function () {
				var box = el("div", null, {
					background: "#fff",
					borderRadius: "8px",
					boxShadow: "0 1px 5px rgba(0,0,0,0.4)",
					font: "13px/1.35 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
					color: "#1f2328",
				});
				box.className = "farm-map-layers";
				var button = el("button", t("Layers"), {
					minWidth: "44px",
					minHeight: "36px",
					padding: "6px 12px",
					border: "0",
					background: "transparent",
					fontWeight: "600",
					cursor: "pointer",
				});
				button.type = "button";
				button.setAttribute("aria-expanded", "false");
				button.setAttribute("aria-label", t("Map layers"));
				var panel = el("div", null, { display: "none", padding: "8px 12px 12px", minWidth: "190px" });
				box.appendChild(button);
				box.appendChild(panel);
				button.addEventListener("click", function () {
					var open = panel.style.display === "none";
					panel.style.display = open ? "block" : "none";
					button.setAttribute("aria-expanded", open ? "true" : "false");
					if (open) {
						load_specs().then(render);
					}
				});
				this._panel = panel;
				if (L.DomEvent) {
					L.DomEvent.disableClickPropagation(box);
					L.DomEvent.disableScrollPropagation(box);
				}
				return box;
			},
		});
		var control = new Control();
		control.addTo(map);

		var legend_box = null;
		var LegendControl = L.Control.extend({
			options: { position: "bottomleft" },
			onAdd: function () {
				legend_box = el("div", null, {
					background: "rgba(255,255,255,0.94)",
					borderRadius: "8px",
					boxShadow: "0 1px 5px rgba(0,0,0,0.3)",
					padding: "8px 10px",
					font: "12px/1.35 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
					color: "#1f2328",
					maxWidth: "240px",
					display: "none",
				});
				legend_box.className = "farm-map-legend";
				return legend_box;
			},
		});
		new LegendControl().addTo(map);

		function radio(group, value, label, checked, disabled, note, on) {
			var row = el("label", null, {
				display: "flex",
				alignItems: "center",
				gap: "6px",
				padding: "3px 0",
				opacity: disabled ? "0.5" : "1",
				cursor: disabled ? "default" : "pointer",
			});
			var input = el("input");
			input.type = "radio";
			input.name = group;
			input.value = value;
			input.checked = !!checked;
			input.disabled = !!disabled;
			input.addEventListener("change", on);
			row.appendChild(input);
			row.appendChild(el("span", label));
			if (note) {
				row.title = note;
			}
			return row;
		}

		function heading(text) {
			return el("div", text, {
				fontSize: "11px",
				fontWeight: "600",
				textTransform: "uppercase",
				letterSpacing: "0.04em",
				color: "#57606a",
				margin: "8px 0 2px",
			});
		}

		var group_id = "fml-" + Math.random().toString(36).slice(2, 8);

		function render() {
			var panel = control._panel;
			if (panel) {
				while (panel.firstChild) {
					panel.removeChild(panel.firstChild);
				}
				if (bases.length > 1) {
					panel.appendChild(heading(t("Base map")));
					bases.forEach(function (b) {
						panel.appendChild(
							radio(group_id + "-base", b.key, t(b.label), state.base === b.key, false, null, function () {
								state.base = b.key;
								remember();
								apply_base();
							})
						);
					});
				}
				panel.appendChild(heading(t("Overlay")));
				OVERLAYS.forEach(function (o) {
					var spec = o.key === "none" ? null : spec_for(o.key);
					var unavailable = o.key !== "none" && specs !== null && !(spec && spec.available);
					var note = unavailable ? (spec && spec.reason) || t("Not built on this site yet.") : null;
					panel.appendChild(
						radio(group_id + "-overlay", o.key, t(o.label), state.overlay === o.key, unavailable, note, function () {
							state.overlay = o.key;
							remember();
							apply_overlay();
						})
					);
				});
				var current = state.overlay === "none" ? null : spec_for(state.overlay);
				if (state.overlay !== "none" && specs !== null && !(current && current.available)) {
					panel.appendChild(
						el("div", (current && current.reason) || t("The slope map isn't built yet."), {
							color: "#9a6700",
							fontSize: "12px",
							marginTop: "4px",
						})
					);
				}
				panel.appendChild(heading(t("Opacity")));
				var range = el("input", null, { width: "100%" });
				range.type = "range";
				range.min = String(MIN_OPACITY * 100);
				range.max = "100";
				range.value = String(Math.round(state.opacity * 100));
				range.setAttribute("aria-label", t("Overlay opacity"));
				range.disabled = state.overlay === "none";
				range.addEventListener("input", function () {
					state.opacity = clamp_opacity(Number(range.value) / 100);
					if (terrain_layer) {
						terrain_layer.setOpacity(state.opacity);
					}
				});
				range.addEventListener("change", remember);
				panel.appendChild(range);
				var legend_row = el("label", null, { display: "flex", alignItems: "center", gap: "6px", marginTop: "6px" });
				var check = el("input");
				check.type = "checkbox";
				check.checked = state.legend;
				check.addEventListener("change", function () {
					state.legend = check.checked;
					remember();
					render();
				});
				legend_row.appendChild(check);
				legend_row.appendChild(el("span", t("Show legend")));
				panel.appendChild(legend_row);
			}
			render_legend();
		}

		function render_legend() {
			if (!legend_box) {
				return;
			}
			while (legend_box.firstChild) {
				legend_box.removeChild(legend_box.firstChild);
			}
			var spec = state.overlay === "none" ? null : spec_for(state.overlay);
			var show = state.legend && spec && spec.available;
			legend_box.style.display = show || problem ? "block" : "none";
			if (problem) {
				legend_box.appendChild(el("div", problem, { color: "#9a6700", marginBottom: show ? "6px" : "0" }));
			}
			if (!show) {
				return;
			}
			legend_box.appendChild(
				el("div", spec.key === "slope_grade" ? t("How steep the ground is") : t("Which way the ground faces"), {
					fontWeight: "600",
					marginBottom: "4px",
				})
			);
			var cover = coverage(rings, spec.bounds);
			if (cover === "none") {
				legend_box.appendChild(
					el("div", t("The slope map isn't built over this ground yet. The office can rebuild it to take this in."), {
						color: "#9a6700",
						marginBottom: "4px",
					})
				);
			} else if (cover === "partial") {
				legend_box.appendChild(
					el("div", t("Part of this ground is outside the slope map's build."), { color: "#57606a", marginBottom: "4px" })
				);
			}
			var holder = el("div", null, { display: "flex", gap: "10px", alignItems: "flex-start" });
			if (spec.key === "slope_aspect") {
				var wheel = el("div", null, {
					width: "44px",
					height: "44px",
					borderRadius: "50%",
					flex: "0 0 44px",
					position: "relative",
				});
				wheel.style.background = wheel_gradient(spec);
				var hole = el("div", "N", {
					position: "absolute",
					inset: "9px",
					borderRadius: "50%",
					background: "#fff",
					textAlign: "center",
					fontSize: "10px",
					fontWeight: "700",
					lineHeight: "26px",
				});
				wheel.appendChild(hole);
				wheel.setAttribute("aria-hidden", "true");
				holder.appendChild(wheel);
			}
			var list = el("div");
			legend_rows(spec).forEach(function (row) {
				var line = el("div", null, { display: "flex", alignItems: "center", gap: "6px", padding: "1px 0" });
				line.appendChild(el("span", null, { width: "12px", height: "12px", borderRadius: "2px", background: row.colour, flex: "0 0 12px" }));
				line.appendChild(el("span", t(row.label) + (row.range ? " " + row.range : "")));
				if (row.dozer) {
					line.appendChild(el("span", t("Dozer: sign-off"), { color: "#9a6700", fontWeight: "600", fontSize: "11px" }));
				}
				list.appendChild(line);
			});
			holder.appendChild(list);
			legend_box.appendChild(holder);
		}

		apply_base();
		if (state.overlay !== "none") {
			apply_overlay();
		} else {
			render();
		}

		return {
			state: function () {
				return { base: state.base, overlay: state.overlay, opacity: state.opacity, legend: state.legend };
			},
			set_overlay: function (key) {
				state.overlay = normalise_overlay(key);
				remember();
				apply_overlay();
			},
			set_base: function (key) {
				if (base_keys.indexOf(key) >= 0) {
					state.base = key;
					remember();
					apply_base();
				}
			},
			bases: bases,
		};
	}

	root.FarmMapLayers = {
		install: install,
		normalise_overlay: normalise_overlay,
		rings_of: rings_of,
		coverage: coverage,
		initial_state: initial_state,
		legend_rows: legend_rows,
		wheel_gradient: wheel_gradient,
		tile_url: tile_url,
		clamp_opacity: clamp_opacity,
		DOZER_SIGN_OFF_LEVEL: DOZER_SIGN_OFF_LEVEL,
	};
})(typeof window !== "undefined" ? window : this);
