# SPDX-License-Identifier: MIT
"""Controller for Field Variety — a child table, empty on purpose.

Frappe imports one module per DocType, child tables included, and a folder with
a JSON and no module breaks `bench migrate` rather than degrading. That is why
this file exists at all; see `crop_variety.py` for the same note.

A LINK TO THE CROP'S CATALOGUE, NOT A SECOND RECORD OF IT. One row says one of
the block's crop's own `Crop Variety` cultivars is planted here, what share of
the block, and when — which is what lets the Pearl blocks record Black Pearl,
Burgundy Pearl and Ebony Pearl in one field rather than forcing one of the three
onto `Field.variety` and losing the other two. Grafting, pollination group,
yield and Brix all stay on `Crop Variety`; the checking that `variety` here
actually names one of them lives on `Field`, in `_check_varieties` — the same
split `Crop Variety Water Requirement` and `Crop Variety Protocol` use for their
own reference back to `Crop.varieties`.
"""

from frappe.model.document import Document


class FieldVariety(Document):
	pass
