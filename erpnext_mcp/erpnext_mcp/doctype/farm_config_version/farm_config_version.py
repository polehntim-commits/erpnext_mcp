# SPDX-License-Identifier: MIT
"""Controller for Farm Config Version. v0.207.0.

The docname is "<kind_slug>:<key>@<version>". ONLY A DRAFT'S BODY MAY CHANGE:
once a version has been staged or published, a save whose body hashes
differently from the stored `body_hash` is refused, and so is a change to the
kind, key or version. Status moves belong to `phone_config` (stage, publish,
rollback, retire); a Desk edit of `status` alone is refused.
docs/design/phone_config_and_compliance_loop.md §1.2.
"""

import frappe
from frappe.model.document import Document

from erpnext_mcp import phone_config


class FarmConfigVersion(Document):
	def autoname(self):
		self.name = phone_config.name_of(self.config_kind, self.config_key, self.version)

	def validate(self):
		if not phone_config.KEY_PATTERN.match(str(self.config_key or "")):
			frappe.throw(f"{self.config_key!r} is not a lower_snake_case key.")
		fresh = phone_config.body_hash(self.body_json or "{}")
		stored = None
		if self.name and frappe.db.exists(phone_config.DOCTYPE, self.name):
			stored = frappe.db.get_value(
				phone_config.DOCTYPE,
				self.name,
				["status", "body_hash", "config_kind", "config_key", "version"],
				as_dict=True,
			)
		if not stored:
			self.body_hash = fresh
			return
		for field in ("config_kind", "config_key", "version"):
			if str(stored.get(field)) != str(self.get(field)):
				frappe.throw(f"{field} cannot change; make a new version instead.")
		if stored.get("status") != phone_config.DRAFT and fresh != stored.get("body_hash"):
			frappe.throw(
				f"{self.name} is {stored.get('status')}: its body is immutable. Make a new Draft "
				"(update_wizard_definition / update_tile / update_label_profile)."
			)
		if stored.get("status") != self.status and not self.flags.get("lifecycle"):
			frappe.throw("status changes through stage/publish/rollback/retire_phone_config only.")
		self.body_hash = fresh
