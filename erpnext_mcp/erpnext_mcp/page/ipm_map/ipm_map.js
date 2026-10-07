// SPDX-License-Identifier: MIT
//
// /app/ipm-map — the IPM relationship graph in the Desk. v0.263.0 (Tim, 2026-10-06).
//
//   * ONE SOURCE OF TRUTH. It draws what erpnext_mcp.api.ipm_map.view answers, which is
//     ipm_graph.graph() — the function behind the phone's get_ipm_graph and the MCP tool. It
//     decides nothing about who may edit: the server says can_edit, and every save goes through
//     ipm_map.save → ipm_graph.save_relationship behind the same gate as the phone.
//
//   * THE LIBRARY IS VENDORED. cytoscape.js (MIT) is served from /assets/erpnext_mcp/vendor/,
//     never a CDN: the server runs behind Tailscale. SVG export is written here from the laid-out
//     positions (the cytoscape-svg plugin is GPL and is not shipped); PNG is cytoscape's own.
//
//   * Workers are view-only. A manager / compliance user adds an edge by dragging in Connect mode
//     or with "Add relationship", and edits or disables an edge by clicking it. A literature edge
//     is never changed: the server writes the farm's copy beside it.

frappe.pages["ipm-map"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("IPM Map"), single_column: true });
	wrapper.ipm_map = erpnext_mcp_ipm_map(page);
};

frappe.pages["ipm-map"].on_page_show = function (wrapper) {
	if (wrapper.ipm_map) {
		wrapper.ipm_map.from_route();
	}
};

function erpnext_mcp_ipm_map(page) {
	const API = "erpnext_mcp.api.ipm_map.";
	const LIB = "/assets/erpnext_mcp/vendor/cytoscape/cytoscape.min.js";

	// Colour and shape by kind; the same groups the phone uses.
	const KIND_STYLE = {
		"Crop": ["#2e7d32", "round-rectangle"],
		"Variety": ["#66bb6a", "round-rectangle"],
		"Insect Pest": ["#e65100", "triangle"],
		"Mite Pest": ["#ef6c00", "triangle"],
		"Disease": ["#8e24aa", "diamond"],
		"Weed": ["#827717", "vee"],
		"Vertebrate Pest": ["#c62828", "pentagon"],
		"Beneficial Insect": ["#1565c0", "ellipse"],
		"Beneficial Mite": ["#1e88e5", "ellipse"],
		"Beneficial Microbe": ["#00838f", "ellipse"],
		"Beneficial Vertebrate": ["#283593", "hexagon"],
		"Pollinator": ["#f9a825", "star"],
		"Product": ["#546e7a", "rectangle"],
	};
	const EDGE_STYLE = {
		attacks: ["#ef6c00", "solid"],
		preys_on: ["#2e7d32", "solid"],
		parasitizes: ["#43a047", "dashed"],
		controls: ["#1b5e20", "solid"],
		harmed_by: ["#c62828", "dashed"],
		hosts: ["#757575", "dotted"],
		competes_with: ["#9e9e9e", "dotted"],
		pollinates: ["#1565c0", "solid"],
	};
	const PLAIN = {
		attacks: __("attacks"), preys_on: __("eats"), parasitizes: __("parasitizes"), controls: __("controls"),
		harmed_by: __("harmed by"), hosts: __("hosts"), competes_with: __("competes with"), pollinates: __("pollinates"),
	};
	// The ends each relation may join — the server's check, offered up front in the dialog.
	const PESTS = ["Insect Pest", "Mite Pest", "Disease", "Weed", "Vertebrate Pest"];
	const BENEFICIALS = ["Beneficial Insect", "Beneficial Mite", "Beneficial Microbe", "Beneficial Vertebrate", "Pollinator"];
	const CROPS = ["Crop", "Variety"];
	const ENDS = {
		attacks: [PESTS, CROPS],
		preys_on: [BENEFICIALS, PESTS],
		parasitizes: [BENEFICIALS, PESTS],
		controls: [BENEFICIALS.concat(["Product"]), PESTS],
		harmed_by: [BENEFICIALS.concat(CROPS), ["Product"]],
		pollinates: [["Pollinator", "Beneficial Insect"], CROPS],
		hosts: [null, null],
		competes_with: [null, null],
	};

	const $body = $(frappe.render_template("ipm_map")).appendTo(page.main);
	const $graph = $body.find(".ipm-graph");
	const $panel = $body.find(".ipm-panel");
	const $status = $body.find(".ipm-status");

	let cy = null;
	let answer = null;
	let choices = null;
	let connect = false;
	let drag_from = null;
	let library = null;
	const fields = {};

	function load_library() {
		if (window.cytoscape) return Promise.resolve(window.cytoscape);
		if (!library) {
			library = new Promise((resolve, reject) => {
				const tag = document.createElement("script");
				tag.src = LIB;
				tag.onload = () => (window.cytoscape ? resolve(window.cytoscape) : reject(new Error("empty")));
				tag.onerror = reject;
				document.head.appendChild(tag);
			});
		}
		return library;
	}

	function control(df, $parent) {
		const field = frappe.ui.form.make_control({ df, parent: $("<div>").appendTo($parent), render_input: true });
		field.refresh();
		return field;
	}

	function checks($where, title, values, label) {
		$where.empty().append($("<strong>").text(title));
		values.forEach((v) => {
			const $l = $("<label>").appendTo($where);
			$("<input type='checkbox'>").attr("value", v).appendTo($l).on("change", reload);
			$l.append(document.createTextNode(label ? label(v) : v));
		});
	}

	function checked($where) {
		return $where.find("input:checked").map((_, el) => el.value).get();
	}

	function build_controls() {
		const $f = $body.find(".ipm-filters").empty();
		fields.crop = control({ fieldtype: "Select", label: __("Crop / variety"), fieldname: "crop",
			options: [""].concat(choices.crops.map((c) => c.name)), change: reload }, $f);
		fields.block = control({ fieldtype: "Select", label: __("Block"), fieldname: "block",
			options: [""].concat(choices.blocks), change: reload }, $f);
		fields.stage = control({ fieldtype: "Int", label: __("BBCH stage"), fieldname: "stage", change: reload }, $f);
		fields.date = control({ fieldtype: "Date", label: __("Or stage on date (block)"), fieldname: "date", change: reload }, $f);
		fields.depth = control({ fieldtype: "Select", label: __("Depth"), fieldname: "depth", options: ["1", "2", "3"],
			default: "2", change: reload }, $f);
		fields.depth.set_value("2");
		fields.include_disabled = control({ fieldtype: "Check", label: __("Include disabled / proposed"),
			fieldname: "include_disabled", change: reload }, $f);
		checks($body.find(".ipm-kinds"), __("Kinds:"), choices.kinds, (k) => __(k));
		checks($body.find(".ipm-relations"), __("Relations:"), choices.relations, (r) => PLAIN[r] || r);
		checks($body.find(".ipm-provenance"), __("Provenance:"), choices.provenance);

		const $a = $body.find(".ipm-actions").empty();
		const button = (label, fn, cls) => $(`<button class="btn btn-xs ${cls || "btn-default"}">`).text(label).on("click", fn).appendTo($a);
		button(__("Fit"), () => cy && cy.fit(undefined, 30));
		button(__("Rings"), () => layout("concentric"));
		button(__("Force"), () => layout("cose"));
		if (choices.can_edit) {
			button(__("Add relationship"), () => edit_dialog({}), "btn-primary");
			const $c = button(__("Connect: off"), () => {
				connect = !connect;
				$c.text(connect ? __("Connect: on — drag from one node to another") : __("Connect: off"));
				if (cy) cy.autoungrabify(connect);
			});
		}
		button(__("Export PNG"), export_png);
		button(__("Export SVG"), export_svg);
		button(__("Export CSV"), export_csv);
		legend();
	}

	function legend() {
		const $l = $body.find(".ipm-legend").empty();
		Object.entries(KIND_STYLE).forEach(([k, [c]]) =>
			$l.append($("<span>").append($("<span class='ipm-swatch'>").css("background", c)).append(document.createTextNode(__(k)))));
		Object.entries(EDGE_STYLE).forEach(([r, [c, s]]) =>
			$l.append($("<span>").append($("<span class='ipm-line'>").css({ "border-top-color": c, "border-top-style": s }))
				.append(document.createTextNode(PLAIN[r]))));
		$l.append($("<span>").text(__("Blue double ring: MBTA-protected. Red ring: a product that harms a beneficial active now. Faded: not active at this stage.")));
	}

	function params() {
		return {
			crop: fields.crop.get_value() || null,
			block: fields.block.get_value() || null,
			stage: fields.stage.get_value() || null,
			date: fields.date.get_value() || null,
			depth: fields.depth.get_value() || 2,
			include_disabled: fields.include_disabled.get_value() ? 1 : 0,
			kinds: JSON.stringify(checked($body.find(".ipm-kinds"))),
			relations: JSON.stringify(checked($body.find(".ipm-relations"))),
			provenance: JSON.stringify(checked($body.find(".ipm-provenance"))),
		};
	}

	function reload() {
		if (!choices) return;
		const p = params();
		if (!p.crop && !p.block) {
			$status.text(__("Pick a crop or a block."));
			return;
		}
		$status.text(__("Loading…"));
		frappe.call({ method: API + "view", args: p }).then((r) => {
			answer = r.message;
			const stage = answer.stage && answer.stage.bbch != null ? ` · BBCH ${answer.stage.bbch} (${answer.stage.source})` : "";
			$status.text(`${answer.crop.name}${answer.block ? " · " + answer.block : ""}${stage} · ${answer.nodes.length} ${__("nodes")}, ${answer.edges.length} ${__("relationships")}`);
			$body.find(".ipm-caveat").text(answer.label_caveat || "");
			draw();
		});
	}

	function elements() {
		const out = answer.nodes.map((n) => ({
			group: "nodes",
			data: { id: n.id, label: n.name + (n.protected ? " ⛨" : ""), kind: n.kind, node: n },
			classes: [n.active_now === false ? "inactive" : "", n.protected ? "protected" : "",
				(n.harms_active || []).length ? "harms" : "", n.id === answer.crop.id ? "focus" : ""].join(" "),
		}));
		answer.edges.forEach((e) => out.push({
			group: "edges",
			data: { id: "e-" + e.id, source: e.subject, target: e.object, relation: e.relation, edge: e },
			classes: [e.active_now === false ? "inactive" : "", e.enabled ? "" : "disabled", "rel-" + e.relation].join(" "),
		}));
		return out;
	}

	function style() {
		const s = [
			{ selector: "node", style: { label: "data(label)", "font-size": 10, "text-valign": "bottom", "text-margin-y": 4,
				width: 22, height: 22, "border-width": 1, "border-color": "#fff", color: "#333", "text-wrap": "wrap", "text-max-width": 110 } },
			{ selector: "node.focus", style: { width: 40, height: 40, "font-weight": "bold", "font-size": 12 } },
			{ selector: "node.protected", style: { "border-width": 4, "border-style": "double", "border-color": "#1f4e9c" } },
			{ selector: "node.harms", style: { "border-width": 3, "border-color": "#c62828" } },
			{ selector: ".inactive", style: { opacity: 0.35 } },
			{ selector: "edge", style: { width: 1.5, "curve-style": "bezier", "target-arrow-shape": "triangle", "arrow-scale": 0.8 } },
			{ selector: "edge.disabled", style: { opacity: 0.2 } },
			{ selector: ":selected", style: { "overlay-opacity": 0.15, "overlay-color": "#1565c0" } },
		];
		Object.entries(KIND_STYLE).forEach(([k, [c, shape]]) =>
			s.push({ selector: `node[kind = "${k}"]`, style: { "background-color": c, shape } }));
		Object.entries(EDGE_STYLE).forEach(([r, [c, line]]) =>
			s.push({ selector: `edge.rel-${r}`, style: { "line-color": c, "target-arrow-color": c, "line-style": line } }));
		return s;
	}

	function ring(node) {
		const kind = node.data("kind");
		if (node.id() === answer.crop.id) return 4;
		if (PESTS.includes(kind) || CROPS.includes(kind)) return 3;
		if (BENEFICIALS.includes(kind)) return 2;
		return 1;
	}

	function layout(name) {
		if (!cy) return;
		const opts = name === "cose"
			? { name: "cose", animate: false, randomize: false, nodeRepulsion: 9000, idealEdgeLength: 90 }
			: { name: "concentric", concentric: ring, levelWidth: () => 1, minNodeSpacing: 18, animate: false };
		cy.layout(opts).run();
		cy.fit(undefined, 30);
	}

	function draw() {
		load_library().then((cytoscape) => {
			if (cy) cy.destroy();
			cy = cytoscape({ container: $graph[0], elements: elements(), style: style(), wheelSensitivity: 0.3 });
			cy.autoungrabify(connect);
			layout("concentric");
			cy.on("tap", "node", (ev) => { if (!connect) open_panel(ev.target.id()); });
			cy.on("tap", "edge", (ev) => edge_clicked(ev.target.data("edge")));
			cy.on("tapstart", "node", (ev) => { if (connect) { drag_from = ev.target.id(); $status.text(__("From {0} — release on the other node.", [ev.target.data("label")])); } });
			cy.on("tapend", "node", (ev) => {
				if (connect && drag_from && ev.target.id() !== drag_from) edit_dialog({ subject: drag_from, object: ev.target.id() });
				drag_from = null;
			});
			cy.on("tapend", (ev) => { if (ev.target === cy) drag_from = null; });
		}, () => {
			$graph.empty().append($("<p class='text-muted p-3'>").text(__("The graph library did not load (/assets/erpnext_mcp/vendor/cytoscape). The relationships are listed below.")));
			$graph.append($("<pre class='p-3'>").text(answer.edges.map((e) => `${e.subject} ${PLAIN[e.relation]} ${e.object}`).join("\n")));
		});
	}

	function name_of(id) {
		const n = answer && answer.nodes.find((x) => x.id === id);
		return n ? n.name : id;
	}

	function open_panel(id) {
		frappe.call({ method: API + "panel", args: { organism: id, block: fields.block.get_value() || null } }).then((r) => {
			const d = r.message;
			const n = d.node;
			const $p = $panel.empty();
			$("<h4>").text(n.name).appendTo($p);
			$("<div class='ipm-muted'>").text([n.kind, n.scientific_name, n.provenance].filter(Boolean).join(" · ")).appendTo($p);
			if (n.protected_status) $("<p>").append($("<strong>").text(`⛨ ${n.protected_status}. `)).append(document.createTextNode(n.protected_note || "")).appendTo($p);
			if (n.bbch_from != null || n.bbch_to != null) $("<p class='ipm-muted'>").text(__("Active BBCH {0}–{1}", [n.bbch_from ?? "?", n.bbch_to ?? "?"])).appendTo($p);
			const shown = answer && answer.nodes.find((x) => x.id === n.id);
			if (shown && (shown.harms_active || []).length) {
				$("<p class='text-danger'>").text(__("Harms beneficials active now: {0}", [shown.harms_active.map(name_of).join(", ")])).appendTo($p);
			}
			if (d.description) $("<p>").text(d.description).appendTo($p);
			$("<strong>").text(__("Relationships")).appendTo($p);
			const $ul = $("<ul>").appendTo($p);
			d.edges.forEach((e) => {
				const $li = $("<li>").text(`${name_of(e.subject)} ${PLAIN[e.relation]} ${name_of(e.object)}` +
					(e.weight != null ? ` (${e.weight})` : "") + ` — ${e.provenance}`).appendTo($ul);
				if (d.can_edit) $("<a href='#' class='ml-1'>").text(__("edit")).on("click", (ev) => { ev.preventDefault(); edge_clicked(e); }).appendTo($li);
			});
			$("<strong>").text(__("Thresholds")).appendTo($p);
			const $t = $("<ul>").appendTo($p);
			(d.thresholds || []).forEach((t) => $("<li>").text(`${t.crop || ""} ${t.crop_stage || ""}: ${t.comparison} ${t.action_threshold} ${t.sample_unit || ""} — ${t.status}${t.enabled ? "" : " (off)"}`).appendTo($t));
			if (!(d.thresholds || []).length) $("<li class='ipm-muted'>").text(__("None on file.")).appendTo($t);
			$("<strong>").text(__("Recent observations")).appendTo($p);
			const $o = $("<ul>").appendTo($p);
			(d.observations || []).forEach((o) => $("<li>").append($(`<a href="/app/crop-observation/${encodeURIComponent(o.name)}">`).text(
				`${o.observed_on || ""} ${o.block || ""}: ${o.count_observed ?? ""} ${o.sample_unit || ""}${o.threshold_exceeded ? " — over threshold" : ""}`)).appendTo($o));
			if (!(d.observations || []).length) $("<li class='ipm-muted'>").text(__("None logged.")).appendTo($o);
			$(`<a href="/app/ipm-organism/${encodeURIComponent(n.id)}">`).text(__("Open the record")).appendTo($p);
		});
	}

	function edge_clicked(e) {
		if (!choices.can_edit) {
			frappe.show_alert({ message: `${name_of(e.subject)} ${PLAIN[e.relation]} ${name_of(e.object)} — ${e.provenance}, ${__("confidence")} ${e.confidence}`, indicator: "blue" });
			return;
		}
		edit_dialog(e);
	}

	function kind_filter(relation, end) {
		const allowed = (ENDS[relation] || [null, null])[end];
		return allowed ? { kind: ["in", allowed], enabled: 1 } : { enabled: 1 };
	}

	function edit_dialog(e) {
		const editing = !!e.id;
		const d = new frappe.ui.Dialog({
			title: editing ? __("Edit relationship") : __("Add relationship"),
			fields: [
				{ fieldname: "subject", fieldtype: "Link", options: "IPM Organism", label: __("Subject"), reqd: 1, default: e.subject, read_only: editing ? 1 : 0 },
				{ fieldname: "relation", fieldtype: "Select", label: __("Relation"), reqd: 1, options: Object.keys(PLAIN), default: e.relation || "preys_on", read_only: editing ? 1 : 0 },
				{ fieldname: "object", fieldtype: "Link", options: "IPM Organism", label: __("Object"), reqd: 1, default: e.object, read_only: editing ? 1 : 0 },
				{ fieldname: "weight", fieldtype: "Float", label: __("Weight (0–1)"), default: e.weight },
				{ fieldname: "confidence", fieldtype: "Float", label: __("Confidence (0–1)"), default: e.confidence ?? 0.7 },
				{ fieldname: "crop", fieldtype: "Link", options: "IPM Organism", label: __("Only for crop"), default: e.crop },
				{ fieldname: "bbch_from", fieldtype: "Int", label: __("BBCH from"), default: e.bbch_from },
				{ fieldname: "bbch_to", fieldtype: "Int", label: __("BBCH to"), default: e.bbch_to },
				{ fieldname: "enabled", fieldtype: "Check", label: __("Enabled"), default: e.enabled === false ? 0 : 1 },
				{ fieldname: "notes", fieldtype: "Small Text", label: __("Notes"), default: e.notes },
				{ fieldname: "about", fieldtype: "HTML", options: editing && e.provenance === "Literature"
					? `<p class="text-muted small">${__("This is a literature relationship. Saving writes the farm&#39;s own copy beside it, which then stands in for it; the literature row is never changed.")}</p>` : "" },
			],
			primary_action_label: __("Save"),
			primary_action(values) {
				const args = editing ? { relationship: e.id } : { subject: values.subject, relation: values.relation, object: values.object };
				["weight", "confidence", "crop", "bbch_from", "bbch_to", "notes"].forEach((k) => { if (values[k] !== undefined && values[k] !== null && values[k] !== "") args[k] = values[k]; });
				args.enabled = values.enabled ? 1 : 0;
				frappe.call({ method: API + "save", args }).then((r) => {
					d.hide();
					frappe.show_alert({ message: r.message.created ? __("Relationship added") : __("Relationship saved"), indicator: "green" });
					reload();
				});
			},
		});
		["subject", "object"].forEach((end, i) => {
			d.fields_dict[end].get_query = () => ({ filters: kind_filter(d.get_value("relation"), i) });
		});
		if (editing) {
			d.set_secondary_action_label(e.enabled ? __("Disable") : __("Enable"));
			d.set_secondary_action(() => frappe.call({ method: API + "save", args: { relationship: e.id, enabled: e.enabled ? 0 : 1 } })
				.then(() => { d.hide(); reload(); }));
		}
		d.show();
	}

	function download(name, blob) {
		const url = URL.createObjectURL(blob);
		const a = document.createElement("a");
		a.href = url;
		a.download = name;
		document.body.appendChild(a);
		a.click();
		a.remove();
		setTimeout(() => URL.revokeObjectURL(url), 1000);
	}

	function stem() {
		return `ipm-map-${(answer && answer.crop.id) || "graph"}${answer && answer.block ? "-" + answer.block.replace(/\W+/g, "-") : ""}`;
	}

	function export_png() {
		if (!cy) return;
		download(stem() + ".png", cy.png({ output: "blob", full: true, scale: 2, bg: "#ffffff" }));
	}

	function esc(text) {
		return String(text ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
	}

	function export_svg() {
		if (!cy) return;
		const box = cy.elements().boundingBox({});
		const pad = 40;
		const w = Math.ceil(box.w + 2 * pad), h = Math.ceil(box.h + 2 * pad);
		const x = (p) => (p.x - box.x1 + pad).toFixed(1), y = (p) => (p.y - box.y1 + pad).toFixed(1);
		const dash = { dashed: "6,4", dotted: "2,3", solid: "" };
		const parts = [`<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" font-family="sans-serif" font-size="10">`,
			`<rect width="100%" height="100%" fill="#ffffff"/>`];
		cy.edges().forEach((edge) => {
			const [c, line] = EDGE_STYLE[edge.data("relation")] || ["#999", "solid"];
			const s = edge.source().position(), t = edge.target().position();
			parts.push(`<line x1="${x(s)}" y1="${y(s)}" x2="${x(t)}" y2="${y(t)}" stroke="${c}" stroke-width="1.5"${dash[line] ? ` stroke-dasharray="${dash[line]}"` : ""}${edge.hasClass("inactive") ? ' opacity="0.35"' : ""}/>`);
		});
		cy.nodes().forEach((node) => {
			const n = node.data("node");
			const [c] = KIND_STYLE[n.kind] || ["#999"];
			const p = node.position();
			const r = n.id === answer.crop.id ? 20 : 11;
			const ring = n.protected ? ' stroke="#1f4e9c" stroke-width="4"' : (n.harms_active || []).length ? ' stroke="#c62828" stroke-width="3"' : ' stroke="#ffffff"';
			parts.push(`<circle cx="${x(p)}" cy="${y(p)}" r="${r}" fill="${c}"${ring}${n.active_now === false ? ' opacity="0.35"' : ""}><title>${esc(n.kind)}</title></circle>`);
			parts.push(`<text x="${x(p)}" y="${(p.y - box.y1 + pad + r + 11).toFixed(1)}" text-anchor="middle" fill="#333">${esc(node.data("label"))}</text>`);
		});
		parts.push("</svg>");
		download(stem() + ".svg", new Blob([parts.join("\n")], { type: "image/svg+xml" }));
	}

	function export_csv() {
		if (!answer) return;
		const cell = (v) => {
			const s = String(v ?? "");
			return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
		};
		const rows = [["record", "id", "name", "kind", "protected_status", "active_now", "subject", "relation", "object", "weight", "confidence", "provenance", "crop", "bbch_from", "bbch_to", "enabled", "source", "notes"]];
		answer.nodes.forEach((n) => rows.push(["node", n.id, n.name, n.kind, n.protected_status, n.active_now, "", "", "", "", "", n.provenance, "", n.bbch_from, n.bbch_to, n.enabled, "", ""]));
		answer.edges.forEach((e) => rows.push(["edge", e.id, "", "", "", e.active_now, e.subject, e.relation, e.object, e.weight, e.confidence, e.provenance, e.crop, e.bbch_from, e.bbch_to, e.enabled, e.source, e.notes]));
		download(stem() + ".csv", new Blob([rows.map((r) => r.map(cell).join(",")).join("\n") + "\n"], { type: "text/csv" }));
	}

	function from_route() {
		const o = frappe.route_options || {};
		frappe.route_options = null;
		const start = () => {
			if (o.crop) fields.crop.set_value(o.crop);
			if (o.block) fields.block.set_value(o.block);
			if (o.organism) setTimeout(() => open_panel(o.organism), 600);
			if (!o.crop && !o.block && !fields.crop.get_value()) {
				const cherry = choices.crops.find((c) => c.name === "sweet-cherry") || choices.crops[0];
				if (cherry) fields.crop.set_value(cherry.name);
			}
			reload();
		};
		if (choices) return start();
		frappe.call({ method: API + "choices" }).then((r) => {
			choices = r.message;
			build_controls();
			start();
		});
	}

	return { from_route, reload };
}
