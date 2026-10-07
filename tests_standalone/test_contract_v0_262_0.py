# SPDX-License-Identifier: MIT
"""Server ↔ iOS contract fixtures for v0.262.0 — the IPM relationship graph.

docs/contracts/ipm_graph_v0_262.yaml. The JSON files in `tests_standalone/contract/v0_262_0/` are what the
real routes answer for one fixed scenario, with volatile values normalised. FarmOps 0.49.0 bundles
byte-identical copies (FarmOpsKit/Tests/FarmOpsKitTests/Fixtures/contract_v0_262_0/) and decodes every
one; this test fails the moment the server's shape drifts from what was frozen.

Regenerate deliberately with FARM_CONTRACT_REGEN=1, then copy the files to the app.
"""

import json
import os
import pathlib

from erpnext_mcp import ipm_graph
from erpnext_mcp.api import mobile as mobile_api

from .fixtures import MAIN
from .harness import STORE, set_roles
from .test_api_mobile import WORKER, MobileAPITestCase

HERE = pathlib.Path(__file__).parent / "contract" / "v0_262_0"
VOLATILE = {"graph_version", "observation", "id"}
BLOCK = "Block 7 Bing"


def normalised(value, key=""):
	if isinstance(value, dict):
		return {k: ("<volatile>" if k in VOLATILE and not (k == "id" and "kind" in value) else normalised(v, k))
		        for k, v in sorted(value.items())}
	if isinstance(value, list):
		return [normalised(v) for v in value]
	return value


def check(case, name: str, value) -> None:
	text = json.dumps({"message": normalised(value)}, indent=1, sort_keys=True, ensure_ascii=False, default=str) + "\n"
	path = HERE / f"{name}.json"
	if os.environ.get("FARM_CONTRACT_REGEN") == "1":
		HERE.mkdir(parents=True, exist_ok=True)
		path.write_text(text)
		return
	case.assertTrue(path.exists(), f"{path.name} is missing — regenerate with FARM_CONTRACT_REGEN=1")
	case.assertEqual(path.read_text(), text, f"{path.name} drifted from the frozen contract")


class ContractIPM(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		ipm_graph.seed()
		STORE.seed("Field", [{"name": BLOCK, "field_name": BLOCK, "crop": "Cherries", "owning_entity": MAIN}])
		STORE.seed("Crop Observation", [{"name": "OBS-STAGE", "block_doctype": "Field", "block": BLOCK,
		                                 "observation_type": "Growth Stage", "growth_stage_code": "85",
		                                 "observed_on": "2026-07-20", "company": MAIN}])
		row = next(r for r in STORE.rows("Pest Action Threshold") if r.get("threat") == "Spider Mites")
		ipm_graph.decide([row["name"]], "approve", "tim@example.com")
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be()

	def test_the_five_answers(self):
		check(self, "get_ipm_graph", mobile_api.get_ipm_graph(block=BLOCK, depth=2, limit=40))
		check(self, "get_ipm_organism", mobile_api.get_ipm_organism(organism="american-robin"))
		check(self, "save_ipm_relationship", mobile_api.save_ipm_relationship(
			subject="green-lacewing", relation="preys_on", object="cherry-slug", weight=0.3, confidence=0.5,
			notes="Seen on the north rows", crop="sweet-cherry", client_request_id="contract-ipm-1"))
		check(self, "record_pest_observation", mobile_api.record_pest_observation(
			block=BLOCK, organism="spider-mites", count=8, sample_unit="Per Leaf", sample_size=25,
			client_request_id="contract-ipm-2"))
		check(self, "get_ipm_threshold_status", mobile_api.get_ipm_threshold_status(
			organism="american-robin", block=BLOCK))
