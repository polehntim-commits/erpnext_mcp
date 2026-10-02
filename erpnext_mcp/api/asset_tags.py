# SPDX-License-Identifier: MIT
"""The QR tag button on the Asset Register form. One whitelisted method. v0.83.0.

    POST /api/method/erpnext_mcp.api.asset_tags.asset_qr_tag

`asset_tag_sheet.render_asset_qr_sheet` answers the Asset Register LIST — thirty
valves ticked, one sheet of labels. This answers the FORM: one pump open in front
of somebody, and the question "where is its tag".

────────────────────────────────────────────────────────────────────────────
WHY THERE IS A DESK ROUTE AT ALL WHEN THERE IS ALREADY AN MCP TOOL
────────────────────────────────────────────────────────────────────────────

`generate_asset_qr` has existed since v0.17.0 and is reachable through the MCP
endpoint, which is the wrong door for a person. That endpoint takes a token an AI
client holds, answers JSON-RPC, and is gated by the `allow_<tool>` switches. A
mechanic with an Asset Register form open has a session, a role and a mouse, and
until this release the only way to get the symbol onto paper was to ask a model
for base64 and paste it somewhere.

THE `allow_<tool>` SWITCHES ARE DELIBERATELY NOT CONSULTED HERE, which is the
call `api/badges.py` made for the badge button, `api/gis.py` made for the map and
`api/__init__.py` made for the phone. Those switches are the AI's leash:
`allow_generate_asset_qr` off means "the model may not do this", and reading it
here would mean an operator who distrusts the model also loses the button on
their own form — which is not what they asked for and not what the switch says.

WHAT GATES IT INSTEAD is `frappe.has_permission("Asset Register", "read",
doc=<name>, throw=True)` on the SPECIFIC record, so a User Permission scoping
somebody to one company scopes this too.

`read` AND NOT `write`, WHICH IS THE WHOLE CHARACTER OF THIS BUTTON. Printing a
tag changes nothing: `generate_asset_qr` renders a symbol from `qr_url`, writes
no field, and stamps no `last_scan_at` — that is `scan_asset`'s job and it is
deliberately not done here, because generating a label is not a sighting of the
machine. Somebody who may look at the register may print its labels.
"""

from __future__ import annotations

import frappe

from .. import asset_tag_sheet, card_redirect
from ..errors import ToolError
from ..tools import asset_tags

ASSET_REGISTER = "Asset Register"


def _speaks_frappe(implementation, *args, **kwargs):
	"""Run the implementation, turning `ToolError` into a modal.

	A FUNCTION AND NOT A DECORATOR, for the reason `api/gis.speaks_frappe` gives at
	length: `frappe.call` reads a whitelisted callable's argument names with
	`inspect.getfullargspec`, which does not follow `functools.wraps`, so a
	decorated method presents as `(*args, **kwargs)` and Frappe answers that by
	forwarding the entire form dict — `cmd` and `csrf_token` included — into a
	function that never asked for them.

	A `ToolError` here is "no Asset Register record called that", "that name
	matches four assets", "this site has no QR encoder". Every one is a sentence
	somebody needs to read. Anything that is NOT a ToolError is left alone and
	still reaches the Error Log, because that is a bug.
	"""
	try:
		return implementation(*args, **kwargs)
	except ToolError as error:
		frappe.throw(str(error), title=frappe._("Asset QR Tag"))


@frappe.whitelist(methods=["POST"])
def asset_qr_tag(asset_name=None, template=None):
	"""Render one asset's QR tag and hand back the symbol and a printable label. Desk only."""
	return _speaks_frappe(_asset_qr_tag, asset_name=asset_name, template=template)


def _asset_qr_tag(asset_name=None, template=None):
	name = str(asset_name or "").strip()
	if not name:
		frappe.throw(frappe._("No asset was named."), title=frappe._("Asset QR Tag"))

	# THE SPECIFIC RECORD, not the doctype. See the module docstring — this is what
	# makes a User Permission that scopes somebody to one company scope the button.
	frappe.has_permission(ASSET_REGISTER, "read", doc=name, throw=True)

	result = asset_tags.generate_asset_qr({"asset_name": name, "format": "png"})
	data = result.data or {}

	# The register row, for the two lines of text under the symbol. Read AFTER the
	# tool so a docname the tool resolved by prefix — it accepts a partial and says
	# so — is the one described here, rather than the string somebody typed.
	resolved = str(data.get("asset_name") or name)
	try:
		row = asset_tags.asset_row(resolved)
	except ToolError:  # pragma: no cover - the tool above already resolved it
		row = {}

	blob = str(data.get("png_base64") or "")
	tag = {
		"asset_name": resolved,
		"asset_type": row.get("asset_type") or "",
		"location": row.get("location") or "",
		"png_base64": blob,
	}
	wanted = str(template or asset_tag_sheet.DEFAULT_TEMPLATE)

	return {
		"asset_name": resolved,
		"asset_type": tag["asset_type"],
		"location": tag["location"],
		"qr_url": data.get("qr_url"),
		# THE SYMBOL AS A data: URI, so the dialog can show it large without a
		# second authenticated request for a private File. There is no attachment
		# to fetch here and deliberately so: a tag is a rendering of a docname, not
		# a record, and filing a copy of every preview somebody pressed would fill
		# the sidebar of every asset on the site.
		"png_data_uri": f"data:image/png;base64,{blob}" if blob else "",
		"modules": data.get("modules"),
		"encoder": data.get("encoder"),
		# The printable document is ONE LABEL ON THE CHOSEN STOCK and not a scaled
		# picture of the symbol, so what comes out of the Print button lands inside
		# a die cut like the sheet does. Same layout, one tag.
		# v0.213.0. The CR80 asset tag card, unless the site asked for the old label.
		"html": (
			asset_tag_sheet.sheet_html([tag], [], wanted)
			if card_redirect.legacy()
			else card_redirect.document(ASSET_REGISTER, [resolved], title=f"Asset Tag — {resolved}")
		),
		"template": asset_tag_sheet.template_spec(wanted)["key"],
		"summary": result.summary,
	}


__all__ = ("asset_qr_tag",)
