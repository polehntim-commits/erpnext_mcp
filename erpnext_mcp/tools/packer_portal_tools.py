# SPDX-License-Identifier: MIT
"""The packer portal — the MCP side. v0.276.0 (docs/contracts/packer_portal_v0_276.yaml).

Reads on; writes OFF until switched on. A credential is refused for a company until `packer_portal_enabled` is on
for it. Nothing here sends anything: Tim gives the packer their link.
"""

from __future__ import annotations

import base64

import frappe

from .. import compat, packer_portal
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
	except packer_portal.PortalError as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None


def _share(args: dict) -> str:
	name = as_str(args, "share", required=True)
	if not frappe.db.exists(packer_portal.SHARE, name):
		raise ToolError(f"no Packer Share {name!r}.")
	return name


def create_packer_share(args: dict) -> ToolResult:
	company = resolve_company(as_str(args, "company"), required=True)
	fields = args.get("fields") or []
	if isinstance(fields, str):
		fields = [f.strip() for f in fields.replace("+", ",").split(",") if f.strip()]
	markets = args.get("markets") or []
	if isinstance(markets, str):
		markets = [m.strip() for m in markets.split(",") if m.strip()]
	sections = args.get("sections") if isinstance(args.get("sections"), dict) else None
	name = _wrap(lambda: packer_portal.create_share(company, as_str(args, "packer_name", required=True), fields,
	                                                season=as_str(args, "season"), markets=markets, sections=sections,
	                                                customer=as_str(args, "customer"), title=as_str(args, "title"),
	                                                actor=_actor()))
	doc = frappe.get_doc(packer_portal.SHARE, name)
	return ToolResult(data={"share": name, "blocks": [b.get("block_ticker") or b.get("field") for b in doc.get("blocks")],
	                        "portal_enabled": packer_portal.enabled(company)},
	                  summary=f"{name}: {len(doc.get('blocks'))} block(s) for {doc.packer_name}",
	                  docstatus_delta="0 → 0 (created)")


def issue_packer_credential(args: dict) -> ToolResult:
	share = _share(args)
	data = _wrap(lambda: packer_portal.issue_credential(share, as_str(args, "contact_name", required=True),
	                                                    as_str(args, "contact_email"),
	                                                    as_int(args, "days", packer_portal.CREDENTIAL_DAYS), _actor()))
	return ToolResult(data=data, summary=f"credential for {data['contact']} — shown once, live until {data['expires_at']}",
	                  docstatus_delta="0 → 0 (created)")


def revoke_packer_access(args: dict) -> ToolResult:
	share = _share(args)
	data = _wrap(lambda: packer_portal.revoke(share, as_str(args, "contact_name"), as_str(args, "reason"), _actor()))
	return ToolResult(data=data, summary=f"{share}: {data['revoked']} credential(s) revoked; share {data['status']}",
	                  docstatus_delta="0 → 0 (updated)")


def get_packer_share(args: dict) -> ToolResult:
	share = _share(args)
	doc = frappe.get_doc(packer_portal.SHARE, share)
	creds = [{"contact": c.get("contact_name"), "email": c.get("contact_email"), "issued_at": str(c.get("issued_at") or ""),
	          "expires_at": str(c.get("expires_at") or ""), "revoked": bool(compat.checked(c.get("revoked"))),
	          "last_used": str(c.get("last_used") or "") or None, "uses": int(c.get("uses") or 0)}
	         for c in doc.get("credentials") or []]
	data = {"share": share, "packer": doc.packer_name, "company": doc.company, "status": doc.status,
	        "credentials": creds, "page": packer_portal.page(share, as_str(args, "season"))}
	return ToolResult(data=data, summary=f"{share}: {doc.packer_name}, {len(creds)} credential(s)")


def get_packer_pack(args: dict) -> ToolResult:
	share = _share(args)
	fmt = as_str(args, "format") or "json"
	body, kind, name = _wrap(lambda: packer_portal.pack(share, block=as_str(args, "block"), season=as_str(args, "season"),
	                                                     fmt=fmt))
	if fmt == "json":
		import json

		return ToolResult(data={"file_name": name, "content_type": kind, "data": json.loads(body)},
		                  summary=f"{name}: the pack as JSON")
	if len(body) > MAX_INLINE:
		raise ToolError(f"{name} is {len(body)} bytes — over the 4 MB inline cap; download it from the portal.")
	return ToolResult(data={"file_name": name, "content_type": kind, "size": len(body),
	                        "content_base64": base64.b64encode(body).decode()},
	                  summary=f"{name}: {len(body)} bytes ({kind})")


def list_packer_access_log(args: dict) -> ToolResult:
	rows = packer_portal.access_log(as_str(args, "share"), as_str(args, "company"), as_int(args, "limit", 200))
	return ToolResult(data={"entries": rows, "count": len(rows)}, summary=f"{len(rows)} access(es)")
