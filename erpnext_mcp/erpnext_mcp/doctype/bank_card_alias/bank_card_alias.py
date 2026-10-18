# SPDX-License-Identifier: MIT
"""Controller for Bank Card Alias — which card a receipt's last four stand for.

v0.331.0. The guards that matter live in `tools/card_aliases.py`, where the
standalone suite can reach them; this controller only normalises the digits so a
row typed in the Desk is stored the way the matcher looks it up.
"""

import re

import frappe
from frappe.model.document import Document


class BankCardAlias(Document):
	def validate(self):
		for fieldname in ("card_last_four", "number"):
			digits = re.sub(r"\D", "", str(self.get(fieldname) or ""))
			if len(digits) != 4:
				frappe.throw(f"{fieldname} must be exactly four digits.")
			self.set(fieldname, digits)
		if self.get("kind") == "Card" and self.number != self.card_last_four:
			frappe.throw("A Card row's number is the card's own last four.")
