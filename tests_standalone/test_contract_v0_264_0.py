# SPDX-License-Identifier: MIT
"""Server ↔ iOS contract fixtures for v0.264.0 — pest degree days per block. docs/contracts/pest_dd_v0_264.yaml.

Weather is mocked (a constant 70 / 50 °F day) and today fixed, so the fixtures are deterministic.
Regenerate deliberately with FARM_CONTRACT_REGEN=1, then copy the files to the app."""

import pathlib

from erpnext_mcp.api import mobile as mobile_api

from . import test_contract_v0_262_0 as v262
from .harness import set_roles
from .test_api_mobile import WORKER, MobileAPITestCase
from .test_pest_dd import BLOCK, dd_site

HERE = pathlib.Path(__file__).parent / "contract" / "v0_264_0"


class ContractPestDD(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		dd_site(self)
		set_roles(WORKER, ["Field Worker"])
		self.be()

	def check(self, name, value):
		before = v262.HERE
		v262.HERE = HERE
		try:
			v262.check(self, name, value)
		finally:
			v262.HERE = before

	def test_the_two_answers(self):
		self.check("get_ipm_graph_with_dd", mobile_api.get_ipm_graph(block=BLOCK, depth=1, limit=30, with_dd=1, kinds="Insect Pest"))
		self.check("get_pest_dd_status", mobile_api.get_pest_dd_status(block=BLOCK))
