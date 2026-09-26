// SPDX-License-Identifier: MIT
//
// /app/farm-overview — every boundary and every building on one satellite map.
// See erpnext_mcp/farm_overview.py, which argues the whole thing. The short
// version of what this file is allowed to do:
//
//   * It CALLS one whitelisted method and draws what comes back. It decides
//     nothing about who may see which register, which entity is in scope, or
//     whether a stored polygon is readable — every one of those is answered by
//     the server, and the answers arrive with the sentence to show.
//
//   * IT WRITES NOTHING. There is no draw tool on this page and no save path,
//     which is not an oversight: `api/gis.save_boundary` compares a polygon
//     against ONE record's recorded acreage before it commits, and a map of
//     forty blocks has no record in front of it. Every popup carries a link to
//     the form, which is where the boundary editor already lives.
//
// THE LIBRARY AND THE TILES COME FROM `geo_map_widget.js` AND NOT FROM HERE.
// That file has held the CDN URL, both tile URLs, both attributions and the zoom
// defaults since v0.32.0, and its own docstring says why: "seven copies of a
// Leaflet bootstrap is seven places for the CDN URL, the tile attribution and
// the zoom defaults to drift apart, and the first symptom of the drift is one
// form that mysteriously has no map." This page is the eighth caller and it
// copies none of them — v0.110.0 exports `load_leaflet` and `add_base_layers` on
// `erpnext_mcp.geo_map` for exactly this. The tile attributions are the CONDITION
// OF USE for Esri and OpenStreetMap rather than a courtesy, and a second copy
// here would be a second place for one to be quietly dropped.
//
// THE WIDGET IS FETCHED BY THIS PAGE RATHER THAN HOOKED IN. `doctype_js` puts it
// on the seven forms that carry geometry and there is no `app_include_js` — both
// deliberate, per hooks.py, because either would put the asset on every Desk page
// for every user. A Page is not a form, so it fetches the file itself from the
// app's own asset path, same origin, and the browser has it cached already for
// anybody who has opened a Field this session.
//
// IT DEGRADES RATHER THAN FAILING, which is the same posture the widget takes on
// a bench with no outbound internet: no Leaflet means the boundaries and the
// buildings are printed as a table of names and centroids with links to the
// records. The records are the coordinates; the map is a reading of them, and
// losing the reading must not look like losing the farm.

frappe.pages["farm-overview"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Farm Overview"),
		single_column: true,
	});
	wrapper.farm_overview = erpnext_mcp_farm_overview(page);
};

frappe.pages["farm-overview"].on_page_show = function (wrapper) {
	// A boundary somebody just traced on a Field form is the first thing they
	// come back here to look at, so the whole answer is re-read on every return
	// rather than only at load. Nothing on this page is cached server-side for
	// the same reason.
	if (wrapper.farm_overview) {
		wrapper.farm_overview.reload();
	}
};

function erpnext_mcp_farm_overview(page) {
	const METHOD = "erpnext_mcp.farm_overview.farm_overview";

	//: Where Frappe serves this app's `public/` directory. `bench build` links
	//: `apps/erpnext_mcp/erpnext_mcp/public` to `sites/assets/erpnext_mcp`, which
	//: is the same path an `app_include_js` entry would name and the same one the
	//: seven form scripts are served from.
	const WIDGET = "/assets/erpnext_mcp/js/geo_map_widget.js";

	//: How long to wait for the widget before printing the table instead. The
	//: same eight seconds `geo_map_widget.js` allows the CDN, and for the same
	//: reason: past any working connection, short enough that a bench with no
	//: network does not look hung.
	const LOAD_TIMEOUT_MS = 8000;

	//: The pin every structure gets. `L.marker` with no icon reaches for a sprite
	//: at a path relative to the stylesheet, which resolves against the CDN and
	//: is one more request that can fail on its own; a circle marker is drawn by
	//: Leaflet itself and cannot 404.
	const MARKER_STYLE = {
		radius: 6,
		color: "#bc4c00",
		fillColor: "#fb8500",
		fillOpacity: 0.95,
		weight: 2,
	};

	//: v0.161.0. THREE KINDS OF PIN AND THREE SHAPES, not three colours of the
	//: same shape. Colour is already spoken for twice on this map — the register
	//: a polygon belongs to, and the urgency of a job — so a reader who has to
	//: tell a cabin from a valve from a task by hue alone is being asked to hold
	//: three colour keys at once. Shape is the axis that was free:
	//:
	//:   structures  a plain circle          (unchanged since v0.110.0)
	//:   assets      a round badge, lettered by type
	//:   tasks       a diamond, coloured by urgency
	//:
	//: EVERY ONE OF THEM IS DRAWN BY LEAFLET OR BY CSS AND NONE IS AN IMAGE.
	//: `L.marker` with no icon reaches for a sprite at a path relative to the
	//: stylesheet, which resolves against the CDN — one more request that can
	//: fail on its own, on a page whose whole posture is that a missing library
	//: must not look like a missing farm.
	const ASSET_ICON_SIZE = 22;
	const TASK_ICON_SIZE = 16;

	let map = null;
	let loading_widget = null;
	let answer = null;
	let company = null;
	// Which operational layer is drawn, or null for none. ONE AT A TIME AND NONE
	// BY DEFAULT — farm_overview.py argues both: five overlapping colour schemes
	// on one polygon is a map nobody reads the important one off, and the layers
	// cost register reads that somebody opening this page to check a boundary
	// should not pay for.
	let overlay = null;
	// v0.185.0. Which slope raster is on — "slope_aspect", "slope_grade" or null.
	// Held here rather than read off the map because the map is rebuilt on every
	// reload, and a layer somebody switched on should survive picking an entity.
	// OFF BY DEFAULT: a raster over the whole farm hides the imagery a boundary
	// check needs, and nobody opening the page to look at a block asked for it.
	let terrain = null;

	function esc(value) {
		return String(value === null || value === undefined ? "" : value)
			.replace(/&/g, "&amp;")
			.replace(/</g, "&lt;")
			.replace(/>/g, "&gt;")
			.replace(/"/g, "&quot;");
	}

	let body;
	try {
		body = $(frappe.render_template("farm_overview", {})).appendTo(page.body);
	} catch (error) {
		// The template ships beside this file and is compiled into the page's own
		// script by Frappe. If it is not there the page cannot render at all, and
		// saying so beats an empty white panel and a console nobody opens.
		$(page.body).html(
			`<div class="text-muted" style="padding:20px">${__(
				"This page&#39;s template did not load. Run bench build and reload."
			)}</div>`
		);
		// eslint-disable-next-line no-console
		console.error(error);
		return null;
	}

	const $company_row = body.find(".fo-company-row");
	const $company = body.find("#fo-company");
	const $overlay_row = body.find(".fo-overlay-row");
	const $overlay = body.find("#fo-overlay");
	const $summary = body.find(".fo-summary");
	const $notices = body.find(".fo-notices");
	const $panel = body.find(".fo-panel");
	const $canvas = body.find(".fo-map");
	const $legend = body.find(".fo-legend");
	const $terrain_legend = body.find(".fo-terrain-legend");

	page.set_primary_action(__("Refresh"), () => reload(), "refresh");

	$company.on("change", function () {
		company = $(this).val() || null;
		reload();
	});

	// A RE-READ AND NOT A CLIENT-SIDE RECOLOUR. The layer is computed server-side
	// against registers that change while somebody is looking at the page — a
	// valve shut two minutes ago is the whole point — so picking a layer asks the
	// server for it rather than switching between snapshots the browser is
	// holding. It is also what keeps the unasked layer's queries from running.
	$overlay.on("change", function () {
		overlay = $(this).val() || null;
		reload();
	});

	/** The widget, loaded once per page. Resolves with `erpnext_mcp.geo_map`.
	 *
	 * ALREADY THERE ON ANY SESSION THAT OPENED A FORM FIRST. Every `doctype_js`
	 * entry inlines this file into that form's script, and the widget installs
	 * itself on the `erpnext_mcp.geo_map` namespace — so the check below is a
	 * cache hit far more often than it is a fetch.
	 */
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
				if (window.erpnext_mcp && erpnext_mcp.geo_map && erpnext_mcp.geo_map.load_leaflet) {
					resolve(erpnext_mcp.geo_map);
				} else {
					// The file was served and did not install what this page reads.
					// That is a build that has not run since v0.110.0, and it is
					// worth its own sentence: it looks identical to no network.
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

	function reload() {
		page.set_indicator(__("Loading"), "orange");
		return frappe
			.call({ method: METHOD, args: { company: company, overlay: overlay } })
			.then((response) => {
				answer = (response && response.message) || null;
				if (!answer) {
					page.set_indicator(__("No answer"), "red");
					return;
				}
				company = answer.company || null;
				render();
			})
			.catch((error) => {
				// A rejected promise nobody listened to is the worst shape this
				// bug takes: the spinner stops, the old map stays on screen and
				// nothing says the numbers under it are stale. Frappe has already
				// shown its own modal for a server refusal, so this only marks the
				// page rather than speaking over it.
				page.set_indicator(__("Not loaded"), "red");
				// eslint-disable-next-line no-console
				console.error(error);
			});
	}

	function render() {
		render_company_picker();
		render_overlay_picker();
		render_summary();
		render_notices();
		render_legend();
		draw();
	}

	/** The layer picker, drawn from what this login's roles allow.
	 *
	 * THE OPTIONS COME FROM THE SERVER AND ARE NOT A HARDCODED LIST. That is the
	 * same mistake `roles.ROLE_INDICATORS` was written to undo on the handset: a
	 * copy of the app's vocabulary compiled into a client goes stale the release
	 * a layer is added, and the symptom is a picker that silently cannot reach a
	 * feature the server has.
	 */
	function render_overlay_picker() {
		const specs = answer.overlay_layers || [];
		if (!specs.length) {
			// Every role gets at least restricted entry, so an empty list means a
			// server older than v0.116.0 rather than a login with nothing to see.
			$overlay_row.hide();
			return;
		}
		const chosen = (answer.overlay || {}).key || "";
		const rows = [`<option value="">${esc(__("None — boundaries only"))}</option>`].concat(
			specs.map(
				(spec) =>
					`<option value="${esc(spec.key)}"${
						spec.key === chosen ? " selected" : ""
					} title="${esc(spec.detail || "")}">${esc(spec.label)}</option>`
			)
		);
		$overlay.html(rows.join(""));
		$overlay_row.show();
	}

	function render_company_picker() {
		const companies = answer.companies || [];
		// ONE ENTITY IS NOT A CHOICE. A picker with a single option on it is a
		// control that can only be set to what it already says, and every farm
		// this app was written for has exactly one.
		if (companies.length < 2) {
			$company_row.hide();
			return;
		}
		const options = companies
			.map(
				(name) =>
					`<option value="${esc(name)}"${name === answer.company ? " selected" : ""}>${esc(
						name
					)}</option>`
			)
			.join("");
		$company.html(options);
		$company_row.show();
	}

	function render_summary() {
		// LABEL FIRST AND THE COUNT AFTER IT, which is not a style choice. The
		// labels are plural nouns run through `__()`, so "{count} {label}" reads
		// "1 parcels" the moment a farm has one of something — and there is no
		// honest way to fix that by trimming an "s", because the translated label
		// is not English and does not pluralise the way English does. Putting the
		// number after the name sidesteps the whole question in every language.
		const parts = (answer.layers || []).map((layer) => `${esc(layer.label)} ${layer.drawn}`);
		const housing = answer.housing || {};
		if (housing.drawn) {
			parts.push(`${esc(housing.label)} ${housing.drawn}`);
		}
		const assets = answer.asset_summary || {};
		if (assets.drawn) {
			parts.push(`${esc(assets.label)} ${assets.drawn}`);
		}
		const tasks = answer.task_summary || {};
		if (tasks.drawn) {
			parts.push(`${esc(tasks.label)} ${tasks.drawn}`);
		}
		// THE OVERLAY'S COUNTS COME FIRST WHEN THERE IS ONE, because they are the
		// numbers somebody opened this page for. "Three restricted, two ready to
		// pick" is a morning; "Fields 40" is a map.
		const live = overlay_counts();
		const line = live.length
			? __("{0} — {1}", [live.join(" · "), parts.join(" · ")])
			: parts.join(" · ");
		$summary.text(parts.length || live.length ? __("Drawn — {0}", [line]) : "");
	}

	/** The three or four numbers the chosen layer actually produced. */
	function overlay_counts() {
		const drawn = answer.overlay;
		if (!drawn) {
			return [];
		}
		const counts = drawn.counts || {};
		const wanted = [
			["restricted", __("restricted")],
			["pre_harvest", __("in pre-harvest")],
			["ready_to_pick", __("ready to pick")],
			["unscouted", __("never scouted")],
			["irrigating", __("watering now")],
			["too_wet", __("too wet")],
			["access_blocked", __("closed to equipment")],
		];
		return wanted
			.filter(([key]) => counts[key])
			.map(([key, label]) => `${counts[key]} ${label}`);
	}

	function render_legend() {
		const entries = (answer.layers || []).map((layer) => {
			const missing = layer.without_boundary
				? ` <span class="fo-legend-count">${esc(
						__("({0} without a boundary)", [layer.without_boundary])
					)}</span>`
				: "";
			return `<div class="fo-legend-entry">
					<span class="fo-swatch" style="background:${esc(layer.colour)}"></span>
					<span>${esc(layer.label)} — ${layer.drawn}</span>${missing}
				</div>`;
		});
		const housing = answer.housing || {};
		if (housing.total) {
			const missing = housing.without_position
				? ` <span class="fo-legend-count">${esc(
						__("({0} without a position)", [housing.without_position])
					)}</span>`
				: "";
			entries.push(`<div class="fo-legend-entry">
					<span class="fo-swatch" style="background:${MARKER_STYLE.fillColor};border-radius:50%;width:12px"></span>
					<span>${esc(housing.label)} — ${housing.drawn}</span>${missing}
				</div>`);
		}
		// v0.161.0. THE TWO NEW LAYERS, EACH KEYED BY THE THING THAT VARIES.
		// An asset's marker varies by TYPE and a task's by URGENCY, so the two
		// legends are built differently on purpose — one swatch per asset type
		// that is actually on this map, and one per urgency that actually has a
		// job in it. A key listing colours nothing on screen is using is a key
		// somebody has to check against the map rather than read.
		const assets = answer.asset_summary || {};
		const icons = assets.icons || {};
		Object.keys(assets.by_asset_type || {}).forEach((kind) => {
			const icon = icons[kind] || icons[""] || {};
			entries.push(`<div class="fo-legend-entry">
					<span class="fo-asset-swatch" style="background:${esc(icon.colour)}">${esc(
						icon.glyph || "A"
					)}</span>
					<span>${esc(icon.label || kind)} — ${assets.by_asset_type[kind]}</span>
				</div>`);
		});
		if (assets.without_position) {
			entries.push(`<div class="fo-legend-entry"><span class="fo-legend-count">${esc(
				__("{0} asset(s) without a position", [assets.without_position])
			)}</span></div>`);
		}

		const tasks = answer.task_summary || {};
		const colours = tasks.colours || {};
		(tasks.urgency_order || []).forEach((urgency) => {
			const count = (tasks.by_urgency || {})[urgency];
			if (!count) {
				return;
			}
			entries.push(`<div class="fo-legend-entry">
					<span class="fo-task-swatch" style="background:${esc(colours[urgency])}"></span>
					<span>${esc(urgency)} — ${count}</span>
				</div>`);
		});

		$legend.html(entries.join(""));
		render_overlay_legend();
	}

	/** The status key for the chosen layer, under the register legend.
	 *
	 * BUILT FROM THE STATUSES ACTUALLY ON THE MAP rather than from a fixed list.
	 * A farm with nothing restricted should not be shown a red "restricted"
	 * swatch it can find no shape for — and a status this build has never heard
	 * of still appears, in whatever colour the server gave it, instead of being
	 * dropped by a client that is older than its server.
	 */
	function render_overlay_legend() {
		body.find(".fo-overlay-legend, .fo-overlay-note").remove();
		const index = overlay_index();
		if (!index.key) {
			return;
		}
		const seen = new Map();
		Object.keys(index.by_name).forEach((name) => {
			const state = index.by_name[name];
			if (state && state.status) {
				seen.set(state.status, state.colour || "#8c959f");
			}
		});
		const swatches = Array.from(seen.entries()).map(
			([status, colour]) =>
				`<div class="fo-legend-entry">
					<span class="fo-swatch" style="background:${esc(colour)}"></span>
					<span>${esc(String(status).replace(/_/g, " "))}</span>
				</div>`
		);
		const spec = (answer.overlay_layers || []).find((entry) => entry.key === index.key) || {};
		$legend.after(
			`<div class="fo-overlay-legend">
				<div class="fo-legend-entry"><strong>${esc(spec.label || index.key)}</strong></div>
				${swatches.join("")}
			</div>
			<div class="fo-overlay-note">${esc(spec.detail || "")}</div>`
		);
	}

	/** Everything the server said was wrong, above the map rather than in a log.
	 *
	 * THE THREE ARE DIFFERENT KINDS OF WRONG AND ARE NOT MERGED. A layer this
	 * login may not read is a permission an operator sets; a boundary that will
	 * not parse is one record somebody has to go and fix; a register at the cap
	 * is a map that is honestly incomplete. Rolling them into one "some things
	 * are missing" line would make all three easy to ignore.
	 */
	function render_notices() {
		const notes = [];

		const refused = answer.refused || [];
		if (refused.length) {
			notes.push(
				`<div class="fo-note fo-note-warn"><p>${esc(
					__("This login may not read: {0}. Those layers are not on the map.", [
						refused.join(", "),
					])
				)}</p></div>`
			);
		}

		// v0.161.0. A JOB THAT IS NOT ON THE MAP IS A FOURTH KIND OF WRONG, and
		// it is two different jobs of work depending on which way it failed.
		// A task naming no location is a dispatch gap — somebody raised it
		// without saying where. A task whose ground has never been traced is a
		// BOUNDARY somebody owes, and the task is fine. Merging them into "6
		// tasks not shown" would leave whoever reads it unable to act on either.
		const task_summary = answer.task_summary || {};
		if (task_summary.without_location) {
			notes.push(
				`<div class="fo-note fo-note-warn"><p>${esc(
					__(
						"{0} open task(s) name no location, so they are not on the map. They are on the dispatch board.",
						[task_summary.without_location]
					)
				)}</p></div>`
			);
		}
		const unplaceable = task_summary.unplaceable || [];
		if (unplaceable.length) {
			const rows = unplaceable
				.map(
					(entry) =>
						`<li><a href="${esc(entry.route)}">${esc(
							entry.task_name || entry.name
						)}</a> — ${esc(entry.location_doctype)} ${esc(entry.location)}</li>`
				)
				.join("");
			notes.push(
				`<div class="fo-note fo-note-warn"><p>${esc(
					__(
						"{0} open task(s) are about ground that has never been traced or stood at, so there is nowhere to put the pin:",
						[unplaceable.length]
					)
				)}</p><ul>${rows}</ul></div>`
			);
		}

		const unreadable = answer.unreadable || [];
		if (unreadable.length) {
			const rows = unreadable
				.map(
					(entry) =>
						`<li><a href="${esc(entry.route)}">${esc(entry.label)}</a> (${esc(
							entry.doctype
						)}) — ${esc(entry.reason)}</li>`
				)
				.join("");
			notes.push(
				`<div class="fo-note fo-note-bad"><p>${esc(
					__(
						"{0} boundary/boundaries are stored but could not be read, so they are not drawn. A boundary that does not draw looks exactly like one that was never traced:",
						[unreadable.length]
					)
				)}</p><ul>${rows}</ul></div>`
			);
		}

		const capped = answer.capped || [];
		if (capped.length) {
			notes.push(
				`<div class="fo-note fo-note-warn"><p>${esc(
					__("{0} reached the {1}-row ceiling, so this map may not show all of it.", [
						capped.join(", "),
						answer.cap,
					])
				)}</p></div>`
			);
		}

		// The chosen layer's own problems, kept separate from the boundary map's
		// for the same reason the three above are kept separate from each other:
		// a layer this login may not pick, a register the overlay could not read
		// and a register that hit its ceiling are three different jobs.
		(answer.overlay_refused || []).forEach((entry) => {
			notes.push(
				`<div class="fo-note fo-note-warn"><p>${esc(
					__("The {0} layer was not drawn — {1}", [entry.key, entry.reason])
				)}</p></div>`
			);
		});
		const drawn = answer.overlay || {};
		(drawn.refused || []).forEach((doctype) => {
			notes.push(
				`<div class="fo-note fo-note-warn"><p>${esc(
					__(
						"This login may not read {0}, so the operational layer over it is blank rather than clear.",
						[doctype]
					)
				)}</p></div>`
			);
		});
		(drawn.warnings || []).forEach((warning) => {
			notes.push(`<div class="fo-note fo-note-warn"><p>${esc(warning)}</p></div>`);
		});

		// v0.185.0. ONE LINE FOR BOTH SLOPE LAYERS, because they are one build:
		// grade reads the terrain aspect caches. An unbuilt layer gets no toggle,
		// so this sentence is the only place a farm learns the layers exist.
		const unbuilt = (answer.terrain_layers || []).filter((spec) => !spec.available);
		if (unbuilt.length) {
			notes.push(
				`<div class="fo-note fo-note-warn"><p>${esc(
					__("The slope layers ({0}) are not on this map: {1}", [
						unbuilt.map((spec) => spec.label).join(", "),
						unbuilt[0].reason || "",
					])
				)}</p></div>`
			);
		}

		$notices.html(notes.join(""));
	}

	/** `{docname: the chosen layer's dict}` for whichever register it colours.
	 *
	 * BUILT ONCE PER RENDER AND NOT LOOKED UP PER SHAPE. A farm at the 500-row
	 * cap with a linear scan per polygon is a quarter of a million comparisons
	 * to draw one map, on the machine in the office.
	 *
	 * THE SUBJECT DECIDES WHICH REGISTER IS INDEXED, and it comes from the
	 * server. An irrigation set is a ZONE fact and a restricted entry is a BLOCK
	 * fact; keying either on the other's docnames would silently colour nothing,
	 * which looks exactly like a farm with no restrictions on it.
	 */
	function overlay_index() {
		const drawn = answer.overlay;
		if (!drawn) {
			return { key: null, subject: null, by_name: {} };
		}
		const by_name = {};
		if (drawn.subject === "zone") {
			(drawn.zones || []).forEach((entry) => {
				by_name[entry.zone] = entry;
			});
		} else {
			(drawn.blocks || []).forEach((entry) => {
				by_name[entry.name] = entry[drawn.key] || null;
			});
		}
		return { key: drawn.key, subject: drawn.subject, by_name: by_name };
	}

	/** Which doctype the chosen layer paints. See `overlay_index`. */
	function overlay_doctype(index) {
		return index.subject === "zone" ? "Irrigation Zone" : "Field";
	}

	/** The overlay dict for one shape, or null if this layer does not paint it. */
	function overlay_for(index, entry) {
		if (!index.key || entry.doctype !== overlay_doctype(index)) {
			return null;
		}
		return index.by_name[entry.name] || null;
	}

	/** The lines the chosen layer adds to a popup, as one block of markup.
	 *
	 * WHAT IS PRINTED IS WHAT THE SERVER SAID, in the server's own words. Every
	 * `warning` and every `reason` on these dicts is a sentence written once —
	 * `spray_rei.warning_line` is emphatic that a worker reading one wording at a
	 * gate and another on a work order has been given two rules — and this page
	 * is one more screen it is read off, not a second author of it.
	 */
	function overlay_popup(state, key) {
		if (!state) {
			return "";
		}
		const lines = [];
		if (key === "irrigation") {
			lines.push(overlay_status_line(state));
			if (state.hours_since_water_off !== null && state.hours_since_water_off !== undefined) {
				lines.push(__("{0} hours since the water came off", [state.hours_since_water_off]));
			}
			if (state.status === "irrigating" && (state.open_valves || []).length) {
				lines.push(__("Open now: {0}", [state.open_valves.join(", ")]));
			}
			if (state.red_hours) {
				lines.push(
					__("Red under {0}h, yellow under {1}h ({2})", [
						state.red_hours,
						state.yellow_hours,
						state.soil_profile || __("shipped default"),
					])
				);
			}
		} else if (key === "spray_rei" || key === "spray_phi") {
			lines.push(state.warning || overlay_status_line(state));
		} else if (key === "harvest") {
			lines.push(overlay_status_line(state));
			if (state.growth_stage_code) {
				lines.push(__("BBCH {0} — {1}", [state.growth_stage_code, state.stage_label || ""]));
			}
			if (state.brix !== null && state.brix !== undefined) {
				lines.push(
					__("{0}° Brix ({1}) against a target of {2}", [
						state.brix,
						state.brix_method || __("method not recorded"),
						state.target === null || state.target === undefined
							? __("none recorded")
							: state.target,
					])
				);
			}
			if (state.short_of) {
				lines.push(__("Short of: {0}", [state.short_of]));
			}
			if (state.stale) {
				lines.push(__("Last walked {0} days ago", [state.observed_days_ago]));
			}
		} else if (key === "equipment_access") {
			lines.push(overlay_status_line(state));
			if (state.decided_by) {
				lines.push(__("Decided by the {0} layer", [state.decided_by]));
			}
			(state.notes || []).forEach((note) => lines.push(note));
		}
		if (state.reason) {
			lines.push(state.reason);
		}
		const body = lines
			.filter(Boolean)
			.map((line) => `<div>${esc(line)}</div>`)
			.join("");
		return `<div class="fo-popup-overlay" style="border-left:3px solid ${esc(
			state.colour || "#8c959f"
		)};padding-left:6px"><strong>${esc(overlay_label())}</strong>${body}</div>`;
	}

	function overlay_status_line(state) {
		return __("Status: {0}", [String(state.status || "unknown").replace(/_/g, " ")]);
	}

	function overlay_label() {
		const drawn = answer.overlay || {};
		const spec = (answer.overlay_layers || []).find((entry) => entry.key === drawn.key);
		return spec ? spec.label : drawn.key || "";
	}

	function popup(entry, layer, overlay_state) {
		const figures = [];
		if (entry.acres) {
			figures.push(__("{0} acres recorded", [entry.acres]));
		}
		if (entry.computed_acres) {
			figures.push(__("{0} enclosed", [entry.computed_acres]));
		}
		return `<div>
				<div class="fo-popup-title">${esc(entry.label)}</div>
				<div class="fo-popup-detail">${esc(layer ? layer.label : entry.doctype)}${
					entry.detail ? " · " + esc(entry.detail) : ""
				}</div>
				<div class="fo-popup-figures">${esc(figures.join(" · "))}</div>
				${overlay_popup(overlay_state, (answer.overlay || {}).key)}
				<a href="${esc(entry.route)}">${esc(__("Open {0}", [entry.name]))}</a>
			</div>`;
	}

	/** One asset's popup: what it is, what it is doing, when it was last seen.
	 *
	 * THE SCAN LINE IS THE ONE WORTH READING and it is the reason the asset layer
	 * is on this map at all. A tag whose last scan is in March is either a
	 * machine nobody has touched since March or a tag nobody can find, and both
	 * are answered by walking to the pin.
	 */
	function asset_popup(entry) {
		const lines = [];
		if (entry.current_state) {
			lines.push(__("State: {0}", [entry.current_state]));
		}
		lines.push(
			entry.last_scan_at
				? __("Last scanned {0}", [entry.last_scan_at]) +
						(entry.last_scan_by ? " · " + entry.last_scan_by : "")
				: // NOT AN EMPTY LINE. "Never scanned" is a fact about the tag and
					// is the single most actionable thing this popup can say; a blank
					// where it should be reads as a popup that failed to load.
					__("Never scanned")
		);
		if (entry.last_service_date) {
			lines.push(__("Serviced {0}", [entry.last_service_date]));
		}
		if (entry.parent_asset) {
			lines.push(__("Under {0}", [entry.parent_asset]));
		}
		return `<div>
				<div class="fo-popup-title">${esc(entry.label)}</div>
				<div class="fo-popup-detail">${esc(entry.icon_label)}${
					entry.description ? " · " + esc(entry.description) : ""
				}</div>
				<div class="fo-popup-figures">${lines.map(esc).join("<br>")}</div>
				<a href="${esc(entry.route)}">${esc(__("Open {0}", [entry.name]))}</a>
			</div>`;
	}

	/** One task's popup, with a link to the job AND to the ground it is about.
	 *
	 * BOTH LINKS, because they answer different questions. Somebody looking at a
	 * red diamond wants either "what is this job" or "which block is this" — and
	 * the second is the one a map is uniquely bad at answering, since the pin
	 * sits at a centroid with nothing written on it.
	 */
	function task_popup(entry) {
		const lines = [];
		lines.push(__("{0} · {1}", [entry.task_type || __("Task"), entry.state || ""]));
		lines.push(
			entry.assigned_to
				? __("Assigned to {0}", [entry.assigned_to_name || entry.assigned_to])
				: // The pool is a state somebody acts on, so it is said rather than
					// left as an absent line.
					__("Nobody has claimed it")
		);
		if (entry.skill_required) {
			lines.push(__("Needs {0}", [entry.skill_required]));
		}
		return `<div>
				<div class="fo-popup-title">${esc(entry.label)}</div>
				<div class="fo-popup-detail">${esc(entry.urgency)}</div>
				<div class="fo-popup-figures">${lines.map(esc).join("<br>")}</div>
				<a href="${esc(entry.route)}">${esc(__("Open {0}", [entry.name]))}</a>
				&nbsp;·&nbsp;
				<a href="${esc(entry.location_route)}">${esc(entry.location)}</a>
			</div>`;
	}

	/** A round lettered badge for an asset, drawn in CSS. See ASSET_ICON_SIZE. */
	function asset_icon(L, entry) {
		return L.divIcon({
			className: "fo-asset-icon",
			html: `<span style="background:${esc(entry.colour)}">${esc(entry.icon)}</span>`,
			iconSize: [ASSET_ICON_SIZE, ASSET_ICON_SIZE],
			iconAnchor: [ASSET_ICON_SIZE / 2, ASSET_ICON_SIZE / 2],
		});
	}

	/** A diamond for a task, coloured by urgency. */
	function task_icon(L, entry) {
		return L.divIcon({
			className: "fo-task-icon",
			html: `<span style="background:${esc(entry.colour)}"></span>`,
			iconSize: [TASK_ICON_SIZE, TASK_ICON_SIZE],
			iconAnchor: [TASK_ICON_SIZE / 2, TASK_ICON_SIZE / 2],
		});
	}

	/** Re-measure and re-fit the map until the browser has actually laid it out.
	 *
	 * LEAFLET MEASURES ITS CONTAINER ONCE, when the map is created, and on the
	 * first load of this page that moment is before the panel has been laid out.
	 * The map is built believing it is NOUGHT PIXELS WIDE — measured on
	 * 2026-08-20 as `getSize() = {x: 0, y: 620}` while the element's own
	 * `offsetHeight` already read 620.
	 *
	 * AND THE VIEW HAS TO BE RE-APPLIED, NOT JUST THE SIZE, which is the half
	 * that is easy to miss. `invalidateSize` corrects the measurement and keeps
	 * the centre and zoom the map already has — and that zoom was computed
	 * against a container of no width, which `getBoundsZoom` answers with ZERO.
	 * The symptom is a map of the whole world with a farm-sized speck on it, on
	 * first load only, correcting itself the instant anything else redraws. That
	 * is the worst shape for this bug to take: it is invisible to whoever is
	 * testing, because they click something.
	 *
	 * A RESIZE OBSERVER AND NOT A `setTimeout`, because the number of
	 * milliseconds to wait is not knowable — it is however long this browser
	 * takes to lay out a panel it has just been handed, on this machine, with
	 * these fonts. A 120ms guess was tried first and still fired against a
	 * zero-width container. The observer fires when the thing it is waiting for
	 * has actually happened, and it also keeps the map honest when the Desk
	 * sidebar is collapsed or the window is dragged narrower.
	 *
	 * The refit is idempotent where the size was right all along, so this costs
	 * nothing on the paths where the bug never existed.
	 */
	function watch_size(instance, apply_view) {
		const element = instance.getContainer();
		const settle = () => {
			if (!element.offsetWidth || !element.offsetHeight) {
				return;
			}
			instance.invalidateSize();
			apply_view();
		};

		if (window.ResizeObserver) {
			const observer = new ResizeObserver(settle);
			observer.observe(element);
			// Stop watching when this map goes, so a session that visits the page
			// ten times is not left with ten observers on ten dead containers.
			instance.on("unload", () => observer.disconnect());
		}
		// The belt to that brace, and the whole story on a browser with no
		// ResizeObserver: one late pass after the current layout has flushed.
		setTimeout(settle, 200);
	}

	function draw() {
		load_widget()
			.then((widget) => widget.load_leaflet().then((L) => ({ L: L, widget: widget })))
			.then(({ L, widget }) => {
				// Leaflet refuses to initialise a container it has already used
				// ("Map container is already initialized"), and this page rebuilds
				// on every return to it and on every entity change. Taking the old
				// map down is what makes the second render work.
				if (map) {
					map.remove();
					map = null;
				}
				$panel.find(".fo-fallback, .fo-empty").remove();
				$canvas.show().empty();

				map = L.map($canvas[0], { scrollWheelZoom: true });

				// v0.161.0. ONE GROUP PER REGISTER, SO EACH CAN BE SWITCHED OFF.
				// A farm with forty blocks, ninety tags and thirty open jobs on
				// one map is unreadable at the zoom where you can see all of it,
				// and the answer is not to draw less — it is to let whoever is
				// looking say what they came for. Every group is ON by default:
				// a map that opened with layers hidden would look like a farm
				// with nothing on it.
				const groups = {};
				(answer.layers || []).forEach((layer) => {
					groups[layer.doctype] = L.layerGroup().addTo(map);
				});
				groups.__markers = L.layerGroup().addTo(map);
				groups.__assets = L.layerGroup().addTo(map);
				groups.__tasks = L.layerGroup().addTo(map);

				const toggles = {};
				(answer.layers || []).forEach((layer) => {
					// A LAYER WITH NOTHING IN IT IS NOT OFFERED. An entry that
					// toggles an empty group is a control that does nothing, and
					// a farm with no irrigation zones registered would get one
					// permanently. The count is still in the legend, where "0" is
					// a fact rather than a dead switch.
					if (!layer.drawn) {
						return;
					}
					toggles[`${layer.label} (${layer.drawn})`] = groups[layer.doctype];
				});
				const housing = answer.housing || {};
				if (housing.drawn) {
					toggles[`${housing.label} (${housing.drawn})`] = groups.__markers;
				}
				const asset_summary = answer.asset_summary || {};
				if (asset_summary.drawn) {
					toggles[`${asset_summary.label} (${asset_summary.drawn})`] = groups.__assets;
				}
				const task_summary = answer.task_summary || {};
				if (task_summary.drawn) {
					toggles[`${task_summary.label} (${task_summary.drawn})`] = groups.__tasks;
				}

				// THE COUNT IS IN THE LABEL, and that is the whole reason this
				// control beats a row of checkboxes of our own. "Fields (31)"
				// answers "did the layer draw nothing, or is it switched off"
				// without unticking anything — which is the question somebody
				// asks the moment a layer looks empty.
				//
				// A LAYER WITH NOTHING IN IT IS NOT OFFERED. An entry that
				// toggles an empty group is a control that does nothing, and
				// three of them is a control nobody trusts.
				const base = widget.add_base_layers(L, map, toggles);
				add_terrain(L, base && base.control);

				// THE VIEW IS SET BEFORE A SINGLE SHAPE IS ADDED, AND THE ORDER IS
				// LOAD-BEARING RATHER THAN TIDY. A Leaflet map has no pixel origin
				// until it has a centre and a zoom, and the SVG renderer a vector
				// layer draws into takes its bounds from that origin — so a
				// `L.geoJSON(...).addTo(map)` on a map with no view throws
				// "Cannot read properties of undefined (reading 'min')" out of
				// `_clipPoints`, deep inside the library, at the moment the view is
				// finally set. Proven on 2026-08-20 against Leaflet 1.9.4: the same
				// eleven shapes draw perfectly with the view set first and throw
				// with it set last.
				//
				// THE BOX IS THE SERVER'S AND NOT A WALK OVER THE LAYERS, which is
				// the other half of why it can come first. `bounds_of` already
				// dropped the coordinates that are not on Earth, so one vertex typed
				// with an extra digit cannot stretch the frame across a continent
				// and draw the whole farm as a dot — the shape is still added below,
				// because it is the record and the record is what somebody has to go
				// and fix.
				const apply_view = () => {
					if (answer.bounds) {
						map.fitBounds(L.latLngBounds(answer.bounds).pad(0.06), {
							maxZoom: widget.MAX_FIT_ZOOM,
						});
					} else {
						const home = widget.HOME_VIEW;
						map.setView(home.centre, home.zoom);
					}
				};
				apply_view();

				if (!answer.bounds) {
					$panel.append(
						`<div class="fo-empty">${esc(
							__(
								"Nothing on this farm has a boundary or a position yet. Trace one on a Parcel, Field or Irrigation Zone form and it appears here."
							)
						)}</div>`
					);
				}

				const index = overlay_index();
				(answer.layers || []).forEach((layer) => {
					(layer.shapes || []).forEach((entry) => {
						if (!entry.geometry) {
							return;
						}
						const state = overlay_for(index, entry);
						// THE REGISTER'S OWN COLOUR STAYS ON EVERY SHAPE THE
						// LAYER DOES NOT PAINT. A restricted-entry overlay
						// colours blocks; the parcels under them and the zones
						// inside them keep their register colours, so the map
						// still reads as a map rather than as five grey shapes
						// and three red ones. The FILL carries the status and
						// the STROKE stays the register's, which is what lets
						// somebody see both facts about one polygon at once.
						const shape = L.geoJSON(entry.geometry, {
							style: {
								color: state ? state.colour : layer.colour,
								weight: state ? layer.weight + 1 : layer.weight,
								fillColor: state ? state.colour : undefined,
								fillOpacity: state ? 0.45 : layer.fill_opacity,
								dashArray: layer.dash_array || undefined,
							},
						})
							.addTo(groups[layer.doctype])
							.bindPopup(popup(entry, layer, state))
							.bindTooltip(
								state
									? esc(entry.label + " — " + String(state.status || ""))
									: esc(entry.label),
								{ sticky: true }
							);

						// v0.161.0. THE TICKER, PAINTED ON THE BLOCK. `block_ticker`
						// is what is on the bin, said on the radio and written on
						// the tally sheet — `field_name` is what is in the
						// database — so it is the label a picker recognises a
						// polygon by without clicking it. A permanent tooltip is
						// Leaflet drawing text at the shape's centre; only Fields
						// have a ticker and only ones that have been given one get
						// a label, so nothing else on the map gains clutter.
						if (entry.block_ticker && entry.centre) {
							L.tooltip({
								permanent: true,
								direction: "center",
								className: "fo-ticker",
							})
								.setContent(esc(entry.block_ticker))
								.setLatLng(entry.centre)
								.addTo(groups[layer.doctype]);
						}
						return shape;
					});
				});

				(answer.markers || []).forEach((entry) => {
					L.circleMarker(entry.point, MARKER_STYLE)
						.addTo(groups.__markers)
						.bindPopup(popup(entry, null, null))
						.bindTooltip(esc(entry.label), { sticky: true });
				});

				(answer.assets || []).forEach((entry) => {
					L.marker(entry.point, { icon: asset_icon(L, entry) })
						.addTo(groups.__assets)
						.bindPopup(asset_popup(entry))
						.bindTooltip(esc(entry.label), { sticky: true });
				});

				// ADDED IN REVERSE, AND THAT IS THE POINT RATHER THAN AN
				// ODDITY. The server sorts `tasks` WORST FIRST, which is the
				// order a list wants — the fallback table below prints it
				// straight through, and so does the legend. A MAP wants the
				// opposite: several jobs on one block share a centroid exactly,
				// Leaflet draws later markers over earlier ones, and the pin that
				// has to be on top and clickable at a block with four jobs on it
				// is the Critical one. So the payload keeps the reading order and
				// this one loop walks it backwards.
				(answer.tasks || [])
					.slice()
					.reverse()
					.forEach((entry) => {
						L.marker(entry.point, { icon: task_icon(L, entry) })
							.addTo(groups.__tasks)
							.bindPopup(task_popup(entry))
							.bindTooltip(esc(entry.label + " — " + entry.urgency), {
								sticky: true,
							});
					});

				watch_size(map, apply_view);
				page.set_indicator(__("Loaded"), "green");
			})
			.catch((error) => {
				render_fallback((error && error.message) || __("unavailable"));
				page.set_indicator(__("No map"), "orange");
			});
	}

	/** The slope rasters, as two entries in the map's own layer control. v0.185.0.
	 *
	 * TILE LAYERS, SO THEY SIT UNDER EVERY SHAPE. Leaflet puts a tile layer in
	 * its tile pane and a polygon in the overlay pane above it, so the blocks,
	 * the pins and whichever operational layer is picked all stay on top and
	 * clickable. The alpha is baked into the pixels server-side, so the imagery
	 * shows through without this page choosing a blend.
	 *
	 * ONE AT A TIME. Aspect and grade both colour every cell of the same ground,
	 * and the two palettes share red; turning one on turns the other off.
	 *
	 * AN UNBUILT LAYER IS NOT OFFERED — a toggle that draws nothing is the
	 * control nobody trusts, see the register toggles above — and the notice
	 * says how to build it. Leaflet itself does not ask for a tile below the
	 * layer's minimum zoom or outside its bounds, so a farm fitted at zoom 10
	 * costs no request until somebody zooms in.
	 */
	function add_terrain(L, control) {
		const specs = (answer.terrain_layers || []).filter((spec) => spec.available);
		if (!control || !control.addOverlay || !specs.length) {
			render_terrain_legend(null);
			return;
		}
		const tiles = {};
		specs.forEach((spec) => {
			const b = spec.bounds;
			tiles[spec.key] = L.tileLayer(spec.tile_url_template, {
				tileSize: spec.tile_size || 256,
				minZoom: spec.min_zoom,
				// Past the deepest zoom the server renders, Leaflet enlarges that
				// zoom's tiles rather than asking for ones that do not exist.
				maxNativeZoom: spec.max_zoom,
				maxZoom: 19,
				bounds: b ? L.latLngBounds([b.south, b.west], [b.north, b.east]) : undefined,
				attribution: esc(spec.source || ""),
			});
			control.addOverlay(tiles[spec.key], spec.label);
		});
		const key_of = (layer) => Object.keys(tiles).find((key) => tiles[key] === layer) || null;
		const owner = map;
		// A MAP BEING TAKEN DOWN IS NOT SOMEBODY UNTICKING A BOX. `map.remove()`
		// — every reload, every entity change — fires `unload` and then removes
		// each layer, and the control reports each removal as `overlayremove`.
		// Left attached, the handler below reads that as "switched off" and the
		// layer is gone after every Refresh. Found on a live bench, 2026-09-26.
		// `unload` comes first, so detaching there is in time.
		owner.on("unload", () => {
			owner.off("overlayadd");
			owner.off("overlayremove");
		});
		map.on("overlayadd", (event) => {
			const key = key_of(event.layer);
			if (!key) {
				return;
			}
			// Set BEFORE the other is removed, so its overlayremove below sees
			// that it is no longer the one on and leaves `terrain` alone.
			terrain = key;
			render_terrain_legend(specs.find((spec) => spec.key === key));
			// THE SIBLING COMES OFF AFTER THE CLICK, NOT DURING IT. Leaflet's
			// `_onInputClick` walks every box and re-adds each ticked layer that
			// is not on the map — so a sibling removed from inside this handler
			// is put straight back by the same walk, whose `overlayadd` then
			// removes THIS layer, and the control is left with both boxes ticked
			// over the wrong raster. Found on a live bench, 2026-09-26. Once the
			// walk is over, a removal also redraws the checkboxes, which a
			// removal inside it does not.
			setTimeout(() => {
				if (terrain !== key || !map) {
					return;
				}
				Object.keys(tiles).forEach((other) => {
					if (other !== key && map.hasLayer(tiles[other])) {
						map.removeLayer(tiles[other]);
					}
				});
			}, 0);
		});
		map.on("overlayremove", (event) => {
			const key = key_of(event.layer);
			if (key && key === terrain) {
				terrain = null;
				render_terrain_legend(null);
			}
		});
		if (terrain && tiles[terrain]) {
			tiles[terrain].addTo(map);
			render_terrain_legend(specs.find((spec) => spec.key === terrain));
		} else {
			terrain = null;
			render_terrain_legend(null);
		}
	}

	/** The key for the slope layer that is on, from the server's own legend. */
	function render_terrain_legend(spec) {
		if (!spec) {
			$terrain_legend.html("").hide();
			return;
		}
		const swatch = (colour, text) =>
			`<div class="fo-legend-entry">
				<span class="fo-swatch" style="background:${esc(colour)}"></span>
				<span>${esc(text)}</span>
			</div>`;
		const rows = [`<div class="fo-legend-entry"><strong>${esc(spec.label)}</strong></div>`];
		if (spec.key === "slope_aspect") {
			(spec.legend || []).forEach((entry) => {
				rows.push(swatch(entry.color, __("{0}-facing", [entry.aspect])));
			});
			if (spec.flat_color) {
				rows.push(swatch(spec.flat_color, __("Flat (under {0}°)", [spec.flat_slope_degrees])));
			}
		} else {
			(spec.legend || []).forEach((entry) => {
				const range =
					entry.max_degrees === null || entry.max_degrees === undefined
						? __("{0}° and over", [entry.min_degrees])
						: __("{0}–{1}°", [entry.min_degrees, entry.max_degrees]);
				rows.push(swatch(entry.color, `${entry.label} (${range})`));
			});
		}
		const zoom = map && map.getZoom ? map.getZoom() : null;
		const hint =
			zoom !== null && zoom !== undefined && zoom < spec.min_zoom
				? " " + __("Zoom in to see it — it draws from zoom {0}.", [spec.min_zoom])
				: "";
		rows.push(
			`<div class="fo-terrain-note">${esc(spec.detail || "")}${esc(hint)} ${esc(
				__("Source: {0}.", [spec.source || ""])
			)}</div>`
		);
		$terrain_legend.html(rows.join("")).css("display", "flex");
	}

	/** What to show when there is no Leaflet: the farm, as a table of names.
	 *
	 * The same call `geo_map_widget.render_fallback` makes for a single record,
	 * and the reason is stronger here: a page that exists to show the whole farm
	 * has to say what it knows about the whole farm even when it cannot draw it.
	 * Every row links to its record, which is the thing somebody actually wants
	 * to reach.
	 */
	function render_fallback(reason) {
		if (map) {
			map.remove();
			map = null;
		}
		// No map, so no raster and nothing for its key to describe.
		render_terrain_legend(null);
		$canvas.hide();
		$panel.find(".fo-fallback, .fo-empty").remove();

		const rows = [];
		const index = overlay_index();
		(answer.layers || []).forEach((layer) => {
			(layer.shapes || []).forEach((entry) => {
				// `centre` AND NOT `centroid`, because this table is the one place
				// the page has no map to point at: it falls back to the middle of
				// the shape for a row whose stored centroid is missing, and a
				// coordinate column full of blanks would be this page failing at
				// exactly the job the fallback exists to do.
				// THE STATUS COLUMN MATTERS MOST HERE. With no Leaflet this table
				// is the whole map, and a fallback that dropped the layer would
				// leave somebody unable to find out which block is restricted by
				// any means at all.
				const state = overlay_for(index, entry);
				rows.push(
					`<tr><td><a href="${esc(entry.route)}">${esc(entry.label)}</a></td><td>${esc(
						layer.label
					)}</td><td>${
						entry.centre ? esc(entry.centre[0] + ", " + entry.centre[1]) : ""
					}</td><td>${esc(state ? String(state.status || "") : "")}</td></tr>`
				);
			});
		});
		(answer.markers || []).forEach((entry) => {
			rows.push(
				`<tr><td><a href="${esc(entry.route)}">${esc(entry.label)}</a></td><td>${esc(
					entry.doctype
				)}</td><td>${esc(entry.point[0] + ", " + entry.point[1])}</td><td></td></tr>`
			);
		});
		// v0.161.0. THE MACHINES AND THE JOBS BELONG IN THE FALLBACK TOO. With no
		// Leaflet this table IS the map, and a fallback that listed the ground
		// and dropped the open work would leave somebody unable to find out what
		// is outstanding by any means at all — which is the same argument the
		// overlay column already makes two blocks above.
		(answer.assets || []).forEach((entry) => {
			rows.push(
				`<tr><td><a href="${esc(entry.route)}">${esc(entry.label)}</a></td><td>${esc(
					entry.icon_label
				)}</td><td>${esc(entry.point[0] + ", " + entry.point[1])}</td><td>${esc(
					entry.current_state || (entry.last_scan_at ? "" : __("never scanned"))
				)}</td></tr>`
			);
		});
		(answer.tasks || []).forEach((entry) => {
			rows.push(
				`<tr><td><a href="${esc(entry.route)}">${esc(entry.label)}</a></td><td>${esc(
					__("Open task")
				)}</td><td>${esc(entry.point[0] + ", " + entry.point[1])}</td><td>${esc(
					entry.urgency + " · " + (entry.state || "")
				)}</td></tr>`
			);
		});

		$panel.append(
			`<div class="fo-fallback">
				<div class="text-muted">${esc(
					__("The map library could not be loaded ({0}), so the farm is listed instead.", [
						reason,
					])
				)}</div>
				<table><thead><tr>
						<th>${esc(__("Record"))}</th>
						<th>${esc(__("Register"))}</th>
						<th>${esc(__("Centroid"))}</th>
						<th>${esc(overlay_label() || __("Status"))}</th>
					</tr></thead><tbody>${rows.join("")}</tbody></table>
			</div>`
		);
	}

	reload();

	return { reload: reload };
}
