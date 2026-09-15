# SPDX-License-Identifier: MIT
"""Controller for a Lot Line Adjustment child table. Empty on purpose.

Frappe imports a controller for every DocType it syncs, child tables included, so
the module must exist. The rules for a row live in `land_adjustment`, which the
parent's controller and the tools both call.
"""

from frappe.model.document import Document


class LotLineAdjustmentEasement(Document):
	pass
