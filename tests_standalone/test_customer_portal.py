# SPDX-License-Identifier: MIT
"""The customer / first-handler portal. v0.313.0 (docs/design/customer_portal_plan.md).

The packer portal made generic: any ERPNext Customer that buys, packs or first-handles the fruit gets a portal, issued
by the grower company; Orchard View is just the first. A customer only ever sees its own blocks. The generic tools
(create_customer_portal, issue_portal_credential, revoke_portal_access, get_customer_portal,
get_customer_portal_pack, list_portal_access_log) do it; the v0.276.0 packer names stay callable and are logged as
superseded; credentials issued as "Packer" still open the page; /portal/<token> and /portal-feed are the new paths.
"""

import json

import frappe

from erpnext_mcp import customer_portal, share_links, superseded
from erpnext_mcp.patches import customer_portal_followups

from .harness import STORE
from .test_api_mobile import MAIN
from .test_farmops_api import PREFIX
from .test_field_self_service import CENTER, WIND
from .test_packer_portal import PortalCase

class HTTP:
	def get(self, path, **headers):
		return self.post(path, method="GET", credential=False, headers=headers or None)


GENERIC = ("create_customer_portal", "issue_portal_credential", "revoke_portal_access", "get_customer_portal",
           "get_customer_portal_pack", "list_portal_access_log")


class AnyCustomer(HTTP, PortalCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{f"allow_{n}": 1 for n in GENERIC})
		STORE.seed("Customer", [{"name": "Monson Fruit", "customer_name": "Monson Fruit"}])
		STORE.commit()

	def test_a_second_customer_sees_only_its_own_block(self):
		got = self.tool_data("create_customer_portal", {"company": MAIN, "customer": "Monson Fruit", "role": "Buyer",
		                                                "fields": "Wind Mecine", "season": "2027"})
		portal = got["portal"]
		STORE.commit()
		self.assertEqual((got["customer"], got["role"], got["blocks"]), ("Monson Fruit", "Buyer", ["MCW"]))
		page = self.tool_data("get_customer_portal", {"portal": portal})["page"]
		self.assertEqual([b["block"] for b in page["blocks"]], ["MCW"])
		self.assertTrue(all(r["block"] == "MCW" for r in page["sprays"]))
		self.assertNotIn("MCC", json.dumps(page))
		self.assertEqual((page["customer"], page["role"], page["grower"]), ("Monson Fruit", "Buyer", MAIN))

	def test_a_portal_is_for_a_real_customer(self):
		error = self.tool_error("create_customer_portal", {"company": MAIN, "customer": "Nobody Packing",
		                                                   "fields": "Wind Mecine"})
		self.assertIn("ERPNext Customer", error)

	def test_the_generic_tools_end_to_end_and_the_log(self):
		self.on()
		made = self.tool_data("issue_portal_credential", {"portal": self.share, "contact_name": "Dana at OVF"})
		self.assertIn("/portal/", made["url"])
		self.assertTrue(made["feed"].endswith("/portal-feed"))
		STORE.commit()
		page = self.get(f"{PREFIX}/portal/{made['token']}/data")
		self.assertEqual(json.loads(page.get_data(as_text=True))["customer"], "Orchard View Farms")
		STORE.commit()
		log = self.tool_data("list_portal_access_log", {"portal": self.share})
		self.assertEqual(log["entries"][0]["what"], "data")
		pack = self.tool_data("get_customer_portal_pack", {"portal": self.share, "format": "csv"})
		self.assertTrue(pack["file_name"].endswith(".csv"))
		self.tool_data("revoke_portal_access", {"portal": self.share, "contact_name": "Dana at OVF"})
		STORE.commit()
		self.assertEqual(self.get(f"{PREFIX}/portal/{made['token']}/data").status_code, 404)


class TheOldNames(HTTP, PortalCase):
	def test_a_credential_issued_as_packer_still_opens_the_page(self):
		self.on()
		made = share_links.issue("Packer", self.share, company=MAIN, actor="t", days=30, contact_name="Old link")
		STORE.commit()
		self.assertIsNotNone(customer_portal.find(made["token"]))
		self.assertEqual(self.get(f"{PREFIX}/packer/{made['token']}/data").status_code, 200)

	def test_each_old_tool_is_superseded_by_its_new_name(self):
		for old, new in superseded.RENAMED.items():
			self.assertEqual(superseded.superseded_by(old), new)

	def test_create_packer_share_still_works_by_packer_name(self):
		self.configure(enabled=1, allow_create_packer_share=1)
		got = self.tool_data("create_packer_share", {"company": MAIN, "packer_name": "Orchard View Farms",
		                                             "fields": "Bing Block", "season": "2027"})
		self.assertEqual((got["customer"], got["role"]), ("Orchard View Farms", "Packer"))


class TheSwitchesCarryAcross(PortalCase):
	def test_an_old_switch_on_turns_the_new_one_on_and_off_stays_off(self):
		self.configure(enabled=1, allow_create_packer_share=1, allow_issue_packer_credential=0,
		               allow_create_customer_portal=0, allow_issue_portal_credential=0)
		customer_portal_followups._switches()
		doc = frappe.get_single("ERPNext MCP Settings")
		self.assertEqual((int(doc.get("allow_create_customer_portal") or 0),
		                  int(doc.get("allow_issue_portal_credential") or 0)), (1, 0))
