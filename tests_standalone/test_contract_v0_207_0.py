# SPDX-License-Identifier: MIT
"""Server ↔ iOS contract fixtures for v0.207.0.

docs/design/phone_config_and_compliance_loop.md §7. The JSON files in
`tests_standalone/contract/v0_207_0/` are what the real route functions answer
for one fixed scenario, with volatile values (timestamps, etags) normalised. The
phone bundles byte-identical copies and decodes every one; this test fails the
moment the server's shape drifts from what was frozen.

Regenerate deliberately with FARM_CONTRACT_REGEN=1.
"""

import json
import os
import pathlib

from erpnext_mcp import tiles
from erpnext_mcp.api import mobile as mobile_api

from . import test_phone_config as base
from .harness import STORE

HERE = pathlib.Path(__file__).parent / "contract" / "v0_207_0"
VOLATILE = {"evaluated_at", "etag", "first_filed_at", "published_on", "timestamp", "started_at"}


def normalised(value):
	if isinstance(value, dict):
		return {k: ("<volatile>" if k in VOLATILE else normalised(v)) for k, v in sorted(value.items())}
	if isinstance(value, list):
		return [normalised(v) for v in value]
	return value


def check(case, name: str, value) -> None:
	text = json.dumps(normalised(value), indent=1, sort_keys=True, ensure_ascii=False, default=str) + "\n"
	path = HERE / f"{name}.json"
	if os.environ.get("FARM_CONTRACT_REGEN") == "1":
		HERE.mkdir(parents=True, exist_ok=True)
		path.write_text(text)
		return
	case.assertTrue(path.exists(), f"{path.name} is missing — regenerate with FARM_CONTRACT_REGEN=1")
	case.assertEqual(path.read_text(), text, f"{path.name} drifted from the frozen contract")


class ContractFixtures(base.PhoneConfigCase):
	def test_wizard_spec_and_submit(self):
		self.draft("create_wizard_definition", "near_miss", base.NEAR_MISS)
		self.publish("Wizard", "near_miss", 1)
		STORE.commit()
		self.be()
		check(self, "wizard_spec", mobile_api.get_wizard_definition(wizard="near_miss"))
		answers = {
			"incident_description": "Ladder slipped",
			"occurred_at": "2026-07-23 08:00:00",
			"injury": False,
		}
		result = mobile_api.submit_wizard_via_mobile(
			wizard="near_miss",
			answers=answers,
			config_version="wizard:near_miss@1",
			client_reference="ref-contract",
		)
		result["result"] = {"shape": sorted(result["result"])}
		check(self, "wizard_submit", result)
		check(
			self,
			"wizard_submit_duplicate",
			mobile_api.submit_wizard_via_mobile(
				wizard="near_miss",
				answers=answers,
				config_version="wizard:near_miss@1",
				client_reference="ref-contract",
			),
		)

	def test_tiles(self):
		tiles.seed()
		STORE.commit()
		self.be()
		check(self, "get_tiles_today", mobile_api.get_tiles(surface="today", app_version="0.21.0"))


class ContractInbox(base.PhoneConfigCase):
	def setUp(self):
		super().setUp()
		base.build_loop_site(self)

	def test_inbox_and_start(self):
		self.be()
		check(self, "compliance_inbox", mobile_api.get_compliance_inbox())
		started = mobile_api.start_template_task(
			template="Detector Test", source_alert="ALERT-1", client_reference="c1"
		)
		check(
			self, "start_template_task_keys", {"keys": sorted(started), "task_keys": sorted(started["task"])}
		)
