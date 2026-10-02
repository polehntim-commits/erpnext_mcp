# SPDX-License-Identifier: MIT
"""Universal asset tags, laid out on a label sheet. v0.83.0.

`tools/asset_tags.generate_asset_qr` and `generate_asset_qr_sheet` have produced
the symbols since v0.17.0 and have never produced a PAGE. An MCP client gets
base64 back and can do what it likes with it; a person with a roll of Avery 5160
in the printer has, until this release, had nowhere to send it. This is the page.

────────────────────────────────────────────────────────────────────────────
IT IS `badge_sheet.py` FOR ASSETS, AND DELIBERATELY THE SAME SHAPE
────────────────────────────────────────────────────────────────────────────

Same three-layer split, for the same reasons that file argues at length:

  * `tag_html` / `sheet_html` are PURE FUNCTIONS. No site, no `frappe.db`, no
    permission check — which is what lets the standalone suite hold the layout
    to the arithmetic without standing a bench up.
  * `render_asset_qr_sheet` is a thin whitelisted wrapper over the TOOL. Every
    gate that matters is `generate_asset_qr_sheet`'s own — the doctype check, the
    hundred-tag cap, the per-row "not found" — plus the permission check this
    module adds because a Desk caller has a session and the tool does not.
  * The Client Script (`asset_tag_list_action`) only posts a list of docnames.

────────────────────────────────────────────────────────────────────────────
WHY THE TAG CARRIES THE DOCNAME IN TEXT AS WELL AS IN THE SYMBOL
────────────────────────────────────────────────────────────────────────────

A QR on a pump housing lives outdoors. It gets sun, dust, hydraulic fluid and a
pressure washer, and the failure mode is not "the label falls off" — it is "the
symbol no longer decodes and the label is still firmly attached". A tag whose
docname is also printed underneath degrades into a label somebody can still type
into the search bar. That is the whole reason the caption is there, and it is why
it is printed even when the symbol failed to render.

THE SYMBOL ENCODES `qr_url`, WHICH IS WHAT THE REGISTER HOLDS. `Asset Register`
builds it as `<public url>/scan/<docname>` in `before_save`, and
`universal_scan` unwraps that back to the bare docname — see its `TagURLsAreUnwrapped`
test. So the docname IS in the symbol, wrapped in the URL that makes a camera
that is not the farm app do something useful with it. Encoding the bare docname
instead would break every phone that scans the tag with its own camera.
"""

from __future__ import annotations

import html
import json

import frappe

from .errors import ToolError

#: Letter, in millimetres. The same page `badge_sheet` prints on.
PAGE = {"width_mm": 215.9, "height_mm": 279.4}

#: The label stock this app knows the geometry of, in millimetres, measured from
#: the manufacturer's own template rather than derived from the page.
#:
#: `across` × `down` is what fits; `pitch_x`/`pitch_y` are centre-to-centre, which
#: is the measurement that matters on a sheet with no gutter — Avery 5160 rows
#: BUTT UP against each other, so a layout that assumed a gap would walk down the
#: page and every label after the third would print across a perforation.
TEMPLATES = {
	"avery_5160": {
		"label": "Avery 5160 — 30 per sheet (66.7 × 25.4mm)",
		"across": 3,
		"down": 10,
		"width_mm": 66.675,
		"height_mm": 25.4,
		"margin_top_mm": 12.7,
		"margin_left_mm": 4.7625,
		"pitch_x_mm": 69.85,
		"pitch_y_mm": 25.4,
	},
	"avery_5163": {
		"label": "Avery 5163 — 10 per sheet (101.6 × 50.8mm)",
		"across": 2,
		"down": 5,
		"width_mm": 101.6,
		"height_mm": 50.8,
		"margin_top_mm": 12.7,
		"margin_left_mm": 4.7625,
		"pitch_x_mm": 104.775,
		"pitch_y_mm": 50.8,
	},
}

#: What an unrecognised template name falls back to. A FALLBACK AND NOT A THROW:
#: the tool takes `template` as free text and always has, so a name this module
#: has never heard of has to produce paper rather than an error dialog. The sheet
#: says which stock it actually laid out on, so nobody loads the wrong roll.
DEFAULT_TEMPLATE = "avery_5160"


def template_spec(name: str) -> dict:
	"""The geometry for a template name, falling back to `DEFAULT_TEMPLATE`.

	Returns a copy carrying `key` and `requested`, so `sheet_html` can say "you
	asked for X, this is laid out as Y" without a second lookup.
	"""
	requested = str(name or "").strip().lower() or DEFAULT_TEMPLATE
	key = requested if requested in TEMPLATES else DEFAULT_TEMPLATE
	spec = dict(TEMPLATES[key])
	spec["key"] = key
	spec["requested"] = requested
	spec["substituted"] = key != requested
	return spec


def labels_per_page(spec: dict) -> int:
	return int(spec["across"]) * int(spec["down"])


def paginate(tags: list, spec: dict) -> list:
	"""Tags grouped into pages. Empty in, empty out."""
	rows = list(tags or [])
	size = labels_per_page(spec)
	return [rows[start : start + size] for start in range(0, len(rows), size)]


def _esc(value) -> str:
	"""Every value on a tag goes through this.

	`html.escape` rather than Frappe's `escape_html` so this file stays a pure
	function the standalone suite can call without a site — the same call
	`badge_sheet._esc` makes and for the same reason.
	"""
	return html.escape(str(value if value is not None else ""), quote=True)


def _qr_src(tag: dict) -> str:
	"""The tag's symbol as something an `<img>` can take, or "".

	`png_base64` is what `generate_asset_qr_sheet` returns. A bench with no QR
	encoder returns none, and that prints a tag with the docname and no symbol
	rather than a broken image icon — see the module docstring on why a tag with
	no symbol is still worth printing.
	"""
	blob = str(tag.get("png_base64") or "")
	return f"data:image/png;base64,{blob}" if blob else ""


def sheet_css(spec: dict) -> str:
	"""The grid, measured from `spec` rather than hard-coded.

	ABSOLUTE POSITIONING AND NOT A FLOW LAYOUT. Label stock is a grid of fixed
	physical positions and the only thing that matters is that ink lands inside
	the die cut. `inline-block` with margins — which is what the badge sheet does,
	correctly, for cards that get guillotined — accumulates rounding error down
	the page, and on stock with no vertical gutter a third of a millimetre per row
	is a label straddling a perforation by row ten.
	"""
	return """
  @page { size: Letter; margin: 0; }
  html, body { margin: 0; padding: 0; background: #ffffff; color: #111111;
               font-family: Helvetica, Arial, sans-serif; }
  .sheet-bar { padding: 10px 14px; border-bottom: 1px solid #d5dce3; background: #f4f6f8;
               font-size: 13px; color: #33404d; }
  .sheet-bar button { font: inherit; padding: 4px 12px; margin-right: 10px; cursor: pointer; }
  .sheet-bar .warn { color: #b3261e; }
  .sheet-note { font-size: 11px; color: #5a6672; padding: 8px 14px; }
  .tag-page { position: relative; width: %(page_w)smm; height: %(page_h)smm;
              overflow: hidden; }
  .tag-page + .tag-page { page-break-before: always; break-before: page; }
  .tag-cell { position: absolute; width: %(cell_w)smm; height: %(cell_h)smm;
              box-sizing: border-box; padding: 1.2mm 1.6mm;
              display: flex; align-items: center; gap: 1.6mm;
              overflow: hidden; }
  .tag-qr { width: %(qr)smm; height: %(qr)smm; flex: 0 0 auto; }
  .tag-text { min-width: 0; }
  .tag-name { font-size: %(name_pt)spt; font-weight: 700; line-height: 1.1;
              word-break: break-all; }
  .tag-meta { font-size: %(meta_pt)spt; color: #444444; line-height: 1.2;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tag-nosymbol { width: %(qr)smm; height: %(qr)smm; flex: 0 0 auto;
                  border: 0.3mm dashed #b9c4ce; box-sizing: border-box; }
  @media screen {
    .tag-page { outline: 1px solid #e2e8ee; margin: 12px auto; }
    .tag-cell { outline: 0.2mm dashed #cfd8e0; }
  }
  @media print {
    .no-print { display: none !important; }
    html, body { background: #ffffff !important; }
    .tag-page { outline: none; margin: 0; }
  }
""" % {
		"page_w": PAGE["width_mm"],
		"page_h": PAGE["height_mm"],
		"cell_w": spec["width_mm"],
		"cell_h": spec["height_mm"],
		# The symbol is square and sized off the SHORT side of the label, less the
		# padding, so it stays square on both stocks without a second constant.
		"qr": round(float(spec["height_mm"]) - 2.4, 2),
		"name_pt": 9 if float(spec["height_mm"]) < 30 else 13,
		"meta_pt": 6.5 if float(spec["height_mm"]) < 30 else 9,
	}


def tag_html(tag: dict, spec: dict, index: int) -> str:
	"""One label, positioned at its slot on the sheet.

	`index` is the position WITHIN THE PAGE, not within the run — `sheet_html`
	restarts it every page, because the slot is a physical place on a piece of
	paper.
	"""
	across = int(spec["across"])
	column = index % across
	row = index // across
	left = float(spec["margin_left_mm"]) + column * float(spec["pitch_x_mm"])
	top = float(spec["margin_top_mm"]) + row * float(spec["pitch_y_mm"])

	symbol = _qr_src(tag)
	parts = [f'<div class="tag-cell" style="left:{round(left, 3)}mm;top:{round(top, 3)}mm">']
	if symbol:
		parts.append(f'<img class="tag-qr" src="{symbol}" alt="">')
	else:
		parts.append('<div class="tag-nosymbol"></div>')

	meta = " · ".join(_esc(value) for value in (tag.get("asset_type"), tag.get("location")) if value)
	parts.append('<div class="tag-text">')
	parts.append(f'<div class="tag-name">{_esc(tag.get("asset_name"))}</div>')
	if meta:
		parts.append(f'<div class="tag-meta">{meta}</div>')
	parts.append("</div></div>")
	return "".join(parts)


def sheet_html(tags: list, errors: list | None = None, template: str = DEFAULT_TEMPLATE) -> str:
	"""The whole printable document, as a string. A PURE FUNCTION — no site needed.

	THE SKIPPED ASSETS ARE PRINTED ON THE PAGE, which is `badge_sheet.sheet_html`'s
	rule and matters more here, not less. The tool's contract is that one bad
	docname does not lose the sheet; that promise is only kept if whoever walks
	out to the shop with the paper can see WHICH pump did not get a label. Thirty
	assets that quietly came back as twenty-eight is two machines that stay
	untagged until somebody notices, which is a season.
	"""
	spec = template_spec(template)
	pages = paginate(tags, spec)
	skipped = list(errors or [])
	count = len(tags or [])

	body = [
		'<div class="sheet-bar no-print">',
		'<button type="button" onclick="window.print()">Print</button>',
		f"<span>{count} tag(s) on {max(1, len(pages))} sheet(s) of {_esc(spec['label'])}.</span>",
	]
	if spec["substituted"]:
		body.append(
			'<span class="warn"> &nbsp;Asked for &quot;'
			+ _esc(spec["requested"])
			+ "&quot;, which this app has no geometry for &mdash; laid out on "
			+ _esc(spec["key"])
			+ ". Check the stock in the printer.</span>"
		)
	if skipped:
		body.append(f'<span class="warn"> &nbsp;{len(skipped)} skipped &mdash; see below.</span>')
	body.append("</div>")
	body.append(
		'<div class="sheet-note no-print">Print at 100% &mdash; "fit to page" '
		"rescales the grid and every label lands off its die cut.</div>"
	)

	if skipped:
		body.append('<div class="sheet-note no-print"><b>No tag was printed for:</b><ul>')
		for entry in skipped:
			who = _esc(entry.get("asset_name") or "an unnamed entry")
			body.append(f"<li>{who} &mdash; {_esc(entry.get('error'))}</li>")
		body.append("</ul></div>")

	if not pages:
		body.append('<div class="sheet-note">No asset tags were produced.</div>')
	for page in pages:
		body.append('<div class="tag-page">')
		body.extend(tag_html(tag, spec, index) for index, tag in enumerate(page))
		body.append("</div>")

	return (
		"<!doctype html>\n"
		'<html><head><meta charset="utf-8">'
		"<title>Asset QR Tags</title>"
		f"<style>{sheet_css(spec)}</style>"
		"</head><body>" + "".join(body) + "</body></html>"
	)


ASSET_REGISTER = "Asset Register"


def _readable(names: list) -> tuple:
	"""Split the requested docnames into the ones this session may read, and errors.

	THE PERMISSION CHECK IS THIS MODULE'S AND NOT THE TOOL'S, which is the one
	thing a Desk route has to add. `generate_asset_qr_sheet` is reached through
	the MCP endpoint, where the gate is the `allow_<tool>` switch and the token;
	here there is a logged-in session, and a User Permission scoping somebody to
	one company has to scope the labels they can print.

	`read` and not `write`: a tag is a rendering of a docname somebody can already
	see, and it changes nothing on the record — `generate_asset_qr_sheet` writes
	nothing, deliberately, and does not stamp `last_scan_at`.

	Refused rows come back as ERRORS rather than as a throw, so one asset in
	another company does not lose the other twenty-nine labels.
	"""
	allowed, errors = [], []
	for raw in names or []:
		name = str(raw or "").strip()
		if not name:
			continue
		try:
			permitted = frappe.has_permission(ASSET_REGISTER, "read", doc=name)
		except Exception:
			# A docname that does not exist raises out of the permission check on
			# some Frappe versions. Let the tool report "not found" in its own
			# words rather than reporting it as a permission failure here.
			permitted = True
		if permitted:
			allowed.append(name)
		else:
			errors.append({"asset_name": name, "error": "you do not have access to this asset"})
	return allowed, errors


@frappe.whitelist(methods=["POST"])
def render_asset_qr_sheet(assets=None, template=None):
	"""Render QR tags for the named assets and hand back a printable sheet.

	A THIN WRAPPER OVER THE TOOL AND NOT A SECOND IMPLEMENTATION, which is the
	rule `badge_sheet.render_badge_sheet` states and the reason neither of these
	files ever draws a symbol itself. Every gate that matters is
	`generate_asset_qr_sheet`'s own — the register check, the hundred-tag cap, the
	per-docname "not found" — and this adds exactly one: the session's own read
	permission, per record.

	`ToolError` becomes `frappe.throw` for the reason `api/gis.speaks_frappe`
	gives: raised out of a whitelisted method it is an HTTP 500 and a traceback in
	the console, and the sentence the tool wrote never reaches the person who
	needs it. Anything that is NOT a ToolError is left alone and still reaches the
	Error Log, because that is a bug.
	"""
	from .tools import asset_tags

	names = assets
	if isinstance(names, str):
		text = names.strip()
		if text.startswith("["):
			# `frappe.call` posts a JS array as a JSON string.
			try:
				names = json.loads(text)
			except ValueError:
				frappe.throw(
					frappe._("The list of assets could not be read."),
					title=frappe._("Asset QR Sheet"),
				)
		else:
			names = [part.strip() for part in text.split(",") if part.strip()]

	if not isinstance(names, list) or not names:
		frappe.throw(frappe._("No assets were named."), title=frappe._("Asset QR Sheet"))

	allowed, refused = _readable(names)
	if not allowed:
		frappe.throw(
			frappe._("You do not have access to any of the assets you selected."),
			title=frappe._("Asset QR Sheet"),
		)

	wanted = str(template or DEFAULT_TEMPLATE)
	try:
		result = asset_tags.generate_asset_qr_sheet({"asset_names": allowed, "template": wanted})
	except ToolError as error:
		frappe.throw(str(error), title=frappe._("Asset QR Sheet"))
		return None  # pragma: no cover - frappe.throw does not return

	data = result.data or {}
	errors = list(data.get("errors") or []) + refused
	# v0.213.0. THE SHEET IS NOW THE CR80 ASSET TAG CARD — see `card_redirect`.
	from . import card_redirect

	if card_redirect.legacy():
		html = sheet_html(data.get("labels") or [], errors, wanted)
	else:
		html = card_redirect.document(
			"Asset Register",
			[label.get("asset_name") for label in data.get("labels") or []],
			errors,
			"Asset Tag Cards",
		)
	return {
		"html": html,
		"label_count": data.get("label_count") or 0,
		"errors": errors,
		"template": template_spec(wanted)["key"],
		"summary": result.summary,
	}


__all__ = (
	"DEFAULT_TEMPLATE",
	"PAGE",
	"TEMPLATES",
	"labels_per_page",
	"paginate",
	"render_asset_qr_sheet",
	"sheet_css",
	"sheet_html",
	"tag_html",
	"template_spec",
)
