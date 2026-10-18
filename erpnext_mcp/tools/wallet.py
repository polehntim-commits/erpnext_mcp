# SPDX-License-Identifier: MIT
"""The badge in a worker's own wallet. One tool. v0.53.0.

`generate_employee_badge_pass` is `generate_employee_badge_qr` with a different
delivery: same register row, same minted `CFL-0001`, same QR payload, wrapped in
an Apple Wallet `.pkpass` file and a Google Wallet save link instead of a PNG to
print. The foreman finishes onboarding, hits this, and AirDrops the file to the
worker standing in front of them — it opens straight into Wallet with no app to
install on the worker's side, which is the whole reason this exists rather than
a second handset app.

────────────────────────────────────────────────────────────────────────────
IT DELEGATES THE BADGE. IT DOES NOT HAVE ITS OWN
────────────────────────────────────────────────────────────────────────────

The identifier, the minting, the register row and the retirement of a replaced
card are `badges.py`'s, called here through the same two functions
`generate_employee_badge_qr` uses — `_choose_badge_id` then `_record_badge`. So
this tool is IDEMPOTENT in exactly the same way: somebody who already holds a
live badge gets a pass for THAT badge rather than a second identifier, and
`regenerate=true` is the lost-card path that mints one and retires the old.

That is not code-sharing for its own sake. A worker with a printed card and a
wallet pass is holding TWO copies of one badge, and the moment those could
diverge is the moment a bucket scanned off the phone pays somebody the card
does not. There is one identifier and both artefacts render it.

────────────────────────────────────────────────────────────────────────────
WHAT IS WRITTEN, AND THE ONE FILE THAT IS REPLACED
────────────────────────────────────────────────────────────────────────────

The `.pkpass` is attached PRIVATELY to the Employee, named `badge-<badge_id>.pkpass`.
Regenerating replaces THAT file — matched on the exact generated name, on that
Employee, and on the `.pkpass` extension, so nothing else attached to the record
can be caught by it. The alternative is an attachment list that grows one file
every time a foreman presses the button on a bad connection, all of them
identical except the one that is current.

The pass is a DERIVED artefact and the register row is the record. Losing the
File loses nothing — this tool rebuilds it byte-identically from the same badge
(see `wallet.zip_pass` on why the archive is deterministic).

────────────────────────────────────────────────────────────────────────────
AN UNSIGNED PASS IS RETURNED AND SAID TO BE UNSIGNED
────────────────────────────────────────────────────────────────────────────

A site with no Apple certificate configured gets a complete, correct, UNSIGNED
`.pkpass` and a result that says `apple.signed: false` with the reason and the
`site_config.json` keys in it. Wallet will refuse to open that file. This is
deliberate and is argued in `wallet.py`'s header: the honest failure is one an
operator can act on, and a self-signed blob with a `signature` member in it
fails identically while looking upstream like it worked.

So the feature ships complete and inert, and it becomes live the day a
certificate lands in `site_config.json` — no code change. `docs/wallet-passes.md`
is what Tim needs to obtain and where to put it.
"""

from __future__ import annotations

import base64
import datetime
import hashlib

import frappe

from .. import compat, settings, wallet
from ..args import as_bool, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import badges
from . import employee as employee_tool
from . import files as files_tool

EMPLOYEE = badges.EMPLOYEE

#: How the attached file is named. The badge is in the name so that an operator
#: reading an Employee's attachment list can see which card the pass is for, and
#: so that a reissued badge produces a NEW file rather than silently overwriting
#: the pass for the badge it replaced — the old one is stale but it is evidence
#: of what was issued, and `_replace_existing` only ever matches its own name.
PASS_FILE_TEMPLATE = "badge-{badge_id}.pkpass"

#: The Apple UTI. Returned in the result because it is what makes an AirDropped
#: file open in Wallet rather than in Files: iOS routes on the type, and a phone
#: that writes these bytes to disk without declaring it hands the worker a zip.
PKPASS_CONTENT_TYPE = "application/vnd.apple.pkpass"

#: The most base64 this tool will put in one result. A pass built from
#: normalised images is 8-40 KB, so nothing real approaches it; the ceiling
#: exists so that an unforeseen giant does not silently become a megabyte in a
#: model's context or on a thin field link. Over it, the bytes are on the File
#: and the caller is told to fetch them there.
MAX_INLINE_BYTES = 800 * 1024

#: What `platform` accepts. `both` is the default because the foreman does not
#: know which handset the worker in front of them has, and building the Google
#: object costs nothing — it is JSON shaping, no signing unless configured.
PLATFORMS = ("both", "apple", "google")


def _issued_at() -> int:
	"""Now, as a Unix timestamp, from Frappe's clock rather than the module's.

	`wallet.build_google_pass` takes this as an argument precisely so that the
	pure module has no clock — see its docstring. Two fallbacks because
	`now_datetime` is not on every Frappe version's `utils`, and a JWT with
	`iat: 0` is still a JWT Google accepts.
	"""
	try:
		return int(frappe.utils.now_datetime().timestamp())
	except Exception:
		pass
	try:
		return int(datetime.datetime.fromisoformat(str(frappe.utils.now())).timestamp())
	except Exception:  # pragma: no cover - a clock this app cannot read
		return 0


def _file_bytes(file_url: str) -> tuple:
	"""The bytes behind a `file_url`, or (b"", reason). NEVER a refusal.

	Both images this tool wants — the employee photograph and the company's badge
	logo — are optional on the card and optional on the pass. `badges._card` has
	always fallen back to initials for a missing photograph and to no mark for a
	missing logo, and a pass is the same card. So every way this can fail — no
	File row for a URL somebody pasted, a File whose bytes are gone from disk, a
	private File this caller may not read — returns the reason as a STRING for
	the result's `warnings`, and the badge still gets issued.
	"""
	url = str(file_url or "").strip()
	if not url:
		return b"", ""
	name = str(frappe.db.get_value("File", {"file_url": url}, "name") or "")
	if not name:
		return b"", f"no File on this site has the URL {url!r}, so that image is not on the pass"
	try:
		return files_tool.read_file_bytes(name), ""
	except Exception as exc:
		return (
			b"",
			f"{url!r} could not be read ({type(exc).__name__}: {exc}), so that image is not on the pass",
		)


def _public_base() -> str:
	"""An HTTPS base URL Google's image fetcher could actually reach, or "".

	HTTPS IS REQUIRED AND IS NOT A PREFERENCE. Google refuses `http://` image
	URIs on a pass object, so a site reachable only over plain HTTP has no images
	on the Android half whatever it configures — better said in a warning than
	discovered as a grey box on a worker's phone.

	`public_url` from the settings form comes first because it is the field an
	operator fills in for exactly this class of problem: a site behind a tunnel
	or a reverse proxy cannot work out its own public name from inside a request,
	and `frappe.utils.get_url()` will confidently answer with the internal one.
	"""
	for candidate in (settings.public_url(), _site_url()):
		base = str(candidate or "").strip().rstrip("/")
		if base.lower().startswith("https://"):
			return base
	return ""


def _site_url() -> str:
	try:
		return str(frappe.utils.get_url() or "")
	except Exception:  # pragma: no cover - a site with no resolvable URL
		return ""


def _public_image_url(file_url: str, base: str) -> str:
	"""An absolute URL for an image Google can fetch, or "" — see `_public_base`.

	A Frappe File is public or private, and the distinction is the URL prefix:
	`/files/…` is served to anybody, `/private/files/…` needs a session. Google's
	fetcher has no session. So a private photograph — which is what
	`set_employee_photo` writes, correctly, because it is a picture of a person —
	simply cannot be on a Google Wallet pass, and the caller is told so rather
	than being given an object with a URL that 403s.
	"""
	url = str(file_url or "").strip()
	if not base or not url.startswith("/files/"):
		return ""
	return f"{base}{url}"


def _card(row: dict, badge_id: str, company: str) -> tuple:
	"""Everything the pass builders need about one badge, and what went wrong.

	The same facts `badges._card` puts on a printed card, plus the image BYTES —
	which the printed card never needed, because a print template is handed a URL
	and fetches it itself, and a `.pkpass` has to carry the pixels inside the
	archive.
	"""
	warnings = []
	photo_url = str(row.get("image") or "")
	logo_url = badges._company_logo(company)

	photo, photo_problem = _file_bytes(photo_url)
	if photo_problem:
		warnings.append(photo_problem)
	logo, logo_problem = _file_bytes(logo_url)
	if logo_problem:
		warnings.append(logo_problem)

	base = _public_base()
	if not base and (photo_url or logo_url):
		warnings.append(
			"this site has no HTTPS URL configured (Public URL in ERPNext MCP Settings), so the "
			"Google Wallet pass carries no images — Google fetches them over the public internet "
			"rather than being sent them. The Apple pass is unaffected; its images are inside the file."
		)

	name = row.get("employee_name") or row["name"]
	return {
		"badge_id": badge_id,
		"employee": row["name"],
		"employee_name": name,
		"employee_number": row.get("employee_number") or "",
		"designation": row.get("designation") or "",
		"company": company,
		"initials": badges._initials(name),
		"issued_on": frappe.utils.today(),
		"photo_png": photo,
		"logo_png": logo,
		"public_photo_url": _public_image_url(photo_url, base),
		"public_logo_url": _public_image_url(logo_url, base),
	}, warnings


def _replace_existing(employee: str, file_name: str) -> list:
	"""Delete the pass this call is about to replace. Returns what it removed.

	NARROW ON PURPOSE — the exact generated name, on this Employee, ending
	`.pkpass`. A regenerated pass for the same badge is byte-identical apart from
	nothing at all, so keeping both copies is keeping a duplicate; a pass for a
	DIFFERENT badge has a different name and is left alone, because it is the
	record of what was issued before the reissue.

	`force=True` because a File attached to an Employee has no dependent links to
	protect and Frappe's link check would otherwise refuse on a document nobody
	is pointing at.
	"""
	removed = []
	rows = frappe.db.get_all(
		"File",
		filters={"attached_to_doctype": EMPLOYEE, "attached_to_name": employee, "file_name": file_name},
		fields=["name", "file_url"],
		limit_page_length=0,
	)
	for row in rows or []:
		if not str(row.get("file_url") or "").endswith(".pkpass"):
			continue
		frappe.delete_doc("File", row["name"], force=True, ignore_permissions=True)
		removed.append(row["name"])
	return removed


def _apple(card: dict, attach_to: str, inline: bool) -> dict:
	"""Build, attach and describe the `.pkpass`."""
	config = wallet.apple_config(dict(frappe.conf or {}))
	built = wallet.build_pkpass(card, config)
	payload = built["pkpass"]

	file_name = PASS_FILE_TEMPLATE.format(badge_id=card["badge_id"])
	replaced = _replace_existing(attach_to, file_name) if attach_to else []
	attachment = (
		files_tool.insert_attachment(
			file_name, payload, is_private=True, doctype=EMPLOYEE, name=attach_to, parent_authorized=True
		)
		if attach_to
		else None
	)

	result = {
		"file_name": file_name,
		"content_type": PKPASS_CONTENT_TYPE,
		"file_url": attachment.get("file_url") if attachment else None,
		"file": attachment.name if attachment else None,
		"replaced_files": replaced,
		"bytes": len(payload),
		"sha256": hashlib.sha256(payload).hexdigest(),
		"signed": built["signed"],
		"configured": bool(config["configured"]),
		"pass_type_identifier": built["pass_type_identifier"],
		"team_identifier": config["team_identifier"],
		"serial_number": built["serial_number"],
		"members": built["members"],
		"pass_json": built["pass_json"],
		"warnings": list(built["warnings"]),
	}
	if inline:
		if len(payload) <= MAX_INLINE_BYTES:
			result["pkpass_base64"] = base64.b64encode(payload).decode("ascii")
		else:
			result["warnings"].append(
				f"the pass is {len(payload)} bytes, over the {MAX_INLINE_BYTES}-byte inline ceiling, "
				"so it is on the Employee record rather than in this result."
			)
	if not config["configured"]:
		result["requires"] = wallet.SIGNING_REQUIREMENTS
	return result


def _google(card: dict) -> dict:
	config = wallet.google_config(dict(frappe.conf or {}))
	built = wallet.build_google_pass(card, config, issued_at=_issued_at())
	result = {
		"save_url": built["save_url"] or None,
		"signed": built["signed"],
		"configured": bool(config["configured"]),
		"issuer_id": config["issuer_id"] or None,
		"object_id": built["object_id"],
		"class_id": built["class_id"],
		"object": built["object"],
		"class": built["class"],
		"warnings": list(built["warnings"]),
	}
	if not config["configured"]:
		result["requires"] = wallet.GOOGLE_REQUIREMENTS
	return result


def generate_employee_badge_pass(args: dict) -> ToolResult:
	"""MUTATING (default OFF). The employee's badge as an Apple Wallet `.pkpass`
	file and a Google Wallet save link — the same badge ID, the same QR and the
	same photograph as the printed card, in the wallet the worker already has on
	their phone.

	IDEMPOTENT WITHOUT `regenerate`, exactly like `generate_employee_badge_qr`,
	because it is the same call underneath: somebody holding a live badge gets a
	pass for THAT badge rather than a second identifier. `regenerate=true` mints
	a new ID and retires the old one, which is the lost-card path.

	A SITE WITH NO APPLE CERTIFICATE STILL GETS A PASS, marked `signed: false`
	with the reason and the config keys in the result. Apple Wallet will refuse to
	open it. That is the honest state of an unconfigured site, and it is what
	makes this feature something an operator can inspect before committing to a
	developer account."""
	badges._require()
	actor = employee_tool.require_hr_role()

	row = badges._employee_row(as_str(args, "employee", required=True))
	company = resolve_company(as_str(args, "company") or str(row.get("company") or ""), required=True)
	employee_tool.require_company_scope(actor, company)

	if row.get("company") and str(row["company"]) != company:
		raise ToolError(
			f"{row['name']} is employed by {row['company']} and this call names {company}. A badge "
			"belongs to the entity that issued it and resolves only against that entity's "
			"captures. Nothing was changed."
		)

	status = str(row.get("status") or "Active")
	if status != "Active":
		raise ToolError(
			f"{row['name']} ({row.get('employee_name')}) has employment status {status}, not "
			"Active. A wallet pass is a live credential for piece-work attribution — "
			"reactivate_employee first. Nothing was changed."
		)

	platform = (as_str(args, "platform") or "both").lower()
	if platform not in PLATFORMS:
		raise ToolError(f"platform must be one of {', '.join(PLATFORMS)}. Nothing was changed.")
	attach = as_bool(args, "attach", True)
	inline = as_bool(args, "include_base64", False)

	# The badge is settled and recorded BEFORE anything is built, the same order
	# `generate_employee_badge_qr` uses and for the same reason: the register is
	# the record, and a pass built against an identifier that was never written
	# is a credential for a badge nobody holds.
	badge_id, created = badges._choose_badge_id(row, company, args)
	recorded = badges._record_badge(row, company, badge_id, args)

	card, warnings = _card(row, badge_id, company)
	apple = _apple(card, row["name"] if attach else "", inline) if platform in ("both", "apple") else None
	google = _google(card) if platform in ("both", "google") else None

	data = {
		"actor": actor,
		"employee": row["name"],
		"employee_name": card["employee_name"],
		"employee_number": card["employee_number"] or None,
		"designation": card["designation"] or None,
		"company": company,
		"badge_id": badge_id,
		"created": created,
		"reused": not created,
		"retired_badges": recorded["retired"],
		"platform": platform,
		"apple": apple,
		"google": google,
		# Card-level warnings — a photograph that could not be read, a site with
		# no public URL. The per-platform lists carry the ones specific to each.
		"warnings": warnings,
	}

	verb = "issued" if created else "reused"
	summary = f"wallet pass for badge {badge_id} ({card['employee_name']}), badge {verb}"
	if apple and not apple["signed"]:
		summary += "; UNSIGNED — Apple Wallet will refuse it until a certificate is configured"
	if google and not google["signed"]:
		summary += "; no Google save link"
	return ToolResult(
		data=data,
		summary=summary,
		docstatus_delta=("none → 0 (badge issued)" if created else "pass rebuilt for an existing badge"),
	)


def pass_available() -> bool:
	"""Predicate: can this site build a wallet pass at all?

	THE REGISTER, AND ONLY THE REGISTER — one prerequisite where
	`generate_employee_badge_qr` has two, and the difference is worth naming
	because it looks like an omission. That tool needs a QR ENCODER because it
	draws the symbol into a PNG somebody prints. This one does not draw anything:
	a wallet pass DECLARES its barcode (`PKBarcodeFormatQR` and the message) and
	Wallet renders it on the device. So a bench with neither `segno` nor `qrcode`
	can still put a badge on a worker's phone, and gating this on an encoder it
	never calls would take that away for nothing.

	`cryptography` is not in here either, and that is the same judgement in a
	different place: a bench without it loses the SIGNATURE, which this tool
	already reports as `signed: false` in a sentence naming what to fix. Gating
	the whole tool on it would take away the unsigned pass an operator uses to
	check the shape before committing to a developer account.
	"""
	try:
		return bool(compat.doctype_exists(badges.BADGE_DOCTYPE))
	except Exception:  # pragma: no cover - a site whose meta cannot be read
		return False
