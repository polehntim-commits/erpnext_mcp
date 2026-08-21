// SPDX-License-Identifier: MIT
//
// The token is shown once, in a dialog the operator has to dismiss, and never
// written into the form's own field display. Anything else — a msgprint that
// scrolls away, a value left in an input — ends up in a screenshot or a browser
// session someone else uses.

frappe.ui.form.on("ERPNext MCP Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Test Configuration"), () => show_selftest(frm));
		set_headline(frm);
		render_connect_panel(frm);
		render_tool_console(frm);

		frm.set_df_property(
			"generate_token",
			"description",
			frm.doc.token_generated_on
				? __("Generating a new token immediately invalidates the current one.")
				: __("No token yet. The endpoint stays off until one exists.")
		);
	},

	enabled(frm) {
		render_connect_panel(frm);
	},

	public_url(frm) {
		render_connect_panel(frm);
	},

	generate_token(frm) {
		frappe.confirm(
			frm.doc.token_generated_on
				? __(
						"Generate a new token? Any MCP client using the current token will stop working immediately."
				  )
				: __("Generate an auth token for MCP clients?"),
			() => {
				frm.call({
					doc: frm.doc,
					method: "generate_token",
					freeze: true,
					freeze_message: __("Generating token..."),
				}).then((r) => {
					if (!r || !r.message) return;
					show_token_once(r.message);
					frm.reload_doc();
				});
			}
		);
	},
});

// The list of write tools comes from the server, not from a copy kept here.
// Hardcoding it is how a form ends up telling an operator "read-only" while a
// tool added in a later version is quietly live. Cached per page load.
let MUTATING_TOOLS = null;

function with_mutating_tools(callback) {
	if (MUTATING_TOOLS) {
		callback(MUTATING_TOOLS);
		return;
	}
	frappe.call({ method: "erpnext_mcp.mcp.mutating_tool_names" }).then((r) => {
		MUTATING_TOOLS = r.message || [];
		callback(MUTATING_TOOLS);
	});
}

function set_headline(frm) {
	if (!frm.doc.enabled) {
		frm.dashboard.set_headline_alert(
			__("The MCP endpoint is off. It answers 404 to every caller."),
			"orange"
		);
		return;
	}
	with_mutating_tools((names) => {
		const live = names.filter((name) => frm.doc[`allow_${name}`]);
		if (live.length) {
			frm.dashboard.set_headline_alert(
				__("MCP is live with {0} write tool(s) enabled: {1}", [
					live.length,
					live.join(", "),
				]),
				"red"
			);
		} else {
			frm.dashboard.set_headline_alert(
				__("MCP is live, read-only. No write tool is enabled."),
				"green"
			);
		}
	});
}

function show_token_once(payload) {
	const dialog = new frappe.ui.Dialog({
		title: __("Copy This Token Now"),
		size: "large",
		fields: [
			{
				fieldtype: "HTML",
				options: `<div class="form-message red">${__(
					"This is the only time this token is shown. It is stored encrypted and cannot be retrieved."
				)}</div>`,
			},
			{
				fieldname: "token",
				fieldtype: "Code",
				label: __("Bearer Token"),
				default: payload.token,
				read_only: 1,
			},
			{
				fieldtype: "HTML",
				options: `<p>${__("Use it as")} <code>Authorization: Bearer &lt;token&gt;</code> ${__(
					"against"
				)} <code>POST ${frappe.utils.escape_html(payload.endpoint)}</code></p>`,
			},
		],
		primary_action_label: __("Copy to Clipboard"),
		primary_action() {
			frappe.utils.copy_to_clipboard(payload.token);
			frappe.show_alert({ message: __("Token copied"), indicator: "green" });
		},
	});
	dialog.show();
}

function show_selftest(frm) {
	frappe.call({ method: "erpnext_mcp.mcp.selftest", freeze: true }).then((r) => {
		const d = r.message || {};
		const row = (label, value) =>
			`<tr><td class="text-muted" style="padding-right:1rem">${label}</td><td><b>${value}</b></td></tr>`;
		const yes_no = (v) =>
			v
				? `<span class="text-success">${__("yes")}</span>`
				: `<span class="text-danger">${__("no")}</span>`;
		frappe.msgprint({
			title: d.ready ? __("MCP Is Ready") : __("MCP Is Not Ready"),
			indicator: d.ready ? "green" : "orange",
			message: `<table>
				${row(__("Enabled"), yes_no(d.enabled))}
				${row(__("Token configured"), yes_no(d.token_configured))}
				${row(__("Allowed CIDRs"), (d.allowed_cidrs || []).join(", ") || "—")}
				${row(__("Runs as"), frappe.utils.escape_html(d.effective_user || "—"))}
				${row(__("Endpoint"), `<code>POST ${frappe.utils.escape_html(d.endpoint || "")}</code>`)}
				${row(__("Protocol versions"), (d.protocol_versions || []).join(", "))}
				${row(
					__("Tools enabled"),
					`${(d.tools_enabled || []).length} / ${d.tools_total || 0}`
				)}
				${row(
					__("Write tools enabled"),
					(d.mutating_tools_enabled || []).join(", ") ||
						`<span class="text-success">${__("none")}</span>`
				)}
				${row(
					__("Unavailable on this site"),
					Object.keys(d.tools_unavailable || {})
						.map(
							(name) =>
								`${frappe.utils.escape_html(name)} <span class="text-muted">(${frappe.utils.escape_html(
									d.tools_unavailable[name] || "prerequisite missing"
								)})</span>`
						)
						.join("<br>") || `<span class="text-muted">${__("none")}</span>`
				)}
			</table>`,
		});
	});
}

// ── Connect to Claude Desktop ───────────────────────────────────────────────
//
// The panel renders masked. Copy and Download re-fetch with reveal=1, so the
// clipboard and the file get a working config while the screen stays safe to
// share — the operator never has to choose between "usable" and "not on my
// screenshot".

const CONNECT_FIELD = "claude_desktop_html";

function render_connect_panel(frm, revealed_payload) {
	const wrapper = frm.get_field(CONNECT_FIELD) && frm.get_field(CONNECT_FIELD).$wrapper;
	if (!wrapper) return;
	if (!frm.doc.enabled) {
		wrapper.empty();
		return;
	}

	const draw = (d) => {
		wrapper.empty().append(connect_panel_html(d || {}));
		wire_connect_panel(frm, wrapper, d || {});
	};

	if (revealed_payload) {
		draw(revealed_payload);
		return;
	}
	frappe
		.call({ method: "erpnext_mcp.onboarding.claude_desktop_config", args: { reveal: 0 } })
		.then((r) => draw(r.message));
}

function connect_panel_html(d) {
	if (!d.token_configured) {
		return `<div class="form-message orange">${__(
			"Generate an auth token above, then this panel will show the client configuration."
		)}</div>`;
	}

	const esc = frappe.utils.escape_html;
	const os_rows = Object.keys(d.config_paths || {})
		.map((key) => {
			const active = key === d.detected_os;
			return `<tr class="${active ? "" : "text-muted"}">
				<td style="padding:2px 12px 2px 0;white-space:nowrap">
					${active ? "<b>" : ""}${esc(d.os_labels[key])}${active ? "</b> ←" : ""}
				</td>
				<td><code>${esc(d.config_paths[key])}</code></td>
			</tr>`;
		})
		.join("");

	// A bare-IP URL reaches Frappe's site router and matches no site directory,
	// so a client can get "site not found" while this very browser works. That
	// asymmetry is baffling without being told, so say it here.
	const routing_warning = d.routing_warning && d.routing_warning.code
		? `<div class="form-message red" style="margin-bottom:12px">
			<b>${__("This URL uses a bare IP address")}</b><br>${esc(d.routing_warning.message)}
		   </div>`
		: "";

	const http_note = d.is_http
		? `<p class="text-muted small">${__(
				"This endpoint is plain HTTP, so the config includes <code>--allow-http</code>. mcp-remote refuses a non-HTTPS origin without it."
		  )}</p>`
		: "";

	return `
<div class="erpnext-mcp-connect">
	${routing_warning}
	<p><b>${__("1. Save this to your Claude Desktop config")}</b><br>
	<span class="text-muted small">${__("Default location — highlighted row is the platform this browser reports.")}</span></p>
	<table style="margin-bottom:12px">${os_rows}</table>

	<pre data-connect="json" style="max-height:260px;overflow:auto;padding:10px;border-radius:4px">${esc(
		d.config_json
	)}</pre>
	${http_note}
	<p>
		<button class="btn btn-xs btn-primary" data-connect-action="copy-json">${__("Copy config JSON")}</button>
		<button class="btn btn-xs btn-default" data-connect-action="download">${__("Download config file")}</button>
		<button class="btn btn-xs btn-default" data-connect-action="reveal">${
			d.revealed ? __("Hide token") : __("Reveal for copy")
		}</button>
		<span class="text-muted small" style="margin-left:8px">${
			d.revealed
				? __("Token visible — do not screenshot.")
				: __("Preview is masked. Copy and Download still give you the real token.")
		}</span>
	</p>

	<p style="margin-top:16px"><b>${__("2. Restart Claude Desktop")}</b><br>
	<span class="text-muted small">${__("Fully quit ({0}) and reopen — reloading the window is not enough.", [
		esc(d.quit_keys[d.detected_os] || "⌘Q"),
	])}</span></p>

	<p><b>${__("3. Check it worked")}</b><br>
	<span class="text-muted small">${__(
		'Ask Claude "get the company topology from erpnext". The <code>erpnext__*</code> tools should be available.'
	)}</span></p>

	<p class="text-muted small">${__(
		"If the file already exists, merge the <code>erpnext</code> key into your existing <code>mcpServers</code> object rather than replacing the file."
	)}</p>

	<hr>
	<p><b>${__("Connect from Claude Code")}</b><br>
	<span class="text-muted small">${__("No bridge needed — Claude Code speaks HTTP MCP directly.")}</span></p>
	<pre data-connect="cli" style="padding:10px;border-radius:4px;white-space:pre-wrap">${esc(
		d.claude_code_command
	)}</pre>
	<p><button class="btn btn-xs btn-default" data-connect-action="copy-cli">${__("Copy command")}</button></p>

	<p class="text-muted small" style="margin-top:12px">${__("Endpoint")}:
		<code>${esc(d.endpoint_url)}</code> <span>${__("from")} ${esc(d.url_source)}</span>
		${
			(d.url_candidates || []).length > 1
				? `<br><span title="${esc(
						(d.url_candidates || [])
							.map((c) => `${c.source}: ${c.base}`)
							.join("\n")
				  )}">${__("Other addresses were available — hover to see what was considered.")}</span>`
				: ""
		}
		<br>${__(
			"Wrong address? Set Public URL above — that always wins."
		)}
	</p>
</div>`;
}

function wire_connect_panel(frm, wrapper, current) {
	wrapper.find("[data-connect-action]").on("click", function () {
		const action = $(this).attr("data-connect-action");

		if (action === "download") {
			// A GET the browser can open. The token rides in the response body,
			// never in the URL, so it stays out of proxy logs and history.
			window.open(current.download_url, "_blank");
			return;
		}

		if (action === "reveal") {
			if (current.revealed) {
				render_connect_panel(frm);
				return;
			}
			with_revealed_config((payload) => render_connect_panel(frm, payload));
			return;
		}

		// Copy always fetches the real token, whatever the preview is showing.
		with_revealed_config((payload) => {
			const text = action === "copy-cli" ? payload.claude_code_command : payload.config_json;
			copy_text(text);
		});
	});
}

function with_revealed_config(callback) {
	frappe
		.call({ method: "erpnext_mcp.onboarding.claude_desktop_config", args: { reveal: 1 } })
		.then((r) => r.message && callback(r.message));
}

function copy_text(text) {
	if (frappe.utils && frappe.utils.copy_to_clipboard) {
		frappe.utils.copy_to_clipboard(text);
		frappe.show_alert({ message: __("Copied — includes your real token"), indicator: "green" });
		return;
	}
	// Non-secure contexts (a LAN site on plain http) have no navigator.clipboard.
	const area = document.createElement("textarea");
	area.value = text;
	area.style.position = "fixed";
	area.style.opacity = "0";
	document.body.appendChild(area);
	area.select();
	try {
		document.execCommand("copy");
		frappe.show_alert({ message: __("Copied — includes your real token"), indicator: "green" });
	} catch (e) {
		frappe.msgprint(__("Could not copy automatically. Select the text above and copy it."));
	}
	document.body.removeChild(area);
}

// ── The tool console ────────────────────────────────────────────────────────
//
// This form carries one checkbox per tool and there are now seven hundred and
// fifty-seven of them. Every one is a real control with a real reason to exist,
// and a page of seven hundred and fifty-seven checkboxes is still a page nobody
// configures — an operator scrolls to the section whose name they recognise,
// ticks two boxes and leaves the rest at whatever the release shipped.
//
// So this adds four things and removes nothing: a SUMMARY that says what is
// actually on before anybody scrolls, DOMAIN chips that narrow the form to one
// part of the operation, a SEARCH box, and PRESET PROFILES that set a whole
// working configuration at once.
//
// NOTHING HERE KEEPS ITS OWN COPY OF THE CATALOGUE. The domains, the profiles
// and every tool's domain and write-ness come from
// `erpnext_mcp.tool_groups.console`, for the same reason the write-tool banner
// above asks the server: a list transcribed into JavaScript is a list that goes
// stale the next release, and this form's whole job is to tell the truth about
// what an AI client can reach.

const CONSOLE_FIELD = "tool_console_html";

//: Fetched once per page load. The payload is the tool→domain map and the
//: profile table, around thirty kilobytes, on a page an operator opens on
//: purpose.
let CONSOLE = null;

//: What the operator is currently looking at. Kept outside the render so a
//: redraw (after a save, after applying a profile) does not silently reset the
//: filter somebody is halfway through using.
const VIEW = { query: "", domain: "", only: "all" };

function render_tool_console(frm) {
	const field = frm.get_field(CONSOLE_FIELD);
	if (!field || !field.$wrapper) return;

	if (CONSOLE) {
		draw_tool_console(frm);
		return;
	}
	frappe.call({ method: "erpnext_mcp.tool_groups.console" }).then((r) => {
		if (!r || !r.message) return;
		CONSOLE = r.message;
		draw_tool_console(frm);
	});
}

function draw_tool_console(frm) {
	const wrapper = frm.get_field(CONSOLE_FIELD).$wrapper;
	wrapper.empty().append(tool_console_html());
	wire_tool_console(frm, wrapper);
	refresh_tool_counts(frm, wrapper);
	apply_tool_filter(frm, wrapper);
}

function tool_console_html() {
	const esc = frappe.utils.escape_html;

	const chips = [
		`<button class="btn btn-xs btn-default erpnext-mcp-chip" data-domain="" title="${__(
			"Every domain"
		)}">${__("All")} <span class="text-muted" data-count-for=""></span></button>`,
	]
		.concat(
			(CONSOLE.domains || []).map(
				(domain) =>
					`<button class="btn btn-xs btn-default erpnext-mcp-chip" data-domain="${esc(
						domain.key
					)}" title="${esc(domain.description)}">${esc(domain.label)}
					<span class="text-muted" data-count-for="${esc(domain.key)}"></span></button>`
			)
		)
		.join(" ");

	const presets = (CONSOLE.profiles || [])
		.map(
			(profile) =>
				`<button class="btn btn-xs btn-default" data-profile="${esc(profile.key)}"
					title="${esc(profile.summary)}">${esc(profile.label)}</button>`
		)
		.join(" ");

	return `
<div class="erpnext-mcp-tool-console">
	<div class="form-message blue" data-console="summary" style="margin-bottom:10px"></div>

	<div style="margin-bottom:8px">
		<input type="search" class="form-control input-sm" data-console="search" style="max-width:340px;display:inline-block"
			placeholder="${__("Search tools — name or label")}">
		<select class="form-control input-sm" data-console="only" style="max-width:180px;display:inline-block;margin-left:6px">
			<option value="all">${__("All tools")}</option>
			<option value="enabled">${__("Enabled only")}</option>
			<option value="disabled">${__("Disabled only")}</option>
			<option value="write">${__("Write tools only")}</option>
			<option value="read">${__("Read tools only")}</option>
		</select>
		<span class="text-muted small" data-console="shown" style="margin-left:8px"></span>
	</div>

	<div style="margin-bottom:8px">${chips}</div>

	<div style="margin-bottom:8px">
		<button class="btn btn-xs btn-default" data-bulk="on">${__("Enable everything shown")}</button>
		<button class="btn btn-xs btn-default" data-bulk="off">${__("Disable everything shown")}</button>
		<span class="text-muted small" style="margin-left:8px">${__(
			"Applies to the tools the filter is currently showing. Not saved until you save the form."
		)}</span>
	</div>

	<div>
		<span class="text-muted small">${__("Presets")}:</span> ${presets}
		<div class="text-muted small" style="margin-top:4px">${__(
			"A preset writes every tool switch at once and saves immediately. It never changes the master switch, the token, the allowlist or the packet types."
		)}</div>
	</div>
</div>`;
}

function wire_tool_console(frm, wrapper) {
	wrapper.find('[data-console="search"]').val(VIEW.query);
	wrapper.find('[data-console="only"]').val(VIEW.only);

	wrapper.find('[data-console="search"]').on("input", function () {
		VIEW.query = $(this).val() || "";
		apply_tool_filter(frm, wrapper);
	});
	wrapper.find('[data-console="only"]').on("change", function () {
		VIEW.only = $(this).val() || "all";
		apply_tool_filter(frm, wrapper);
	});
	wrapper.find("[data-domain]").on("click", function () {
		VIEW.domain = $(this).attr("data-domain") || "";
		apply_tool_filter(frm, wrapper);
	});
	wrapper.find("[data-bulk]").on("click", function () {
		bulk_set(frm, wrapper, $(this).attr("data-bulk") === "on");
	});
	wrapper.find("[data-profile]").on("click", function () {
		preview_profile(frm, $(this).attr("data-profile"));
	});

	wire_count_listener(frm, wrapper);
}

// The counts have to move when a checkbox does, and there is no per-field event
// to bind seven hundred handlers to — nor any reason to. One delegated listener
// on the form recomputes from `frm.doc`, which is what the form has already
// updated by the time the event reaches here.
//
// Its own function because `bulk_set` DETACHES it for the duration of a batch:
// this handler walks the whole catalogue, so leaving it on during a set of
// seven hundred switches would run it seven hundred times over seven hundred
// entries. Namespaced so `off` cannot take anybody else's handler with it.
function wire_count_listener(frm, wrapper) {
	frm.$wrapper.off("change.mcpconsole").on("change.mcpconsole", "input[type=checkbox]", () => {
		refresh_tool_counts(frm, wrapper);
	});
}

function tool_switches(frm) {
	return frm.$wrapper.find('.frappe-control[data-fieldname^="allow_"]');
}

function apply_tool_filter(frm, wrapper) {
	const query = (VIEW.query || "").toLowerCase().trim();
	let shown = 0;

	tool_switches(frm).each(function () {
		const control = $(this);
		const fieldname = control.attr("data-fieldname");
		const tool = fieldname.slice("allow_".length);
		// The two compliance packet types carry an `allow_` switch and are not
		// tools, so they live in their own small map. Filed under a domain like
		// everything else — a chip that hid them would be a chip that hid part
		// of the form for no reason an operator could see.
		const info = (CONSOLE.tools || {})[tool] || (CONSOLE.switches || {})[tool];
		let visible = true;

		if (VIEW.domain) visible = !!info && info.domain === VIEW.domain;
		if (visible && VIEW.only === "enabled") visible = !!frm.doc[fieldname];
		if (visible && VIEW.only === "disabled") visible = !frm.doc[fieldname];
		if (visible && VIEW.only === "write") visible = !!info && !!info.mutating;
		// The mirror of "write", and the one that makes a DOMAIN'S READS settable
		// on their own: pick a chip, pick this, press "Enable everything shown".
		// Without it the only additive bulk control enables a domain's writes
		// alongside its reads, and the profiles — which do separate reads from
		// writes — are absolute, so neither reaches "give the bookkeeper the
		// compliance reads and change nothing else".
		//
		// THE TWO PACKET TYPES ARE EXCLUDED DELIBERATELY. They carry an `allow_`
		// switch and live in `CONSOLE.switches` rather than `CONSOLE.tools`,
		// because they are artefacts this app can build and not tools a client
		// can call. `mutating` is false on both, so a bare `!info.mutating` would
		// sweep them into every read filter and let "enable all Compliance reads"
		// tick a packet type. Harmless in blast radius, wrong in meaning.
		if (visible && VIEW.only === "read")
			visible = !!info && !info.mutating && !(CONSOLE.switches || {})[tool];
		if (visible && query) {
			const haystack = (tool + " " + control.find(".label-area").text()).toLowerCase();
			visible = haystack.indexOf(query) !== -1;
		}

		control.toggle(visible);
		if (visible) shown += 1;
	});

	// A section whose every switch is filtered out is a heading over nothing, and
	// a page of ninety empty headings is worse than the unfiltered form. Sections
	// with no tool switches at all — Connection, Network, Attribution — are left
	// exactly as they are: the filter is about tools and must not hide the
	// endpoint's own configuration.
	frm.$wrapper.find(".form-section").each(function () {
		const section = $(this);
		const switches = section.find('.frappe-control[data-fieldname^="allow_"]');
		if (!switches.length) return;
		section.toggle(switches.filter(":visible").length > 0);
	});

	wrapper
		.find('[data-console="shown"]')
		.text(__("Showing {0} of {1} switches", [shown, tool_switches(frm).length]));
	wrapper.find("[data-domain]").removeClass("btn-primary").addClass("btn-default");
	wrapper
		.find(`[data-domain="${VIEW.domain}"]`)
		.removeClass("btn-default")
		.addClass("btn-primary");
}

function refresh_tool_counts(frm, wrapper) {
	const per_domain = {};
	let total = 0;
	let enabled = 0;
	let writes_live = 0;

	Object.keys(CONSOLE.tools || {}).forEach((tool) => {
		const info = CONSOLE.tools[tool];
		const on = !!frm.doc[`allow_${tool}`];
		const bucket = (per_domain[info.domain] = per_domain[info.domain] || { total: 0, on: 0 });
		bucket.total += 1;
		total += 1;
		if (on) {
			bucket.on += 1;
			enabled += 1;
			if (info.mutating) writes_live += 1;
		}
	});

	wrapper.find('[data-count-for=""]').text(`${enabled}/${total}`);
	(CONSOLE.domains || []).forEach((domain) => {
		const bucket = per_domain[domain.key] || { total: 0, on: 0 };
		wrapper.find(`[data-count-for="${domain.key}"]`).text(`${bucket.on}/${bucket.total}`);
	});

	const writes = writes_live
		? `<span class="text-danger">${__("{0} write tool(s) enabled", [writes_live])}</span>`
		: `<span class="text-success">${__("no write tools enabled")}</span>`;
	wrapper
		.find('[data-console="summary"]')
		.html(`<b>${__("{0} of {1} tools enabled", [enabled, total])}</b> — ${writes}`);
}

function bulk_set(frm, wrapper, value) {
	const visible = tool_switches(frm).filter(":visible");
	if (!visible.length) {
		frappe.show_alert({ message: __("Nothing is showing to change."), indicator: "orange" });
		return;
	}
	const apply = () => {
		// ONE `set_value` WITH A DICT, NOT 759 CALLS. With no filter active this
		// touches every switch on the form, and `set_value` re-renders the field
		// and re-runs the form's own change handling per call — seven hundred of
		// those in a loop locks the tab up for seconds on a laptop, which reads as
		// the page having crashed rather than as it working. The dict form is
		// Frappe's own batch API and does the render pass once.
		//
		// The count listener comes off first for the same reason: it recomputes
		// over the whole catalogue on every checkbox event, and leaving it
		// attached makes a bulk set quadratic. Counts and filter are refreshed
		// once, at the end, from `frm.doc`.
		const patch = {};
		visible.each(function () {
			patch[$(this).attr("data-fieldname")] = value ? 1 : 0;
		});
		frm.$wrapper.off("change.mcpconsole");
		Promise.resolve(frm.set_value(patch)).then(() => {
			wire_count_listener(frm, wrapper);
			refresh_tool_counts(frm, wrapper);
			apply_tool_filter(frm, wrapper);
		});
	};
	if (!value) {
		apply();
		return;
	}
	// Turning things ON in bulk can turn write tools on in bulk, which is the one
	// direction worth stopping to read a sentence about.
	frappe.confirm(
		__("Enable all {0} switches currently showing? Save the form to make it take effect.", [
			visible.length,
		]),
		apply
	);
}

function preview_profile(frm, key) {
	if (frm.is_dirty()) {
		frappe.msgprint({
			title: __("Unsaved Changes"),
			indicator: "orange",
			message: __(
				"A preset saves the whole form. Save or reload your current changes first, so they are not written under a different name."
			),
		});
		return;
	}
	frappe
		.call({
			method: "erpnext_mcp.tool_groups.apply_profile",
			args: { profile: key, dry_run: 1 },
			freeze: true,
		})
		.then((r) => r && r.message && show_profile_dialog(frm, r.message));
}

function show_profile_dialog(frm, plan) {
	const esc = frappe.utils.escape_html;
	const profile = plan.profile || {};
	const writes = plan.write_tools_enabled || [];
	const names = (list) =>
		list.length ? list.map(esc).join(", ") : `<span class="text-muted">${__("none")}</span>`;

	const dialog = new frappe.ui.Dialog({
		title: __("Apply preset: {0}", [profile.label || ""]),
		size: "large",
		fields: [
			{
				fieldtype: "HTML",
				options: `
<p>${esc(profile.summary || "")}</p>
<table class="table table-bordered" style="margin-bottom:8px">
	<tr><td class="text-muted">${__("Domains it can read")}</td><td>${names(
					profile.read_domain_labels || []
				)}</td></tr>
	<tr><td class="text-muted">${__("Domains it can write")}</td><td>${names(
					profile.write_domain_labels || []
				)}</td></tr>
	<tr><td class="text-muted">${__("Tools enabled afterwards")}</td><td><b>${
					plan.will_be_enabled
				}</b> ${__("of")} ${plan.total}</td></tr>
	<tr><td class="text-muted">${__("Switching on")}</td><td>${plan.enabling.length}</td></tr>
	<tr><td class="text-muted">${__("Switching off")}</td><td>${plan.disabling.length}</td></tr>
	<tr><td class="text-muted">${__("Write tools this enables")}</td><td>${
					writes.length
						? `<span class="text-danger">${writes.length}</span>: ${esc(writes.join(", "))}`
						: `<span class="text-success">${__("none")}</span>`
				}</td></tr>
</table>
<div class="form-message ${plan.disabling.length ? "orange" : "blue"}">${__(
					"This writes every tool switch and saves immediately. It does not change the master switch, the token, the allowed CIDRs or the packet types. The change is recorded as one Version entry you can read afterwards."
				)}</div>`,
			},
		],
		primary_action_label: __("Apply preset"),
		primary_action() {
			dialog.hide();
			frappe
				.call({
					method: "erpnext_mcp.tool_groups.apply_profile",
					args: { profile: profile.key },
					freeze: true,
					freeze_message: __("Writing tool switches..."),
				})
				.then((r) => {
					if (!r || !r.message) return;
					frappe.show_alert({
						message: __("{0} applied — {1} tools enabled", [
							profile.label,
							r.message.summary.enabled,
						]),
						indicator: "green",
					});
					frm.reload_doc();
				});
		},
	});
	dialog.show();
}
