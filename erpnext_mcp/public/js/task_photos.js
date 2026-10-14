// SPDX-License-Identifier: MIT
/**
 * v0.328.0. Photos where people look for them (display only).
 *
 * Farm Task: every assignment's evidence in one gallery — the step it proves, before / after, when and where.
 * Asset Register: every task about this asset, open or done (by asset, location, places or its cabin), with a
 * photo strip — MC-SHOWER-TOILET-1 used to show none although FT-2026-10-00013 photographed it.
 */
(function () {
	const esc = (text) => frappe.utils.escape_html(String(text == null ? "" : text));

	function thumb(photo) {
		const label = [photo.step, photo.phase, photo.captured_on && frappe.datetime.str_to_user(photo.captured_on)]
			.filter(Boolean).join(" · ");
		const where = photo.latitude != null && photo.longitude != null
			? ` <a href="https://maps.google.com/?q=${photo.latitude},${photo.longitude}" target="_blank" rel="noopener">${__("map")}</a>`
			: "";
		return `<div style="display:inline-block;width:132px;margin:0 8px 8px 0;vertical-align:top">
			<a href="${esc(photo.url)}" target="_blank" rel="noopener">
				<img src="${esc(photo.url)}" style="width:132px;height:99px;object-fit:cover;border-radius:6px;background:var(--bg-light-gray)" loading="lazy">
			</a>
			<div class="small text-muted" style="line-height:1.25">${esc(label)}${where}</div></div>`;
	}

	frappe.ui.form.on("Farm Task", {
		refresh(frm) {
			if (frm.is_new()) return;
			frappe.call({ method: "erpnext_mcp.asset_photos.desk_gallery", args: { task: frm.doc.name }, type: "GET" })
				.then((r) => {
					const data = (r && r.message) || {};
					if (!data.count) return;
					const html = (data.assignments || []).filter((a) => (a.evidence || []).length).map((a) =>
						`<div style="margin-bottom:6px"><b>${esc(a.assignment)}</b> · ${esc(a.state || "")}` +
						`${a.worker ? " · " + esc(a.worker) : ""}</div>` +
						a.evidence.filter((e) => e.type === "Photo").map(thumb).join("") +
						a.evidence.filter((e) => e.type !== "Photo").map((e) =>
							`<div class="small"><a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.type)}</a></div>`).join("")
					).join("<hr style='margin:8px 0'>");
					frm.dashboard.add_section(html, __("Photos ({0})", [data.count]));
				});
		},
	});

	frappe.ui.form.on("Asset Register", {
		refresh(frm) {
			if (frm.is_new()) return;
			frappe.call({ method: "erpnext_mcp.asset_photos.desk_asset_tasks", args: { asset: frm.doc.name }, type: "GET" })
				.then((r) => {
					const tasks = ((r && r.message) || {}).tasks || [];
					if (!tasks.length) return;
					const html = tasks.map((t) =>
						`<div style="margin-bottom:6px"><a href="/app/farm-task/${encodeURIComponent(t.task)}"><b>${esc(t.task)}</b></a>` +
						` ${esc(t.task_name || "")} · ${esc(t.state || "")}` +
						(t.assignment ? ` · <a href="/app/farm-task-assignment/${encodeURIComponent(t.assignment)}">${esc(t.assignment)}</a>` : "") +
						(t.completed_at ? ` · ${esc(frappe.datetime.str_to_user(t.completed_at))}` : "") + `</div>` +
						(t.photos || []).map(thumb).join("")
					).join("<hr style='margin:8px 0'>");
					frm.dashboard.add_section(html, __("Tasks & photos ({0})", [tasks.length]));
				});
		},
	});
})();
