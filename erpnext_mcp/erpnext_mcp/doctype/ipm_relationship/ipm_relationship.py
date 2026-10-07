# SPDX-License-Identifier: MIT
"""Controller for IPM Relationship — an edge of the IPM graph. v0.262.0. See `erpnext_mcp/ipm_graph.py`.

THE DIRECTION IS PART OF THE MEANING, so the kinds at each end are checked: a crop does not prey on
anything, and "Ladybug harmed_by Warrior II" read backwards would say the insecticide is hurt by the
beetle. Only the combinations that would invert a recommendation are refused.
"""

import frappe
from frappe.model.document import Document

CROPS = {"Crop", "Variety"}
PESTS = {"Insect Pest", "Mite Pest", "Disease", "Weed", "Vertebrate Pest"}
BENEFICIALS = {"Beneficial Insect", "Beneficial Mite", "Beneficial Microbe", "Beneficial Vertebrate", "Pollinator"}

#: relation → (allowed subject kinds, allowed object kinds); None = any.
ENDS = {
	"attacks": (PESTS, CROPS),
	"preys_on": (BENEFICIALS, PESTS),
	"parasitizes": (BENEFICIALS, PESTS),
	"controls": (BENEFICIALS | {"Product"}, PESTS),
	"harmed_by": (BENEFICIALS | CROPS, {"Product"}),
	"pollinates": ({"Pollinator", "Beneficial Insect"}, CROPS),
	"hosts": (None, None),
	"competes_with": (None, None),
}


class IPMRelationship(Document):
	def validate(self):
		if self.subject == self.object:
			frappe.throw("An organism cannot be related to itself.", frappe.ValidationError)
		ends = ENDS.get(self.relation)
		if ends is None:
			frappe.throw(f"Unknown relation {self.relation!r}.", frappe.ValidationError)
		kinds = {
			end: frappe.db.get_value("IPM Organism", self.get(end), "kind") for end in ("subject", "object")
		}
		for end, allowed in zip(("subject", "object"), ends):
			if allowed is not None and kinds[end] not in allowed:
				frappe.throw(
					f"{self.relation}: the {end} must be {', '.join(sorted(allowed))} — {self.get(end)} is "
					f"{kinds[end] or 'not an IPM Organism'}.",
					frappe.ValidationError,
				)
		for key in ("weight", "confidence"):
			value = self.get(key)
			if value not in (None, "") and not 0 <= float(value) <= 1:
				frappe.throw(f"{key} is between 0 and 1.", frappe.ValidationError)
		if self.crop and frappe.db.get_value("IPM Organism", self.crop, "kind") not in CROPS:
			frappe.throw("crop must be a Crop or Variety.", frappe.ValidationError)
