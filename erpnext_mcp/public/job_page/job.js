// SPDX-License-Identifier: MIT
// v0.271.0. The public Contractor Job page. Everything it shows comes from ./data (only this job); nothing is
// inlined into the HTML. Text is set with textContent, never innerHTML.
(function () {
	"use strict";
	var base = location.pathname.replace(/\/+$/, "");
	function el(tag, text, cls) { var e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; }
	function sec(title) { var s = el("section"); s.appendChild(el("h2", title)); document.getElementById("body").appendChild(s); return s; }
	function post(path, body, type) {
		return fetch(base + path, { method: "POST", headers: { "Content-Type": type || "application/json" }, body: body, credentials: "omit" })
			.then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || "Not saved"); return j; }); });
	}
	function directions(p) {
		var q = p.lat + "," + p.lon;
		var apple = "https://maps.apple.com/?daddr=" + q, google = "https://www.google.com/maps/dir/?api=1&destination=" + q;
		return /iPhone|iPad|Macintosh/.test(navigator.userAgent) ? apple : google;
	}
	fetch(base + "/data", { credentials: "omit" }).then(function (r) {
		if (!r.ok) throw new Error("This link is not available.");
		return r.json();
	}).then(function (d) {
		document.getElementById("title").textContent = d.title;
		document.getElementById("dates").textContent = [d.start_date, d.end_date].filter(Boolean).join(" → ");
		var map = L.map("map", { zoomControl: true });
		var sat = L.tileLayer("https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
			{ maxZoom: 19, attribution: "Imagery: USGS The National Map" });
		var street = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
			{ maxZoom: 19, attribution: "&copy; OpenStreetMap contributors" });
		sat.addTo(map); L.control.layers({ "Satellite": sat, "Streets": street }).addTo(map);
		var group = L.featureGroup().addTo(map);
		(d.blocks || []).forEach(function (b) { L.geoJSON(b.geojson, { style: { color: "#ffd400", weight: 3, fillOpacity: 0.08 } }).bindTooltip(b.label).addTo(group); });
		(d.hazards || []).forEach(function (h) {
			L.circleMarker([h.lat, h.lon], { radius: 8, color: h.kind === "valve" ? "#d1242f" : "#bf8700", weight: 3, fillOpacity: 0.7 })
				.bindTooltip(h.label).addTo(group);
		});
		var go = d.delivery_spot || d.entrance;
		if (d.entrance) L.marker([d.entrance.lat, d.entrance.lon]).bindTooltip("Entrance").addTo(group);
		if (d.delivery_spot) L.marker([d.delivery_spot.lat, d.delivery_spot.lon]).bindTooltip(d.delivery_spot.label).addTo(group);
		if (group.getLayers().length) map.fitBounds(group.getBounds().pad(0.15)); else map.setView([45.58, -121.2], 13);

		var where = sec("Getting there");
		if (go) { var a = el("a", "Directions", "btn"); a.href = directions(d.entrance || go); a.rel = "noopener noreferrer"; where.appendChild(a); }
		if (d.gate_notes) where.appendChild(el("p", d.gate_notes));
		if (d.hazards && d.hazards.length) where.appendChild(el("p", d.hazards.length + " marked hazard(s) on the map — red: valves to protect, amber: avoid.", "small"));

		var work = sec("The job");
		work.appendChild(el("p", d.scope));
		if (d.expected && d.expected.length) {
			var ul = el("ul"); d.expected.forEach(function (x) { ul.appendChild(el("li", x.qty + " " + (x.uom || "") + " — " + x.item)); }); work.appendChild(ul);
		}
		if (d.sop_link) { var s = el("a", "Safety procedure", "btn alt"); s.href = d.sop_link; s.rel = "noopener noreferrer"; work.appendChild(s); }

		if (d.contact && (d.contact.name || d.contact.phone)) {
			var c = sec("Farm contact"); c.appendChild(el("p", d.contact.name || ""));
			if (d.contact.phone) { var t = el("a", "Call " + d.contact.phone, "btn alt"); t.href = "tel:" + d.contact.phone.replace(/[^0-9+]/g, ""); c.appendChild(t); }
		}

		var act = sec("Report");
		var msg = el("p", "", "small"); var note = el("textarea"); note.rows = 2; note.placeholder = "Note (optional)";
		var who = el("input"); who.placeholder = "Your name (optional)";
		var done = (d.done || []).map(function (e) { return e.event; });
		// v0.275.0. Counts with the tap (pollination: hives, pallets, frame strength).
		var countInputs = {};
		(d.actions.counts || []).forEach(function (k) {
			var label = el("label", k.replace(/_/g, " ") + " "); var i = el("input"); i.type = "number"; i.min = "0";
			i.inputMode = "decimal"; label.appendChild(i); countInputs[k] = i; act.appendChild(label);
		});
		function counts() { var c = {}; Object.keys(countInputs).forEach(function (k) { if (countInputs[k].value !== "") c[k] = Number(countInputs[k].value); }); return c; }
		(d.actions.events || []).forEach(function (ev) {
			var b = el("button", done.indexOf(ev) >= 0 ? ev + " ✓" : ev, "btn"); b.disabled = done.indexOf(ev) >= 0;
			b.onclick = function () { post("/event", JSON.stringify({ event: ev, note: note.value, name: who.value, counts: counts() }))
				.then(function () { b.textContent = ev + " ✓"; b.disabled = true; msg.textContent = "Saved. Thank you."; msg.className = "ok"; })
				.catch(function (e) { msg.textContent = e.message; msg.className = "err"; }); };
			act.appendChild(b);
		});
		if (d.actions.photos) {
			var f = el("input"); f.type = "file"; f.accept = "image/*"; f.capture = "environment"; f.style.display = "none";
			var p = el("button", d.kind === "Contractor Work" ? "Add photo" : "Photo of the ticket", "btn alt");
			p.onclick = function () { f.click(); };
			f.onchange = function () { if (!f.files[0]) return; msg.textContent = "Sending…";
				post("/photo?kind=" + (d.kind === "Contractor Work" ? "photo" : "ticket"), f.files[0], f.files[0].type || "image/jpeg")
					.then(function () { msg.textContent = "Photo saved."; msg.className = "ok"; })
					.catch(function (e) { msg.textContent = e.message; msg.className = "err"; }); };
			act.appendChild(p); act.appendChild(f);
		}
		if (d.actions.note) { act.appendChild(note); var n = el("button", "Send note", "btn alt");
			n.onclick = function () { if (!note.value.trim()) return; post("/event", JSON.stringify({ event: "Note", note: note.value, name: who.value }))
				.then(function () { note.value = ""; msg.textContent = "Note sent."; msg.className = "ok"; })
				.catch(function (e) { msg.textContent = e.message; msg.className = "err"; }); };
			act.appendChild(n); }
		act.appendChild(who); act.appendChild(msg);
	}).catch(function (e) {
		document.getElementById("title").textContent = "Not available";
		document.getElementById("body").appendChild(el("p", e.message));
	});
})();
