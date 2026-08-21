# SPDX-License-Identifier: MIT
"""Controller for Crop Variety Water Requirement — a child table, empty on purpose.

Frappe imports one module per DocType, child tables included, and a folder with
a JSON and no module breaks `bench migrate` rather than degrading. See
`crop_water_requirement.py` for the same note and `test_packaging.py` for the
release that learned it the hard way.

NOTHING HERE IS TRUE OF ONE ROW ALONE, WHICH IS WHY THIS FILE IS EMPTY. Whether
the variety named exists needs the parent's variety list. Whether two rows
collide needs the siblings. Whether the Kc is in range is the same question the
crop-level table already answers, and answering it twice in two places is how
the two answers drift apart. All three live in `crop.py`, where the whole
document is in hand — a reader looking for "what can be wrong with a water row"
finds the crop's and the variety's rules next to each other.
"""

from frappe.model.document import Document


class CropVarietyWaterRequirement(Document):
	pass
