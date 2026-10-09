# SPDX-License-Identifier: MIT
"""One engine for every link handed to someone outside the farm. v0.285.0 (pattern P2;
docs/design/plan_patterns_consolidation_2026_10.md §1).

v0.271's Contractor Job Link and v0.276's packer credential were two copies of one idea; they are now one Share Link
record (and one Share Link View log) with a KIND. What a kind's page shows, which actions it allows and whether its
target is still live stay with the kind (`job_links`, `packer_portal`); what every kind shares lives here:

- THE TOKEN IS A CREDENTIAL. 32 random bytes, shown ONCE; only its SHA-256 is kept, unique and indexed, so a lookup
  is one row whatever the number of shares. Every failure — unknown, revoked, expired, target closed, company's
  sharing off — answers the same None.
- EXPIRY: a kind's default life, capped by its maximum; a kind may cap it further (a job's end date).
- REVOKE one link, or every link to a target (optionally one contact's). A revoked link stays revoked; extend never
  revives it.
- EVERY USE IS A ROW in Share Link View (append-only, its own table — no parent document rewritten per view), plus a
  use count and last-used stamp on the link.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import secrets

import frappe

LINK = "Share Link"
VIEW = "Share Link View"
#: kind → its target doctype, public path and life. The kind's own module decides what the page shows.
KINDS = {
	"Job": {"doctype": "Contractor Job", "path": "job", "default_days": 30, "max_days": 365},
	"Packer": {"doctype": "Packer Share", "path": "packer", "default_days": 400, "max_days": 800},
}
LINK_FIELDS = ["name", "kind", "target_doctype", "target", "company", "contact_name", "contact_email", "status",
               "issued_by", "issued_at", "expires_at", "revoked_at", "revoked_by", "revoked_reason", "use_count",
               "last_used_at"]


class ShareLinkError(ValueError):
	pass


def hash_token(token: str) -> str:
	return hashlib.sha256(str(token or "").encode()).hexdigest()


def _now() -> datetime.datetime:
	return frappe.utils.get_datetime(frappe.utils.now())


def _stamp(value: datetime.datetime) -> str:
	return value.strftime("%Y-%m-%d %H:%M:%S")


def url(kind: str, token: str) -> str:
	from . import settings

	base = settings.farmops_public_url() or str(frappe.utils.get_url() or "").rstrip("/")
	return f"{base}/farmops/api/{KINDS[kind]['path']}/{token}"


def issue(kind: str, target: str, *, company: str, actor: str, days: int | None = None, not_after=None,
          contact_name: str = "", contact_email: str = "") -> dict:
	"""A new link — the token is in the answer and nowhere else."""
	spec = KINDS[kind]
	life = spec["default_days"] if days is None else int(days)
	life = max(1, min(life, spec["max_days"]))
	expires = _now() + datetime.timedelta(days=life)
	if not_after is not None:
		expires = min(expires, not_after)
	token = secrets.token_urlsafe(32)
	doc = frappe.new_doc(LINK)
	doc.update({"kind": kind, "target_doctype": spec["doctype"], "target": target, "company": company or None,
	            "contact_name": (contact_name or "").strip()[:80] or None, "contact_email": contact_email or None,
	            "status": "Active", "token_hash": hash_token(token), "issued_by": actor or None,
	            "issued_at": frappe.utils.now(), "expires_at": _stamp(expires), "use_count": 0})
	doc.insert(ignore_permissions=True)
	return {"link": doc.name, "token": token, "url": url(kind, token), "expires_at": doc.expires_at}


def find(token: str, kind: str) -> dict | None:
	"""The live link of this kind, or None — the same None for every reason. Expiry is recorded when seen."""
	token = str(token or "")
	if len(token) < 20 or len(token) > 200:
		return None
	digest = hash_token(token)
	rows = frappe.db.get_all(LINK, filters={"token_hash": digest}, fields=[*LINK_FIELDS, "token_hash"], limit=1) or []
	if not rows or not hmac.compare_digest(str(rows[0].get("token_hash") or ""), digest):
		return None
	row = dict(rows[0])
	row.pop("token_hash", None)
	if row.get("kind") != kind or row.get("status") != "Active":
		return None
	if row.get("expires_at") and frappe.utils.get_datetime(row["expires_at"]) < _now():
		frappe.db.set_value(LINK, row["name"], "status", "Expired")
		return None
	return row


def record_use(link: dict, what: str, *, detail: str = "", ip: str = "", user_agent: str = "") -> None:
	"""One Share Link View row, and the link's count and last-used stamp."""
	now = frappe.utils.now()
	frappe.get_doc({"doctype": VIEW, "share_link": link["name"], "kind": link.get("kind"), "target": link.get("target"),
	                "company": link.get("company") or None, "contact_name": link.get("contact_name") or None,
	                "at": now, "what": str(what or "")[:40], "detail": str(detail or "")[:140] or None,
	                "ip": str(ip or "")[:60] or None, "user_agent": str(user_agent or "")[:140] or None}
	               ).insert(ignore_permissions=True)
	count = int(frappe.db.get_value(LINK, link["name"], "use_count") or 0)
	frappe.db.set_value(LINK, link["name"], {"use_count": count + 1, "last_used_at": now})


def extend(link: str, actor: str, *, days: int = 7, until=None) -> dict:
	doc = frappe.get_doc(LINK, link)
	if doc.status == "Revoked":
		raise ShareLinkError("a revoked link stays revoked — issue a new one.")
	new = until if until is not None else _now() + datetime.timedelta(days=max(1, int(days)))
	cap = _now() + datetime.timedelta(days=KINDS[doc.kind]["max_days"])
	doc.expires_at = _stamp(min(new, cap))
	doc.status = "Active"
	doc.save(ignore_permissions=True)
	return {"link": link, "expires_at": doc.expires_at, "extended_by": actor}


def _revoke_doc(doc, actor: str, reason: str) -> None:
	doc.status = "Revoked"
	doc.revoked_at = frappe.utils.now()
	doc.revoked_by = actor or None
	doc.revoked_reason = (reason or "revoked")[:500]
	doc.save(ignore_permissions=True)


def revoke(link: str, actor: str, reason: str = "") -> dict:
	_revoke_doc(frappe.get_doc(LINK, link), actor, reason)
	return {"link": link, "status": "Revoked"}


def revoke_target(kind: str, target: str, actor: str, reason: str = "", contact_name: str = "") -> int:
	"""Revoke every live link to a target (or one contact's). Returns how many."""
	filters = {"kind": kind, "target": target, "status": "Active"}
	if contact_name:
		filters["contact_name"] = contact_name
	names = frappe.db.get_all(LINK, filters=filters, pluck="name") or []
	for name in names:
		_revoke_doc(frappe.get_doc(LINK, name), actor, reason)
	return len(names)


def expire_target(kind: str, target: str) -> int:
	"""Close a target's live links as Expired (the job closed) — not a revocation."""
	names = frappe.db.get_all(LINK, filters={"kind": kind, "target": target, "status": "Active"}, pluck="name") or []
	for name in names:
		frappe.db.set_value(LINK, name, "status", "Expired")
	return len(names)


def _plain(row: dict) -> dict:
	return {k: (str(v) if isinstance(v, (datetime.date, datetime.datetime)) else v) for k, v in dict(row).items()}


def links(kind: str, *, target: str = "", company: str = "", limit: int = 500) -> list:
	filters = {"kind": kind}
	if target:
		filters["target"] = target
	if company:
		filters["company"] = company
	return [_plain(r) for r in frappe.db.get_all(LINK, filters=filters, fields=LINK_FIELDS, order_by="issued_at desc",
	                                             limit=limit) or []]


def views(kind: str, *, link: str = "", target: str = "", company: str = "", limit: int = 200) -> list:
	filters = {"kind": kind}
	if link:
		filters["share_link"] = link
	if target:
		filters["target"] = target
	if company:
		filters["company"] = company
	rows = frappe.db.get_all(VIEW, filters=filters, fields=["share_link", "target", "contact_name", "at", "what",
	                                                         "detail", "ip", "user_agent"],
	                         order_by="at desc", limit=max(1, min(int(limit), 5000))) or []
	return [_plain(r) for r in rows]
