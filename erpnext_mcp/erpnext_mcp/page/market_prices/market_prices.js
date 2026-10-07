// SPDX-License-Identifier: MIT
//
// /app/market-prices — USDA AMS market prices in the Desk. v0.266.0 (Tim, 2026-10-06).
//
//   * ONE SOURCE OF TRUTH: it draws erpnext_mcp.api.market_dashboard.view, which is market_prices.chart() /
//     card() — the phone's and MCP's answers.
//   * STOCK-MARKET STYLE: candles per size (the derivation is the contract's: open / close = first / last quote's
//     mostly-mid, high / low = max high / min low, Priced rows only), a volume bar pane UNDER the price pane on the
//     same time axis (two synced charts: lightweight-charts 4 has no native second pane), crosshair, zoom and pan.
//   * NO CARRY-FORWARD: a weekday with no quote is a visible gap (whitespace, never a filled bar), and a day the
//     report listed the size without a price carries a "no quote" marker. Nothing is interpolated.
//   * SHIPPING POINT IS THE PRIMARY LINE. Terminal is an optional overlay; terminal − shipping is the COST OF
//     MARKET ACCESS, never margin. Grower return $/lb and breakeven $/lb are optional overlays on a left scale.
//   * The library is VENDORED (/assets/erpnext_mcp/vendor/lightweight-charts, Apache-2.0, attribution kept).
//     Season-over-season and the weekly bars use the Desk's own frappe.Chart. No CDN anywhere.

frappe.pages["market-prices"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Market Prices"), single_column: true });
	wrapper.market = erpnext_mcp_market_prices(page);
};

frappe.pages["market-prices"].on_page_show = function (wrapper) {
	if (wrapper.market) wrapper.market.start();
};

function erpnext_mcp_market_prices(page) {
	const API = "erpnext_mcp.api.market_dashboard.";
	const LIB = "/assets/erpnext_mcp/vendor/lightweight-charts/lightweight-charts.standalone.production.js";
	const UP = "#1a7f37", DOWN = "#c62828", TERMINAL = "#6f42c1", RETURN = "#0b6bcb", BREAKEVEN = "#b26a00";

	const $body = $(frappe.render_template("market_prices")).appendTo(page.main);
	const $tip = $body.find(".mk-tip");
	const fields = {};
	let choices = null, answer = null, library = null, charts = [];

	function load_library() {
		if (window.LightweightCharts) return Promise.resolve(window.LightweightCharts);
		if (!library) {
			library = new Promise((resolve, reject) => {
				const tag = document.createElement("script");
				tag.src = LIB;
				tag.onload = () => (window.LightweightCharts ? resolve(window.LightweightCharts) : reject(new Error("empty")));
				tag.onerror = reject;
				document.head.appendChild(tag);
			});
		}
		return library;
	}

	function control(df, $parent) {
		const f = frappe.ui.form.make_control({ df, parent: $("<div>").appendTo($parent), render_input: true });
		f.refresh();
		return f;
	}

	function build_controls() {
		const $f = $body.find(".mk-filters").empty();
		fields.commodity = control({ fieldtype: "Select", label: __("Commodity"), fieldname: "commodity",
			options: choices.commodities.map((c) => ({ value: c.key, label: c.title })), change: () => start(fields.commodity.get_value()) }, $f);
		fields.commodity.set_value(choices.commodity);
		fields.size = control({ fieldtype: "Select", label: __("Size"), fieldname: "size",
			options: [{ value: "", label: __("All sizes (small multiples)") }].concat(choices.sizes.map((s) => ({ value: s, label: s }))), change: reload }, $f);
		fields.variety = control({ fieldtype: "Select", label: __("Variety"), fieldname: "variety", options: [""].concat(choices.varieties), change: reload }, $f);
		fields.district = control({ fieldtype: "Select", label: __("District"), fieldname: "district", options: [""].concat(choices.districts), change: reload }, $f);
		fields.interval = control({ fieldtype: "Select", label: __("Candles"), fieldname: "interval",
			options: [{ value: "day", label: __("Daily") }, { value: "week", label: __("Weekly") }], change: reload }, $f);
		fields.interval.set_value("day");
		fields.season = control({ fieldtype: "Select", label: __("Season"), fieldname: "season", options: [""].concat(choices.seasons.map(String)), change: reload }, $f);
		fields.from_date = control({ fieldtype: "Date", label: __("From"), fieldname: "from_date", change: reload }, $f);
		fields.to_date = control({ fieldtype: "Date", label: __("To"), fieldname: "to_date", change: reload }, $f);
		const $o = $body.find(".mk-overlays").empty().append($("<strong>").text(__("Overlays:")));
		[["terminal", __("Terminal (context)")], ["grower_return", __("Grower return $/lb")], ["breakeven", __("Breakeven $/lb")]].forEach(([k, l]) => {
			const $l = $("<label>").appendTo($o);
			$("<input type='checkbox'>").attr("value", k).appendTo($l).on("change", reload);
			$l.append(document.createTextNode(l));
		});
		if (choices.headline_size && choices.sizes.includes(choices.headline_size)) fields.size.set_value(choices.headline_size);
	}

	function params() {
		return {
			commodity: fields.commodity.get_value(), size: fields.size.get_value() || null, variety: fields.variety.get_value() || null,
			district: fields.district.get_value() || null, interval: fields.interval.get_value() || "day",
			season: fields.season.get_value() || null, from_date: fields.from_date.get_value() || null, to_date: fields.to_date.get_value() || null,
			overlays: $body.find(".mk-overlays input:checked").map((_, el) => el.value).get().join(","),
		};
	}

	function reload() {
		if (!choices) return;
		frappe.call({ method: API + "view", args: params() }).then((r) => {
			answer = r.message;
			draw_card(answer.card);
			draw_charts();
			draw_seasons();
			draw_percentiles();
			draw_issues();
			$body.find(".mk-caveat").text(answer.card.caveat || "");
		});
	}

	function money(v, d = 2) { return v == null ? "—" : "$" + Number(v).toFixed(d); }
	function pct(v) { return v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(1)}%`; }

	function draw_card(c) {
		const $c = $body.find(".mk-card").empty();
		const tile = (k, v, s, cls) => $(`<div class="mk-tile"><div class="mk-k"></div><div class="mk-v ${cls || ""}"></div><div class="mk-s"></div></div>`)
			.find(".mk-k").text(k).end().find(".mk-v").text(v).end().find(".mk-s").text(s || "").end().appendTo($c);
		const head = (c.sizes || []).find((s) => s.size === c.headline_size) || {};
		tile(__("Shipping point · {0}", [c.headline_size || "—"]), money(head.mid), `${c.unit || ""} · ${c.as_of || __("no quote yet")}`);
		tile(__("Week on week"), pct(head.change_wow_pct), (c.signal || {}).label ? `${c.signal.label} — ${c.signal.why}` : "", head.change_wow_pct > 0 ? "mk-up" : head.change_wow_pct < 0 ? "mk-down" : "");
		tile(__("Grower return"), c.grower_return_per_lb == null ? "—" : money(c.grower_return_per_lb) + "/lb", __("after configured deductions"));
		if (c.breakeven) {
			const gap = c.above_breakeven_per_lb;
			tile(__("vs breakeven {0}/lb", [money(c.breakeven.per_lb)]), gap == null ? "—" : `${gap >= 0 ? "+" : "−"}${money(Math.abs(gap))}/lb`,
				c.breakeven.label, gap == null ? "" : gap >= 0 ? "mk-up" : "mk-down");
		}
		if (c.terminal) tile(__("Terminal (context)"), money(c.terminal.mid), __("cost of market access {0} · {1}", [money(c.terminal.cost_of_market_access), c.terminal.market]));
		if (c.volume) tile(__("Movement"), `${c.volume.value} ${c.volume.unit}`, `${__("week of")} ${c.volume.report_date} · ${pct(c.volume.change_wow_pct)}`);
		if (c.data_issues_open) tile(__("Data to look at"), String(c.data_issues_open), __("open market data issues"), "mk-down");
	}

	function weekdays_between(a, b) {
		const out = [];
		const d = frappe.datetime.str_to_obj(a), end = frappe.datetime.str_to_obj(b);
		while (d <= end) {
			if (d.getDay() !== 0 && d.getDay() !== 6) out.push(frappe.datetime.obj_to_str(d).slice(0, 10));
			d.setDate(d.getDate() + 1);
		}
		return out;
	}

	// Candles with WHITESPACE for every weekday without a quote, so a gap shows as a gap.
	function candle_data(series, interval, span) {
		const have = Object.fromEntries(series.candles.map((c) => [c.time, c]));
		const times = interval === "week" ? series.candles.map((c) => c.time) : span;
		return times.map((t) => (have[t] ? { time: t, open: have[t].open, high: have[t].high, low: have[t].low, close: have[t].close } : { time: t }));
	}

	function make_chart(LWC, el, height, opts) {
		const chart = LWC.createChart(el, Object.assign({
			height, autoSize: true, layout: { background: { color: "transparent" }, textColor: getComputedStyle(document.body).color, attributionLogo: true },
			grid: { vertLines: { color: "rgba(128,128,128,0.12)" }, horzLines: { color: "rgba(128,128,128,0.12)" } },
			timeScale: { borderVisible: false }, rightPriceScale: { borderVisible: false }, crosshair: { mode: 1 },
		}, opts || {}));
		charts.push(chart);
		return chart;
	}

	function clear_charts() {
		charts.forEach((c) => c.remove());
		charts = [];
	}

	function draw_charts() {
		load_library().then((LWC) => {
			clear_charts();
			const chart = answer.chart;
			const all = chart.series.flatMap((s) => s.candles.map((c) => c.time).concat(s.not_quoted_dates || [])).sort();
			const span = all.length ? weekdays_between(all[0], all[all.length - 1]) : [];
			const single = !!fields.size.get_value();
			$body.find(".mk-single").toggle(single);
			$body.find(".mk-multiples").toggle(!single);
			if (single) draw_single(LWC, chart, span);
			else draw_multiples(LWC, chart, span);
		}, () => $tip.text(__("The chart library did not load (/assets/erpnext_mcp/vendor/lightweight-charts).")));
	}

	function markers(series) {
		return (series.not_quoted_dates || []).map((t) => ({ time: t, position: "belowBar", shape: "circle", color: "#9e9e9e", text: __("no quote") }));
	}

	function draw_single(LWC, chart, span) {
		const $p = $body.find(".mk-price").empty(), $v = $body.find(".mk-volume").empty();
		const price = make_chart(LWC, $p[0], 380, { leftPriceScale: { visible: true, borderVisible: false } });
		const volume = make_chart(LWC, $v[0], 120, { timeScale: { visible: true } });
		const s = chart.series[0] || { candles: [], not_quoted_dates: [] };
		const candles = price.addCandlestickSeries({ upColor: UP, downColor: DOWN, borderVisible: false, wickUpColor: UP, wickDownColor: DOWN,
			title: `${s.size || ""} · ${chart.unit}` });
		candles.setData(candle_data(s, chart.interval, span));
		candles.setMarkers(markers(s));
		if (chart.terminal) {
			const t = price.addLineSeries({ color: TERMINAL, lineWidth: 1, lineStyle: 2, title: __("terminal (context)") });
			t.setData(chart.terminal.filter((p) => p.value != null));
		}
		let ret = null;
		if (chart.grower_return) {
			ret = price.addLineSeries({ color: RETURN, lineWidth: 2, priceScaleId: "left", title: __("grower $/lb") });
			ret.setData(chart.grower_return.filter((p) => p.value != null));
		}
		if (chart.breakeven) {
			const host = ret || price.addLineSeries({ priceScaleId: "left", color: "transparent", lastValueVisible: false, priceLineVisible: false });
			if (!ret) host.setData(span.length ? [{ time: span[0], value: chart.breakeven.per_lb }] : []);
			host.createPriceLine({ price: chart.breakeven.per_lb, color: BREAKEVEN, lineWidth: 2, lineStyle: 1, axisLabelVisible: true,
				title: __("breakeven {0}/lb", [money(chart.breakeven.per_lb)]) });
		}
		const bars = volume.addHistogramSeries({ color: "rgba(11,107,203,0.45)", priceFormat: { type: "volume" } });
		bars.setData((chart.volume || []).map((v) => ({ time: v.time, value: v.value })));
		sync(price, volume, candles, bars, chart);
		price.timeScale().fitContent();
	}

	function sync(price, volume, candles, bars, chart) {
		let busy = false;
		price.timeScale().subscribeVisibleLogicalRangeChange((r) => { if (!busy && r) { busy = true; volume.timeScale().setVisibleLogicalRange(r); busy = false; } });
		volume.timeScale().subscribeVisibleLogicalRangeChange((r) => { if (!busy && r) { busy = true; price.timeScale().setVisibleLogicalRange(r); busy = false; } });
		price.subscribeCrosshairMove((p) => {
			if (!p || !p.time) { $tip.text(""); return; }
			const c = p.seriesData.get(candles);
			const v = (chart.volume || []).find((x) => x.time === p.time);
			const nq = ((chart.series[0] || {}).not_quoted_dates || []).includes(p.time);
			$tip.text(c && c.open != null
				? `${p.time} · O ${money(c.open)} H ${money(c.high)} L ${money(c.low)} C ${money(c.close)}${v ? ` · ${v.value} ${v.unit}` : ""}`
				: `${p.time} · ${nq ? __("listed, no quote") : __("no quote in the report")}`);
		});
	}

	function draw_multiples(LWC, chart, span) {
		const $m = $body.find(".mk-multiples").empty();
		chart.series.forEach((s) => {
			const $box = $("<div class='mk-mini'>").appendTo($m);
			$("<div class='mk-mini-title'>").text(`${s.size || "—"} · ${s.candles.length} ${__("quoted")}${s.not_quoted_dates.length ? ` · ${s.not_quoted_dates.length} ${__("no quote")}` : ""}`).appendTo($box);
			const c = make_chart(LWC, $box[0], 220);
			const series = c.addCandlestickSeries({ upColor: UP, downColor: DOWN, borderVisible: false, wickUpColor: UP, wickDownColor: DOWN });
			series.setData(candle_data(s, chart.interval, span));
			series.setMarkers(markers(s));
			c.timeScale().fitContent();
		});
	}

	function draw_seasons() {
		const $s = $body.find(".mk-seasons").empty();
		const seasons = answer.chart.seasons || [];
		if (!seasons.length) { $s.text(__("Pick no single season to compare seasons.")); return; }
		const weeks = Array.from(new Set(seasons.flatMap((s) => s.week_of_season.map((w) => w.week)))).sort((a, b) => a - b);
		new frappe.Chart($s[0], {
			type: "line", height: 240, colors: ["#0b6bcb", "#b26a00", "#1a7f37", "#6f42c1", "#c62828"],
			data: { labels: weeks.map((w) => __("Wk {0}", [w])),
				datasets: seasons.map((s) => ({ name: String(s.season), values: weeks.map((w) => { const p = s.week_of_season.find((x) => x.week === w); return p ? p.close : null; }) })) },
			lineOptions: { spline: 0, hideDots: 0 }, axisOptions: { xIsSeries: 1 },
		});
	}

	function draw_percentiles() {
		const $p = $body.find(".mk-pct").empty();
		const curve = answer.season_curve || {};
		if (curve.peak_week) $("<p class='text-muted small'>").text(__("Harvest timing: across {0} season(s) the weekly close has usually peaked in week {1} of the season{2}.",
			[curve.seasons, curve.peak_week, curve.current_week ? __("; this season is in week {0}", [curve.current_week]) : ""])).appendTo($p);
		const rows = answer.percentiles || [];
		if (!rows.length) { $p.text(__("No priced history yet.")); return; }
		const $t = $("<table class='table table-sm'>").appendTo($p);
		$("<tr>").append(["Season", "Quotes", "p10", "p50", "p90"].map((h) => $("<th>").text(__(h)))).appendTo($t);
		rows.forEach((r) => $("<tr>").append([r.season, r.quotes, money(r.p10), money(r.p50), money(r.p90)].map((v) => $("<td>").text(v))).appendTo($t));
	}

	function draw_bars() {
		const $b = $body.find(".mk-bars").empty();
		const weekly = {};
		(answer.chart.series || []).forEach((s) => {
			const byWeek = {};
			s.candles.forEach((c) => {
				const d = frappe.datetime.str_to_obj(c.time); d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
				byWeek[frappe.datetime.obj_to_str(d).slice(0, 10)] = c.close;
			});
			weekly[s.size || "—"] = byWeek;
		});
		const labels = Array.from(new Set(Object.values(weekly).flatMap((w) => Object.keys(w)))).sort();
		if (!labels.length) return;
		new frappe.Chart($b[0], {
			type: "bar", height: 240,
			data: { labels, datasets: Object.entries(weekly).map(([size, w]) => ({ name: size, values: labels.map((l) => (l in w ? w[l] : null)) })) },
			barOptions: { spaceRatio: 0.3 },
		});
	}

	function draw_issues() {
		draw_bars();
		const $i = $body.find(".mk-issues").empty();
		if (!answer.issues.length) { $i.text(__("Nothing open.")); return; }
		const $ul = $("<ul>").appendTo($i);
		answer.issues.forEach((x) => $("<li>").append($(`<a href="/app/market-data-issue/${encodeURIComponent(x.name)}">`).text(
			`${x.kind} · ${x.report_slug || ""} ${x.report_date || ""} — ${x.detail || ""}${x.count > 1 ? ` (${x.count})` : ""}`)).appendTo($ul));
	}

	function start(commodity) {
		frappe.call({ method: API + "choices", args: { commodity: commodity || (choices && choices.commodity) || null } }).then((r) => {
			choices = r.message;
			build_controls();
			reload();
		});
	}

	return { start, reload };
}
