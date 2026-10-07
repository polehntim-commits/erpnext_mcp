# SPDX-License-Identifier: MIT
"""Controller for IPM Organism — a node of the IPM graph. v0.262.0. See `erpnext_mcp/ipm_graph.py`."""

import re

import frappe
from frappe.model.document import Document

CROP_KINDS = ("Crop", "Variety")


def slug(text: str) -> str:
	"""The node id: lower-case, words joined by hyphens. 'Spotted Wing Drosophila' → 'spotted-wing-drosophila'."""
	return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:120]


class IPMOrganism(Document):
	def autoname(self):
		self.organism_key = self.organism_key or slug(self.organism_name)
		if not self.organism_key:
			frappe.throw("An IPM Organism needs a name.", frappe.ValidationError)
		self.name = self.organism_key

	def validate(self):
		for low, high in (("bbch_from", "bbch_to"), ("dd_from", "dd_to")):
			a, b = self.get(low), self.get(high)
			if a not in (None, "") and b not in (None, "") and float(a) > float(b):
				frappe.throw(f"{low} ({a}) is after {high} ({b}).", frappe.ValidationError)
		if self.kind == "Variety":
			parent_kind = frappe.db.get_value("IPM Organism", self.parent_organism, "kind") if self.parent_organism else None
			if parent_kind != "Crop":
				frappe.throw("A Variety's parent must be a Crop.", frappe.ValidationError)
		if self.confidence not in (None, "") and not 0 <= float(self.confidence) <= 1:
			frappe.throw("confidence is between 0 and 1.", frappe.ValidationError)
