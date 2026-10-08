# SPDX-License-Identifier: MIT
"""The packer portal. v0.276.0 (docs/contracts/packer_portal_v0_276.yaml).

Mill Creek's two blocks committed to OVF, by ticker: the sprays with EPA number, rates, REI / PHI and MRL by market;
IPM; projections from last season's tickets; clear to harvest (PHI, an open REI, an MRL gap); nothing else — no cost,
no price, no person, no other block; a named credential shown once and hashed; the same 404 for every bad or revoked
credential and while the company's portal is off; CSV / XLSX / PDF / JSON; the Bearer feed and its switch; every view
and download logged.
"""

import hashlib
import json
from unittest import mock

import frappe

from erpnext_mcp import flags, packer_portal

from .harness import STORE
from .test_api_mobile import MAIN
from .test_farmops_api import PREFIX, FarmOpsAPITestCase
from .test_field_self_service import CENTER, WIND, mill_creek


class PortalCase(FarmOpsAPITestCase):
	def setUp(self):
		super().setUp()
		mill_creek(self)
		frappe.db.set_value("Field", CENTER, "block_ticker", "MCC")
		frappe.db.set_value("Field", WIND, "block_ticker", "MCW")
		STORE.seed("MRL Record", [{"name": "MRL-1", "chemical": "Captan", "crop": "Cherries", "market": "US",
		                           "mrl_ppm": 25}])
		STORE.seed("Spray Application", [
			{"name": "SPRAY-1", "company": MAIN, "status": "Completed", "completed_at": "2027-05-02 07:00:00",
			 "phi_days": 0, "phi_clears_on": "2027-05-02", "rei_hours": 24,
			 "products_applied": json.dumps([{"item": "CAPTAN-80", "item_name": "Captan", "rate_per_acre": 5,
			                                  "rate_uom": "lb", "epa_reg_number": "66330-38", "rei_hours": 24,
			                                  "phi_days": 0, "target": "brown rot", "lot_no": "C2609A"}]),
			 "blocks": [{"block": CENTER, "acres": 11.3}, {"block": "Pearls - OT", "acres": 9}]},
			{"name": "SPRAY-2", "company": MAIN, "status": "Completed", "completed_at": "2027-06-10 07:00:00",
			 "phi_days": 14, "phi_clears_on": "2027-06-24", "rei_hours": 12,
			 "products_applied": json.dumps([{"item": "DELEGATE", "item_name": "Delegate WG", "rate_per_acre": 7,
			                                  "rate_uom": "oz", "epa_reg_number": "62719-541", "phi_days": 14}]),
			 "blocks": [{"block": WIND, "acres": 13.2}]},
		])
		STORE.seed("Crop Observation", [{"name": "OBS-27", "block_doctype": "Field", "block": WIND,
		                                 "observation_type": "Pest", "threat": "Western Cherry Fruit Fly",
		                                 "observed_on": "2027-06-01", "count_observed": 3, "threshold_exceeded": 1}])
		STORE.commit()
		self.share = packer_portal.create_share(MAIN, "Orchard View Farms", ["Bing Block"], season="2027",
		                                        markets=["US", "Japan"])
		STORE.commit()
		patcher = mock.patch.object(frappe.utils, "today", lambda: "2027-06-15")
		patcher.start()
		self.addCleanup(patcher.stop)

	def on(self, value=True):
		flags.upsert(packer_portal.FLAG, "Flag", value, company=MAIN, description="t", owner_area="packer", active=True)
		STORE.commit()

	def issue(self, who="Dana at OVF"):
		self.on()
		out = packer_portal.issue_credential(self.share, who, "dana@ovf.example")
		STORE.commit()
		return out


class WhatTheyAndOnlyTheySee(PortalCase):
	FORBIDDEN = ("Pearls", "4200", "Contract pruning", "Bing Block - MC", "Wind Mecine - MC", "Spider Mites",
	             "price", "cost", "employee", "applicator", "Tim")

	def test_the_sections_by_ticker_and_nothing_else(self):
		data = packer_portal.page(self.share)
		self.assertEqual([b["block"] for b in data["blocks"]], ["MCC", "MCW"])
		spray = next(r for r in data["sprays"] if r["product"] == "Captan")
		self.assertEqual((spray["block"], spray["epa_reg_number"], spray["rate"], spray["lot_no"]),
		                 ("MCC", "66330-38", 5, "C2609A"))
		self.assertEqual(spray["mrl"], {"US": "25 ppm", "Japan": "no record"})
		self.assertEqual(len(data["sprays"]), 2, "the other company's block on the same application is not shown")
		self.assertEqual([r["threat"] for r in data["ipm"]], ["Western Cherry Fruit Fly"])
		mcc = next(r for r in data["projections"] if r["block"] == "MCC")
		self.assertEqual(mcc["seasons_used"], [2026])
		self.assertEqual(mcc["projected_tons"], 9.0, "18,000 lb last season")
		text = json.dumps(data)
		for leak in self.FORBIDDEN:
			self.assertNotIn(leak, text, leak)
		for section, rows in data.items():
			if section in packer_portal.ALLOWED:
				for row in rows:
					self.assertLessEqual(set(row), set(packer_portal.ALLOWED[section]), section)

	def test_clear_to_harvest(self):
		STORE.seed("Spray REI", [{"name": "REI-1", "status": "Active", "block_doctype": "Field", "block": CENTER,
		                          "company": MAIN, "expires_at": "2099-01-01 08:00:00"}])
		STORE.commit()
		status = {r["block"]: r for r in packer_portal.clearance(self.share, "2027-06-15")}
		self.assertEqual(status["MCW"]["status"], "Not yet")
		self.assertIn("PHI until 2027-06-24", status["MCW"]["reasons"])
		self.assertEqual(status["MCC"]["status"], "Not yet", "an open re-entry interval")
		frappe.db.set_value("Spray REI", "REI-1", "status", "Closed")
		STORE.commit()
		status = {r["block"]: r for r in packer_portal.clearance(self.share, "2027-06-15")}
		self.assertEqual(status["MCC"]["status"], "Check", "Captan has no Japan MRL on file")

	def test_sections_switch_off(self):
		frappe.db.set_value(packer_portal.SHARE, self.share, "sections", json.dumps({"sprays": 1, "ipm": 0,
		                                                                            "projections": 0, "clearance": 1}))
		STORE.commit()
		data = packer_portal.page(self.share)
		self.assertNotIn("ipm", data)
		self.assertNotIn("projections", data)


class TheCredential(PortalCase):
	def test_off_until_on_shown_once_hashed_and_revocable(self):
		with self.assertRaisesRegex(packer_portal.PortalError, "packer_portal_enabled"):
			packer_portal.issue_credential(self.share, "Dana")
		out = self.issue()
		doc = frappe.get_doc(packer_portal.SHARE, self.share)
		self.assertEqual(doc.credentials[0].get("token_hash"), hashlib.sha256(out["token"].encode()).hexdigest())
		self.assertNotIn(out["token"], json.dumps(doc.as_dict(), default=str))
		self.assertEqual(packer_portal.find(out["token"])["contact"], "Dana at OVF")
		self.on(False)
		self.assertIsNone(packer_portal.find(out["token"]), "the company's portal off")
		self.on(True)
		packer_portal.revoke(self.share, "Dana at OVF", "left OVF")
		STORE.commit()
		self.assertIsNone(packer_portal.find(out["token"]))


class OverHTTP(PortalCase):
	def get(self, path, **headers):
		return self.post(path, method="GET", credential=False, headers=headers or None)

	def test_page_data_downloads_one_404_and_the_log(self):
		token = self.issue()["token"]
		page = self.get(f"{PREFIX}/packer/{token}")
		self.assertEqual(page.status_code, 200)
		self.assertEqual(page.headers["X-Robots-Tag"], "noindex, nofollow")
		self.assertEqual(page.headers["Referrer-Policy"], "no-referrer")
		self.assertNotIn("MCC", page.get_data(as_text=True), "the shell carries no data")
		data = json.loads(self.get(f"{PREFIX}/packer/{token}/data").get_data(as_text=True))
		self.assertEqual(data["packer"], "Orchard View Farms")
		for fmt, kind in (("csv", "text/csv"), ("xlsx", "application/vnd.openxmlformats"), ("pdf", "application/pdf")):
			answer = self.get(f"{PREFIX}/packer/{token}/download?format={fmt}&block=MCC")
			self.assertEqual(answer.status_code, 200, fmt)
			self.assertTrue(answer.headers["Content-Type"].startswith(kind), fmt)
			self.assertIn("attachment", answer.headers["Content-Disposition"])
		self.assertTrue(self.get(f"{PREFIX}/packer/{token}/download?format=pdf").get_data().startswith(b"%PDF"))
		self.assertEqual(self.get(f"{PREFIX}/packer/{token}/download?format=csv&block=XYZ").status_code, 400)
		bad = [self.get(f"{PREFIX}/packer/{t}") for t in ("nope" * 6, token + "x")]
		self.assertEqual({r.status_code for r in bad}, {404})
		entries = packer_portal.access_log(self.share)
		self.assertEqual({e["what"] for e in entries}, {"page", "data", "download csv", "download xlsx", "download pdf"})
		self.assertTrue(all(e["contact"] == "Dana at OVF" for e in entries))

	def test_the_bearer_feed_and_its_switch(self):
		token = self.issue()["token"]
		feed = self.get(f"{PREFIX}/packer-feed", Authorization=f"Bearer {token}")
		self.assertEqual(feed.status_code, 200)
		self.assertEqual(json.loads(feed.get_data(as_text=True))["season"], "2027")
		self.assertEqual(self.get(f"{PREFIX}/packer-feed").status_code, 404)
		frappe.db.set_value(packer_portal.SHARE, self.share, "sections", json.dumps({"feed": 0}))
		STORE.commit()
		self.assertEqual(self.get(f"{PREFIX}/packer-feed", Authorization=f"Bearer {token}").status_code, 404)


class TheTools(PortalCase):
	def test_reads_and_the_pack(self):
		self.issue()
		share = self.tool_data("get_packer_share", {"share": self.share})
		self.assertEqual(share["credentials"][0]["contact"], "Dana at OVF")
		self.assertNotIn("token_hash", json.dumps(share))
		pack = self.tool_data("get_packer_pack", {"share": self.share, "format": "csv"})
		self.assertTrue(pack["file_name"].endswith(".csv"))
		self.assertIn("MRL Japan", __import__("base64").b64decode(pack["content_base64"]).decode())
		self.assertIn("allow_create_packer_share", self.tool_error("create_packer_share",
		                                                           {"company": MAIN, "packer_name": "X", "fields": "MCC"}))
