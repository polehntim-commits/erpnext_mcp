# SPDX-License-Identifier: MIT
"""The customer / first-handler portal — the MCP side. v0.313.0 (docs/design/customer_portal_plan.md); was the packer
portal, v0.276.0.

Reads on; writes OFF until switched on. A credential is refused for a company until `customer_portal_enabled` (or the
older `packer_portal_enabled`) is on for it. Nothing here sends anything: the grower gives the customer their link.

THE v0.276.0 NAMES STAY CALLABLE (create_packer_share, issue_packer_credential, revoke_packer_access,
get_packer_share, get_packer_pack, list_packer_access_log): each is the generic tool under its old name, logged as
superseded, retired only after 30 days of no use and Tim's OK.
"""

from __future__ import annotations

import base64

import frappe

from .. import customer_portal
from ..args import as_int, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult

MAX_INLINE = 4 * 1024 * 1024


def _actor() -> str:
	from .. import security

	return security.caller_identity() or str(getattr(frappe.session, "user", "") or "")


def _wrap(fn):
	try:
		return fn()
	except customer_portal.PortalError as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None


def _portal(args: dict) -> str:
	name = as_str(args, "portal") or as_str(args, "share")
	if not name:
		raise ToolError("portal is required — the Customer Portal (CPT-…).")
	if not frappe.db.exists(customer_portal.SHARE, name):
		raise ToolError(f"no Customer Portal {name!r}.")
	return name


def _list(raw) -> list:
	if isinstance(raw, str):
		return [x.strip() for x in raw.replace("+", ",").split(",") if x.strip()]
	return list(raw or [])


# ── the generic tools ────────────────────────────────────────────────────────
def create_customer_portal(args: dict) -> ToolResult:
	company = resolve_company(as_str(args, "company"), required=True)
	customer = as_str(args, "customer", required=True)
	sections = args.get("sections") if isinstance(args.get("sections"), dict) else None
	name = _wrap(lambda: customer_portal.create_share(
		company, customer, _list(args.get("fields")), season=as_str(args, "season"), markets=_list(args.get("markets")),
		sections=sections, role=as_str(args, "role") or "Packer", display_name=as_str(args, "display_name"),
		title=as_str(args, "title"), actor=_actor()))
	doc = frappe.get_doc(customer_portal.SHARE, name)
	return ToolResult(data={"portal": name, "customer": doc.customer, "role": doc.customer_role,
	                        "blocks": [b.get("block_ticker") or b.get("field") for b in doc.get("blocks")],
	                        "portal_enabled": customer_portal.enabled(company)},
	                  summary=f"{name}: {len(doc.get('blocks'))} block(s) for {doc.customer_name} ({doc.customer_role})",
	                  docstatus_delta="0 → 0 (created)")


def issue_portal_credential(args: dict) -> ToolResult:
	portal = _portal(args)
	data = _wrap(lambda: customer_portal.issue_credential(portal, as_str(args, "contact_name", required=True),
	                                                      as_str(args, "contact_email"),
	                                                      as_int(args, "days", customer_portal.CREDENTIAL_DAYS),
	                                                      _actor()))
	data["portal"] = data.pop("share")
	return ToolResult(data=data, summary=f"credential for {data['contact']} — shown once, live until {data['expires_at']}",
	                  docstatus_delta="0 → 0 (created)")


def revoke_portal_access(args: dict) -> ToolResult:
	portal = _portal(args)
	data = _wrap(lambda: customer_portal.revoke(portal, as_str(args, "contact_name"), as_str(args, "reason"), _actor()))
	data["portal"] = data.pop("share")
	return ToolResult(data=data, summary=f"{portal}: {data['revoked']} credential(s) revoked; portal {data['status']}",
	                  docstatus_delta="0 → 0 (updated)")


def get_customer_portal(args: dict) -> ToolResult:
	portal = _portal(args)
	doc = frappe.get_doc(customer_portal.SHARE, portal)
	creds = customer_portal.credentials(portal)
	data = {"portal": portal, "customer": doc.customer, "customer_name": doc.customer_name,
	        "role": doc.get("customer_role"), "company": doc.company, "status": doc.status, "credentials": creds,
	        "page": customer_portal.page(portal, as_str(args, "season"))}
	return ToolResult(data=data, summary=f"{portal}: {doc.customer_name}, {len(creds)} credential(s)")


def get_customer_portal_pack(args: dict) -> ToolResult:
	portal = _portal(args)
	fmt = as_str(args, "format") or "json"
	body, kind, name = _wrap(lambda: customer_portal.pack(portal, block=as_str(args, "block"),
	                                                       season=as_str(args, "season"), fmt=fmt))
	if fmt == "json":
		import json

		return ToolResult(data={"file_name": name, "content_type": kind, "data": json.loads(body)},
		                  summary=f"{name}: the pack as JSON")
	if len(body) > MAX_INLINE:
		raise ToolError(f"{name} is {len(body)} bytes — over the 4 MB inline cap; download it from the portal.")
	return ToolResult(data={"file_name": name, "content_type": kind, "size": len(body),
	                        "content_base64": base64.b64encode(body).decode()},
	                  summary=f"{name}: {len(body)} bytes ({kind})")


def list_portal_access_log(args: dict) -> ToolResult:
	rows = customer_portal.access_log(as_str(args, "portal") or as_str(args, "share"), as_str(args, "company"),
	                                  as_int(args, "limit", 200))
	return ToolResult(data={"entries": rows, "count": len(rows)}, summary=f"{len(rows)} access(es)")


# ── the v0.276.0 names, kept ─────────────────────────────────────────────────
def create_packer_share(args: dict) -> ToolResult:
	"""v0.276.0's create: `packer_name` is the name shown; a Customer is found or must be named."""
	customer = as_str(args, "customer")
	packer_name = as_str(args, "packer_name")
	if not customer and packer_name:
		customer = frappe.db.get_value("Customer", {"customer_name": packer_name}, "name") or (
			packer_name if frappe.db.exists("Customer", packer_name) else "")
	if not customer:
		raise ToolError(f"a portal is for an ERPNext Customer — {packer_name!r} is not one. Pass customer (or create the "
		                "Customer first). Nothing was changed.")
	return create_customer_portal({**args, "customer": customer, "role": "Packer",
	                               "display_name": packer_name or as_str(args, "display_name")})


def issue_packer_credential(args: dict) -> ToolResult:
	return issue_portal_credential(args)


def revoke_packer_access(args: dict) -> ToolResult:
	return revoke_portal_access(args)


def get_packer_share(args: dict) -> ToolResult:
	return get_customer_portal(args)


def get_packer_pack(args: dict) -> ToolResult:
	return get_customer_portal_pack(args)


def list_packer_access_log(args: dict) -> ToolResult:
	return list_portal_access_log(args)
