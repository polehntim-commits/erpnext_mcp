# SPDX-License-Identifier: MIT
"""Controller for Crop Variety Protocol — a child table, empty on purpose.

Frappe imports one module per DocType, child tables included, and a folder with
a JSON and no module breaks `bench migrate` rather than degrading. See
`crop_variety.py` for the same note.

WHAT IS NOT CHECKED HERE, AND WHY IT IS NOT CHECKED ANYWHERE. Whether the
variety named exists needs the parent's variety list, so that check is in
`crop.py` with the rest. But there is deliberately NO rule that two rows may not
share a variety and a practice: a GA program is two or three applications at
different timings, and a uniqueness rule on (variety, practice) would refuse the
commonest real recipe in the file. What `crop.py` refuses instead is the exact
repeat — same variety, same practice, same stage, same product — which is
double entry rather than a schedule.

There is nothing true of one of these rows on its own.
"""

from frappe.model.document import Document


class CropVarietyProtocol(Document):
	pass
