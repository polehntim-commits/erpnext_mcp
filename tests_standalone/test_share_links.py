# SPDX-License-Identifier: MIT
"""One share-link engine for every kind. v0.285.0 (pattern P2).

A job link and a packer credential are rows of one Share Link table, found by their hash alone; one kind's token never
opens another kind's page; every use is a Share Link View row; a revoked link stays revoked; extend is capped by the
kind's maximum life.
"""

import datetime

from erpnext_mcp import job_links, packer_portal, share_links

from .harness import STORE, frappe
from .test_job_links import JobCase
from .test_packer_portal import PortalCase


class OneTable(PortalCase):
	def test_both_kinds_live_in_share_link_and_never_cross(self):
		cred = self.issue()
		self.assertIsNotNone(share_links.find(cred["token"], "Customer Portal"))
		self.assertIsNone(share_links.find(cred["token"], "Job"), "a packer credential never opens a job page")
		self.assertIsNone(job_links.find(cred["token"]))
		row = frappe.db.get_value("Share Link", {"target": self.share}, ["kind", "target_doctype", "status"], as_dict=True)
		self.assertEqual((row["kind"], row["target_doctype"], row["status"]), ("Customer Portal", "Customer Portal", "Active"))

	def test_every_use_is_a_view_row_and_counted(self):
		cred = self.issue()
		found = packer_portal.find(cred["token"])
		for what in ("page", "data", "download csv"):
			packer_portal.log(found, what, ip="10.0.0.9", user_agent="Mozilla")
		STORE.commit()
		views = share_links.views("Customer Portal", target=self.share)
		self.assertEqual(sorted(v["what"] for v in views), ["data", "download csv", "page"])
		self.assertEqual(frappe.db.get_value("Share Link", found["name"], "use_count"), 3)
		self.assertEqual(packer_portal.access_log(self.share)[0]["customer"], "Orchard View Farms")

	def test_revoked_stays_revoked_and_extend_is_capped(self):
		cred = self.issue()
		link = packer_portal.credentials(self.share)[0]["link"]
		far = share_links._now() + datetime.timedelta(days=5000)
		got = share_links.extend(link, "tim", until=far)
		self.assertLessEqual(frappe.utils.get_datetime(got["expires_at"]),
		                     share_links._now() + datetime.timedelta(days=share_links.KINDS["Packer"]["max_days"] + 1))
		packer_portal.revoke(self.share, reason="season over")
		STORE.commit()
		self.assertIsNone(packer_portal.find(cred["token"]))
		with self.assertRaises(share_links.ShareLinkError):
			share_links.extend(link, "tim", days=3)


class JobKind(JobCase):
	def test_closing_the_job_expires_its_links(self):
		_data, token = self.ready_and_link()
		self.assertIsNotNone(job_links.find(token))
		job_links.close_job(self.job, "tim")
		STORE.commit()
		self.assertEqual(frappe.db.get_value("Share Link", _data["link"], "status"), "Expired")
		self.assertIsNone(job_links.find(token))
