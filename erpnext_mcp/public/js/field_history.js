// SPDX-License-Identifier: MIT
// v0.269.0. The Field form's History and "Add task here" — the same history the phone's field card shows
// (erpnext_mcp.api.field_desk). Costs appear only for the roles that read them.
frappe.ui.form.on("Field", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Add task here"), () => {
			frappe.new_doc("Farm Task", {
				location_doctype: "Field",
				location: frm.doc.name,
				company: frm.doc.owning_entity,
			});
		});
		const KINDS = ["task", "spray", "ipm", "phenology", "irrigation", "inspection", "harvest", "planting",
			"food_safety", "record", "note", "cost"];
		const year = new Date().getFullYear();
		const wrap = $(`<div class="field-history" style="margin-top:12px">
			<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
				<b>${__("History")}</b>
				<select class="form-control input-xs fh-kind" style="width:auto">
					<option value="">${__("All kinds")}</option>
					${KINDS.map((k) => `<option value="${k}">${frappe.utils.escape_html(k)}</option>`).join("")}
				</select>
				<select class="form-control input-xs fh-season" style="width:auto">
					<option value="">${__("All seasons")}</option>
					${[0, 1, 2, 3, 4].map((d) => `<option>${year - d}</option>`).join("")}
				</select>
			</div>
			<div class="fh-list" style="margin-top:8px"></div>
			<button class="btn btn-xs btn-default fh-more" style="display:none">${__("Older")}</button>
		</div>`);
		frm.dashboard.add_section(wrap, __("History"));
		let before = "";
		const load = (append) => {
			frappe.call({
				method: "erpnext_mcp.api.field_desk.field_history",
				args: { field: frm.doc.name, types: wrap.find(".fh-kind").val(), season: wrap.find(".fh-season").val(),
					before: append ? before : "" },
				callback: (r) => {
					const data = r.message || {};
					const rows = (data.events || []).map((e) => `<div style="padding:4px 0;border-bottom:1px solid var(--border-color)">
						<span class="text-muted">${frappe.utils.escape_html((e.when || "").slice(0, 16))}</span>
						<span class="indicator-pill grey" style="margin:0 6px">${frappe.utils.escape_html(e.kind)}</span>
						<a href="/app/${frappe.router.slug(e.doctype)}/${encodeURIComponent(e.docname || "")}">${frappe.utils.escape_html(e.title || "")}</a>
						<div class="text-muted small">${frappe.utils.escape_html(e.detail || "")}</div></div>`).join("");
					const list = wrap.find(".fh-list");
					if (append) list.append(rows); else list.html(rows || `<div class="text-muted">${__("Nothing recorded yet.")}</div>`);
					before = data.before || "";
					wrap.find(".fh-more").toggle(Boolean(data.more));
				},
			});
		};
		wrap.find(".fh-kind, .fh-season").on("change", () => load(false));
		wrap.find(".fh-more").on("click", () => load(true));
		load(false);
	},
});
