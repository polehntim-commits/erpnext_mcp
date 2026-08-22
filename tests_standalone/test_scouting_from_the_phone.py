# SPDX-License-Identifier: MIT
"""The scouting round as the handset closes it. v0.117.0, `SERVER_CHANGES.md` §26.

v0.115.0 turned a completed scouting task into a Crop Observation and v0.116.0
drew the harvest overlay off it. Both read the measurements out of the task's
`creates_record_data`, which `complete_farm_task` stamps as the template's
defaults with the completion's own `record_data` merged over the top — AND A
HANDSET CANNOT SEND `record_data`, on purpose and for a good reason. So a round
closed from the app filed an observation carrying `observation_type: General`,
`scouting_method: Visual` and **null Brix, null growth stage**, on a round where
somebody stood in the block and read both. The map then drew that block grey.

FOUR CLAIMS, in the order of how much damage getting each one wrong would do.

1. **THE NUMBERS REACH THE COLUMNS THE MAP READS.**
   `ARoundWalkedFromThePhoneReachesTheRegister`, and its negative control — the
   identical round closed WITHOUT the four arguments, asserted to produce the
   null-Brix row this release exists to stop. A test that only checked the new
   path would pass just as well against a build that had never had the bug.

2. **NOTHING ELSE BECAME WRITABLE.** `TheHandsetStillCannotComposeARecord`.
   `record_data` and `worker_id` stay off the signature, so the reasoning in
   `complete_task_via_mobile`'s docstring is intact: what arrives is four named
   measurement columns, not an open dictionary, and the pest half — the threat,
   the category and the count that move a block's pest pressure — is still not
   reachable from a phone.

3. **A BAD READING IS REFUSED WHILE THE SCOUT IS STILL IN THE BLOCK.**
   `TheReadingsAreRefusedAtTheDoor`. The observation is written days later by an
   idempotent sweep, so a payload its controller refuses lands in that sweep's
   `refused` list — correct, and read by nobody, a week after the phone that
   could have fixed it left the orchard.

4. **THE `gps` CONTRACT KEY IS ACCEPTED BY THE CREATE.**
   `TheGpsContractSurvivesTheRoundTrip`, `SERVER_CHANGES.md` §27. The completion
   has enforced it since v0.115.0; this is the other end — a scouting task
   RAISED FROM A PHONE can demand the coordinate its own observation needs, and
   the completion of that phone-raised task refuses without one.
"""

import inspect
import json

import frappe

from erpnext_mcp import task_templates
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.erpnext_mcp.doctype.crop_observation import crop_observation as observation_rules

from .fixtures import MAIN
from .harness import STORE
from .test_wave2_mobile_surface import MANAGER, WORKER, Wave2TestCase
from .test_wave2_mobile_surface import ON as WAVE2_ON

OBSERVATION = "Crop Observation"
FARM_TASK = "Farm Task"

#: The contract the shipped Field Scouting template carries, `gps` included.
SCOUT_CONTRACT = {"photos": True, "findings_text": True, "gps": True}

#: What the seeded template stamps on every round it raises. The two nulls this
#: release fills are what is NOT in here.
TEMPLATE_DEFAULTS = {"observation_type": "General", "scouting_method": "Visual"}

A_FIX = "45.5152,-121.1787"
A_PHOTO = [{"file_url": "/files/bing-canopy.jpg", "kind": "photo", "file_name": "bing-canopy.jpg"}]

ON = {
	**WAVE2_ON,
	**{
		f"allow_{name}": 1
		for name in (
			"claim_farm_task",
			"get_farm_task",
			"create_task_from_template",
			"list_farm_task_templates",
			"index_scouting_observations",
			"list_crop_observations",
		)
	},
}


class PhoneScoutingTestCase(Wave2TestCase):
	"""A block, a scouting task that produces a Crop Observation, and a scout."""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		self.block = self.a_block(field_name="Yellow Camp Block 3")
		self.task = self.a_scouting_task()

	def a_scouting_task(self, **overrides):
		payload = {
			"task_name": f"Scout — {self.block}",
			"task_type": "Scouting",
			"evidence_required": dict(SCOUT_CONTRACT),
			"location_doctype": "Field",
			"location": self.block,
			"creates_record": OBSERVATION,
			"creates_record_data": dict(TEMPLATE_DEFAULTS),
			"company": MAIN,
		}
		payload.update(overrides)
		return self.tool_data("create_farm_task", payload)["name"]

	def walk(self, task=None, **readings):
		"""One round: claimed, started and closed over the mobile surface alone."""
		task = task or self.task
		self.be(WORKER)
		mobile_api.claim_task(task=task)
		mobile_api.start_task(task=task)
		return mobile_api.complete_task_via_mobile(
			task=task,
			findings_text="east row a week behind the rest",
			farm_location_gps=A_FIX,
			evidence_files=list(A_PHOTO),
			**readings,
		)

	def sweep(self, **overrides):
		payload = {"date_from": frappe.utils.today(), "date_to": frappe.utils.today()}
		payload.update(overrides)
		self.be("Administrator")
		return self.tool_data("index_scouting_observations", payload)

	def one_observation(self):
		written = self.sweep()["observations_written"]
		self.assertEqual(len(written), 1, "expected exactly one observation from one round")
		return frappe.db.get_value(
			OBSERVATION,
			written[0]["observation"],
			[
				"observation_type",
				"brix_reading",
				"brix_method",
				"growth_stage_code",
				"observed_gps",
				"block",
			],
			as_dict=True,
		)

	def stamped(self, task=None):
		"""What the completion left on the task for the sweep to read."""
		raw = frappe.db.get_value(FARM_TASK, task or self.task, "creates_record_data")
		return json.loads(raw) if raw else {}

	def a_template_round(self):
		"""One round raised from the `Field Scouting` template `bench migrate` seeds."""
		self.be("Administrator")
		task_templates.seed_farm_task_templates()
		return self.tool_data(
			"create_task_from_template",
			{
				"template": "Field Scouting",
				"location_doctype": "Field",
				"location": self.block,
				"company": MAIN,
			},
		)["name"]

	def refusal(self, **readings):
		"""One completion that must be refused. Returns the message."""
		with self.assertRaises(frappe.ValidationError) as caught:
			self.walk(**readings)
		return str(caught.exception)


# ── 1. the numbers reach the columns the map reads ──────────────────────────
class ARoundWalkedFromThePhoneReachesTheRegister(PhoneScoutingTestCase):
	def test_the_four_readings_land_in_the_observations_own_columns(self):
		self.walk(
			observation_type="Harvest Readiness",
			growth_stage_code="87",
			brix_reading=18.5,
			brix_method="Refractometer",
		)
		row = self.one_observation()
		self.assertEqual(row["observation_type"], "Harvest Readiness")
		self.assertEqual(float(row["brix_reading"]), 18.5)
		self.assertEqual(row["brix_method"], "Refractometer")
		self.assertEqual(row["growth_stage_code"], "87")
		self.assertEqual(row["observed_gps"], A_FIX)
		self.assertEqual(row["block"], self.block)

	def test_the_same_round_without_them_is_the_grey_block_this_release_closes(self):
		"""THE NEGATIVE CONTROL. Exactly the round above, minus the arguments.

		This is what every scouting round closed from a phone filed before
		v0.117.0: the template's own defaults, and two nulls where the readings
		the scout actually took should be. `overlays.harvest_overlay` draws that
		block grey with `short_of` reporting that nobody took a reading.
		"""
		self.walk()
		row = self.one_observation()
		self.assertEqual(row["observation_type"], "General")
		self.assertIn(row["brix_reading"], (None, 0, 0.0))
		self.assertIn(row["growth_stage_code"], (None, ""))

	def test_the_readings_are_stamped_on_the_task_where_the_sweep_reads_them(self):
		"""The sweep runs days later and reads `creates_record_data`, so the
		completion has to leave them there rather than anywhere of its own."""
		self.walk(growth_stage_code="81", brix_reading=14.5, brix_method="Estimate")
		stamped = self.stamped()
		self.assertEqual(stamped["growth_stage_code"], "81")
		self.assertEqual(stamped["brix_reading"], 14.5)
		self.assertEqual(stamped["brix_method"], "Estimate")

	def test_the_templates_defaults_survive_what_the_phone_did_not_send(self):
		"""A merge, not a replacement: `scouting_method` is the template's answer
		and the phone has no argument for it."""
		self.walk(growth_stage_code="87")
		self.assertEqual(self.stamped()["scouting_method"], "Visual")
		self.assertEqual(self.stamped()["observation_type"], "General")

	def test_a_number_sent_as_a_string_is_still_a_number(self):
		"""An HTTP body carries `"18.5"`, and a Float column that stored the
		string would compare wrong in every threshold and every overlay."""
		self.walk(brix_reading="18.5", brix_method="refractometer")
		self.assertEqual(self.stamped()["brix_reading"], 18.5)
		self.assertEqual(self.stamped()["brix_method"], "Refractometer")

	def test_the_shipped_templates_contract_and_defaults_arrive_on_the_task(self):
		"""The path a foreman actually takes: the template `bench migrate` seeds."""
		task = self.a_template_round()
		row = frappe.db.get_value(
			FARM_TASK, task, ["creates_record", "evidence_required", "creates_record_data"], as_dict=True
		)
		self.assertEqual(row["creates_record"], OBSERVATION)
		self.assertTrue(json.loads(row["evidence_required"])["gps"])
		self.assertEqual(json.loads(row["creates_record_data"]), TEMPLATE_DEFAULTS)

	def test_the_shipped_templates_round_still_cannot_be_CLOSED_from_a_phone(self):
		"""AND THAT IS A SECOND GAP, NOT THIS ONE. `Field Scouting` snapshots
		four REQUIRED checklist items onto every round it raises, and
		`complete_farm_task` refuses a completion that leaves them unticked. The
		mobile surface has no `checklist` argument and no route that ticks an
		item, so the round a foreman raises from the phone — `create_task_from_
		template` IS published — is one no phone can close.

		Asserted rather than left to be discovered: this test fails the day the
		checklist reaches the handset, which is the day somebody should also
		delete it. The four measurement arguments are what §26 asked for and
		they work on every other door onto a scouting round; they cannot make
		the checklist reachable, and pretending otherwise by testing this path
		with a checklist-free template would have hidden the wall entirely.
		"""
		task = self.a_template_round()
		with self.assertRaises(frappe.ValidationError) as caught:
			self.walk(task=task, growth_stage_code="87", brix_reading=18.0, brix_method="Refractometer")
		self.assertIn("checklist item(s) are not marked done", str(caught.exception))
		self.assertNotIn("checklist", self.accepts(mobile_api.complete_task_via_mobile))


# ── 2. nothing else became writable ─────────────────────────────────────────
class TheHandsetStillCannotComposeARecord(PhoneScoutingTestCase):
	def test_record_data_and_worker_id_are_still_not_in_the_signature(self):
		"""The refusal the four arguments exist to avoid reopening. Frappe drops
		a body key a whitelisted method does not declare, so an argument absent
		from the signature is one no phone can send."""
		accepted = set(inspect.signature(mobile_api.complete_task_via_mobile).parameters)
		for forbidden in ("record_data", "worker_id", "signature_file"):
			self.assertNotIn(forbidden, accepted)

	def test_the_four_are_declared_so_the_route_delivers_them(self):
		"""`routes.bind` keeps only the keys a signature names."""
		accepted = self.accepts(mobile_api.complete_task_via_mobile)
		for argument in mobile_api.MEASUREMENT_ARGUMENTS:
			self.assertIn(argument, accepted)

	def test_the_pest_half_is_not_reachable_from_a_phone(self):
		"""The threat, its category and the count carry the threshold engine
		behind them, and whether a handset should move a block's pest pressure
		is a decision to make on its own."""
		accepted = set(inspect.signature(mobile_api.complete_task_via_mobile).parameters)
		for forbidden in ("threat", "threat_category", "count_observed", "sample_size"):
			self.assertNotIn(forbidden, accepted)

	def test_only_the_four_named_columns_reach_the_stamped_payload(self):
		self.walk(
			observation_type="Growth Stage",
			growth_stage_code="71",
			brix_reading=9.0,
			brix_method="Estimate",
		)
		added = set(self.stamped()) - set(TEMPLATE_DEFAULTS)
		self.assertEqual(added, set(mobile_api.MEASUREMENT_ARGUMENTS) - set(TEMPLATE_DEFAULTS))

	def test_a_round_that_sends_nothing_leaves_the_task_untouched(self):
		self.walk()
		self.assertEqual(self.stamped(), TEMPLATE_DEFAULTS)


# ── 3. a bad reading is refused while the scout is still in the block ───────
class TheReadingsAreRefusedAtTheDoor(PhoneScoutingTestCase):
	def test_an_observation_type_the_register_does_not_know_is_refused(self):
		message = self.refusal(observation_type="Maturity Walk")
		self.assertIn("Maturity Walk", message)
		for known in observation_rules.OBSERVATION_TYPES:
			self.assertIn(known, message)

	def test_a_pest_scout_is_refused_from_this_door_and_says_why(self):
		"""The one type whose record is invalid without a threat and a count,
		neither of which this transport can send. Accepting the word would stamp
		a round the sweep is then obliged to refuse."""
		message = self.refusal(observation_type="Pest Scout")
		self.assertIn("cannot be sent from a handset", message)
		self.assertIn("Harvest Readiness", message)

	def test_a_reading_with_no_method_is_refused(self):
		message = self.refusal(brix_reading=18.5)
		self.assertIn("brix_method", message)
		self.assertIn("Refractometer", message)

	def test_a_method_with_no_reading_is_refused(self):
		message = self.refusal(brix_method="Refractometer")
		self.assertIn("no Brix reading", message)

	def test_a_method_the_register_does_not_know_is_refused(self):
		self.assertIn("Guess", self.refusal(brix_reading=18.5, brix_method="Guess"))

	def test_a_misplaced_decimal_point_is_refused(self):
		message = self.refusal(brix_reading=185.0, brix_method="Refractometer")
		self.assertIn("ceiling", message)

	def test_a_reading_that_is_not_a_number_is_refused(self):
		self.assertIn("not a number", self.refusal(brix_reading="sweet", brix_method="Estimate"))

	def test_a_negative_reading_is_refused(self):
		self.assertIn("negative", self.refusal(brix_reading=-1, brix_method="Estimate"))

	def test_a_refusal_files_nothing_and_the_corrected_round_still_closes(self):
		"""The point of refusing at the door rather than at the sweep: the scout
		reads the message, fixes the decimal point and sends the completion
		again — with its photographs, its findings and its fix intact."""
		self.refusal(brix_reading=185.0, brix_method="Refractometer")
		self.assertEqual(self.stamped(), TEMPLATE_DEFAULTS)
		self.assertFalse(STORE.rows(OBSERVATION))

		done = self.walk(brix_reading=18.5, brix_method="Refractometer")
		self.assertEqual(frappe.db.get_value(FARM_TASK, self.task, "state"), "Completed")
		self.assertEqual(done["evidence_filed"], 1)
		self.assertEqual(self.stamped()["brix_reading"], 18.5)

	def test_a_task_that_produces_no_observation_has_nowhere_to_put_them(self):
		"""Sent against an inspection, the four readings would be stamped into a
		payload a different builder reads. Refused by name instead."""
		task = self.a_scouting_task(
			task_name="Habitability walk",
			task_type="Inspection",
			creates_record="Housing Inspection",
			creates_record_data={},
			evidence_required={"photos": True, "findings_text": True},
		)
		with self.assertRaises(frappe.ValidationError) as caught:
			self.walk(task=task, brix_reading=18.5, brix_method="Refractometer")
		self.assertIn("Housing Inspection", str(caught.exception))
		self.assertIn("brix_reading", str(caught.exception))

	def test_a_method_the_template_already_carries_answers_for_a_bare_reading(self):
		"""The pairing check reads the TASK's defaults, not the submission alone.
		A template that has already said how the reading is taken must not make
		the phone repeat it to be believed."""
		task = self.a_scouting_task(
			task_name="Scout — with a standing method",
			creates_record_data={**TEMPLATE_DEFAULTS, "brix_method": "Refractometer"},
		)
		self.walk(task=task, brix_reading=18.5)
		self.assertEqual(self.stamped(task)["brix_reading"], 18.5)
		self.assertEqual(self.stamped(task)["brix_method"], "Refractometer")

	def test_a_standing_default_is_never_something_a_completion_has_to_justify(self):
		"""The same task, and a completion that touched neither half of the pair.
		The template's own `brix_method` with no reading is the template's
		business — refusing here would close a round over a field the phone did
		not send."""
		task = self.a_scouting_task(
			task_name="Scout — method only",
			creates_record_data={**TEMPLATE_DEFAULTS, "brix_method": "Refractometer"},
		)
		self.walk(task=task, growth_stage_code="87")
		self.assertEqual(frappe.db.get_value(FARM_TASK, task, "state"), "Completed")


# ── 4. the gps contract key, at the end the create owns ─────────────────────
class TheGpsContractSurvivesTheRoundTrip(PhoneScoutingTestCase):
	"""`SERVER_CHANGES.md` §27. The completion has enforced `gps` since
	v0.115.0 and the create's vocabulary accepts it — asserted here through the
	MOBILE door, which is the one the audit says cannot reach it. A scouting
	round raised from a phone can demand the coordinate its observation needs.
	"""

	def test_a_phone_raised_task_can_ask_for_the_fix(self):
		self.be(MANAGER)
		task = mobile_api.create_farm_task(
			task_name="Scout the ridge",
			task_type="Scouting",
			evidence_required=dict(SCOUT_CONTRACT),
			location_doctype="Field",
			location=self.block,
			company=MAIN,
		)["name"]
		stored = json.loads(frappe.db.get_value(FARM_TASK, task, "evidence_required"))
		self.assertTrue(stored["gps"])

	def test_the_completion_of_a_phone_raised_task_refuses_without_one(self):
		"""The pair closing: what the create can ask for, the completion demands."""
		self.be(MANAGER)
		task = mobile_api.create_farm_task(
			task_name="Scout the ridge",
			task_type="Scouting",
			evidence_required=dict(SCOUT_CONTRACT),
			location_doctype="Field",
			location=self.block,
			company=MAIN,
		)["name"]

		self.be(WORKER)
		mobile_api.claim_task(task=task)
		mobile_api.start_task(task=task)
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.complete_task_via_mobile(
				task=task,
				findings_text="east row a week behind the rest",
				evidence_files=list(A_PHOTO),
			)
		self.assertIn("gps", str(caught.exception))
		self.assertFalse(STORE.rows(OBSERVATION))

	def test_a_misspelt_key_is_still_refused_at_the_same_door(self):
		"""`gps` joining the vocabulary must not turn `gpss` into a silent no-op:
		a key outside the list asks for nothing and looks like it asks for
		something, which is the worst of both."""
		self.be(MANAGER)
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.create_farm_task(
				task_name="Scout the ridge",
				task_type="Scouting",
				evidence_required={"photos": True, "gpss": True},
				company=MAIN,
			)
		self.assertIn("gpss", str(caught.exception))
		self.assertFalse([row for row in STORE.rows(FARM_TASK) if row.get("task_name") == "Scout the ridge"])
