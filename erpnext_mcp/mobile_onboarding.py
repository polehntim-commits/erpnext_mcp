# SPDX-License-Identifier: MIT
"""Enrolling a phone, as a page somebody can stand at with the worker.

Item 20 of the update plan. Everything this page does, the tools in
`tools/mobile.py` have done since v0.17.0: `create_mobile_user` makes the
account, assigns the role, writes a User Permission per entity and mints the
credential; `generate_mobile_login_qr` draws the card the phone scans;
`list_mobile_users` reports who is enrolled and what is wrong with any of it.

WHAT WAS MISSING WAS A PLACE FOR A PERSON TO DO IT FROM. Enrolment happens on
the morning of somebody's first day, in a farm office, with the worker standing
there holding their phone. The two surfaces that could do it were an MCP client
and `bench console`, and neither is a thing an office manager has open. So the
work went either undone or through somebody who had to be interrupted for it,
which for the one step that gates every other feature on the handset is the
worst possible place to put a bottleneck.

────────────────────────────────────────────────────────────────────────────
A WRAPPER, AND NOT A SECOND IMPLEMENTATION
────────────────────────────────────────────────────────────────────────────

The rule `badge_sheet.render_badge_sheet` states and `asset_tag_sheet` repeats:
every gate that matters belongs to the TOOL. The role catalogue, the refusal to
create an account with no entity scoping, the HTTPS-only endpoint check, the
one-week ceiling on how long a card stays valid, the "this user already exists,
say so on purpose" refusal — every one of them is `tools/mobile.py`'s, every one
of them still runs, and nothing here weakens any of it.

This module adds exactly one gate of its own, and it is the one the tools cannot
make: a **session**. An MCP tool is reached through the shared token and the
CIDR allow-list and runs as this app's own System User; a Desk caller is a person
Frappe has already identified. So the question "may THIS person enrol somebody"
has no answer inside the tool and has to be asked here.

────────────────────────────────────────────────────────────────────────────
THE GATE IS FRAPPE'S OWN PERMISSION TABLE, AND THAT IS DELIBERATE
────────────────────────────────────────────────────────────────────────────

`frappe.only_for("System Manager")` would have been one line. It is the wrong
line, because it puts the decision in code an operator cannot see and cannot
change: a farm that wants its office manager to enrol pickers would need a
release.

So enrolment asks for **create permission on `Mobile Access Grant`** — this app's
own register, which ships System-Manager-only and can be widened by adding a row
to its permission table in the Desk — **and create permission on `User`**, which
is Frappe's. Both, because the page does both things: it writes a grant and it
creates a login. The second is the important half. It means this page can never
produce an account the caller could not have produced by hand on the User form,
so no amount of widening the first grants anybody an escalation they did not
already have.

Reading the roster asks for read on `Mobile Access Grant` and nothing else.

THE PAGE RECORD ITSELF NAMES NO ROLE, for the same reason. A standard Page is
rewritten from this app's JSON at every `bench migrate`, so a role list stored
there is a decision an operator makes and then silently loses. `onboarding_context`
answers `may_enrol: false` with the reason instead, and the page renders the
sentence rather than a broken form. Desk access is off on both phone-only roles,
so "any Desk user" is already a much smaller set than it sounds.

────────────────────────────────────────────────────────────────────────────
THE PRE-FLIGHT, WHICH IS THE ONLY THING HERE THAT IS NOT A PASS-THROUGH
────────────────────────────────────────────────────────────────────────────

`generate_mobile_login_qr` refuses in two ways that have nothing to do with the
person being enrolled: a bench with no QR encoder, and a site whose `public_url`
is empty or is not HTTPS. Both are true or false before anybody types a name.

Run in tool order — create the account, then draw the card — either one leaves a
worker HALF ENROLLED: the login exists, the credential exists, and the person
standing at the desk has nothing to scan. Recovering from that means finding a
different screen, which is the thing this page exists to spare somebody.

So `enrolment_blockers` asks both questions FIRST and refuses before a single
row is written. It is a pure function over two facts so the page can render the
same sentence in a banner at load time, greying the button out before anybody
fills the form in rather than after.

The residual case — the encoder present at page load and gone by the time the
button was pressed, or a race on the settings field — is still handled, and
handled as a HALF SUCCESS rather than as an error: the account was made, it is
reported as made, and the failure to draw the card comes back in `qr_error` with
the Regenerate button pointed at it. Reporting that as a failure would invite
somebody to press the button again and meet "this user already exists".

────────────────────────────────────────────────────────────────────────────
WHAT DOES NOT COME BACK IN THE JSON
────────────────────────────────────────────────────────────────────────────

`api_secret`. `create_mobile_user` returns it — the tool's caller has nowhere
else to get it — and this page has the PNG, which already carries it. Putting
the raw secret in the response as well would put a live credential in the
browser's network log, in devtools, and in whatever the tab's memory gets
swapped to, to no end: nobody types this one in.

The card itself is a live credential and says so in print, on the card, where
the person handling it will read it.

The operator-facing half of all this is `docs/mobile-onboarding.md`.
"""

from __future__ import annotations

import html
import json

import frappe

from . import roles
from .errors import ToolError
from .render import qr

GRANT = "Mobile Access Grant"
USER = "User"
COMPANY = "Company"

#: Where the page answers. Spelled once so the test can hold the Page record,
#: the JS and this module to the same route.
PAGE_ROUTE = "mobile-onboarding"
PAGE_TITLE = "Mobile Onboarding"

#: The longest a card may stay valid to enrol with. A MIRROR of the ceiling
#: `generate_mobile_login_qr` enforces, and not a second enforcement: it is here
#: only so the form's number box can stop at the same place the tool would have
#: refused at, one round trip earlier. The tool remains the thing that says no.
MAX_EXPIRY_HOURS = 168

#: What a card is printed at. 55mm of QR is what a phone reads from a hand's
#: distance without anybody leaning in, and the rest of the card is sized around
#: it rather than the other way round.
CARD_WIDTH_MM = 100
QR_SIZE_MM = 55


# ── pure helpers (no site, no session; unit-testable without a bench) ────────
def _esc(value) -> str:
	return html.escape(str(value if value is not None else ""), quote=True)


def enrolment_blockers(endpoint_url: str, encoder_available: bool) -> list:
	"""Everything that would make **Create & Generate QR** fail for reasons that
	are not about the worker. Empty means the page may enrol.

	A PURE FUNCTION over the two facts, so the same list drives the banner at
	page load and the refusal at submit time. Each entry carries a `code` the
	page can key on, the sentence a person reads, and — where there is one — the
	exact thing to go and do.

	The sentences are the tool's own, shortened. A pre-flight that invented its
	own wording would drift from the refusal it is predicting, and the day it
	drifted would be the day somebody fixed the wrong thing.
	"""
	found = []
	if not encoder_available:
		found.append(
			{
				"code": "NO_QR_ENCODER",
				"message": (
					"This bench has no QR encoder, so no login card can be drawn. Everything "
					"else about enrolment works — the account, the scoping and the credential "
					"— but the worker would have nothing to scan."
				),
				"fix": qr.REQUIRES,
			}
		)

	url = str(endpoint_url or "").strip()
	if not url:
		found.append(
			{
				"code": "NO_PUBLIC_URL",
				"message": (
					"This site does not know its own public URL, so a card would point the phone at nothing."
				),
				"fix": (
					"Fill in Public URL on ERPNext MCP Settings with the address the phones "
					"reach — the Tailscale Funnel one, https://<host>.<tailnet>.ts.net. "
					"get_tailscale_funnel_config reports what this machine is actually serving."
				),
			}
		)
	elif not url.lower().startswith("https://"):
		found.append(
			{
				"code": "ENDPOINT_NOT_HTTPS",
				"message": (
					f"The endpoint is {url}, which is not HTTPS. The card carries a live "
					"credential; encoding one for a plaintext endpoint would put it on the "
					"wire in the clear at every call, forever."
				),
				"fix": "Fix Public URL on ERPNext MCP Settings so it starts with https://.",
			}
		)
	return found


CARD_CSS = """
@page { size: Letter; margin: 14mm; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       color: #1f272e; margin: 0; padding: 0 0 12mm; background: #fff; }
.mo-bar { padding: 10px 14px; border-bottom: 1px solid #d9dfe4; background: #f7f9fa;
          font-size: 12px; display: flex; gap: 14px; align-items: center; }
.mo-bar button { font: inherit; padding: 5px 14px; cursor: pointer; }
.mo-card { width: %(width)smm; margin: 10mm auto 0; border: 1.5px solid #1f272e;
           border-radius: 3mm; padding: 7mm 7mm 6mm; text-align: center;
           page-break-inside: avoid; }
.mo-kind { font-size: 8pt; letter-spacing: .16em; text-transform: uppercase; color: #6b7a86; }
.mo-name { font-size: 17pt; font-weight: 700; margin: 2mm 0 0; }
.mo-user { font-size: 10pt; color: #47555f; word-break: break-all; }
.mo-role { font-size: 9.5pt; margin-top: 1.5mm; }
.mo-qr { width: %(qr)smm; height: %(qr)smm; margin: 5mm auto 0; display: block; }
.mo-noqr { margin: 5mm 0 0; padding: 6mm; border: 1px dashed #b3bfc7; font-size: 9pt;
           color: #8a2f2f; }
.mo-scan { font-size: 10pt; font-weight: 600; margin-top: 4mm; }
.mo-expiry { font-size: 10pt; margin-top: 1.5mm; }
.mo-meta { font-size: 8pt; color: #6b7a86; margin-top: 4mm; text-align: left;
           border-top: 1px solid #e3e8eb; padding-top: 3mm; line-height: 1.5; }
.mo-meta b { color: #47555f; font-weight: 600; }
.mo-warn { font-size: 8.5pt; margin-top: 4mm; padding: 3mm; text-align: left;
           border: 1px solid #d8b4b4; background: #fdf6f6; color: #7a2626; line-height: 1.45; }
@media print { .no-print { display: none; } .mo-card { margin-top: 0; } }
""" % {"width": CARD_WIDTH_MM, "qr": QR_SIZE_MM}


def card_html(card: dict) -> str:
	"""One enrolment card. A PURE FUNCTION — no site needed.

	THE NAME AND THE EMAIL ARE BOTH PRINTED, and that is not redundancy: an
	office that enrols six people in a morning has six of these on a desk, and
	`Maria R.` twice with different accounts is exactly the mix-up that ends with
	somebody scanning a card that scopes them to the wrong entity.

	A CARD WITH NO SYMBOL STILL PRINTS, saying so where the symbol would have
	been. `badge_sheet` argues the same case for a badge: the failure that
	matters is the one where somebody collects the paper and does not notice
	what is missing.
	"""
	png = str(card.get("png_base64") or "")
	entities = card.get("entity_access") or []
	if isinstance(entities, str):
		entities = [entities]

	parts = [
		'<div class="mo-card">',
		'<div class="mo-kind">Farm Ops &mdash; phone enrolment</div>',
		f'<div class="mo-name">{_esc(card.get("full_name") or card.get("user"))}</div>',
		f'<div class="mo-user">{_esc(card.get("user"))}</div>',
		f'<div class="mo-role">{_esc(card.get("role"))} &middot; '
		f"{_esc(', '.join(str(name) for name in entities))}</div>",
	]
	if png:
		parts.append(f'<img class="mo-qr" src="data:image/png;base64,{_esc(png)}" alt="">')
	else:
		parts.append(
			'<div class="mo-noqr"><b>No QR code was drawn for this card.</b><br>'
			f"{_esc(card.get('qr_error') or 'The site could not encode one.')}</div>"
		)
	parts.append(
		'<div class="mo-scan">Open Farm Ops on the phone and scan this.</div>'
		f'<div class="mo-expiry">Valid to enrol until <b>{_esc(card.get("expires_at"))}</b></div>'
	)

	meta = [f"<b>Server</b> {_esc(card.get('endpoint') or card.get('endpoint_url'))}"]
	if card.get("employee"):
		meta.append(f"<b>Employee</b> {_esc(card.get('employee'))}")
	if card.get("preferred_company"):
		meta.append(f"<b>Opens on</b> {_esc(card.get('preferred_company'))}")
	parts.append('<div class="mo-meta">' + "<br>".join(meta) + "</div>")

	parts.append(
		'<div class="mo-warn"><b>This card is a live credential.</b> Anybody who '
		"photographs it has this account until the token is replaced. Let it be scanned, "
		"do not send it in a message, and destroy it afterwards &mdash; it stops working "
		"for enrolment at the time above either way.</div>"
	)
	parts.append("</div>")
	return "".join(parts)


def card_document(card: dict) -> str:
	"""The whole printable page around one card, as a string. Pure.

	Handed to the browser as a blob for the reason `badge_list_action` gives at
	length: a document written into `about:blank` with `document.write` prints
	BLANK, because the print path re-reads the document's own URL and
	`about:blank` has nothing behind it.
	"""
	who = str(card.get("full_name") or card.get("user") or "").strip()
	title = f"Farm Ops enrolment — {who}" if who else "Farm Ops enrolment"
	bar = (
		'<div class="mo-bar no-print">'
		'<button type="button" onclick="window.print()">Print</button>'
		"<span>One enrolment card. Hand it to the worker, watch them scan it, "
		"then destroy it.</span>"
		"</div>"
	)
	return (
		"<!doctype html>\n"
		'<html><head><meta charset="utf-8">'
		f"<title>{_esc(title)}</title>"
		f"<style>{CARD_CSS}</style>"
		"</head><body>" + bar + card_html(card) + "</body></html>"
	)


# ── the session gate ────────────────────────────────────────────────────────
def _may(doctype: str, ptype: str) -> bool:
	"""`frappe.has_permission`, never raising. A permission check that threw
	would take the page's own banner down with it."""
	try:
		return bool(frappe.has_permission(doctype, ptype))
	except Exception:  # pragma: no cover - a site mid-migrate with no meta
		return False


def permission_state() -> dict:
	"""Who the caller is allowed to be on this page, and the sentence if not.

	Returned rather than thrown so `onboarding_context` can render a page that
	explains itself. The mutating methods below still throw.
	"""
	may_read = _may(GRANT, "read")
	may_grant = _may(GRANT, "create")
	may_user = _may(USER, "create")
	missing = []
	if not may_grant:
		missing.append(f"create permission on {GRANT}")
	if not may_user:
		missing.append(f"create permission on {USER}")
	return {
		"may_read": may_read,
		"may_enrol": bool(may_grant and may_user),
		"missing": missing,
		"note": (
			""
			if not missing
			else (
				"Enrolling somebody makes a login and writes this app's access register, so "
				"it needs " + " and ".join(missing) + ". Both ship System-Manager-only. An "
				"operator who wants another role to enrol adds it to those permission tables "
				"in the Desk — this page follows them and holds no role list of its own."
			)
		),
	}


def _require_read() -> None:
	if not _may(GRANT, "read"):
		frappe.throw(
			frappe._("You do not have permission to read {0}.").format(GRANT),
			title=frappe._(PAGE_TITLE),
		)


def _require_enrolment() -> None:
	state = permission_state()
	if not state["may_enrol"]:
		frappe.throw(state["note"], title=frappe._(PAGE_TITLE))


# ── argument coercion ───────────────────────────────────────────────────────
def _as_list(value) -> list:
	"""A JS array, a JSON string of one, or a comma/newline separated string.

	`frappe.call` posts a JS array as a JSON string, which is the same coercion
	`badge_sheet` and `asset_tag_sheet` both do at their own front doors.
	"""
	if value is None:
		return []
	if isinstance(value, (list, tuple)):
		return [str(part).strip() for part in value if str(part).strip()]
	text = str(value).strip()
	if not text:
		return []
	if text.startswith("["):
		try:
			parsed = json.loads(text)
		except ValueError:
			frappe.throw(
				frappe._("The list of companies could not be read."),
				title=frappe._(PAGE_TITLE),
			)
			return []  # pragma: no cover - frappe.throw does not return
		return [str(part).strip() for part in parsed if str(part).strip()]
	return [part.strip() for part in text.replace("\n", ",").split(",") if part.strip()]


def _hours(value, default: int) -> int:
	"""The form's number, or the tool's default. Out of range is left to the tool."""
	if value in (None, ""):
		return int(default)
	try:
		return int(frappe.utils.cint(value))
	except Exception:
		return int(default)


def _endpoint(mobile) -> str:
	try:
		return str(mobile._mobile_base_url({}) or "")
	except Exception:  # pragma: no cover - settings unreadable
		return ""


def _preflight(mobile) -> None:
	"""Refuse BEFORE anything is written. See the module docstring."""
	blockers = enrolment_blockers(_endpoint(mobile), qr.available())
	if not blockers:
		return
	lines = "<br><br>".join(f"{entry['message']}<br><i>{entry['fix']}</i>" for entry in blockers)
	frappe.throw(
		frappe._("No account was created.") + "<br><br>" + lines,
		title=frappe._(PAGE_TITLE),
	)


def _draw(mobile, user: str, hours: int, rotate: bool) -> tuple:
	"""The card, or the sentence saying why there is none. Never raises ToolError.

	A HALF SUCCESS IS REPORTED AS ONE. By the time this runs on the create path
	the account exists, and turning a drawing failure into an error would invite
	somebody to press the button again and meet "this user already exists".
	"""
	try:
		result = mobile.generate_mobile_login_qr(
			{"user": user, "expiry_hours": hours, "rotate_token": bool(rotate)}
		)
	except ToolError as error:
		return None, str(error)
	data = result.data or {}
	return (
		{
			"png_base64": data.get("png_base64"),
			"pixels": data.get("pixels"),
			"expires_at": data.get("expires_at"),
			"expiry_hours": data.get("expiry_hours"),
			"endpoint": data.get("endpoint"),
			"token_rotated": bool(data.get("token_rotated")),
			"security_note": data.get("security_note"),
			"summary": result.summary,
		},
		None,
	)


def _card(account: dict, drawn: dict | None, qr_error: str | None, endpoint: str) -> dict:
	"""One dict, in the shape `card_html` reads, from whichever halves arrived."""
	drawn = drawn or {}
	return {
		"user": account.get("user"),
		"full_name": account.get("full_name"),
		"role": account.get("role"),
		"entity_access": account.get("entity_access") or [],
		"preferred_company": account.get("preferred_company"),
		"employee": account.get("employee"),
		"endpoint": drawn.get("endpoint") or endpoint,
		"endpoint_url": endpoint,
		"expires_at": drawn.get("expires_at"),
		"png_base64": drawn.get("png_base64"),
		"qr_error": qr_error,
	}


# ── the four whitelisted methods ────────────────────────────────────────────
@frappe.whitelist()
def onboarding_context() -> dict:
	"""Everything the page needs before anybody types: roles, entities, readiness.

	ASKED AT LOAD TIME AND NOT AT SUBMIT TIME, which is the whole point. A site
	with no public URL cannot enrol anybody, and finding that out after filling
	in a name, an email and a role is finding it out in the worst order.
	"""
	from .tools import mobile

	state = permission_state()
	endpoint = _endpoint(mobile)
	encoder = qr.available()

	try:
		rows = frappe.get_list(COMPANY, fields=["name"], order_by="name") or []
		companies = [row.get("name") for row in rows]
	except Exception:
		# A caller with no Company read permission gets an empty picker and the
		# tool's own "that is not a Company on this site" if they post one anyway.
		companies = []
	companies = [name for name in companies if name]

	return {
		"page_route": PAGE_ROUTE,
		"may_read": state["may_read"],
		"may_enrol": state["may_enrol"],
		"permission_note": state["note"],
		"roles": [roles.describe_role(spec, include_permissions=False) for spec in roles.ROLE_SPECS],
		"job_titles": roles.job_titles(),
		"companies": companies,
		"endpoint_url": endpoint,
		"encoder_available": bool(encoder),
		"blockers": enrolment_blockers(endpoint, encoder),
		"default_expiry_hours": mobile.DEFAULT_QR_HOURS,
		"max_expiry_hours": MAX_EXPIRY_HOURS,
	}


@frappe.whitelist(methods=["POST"])
def create_and_enrol(
	full_name=None,
	email=None,
	role=None,
	companies=None,
	expiry_hours=None,
	notes=None,
	update_existing=0,
):
	"""**Create & Generate QR** — the account, the scoping, the credential, the card.

	Two tool calls and nothing else. `ToolError` becomes `frappe.throw` for the
	reason `api/gis.speaks_frappe` gives: raised out of a whitelisted method it
	is an HTTP 500 and a traceback in the console, and the sentence the tool
	wrote — the one naming which company does not exist, or that this person is
	already enrolled — never reaches the person who needs it. Anything that is
	NOT a ToolError propagates and still reaches the Error Log, because that is
	a bug.
	"""
	from .tools import mobile

	_require_enrolment()
	_preflight(mobile)

	arguments = {
		"email": str(email or "").strip(),
		"full_name": str(full_name or "").strip(),
		"role": str(role or "").strip(),
		"entity_access": _as_list(companies),
		"notes": str(notes or "").strip(),
		"update_existing": bool(frappe.utils.cint(update_existing)),
	}
	try:
		created = mobile.create_mobile_user(arguments)
	except ToolError as error:
		frappe.throw(str(error), title=frappe._(PAGE_TITLE))
		return None  # pragma: no cover - frappe.throw does not return

	account = created.data or {}
	# ONE ROTATION AND NOT TWO. `create_mobile_user` mints a credential for a new
	# account, so the card prints THAT one; asking for another a millisecond
	# later would issue two secrets to hand over one, and the first would be
	# dead before it was ever on paper. An account that was only re-scoped kept
	# its old credential by design, so the card needs a fresh one.
	rotate = not account.get("api_key")
	hours = _hours(expiry_hours, mobile.DEFAULT_QR_HOURS)
	drawn, qr_error = _draw(mobile, account.get("user") or arguments["email"], hours, rotate)

	card = _card(account, drawn, qr_error, _endpoint(mobile))
	return {
		"user": account.get("user"),
		"created": bool(account.get("created")),
		"updated": bool(account.get("updated")),
		"full_name": account.get("full_name"),
		"role": account.get("role"),
		"role_summary": account.get("role_summary"),
		"entity_access": account.get("entity_access") or [],
		"preferred_company": account.get("preferred_company"),
		"employee": account.get("employee"),
		"desk_access": bool(account.get("desk_access")),
		"companion_roles_missing": account.get("companion_roles_missing") or [],
		"qr": drawn,
		"qr_error": qr_error,
		"card_html": card_document(card),
		"summary": created.summary,
	}


@frappe.whitelist(methods=["POST"])
def regenerate_qr(user=None, expiry_hours=None, rotate_token=1):
	"""**Regenerate QR** — a second card for somebody already enrolled.

	The re-enrolment path: a phone replaced, a phone lost, a token the idle sweep
	took away, or a card that expired on a desk before anybody scanned it.

	`rotate_token` DEFAULTS TO TRUE, matching the tool. Re-printing the existing
	credential leaves every earlier photograph of it working, and the ordinary
	reason somebody asks for a second card is that the first one is somewhere
	they can no longer account for. Untick it to re-print a card for a phone
	that is still working.
	"""
	from .tools import mobile

	_require_enrolment()
	_preflight(mobile)

	email = str(user or "").strip().lower()
	if not email:
		frappe.throw(frappe._("Name the account to issue a card for."), title=frappe._(PAGE_TITLE))

	hours = _hours(expiry_hours, mobile.DEFAULT_QR_HOURS)
	drawn, qr_error = _draw(mobile, email, hours, bool(frappe.utils.cint(rotate_token)))
	if qr_error:
		# NOTHING WAS CREATED HERE, so unlike the create path there is no half
		# success to preserve and the honest answer is a refusal the person can act on.
		frappe.throw(qr_error, title=frappe._(PAGE_TITLE))

	grant = mobile._grant_row(email)
	account = {
		"user": email,
		"full_name": grant.get("full_name"),
		"role": grant.get("mobile_role"),
		"preferred_company": grant.get("preferred_company"),
		"entity_access": roles.companies_for(email),
		"employee": mobile._employee_for(email) or None,
	}
	card = _card(account, drawn, None, _endpoint(mobile))
	return {
		"user": email,
		"qr": drawn,
		"qr_error": None,
		"card_html": card_document(card),
		"summary": (drawn or {}).get("summary"),
	}


@frappe.whitelist()
def mobile_users(company=None, include_revoked=0):
	"""The roster the page lists: who is enrolled, on what, and what is wrong.

	A THIN WRAPPER. `list_mobile_users` does the work, including the drift checks
	— an account with no User Permission on Company, a grant that disagrees with
	the live scoping, a revoked account whose token still answers. Those are the
	rows worth a manager's attention and they come through unchanged, in
	`concerns`.
	"""
	from .tools import mobile

	_require_read()
	try:
		result = mobile.list_mobile_users(
			{
				"company": str(company or "").strip(),
				"include_revoked": bool(frappe.utils.cint(include_revoked)),
			}
		)
	except ToolError as error:
		frappe.throw(str(error), title=frappe._(PAGE_TITLE))
		return None  # pragma: no cover - frappe.throw does not return

	data = result.data or {}
	users = [
		{
			"user": row.get("user"),
			"full_name": row.get("full_name"),
			"role": row.get("role"),
			"state": row.get("state"),
			"user_enabled": bool(row.get("user_enabled")),
			"has_live_token": bool(row.get("has_live_token")),
			"entity_access": row.get("entity_access") or [],
			"preferred_company": row.get("preferred_company"),
			"last_seen_on": row.get("last_seen_on"),
			"last_qr_issued_on": row.get("last_qr_issued_on"),
			"token_issued_on": row.get("token_issued_on"),
			"token_review_due": row.get("token_review_due"),
			"token_review_overdue": bool(row.get("token_review_overdue")),
			"employee": row.get("employee"),
			"concerns": row.get("concerns") or [],
		}
		for row in data.get("users") or []
	]
	return {
		"count": data.get("count") or 0,
		"needing_attention": data.get("needing_attention") or 0,
		"company_filter": data.get("company_filter"),
		"users": users,
		"summary": result.summary,
	}


__all__ = (
	"CARD_CSS",
	"MAX_EXPIRY_HOURS",
	"PAGE_ROUTE",
	"PAGE_TITLE",
	"card_document",
	"card_html",
	"create_and_enrol",
	"enrolment_blockers",
	"mobile_users",
	"onboarding_context",
	"permission_state",
	"regenerate_qr",
)
