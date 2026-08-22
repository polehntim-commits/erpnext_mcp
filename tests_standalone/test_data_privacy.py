# SPDX-License-Identifier: MIT
"""`data_privacy` — keeping worker PII out of anything that leaves the farm.

Cycle 2 of the Farm App retirement. The failure this prevents is not a role gate
failing; it is a correct report, generated with correct permissions, being sent
to a customer with a crew's names in it. Four claims.

1. **A LIST OF WORKER RECORDS IS DROPPED, NOT EMPTIED.** `WhatComesOut`. A list
   of three empty dicts still discloses that three people worked the block,
   which on a farm this size is the disclosure. The aggregates that leak the
   same thing — `total_piece_units`, a crew count — go with them.

2. **WHAT MUST STAY, STAYS.** `WhatSurvives`. Hex production, quantities,
   grades, temperatures, lot ids. A stripper that took the quantity out because
   it sat next to a name would produce a document that protects the crew by
   being useless, and the farm would go back to sending the spreadsheet.

3. **THE STRIPPER IS CHECKABLE.** `TheAudit`. `audit()` names every path it
   would remove, so a test can assert a real export is clean and a reviewer can
   see what a change to the key list actually catches.

4. **A PSEUDONYM IS ONE PERSON, ONCE.** `Pseudonyms`. Every identity field of
   one record resolves to one label, the same person keeps the label across the
   document, and it does not survive to the next one.
"""

import unittest

from erpnext_mcp import data_privacy

#: A traceability payload in the shape one actually leaves the farm in: block
#: production that must survive, and crew records that must not.
EXPORT = {
	"lot": "L-2026-114",
	"quantity": 420,
	"unit": "bins",
	"quality_grade": "Extra Fancy",
	"temperature_f": 34.1,
	"bbch_code": "87",
	"hex_production": {"8928308280fffff": 120, "8928308281fffff": 300},
	"total_piece_units": 300,
	"buckets": [
		{"employee": "HR-0001", "employee_name": "A Picker", "units": 40, "hex": "8928308280fffff"},
		{"employee": "HR-0002", "units": 30, "hex": "8928308281fffff"},
		{"employee": "HR-0001", "units": 50, "hex": "8928308280fffff"},
	],
	"shipment": {"pallet": "P-9", "owner": "office@farm.example", "ship_date": "2026-08-20"},
}


class WhatComesOut(unittest.TestCase):
	def setUp(self):
		self.clean = data_privacy.strip(EXPORT)

	def test_the_crew_list_is_dropped_whole_rather_than_emptied(self):
		"""A list of three empty dicts still says three people worked here."""
		self.assertEqual(self.clean["buckets"], [])

	def test_the_aggregate_that_names_a_crew_size_goes_too(self):
		self.assertNotIn("total_piece_units", self.clean)

	def test_frappes_own_audit_columns_go(self):
		"""An outward record that says who touched it is an outward record
		naming staff."""
		self.assertNotIn("owner", self.clean["shipment"])

	def test_the_original_is_not_modified(self):
		self.assertEqual(len(EXPORT["buckets"]), 3)
		self.assertIn("total_piece_units", EXPORT)

	def test_a_key_nobody_listed_still_goes_if_its_name_says_worker(self):
		payload = {"crew_member_names": ["A", "B"], "spray_applicator": "HR-3", "block": "A4"}
		self.assertEqual(data_privacy.strip(payload), {"block": "A4"})

	def test_the_caller_can_name_more_keys(self):
		self.assertNotIn("lot", data_privacy.strip(EXPORT, extra_keys=["lot"]))

	def test_a_cycle_terminates_rather_than_recursing_forever(self):
		payload = {"block": "A4"}
		payload["self"] = payload
		self.assertEqual(data_privacy.strip(payload)["block"], "A4")

	def test_a_tuple_stays_a_tuple(self):
		"""A caller that put a tuple in gets one back — a silently changed type
		is a serialiser failing three functions later."""
		self.assertIsInstance(data_privacy.strip({"rows": (1, 2)})["rows"], tuple)


class WhatSurvives(unittest.TestCase):
	def setUp(self):
		self.clean = data_privacy.strip(EXPORT)

	def test_the_substance_of_the_traceability_record_is_intact(self):
		for key in ("lot", "quantity", "unit", "quality_grade", "temperature_f", "bbch_code"):
			self.assertIn(key, self.clean, key)
		self.assertEqual(self.clean["quantity"], 420)

	def test_hex_production_survives_because_it_is_geographic_and_not_personal(self):
		self.assertEqual(self.clean["hex_production"], EXPORT["hex_production"])

	def test_the_shipment_keeps_everything_that_is_not_a_person(self):
		self.assertEqual(self.clean["shipment"], {"pallet": "P-9", "ship_date": "2026-08-20"})

	def test_a_key_that_looks_personal_and_is_not_is_kept(self):
		"""`employee_count` on a Market Participant is a competitor's published
		headcount. Losing it silently produces a report that is wrong rather
		than one that is redacted."""
		self.assertEqual(data_privacy.strip({"employee_count": 300}), {"employee_count": 300})

	def test_a_row_that_merely_carries_an_owner_is_not_a_worker_record(self):
		"""Only a named worker field makes a list item a person. A shipment line
		with an `owner` on it is a shipment line."""
		payload = {"lines": [{"owner": "office@farm.example", "pallet": "P-1", "cases": 40}]}
		self.assertEqual(data_privacy.strip(payload), {"lines": [{"pallet": "P-1", "cases": 40}]})

	def test_safe_columns_keeps_the_order_a_csv_writer_needs(self):
		columns = ["lot", "employee_name", "quantity", "employee_count", "employee"]
		self.assertEqual(data_privacy.safe_columns(columns), ["lot", "quantity", "employee_count"])

	def test_a_csv_row_loses_the_column_rather_than_being_blanked(self):
		"""A header naming `employee_name` in a file that is supposed to have no
		worker data in it is a question the farm should not have to answer."""
		row = data_privacy.redact_row({"lot": "L-1", "employee_name": "A Picker", "units": 40})
		self.assertEqual(row, {"lot": "L-1", "units": 40})


class TheAudit(unittest.TestCase):
	def test_every_path_it_would_remove_is_named(self):
		paths = {finding["path"] for finding in data_privacy.audit(EXPORT)}
		self.assertIn("total_piece_units", paths)
		self.assertIn("buckets[0].employee_name", paths)
		self.assertIn("shipment.owner", paths)

	def test_the_reason_distinguishes_the_three_kinds(self):
		reasons = {finding["path"]: finding["reason"] for finding in data_privacy.audit(EXPORT)}
		self.assertIn("aggregate", reasons["total_piece_units"])
		self.assertIn("direct worker identifier", reasons["buckets[0].employee"])
		self.assertIn("Frappe audit column", reasons["shipment.owner"])

	def test_a_stripped_payload_audits_clean(self):
		"""The round trip that makes the whole module checkable."""
		self.assertFalse(data_privacy.is_clean(EXPORT))
		self.assertTrue(data_privacy.is_clean(data_privacy.strip(EXPORT)))

	def test_a_payload_that_never_had_any_audits_clean(self):
		self.assertTrue(data_privacy.is_clean({"lot": "L-1", "quantity": 4}))


class Pseudonyms(unittest.TestCase):
	def setUp(self):
		self.labelled = data_privacy.strip(EXPORT, pseudonyms=True)

	def test_the_crew_survives_with_its_production_and_without_its_identity(self):
		"""Four pickers worked this block is legitimate context for a food-safety
		investigation and carries no identity."""
		self.assertEqual(len(self.labelled["buckets"]), 3)
		self.assertEqual([row["units"] for row in self.labelled["buckets"]], [40, 30, 50])

	def test_no_real_identifier_survives(self):
		text = repr(self.labelled)
		self.assertNotIn("HR-0001", text)
		self.assertNotIn("A Picker", text)
		self.assertNotIn("office@farm.example", text)

	def test_one_record_is_one_person_across_all_its_identity_fields(self):
		"""Keying the label on each value would print a row whose `employee` is
		Worker 1 and whose `employee_name` is Worker 2, and a reader counting
		labels would count the crew twice."""
		first = self.labelled["buckets"][0]
		self.assertEqual(first["employee"], first["employee_name"])

	def test_the_same_person_keeps_one_label_across_the_document(self):
		rows = self.labelled["buckets"]
		self.assertEqual(rows[0]["employee"], rows[2]["employee"])
		self.assertNotEqual(rows[0]["employee"], rows[1]["employee"])

	def test_an_office_account_is_staff_and_not_a_worker(self):
		"""The account that last saved a row is usually an office user, and
		calling them a worker puts a person in the crew who never picked."""
		self.assertTrue(self.labelled["shipment"]["owner"].startswith("Staff"))

	def test_the_labels_do_not_survive_to_the_next_document(self):
		"""A label stable across a season's exports is an identifier again, just
		one the reader has to work slightly harder to resolve."""
		one = data_privacy.strip({"rows": [{"employee": "HR-0002", "units": 1}]}, pseudonyms=True)
		two = data_privacy.strip(
			{"rows": [{"employee": "HR-0001", "units": 1}, {"employee": "HR-0002", "units": 1}]},
			pseudonyms=True,
		)
		self.assertEqual(one["rows"][0]["employee"], "Worker 1")
		self.assertEqual(two["rows"][1]["employee"], "Worker 2")

	def test_the_aggregate_that_names_a_crew_size_still_goes(self):
		"""Pseudonyms are for the per-worker breakdown; they are not a licence
		to keep the count."""
		self.assertNotIn("total_piece_units", self.labelled)


if __name__ == "__main__":  # pragma: no cover
	unittest.main()
