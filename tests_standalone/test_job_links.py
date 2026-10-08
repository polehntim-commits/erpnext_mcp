# SPDX-License-Identifier: MIT
"""Contractor and supplier job links. v0.271.0 (docs/contracts/job_links_v0_271.yaml).

Mill Creek's old Bing (Wind Machine + Center Piece) as the KEWI orchard-removal job: drafted with its prep tasks, not
shareable until the company's sharing is on and the prep is done (or overridden), one link shown once, the same 404 for
every bad token, expiry by end date and by closing, revocation, the page's data boundary (only this job), the
contractor's actions, photos, the view log, the post-completion prompt, the HTTP headers and the phone's gates.
"""

import hashlib
import json
from unittest import mock

import frappe

from erpnext_mcp import flags, job_links, task_templates
from erpnext_mcp.api import mobile as mobile_api

from . import test_contract_v0_262_0 as v262
from .harness import STORE, set_roles
from .test_api_mobile import MAIN, WORKER
from .test_farmops_api import PREFIX, FarmOpsAPITestCase
from .test_field_self_service import CENTER, WIND, mill_creek

HERE = v262.HERE.parent / "v0_271_0"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


class JobCase(FarmOpsAPITestCase):
	def setUp(self):
		super().setUp()
		mill_creek(self)
		task_templates.seed_farm_task_templates()
		job_links.seed()
		STORE.commit()
		self.job = job_links.create_job("orchard_removal", MAIN, fields=["Bing Block"], supplier="",
		                                title="Remove the old Bing", start_date="2026-10-20", end_date="2026-11-15",
		                                contact_name="Tim", contact_phone="541-555-0100", entrance=(45.5838, -121.2276),
		                                gate_notes="Gate code at the shop.", actor="tim@example.test")
		STORE.commit()

	def share_on(self):
		flags.upsert(job_links.FLAG, "Flag", True, company=MAIN, description="test", owner_area="job_links", active=True)
		STORE.commit()

	def ready_and_link(self):
		self.share_on()
		job_links.mark_ready(self.job, "tim@example.test", override_reason="crew finished early")
		data = job_links.issue_link(self.job, "tim@example.test")
		STORE.commit()
		return data, data["url"].rsplit("/", 1)[1]


class TheDraft(JobCase):
	def test_blocks_by_group_alias_and_prep_tasks_per_block(self):
		doc = frappe.get_doc("Contractor Job", self.job)
		self.assertEqual(doc.status, "Draft")
		self.assertEqual(sorted(r.get("field") for r in doc.fields), sorted([CENTER, WIND]))
		prep = [t for t in doc.prep_tasks.splitlines() if t]
		self.assertEqual(len(prep), 4, "gather sprinklers + mark valves on each of the two blocks")
		self.assertEqual({frappe.db.get_value("Farm Task", t, "location") for t in prep}, {CENTER, WIND})

	def test_not_shareable_while_sharing_is_off_or_prep_is_open(self):
		with self.assertRaisesRegex(job_links.JobError, "sharing job links is off"):
			job_links.issue_link(self.job, "tim")
		self.share_on()
		with self.assertRaisesRegex(job_links.JobError, "not ready"):
			job_links.issue_link(self.job, "tim")
		with self.assertRaisesRegex(job_links.JobError, "prep task"):
			job_links.mark_ready(self.job, "tim")
		for task in frappe.get_doc("Contractor Job", self.job).prep_tasks.splitlines():
			frappe.db.set_value("Farm Task", task, "state", "Completed")
		self.assertEqual(job_links.mark_ready(self.job, "tim")["prep_done"], True)
		self.assertIn("/farmops/api/job/", job_links.issue_link(self.job, "tim")["url"])


class TheLink(JobCase):
	def test_shown_once_hashed_and_found(self):
		data, token = self.ready_and_link()
		row = frappe.get_doc("Contractor Job Link", data["link"])
		self.assertEqual(row.token_hash, hashlib.sha256(token.encode()).hexdigest())
		self.assertNotIn(token, json.dumps(row.as_dict(), default=str))
		self.assertEqual(job_links.find(token)["name"], data["link"])
		self.assertLessEqual(str(row.expires_at), "2026-11-15 23:59:59", "never outlives the job's end date")

	def test_every_bad_token_is_the_same_none(self):
		data, token = self.ready_and_link()
		for guess in ("", "x", token[:-1] + ("A" if token[-1] != "A" else "B"), "a" * 300):
			self.assertIsNone(job_links.find(guess))
		job_links.revoke(data["link"], "tim", "wrong person")
		self.assertIsNone(job_links.find(token))

	def test_expiry_and_closing(self):
		data, token = self.ready_and_link()
		frappe.db.set_value("Contractor Job Link", data["link"], "expires_at", "2020-01-01 00:00:00")
		self.assertIsNone(job_links.find(token))
		self.assertEqual(frappe.db.get_value("Contractor Job Link", data["link"], "status"), "Expired")
		job_links.extend(data["link"], "tim", days=3)
		self.assertIsNotNone(job_links.find(token))
		job_links.close_job(self.job, "tim")
		self.assertIsNone(job_links.find(token))

	def test_turning_sharing_off_stops_live_links(self):
		_data, token = self.ready_and_link()
		flags.upsert(job_links.FLAG, "Flag", False, company=MAIN, description="off", owner_area="job_links", active=True)
		STORE.commit()
		self.assertIsNone(job_links.find(token))


class OnlyThisJob(JobCase):
	ALLOWED = {"title", "kind", "status", "scope", "start_date", "end_date", "contact", "entrance", "delivery_spot",
	           "gate_notes", "sop_link", "blocks", "hazards", "expected", "actions", "done"}

	def test_the_page_carries_only_this_job(self):
		data = job_links.page_data(self.job)
		self.assertEqual(set(data), self.ALLOWED)
		self.assertEqual(sorted(b["label"] for b in data["blocks"]), ["MC Centerpiece", "Wind Mecine"])
		text = json.dumps(data)
		for leak in ("Pearls", "40-MAIN", "MC-V1", MAIN, "Bing Block - MC", "541-555-0100", "tim@example.test",
		             "Spider Mites", "Pruning", "4200"):
			self.assertNotIn(leak, text, leak)
		self.assertEqual([h["kind"] for h in data["hazards"]], ["valve"], "the one valve inside the job's blocks")
		self.assertIsNone(data["sop_link"])

	def test_the_phone_number_only_when_tim_shows_it(self):
		frappe.db.set_value("Contractor Job", self.job, "show_contact_phone", 1)
		self.assertEqual(job_links.page_data(self.job)["contact"]["phone"], "541-555-0100")


class WhatTheContractorDoes(JobCase):
	def test_arrived_done_and_the_crop_class_prompt(self):
		data, token = self.ready_and_link()
		link = job_links.find(token)
		self.assertEqual(job_links.record_event(link, "Arrived", note="on site 7am", ip="1.2.3.4")["status"], "In Progress")
		done = job_links.record_event(link, "Done", ip="1.2.3.4")
		self.assertEqual(done["status"], "Done")
		alert = frappe.get_doc("Compliance Alert", done["prompts"][0])
		self.assertIn("lease crop class", alert.alert_message)
		self.assertTrue(job_links.record_event(link, "Done")["already"])
		with self.assertRaises(job_links.JobError):
			job_links.record_event(link, "Delivered")

	def test_photos_are_private_capped_and_images_only(self):
		_data, token = self.ready_and_link()
		link = job_links.find(token)
		job_links.attach_photo(link, JPEG, ip="1.2.3.4")
		files = [f for f in STORE.rows("File") if f.get("attached_to_name") == self.job]
		self.assertEqual(len(files), 1)
		self.assertEqual(int(files[0].get("is_private") or 0), 1)
		with self.assertRaises(job_links.JobError):
			job_links.attach_photo(link, b"<html>", ip="1.2.3.4")
		with self.assertRaises(job_links.JobError):
			job_links.attach_photo(link, b"\xff\xd8\xff" + b"0" * (job_links.MAX_PHOTO_BYTES + 1))


class OverHTTP(JobCase):
	def test_page_data_headers_and_one_404(self):
		_data, token = self.ready_and_link()
		page = self.post(f"{PREFIX}/job/{token}", method="GET", credential=False)
		self.assertEqual(page.status_code, 200)
		self.assertTrue(page.headers["Content-Type"].startswith("text/html"))
		for header, want in (("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"),
		                     ("X-Robots-Tag", "noindex, nofollow"), ("X-Frame-Options", "DENY")):
			self.assertEqual(page.headers[header], want)
		self.assertIn("default-src 'none'", page.headers["Content-Security-Policy"])
		body = page.get_data(as_text=True)
		self.assertNotIn("Bing", body, "the HTML shell carries no data")
		data = self.post(f"{PREFIX}/job/{token}/data", method="GET", credential=False)
		self.assertEqual(json.loads(data.get_data(as_text=True))["title"], "Remove the old Bing")
		bad = [self.post(f"{PREFIX}/job/{t}", method="GET", credential=False) for t in ("nope", token + "x")]
		self.assertEqual({(r.status_code, r.get_data(as_text=True)) for r in bad}, {(bad[0].status_code, bad[0].get_data(as_text=True))})
		self.assertEqual(bad[0].status_code, 404)
		views = frappe.get_doc("Contractor Job Link", _data["link"]).view_count
		self.assertEqual(views, 2)

	def test_assets_are_a_fixed_list(self):
		ok = self.post(f"{PREFIX}/job-assets/leaflet.js", method="GET", credential=False)
		self.assertEqual(ok.status_code, 200)
		for path in ("../../hooks.py", "job.html", "vendor/leaflet/LICENSE"):
			self.assertEqual(self.post(f"{PREFIX}/job-assets/{path}", method="GET", credential=False).status_code, 404)

	def test_an_event_over_http(self):
		_data, token = self.ready_and_link()
		answer = self.post(f"{PREFIX}/job/{token}/event", {"event": "Arrived", "name": "Dale"}, credential=False)
		self.assertEqual(json.loads(answer.get_data(as_text=True))["status"], "In Progress")


class FromThePhone(JobCase):
	def test_a_worker_cannot_and_a_manager_shares(self):
		self.be(WORKER)
		with self.assertRaises(frappe.PermissionError):
			mobile_api.list_contractor_jobs()
		set_roles(WORKER, ["Field Worker", "Foreman"])
		self.be(WORKER)
		self.assertEqual([j["name"] for j in mobile_api.list_contractor_jobs()["jobs"]], [self.job])
		with self.assertRaises(frappe.PermissionError):
			mobile_api.create_job_link(job=self.job)
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.share_on()
		self.be(WORKER)
		mobile_api.mark_contractor_job_ready(job=self.job, override_reason="ok")
		answer = mobile_api.create_job_link(job=self.job)
		self.assertIn("/farmops/api/job/", answer["url"])


class Contract(JobCase):
	def check(self, name, value):
		before = v262.HERE
		v262.HERE = HERE
		try:
			v262.check(self, name, value)
		finally:
			v262.HERE = before

	def test_the_phone_answers(self):
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.share_on()
		with mock.patch("secrets.token_urlsafe", lambda n=32: "TOKEN-FOR-THE-FIXTURE"):
			self.be(WORKER)
			mobile_api.mark_contractor_job_ready(job=self.job, override_reason="fixture")
			link = mobile_api.create_job_link(job=self.job)
		link["link"] = "<link>"
		link["expires_at"] = "<expires_at>"
		self.check("create_job_link", link)
		self.check("list_contractor_jobs", mobile_api.list_contractor_jobs())
		answer = mobile_api.get_contractor_job(job=self.job)
		for row in answer["links"]:
			row.update({k: f"<{k}>" for k in ("name", "issued_at", "expires_at") if k in row})
		self.check("get_contractor_job", answer)
