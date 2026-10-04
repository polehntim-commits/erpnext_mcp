# SPDX-License-Identifier: MIT
"""A business card becomes an ERPNext Contact. v0.231.0.

docs/design/business_card_contacts.md (Tell the Farm AFB-2026-00031, approved
2026-10-04). Collect once, use everywhere: the card is a standard **Contact**
(with its emails, phones and links) and an **Address** — the records a Supplier,
a Customer, a Purchase Order and the office@ triage already read. No new doctype;
five custom fields on Contact say how and where it was captured.

THE PHONE READS THE CARD. Apple Vision and the on-device model do the OCR and the
split into name, title and company; the server never sees the image text unless a
caller sends it. What arrives here is a `card` dict a person has already
confirmed.

TWO DECISIONS ARE A PERSON'S, NEVER THIS MODULE'S:

* **A duplicate.** An existing Contact with the same email or phone is a merge
  proposal. `save` refuses until the caller says `merge_into` (update that one)
  or `save_as_new`.
* **The company link.** `preview` offers the match (the merchant resolver's
  domain/phone/name cascade for a Supplier; a name match for a Customer or one
  of the farm's own Companies). `save` links only what the caller names in
  `link_to`, or creates the party named in `create_party`. Nothing is linked or
  created on a guess.

SAME REQUEST, SAME CONTACT: `client_request_id` is kept on the Contact, and a
resend answers with the one already filed.
"""

from __future__ import annotations

import re

import frappe

from . import compat
from .errors import ToolError

CONTACT = "Contact"
ADDRESS = "Address"
CUSTOM_FIELD = "Custom Field"
#: Farm Manager, plus the roles behind the Owner and Bookkeeper personas
#: (`sidebar.PERSONAS`) and System Manager. Not Family Member: that role is broad.
ROLES = ("Farm Manager", "Accounts Manager", "Accounts User", "System Manager")
LINKABLE = ("Supplier", "Customer", "Company")
SOURCE = "Business card"
MAX_EMAILS = 6
MAX_PHONES = 6

FIELDS = (
	{"fieldname": "card_source", "label": "Captured From", "fieldtype": "Data", "insert_after": "company_name",
	 "read_only": 1, "description": "v0.231.0. How this contact reached the farm, e.g. Business card."},
	{"fieldname": "card_met_on", "label": "Met On", "fieldtype": "Date", "insert_after": "card_source"},
	{"fieldname": "card_met_at", "label": "Met At", "fieldtype": "Data", "insert_after": "card_met_on",
	 "description": "Where — a show, a block, the shop."},
	{"fieldname": "card_website", "label": "Website", "fieldtype": "Data", "insert_after": "card_met_at"},
	{"fieldname": "card_captured_by", "label": "Captured By", "fieldtype": "Link", "options": "User",
	 "insert_after": "card_website", "read_only": 1},
	{"fieldname": "card_client_request_id", "label": "Request ID", "fieldtype": "Data",
	 "insert_after": "card_captured_by", "read_only": 1, "hidden": 1},
)


# ── fields ────────────────────────────────────────────────────────────────
def ensure_fields() -> bool:
	"""The Contact custom fields. Never raises; False when the site will not take them."""
	try:
		if all(compat.has_field(CONTACT, f["fieldname"]) for f in FIELDS):
			return True
		if not compat.doctype_exists(CUSTOM_FIELD) or not compat.doctype_exists(CONTACT):
			return False
		for field in FIELDS:
			if compat.has_field(CONTACT, field["fieldname"]):
				continue
			if frappe.db.exists(CUSTOM_FIELD, {"dt": CONTACT, "fieldname": field["fieldname"]}):
				continue
			doc = frappe.new_doc(CUSTOM_FIELD)
			doc.dt = CONTACT
			for key, value in field.items():
				doc.set(key, value)
			doc.insert(ignore_permissions=True)
		frappe.clear_cache(doctype=CONTACT)
		return True
	except Exception:  # pragma: no cover - a site that will not take a Custom Field
		return False


def require_role(user: str) -> None:
	if not set(frappe.get_roles(user) or []) & set(ROLES):
		raise frappe.PermissionError(
			"The contact register is restricted to a Farm Manager, the bookkeeper (Accounts Manager / Accounts "
			"User) or a System Manager. Nothing was read or saved."
		)


# ── cleaning what the phone confirmed ───────────────────────────────────────
def _text(value, limit: int = 140) -> str:
	return " ".join(str(value or "").split())[:limit]


def _emails(raw) -> list:
	out = []
	for value in raw or []:
		email = _text(value.get("email") if isinstance(value, dict) else value).lower()
		if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email) and email not in out:
			out.append(email)
	return out[:MAX_EMAILS]


def _phones(raw) -> list:
	from .tools import receipts

	out, seen = [], set()
	for value in raw or []:
		number = _text(value.get("number") if isinstance(value, dict) else value, 40)
		kind = _text(value.get("kind") if isinstance(value, dict) else "", 20).lower()
		digits = receipts.normalize_phone(number)
		if not digits or not receipts.plausible_phone(digits) or digits in seen:
			continue
		seen.add(digits)
		out.append({"number": number, "digits": digits, "kind": kind if kind in ("mobile", "work", "fax") else ""})
	return out[:MAX_PHONES]


def clean(card: dict) -> dict:
	"""The card as this module stores it. Refuses one with no name and no company."""
	if not isinstance(card, dict):
		raise ToolError("card must be an object. Nothing was saved.")
	address = card.get("address") if isinstance(card.get("address"), dict) else {}
	out = {
		"first_name": _text(card.get("first_name")),
		"last_name": _text(card.get("last_name")),
		"title": _text(card.get("title")),
		"company": _text(card.get("company")),
		"emails": _emails(card.get("emails")),
		"phones": _phones(card.get("phones")),
		"website": _text(card.get("website"), 200),
		"address": {k: _text(address.get(k)) for k in ("line1", "line2", "city", "state", "postal_code", "country")},
		"notes": str(card.get("notes") or "").strip()[:2000],
		"met_at": _text(card.get("met_at")),
		"met_on": _text(card.get("met_on"), 10),
	}
	if not (out["first_name"] or out["last_name"] or out["company"]):
		raise ToolError("a card needs a name or a company. Nothing was saved.")
	return out


# ── what a person decides ───────────────────────────────────────────────────
def duplicates(card: dict) -> list:
	"""Contacts already holding one of the card's emails or phones."""
	from .tools import receipts

	found: dict = {}
	if card["emails"] and compat.doctype_exists("Contact Email"):
		for parent in frappe.db.get_all(
			"Contact Email", filters={"email_id": ("in", card["emails"]), "parenttype": CONTACT}, pluck="parent"
		) or []:
			found.setdefault(parent, set()).add("email")
	wanted = {p["digits"] for p in card["phones"]}
	if wanted and compat.doctype_exists("Contact Phone"):
		for row in frappe.db.get_all(
			"Contact Phone", filters={"parenttype": CONTACT}, fields=["parent", "phone"], limit=20000
		) or []:
			if receipts.normalize_phone(row.get("phone")) in wanted:
				found.setdefault(row["parent"], set()).add("phone")
	return [
		{**describe(name), "matched_on": sorted(reasons)}
		for name, reasons in sorted(found.items())
		if frappe.db.exists(CONTACT, name)
	]


def match(card: dict) -> dict:
	"""Which party this card's company is, offered — never applied."""
	from .tools import receipts

	domain = receipts.normalize_domain(card["website"]) or (
		card["emails"][0].split("@", 1)[1] if card["emails"] else ""
	)
	phone = card["phones"][0]["digits"] if card["phones"] else ""
	out = {"supplier": None, "customer": None, "company": None, "confidence": 0.0, "method": None,
		"auto_link_safe": False}
	if card["company"] or domain or phone:
		try:
			resolved = receipts.resolve_merchant(card["company"], merchant_url=domain, merchant_phone=phone)
		except Exception:  # pragma: no cover - a resolver that cannot read its tables
			resolved = {}
		if resolved.get("supplier"):
			out.update(
				supplier=resolved["supplier"],
				confidence=resolved.get("confidence") or 0.0,
				method=resolved.get("method"),
				auto_link_safe=bool(resolved.get("auto_link_safe")),
			)
	name = card["company"]
	if name:
		for doctype, field, key in (("Customer", "customer_name", "customer"), ("Company", "company_name", "company")):
			if not compat.doctype_exists(doctype):
				continue
			hit = frappe.db.get_value(doctype, {field: name}, "name") or (
				name if frappe.db.exists(doctype, name) else None
			)
			if hit:
				out[key] = hit
	return out


def preview(card: dict) -> dict:
	"""What `save` would face: the duplicates and the match. Reads only."""
	cleaned = clean(card)
	return {"card": cleaned, "duplicates": duplicates(cleaned), "match": match(cleaned)}


# ── the write ───────────────────────────────────────────────────────────────
def save(user: str, card: dict, decision: dict | None = None, client_request_id: str = "",
	photos: list | None = None) -> dict:
	"""Create or update the Contact. See the module docstring for the two decisions."""
	decision = decision or {}
	key = _text(client_request_id, 100)
	ensure_fields()
	if key and compat.has_field(CONTACT, "card_client_request_id"):
		done = frappe.db.get_value(CONTACT, {"card_client_request_id": key}, "name")
		if done:
			return {**describe(done), "replayed": True, "created": False}

	cleaned = clean(card)
	merge_into = _text(decision.get("merge_into"))
	found = duplicates(cleaned)
	if merge_into:
		if not frappe.db.exists(CONTACT, merge_into):
			raise ToolError(f"no Contact called {merge_into!r} to merge into. Nothing was saved.")
	elif found and not decision.get("save_as_new"):
		names = ", ".join(f"{d['name']} ({'/'.join(d['matched_on'])})" for d in found)
		raise ToolError(
			f"this card matches {names}. Say merge_into (update that contact) or save_as_new. Nothing was saved.",
			"error.contact.duplicate",
		)

	link_to = decision.get("link_to") if isinstance(decision.get("link_to"), dict) else {}
	party = _party(cleaned, link_to, _text(decision.get("create_party")))

	doc = frappe.get_doc(CONTACT, merge_into) if merge_into else frappe.new_doc(CONTACT)
	_fill(doc, cleaned, user, key, merging=bool(merge_into))
	if party and not any(
		row.get("link_doctype") == party[0] and row.get("link_name") == party[1] for row in doc.get("links") or []
	):
		doc.append("links", {"link_doctype": party[0], "link_name": party[1]})
	doc.flags.ignore_permissions = True
	if merge_into:
		doc.save(ignore_permissions=True)
	else:
		doc.insert(ignore_permissions=True)

	address = _address(doc.name, cleaned, party)
	if cleaned["notes"]:
		_note(doc.name, cleaned["notes"], user)
	attached = _attach(doc.name, photos or [], user)
	return {
		**describe(doc.name),
		"created": not merge_into,
		"merged": bool(merge_into),
		"address": address,
		"photos_attached": attached,
		"linked_to": {"doctype": party[0], "name": party[1]} if party else None,
		"replayed": False,
	}


def _party(card: dict, link_to: dict, create: str):
	"""(doctype, name) to link, or None. Only what the caller named."""
	doctype = _text(link_to.get("doctype"))
	name = _text(link_to.get("name"))
	if doctype or name:
		if doctype not in LINKABLE or not name or not frappe.db.exists(doctype, name):
			raise ToolError(f"link_to must name an existing {', '.join(LINKABLE)}; got {doctype} {name!r}. Nothing was saved.")
		return doctype, name
	if create:
		if create not in ("Supplier", "Customer"):
			raise ToolError("create_party is Supplier or Customer. Nothing was saved.")
		if not card["company"]:
			raise ToolError(f"a {create} needs the card's company name. Nothing was saved.")
		from .tools import masters

		if create == "Supplier":
			made = masters.create_supplier({"supplier_name": card["company"]}).data
		else:
			made = masters.create_customer({"customer_name": card["company"]}).data
		return create, made.get("name") or card["company"]
	return None


def _fill(doc, card: dict, user: str, key: str, *, merging: bool) -> None:
	"""Write the card onto the Contact. Merging fills blanks and adds what is new; it never overwrites."""

	def put(field, value):
		if value and (not merging or not doc.get(field)) and (compat.has_field(CONTACT, field) or field in ("first_name", "last_name")):
			doc.set(field, value)

	put("first_name", card["first_name"] or card["company"])
	put("last_name", card["last_name"])
	put("designation", card["title"])
	put("company_name", card["company"])
	put("card_website", card["website"])
	put("card_met_at", card["met_at"])
	put("card_met_on", card["met_on"])
	if not merging:
		put("card_source", SOURCE)
		put("card_captured_by", user)
		put("card_client_request_id", key)
	have_emails = {str(r.get("email_id") or "").lower() for r in doc.get("email_ids") or []}
	for index, email in enumerate(card["emails"]):
		if email not in have_emails:
			doc.append("email_ids", {"email_id": email, "is_primary": 1 if not have_emails and index == 0 else 0})
	if card["emails"] and not doc.get("email_id"):
		doc.set("email_id", card["emails"][0])
	from .tools import receipts

	have_phones = {receipts.normalize_phone(r.get("phone")) for r in doc.get("phone_nos") or []}
	for phone in card["phones"]:
		if phone["digits"] in have_phones:
			continue
		doc.append(
			"phone_nos",
			{"phone": phone["number"], "is_primary_mobile_no": 1 if phone["kind"] == "mobile" else 0,
			 "is_primary_phone": 1 if phone["kind"] != "mobile" and not have_phones else 0},
		)
		have_phones.add(phone["digits"])


def _address(contact: str, card: dict, party) -> str | None:
	"""An Address linked to the Contact (and the party), when the card had one."""
	parts = card["address"]
	if not (parts["line1"] and parts["city"]) or not compat.doctype_exists(ADDRESS):
		return None
	country = parts["country"] or "United States"
	if compat.doctype_exists("Country") and not frappe.db.exists("Country", country):
		country = "United States" if frappe.db.exists("Country", "United States") else country
	doc = frappe.new_doc(ADDRESS)
	doc.address_title = card["company"] or f"{card['first_name']} {card['last_name']}".strip()
	doc.address_type = "Office"
	doc.address_line1 = parts["line1"]
	doc.address_line2 = parts["line2"] or None
	doc.city = parts["city"]
	doc.state = parts["state"] or None
	doc.pincode = parts["postal_code"] or None
	doc.country = country
	doc.append("links", {"link_doctype": CONTACT, "link_name": contact})
	if party:
		doc.append("links", {"link_doctype": party[0], "link_name": party[1]})
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _note(contact: str, text: str, user: str) -> None:
	try:
		frappe.get_doc(
			{"doctype": "Comment", "comment_type": "Comment", "reference_doctype": CONTACT,
			 "reference_name": contact, "content": text, "comment_email": user}
		).insert(ignore_permissions=True)
	except Exception:  # pragma: no cover - a site whose Comment refuses; the card is still filed
		pass


def _attach(contact: str, tokens: list, user: str) -> int:
	"""Attach the caller's own staged photos (front, back) to the Contact, private."""
	count = 0
	for token in tokens[:4]:
		token = _text(token)
		if not token or not frappe.db.exists("File", token):
			continue
		row = frappe.db.get_value("File", token, ["owner", "attached_to_doctype"], as_dict=True) or {}
		if row.get("owner") not in (user, "Administrator") or row.get("attached_to_doctype"):
			continue
		frappe.db.set_value("File", token, {"attached_to_doctype": CONTACT, "attached_to_name": contact,
			"is_private": 1})
		count += 1
	return count


# ── reading ─────────────────────────────────────────────────────────────────
def describe(name: str) -> dict:
	doc = frappe.get_doc(CONTACT, name)
	return {
		"name": doc.name,
		"first_name": doc.get("first_name") or None,
		"last_name": doc.get("last_name") or None,
		"title": doc.get("designation") or None,
		"company": doc.get("company_name") or None,
		"emails": [r.get("email_id") for r in doc.get("email_ids") or [] if r.get("email_id")]
		or ([doc.get("email_id")] if doc.get("email_id") else []),
		"phones": [r.get("phone") for r in doc.get("phone_nos") or [] if r.get("phone")],
		"links": [{"doctype": r.get("link_doctype"), "name": r.get("link_name")} for r in doc.get("links") or []],
		"source": doc.get("card_source") or None,
		"met_on": str(doc.get("card_met_on") or "") or None,
		"met_at": doc.get("card_met_at") or None,
		"website": doc.get("card_website") or None,
	}


def search(query: str = "", company: str = "", met_at: str = "", limit: int = 50) -> list:
	"""Contacts by name, company, email, phone or where met. Newest first."""
	from .tools import receipts

	if not compat.doctype_exists(CONTACT):
		return []
	text = _text(query).lower()
	digits = receipts.normalize_phone(text) if text else ""
	fields = compat.existing_fields(CONTACT, ("name", "first_name", "last_name", "company_name", "email_id",
		"card_met_at", "designation"))
	rows = frappe.db.get_all(CONTACT, fields=fields, order_by="creation desc", limit=5000) or []
	phones: dict = {}
	if digits and compat.doctype_exists("Contact Phone"):
		for row in frappe.db.get_all("Contact Phone", filters={"parenttype": CONTACT}, fields=["parent", "phone"], limit=20000) or []:
			phones.setdefault(row["parent"], []).append(receipts.normalize_phone(row.get("phone")))
	out = []
	for row in rows:
		row = dict(row)
		hay = " ".join(str(row.get(k) or "") for k in fields).lower()
		if text and text not in hay and not (digits and digits in phones.get(row["name"], [])):
			continue
		if company and company.lower() not in str(row.get("company_name") or "").lower():
			continue
		if met_at and met_at.lower() not in str(row.get("card_met_at") or "").lower():
			continue
		out.append(describe(row["name"]))
		if len(out) >= max(1, min(int(limit or 50), 200)):
			break
	return out
