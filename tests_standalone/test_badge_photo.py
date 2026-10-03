# SPDX-License-Identifier: MIT
"""The badge photo, captured as a task (v0.214.0).

docs/design/badge_photo_and_fixed_assets.md, Part A.
"""

import io
import unittest

import frappe

from erpnext_mcp import badge_photo, form_schema, registry, task_templates
from erpnext_mcp.api import badges as desk_badges
from erpnext_mcp.api import mobile as mobile_api

from .fixtures import MAIN, install_hrms
from .harness import STORE, set_roles
from .test_api_mobile import OUTSIDER_EMPLOYEE, WORKER, WORKER_EMPLOYEE, MobileAPITestCase

try:
	from PIL import Image

	HAS_PIL = True
except Exception:  # pragma: no cover
	HAS_PIL = False

CAL = "EMP-CAL"
ON = {f"allow_{name}": 1 for name in ("request_badge_photo", "set_employee_photo")}


def a_photo(width=1200, height=900, with_gps=True, color=(40, 120, 200)) -> bytes:
	"""A landscape JPEG that carries EXIF — a GPS block and a camera make."""
	image = Image.new("RGB", (width, height), color)
	exif = Image.Exif()
	exif[0x010F] = "FarmPhone"  # Make
	if with_gps:
		exif[0x8825] = {1: "N", 2: (45.0, 34.0, 52.5), 3: "W", 4: (121.0, 11.0, 5.9)}
	out = io.BytesIO()
	image.save(out, format="JPEG", exif=exif)
	return out.getvalue()


@unittest.skipUnless(HAS_PIL, "needs Pillow, which Frappe ships")
class BadgePhotoCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		install_hrms()
		from .test_api_mobile import ON as BASE

		self.configure(enabled=1, **{**BASE, **ON})
		STORE.seed(
			"Employee", [{"name": CAL, "employee_name": "Cal Reyes", "company": MAIN, "status": "Active"}]
		)
		self.be("Administrator")
		task_templates.seed_farm_task_templates()
		STORE.commit()

	def a_file(self, name="portrait.jpg", content=None) -> str:
		doc = frappe.get_doc(
			{"doctype": "File", "file_name": name, "is_private": 1, "content": content or a_photo()}
		).insert()
		STORE.commit()
		return doc.name

	def picker(self):
		set_roles(WORKER, ["Field Worker"])
		return self.be()

	def foreman(self):
		set_roles(WORKER, ["Field Worker", "Foreman"])
		return self.be()

	def complete(self, task, file, **answers):
		mobile_api.start_task(task=task)
		return mobile_api.complete_task_via_mobile(
			task=task,
			form_answers={
				"employee": answers.pop("employee", WORKER_EMPLOYEE),
				"photo": [file],
				"consent": True,
				**answers,
			},
			evidence_files=[{"file_token": file, "file_name": "portrait.jpg", "kind": "photo"}],
		)

	def image_of(self, employee) -> bytes:
		url = STORE.get_raw("Employee", employee)["image"]
		name = frappe.db.get_value("File", {"file_url": url}, "name")
		return frappe.get_doc("File", name).get_content()


class TheTemplate(BadgePhotoCase):
	def test_it_is_seeded_in_two_languages_with_a_guided_portrait(self):
		row = STORE.rows("Farm Task Template")
		template = next(r for r in row if r["template_name"] == badge_photo.TEMPLATE)
		self.assertEqual(template["title_es"], "Foto para el gafete")
		fields = {f["key"]: f for f in form_schema.as_fields(template["form_schema"])}
		self.assertEqual(list(fields), ["employee", "photo", "consent", "request_card_print"])
		self.assertEqual((fields["photo"]["guide"], fields["photo"]["camera"]), ("portrait_4x5", "front"))
		self.assertEqual((fields["photo"]["min_count"], fields["photo"]["max_count"]), (1, 1))
		self.assertEqual(fields["employee"]["link"]["doctype"], "Employee")
		self.assertTrue(fields["consent"]["required"])
		self.assertIn("lentes de sol", fields["photo"]["help"]["es"])
		self.assertEqual(form_schema.validate(badge_photo.SEED_TEMPLATE["form_schema"])["errors"], [])
		# Seeded once: an edited template is not put back.
		self.assertIn(badge_photo.TEMPLATE, task_templates.seed_farm_task_templates()["present"])

	def test_the_two_hints_are_a_closed_vocabulary_on_a_photo_only(self):
		bad = [{"key": "p", "type": "photo", "label": {"en": "P"}, "guide": "passport", "camera": "selfie"}]
		codes = [e["message"] for e in form_schema.validate(bad)["errors"]]
		self.assertEqual(len(codes), 2)
		misplaced = [{"key": "t", "type": "text", "label": {"en": "T"}, "guide": "portrait_4x5"}]
		self.assertIn("belongs on a photo field", form_schema.validate(misplaced)["errors"][0]["message"])


class Raising(BadgePhotoCase):
	def test_anybody_asks_for_their_own_and_asking_twice_answers_the_same_task(self):
		self.picker()
		first = mobile_api.request_badge_photo()
		self.assertEqual(
			(first["already"], first["employee"], first["assigned_to"]),
			(False, WORKER_EMPLOYEE, WORKER_EMPLOYEE),
		)
		row = STORE.get_raw("Farm Task", first["task"])
		self.assertEqual((row["subject_doctype"], row["subject_docname"]), ("Employee", WORKER_EMPLOYEE))
		self.assertIn("Ana Ramos", row["task_name"])
		self.assertEqual(first["task_detail"]["name"], first["task"])
		STORE.commit()
		again = mobile_api.request_badge_photo()
		self.assertEqual((again["already"], again["task"]), (True, first["task"]))

	def test_somebody_elses_needs_a_role_and_their_entity(self):
		self.picker()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.request_badge_photo(employee=CAL)
		self.assertIn("only for themselves", str(caught.exception))
		self.foreman()
		raised = mobile_api.request_badge_photo(employee=CAL, assign_to=WORKER_EMPLOYEE)
		self.assertEqual((raised["employee"], raised["assigned_to"]), (CAL, WORKER_EMPLOYEE))
		with self.assertRaises(Exception):
			mobile_api.request_badge_photo(employee=OUTSIDER_EMPLOYEE)

	def test_over_mcp_and_from_the_desk_button(self):
		self.be("Administrator")
		self.assertIn("request_badge_photo", registry.MUTATING_TOOLS)
		self.assertIn("set_employee_photo", registry.MUTATING_TOOLS)
		raised = self.tool_data("request_badge_photo", {"employee": CAL})
		self.assertEqual((raised["already"], raised["has_photo"]), (False, False))
		self.assertTrue(desk_badges.request_badge_photo(employee=CAL)["already"])
		self.assertTrue(badge_photo.seed_desk_button()["created"])
		self.assertEqual(badge_photo.seed_desk_button()["reason"], "already present")
		self.assertIn(
			badge_photo.DESK_METHOD, STORE.get_raw("Client Script", badge_photo.SCRIPT_NAME)["script"]
		)

	def test_onboarding_and_a_photoless_card_request_raise_it_and_the_flag_stops_them(self):
		self.be("Administrator")
		answer = badge_photo.auto_request(CAL)
		self.assertFalse(answer["already"])
		self.assertTrue(badge_photo.auto_request(CAL)["already"])
		frappe.db.set_value("Employee", WORKER_EMPLOYEE, "image", "/private/files/have-one.jpg")
		self.assertIsNone(badge_photo.auto_request(WORKER_EMPLOYEE), "somebody with a photo is not asked")
		self.assertIsNone(badge_photo.auto_request("EMP-NOBODY"), "and it never raises")


class ACardWithNoPhoto(BadgePhotoCase):
	def test_asking_for_a_card_raises_the_photo_task_and_still_queues_the_card(self):
		from erpnext_mcp import card_art, card_print
		from erpnext_mcp.render import qr

		if not (qr.available() and card_art.reportlab_available()):
			self.skipTest("needs a QR encoder and reportlab")
		self.be("Administrator")
		card_print.seed()
		set_roles(WORKER, ["Field Worker", "Foreman", card_print.REQUESTER_ROLE])
		STORE.commit()
		self.be()
		answer = card_print.request(
			WORKER, "Employee ID", WORKER_EMPLOYEE, "00000000-0000-4000-8000-000000000001"
		)
		self.assertTrue(answer["created"])
		self.assertTrue(any("has no photo" in w for w in answer["warnings"]))
		task = answer["badge_photo_task"]
		self.assertEqual((task["employee"], task["already"]), (WORKER_EMPLOYEE, False))
		self.assertEqual(STORE.get_raw("Farm Task", task["task"])["subject_docname"], WORKER_EMPLOYEE)


class Completing(BadgePhotoCase):
	def test_the_photo_becomes_the_employees_cropped_resized_and_stripped(self):
		self.be("Administrator")
		frappe.db.set_value("Employee", WORKER_EMPLOYEE, "image", "/files/old-ana.jpg")
		file = self.a_file()
		self.picker()
		task = mobile_api.request_badge_photo()["task"]
		STORE.commit()
		self.complete(task, file)

		row = STORE.get_raw("Employee", WORKER_EMPLOYEE)
		self.assertTrue(row["image"].startswith("/private/files/badge-photo-EMP-ANA-"))
		image = Image.open(io.BytesIO(self.image_of(WORKER_EMPLOYEE)))
		self.assertEqual((image.size, image.format), ((600, 750), "JPEG"))
		self.assertEqual(dict(image.getexif()), {}, "no camera make, and no GPS")
		self.assertNotIn(0x8825, image.getexif())
		stored = next(r for r in STORE.rows("File") if r.get("file_url") == row["image"])
		self.assertEqual(
			(stored["is_private"], stored["attached_to_doctype"], stored["attached_to_name"]),
			(1, "Employee", WORKER_EMPLOYEE),
		)
		# The source upload is still there, and the change is on the record.
		self.assertTrue(frappe.db.exists("File", file))
		comment = next(r for r in STORE.rows("Comment") if r.get("reference_name") == WORKER_EMPLOYEE)
		self.assertIn("/files/old-ana.jpg", comment["content"])
		self.assertEqual(STORE.get_raw("Farm Task", task)["state"], "Completed")
		# And the card renderer reads it: no more "has no photo".
		from erpnext_mcp import card_print

		self.assertTrue(card_print.file_bytes(row["image"]))

	def test_a_portrait_is_trimmed_top_and_bottom_and_orientation_is_applied_first(self):
		tall = Image.new("RGB", (600, 1200), (10, 200, 10))
		out = io.BytesIO()
		tall.save(out, format="JPEG")
		jpeg, width, height = badge_photo.process(out.getvalue())
		self.assertEqual((width, height, Image.open(io.BytesIO(jpeg)).size), (600, 750, (600, 750)))
		with self.assertRaises(Exception):
			badge_photo.process(b"not a picture")
		tiny = io.BytesIO()
		Image.new("RGB", (60, 80)).save(tiny, format="JPEG")
		with self.assertRaises(Exception) as caught:
			badge_photo.process(tiny.getvalue())
		self.assertIn("too small", str(caught.exception))

	def test_consent_and_the_photo_are_required(self):
		file = self.a_file()
		self.picker()
		task = mobile_api.request_badge_photo()["task"]
		STORE.commit()
		mobile_api.start_task(task=task)
		STORE.commit()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.complete_task_via_mobile(
				task=task,
				form_answers={"employee": WORKER_EMPLOYEE, "photo": [file], "consent": False},
				evidence_files=[{"file_token": file, "file_name": "portrait.jpg", "kind": "photo"}],
			)
		self.assertIn("Consent", str(caught.exception))
		self.assertFalse(STORE.get_raw("Employee", WORKER_EMPLOYEE).get("image"))

	def test_a_picker_may_not_take_somebody_elses_and_a_foreman_may(self):
		file = self.a_file()
		self.foreman()
		task = mobile_api.request_badge_photo(employee=CAL, assign_to=WORKER_EMPLOYEE)["task"]
		STORE.commit()
		self.picker()
		mobile_api.start_task(task=task)
		STORE.commit()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.complete_task_via_mobile(
				task=task,
				form_answers={"employee": CAL, "photo": [file], "consent": True},
				evidence_files=[{"file_token": file, "file_name": "portrait.jpg", "kind": "photo"}],
			)
		self.assertIn("badge photo of EMP-CAL", str(caught.exception))
		self.foreman()
		done = mobile_api.complete_task_via_mobile(
			task=task,
			form_answers={"employee": CAL, "photo": [file], "consent": True},
			evidence_files=[{"file_token": file, "file_name": "portrait.jpg", "kind": "photo"}],
		)
		self.assertTrue(STORE.get_raw("Employee", CAL)["image"])
		self.assertTrue(done)

	def test_a_picture_that_will_not_decode_is_reported_and_the_task_still_closes(self):
		file = self.a_file(content=b"this is not a jpeg")
		self.picker()
		task = mobile_api.request_badge_photo()["task"]
		STORE.commit()
		self.complete(task, file)
		self.assertEqual(STORE.get_raw("Farm Task", task)["state"], "Completed")
		self.assertFalse(STORE.get_raw("Employee", WORKER_EMPLOYEE).get("image"))


class SettingItDirectly(BadgePhotoCase):
	def test_set_employee_photo_does_the_same_five_things(self):
		file = self.a_file()
		self.be("Administrator")
		answer = self.tool_data("set_employee_photo", {"employee": CAL, "file": file})
		self.assertEqual((answer["width"], answer["height"], answer["previous_image"]), (600, 750, None))
		self.assertEqual(STORE.get_raw("Employee", CAL)["image"], answer["image"])
		second = self.tool_data("set_employee_photo", {"employee": CAL, "file_url": answer["image"]})
		self.assertEqual(second["previous_image"], answer["image"])
		self.assertTrue(frappe.db.exists("File", answer["file"]), "the previous photo is kept")
		self.assertIn(
			"is not a File",
			self.tool_error("set_employee_photo", {"employee": CAL, "file_url": "/files/nope.jpg"}),
		)


class FT_2026_10_00001(BadgePhotoCase):
	"""v0.216.1. The task a foreman raised from the template on the Work screen:
	no subject, no form answers, and the phone filed a before photo, an after
	photo and a signature through the generic completion screen. The handler had
	nobody and no picture, so Employee.image was never set."""

	def raise_from_template(self):
		self.be("Administrator")
		from erpnext_mcp.tools import tasktemplates

		task = tasktemplates.create_task_from_template(
			{"template": badge_photo.TEMPLATE, "assigned_to": WORKER_EMPLOYEE}, origin="foreman_dispatch"
		).data["name"]
		STORE.commit()
		return task

	def complete_generically(self, task, evidence):
		mobile_api.start_task(task=task)
		return mobile_api.complete_task_via_mobile(task=task, evidence_files=evidence)

	def test_the_after_photo_becomes_the_assignees_badge_photo(self):
		before = self.a_file("FT_photo_before_A.jpg")
		after = self.a_file("FT_photo_after_B.jpg", a_photo(color=(10, 200, 10)))
		signature = self.a_file("FT_signature_C.png")
		task = self.raise_from_template()
		self.picker()
		answer = self.complete_generically(
			task,
			[
				{
					"file_token": before,
					"file_name": "FT_photo_before_A.jpg",
					"kind": "photo",
					"phase": "before",
				},
				{"file_token": after, "file_name": "FT_photo_after_B.jpg", "kind": "photo", "phase": "after"},
				{"file_token": signature, "file_name": "FT_signature_C.png", "kind": "signature"},
			],
		)
		self.assertTrue(answer)
		self.assertTrue(STORE.get_raw("Employee", WORKER_EMPLOYEE).get("image"), "Employee.image was set")
		image = Image.open(io.BytesIO(self.image_of(WORKER_EMPLOYEE))).convert("RGB")
		self.assertGreater(image.getpixel((300, 375))[1], 150, "the AFTER photo, not the before one")
		# The task now names its subject, so it shows under the Employee.
		row = STORE.get_raw("Farm Task", task)
		self.assertEqual((row["subject_doctype"], row["subject_docname"]), ("Employee", WORKER_EMPLOYEE))

	def test_raised_from_the_template_it_names_the_person(self):
		task = self.raise_from_template()
		row = STORE.get_raw("Farm Task", task)
		self.assertEqual((row["subject_doctype"], row["subject_docname"]), ("Employee", WORKER_EMPLOYEE))
		self.assertTrue(row["task_name"].startswith("Badge photo — "))

	def test_no_photograph_at_all_is_refused_before_anything_is_written(self):
		task = self.raise_from_template()
		self.picker()
		STORE.commit()
		with self.assertRaises(Exception) as caught:
			self.complete_generically(task, [])
		self.assertIn("no photograph came with it", str(caught.exception))
		self.assertNotEqual(STORE.get_raw("Farm Task", task)["state"], "Completed")

	def test_the_form_portrait_still_wins_over_evidence(self):
		portrait_file = self.a_file("portrait.jpg", a_photo(color=(10, 200, 10)))
		other = self.a_file("FT_photo_after_X.jpg")
		self.assertEqual(
			badge_photo.portrait({"photo": [portrait_file]}, [{"file": other, "evidence_type": "Photo"}]),
			portrait_file,
		)

	def test_a_signature_is_never_the_portrait(self):
		self.assertIsNone(badge_photo.portrait({}, [{"file": "S", "evidence_type": "Signature"}]))

	def test_a_failure_is_written_on_the_task_and_the_employee(self):
		task = {"name": "FT-X", "template": badge_photo.TEMPLATE, "assigned_to": WORKER_EMPLOYEE}
		STORE.seed("Farm Task", [{"name": "FT-X", "task_name": "Badge photo", "state": "Completed"}])
		answer = badge_photo.on_completed(task, {}, WORKER_EMPLOYEE, "Administrator", evidence=[])
		self.assertIn("no photograph", answer["error"])
		texts = [row.get("content") or "" for row in STORE.rows("Comment")]
		self.assertTrue(any("Badge photo was NOT set from FT-X" in text for text in texts))


class EmployeeConnections(BadgePhotoCase):
	def test_farm_tasks_appear_on_the_employee_form_once(self):
		self.assertTrue(badge_photo.seed_employee_connection()["created"])
		self.assertEqual(badge_photo.seed_employee_connection()["reason"], "already present")
		row = next(r for r in STORE.rows("DocType Link") if r.get("parent") == "Employee")
		self.assertEqual(
			(row["link_doctype"], row["link_fieldname"], row["custom"]), ("Farm Task", "assigned_to", 1)
		)
