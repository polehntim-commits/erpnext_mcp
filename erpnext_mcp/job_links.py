# SPDX-License-Identifier: MIT
"""Contractor and supplier jobs, shared by a time-limited link. v0.271.0 (docs/contracts/job_links_v0_271.yaml).

ONE ENGINE, TEMPLATES AS DATA. A Job Template (Farm Config Version, kind "Job Template") says what kind of job it is —
contractor work, supplier delivery, supplier pickup — its default scope, the prep tasks the crew does first, which
hazard layers the page draws, which actions the contractor gets, how long a link lives, and what to prompt for when
the job is done. A Contractor Job is the work order; a Contractor Job Link is one shareable, revocable link to it.

THE LINK IS A CREDENTIAL AND IS TREATED AS ONE: 32 random bytes, only the SHA-256 stored; live from issue until the
job closes or its end date (whichever first), extendable and revocable; every view logged; every token that will
not work — unknown, expired, revoked, closed job, sharing off — gets the same 404.

THE PAGE CARRIES ONLY THIS JOB: its blocks' outlines, the hazards inside them, the entrance and delivery spot, the
scope, dates and the contact Tim chose to show. No other block, asset, person, price or company.

SHARING IS OFF PER ISSUING COMPANY until Tim turns it on (`job_links_enabled`), so a job for a company that is not
live yet (Constancy) can be prepared as a draft and cannot be shared.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import secrets

import frappe

from . import compat

JOB = "Contractor Job"
LINK = "Contractor Job Link"
KIND = "Job Template"
TASK = "Farm Task"
FLAG = "job_links_enabled"
KINDS = {"contractor_work": "Contractor Work", "supplier_delivery": "Supplier Delivery", "supplier_pickup": "Supplier Pickup"}
EVENTS_BY_KIND = {
	"Contractor Work": ("Arrived", "Done"),
	"Supplier Delivery": ("Delivered",),
	"Supplier Pickup": ("Picked Up",),
}
DONE_EVENTS = ("Done", "Delivered", "Picked Up")
CLOSED = ("Closed", "Cancelled")
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_NOTE = 1000
VIEW_LOG_CAP = 500
PREP_TEMPLATES = ("Gather sprinklers before removal", "Mark valves before removal")


class JobError(ValueError):
	"""A refusal a caller can act on."""


# ── templates (data) ─────────────────────────────────────────────────────────
SEED_TEMPLATES = {
	"orchard_removal": {
		"title": {"en": "Orchard removal", "es": "Remoción de huerto"},
		"kind": "contractor_work",
		"default_scope": "Remove the trees in the blocks shown: push or pull, stump / grind, and haul off the debris. "
		                 "Protect the valves and lines marked on the map.",
		"prep_tasks": list(PREP_TEMPLATES),
		"hazard_layers": ["valves", "hazards"],
		"actions": {"arrived": True, "done": True, "photos": True, "note": True},
		"link_days": 45,
		"show_sop": False,
		"post_completion": [{"prompt": "lease_crop_class",
		                     "message": "The trees are out: change the lease crop class of these blocks to fallow / "
		                                "replant (CF–OMLC lease) — a person decides; nothing changes by itself."}],
	},
	"supplier_delivery": {
		"title": {"en": "Supplier delivery", "es": "Entrega de proveedor"},
		"kind": "supplier_delivery",
		"default_scope": "Deliver to the spot marked on the map. Tap Delivered and photograph the delivery ticket.",
		"prep_tasks": [],
		"hazard_layers": [],
		"actions": {"photos": True, "note": True},
		"link_days": 14,
		"show_sop": False,
		"post_completion": [],
	},
	"supplier_pickup": {
		"title": {"en": "Supplier pickup / return", "es": "Recogida / devolución de proveedor"},
		"kind": "supplier_pickup",
		"default_scope": "Collect from the spot marked on the map. Tap Picked up and photograph the ticket.",
		"prep_tasks": [],
		"hazard_layers": [],
		"actions": {"photos": True, "note": True},
		"link_days": 14,
		"show_sop": False,
		"post_completion": [],
	},
}

PREP_TASK_TEMPLATES = (
	{
		"template_name": "Gather sprinklers before removal",
		"title_es": "Recoger aspersores antes de la remoción",
		"task_type": "Irrigation",
		"description": "Pull the sprinkler heads and risers in this block so the contractor does not destroy them.",
		"instructions": "Pull every head and riser; bag and label them by block for the shop rebuild list.",
		"instructions_es": "Quita cada cabeza y elevador; embólsalos y etiquétalos por bloque para reconstruirlos en el taller.",
		"estimated_duration_minutes": 240,
		"skill_required": "irrigation",
		"dispatch_mode": "Either",
		"evidence_required": {"photos": True},
		"checklist": [{"item_name": "Every head and riser pulled", "required": True},
		              {"item_name": "Bagged and labelled by block", "required": True}],
		"enabled": 1,
	},
	{
		"template_name": "Mark valves before removal",
		"title_es": "Marcar válvulas antes de la remoción",
		"task_type": "Irrigation",
		"description": "Find and flag every valve in this block, and drop a pin for each on the phone (Add hazard pin).",
		"instructions": "Flag each valve so a machine operator can see it, and drop a Valve pin at it from the field card.",
		"instructions_es": "Marca cada válvula para que el operador la vea, y pon un pin de Válvula desde la tarjeta del bloque.",
		"estimated_duration_minutes": 120,
		"skill_required": "irrigation",
		"dispatch_mode": "Either",
		"evidence_required": {"photos": True, "gps": True},
		"checklist": [{"item_name": "Every valve flagged", "required": True},
		              {"item_name": "A pin dropped for each valve", "required": True}],
		"enabled": 1,
	},
)


def validate(body: dict, *, key: str = "", for_publish: bool = False) -> dict:
	errors, warnings = [], []
	if body.get("kind") not in KINDS:
		errors.append(f"kind is one of {', '.join(KINDS)}")
	days = body.get("link_days")
	if not isinstance(days, int) or not 1 <= days <= 365:
		errors.append("link_days: whole days, 1–365")
	for name in body.get("prep_tasks") or []:
		if for_publish and compat.doctype_exists("Farm Task Template") and not frappe.db.exists(
			"Farm Task Template", {"template_name": name}):
			errors.append(f"prep_tasks: no Farm Task Template {name!r}")
	for layer in body.get("hazard_layers") or []:
		if layer not in ("valves", "hazards"):
			errors.append(f"hazard_layers: valves or hazards, not {layer!r}")
	return {"errors": errors, "warnings": warnings}


def template(key: str) -> dict:
	from . import phone_config

	doc = phone_config.doc_of(KIND, key, status=phone_config.PUBLISHED)
	if doc is None:
		raise JobError(f"no published Job Template {key!r}. Seeded: {', '.join(SEED_TEMPLATES)} (publish in the Desk).")
	return phone_config.body_of(doc)


def seed() -> dict:
	"""The three templates, published (they are shapes, not commitments: a job is still a draft until shared)."""
	from . import phone_config

	return {"published": [n for key, body in SEED_TEMPLATES.items()
	                      if (n := phone_config.seed(KIND, key, body, "Built-in job template, seeded at install (v0.271.0)."))]}


# ── the job ──────────────────────────────────────────────────────────────────
def _now() -> datetime.datetime:
	return frappe.utils.get_datetime(frappe.utils.now())


def create_job(template_key: str, company: str, *, fields=(), supplier="", purchase_order="", title="", scope="",
               start_date="", end_date="", contact_name="", contact_phone="", show_contact_phone=False,
               entrance=None, gate_notes="", delivery_spot=None, delivery_spot_label="", actor="") -> str:
	"""A Draft job, with its prep tasks raised per block. Fields by name or alias (a named group counts)."""
	from . import field_names

	body = template(template_key)
	if not frappe.db.exists("Company", company):
		raise JobError(f"no Company {company!r}.")
	resolved = []
	for name in fields or []:
		try:
			for field in field_names.resolve_many(name, [company]):
				if field not in resolved:
					resolved.append(field)
		except ValueError as exc:
			raise JobError(str(exc)) from None
	doc = frappe.new_doc(JOB)
	doc.job_title = (title or (body.get("title") or {}).get("en") or template_key)[:140]
	doc.template = template_key
	doc.kind = KINDS[body["kind"]]
	doc.status = "Draft"
	doc.company = company
	doc.supplier = supplier or None
	doc.purchase_order = purchase_order or None
	doc.scope = scope or body.get("default_scope") or ""
	doc.start_date = start_date or None
	doc.end_date = end_date or None
	doc.contact_name = contact_name or None
	doc.contact_phone = contact_phone or None
	doc.show_contact_phone = 1 if show_contact_phone else 0
	doc.hazard_layers = "\n".join(body.get("hazard_layers") or [])
	doc.gate_notes = gate_notes or None
	doc.show_sop = 1 if body.get("show_sop") else 0
	doc.actions = json.dumps(body.get("actions") or {})
	doc.post_completion = json.dumps(body.get("post_completion") or [])
	if entrance:
		doc.entrance_lat, doc.entrance_lon = float(entrance[0]), float(entrance[1])
	if delivery_spot:
		doc.delivery_spot_lat, doc.delivery_spot_lon = float(delivery_spot[0]), float(delivery_spot[1])
		doc.delivery_spot_label = delivery_spot_label or "Delivery spot"
	for field in resolved:
		doc.append("fields", {"field": field, "field_name": frappe.db.get_value("Field", field, "field_name") or field})
	doc.insert(ignore_permissions=True)
	doc.prep_tasks = "\n".join(_raise_prep(doc, body))
	doc.save(ignore_permissions=True)
	return doc.name


def _raise_prep(doc, body: dict) -> list:
	from .tools import tasktemplates

	made = []
	for template_name in body.get("prep_tasks") or []:
		if not compat.doctype_exists("Farm Task Template") or not frappe.db.get_value(
			"Farm Task Template", {"template_name": template_name}, "enabled"):
			continue
		for row in (doc.get("fields") or [None]):
			field = row.get("field") if row else None
			result = tasktemplates.create_task_from_template(
				{"template": template_name, "company": doc.company,
				 **({"location_doctype": "Field", "location": field} if field else {}),
				 "task_name": f"{template_name} — {row.get('field_name') if row else doc.job_title}",
				 "notes": f"Before {doc.job_title} ({doc.name}).",
				 **({"due_date": str(doc.start_date)} if doc.start_date else {})},
				origin="compliance_rule", fields={"source_workorder": f"job:{doc.name}:{template_name}:{field or ''}"[:140]})
			made.append((result.data or {}).get("name"))
	return [m for m in made if m]


def readiness(job: str) -> dict:
	doc = frappe.get_doc(JOB, job)
	tasks = [t for t in str(doc.prep_tasks or "").splitlines() if t.strip()]
	states = {t: frappe.db.get_value(TASK, t, "state") for t in tasks}
	open_ = sorted(t for t, s in states.items() if s != "Completed")
	return {
		"job": doc.name, "status": doc.status,
		"prep_tasks": [{"task": t, "state": s} for t, s in sorted(states.items())],
		"prep_done": not open_, "open_prep": open_,
		"overridden": bool(str(doc.prep_override_reason or "").strip()),
		"ready": doc.status not in ("Draft",) or not open_,
		"sharing_enabled": sharing_enabled(doc.company),
	}


def mark_ready(job: str, actor: str, override_reason: str = "") -> dict:
	"""Ready for the contractor: every prep task done — or Tim says why it is ready anyway."""
	doc = frappe.get_doc(JOB, job)
	if doc.status in CLOSED:
		raise JobError(f"{job} is {doc.status}.")
	state = readiness(job)
	if not state["prep_done"]:
		if not str(override_reason or "").strip():
			raise JobError(f"{len(state['open_prep'])} prep task(s) not done: {', '.join(state['open_prep'])}. "
			               "Finish them, or give a reason to mark it ready anyway.")
		doc.prep_override_reason = override_reason.strip()
		doc.prep_override_by = actor
	if doc.status == "Draft":
		doc.status = "Ready"
	doc.ready_at = frappe.utils.now()
	doc.save(ignore_permissions=True)
	return readiness(job)


def sharing_enabled(company: str) -> bool:
	try:
		from . import flags

		return bool(flags.value(FLAG, company=company, default=False))
	except Exception:
		return False


def close_job(job: str, actor: str, cancel: bool = False) -> dict:
	doc = frappe.get_doc(JOB, job)
	doc.status = "Cancelled" if cancel else "Closed"
	doc.save(ignore_permissions=True)
	for name in frappe.db.get_all(LINK, filters={"job": job, "status": "Active"}, pluck="name") or []:
		frappe.db.set_value(LINK, name, "status", "Expired")
	return {"job": job, "status": doc.status}


# ── the link ─────────────────────────────────────────────────────────────────
def hash_token(token: str) -> str:
	return hashlib.sha256(str(token or "").encode()).hexdigest()


def link_url(token: str) -> str:
	from . import settings

	base = settings.farmops_public_url() or str(frappe.utils.get_url() or "").rstrip("/")
	return f"{base}/farmops/api/job/{token}"


def _end_of(day) -> datetime.datetime:
	return datetime.datetime.combine(frappe.utils.getdate(day), datetime.time(23, 59, 59))


def issue_link(job: str, actor: str, days: int | None = None) -> dict:
	"""A new link — shown ONCE. Refused while sharing is off for the company or the job is not ready."""
	doc = frappe.get_doc(JOB, job)
	if doc.status in CLOSED:
		raise JobError(f"{job} is {doc.status}; it cannot be shared.")
	if not sharing_enabled(doc.company):
		raise JobError(f"sharing job links is off for {doc.company} (it is not live yet, or Tim has not turned on "
		               f"`{FLAG}` for it). The job stays a draft until then.")
	if doc.status == "Draft":
		raise JobError(f"{job} is not ready for the contractor yet — finish the prep tasks (or mark it ready with a "
		               "reason) first.")
	try:
		body_days = int(template(doc.template).get("link_days") or 30) if doc.template else 30
	except JobError:
		body_days = 30
	expires = _now() + datetime.timedelta(days=int(days or body_days))
	if doc.end_date:
		expires = min(expires, _end_of(doc.end_date))
	token = secrets.token_urlsafe(32)
	link = frappe.new_doc(LINK)
	link.job = job
	link.company = doc.company
	link.status = "Active"
	link.token_hash = hash_token(token)
	link.issued_by = actor
	link.issued_at = frappe.utils.now()
	link.expires_at = expires.strftime("%Y-%m-%d %H:%M:%S")
	link.view_count = 0
	link.insert(ignore_permissions=True)
	if doc.status == "Ready":
		doc.status = "Shared"
		doc.save(ignore_permissions=True)
	return {"link": link.name, "url": link_url(token), "expires_at": link.expires_at, "job": job,
	        "shown_once": "Copy or share it now — only its hash is kept."}


def find(token: str) -> dict | None:
	"""The live link and its job, or None — the same None for every kind of failure."""
	if not token or len(token) > 200:
		return None
	digest = hash_token(token)
	rows = frappe.db.get_all(LINK, filters={"token_hash": digest}, fields=["name", "job", "status", "expires_at",
	                                                                        "token_hash"], limit=1) or []
	if not rows or not hmac.compare_digest(str(rows[0].get("token_hash") or ""), digest):
		return None
	row = dict(rows[0])
	if row.get("status") != "Active":
		return None
	job = frappe.db.get_value(JOB, row["job"], ["name", "status", "company"], as_dict=True) or {}
	if not job or job.get("status") in CLOSED or not sharing_enabled(job.get("company")):
		return None
	if row.get("expires_at") and frappe.utils.get_datetime(row["expires_at"]) < _now():
		frappe.db.set_value(LINK, row["name"], "status", "Expired")
		return None
	return row


def record_view(link: str, what: str, ip: str, user_agent: str = "") -> None:
	doc = frappe.get_doc(LINK, link)
	doc.view_count = int(doc.view_count or 0) + 1
	doc.last_viewed_at = frappe.utils.now()
	if len(doc.get("views") or []) < VIEW_LOG_CAP:
		doc.append("views", {"at": frappe.utils.now(), "ip": (ip or "")[:60], "what": what,
		                     "user_agent": str(user_agent or "")[:140]})
	doc.save(ignore_permissions=True)


def extend(link: str, actor: str, days: int = 0, until: str = "") -> dict:
	doc = frappe.get_doc(LINK, link)
	if doc.status == "Revoked":
		raise JobError("a revoked link stays revoked — issue a new one.")
	new = _end_of(until) if until else _now() + datetime.timedelta(days=int(days or 7))
	doc.expires_at = new.strftime("%Y-%m-%d %H:%M:%S")
	doc.status = "Active"
	doc.save(ignore_permissions=True)
	return {"link": link, "expires_at": doc.expires_at, "extended_by": actor}


def revoke(link: str, actor: str, reason: str = "") -> dict:
	doc = frappe.get_doc(LINK, link)
	doc.status = "Revoked"
	doc.revoked_at = frappe.utils.now()
	doc.revoked_by = actor
	doc.revoked_reason = reason or None
	doc.save(ignore_permissions=True)
	return {"link": link, "status": "Revoked"}


def links_of(job: str = "", company: str = "", include_views: bool = True) -> list:
	filters = {"job": job} if job else {}
	if company:
		filters["job"] = ("in", frappe.db.get_all(JOB, filters={"company": company}, pluck="name") or ["-"])
	out = []
	for row in frappe.db.get_all(LINK, filters=filters, fields=["name", "job", "status", "issued_by", "issued_at",
	                                                             "expires_at", "revoked_at", "revoked_by", "view_count",
	                                                             "last_viewed_at"], order_by="creation desc", limit=500) or []:
		entry = {k: (str(v) if isinstance(v, (datetime.date, datetime.datetime)) else v) for k, v in dict(row).items()}
		if include_views:
			entry["views"] = [{"at": str(v.get("at") or ""), "ip": v.get("ip"), "what": v.get("what")}
			                  for v in (frappe.get_doc(LINK, row["name"]).get("views") or [])][-50:]
		out.append(entry)
	return out


# ── what the page shows — ONLY this job ──────────────────────────────────────
def _hazards(job_doc) -> list:
	from . import field_card

	layers = {line.strip() for line in str(job_doc.hazard_layers or "").splitlines() if line.strip()}
	if not layers:
		return []
	out, seen = [], set()
	for row in job_doc.get("fields") or []:
		for h in field_card.hazards(row.get("field")):
			if h["name"] in seen:
				continue
			is_valve = h.get("asset_type") == "Irrigation Valve"
			if (is_valve and "valves" not in layers) or (not is_valve and "hazards" not in layers):
				continue
			seen.add(h["name"])
			out.append({"kind": "valve" if is_valve else "hazard", "lat": h["latitude"], "lon": h["longitude"],
			            "label": "Valve — protect" if is_valve else (h.get("description") or "Hazard — avoid")[:80]})
	return out


def _outlines(job_doc) -> list:
	out = []
	for row in job_doc.get("fields") or []:
		shape = frappe.db.get_value("Field", row.get("field"), "boundary_geojson")
		if shape:
			try:
				out.append({"label": row.get("field_name") or "Block", "geojson": json.loads(shape)})
			except ValueError:
				continue
	return out


def _expected_lines(job_doc) -> list:
	if not job_doc.purchase_order or not compat.doctype_exists("Purchase Order Item"):
		return []
	rows = frappe.db.get_all("Purchase Order Item", filters={"parent": job_doc.purchase_order},
	                         fields=["item_name", "qty", "uom"], order_by="idx asc", limit=100) or []
	return [{"item": r.get("item_name"), "qty": r.get("qty"), "uom": r.get("uom")} for r in rows]


def page_data(job: str) -> dict:
	"""Everything the public page shows, and nothing else. Allow-listed key by key."""
	doc = frappe.get_doc(JOB, job)
	actions = json.loads(doc.actions or "{}") if isinstance(doc.actions, str) else (doc.actions or {})
	events = [{"event": e.get("event"), "at": str(e.get("at") or "")[:16]} for e in doc.get("events") or []
	          if e.get("event") in ("Arrived", "Done", "Delivered", "Picked Up")]
	return {
		"title": doc.job_title,
		"kind": doc.kind,
		"status": doc.status,
		"scope": doc.scope or "",
		"start_date": str(doc.start_date or "") or None,
		"end_date": str(doc.end_date or "") or None,
		"contact": {"name": doc.contact_name or None,
		            "phone": doc.contact_phone if cint(doc.show_contact_phone) else None},
		"entrance": {"lat": doc.entrance_lat, "lon": doc.entrance_lon} if doc.entrance_lat and doc.entrance_lon else None,
		"delivery_spot": ({"lat": doc.delivery_spot_lat, "lon": doc.delivery_spot_lon,
		                   "label": doc.delivery_spot_label or "Delivery spot"}
		                  if doc.delivery_spot_lat and doc.delivery_spot_lon else None),
		"gate_notes": doc.gate_notes or None,
		"sop_link": doc.sop_link if cint(doc.show_sop) and doc.sop_link else None,
		"blocks": _outlines(doc),
		"hazards": _hazards(doc),
		"expected": _expected_lines(doc),
		"actions": {"events": list(EVENTS_BY_KIND.get(doc.kind, ())),
		            "photos": bool(actions.get("photos", True)), "note": bool(actions.get("note", True))},
		"done": [e for e in events],
	}


def cint(value) -> int:
	try:
		return int(value or 0)
	except (TypeError, ValueError):
		return 0


# ── what the contractor does ─────────────────────────────────────────────────
def record_event(link: dict, event: str, *, note: str = "", name_given: str = "", counts=None, ip: str = "",
                 file: str = "") -> dict:
	doc = frappe.get_doc(JOB, link["job"])
	allowed = set(EVENTS_BY_KIND.get(doc.kind, ())) | {"Note", "Photo"}
	if event not in allowed:
		raise JobError(f"{event!r} is not an action on this job.")
	if event in DONE_EVENTS and any(e.get("event") == event for e in doc.get("events") or []):
		return {"event": event, "already": True, "status": doc.status}
	doc.append("events", {"event": event, "at": frappe.utils.now(), "ip": (ip or "")[:60],
	                      "name_given": str(name_given or "")[:80] or None, "note": str(note or "")[:MAX_NOTE] or None,
	                      "counts": json.dumps(counts) if counts else None, "file": file or None})
	if event == "Arrived" and doc.status in ("Ready", "Shared"):
		doc.status = "In Progress"
	prompts = []
	if event in DONE_EVENTS:
		doc.status = "Done"
		doc.done_at = frappe.utils.now()
		prompts = _post_completion(doc)
	doc.save(ignore_permissions=True)
	return {"event": event, "status": doc.status, "prompts": prompts}


def _post_completion(doc) -> list:
	"""Prompts for a person — never a change. Raised as a compliance alert to the managers."""
	raw = doc.post_completion
	items = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
	if not items or not compat.doctype_exists("Compliance Alert"):
		return []
	from .alerts import base as alerts

	made = []
	blocks = ", ".join(r.get("field_name") or r.get("field") for r in doc.get("fields") or []) or doc.job_title
	for item in items:
		key = alerts.alert_key(f"job_{item.get('prompt') or 'prompt'}", JOB, doc.name)
		if frappe.db.exists("Compliance Alert", key):
			continue
		alert = frappe.new_doc("Compliance Alert")
		alert.alert_key = key
		alert.alert_type = f"job_{item.get('prompt') or 'prompt'}"
		alert.severity = "Info"
		alert.category = "Other"
		alert.company = doc.company
		alert.source_doctype = JOB
		alert.source_docname = doc.name
		alert.alert_message = f"{doc.job_title} — {blocks}: {item.get('message') or 'follow up'}"
		alert.first_seen = frappe.utils.today()
		alert.last_refreshed = frappe.utils.now()
		alert.insert(ignore_permissions=True)
		made.append(alert.name)
	return made


def _image_ok(head: bytes) -> str:
	if head.startswith(b"\xff\xd8\xff"):
		return "jpg"
	if head.startswith(b"\x89PNG\r\n\x1a\n"):
		return "png"
	if len(head) >= 12 and head[4:8] == b"ftyp":
		return "heic"
	return ""


def attach_photo(link: dict, content: bytes, ip: str = "", kind: str = "Photo") -> dict:
	"""One photo onto the job — private, size-capped, an image by its first bytes or refused."""
	if not content:
		raise JobError("the photo was empty.")
	if len(content) > MAX_PHOTO_BYTES:
		raise JobError(f"the photo is over {MAX_PHOTO_BYTES // (1024 * 1024)} MB.")
	ext = _image_ok(content[:16])
	if not ext:
		raise JobError("that is not a JPEG, PNG or HEIC photo.")
	doc = frappe.get_doc(JOB, link["job"])
	if not json.loads(doc.actions or "{}").get("photos", True):
		raise JobError("photos are not taken on this job.")
	stamp = frappe.utils.now().replace(" ", "_").replace(":", "")[:15]
	saved = frappe.get_doc({"doctype": "File", "file_name": f"{doc.name}-{stamp}.{ext}", "attached_to_doctype": JOB,
	                        "attached_to_name": doc.name, "is_private": 1, "content": content})
	saved.insert(ignore_permissions=True)
	record_event(link, "Photo", ip=ip, file=saved.name, note=kind if kind != "Photo" else "")
	return {"saved": True}
