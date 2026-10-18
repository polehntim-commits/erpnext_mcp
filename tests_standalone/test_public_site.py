# SPDX-License-Identifier: MIT
"""The public website: built over MCP, served under one prefix, guarded, private by default. v0.335.0.

EIGHT CLAIMS.

1. `Lifecycle` — drafts, preview, publish (its own switch, default OFF), rollback,
   unpublish; nothing is served until a person ticks Public Site Enabled.
2. `Validation` — a bad prefix, slug, field name, routing or missing company is refused.
3. `Privacy` — only the public company's openings are shown; no other company's
   name reaches a page; media is served only for files the published pages name.
4. `Guards` — honeypot, time token, forged token, rate limits, image type and size.
5. `Submissions` — a report, a visit and an application are stored and routed.
6. `Visitors` — check-in hands out a code, check-out by it, acknowledgement required.
7. `Building` — pages, blocks, crew entries, form fields, categories, routing, jobs,
   QR codes and triage, all over MCP.
8. `TheRenderer` — answers only on the live prefix, only while enabled.
"""

import io
import json
import unittest

import frappe

from erpnext_mcp import public_site, public_site_renderer, registry

from .fixtures import EMPLOYEES, MAIN, OTHER, SeededTestCase, install_hrms
from .harness import ROLES, STORE, set_roles

TOOLS = [
	name
	for name in registry.TOOLS
	if "public_site" in name
	or name in ("set_crew_info_entry", "create_public_job_opening", "close_public_job_opening")
]
ON = {f"allow_{name}": 1 for name in TOOLS}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
T0 = 1_800_000_000.0


class PublicSiteCase(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.configure(public_site_enabled=1, **ON)
		self._roles = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(lambda: (ROLES.clear(), ROLES.update(self._roles)))
		set_roles("Administrator", ["System Manager"])
		install_hrms()
		STORE.commit()

	def build(self, **extra):
		args = {
			"company": MAIN,
			"brand_name": "Public Brand",
			"base_url": "https://public.example",
			"qr_sources": [{"tag": "gate", "label": "Front gate"}],
			**extra,
		}
		data = self.tool_data("configure_public_site", args)
		return data

	def publish(self, **extra):
		return self.tool_data("publish_public_site", {"change_note": "first publish of the site", **extra})

	def live(self, path="", **kwargs):
		page = public_site.render(path, **kwargs)
		STORE.commit()  # a web request commits at its end
		return page

	def post(self, path, values, *, age=10, ip="10.0.0.1", files=None, form="report"):
		token = public_site.form_token(form, T0)
		page = public_site.render(
			path,
			method="POST",
			form={"_token": token, "lang": "en", **values},
			files=files,
			ip=ip,
			now=T0 + age,
		)
		STORE.commit()
		return page

	def entries(self, **filters):
		return frappe.db.get_all(public_site.ENTRY, filters=filters, fields=["*"])


class Lifecycle(PublicSiteCase):
	def test_configure_drafts_the_site_and_neutral_defaults(self):
		data = self.build()
		self.assertEqual(data["site"]["status"], "Draft")
		self.assertEqual(
			sorted(data["defaults_drafted"]),
			sorted(
				[
					"page_home",
					"page_story",
					"page_contact",
					"page_crew",
					"form_report",
					"form_visit",
					"form_apply",
				]
			),
		)
		self.assertEqual(public_site.bodies(), {})
		self.assertEqual(public_site.live_prefix(), "")

	def test_publish_makes_it_live_but_only_while_enabled(self):
		self.build()
		data = self.publish()
		self.assertEqual(data["refused"], [])
		self.assertEqual(len(data["published"]), 8)
		self.assertEqual(public_site.live_prefix(), "farm")
		self.configure(public_site_enabled=0, **ON)
		self.assertEqual(public_site.live_prefix(), "")

	def test_publish_is_off_by_default_and_never_switches_the_site_on(self):
		self.configure(
			public_site_enabled=0, **{k: v for k, v in ON.items() if k != "allow_publish_public_site"}
		)
		self.build()
		error = self.tool_error("publish_public_site", {"change_note": "first publish of the site"})
		self.assertIn("switched off", error)
		self.assertEqual(public_site.bodies(), {})
		self.configure(public_site_enabled=0, **ON)
		self.publish()
		self.assertFalse(public_site.enabled())
		self.assertEqual(public_site.live_prefix(), "")

	def test_the_switch_ships_off(self):
		meta = {f["fieldname"]: f for f in frappe.get_meta("ERPNext MCP Settings").fields}
		self.assertEqual(meta["public_site_enabled"]["default"], "0")
		self.assertEqual(meta["allow_publish_public_site"]["default"], "0")
		for name in TOOLS:
			with self.subTest(tool=name):
				expected = "0" if registry.TOOLS[name]["mutating"] else "1"
				self.assertEqual(meta[f"allow_{name}"]["default"], expected)

	def test_publish_without_a_company_is_refused_for_that_key(self):
		self.tool_data("configure_public_site", {"brand_name": "X"})
		data = self.publish()
		self.assertIn("site", [row["key"] for row in data["refused"]])
		self.assertEqual(public_site.live_prefix(), "")

	def test_edits_land_in_one_draft_and_rollback_goes_back(self):
		self.build()
		self.publish()
		self.tool_data(
			"set_public_site_page", {"slug": "story", "title": {"en": "Story v2", "es": "Historia 2"}}
		)
		self.tool_data("set_public_site_page", {"slug": "story", "order": 11})
		self.assertIn("Story v2", self.tool_data("preview_public_site", {"path": "story"})["html"])
		self.assertNotIn("Story v2", self.live("story").body)
		self.publish(keys=["page_story"])
		self.assertIn("Story v2", self.live("story").body)
		self.tool_data("rollback_public_site", {"key": "page_story"})
		self.assertNotIn("Story v2", self.live("story").body)

	def test_unpublish_takes_a_page_off(self):
		self.build()
		self.publish()
		self.assertEqual(self.live("story").status, 200)
		self.tool_data("unpublish_public_site_page", {"slug": "story"})
		self.assertEqual(self.live("story").status, 404)
		self.assertIn("cannot", self.tool_error("unpublish_public_site_page", {"slug": "home"}))


class Validation(PublicSiteCase):
	def test_a_reserved_prefix_is_refused(self):
		for prefix in ("app", "api", "files", "login"):
			with self.subTest(prefix=prefix):
				self.assertIn(
					"route_prefix", self.tool_error("configure_public_site", {"route_prefix": prefix})
				)

	def test_an_unknown_company_is_refused(self):
		self.assertIn("no Company", self.tool_error("configure_public_site", {"company": "Nobody Ltd"}))

	def test_a_bad_slug_or_block_is_refused(self):
		self.build()
		self.assertIn("slug", self.tool_error("set_public_site_page", {"slug": "Bad Slug"}))
		self.assertIn("taken", self.tool_error("set_public_site_page", {"slug": "jobs"}))
		error = self.tool_error(
			"set_public_site_page",
			{"slug": "story", "blocks": [{"type": "image", "image": "https://evil/x.png"}]},
		)
		self.assertIn("File url", error)

	def test_a_reserved_field_name_is_refused(self):
		self.build()
		for name in ("website", "_token", "lang"):
			with self.subTest(field=name):
				error = self.tool_error(
					"set_public_site_form",
					{"form": "report", "field_op": "add", "field": name, "definition": {"label": "x"}},
				)
				self.assertIn("Nothing was saved", error)

	def test_farm_task_routing_needs_a_reporter(self):
		self.build()
		error = self.tool_error(
			"set_public_site_form", {"form": "report", "routing": {"create": "farm_task"}}
		)
		self.assertIn("reporter_employee", error)

	def test_validate_names_the_applicant_target(self):
		body = public_site.default_forms(MAIN)["form_apply"]
		body["fields"] = [f for f in body["fields"] if f.get("target") != "applicant_name"]
		self.assertTrue(
			any("applicant_name" in e for e in public_site.validate(body, key="form_apply")["errors"])
		)


class Privacy(PublicSiteCase):
	def opening(self, title, company, publish=1, status="Open"):
		doc = frappe.get_doc(
			{
				"doctype": "Job Opening",
				"job_title": title,
				"company": company,
				"designation": "Picker",
				"status": status,
				"publish": publish,
			}
		)
		doc.insert(ignore_permissions=True)
		STORE.commit()
		return doc.name

	def test_only_the_public_companys_openings_are_shown(self):
		self.build()
		self.publish()
		mine = self.opening("Orchard picker", MAIN)
		other = self.opening("Private role", OTHER)
		self.opening("Closed role", MAIN, status="Closed")
		self.opening("Unpublished role", MAIN, publish=0)
		html = self.live("jobs").body
		self.assertIn("Orchard picker", html)
		for hidden in ("Private role", "Closed role", "Unpublished role"):
			self.assertNotIn(hidden, html)
		self.assertEqual(self.live(f"jobs/{other}").status, 404)
		self.assertEqual(self.live(f"jobs/{other}/apply").status, 404)
		self.assertEqual(self.live(f"jobs/{mine}").status, 200)

	def test_no_other_company_name_reaches_any_page(self):
		self.build()
		self.publish()
		self.opening("Private role", OTHER)
		for path in ("", "story", "contact", "crew", "jobs", "report", "visit", "visit/out", "nope"):
			with self.subTest(path=path):
				self.assertNotIn(OTHER, self.live(path).body)

	def test_the_default_site_is_not_bound_to_any_company(self):
		self.assertEqual(public_site.default_site()["company"], "")
		self.assertEqual(public_site.open_jobs({"site": public_site.default_site()}), [])

	def test_media_serves_only_files_a_published_page_names(self):
		logo = frappe.get_doc({"doctype": "File", "file_name": "logo.png", "content": PNG, "is_private": 0})
		logo.insert(ignore_permissions=True)
		secret = frappe.get_doc(
			{"doctype": "File", "file_name": "payroll.png", "content": PNG, "is_private": 1}
		)
		secret.insert(ignore_permissions=True)
		text = frappe.get_doc(
			{"doctype": "File", "file_name": "notes.txt", "content": b"hello", "is_private": 0}
		)
		text.insert(ignore_permissions=True)
		STORE.commit()
		self.build(logo=logo.file_url)
		self.tool_data(
			"set_public_site_page",
			{"slug": "story", "block_op": "add", "block": {"type": "image", "image": text.file_url}},
		)
		self.publish()
		served = self.live("media", form={"u": logo.file_url})
		self.assertEqual((served.status, served.content_type), (200, "image/png"))
		self.assertEqual(self.live("media", form={"u": secret.file_url}).status, 404)
		self.assertEqual(self.live("media", form={"u": text.file_url}).status, 404, "not an image")

	def test_the_default_content_names_nothing_of_this_install(self):
		blob = json.dumps(public_site.all_defaults())
		for word in ("Constancy", "Orchard Meadow", "OML", "tailscale", ".ts.net"):
			self.assertNotIn(word, blob)


class Guards(PublicSiteCase):
	def setUp(self):
		super().setUp()
		self.build()
		self.publish()

	def report(self, **kwargs):
		return self.post("report", {"category": "safety", "description": "Broken ladder"}, **kwargs)

	def test_a_good_report_is_stored(self):
		page = self.report()
		self.assertEqual(page.status, 200, page.body)
		rows = self.entries(entry_type="Report")
		self.assertEqual(len(rows), 1)
		self.assertEqual(
			(rows[0]["category"], rows[0]["company"], rows[0]["status"]), ("safety", MAIN, "New")
		)

	def test_the_honeypot_thanks_and_drops(self):
		page = self.post("report", {"category": "safety", "description": "spam", "website": "http://x"})
		self.assertEqual(page.status, 200)
		self.assertEqual(self.entries(entry_type="Report"), [])

	def test_too_fast_forged_and_stale_tokens_are_refused(self):
		self.assertIn("too quickly", self.report(age=1).body)
		forged = public_site.render(
			"report",
			method="POST",
			now=T0 + 10,
			form={"_token": f"{int(T0)}.deadbeef", "category": "safety", "description": "x"},
		)
		self.assertIn("not loaded from this site", forged.body)
		self.assertIn("expired", self.report(age=7300).body)
		other_form = public_site.render(
			"report",
			method="POST",
			now=T0 + 10,
			form={"_token": public_site.form_token("visit", T0), "category": "safety", "description": "x"},
		)
		self.assertEqual(other_form.status, 400)
		self.assertEqual(self.entries(entry_type="Report"), [])

	def test_the_per_visitor_limit(self):
		for _ in range(5):
			self.assertEqual(self.report().status, 200)
		blocked = self.report()
		self.assertEqual(blocked.status, 400)
		self.assertIn("too many", blocked.body)
		self.assertEqual(self.report(ip="10.0.0.2").status, 200, "another visitor is not blocked")

	def test_a_photo_must_be_an_image_under_the_cap(self):
		self.assertEqual(self.report(files={"photo": io.BytesIO(PNG)}).status, 200)
		self.assertTrue(self.entries(entry_type="Report")[0]["photo"])
		self.assertIn("JPEG, PNG or WebP", self.report(files={"photo": io.BytesIO(b"<?php evil")}).body)
		big = PNG + b"\x00" * (5 * 1024 * 1024)
		self.assertIn("over 5 MB", self.report(files={"photo": io.BytesIO(big)}).body)

	def test_the_upload_is_read_only_to_the_cap(self):
		class Upload:
			asked = None

			def read(self, size=-1):
				Upload.asked = size
				return PNG

		public_site._photo_bytes({"photo": Upload()}, 100)
		self.assertEqual(Upload.asked, 101)

	def test_a_select_value_must_be_a_choice(self):
		page = self.post("report", {"category": "<script>", "description": "x"})
		self.assertEqual(page.status, 400)
		self.assertNotIn("<script>", page.body)

	def test_the_form_carries_the_traps(self):
		html = self.live("report").body
		self.assertIn('name="website"', html)
		self.assertIn('name="_token"', html)


class Submissions(PublicSiteCase):
	def setUp(self):
		super().setUp()
		self.build()

	def test_a_report_can_file_a_farm_task(self):
		self.tool_data(
			"set_public_site_form",
			{
				"form": "report",
				"routing": {"create": "farm_task", "reporter_employee": EMPLOYEES[0], "task_type": "Repair"},
			},
		)
		self.publish()
		page = self.post("report", {"category": "equipment", "description": "Flat tyre", "location": "Shed"})
		self.assertEqual(page.status, 200, page.body)
		row = self.entries(entry_type="Report")[0]
		self.assertEqual(row["linked_doctype"], "Farm Task", row.get("notes"))
		self.assertTrue(frappe.db.exists("Farm Task", row["linked_document"]))

	def test_an_intake_report_is_promoted_by_triage(self):
		self.publish()
		self.post("report", {"category": "water", "description": "Leak"})
		name = self.entries(entry_type="Report")[0]["name"]
		error = self.tool_error("update_public_site_entry", {"name": name, "promote": True})
		self.assertIn("reporter_employee", error)
		data = self.tool_data(
			"update_public_site_entry", {"name": name, "promote": True, "reporter_employee": EMPLOYEES[0]}
		)
		self.assertEqual(data["promoted"]["doctype"], "Farm Task")
		again = self.tool_data("update_public_site_entry", {"name": name, "promote": True})
		self.assertTrue(again["promoted"]["already"])

	def test_an_application_with_an_email_becomes_a_job_applicant(self):
		self.publish()
		job = frappe.get_doc(
			{
				"doctype": "Job Opening",
				"job_title": "Picker",
				"company": MAIN,
				"designation": "Picker",
				"status": "Open",
				"publish": 1,
			}
		)
		job.insert(ignore_permissions=True)
		STORE.commit()
		page = self.post(
			f"jobs/{job.name}/apply",
			{"name": "Ana", "phone": "555-123-4567", "email": "ana@example.com", "experience": "Pruning"},
			form="apply",
		)
		self.assertEqual(page.status, 200, page.body)
		applicant = frappe.db.get_all("Job Applicant", fields=["*"])[0]
		self.assertEqual((applicant["applicant_name"], applicant["job_title"]), ("Ana", job.name))
		self.assertEqual(applicant["cover_letter"], "Pruning")
		entry = self.entries(entry_type="Application")[0]
		self.assertEqual((entry["linked_doctype"], entry["job_opening"]), ("Job Applicant", job.name))

	def test_a_phone_only_application_stays_an_entry(self):
		self.publish()
		job = frappe.get_doc(
			{
				"doctype": "Job Opening",
				"job_title": "Picker",
				"company": MAIN,
				"designation": "Picker",
				"status": "Open",
				"publish": 1,
			}
		)
		job.insert(ignore_permissions=True)
		STORE.commit()
		self.post(f"jobs/{job.name}/apply", {"name": "Luis", "phone": "555-765-4321"}, form="apply")
		self.assertEqual(frappe.db.get_all("Job Applicant"), [])
		self.assertIn("No email", self.entries(entry_type="Application")[0]["notes"])

	def test_notify_users_get_a_todo(self):
		STORE.seed("User", [{"name": "triage@example.com", "enabled": 1}])
		STORE.commit()
		self.tool_data(
			"set_public_site_form",
			{"form": "report", "routing": {"notify_users": ["triage@example.com"], "notify_roles": []}},
		)
		self.publish()
		self.post("report", {"category": "safety", "description": "x"})
		todos = frappe.db.get_all(
			"ToDo", filters={"reference_type": public_site.ENTRY}, fields=["allocated_to"]
		)
		self.assertEqual([t["allocated_to"] for t in todos], ["triage@example.com"])

	def test_a_switched_off_module_is_not_served(self):
		self.tool_data("configure_public_site", {"modules": {"report": False}, "seed_defaults": False})
		self.publish()
		self.assertEqual(self.live("report").status, 404)
		self.assertEqual(self.post("report", {"category": "safety", "description": "x"}).status, 404)
		self.assertNotIn("Report a problem", self.live("").body)


class Visitors(PublicSiteCase):
	def setUp(self):
		super().setUp()
		self.build()
		self.publish()

	def check_in(self, **extra):
		values = {"name": "Inspector", "purpose": "inspection", "acknowledged": "1", **extra}
		return self.post("visit", values, form="visit")

	def test_check_in_and_out_by_code(self):
		page = self.check_in()
		self.assertEqual(page.status, 200, page.body)
		row = self.entries(entry_type="Visit")[0]
		self.assertTrue(row["check_in"] and row["acknowledged"] and row["acknowledgement_text"])
		code = page.body.split('<p class="code">')[1].split("<")[0]
		self.assertNotIn(code, json.dumps(row), "only the code's hash is stored")
		self.assertEqual(
			public_site.render("visit/out", method="POST", form={"code": "WRONGCODE"}).status, 400
		)
		out = public_site.render("visit/out", method="POST", form={"code": code.lower()})
		STORE.commit()
		self.assertEqual(out.status, 200)
		self.assertTrue(self.entries(entry_type="Visit")[0]["check_out"])

	def test_the_acknowledgement_is_required(self):
		page = self.check_in(acknowledged="")
		self.assertEqual(page.status, 400)
		self.assertEqual(self.entries(entry_type="Visit"), [])

	def test_on_site_lists_who_has_not_left_and_triage_checks_out(self):
		self.check_in()
		listed = self.tool_data("list_public_site_entries", {"on_site": True})
		self.assertEqual(listed["count"], 1)
		self.tool_data("update_public_site_entry", {"name": listed["entries"][0]["name"], "check_out": True})
		self.assertEqual(self.tool_data("list_public_site_entries", {"on_site": True})["count"], 0)

	def test_scans_are_counted_per_known_source_per_day(self):
		for _ in range(3):
			self.live("", form={"src": "gate"})
		self.live("", form={"src": "unknown"})
		rows = self.entries(entry_type="Scan")
		self.assertEqual([(r["source"], r["hits"]) for r in rows], [("gate", 3)])

	def test_scans_do_not_count_toward_the_rate_limit(self):
		for _ in range(10):
			self.live("", form={"src": "gate"})
		self.assertEqual(self.check_in().status, 200)


class Building(PublicSiteCase):
	def setUp(self):
		super().setUp()
		self.build()

	def test_a_new_page_needs_no_code(self):
		self.tool_data(
			"set_public_site_page",
			{
				"slug": "harvest",
				"title": {"en": "Harvest", "es": "Cosecha"},
				"blocks": [{"type": "text", "text": {"en": "Apples", "es": "Manzanas"}}],
			},
		)
		self.publish()
		self.assertIn("Apples", self.live("harvest").body)
		self.assertIn("Manzanas", self.live("harvest", form={"lang": "es"}).body)
		self.assertIn("Harvest", self.live("").body, "in the menu")

	def test_block_operations(self):
		slug = {"slug": "story"}
		self.tool_data(
			"set_public_site_page",
			{**slug, "block_op": "add", "block": {"type": "heading", "text": "Roots"}, "index": 0},
		)
		self.tool_data(
			"set_public_site_page", {**slug, "block_op": "update", "index": 0, "block": {"text": "Our roots"}}
		)
		self.tool_data("set_public_site_page", {**slug, "block_op": "move", "index": 0, "to": 1})
		blocks = public_site.bodies(drafts=True)["page_story"]["blocks"]
		self.assertEqual([b["type"] for b in blocks], ["text", "heading"])
		self.assertEqual(blocks[1]["text"]["en"], "Our roots")
		self.tool_data("set_public_site_page", {**slug, "block_op": "remove", "index": 1})
		self.assertEqual(len(public_site.bodies(drafts=True)["page_story"]["blocks"]), 1)
		self.assertIn(
			"index", self.tool_error("set_public_site_page", {**slug, "block_op": "remove", "index": 9})
		)

	def test_crew_entries(self):
		self.tool_data(
			"set_crew_info_entry",
			{
				"entry": "parking",
				"title": {"en": "Parking", "es": "Estacionamiento"},
				"text": {"en": "Park by the shop.", "es": "Estacione junto al taller."},
			},
		)
		self.tool_data(
			"set_crew_info_entry", {"entry": "parking", "title": "Parking", "text": "Park by the barn."}
		)
		self.tool_data("set_crew_info_entry", {"entry": "payday", "op": "remove"})
		blocks = public_site.bodies(drafts=True)["page_crew"]["blocks"]
		entries = [b.get("entry") for b in blocks]
		self.assertEqual(entries.count("parking"), 2)
		self.assertNotIn("payday", entries)
		self.assertIn("Park by the barn.", json.dumps(blocks))
		self.assertIn(
			"no crew entry", self.tool_error("set_crew_info_entry", {"entry": "nope", "op": "remove"})
		)

	def test_form_fields_categories_and_order(self):
		form = {"form": "report"}
		self.tool_data(
			"set_public_site_form",
			{
				**form,
				"field_op": "add",
				"field": "block",
				"index": 0,
				"definition": {"type": "text", "label": {"en": "Block", "es": "Bloque"}},
			},
		)
		self.tool_data("set_public_site_form", {**form, "field_op": "move", "field": "block", "to": 2})
		self.tool_data("set_public_site_form", {**form, "field_op": "remove", "field": "phone"})
		self.tool_data(
			"set_public_site_form",
			{**form, "categories": [{"value": "fence", "label": {"en": "Fence down", "es": "Cerca caída"}}]},
		)
		body = public_site.bodies(drafts=True)["form_report"]
		names = [f["name"] for f in body["fields"]]
		self.assertEqual(names[2], "block")
		self.assertNotIn("phone", names)
		self.assertEqual(body["fields"][names.index("category")]["options"][0]["value"], "fence")
		self.assertIn(
			"Cerca caída", self.tool_data("preview_public_site", {"path": "report", "lang": "es"})["html"]
		)

	def test_preview_and_list(self):
		data = self.tool_data("preview_public_site", {"path": ""})
		self.assertEqual(data["status"], 200)
		self.assertIn("Public Brand", data["html"])
		live = self.tool_data("preview_public_site", {"path": "", "drafts": False})
		self.assertNotIn("Public Brand", live["html"])
		pages = self.tool_data("list_public_site_pages")
		self.assertEqual({p["slug"] for p in pages["pages"]}, {"home", "story", "contact", "crew"})
		self.assertFalse(any(p["live"] for p in pages["pages"]))
		site = self.tool_data("get_public_site")
		self.assertEqual(site["funnel_paths"], ["/farm", "/farm/*"])
		self.assertEqual(site["public_url"], "https://public.example/farm")

	def test_jobs_are_created_only_for_the_public_company(self):
		data = self.tool_data(
			"create_public_job_opening",
			{
				"job_title": "Tractor driver",
				"designation": "Picker",
				"description": {"en": "Drive", "es": "Manejar"},
			},
		)
		self.assertEqual(data["company"], MAIN)
		error = self.tool_error(
			"create_public_job_opening", {"job_title": "X", "designation": "Picker", "company": OTHER}
		)
		self.assertIn("not the public site's company", error)
		self.publish()
		self.assertIn("Tractor driver", self.live("jobs").body)
		self.tool_data("close_public_job_opening", {"name": data["name"]})
		self.assertNotIn("Tractor driver", self.live("jobs").body)

	def test_qr_codes(self):
		from erpnext_mcp.render import qr

		if not qr.available():
			self.skipTest("no QR encoder in this environment")
		data = self.tool_data("generate_public_site_qr", {"path": "visit", "src": "gate"})
		self.assertEqual(data["url"], "https://public.example/farm/visit?src=gate")
		self.assertTrue(data["png_base64"])
		self.assertIn(
			"not one of the configured", self.tool_error("generate_public_site_qr", {"src": "nowhere"})
		)
		try:
			import reportlab  # noqa: F401
		except ImportError:
			return
		for fmt in ("card_pdf", "sign_pdf"):
			with self.subTest(format=fmt):
				self.assertGreater(
					self.tool_data("generate_public_site_qr", {"format": fmt})["pdf_bytes"], 500
				)

	def test_qr_needs_a_base_url(self):
		self.tool_data("configure_public_site", {"base_url": "", "seed_defaults": False})
		body = public_site.bodies(drafts=True)["site"]
		body["base_url"] = ""
		from erpnext_mcp import phone_config

		phone_config.save_draft(public_site.KIND, "site", body, "clear base", authored_by="AI-proposed")
		self.assertIn("base_url", self.tool_error("generate_public_site_qr", {}))

	def test_entry_triage(self):
		self.publish()
		self.post("report", {"category": "safety", "description": "x"})
		name = self.tool_data("list_public_site_entries", {"entry_type": "report"})["entries"][0]["name"]
		self.tool_data("update_public_site_entry", {"name": name, "status": "Spam", "notes": "bot"})
		got = self.tool_data("get_public_site_entry", {"name": name})
		self.assertEqual((got["status"], got["notes"], got["answers"]["description"]), ("Spam", "bot", "x"))
		self.assertIn(
			"status is", self.tool_error("update_public_site_entry", {"name": name, "status": "Gone"})
		)


class TheRenderer(PublicSiteCase):
	def test_answers_only_on_the_live_prefix(self):
		self.assertFalse(public_site_renderer.PublicSitePage("farm").can_render())
		self.build()
		self.publish()
		for path, expected in (
			("farm", True),
			("farm/report", True),
			("farmland", False),
			("app/farm", False),
			("", False),
		):
			with self.subTest(path=path):
				self.assertEqual(public_site_renderer.PublicSitePage(path).can_render(), expected)
		self.configure(public_site_enabled=0, **ON)
		self.assertFalse(public_site_renderer.PublicSitePage("farm").can_render())

	def test_registered_as_a_page_renderer(self):
		from erpnext_mcp import hooks

		self.assertIn("erpnext_mcp.public_site_renderer.PublicSitePage", hooks.page_renderer)

	def test_the_public_entry_has_no_guest_permission(self):
		path = public_site.__file__.replace(
			"public_site.py", "erpnext_mcp/doctype/public_site_entry/public_site_entry.json"
		)
		with open(path) as handle:
			meta = json.load(handle)
		self.assertNotIn("Guest", {p["role"] for p in meta["permissions"]})


if __name__ == "__main__":
	unittest.main()
