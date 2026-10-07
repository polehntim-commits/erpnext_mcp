# SPDX-License-Identifier: MIT
"""Controller for USDA Price Quote — one market quotation, kept.

WHAT `validate` REFUSES, AND WHY EACH ONE MATTERS TO A NUMBER SOMEBODY QUOTES.

A HIGH BELOW ITS LOW. Almost always a transposition on a hand-entered quote, and
it is the one error that survives every downstream check: a midpoint computed
from a reversed band is still a plausible-looking price, and the overlay would
report a farm comfortably above breakeven on a number that was never real.

A MOSTLY BAND OUTSIDE THE RANGE IT IS INSIDE OF. AMS publishes the mostly band as
a narrower band WITHIN the low-to-high range; one that sticks out is a parse that
matched the wrong columns, and it is worth catching at the row rather than three
reports later.

A NEGATIVE PRICE. A grower can certainly lose money on a load, and that loss is a
fact about their settlement and not about the market's asking price — a negative
shipping point quotation is a parse error every time.

A DATE IN THE FUTURE. A report cannot have been published tomorrow, and a quote
dated forward would win every "most recent" lookup in this app permanently.

NAMING CARRIES THE IDENTITY. Commodity, variety, market, package and report date
together are what make two quotations the same quotation, so they are the
docname. That makes a refetch of yesterday's report an update of the row that is
already there rather than a second copy of it — which matters because the overlay
reads "the most recent quote", and a register with three copies of one day would
answer from whichever was written last.
"""

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, nowdate

SOURCES = ("USDA AMS Market News", "Manual", "Broker Quote")


class USDAPriceQuote(Document):
	def autoname(self):
		# v0.265.0. A market point (fetched through a Market Commodity config) has eight identity parts; its name
		# is readable and ends in the identity hash, which keeps it unique and under 140 characters.
		if self.get("commodity_key") and self.get("row_hash"):
			self.name = f"{self.commodity_key}·{(self.market_type or '')[:1]}·{self.report_date}·{self.size or '-'}·{self.row_hash[:10]}"
			return
		parts = [
			str(self.commodity or "").strip().upper(),
			str(self.variety or "").strip().upper(),
			str(self.market or "").strip().upper(),
			str(self.package or "").strip(),
			str(self.report_date or ""),
		]
		self.name = " · ".join(part for part in parts if part)

	def validate(self):
		self.commodity = str(self.commodity or "").strip().upper()
		if not self.commodity:
			frappe.throw(_("Commodity is required — a price with no commodity belongs to nothing."))
		if not self.report_date:
			frappe.throw(
				_(
					"Report Date is required. It is what says how stale the number is, and a quote with no date wins every 'most recent' lookup forever."
				)
			)
		if self.get("quote_status") not in (None, "", "Priced", "Not Quoted"):
			frappe.throw(_("Quote Status is Priced or Not Quoted."))
		if self.get("quote_status") == "Not Quoted":
			# A listed item with no price: the prices stay EMPTY — never a carried-forward number.
			for field in ("low_price", "high_price", "mostly_low", "mostly_high"):
				self.set(field, None)
		if str(self.source or "") not in SOURCES:
			frappe.throw(_("Source must be one of: {0}.").format(", ".join(SOURCES)))

		if getdate(self.report_date) > getdate(nowdate()):
			frappe.throw(
				_("Report Date {0} is in the future. A report cannot have been published tomorrow.").format(
					self.report_date
				)
			)

		self._refuse_an_impossible_band()
		self._normalise_payload()

	def _refuse_an_impossible_band(self) -> None:
		low = _number(self.low_price)
		high = _number(self.high_price)
		mostly_low = _number(self.mostly_low)
		mostly_high = _number(self.mostly_high)

		for label, value in (
			("Low", low),
			("High", high),
			("Mostly Low", mostly_low),
			("Mostly High", mostly_high),
		):
			if value is not None and value < 0:
				frappe.throw(
					_(
						"{0} is {1}. A shipping point quotation is what a district was ASKING, "
						"which is never negative — a load that lost money is a fact about a "
						"settlement, not about the market."
					).format(label, value)
				)

		if low is not None and high is not None and high < low:
			frappe.throw(
				_(
					"High {0} is below Low {1}. Almost always two figures transposed — and a "
					"reversed band still produces a perfectly plausible midpoint, so nothing "
					"downstream would catch it."
				).format(high, low)
			)
		if mostly_low is not None and mostly_high is not None and mostly_high < mostly_low:
			frappe.throw(_("Mostly High {0} is below Mostly Low {1}.").format(mostly_high, mostly_low))

		if low is not None and mostly_low is not None and mostly_low < low:
			frappe.throw(
				_(
					"Mostly Low {0} is below Low {1}. The mostly band sits INSIDE the range, so "
					"one that sticks out is a parse that matched the wrong columns."
				).format(mostly_low, low)
			)
		if high is not None and mostly_high is not None and mostly_high > high:
			frappe.throw(
				_("Mostly High {0} is above High {1}. The mostly band sits inside the range.").format(
					mostly_high, high
				)
			)

	def _normalise_payload(self) -> None:
		"""Keep `payload` as JSON text, or as nothing. Never as a broken string.

		A Code field is text, and a caller handing it a dict is the normal case
		from `services/usda_prices.py`. Serialising here means the column is
		readable by `json.loads` whoever wrote the row — which is the entire
		point of keeping the source record at all.
		"""
		if self.payload in (None, ""):
			return
		if isinstance(self.payload, (dict, list)):
			self.payload = json.dumps(self.payload, indent=1, sort_keys=True, default=str)
			return
		try:
			json.loads(self.payload)
		except Exception:
			# Not JSON. Wrap rather than discard: a source row this app could not
			# parse is the row most worth keeping, and a reparse tool can still
			# find it under a key that says what it is.
			self.payload = json.dumps({"unparsed": str(self.payload)}, indent=1)


def _number(value):
	if value in (None, ""):
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None
