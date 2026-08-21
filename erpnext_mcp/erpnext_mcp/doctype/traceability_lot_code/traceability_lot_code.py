# SPDX-License-Identifier: MIT
"""Controller for Traceability Lot Code — the one identifier FSMA 204 asks for.

WHAT IT DERIVES rather than takes. The company, off the Field's `owning_entity`
where the caller left it blank — and `owning_entity` rather than `company`, which
is the trap this app has already fallen into once: the four location registers
name that column differently from everything else, and a check that reads
`company` off a Field passes everything because the column is simply absent. Each
source row's `source_field`, snapshotted, so a pallet read two years later names
the block this app believed fed it at the moment somebody combined it.

WHAT IT REFUSES, AND IT IS A SHORT LIST BECAUSE A LOT CODE IS A NAME.

  * A lot that is its own source. `source_lots` is a directed edge and a self
    loop makes `trace_lot_backward` walk forever; the visited set in the tool
    layer stops it, and a graph that cannot contain the cycle in the first place
    is better than one that survives it.
  * The same source lot twice. Two rows for one input is that input's share of
    the pallet counted twice, and it is invisible on the form where the two rows
    sit next to each other looking deliberate — the identical argument Bin Seal
    makes about a duplicate contributor.
  * A negative quantity, on the lot or on any source row. A traceability figure
    is a settlement figure before long, and a negative one propagates.

WHAT IT MERELY RECORDS.

A lot with no `field` and no `source_lots`. That is fruit whose origin was never
written down, and it is a real thing that happens on the busiest afternoon of the
season. The reads name it — `trace_lot_backward` reports it as a break rather
than returning an empty answer that looks like a clean bill.

A lot whose `quantity` disagrees with the quantities on its events. Two different
measurements taken by two different people; see the field description. Balancing
them would delete the disagreement, which is the fact somebody needs.

A lot code that does not look like the generated shape. `create_traceability_lot`
generates one, and an operation importing three seasons of history from a
spreadsheet has codes of its own that are already printed on bins. Refusing those
would refuse the records; the uniqueness constraint is the invariant that matters
and it holds either way.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class TraceabilityLotCode(Document):
	def validate(self):
		self._normalise_the_code()
		self._check_the_quantity()
		self._fill_from_the_field()
		self._check_the_sources()

	# ── the parts ───────────────────────────────────────────────────────────
	def _normalise_the_code(self) -> None:
		"""Trimmed and upper-cased, because a lot code is read off a bin.

		'yc3-bing-20260821-01' and 'YC3-BING-20260821-01' are the same lot to
		everybody except a unique index, and the version somebody types down a
		telephone is whichever one their keyboard was in.
		"""
		code = str(self.lot_code or "").strip().upper()
		if not code:
			frappe.throw(
				_(
					"A lot needs a code. It is the only identifier that travels with the fruit, "
					"and a lot without one cannot be joined to a spray record, a bin or a "
					"shipment — which is the entire point of the record."
				),
				title=_("No Lot Code"),
			)
		self.lot_code = code

	def _check_the_quantity(self) -> None:
		if self.quantity not in (None, "") and frappe.utils.flt(self.quantity) < 0:
			frappe.throw(
				_(
					"A lot cannot hold {0}. A traceability figure becomes a settlement figure "
					"before long, and a negative one propagates."
				).format(self.quantity),
				title=_("Negative Quantity"),
			)

	def _fill_from_the_field(self) -> None:
		"""Company off the block, where the caller left it blank.

		`owning_entity` AND NOT `company`. Field, Parcel, Irrigation Zone and
		Housing Unit name their entity column that way; a lookup for `company`
		here returns nothing on every site and the lot silently arrives unscoped.
		"""
		if self.company or not self.field:
			return
		row = frappe.db.get_value("Field", self.field, ["owning_entity"], as_dict=True) or {}
		self.company = row.get("owning_entity") or None

	def _check_the_sources(self) -> None:
		seen: dict = {}
		for row in self.get("source_lots") or []:
			if not row.source_lot:
				continue
			if row.source_lot == self.name or row.source_lot == self.lot_code:
				frappe.throw(
					_(
						"{0} cannot be made from itself. `source_lots` is a directed edge and a "
						"self loop is a trace that never terminates."
					).format(self.lot_code),
					title=_("A Lot Made From Itself"),
				)
			if row.source_lot in seen:
				frappe.throw(
					_(
						"{0} is on this lot's sources twice. Two rows for one input is that "
						"input's share counted twice, and it is invisible on the form where the "
						"two rows sit next to each other looking deliberate. A lot that went in "
						"more than once is ONE row with the quantities added up."
					).format(row.source_lot),
					title=_("A Source Twice"),
				)
			seen[row.source_lot] = True
			if row.quantity_contributed not in (None, "") and frappe.utils.flt(row.quantity_contributed) < 0:
				frappe.throw(
					_("{0} cannot have contributed a negative quantity.").format(row.source_lot),
					title=_("Negative Contribution"),
				)
			if not str(row.source_field or "").strip():
				row.source_field = (
					frappe.db.get_value("Traceability Lot Code", row.source_lot, "field") or None
				)
