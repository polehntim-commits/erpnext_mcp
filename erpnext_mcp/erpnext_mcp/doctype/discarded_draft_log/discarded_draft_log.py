# SPDX-License-Identifier: MIT
"""Controller for Discarded Draft Log — the record of a draft purged for good. v0.334.0.

Written by `tools/discard.purge` before each delete; see that module. Read-only but
for `allow_recreate`, which is how an operator lets Bank Bridge draft a purged
entry again.
"""

from frappe.model.document import Document


class DiscardedDraftLog(Document):
	pass
