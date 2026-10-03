# SPDX-License-Identifier: MIT
"""The badge photo, captured as a task. v0.214.0.

docs/design/badge_photo_and_fixed_assets.md, Part A.

A card with no photograph prints initials, and until now getting a photograph
onto an Employee meant somebody at a desk attaching a file. This makes it a
piece of work like any other: a seeded template ("Badge photo") whose form asks
for the person, one portrait and their consent; a request that raises it — by
hand, at onboarding, or when a card is asked for and there is no photo — and a
completion handler that turns the answer into `Employee.image`.

WHAT THE HANDLER DOES TO THE PICTURE. Cropped to 4:5 about its centre, resized
to 600 x 750, re-encoded as JPEG with NO metadata: a phone photograph carries
the GPS position it was taken at and the handset's serial, and neither belongs
on a personnel record. Stored as a PRIVATE file on the Employee; the card
renderer reads a File by its row, so privacy costs the card nothing.

THE PREVIOUS PHOTO IS KEPT. Its File is not deleted, and a comment on the
Employee names the old file, the new one and who changed it.

NOTHING HERE RAISES INTO A COMPLETION. The work — a person stood still for a
photograph — is done by the time the handler runs; a picture that will not
decode is reported on the answer and the task still closes.
"""

from __future__ import annotations

import io
import json

import frappe

from . import compat
from .errors import ToolError

EMPLOYEE = "Employee"
FARM_TASK = "Farm Task"
TEMPLATE = "Badge photo"
FLAG_AUTO = "badge_photo_auto_request"

WIDTH, HEIGHT = 600, 750

#: Who may raise or complete a badge photo for SOMEBODY ELSE, and set one directly.
ROLES = ("System Manager", "HR Manager", "HR User", "Farm Manager", "Foreman")

OPEN_STATES = ("Draft", "Available", "Claimed", "In-Progress", "Paused")


def _t(en: str, es: str) -> dict:
	return {"en": en, "es": es}


SEED_TEMPLATE = {
	"template_name": TEMPLATE,
	"title_es": "Foto para el gafete",
	"task_type": "Other",
	"description": "One portrait for the ID card. Completing it puts the photo on the Employee record.",
	"skill_required": "",
	"estimated_duration_minutes": 5,
	"dispatch_mode": "Either",
	"default_urgency": "Normal",
	# The portrait IS the photo this contract asks for: the phone counts a photo
	# taken on the form toward it. v0.216.1 (FT-2026-10-00001): app 0.27.1 hides
	# its generic before/after frames and signature pad when the form takes the
	# photo, and the handler falls back to the last photo filed when a phone
	# sent no form. An empty contract is refused by Farm Task on purpose.
	"evidence_required": {"photos": True},
	"instructions": (
		"Stand in front of a plain wall, in even light. Face the camera, head and shoulders in the "
		"frame. No hat and no sunglasses."
	),
	"instructions_es": (
		"Párese frente a una pared lisa, con luz pareja. Mire a la cámara, con la cabeza y los "
		"hombros dentro del marco. Sin gorra y sin lentes de sol."
	),
	"form_schema": [
		{
			"key": "employee",
			"type": "link",
			"link": {"doctype": EMPLOYEE},
			"label": _t("Whose badge photo", "De quién es la foto"),
			"required": True,
		},
		{
			"key": "photo",
			"type": "photo",
			"label": _t("Badge photo", "Foto para el gafete"),
			"help": _t(
				"Plain background. Face the camera. No hat or sunglasses.",
				"Fondo liso. Mire a la cámara. Sin gorra ni lentes de sol.",
			),
			"required": True,
			"min_count": 1,
			"max_count": 1,
			"guide": "portrait_4x5",
			"camera": "front",
		},
		{
			"key": "consent",
			"type": "attestation",
			"label": _t("Consent", "Consentimiento"),
			"statement": _t(
				"This is the person named above, and this photo may be used on their farm ID card.",
				"Esta es la persona nombrada arriba, y esta foto puede usarse en su gafete de la granja.",
			),
			"required": True,
		},
		{
			"key": "request_card_print",
			"type": "check",
			"label": _t("Ask for a new ID card with this photo", "Pedir un gafete nuevo con esta foto"),
			"required": False,
		},
	],
}


# ── who ─────────────────────────────────────────────────────────────────────
def _roles_of(user: str) -> set:
	from . import roles

	try:
		return set(frappe.get_roles(user) or []) or set(roles.all_roles_of(user) or [])
	except Exception:
		return set()


def employee_of(user: str) -> str:
	if not (user and compat.doctype_exists(EMPLOYEE) and compat.has_field(EMPLOYEE, "user_id")):
		return ""
	try:
		return str(frappe.db.get_value(EMPLOYEE, {"user_id": user}, "name") or "")
	except Exception:
		return ""


def may_for_others(user: str) -> bool:
	return bool(user and user != "Guest" and _roles_of(user) & set(ROLES))


def require_may(user: str, employee: str, what: str) -> None:
	"""Their own photo, or a role that may handle somebody else's."""
	if employee and employee_of(user) == employee:
		return
	if may_for_others(user):
		return
	raise ToolError(
		f"{user or 'this account'} may {what} only for themselves; for somebody else it takes one of "
		f"{', '.join(ROLES)}. Nothing was changed."
	)


def _employee(reference: str) -> dict:
	name = str(reference or "").strip()
	if not name or not compat.doctype_exists(EMPLOYEE) or not frappe.db.exists(EMPLOYEE, name):
		raise ToolError(f"no Employee called {name!r} on this site. Nothing was changed.")
	fields = compat.existing_fields(EMPLOYEE, ("name", "employee_name", "company", "image", "status"))
	return dict(frappe.db.get_value(EMPLOYEE, name, fields, as_dict=True) or {"name": name})


# ── the picture ─────────────────────────────────────────────────────────────
def process(content: bytes) -> tuple:
	"""(jpeg bytes, width, height): 4:5 about the centre, 600 x 750, no metadata."""
	try:
		from PIL import Image, ImageOps
	except Exception as exc:  # pragma: no cover - Frappe ships Pillow
		raise ToolError(f"this server cannot process images ({type(exc).__name__}).") from exc
	try:
		image = Image.open(io.BytesIO(content))
		# The orientation is a metadata tag; apply it BEFORE the metadata goes.
		image = ImageOps.exif_transpose(image)
		image = image.convert("RGB")
	except Exception as exc:
		raise ToolError(
			f"that file is not a photograph this server can read ({type(exc).__name__})."
		) from exc
	width, height = image.size
	if width < 120 or height < 150:
		raise ToolError(f"that photograph is too small ({width} x {height}) to print on a card.")
	if width * HEIGHT > height * WIDTH:  # too wide: trim the sides
		keep = round(height * WIDTH / HEIGHT)
		left = (width - keep) // 2
		image = image.crop((left, 0, left + keep, height))
	else:  # too tall: trim top and bottom
		keep = round(width * HEIGHT / WIDTH)
		top = (height - keep) // 2
		image = image.crop((0, top, width, top + keep))
	image = image.resize((WIDTH, HEIGHT), Image.LANCZOS)
	out = io.BytesIO()
	# A fresh RGB image saved with no `exif=`: nothing the camera wrote survives.
	image.save(out, format="JPEG", quality=88, optimize=True)
	return out.getvalue(), WIDTH, HEIGHT


def _file_bytes(reference) -> tuple:
	"""(bytes, File docname, file_url) for a File docname, a file_url, or an answer item."""
	if isinstance(reference, dict):
		reference = (
			reference.get("file")
			or reference.get("file_token")
			or reference.get("file_docname")
			or reference.get("file_url")
			or reference.get("name")
		)
	text = str(reference or "").strip()
	if not text:
		raise ToolError("no photograph was given.")
	name = text if frappe.db.exists("File", text) else frappe.db.get_value("File", {"file_url": text}, "name")
	if not name:
		raise ToolError(
			f"{text!r} is not a File on this site. Upload it first (stage_file_chunk and "
			"commit_staged_file, or attach it in the Desk)."
		)
	doc = frappe.get_doc("File", name)
	content = doc.get_content()
	content = content.encode("latin-1") if isinstance(content, str) else bytes(content)
	return content, str(name), str(doc.get("file_url") or "")


def set_photo(employee: str, reference, by: str = "") -> dict:
	"""Make one file the Employee's badge photo. Raises `ToolError` saying why not."""
	from .tools import artifacts

	row = _employee(employee)
	content, source, _url = _file_bytes(reference)
	jpeg, width, height = process(content)
	previous = str(row.get("image") or "")
	stamp = str(frappe.utils.now()).replace("-", "").replace(":", "").replace(" ", "-")[:15]
	attachment = artifacts.attach_bytes(
		EMPLOYEE, row["name"], f"badge-photo-{row['name']}-{stamp}.jpg", jpeg, field="image"
	)
	image = str(attachment.get("file_url") or "")
	_comment(
		row["name"],
		f"Badge photo set to {image}"
		+ (f" (was {previous}; the earlier file is kept)" if previous else "")
		+ (f" by {by}" if by else "")
		+ ".",
	)
	return {
		"employee": row["name"],
		"employee_name": row.get("employee_name") or row["name"],
		"image": image,
		"file": attachment.get("name"),
		"previous_image": previous or None,
		"source_file": source,
		"width": width,
		"height": height,
		"bytes": len(jpeg),
	}


def _comment(employee: str, text: str) -> None:
	try:
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Info",
				"reference_doctype": EMPLOYEE,
				"reference_name": employee,
				"content": text,
			}
		).insert(ignore_permissions=True)
	except Exception:  # pragma: no cover - the photo is set either way
		pass


# ── raising the task ────────────────────────────────────────────────────────
def template_ready() -> bool:
	return compat.doctype_exists("Farm Task Template") and bool(
		frappe.db.exists("Farm Task Template", {"template_name": TEMPLATE})
	)


def open_task(employee: str) -> str:
	if not compat.doctype_exists(FARM_TASK) or not compat.has_field(FARM_TASK, "subject_docname"):
		return ""
	names = frappe.db.get_all(
		FARM_TASK,
		filters={
			"subject_doctype": EMPLOYEE,
			"subject_docname": employee,
			"state": ("in", list(OPEN_STATES)),
		},
		fields=["name", "template"],
		order_by="creation desc",
		limit=20,
	)
	for row in names or []:
		template = str(row.get("template") or "")
		if template and str(frappe.db.get_value("Farm Task Template", template, "template_name")) == TEMPLATE:
			return str(row["name"])
	return ""


def request(employee: str, assign_to: str = "", origin: str = "") -> dict:
	"""Raise the badge photo task for one person, or answer the one already open."""
	import json

	from .tools import dispatch, tasktemplates

	row = _employee(employee)
	if not template_ready():
		raise ToolError(
			f"this site has no {TEMPLATE!r} task template — it is seeded by `bench --site <site> migrate` "
			"(v0.214.0). Nothing was changed."
		)
	existing = open_task(row["name"])
	if existing:
		return {"task": existing, "already": True, "employee": row["name"], **_brief(existing)}
	holder = str(assign_to or "").strip() or row["name"]
	if holder != row["name"]:
		_employee(holder)
	name = str(row.get("employee_name") or row["name"])
	created = tasktemplates.create_task_from_template(
		{
			"template": TEMPLATE,
			"company": row.get("company") or None,
			"assigned_to": holder,
			"notes": f"Badge photo for {name}.",
		},
		origin=origin,
		fields={
			"task_name": f"Badge photo — {name}",
			"subject_doctype": EMPLOYEE,
			"subject_docname": row["name"],
			"form_answers": json.dumps({"employee": row["name"]}),
		},
	).data
	task = str(created.get("name") or "")
	return {
		"task": task,
		"already": False,
		"employee": row["name"],
		**_brief(task),
		"_task": dispatch.task_row(task),
	}


def _brief(task: str) -> dict:
	row = frappe.db.get_value(FARM_TASK, task, ["task_name", "state", "assigned_to"], as_dict=True) or {}
	return {
		"task_name": row.get("task_name"),
		"state": row.get("state"),
		"assigned_to": row.get("assigned_to") or None,
	}


def auto_request(employee: str, origin: str = "") -> dict | None:
	"""Raise the task because something noticed there is no photo. NEVER RAISES."""
	try:
		from . import flags

		if not flags.enabled(FLAG_AUTO, default=True):
			return None
		row = _employee(employee)
		if str(row.get("image") or "").strip():
			return None
		answer = request(row["name"], origin=origin)
		answer.pop("_task", None)
		return answer
	except Exception:
		return None


# ── completing it ───────────────────────────────────────────────────────────
def is_badge_photo(task: dict) -> bool:
	template = str(task.get("template") or "")
	if not template:
		return False
	try:
		return (
			str(frappe.db.get_value("Farm Task Template", template, "template_name") or template) == TEMPLATE
		)
	except Exception:
		return template == TEMPLATE


def subject(task: dict, answers: dict | None = None) -> str:
	"""Whose photo. The task's subject, else the form's answer, else WHO IT IS ASSIGNED TO.

	v0.216.1. The last fallback is FT-2026-10-00001: a foreman raised "Badge photo"
	from the template on the Work screen, so the task had no subject and the phone
	sent no form — and the handler had nobody to give the photo to. A badge photo
	task assigned to a person, with nothing else said, is that person's.
	"""
	if str(task.get("subject_doctype") or "") == EMPLOYEE and task.get("subject_docname"):
		return str(task["subject_docname"])
	named = str((answers or {}).get("employee") or "")
	if named:
		return named
	holder = str(task.get("assigned_to") or "")
	if holder and frappe.db.exists(EMPLOYEE, holder):
		return holder
	return ""


def _answers(raw) -> dict:
	if isinstance(raw, str):
		try:
			raw = json.loads(raw) if raw.strip() else {}
		except ValueError:
			return {}
	return raw if isinstance(raw, dict) else {}


def _is_photo(row: dict) -> bool:
	kind = str(row.get("evidence_type") or row.get("kind") or "").lower()
	if kind:
		return kind == "photo"
	name = str(row.get("file_name") or row.get("file_url") or row.get("caption") or "").lower()
	return name.endswith((".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"))


def portrait(answers: dict | None, evidence=None):
	"""The photograph to use. The form's portrait field; else the LAST photo filed.

	v0.216.1. An app that drew the generic evidence screen instead of the form
	files the picture as evidence — "before" at pickup, "after" at completion —
	and the after one, taken last, is the portrait (Tim's FT-2026-10-00001: the
	file set by hand was the after photo). A signature is never a portrait.
	"""
	photo = _answers(answers).get("photo")
	if isinstance(photo, (list, tuple)):
		photo = photo[0] if photo else None
	if photo:
		return photo
	rows = [row for row in (evidence or []) if isinstance(row, dict) and _is_photo(row)]
	if not rows:
		return None
	after = [row for row in rows if str(row.get("phase") or "").lower() != "before"]
	return (after or rows)[-1]


def precheck(task: dict, worker: str, actor: str, answers=None, evidence=None) -> None:
	"""Before anything is written: there is a portrait, and it is theirs or a supervisor's to take."""
	if answers is not None or evidence is not None:
		if not portrait(answers, evidence):
			raise ToolError(
				f"{task.get('name')} is a badge photo, and no photograph came with it. Take the "
				"portrait — one photo, face to the camera — and complete it again. Nothing was changed."
			)
	who = subject(task, _answers(answers))
	if not who or worker == who:
		return
	if may_for_others(actor):
		return
	raise ToolError(
		f"{task.get('name')} is the badge photo of {who}. A person takes their own; for somebody else "
		f"it takes one of {', '.join(ROLES)}. Nothing was changed."
	)


def on_completed(task: dict, answers: dict | None, worker: str, actor: str, evidence=None) -> dict:
	"""Turn the completed task into `Employee.image`. NEVER RAISES.

	v0.216.1: the portrait is the form's photo or else the last photo filed
	(`portrait`); the person is the subject, the form's answer or the assignee
	(`subject`). The task is given its subject if it had none, so it shows on the
	Employee's Farm Tasks. A failure is no longer only a key in an answer nobody
	reads: it is a comment on the task and on the Employee.
	"""
	answers = _answers(answers)
	out: dict = {"employee": subject(task, answers) or None}
	who = ""
	try:
		who = subject(task, answers)
		if not who:
			raise ToolError("the task names no employee and is assigned to nobody.")
		picture = portrait(answers, evidence)
		if not picture:
			raise ToolError("no photograph was filed with it.")
		out.update(set_photo(who, picture, actor))
		out["source"] = "form" if answers.get("photo") else "evidence"
		_give_subject(task, who)
		if compat.checked(answers.get("request_card_print")):
			out["card_print"] = _card(who, str(task.get("name") or ""), actor)
	except Exception as exc:
		out["error"] = str(exc) or type(exc).__name__
		_report_failure(task, who, out["error"])
	return out


def _give_subject(task: dict, employee: str) -> None:
	name = str(task.get("name") or "")
	if not name or task.get("subject_docname"):
		return
	try:
		frappe.db.set_value(
			FARM_TASK, name, {"subject_doctype": EMPLOYEE, "subject_docname": employee}, update_modified=False
		)
	except Exception:  # pragma: no cover - a column a site lacks
		pass


def _report_failure(task: dict, employee: str, reason: str) -> None:
	text = (
		f"Badge photo was NOT set from {task.get('name')}: {reason} The task is complete; set the "
		"photo with set_employee_photo, or raise a new badge photo task."
	)
	for doctype, name in ((FARM_TASK, task.get("name")), (EMPLOYEE, employee)):
		if not name:
			continue
		try:
			frappe.get_doc(doctype, name).add_comment("Comment", text)
		except Exception:  # pragma: no cover - a comment is a courtesy
			pass


def _card(employee: str, task: str, actor: str) -> dict:
	from . import card_print

	try:
		if not card_print.ready() or not card_print.can_request(actor):
			return {
				"requested": False,
				"reason": "this account may not ask for cards; a Card Print Requester can.",
			}
		key = f"badge-photo-{task}"[:64]
		answer = card_print.request(actor, "Employee ID", employee, key, requested_from="API")
		job = answer.get("job") or {}
		return {"requested": True, "job": job.get("name"), "status": job.get("status")}
	except Exception as exc:
		return {"requested": False, "reason": str(exc) or type(exc).__name__}


#: Template name → handler. A CLOSED REGISTRY: a completion runs code only for a
#: template named here, never for a name that arrives in data.
COMPLETION_HANDLERS = {TEMPLATE: on_completed}


# ── the Desk button ─────────────────────────────────────────────────────────
SCRIPT_NAME = "Employee — Request Badge Photo"
DESK_METHOD = "erpnext_mcp.api.badges.request_badge_photo"
SCRIPT_SOURCE = """// erpnext_mcp:badge-photo-button@r1
// Added by erpnext_mcp (v0.214.0). Untick `enabled` above, or delete this row,
// to remove the button — the app will not put it back.
frappe.ui.form.on("Employee", {
	refresh: function (frm) {
		if (frm.is_new()) { return; }
		frm.add_custom_button(__("Request badge photo"), function () {
			frappe.call({
				method: "%(method)s",
				args: { employee: frm.doc.name },
				freeze: true,
			}).then(function (response) {
				const answer = (response || {}).message || {};
				if (!answer.task) { return; }
				frappe.msgprint(
					answer.already
						? __("A badge photo task is already open: {0}", [answer.task])
						: __("Badge photo task raised: {0}. It is on their phone under My Tasks.", [answer.task])
				);
			});
		}, __("Badge"));
	},
});
""".replace("%(method)s", DESK_METHOD)


#: v0.216.1. The Employee form's Connections: the Farm Tasks assigned to that
#: person — a badge photo task among them. Tim could not find FT-2026-10-00001
#: from the Desk. A custom DocType Link (Customize Form → Links), seeded once.
CONNECTION_GROUP = "Farm Ops"


def seed_employee_connection() -> dict:
	"""Add "Farm Task" to the Employee form's Connections. Never raises; never duplicates."""
	report = {"created": False, "reason": ""}
	try:
		if not frappe.db.exists("DocType", "DocType Link") or not frappe.db.exists("DocType", EMPLOYEE):
			report["reason"] = "this site has no DocType Link or Employee doctype"
			return report
		if frappe.db.exists("DocType Link", {"parent": EMPLOYEE, "link_doctype": FARM_TASK}):
			report["reason"] = "already present"
			return report
		doc = frappe.get_doc(
			{
				"doctype": "DocType Link",
				"parent": EMPLOYEE,
				"parenttype": "DocType",
				"parentfield": "links",
				"link_doctype": FARM_TASK,
				"link_fieldname": "assigned_to",
				"group": CONNECTION_GROUP,
				"custom": 1,
			}
		)
		doc.insert(ignore_permissions=True)
		try:
			frappe.clear_cache(doctype=EMPLOYEE)
		except Exception:  # pragma: no cover
			pass
		report["created"] = True
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		report["reason"] = f"{type(exc).__name__}: {exc}"
	return report


def seed_desk_button() -> dict:
	"""Create the Employee form button if it is not there. Never raises; never overwrites."""
	report = {"created": False, "name": SCRIPT_NAME, "reason": ""}
	try:
		if not frappe.db.exists("DocType", "Client Script") or not frappe.db.exists("DocType", EMPLOYEE):
			report["reason"] = "this site has no Client Script or Employee doctype"
			return report
		if frappe.db.exists("Client Script", SCRIPT_NAME):
			report["reason"] = "already present"
			return report
		doc = frappe.get_doc(
			{
				"doctype": "Client Script",
				"name": SCRIPT_NAME,
				"dt": EMPLOYEE,
				"view": "Form",
				"enabled": 1,
				"script": SCRIPT_SOURCE,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		report["created"] = True
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		report["reason"] = f"{type(exc).__name__}: {exc}"
	return report
