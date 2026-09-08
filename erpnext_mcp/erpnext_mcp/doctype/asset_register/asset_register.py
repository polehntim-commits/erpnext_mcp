# SPDX-License-Identifier: MIT
"""Controller for Asset Register — one tagged, scannable asset on the farm.

THE DOCNAME IS THE PRINTABLE ID. A worker reads "MC-Valve-05" off the label, and
that string is both the QR payload and the primary key. Naming is set-by-user
rather than auto-generated because the tag has to match what is already printed,
welded, or painted on the asset — a serial number the system invents would be a
second name nobody recognises in the field.

LOCATION IS A SELF-REFERENTIAL LINK. A valve belongs to a zone, a zone belongs
to a block, a block belongs to a ranch — all Asset Register records, forming a
tree as deep as the operation needs. The link is validated against this same
doctype, so the hierarchy is always traversable.

QR URL IS DERIVED, NOT TYPED. The controller builds it from the docname and the
site's public URL (when one is configured in MCP Settings), so a tag and a
record are always in step. An operator who changes a public_url re-saves the
asset and the QR points to the new address.

RETIREMENT IS SOFT. Setting `retired_at` takes the asset out of active lists and
stops compliance alerts, but the record, its history, and its tag all survive.
An asset removed from the register would be an asset whose spray logs, inspection
sessions, and water tests point at a docname that no longer exists.
"""

import frappe
from frappe import _
from frappe.model.document import Document

#: v0.162.0. REMOVED, AND ITS ABSENCE IS THE POINT. This tuple was the third of
#: four disagreeing answers to "what asset types are there" — it named ten, the
#: doctype's own Select named thirteen, and `tools/asset_tags` named twelve. It
#: was never consulted by anything: `validate` below checks only that
#: `asset_type` is set, so the list sat here looking authoritative and gating
#: nothing. `Farm Asset Type` is the register now, `asset_type` is a Link to it,
#: and Frappe's own link validation is what refuses a type this site has not got.


class AssetRegister(Document):
	def validate(self):
		if not self.asset_type:
			frappe.throw(_("Asset Type is required."))
		if not self.company:
			frappe.throw(_("Company is required — every asset belongs to somebody."))

		if self.location:
			if self.location == self.name:
				frappe.throw(_("An asset cannot be its own parent."))
			if not frappe.db.exists("Asset Register", self.location):
				frappe.throw(_("Location {0} does not exist in Asset Register.").format(self.location))

		if self.gps_latitude is not None and self.gps_latitude != 0:
			lat = float(self.gps_latitude or 0)
			if lat < -90 or lat > 90:
				frappe.throw(_("GPS Latitude must be between -90 and 90."))
		if self.gps_longitude is not None and self.gps_longitude != 0:
			lon = float(self.gps_longitude or 0)
			if lon < -180 or lon > 180:
				frappe.throw(_("GPS Longitude must be between -180 and 180."))

		self.qr_url = _build_qr_url(self.name)

	def before_save(self):
		if self.current_state and isinstance(self.current_state, str):
			import json

			try:
				json.loads(self.current_state)
			except (json.JSONDecodeError, ValueError):
				frappe.throw(_("Current State must be valid JSON."))


def _build_qr_url(asset_name: str) -> str:
	"""The URL the QR code encodes: /scan/<asset_name> on the site's public URL."""
	try:
		from erpnext_mcp import settings

		public_url = settings.public_url()
	except Exception:
		public_url = ""
	base = (public_url or "").rstrip("/")
	if not base:
		base = frappe.utils.get_url()
	from urllib.parse import quote

	return f"{base}/scan/{quote(asset_name, safe='')}"
