# SPDX-License-Identifier: MIT
"""Server ↔ iOS contract fixtures for v0.209.0 (the card print queue, Amendment 1).

docs/design/card_print_queue.md §4.1 and §A4. What the five phone routes really answer
for one fixed scenario, volatile values normalised. The phone bundles
byte-identical copies and decodes every one. Regenerate with FARM_CONTRACT_REGEN=1
(needs a QR encoder, as a request renders a card).
"""

import json
import os
import pathlib

from erpnext_mcp import card_print
from erpnext_mcp.api import mobile as mobile_api

from . import test_card_print as base
from .harness import STORE

HERE = pathlib.Path(__file__).parent / "contract" / "v0_209_0"
VOLATILE = {"requested_at", "claimed_at", "printed_at", "last_seen_at"}


def normalised(value):
	if isinstance(value, dict):
		return {k: ("<volatile>" if k in VOLATILE and v else normalised(v)) for k, v in sorted(value.items())}
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


@base.NEEDS_QR
class CardPrintContract(base.CardPrintCase):
	def test_the_five_routes(self):
		"""A single-sided station, which is what the farm has: the front prints, the
		back waits for a flip, and the back is its own job."""
		row = card_print._employee(base.WORKER_EMPLOYEE)[3]
		card_print._employee_card(row, base.MAIN)
		STORE.commit()
		self.be()
		first = mobile_api.request_card_print(
			job_type="Employee ID", reference_name=base.WORKER_EMPLOYEE, client_request_id=base.uid(1)
		)
		check(self, "request_card_print", first)
		check(
			self,
			"request_card_print_duplicate",
			mobile_api.request_card_print(
				job_type="Employee ID", reference_name=base.WORKER_EMPLOYEE, client_request_id=base.uid(1)
			),
		)
		name = first["job"]["name"]
		self.be("Administrator")
		card_print.claim(card_print.DEFAULT_STATION, base.STATION_USER, "Ready", "", "1.1.0")
		card_print.complete(name, base.STATION_USER, True)
		STORE.commit()
		self.be()
		check(self, "list_card_print_jobs", mobile_api.list_card_print_jobs())
		back = mobile_api.request_card_back(name=name, client_request_id=base.uid(2))
		check(self, "request_card_back", back)
		back_name = back["job"]["name"]
		self.be("Administrator")
		card_print.claim(card_print.DEFAULT_STATION, base.STATION_USER, "Ready", "", "1.1.0")
		card_print.complete(back_name, base.STATION_USER, False, "the printer is out of ribbon")
		STORE.commit()
		self.be()
		check(self, "retry_card_print_job", mobile_api.retry_card_print_job(name=back_name))
		check(self, "cancel_card_print_job", mobile_api.cancel_card_print_job(name=back_name))
