# SPDX-License-Identifier: MIT
"""Controller for Extraction Config. v0.206.0.

The docname and `config_version` are both "<document_type>@<version>", so a
Document Validation's `config_version` is also a link a person can follow. The
body is checked by `extraction_config.problems` on every save: a config the
phone would refuse never reaches it. Status changes belong to
`extraction_config.publish`, which keeps exactly one Published row per type.
"""

import json

import frappe
from frappe.model.document import Document

from erpnext_mcp import extraction_config


class ExtractionConfig(Document):
	def autoname(self):
		self.name = extraction_config.version_string(self.document_type, self.version)

	def validate(self):
		self.config_version = extraction_config.version_string(self.document_type, self.version)
		try:
			body = json.loads(self.config_json or "")
		except ValueError as exc:
			frappe.throw(f"config_json is not JSON: {exc}")
		problems = extraction_config.problems(body)
		if problems:
			frappe.throw("This extraction config would be refused by the phone: " + "; ".join(problems))
