# SPDX-License-Identifier: MIT
"""The pre-harvest interval, enforced at the moment harvest is initiated on a block.

THE CLAIM THIS FILE DEFENDS, and it is one sentence: a Harvest task cannot be
raised on, or dispatched to, a block that is still inside a pre-harvest interval.

WHY THIS ONE REFUSES WHEN THE RESTRICTED-ENTRY GUARD BESIDE IT WARNS is the most
important thing here and is asserted in both directions, because the two look
identical and are opposite decisions:

  * An REI is a condition on ENTRY, and entry inside one is LAWFUL with the
    label's PPE on (40 CFR 170.607). A server refusing it would invent a rule
    stricter than the regulation, so `_rei_warnings` tells the foreman and
    dispatches. `TheTwoIntervalsAreNotTheSameDecision` pins that down.
  * A PHI is a condition on the FRUIT, and there is no PPE for it. A pick inside
    the interval is residue above tolerance on a load, found at the packing house
    days later and traced back to a block and a date.

FIVE CLAIMS.

1. `TheReader` — both registers a spray leaves a date on are read, and the
   longest window wins. A site that files sprays as Farm Tasks and never writes
   a Spray Application gets the same answer.

2. `RaisingTheHarvest` — refused at `create_farm_task`, before anything is
   inserted, with the block, the date it opens, the product and the record.

3. `TheBoundary` — the day the block actually opens, asserted on both sides.
   `phi_clears_on` is the LAST day of the interval and the pick is the day after;
   an off-by-one here is off by one in the dangerous direction, and this is the
   test that would catch a guard that cleared a block the day before the
   compliance alert about it went out.

4. `TheOverride` — a foreman may set it aside with a reason, the reason lands on
   the task itself and not only in a log, and an override without one is refused.

5. `TheWorkersDoorHasNoOverride` — `claim_farm_task` refuses and names the tool
   that can, because "the picker decided the interval did not apply" is not a
   defence anybody can offer afterwards.
"""

import frappe

from erpnext_mcp import compliance_fields
from erpnext_mcp.tools import spray as spray_tools

from .fixtures import MAIN, SPRAY, V12TestCase, seed_masters, seed_stock
from .harness import STORE

BLOCK = "Yellow Camp Block 3 - MC"
BLOCK_TWO = "Yellow Camp Block 4 - MC"

#: One photograph and a finding — the contract every task here closes on.
PICK = {"photos": True, "findings_text": True}

WORKER = "HR-EMP-00001"

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_parcel",
		"create_field",
		"create_spray_tank_mix",
		"create_spray_application",
		"get_spray_application",
		"list_active_reis",
		"create_farm_task",
		"assign_farm_task",
		"claim_farm_task",
		"get_farm_task",
	)
}


class PhiTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		seed_masters()
		seed_stock()
		self.configure(enabled=1, **ALL_ON)
		# Through the real installer rather than by hand: the whole mechanism
		# turns on `compat.has_field` seeing `Item.phi_days`, and a fixture that
		# wrote the column directly would prove the window works on a site
		# configured in a way this app never produces.
		compliance_fields.install_compliance_fields()
		spray = STORE.get_raw("Item", SPRAY)
		spray["rei_hours"] = 4
		spray["phi_days"] = 14
		self._farm()
		STORE.seed(
			"Employee",
			[
				{
					"name": WORKER,
					"employee_name": "Ada Orchard",
					"status": "Active",
					"company": MAIN,
				}
			],
		)

	def _farm(self):
		self.tool_data(
			"create_parcel",
			{"owning_entity": MAIN, "parcel_name": "Mill Creek", "acreage": 131.43},
		)
		for name in ("Yellow Camp Block 3", "Yellow Camp Block 4"):
			self.tool_data(
				"create_field",
				{"parcel": "Mill Creek", "field_name": name, "acreage": 12.5, "variety": "Bing"},
			)

	# ── helpers ─────────────────────────────────────────────────────────────
	def a_spray(self, blocks=(BLOCK,), **kw):
		payload = {
			"blocks": list(blocks),
			"company": MAIN,
			"products": [{"item_code": SPRAY, "rate_per_acre": 5, "rate_uom": "Lb"}],
		}
		payload.update(kw)
		return self.tool_data("create_spray_application", payload)

	def a_harvest(self, block=BLOCK, **kw):
		payload = {
			"task_name": f"Pick {block}",
			"task_type": "Harvest",
			"evidence_required": PICK,
			"company": MAIN,
			"location_doctype": "Field",
			"location": block,
		}
		payload.update(kw)
		return payload

	def raise_harvest(self, block=BLOCK, **kw):
		return self.tool_data("create_farm_task", self.a_harvest(block, **kw))

	def refuse_harvest(self, block=BLOCK, **kw):
		return self.tool_error("create_farm_task", self.a_harvest(block, **kw))

	def set_clears_on(self, date, application=None):
		"""Move a Spray Application's stamped clearing date. Returns its docname."""
		rows = STORE.rows("Spray Application")
		row = STORE.get_raw("Spray Application", application) if application else rows[-1]
		row["phi_clears_on"] = date
		return row["name"]

	def days_from_today(self, days):
		return str(frappe.utils.add_days(frappe.utils.today(), days))


class TheReader(PhiTestCase):
	def test_a_spray_application_puts_the_block_inside_its_interval(self):
		self.a_spray()
		windows = spray_tools.phi_windows_for_blocks([BLOCK], MAIN)
		self.assertEqual(len(windows), 1)
		self.assertEqual(windows[0]["block"], BLOCK)
		self.assertEqual(windows[0]["source_doctype"], "Spray Application")
		self.assertEqual(windows[0]["phi_source_item"], SPRAY)
		self.assertEqual(windows[0]["phi_clears_on"], self.days_from_today(14))

	def test_a_block_that_was_not_sprayed_has_no_window(self):
		self.a_spray(blocks=(BLOCK,))
		self.assertEqual(spray_tools.phi_windows_for_blocks([BLOCK_TWO], MAIN), [])

	def test_a_completed_spray_farm_task_is_read_too(self):
		"""The other register a spray leaves a date on.

		`stock_bridge.spray_windows` stamps `phi_clears_on` on the task when its
		tank mix is drawn down, which is the path a spray dispatched from the
		board takes. A site that files sprays only that way would get an empty
		answer from a reader that consulted Spray Application alone — and an
		empty answer here reads as 'this block is clear'.
		"""
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "FT-SPRAY-01",
					"task_name": "Cover 1",
					"task_type": "Spray",
					"state": "Completed",
					"company": MAIN,
					"location_doctype": "Field",
					"location": BLOCK_TWO,
					"phi_clears_on": self.days_from_today(9),
					"phi_source_item": SPRAY,
				}
			],
		)
		windows = spray_tools.phi_windows_for_blocks([BLOCK_TWO], MAIN)
		self.assertEqual(len(windows), 1)
		self.assertEqual(windows[0]["source_doctype"], "Farm Task")
		self.assertEqual(windows[0]["source"], "FT-SPRAY-01")

	def test_the_longest_window_is_reported_first(self):
		"""Two sprays a week apart leave two dates; the block opens on the later."""
		self.a_spray()
		first = self.set_clears_on(self.days_from_today(3))
		self.a_spray()
		self.set_clears_on(self.days_from_today(11))
		windows = spray_tools.phi_windows_for_blocks([BLOCK], MAIN)
		self.assertEqual(len(windows), 2)
		self.assertEqual(windows[0]["phi_clears_on"], self.days_from_today(11))
		self.assertNotEqual(windows[0]["source"], first)


class RaisingTheHarvest(PhiTestCase):
	def test_a_harvest_on_a_sprayed_block_is_refused(self):
		self.a_spray()
		error = self.refuse_harvest()
		self.assertIn(BLOCK, error)
		self.assertIn(self.days_from_today(15), error)  # the day AFTER phi_clears_on
		self.assertIn(SPRAY, error)
		self.assertIn("residue violation on a shipped load", error)
		self.assertIn("Nothing was created", error)

	def test_nothing_is_inserted_when_it_refuses(self):
		"""Refused on the ARGUMENTS, before the document exists — so there is no
		half-created task and nothing to roll back."""
		self.a_spray()
		before = len(STORE.rows("Farm Task"))
		self.refuse_harvest()
		self.assertEqual(len(STORE.rows("Farm Task")), before)

	def test_an_unsprayed_block_is_raised_normally(self):
		self.a_spray(blocks=(BLOCK,))
		data = self.raise_harvest(BLOCK_TWO)
		self.assertEqual(data["task_type"], "Harvest")
		self.assertNotIn("phi_override", data)

	def test_a_lowercase_task_type_does_not_slip_the_guard(self):
		"""The guard runs on the arguments, BEFORE `as_choice` normalises them.

		A caller sending 'harvest' would otherwise get a task of type Harvest
		with no guard run against it at all — the worst of the three outcomes.
		"""
		self.a_spray()
		error = self.tool_error("create_farm_task", self.a_harvest(task_type="harvest"))
		self.assertIn("pre-harvest interval", error)

	def test_a_task_with_no_location_is_not_guessed_at(self):
		self.a_spray()
		payload = self.a_harvest()
		payload.pop("location")
		payload.pop("location_doctype")
		self.assertTrue(self.tool_data("create_farm_task", payload)["name"])


class TheTwoIntervalsAreNotTheSameDecision(PhiTestCase):
	"""The pair that would look like an inconsistency if only one were read."""

	def test_a_non_harvest_task_on_the_same_block_is_raised_with_a_warning(self):
		"""The REI is live on this block too — four hours from the same spray —
		and a Prune is dispatched with a sentence rather than refused. Entry
		inside an REI is lawful with the label's PPE on."""
		self.a_spray()
		data = self.raise_harvest(task_type="Inspection", task_name="Walk the block")
		self.assertTrue(data["name"])
		self.assertTrue(any("REI active" in warning for warning in data["warnings"]), data.get("warnings"))

	def test_the_same_block_refuses_the_harvest(self):
		self.a_spray()
		self.assertIn("pre-harvest interval", self.refuse_harvest())


class TheBoundary(PhiTestCase):
	"""`phi_clears_on` is the LAST day of the interval; the pick is the day after.

	The `phi_harvest_window` compliance rule raises while `phi_clears_on >= today`
	and silences the day after, so a guard that cleared the block ON that date
	would open it a day before the alert about it went out. Both sides asserted.
	"""

	def test_the_block_is_still_shut_on_the_clearing_date_itself(self):
		self.a_spray()
		self.set_clears_on(frappe.utils.today())
		error = self.refuse_harvest()
		self.assertIn(self.days_from_today(1), error)

	def test_the_block_opens_the_day_after(self):
		self.a_spray()
		self.set_clears_on(self.days_from_today(-1))
		self.assertTrue(self.raise_harvest()["name"])

	def test_the_reader_agrees_with_the_guard(self):
		self.a_spray()
		self.set_clears_on(frappe.utils.today())
		windows = spray_tools.phi_windows_for_blocks([BLOCK], MAIN)
		self.assertEqual(len(windows), 1)
		self.assertEqual(windows[0]["days_remaining"], 1)


class TheOverride(PhiTestCase):
	def test_an_override_without_a_reason_is_refused(self):
		self.a_spray()
		error = self.refuse_harvest(override_phi=True)
		self.assertIn("phi_override_reason", error)
		self.assertIn("Nothing was created", error)

	def test_an_override_with_a_reason_raises_the_task_and_says_so(self):
		self.a_spray()
		reason = "The tank only covered the west four acres; this pick is the east block."
		data = self.raise_harvest(override_phi=True, phi_override_reason=reason)
		self.assertTrue(data["name"])
		self.assertEqual(data["phi_override"]["reason"], reason)
		self.assertEqual(data["phi_override"]["windows_overridden"][0]["block"], BLOCK)
		self.assertTrue(any("override" in warning.lower() for warning in data["warnings"]))

	def test_the_reason_lands_on_the_task_and_not_only_in_a_log(self):
		"""A load questioned at the packing house is traced to a block and a
		date, and the record somebody opens is the task."""
		self.a_spray()
		data = self.raise_harvest(
			override_phi=True, phi_override_reason="Label PHI corrected to 7 days by the registrant."
		)
		notes = str(frappe.db.get_value("Farm Task", data["name"], "notes") or "")
		self.assertIn("PRE-HARVEST INTERVAL OVERRIDDEN", notes)
		self.assertIn("Label PHI corrected", notes)
		self.assertIn(BLOCK, notes)

	def test_an_existing_note_is_kept(self):
		self.a_spray()
		data = self.raise_harvest(
			notes="Start at the top of the row.",
			override_phi=True,
			phi_override_reason="Part-block tank.",
		)
		notes = str(frappe.db.get_value("Farm Task", data["name"], "notes") or "")
		self.assertIn("Start at the top of the row.", notes)
		self.assertIn("PRE-HARVEST INTERVAL OVERRIDDEN", notes)

	def test_dispatching_into_the_interval_is_refused_and_overridable(self):
		task = self.raise_harvest(BLOCK_TWO)["name"]
		# The spray happens AFTER the task was raised, which is the ordinary
		# order: a picking plan is made in advance and the cover spray lands on
		# top of it. The guard at the dispatch is what catches that.
		self.a_spray(blocks=(BLOCK_TWO,))
		error = self.tool_error("assign_farm_task", {"task": task, "assigned_to": WORKER})
		self.assertIn("pre-harvest interval", error)
		self.assertIn("Nothing was changed", error)

		data = self.tool_data(
			"assign_farm_task",
			{
				"task": task,
				"assigned_to": WORKER,
				"override_phi": True,
				"phi_override_reason": "Fruit is going to the juicer, not the fresh line.",
			},
		)
		self.assertEqual(data["phi_override"]["reason"], "Fruit is going to the juicer, not the fresh line.")
		notes = str(frappe.db.get_value("Farm Task", task, "notes") or "")
		self.assertIn("PRE-HARVEST INTERVAL OVERRIDDEN", notes)


class TheWorkersDoorHasNoOverride(PhiTestCase):
	def test_a_worker_cannot_claim_a_harvest_inside_the_interval(self):
		task = self.raise_harvest(BLOCK_TWO, dispatch_mode="Self-pick")["name"]
		self.a_spray(blocks=(BLOCK_TWO,))
		error = self.tool_error("claim_farm_task", {"task": task, "worker_id": WORKER})
		self.assertIn("pre-harvest interval", error)
		self.assertIn("assign_farm_task", error)
		self.assertIn("not a defence", error)

	def test_the_override_argument_is_not_on_the_claim_schema(self):
		"""Advertised as closed AND refused by the handler.

		`additionalProperties: false` is a promise made only to well-behaved
		callers — nothing this app controls enforces it — so the absence of the
		override has to be a property of the code and not only of the schema.
		"""
		from erpnext_mcp import registry
		from erpnext_mcp.errors import ToolError
		from erpnext_mcp.tools import dispatch

		self.assertNotIn("override_phi", registry.TOOLS["claim_farm_task"]["inputSchema"]["properties"])
		task = self.raise_harvest(BLOCK_TWO, dispatch_mode="Self-pick")["name"]
		self.a_spray(blocks=(BLOCK_TWO,))
		with self.assertRaises(ToolError) as caught:
			dispatch.claim_farm_task(
				{
					"task": task,
					"worker_id": WORKER,
					"override_phi": True,
					"phi_override_reason": "let me through",
				}
			)
		self.assertIn("no override on this tool", str(caught.exception))

	def test_a_worker_may_still_claim_a_clear_block(self):
		task = self.raise_harvest(BLOCK_TWO, dispatch_mode="Self-pick")["name"]
		self.assertTrue(self.tool_data("claim_farm_task", {"task": task, "worker_id": WORKER}))


# ── v0.194.0 ────────────────────────────────────────────────────────────────
class TheStartIsTheLastDoor(PhiTestCase):
	"""A Harvest task claimed on a clear block, then sprayed, then started.

	Create, assign and claim were guarded; start was not, so a task taken the
	evening before a morning cover spray went out to the block with nothing said.
	The start refuses only windows opened AFTER the claim or dispatch — anything
	older was already refused or overridden with a reason at that door.
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON, allow_start_farm_task=1)

	def claimed(self, block=BLOCK_TWO):
		task = self.raise_harvest(block, dispatch_mode="Self-pick")["name"]
		self.tool_data("claim_farm_task", {"task": task, "worker_id": WORKER})
		return task

	def start(self, task):
		return self.tool_data("start_farm_task", {"task": task, "worker_id": WORKER})

	def test_a_spray_after_the_claim_refuses_the_start(self):
		task = self.claimed()
		self.a_spray(blocks=(BLOCK_TWO,))
		error = self.tool_error("start_farm_task", {"task": task, "worker_id": WORKER})
		self.assertIn("pre-harvest interval", error)
		self.assertIn("assign_farm_task", error)
		self.assertIn("Nothing was started", error)
		self.assertNotEqual(frappe.db.get_value("Farm Task", task, "state"), "In-Progress")

	def test_a_clear_block_starts(self):
		task = self.claimed()
		self.assertEqual(self.start(task)["assignment"]["state"], "In-Progress")

	def test_a_spray_on_another_block_does_not_refuse(self):
		task = self.claimed(BLOCK_TWO)
		self.a_spray(blocks=(BLOCK,))
		self.assertEqual(self.start(task)["assignment"]["state"], "In-Progress")

	def test_a_foremans_override_is_not_re_refused_at_the_start(self):
		task = self.raise_harvest(BLOCK_TWO)["name"]
		self.a_spray(blocks=(BLOCK_TWO,))
		self.tool_data(
			"assign_farm_task",
			{
				"task": task,
				"assigned_to": WORKER,
				"override_phi": True,
				"phi_override_reason": "Juice fruit, not fresh.",
			},
		)
		self.assertEqual(self.start(task)["assignment"]["state"], "In-Progress")

	def test_a_new_spray_after_an_override_is_refused(self):
		"""The override covered the spray the foreman was shown, not the next one."""
		task = self.raise_harvest(BLOCK_TWO)["name"]
		self.a_spray(blocks=(BLOCK_TWO,))
		self.tool_data(
			"assign_farm_task",
			{
				"task": task,
				"assigned_to": WORKER,
				"override_phi": True,
				"phi_override_reason": "Juice fruit.",
			},
		)
		self.a_spray(blocks=(BLOCK_TWO,))
		error = self.tool_error("start_farm_task", {"task": task, "worker_id": WORKER})
		self.assertIn("pre-harvest interval", error)

	def test_a_task_that_is_not_a_harvest_starts_inside_the_interval(self):
		task = self.raise_harvest(BLOCK_TWO, task_type="Scouting", dispatch_mode="Self-pick")["name"]
		self.tool_data("claim_farm_task", {"task": task, "worker_id": WORKER})
		self.a_spray(blocks=(BLOCK_TWO,))
		self.assertEqual(self.start(task)["assignment"]["state"], "In-Progress")
