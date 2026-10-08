// SPDX-License-Identifier: MIT
// v0.276.0. The packer portal page. Everything comes from ./data (only this share's committed blocks); nothing is
// inlined into the HTML. Text is set with textContent, never innerHTML. Downloads are links to ./download.
(function () {
	"use strict";
	var base = location.pathname.replace(/\/+$/, "");
	function el(tag, text, cls) { var e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; }
	function sec(title) { var s = el("section"); s.appendChild(el("h2", title)); document.getElementById("body").appendChild(s); return s; }
	function table(parent, headers, rows) {
		var t = el("table"), head = el("tr");
		headers.forEach(function (h) { head.appendChild(el("th", h)); }); t.appendChild(head);
		rows.forEach(function (r) { var tr = el("tr"); r.forEach(function (v) { tr.appendChild(el("td", v == null ? "" : String(v))); }); t.appendChild(tr); });
		if (!rows.length) { var tr = el("tr"), td = el("td", "None this season."); td.colSpan = headers.length; tr.appendChild(td); t.appendChild(tr); }
		parent.appendChild(t);
	}
	function download(parent, label, fmt, block, season) {
		var a = el("a", label, "btn alt");
		a.href = base + "/download?format=" + fmt + (block ? "&block=" + encodeURIComponent(block) : "") + "&season=" + encodeURIComponent(season);
		a.rel = "noopener noreferrer"; parent.appendChild(a);
	}
	fetch(base + "/data", { credentials: "omit" }).then(function (r) {
		if (!r.ok) throw new Error("This link is not available.");
		return r.json();
	}).then(function (d) {
		document.getElementById("title").textContent = d.grower + " — for " + d.packer;
		document.getElementById("dates").textContent = "Season " + d.season + " · " + d.blocks.map(function (b) { return b.block + (b.variety ? " (" + b.variety + ")" : ""); }).join(", ");
		var dl = sec("Download");
		download(dl, "Spray record (CSV)", "csv", "", d.season);
		download(dl, "Workbook (XLSX)", "xlsx", "", d.season);
		download(dl, "PDF pack", "pdf", "", d.season);
		d.blocks.forEach(function (b) { download(dl, "PDF — " + b.block, "pdf", b.block, d.season); });
		if (d.clearance) {
			table(sec("Clear to harvest"), ["Block", "Variety", "Status", "PHI clears", "Re-entry until", "Why"],
				d.clearance.map(function (r) { return [r.block, r.variety, r.status, r.phi_clears_on, r.open_rei_until, (r.reasons || []).join("; ")]; }));
		}
		if (d.sprays) {
			table(sec("Sprays"), ["Date", "Block", "Product", "EPA Reg No", "Rate", "REI h", "PHI d"].concat(d.markets.map(function (m) { return "MRL " + m; })),
				d.sprays.map(function (r) { return [r.date, r.block, r.product, r.epa_reg_number, (r.rate || "") + " " + (r.rate_uom || ""), r.rei_hours, r.phi_days]
					.concat(d.markets.map(function (m) { return (r.mrl || {})[m]; })); }));
		}
		if (d.ipm) {
			table(sec("IPM"), ["Date", "Block", "Type", "Threat", "Count", "% affected", "Over threshold"],
				d.ipm.map(function (r) { return [r.date, r.block, r.type, r.threat, r.count, r.percent_affected, r.threshold_exceeded ? "yes" : ""]; }));
		}
		if (d.projections) {
			table(sec("Projections"), ["Block", "Variety", "Acres", "t/ac", "Projected t", "Window"],
				d.projections.map(function (r) { return [r.block, r.variety, r.acres, r.tons_per_acre, r.projected_tons, [r.window_start, r.window_end].filter(Boolean).join(" → ")]; }));
		}
	}).catch(function (e) {
		document.getElementById("title").textContent = "Not available";
		document.getElementById("body").appendChild(el("p", e.message));
	});
})();
