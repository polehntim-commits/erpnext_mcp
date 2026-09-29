# SPDX-License-Identifier: MIT
"""Controller for Farm Feature Flag. v0.206.0.

Normalises the key and the version bounds so resolution (`flags.value`) never
has to guess. docs/design/config_flags_triage.md §2.
"""

import frappe
from frappe.model.document import Document

from erpnext_mcp import flags


class FarmFeatureFlag(Document):
	def validate(self):
		self.flag_key = str(self.flag_key or "").strip()
		if not flags.KEY_PATTERN.match(self.flag_key):
			frappe.throw(f"{self.flag_key!r} is not a lower_snake_case flag key, e.g. label_capture_v2.")
		for fieldname in ("min_app_version", "max_app_version"):
			value = str(self.get(fieldname) or "").strip()
			if value and flags.parse_version(value) is None:
				frappe.throw(f"{fieldname} {value!r} is not a version like 1.42 or 1.42.3.")
			self.set(fieldname, value)
		self.roles = "\n".join(flags.role_list(self.roles))
		self.users = "\n".join(flags.role_list(self.get("users")))
