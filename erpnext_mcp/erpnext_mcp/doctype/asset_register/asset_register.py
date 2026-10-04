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

		# v0.214.0. A FIXED asset's position changes only through an explicit,
		# logged move — see `asset_moves`. Here so the Desk form and every code
		# path that saves this document are covered, not only the tools.
		from erpnext_mcp import asset_moves

		asset_moves.guard(self)

		# v0.226.0. A tag minted on a phone encodes its UUID, and keeps it.
		self.qr_url = _build_qr_url(self.get("tag_uuid") or self.name)

		self._service_defaults_from_type()

	def _service_defaults_from_type(self):
		"""v0.230.5. A new asset, or one moved to another type, starts from that type's
		service interval — only where it has none of its own. A figure somebody set
		is never overwritten, and a type with no default changes nothing."""
		from erpnext_mcp import compat

		if not compat.has_field("Farm Asset Type", "default_service_interval_hours"):
			return
		if not self.is_new():
			before = frappe.db.get_value("Asset Register", self.name, "asset_type")
			if before == self.asset_type:
				return
		defaults = frappe.db.get_value(
			"Farm Asset Type",
			self.asset_type,
			["default_service_interval_hours", "default_service_interval_days"],
			as_dict=True,
		) or {}
		for field, default in (
			("service_interval_hours", defaults.get("default_service_interval_hours")),
			("service_interval_days", defaults.get("default_service_interval_days")),
		):
			if default and not self.get(field):
				self.set(field, default)

	def before_save(self):
		if self.current_state and isinstance(self.current_state, str):
			import json

			try:
				json.loads(self.current_state)
			except (json.JSONDecodeError, ValueError):
				frappe.throw(_("Current State must be valid JSON."))


def _build_qr_url(asset_name: str) -> str:
	"""The URL the QR code encodes.

	v0.216.0: `<farmops_public_url>/farmops/api/scan/<name>` when that setting is
	filled in — a page the sidecar really serves, so a tag needs nothing under
	/erpnext to be public. Otherwise `<public_url>/scan/<name>` as before. Either
	shape is unwound by `universal_scan.scan_target`, so every printed tag keeps
	resolving in the app.
	"""
	from urllib.parse import quote

	try:
		from erpnext_mcp import settings

		farmops = settings.farmops_public_url()
		public_url = settings.public_url()
	except Exception:
		farmops = public_url = ""
	if farmops:
		return f"{farmops}/farmops/api/scan/{quote(asset_name, safe='')}"
	base = (public_url or "").rstrip("/")
	if not base:
		base = frappe.utils.get_url()
	from urllib.parse import quote

	return f"{base}/scan/{quote(asset_name, safe='')}"


def tag_url(row: dict) -> str:
	"""What a tag for this asset encodes NOW, from the current settings.

	v0.230.4. Every renderer asks this rather than trusting the stored `qr_url`. The
	stored value is written only on save, so an asset saved before the Farm Ops
	cutover kept `<public url>/scan/<name>` — on OML `…/erpnext/scan/…`, a path no
	longer on the public Funnel — and a card printed from it opened nothing in a
	phone's camera.
	"""
	return _build_qr_url(str(row.get("tag_uuid") or row.get("name") or ""))


def regenerate_qr_urls() -> dict:
	"""Rewrite every stored `qr_url` that is not what `tag_url` says now. Idempotent.

	Runs after every migrate. A row already right is not touched; `modified` is not
	moved, because nothing about the asset changed — only where its tag points.
	"""
	from erpnext_mcp import compat

	report = {"checked": 0, "changed": 0, "examples": []}
	if not compat.doctype_exists("Asset Register"):
		return report
	fields = compat.existing_fields("Asset Register", ("name", "tag_uuid", "qr_url"))
	for row in frappe.db.get_all("Asset Register", fields=fields, limit=100000) or []:
		row = dict(row)
		report["checked"] += 1
		wanted = tag_url(row)
		if wanted and row.get("qr_url") != wanted:
			frappe.db.set_value("Asset Register", row["name"], "qr_url", wanted, update_modified=False)
			report["changed"] += 1
			if len(report["examples"]) < 3:
				report["examples"].append({"asset": row["name"], "was": row.get("qr_url"), "now": wanted})
	return report
